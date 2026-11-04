"""Turn an ELF file into a memmap config: its allocated sections, placed in memory regions.

The ELF says where each section runs and where its bytes are loaded from, but not how big flash
and RAM are; those come from `--region` specs. The result is ordinary YAML for the user to edit.
"""

from __future__ import annotations

import itertools
import json
import math
import re
from dataclasses import dataclass, field

from .diagnostics import Diagnostic
from .elf import PT_LOAD, SHF_TLS, Elf, Section
from .units import diagnostic_address, format_address, parse_address, parse_size

_ID_RE = re.compile(r"^[a-z0-9_]+$")
_NOTE_NAMES = 6  # section names a note lists before "+N more"
FOLD_OVER = 8  # a region with more blocks than this folds its runs of small sections
_SHF_EXECINSTR = 0x4
COLUMN_WIDTH = 150  # px, room for section names such as .ARM.exidx and the usage title
_UNITS = ((1024**3, "GB"), (1024**2, "MB"), (1024, "KB"))


class ImportProblem(ValueError):
    """The ELF and the `--region` specs do not fit together, or a spec is malformed."""


@dataclass(frozen=True)
class MemorySpec:
    """One memory region from `--region`: an id, a range and a display name."""

    id: str
    start: int
    size: int
    name: str

    @property
    def end(self) -> int:
        """Last address, inclusive."""
        return self.start + self.size - 1


def parse_region_spec(text: str) -> MemorySpec:
    """Parse `id=start:size[:name]`, such as `flash=0x08000000:64K:Internal flash`."""
    region_id, separator, rest = text.partition("=")
    parts = rest.split(":", 2)
    if not separator or len(parts) < 2:
        raise ImportProblem(f"bad --region {text!r}, expected id=start:size[:name]")
    region_id = region_id.strip()
    if not _ID_RE.match(region_id):
        raise ImportProblem(f"bad --region id {region_id!r}, use lowercase letters, digits and _")
    try:
        start, size = parse_address(parts[0]), parse_size(parts[1])
    except ValueError as exc:
        raise ImportProblem(f"bad --region {text!r}: {exc}") from None
    name = parts[2].strip() if len(parts) == 3 and parts[2].strip() else region_id.upper()
    return MemorySpec(region_id, start, size, name)


@dataclass
class _Item:
    """A block to write: a section, a load image, a folded group of small sections, or free
    space. A group holds its sections as `children`; `expanded` gives it a detail column."""

    id: str
    name: str
    start: int
    size: int
    note: str | None = None
    free: bool = False
    children: list[_Item] = field(default_factory=list)
    expanded: bool = False

    @property
    def end(self) -> int:
        """Last address, inclusive."""
        return self.start + self.size - 1


@dataclass
class _Bucket:
    """The items placed in one memory region, or at the top level when `memory` is None."""

    memory: MemorySpec | None
    items: list[_Item] = field(default_factory=list)


@dataclass
class Imported:
    """The generated YAML and what the import had to say about the ELF."""

    text: str
    diagnostics: list[Diagnostic]


class _Ids:
    """Hands out unique region ids derived from section names."""

    def __init__(self, taken: list[str]) -> None:
        """Start with the ids that are already in use."""
        self.taken = set(taken)

    def make(self, text: str) -> str:
        """A fresh id from `text`: lowercase, non-alphanumerics as _, suffixed when taken."""
        base = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "section"
        candidate, number = base, 2
        while candidate in self.taken:
            candidate, number = f"{base}_{number}", number + 1
        self.taken.add(candidate)
        return candidate


def _check_memories(memories: list[MemorySpec]) -> None:
    """Reject duplicate ids and overlapping regions among the `--region` specs."""
    seen: set[str] = set()
    for memory in memories:
        if memory.id in seen:
            raise ImportProblem(f"--region {memory.id} is given twice")
        seen.add(memory.id)
    ordered = sorted(memories, key=lambda memory: memory.start)
    for low, high in itertools.pairwise(ordered):
        if high.start <= low.end:
            raise ImportProblem(f"--region {low.id} and {high.id} overlap")


def _wanted(section: Section) -> bool:
    """Whether a section takes space on the target. Thread-local .tbss only has a template."""
    if not section.allocated or section.size == 0:
        return False
    return not (section.nobits and section.flags & SHF_TLS)


def _approx_size(size: int) -> str:
    """A size rounded to one decimal, for usage figures: 1.6 KB, 59.5 KB, 512 B."""
    for factor, suffix in _UNITS:
        if size >= factor:
            return f"{size / factor:.1f}".removesuffix(".0") + f" {suffix}"
    return f"{size} B"


def _floor_size(size: int) -> str:
    """A free size: exact when some unit shows it with one decimal (1920 KB, 4.5 KB), else
    rounded down to three significant digits with ≈, so it never overstates (≈1.99 GB)."""
    for factor, suffix in _UNITS:
        value = size / factor
        if value >= 1 and round(value, 1) == value:
            return f"{value:.1f}".removesuffix(".0") + f" {suffix}"
    for factor, suffix in _UNITS:
        if size >= factor:
            value = size / factor
            decimals = 0 if value >= 100 else 1 if value >= 10 else 2
            floored = math.floor(value * 10**decimals) / 10**decimals
            text = f"{floored:.{decimals}f}"
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            exact = floored * factor == size
            return f"{'' if exact else '≈'}{text} {suffix}"
    return f"{size} B"


def _name_list(names: list[str]) -> str:
    """Section names for a note, cut short after _NOTE_NAMES."""
    shown = ", ".join(names[:_NOTE_NAMES])
    extra = len(names) - _NOTE_NAMES
    return f"{shown}, +{extra} more" if extra > 0 else shown


class _Importer:
    """Places the sections of one ELF into buckets, collecting diagnostics as it goes."""

    def __init__(self, elf: Elf, memories: list[MemorySpec], min_size: int) -> None:
        """Prepare one bucket per memory region and one for the top level."""
        self.elf = elf
        self.memories = memories
        self.min_size = min_size
        self.ids = _Ids([memory.id for memory in memories])
        self.buckets = [_Bucket(memory) for memory in sorted(memories, key=lambda m: m.start)]
        self.top = _Bucket(None)
        self.used: dict[str, int] = {}  # bytes taken in each memory region, before folding
        self.diagnostics: list[Diagnostic] = []

    def hex(self, value: int) -> str:
        """An address at this ELF's width, the way diagnostics print it."""
        return diagnostic_address(value, self.elf.bits)

    def bucket_for(self, item: _Item) -> _Bucket:
        """The memory region holding `item`, or the top level. An item that crosses a region
        edge means the `--region` specs are wrong."""
        for bucket in self.buckets:
            memory = bucket.memory
            assert memory is not None
            if memory.start <= item.start and item.end <= memory.end:
                return bucket
            if item.start <= memory.end and memory.start <= item.end:
                raise ImportProblem(
                    f"{item.name} ({self.hex(item.start)} - {self.hex(item.end)}) crosses the "
                    f"edge of --region {memory.id} ({self.hex(memory.start)} - "
                    f"{self.hex(memory.end)}); check its start and size"
                )
        if self.memories:
            self.diagnostics.append(
                Diagnostic(
                    "W110",
                    f"{item.name} ({self.hex(item.start)} - {self.hex(item.end)}) is outside "
                    "every --region, so it is placed at the top level",
                    hint="add a --region that covers it, or check the start and size of yours",
                    subject=item.id,
                )
            )
        return self.top

    def place(self) -> None:
        """Put every wanted section in a bucket where it runs, and each load image where it is
        stored."""
        wanted = [section for section in self.elf.sections if _wanted(section)]
        for section in wanted:
            item = _Item(self.ids.make(section.name), section.name, section.address, section.size)
            self.bucket_for(item).items.append(item)
        for image in self.load_images(wanted):
            self.bucket_for(image).items.append(image)

    def load_images(self, sections: list[Section]) -> list[_Item]:
        """One block per loadable segment that is stored somewhere other than where it runs,
        such as the initial values of .data. Startup code copies each in one piece."""
        images = []
        for segment in self.elf.segments:
            if segment.kind != PT_LOAD or segment.file_size == 0:
                continue
            if segment.physical == segment.virtual:
                continue
            members = [
                section
                for section in sections
                if not section.nobits
                and segment.offset <= section.offset < segment.offset + segment.file_size
            ]
            if not members:
                continue
            target = format_address(segment.virtual, self.elf.bits // 4)
            names = [section.name for section in members]
            if len(members) == 1:  # with several banks, a bare address leaves the reader to look
                bank = self.memory_at(segment.virtual)
                where = f"{bank.name} at {target}" if bank else target
                name, note = f"{names[0]} (load)", f"copied to {where}"
            else:
                name, note = self.image_name(segment.virtual, members), f"copied to {target}: "
                note += _name_list(names)
            images.append(
                _Item(self.ids.make(name), name, segment.physical, segment.file_size, note=note)
            )
        return images

    def memory_at(self, address: int) -> MemorySpec | None:
        """The `--region` that holds `address`, if any."""
        return next((m for m in self.memories if m.start <= address <= m.end), None)

    def image_name(self, address: int, members: list[Section]) -> str:
        """A name for a load image of several sections, after where it is copied to and what
        it holds: RAM init data, or DTIM code and data when it includes code."""
        what = "code and data" if any(s.flags & _SHF_EXECINSTR for s in members) else "init data"
        target = self.memory_at(address)
        return f"{target.name} {what}" if target else what.capitalize()

    def drop_overlaps(self, bucket: _Bucket) -> None:
        """Sort a bucket and leave out any item that overlaps the one before it."""
        kept: list[_Item] = []
        for item in sorted(bucket.items, key=lambda item: (item.start, -item.size)):
            if kept and item.start <= kept[-1].end:
                self.diagnostics.append(
                    Diagnostic(
                        "W111",
                        f"{item.name} overlaps {kept[-1].name}, so it is left out",
                        hint="overlays and NOLOAD sections can share addresses; add it by hand",
                        subject=item.id,
                    )
                )
                continue
            kept.append(item)
        bucket.items = kept

    def fold(self, bucket: _Bucket) -> None:
        """Group each run of two or more consecutive sections under `min_size` into one block
        that holds them as children. A gap of `min_size` or more ends a run, as it gets a Free
        block of its own. A region of FOLD_OVER blocks or fewer draws every section in place."""
        if len(bucket.items) <= FOLD_OVER:
            return
        folded: list[_Item] = []
        run: list[_Item] = []

        def flush() -> None:
            """Move the pending run to `folded`, grouped when it holds more than one item."""
            if len(run) == 1:
                folded.append(run[0])
            elif run:
                start, end = run[0].start, run[-1].end
                group = _Item(
                    self.ids.make("small_sections"),
                    f"{len(run)} small sections",
                    start,
                    end - start + 1,
                    note=_name_list([item.name for item in run]),
                    children=list(run),
                )
                folded.append(group)
            run.clear()

        for item in bucket.items:
            if item.size < self.min_size:
                if run and item.start - run[-1].end - 1 >= self.min_size:
                    flush()
                run.append(item)
            else:
                flush()
                folded.append(item)
        flush()
        bucket.items = folded

    def expand_groups(self) -> None:
        """Give every group a detail column, one level deep. When the memory regions are
        themselves zoomed out of a whole address space column, which happens when sections lie
        outside them, their groups stay one block with a note, as that would be a second level."""
        zoomed_regions = bool(self.top.items) or not self.buckets
        buckets = [self.top] if zoomed_regions else self.buckets
        for bucket in buckets:
            for item in bucket.items:
                if item.children:
                    item.expanded = True
                    item.note = None  # the detail column lists the sections

    def add_free(self, bucket: _Bucket) -> None:
        """Add a Free block for each gap of at least `min_size` bytes in a memory region.
        Smaller gaps stay unfilled, which draws them as an edge with both addresses."""
        memory = bucket.memory
        assert memory is not None
        threshold = max(self.min_size, 1)
        items: list[_Item] = []
        cursor = memory.start
        for item in [*bucket.items, None]:
            limit = item.start if item is not None else memory.end + 1
            if limit - cursor >= threshold:
                size = limit - cursor
                name = f"Free\n{_floor_size(size)}"
                items.append(_Item(self.ids.make(f"{memory.id}_free"), name, cursor, size, free=True))
            if item is not None:
                items.append(item)
                cursor = item.end + 1
        bucket.items = items

    def run(self) -> list[_Bucket]:
        """Place, clean up and complete every bucket. Returns the memory region buckets."""
        self.place()
        for bucket in [*self.buckets, self.top]:
            self.drop_overlaps(bucket)
        self.used = {
            bucket.memory.id: sum(item.size for item in bucket.items)
            for bucket in self.buckets
            if bucket.memory is not None
        }
        if self.min_size > 0:
            for bucket in [*self.buckets, self.top]:
                self.fold(bucket)
            self.expand_groups()
        for bucket in self.buckets:
            self.add_free(bucket)
        return self.buckets


def _quote(text: str) -> str:
    """A YAML double-quoted scalar. JSON strings are valid YAML."""
    return json.dumps(text, ensure_ascii=False)


def _comment_safe(text: str) -> str:
    """`text` for a YAML comment: anything that could end the line becomes ?."""
    return "".join(char if char.isprintable() else "?" for char in text)


def _size_literal(size: int) -> str:
    """A size in the shortest exact form: 64K, 1M, or plain bytes."""
    for factor, suffix in ((1024**3, "G"), (1024**2, "M"), (1024, "K")):
        if size % factor == 0:
            return f"{size // factor}{suffix}"
    return str(size)


def _address_literal(value: int, bits: int) -> str:
    """An address as zero-padded hex, such as 0x08000000."""
    return f"0x{value:0{bits // 4}X}"


def _item_fields(item: _Item, bits: int) -> list[str]:
    """The keys of one region, as `key: value` strings."""
    fields = [f"id: {_quote(item.id)}", f"name: {_quote(item.name)}"]
    if item.free:
        fields.append("kind: reserved")
    fields += [f"start: {_address_literal(item.start, bits)}", f"size: {_size_literal(item.size)}"]
    if item.note:
        fields.append(f"note: {_quote(item.note)}")
    return fields


def _item_lines(item: _Item, bits: int, indent: str) -> list[str]:
    """One region as list entry lines: a flow mapping on one line, or a block mapping with its
    children one per line."""
    fields = _item_fields(item, bits)
    if not item.children:
        return [f"{indent}- {{ " + ", ".join(fields) + " }"]
    lines = [f"{indent}- {fields[0]}"]
    lines += [f"{indent}  {field}" for field in fields[1:]]
    lines.append(f"{indent}  children:")
    for child in item.children:
        lines += _item_lines(child, bits, indent + "    ")
    return lines


def _usage_title(memory: MemorySpec, used: int) -> str:
    """A column title: the region name, then how much of it the sections use."""
    percent = round(100 * used / memory.size)
    if used < memory.size:
        percent = min(percent, 99)  # 99.6% is not full
    shown = "<1" if percent == 0 and used else str(percent)
    return f"{memory.name}\n{_approx_size(used)} of {_approx_size(memory.size)} ({shown}%)"


def _expansions(buckets: list[_Bucket], top: _Bucket) -> list[tuple[str, str]]:
    """Region ids with a detail column of their own, with their titles."""
    found = []
    for bucket in [*buckets, top]:
        for item in bucket.items:
            if item.expanded:
                where = bucket.memory.name if bucket.memory else "memory"
                found.append((item.id, f"{item.name} in {where}"))
    return found


def _write_yaml(
    source: str, bits: int, buckets: list[_Bucket], top: _Bucket, used: dict[str, int]
) -> str:
    """The config text, laid out like the examples: one line per section."""
    lines = [
        f"# Generated by `memmap from-elf` from {_comment_safe(source)}. Section ranges come from",
        "# the ELF and memory regions from --region. Rename, merge or annotate blocks freely.",
        "version: 1",
        "",
        "figure:",
        f"  title: {_quote(f'{source} memory map')}",
        "",
        "memory:",
    ]
    if bits == 64:
        lines.append("  address_bits: 64")
    lines += [
        "  fill_gaps: none                   # gaps under --min-size show as a split edge",
        "  regions:",
    ]
    entries: list[tuple[int, list[str]]] = []
    for bucket in buckets:
        memory = bucket.memory
        assert memory is not None
        block = [
            f"    - id: {_quote(memory.id)}",
            f"      name: {_quote(memory.name)}",
            f"      start: {_address_literal(memory.start, bits)}",
            f"      size: {_size_literal(memory.size)}",
        ]
        if bucket.items:
            block.append("      children:")
            for item in bucket.items:
                block += _item_lines(item, bits, "        ")
        entries.append((memory.start, block))
    entries += [(item.start, _item_lines(item, bits, "    ")) for item in top.items]
    for _, block in sorted(entries, key=lambda entry: entry[0]):
        lines += block

    lines += [
        "",
        "view:",
        "  defaults:",
        "    labels: { size: true }",
        "  columns:",
    ]
    expand: list[str] = []
    if buckets and not top.items:
        for bucket in buckets:
            memory = bucket.memory
            assert memory is not None
            title = _quote(_usage_title(memory, used[memory.id]))
            lines.append(
                f"    - {{ id: {_quote(memory.id + '_col')}, root: {_quote(memory.id)}, title: {title}, "
                f"width: {COLUMN_WIDTH} }}"
            )
    else:  # no regions, or sections outside them: show the whole address space
        lines.append(f'    - {{ id: all, root: "/", width: {COLUMN_WIDTH} }}')
        for bucket in buckets:
            memory = bucket.memory
            assert memory is not None
            title = _quote(_usage_title(memory, used[memory.id]))
            expand.append(f"    - {{ region: {_quote(memory.id)}, title: {title} }}")
    for region, title in _expansions(buckets, top):  # a column of its own, for its width
        root, column = _quote(region), _quote(f"{region}_col")
        lines.append(f"    - {{ id: {column}, root: {root}, width: {COLUMN_WIDTH} }}")
        expand.append(f"    - {{ region: {root}, to: {column}, title: {_quote(title)} }}")
    if expand:
        lines += ["  expand:", *expand]
    lines += [
        "",
        "style:",
        "  reserved: { show_label: true }  # names the Free blocks",
        "",
    ]
    return "\n".join(lines)


def import_elf(
    elf: Elf, source: str, memories: list[MemorySpec], min_size: int = 64
) -> Imported:
    """Build a config from an ELF. `source` names the ELF in the header comment and title.
    Raises ImportProblem when the sections and the memory regions do not fit together."""
    _check_memories(memories)
    if not elf.sections:
        raise ImportProblem(f"{source} has no section headers (stripped?), so nothing to draw")
    importer = _Importer(elf, memories, min_size)
    buckets = importer.run()
    if not any(bucket.items for bucket in [*buckets, importer.top]):
        raise ImportProblem(f"{source} has no allocated sections to draw")
    text = _write_yaml(source, elf.bits, buckets, importer.top, importer.used)
    return Imported(text, importer.diagnostics)
