// Hermes Dashboard Monitor — ESP32-C6 1.47" LCD (LANDSCAPE)
// Displays live Hermes gateway stats from the local relay (:9120, merges
// tokens + host stats from the Mac usage-server).
//
// Board: Waveshare ESP32-C6-LCD-1.47
// Display: ST7789V3, 172×320 (round), SPI — MOSI=6 SCLK=7 CS=14 DC=15 RST=21 BL=22
// Chip: ESP32-C6 (RISC-V, 160MHz, 4MB Flash)
// Libs: LovyanGFX (TFT_eSPI does not support C6 — VSPI registers don't exist on RISC-V)
//       FastLED (WS2812 RGB on GPIO8)

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
#define ROUND_R   80   // usable radius inside round glass

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

// Forward decls — globals are defined below (single-TU compile)
extern bool fetch_ok;
extern bool gateway_degraded;
extern bool gateway_busy;
extern unsigned long last_fetch;

void set_led(uint8_t r, uint8_t g, uint8_t b) {
    CRGB c(r, g, b);
    if (leds[0] != c) { leds[0] = c; FastLED.show(); }
}

void led_init() {
    FastLED.addLeds<WS2812, LED_PIN, GRB>(leds, NUM_LEDS);
    FastLED.setBrightness(96);
    set_led(0, 0, 0);
}

// Green flash on fetch, orange when busy/degraded, red when offline.
void led_update() {
    if (!fetch_ok)                 { set_led(255, 0, 0); }     // offline
    else if (gateway_degraded || gateway_busy) { set_led(255, 120, 0); } // busy/degraded
    else if (millis() - last_fetch < 250)      { set_led(0, 200, 0); }   // fresh fetch
    else                           { set_led(0, 0, 0); }
}

// ── Globals ───────────────────────────────────────────
unsigned long last_fetch = 0;
const unsigned long FETCH_INTERVAL = 10000; // 10s poll
bool fetch_ok = false;

int active_sessions = 0;
int active_agents = 0;
bool gateway_busy = false;
bool gateway_degraded = false;
int platforms_up = 0;
int platforms_total = 0;
int attention_idx[8];
int attention_count = 0;
int disk_pct = 0;
String version_str = "";
String profile_count = "";

bool can_update = false;
bool session_valid = true;

// Components health (gateway/dashboard/storage/platforms from /api/status)
bool comp_gw_ok = true, comp_dash_ok = true, comp_storage_ok = true, comp_platforms_ok = true;
int dash_errors = 0;

// ── Token usage (merged by relay from Mac usage-server) ──
long long tokens_total_24h = 0;
long long tokens_in_24h = 0;
long long tokens_out_24h = 0;
long long tokens_cache_24h = 0;
double tokens_cost_24h = 0.0;
long long tokens_calls_24h = 0;
long long tokens_sess_24h = 0;

long long tokens_total_7d = 0;
double tokens_cost_7d = 0.0;
long long tokens_calls_7d = 0;
long long tokens_sess_7d = 0;

// Host stats (Mac running the gateway)
int host_cpu = 0;
int host_ram = 0;

const unsigned long PAGE_MS = 12000; // rotate pages every 12s
const int PAGE_COUNT = 4;            // STATUS / TOKENS 24H / TOKENS 7D / HEALTH

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

// ── JSON fetch ────────────────────────────────────────
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

    String payload = http.getString();
    http.end();

    // Parse JSON (ArduinoJson v7 — JsonDocument grows as needed)
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, payload);
    if (err) return false;

    active_sessions = doc["active_sessions"] | 0;
    active_agents = doc["active_agents"] | 0;
    gateway_busy = doc["gateway_busy"] | false;
    version_str = doc["version"] | "?";
    const char* overall = doc["overall"];
    gateway_degraded = (overall && strcmp(overall, "degraded") == 0);

    // Count platforms + flag attention platforms
    platforms_up = 0;
    platforms_total = 0;
    attention_count = 0;
    JsonObject platforms = doc["gateway_platforms"].as<JsonObject>();
    int pi = 0;
    for (JsonPair kv : platforms) {
        platforms_total++;
        const char* state = kv.value()["state"];
        if (state && strcmp(state, "connected") == 0) platforms_up++;
        if (kv.value()["needs_attention"] | false) {
            if (attention_count < 8) attention_idx[attention_count++] = pi;
        }
        pi++;
    }

    // Disk (use | 0.0 — | 0 falls back on float values in ArduinoJson)
    JsonObject disk = doc["disk"];
    disk_pct = (int)(disk["used_percent"] | 0.0);

    // Profile count
    JsonArray profiles = doc["profiles"].as<JsonArray>();
    profile_count = String(profiles.size());

    // Update + session health
    can_update = doc["can_update_hermes"] | false;
    const char* nsv = doc["nous_session_valid"];
    session_valid = (nsv && strcmp(nsv, "valid") == 0);

    // Component health
    JsonObject comp = doc["components"];
    if (comp.isNull()) {
        comp_gw_ok = comp_dash_ok = comp_storage_ok = comp_platforms_ok = true;
        dash_errors = 0;
    } else {
        comp_gw_ok        = (strcmp(comp["gateway"]["status"]   | "?", "ok") == 0);
        comp_dash_ok      = (strcmp(comp["dashboard"]["status"] | "?", "ok") == 0);
        dash_errors       = comp["dashboard"]["recent_unhandled_errors"] | 0;
        comp_storage_ok   = (strcmp(comp["storage"]["status"]   | "?", "ok") == 0);
        comp_platforms_ok = (strcmp(comp["platforms"]["status"] | "?", "ok") == 0);
    }

    // Token usage (best-effort — absent when the usage server is down)
    JsonObject tk = doc["tokens_24h"];
    if (!tk.isNull()) {
        tokens_total_24h = tk["total"] | 0LL;
        tokens_in_24h    = tk["input"]  | 0LL;
        tokens_out_24h   = tk["output"] | 0LL;
        tokens_cache_24h = tk["cache"]  | 0LL;
        tokens_cost_24h  = tk["est_cost"] | 0.0;
        tokens_calls_24h = tk["api_calls"] | 0LL;
        tokens_sess_24h  = tk["sessions"] | 0LL;
    } else {
        tokens_total_24h = tokens_in_24h = tokens_out_24h = tokens_cache_24h = 0;
        tokens_cost_24h = 0.0;
        tokens_calls_24h = tokens_sess_24h = 0;
    }

    JsonObject tk7 = doc["tokens_7d"];
    if (!tk7.isNull()) {
        tokens_total_7d = tk7["total"] | 0LL;
        tokens_cost_7d  = tk7["est_cost"] | 0.0;
        tokens_calls_7d = tk7["api_calls"] | 0LL;
        tokens_sess_7d  = tk7["sessions"] | 0LL;
    } else {
        tokens_total_7d = 0;
        tokens_cost_7d = 0.0;
        tokens_calls_7d = tokens_sess_7d = 0;
    }

    // Host stats (best-effort)
    JsonObject host = doc["host"];
    if (!host.isNull()) {
        host_cpu = host["cpu_percent"] | 0;
        host_ram = host["ram_used_percent"] | 0;
    }

    return true;
}

// ── Render: platform status ring (attention platforms blink) ──
void draw_platform_ring(int up, int total) {
    if (total < 2) return;
    bool blink_on = (millis() / 400) % 2 == 0;
    // Dots in an arc across the TOP of the round display (angles 150°→30°)
    int radius = ROUND_R - 20;
    int start_angle = 150; // lower-left
    int end_angle = 30;    // lower-right (sweeps 120° over the top)

    for (int i = 0; i < total; i++) {
        float angle = start_angle + (float)i / (total - 1) * (end_angle - start_angle);
        float rad = angle * PI / 180.0;
        int x = CENTER_X + radius * cos(rad);
        int y = CENTER_Y - radius * sin(rad);   // sin>0 → above center → top arc

        bool attn = false;
        for (int a = 0; a < attention_count; a++) if (attention_idx[a] == i) attn = true;

        uint16_t color;
        if (attn)      color = blink_on ? C_YELLOW : C_BG;   // blink while retrying
        else if (i < up) color = C_OK;
        else             color = C_DOWN;
        tft.fillCircle(x, y, 4, color);
    }
}

// ── Render: big center number ─────────────────────────
void draw_center(int sessions) {
    tft.setTextColor(C_TEXT, C_BG);
    tft.setTextDatum(MC_DATUM);

    // Big number
    tft.setFont(&fonts::DejaVu40);
    tft.drawNumber(sessions, CENTER_X, CENTER_Y - 12);

    // Label below
    tft.setFont(&fonts::Font2);
    tft.drawString(sessions == 1 ? "SESSION" : "SESSIONS", CENTER_X, CENTER_Y + 22);
}

// ── Render: gateway state line ────────────────────────
void draw_state(bool busy, bool degraded) {
    tft.setFont(&fonts::Font2);
    if (degraded) {
        tft.setTextColor(C_DEGRADED, C_BG);
        tft.drawString("DEGRADED", CENTER_X, CENTER_Y + 40);
    } else if (busy) {
        tft.setTextColor(C_YELLOW, C_BG);
        tft.drawString("BUSY", CENTER_X, CENTER_Y + 40);
    } else {
        tft.setTextColor(C_OK, C_BG);
        tft.drawString("RUNNING", CENTER_X, CENTER_Y + 40);
    }
}

// ── Render: disk bar at bottom ────────────────────────
void draw_disk_bar(int pct) {
    int bar_w = ROUND_R * 2 - 20;
    int bar_h = 6;
    int bar_x = CENTER_X - bar_w / 2;
    int bar_y = CENTER_Y + ROUND_R - 26;   // 140 in landscape

    tft.drawRect(bar_x, bar_y, bar_w, bar_h, C_DIM);

    uint16_t bar_color = (pct > 85) ? C_DOWN : (pct > 70) ? C_DEGRADED : C_OK;
    int fill = (bar_w - 2) * pct / 100;
    if (fill > 0) tft.fillRect(bar_x + 1, bar_y + 1, fill, bar_h - 2, bar_color);

    tft.setFont(&fonts::Font0);
    tft.setTextColor(C_DIM, C_BG);
    tft.drawString("DISK " + String(pct) + "%", CENTER_X, bar_y + 10);
}

// ── Render: footer — version + profiles + update dot ──
void draw_footer(const String& ver, const String& pro) {
    tft.setFont(&fonts::Font0);
    tft.setTextColor(C_DIM, C_BG);
    String footer = "v" + ver + "  " + pro + " bots";
    tft.drawString(footer, CENTER_X, CENTER_Y + ROUND_R - 4);   // 162

    if (can_update) {
        int w = tft.textWidth(footer);
        tft.fillCircle(CENTER_X + w / 2 + 10, CENTER_Y + ROUND_R - 4, 3, C_YELLOW);
    }
}

// ── Token formatting (e.g. 79599877 → "79.6M") ────────
String fmt_tokens(long long v) {
    if (v >= 100000000LL) { char b[16]; snprintf(b, sizeof b, "%.0fM", v / 1000000.0); return b; }
    if (v >= 1000000LL)   { char b[16]; snprintf(b, sizeof b, "%.1fM", v / 1000000.0); return b; }
    if (v >= 1000LL)      { char b[16]; snprintf(b, sizeof b, "%.0fk", v / 1000.0); return b; }
    return String((long)v);
}

// ── Render: tokens page (24h or 7d) ───────────────────
void draw_tokens_page(bool is7d) {
    tft.fillScreen(C_BG);
    tft.setTextDatum(MC_DATUM);

    long long total = is7d ? tokens_total_7d : tokens_total_24h;
    double cost    = is7d ? tokens_cost_7d  : tokens_cost_24h;
    long long calls = is7d ? tokens_calls_7d : tokens_calls_24h;
    long long sess  = is7d ? tokens_sess_7d  : tokens_sess_24h;

    // Heading
    tft.setTextColor(C_DIM, C_BG);
    tft.setFont(&fonts::Font2);
    tft.drawString(is7d ? "TOKENS 7D" : "TOKENS 24H", CENTER_X, 18);

    // Big total
    tft.setTextColor(C_TEXT, C_BG);
    tft.setFont(&fonts::DejaVu40);
    tft.drawString(total > 0 ? fmt_tokens(total) : "--", CENTER_X, 52);

    if (is7d) {
        // Cost headline
        tft.setTextColor(C_TEXT, C_BG);
        tft.setFont(&fonts::Font2);
        tft.drawString("$" + String(cost, 2), CENTER_X, 90);
        // Calls / sessions
        tft.setTextColor(C_DIM, C_BG);
        tft.setFont(&fonts::Font0);
        tft.drawString("CALLS " + fmt_tokens(calls) + "  SES " + String(sess), CENTER_X, 114);
        // In/Out split
        tft.drawString("IN " + fmt_tokens(tokens_in_24h) + "  OUT " + fmt_tokens(tokens_out_24h), CENTER_X, 140);
    } else {
        // In/Out split
        tft.setTextColor(C_TEXT, C_BG);
        tft.setFont(&fonts::Font2);
        String io = "IN " + fmt_tokens(tokens_in_24h) + "  OUT " + fmt_tokens(tokens_out_24h);
        tft.drawString(io, CENTER_X, 90);
        // Cache + cost
        tft.setTextColor(C_DIM, C_BG);
        tft.setFont(&fonts::Font0);
        tft.drawString("CACHE " + fmt_tokens(tokens_cache_24h) + "  $" + String(cost, 2), CENTER_X, 114);
        // Calls / sessions today
        tft.drawString("CALLS " + fmt_tokens(calls) + "  SES " + String(sess), CENTER_X, 140);
    }
}

// ── Render: health page ───────────────────────────────
void draw_health_page() {
    tft.fillScreen(C_BG);
    tft.setTextDatum(MC_DATUM);

    // Heading
    tft.setTextColor(C_DIM, C_BG);
    tft.setFont(&fonts::Font2);
    tft.drawString("HEALTH", CENTER_X, 26);

    // Full-word component rows — single centered column, dots pulled well
    // inside the round glass (old two-column layout clipped at the edge).
    const int rows_y[4]   = {48, 70, 92, 114};
    const char* rows_txt[4] = {"GATEWAY", "STORAGE", "DASHBOARD", "PLATFORMS"};
    bool rows_ok[4] = {comp_gw_ok, comp_storage_ok, comp_dash_ok, comp_platforms_ok};

    tft.setFont(&fonts::Font2);
    for (int i = 0; i < 4; i++) {
        tft.setTextColor(C_TEXT, C_BG);
        tft.drawString(rows_txt[i], CENTER_X, rows_y[i]);
        tft.fillCircle(CENTER_X - 56, rows_y[i], 3, rows_ok[i] ? C_OK : C_DOWN);
    }
    // Dashboard error count (orange suffix, only when > 0)
    if (dash_errors > 0) {
        tft.setFont(&fonts::Font0);
        tft.setTextColor(C_DEGRADED, C_BG);
        tft.drawString(String(dash_errors) + "E", CENTER_X + 55, 92);
    }

    // Auth state (Nous session) + host CPU/RAM
    tft.setFont(&fonts::Font2);
    tft.setTextColor(session_valid ? C_OK : C_DOWN, C_BG);
    tft.drawString(session_valid ? "AUTH OK" : "AUTH EXPIRED", CENTER_X, 136);

    tft.setFont(&fonts::Font0);
    tft.setTextColor(C_DIM, C_BG);
    tft.drawString("CPU " + String(host_cpu) + "%  RAM " + String(host_ram) + "%", CENTER_X, 148);
}

// ── Render: full screen (rotates 4 pages) ─────────────
void render() {
    int page = (millis() / PAGE_MS) % PAGE_COUNT;
    switch (page) {
        case 0: { // STATUS
            tft.fillScreen(C_BG);
            draw_platform_ring(platforms_up, platforms_total);
            draw_center(active_sessions);
            draw_state(gateway_busy, gateway_degraded);
            draw_disk_bar(disk_pct);
            draw_footer(version_str, profile_count);
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
        Serial.printf("[dash] fetch OK: %d sessions, %d/%d platforms\n", active_sessions, platforms_up, platforms_total);
        render();
    } else {
        Serial.println("[dash] initial fetch FAILED");
        show_error();
    }
}

// ── LOOP ──────────────────────────────────────────────
void loop() {
    unsigned long now = millis();
    if (now - last_fetch >= FETCH_INTERVAL) {
        last_fetch = now;

        if (WiFi.status() != WL_CONNECTED) {
            wifi_connect();
        }

        fetch_ok = fetch_dashboard();
        if (fetch_ok) {
            render();
        } else {
            show_error();
        }
    }
    led_update();
    delay(50);
}
