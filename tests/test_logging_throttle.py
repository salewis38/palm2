import asyncio
import logging
import unittest
from datetime import datetime
from unittest import mock

from support import FakeClient, FakeClock, FakeHTTP, FakeResponse, PalmAsyncCase, PalmCase, make_telemetry

EM_OK = FakeResponse(json_data={"emeters": [{"power": 0, "is_valid": True}]})


class ThrottleUnit(PalmCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.th = self.palm.LogThrottle(interval=900, clock=self.clock)

    def test_first_failure_is_logged_repeats_are_not(self):
        with self.assertLogs("PALM", "INFO") as logs:
            self.assertTrue(self.th.failure("k", logging.ERROR, "boom %s", 1))
            for _ in range(10):
                self.clock.advance(60)
                self.assertFalse(self.th.failure("k", logging.ERROR, "boom %s", 1))
        self.assertEqual(len(logs.output), 1)

    def test_suppressed_repeats_remain_visible_at_debug(self):
        with self.assertLogs("PALM", "DEBUG") as logs:
            self.th.failure("k", logging.ERROR, "boom")
            self.th.failure("k", logging.ERROR, "boom")
        self.assertEqual(len(logs.output), 2)

    def test_summary_line_after_interval_counts_suppressed(self):
        with self.assertLogs("PALM", "INFO") as logs:
            self.th.failure("k", logging.WARNING, "down: %s", "x")
            for _ in range(5):
                self.clock.advance(100)
                self.th.failure("k", logging.WARNING, "down: %s", "x")        # 4 suppressed, then due at 900?
            self.clock.advance(400)
            self.assertTrue(self.th.failure("k", logging.WARNING, "down: %s", "x"))
        self.assertEqual(len(logs.output), 2)
        self.assertIn("still failing", logs.output[1])
        self.assertIn("down: x", logs.output[1])

    def test_recovery_logged_once_with_totals(self):
        self.th.failure("k", logging.ERROR, "boom")
        self.clock.advance(1800)
        self.th.failure("k", logging.ERROR, "boom")
        with self.assertLogs("PALM", "INFO") as logs:
            self.assertTrue(self.th.recovered("k", "fine again"))
            self.assertFalse(self.th.recovered("k", "fine again"))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("after 2 failure", logs.output[0])
        self.assertIn("30 min", logs.output[0])

    def test_recovered_is_silent_when_nothing_failed(self):
        with self.assertNoLogs("PALM", "DEBUG"):
            self.assertFalse(self.th.recovered("never", "x"))

    def test_keys_are_independent(self):
        with self.assertLogs("PALM", "INFO") as logs:
            self.assertTrue(self.th.failure("a", logging.ERROR, "a"))
            self.assertTrue(self.th.failure("b", logging.ERROR, "b"))
        self.assertEqual(len(logs.output), 2)

    def test_traceback_is_attached_to_the_first_report(self):
        with self.assertLogs("PALM", "ERROR") as logs:
            try:
                raise RuntimeError("kaboom")
            except RuntimeError:
                self.th.failure("k", logging.ERROR, "cycle failed", exc_info=True)
        self.assertIn("RuntimeError: kaboom", logs.output[0])

    def test_is_failing_and_forget(self):
        self.assertFalse(self.th.is_failing("k"))
        self.th.failure("k", logging.ERROR, "x")
        self.assertTrue(self.th.is_failing("k"))
        self.th.forget("k")
        self.assertFalse(self.th.is_failing("k"))


class ClockBackoff:
    """Backoff driven by a fake clock so a whole day can be simulated instantly."""

    def __init__(self, palm, clock, base=10.0, cap=300.0):
        self.__dict__.update(base=base, cap=cap, failures=0, not_before=0.0, clock=clock)

    def ready(self):
        return self.clock() >= self.not_before

    def success(self):
        self.failures, self.not_before = 0, 0.0

    def failure(self):
        self.failures += 1
        delay = min(self.cap, self.base * 2 ** (self.failures - 1))
        self.not_before = self.clock() + delay
        return delay


class DayLongOutages(PalmAsyncCase):
    """A bad day must not turn into thousands of journal lines on an SD card."""

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        patcher = mock.patch.object(self.palm, "THROTTLE", self.palm.LogThrottle(clock=self.clock))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_shelly_em_down_all_day(self):
        http = FakeHTTP(default=FakeResponse(500))
        shelly = self.palm.Shelly(http)
        with self.assertLogs("PALM", "INFO") as logs:
            for _ in range(1440):
                await shelly.read_em()
                self.clock.advance(60)
            http.default = EM_OK
            await shelly.read_em()
        self.assertLessEqual(len(logs.output), 100)            # was 1,440
        self.assertIn("restored", logs.output[-1])

    async def test_inverter_down_all_day(self):
        client = FakeClient()
        client.fail_refresh = True
        with mock.patch.object(self.palm, "Client", lambda ip, port: client):
            inv = self.palm.GivEnergyLocal(asyncio.Event())
            inv._backoff = ClockBackoff(self.palm, self.clock)
            with self.assertLogs("PALM", "INFO") as logs:
                for _ in range(1440):
                    await inv.poll()
                    self.clock.advance(60)
                client.fail_refresh = False
                await inv.poll(force=True)
        self.assertLessEqual(len(logs.output), 100)            # was ~290
        self.assertIn("restored", logs.output[-1])

    async def test_pvoutput_unreachable_all_day(self):
        up = self.palm.PVOutputUploader(FakeHTTP(default=httpx_error()))
        with self.assertLogs("PALM", "INFO") as logs:
            for i in range(288):
                await up.upload({"d": datetime.now().strftime("%Y%m%d"), "t": "10:00", "v2": i})
                self.clock.advance(300)
        self.assertLessEqual(len(logs.output), 200)            # was 576

    async def test_skipped_uploads_while_inverter_is_stale(self):
        self.cfg.PVOutput.enable = True
        svc = self.palm.Service(FakeHTTP())
        with self.assertLogs("PALM", "INFO") as logs:
            for i in range(288):
                svc._maybe_upload(datetime(2026, 6, 10, i // 12, (i % 12) * 5 + 4))
                self.clock.advance(300)
        self.assertLessEqual(len(logs.output), 100)            # was 288

    async def test_uploads_resumed_is_reported_once(self):
        self.cfg.PVOutput.enable = True
        svc = self.palm.Service(FakeHTTP())
        svc.uploader.upload = mock.AsyncMock()
        with self.assertLogs("PALM", "INFO") as logs:
            svc._maybe_upload(datetime(2026, 6, 10, 12, 4))
            svc.inverter.latest = make_telemetry()
            svc._maybe_upload(datetime(2026, 6, 10, 12, 9))
        await asyncio.gather(*list(self.palm._background), return_exceptions=True)
        self.assertTrue(any("resumed" in line for line in logs.output))

    async def test_command_that_never_verifies_does_not_spam(self):
        client = FakeClient()
        client.ignore_writes = True                             # e.g. a wrong register name in verification
        with mock.patch.object(self.palm, "Client", lambda ip, port: client):
            inv = self.palm.GivEnergyLocal(asyncio.Event())
            with self.assertLogs("PALM", "INFO") as logs:
                for _ in range(48):                             # retried every 5 min for 4 hours
                    self.assertFalse(await inv.set_mode("charge_now"))
                    self.clock.advance(300)
                client.ignore_writes = False
                self.assertTrue(await inv.set_mode("charge_now"))
        info_plus = [line for line in logs.output]
        self.assertLessEqual(len(info_plus), 45)                # was ~290
        self.assertTrue(any("working again" in line for line in info_plus))
        self.assertTrue(any("Verification FAILED" in line for line in info_plus))

    async def test_repeating_cycle_exception_logs_one_traceback(self):
        svc = self.palm.Service(FakeHTTP())
        svc.manager.start = mock.AsyncMock()
        svc.manager.shutdown = mock.AsyncMock()
        n = {"i": 0}

        async def bad_cycle():
            raise RuntimeError("same bug every minute")

        async def tick():
            n["i"] += 1
            self.clock.advance(60)
            if n["i"] >= 300:
                svc.stop_event.set()

        svc.cycle, svc._sleep_until_next_cycle = bad_cycle, tick
        with self.assertLogs("PALM", "INFO") as logs:
            await svc.run()
        tracebacks = [line for line in logs.output if "Traceback" in line]
        self.assertGreaterEqual(len(tracebacks), 1)             # the first occurrence keeps its traceback
        self.assertLessEqual(len(tracebacks), 21)               # was 300 tracebacks (one per cycle)


def httpx_error():
    from support import httpx
    return httpx.HTTPError("network down")


class ApplyFailures(PalmAsyncCase):
    async def test_reconcile_failures_are_throttled_and_recovery_reported(self):
        from support import FakeInverter, FakeShelly
        clock = FakeClock()
        with mock.patch.object(self.palm, "THROTTLE", self.palm.LogThrottle(clock=clock)):
            inv = FakeInverter(make_telemetry())
            m = self.palm.BatteryManager(inv, FakeShelly(), self.palm.Env(None))
            m.paused = False
            m.desired_state = self.palm.BatteryState.PEAK_SHAVE
            inv.fail = {"discharge_now"}
            with self.assertLogs("PALM", "INFO") as logs:
                for _ in range(40):
                    m._backoff.not_before = 0
                    await m.reconcile()
                    clock.advance(300)
                inv.fail = set()
                m._backoff.not_before = 0
                await m.reconcile()
        self.assertLessEqual(len(logs.output), 25)
        self.assertTrue(any("Applied PEAK_SHAVE after earlier failures" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
