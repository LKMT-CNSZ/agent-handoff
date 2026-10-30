"""Project discovery, configuration and the shared CLI context."""

from __future__ import annotations

import os
from typing import List, Optional

from . import SCHEMA_VERSION, util, yamlio
from .gitinfo import Git, GitUnavailable

STATE_DIR_NAME = ".agent-state"
SKILL_REL_PATH = os.path.join(".agents", "skills", "agent-handoff")
CONFIG_NAME = "config.yaml"

SUB_DIRS = ("active", "completed", "failed", "history", "locks")

# Defaults for the binary asset guard.  Extensions are intentionally about
# "cannot be merged as text", not about any specific engine.
DEFAULT_BINARY_EXTENSIONS = [
    "uasset", "umap", "ubulk", "uexp", "upk", "pak", "utoc", "ucas",
    "blend", "blend1", "fbx", "obj", "glb", "gltf", "abc",
    "psd", "tga", "exr", "hdr", "png", "jpg", "jpeg", "dds", "tif", "tiff",
    "wav", "mp3", "ogg", "flac", "bwav",
    "zip", "7z", "rar", "dll", "exe", "pdb", "lib", "so",
    "mp4", "mov", "avi", "webm",
]

DEFAULT_CONFIG = {
    "schema_version": str(SCHEMA_VERSION),
    "default_agent": "dsh",
    "default_base_branch": "main",
    "branch_prefix": "feature/",
    "lock": {"ttl_minutes": "90"},
    "verification": {"timeout_seconds": "900"},
    "git": {
        # Refuse to start a tracked task when the branch matches this pattern,
        # unless the caller passes --allow-protected-branch.
        "protected_branches": "main master trunk",
    },
    "assets": {
        "strategy": "git-lfs",  # informational placeholder for future P4/LFS policy
        "large_file_mb": "25",
    },
    "binary_extensions": list(DEFAULT_BINARY_EXTENSIONS),
}


class ConfigError(RuntimeError):
    pass


def _merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        elif value is not None:
            merged[key] = value
    return merged


def find_project_root(start: Optional[str] = None) -> Optional[str]:
    """Walk up from ``start`` looking for a ``.agent-state`` directory.

    The search never leaves the surrounding git work tree.  Without that
    boundary a nested repository (a scratch clone, a worktree, a submodule)
    would silently pick up the parent project's state - and write into it.
    """

    current = os.path.abspath(start or os.getcwd())
    if os.path.isfile(current):
        current = os.path.dirname(current)
    while True:
        if os.path.isdir(os.path.join(current, STATE_DIR_NAME)):
            return current
        if os.path.exists(os.path.join(current, ".git")):
            # Top of this work tree and still no state: it is not initialized.
            return None
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def find_git_root(start: Optional[str] = None) -> Optional[str]:
    current = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


class Context:
    def __init__(self, root: str, config: dict, git: Optional[Git], agent: str, agent_source: str):
        self.root = os.path.abspath(root)
        self.state_dir = os.path.join(self.root, STATE_DIR_NAME)
        self.config = config
        self.git = git
        self.agent = agent
        self.agent_source = agent_source
        self.machine = util.hostname()
        self.warnings: List[str] = []

    # -- paths -------------------------------------------------------------
    def state_path(self, *parts: str) -> str:
        return os.path.join(self.state_dir, *parts)

    def task_dir(self, status: str) -> str:
        return self.state_path(status)

    def active_dir(self) -> str:
        return self.state_path("active")

    def completed_dir(self) -> str:
        return self.state_path("completed")

    def failed_dir(self) -> str:
        return self.state_path("failed")

    def history_dir(self) -> str:
        return self.state_path("history")

    def lock_dir(self) -> str:
        return self.state_path("locks")

    def skill_dir_in_repo(self) -> str:
        return os.path.join(self.root, SKILL_REL_PATH)

    # -- config helpers ----------------------------------------------------
    def lock_ttl_minutes(self) -> int:
        lock = util.as_dict(self.config.get("lock"))
        return max(1, util.as_int(lock.get("ttl_minutes"), 90))

    def verification_timeout(self) -> int:
        verification = util.as_dict(self.config.get("verification"))
        return max(10, util.as_int(verification.get("timeout_seconds"), 900))

    def branch_prefix(self) -> str:
        return str(self.config.get("branch_prefix") or "feature/")

    def protected_branches(self) -> List[str]:
        git_cfg = util.as_dict(self.config.get("git"))
        raw = str(git_cfg.get("protected_branches") or "")
        names: List[str] = []
        for chunk in util.split_list(raw):
            for word in chunk.replace("\t", " ").split(" "):
                word = word.strip()
                if word and word not in names:
                    names.append(word)
        return names

    def binary_extensions(self) -> set:
        values = self.config.get("binary_extensions")
        if not isinstance(values, list) or not values:
            values = DEFAULT_BINARY_EXTENSIONS
        return {str(item).lower().lstrip(".") for item in values}

    def is_binary_path(self, path: str) -> bool:
        name = str(path).rsplit("/", 1)[-1]
        if "." not in name:
            return False
        return name.rsplit(".", 1)[-1].lower() in self.binary_extensions()

    def large_file_bytes(self) -> int:
        assets = util.as_dict(self.config.get("assets"))
        return max(1, util.as_int(assets.get("large_file_mb"), 25)) * 1024 * 1024

    def asset_strategy(self) -> str:
        assets = util.as_dict(self.config.get("assets"))
        return str(assets.get("strategy") or "none")


def load_config(root: str) -> tuple:
    """Return ``(config, problems)``; never raises on a broken config."""

    problems: List[str] = []
    path = os.path.join(root, STATE_DIR_NAME, CONFIG_NAME)
    config = dict(DEFAULT_CONFIG)
    if not os.path.isfile(path):
        problems.append("%s is missing; using built-in defaults" % CONFIG_NAME)
        return config, problems
    try:
        raw = yamlio.load(path)
    except (yamlio.YamlError, OSError) as exc:
        problems.append("%s cannot be parsed: %s" % (CONFIG_NAME, exc))
        return config, problems
    if not isinstance(raw, dict):
        problems.append("%s must be a mapping" % CONFIG_NAME)
        return config, problems
    try:
        config = _merge(DEFAULT_CONFIG, raw)
    except Exception as exc:  # pragma: no cover - defensive
        problems.append("%s could not be merged: %s" % (CONFIG_NAME, exc))
        return dict(DEFAULT_CONFIG), problems
    version = str(config.get("schema_version") or "")
    if version and version.split(".")[0] != "1":
        problems.append("config schema_version %r is newer than this skill supports" % version)
    return config, problems


def resolve_agent(explicit: Optional[str], config: dict) -> tuple:
    """Return ``(agent, source)``.  An explicit --agent always wins."""

    if explicit:
        return str(explicit).strip(), "argument"
    env = os.environ.get("AGENT_HANDOFF_AGENT", "").strip()
    if env:
        return env, "env:AGENT_HANDOFF_AGENT"
    default = str(config.get("default_agent") or "unknown-agent").strip()
    return default, "config:default_agent"


def ensure_state_dirs(root: str) -> List[str]:
    """Create the state skeleton; returns the paths that had to be created."""

    created = []
    state_dir = os.path.join(root, STATE_DIR_NAME)
    if not os.path.isdir(state_dir):
        os.makedirs(state_dir, exist_ok=True)
        created.append(state_dir)
    for name in SUB_DIRS:
        path = os.path.join(state_dir, name)
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
            created.append(path)
    return created


def make_context(
    start: Optional[str] = None,
    agent: Optional[str] = None,
    require_state: bool = True,
    require_git: bool = True,
) -> Context:
    """Build a CLI context.

    Raises ``ConfigError`` with an actionable message when the project layout is
    wrong.  Callers translate that into a non-zero exit + readable report.
    """

    root = find_project_root(start)
    if root is None:
        git_root = find_git_root(start)
        hint = ""
        if git_root:
            hint = (
                "\n  %s has a git repository but no %s directory.\n"
                "  Run: python .agents/skills/agent-handoff/scripts/doctor.py --fix"
                % (git_root, STATE_DIR_NAME)
            )
        raise ConfigError(
            "not inside an agent-handoff project: no %s directory found upward "
            "from %s%s" % (STATE_DIR_NAME, os.path.abspath(start or os.getcwd()), hint)
        )
    config, problems = load_config(root)
    git: Optional[Git] = None
    git_problem: Optional[str] = None
    try:
        candidate = Git(root)
        if candidate.is_repo():
            git = candidate
            detected = candidate.toplevel()
            if detected and os.path.realpath(detected) != os.path.realpath(root):
                git_problem = "project root %s is not the git top level %s" % (root, detected)
        else:
            git_problem = "%s is not inside a git work tree" % root
    except GitUnavailable as exc:
        git_problem = str(exc)
    if require_git and git is None:
        raise ConfigError(
            "git is required for this command but is not usable: %s" % (git_problem or "unknown")
        )
    resolved_agent, source = resolve_agent(agent, config)
    context = Context(root, config, git, resolved_agent, source)
    context.warnings.extend(problems)
    if git_problem:
        context.warnings.append(git_problem)
    if require_state and not os.path.isdir(os.path.join(root, STATE_DIR_NAME)):
        raise ConfigError("state directory disappeared while starting: %s" % root)
    return context
