import json
import re
import struct

import pytest
from click.testing import CliRunner
from conftest import ROOT
from elf_builder import PROGBITS, code, make_elf

from memmap.cli import app
from memmap.from_elf import ImportProblem, MemorySpec, import_elf, parse_region_spec
from memmap.from_elf.elf import SHF_ALLOC, SHF_TLS, SHT_NOBITS, ElfError, read_elf
from memmap.from_elf.importer import UNALLOCATED_NOTE
from memmap.pipeline import run

FIXTURES = ROOT / "tests" / "fixtures"
FIRMWARE = FIXTURES / "firmware.elf"
# The same firmware written by binutils in every ELF class and byte order (see elf_src/build.sh).
REAL_VARIANTS = {
    "firmware.elf": 32,
    "firmware_be.elf": 32,
    "firmware64.elf": 64,
    "firmware64_be.elf": 64,
}
FLASH = "flash=0x08000000:64K"
RAM = "ram=0x20000000:20K"
runner = CliRunner()


def invoke(*args):
    return runner.invoke(app, [str(arg) for arg in args])


def check_strict(text, tmp_path):
    """Write generated text to layout.yaml and return the codes the pipeline reports, so that
    `== []` means it passes `check --strict`."""
    file = tmp_path / "layout.yaml"
    file.write_text(text)
    return [d.code for d in run(file).diagnostics]


# Reading


def test_reads_the_fixture_firmware():
    elf = read_elf(FIRMWARE.read_bytes())
    assert elf.bits == 32
    by_name = {section.name: section for section in elf.sections}
    assert by_name[".isr_vector"].address == 0x08000000
    assert by_name[".bss"].nobits and by_name[".bss"].allocated
    assert not by_name[".comment"].allocated
    loads = [(s.virtual, s.physical, s.file_size) for s in elf.segments if s.kind == 1]
    assert (0x20000000, 0x08000622, 0x40) in loads  # .data runs in RAM, stored in flash


@pytest.mark.parametrize("bits", [32, 64])
@pytest.mark.parametrize("little", [True, False])
def test_reads_every_class_and_byte_order(bits, little):
    data = make_elf(
        [code(".text", 0x1000, 0x40, 0x200), code(".data", 0x8000, 0x10, 0x240)],
        segments=[(0x200, 0x1000, 0x1000, 0x40, 0x40), (0x240, 0x8000, 0x1040, 0x10, 0x10)],
        bits=bits,
        little=little,
    )
    elf = read_elf(data)
    assert elf.bits == bits
    assert [(s.name, s.address, s.size) for s in elf.sections[:2]] == [
        (".text", 0x1000, 0x40),
        (".data", 0x8000, 0x10),
    ]
    assert [(s.virtual, s.physical) for s in elf.segments] == [(0x1000, 0x1000), (0x8000, 0x1040)]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"PK\x03\x04", "not an ELF"),
        (b"\x7fELF\x01", "truncated"),
        (b"\x7fELF\x03\x01" + bytes(10), "unknown ELF class"),
        (make_elf([code(".text", 0, 4)])[:60], "truncated"),
        (make_elf([code(".text", 0, 4)], file_type=1), "object file"),
        (make_elf([code(".text", 0x100, 4)], file_type=4), "core dump"),
    ],
)
def test_rejects_what_it_cannot_read(data, message):
    with pytest.raises(ElfError, match=message):
        read_elf(data)


@pytest.mark.parametrize("field", [32, 40])  # e_phoff, e_shoff
def test_rejects_a_64_bit_header_offset_past_any_file(field):
    data = bytearray(make_elf([code(".text", 0, 4)], segments=[(0x100, 0, 0, 4, 4)], bits=64))
    struct.pack_into("<Q", data, field, 0xFFFF_FFFF_FFFF_FF00)
    with pytest.raises(ElfError, match="truncated"):
        read_elf(bytes(data))


@pytest.mark.parametrize("name", REAL_VARIANTS)
def test_reads_real_files_of_every_class_and_byte_order(name, tmp_path):
    elf = read_elf((FIXTURES / name).read_bytes())
    assert elf.bits == REAL_VARIANTS[name]
    allocated = [(s.name, s.kind, s.address, s.size) for s in elf.sections if s.allocated]
    assert allocated == [
        (".isr_vector", 1, 0x08000000, 0xC0),
        (".text", 1, 0x080000C0, 0x84),
        (".rodata", 1, 0x08000144, 0x4DE),
        (".data", 1, 0x20000000, 0x40),
        (".bss", SHT_NOBITS, 0x20000040, 0x1000),
        ("._stack", SHT_NOBITS, 0x20001040, 0x800),
    ]
    imported = import_elf(elf, name, [parse_region_spec(FLASH), parse_region_spec(RAM)])
    assert check_strict(imported.text, tmp_path) == []
    data_load = run(tmp_path / "layout.yaml").tree.index["data_load"]
    assert (data_load.start, data_load.size) == (0x08000622, 0x40)


def test_reads_extended_section_and_segment_counts():
    """Past 0xff00 sections or 0xffff segments, the counts move into section 0's header."""
    data = bytearray(make_elf([code(".text", 0x100, 4)], segments=[(0x100, 0x100, 0x100, 4, 4)]))
    sh_offset, ph_count, sh_count, names = *struct.unpack_from("<I", data, 32), 1, 3, 2
    struct.pack_into("<H", data, 44, 0xFFFF)  # e_phnum = PN_XNUM
    struct.pack_into("<HH", data, 48, 0, 0xFFFF)  # e_shnum = 0, e_shstrndx = SHN_XINDEX
    struct.pack_into("<IIII", data, sh_offset + 20, sh_count, names, ph_count, 0)
    elf = read_elf(bytes(data))
    assert [s.name for s in elf.sections] == [".text", ".shstrtab"]
    assert [s.virtual for s in elf.segments] == [0x100]


def test_reads_an_image_converted_to_an_object_file():
    # objcopy -I ihex -O elf32-littlearm gives a relocatable file with real addresses
    elf = read_elf(make_elf([code(".sec1", 0x08000000, 0x20000)], file_type=1))
    assert [(s.name, s.address) for s in elf.sections[:1]] == [(".sec1", 0x08000000)]


# Importing


def test_import_of_the_fixture_passes_strict_check(tmp_path):
    elf = read_elf(FIRMWARE.read_bytes())
    memories = [parse_region_spec(FLASH), parse_region_spec(RAM)]
    imported = import_elf(elf, "firmware.elf", memories)
    assert imported.diagnostics == []
    assert check_strict(imported.text, tmp_path) == []
    tree = run(tmp_path / "layout.yaml").tree
    assert [child.id for child in tree.index["flash"].children] == [
        "isr_vector", "text", "rodata", "data_load", "flash_free",
    ]
    assert tree.index["data_load"].start == 0x08000622
    assert tree.index["data_load"].note == "copied to RAM at 0x2000 0000"
    assert tree.index["ram_free"].reserved
    assert '"FLASH\\n1.6 KB of 64 KB (2%)"' in imported.text


def test_import_without_regions_draws_the_address_space(tmp_path):
    imported = import_elf(read_elf(FIRMWARE.read_bytes()), "firmware.elf", [])
    assert imported.diagnostics == []
    assert check_strict(imported.text, tmp_path) == []
    assert 'root: "/"' in imported.text and "expand:" not in imported.text


def test_sections_outside_every_region_warn_and_go_to_the_top(tmp_path):
    imported = import_elf(read_elf(FIRMWARE.read_bytes()), "fw", [parse_region_spec(FLASH)])
    assert [d.code for d in imported.diagnostics] == ["W110"] * 3
    assert check_strict(imported.text, tmp_path) == []
    assert "expand:" in imported.text


def test_a_section_across_a_region_edge_is_an_import_problem():
    with pytest.raises(ImportProblem, match="crosses the edge of --region ram"):
        import_elf(
            read_elf(FIRMWARE.read_bytes()), "fw", [parse_region_spec("ram=0x20000000:1K")]
        )


def test_overlapping_sections_keep_the_first(tmp_path):
    data = make_elf([code(".a", 0x100, 0x100), code(".b", 0x180, 0x100)])
    imported = import_elf(read_elf(data), "fw", [MemorySpec("mem", 0, 0x1000, "MEM")])
    assert [d.code for d in imported.diagnostics] == ["W111"]
    assert check_strict(imported.text, tmp_path) == []


def test_skips_unallocated_empty_and_thread_local_sections(tmp_path):
    data = make_elf(
        [
            code(".text", 0x100, 0x100),
            (".tbss", SHT_NOBITS, SHF_ALLOC | SHF_TLS, 0x200, 0x10, 0),
            code(".empty", 0x300, 0),
            (".debug_info", PROGBITS, 0, 0, 0x500, 0),
        ]
    )
    imported = import_elf(read_elf(data), "fw", [MemorySpec("mem", 0, 0x1000, "MEM")])
    assert ".text" in imported.text
    assert not any(name in imported.text for name in (".tbss", ".empty", ".debug_info"))


def fillers(start, count, size=0x100):
    """Large sections from `start` on, to push a region over FOLD_OVER blocks."""
    return [code(f".fill{i}", start + i * size, size) for i in range(count)]


def test_runs_of_small_sections_fold_into_one_block(tmp_path):
    data = make_elf(
        [
            code(".text", 0x000, 0x400),
            code(".init_array", 0x400, 4),
            code(".fini_array", 0x404, 4),
            code(".ARM.exidx", 0x408, 8),
            code(".preinit_array", 0x410, 4),
            code(".rodata", 0x420, 0x100),
            code(".tiny", 0x520, 4),
            *fillers(0x600, 3),
        ]
    )
    memories = [MemorySpec("mem", 0, 0x1000, "MEM")]
    imported = import_elf(read_elf(data), "fw", memories, min_size=64)
    assert check_strict(imported.text, tmp_path) == []
    tree = run(tmp_path / "layout.yaml").tree
    folded = tree.index["small_sections"]
    assert (folded.start, folded.end, folded.name) == (0x400, 0x413, "4 small sections")
    assert [child.name for child in folded.children] == [
        ".init_array", ".fini_array", ".ARM.exidx", ".preinit_array",
    ]
    assert "tiny" in tree.index  # a lone small section keeps its own block
    unfolded = import_elf(read_elf(data), "fw", memories, min_size=0).text
    assert "small_sections" not in unfolded and "init_array" in unfolded


def test_a_region_with_few_blocks_draws_small_sections_in_place():
    data = make_elf(
        [code(".text", 0x000, 0x400), code(".bss", 0x400, 2), code(".heap", 0x402, 4)]
    )
    text = import_elf(read_elf(data), "fw", [MemorySpec("mem", 0, 0x1000, "MEM")]).text
    assert "small_sections" not in text and '".heap"' in text


def test_every_group_gets_a_detail_column(tmp_path):
    sections = [code(".text", 0x000, 0x400)]
    sections += [code(f".a{i}", 0x400 + 8 * i, 8) for i in range(3)]
    sections += [code(".rodata", 0x418, 0x400)]
    sections += [code(f".b{i}", 0x818 + 4 * i, 4) for i in range(8)]
    imported = import_elf(read_elf(make_elf(sections)), "fw", [MemorySpec("mem", 0, 0x1000, "MEM")])
    assert check_strict(imported.text, tmp_path) == []
    result = run(tmp_path / "layout.yaml")
    groups = [result.tree.index["small_sections"], result.tree.index["small_sections_2"]]
    assert [(len(g.children), g.note) for g in groups] == [(3, None), (8, None)]
    assert [(c.root.id, c.title) for c in result.plan.columns] == [
        ("mem", "MEM\n2.1 KB of 4 KB (51%)"),
        ("small_sections", "3 small sections in MEM"),
        ("small_sections_2", "8 small sections in MEM"),
    ]


def test_groups_in_zoomed_regions_keep_a_note_instead_of_a_second_level(tmp_path):
    sections = [code(".text", 0, 0x400), *[code(f".c{i}", 0x400 + 4 * i, 4) for i in range(9)]]
    sections += [code(".rodata", 0x424, 0x400), code(".stray", 0x8000, 0x100)]
    imported = import_elf(read_elf(make_elf(sections)), "fw", [MemorySpec("mem", 0, 0x1000, "MEM")])
    assert [d.code for d in imported.diagnostics] == ["W110"]
    assert check_strict(imported.text, tmp_path) == []
    result = run(tmp_path / "layout.yaml")
    assert result.tree.index["small_sections"].note == ".c0, .c1, .c2, .c3, .c4, .c5, +3 more"
    assert [c.root.id for c in result.plan.columns] == ["/", "mem"]


def test_folding_stops_at_a_free_gap(tmp_path):
    sections = [code(".text", 0x000, 0x400), code(".a", 0x400, 8), code(".b", 0x408, 8)]
    sections += [code(".apps", 0x800, 1), *fillers(0x900, 5)]
    imported = import_elf(read_elf(make_elf(sections)), "fw", [MemorySpec("mem", 0, 0x2000, "MEM")])
    assert check_strict(imported.text, tmp_path) == []
    index = run(tmp_path / "layout.yaml").tree.index
    assert (index["small_sections"].start, index["small_sections"].end) == (0x400, 0x40F)
    assert (index["apps"].start, index["apps"].end) == (0x800, 0x800)
    assert (index["mem_free"].start, index["mem_free"].end) == (0x410, 0x7FF)


def test_one_load_image_per_segment(tmp_path):
    def sections(data_flags):
        return [
            code(".text", 0x000, 0x400, 0x100),
            ("datas", PROGBITS, data_flags, 0x8000, 0x40, 0x500),
            code("devices", 0x8040, 0x20, 0x540),
            (".bss", SHT_NOBITS, SHF_ALLOC, 0x8060, 0x100, 0x560),
            code(".fast", 0x9000, 0x10, 0x560),
        ]

    segments = [
        (0x100, 0x000, 0x000, 0x400, 0x400),
        (0x500, 0x8000, 0x400, 0x60, 0x160),  # datas and devices, stored after .text
        (0x560, 0x9000, 0x460, 0x10, 0x10),  # one section copied on its own
        (0x570, 0x9100, 0x470, 0, 0x100),  # nothing stored, so no image
    ]
    memories = [MemorySpec("flash", 0, 0x1000, "Flash"), MemorySpec("ram", 0x8000, 0x2000, "RAM")]
    imported = import_elf(read_elf(make_elf(sections(SHF_ALLOC), segments)), "fw", memories)
    assert check_strict(imported.text, tmp_path) == []
    flash = run(tmp_path / "layout.yaml").tree.index["flash"]
    images = [(c.name, c.start, c.size, c.note) for c in flash.children if c.start >= 0x400]
    assert images[:2] == [
        ("RAM init data", 0x400, 0x60, "copied to 0x0000 8000: datas, devices"),
        (".fast (load)", 0x460, 0x10, "copied to RAM at 0x0000 9000"),
    ]
    with_code = make_elf(sections(SHF_ALLOC | 0x4), segments)  # 0x4: SHF_EXECINSTR
    assert '"RAM code and data"' in import_elf(read_elf(with_code), "fw", memories).text
    assert '"Init data"' in import_elf(read_elf(make_elf(sections(SHF_ALLOC), segments)), "fw", []).text


def _armlink_elf(zi_start):
    """An armlink-style image: one LOAD segment holding ER_IROM1, with the RW bytes of
    RW_IRAM1 stored right after the code, and RW and ZI sections sharing one name."""
    rw = SHF_ALLOC | 0x1  # SHF_WRITE
    sections = [
        ("ER_IROM1", PROGBITS, SHF_ALLOC | 0x4, 0x08000000, 0x100, 0x34),
        ("RW_IRAM1", PROGBITS, rw, 0x20000000, 0x10, 0x134),
        ("RW_IRAM1", SHT_NOBITS, rw, zi_start, 0x40, 0x144),
    ]
    return make_elf(sections, [(0x34, 0x08000000, 0x08000000, 0x110, 0x200)])


ARMLINK_MEMORIES = [
    MemorySpec("er_irom1", 0x08000000, 0x1000, "ER_IROM1"),
    MemorySpec("rw_iram1", 0x20000000, 0x1000, "RW_IRAM1"),
]


@pytest.mark.parametrize(
    ("zi_start", "rw_size", "verb"),
    [(0x20000010, 0x10, "copied to"), (0x20000020, 0x20, "compressed, unpacked to")],
)
def test_armlink_rw_data_is_drawn_in_ram_and_stored_after_the_code(
    zi_start, rw_size, verb, tmp_path
):
    """armlink stores RW data in the code's segment, and with compression its section header
    gives the packed size: the RW data then runs up to the ZI data."""
    imported = import_elf(read_elf(_armlink_elf(zi_start)), "fw", ARMLINK_MEMORIES)
    assert check_strict(imported.text, tmp_path) == []
    index = run(tmp_path / "layout.yaml").tree.index
    ram = [(c.name, c.start, c.size) for c in index["rw_iram1"].children if c.kind != "reserved"]
    assert ram == [("RW_IRAM1 (RW)", 0x20000000, rw_size), ("RW_IRAM1 (ZI)", zi_start, 0x40)]
    flash = [(c.name, c.start, c.size, c.note) for c in index["er_irom1"].children]
    assert flash[1] == (
        "RW_IRAM1 (load)", 0x08000100, 0x10, f"{verb} RW_IRAM1 at 0x2000 0000"
    )
    assert flash[2][1] == 0x08000110  # free space starts after the stored RW data


@pytest.mark.parametrize(
    "between",
    [
        [("RW_IRAM1", SHT_NOBITS, SHF_ALLOC | 0x1, 0x20000010, 0x40, 0x140, 8)],  # padding only
        [
            code("STACK", 0x2000000C, 0x4, 0x140),  # another section in the gap
            ("RW_IRAM1", SHT_NOBITS, SHF_ALLOC | 0x1, 0x20000020, 0x40, 0x144),
        ],
    ],
    ids=["alignment", "another_section"],
)
def test_rw_data_is_not_unpacked_over_padding_or_another_section(between):
    """A gap before the ZI data that its alignment explains, or that another section fills,
    is not compressed RW data."""
    sections = [
        ("ER_IROM1", PROGBITS, SHF_ALLOC | 0x4, 0x08000000, 0x100, 0x34),
        ("RW_IRAM1", PROGBITS, SHF_ALLOC | 0x1, 0x20000000, 0xC, 0x134),
        *between,
    ]
    data = make_elf(sections, [(0x34, 0x08000000, 0x08000000, 0x10C, 0x200)])
    text = import_elf(read_elf(data), "fw", ARMLINK_MEMORIES).text
    assert '"RW_IRAM1 (RW)", start: 0x20000000, size: 12 }' in text
    assert "copied to RW_IRAM1 at 0x2000 0000" in text


def test_section_names_lose_the_blanks_iar_leaves_around_them():
    data = make_elf([("P2 zi ", SHT_NOBITS, SHF_ALLOC, 0x100, 0x10, 0)])
    text = import_elf(read_elf(data), "fw", [MemorySpec("mem", 0, 0x1000, "MEM")]).text
    assert 'name: "P2 zi",' in text


def test_unallocated_sections_inside_a_region_are_drawn_with_a_note(tmp_path):
    """The Pico SDK declares its RAM vector table, heap and stacks as (COPY), which leaves
    them without SHF_ALLOC: drawn when they lie in a region and overlap nothing allocated."""
    data = make_elf(
        [
            code(".data", 0x20000100, 0x40),
            (".ram_vector_table", PROGBITS, 0, 0x20000000, 0xC0, 0),
            (".heap", PROGBITS, 0, 0x20000200, 0x800, 0),
            (".shadow", PROGBITS, 0, 0x20000120, 0x10, 0),  # overlaps .data
            (".outside", PROGBITS, 0, 0x30000000, 0x10, 0),  # in no region
            (".symbols", 2, 0, 0x20000300, 0x10, 0),  # SHT_SYMTAB, not memory
            (".debug_info", PROGBITS, 0, 0, 0x500, 0),
        ]
    )
    imported = import_elf(read_elf(data), "fw", [MemorySpec("ram", 0x20000000, 0x4000, "RAM")])
    assert imported.diagnostics == []
    assert check_strict(imported.text, tmp_path) == []
    ram = run(tmp_path / "layout.yaml").tree.index["ram"]
    drawn_sections = [(c.name, c.note) for c in ram.children if c.kind != "reserved"]
    assert drawn_sections == [
        (".ram_vector_table", UNALLOCATED_NOTE),
        (".data", None),
        (".heap", UNALLOCATED_NOTE),
    ]


def test_several_banks_get_a_column_each_and_copies_name_their_bank(tmp_path):
    """Like an STM32H7: code in flash and ITCM, data in DTCM and AXI SRAM, SRAM4 unused."""
    run_code, data = SHF_ALLOC | 0x4, SHF_ALLOC | 0x1  # executable, writable
    sections = [
        (".text", PROGBITS, run_code, 0x08000000, 0x8000, 0x1000),
        (".itcm_text", PROGBITS, run_code, 0x00000000, 0x800, 0x9000),
        (".data", PROGBITS, data, 0x20000000, 0x100, 0x9800),
        (".bss", SHT_NOBITS, data, 0x20000100, 0x1000, 0x9900),
        (".axi_data", PROGBITS, data, 0x24000000, 0x80, 0x9900),
        (".framebuffer", SHT_NOBITS, data, 0x24000080, 0x10000, 0x9980),
    ]
    segments = [
        (0x1000, 0x08000000, 0x08000000, 0x8000, 0x8000),
        (0x9000, 0x00000000, 0x08008000, 0x800, 0x800),  # each copy goes to another bank
        (0x9800, 0x20000000, 0x08008800, 0x100, 0x1100),
        (0x9900, 0x24000000, 0x08008900, 0x80, 0x10080),
    ]
    specs = [
        "flash=0x08000000:2M:Flash",
        "itcm=0x0:64K:ITCM",
        "dtcm=0x20000000:128K:DTCM",
        "axi=0x24000000:512K:AXI SRAM",
        "sram4=0x38000000:64K:SRAM4",
    ]
    memories = [parse_region_spec(spec) for spec in specs]
    imported = import_elf(read_elf(make_elf(sections, segments)), "fw", memories)
    assert imported.diagnostics == []
    assert check_strict(imported.text, tmp_path) == []
    index = run(tmp_path / "layout.yaml").tree.index
    assert {copy: index[copy].note for copy in ("itcm_text_load", "data_load", "axi_data_load")} == {
        "itcm_text_load": "copied to ITCM at 0x0000 0000",
        "data_load": "copied to DTCM at 0x2000 0000",
        "axi_data_load": "copied to AXI SRAM at 0x2400 0000",
    }
    assert [(c.name, c.size) for c in index["sram4"].children] == [("Free\n64 KB", 0x10000)]
    assert re.findall(r'root: "(\w+)"', imported.text) == ["itcm", "flash", "dtcm", "axi", "sram4"]
    assert '"SRAM4\\n0 B of 64 KB (0%)"' in imported.text


def test_free_blocks_print_their_size_rounded_down(tmp_path):
    data = make_elf([code(".text", 0x000, 0x100), code(".data", 0x80000000, 0x10)])
    memories = [MemorySpec("flash", 0, 0x10000, "Flash"), MemorySpec("ram", 0x80000000, 1 << 31, "RAM")]
    imported = import_elf(read_elf(data), "fw", memories)
    assert check_strict(imported.text, tmp_path) == []
    index = run(tmp_path / "layout.yaml").tree.index
    assert index["flash_free"].name == "Free\n≈63.7 KB"  # 63.75 KB, never rounded up
    assert index["ram_free"].name == "Free\n≈1.99 GB"
    exact = import_elf(read_elf(data), "fw", [MemorySpec("flash", 0, 0x780100, "Flash")]).text
    assert '"Free\\n7.5 MB"' in exact  # exact, so no ≈


def test_only_gaps_of_min_size_get_a_free_block(tmp_path):
    data = make_elf([code(".a", 0x000, 0x100), code(".b", 0x110, 0x100), code(".c", 0x300, 0x100)])
    imported = import_elf(read_elf(data), "fw", [MemorySpec("mem", 0, 0x1000, "MEM")], min_size=64)
    assert check_strict(imported.text, tmp_path) == []
    free = [n for n in run(tmp_path / "layout.yaml").tree.index.values() if n.reserved]
    assert [(n.start, n.end) for n in free] == [(0x210, 0x2FF), (0x400, 0xFFF)]


def test_ids_are_cleaned_up_and_unique(tmp_path):
    data = make_elf(
        [code(".ARM.exidx", 0x000, 0x100), code("arm_exidx", 0x100, 0x100), code(".mem", 0x200, 0x100)]
    )
    imported = import_elf(read_elf(data), "fw", [MemorySpec("mem", 0, 0x1000, "MEM")])
    assert check_strict(imported.text, tmp_path) == []
    index = run(tmp_path / "layout.yaml").tree.index
    assert index["arm_exidx"].name == ".ARM.exidx"
    assert index["arm_exidx_2"].name == "arm_exidx"
    assert index["mem_2"].name == ".mem"


def test_64_bit_elf_sets_address_bits(tmp_path):
    data = make_elf([code(".text", 0x8000_0000_0000, 0x1000)], bits=64)
    imported = import_elf(read_elf(data), "fw", [])
    assert "address_bits: 64" in imported.text
    assert check_strict(imported.text, tmp_path) == []


def test_ids_and_references_are_quoted_so_yaml_keeps_them_strings(tmp_path):
    data = make_elf([code("123", 0x000, 0x100), code(".true", 0x100, 0x100), code(".null", 0x200, 0x100)])
    memories = [parse_region_spec("456=0:0x1000"), parse_region_spec("no=0x1000:0x1000")]
    imported = import_elf(read_elf(data), "fw", memories)
    assert check_strict(imported.text, tmp_path) == []
    index = run(tmp_path / "layout.yaml").tree.index
    assert {"123", "true", "null", "456", "no"} <= set(index)


def test_a_line_break_in_the_file_name_stays_inside_the_comment(tmp_path):
    imported = import_elf(read_elf(FIRMWARE.read_bytes()), "fw\nversion: 2.elf", [])
    assert imported.text.splitlines()[0].startswith("# Generated by `memmap from-elf` from fw?version")
    assert check_strict(imported.text, tmp_path) == []


def test_usage_percent_rounds_down():
    data = make_elf([code(".text", 0, 996)])
    text = import_elf(read_elf(data), "fw", [MemorySpec("mem", 0, 1000, "MEM")]).text
    assert "996 B of 1000 B (99%)" in text  # 99.6%, not shown as full


def test_a_file_without_section_headers_is_an_import_problem():
    data = bytearray(make_elf([code(".text", 0x100, 4)], segments=[(0x100, 0x100, 0x100, 4, 4)]))
    struct.pack_into("<I", data, 32, 0)  # e_shoff = 0: stripped of section headers
    struct.pack_into("<HH", data, 48, 0, 0)
    with pytest.raises(ImportProblem, match="no section headers"):
        import_elf(read_elf(bytes(data)), "fw", [])


def test_an_elf_with_nothing_to_draw_is_an_import_problem():
    with pytest.raises(ImportProblem, match="no allocated sections"):
        import_elf(read_elf(make_elf([(".debug_info", PROGBITS, 0, 0, 0x500, 0)])), "fw", [])


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("flash=0x08000000:64K", MemorySpec("flash", 0x08000000, 0x10000, "FLASH")),
        ("ram=0x2000_0000:20K:Main SRAM", MemorySpec("ram", 0x20000000, 0x5000, "Main SRAM")),
        ("ext=0x90000000:16M:QSPI: boot", MemorySpec("ext", 0x90000000, 16 << 20, "QSPI: boot")),
    ],
)
def test_parses_region_specs(spec, expected):
    assert parse_region_spec(spec) == expected


@pytest.mark.parametrize(
    "spec", ["flash", "flash=0x0800", "Flash=0:1K", "flash=0x08000000:0", "flash=zz:1K"]
)
def test_rejects_bad_region_specs(spec):
    with pytest.raises(ImportProblem):
        parse_region_spec(spec)


@pytest.mark.parametrize(
    "specs", [["a=0:1K", "a=0x1000:1K"], ["a=0:2K", "b=0x400:1K"]], ids=["duplicate", "overlap"]
)
def test_rejects_conflicting_regions(specs):
    with pytest.raises(ImportProblem):
        import_elf(read_elf(FIRMWARE.read_bytes()), "fw", [parse_region_spec(s) for s in specs])


# Command line


def test_cli_writes_a_checked_config(tmp_path):
    target = tmp_path / "layout.yaml"
    result = invoke("from-elf", FIRMWARE, "-o", target, "--region", FLASH, "--region", RAM)
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == str(target)
    assert invoke("render", target, "-o", tmp_path / "layout.svg", "--strict").exit_code == 0


def test_cli_defaults_the_output_next_to_the_elf(tmp_path):
    elf = tmp_path / "app.elf"
    elf.write_bytes(FIRMWARE.read_bytes())
    assert invoke("from-elf", elf).exit_code == 0
    assert (tmp_path / "app.yaml").read_text().startswith("# Generated by `memmap from-elf`")


def test_cli_will_not_overwrite_without_force(tmp_path):
    target = tmp_path / "layout.yaml"
    target.write_text("keep me")
    assert invoke("from-elf", FIRMWARE, "-o", target).exit_code == 2
    assert target.read_text() == "keep me"
    assert invoke("from-elf", FIRMWARE, "-o", target, "--force").exit_code == 0


def test_cli_json_reports_import_warnings(tmp_path):
    target = tmp_path / "layout.yaml"
    result = invoke("from-elf", FIRMWARE, "-o", target, "--region", FLASH, "--json")
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["ok"] is True and data["output"] == str(target)
    assert [d["code"] for d in data["diagnostics"]] == ["W110"] * 3
    strict = invoke("from-elf", FIRMWARE, "-o", target, "--region", FLASH, "--force", "--strict")
    assert strict.exit_code == 1


@pytest.mark.parametrize(
    "args",
    [
        ["--region", "flash"],
        ["--min-size", "lots"],
        ["--region", "ram=0x20000000:1K"],
    ],
)
def test_cli_usage_problems_exit_2_with_e090(args, tmp_path):
    result = invoke("from-elf", FIRMWARE, "-o", tmp_path / "out.yaml", "--json", *args)
    assert result.exit_code == 2
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "E090"
    assert not (tmp_path / "out.yaml").exists()


def test_cli_will_not_overwrite_the_elf(tmp_path):
    elf = tmp_path / "fw.elf"
    elf.write_bytes(FIRMWARE.read_bytes())
    result = invoke("from-elf", elf, "-o", elf, "--force", "--json")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "E090"
    assert elf.read_bytes() == FIRMWARE.read_bytes()
    named_yaml = tmp_path / "fw.yaml"  # the default output would be the ELF itself
    named_yaml.write_bytes(FIRMWARE.read_bytes())
    assert invoke("from-elf", named_yaml, "--force").exit_code == 2
    assert named_yaml.read_bytes() == FIRMWARE.read_bytes()


def test_cli_min_size_0_turns_folding_off(tmp_path):
    elf = tmp_path / "fw.elf"
    sections = [code(".text", 0, 0x400), *[code(f".s{i}", 0x400 + 4 * i, 4) for i in range(10)]]
    elf.write_bytes(make_elf(sections))
    region = "mem=0:0x1000"
    assert invoke("from-elf", elf, "-o", tmp_path / "a.yaml", "--region", region).exit_code == 0
    assert "small_sections" in (tmp_path / "a.yaml").read_text()
    result = invoke("from-elf", elf, "-o", tmp_path / "b.yaml", "--region", region, "--min-size", "0")
    assert result.exit_code == 0
    text = (tmp_path / "b.yaml").read_text()
    assert "small_sections" not in text and all(f'".s{i}"' in text for i in range(10))


def test_cli_reports_a_file_that_is_not_elf(tmp_path):
    result = invoke("from-elf", ROOT / "pyproject.toml", "-o", tmp_path / "out.yaml")
    assert result.exit_code == 2
    assert "not an ELF file" in result.stderr


def test_cli_reports_a_missing_elf(tmp_path):
    assert invoke("from-elf", tmp_path / "missing.elf", "-o", tmp_path / "out.yaml").exit_code == 2
