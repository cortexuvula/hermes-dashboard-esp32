#!/usr/bin/env python3
"""Hermes Dashboard Relay — runs on omarchy-home.

Fetches the Mac's Hermes dashboard API via Tailscale (100.79.10.43:9119)
and re-serves it on localhost:9120 for the ESP32 on the home LAN. Also
merges token-usage totals from the Mac's usage-server (:9121) so the
widget can show a tokens page without any auth.

Usage:
    python3 relay.py [--listen-port 9120] [--upstream http://100.x.x.x:9119/api/status] [--usage http://100.x.x.x:9121/]

Systemd unit (optional):
    [Unit]
    Description=Hermes Dashboard Relay
    After=network-online.target

    [Service]
    ExecStart=/usr/bin/python3 /opt/hermes-dashboard-relay/relay.py
    Restart=always
    RestartSec=30

    [Install]
    WantedBy=multi-user.target
"""

import http.server
import urllib.request
import argparse
import json

UPSTREAM = "http://100.79.10.43:9119/api/status"  # Mac's Tailscale IP
USAGE_URL = "http://100.79.10.43:9121/"            # Mac's token-usage micro-service
LISTEN_PORT = 9120
TIMEOUT = 8
USAGE_TIMEOUT = 3


def _fetch(url: str, timeout: int):
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
        return resp.read()


class RelayHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status" or self.path == "/":
            try:
                data = json.loads(_fetch(UPSTREAM, TIMEOUT))
                # Merge token usage (best-effort — if the usage server is down,
                # serve the status unchanged).
                try:
                    usage = json.loads(_fetch(USAGE_URL, USAGE_TIMEOUT))
                    data["tokens_24h"] = usage.get("h24", {})
                    data["tokens_7d"] = usage.get("d7", {})
                    data["host"] = usage.get("host", {})
                except Exception:
                    pass
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Log each request so we can confirm the ESP32 is polling
        print(f"[relay] {self.client_address[0]} {self.command} {self.path}", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--listen-port", type=int, default=LISTEN_PORT)
    p.add_argument("--upstream", default=UPSTREAM)
    p.add_argument("--usage", default=USAGE_URL)
    args = p.parse_args()
    UPSTREAM = args.upstream
    USAGE_URL = args.usage

    print(f"Relay: {UPSTREAM} → :{args.listen_port} (usage: {USAGE_URL})")
    # Bind all interfaces — ESP32 on the same LAN must reach this relay
    httpd = http.server.HTTPServer(("0.0.0.0", args.listen_port), RelayHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
