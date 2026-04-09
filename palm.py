#!/usr/bin/env python3
"""PALM - PV Active Load Manager."""

import sys
import time
import threading
import json
from urllib.parse import urlencode
import logging
import requests
from palm_utils import GivEnergyObjLocal, SolcastObj, t_to_mins, t_to_hrs
import palm_settings as stgs

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
# v1.1.0    06/Aug/23 Split out generic functions as palm_utils.py
# v1.1.1    19/Nov/23 Updated to Shelly Gen 2 switch, improved readability
# v1.1.2    03/Dec/23 Added Shelly switch to load balancing, updated Events logic for robustness
# v1.1.3    01/Jan/24 Added routine to update PVOutput daily stats with IO Smart Charge periods
# v1.1.3a   28/Jan/24 Revise PVOutput write timing to improve alignment of inverter and local data
# v1.1.3b   28/Mar/24 Remove manual hold, fixed in new AC3 firmware. Remove v3 from PVO payload
# v1.1.4    21/Apr/24 Tidied up Agile Export
# v1.1.4a   09/May/24 Minor bugfix on resummarise to account for further NaN in received data
# v1.1.5    12/Oct/25 Revised EV charging logic to avoid morning battery discharge
# v2.0.0    02/Apr/26 Move to local control and significantly rationalise functionality

PALM_VERSION = "v2.0.0"
# -*- coding: utf-8 -*-
# pylint: disable=logging-not-lazy
# pylint: disable=consider-using-f-string

class EnvObj:
    """Stores environmental info - weather, CO2, etc."""

    def __init__(self):
        self.co2_intensity: int = 200
        self.co2_high: bool = False
        self.temp_deg_c: float = 15
        self.weather: [str] = []
        self.weather_symbol: str = "0"
        self.current_weather: [str] = []
        self.sunshine: int = 0
        self.sr_time: str = "06:00"
        self.virt_sr_time: str = "09:00"
        self.ss_time: str = "21:00"
        self.virt_ss_time: str = "21:00"

    def update_co2(self):
        """Import latest CO2 intensity data."""

        timestring = time.strftime("%Y-%m-%dT%H:%MZ", time.localtime())
        url = stgs.CarbonIntensity.url + timestring + stgs.CarbonIntensity.RegionID

        headers = {
            'Accept': 'application/json'
        }

        try:
            resp = requests.get(url, params={}, headers=headers, timeout=10)
            resp.raise_for_status()
        except requests.exceptions.RequestException as error:
            logger.warning("Warning: Problem obtaining CO2 intensity: "+ str(error))
            return

        if len(resp.content) < 50:
            logger.warning("Warning: Carbon intensity data missing/short")
            return

        co2_intens_raw: int = []
        co2_intens_raw = json.loads(resp.content.decode('utf-8'))['data']['data']

        self.co2_intensity = co2_intens_raw[0]['intensity']['forecast']

        co2_intens_near = 0
        co2_intens_far = 0
        i = 0
        try:
            while i < 5:
                co2_intens_near += int(co2_intens_raw[i]['intensity']['forecast']) / 5
                co2_intens_far += int(co2_intens_raw[i + 6]['intensity']['forecast']) / 5
                i += 1
            co2_intens_near = round(co2_intens_near, 0)
            co2_intens_far = round(co2_intens_far, 0)

        except Exception as error:
            logger.warning("Warning: Problem calculating CO2 intensity trend: "+ str(error))

        self.co2_high = co2_intens_far > 1.3 * co2_intens_near or \
            co2_intens_far > stgs.CarbonIntensity.Threshold and \
            co2_intens_far > co2_intens_near

        logger.debug(str(co2_intens_raw))
        logger.debug("CO2 Intensity: "+ str(self.co2_intensity)+ str(co2_intens_near)+
            str(co2_intens_far)+ str(self.co2_high))

    # def check_sr_ss(self) -> bool:
    #     """Adjust sunrise and sunset to reflect actual conditions"""
    #
    #     new_virt_sr_ss = False
    #     pwr_threshold = stgs.PVData.PwrThreshold
    #     if stgs.pg.t_now_mins < t_to_mins(env_obj.virt_sr_time):  # Gen started?
    #         if (inverter.sys_status[1]['solar']['power'] < pwr_threshold <
    #             inverter.sys_status[0]['solar']['power']):
    #             new_virt_sr_ss = True
    #             self.virt_sr_time = inverter.sys_status[0]['time'][11:]
    #             logger.info("VSunrise/set (Sunrise detected) VSR: " +
    #                   str(env_obj.virt_sr_time)+ " VSS: "+ str(env_obj.virt_ss_time))
    #     elif stgs.pg.t_now_mins > 900:  # It's afternoon, gen ended?
    #         if (inverter.sys_status[0]['solar']['power'] < pwr_threshold and \
    #             (pwr_threshold < inverter.sys_status[1]['solar']['power'] or \
    #             stgs.pg.loop_counter < 10)):
    #             new_virt_sr_ss = True
    #             self.virt_ss_time = inverter.sys_status[0]['time'][11:]
    #             logger.info("VSunrise/set (Sunset detected) VSR: " +
    #                   str(env_obj.virt_sr_time)+ " VSS: "+ str(env_obj.virt_ss_time))
    #         elif (inverter.sys_status[0]['solar']['power'] > 2 * pwr_threshold >
    #             inverter.sys_status[1]['solar']['power']):
    #             # False alarm - sun back up (added hysteresis to threshold)
    #             new_virt_sr_ss = True
    #             self.virt_ss_time = env_obj.ss_time
    #             logger.info('VSunrise/set (False alarm) VSR:' +
    #                   str(env_obj.virt_sr_time)+ " VSS:"+ str(env_obj.virt_ss_time))
    #     return new_virt_sr_ss
    #
    # def reset_sr_ss(self):
    #     """Reset sunrise & sunset each day."""
    #
    #     self.sr_time: str = "06:00"
    #     self.virt_sr_time: str = "09:00"
    #     self.ss_time: str = "21:30"
    #     self.virt_ss_time: str = "21:30"

    def update_weather_curr(self):
        """Download latest weather from OpenWeatherMap."""

        url = stgs.OpenWeatherMap.url + "onecall"
        payload = stgs.OpenWeatherMap.payload

        try:
            resp = requests.get(url, params=payload, timeout=5)
            resp.raise_for_status()
        except requests.exceptions.RequestException as error:
            logger.error(error)
            return

        if len(resp.content) < 50:
            logger.warning("Warning: Weather data missing/short")
            logger.warning(resp.content)
            return

        current_weather = json.loads(resp.content.decode('utf-8'))
        logger.debug(str(current_weather))
        self.current_weather = current_weather

        self.temp_deg_c = round(current_weather['current']['temp'] - 273, 1)
        self.weather_symbol = current_weather['current']['weather'][0]['id']

# End of EnvObj() class definition


def set_shelly_switch(base_url: str, turn_on: bool) -> bool:
    """Operates a Shelly Plus 1 (Gen 2) switch on/off."""

    if turn_on:
        sw_cmd = "on"
    else:
        sw_cmd = "off"

    url:str = base_url + "relay/0/?turn=" + sw_cmd

    try:
        resp = requests.put(url, timeout=5)
        resp.raise_for_status()
    except requests.exceptions.RequestException as error:
        logger.error(str(error))
        return False

    return True

#  End of set_shelly_switch()


def read_shelly_switch(base_url: str) -> str:
    """Reads Shelly Plus 1 (Gen 2) switch value"""

    url:str = str(base_url) + "rpc/Input.GetStatus?id=0"

    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
    except requests.exceptions.RequestException as error:
        logger.error("Missing response from Shelly EM: "+ str(error))
        return "Error"

    parsed = json.loads(resp.content.decode('utf-8'))
    logger.debug(str(parsed))

    if parsed['state'] is True:
        return "On-stat"
    return "Off"

# End of read_shelly_switch()


class EVObj:
    """Reports status and stores instantaneous measured power to EV using Shelly EM"""

    def __init__(self):
        self.power: int = 0
        self.power_last: int = 0
        self.active_now: bool = False
        self.active_last: bool = False
        self.active: bool = False
        self.confirmed_active: bool = False

    def charging(self) -> bool:
        """Polls Shelly EM and updates status"""

        url:str = str(stgs.Shelly.em0_url)
        if url == "":
            return False

        try:
            resp = requests.put(url, timeout=5)
            resp.raise_for_status()
        except requests.exceptions.RequestException as error:
            logger.error("Missing response from Shelly EM"+ str(error))
            return False

        parsed = json.loads(resp.content.decode('utf-8'))
        logger.debug(str(parsed))

        if parsed['is_valid'] is True:
            self.power_last = self.power
            self.power = int(parsed['power'])
            self.active_last = self.active_now
            # active_now is used to control inverter response. Ingore daytime PV trickle charging
            self.active_now = self.power > max(inverter.pv_power, 1000) and inverter.soc < 95
            if self.active_now is True and self.active_last is False:  # Edge detect
                logger.warning("EV charging detected, power = "+ str(parsed['power']))
        self.active = self.active_last and self.active_now
        return self.active
    # End of charging()

# End of EVObj


def put_pv_output():
    """Upload generation/consumption data to PVOutput.org."""

    url = stgs.PVOutput.url + "addstatus.jsp"
    key = stgs.PVOutput.key
    sid = stgs.PVOutput.sid

    # Backdate measurements by 60 seconds
    post_date = time.strftime("%Y%m%d", time.localtime(time.time() - 60))
    post_time = time.strftime("%H:%M", time.localtime(time.time() - 60))

    batt_power_out = inverter.batt_power if inverter.batt_power > 0 else 0
    batt_power_in = -1 * inverter.batt_power if inverter.batt_power < 0 else 0
    total_cons = inverter.consumption - inverter.batt_power
    load_pwr = total_cons  # if total_cons > 0 else 0

    if stgs.pg.test_mode is True:
        print("### TEST Inverter read time: ", t_to_hrs(inverter.read_time_mins))

    payload = {
        "t"   : post_time,
        "key" : key,
        "sid" : sid,
        "d"   : post_date
    }

    part_payload = {
        "v2"  : inverter.pv_power,
        "v4"  : load_pwr,
        "v5"  : env_obj.temp_deg_c,
        "v6"  : inverter.line_voltage,
        "v7"  : ev.power_last,
        "v8"  : batt_power_out,
        "v9"  : env_obj.co2_intensity,
        "v10" : CO2_USAGE_VAR,
        "v11" : batt_power_in,
        "v12" : inverter.line_frequency,
        "b1"  : inverter.batt_power * -1,
        "b2"  : inverter.soc,
        "b3"  : int(stgs.GE.batt_capacity * stgs.GE.batt_utilisation *1000),
        "b4"  : inverter.e_battery_charge_total,
        "b5"  : inverter.e_battery_discharge_total
    }

    payload.update(part_payload)  # Concatenate the data, don't escape ":"
    payload = urlencode(payload, doseq=True, quote_via=lambda x,y,z,w: x)

    time.sleep(2)  # PVOutput has a 1 second rate limit. Avoid any clashes

    if not stgs.pg.test_mode:
        try:
            resp = requests.get(url, params=payload, timeout=10)
            resp.raise_for_status()
        except requests.exceptions.RequestException as error:
            logger.warning("PVOutput Write Error "+ stgs.pg.long_t_now)
            logger.warning(error)
            return()

    logger.info("Data; Write to pvoutput.org; "+ post_date+"; "+ post_time+ "; "+ str(part_payload))
    return()

#  End of put_pv_output()


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
        self.resumm_pvoutput: bool = False
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

            # 5 minutes before off-peak start and 1hr before off-peak ends
            self.update_pv_fcast = \
                ((stgs.pg.test_mode or stgs.pg.once_mode) and stgs.pg.loop_counter == 1) or \
                t_now == (t_to_mins(stgs.GE.start_time) + 1435) % 1440 or \
                t_now == (t_to_mins(stgs.GE.end_time) + 1375) % 1440

            # 2 minutes before off-peak start for setting overnight battery charging target
            self.update_soc = \
                ((stgs.pg.test_mode or stgs.pg.once_mode) and stgs.pg.loop_counter == 2) or \
                t_now == (t_to_mins(stgs.GE.start_time) + 1438) % 1440 or \
                t_now == (t_to_mins(stgs.GE.end_time) + 1380) % 1440

            # Repeat 60 mins before end of off-peak in case of Solcast fine-tuning
            self.update_soc_pass_2 = \
                t_now == (t_to_mins(stgs.GE.end_time) + 1380) % 1440

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
            events.winter is False and events.shoulder is False
        if stgs.GE.pm_export_start != "":
            self.pm_export_start = t_now == t_to_mins(stgs.GE.pm_export_start)

        # Afternoon boost options
        elif stgs.GE.boost_start != "" and stgs.GE.boost_finish != "":
            self.pm_boost_start = self.winter or self.shoulder is True and \
                t_now == t_to_mins(stgs.GE.boost_start)
            self.pm_boost_end = self.winter or self.shoulder is True and \
                t_now == t_to_mins(stgs.GE.boost_finish)

        # Summarise daily data at PVOutput.org
        self.resumm_pvoutput = stgs.PVOutput.enable is True and \
                    stgs.Shelly.em0_url != "" and \
                    (stgs.pg.test_mode and stgs.pg.loop_counter == 4 or \
                    stgs.pg.t_now_mins == 1420)

        # Update carbon intensity and weather every 15 mins
        self.update_carbon_intensity = \
            stgs.CarbonIntensity.enable is True and stgs.pg.loop_counter % 15 == 14
        self.update_weather = \
            stgs.OpenWeatherMap.enable is True and stgs.pg.loop_counter % 15 == 14

# End of EventsObj

if __name__ == '__main__':

    # Parse any command-line arguments

    MESSAGE = ""
    if len(sys.argv) > 1:
        if str(sys.argv[1]) in ["-t", "--test"]:
            stgs.pg.test_mode = True
            stgs.pg.debug_mode = True
            MESSAGE = "Running in test mode... 5 sec loop time, no external server writes"
        elif str(sys.argv[1]) in ["-d", "--debug"]:
            stgs.pg.debug_mode = True
            MESSAGE = "Running in debug mode, extra verbose"
        elif str(sys.argv[1]) in ["-o", "--once"]:
            stgs.pg.once_mode = True
            MESSAGE = "Running in once mode, execute forecast and inverter SoC update, then exit"

    if stgs.pg.debug_mode:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("PALM")
    #logger.basicConfig(filename='palm_log_test.txt', encoding='utf-8', level=logger.DEBUG)

    logger.critical("PALM... PV Automated Load Manager Version: "+ PALM_VERSION)
    logger.critical("Command line options (only one can be used):")
    logger.critical("-t | --test  | test mode (12x speed, no external server writes)")
    logger.critical("-d | --debug | debug mode, extra verbose")
    logger.critical("-o | --once  | once mode, updates inverter SoC target and then exits")
    logger.critical("")
    if MESSAGE != "":
        logger.critical(MESSAGE)

    EV_ACTIVE_VAR: bool = False
    FORCE_DISCHARGE_VAR: bool = False

    while True:  # Main Loop
        # Current time definitions
        stgs.pg.long_t_now: str = time.strftime("%d-%m-%Y %H:%M:%S %z", time.localtime())
        stgs.pg.month: str = stgs.pg.long_t_now[3:5]
        stgs.pg.t_now: str = stgs.pg.long_t_now[11:]
        stgs.pg.t_now_mins: int = t_to_mins(stgs.pg.t_now)

        if stgs.pg.loop_counter == 0:  # Initialise
            logger.critical("Initialising at: "+ stgs.pg.long_t_now)
            logger.critical("")
            sys.stdout.flush()

            # Initialise event semaphores
            events: EventsObj = EventsObj()

            # Object to capture EV charging status/current
            ev: EVObj = EVObj()

            # GivEnergy power object
            inverter: GivEnergyObjLocal = GivEnergyObjLocal()
            time.sleep(10)

            # if stgs.pg.once_mode is False:
            #     if stgs.pg.month in stgs.GE.winter:
            #         inverter.set_mode("set_soc_winter")
            #     else:
            #         inverter.set_mode("set_soc")

            # Solcast PV prediction object
            pv_forecast: SolcastObj = SolcastObj()

            # Misc environmental data: weather, CO2, etc
            CO2_USAGE_VAR: int = 0
            env_obj: EnvObj = EnvObj()
            if stgs.CarbonIntensity.enable is True:
                env_obj.update_co2()
            if stgs.OpenWeatherMap.enable is True:
                env_obj.update_weather_curr()

        else:
            # Schedule activities at specific intervals
            events.update()

            if events.update_pv_fcast is True:
                try:
                    pv_forecast.update()
                except Exception:
                    logger.warning("Warning; Solcast download failure")

            if stgs.pg.once_mode is False:

                # Reset sunrise and sunset for next day
                # env_obj.reset_sr_ss()

                # Poll car charger during additional Intelligent Octopus slots
                # If car is charging, either pause or charge inverter, depending on battery state
                # A Shelly switch also overrides the UFH thermostat in winter months to force on
                EV_ACTIVE_VAR = ev.charging()

                # Detect state changes on EV charger.
                # Updated 09/03/2025 for latest Ohme charger operation with IOG
                # Updated 12/10/2025 for IOG is charging EV through off-peak transition
                # Threshold increased in ev.confirmed_active to ignore car charging from PV
                # Remove off_peak condition for start of battery charge to avoid early discharge
                # Stop charging if EV charging stops outside off peak window
                if EV_ACTIVE_VAR is True and events.off_peak is False:
                    if ev.confirmed_active is False:  # Off-peak charging
                        ev.confirmed_active = True
                        logger.info("EV charging: enabling battery boost at "+ \
                            stgs.pg.long_t_now)
                        inverter.set_mode("charge_now")
                        if env_obj.temp_deg_c < 15 and stgs.pg.t_now_mins < t_to_mins("21:30"):
                            # Force heating on
                            set_shelly_switch(stgs.Shelly.sw1_url, True)

                    # if events.off_pk_end is True:  # EV charging through off-peak transition
                    #     inverter.set_mode("pause_discharge")
                    #
                elif EV_ACTIVE_VAR is False:  # EV not charging
                    if ev.confirmed_active is True:  # Charging just stopped
                        if stgs.pg.t_now_mins % 30 < 3 and events.off_pk is False:  # Timeslot end?
                            ev.confirmed_active = False
                            logger.info("EV charging inactive, resuming ECO battery mode at "+ \
                                stgs.pg.long_t_now)
                            inverter.set_mode("play")
                        if (events.off_pk_start or stgs.pg.t_now_mins % 30 < 3) and \
                            read_shelly_switch(stgs.Shelly.sw1_url) == "Off":  # Thermostat inactive
                            set_shelly_switch(stgs.Shelly.sw1_url, False)  # Disable override

                    # If export finish time is set, delay summer charging to maximise AM export
                    if events.off_pk_end is True and stgs.GE.am_export_finish != "" and \
                        events.winter is False and events.shoulder is False:
                        inverter.set_mode("pause_charge")
                    if events.am_export_finish:
                        inverter.set_mode("play")

                    # Export excess charge during evening peak if heating not likely to be used
                    if events.pm_export_start and inverter.soc > 80 and env_obj.temp_deg_c > 16:
                        inverter.set_mode("discharge_now")
                        FORCE_DISCHARGE_VAR = True
                    if FORCE_DISCHARGE_VAR and 0 < inverter.soc < 50:
                        inverter.set_mode("play")
                        FORCE_DISCHARGE_VAR = False

                    # PM battery boost in shoulder/winter months for Cosy Octopus, etc
                    if events.pm_boost_start is True:
                        logger.info("Enabling afternoon battery boost")
                        inverter.tgt_soc = int(stgs.GE.max_soc_target)
                        inverter.set_mode("charge_now_soc")

                    if events.pm_boost_end is True:
                        inverter.set_mode("set_soc")  # Set inverter for next timed charge period


                # Update carbon intensity every 15 mins as background task
                if events.update_carbon_intensity is True:
                    do_get_carbon_intensity = threading.Thread(target=env_obj.update_co2())
                    do_get_carbon_intensity.daemon = True
                    do_get_carbon_intensity.start()


                # Update weather every 15 mins as background task
                if events.update_weather is True:
                    do_get_weather = threading.Thread(target=env_obj.update_weather_curr())
                    do_get_weather.daemon = True
                    do_get_weather.start()

                #  Refresh utilisation data from GivEnergy server. Check every 5 minutes
                inverter.get_latest_data()
                CO2_USAGE_VAR = int(env_obj.co2_intensity * inverter.grid_power / 1000)

                if stgs.pg.t_now_mins > inverter.read_time_mins + 7:
                    logger.critical("Inverter last seen at: "+ t_to_hrs(inverter.read_time_mins))

                # Publish data to PVOutput.org
                if stgs.PVOutput.enable is True and \
                    (stgs.pg.test_mode or \
                    stgs.pg.t_now_mins == inverter.read_time_mins + 1 or \
                    stgs.pg.loop_counter > stgs.pg.pvo_tstamp + 4):

                    stgs.pg.pvo_tstamp = stgs.pg.loop_counter
                    if stgs.pg.t_now_mins < 6:  # Reset totals to avoid PVOutput carry-over issue
                        inverter.pv_energy = 0
                        inverter.grid_energy = 0
                    do_put_pv_output = threading.Thread(target=put_pv_output)
                    do_put_pv_output.daemon = True
                    do_put_pv_output.start()

        stgs.pg.loop_counter += 1

        if stgs.pg.t_now_mins == 0:  # Reset frame counter every 24 hours
            stgs.pg.loop_counter = 1

        if stgs.pg.test_mode or stgs.pg.once_mode:  # Wait 5 seconds
            time.sleep(5)
        else:  # Sync to minute rollover on system clock
            CURRENT_MINUTE = int(time.strftime("%M", time.localtime()))
            while int(time.strftime("%M", time.localtime())) == CURRENT_MINUTE:
                time.sleep(10)

        sys.stdout.flush()
# End of main
