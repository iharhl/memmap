"""Turn an ESP-IDF partition table into a memmap config: one flash column holding the
bootloader, the partition table, every partition and the free space between them.

A table says where each partition is, but not how big the flash is, where the partition table
itself sits or where the ROM loads the bootloader from. Those come from the project's sdkconfig
or from options, and the config's header says which gave each one.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..diagnostics import Diagnostic
from ..from_elf.blocks import Bucket, Item
from ..from_elf.importer import Imported
from ..from_elf.regions import ImportProblem, MemorySpec, unique_id
from ..from_elf.sizes import approx_size, floor_size
from . import esp_idf
from .esp_idf import (
    APP,
    BOOTLOADER,
    DATA,
    PARTITION_TABLE,
    SECTOR,
    TABLE_SIZE,
    Partition,
)
from .sdkconfig import SdkConfig
from .writer import write_yaml

DEFAULT_TABLE_OFFSET = 0x8000  # CONFIG_PARTITION_TABLE_OFFSET's default
OTADATA_SIZE = 0x2000
NVS_MIN_SIZE = 0x3000  # a read-write NVS partition needs at least three sectors
BOOTLOADER_UNKNOWN_NOTE = "from 0x0, 0x1000 or 0x2000, by chip"


@dataclass(frozen=True)
class Setting:
    """A number the drawing needs, and what gave it, for the config's header."""

    value: int | None
    origin: str


@dataclass(frozen=True)
class Settings:
    """Where the flash, partition table and bootloader are, with their origins."""

    flash_size: Setting
    table_offset: Setting
    bootloader_offset: Setting
    recovery_offset: int | None = None
    secure_boot_v1: bool = False
    flash_size_text: str | None = None  # an sdkconfig value that is not a size, for the hint
    sdkconfig_name: str | None = None


def settings(
    flash_size: int | None,
    table_offset: int | None,
    sdkconfig: SdkConfig | None = None,
    sdkconfig_name: str = "sdkconfig",
    bootloader_offset: int | None = None,
) -> Settings:
    """The settings, from the options where given, else from the sdkconfig, else the defaults."""
    config = sdkconfig or SdkConfig()
    if flash_size is not None:
        flash = Setting(flash_size, "--flash-size")
    elif config.flash_size is not None:
        flash = Setting(config.flash_size, f"CONFIG_ESPTOOLPY_FLASHSIZE in {sdkconfig_name}")
    else:
        flash = Setting(None, "")
    if table_offset is not None:
        table = Setting(table_offset, "--table-offset")
    elif config.table_offset is not None:
        table = Setting(config.table_offset, f"CONFIG_PARTITION_TABLE_OFFSET in {sdkconfig_name}")
    else:
        table = Setting(DEFAULT_TABLE_OFFSET, "the default")
    if bootloader_offset is not None:
        bootloader = Setting(bootloader_offset, "--bootloader-offset")
    elif config.bootloader_offset is not None:
        bootloader = Setting(config.bootloader_offset, f"{config.bootloader_from} in {sdkconfig_name}")
    else:
        bootloader = Setting(None, "")
    return Settings(
        flash,
        table,
        bootloader,
        recovery_offset=config.recovery_offset,
        secure_boot_v1=config.secure_boot_v1,
        flash_size_text=config.flash_size_text,
        sdkconfig_name=sdkconfig_name if sdkconfig else None,
    )


def _hex(value: int) -> str:
    """An offset for a message: 0x9000."""
    return f"0x{value:x}"


def _check_settings(settings: Settings, flash_size: int) -> None:
    """Reject a partition table offset or bootloader offset the build would reject, or that
    leaves the bootloader or the partition table no room in the flash."""
    table = settings.table_offset.value
    assert table is not None
    if table % SECTOR:
        raise ImportProblem(
            f"the partition table offset {_hex(table)} from {settings.table_offset.origin} is not "
            "a multiple of 0x1000"
        )
    bootloader = settings.bootloader_offset.value
    if bootloader is not None and bootloader >= table:
        raise ImportProblem(
            f"the bootloader at {_hex(bootloader)} is not below the partition table at {_hex(table)}"
        )
    if table == 0:
        raise ImportProblem(
            f"the partition table at 0x0, from {settings.table_offset.origin}, leaves no room for "
            "the bootloader below it",
            hint="give the offset of CONFIG_PARTITION_TABLE_OFFSET, 0x8000 by default",
        )
    if table + TABLE_SIZE > flash_size:
        raise ImportProblem(
            f"the partition table at {_hex(table)} runs past the end of the "
            f"{approx_size(flash_size)} flash",
            hint="check the flash size and the partition table offset",
        )


def _flash_size(settings: Settings, near: str | None) -> int:
    """The flash size, or the problem that says how to give it. `near` names an sdkconfig
    found next to the table, which the hint then suggests."""
    if settings.flash_size.value is not None:
        return settings.flash_size.value
    if settings.flash_size_text is not None:
        message = (
            f"{settings.sdkconfig_name} sets CONFIG_ESPTOOLPY_FLASHSIZE to "
            f"{settings.flash_size_text!r}, which is not a size"
        )
        hint = "pass the size of the flash chip with --flash-size, such as --flash-size 4M"
    elif settings.sdkconfig_name is not None:
        message = f"{settings.sdkconfig_name} does not set CONFIG_ESPTOOLPY_FLASHSIZE"
        hint = "pass the size of the flash chip with --flash-size, such as --flash-size 4M"
    else:
        message = "the flash size is not known: the partition table does not record it"
        hint = "pass the project's sdkconfig with --sdkconfig, or the size with --flash-size 4M"
        if near:
            hint = f"pass --sdkconfig {near}, or the size with --flash-size, such as 4M"
    raise ImportProblem(message, hint=hint)


def _check_fit(partitions: list[Partition], table_offset: int, flash_size: int) -> None:
    """Stop on what cannot be drawn: partitions that overlap each other, the partition table
    or the bootloader below it, or run past the end of the flash."""
    table_end = table_offset + TABLE_SIZE
    last: Partition | None = None
    for partition in sorted(partitions, key=lambda p: (p.offset, p.stop)):
        if partition.size <= 0:
            raise ImportProblem(
                f"{partition.where} has size 0, which memmap cannot draw",
                hint="give it a size, or leave it out of the table",
            )
        fixed = partition.is_primary(BOOTLOADER) or partition.is_primary(PARTITION_TABLE)
        if not fixed and partition.offset < table_end:
            raise ImportProblem(
                f"{partition.where} starts at {_hex(partition.offset)}, below the end of the "
                f"partition table at {_hex(table_end)}",
                hint="pass the partition table offset the build uses, with --sdkconfig or "
                "--table-offset",
            )
        if last is not None and partition.offset < last.stop:
            raise ImportProblem(
                f"{partition.where} ({_hex(partition.offset)} - {_hex(partition.stop - 1)}) "
                f"overlaps {last.where} ({_hex(last.offset)} - {_hex(last.stop - 1)})"
            )
        if partition.stop > flash_size:
            raise ImportProblem(
                f"{partition.where} ends at {_hex(partition.stop - 1)}, past the end of the "
                f"{approx_size(flash_size)} flash",
                hint="check the flash size: CONFIG_ESPTOOLPY_FLASHSIZE, or --flash-size",
            )
        last = partition


def _rule_warnings(partitions: list[Partition], ids: dict[int, str], secure_v1: bool) -> list[Diagnostic]:
    """W113 for each rule of ESP-IDF's table check that the table breaks. The build stops on
    these, but the table can still be drawn. Some rules are newer than others, so a table an
    older ESP-IDF accepted can break one."""
    found: list[Diagnostic] = []

    def warn(partition: Partition, problem: str, hint: str, rejects: str = "") -> None:
        """Report one broken rule. `rejects` qualifies when the build rejects it."""
        found.append(
            Diagnostic(
                "W113",
                f"{partition.where} {problem}, which current ESP-IDF rejects{rejects}",
                hint=hint,
                subject=ids[id(partition)],
            )
        )

    for partition in partitions:
        align = esp_idf.alignment(partition.type)
        if partition.offset % align:
            fixed = -(-partition.offset // align) * align
            warn(
                partition,
                f"starts at {_hex(partition.offset)}, not on a {approx_size(align)} boundary",
                f"start it at {_hex(fixed)}, or leave its offset blank",
            )
        size_align = 0x10000 if secure_v1 else SECTOR
        if partition.type == APP and partition.size % size_align:
            warn(
                partition,
                f"has size {_hex(partition.size)}, not a multiple of {approx_size(size_align)}",
                f"round the size up to {_hex(-(-partition.size // size_align) * size_align)}",
            )
        if partition.type == DATA and partition.subtype in (0x00, 0x03) and "readonly" in partition.flags:
            warn(partition, f"is {partition.subtype_name} data marked readonly", "remove the readonly flag")
        nvs = partition.type == DATA and partition.subtype == esp_idf.SUBTYPES[DATA]["nvs"]
        if nvs and partition.size < NVS_MIN_SIZE and "readonly" not in partition.flags:
            warn(
                partition,
                f"is a read-write NVS partition of {approx_size(partition.size)}, less than 12 KB",
                "make it at least 0x3000, or flag it readonly",
            )
    for partition in partitions:
        if partition.subtype is None:
            warn(
                partition,
                f"has subtype {partition.subtype_text!r}",
                "check the spelling; a subtype a component registers is fine",
                " unless a component registers it",
            )
    names = Counter(partition.name for partition in partitions)
    for partition in partitions:
        if names[partition.name] > 1:
            warn(partition, f"shares its name with {names[partition.name] - 1} more", "give each partition its own name")
    for subtype, label in ((0x00, "otadata (data, ota)"), (0x90, "TEE otadata (data, tee_ota)")):
        matching = [p for p in partitions if p.type == DATA and p.subtype == subtype]
        for partition in matching:
            if len(matching) > 1:
                warn(partition, f"is one of {len(matching)} {label} partitions", "keep only one")
            elif partition.size != OTADATA_SIZE:
                warn(partition, f"is an {label} partition of {_hex(partition.size)} bytes", "make it 0x2000")
    return found


def _note(partition: Partition) -> str:
    """The text beside a partition: its type, subtype and flags. A subtype left blank in the
    CSV is left out, as its value depends on the ESP-IDF version."""
    subtype = [] if partition.subtype_text == "" else [partition.subtype_name]
    return ", ".join([partition.type_name, *subtype, *partition.flags])


def _items(
    partitions: list[Partition], settings: Settings, flash_size: int
) -> tuple[list[Item], dict[int, str]]:
    """The blocks of the flash column, in address order with free space between them, and the
    id of each partition's block by id(partition)."""
    taken = {"flash"}
    table_offset = settings.table_offset.value
    assert table_offset is not None
    bootloader = settings.bootloader_offset.value
    placed: list[Item] = []
    ids: dict[int, str] = {}
    boot: Item | None = None
    if not any(p.is_primary(BOOTLOADER) for p in partitions):
        start = bootloader if bootloader is not None else 0
        note = None if bootloader is not None else BOOTLOADER_UNKNOWN_NOTE
        boot = Item(
            unique_id("bootloader", taken, "bootloader"),
            "Bootloader",
            start,
            table_offset - start,
            note,
        )
        placed.append(boot)
    if not any(p.is_primary(PARTITION_TABLE) for p in partitions):
        table_id = unique_id("partition_table", taken, "table")
        placed.append(Item(table_id, "Partition table", table_offset, TABLE_SIZE))
    for partition in partitions:
        item_id = unique_id(partition.name, taken, "partition")
        item = Item(item_id, partition.name, partition.offset, partition.size, _note(partition))
        ids[id(partition)] = item_id
        placed.append(item)
        if partition.is_primary(BOOTLOADER):
            boot = item
    placed.sort(key=lambda item: item.start)
    items: list[Item] = []
    cursor = 0
    for block in [*placed, None]:
        stop = block.start if block is not None else flash_size
        if stop > cursor:
            if block is not None and block is boot and cursor == 0:  # the ROM never reads it
                name, free_id = "Reserved", unique_id("reserved", taken, "reserved")
            else:
                name, free_id = f"Free\n{floor_size(stop - cursor)}", unique_id("free", taken, "free")
            items.append(Item(free_id, name, cursor, stop - cursor, free=True))
        if block is not None:
            items.append(block)
            cursor = max(cursor, block.start + block.size)
    return items, ids


def _own_entry(setting: Setting, partitions: list[Partition], ptype: int, what: str) -> Setting:
    """The offset of a binary table's own primary entry of type `ptype`, which wins over the
    default. An option or sdkconfig that says otherwise belongs to another
    build, so that stops the import."""
    entry = next((p for p in partitions if p.is_primary(ptype)), None)
    if entry is None:
        return setting
    given = setting.value is not None and setting.origin != "the default"
    if given and setting.value != entry.offset:
        assert setting.value is not None
        raise ImportProblem(
            f"the table's own {what} entry is at {_hex(entry.offset)}, but {setting.origin} "
            f"says {_hex(setting.value)}",
            hint="check that the sdkconfig belongs to the build that wrote this table",
        )
    if given:
        return setting
    return Setting(entry.offset, "its own entry in the table")


def import_table(data: bytes, source: str, settings: Settings, near_sdkconfig: str | None = None) -> Imported:
    """The config for an ESP-IDF partition table, CSV or binary. `source` is the file name and
    `near_sdkconfig` an sdkconfig next to it, suggested when the flash size is missing."""
    table_offset = settings.table_offset
    binary = esp_idf.is_binary(data)
    if binary:
        partitions = esp_idf.read_binary(data)
        kind = "an ESP-IDF binary partition table"
    else:
        assert table_offset.value is not None
        partitions = esp_idf.read_csv(
            esp_idf.decode(data),
            table_offset.value,
            settings.bootloader_offset.value,
            settings.recovery_offset,
        )
        kind = "an ESP-IDF partition table"
    if binary:  # a CSV's own entries are placed at the offsets the settings give
        settings = Settings(
            settings.flash_size,
            _own_entry(table_offset, partitions, PARTITION_TABLE, "partition table"),
            _own_entry(settings.bootloader_offset, partitions, BOOTLOADER, "bootloader"),
            settings.recovery_offset,
            settings.secure_boot_v1,
            settings.flash_size_text,
            settings.sdkconfig_name,
        )
    table_offset = settings.table_offset
    flash_size = _flash_size(settings, near_sdkconfig)
    _check_settings(settings, flash_size)
    assert table_offset.value is not None
    _check_fit(partitions, table_offset.value, flash_size)
    items, ids = _items(partitions, settings, flash_size)
    # A binary table passed the check of the ESP-IDF version that built it, so only a CSV is
    # checked here: newer versions check more, and older ones read blank subtypes differently.
    diagnostics = [] if binary else _rule_warnings(partitions, ids, settings.secure_boot_v1)
    used = sum(item.size for item in items if not item.free)
    bucket = Bucket(MemorySpec("flash", 0, flash_size, "Flash"), items, used)
    origins = _origins(settings, flash_size)
    return Imported(write_yaml(source, kind, bucket, origins), diagnostics)


def _origins(settings: Settings, flash_size: int) -> list[str]:
    """Header lines saying where the flash size and offsets came from."""
    table = settings.table_offset
    bootloader = settings.bootloader_offset
    assert table.value is not None
    lines = [
        f"Flash size {approx_size(flash_size)}, from {settings.flash_size.origin}.",
        f"Partition table at {_hex(table.value)}, from {table.origin}.",
    ]
    if bootloader.value is not None:
        lines.append(f"Bootloader at {_hex(bootloader.value)}, from {bootloader.origin}.")
    else:
        lines.append(
            "Bootloader offset not known, so the Bootloader block starts at 0: pass "
            "--bootloader-offset or --sdkconfig to place it."
        )
    return lines
