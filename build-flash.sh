#!/usr/bin/env bash
# Build (and optionally flash) one ESP32 unit.
#
#   ./build-flash.sh home          # compile home build, flash board on omarchy-home (/dev/ttyACM0)
#   ./build-flash.sh work          # compile work build, flash board on the Mac (/dev/cu.usbmodem*)
#   ./build-flash.sh home --build-only
#
# Unit configs live in hermes-dash-esp32/wifi_config.{work,home}.h; wifi_config.h is
# only a selector, so flashing one unit can never overwrite the other's credentials.
set -euo pipefail

UNIT="${1:-}"
shift || true
BUILD_ONLY=0
[[ "${1:-}" == "--build-only" ]] && BUILD_ONLY=1

SKETCH_DIR="$HOME/Development/hermes-dashboard-esp32/hermes-dash-esp32"
FQBN="esp32:esp32:esp32c6:CDCOnBoot=cdc,PartitionScheme=huge_app"   # huge_app REQUIRED (FastLED)
HOME_HOST="omarchy-home"

case "$UNIT" in
  home) OUT="$SKETCH_DIR/build-home"; FLAGS="-DUNIT_HOME" ;;
  work) OUT="$SKETCH_DIR/build-work"; FLAGS="" ;;
  *) echo "usage: $0 {home|work} [--build-only]" >&2; exit 2 ;;
esac

cd "$SKETCH_DIR"
echo "== compiling $UNIT build (landscape UI, 4 pages) =="
rm -rf "$OUT"
# shellcheck disable=SC2086
arduino-cli compile --fqbn "$FQBN" --build-property compiler.cpp.extra_flags=$FLAGS --output-dir "$OUT" .

BIN="$OUT/hermes-dash-esp32.ino.merged.bin"
echo "== built: $BIN ($(stat -f%z "$BIN") bytes) =="
for s in $( [[ "$UNIT" == home ]] && echo "onCortex 192.168.1.171" || echo "cortexWork 192.168.4.37" ); do
  grep -q "$s" "$BIN" || { echo "FAIL: expected config string '$s' missing from image" >&2; exit 1; }
done
echo "== config strings verified in image =="
[[ $BUILD_ONLY == 1 ]] && exit 0

if [[ "$UNIT" == home ]]; then
  echo "== flashing on $HOME_HOST:/dev/ttyACM0 =="
  scp -q "$BIN" "$HOME_HOST:/tmp/hermes-dash-home.bin"
  ssh "$HOME_HOST" '~/.local/bin/esptool --chip esp32c6 --port /dev/ttyACM0 --baud 460800 --before default_reset --after hard_reset write_flash -z 0x0 /tmp/hermes-dash-home.bin'
  echo "== verify: ssh $HOME_HOST 'journalctl --user -u hermes-dash-relay --since \"-1m\" | grep 192.168.1.174' =="
else
  PORT=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)
  [[ -n "$PORT" ]] || { echo "FAIL: no /dev/cu.usbmodem* (board not attached?)" >&2; exit 1; }
  echo "== flashing on $PORT =="
  arduino-cli upload -p "$PORT" --fqbn "$FQBN" --input-dir "$OUT" .
fi
