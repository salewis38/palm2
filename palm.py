#!/usr/bin/env python3
"""
PALM - PV Active Load Manager
Integrates local Modbus control with resilient error handling.
"""

from __future__ import annotations

import argparse
import asyncio
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
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from pprint import pprint
from typing import Any, Optional
from urllib.parse import quote

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
#           - Success line shortened (payload moved to DEBUG)
#           - Lock file lives in $RUNTIME_DIRECTORY (tmpfs); state dir honours $STATE_DIRECTORY

PALM_VERSION = "v2.0.4"
# -*- coding: utf-8 -*-
# pylint: disable=logging-fstring-interpolation, max-line-length = 120, docstring-min-length = 5, max-module-lines = 1500

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
RUNTIME_DIR = resolve_dir(getattr(_PG, "runtime_dir", None), "RUNTIME_DIRECTORY", STATE_DIR)         # tmpfs under systemd

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
        with os.fdopen(fd, "w") as fh:
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


THROTTLE = LogThrottle()


# --------------------------------------------------------------------------- #
# Settings validation (fail fast instead of silently becoming midnight)
# --------------------------------------------------------------------------- #
def validate_settings() -> list[str]:
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
    return errors


def acquire_instance_lock():
    """Only one PALM may talk to the inverter. Returns a handle to keep alive, or None if held."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(RUNTIME_DIR / "palm.lock", "w")
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
        if self._client is None:
            self._client = Client(stgs.GE.local_ip, stgs.GE.local_port)
        if not self._client.connected:
            await asyncio.wait_for(self._client.connect(), timeout=CONNECT_TIMEOUT_S)
        return self._client

    async def close_connection(self) -> None:
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

    async def set_mode(self, cmd: str, interruptible: bool = True) -> bool:
        """Run an inverter command and verify it. Returns True only if it took effect."""
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
                # Ensure plant is refreshed so the commands object is populated
                await asyncio.wait_for(client.refresh_plant(full_refresh=False), timeout=10.0)
                requests, verify, settle = self._build_sequence(cmd, client.commands)
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

    SD-card friendly: nothing is written while uploads succeed. When they fail, each queued entry is
    APPENDED to the spool (one ~200-byte line: no fsync, no temp file, no rename). The file is rewritten
    only when entries are sent (a backlog draining) or, during a very long outage, once it has grown to
    twice the queue cap. Entries pruned by age or cap are simply left on disk until then, because the
    loader prunes again anyway. It also tolerates torn or corrupt lines, so no durability tricks are needed."""

    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0                       # epoch seconds
        self._last_energy: dict[str, Any] = {}
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
        for entry in entries:
            try:
                if datetime.strptime(str(entry["d"]), "%Y%m%d").date() >= cutoff:
                    keep.append(entry)
            except (KeyError, ValueError):
                continue
        return keep[-SPOOL_MAX_ENTRIES:]

    def _load_spool(self) -> list[dict]:
        try:
            with open(self.spool_path) as fh:
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
                entry = json.loads(line)
            except ValueError:
                clean = False
                continue
            if isinstance(entry, dict):
                entries.append(entry)
            else:
                clean = False
        pruned = self._prune(entries)
        self._dirty = not clean or len(pruned) != len(entries)
        return pruned

    def _append(self, entry: dict) -> None:
        """Add one line to the spool. The cheap, common failure-path write."""
        try:
            self.spool_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.spool_path, "a") as fh:
                fh.write(json.dumps(entry) + "\n")
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
 #       # Daily generation total (Wh). Skip if it goes backwards (API/register glitch).
 #       if self._last_energy.get("date") == day and t.pv_energy_wh + 1 < self._last_energy.get("wh", 0):
 #           logger.warning("PV energy total went backwards (%s < %s); omitting v1",
 #                          t.pv_energy_wh, self._last_energy.get("wh"))
 #       else:
 #           fields["v1"] = t.pv_energy_wh
 #           self._last_energy = {"date": day, "wh": t.pv_energy_wh}
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
            logger.info("Data; Write to pvoutput.org; %s; %s; %s", fields.get("d"), fields.get("t"),
                {k: v for k, v in fields.items() if k not in ("d", "t")})
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

    async def upload(self, fields: dict) -> None:
        async with self._lock:
            if stgs.pg.test_mode:
                logger.info("TEST ONLY: PVOutput payload: %s", fields)
                return
            queue = self._prune(self._queue + [fields])
            remaining: list[dict] = []
            sent = len(queue)                               # entries removed from the front of the queue
            for i, entry in enumerate(queue):
                if await self._post(entry) == "retry":
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

    # -- pure-ish decision logic --------------------------------------------
    @staticmethod
    def is_winter(now: datetime) -> bool:
        return now.month in stgs.GE.winter

    @staticmethod
    def is_off_peak(now: datetime) -> bool:
        return in_window(minutes_of(now), parse_hhmm(stgs.GE.start_time), parse_hhmm(stgs.GE.end_time))

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
        """Agile export trigger (evening); warmer months only. Missing/stale inputs -> no export."""
        pm_start = getattr(stgs.GE, "pm_export_start", "")
        if not pm_start or telemetry is None or self.is_winter(now):
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

        if not await self.inverter.set_mode(cmd, interruptible):
            return False
        if target == BatteryState.EV_PROTECT:
            self.paused = True
        self.applied_state = target
        return True

    async def reconcile(self) -> None:
        if self.applied_state == self.desired_state:
            self._backoff.success()
            return
        if not self._backoff.ready():
            return
        key = f"apply:{self.desired_state.name}"
        if await self.apply(self.desired_state):
            self._backoff.success()
            THROTTLE.recovered(key, "Applied %s after earlier failures", self.desired_state.name)
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

    async def update(self) -> None:
        now = datetime.now()
        telemetry = self.inverter.fresh()
        self.sample_ev(telemetry)
        self.desired_state = self.decide(now, telemetry)
        await self.reconcile()
        await self.reconcile_heater()

    async def start(self) -> None:
        """Put the hardware in a known state on start-up (also clears a stale pause)."""
        self.desired_state = BatteryState.ECO_OPTIMISE
        await self.apply(BatteryState.ECO_OPTIMISE)      # on failure, update() keeps retrying
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
#  End of BatteryManager


# --------------------------------------------------------------------------- #
# Main service
# --------------------------------------------------------------------------- #
class Service:
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

        self._check_clock(now)
        self._maybe_upload(now)

        if self.once or self.execute:
            self._report()
            if self.inverter.fresh() is None:
                self.exit_code = 1
        elif stgs.pg.test_mode:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S %z', time.localtime())} Cycle: {self.loop_counter}")

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop_event.set)

        logger.info("PALM Service Started")
        sd_notify("READY=1")
        if self.service_mode:
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
    logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S',
                        level=logging.DEBUG if debug else logging.INFO,
                        force=True)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def entry() -> int:
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
