#!/usr/bin/env bash
# Build (and only with --flash, flash) one ESP32 unit.
#
#   ./build-flash.sh home              # compile + verify image (NO flash)
#   ./build-flash.sh home --flash      # compile + verify + flash home unit
#   ./build-flash.sh work --flash      # compile + verify + flash a locally
#                                      #   attached board over USB
#   ./build-flash.sh home --build-only # explicit alias of the default
#
# Flashing is NEVER implicit: without --flash nothing is transferred and no
# hardware is touched. The script resolves the project relative to ITSELF
# (not $HOME), so a copy elsewhere builds its own checkout, and any
# unrecognised argument aborts before any deletion, build or transfer.
#
# Remote transfer (home unit) goes to a private temp dir on the remote
# (mktemp -d, mode 700) that is removed afterwards.
#
# Unit configs live in hermes-dash-esp32/wifi_config.{work,home}.h;
# wifi_config.h is only a selector, so flashing one unit can never overwrite
# the other's credentials. The expected image markers (SSID + dashboard
# host) are read FROM that unit's config, so no real hostname or SSID is
# baked into this script.
#
# Overridable environment:
#   HERMES_DASH_REMOTE_HOST  remote flash host          (default omarchy-home)
#   HERMES_DASH_REMOTE_USER  remote user (default: ssh config default)
#   HERMES_DASH_REMOTE_DEV   remote serial device       (default /dev/ttyACM0)
#   HERMES_DASH_LOCAL_PORT   local (work unit) serial port (default: scan
#                             /dev/cu.usbmodem*)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKETCH_DIR="$SCRIPT_DIR/hermes-dash-esp32"

usage() {
  echo "usage: $0 {home|work} [--build-only|--flash] [--remote-host HOST] [--remote-user USER] [--remote-dev DEV] [--local-port PORT]" >&2
}

# ── Strict argument parsing (audit A2): every flag recognised, exactly one
#    unit, no extra positionals; anything unknown exits BEFORE any deletion,
#    build, transfer or hardware access.
UNIT=""
WANT_FLASH=0
BUILD_ONLY_SEEN=0
REMOTE_HOST="${HERMES_DASH_REMOTE_HOST:-omarchy-home}"
REMOTE_USER="${HERMES_DASH_REMOTE_USER:-}"
REMOTE_DEV="${HERMES_DASH_REMOTE_DEV:-/dev/ttyACM0}"
LOCAL_PORT="${HERMES_DASH_LOCAL_PORT:-}"

while (( $# )); do
  case "$1" in
    home|work)
      [[ -z "$UNIT" ]] || { echo "FAIL: unit given twice ('$UNIT' and '$1')" >&2; usage; exit 2; }
      UNIT="$1"
      ;;
    --build-only) BUILD_ONLY_SEEN=1 ;;
    --flash)      WANT_FLASH=1 ;;
    --remote-host) shift; [[ $# -ge 1 && "$1" != -* ]] || { echo "FAIL: --remote-host needs a value" >&2; usage; exit 2; }; REMOTE_HOST="$1" ;;
    --remote-user) shift; [[ $# -ge 1 && "$1" != -* ]] || { echo "FAIL: --remote-user needs a value" >&2; usage; exit 2; }; REMOTE_USER="$1" ;;
    --remote-dev)  shift; [[ $# -ge 1 && "$1" != -* ]] || { echo "FAIL: --remote-dev needs a value" >&2; usage; exit 2; }; REMOTE_DEV="$1" ;;
    --local-port)  shift; [[ $# -ge 1 && "$1" != -* ]] || { echo "FAIL: --local-port needs a value" >&2; usage; exit 2; }; LOCAL_PORT="$1" ;;
    *)
      echo "FAIL: unknown argument '$1'" >&2
      usage
      exit 2
      ;;
  esac
  shift
done

[[ -n "$UNIT" ]] || { echo "FAIL: missing unit argument" >&2; usage; exit 2; }
if [[ $BUILD_ONLY_SEEN -eq 1 && $WANT_FLASH -eq 1 ]]; then
  echo "FAIL: --build-only and --flash are mutually exclusive" >&2; exit 2
fi

# ── Resolve the sketch relative to THIS script (audit A1), never $HOME.
if [[ ! -d "$SKETCH_DIR" ]]; then
  echo "FAIL: sketch dir not found next to this script: $SKETCH_DIR" >&2
  exit 2
fi

case "$UNIT" in
  home) FLAGS="-DUNIT_HOME" ;;
  work) FLAGS="" ;;
esac
OUT="$SKETCH_DIR/build-$UNIT"
FQBN="esp32:esp32:esp32c6:CDCOnBoot=cdc,PartitionScheme=huge_app"   # huge_app REQUIRED (FastLED)

CONFIG="$SKETCH_DIR/wifi_config.$UNIT.h"
if [[ ! -f "$CONFIG" ]]; then
  echo "FAIL: unit config $CONFIG not found (copy the .example and fill it in; it is gitignored)" >&2
  exit 2
fi
# Expected image markers come from the unit config itself.
SSID_MARKER="$(sed -n 's/^#define[[:space:]]*WIFI_SSID[[:space:]]*"\(.*\)".*/\1/p' "$CONFIG" | head -1)"
URL_MARKER="$(sed -n 's|^#define[[:space:]]*DASHBOARD_URL[[:space:]]*"http://\([^/:"<>]*\).*|\1|p' "$CONFIG" | head -1)"
if [[ -z "$SSID_MARKER" || -z "$URL_MARKER" ]]; then
  echo "FAIL: could not read WIFI_SSID / DASHBOARD_URL markers from $CONFIG" >&2
  exit 2
fi

GIT_REV="$(cd "$SCRIPT_DIR" && git describe --always --dirty 2>/dev/null || echo 'unknown (not a git repo)')"

cd "$SKETCH_DIR"
echo "== compiling $UNIT build (landscape UI, 4 pages) in $SKETCH_DIR =="
rm -rf "$OUT"
# shellcheck disable=SC2086
arduino-cli compile --fqbn "$FQBN" --build-property compiler.cpp.extra_flags=$FLAGS --output-dir "$OUT" .

BIN="$OUT/hermes-dash-esp32.ino.merged.bin"
[[ -f "$BIN" ]] || { echo "FAIL: compiler produced no $BIN" >&2; exit 1; }
echo "== built: $BIN ($(wc -c < "$BIN" | tr -d ' ') bytes) =="
for m in "$SSID_MARKER" "$URL_MARKER"; do
  grep -F -q -- "$m" "$BIN" || { echo "FAIL: expected config string '$m' missing from image" >&2; exit 1; }
done
echo "== config strings verified in image (SSID '$SSID_MARKER', host '$URL_MARKER') =="

if [[ $WANT_FLASH -eq 0 ]]; then
  echo "== build-only: not flashing (pass --flash to transfer + flash) =="
  exit 0
fi

# ── Pre-flash summary (audit A1): everything an operator should confirm.
echo "== PRE-FLASH =========================================================="
echo "    source dir : $SKETCH_DIR"
echo "    unit       : $UNIT"
echo "    git rev    : $GIT_REV"
echo "    config     : $CONFIG"
echo "    markers    : SSID '$SSID_MARKER', host '$URL_MARKER' (verified in image)"
echo "======================================================================="

REMOTE_TMP=""
cleanup_remote() {
  if [[ -n "$REMOTE_TMP" ]]; then
    ssh "$REMOTE" "rm -rf '$REMOTE_TMP'" || echo "WARN: could not remove remote temp dir $REMOTE_TMP" >&2
  fi
}

if [[ "$UNIT" == home ]]; then
  REMOTE="${REMOTE_USER:+$REMOTE_USER@}$REMOTE_HOST"
  echo "== flashing on $REMOTE:$REMOTE_DEV =="
  REMOTE_TMP="$(ssh "$REMOTE" 'd=$(mktemp -d /tmp/hermes-dash.XXXXXXXX) && chmod 700 "$d" && echo "$d"')"
  if [[ -z "$REMOTE_TMP" || "$REMOTE_TMP" != /tmp/hermes-dash.* ]]; then
    echo "FAIL: remote did not return a valid temp dir (got: '$REMOTE_TMP')" >&2
    exit 1
  fi
  trap cleanup_remote EXIT
  scp -q "$BIN" "$REMOTE:$REMOTE_TMP/hermes-dash-$UNIT.bin"
  ssh "$REMOTE" "~/.local/bin/esptool --chip esp32c6 --port $REMOTE_DEV --baud 460800 --before default_reset --after hard_reset write_flash -z 0x0 $REMOTE_TMP/hermes-dash-$UNIT.bin"
  cleanup_remote
  REMOTE_TMP=""
  trap - EXIT
  echo "== verify: ssh $REMOTE 'journalctl --user -u hermes-dash-relay --since \"-1m\" | grep $URL_MARKER' =="
else
  if [[ -z "$LOCAL_PORT" ]]; then
    LOCAL_PORT="$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)"
  fi
  [[ -n "$LOCAL_PORT" ]] || { echo "FAIL: no /dev/cu.usbmodem* (board not attached?)" >&2; exit 1; }
  echo "== flashing on $LOCAL_PORT =="
  arduino-cli upload -p "$LOCAL_PORT" --fqbn "$FQBN" --input-dir "$OUT" .
fi
