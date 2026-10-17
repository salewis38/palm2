import logging
import sys
import unittest
from unittest import mock

from support import PalmCase


def run_entry(case, *argv, amain=None, validate=None, lock="ok"):
    """Run palm.entry() with a given argv and the heavy parts stubbed out."""
    async def fake_amain():
        return 0

    patches = [
        mock.patch.object(sys, "argv", ["palm", *argv]),
        mock.patch.object(case.palm, "amain", amain or fake_amain),
        mock.patch.object(case.palm, "validate_settings", return_value=validate or []),
        mock.patch.object(case.palm, "acquire_instance_lock",
                          return_value=mock.MagicMock() if lock == "ok" else None),
    ]
    for p in patches:
        p.start()
        case.addCleanup(p.stop)
    root = logging.getLogger()
    saved = (root.level, list(root.handlers))

    def restore_logging():
        root.handlers[:] = saved[1]
        root.setLevel(saved[0])

    case.addCleanup(restore_logging)
    return case.palm.entry()


class ParseArgs(PalmCase):
    def test_execute_accepts_known_commands_only(self):
        self.assertEqual(self.palm.parse_args(["-x", "pause"]).execute, "pause")
        with self.assertRaises(SystemExit):
            self.palm.parse_args(["-x", "nope"])
        with self.assertRaises(SystemExit):
            self.palm.parse_args(["-x"])

    def test_modes_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self.palm.parse_args(["-t", "-o"])

    def test_every_documented_command_is_known(self):
        for cmd in ("charge_now", "charge_now_soc", "discharge_now", "pause", "end_pause",
                    "play", "set_soc", "get_status"):
            self.assertIn(cmd, self.palm.KNOWN_COMMANDS)


class Entry(PalmCase):
    def test_flags_set_runtime_settings(self):
        run_entry(self, "-x", "play")
        self.assertTrue(self.cfg.pg.once_mode and self.cfg.pg.execute_mode)
        self.assertEqual(self.cfg.pg.mode_cmd, "play")

    def test_test_mode_enables_debug_and_skips_the_instance_lock(self):
        run_entry(self, "-t")
        self.assertTrue(self.cfg.pg.test_mode and self.cfg.pg.debug_mode)
        self.palm.acquire_instance_lock.assert_not_called()

    def test_once_flag(self):
        run_entry(self, "-o")
        self.assertTrue(self.cfg.pg.once_mode)
        self.assertFalse(self.cfg.pg.execute_mode)

    def test_invalid_settings_exit_2_before_touching_hardware(self):
        called = []

        async def amain():
            called.append(1)
            return 0

        with self.assertLogs("PALM", "CRITICAL"):
            rc = run_entry(self, amain=amain, validate=["GE.start_time: bad"])
        self.assertEqual(rc, 2)
        self.assertEqual(called, [])

    def test_second_instance_exits_3(self):
        with self.assertLogs("PALM", "CRITICAL"):
            self.assertEqual(run_entry(self, lock="held"), 3)

    def test_crash_exits_1_so_systemd_restarts_it(self):
        def amain():
            raise RuntimeError("boom")

        with self.assertLogs("PALM", "CRITICAL") as logs:
            rc = run_entry(self, amain=amain)
        self.assertEqual(rc, 1)
        self.assertTrue(any("Global crash" in line for line in logs.output))

    def test_async_crash_also_exits_1(self):
        async def amain():
            raise RuntimeError("late boom")

        with self.assertLogs("PALM", "CRITICAL"):
            self.assertEqual(run_entry(self, amain=amain), 1)

    def test_keyboard_interrupt_exits_130(self):
        def amain():
            raise KeyboardInterrupt

        self.assertEqual(run_entry(self, amain=amain), 130)

    def test_exit_code_of_service_is_propagated(self):
        async def amain():
            return 1

        self.assertEqual(run_entry(self, "-x", "pause", amain=amain), 1)

    def test_lock_handle_is_released(self):
        handle = mock.MagicMock()
        with mock.patch.object(self.palm, "acquire_instance_lock", return_value=handle):
            run_entry_args = ["palm"]
            with mock.patch.object(sys, "argv", run_entry_args), \
                    mock.patch.object(self.palm, "validate_settings", return_value=[]), \
                    mock.patch.object(self.palm, "amain", lambda: _zero()):
                self.palm.entry()
        handle.close.assert_called_once()


async def _zero():
    return 0


@unittest.skipIf(sys.platform == "win32", "flock is POSIX only")
class InstanceLock(PalmCase):
    def test_second_holder_is_refused_until_first_releases(self):
        first = self.palm.acquire_instance_lock()
        self.assertIsNotNone(first)
        self.assertIsNone(self.palm.acquire_instance_lock())
        first.close()
        again = self.palm.acquire_instance_lock()
        self.assertIsNotNone(again)
        again.close()


if __name__ == "__main__":
    unittest.main()
