"""Task schema, discovery and the checkpoint-vs-reality comparison.

The comparison in ``collect_evidence`` is the heart of the system.  A checkpoint
stores a fingerprint of the dirty work tree at the moment it was written; the
same fingerprint is recomputed on resume.  Any difference means "the previous
agent did work this checkpoint does not know about", which is exactly the crash
signal we must never ignore.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from . import SCHEMA_VERSION, util, yamlio

TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")

ACTIVE_STATUSES = ("in_progress", "blocked", "paused")
TERMINAL_STATUSES = ("completed", "failed")
STATUS_LABELS = {
    "in_progress": "in progress",
    "blocked": "blocked",
    "paused": "paused",
    "completed": "completed",
    "failed": "failed",
}

# Canonical field order for the written state file: git facts first, prose last.
FIELD_ORDER = [
    "task_id",
    "format_version",
    "title",
    "goal",
    "owner",
    "status",
    "created_at",
    "updated_at",
    "branch",
    "base_branch",
    "commit",
    "parent_task",
    "dependencies",
    "write_scope",
    "next_action",
    "completed",
    "remaining",
    "changed_files",
    "file_hashes",
    "hashes_capped",
    "verification",
    "verification_commands",
    "checkpoint",
    "git_snapshot",
    "warnings",
    "notes",
]

LIST_FIELDS = ("dependencies", "write_scope", "completed", "remaining", "changed_files", "warnings")
TEXT_FIELDS = (
    "task_id",
    "title",
    "goal",
    "owner",
    "status",
    "created_at",
    "updated_at",
    "branch",
    "base_branch",
    "commit",
    "parent_task",
    "next_action",
    "notes",
    "format_version",
    "hashes_capped",
)

HASH_CAP = 400
HASH_MAX_BYTES = 2 * 1024 * 1024


# ---------------------------------------------------------------------------
# task objects
# ---------------------------------------------------------------------------
def _text(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize(raw: dict) -> dict:
    data: Dict[str, object] = {}
    for key in FIELD_ORDER:
        if key in raw:
            data[key] = raw[key]
    for key, value in raw.items():
        if key not in data:
            data[key] = value

    for key in TEXT_FIELDS:
        if key in data:
            data[key] = _text(data[key])
    for key in LIST_FIELDS:
        data[key] = util.as_list(data.get(key))

    checkpoint = util.as_dict(data.get("checkpoint"))
    data["checkpoint"] = {
        "id": _text(checkpoint.get("id")) or "",
        "at": _text(checkpoint.get("at")) or "",
        "by": _text(checkpoint.get("by")) or "",
        "kind": _text(checkpoint.get("kind")) or "progress",
        "count": _text(checkpoint.get("count")) or "0",
        "head": _text(checkpoint.get("head")) or "",
        "dirty": _text(checkpoint.get("dirty")) or "false",
        "changed_count": _text(checkpoint.get("changed_count")) or "0",
        "note": _text(checkpoint.get("note")) or "",
    }

    verification = util.as_dict(data.get("verification"))
    data["verification"] = {
        str(k): (_text(v) or "") for k, v in verification.items() if _text(k)
    }

    commands = []
    for entry in util.as_list_entries(data.get("verification_commands")):
        if isinstance(entry, dict):
            command = {
                "name": _text(entry.get("name")) or "",
                "command": _text(entry.get("command")) or "",
                "required": _text(entry.get("required")) or "true",
            }
            if command["command"]:
                commands.append(command)
    data["verification_commands"] = commands

    hashes = []
    for entry in util.as_list_entries(data.get("file_hashes")):
        if isinstance(entry, dict) and entry.get("path"):
            hashes.append(
                {
                    "path": util.to_posix(_text(entry.get("path")) or ""),
                    "sha256": _text(entry.get("sha256")) or "",
                    "size": _text(entry.get("size")) or "",
                    "code": _text(entry.get("code")) or "",
                }
            )
    data["file_hashes"] = hashes

    snapshot = util.as_dict(data.get("git_snapshot"))
    data["git_snapshot"] = {
        "branch": _text(snapshot.get("branch")) or "",
        "head": _text(snapshot.get("head")) or "",
        "head_short": _text(snapshot.get("head_short")) or "",
        "dirty": _text(snapshot.get("dirty")) or "false",
        "changed_count": _text(snapshot.get("changed_count")) or "0",
        "captured_at": _text(snapshot.get("captured_at")) or "",
    }

    if not data.get("format_version"):
        data["format_version"] = str(SCHEMA_VERSION)
    return data


class Task:
    def __init__(self, data: dict, path: Optional[str] = None):
        self.raw = normalize(data)
        self.path = path

    # -- simple accessors --------------------------------------------------
    def get(self, key: str, default: object = None) -> object:
        return self.raw.get(key, default)

    def get_list(self, key: str) -> List[str]:
        return list(self.raw.get(key) or [])

    def set(self, key: str, value: object) -> None:
        self.raw[key] = value

    def append(self, key: str, values) -> List[str]:
        current = self.get_list(key)
        for value in values if isinstance(values, (list, tuple)) else [values]:
            text = str(value).strip()
            if text and text not in current:
                current.append(text)
        self.raw[key] = current
        return current

    def drop_matching(self, key: str, prefix: str) -> Tuple[List[str], int]:
        """Remove entries whose start matches ``prefix``; must hit exactly one."""

        current = self.get_list(key)
        needle = str(prefix).strip().lower()
        matches = [item for item in current if item.lower().startswith(needle)]
        if len(matches) != 1:
            return current, len(matches)
        remaining = [item for item in current if item not in matches]
        self.raw[key] = remaining
        return remaining, 1

    # -- conveniences ------------------------------------------------------
    @property
    def task_id(self) -> str:
        return str(self.get("task_id") or "")

    @property
    def title(self) -> str:
        return str(self.get("title") or "")

    @property
    def status(self) -> str:
        return str(self.get("status") or "")

    @property
    def checkpoint(self) -> dict:
        return util.as_dict(self.get("checkpoint"))

    @property
    def git_snapshot(self) -> dict:
        return util.as_dict(self.get("git_snapshot"))

    def checkpoint_id(self) -> str:
        return str(self.checkpoint.get("id") or "")

    def touch(self, when: Optional[str] = None) -> None:
        self.set("updated_at", when or util.iso())

    # -- validation --------------------------------------------------------
    def validate(self) -> List[str]:
        problems: List[str] = []
        if not self.task_id:
            problems.append("task_id is missing")
        elif not TASK_ID_RE.match(self.task_id):
            problems.append(
                "task_id %r is invalid (use lowercase letters, digits, '.', '_' or '-')"
                % self.task_id
            )
        if not self.title:
            problems.append("title is missing")
        if not self.get("goal"):
            problems.append("goal is missing")
        if not self.get("owner"):
            problems.append("owner is missing")
        if not self.get("created_at"):
            problems.append("created_at is missing")
        if not self.get("updated_at"):
            problems.append("updated_at is missing")
        status = self.status
        if status and status not in ACTIVE_STATUSES + TERMINAL_STATUSES:
            problems.append(
                "status %r is not one of %s"
                % (status, ", ".join(ACTIVE_STATUSES + TERMINAL_STATUSES))
            )
        commit = str(self.get("commit") or "")
        if commit and not COMMIT_RE.match(commit):
            problems.append("commit %r is not a git object id" % commit)
        branch = str(self.get("branch") or "")
        if branch and (branch.startswith("-") or " " in branch):
            problems.append("branch %r is not a valid branch name" % branch)
        for entry in util.as_list_entries(self.get("verification_commands")):
            if isinstance(entry, dict):
                if not entry.get("name"):
                    problems.append("verification_commands entry without a name")
                if not entry.get("command"):
                    problems.append(
                        "verification_commands entry %r has no command" % entry.get("name")
                    )
        for path in self.get_list("changed_files"):
            if path.startswith("..") or path.startswith("/"):
                problems.append("changed_files entry escapes the project: %r" % path)
        return problems

    # -- persistence -------------------------------------------------------
    def to_ordered(self) -> dict:
        ordered: Dict[str, object] = {}
        for key in FIELD_ORDER:
            if key in self.raw:
                ordered[key] = self.raw[key]
        for key in sorted(self.raw):
            if key not in ordered:
                ordered[key] = self.raw[key]
        return ordered

    def to_yaml(self) -> str:
        return yamlio.dumps(self.to_ordered())

    def save(self, path: Optional[str] = None) -> str:
        target = path or self.path
        if not target:
            raise ValueError("task has no path; pass one to save()")
        yamlio.dump(self.to_ordered(), target)
        self.path = target
        return target

    def summary_line(self) -> str:
        cp = self.checkpoint
        bits = ["%s  %s" % (self.task_id, util.truncate(self.title, 60))]
        bits.append("[%s]" % (STATUS_LABELS.get(self.status, self.status or "?")))
        if self.get("owner"):
            bits.append("owner=%s" % self.get("owner"))
        if self.get("branch"):
            bits.append("branch=%s" % self.get("branch"))
        if cp.get("id"):
            bits.append("checkpoint=%s" % cp.get("id"))
        return "  ".join(bits)


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def task_path(context, task_id: str, status: str = "active") -> str:
    return os.path.join(context.state_path(status), "%s.yaml" % task_id)


def find_task_file(context, task_id: str) -> Optional[str]:
    for status in ("active", "completed", "failed"):
        candidate = task_path(context, task_id, status)
        if os.path.isfile(candidate):
            return candidate
    return None


def load_task_file(path: str) -> Task:
    data = yamlio.load(path)
    if not isinstance(data, dict):
        raise ValueError("task file %s must contain a mapping" % path)
    return Task(data, path)


def load_tasks_in(context, status: str) -> Tuple[List[Task], List[dict]]:
    tasks: List[Task] = []
    problems: List[dict] = []
    directory = context.state_path(status)
    if not os.path.isdir(directory):
        problems.append({"path": directory, "message": "directory is missing"})
        return tasks, problems
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".yaml"):
            continue
        path = os.path.join(directory, name)
        try:
            task = load_task_file(path)
        except (yamlio.YamlError, ValueError, OSError) as exc:
            problems.append({"path": path, "message": "cannot parse: %s" % exc})
            continue
        problems.extend(
            {"path": path, "message": message, "task_id": task.task_id}
            for message in task.validate()
        )
        tasks.append(task)
    return tasks, problems


def load_active_tasks(context) -> Tuple[List[Task], List[dict]]:
    return load_tasks_in(context, "active")


def resolve_task(context, task_id: Optional[str], allow_terminal: bool = False) -> Tuple[Optional[Task], List[str]]:
    """Pick the task for an action; never guesses when the answer is ambiguous."""

    notes: List[str] = []
    if task_id:
        clean = task_id.strip()
        path = find_task_file(context, clean)
        if not path:
            return None, ["no task named %r in active/, completed/ or failed/" % clean]
        try:
            task = load_task_file(path)
        except (yamlio.YamlError, ValueError, OSError) as exc:
            return None, ["task %r cannot be parsed: %s" % (clean, exc)]
        if not allow_terminal and task.status in TERMINAL_STATUSES:
            return None, [
                "task %r is already %s (%s)" % (clean, task.status, path)
            ]
        return task, notes
    tasks, problems = load_active_tasks(context)
    for problem in problems:
        notes.append("%s: %s" % (problem.get("path"), problem.get("message")))
    if not tasks:
        return None, ["no active task in %s" % context.active_dir()]
    if len(tasks) == 1:
        return tasks[0], notes
    ids = ", ".join(task.task_id for task in tasks)
    return None, [
        "%d active tasks (%s); pass --id to choose one" % (len(tasks), ids)
    ]


# ---------------------------------------------------------------------------
# work tree snapshot
# ---------------------------------------------------------------------------
def collect_snapshot(context, entries: Optional[List[dict]] = None) -> dict:
    """Fingerprint the dirty work tree.

    ``changed_files`` always lists every dirty path.  ``file_hashes`` stores the
    content hash of manageable files so a later run can tell "same tree as the
    checkpoint" from "different tree".

    ``.agent-state/`` is excluded on purpose: the state directory is the
    checkpoint mechanism itself, it changes on every command, and counting it as
    work would make every resume look like a crash.
    """

    git = context.git
    if entries is None:
        entries = git.status_entries() if git else []
    from .config import STATE_DIR_NAME

    state_prefix = STATE_DIR_NAME + "/"
    code_by_path: Dict[str, str] = {}
    for entry in entries:
        paths = [entry["path"]]
        if entry.get("original"):
            paths.append(entry["original"])
        for path in paths:
            if path.startswith(state_prefix):
                continue
            code_by_path.setdefault(path, entry["code"])
    changed = sorted(code_by_path)

    hashes: List[dict] = []
    capped = False
    omitted: List[str] = []
    for path in changed:
        if len(hashes) >= HASH_CAP:
            capped = True
            break
        full = os.path.join(context.root, path.replace("/", os.sep))
        size = util.file_size(full)
        code = code_by_path.get(path, "")
        if size is None:
            hashes.append({"path": path, "sha256": "missing", "size": "", "code": code})
            continue
        if size > HASH_MAX_BYTES:
            omitted.append(path)
            continue
        digest = util.sha256_file(full)
        if digest is None:
            omitted.append(path)
            continue
        hashes.append({"path": path, "sha256": digest, "size": str(size), "code": code})
    return {
        "changed_files": changed,
        "file_hashes": hashes,
        "capped": capped,
        "omitted": omitted,
        "count": len(changed),
        "binary": [path for path in changed if context.is_binary_path(path)],
    }


# ---------------------------------------------------------------------------
# evidence + mode
# ---------------------------------------------------------------------------
def _check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def collect_evidence(context, task: Task, lock_state: Optional[dict] = None) -> dict:
    git = context.git
    checks: List[dict] = []
    evidence: Dict[str, object] = {
        "task_id": task.task_id,
        "checks": checks,
        "lock": lock_state or {},
        "problems": task.validate(),
    }
    if git is None:
        checks.append(_check("git", "fail", "git is not usable in this project"))
        evidence["mode"] = "CORRUPT"
        evidence["reasons"] = ["git is not usable; nothing about the work tree can be trusted"]
        return evidence

    has_commit = git.has_commit()
    branch = git.branch()
    head = git.head()
    head_short = git.head_short()
    detached = git.detached()
    entries = git.status_entries()
    snapshot = collect_snapshot(context, entries)
    checkpoint = task.checkpoint
    cp_head = str(checkpoint.get("head") or task.get("commit") or "")
    cp_branch = str(task.get("branch") or "")
    cp_dirty = util.as_bool(checkpoint.get("dirty"), False)

    evidence.update(
        {
            "branch": branch,
            "detached": detached,
            "head": head,
            "head_short": head_short,
            "has_commit": has_commit,
            "commit_count": git.commit_count(),
            "dirty": bool(entries),
            "changed_count": snapshot["count"],
            "changed_files": snapshot["changed_files"],
            "binary_changed": snapshot["binary"],
            "checkpoint_head": cp_head or None,
            "checkpoint_branch": cp_branch or None,
            "checkpoint_dirty": cp_dirty,
            "checkpoint_at": str(checkpoint.get("at") or ""),
            "checkpoint_head_short": (cp_head[:8] if cp_head else None),
            "snapshot_hashes": snapshot["file_hashes"],
            "status_entries": entries,
        }
    )

    if not has_commit:
        checks.append(
            _check(
                "git baseline",
                "warn",
                "repository has no commit yet; branch / HEAD comparisons are limited",
            )
        )
    else:
        checks.append(_check("git baseline", "ok", "HEAD %s on %s" % (head_short, branch or "(detached)")))

    if cp_branch:
        if git.branch_exists(cp_branch):
            checks.append(_check("task branch", "ok", "%s exists" % cp_branch))
            evidence["branch_exists"] = True
        else:
            checks.append(
                _check("task branch", "diff", "checkpoint names %r but that branch does not exist" % cp_branch)
            )
            evidence["branch_exists"] = False
    else:
        checks.append(_check("task branch", "warn", "task has no branch recorded"))

    if detached:
        checks.append(_check("current branch", "warn", "detached HEAD; commits would be unreachable"))
    elif cp_branch and branch:
        if branch == cp_branch:
            checks.append(_check("current branch", "ok", "on %s" % branch))
            evidence["branch_matches"] = True
        else:
            checks.append(
                _check(
                    "current branch",
                    "diff",
                    "on %r but the task belongs to %r" % (branch, cp_branch),
                )
            )
            evidence["branch_matches"] = False

    if cp_head:
        if git.commit_exists(cp_head):
            checks.append(_check("checkpoint commit", "ok", "%s exists" % cp_head[:8]))
            evidence["checkpoint_commit_exists"] = True
        else:
            checks.append(
                _check(
                    "checkpoint commit",
                    "diff",
                    "checkpoint commit %s does not exist in this clone" % cp_head[:8],
                )
            )
            evidence["checkpoint_commit_exists"] = False
    else:
        checks.append(_check("checkpoint commit", "warn", "checkpoint recorded no commit"))

    head_drift = bool(cp_head and head and cp_head != head)
    evidence["head_drift"] = head_drift
    if head_drift and cp_head:
        ancestor = git.is_ancestor(cp_head, head)
        evidence["checkpoint_is_ancestor"] = ancestor
        ahead = len(git.commits_between(cp_head, head)) if ancestor else 0
        evidence["commits_since_checkpoint"] = ahead
        if ancestor:
            checks.append(
                _check(
                    "checkpoint head",
                    "warn",
                    "HEAD advanced %d commit(s) after the checkpoint (%s -> %s)"
                    % (ahead, cp_head[:8], head_short),
                )
            )
        else:
            checks.append(
                _check(
                    "checkpoint head",
                    "diff",
                    "checkpoint head %s is not an ancestor of HEAD %s (history diverged / rewritten)"
                    % (cp_head[:8], head_short),
                )
            )

    # --- the crash detector: compare the work tree with the fingerprint ----
    from .config import STATE_DIR_NAME

    state_prefix = STATE_DIR_NAME + "/"
    snapshot_hashes = {
        entry["path"]: entry["sha256"]
        for entry in util.as_list_entries(task.get("file_hashes"))
        if isinstance(entry, dict)
        and entry.get("sha256")
        and entry.get("path")
        and not str(entry.get("path")).startswith(state_prefix)
    }
    current_hashes = {entry["path"]: entry["sha256"] for entry in snapshot["file_hashes"]}
    capped = util.as_bool(task.get("hashes_capped"), False)

    new_outside: List[str] = []
    hash_drift: List[str] = []
    vanished: List[str] = []
    if snapshot_hashes:
        new_outside = sorted(set(current_hashes) - set(snapshot_hashes))
        hash_drift = sorted(
            path
            for path in set(current_hashes) & set(snapshot_hashes)
            if current_hashes[path] != snapshot_hashes[path]
        )
        vanished = sorted(set(snapshot_hashes) - set(current_hashes))

    vanished_detail: Dict[str, str] = {}
    for path in vanished:
        full = os.path.join(context.root, path.replace("/", os.sep))
        if not os.path.exists(full):
            vanished_detail[path] = "deleted"
        elif has_commit and git.run(["cat-file", "-e", "HEAD:%s" % path]).ok:
            vanished_detail[path] = "committed"
        else:
            vanished_detail[path] = "reverted"

    claimed = task.get_list("changed_files")
    missing_claimed = [
        path
        for path in claimed
        if not os.path.exists(os.path.join(context.root, path.replace("/", os.sep)))
    ]
    unexplained_vanished = [p for p in vanished if vanished_detail.get(p) != "committed"]
    count_mismatch = False
    if not snapshot_hashes and snapshot["count"] and cp_dirty:
        # Old checkpoint without hashes: fall back to a count comparison.
        count_mismatch = util.as_int(checkpoint.get("changed_count"), 0) != snapshot["count"]
    elif capped and not (new_outside or hash_drift):
        count_mismatch = util.as_int(checkpoint.get("changed_count"), 0) != snapshot["count"]

    tree_diff = bool(new_outside or hash_drift or unexplained_vanished or count_mismatch)
    evidence.update(
        {
            "tree_diff": tree_diff,
            "new_since_checkpoint": new_outside,
            "hash_drift": hash_drift,
            "vanished_since_checkpoint": unexplained_vanished,
            "vanished_detail": vanished_detail,
            "missing_claimed_files": missing_claimed,
            "count_mismatch": count_mismatch,
            "snapshot_trustworthy": bool(snapshot_hashes) and not capped,
        }
    )

    if tree_diff:
        details = []
        if new_outside:
            details.append("%d path(s) changed after the checkpoint" % len(new_outside))
        if hash_drift:
            details.append("%d file(s) edited after the checkpoint" % len(hash_drift))
        if unexplained_vanished:
            details.append("%d checkpoint path(s) no longer dirty" % len(unexplained_vanished))
        if count_mismatch:
            details.append("dirty file count differs from the checkpoint")
        checks.append(
            _check(
                "work tree vs checkpoint",
                "diff",
                "; ".join(details) + " - the previous agent did work the checkpoint never recorded",
            )
        )
        evidence["recovery_signals"] = details
    elif snapshot["count"]:
        checks.append(
            _check(
                "work tree vs checkpoint",
                "ok",
                "%d dirty path(s), identical to the checkpoint fingerprint"
                % snapshot["count"],
            )
        )
    else:
        checks.append(_check("work tree vs checkpoint", "ok", "work tree is clean"))

    if missing_claimed:
        checks.append(
            _check(
                "claimed files",
                "warn",
                "%d file(s) listed in changed_files no longer exist: %s"
                % (len(missing_claimed), ", ".join(missing_claimed[:3])),
            )
        )
    elif claimed:
        checks.append(_check("claimed files", "ok", "%d claimed file(s) exist" % len(claimed)))

    if snapshot["binary"]:
        checks.append(
            _check(
                "binary assets",
                "warn",
                "%d changed binary file(s); never resolve these with a text merge: %s"
                % (len(snapshot["binary"]), ", ".join(snapshot["binary"][:3])),
            )
        )

    lock = lock_state or {}
    if lock.get("state") in ("live_same", "live_foreign"):
        checks.append(
            _check(
                "lock",
                "warn",
                "%s holds %s (last heartbeat %s ago)"
                % (lock.get("agent") or "?", task.task_id, lock.get("age_human") or "?"),
            )
        )
    elif lock.get("state") == "stale":
        checks.append(
            _check(
                "lock",
                "warn",
                "stale lock from %s (%s); takeover is safe after explicit confirmation"
                % (lock.get("agent") or "?", ", ".join(lock.get("reasons") or [])),
            )
        )
    elif lock.get("state") == "none":
        checks.append(_check("lock", "warn", "no lock file; ownership is unknown"))
    else:
        checks.append(_check("lock", "ok", "lock is consistent"))

    mode, reasons = decide_mode(evidence)
    evidence["mode"] = mode
    evidence["reasons"] = reasons
    return evidence


def decide_mode(evidence: dict) -> Tuple[str, List[str]]:
    """Turn the evidence into one of the documented resume modes."""

    problems = evidence.get("problems") or []
    if problems:
        return "CORRUPT", ["task state is invalid: " + "; ".join(problems)]

    lock = evidence.get("lock") or {}
    if lock.get("state") == "live_foreign":
        return "BLOCKED", [
            "%s may still be running this task (lock heartbeat %s ago)"
            % (lock.get("agent") or "another agent", lock.get("age_human") or "?")
        ]

    if evidence.get("checkpoint_commit_exists") is False:
        return "DIVERGED", [
            "checkpoint commit does not exist here; the checkpoint belongs to another clone or was rewritten"
        ]

    if evidence.get("head_drift") and evidence.get("checkpoint_is_ancestor") is False:
        return "DIVERGED", [
            "checkpoint head is not an ancestor of HEAD: history was rewritten or diverged"
        ]

    if evidence.get("tree_diff"):
        return "RECOVERY", list(evidence.get("recovery_signals") or ["work tree differs from the checkpoint"])

    if evidence.get("head_drift"):
        return "CHECKPOINT_STALE", [
            "HEAD moved since the checkpoint but the work tree matches it; refresh the checkpoint before trusting its claims"
        ]

    if evidence.get("missing_claimed_files"):
        return "CHECKPOINT_STALE", ["files claimed as changed are missing from the work tree"]

    if not evidence.get("has_commit"):
        return "CHECKPOINT_STALE", [
            "repository has no baseline commit yet; branch and diff checks are degraded"
        ]

    return "CLEAN", ["work tree, branch and checkpoint agree"]


MODE_TEXT = {
    "CLEAN": "CLEAN RESUME",
    "CHECKPOINT_STALE": "CHECKPOINT STALE",
    "RECOVERY": "RECOVERY MODE",
    "DIVERGED": "HISTORY DIVERGED",
    "BLOCKED": "BLOCKED - ANOTHER AGENT MAY BE WORKING",
    "CORRUPT": "STATE CORRUPT",
    "NO_TASK": "NO ACTIVE TASK",
}

MODE_EXIT = {
    "CLEAN": 0,
    "CHECKPOINT_STALE": 0,
    "RECOVERY": 0,
    "DIVERGED": 2,
    "BLOCKED": 3,
    "CORRUPT": 2,
    "NO_TASK": 2,
}
