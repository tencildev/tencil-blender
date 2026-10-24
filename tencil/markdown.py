"""The agent's Markdown, laid out with Blender labels.

Labels have no bold or monospace, so the markup becomes layout: a line that's only bold
(or a `#` heading) is a heading with room above it, list items get a bullet and a
hanging indent, and the inline markers (`**`, backticks, link syntax) are dropped.
"""

from __future__ import annotations

import re
import textwrap
from typing import NamedTuple

import bpy

_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_CODE = re.compile(r"`([^`]+)`")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_BOLD_LINE = re.compile(r"^\*\*(.+?)\*\*:?\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")


class Line(NamedTuple):
    """One label's worth: a heading, a gap, or text with an optional list marker."""

    kind: str  # "heading", "gap" or "text"
    text: str = ""
    marker: str = ""  # "•" or "1." on an item's first line; " " on its continuation lines
    depth: int = 0


def plain(text: str) -> str:
    """Markdown without its markers, for one-line previews."""
    text = _LINK.sub(r"\1", text)
    text = _BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    text = _ITALIC.sub(r"\1", text)
    text = _CODE.sub(r"\1", text)
    return text.replace("```", "")


def lines(text: str, width: int) -> list[Line]:
    out: list[Line] = []
    in_code = False
    gap = False
    for raw in text.strip().splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            out.extend(Line("text", w, depth=1) for w in _wrap(line or " ", width - 3))
            continue
        if not line.strip():
            gap = True
            continue
        heading = _HEADING.match(line) or _BOLD_LINE.match(line)
        if heading:
            out.append(Line("heading", plain(heading.group(1)).rstrip(":")))
            gap = False
            continue
        if gap and out:
            out.append(Line("gap"))
        gap = False
        bullet = _BULLET.match(line)
        numbered = _NUMBERED.match(line)
        if bullet or numbered:
            match = bullet or numbered
            depth = len(match.group(1).expandtabs(4)) // 2
            marker = "•" if bullet else f"{numbered.group(2)}."
            body = plain(match.group(2) if bullet else numbered.group(3))
            for index, wrapped in enumerate(_wrap(body, width - 4 - 3 * depth)):
                out.append(Line("text", wrapped, marker if index == 0 else " ", depth))
        else:
            out.extend(Line("text", w) for w in _wrap(plain(line), width))
    return out


def draw(layout: bpy.types.UILayout, content: list[Line]) -> None:
    column = layout.column(align=True)
    for line in content:
        if line.kind == "heading":
            column.separator(factor=1.0)
            column.label(text=line.text)
        elif line.kind == "gap":
            column.separator(factor=0.6)
        else:
            row = column.row(align=True)
            row.scale_y = 0.8
            if line.depth:
                row.separator(factor=1.5 * line.depth)
            # Spaces under the marker, so an item's continuation lines hang under its text.
            prefix = f"{line.marker}  " if line.marker.strip() else "     " * bool(line.marker)
            row.label(text=prefix + line.text)


def _wrap(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, max(width, 20)) or [""]
