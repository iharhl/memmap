"""GNU ld map files (`-Wl,-Map=firmware.map`): the Memory Configuration table.

    Memory Configuration

    Name             Origin             Length             Attributes
    FLASH            0x08000000         0x00010000         xr
    *default*        0x00000000         0xffffffff

The values are the MEMORY block of the linker script, already worked out. A 64-bit ld prints
every address at 16 digits, even for a 32-bit target.
"""

from __future__ import annotations

import re

from ..regions import ImportProblem
from .model import LinkerRegion

NAME = "GNU ld map"
_TABLE = re.compile(r"^Memory Configuration[ \t]*$", re.MULTILINE)
_END = "Linker script and memory map"


def matches(text: str) -> bool:
    """Whether `text` is a GNU ld map: it has the Memory Configuration table."""
    return bool(_TABLE.search(text))


def read(text: str) -> list[LinkerRegion]:
    """The rows of the Memory Configuration table, except the catch-all *default* region."""
    found = _TABLE.search(text)
    assert found is not None
    regions = []
    for line in text[found.end() :].splitlines():
        if line.startswith(_END):
            break
        fields = line.split()
        if len(fields) < 3 or fields[0] in ("Name", "*default*"):
            continue
        try:
            origin, length = int(fields[1], 16), int(fields[2], 16)
        except ValueError:
            raise ImportProblem(f"cannot read the memory configuration line {line.strip()!r}") from None
        regions.append(LinkerRegion(fields[0], origin, length))
    return regions
