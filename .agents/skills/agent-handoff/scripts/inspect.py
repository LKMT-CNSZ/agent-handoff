#!/usr/bin/env python3
"""Read-only inspection: repository facts, one task's evidence, the file table.

    python inspect.py                     # repo summary + task detail when unambiguous
    python inspect.py --id combat-0142 --files
    python inspect.py --git               # branch, HEAD, diff stat, recent commits
    python inspect.py --json --id combat-0142

This command never writes to the project.  It is safe to run at any time, from
any agent, while somebody else is working.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from handofflib import common, history, locks, report, tasks as tasklib, util  # noqa: E402

COMMAND = "inspect"


def git_summary(context) -> dict:
    git = context.git
    facts = {
        "is_repo": bool(git and git.is_repo()),
        "branch": git.branch() if git else None,
        "detached": git.detached() if git else False,
        "head": git.head() if git else None,
        "head_short": git.head_short() if git else None,
        "commit_count": git.commit_count() if git else 0,
        "status_entries": [],
        "diff_stat": "",
        "recent_commits": [],
    }
    if not git or not git.is_repo():
        return facts
    facts["status_entries"] = git.status_entries()
    facts["diff_stat"] = git.diff_stat()
    facts["recent_commits"] = git.lines(["log", "--oneline", "--no-decorate", "-n", "5"])
    facts["toplevel"] = git.toplevel()
    return facts


def file_rows(context, task, evidence) -> list:
    entries = {entry["path"]: entry for entry in evidence.get("status_entries") or []}
    hash_state = {}
    snapshot = {
        entry["path"]: entry["sha256"]
        for entry in util.as_list_entries(task.get("file_hashes"))
        if isinstance(entry, dict)
    }
    current = {
        entry["path"]: entry["sha256"]
        for entry in (evidence.get("snapshot_hashes") or [])
    }
    for path in evidence.get("changed_files") or []:
        if path in snapshot and path in current:
            hash_state[path] = "same as checkpoint" if snapshot[path] == current[path] else "edited since checkpoint"
        elif path in current:
            hash_state[path] = "new since checkpoint"
        else:
            hash_state[path] = "not in checkpoint"
    rows = []
    for path in evidence.get("changed_files") or []:
        full = os.path.join(context.root, path.replace("/", os.sep))
        exists = os.path.exists(full)
        size = util.file_size(full) if exists else None
        mtime = util.mtime_utc(full) if exists else None
        rows.append(
            [
                path,
                (entries.get(path) or {}).get("code", ""),
                hash_state.get(path, ""),
                "yes" if context.is_binary_path(path) else "",
                util.humanize_seconds(util.age_seconds(util.iso(mtime))) if mtime else ("missing" if not exists else ""),
                str(size) if size is not None else "",
            ]
        )
    return rows


def print_git(context) -> dict:
    facts = git_summary(context)
    report.section("GIT")
    if not facts["is_repo"]:
        report.kv("repository", "not a git work tree (degraded mode)", 16)
        return facts
    report.kv("top level", facts.get("toplevel") or context.root, 16)
    report.kv(
        "branch",
        "%s%s" % (facts["branch"] or "(none)", "  [detached]" if facts["detached"] else ""),
        16,
    )
    report.kv("head", "%s  (%d commit(s))" % (facts["head_short"] or "(no commit)", facts["commit_count"]), 16)
    report.kv("dirty paths", str(len(facts["status_entries"])), 16)
    if facts["recent_commits"]:
        report.echo("")
        report.echo("  recent commits:")
        for line in facts["recent_commits"]:
            report.echo("    %s" % line)
    if facts["diff_stat"]:
        report.echo("")
        report.echo("  diff --stat:")
        for line in facts["diff_stat"].splitlines():
            report.echo("    %s" % line)
    if facts["status_entries"]:
        report.echo("")
        report.echo("  status:")
        for entry in facts["status_entries"][:40]:
            report.echo("    %-2s %s" % (entry["code"], entry["path"]))
        if len(facts["status_entries"]) > 40:
            report.echo("    ... %d more" % (len(facts["status_entries"]) - 40))
    return facts


def print_task(context, task, args) -> dict:
    lock_state = locks.analyze(context, task.task_id)
    evidence = tasklib.collect_evidence(context, task, lock_state)

    report.section("TASK  %s" % task.task_id)
    report.kv("title", task.title, 14)
    report.kv("owner", "%s  status=%s" % (task.get("owner"), task.status), 14)
    report.kv("branch", "%s  base=%s" % (task.get("branch") or "-", task.get("base_branch") or "-"), 14)
    report.kv(
        "checkpoint",
        "%s  %s  by %s  kind=%s"
        % (
            task.checkpoint_id() or "-",
            task.checkpoint.get("at") or "-",
            task.checkpoint.get("by") or "-",
            task.checkpoint.get("kind") or "-",
        ),
        14,
    )
    report.kv("created", "%s  (updated %s)" % (task.get("created_at"), task.get("updated_at")), 14)
    report.kv("mode", tasklib.MODE_TEXT.get(evidence["mode"], evidence["mode"]), 14)
    if task.get("next_action"):
        report.kv("next", util.truncate(str(task.get("next_action")), 100), 14)
    if task.get_list("dependencies"):
        report.kv("depends on", ", ".join(task.get_list("dependencies")), 14)
    if task.get_list("write_scope"):
        report.kv("write scope", ", ".join(task.get_list("write_scope")), 14)

    report.section("CHECKPOINT vs REALITY")
    for check in evidence.get("checks") or []:
        report.check_line(check)
    for reason_text in evidence.get("reasons") or []:
        report.echo("  - %s" % reason_text)

    report.section("LOCK")
    report.kv(
        "state",
        "%s   agent=%s machine=%s pid=%s heartbeat=%s"
        % (
            lock_state["state"],
            lock_state.get("agent") or "-",
            lock_state.get("machine") or "-",
            lock_state.get("pid") or "-",
            lock_state.get("heartbeat") or "-",
        ),
        14,
    )
    for reason_text in lock_state.get("reasons") or []:
        report.echo("  - %s" % reason_text)

    if task.get_list("completed"):
        report.section("COMPLETED (checkpoint text, unverified)")
        for item in task.get_list("completed"):
            report.echo("  - %s" % item)
    if task.get_list("remaining"):
        report.section("REMAINING")
        for item in task.get_list("remaining"):
            report.echo("  - %s" % item)
    if task.get_list("warnings"):
        report.section("WARNINGS")
        for item in task.get_list("warnings"):
            report.echo("  ! %s" % item)
    if task.get("verification") or task.get("verification_commands"):
        report.section("VERIFICATION")
        verification = util.as_dict(task.get("verification"))
        for key, value in verification.items():
            report.kv(key, value, 14)
        for entry in util.as_list_entries(task.get("verification_commands")):
            if isinstance(entry, dict):
                required = util.as_bool(entry.get("required"), True)
                report.echo("  [%s] %s: %s" % ("req" if required else "opt", entry.get("name"), entry.get("command")))

    rows = file_rows(context, task, evidence)
    if args.files and rows:
        report.section("CHANGED FILES (%d)" % len(rows))
        report.table(["path", "git", "checkpoint", "bin", "modified", "size"], rows)
    elif rows:
        report.section("CHANGED FILES (%d)  [use --files for the table]" % len(rows))
        for row in rows[:15]:
            report.echo("  %-2s %s" % (row[1], row[0]))
        if len(rows) > 15:
            report.echo("  ... %d more" % (len(rows) - 15))

    events = history.read_events(context, task.task_id)
    if events:
        report.section("JOURNAL (last 8)")
        for event in events[-8:]:
            report.echo(
                "  %s  %-20s %-10s %s"
                % (
                    event.get("at", "?"),
                    event.get("event", "?"),
                    event.get("agent", "?"),
                    util.truncate(
                        str(event.get("data", {}).get("checkpoint")
                            or event.get("data", {}).get("mode")
                            or event.get("data", {}).get("reason")
                            or ""),
                        40,
                    ),
                )
            )

    claimed_missing = evidence.get("missing_claimed_files") or []
    if claimed_missing:
        report.section("CLAIMED FILES THAT NO LONGER EXIST")
        for path in claimed_missing:
            report.echo("  ! %s" % path)
    return evidence


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="inspect.py", description="Read-only report about the project and its active tasks."
    )
    common.add_common_arguments(parser)
    parser.add_argument("--id", help="inspect one task")
    parser.add_argument("--files", action="store_true", help="print the changed-file table")
    parser.add_argument("--git", action="store_true", help="print git facts and dirty paths")
    args = parser.parse_args()
    context = common.build_context(args)

    active, problems = tasklib.load_active_tasks(context)
    selected = None
    if args.id:
        selected = next((task for task in active if task.task_id == args.id.strip()), None)
        if selected is None:
            path = tasklib.find_task_file(context, args.id.strip())
            hint = " (found at %s)" % report.relpath(path, context.root) if path else ""
            raise common.CliError("no active task with id %r%s" % (args.id, hint))
    elif len(active) == 1:
        selected = active[0]

    if args.json:
        payload = {
            "ok": True,
            "project": context.root,
            "git": git_summary(context),
            "active_tasks": [
                {
                    "task_id": task.task_id,
                    "title": task.title,
                    "status": task.status,
                    "owner": task.get("owner"),
                    "checkpoint": task.checkpoint,
                    "updated_at": task.get("updated_at"),
                    "lock": locks.analyze(context, task.task_id),
                }
                for task in active
            ],
            "problems": problems,
        }
        if selected is not None:
            lock_state = locks.analyze(context, selected.task_id)
            evidence = tasklib.collect_evidence(context, selected, lock_state)
            payload["task"] = {
                "raw": selected.to_ordered(),
                "mode": evidence.get("mode"),
                "reasons": evidence.get("reasons"),
                "checks": evidence.get("checks"),
                "evidence": {
                    "dirty": evidence.get("dirty"),
                    "changed_files": evidence.get("changed_files"),
                    "new_since_checkpoint": evidence.get("new_since_checkpoint"),
                    "hash_drift": evidence.get("hash_drift"),
                    "missing_claimed_files": evidence.get("missing_claimed_files"),
                    "binary_changed": evidence.get("binary_changed"),
                },
                "lock": lock_state,
                "journal": history.read_events(context, selected.task_id),
            }
        common.emit_json(payload)
        return common.EXIT_OK

    if not args.quiet:
        common.print_header(context, "inspect")
    git_facts = git_summary(context)
    report.title("PROJECT INSPECTION")
    report.kv("project", context.root, 16)
    report.kv(
        "git",
        "%s @ %s  %d dirty path(s), incl. .agent-state"
        % (
            git_facts.get("branch") or "(none)",
            git_facts.get("head_short") or "(no commit)",
            len(git_facts.get("status_entries") or []),
        ),
        16,
    )
    report.kv("active tasks", str(len(active)), 16)
    report.kv("state dir", report.relpath(context.state_dir, context.root), 16)
    if args.git:
        print_git(context)

    if selected is not None:
        print_task(context, selected, args)
    elif active:
        report.section("ACTIVE TASKS")
        report.table(
            ["task_id", "title", "status", "owner", "checkpoint", "updated", "lock"],
            [
                [
                    task.task_id,
                    util.truncate(task.title, 30),
                    task.status,
                    str(task.get("owner") or "-"),
                    task.checkpoint_id() or "-",
                    util.humanize_seconds(util.age_seconds(task.get("updated_at"))),
                    locks.analyze(context, task.task_id)["state"],
                ]
                for task in active
            ],
        )
        report.echo("")
        report.echo("  %d active tasks: pass --id TASK_ID for full evidence." % len(active))
    else:
        report.section("NO ACTIVE TASK")
        report.echo("  Nothing is in progress.  Create one with:")
        report.echo(
            '    python .agents/skills/agent-handoff/scripts/checkpoint.py start '
            '--id <area-NNNN> --title "..." --goal "..."'
        )

    for problem in problems:
        report.echo("  ! %s: %s" % (report.relpath(problem.get("path"), context.root), problem.get("message")))
    return common.EXIT_OK


if __name__ == "__main__":
    sys.exit(common.run(main, COMMAND))
