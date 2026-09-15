# Hermes Dashboard — ESP32-C6 1.47" LCD (sketch)

Round desk widget showing live Hermes gateway stats + token usage.
Start at the repo root: **[../README.md](../README.md)** (build, config, flashing,
troubleshooting) and **[../PROJECT.md](../PROJECT.md)** (deployment history).

## Display

LANDSCAPE — `tft.setRotation(1)`, 320×172 (USB port to the right; use `3` to flip
if your mount is the other way up). 4 pages, rotating every 12 s:

| Page | Content |
|---|---|
| STATUS | platform ring, active sessions, gateway state, disk bar, version + profiles |
| TOKENS 24H | total, IN/OUT, cache, est. cost, calls/sessions |
| TOKENS 7D | 7-day total, cost, calls/sessions, IN/OUT |
| HEALTH | GATEWAY/STORAGE/DASHBOARD/PLATFORMS dots, dashboard errors, auth state, host CPU/RAM |

Everything is laid out around `CENTER_X/CENTER_Y` (160, 86) so the round aperture
stays respected — the old portrait tokens page drew to y=226, which is off-screen
at 172 px tall.

## Files

- `hermes-dash-esp32.ino` — the sketch (LovyanGFX `LGFX` panel class, fetch/parse,
  4 draw pages, FastLED status LED).
- `wifi_config.h` — **selector only**; includes `wifi_config.work.h` (default) or
  `wifi_config.home.h` (`-DUNIT_HOME`). Real unit configs are gitignored; see the
  `.example` files.
- `../build-flash.sh` — compile + verify + flash for either unit.

## Build / flash

```bash
../build-flash.sh home --build-only   # or: work
../build-flash.sh home                # compile and flash the home board
```

FQBN is `esp32:esp32:esp32c6:CDCOnBoot=cdc,PartitionScheme=huge_app`
(`huge_app` is required because FastLED pushes the sketch past the default
1.25 MB app partition; `CDCOnBoot=cdc` keeps `Serial` on USB).