#!/usr/bin/env python3
"""Hermes token-usage micro-service — reads the same SessionDB the dashboard
analytics use and serves compact 24h/7d token totals over HTTP for the
ESP32 dashboard relay (omarchy-home).

Bind: 0.0.0.0:9121 (aggregate daily totals only — no prompts, no PII).
Data source: ~/.hermes/state.db (sessions table), identical SQL to
hermes_cli/web_routers/analytics.py totals query.
"""
import json
import re
import sqlite3
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB_PATH = str(Path.home() / ".hermes" / "state.db")
PORT = 9121

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


def _totals(days: int):
    cutoff = time.time() - days * 86400
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        row = conn.execute(TOTALS_SQL, (cutoff,)).fetchone()
    finally:
        conn.close()
    return {
        "input": row[0], "output": row[1], "cache": row[2],
        "reasoning": row[3], "api_calls": row[4], "sessions": row[5],
        "est_cost": round(row[6], 4),
        "total": row[0] + row[1] + row[2] + row[3],
    }


def _sh(cmd: str) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=3).stdout or ""
    except Exception:
        return ""


def _host_stats() -> dict:
    """Host CPU/RAM for the desk widget — stdlib only (system python3).

    CPU: 1-min loadavg / core count → %. RAM: vm_stat pages vs hw.memsize.
    All best-effort; empty dict on any failure (callers tolerate absence).
    """
    try:
        total_mb = 0
        m = re.search(r"(\d+)", _sh("sysctl -n hw.memsize"))
        if m:
            total_mb = int(m.group(1)) / 1048576.0
        nproc_m = re.search(r"(\d+)", _sh("sysctl -n hw.ncpu"))
        nproc = int(nproc_m.group(1)) if nproc_m else 1
        load_m = re.search(r"\{\s*([\d.]+)", _sh("sysctl -n vm.loadavg"))
        load1 = float(load_m.group(1)) if load_m else 0.0

        vm = _sh("vm_stat")
        page = 4096
        pm = re.search(r"page size of (\d+) bytes", vm)
        if pm:
            page = int(pm.group(1))
        def _pages(field: str) -> float:
            fm = re.search(field + r":\s*(\d+)\.", vm)
            return int(fm.group(1)) * page / 1048576.0 if fm else 0.0
        avail_mb = _pages("Pages free") + _pages("Pages inactive") + _pages("Pages speculative")
        used_mb = max(0.0, total_mb - avail_mb)

        return {
            "cpu_percent": round(min(100.0, load1 / nproc * 100.0)),
            "ram_used_percent": round(used_mb / total_mb * 100.0) if total_mb else 0,
            "ram_total_mb": int(total_mb),
        }
    except Exception:
        return {}


def _payload():
    return {
        "h24": _totals(1),
        "d7": _totals(7),
        "host": _host_stats(),
        "generated_at": int(time.time()),
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/usage"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = json.dumps(_payload()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            body = json.dumps({"error": str(e)}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):  # quiet — polled every 10s
        pass


if __name__ == "__main__":
    print(f"[usage-server] {DB_PATH} on :{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
