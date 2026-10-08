#!/usr/bin/env python3
"""Measure how much PALM writes to disk and to the log in a simulated day. No hardware or network needed.

    python tools/measure_wear.py                       # measure ./palm.py
    PALM_MODULE=palm_old python tools/measure_wear.py  # measure another version on PYTHONPATH, to compare
    python tools/measure_wear.py --json out.json

Scenarios run against the fakes in tests/support.py. "Log lines" are INFO and above, i.e. what a default
install sends to the journal. The same scenarios are asserted (with thresholds) in tests/test_wear.py and
tests/test_logging_throttle.py, so a regression fails CI.
"""
import argparse
import asyncio
import json
import logging
import os
import pathlib
import sys
import tempfile
from datetime import datetime
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tests"))
import support as S  # noqa: E402  (installs the fakes, then imports the PALM module)

palm = S.palm
RESULTS = {}


class LineCounter(logging.Handler):
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")

    def __init__(self):
        super().__init__(logging.INFO)               # what a default (INFO) install would journal
        self.lines = self.bytes = 0

    def emit(self, record):
        self.lines += 1
        self.bytes += len(self.fmt.format(record).encode()) + 1


lc = LineCounter()
palm.logger.addHandler(lc)
palm.logger.setLevel(logging.DEBUG)
palm.logger.propagate = False

written = {"bytes": 0, "rewrites": 0, "appends": 0}
_real_atomic = palm.atomic_write


def counting_atomic(path, text, *a, **k):
    written["bytes"] += len(text.encode())
    written["rewrites"] += 1
    return _real_atomic(path, text, *a, **k)


palm.atomic_write = counting_atomic
if hasattr(palm.PVOutputUploader, "_append"):
    _real_append = palm.PVOutputUploader._append

    def counting_append(self, entry):
        written["bytes"] += len(json.dumps(entry)) + 1
        written["appends"] += 1
        return _real_append(self, entry)

    palm.PVOutputUploader._append = counting_append


class ClockBackoff:
    def __init__(self, clock, base=10.0, cap=300.0):
        self.base, self.cap, self.failures, self.not_before, self.clock = base, cap, 0, 0.0, clock

    def ready(self):
        return self.clock() >= self.not_before

    def success(self):
        self.failures, self.not_before = 0, 0.0

    def failure(self):
        self.failures += 1
        delay = min(self.cap, self.base * 2 ** (self.failures - 1))
        self.not_before = self.clock() + delay
        return delay


class Scenario:
    def __init__(self, name):
        self.name = name
        self.dir = tempfile.mkdtemp(prefix="wear")
        self.cfg = S.make_settings()
        self.cfg.PVOutput.spool_file = os.path.join(self.dir, "spool.jsonl")
        self.cfg.PVOutput.enable = True
        self.clock = S.FakeClock()
        self.patches = [mock.patch.object(palm, n, v, create=True) for n, v in (
            ("stgs", self.cfg), ("STATE_DIR", pathlib.Path(self.dir)), ("RUNTIME_DIR", pathlib.Path(self.dir)),
            ("VERIFY_DELAY_S", 0.0), ("PAUSE_SETTLE_S", 0.0))]
        if hasattr(palm, "LogThrottle"):
            self.patches.append(mock.patch.object(palm, "THROTTLE", palm.LogThrottle(clock=self.clock)))
        self.watch = S.DiskWatch(self.dir)

    def __enter__(self):
        for p in self.patches:
            p.start()
        written.update(bytes=0, rewrites=0, appends=0)
        lc.lines = lc.bytes = 0
        self.watch.__enter__()
        return self

    def __exit__(self, *exc):
        self.watch.__exit__()
        spool = pathlib.Path(self.cfg.PVOutput.spool_file)
        RESULTS[self.name] = dict(
            file_opens=self.watch.write_opens, fsyncs=self.watch.fsyncs, renames=self.watch.renames,
            kb_written=round(written["bytes"] / 1024, 1), log_lines=lc.lines, log_kb=round(lc.bytes / 1024, 1),
            spool_kb=round(spool.stat().st_size / 1024, 1) if spool.exists() else 0.0)
        for p in reversed(self.patches):
            p.stop()
        return False


def entry(i):
    wall = datetime.now().replace(hour=(i * 5 // 60) % 24, minute=(i * 5) % 60, second=0, microsecond=0)
    return palm.PVOutputUploader(None).build_fields(S.make_telemetry(wall=wall, pv_power=1234), 0, 12.3, 180)


async def run():
    with Scenario("A  healthy day (1440 cycles, 288 uploads)") as sc:
        client = S.FakeClient()
        with mock.patch.object(palm, "Client", lambda ip, port: client):
            svc = palm.Service(S.FakeHTTP(default=S.FakeResponse(json_data={"emeters": [{"power": 0, "is_valid": True}]})))
            for _ in range(1440):
                await svc.cycle()
        up = palm.PVOutputUploader(S.FakeHTTP(default=S.FakeResponse(200)))
        for i in range(288):
            await up.upload(entry(i))

    for n, label in ((288, "B  PVOutput down, 288 uploads (1 day)"), (600, "B2 PVOutput down, 600 uploads (cap hit)")):
        with Scenario(label) as sc:
            up = palm.PVOutputUploader(S.FakeHTTP(default=S.FakeResponse(500, "down")))
            for i in range(n):
                await up.upload(entry(i))
                sc.clock.advance(300)

    with Scenario("C1 Shelly EM unreachable, 1440 polls") as sc:
        sh = palm.Shelly(S.FakeHTTP(default=S.FakeResponse(500)))
        for _ in range(1440):
            await sh.read_em()
            sc.clock.advance(60)

    with Scenario("C2 inverter unreachable, 1440 polls") as sc:
        client = S.FakeClient()
        client.fail_refresh = True
        with mock.patch.object(palm, "Client", lambda ip, port: client):
            inv = palm.GivEnergyLocal(asyncio.Event())
            inv._backoff = ClockBackoff(sc.clock)
            for _ in range(1440):
                await inv.poll()
                sc.clock.advance(60)

    with Scenario("C3 upload skipped (stale inverter), 288 slots") as sc:
        svc = palm.Service(S.FakeHTTP())
        for i in range(288):
            svc._maybe_upload(datetime(2026, 6, 10, i // 12, (i % 12) * 5 + 4))
            sc.clock.advance(300)

    with Scenario("C4 command never verifies, 48 retries (4 h)") as sc:
        client = S.FakeClient()
        client.ignore_writes = True
        with mock.patch.object(palm, "Client", lambda ip, port: client):
            inv = palm.GivEnergyLocal(asyncio.Event())
            for _ in range(48):
                await inv.set_mode("charge_now")
                sc.clock.advance(300)

    with Scenario("C5 same exception every cycle, 1440 cycles") as sc:
        svc = palm.Service(S.FakeHTTP())
        svc.manager.start = mock.AsyncMock()
        svc.manager.shutdown = mock.AsyncMock()
        n = {"i": 0}

        async def bad_cycle():
            raise RuntimeError("same bug every minute")

        async def tick():
            n["i"] += 1
            sc.clock.advance(60)
            if n["i"] >= 1440:
                svc.stop_event.set()

        svc.cycle, svc._sleep_until_next_cycle = bad_cycle, tick
        await svc.run()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    args = ap.parse_args()
    asyncio.run(run())
    print(f"PALM {getattr(palm, 'PALM_VERSION', '?')}   (per simulated day; log = INFO and above)\n")
    print(f"{'scenario':<52}{'file opens':>11}{'fsyncs':>8}{'renames':>9}{'KB written':>12}{'log lines':>11}{'log KB':>8}")
    for name, r in RESULTS.items():
        print(f"{name:<52}{r['file_opens']:>11}{r['fsyncs']:>8}{r['renames']:>9}{r['kb_written']:>12}"
              f"{r['log_lines']:>11}{r['log_kb']:>8}")
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps({"version": getattr(palm, "PALM_VERSION", "?"), "results": RESULTS}, indent=2))


if __name__ == "__main__":
    main()
