#!/usr/bin/env python3
"""Write a checkpoint (or create a task) in ``.agent-state``.

    python checkpoint.py start --id combat-0142 --title "三段轻攻击" \
        --goal "实现玩家三段轻攻击 Combo" --check "rules=pwsh -NoProfile -File scripts/Test-CombatRules.ps1"

    python checkpoint.py save  --done "第三段攻击完成" --todo "hitbox 测试" \
        --next "运行 CombatComboTest"

Git facts are collected automatically; agents only describe intent and progress.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from handofflib import common, history, locks, report, tasks as tasklib, util  # noqa: E402

COMMAND = "checkpoint"


def _read_goal(args) -> str:
    if args.goal_file:
        text = util.read_text(args.goal_file)
        if text is None:
            raise common.CliError("cannot read --goal-file %s" % args.goal_file)
        return text.strip()
    return (args.goal or "").strip()


def _parse_pairs(values) -> dict:
    pairs = {}
    for item in values or []:
        if "=" not in item:
            raise common.CliError(
                "expected NAME=VALUE for --verification, got %r" % item, common.EXIT_REFUSED
            )
        name, value = item.split("=", 1)
        name = name.strip()
        if not name:
            raise common.CliError("empty verification name in %r" % item, common.EXIT_REFUSED)
        pairs[name] = value.strip()
    return pairs


def _parse_checks(values, required: bool) -> list:
    checks = []
    for item in values or []:
        if "=" not in item:
            raise common.CliError(
                "expected NAME=COMMAND for --check/--optional-check, got %r" % item,
                common.EXIT_REFUSED,
            )
        name, command = item.split("=", 1)
        name = name.strip()
        command = command.strip()
        if not name or not command:
            raise common.CliError("empty check name or command in %r" % item, common.EXIT_REFUSED)
        checks.append(
            {"name": name, "command": command, "required": "true" if required else "false"}
        )
    return checks


def _is_protected(context, branch: str) -> bool:
    return bool(branch) and branch in context.protected_branches()


def _branch_for_new_task(context, args) -> tuple:
    """Return ``(branch_name_or_None, notes)`` and perform creation."""

    notes = []
    if args.branch is not None and args.branch.strip().lower() in ("none", "no", "off", ""):
        return None, ["no branch requested (--branch none)"]
    if args.branch and args.branch not in ("auto",):
        name = args.branch.strip()
    else:
        name = context.branch_prefix() + args.id

    git = context.git
    if not git.has_commit():
        notes.append(
            "repository has no commit yet, so no branch could be created; "
            "task records branch=null until a baseline commit exists"
        )
        return None, notes
    if _is_protected(context, name):
        raise common.CliError(
            "refusing to use protected branch %r for a task; pick feature/<id>" % name,
            common.EXIT_REFUSED,
        )
    current = git.branch()
    if git.branch_exists(name):
        if current == name:
            notes.append("branch %r already exists and is checked out" % name)
            return name, notes
        raise common.CliError(
            "branch %r already exists but is not checked out; refusing to switch branches "
            "implicitly. Run 'git switch %s' yourself, or pass --branch none."
            % (name, name),
            common.EXIT_REFUSED,
        )
    if current and _is_protected(context, current) and not args.allow_protected_branch:
        notes.append("creating %r from protected branch %r" % (name, current))
    start = args.base_branch or None
    if start and not git.any_ref_exists(start):
        raise common.CliError("--base-branch %r does not exist" % start, common.EXIT_REFUSED)
    result = git.create_branch(name, start)
    if not result.ok:
        raise common.CliError(
            "could not create branch %r: %s" % (name, (result.err or result.out).strip()),
            common.EXIT_ERROR,
        )
    notes.append("created branch %r" % name)
    return name, notes


def _write_lock(context, task, notes, claim_note: str, force: bool = False) -> None:
    lock_state = locks.analyze(context, task.task_id)
    if lock_state["state"] == "live_foreign" and not force:
        raise common.CliError(
            "task is locked by %s@%s (heartbeat %s ago); pass --force only after "
            "confirming that agent stopped"
            % (lock_state["agent"], lock_state["machine"], lock_state["age_human"]),
            common.EXIT_BLOCKED,
        )
    path = locks.write(context, task.raw, context.agent, branch=str(task.get("branch") or ""))
    notes.append(claim_note)
    if lock_state["state"] in ("stale", "corrupt"):
        notes.append(
            "took over %s lock from %s@%s (%s)"
            % (
                lock_state["state"],
                lock_state.get("agent"),
                lock_state.get("machine"),
                "; ".join(lock_state.get("reasons") or []),
            )
        )
        history.append_event(
            context,
            task.task_id,
            "lock_stale_taken",
            data={"from": "%s@%s" % (lock_state.get("agent"), lock_state.get("machine")),
                  "reasons": lock_state.get("reasons"), "lock": path},
        )
    elif lock_state["state"] == "live_same":
        locks.refresh(context, task.task_id)


def _finish_checkpoint(context, task, kind: str, note: str, notes) -> str:
    snapshot = tasklib.collect_snapshot(context)
    git = context.git
    previous = task.checkpoint
    count = util.as_int(previous.get("count"), 0) + 1
    checkpoint_id = "ck-%04d" % count
    head = git.head() if git else None
    task.set("changed_files", snapshot["changed_files"])
    task.set("file_hashes", snapshot["file_hashes"])
    task.set("hashes_capped", "true" if snapshot["capped"] else "false")
    task.set(
        "checkpoint",
        {
            "id": checkpoint_id,
            "at": util.iso(),
            "by": context.agent,
            "kind": kind,
            "count": str(count),
            "head": head or "",
            "dirty": "true" if snapshot["count"] else "false",
            "changed_count": str(snapshot["count"]),
            "note": note or "",
        },
    )
    task.set(
        "git_snapshot",
        {
            "branch": (git.branch() if git else None) or "",
            "head": head or "",
            "head_short": (head[:8] if head else ""),
            "dirty": "true" if snapshot["count"] else "false",
            "changed_count": str(snapshot["count"]),
            "captured_at": util.iso(),
        },
    )
    task.touch()
    return checkpoint_id


def _human_summary(context, task, snapshot_note: str) -> None:
    checkpoint = task.checkpoint
    report.echo("")
    report.kv("task", "%s  %s" % (task.task_id, task.title))
    report.kv("owner", task.get("owner"))
    report.kv(
        "checkpoint",
        "%s  %s  by %s" % (checkpoint.get("id"), checkpoint.get("at"), checkpoint.get("by")),
    )
    branch = task.get("branch") or "(none)"
    head = task.git_snapshot.get("head_short") or "(no commit yet)"
    report.kv("git", "%s @ %s" % (branch, head))
    dirty_flag = "dirty" if util.as_bool(checkpoint.get("dirty"), False) else "clean"
    report.kv(
        "worktree",
        "%s, %s path(s) changed, %s hashed"
        % (dirty_flag, checkpoint.get("changed_count"), len(task.get_list("file_hashes"))),
    )
    binary = [path for path in task.get_list("changed_files") if context.is_binary_path(path)]
    if binary:
        report.kv("binary", "%d changed: %s" % (len(binary), ", ".join(binary[:3])))
    if task.get_list("completed"):
        report.kv("completed", "%d item(s)" % len(task.get_list("completed")))
    if task.get_list("remaining"):
        report.kv("remaining", "%d item(s)" % len(task.get_list("remaining")))
        for item in task.get_list("remaining")[:4]:
            report.echo("    - %s" % util.truncate(item, 90))
    if task.get("next_action"):
        report.kv("next", util.truncate(str(task.get("next_action")), 110))
    if snapshot_note:
        report.echo("")
        report.echo("  %s" % snapshot_note)


def _start(args) -> int:
    context = common.build_context(args)
    task_id = args.id.strip()
    if not tasklib.TASK_ID_RE.match(task_id):
        raise common.CliError(
            "task id %r is invalid: use lowercase letters, digits, '.', '_' or '-'" % task_id,
            common.EXIT_REFUSED,
        )
    existing = tasklib.find_task_file(context, task_id)
    if existing:
        raise common.CliError(
            "task %r already exists at %s; use 'save' to continue it, or pick a new id"
            % (task_id, report.relpath(existing, context.root)),
            common.EXIT_REFUSED,
        )
    goal = _read_goal(args)
    if not goal:
        raise common.CliError("a goal is required: pass --goal TEXT or --goal-file PATH")
    title = args.title.strip()
    if not title:
        raise common.CliError("--title must not be empty")

    notes = []
    git = context.git
    current = git.branch()
    if current and _is_protected(context, current) and args.branch != "none" and not args.allow_protected_branch:
        notes.append(
            "started from protected branch %r; the task branch is created from it, "
            "and main itself must stay untouched" % current
        )
    base_branch = args.base_branch or (current or "") if current else (args.base_branch or "")
    branch, branch_notes = _branch_for_new_task(context, args)
    notes.extend(branch_notes)

    task = tasklib.Task(
        {
            "task_id": task_id,
            "format_version": str(tasklib.SCHEMA_VERSION),
            "title": title,
            "goal": goal,
            "owner": context.agent,
            "status": "in_progress",
            "created_at": util.iso(),
            "updated_at": util.iso(),
            "branch": branch,
            "base_branch": base_branch,
            "commit": None,
            "parent_task": args.parent or None,
            "dependencies": args.dep or [],
            "write_scope": args.scope or [],
            "next_action": args.next or "",
            "completed": [],
            "remaining": args.remaining or [],
            "changed_files": [],
            "file_hashes": [],
            "hashes_capped": "false",
            "verification": _parse_pairs(args.verification),
            "verification_commands": _parse_checks(args.check, True)
            + _parse_checks(args.optional_check, False),
            "checkpoint": {},
            "git_snapshot": {},
            "warnings": args.warning or [],
            "notes": args.note or "",
        }
    )
    problems = task.validate()
    if problems:
        raise common.CliError("task definition is invalid: " + "; ".join(problems))
    for dependency in task.get_list("dependencies"):
        if not tasklib.find_task_file(context, dependency):
            notes.append("dependency %r does not exist (recorded as declared)" % dependency)

    checkpoint_id = _finish_checkpoint(context, task, args.kind or "baseline", args.note or "", notes)
    path = task.save(tasklib.task_path(context, task_id))
    if not args.no_lock:
        _write_lock(context, task, notes, "lock created for %s" % context.agent)
    history.append_event(
        context,
        task_id,
        "task_created",
        data={
            "checkpoint": checkpoint_id,
            "branch": task.get("branch"),
            "base_branch": task.get("base_branch"),
            "head": task.git_snapshot.get("head"),
            "goal": goal,
            "remaining": task.get_list("remaining"),
            "write_scope": task.get_list("write_scope"),
            "dependencies": task.get_list("dependencies"),
        },
    )
    common.audit(
        context,
        COMMAND,
        sys.argv[1:],
        "created",
        task_id,
        detail="checkpoint=%s branch=%s" % (checkpoint_id, task.get("branch")),
    )
    if args.json:
        common.emit_json(
            {
                "ok": True,
                "action": "start",
                "task_id": task_id,
                "state_file": path,
                "checkpoint": checkpoint_id,
                "branch": task.get("branch"),
                "base_branch": task.get("base_branch"),
                "head": task.git_snapshot.get("head"),
                "changed_count": util.as_int(task.checkpoint.get("changed_count")),
                "notes": notes,
            }
        )
        return common.EXIT_OK
    if not args.quiet:
        common.print_header(context, "checkpoint start")
    report.echo("")
    report.echo("TASK CREATED  %s" % task_id)
    _human_summary(context, task, "")
    report.echo("")
    report.echo("state  %s" % report.relpath(path, context.root))
    for note in notes:
        report.echo("  ! %s" % note)
    report.echo("")
    report.echo("Next: implement, then `checkpoint.py save --done ... --next ...`.")
    return common.EXIT_OK


def _save(args) -> int:
    context = common.build_context(args)
    task, notes = tasklib.resolve_task(context, args.id)
    if task is None:
        raise common.CliError("; ".join(notes) if notes else "no task to checkpoint")

    lock_state = locks.analyze(context, task.task_id)
    if lock_state["state"] == "live_foreign":
        allowed, reason = locks.can_take(context, lock_state, args.force)
        if not allowed:
            raise common.CliError(reason, common.EXIT_BLOCKED)
        notes.append(reason)
        history.append_event(
            context,
            task.task_id,
            "lock_forced",
            data={
                "from_agent": lock_state.get("agent"),
                "from_machine": lock_state.get("machine"),
                "reason": reason,
                "arguments": sys.argv[1:],
            },
        )
    elif lock_state["state"] == "corrupt" and not args.force:
        raise common.CliError(
            "lock file %s is corrupt; inspect it and pass --force to replace it"
            % report.relpath(locks.lock_path(context, task.task_id), context.root),
            common.EXIT_REFUSED,
        )

    if args.done:
        task.append("completed", args.done)
    if args.todo:
        task.append("remaining", args.todo)
    for prefix in args.drop_todo or []:
        remaining, hits = task.drop_matching("remaining", prefix)
        if hits != 1:
            raise common.CliError(
                "--drop-todo %r matched %d remaining item(s); nothing was written. "
                "Use enough of the text to match exactly one item." % (prefix, hits),
                common.EXIT_REFUSED,
            )
        notes.append("dropped remaining item matching %r" % prefix)
    if args.next is not None:
        task.set("next_action", args.next.strip())
    if args.warning:
        task.append("warnings", args.warning)
    pairs = _parse_pairs(args.verification)
    if pairs:
        verification = dict(util.as_dict(task.get("verification")))
        verification.update(pairs)
        task.set("verification", verification)
    new_checks = _parse_checks(args.check, True) + _parse_checks(args.optional_check, False)
    if new_checks:
        existing = [entry for entry in util.as_list_entries(task.get("verification_commands"))]
        names = {str(entry.get("name")) for entry in existing if isinstance(entry, dict)}
        for entry in new_checks:
            if entry["name"] in names:
                existing = [
                    item
                    for item in existing
                    if not (isinstance(item, dict) and item.get("name") == entry["name"])
                ]
            existing.append(entry)
            names.add(entry["name"])
        task.set("verification_commands", existing)
    if args.note is not None:
        task.set("notes", args.note)
    if args.kind == "recovery" and not args.note:
        notes.append(
            "recovery checkpoint without --note: the investigation result is not recorded"
        )

    previous_owner = str(task.get("owner") or "")
    task.set("owner", context.agent)
    if previous_owner and previous_owner != context.agent:
        history.append_event(
            context,
            task.task_id,
            "ownership_transferred",
            data={"from": previous_owner, "to": context.agent, "reason": "checkpoint save"},
            agent=context.agent,
        )
        notes.append("ownership transferred from %s to %s" % (previous_owner, context.agent))

    checkpoint_id = _finish_checkpoint(context, task, args.kind, args.note or "", notes)
    task.save()
    if not args.no_lock:
        _write_lock(context, task, notes, "lock heartbeat refreshed for %s" % context.agent, force=args.force)
    history.append_event(
        context,
        task.task_id,
        "checkpoint",
        data={
            "checkpoint": checkpoint_id,
            "kind": args.kind,
            "branch": task.git_snapshot.get("branch"),
            "head": task.git_snapshot.get("head"),
            "dirty": task.checkpoint.get("dirty"),
            "changed_files": task.get_list("changed_files"),
            "file_hashes": task.get("file_hashes"),
            "hashes_capped": task.get("hashes_capped"),
            "completed": task.get_list("completed"),
            "remaining": task.get_list("remaining"),
            "next_action": task.get("next_action"),
            "warnings": task.get_list("warnings"),
            "verification": task.get("verification"),
            "note": args.note or "",
        },
    )
    common.audit(
        context,
        COMMAND,
        sys.argv[1:],
        "saved",
        task.task_id,
        detail="%s kind=%s dirty=%s" % (checkpoint_id, args.kind, task.checkpoint.get("dirty")),
    )

    if args.json:
        common.emit_json(
            {
                "ok": True,
                "action": "save",
                "task_id": task.task_id,
                "state_file": task.path,
                "checkpoint": checkpoint_id,
                "kind": args.kind,
                "branch": task.get("branch"),
                "head": task.git_snapshot.get("head"),
                "dirty": util.as_bool(task.checkpoint.get("dirty"), False),
                "changed_files": task.get_list("changed_files"),
                "completed": task.get_list("completed"),
                "remaining": task.get_list("remaining"),
                "next_action": task.get("next_action"),
                "lock": lock_state["state"],
                "notes": notes,
            }
        )
        return common.EXIT_OK
    if not args.quiet:
        common.print_header(context, "checkpoint save")
    report.echo("")
    report.echo("CHECKPOINT SAVED  %s" % task.task_id)
    _human_summary(context, task, "")
    for note in notes:
        report.echo("  ! %s" % note)
    return common.EXIT_OK


def _release(args) -> int:
    context = common.build_context(args)
    task, notes = tasklib.resolve_task(context, args.id)
    if task is None:
        raise common.CliError("; ".join(notes) if notes else "no task to release")
    lock_state = locks.analyze(context, task.task_id)
    removed = False
    if lock_state["state"] == "none":
        notes.append("no lock file for %s" % task.task_id)
    elif lock_state["state"] == "live_same" or args.force:
        removed = locks.remove(context, task.task_id, agent=context.agent)
        notes.append("lock released" if removed else "lock could not be removed")
    else:
        raise common.CliError(
            "lock belongs to %s@%s; refusing to release somebody else's lock without --force"
            % (lock_state.get("agent"), lock_state.get("machine")),
            common.EXIT_REFUSED,
        )
    history.append_event(
        context,
        task.task_id,
        "lock_released",
        data={"reason": args.reason, "force": bool(args.force), "removed": removed},
    )
    common.audit(context, COMMAND, sys.argv[1:], "released", task.task_id, detail=args.reason)
    if args.json:
        common.emit_json({"ok": True, "action": "release", "task_id": task.task_id, "removed": removed, "notes": notes})
        return common.EXIT_OK
    report.echo("LOCK RELEASED  %s  (%s)" % (task.task_id, args.reason))
    for note in notes:
        report.echo("  ! %s" % note)
    return common.EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="checkpoint.py",
        description="Create a task or write a checkpoint into .agent-state.",
    )
    parser.add_argument("action", choices=["start", "save", "release"], help="what to do")
    common.add_common_arguments(parser)
    parser.add_argument("--id", help="task id, e.g. combat-0142")
    parser.add_argument("--title", help="short task title (start)")
    parser.add_argument("--goal", help="task goal text (start)")
    parser.add_argument("--goal-file", help="read the goal from a file (start)")
    parser.add_argument("--branch", default="auto", help="auto | NAME | none (start, default auto)")
    parser.add_argument("--base-branch", help="branch to start from (start)")
    parser.add_argument("--parent", help="parent task id (start)")
    parser.add_argument("--dep", action="append", default=[], help="dependency task id (start, repeatable)")
    parser.add_argument("--scope", action="append", default=[], help="advisory write scope path (start, repeatable)")
    parser.add_argument("--allow-protected-branch", action="store_true", help="allow starting from main/master")
    parser.add_argument("--no-lock", action="store_true", help="do not create/refresh a lock file")
    parser.add_argument("--kind", default=None, help="checkpoint kind: baseline/progress/recovery/handoff")
    parser.add_argument("--done", action="append", default=[], help="append a completed item (save)")
    parser.add_argument("--todo", action="append", default=[], help="append a remaining item (save)")
    parser.add_argument("--drop-todo", action="append", default=[], help="remove one remaining item by unique prefix (save)")
    parser.add_argument("--next", default=None, help="set next_action (start/save)")
    parser.add_argument("--remaining", action="append", default=[], help="initial remaining item (start)")
    parser.add_argument("--warning", action="append", default=[], help="append a warning")
    parser.add_argument("--note", default=None, help="checkpoint note / recovery summary")
    parser.add_argument("--verification", action="append", default=[], help="NAME=VALUE verification fact")
    parser.add_argument("--check", action="append", default=[], help="NAME=COMMAND required verification command")
    parser.add_argument("--optional-check", action="append", default=[], help="NAME=COMMAND optional verification command")
    parser.add_argument("--force", action="store_true", help="take a live lock / release a foreign lock")
    parser.add_argument("--reason", default="released by owner", help="reason for release")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.action == "start":
        if args.kind is None:
            args.kind = "baseline"
        return _start(args)
    if args.action == "save":
        if args.kind is None:
            args.kind = "progress"
        return _save(args)
    return _release(args)


if __name__ == "__main__":
    sys.exit(common.run(main, COMMAND))
