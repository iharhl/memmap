"""Read an ESP-IDF partition table: the CSV a project writes, or the binary the build makes of it.

Placement follows ESP-IDF's gen_esp32part.py, so that a CSV gives the offsets the build would
write into the binary table: a blank offset follows the previous partition, aligned to 64K for
an app and to 4K for anything else, starting after the partition table. Offsets are positions in
the flash chip, not CPU addresses.
"""

from __future__ import annotations

import codecs
import hashlib
import re
import struct
from dataclasses import dataclass

from ..from_elf.regions import ImportProblem

TABLE_SIZE = 0x1000  # the partition table takes one flash sector
SECTOR = 0x1000  # SPI flash erase sector
APP_ALIGN = 0x10000  # an app partition starts on a 64K boundary, as the MMU maps 64K pages

APP, DATA, BOOTLOADER, PARTITION_TABLE = 0x00, 0x01, 0x02, 0x03
TYPES = {"app": APP, "data": DATA, "bootloader": BOOTLOADER, "partition_table": PARTITION_TABLE}
PRIMARY = 0x00  # the subtype of the bootloader and partition table the ROM boots from
SUBTYPES: dict[int, dict[str, int]] = {
    BOOTLOADER: {"primary": 0x00, "ota": 0x01, "recovery": 0x02},
    PARTITION_TABLE: {"primary": 0x00, "ota": 0x01},
    APP: {
        "factory": 0x00,
        "test": 0x20,
        **{f"ota_{slot}": 0x10 + slot for slot in range(16)},
        **{f"tee_{slot}": 0x30 + slot for slot in range(2)},
    },
    DATA: {
        "ota": 0x00,
        "phy": 0x01,
        "nvs": 0x02,
        "coredump": 0x03,
        "nvs_keys": 0x04,
        "efuse": 0x05,
        "undefined": 0x06,
        "esphttpd": 0x80,
        "fat": 0x81,
        "spiffs": 0x82,
        "littlefs": 0x83,
        "tee_ota": 0x90,
    },
}
UNDEFINED = SUBTYPES[DATA]["undefined"]  # what a blank subtype means
FLAGS = {"encrypted": 0, "readonly": 1}  # flag name: bit in the binary entry

_ENTRY = struct.Struct("<2sBBLL16sL")  # magic, type, subtype, offset, size, name, flags
_MAGIC = b"\xaa\x50"
_MD5_MAGIC = b"\xeb\xeb"
_END = b"\xff" * _ENTRY.size
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),  # before UTF-16, whose LE mark it starts with
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
# The build expands environment variables, as os.path.expandvars does, before reading a line.
_VARIABLE = re.compile(r"(?<!\\)\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
BOOTLOADER_SETTING = "CONFIG_BOOTLOADER_OFFSET_IN_FLASH"
BOOTLOADER_OPTION = "--bootloader-offset, or "
CSV_HINT = "an ESP-IDF partition table has the columns Name, Type, SubType, Offset, Size, Flags"


@dataclass
class Partition:
    """One entry of the table. `subtype` is None for a subtype name ESP-IDF does not define,
    such as one a component registers; `subtype_name` is then the name as written."""

    name: str
    type: int
    subtype: int | None
    offset: int
    size: int
    flags: tuple[str, ...] = ()
    line: int | None = None  # in the CSV, None for a binary table
    subtype_text: str | None = None  # as written in the CSV, "" when blank

    @property
    def stop(self) -> int:
        """The first offset after the partition."""
        return self.offset + self.size

    @property
    def type_name(self) -> str:
        """`app`, `data` and so on, or the number in hex for a custom type."""
        return next((name for name, value in TYPES.items() if value == self.type), f"0x{self.type:02x}")

    @property
    def subtype_name(self) -> str:
        """`ota_0`, `nvs` and so on, or the number in hex when the type has no such name."""
        names = SUBTYPES.get(self.type, {})
        if self.subtype is None:
            return self.subtype_text or ""
        return next((name for name, value in names.items() if value == self.subtype), f"0x{self.subtype:02x}")

    def is_primary(self, ptype: int) -> bool:
        """Whether this is the primary bootloader or partition table, which the ROM finds at a
        fixed offset rather than through the table."""
        return self.type == ptype and self.subtype == PRIMARY

    @property
    def where(self) -> str:
        """How messages name the partition: `line 4 (nvs)`, or just the name for a binary."""
        return f"line {self.line} ({self.name})" if self.line is not None else self.name


def is_binary(data: bytes) -> bool:
    """Whether `data` is a binary table: it starts with an entry's magic bytes."""
    return data[:2] == _MAGIC


def alignment(ptype: int) -> int:
    """The offset an entry of this type must be a multiple of."""
    return APP_ALIGN if ptype == APP else SECTOR


def read_binary(data: bytes) -> list[Partition]:
    """The entries of a binary table, as the build writes it: 32-byte entries, an MD5 entry,
    then 0xFF padding."""
    md5 = hashlib.md5(usedforsecurity=False)
    partitions: list[Partition] = []
    for number, at in enumerate(range(0, len(data), _ENTRY.size), 1):
        chunk = data[at : at + _ENTRY.size]
        if len(chunk) != _ENTRY.size:
            raise ImportProblem("the binary table ends inside an entry", hint="rebuild the table")
        if chunk == _END:
            return partitions
        if chunk[:2] == _MD5_MAGIC:
            if chunk[16:] != md5.digest():
                raise ImportProblem(
                    "the binary table's MD5 checksum does not match its entries",
                    hint="the file is damaged; rebuild the table",
                )
            continue
        md5.update(chunk)
        magic, ptype, subtype, offset, size, raw_name, flag_bits = _ENTRY.unpack(chunk)
        if magic != _MAGIC:
            raise ImportProblem(f"entry {number} of the binary table is not a partition entry")
        name = raw_name.split(b"\0", 1)[0].decode("utf-8", "replace")
        flags = tuple(flag for flag, bit in FLAGS.items() if flag_bits & (1 << bit))
        partitions.append(Partition(name, ptype, subtype, offset, size, flags))
    raise ImportProblem("the binary table has no end marker", hint="rebuild the table")


def decode(data: bytes) -> str:
    """The text of a CSV table, honouring a byte order mark as the build does."""
    encoding = next((name for bom, name in _BOMS if data.startswith(bom)), "utf-8")
    try:
        return data.decode(encoding)
    except UnicodeDecodeError:
        raise ImportProblem("the file is neither UTF-8 text nor a binary partition table") from None


def parse_int(text: str, keywords: dict[str, int] | None = None) -> int:
    """A number as gen_esp32part.py reads one: Python's int(text, 0), so 0x and 0o prefixes and
    underscores work, with K and M suffixes, else one of `keywords`. Raises ValueError."""
    for letter, factor in (("k", 1024), ("m", 1024 * 1024)):
        if text.lower().endswith(letter):
            return parse_int(text[:-1], keywords) * factor
    try:
        return int(text, 0)
    except ValueError:
        if keywords and text.lower() in keywords:
            return keywords[text.lower()]
        raise


@dataclass
class _Row:
    """A CSV line before placement: the offset may be blank, and a negative size gives the
    offset the partition ends at."""

    partition: Partition
    offset: int | None
    size: int


def _number(text: str, line: int, column: str) -> int:
    """An offset or size field as a number, or the error the build would stop with."""
    try:
        return parse_int(text)
    except ValueError:
        raise ImportProblem(f"line {line}: bad {column} {text!r}", hint=CSV_HINT) from None


def _row(
    line: str, number: int, table_offset: int, bootloader: int | None, recovery: int | None
) -> _Row:
    """One CSV line. Missing trailing fields are blank."""
    name, ptype_text, subtype_text, offset_text, size_text, flags_text = [
        part.strip() for part in (line + ",,,,,").split(",")[:6]
    ]
    if not ptype_text:
        raise ImportProblem(f"line {number}: the type is blank", hint=CSV_HINT)
    try:
        ptype = parse_int(ptype_text, TYPES)
    except ValueError:
        hint = f"use {', '.join(TYPES)} or a number"
        if ptype_text.strip('"').lower() == "type":
            hint = "start the header line with #: every other line is read as a partition"
        raise ImportProblem(f"line {number}: unknown type {ptype_text!r}", hint=hint) from None
    if not 0 <= ptype <= 0xFF:
        raise ImportProblem(f"line {number}: type {ptype_text} is not between 0 and 0xFF")
    if not subtype_text:
        if ptype == APP:
            raise ImportProblem(f"line {number}: an app partition needs a subtype", hint=CSV_HINT)
        subtype: int | None = UNDEFINED
    else:
        try:
            subtype = parse_int(subtype_text, SUBTYPES.get(ptype, {}))
        except ValueError:
            if not re.fullmatch(r"[A-Za-z_]\w*", subtype_text):
                raise ImportProblem(f"line {number}: bad subtype {subtype_text!r}", hint=CSV_HINT) from None
            subtype = None  # a subtype a component registers: the build knows it, memmap does not
        if subtype is not None and not 0 <= subtype <= 0xFF:
            raise ImportProblem(
                f"line {number}: subtype {subtype_text} is not between 0 and 0xFF", hint=CSV_HINT
            )
    flags = tuple(flag for flag in flags_text.split(":") if flag)
    for flag in flags:
        if flag not in FLAGS:
            raise ImportProblem(
                f"line {number}: unknown flag {flag!r}",
                hint="the flags are encrypted and readonly, separated by a colon",
            )
    partition = Partition(name, ptype, subtype, 0, 0, flags, number, subtype_text)

    def needs(value: int | None, what: str, setting: str, option: str = "") -> int:
        """The bootloader offset an entry is placed at, or the error saying where it comes from."""
        if value is None:
            raise ImportProblem(
                f"line {number}: a {what} entry is placed at the {what} offset, which is not known",
                hint=f"pass {option}the project's sdkconfig with --sdkconfig, which gives it as "
                f"{setting}",
            )
        return value

    offset: int | None = None
    if ptype == BOOTLOADER and subtype == PRIMARY:
        offset = needs(bootloader, "bootloader", BOOTLOADER_SETTING, BOOTLOADER_OPTION)
    elif ptype == BOOTLOADER and subtype == SUBTYPES[BOOTLOADER]["recovery"]:
        offset = needs(recovery, "recovery bootloader", "CONFIG_BOOTLOADER_RECOVERY_OFFSET")
    elif ptype == PARTITION_TABLE and subtype == PRIMARY:
        offset = table_offset
    elif offset_text:
        offset = _number(offset_text, number, "offset")
    if ptype == BOOTLOADER:  # every bootloader slot is as big as the space below the table
        size = table_offset - needs(bootloader, "bootloader", BOOTLOADER_SETTING, BOOTLOADER_OPTION)
    elif ptype == PARTITION_TABLE:
        size = TABLE_SIZE
    elif size_text:
        size = _number(size_text, number, "size")
    else:
        raise ImportProblem(f"line {number}: the size is blank", hint=CSV_HINT)
    return _Row(partition, offset, size)


def _place(rows: list[_Row], table_offset: int) -> None:
    """Fill in blank offsets and negative sizes, as the build does: each partition follows the
    one above it in the file, so listed offsets must go up."""
    last_end = table_offset + TABLE_SIZE
    previous: Partition | None = None
    for row in rows:
        partition = row.partition
        if partition.is_primary(BOOTLOADER) or partition.is_primary(PARTITION_TABLE):
            assert row.offset is not None
            partition.offset, partition.size = row.offset, row.size
            continue  # at fixed offsets, below the other partitions
        if row.offset is not None and row.offset < last_end:
            if previous is None:
                where = f"the partition table ends at 0x{last_end:x}"
                hint = f"start it at 0x{last_end:x} or higher, or leave its offset blank"
            elif row.offset >= previous.offset:
                where = f"{previous.name}, listed before it, runs to 0x{last_end - 1:x}"
                hint = f"start it at 0x{last_end:x} or higher, or make {previous.name} smaller"
            else:
                where = f"it is listed after {previous.name}, which starts higher"
                hint = "list partitions in address order: the build places them in file order"
            raise ImportProblem(
                f"line {partition.line}: {partition.name} starts at {hex(row.offset)}, but {where}",
                hint=hint,
            )
        if row.offset is None:
            row.offset = -(-last_end // alignment(partition.type)) * alignment(partition.type)
        size = row.size if row.size >= 0 else -row.size - row.offset
        if size <= 0:
            raise ImportProblem(
                f"line {partition.line}: {partition.name} has size {size}, which memmap cannot draw",
                hint="give it a size, or leave it out of the table",
            )
        partition.offset, partition.size = row.offset, size
        last_end = partition.stop
        previous = partition


def read_csv(
    text: str, table_offset: int, bootloader: int | None = None, recovery: int | None = None
) -> list[Partition]:
    """The entries of a CSV table, placed. `bootloader` and `recovery` are the offsets of the
    bootloader slots, needed only when the table lists them."""
    rows = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        variable = _VARIABLE.search(line)
        if variable:
            raise ImportProblem(
                f"line {number}: {variable.group(0)} is an environment variable, which the build "
                "expands and memmap does not",
                hint="replace it with its value",
            )
        rows.append(_row(line, number, table_offset, bootloader, recovery))
    _place(rows, table_offset)
    return [row.partition for row in rows]
