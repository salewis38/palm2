"""Housekeeping checks on palm.py itself."""
import importlib.util
import pathlib
import re
import unittest
from unittest import mock

from support import palm

ROOT = pathlib.Path(palm.__file__).resolve().parent
SOURCE = pathlib.Path(palm.__file__).read_text(encoding="utf-8")


class Changelog(unittest.TestCase):
    def test_current_version_has_a_changelog_entry(self):
        match = re.search(r'^PALM_VERSION = "(v[\d.]+[a-z]?)"', SOURCE, re.M)
        self.assertIsNotNone(match, "PALM_VERSION not found")
        version = match.group(1)
        header = SOURCE.split("PALM_VERSION", 1)[0]
        self.assertRegex(header, rf"(?m)^# {re.escape(version)}\s", f"no '# {version}' line in the changelog")

    def test_changelog_does_not_contradict_the_success_log_line(self):
        """The upload summary is logged at INFO with its payload; the changelog must not say otherwise."""
        payload_at_info = 'logger.info("Data; Write to pvoutput.org; %s; %s; %s"' in SOURCE
        claims_debug = "payload moved to DEBUG" in SOURCE.split("PALM_VERSION", 1)[0]
        reverted = re.search(r"payload moved to DEBUG\)\s+\[reverted", SOURCE) is not None
        if payload_at_info and claims_debug:
            self.assertTrue(reverted, "changelog says the payload moved to DEBUG, but it is logged at INFO")


class DeadCode(unittest.TestCase):
    def test_no_private_state_is_assigned_but_never_read(self):
        """Catches leftovers such as an energy-guard cache whose only reader was removed."""
        assigned = set(re.findall(r"self\.(_[a-z_]+)\s*(?::[^=\n]+)?=", SOURCE))
        for name in sorted(assigned):
            reads = len(re.findall(rf"self\.{name}\b", SOURCE)) - len(re.findall(rf"self\.{name}\s*(?::[^=\n]+)?=[^=]", SOURCE))
            self.assertGreater(reads, 0, f"self.{name} is assigned but never read")


class ExampleSettings(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location("palm_settings_example", ROOT / "palm_settings.example.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_example_settings_pass_validation(self):
        with mock.patch.object(palm, "stgs", self.load()):
            self.assertEqual(palm.validate_settings(), [])

    def test_the_removed_opt_out_setting_is_gone_from_code_and_example(self):
        code = SOURCE.split("PALM_VERSION", 1)[1]
        self.assertNotIn("refresh_at_offpeak_start", code)
        self.assertFalse(hasattr(self.load().GE, "refresh_at_offpeak_start"))


if __name__ == "__main__":
    unittest.main()
