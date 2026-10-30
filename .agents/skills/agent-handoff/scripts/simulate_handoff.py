#!/usr/bin/env python3
"""Run the cross-agent handoff simulation end to end and print the transcript.

    python .agents/skills/agent-handoff/scripts/simulate_handoff.py
    python .agents/skills/agent-handoff/scripts/simulate_handoff.py --keep
    python .agents/skills/agent-handoff/scripts/simulate_handoff.py --scenario blocked

Scenarios (each runs in its own throwaway git repository):

  handoff        Codex starts a task, checkpoints, keeps working without a
                 checkpoint, "crashes"; DSH detects RECOVERY MODE, takes over,
                 records the recovery, finishes the task and commits.
  blocked        A live foreign lock must stop DSH from taking over.
  failing-check  A failing declared check keeps the task active (exit 5).

Nothing in the real project is touched: everything happens under ``work/``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from handofflib import report, scenario, util  # noqa: E402


def project_root() -> str:
    # <skill>/scripts/simulate_handoff.py -> <project>
    skill = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.dirname(os.path.dirname(os.path.dirname(skill)))


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="simulate_handoff.py",
        description="Drive the real checkpoint/resume/finish scripts in a throwaway repository.",
    )
    parser.add_argument(
        "--scenario",
        default="all",
        choices=list(scenario.SCENARIOS) + ["all"],
        help="which scenario to run (default: all)",
    )
    parser.add_argument("--workdir", default=None, help="parent directory (default: <project>/work/handoff-sim)")
    parser.add_argument("--keep", action="store_true", help="keep the generated repositories")
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    args = parser.parse_args()

    base_root = args.workdir or os.path.join(project_root(), "work", "handoff-sim")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(base_root, stamp)
    os.makedirs(base, exist_ok=True)

    names = list(scenario.SCENARIOS) if args.scenario == "all" else [args.scenario]
    log = (lambda message: None) if args.quiet else print

    report.title("agent-handoff simulation")
    report.kv("scenario(s)", ", ".join(names), 16)
    report.kv("scratch root", base, 16)
    report.echo("")

    results = []
    for name in names:
        if not args.quiet:
            report.echo("")
            report.echo("=== scenario: %s ===" % name)
        result = scenario.run_scenario(name, base, log=log)
        results.append(result)

    report.echo("")
    report.title("SIMULATION SUMMARY")
    rows = []
    for result in results:
        passed = sum(1 for step in result["steps"] if step["ok"])
        rows.append(
            [
                result["name"],
                "PASS" if result["ok"] else "FAIL",
                "%d/%d" % (passed, max(passed, len(result["steps"]))),
                result["error"] or "",
            ]
        )
    report.table(["scenario", "result", "steps", "error"], rows)
    ok = all(result["ok"] for result in results)

    if ok:
        report.echo("")
        report.echo("Chain verified: start -> checkpoint -> crash -> RECOVERY -> takeover -> finish.")
        report.echo("Artifacts:")
        for result in results:
            report.echo("  %s" % result["root"])
        if not args.keep:
            report.echo("")
            report.echo("Removing the scratch repositories (pass --keep to inspect them).")
            util.rmtree(base)
    else:
        report.echo("")
        report.echo("Artifacts kept for inspection: %s" % base)
    report.echo("")
    report.echo("RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
