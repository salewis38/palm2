"""SD-card wear regression tests: what PALM writes to disk, and how often."""
import json
import pathlib
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

from support import DiskWatch, FakeHTTP, FakeResponse, PalmAsyncCase, PalmCase

ROOT = pathlib.Path(__file__).resolve().parent.parent


def today():
    return datetime.now().strftime("%Y%m%d")


def entry(i, day=None):
    return {"d": day or today(), "t": f"{(i * 5 // 60) % 24:02d}:{(i * 5) % 60:02d}", "v2": i}


class SpoolWear(PalmAsyncCase):
    def make(self, default=None, responses=None):
        http = FakeHTTP(responses, default if default is not None else FakeResponse(200))
        return self.palm.PVOutputUploader(http), http

    @property
    def spool(self):
        return self.tmp / "spool.jsonl"

    def lines(self):
        return [json.loads(line) for line in self.spool.read_text().splitlines()]

    async def test_healthy_day_touches_no_files_at_all(self):
        up, _ = self.make()
        with DiskWatch(self.tmp) as watch:
            for i in range(288):
                await up.upload(entry(i))
        self.assertEqual(watch.total, 0)
        self.assertFalse(self.spool.exists())

    async def test_outage_only_appends_one_line_per_failure(self):
        up, _ = self.make(default=FakeResponse(500, "down"))
        with mock.patch.object(self.palm, "atomic_write", wraps=self.palm.atomic_write) as rewrite, \
                DiskWatch(self.tmp) as watch:
            for i in range(288):
                await up.upload(entry(i))
        self.assertEqual(rewrite.call_count, 0)                 # no whole-file rewrites (was 288)
        self.assertEqual((watch.fsyncs, watch.renames, watch.removes), (0, 0, 0))   # was 288 fsyncs/renames
        self.assertEqual(watch.write_opens, 288)                # one small append each
        self.assertEqual([e["v2"] for e in self.lines()], list(range(288)))
        self.assertLess(self.spool.stat().st_size, 80_000)      # about 60 KB, was 8.4 MB written

    async def test_backlog_drain_deletes_the_file_without_rewriting_it(self):
        up, http = self.make(default=FakeResponse(500))
        for i in range(5):
            await up.upload(entry(i))
        http.default = FakeResponse(200)
        with mock.patch.object(self.palm, "atomic_write", wraps=self.palm.atomic_write) as rewrite, \
                DiskWatch(self.tmp) as watch:
            await up.upload(entry(5))
        self.assertEqual(rewrite.call_count, 0)
        self.assertEqual((watch.write_opens, watch.fsyncs, watch.removes), (0, 0, 1))
        self.assertFalse(self.spool.exists())
        self.assertEqual(up._queue, [])

    async def test_partial_drain_rewrites_once_and_never_fsyncs(self):
        up, http = self.make(default=FakeResponse(500))
        for i in range(3):
            await up.upload(entry(i))
        http.responses = [FakeResponse(200), FakeResponse(200)]      # e0, e1 go; e2 hits the 500 default
        with mock.patch.object(self.palm, "atomic_write", wraps=self.palm.atomic_write) as rewrite, \
                DiskWatch(self.tmp) as watch:
            await up.upload(entry(3))
        self.assertEqual(rewrite.call_count, 1)
        self.assertIs(rewrite.call_args.kwargs.get("durable"), False)
        self.assertEqual(watch.fsyncs, 0)
        self.assertEqual([e["v2"] for e in self.lines()], [2, 3])

    async def test_spool_survives_restart_and_keeps_appending(self):
        first, _ = self.make(default=FakeResponse(500))
        for i in range(3):
            await first.upload(entry(i))
        second, _ = self.make(default=FakeResponse(500))
        self.assertEqual(len(second._queue), 3)
        with mock.patch.object(self.palm, "atomic_write", wraps=self.palm.atomic_write) as rewrite:
            await second.upload(entry(3))
        self.assertEqual(rewrite.call_count, 0)                 # clean file: still just an append
        self.assertEqual(len(self.lines()), 4)

    async def test_torn_final_line_is_repaired_on_the_next_change(self):
        self.spool.write_text(json.dumps(entry(0)) + "\n" + json.dumps(entry(1)) + "\n" + '{"d": "2026')   # power cut
        up, _ = self.make(default=FakeResponse(500))
        self.assertEqual(len(up._queue), 2)
        self.assertTrue(up._dirty)
        with mock.patch.object(self.palm, "atomic_write", wraps=self.palm.atomic_write) as rewrite:
            await up.upload(entry(2))
        self.assertEqual(rewrite.call_count, 1)                 # one clean rewrite...
        self.assertTrue(self.spool.read_text().endswith("\n"))
        self.assertEqual([e["v2"] for e in self.lines()], [0, 1, 2])
        self.assertFalse(up._dirty)                             # ...and then appends again
        with mock.patch.object(self.palm, "atomic_write", wraps=self.palm.atomic_write) as rewrite:
            await up.upload(entry(3))
        self.assertEqual(rewrite.call_count, 0)

    async def test_stale_entries_are_dropped_from_memory_and_disk_is_tidied_lazily(self):
        old_day = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        self.spool.write_text("".join(json.dumps(e) + "\n" for e in (entry(0, old_day), entry(1))))
        up, _ = self.make(default=FakeResponse(500))
        self.assertEqual([e["v2"] for e in up._queue], [1])
        self.assertTrue(up._dirty)                              # file differs from memory...
        await up.upload(entry(2))
        self.assertEqual([e["v2"] for e in self.lines()], [1, 2])   # ...so the first change normalises it

    async def test_outage_beyond_the_queue_cap_still_only_appends(self):
        cap = self.palm.SPOOL_MAX_ENTRIES
        up, _ = self.make(default=FakeResponse(500))
        with mock.patch.object(self.palm, "atomic_write", wraps=self.palm.atomic_write) as rewrite, \
                DiskWatch(self.tmp) as watch:
            for i in range(2 * cap):
                await up.upload(entry(i))
        self.assertEqual(rewrite.call_count, 0)                 # was a full rewrite on EVERY upload past the cap
        self.assertEqual(watch.renames, 0)
        self.assertEqual(len(up._queue), cap)                   # memory stays bounded
        with mock.patch.object(self.palm, "atomic_write", wraps=self.palm.atomic_write) as rewrite:
            for i in range(2 * cap, 2 * cap + 50):
                await up.upload(entry(i))
        self.assertEqual(rewrite.call_count, 1)                 # one compaction when the file reached 2x the cap
        self.assertLessEqual(len(self.lines()), cap + 50)
        reloaded = self.palm.PVOutputUploader(None)             # restart: newest `cap` entries survive
        self.assertEqual([e["v2"] for e in reloaded._queue], list(range(cap + 50, 2 * cap + 50)))

    async def test_unwritable_spool_is_survivable_and_logged_once(self):
        (self.tmp / "blocker").write_text("a file, not a directory")
        up, _ = self.make(default=FakeResponse(500))
        up.spool_path = self.tmp / "blocker" / "spool.jsonl"
        with self.assertLogs("PALM", "ERROR") as logs:
            for i in range(5):
                await up.upload(entry(i))
        self.assertEqual(len([line for line in logs.output if "PVOutput spool" in line]), 1)
        self.assertEqual(len(up._queue), 5)                     # nothing lost from memory
        self.assertTrue(up._dirty)
        up.spool_path = self.spool                              # disk recovers
        await up.upload(entry(5))
        self.assertEqual([e["v2"] for e in self.lines()], list(range(6)))
        self.assertFalse(up._dirty)


class FileEncoding(PalmAsyncCase):
    """Every file PALM opens states its encoding, so behaviour never depends on the Pi's locale."""

    async def test_every_open_call_passes_an_explicit_utf8_encoding(self):
        real_open, calls = open, []

        def spy(file, mode="r", *args, **kwargs):
            if str(file).startswith(str(self.tmp)):
                calls.append((str(file), mode, kwargs.get("encoding")))
            return real_open(file, mode, *args, **kwargs)

        up = self.palm.PVOutputUploader(FakeHTTP(default=FakeResponse(500)))
        with mock.patch("builtins.open", spy):
            await up.upload(entry(0))                          # append to the spool
            self.palm.PVOutputUploader(None)                   # load the spool again
            self.palm.acquire_instance_lock().close()          # lock file
        modes = {mode for _, mode, _ in calls}
        self.assertTrue({"a", "r", "w"} <= modes, calls)       # all three paths were exercised
        for path, mode, encoding in calls:
            self.assertEqual(encoding, "utf-8", f"open({path!r}, {mode!r}) has no explicit encoding")


class Directories(PalmCase):
    def test_resolve_dir_precedence(self):
        f = self.palm.resolve_dir
        with mock.patch.dict("os.environ", {"STATE_DIRECTORY": "/var/lib/palm:/other"}):
            self.assertEqual(f("/explicit", "STATE_DIRECTORY", pathlib.Path("/default")), pathlib.Path("/explicit"))
            self.assertEqual(f(None, "STATE_DIRECTORY", pathlib.Path("/default")), pathlib.Path("/var/lib/palm"))
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("STATE_DIRECTORY", None)
            self.assertEqual(f("", "STATE_DIRECTORY", pathlib.Path("/default")), pathlib.Path("/default"))

    def test_lock_file_goes_to_the_runtime_dir_not_the_state_dir(self):
        run, state = self.tmp / "run", self.tmp / "state"
        with mock.patch.object(self.palm, "RUNTIME_DIR", run), mock.patch.object(self.palm, "STATE_DIR", state):
            handle = self.palm.acquire_instance_lock()
        self.addCleanup(handle.close)
        self.assertTrue((run / "palm.lock").exists())
        self.assertFalse(state.exists())                        # persistent storage is not even created

    def test_spool_defaults_to_the_state_dir(self):
        del self.cfg.PVOutput.spool_file
        up = self.palm.PVOutputUploader(None)
        self.assertEqual(up.spool_path, self.tmp / "pvoutput_spool.jsonl")

    def test_non_durable_atomic_write_never_fsyncs(self):
        with DiskWatch(self.tmp) as watch:
            self.palm.atomic_write(self.tmp / "a.txt", "x", durable=False)
            self.assertEqual(watch.fsyncs, 0)
            self.palm.atomic_write(self.tmp / "a.txt", "y")
            self.assertEqual(watch.fsyncs, 1)                   # default stays durable


class UnitFile(unittest.TestCase):
    """Guards the restart-loop and writable-path settings in deploy/palm.service."""

    def setUp(self):
        text = (ROOT / "deploy" / "palm.service").read_text()
        self.lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]

    def test_config_errors_do_not_restart_loop(self):
        self.assertIn("RestartPreventExitStatus=2 3", self.lines)
        self.assertIn("Restart=on-failure", self.lines)
        delay = int(next(ln for ln in self.lines if ln.startswith("RestartSec=")).split("=")[1])
        self.assertGreaterEqual(delay, 30)

    def test_only_state_and_runtime_directories_are_writable(self):
        for line in ("StateDirectory=palm", "RuntimeDirectory=palm", "ProtectSystem=strict",
                     "Environment=PYTHONDONTWRITEBYTECODE=1"):
            self.assertIn(line, self.lines)

    def test_exit_codes_match_the_program(self):
        src = (ROOT / "palm.py").read_text()
        self.assertIn("return 2", src)                          # invalid settings
        self.assertIn("return 3", src)                          # second instance


if __name__ == "__main__":
    unittest.main()
