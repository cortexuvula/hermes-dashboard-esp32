// ─────────────────────────────────────────────────────
// Deployment Configuration — one file per ESP32 unit
// ─────────────────────────────────────────────────────
// wifi_config.h is a SELECTOR: it pulls in the unit's own config so a build
// for one board can never clobber the other board's credentials.
//
//   WORK unit (office LAN, cortexWork)  → wifi_config.work.h   [default]
//   HOME unit (home LAN,  onCortex)     → wifi_config.home.h   [-DUNIT_HOME]
//
// Compile home:  arduino-cli compile --fqbn esp32:esp32:esp32c6:CDCOnBoot=cdc,PartitionScheme=huge_app \
//                    --build-property compiler.cpp.extra_flags=-DUNIT_HOME --output-dir build-home .
// Compile work:  same but omit -DUNIT_HOME (see build-flash.sh).

#if defined(UNIT_HOME)
  #include "wifi_config.home.h"
#else
  #include "wifi_config.work.h"
#endif
