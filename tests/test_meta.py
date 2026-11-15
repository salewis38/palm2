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


class PageModule(unittest.TestCase):
    def test_palm_gui_holds_data_only(self):
        """palm_gui.py is one string and a docstring: no code to run, nothing to review for behaviour."""
        import ast
        tree = ast.parse((ROOT / "palm_gui.py").read_text(encoding="utf-8"))
        body = tree.body
        self.assertTrue(isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant), "docstring first")
        rest = body[1:]
        self.assertEqual(len(rest), 1)
        self.assertIsInstance(rest[0], ast.Assign)
        self.assertEqual([t.id for t in rest[0].targets], ["PAGE"])
        self.assertIsInstance(rest[0].value, ast.Constant)

    def test_palm_py_no_longer_embeds_the_page(self):
        self.assertNotIn("<!doctype html>\n<html", SOURCE.replace(palm.FALLBACK_PAGE, ""))
        self.assertNotIn("WEB_PAGE", SOURCE)
        self.assertNotIn("<script>", SOURCE)

    def test_palm_py_does_not_import_the_page_at_start_up(self):
        """The controller must run without palm_gui.py: it is loaded lazily, only when the server starts."""
        top_level = [line for line in SOURCE.splitlines() if line.startswith(("import palm_gui", "from palm_gui"))]
        self.assertEqual(top_level, [])


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
