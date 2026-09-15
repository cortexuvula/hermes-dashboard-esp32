#!/usr/bin/env python3
"""
Layout envelope checker for the ESP32-C6 round dashboard (hermes-dash-esp32).

Two properties are taken FROM THE SKETCH, so the checker cannot pass while the
firmware drifts:
  1. POSITIONS  — parsed from the #define layout constants.
  2. TYPOGRAPHY — scanned from the setFont(...) call immediately preceding each
                  draw call, so changing a font in the sketch changes this check.

Every drawn element is modelled with a WORST-CASE string (longest version,
bounded bot count, 6-char token totals, "--" states) and
tested against the round aperture:  r <= ROUND_R  where ROUND_R is parsed from
the sketch (the project's own "usable radius inside round glass").

THIRD PROPERTY — ELEMENT VS ELEMENT (the aperture test cannot see it):
  A layout can keep every element inside the circle and still draw two of them on
  top of each other; that shipped once ("text overlapping" on STATUS and HEALTH,
  reported from the hardware).  So every element also gets a VISUAL box — glyph
  band height (GLYPH_H), not the em box, which would make adjacent lines look
  like collisions — and all same-page pairs are intersected; anything overlapping
  by more than MIN_GAP_PX fails.  The modelled strings are data-shape aware:
  the footer is server-driven, so the sketch BOUNDS it and WORST["FOOTER"] models
  exactly that bound.

Envelope formula (center CX,CY):
  centered text : r = hypot(w/2, |y - CY| + h/2)
  offset text   : r = hypot(|x - CX| + w/2, |y - CY| + h/2)
  centered rect : r = hypot(max(|x-CX|,|x+w-CX|), max(|y-CY|,|y+h-CY|))
  circle        : r = hypot(|cx-CX| + rad, |cy-CY| + rad)

#3/F3: COMPLETENESS ASSERTION
  - Broad scanner: matches ALL LovyanGFX draw/fill/push/print/setPixel calls
    (not just a whitelist of known APIs).  An unscanned API cannot exist silently.
  - Positional claiming: each claim must correspond to exactly ONE draw line,
    and no draw line may be claimed twice.  A duplicated or moved element FAILS.

Usage:
  python3 tools/check_layout.py                  # current sketch: expect PASS (exit 0)
                                                 # (aperture + completeness + overlap)
  python3 tools/check_layout.py --prefix         # original pre-audit coordinates:
                                                 # must FAIL (exit 1) — proves the gate
                                                 # detects the A11 defects
  python3 tools/check_layout.py --overlap-prefix # the layout as reported on hardware
                                                 # (SESSIONS 100, STATE 118, AUTH 134,
                                                 # dot dx 50): must FAIL (exit 1) —
                                                 # proves the overlap check detects the
                                                 # "text overlapping" defect it exists for
  python3 tools/check_layout.py --sketch P       # check a different sketch file
                                                 # (used to prove drift is detected)
"""
import math
import re
import sys
from pathlib import Path

# Font metrics: LovyanGFX built-in fonts are fixed-advance for these families.
FONTS = {
    "Font0": (6, 8),
    "Font2": (8, 16),
    "DejaVu24": (13, 24),   # boot screen uses this
    "DejaVu40": (22, 40),   # digit advance; used for the big numbers
}

DEFAULT_SKETCH = Path(__file__).parent.parent / "hermes-dash-esp32" / "hermes-dash-esp32.ino"

# Non-rendering calls to exclude from the draw surface scanner.
# These set state but don't draw at specific coordinates.
NON_RENDERING = {
    "fillScreen",   # clears the whole screen — not a positioned element
    "setFont",
    "setTextColor",
    "setTextDatum",
    "begin",
    "setRotation",
    "end",
}

# Broad regex: any tft.* call that could place pixels on screen.
# Matches: draw*, fill*, push*, print*, setPixel*
# NOTE: 'tft' is the sketch's DRAW TARGET, which may be a real panel or an offscreen
# canvas sprite (double buffering). It deliberately stays named `tft` at every call site
# so this scan keeps working; see MIN_SCANNED_DRAWS for the guard that makes a rename fail
# loudly instead of passing vacuously.
DRAW_CALL_RE = re.compile(r"tft\.(draw|fill|push|print|setPixel)[A-Za-z0-9_]*\(")

# Floor for the scan above. A renamed/aliased draw target would make DRAW_CALL_RE find
# ZERO calls, and zero found means nothing is unclaimed — the completeness assertion would
# pass on a completely unverified layout. Measured value at the double-buffering commit
# (the scanner counts 40: fillScreen is excluded as non-rendering); raise it when elements
# are added.
MIN_SCANNED_DRAWS = 40


class Sketch:
    """Positions and draw-site fonts read out of the sketch."""

    def __init__(self, path):
        self.path = Path(path)
        self.text = self.path.read_text()
        self.const = {}
        for m in re.finditer(r"#define\s+([A-Z][A-Z0-9_]*)\s+(\d+)", self.text):
            self.const[m.group(1)] = int(m.group(2))
        # (line_no, font, line_text) for every draw call, font = nearest preceding setFont
        self.draws = []
        font = None
        for i, line in enumerate(self.text.splitlines(), 1):
            fm = re.search(r"setFont\(&fonts::(\w+)\)", line)
            if fm:
                font = fm.group(1)
            # F3: broad scanner — catch ALL rendering APIs
            if DRAW_CALL_RE.search(line):
                # Exclude non-rendering calls
                method_match = re.search(r"\.(\w+)\(", line)
                if method_match and method_match.group(1) not in NON_RENDERING:
                    self.draws.append((i, font, line.strip()))

    def val(self, name):
        if name not in self.const:
            raise SystemExit(f"ERROR: #define {name} not found in {self.path}")
        return self.const[name]

    def font_at(self, selector):
        """Font in force at the first draw call whose line contains `selector`."""
        for line_no, font, text in self.draws:
            if selector in text:
                return font, line_no
        raise SystemExit(
            f"ERROR: no draw call referencing {selector!r} found in {self.path} "
            f"— the checker cannot verify this element"
        )


# ── geometry ───────────────────────────────────────────────────────────────
def centered(y, w, h, cx, cy):
    return math.hypot(w / 2.0, abs(y - cy) + h / 2.0)


def offset(x, y, w, h, cx, cy):
    return math.hypot(abs(x - cx) + w / 2.0, abs(y - cy) + h / 2.0)


def rect_r(x, y, w, h, cx, cy):
    return math.hypot(max(abs(x - cx), abs(x + w - cx)),
                      max(abs(y - cy), abs(y + h - cy)))


def circle_r(bx, by, rad, cx, cy):
    return math.hypot(abs(bx - cx) + rad, abs(by - cy) + rad)


def text_w(s, font):
    return len(s) * FONTS[font][0]


def text_h(font):
    return FONTS[font][1]


WORST = {
    "SESSIONS": "SESSIONS",
    "STATE": "DEGRADED",
    "OVERFLOW": "+99",
    "DISK": "DISK 100%",
    "FOOTER": "v0.21.3 99+ bots",   # sketch bounds version to 6 chars + count to 3 ("99+")
    "TK_HEAD": "TOKENS 24H",
    "TK_QUAL": "NEW SESSIONS ONLY",
    "TK_BIG": "12345M",              # 6-char DejaVu40 worst case (staleness shown via dim color, not "?" suffix)
    "TK_IO": "IN 1234M  OUT 12345M?",     # 24H/7D in-out split, worst case incl. stale ?
    "TK_COST": "$9999.99?",               # 7D cost headline
    "TK_CACHE": "CACHE 1234M  $99.99",    # 24H cache + cost
    "TK_CALLS": "CALLS 1234M  SES 12345", # calls / sessions
    "HL_HEAD": "HEALTH",
    "HL_ROW": "PLATFORMS",
    "HL_ERR": "9E",
    "HL_AUTH": "AUTH EXPIRED",
    "HL_HOST": "CPU 100% RAM 100%?",      # single space keeps it inside R=80
    # boot/error/wifi screens
    "BOOT_HERMES": "HERMES",
    "BOOT_DASH": "Dashboard",
    "ERR_OFFLINE": "OFFLINE",
    "WIFI_CONNECT": "Connecting",
    "WIFI_SSID": "WiFi...",
}


def check(sk, prefix=False):
    """Run every element check. Returns (results, cx, cy, limit, claimed_lines)."""
    cx = sk.val("CENTER_X")
    cy = sk.val("CENTER_Y")
    limit = sk.val("ROUND_R")
    res = []
    claimed = {}  # F3: line_no -> claimant name (positional, 1:1)

    def add(name, r, note="", line_no=None):
        res.append((name, r, r <= limit, note))
        if line_no is not None:
            if line_no in claimed:
                # F3: duplicate claim = FAIL (catches BYPASS B)
                res.append((f"DUPLICATE CLAIM at line {line_no}", 999, False,
                            f"claimed by both {claimed[line_no]!r} and {name!r}"))
            else:
                claimed[line_no] = name

    def font_check(name, selector, expected):
        """Verify the sketch's draw site uses the font this check assumes."""
        actual, line_no = sk.font_at(selector)
        if actual is None:
            raise SystemExit(f"ERROR: {name}: no setFont found before {selector} (line {line_no})")
        if actual != expected:
            raise SystemExit(
                f"ERROR: {name}: FONT MISMATCH — {selector} draws with font::{actual} "
                f"(line {line_no}) but this check models font::{expected}. "
                f"Update the checker for the new font."
            )
        return actual, line_no

    if prefix:
        # Original pre-audit coordinates (main @ 96de3b5) for the three elements the
        # audit found outside the glass. The gate must FAIL here.
        bar_y, bar_w, bar_h = cy + 54, 140, 6          # was CENTER_Y+ROUND_R-26 = 140
        add("disk bar (PRE-FIX)", rect_r(cx - bar_w // 2, bar_y, bar_w, bar_h, cx, cy),
            "was x=90..230, y=140..146")
        foot_y = cy + 76                               # was CENTER_Y+ROUND_R-4 = 162
        f, ln = font_check("footer (PRE-FIX)", "drawString(footer", "Font0")
        add("footer (PRE-FIX)", centered(foot_y, text_w(WORST["FOOTER"], f), text_h(f), cx, cy),
            "baseline y=162 needs ±45px, circle allows ±40px", line_no=ln)
        add("update dot (PRE-FIX)", circle_r(cx + 55, foot_y, 3, cx, cy),
            "was (215,162) — outside the glass entirely")
        return res, cx, cy, limit, claimed

    # ── STATUS page ────────────────────────────────────────────────────────
    ring_r = sk.val("STATUS_RING_R")
    dot_r = sk.val("STATUS_RING_DOT_R")
    # Ring dots: there are exactly 8 fillCircle calls with STATUS_RING_DOT_R
    # Claim them positionally: each dot gets the next unclaimed fillCircle with that pattern
    ring_dot_lines = [ln for ln, f, t in sk.draws
                      if "fillCircle" in t and "STATUS_RING_DOT_R" in t]
    for i in range(8):
        angle = 150 + i * (30 - 150) / 7
        rad = math.radians(angle)
        dx = int(ring_r * math.cos(rad))
        dy = int(ring_r * math.sin(rad))
        if i < len(ring_dot_lines):
            add(f"ring dot {i}", circle_r(cx + dx, cy - dy, dot_r, cx, cy),
                line_no=ring_dot_lines[i])
        else:
            add(f"ring dot {i}", circle_r(cx + dx, cy - dy, dot_r, cx, cy))

    f, ln = font_check("overflow", "STATUS_OVERFLOW_DX", "Font0")
    add("overflow +99", offset(cx + sk.val("STATUS_OVERFLOW_DX"),
                              cy - sk.val("STATUS_OVERFLOW_DY"),
                              text_w(WORST["OVERFLOW"], f), text_h(f), cx, cy),
        line_no=ln)

    # Big number: claim the first drawNumber or drawString at STATUS_NUM_Y
    num_candidates = [ln for ln, f, t in sk.draws
                      if ("drawNumber(active_sessions" in t or
                          'drawString("--", CENTER_X, STATUS_NUM_Y' in t)
                      and ln not in claimed]
    f, ln = font_check("big number", "STATUS_NUM_Y", "DejaVu40")
    add("number 999", centered(sk.val("STATUS_NUM_Y"), text_w("999", f), text_h(f), cx, cy),
        line_no=ln)
    # Claim the alternate branch
    for alt_ln in num_candidates:
        if alt_ln != ln:
            claimed[alt_ln] = "number 999 (alt branch)"

    # Sessions label
    sess_candidates = [ln for ln, f, t in sk.draws
                       if ("SESSION" in t and "STATUS_SESS_Y" in t)
                       or 'drawString("", CENTER_X, STATUS_SESS_Y' in t]
    f, ln = font_check("sessions label", "STATUS_SESS_Y", "Font2")
    add("SESSIONS", centered(sk.val("STATUS_SESS_Y"), text_w(WORST["SESSIONS"], f), text_h(f), cx, cy),
        line_no=ln)
    for alt_ln in sess_candidates:
        if alt_ln != ln and alt_ln not in claimed:
            claimed[alt_ln] = "SESSIONS (alt branch)"

    # State label
    state_candidates = [ln for ln, f, t in sk.draws
                        if "STATUS_STATE_Y" in t and "drawString" in t]
    f, ln = font_check("state", "STATUS_STATE_Y", "Font2")
    add("DEGRADED", centered(sk.val("STATUS_STATE_Y"), text_w(WORST["STATE"], f), text_h(f), cx, cy),
        line_no=ln)
    for alt_ln in state_candidates:
        if alt_ln != ln and alt_ln not in claimed:
            claimed[alt_ln] = "state (alt branch)"

    # Disk bar
    bar_y = cy + sk.val("STATUS_DISK_BAR_DY")
    bar_w, bar_h = sk.val("STATUS_DISK_BAR_W"), sk.val("STATUS_DISK_BAR_H")
    for ln, f, t in sk.draws:
        if ("drawRect(bar_x, bar_y" in t or "fillRect(bar_x + 1" in t) and ln not in claimed:
            claimed[ln] = "disk bar"
    add("disk bar", rect_r(cx - bar_w // 2, bar_y, bar_w, bar_h, cx, cy))

    # Disk label
    disk_candidates = [ln for ln, f, t in sk.draws
                       if "DISK" in t and "STATUS_DISK_LBL_DY" in t]
    f, ln = font_check("disk label", "STATUS_DISK_LBL_DY", "Font0")
    add("DISK 100%", centered(bar_y + sk.val("STATUS_DISK_LBL_DY"),
                              text_w(WORST["DISK"], f), text_h(f), cx, cy),
        line_no=ln)
    for alt_ln in disk_candidates:
        if alt_ln != ln and alt_ln not in claimed:
            claimed[alt_ln] = "DISK label (alt branch)"

    # Footer
    foot_y = cy + sk.val("STATUS_FOOTER_DY")
    f, ln = font_check("footer", "drawString(footer", "Font0")
    add("footer", centered(foot_y, text_w(WORST["FOOTER"], f), text_h(f), cx, cy),
        line_no=ln)
    # Update dot
    for ln, f, t in sk.draws:
        if "fillCircle(CENTER_X + STATUS_DOT_DX, footer_y + STATUS_DOT_DY" in t and ln not in claimed:
            claimed[ln] = "update dot"
            break
    add("update dot", circle_r(cx + sk.val("STATUS_DOT_DX"), foot_y + sk.val("STATUS_DOT_DY"), sk.val("STATUS_DOT_R"), cx, cy))

    # ── TOKEN pages (both share the TK_* y positions) ───────────────────────
    f, ln = font_check("tk heading", "TK_HEAD_Y", "Font0")
    add("tk heading", centered(sk.val("TK_HEAD_Y"), text_w(WORST["TK_HEAD"], f), text_h(f), cx, cy),
        line_no=ln)

    f, ln = font_check("tk qualifier", "TK_QUAL_Y", "Font0")
    add("tk qualifier", centered(sk.val("TK_QUAL_Y"), text_w(WORST["TK_QUAL"], f), text_h(f), cx, cy),
        line_no=ln)

    # TK_BIG_Y branches: claim all drawString at TK_BIG_Y positionally
    big_candidates = [ln for ln, f, t in sk.draws if "TK_BIG_Y" in t and "drawString" in t]
    f, ln = font_check("tk big total", "TK_BIG_Y", "DejaVu40")
    add("tk total", centered(sk.val("TK_BIG_Y"), text_w(WORST["TK_BIG"], f), text_h(f), cx, cy),
        line_no=ln)
    for alt_ln in big_candidates:
        if alt_ln != ln and alt_ln not in claimed:
            claimed[alt_ln] = "tk total (alt branch)"

    # TK_DET1_Y: 7D cost (Font2) and 24H io (Font0)
    f, ln = font_check("tk cost (7D)", "drawString(cost_s", "Font2")
    add("tk det1 cost/7D", centered(sk.val("TK_DET1_Y"), text_w(WORST["TK_COST"], f), text_h(f), cx, cy),
        line_no=ln)
    f, ln = font_check("tk io (24H)", "drawString(io, CENTER_X, TK_DET1_Y", "Font0")
    add("tk det1 io/24H", centered(sk.val("TK_DET1_Y"), text_w(WORST["TK_IO"], f), text_h(f), cx, cy),
        line_no=ln)

    # TK_DET2_Y
    f, ln = font_check("tk calls (7D)", "drawString(cs, CENTER_X, TK_DET2_Y", "Font0")
    add("tk det2 calls/7D", centered(sk.val("TK_DET2_Y"), text_w(WORST["TK_CALLS"], f), text_h(f), cx, cy),
        line_no=ln)
    f, ln = font_check("tk cache (24H)", "drawString(cc", "Font0")
    add("tk det2 cache/24H", centered(sk.val("TK_DET2_Y"), text_w(WORST["TK_CACHE"], f), text_h(f), cx, cy),
        line_no=ln)

    # TK_DET3_Y
    f, ln = font_check("tk io (7D)", "drawString(io, CENTER_X, TK_DET3_Y", "Font0")
    add("tk det3 io/7D", centered(sk.val("TK_DET3_Y"), text_w(WORST["TK_IO"], f), text_h(f), cx, cy),
        line_no=ln)
    f, ln = font_check("tk calls (24H)", "drawString(cs, CENTER_X, TK_DET3_Y", "Font0")
    add("tk det3 calls/24H", centered(sk.val("TK_DET3_Y"), text_w(WORST["TK_CALLS"], f), text_h(f), cx, cy),
        line_no=ln)

    # ── HEALTH page ────────────────────────────────────────────────
    f, ln = font_check("hl heading", "HL_HEAD_Y", "Font2")
    add("hl heading", centered(sk.val("HL_HEAD_Y"), text_w(WORST["HL_HEAD"], f), text_h(f), cx, cy),
        line_no=ln)

    row_names = ["GATEWAY", "STORAGE", "DASHBOARD", "PLATFORMS"]
    # Find the single drawString(rows_txt line inside the for loop
    rows_draw_lines = [ln for ln, f, t in sk.draws if "drawString(rows_txt" in t]
    hl_dot_lines = [ln for ln, f, t in sk.draws
                    if "fillCircle" in t and "HL_DOT_DX" in t]
    for i, cname in enumerate(["HL_ROW0_Y", "HL_ROW1_Y", "HL_ROW2_Y", "HL_ROW3_Y"]):
        y = sk.val(cname)
        f = "Font2"  # rows are always Font2
        # Claim the shared for-loop draw line once (first iteration only)
        claim_ln = rows_draw_lines[0] if i == 0 and rows_draw_lines else None
        add(f"hl row {i}",
            centered(y, text_w(WORST["HL_ROW"], f), text_h(f), cx, cy),
            f"worst label PLATFORMS; {row_names[i]} drawn here",
            line_no=claim_ln)
        if i < len(hl_dot_lines) and hl_dot_lines[i] not in claimed:
            claimed[hl_dot_lines[i]] = f"hl row {i} dot"
        add(f"hl row {i} dot", circle_r(cx - sk.val("HL_DOT_DX"), y, sk.val("HL_DOT_R"), cx, cy))

    f, ln = font_check("hl error", "HL_ERR_DX", "Font0")
    add("hl error 9E", offset(cx + sk.val("HL_ERR_DX"), sk.val("HL_ERR_Y"),
                              text_w(WORST["HL_ERR"], f), text_h(f), cx, cy),
        line_no=ln)

    f, ln = font_check("hl auth", "HL_AUTH_Y", "Font2")
    add("hl auth", centered(sk.val("HL_AUTH_Y"), text_w(WORST["HL_AUTH"], f), text_h(f), cx, cy),
        line_no=ln)

    f, ln = font_check("hl host", "HL_HOST_Y", "Font0")
    add("hl host", centered(sk.val("HL_HOST_Y"), text_w(WORST["HL_HOST"], f), text_h(f), cx, cy),
        line_no=ln)

    # ── ERROR SCREEN (show_error) ───────────────────────────
    f, ln = font_check("error offline", "OFFLINE", "Font2")
    add("error OFFLINE", centered(cy, text_w(WORST["ERR_OFFLINE"], f), text_h(f), cx, cy),
        line_no=ln)

    # ── BOOT SCREEN (setup) ────────────────────────────────
    f, ln = font_check("boot HERMES", "HERMES", "DejaVu24")
    add("boot HERMES", centered(cy - 40, text_w(WORST["BOOT_HERMES"], f), text_h(f), cx, cy),
        line_no=ln)
    f, ln = font_check("boot Dashboard", "Dashboard", "Font2")
    add("boot Dashboard", centered(cy + 10, text_w(WORST["BOOT_DASH"], f), text_h(f), cx, cy),
        line_no=ln)

    # ── WIFI CONNECT SCREEN ────────────────────────────
    f, ln = font_check("wifi Connecting", "Connecting", "Font2")
    add("wifi Connecting", centered(cy - 20, text_w(WORST["WIFI_CONNECT"], f), text_h(f), cx, cy),
        line_no=ln)
    f, ln = font_check("wifi WiFi...", "WiFi...", "Font2")
    add("wifi WiFi...", centered(cy + 10, text_w(WORST["WIFI_SSID"], f), text_h(f), cx, cy),
        line_no=ln)
    # Claim the drawChar dots
    for ln, f, t in sk.draws:
        if "drawChar" in t and ln not in claimed:
            claimed[ln] = "wifi dots"

    return res, cx, cy, limit, claimed


# Visual glyph bands. Calibrated against PHOTOGRAPHS of the panel (this model was wrong
# twice before that): MC_DATUM centres the full font CELL, and the built-in bitmap fonts
# fill it, so Font0 = 8 px and Font2 = 16 px — not the 7/13 previously estimated from the
# em box. Those estimates erred SMALL, which is the direction that hides a real collision.
# The scalable faces get their drawn cap band (DejaVu40 digits ~29 px), not their line
# height, which is far larger and would demand impossible spacing.
GLYPH_H = {"Font0": 8, "Font2": 16, "DejaVu24": 20, "DejaVu40": 29}

# Minimum SEPARATION (px) between two elements, measured on both axes. Calibrated against
# hardware photographs of the panel, which showed that (a) MC_DATUM centres the FULL font
# CELL and these bitmap fonts fill it, so the real bands are Font0 8 / Font2 16 px — not
# the 7/13 estimated from the em box; and (b) a gap of 0 px (boxes touching exactly) reads
# as overlapping text on the glass. So the test is "gap >= MIN_GAP", not "overlap > eps":
# the earlier overlap>0.5 form PASSED a touching pair, which is precisely the defect that
# shipped twice and was reported from the hardware.
MIN_GAP_PX = 1.5

# Number of visual boxes the overlap model must produce. The box list is built from the
# same constants as the aperture check but is a SEPARATE list, so this asserts it cannot
# silently diverge: add an element (or a box) and this fails until you reconcile them.
EXPECTED_BOXES = 40


def overlap_boxes(sk, cx, cy, prefix=False):
    """Every drawn element as a visual box, per page, from the parsed constants.

    prefix=True models the layout as REPORTED ON HARDWARE (SESSIONS 100, STATE 118,
    AUTH 134, dot dx 50) — the state of the display before the overlap fix. Used by
    --overlap-prefix, which must FAIL.
    """
    out = []  # (page, name, x0, x1, y0, y1)

    def val(name, normal):
        """Pre-fix override for the four constants involved in the reported defect."""
        if not prefix:
            return sk.val(name)
        return {"STATUS_NUM_Y": 72, "STATUS_SESS_Y": 94, "STATUS_STATE_Y": 110,
                "STATUS_DISK_BAR_DY": 32, "STATUS_DISK_LBL_DY": 10,
                "STATUS_FOOTER_DY": 52, "STATUS_DOT_DX": 54, "STATUS_DOT_DY": 0,
                "HL_HEAD_Y": 26, "HL_ROW0_Y": 48, "HL_ROW1_Y": 70, "HL_ROW2_Y": 92,
                "HL_ROW3_Y": 114, "HL_AUTH_Y": 128, "HL_HOST_Y": 140,
                "TK_DET1_Y": 88}.get(name, sk.val(name))

    def txt(page, name, x, y, s, font):
        w = text_w(s, font)
        h = GLYPH_H.get(font, FONTS[font][1])
        out.append((page, name, x - w / 2.0, x + w / 2.0, y - h / 2.0, y + h / 2.0))

    def rect(page, name, x, y, w, h):
        out.append((page, name, x, x + w, y, y + h))

    def circ(page, name, bx, by, r):
        out.append((page, name, bx - r, bx + r, by - r, by + r))

    # ── STATUS ─────────────────────────────────────────────────────────────
    ring_r, dot_r = sk.val("STATUS_RING_R"), sk.val("STATUS_RING_DOT_R")
    for i in range(8):
        ang = math.radians(150 + i * (30 - 150) / 7)
        circ("STATUS", f"ring dot {i}", cx + int(ring_r * math.cos(ang)),
             cy - int(ring_r * math.sin(ang)), dot_r)
    txt("STATUS", "overflow +99", cx + sk.val("STATUS_OVERFLOW_DX"),
        cy - sk.val("STATUS_OVERFLOW_DY"), WORST["OVERFLOW"], "Font0")
    txt("STATUS", "number", cx, sk.val("STATUS_NUM_Y"), "999", "DejaVu40")
    txt("STATUS", "SESSIONS", cx, val("STATUS_SESS_Y", None), WORST["SESSIONS"], "Font2")
    txt("STATUS", "state", cx, val("STATUS_STATE_Y", None), WORST["STATE"], "Font2")
    bar_y = cy + val("STATUS_DISK_BAR_DY", None)
    bar_w, bar_h = sk.val("STATUS_DISK_BAR_W"), sk.val("STATUS_DISK_BAR_H")
    rect("STATUS", "disk bar", cx - bar_w / 2.0, bar_y, bar_w, bar_h)
    txt("STATUS", "disk label", cx, bar_y + val("STATUS_DISK_LBL_DY", None), WORST["DISK"], "Font0")
    foot_y = cy + val("STATUS_FOOTER_DY", None)
    txt("STATUS", "footer", cx, foot_y, WORST["FOOTER"], "Font0")
    circ("STATUS", "update dot", cx + val("STATUS_DOT_DX", None), foot_y + val("STATUS_DOT_DY", None), sk.val("STATUS_DOT_R"))

    # ── TOKEN 24H and 7D (the two pages share the y positions but not the fonts) ──
    for page, det1 in (("TOKEN 24H", ("io", WORST["TK_IO"], "Font0")),
                       ("TOKEN 7D", ("cost", WORST["TK_COST"], "Font2"))):
        txt(page, "heading", cx, sk.val("TK_HEAD_Y"), WORST["TK_HEAD"], "Font0")
        txt(page, "qualifier", cx, sk.val("TK_QUAL_Y"), WORST["TK_QUAL"], "Font0")
        txt(page, "big total", cx, sk.val("TK_BIG_Y"), WORST["TK_BIG"], "DejaVu40")
        txt(page, f"det1 {det1[0]}", cx, val("TK_DET1_Y", None), det1[1], det1[2])
        txt(page, "det2", cx, sk.val("TK_DET2_Y"),
            WORST["TK_CACHE"] if page == "TOKEN 24H" else WORST["TK_CALLS"],
            "Font0")
        txt(page, "det3", cx, sk.val("TK_DET3_Y"),
            WORST["TK_CALLS"] if page == "TOKEN 24H" else WORST["TK_IO"], "Font0")

    # ── HEALTH ─────────────────────────────────────────────────────────────
    txt("HEALTH", "heading", cx, val("HL_HEAD_Y", None), WORST["HL_HEAD"], "Font2")
    rows = [val(n, None) for n in ("HL_ROW0_Y", "HL_ROW1_Y", "HL_ROW2_Y", "HL_ROW3_Y")]
    for i, y in enumerate(rows):
        txt("HEALTH", f"row {i}", cx, y, WORST["HL_ROW"], "Font2")
        circ("HEALTH", f"row {i} dot", cx - sk.val("HL_DOT_DX"), y, sk.val("HL_DOT_R"))
    txt("HEALTH", "error 9E", cx + sk.val("HL_ERR_DX"), sk.val("HL_ERR_Y"), WORST["HL_ERR"], "Font0")
    txt("HEALTH", "auth", cx, val("HL_AUTH_Y", None), WORST["HL_AUTH"], "Font2")
    txt("HEALTH", "host", cx, val("HL_HOST_Y", None), WORST["HL_HOST"], "Font0")

    return out


def check_overlaps(sk, cx, cy, prefix=False, min_gap=MIN_GAP_PX):
    """Pairwise separation check per page.

    Returns (box_count, [(page, a, b, gap_x, gap_y)]) for every same-page pair that is
    closer than min_gap on BOTH axes. A negative gap means the boxes intersect.
    """
    boxes = overlap_boxes(sk, cx, cy, prefix=prefix)
    bad = []
    for i in range(len(boxes)):
        pi, ni, ax0, ax1, ay0, ay1 = boxes[i]
        for j in range(i + 1, len(boxes)):
            pj, nj, bx0, bx1, by0, by1 = boxes[j]
            if pi != pj:
                continue
            gap_x = max(ax0, bx0) - min(ax1, bx1)
            gap_y = max(ay0, by0) - min(ay1, by1)
            if gap_x < min_gap and gap_y < min_gap:
                bad.append((pi, ni, nj, gap_x, gap_y))
    return len(boxes), bad


def main(argv):
    prefix = "--prefix" in argv
    sketch = DEFAULT_SKETCH
    if "--sketch" in argv:
        sketch = argv[argv.index("--sketch") + 1]

    sk = Sketch(sketch)
    res, cx, cy, limit, claimed = check(sk, prefix=prefix)

    mode = "PRE-FIX coordinates (gate must FAIL)" if prefix else "current sketch"
    print(f"=== LAYOUT ENVELOPE CHECK — {mode} ===")
    print(f"sketch: {sk.path}")
    print(f"center: ({cx},{cy})  limit: R={limit} (parsed from sketch)\n")

    for name, r, ok, note in res:
        line = f"  {name:24s} r={r:6.1f}  {'ok' if ok else 'FAIL'}"
        if note:
            line += f"   ({note})"
        print(line)

    bad = [x for x in res if not x[2]]
    print()
    if prefix:
        if bad:
            print(f"GATE OK — pre-fix layout correctly FAILS ({len(bad)} element(s) outside R={limit}):")
            for name, r, _, _ in bad:
                print(f"  - {name}: r={r:.1f} > {limit}")
            return 1
        print("GATE BROKEN — pre-fix layout PASSED; the check does not detect the A11 defects")
        return 2

    # F3: COMPLETENESS ASSERTION — every draw call must be claimed (positional, 1:1)
    # Guard the scan itself first: a receiver rename makes DRAW_CALL_RE find nothing, and
    # nothing-found trivially satisfies "everything is claimed".
    if len(sk.draws) < MIN_SCANNED_DRAWS:
        print(f"SCAN BROKEN: only {len(sk.draws)} draw call(s) found, expected >= "
              f"{MIN_SCANNED_DRAWS}. The scanner is receiver-locked to 'tft.' — if the draw "
              f"target was renamed or aliased, update DRAW_CALL_RE and this floor together "
              f"(otherwise this gate passes on an unverified layout).")
        return 1

    unclaimed = []
    for line_no, font, text in sk.draws:
        if line_no not in claimed:
            unclaimed.append((line_no, text))

    if unclaimed:
        print(f"COMPLETENESS FAILURE: {len(unclaimed)} unclaimed draw call(s):")
        for line_no, text in unclaimed:
            print(f"  line {line_no}: {text}")
        return 1

    if bad:
        print(f"FAILED: {len(bad)} element(s) exceed R={limit}")
        for name, r, _, _ in bad:
            print(f"  - {name}: r={r:.1f}")
        return 1

    # OVERLAP CHECK — the aperture gate above cannot see two elements colliding.
    # Drawn on real hardware this is what "text overlapping" looks like.
    n_boxes, overlaps = check_overlaps(sk, cx, cy, prefix=("--overlap-prefix" in argv))

    # --overlap-prefix models the layout as REPORTED on hardware and must FAIL, so the
    # gate's ability to detect this defect class is itself regression-tested.
    if "--overlap-prefix" in argv:
        if not overlaps:
            print("GATE BROKEN — the reported pre-fix layout produced no collision; the "
                  "overlap check cannot detect the defect it exists for.")
            return 1
        print(f"GATE OK — pre-fix layout correctly FAILS ({len(overlaps)} collision(s)):")
        for page, a, b, gap_x, gap_y in overlaps:
            print(f"  [{page}] {a} <-> {b}   gap {gap_x:.1f}x{gap_y:.1f} px (min {MIN_GAP_PX})")
        return 0

    if n_boxes != EXPECTED_BOXES:
        print(f"MODEL DRIFT: overlap model built {n_boxes} boxes, expected {EXPECTED_BOXES} — "
              f"an element was added to one list and not the other. Reconcile them.")
        return 1

    if overlaps:
        print()
        print(f"OVERLAP FAILURE: {len(overlaps)} element pair(s) collide on screen "
              f"({n_boxes} boxes checked):")
        for page, a, b, gap_x, gap_y in overlaps:
            print(f"  [{page}] {a} <-> {b}   gap {gap_x:.1f}x{gap_y:.1f} px (min {MIN_GAP_PX})")
        return 1

    print(f"PASSED: all {len(res)} elements within R={limit}, all {len(sk.draws)} draw calls claimed, "
          f"{n_boxes} boxes with no collisions")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
