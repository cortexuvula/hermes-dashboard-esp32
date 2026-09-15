#!/usr/bin/env python3
"""Hermes Dashboard Relay — runs on the relay host (e.g. omarchy-home).

Fetches the Hermes dashboard /api/status upstream (with a hard ≤3 s budget
and a 128 KB read cap) and re-serves an ALLOWLISTED compact object on
:9120 for the ESP32 on the LAN. Token-usage totals and host stats come
from usage-server (:9121) via a background refresher thread — a stalled
usage upstream can NEVER block or delay a response (audit A5). The
worst-case response time is bounded by the status timeout alone (<4 s).

Contract (schema 2) — see the CONTRACT section in ../README.md:
  - Only the keys the board parses are emitted; nothing passes through.
  - Unavailable usage data is JSON null, never omitted, never zeroed
    (tokens_24h / tokens_7d / host).
  - usage_age_s  int|null  seconds since the usage snapshot was taken
    (computed relay-side; the board has no clock).
  - generated_at int|null  epoch seconds, forwarded from usage-server.
  - schema       int       contract version (2).

Usage:
    python3 relay.py [--listen-port 9120] [--bind 0.0.0.0]
                     [--upstream http://HOST:9119/api/status]
                     [--usage http://HOST:9121/]

Binds 0.0.0.0 by default (the board polls it over the LAN — allow inbound
TCP 9120 from the board's subnet in the firewall). Use --bind 127.0.0.1 to
restrict to localhost. CORS is OFF unless --cors is passed (only if a
browser consumer needs it).
"""

import argparse
import http.server
import json
import socket
import sys
import threading
import time
import urllib.request

UPSTREAM = "http://100.79.10.43:9119/api/status"  # Mac's Tailscale IP
USAGE_URL = "http://100.79.10.43:9121/"            # usage-server on same host
LISTEN_PORT = 9120
STATUS_TIMEOUT = 3.0      # hard budget; worst-case response stays < 4 s (A5)
USAGE_TIMEOUT = 2.0       # background refresher only, never on request path
USAGE_REFRESH = 5.0       # background refresh interval (board polls ~10 s)
STATUS_CAP = 128 * 1024   # hard byte cap on the upstream status body (A9)
USAGE_CAP = 32 * 1024     # hard byte cap on the usage body (A9)
MAX_CONCURRENCY = 8       # over-limit requests are logged + rejected (A4)
CONN_TIMEOUT = 5.0        # per-connection read/write socket timeout (A4)

SCHEMA = 2

# Top-level keys the board parses (hermes-dash-esp32.ino parsePayload()).
# Anything upstream not in this set is DROPPED (A3: never pass through).
STATUS_ALLOWLIST = (
    "active_sessions", "active_agents", "gateway_busy", "version", "overall",
    "gateway_platforms", "disk", "profiles", "can_update_hermes",
    "nous_session_valid", "components",
)
# Fields of each tokens_* object the board reads (input/output added for 7d
# in schema 2 — keep every field the board needs).
TOKEN_FIELDS = ("total", "input", "output", "cache", "reasoning",
                "api_calls", "sessions", "est_cost")
HOST_FIELDS = ("cpu_percent", "load_percent", "ram_used_percent", "ram_total_mb")


class UpstreamError(Exception):
    pass


def _fetch_capped(url: str, timeout: float, cap: int) -> bytes:
    """GET url, enforcing a hard byte cap even when length is unknown or
    chunked (A9). Raises UpstreamError on transport failure or over-cap."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            chunks, total = [], 0
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    raise UpstreamError(
                        f"upstream body exceeds {cap} byte cap: {url}")
                chunks.append(chunk)
            return b"".join(chunks)
    except UpstreamError:
        raise
    except Exception as e:  # timeout, DNS, HTTP error, ...
        raise UpstreamError(str(e)) from e


def _pick(obj, fields):
    """Allowlist a flat dict; missing/unavailable → None, never zeroed."""
    if not isinstance(obj, dict):
        return None
    return {f: obj.get(f) for f in fields}


class UsageRefresher(threading.Thread):
    """Background thread that keeps the newest usage snapshot (A5/A6).
    Request handling NEVER waits on this — it reads the last snapshot."""

    def __init__(self, url: str, timeout: float, interval: float):
        super().__init__(daemon=True, name="usage-refresher")
        self.url = url
        self.timeout = timeout
        self.interval = interval
        self._lock = threading.Lock()
        self._snapshot = None   # parsed usage payload dict, or None
        self._taken_at = None   # time.time() when snapshot was fetched

    def run(self):
        while True:
            try:
                raw = _fetch_capped(self.url, self.timeout, USAGE_CAP)
                snap = json.loads(raw)
                with self._lock:
                    self._snapshot = snap
                    self._taken_at = time.time()
            except Exception as e:
                # Keep serving the last snapshot; usage_age_s tells the
                # board how stale it is. Failures are logged, not swallowed
                # silently (A6).
                print(f"[relay] usage refresh failed: {e}", flush=True)
            time.sleep(self.interval)

    def snapshot(self):
        """Return (payload|None, taken_at|None)."""
        with self._lock:
            return self._snapshot, self._taken_at


def build_payload(status: dict, refresher: UsageRefresher) -> dict:
    """Assemble the schema-2 response: allowlisted status keys + usage."""
    payload = {k: status.get(k) for k in STATUS_ALLOWLIST}  # missing → null

    usage, taken_at = refresher.snapshot() if refresher else (None, None)
    ga = usage.get("generated_at") if isinstance(usage, dict) else None
    payload["tokens_24h"] = _pick(usage.get("h24"), TOKEN_FIELDS) \
        if isinstance(usage, dict) else None
    payload["tokens_7d"] = _pick(usage.get("d7"), TOKEN_FIELDS) \
        if isinstance(usage, dict) else None
    payload["host"] = _pick(usage.get("host"), HOST_FIELDS) \
        if isinstance(usage, dict) else None
    payload["usage_age_s"] = int(time.time() - taken_at) if taken_at else None
    payload["generated_at"] = ga if isinstance(ga, (int, float)) else None
    payload["schema"] = SCHEMA
    return payload


DESCRIPTION = ("Hermes Dashboard Relay — allowlisted, time-bounded re-server "
               "of the Hermes dashboard status for the ESP32 widget.")


class RelayServer(http.server.ThreadingHTTPServer):
    """Threading server carrying its config and concurrency slots."""

    daemon_threads = True

    def __init__(self, addr, cfg: "RelayConfig",
                 max_concurrency: int = MAX_CONCURRENCY):
        super().__init__(addr, RelayHandler)
        self.cfg = cfg
        self.slots = threading.BoundedSemaphore(max_concurrency)


class RelayHandler(http.server.BaseHTTPRequestHandler):
    # Per-connection socket timeout (A4): a client withholding bytes or a
    # reader that never drains cannot hold a slot indefinitely.
    timeout = CONN_TIMEOUT

    def do_GET(self):
        if self.path not in ("/api/status", "/"):
            self.send_response(404)
            self.end_headers()
            return
        # Bounded concurrency (A4): reject over-limit, never queue.
        if not self.server.slots.acquire(blocking=False):
            print(f"[relay] 503 over-limit {self.client_address[0]} "
                  f"{self.command} {self.path}", flush=True)
            self._send(503, {"error": "relay busy, try again"})
            return
        try:
            try:
                raw = _fetch_capped(self.server.cfg.upstream,
                                    self.server.cfg.status_timeout,
                                    STATUS_CAP)
                status = json.loads(raw)
                if not isinstance(status, dict):
                    raise UpstreamError("upstream status is not a JSON object")
                self._send(200, build_payload(status,
                                              self.server.cfg.refresher))
            except Exception as e:
                # Status fetch failed → 502, board renders OFFLINE (A5).
                self._send(502, {"error": str(e)})
        finally:
            self.server.slots.release()

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if self.server.cfg.cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_one_request(self):
        # A timed-out/stalled connection must not traceback noisily.
        try:
            super().handle_one_request()
        except (socket.timeout, TimeoutError, ConnectionError):
            self.close_connection = True

    def log_message(self, format, *args):
        # Log each request so we can confirm the ESP32 is polling
        print(f"[relay] {self.client_address[0]} {self.command} {self.path}",
              flush=True)


class RelayConfig:
    def __init__(self, upstream, usage_url, status_timeout=STATUS_TIMEOUT,
                 cors=False):
        self.upstream = upstream
        self.cors = cors
        self.status_timeout = status_timeout
        self.refresher = UsageRefresher(usage_url, USAGE_TIMEOUT,
                                        USAGE_REFRESH)


def make_server(bind: str, port: int, cfg: RelayConfig,
                max_concurrency: int = MAX_CONCURRENCY):
    """Bind the server, start the usage refresher, return the httpd."""
    httpd = RelayServer((bind, port), cfg, max_concurrency)
    cfg.refresher.start()
    return httpd


def main(argv=None):
    p = argparse.ArgumentParser(description=DESCRIPTION)
    p.add_argument("--listen-port", type=int, default=LISTEN_PORT)
    p.add_argument("--bind", default="0.0.0.0",
                   help="interface to bind (default 0.0.0.0 — the board "
                        "polls over the LAN; use 127.0.0.1 to restrict)")
    p.add_argument("--upstream", default=UPSTREAM)
    p.add_argument("--usage", default=USAGE_URL)
    p.add_argument("--cors", action="store_true",
                   help="add Access-Control-Allow-Origin: * (only if a "
                        "browser consumer needs it)")
    p.add_argument("--status-timeout", type=float, default=STATUS_TIMEOUT,
                   help="budget for the upstream status fetch (s)")
    args = p.parse_args(argv)

    cfg = RelayConfig(args.upstream, args.usage, args.status_timeout,
                      cors=args.cors)
    httpd = make_server(args.bind, args.listen_port, cfg)
    print(f"Relay: {args.upstream} → {args.bind}:{args.listen_port} "
          f"(usage: {args.usage}, schema {SCHEMA}, "
          f"status budget {args.status_timeout}s)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
