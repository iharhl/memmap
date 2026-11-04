"""Arm linker map files (armlink `--map`, as Keil MDK writes them): the execution regions.

    Load Region LR_IROM1 (Base: 0x08000000, Size: 0x0000158c, Max: 0x00010000, ABSOLUTE)
      Execution Region ER_IROM1 (Base: 0x08000000, Size: 0x00001538, Max: 0x00010000, ABSOLUTE)
      Execution Region RW_IRAM1 (Exec base: 0x20000000, Load base: 0x08005434, Size: ...

Execution regions are where code and data run, and `Max` is the size the scatter file gives
them. armlink 5.05 writes `Base:`, and 5.06 `Exec base:` and `Load base:`; no map from the
armlink of Arm Compiler 6 is tested yet. A region placed with --ro-base or --rw-base has no
limit, which the map shows as a Max of all ones.
"""

from __future__ import annotations

import re

from .model import LinkerRegion

NAME = "armlink map"
_REGION = re.compile(
    r"^[ \t]*Execution Region[ \t]+(\S+)[ \t]+\((?:Exec base|Base):[ \t]*(0x[0-9a-fA-F]+),"
    r"[^)\n]*?\bMax:[ \t]*(0x[0-9a-fA-F]+)",
    re.MULTILINE,
)


def matches(text: str) -> bool:
    """Whether `text` is an armlink map with a memory map of the image."""
    return "Memory Map of the image" in text and bool(_REGION.search(text))


def read(text: str) -> list[LinkerRegion]:
    """Each execution region with its base and maximum size."""
    regions = []
    for match in _REGION.finditer(text):
        name, base, limit = match.group(1), int(match.group(2), 16), match.group(3)
        unlimited = set(limit[2:].lower()) == {"f"}
        regions.append(LinkerRegion(name, base, None if unlimited else int(limit, 16)))
    return regions
