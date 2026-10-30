#!/usr/bin/env python3
"""Run the agent-handoff test suite.

    python .agents/skills/agent-handoff/scripts/run_tests.py
    python .agents/skills/agent-handoff/scripts/run_tests.py -v
    HANDOFF_TEST_VERBOSE=1 ...   # show every scenario step

Standard library only: unittest discovery, no pytest required.
"""

from __future__ import annotations

import os
import sys
import unittest


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    tests_dir = os.path.join(here, "handofflib", "tests")
    if here not in sys.path:
        sys.path.insert(0, here)
    sys.path.insert(0, os.path.dirname(here))

    verbosity = 2 if any(arg in ("-v", "--verbose") for arg in sys.argv[1:]) else 1
    loader = unittest.TestLoader()
    suite = loader.discover(tests_dir, pattern="test_*.py", top_level_dir=here)
    if suite.countTestCases() == 0:
        sys.stderr.write("no tests discovered under %s\n" % tests_dir)
        return 2
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    print("")
    print("tests run: %d  failures: %d  errors: %d  skipped: %d"
          % (result.testsRun, len(result.failures), len(result.errors), len(result.skipped)))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
