#!/usr/bin/env python3
"""
PALM - PV Active Load Manager (Robust Version)
Integrates local Modbus control with resilient error handling.
"""

import logging
import asyncio
import signal
from datetime import datetime, timedelta
import time
import httpx
import palm_settings as stgs
from givenergy_modbus.client.client import Client

# This software in any form is covered by the following Open Source BSD license:
#
# Copyright 2026, Steve Lewis
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification, are permitted
# provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this list of conditions
# and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice, this list of
# conditions and the following disclaimer in the documentation and/or other materials provided
# with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS
# OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY
# AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY
# WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

# Changelog:
# v2.0.0    10/Apr/26 First version to handle continuous Modbus data collection and control.

#FIXME: Needs CLI modes, evening export, inverter control based on EV charging, etc. to be restored.


PALM_VERSION = "v2.0.0"
# -*- coding: utf-8 -*-
# pylint: disable=logging-not-lazy
# pylint: disable=consider-using-f-string

# Enhanced logging
logging.basicConfig(
    format='%(asctime)s %(levelname)-8s %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Get the logger for 'httpx'
httpx_logger = logging.getLogger("httpx")

# Set the logging level to WARNING to ignore INFO and DEBUG logs
httpx_logger.setLevel(logging.WARNING)

class GivEnergyObjLocal:
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
        """Fetch data."""

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

                # Define verification expectations
                # Format: (attribute_to_check, expected_value)
                verify_target = None

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
#  End of GivEnergyObjLocal()


async def put_pv_output(data_snapshot: dict):
    """ Asynchronously uploads data to PVOutput.org.
    Bypasses standard URL encoding for the timestamp to preserve literal colons."""

    # 1. Prepare the base data
    now = datetime.now() - timedelta(seconds=60)
    post_date = now.strftime("%Y%m%d")
    post_time = now.strftime("%H:%M")  # This is the string we must protect

    batt_pwr = data_snapshot.get('batt_power', 0)
    load_pwr = data_snapshot.get('consumption', 0)  # Changed since original

    # 2. Build the payload dictionary
    payload = {
        "d"  : post_date,
        "key": stgs.PVOutput.key,
        "sid": stgs.PVOutput.sid,
        "v2" : data_snapshot.get('pv_power', 0),
        "v4" : load_pwr,
        "v5" : data_snapshot.get('aux_temp', 0),
        "v6" : data_snapshot.get('line_voltage', 0),
        "v7" : data_snapshot.get('aux_ev_power',0),
        "v8" : max(batt_pwr, 0),
        "v9" : data_snapshot.get('aux_co2',0),
        "v10": int(data_snapshot.get('aux_co2',0) * load_pwr),
        "v11": abs(min(batt_pwr, 0)),
        "v12": data_snapshot.get('line_frequency', 0),
        "b1" : batt_pwr * -1,
        "b2" : data_snapshot.get('soc', 0),
        "b3" : int(stgs.GE.batt_capacity * stgs.GE.batt_utilisation *1000),
        "b4" : data_snapshot.get('e_battery_charge_total', 0),
        "b5" : data_snapshot.get('e_battery_discharge_total', 0)
    }

    # Legacy part_payload. Now only used for logging
    part_payload = {
        "v2" : data_snapshot.get('pv_power', 0),
        "v4" : load_pwr,
        "v5" : data_snapshot.get('aux_temp', 0),
        "v6" : data_snapshot.get('line_voltage', 0),
        "v7" : data_snapshot.get('aux_ev_power',0),
        "v8" : max(batt_pwr, 0),
        "v9" : data_snapshot.get('aux_co2',0),
        "v10": int(data_snapshot.get('aux_co2',0) * load_pwr),
        "v11": abs(min(batt_pwr, 0)),
        "v12": data_snapshot.get('line_frequency', 0),
        "b1" : batt_pwr * -1,
        "b2" : data_snapshot.get('soc', 0),
        "b3" : int(stgs.GE.batt_capacity * stgs.GE.batt_utilisation *1000),
        "b4" : data_snapshot.get('e_battery_charge_total', 0),
        "b5" : data_snapshot.get('e_battery_discharge_total', 0)
    }

    # 3. Manually construct the URL to prevent httpx from encoding the colon
    # Encode the other params, then tack on the raw time at the end
    base_url = f"{stgs.PVOutput.url.rstrip('/')}/addstatus.jsp"

    # Standard encode everything EXCEPT time
    query_string = "&".join([f"{k}={v}" for k, v in payload.items()])

    # Add the "protected" time parameter with its literal colon
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

#  End of put_pv_output()

class ShellyObj():
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
                return "On-stat" if state else "Off"

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

class EnvObj:
    """Stores environmental info - weather, CO2, etc., with async updates."""

    def __init__(self):
        self.co2_intensity: int = 200
        self.temp_deg_c: float = 15.0
        self.weather_symbol: str = "0"
        self.current_weather: dict = {}
        self._lock = asyncio.Lock()

    async def update_co2(self):
        """Asynchronously import and analyze CO2 intensity data."""

        url = f"{stgs.CarbonIntensity.url.rstrip('/')}/{stgs.CarbonIntensity.PostCode}"

        headers = {'Accept': 'application/json'}

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url, headers=headers, timeout=10.0)
                resp.raise_for_status()
                data = resp.json()

            # Navigates the Carbon Intensity API response to retrieve forecast intensity value.
                # data['data'] is a list of regions
                # [0] gets the first region
                # ['data'] inside that is a list of half-hour slots
                # [0] gets the first slot
                # ['intensity']['forecast'] is the value 53
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

# # End of EnvObj


class EventsObj:
    """Definitions used to trigger events each minute in scheduler. Less messy this way """

    def __init__(self):
        self.shoulder: bool = False
        self.winter: bool = False
        self.off_pk: bool = False
        self.off_pk_start: bool = False
        self.off_pk_ending: bool = False
        self.off_pk_end: bool = False
        self.am_export_finish = False
        self.pm_export_start = False
        self.pm_boost_start: bool = False
        self.pm_boost_end: bool = False
        self.update_pv_fcast: bool = False
        self.update_soc: bool = False
        self.update_soc_pass_2: bool = False
        self.update_carbon_intensity: bool = False
        self.update_weather: bool = False

    def update(self):
        """Values are updated every minute for use by main code loop"""
        t_now = stgs.pg.t_now_mins
        t_plus_hr = t_now + 60 % 1440

        self.shoulder = stgs.pg.month in stgs.GE.shoulder
        self.winter = stgs.pg.month in stgs.GE.winter

        if stgs.GE.start_time != "" and stgs.GE.end_time != "":
            # Is current time within off-peak window? Needs to consider spanning midnight
            self.off_pk_start = t_to_mins(stgs.GE.start_time) == t_now
            self.off_pk = t_to_mins(stgs.GE.start_time) <= t_now < t_to_mins(stgs.GE.end_time) or \
                t_now >= t_to_mins(stgs.GE.start_time) > t_to_mins(stgs.GE.end_time) or \
                t_to_mins(stgs.GE.start_time) > t_to_mins(stgs.GE.end_time) > t_now

        if stgs.GE.end_time != "" and stgs.GE.end_time_winter != "":
            # Flag 1 hour before end of off-peak
            self.off_pk_ending = self.winter is True and \
                t_plus_hr == t_to_mins(stgs.GE.end_time_winter) or \
                self.winter is False and t_plus_hr == t_to_mins(stgs.GE.end_time)
            # Flag at end of off-peak
            self.off_pk_end = \
                self.winter is True and t_now == t_to_mins(stgs.GE.end_time_winter) or \
                self.winter is False and t_now == t_to_mins(stgs.GE.end_time)

        # Agile Export triggers (morning & evening)
        if stgs.GE.am_export_finish != "":
            self.am_export_finish = t_now == t_to_mins(stgs.GE.am_export_finish) and \
            self.winter is False and self.shoulder is False
        if stgs.GE.pm_export_start != "":
            self.pm_export_start = t_now == t_to_mins(stgs.GE.pm_export_start)

        # Update carbon intensity and weather every 15 mins
        self.update_carbon_intensity = \
            stgs.CarbonIntensity.enable is True and stgs.pg.loop_counter % 15 == 1
        self.update_weather = \
            stgs.OpenWeatherMap.enable is True and stgs.pg.loop_counter % 15 == 1

# End of EventsObj


# --- Utility Functions ---
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

# --- Main Supervisor ---

async def main():
    """Main loop"""
    # Inverter interface
    inverter = GivEnergyObjLocal()

    # Shelly switches and energy monitor for EV charging
    shelly = ShellyObj()

    # Misc environmental data: weather, CO2, etc
    env_obj: EnvObj = EnvObj()

    # Initialise event semaphores
    events: EventsObj = EventsObj()

    stop_event = asyncio.Event()

    # Linux Signal Handling
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    logger.info("PALM v2 Service Started")

    # await inverter.set_mode("play")

    counter = 0
    while not stop_event.is_set():

        # post_time: str = time.strftime("%d-%m-%Y %H:%M:%S %z", time.localtime())

        # Schedule activities at specific intervals
        events.update()

        if events.update_carbon_intensity is True:
            asyncio.create_task(env_obj.update_co2())

        if events.update_weather is True:
            asyncio.create_task(env_obj.update_weather_curr())

        # Read EV power meter
        await shelly.read_em()

        # Fudges to get all parameters into PVOutput
        inverter.aux_ev_power = shelly.ev_power
        inverter.aux_co2: int = env_obj.co2_intensity
        inverter.aux_temp: int = env_obj.temp_deg_c

        # Fetch inverter data
        await inverter.get_latest_data()

        # print(f"{post_time} Cycle: {counter}")
        # print(inverter.__dict__)
        # print()

        # if ev_active:
            # await set_shelly_switch(stgs.Shelly.switch_url, True)

        # Publish data to PVOutput.org
        if stgs.PVOutput.enable is True and counter % 5 == 4:
            # Fire and forget in the background
            snapshot = inverter.__dict__.copy()
            asyncio.create_task(put_pv_output(snapshot))

        # Sleep until next minute rollover (non-blocking)
        current_minute = int(time.strftime("%M", time.localtime()))
        while int(time.strftime("%M", time.localtime())) == current_minute:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

        # Reset frame counter every 24 hours
        if t_to_mins(time.strftime("%H:%M", time.localtime())) == 0:
            counter = 1
        else:
            counter += 1
        stgs.pg.loop_counter = counter

    await inverter.close_connection()
    logger.info("PALM v2 Service Stopped Cleanly")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        logger.critical(f"Global Crash: {e}")
