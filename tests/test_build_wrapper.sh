#!/usr/bin/env bash
# Tests for build-flash.sh — bash only, no dependencies.
#
# Strategy: run the wrapper in a sandbox COPY of this repo (so a bug can
# never touch the real checkout or $HOME/Development), with stub
# arduino-cli / scp / ssh on PATH that log their invocation (pwd + args)
# and behave benignly.
#
# Run: bash tests/test_build_wrapper.sh
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
WRAP="$REPO/build-flash.sh"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ok   - $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL - $1"; }

# ── sandbox: a copy of the repo at a DIFFERENT path (proves the wrapper
#    resolves its own checkout, audit A1)
SANDBOX="$(mktemp -d /tmp/dash-wrap-test.XXXXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT
cp -R "$REPO/hermes-dash-esp32" "$SANDBOX/"
cp "$WRAP" "$SANDBOX/"
mkdir -p "$SANDBOX/stub"
chmod +x "$SANDBOX/build-flash.sh"

# fake unit configs (the real ones are gitignored; tests must not need them)
cat > "$SANDBOX/hermes-dash-esp32/wifi_config.home.h" <<'EOF'
#define WIFI_SSID "testSsidHome"
#define WIFI_PASS "not-a-real-password"
#define DASHBOARD_URL "http://192.168.1.171:9120/api/status"
EOF
cat > "$SANDBOX/hermes-dash-esp32/wifi_config.work.h" <<'EOF'
#define WIFI_SSID "testSsidWork"
#define WIFI_PASS "not-a-real-password"
#define DASHBOARD_URL "http://192.168.4.37:9120/api/status"
EOF

# ── stubs that log pwd + args, and fake a successful build artifact
LOG="$SANDBOX/calls.log"
export LOG   # stubs are written with quoted heredocs; they log via \$LOG
cat > "$SANDBOX/stub/arduino-cli" <<EOF
#!/usr/bin/env bash
echo "arduino-cli pwd=\$(pwd) args=\$*" >> "$LOG"
if [[ "\$1" == "compile" ]]; then
  # emulate: emit a merged.bin containing the config markers of BOTH units
  # (grep -F succeeds either way; the wrapper checks the right ones per unit)
  out=""
  while [[ \$# -gt 0 ]]; do
    [[ "\$1" == "--output-dir" ]] && { shift; out="\$1"; }
    shift
  done
  mkdir -p "\$out"
  printf 'testSsidHome\ntestSsidWork\n192.168.1.171\n192.168.4.37\n' \\
    > "\$out/hermes-dash-esp32.ino.merged.bin"
fi
exit 0
EOF
cat > "$SANDBOX/stub/scp" <<EOF
#!/usr/bin/env bash
echo "scp pwd=\$(pwd) args=\$*" >> "$LOG"
exit 0
EOF
cat > "$SANDBOX/stub/ssh" <<EOF
#!/usr/bin/env bash
echo "ssh pwd=\$(pwd) args=\$*" >> "$LOG"
if [[ "\$*" == *"mktemp -d"* ]]; then echo "/tmp/hermes-dash.ABC12345"; exit 0; fi
exit 0
EOF
chmod +x "$SANDBOX/stub/"*

run() { ( cd /tmp && PATH="$SANDBOX/stub:$PATH" "$SANDBOX/build-flash.sh" "$@" ); }
calls() { grep -c . "$LOG" 2>/dev/null || true; }
reset() { : > "$LOG"; }

echo "== build-flash.sh wrapper tests =="

# 1. default run does NOT flash (no scp/ssh/esptool/upload)
reset
out="$(run home 2>&1)"; rc=$?
if [[ $rc -eq 0 && "$(grep -c -E '^(scp|ssh) ' "$LOG")" -eq 0 && "$out" == *"not flashing"* ]]; then
  ok "default run builds + verifies only, never flashes (rc=0, no scp/ssh)"
else
  bad "default run flashed or failed: rc=$rc log=[$(cat "$LOG")] out=[$out]"
fi

# 2. --build-only behaves like the default (no flash)
reset
out="$(run home --build-only 2>&1)"; rc=$?
if [[ $rc -eq 0 && "$(grep -c -E '^(scp|ssh) ' "$LOG")" -eq 0 ]]; then
  ok "--build-only does not flash"
else
  bad "--build-only flashed: rc=$rc log=[$(cat "$LOG")]"
fi

# 3. --flash DOES transfer + flash (home: scp + ssh)
reset
out="$(run home --flash 2>&1)"; rc=$?
if [[ $rc -eq 0 && "$(grep -c '^scp ' "$LOG")" -ge 1 && "$(grep -c '^ssh ' "$LOG")" -ge 2 ]]; then
  ok "--flash home performs scp + remote esptool via ssh"
else
  bad "--flash home did not transfer: rc=$rc log=[$(cat "$LOG")] out=[$out]"
fi

# 3b. --flash uses a private mktemp dir on the remote, not a fixed path
if grep -q 'hermes-dash.ABC12345' "$LOG" && ! grep -q '/tmp/hermes-dash-home.bin' "$LOG"; then
  ok "remote transfer uses validated mktemp dir, cleaned up afterwards"
else
  bad "remote transfer path wrong: log=[$(cat "$LOG")]"
fi

# 3c. --flash prints pre-flash summary with resolved dir/unit/rev
if [[ "$out" == *"$SANDBOX/hermes-dash-esp32"* && "$out" == *"unit       : home"* ]]; then
  ok "pre-flash summary shows resolved source dir + unit"
else
  bad "pre-flash summary missing: out=[$out]"
fi

# 4. unknown flag → non-zero, NO side effects (no compile/transfer)
reset
out="$(run home --build-onyl 2>&1)"; rc=$?
if [[ $rc -ne 0 && $(calls) -eq 0 && "$out" == *"unknown argument"* ]]; then
  ok "unknown flag exits non-zero before any build/transfer"
else
  bad "unknown flag not rejected safely: rc=$rc calls=$(calls) out=[$out]"
fi

# 5. unknown flag with flash potential: even --flash is ignored on bad args
reset
out="$(run hme --flash 2>&1)"; rc=$?
if [[ $rc -ne 0 && $(calls) -eq 0 ]]; then
  ok "misspelled unit rejected before any build/transfer"
else
  bad "misspelled unit not rejected safely: rc=$rc calls=$(calls)"
fi

# 6. extra positional rejected
reset
out="$(run home extra 2>&1)"; rc=$?
if [[ $rc -ne 0 && $(calls) -eq 0 ]]; then
  ok "extra positional argument rejected"
else
  bad "extra positional accepted: rc=$rc"
fi

# 7. missing unit rejected
reset
out="$(run 2>&1)"; rc=$?
if [[ $rc -ne 0 && $(calls) -eq 0 && "$out" == *"usage:"* ]]; then
  ok "missing unit rejected with usage line"
else
  bad "missing unit not rejected: rc=$rc out=[$out]"
fi

# 8. resolves its OWN checkout, never $HOME/Development (A1)
reset
run home --build-only >/dev/null 2>&1
first_pwd="$(grep '^arduino-cli' "$LOG" | head -1 | sed 's/.*pwd=\(.*\) args=.*/\1/')"
if [[ "$first_pwd" == "$SANDBOX/hermes-dash-esp32" ]]; then
  ok "compiles the sandbox checkout (pwd=$first_pwd), not \$HOME"
else
  bad "compiled wrong dir: [$first_pwd]"
fi
if [[ ! -e "$HOME/Development/hermes-dashboard-esp32/build-home" ]]; then
  ok "live checkout build dir untouched"
else
  # only a failure if THIS test created it — it must not have (see 8)
  bad "live checkout build dir was touched"
fi

# 9. missing unit config rejected before compiling
mv "$SANDBOX/hermes-dash-esp32/wifi_config.home.h" "$SANDBOX/h.conf"
reset
out="$(run home 2>&1)"; rc=$?
if [[ $rc -ne 0 && $(calls) -eq 0 && "$out" == *"wifi_config.home.h not found"* ]]; then
  ok "missing unit config rejected before any compile"
else
  bad "missing config not rejected: rc=$rc calls=$(calls) out=[$out]"
fi
mv "$SANDBOX/h.conf" "$SANDBOX/hermes-dash-esp32/wifi_config.home.h"

# 10. marker mismatch → refuse to proceed (simulated bad image)
cat > "$SANDBOX/stub/arduino-cli" <<'EOF'
#!/usr/bin/env bash
echo "arduino-cli pwd=$(pwd) args=$*" >> "$LOG"
if [[ "$1" == "compile" ]]; then
  out=""
  while [[ $# -gt 0 ]]; do
    [[ "$1" == "--output-dir" ]] && { shift; out="$1"; }
    shift
  done
  mkdir -p "$out"
  printf 'WRONG-IMAGE-CONTENTS\n' > "$out/hermes-dash-esp32.ino.merged.bin"
fi
exit 0
EOF
chmod +x "$SANDBOX/stub/arduino-cli"
reset
out="$(run home --flash 2>&1)"; rc=$?
if [[ $rc -ne 0 && "$out" == *"missing from image"* && "$(grep -c -E '^(scp|ssh) ' "$LOG")" -eq 0 ]]; then
  ok "image without unit markers is refused BEFORE transfer"
else
  bad "bad image not refused: rc=$rc log=[$(cat "$LOG")] out=[$out]"
fi

# 11. missing sketch dir → loud failure
mv "$SANDBOX/hermes-dash-esp32" "$SANDBOX/sketch.bak"
reset
out="$(run home 2>&1)"; rc=$?
if [[ $rc -ne 0 && $(calls) -eq 0 && "$out" == *"sketch dir not found"* ]]; then
  ok "absent sketch dir fails loudly before anything"
else
  bad "absent sketch dir not handled: rc=$rc out=[$out]"
fi
mv "$SANDBOX/sketch.bak" "$SANDBOX/hermes-dash-esp32"

# 12. R2: garbage from remote mktemp → non-zero, clear message, and the
# cleanup NEVER rm -rf's the unvalidated string (only validated paths)
cat > "$SANDBOX/stub/ssh" <<'EOF'
#!/usr/bin/env bash
echo "ssh pwd=$(pwd) args=$*" >> "$LOG"
if [[ "$*" == *"mktemp -d"* ]]; then echo "WEIRDOUTPUT"; exit 0; fi
exit 0
EOF
chmod +x "$SANDBOX/stub/ssh"
# restore a good compile stub first (test 10 replaced it with the bad-image one)
cat > "$SANDBOX/stub/arduino-cli" <<'EOF'
#!/usr/bin/env bash
echo "arduino-cli pwd=$(pwd) args=$*" >> "$LOG"
if [[ "$1" == "compile" ]]; then
  out=""
  while [[ $# -gt 0 ]]; do
    [[ "$1" == "--output-dir" ]] && { shift; out="$1"; }
    shift
  done
  mkdir -p "$out"
  printf 'testSsidHome\ntestSsidWork\n192.168.1.171\n192.168.4.37\n' > "$out/hermes-dash-esp32.ino.merged.bin"
fi
exit 0
EOF
chmod +x "$SANDBOX/stub/arduino-cli"
reset
out="$(run home --flash 2>&1)"; rc=$?
rmcount="$(grep -c 'rm -rf' "$LOG" 2>/dev/null || true)"
rmtargets="$(grep 'rm -rf' "$LOG" 2>/dev/null || true)"
if [[ $rc -ne 0 && "$out" == *"FAIL: remote did not return a valid temp dir"* ]]; then
  if [[ "$rmcount" -eq 0 ]]; then
    ok "R2: garbage mktemp output → non-zero + clear message, NO rm -rf issued"
  else
    bad "R2: cleanup rm -rf'd an unvalidated path: [$rmtargets]"
  fi
else
  bad "R2: garbage mktemp not handled: rc=$rc out=[$out] rm=[$rmtargets]"
fi
# and confirm a VALID run still cleans up (regression: guard must not
# disable legitimate cleanup)
cat > "$SANDBOX/stub/ssh" <<'EOF'
#!/usr/bin/env bash
echo "ssh pwd=$(pwd) args=$*" >> "$LOG"
if [[ "$*" == *"mktemp -d"* ]]; then echo "/tmp/hermes-dash.ABC12345"; exit 0; fi
exit 0
EOF
chmod +x "$SANDBOX/stub/ssh"
reset
out="$(run home --flash 2>&1)"; rc=$?
if [[ $rc -eq 0 && "$(grep -c "rm -rf '/tmp/hermes-dash.ABC12345'" "$LOG")" -ge 1 ]]; then
  ok "R2: valid mktemp path still cleaned up by the guarded cleanup"
else
  bad "R2: guarded cleanup skipped legitimate cleanup: rc=$rc log=[$(cat "$LOG")]"
fi

echo
echo "passed=$PASS failed=$FAIL"
[[ $FAIL -eq 0 ]]
