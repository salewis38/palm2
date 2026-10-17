import unittest

from support import PalmCase


class ValidateSettings(PalmCase):
    def test_good_settings_have_no_errors(self):
        self.assertEqual(self.palm.validate_settings(), [])

    def test_bad_times_rejected(self):
        self.cfg.GE.start_time = "23.30"
        self.cfg.GE.end_time = "25:00"
        errors = self.palm.validate_settings()
        self.assertEqual(len([e for e in errors if "start_time" in e or "end_time" in e]), 2)

    def test_pm_export_start_optional_but_validated(self):
        self.cfg.GE.pm_export_start = ""
        self.assertEqual(self.palm.validate_settings(), [])
        self.cfg.GE.pm_export_start = "5pm"
        self.assertTrue(any("pm_export_start" in e for e in self.palm.validate_settings()))

    def test_rates_must_be_in_range(self):
        for bad in (0, -1, None, "fast", 6.0):
            with self.subTest(bad=bad):
                self.cfg.GE.charge_rate = bad
                self.assertTrue(any("charge_rate" in e for e in self.palm.validate_settings()))
        self.cfg.GE.charge_rate = 2.6
        self.cfg.GE.discharge_rate = 0
        self.assertTrue(any("discharge_rate" in e for e in self.palm.validate_settings()))

    def test_months_must_be_valid(self):
        self.cfg.GE.winter = [0, 13]
        self.assertTrue(any("winter" in e for e in self.palm.validate_settings()))
        self.cfg.GE.winter = "dec"
        self.assertTrue(any("winter" in e for e in self.palm.validate_settings()))

    def test_pvoutput_credentials_required_only_when_enabled(self):
        self.cfg.PVOutput.key = ""
        self.assertEqual(self.palm.validate_settings(), [])
        self.cfg.PVOutput.enable = True
        self.assertTrue(any("PVOutput.key" in e for e in self.palm.validate_settings()))
        self.cfg.PVOutput.key = "abc"
        self.assertEqual(self.palm.validate_settings(), [])

    def test_ev_threshold_must_be_numeric(self):
        self.cfg.GE.ev_power_threshold = "lots"
        self.assertTrue(any("ev_power_threshold" in e for e in self.palm.validate_settings()))


if __name__ == "__main__":
    unittest.main()
