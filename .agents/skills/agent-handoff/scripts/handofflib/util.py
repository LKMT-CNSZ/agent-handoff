"""Small, dependency free helpers shared by all handoff CLIs."""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import socket
import sys
from typing import Iterable, Optional

UTC = _dt.timezone.utc


# --------------------------------------------------------------------------
# stdio
# --------------------------------------------------------------------------
def configure_stdio() -> None:
    """Never crash on a console that cannot encode Chinese."""

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------
def utc_now() -> _dt.datetime:
    return _dt.datetime.now(UTC)


def iso(moment: Optional[_dt.datetime] = None) -> str:
    moment = moment or utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(text: Optional[str]) -> Optional[_dt.datetime]:
    if not text:
        return None
    raw = str(text).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        moment = _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def age_seconds(stamp: Optional[str]) -> Optional[float]:
    moment = parse_iso(stamp)
    if moment is None:
        return None
    return (utc_now() - moment).total_seconds()


def humanize_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    seconds = float(seconds)
    if seconds < 0:
        return "in the future"
    minutes = seconds / 60.0
    if minutes < 1:
        return "%ds" % int(seconds)
    if minutes < 60:
        return "%dm" % int(minutes)
    hours = minutes / 60.0
    if hours < 48:
        return "%.1fh" % hours
    return "%.1fd" % (hours / 24.0)


def mtime_utc(path: str) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime.fromtimestamp(os.path.getmtime(path), UTC)
    except OSError:
        return None


# --------------------------------------------------------------------------
# host / process
# --------------------------------------------------------------------------
def hostname() -> str:
    try:
        return socket.gethostname() or "unknown-host"
    except Exception:
        return "unknown-host"


def pid_alive(pid: Optional[int]) -> Optional[bool]:
    """Best effort liveness probe.

    Returns True/False when the answer is trustworthy, None when unknown.
    On Windows os.kill(pid, 0) must never be used: Python maps it to
    TerminateProcess and would kill the other agent's process.
    """

    if not pid:
        return None
    pid = int(pid)
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                # 87 = ERROR_INVALID_PARAMETER -> no such process.
                return False if kernel32.GetLastError() == 87 else None
            try:
                code = ctypes.c_ulong(0)
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return int(code.value) == 259  # STILL_ACTIVE
                return None
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError:
        return None
    return True


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------
def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str, chunk: int = 1024 * 1024) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                block = handle.read(chunk)
                if not block:
                    break
                digest.update(block)
        return digest.hexdigest()
    except (OSError, IOError):
        return None


def file_size(path: str) -> Optional[int]:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def to_posix(path: str) -> str:
    return str(path).replace("\\", "/")


def relposix(path: str, root: str) -> str:
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return to_posix(path)
    return to_posix(rel)


def is_within(child: str, parent: str) -> bool:
    child_abs = os.path.abspath(child)
    parent_abs = os.path.abspath(parent)
    try:
        return os.path.commonpath([child_abs, parent_abs]) == parent_abs
    except ValueError:
        return False


def read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def write_text(path: str, text: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, path)


def append_text(path: str, text: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


# --------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------
def truncate(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def dedupe(items: Iterable[str]) -> list:
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def split_list(text: Optional[str]) -> list:
    """Split a comma / semicolon separated CLI value."""

    if not text:
        return []
    out = []
    for chunk in str(text).replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            out.append(chunk)
    return out


def as_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("true", "yes", "y", "1", "on"):
        return True
    if text in ("false", "no", "n", "0", "off"):
        return False
    return default


def as_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def as_list(value: object) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def as_list_entries(value: object) -> list:
    """Accept a list, a single mapping, or nothing; return a list of mappings."""

    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def plural(count: int, singular: str, plural_form: Optional[str] = None) -> str:
    word = singular if count == 1 else (plural_form or singular + "s")
    return "%d %s" % (count, word)


def python_exe() -> str:
    return sys.executable or "python"


def rmtree(path: str) -> None:
    """Remove a tree even when git objects are read-only (Windows)."""

    import shutil
    import stat

    def _on_error(function, target, _excinfo):
        try:
            os.chmod(target, stat.S_IWRITE)
            function(target)
        except OSError:
            pass

    if not os.path.isdir(path):
        return
    try:
        shutil.rmtree(path, onerror=_on_error)
    except TypeError:  # Python without onerror support
        shutil.rmtree(path, ignore_errors=True)
