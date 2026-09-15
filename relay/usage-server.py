#!/usr/bin/env python3
"""Hermes token-usage micro-service — reads the same SessionDB the dashboard
analytics use and serves compact 24h/7d token totals over HTTP for the
ESP32 dashboard relay.

Defaults changed (audit A3): binds 127.0.0.1:9121. When a relay on another
host needs it, pass `--bind 0.0.0.0` DELIBERATELY (see deploy/ + PROJECT.md).

Data source: ~/.hermes/state.db (sessions table), identical SQL to
hermes_cli/web_routers/analytics.py totals query — deliberately unchanged
(audit A8): "24h"/"7d" are COHORTS (sessions STARTED inside the window,
summing their CUMULATIVE counters), not rolling windows; the DB has no
per-event timestamps. The payload states this via `window_basis`.

Caching (audit A4): the collection payload is refreshed on a background
thread at most once per REFRESH_INTERVAL (~10 s); requests serve the cached
snapshot so request rate can never drive DB queries or host subprocesses.

stdlib-only (system python3 on Linux and macOS). No dependencies.
"""
import argparse
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB_PATH = str(Path.home() / ".hermes" / "state.db")
PORT = 9121
BIND = "127.0.0.1"
REFRESH_INTERVAL = 10.0   # max collection rate (A4)
HOST_TIMEOUT = 3.0        # per-subprocess budget for host stats (A13)
MAX_CONCURRENCY = 8       # over-limit requests are rejected, not queued (A4)
CONN_TIMEOUT = 5.0        # per-connection socket timeout (A4)
WINDOW_BASIS = "session_started_at"

TOTALS_SQL = """
    SELECT COALESCE(SUM(input_tokens),0) AS input,
           COALESCE(SUM(output_tokens),0) AS output,
           COALESCE(SUM(cache_read_tokens),0) AS cache,
           COALESCE(SUM(reasoning_tokens),0) AS reasoning,
           COALESCE(SUM(api_call_count),0) AS api_calls,
           COUNT(*) AS sessions,
           COALESCE(SUM(estimated_cost_usd),0) AS est_cost
    FROM sessions WHERE started_at > ?
"""


def _totals(db_path: str, days: int):
    """Cohort totals for sessions started within `days`. Returns None on
    any sampling failure — never a numeric 0 dressed as data (A6)."""
    cutoff = time.time() - days * 86400
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(TOTALS_SQL, (cutoff,)).fetchone()
        finally:
            conn.close()
    except Exception as e:
        print(f"[usage-server] totals({days}d) failed: {e}", flush=True)
        return None
    return {
        "input": row[0], "output": row[1], "cache": row[2],
        "reasoning": row[3], "api_calls": row[4], "sessions": row[5],
        "est_cost": round(row[6], 4),
        "total": row[0] + row[1] + row[2] + row[3],
    }


def _sh(cmd: str) -> str:
    try:
        return subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=HOST_TIMEOUT).stdout or ""
    except Exception:
        return ""


def _cpu_sample_linux():
    """Sampled CPU utilisation from /proc/stat (two reads, 0.25 s apart).
    Returns int percent 0-100, or None if not obtainable."""
    def read():
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        vals = [int(x) for x in parts]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return idle, sum(vals)
    try:
        idle1, tot1 = read()
        time.sleep(0.25)
        idle2, tot2 = read()
        dtot = tot2 - tot1
        if dtot <= 0:
            return None
        return round((1 - (idle2 - idle1) / dtot) * 100)
    except Exception:
        return None


def _cpu_sample_macos():
    """Sampled CPU utilisation via `top -l 2` (second sample is a real
    interval average). Returns int percent 0-100, or None."""
    out = _sh("top -l 2 -n 0 -stats cpu")
    # take the LAST "CPU usage: x.xx% user, y.yy% sys, z.zz% idle" line
    matches = re.findall(
        r"CPU usage:\s*[\d.]+%\s*user,\s*[\d.]+%\s*sys,\s*([\d.]+)%\s*idle",
        out)
    if not matches:
        return None
    try:
        return round(100.0 - float(matches[-1]))
    except ValueError:
        return None


def _loadavg():
    """1-minute loadavg and core count → normalised load percent."""
    try:
        if platform.system() == "Darwin":
            m = re.search(r"\{\s*([\d.]+)", _sh("sysctl -n vm.loadavg"))
        else:
            with open("/proc/loadavg") as f:
                m = re.match(r"([\d.]+)", f.read())
        load1 = float(m.group(1)) if m else None
    except Exception:
        load1 = None
    nproc = _ncpu()
    if load1 is None or not nproc:
        return None
    return round(min(100.0, load1 / nproc * 100.0))


def _ncpu():
    nproc_m = re.search(r"(\d+)", _sh("sysctl -n hw.ncpu"))
    if nproc_m:
        return int(nproc_m.group(1))
    return os.cpu_count()


def _host_stats() -> dict:
    """Host load/RAM for the desk widget — stdlib only (system python3).

    load_percent: 1-min loadavg ÷ core count, clamped to 100 — a QUEUEING
      metric, window documented (A13; this was previously mislabelled
      cpu_percent).
    cpu_percent: sampled utilisation (macOS `top -l 2`, Linux /proc/stat),
      or null when no real sample is obtainable — never a fake 0 (A13).
    All best-effort; failed parts are null, not zero.
    """
    stats = {"load_percent": _loadavg(), "cpu_percent": None,
             "ram_used_percent": None, "ram_total_mb": None}
    try:
        if platform.system() == "Darwin":
            stats["cpu_percent"] = _cpu_sample_macos()
        else:
            stats["cpu_percent"] = _cpu_sample_linux()

        total_mb = None
        m = re.search(r"(\d+)", _sh("sysctl -n hw.memsize"))
        if m:
            total_mb = int(m.group(1)) / 1048576.0
        elif platform.system() != "Darwin":
            mi = re.search(r"MemTotal:\s+(\d+)\s*kB", _sh("cat /proc/meminfo"))
            if mi:
                total_mb = int(mi.group(1)) / 1024.0
        stats["ram_total_mb"] = int(total_mb) if total_mb else None

        if total_mb:
            if platform.system() == "Darwin":
                vm = _sh("vm_stat")
                page = 4096
                pm = re.search(r"page size of (\d+) bytes", vm)
                if pm:
                    page = int(pm.group(1))

                def _pages(field: str) -> float:
                    fm = re.search(field + r":\s*(\d+)\.", vm)
                    return int(fm.group(1)) * page / 1048576.0 if fm else 0.0
                avail = (_pages("Pages free") + _pages("Pages inactive")
                         + _pages("Pages speculative"))
            else:
                avail = None
                ma = re.search(r"MemAvailable:\s+(\d+)\s*kB",
                               _sh("cat /proc/meminfo"))
                if ma:
                    avail = int(ma.group(1)) / 1024.0
            if avail is not None:
                used = max(0.0, total_mb - avail)
                stats["ram_used_percent"] = round(used / total_mb * 100.0)
    except Exception:
        pass  # failed parts stay null
    return stats


def collect_payload(db_path: str) -> dict:
    h24 = _totals(db_path, 1)
    d7 = _totals(db_path, 7)
    return {
        "h24": h24,
        "d7": d7,
        "host": _host_stats(),
        "generated_at": int(time.time()),
        "window_basis": WINDOW_BASIS,
        "schema": 2,
    }


class Cache:
    """Background-refreshed snapshot (A4): serving is always the cached
    payload; request rate cannot drive collection work."""

    def __init__(self, db_path: str, interval: float):
        self.db_path = db_path
        self.interval = interval
        self._lock = threading.Lock()
        self._payload = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="usage-collector")

    def start(self):
        self._thread.start()
        return self

    def _run(self):
        while True:
            try:
                payload = collect_payload(self.db_path)
                with self._lock:
                    self._payload = payload
            except Exception as e:
                print(f"[usage-server] collection failed: {e}", flush=True)
            time.sleep(self.interval)

    def payload(self):
        with self._lock:
            return self._payload


class Handler(BaseHTTPRequestHandler):
    # Per-connection socket timeout (A4).
    timeout = CONN_TIMEOUT

    def do_GET(self):
        if self.path not in ("/", "/usage"):
            self.send_response(404)
            self.end_headers()
            return
        if not self.server.slots.acquire(blocking=False):
            print(f"[usage-server] 503 over-limit {self.client_address[0]}",
                  flush=True)
            self._send(503, {"error": "usage-server busy, try again"})
            return
        try:
            payload = self.server.cache.payload()
            if payload is None:  # first collection not done yet
                self._send(503, {"error": "usage data not yet collected"})
            else:
                self._send(200, payload)
        finally:
            self.server.slots.release()

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (TimeoutError, ConnectionError):
            self.close_connection = True

    def log_message(self, *args):  # quiet — polled every 10s
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, cache: Cache,
                 max_concurrency: int = MAX_CONCURRENCY):
        super().__init__(addr, Handler)
        self.cache = cache
        self.slots = threading.BoundedSemaphore(max_concurrency)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Hermes token-usage micro-service (stdlib-only)")
    p.add_argument("--db", default=DB_PATH,
                   help=f"path to the Hermes state DB (default {DB_PATH})")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--bind", default=BIND,
                   help=f"interface to bind (default {BIND}; pass "
                        "0.0.0.0 deliberately when a relay on another "
                        "host must reach this service)")
    p.add_argument("--refresh-interval", type=float, default=REFRESH_INTERVAL,
                   help="max collection rate in seconds (cached between)")
    args = p.parse_args(argv)

    cache = Cache(args.db, args.refresh_interval).start()
    print(f"[usage-server] {args.db} on {args.bind}:{args.port} "
          f"(refresh {args.refresh_interval}s, window_basis={WINDOW_BASIS})",
          flush=True)
    Server((args.bind, args.port), cache).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
