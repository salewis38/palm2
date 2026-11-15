#!/usr/bin/env python3
"""
PALM - PV Active Load Manager
Integrates local Modbus control with resilient error handling.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import importlib
import ipaddress
import json
import logging
import math
import os
import random
import signal
import socket
import sys
import tempfile
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from pprint import pprint
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import palm_settings as stgs
from givenergy_modbus.client.client import Client

try:  # POSIX only; the single-instance lock is skipped elsewhere
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

# Copyright 2026, Steve Lewis
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software
# and associated documentation files (the “Software”), to deal in the Software without
# restriction, including without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or
# substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
# BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
# DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

# Changelog:
# v2.0.0    10/Apr/26 First version to handle continuous Modbus data collection and control.
# v2.0.1    10/Apr/26 Added state machine for battery control and CLI settings.
# v2.0.1a   19/Apr/26 Bugfix on EV charging logic
# v2.0.1b   21/Apr/26 Added 15s wait to pause/end pause battery controls
# v2.0.2    12/May/26 Read charge/discharge limits from settings, added get_status option
# v2.0.2a   01/Jul/26 Added safe restart to inverter control
# v2.0.3    Robustness release:
#           - Level-triggered control: desired vs applied state, retry with backoff
#           - Command results are checked; failed commands are retried, not assumed
#           - Safe state (end pause, play, heater off) on start-up and on shutdown
#           - Sensor freshness tracking; stale data is never acted on or uploaded
#           - Non-blocking backoff (no sleeps inside the poll); command lock released
#             before verification; stop-aware sleeps
#           - PVOutput: key in headers, retries, on-disk spool, omit missing fields
#           - Per-cycle exception handling, non-zero exit on crash, strict settings
#             validation, argparse CLI, single logging setup, instance lock,
#             systemd notify/watchdog, inverter clock-drift warning
# v2.0.4    SD-card wear reduction (no change to control behaviour):
#           - PVOutput spool is append-only with no fsync/rename; rewritten only when entries leave it
#           - Repeating failures are logged once, then summarised every 15 min, plus a recovery line
#           - Success line shortened (payload moved to DEBUG)  [reverted in v2.0.5]
#           - Lock file lives in $RUNTIME_DIRECTORY (tmpfs); state dir honours $STATE_DIRECTORY
# v2.0.5    - PVOutput success line restored to the original format, full payload at INFO:
#             Data; Write to pvoutput.org; 20261008; 19:19; {'v2': 0, 'v4': 553, ...}
#           - Daily generation total (v1) is no longer uploaded; PVOutput integrates the power samples
#           - Every open() states its encoding; local variable `entry` renamed (pylint)
# v2.0.6    - When the overnight (off-peak) charge window opens, the inverter settings are re-asserted
#             (the 'play' command in normal operation) so changes made by another program cannot persist,
#             after a full register read that logs whether anything had been changed. Once per window,
#             verified and retried with backoff like any command. Disable: GE.refresh_at_offpeak_start = False
# v2.0.7    - Evening export (PEAK_SHAVE) ends when the overnight window opens and cannot start inside it
#           - The overnight re-assert is always on: GE.refresh_at_offpeak_start has been removed
# v2.1.0    - Optional read-only status web server (Web section of palm_settings): live page, /status.json,
#             /history.json (24 h kept in memory only) and /metrics (Prometheus). Standard library only,
#             GET/HEAD only, loopback by default, optional token, Host-header check, no disk writes
# v2.1.1    - The status page moved out of palm.py into palm_gui.py, which sits next to it. It is optional:
#             without it a minimal page is served and /status.json and /metrics are unaffected
#           - Host system stats on the page, /status.json and /metrics: uptime and load averages

PALM_VERSION = "v2.1.1"
# -*- coding: utf-8 -*-
# pylint: disable=logging-fstring-interpolation, consider-using-with

logger = logging.getLogger("PALM")

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #
STALE_AFTER_S = 180               # inverter / EV readings older than this are not used
ENV_STALE_AFTER_S = 3 * 3600      # weather / CO2 older than this are not used
ENV_UPDATE_EVERY_S = 900          # weather / CO2 refresh interval
CONNECT_TIMEOUT_S = 5.0
REFRESH_TIMEOUT_S = 15.0
COMMAND_TIMEOUT_S = 15.0
FULL_REFRESH_EVERY = 1            # 1 = full refresh every poll (original behaviour); raise to lighten Modbus load
PAUSE_SETTLE_S = 15.0             # pause/end_pause take a while to be accepted
VERIFY_DELAY_S = 10.0
VERIFY_ATTEMPTS = 3
MAX_RATE_REGISTER = 50            # upper bound for charge/discharge limit registers; check your model
SHUTDOWN_TIMEOUT_S = 120.0
CLOCK_CHECK_EVERY_S = 3600
CLOCK_DRIFT_WARN_MIN = 3
DEFAULT_CO2 = 200                 # used for decisions only when CarbonIntensity is disabled in settings
DEFAULT_TEMP_C = 15.0             # used for decisions only when OpenWeatherMap is disabled in settings
SPOOL_MAX_ENTRIES = 300
SPOOL_MAX_AGE_DAYS = 13           # PVOutput rejects status older than 14 days (free tier)
LOG_REPEAT_S = 900.0              # a repeating failure is logged at most this often (plus a recovery line)


def resolve_dir(setting: Any, env_var: str, default: Path) -> Path:
    """Explicit setting wins, then the directory systemd provides (StateDirectory=/RuntimeDirectory=), then default."""
    if setting:
        return Path(setting)
    from_env = os.environ.get(env_var, "").split(":")[0].strip()
    return Path(from_env) if from_env else Path(default)


_PG = getattr(stgs, "pg", None)
STATE_DIR = resolve_dir(getattr(_PG, "state_dir", None), "STATE_DIRECTORY", Path.home() / ".palm")   # persistent
RUNTIME_DIR = resolve_dir(getattr(_PG, "runtime_dir", None), "RUNTIME_DIRECTORY", STATE_DIR)         # tmpfs

KNOWN_COMMANDS = (
    "charge_now", "charge_now_soc", "discharge_now", "pause", "end_pause",
    "play", "set_soc", "get_status",
)


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #
def num(value: Any) -> Optional[float]:
    """Convert to a finite float, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def parse_hhmm(text: Any) -> int:
    """Strict 'HH:MM' -> minutes after midnight. Raises ValueError on bad input."""
    if not isinstance(text, str):
        raise ValueError(f"time must be a string 'HH:MM', got {text!r}")
    hours_s, sep, mins_s = text.strip().partition(":")
    if not sep:
        raise ValueError(f"time must look like 'HH:MM', got {text!r}")
    hours, mins = int(hours_s), int(mins_s)
    if not (0 <= hours < 24 and 0 <= mins < 60):
        raise ValueError(f"time out of range: {text!r}")
    return hours * 60 + mins


def t_to_hrs_raw(mins: int) -> int:
    """Minutes after midnight -> HHMM integer, as used by the inverter slot registers."""
    mins = max(0, min(int(mins), 1439))
    return (mins // 60) * 100 + (mins % 60)


def minutes_of(moment: datetime) -> int:
    return moment.hour * 60 + moment.minute


def in_window(now_min: int, start_min: int, end_min: int) -> bool:
    """True if now is in [start, end). Handles windows spanning midnight."""
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min


def rate_register(kw: float) -> int:
    return int(clamp(int(kw * 10 - 1), 0, MAX_RATE_REGISTER))


def atomic_write(path: Path, text: str, durable: bool = True) -> None:
    """Write via temp file + rename. durable=False skips fsync (flash-friendly; for data that can be re-created)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            if durable:
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def sd_notify(message: str) -> None:
    """Minimal systemd notify (READY/WATCHDOG/STOPPING). Silent no-op outside systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(message.encode())
    except OSError:
        pass


_background: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    """Create a background task whose result is never silently lost."""
    task = asyncio.create_task(coro)
    _background.add(task)

    def _done(t: asyncio.Task) -> None:
        _background.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error("Background task failed", exc_info=t.exception())

    task.add_done_callback(_done)
    return task


class Backoff:
    """Non-blocking exponential backoff: ask ready() instead of sleeping."""

    def __init__(self, base: float = 15.0, cap: float = 300.0):
        self.base, self.cap = base, cap
        self.failures = 0
        self.not_before = 0.0

    def ready(self) -> bool:
        return time.monotonic() >= self.not_before

    def success(self) -> None:
        self.failures = 0
        self.not_before = 0.0

    def failure(self) -> float:
        self.failures += 1
        delay = min(self.cap, self.base * 2 ** (self.failures - 1)) * random.uniform(0.9, 1.1)
        self.not_before = time.monotonic() + delay
        return delay


class Reading:
    """A value plus the time it was obtained, so stale data can be ignored."""

    def __init__(self) -> None:
        self.value: Any = None
        self.ts: float = 0.0

    def set(self, value: Any) -> None:
        self.value, self.ts = value, time.monotonic()

    def age(self) -> float:
        return time.monotonic() - self.ts if self.value is not None else math.inf

    def get(self, max_age: float) -> Any:
        return self.value if self.age() <= max_age else None


class LogThrottle:
    """Keep repeating failures from flooding the log (and the SD card behind it).

    The first failure of a kind is logged immediately. Further identical failures are counted and
    logged at DEBUG, with one summary line at most every `interval` seconds. recovered() logs a
    single line when the condition clears."""

    def __init__(self, interval: float = LOG_REPEAT_S, clock=time.monotonic):
        self.interval = interval
        self._clock = clock
        self._state: dict[str, dict] = {}

    def is_failing(self, key: str) -> bool:
        return key in self._state

    def failure(self, key: str, level: int, msg: str, *args, exc_info: bool = False) -> bool:
        """Log a failure, throttled per key. Returns True if a line was emitted at `level`."""
        now = self._clock()
        st = self._state.get(key)
        if st is None:
            self._state[key] = {"first": now, "last": now, "suppressed": 0, "total": 1}
            logger.log(level, msg, *args, exc_info=exc_info)
            return True
        st["total"] += 1
        if now - st["last"] >= self.interval:
            logger.log(level, msg + " (still failing; %d suppressed in the last %.0f min)",
                       *args, st["suppressed"], (now - st["last"]) / 60.0, exc_info=exc_info)
            st["last"], st["suppressed"] = now, 0
            return True
        st["suppressed"] += 1
        logger.debug(msg, *args, exc_info=exc_info)
        return False

    def recovered(self, key: str, msg: str, *args) -> bool:
        """Log one INFO line if `key` was failing, then forget it."""
        st = self._state.pop(key, None)
        if st is None:
            return False
        logger.info(msg + " (after %d failure(s) over %.0f min)", *args, st["total"],
                    (self._clock() - st["first"]) / 60.0)
        return True

    def forget(self, key: str) -> None:
        self._state.pop(key, None)

    def failing(self) -> dict[str, dict[str, int]]:
        """Conditions failing right now: key -> seconds since the first failure and number of occurrences."""
        now = self._clock()
        return {key: {"since_s": round(now - st["first"]), "count": st["total"]} for key, st in self._state.items()}


THROTTLE = LogThrottle()


# --------------------------------------------------------------------------- #
# Settings validation (fail fast instead of silently becoming midnight)
# --------------------------------------------------------------------------- #
def validate_settings() -> list[str]:
    """Check palm_settings and return a list of problems (empty when everything is valid)."""
    errors: list[str] = []

    for name in ("start_time", "end_time"):
        try:
            parse_hhmm(getattr(stgs.GE, name))
        except (ValueError, AttributeError, TypeError) as exc:
            errors.append(f"GE.{name}: {exc}")
    pm_start = getattr(stgs.GE, "pm_export_start", "")
    if pm_start:
        try:
            parse_hhmm(pm_start)
        except (ValueError, TypeError) as exc:
            errors.append(f"GE.pm_export_start: {exc}")

    for name in ("charge_rate", "discharge_rate"):
        value = num(getattr(stgs.GE, name, None))
        if value is None or value < 0.1 or int(value * 10 - 1) > MAX_RATE_REGISTER:
            errors.append(f"GE.{name} must be between 0.1 and {(MAX_RATE_REGISTER + 1) / 10:.1f} (kW)")

    if num(getattr(stgs.GE, "ev_power_threshold", None)) is None:
        errors.append("GE.ev_power_threshold must be a number")
    for name in ("winter", "shoulder"):
        try:
            months = list(getattr(stgs.GE, name))
            if not all(isinstance(m, int) and 1 <= m <= 12 for m in months):
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            errors.append(f"GE.{name} must be a list of month numbers 1-12")

    if getattr(stgs.PVOutput, "enable", False):
        for name in ("key", "sid", "url"):
            if not str(getattr(stgs.PVOutput, name, "") or "").strip():
                errors.append(f"PVOutput.{name} must be set when PVOutput is enabled")
        for name in ("batt_capacity", "batt_utilisation"):
            if num(getattr(stgs.GE, name, None)) is None:
                errors.append(f"GE.{name} must be a number")
    errors.extend(_web_errors())
    return errors


def acquire_instance_lock():
    """Only one PALM may talk to the inverter. Returns a handle to keep alive, or None if held."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(RUNTIME_DIR / "palm.lock", "w", encoding="utf-8")
    if fcntl is not None:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
    return handle


# --------------------------------------------------------------------------- #
# Inverter
# --------------------------------------------------------------------------- #
@dataclass
class Telemetry:
    """One consistent snapshot of the inverter, with the time it was read."""
    ts: float                    # time.monotonic() when read
    wall: datetime
    inverter_mins: Optional[int]
    grid_ok: bool
    line_voltage: float
    line_frequency: float
    grid_power: int
    pv_power: int
    pv_energy_wh: int
    batt_power: int
    consumption: int
    soc: int
    grid_energy_wh: int
    e_battery_charge_total_wh: int
    e_battery_discharge_total_wh: int

    def age(self) -> float:
        return time.monotonic() - self.ts


def _matches(actual: Any, expected: Any) -> bool:
    if actual is None:
        return False
    if actual == expected:
        return True
    try:
        return int(actual) == int(expected)
    except (TypeError, ValueError):
        return False


class GivEnergyLocal:
    """GivEnergy inverter (local Modbus access)."""

    def __init__(self, stop_event: asyncio.Event):
        self.stop_event = stop_event
        self.latest: Optional[Telemetry] = None
        self.tgt_soc: int = 100
        self._client = None
        self._lock = asyncio.Lock()
        self._backoff = Backoff(base=10.0, cap=300.0)
        self._polls = 0

    # -- state ---------------------------------------------------------------
    @property
    def last_update_success(self) -> bool:
        return self.fresh() is not None

    def fresh(self, max_age: float = STALE_AFTER_S) -> Optional[Telemetry]:
        t = self.latest
        return t if t is not None and t.age() <= max_age else None

    async def _sleep(self, seconds: float, interruptible: bool = True) -> None:
        if not interruptible:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # -- connection ----------------------------------------------------------
    async def _ensure_connected(self):
        """Return the Modbus client, creating it and connecting if necessary."""
        if self._client is None:
            self._client = Client(stgs.GE.local_ip, stgs.GE.local_port)
        if not self._client.connected:
            await asyncio.wait_for(self._client.connect(), timeout=CONNECT_TIMEOUT_S)
        return self._client

    async def close_connection(self) -> None:
        """Close the Modbus connection, ignoring errors; the next use reconnects."""
        if self._client:
            try:
                await asyncio.wait_for(self._client.close(), timeout=2.0)
            except Exception:  # pylint: disable=broad-except
                pass
            finally:
                self._client = None

    # -- reading -------------------------------------------------------------
    @staticmethod
    def _is_data_sane(inv) -> bool:
        try:
            volts = float(inv.v_ac1)
            return (0 <= volts <= 300                      # 0 V is a valid reading during a grid outage
                    and 0 <= inv.battery_percent <= 100
                    and -20000 <= inv.p_grid_out <= 20000)
        except (AttributeError, TypeError, ValueError):
            return False

    @staticmethod
    def _build_telemetry(inv) -> Telemetry:
        # Which register is "battery power" differs between models/library versions.
        # p_inverter_out is the original behaviour; set GE.batt_power_attr = "p_battery" to change.
        batt_attr = getattr(stgs.GE, "batt_power_attr", "p_inverter_out")
        hour = getattr(inv, "system_time_hour", None)
        minute = getattr(inv, "system_time_minute", None)
        volts = float(inv.v_ac1)
        pv2_w = int(getattr(inv, "p_pv2", 0) or 0)
        pv2_kwh = float(getattr(inv, "e_pv2_day", 0) or 0)
        return Telemetry(
            ts=time.monotonic(),
            wall=datetime.now(),
            inverter_mins=(int(hour) * 60 + int(minute)) if hour is not None and minute is not None else None,
            grid_ok=volts >= 100,
            line_voltage=volts,
            line_frequency=float(inv.f_ac1),
            grid_power=-1 * int(inv.p_grid_out),
            pv_power=int(inv.p_pv1) + pv2_w,
            pv_energy_wh=int((float(inv.e_pv1_day) + pv2_kwh) * 1000),
            batt_power=int(getattr(inv, batt_attr)),
            consumption=max(int(inv.p_load_demand), 0),
            soc=int(inv.battery_percent),
            grid_energy_wh=max(int((inv.e_grid_in_day - inv.e_grid_out_day) * 1000), 0),
            e_battery_charge_total_wh=int(inv.e_battery_charge_total * 1000),
            e_battery_discharge_total_wh=int(inv.e_battery_discharge_total * 1000),
        )

    async def poll(self, force: bool = False) -> bool:
        """Read the inverter. Never blocks on backoff; returns True on a fresh good reading."""
        if not force and not self._backoff.ready():
            return False
        self._polls += 1
        async with self._lock:
            try:
                client = await self._ensure_connected()
                full = (self._polls - 1) % FULL_REFRESH_EVERY == 0
                await asyncio.wait_for(
                    client.refresh_plant(full_refresh=full, timeout=2, retries=2),
                    timeout=REFRESH_TIMEOUT_S)
                inv = client.plant.inverter
                if inv is None or not self._is_data_sane(inv):
                    raise ValueError("Inverter data failed sanity check")
                telemetry = self._build_telemetry(inv)
            except Exception as exc:  # pylint: disable=broad-except
                delay = self._backoff.failure()
                THROTTLE.failure("inverter_read", logging.ERROR,
                                 "Inverter read failed (%d consecutive, retrying in %.0fs): %s: %s",
                                 self._backoff.failures, delay, exc.__class__.__name__, exc)
                await self.close_connection()
                return False
        self.latest = telemetry
        self._backoff.success()
        THROTTLE.recovered("inverter_read", "Inverter reads restored")
        return True

    # -- commands ------------------------------------------------------------
    def _build_sequence(self, cmd: str, cmds) -> tuple[list, list[tuple[str, Any]], float]:
        """Build ALL requests first (a bad value fails before anything is written).
        Returns (requests, verification targets, settle seconds)."""
        start_time = t_to_hrs_raw(parse_hhmm(stgs.GE.start_time))   # start of off-peak, e.g. 23:30
        end_time = t_to_hrs_raw(parse_hhmm(stgs.GE.end_time))       # end of off-peak, e.g. 05:30
        charge_rate = rate_register(stgs.GE.charge_rate)
        discharge_rate = rate_register(stgs.GE.discharge_rate)
        tgt_soc = int(clamp(self.tgt_soc, 4, 100))

        if cmd == "charge_now":
            return ([cmds.set_charge_slot_1_start(0), cmds.set_charge_slot_1_end(2359),
                     cmds.set_enable_discharge(False), cmds.set_charge_target(100),
                     cmds.set_enable_charge(True)],
                    [("enable_charge", True), ("enable_discharge", False)], 0.0)
        if cmd == "charge_now_soc":
            return ([cmds.set_charge_slot_1_start(0), cmds.set_charge_slot_1_end(2359),
                     cmds.set_enable_discharge(False), cmds.set_charge_target(tgt_soc),
                     cmds.set_enable_charge(True)],
                    [("enable_charge", True), ("enable_discharge", False)], 0.0)
        if cmd == "discharge_now":
            return ([cmds.set_discharge_slot_1_start(1), cmds.set_discharge_slot_1_end(2359),
                     cmds.set_enable_discharge(True), cmds.set_enable_charge(False)],
                    [("enable_discharge", True), ("enable_charge", False)], 0.0)
        if cmd == "pause":        # pause register: 0 = run, 3 = pause
            return ([cmds.set_battery_pause_mode(3)], [("battery_pause_mode", 3)], PAUSE_SETTLE_S)
        if cmd == "end_pause":
            return ([cmds.set_battery_pause_mode(0)], [("battery_pause_mode", 0)], PAUSE_SETTLE_S)
        if cmd == "play":
            return ([cmds.set_charge_slot_1_start(start_time), cmds.set_charge_slot_1_end(end_time),
                     cmds.set_discharge_slot_1_start(1), cmds.set_discharge_slot_1_end(2359),
                     cmds.set_charge_target(100),
                     cmds.set_battery_discharge_limit(discharge_rate),
                     cmds.set_battery_charge_limit(charge_rate),
                     cmds.set_enable_discharge(False), cmds.set_enable_charge(True)],
                    [("enable_charge", True), ("enable_discharge", False)], 0.0)
        if cmd == "set_soc":
            return ([cmds.set_charge_target(tgt_soc), cmds.enable_charge_target(True)],
                    [("enable_charge_target", True)], 0.0)
        raise ValueError(f"Unknown command: {cmd}")

    async def set_mode(self, cmd: str, interruptible: bool = True, audit: bool = False) -> bool:
        """Run an inverter command and verify it. Returns True only if it took effect.

        audit=True first reads ALL registers and reports whether the settings PALM relies on still match
        what it last wrote, i.e. whether another program has changed them."""
        if cmd not in KNOWN_COMMANDS:
            logger.error("Unknown command: %s", cmd)
            return False
        if stgs.pg.test_mode:
            logger.info("TEST ONLY: Setting inverter mode: %s", cmd)
            return True
        logger.log(logging.DEBUG if THROTTLE.is_failing(f"cmd:{cmd}") else logging.INFO,
                   "Setting inverter mode: %s", cmd)

        try:
            async with self._lock:       # lock is held for the writes only, not the waits
                client = await self._ensure_connected()
                if cmd == "get_status":
                    await asyncio.wait_for(client.refresh_plant(full_refresh=True), timeout=REFRESH_TIMEOUT_S)
                    print(client.plant.inverter)
                    return True
                # Ensure plant is refreshed so the commands object is populated. An audit needs current
                # configuration registers, so it uses a full refresh (once per night).
                await asyncio.wait_for(client.refresh_plant(full_refresh=audit),
                                       timeout=REFRESH_TIMEOUT_S if audit else 10.0)
                requests, verify, settle = self._build_sequence(cmd, client.commands)
                if audit:
                    self._audit_registers(cmd, client.plant.inverter, verify)
                for request in requests:
                    await asyncio.wait_for(client.execute(request, 2.0, 2), timeout=COMMAND_TIMEOUT_S)
            if settle:
                await self._sleep(settle, interruptible)
            ok = await self._verify(cmd, verify, interruptible)
            if ok:
                THROTTLE.recovered(f"cmd:{cmd}", "Inverter command %s working again", cmd)
            return ok
        except Exception as exc:  # pylint: disable=broad-except
            THROTTLE.failure(f"cmd:{cmd}", logging.ERROR, "Command execution failure for %s: %s: %s",
                             cmd, exc.__class__.__name__, exc)
            await self.close_connection()
            return False

    @staticmethod
    def _audit_registers(cmd: str, inv, targets: list[tuple[str, Any]]) -> None:
        """Report whether the registers PALM verifies for `cmd` still hold the values PALM last wrote.

        Only the verified registers can be compared; the re-assert that follows rewrites all of them."""
        drift = [(attr, expected, getattr(inv, attr, None)) for attr, expected in targets
                 if not _matches(getattr(inv, attr, None), expected)]
        if drift:
            THROTTLE.failure("register_drift", logging.WARNING,
                             "Inverter settings differ from PALM's %s settings (changed by another program?): %s",
                             cmd, ", ".join(f"{a} is {g!r}, expected {e!r}" for a, e, g in drift))
            return
        THROTTLE.forget("register_drift")
        logger.log(logging.DEBUG if THROTTLE.is_failing(f"cmd:{cmd}") else logging.INFO,
                   "Inverter settings match PALM's %s settings (checked: %s)", cmd, ", ".join(a for a, _ in targets))

    async def _verify(self, cmd: str, targets: list[tuple[str, Any]], interruptible: bool) -> bool:
        for attempt in range(1, VERIFY_ATTEMPTS + 1):
            await self._sleep(VERIFY_DELAY_S, interruptible)
            if interruptible and self.stop_event.is_set():
                return False
            try:
                async with self._lock:
                    client = await self._ensure_connected()
                    await asyncio.wait_for(client.refresh_plant(full_refresh=False), timeout=10.0)
                    inv = client.plant.inverter
                    mismatches = [(attr, expected, getattr(inv, attr, None))
                                  for attr, expected in targets
                                  if not _matches(getattr(inv, attr, None), expected)]
            except Exception as exc:  # pylint: disable=broad-except
                THROTTLE.failure(f"verify_read:{cmd}", logging.WARNING,
                                 "Verification read failed for %s (attempt %d/%d): %s",
                                 cmd, attempt, VERIFY_ATTEMPTS, exc)
                await self.close_connection()
                continue
            if not mismatches:
                logger.info("Verification SUCCESS: %s (attempt %d)", cmd, attempt)
                THROTTLE.forget(f"pending:{cmd}")
                THROTTLE.forget(f"verify_read:{cmd}")
                return True
            THROTTLE.failure(f"pending:{cmd}", logging.WARNING, "Verification PENDING for %s (attempt %d/%d): %s",
                             cmd, attempt, VERIFY_ATTEMPTS,
                             ", ".join(f"{a} expected {e!r} got {g!r}" for a, e, g in mismatches))
        THROTTLE.failure(f"cmd:{cmd}", logging.ERROR, "Verification FAILED: %s did not take effect.", cmd)
        return False
#  End of GivEnergyLocal() class


# --------------------------------------------------------------------------- #
# PVOutput
# --------------------------------------------------------------------------- #
class PVOutputUploader:
    """Uploads status to PVOutput with retries and a small on-disk spool for failures.

    SD-card friendly: nothing is written while uploads succeed. When they fail, each queued item is
    APPENDED to the spool (one ~200-byte line: no fsync, no temp file, no rename). The file is rewritten
    only when entries are sent (a backlog draining) or, during a very long outage, once it has grown to
    twice the queue cap. Entries pruned by age or cap are simply left on disk until then, because the
    loader prunes again anyway. It also tolerates torn or corrupt lines, so no durability tricks are needed."""

    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0                       # epoch seconds
        self.last_ok: Optional[datetime] = None         # time of the last accepted write
        self.writes_ok = 0                              # accepted writes since start
        spool = getattr(stgs.PVOutput, "spool_file", None) or STATE_DIR / "pvoutput_spool.jsonl"
        self.spool_path = Path(spool)
        self._dirty = False                 # file may differ from memory -> rewrite it on the next change
        self._queue: list[dict] = self._load_spool()
        self._disk_len = len(self._queue)   # entries believed to be on disk

    # -- spool ---------------------------------------------------------------
    @staticmethod
    def _prune(entries: list[dict]) -> list[dict]:
        cutoff = datetime.now().date() - timedelta(days=SPOOL_MAX_AGE_DAYS)
        keep = []
        for item in entries:
            try:
                if datetime.strptime(str(item["d"]), "%Y%m%d").date() >= cutoff:
                    keep.append(item)
            except (KeyError, ValueError):
                continue
        return keep[-SPOOL_MAX_ENTRIES:]

    def _load_spool(self) -> list[dict]:
        try:
            with open(self.spool_path, encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            return []
        except OSError as exc:
            THROTTLE.failure("spool_io", logging.ERROR, "Could not read PVOutput spool: %s", exc)
            self._dirty = True
            return []
        clean = not text or text.endswith("\n")             # a missing final newline means a torn write
        entries: list[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                clean = False
                continue
            if isinstance(item, dict):
                entries.append(item)
            else:
                clean = False
        pruned = self._prune(entries)
        self._dirty = not clean or len(pruned) != len(entries)
        return pruned

    def _append(self, item: dict) -> None:
        """Add one line to the spool. The cheap, common failure-path write."""
        try:
            self.spool_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.spool_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(item) + "\n")
            self._disk_len += 1
            THROTTLE.recovered("spool_io", "PVOutput spool writable again")
        except OSError as exc:
            self._dirty = True
            THROTTLE.failure("spool_io", logging.ERROR, "Could not append to PVOutput spool: %s", exc)

    def _persist(self) -> None:
        """Rewrite (or delete) the spool. Rare: only after entries were removed or the file was damaged."""
        try:
            if self._queue:
                atomic_write(self.spool_path, "".join(json.dumps(e) + "\n" for e in self._queue), durable=False)
            else:
                self.spool_path.unlink(missing_ok=True)
            self._disk_len, self._dirty = len(self._queue), False
            THROTTLE.recovered("spool_io", "PVOutput spool writable again")
        except OSError as exc:
            self._dirty = True
            THROTTLE.failure("spool_io", logging.ERROR, "Could not update PVOutput spool: %s", exc)

    def _sync_spool(self, grew_only: bool, new_entry: dict) -> None:
        """Bring the file in line with memory using the fewest, smallest writes."""
        if not self._queue and self._disk_len == 0 and not self._dirty:
            return                                          # healthy path: no disk activity at all
        if self._queue and grew_only and not self._dirty and self._disk_len < 2 * SPOOL_MAX_ENTRIES:
            self._append(new_entry)
        else:
            self._persist()                                 # rewrite, compact, or delete when empty

    # -- payload -------------------------------------------------------------
    def build_fields(self, t: Telemetry, ev_power: Optional[float], temp_c: Optional[float],
                     co2: Optional[float]) -> dict[str, Any]:
        """Build the status fields. Anything unknown is omitted rather than sent as 0."""
        batt = t.batt_power
        day = t.wall.strftime("%Y%m%d")
        fields: dict[str, Any] = {
            "d": day,
            "t": t.wall.strftime("%H:%M"),
            "v2": t.pv_power,
            "v4": t.consumption,
            "v5": temp_c,
            "v6": round(t.line_voltage, 1),
            "v7": int(ev_power) if ev_power is not None else None,
            "v8": max(batt, 0),                    # battery discharging
            "v9": int(co2) if co2 is not None else None,
            "v10": int(co2 * t.consumption) if co2 is not None else None,
            "v11": max(-batt, 0),                  # battery charging
            "v12": round(t.line_frequency, 2),
            "b1": -batt,
            "b2": t.soc,
            "b3": int(stgs.GE.batt_capacity * stgs.GE.batt_utilisation * 1000),
            "b4": t.e_battery_charge_total_wh,
            "b5": t.e_battery_discharge_total_wh,
        }
        return {k: v for k, v in fields.items() if v is not None}

    # -- upload --------------------------------------------------------------
    async def _post(self, fields: dict) -> str:
        """Returns 'ok', 'drop' (permanently rejected) or 'retry'."""
        if time.time() < self._blocked_until:
            return "retry"
        url = f"{stgs.PVOutput.url.rstrip('/')}/addstatus.jsp"
        # Percent-encode values but keep literal colons in the time (as the original did)
        body = "&".join(f"{k}={quote(str(v), safe=':')}" for k, v in fields.items())
        headers = {
            "X-Pvoutput-Apikey": str(stgs.PVOutput.key),     # secrets in headers, never in the URL/logs
            "X-Pvoutput-SystemId": str(stgs.PVOutput.sid),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            resp = await self.http.post(url, content=body, headers=headers, timeout=10.0)
        except httpx.HTTPError as exc:
            THROTTLE.failure("pvoutput", logging.WARNING, "PVOutput connection failed: %s: %s",
                             exc.__class__.__name__, exc)
            return "retry"

        remaining = num(resp.headers.get("X-Rate-Limit-Remaining"))
        reset = num(resp.headers.get("X-Rate-Limit-Reset"))
        if remaining is not None and remaining <= 0 and reset:
            self._blocked_until = reset
        text = (resp.text or "").strip()[:300]

        if resp.status_code == 200:
            # One line per accepted write (replayed spool entries included), in the original format:
            # Data; Write to pvoutput.org; 20261008; 19:19; {'v2': 0, 'v4': 553, ...}
            logger.info("Data; Write to pvoutput.org; %s; %s; %s", fields.get("d"), fields.get("t"),
                        {k: v for k, v in fields.items() if k not in ("d", "t")})
            self.last_ok = datetime.now()
            self.writes_ok += 1
            THROTTLE.forget("pvoutput_deferred")
            THROTTLE.recovered("pvoutput", "PVOutput uploads restored")
            return "ok"
        if resp.status_code == 403 and "exceeded" in text.lower():
            self._blocked_until = reset or time.time() + 3600
            logger.warning("PVOutput rate limit exceeded; pausing uploads")
            return "retry"
        if resp.status_code in (401, 403):
            self._blocked_until = time.time() + 1800
            logger.error("PVOutput authentication/permission error (HTTP %d): %s", resp.status_code, text)
            return "retry"
        if 400 <= resp.status_code < 500:
            logger.error("PVOutput rejected data (HTTP %d), dropping: %s | %s", resp.status_code, text, fields)
            return "drop"
        THROTTLE.failure("pvoutput", logging.WARNING, "PVOutput server error (HTTP %d): %s", resp.status_code, text)
        return "retry"

    def status(self) -> dict[str, Any]:
        """Upload state as plain data for the status page. Contains no credentials."""
        return {
            "enabled": bool(getattr(stgs.PVOutput, "enable", False)),
            "queued": len(self._queue),
            "writes_ok": self.writes_ok,
            "last_ok": self.last_ok.isoformat(timespec="seconds") if self.last_ok else None,
            "last_ok_epoch": round(self.last_ok.timestamp()) if self.last_ok else None,
            "blocked_for_s": max(0, round(self._blocked_until - time.time())),
        }

    async def upload(self, fields: dict) -> None:
        """Send one status entry, replaying any backlog first and in order; failures are queued and spooled."""
        async with self._lock:
            if stgs.pg.test_mode:
                logger.info("TEST ONLY: PVOutput payload: %s", fields)
                return
            queue = self._prune(self._queue + [fields])
            remaining: list[dict] = []
            sent = len(queue)                               # entries removed from the front of the queue
            for i, item in enumerate(queue):
                if await self._post(item) == "retry":
                    remaining, sent = queue[i:], i
                    THROTTLE.failure("pvoutput_deferred", logging.INFO, "PVOutput upload deferred; %d entr%s queued",
                                     len(remaining), "y" if len(remaining) == 1 else "ies")
                    break
            self._queue = remaining
            self._sync_spool(grew_only=(sent == 0 and bool(remaining) and remaining[-1] is fields), new_entry=fields)
#  End of PVOutputUploader


# --------------------------------------------------------------------------- #
# Shelly
# --------------------------------------------------------------------------- #
class Shelly:
    """Shelly switches and power meter."""

    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self.ev_power = Reading()

    async def set_switch(self, base_url: str, turn_on: bool, attempts: int = 3) -> bool:
        """Operates a Shelly Gen 2 switch via RPC-over-HTTP, with retries."""
        sw_cmd = "on" if turn_on else "off"
        if stgs.pg.test_mode:
            logger.info("TEST ONLY: Shelly switch %s", sw_cmd)
            return True
        url = f"{base_url.rstrip('/')}/rpc/Switch.Set?id=0&on={'true' if turn_on else 'false'}"
        for attempt in range(1, attempts + 1):
            try:
                resp = await self.http.get(url, timeout=5.0)
                resp.raise_for_status()
                logger.info("Shelly switch set to %s", sw_cmd)
                return True
            except httpx.HTTPError as error:
                logger.warning("Shelly switch %s failed (attempt %d/%d): %s", sw_cmd, attempt, attempts, error)
                if attempt < attempts:
                    await asyncio.sleep(attempt)
        return False

    async def read_switch(self, base_url: str) -> str:
        """Reads Shelly Gen 2 switch state. Returns 'On', 'Off' or 'Error'."""
        url = f"{base_url.rstrip('/')}/rpc/Switch.GetStatus?id=0"
        try:
            resp = await self.http.get(url, timeout=5.0)
            resp.raise_for_status()
            return "On" if resp.json().get("output", False) else "Off"
        except (httpx.HTTPError, ValueError, AttributeError) as error:
            logger.error("Bad response from Shelly switch: %s", error)
            return "Error"

    async def read_em(self) -> bool:
        """Polls the Shelly EM. Updates ev_power on success; leaves it to go stale on failure."""
        url = getattr(stgs.Shelly, "em0_url", None)
        if not url:
            return False
        try:
            resp = await self.http.get(str(url), timeout=5.0)
            resp.raise_for_status()
            parsed = resp.json()
        except (httpx.HTTPError, ValueError) as error:
            THROTTLE.failure("shelly_em", logging.WARNING, "Shelly EM unreachable or invalid: %s", error)
            return False

        try:
            # Gen 1 returns an 'emeters' list; otherwise treat the document as the meter itself
            emeter = parsed["emeters"][0] if "emeters" in parsed else parsed
            if not emeter.get("is_valid", True):
                THROTTLE.failure("shelly_em", logging.WARNING, "Shelly EM reports invalid reading")
                return False
            power = num(emeter.get("power", emeter.get("act_power")))
        except (KeyError, IndexError, TypeError, AttributeError) as error:
            THROTTLE.failure("shelly_em", logging.ERROR, "Shelly EM data corruption: %s", error)
            return False

        if power is None or power > 22000:
            THROTTLE.failure("shelly_em", logging.WARNING, "Shelly EM power out of range: %r", power)
            return False
        self.ev_power.set(int(max(power, 0)))
        THROTTLE.recovered("shelly_em", "Shelly EM readings restored")
        return True
#  End of Shelly() class


# --------------------------------------------------------------------------- #
# Environment (CO2 and weather)
# --------------------------------------------------------------------------- #
class Env:
    """Environmental info. Values carry timestamps and are ignored when stale."""

    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self.co2 = Reading()
        self.temp = Reading()
        self.weather_symbol: str = "0"
        self.current_weather: dict = {}

    def decision_co2(self) -> Optional[float]:
        """CO2 for control decisions: real value, default if the feed is disabled, None if stale."""
        if not getattr(stgs.CarbonIntensity, "enable", False):
            return DEFAULT_CO2
        return self.co2.get(ENV_STALE_AFTER_S)

    def decision_temp(self) -> Optional[float]:
        if not getattr(stgs.OpenWeatherMap, "enable", False):
            return DEFAULT_TEMP_C
        return self.temp.get(ENV_STALE_AFTER_S)

    async def update_co2(self) -> None:
        """Fetch the forecast carbon intensity (g/kWh) for the configured postcode."""
        url = f"{stgs.CarbonIntensity.url.rstrip('/')}/{stgs.CarbonIntensity.PostCode}"
        try:
            resp = await self.http.get(url, headers={'Accept': 'application/json'}, timeout=10.0)
            resp.raise_for_status()
            value = num(resp.json()['data'][0]['data'][0]['intensity']['forecast'])
            if value is None:
                raise ValueError("forecast intensity missing")
            self.co2.set(int(value))
            logger.info("CO2: %dg/kWh", self.co2.value)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
            logger.error("Error updating CO2 intensity: %s: %s", error.__class__.__name__, error)

    async def update_weather_curr(self) -> None:
        """Fetch the current temperature (Kelvin converted to Celsius) and weather symbol."""
        url = f"{stgs.OpenWeatherMap.url.rstrip('/')}/onecall"
        try:
            resp = await self.http.get(url, params=stgs.OpenWeatherMap.payload, timeout=7.0)
            resp.raise_for_status()
            data = resp.json()
            current = data.get('current') or {}
            raw_temp = num(current.get('temp'))
            if raw_temp is None:
                raise ValueError("current temperature missing")
            temp_c = round(raw_temp - 273.15, 1)         # Kelvin to Celsius
            if not -20 < temp_c < 50:
                raise ValueError(f"temperature out of range: {temp_c}")
            self.current_weather = data
            self.temp.set(temp_c)
            weather_info = current.get('weather') or [{}]
            self.weather_symbol = str(weather_info[0].get('id', '0'))
            logger.info("Weather: %s°C, Symbol ID: %s", temp_c, self.weather_symbol)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
            logger.error("Error obtaining weather data: %s: %s", error.__class__.__name__, error)
# End of Env() class


# --------------------------------------------------------------------------- #
# Battery state machine (level-triggered: decide -> reconcile)
# --------------------------------------------------------------------------- #
class BatteryState(Enum):
    """State Definitions for BatteryManager"""
    ECO_OPTIMISE = auto()       # Standard self-consumption mode
    GRID_CHARGE = auto()        # Reserved - not implemented
    EV_PROTECT = auto()         # Halt discharge while EV is drawing high power
    WINTER_BOOST = auto()       # Active grid charge during winter EV load (aligned to 00/30)
    PEAK_SHAVE = auto()         # Discharge to cap grid import during expensive peaks
    EMERGENCY_RESERVE = auto()  # Reserved - not implemented


STATE_COMMANDS = {
    BatteryState.ECO_OPTIMISE: "play",
    BatteryState.EV_PROTECT: "pause",
    BatteryState.WINTER_BOOST: "charge_now",
    BatteryState.PEAK_SHAVE: "discharge_now",
}


class BatteryManager:
    """Decides the desired state every cycle and keeps retrying until it is applied."""

    def __init__(self, inverter: GivEnergyLocal, shelly: Shelly, env: Env):
        self.inverter = inverter
        self.shelly = shelly
        self.env = env
        self.desired_state = BatteryState.ECO_OPTIMISE
        self.applied_state: Optional[BatteryState] = None   # None = unknown
        self.paused: Optional[bool] = None                  # None = unknown, False = known running
        self.boost_until: Optional[datetime] = None
        self.heater_wanted = False
        self.heater_state: Optional[bool] = None            # None = unknown
        self.ev_power_threshold = stgs.GE.ev_power_threshold
        self._ev_samples: deque = deque(maxlen=3)
        self._backoff = Backoff(base=15.0, cap=300.0)
        self._heater_backoff = Backoff(base=10.0, cap=120.0)
        self._refreshed_window: Optional[date] = None       # off-peak window (by opening date) already re-asserted
        self._refresh_pending: Optional[date] = None        # window opened; waiting for a successful re-assert

    # -- pure-ish decision logic --------------------------------------------
    @staticmethod
    def is_winter(now: datetime) -> bool:
        return now.month in stgs.GE.winter

    @staticmethod
    def is_off_peak(now: datetime) -> bool:
        return in_window(minutes_of(now), parse_hhmm(stgs.GE.start_time), parse_hhmm(stgs.GE.end_time))

    def window_key(self, now: datetime) -> Optional[date]:
        """Date on which the off-peak window containing `now` opened, or None outside the window."""
        if not self.is_off_peak(now):
            return None
        start = parse_hhmm(stgs.GE.start_time)
        if start > parse_hhmm(stgs.GE.end_time) and minutes_of(now) < start:
            return (now - timedelta(days=1)).date()         # after midnight: the window opened yesterday evening
        return now.date()

    def _note_window(self, now: datetime) -> None:
        """Flag a re-assert when an off-peak window has opened that has not been re-asserted yet."""
        key = self.window_key(now)
        if key is not None and key not in (self._refreshed_window, self._refresh_pending):
            self._refresh_pending = key

    def sample_ev(self, telemetry: Optional[Telemetry]) -> None:
        """Called every cycle so the 3-sample window is never stale."""
        ev = self.shelly.ev_power.get(STALE_AFTER_S)
        if telemetry is None or ev is None:
            self._ev_samples.clear()
            return
        self._ev_samples.append(ev - telemetry.pv_power)

    def is_ev_charging(self) -> bool:
        """EV above threshold (net of PV) for 3 consecutive fresh samples."""
        return (len(self._ev_samples) == self._ev_samples.maxlen
                and min(self._ev_samples) > self.ev_power_threshold)

    def is_pm_export(self, now: datetime, telemetry: Optional[Telemetry]) -> bool:
        """Agile export trigger (evening); warmer months only. Missing/stale inputs -> no export.

        Never inside the overnight off-peak window: export ends the moment it opens (the battery then charges)."""
        pm_start = getattr(stgs.GE, "pm_export_start", "")
        if not pm_start or telemetry is None or self.is_winter(now) or self.is_off_peak(now):
            return False
        temp, co2 = self.env.decision_temp(), self.env.decision_co2()
        if temp is None or co2 is None or not (temp > 14 and co2 > 120):
            return False
        return ((telemetry.soc > 90 and minutes_of(now) >= parse_hhmm(pm_start))
                or (telemetry.soc > 30 and self.desired_state == BatteryState.PEAK_SHAVE))

    @staticmethod
    def aligned_expiry(now: datetime) -> datetime:
        """Next :00 or :30 boundary, as an absolute time (immune to midnight rollover)."""
        return now.replace(second=0, microsecond=0) + timedelta(minutes=30 - now.minute % 30)

    def decide(self, now: datetime, telemetry: Optional[Telemetry]) -> BatteryState:
        """Choose the battery state wanted now (EV load, tariff window, evening export), holding a boost to expiry."""
        if self.boost_until is not None:
            if telemetry is None:
                logger.warning("Inverter data stale during boost; abandoning boost (fail safe)")
                self.boost_until = None
            elif now < self.boost_until:
                return self.desired_state
            else:
                self.boost_until = None
                logger.info("Boost period completed.")

        if self.is_ev_charging() and not self.is_off_peak(now):
            self.boost_until = self.aligned_expiry(now)
            return BatteryState.WINTER_BOOST if self.is_winter(now) else BatteryState.EV_PROTECT
        if self.is_pm_export(now, telemetry):
            return BatteryState.PEAK_SHAVE
        return BatteryState.ECO_OPTIMISE

    # -- actuation -----------------------------------------------------------
    async def apply(self, target: BatteryState, interruptible: bool = True) -> bool:
        """Drive the inverter to `target`. Only records success if every command was verified."""
        cmd = STATE_COMMANDS.get(target)
        if cmd is None:
            logger.error("No inverter command mapped for state %s", target.name)
            return False
        # A retry of a transition that is already failing is logged at DEBUG, not INFO, every time
        level = logging.DEBUG if THROTTLE.is_failing(f"apply:{target.name}") else logging.INFO
        reasserting = target == self.applied_state and self._refresh_pending is not None
        if reasserting:
            logger.log(level, "Overnight charge window opened: re-asserting %s settings", target.name)
        else:
            logger.log(level, "Transitioning: %s -> %s",
                       self.applied_state.name if self.applied_state else "UNKNOWN", target.name)

        until = self.boost_until.strftime("%H:%M") if self.boost_until else "?"
        if target == BatteryState.WINTER_BOOST:
            temp = self.env.decision_temp()
            self.heater_wanted = temp is not None and temp < 15
            logger.log(level, "EV load detected. Boosting to %s", until)
        elif target == BatteryState.EV_PROTECT:
            logger.log(level, "EV load detected. Pausing to %s", until)

        # Make sure the battery is not left paused unless that is the target
        if target != BatteryState.EV_PROTECT and self.paused is not False:
            if not await self.inverter.set_mode("end_pause", interruptible):
                return False
            self.paused = False

        if not await self.inverter.set_mode(cmd, interruptible, audit=reasserting):
            return False
        if target == BatteryState.EV_PROTECT:
            self.paused = True
        self.applied_state = target
        return True

    async def reconcile(self) -> None:
        """If the inverter is not in the wanted state (or is due its overnight re-assert), try to apply it."""
        if self.applied_state == self.desired_state and self._refresh_pending is None:
            self._backoff.success()
            return
        if not self._backoff.ready():
            return
        key = f"apply:{self.desired_state.name}"
        if await self.apply(self.desired_state):
            self._backoff.success()
            THROTTLE.recovered(key, "Applied %s after earlier failures", self.desired_state.name)
            if self._refresh_pending is not None:                # the window's re-assert is done (and verified)
                self._refreshed_window, self._refresh_pending = self._refresh_pending, None
        else:
            delay = self._backoff.failure()
            THROTTLE.failure(key, logging.ERROR, "Could not apply %s (%d consecutive failures); retrying in %.0fs",
                             self.desired_state.name, self._backoff.failures, delay)

    async def reconcile_heater(self) -> None:
        """Heater is on only while WINTER_BOOST is applied and was wanted; keep retrying until true."""
        url = getattr(stgs.Shelly, "sw1_url", None)
        if not url:
            return
        want = self.applied_state == BatteryState.WINTER_BOOST and self.heater_wanted
        if self.heater_state == want or not self._heater_backoff.ready():
            return
        if await self.shelly.set_switch(url, want):
            self.heater_state = want
            self._heater_backoff.success()
        else:
            self._heater_backoff.failure()

    async def update(self, now: Optional[datetime] = None) -> None:
        """One control step: sample the EV load, decide the wanted state, then reconcile inverter and heater."""
        now = now or datetime.now()
        telemetry = self.inverter.fresh()
        self.sample_ev(telemetry)
        self.desired_state = self.decide(now, telemetry)
        self._note_window(now)
        await self.reconcile()
        await self.reconcile_heater()

    async def start(self, now: Optional[datetime] = None) -> None:
        """Put the hardware in a known state on start-up (also clears a stale pause)."""
        self.desired_state = BatteryState.ECO_OPTIMISE
        if await self.apply(BatteryState.ECO_OPTIMISE):  # on failure, update() keeps retrying
            self._refreshed_window = self.window_key(now or datetime.now())   # play was just written
        await self.reconcile_heater()

    async def shutdown(self) -> None:
        """Best-effort return to a safe state on exit."""
        self.desired_state = BatteryState.ECO_OPTIMISE
        self.boost_until = None
        self.heater_wanted = False
        try:
            await asyncio.wait_for(self._safe_state(), timeout=SHUTDOWN_TIMEOUT_S)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Could not fully restore safe state on shutdown: %s: %s", exc.__class__.__name__, exc)

    async def _safe_state(self) -> None:
        url = getattr(stgs.Shelly, "sw1_url", None)
        if url and self.heater_state is not False:
            if await self.shelly.set_switch(url, False):
                self.heater_state = False
        if self.applied_state != BatteryState.ECO_OPTIMISE or self.paused is not False:
            await self.apply(BatteryState.ECO_OPTIMISE, interruptible=False)
    def status(self, now: datetime) -> dict[str, Any]:
        """Control state as plain data for the status page."""
        return {
            "desired": self.desired_state.name,
            "applied": self.applied_state.name if self.applied_state else None,
            "paused": self.paused,
            "boost_until": self.boost_until.isoformat(timespec="seconds") if self.boost_until else None,
            "heater_wanted": self.heater_wanted,
            "heater_on": self.heater_state,
            "ev_charging": self.is_ev_charging(),
            "off_peak": self.is_off_peak(now),
            "offpeak_window": f"{stgs.GE.start_time}-{stgs.GE.end_time}",
            "reasserted_window": self._refreshed_window.isoformat() if self._refreshed_window else None,
            "reassert_pending": self._refresh_pending is not None,
        }
#  End of BatteryManager


# --------------------------------------------------------------------------- #
# Status web server (read-only; serves in-memory data only, writes nothing to disk)
# --------------------------------------------------------------------------- #
WEB_MAX_CONNECTIONS = 8           # simultaneous connections; further ones are answered 503
WEB_REQUEST_TIMEOUT_S = 5.0       # a request must arrive and be answered within this time (slow-client defence)
WEB_MAX_LINE = 4096               # longest request or header line accepted
WEB_MAX_HEADERS = 32
WEB_MAX_HEADER_BYTES = 8192
WEB_HISTORY_FIELDS = ("pv", "load", "grid", "battery", "soc", "ev")
WEB_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain")
HTTP_REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
                405: "Method Not Allowed", 414: "URI Too Long", 431: "Request Header Fields Too Large",
                500: "Internal Server Error", 503: "Service Unavailable"}


def web_settings() -> dict[str, Any]:
    """Read the optional Web section of palm_settings, with safe defaults (disabled, loopback only)."""
    cfg = getattr(stgs, "Web", None)
    return {
        "enable": bool(getattr(cfg, "enable", False)),
        "host": str(getattr(cfg, "host", "") or "127.0.0.1"),
        "port": int(getattr(cfg, "port", 8080)),
        "token": str(getattr(cfg, "token", "") or ""),
        "history_hours": float(getattr(cfg, "history_hours", 24)),
        "allowed_hosts": [str(h).strip().lower() for h in (getattr(cfg, "allowed_hosts", None) or [])],
    }


def _web_errors() -> list[str]:
    """Problems with the optional Web settings (nothing is checked while the server is disabled)."""
    cfg = getattr(stgs, "Web", None)
    if not getattr(cfg, "enable", False):
        return []
    errors: list[str] = []
    host = getattr(cfg, "host", "127.0.0.1")
    if not isinstance(host, str) or not host.strip():
        errors.append("Web.host must be an address such as '127.0.0.1' or '0.0.0.0'")
    port = getattr(cfg, "port", 8080)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        errors.append("Web.port must be an integer between 1 and 65535")
    token = getattr(cfg, "token", "")
    if token is not None and not isinstance(token, str):
        errors.append("Web.token must be a string")
    hours = num(getattr(cfg, "history_hours", 24))
    if hours is None or not 0 <= hours <= 72:
        errors.append("Web.history_hours must be between 0 and 72")
    allowed = getattr(cfg, "allowed_hosts", None) or []
    if not isinstance(allowed, (list, tuple)) or not all(isinstance(h, str) and h.strip() for h in allowed):
        errors.append("Web.allowed_hosts must be a list of host names")
    return errors


def system_stats(uptime_file: str = "/proc/uptime") -> dict[str, Any]:
    """Host uptime and load averages for the status page. Anything unavailable (e.g. off Linux) is None."""
    uptime = None
    try:
        with open(uptime_file, encoding="utf-8") as fh:
            uptime = round(float(fh.read().split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    try:
        load: tuple = tuple(round(value, 2) for value in os.getloadavg())
    except (OSError, AttributeError):
        load = (None, None, None)
    return {"uptime_s": uptime, "load1": load[0], "load5": load[1], "load15": load[2], "cpus": os.cpu_count()}


def is_loopback(host: str) -> bool:
    """True for localhost and loopback addresses (the server is then reachable from this machine only)."""
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:
        return False


def host_allowed(host_header: str, extra: list[str]) -> bool:
    """DNS-rebinding defence: accept only IP literals, local-only names and explicitly allowed names.

    A page on a public web site cannot make a browser send a Host header like `192.168.1.5`, `raspberrypi`
    or `pi.local`, but it can make it send the public name it controls; that is what this rejects."""
    host = host_header.strip().lower()
    if not host:
        return True                                      # HTTP/1.0 clients may omit the header
    if host.startswith("["):                             # [::1]:8080
        name = host[1:].split("]", 1)[0]
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return "." not in name or name.endswith(WEB_LOCAL_SUFFIXES) or name in extra


class History:
    """Rolling in-memory record of recent readings for the status page's charts. Never written to disk."""

    def __init__(self, minutes: float):
        self._rows: deque = deque(maxlen=max(1, round(minutes)))
        self._last_ts: Optional[float] = None

    def __len__(self) -> int:
        return len(self._rows)

    def add(self, telemetry: Optional[Telemetry], ev_power: Optional[float], now: Optional[float] = None) -> bool:
        """Record one reading (once per distinct reading). Returns True if a row was added."""
        if telemetry is None or telemetry.ts == self._last_ts:
            return False
        self._last_ts = telemetry.ts
        self._rows.append((int(now if now is not None else time.time()), telemetry.pv_power, telemetry.consumption,
                           telemetry.grid_power, telemetry.batt_power, telemetry.soc,
                           int(ev_power) if ev_power is not None else None))
        return True

    def columns(self) -> dict[str, Any]:
        """The history as parallel lists (compact JSON): t, then one list per field in WEB_HISTORY_FIELDS."""
        rows = list(self._rows)
        cols: dict[str, Any] = {"interval_s": 60, "t": [r[0] for r in rows]}
        for index, name in enumerate(WEB_HISTORY_FIELDS, start=1):
            cols[name] = [r[index] for r in rows]
        return cols


def _label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_metrics(snap: dict[str, Any]) -> str:
    """Prometheus text exposition of a status snapshot. Values that are unknown are simply omitted."""
    out: list[str] = []
    seen: set[str] = set()

    def gauge(name: str, help_text: str, value: Any, labels: str = "") -> None:
        if value is None:
            return
        if name not in seen:
            seen.add(name)
            out.append(f"# HELP {name} {help_text}")
            out.append(f"# TYPE {name} gauge")
        out.append(f"{name}{labels} {int(value) if isinstance(value, bool) else value}")

    service = snap.get("service") or {}
    inv = snap.get("inverter") or {}
    env = snap.get("environment") or {}
    control = snap.get("control") or {}
    pvo = snap.get("pvoutput") or {}
    health = snap.get("health") or {}
    system = snap.get("system") or {}
    gauge("palm_info", "PALM version", 1, '{version="%s"}' % _label(service.get("version", "")))
    rows = [
        ("palm_uptime_seconds", "Seconds since PALM started", service.get("uptime_s")),
        ("palm_system_uptime_seconds", "Seconds since the host booted", system.get("uptime_s")),
        ("palm_system_load1", "Host load average over 1 minute", system.get("load1")),
        ("palm_system_load5", "Host load average over 5 minutes", system.get("load5")),
        ("palm_system_load15", "Host load average over 15 minutes", system.get("load15")),
        ("palm_system_cpus", "Number of CPUs on the host", system.get("cpus")),
        ("palm_inverter_data_fresh", "1 if the latest inverter reading is recent enough to act on", inv.get("fresh")),
        ("palm_inverter_data_age_seconds", "Age of the latest inverter reading", inv.get("age_s")),
        ("palm_battery_soc_percent", "Battery state of charge", inv.get("soc_percent")),
        ("palm_pv_power_watts", "Solar generation", inv.get("pv_w")),
        ("palm_load_power_watts", "House consumption", inv.get("load_w")),
        ("palm_grid_power_watts", "Grid power (positive = import, as PALM reads it)", inv.get("grid_w")),
        ("palm_battery_power_watts", "Battery power (positive = discharging)", inv.get("battery_w")),
        ("palm_grid_voltage_volts", "Grid voltage", inv.get("voltage_v")),
        ("palm_grid_frequency_hertz", "Grid frequency", inv.get("frequency_hz")),
        ("palm_pv_energy_today_watt_hours", "Solar energy generated today", inv.get("pv_energy_today_wh")),
        ("palm_battery_charged_watt_hours", "Lifetime energy into the battery", inv.get("battery_charge_total_wh")),
        ("palm_battery_discharged_watt_hours", "Lifetime energy out of the battery",
         inv.get("battery_discharge_total_wh")),
        ("palm_ev_power_watts", "EV charger power", (snap.get("ev") or {}).get("power_w")),
        ("palm_co2_intensity_grams_per_kwh", "Grid carbon intensity forecast", env.get("co2_g_per_kwh")),
        ("palm_temperature_celsius", "Outside temperature", env.get("temp_c")),
        ("palm_battery_paused", "1 if the battery is paused", control.get("paused")),
        ("palm_heater_on", "1 if PALM has switched the heater on", control.get("heater_on")),
        ("palm_off_peak", "1 inside the overnight off-peak window", control.get("off_peak")),
        ("palm_pvoutput_queued_entries", "Status entries waiting to be sent to PVOutput", pvo.get("queued")),
        ("palm_pvoutput_writes_ok", "Successful PVOutput writes since start", pvo.get("writes_ok")),
        ("palm_pvoutput_last_success_timestamp_seconds", "Time of the last successful PVOutput write",
         pvo.get("last_ok_epoch")),
        ("palm_failing_conditions", "Number of conditions currently failing", len(health.get("failing") or {})),
    ]
    for name, help_text, value in rows:
        gauge(name, help_text, value)
    for kind in ("desired", "applied"):
        current = control.get(kind)
        for state in STATE_COMMANDS:
            gauge("palm_battery_state", "Battery state machine: 1 for the current state", int(current == state.name),
                  '{kind="%s",state="%s"}' % (kind, state.name))
    return "\n".join(out) + "\n"


FALLBACK_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>PALM</title></head>
<body style="font:15px system-ui,sans-serif;margin:2em;line-height:1.5">
<h1>PALM</h1>
<p>The full status page (<code>palm_gui.py</code>) is not installed next to <code>palm.py</code>, so this minimal page
is shown. The data is available as <b>/status.json</b>, <b>/history.json</b> and <b>/metrics</b> (add your
<code>?token=</code> to the address if a token is set).</p>
</body></html>
"""


def load_page() -> str:
    """The status page from palm_gui.py (next to palm.py), or a minimal built-in page if that file is missing."""
    try:
        return importlib.import_module("palm_gui").PAGE
    except (ImportError, AttributeError):
        logger.warning("palm_gui.py not found next to palm.py (or it has no PAGE): serving a minimal status page; "
                       "/status.json and /metrics are unaffected")
        return FALLBACK_PAGE


class _BadRequest(Exception):
    """A request that is refused before routing (malformed, too large...)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class StatusServer:
    """Tiny read-only HTTP server for the status page, JSON and Prometheus metrics.

    Standard library only. GET and HEAD only, one request per connection, no files are served and nothing
    is written to disk. Limits: connection cap, per-request time limit, request/header size limits, Host
    header check (DNS rebinding) and an optional shared token."""

    def __init__(self, snapshot, history, host: str, port: int, token: str = "",
                 allowed_hosts: Optional[list[str]] = None, page: Optional[str] = None):
        self.snapshot, self.history = snapshot, history
        self.page = page if page is not None else load_page()
        self.host, self.port, self.token = host, port, token
        self.allowed_hosts = list(allowed_hosts or [])
        self._server: Optional[asyncio.AbstractServer] = None
        self._writers: set = set()
        self._active = 0
        self.routes = {"/": self._page, "/status.json": self._status_json,
                       "/history.json": self._history_json, "/metrics": self._metrics}

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        """Start listening. Raises OSError if the address cannot be bound."""
        self._server = await asyncio.start_server(self._handle, self.host, self.port, limit=WEB_MAX_LINE)
        self.port = self._server.sockets[0].getsockname()[1]      # resolves port 0 to the real port

    async def stop(self) -> None:
        """Stop accepting, drop open connections and wait (briefly) for the listener to close."""
        if self._server is None:
            return
        self._server.close()
        for writer in list(self._writers):
            writer.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
        except (asyncio.TimeoutError, OSError):
            pass
        self._server = None

    # -- routes (each returns body bytes and a content type) -------------------
    def _page(self) -> tuple[bytes, str]:
        return self.page.encode("utf-8"), "text/html; charset=utf-8"

    def _status_json(self) -> tuple[bytes, str]:
        return json.dumps(self.snapshot(), separators=(",", ":")).encode("utf-8"), "application/json"

    def _history_json(self) -> tuple[bytes, str]:
        return json.dumps(self.history(), separators=(",", ":")).encode("utf-8"), "application/json"

    def _metrics(self) -> tuple[bytes, str]:
        return render_metrics(self.snapshot()).encode("utf-8"), "text/plain; version=0.0.4; charset=utf-8"

    # -- request handling ----------------------------------------------------
    def _authorised(self, headers: dict[str, str], query: dict[str, list[str]]) -> bool:
        if not self.token:
            return True
        supplied = ""
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        elif "token" in query:
            supplied = query["token"][0]
        return hmac.compare_digest(supplied.encode("utf-8"), self.token.encode("utf-8"))

    @staticmethod
    async def _read_request(reader: asyncio.StreamReader) -> Optional[tuple[str, str, dict[str, str]]]:
        """Read the request line and headers. Returns None if the client closed without sending anything."""
        try:
            line = await reader.readline()
        except ValueError:                                  # longer than the stream limit
            raise _BadRequest(414, "request line too long") from None
        if not line:
            return None
        parts = line.decode("latin-1").split()
        if len(parts) != 3 or not parts[2].startswith("HTTP/"):
            raise _BadRequest(400, "bad request")
        headers: dict[str, str] = {}
        total = len(line)
        for _ in range(WEB_MAX_HEADERS + 1):
            try:
                raw = await reader.readline()
            except ValueError:
                raise _BadRequest(431, "header line too long") from None
            total += len(raw)
            if raw in (b"\r\n", b"\n", b""):
                return parts[0].upper(), parts[1], headers
            if total > WEB_MAX_HEADER_BYTES:
                raise _BadRequest(431, "headers too large")
            name, sep, value = raw.decode("latin-1").partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        raise _BadRequest(431, "too many headers")

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, status: int, body: bytes = b"",
                    ctype: str = "text/plain; charset=utf-8", head: bool = False,
                    extra: tuple[str, ...] = ()) -> None:
        lines = [f"HTTP/1.1 {status} {HTTP_REASONS.get(status, 'Error')}",
                 f"Content-Type: {ctype}", f"Content-Length: {len(body)}",
                 "Cache-Control: no-store", "X-Content-Type-Options: nosniff", "X-Frame-Options: DENY",
                 "Referrer-Policy: no-referrer",
                 "Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; "
                 "script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
                 "frame-ancestors 'none'",
                 "Connection: close", *extra]
        writer.write("\r\n".join(lines).encode("latin-1") + b"\r\n\r\n" + (b"" if head else body))
        await writer.drain()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await self._read_request(reader)
        except _BadRequest as exc:
            await self._send(writer, exc.status, (exc.message + "\n").encode())
            return
        if request is None:
            return
        method, target, headers = request
        if method not in ("GET", "HEAD"):
            await self._send(writer, 405, b"method not allowed\n", extra=("Allow: GET, HEAD",))
            return
        head = method == "HEAD"
        if not host_allowed(headers.get("host", ""), self.allowed_hosts):
            await self._send(writer, 403, b"host not allowed\n", head=head)
            return
        split = urlsplit(target)
        if split.path == "/healthz":                        # open on purpose: carries no information
            await self._send(writer, 200, b"ok\n", head=head)
            return
        if not self._authorised(headers, parse_qs(split.query)):
            await self._send(writer, 401, b"token required\n", head=head,
                             extra=('WWW-Authenticate: Bearer realm="palm"',))
            return
        route = self.routes.get(split.path)
        if route is None:
            await self._send(writer, 404, b"not found\n", head=head)
            return
        try:
            body, ctype = route()
        except Exception:  # pylint: disable=broad-except
            THROTTLE.failure("web", logging.ERROR, "Status server could not build %s", split.path, exc_info=True)
            await self._send(writer, 500, b"internal error\n", head=head)
            return
        await self._send(writer, 200, body, ctype, head=head)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._active >= WEB_MAX_CONNECTIONS:
            try:
                await self._send(writer, 503, b"busy\n")
            except (ConnectionError, OSError):
                pass
            await self._finish(reader, writer)
            return
        self._active += 1
        self._writers.add(writer)
        try:
            await asyncio.wait_for(self._serve(reader, writer), timeout=WEB_REQUEST_TIMEOUT_S)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            logger.debug("Status server: connection dropped", exc_info=True)
        except Exception:  # pylint: disable=broad-except
            THROTTLE.failure("web", logging.WARNING, "Status server error", exc_info=True)
        finally:
            self._active -= 1
            self._writers.discard(writer)
            await self._finish(reader, writer)

    @staticmethod
    async def _finish(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Close politely: send FIN, then swallow a little unread input.

        Closing a socket that still holds unread data makes TCP send a reset, which can destroy the response
        before the client has read it (an early 405/414/431/503 for a request whose body we never read)."""
        try:
            if writer.can_write_eof():
                writer.write_eof()
            await asyncio.wait_for(reader.read(WEB_MAX_LINE), timeout=0.25)
        except Exception:  # pylint: disable=broad-except
            pass
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except Exception:  # pylint: disable=broad-except
            pass
#  End of StatusServer


# --------------------------------------------------------------------------- #
# Main service
# --------------------------------------------------------------------------- #
class Service:
    """Owns the helpers and runs the poll, control and upload loop (a single pass in -o/-x modes)."""
    def __init__(self, http: httpx.AsyncClient):
        self.stop_event = asyncio.Event()
        self.inverter = GivEnergyLocal(self.stop_event)
        self.shelly = Shelly(http)
        self.env = Env(http)
        self.manager = BatteryManager(self.inverter, self.shelly, self.env)
        self.uploader = PVOutputUploader(http)
        self.once = bool(stgs.pg.once_mode)
        self.execute = bool(stgs.pg.execute_mode)
        self.service_mode = not (self.once or self.execute)
        self.loop_counter = 0
        self.exit_code = 0
        self._next_env_update = 0.0
        self._next_clock_check = 0.0
        self._last_upload_slot: Optional[tuple] = None
        self._started = time.monotonic()
        self._started_wall = datetime.now()
        self.history: Optional[History] = None             # rolling readings for the status page (RAM only)
        self.web: Optional[StatusServer] = None

    # -- status web server -----------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Everything PALM knows, as plain JSON-safe data built from in-memory state. Contains no secrets."""
        now = datetime.now()
        reading = self.inverter.latest
        inverter = None
        if reading is not None:
            offset = None
            if reading.inverter_mins is not None:
                offset = (reading.inverter_mins - minutes_of(now) + 720) % 1440 - 720
            inverter = {
                "fresh": self.inverter.fresh() is not None,
                "age_s": round(reading.age()),
                "reading_time": reading.wall.isoformat(timespec="seconds"),
                "soc_percent": reading.soc,
                "pv_w": reading.pv_power,
                "load_w": reading.consumption,
                "grid_w": reading.grid_power,
                "battery_w": reading.batt_power,
                "voltage_v": round(reading.line_voltage, 1),
                "frequency_hz": round(reading.line_frequency, 2),
                "grid_ok": reading.grid_ok,
                "pv_energy_today_wh": reading.pv_energy_wh,
                "grid_energy_today_wh": reading.grid_energy_wh,
                "battery_charge_total_wh": reading.e_battery_charge_total_wh,
                "battery_discharge_total_wh": reading.e_battery_discharge_total_wh,
                "clock_offset_min": offset,
            }
        ev, co2, temp = self.shelly.ev_power, self.env.co2, self.env.temp
        failing = THROTTLE.failing()
        return {
            "service": {
                "version": PALM_VERSION,
                "time": now.isoformat(timespec="seconds"),
                "started": self._started_wall.isoformat(timespec="seconds"),
                "uptime_s": round(time.monotonic() - self._started),
                "cycle": self.loop_counter,
            },
            "system": system_stats(),
            "inverter": inverter,
            "ev": {"power_w": ev.get(STALE_AFTER_S), "age_s": round(ev.age()) if ev.value is not None else None},
            "environment": {
                "co2_g_per_kwh": co2.value,
                "co2_age_s": round(co2.age()) if co2.value is not None else None,
                "temp_c": temp.value,
                "temp_age_s": round(temp.age()) if temp.value is not None else None,
                "weather_symbol": self.env.weather_symbol,
            },
            "control": self.manager.status(now),
            "pvoutput": self.uploader.status(),
            "health": {"ok": not failing, "failing": failing},
        }

    def history_columns(self) -> dict[str, Any]:
        """The chart history (empty when the history is switched off)."""
        return (self.history or History(1)).columns()

    async def _start_web(self) -> None:
        """Start the status server if enabled. A failure to bind is logged and never stops the control loop."""
        cfg = web_settings()
        if not cfg["enable"]:
            return
        self.history = History(cfg["history_hours"] * 60) if cfg["history_hours"] > 0 else None
        server = StatusServer(self.snapshot, self.history_columns, cfg["host"], cfg["port"], cfg["token"],
                              cfg["allowed_hosts"])
        try:
            await server.start()
        except (OSError, ValueError) as exc:
            logger.error("Status server could not start on %s:%s: %s", cfg["host"], cfg["port"], exc)
            self.history = None
            return
        self.web = server
        shown = "<this-host>" if cfg["host"] in ("0.0.0.0", "::") else cfg["host"]
        logger.info("Status server listening on http://%s:%d/", shown, server.port)
        if not cfg["token"] and not is_loopback(cfg["host"]):
            logger.warning("Status server is reachable from your network without a token (set Web.token)")

    async def _sleep_until_next_cycle(self) -> None:
        if stgs.pg.test_mode:
            delay = 15.0
        else:                                   # next minute rollover
            delay = 60.0 - (time.time() % 60.0) + 0.1
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    def _check_clock(self, now: datetime) -> None:
        """GivEnergy inverters do not follow BST automatically; slots run on the inverter clock."""
        if time.monotonic() < self._next_clock_check:
            return
        telemetry = self.inverter.fresh()
        if telemetry is None or telemetry.inverter_mins is None:
            return
        self._next_clock_check = time.monotonic() + CLOCK_CHECK_EVERY_S
        diff = abs(telemetry.inverter_mins - minutes_of(now))
        diff = min(diff, 1440 - diff)
        if diff > CLOCK_DRIFT_WARN_MIN:
            logger.warning("Inverter clock differs from host clock by %d min "
                           "(schedules run on the inverter clock)", diff)

    async def _update_env(self) -> None:
        if time.monotonic() < self._next_env_update:
            return
        self._next_env_update = time.monotonic() + ENV_UPDATE_EVERY_S
        coros = []
        if getattr(stgs.CarbonIntensity, "enable", False):
            coros.append(self.env.update_co2())
        if getattr(stgs.OpenWeatherMap, "enable", False):
            coros.append(self.env.update_weather_curr())
        if not coros:
            return
        if self.service_mode:
            for coro in coros:
                spawn(coro)
        else:                                   # one-shot modes: wait so the report is meaningful
            await asyncio.gather(*coros, return_exceptions=True)

    def _maybe_upload(self, now: datetime) -> None:
        if not getattr(stgs.PVOutput, "enable", False) or not self.service_mode:
            return
        if now.minute % 5 != 4:
            return
        slot = (now.date(), now.hour, now.minute // 5)
        if slot == self._last_upload_slot:
            return
        self._last_upload_slot = slot
        telemetry = self.inverter.fresh()
        if telemetry is None:
            THROTTLE.failure("upload_skipped", logging.WARNING, "Skipping PVOutput upload: no fresh inverter data")
            return
        THROTTLE.recovered("upload_skipped", "PVOutput uploads resumed")
        fields = self.uploader.build_fields(
            telemetry,
            ev_power=self.shelly.ev_power.get(STALE_AFTER_S),
            temp_c=self.env.temp.get(ENV_STALE_AFTER_S),
            co2=self.env.co2.get(ENV_STALE_AFTER_S))
        spawn(self.uploader.upload(fields))

    def _report(self) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime())
        print(f"{stamp} Cycle: {self.loop_counter}")
        telemetry = self.inverter.latest
        pprint({"inverter": asdict(telemetry) if telemetry else None,
                "ev_power_w": self.shelly.ev_power.value,
                "co2_g_per_kwh": self.env.co2.value,
                "temp_c": self.env.temp.value})

    async def cycle(self) -> None:
        """Update for each new cycle"""
        now = datetime.now()
        await self._update_env()

        results = await asyncio.gather(self.shelly.read_em(), self.inverter.poll(), return_exceptions=True)
        for name, result in zip(("Shelly EM read", "inverter poll"), results):
            if isinstance(result, BaseException):
                logger.error("%s raised unexpectedly", name, exc_info=result)

        if self.execute:                        # single-shot command
            ok = await self.inverter.set_mode(stgs.pg.mode_cmd)
            if not ok:
                self.exit_code = 1
            await self.inverter.poll(force=True)    # repeat after command executed
        elif self.service_mode:
            await self.manager.update()

        if self.history is not None:
            self.history.add(self.inverter.fresh(), self.shelly.ev_power.get(STALE_AFTER_S))
        self._check_clock(now)
        self._maybe_upload(now)

        if self.once or self.execute:
            self._report()
            if self.inverter.fresh() is None:
                self.exit_code = 1
        elif stgs.pg.test_mode:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S %z', time.localtime())} Cycle: {self.loop_counter}")

    async def run(self) -> int:
        """Main loop"""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop_event.set)

        logger.info("PALM Service Started")
        sd_notify("READY=1")
        if self.service_mode:
            await self._start_web()                 # before manager.start(), so the page works with the inverter down
            await self.manager.start()

        while not self.stop_event.is_set():
            try:
                await self.cycle()
                THROTTLE.recovered("cycle_exception", "Main cycle running normally again")
            except Exception:  # pylint: disable=broad-except
                THROTTLE.failure("cycle_exception", logging.ERROR, "Unhandled error in main cycle; continuing",
                                 exc_info=True)
            sd_notify("WATCHDOG=1")             # liveness: the loop is turning over

            if not self.service_mode:
                break
            # Reset frame counter every 24 hours
            self.loop_counter = 1 if minutes_of(datetime.now()) == 0 else self.loop_counter + 1
            stgs.pg.loop_counter = self.loop_counter
            await self._sleep_until_next_cycle()

        sd_notify("STOPPING=1")
        if self.web is not None:
            await self.web.stop()
        if _background:
            await asyncio.wait(list(_background), timeout=10)
        if self.service_mode:
            await self.manager.shutdown()
        await self.inverter.close_connection()
        logger.info("PALM Service Stopped Cleanly")
        return self.exit_code


async def amain() -> int:
    timeout = httpx.Timeout(10.0, connect=5.0)
    transport = httpx.AsyncHTTPTransport(retries=2)       # retries connection errors only
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as http:
        return await Service(http).run()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse the command line: test, debug, once or execute mode."""
    parser = argparse.ArgumentParser(description="PALM - PV Active Load Manager " + PALM_VERSION)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-t", "--test", action="store_true",
                       help="test mode (15s loop, no inverter/Shelly/PVOutput writes)")
    group.add_argument("-d", "--debug", action="store_true", help="debug mode, extra verbose")
    group.add_argument("-o", "--once", action="store_true", help="report inverter status once, then exit")
    group.add_argument("-x", "--execute", metavar="CMD", choices=KNOWN_COMMANDS,
                       help="run one inverter command and exit: " + " | ".join(KNOWN_COMMANDS))
    return parser.parse_args(argv)


def configure_logging(debug: bool) -> None:
    """Log to stderr at INFO (DEBUG when requested) and quieten httpx."""
    logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S',
                        level=logging.DEBUG if debug else logging.INFO,
                        force=True)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def entry() -> int:
    """Program wrapper"""
    args = parse_args()
    message = ""
    if args.test:
        stgs.pg.test_mode = True
        stgs.pg.debug_mode = True
        message = "Running in test mode..."
    elif args.debug:
        stgs.pg.debug_mode = True
        message = "Running in debug mode, extra verbose"
    elif args.once:
        stgs.pg.once_mode = True
        message = "Running in once mode..."
    elif args.execute:
        stgs.pg.once_mode = True
        stgs.pg.execute_mode = True
        stgs.pg.mode_cmd = args.execute
        message = "Executing inverter command: " + args.execute

    configure_logging(bool(stgs.pg.debug_mode))
    logger.info("PALM... PV Automated Load Manager Version: %s", PALM_VERSION)
    if message:
        logger.info(message)

    errors = validate_settings()
    if errors:
        for err in errors:
            logger.critical("Invalid setting: %s", err)
        return 2

    lock = None
    if not stgs.pg.test_mode:
        lock = acquire_instance_lock()
        if lock is None:
            logger.critical("Another PALM instance is already running; exiting")
            return 3

    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130
    except Exception:  # pylint: disable=broad-except
        logger.critical("Global crash", exc_info=True)
        return 1                                # non-zero so Restart=on-failure kicks in
    finally:
        if lock is not None:
            lock.close()


if __name__ == '__main__':
    sys.exit(entry())
