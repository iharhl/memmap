"""Build minimal ELF files for tests: headers only, no section contents."""

import struct

from memmap.elf import SHF_ALLOC

PROGBITS = 1


def make_elf(sections, segments=(), bits=32, little=True, file_type=2):
    """A minimal ELF with the given section and segment headers and no section contents.
    Each section is (name, type, flags, address, size, offset); each segment is
    (offset, virtual, physical, file_size, memory_size)."""
    order = "<" if little else ">"
    if bits == 32:
        header_fmt, section_fmt, segment_fmt = "HHIIIIIHHHHHH", "IIIIIIIIII", "IIIIIIII"
    else:
        header_fmt, section_fmt, segment_fmt = "HHIQQQIHHHHHH", "IIQQQQIIQQ", "IIQQQQQQ"
    header_size = 16 + struct.calcsize(order + header_fmt)
    section_size = struct.calcsize(order + section_fmt)
    segment_size = struct.calcsize(order + segment_fmt)

    names = b"\0"
    name_offsets = []
    for section in [*sections, (".shstrtab",)]:
        name_offsets.append(len(names))
        names += section[0].encode() + b"\0"
    ph_offset = header_size
    names_offset = ph_offset + len(segments) * segment_size
    sh_offset = names_offset + len(names)

    def section_header(name, kind, flags, address, size, offset):
        return struct.pack(order + section_fmt, name, kind, flags, address, offset, size, 0, 0, 1, 0)

    def segment_header(offset, virtual, physical, file_size, memory_size):
        if bits == 32:
            return struct.pack(order + segment_fmt, 1, offset, virtual, physical, file_size, memory_size, 5, 4)
        return struct.pack(order + segment_fmt, 1, 5, offset, virtual, physical, file_size, memory_size, 4)

    count = len(sections) + 2
    ident = b"\x7fELF" + bytes([1 if bits == 32 else 2, 1 if little else 2, 1]) + bytes(9)
    header = struct.pack(
        order + header_fmt, file_type, 40, 1, 0, ph_offset if segments else 0, sh_offset, 0,
        header_size, segment_size, len(segments), section_size, count, count - 1,
    )
    data = ident + header + b"".join(segment_header(*segment) for segment in segments) + names
    data += section_header(0, 0, 0, 0, 0, 0)
    for name_offset, (_, kind, flags, address, size, offset) in zip(name_offsets, sections):
        data += section_header(name_offset, kind, flags, address, size, offset)
    data += section_header(name_offsets[-1], 3, 0, 0, len(names), names_offset)
    return data


def code(name, address, size, offset=0x1000):
    """An allocated PROGBITS section header tuple for make_elf."""
    return (name, PROGBITS, SHF_ALLOC, address, size, offset)
