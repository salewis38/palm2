"""Example settings for PALM.

Copy to `palm_settings.py` (which is git-ignored) and edit. Keep secrets in environment
variables rather than in the file where possible.
"""
import os


class pg:  # runtime flags - set from the command line, leave as-is
    test_mode = False
    debug_mode = False
    once_mode = False
    execute_mode = False
    mode_cmd = ""
    loop_counter = 0
    # Directories (all optional). Precedence: these settings, then systemd's $STATE_DIRECTORY /
    # $RUNTIME_DIRECTORY (set by StateDirectory=/RuntimeDirectory= in deploy/palm.service), then ~/.palm.
    # state_dir = "/var/lib/palm"        # persistent: PVOutput retry spool
    # runtime_dir = "/run/palm"          # volatile (tmpfs): lock file


class GE:  # GivEnergy inverter and tariff
    local_ip = "192.168.1.50"
    local_port = 8899
    start_time = "23:30"                  # off-peak start, HH:MM
    end_time = "05:30"                    # off-peak end, HH:MM
    charge_rate = 2.6                     # kW, 0.1 - 5.1
    discharge_rate = 2.6                  # kW, 0.1 - 5.1
    batt_capacity = 9.5                   # kWh
    batt_utilisation = 0.9                # usable fraction
    ev_power_threshold = 1500             # W drawn from grid before the EV counts as charging
    winter = [11, 12, 1, 2]               # months
    shoulder = [3, 10]
    pm_export_start = "17:00"             # "" disables evening export
    # batt_power_attr = "p_battery"       # optional: register used for battery power (default p_inverter_out)


class PVOutput:
    enable = False
    url = "https://pvoutput.org/service/r2"
    key = os.environ.get("PVOUTPUT_API_KEY", "")
    sid = os.environ.get("PVOUTPUT_SYSTEM_ID", "")
    # Retry spool for failed uploads. Default: <state dir>/pvoutput_spool.jsonl (append-only, written only
    # while PVOutput is unreachable). To keep it in RAM instead (zero SD wear; lost on reboot):
    # spool_file = "/run/palm/pvoutput_spool.jsonl"


class Shelly:
    em0_url = "http://192.168.1.60/status"    # EV power meter (None to disable)
    sw1_url = "http://192.168.1.61"           # heater switch (None to disable)


class CarbonIntensity:
    enable = False
    url = "https://api.carbonintensity.org.uk/regional/postcode"
    PostCode = "RG10"                         # outward code only


class OpenWeatherMap:
    enable = False
    url = "https://api.openweathermap.org/data/3.0"
    payload = {"lat": 51.5, "lon": -0.1, "exclude": "minutely,hourly,daily", "appid": os.environ.get("OWM_API_KEY", "")}
