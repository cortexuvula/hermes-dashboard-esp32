# Hermes Dashboard — ESP32 1.47" Round Display

ESP32-C6 desk widget that shows live Hermes gateway stats from `http://<host>:9119/api/status`.

**Public repo**: https://github.com/cortexuvula/hermes-dashboard-esp32 (branch `main`). Committed:
`PROJECT.md`, `README.md`, `build-flash.sh`, the sketch, `wifi_config.h` (selector), the `.example`
unit configs, `relay/`, `deploy/`, `tests/`, `.github/workflows/ci.yml`. **Gitignored** (never publish): `wifi_config.work.h`,
`wifi_config.home.h`, `build*/` — a compiled `merged.bin`/`.elf` embeds the WiFi PSK in plaintext.
Local git note: after the Xcode 27 update, `/usr/bin/git` was blocked by an unaccepted Xcode license;
run `sudo xcodebuild -license accept` (done 2026-09-15 — both git and Homebrew work again).

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  WORK                                      HOME     │
│                                                     │
│  ESP32-C6 ──WiFi──▶ Mac (LAN)        ESP32-C6 ──▶  │
│  :9119/api/status      │              omarchy-home │
│                         │                  │        │
│                         │◀─Tailscale─▶     │        │
│                         │   100.x.x.x      │        │
│                         │                  │        │
│                     relay.py ◀─────────────┘        │
│                     :9120 → :9119                   │
└─────────────────────────────────────────────────────┘
```

- **Work unit**: ESP32 on same LAN as Mac, hits dashboard directly at `<mac-lan-ip>:9119`
- **Home unit**: ESP32 on same LAN as omarchy-home, hits `relay.py` at `<omarchy-lan-ip>:9120`, which fetches from the Mac via Tailscale (`100.x.x.x:9119`)

## Project structure

```
hermes-dashboard-esp32/
├── PROJECT.md          ← this file
├── firmware/
│   ├── hermes-dash-esp32.ino   ← Arduino sketch
│   ├── User_Setup.h            ← TFT_eSPI pin config
│   └── wifi_config.h           ← WiFi + dashboard URL
└── relay/
    └── relay.py                ← Tailscale relay (runs on omarchy-home)
```

## Tomorrow — when the board arrives  *(historical — the board arrived; kept for the bring-up notes)*

### 1. Identify the board
Plug in USB-C, check `ls /dev/cu.*` — it should appear as `/dev/cu.usbmodem*` or similar.
Run `esptool chip_id` to confirm it's an ESP32-C6.

### 2. Install Arduino libraries  *(historical — the build now uses LovyanGFX + FastLED, not TFT_eSPI/SmartLed; see README)*
```bash
arduino-cli lib install "TFT_eSPI"
arduino-cli lib install "ArduinoJson"
```

### 3. Configure for deployment  *(historical — TFT_eSPI/User_Setup.h approach predates the LovyanGFX switch; see README)*
Edit `firmware/wifi_config.h`:
- WiFi SSID + password for the target network
- Dashboard URL:

| Deployment | WiFi network | Dashboard URL |
|---|---|---|
| **Work** | Office WiFi | `http://<mac-lan-ip>:9119/api/status` |
| **Home** | Home WiFi | `http://<omarchy-lan-ip>:9120/api/status` |

### 4. Compile (test without flashing)
```bash
cd firmware
arduino-cli compile --fqbn esp32:esp32:esp32c6 hermes-dash-esp32.ino
```

### 5. Flash
```bash
arduino-cli upload -p /dev/cu.usbmodem* --fqbn esp32:esp32:esp32c6 hermes-dash-esp32.ino
```

### 6. Set up the relay (home unit only)
On omarchy-home:
```bash
# find the Mac's Tailscale IP
tailscale status | grep mac

# start the relay
python3 relay/relay.py --upstream http://100.X.X.X:9119/api/status --listen-port 9120

# (optional) as a systemd service for persistence
```

## Services audit (2026-09-15) — relay + usage-server rework

Fixed per an independent audit (IDs referenced in commit messages):

- **Wrapper (A1/A2)**: `build-flash.sh` resolves the project relative to itself, parses arguments strictly, and **never flashes unless `--flash` is passed**. See README for the new CLI.
- **Exposure (A3)**: relay emits an allowlisted schema-2 object (CONTRACT table in README) — nothing upstream passes through. CORS is opt-in (`--cors`). usage-server binds **127.0.0.1 by default** — our cross-host deployment passes `--bind 0.0.0.0` deliberately (launchd plist in `deploy/` does); relay stays LAN-reachable by default (`--bind` to override; firewall rule documented in README).
- **Limits (A4)**: both services have per-connection socket timeouts, bounded concurrency (semaphore; over-limit requests get logged 503s, never queue), and usage-server serves a cached payload refreshed at most every 10 s so request rate cannot drive collection.
- **Timing (A5)**: the status fetch runs under a hard TOTAL budget (3.0 s) enforced against a monotonic deadline inside `_fetch_capped` — a trickle of bytes that completes each recv inside the socket timeout is still aborted at the budget (worst case observed ~3.0 s + response write; the old "≤ 4 s via socket timeout" claim was falsified by review and corrected). Usage is refreshed on a background thread (a stalled usage upstream costs ~1–5 ms). Status failure → 502 + generic JSON error (board renders OFFLINE); upstream detail is relay-log-only.
- **Null semantics (A6)**: unavailable usage data is JSON null (never omitted, never zeroed); `usage_age_s` = **DATA age** — seconds since the producer's `generated_at` (relay fetch time only as a fallback), clamped ≥ 0, because usage-server serves its last-good payload indefinitely and the board has no other staleness signal; new `generated_at`, `schema: 2`; `components` null (never fabricated) when upstream omits it.
- **Cohorts (A8)**: `window_basis: "session_started_at"` in the usage payload; SQL unchanged (deliberately matches dashboard analytics). 24H/7D are session-start cohorts, not rolling windows — documented in README + on-display qualifier (firmware lane).
- **Read caps (A9)**: hard byte caps — status 128 KB, usage 32 KB — enforced even when length is unknown/chunked.
- **CPU (A13)**: `load_percent` = 1-min loadavg ÷ cores (queueing); `cpu_percent` = real sampled utilisation (macOS `top -l 2`, Linux `/proc/stat`), null when unobtainable — never a fake 0.
- **Tests/CI (Imp 2)**: `tests/test_relay.py` (16 tests, stdlib unittest, synthetic upstreams) + `tests/test_build_wrapper.sh` (14 assertions, stubbed arduino-cli/scp/ssh) + `.github/workflows/ci.yml` (python + shell + compile-only firmware gate; `tools/ci-compile.sh` if the firmware lane lands it, inline equivalent otherwise; dummy wifi config only).

**Deploy note for the Mac relay pair**: after pulling this rework to the live machines, the usage-server plist needs the new `--bind 0.0.0.0` (the relay on omarchy-home fetches it over Tailscale) and `hermes-dash-relay.service` needs `--usage …:9121/`. `deploy/` files are operator-specific examples.

## Deploy status (2026-09-15) — HOME UNIT re-flashed with the landscape build

**Board**: Waveshare ESP32-C6-LCD-1.47 on omarchy-home `/dev/ttyACM0`, WiFi `onCortex`, DHCP **192.168.1.174**.
**What changed**: the home board was still on the 2026-09-13 home build; it now carries the Sep-14 landscape
4-page firmware (STATUS / TOKENS 24H / TOKENS 7D / HEALTH) + FastLED RGB + `huge_app`.
**Config split (no more clobbering)**: `wifi_config.h` is now only a selector and includes
`wifi_config.work.h` (default) or `wifi_config.home.h` (`-DUNIT_HOME`). The work build and the home build
target different output dirs (`build-work/`, `build-home/`). Use **`./build-flash.sh {home|work} [--build-only|--flash]`** —
it compiles with the right flag, greps the image for the expected SSID/IP, then flashes (home: scp +
esptool over ssh; work: arduino-cli upload to `/dev/cu.usbmodem*`). *(CLI updated 2026-09-15: `--build-only` is now the
default behaviour and flashing requires explicit `--flash`.)*
**Home WiFi PSK**: NOT in BWS (`WIFI_PASSWORD` there = the office `cortexWork` PSK). The `onCortex` PSK was
pulled from omarchy-home's NetworkManager profile (`sudo cat /etc/NetworkManager/system-connections/onCortex.nmconnection`)
and lives only in `wifi_config.home.h` (chmod 600).
**Relay fix**: the omarchy-home relay was an older `relay.py` that merged `tokens_24h`/`tokens_7d` but **not `host`**,
so the HEALTH page's CPU/RAM line read 0%/0%. It now runs the current `relay/relay.py` with
`--usage http://100.79.10.43:9121/` (`ExecStart` updated, unit reloaded). Verify from the host, not from the Mac —
`ssh omarchy-home 'curl -s http://127.0.0.1:9120/api/status'` must show `host`, `tokens_24h`, `tokens_7d`
(cross-LAN curl from the Mac to 192.168.1.171 fails; the LANs are separate).
**Verified 2026-09-15**: esptool write hash-verified; serial boot log → `WiFi status=3, IP 192.168.1.174`,
`GET http://192.168.1.171:9120/api/status -> 200`, `fetch OK: 2 sessions, 8/8 platforms`; relay journal shows
polls every ~10s from 192.168.1.174.

## Deploy status (2026-09-14) — WORK UNIT on the Mac (landscape UI)

**Board**: same Waveshare ESP32-C6-LCD-1.47, now attached to the Mac (`/dev/cu.usbmodem5101`), office WiFi **`cortexWork`** (password in Bitwarden BWS as `WIFI_PASSWORD` — pull with `bws secret get <id> --output json`; do NOT use plain `bws secret get`, it dumps the whole JSON object).
**Config**: `wifi_config.h` → SSID `cortexWork`, DASHBOARD_URL `http://192.168.4.37:9120/api/status` (Mac's LAN IP → LOCAL relay, see below). Board DHCP: `192.168.4.158`.
**Mac relay (tokens)**: the dashboard `:9119/api/status` does NOT include `tokens_24h` — the merge lives in `relay/relay.py`. Running ON THE MAC as launchd agent **`ai.hermes.dash-relay`** (`~/Library/LaunchAgents/ai.hermes.dash-relay.plist`): `--listen-port 9120 --upstream http://127.0.0.1:9119/api/status --usage http://127.0.0.1:9121/` (usage-server `ai.hermes.usage-server` already on :9121). Logs: `~/.hermes/logs/dash-relay.log` (shows `[relay] <ip> GET /api/status` polls).
**UI**: LANDSCAPE — `tft.setRotation(1)` (320×172, center 160,86); tokens page re-laid-out for the short axis (was portrait 172×320). Verified: boot log shows `WiFi status=3, IP 192.168.4.158` + `GET ... -> 200`.
**Pages (4 × 12s rotation)**: STATUS (ring w/ attention blink, sessions, state, disk, footer + yellow update dot) · TOKENS 24H (total, IN/OUT, cache+cost, calls/sessions) · TOKENS 7D (total, $cost, calls/sessions, IN/OUT) · HEALTH (GW/DASH/STOR/PLAT dots + dash err count, session validity, host CPU/RAM, UPDATE AVAILABLE).
**RGB LED (GPIO8, WS2812, FastLED 3.10.5 — SmartLed NOT in core 3.3.11)**: green flash per fetch, orange when busy/degraded, red offline.
**Build flag**: `PartitionScheme=huge_app` is REQUIRED — FastLED pushes the sketch past the default 1.25MB app partition (1.39MB/44% of 3MB with it). FQBN: `esp32:esp32:esp32c6:CDCOnBoot=cdc,PartitionScheme=huge_app`.
**Note**: this overwrote the home-unit config (onCortex / relay :9120). To redeploy home, restore SSID `onCortex` + URL `http://192.168.1.171:9120/api/status` and reflash (PROJECT.md home section below has the old layout).

## Deploy status (2026-09-13) — HOME UNIT on omarchy-home

**Board**: Waveshare ESP32-C6-LCD-1.47 attached via USB-C → `/dev/ttyACM0` (native USB serial).
**Toolchain**: compile on Mac (`arduino-cli`, fqbn `esp32:esp32:esp32c6:CDCOnBoot=cdc`), flash via esptool ON omarchy-home (`~/.local/bin/esptool`, installed user-level, no sudo).

```bash
# on Mac:
arduino-cli compile --fqbn esp32:esp32:esp32c6:CDCOnBoot=cdc --output-dir /tmp/esp32-build hermes-dash-esp32.ino
scp /tmp/esp32-build/hermes-dash-esp32.ino.merged.bin omarchy-home:/tmp/hermes-dash.bin
# on omarchy-home:
~/.local/bin/esptool --chip esp32c6 --port /dev/ttyACM0 --baud 460800 write_flash -z 0x0 /tmp/hermes-dash.bin
```

**Display lib**: LovyanGFX (NOT TFT_eSPI — fails on C6 RISC-V: no VSPI registers). Custom LGFX class in sketch: ST7789V3 172×320, MOSI=6 SCLK=7 CS=14 DC=15 RST=21 BL=22, offset_x=34, invert=true. Backlight PWM ≤50% via `setBrightness(128)`.

**Relay**: systemd user service `hermes-dash-relay` on omarchy-home (unit in `~/.config/systemd/user/`), `/usr/bin/python3 ~/hermes-dashboard-relay/relay.py --listen-port 9120 --upstream http://100.79.10.43:9119/api/status`. Runs on boot (user session), Restart=always. Pitfall: relay.py must live at `~/hermes-dashboard-relay/relay.py` and ExecStart uses `/usr/bin/python3` (pip user-site has no python binary).

**WiFi**: home SSID `onCortex` — HIDDEN network, 2.4GHz ch 11. Password in `wifi_config.h` (local file, no git).

### ✅ RESOLVED — it was ufw on omarchy-home, not the router
Symptom: ESP32 GET → connection refused; omarchy-laptop → omarchy-home :9120/:22 timeouts; ping works both ways; router tests clean. Root cause: **ufw active on the Omarchy boxes** (`sudo ufw status verbose`: default deny incoming; only 53317 Syncthing + docker-dns allowed). All cross-machine traffic rides Tailscale so the deny was never noticed. The UniFi UDM Pro was innocent (no AP isolation, no traffic rules).
**Fix (applied 2026-09-13)**: `sudo ufw allow from 192.168.1.0/24 to any port 9120 proto tcp comment 'hermes dashboard relay (ESP32)'` — rule persists across reboots. Verify: `journalctl --user -u hermes-dash-relay | grep '\[relay\]'` shows `192.168.1.174 GET /api/status` every ~10s. Revert with `sudo ufw delete allow from 192.168.1.0/24 to any port 9120 proto tcp`. Laptop's ufw left untouched (inbound deny kept, by design).
**Widget is LIVE**: ESP32 = 192.168.1.174, poll ~10s, display shows sessions / platform ring / RUNNING / disk / version + bot count. Unplug/replug safe — reconnects on its own.

**Reading the widget**: DEGRADED state = gateway's `overall` health, not the widget. 2026-09-13 fixed a pre-existing HA connector outage that the widget surfaced: stale `HASS_URL` (dead Tailscale IP, fixed in `~/.hermes/.env`) + stale `HASS_TOKEN` (rotated in Bitwarden; must delete `~/.hermes/cache/bws_cache.json` before gateway restart — see hermes-agent skill `references/bws-secret-cache-pitfall.md`).

**Pages (2026-09-13)**: the display rotates two pages (~20s each): **STATUS** (ring/sessions/RUNNING/disk/footer) ↔ **TOKENS 24H** (total + IN/OUT split + CACHE + cost, formatted k/M). Token data path: `usage-server.py` on the Mac (launchd agent `ai.hermes.usage-server`, :9121, reads `~/.hermes/state.db` with the same SQL as dashboard analytics — no auth wall) → relay merges `tokens_24h`/`tokens_7d` into the status JSON. Disk parse pitfall: ArduinoJson `| 0` falls back on float values — use `| 0.0` (fixed; DISK was showing 0% at 84% real).

### Serial debugging pitfall (ESP32-C6 USB-JTAG)
Opening `/dev/ttyACM0` asserts DTR/RTS → hardware reset into DOWNLOAD mode ("waiting for download"), killing the running app. To capture boot logs: hold the port open with lines neutral (`pyserial` on omarchy-home rejects dtr/rts constructor kwargs — set `s.dtr=False; s.rts=False` AFTER open), then reset via esptool on a second fd: `~/.local/bin/esptool --port /dev/ttyACM0 --after hard_reset chip_id`. Pattern: `python3 /tmp/serial_hold.py &` then esptool reset.

## Board details — Waveshare ESP32-C6-LCD-1.47

| Pin | GPIO | Notes |
|---|---|---|
| MOSI | 6 | Shared with SD card |
| SCLK | 7 | Shared with SD card |
| LCD_CS | 14 | |
| LCD_DC | 15 | |
| LCD_RST | 21 | |
| LCD_BL | 22 | Backlight PWM — keep ≤ 50% |
| RGB LED | 8 | WS2812-style |
| SD_CS | 4 | Not used (unless we want logging) |

**Warnings from Waveshare**:
- Backlight max 50% — full brightness overheats the display
- Don't remove the screen when soldering headers
- ESP32-C6FH4 = 4MB flash (not 16MB)

## Display layout

```
  ┌─────────────┐
  │ ●●●●●●●●●    │  platform status ring (green=up, red=down)
  │              │
  │      3       │  active sessions (big centered number)
  │   SESSIONS   │
  │   RUNNING    │  gateway state (RUNNING/BUSY/DEGRADED)
  │   ████░░░    │  disk bar + %
  │ v0.21  31⚙  │  version + profile count
  └─────────────┘
    172×320, round glass (~160px usable)
```

## Future ideas
- RGB LED as activity indicator (blink on fetch, red if offline)
- Show which bots are currently active
- Token usage / context % from the dashboard API
- Touch input (this board doesn't have touch — C6-LCD-1.47 is non-touch; the touch version is C6-Touch-LCD-1.47)