#!/usr/bin/env python3
"""
Layout envelope checker for the ESP32-C6 round dashboard (hermes-dash-esp32).

Two properties are taken FROM THE SKETCH, so the checker cannot pass while the
firmware drifts:
  1. POSITIONS  — parsed from the #define layout constants.
  2. TYPOGRAPHY — scanned from the setFont(...) call immediately preceding each
                  draw call, so changing a font in the sketch changes this check.

Every drawn element is modelled with a WORST-CASE string (longest version,
two-digit bot count, 6-char token totals, "--" states, stale "?" suffixes) and
tested against the round aperture:  r <= ROUND_R  where ROUND_R is parsed from
the sketch (the project's own "usable radius inside round glass").

Envelope formula (center CX,CY):
  centered text : r = hypot(w/2, |y - CY| + h/2)
  offset text   : r = hypot(|x - CX| + w/2, |y - CY| + h/2)
  centered rect : r = hypot(max(|x-CX|,|x+w-CX|), max(|y-CY|,|y+h-CY|))
  circle        : r = hypot(|cx-CX| + rad, |cy-CY| + rad)

Usage:
  python3 tools/check_layout.py               # current sketch: expect PASS (exit 0)
  python3 tools/check_layout.py --prefix      # original pre-audit coordinates:
                                              # must FAIL (exit 1) — proves the gate
                                              # detects the A11 defects
  python3 tools/check_layout.py --sketch P    # check a different sketch file
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
    "DejaVu40": (22, 40),   # digit advance; used for the big numbers
}

DEFAULT_SKETCH = Path(__file__).parent.parent / "hermes-dash-esp32" / "hermes-dash-esp32.ino"


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
            if re.search(r"\.(drawString|drawNumber|drawRect|fillRect|fillCircle)\(", line):
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
    "FOOTER": "v0.21.3  99 bots",
    "TK_HEAD": "TOKENS 24H",
    "TK_QUAL": "NEW SESSIONS ONLY",
    "TK_BIG": "12345M",
    "TK_IO": "IN 1234M  OUT 12345M?",     # 24H/7D in-out split, worst case incl. stale ?
    "TK_COST": "$9999.99?",               # 7D cost headline
    "TK_CACHE": "CACHE 1234M  $99.99",    # 24H cache + cost
    "TK_CALLS": "CALLS 1234M  SES 12345", # calls / sessions
    "HL_HEAD": "HEALTH",
    "HL_ROW": "PLATFORMS",
    "HL_ERR": "9E",
    "HL_AUTH": "AUTH EXPIRED",
    "HL_HOST": "CPU 100% RAM 100%?",      # single space keeps it inside R=80
}


def check(sk, prefix=False):
    """Run every element check. Returns (results, cx, cy, limit)."""
    cx = sk.val("CENTER_X")
    cy = sk.val("CENTER_Y")
    limit = sk.val("ROUND_R")
    res = []

    def add(name, r, note=""):
        res.append((name, r, r <= limit, note))

    def font_check(name, selector, expected):
        """Verify the sketch's draw site uses the font this check assumes."""
        actual, line_no = sk.font_at(selector)
        if actual is None:
            raise SystemExit(f"ERROR: {name}: no setFont found before {selector} (line {line_no})")
        if actual != expected:
            raise SystemExit(
                f"ERROR: {name}: FONT MISMATCH — {selector} draws with fonts::{actual} "
                f"(line {line_no}) but this check models fonts::{expected}. "
                f"Update the checker for the new font."
            )
        return actual

    if prefix:
        # Original pre-audit coordinates (main @ 96de3b5) for the three elements the
        # audit found outside the glass. The gate must FAIL here.
        bar_y, bar_w, bar_h = cy + 54, 140, 6          # was CENTER_Y+ROUND_R-26 = 140
        add("disk bar (PRE-FIX)", rect_r(cx - bar_w // 2, bar_y, bar_w, bar_h, cx, cy),
            "was x=90..230, y=140..146")
        foot_y = cy + 76                               # was CENTER_Y+ROUND_R-4 = 162
        f = font_check("footer (PRE-FIX)", "drawString(footer", "Font0")
        add("footer (PRE-FIX)", centered(foot_y, text_w(WORST["FOOTER"], f), text_h(f), cx, cy),
            "baseline y=162 needs ±45px, circle allows ±40px")
        add("update dot (PRE-FIX)", circle_r(cx + 55, foot_y, 3, cx, cy),
            "was (215,162) — outside the glass entirely")
        return res, cx, cy, limit

    # ── STATUS page ────────────────────────────────────────────────────────
    ring_r = sk.val("STATUS_RING_R")
    dot_r = sk.val("STATUS_RING_DOT_R")
    for i in range(8):
        angle = 150 + i * (30 - 150) / 7
        rad = math.radians(angle)
        dx = int(ring_r * math.cos(rad))
        dy = int(ring_r * math.sin(rad))
        add(f"ring dot {i}", circle_r(cx + dx, cy - dy, dot_r, cx, cy))

    f = font_check("overflow", "STATUS_OVERFLOW_DX", "Font0")
    add("overflow +99", offset(cx + sk.val("STATUS_OVERFLOW_DX"),
                              cy - sk.val("STATUS_OVERFLOW_DY"),
                              text_w(WORST["OVERFLOW"], f), text_h(f), cx, cy))

    f = font_check("big number", "STATUS_NUM_Y", "DejaVu40")
    add("number 999", centered(sk.val("STATUS_NUM_Y"), text_w("999", f), text_h(f), cx, cy))

    f = font_check("sessions label", "STATUS_SESS_Y", "Font2")
    add("SESSIONS", centered(sk.val("STATUS_SESS_Y"), text_w(WORST["SESSIONS"], f), text_h(f), cx, cy))

    f = font_check("state", "STATUS_STATE_Y", "Font2")
    add("DEGRADED", centered(sk.val("STATUS_STATE_Y"), text_w(WORST["STATE"], f), text_h(f), cx, cy))

    bar_y = cy + sk.val("STATUS_DISK_BAR_DY")
    bar_w, bar_h = sk.val("STATUS_DISK_BAR_W"), sk.val("STATUS_DISK_BAR_H")
    add("disk bar", rect_r(cx - bar_w // 2, bar_y, bar_w, bar_h, cx, cy))

    f = font_check("disk label", "STATUS_DISK_LBL_DY", "Font0")
    add("DISK 100%", centered(bar_y + sk.val("STATUS_DISK_LBL_DY"),
                              text_w(WORST["DISK"], f), text_h(f), cx, cy))

    foot_y = cy + sk.val("STATUS_FOOTER_DY")
    f = font_check("footer", "drawString(footer", "Font0")
    add("footer", centered(foot_y, text_w(WORST["FOOTER"], f), text_h(f), cx, cy))
    add("update dot", circle_r(cx + sk.val("STATUS_DOT_DX"), foot_y, sk.val("STATUS_DOT_R"), cx, cy))

    # ── TOKEN pages (both share the TK_* y positions) ───────────────────────
    f = font_check("tk heading", "TK_HEAD_Y", "Font0")
    add("tk heading", centered(sk.val("TK_HEAD_Y"), text_w(WORST["TK_HEAD"], f), text_h(f), cx, cy))

    f = font_check("tk qualifier", "TK_QUAL_Y", "Font0")
    add("tk qualifier", centered(sk.val("TK_QUAL_Y"), text_w(WORST["TK_QUAL"], f), text_h(f), cx, cy))

    f = font_check("tk big total", "TK_BIG_Y", "DejaVu40")
    add("tk total", centered(sk.val("TK_BIG_Y"), text_w(WORST["TK_BIG"], f), text_h(f), cx, cy))

    # TK_DET1_Y is used twice: 7D cost headline (Font2) and 24H in/out (Font0).
    f = font_check("tk cost (7D)", "drawString(cost_s", "Font2")
    add("tk det1 cost/7D", centered(sk.val("TK_DET1_Y"), text_w(WORST["TK_COST"], f), text_h(f), cx, cy))
    f = font_check("tk io (24H)", "drawString(io, CENTER_X, TK_DET1_Y", "Font0")
    add("tk det1 io/24H", centered(sk.val("TK_DET1_Y"), text_w(WORST["TK_IO"], f), text_h(f), cx, cy))

    f = font_check("tk calls (7D)", "drawString(cs, CENTER_X, TK_DET2_Y", "Font0")
    add("tk det2 calls/7D", centered(sk.val("TK_DET2_Y"), text_w(WORST["TK_CALLS"], f), text_h(f), cx, cy))
    f = font_check("tk cache (24H)", "drawString(cc", "Font0")
    add("tk det2 cache/24H", centered(sk.val("TK_DET2_Y"), text_w(WORST["TK_CACHE"], f), text_h(f), cx, cy))

    f = font_check("tk io (7D)", "drawString(io, CENTER_X, TK_DET3_Y", "Font0")
    add("tk det3 io/7D", centered(sk.val("TK_DET3_Y"), text_w(WORST["TK_IO"], f), text_h(f), cx, cy))
    f = font_check("tk calls (24H)", "drawString(cs, CENTER_X, TK_DET3_Y", "Font0")
    add("tk det3 calls/24H", centered(sk.val("TK_DET3_Y"), text_w(WORST["TK_CALLS"], f), text_h(f), cx, cy))

    # ── HEALTH page ────────────────────────────────────────────────────────
    f = font_check("hl heading", "HL_HEAD_Y", "Font2")
    add("hl heading", centered(sk.val("HL_HEAD_Y"), text_w(WORST["HL_HEAD"], f), text_h(f), cx, cy))

    row_names = ["GATEWAY", "STORAGE", "DASHBOARD", "PLATFORMS"]
    for i, cname in enumerate(["HL_ROW0_Y", "HL_ROW1_Y", "HL_ROW2_Y", "HL_ROW3_Y"]):
        y = sk.val(cname)
        f = font_check(f"hl row {i}", "drawString(rows_txt", "Font2")
        add(f"hl row {i}",
            centered(y, text_w(WORST["HL_ROW"], f), text_h(f), cx, cy),
            f"worst label PLATFORMS; {row_names[i]} drawn here")
        add(f"hl row {i} dot", circle_r(cx - sk.val("HL_DOT_DX"), y, sk.val("HL_DOT_R"), cx, cy))

    f = font_check("hl error", "HL_ERR_DX", "Font0")
    add("hl error 9E", offset(cx + sk.val("HL_ERR_DX"), sk.val("HL_ERR_Y"),
                              text_w(WORST["HL_ERR"], f), text_h(f), cx, cy))

    f = font_check("hl auth", "HL_AUTH_Y", "Font2")
    add("hl auth", centered(sk.val("HL_AUTH_Y"), text_w(WORST["HL_AUTH"], f), text_h(f), cx, cy))

    f = font_check("hl host", "HL_HOST_Y", "Font0")
    add("hl host", centered(sk.val("HL_HOST_Y"), text_w(WORST["HL_HOST"], f), text_h(f), cx, cy))

    return res, cx, cy, limit


def main(argv):
    prefix = "--prefix" in argv
    sketch = DEFAULT_SKETCH
    if "--sketch" in argv:
        sketch = argv[argv.index("--sketch") + 1]

    sk = Sketch(sketch)
    res, cx, cy, limit = check(sk, prefix=prefix)

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
    if bad:
        print(f"FAILED: {len(bad)} element(s) exceed R={limit}")
        for name, r, _, _ in bad:
            print(f"  - {name}: r={r:.1f}")
        return 1
    print(f"PASSED: all {len(res)} elements within R={limit}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))