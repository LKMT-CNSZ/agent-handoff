"""Thin, explicit git wrapper.

Design rules:

* read commands never take the index lock (``GIT_OPTIONAL_LOCKS=0``) so two
  agents can inspect the same repository at the same time;
* no destructive command is exposed here at all - the handoff CLIs only ever
  create branches, check out an explicitly named branch, add explicit paths and
  commit an explicitly stated message;
* every command's raw output is available to the caller so the report can quote
  evidence instead of summarising it away.
"""

from __future__ import annotations

import os
import subprocess
from typing import List, Optional, Sequence, Tuple

from . import util

# Status codes reported by ``git status --porcelain``.
STATUS_LABELS = {
    "??": "untracked",
    "!!": "ignored",
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "U": "unmerged",
    "UU": "unmerged",
    "AA": "unmerged",
    "DD": "unmerged",
}


class GitError(RuntimeError):
    pass


class GitUnavailable(GitError):
    pass


class GitResult:
    def __init__(self, args: Sequence[str], code: int, out: str, err: str):
        self.args = list(args)
        self.code = code
        self.out = out
        self.err = err

    @property
    def ok(self) -> bool:
        return self.code == 0

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return "GitResult(%r, %s)" % (self.args, self.code)


class Git:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    # -- plumbing ----------------------------------------------------------
    def run(self, args: Sequence[str], timeout: int = 120, check: bool = False) -> GitResult:
        env = dict(os.environ)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env.setdefault("GIT_PAGER", "cat")
        command = [
            "git",
            "-c",
            "core.quotepath=false",
            "-c",
            "color.ui=false",
            "--no-pager",
        ] + list(args)
        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                capture_output=True,
                timeout=timeout,
                env=env,
            )
        except FileNotFoundError:
            raise GitUnavailable(
                "git executable not found on PATH; install Git for Windows or add it to PATH."
            )
        except subprocess.TimeoutExpired:
            raise GitError("git %s timed out after %ss" % (" ".join(args), timeout))
        out = completed.stdout.decode("utf-8", "replace")
        err = completed.stderr.decode("utf-8", "replace")
        result = GitResult(args, completed.returncode, out, err)
        if check and not result.ok:
            raise GitError(
                "git %s failed (%s): %s"
                % (" ".join(args), result.code, (err or out).strip())
            )
        return result

    def text(self, args: Sequence[str], timeout: int = 120, default: str = "") -> str:
        result = self.run(args, timeout=timeout)
        if not result.ok:
            return default
        return result.out.strip()

    def lines(self, args: Sequence[str], timeout: int = 120) -> List[str]:
        result = self.run(args, timeout=timeout)
        if not result.ok or not result.out.strip():
            return []
        return [line for line in result.out.replace("\r\n", "\n").split("\n") if line.strip()]

    # -- repository state --------------------------------------------------
    def version(self) -> Optional[str]:
        try:
            return self.text(["--version"])
        except GitError:
            return None

    def is_repo(self) -> bool:
        result = self.run(["rev-parse", "--is-inside-work-tree"])
        return result.ok and result.out.strip() == "true"

    def toplevel(self) -> Optional[str]:
        result = self.run(["rev-parse", "--show-toplevel"])
        if not result.ok:
            return None
        return os.path.realpath(result.out.strip())

    def head(self) -> Optional[str]:
        result = self.run(["rev-parse", "--verify", "--quiet", "HEAD"])
        if not result.ok:
            return None
        sha = result.out.strip()
        return sha or None

    def head_short(self) -> Optional[str]:
        sha = self.head()
        return sha[:8] if sha else None

    def symbolic_branch(self) -> Optional[str]:
        result = self.run(["symbolic-ref", "--quiet", "--short", "HEAD"])
        if not result.ok:
            return None
        name = result.out.strip()
        return name or None

    def branch(self) -> Optional[str]:
        return self.symbolic_branch()

    def detached(self) -> bool:
        return self.head() is not None and self.symbolic_branch() is None

    def has_commit(self) -> bool:
        return self.head() is not None

    def commit_count(self) -> int:
        result = self.run(["rev-list", "--count", "HEAD"])
        if not result.ok:
            return 0
        try:
            return int(result.out.strip())
        except ValueError:
            return 0

    def commit_exists(self, sha: Optional[str]) -> bool:
        if not sha:
            return False
        return self.run(["cat-file", "-e", "%s^{commit}" % sha]).ok

    def branch_exists(self, name: Optional[str]) -> bool:
        if not name:
            return False
        return self.run(["rev-parse", "--verify", "--quiet", "refs/heads/%s" % name]).ok

    def any_ref_exists(self, name: Optional[str]) -> bool:
        if not name:
            return False
        return self.run(["rev-parse", "--verify", "--quiet", name]).ok

    def is_ancestor(self, older: Optional[str], newer: Optional[str]) -> Optional[bool]:
        if not older or not newer:
            return None
        if not (self.commit_exists(older) and self.commit_exists(newer)):
            return None
        result = self.run(["merge-base", "--is-ancestor", older, newer])
        if result.code == 0:
            return True
        if result.code == 1:
            return False
        return None

    def commits_between(self, older: str, newer: str = "HEAD", limit: int = 20) -> List[str]:
        if not (self.commit_exists(older) and self.commit_exists(newer)):
            return []
        return self.lines(["log", "--oneline", "--no-decorate", "-n", str(limit), "%s..%s" % (older, newer)])

    def commit_meta(self, sha: Optional[str] = None) -> dict:
        sha = sha or self.head()
        if not sha or not self.commit_exists(sha):
            return {}
        result = self.run(
            ["show", "-s", "--format=%H%x00%an%x00%ae%x00%aI%x00%s", sha]
        )
        if not result.ok:
            return {}
        parts = result.out.strip().split("\x00")
        keys = ["sha", "author", "email", "authored_at", "subject"]
        return dict(zip(keys, parts))

    # -- work tree ---------------------------------------------------------
    def status_entries(self, timeout: int = 120) -> List[dict]:
        """Parse ``git status --porcelain=v1 -z`` into ordered dicts."""

        result = self.run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=dirty"],
            timeout=timeout,
        )
        if not result.ok:
            raise GitError("git status failed: %s" % (result.err or result.out).strip())
        tokens = result.out.split("\x00")
        entries: List[dict] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if not token:
                index += 1
                continue
            if len(token) < 3:
                index += 1
                continue
            code = token[:2]
            path = token[3:]
            original = None
            if code[0] in ("R", "C") or code[1] in ("R", "C"):
                if index + 1 < len(tokens):
                    original = tokens[index + 1]
                    index += 2
                else:
                    index += 1
            else:
                index += 1
            entries.append(
                {
                    "code": code,
                    "path": util.to_posix(path),
                    "original": util.to_posix(original) if original else None,
                    "untracked": code == "??",
                    "staged": code[0] not in ("?", " "),
                    "label": STATUS_LABELS.get(code, STATUS_LABELS.get(code[0], "changed")),
                }
            )
        return entries

    def changed_paths(self) -> List[str]:
        entries = self.status_entries()
        paths = [entry["path"] for entry in entries]
        for entry in entries:
            if entry.get("original"):
                paths.append(entry["original"])
        return util.dedupe(sorted(paths))

    def untracked_paths(self) -> List[str]:
        return [entry["path"] for entry in self.status_entries() if entry["untracked"]]

    def diff_stat(self) -> str:
        for args in (["diff", "HEAD", "--stat"], ["diff", "--stat"], ["diff", "--cached", "--stat"]):
            result = self.run(args)
            if result.ok and result.out.strip():
                return result.out.strip()
        return ""

    def diff_names(self) -> List[str]:
        for args in (["diff", "HEAD", "--name-only", "-z"], ["diff", "--name-only", "-z"]):
            result = self.run(args)
            if result.ok:
                return [util.to_posix(item) for item in result.out.split("\x00") if item.strip()]
        return []

    def show_file(self, rev: str, path: str) -> Optional[str]:
        result = self.run(["show", "%s:%s" % (rev, path)])
        return result.out if result.ok else None

    # -- writes (all explicit, no force anywhere) --------------------------
    def create_branch(self, name: str, start: Optional[str] = None) -> GitResult:
        args = ["switch", "-c", name]
        if start:
            args.append(start)
        result = self.run(args, check=False)
        if not result.ok and "not a valid object name" in (result.err or ""):
            result = self.run(["checkout", "-b", name] + ([start] if start else []))
        return result

    def checkout(self, name: str) -> GitResult:
        result = self.run(["switch", name], check=False)
        if not result.ok and "is not a branch" in (result.err or ""):
            result = self.run(["checkout", name], check=False)
        return result

    def add(self, paths: Sequence[str]) -> GitResult:
        if not paths:
            raise GitError("refusing to run 'git add' with no explicit path")
        return self.run(["add", "--"] + list(paths))

    def commit(self, message: str, allow_empty: bool = False) -> GitResult:
        args = ["commit", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        return self.run(args)

    def check_ignore(self, paths: Sequence[str]) -> List[str]:
        if not paths:
            return []
        result = self.run(["check-ignore", "--no-index"] + ["--"] + list(paths))
        if result.code not in (0, 1):
            return []
        return [line.strip() for line in result.out.splitlines() if line.strip()]

    def ls_files(self) -> List[str]:
        result = self.run(["ls-files", "-z"])
        if not result.ok:
            return []
        return [util.to_posix(item) for item in result.out.split("\x00") if item.strip()]

    def tracked_size(self, path: str) -> Tuple[Optional[int], bool]:
        full = os.path.join(self.root, path.replace("/", os.sep))
        if os.path.isfile(full):
            return util.file_size(full), True
        return None, False

    def gitattributes_text(self) -> Optional[str]:
        path = os.path.join(self.root, ".gitattributes")
        return util.read_text(path)
