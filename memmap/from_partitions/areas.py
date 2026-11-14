"""Lay out Zephyr and Partition Manager partitions: one region per flash device or memory
region, partitions nested where one holds another, free space between them.

Both formats can list areas that overlap. A Partition Manager container (an entry with `span`)
holds the partitions it spans, and a devicetree may give two partitions the same range or put
one inside another. memmap draws a tree, so an area inside another becomes its child, areas with
the same range become one block, and an area that only partly overlaps another is left out:
with W114 for a partition, with a header comment for a container, which only groups others.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..diagnostics import Diagnostic
from ..from_elf.blocks import Bucket, Item
from ..from_elf.regions import ImportProblem, MemorySpec, unique_id
from ..from_elf.sizes import approx_size, floor_size

MAX_SECTORS = 4096  # more erase blocks than this are not worth a sectors entry


@dataclass
class Area:
    """A partition, or a Partition Manager container that groups partitions. `key` is the id to
    derive the block's id from, `where` names it in messages, and `start` is absolute. A `weak`
    partition is one the build places in the space the others leave, such as Partition
    Manager's `app`: it gives way when it partly overlaps another. `hint` then says why."""

    key: str
    name: str
    start: int
    size: int
    where: str
    note: str | None = None
    container: bool = False
    weak: str | None = None
    aliases: list[str] = field(default_factory=list)
    children: list[Area] = field(default_factory=list)

    @property
    def stop(self) -> int:
        """The first address after the area."""
        return self.start + self.size

    def holds(self, other: Area) -> bool:
        """Whether `other` lies inside this area."""
        return self.start <= other.start and other.stop <= self.stop

    def overlaps(self, other: Area) -> bool:
        """Whether the two share an address."""
        return self.start < other.stop and other.start < self.stop


@dataclass
class Device:
    """A flash device or memory region: one region and column of the drawing. `sector` is its
    erase block size, when known, and `about` a header line describing where it came from.
    `own_space` marks memory the CPU does not map, such as a flash chip on a SPI bus, whose
    addresses are offsets in the chip."""

    key: str
    name: str
    start: int
    size: int
    areas: list[Area]
    sector: int | None = None
    about: str | None = None
    own_space: bool = False

    @property
    def stop(self) -> int:
        """The first address after the device."""
        return self.start + self.size


@dataclass
class Region:
    """A device laid out: its blocks, as a bucket, its erase block size and its address space,
    None for the CPU's, with the reason it has one."""

    bucket: Bucket
    sector: int | None
    space: str | None = None
    space_reason: str = ""


@dataclass
class Layout:
    """The regions to write, what was left out and the comments saying why."""

    regions: list[Region]
    diagnostics: list[Diagnostic]
    comments: list[str]


def _hex(value: int) -> str:
    """An address for a message: 0x8000."""
    return f"0x{value:x}"


def _merge_same_ranges(areas: list[Area]) -> list[Area]:
    """One area per range: a partition before a container, then the first listed. The others
    are named in its note."""
    kept: dict[tuple[int, int], Area] = {}
    order: list[tuple[int, int]] = []
    for area in sorted(areas, key=lambda a: a.container):
        key = (area.start, area.size)
        if key in kept:
            kept[key].aliases.append(area.name)
        else:
            kept[key] = area
            order.append(key)
    return [kept[key] for key in order]


def _insert(area: Area, top: list[Area]) -> Area | None:
    """Put `area` in the tree under the smallest area that holds it, taking in the areas it
    holds. Returns the area it partly overlaps when it does not fit, and then changes nothing."""
    siblings = top
    while True:
        parent = next((other for other in siblings if other.holds(area)), None)
        if parent is None:
            break
        siblings = parent.children
    inside = [other for other in siblings if area.holds(other)]
    clash = next((o for o in siblings if o.overlaps(area) and o not in inside), None)
    if clash is not None:
        return clash
    area.children += inside
    area.children.sort(key=lambda a: a.start)
    siblings[:] = [other for other in siblings if other not in inside] + [area]
    siblings.sort(key=lambda a: a.start)
    return None


def _named(area: Area) -> str:
    """How a message names an area, with the areas merged into it: `a (and b)`."""
    if not area.aliases:
        return area.where
    return f"{area.where} (and {', '.join(area.aliases)}, with the same range)"


def nest(device: Device, region_id: str = "") -> tuple[list[Area], list[Diagnostic], list[str]]:
    """The device's areas as a tree, with W114 for each partition left out and a comment for
    each container left out. `region_id` is the subject of the W114s."""
    diagnostics: list[Diagnostic] = []
    comments: list[str] = []
    fitting: list[Area] = []
    for area in device.areas:
        if area.size <= 0:
            comments.append(f"{area.where} has size 0, so it is not drawn.")
        elif area.start < device.start or area.stop > device.stop:
            diagnostics.append(
                Diagnostic(
                    "W114",
                    f"{area.where} ({_hex(area.start)} - {_hex(area.stop - 1)}) lies outside "
                    f"{device.name} ({_hex(device.start)} - {_hex(device.stop - 1)}), left out",
                    hint="check the partition's reg against the size of its flash",
                    subject=region_id or None,
                )
            )
        else:
            fitting.append(area)
    merged = _merge_same_ranges(fitting)
    top: list[Area] = []
    # Partitions first, then containers, then the weak partitions, which give way to both.
    order = sorted(merged, key=lambda a: (a.weak is not None, a.container, -a.size, a.start))
    for area in order:
        clash = _insert(area, top)
        if clash is None:
            continue
        what = (
            f"{_named(area)} ({_hex(area.start)} - {_hex(area.stop - 1)}) partly overlaps "
            f"{clash.where} ({_hex(clash.start)} - {_hex(clash.stop - 1)})"
        )
        if area.container and area.weak is None:
            comments.append(f"{what}, so it is not drawn.")
        else:
            diagnostics.append(
                Diagnostic(
                    "W114",
                    f"{what}, left out",
                    hint=area.weak
                    or "memmap draws one area inside another, but not two that partly overlap",
                    subject=region_id or None,
                )
            )
    return top, diagnostics, comments


def _note(area: Area) -> str | None:
    """The text beside a block: its role, then the areas with the same range."""
    parts = [area.note] if area.note else []
    if area.aliases:
        parts.append("also " + ", ".join(area.aliases))
    return "; ".join(parts) or None


def _items(areas: list[Area], start: int, stop: int, taken: set[str]) -> list[Item]:
    """Blocks for `areas` between `start` and `stop`, with Free blocks in the gaps."""
    items: list[Item] = []
    cursor = start
    for area in [*areas, None]:
        until = area.start if area is not None else stop
        if until > cursor:
            name = f"Free\n{floor_size(until - cursor)}"
            items.append(Item(unique_id("free", taken, "free"), name, cursor, until - cursor, free=True))
        if area is None:
            break
        item = Item(unique_id(area.key, taken, "partition"), area.name, area.start, area.size, _note(area))
        if area.children:
            item.children = _items(area.children, area.start, area.stop, taken)
            item.expanded = True
        items.append(item)
        cursor = area.stop
    return items


def lay_out(devices: list[Device]) -> Layout:
    """Each device as a region of blocks, in the order given."""
    taken: set[str] = set()
    region_ids = [unique_id(device.key, taken, "flash") for device in devices]
    regions: list[Region] = []
    diagnostics: list[Diagnostic] = []
    comments: list[str] = []
    mapped: list[Device] = []
    for device, region_id in zip(devices, region_ids):
        if device.size <= 0:
            raise ImportProblem(f"{device.name} has size 0, which memmap cannot draw")
        space, reason = (region_id, "offsets in the chip, not CPU addresses") if device.own_space else (None, "")
        if space is None and any(device.start < other.stop and other.start < device.stop for other in mapped):
            space, reason = region_id, "overlaps another region"
            comments.append(f"{device.name} overlaps another region, so it gets an address space of its own.")
        if space is None:
            mapped.append(device)
        tree, found, notes = nest(device, region_id)
        diagnostics += found
        comments += notes
        items = _items(tree, device.start, device.stop, taken)
        used = sum(item.size for item in items if not item.free)
        memory = MemorySpec(region_id, device.start, device.size, device.name)
        sector = device.sector
        if sector is not None and (device.size % sector or device.size // sector > MAX_SECTORS):
            comments.append(
                f"{device.name} has no sectors: its {approx_size(sector)} erase blocks do not divide it "
                f"or are more than {MAX_SECTORS}, so partition edges are not checked against them."
            )
            sector = None
        regions.append(Region(Bucket(memory, items, used), sector, space, reason))
    return Layout(regions, diagnostics, comments)

