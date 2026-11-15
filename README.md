# PALM - PV Active Load Manager

Local Modbus control of a GivEnergy inverter, with Shelly EV/heater integration and
PVOutput.org uploads. See the changelog in `palm.py`.

## Layout

| Path | Purpose |
|---|---|
| `palm.py` | The application |
| `palm_gui.py` | The status page (one HTML/CSS/JavaScript string). Optional: sits next to `palm.py`; without it a minimal page is served |
| `palm_settings.example.py` | Copy to `palm_settings.py` (git-ignored) and edit |
| `tests/` | Unit tests (standard library `unittest`; no hardware or network needed) |
| `.github/workflows/ci.yml` | GitHub Actions: Python 3.10-3.13 on Ubuntu 24.04 (blocking) plus an informational Ubuntu 26.04 job |
| `.github/dependabot.yml` | Monthly pull requests to keep the actions used by CI on supported versions |
| `deploy/palm.service` | systemd unit (notify, watchdog, restart policy, SD-friendly writable paths) |
| `tools/measure_wear.py` | Prints what PALM writes to disk and logs in a simulated day |
| `tools/stdlib_coverage.py` | Dependency-free coverage report (Python 3.12+) |

## Running the tests

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt          # httpx, coverage, ruff
python -m unittest discover -s tests -v

# with coverage
coverage run --source=. --omit="tests/*,palm_settings*.py" -m unittest discover -s tests
coverage report -m
```

The tests never import your real `palm_settings.py` or the real Modbus library. They install a
fake settings module and a stub `givenergy_modbus`, and drive PALM through fakes for the inverter
client, Shelly and PVOutput HTTP calls. If your application file is not called `palm.py`, run
the tests with `PALM_MODULE=your_module_name`.

## Status web page

PALM can serve everything it knows on a small read-only web server, built into `palm.py` (standard library only; no
extra packages, nothing written to disk). The page itself lives in **`palm_gui.py`**, which sits next to `palm.py`;
copy both files when you deploy. It is **off by default**. Enable it in `palm_settings.py`:

```python
class Web:
    enable = True
    host = "127.0.0.1"      # this machine only; "0.0.0.0" to reach it from your LAN
    port = 8080
    token = ""              # optional shared secret (strongly recommended once host is not 127.0.0.1)
    history_hours = 24      # chart history, kept in RAM only (0 = none)
```

Restart PALM, then browse to `http://<pi-address>:8080/` (or `curl http://localhost:8080/status.json`).

| URL | What it returns |
|---|---|
| `/` | A live page: battery, solar, load, grid, EV, carbon and temperature; control state; Pi uptime and load averages; health; 24 h of charts. Refreshes itself. |
| `/status.json` | Everything as JSON: service, host system stats (`uptime_s`, `load1`/`load5`/`load15`, `cpus`), inverter, EV, environment, control state, PVOutput queue, failing conditions. |
| `/history.json` | The rolling history as parallel lists (`t`, `pv`, `load`, `grid`, `battery`, `soc`, `ev`), one sample a minute. |
| `/metrics` | Prometheus text format, for Grafana or similar. |
| `/healthz` | `ok`, always without a token, for uptime checks. It carries no information. |

* **`palm_gui.py` is optional.** `palm.py` loads it only when the server starts. If it is missing, a minimal page that
  points at the JSON and metrics is served instead (with a warning in the log); the controller, `/status.json` and
  `/metrics` are unaffected. It contains data only (one string), so there is nothing in it to run or review for behaviour.
* **System stats** are the host's uptime and 1/5/15-minute load averages (with the core count; the page marks a load
  above the number of cores as high). They come from `/proc/uptime` and `os.getloadavg()`: reads from the kernel, no disk
  access. `/metrics` has `palm_system_uptime_seconds`, `palm_system_load1/5/15` and `palm_system_cpus`.
* **Data comes from memory.** Handlers only read snapshots PALM already holds, so a page load can never slow the control
  loop, and a failure to bind the port is logged and ignored: control carries on. History (about 0.5 MB for 24 h) is lost
  on restart; PVOutput keeps the long-term record. Grid power is shown as PALM reads it (positive = import).
* **Security.** Read-only (GET and HEAD only; there are no controls on it) and no credentials or addresses of other
  devices ever appear in it. It listens on **loopback only** unless you set `host`. If you do:
  * set `Web.token` (`Authorization: Bearer <token>` or `?token=<token>`; the page passes it on to its own requests);
  * restrict the port to your LAN, for example `sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp`;
  * the Host header must be an IP address, `localhost`, a single-label name (`raspberrypi`) or `*.local`/`*.lan`/
    `*.home.arpa`, or listed in `Web.allowed_hosts`. This defeats DNS-rebinding attacks, in which a web site you visit
    tries to read a service on your LAN through your browser;
  * limits: 8 simultaneous connections, 5 s per request, small request and header sizes, one request per connection.
  * It is plain HTTP: the token is visible to anyone who can watch your network traffic. For anything beyond a trusted
    home LAN use a VPN (Tailscale, WireGuard) or a TLS reverse proxy (Caddy) in front of it, and **never port-forward it**.
* **Prometheus:** `scrape_configs: [{job_name: palm, static_configs: [{targets: ["raspberrypi.local:8080"]}]}]`, plus
  `authorization: {credentials: "<token>"}` if a token is set.
* **Tests:** `tests/test_web.py` (protocol, limits, token, Host header, metrics, snapshot, integration) and
  `tests/test_web_page.py`, which runs `palm_gui.py`'s JavaScript in Node against the real server output (skipped if Node
  is not installed).

## Overnight register refresh

When the overnight (off-peak) charge window opens (`GE.start_time`), PALM re-asserts the inverter settings, so a
change made by another program (a phone app, a script, another integration) cannot carry into the overnight charge.

* **What is sent:** the command for the state PALM currently wants. Normally that is `play`, which rewrites the charge
  and discharge slots, charge target, charge/discharge limits and the charge/discharge enables from `palm_settings.py`.
* **When:** once per window, on the first control cycle after the window opens. A restart inside the window counts as
  the re-assert; a restart outside it does not. If the inverter is unreachable it is retried with backoff until it works.
* **Check before rewrite:** a full register read first, then one log line: either
  `Inverter settings match PALM's play settings (checked: enable_charge, enable_discharge)` or a WARNING such as
  `... differ from PALM's play settings (changed by another program?): enable_charge is False, expected True`.
  Only the registers PALM verifies can be compared; the rewrite that follows covers all of them.
* **Always on:** there is no setting for it. (`GE.refresh_at_offpeak_start` from v2.0.6 has been removed; if it is still
  in your `palm_settings.py` it is simply ignored.)
* **Evening export ends when the window opens:** export (`PEAK_SHAVE`) is never active inside the off-peak window, so
  at `GE.start_time` the inverter goes back to `play` (that change is the re-assert) and the overnight charge proceeds.
  Export can resume from `GE.pm_export_start` the next evening.

## SD-card wear (Raspberry Pi)

When everything is healthy PALM writes **nothing** to disk: no state files, no cache, no log file; only INFO
lines to stderr (about 300 lines, roughly 70 KB, a day; one of them is the
`Data; Write to pvoutput.org; ...` summary after each upload). Writes happen only when something is wrong, and are kept small:

| What | Behaviour |
|---|---|
| PVOutput retry spool | Append-only: one ~200 byte line per failed upload, no fsync, no temp file, no rename. Rewritten only when entries are sent, or once the file reaches twice the queue cap. Torn or corrupt lines are skipped on load, so no durability tricks are needed. |
| Repeating failures | Logged once, then at most one summary line per 15 minutes, plus a "restored" line. Suppressed repeats remain visible at DEBUG (`-d`). |
| Lock file | `$RUNTIME_DIRECTORY` (tmpfs under systemd), not the SD card. |
| Persistent state | `$STATE_DIRECTORY` (`/var/lib/palm`), only the spool. Or set `PVOutput.spool_file = "/run/palm/..."` to keep even that in RAM. |
| Restart loops | `RestartPreventExitStatus=2 3` in the unit: bad settings and a second instance no longer restart forever. |

Measure it yourself (no hardware needed), and see the thresholds enforced in `tests/test_wear.py` and
`tests/test_logging_throttle.py`:

```bash
python tools/measure_wear.py
```

Beyond PALM, the biggest SD-card wear sources on a Pi are usually the OS: swap (disable `dphys-swapfile` or use
zram), a persistent journal (`journalctl --disk-usage`; cap it with `SystemMaxUse=` in a journald drop-in),
missing `noatime`, and apt timers. To see where writes really come from on your Pi:

```bash
awk '{printf "%.0f MB written since boot\n", $7*512/1048576}' /sys/block/mmcblk0/stat
sudo iotop -oPa -d 60            # accumulated writes per process (or: sudo pidstat -d 60)
journalctl -u palm --since "24 hours ago" | wc -l
```

### Coverage report

```bash
python tools/stdlib_coverage.py                 # Python 3.12+, no dependencies; writes ./coverage-report/
python tools/stdlib_coverage.py --fail-under 90 # optional gate
```

This writes `coverage-report/index.html` (annotated source), `COVERAGE.md` and `coverage.json`.
It measures statements and branch outcomes for `palm.py` using `sys.monitoring`; its line hits were
cross-checked against the standard-library `trace` module (identical). Figures differ slightly from
coverage.py (which CI also runs) because branch measurement is defined differently.

### What is covered

* **Control logic** - desired-vs-applied state machine, retry/backoff, EV detection and hold windows
  (including across midnight), evening export rules, heater handling, shutdown to a safe state.
* **Inverter I/O** - register mapping, sanity checks, command sequencing and read-back verification,
  failure paths, lock behaviour, stop-aware waits.
* **PVOutput** - payload shape, secrets only in headers, spool and replay in order, rate-limit and
  auth handling, permanent-rejection handling, restart persistence.
* **Shelly / weather / CO2** - parsing, malformed payloads, retries, staleness.
* **Status web server** - requests, limits, token and Host-header checks, snapshot/metrics contents, the page's JavaScript.
* **Service and CLI** - one-shot modes never command the inverter unexpectedly, crash and exit
  codes, instance lock, watchdog notifications, settings validation.

### What is NOT covered

Nothing here exercises a real inverter, so these remain to be checked on your hardware:
register attribute names used for verification (`enable_charge`, `enable_discharge`,
`enable_charge_target`, `battery_pause_mode`), the battery-power register, and real PVOutput
acceptance of the payload. Run `python palm.py -t` (test mode: no writes) and then
`python palm.py -o` against the real inverter before enabling the service.

## Getting this into GitHub

```bash
git clone https://github.com/<you>/<repo>.git && cd <repo>
git checkout -b tests-and-ci
# copy the contents of this archive into the repo root (palm.py replaces the old version)
git rm --cached palm_settings.py 2>/dev/null || true     # only matters if it was ever tracked
git add .
git commit -m "Add test suite and CI"
git push -u origin tests-and-ci
# open a pull request; the CI workflow runs on it
```

### CI runners and action versions

* **Runner images are pinned** (`ubuntu-24.04`), not `ubuntu-latest`, which GitHub is moving to Ubuntu 26.04 between
  19 October and 19 November 2026. A separate **Ubuntu 26.04 job** runs on Python 3.12 and never fails the run; once
  it has been green for a while, add `ubuntu-26.04` to the `os` list in `ci.yml` and delete the `include` entry.
* **Actions use their Node 24 releases** (`checkout@v6`, `setup-python@v6`, `upload-artifact@v6`). GitHub is removing the
  Node 20 runtime, and older majors print "Node.js 20 is deprecated". Dependabot opens a monthly pull request when newer
  versions appear, and `tests/test_workflow.py` fails if a runner label floats or an action drops back to a Node 20 major.
* Reports and artifacts come from the 24.04 baseline only, so two matrix jobs never upload an artifact with the same name.

CI refuses to pass if `palm_settings.py` is tracked. If it was ever committed, its keys remain in
git history: rotate the PVOutput and OpenWeatherMap keys.

The lint step is `continue-on-error` because it has not yet been run against this code base; remove
that line once `ruff check .` is green.
