#!/usr/bin/env python3
"""PALM - PV Active Load Manager."""

import time
import json
from typing import Tuple
import logging
import asyncio
import requests
import palm_settings as stgs
from givenergy_modbus.client.client import Client

logger = logging.getLogger(__name__)

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

###########################################
# This code provides several functions:
# 1. Collection of generation/consumption data from GivEnergy API & upload to PVOutput
# 2. Load management - lights, excess power dumping, etc
# 3. Setting overnight charge point, based on SolCast forecast & actual usage
###########################################

# Changelog:
# v0.6.0    12/Feb/22 First cut at GivEnergy interface
# ...
# v0.10.0   21/Jun/23 Added multi-day averaging for usage calcs
# v1.0.0    15/Jul/23 Random start time, Solcast data correction, IO compatibility, 48-hour fcast
# v1.1.0    06/Aug/23 Split out generic functions as palm_utils.py, remove randomised start time
# v1.1.0a   11/Nov/23 Fixed resume operation after daytime charging, bugfix for chart generation
# v1.1.1    23/Mar/24 Improved SoC calcs with additional backward pass to determine min_charge
# v1.1.2    12/Apr/24 Added extra GivEnergy API commands
# v1.1.3    12/May/24 Added get_presets to GE class
# v1.1.4    09/Jul/25 Relaxed response code checking from Givenergy server
# v2.0.0    02/Apr/26 Move to local control and significantly rationalise functionality

PALM_VERSION = "v2.0.0"
# -*- coding: utf-8 -*-
# pylint: disable=logging-not-lazy
# pylint: disable=consider-using-f-string

class GivEnergyObjLocal:
    """Class for GivEnergy inverter (local access)"""

    def __init__(self):
        # sys_item = {'time': '',
        #             'solar': {'power': 0, 'arrays':
        #                           [{'array': 1, 'voltage': 0, 'current': 0, 'power': 0},
        #                            {'array': 2, 'voltage': 0, 'current': 0, 'power': 0}]},
        #             'grid': {'voltage': 0, 'current': 0, 'power': 0, 'frequency': 0},
        #             'battery': {'percent': 0, 'power': 0, 'temperature': 0},
        #             'inverter': {'temperature': 0, 'power': 0, 'output_voltage': 0, \
        #                 'output_frequency': 0, 'eps_power': 0},
        #             'consumption': 0}
        # self.sys_status: List[str] = [sys_item] * 5
        #
        # meter_item = {'time': '',
        #               'today': {'solar': 0, 'grid': {'import': 0, 'export': 0},
        #                         'battery': {'charge': 0, 'discharge': 0}, 'consumption': 0},
        #               'total': {'solar': 0, 'grid': {'import': 0, 'export': 0},
        #                         'battery': {'charge': 0, 'discharge': 0}, 'consumption': 0}}
        # self.meter_status: List[str] = [meter_item] * 5

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
        # self.base_load = stgs.GE.base_load
        self.tgt_soc: int = 100
        self.cmd_list = "N/A: Local Only"

    def get_presets(self):
        """Deprecated: Returns list of valid API commands"""
        return False


    def get_latest_data(self):
        """Download latest data."""

        async def get_giv_data():
            client = Client(stgs.GE.local_ip, stgs.GE.local_port)

            await client.connect()

            await asyncio.wait_for(client.refresh_plant(full_refresh=True, timeout=5, retries=3), timeout=15.0)

            # await client.refresh_plant(full_refresh=True, timeout=5, retries=3)
            inverter = client.plant.inverter

            await client.close()

            # print("######")
            # print("Battery SoC:", inverter.battery_percent)
            #
            # print("Charge Start:", inverter.charge_slot_1_start)
            # print("Charge End:", inverter.charge_slot_1_end)
            #
            # print("Discharge Start:", inverter.discharge_slot_1_start)
            # print("Discharge End:", inverter.discharge_slot_1_end)
            #
            # print("AC Voltage:", inverter.v_ac1)
            # print("AC Frequency:", inverter.f_ac1)
            #
            # print("PV Power:", inverter.p_pv1)
            # print("Inverter Power:", inverter.p_inverter_out)
            # print("Grid Power:", inverter.p_grid_out)
            # print("Load Power:", inverter.p_load_demand)
            # print(inverter)
            #
            # print("######")

            return inverter

        utc_timenow_mins = t_to_mins(time.strftime("%H:%M:%S", time.gmtime()))
        if (utc_timenow_mins > self.read_time_mins + 5 or
            utc_timenow_mins < self.read_time_mins):  # Update every 5 minutes plus day rollover

            for attempt in range(3):
                # print("Attempt", attempt)
                try:
                    local_inverter = asyncio.run(get_giv_data())

                    if local_inverter !="":
                        self.read_time_mins = local_inverter.system_time_hour * 60 + \
                            local_inverter.system_time_minute
                        # Unlike API reports, inverter time already adjusted for BST
                        # if time.strftime("%z", time.localtime()) == "+0100":
                        #     self.read_time_mins = (self.read_time_mins + 60) % 1440
                        self.line_voltage = float(local_inverter.v_ac1)
                        self.line_frequency = float(local_inverter.f_ac1)
                        self.grid_power = -1 * int(local_inverter.p_grid_out)  # -ve = export
                        self.pv_power = int(local_inverter.p_pv1)
                        self.batt_power = int(local_inverter.p_inverter_out)  # -ve = charging
                        # Filter out missing values form inverter
                        if int(local_inverter.p_load_demand) > 0:
                            self.consumption = int(local_inverter.p_load_demand)
                        self.soc = int(local_inverter.battery_percent)
                        self.pv_energy = int(local_inverter.e_pv1_day * 1000)
                        self.e_battery_charge_total = int(local_inverter.e_battery_charge_total * 1000)
                        self.e_battery_discharge_total = int(local_inverter.e_battery_discharge_total \
                            * 1000)

                        # Daily grid energy must be >=0 for PVOutput.org
                        self.grid_energy = max(int((local_inverter.e_grid_in_day-local_inverter.e_grid_out_day) * 1000), 0)
                        logger.debug(local_inverter.__dict__)
                        break

                    time.sleep(10)
                except:
                    logger.info("Failed to read inverter data")


    def get_load_hist(self):
        """Deprecated,could amend to read back from PVOutput in the future"""
        return False

    def set_mode(self, cmd: str):
        """Configures inverter operating mode"""

        async def giv_ctl(cmd):

            client = Client(stgs.GE.local_ip, stgs.GE.local_port)
            await client.connect()

            await asyncio.wait_for(client.refresh_plant(full_refresh=True, timeout=5, retries=3), timeout=15.0)
            # await client.refresh_plant(full_refresh=True, timeout=5, retries=3)

            commands = client.commands

            # This command works
            # await client.execute(commands.set_charge_target(100),5,2)

            if cmd == "charge_now":   # Replicates App command for CHARGE AT FULL POWER

                await client.execute(commands.set_charge_slot_1_start(0),5,2)
                await client.execute(commands.set_charge_slot_1_end(2359),5,2)
                await client.execute(commands.set_enable_discharge(False),5,2)
                await client.execute(commands.set_enable_charge(True),5,2)
                await client.execute(commands.set_charge_target(100),5,2)

            elif cmd == "charge_now_soc":
                await client.execute(commands.set_charge_slot_1_start(0),5,2)
                await client.execute(commands.set_charge_slot_1_end(2359),5,2)
                await client.execute(commands.set_enable_discharge(False),5,2)
                await client.execute(commands.set_enable_charge(True),5,2)
                await client.execute(commands.set_charge_target(self.tgt_soc),5,2)

            elif cmd == "discharge_now":  # Replicates App command for DISCHARGE AT FULL POWER
                await client.execute(commands.set_charge_slot_1_start(0),5,2)
                await client.execute(commands.set_charge_slot_1_end(2359),5,2)
                await client.execute(commands.set_enable_discharge(True),5,2)
                await client.execute(commands.set_enable_charge(False),5,2)

            elif cmd == "pause_charge":
                await client.execute(commands.set_enable_charge(False),5,2)

            elif cmd == "pause_discharge":
                await client.execute(commands.set_enable_discharge(False),5,2)
                await client.execute(commands.set_battery_discharge_limit(0),5,2)

            elif cmd == "pause":
                await client.execute(commands.set_enable_charge(False),5,2)
                await client.execute(commands.set_enable_discharge(False),5,2)
                await client.execute(commands.set_battery_discharge_limit(0),5,2)


            elif cmd == "play":  # Replicates App commands for PLAY
                await client.execute(commands.set_charge_slot_1_start(2330),5,2)
                await client.execute(commands.set_charge_slot_1_end(530),5,2)
                await client.execute(commands.set_discharge_slot_1_start(1),5,2)
                await client.execute(commands.set_discharge_slot_1_end(2359),5,2)

                await client.execute(commands.set_enable_discharge(False),5,2)
                await client.execute(commands.set_enable_charge(True),5,2)
                await client.execute(commands.set_charge_target(100),5,2)
                await client.execute(commands.set_battery_discharge_limit(29),5,2)

            elif cmd == "set_soc":  # Sets target SoC to value
                await client.execute(commands.set_charge_target(self.tgt_soc),5,2)
                if stgs.GE.start_time != "":
                    start_time = t_to_hrs_raw(t_to_mins(stgs.GE.start_time))
                    await client.execute(commands.set_charge_slot_1_start(start_time),5,2)
                if stgs.GE.end_time != "":
                    end_time = t_to_hrs_raw(t_to_mins(stgs.GE.end_time))
                    await client.execute(commands.set_charge_slot_1_end(end_time),5,2)

            elif cmd == "set_soc_winter":  # Restore default overnight charge params
                await client.execute(commands.set_charge_target(100),5,2)
                if stgs.GE.start_time != "":
                    start_time = t_to_hrs_raw(t_to_mins(stgs.GE.start_time))
                    await client.execute(commands.set_charge_slot_1_start(start_time),5,2)
                if stgs.GE.end_time_winter != "":
                    end_time = t_to_hrs_raw(t_to_mins(stgs.GE.end_time_winter))
                    await client.execute(commands.set_charge_slot_1_end(end_time),5,2)

            elif cmd == "test":
                logger.debug("Test set_mode")

            else:
                logger.error("unknown inverter command: "+ cmd)

            await client.close()

            return

        print("Setting mode", cmd)

        asyncio.run(giv_ctl(cmd))

# End of GivEnergyObjLocal() class definition


class SolcastObj:
    """Stores daily Solcast data."""

    def __init__(self):
        # Skeleton solcast summary array
        self.pv_est10_day: [int] = [0] * 7
        self.pv_est50_day: [int] = [0] * 7
        self.pv_est90_day: [int] = [0] * 7

        self.pv_est10_30: [int] = [0] * 96
        self.pv_est50_30: [int] = [0] * 96
        self.pv_est90_30: [int] = [0] * 96

    def update(self):
        """Updates forecast generation from Solcast."""

        def get_solcast(url) -> Tuple[bool, str]:
            """Download latest Solcast forecast."""

            solcast_url = url + stgs.Solcast.cmd + "&api_key="+ stgs.Solcast.key
            try:
                resp = requests.get(solcast_url, timeout=5)
                resp.raise_for_status()
            except requests.exceptions.RequestException as error:
                logger.error(error)
                return False, ""
            if 299 < resp.status_code < 200:
                logger.error("Invalid response: "+ str(resp.status_code))
                return False, ""

            if len(resp.content) < 50:
                logger.warning("Warning: Solcast data missing/short")
                logger.warning(resp.content)
                return False, ""

            solcast_data = json.loads(resp.content.decode('utf-8'))
            logger.debug(str(solcast_data))

            return True, solcast_data
        #  End of get_solcast()

        # Download latest data for each array, abort if unsuccessful
        result, solcast_data_1 = get_solcast(stgs.Solcast.url_se)
        if not result:
            logger.warning("Error; Problem with Solcast data, using previous values (if any)")
            return

        if stgs.Solcast.url_sw != "":  # Two arrays are specified
            logger.info("url_sw = '"+str(stgs.Solcast.url_sw)+"'")
            result, solcast_data_2 = get_solcast(stgs.Solcast.url_sw)
            if not result:
                logger.warning("Error; Problem with Solcast data, using previous values (if any)")
                return
        else:
            logger.info("No second array")

        logger.info("Successful Solcast download.")

        # Combine forecast for PV arrays & align data with day boundaries
        pv_est10 = [0] * 10080
        pv_est50 = [0] * 10080
        pv_est90 = [0] * 10080

        if stgs.Solcast.url_sw != "":  # Two arrays are specified
            forecast_lines = min(len(solcast_data_1['forecasts']), \
                len(solcast_data_2['forecasts'])) - 1
        else:
            forecast_lines = len(solcast_data_1['forecasts']) - 1
        interval = int(solcast_data_1['forecasts'][0]['period'][2:4])
        solcast_offset = t_to_mins(solcast_data_1['forecasts'][0]['period_end'][11:16]) \
            - interval - 60

        # Check for BST and convert to local time to align with GivEnergy data
        if time.strftime("%z", time.localtime()) == "+0100":
            logger.info("Applying BST offset to Solcast data")
            solcast_offset += 60

        i = solcast_offset
        cntr = 0
        while i < solcast_offset + forecast_lines * interval:
            try:
                pv_est10[i] = int(solcast_data_1['forecasts'][cntr]['pv_estimate10'] * 1000)
                pv_est50[i] = int(solcast_data_1['forecasts'][cntr]['pv_estimate'] * 1000)
                pv_est90[i] = int(solcast_data_1['forecasts'][cntr]['pv_estimate90'] * 1000)
            except Exception:
                logger.error("Error: Unexpected end of Solcast data (array #1). i="+ \
                    str(i)+ "cntr="+ str(cntr))
                break

            if i > 1 and i % interval == 0:
                cntr += 1
            i += 1

        if stgs.Solcast.url_sw != "":  # Two arrays are specified
            i = solcast_offset
            cntr = 0
            while i < solcast_offset + forecast_lines * interval:
                try:
                    pv_est10[i] += int(solcast_data_2['forecasts'][cntr]['pv_estimate10'] * 1000)
                    pv_est50[i] += int(solcast_data_2['forecasts'][cntr]['pv_estimate'] * 1000)
                    pv_est90[i] += int(solcast_data_2['forecasts'][cntr]['pv_estimate90'] * 1000)
                except Exception:
                    logger.error("Error: Unexpected end of Solcast data (array #2). i="+ \
                        str(i)+ "cntr="+ str(cntr))
                    break

                if i > 1 and i % interval == 0:
                    cntr += 1
                i += 1

        if solcast_offset > 720:  # Forget about current day as it's already afternoon
            offset = 1440 - 90
        else:
            offset = 0

        i = 0
        while i < 7:  # Summarise daily forecasts
            start = i * 1440 + offset + 1
            end = start + 1439
            self.pv_est10_day[i] = round(sum(pv_est10[start:end]) / 60000, 3)
            self.pv_est50_day[i] = round(sum(pv_est50[start:end]) / 60000, 3)
            self.pv_est90_day[i] = round(sum(pv_est90[start:end]) / 60000, 3)
            i += 1

        i = 0
        while i < 96:  # Calculate half-hourly generation
            start = i * 30 + offset + 1
            end = start + 29
            self.pv_est10_30[i] = round(sum(pv_est10[start:end])/60000, 3)
            self.pv_est50_30[i] = round(sum(pv_est50[start:end])/60000, 3)
            self.pv_est90_30[i] = round(sum(pv_est90[start:end])/60000, 3)
            i += 1

        timestamp = time.strftime("%d-%m-%Y %H:%M:%S", time.localtime())
        logger.info("PV Estimate 10% (hrly, 7 days) / kWh; "+ timestamp+ "; "+
            str(self.pv_est10_30[0:47])+ str(self.pv_est10_day[0:6]))
        logger.info("PV Estimate 50% (hrly, 7 days) / kWh; "+ timestamp+ "; "+
            str(self.pv_est50_30[0:47])+ str(self.pv_est50_day[0:6]))
        logger.info("PV Estimate 90% (hrly, 7 days) / kWh; "+ timestamp+ "; "+
            str(self.pv_est90_30[0:47])+ str(self.pv_est90_day[0:6]))

# End of SolcastObj() class definition

def t_to_mins(time_in_hrs: str) -> int:
    """Convert times from HH:MM format to mins after midnight."""

    try:
        time_in_mins = 60 * int(time_in_hrs[0:2]) + int(time_in_hrs[3:5])
        return time_in_mins
    except Exception:
        return 0

#  End of t_to_mins()

def t_to_hrs(time_in: int) -> str:
    """Convert times from mins after midnight format to HH:MM."""

    try:
        hours = int(time_in // 60)
        mins = int(time_in - hours * 60)
        time_in_hrs = '{:02d}{}{:02d}'.format(hours, ":", mins)
        return time_in_hrs
    except Exception:
        return "00:00"

#  End of t_to_hrs()

def t_to_hrs_raw(time_in: int) -> int:
    """Convert times from mins after midnight format to HH:MM."""

    try:
        hours = int(time_in // 60)
        mins = int(time_in - hours * 60)
        time_in_hrs = '{:02d}{:02d}'.format(hours, mins)
        return int(time_in_hrs)
    except Exception:
        return 0

#  End of t_to_hrs_raw()

# End of palm_utils
