#!/usr/bin/env python3
"""Tests for relay/relay.py — stdlib unittest, synthetic upstreams on
ephemeral ports. Run: python3 -m unittest discover -s tests -v
"""
import http.server
import json
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "relay"))
import relay  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SlowHandler(http.server.BaseHTTPRequestHandler):
    """Configurable upstream: replies from attributes set per-test."""

    status_delay = 0.0
    status_body = b"{}"
    status_code = 200
    # usage_delay big → stalled usage upstream (audit A5)
    usage_delay = 0.0
    usage_body = b"{}"
    usage_code = 200
    chunked = False
    # status_drip > 0 → send the status body 1 byte at a time with that
    # delay, Content-Length declared (Finding 1: every individual recv
    # completes well inside the socket timeout)
    status_drip = 0.0
    # header_drip > 0 → send the status line, then trickle HEADER bytes
    # at that delay (R1: header phase must respect the total budget)
    header_drip = 0.0

    def do_GET(self):
        if self.path.startswith("/api/status"):
            if self.header_drip > 0:
                self.wfile.write(b"HTTP/1.0 200 OK\r\n")
                self.wfile.flush()
                for line in (b"Content-Type: application/json\r\n",
                             f"Content-Length: "
                             f"{len(self.status_body)}\r\n".encode(),
                             b"\r\n"):
                    for i in range(len(line)):
                        self.wfile.write(line[i:i + 1])
                        self.wfile.flush()
                        time.sleep(self.header_drip)
                self.wfile.write(self.status_body)
                return
            if self.status_drip > 0:
                time.sleep(self.status_delay)
                self.send_response(self.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(self.status_body)))
                self.end_headers()
                for i in range(len(self.status_body)):
                    self.wfile.write(self.status_body[i:i + 1])
                    self.wfile.flush()
                    time.sleep(self.status_drip)
                return
            time.sleep(self.status_delay)
            self._reply(self.status_code, self.status_body)
        else:
            time.sleep(self.usage_delay)
            self._reply(self.usage_code, self.usage_body)

    def _reply(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if self.chunked:
            # no Content-Length → connection terminates the body
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        else:
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_upstream():
    """Start a synthetic status+usage upstream on ephemeral ports.
    Returns (status_url, usage_url, handler_class)."""
    handler = type("H", (SlowHandler,), {})  # per-test attribute isolation
    httpd_status = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd_usage = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    for h in (httpd_status, httpd_usage):
        threading.Thread(target=h.serve_forever, daemon=True).start()
    s = f"http://127.0.0.1:{httpd_status.server_port}/api/status"
    u = f"http://127.0.0.1:{httpd_usage.server_port}/"
    return s, u, handler


SAMPLE_STATUS = {
    "active_sessions": 2, "active_agents": 1, "gateway_busy": False,
    "version": "v0.21", "overall": "ok",
    "gateway_platforms": {"telegram": {"state": "connected",
                                       "needs_attention": False}},
    "disk": {"used_percent": 84.2},
    "profiles": ["a", "b"],
    "can_update_hermes": False,
    "nous_session_valid": "valid",
    "components": {"gateway": {"status": "ok"}},
    # keys the board does NOT use — must be dropped (A3):
    "secret_admin_flag": True,
    "internal_routing": {"private": "data"},
    "auth_token": "s3cret",
}

SAMPLE_USAGE = {
    "h24": {"input": 10, "output": 20, "cache": 5, "reasoning": 1,
            "api_calls": 3, "sessions": 2, "est_cost": 0.42,
            "total": 36, "unused_field": "x"},
    "d7": {"input": 100, "output": 200, "cache": 50, "reasoning": 10,
           "api_calls": 30, "sessions": 9, "est_cost": 4.2, "total": 360},
    "host": {"cpu_percent": 12, "load_percent": 34, "ram_used_percent": 61,
             "ram_total_mb": 32768, "secret": "no"},
    "generated_at": None,  # per-test: set fresh — a fixed epoch would be
                           # >1 d old and (correctly) sanitized to null (R3)
    "window_basis": "session_started_at",
}


class RelayTestCase(unittest.TestCase):
    def setUp(self):
        self.status_url, self.usage_url, self.handler = start_upstream()
        self.handler.status_body = json.dumps(SAMPLE_STATUS).encode()
        self.handler.usage_body = json.dumps(SAMPLE_USAGE).encode()
        self.cfg = relay.RelayConfig(self.status_url, self.usage_url)
        self.httpd = relay.make_server("127.0.0.1", 0, self.cfg)
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()
        self.port = self.httpd.server_port
        self.url = f"http://127.0.0.1:{self.port}/api/status"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, url=None):
        with urllib.request.urlopen(url or self.url, timeout=10) as r:
            return r.status, json.loads(r.read())


class TestAllowlist(RelayTestCase):
    def test_unused_upstream_keys_are_dropped(self):
        code, data = self.get()
        self.assertEqual(code, 200)
        for key in ("secret_admin_flag", "internal_routing", "auth_token"):
            self.assertNotIn(key, data, f"allowlist leaked upstream key {key!r}")

    def test_exact_top_level_keyset(self):
        _, data = self.get()
        expected = set(relay.STATUS_ALLOWLIST) | {
            "tokens_24h", "tokens_7d", "host", "usage_age_s",
            "generated_at", "schema"}
        self.assertEqual(set(data), expected)

    def test_nested_usage_fields_allowlisted(self):
        _, data = self.get()
        self.assertEqual(
            set(data["tokens_24h"]),
            {"total", "input", "output", "cache", "reasoning",
             "api_calls", "sessions", "est_cost"})
        self.assertNotIn("unused_field", data["tokens_24h"])
        self.assertNotIn("secret", data["host"])
        # tokens_7d carries input/output (schema 2)
        self.assertIn("input", data["tokens_7d"])
        self.assertIn("output", data["tokens_7d"])


class TestNullSemantics(RelayTestCase):
    def test_usage_null_not_omitted(self):
        # usage upstream dead FROM THE START → tokens/host null, not omitted
        # (a refresher that had a good snapshot keeps serving it — that is
        # the intended staleness behaviour, tested separately below)
        cfg = relay.RelayConfig(self.status_url, "http://127.0.0.1:9/")
        cfg.refresher.interval = 0.05
        httpd = relay.make_server("127.0.0.1", 0, cfg)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            time.sleep(0.3)  # let the refresher fail at least once
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{httpd.server_port}/api/status",
                    timeout=10) as r:
                self.assertEqual(r.status, 200)
                data = json.loads(r.read())
            for key in ("tokens_24h", "tokens_7d", "host", "usage_age_s",
                        "generated_at"):
                self.assertIn(key, data)
                self.assertIsNone(data[key],
                                  f"{key} should be null, not omitted")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_components_null_when_upstream_omits(self):
        body = dict(SAMPLE_STATUS)
        del body["components"]
        self.handler.status_body = json.dumps(body).encode()
        _, data = self.get()
        self.assertIn("components", data)
        self.assertIsNone(data["components"])

    def test_schema_and_generated_at_forwarded(self):
        fresh = dict(SAMPLE_USAGE)
        fresh["generated_at"] = int(time.time()) - 2
        self.handler.usage_body = json.dumps(fresh).encode()
        self.cfg.refresher.interval = 0.05
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, data = self.get()
            if data["generated_at"] == fresh["generated_at"]:
                break
            time.sleep(0.05)
        self.assertEqual(data["schema"], 2)
        self.assertEqual(data["generated_at"], fresh["generated_at"])

    def test_usage_age_s_int(self):
        fresh = dict(SAMPLE_USAGE)
        fresh["generated_at"] = int(time.time()) - 2
        self.handler.usage_body = json.dumps(fresh).encode()
        self.cfg.refresher.interval = 0.05
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, data = self.get()
            if data["generated_at"] == fresh["generated_at"]:
                break
            time.sleep(0.05)
        self.assertIsInstance(data["usage_age_s"], int)
        self.assertLess(data["usage_age_s"], 30)


class TestNonBlocking(RelayTestCase):
    def test_drip_feed_bounded_by_total_budget(self):
        # Finding 1: an upstream that TRICKLES the body (declared
        # Content-Length, one byte per drip, each recv completing inside
        # the socket timeout) must not hold the response past the total
        # status budget. Old code: per-recv socket timeout only →
        # unbounded (measured 4.29 s @0.2 s/byte, 10.58 s @0.5 s/byte).
        self.handler.status_drip = 0.5   # 24-byte body → ~12 s if unbounded
        t0 = time.monotonic()
        try:
            urllib.request.urlopen(self.url, timeout=30)
            self.fail("expected 502 for over-budget drip upstream")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 502)
            e.read()
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, relay.STATUS_TIMEOUT + 1.0,
                        f"drip upstream answered after {elapsed:.2f}s, "
                        f"budget {relay.STATUS_TIMEOUT}s exceeded")

    def test_header_drip_bounded_by_total_budget(self):
        # R1: an upstream that sends the status line then TRICKLES THE
        # HEADERS (1 byte per 0.5 s, every recv inside the per-op socket
        # timeout) must not hold the response past the total budget.
        # Old code bounded the body loop only → measured 7.02 s stuck.
        self.handler.header_drip = 0.5
        t0 = time.monotonic()
        try:
            urllib.request.urlopen(self.url, timeout=30)
            self.fail("expected 502 for over-budget header-drip upstream")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 502)
            e.read()
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, relay.STATUS_TIMEOUT + 1.0,
                        f"header-drip upstream answered after {elapsed:.2f}s, "
                        f"budget {relay.STATUS_TIMEOUT}s exceeded")

    def test_over_budget_fetch_releases_worker_and_fd(self):
        # S2: when the join deadline expires, the fetch socket is
        # force-closed so the abandoned worker exits promptly — repeated
        # over-budget polls must not accumulate worker threads.
        self.handler.status_delay = 30.0   # upstream never answers in time

        def workers():
            return [t for t in threading.enumerate()
                    if t.name == "relay-upstream-fetch"]

        for _ in range(5):
            t0 = time.monotonic()
            try:
                urllib.request.urlopen(self.url, timeout=30)
                self.fail("expected 502")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 502)
                e.read()
            self.assertLess(time.monotonic() - t0,
                            relay.STATUS_TIMEOUT + 1.0)
        time.sleep(0.5)  # let the abandoned workers observe the closed socket
        self.assertEqual(len(workers()), 0,
                         "abandoned fetch workers are lingering (S2)")
        self.handler.status_delay = 0.0

    def test_stalled_usage_does_not_block_response(self):
        # usage upstream hangs forever; relay must still answer fast (A5)
        self.cfg.refresher.url = self.usage_url
        self.handler.usage_delay = 30.0
        self.cfg.refresher.interval = 0.05
        time.sleep(0.2)  # let one refresh attempt start (it will hang)
        t0 = time.monotonic()
        code, data = self.get()
        elapsed = time.monotonic() - t0
        self.assertEqual(code, 200)
        self.assertLess(elapsed, 4.0,
                        f"worst-case response {elapsed:.2f}s exceeds 4 s")
        # usage is unavailable, presented as null — never blocks the response
        self.assertIn("tokens_24h", data)

    def test_502_when_status_fails(self):
        self.handler.status_code = 500
        try:
            urllib.request.urlopen(self.url, timeout=10)
            self.fail("expected 502")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 502)
            body = json.loads(e.read())
            self.assertIn("error", body)

    def test_withheld_request_cannot_block_concurrent_poll(self):
        # A client connects and sends nothing (withheld request). A normal
        # poll must still complete (A4: threading + socket timeouts).
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            t0 = time.monotonic()
            code, _ = self.get()
            elapsed = time.monotonic() - t0
            self.assertEqual(code, 200)
            self.assertLess(elapsed, 4.0,
                            f"withheld request stalled a poll: {elapsed:.2f}s")
        finally:
            sock.close()


class TestCaps(RelayTestCase):
    def test_oversize_status_rejected(self):
        big = dict(SAMPLE_STATUS)
        big["blob"] = "x" * (relay.STATUS_CAP + 1)
        self.handler.status_body = json.dumps(big).encode()
        try:
            urllib.request.urlopen(self.url, timeout=10)
            self.fail("expected 502 for oversize body")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 502)

    def test_oversize_usage_rejected_by_refresher(self):
        big = dict(SAMPLE_USAGE)
        big["blob"] = "x" * (relay.USAGE_CAP + 1)
        self.handler.usage_body = json.dumps(big).encode()
        self.cfg.refresher.interval = 0.05
        time.sleep(0.3)
        _, data = self.get()
        # over-cap usage is treated as unavailable → null, response still 200
        self.assertIsNone(data["tokens_24h"])

    def test_oversize_unknown_length_enforced(self):
        # chunked / unknown-length oversize body must still hit the cap
        big = dict(SAMPLE_STATUS)
        big["blob"] = "x" * (relay.STATUS_CAP + 1)
        self.handler.status_body = json.dumps(big).encode()
        self.handler.chunked = True  # no Content-Length header
        try:
            urllib.request.urlopen(self.url, timeout=10)
            self.fail("expected 502 for oversize unknown-length body")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 502)


class TestUsageAge(RelayTestCase):
    """usage_age_s must measure DATA age (producer's generated_at), not
    the relay's fetch time (Finding 2)."""

    def test_age_reflects_producer_timestamp(self):
        # usage-server serving its last-good payload (collector dead):
        # generated_at is 1 hour old but the relay fetched it seconds ago.
        # The age must report ~3600 s, not ~0 s.
        stale = dict(SAMPLE_USAGE)
        stale["generated_at"] = int(time.time()) - 3600
        self.handler.usage_body = json.dumps(stale).encode()
        self.cfg.refresher.interval = 0.05
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, data = self.get()
            if data["generated_at"] == stale["generated_at"]:
                break
            time.sleep(0.05)
        self.assertEqual(data["generated_at"], stale["generated_at"])
        self.assertGreater(data["usage_age_s"], 3500,
                           f"age {data['usage_age_s']}s looks like fetch "
                           f"age, not data age")
        self.assertLess(data["usage_age_s"], 3700)

    def test_age_falls_back_to_fetch_time_without_generated_at(self):
        # producer omits generated_at → fall back to relay fetch time
        no_ga = dict(SAMPLE_USAGE)
        del no_ga["generated_at"]
        self.handler.usage_body = json.dumps(no_ga).encode()
        self.cfg.refresher.interval = 0.05
        deadline = time.monotonic() + 5
        _, data = None, None
        while time.monotonic() < deadline:
            _, data = self.get()
            if data["generated_at"] is None:
                break
            time.sleep(0.05)
        self.assertIsNone(data["generated_at"])
        self.assertIsNotNone(data["usage_age_s"], "fetch-time fallback")
        self.assertLess(data["usage_age_s"], 60)

    def test_age_never_negative_with_skewed_future_timestamp(self):
        # producer clock ahead of the relay → age clamped to 0, not negative
        future = dict(SAMPLE_USAGE)
        future["generated_at"] = int(time.time()) + 600
        self.handler.usage_body = json.dumps(future).encode()
        self.cfg.refresher.interval = 0.05
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, data = self.get()
            if data["generated_at"] == future["generated_at"]:
                break
            time.sleep(0.05)
        self.assertEqual(data["generated_at"], future["generated_at"])
        self.assertEqual(data["usage_age_s"], 0)

    def test_implausible_generated_at_treated_as_unknown(self):
        # R3: negative / zero / absurdly-far-FUTURE producer timestamps
        # must not yield a nonsense age (epoch-negative used to produce
        # ~1_800_005_000 s) nor be forwarded as-is.
        for ga in (-5000, 0, int(time.time()) + 48 * 3600):
            stale = dict(SAMPLE_USAGE)
            stale["generated_at"] = ga
            self.handler.usage_body = json.dumps(stale).encode()
            self.cfg.refresher.interval = 0.05
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                _, data = self.get()
                if data["generated_at"] is None:
                    break
                time.sleep(0.05)
            self.assertIsNone(data["generated_at"])
            self.assertIsNotNone(data["usage_age_s"])  # fetch-time fallback
            self.assertLess(data["usage_age_s"], 120,
                            f"nonsense age for generated_at={ga}: "
                            f"{data['usage_age_s']}")

    def test_arbitrarily_old_data_reports_true_age(self):
        # S1: OLD producer timestamps are the truth, not nonsense — the
        # old ±24 h window rejected the past direction and fell back to
        # FETCH time, reporting ~0 s ("fresh") for exactly the stalest
        # data (usage-server serving last-good for days). 25 h / 48 h /
        # 7 d must report the true large age; 23 h (inside the old
        # window) must keep working.
        for label, age_s in (("23h", 23 * 3600), ("25h", 25 * 3600),
                             ("48h", 48 * 3600), ("7d", 7 * 86400)):
            stale = dict(SAMPLE_USAGE)
            stale["generated_at"] = int(time.time()) - age_s
            self.handler.usage_body = json.dumps(stale).encode()
            self.cfg.refresher.interval = 0.05
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                _, data = self.get()
                if data["generated_at"] == stale["generated_at"]:
                    break
                time.sleep(0.05)
            self.assertEqual(data["generated_at"], stale["generated_at"])
            self.assertGreater(data["usage_age_s"], age_s - 120,
                               f"{label}-old data reported age "
                               f"{data['usage_age_s']}s — must be ~{age_s}s, "
                               f"not fresh")

    def test_age_null_when_never_had_data(self):
        cfg = relay.RelayConfig(self.status_url, "http://127.0.0.1:9/")
        cfg.refresher.interval = 0.05
        httpd = relay.make_server("127.0.0.1", 0, cfg)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            time.sleep(0.3)
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{httpd.server_port}/api/status",
                    timeout=10) as r:
                data = json.loads(r.read())
            self.assertIsNone(data["usage_age_s"])
            self.assertIsNone(data["generated_at"])
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestErrorBody(RelayTestCase):
    def test_502_body_does_not_leak_upstream_url(self):
        # N4: the upstream exception (which contains the URL) must reach
        # the relay log only — the client gets a generic reason.
        self.handler.status_code = 500
        try:
            urllib.request.urlopen(self.url, timeout=10)
            self.fail("expected 502")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 502)
            body = e.read().decode()
            self.assertNotIn("127.0.0.1", body)
            self.assertNotIn("http://", body)
            self.assertIn("error", json.loads(body))


class TestCors(RelayTestCase):
    def test_no_cors_header_by_default(self):
        with urllib.request.urlopen(self.url, timeout=10) as r:
            self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"))

    def test_cors_opt_in(self):
        cfg = relay.RelayConfig(self.status_url, self.usage_url, cors=True)
        httpd = relay.make_server("127.0.0.1", 0, cfg)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{httpd.server_port}/api/status",
                    timeout=10) as r:
                self.assertEqual(
                    r.headers.get("Access-Control-Allow-Origin"), "*")
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestConcurrency(RelayTestCase):
    def test_over_limit_rejected_not_queued(self):
        # Build a tiny-slot server: 1 slot, status upstream slow.
        cfg = relay.RelayConfig(self.status_url, "http://127.0.0.1:9/")
        httpd = relay.make_server("127.0.0.1", 0, cfg, max_concurrency=1)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_port}/api/status"
        try:
            self.handler.status_delay = 1.0
            # Fire two requests CONCURRENTLY: both hit the slow upstream
            # while the single slot is held by the first.
            results = []

            def poll():
                try:
                    r = urllib.request.urlopen(url, timeout=10)
                    results.append((r.status, r.read()))
                except urllib.error.HTTPError as e:
                    results.append((e.code, e.read()))

            threads = [threading.Thread(target=poll) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
            codes = sorted(c for c, _ in results)
            self.assertEqual(len(results), 2, "both requests must complete")
            self.assertEqual(codes, [200, 503],
                             f"expected one 200 + one 503, got {codes}")
            body_503 = [b for c, b in results if c == 503][0]
            self.assertIn(b"busy", body_503)
        finally:
            self.handler.status_delay = 0.0
            httpd.shutdown()
            httpd.server_close()


class TestAllOrNothing(RelayTestCase):
    """S3: period/host objects are complete-and-numeric or null — never
    partial (a null INSIDE a present object renders as measured 0 on the
    board, because per-field defaults apply once the object is present)."""

    def _get_with_usage(self, usage):
        self.handler.usage_body = json.dumps(usage).encode()
        self.cfg.refresher.interval = 0.05
        deadline = time.monotonic() + 5
        data = None
        while time.monotonic() < deadline:
            _, data = self.get()
            if data["generated_at"] == usage.get("generated_at"):
                break
            time.sleep(0.05)
        return data

    def test_period_one_field_null_nulls_whole_object(self):
        u = dict(SAMPLE_USAGE)
        u["generated_at"] = int(time.time()) - 2
        u["h24"] = dict(u.get("h24") or {})
        u["h24"]["total"] = None
        data = self._get_with_usage(u)
        self.assertIsNone(data["tokens_24h"],
                          "one null field must null the whole period object")
        self.assertIsNotNone(data["tokens_7d"],
                             "the untouched period stays intact")

    def test_period_mistyped_field_nulls_whole_object(self):
        for bad in ("0.42", True):   # string where a number belongs; bool
            u = dict(SAMPLE_USAGE)
            u["generated_at"] = int(time.time()) - 2
            u["h24"] = dict(u.get("h24") or {})
            u["h24"]["est_cost"] = bad
            data = self._get_with_usage(u)
            self.assertIsNone(data["tokens_24h"],
                               f"mistyped field {bad!r} must null the object")
            u["h24"]["est_cost"] = 0.42
            u["h24"].pop("cache", None)   # missing field, same rule
            data = self._get_with_usage(u)
            self.assertIsNone(data["tokens_24h"],
                              "a missing field must null the object")

    def test_period_complete_numeric_emitted_intact(self):
        u = dict(SAMPLE_USAGE)
        u["generated_at"] = int(time.time()) - 2
        complete = {"total": 36, "input": 10, "output": 20, "cache": 5,
                    "reasoning": 1, "api_calls": 3, "sessions": 2,
                    "est_cost": 0.42}
        u["h24"] = complete
        data = self._get_with_usage(u)
        self.assertEqual(data["tokens_24h"], complete)
        self.assertEqual(set(data["tokens_24h"]), set(relay.TOKEN_FIELDS))

    def test_host_one_field_null_nulls_whole_object(self):
        u = dict(SAMPLE_USAGE)
        u["generated_at"] = int(time.time()) - 2
        u["host"] = {"cpu_percent": None, "load_percent": 34,
                     "ram_used_percent": 61, "ram_total_mb": 32768}
        data = self._get_with_usage(u)
        self.assertIsNone(data["host"],
                          "one null field must null the whole host object")

    def test_host_mistyped_field_nulls_whole_object(self):
        for field, bad in (("ram_total_mb", "32768"),
                           ("load_percent", False)):
            u = dict(SAMPLE_USAGE)
            u["generated_at"] = int(time.time()) - 2
            u["host"] = {"cpu_percent": 12, "load_percent": 34,
                         "ram_used_percent": 61, "ram_total_mb": 32768}
            u["host"][field] = bad
            data = self._get_with_usage(u)
            self.assertIsNone(data["host"],
                               f"mistyped {field}={bad!r} must null host")

    def test_host_complete_numeric_emitted_intact(self):
        u = dict(SAMPLE_USAGE)
        u["generated_at"] = int(time.time()) - 2
        complete = {"cpu_percent": 12, "load_percent": 34,
                    "ram_used_percent": 61, "ram_total_mb": 32768}
        u["host"] = complete
        data = self._get_with_usage(u)
        self.assertEqual(data["host"], complete)
        self.assertEqual(set(data["host"]), set(relay.HOST_FIELDS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
