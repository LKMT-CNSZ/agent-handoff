#!/usr/bin/env python3
"""Close a task: verify, commit (explicit), archive to completed/, keep history.

    python finish.py --id combat-0142 --commit "combat: three-hit light combo"
    python finish.py --id combat-0142 --skip-checks --allow-unverified
    python finish.py --id combat-0142 --fail --reason "blocked on missing animation"

Rules enforced here:

* the work tree must be clean after an optional explicit commit, so finished work
  is never left only on disk;
* ``remaining`` must be empty, or the caller must state ``--keep-remaining``;
* declared verification commands run unless ``--skip-checks`` is explicit;
* the state file is *moved* to completed/ and the journal is appended - nothing
  is deleted.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from handofflib import checks, common, history, locks, report, tasks as tasklib, util  # noqa: E402

COMMAND = "finish"


def _verification_facts(task) -> dict:
    facts = dict(util.as_dict(task.get("verification")))
    for entry in util.as_list_entries(task.get("verification_runs")):
        if isinstance(entry, dict) and entry.get("name"):
            facts[str(entry.get("name"))] = str(entry.get("status") or "")
    return facts


def _dirty_paths(context) -> list:
    if not context.git:
        return []
    return [entry["path"] for entry in context.git.status_entries()]


def _split_dirty(context, task, dirty, add_all: bool) -> tuple:
    """Split dirty paths into work paths, state paths and other people's paths."""

    declared = set(task.get_list("changed_files"))
    scopes = [scope.strip().rstrip("/") for scope in task.get_list("write_scope") if scope.strip()]
    work, state, outside = [], [], []
    for path in dirty:
        if path.startswith(".agent-state/"):
            if not path.startswith(".agent-state/locks/"):
                state.append(path)
            continue
        if add_all:
            work.append(path)
            continue
        if path in declared or any(path == scope or path.startswith(scope + "/") for scope in scopes):
            work.append(path)
            continue
        outside.append(path)
    return work, state, outside


def _commit_paths(context, paths, message: str, notes, label: str):
    """Stage exactly these paths and commit them.  Returns the new HEAD."""

    git = context.git
    if not paths:
        notes.append("nothing to commit for %s" % label)
        return None
    result = git.add(paths)
    if not result.ok:
        raise common.CliError(
            "git add failed for %s: %s" % (label, (result.err or result.out).strip()),
            common.EXIT_ERROR,
        )
    staged = git.lines(["diff", "--cached", "--name-only"])
    if not staged:
        notes.append("nothing staged for %s" % label)
        return None
    commit = git.commit(message)
    if not commit.ok:
        raise common.CliError(
            "git commit failed for %s: %s" % (label, (commit.err or commit.out).strip()),
            common.EXIT_ERROR,
        )
    notes.append("committed %d path(s) for %s" % (len(staged), label))
    return git.head()


def _archive(context, task, target_status: str, extra: dict) -> str:
    source = task.path
    if not source or not os.path.isfile(source):
        raise common.CliError("task state file disappeared: %r" % source)
    target = tasklib.task_path(context, task.task_id, target_status)
    if os.path.exists(target):
        raise common.CliError("refusing to overwrite existing %s" % report.relpath(target, context.root))
    for key, value in extra.items():
        task.set(key, value)
    task.touch()
    task.save(target)
    os.remove(source)
    return target


def _human(context, task, args, results, notes, final_commit, archived) -> None:
    report.title("FINISH  %s" % task.task_id)
    report.kv("title", task.title, 14)
    report.kv("owner", "%s -> %s" % (task.get("owner"), context.agent), 14)
    report.kv("branch", str(task.get("branch") or "(none)"), 14)
    report.kv("commit", final_commit or "(none)", 14)
    report.kv("archived", report.relpath(archived, context.root), 14)
    if results:
        report.section("VERIFICATION")
        for record in results:
            report.echo(
                "  [%s] %-16s %s (%ss)"
                % (
                    "pass" if record.get("status") == checks.PASS else record.get("status"),
                    record.get("name"),
                    record.get("command"),
                    record.get("duration_seconds"),
                )
            )
            tail = (record.get("output_tail") or "").splitlines()[-6:]
            for line in tail:
                report.echo("        %s" % line)
        report.echo("")
        report.kv("verification", checks.overall(results), 14)
    else:
        report.kv("verification", "not run", 14)
    if task.get_list("remaining"):
        report.section("REMAINING (kept deliberately)")
        for item in task.get_list("remaining"):
            report.echo("  - %s" % item)
    if notes:
        report.section("NOTES")
        for note in notes:
            report.echo("  - %s" % note)
    report.section("NEXT")
    report.bullet(
        [
            "push the branch when the work is ready to share: git push -u origin %s"
            % (task.get("branch") or "HEAD"),
            "review and merge outside the handoff system; it never merges for you",
            "start the next task with checkpoint.py start --id <area-NNNN> ...",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="finish.py", description="Verify, commit (explicit), archive a task and append its journal."
    )
    common.add_common_arguments(parser)
    parser.add_argument("--id", help="task id (optional when exactly one task is active)")
    parser.add_argument("--commit", metavar="MESSAGE", help="stage this task's paths and commit them")
    parser.add_argument("--commit-all", action="store_true", help="with --commit: stage every dirty path")
    parser.add_argument("--skip-checks", action="store_true", help="do not run declared verification commands")
    parser.add_argument("--only-check", action="append", default=[], help="run only these named checks")
    parser.add_argument("--include-optional", action="store_true", help="also run checks marked required=false")
    parser.add_argument("--timeout", type=int, default=None, help="per-command timeout in seconds")
    parser.add_argument("--allow-unverified", action="store_true", help="record an explicit unverified warning")
    parser.add_argument("--keep-remaining", action="store_true", help="archive even with remaining items")
    parser.add_argument("--force", action="store_true", help="take a live lock to finish")
    parser.add_argument("--dry-run", action="store_true", help="report what would happen, write nothing")
    parser.add_argument("--fail", action="store_true", help="archive into failed/ instead of completed/")
    parser.add_argument("--reason", help="reason for --fail")
    args = parser.parse_args()

    context = common.build_context(args)
    task, notes = tasklib.resolve_task(context, args.id)
    if task is None:
        raise common.CliError("; ".join(notes) if notes else "no active task to finish")

    lock_state = locks.analyze(context, task.task_id)
    if lock_state["state"] in ("live_foreign", "corrupt"):
        allowed, reason = locks.can_take(context, lock_state, args.force)
        if not allowed:
            raise common.CliError(reason, common.EXIT_BLOCKED)
        notes.append(reason)
        if not args.dry_run:
            history.append_event(
                context,
                task.task_id,
                "lock_forced",
                data={"from_agent": lock_state.get("agent"), "reason": reason, "arguments": sys.argv[1:]},
            )

    # ---------------------------------------------------------------- fail --
    if args.fail:
        if not args.reason:
            raise common.CliError("--fail requires --reason TEXT", common.EXIT_REFUSED)
        if args.dry_run:
            report.echo("DRY RUN: would archive %s into failed/ (%s)" % (task.task_id, args.reason))
            return common.EXIT_OK
        snapshot_note = "dirty %d path(s)" % len(_dirty_paths(context))
        target = _archive(
            context,
            task,
            "failed",
            {"status": "failed", "finished_at": util.iso(), "finished_by": context.agent,
             "failure_reason": args.reason},
        )
        history.append_event(
            context,
            task.task_id,
            "failed",
            data={
                "reason": args.reason,
                "agent": context.agent,
                "head": context.git.head() if context.git else None,
                "work_tree": snapshot_note,
                "remaining": task.get_list("remaining"),
                "state_file": report.relpath(target, context.root),
            },
        )
        locks.remove(context, task.task_id, agent=context.agent)
        common.audit(context, COMMAND, sys.argv[1:], "failed", task.task_id, detail=args.reason)
        if args.json:
            common.emit_json(
                {"ok": True, "action": "fail", "task_id": task.task_id, "reason": args.reason,
                 "state_file": target, "notes": notes}
            )
            return common.EXIT_OK
        report.title("TASK FAILED  %s" % task.task_id)
        report.kv("reason", args.reason, 12)
        report.kv("archived", report.relpath(target, context.root), 12)
        return common.EXIT_OK

    # -------------------------------------------------------- remaining ----
    remaining = task.get_list("remaining")
    if remaining and not args.keep_remaining:
        raise common.CliError(
            "task still has %d remaining item(s): %s\n"
            "  Finish them, remove them with `checkpoint.py save --drop-todo PREFIX`, or pass "
            "--keep-remaining to archive the task with the list recorded."
            % (
                len(remaining),
                "; ".join(util.truncate(item, 60) for item in remaining[:5]),
            ),
            common.EXIT_REFUSED,
        )
    if remaining and args.keep_remaining:
        notes.append("%d remaining item(s) archived deliberately with --keep-remaining" % len(remaining))

    # A plain git commit includes the entire index, even when git add was
    # limited to task paths. Never absorb a user's existing staged changes or
    # overwrite partially staged content while preparing the two commits.
    if not args.dry_run and context.git:
        staged = [entry["path"] for entry in context.git.status_entries() if entry["staged"]]
        if staged:
            raise common.CliError(
                "finish requires an empty Git index; existing staged changes were left untouched:\n  %s"
                % "\n  ".join(staged[:15]), common.EXIT_REFUSED,
            )

    # ----------------------------------------------------------- checks ----
    results = []
    if args.skip_checks:
        notes.append("verification commands skipped (--skip-checks)")
    elif args.dry_run:
        notes.append("dry run: verification commands were not executed")
    else:
        results = checks.run_task_checks(
            context,
            task.raw,
            only=args.only_check or None,
            include_optional=args.include_optional,
            timeout=args.timeout,
        )
        if not results:
            notes.append(
                "this task declares no verification commands; verification recorded as unverified"
            )
    failures = checks.required_failures(results)
    if failures and not args.dry_run:
        history.append_event(
            context,
            task.task_id,
            "finish_blocked",
            data={
                "reason": "verification failed",
                "failures": [
                    {
                        "name": record.get("name"),
                        "command": record.get("command"),
                        "status": record.get("status"),
                        "exit_code": record.get("exit_code"),
                        "output_tail": util.truncate(record.get("output_tail") or "", 1500),
                    }
                    for record in failures
                ],
                "agent": context.agent,
            },
        )
        common.audit(context, COMMAND, sys.argv[1:], "blocked-checks", task.task_id)
        if args.json:
            common.emit_json(
                {
                    "ok": False,
                    "action": "finish",
                    "task_id": task.task_id,
                    "verification": checks.overall(results),
                    "runs": results,
                    "message": "required verification failed; the task stays active",
                }
            )
            return common.EXIT_CHECKS_FAILED
        report.title("FINISH BLOCKED  %s" % task.task_id)
        report.section("VERIFICATION")
        for record in results:
            report.echo(
                "  [%s] %-16s %s" % (record.get("status"), record.get("name"), record.get("command"))
            )
            for line in (record.get("output_tail") or "").splitlines()[-8:]:
                report.echo("        %s" % line)
        report.echo("")
        report.echo("The task stays active. Fix the failure, then run finish again.")
        report.echo("Journal entry: finish_blocked")
        return common.EXIT_CHECKS_FAILED

    unverified = not results
    if unverified and not args.allow_unverified:
        raise common.CliError(
            "no verification result is available; pass --allow-unverified to record that explicitly",
            common.EXIT_REFUSED,
        )

    # ----------------------------------------------------------- commit ----
    final_commit = context.git.head() if context.git else None
    dirty = _dirty_paths(context)
    check_note = checks.overall(results)
    work_paths, state_paths, outside = _split_dirty(context, task, dirty, args.commit_all)

    if args.dry_run:
        report.title("DRY RUN  %s" % task.task_id)
        report.kv("verification", check_note, 14)
        report.kv("dirty work paths", str(len(work_paths)), 14)
        report.kv("dirty state paths", str(len(state_paths)), 14)
        report.kv("outside scope", str(len(outside)), 14)
        for path in (work_paths + outside)[:20]:
            report.echo("  %s" % path)
        report.kv("commit arg", args.commit or "(none)", 14)
        report.echo("")
        report.echo("No file was written.  Re-run without --dry-run to finish.")
        return common.EXIT_OK

    runs = [
        {
            "name": str(record.get("name")),
            "command": util.truncate(str(record.get("command")), 300),
            "status": str(record.get("status")),
            "exit_code": str(record.get("exit_code") if record.get("exit_code") is not None else ""),
            "at": util.iso(),
            "required": "true" if record.get("required", True) else "false",
        }
        for record in results
    ]

    if not args.commit:
        raise common.CliError(
            "finish archives the task, so the work and the handoff state must be committed:\n"
            "  work paths: %d, state paths: %d\n"
            "  Run: finish.py --commit \"message\"   (add --commit-all to stage every dirty path)"
            % (len(work_paths), len(state_paths)),
            common.EXIT_REFUSED,
        )
    if outside:
        raise common.CliError(
            "%d dirty path(s) outside this task's scope would be left behind:\n  %s\n"
            "  Commit, stash or revert them yourself first; nothing was changed by this run."
            % (len(outside), "\n  ".join(outside[:15])),
            common.EXIT_REFUSED,
        )

    # 1) the work itself
    work_commit = _commit_paths(context, work_paths, args.commit, notes, "the task work")
    if work_commit:
        final_commit = work_commit
    elif final_commit is None:
        notes.append("no baseline commit: the archived state will not be reachable from git")

    verification_facts = _verification_facts(task)
    for record in results:
        verification_facts[str(record.get("name"))] = str(
            "pass" if record.get("status") == checks.PASS else record.get("status")
        )
    if not results:
        verification_facts.setdefault("recorded", "unverified")

    extra = {
        "status": "completed",
        "commit": final_commit,
        "finished_at": util.iso(),
        "finished_by": context.agent,
        "verification": verification_facts,
        "verification_runs": runs,
    }
    if not runs:
        notes.append("archived as unverified; no command output is recorded")

    # 2) archive the task, journal it, release the lock
    target = _archive(context, task, "completed", extra)
    history.append_event(
        context,
        task.task_id,
        "finished",
        data={
            "commit": final_commit,
            "agent": context.agent,
            "verification": verification_facts,
            "runs": runs,
            "remaining_kept": task.get_list("remaining"),
            "warnings": task.get_list("warnings"),
            "notes": notes,
            "state_file": report.relpath(target, context.root),
        },
    )
    lock_removed = locks.remove(context, task.task_id, agent=context.agent)
    if not lock_removed:
        notes.append("no lock file to remove (already gone)")
    common.audit(
        context,
        COMMAND,
        sys.argv[1:],
        "completed",
        task.task_id,
        detail="commit=%s verification=%s" % (final_commit, check_note),
    )

    # 3) the handed-over state travels in its own commit so any machine can resume
    state_commit = _commit_paths(
        context,
        [path for path in _dirty_paths(context) if path.startswith(".agent-state/")
         and not path.startswith(".agent-state/locks/")],
        "handoff: archive %s (%s)" % (task.task_id, task.get("branch") or "no branch"),
        notes,
        "the handoff state",
    )
    leftovers = [
        path
        for path in _dirty_paths(context)
        if not path.startswith(".agent-state/locks/")
    ]
    if leftovers:
        raise common.CliError(
            "the task is archived but %d path(s) are still dirty:\n  %s\n"
            "  The task state is already committed/moved; inspect and commit the rest yourself."
            % (len(leftovers), "\n  ".join(leftovers[:15])),
            common.EXIT_ERROR,
        )
    head_after = context.git.head() or final_commit
    if state_commit:
        notes.append("handoff state committed as %s" % state_commit[:8])
    events = history.read_events(context, task.task_id)
    if args.json:
        common.emit_json(
            {
                "ok": True,
                "action": "finish",
                "task_id": task.task_id,
                "commit": final_commit,
                "state_commit": state_commit,
                "head_after": head_after,
                "verification": verification_facts,
                "runs": results,
                "state_file": target,
                "journal_events": len(events),
                "lock_removed": lock_removed,
                "notes": notes,
            }
        )
        return common.EXIT_OK
    if not args.quiet:
        common.print_header(context, "finish")
    _human(context, task, args, results, notes, final_commit, target)
    return common.EXIT_OK


if __name__ == "__main__":
    sys.exit(common.run(main, COMMAND))
