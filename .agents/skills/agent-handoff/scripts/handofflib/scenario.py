"""End-to-end scenarios used by both the unit tests and ``simulate_handoff.py``.

Everything here drives the real CLI scripts as subprocesses inside a throwaway
git repository, so the scenarios exercise exactly what an agent would run:
no monkeypatched internals, no shortcuts.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from typing import List, Optional

from . import util, yamlio

SKILL_REL = os.path.join(".agents", "skills", "agent-handoff")


class ScenarioFailure(RuntimeError):
    def __init__(self, step: str, message: str = ""):
        self.step = step
        super().__init__(step + (": " + message if message else ""))


# ---------------------------------------------------------------------------
# environment helpers
# ---------------------------------------------------------------------------
def skill_dir_from_module() -> str:
    # <skill>/scripts/handofflib/scenario.py -> <skill>
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _env(agent: str) -> dict:
    env = dict(os.environ)
    env["AGENT_HANDOFF_AGENT"] = agent
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("AGENT_HANDOFF_PYTHON", None)
    return env


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "git",
            "-c",
            "core.quotepath=false",
            "-c",
            "color.ui=false",
            "-c",
            "user.name=handoff-sim",
            "-c",
            "user.email=handoff-sim@example.invalid",
            "-c",
            "commit.gpgsign=false",
        ]
        + list(args),
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _tail(text: Optional[str], lines: int = 12) -> str:
    if not text:
        return ""
    body = text.replace("\r\n", "\n").strip().split("\n")
    return "\n".join(body[-lines:])


def _write(root: str, rel: str, text: str) -> None:
    util.write_text(os.path.join(root, rel.replace("/", os.sep)), text)


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
class Runner:
    def __init__(self, root: str, log=print):
        self.root = root
        self.log = log
        self.steps: List[dict] = []
        self.scripts = os.path.join(root, SKILL_REL, "scripts")

    def run(self, script: str, args: List[str], agent: str = "dsh") -> subprocess.CompletedProcess:
        # --repo pins the project root: the scenarios must never depend on, or
        # accidentally write into, whatever repository happens to contain them.
        command = [sys.executable, os.path.join(self.scripts, script), "--repo", self.root] + list(args)
        proc = subprocess.run(
            command,
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_env(agent),
        )
        return proc

    def expect(self, name: str, condition: bool, detail: str = "", output: Optional[str] = None) -> None:
        ok = bool(condition)
        self.steps.append({"name": name, "ok": ok, "detail": detail})
        self.log("[%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  - " + detail) if detail else ""))
        if not ok:
            if output:
                self.log("  --- command output (tail) ---")
                for line in _tail(output).splitlines():
                    self.log("  " + line)
            failure = ScenarioFailure(name, detail)
            failure.steps = list(self.steps)
            raise failure

    def expect_rc(self, name: str, proc: subprocess.CompletedProcess, expected) -> None:
        codes = expected if isinstance(expected, (tuple, list)) else (expected,)
        detail = "exit=%d (expected %s)" % (proc.returncode, "/".join(str(code) for code in codes))
        if proc.returncode not in codes:
            detail += "  stderr: " + _tail(proc.stderr, 6).replace("\n", " ")
        self.expect(name, proc.returncode in codes, detail, proc.stdout + "\n" + proc.stderr)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def prepare_repo(root: str, log=print, with_state: bool = True) -> None:
    """Create a small but realistic git project that vendors the skill."""

    os.makedirs(root, exist_ok=True)
    skill_target = os.path.join(root, SKILL_REL)
    if not os.path.isdir(skill_target):
        shutil.copytree(skill_dir_from_module(), skill_target, ignore=shutil.ignore_patterns("__pycache__"))
    _write(
        root,
        "AGENTS.md",
        "# Test project\n\nLong lived rules. Uses .agents/skills/agent-handoff (agent-handoff).\n",
    )
    _write(root, "src/combat/ComboState.txt", "combo state: idle\n")
    _write(root, "src/combat/WuxiaCombatComponent.txt", "combat component: v1\n")
    _write(root, "src/enemy/WuxiaEnemy.txt", "enemy: v1\n")
    _write(root, ".gitignore", "work/\n__pycache__/\n")

    if not os.path.isdir(os.path.join(root, ".git")):
        _git(root, "init", "-b", "main")
        _git(root, "config", "user.name", "handoff-sim")
        _git(root, "config", "user.email", "handoff-sim@example.invalid")
        _git(root, "config", "commit.gpgsign", "false")
    _write(root, "src/combat/ComboState.txt", "combo state: idle\n")
    _git(root, "add", "-A")
    result = _git(root, "commit", "-m", "baseline: test project")
    if result.returncode != 0 and "nothing to commit" not in (result.stdout + result.stderr):
        raise ScenarioFailure("prepare", "baseline commit failed: %s" % _tail(result.stderr))
    log("[PASS] prepared scratch repository at %s" % root)


def _state_file(root: str, status: str, task_id: str) -> str:
    return os.path.join(root, ".agent-state", status, "%s.yaml" % task_id)


def _read_state(root: str, status: str, task_id: str) -> dict:
    return yamlio.load(_state_file(root, status, task_id))


def _journal(root: str, task_id: str) -> List[dict]:
    import json

    path = os.path.join(root, ".agent-state", "history", "%s.jsonl" % task_id)
    events = []
    for line in (util.read_text(path) or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return events


def _write_stale_lock(root: str, task_id: str, agent: str, branch: str, age_minutes: int = 180) -> None:
    """Simulate an agent that died: dead pid and an old heartbeat."""

    import datetime as dt

    stamp = util.iso(util.utc_now() - dt.timedelta(minutes=age_minutes))
    payload = {
        "task_id": task_id,
        "agent": agent,
        "machine": util.hostname(),
        "pid": "999999",
        "started_at": stamp,
        "heartbeat": stamp,
        "branch": branch,
    }
    yamlio.dump(payload, os.path.join(root, ".agent-state", "locks", "%s.lock" % task_id))


def _write_live_lock(root: str, task_id: str, agent: str, branch: str) -> None:
    """Simulate an agent that is still working: fresh heartbeat and a live pid."""

    payload = {
        "task_id": task_id,
        "agent": agent,
        "machine": util.hostname(),
        "pid": str(os.getpid()),
        "started_at": util.iso(),
        "heartbeat": util.iso(),
        "branch": branch,
    }
    yamlio.dump(payload, os.path.join(root, ".agent-state", "locks", "%s.lock" % task_id))


def _check_command(exit_code: int = 0) -> str:
    exe = sys.executable.replace('"', "")
    return '"%s" -c "import sys; sys.exit(%d)"' % (exe, exit_code)


# ---------------------------------------------------------------------------
# scenario 1: codex -> crash -> dsh resume -> finish
# ---------------------------------------------------------------------------
def run_handoff_scenario(root: str, log=print) -> List[dict]:
    prepare_repo(root, log=log)
    runner = Runner(root, log=log)
    task_id = "combat-0001"
    branch = "feature/" + task_id

    # 1. environment scaffold + baseline health
    proc = runner.run("doctor.py", ["--fix"], agent="dsh")
    runner.expect_rc("doctor --fix scaffolds the project", proc, (0, 1))
    runner.expect(
        "state skeleton exists",
        all(
            os.path.isdir(os.path.join(root, ".agent-state", name))
            for name in ("active", "completed", "failed", "history", "locks")
        ),
        ".agent-state/{active,completed,failed,history,locks}",
    )

    # 2. codex starts a task: branch + lock + ck-0001
    proc = runner.run(
        "checkpoint.py",
        [
            "start",
            "--id", task_id,
            "--title", "three-hit light combo",
            "--goal", "Implement the three-hit light combo with input buffering.",
            "--scope", "src/combat",
            "--check", "smoke=" + _check_command(0),
            "--remaining", "third hit",
            "--next", "read ComboState.txt",
        ],
        agent="codex",
    )
    runner.expect_rc("codex start creates the task", proc, 0)
    state = _read_state(root, "active", task_id)
    runner.expect("task is owned by codex", state.get("owner") == "codex", "owner=%s" % state.get("owner"))
    runner.expect("task branch was created", state.get("branch") == branch, "branch=%s" % state.get("branch"))
    runner.expect(
        "branch exists in git",
        _git(root, "rev-parse", "--verify", "refs/heads/" + branch).returncode == 0,
        branch,
    )
    runner.expect(
        "checkpoint ck-0001 recorded",
        (state.get("checkpoint") or {}).get("id") == "ck-0001",
        "checkpoint=%s" % (state.get("checkpoint") or {}).get("id"),
    )
    runner.expect(
        "lock files the owner as codex",
        os.path.isfile(os.path.join(root, ".agent-state", "locks", "%s.lock" % task_id)),
        "locks/%s.lock" % task_id,
    )

    # 3. codex implements two files and checkpoints
    _write(root, "src/combat/ComboState.txt", "combo state: three stages\n")
    _write(root, "src/combat/ComboInput.txt", "input buffer: 250ms\n")
    proc = runner.run(
        "checkpoint.py",
        [
            "save",
            "--id", task_id,
            "--done", "ComboState three stages",
            "--done", "input buffer",
            "--drop-todo", "third hit",
            "--todo", "hitbox test",
            "--next", "run the combo smoke test",
        ],
        agent="codex",
    )
    runner.expect_rc("codex checkpoints progress", proc, 0)
    state = _read_state(root, "active", task_id)
    runner.expect(
        "second checkpoint recorded",
        (state.get("checkpoint") or {}).get("id") == "ck-0002",
        (state.get("checkpoint") or {}).get("id"),
    )
    runner.expect(
        "work tree fingerprint captured",
        len(state.get("file_hashes") or []) >= 2,
        "%d hashed path(s), %d changed" % (len(state.get("file_hashes") or []), len(state.get("changed_files") or [])),
    )
    runner.expect(
        "checkpoint lists remaining work",
        "hitbox test" in (state.get("remaining") or []),
        ", ".join(state.get("remaining") or []),
    )

    # 3b. a freshly written checkpoint must not look like a crash: writing state
    # files is not work, so the fingerprint must ignore .agent-state/
    proc = runner.run("resume.py", ["--id", task_id, "--brief"], agent="codex")
    runner.expect_rc("resume right after a checkpoint succeeds", proc, 0)
    runner.expect(
        "resume reports CLEAN RESUME for an up-to-date checkpoint",
        "CLEAN RESUME" in proc.stdout,
        "state-file churn must not trigger RECOVERY",
    )

    # 4. codex keeps working for 8 files and then dies without a checkpoint
    for index in range(1, 9):
        _write(root, "src/combat/ComboStep%d.txt" % index, "stage %d: implemented, unverified\n" % index)
    _write(root, "src/combat/ComboState.txt", "combo state: three stages + cancel window\n")
    _write_stale_lock(root, task_id, "codex", branch)
    runner.expect(
        "crash simulated: 10 dirty paths, dead pid lock",
        len(
            [
                path
                for path in _git(root, "status", "--porcelain").stdout.splitlines()
                if path.strip()
            ]
        )
        >= 10,
        "uncommitted work exists",
    )

    # 5. dsh resumes read-only and detects recovery
    proc = runner.run("resume.py", ["--id", task_id], agent="dsh")
    runner.expect_rc("dsh resume runs read-only", proc, 0)
    output = proc.stdout
    runner.expect("resume reports RECOVERY MODE", "RECOVERY MODE" in output, "mode detected")
    runner.expect(
        "resume names the uncheckpointed files",
        "ComboStep1.txt" in output and "ComboState.txt" in output,
        "new + drifted paths listed",
    )
    runner.expect(
        "resume flags the stale lock",
        "stale" in output.lower(),
        "lock diagnosed as stale",
    )
    runner.expect(
        "resume does not change ownership without --take",
        _read_state(root, "active", task_id).get("owner") == "codex",
        "owner still codex",
    )
    json_proc = runner.run("resume.py", ["--id", task_id, "--json"], agent="dsh")
    runner.expect_rc("resume --json works", json_proc, 0)
    runner.expect(
        "json exposes recovery evidence",
        '"mode": "RECOVERY"' in json_proc.stdout and '"new_since_checkpoint"' in json_proc.stdout,
        "mode + evidence in JSON",
    )

    # 6. dsh takes over, but may not switch branches with a dirty tree
    proc = runner.run("resume.py", ["--id", task_id, "--take", "--checkout"], agent="dsh")
    runner.expect_rc("checkout with a dirty tree is refused", proc, 4)
    proc = runner.run("resume.py", ["--id", task_id, "--take"], agent="dsh")
    runner.expect_rc("dsh takes ownership", proc, 0)
    state = _read_state(root, "active", task_id)
    runner.expect("owner is now dsh", state.get("owner") == "dsh", "owner=%s" % state.get("owner"))
    events = [event.get("event") for event in _journal(root, task_id)]
    runner.expect("journal recorded the takeover", "taken_over" in events, ", ".join(events))
    runner.expect(
        "takeover is attributed to dsh",
        any(
            event.get("event") == "taken_over" and event.get("agent") == "dsh"
            for event in _journal(root, task_id)
        ),
        "taken_over by dsh",
    )

    # 7. dsh records what it found, then finishes the remaining work
    proc = runner.run(
        "checkpoint.py",
        [
            "save",
            "--id", task_id,
            "--kind", "recovery",
            "--note", "codex finished all eight combo stages but never checkpointed them",
            "--done", "eight combo stages (recovered, unverified)",
            "--drop-todo", "hitbox test",
            "--todo", "run the smoke check",
            "--next", "run the smoke check and commit",
        ],
        agent="dsh",
    )
    runner.expect_rc("dsh writes the recovery checkpoint", proc, 0)
    state = _read_state(root, "active", task_id)
    runner.expect(
        "recovery checkpoint recorded",
        (state.get("checkpoint") or {}).get("kind") == "recovery",
        "kind=%s" % (state.get("checkpoint") or {}).get("kind"),
    )
    runner.expect(
        "recovery note stored",
        "eight combo stages" in str(state.get("notes") or ""),
        "notes recorded",
    )

    _write(root, "src/combat/ComboTests.txt", "smoke: pass\n")
    proc = runner.run(
        "finish.py",
        ["--id", task_id, "--commit", "combat: three-hit light combo"],
        agent="dsh",
    )
    runner.expect_rc("finish refuses to archive with remaining work", proc, 4)
    runner.expect(
        "task is untouched by the refused finish",
        os.path.isfile(_state_file(root, "active", task_id)),
        "active/%s.yaml kept" % task_id,
    )
    proc = runner.run(
        "checkpoint.py",
        [
            "save",
            "--id", task_id,
            "--done", "smoke check passed",
            "--drop-todo", "run the smoke check",
            "--next", "commit and finish",
        ],
        agent="dsh",
    )
    runner.expect_rc("dsh clears the last remaining item", proc, 0)
    proc = runner.run(
        "finish.py",
        ["--id", task_id, "--commit", "combat: three-hit light combo"],
        agent="dsh",
    )
    runner.expect_rc("finish runs the declared check and archives", proc, 0)
    runner.expect(
        "task moved to completed/",
        os.path.isfile(_state_file(root, "completed", task_id))
        and not os.path.isfile(_state_file(root, "active", task_id)),
        "completed/%s.yaml" % task_id,
    )
    final = _read_state(root, "completed", task_id)
    runner.expect("archived status is completed", final.get("status") == "completed", final.get("status"))
    runner.expect("final commit recorded", bool(final.get("commit")), str(final.get("commit")))
    runner.expect(
        "verification result recorded",
        (final.get("verification") or {}).get("smoke") == "pass",
        str(final.get("verification")),
    )
    runner.expect(
        "lock released",
        not os.path.isfile(os.path.join(root, ".agent-state", "locks", "%s.lock" % task_id)),
        "locks/%s.lock removed" % task_id,
    )
    runner.expect(
        "work tree is clean after finish",
        _git(root, "status", "--porcelain").stdout.strip() == "",
        _git(root, "status", "--porcelain").stdout.strip()[:200],
    )
    events = [event.get("event") for event in _journal(root, task_id)]
    for expected_event in ("task_created", "checkpoint", "taken_over", "finished"):
        runner.expect(
            "journal contains %s" % expected_event,
            expected_event in events,
            ", ".join(events),
        )
    log_text = _git(root, "log", "--oneline").stdout
    runner.expect(
        "history was committed on the feature branch",
        "combat: three-hit light combo" in log_text,
        log_text.replace("\n", " | ")[:200],
    )

    proc = runner.run("doctor.py", [], agent="dsh")
    runner.expect_rc("doctor reports a healthy project", proc, 0)
    runner.expect("doctor says HEALTHY", "System Status: HEALTHY" in proc.stdout, "no findings")

    proc = runner.run("inspect.py", ["--git"], agent="dsh")
    runner.expect_rc("inspect works after archiving", proc, 0)
    runner.expect(
        "inspect reports no active task",
        "NO ACTIVE TASK" in proc.stdout,
        "nothing in progress",
    )
    return runner.steps


# ---------------------------------------------------------------------------
# scenario 2: a live foreign lock blocks takeover
# ---------------------------------------------------------------------------
def run_blocked_scenario(root: str, log=print) -> List[dict]:
    prepare_repo(root, log=log)
    runner = Runner(root, log=log)
    task_id = "enemy-0201"
    runner.run("doctor.py", ["--fix"], agent="dsh")
    proc = runner.run(
        "checkpoint.py",
        ["start", "--id", task_id, "--title", "enemy aggro", "--goal", "Implement enemy aggro."],
        agent="codex",
    )
    runner.expect_rc("start second task", proc, 0)
    _write_live_lock(root, task_id, "codex", "feature/" + task_id)
    proc = runner.run("resume.py", ["--id", task_id], agent="dsh")
    runner.expect_rc("resume is blocked by a live lock", proc, 3)
    runner.expect("resume says BLOCKED", "BLOCKED" in proc.stdout, "live lock detected")
    proc = runner.run("resume.py", ["--id", task_id, "--take"], agent="dsh")
    runner.expect_rc("takeover without --force is refused", proc, 3)
    runner.expect(
        "ownership unchanged while blocked",
        _read_state(root, "active", task_id).get("owner") == "codex",
        "owner still codex",
    )
    proc = runner.run("checkpoint.py", ["save", "--id", task_id, "--done", "x"], agent="dsh")
    runner.expect_rc("checkpoint save without --force is refused", proc, 3)
    proc = runner.run(
        "finish.py", ["--id", task_id, "--fail", "--reason", "cleanup", "--force"], agent="dsh"
    )
    runner.expect_rc("forced archive cleans the scenario up", proc, 0)
    runner.expect(
        "task archived as failed",
        os.path.isfile(_state_file(root, "failed", task_id)),
        "failed/%s.yaml" % task_id,
    )
    events = [event.get("event") for event in _journal(root, task_id)]
    runner.expect("forced takeover journalled", "lock_forced" in events, ", ".join(events))
    return runner.steps


# ---------------------------------------------------------------------------
# scenario 3: a failing check blocks finish
# ---------------------------------------------------------------------------
def run_failing_check_scenario(root: str, log=print) -> List[dict]:
    prepare_repo(root, log=log)
    runner = Runner(root, log=log)
    task_id = "combat-0002"
    runner.run("doctor.py", ["--fix"], agent="dsh")
    proc = runner.run(
        "checkpoint.py",
        [
            "start",
            "--id", task_id,
            "--title", "failing check demo",
            "--goal", "Prove that a failing verification keeps the task active.",
            "--check", "smoke=" + _check_command(3),
        ],
        agent="codex",
    )
    runner.expect_rc("start task with a failing check", proc, 0)
    _write(root, "src/combat/Fail.txt", "step\n")
    proc = runner.run("finish.py", ["--id", task_id], agent="codex")
    runner.expect_rc("finish refuses to archive on failure", proc, 5)
    runner.expect(
        "task stays active after a failed check",
        os.path.isfile(_state_file(root, "active", task_id)),
        "active/%s.yaml kept" % task_id,
    )
    events = [event.get("event") for event in _journal(root, task_id)]
    runner.expect("failure journalled", "finish_blocked" in events, ", ".join(events))
    proc = runner.run(
        "finish.py", ["--id", task_id, "--fail", "--reason", "demo cleanup"], agent="codex"
    )
    runner.expect_rc("failed archive works", proc, 0)
    return runner.steps


SCENARIOS = {
    "handoff": run_handoff_scenario,
    "blocked": run_blocked_scenario,
    "failing-check": run_failing_check_scenario,
}


def run_scenario(name: str, base_dir: str, log=print) -> dict:
    """Run one scenario in ``base_dir/<name>``; never raises on assertion failure."""

    scenario = SCENARIOS[name]
    root = os.path.join(base_dir, name)
    steps: List[dict] = []
    error = None
    try:
        steps = scenario(root, log=log)
    except ScenarioFailure as exc:
        error = str(exc)
        steps = getattr(exc, "steps", steps) or steps
    return {
        "name": name,
        "root": root,
        "ok": error is None,
        "error": error,
        "steps": steps,
    }
