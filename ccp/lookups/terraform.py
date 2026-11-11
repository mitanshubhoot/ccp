"""Terraform resource blocks in a file's text (spec.md A-158, A-159).

A scanner, not a parser: it finds each top-level ``resource "type" "name" {`` and the line
its braces close on, skipping braces inside strings, comments and heredocs. That is enough
to say which block names a resource, to place a suggested change inside or after it, and to
check that a rendered change touches nothing else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEAD = re.compile(r'^\s*resource\s+"([A-Za-z0-9_]+)"\s+"([A-Za-z0-9_-]+)"\s*\{')
_HEREDOC = re.compile(r"<<-?([A-Z_][A-Z0-9_]*)")


@dataclass(frozen=True)
class Block:
    type: str
    name: str
    start_line: int  # 1-based, the `resource` line
    end_line: int  # 1-based, the closing brace's line
    text: str

    @property
    def address(self) -> str:
        return f"{self.type}.{self.name}"


def _depth_change(line: str, state: dict[str, object]) -> int:
    """The net braces on one line, outside strings and comments; heredocs are skipped
    whole (the state carries an open heredoc's terminator between lines)."""
    terminator = state.get("heredoc")
    if isinstance(terminator, str):
        if line.strip() == terminator:
            state["heredoc"] = None
        return 0
    depth = 0
    in_string = False
    i = 0
    while i < len(line):
        ch = line[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "#" or line.startswith("//", i):
            break
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "<" and (found := _HEREDOC.match(line, i)):
            state["heredoc"] = found.group(1)
            break
        i += 1
    return depth


def blocks(text: str) -> list[Block]:
    """Every top-level resource block, in file order. An unclosed block is left out."""
    lines = text.splitlines()
    out: list[Block] = []
    i = 0
    while i < len(lines):
        head = _HEAD.match(lines[i])
        if head is None:
            i += 1
            continue
        state: dict[str, object] = {}
        depth = 0
        end: int | None = None
        for j in range(i, len(lines)):
            depth += _depth_change(lines[j], state)
            if depth <= 0:
                end = j
                break
        if end is None:
            break
        out.append(
            Block(head.group(1), head.group(2), i + 1, end + 1, "\n".join(lines[i : end + 1]))
        )
        i = end + 1
    return out


def balanced(text: str) -> bool:
    """Braces outside strings, comments and heredocs close, and no string or heredoc is
    left open: the offline syntax check a rendered change must pass."""
    state: dict[str, object] = {}
    depth = 0
    for line in text.splitlines():
        if not state.get("heredoc") and _open_string(line):
            return False
        depth += _depth_change(line, state)
        if depth < 0:
            return False
    return depth == 0 and not state.get("heredoc")


def _open_string(line: str) -> bool:
    """A line that ends inside a string literal (quotes outside comments do not pair up)."""
    in_string = False
    i = 0
    while i < len(line):
        ch = line[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "#" or line.startswith("//", i):
            return False
        elif ch == "<" and _HEREDOC.match(line, i):
            return False
        i += 1
    return in_string


def mentioning(text: str, term: str, types: tuple[str, ...]) -> list[Block]:
    """The blocks of the given types whose text names the term (a bucket's or a user's
    name as a quoted value)."""
    quoted = f'"{term}"'
    return [b for b in blocks(text) if b.type in types and quoted in b.text]
