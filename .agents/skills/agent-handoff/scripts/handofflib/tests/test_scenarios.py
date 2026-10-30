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

    def test_finish_preserves_existing_staged_and_unstaged_content(self):
        root = os.path.join(self.base, "staged-index")
        scenario.prepare_repo(root, log=lambda _: None)
        runner = scenario.Runner(root, log=lambda _: None)
        doctor = runner.run("doctor.py", ["--fix"], agent="codex")
        self.assertIn(doctor.returncode, (0, 1), doctor.stdout + doctor.stderr)
        started = runner.run("checkpoint.py", ["start", "--id", "test-0001", "--title", "test",
                             "--goal", "test staged index", "--scope", "src/combat"], agent="codex")
        self.assertEqual(started.returncode, 0, started.stderr)
        target = "src/combat/ComboState.txt"
        scenario._write(root, target, "staged version\n")
        self.assertEqual(scenario._git(root, "add", "--", target).returncode, 0)
        scenario._write(root, target, "unstaged version\n")
        before = scenario._git(root, "show", ":" + target).stdout
        head = scenario._git(root, "rev-parse", "HEAD").stdout
        proc = runner.run("finish.py", ["--id", "test-0001", "--commit", "test",
                          "--allow-unverified"], agent="codex")
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertEqual(scenario._git(root, "show", ":" + target).stdout, before)
        self.assertEqual(scenario._git(root, "rev-parse", "HEAD").stdout, head)
        with open(os.path.join(root, target), encoding="utf-8") as content:
            self.assertEqual(content.read(), "unstaged version\n")


if __name__ == "__main__":
    unittest.main()
