"""Shared test support: hardware stubs, fake settings and fakes.

Importing this module (before the PALM module) installs:
  * a stub `givenergy_modbus` (tests never talk to real hardware),
  * a stub `httpx` ONLY if the real one is not installed,
  * a fake `palm_settings` (so a developer's real settings/secrets are never used).
"""
from __future__ import annotations

import importlib
import os
import pathlib
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODULE_NAME = os.environ.get("PALM_MODULE", "palm")


def make_settings() -> types.ModuleType:
    mod = types.ModuleType("palm_settings")
    mod.pg = SimpleNamespace(test_mode=False, debug_mode=False, once_mode=False,
                             execute_mode=False, mode_cmd="", loop_counter=0)
    mod.GE = SimpleNamespace(
        local_ip="127.0.0.1", local_port=8899, start_time="23:30", end_time="05:30",
        charge_rate=2.6, discharge_rate=2.6, batt_capacity=9.5, batt_utilisation=0.9,
        ev_power_threshold=1500, winter=[11, 12, 1, 2], shoulder=[3, 10], pm_export_start="17:00")
    mod.PVOutput = SimpleNamespace(enable=False, url="https://pvoutput.example/service/r2",
                                   key="TESTKEY", sid="12345")
    mod.Shelly = SimpleNamespace(em0_url="http://shelly-em.test/status", sw1_url="http://shelly-sw.test")
    mod.CarbonIntensity = SimpleNamespace(enable=False, url="https://carbon.test/regional/postcode",
                                          PostCode="AB1")
    mod.OpenWeatherMap = SimpleNamespace(enable=False, url="https://weather.test/data/3.0",
                                         payload={"lat": 0, "lon": 0})
    return mod


def _install_stubs() -> None:
    try:
        import httpx  # noqa: F401
    except ImportError:  # local runs without network
        stub = types.ModuleType("httpx")

        class HTTPError(Exception):
            pass

        class HTTPStatusError(HTTPError):
            pass

        class Timeout:
            def __init__(self, *a, **k):
                pass

        class AsyncHTTPTransport:
            def __init__(self, *a, **k):
                pass

        class AsyncClient:
            pass

        stub.HTTPError, stub.HTTPStatusError = HTTPError, HTTPStatusError
        stub.Timeout, stub.AsyncHTTPTransport, stub.AsyncClient = Timeout, AsyncHTTPTransport, AsyncClient
        sys.modules["httpx"] = stub

    pkg = types.ModuleType("givenergy_modbus")
    pkg.__path__ = []
    client_pkg = types.ModuleType("givenergy_modbus.client")
    client_pkg.__path__ = []
    client_mod = types.ModuleType("givenergy_modbus.client.client")

    class Client:  # replaced per-test via mock.patch
        def __init__(self, *a, **k):
            raise AssertionError("Real Modbus client must never be created in tests")

    client_mod.Client = Client
    sys.modules.update({"givenergy_modbus": pkg, "givenergy_modbus.client": client_pkg,
                        "givenergy_modbus.client.client": client_mod})
    sys.modules["palm_settings"] = make_settings()


_install_stubs()
palm = importlib.import_module(MODULE_NAME)
import httpx  # noqa: E402  (real or stub, whichever _install_stubs left in place)


# --------------------------------------------------------------------------- #
# Base classes
# --------------------------------------------------------------------------- #
class PalmMixin:
    """Per-test isolation: fresh settings, temp state dir, no real waiting."""

    def setUp(self):
        super().setUp()
        self.palm = palm
        self.cfg = make_settings()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.cfg.PVOutput.spool_file = str(self.tmp / "spool.jsonl")
        for name, value in (("stgs", self.cfg), ("STATE_DIR", self.tmp), ("RUNTIME_DIR", self.tmp),
                            ("THROTTLE", palm.LogThrottle()),
                            ("VERIFY_DELAY_S", 0.0), ("PAUSE_SETTLE_S", 0.0)):
            patcher = mock.patch.object(palm, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class PalmCase(PalmMixin, unittest.TestCase):
    pass


class PalmAsyncCase(PalmMixin, unittest.IsolatedAsyncioTestCase):
    pass


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def make_telemetry(**overrides):
    data = dict(ts=time.monotonic(), wall=datetime.now(), inverter_mins=0, grid_ok=True,
                line_voltage=240.0, line_frequency=50.0, grid_power=0, pv_power=0,
                pv_energy_wh=1000, batt_power=-500, consumption=300, soc=50, grid_energy_wh=0,
                e_battery_charge_total_wh=1, e_battery_discharge_total_wh=2)
    data.update(overrides)
    return palm.Telemetry(**data)


def sane_inverter(**overrides):
    inv = SimpleNamespace(
        v_ac1=240.0, f_ac1=50.0, p_grid_out=100, p_pv1=2000, p_inverter_out=-300, p_battery=-250,
        p_load_demand=900, battery_percent=60, e_pv1_day=5.0, e_battery_charge_total=100.0,
        e_battery_discharge_total=90.0, e_grid_in_day=1.0, e_grid_out_day=0.5,
        system_time_hour=12, system_time_minute=0,
        enable_charge=False, enable_discharge=False, enable_charge_target=False, battery_pause_mode=0)
    for key, value in overrides.items():
        setattr(inv, key, value)
    return inv


class FakeCommands:
    """Any `cmds.set_x(value)` returns a ('set_x', (value,)) request tuple."""

    def __getattr__(self, name):
        def make(*args):
            return (name, args)
        return make


class FakeClient:
    """Stands in for givenergy_modbus Client. Applies writes to its inverter unless told not to."""

    def __init__(self, inverter=None, commands=None):
        self.connected = False
        self.closed = False
        self.executed: list = []
        self.refresh_calls = 0
        self.full_refresh_calls = 0
        self.fail_refresh = False
        self.fail_execute = False
        self.ignore_writes = False
        self.inverter = inverter if inverter is not None else sane_inverter()
        self.plant = SimpleNamespace(inverter=self.inverter)
        self.commands = commands if commands is not None else FakeCommands()

    async def connect(self):
        self.connected = True

    async def close(self):
        self.closed = True
        self.connected = False

    async def refresh_plant(self, full_refresh=False, timeout=None, retries=None):
        self.refresh_calls += 1
        if full_refresh:
            self.full_refresh_calls += 1
        if self.fail_refresh:
            raise ConnectionError("modbus down")

    async def execute(self, request, timeout, retries):
        if self.fail_execute:
            raise ConnectionError("write failed")
        name, args = request
        self.executed.append(request)
        if not self.ignore_writes:
            attr = name[4:] if name.startswith("set_") else name
            setattr(self.inverter, attr, args[0] if args else None)


class FakeInverter:
    """Minimal GivEnergyLocal replacement for BatteryManager tests."""

    def __init__(self, telemetry=None):
        self.latest = telemetry
        self.calls: list[str] = []
        self.audits: list[bool] = []
        self.fail: set[str] = set()

    def fresh(self, max_age=180):
        return self.latest

    async def set_mode(self, cmd, interruptible=True, audit=False):
        self.calls.append(cmd)
        self.audits.append(audit)
        return cmd not in self.fail


class FakeShelly:
    def __init__(self):
        self.ev_power = palm.Reading()
        self.sw: list[bool] = []
        self.attempts = 0
        self.fail = False

    async def set_switch(self, url, on, attempts=3):
        self.attempts += 1
        if self.fail:
            return False
        self.sw.append(on)
        return True


class FakeResponse:
    _MISSING = object()

    def __init__(self, status=200, text="OK", headers=None, json_data=_MISSING):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self._json = json_data

    def json(self):
        if self._json is FakeResponse._MISSING:
            raise ValueError("no JSON body")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPError(f"HTTP {self.status_code}")


class FakeHTTP:
    """Scripted stand-in for httpx.AsyncClient. Items may be responses or exceptions."""

    def __init__(self, responses=None, default=None):
        self.responses = list(responses or [])
        self.default = default if default is not None else FakeResponse()
        self.calls: list[tuple] = []

    async def _do(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0) if self.responses else self.default
        if isinstance(item, Exception):
            raise item
        return item

    async def get(self, url, **kwargs):
        return await self._do("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return await self._do("POST", url, **kwargs)


# --------------------------------------------------------------------------- #
# Disk-activity watcher (SD-card wear regression tests)
# --------------------------------------------------------------------------- #
_WATCHES: list = []
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def _audit(event, args):
    if not _WATCHES:
        return
    try:
        if event == "open":
            path, _mode, flags = args
            if isinstance(flags, int) and flags & _WRITE_FLAGS and not isinstance(path, int):
                for w in _WATCHES:
                    w._note("write_opens", path)
        elif event == "os.rename":
            for w in _WATCHES:
                w._note("renames", args[0])
        elif event == "os.remove":
            for w in _WATCHES:
                w._note("removes", args[0])
    except Exception:  # an audit hook must never break the program under test
        pass


sys.addaudithook(_audit)
_real_fsync = os.fsync


def _counting_fsync(fd):
    for w in _WATCHES:
        w.fsyncs += 1
    return _real_fsync(fd)


os.fsync = _counting_fsync


class DiskWatch:
    """Counts file opens-for-write, renames, removes and fsyncs under `prefix` while active."""

    def __init__(self, prefix):
        self.prefix = str(prefix)
        self.write_opens = self.renames = self.removes = self.fsyncs = 0

    def _note(self, counter, path):
        if os.fsdecode(path).startswith(self.prefix):
            setattr(self, counter, getattr(self, counter) + 1)

    def __enter__(self):
        _WATCHES.append(self)
        return self

    def __exit__(self, *exc):
        _WATCHES.remove(self)
        return False

    @property
    def total(self):
        return self.write_opens + self.renames + self.removes + self.fsyncs


class FakeClock:
    """Manually advanced clock for time-dependent code (backoff, log throttling)."""

    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
