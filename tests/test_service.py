import asyncio
import contextlib
import io
import sys
import time
import unittest
from datetime import datetime
from unittest import mock

from support import FakeClient, FakeHTTP, FakeResponse, PalmAsyncCase, make_telemetry

EM_OK = FakeResponse(json_data={"emeters": [{"power": 0, "is_valid": True}]})


class ServiceCase(PalmAsyncCase):
    def setUp(self):
        super().setUp()
        self.client = FakeClient()
        patcher = mock.patch.object(self.palm, "Client", lambda ip, port: self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def service(self, **pg):
        for key, value in pg.items():
            setattr(self.cfg.pg, key, value)
        return self.palm.Service(FakeHTTP(default=EM_OK))

    async def cycle_quietly(self, svc):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            await svc.cycle()
        return buf.getvalue()


class OneShotModes(ServiceCase):
    async def test_once_mode_reports_and_never_commands_the_inverter(self):
        svc = self.service(once_mode=True)
        self.assertFalse(svc.service_mode)
        out = await self.cycle_quietly(svc)
        self.assertIn("Cycle", out)
        self.assertIn("soc", out)
        self.assertEqual(self.client.executed, [])
        self.assertEqual(svc.exit_code, 0)

    async def test_once_mode_with_unreachable_inverter_exits_nonzero(self):
        svc = self.service(once_mode=True)
        self.client.fail_refresh = True
        with self.assertLogs("PALM", "ERROR"):
            await self.cycle_quietly(svc)
        self.assertEqual(svc.exit_code, 1)

    async def test_execute_mode_success(self):
        svc = self.service(once_mode=True, execute_mode=True, mode_cmd="pause")
        await self.cycle_quietly(svc)
        self.assertIn(("set_battery_pause_mode", (3,)), self.client.executed)
        self.assertEqual(svc.exit_code, 0)

    async def test_execute_mode_failure_sets_exit_code(self):
        svc = self.service(once_mode=True, execute_mode=True, mode_cmd="pause")
        self.client.ignore_writes = True
        with self.assertLogs("PALM", "ERROR"):
            await self.cycle_quietly(svc)
        self.assertEqual(svc.exit_code, 1)


class Uploading(ServiceCase):
    def setUp(self):
        super().setUp()
        self.cfg.PVOutput.enable = True

    async def drain(self):
        if self.palm._background:
            await asyncio.gather(*list(self.palm._background), return_exceptions=True)

    async def test_uploads_only_on_the_fifth_minute_slot_and_once_per_slot(self):
        svc = self.service()
        svc.inverter.latest = make_telemetry()
        svc.uploader.upload = mock.AsyncMock()
        svc._maybe_upload(datetime(2026, 6, 10, 12, 3))
        svc._maybe_upload(datetime(2026, 6, 10, 12, 5))
        await self.drain()
        svc.uploader.upload.assert_not_called()
        svc._maybe_upload(datetime(2026, 6, 10, 12, 4, 5))
        svc._maybe_upload(datetime(2026, 6, 10, 12, 4, 40))             # same slot again
        await self.drain()
        self.assertEqual(svc.uploader.upload.await_count, 1)
        svc._maybe_upload(datetime(2026, 6, 10, 12, 9))
        await self.drain()
        self.assertEqual(svc.uploader.upload.await_count, 2)

    async def test_stale_inverter_data_is_never_uploaded(self):
        svc = self.service()
        svc.inverter.latest = make_telemetry(ts=time.monotonic() - 1000)
        svc.uploader.upload = mock.AsyncMock()
        with self.assertLogs("PALM", "WARNING"):
            svc._maybe_upload(datetime(2026, 6, 10, 12, 4))
        await self.drain()
        svc.uploader.upload.assert_not_called()

    async def test_disabled_and_one_shot_modes_do_not_upload(self):
        svc = self.service()
        svc.inverter.latest = make_telemetry()
        svc.uploader.upload = mock.AsyncMock()
        self.cfg.PVOutput.enable = False
        svc._maybe_upload(datetime(2026, 6, 10, 12, 4))
        self.cfg.PVOutput.enable = True
        svc.service_mode = False
        svc._maybe_upload(datetime(2026, 6, 10, 12, 14))
        await self.drain()
        svc.uploader.upload.assert_not_called()


class Housekeeping(ServiceCase):
    async def test_env_refresh_is_throttled(self):
        self.cfg.CarbonIntensity.enable = True
        svc = self.service()
        svc.env.update_co2 = mock.AsyncMock()
        await svc._update_env()
        await svc._update_env()                                         # within the refresh interval
        await asyncio.gather(*list(self.palm._background), return_exceptions=True)
        self.assertEqual(svc.env.update_co2.await_count, 1)

    async def test_env_is_awaited_in_one_shot_mode(self):
        self.cfg.CarbonIntensity.enable = True
        svc = self.service(once_mode=True)
        svc.env.update_co2 = mock.AsyncMock()
        await svc._update_env()
        svc.env.update_co2.assert_awaited_once()                        # no fire-and-forget

    async def test_clock_drift_is_reported(self):
        svc = self.service()
        now = datetime(2026, 7, 1, 12, 0)
        svc.inverter.latest = make_telemetry(inverter_mins=11 * 60)     # an hour behind, e.g. missed BST
        with self.assertLogs("PALM", "WARNING") as logs:
            svc._check_clock(now)
        self.assertTrue(any("clock" in line for line in logs.output))

    async def test_clock_check_wraps_around_midnight(self):
        svc = self.service()
        svc.inverter.latest = make_telemetry(inverter_mins=1439)
        with self.assertNoLogs("PALM", "WARNING"):                      # 23:59 vs 00:00 is a 1 min difference
            svc._check_clock(datetime(2026, 7, 1, 0, 0))


@unittest.skipIf(sys.platform == "win32", "uses POSIX signal handlers")
class RunLoop(ServiceCase):
    async def test_cycle_exception_does_not_kill_the_service(self):
        svc = self.service()
        svc.manager.start = mock.AsyncMock()
        svc.manager.shutdown = mock.AsyncMock()
        runs = {"n": 0}

        async def bad_cycle():
            runs["n"] += 1
            raise RuntimeError("boom")

        async def stop_instead_of_sleeping():
            svc.stop_event.set()

        svc.cycle = bad_cycle
        svc._sleep_until_next_cycle = stop_instead_of_sleeping
        with self.assertLogs("PALM", "ERROR") as logs:
            rc = await svc.run()
        self.assertEqual(rc, 0)
        self.assertEqual(runs["n"], 1)
        self.assertTrue(any("Unhandled error" in line for line in logs.output))
        svc.manager.start.assert_awaited_once()
        svc.manager.shutdown.assert_awaited_once()                      # safe state restored on exit

    async def test_one_shot_modes_do_not_start_or_shut_down_the_manager(self):
        svc = self.service(once_mode=True)
        svc.manager.start = mock.AsyncMock()
        svc.manager.shutdown = mock.AsyncMock()
        await self.cycle_quietly(svc)                                    # just ensure cycle works
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = await svc.run()
        self.assertEqual(rc, 0)
        svc.manager.start.assert_not_awaited()
        svc.manager.shutdown.assert_not_awaited()                        # -x pause must stay paused

    async def test_loop_counter_and_watchdog(self):
        svc = self.service()
        svc.manager.start = mock.AsyncMock()
        svc.manager.shutdown = mock.AsyncMock()
        svc.cycle = mock.AsyncMock()

        async def stop_after_sleep():
            svc.stop_event.set()

        svc._sleep_until_next_cycle = stop_after_sleep
        with mock.patch.object(self.palm, "sd_notify") as notify:
            await svc.run()
        sent = [c.args[0] for c in notify.call_args_list]
        self.assertEqual(sent[0], "READY=1")
        self.assertIn("WATCHDOG=1", sent)
        self.assertIn("STOPPING=1", sent)
        self.assertEqual(self.cfg.pg.loop_counter, svc.loop_counter)


if __name__ == "__main__":
    unittest.main()
