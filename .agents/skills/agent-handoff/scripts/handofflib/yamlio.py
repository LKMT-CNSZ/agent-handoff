"""A deliberately small, strict YAML subset for ``.agent-state`` files.

Why not PyYAML?  The handoff skill has to run on a fresh Windows machine with
no ``pip install``.  This module therefore ships its own reader/writer for the
subset the schema actually needs:

* block mappings (arbitrary nesting)
* block sequences of scalars and of mappings (one level of item mapping)
* flow sequences of scalars: ``[a, b]``
* empty ``[]`` / ``{}``
* literal (``|``, ``|-``) and folded (``>``, ``>-``) block scalars
* comments, blank lines, ``---`` / ``...`` document markers
* single and double quoted scalars

Everything else is rejected with a line number instead of being silently
misread.  That matters: a state file is recovery data, and a silently
misparsed field could send an agent down the wrong path.

On load every scalar is returned as ``str`` (or ``None``).  No implicit
booleans / ints / dates, so YAML's famous type surprises (``no``, ``1.0``,
``2026-05-01``) cannot change meaning.  Callers convert explicitly.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

__all__ = ["YamlError", "dump", "dumps", "load", "loads"]


class YamlError(ValueError):
    def __init__(self, message: str, lineno: Optional[int] = None):
        self.lineno = lineno
        if lineno is not None:
            message = "line %d: %s" % (lineno, message)
        super().__init__(message)


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------
_BLOCK_INDICATORS = ("|", "|-", "|+", ">", ">-", ">+")
_NUMBER_RE = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")
_RESERVED = {
    "true", "false", "null", "~", "yes", "no", "on", "off", "y", "n",
    "True", "False", "Null", "NULL", "TRUE", "FALSE",
}
_LEADING_SPECIAL = set("-?:,[]{}#&*!|>'\"%@` ")


def _plain_ok(text: str) -> bool:
    if not text:
        return False
    if text != text.strip():
        return False
    if "\n" in text or "\t" in text:
        return False
    if text[0] in _LEADING_SPECIAL:
        return False
    if text.endswith(":"):
        return False
    if ": " in text or " #" in text:
        return False
    if text in _RESERVED:
        return False
    if _NUMBER_RE.match(text):
        return False
    for char in text:
        if ord(char) < 0x20 or ord(char) == 0x7F:
            return False
    return True


def _quote_key(text: str) -> str:
    if not text:
        raise YamlError("empty mapping key")
    if "\n" in text or "\r" in text:
        raise YamlError("mapping keys cannot contain newlines")
    if _plain_ok(text):
        return text
    return _double(text)


def _double(text: str) -> str:
    out = ['"']
    for char in text:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif char == "\r":
            out.append("\\r")
        elif ord(char) < 0x20:
            out.append("\\x%02x" % ord(char))
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _scalar(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(
            "YAML scalar must be str or None, got %s (%r). Convert explicitly "
            "so state files never change meaning between readers."
            % (type(value).__name__, value)
        )
    if _plain_ok(value):
        return value
    return _double(value)


def dumps(data: Any) -> str:
    lines: List[str] = []
    if not isinstance(data, dict):
        raise TypeError("top level must be a mapping, got %s" % type(data).__name__)
    _emit_mapping(data, 0, lines)
    return "\n".join(lines) + "\n"


def dump(data: Any, path: str) -> None:
    from .util import write_text

    write_text(path, dumps(data))


def _emit_mapping(data: dict, col: int, lines: List[str], first_prefix: Optional[str] = None) -> None:
    for index, (key, value) in enumerate(data.items()):
        if not isinstance(key, str):
            raise TypeError("mapping keys must be str, got %r" % (key,))
        prefix = first_prefix if (index == 0 and first_prefix is not None) else " " * col
        head = prefix + _quote_key(key) + ":"
        if isinstance(value, dict):
            if value:
                lines.append(head)
                _emit_mapping(value, col + 2, lines)
            else:
                lines.append(head + " {}")
        elif isinstance(value, list):
            if value:
                lines.append(head)
                _emit_sequence(value, col + 2, lines)
            else:
                lines.append(head + " []")
        else:
            if isinstance(value, str) and "\n" in value:
                stripped = value.rstrip("\n")
                if stripped != value:
                    lines.append(head + " |")
                else:
                    lines.append(head + " |-")
                for block_line in stripped.split("\n"):
                    lines.append((" " * (col + 2) + block_line) if block_line else "")
            else:
                text = _scalar(value)
                if text == "" and value is None:
                    lines.append(head)
                else:
                    lines.append(head + " " + text)


def _emit_sequence(items: list, col: int, lines: List[str]) -> None:
    pad = " " * col
    for item in items:
        if isinstance(item, dict):
            if not item:
                lines.append(pad + "- {}")
            else:
                _emit_mapping(item, col + 2, lines, first_prefix=pad + "- ")
        elif isinstance(item, list):
            raise TypeError("nested sequences are not part of the handoff schema")
        else:
            if isinstance(item, str) and "\n" in item:
                raise TypeError("multi-line values are not supported inside sequences")
            text = _scalar(item)
            if text == "" and item is None:
                lines.append(pad + "-")
            else:
                lines.append(pad + "- " + text)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
class _Parser:
    def __init__(self, text: str):
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if text.startswith("\ufeff"):
            text = text[1:]
        self.lines = text.split("\n")
        self.i = 0

    # -- cursor helpers ----------------------------------------------------
    def _eof(self) -> bool:
        return self.i >= len(self.lines)

    def _indent(self, line: str) -> int:
        count = 0
        for char in line:
            if char == " ":
                count += 1
            elif char == "\t":
                raise YamlError("tab characters are not allowed for indentation", self.i + 1)
            else:
                break
        return count

    @staticmethod
    def _is_skippable(line: str) -> bool:
        stripped = line.strip()
        return stripped == "" or stripped.startswith("#") or stripped == "---"

    def _skip_to_content(self) -> None:
        while not self._eof():
            line = self.lines[self.i]
            if line.strip() == "...":
                self.i = len(self.lines)
                return
            if self._is_skippable(line):
                self.i += 1
                continue
            return

    # -- block scalars -----------------------------------------------------
    def _block_scalar(self, parent_col: int, style: str) -> str:
        start = self.i
        collected: List[Tuple[int, str]] = []
        pending_blank: List[str] = []
        index = self.i
        dedent: Optional[int] = None
        while index < len(self.lines):
            line = self.lines[index]
            if line.strip() == "":
                pending_blank.append("")
                index += 1
                continue
            indent = self._indent(line)
            if indent <= parent_col:
                break
            if dedent is None:
                dedent = indent
            elif indent < dedent:
                break
            for blank in pending_blank:
                collected.append((dedent, ""))
            pending_blank = []
            collected.append((indent, line[indent:]))
            index += 1
        self.i = index
        if not collected:
            return ""
        if dedent is None:
            dedent = parent_col + 2
        if style.startswith(">"):
            text = _fold([body for _, body in collected])
        else:
            text = "\n".join(body for _, body in collected)
        if style.endswith("-"):
            return text.rstrip("\n")
        if style.endswith("+"):
            return text + "\n"
        return text.rstrip("\n") + "\n"

    # -- scalars -----------------------------------------------------------
    def _scalar(self, raw: str, lineno: int) -> Any:
        text = _strip_comment(raw, lineno).strip()
        if text == "":
            return None
        if text in ("~", "null", "Null", "NULL"):
            return None
        if text[0] == '"':
            return _unquote_double(text, lineno)
        if text[0] == "'":
            return _unquote_single(text, lineno)
        if text[0] == "[":
            return _flow_sequence(text, lineno)
        if text[0] == "{":
            if text.strip() == "{}":
                return {}
            raise YamlError("only the empty flow mapping {} is supported", lineno)
        if text[0] in ("&", "*", "!"):
            raise YamlError("anchors / aliases / tags are not supported: %r" % text, lineno)
        return text

    def _split_key(self, content: str, lineno: int) -> Tuple[str, str]:
        quote: Optional[str] = None
        index = 0
        while index < len(content):
            char = content[index]
            if quote:
                if quote == '"' and char == "\\":
                    index += 2
                    continue
                if char == quote:
                    if quote == "'" and index + 1 < len(content) and content[index + 1] == "'":
                        index += 2
                        continue
                    quote = None
                index += 1
                continue
            if char in ("'", '"'):
                quote = char
                index += 1
                continue
            if char == ":":
                following = content[index + 1: index + 2]
                if following in ("", " "):
                    key_text = content[:index].strip()
                    rest = content[index + 1:].strip()
                    if not key_text:
                        raise YamlError("empty mapping key", lineno)
                    key = self._scalar(key_text, lineno)
                    if key is None:
                        raise YamlError("empty mapping key", lineno)
                    return str(key), rest
            index += 1
        raise YamlError("expected 'key: value', got %r" % content.strip(), lineno)

    @staticmethod
    def _looks_like_key(text: str) -> bool:
        quote: Optional[str] = None
        index = 0
        while index < len(text):
            char = text[index]
            if quote:
                if quote == '"' and char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = None
                index += 1
                continue
            if char in ("'", '"'):
                quote = char
            elif char == ":" and text[index + 1: index + 2] in ("", " "):
                return index > 0
            index += 1
        return False

    # -- structure ---------------------------------------------------------
    def _value_after_key(self, rest: str, key_col: int, lineno: int) -> Any:
        rest = _strip_comment(rest, lineno).rstrip()
        if rest == "":
            self._skip_to_content()
            if self._eof():
                return None
            next_indent = self._indent(self.lines[self.i])
            if next_indent > key_col:
                return self._block(next_indent)
            return None
        if rest in _BLOCK_INDICATORS:
            return self._block_scalar(key_col, rest)
        return self._scalar(rest, lineno)

    def _block(self, col: int) -> Any:
        self._skip_to_content()
        if self._eof():
            return None
        line = self.lines[self.i]
        indent = self._indent(line)
        if indent != col:
            raise YamlError(
                "expected indentation %d, found %d" % (col, indent), self.i + 1
            )
        content = line[indent:]
        if content == "-" or content.startswith("- "):
            return self._sequence(col)
        return self._mapping(col)

    def _mapping(self, col: int) -> dict:
        result: dict = {}
        while True:
            self._skip_to_content()
            if self._eof():
                break
            line = self.lines[self.i]
            indent = self._indent(line)
            if indent < col:
                break
            if indent > col:
                raise YamlError("unexpected indentation %d" % indent, self.i + 1)
            content = line[indent:]
            if content == "-" or content.startswith("- "):
                raise YamlError("sequence item where a mapping key was expected", self.i + 1)
            lineno = self.i + 1
            key, rest = self._split_key(content, lineno)
            if key in result:
                raise YamlError("duplicate key %r" % key, lineno)
            self.i += 1
            result[key] = self._value_after_key(rest, col, lineno)
        return result

    def _sequence(self, col: int) -> list:
        items: list = []
        while True:
            self._skip_to_content()
            if self._eof():
                break
            line = self.lines[self.i]
            indent = self._indent(line)
            if indent < col:
                break
            if indent > col:
                raise YamlError("unexpected indentation %d" % indent, self.i + 1)
            content = line[indent:]
            if not (content == "-" or content.startswith("- ")):
                break
            lineno = self.i + 1
            after_dash = content[1:]
            stripped = after_dash.lstrip(" ")
            child_col = col + 1 + (len(after_dash) - len(stripped))
            rest = stripped.rstrip()
            self.i += 1
            rest_clean = _strip_comment(rest, lineno).rstrip()
            if rest_clean == "":
                self._skip_to_content()
                if not self._eof() and self._indent(self.lines[self.i]) > col:
                    items.append(self._block(self._indent(self.lines[self.i])))
                else:
                    items.append(None)
            elif rest_clean in _BLOCK_INDICATORS:
                items.append(self._block_scalar(col, rest_clean))
            elif self._looks_like_key(rest_clean):
                items.append(self._sequence_item_mapping(rest_clean, child_col, lineno))
            else:
                items.append(self._scalar(rest_clean, lineno))
        return items

    def _sequence_item_mapping(self, first_rest: str, child_col: int, lineno: int) -> dict:
        result: dict = {}
        key, rest = self._split_key(first_rest, lineno)
        result[key] = self._value_after_key(rest, child_col, lineno)
        while True:
            self._skip_to_content()
            if self._eof():
                break
            line = self.lines[self.i]
            indent = self._indent(line)
            if indent < child_col:
                break
            if indent > child_col:
                raise YamlError(
                    "unexpected indentation %d inside sequence item" % indent, self.i + 1
                )
            content = line[indent:]
            item_lineno = self.i + 1
            key2, rest2 = self._split_key(content, item_lineno)
            if key2 in result:
                raise YamlError("duplicate key %r" % key2, item_lineno)
            self.i += 1
            result[key2] = self._value_after_key(rest2, child_col, item_lineno)
        return result


def _fold(lines: List[str]) -> str:
    out: List[str] = []
    previous_blank = False
    for line in lines:
        if line == "":
            out.append("\n")
            previous_blank = True
            continue
        if out and not previous_blank:
            out.append(" ")
        out.append(line)
        previous_blank = False
    return "".join(out)


def _strip_comment(text: str, lineno: int) -> str:
    quote: Optional[str] = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if quote == '"' and char == "\\":
                index += 2
                continue
            if char == quote:
                if quote == "'" and text[index + 1: index + 2] == "'":
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in (" ", "\t")):
            return text[:index]
        index += 1
    return text


def _unquote_double(text: str, lineno: int) -> str:
    if len(text) < 2 or not text.endswith('"'):
        raise YamlError("unterminated double quoted string", lineno)
    body = text[1:-1]
    out: List[str] = []
    index = 0
    escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "0": "\0"}
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        index += 1
        if index >= len(body):
            raise YamlError("dangling escape in double quoted string", lineno)
        marker = body[index]
        if marker in escapes:
            out.append(escapes[marker])
            index += 1
            continue
        if marker == "x" and index + 2 < len(body):
            try:
                out.append(chr(int(body[index + 1: index + 3], 16)))
            except ValueError:
                raise YamlError("bad \\x escape", lineno)
            index += 3
            continue
        if marker == "u" and index + 4 < len(body):
            try:
                out.append(chr(int(body[index + 1: index + 5], 16)))
            except ValueError:
                raise YamlError("bad \\u escape", lineno)
            index += 5
            continue
        out.append(marker)
        index += 1
    return "".join(out)


def _unquote_single(text: str, lineno: int) -> str:
    if len(text) < 2 or not text.endswith("'"):
        raise YamlError("unterminated single quoted string", lineno)
    return text[1:-1].replace("''", "'")


def _flow_sequence(text: str, lineno: int) -> list:
    if not text.endswith("]"):
        raise YamlError("unterminated flow sequence", lineno)
    body = text[1:-1].strip()
    if not body:
        return []
    items: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    index = 0
    while index < len(body):
        char = body[index]
        if quote:
            current.append(char)
            if quote == '"' and char == "\\" and index + 1 < len(body):
                current.append(body[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
        elif char == ",":
            items.append("".join(current).strip())
            current = []
        elif char in ("[", "{"):
            raise YamlError("nested flow collections are not supported", lineno)
        else:
            current.append(char)
        index += 1
    items.append("".join(current).strip())
    parser = _Parser("")
    return [parser._scalar(item, lineno) for item in items if item != ""]


def loads(text: str) -> Any:
    parser = _Parser(text)
    parser._skip_to_content()
    if parser._eof():
        return {}
    indent = parser._indent(parser.lines[parser.i])
    if indent != 0:
        raise YamlError("top level must not be indented", parser.i + 1)
    first = parser.lines[parser.i]
    if not (first.strip() == "-" or first.strip().startswith("- ")):
        result = parser._mapping(0)
    else:
        result = parser._sequence(0)
    parser._skip_to_content()
    if not parser._eof():
        raise YamlError("trailing content after document", parser.i + 1)
    return result


def load(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return loads(handle.read())
