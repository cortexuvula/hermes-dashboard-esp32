#!/usr/bin/env python3
"""Hermes Dashboard Relay — runs on the relay host (e.g. omarchy-home).

Fetches the Hermes dashboard /api/status upstream (with a hard TOTAL
budget of STATUS_TIMEOUT seconds enforced against a monotonic deadline —
not merely a per-socket-operation timeout — plus a 128 KB read cap) and
re-serves an ALLOWLISTED compact object on :9120 for the ESP32 on the
LAN. Token-usage totals and host stats come from usage-server (:9121)
via a background refresher thread — a stalled or trickling usage upstream
can NEVER block or delay a response (audit A5). Every upstream fetch
(status and usage alike) runs on a worker thread that the caller abandons
at a hard total budget, so trickled BODY bytes and trickled HEADER lines
are both bounded (Finding 1 + R1). Worst-case response time is bounded by
the status budget: the fetch is abandoned at ~STATUS_TIMEOUT (3.0 s) +
response write, comfortably inside the board's 8 s HTTP wait.

Connection-layer limit (documented limitation, Finding 3): the relay
spawns a thread + fd per accepted connection BEFORE the concurrency
semaphore is consulted, and the per-connection timeout is per-operation —
a client trickling ~1 byte per just-under-CONN_TIMEOUT seconds can hold
a thread for a long time at trivial cost. For a LAN service polled by a
single board this is accepted; it is not a general DoS defence.

Contract (schema 2) — see the CONTRACT section in ../README.md:
  - Only the keys the board parses are emitted; nothing passes through.
  - Unavailable usage data is JSON null, never omitted, never zeroed
    (tokens_24h / tokens_7d / host).
  - usage_age_s  int|null  age of the usage DATA: seconds since the
    producer's generated_at (falling back to the relay's fetch time only
    when generated_at is absent), clamped >= 0. The board has no clock,
    and usage-server may serve its last-good payload indefinitely, so
    this producer-side age is the only staleness signal (Finding 2).
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
# TOTAL budget for the whole upstream status fetch — connect + status
# line + HEADERS + body (R1: the header phase was previously unbounded) —
# enforced by running the fetch on a worker thread joined with this
# budget, plus an in-loop monotonic deadline for the body reads. A
# trickle of bytes or header lines that completes each recv inside the
# socket timeout still aborts here (A5, Finding 1, R1). Worst-case
# response ≈ this + response write, inside the board's 8 s HTTP wait.
STATUS_TIMEOUT = 3.0
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
    """GET url with a TOTAL time budget and a hard byte cap.

    `timeout` bounds the WHOLE fetch — connect + status line + headers +
    body. urllib's timeout is per-socket-operation, so an upstream that
    trickles bytes or HEADER LINES (each recv completing inside the socket
    timeout) could otherwise hold the caller arbitrarily long (Finding 1
    + R1). The fetch therefore runs on a daemon worker thread which the
    caller joins with the remaining budget; if the worker is still alive
    at the deadline it is abandoned (daemon threads never block exit) and
    the caller raises. Inside the worker, a monotonic deadline is checked
    before every body read and the socket timeout is re-armed to the
    REMAINING budget, so a well-behaved upstream aborts on its own
    without waiting to be abandoned. The byte cap is enforced even when
    the length is unknown or chunked (A9).

    Raises UpstreamError on transport failure, over-budget or over-cap.
    """
    deadline = time.monotonic() + timeout
    result: dict = {}

    def _work():
        try:
            result["body"] = _fetch_body(url, deadline, cap)
        except Exception as e:  # includes UpstreamError
            result["error"] = e

    def _arm(sock, remaining):
        # Bound the NEXT blocking operation by the remaining budget only.
        try:
            sock.settimeout(max(0.05, remaining))
        except Exception:
            pass

    def _fetch_body(url, deadline, cap):
        req = urllib.request.Request(url,
                                     headers={"Accept": "application/json"})
        # An urllib timeout equal to the full budget bounds connect/status
        # line/each header line individually; the join() deadline below is
        # what bounds the phases collectively.
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # read1 returns as soon as ANY bytes are available; a plain
            # read(8192) on a length-delimited body blocks until the full
            # amount arrives, which would ignore the deadline between reads.
            reader = getattr(resp, "read1", None) or resp.read
            sock = None
            try:  # http.client: BufferedReader → SocketIO → socket
                sock = resp.fp.raw._sock
            except Exception:
                pass
            chunks, total = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UpstreamError(
                        f"upstream exceeded total budget of {timeout}s: {url}")
                if sock is not None:
                    _arm(sock, remaining)
                chunk = reader(8192)
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    raise UpstreamError(
                        f"upstream body exceeds {cap} byte cap: {url}")
                chunks.append(chunk)
            return b"".join(chunks)

    worker = threading.Thread(target=_work, daemon=True,
                              name="relay-upstream-fetch")
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        # Still running past the deadline: abandon it (daemon — it dies
        # with the process; its socket closes when it next times out on
        # its own re-armed timeouts) and report the over-budget.
        raise UpstreamError(
            f"upstream exceeded total budget of {timeout}s: {url}")
    if "error" in result:
        raise result["error"]
    return result["body"]


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


# A producer timestamp outside this window of the relay's clock is treated
# as unknown (R3): a negative or absurdly-far-future generated_at would
# otherwise yield a nonsense age (~1.8 Gs for epoch-negative values) or be
# forwarded as-is. Both cases fall back to the fetch-time age path.
GENERATED_AT_MAX_SKEW_S = 24 * 3600  # ±1 day around the relay's clock


def _sanitize_generated_at(ga, now):
    """Return a plausible producer timestamp or None (R3)."""
    if not isinstance(ga, (int, float)) or isinstance(ga, bool):
        return None
    if ga <= 0 or abs(now - ga) > GENERATED_AT_MAX_SKEW_S:
        return None
    return ga


def build_payload(status: dict, refresher: UsageRefresher, now=None) -> dict:
    """Assemble the schema-2 response: allowlisted status keys + usage."""
    payload = {k: status.get(k) for k in STATUS_ALLOWLIST}  # missing → null
    now = time.time() if now is None else now

    usage, taken_at = refresher.snapshot() if refresher else (None, None)
    ga = usage.get("generated_at") if isinstance(usage, dict) else None
    ga = _sanitize_generated_at(ga, now)
    payload["tokens_24h"] = _pick(usage.get("h24"), TOKEN_FIELDS) \
        if isinstance(usage, dict) else None
    payload["tokens_7d"] = _pick(usage.get("d7"), TOKEN_FIELDS) \
        if isinstance(usage, dict) else None
    payload["host"] = _pick(usage.get("host"), HOST_FIELDS) \
        if isinstance(usage, dict) else None
    # usage_age_s measures the age of the DATA, not of the relay's fetch:
    # prefer the producer's generated_at — usage-server serves its last-good
    # payload indefinitely when its collector fails, so a freshly-fetched
    # snapshot can still be hours old (Finding 2). generated_at of 0/None
    # means unknown → fall back to the fetch time; clamp at >= 0 so a skewed
    # future producer timestamp can never emit a negative age.
    data_time = ga if ga else taken_at
    payload["usage_age_s"] = max(0, int(now - data_time)) if data_time else None
    payload["generated_at"] = ga if ga else None
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
                # Full detail (incl. the upstream URL) goes to the relay's
                # log only; LAN clients get a generic reason (N4).
                print(f"[relay] 502 upstream error for "
                      f"{self.client_address[0]}: {e}", flush=True)
                self._send(502, {"error": "upstream status fetch failed"})
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
