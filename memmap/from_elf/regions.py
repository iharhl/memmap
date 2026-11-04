"""Memory regions for an import: what `--region` gives, and how they are checked.

The ELF says where each section runs, not how big flash and RAM are. Regions come from
`--region` specs, from a linker map (see `linker`), or both.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

from ..diagnostics import Diagnostic
from ..units import parse_address, parse_size
from .sizes import address_literal, size_literal

ID_RE = re.compile(r"^[a-z0-9_]+$")


class ImportProblem(ValueError):
    """The ELF and the memory regions do not fit together, or a region is malformed. `hint`
    says what to give instead, when there is something to say."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        """Keep the message and the hint apart, as --json reports them apart."""
        super().__init__(message)
        self.hint = hint


def unique_id(text: str, taken: set[str], fallback: str) -> str:
    """An id from `text`: lowercase, non-alphanumerics as _, `fallback` when nothing is left,
    suffixed _2, _3 and so on when taken. Adds it to `taken`."""
    base = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or fallback
    candidate, number = base, 2
    while candidate in taken:
        candidate, number = f"{base}_{number}", number + 1
    taken.add(candidate)
    return candidate


def describe_region(name: str, start: int, size: int | None, bits: int) -> str:
    """A region for a message: `FLASH (0x08000000, 64K)`, or without the size when it has none."""
    where = address_literal(start, bits)
    return f"{name} ({where})" if size is None else f"{name} ({where}, {size_literal(size)})"


@dataclass(frozen=True)
class MemorySpec:
    """One memory region: an id, a range and a display name. `source` names the map it was
    read from, or is None for a `--region` spec. A region from a map is only drawn when it
    holds a section."""

    id: str
    start: int
    size: int
    name: str
    source: str | None = None

    @property
    def end(self) -> int:
        """Last address, inclusive."""
        return self.start + self.size - 1

    @property
    def optional(self) -> bool:
        """Whether the region is left without a column when it holds nothing."""
        return self.source is not None

    @property
    def label(self) -> str:
        """How messages name the region: `--region flash` or `FLASH in firmware.map`."""
        return f"--region {self.id}" if self.source is None else f"{self.name} in {self.source}"


@dataclass(frozen=True)
class LeftOut:
    """A map region the import does not draw, and why. Each one is noted in the config's
    header. One with a `hint` loses something, so it is also reported as W112."""

    id: str
    name: str
    start: int
    size: int | None
    source: str
    reason: str
    hint: str | None = None

    def note(self, bits: int) -> str:
        """The header comment, such as `Left out of fw.map: RAM (0x20000000, 20K): size 0.`"""
        described = describe_region(self.name, self.start, self.size, bits)
        return f"Left out of {self.source}: {described}: {self.reason}."

    def warning(self, bits: int) -> Diagnostic | None:
        """W112 for the region, or None when nothing in it is lost."""
        if self.hint is None:
            return None
        described = describe_region(self.name, self.start, self.size, bits)
        message = f"{described} in {self.source} is left out: {self.reason}"
        return Diagnostic("W112", message, hint=self.hint, subject=self.id)


def parse_region_spec(text: str) -> MemorySpec:
    """Parse `id=start:size[:name]`, such as `flash=0x08000000:64K:Internal flash`."""
    region_id, separator, rest = text.partition("=")
    parts = rest.split(":", 2)
    if not separator or len(parts) < 2:
        raise ImportProblem(f"bad --region {text!r}, expected id=start:size[:name]")
    region_id = region_id.strip()
    if not ID_RE.match(region_id):
        raise ImportProblem(f"bad --region id {region_id!r}, use lowercase letters, digits and _")
    try:
        start, size = parse_address(parts[0]), parse_size(parts[1])
    except ValueError as exc:
        raise ImportProblem(f"bad --region {text!r}: {exc}") from None
    name = parts[2].strip() if len(parts) == 3 and parts[2].strip() else region_id.upper()
    return MemorySpec(region_id, start, size, name)


def check_memories(memories: list[MemorySpec]) -> None:
    """Reject duplicate ids and overlapping regions."""
    seen: set[str] = set()
    for memory in memories:
        if memory.id in seen:
            raise ImportProblem(f"--region {memory.id} is given twice")
        seen.add(memory.id)
    ordered = sorted(memories, key=lambda memory: memory.start)
    for low, high in itertools.pairwise(ordered):
        if high.start <= low.end:
            raise ImportProblem(f"{low.label} and {high.label} overlap")
