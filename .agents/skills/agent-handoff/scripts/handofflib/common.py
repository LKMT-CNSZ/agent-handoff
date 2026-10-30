"""Shared CLI plumbing: exit codes, JSON mode, context bootstrap, auditing."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from typing import Callable, List, Optional

from . import SCHEMA_VERSION, __version__, config, history, report, util, yamlio
from .gitinfo import GitError, GitUnavailable

EXIT_OK = 0
EXIT_WARN = 1
EXIT_ERROR = 2
EXIT_BLOCKED = 3
EXIT_REFUSED = 4
EXIT_CHECKS_FAILED = 5

EXIT_MEANINGS = {
    EXIT_OK: "success",
    EXIT_WARN: "success with warnings",
    EXIT_ERROR: "error / nothing usable found",
    EXIT_BLOCKED: "blocked by another live agent",
    EXIT_REFUSED: "safety refusal",
    EXIT_CHECKS_FAILED: "verification failed",
}


class CliError(RuntimeError):
    def __init__(self, message: str, exit_code: int = EXIT_ERROR):
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)


# ---------------------------------------------------------------------------
# argument helpers
# ---------------------------------------------------------------------------
def add_common_arguments(parser: argparse.ArgumentParser, include_agent: bool = True) -> None:
    parser.add_argument("--repo", default=None, help="project root (default: search upward from CWD)")
    if include_agent:
        parser.add_argument(
            "--agent",
            default=None,
            help="agent identity, e.g. codex / dsh / claude (default: $AGENT_HANDOFF_AGENT, then config)",
        )
    parser.add_argument("--json", action="store_true", help="machine readable output on stdout")
    parser.add_argument("--quiet", action="store_true", help="suppress the context header")


def build_context(args, require_git: bool = True):
    try:
        context = config.make_context(
            start=args.repo,
            agent=getattr(args, "agent", None),
            require_git=require_git,
        )
    except config.ConfigError as exc:
        raise CliError(str(exc), EXIT_ERROR)
    if str(context.agent_source).startswith("config:"):
        context.warnings.append(
            "agent identity came from %s (%r); pass --agent NAME so the journal is attributable"
            % (context.agent_source, context.agent)
        )
    return context


def print_header(context, command: str) -> None:
    report.echo("agent-handoff v%s  |  %s" % (__version__, command))
    report.kv("project", context.root, 14)
    report.kv(
        "agent",
        "%s (%s)  machine %s  python %s"
        % (context.agent, context.agent_source, context.machine, sys.version.split()[0]),
        14,
    )
    for warning in context.warnings:
        report.echo("  ! %s" % warning)


def audit(context, command: str, argv: List[str], outcome: str, task_id: Optional[str] = None, detail: Optional[str] = None) -> None:
    try:
        history.append_command(context, command, argv, outcome, task_id=task_id, detail=detail)
    except OSError:
        pass


def emit_json(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()


def finish_cli(exit_code: int, message: Optional[str] = None) -> int:
    if message:
        report.echo("")
        report.echo(message)
    return exit_code


# ---------------------------------------------------------------------------
# scaffolding
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_TEXT = """# agent-handoff project configuration (V0.1)
# Long lived project rules live in AGENTS.md; dynamic task state lives in
# .agent-state/{{active,completed,failed,history,locks}}.
schema_version: "{schema}"

# Owner recorded when --agent is not passed.  Agents should always pass --agent.
default_agent: dsh

default_base_branch: main
branch_prefix: feature/

lock:
  # A lock whose heartbeat is older than this is stale.  Every checkpoint,
  # resume --take and finish refreshes the heartbeat.
  ttl_minutes: "90"

verification:
  # Hard limit for one declared verification command.
  timeout_seconds: "900"

git:
  # Tasks must not start here; feature branches are created from them.
  protected_branches: main master trunk

assets:
  # Placeholder for the future LFS / Perforce policy: doctor only reports.
  strategy: git-lfs
  large_file_mb: "25"

# Extensions that must never be resolved by a text merge.
binary_extensions:
{binary}
"""

STATE_README = """# .agent-state

This directory is the recovery index for the project.  It is *not* the truth:
the work tree and git are.  Read `.agents/skills/agent-handoff/SKILL.md` first.

```text
active/     one YAML file per unfinished task
completed/  finished tasks, kept forever
failed/     abandoned tasks with a recorded reason
history/    append only journals: <task>.jsonl and commands.jsonl
locks/      <task>.lock heartbeat files (machine local, not committed)
```

Commit `active/`, `completed/`, `failed/` and `history/` so any machine can
resume.  Do not commit `locks/`: they describe a running process on one machine.

Do not edit `active/*.yaml` by hand when `checkpoint.py` can do it for you.
"""


def scaffold_state(root: str) -> List[str]:
    created = config.ensure_state_dirs(root)
    state_dir = os.path.join(root, config.STATE_DIR_NAME)
    config_path = os.path.join(state_dir, config.CONFIG_NAME)
    if not os.path.isfile(config_path):
        binary = "\n".join("  - %s" % item for item in config.DEFAULT_BINARY_EXTENSIONS)
        body = DEFAULT_CONFIG_TEXT.format(schema=SCHEMA_VERSION, binary=binary)
        util.write_text(config_path, body)
        created.append(config_path)
    readme_path = os.path.join(state_dir, "README.md")
    if not os.path.isfile(readme_path):
        util.write_text(readme_path, STATE_README)
        created.append(readme_path)
    return created


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
def run(main: Callable[[], int], command: str) -> int:
    """Wrap a CLI body so every failure path is readable and non-zero."""

    util.configure_stdio()
    try:
        return int(main())
    except CliError as exc:
        sys.stderr.write("ERROR: %s\n" % exc.message)
        return exc.exit_code
    except (GitError, GitUnavailable) as exc:
        sys.stderr.write("GIT ERROR: %s\n" % exc)
        return EXIT_ERROR
    except (yamlio.YamlError, ValueError, OSError) as exc:
        sys.stderr.write("ERROR: %s\n" % exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        sys.stderr.write("INTERRUPTED: no further changes were written.\n")
        return EXIT_ERROR
    except Exception as exc:  # pragma: no cover - last resort
        sys.stderr.write("UNEXPECTED ERROR in %s: %s\n" % (command, exc))
        if os.environ.get("AGENT_HANDOFF_DEBUG"):
            traceback.print_exc()
        else:
            sys.stderr.write("set AGENT_HANDOFF_DEBUG=1 for a traceback\n")
        return EXIT_ERROR
