// Hermes Dashboard Monitor — ESP32-C6 1.47" LCD (LANDSCAPE)
// Displays live Hermes gateway stats from the local relay (:9120, merges
// tokens + host stats from the Mac usage-server).
//
// Board: Waveshare ESP32-C6-LCD-1.47
// Display: ST7789V3, 172×320 (round), SPI — MOSI=6 SCLK=7 CS=14 DC=15 RST=21 BL=22
// Chip: ESP32-C6 (RISC-V, 160MHz, 4MB Flash)
// Libs: LovyanGFX (TFT_eSPI does not support C6 — VSPI registers don't exist on RISC-V)
//       FastLED (WS2812 RGB on GPIO8)
//
// Audit fixes applied: A6, A7, A8, A9, A10, A11, A12

#include "wifi_config.h"
#ifndef WIFI_SSID
#define WIFI_SSID "YOUR_SSID"
#define WIFI_PASS "YOUR_PASSWORD"
#endif

#ifndef DASHBOARD_URL
#define DASHBOARD_URL "http://192.168.4.37:9120/api/status"
#endif

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <LovyanGFX.hpp>
#include <FastLED.h>

// ── Board init — Waveshare ESP32-C6-LCD-1.47 ─────────
class LGFX : public lgfx::LGFX_Device
{
  lgfx::Panel_ST7789 _panel;
  lgfx::Bus_SPI _bus;
  lgfx::Light_PWM _light;

public:
  LGFX(void)
  {
    { // SPI bus: MOSI=6, SCLK=7, DC=15
      auto cfg = _bus.config();
      cfg.spi_host = SPI2_HOST;      // FSPI on C6
      cfg.spi_mode = 0;
      cfg.freq_write = 80000000;
      cfg.freq_read = 16000000;
      cfg.spi_3wire = false;
      cfg.use_lock = true;
      cfg.dma_channel = SPI_DMA_CH_AUTO;
      cfg.pin_sclk = 7;
      cfg.pin_mosi = 6;
      cfg.pin_miso = -1;             // no MISO on LCD bus
      cfg.pin_dc = 15;
      _bus.config(cfg);
      _panel.setBus(&_bus);
    }
    { // Panel: ST7789V3 172×320 — glass is 240×320, round 172px window is centered
      auto cfg = _panel.config();
      cfg.pin_cs = 14;
      cfg.pin_rst = 21;
      cfg.panel_width = 172;
      cfg.panel_height = 320;
      cfg.offset_x = 34;             // (240-172)/2
      cfg.offset_y = 0;
      cfg.invert = true;             // ST7789V3 (Waveshare init sends INVON)
      cfg.rgb_order = false;
      _panel.config(cfg);
    }
    { // Backlight: GPIO22, PWM — keep ≤ 50% (Waveshare warning)
      auto cfg = _light.config();
      cfg.pin_bl = 22;
      cfg.invert = false;
      cfg.freq = 12000;
      cfg.pwm_channel = 0;
      _light.config(cfg);
      _panel.setLight(&_light);
    }
    setPanel(&_panel);
  }
};
LGFX tft;

// ── Display constants ─────────────────────────────────
// Landscape UI: panel rotated 90° → 320x172, circle center (160,86)
#define SCREEN_W  320
#define SCREEN_H  172
#define CENTER_X  160
#define CENTER_Y  86
#define ROUND_R   80   // usable radius inside round glass (layout hard limit)

// ── LAYOUT CONSTANTS ──────────────────────────────────────
// All display coordinates in one place. tools/check_layout.py reads these.
// If you move anything, update the constant — the checker will catch drift.
#define LAYOUT_CENTER_X       160
#define LAYOUT_CENTER_Y       86

// STATUS page
#define STATUS_RING_R         50
#define STATUS_RING_DOT_R     4
#define STATUS_OVERFLOW_DX    58     // CENTER_X + this = overflow x
#define STATUS_OVERFLOW_DY    25     // CENTER_Y - this = overflow y
#define STATUS_NUM_Y          72
#define STATUS_NUM_W          48     // DejaVu40 2-digit
#define STATUS_SESS_Y         94     // was 100 — glyph band collided with the state line
#define STATUS_STATE_Y        110    // was 118 — its band overlapped the disk bar (120..126)
#define STATUS_DISK_BAR_W     112
#define STATUS_DISK_BAR_H     6
#define STATUS_DISK_BAR_DY    32     // CENTER_Y + this (was 34 — clears the label)
#define STATUS_DISK_LBL_DY    10     // disk_bar_y + this
#define STATUS_FOOTER_DY      52     // CENTER_Y + this
#define STATUS_DOT_DX         54     // CENTER_X + this (fixed inset; 54 keeps it clear of the bounded footer)
#define STATUS_DOT_R          3

// TOKENS page
#define TK_HEAD_Y             24     // Font0 (was Font2@18 — exceeded R=80)
#define TK_QUAL_Y             34     // Font0
#define TK_BIG_Y              66     // DejaVu40 (moved to fit 6-char worst case in R=80)
#define TK_BIG_W              150    // 6 chars DejaVu40 worst case
#define TK_DET1_Y             88     // Font0 for 24H IN/OUT (was Font2 — too wide for R=80)
#define TK_DET2_Y             112    // Font0 (was 110)
#define TK_DET3_Y             126    // Font0 (moved from 130 — calls worst-case r=79.3 at R=80)

// HEALTH page
#define HL_HEAD_Y             26
#define HL_ROW0_Y             48
#define HL_ROW1_Y             70
#define HL_ROW2_Y             92
#define HL_ROW3_Y             114
#define HL_DOT_DX             56     // CENTER_X - this
#define HL_DOT_R              3
#define HL_ERR_DX             55     // CENTER_X + this
#define HL_ERR_Y              92
#define HL_AUTH_Y             128    // was 134 — glyph band overlapped the host line (136..144)
#define HL_HOST_Y             140    // was 150 — exceeded R=80 (moved up)

// ── Colors (RGB565) ───────────────────────────────────
#define C_BG        0x0000  // black
#define C_OK        0x07E0  // green
#define C_DOWN      0xF800  // red
#define C_DEGRADED  0xFD20  // orange
#define C_TEXT      0xFFFF  // white
#define C_DIM       0x7BEF  // dark grey
#define C_YELLOW    0xFFE0
#define C_ACCENT    0x4A90  // Hermes blue-ish

// ── RGB LED (GPIO8, WS2812-style) ─────────────────────
#define LED_PIN     8
#define NUM_LEDS    1
CRGB leds[NUM_LEDS];

// Forward decls
extern bool fetch_ok;
extern bool gateway_degraded;
extern bool gateway_busy;
extern unsigned long last_success;

void set_led(uint8_t r, uint8_t g, uint8_t b) {
    CRGB c(r, g, b);
    if (leds[0] != c) { leds[0] = c; FastLED.show(); }
}

void led_init() {
    FastLED.addLeds<WS2812, LED_PIN, GRB>(leds, NUM_LEDS);
    FastLED.setBrightness(96);
    set_led(0, 0, 0);
}

// A12 fix: LED pulse uses last_success (set AFTER successful fetch),
// not last_fetch (which was set before the network call).
void led_update() {
    if (!fetch_ok)                 { set_led(255, 0, 0); }     // offline
    else if (gateway_degraded || gateway_busy) { set_led(255, 120, 0); } // busy/degraded
    else if (millis() - last_success < 250)    { set_led(0, 200, 0); }   // fresh fetch pulse
    else                           { set_led(0, 0, 0); }
}

// ── A7: Period usage struct (one per time window) ─────
// No page may read another period's data.
struct PeriodUsage {
    long long total  = 0;
    long long input  = 0;
    long long output = 0;
    long long cache  = 0;
    double    cost   = 0.0;
    long long calls  = 0;
    long long sessions = 0;
    bool      valid  = false;  // false = data absent/null → show "--"
};

// ── A10: Per-platform state (max 8 rendered) ──────────
#define MAX_PLATFORMS 8
struct PlatformState {
    bool connected      = false;
    bool needs_attention = false;
};

// ── Globals ───────────────────────────────────────────
unsigned long last_fetch   = 0;
unsigned long last_success = 0;          // A12: set AFTER successful fetch
const unsigned long FETCH_INTERVAL = 10000; // 10s poll

bool fetch_ok = false;
int  active_sessions = 0;
bool active_sessions_known = false;
bool gateway_busy    = false;
bool gateway_busy_known = false;
bool gateway_degraded = false;
bool overall_known = false;

// A10: per-platform state replaces attention_idx[] and platforms_up
PlatformState g_platforms[MAX_PLATFORMS];
int g_platforms_rendered = 0;  // how many dots to draw (0–8)
int g_platforms_total    = 0;  // actual total from JSON (for overflow marker)

int    disk_pct = 0;
bool   disk_known = false;
String version_str = "";
String profile_count = "";
bool   profile_known = false;
bool   can_update = false;

// A6 tri-state session validity (was boolean)
enum SessionState : uint8_t {
    SESS_UNKNOWN  = 0,
    SESS_EXPIRED  = 1,
    SESS_OK       = 2
};
SessionState g_session_state = SESS_UNKNOWN;

// A6 tri-state component health
enum CompState : uint8_t {
    COMP_UNKNOWN = 0,
    COMP_DOWN    = 1,
    COMP_OK      = 2
};
CompState comp_gw = COMP_UNKNOWN, comp_dash = COMP_UNKNOWN,
          comp_storage = COMP_UNKNOWN, comp_platforms = COMP_UNKNOWN;
int dash_errors = 0;

// A6: host absent → unknown (not silently carried over)
bool g_host_known = false;
int  host_cpu = 0;
int  host_ram = 0;

// A7: one struct per period — no cross-period reads
PeriodUsage usage_24h;
PeriodUsage usage_7d;

// A6: staleness from usage_age_s (board has no clock/NTP)
int  g_usage_age_s = -1;  // -1 = unknown; >60 = stale
bool g_age_known = false;  // false = age unknown → treat as stale
int  g_schema      = 0;   // contract version; 0 = not seen

const unsigned long PAGE_MS = 12000; // rotate pages every 12s
const int PAGE_COUNT = 4;            // STATUS / TOKENS 24H / TOKENS 7D / HEALTH

// A9: max response size (32 KB — protect no-PSRAM heap)
const int MAX_RESPONSE_SIZE = 32768;

// ── Helpers ───────────────────────────────────────────
// A6: age unknown → treat as stale (never present unknown as fresh)
static inline bool is_stale() { return !g_age_known || g_usage_age_s > 60; }

// Token formatting (e.g. 79599877 → "79.6M")
String fmt_tokens(long long v) {
    if (v >= 100000000LL) { char b[16]; snprintf(b, sizeof b, "%.0fM", v / 1000000.0); return b; }
    if (v >= 1000000LL)   { char b[16]; snprintf(b, sizeof b, "%.1fM", v / 1000000.0); return b; }
    if (v >= 1000LL)      { char b[16]; snprintf(b, sizeof b, "%.0fk", v / 1000.0); return b; }
    return String((long)v);
}

// ── JSON fetch (A6/A7/A9) ────────────────────────────
bool fetch_dashboard() {
    HTTPClient http;
    http.begin(DASHBOARD_URL);
    http.setTimeout(8000);
    int code = http.GET();
    Serial.printf("[dash] GET %s -> %d (%s)\n", DASHBOARD_URL, code, http.errorToString(code).c_str());
    if (code != 200) {
        http.end();
        return false;
    }

    // A9 + #4: reject oversized responses (protect ~300KB heap on no-PSRAM C6)
    // Fast-path when Content-Length is known; bounded stream read when unknown.
    int resp_size = http.getSize();
    if (resp_size > MAX_RESPONSE_SIZE) {
        Serial.printf("[dash] response too large: %d bytes (max %d) — rejected\n", resp_size, MAX_RESPONSE_SIZE);
        http.end();
        return false;
    }

    String payload;
    if (resp_size > 0) {
        // Known length: read in one shot (safe — already size-checked above)
        payload = http.getString();
    } else {
        // #4/F2: Unknown length (chunked): bounded stream read, abort on overflow.
        // Our relay currently answers HTTP/1.0 and CLOSES the socket after the body,
        // so the loop exits immediately on !connected().  The idle guard below is
        // a safety net for keep-alive or misbehaving responders — it prevents an
        // 8 s burn inside loop() which would freeze page rotation, blink and LED.
        WiFiClient* stream = http.getStreamPtr();
        payload.reserve(4096);
        unsigned long t0 = millis();
        unsigned long last_byte = millis();
        while (stream->connected() && (millis() - t0 < 8000)) {
            size_t avail = stream->available();
            if (avail) {
                char buf[512];
                size_t to_read = (avail < sizeof(buf)) ? avail : sizeof(buf);
                size_t got = stream->readBytes(buf, to_read);
                if (payload.length() + got > (unsigned)MAX_RESPONSE_SIZE) {
                    Serial.printf("[dash] chunked response exceeded %d bytes — aborted\n", MAX_RESPONSE_SIZE);
                    http.end();
                    return false;
                }
                payload.concat(buf, got);
                last_byte = millis();
            } else {
                delay(1);
                // F2: idle guard — exit after 500 ms with no new bytes
                if (millis() - last_byte > 500) break;
            }
        }
    }
    http.end();

    if ((int)payload.length() > MAX_RESPONSE_SIZE) {
        Serial.printf("[dash] payload too large after read: %d bytes — rejected\n", (int)payload.length());
        return false;
    }

    // A9: ArduinoJson filter — only retain fields the board uses.
    // This avoids a full duplicate document on a no-PSRAM device.
    JsonDocument filter;
    filter["active_sessions"]      = true;
    filter["gateway_busy"]         = true;
    filter["version"]              = true;
    filter["overall"]              = true;
    filter["gateway_platforms"]    = true;
    filter["disk"]                 = true;
    filter["profiles"]             = true;
    filter["can_update_hermes"]    = true;
    filter["nous_session_valid"]   = true;
    filter["components"]           = true;
    filter["tokens_24h"]           = true;
    filter["tokens_7d"]            = true;
    filter["host"]                 = true;
    filter["usage_age_s"]          = true;
    filter["schema"]               = true;
    // #7: active_agents removed (dead code — fetched but never rendered)

    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, payload, DeserializationOption::Filter(filter));
    if (err) {
        // A9: distinguish parse errors from network errors in serial log
        Serial.printf("[dash] JSON PARSE ERROR: %s\n", err.c_str());
        return false;
    }

    // --- Core fields (#2: track presence, absent = unknown) ---
    if (doc["active_sessions"].is<int>()) {
        active_sessions = doc["active_sessions"].as<int>();
        active_sessions_known = true;
    } else {
        active_sessions_known = false;
    }
    
    if (doc["gateway_busy"].is<bool>()) {
        gateway_busy = doc["gateway_busy"].as<bool>();
        gateway_busy_known = true;
    } else {
        gateway_busy_known = false;
    }
    
    version_str = doc["version"] | "?";
    
    if (doc["overall"].is<const char*>()) {
        const char* overall = doc["overall"].as<const char*>();
        gateway_degraded = (overall && strcmp(overall, "degraded") == 0);
        overall_known = true;
    } else {
        gateway_degraded = false;
        overall_known = false;
    }

    // A6: schema version (log at boot, tolerate unknown)
    if (doc["schema"].is<int>()) {
        g_schema = doc["schema"].as<int>();
    }

    // A6: staleness — usage_age_s (null = unknown → treat as stale)
    if (doc["usage_age_s"].is<int>()) {
        g_usage_age_s = doc["usage_age_s"].as<int>();
        g_age_known = true;
    } else {
        g_usage_age_s = -1;
        g_age_known = false;
    }

    // --- A10: Platforms — per-platform state, max 8 rendered ---
    g_platforms_total    = 0;
    g_platforms_rendered = 0;
    JsonObject platforms = doc["gateway_platforms"].as<JsonObject>();
    for (JsonPair kv : platforms) {
        int idx = g_platforms_total;
        g_platforms_total++;
        if (idx < MAX_PLATFORMS) {
            const char* state = kv.value()["state"];
            g_platforms[idx].connected      = (state && strcmp(state, "connected") == 0);
            g_platforms[idx].needs_attention = kv.value()["needs_attention"] | false;
            g_platforms_rendered++;
        }
        // entries beyond MAX_PLATFORMS are counted for overflow marker only
    }

    // Disk (#2: track presence)
    JsonObject disk = doc["disk"];
    if (!disk.isNull() && disk["used_percent"].is<double>()) {
        disk_pct = (int)disk["used_percent"].as<double>();
        disk_known = true;
    } else {
        disk_known = false;
    }

    // Profiles (#2: track presence)
    JsonArray profiles = doc["profiles"].as<JsonArray>();
    if (!profiles.isNull()) {
        profile_count = String(profiles.size());
        profile_known = true;
    } else {
        profile_known = false;
    }

    // Update availability
    can_update = doc["can_update_hermes"] | false;

    // A6: session validity — tri-state (OK / EXPIRED / UNKNOWN)
    if (doc["nous_session_valid"].isNull() || !doc.containsKey("nous_session_valid")) {
        g_session_state = SESS_UNKNOWN;
    } else {
        const char* nsv = doc["nous_session_valid"];
        if (nsv && strcmp(nsv, "valid") == 0) g_session_state = SESS_OK;
        else                                   g_session_state = SESS_EXPIRED;
    }

    // A6: components — null = UNKNOWN (not silently healthy)
    // #6: also check inner status fields (absent = UNKNOWN, not DOWN)
    JsonObject comp = doc["components"];
    if (comp.isNull() || !doc.containsKey("components")) {
        comp_gw = comp_dash = comp_storage = comp_platforms = COMP_UNKNOWN;
        dash_errors = 0;
    } else {
        // Gateway
        if (comp["gateway"].is<JsonObject>() && comp["gateway"]["status"].is<const char*>()) {
            comp_gw = (strcmp(comp["gateway"]["status"].as<const char*>(), "ok") == 0) ? COMP_OK : COMP_DOWN;
        } else {
            comp_gw = COMP_UNKNOWN;
        }
        // Dashboard
        if (comp["dashboard"].is<JsonObject>() && comp["dashboard"]["status"].is<const char*>()) {
            comp_dash = (strcmp(comp["dashboard"]["status"].as<const char*>(), "ok") == 0) ? COMP_OK : COMP_DOWN;
            dash_errors = comp["dashboard"]["recent_unhandled_errors"] | 0;
        } else {
            comp_dash = COMP_UNKNOWN;
            dash_errors = 0;
        }
        // Storage
        if (comp["storage"].is<JsonObject>() && comp["storage"]["status"].is<const char*>()) {
            comp_storage = (strcmp(comp["storage"]["status"].as<const char*>(), "ok") == 0) ? COMP_OK : COMP_DOWN;
        } else {
            comp_storage = COMP_UNKNOWN;
        }
        // Platforms
        if (comp["platforms"].is<JsonObject>() && comp["platforms"]["status"].is<const char*>()) {
            comp_platforms = (strcmp(comp["platforms"]["status"].as<const char*>(), "ok") == 0) ? COMP_OK : COMP_DOWN;
        } else {
            comp_platforms = COMP_UNKNOWN;
        }
    }

    // A7: token usage — one struct per period, null = absent
    // #6: also check inner fields (null = absent, not zero)
    JsonObject tk24 = doc["tokens_24h"];
    if (!tk24.isNull() && doc.containsKey("tokens_24h")) {
        usage_24h.total    = tk24["total"].is<long long>() ? tk24["total"].as<long long>() : 0LL;
        usage_24h.input    = tk24["input"].is<long long>() ? tk24["input"].as<long long>() : 0LL;
        usage_24h.output   = tk24["output"].is<long long>() ? tk24["output"].as<long long>() : 0LL;
        usage_24h.cache    = tk24["cache"].is<long long>() ? tk24["cache"].as<long long>() : 0LL;
        usage_24h.cost     = tk24["est_cost"].is<double>() ? tk24["est_cost"].as<double>() : 0.0;
        usage_24h.calls    = tk24["api_calls"].is<long long>() ? tk24["api_calls"].as<long long>() : 0LL;
        usage_24h.sessions = tk24["sessions"].is<long long>() ? tk24["sessions"].as<long long>() : 0LL;
        usage_24h.valid    = true;
    } else {
        usage_24h = PeriodUsage();  // reset — valid=false → show "--"
    }

    // A7 fix: 7d now reads ALL fields including input/output (was missing)
    JsonObject tk7 = doc["tokens_7d"];
    if (!tk7.isNull() && doc.containsKey("tokens_7d")) {
        usage_7d.total    = tk7["total"].is<long long>() ? tk7["total"].as<long long>() : 0LL;
        usage_7d.input    = tk7["input"].is<long long>() ? tk7["input"].as<long long>() : 0LL;
        usage_7d.output   = tk7["output"].is<long long>() ? tk7["output"].as<long long>() : 0LL;
        usage_7d.cache    = tk7["cache"].is<long long>() ? tk7["cache"].as<long long>() : 0LL;
        usage_7d.cost     = tk7["est_cost"].is<double>() ? tk7["est_cost"].as<double>() : 0.0;
        usage_7d.calls    = tk7["api_calls"].is<long long>() ? tk7["api_calls"].as<long long>() : 0LL;
        usage_7d.sessions = tk7["sessions"].is<long long>() ? tk7["sessions"].as<long long>() : 0LL;
        usage_7d.valid    = true;
    } else {
        usage_7d = PeriodUsage();
    }

    // A6: host — null/absent = UNKNOWN (not silently carried over)
    // #6: also check inner fields
    JsonObject host = doc["host"];
    if (!host.isNull() && doc.containsKey("host")) {
        if (host["cpu_percent"].is<int>()) {
            host_cpu = host["cpu_percent"].as<int>();
        }
        if (host["ram_used_percent"].is<int>()) {
            host_ram = host["ram_used_percent"].as<int>();
        }
        g_host_known = host["cpu_percent"].is<int>() && host["ram_used_percent"].is<int>();
    } else {
        g_host_known = false;
        // values retained but g_host_known=false → render as "--"
    }

    return true;
}

// ── Render: platform ring (A10) ───────────────────────
// Dots in arc 150°→30°, colour from per-platform state (not index).
// Handles total 0, 1, ≤8, and >8 (overflow marker).
void draw_platform_ring() {
    int n = g_platforms_rendered;
    if (n == 0 && g_platforms_total == 0) return;

    bool blink_on = (millis() / 400) % 2 == 0;
    int ring_r = STATUS_RING_R;
    float start_angle = 150.0f;
    float end_angle   = 30.0f;

    if (n == 1) {
        float rad = 90.0f * PI / 180.0f;
        int x = CENTER_X + (int)(ring_r * cos(rad));
        int y = CENTER_Y - (int)(ring_r * sin(rad));
        uint16_t color;
        if (g_platforms[0].needs_attention) color = blink_on ? C_YELLOW : C_BG;
        else if (g_platforms[0].connected)  color = C_OK;
        else                                 color = C_DOWN;
        tft.fillCircle(x, y, STATUS_RING_DOT_R, color);
        return;
    }

    for (int i = 0; i < n; i++) {
        float angle;
        if (n > 1) angle = start_angle + (float)i / (n - 1) * (end_angle - start_angle);
        else       angle = 90.0f;
        float rad = angle * PI / 180.0f;
        int x = CENTER_X + (int)(ring_r * cos(rad));
        int y = CENTER_Y - (int)(ring_r * sin(rad));

        uint16_t color;
        if (g_platforms[i].needs_attention) color = blink_on ? C_YELLOW : C_BG;
        else if (g_platforms[i].connected)   color = C_OK;
        else                                  color = C_DOWN;
        tft.fillCircle(x, y, STATUS_RING_DOT_R, color);
    }

    if (g_platforms_total > MAX_PLATFORMS) {
        int overflow = g_platforms_total - MAX_PLATFORMS;
        tft.setFont(&fonts::Font0);
        tft.setTextColor(C_YELLOW, C_BG);
        tft.setTextDatum(MC_DATUM);
        tft.drawString("+" + String(overflow), CENTER_X + STATUS_OVERFLOW_DX, CENTER_Y - STATUS_OVERFLOW_DY);
    }
}

// ── Render: big center number ─────────────────────────
// #2: absent sessions renders "--", not 0
void draw_center() {
    tft.setTextColor(C_TEXT, C_BG);
    tft.setTextDatum(MC_DATUM);
    tft.setFont(&fonts::DejaVu40);
    if (active_sessions_known) {
        tft.drawNumber(active_sessions, CENTER_X, STATUS_NUM_Y);
    } else {
        tft.drawString("--", CENTER_X, STATUS_NUM_Y);
    }
    tft.setFont(&fonts::Font2);
    if (active_sessions_known) {
        tft.drawString(active_sessions == 1 ? "SESSION" : "SESSIONS", CENTER_X, STATUS_SESS_Y);
    } else {
        tft.drawString("", CENTER_X, STATUS_SESS_Y);
    }
}

// ── Render: gateway state line ────────────────────────
// #2/F1: absent overall → "UNKNOWN" (dim), never green RUNNING
// gateway_busy alone must NOT certify RUNNING
void draw_state() {
    tft.setFont(&fonts::Font2);
    tft.setTextDatum(MC_DATUM);
    if (!overall_known) {
        tft.setTextColor(C_DIM, C_BG);
        tft.drawString("UNKNOWN", CENTER_X, STATUS_STATE_Y);
    } else if (gateway_degraded) {
        tft.setTextColor(C_DEGRADED, C_BG);
        tft.drawString("DEGRADED", CENTER_X, STATUS_STATE_Y);
    } else if (gateway_busy && gateway_busy_known) {
        tft.setTextColor(C_YELLOW, C_BG);
        tft.drawString("BUSY", CENTER_X, STATUS_STATE_Y);
    } else {
        tft.setTextColor(C_OK, C_BG);
        tft.drawString("RUNNING", CENTER_X, STATUS_STATE_Y);
    }
}

// ── Render: disk bar (A11: moved up inside aperture) ──
// #2: absent disk → grey bar and "DISK --"
void draw_disk_bar() {
    int bar_w = STATUS_DISK_BAR_W;
    int bar_h = STATUS_DISK_BAR_H;
    int bar_x = CENTER_X - bar_w / 2;
    int bar_y = CENTER_Y + STATUS_DISK_BAR_DY;

    tft.drawRect(bar_x, bar_y, bar_w, bar_h, C_DIM);
    if (disk_known) {
        uint16_t bar_color = (disk_pct > 85) ? C_DOWN : (disk_pct > 70) ? C_DEGRADED : C_OK;
        int fill = (bar_w - 2) * disk_pct / 100;
        if (fill > 0) tft.fillRect(bar_x + 1, bar_y + 1, fill, bar_h - 2, bar_color);
    }

    tft.setFont(&fonts::Font0);
    tft.setTextColor(C_DIM, C_BG);
    tft.setTextDatum(MC_DATUM);
    if (disk_known) {
        tft.drawString("DISK " + String(disk_pct) + "%", CENTER_X, bar_y + STATUS_DISK_LBL_DY);
    } else {
        tft.drawString("DISK --", CENTER_X, bar_y + STATUS_DISK_LBL_DY);
    }
}

// ── Render: footer (A11: moved up, dot at fixed inset) ─
// #2: absent profiles → footer without bogus count
void draw_footer() {
    tft.setFont(&fonts::Font0);
    tft.setTextColor(C_DIM, C_BG);
    tft.setTextDatum(MC_DATUM);
    int footer_y = CENTER_Y + STATUS_FOOTER_DY;
    // Bound the footer text: version_str and profile_count are SERVER-DRIVEN, so an
    // unbounded string ("v0.100.100  100 bots") would run into the update dot while
    // the layout gate stayed green. Both fields are clamped so the footer's worst case
    // is exactly what tools/check_layout.py models ("v0.21.3 99+ bots", 16 chars).
    String ver_short = version_str;
    if (ver_short.length() > 6) ver_short = ver_short.substring(0, 6);
    String bots = profile_count;
    if (bots.length() > 3) bots = "99+";
    String footer;
    if (profile_known) {
        footer = "v" + ver_short + " " + bots + " bots";
    } else {
        footer = "v" + ver_short;
    }
    tft.drawString(footer, CENTER_X, footer_y);

    if (can_update) {
        tft.fillCircle(CENTER_X + STATUS_DOT_DX, footer_y, STATUS_DOT_R, C_YELLOW);
    }
}

// ── Render: tokens page (A7 + A8) ─────────────────────
// Each page reads ONLY its own PeriodUsage struct — no cross-period access.
void draw_tokens_page(bool is7d) {
    tft.fillScreen(C_BG);
    tft.setTextDatum(MC_DATUM);

    // A7: read from THIS period's struct only
    const PeriodUsage& u = is7d ? usage_7d : usage_24h;

    // Heading — Font0 at TK_HEAD_Y (was Font2@18 — exceeded R=80)
    tft.setTextColor(C_DIM, C_BG);
    tft.setFont(&fonts::Font0);
    tft.drawString(is7d ? "TOKENS 7D" : "TOKENS 24H", CENTER_X, TK_HEAD_Y);

    // A8: honest qualifier — cohort totals, not rolling windows
    tft.drawString("NEW SESSIONS ONLY", CENTER_X, TK_QUAL_Y);

    // Big total (A6: "--" for absent, "0" for real zero)
    // Stale shown via dim color, not "?" suffix (to fit R=80 at 7 chars DejaVu40)
    if (!u.valid) {
        tft.setTextColor(C_DIM, C_BG);
        tft.setFont(&fonts::DejaVu40);
        tft.drawString("--", CENTER_X, TK_BIG_Y);
    } else if (is_stale()) {
        tft.setTextColor(C_DIM, C_BG);  // dim = stale
        tft.setFont(&fonts::DejaVu40);
        tft.drawString(fmt_tokens(u.total), CENTER_X, TK_BIG_Y);
    } else {
        tft.setTextColor(C_TEXT, C_BG);
        tft.setFont(&fonts::DejaVu40);
        tft.drawString(fmt_tokens(u.total), CENTER_X, TK_BIG_Y);
    }

    if (is7d) {
        // Cost headline
        tft.setTextColor(C_TEXT, C_BG);
        tft.setFont(&fonts::Font2);
        String cost_s = u.valid ? ("$" + String(u.cost, 2)) : "--";
        if (u.valid && is_stale()) cost_s += "?";
        tft.drawString(cost_s, CENTER_X, TK_DET1_Y);

        // Calls / sessions
        tft.setTextColor(C_DIM, C_BG);
        tft.setFont(&fonts::Font0);
        String cs = u.valid
            ? ("CALLS " + fmt_tokens(u.calls) + "  SES " + String(u.sessions))
            : "CALLS --  SES --";
        tft.drawString(cs, CENTER_X, TK_DET2_Y);

        // In/Out split — A7 fix: reads usage_7d.input/output (was reading 24h)
        // Font0 at TK_DET3_Y (was Font0@136 — exceeded R=80)
        String io = u.valid
            ? ("IN " + fmt_tokens(u.input) + "  OUT " + fmt_tokens(u.output))
            : "IN --  OUT --";
        if (u.valid && is_stale()) io += "?";
        tft.drawString(io, CENTER_X, TK_DET3_Y);
    } else {
        // In/Out split — Font0 at TK_DET1_Y. Font2 here is 160px wide and cannot
        // fit inside R=80 at any y (checked by tools/check_layout.py).
        tft.setTextColor(C_TEXT, C_BG);
        tft.setFont(&fonts::Font0);
        String io = u.valid
            ? ("IN " + fmt_tokens(u.input) + "  OUT " + fmt_tokens(u.output))
            : "IN --  OUT --";
        if (u.valid && is_stale()) io += "?";
        tft.drawString(io, CENTER_X, TK_DET1_Y);

        // Cache + cost — Font0 at TK_DET2_Y
        tft.setTextColor(C_DIM, C_BG);
        tft.setFont(&fonts::Font0);
        String cc = u.valid
            ? ("CACHE " + fmt_tokens(u.cache) + "  $" + String(u.cost, 2))
            : "CACHE --  $--";
        tft.drawString(cc, CENTER_X, TK_DET2_Y);

        // Calls / sessions — Font0 at TK_DET3_Y
        String cs = u.valid
            ? ("CALLS " + fmt_tokens(u.calls) + "  SES " + String(u.sessions))
            : "CALLS --  SES --";
        tft.drawString(cs, CENTER_X, TK_DET3_Y);
    }
}

// ── Render: health page (A6 tri-state) ────────────────
void draw_health_page() {
    tft.fillScreen(C_BG);
    tft.setTextDatum(MC_DATUM);

    // Heading
    tft.setTextColor(C_DIM, C_BG);
    tft.setFont(&fonts::Font2);
    tft.drawString("HEALTH", CENTER_X, HL_HEAD_Y);

    // Component rows — tri-state: OK (green) / DOWN (red) / UNKNOWN (grey)
    const int rows_y[4]     = {HL_ROW0_Y, HL_ROW1_Y, HL_ROW2_Y, HL_ROW3_Y};
    const char* rows_txt[4] = {"GATEWAY", "STORAGE", "DASHBOARD", "PLATFORMS"};
    CompState rows_state[4] = {comp_gw, comp_storage, comp_dash, comp_platforms};

    tft.setFont(&fonts::Font2);
    for (int i = 0; i < 4; i++) {
        tft.setTextColor(C_TEXT, C_BG);
        tft.drawString(rows_txt[i], CENTER_X, rows_y[i]);
        uint16_t dot_color;
        switch (rows_state[i]) {
            case COMP_OK:      dot_color = C_OK;      break;
            case COMP_DOWN:    dot_color = C_DOWN;    break;
            default:           dot_color = C_DIM;     break;  // UNKNOWN → grey
        }
        tft.fillCircle(CENTER_X - HL_DOT_DX, rows_y[i], HL_DOT_R, dot_color);
    }

    // Dashboard error count (orange suffix, only when > 0)
    if (dash_errors > 0) {
        tft.setFont(&fonts::Font0);
        tft.setTextColor(C_DEGRADED, C_BG);
        tft.drawString(String(dash_errors) + "E", CENTER_X + HL_ERR_DX, HL_ERR_Y);
    }

    // Auth state — tri-state (UNKNOWN / EXPIRED / OK)
    tft.setFont(&fonts::Font2);
    const char* auth_txt;
    uint16_t auth_color;
    switch (g_session_state) {
        case SESS_OK:      auth_txt = "AUTH OK";      auth_color = C_OK;   break;
        case SESS_EXPIRED: auth_txt = "AUTH EXPIRED";  auth_color = C_DOWN; break;
        default:           auth_txt = "AUTH ?";        auth_color = C_DIM;  break;
    }
    tft.setTextColor(auth_color, C_BG);
    tft.drawString(auth_txt, CENTER_X, HL_AUTH_Y);

    // Host CPU/RAM — A6: "--" when absent (not silently carried over)
    // Font0 at HL_HOST_Y (was y=150 — exceeded R=80)
    tft.setFont(&fonts::Font0);
    tft.setTextColor(C_DIM, C_BG);
    String host_str;
    if (!g_host_known) {
        host_str = "CPU -- RAM --";
    } else {
        // single space between the metrics: with the stale "?" suffix the
        // 19-char form measured r=81.3 > R=80 (tools/check_layout.py)
        host_str = "CPU " + String(host_cpu) + "% RAM " + String(host_ram) + "%";
        if (is_stale()) host_str += "?";
    }
    tft.drawString(host_str, CENTER_X, HL_HOST_Y);
}

// ── Render: full screen (rotates 4 pages) ─────────────
void render() {
    int page = (millis() / PAGE_MS) % PAGE_COUNT;
    switch (page) {
        case 0: { // STATUS
            tft.fillScreen(C_BG);
            draw_platform_ring();
            draw_center();
            draw_state();
            draw_disk_bar();
            draw_footer();
            break;
        }
        case 1: draw_tokens_page(false); break;  // TOKENS 24H
        case 2: draw_tokens_page(true);  break;  // TOKENS 7D
        case 3: draw_health_page();      break;  // HEALTH
    }
}

// ── Error screen ──────────────────────────────────────
void show_error() {
    tft.fillScreen(C_BG);
    tft.setTextColor(C_DOWN, C_BG);
    tft.setTextDatum(MC_DATUM);
    tft.setFont(&fonts::Font2);
    tft.drawString("OFFLINE", CENTER_X, CENTER_Y);
}

// ── SETUP ─────────────────────────────────────────────
void setup() {
    Serial.begin(115200);
    delay(300);
    Serial.println("[dash] boot — LovyanGFX + FastLED");

    led_init();
    set_led(0, 120, 0); // boot: dim green

    tft.init();
    tft.setRotation(1);   // LANDSCAPE: 320x172, 90° CW (USB port to the right; use 3 to flip)
    tft.setBrightness(128); // 50% max per Waveshare warning
    tft.fillScreen(C_BG);

    // Boot screen
    tft.setTextColor(C_ACCENT, C_BG);
    tft.setTextDatum(MC_DATUM);
    tft.setFont(&fonts::DejaVu24);
    tft.drawString("HERMES", CENTER_X, CENTER_Y - 40);
    tft.setTextColor(C_DIM, C_BG);
    tft.setFont(&fonts::Font2);
    tft.drawString("Dashboard", CENTER_X, CENTER_Y + 10);
    delay(1000);

    wifi_connect();
    Serial.printf("[dash] WiFi: %s, IP %s\n", WIFI_SSID, WiFi.localIP().toString().c_str());

    // Initial fetch
    fetch_ok = fetch_dashboard();
    if (fetch_ok) {
        last_success = millis();  // A12: set AFTER success
        Serial.printf("[dash] fetch OK: %d sessions, %d/%d platforms, schema=%d\n",
                      active_sessions, g_platforms_rendered, g_platforms_total, g_schema);
        render();
    } else {
        Serial.println("[dash] initial fetch FAILED");
        show_error();
    }
}

// ── LOOP (A12: decoupled UI timing from fetch) ───────
void loop() {
    unsigned long now = millis();

    // A12: page rotation driven by millis() — independent of fetch
    static int current_page = -1;
    int new_page = (now / PAGE_MS) % PAGE_COUNT;
    bool page_changed = (new_page != current_page);

    // A12: attention blink state change triggers redraw
    static bool blink_was_on = false;
    bool blink_now_on = (now / 400) % 2 == 0;
    bool blink_changed = (blink_now_on != blink_was_on);

    // #5: blink redraw only on STATUS page (ring is the only thing that blinks)
    bool should_redraw = page_changed;
    if (!page_changed && blink_changed && fetch_ok && current_page == 0) {
        should_redraw = true;
    }

    if (should_redraw) {
        current_page = new_page;
        blink_was_on = blink_now_on;
        if (fetch_ok) render();
    }

    // Fetch on its own schedule (10s)
    if (now - last_fetch >= FETCH_INTERVAL) {
        last_fetch = now;  // unsigned subtraction is rollover-safe

        if (WiFi.status() != WL_CONNECTED) {
            wifi_connect();
        }

        fetch_ok = fetch_dashboard();
        if (fetch_ok) {
            last_success = millis();  // #1 A12: stamp AFTER success, not before
            render();
        } else {
            show_error();
        }
    }

    led_update();
    delay(50);
}

// ── WiFi ──────────────────────────────────────────────
void wifi_connect() {
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    Serial.printf("[dash] connecting to %s...\n", WIFI_SSID);
    tft.fillScreen(C_BG);
    tft.setTextColor(C_TEXT, C_BG);
    tft.setTextDatum(MC_DATUM);
    tft.setFont(&fonts::Font2);
    tft.drawString("Connecting", CENTER_X, CENTER_Y - 20);
    tft.drawString("WiFi...", CENTER_X, CENTER_Y + 10);

    int dots = 0;
    unsigned long t0 = millis();
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        dots++;
        if (dots % 20 == 0) { // every 10s
            Serial.printf("[dash] ...still connecting (%lus), status=%d\n", (millis() - t0) / 1000, WiFi.status());
        }
        tft.drawChar('.', CENTER_X - 12 + (dots % 3) * 12, CENTER_Y + 40);
        if (millis() - t0 > 45000) break; // hidden SSID + slow router: cap at 45s
    }
    tft.fillScreen(C_BG);
    Serial.printf("[dash] WiFi status=%d, IP %s\n", WiFi.status(), WiFi.localIP().toString().c_str());
}
