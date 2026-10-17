import asyncio
import contextlib
import io
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support import FakeClient, FakeCommands, PalmAsyncCase, sane_inverter


class InverterCase(PalmAsyncCase):
    def setUp(self):
        super().setUp()
        self.client = FakeClient()
        patcher = mock.patch.object(self.palm, "Client", lambda ip, port: self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self):
        return self.palm.GivEnergyLocal(asyncio.Event())


class SanityAndTelemetry(InverterCase):
    def test_sanity_accepts_outage_and_rejects_garbage(self):
        sane = self.palm.GivEnergyLocal._is_data_sane
        self.assertTrue(sane(sane_inverter()))
        self.assertTrue(sane(sane_inverter(v_ac1=0)))                 # grid outage is valid data
        for bad in (dict(v_ac1=999), dict(battery_percent=101), dict(battery_percent=-1),
                    dict(p_grid_out=50000)):
            with self.subTest(bad=bad):
                self.assertFalse(sane(sane_inverter(**bad)))
        self.assertFalse(sane(SimpleNamespace()))                      # missing attributes

    def test_build_telemetry_mapping(self):
        t = self.palm.GivEnergyLocal._build_telemetry(sane_inverter())
        self.assertEqual(t.grid_power, -100)
        self.assertEqual(t.pv_power, 2000)
        self.assertEqual(t.pv_energy_wh, 5000)
        self.assertEqual(t.batt_power, -300)                           # p_inverter_out by default
        self.assertEqual(t.consumption, 900)
        self.assertEqual(t.soc, 60)
        self.assertEqual(t.grid_energy_wh, 500)
        self.assertEqual(t.e_battery_charge_total_wh, 100000)
        self.assertEqual(t.inverter_mins, 720)
        self.assertTrue(t.grid_ok)

    def test_both_pv_strings_are_summed(self):
        t = self.palm.GivEnergyLocal._build_telemetry(sane_inverter(p_pv2=500, e_pv2_day=1.5))
        self.assertEqual(t.pv_power, 2500)
        self.assertEqual(t.pv_energy_wh, 6500)

    def test_battery_register_is_configurable(self):
        self.cfg.GE.batt_power_attr = "p_battery"
        t = self.palm.GivEnergyLocal._build_telemetry(sane_inverter())
        self.assertEqual(t.batt_power, -250)

    def test_outage_and_negative_load(self):
        t = self.palm.GivEnergyLocal._build_telemetry(sane_inverter(v_ac1=0, p_load_demand=-5))
        self.assertFalse(t.grid_ok)
        self.assertEqual(t.consumption, 0)                             # no stale carry-over of old load


class Polling(InverterCase):
    async def test_poll_success(self):
        inv = self.make()
        self.assertTrue(await inv.poll())
        self.assertEqual(inv.latest.soc, 60)
        self.assertTrue(inv.last_update_success)
        self.assertIsNotNone(inv.fresh())

    async def test_poll_failure_backs_off_without_blocking(self):
        inv = self.make()
        self.client.fail_refresh = True
        started = time.monotonic()
        self.assertFalse(await inv.poll())
        self.assertLess(time.monotonic() - started, 1.0)               # no sleeping inside poll
        self.assertTrue(self.client.closed)                            # connection recycled
        self.assertEqual(inv._backoff.failures, 1)

        calls = self.client.refresh_calls
        self.assertFalse(await inv.poll())                             # in backoff: no I/O attempted
        self.assertEqual(self.client.refresh_calls, calls)

        self.client.fail_refresh = False
        self.assertTrue(await inv.poll(force=True))                    # force bypasses backoff
        self.assertEqual(inv._backoff.failures, 0)

    async def test_insane_data_is_rejected_and_not_published(self):
        inv = self.make()
        self.client.inverter.v_ac1 = 999
        self.assertFalse(await inv.poll())
        self.assertIsNone(inv.latest)

    async def test_stale_data_is_not_fresh(self):
        inv = self.make()
        await inv.poll()
        inv.latest.ts -= 1000
        self.assertIsNone(inv.fresh())
        self.assertFalse(inv.last_update_success)


class Commands(InverterCase):
    async def test_play_sequence_and_verification(self):
        inv = self.make()
        self.assertTrue(await inv.set_mode("play"))
        names = [n for n, _ in self.client.executed]
        self.assertEqual(len(names), 9)
        self.assertIn(("set_charge_slot_1_start", (2330,)), self.client.executed)
        self.assertIn(("set_charge_slot_1_end", (530,)), self.client.executed)
        self.assertIn(("set_battery_charge_limit", (25,)), self.client.executed)
        self.assertIn(("set_battery_discharge_limit", (25,)), self.client.executed)

    async def test_pause_and_end_pause(self):
        inv = self.make()
        self.assertTrue(await inv.set_mode("pause"))
        self.assertEqual(self.client.inverter.battery_pause_mode, 3)
        self.assertTrue(await inv.set_mode("end_pause"))
        self.assertEqual(self.client.inverter.battery_pause_mode, 0)

    async def test_charge_and_discharge_now(self):
        inv = self.make()
        self.assertTrue(await inv.set_mode("charge_now"))
        self.assertTrue(self.client.inverter.enable_charge)
        self.assertFalse(self.client.inverter.enable_discharge)
        self.assertTrue(await inv.set_mode("discharge_now"))
        self.assertTrue(self.client.inverter.enable_discharge)
        self.assertFalse(self.client.inverter.enable_charge)

    async def test_set_soc_target_is_clamped(self):
        inv = self.make()
        for tgt, expected in ((1, 4), (50, 50), (500, 100)):
            with self.subTest(tgt=tgt):
                self.client.executed.clear()
                inv.tgt_soc = tgt
                self.assertTrue(await inv.set_mode("set_soc"))
                self.assertIn(("set_charge_target", (expected,)), self.client.executed)

    async def test_unknown_command(self):
        inv = self.make()
        with self.assertLogs("PALM", "ERROR"):
            self.assertFalse(await inv.set_mode("self_destruct"))
        self.assertEqual(self.client.executed, [])

    async def test_test_mode_never_touches_hardware(self):
        self.cfg.pg.test_mode = True
        inv = self.make()
        with mock.patch.object(self.palm, "Client", side_effect=AssertionError("must not connect")):
            self.assertTrue(await inv.set_mode("charge_now"))

    async def test_write_failure_returns_false_and_recycles_connection(self):
        inv = self.make()
        self.client.fail_execute = True
        with self.assertLogs("PALM", "ERROR"):
            self.assertFalse(await inv.set_mode("play"))
        self.assertTrue(self.client.closed)

    async def test_unverified_command_is_reported_as_failure(self):
        inv = self.make()
        self.client.ignore_writes = True                               # inverter silently ignores writes
        with self.assertLogs("PALM", "ERROR") as logs:
            self.assertFalse(await inv.set_mode("charge_now"))
        self.assertTrue(any("Verification FAILED" in line for line in logs.output))

    async def test_bad_request_fails_before_any_write(self):
        class Broken(FakeCommands):
            def __getattr__(self, name):
                if name == "set_charge_target":
                    raise AttributeError(name)
                return super().__getattr__(name)

        self.client.commands = Broken()
        inv = self.make()
        with self.assertLogs("PALM", "ERROR"):
            self.assertFalse(await inv.set_mode("play"))
        self.assertEqual(self.client.executed, [])                     # nothing half-applied

    async def test_get_status_prints_inverter(self):
        inv = self.make()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertTrue(await inv.set_mode("get_status"))
        self.assertIn("v_ac1", buf.getvalue())

    async def test_lock_is_free_while_waiting_to_verify(self):
        """Polling must not be blocked for the whole settle/verify period."""
        inv = self.make()
        self.palm.PAUSE_SETTLE_S = 0.3
        task = asyncio.create_task(inv.set_mode("pause"))
        await asyncio.sleep(0.05)
        self.assertFalse(inv._lock.locked())
        self.assertTrue(await task)


class RegisterAudit(InverterCase):
    """audit=True reports whether another program changed the settings PALM relies on."""

    async def test_drift_is_reported_and_then_corrected(self):
        inv = self.make()
        self.client.inverter.enable_charge = False                  # someone turned charging off
        self.client.inverter.enable_discharge = True                # ...and discharging on
        with self.assertLogs("PALM", "WARNING") as logs:
            self.assertTrue(await inv.set_mode("play", audit=True))
        warning = next(line for line in logs.output if "another program" in line)
        self.assertIn("enable_charge is False, expected True", warning)
        self.assertIn("enable_discharge is True, expected False", warning)
        self.assertTrue(self.client.inverter.enable_charge)         # play put them right
        self.assertFalse(self.client.inverter.enable_discharge)

    async def test_unchanged_registers_are_confirmed_quietly(self):
        inv = self.make()
        self.client.inverter.enable_charge, self.client.inverter.enable_discharge = True, False
        with self.assertLogs("PALM", "INFO") as logs:
            self.assertTrue(await inv.set_mode("play", audit=True))
        self.assertTrue(any("settings match" in line for line in logs.output))
        self.assertFalse([r for r in logs.records if r.levelname == "WARNING"])

    async def test_no_audit_unless_asked_for(self):
        inv = self.make()
        self.client.inverter.enable_charge = False
        with self.assertLogs("PALM", "INFO") as logs:
            await inv.set_mode("play")
        self.assertFalse([line for line in logs.output if "another program" in line or "settings match" in line])

    async def test_audit_reads_all_registers_first_but_ordinary_commands_do_not(self):
        inv = self.make()
        await inv.set_mode("play")
        self.assertEqual(self.client.full_refresh_calls, 0)
        await inv.set_mode("play", audit=True)
        self.assertEqual(self.client.full_refresh_calls, 1)

    async def test_audit_checks_the_registers_of_the_command_being_reasserted(self):
        inv = self.make()
        self.client.inverter.enable_discharge = False               # drifted, for a discharge_now re-assert
        with self.assertLogs("PALM", "WARNING") as logs:
            await inv.set_mode("discharge_now", audit=True)
        self.assertTrue(any("discharge_now" in line and "enable_discharge is False, expected True" in line
                            for line in logs.output))

    async def test_repeated_drift_warnings_are_throttled(self):
        inv = self.make()
        with self.assertLogs("PALM", "INFO") as logs:
            for _ in range(3):
                self.client.inverter.enable_charge = False
                await inv.set_mode("play", audit=True)
        self.assertEqual(len([line for line in logs.output if "another program" in line]), 1)


class StopAwareSleeps(InverterCase):
    async def test_sleep_returns_immediately_when_stopping(self):
        inv = self.make()
        inv.stop_event.set()
        await asyncio.wait_for(inv._sleep(30), timeout=1)

    async def test_non_interruptible_sleep_ignores_stop(self):
        inv = self.make()
        inv.stop_event.set()
        started = time.monotonic()
        await inv._sleep(0.1, interruptible=False)
        self.assertGreaterEqual(time.monotonic() - started, 0.09)

    async def test_verification_aborts_when_stopping(self):
        inv = self.make()
        self.client.ignore_writes = True
        inv.stop_event.set()
        self.assertFalse(await inv._verify("charge_now", [("enable_charge", True)], interruptible=True))


if __name__ == "__main__":
    unittest.main()
