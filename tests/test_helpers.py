import asyncio
import os
import unittest
from datetime import datetime
from unittest import mock

from support import PalmAsyncCase, PalmCase, palm


class TimeHelpers(unittest.TestCase):
    def test_parse_hhmm_valid(self):
        for text, expected in (("00:00", 0), ("05:30", 330), ("23:59", 1439), (" 7:05 ", 425)):
            with self.subTest(text=text):
                self.assertEqual(palm.parse_hhmm(text), expected)

    def test_parse_hhmm_invalid(self):
        for bad in ("23.30", "24:00", "12:60", "", "noon", None, 1230, "12"):
            with self.subTest(bad=bad), self.assertRaises((ValueError, TypeError)):
                palm.parse_hhmm(bad)

    def test_in_window_normal_and_midnight_spanning(self):
        w = palm.in_window
        self.assertTrue(w(6 * 60, 5 * 60, 7 * 60))
        self.assertFalse(w(8 * 60, 5 * 60, 7 * 60))
        self.assertFalse(w(7 * 60, 5 * 60, 7 * 60))             # end exclusive
        self.assertTrue(w(23 * 60 + 45, 23 * 60 + 30, 5 * 60 + 30))
        self.assertTrue(w(60, 23 * 60 + 30, 5 * 60 + 30))
        self.assertFalse(w(12 * 60, 23 * 60 + 30, 5 * 60 + 30))
        self.assertFalse(w(100, 600, 600))                       # zero-length window

    def test_t_to_hrs_raw(self):
        self.assertEqual(palm.t_to_hrs_raw(0), 0)
        self.assertEqual(palm.t_to_hrs_raw(23 * 60 + 30), 2330)
        self.assertEqual(palm.t_to_hrs_raw(5 * 60 + 5), 505)
        self.assertEqual(palm.t_to_hrs_raw(99999), 2359)          # clamped

    def test_rate_register_clamped(self):
        self.assertEqual(palm.rate_register(2.6), 25)
        self.assertEqual(palm.rate_register(0.0), 0)
        self.assertEqual(palm.rate_register(99), palm.MAX_RATE_REGISTER)

    def test_aligned_expiry(self):
        f = palm.BatteryManager.aligned_expiry
        self.assertEqual(f(datetime(2026, 6, 1, 12, 0, 0)), datetime(2026, 6, 1, 12, 30))
        self.assertEqual(f(datetime(2026, 6, 1, 12, 29, 59)), datetime(2026, 6, 1, 12, 30))
        self.assertEqual(f(datetime(2026, 6, 1, 12, 30, 1)), datetime(2026, 6, 1, 13, 0))
        self.assertEqual(f(datetime(2026, 6, 1, 23, 50, 10)), datetime(2026, 6, 2, 0, 0))   # across midnight


class NumHelper(unittest.TestCase):
    def test_num(self):
        self.assertEqual(palm.num("3.5"), 3.5)
        self.assertEqual(palm.num(7), 7.0)
        for bad in (None, True, "abc", float("nan"), float("inf"), [], {}):
            with self.subTest(bad=bad):
                self.assertIsNone(palm.num(bad))


class BackoffAndReading(unittest.TestCase):
    def test_backoff_grows_caps_and_resets(self):
        b = palm.Backoff(base=10.0, cap=60.0)
        self.assertTrue(b.ready())
        delays = [b.failure() for _ in range(6)]
        self.assertGreaterEqual(delays[0], 9.0)
        self.assertLessEqual(delays[0], 11.0)
        self.assertLess(delays[0], delays[1])
        self.assertLessEqual(max(delays), 66.0)                   # cap + jitter
        self.assertFalse(b.ready())
        b.success()
        self.assertTrue(b.ready())
        self.assertEqual(b.failures, 0)

    def test_reading_freshness(self):
        r = palm.Reading()
        self.assertIsNone(r.get(100))                              # never set
        with mock.patch.object(palm.time, "monotonic", return_value=1000.0):
            r.set(42)
        with mock.patch.object(palm.time, "monotonic", return_value=1005.0):
            self.assertEqual(r.get(10), 42)
        with mock.patch.object(palm.time, "monotonic", return_value=1011.0):
            self.assertIsNone(r.get(10))                           # stale


class AtomicWrite(PalmCase):
    def test_atomic_write_replaces_and_leaves_no_temp_files(self):
        target = self.tmp / "sub" / "state.json"
        palm.atomic_write(target, "one")
        palm.atomic_write(target, "two")
        self.assertEqual(target.read_text(), "two")
        self.assertEqual([p.name for p in target.parent.iterdir()], ["state.json"])


class Spawn(PalmAsyncCase):
    async def test_failed_background_task_is_logged_not_lost(self):
        async def boom():
            raise RuntimeError("kaboom")

        with self.assertLogs("PALM", "ERROR") as logs:
            task = palm.spawn(boom())
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
        self.assertTrue(any("Background task failed" in line for line in logs.output))
        self.assertNotIn(task, palm._background)


@unittest.skipUnless(hasattr(__import__("socket"), "AF_UNIX"), "needs AF_UNIX")
class SdNotify(PalmCase):
    def test_noop_without_socket(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOTIFY_SOCKET", None)
            palm.sd_notify("READY=1")                              # must not raise

    def test_sends_datagram(self):
        import socket
        path = str(self.tmp / "n.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(server.close)
        server.bind(path)
        server.settimeout(2)
        with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": path}):
            palm.sd_notify("WATCHDOG=1")
        self.assertEqual(server.recv(100), b"WATCHDOG=1")

    def test_unreachable_socket_is_swallowed(self):
        with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": str(self.tmp / "nothing.sock")}):
            palm.sd_notify("READY=1")


if __name__ == "__main__":
    unittest.main()
