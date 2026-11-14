"""Read an nRF Connect SDK Partition Manager layout: the `partitions.yml` the build writes, or a
`pm_static.yml` that fixes the layout, which has the same format.

Each top-level key is a partition with `address`, `size`, `end_address` (exclusive) and
`region`, `flash_primary` when left out. A partition with `span` is a container: it holds the
partitions it names, such as `mcuboot_primary` holding `mcuboot_pad` and `app`. The file does
not say how big each region is, so a region is drawn from its lowest to its highest partition.
"""

from __future__ import annotations

import re
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from ..from_elf.regions import ImportProblem
from ..from_elf.sizes import approx_size
from .areas import Area, Device

PRIMARY = "flash_primary"
# Regions in the order they are drawn, with their names. Others follow by name.
REGIONS = {
    PRIMARY: "Flash",
    "external_flash": "External flash",
    "sram_primary": "SRAM",
    "otp": "OTP",
}
_KEY = re.compile(r"^[A-Za-z_][\w-]*:\s*(#.*)?$", re.MULTILINE)
_FIELD = re.compile(r"^\s+(address|end_address|size|region|span):", re.MULTILINE)
# An entry in flow style, on one line: `app: { address: 0x0, size: 0x1000 }`.
_FLOW = re.compile(r"^[A-Za-z_][\w-]*:\s*\{[^}\n]*\b(address|end_address|size|span)\s*:", re.MULTILINE)
# What a partition holds, by the name the nRF Connect SDK gives it.
ROLES = {
    "mcuboot": "MCUboot",
    "mcuboot_pad": "MCUboot image header",
    "mcuboot_primary": "MCUboot primary slot",
    "mcuboot_secondary": "MCUboot secondary slot",
    "mcuboot_scratch": "MCUboot scratch",
    "mcuboot_primary_app": "image in the primary slot",
    "b0": "nRF Secure Immutable Bootloader",
    "b0n": "nRF Secure Immutable Bootloader, network core",
    "s0": "NSIB slot 0",
    "s1": "NSIB slot 1",
    "provision": "NSIB keys",
    "app": "application",
    "tfm": "TF-M",
    "settings_storage": "Zephyr settings",
}
APP_HINT = (
    "the build places app in the space the other partitions leave, so an app entry in "
    "pm_static.yml can be stale: check the file's other partitions"
)
_FIELDS = ("address", "end_address", "size", "span")
_IMAGE_SLOT = re.compile(r"mcuboot_(primary|secondary)_(\d+)")


def is_partition_manager(text: str) -> bool:
    """Whether `text` looks like a Partition Manager file: top-level keys holding indented
    address, size, region or span fields."""
    return bool(_KEY.search(text) and _FIELD.search(text)) or bool(_FLOW.search(text))


def _role(name: str) -> str | None:
    """What a partition holds, from its name."""
    if name in ROLES:
        return ROLES[name]
    slot = _IMAGE_SLOT.fullmatch(name)
    if slot:
        return f"MCUboot {slot.group(1)} slot, image {slot.group(2)}"
    return None


def _int(entry: dict[str, Any], key: str, name: str) -> int | None:
    """A number field of an entry, or None when it is missing."""
    value = entry.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ImportProblem(f"{name}: {key} {value!r} is not a number")
    if value < 0:
        raise ImportProblem(f"{name}: {key} {value} is negative")
    return value


def _span(entry: dict[str, Any]) -> list[str]:
    """The partitions a container spans: a list of names, or one name."""
    span = entry.get("span")
    if isinstance(span, str):
        return [span]
    if isinstance(span, list):
        return [str(name) for name in span]
    return []


def _load(text: str) -> dict[str, dict[str, Any]]:
    """The file's partitions by name."""
    try:
        data = YAML(typ="safe", pure=True).load(text)
    except YAMLError as exc:
        problem = str(exc).strip().splitlines()[0] if str(exc).strip() else "invalid YAML"
        raise ImportProblem(f"the Partition Manager file is not valid YAML: {problem}") from None
    if not isinstance(data, dict):
        raise ImportProblem("the Partition Manager file is not a mapping of partitions")
    entries = {
        str(name): entry
        for name, entry in data.items()
        if isinstance(entry, dict) and any(field in entry for field in _FIELDS)
    }
    if not entries:
        raise ImportProblem(
            "no top-level entry has an address, size or span, so this is not a Partition "
            "Manager file",
            hint="pass build/partitions.yml or a pm_static.yml",
        )
    return entries


def _ranges(entries: dict[str, dict[str, Any]], comments: list[str]) -> dict[str, tuple[int, int]]:
    """The start and size of each partition. A container that gives no address or size gets the
    range of the partitions it spans."""
    ranges: dict[str, tuple[int, int]] = {}
    for name, entry in entries.items():
        address = _int(entry, "address", name)
        size = _int(entry, "size", name)
        end = _int(entry, "end_address", name)
        if size is None and address is not None and end is not None and end < address:
            raise ImportProblem(f"{name}: end_address 0x{end:x} is below its address 0x{address:x}")
        if size is None and address is not None and end is not None:
            size = end - address
        elif size is not None and address is not None and end is not None and end != address + size:
            comments.append(
                f"{name}: end_address 0x{end:x} is not address + size, 0x{address + size:x}; "
                "drawn by its size."
            )
        if address is not None and size is not None:
            ranges[name] = (address, size)

    def spanned(name: str, seen: tuple[str, ...]) -> tuple[int, int] | None:
        """The range of a container from the partitions it spans, which may be containers
        without a range of their own. None when none of them has one, or for a cycle."""
        if name in ranges:
            return ranges[name]
        if name in seen or name not in entries:
            return None
        parts = [found for part in _span(entries[name]) if (found := spanned(part, (*seen, name)))]
        if not parts:
            return None
        start = min(part_start for part_start, _ in parts)
        ranges[name] = (start, max(part_start + size for part_start, size in parts) - start)
        return ranges[name]

    for name in entries:  # containers left to the partitions they span
        if spanned(name, ()) is None:
            comments.append(f"{name} gives no address and size, so it is not drawn.")
    return ranges


def read_partition_manager(text: str) -> tuple[list[Device], list[str]]:
    """The regions of a Partition Manager file, each with its partitions, and header comments
    on what was not drawn."""
    entries = _load(text)
    comments: list[str] = []
    ranges = _ranges(entries, comments)
    by_region: dict[str, list[Area]] = {}
    devices_named: dict[str, set[str]] = {}
    for name, entry in entries.items():
        if name not in ranges:
            continue
        start, size = ranges[name]
        region = entry.get("region") or PRIMARY
        if not isinstance(region, str):
            raise ImportProblem(f"{name}: region {region!r} is not a name")
        container = bool(_span(entry))
        area = Area(name, name, start, size, name, _role(name), container)
        if name == "app":
            area.weak = APP_HINT
        by_region.setdefault(region, []).append(area)
        device = entry.get("device")
        if isinstance(device, str) and re.fullmatch(r"\w+", device):
            devices_named.setdefault(region, set()).add(device)
    order = [*(r for r in REGIONS if r in by_region), *sorted(r for r in by_region if r not in REGIONS)]
    devices: list[Device] = []
    for region in order:
        areas = by_region[region]
        drawn = [area for area in areas if area.size > 0]
        if not drawn:
            comments += [f"{area.where} has size 0, so it is not drawn." for area in areas]
            continue
        start = min(area.start for area in drawn)
        stop = max(area.stop for area in drawn)
        name = REGIONS.get(region, region)
        about = f"{region}: {approx_size(stop - start)} from 0x{start:x}"
        chips = devices_named.get(region)
        if chips:
            about += f", on {', '.join(sorted(chips))}"
        external = region == "external_flash"
        devices.append(Device(region, name, start, stop - start, areas, None, about + ".", external))
    if not devices:
        raise ImportProblem("the Partition Manager file has no partition with a size")
    return devices, comments
