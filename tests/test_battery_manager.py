import asyncio
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from support import FakeInverter, FakeShelly, PalmAsyncCase, PalmCase, make_telemetry


class ManagerCase(PalmAsyncCase):
    window_refresh = False          # the overnight re-assert is exercised only where a test says so (it is clock-driven)

    def setUp(self):
        super().setUp()
        if not self.window_refresh:
            patcher = mock.patch.object(self.palm.BatteryManager, "_note_window", lambda manager, now: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.S = self.palm.BatteryState
        self.tele = make_telemetry()
        self.inv = FakeInverter(self.tele)
        self.sh = FakeShelly()
        self.env = self.palm.Env(None)
        self.m = self.palm.BatteryManager(self.inv, self.sh, self.env)

    def ev_samples(self, watts=7000, n=3, tele=None):
        self.sh.ev_power.set(watts)
        for _ in range(n):
            self.m.sample_ev(tele or self.tele)


class Startup(ManagerCase):
    async def test_failed_start_is_not_assumed_applied_and_recovers(self):
        self.inv.fail = {"end_pause"}
        await self.m.start()
        self.assertIsNone(self.m.applied_state)                       # unknown, NOT assumed ECO
        self.inv.fail = set()
        await self.m.update()
        self.assertEqual(self.m.applied_state, self.S.ECO_OPTIMISE)
        self.assertIs(self.m.paused, False)

    async def test_start_clears_a_stale_pause_then_plays_and_heater_off(self):
        await self.m.start()
        self.assertEqual(self.inv.calls, ["end_pause", "play"])
        self.assertEqual(self.sh.sw, [False])                         # heater forced off at start

    async def test_no_heater_url_means_no_switching(self):
        self.cfg.Shelly.sw1_url = None
        await self.m.start()
        self.assertEqual(self.sh.sw, [])


class Decisions(ManagerCase):
    def test_ev_needs_three_consecutive_samples(self):
        self.ev_samples(n=2)
        self.assertFalse(self.m.is_ev_charging())
        self.ev_samples(n=1)
        self.assertTrue(self.m.is_ev_charging())

    def test_ev_power_is_net_of_pv(self):
        self.ev_samples(watts=7000, tele=make_telemetry(pv_power=6000))   # only 1000 W from grid
        self.assertFalse(self.m.is_ev_charging())

    def test_stale_ev_meter_clears_history(self):
        self.ev_samples()
        self.sh.ev_power.ts -= 1000
        self.m.sample_ev(self.tele)
        self.assertFalse(self.m.is_ev_charging())

    def test_stale_inverter_clears_history(self):
        self.ev_samples()
        self.m.sample_ev(None)
        self.assertFalse(self.m.is_ev_charging())

    def test_summer_ev_pauses_winter_ev_boosts(self):
        self.ev_samples()
        self.assertEqual(self.m.decide(datetime(2026, 6, 10, 12, 5, 10), self.tele), self.S.EV_PROTECT)
        self.m.boost_until = None
        self.assertEqual(self.m.decide(datetime(2026, 12, 10, 12, 5, 10), self.tele), self.S.WINTER_BOOST)

    def test_ev_ignored_during_off_peak(self):
        self.ev_samples()
        self.assertEqual(self.m.decide(datetime(2026, 6, 10, 2, 0), self.tele), self.S.ECO_OPTIMISE)

    def test_boost_held_until_aligned_boundary_then_released(self):
        self.ev_samples()
        d = self.m.decide(datetime(2026, 6, 10, 12, 5, 10), self.tele)
        self.assertEqual(self.m.boost_until, datetime(2026, 6, 10, 12, 30))
        self.m.desired_state = d
        self.sh.ev_power.set(0)
        self.m.sample_ev(self.tele)                                    # EV has stopped
        self.assertEqual(self.m.decide(datetime(2026, 6, 10, 12, 20), self.tele), self.S.EV_PROTECT)  # held
        with self.assertLogs("PALM", "INFO"):
            self.assertEqual(self.m.decide(datetime(2026, 6, 10, 12, 31), self.tele), self.S.ECO_OPTIMISE)
        self.assertIsNone(self.m.boost_until)

    def test_boost_is_not_frozen_by_midnight(self):
        self.cfg.GE.start_time, self.cfg.GE.end_time = "01:00", "05:00"   # keep 23:50 outside off-peak
        self.ev_samples()
        d = self.m.decide(datetime(2026, 6, 10, 23, 50, 10), self.tele)
        self.assertEqual(self.m.boost_until, datetime(2026, 6, 11, 0, 0))
        self.m.desired_state = d
        self.assertEqual(self.m.decide(datetime(2026, 6, 10, 23, 55), self.tele), self.S.EV_PROTECT)
        self.m._ev_samples.clear()
        self.assertEqual(self.m.decide(datetime(2026, 6, 11, 0, 5), self.tele), self.S.ECO_OPTIMISE)

    def test_stale_telemetry_abandons_boost(self):
        self.m.boost_until = datetime.now() + timedelta(minutes=20)
        self.m.desired_state = self.S.EV_PROTECT
        with self.assertLogs("PALM", "WARNING"):
            self.assertEqual(self.m.decide(datetime.now(), None), self.S.ECO_OPTIMISE)
        self.assertIsNone(self.m.boost_until)


class PmExport(ManagerCase):
    def check(self, soc=95, month=6, hour=18, temp=20.0, co2=200.0, pm="17:00",
              desired=None, tele=True, feeds=True, minute=0):
        self.cfg.GE.pm_export_start = pm
        self.cfg.OpenWeatherMap.enable = feeds
        self.cfg.CarbonIntensity.enable = feeds
        self.env.temp.value = None
        self.env.co2.value = None
        if feeds and temp is not None:
            self.env.temp.set(temp)
        if feeds and co2 is not None:
            self.env.co2.set(co2)
        self.m.desired_state = desired or self.S.ECO_OPTIMISE
        telemetry = make_telemetry(soc=soc) if tele else None
        return self.m.is_pm_export(datetime(2026, month, 10, hour, minute), telemetry)

    def test_conditions(self):
        self.assertTrue(self.check())
        cases = {
            "winter": dict(month=12),
            "soc too low": dict(soc=80),
            "before start": dict(hour=16),
            "too cold": dict(temp=10.0),
            "low carbon": dict(co2=100.0),
            "disabled": dict(pm=""),
            "no telemetry": dict(tele=False),
            "stale weather": dict(temp=None),
            "stale co2": dict(co2=None),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                self.assertFalse(self.check(**kwargs))

    def test_never_inside_the_overnight_window(self):
        self.assertTrue(self.check(hour=23, minute=29))                                    # last minute before it opens
        for hour, minute in ((23, 30), (23, 45), (0, 10), (4, 59)):
            with self.subTest(hour=hour, minute=minute):
                self.assertFalse(self.check(hour=hour, minute=minute))
        self.assertFalse(self.check(soc=100, hour=23, minute=30))                          # however full the battery is

    def test_hysteresis_does_not_carry_export_into_the_window(self):
        self.assertTrue(self.check(soc=50, hour=23, minute=29, desired=self.S.PEAK_SHAVE))
        self.assertFalse(self.check(soc=50, hour=23, minute=30, desired=self.S.PEAK_SHAVE))

    def test_window_start_setting_is_honoured(self):
        self.cfg.GE.start_time, self.cfg.GE.end_time = "22:00", "06:00"
        self.assertTrue(self.check(hour=21, minute=59))
        self.assertFalse(self.check(hour=22, minute=0))

    def test_feeds_disabled_use_original_defaults(self):
        self.assertTrue(self.check(feeds=False))                       # 15 C / 200 g assumed, as before

    def test_hysteresis_keeps_exporting_until_30_percent(self):
        self.assertTrue(self.check(soc=50, hour=20, desired=self.S.PEAK_SHAVE))
        self.assertFalse(self.check(soc=25, hour=20, desired=self.S.PEAK_SHAVE))
        self.assertFalse(self.check(soc=50, hour=20, desired=self.S.ECO_OPTIMISE))


class Apply(ManagerCase):
    async def test_leaving_ev_protect_ends_pause_first(self):
        self.m.applied_state, self.m.paused = self.S.EV_PROTECT, True
        self.assertTrue(await self.m.apply(self.S.ECO_OPTIMISE))
        self.assertEqual(self.inv.calls, ["end_pause", "play"])
        self.assertIs(self.m.paused, False)

    async def test_end_pause_failure_blocks_the_rest(self):
        self.m.applied_state, self.m.paused = self.S.EV_PROTECT, True
        self.inv.fail = {"end_pause"}
        self.assertFalse(await self.m.apply(self.S.ECO_OPTIMISE))
        self.assertEqual(self.inv.calls, ["end_pause"])
        self.assertIs(self.m.paused, True)
        self.assertEqual(self.m.applied_state, self.S.EV_PROTECT)

    async def test_pause_marks_paused(self):
        self.m.paused = False
        self.assertTrue(await self.m.apply(self.S.EV_PROTECT))
        self.assertIs(self.m.paused, True)
        self.assertEqual(self.inv.calls, ["pause"])

    async def test_failed_command_keeps_old_state_and_backs_off(self):
        await self.m.start()
        self.m.desired_state = self.S.PEAK_SHAVE
        self.inv.fail = {"discharge_now"}
        with self.assertLogs("PALM", "ERROR"):
            await self.m.reconcile()
        self.assertEqual(self.m.applied_state, self.S.ECO_OPTIMISE)    # not recorded as applied
        self.assertFalse(self.m._backoff.ready())
        n = len(self.inv.calls)
        await self.m.reconcile()                                       # still backing off: no new attempt
        self.assertEqual(len(self.inv.calls), n)
        self.inv.fail = set()
        self.m._backoff.not_before = 0
        await self.m.reconcile()
        self.assertEqual(self.m.applied_state, self.S.PEAK_SHAVE)

    async def test_unmapped_state_is_refused(self):
        with self.assertLogs("PALM", "ERROR"):
            self.assertFalse(await self.m.apply(self.S.GRID_CHARGE))

    async def test_pause_state_survives_until_explicitly_ended(self):
        await self.m.start()
        await self.m.apply(self.S.EV_PROTECT)
        await self.m.apply(self.S.PEAK_SHAVE)
        self.assertEqual(self.inv.calls[-2:], ["end_pause", "discharge_now"])


class Heater(ManagerCase):
    def setUp(self):
        super().setUp()
        self.cfg.OpenWeatherMap.enable = True

    async def test_cold_winter_boost_turns_heater_on_then_off(self):
        self.env.temp.set(5.0)
        await self.m.start()
        self.m.paused = False
        await self.m.apply(self.S.WINTER_BOOST)
        await self.m.reconcile_heater()
        self.assertIs(self.sh.sw[-1], True)
        await self.m.apply(self.S.ECO_OPTIMISE)
        await self.m.reconcile_heater()
        self.assertIs(self.sh.sw[-1], False)

    async def test_warm_day_does_not_turn_heater_on(self):
        self.env.temp.set(20.0)
        await self.m.start()
        await self.m.apply(self.S.WINTER_BOOST)
        await self.m.reconcile_heater()
        self.assertNotIn(True, self.sh.sw)

    async def test_unknown_temperature_does_not_turn_heater_on(self):
        await self.m.start()                                           # feed enabled but no reading
        await self.m.apply(self.S.WINTER_BOOST)
        await self.m.reconcile_heater()
        self.assertNotIn(True, self.sh.sw)

    async def test_heater_switching_retries_until_confirmed(self):
        self.m.applied_state, self.m.heater_wanted = self.S.WINTER_BOOST, True
        self.sh.fail = True
        await self.m.reconcile_heater()
        self.assertIsNone(self.m.heater_state)
        await self.m.reconcile_heater()                                # backing off
        self.assertEqual(self.sh.attempts, 1)
        self.sh.fail = False
        self.m._heater_backoff.not_before = 0
        await self.m.reconcile_heater()
        self.assertIs(self.m.heater_state, True)

    async def test_heater_off_is_unconditional_even_if_state_unknown(self):
        self.m.applied_state, self.m.heater_state = self.S.ECO_OPTIMISE, None
        await self.m.reconcile_heater()
        self.assertEqual(self.sh.sw, [False])


class UpdateLoop(ManagerCase):
    async def test_full_ev_cycle_pause_then_release(self):
        self.m.is_off_peak = lambda now: False
        self.m.is_winter = lambda now: False
        await self.m.start()
        self.sh.ev_power.set(7000)
        for _ in range(3):
            await self.m.update()
        self.assertEqual(self.m.applied_state, self.S.EV_PROTECT)
        self.assertIs(self.m.paused, True)
        self.m.boost_until = datetime(2000, 1, 1)                      # boost window over
        self.sh.ev_power.set(0)
        await self.m.update()
        await self.m.update()
        self.assertEqual(self.m.applied_state, self.S.ECO_OPTIMISE)
        self.assertIs(self.m.paused, False)

    async def test_stale_data_mid_boost_falls_back_to_eco(self):
        self.m.is_off_peak = lambda now: False
        self.m.is_winter = lambda now: True
        self.env.temp.value = None
        await self.m.start()
        self.sh.ev_power.set(7000)
        for _ in range(3):
            await self.m.update()
        self.assertEqual(self.m.applied_state, self.S.WINTER_BOOST)
        self.inv.latest = None                                         # inverter went silent
        with self.assertLogs("PALM", "WARNING"):
            await self.m.update()
        self.assertEqual(self.m.desired_state, self.S.ECO_OPTIMISE)


class Shutdown(ManagerCase):
    async def test_restores_safe_state_and_turns_heater_off(self):
        self.m.applied_state, self.m.heater_state, self.m.paused = self.S.WINTER_BOOST, True, False
        await self.m.shutdown()
        self.assertEqual(self.m.applied_state, self.S.ECO_OPTIMISE)
        self.assertIs(self.sh.sw[-1], False)
        self.assertEqual(self.inv.calls, ["play"])

    async def test_clears_pause_on_shutdown(self):
        self.m.applied_state, self.m.paused = self.S.EV_PROTECT, True
        await self.m.shutdown()
        self.assertEqual(self.inv.calls, ["end_pause", "play"])

    async def test_hung_inverter_cannot_block_shutdown_forever(self):
        class Hung(FakeInverter):
            async def set_mode(self, cmd, interruptible=True, audit=False):
                await asyncio.sleep(30)

        self.m.inverter = Hung(self.tele)
        self.m.applied_state, self.m.paused = self.S.EV_PROTECT, True
        with mock.patch.object(self.palm, "SHUTDOWN_TIMEOUT_S", 0.05), self.assertLogs("PALM", "ERROR"):
            await self.m.shutdown()


class OffPeakRefresh(ManagerCase):
    """When the overnight charge window opens, the inverter settings are re-asserted (once per night)."""

    NOON = datetime(2026, 6, 10, 12, 0)
    window_refresh = True                                        # default window 23:30 - 05:30

    def test_window_key_for_a_window_spanning_midnight(self):
        key = self.m.window_key
        self.assertIsNone(key(datetime(2026, 6, 10, 12, 0)))
        self.assertIsNone(key(datetime(2026, 6, 10, 23, 29)))
        self.assertEqual(key(datetime(2026, 6, 10, 23, 30)), date(2026, 6, 10))
        self.assertEqual(key(datetime(2026, 6, 11, 0, 10)), date(2026, 6, 10))    # still last evening's window
        self.assertEqual(key(datetime(2026, 6, 11, 5, 29)), date(2026, 6, 10))
        self.assertIsNone(key(datetime(2026, 6, 11, 5, 30)))

    def test_window_key_for_a_same_day_window_and_for_no_window(self):
        self.cfg.GE.start_time, self.cfg.GE.end_time = "01:00", "05:00"
        self.assertEqual(self.m.window_key(datetime(2026, 6, 10, 1, 0)), date(2026, 6, 10))
        self.assertEqual(self.m.window_key(datetime(2026, 6, 10, 4, 59)), date(2026, 6, 10))
        self.assertIsNone(self.m.window_key(datetime(2026, 6, 10, 5, 0)))
        self.cfg.GE.start_time = self.cfg.GE.end_time = "02:00"                    # zero-length window
        self.assertIsNone(self.m.window_key(datetime(2026, 6, 10, 2, 0)))

    async def test_play_is_reasserted_once_when_the_window_opens(self):
        await self.m.start(now=self.NOON)
        self.assertEqual(self.inv.calls, ["end_pause", "play"])
        self.inv.calls.clear()
        self.inv.audits.clear()

        await self.m.update(now=datetime(2026, 6, 10, 23, 29))
        self.assertEqual(self.inv.calls, [])                                   # window not open yet
        await self.m.update(now=datetime(2026, 6, 10, 23, 30))
        self.assertEqual(self.inv.calls, ["play"])                             # the re-assert
        self.assertEqual(self.inv.audits, [True])                              # ...with a register audit
        for later in (datetime(2026, 6, 10, 23, 31), datetime(2026, 6, 10, 23, 59), datetime(2026, 6, 11, 0, 0),
                      datetime(2026, 6, 11, 2, 0), datetime(2026, 6, 11, 5, 29), datetime(2026, 6, 11, 12, 0)):
            await self.m.update(now=later)
        self.assertEqual(self.inv.calls, ["play"])                             # not repeated, even past midnight

        await self.m.update(now=datetime(2026, 6, 11, 23, 30))                 # next night
        self.assertEqual(self.inv.calls, ["play", "play"])
        self.assertEqual(self.inv.audits, [True, True])

    async def test_the_reassert_is_retried_until_it_succeeds_then_not_repeated(self):
        await self.m.start(now=self.NOON)
        self.inv.calls.clear()
        self.inv.fail = {"play"}
        with self.assertLogs("PALM", "ERROR"):
            await self.m.update(now=datetime(2026, 6, 10, 23, 30))
        self.assertEqual(self.inv.calls, ["play"])
        self.assertIsNotNone(self.m._refresh_pending)                          # still owed
        self.assertIsNone(self.m._refreshed_window)
        await self.m.update(now=datetime(2026, 6, 10, 23, 31))
        self.assertEqual(self.inv.calls, ["play"])                             # backing off, no hammering
        self.m._backoff.not_before = 0
        await self.m.update(now=datetime(2026, 6, 10, 23, 32))
        self.assertEqual(self.inv.calls, ["play", "play"])

        self.inv.fail = set()
        self.m._backoff.not_before = 0
        await self.m.update(now=datetime(2026, 6, 10, 23, 40))
        self.assertEqual(self.inv.calls, ["play", "play", "play"])
        self.assertIsNone(self.m._refresh_pending)
        self.assertEqual(self.m._refreshed_window, date(2026, 6, 10))
        await self.m.update(now=datetime(2026, 6, 10, 23, 41))
        self.assertEqual(len(self.inv.calls), 3)

    async def test_a_restart_inside_the_window_counts_as_the_reassert(self):
        await self.m.start(now=datetime(2026, 6, 11, 2, 0))                    # service restarted at 02:00
        self.inv.calls.clear()
        await self.m.update(now=datetime(2026, 6, 11, 2, 1))
        self.assertEqual(self.inv.calls, [])
        await self.m.update(now=datetime(2026, 6, 11, 23, 30))                 # but tonight's window still works
        self.assertEqual(self.inv.calls, ["play"])

    async def test_a_failed_start_inside_the_window_is_reasserted_on_recovery(self):
        self.inv.fail = {"end_pause"}
        await self.m.start(now=datetime(2026, 6, 11, 2, 0))                    # start-up failed: unknown state
        self.assertIsNone(self.m.applied_state)
        self.inv.fail, self.inv.calls = set(), []
        await self.m.update(now=datetime(2026, 6, 11, 2, 1))
        self.assertEqual(self.inv.calls, ["end_pause", "play"])                # normal recovery, one apply

    async def test_a_same_day_window_triggers_at_its_start(self):
        self.cfg.GE.start_time, self.cfg.GE.end_time = "01:00", "05:00"
        await self.m.start(now=self.NOON)
        self.inv.calls.clear()
        await self.m.update(now=datetime(2026, 6, 11, 0, 59))
        self.assertEqual(self.inv.calls, [])
        await self.m.update(now=datetime(2026, 6, 11, 1, 0))
        self.assertEqual(self.inv.calls, ["play"])

    async def test_evening_export_ends_when_the_window_opens(self):
        S = self.palm.BatteryState
        self.inv.latest = make_telemetry(soc=95)                 # warm, high-carbon, battery full: export conditions
        await self.m.start(now=self.NOON)
        await self.m.update(now=datetime(2026, 6, 10, 18, 0))
        self.assertEqual(self.m.applied_state, S.PEAK_SHAVE)     # real decide(): evening export is running
        self.inv.calls.clear()
        self.inv.audits.clear()

        await self.m.update(now=datetime(2026, 6, 10, 23, 29))
        self.assertEqual((self.m.applied_state, self.inv.calls), (S.PEAK_SHAVE, []))      # still exporting

        await self.m.update(now=datetime(2026, 6, 10, 23, 30))                            # the window opens
        self.assertEqual(self.m.desired_state, S.ECO_OPTIMISE)
        self.assertEqual(self.m.applied_state, S.ECO_OPTIMISE)
        self.assertEqual(self.inv.calls, ["play"])               # export stops; this change IS the re-assert
        self.assertEqual(self.m._refreshed_window, date(2026, 6, 10))
        self.assertIsNone(self.m._refresh_pending)

        for later in (datetime(2026, 6, 10, 23, 31), datetime(2026, 6, 10, 23, 59), datetime(2026, 6, 11, 0, 30),
                      datetime(2026, 6, 11, 4, 0)):
            await self.m.update(now=later)
        self.assertEqual(self.inv.calls, ["play"])               # export does not restart inside the window

        await self.m.update(now=datetime(2026, 6, 11, 17, 30))   # next evening, outside the window: export resumes
        self.assertEqual(self.inv.calls, ["play", "discharge_now"])

    async def test_export_cannot_start_inside_the_window_even_with_a_full_battery(self):
        S = self.palm.BatteryState
        self.inv.latest = make_telemetry(soc=100)
        await self.m.start(now=self.NOON)
        await self.m.update(now=datetime(2026, 6, 10, 23, 45))   # after 17:00, battery full, but the window is open
        self.assertEqual((self.m.desired_state, self.m.applied_state), (S.ECO_OPTIMISE, S.ECO_OPTIMISE))
        self.assertNotIn("discharge_now", self.inv.calls)

    async def test_a_window_that_opens_while_export_is_failing_still_ends_it(self):
        S = self.palm.BatteryState
        self.inv.latest = make_telemetry(soc=95)
        await self.m.start(now=self.NOON)
        await self.m.update(now=datetime(2026, 6, 10, 18, 0))
        self.inv.fail = {"play"}
        with self.assertLogs("PALM", "ERROR"):
            await self.m.update(now=datetime(2026, 6, 10, 23, 30))
        self.assertEqual(self.m.applied_state, S.PEAK_SHAVE)     # not changed yet...
        self.inv.fail = set()
        self.m._backoff.not_before = 0
        await self.m.update(now=datetime(2026, 6, 10, 23, 35))
        self.assertEqual(self.m.applied_state, S.ECO_OPTIMISE)   # ...but retried until export is stopped

    async def test_a_transition_at_window_open_is_the_reassert_and_is_not_repeated(self):
        S = self.palm.BatteryState
        await self.m.start(now=self.NOON)
        self.m.applied_state, self.m.paused = S.EV_PROTECT, True               # an EV pause ending right at 23:30
        self.inv.calls.clear()
        self.inv.audits.clear()
        await self.m.update(now=datetime(2026, 6, 10, 23, 30))
        self.assertEqual(self.inv.calls, ["end_pause", "play"])
        self.assertEqual(self.inv.audits, [False, False])                      # a real change: nothing to audit
        await self.m.update(now=datetime(2026, 6, 10, 23, 31))
        self.assertEqual(len(self.inv.calls), 2)

    async def test_ordinary_transitions_are_never_audited(self):
        await self.m.start(now=self.NOON)
        self.inv.audits.clear()
        self.m.decide = lambda now, telemetry: self.palm.BatteryState.PEAK_SHAVE
        await self.m.update(now=datetime(2026, 6, 10, 18, 0))
        self.assertEqual(self.inv.audits, [False])


class StaticHelpers(PalmCase):
    def test_is_winter_uses_configured_months(self):
        self.assertTrue(self.palm.BatteryManager.is_winter(datetime(2026, 1, 5)))
        self.assertFalse(self.palm.BatteryManager.is_winter(datetime(2026, 7, 5)))

    def test_off_peak_spans_midnight(self):
        f = self.palm.BatteryManager.is_off_peak
        self.assertTrue(f(datetime(2026, 6, 1, 23, 45)))
        self.assertTrue(f(datetime(2026, 6, 1, 3, 0)))
        self.assertFalse(f(datetime(2026, 6, 1, 12, 0)))


if __name__ == "__main__":
    unittest.main()
