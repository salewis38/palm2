"""Status web server: protocol behaviour, security limits, data (snapshot/metrics/history) and integration."""
import asyncio
import json
import pathlib
import re
import socket
import sys
import types
import unittest
from datetime import datetime
from unittest import mock

import palm_gui
from support import DiskWatch, FakeClient, FakeHTTP, FakeResponse, PalmAsyncCase, PalmCase, make_telemetry

SNAP = {
    "service": {"version": "vTEST", "uptime_s": 5, "time": "2026-06-10T12:00:00"},
    "system": {"uptime_s": 90000, "load1": 0.5, "load5": 0.25, "load15": 0.1, "cpus": 4},
    "inverter": {"fresh": True, "age_s": 3, "soc_percent": 55, "pv_w": 1200, "load_w": 553, "grid_w": -20,
                 "battery_w": 556, "voltage_v": 234.4, "frequency_hz": 50.09, "pv_energy_today_wh": 5000,
                 "battery_charge_total_wh": 3246800, "battery_discharge_total_wh": 2747900},
    "ev": {"power_w": 7000}, "environment": {"co2_g_per_kwh": 59, "temp_c": 13.0},
    "control": {"desired": "ECO_OPTIMISE", "applied": "PEAK_SHAVE", "paused": False, "heater_on": None, "off_peak": False},
    "pvoutput": {"queued": 2, "writes_ok": 7, "last_ok_epoch": 1781000000},
    "health": {"ok": False, "failing": {"inverter_read": {"since_s": 60, "count": 3}}},
}


def raw_get(path, extra="", host="127.0.0.1", method="GET"):
    head = f"Host: {host}\r\n" if host else ""
    return f"{method} {path} HTTP/1.1\r\n{head}{extra}\r\n".encode("latin-1")


async def exchange(port, raw, timeout=3.0):
    """Send raw bytes, return everything the server sends until it closes the connection."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(raw)
        await writer.drain()
        return await asyncio.wait_for(reader.read(-1), timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def request(port, raw, timeout=3.0):
    data = await exchange(port, raw, timeout)
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1]) if lines and lines[0] else 0
    headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    return status, headers, body


class ServerCase(PalmAsyncCase):
    token = ""
    allowed_hosts = None

    def snap(self):
        return SNAP

    def hist(self):
        return {"interval_s": 60, "t": [1, 2], "pv": [1, 2], "load": [3, 4], "grid": [0, 0], "battery": [5, 6],
                "soc": [50, 51], "ev": [None, None]}

    async def asyncSetUp(self):
        self.server = self.palm.StatusServer(self.snap, self.hist, "127.0.0.1", 0, self.token, self.allowed_hosts)
        await self.server.start()
        self.addAsyncCleanup(self.server.stop)
        self.port = self.server.port

    async def get(self, path, extra="", host="127.0.0.1", method="GET"):
        return await request(self.port, raw_get(path, extra, host, method))


class Endpoints(ServerCase):
    async def test_page(self):
        status, headers, body = await self.get("/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        self.assertEqual(headers["content-length"], str(len(body)))
        self.assertTrue(body.startswith(b"<!doctype html>"))
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertIn("connect-src 'self'", headers["content-security-policy"])

    async def test_page_loads_nothing_from_other_sites_and_never_injects_html(self):
        page = (await self.get("/"))[2].decode()
        for forbidden in ("src=", "href=", "@import", "url(", "innerHTML", "outerHTML", "document.write", "eval("):
            self.assertNotIn(forbidden, page)
        urls = re.findall(r"https?://[^\s\"']+", page)
        self.assertEqual(urls, ["http://www.w3.org/2000/svg"])          # the SVG namespace id; nothing is fetched

    async def test_status_json(self):
        status, headers, body = await self.get("/status.json")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(json.loads(body), SNAP)

    async def test_history_json(self):
        status, _, body = await self.get("/history.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["soc"], [50, 51])

    async def test_metrics(self):
        status, headers, body = await self.get("/metrics")
        text = body.decode()
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/plain; version=0.0.4; charset=utf-8")
        self.assertIn("palm_battery_soc_percent 55\n", text)
        self.assertIn('palm_info{version="vTEST"} 1', text)
        self.assertIn('palm_battery_state{kind="applied",state="PEAK_SHAVE"} 1', text)
        self.assertIn("palm_system_uptime_seconds 90000\n", text)
        self.assertIn("palm_system_load1 0.5\n", text)
        self.assertIn("palm_system_load15 0.1\n", text)
        self.assertIn("palm_system_cpus 4\n", text)

    async def test_healthz(self):
        status, _, body = await self.get("/healthz")
        self.assertEqual((status, body), (200, b"ok\n"))

    async def test_head_returns_headers_only(self):
        status, headers, body = await self.get("/status.json", method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(int(headers["content-length"]), len(json.dumps(SNAP, separators=(",", ":"))))

    async def test_unknown_paths_and_query_strings(self):
        for path in ("/nope", "/../etc/passwd", "/status.json/extra", "//", "/%2e%2e/etc/passwd"):
            with self.subTest(path=path):
                self.assertEqual((await self.get(path))[0], 404)
        self.assertEqual((await self.get("/status.json?x=1&y=2"))[0], 200)

    async def test_only_get_and_head_are_allowed(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"):
            with self.subTest(method=method):
                status, headers, _ = await self.get("/status.json", method=method)
                self.assertEqual(status, 405)
                self.assertEqual(headers["allow"], "GET, HEAD")


class PageFile(ServerCase):
    """The page lives in palm_gui.py, next to palm.py. It is optional: the controller must never depend on it."""

    async def test_the_page_served_is_the_palm_gui_module(self):
        self.assertEqual((await self.get("/"))[2], palm_gui.PAGE.encode("utf-8"))
        self.assertEqual(self.palm.load_page(), palm_gui.PAGE)

    async def test_the_page_module_is_a_single_string_and_carries_the_system_section(self):
        self.assertIsInstance(palm_gui.PAGE, str)
        for needle in ("<!doctype html>", 'id="system"', "System uptime", "Load average"):
            self.assertIn(needle, palm_gui.PAGE)

    async def start_without_the_file(self, module):
        with mock.patch.dict(sys.modules, {"palm_gui": module}), self.assertLogs("PALM", "WARNING") as logs:
            server = self.palm.StatusServer(self.snap, self.hist, "127.0.0.1", 0)
        await server.start()
        self.addAsyncCleanup(server.stop)
        return server, logs

    async def test_a_missing_file_gives_a_minimal_page_and_everything_else_still_works(self):
        for label, module in (("missing", None), ("no PAGE in it", types.ModuleType("palm_gui"))):
            with self.subTest(label):
                server, logs = await self.start_without_the_file(module)
                self.assertTrue(any("palm_gui.py not found" in line for line in logs.output))
                status, headers, body = await request(server.port, raw_get("/"))
                self.assertEqual(status, 200)
                self.assertIn(b"palm_gui.py", body)
                self.assertIn(b"/status.json", body)
                self.assertIn("default-src 'none'", headers["content-security-policy"])
                self.assertEqual((await request(server.port, raw_get("/status.json")))[0], 200)
                self.assertEqual((await request(server.port, raw_get("/metrics")))[0], 200)

    async def test_the_fallback_page_is_static_and_safe(self):
        page = self.palm.FALLBACK_PAGE
        for forbidden in ("<script", "src=", "innerHTML", "http://", "https://"):
            self.assertNotIn(forbidden, page)

    async def test_a_page_can_be_injected_for_tests_and_overrides_the_file(self):
        server = self.palm.StatusServer(self.snap, self.hist, "127.0.0.1", 0, page="<!doctype html><p>custom</p>")
        await server.start()
        self.addAsyncCleanup(server.stop)
        self.assertEqual((await request(server.port, raw_get("/")))[2], b"<!doctype html><p>custom</p>")

    async def test_a_missing_page_file_does_not_stop_the_service(self):
        self.cfg.Web.enable = True
        svc = self.palm.Service(FakeHTTP())
        svc.manager.start, svc.manager.shutdown = mock.AsyncMock(), mock.AsyncMock()
        seen = {}

        async def hook():
            seen["page"] = await request(svc.web.port, raw_get("/"))
            seen["status"] = await request(svc.web.port, raw_get("/status.json"))
            svc.stop_event.set()

        svc._sleep_until_next_cycle = hook
        with mock.patch.dict(sys.modules, {"palm_gui": None}), mock.patch.object(self.palm, "Client", lambda i, p: FakeClient()), \
                self.assertLogs("PALM", "WARNING") as logs:
            rc = await svc.run()
        self.assertEqual(rc, 0)
        self.assertIn(b"palm_gui.py", seen["page"][2])
        self.assertEqual(seen["status"][0], 200)
        self.assertEqual(len([line for line in logs.output if "palm_gui.py not found" in line]), 1)


class Robustness(ServerCase):
    async def test_malformed_requests_are_refused(self):
        for raw in (b"BANANA\r\n\r\n", b"GET /\r\n\r\n", b"GET / FTP/1.0\r\n\r\n", b"\x00\x01\x02\r\n\r\n"):
            with self.subTest(raw=raw):
                self.assertEqual((await request(self.port, raw))[0], 400)
        self.assertEqual((await self.get("/healthz"))[0], 200)               # still serving

    async def test_oversized_request_line(self):
        status, _, _ = await request(self.port, b"GET /" + b"a" * 5000 + b" HTTP/1.1\r\n\r\n")
        self.assertEqual(status, 414)
        self.assertEqual((await self.get("/healthz"))[0], 200)

    async def test_oversized_and_excessive_headers(self):
        big_line = b"GET / HTTP/1.1\r\nX: " + b"a" * 6000 + b"\r\n\r\n"
        self.assertEqual((await request(self.port, big_line))[0], 431)
        many_small = b"GET / HTTP/1.1\r\n" + b"".join(b"X-%d: y\r\n" % i for i in range(60)) + b"\r\n"
        self.assertEqual((await request(self.port, many_small))[0], 431)
        big_total = b"GET / HTTP/1.1\r\n" + b"".join(b"X-%d: %s\r\n" % (i, b"z" * 1000) for i in range(12)) + b"\r\n"
        self.assertEqual((await request(self.port, big_total))[0], 431)
        self.assertEqual((await self.get("/healthz"))[0], 200)

    async def test_slow_or_silent_clients_are_dropped(self):
        with mock.patch.object(self.palm, "WEB_REQUEST_TIMEOUT_S", 0.2):
            self.assertEqual(await exchange(self.port, b""), b"")
            self.assertEqual(await exchange(self.port, b"GET / HTTP/1.1\r\nHost: x"), b"")   # never finishes
        self.assertEqual((await self.get("/healthz"))[0], 200)

    async def test_connection_cap(self):
        with mock.patch.object(self.palm, "WEB_MAX_CONNECTIONS", 2), \
                mock.patch.object(self.palm, "WEB_REQUEST_TIMEOUT_S", 5.0):
            idle = [await asyncio.open_connection("127.0.0.1", self.port) for _ in range(2)]
            await asyncio.sleep(0.1)
            self.assertEqual((await self.get("/healthz"))[0], 503)           # over the cap
            for _, writer in idle:
                writer.close()
            await asyncio.sleep(0.1)
            self.assertEqual((await self.get("/healthz"))[0], 200)           # capacity is released again

    async def test_early_replies_are_delivered_even_when_the_request_body_is_unread(self):
        """A POST with a body is refused with 405 before the body is read; the client must still get the 405."""
        body = b"x" * 3000
        raw = (b"POST /status.json HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 3000\r\n\r\n" + body)
        for _ in range(20):
            status, headers, _ = await request(self.port, raw)
            self.assertEqual(status, 405)
            self.assertEqual(headers["allow"], "GET, HEAD")

    async def test_a_failing_handler_gives_500_and_the_server_carries_on(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return SNAP

        self.server.snapshot = flaky
        with self.assertLogs("PALM", "ERROR"):
            self.assertEqual((await self.get("/status.json"))[0], 500)
        self.assertEqual((await self.get("/"))[0], 200)
        self.assertEqual((await self.get("/status.json"))[0], 200)

    async def test_no_tasks_or_connections_leak(self):
        before = len(asyncio.all_tasks())
        for _ in range(40):
            await self.get("/status.json")
        await asyncio.sleep(0.1)
        self.assertEqual(len(asyncio.all_tasks()), before)
        self.assertEqual((self.server._active, len(self.server._writers)), (0, 0))

    async def test_serving_writes_nothing_to_disk(self):
        with DiskWatch("/") as watch:
            for path in ("/", "/status.json", "/history.json", "/metrics", "/healthz", "/missing"):
                await self.get(path)
        self.assertEqual(watch.total, 0)

    async def test_stop_closes_open_connections_and_the_listener(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        await asyncio.sleep(0.05)
        await self.server.stop()
        self.assertEqual(await asyncio.wait_for(reader.read(-1), 2), b"")
        writer.close()
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", self.port)
        await self.server.stop()                                             # a second stop is harmless


class TokenProtected(ServerCase):
    token = "s3cret-token"

    async def test_data_needs_the_token_but_healthz_does_not(self):
        for path in ("/", "/status.json", "/history.json", "/metrics"):
            with self.subTest(path=path):
                status, headers, body = await self.get(path)
                self.assertEqual(status, 401)
                self.assertIn("Bearer", headers["www-authenticate"])
                self.assertNotIn(b"vTEST", body)
        self.assertEqual((await self.get("/healthz"))[0], 200)

    async def test_wrong_tokens_are_refused(self):
        for supplied in ("", "wrong", "s3cret-token-longer", "s3cret-toke", "S3CRET-TOKEN"):
            with self.subTest(supplied=supplied):
                self.assertEqual((await self.get("/status.json?token=" + supplied))[0], 401)
                self.assertEqual((await self.get("/status.json", f"Authorization: Bearer {supplied}\r\n"))[0], 401)
        self.assertEqual((await self.get("/status.json?token=s3cret-token%20"))[0], 401)      # trailing space matters

    async def test_bearer_header_and_query_parameter_both_work(self):
        self.assertEqual((await self.get("/status.json", "Authorization: Bearer s3cret-token\r\n"))[0], 200)
        self.assertEqual((await self.get("/status.json?token=s3cret-token"))[0], 200)
        self.assertEqual((await self.get("/?token=s3cret-token"))[0], 200)

    async def test_a_wrong_bearer_is_not_rescued_by_a_correct_query(self):
        status, _, _ = await self.get("/status.json?token=s3cret-token", "Authorization: Bearer nope\r\n")
        self.assertEqual(status, 401)

    async def test_the_token_is_never_echoed_back(self):
        for path in ("/?token=s3cret-token", "/status.json?token=s3cret-token", "/metrics?token=s3cret-token"):
            status, headers, body = await self.get(path)
            self.assertEqual(status, 200)
            self.assertNotIn(b"s3cret-token", body)
            self.assertNotIn("s3cret-token", " ".join(headers.values()))


class HostHeader(PalmCase):
    def test_accepts_ips_local_names_and_the_allow_list(self):
        for host in ("127.0.0.1", "127.0.0.1:8080", "192.168.1.5:8080", "[::1]:8080", "[::1]", "localhost", "localhost:8080",
                     "raspberrypi", "raspberrypi:8080", "pi.local", "pi.local:8080", "pi.home.arpa", "nas.lan", ""):
            with self.subTest(host=host):
                self.assertTrue(self.palm.host_allowed(host, []))
        self.assertTrue(self.palm.host_allowed("pi.example.com:443", ["pi.example.com"]))

    def test_rejects_public_names_which_is_the_rebinding_attack(self):
        for host in ("evil.example.com", "evil.example.com:8080", "192.168.1.5.evil.com", "attacker.net", "pi.local.evil.com",
                     ":8080", "EVIL.EXAMPLE.COM"):
            with self.subTest(host=host):
                self.assertFalse(self.palm.host_allowed(host, []))


class HostHeaderOverTheWire(ServerCase):
    async def test_a_rebinding_style_request_is_refused_even_for_healthz(self):
        for path in ("/status.json", "/healthz", "/"):
            self.assertEqual((await self.get(path, host="evil.example.com"))[0], 403)
        self.assertEqual((await self.get("/status.json", host="pi.local:8080"))[0], 200)
        self.assertEqual((await self.get("/status.json", host=""))[0], 200)         # HTTP/1.0 style: no Host


class AllowedHostsSetting(ServerCase):
    allowed_hosts = ["pi.example.com"]

    async def test_configured_names_are_accepted(self):
        self.assertEqual((await self.get("/status.json", host="pi.example.com"))[0], 200)
        self.assertEqual((await self.get("/status.json", host="other.example.com"))[0], 403)


class History(PalmCase):
    def test_rows_are_added_once_per_distinct_reading_and_bounded(self):
        history = self.palm.History(3)
        t = make_telemetry(pv_power=100)
        self.assertTrue(history.add(t, 5, now=1000))
        self.assertFalse(history.add(t, 5, now=1060))                       # same reading again
        for i in range(5):
            history.add(make_telemetry(pv_power=200 + i), None, now=1100 + i)
        self.assertEqual(len(history), 3)                                    # ring: only the newest three
        cols = history.columns()
        self.assertEqual(cols["t"], [1102, 1103, 1104])
        self.assertEqual(cols["pv"], [202, 203, 204])
        self.assertEqual(cols["ev"], [None, None, None])
        for name in self.palm.WEB_HISTORY_FIELDS:
            self.assertEqual(len(cols[name]), 3)

    def test_nothing_is_recorded_without_a_reading(self):
        history = self.palm.History(10)
        self.assertFalse(history.add(None, 100))
        self.assertEqual(history.columns()["t"], [])

    def test_a_day_of_minutes_is_small(self):
        import sys
        history = self.palm.History(24 * 60)
        for i in range(24 * 60):
            history.add(make_telemetry(pv_power=i, consumption=i, soc=i % 100), 1234, now=i)
        self.assertEqual(len(history), 1440)
        size = sum(sys.getsizeof(row) + sum(sys.getsizeof(v) for v in row) for row in history._rows)
        self.assertLess(size, 600_000)                                       # well under 1 MB for 24 h

    def test_minimum_size_is_one(self):
        self.assertEqual(self.palm.History(0)._rows.maxlen, 1)


class SystemStats(PalmCase):
    def uptime_file(self, text):
        path = self.tmp / "uptime"
        path.write_text(text)
        return str(path)

    def test_reads_uptime_and_load_averages(self):
        with mock.patch("os.getloadavg", return_value=(0.123, 0.456, 0.789)), mock.patch("os.cpu_count", return_value=4):
            stats = self.palm.system_stats(self.uptime_file("12345.67 98765.43\n"))
        self.assertEqual(stats, {"uptime_s": 12346, "load1": 0.12, "load5": 0.46, "load15": 0.79, "cpus": 4})

    def test_anything_unavailable_is_none_not_an_error(self):
        with mock.patch("os.getloadavg", side_effect=OSError), mock.patch("os.cpu_count", return_value=None):
            stats = self.palm.system_stats(str(self.tmp / "no-such-file"))
        self.assertEqual(stats, {"uptime_s": None, "load1": None, "load5": None, "load15": None, "cpus": None})
        with mock.patch("os.getloadavg", side_effect=AttributeError):                  # platforms without getloadavg
            self.assertIsNone(self.palm.system_stats(str(self.tmp / "x"))["load1"])

    def test_garbage_in_the_uptime_file_is_ignored(self):
        for text in ("", "   ", "not-a-number 1\n", "\n"):
            with self.subTest(text=text):
                self.assertIsNone(self.palm.system_stats(self.uptime_file(text))["uptime_s"])

    def test_the_real_host_returns_plausible_numbers(self):
        stats = self.palm.system_stats()
        self.assertEqual(set(stats), {"uptime_s", "load1", "load5", "load15", "cpus"})
        if stats["uptime_s"] is not None:
            self.assertGreater(stats["uptime_s"], 0)
        if stats["load1"] is not None:
            self.assertGreaterEqual(stats["load1"], 0)

    def test_reading_system_stats_writes_nothing(self):
        with DiskWatch("/") as watch:
            for _ in range(20):
                self.palm.system_stats()
        self.assertEqual(watch.total, 0)

    def test_system_gauges_are_omitted_when_unknown(self):
        snap = {"system": {"uptime_s": None, "load1": None, "load5": None, "load15": None, "cpus": None}}
        text = self.palm.render_metrics(snap)
        self.assertNotIn("palm_system_", text)


class Metrics(PalmCase):
    def test_exposition_format(self):
        text = self.palm.render_metrics(SNAP)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(text.count("# TYPE palm_battery_state gauge"), 1)      # once per metric, not per label set
        self.assertIn("palm_battery_power_watts 556\n", text)
        self.assertIn("palm_failing_conditions 1\n", text)
        self.assertIn("palm_pvoutput_last_success_timestamp_seconds 1781000000\n", text)
        self.assertIn("palm_battery_paused 0\n", text)                          # booleans become 0/1
        self.assertNotIn("palm_heater_on", text)                                 # unknown (None) is omitted
        for line in text.splitlines():
            self.assertRegex(line, r"^(# (HELP|TYPE) \w+ .+|\w+(\{[^}]*\})? -?[\d.]+)$")

    def test_unknown_values_are_omitted_and_empty_input_is_safe(self):
        self.assertIn("palm_failing_conditions 0", self.palm.render_metrics({}))
        self.assertNotIn("palm_battery_soc_percent", self.palm.render_metrics({"inverter": None}))

    def test_state_labels_mark_exactly_one_state(self):
        text = self.palm.render_metrics(SNAP)
        applied = [ln for ln in text.splitlines() if ln.startswith('palm_battery_state{kind="applied"')]
        self.assertEqual(len(applied), len(self.palm.STATE_COMMANDS))
        self.assertEqual([ln for ln in applied if ln.endswith(" 1")], ['palm_battery_state{kind="applied",state="PEAK_SHAVE"} 1'])

    def test_label_values_are_escaped(self):
        self.assertEqual(self.palm._label('a"b\\c\nd'), 'a\\"b\\\\c\\nd')


class SettingsValidation(PalmCase):
    def errors(self, **web):
        self.cfg.Web.enable = True
        self.cfg.Web.port = 8080                      # the test fixture uses 0 (ephemeral), which is not a valid setting
        for key, value in web.items():
            setattr(self.cfg.Web, key, value)
        return self.palm.validate_settings()

    def test_defaults_are_valid_and_disabled_settings_are_not_checked(self):
        self.assertEqual(self.errors(), [])
        self.cfg.Web.enable = False
        self.cfg.Web.port = "junk"
        self.assertEqual(self.palm.validate_settings(), [])

    def test_bad_values_are_reported(self):
        for key, bad in (("port", 0), ("port", 70000), ("port", "80"), ("port", True), ("host", ""), ("host", 5),
                         ("token", 123), ("history_hours", -1), ("history_hours", 100), ("history_hours", "day"),
                         ("allowed_hosts", "pi.local"), ("allowed_hosts", [""]), ("allowed_hosts", [1])):
            with self.subTest(key=key, bad=bad):
                self.assertTrue(any(f"Web.{key}" in e for e in self.errors(**{key: bad})), (key, bad))

    def test_good_values(self):
        self.assertEqual(self.errors(host="0.0.0.0", port=8080, token="x", history_hours=0, allowed_hosts=["pi.example.com"]), [])

    def test_missing_web_section_means_disabled(self):
        del self.cfg.Web
        self.assertFalse(self.palm.web_settings()["enable"])
        self.assertEqual(self.palm.validate_settings(), [])
        self.assertEqual(self.palm.web_settings()["host"], "127.0.0.1")           # loopback unless asked otherwise

    def test_is_loopback(self):
        for host in ("127.0.0.1", "127.1.2.3", "::1", "localhost", "LOCALHOST"):
            self.assertTrue(self.palm.is_loopback(host), host)
        for host in ("0.0.0.0", "192.168.1.5", "::", "pi.local"):
            self.assertFalse(self.palm.is_loopback(host), host)


class ServiceCase(PalmAsyncCase):
    def setUp(self):
        super().setUp()
        self.client = FakeClient()
        patcher = mock.patch.object(self.palm, "Client", lambda ip, port: self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg.Web.enable = True

    def service(self, **pg):
        for key, value in pg.items():
            setattr(self.cfg.pg, key, value)
        return self.palm.Service(FakeHTTP(default=FakeResponse(json_data={"emeters": [{"power": 0, "is_valid": True}]})))


class Snapshot(ServiceCase):
    async def test_structure_and_values(self):
        svc = self.service()
        svc.inverter.latest = make_telemetry(soc=55, pv_power=1200, consumption=553, batt_power=556, grid_power=-20,
                                             inverter_mins=0)
        svc.shelly.ev_power.set(7000)
        svc.env.co2.set(59)
        svc.env.temp.set(13.0)
        snap = svc.snapshot()
        self.assertEqual(set(snap), {"service", "system", "inverter", "ev", "environment", "control", "pvoutput", "health"})
        self.assertEqual(set(snap["system"]), {"uptime_s", "load1", "load5", "load15", "cpus"})
        self.assertEqual(snap["service"]["version"], self.palm.PALM_VERSION)
        inv = snap["inverter"]
        self.assertEqual((inv["soc_percent"], inv["pv_w"], inv["load_w"], inv["battery_w"], inv["grid_w"]),
                         (55, 1200, 553, 556, -20))
        self.assertTrue(inv["fresh"])
        self.assertEqual(snap["ev"]["power_w"], 7000)
        self.assertEqual((snap["environment"]["co2_g_per_kwh"], snap["environment"]["temp_c"]), (59, 13.0))
        self.assertEqual(snap["control"]["desired"], "ECO_OPTIMISE")
        self.assertIsNone(snap["control"]["applied"])
        self.assertEqual(snap["health"], {"ok": True, "failing": {}})
        json.dumps(snap)                                                       # JSON-safe

    async def test_no_secrets_or_internal_addresses(self):
        self.cfg.PVOutput.enable = True
        svc = self.service()
        svc.inverter.latest = make_telemetry()
        text = json.dumps(svc.snapshot()) + self.palm.render_metrics(svc.snapshot())
        for secret in ("TESTKEY", "12345", "shelly-em.test", "shelly-sw.test", "carbon.test", "weather.test",
                       "pvoutput.example", "127.0.0.1"):
            self.assertNotIn(secret, text)

    async def test_before_any_reading(self):
        snap = self.service().snapshot()
        self.assertIsNone(snap["inverter"])
        self.assertEqual(snap["ev"], {"power_w": None, "age_s": None})
        self.assertEqual(snap["environment"]["co2_g_per_kwh"], None)
        self.assertIsNotNone(self.palm.render_metrics(snap))

    async def test_stale_data_is_flagged_not_hidden(self):
        svc = self.service()
        svc.inverter.latest = make_telemetry(ts=__import__("time").monotonic() - 1000)
        inv = svc.snapshot()["inverter"]
        self.assertFalse(inv["fresh"])
        self.assertGreaterEqual(inv["age_s"], 1000)

    async def test_inverter_clock_offset_wraps_around_midnight(self):
        svc = self.service()
        with mock.patch.object(self.palm, "datetime", wraps=datetime) as fake:
            fake.now.return_value = datetime(2026, 7, 1, 0, 5)
            svc.inverter.latest = make_telemetry(inverter_mins=23 * 60 + 55)      # 23:55 vs 00:05 host
            self.assertEqual(svc.snapshot()["inverter"]["clock_offset_min"], -10)
            svc.inverter.latest = make_telemetry(inverter_mins=65)                # an hour fast
            self.assertEqual(svc.snapshot()["inverter"]["clock_offset_min"], 60)

    async def test_failing_conditions_are_listed_then_cleared(self):
        svc = self.service()
        with self.assertLogs("PALM", "ERROR"):
            self.palm.THROTTLE.failure("inverter_read", 40, "boom")
            self.palm.THROTTLE.failure("inverter_read", 40, "boom")
        health = svc.snapshot()["health"]
        self.assertFalse(health["ok"])
        self.assertEqual(health["failing"]["inverter_read"]["count"], 2)
        self.palm.THROTTLE.recovered("inverter_read", "fine")
        self.assertTrue(svc.snapshot()["health"]["ok"])

    async def test_pvoutput_status_after_a_write(self):
        svc = self.service()
        before = svc.snapshot()["pvoutput"]
        self.assertEqual((before["queued"], before["writes_ok"], before["last_ok"]), (0, 0, None))
        svc.uploader.http = FakeHTTP(default=FakeResponse(200))
        await svc.uploader._post({"d": "20261008", "t": "19:19", "v2": 0})
        after = svc.snapshot()["pvoutput"]
        self.assertEqual(after["writes_ok"], 1)
        self.assertIsNotNone(after["last_ok"])
        self.assertAlmostEqual(after["last_ok_epoch"], datetime.now().timestamp(), delta=5)

    async def test_control_status_reflects_the_state_machine(self):
        svc = self.service()
        S = self.palm.BatteryState
        svc.manager.applied_state, svc.manager.paused, svc.manager.heater_state = S.EV_PROTECT, True, False
        control = svc.snapshot()["control"]
        self.assertEqual((control["applied"], control["paused"], control["heater_on"]), ("EV_PROTECT", True, False))
        self.assertEqual(control["offpeak_window"], "23:30-05:30")


class Integration(ServiceCase):
    async def run_with_hook(self, svc, hook):
        svc.manager.start, svc.manager.shutdown = mock.AsyncMock(), mock.AsyncMock()
        results = {}

        async def sleep_hook():
            results["value"] = await hook()
            svc.stop_event.set()

        svc._sleep_until_next_cycle = sleep_hook
        rc = await svc.run()
        return rc, results.get("value")

    async def test_the_server_runs_with_the_service_and_stops_with_it(self):
        svc = self.service()
        seen = {}

        async def hook():
            seen["port"] = svc.web.port
            return await request(svc.web.port, raw_get("/status.json"))

        with self.assertLogs("PALM", "INFO") as logs:
            rc, (status, _, body) = await self.run_with_hook(svc, hook)
        self.assertEqual(rc, 0)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["service"]["version"], self.palm.PALM_VERSION)
        self.assertIsNotNone(data["inverter"])                                  # the cycle had run
        self.assertTrue(any("Status server listening on http://127.0.0.1:" in line for line in logs.output))
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", seen["port"])           # stopped on shutdown
        self.assertEqual(len(svc.history), 1)

    async def test_it_is_off_unless_enabled(self):
        self.cfg.Web.enable = False
        svc = self.service()

        async def hook():
            return (svc.web, svc.history)

        rc, value = await self.run_with_hook(svc, hook)
        self.assertEqual((rc, value), (0, (None, None)))

    async def test_one_shot_modes_never_start_it(self):
        svc = self.service(once_mode=True)
        with mock.patch("builtins.print"):
            await svc.run()
        self.assertIsNone(svc.web)

    async def test_a_port_that_cannot_be_bound_never_stops_the_control_loop(self):
        blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        self.addAsyncCleanup(lambda: (blocker.close(), blocker.wait_closed())[1])
        self.cfg.Web.port = blocker.sockets[0].getsockname()[1]
        svc = self.service()
        ran = {}

        async def hook():
            ran["cycle"] = svc.inverter.latest is not None
            return (svc.web, svc.history)

        with self.assertLogs("PALM", "ERROR") as logs:
            rc, value = await self.run_with_hook(svc, hook)
        self.assertEqual((rc, value), (0, (None, None)))
        self.assertTrue(ran["cycle"])                                           # inverter polling carried on
        self.assertTrue(any("Status server could not start" in line for line in logs.output))

    async def test_an_open_listener_without_a_token_is_called_out(self):
        for host, token, expect_warning in (("0.0.0.0", "", True), ("0.0.0.0", "tok", False), ("127.0.0.1", "", False)):
            with self.subTest(host=host, token=token):
                self.cfg.Web.host, self.cfg.Web.token = host, token
                svc = self.service()
                with self.assertLogs("PALM", "INFO") as logs:
                    await svc._start_web()
                await svc.web.stop()
                warned = any("without a token" in line for line in logs.output)
                self.assertEqual(warned, expect_warning)

    async def test_history_collects_one_row_per_cycle_and_can_be_switched_off(self):
        svc = self.service()
        await svc._start_web()
        self.addAsyncCleanup(svc.web.stop)
        for _ in range(3):
            await svc.cycle()
        self.assertEqual(len(svc.history), 3)
        columns = svc.history_columns()
        self.assertEqual(len(columns["soc"]), 3)

        self.cfg.Web.history_hours = 0
        off = self.service()
        await off._start_web()
        self.addAsyncCleanup(off.web.stop)
        await off.cycle()
        self.assertIsNone(off.history)
        self.assertEqual(off.history_columns()["t"], [])

    async def test_a_wedged_browser_connection_does_not_stall_the_control_loop(self):
        svc = self.service()
        await svc._start_web()
        self.addAsyncCleanup(svc.web.stop)
        _, writer = await asyncio.open_connection("127.0.0.1", svc.web.port)     # connects, never sends
        started = asyncio.get_running_loop().time()
        await svc.cycle()
        self.assertLess(asyncio.get_running_loop().time() - started, 2.0)
        writer.close()


if __name__ == "__main__":
    unittest.main()
