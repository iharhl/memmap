"""IAR ILINK map files: the address ranges of the placement summary.

    "A0":  place at address 0x800'0000 { ro section .intvec };
    "P1":  place in [from 0x800'0000 to 0x80f'ffff] { ro };
    "P2":  place in [from 0x2000'0000 to 0x2001'bfff] |
                    [from 0x2001'c000 to 0x2001'ffff] { rw, block CSTACK, block HEAP };

Each `place in` statement names the ranges of a region from the .icf file, worked out, but not
the region's name. So overlapping and adjoining ranges are merged into one region, called RAM
when read-write data or a stack goes there, ROM when only read-only content does, and Memory
when the statements do not say, as for named sections only. Newer versions write digit groups
with ', and some older ones write a range as [0x8000-0xffff].
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..regions import ImportProblem
from .model import LinkerRegion

NAME = "IAR map"
_SUMMARY = "*** PLACEMENT SUMMARY"
_NUMBER = r"(0x[0-9a-fA-F']+|[0-9][0-9']*)"
_RANGES = (
    (re.compile(rf"\[\s*from\s+{_NUMBER}\s+to\s+{_NUMBER}\s*\]"), False),
    (re.compile(rf"\[\s*from\s+{_NUMBER}\s+size\s+{_NUMBER}\s*\]"), True),
    (re.compile(rf"\[\s*{_NUMBER}\s*-\s*{_NUMBER}\s*\]"), False),
)
_PLACE = re.compile(r'"[^"]+":\s*place\s+(?:in|at start of|at end of)\s+([^{]*)\{([^}]*)\}')
_READ_ONLY = re.compile(r"^(ro|readonly)\b")
_READ_WRITE = re.compile(r"^(rw|readwrite|zi|zeroinit)\b|^block\s+\w*(STACK|HEAP)", re.IGNORECASE)
_NAMES = {"rw": "RAM", "ro": "ROM", "": "Memory"}


@dataclass
class _Range:
    """An address range from one statement, and what goes there: rw for read-write data or a
    stack, ro for read-only content only, or "" when the statement does not say."""

    start: int
    end: int
    kind: str


def _kind(items: list[str]) -> str:
    """What a statement places: rw, ro or ""."""
    if any(_READ_WRITE.match(item) for item in items):
        return "rw"
    if items and all(_READ_ONLY.match(item) for item in items):
        return "ro"
    return ""


def _merged_kind(first: str, second: str) -> str | None:
    """The kind of two ranges together, or None when they hold different kinds of content."""
    if first and second and first != second:
        return None
    return first or second


def matches(text: str) -> bool:
    """Whether `text` is an IAR ILINK map with a placement summary."""
    return "IAR ELF Linker" in text[:4096] and _SUMMARY in text


def _number(text: str) -> int:
    """A number as IAR writes it, with ' between digit groups."""
    digits = text.replace("'", "")
    try:
        return int(digits, 16) if digits[:2].lower() == "0x" else int(digits)
    except ValueError:
        raise ImportProblem(f"cannot read the address {text!r} in the placement summary") from None


def _ranges(text: str) -> list[_Range]:
    """The ranges of every `place in` and `place at start/end of` statement, except any that
    ends before it starts, as no linker writes one."""
    found = []
    block = text[text.index(_SUMMARY) + len(_SUMMARY) :]
    table = re.search(r"^\s+Section\s+Kind\s+Address", block, re.MULTILINE)
    for statement in block[: table.start() if table else len(block)].split(";"):
        statement = " ".join(statement.split())
        placed = _PLACE.search(statement)
        if not placed:
            continue
        where, content = placed.groups()
        kind = _kind([item.strip() for item in content.split(",") if item.strip()])
        for pattern, sized in _RANGES:
            for match in pattern.finditer(where):
                start, last = _number(match.group(1)), _number(match.group(2))
                end = start + last - 1 if sized else last
                if end >= start:
                    found.append(_Range(start, end, kind))
    return found


def read(text: str) -> list[LinkerRegion]:
    """The merged placement ranges as regions named RAM, ROM or Memory, numbered when
    repeated. Overlapping ranges always merge, adjoining ones when they hold the same kind."""
    merged: list[_Range] = []
    for item in sorted(_ranges(text), key=lambda item: (item.start, item.end)):
        last = merged[-1] if merged else None
        kind = _merged_kind(last.kind, item.kind) if last else None
        if last is not None and item.start <= last.end:
            last.end = max(last.end, item.end)
            last.kind = "rw" if kind is None else kind
        elif last is not None and item.start == last.end + 1 and kind is not None:
            last.end, last.kind = item.end, kind
        else:
            merged.append(_Range(item.start, item.end, item.kind))
    counts: dict[str, int] = {}
    regions = []
    for item in merged:
        base = _NAMES[item.kind]
        counts[base] = counts.get(base, 0) + 1
        name = base if counts[base] == 1 else f"{base} {counts[base]}"
        regions.append(LinkerRegion(name, item.start, item.end - item.start + 1))
    return regions
