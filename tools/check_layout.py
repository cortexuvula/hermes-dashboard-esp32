#!/usr/bin/env python3
"""
Layout envelope checker for the ESP32-C6 round dashboard (A11).

Constraint:
  hypot(|x-cx| + w/2, |y-cy| + h/2) <= ROUND_R - 4 = 76

where ROUND_R=80, center=(160,86), landscape 320x172.
Every drawn element (including glyph corners and dot radii) must fit
inside the round glass aperture.

Usage:
  python3 tools/check_layout.py --prefix   # model pre-fix (broken) layout
  python3 tools/check_layout.py            # model post-fix layout (default)

Exits 0 on pass, 1 on violation.
"""

import math
import sys

# ── Display constants (from firmware) ──────────────────────
CX = 160
CY = 86
ROUND_R = 80
MARGIN = 4
MAX_R = ROUND_R - MARGIN  # 76

# ── Font metrics (LovyanGFX built-ins) ───────────────────
FONT0_H = 8   # Font0: 5px wide per char, 8px tall
FONT2_H = 16  # Font2: 8px wide per char, 16px tall

def check_centered_text(label, y, text_w, text_h):
    """Centered text at (CX, y). Worst corner from (CX, CY)."""
    dx = text_w / 2.0
    dy = abs(y - CY) + text_h / 2.0
    r = math.hypot(dx, dy)
    return r

def check_rect(label, x, y, w, h):
    """Arbitrary rect (x,y,w,h). Worst corner from (CX, CY)."""
    dx = max(abs(x - CX), abs(x + w - CX))
    dy = max(abs(y - CY), abs(y + h - CY))
    r = math.hypot(dx, dy)
    return r

def check_circle(label, cx_dot, cy_dot, radius):
    """Filled circle. Worst point = center distance + radius."""
    d = math.hypot(cx_dot - CX, cy_dot - CY)
    return d + radius

def main():
    pre_fix = "--prefix" in sys.argv
    errors = []
    results = []

    print(f"Layout checker — {'PRE-FIX (broken)' if pre_fix else 'POST-FIX'} model")
    print(f"Center: ({CX}, {CY}), ROUND_R={ROUND_R}, margin={MARGIN}, max_r={MAX_R}")
    print()

    if pre_fix:
        # ═══════════════════════════════════════════════════
        # PRE-FIX layout (original .ino, pre-audit)
        # ═══════════════════════════════════════════════════

        # Platform ring: radius=60 (ROUND_R-20), dot_r=4
        ring_r = ROUND_R - 20  # 60
        for i in range(8):
            if i == 0:
                angle = 150.0
            else:
                angle = 150.0 + i / 7.0 * (30.0 - 150.0)
            rad = math.radians(angle)
            dx_dot = CX + ring_r * math.cos(rad)
            dy_dot = CY - ring_r * math.sin(rad)
            r = check_circle(f"ring_dot_{i}", int(round(dx_dot)), int(round(dy_dot)), 4)
            results.append((f"ring_dot_{i} @ ({dx_dot:.0f},{dy_dot:.0f})", r))

        # Center: DejaVu40 at (160, 74), w≈48, h≈40
        results.append(("center_num DejaVu40 @y=74", check_centered_text("cn", 74, 48, 40)))

        # Sessions: Font2 at (160, 108), w=64, h=16
        results.append(("sessions Font2 @y=108", check_centered_text("sl", 108, 64, 16)))

        # State: Font2 at (160, 126), w=64, h=16
        results.append(("state DEGRADED Font2 @y=126", check_centered_text("st", 126, 64, 16)))

        # Disk bar: rect(90, 140, 140, 6) — original bar_w = ROUND_R*2-20 = 140
        results.append(("disk_bar rect(90,140,140,6)", check_rect("db", 90, 140, 140, 6)))

        # Disk label: Font0 at (160, 150), "DISK 99%" = 8*5=40px
        results.append(("disk_label Font0 @y=150", check_centered_text("dl", 150, 40, FONT0_H)))

        # Footer: Font0 at (160, 162), worst-case "v12.345.6  99 bots" = 19*5=95px
        results.append(("footer Font0 @y=162 (95px)", check_centered_text("ft", 162, 95, FONT0_H)))

        # Update dot: fillCircle(215, 162, 3) — audit-measured position
        results.append(("update_dot (215,162,r=3)", check_circle("ud", 215, 162, 3)))

    else:
        # ═══════════════════════════════════════════════════
        # POST-FIX layout (new coordinates)
        # ═══════════════════════════════════════════════════

        # Platform ring: radius=50 (ROUND_R-30), dot_r=4
        ring_r = 50
        for i in range(8):
            if i == 0:
                angle = 150.0
            else:
                angle = 150.0 + i / 7.0 * (30.0 - 150.0)
            rad = math.radians(angle)
            dx_dot = CX + ring_r * math.cos(rad)
            dy_dot = CY - ring_r * math.sin(rad)
            r = check_circle(f"ring_dot_{i}", int(round(dx_dot)), int(round(dy_dot)), 4)
            results.append((f"ring_dot_{i} @ ({dx_dot:.0f},{dy_dot:.0f})", r))

        # Overflow marker "+9" at (CX+ring_r+8, CY-ring_r/2) = (218, 61), 10x8 rect
        ox = CX + ring_r + 8  # 218
        oy = CY - ring_r // 2  # 61
        results.append(("overflow +N rect(213,57,10,8)", check_rect("ov", ox - 5, oy - 4, 10, 8)))

        # Center: DejaVu40 at (160, 72), w≈48 (2-digit), h≈40
        results.append(("center_num DejaVu40 @y=72", check_centered_text("cn", 72, 48, 40)))

        # Sessions: Font2 at (160, 100), w=64, h=16
        results.append(("sessions Font2 @y=100", check_centered_text("sl", 100, 64, 16)))

        # State: Font2 at (160, 118), w=64, h=16
        results.append(("state DEGRADED Font2 @y=118", check_centered_text("st", 118, 64, 16)))

        # Disk bar: rect(104, 120, 112, 6)
        results.append(("disk_bar rect(104,120,112,6)", check_rect("db", 104, 120, 112, 6)))

        # Disk label: Font0 at (160, 130), "DISK 99%" = 8*5=40px
        results.append(("disk_label Font0 @y=130", check_centered_text("dl", 130, 40, FONT0_H)))

        # Footer: Font0 at (160, 138), worst-case "v12.345.6  99 bots" = 19*5=95px
        results.append(("footer Font0 @y=138 (95px)", check_centered_text("ft", 138, 95, FONT0_H)))

        # Update dot: fillCircle(210, 138, 3) — FIXED position, not text-relative
        results.append(("update_dot (210,138,r=3)", check_circle("ud", 210, 138, 3)))

    # ── Print results ──────────────────────────────────────
    print(f"{'Element':<40} {'Status':<8} {'Radius':>8}  {'Limit':>6}  {'Over':>6}")
    print("-" * 72)
    for label, r in results:
        ok = r <= MAX_R
        status = "PASS" if ok else "FAIL"
        over = r - MAX_R if not ok else 0.0
        print(f"  {label:<38} {status:<8} {r:>7.1f}  {MAX_R:>5.0f}  {over:>+5.1f}" if not ok
              else f"  {label:<38} {status:<8} {r:>7.1f}  {MAX_R:>5.0f}        ")
        if not ok:
            errors.append(f"  FAIL: {label} r={r:.1f} > {MAX_R}")

    print()
    if errors:
        print(f"FAILED — {len(errors)} element(s) outside the glass:")
        for e in errors:
            print(e)
        sys.exit(1)
    else:
        print(f"PASSED — all {len(results)} elements within r={MAX_R}")
        sys.exit(0)

if __name__ == "__main__":
    main()
