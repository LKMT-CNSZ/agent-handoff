#!/usr/bin/env python3
"""Check the whole collaboration environment and print a human readable report.

    python doctor.py            # inspect only
    python doctor.py --fix      # also create missing state directories/config
    python doctor.py --json

Exit codes: 0 HEALTHY, 1 DEGRADED (warnings), 2 UNHEALTHY (needs a human).
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from handofflib import (  # noqa: E402
    __version__,
    common,
    config,
    history,
    locks,
    report,
    tasks as tasklib,
    util,
)

COMMAND = "doctor"

REQUIRED_SKILL_FILES = [
    "SKILL.md",
    "VERSION",
    "references/protocol.md",
    "references/task-schema.md",
    "references/recovery.md",
    "scripts/checkpoint.py",
    "scripts/resume.py",
    "scripts/finish.py",
    "scripts/inspect.py",
    "scripts/doctor.py",
    "scripts/run_tests.py",
    "scripts/simulate_handoff.py",
    "scripts/handofflib/__init__.py",
    "scripts/handofflib/tasks.py",
    "scripts/handofflib/gitinfo.py",
    "scripts/handofflib/locks.py",
    "scripts/handofflib/yamlio.py",
    "scripts/handofflib/scenario.py",
    "examples/combat-0142.example.yaml",
]

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _finding(findings, name, status, detail, group="general"):
    findings.append({"name": name, "status": status, "detail": detail, "group": group})
    return findings[-1]


def check_environment(findings):
    version = sys.version_info
    if version >= (3, 8):
        _finding(findings, "Python", "ok", "%d.%d.%d (standard library only)" % version[:3], "environment")
    else:
        _finding(findings, "Python", "fail", "%d.%d.%d is too old; 3.8+ required" % version[:3], "environment")
    _finding(findings, "Machine", "ok", util.hostname(), "environment")


def check_git(context, findings):
    git = context.git
    if git is None:
        _finding(findings, "Git", "fail", "git is not usable in this project", "git")
        return {}
    facts = {"branch": git.branch(), "head": git.head(), "head_short": git.head_short()}
    version = git.version()
    _finding(
        findings,
        "Git",
        "ok",
        "%s  (top level %s)" % (version or "git", git.toplevel() or context.root),
        "git",
    )
    if facts["head"]:
        _finding(
            findings,
            "HEAD",
            "ok",
            "%s on %s  (%d commit(s))"
            % (facts["head_short"], facts["branch"] or "(detached)", git.commit_count()),
            "git",
        )
    else:
        _finding(
            findings,
            "HEAD",
            "warn",
            "repository has no commit yet; branches and checkpoint commits cannot exist. "
            "Make a baseline commit before relying on branch isolation.",
            "git",
        )
    entries = git.status_entries()
    facts["dirty"] = bool(entries)
    facts["dirty_count"] = len(entries)
    binary = [entry["path"] for entry in entries if context.is_binary_path(entry["path"])]
    if entries:
        _finding(
            findings,
            "Work tree",
            "warn" if len(entries) > 200 else "ok",
            "dirty: %d path(s)%s"
            % (len(entries), (", %d binary" % len(binary)) if binary else ""),
            "git",
        )
    else:
        _finding(findings, "Work tree", "ok", "clean", "git")
    facts["binary_dirty"] = binary
    return facts


def check_state_dirs(context, findings, fix: bool):
    state_dir = context.state_dir
    created = []
    if fix:
        created = common.scaffold_state(context.root)
    if not os.path.isdir(state_dir):
        _finding(findings, "State dir", "fail", "%s is missing (run --fix)" % state_dir, "state")
        return
    missing = [
        name for name in config.SUB_DIRS if not os.path.isdir(os.path.join(state_dir, name))
    ]
    if missing:
        _finding(
            findings,
            "State dir",
            "fail" if not fix else "warn",
            "missing subdirectories: %s%s"
            % (", ".join(missing), "" if fix else " (run --fix)"),
            "state",
        )
    else:
        _finding(findings, "State dir", "ok", "active/completed/failed/history/locks present", "state")
    if created:
        _finding(findings, "Scaffold", "ok", "created %d path(s)" % len(created), "state")
    try:
        handle, path = tempfile.mkstemp(prefix=".doctor-", dir=state_dir)
        os.close(handle)
        os.remove(path)
        _finding(findings, "Writable", "ok", "state directory accepts writes", "state")
    except OSError as exc:
        _finding(findings, "Writable", "fail", "state directory is not writable: %s" % exc, "state")
    for warning in context.warnings:
        _finding(findings, "Config", "warn", warning, "state")
    if not context.warnings:
        _finding(findings, "Config", "ok", "%s parsed" % config.CONFIG_NAME, "state")


def check_skill(context, findings):
    in_repo = context.skill_dir_in_repo()
    candidates = []
    if os.path.isdir(in_repo):
        candidates.append((in_repo, "in-repo"))
    if os.path.isdir(SKILL_DIR) and os.path.realpath(SKILL_DIR) != os.path.realpath(in_repo):
        candidates.append((SKILL_DIR, "running copy"))
    if not candidates:
        _finding(findings, "Skill", "fail", "agent-handoff skill directory not found", "skill")
        return
    for directory, label in candidates:
        missing = [
            rel
            for rel in REQUIRED_SKILL_FILES
            if not os.path.isfile(os.path.join(directory, rel.replace("/", os.sep)))
        ]
        if missing:
            _finding(
                findings,
                "Skill (%s)" % label,
                "fail" if label == "in-repo" else "warn",
                "missing: %s" % ", ".join(missing[:6]),
                "skill",
            )
        else:
            version = (util.read_text(os.path.join(directory, "VERSION")) or "?").strip()
            _finding(
                findings,
                "Skill (%s)" % label,
                "ok",
                "complete at %s  (version %s)"
                % (report.relpath(directory, context.root), version),
                "skill",
            )
    if not os.path.isdir(in_repo):
        _finding(
            findings,
            "Skill vendoring",
            "warn",
            "%s is missing: other agents cloning this repository will not find the skill"
            % config.SKILL_REL_PATH,
            "skill",
        )
    agents_md = os.path.join(context.root, "AGENTS.md")
    text = util.read_text(agents_md)
    if text is None:
        _finding(findings, "AGENTS.md", "warn", "missing; long lived rules are not discoverable", "skill")
    elif "agent-handoff" not in text and "Agent Handoff" not in text:
        _finding(
            findings,
            "AGENTS.md",
            "warn",
            "does not mention agent-handoff; agents may never load the skill",
            "skill",
        )
    else:
        _finding(findings, "AGENTS.md", "ok", "mentions agent-handoff", "skill")


def check_tasks(context, findings):
    active, problems = tasklib.load_active_tasks(context)
    completed, completed_problems = tasklib.load_tasks_in(context, "completed")
    failed, failed_problems = tasklib.load_tasks_in(context, "failed")
    all_problems = problems + completed_problems + failed_problems

    for problem in all_problems:
        _finding(
            findings,
            "Task file",
            "fail",
            "%s: %s" % (report.relpath(problem.get("path"), context.root), problem.get("message")),
            "tasks",
        )
    ids = {}
    for status, group in (("active", active), ("completed", completed), ("failed", failed)):
        for task in group:
            ids.setdefault(task.task_id, set()).add(status)
    for task_id, statuses in ids.items():
        if len(statuses) > 1:
            _finding(
                findings,
                "Duplicate task",
                "fail",
                "%s appears in %s" % (task_id, " and ".join(sorted(statuses))),
                "tasks",
            )
    branches = {}
    for task in active:
        if task.get("branch"):
            branches.setdefault(str(task.get("branch")), []).append(task.task_id)
    for branch, owners in branches.items():
        if len(owners) > 1:
            _finding(
                findings,
                "Branch conflict",
                "warn",
                "%s is claimed by %s in parallel" % (branch, ", ".join(owners)),
                "tasks",
            )
    scopes = []
    for task in active:
        for scope in task.get_list("write_scope"):
            scopes.append((scope.rstrip("/"), task.task_id))
    for index, (scope, owner) in enumerate(scopes):
        for other_scope, other_owner in scopes[index + 1:]:
            if owner == other_owner or not scope or not other_scope:
                continue
            if (
                scope == other_scope
                or scope.startswith(other_scope + "/")
                or other_scope.startswith(scope + "/")
            ):
                _finding(
                    findings,
                    "Write scope overlap",
                    "warn",
                    "%s and %s both claim %s" % (owner, other_owner, scope),
                    "tasks",
                )

    modes = {}
    for task in active:
        lock_state = locks.analyze(context, task.task_id)
        evidence = tasklib.collect_evidence(context, task, lock_state)
        mode = evidence.get("mode")
        modes[task.task_id] = mode
        detail = "%s  owner=%s  ckpt=%s  %s" % (
            task.task_id,
            task.get("owner"),
            task.checkpoint_id() or "-",
            tasklib.MODE_TEXT.get(mode, mode),
        )
        severity = "ok"
        if mode in ("CORRUPT", "DIVERGED"):
            severity = "fail"
        elif mode in ("RECOVERY", "CHECKPOINT_STALE", "BLOCKED"):
            severity = "warn"
        _finding(findings, "Task %s" % task.task_id, severity, detail, "tasks")
        if mode == "RECOVERY":
            _finding(
                findings,
                "Recovery",
                "warn",
                "%s: %s" % (task.task_id, "; ".join(evidence.get("reasons") or [])),
                "tasks",
            )
        if evidence.get("checkpoint_commit_exists") is False:
            _finding(
                findings,
                "Checkpoint commit",
                "fail",
                "%s references %s which does not exist in this clone"
                % (task.task_id, evidence.get("checkpoint_head_short")),
                "tasks",
            )
        if evidence.get("branch_exists") is False:
            _finding(
                findings,
                "Checkpoint branch",
                "warn",
                "%s references branch %r which does not exist"
                % (task.task_id, evidence.get("checkpoint_branch")),
                "tasks",
            )
    if not active:
        _finding(findings, "Active tasks", "ok", "none (nothing to resume)", "tasks")
    else:
        _finding(
            findings,
            "Active tasks",
            "ok",
            "%d active: %s" % (len(active), ", ".join(sorted(modes))),
            "tasks",
        )

    for task_id in locks.find_orphan_locks(context):
        _finding(findings, "Orphan lock", "warn", "%s.lock has no active task" % task_id, "locks")
    stale = 0
    live = 0
    for task in active:
        state = locks.analyze(context, task.task_id)
        if state["state"] == "stale":
            stale += 1
            _finding(
                findings,
                "Stale lock",
                "warn",
                "%s: %s" % (task.task_id, "; ".join(state.get("reasons") or [])),
                "locks",
            )
        elif state["state"] in ("live_same", "live_foreign"):
            live += 1
            _finding(
                findings,
                "Live lock",
                "ok",
                "%s held by %s@%s (heartbeat %s ago)"
                % (task.task_id, state.get("agent"), state.get("machine"), state.get("age_human")),
                "locks",
            )
        elif state["state"] == "corrupt":
            _finding(findings, "Corrupt lock", "fail", "%s.lock cannot be parsed" % task.task_id, "locks")
        elif state["state"] == "none":
            _finding(
                findings,
                "Lock",
                "warn",
                "%s has no lock file; ownership of in-progress work is unknown" % task.task_id,
                "locks",
            )
    if not active:
        _finding(findings, "Locks", "ok", "no active task needs a lock", "locks")
    if not all_problems and not active:
        _finding(
            findings,
            "Archive",
            "ok",
            "%d completed, %d failed" % (len(completed), len(failed)),
            "tasks",
        )

    # state must travel with git
    if context.git and context.git.has_commit():
        rel = config.STATE_DIR_NAME
        ignored = context.git.check_ignore([rel])
        untracked = any(
            entry["path"] == rel or entry["path"].startswith(rel + "/")
            for entry in context.git.status_entries()
        )
        if ignored:
            _finding(
                findings,
                "State versioned",
                "fail",
                "%s is ignored by git; other machines could never resume" % rel,
                "state",
            )
        elif untracked:
            _finding(
                findings,
                "State versioned",
                "warn",
                "%s is not committed yet; commit it so another agent/machine can resume" % rel,
                "state",
            )
        else:
            _finding(findings, "State versioned", "ok", "%s is tracked by git" % rel, "state")
    journal = history.command_log_path(context)
    if os.path.isfile(journal):
        size = util.file_size(journal) or 0
        _finding(findings, "Audit log", "ok", "%d bytes" % size, "state")
    return {"tasks": active, "modes": modes, "stale": stale, "live": live}


def check_assets(context, findings):
    git = context.git
    if git is None:
        return
    tracked = git.ls_files()
    if not tracked:
        return
    extensions = context.binary_extensions()
    binaries = [path for path in tracked if path.rsplit(".", 1)[-1].lower() in extensions]
    if not binaries:
        _finding(findings, "Binary assets", "ok", "no tracked binary asset files", "assets")
        return
    threshold = context.large_file_bytes()
    large = []
    total = 0
    for path in binaries:
        size, exists = git.tracked_size(path)
        if not exists or size is None:
            continue
        total += size
        if size >= threshold:
            large.append((path, size))
    attributes = git.gitattributes_text() or ""
    lfs = "filter=lfs" in attributes
    detail = "%d tracked binary file(s), %.1f MB total" % (len(binaries), total / (1024.0 * 1024.0))
    if lfs:
        _finding(findings, "Binary assets", "ok", detail + "; git-lfs is configured", "assets")
    elif large:
        _finding(
            findings,
            "Binary assets",
            "warn",
            detail
            + "; %d file(s) over %.0f MB (largest: %s) and no filter=lfs in .gitattributes. "
            "Decide the LFS / Perforce strategy before committing large assets."
            % (
                len(large),
                threshold / (1024.0 * 1024.0),
                max(large, key=lambda item: item[1])[0],
            ),
            "assets",
        )
    else:
        _finding(
            findings,
            "Binary assets",
            "ok",
            detail + "; none over the large-file threshold",
            "assets",
        )
    if context.asset_strategy() not in ("none", ""):
        _finding(
            findings,
            "Asset policy",
            "ok",
            "configured strategy: %s (not enforced in V0.1)" % context.asset_strategy(),
            "assets",
        )


def render(context, findings, facts, modes, args) -> int:
    fails = [item for item in findings if item["status"] == "fail"]
    warns = [item for item in findings if item["status"] == "warn"]
    if fails:
        status_text, exit_code = "UNHEALTHY", 2
    elif warns:
        status_text, exit_code = "DEGRADED", 1
    else:
        status_text, exit_code = "HEALTHY", 0

    active_modes = list(modes.values()) if modes else []
    checkpoint_ok = all(mode in ("CLEAN", "CHECKPOINT_STALE") for mode in active_modes)
    recovery = any(mode == "RECOVERY" for mode in active_modes)
    summary = [
        ("Git", "OK" if context.git is not None else "MISSING"),
        ("Branch", facts.get("branch") or ("(no commit yet)" if context.git else "-")),
        ("Active Tasks", str(len(active_modes))),
        ("Stale Locks", str(facts.get("stale", 0))),
        ("Dirty Workspace", "YES" if facts.get("dirty") else "NO"),
        ("Checkpoint", "OK" if checkpoint_ok else ("RECOVERY" if recovery else "ATTENTION")),
        ("Recovery Required", "YES" if recovery else "NO"),
    ]

    if args.json:
        common.emit_json(
            {
                "ok": exit_code == 0,
                "system_status": status_text,
                "exit_code": exit_code,
                "project": context.root,
                "agent": context.agent,
                "skill_version": __version__,
                "summary": {name: value for name, value in summary},
                "findings": findings,
                "tasks": modes,
            }
        )
        return exit_code

    report.title("Agent Handoff Doctor")
    for name, value in summary:
        report.kv(name, value, 20)
    report.rule()
    if fails:
        report.section("FAILURES")
        for item in fails:
            report.check_line(item)
    if warns:
        report.section("WARNINGS")
        for item in warns:
            report.check_line(item)
    if not fails and not warns:
        report.section("CHECKS")
        for item in findings:
            report.check_line(item)
    report.rule()
    report.echo("System Status: %s" % status_text)
    if exit_code != 0:
        report.echo("")
        report.echo("Nothing was modified.  Fix the items above, then run doctor again.")
    report.exit_note(exit_code, common.EXIT_MEANINGS.get(exit_code, status_text))
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="doctor.py", description="Check git, state files, locks and the skill itself."
    )
    parser.add_argument("--repo", default=None, help="project root (default: search upward from CWD)")
    parser.add_argument("--agent", default=None, help="agent identity (default: env / config)")
    parser.add_argument("--json", action="store_true", help="machine readable output")
    parser.add_argument("--fix", action="store_true", help="create missing state directories and config")
    parser.add_argument("--quiet", action="store_true", help="suppress the header")
    args = parser.parse_args()

    util.configure_stdio()
    findings = []
    check_environment(findings)

    root = config.find_project_root(args.repo)
    if root is None and args.fix:
        git_root = config.find_git_root(args.repo)
        target = git_root or os.path.abspath(args.repo or os.getcwd())
        created = common.scaffold_state(target)
        findings.append(
            {
                "name": "Scaffold",
                "status": "ok",
                "detail": "created %d path(s) under %s" % (len(created), target),
                "group": "state",
            }
        )
        root = target

    context = None
    facts = {}
    modes = {}
    if root is not None:
        try:
            context = config.make_context(start=root, agent=args.agent, require_git=False)
        except config.ConfigError as exc:
            findings.append(
                {"name": "Project", "status": "fail", "detail": str(exc), "group": "state"}
            )
    if context is not None:
        facts = check_git(context, findings) or {}
        check_state_dirs(context, findings, args.fix)
        check_skill(context, findings)
        task_facts = check_tasks(context, findings) or {}
        modes = task_facts.get("modes") or {}
        facts["stale"] = task_facts.get("stale", 0)
        facts["live"] = task_facts.get("live", 0)
        check_assets(context, findings)
        if not args.quiet:
            common.print_header(context, "doctor")
        code = render(context, findings, facts, modes, args)
        if args.fix:
            common.audit(
                context, COMMAND, sys.argv[1:], "fixed" if code == 0 else "fix-incomplete"
            )
        return code

    if args.json:
        common.emit_json(
            {
                "ok": False,
                "system_status": "UNHEALTHY",
                "exit_code": 2,
                "findings": findings,
            }
        )
        return 2
    report.title("Agent Handoff Doctor")
    report.kv("Project", os.path.abspath(args.repo or os.getcwd()), 20)
    for item in findings:
        report.check_line(item)
    report.rule()
    report.echo("System Status: UNHEALTHY")
    report.echo("")
    if config.find_git_root(args.repo):
        report.echo("Run with --fix in the repository root to create .agent-state.")
    return 2


if __name__ == "__main__":
    sys.exit(common.run(main, COMMAND))
