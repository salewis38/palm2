import json
import time
import unittest
from datetime import datetime, timedelta

from support import FakeHTTP, FakeResponse, PalmAsyncCase, PalmCase, httpx, make_telemetry


def today():
    return datetime.now().strftime("%Y%m%d")


def entry(v2, t="10:00", day=None):
    return {"d": day or today(), "t": t, "v2": v2}


class BuildFields(PalmCase):
    def setUp(self):
        super().setUp()
        self.cfg.GE.batt_capacity, self.cfg.GE.batt_utilisation = 10, 0.9
        self.up = self.palm.PVOutputUploader(None)

    def test_unknown_values_are_omitted_not_zeroed(self):
        t = make_telemetry(pv_power=1234, batt_power=-500)
        f = self.up.build_fields(t, ev_power=None, temp_c=None, co2=None)
        for key in ("v5", "v7", "v9", "v10"):
            self.assertNotIn(key, f)
        self.assertEqual(f["v2"], 1234)
        self.assertEqual((f["v8"], f["v11"], f["b1"]), (0, 500, 500))   # charging 500 W
        self.assertEqual(f["b3"], 9000)

    def test_discharging_battery_mapping(self):
        f = self.up.build_fields(make_telemetry(batt_power=700), None, None, None)
        self.assertEqual((f["v8"], f["v11"], f["b1"]), (700, 0, -700))

    def test_known_values_are_included(self):
        f = self.up.build_fields(make_telemetry(consumption=400), ev_power=3000, temp_c=12.5, co2=150)
        self.assertEqual((f["v7"], f["v5"], f["v9"], f["v10"]), (3000, 12.5, 150, 60000))

    def test_timestamp_comes_from_the_reading(self):
        wall = datetime.now().replace(second=0, microsecond=0)
        f = self.up.build_fields(make_telemetry(wall=wall), None, None, None)
        self.assertEqual((f["d"], f["t"]), (wall.strftime("%Y%m%d"), wall.strftime("%H:%M")))

    def test_daily_energy_total_is_not_uploaded(self):
        """The daily generation total (v1) is deliberately not sent; PVOutput integrates the power samples."""
        for energy in (0, 1000, 25000):
            f = self.up.build_fields(make_telemetry(pv_energy_wh=energy), None, None, None)
            self.assertNotIn("v1", f)

    def test_payload_keys_are_exactly_the_documented_set(self):
        f = self.up.build_fields(make_telemetry(), ev_power=1, temp_c=1.0, co2=1)
        self.assertEqual(list(f), ["d", "t", "v2", "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12",
                                   "b1", "b2", "b3", "b4", "b5"])


class Upload(PalmAsyncCase):
    def make(self, responses=None, default=None):
        http = FakeHTTP(responses, default)
        return self.palm.PVOutputUploader(http), http

    @property
    def spool(self):
        return self.tmp / "spool.jsonl"

    async def test_request_shape_and_secret_handling(self):
        up, http = self.make()
        wall = datetime.now().replace(second=0, microsecond=0)
        fields = up.build_fields(make_telemetry(wall=wall), None, None, None)
        await up.upload(fields)
        method, url, kw = http.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/addstatus.jsp"))
        self.assertEqual(kw["headers"]["X-Pvoutput-Apikey"], "TESTKEY")
        self.assertEqual(kw["headers"]["X-Pvoutput-SystemId"], "12345")
        self.assertNotIn("TESTKEY", kw["content"])                      # key never in the body/URL
        self.assertNotIn("TESTKEY", url)
        self.assertIn("t=" + wall.strftime("%H:%M"), kw["content"])     # literal colon preserved
        self.assertFalse(self.spool.exists())                           # nothing written on the happy path

    async def test_success_is_logged_without_secrets(self):
        up, _ = self.make()
        with self.assertLogs("PALM", "INFO") as logs:
            await up.upload(entry(5))
        self.assertTrue(any("pvoutput.org" in line for line in logs.output))
        self.assertFalse(any("TESTKEY" in line for line in logs.output))

    async def test_server_error_is_spooled_then_replayed_in_order(self):
        up, http = self.make([FakeResponse(500, "oops")])
        await up.upload(entry(111, "10:00"))
        self.assertEqual(len(up._queue), 1)
        self.assertTrue(self.spool.exists())

        http.responses = [FakeResponse(200), FakeResponse(200)]
        await up.upload(entry(222, "10:05"))
        bodies = [c[2]["content"] for c in http.calls[1:]]
        self.assertIn("v2=111", bodies[0])                              # oldest first
        self.assertIn("v2=222", bodies[1])
        self.assertEqual(up._queue, [])
        self.assertFalse(self.spool.exists())                           # spool cleaned up

    async def test_connection_error_is_queued(self):
        up, _ = self.make([httpx.HTTPError("network down")])
        await up.upload(entry(1))
        self.assertEqual(len(up._queue), 1)

    async def test_permanent_rejection_is_dropped_not_retried(self):
        up, http = self.make([FakeResponse(400, "Bad request 400: Moon Powered")])
        with self.assertLogs("PALM", "ERROR"):
            await up.upload(entry(1))
        self.assertEqual(up._queue, [])
        self.assertFalse(self.spool.exists())
        self.assertEqual(len(http.calls), 1)

    async def test_bad_entry_does_not_block_those_behind_it(self):
        up, http = self.make([FakeResponse(500)])
        await up.upload(entry(1, "10:00"))
        http.responses = [FakeResponse(400, "bad"), FakeResponse(200)]
        with self.assertLogs("PALM", "ERROR"):
            await up.upload(entry(2, "10:05"))
        self.assertEqual(up._queue, [])
        self.assertEqual(len(http.calls), 3)

    async def test_rate_limit_pauses_all_uploads(self):
        reset = time.time() + 3600
        up, http = self.make([FakeResponse(403, "Forbidden 403: Exceeded number of requests",
                                           headers={"X-Rate-Limit-Reset": str(reset)})])
        await up.upload(entry(1))
        self.assertEqual(len(up._queue), 1)
        self.assertAlmostEqual(up._blocked_until, reset, delta=1)
        await up.upload(entry(2))
        self.assertEqual(len(http.calls), 1)                            # no hammering while blocked
        self.assertEqual(len(up._queue), 2)

    async def test_rate_header_exhausted_blocks_following_calls(self):
        reset = time.time() + 600
        up, http = self.make([FakeResponse(200, headers={"X-Rate-Limit-Remaining": "0",
                                                         "X-Rate-Limit-Reset": str(reset)})])
        await up.upload(entry(1))
        await up.upload(entry(2))
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(len(up._queue), 1)

    async def test_auth_failure_backs_off_and_keeps_data(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.spool.unlink(missing_ok=True)                      # isolate sub-tests from each other
                up, http = self.make([FakeResponse(status, "Forbidden: Invalid API key")])
                with self.assertLogs("PALM", "ERROR"):
                    await up.upload(entry(1))
                self.assertEqual(len(up._queue), 1)
                self.assertGreater(up._blocked_until, time.time() + 1500)

    async def test_test_mode_sends_nothing(self):
        self.cfg.pg.test_mode = True
        up, http = self.make()
        await up.upload(entry(1))
        self.assertEqual(http.calls, [])
        self.assertEqual(up._queue, [])


class SuccessLogLine(PalmAsyncCase):
    """After every write to PVOutput the original one-line summary is logged at INFO."""

    SAMPLE = {"d": "20261008", "t": "19:19", "v2": 0, "v4": 553, "v5": 13.0, "v6": 234.4, "v7": 2, "v8": 556,
              "v9": 59, "v10": 32627, "v11": 0, "v12": 50.09, "b1": -556, "b2": 55, "b3": 8840,
              "b4": 3246800, "b5": 2747900}

    async def test_line_matches_the_documented_format_exactly(self):
        up = self.palm.PVOutputUploader(FakeHTTP(default=FakeResponse(200)))
        with self.assertLogs("PALM", "INFO") as logs:
            self.assertEqual(await up._post(dict(self.SAMPLE)), "ok")
        self.assertEqual(
            logs.records[0].getMessage(),
            "Data; Write to pvoutput.org; 20261008; 19:19; {'v2': 0, 'v4': 553, 'v5': 13.0, 'v6': 234.4, "
            "'v7': 2, 'v8': 556, 'v9': 59, 'v10': 32627, 'v11': 0, 'v12': 50.09, 'b1': -556, 'b2': 55, "
            "'b3': 8840, 'b4': 3246800, 'b5': 2747900}")
        self.assertEqual(logs.records[0].levelname, "INFO")

    async def test_secrets_are_never_in_the_line(self):
        up = self.palm.PVOutputUploader(FakeHTTP(default=FakeResponse(200)))
        with self.assertLogs("PALM", "INFO") as logs:
            await up._post(dict(self.SAMPLE))
        self.assertNotIn("TESTKEY", logs.output[0])
        self.assertNotIn("12345", logs.output[0])

    async def test_one_line_per_write_including_replayed_entries(self):
        http = FakeHTTP(default=FakeResponse(500))
        up = self.palm.PVOutputUploader(http)
        with self.assertLogs("PALM", "INFO") as logs:
            for i in range(2):
                await up.upload(entry(i, "10:%02d" % i))           # failures: nothing was written
            self.assertFalse([r for r in logs.records if r.getMessage().startswith("Data; Write")])
            http.default = FakeResponse(200)
            await up.upload(entry(2, "10:02"))                     # backlog drains: three writes
        written = [r.getMessage() for r in logs.records if r.getMessage().startswith("Data; Write to pvoutput.org;")]
        self.assertEqual(len(written), 3)
        self.assertTrue(written[0].endswith("{'v2': 0}") and written[2].endswith("{'v2': 2}"))
        self.assertIn("; 10:01; ", written[1])

    async def test_rejected_writes_are_not_reported_as_written(self):
        up = self.palm.PVOutputUploader(FakeHTTP(default=FakeResponse(400, "Bad request")))
        with self.assertLogs("PALM", "INFO") as logs:
            await up._post(dict(self.SAMPLE))
        self.assertFalse([r for r in logs.records if r.getMessage().startswith("Data; Write")])

    async def test_real_payload_from_build_fields_uses_the_same_layout(self):
        up = self.palm.PVOutputUploader(FakeHTTP(default=FakeResponse(200)))
        fields = up.build_fields(make_telemetry(pv_power=0, consumption=553), ev_power=2, temp_c=13.0, co2=59)
        with self.assertLogs("PALM", "INFO") as logs:
            await up._post(fields)
        message = logs.records[0].getMessage()
        self.assertRegex(message, r"^Data; Write to pvoutput\.org; \d{8}; \d\d:\d\d; \{'v2': 0, 'v4': 553, 'v5': 13\.0, ")
        self.assertNotIn("'d':", message)
        self.assertNotIn("'t':", message)
        self.assertNotIn("'v1'", message)

    async def test_real_pipeline_reproduces_the_documented_line_exactly(self):
        """Telemetry -> build_fields -> upload log line, character for character."""
        self.cfg.GE.batt_capacity, self.cfg.GE.batt_utilisation = 10.4, 0.85       # b3 = 8840
        up = self.palm.PVOutputUploader(FakeHTTP(default=FakeResponse(200)))
        telemetry = make_telemetry(wall=datetime(2026, 10, 8, 19, 19), pv_power=0, consumption=553,
                                   line_voltage=234.4, line_frequency=50.09, batt_power=556, soc=55,
                                   e_battery_charge_total_wh=3246800, e_battery_discharge_total_wh=2747900)
        fields = up.build_fields(telemetry, ev_power=2, temp_c=13.0, co2=59)
        with self.assertLogs("PALM", "INFO") as logs:
            await up._post(fields)
        self.assertEqual(
            logs.records[0].getMessage(),
            "Data; Write to pvoutput.org; 20261008; 19:19; {'v2': 0, 'v4': 553, 'v5': 13.0, 'v6': 234.4, "
            "'v7': 2, 'v8': 556, 'v9': 59, 'v10': 32627, 'v11': 0, 'v12': 50.09, 'b1': -556, 'b2': 55, "
            "'b3': 8840, 'b4': 3246800, 'b5': 2747900}")


class Spool(PalmCase):
    def test_load_prunes_corrupt_old_and_non_dict_entries(self):
        old = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        lines = [json.dumps(entry(1)), "{not json", json.dumps(entry(2, day=old)),
                 json.dumps([1, 2]), json.dumps({"t": "10:00"}), "", json.dumps(entry(3, "10:10"))]
        (self.tmp / "spool.jsonl").write_text("\n".join(lines) + "\n")
        up = self.palm.PVOutputUploader(None)
        self.assertEqual([e["v2"] for e in up._queue], [1, 3])

    def test_queue_survives_restart(self):
        first = self.palm.PVOutputUploader(None)
        first._queue = [entry(7)]
        first._persist()
        second = self.palm.PVOutputUploader(None)
        self.assertEqual(second._queue, [entry(7)])

    def test_queue_is_capped_keeping_newest(self):
        entries = [entry(i) for i in range(self.palm.SPOOL_MAX_ENTRIES + 25)]
        pruned = self.palm.PVOutputUploader._prune(entries)
        self.assertEqual(len(pruned), self.palm.SPOOL_MAX_ENTRIES)
        self.assertEqual(pruned[-1]["v2"], self.palm.SPOOL_MAX_ENTRIES + 24)

    def test_unwritable_spool_does_not_raise(self):
        up = self.palm.PVOutputUploader(None)
        up.spool_path = self.tmp / "file.txt" / "spool.jsonl"           # parent is a file
        (self.tmp / "file.txt").write_text("x")
        up._queue = [entry(1)]
        with self.assertLogs("PALM", "ERROR"):
            up._persist()


if __name__ == "__main__":
    unittest.main()
