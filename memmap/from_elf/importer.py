"""Turn an ELF file into a memmap config: its allocated sections, placed in memory regions.

The ELF says where each section runs and where its bytes are loaded from, but not how big flash
and RAM are; those come from `--region` specs or a linker map. The result is ordinary YAML for
the user to edit.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..diagnostics import Diagnostic
from ..units import diagnostic_address, format_address
from .blocks import Bucket, Item
from .elf import (
    PT_LOAD,
    SHF_EXECINSTR,
    SHF_TLS,
    SHF_WRITE,
    SHT_NOBITS,
    SHT_PROGBITS,
    Elf,
    Section,
    Segment,
)
from .regions import ImportProblem, LeftOut, MemorySpec, check_memories, unique_id
from .sizes import address_literal, floor_size
from .writer import write_yaml

_NOTE_NAMES = 6  # section names a note lists before "+N more"
FOLD_OVER = 8  # a region with more blocks than this folds its runs of small sections
UNALLOCATED_NOTE = "not allocated in the ELF, as a (COPY) section"


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
        return unique_id(text, self.taken, "section")


class _Crossing(Exception):
    """An item runs across the edge of a memory region, so the region is wrong."""

    def __init__(self, item: Item, memory: MemorySpec) -> None:
        """Keep the item and the region it crosses."""
        super().__init__(f"{item.name} crosses {memory.label}")
        self.item = item
        self.memory = memory


def _wanted(section: Section) -> bool:
    """Whether a section takes space on the target. Thread-local .tbss only has a template."""
    if not section.allocated or section.size == 0:
        return False
    return not (section.nobits and section.flags & SHF_TLS)


def _name_list(names: list[str]) -> str:
    """Section names for a note, cut short after _NOTE_NAMES."""
    shown = ", ".join(names[:_NOTE_NAMES])
    extra = len(names) - _NOTE_NAMES
    return f"{shown}, +{extra} more" if extra > 0 else shown


def _align_up(value: int, align: int) -> int:
    """`value` rounded up to a multiple of `align`."""
    return -(-value // align) * align


def _rw_zi_pairs(sections: list[Section]) -> list[tuple[Section, Section]]:
    """Pairs of a writable PROGBITS and a NOBITS section with the same name, the RW and ZI
    parts of one armlink execution region, such as two sections called RW_IRAM1."""
    by_name: dict[str, list[Section]] = {}
    for section in sections:
        by_name.setdefault(section.name.strip(), []).append(section)
    pairs = []
    for group in by_name.values():
        data = [s for s in group if not s.nobits]
        zero = [s for s in group if s.nobits]
        if len(data) == 1 and len(zero) == 1 and data[0].flags & SHF_WRITE:
            pairs.append((data[0], zero[0]))
    return pairs


def _display_names(sections: list[Section]) -> dict[Section, str]:
    """The name each section is drawn with: its own without surrounding blanks, which IAR
    leaves in, and with (RW) or (ZI) when an armlink region has both under one name."""
    names = {section: section.name.strip() for section in sections}
    for data, zero in _rw_zi_pairs(sections):
        names[data] += " (RW)"
        names[zero] += " (ZI)"
    return names


def _unpacked_sizes(sections: list[Section]) -> dict[Section, int]:
    """RAM sizes of RW data that armlink stored compressed. Its section header then gives the
    compressed size, and the ZI data of the same region starts further on than alignment
    explains: the RW data runs up to it."""
    sizes = {}
    for data, zero in _rw_zi_pairs(sections):
        packed_end = data.address + data.size
        if zero.address <= _align_up(packed_end, zero.align):
            continue
        others = [other for other in sections if other is not data]
        if any(packed_end <= other.address < zero.address for other in others):
            continue
        sizes[data] = zero.address - data.address
    return sizes


class _Importer:
    """Places the sections of one ELF into buckets, collecting diagnostics as it goes."""

    def __init__(self, elf: Elf, memories: list[MemorySpec], min_size: int) -> None:
        """Prepare one bucket per memory region and one for the top level."""
        self.elf = elf
        self.memories = memories
        self.min_size = min_size
        self.ids = _Ids([memory.id for memory in memories])
        self.buckets = [Bucket(memory) for memory in sorted(memories, key=lambda m: m.start)]
        self.top = Bucket(None)
        self.diagnostics: list[Diagnostic] = []

    def hex(self, value: int) -> str:
        """An address at this ELF's width, the way diagnostics print it."""
        return diagnostic_address(value, self.elf.bits)

    def bucket_for(self, item: Item) -> Bucket:
        """The memory region holding `item`, or the top level. Raises _Crossing for an item
        that crosses a region edge, as the region is then wrong."""
        for bucket in self.buckets:
            memory = bucket.memory
            assert memory is not None
            if memory.start <= item.start and item.end <= memory.end:
                return bucket
            if item.start <= memory.end and memory.start <= item.end:
                raise _Crossing(item, memory)
        if self.memories:
            from_map = all(memory.source for memory in self.memories)
            self.diagnostics.append(
                Diagnostic(
                    "W110",
                    f"{item.name} ({self.hex(item.start)} - {self.hex(item.end)}) is outside "
                    "every memory region, so it is placed at the top level",
                    hint="check that the map belongs to this ELF, or add a --region that "
                    "covers it"
                    if from_map
                    else "add a --region that covers it, or check the start and size of yours",
                    subject=item.id,
                )
            )
        return self.top

    def place(self) -> None:
        """Put every wanted section in a bucket where it runs, and each load image where it is
        stored."""
        wanted = [section for section in self.elf.sections if _wanted(section)]
        names = _display_names(wanted)
        unpacked = _unpacked_sizes(wanted)
        for section in wanted:
            name = names[section]
            size = unpacked.get(section, section.size)
            item = Item(self.ids.make(name), name, section.address, size)
            self.bucket_for(item).items.append(item)
        for item in self.unallocated(wanted):
            self.bucket_for(item).items.append(item)
        for image in self.load_images(wanted, set(unpacked)):
            self.bucket_for(image).items.append(image)

    def unallocated(self, wanted: list[Section]) -> list[Item]:
        """Sections the linker script reserved space for without allocating them, such as the
        heap and stacks the Pico SDK declares as (COPY): each with an address and a size that
        lie inside one memory region and overlap no allocated section. Debug and other
        non-memory sections sit at address 0, so a (COPY) section there is not told apart from
        them and is left out."""
        items = []
        for section in self.elf.sections:
            if section.allocated or not section.size or not section.address:
                continue
            if section.kind not in (SHT_PROGBITS, SHT_NOBITS):
                continue
            end = section.address + section.size - 1
            memory = self.memory_at(section.address)
            if memory is None or end > memory.end:
                continue
            if any(w.address <= end and section.address < w.address + w.size for w in wanted):
                continue
            name = section.name.strip()
            items.append(
                Item(self.ids.make(name), name, section.address, section.size, UNALLOCATED_NOTE)
            )
        return items

    def load_images(self, sections: list[Section], compressed: set[Section]) -> list[Item]:
        """One block per loadable segment that is stored somewhere other than where it runs,
        such as the initial values of .data. Startup code copies each in one piece. armlink
        instead puts RW data in the segment of the code before it, so a segment loaded where
        it runs can still hold sections stored apart, which get a block each."""
        images = []
        for segment in self.elf.segments:
            if segment.kind != PT_LOAD or segment.file_size == 0:
                continue
            members = [
                section
                for section in sections
                if not section.nobits
                and segment.offset <= section.offset < segment.offset + segment.file_size
            ]
            if not members:
                continue
            if segment.physical == segment.virtual:
                images += self.stored_apart(segment, members, compressed)
                continue
            target = format_address(segment.virtual, self.elf.bits // 4)
            names = [section.name.strip() for section in members]
            if len(members) == 1:  # with several banks, a bare address leaves the reader to look
                name, note = f"{names[0]} (load)", f"copied to {self.bank_at(segment.virtual)}"
            else:
                name, note = self.image_name(segment.virtual, members), f"copied to {target}: "
                note += _name_list(names)
            images.append(
                Item(self.ids.make(name), name, segment.physical, segment.file_size, note=note)
            )
        return images

    def stored_apart(
        self, segment: Segment, members: list[Section], compressed: set[Section]
    ) -> list[Item]:
        """The sections of a segment loaded where it runs whose bytes it loads somewhere other
        than their own address, as armlink does with RW data. `compressed` sections are stored
        packed, so their copy is smaller than the RAM they unpack to."""
        images = []
        for section in members:
            stored = segment.physical + section.offset - segment.offset
            if stored == section.address:
                continue
            size = min(section.size, segment.offset + segment.file_size - section.offset)
            name = f"{section.name.strip()} (load)"
            where = self.bank_at(section.address)
            verb = "compressed, unpacked to" if section in compressed else "copied to"
            images.append(Item(self.ids.make(name), name, stored, size, note=f"{verb} {where}"))
        return images

    def bank_at(self, address: int) -> str:
        """Where a load image is copied to: the memory region and address, or the address."""
        target = format_address(address, self.elf.bits // 4)
        bank = self.memory_at(address)
        return f"{bank.name} at {target}" if bank else target

    def memory_at(self, address: int) -> MemorySpec | None:
        """The memory region that holds `address`, if any."""
        return next((m for m in self.memories if m.start <= address <= m.end), None)

    def image_name(self, address: int, members: list[Section]) -> str:
        """A name for a load image of several sections, after where it is copied to and what
        it holds: RAM init data, or DTIM code and data when it includes code."""
        what = "code and data" if any(s.flags & SHF_EXECINSTR for s in members) else "init data"
        target = self.memory_at(address)
        return f"{target.name} {what}" if target else what.capitalize()

    def drop_overlaps(self, bucket: Bucket) -> None:
        """Sort a bucket and leave out any item that overlaps the one before it."""
        kept: list[Item] = []
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

    def fold(self, bucket: Bucket) -> None:
        """Group each run of two or more consecutive sections under `min_size` into one block
        that holds them as children. A gap of `min_size` or more ends a run, as it gets a Free
        block of its own. A region of FOLD_OVER blocks or fewer draws every section in place."""
        if len(bucket.items) <= FOLD_OVER:
            return
        folded: list[Item] = []
        run: list[Item] = []

        def flush() -> None:
            """Move the pending run to `folded`, grouped when it holds more than one item."""
            if len(run) == 1:
                folded.append(run[0])
            elif run:
                start, end = run[0].start, run[-1].end
                group = Item(
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
        zoomed_regions = bool(self.top.items) or not any(bucket.drawn for bucket in self.buckets)
        buckets = [self.top] if zoomed_regions else self.buckets
        for bucket in buckets:
            for item in bucket.items:
                if item.children:
                    item.expanded = True
                    item.note = None  # the detail column lists the sections

    def add_free(self, bucket: Bucket) -> None:
        """Add a Free block for each gap of at least `min_size` bytes in a memory region.
        Smaller gaps stay unfilled, which draws them as an edge with both addresses."""
        memory = bucket.memory
        assert memory is not None
        threshold = max(self.min_size, 1)
        items: list[Item] = []
        cursor = memory.start
        for item in [*bucket.items, None]:
            limit = item.start if item is not None else memory.end + 1
            if limit - cursor >= threshold:
                size = limit - cursor
                name = f"Free\n{floor_size(size)}"
                items.append(Item(self.ids.make(f"{memory.id}_free"), name, cursor, size, free=True))
            if item is not None:
                items.append(item)
                cursor = item.end + 1
        bucket.items = items

    def run(self) -> list[Bucket]:
        """Place, clean up and complete every bucket. Returns the memory region buckets."""
        self.place()
        for bucket in [*self.buckets, self.top]:
            self.drop_overlaps(bucket)
        for bucket in self.buckets:
            bucket.used = sum(item.size for item in bucket.items)
        if self.min_size > 0:
            for bucket in [*self.buckets, self.top]:
                self.fold(bucket)
            self.expand_groups()
        for bucket in self.buckets:
            self.add_free(bucket)
        return self.buckets


def _regions_from(memories: list[MemorySpec]) -> str:
    """Where the memory regions came from, for the header: a map, --region, or both."""
    sources = dict.fromkeys(memory.source or "--region" for memory in memories)
    return " and ".join(sorted(sources, key=lambda source: source == "--region")) or "--region"


def _without_crossed(crossing: _Crossing, bits: int) -> LeftOut:
    """The map region that `crossing` runs across, left out. Raises ImportProblem when it is
    a --region instead, as only the user can say which of its edges is wrong."""
    item, memory = crossing.item, crossing.memory
    if memory.source is None:
        raise ImportProblem(
            f"{item.name} ({diagnostic_address(item.start, bits)} - "
            f"{diagnostic_address(item.end, bits)}) crosses the edge of {memory.label} "
            f"({diagnostic_address(memory.start, bits)} - {diagnostic_address(memory.end, bits)})",
            hint="check its start and size",
        )
    span = f"{address_literal(item.start, bits)} - {address_literal(item.end, bits)}"
    start = address_literal(memory.start, bits) if memory.start <= item.start else "START"
    hint = f"give it with --region {memory.id}={start}:SIZE, large enough to hold {item.name}"
    reason = f"{item.name} ({span}) runs across its edge"
    return LeftOut(memory.id, memory.name, memory.start, memory.size, memory.source, reason, hint)


def import_elf(
    elf: Elf,
    source: str,
    memories: list[MemorySpec],
    min_size: int = 64,
    notes: tuple[str, ...] = (),
) -> Imported:
    """Build a config from an ELF. `source` names the ELF in the header comment and title, and
    `notes` are comment lines for the header. A map region that a section runs across is left
    out with W112, and the sections placed again. Raises ImportProblem when the sections and
    the memory regions do not fit together."""
    check_memories(memories)
    if not elf.sections:
        raise ImportProblem(f"{source} has no section headers (stripped?), so nothing to draw")
    left: list[LeftOut] = []
    while True:
        importer = _Importer(elf, memories, min_size)
        try:
            buckets = importer.run()
            break
        except _Crossing as crossing:
            left.append(_without_crossed(crossing, elf.bits))
            memories = [memory for memory in memories if memory is not crossing.memory]
    if not any(bucket.items for bucket in [*buckets, importer.top]):
        raise ImportProblem(f"{source} has no allocated sections to draw")
    header = (*notes, *(entry.note(elf.bits) for entry in left))
    text = write_yaml(source, elf.bits, buckets, importer.top, _regions_from(memories), header)
    warnings = [warning for entry in left if (warning := entry.warning(elf.bits))]
    return Imported(text, warnings + importer.diagnostics)
