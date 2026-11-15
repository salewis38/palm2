"""The status page's JavaScript, run in Node against a fake DOM and the real Service snapshot/history output.

Skipped when Node is not installed (GitHub's Ubuntu runners have it)."""
import asyncio
import json
import pathlib
import shutil
import subprocess
import unittest

import palm_gui
from support import make_telemetry
from test_web import ServiceCase

NODE = shutil.which("node")
HARNESS = pathlib.Path(__file__).with_name("web_page_harness.js")


def run_page(page, status, history, search="", fail_fetch=0):
    payload = {"page": page, "status": status, "history": history, "search": search, "failFetch": fail_fetch}
    proc = subprocess.run([NODE, str(HARNESS)], input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


async def run_page_async(*args, **kwargs):
    """Run Node in a worker thread so the test's event loop is never blocked."""
    return await asyncio.to_thread(run_page, *args, **kwargs)


def on_the_wire(value):
    return json.loads(json.dumps(value))                      # exactly what the browser would receive


@unittest.skipUnless(NODE, "node is not installed")
class PageScript(ServiceCase):
    def fill(self, svc, rows=30):
        svc.inverter.latest = make_telemetry(soc=55, pv_power=840, consumption=553, batt_power=556, grid_power=-20,
                                             inverter_mins=0)
        svc.shelly.ev_power.set(700)
        svc.env.co2.set(59)
        svc.env.temp.set(13.0)
        svc.history = self.palm.History(24 * 60)
        for i in range(rows):
            t = make_telemetry(soc=40 + i, pv_power=100 * (i % 9), consumption=300 + i, batt_power=(-1) ** i * 200,
                               grid_power=50 - i)
            svc.history.add(t, 700 if i > 10 else None, now=1_780_000_000 + 60 * i)

    async def render(self, svc, **kw):
        return await run_page_async(palm_gui.PAGE, on_the_wire(svc.snapshot()), on_the_wire(svc.history_columns()), **kw)

    async def test_the_real_server_output_renders_completely(self):
        svc = self.service()
        self.fill(svc)
        out = await self.render(svc)
        self.assertEqual(out["ver"], self.palm.PALM_VERSION)
        self.assertTrue(out["stamp"].startswith("Updated"), out["stamp"])
        self.assertEqual(out["stampClass"], "")
        cards = " | ".join(out["cards"])
        for expected in ("Battery", "55", "Solar", "840", "House load", "553", "Battery discharging", "556",
                         "EV charger", "700", "Grid carbon", "59", "Outside", "13"):
            self.assertIn(expected, cards)
        control = " ".join(out["control"])
        for expected in ("Wanted state", "ECO_OPTIMISE", "Applied state", "Overnight window", "23:30-05:30",
                         "PVOutput", "Running for"):
            self.assertIn(expected, control)
        self.assertEqual([h["text"] for h in out["health"]], ["All systems normal"])
        self.assertEqual(out["health"][0]["cls"], "ok")
        self.assertEqual(out["charts"], 3)                                  # power, charge, EV
        self.assertGreaterEqual(out["svgs"], 3)
        self.assertGreaterEqual(out["paths"], 6)

    async def test_the_token_is_carried_on_every_request(self):
        svc = self.service()
        self.fill(svc)
        out = await self.render(svc, search="?token=abc")
        self.assertEqual(out["fetched"], ["/status.json?token=abc", "/history.json?token=abc"])

    async def test_battery_label_follows_the_direction_of_flow(self):
        svc = self.service()
        self.fill(svc)
        svc.inverter.latest = make_telemetry(batt_power=-300, grid_power=-250)
        cards = " | ".join((await self.render(svc))["cards"])
        self.assertIn("Battery charging", cards)
        self.assertIn("Grid export", cards)

    async def test_before_the_first_reading_nothing_breaks(self):
        svc = self.service()
        svc.history = self.palm.History(60)
        out = await self.render(svc)
        self.assertEqual(out["stampClass"], "")
        self.assertEqual(out["cards"], [])
        self.assertEqual(out["health"], [{"cls": "bad", "text": "No inverter reading yet"}])
        self.assertEqual(out["charts"], 1)                                  # "History is still collecting..."

    async def test_stale_data_and_failing_conditions_are_shown_as_problems(self):
        svc = self.service()
        self.fill(svc)
        svc.inverter.latest.ts -= 1000
        with self.assertLogs("PALM", "ERROR"):
            self.palm.THROTTLE.failure("inverter_read", 40, "boom")
            self.palm.THROTTLE.failure("cmd:play", 40, "boom")
        out = await self.render(svc)
        texts = [h["text"] for h in out["health"]]
        self.assertTrue(any(t.startswith("Inverter data is stale") for t in texts), texts)
        self.assertTrue(any(t.startswith("inverter_read: failing for") for t in texts), texts)
        self.assertTrue(any(t.startswith("cmd:play: failing for") for t in texts), texts)
        self.assertTrue(all(h["cls"] == "bad" for h in out["health"]))

    async def test_hostile_text_is_never_turned_into_markup(self):
        svc = self.service()
        self.fill(svc)
        status = on_the_wire(svc.snapshot())
        evil = '<img src=x onerror=alert(1)>'
        status["health"]["failing"][evil] = {"since_s": 5, "count": 1}
        status["control"]["desired"] = '<script>alert(2)</script>'
        out = await run_page_async(palm_gui.PAGE, status, on_the_wire(svc.history_columns()))
        self.assertNotIn("img", out["tags"])
        self.assertNotIn("script", out["tags"])
        self.assertTrue(any(evil in h["text"] for h in out["health"]))          # shown as plain text
        self.assertIn("<script>alert(2)</script>", " ".join(out["control"]))

    async def test_an_empty_or_odd_history_never_breaks_the_charts(self):
        svc = self.service()
        self.fill(svc)
        status = on_the_wire(svc.snapshot())
        base = on_the_wire(svc.history_columns())
        cases = {
            "empty": {**base, **{k: [] for k in ("t", "pv", "load", "grid", "battery", "soc", "ev")}},
            "one point": {k: (v[:1] if isinstance(v, list) else v) for k, v in base.items()},
            "all flat": {**base, **{k: [5] * len(base["t"]) for k in ("pv", "load", "grid", "battery", "soc")}},
            "all gaps": {**base, **{k: [None] * len(base["t"]) for k in ("pv", "load", "grid", "battery", "soc", "ev")}},
            "gaps inside": {**base, "pv": [None if i % 4 == 0 else v for i, v in enumerate(base["pv"])]},
            "no ev data": {**base, "ev": [None] * len(base["t"])},
        }
        for name, history in cases.items():
            with self.subTest(case=name):
                out = await run_page_async(palm_gui.PAGE, status, history)
                self.assertEqual(out["stampClass"], "", out["stamp"])
                self.assertGreaterEqual(out["charts"], 1)
        self.assertEqual((await run_page_async(palm_gui.PAGE, status, cases["no ev data"]))["charts"], 2)

    async def system_section(self, system, **kw):
        svc = self.service()
        self.fill(svc)
        status = on_the_wire(svc.snapshot())
        if system is None:
            del status["system"]
        else:
            status["system"] = system
        out = await run_page_async(palm_gui.PAGE, status, on_the_wire(svc.history_columns()), **kw)
        self.assertEqual(out["stampClass"], "", out["stamp"])                       # rendered without error
        return out["system"]

    async def test_the_real_snapshot_includes_system_stats_on_the_page(self):
        svc = self.service()
        self.fill(svc)
        out = await self.render(svc)
        self.assertEqual(out["system"][0], "System uptime")
        self.assertEqual(out["system"][2], "Load average (1 / 5 / 15 min)")

    async def test_system_stats_are_formatted(self):
        rows = await self.system_section({"uptime_s": 90000, "load1": 0.5, "load5": 0.25, "load15": 0.1, "cpus": 4})
        self.assertEqual(rows, ["System uptime", "25.0 h", "Load average (1 / 5 / 15 min)", "0.50 / 0.25 / 0.10 (4 cores)"])
        rows = await self.system_section({"uptime_s": 432000, "load1": 0.0, "load5": 0.0, "load15": 0.0, "cpus": 1})
        self.assertEqual(rows[1], "5.0 days")
        self.assertEqual(rows[3], "0.00 / 0.00 / 0.00 (1 core)")
        rows = await self.system_section({"uptime_s": 600, "load1": 0.5, "load5": 0.5, "load15": 0.5, "cpus": None})
        self.assertEqual((rows[1], rows[3]), ("10 min", "0.50 / 0.50 / 0.50"))

    async def test_a_busy_host_is_flagged_only_when_load_exceeds_the_cores(self):
        busy = await self.system_section({"uptime_s": 1000, "load1": 4.2, "load5": 2.0, "load15": 1.0, "cpus": 4})
        self.assertTrue(busy[3].endswith("(4 cores) - high"), busy)
        quiet = await self.system_section({"uptime_s": 1000, "load1": 3.9, "load5": 2.0, "load15": 1.0, "cpus": 4})
        self.assertNotIn("high", quiet[3])

    async def test_unavailable_system_stats_show_a_dash_and_never_break_the_page(self):
        rows = await self.system_section({"uptime_s": None, "load1": None, "load5": None, "load15": None, "cpus": None})
        self.assertEqual(rows, ["System uptime", "-", "Load average (1 / 5 / 15 min)", "-"])
        partial = await self.system_section({"uptime_s": 5, "load1": 0.1, "load5": None, "load15": 0.1, "cpus": 2})
        self.assertEqual(partial[3], "-")                                           # all three or none
        self.assertEqual(await self.system_section(None), [])                       # a server without the field

    async def test_a_refused_request_is_reported_on_the_page(self):
        svc = self.service()
        self.fill(svc)
        out = await self.render(svc, fail_fetch=401)
        self.assertEqual(out["stampClass"], "bad")
        self.assertEqual(out["stamp"], "Connection lost: HTTP 401")


if __name__ == "__main__":
    unittest.main()
