"""Append-only journals.

Two logs, both plain text under git:

``history/<task_id>.jsonl``
    Everything that happened to one task - created, checkpointed, taken over,
    checks run, finished.  Never rewritten, never pruned.

``history/commands.jsonl``
    One line per state-changing CLI invocation, whoever ran it.  This is the
    audit trail the project requires: every automated action is attributable to
    an agent, a machine and a timestamp.
"""

from __future__ import annotations

import json
import os
from typing import List, Optional

from . import util


def task_journal_path(context, task_id: str) -> str:
    return os.path.join(context.history_dir(), "%s.jsonl" % task_id)


def command_log_path(context) -> str:
    return os.path.join(context.history_dir(), "commands.jsonl")


def append_event(
    context,
    task_id: str,
    event: str,
    data: Optional[dict] = None,
    agent: Optional[str] = None,
) -> dict:
    record = {
        "at": util.iso(),
        "event": event,
        "task_id": task_id,
        "agent": agent or context.agent,
        "machine": context.machine,
    }
    if data:
        record["data"] = data
    line = json.dumps(record, ensure_ascii=False, sort_keys=False)
    util.append_text(task_journal_path(context, task_id), line + "\n")
    return record


def read_events(context, task_id: str) -> List[dict]:
    path = task_journal_path(context, task_id)
    text = util.read_text(path) or ""
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            events.append({"event": "unreadable", "raw": util.truncate(line, 200)})
            continue
        if isinstance(record, dict):
            events.append(record)
    return events


def append_command(
    context,
    command: str,
    argv: List[str],
    outcome: str,
    task_id: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    record = {
        "at": util.iso(),
        "command": command,
        "argv": list(argv or []),
        "outcome": outcome,
        "agent": context.agent,
        "agent_source": context.agent_source,
        "machine": context.machine,
        "task_id": task_id or "",
    }
    if detail:
        record["detail"] = util.truncate(detail, 400)
    util.append_text(command_log_path(context), json.dumps(record, ensure_ascii=False) + "\n")


def read_commands(context, limit: int = 20) -> List[dict]:
    text = util.read_text(command_log_path(context)) or ""
    records = []
    for line in text.splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records
