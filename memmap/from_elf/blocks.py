"""The blocks an import writes, grouped by the memory region that holds them."""

from __future__ import annotations

from dataclasses import dataclass, field

from .regions import MemorySpec


@dataclass
class Item:
    """A block to write: a section, a load image, a folded group of small sections, or free
    space. A group holds its sections as `children`; `expanded` gives it a detail column."""

    id: str
    name: str
    start: int
    size: int
    note: str | None = None
    free: bool = False
    children: list[Item] = field(default_factory=list)
    expanded: bool = False

    @property
    def end(self) -> int:
        """Last address, inclusive."""
        return self.start + self.size - 1


@dataclass
class Bucket:
    """The items placed in one memory region, or at the top level when `memory` is None.
    `used` counts the bytes its sections take, before folding and free space."""

    memory: MemorySpec | None
    items: list[Item] = field(default_factory=list)
    used: int = 0

    @property
    def drawn(self) -> bool:
        """Whether the region gets a column: always for `--region`, for a region read from a
        linker file only when it holds a section."""
        return self.memory is not None and (bool(self.used) or not self.memory.optional)
