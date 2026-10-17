"""Guards on the GitHub Actions setup: no deprecated action runtimes, no floating runner label, no artifact clashes.

Text-based on purpose, so the tests need no YAML library."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"

# Oldest major version of each action that runs on Node 24 (older majors run on the deprecated Node 20 runtime).
MIN_NODE24_MAJOR = {
    "actions/checkout": 5,
    "actions/setup-python": 6,
    "actions/upload-artifact": 6,
    "actions/download-artifact": 7,
}


def steps(text):
    """Split the job's step list (steps are indented six spaces) into one text block per step."""
    return re.split(r"(?m)^      - ", text)[1:]


class Workflow(unittest.TestCase):
    def setUp(self):
        self.text = CI.read_text(encoding="utf-8")

    def test_actions_are_on_node24_releases(self):
        found = re.findall(r"uses:\s*(actions/[\w-]+)@v(\d+)", self.text)
        self.assertTrue(found, "no actions found")
        for action, major in found:
            minimum = MIN_NODE24_MAJOR.get(action)
            if minimum is not None:
                self.assertGreaterEqual(int(major), minimum,
                                        f"{action}@v{major} runs on the deprecated Node 20 runtime (need v{minimum}+)")

    def test_no_floating_runner_labels(self):
        self.assertNotRegex(self.text, r"(?m)^\s*(runs-on:|-?\s*os:).*-latest", "use a pinned runner image")
        runs_on = re.findall(r"(?m)^\s*runs-on:\s*(.+?)\s*$", self.text)
        self.assertTrue(runs_on)
        for value in runs_on:
            self.assertTrue(value == "${{ matrix.os }}" or re.fullmatch(r"ubuntu-\d\d\.\d\d(-arm)?", value), value)
        labels = re.findall(r"os:\s*\[(.*?)\]", self.text) + re.findall(r"-\s*os:\s*(\S+)", self.text)
        self.assertTrue(labels)
        for group in labels:
            for label in [x.strip() for x in group.split(",")]:
                self.assertRegex(label, r"^ubuntu-\d\d\.\d\d(-arm)?$", "runner labels must be pinned")

    def test_baseline_runner_is_blocking_and_the_preview_is_not(self):
        self.assertRegex(self.text, r"os:\s*\[ubuntu-24\.04\]")
        self.assertIn("continue-on-error: ${{ matrix.experimental }}", self.text)
        self.assertRegex(self.text, r"(?s)include:\s*-\s*os:\s*ubuntu-26\.04.*?experimental:\s*true")

    def test_artifact_uploads_cannot_collide_between_matrix_jobs(self):
        uploads = [s for s in steps(self.text) if "actions/upload-artifact@" in s]
        self.assertTrue(uploads)
        for step in uploads:
            self.assertIn("matrix.os", step, "upload step must be limited to one runner image (or name must include it)")

    def test_ci_runs_the_whole_suite_and_guards_secrets(self):
        self.assertIn("unittest discover -s tests", self.text)
        self.assertIn("git ls-files --error-unmatch palm_settings.py", self.text)
        self.assertRegex(self.text, r"(?m)^permissions:\s*\n\s+contents:\s*read")


class Dependabot(unittest.TestCase):
    def test_actions_are_kept_current_automatically(self):
        text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        self.assertIn('package-ecosystem: "github-actions"', text)
        self.assertRegex(text, r"interval:\s*\"(weekly|monthly)\"")


if __name__ == "__main__":
    unittest.main()
