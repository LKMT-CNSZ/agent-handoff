"""Run the verification commands a task declares.

Commands are plain strings executed by the platform shell (``cmd.exe`` on
Windows, ``/bin/sh`` elsewhere), so a project can declare PowerShell, bash or
compiler invocations without the handoff scripts knowing about them.  The full
output is captured, the tail is printed, and the exit code decides pass/fail.

Nothing here guesses: if a command is not declared, the task is not verified.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Iterable, List, Optional

from . import util

PASS = "pass"
FAIL = "fail"
TIMEOUT = "timeout"
SKIPPED = "skipped"

OUTPUT_TAIL_LINES = 40
OUTPUT_TAIL_CHARS = 6000


def run_one(name: str, command: str, cwd: str, timeout: int) -> dict:
    started = time.time()
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("GIT_OPTIONAL_LOCKS", "0")
    record = {
        "name": name,
        "command": command,
        "exit_code": None,
        "status": FAIL,
        "duration_seconds": 0.0,
        "output_tail": "",
    }
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        output = completed.stdout.decode("utf-8", "replace") + completed.stderr.decode("utf-8", "replace")
        record["exit_code"] = completed.returncode
        record["status"] = PASS if completed.returncode == 0 else FAIL
    except subprocess.TimeoutExpired as exc:
        raw = (exc.stdout or b"") + (exc.stderr or b"")
        output = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        record["exit_code"] = None
        record["status"] = TIMEOUT
        output += "\n[timeout after %ss]" % timeout
    except (OSError, ValueError) as exc:
        output = "[could not run command: %s]" % exc
        record["status"] = FAIL
    record["duration_seconds"] = round(time.time() - started, 2)
    record["output_tail"] = _tail(output)
    return record


def _tail(text: str, lines: int = OUTPUT_TAIL_LINES, chars: int = OUTPUT_TAIL_CHARS) -> str:
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return ""
    chunks = text.split("\n")
    text = "\n".join(chunks[-lines:])
    if len(text) > chars:
        text = "...(truncated)\n" + text[-chars:]
    return text


def select_commands(task: dict, only: Optional[Iterable[str]] = None, include_optional: bool = True) -> List[dict]:
    selected = []
    wanted = {item.strip().lower() for item in (only or []) if item and item.strip()}
    for entry in util.as_list_entries(task.get("verification_commands")):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        command = str(entry.get("command") or "").strip()
        if not command:
            continue
        if wanted and name.lower() not in wanted:
            continue
        required = util.as_bool(entry.get("required"), True)
        if not required and not include_optional and not wanted:
            continue
        selected.append({"name": name or "check", "command": command, "required": required})
    return selected


def run_task_checks(
    context,
    task: dict,
    only: Optional[Iterable[str]] = None,
    include_optional: bool = True,
    timeout: Optional[int] = None,
) -> List[dict]:
    commands = select_commands(task, only=only, include_optional=include_optional)
    timeout = int(timeout or context.verification_timeout())
    results = []
    for entry in commands:
        results.append(run_one(entry["name"], entry["command"], context.root, timeout))
        results[-1]["required"] = entry["required"]
    return results


def overall(results: Iterable[dict]) -> str:
    statuses = [record.get("status") for record in results]
    if not statuses:
        return "unverified"
    if all(status == PASS for status in statuses):
        return "pass"
    if any(status == FAIL for status in statuses):
        return "fail"
    if any(status == TIMEOUT for status in statuses):
        return "timeout"
    return "partial"


def required_failures(results: Iterable[dict]) -> List[dict]:
    return [
        record
        for record in results
        if record.get("required", True) and record.get("status") != PASS
    ]
