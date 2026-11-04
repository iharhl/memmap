"""A small ELF reader: the header, section headers and program headers, nothing more.

memmap only needs where each section sits, so this avoids a dependency. It handles 32- and 64-bit
files in either byte order.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# Section header types and flags used by the importer.
SHT_NOBITS = 8
SHF_ALLOC = 0x2
SHF_TLS = 0x400
PT_LOAD = 1

_ET_REL = 1
_ET_CORE = 4
_SHN_XINDEX = 0xFFFF
_PN_XNUM = 0xFFFF

# Header layouts after e_ident, per ELF class: (file header, section header, program header).
_LAYOUTS = {
    32: ("HHIIIIIHHHHHH", "IIIIIIIIII", "IIIIIIII"),
    64: ("HHIQQQIHHHHHH", "IIQQQQIIQQ", "IIQQQQQQ"),
}
_IDENT_SIZE = 16


class ElfError(ValueError):
    """The file is not an ELF memmap can read."""


@dataclass(frozen=True)
class Section:
    """One section header. `address` is where the section runs (its VMA)."""

    name: str
    kind: int
    flags: int
    address: int
    offset: int
    size: int

    @property
    def allocated(self) -> bool:
        """Whether the section occupies memory on the target."""
        return bool(self.flags & SHF_ALLOC)

    @property
    def nobits(self) -> bool:
        """Whether the section has no bytes in the file, like .bss."""
        return self.kind == SHT_NOBITS


@dataclass(frozen=True)
class Segment:
    """One program header. `physical` is where its bytes are loaded (the LMA)."""

    kind: int
    offset: int
    virtual: int
    physical: int
    file_size: int
    memory_size: int


@dataclass(frozen=True)
class Elf:
    """What memmap reads from an ELF file."""

    bits: int
    sections: list[Section]
    segments: list[Segment]


def _unpack(fmt: str, data: bytes, offset: int) -> tuple[int, ...]:
    """Unpack one structure, reporting a short file as an ElfError. A 64-bit offset can be too
    large for struct to take at all, which it reports as OverflowError."""
    try:
        return struct.unpack_from(fmt, data, offset)
    except (struct.error, OverflowError):
        raise ElfError("file is truncated") from None


def _name(table: bytes, offset: int) -> str:
    """A null-terminated string from a string table."""
    if offset >= len(table):
        return ""
    end = table.find(b"\0", offset)
    return table[offset : end if end >= 0 else len(table)].decode("utf-8", "replace")


def _table(
    data: bytes, fmt: str, offset: int, count: int, entry_size: int
) -> list[tuple[int, ...]]:
    """Read `count` header entries of `entry_size` bytes each, starting at `offset`."""
    if count and entry_size < struct.calcsize(fmt):
        raise ElfError(f"header entries of {entry_size} bytes are too small")
    return [_unpack(fmt, data, offset + index * entry_size) for index in range(count)]


def read_elf(data: bytes) -> Elf:
    """Parse an executable or shared ELF file, or an image converted to an object file.
    Raises ElfError for anything else."""
    if data[:4] != b"\x7fELF":
        raise ElfError("not an ELF file")
    if len(data) < _IDENT_SIZE:
        raise ElfError("file is truncated")
    bits = {1: 32, 2: 64}.get(data[4])
    order = {1: "<", 2: ">"}.get(data[5])
    if bits is None or order is None:
        raise ElfError("unknown ELF class or byte order")
    header_fmt, section_fmt, segment_fmt = (order + fmt for fmt in _LAYOUTS[bits])
    header = _unpack(header_fmt, data, _IDENT_SIZE)
    file_type, ph_offset, sh_offset = header[0], header[4], header[5]
    ph_size, ph_count, sh_size, sh_count, sh_names = header[8:13]
    if file_type == _ET_CORE:
        raise ElfError("this is a core dump, not a firmware image")

    raw_sections: list[tuple[int, ...]] = []
    if sh_offset:
        first = _table(data, section_fmt, sh_offset, 1, sh_size)[0]
        if sh_count == 0:  # more than 0xff00 sections: the count is in section 0
            sh_count = first[5]
        if sh_names == _SHN_XINDEX:
            sh_names = first[6]
        if ph_count == _PN_XNUM:
            ph_count = first[7]
        raw_sections = _table(data, section_fmt, sh_offset, sh_count, sh_size)

    names = b""
    if raw_sections:
        if sh_names >= len(raw_sections):
            raise ElfError("section name table index is out of range")
        table = raw_sections[sh_names]
        names = data[table[4] : table[4] + table[5]]

    sections = [
        Section(_name(names, raw[0]), raw[1], raw[2], raw[3], raw[4], raw[5])
        for raw in raw_sections[1:]  # section 0 is always empty
    ]
    # An unlinked object file has every section at address 0. One with real addresses, as
    # objcopy makes from a .hex or .bin image, is drawn like an executable.
    if file_type == _ET_REL and not any(s.allocated and s.address for s in sections):
        raise ElfError("this is an object file (.o), link it into an executable first")
    segments = []
    for raw in _table(data, segment_fmt, ph_offset, ph_count if ph_offset else 0, ph_size):
        if bits == 32:
            kind, offset, virtual, physical, file_size, memory_size = raw[:6]
        else:  # the 64-bit layout moves p_flags up to second place
            kind, _, offset, virtual, physical, file_size, memory_size = raw[:7]
        segments.append(Segment(kind, offset, virtual, physical, file_size, memory_size))
    return Elf(bits, sections, segments)
