#!/usr/bin/env python3
"""Work out what really needs doing next, and prove it from git + the work tree.

    python resume.py                    # pick the most likely unfinished task
    python resume.py --hint "战斗 连击"   # narrow by words
    python resume.py --id combat-0142   # exact task
    python resume.py --take             # also take ownership (lock + owner)
    python resume.py --checkout         # also switch to the task branch (clean tree only)

The command is read-only unless ``--take`` or ``--checkout`` is given.  It never
runs ``git reset``, never deletes anything, and never "fixes" a dirty work tree.

Exit codes: 0 = usable evidence (CLEAN / CHECKPOINT_STALE / RECOVERY),
2 = no task / corrupt state / diverged history, 3 = another agent may be working.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from handofflib import common, history, locks, report, tasks as tasklib, util  # noqa: E402

COMMAND = "resume"

STOPWORDS = {
    "继续", "接着", "任务", "的", "了", "帮我", "我们", "你", "我", "做", "做没",
    "没做完", "没完成", "未完成", "上一个", "上次", "恢复", "一下", "把", "这个",
    "那个", "工作", "开发", "代码", "项目", "现在", "直接", "然后", "开始", "问题",
    "the", "a", "an", "task", "resume", "continue", "finish", "unfinished", "work",
    "code", "project", "and", "to", "of", "for", "in", "on", "with", "please",
}

PATH_RE = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_.\-]*[/\\][A-Za-z0-9_.\-/\\]*[A-Za-z0-9_]"
    r"|(?<![\w.-])[A-Za-z0-9_][A-Za-z0-9_.\-]*\.(?:cpp|cc|h|hpp|cs|py|js|ts|md|ini|json|yaml|yml|toml|txt|uproject|uasset|umap|ps1|sh|bat|cfg)"
)


# ---------------------------------------------------------------------------
# task selection
# ---------------------------------------------------------------------------
def _tokenize(hint: str) -> list:
    raw = re.split(r"[\s,，。、;；:：!！?？()（）\[\]「」《》\"'`]+", hint or "")
    tokens = []
    for token in raw:
        clean = token.strip().lower()
        if len(clean) < 2 or clean in STOPWORDS:
            continue
        tokens.append(clean)
    return tokens


def _searchable(task) -> str:
    parts = [
        task.task_id,
        task.title,
        str(task.get("goal") or ""),
        str(task.get("next_action") or ""),
        str(task.get("owner") or ""),
        str(task.get("branch") or ""),
    ]
    parts.extend(task.get_list("completed"))
    parts.extend(task.get_list("remaining"))
    parts.extend(task.get_list("write_scope"))
    return "\n".join(parts).lower()


def score_task(task, tokens) -> int:
    if not tokens:
        return 0
    haystack = _searchable(task)
    score = 0
    for token in tokens:
        if token == task.task_id.lower():
            score += 100
        elif token in haystack:
            score += min(len(token), 12)
    return score


def select_task(context, args):
    active, problems = tasklib.load_active_tasks(context)
    alternatives = []
    notes = []
    for problem in problems:
        notes.append("%s: %s" % (problem.get("path"), problem.get("message")))
    usable = [task for task in active if task.status in tasklib.ACTIVE_STATUSES]
    if args.id:
        match = next((task for task in active if task.task_id == args.id.strip()), None)
        if match is None:
            path = tasklib.find_task_file(context, args.id.strip())
            hint = ""
            if path:
                hint = " (it exists at %s but is not an active task)" % report.relpath(path, context.root)
            raise common.CliError(
                "no active task with id %r%s" % (args.id, hint), common.EXIT_ERROR
            )
        chosen = match
        reason = "explicit --id"
    else:
        if not usable:
            raise common.CliError(
                "no active task under %s" % report.relpath(context.active_dir(), context.root),
                common.EXIT_ERROR,
            )
        tokens = _tokenize(args.hint or "")
        scored = sorted(
            (
                (score_task(task, tokens), util.parse_iso(task.get("updated_at")) or util.parse_iso("1970-01-01T00:00:00Z"), task)
                for task in usable
            ),
            key=lambda item: (item[0], item[1]),
            reverse=True,
        )
        best_score = scored[0][0]
        if tokens and best_score == 0:
            notes.append(
                "hint %r matched no active task; falling back to the most recently updated task"
                % (args.hint,)
            )
            scored = sorted(
                scored,
                key=lambda item: (item[2].get("updated_at") or ""),
                reverse=True,
            )
            chosen, best_score = scored[0][2], 0
            reason = "most recently updated (hint matched nothing)"
        elif tokens:
            chosen = scored[0][2]
            reason = "best match for hint (%d tokens, score %d)" % (len(tokens), best_score)
        else:
            scored = sorted(scored, key=lambda item: (item[2].get("updated_at") or ""), reverse=True)
            chosen = scored[0][2]
            reason = "most recently updated active task"
        for item in scored:
            if item[2].task_id != chosen.task_id:
                alternatives.append(item[2])
    return chosen, reason, alternatives, notes


def alternatives_summary(context, tasks, limit: int = 5) -> list:
    rows = []
    for task in tasks[:limit]:
        lock_state = locks.analyze(context, task.task_id)
        rows.append(
            {
                "task_id": task.task_id,
                "title": task.title,
                "status": task.status,
                "owner": task.get("owner"),
                "checkpoint": task.checkpoint_id(),
                "updated_at": task.get("updated_at"),
                "age": util.humanize_seconds(util.age_seconds(task.get("updated_at"))),
                "lock": lock_state["state"],
            }
        )
    return rows


# ---------------------------------------------------------------------------
# investigation checklist
# ---------------------------------------------------------------------------
def extract_paths(texts) -> list:
    found = []
    for text in texts:
        for match in PATH_RE.findall(str(text or "")):
            cleaned = util.to_posix(match.strip())
            if cleaned and cleaned not in found:
                found.append(cleaned)
    return found


def build_investigation(context, task, evidence) -> list:
    steps = []
    new_paths = evidence.get("new_since_checkpoint") or []
    drifted = evidence.get("hash_drift") or []
    vanished = evidence.get("vanished_since_checkpoint") or []
    if new_paths or drifted or vanished:
        steps.append(
            "Inspect the uncheckpointed changes first: %s"
            % ", ".join((new_paths + drifted + vanished)[:6])
        )
        steps.append(
            "Run `git --no-pager diff -- %s` and read the result; do not stage, revert or reformat it yet"
            % " ".join((new_paths + drifted)[:4] if (new_paths + drifted) else ".")
        )
    completed = task.get_list("completed")
    if completed:
        steps.append(
            "The checkpoint claims %d completed item(s). Confirm each one in the actual code "
            "before building on it (checkpoint text is an index, not evidence)." % len(completed)
        )
        paths = extract_paths(completed)
        if paths:
            steps.append("Read these claimed-done files: %s" % ", ".join(paths[:5]))
    remaining = task.get_list("remaining")
    if remaining:
        paths = extract_paths(remaining)
        if paths:
            steps.append(
                "Check whether the remaining work already exists in code: %s"
                % ", ".join(paths[:5])
            )
        steps.append(
            "Re-read the remaining list against the code and rewrite it if it is out of date."
        )
    if evidence.get("dirty"):
        binary = evidence.get("binary_changed") or []
        if binary:
            steps.append(
                "Binary assets changed (%d). Never merge or regenerate them blindly; confirm who "
                "produced them and keep one owner per asset." % len(binary)
            )
    if task.get("verification_commands"):
        steps.append("Run the declared verification commands before claiming progress.")
    else:
        steps.append(
            "This task declares no verification commands; find the project's canonical check "
            "(see AGENTS.md / docs) and record it with `checkpoint.py save --check NAME=COMMAND`."
        )
    if evidence.get("mode") == "RECOVERY":
        steps.append(
            "After the investigation, write a recovery checkpoint: "
            "`checkpoint.py save --kind recovery --note \"what the uncheckpointed work does\"`."
        )
    return steps


def command_plan(context, task, mode, taken: bool) -> list:
    prefix = "python .agents/skills/agent-handoff/scripts"
    steps = []
    if not taken:
        steps.append(
            "%s/resume.py --id %s --take --agent %s"
            % (prefix, task.task_id, context.agent)
        )
    branch = str(task.get("branch") or "")
    if branch and mode != "BLOCKED":
        steps.append("git switch %s   # only if the work tree is clean" % branch)
    steps.append("%s/inspect.py --id %s --files" % (prefix, task.task_id))
    steps.append("...do the work and verify it...")
    if mode == "RECOVERY":
        steps.append(
            '%s/checkpoint.py save --id %s --kind recovery --note "what was recovered"'
            % (prefix, task.task_id)
        )
    steps.append(
        '%s/checkpoint.py save --id %s --done "..." --next "..."' % (prefix, task.task_id)
    )
    steps.append('%s/finish.py --id %s --commit "..."' % (prefix, task.task_id))
    return steps


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def render(context, task, reason, evidence, alternatives, notes, args, taken: bool = False) -> None:
    mode = evidence["mode"]
    checkpoint = task.checkpoint
    report.title("RESUME  %s  |  %s" % (task.task_id, tasklib.MODE_TEXT.get(mode, mode)))
    for note in notes:
        report.echo("  ! %s" % note)
    for warning in context.warnings:
        report.echo("  ! %s" % warning)

    report.section("TASK")
    report.kv("title", task.title, 14)
    goal_lines = [line for line in str(task.get("goal") or "").splitlines() if line.strip()]
    if goal_lines:
        report.kv("goal", goal_lines[0], 14)
        for line in goal_lines[1:4]:
            report.echo(" " * 14 + line)
    report.kv("owner", "%s  (status %s)" % (task.get("owner"), task.status), 14)
    report.kv(
        "branch",
        "%s   base=%s" % (task.get("branch") or "(none)", task.get("base_branch") or "(none)"),
        14,
    )
    report.kv(
        "checkpoint",
        "%s  %s  by %s  kind=%s  dirty=%s  changed=%s"
        % (
            checkpoint.get("id") or "(none)",
            checkpoint.get("at") or "?",
            checkpoint.get("by") or "?",
            checkpoint.get("kind") or "?",
            checkpoint.get("dirty"),
            checkpoint.get("changed_count"),
        ),
        14,
    )
    report.kv(
        "git now",
        "%s @ %s  (%d dirty path(s))"
        % (
            evidence.get("branch") or "(detached/none)",
            evidence.get("head_short") or "(no commit)",
            evidence.get("changed_count") or 0,
        ),
        14,
    )
    report.kv(
        "claimed next",
        util.truncate(str(task.get("next_action") or "(none)"), 100),
        14,
    )
    report.kv("selected by", reason, 14)

    report.section("MODE  %s" % tasklib.MODE_TEXT.get(mode, mode))
    for reason_text in evidence.get("reasons") or []:
        report.echo("  - %s" % reason_text)
    report.echo("")
    for check in evidence.get("checks") or []:
        report.check_line(check)

    if not args.brief:
        dirty = evidence.get("changed_files") or []
        if dirty:
            report.section("WORK TREE (%d path(s) differ from HEAD)" % len(dirty))
            drift = set(evidence.get("hash_drift") or [])
            new = set(evidence.get("new_since_checkpoint") or [])
            for path in dirty[:40]:
                tags = []
                if path in new:
                    tags.append("new since checkpoint")
                if path in drift:
                    tags.append("edited since checkpoint")
                if context.is_binary_path(path):
                    tags.append("binary")
                report.echo("  %s%s" % (path, ("   <- " + ", ".join(tags)) if tags else ""))
            if len(dirty) > 40:
                report.echo("  ... %d more" % (len(dirty) - 40))

        completed = task.get_list("completed")
        remaining = task.get_list("remaining")
        report.section("CHECKPOINT CLAIMS (unverified text)")
        if completed:
            report.echo("  completed:")
            for item in completed[:10]:
                report.echo("    - %s" % util.truncate(item, 100))
            if len(completed) > 10:
                report.echo("    ... %d more" % (len(completed) - 10))
        if remaining:
            report.echo("  remaining:")
            for item in remaining[:10]:
                report.echo("    - %s" % util.truncate(item, 100))
            if len(remaining) > 10:
                report.echo("    ... %d more" % (len(remaining) - 10))
        if not completed and not remaining:
            report.echo("  (empty - the previous agent recorded no progress detail)")
        warnings = task.get_list("warnings")
        if warnings:
            report.echo("  warnings:")
            for item in warnings[:6]:
                report.echo("    ! %s" % util.truncate(item, 100))

    lock = evidence.get("lock") or {}
    report.section("LOCK")
    report.kv(
        "state",
        "%s  %s"
        % (
            lock.get("state"),
            "%s@%s  heartbeat %s ago  pid %s"
            % (lock.get("agent") or "-", lock.get("machine") or "-", lock.get("age_human"), lock.get("pid") or "-"),
        ),
        12,
    )
    for reason_text in lock.get("reasons") or []:
        report.echo("  - %s" % reason_text)

    investigation = build_investigation(context, task, evidence)
    report.section("REQUIRED INVESTIGATION - do not trust the checkpoint text alone")
    report.numbered(investigation)

    report.section("SAFETY RULES FOR THIS RESUME")
    report.bullet(
        [
            "real code > git > checkpoint description; when they disagree, report the difference",
            "no `git reset --hard`, no `git clean`, no force push, no deleting unknown edits",
            "do not modify main/master directly; commit on %s"
            % (task.get("branch") or "a feature branch"),
            "if the previous agent's intent is genuinely unclear: stop before overwriting, "
            "finish all safe investigation, then ask one minimal question",
        ]
    )

    if alternatives:
        report.section("OTHER ACTIVE TASKS")
        report.table(
            ["task_id", "status", "owner", "checkpoint", "updated", "lock"],
            [
                [
                    item["task_id"],
                    item["status"],
                    str(item["owner"]),
                    item["checkpoint"],
                    item["age"],
                    item["lock"],
                ]
                for item in alternatives
            ],
        )

    report.section("COMMAND PLAN")
    for line in command_plan(context, task, mode, taken=taken):
        report.echo("  %s" % line)


def list_all(context, args) -> int:
    active, problems = tasklib.load_active_tasks(context)
    rows = []
    payload = []
    for task in sorted(active, key=lambda item: str(item.get("updated_at")), reverse=True):
        lock_state = locks.analyze(context, task.task_id)
        rows.append(
            [
                task.task_id,
                util.truncate(task.title, 32),
                task.status,
                str(task.get("owner") or "-"),
                task.checkpoint_id() or "-",
                util.humanize_seconds(util.age_seconds(task.get("updated_at"))),
                lock_state["state"],
            ]
        )
        payload.append(
            {
                "task_id": task.task_id,
                "title": task.title,
                "status": task.status,
                "owner": task.get("owner"),
                "checkpoint": task.checkpoint_id(),
                "updated_at": task.get("updated_at"),
                "lock": lock_state,
                "remaining": task.get_list("remaining"),
            }
        )
    if args.json:
        common.emit_json({"ok": True, "project": context.root, "tasks": payload, "problems": problems})
        return common.EXIT_OK
    report.title("ACTIVE TASKS  (%d)" % len(active))
    if rows:
        report.table(["task_id", "title", "status", "owner", "ckpt", "updated", "lock"], rows)
    else:
        report.echo("no active task")
    for problem in problems:
        report.echo("  ! %s: %s" % (report.relpath(problem.get("path"), context.root), problem.get("message")))
    return common.EXIT_OK


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def _take(context, task, evidence, args) -> list:
    notes = []
    lock_state = evidence.get("lock") or {}
    allowed, reason = locks.can_take(context, lock_state, args.force)
    if not allowed:
        raise common.CliError(reason, common.EXIT_BLOCKED)
    notes.append(reason)
    branch = str(task.get("branch") or "")
    if args.checkout:
        if evidence.get("dirty"):
            raise common.CliError(
                "refusing to switch branches with %d dirty path(s); inspect or checkpoint the "
                "work tree first (nothing was changed)" % len(evidence.get("changed_files") or []),
                common.EXIT_REFUSED,
            )
        if not branch:
            notes.append("task records no branch; skipped --checkout")
        elif not context.git.branch_exists(branch):
            raise common.CliError(
                "task branch %r does not exist here; nothing was changed" % branch,
                common.EXIT_REFUSED,
            )
        elif evidence.get("branch") != branch:
            result = context.git.checkout(branch)
            if not result.ok:
                raise common.CliError(
                    "could not switch to %r: %s" % (branch, (result.err or result.out).strip()),
                    common.EXIT_ERROR,
                )
            notes.append("switched to branch %r" % branch)
        else:
            notes.append("already on branch %r" % branch)

    previous_owner = str(task.get("owner") or "")
    task.set("owner", context.agent)
    task.touch()
    task.save()
    lock_path = locks.write(context, task.raw, context.agent, branch=branch)
    history.append_event(
        context,
        task.task_id,
        "taken_over",
        data={
            "from": previous_owner,
            "to": context.agent,
            "machine": context.machine,
            "mode": evidence.get("mode"),
            "forced": bool(args.force),
            "lock_state": lock_state.get("state"),
            "reason": reason,
            "head": evidence.get("head"),
            "changed_count": evidence.get("changed_count"),
        },
    )
    notes.append("lock written: %s" % report.relpath(lock_path, context.root))
    return notes


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="resume.py",
        description="Report what a task really needs next, comparing the checkpoint with git and the work tree.",
    )
    common.add_common_arguments(parser)
    parser.add_argument("--id", help="exact task id")
    parser.add_argument("--hint", help="words describing the task, e.g. 'combat combo'")
    parser.add_argument("--list", action="store_true", help="list active tasks and exit")
    parser.add_argument("--take", action="store_true", help="take ownership (owner field + lock + journal)")
    parser.add_argument("--checkout", action="store_true", help="also switch to the task branch (clean tree only)")
    parser.add_argument("--force", action="store_true", help="take a live foreign lock; recorded in history")
    parser.add_argument("--brief", action="store_true", help="hide work tree and claim details")
    args = parser.parse_args()
    context = common.build_context(args)
    if args.list:
        return list_all(context, args)

    task, reason, alternatives, notes = select_task(context, args)
    lock_state = locks.analyze(context, task.task_id)
    evidence = tasklib.collect_evidence(context, task, lock_state)
    mode = evidence["mode"]
    exit_code = tasklib.MODE_EXIT.get(mode, common.EXIT_ERROR)

    take_notes = []
    if args.take:
        if mode == "CORRUPT":
            raise common.CliError(
                "refusing to take ownership: the task state is invalid (%s)"
                % "; ".join(evidence.get("problems") or []),
                common.EXIT_ERROR,
            )
        if mode == "BLOCKED" and not args.force:
            raise common.CliError(
                "; ".join(evidence.get("reasons") or []), common.EXIT_BLOCKED
            )
        take_notes = _take(context, task, evidence, args)
        if args.checkout:
            evidence = tasklib.collect_evidence(context, task, locks.analyze(context, task.task_id))
        notes.extend(take_notes)

    # Read-only resumes do not write anything, including the audit log, so a
    # clean work tree stays clean.
    if args.take:
        common.audit(
            context,
            COMMAND,
            sys.argv[1:],
            "taken",
            task.task_id,
            detail="mode=%s forced=%s" % (mode, bool(args.force)),
        )

    if args.json:
        common.emit_json(
            {
                "ok": mode not in ("CORRUPT", "DIVERGED", "BLOCKED"),
                "project": context.root,
                "agent": context.agent,
                "selection": {"task_id": task.task_id, "reason": reason},
                "mode": mode,
                "mode_text": tasklib.MODE_TEXT.get(mode, mode),
                "reasons": evidence.get("reasons"),
                "exit_code": exit_code,
                "task": {
                    "task_id": task.task_id,
                    "title": task.title,
                    "goal": task.get("goal"),
                    "owner": task.get("owner"),
                    "status": task.status,
                    "branch": task.get("branch"),
                    "base_branch": task.get("base_branch"),
                    "checkpoint": task.checkpoint,
                    "completed": task.get_list("completed"),
                    "remaining": task.get_list("remaining"),
                    "next_action": task.get("next_action"),
                    "warnings": task.get_list("warnings"),
                    "write_scope": task.get_list("write_scope"),
                    "verification_commands": util.as_list_entries(task.get("verification_commands")),
                    "verification": task.get("verification"),
                },
                "evidence": {
                    "branch": evidence.get("branch"),
                    "head": evidence.get("head"),
                    "dirty": evidence.get("dirty"),
                    "changed_files": evidence.get("changed_files"),
                    "new_since_checkpoint": evidence.get("new_since_checkpoint"),
                    "hash_drift": evidence.get("hash_drift"),
                    "vanished_since_checkpoint": evidence.get("vanished_since_checkpoint"),
                    "binary_changed": evidence.get("binary_changed"),
                    "tree_diff": evidence.get("tree_diff"),
                    "head_drift": evidence.get("head_drift"),
                    "checks": evidence.get("checks"),
                },
                "lock": lock_state,
                "taken": bool(args.take),
                "take_notes": take_notes,
                "alternatives": alternatives_summary(context, alternatives),
                "investigation": build_investigation(context, task, evidence),
                "next_commands": command_plan(context, task, mode, taken=bool(args.take)),
                "notes": notes,
            }
        )
        return exit_code

    if not args.quiet:
        common.print_header(context, "resume")
    render(context, task, reason, evidence, alternatives, notes, args, taken=bool(args.take))
    report.exit_note(exit_code, common.EXIT_MEANINGS.get(exit_code, "see modes above"))
    return exit_code


if __name__ == "__main__":
    sys.exit(common.run(main, COMMAND))
