# hermes-dashboard-esp32

Desk widget firmware for the **Waveshare ESP32-C6-LCD-1.47** (round 172×320
ST7789V3, Wi-Fi 6, 4 MB flash) that shows live [Hermes Agent](https://hermes-agent.nousresearch.com/)
gateway stats on a small round display — platform health, active sessions, disk,
token usage, and host CPU/RAM — plus an RGB status LED.

```
   ESP32-C6 (round glass)                relay host (any always-on box)
        │  WiFi, HTTP GET every 10s           │
        └──────────►  relay.py :9120 ───────────► Tailscale ─► dashboard :9119/api/status
                                                                          usage-server :9121/usage
```

The relay exists for two reasons: token-usage totals (`tokens_24h`,
`tokens_7d`) and host CPU/RAM (`host`) are not part of `/api/status` —
`relay.py` merges them into one compact JSON response the ESP32 can parse
cheaply — and it collapses the network topology (the home board cannot
reach the Mac's LAN directly). Note: the dashboard's status route itself is
public/unauthenticated; the relay is not an auth bypass.

## CONTRACT — relay response, schema 2

One authoritative list of what the board reads. A relay copy that emits
anything else is stale (check `schema`). Supported upstream: Hermes
dashboard `GET /api/status` (as of Hermes v0.2x).

| Field | Type | Meaning |
|---|---|---|
| `active_sessions`, `active_agents` | int | from upstream |
| `gateway_busy`, `can_update_hermes` | bool | from upstream |
| `version` | string | upstream Hermes version |
| `overall` | string | `"ok"`/`"degraded"`/… |
| `gateway_platforms` | object | per-platform `{state, needs_attention}` |
| `disk.used_percent` | float | upstream disk usage |
| `profiles` | array | upstream profile list (count shown) |
| `nous_session_valid` | string | `"valid"`/… |
| `components` | object\|null | passes through; **null if upstream omits it** (never fabricated) |
| `tokens_24h`, `tokens_7d` | object\|null | `{total, input, output, cache, reasoning, api_calls, sessions, est_cost}` — **JSON null when usage data is unavailable, never omitted, never zeroed** |
| `host` | object\|null | `{cpu_percent, load_percent, ram_used_percent, ram_total_mb}` — null when unavailable |
| `usage_age_s` | int\|null | **age of the usage DATA**: seconds since the producer's `generated_at` (falling back to the relay's fetch time only when `generated_at` is absent), clamped ≥ 0. The board has no clock, and usage-server may serve its last-good payload indefinitely when its collector fails, so this producer-side age is the board's only staleness signal |
| `generated_at` | int\|null | epoch seconds, forwarded from the usage snapshot |
| `schema` | int | contract version — currently `2` |

Null semantics: any unavailable value is JSON `null` — the firmware renders
"--"/blank for nulls; a zero would be a fabricated fact.

Host fields: `cpu_percent` is a real sampled CPU utilisation (null when no
sample is obtainable). `load_percent` is the 1-minute load average ÷ core
count, clamped to 100 — a **queueing** metric, not utilisation.

Cohort semantics (important): `tokens_24h` / `tokens_7d` sum the CUMULATIVE
session counters of sessions **started** inside the window
(`window_basis: "session_started_at"` in the usage payload). They are
cohorts, not rolling windows — the Hermes state DB has no per-event
timestamps, so this cannot be made a true rolling window. The SQL
deliberately matches the dashboard's own analytics. The display shows a
qualifier for these numbers (firmware ≥ schema 2).

Upstream read caps: status ≤ 128 KB, usage ≤ 32 KB — oversize upstream
responses fail predictably (502 / null usage fields).

Relay timing: the upstream status fetch runs under a hard TOTAL budget
(3.0 s by default) enforced against a monotonic deadline inside the relay —
not merely a per-socket-operation timeout — so even an upstream that
trickles bytes (each recv completing inside the socket timeout) is
aborted at the budget. Worst-case response ≈ the status budget plus the
response write (~3.1 s observed), comfortably inside the board's 8 s HTTP
wait. Usage is fetched by a background refresher and NEVER blocks a
response. If the status fetch fails the relay answers 502 with a generic
`{"error": …}` (upstream detail goes to the relay log only) and the board
renders OFFLINE.

Connection-layer limit (accepted, documented): the relay spawns a thread
and file descriptor per accepted connection BEFORE the concurrency
semaphore is consulted, and the per-connection timeout is per-operation —
a client trickling ~1 byte per just-under-timeout interval can hold a
thread for a long time at trivial cost (fd/thread exhaustion at scale).
The concurrency semaphore bounds request *processing*, not connection
count. For a LAN service polled by a single board this is an accepted
trade-off; it is not a general DoS defence.

## Display

Landscape UI, 4 pages, rotating every 12 s (`tft.setRotation(1)` → 320×172):

| Page | Content |
|---|---|
| STATUS | platform up/down ring, active sessions, gateway state (RUNNING/BUSY/DEGRADED), disk bar, version + profile count |
| TOKENS 24H | total tokens, IN/OUT split, cache, est. cost, calls/sessions (cohort — see CONTRACT) |
| TOKENS 7D | 7-day cohort total, cost, calls/sessions, IN/OUT |
| HEALTH | GATEWAY/STORAGE/DASHBOARD/PLATFORMS status dots, dashboard error count, auth state, host CPU/RAM |

RGB LED (GPIO8, WS2812): green flash per successful fetch, orange when the
gateway is busy/degraded, red when offline.

## Hardware / pinout

| Signal | GPIO |
|---|---|
| MOSI | 6 |
| SCLK | 7 |
| LCD_CS | 14 |
| LCD_DC | 15 |
| LCD_RST | 21 |
| LCD_BL | 22 (backlight PWM — keep ≤ 50 %, `setBrightness(128)`) |
| RGB LED | 8 (WS2812-style, driven with FastLED) |
| SD_CS | 4 (unused) |

Panel: ST7789V3, 172×320 window on a 240×320 round glass → `offset_x = 34`,
`invert = true`.

**Library notes:** TFT_eSPI does **not** compile on ESP32-C6 (it hard-codes
classic ESP32 SPI registers that don't exist on RISC-V) — this project uses
**LovyanGFX** with a custom `LGFX` class. `SmartLed.h` isn't in esp32 core
3.3.x, so the WS2812 LED uses **FastLED**, which pushes the sketch past the
default 1.25 MB app partition — hence `PartitionScheme=huge_app`.

## Build

Toolchain: `arduino-cli` with esp32 core 3.3.x, and the two libraries above.

```bash
arduino-cli lib install LovyanGFX ArduinoJson FastLED

cd hermes-dash-esp32
arduino-cli compile \
  --fqbn esp32:esp32:esp32c6:CDCOnBoot=cdc,PartitionScheme=huge_app \
  --output-dir build hermes-dash-esp32.ino
```

`CDCOnBoot=cdc` is required — without it `Serial` goes to UART0 pins and the USB
port stays silent. `--output-dir` also emits
`hermes-dash-esp32.ino.merged.bin`, the complete flash image.

## Configure

`wifi_config.h` is only a **selector** — it includes one unit config, so a build
for one board can never overwrite another board's credentials:

```c
#if defined(UNIT_HOME)
  #include "wifi_config.home.h"
#else
  #include "wifi_config.work.h"   // default
#endif
```

1. Copy the example for your unit and fill it in:
   `cp wifi_config.home.h.example wifi_config.home.h`
2. Set `WIFI_SSID`, `WIFI_PASS`, and `DASHBOARD_URL` (the relay host, e.g.
   `http://192.168.1.171:9120/api/status`).
3. Add `-DUNIT_HOME` when building that unit.

> **Credentials:** `wifi_config.home.h` and `wifi_config.work.h` are
> **gitignored** — and so are the build directories, because a compiled
> `merged.bin` embeds the WiFi password in plaintext. Only the `.example` files
> belong in the repo.

## build-flash.sh (build wrapper)

**Flashing is never implicit.** A bare `./build-flash.sh home` compiles and
verifies the image only — no transfer, no hardware access. The wrapper
resolves the project relative to itself (safe to copy elsewhere), rejects
every unrecognised argument before doing anything, and reads the expected
image markers (SSID + dashboard host) from the unit's own config file:

```bash
./build-flash.sh home              # compile + verify only (DEFAULT, no flash)
./build-flash.sh home --build-only # same as the default, explicit
./build-flash.sh home --flash      # compile + verify + flash the home board
./build-flash.sh work --flash      # compile + verify + flash a locally attached board
```

Before flashing it prints the resolved source dir, unit, config file, the
markers found in the image and `git describe --always --dirty`, and refuses
to transfer if the image lacks the expected markers. The home-unit transfer
goes to a private `mktemp -d` dir (mode 700) on the remote, removed
afterwards.

Optional flags/env (remote unit): `--remote-host` / `HERMES_DASH_REMOTE_HOST`
(default `omarchy-home`), `--remote-user` / `HERMES_DASH_REMOTE_USER`,
`--remote-dev` / `HERMES_DASH_REMOTE_DEV` (default `/dev/ttyACM0`),
`--local-port` / `HERMES_DASH_LOCAL_PORT` (work unit; default scans
`/dev/cu.usbmodem*`).

## Flash

**Historical (pre-Sep-15-2026) manual procedure — kept for reference; use
`build-flash.sh --flash` instead.** The ESP32's USB serial port must be
local to the flashing machine (`esptool` cannot reach a board over SSH):

```bash
scp hermes-dash-esp32/build-home/hermes-dash-esp32.ino.merged.bin <host>:/tmp/dash.bin
ssh <host> '~/.local/bin/esptool --chip esp32c6 --port /dev/ttyACM0 --baud 460800 \
              write_flash -z 0x0 /tmp/dash.bin'
```

The merged image goes to `0x0` as a single file — no bootloader/partition
address juggling.

## Relay + usage server

Both are stdlib-only Python 3 — no dependencies — and run under system
python3 on Linux and macOS.

- `relay/relay.py` — run on the host on the board's LAN; serves `:9120`.
  Binds **0.0.0.0 by default** (the board polls it over the LAN — allow
  inbound TCP 9120 from the board's subnet in the firewall, e.g.
  `sudo ufw allow from 192.168.1.0/24 to any port 9120 proto tcp`); use
  `--bind 127.0.0.1` to restrict to localhost. Fetches `/api/status`
  upstream (≤ 3 s budget, 128 KB cap) and merges usage fields per the
  CONTRACT above. CORS is off unless `--cors` is passed (only if a browser
  consumer needs it). Logs every request (`[relay] <ip> GET /api/status`),
  which is the easiest way to confirm the board is alive. Service file:
  `deploy/hermes-dash-relay.service` (operator-specific example).
- `relay/usage-server.py` — run on the machine that owns the Hermes SQLite
  state DB; serves token totals as JSON on `:9121`. Binds **127.0.0.1 by
  default** — pass `--bind 0.0.0.0` deliberately when a relay on another
  host must reach it (and allow inbound TCP 9121 from that host only).
  Serves a cached payload refreshed at most every 10 s (`--refresh-interval`)
  so request rate cannot drive DB queries or host-stat subprocesses. `--db`
  selects the state DB path, `--port` the port. Launchd agent:
  `deploy/ai.hermes.usage-server.plist` (operator-specific example).

```bash
python3 relay/relay.py --listen-port 9120 --bind 0.0.0.0 \
  --upstream http://<dashboard-host>:9119/api/status \
  --usage    http://<dashboard-host>:9121/

python3 relay/usage-server.py --db ~/.hermes/state.db --port 9121 --bind 127.0.0.1
```

## Tests

```bash
python3 -m unittest discover -s tests -v   # relay tests (synthetic upstreams)
bash tests/test_build_wrapper.sh           # wrapper tests (stubbed toolchain)
```

CI (`.github/workflows/ci.yml`) runs both plus a compile-only firmware gate
(dummy `wifi_config.work.h` — a real-config binary is never produced).

## Debugging notes (learned the hard way)

- **Config key parity:** a stale relay copy that merges only *some* fields makes
  the board poll happily while a page renders blanks. The `schema` field plus
  the CONTRACT table above is the check: `curl -s http://127.0.0.1:9120/api/status`
  must show `schema: 2` and every CONTRACT field.
- **ESP32-C6 USB-Serial-JTAG trap:** merely opening `/dev/ttyACM0` asserts
  DTR/RTS and hardware-resets the chip into *download mode*, killing the running
  app. To read a boot log, hold the port with neutral lines
  (`s.dtr = False; s.rts = False` after open) and trigger a clean app reset from
  a second file descriptor (`esptool --after hard_reset chip_id`).
- **ArduinoJson:** `doc["x"] | 0` falls back on float values — use `| 0.0`
  (this silently rendered a real 84 % disk as 0 %).
- **Round-glass envelope:** the bounding rectangle is not the visible area. A
  row can sit fully inside the 320×172 rect and still be clipped by the round
  aperture — check each element's full envelope against the circle radius.

Full deployment history and pins live in [PROJECT.md](PROJECT.md).
