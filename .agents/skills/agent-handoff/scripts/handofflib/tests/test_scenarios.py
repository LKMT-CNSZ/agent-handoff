"""End-to-end tests: the three scenarios drive the real CLI scripts."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from handofflib import scenario, util  # noqa: E402


class ScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="handoff-scenario-")
        cls.quiet = not os.environ.get("HANDOFF_TEST_VERBOSE")

    @classmethod
    def tearDownClass(cls):
        util.rmtree(cls.base)

    def _run(self, name: str):
        log = (lambda message: None) if self.quiet else print
        result = scenario.run_scenario(name, self.base, log=log)
        failures = [step for step in result["steps"] if not step["ok"]]
        self.assertTrue(
            result["ok"],
            "scenario %r failed: %s\nfailed steps: %s\nartifacts: %s"
            % (name, result["error"], failures, result["root"]),
        )
        self.assertGreaterEqual(len(result["steps"]), 5, "scenario barely ran")
        return result

    def test_handoff_scenario(self):
        result = self._run("handoff")
        names = [step["name"] for step in result["steps"]]
        for expected in (
            "resume reports RECOVERY MODE",
            "dsh takes ownership",
            "finish runs the declared check and archives",
            "doctor reports a healthy project",
        ):
            self.assertIn(expected, names)

    def test_blocked_scenario(self):
        self._run("blocked")

    def test_failing_check_scenario(self):
        self._run("failing-check")


if __name__ == "__main__":
    unittest.main()
