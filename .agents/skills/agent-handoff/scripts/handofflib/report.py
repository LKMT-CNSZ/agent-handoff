"""Plain-text reporting helpers.

Deliberately ASCII and colourless: output goes into terminal transcripts, CI
logs and ``docs/`` evidence files where ANSI codes or box drawing characters are
noise.  Chinese text is emitted as-is (the state files are UTF-8).
"""

from __future__ import annotations

import os
import sys
from typing import Iterable, List, Optional, Sequence

from . import util

WIDTH = 78

STATUS_MARK = {
    "ok": "OK",
    "warn": "WARN",
    "diff": "DIFF",
    "fail": "FAIL",
    "info": "INFO",
    "skip": "SKIP",
    "na": "N/A",
}


def echo(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def rule(char: str = "-") -> None:
    echo(char * WIDTH)


def title(text: str) -> None:
    echo("=" * WIDTH)
    echo(text)
    echo("=" * WIDTH)


def section(text: str) -> None:
    echo("")
    echo(text)
    echo("-" * min(WIDTH, max(8, len(text))))


def kv(label: str, value: str, label_width: int = 20) -> None:
    label_text = str(label)
    if len(label_text) < label_width:
        label_text = label_text + " " * (label_width - len(label_text))
    echo("%s%s" % (label_text, value))


def bullet(items: Iterable[str], indent: int = 2, marker: str = "-") -> None:
    pad = " " * indent
    for item in items:
        echo("%s%s %s" % (pad, marker, item))


def numbered(items: Iterable[str], indent: int = 2) -> None:
    pad = " " * indent
    for index, item in enumerate(items, start=1):
        echo("%s%d. %s" % (pad, index, item))


def check_line(check: dict) -> None:
    mark = STATUS_MARK.get(str(check.get("status")), str(check.get("status")).upper())
    echo("  [%-4s] %-24s %s" % (mark, check.get("name"), check.get("detail")))


def table(headers: Sequence[str], rows: Sequence[Sequence[str]], widths: Optional[Sequence[int]] = None) -> None:
    columns = len(headers)
    if widths is None:
        widths = []
        for index in range(columns):
            longest = len(str(headers[index]))
            for row in rows:
                if index < len(row):
                    longest = max(longest, len(str(row[index])))
            widths.append(min(longest, 60))
    header_line = "  ".join(str(headers[i]).ljust(widths[i]) for i in range(columns))
    echo(header_line.rstrip())
    echo("  ".join("-" * widths[i] for i in range(columns)))
    for row in rows:
        cells = []
        for index in range(columns):
            value = str(row[index]) if index < len(row) else ""
            cells.append(value.ljust(widths[index]))
        echo("  ".join(cells).rstrip())


def relpath(path: str, root: str) -> str:
    try:
        return util.to_posix(os.path.relpath(path, root))
    except ValueError:
        return util.to_posix(path)


def exit_note(code: int, meaning: str) -> None:
    echo("")
    echo("EXIT %d (%s)" % (code, meaning))
