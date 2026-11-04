"""Turn the regions of a linker map into memory regions for the import.

Each region gets an id from its name, FLASH as flash. A `--region` with the same id replaces
it, and one with a new id is added. A map region that cannot be drawn is left out: one without
a size or of size 0, one that runs past the address space, and one that overlaps a `--region`
or a lower or larger region of the map, as when a script declares one RAM twice, for its
instruction and data bus.

Each region left out is noted in the config's header. It is also reported as W112, with what to
give instead, unless nothing in it is lost: a region of size 0, one inside the region it
overlaps, or one whose start lies in a drawn region. A region that holds no section is not left
out: it stays in `memory` without a column, like the IDT_LIST region Zephyr declares near the
top of memory for a build step of its own.

When nothing is left to draw, because the map lists no regions, as an lld map does, or none
that can be drawn, and no `--region` stands in, the import stops and says what to give instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ...diagnostics import Diagnostic
from ..regions import (
    ImportProblem,
    LeftOut,
    MemorySpec,
    check_memories,
    describe_region,
    unique_id,
)
from ..sizes import address_literal, size_literal
from . import gnu_map, iar_map
from .model import LLD_MAP, LinkerFile, LinkerRegion


@dataclass
class Resolved:
    """What the import gets from a map: the regions to draw, comment lines for the config's
    header, and W112 for each region left out that loses something."""

    memories: list[MemorySpec]
    notes: list[str] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)


def region_id(name: str, taken: set[str]) -> str:
    """An id for a region name: lowercase, non-alphanumerics as _, suffixed when taken."""
    return unique_id(name, taken, "region")


def _why_left_out(region: LinkerRegion, bits: int) -> str | None:
    """Why a region cannot be drawn, or None when it can."""
    if region.size is None:
        return "no size"
    if region.size == 0:
        return "size 0"
    if region.start + region.size > 1 << bits:
        return f"ends past the {bits}-bit address space"
    return None


def _empty(linker: LinkerFile) -> str:
    """Why a map lists no regions at all."""
    if linker.kind == LLD_MAP:
        return f"{linker.name} is an lld map, which lists no memory regions"
    if linker.kind == gnu_map.NAME:
        return f"{linker.name} lists no memory regions, as its linker script has no MEMORY block"
    return f"{linker.name} lists no memory regions"


def _spec(rid: str, start: int, bits: int) -> str:
    """The --region to give for a region whose start is known but not its size."""
    return f"--region {rid}={address_literal(start, bits)}:SIZE"


def _nothing_to_draw(linker: LinkerFile, unusable: list[LeftOut], bits: int) -> ImportProblem:
    """The problem of a map that leaves no region to draw, with the --region specs to give
    instead as its hint: one per start of a region without a size."""
    if not linker.regions:
        hint = "give each region with --region ID=START:SIZE"
        if linker.kind == LLD_MAP:
            hint += ", copying ORIGIN and LENGTH from the linker script's MEMORY block"
        return ImportProblem(_empty(linker), hint)
    starts: dict[int, str] = {}
    for entry in unusable:
        if entry.size is None:
            starts.setdefault(entry.start, entry.id)
    reasons = "; ".join(
        f"{describe_region(entry.name, entry.start, entry.size, bits)}: {entry.reason}"
        for entry in unusable
    )
    specs = " ".join(_spec(rid, start, bits) for start, rid in starts.items())
    hint = f"give them with --region, such as {specs}" if specs else (
        "give each region with --region ID=START:SIZE"
    )
    return ImportProblem(f"{linker.name} has no region memmap can draw ({reasons})", hint)


def _overlap_hint(memory: MemorySpec, other: MemorySpec, bits: int) -> str | None:
    """What to give for a map region left out as it overlaps `other`, or None when it lies
    inside `other`, so nothing in it is lost."""
    if other.start <= memory.start and memory.end <= other.end:
        return None
    if other.source is None:
        return f"to split {memory.name}, give one of the --region specs its id, {memory.id}"
    spec = f"{other.id}={address_literal(memory.start, bits)}:{size_literal(memory.size)}"
    return f"to draw it instead of {other.name}, give it with --region {spec}:{memory.name}"


def _without_overlaps(
    memories: list[MemorySpec], source: str, bits: int
) -> tuple[list[MemorySpec], list[LeftOut]]:
    """`memories` without each map region that overlaps a --region, or a map region that
    starts lower or, at the same start, is larger or listed first; and those left out."""
    kept = [memory for memory in memories if memory.source is None]
    left: list[LeftOut] = []
    for memory in sorted((m for m in memories if m.source), key=lambda m: (m.start, -m.size)):
        other = next((k for k in kept if memory.start <= k.end and k.start <= memory.end), None)
        if other is None:
            kept.append(memory)
            continue
        reason = f"overlaps {other.name if other.source else other.label}"
        hint = _overlap_hint(memory, other, bits)
        left.append(LeftOut(memory.id, memory.name, memory.start, memory.size, source, reason, hint))
    return [memory for memory in memories if memory in kept], left


def _with_hints(unusable: list[LeftOut], drawn: list[MemorySpec], bits: int) -> list[LeftOut]:
    """`unusable` with the hint for each W112. A region of size 0 holds nothing, and one whose
    start lies in a drawn region is drawn there, so neither gets one; nor does a second region
    at the same start, as one --region covers both."""
    warned: set[int] = set()
    hinted = []
    for entry in unusable:
        covered = any(memory.start <= entry.start <= memory.end for memory in drawn)
        if entry.size == 0 or covered or entry.start in warned:
            hinted.append(entry)
            continue
        warned.add(entry.start)
        hint = f"give it with {_spec(entry.id, entry.start, bits)}"
        if entry.size is not None:  # past the address space
            hint = f"check that the map belongs to this ELF, or {hint}"
        hinted.append(replace(entry, hint=hint))
    return hinted


def resolve_memories(linker: LinkerFile, specs: list[MemorySpec], bits: int) -> Resolved:
    """The memory regions of `linker`, with `specs` from --region replacing or adding to
    them. Raises ImportProblem when no region is left to draw, or when the specs repeat an id
    or overlap each other."""
    check_memories(specs)
    overrides = {spec.id: spec for spec in specs}
    taken: set[str] = set()
    memories: list[MemorySpec] = []
    unusable: list[LeftOut] = []
    for region in linker.regions:
        rid = region_id(region.name, taken)
        if rid in overrides:
            memories.append(overrides.pop(rid))
            continue
        reason = _why_left_out(region, bits)
        if reason is None:
            assert region.size is not None
            memories.append(MemorySpec(rid, region.start, region.size, region.name, linker.name))
            continue
        unusable.append(LeftOut(rid, region.name, region.start, region.size, linker.name, reason))
    memories += overrides.values()
    if not memories:
        raise _nothing_to_draw(linker, unusable, bits)
    memories, overlapping = _without_overlaps(memories, linker.name, bits)
    left = _with_hints(unusable, memories, bits) + overlapping
    notes = [] if linker.regions else [f"{_empty(linker)}."]
    if linker.kind == iar_map.NAME:
        notes.append(
            f"{linker.name} is an IAR map, which names no regions: these are its placement "
            "ranges, called ROM, RAM or Memory by what is placed there."
        )
    notes += [entry.note(bits) for entry in left]
    warnings = [entry.warning(bits) for entry in left]
    return Resolved(memories, notes, [warning for warning in warnings if warning])
