#!/usr/bin/env python3
"""
PALMv2 - PV Active Load Manager
Integrates local Modbus control with resilient error handling.
"""

import logging
import asyncio
import signal
import sys
from pprint import pprint
from datetime import datetime, timedelta
import time
from enum import Enum, auto
import httpx
import palm_settings as stgs
from givenergy_modbus.client.client import Client

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

PALM_VERSION = "v2.0.1"
# -*- coding: utf-8 -*-
# pylint: disable=logging-not-lazy
# pylint: disable=consider-using-f-string
# pylint: disable=logging-fstring-interpolation

# Enhanced logging
logging.basicConfig(
    format='%(asctime)s %(levelname)-8s %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Set the logger for 'httpx' to WARNING to ignore INFO and DEBUG logs
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)


class GivEnergyLocal:
    """Class for GivEnergy inverter (local access) with robust sync."""

    def __init__(self):
        # Data Registers
        self.read_time_mins: int = -100
        self.line_voltage: float = 0
        self.line_frequency: float = 50
        self.grid_power: int = 0
        self.grid_energy: int = 0
        self.pv_power: int = 0
        self.pv_energy: int = 0
        self.batt_power: int = 0
        self.consumption: int = 0
        self.e_battery_charge_total = 0
        self.e_battery_discharge_total = 0
        self.soc: int = 0
        self.tgt_soc: int = 100
        self.aux_ev_power: int = 0
        self.aux_co2: int = 0
        self.aux_temp: int = 0

        # Operational State
        self.last_update_success = False
        self._client = None
        self._lock = asyncio.Lock()
        self._consecutive_failures = 0
        self.MAX_FAILURES = 3

    async def _get_client(self):
        """Returns existing client or initializes a new one."""
        if self._client is None:
            self._client = Client(stgs.GE.local_ip, stgs.GE.local_port)
        return self._client

    def _is_data_sane(self, inv) -> bool:
        """Enhanced sanity checks."""
        try:
            checks = [
                100 <= inv.v_ac1 <= 300,
                0 <= inv.battery_percent <= 100,
                -20000 <= inv.p_grid_out <= 20000  # Sanity check for power spikes
            ]
            return all(checks)
        except (AttributeError, TypeError):
            return False

    async def close_connection(self):
        """Gracefully closes the Modbus connection."""
        if self._client:
            try:
                await asyncio.wait_for(self._client.close(), timeout=2.0)
            except Exception:
                pass
            finally:
                self._client = None

    async def get_latest_data(self):
        """Fetch inverter data."""
        if self._consecutive_failures >= self.MAX_FAILURES:
            logger.warning("Cooling down due to consecutive failures...")
            await asyncio.sleep(30)
            self._consecutive_failures = 0

        async with self._lock:
            try:
                client = await self._get_client()
                if not client.connected:
                    await asyncio.wait_for(client.connect(), timeout=5.0)

                # Full refresh is expensive; ensure timeout is realistic
                await asyncio.wait_for(
                    client.refresh_plant(full_refresh=True, timeout=2, retries=2),
                    timeout=15.0
                )

                inverter = client.plant.inverter
                if inverter and self._is_data_sane(inverter):
                    self._update_internal_state(inverter)
                    self.last_update_success = True
                    self._consecutive_failures = 0
                    # logger.info("Inverter data updated.")
                else:
                    raise ValueError("Inverter data failed sanity check")

            except (asyncio.TimeoutError, Exception) as e:
                self._consecutive_failures += 1
                self.last_update_success = False
                logger.error(f"Read failure ({self._consecutive_failures}/{self.MAX_FAILURES}): {e}")
                await self.close_connection()

    def _update_internal_state(self, inv):
        """Mapping logic from raw inverter registers to class variables."""
        self.read_time_mins = inv.system_time_hour * 60 + inv.system_time_minute
        self.line_voltage = float(inv.v_ac1)
        self.line_frequency = float(inv.f_ac1)
        self.grid_power = -1 * int(inv.p_grid_out)
        self.pv_power = int(inv.p_pv1)
        self.batt_power = int(inv.p_inverter_out)

        if int(inv.p_load_demand) > 0:
            self.consumption = int(inv.p_load_demand)

        self.soc = int(inv.battery_percent)
        self.pv_energy = int(inv.e_pv1_day * 1000)
        self.e_battery_charge_total = int(inv.e_battery_charge_total * 1000)
        self.e_battery_discharge_total = int(inv.e_battery_discharge_total * 1000)

        # Grid energy calculation for PVOutput
        self.grid_energy = round(max(int((inv.e_grid_in_day - inv.e_grid_out_day) * 1000), 0),2)

    async def set_mode(self, cmd: str):
        """Executes inverter control commands with persistence and locking."""
        if stgs.pg.test_mode:
            logger.info(f"DRY RUN: Setting inverter mode: {cmd}")
            return
        logger.info(f"Setting inverter mode: {cmd}")

        async with self._lock:
            client = await self._get_client()
            try:
                # Connection Check
                if not client.connected:
                    await asyncio.wait_for(client.connect(), timeout=5.0)

                # Ensure plant is refreshed so commands object is populated
                await asyncio.wait_for(client.refresh_plant(full_refresh=False), timeout=10.0)
                cmds = client.commands

                verify_target = None  # Used to read-back last command in a sequence

                if cmd == "charge_now":
                    await client.execute(cmds.set_charge_slot_1_start(0),2.0,2)
                    await client.execute(cmds.set_charge_slot_1_end(2359),2.0,2)
                    await client.execute(cmds.set_enable_discharge(False),2.0,2)
                    await client.execute(cmds.set_charge_target(100),2.0,2)
                    await client.execute(cmds.set_enable_charge(True),2.0,2)
                    verify_target = ("enable_charge", True)

                elif cmd == "charge_now_soc":
                    await client.execute(cmds.set_charge_slot_1_start(0),2.0,2)
                    await client.execute(cmds.set_charge_slot_1_end(2359),2.0,2)
                    await client.execute(cmds.set_enable_discharge(False),2.0,2)
                    await client.execute(cmds.set_charge_target(self.tgt_soc),2.0,2)
                    await client.execute(cmds.set_enable_charge(True),2.0,2)
                    verify_target = ("enable_charge", True)

                elif cmd == "discharge_now":
                    await client.execute(cmds.set_charge_slot_1_start(0),2.0,2)
                    await client.execute(cmds.set_charge_slot_1_end(2359),2.0,2)
                    await client.execute(cmds.set_enable_discharge(True),2.0,2)
                    await client.execute(cmds.set_enable_charge(False),2.0,2)
                    verify_target = ("enable_charge", False)

                elif cmd == "pause":
                    await client.execute(cmds.set_enable_discharge(False),2.0,2)
                    await client.execute(cmds.set_battery_discharge_limit(0),2.0,2)
                    await client.execute(cmds.set_enable_charge(False),2.0,2)
                    verify_target = ("enable_charge", False)

                elif cmd == "play":
                    await client.execute(cmds.set_charge_slot_1_start(2330),2.0,2)
                    await client.execute(cmds.set_charge_slot_1_end(530),2.0,2)
                    await client.execute(cmds.set_discharge_slot_1_start(1),2.0,2)
                    await client.execute(cmds.set_discharge_slot_1_end(2359),2.0,2)
                    await client.execute(cmds.set_charge_target(100),2.0,2)
                    await client.execute(cmds.set_battery_discharge_limit(29),2.0,2)
                    await client.execute(cmds.set_enable_discharge(False),2.0,2)
                    await client.execute(cmds.set_enable_charge(True),2.0,2)
                    verify_target = ("enable_charge", True)

                elif cmd == "set_soc":
                    await client.execute(cmds.set_charge_target(self.tgt_soc),2.0,2)
                    await client.execute(cmds.enable_charge_target(True),2.0,2)
                    verify_target = ("enable_charge_target", True)

                else:
                    logger.error(f"Unknown command: {cmd}")

                if verify_target:
                    attr, expected = verify_target
                    for attempt in range(1, 4):
                        await asyncio.sleep(2) # Give the inverter time to process
                        await client.refresh_plant(full_refresh=False)

                        # Get actual value from the inverter object
                        actual = getattr(client.plant.inverter, attr, None)

                        if actual == expected:
                            logger.info(f"Verification SUCCESS: {attr} is {actual} on attempt {attempt}")
                            return True

                        logger.warning(f"Verification PENDING: Expected {attr}={expected}, got {actual} (Attempt {attempt}/3)")

                    logger.error(f"Verification FAILED: {cmd} did not take effect.")
                    return False

            except Exception as e:
                logger.error(f"Command execution failure for {cmd}: {e}")
                await self.close_connection()
                return False
#  End of GivEnergyLocal() class


async def pvoutput_put(data_snapshot: dict):
    """ Asynchronously uploads data to PVOutput.org.
    Bypasses standard URL encoding for the timestamp to preserve literal colons."""

    now = datetime.now() - timedelta(seconds=60)
    post_date = now.strftime("%Y%m%d")
    post_time = now.strftime("%H:%M")  # This is the string we must protect

    payload = {
        "d"  : post_date,
        "key": stgs.PVOutput.key,
        "sid": stgs.PVOutput.sid,
        "v2" : data_snapshot.get('pv_power', 0),
        "v4" : data_snapshot.get('consumption', 0),
        "v5" : data_snapshot.get('aux_temp', 0),
        "v6" : data_snapshot.get('line_voltage', 0),
        "v7" : data_snapshot.get('aux_ev_power',0),
        "v9" : data_snapshot.get('aux_co2',0),
        "v10": int(data_snapshot.get('aux_co2',0) * data_snapshot.get('consumption', 0)),
        "v12": data_snapshot.get('line_frequency', 0),
        "b1" : data_snapshot.get('batt_power', 0) * -1,
        "b2" : data_snapshot.get('soc', 0),
        "b3" : int(stgs.GE.batt_capacity * stgs.GE.batt_utilisation *1000),
        "b4" : data_snapshot.get('e_battery_charge_total', 0),
        "b5" : data_snapshot.get('e_battery_discharge_total', 0)
    }

    # Legacy part_payload. Now only used for logging
    part_payload = {
        "v2" : data_snapshot.get('pv_power', 0),
        "v4" : data_snapshot.get('consumption', 0),
        "v5" : data_snapshot.get('aux_temp', 0),
        "v6" : data_snapshot.get('line_voltage', 0),
        "v7" : data_snapshot.get('aux_ev_power',0),
        "v9" : data_snapshot.get('aux_co2',0),
        "v10": int(data_snapshot.get('aux_co2',0) * data_snapshot.get('consumption', 0)),
        "v12": data_snapshot.get('line_frequency', 0),
        "b1" : data_snapshot.get('batt_power', 0) * -1,
        "b2" : data_snapshot.get('soc', 0),
        "b3" : int(stgs.GE.batt_capacity * stgs.GE.batt_utilisation *1000),
        "b4" : data_snapshot.get('e_battery_charge_total', 0),
        "b5" : data_snapshot.get('e_battery_discharge_total', 0)
    }

    # Manually construct URL to prevent httpx from encoding the colon
    base_url = f"{stgs.PVOutput.url.rstrip('/')}/addstatus.jsp"
    query_string = "&".join([f"{k}={v}" for k, v in payload.items()])
    final_url = f"{base_url}?{query_string}&t={post_time}"

    if stgs.pg.test_mode:
        logger.info(f"DRY RUN URL: {final_url}")
        return

    await asyncio.sleep(2) # Rate limit respect

    async with httpx.AsyncClient() as client:
        try:
            # Pass the full final_url (including params) as the first argument
            response = await client.get(final_url, timeout=10.0)
            response.raise_for_status()
            logger.info("Data; Write to pvoutput.org; "+ post_date+"; "+ post_time+ "; "+ str(part_payload))

        except httpx.HTTPStatusError as e:
            logger.error(f"PVOutput API Error ({e.response.status_code}): {e.response.text}")
        except Exception as e:
            logger.error(f"PVOutput Connection Failed: {e}")
#  End of pvoutput_put()


class Shelly():
    """Routines to return status of Shelly switches and power meters and set switches. """

    def __init__(self):
        self._lock = asyncio.Lock()
        self.ev_power: int = 0

    async def set_switch(self, base_url: str, turn_on: bool) -> bool:
        """Operates a Shelly Plus 1 (Gen 2) switch using the RPC-over-HTTP API."""
        sw_cmd = "on" if turn_on else "off"
        # Gen 2 Shelly uses the /rpc/Switch.Set endpoint for robust control
        url = f"{base_url.rstrip('/')}/rpc/Switch.Set?id=0&on={'true' if turn_on else 'false'}"

        async with httpx.AsyncClient() as client:
            try:
                # Gen 2 prefers GET or POST for RPC calls
                resp = await client.get(url, timeout=5.0)
                resp.raise_for_status()
                logger.info(f"Shelly switch set to {sw_cmd}")
                return True
            except httpx.HTTPError as error:
                logger.error(f"Failed to set Shelly switch: {error}")
                return False

    async def read_switch(self, base_url: str) -> str:
        """Reads Shelly Gen 2 switch state using the RPC Input.GetStatus endpoint."""
        url = f"{base_url.rstrip('/')}/rpc/Switch.GetStatus?id=0"

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url, timeout=5.0)
                resp.raise_for_status()
                parsed = resp.json()

                # Shelly Gen 2 'Switch.GetStatus' returns { "output": bool, ... }
                state = parsed.get('output', False)
                return "On" if state else "Off"

            except (httpx.HTTPError, KeyError) as error:
                logger.error(f"Missing response from Shelly Switch: {error}")
                return "Error"

    async def read_em(self) -> int:
        """ Polls Shelly EM and returns status."""
        url = str(stgs.Shelly.em0_url)
        if not url:
            return False
        power = 0

        async with self._lock:
            async with httpx.AsyncClient() as client:
                try:
                    # FIX: Shelly EM (Gen 1) status is fetched via GET, not PUT
                    resp = await client.get(url, timeout=5.0)
                    resp.raise_for_status()
                    parsed = resp.json()
                except httpx.HTTPError as error:
                    logger.error(f"Shelly EM Unreachable: {error}")
                    return False

            # Gen 1 Shelly EM returns 'emeters' list, Gen 2 returns 'em:0'
            # Assuming Gen 1 based on original 'is_valid' logic
            try:
                # Handle both Gen 1 and Gen 2 style JSON snapshots
                emeter = parsed['emeters'][0] if 'emeters' in parsed else parsed

                if emeter.get('is_valid', True):
                    power = int(emeter.get('power', 0))
                    if 0 > power > 22000:
                        return False

            except (KeyError, IndexError, TypeError) as e:
                logger.error(f"Shelly EM Data Corruption: {e}")
                return False

        self.ev_power = power

        return
#  End of Shelly() class


class Env:
    """Stores environmental info - weather, CO2, etc., with async updates."""
    def __init__(self):
        self.co2_intensity: int = 200
        self.temp_deg_c: float = 15.0
        self.weather_symbol: str = "0"
        self.current_weather: dict = {}
        self._lock = asyncio.Lock()

    async def update_co2(self):
        """Asynchronously import and extract CO2 intensity data."""
        url = f"{stgs.CarbonIntensity.url.rstrip('/')}/{stgs.CarbonIntensity.PostCode}"

        headers = {'Accept': 'application/json'}

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url, headers=headers, timeout=10.0)
                resp.raise_for_status()
                data = resp.json()

                # Navigates the Carbon Intensity API response to get forecast intensity value.
                self.co2_intensity = data['data'][0]['data'][0]['intensity']['forecast']

                logger.info(f"CO2 Updated: {self.co2_intensity}g/kWh")
                return

            except (httpx.HTTPError, KeyError, ZeroDivisionError) as error:
                logger.error(f"Error updating CO2 intensity: {error}")

    async def update_weather_curr(self):
        """Download latest weather from OpenWeatherMap using async client."""
        url = f"{stgs.OpenWeatherMap.url.rstrip('/')}/onecall"
        payload = stgs.OpenWeatherMap.payload

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url, params=payload, timeout=7.0)
                resp.raise_for_status()
                data = resp.json()

                self.current_weather = data
                # Convert Kelvin to Celsius (273.15 is the precise offset)
                raw_temp = data.get('current', {}).get('temp', 288.15)
                self.temp_deg_c = round(raw_temp - 273.15, 1)

                # Fetch weather ID symbol
                weather_info = data.get('current', {}).get('weather', [{}])
                self.weather_symbol = str(weather_info[0].get('id', '0'))

                logger.info(f"Weather Updated: {self.temp_deg_c}°C, Symbol ID: {self.weather_symbol}")

            except (httpx.HTTPError, KeyError) as error:
                logger.error(f"Error obtaining weather data: {error}")
# End of Env() class


class BatteryState(Enum):
    """State Definitions for BatteryManager"""
    ECO_OPTIMISE = auto()      # Standard self-consumption mode
    GRID_CHARGE = auto()       # Forced charging (off-peak or low carbon)
    EV_PROTECT = auto()        # Halt discharge while EV is drawing high power
    WINTER_BOOST = auto()      # Active grid charge during winter EV load (Aligned to 00/30)
    PEAK_SHAVE = auto()        # Discharge to cap grid import during expensive peaks
    EMERGENCY_RESERVE = auto() # Hold charge due to grid instability/storm


class BatteryManager:
    """State machine to control battery and other outputs"""
    def __init__(self, inverter, shelly):
        self.inverter = inverter
        self.shelly = shelly
        self.current_state = BatteryState.ECO_OPTIMISE
        self.last_state = None

        # Configuration thresholds
        self.EV_POWER_THRESHOLD = 3000  # Watts

        # Winter Boost Logic
        self.boost_expiry = None

    def is_winter(self):
        """Checks if current date falls within the defined winter months."""
        return datetime.now().month in stgs.GE.winter

    def is_shoulder(self):
        """Checks if current date falls within the defined winter months."""
        return datetime.now().month in stgs.GE.shoulder

    def is_off_peak(self):
        """Checks if current time is in off peak period. Allows for spanning midnight. """
        t_now = t_to_mins(time.strftime("%H:%M", time.localtime()))
        return t_to_mins(stgs.GE.start_time) <= t_now < t_to_mins(stgs.GE.end_time) or \
            t_now >= t_to_mins(stgs.GE.start_time) > t_to_mins(stgs.GE.end_time) or \
            t_to_mins(stgs.GE.start_time) > t_to_mins(stgs.GE.end_time) > t_now

    def is_pm_export(self):
        """Agile Export trigger (evening)"""
        if stgs.GE.pm_export_start != "":
            t_now = t_to_mins(time.strftime("%H:%M", time.localtime()))
            return self.inverter.soc > 50 and t_now == t_to_mins(stgs.GE.pm_export_start)

    def calculate_aligned_expiry(self, now):
        """ Calculates expiry time that ends on the next :00 or :30 boundary. """
        # Calculate minutes until the next half-hour mark (00 or 30)
        minutes_to_next_boundary = 30 - (now.minute % 30)

        expiry = now + timedelta(minutes=minutes_to_next_boundary)
        # Strip seconds and microseconds for a clean boundary
        return expiry.replace(second=0, microsecond=0)

    async def update(self):
        """ Determines next state from changes to inputs and triggers inverter commands """
        t_now = datetime.now()

        # 1. Determine Target State (Priority Logic)
        new_state = BatteryState.ECO_OPTIMISE  # Default baseline

        # Check if we are currently in a Winter Boost
        if self.boost_expiry and t_now < self.boost_expiry:
            new_state = BatteryState.WINTER_BOOST

        # If not already boosting, check if we should start a new Winter Boost
        elif self.is_winter() and not self.is_off_peak() and \
            self.inverter.aux_ev_power > self.EV_POWER_THRESHOLD and \
                t_now.minute % 30 < 20:
            self.boost_expiry = self.calculate_aligned_expiry(t_now)
            logging.info(f"EV load detected. Initiating Winter Boost {self.boost_expiry.strftime('%H:%M')}")
            new_state = BatteryState.WINTER_BOOST

        # Standard logic if no boost is active
        # elif self.is_off_peak():
        #     new_state = BatteryState.GRID_CHARGE

        elif not self.is_winter() and not self.is_off_peak() and \
            self.inverter.aux_ev_power > self.EV_POWER_THRESHOLD and \
                t_now.minute % 30 < 20:
            # Summer/Spring behaviour: just pause discharge
            new_state = BatteryState.EV_PROTECT

        elif self.is_pm_export():
            new_state = BatteryState.PEAK_SHAVE

        # Clear expiry if we are no longer in boost and time has passed
        if self.boost_expiry and t_now >= self.boost_expiry:
            logging.info("Winter Boost period completed.")
            # Turn off heating if needed
            if await self.shelly.read_switch(stgs.Shelly.sw1_url) == "On":
                logging.info("Turning off heating.")
                asyncio.create_task(self.shelly.set_switch(stgs.Shelly.sw1_url, False))
            self.boost_expiry = None

        # 2. Handle State Transitions
        if new_state != self.current_state:
            await self.transition_to(new_state)

    async def transition_to(self, target_state):
        """Executes the specific inverter commands for the new state."""
        logging.info(f"Transitioning: {self.current_state.name} -> {target_state.name}")

        try:
            if target_state == BatteryState.WINTER_BOOST:
                if self.inverter.aux_temp < 15:  # Force heating on
                    logging.info("Turning off heating.")
                    asyncio.create_task(self.shelly.set_switch(stgs.Shelly.sw1_url, True))
                # Command grid charge
                await self.inverter.set_mode("charge_now")

            elif target_state == BatteryState.GRID_CHARGE:
                await self.inverter.set_mode("charge_now")

            elif target_state == BatteryState.EV_PROTECT:
                await self.inverter.set_mode("pause_discharge")

            elif target_state == BatteryState.ECO_OPTIMISE:
                await self.inverter.set_mode("play")

            elif target_state == BatteryState.PEAK_SHAVE:
                await self.inverter.set_mode("discharge_now")

            self.last_state = self.current_state
            self.current_state = target_state

        except Exception as e:
            logging.error(f"Failed to transition to {target_state.name}: {e}")


# Utility Functions
def t_to_mins(time_str: str) -> int:
    """Safe conversion to mins after midnight with validation."""
    if not time_str or not isinstance(time_str, str):
        return 0
    try:
        h, m = map(int, time_str.split(':'))
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
        return 0
    except (ValueError, AttributeError):
        return 0

def t_to_hrs_raw(mins: int) -> int:
    """Strict HHMM format integer conversion."""
    mins = max(0, min(mins, 1439)) # Clamp to 23:59
    return int(f"{mins // 60:02d}{mins % 60:02d}")

async def main():
    """Main loop"""

    inverter = GivEnergyLocal()  # Inverter interface

    shelly = Shelly()  # Shelly switches and energy monitor for EV charging

    manager = BatteryManager(inverter, shelly)  # Battery manager state machine

    env_obj: Env = Env()  # Misc environmental data: weather, CO2, etc

    stop_event = asyncio.Event()

    # Linux Signal Handling
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    logger.info("PALM v2 Service Started")

    loop_counter = 0
    while not stop_event.is_set():

        # Update carbon intensity and weather every 15 mins
        if stgs.CarbonIntensity.enable is True and loop_counter % 15 == 0:
            asyncio.create_task(env_obj.update_co2())

        if stgs.OpenWeatherMap.enable is True and loop_counter % 15 == 0:
            asyncio.create_task(env_obj.update_weather_curr())

        # Read EV power meter
        await shelly.read_em()

        # Read status of Shelly switch (example only)
        # switch = await shelly.read_switch(stgs.Shelly.sw1_url)
        # print("Switch status:", switch)
        # if switch == "Off":
        #     asyncio.create_task(shelly.set_switch(stgs.Shelly.sw1_url, True))

        # Fetch inverter data
        await inverter.get_latest_data()

        # Combine all other parameters
        inverter.aux_ev_power = shelly.ev_power
        inverter.aux_co2: int = env_obj.co2_intensity
        inverter.aux_temp: int = env_obj.temp_deg_c

        if not stgs.pg.execute_mode:
            # Run state machine to control inverter
            await manager.update()
        else:  # Single shot command
            await inverter.set_mode(stgs.pg.mode_cmd)

        # Publish data to PVOutput.org. Fire and forget in the background
        if stgs.PVOutput.enable is True and loop_counter % 5 == 4:
            snapshot = inverter.__dict__.copy()
            asyncio.create_task(pvoutput_put(snapshot))

        # Once mode
        if stgs.pg.once_mode is True:
            post_time: str = time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime())
            print(f"{post_time} Cycle: {loop_counter}")
            pprint(inverter.__dict__)
            break

        # Loop timer
        if stgs.pg.test_mode is False:  # Sleep until next minute rollover (non-blocking)
            current_minute = int(time.strftime("%M", time.localtime()))
            while int(time.strftime("%M", time.localtime())) == current_minute:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
        else:  # Test mode. 15 second loop
            post_time: str = time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime())
            print(f"{post_time} Cycle: {loop_counter}")
            pprint(inverter.__dict__)
            print()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                pass

        # Reset frame counter every 24 hours
        if t_to_mins(time.strftime("%H:%M", time.localtime())) == 0:
            loop_counter = 1
        else:
            loop_counter += 1

    await inverter.close_connection()
    logger.info("PALM v2 Service Stopped Cleanly")


if __name__ == '__main__':
    # Parse any command-line arguments
    MESSAGE: str = ""

    if len(sys.argv) > 1:
        if str(sys.argv[1]) in ["-t", "--test"]:
            stgs.pg.test_mode = True
            stgs.pg.debug_mode = True
            MESSAGE = "Running in test mode..."
        elif str(sys.argv[1]) in ["-d", "--debug"]:
            stgs.pg.debug_mode = True
            MESSAGE = "Running in debug mode, extra verbose"
        elif str(sys.argv[1]) in ["-o", "--once"]:
            stgs.pg.once_mode = True
            MESSAGE = "Running in once mode..."
        elif str(sys.argv[1]) in ["-x", "--execute"]:
            stgs.pg.once_mode = True
            stgs.pg.execute_mode = True
            stgs.pg.mode_cmd = str(sys.argv[2])
            MESSAGE = "Executing inverter command: "+ stgs.pg.mode_cmd

    if stgs.pg.debug_mode is True:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("PALM")

    logger.critical("PALM... PV Automated Load Manager Version: "+ PALM_VERSION)
    logger.critical("Command line options (only one can be used):")
    logger.critical("-t | --test  | test mode (4x speed, no external server writes)")
    logger.critical("-d | --debug | debug mode, extra verbose")
    logger.critical("-o | --once  | once mode, reports inverter status and then exit")
    logger.critical("-x | --execute [charge_now | discharge_now | pause | play] | run inverter command and exit")
    logger.critical("")
    if MESSAGE != "":
        logger.critical(MESSAGE)
        logger.critical("")

    try:
        asyncio.run(main())
    except Exception as e:
        logger.critical(f"Global Crash: {e}")
