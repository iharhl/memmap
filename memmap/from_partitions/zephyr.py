"""Read the flash partitions of a Zephyr build from its merged devicetree, `zephyr.dts`.

Every node with `compatible = "fixed-partitions"` lists the partitions of its parent, a flash
device. A partition's `reg` is an offset into that flash. The flash's own `reg` gives its base
address and size, as its parent's `#address-cells` and `#size-cells` say. A flash on a SPI or
QSPI bus, such as a `jedec,spi-nor` chip, has a chip select in `reg` instead, and its `size`
property gives its size in bits: it has no CPU address, so it is drawn from 0.
"""

from __future__ import annotations

import re

from ..from_elf.regions import ImportProblem
from ..from_elf.sizes import approx_size
from . import dts
from .areas import Area, Device
from .dts import Node, Reference

FIXED = "fixed-partitions"
SUBPARTITIONS = "fixed-subpartitions"  # a partition split into partitions, as on the nRF54H20
NO_PARTITIONS_HINT = (
    "the board defines no flash partitions. An nRF Connect SDK build that uses the Partition "
    "Manager lays out flash in build/partitions.yml instead: pass that"
)
# A NOR flash binding gives the chip's size in bits, in its `size` property.
_NOR = re.compile(r".*-nor")
_STM32_NOR = re.compile(r"st,stm32-[oqx]spi-nor")
# Node labels Zephyr gives MCUboot's partitions, after a prefix such as cpuapp_ on multi-core
# chips.
_BOOT = re.compile(r"(?:\w+_)?boot_partition")
_SLOT = re.compile(r"(?:\w+_)?slot(\d+)(_ns)?_partition")
_SCRATCH = re.compile(r"(?:\w+_)?scratch_partition")


def _number(cells: list[int]) -> int:
    """A number spread over 32-bit cells, most significant first."""
    value = 0
    for cell in cells:
        value = (value << 32) | cell
    return value


def _hex(value: int) -> str:
    """An address for a message: 0x8000."""
    return f"0x{value:x}"


def _reg(node: Node, address_cells: int, size_cells: int) -> tuple[int, int] | None:
    """The first address and size in a node's `reg`, or None when it has none or they do not
    fit the cell counts."""
    found = node.properties.get("reg")
    if found is None:
        return None
    try:
        cells = found.cells()
    except ValueError:
        return None
    if size_cells == 0 or len(cells) < address_cells + size_cells:
        return None
    return _number(cells[:address_cells]), _number(cells[address_cells : address_cells + size_cells])


def translate(address: int, node: Node) -> int:
    """An address in a node's reg, mapped through the `ranges` of the nodes above it as
    Zephyr maps it: up to the first parent without `ranges`. An empty `ranges` maps an
    address to itself. A partition's address is so an offset into its flash, and a flash's
    address one the CPU reads it at."""
    for _ in range(64):  # deeper than any devicetree
        parent = node.parent
        found = parent.properties.get("ranges") if parent is not None else None
        if parent is None or found is None:
            return address
        if found.values:
            try:
                cells = found.cells()
            except ValueError:
                return address
            child_cells = parent.cell_count("#address-cells", 2)
            above = parent.parent
            parent_cells = above.cell_count("#address-cells", 2) if above is not None else 2
            length_cells = parent.cell_count("#size-cells", 1)
            width = child_cells + parent_cells + length_cells
            for at in range(0, len(cells) - width + 1, width):
                child = _number(cells[at : at + child_cells])
                target = _number(cells[at + child_cells : at + child_cells + parent_cells])
                length = _number(cells[at + child_cells + parent_cells : at + width])
                if child <= address < child + length:
                    address = target + address - child
                    break
            else:
                return address
        node = parent
    return address


def _disabled(node: Node | None) -> bool:
    """Whether the node or one above it has status "disabled"."""
    while node is not None:
        status = node.properties.get("status")
        if status is not None and status.values == ["disabled"]:
            return True
        node = node.parent
    return False


def _flash(flash: Node) -> tuple[int, int | None, str]:
    """A flash device's base address and size, and where they came from. A flash without an
    address and size in `reg` starts at 0, and its `size` property gives its size in bits."""
    parent = flash.parent
    assert parent is not None
    found = _reg(flash, parent.cell_count("#address-cells", 2), parent.cell_count("#size-cells", 1))
    if found is not None and found[1] > 0:
        return translate(found[0], flash), found[1], "its reg"
    bits = flash.properties.get("size")
    if bits is not None and any(_NOR.fullmatch(name) for name in flash.compatible()):
        try:
            value = _number(bits.cells())
        except ValueError:
            value = 0
        if value and value % 8 == 0:
            return 0, value // 8, "its size property in bits, drawn from 0 as the CPU does not map it"
    if any(_STM32_NOR.fullmatch(name) for name in flash.compatible()):
        # Older STM32 QSPI boards give the window the chip is mapped at and its size in bytes,
        # as two cells of reg, though the bus has no size cells. The partitions count from 0.
        cells = _cells(flash, "reg")
        if len(cells) == 2 and cells[1] > 0:
            return 0, cells[1], "the size in its reg, drawn from 0 as partitions count from 0"
    return 0, None, ""


def _cells(node: Node, name: str) -> list[int]:
    """The numbers of a property, or none when it is missing or holds anything else."""
    found = node.properties.get(name)
    try:
        return found.cells() if found else []
    except ValueError:
        return []


def _erase_block(flash: Node) -> int | None:
    """The `erase-block-size` of a flash, or None."""
    found = flash.properties.get("erase-block-size")
    try:
        cells = found.cells() if found else []
    except ValueError:
        return None
    return cells[0] if len(cells) == 1 and cells[0] > 0 else None


def _role(labels: list[str]) -> str | None:
    """What MCUboot uses a partition for, from the node label Zephyr gives it."""
    for label in labels:
        if _BOOT.fullmatch(label):
            return "MCUboot"
        if _SCRATCH.fullmatch(label):
            return "MCUboot scratch"
        slot = _SLOT.fullmatch(label)
        if slot:
            number = int(slot.group(1))
            role = f"MCUboot {'secondary' if number % 2 else 'primary'} slot"
            image = number // 2
            if slot.group(2):
                return f"{role}, non-secure image"
            return role if image == 0 else f"{role}, image {image}"
    return None


def _first_string(node: Node, name: str) -> str | None:
    """The first string of a property, or None."""
    found = node.properties.get(name)
    try:
        strings = found.strings() if found else []
    except ValueError:
        return None
    return strings[0] if strings else None


def _code_partition(root: Node) -> Node | None:
    """The partition that `zephyr,code-partition` in /chosen names: where the app runs from."""
    chosen = root.child("chosen")
    found = chosen.properties.get("zephyr,code-partition") if chosen else None
    if found is None or len(found.values) != 1 or not isinstance(found.values[0], Reference):
        return None
    target = found.values[0].target
    if target.startswith("{"):
        path = target[1:-1]
        return next((node for node in root.walk() if node.path == path), None)
    return next((node for node in root.walk() if target in node.labels), None)


def _areas(
    partitions: Node, base: int, code: Node | None, comments: list[str], depth: int = 0
) -> list[Area]:
    """The areas of a fixed-partitions node in a flash at `base`, with the partitions of each
    fixed-subpartitions node inside them. A subpartition's `reg` is mapped to an offset into
    the flash through the `ranges` of the partition that holds it."""
    address_cells = partitions.cell_count("#address-cells", 2)
    size_cells = partitions.cell_count("#size-cells", 1)
    areas: list[Area] = []
    for node in partitions.children:
        label = node.labels[0] if node.labels else None
        where = f"{label or node.name} (devicetree line {node.line})"
        found = _reg(node, address_cells, size_cells)
        if found is None:
            comments.append(f"{where} has no address and size in reg, so it is not drawn.")
            continue
        offset, size = translate(found[0], node), found[1]
        name = _first_string(node, "label") or label or node.name.split("@")[0]
        notes = [role] if (role := _role(node.labels)) else []
        if node is code:
            notes.append("code partition")
        if "read-only" in node.properties:
            notes.append("read-only")
        nested = SUBPARTITIONS in node.compatible() and depth < 4
        area = Area(
            label or name, name, base + offset, size, where, ", ".join(notes) or None, nested
        )
        areas.append(area)
        if nested:
            areas += _areas(node, base, code, comments, depth + 1)
    return areas


def read_devicetree(text: str) -> tuple[list[Device], list[str]]:
    """The flash devices that hold fixed partitions, in file order, and header comments on
    what was not drawn."""
    root = dts.parse(text)
    code = _code_partition(root)
    devices: list[Device] = []
    comments: list[str] = []
    found = [node for node in root.walk() if FIXED in node.compatible()]
    if not found:
        raise ImportProblem("the devicetree has no fixed-partitions node", hint=NO_PARTITIONS_HINT)
    for partitions in found:
        flash = partitions.parent
        if flash is None or flash.parent is None:
            comments.append(f"{partitions.path} is not inside a flash node, so it is not drawn.")
            continue
        label = flash.labels[0] if flash.labels else flash.name.split("@")[0]
        if _disabled(flash):  # the partitions still say how the flash is laid out
            comments.append(f"{label} has status disabled, or a node above it has.")
        base, size, origin = _flash(flash)
        mapped = origin == "its reg"
        areas = _areas(partitions, base, code, comments)
        if not areas:
            comments.append(f"{label}, {flash.path}, has no partitions to draw.")
            continue
        if size is None:
            size = max(area.stop for area in areas) - base
            origin = "its last partition, as it gives no size in bytes"
        erase = _erase_block(flash)
        about = f"{label}, {flash.path}: {approx_size(size)} at {_hex(base)}, from {origin}"
        if erase:
            about += f"; {approx_size(erase)} erase blocks"
        devices.append(Device(label, label, base, size, areas, erase, about + ".", not mapped))
    if not devices:
        raise ImportProblem("no fixed-partitions node in the devicetree lists a partition")
    return devices, comments
