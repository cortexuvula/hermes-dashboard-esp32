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

The relay exists because the dashboard API needs auth, and because token-usage
totals (`tokens_24h`, `tokens_7d`) and host CPU/RAM (`host`) are not part of
`/api/status` — `relay.py` merges them into one unauthenticated local JSON
response that the ESP32 can parse cheaply.

## Display

Landscape UI, 4 pages, rotating every 12 s (`tft.setRotation(1)` → 320×172):

| Page | Content |
|---|---|
| STATUS | platform up/down ring, active sessions, gateway state (RUNNING/BUSY/DEGRADED), disk bar, version + profile count |
| TOKENS 24H | total tokens, IN/OUT split, cache, est. cost, calls/sessions |
| TOKENS 7D | 7-day total, cost, calls/sessions, IN/OUT |
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

`build-flash.sh` wraps the whole thing (compile → verify the image contains the
expected SSID/URL strings → flash):

```bash
./build-flash.sh home --build-only   # compile the home variant only
./build-flash.sh home                # compile + flash the board on the relay host
./build-flash.sh work                # compile + flash a directly-attached board
```

## Flash

The ESP32's USB serial port must be local to the flashing machine (`esptool`
cannot reach a board over SSH):

```bash
scp hermes-dash-esp32/build-home/hermes-dash-esp32.ino.merged.bin <host>:/tmp/dash.bin
ssh <host> '~/.local/bin/esptool --chip esp32c6 --port /dev/ttyACM0 --baud 460800 \
              write_flash -z 0x0 /tmp/dash.bin'
```

The merged image goes to `0x0` as a single file — no bootloader/partition
address juggling.

## Relay + usage server

- `relay/relay.py` — run on the host on the board's LAN; serves `:9120`, fetching
  `/api/status` upstream and merging `tokens_24h` / `tokens_7d` / `host`.
  Logs every request (`[relay] <ip> GET /api/status`), which is the easiest way
  to confirm the board is alive. Service file: `deploy/hermes-dash-relay.service`.
- `relay/usage-server.py` — run on the machine that owns the Hermes SQLite state
  DB; serves token totals as JSON on `:9121`.
  Launchd agent: `deploy/ai.hermes.usage-server.plist`.

```bash
python3 relay/relay.py --listen-port 9120 \
  --upstream http://<dashboard-host>:9119/api/status \
  --usage    http://<dashboard-host>:9121/
```

Both are stdlib-only Python 3 — no dependencies.

## Debugging notes (learned the hard way)

- **Config key parity:** a stale relay copy that merges only *some* fields makes
  the board poll happily while a page renders blanks. Verify the merged key set
  from the relay's own host (`curl -s http://127.0.0.1:9120/api/status`) against
  every key the firmware parses.
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