"""Lightweight per-task agent lock.

A lock is advisory but honest: it records *who* said they were working, on which
machine, from which pid, and when they last proved they were alive.  The lock is
never a license to overwrite somebody else's uncommitted work - it only decides
whether the next agent should stop and ask.

Staleness is deliberately conservative:

* heartbeat older than ``lock.ttl_minutes`` -> stale;
* the recorded PID belongs to a short-lived command, not the Agent session;
* everything else -> live, and the next agent must not touch the task without
  an explicit ``--force`` that is written into history.
"""

from __future__ import annotations

import os
from typing import Optional

from . import util, yamlio

LOCK_STATES = ("none", "live_same", "live_foreign", "stale", "corrupt")

LOCK_FIELDS = ["task_id", "agent", "machine", "pid", "started_at", "heartbeat", "branch"]


def lock_path(context, task_id: str) -> str:
    return os.path.join(context.lock_dir(), "%s.lock" % task_id)


def read(context, task_id: str) -> Optional[dict]:
    path = lock_path(context, task_id)
    if not os.path.isfile(path):
        return None
    try:
        data = yamlio.load(path)
    except (yamlio.YamlError, OSError) as exc:
        return {"__corrupt__": str(exc), "path": path, "task_id": task_id}
    if not isinstance(data, dict):
        return {"__corrupt__": "lock is not a mapping", "path": path, "task_id": task_id}
    return data


def write(context, task: dict, agent: Optional[str] = None, branch: Optional[str] = None) -> str:
    task_id = str(task.get("task_id"))
    agent = agent or context.agent
    path = lock_path(context, task_id)
    previous = read(context, task_id) or {}
    started = util.iso()
    if (
        previous
        and not previous.get("__corrupt__")
        and previous.get("agent") == agent
        and previous.get("machine") == context.machine
        and previous.get("started_at")
    ):
        started = str(previous.get("started_at"))
    payload = {
        "task_id": task_id,
        "agent": agent,
        "machine": context.machine,
        "pid": str(os.getpid()),
        "started_at": started,
        "heartbeat": util.iso(),
        "branch": branch or str(task.get("branch") or ""),
    }
    yamlio.dump(payload, path)
    return path


def refresh(context, task_id: str, agent: Optional[str] = None) -> tuple:
    """Refresh the heartbeat when we own the lock.

    Returns ``(refreshed, reason)``.  A foreign lock is never refreshed.
    """

    data = read(context, task_id)
    if data is None:
        return False, "no lock file"
    if data.get("__corrupt__"):
        return False, "lock file is corrupt"
    agent = agent or context.agent
    if data.get("agent") != agent or data.get("machine") != context.machine:
        return False, "lock belongs to %s@%s" % (data.get("agent"), data.get("machine"))
    data["heartbeat"] = util.iso()
    data["pid"] = str(os.getpid())
    yamlio.dump({key: data.get(key, "") or "" for key in LOCK_FIELDS}, lock_path(context, task_id))
    return True, "ok"


def remove(context, task_id: str, agent: Optional[str] = None) -> bool:
    """Remove our own lock.  A foreign lock is left untouched."""

    data = read(context, task_id)
    if data is None:
        return False
    if data.get("__corrupt__"):
        return False
    if agent and data.get("agent") and data.get("agent") != agent:
        return False
    try:
        os.remove(lock_path(context, task_id))
    except OSError:
        return False
    return True


def analyze(context, task_id: str, ttl_minutes: Optional[int] = None) -> dict:
    ttl = int(ttl_minutes or context.lock_ttl_minutes())
    ttl_seconds = ttl * 60
    data = read(context, task_id)
    result = {
        "present": data is not None,
        "task_id": task_id,
        "state": "none",
        "reasons": [],
        "agent": None,
        "machine": None,
        "pid": None,
        "branch": None,
        "heartbeat": None,
        "started_at": None,
        "age_seconds": None,
        "age_human": "unknown",
        "ttl_minutes": ttl,
    }
    if data is None:
        result["reasons"] = ["no lock file"]
        return result
    if data.get("__corrupt__"):
        result["state"] = "corrupt"
        result["reasons"] = ["lock file cannot be parsed: %s" % data["__corrupt__"]]
        return result

    result.update(
        {
            "agent": data.get("agent"),
            "machine": data.get("machine"),
            "pid": data.get("pid"),
            "branch": data.get("branch"),
            "heartbeat": data.get("heartbeat"),
            "started_at": data.get("started_at"),
        }
    )
    age = util.age_seconds(data.get("heartbeat") or data.get("started_at"))
    result["age_seconds"] = age
    result["age_human"] = util.humanize_seconds(age)

    same_machine = data.get("machine") == context.machine
    same_agent = data.get("agent") == context.agent and same_machine
    alive = util.pid_alive(util.as_int(data.get("pid"), 0)) if same_machine else None
    result["pid_alive"] = alive

    reasons = []
    if age is None:
        result["state"] = "corrupt"
        result["reasons"] = ["heartbeat is missing or unparsable"]
        return result
    if age > ttl_seconds:
        reasons.append("heartbeat is %s old, ttl is %d min" % (util.humanize_seconds(age), ttl))

    if same_agent:
        result["state"] = "live_same"
        result["reasons"] = ["held by this agent on this machine"]
        return result
    if reasons:
        result["state"] = "stale"
        result["reasons"] = reasons
        return result
    result["state"] = "live_foreign"
    result["reasons"] = [
        "%s@%s heartbeated %s ago" % (data.get("agent"), data.get("machine"), result["age_human"])
    ]
    return result


def can_take(context, evidence_lock: dict, force: bool) -> tuple:
    """Decide whether the current agent may take ownership."""

    state = evidence_lock.get("state")
    if state == "none":
        return True, "no existing lock"
    if state == "live_same":
        return True, "lock already held by this agent"
    if state == "stale":
        return True, "stale lock: %s" % "; ".join(evidence_lock.get("reasons") or [])
    if state == "corrupt":
        if force:
            return True, "corrupt lock replaced because --force was given"
        return False, "lock file is corrupt; pass --force after inspecting it"
    if force:
        return True, "live lock of %s@%s taken because --force was given" % (
            evidence_lock.get("agent"),
            evidence_lock.get("machine"),
        )
    return False, (
        "%s@%s may still be working (heartbeat %s ago); wait, or pass --force "
        "only after confirming the other agent stopped"
        % (evidence_lock.get("agent"), evidence_lock.get("machine"), evidence_lock.get("age_human"))
    )


def find_orphan_locks(context) -> list:
    """Lock files that have no matching active task."""

    orphans = []
    directory = context.lock_dir()
    if not os.path.isdir(directory):
        return orphans
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".lock"):
            continue
        task_id = name[: -len(".lock")]
        if not os.path.isfile(os.path.join(context.active_dir(), "%s.yaml" % task_id)):
            orphans.append(task_id)
    return orphans
