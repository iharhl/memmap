"""What memmap takes from a linker map: its memory regions."""

from __future__ import annotations

from dataclasses import dataclass, field

LLD_MAP = "lld map"


@dataclass(frozen=True)
class LinkerRegion:
    """One memory region, named as the map names it. `size` is None when the map sets no
    limit, as armlink does for a region placed without a scatter file."""

    name: str
    start: int
    size: int | None


@dataclass
class LinkerFile:
    """The regions read from one map. `kind` names its format, such as "GNU ld map". An lld
    map, or a GNU ld map of a script without a MEMORY block, has no regions."""

    name: str
    kind: str
    regions: list[LinkerRegion] = field(default_factory=list)
