"""Memory regions from linker maps: `memmap from-elf --map`.

The readers run on real maps from tests/fixtures/linker_snapshots/ (see docs/from-elf.md),
compared with tests/golden/linker/regions.json. Regenerate after an intended change with:

    MEMMAP_UPDATE_GOLDEN=1 uv run pytest tests/test_linker.py
"""

import json
import os
import re

import pytest
from click.testing import CliRunner
from conftest import ROOT
from elf_builder import code, make_elf

from memmap.cli import app
from memmap.from_elf import (
    ImportProblem,
    LinkerFile,
    LinkerRegion,
    parse_region_spec,
    read_map,
    resolve_memories,
)

FIXTURES = ROOT / "tests" / "fixtures"
SNAPSHOTS = FIXTURES / "linker_snapshots"
INDEX = json.loads((SNAPSHOTS / "index.json").read_text(encoding="utf-8"))
ENTRIES = [entry["name"] for entry in INDEX]
GOLDEN = ROOT / "tests" / "golden" / "linker" / "regions.json"
UPDATE = os.environ.get("MEMMAP_UPDATE_GOLDEN") == "1"
SOURCES = ROOT / "build" / "linker"
FIRMWARE = FIXTURES / "firmware.elf"
FIRMWARE_MAP = FIXTURES / "firmware.map"
LLD_HEADER = "             VMA              LMA     Size Align Out     In      Symbol\n"
runner = CliRunner()


def invoke(*args):
    return runner.invoke(app, [str(arg) for arg in args])


def summary(linker):
    """A map's regions as JSON-friendly rows."""
    return {
        "kind": linker.kind,
        "regions": [
            [r.name, f"0x{r.start:X}", None if r.size is None else f"0x{r.size:X}"]
            for r in linker.regions
        ],
    }


def write(tmp_path, name, text):
    file = tmp_path / name
    file.write_text(text, encoding="utf-8")
    return file


def gnu_map(*rows):
    """GNU ld map text whose Memory Configuration table has `rows`, each `NAME ORIGIN LENGTH`."""
    lines = ["Memory Configuration", "", "Name             Origin             Length             Attributes"]
    lines += [*rows, "*default*        0x00000000         0xffffffff", "", "Linker script and memory map", ""]
    return "\n".join(lines)


def elf_file(tmp_path, sections, name="fw.elf"):
    file = tmp_path / name
    file.write_bytes(make_elf(sections))
    return file


def cortex_m(tmp_path):
    """A small image with code in flash at 0x08000000 and data in RAM at 0x20000000."""
    return elf_file(tmp_path, [code(".text", 0x08000000, 0x1000), code(".data", 0x20000000, 0x100)])


def usage_problem(result):
    """The message and hint of the one E090 of a --json run that exited 2."""
    assert result.exit_code == 2, result.output
    (diagnostic,) = json.loads(result.stdout)["diagnostics"]
    assert diagnostic["code"] == "E090"
    return diagnostic["message"], diagnostic["hint"]


def warnings(result):
    """(code, subject, hint) of each diagnostic of a --json run."""
    return [(d["code"], d["subject"], d["hint"]) for d in json.loads(result.stdout)["diagnostics"]]


def header(file):
    """The comment lines at the top of a generated config, one string."""
    lines = file.read_text().splitlines()
    return "\n".join(line for line in lines[: lines.index("version: 1")])


# Real files


def test_snapshots_are_listed():
    assert len(ENTRIES) >= 13
    assert set(ENTRIES) == {path.name for path in SNAPSHOTS.glob("*.map")}


def test_snapshots_read_to_the_golden_regions():
    found = {name: summary(read_map(SNAPSHOTS / name)) for name in ENTRIES}
    if UPDATE:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(found, indent=2) + "\n", encoding="utf-8", newline="\n")
    assert found == json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ENTRIES)
def test_full_file_reads_like_its_snapshot(name):
    """The excerpt keeps everything the reader needs. Runs only where refresh.py has the real
    file in build/linker/."""
    source = SOURCES / name
    if not source.exists():
        pytest.skip(f"build/linker/{name} is missing")
    assert summary(read_map(source)) == summary(read_map(SNAPSHOTS / name))


# Readers


GNU_MAP = """Memory Configuration\r
\r
Name             Origin             Length             Attributes\r
FLASH            0x08000000         0x00010000         xr\r
RAM              0x20000000         0x00005000         xrw !x\r
a_very_long_region_name 0x30000000  0x00000100\r
*default*        0x00000000         0xffffffff\r
\r
Linker script and memory map\r
\r
FAKE             0x40000000         0x00000100\r
"""


def test_gnu_map(tmp_path):
    linker = read_map(write(tmp_path, "fw.map", GNU_MAP))
    assert linker.kind == "GNU ld map"
    assert [(r.name, r.start, r.size) for r in linker.regions] == [
        ("FLASH", 0x08000000, 0x10000),
        ("RAM", 0x20000000, 0x5000),
        ("a_very_long_region_name", 0x30000000, 0x100),
    ]


def test_gnu_map_of_a_script_without_memory_has_no_regions(tmp_path):
    linker = read_map(write(tmp_path, "fw.map", gnu_map()))
    assert (linker.kind, linker.regions) == ("GNU ld map", [])


@pytest.mark.parametrize(
    "first_line",
    [LLD_HEADER, "Address  Size     Align Out     In      Symbol\n"],  # the second: lld before 8
)
def test_lld_map_has_no_regions(tmp_path, first_line):
    linker = read_map(write(tmp_path, "fw.map", first_line + "       0      0   100     1 .text\n"))
    assert (linker.kind, linker.regions) == ("lld map", [])


def test_armlink_map_without_a_limit(tmp_path):
    text = (
        "Memory Map of the image\n"
        "  Load Region LR_1 (Base: 0x08000000, Size: 0x0000023c, Max: 0xffffffff, ABSOLUTE)\n"
        "    Execution Region ER_RO (Exec base: 0x08000000, Load base: 0x08000000, Size: "
        "0x0000023c, Max: 0xffffffff, ABSOLUTE)\n"
        "    Execution Region ARM_LIB_STACK (Base: 0x2001f000, Size: 0x00001000, Max: "
        "0x00001000, ABSOLUTE, EMPTY)\n"
    )
    linker = read_map(write(tmp_path, "fw.map", text))
    assert [(r.name, r.start, r.size) for r in linker.regions] == [
        ("ER_RO", 0x08000000, None),
        ("ARM_LIB_STACK", 0x2001F000, 0x1000),
    ]


def test_iar_map_kinds_and_merging(tmp_path):
    text = """# IAR ELF Linker V9.30.1.335/W64 for ARM
*** PLACEMENT SUMMARY
"A0":  place at address 0x800'0000 { ro section .intvec };
"P1":  place in [from 0x800'0000 to 0x80f'ffff] { ro };
"P2":  place in [from 0x2000'0000 to 0x2001'bfff] |
                [from 0x2001'c000 to 0x2001'ffff] { rw, block CSTACK };
"P3":  place in [from 0x1000'0000 size 0x1'0000] { block HEAP };
"P4":  place in [0x4000-0x407f] { section .eeprom };
"P5":  place in [from 0x2002'0000 to 0x2002'0fff] { section .noinit };
  Section            Kind         Address    Size  Object
"P9":  place in [from 0x9000'0000 to 0x9000'ffff] { ro };
"""
    linker = read_map(write(tmp_path, "fw.map", text))
    assert [(r.name, r.start, r.size) for r in linker.regions] == [
        ("Memory", 0x4000, 0x80),
        ("ROM", 0x08000000, 0x100000),
        ("RAM", 0x10000000, 0x10000),
        ("RAM 2", 0x20000000, 0x21000),  # the unknown .noinit range adjoins RAM and joins it
    ]


def test_iar_map_with_fixed_addresses_only_has_no_regions(tmp_path):
    text = "# IAR ELF Linker V9.30\n*** PLACEMENT SUMMARY\n\"A0\":  place at address 0x800'0000 { ro };\n"
    linker = read_map(write(tmp_path, "fw.map", text))
    assert (linker.kind, linker.regions) == ("IAR map", [])


def test_iar_map_adjoining_ranges_of_other_kinds_stay_apart(tmp_path):
    text = """# IAR ELF Linker V8.40
*** PLACEMENT SUMMARY
"P1":  place in [from 0x0 to 0xfff] { ro };
"P2":  place in [from 0x1000 to 0x1fff] { rw };
"P3":  place in [from 0x2000 to 0x2fff] { rw, block CSTACK };
"""
    linker = read_map(write(tmp_path, "fw.map", text))
    assert [(r.name, r.start, r.size) for r in linker.regions] == [("ROM", 0, 0x1000), ("RAM", 0x1000, 0x2000)]


def test_iar_map_reversed_range_is_skipped(tmp_path):
    text = "# IAR ELF Linker V8.40\n*** PLACEMENT SUMMARY\n\"P1\":  place in [from 0x2000 to 0x1000] { rw };\n"
    assert read_map(write(tmp_path, "fw.map", text)).regions == []


def test_iar_map_with_a_bad_number(tmp_path):
    text = "# IAR ELF Linker V8.40\n*** PLACEMENT SUMMARY\n\"P1\":  place in [from 0x' to 0x'] { rw };\n"
    with pytest.raises(ImportProblem, match="cannot read the address \"0x'\" in the placement summary"):
        read_map(write(tmp_path, "fw.map", text))


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (LLD_HEADER, "lld map"),
        (gnu_map("FLASH 0x0 0x1000 xr"), "GNU ld map"),  # the table on the first line
    ],
)
def test_maps_with_a_byte_order_mark(tmp_path, text, kind):
    file = tmp_path / "fw.map"
    file.write_bytes(b"\xef\xbb\xbf" + text.encode())
    assert read_map(file).kind == kind


NOT_MAPS = {
    "memory.x": (
        "MEMORY\n{\n  FLASH : ORIGIN = 0, LENGTH = 1M\n}\n",
        "memory.x is a linker script, which memmap does not read",
        ("pass the map file the linker writes, or give each region with --region ID=START:SIZE "
        "from the script's MEMORY block"),
    ),
    "fw.ld": (
        "/* boot */\nSECTIONS {\n  .text : { *(.text) }\n}\n",
        "fw.ld is a linker script, which memmap does not read",
        "pass the map file the linker writes, or give each region with --region ID=START:SIZE",
    ),
    "notes.txt": ("/* MEMORY { a : ORIGIN = 0, LENGTH = 1 } */\n", "notes.txt is not a map file memmap can read", None),
    "empty.map": ("", "empty.map is not a map file memmap can read", None),
    "fw.elf": (make_elf([code(".text", 0, 16)]), "fw.elf is not a map file memmap can read", None),
}


@pytest.mark.parametrize(("name", "case"), NOT_MAPS.items(), ids=NOT_MAPS)
def test_files_that_are_not_maps(tmp_path, name, case):
    content, message, hint = case
    file = tmp_path / name
    file.write_bytes(content if isinstance(content, bytes) else content.encode())
    with pytest.raises(ImportProblem) as raised:
        read_map(file)
    assert str(raised.value) == message
    assert raised.value.hint == (hint or "pass the map file of a GNU ld, armlink or IAR link")


def test_unreadable_maps(tmp_path):
    with pytest.raises(ImportProblem, match="cannot read .*missing.map"):
        read_map(tmp_path / "missing.map")
    with pytest.raises(ImportProblem, match="cannot read"):
        read_map(tmp_path)  # a directory


# Resolving regions


def linker_file(*regions, kind="GNU ld map"):
    return LinkerFile("fw.map", kind, list(regions))


def rows(resolved):
    return [(m.id, m.start, m.size, m.source) for m in resolved.memories]


def w112(resolved):
    """(subject, hint) of each W112 of a resolve."""
    assert {d.code for d in resolved.diagnostics} <= {"W112"}
    return [(d.subject, d.hint) for d in resolved.diagnostics]


def test_resolve_ids_names_and_sources():
    linker = linker_file(LinkerRegion("FLASH", 0x0, 0x1000), LinkerRegion("RAM_D1", 0x2000, 0x100))
    resolved = resolve_memories(linker, [], 32)
    assert [(m.id, m.name, m.source, m.optional) for m in resolved.memories] == [
        ("flash", "FLASH", "fw.map", True),
        ("ram_d1", "RAM_D1", "fw.map", True),
    ]
    assert (resolved.notes, resolved.diagnostics) == ([], [])


def test_resolve_leaves_out_what_cannot_be_drawn():
    linker = linker_file(
        LinkerRegion("FLASH", 0x0, 0x1000),
        LinkerRegion("TOP", 0xFFFFF800, 0x1000),
        LinkerRegion("ER_RW", 0x20000000, None),
        LinkerRegion("EMPTY", 0x30000000, 0),
    )
    resolved = resolve_memories(linker, [], 32)
    assert [m.id for m in resolved.memories] == ["flash"]
    assert resolved.notes == [
        "Left out of fw.map: TOP (0xFFFFF800, 4K): ends past the 32-bit address space.",
        "Left out of fw.map: ER_RW (0x20000000): no size.",
        "Left out of fw.map: EMPTY (0x30000000, 0): size 0.",
    ]
    assert w112(resolved) == [  # a region of size 0 holds nothing, so it loses nothing
        ("top", "check that the map belongs to this ELF, or give it with --region top=0xFFFFF800:SIZE"),
        ("er_rw", "give it with --region er_rw=0x20000000:SIZE"),
    ]
    assert resolved.diagnostics[1].message == "ER_RW (0x20000000) in fw.map is left out: no size"


@pytest.mark.parametrize(
    ("bits", "start", "size", "drawn"),
    [
        (32, 0xFFFFF7FF, 0x800, True),  # Zephyr's IDT_LIST, ending one byte below the top
        (32, 0xFFFFF000, 0x1000, True),  # ending on the last address
        (32, 0xFFFFF001, 0x1000, False),  # one byte past it
        (64, 0xFFFFFFFF_FFFFF000, 0x1000, True),
        (64, 0xFFFFFFFF_FFFFF001, 0x1000, False),
    ],
)
def test_resolve_address_space_edges(bits, start, size, drawn):
    linker = linker_file(LinkerRegion("TOP", start, size), LinkerRegion("LOW", 0, 0x100))
    assert [m.id for m in resolve_memories(linker, [], bits).memories] == (["top", "low"] if drawn else ["low"])


def test_resolve_with_region_overrides():
    linker = linker_file(LinkerRegion("FLASH", 0x0, 0x1000), LinkerRegion("ER_RW", 0x4000, None))
    specs = [parse_region_spec("flash=0x0:2K:Flash"), parse_region_spec("er_rw=0x4000:1K"),
             parse_region_spec("extra=0x8000:1K")]
    resolved = resolve_memories(linker, specs, 32)
    assert rows(resolved) == [
        ("flash", 0, 2048, None),
        ("er_rw", 0x4000, 1024, None),
        ("extra", 0x8000, 1024, None),
    ]
    assert (resolved.notes, resolved.diagnostics) == ([], [])


def test_resolve_repeated_names_get_numbered_ids():
    linker = linker_file(LinkerRegion("RAM", 0, 16), LinkerRegion("ram", 16, 16))
    assert [m.id for m in resolve_memories(linker, [], 32).memories] == ["ram", "ram_2"]


def test_resolve_overlapping_map_regions_keep_the_lower_or_larger():
    """GNU ld lets MEMORY regions overlap, as when a script declares one RAM twice, once for
    each bus. The import draws flat regions, so each overlapping one but the first is left out,
    with W112 only when part of it is lost."""
    linker = linker_file(
        LinkerRegion("BOOT", 0x08000000, 0x2000),  # inside FLASH, though listed first
        LinkerRegion("FLASH", 0x08000000, 0x10000),
        LinkerRegion("RAM_IBUS", 0x20000000, 0x5000),
        LinkerRegion("RAM_DBUS", 0x20000000, 0x5000),  # the same range: the first one stays
        LinkerRegion("TAIL", 0x20004000, 0x2000),  # starts inside RAM_IBUS, ends past it
    )
    resolved = resolve_memories(linker, [], 32)
    assert [m.id for m in resolved.memories] == ["flash", "ram_ibus"]
    assert resolved.notes == [
        "Left out of fw.map: BOOT (0x08000000, 8K): overlaps FLASH.",
        "Left out of fw.map: RAM_DBUS (0x20000000, 20K): overlaps RAM_IBUS.",
        "Left out of fw.map: TAIL (0x20004000, 8K): overlaps RAM_IBUS.",
    ]
    assert w112(resolved) == [
        ("tail", "to draw it instead of RAM_IBUS, give it with --region ram_ibus=0x20004000:8K:TAIL"),
    ]


def test_resolve_following_the_overlap_hint_draws_the_other_region():
    linker = linker_file(LinkerRegion("RAM", 0x20000000, 0x1000), LinkerRegion("RAM_ALIAS", 0x20000800, 0x10000))
    (hint,) = [d.hint for d in resolve_memories(linker, [], 32).diagnostics]
    spec = re.search(r"--region (\S+)", hint).group(1)
    resolved = resolve_memories(linker, [parse_region_spec(spec)], 32)
    assert rows(resolved) == [("ram", 0x20000800, 0x10000, None)]
    assert resolved.diagnostics == []  # RAM_ALIAS lies inside the --region that took its range


def test_resolve_region_spec_inside_a_map_region():
    """A new --region inside a map region leaves the map region out. Giving one of the specs
    the map region's id splits it instead, without a warning."""
    linker = linker_file(LinkerRegion("FLASH", 0x08000000, 0x10000), LinkerRegion("RAM", 0x20000000, 0x5000))
    specs = [parse_region_spec("boot=0x08000000:8K"), parse_region_spec("app=0x08002000:56K")]
    resolved = resolve_memories(linker, specs, 32)
    assert [m.id for m in resolved.memories] == ["ram", "boot", "app"]
    assert resolved.notes == ["Left out of fw.map: FLASH (0x08000000, 64K): overlaps --region boot."]
    assert w112(resolved) == [("flash", "to split FLASH, give one of the --region specs its id, flash")]
    specs = [parse_region_spec("flash=0x08000000:8K:Boot"), parse_region_spec("app=0x08002000:56K")]
    resolved = resolve_memories(linker, specs, 32)
    assert [m.id for m in resolved.memories] == ["flash", "ram", "app"]
    assert (resolved.notes, resolved.diagnostics) == ([], [])


def test_resolve_region_spec_around_a_map_region():
    linker = linker_file(LinkerRegion("FLASH", 0x08000000, 0x10000))
    resolved = resolve_memories(linker, [parse_region_spec("rom=0x08000000:1M")], 32)
    assert [m.id for m in resolved.memories] == ["rom"]
    assert resolved.notes == ["Left out of fw.map: FLASH (0x08000000, 64K): overlaps --region rom."]
    assert resolved.diagnostics == []  # FLASH lies inside it


@pytest.mark.parametrize(
    ("specs", "message"),
    [
        (["flash=0x08000000:64K", "flash=0x08000000:32K"], "--region flash is given twice"),
        (["a=0x08000000:64K", "b=0x08001000:4K"], "--region a and --region b overlap"),
    ],
)
def test_resolve_rejects_bad_region_specs(specs, message):
    linker = linker_file(LinkerRegion("FLASH", 0x08000000, 0x10000))
    with pytest.raises(ImportProblem, match=re.escape(message)):
        resolve_memories(linker, [parse_region_spec(spec) for spec in specs], 32)


GIVE_EACH = "give each region with --region ID=START:SIZE"
NOTHING_TO_DRAW = {
    "lld": (
        linker_file(kind="lld map"),
        "fw.map is an lld map, which lists no memory regions",
        GIVE_EACH + ", copying ORIGIN and LENGTH from the linker script's MEMORY block",
    ),
    "gnu without MEMORY": (
        linker_file(),
        "fw.map lists no memory regions, as its linker script has no MEMORY block",
        GIVE_EACH,
    ),
    "iar without ranges": (linker_file(kind="IAR map"), "fw.map lists no memory regions", GIVE_EACH),
    "no sizes": (
        linker_file(LinkerRegion("ER_RO", 0x08000000, None), LinkerRegion("ER_RW", 0x20000000, None),
                    LinkerRegion("ER_ZI", 0x20000000, None), kind="armlink map"),
        ("fw.map has no region memmap can draw (ER_RO (0x08000000): no size; ER_RW (0x20000000): "
        "no size; ER_ZI (0x20000000): no size)"),
        "give them with --region, such as --region er_ro=0x08000000:SIZE --region er_rw=0x20000000:SIZE",
    ),
    "none that fit": (
        linker_file(LinkerRegion("TOP", 0xFFFFF800, 0x1000), LinkerRegion("NONE", 0x0, 0)),
        ("fw.map has no region memmap can draw (TOP (0xFFFFF800, 4K): ends past the 32-bit "
        "address space; NONE (0x00000000, 0): size 0)"),
        GIVE_EACH,
    ),
}


@pytest.mark.parametrize(("linker", "message", "hint"), NOTHING_TO_DRAW.values(), ids=NOTHING_TO_DRAW)
def test_resolve_with_nothing_to_draw(linker, message, hint):
    with pytest.raises(ImportProblem) as raised:
        resolve_memories(linker, [], 32)
    assert (str(raised.value), raised.value.hint) == (message, hint)


def test_resolve_map_without_regions_uses_the_specs():
    specs = [parse_region_spec("flash=0x0:1M"), parse_region_spec("ram=0x20000000:256K")]
    resolved = resolve_memories(linker_file(kind="lld map"), specs, 32)
    assert rows(resolved) == [("flash", 0, 1 << 20, None), ("ram", 0x20000000, 256 << 10, None)]
    assert (resolved.notes, resolved.diagnostics) == (["fw.map is an lld map, which lists no memory regions."], [])


def test_resolve_one_w112_per_start_of_regions_without_a_size():
    linker = linker_file(LinkerRegion("ER_RO", 0x08000000, 0x10000), LinkerRegion("ER_RW", 0x20000000, None),
                         LinkerRegion("ER_ZI", 0x20000000, None))
    resolved = resolve_memories(linker, [], 32)
    assert w112(resolved) == [("er_rw", "give it with --region er_rw=0x20000000:SIZE")]
    assert len(resolved.notes) == 2  # both are noted
    resolved = resolve_memories(linker, [parse_region_spec("er_rw=0x20000000:20K")], 32)
    assert resolved.notes == ["Left out of fw.map: ER_ZI (0x20000000): no size."]
    assert resolved.diagnostics == []  # its start lies in er_rw, which is drawn


def test_resolve_notes_that_iar_regions_are_placement_ranges():
    resolved = resolve_memories(read_map(SNAPSHOTS / "iar8_stm32f030.map"), [], 32)
    assert resolved.notes == [
        ("iar8_stm32f030.map is an IAR map, which names no regions: these are its placement ranges, "
        "called ROM, RAM or Memory by what is placed there.")
    ]


def test_resolve_iar_ids_follow_address_order():
    """CCM RAM at 0x10000000 comes first, so the main SRAM is ram_2, and that is the id that
    replaces it."""
    linker = read_map(SNAPSHOTS / "iar8_stm32f407_ccm.map")
    assert [m.id for m in resolve_memories(linker, [], 32).memories] == ["rom", "ram", "ram_2"]
    resolved = resolve_memories(linker, [parse_region_spec("ram_2=0x20000000:128K:SRAM")], 32)
    assert [(m.id, m.name) for m in resolved.memories] == [("rom", "ROM"), ("ram", "RAM"), ("ram_2", "SRAM")]


# Importing with a map


def test_cli_map_matches_region_specs(tmp_path):
    by_region = tmp_path / "region.yaml"
    by_map = tmp_path / "map.yaml"
    assert invoke("from-elf", FIRMWARE, "-o", by_region, "--region", "flash=0x08000000:64K",
                  "--region", "ram=0x20000000:20K").exit_code == 0
    assert invoke("from-elf", FIRMWARE, "-o", by_map, "--map", FIRMWARE_MAP, "--strict").exit_code == 0
    body = [file.read_text().split("\n", 2)[2] for file in (by_region, by_map)]
    assert body[0] == body[1]
    assert "memory regions from firmware.map. Rename" in header(by_map)


def test_cli_64_bit_image_with_a_map(tmp_path):
    out = tmp_path / "out.yaml"
    result = invoke("from-elf", FIXTURES / "firmware64.elf", "-o", out, "--map", FIRMWARE_MAP, "--strict")
    assert result.exit_code == 0, result.output
    assert "      start: 0x0000000008000000\n      size: 64K" in out.read_text()


def test_regions_with_nothing_in_them_get_no_column(tmp_path):
    """The STM32F407 Keil map has a CCM RAM region that this image does not use."""
    elf = cortex_m(tmp_path)
    result = invoke("from-elf", elf, "--map", SNAPSHOTS / "keil_f407_compressed.map", "--strict")
    assert result.exit_code == 0, result.output
    text = (tmp_path / "fw.yaml").read_text()
    assert '- id: "rw_iram2"' in text  # still in memory
    assert 'root: "rw_iram2"' not in text
    assert "# No section lies in RW_IRAM2. To draw one, add a column with its id as root." in text


def test_cli_zephyr_map(tmp_path):
    """Zephyr declares IDT_LIST just below the top of memory for a build step of its own. It
    stays without a column, and so does not stretch the drawing."""
    elf = elf_file(tmp_path, [code(".text", 0x0, 0x1000), code(".data", 0x20000000, 0x100)])
    result = invoke("from-elf", elf, "--map", SNAPSHOTS / "zephyr_nrf52dk.map", "--strict")
    assert result.exit_code == 0, result.output
    text = (tmp_path / "fw.yaml").read_text()
    assert "    # No section lies in IDT_LIST. To draw one" in text
    assert [line.split(",")[1] for line in text.splitlines() if "root:" in line] == [
        ' root: "flash"',
        ' root: "sram"',
    ]


def test_cli_reports_left_out_regions(tmp_path):
    """A region left out is noted in the config and reported as W112, which --strict fails on."""
    top = "TOP              0xfffff800         0x00001000"
    map_file = write(tmp_path, "fw.map", GNU_MAP.replace("a_very_long_region_name 0x30000000  0x00000100", top))
    elf = elf_file(tmp_path, [code(".text", 0x08000000, 0x1000)])
    result = invoke("from-elf", elf, "--map", map_file, "--json")
    assert result.exit_code == 0, result.output
    assert warnings(result) == [
        ("W112", "top", "check that the map belongs to this ELF, or give it with --region top=0xFFFFF800:SIZE")
    ]
    assert "\n# Left out of fw.map: TOP (0xFFFFF800, 4K): ends past the 32-bit address space." in (
        header(tmp_path / "fw.yaml")
    )
    assert '- id: "top"' not in (tmp_path / "fw.yaml").read_text()
    strict = invoke("from-elf", elf, "--map", map_file, "--force", "--strict")
    assert strict.exit_code == 1
    assert "W112" in strict.output and "TOP (0xFFFFF800, 4K) in fw.map is left out" in strict.output


def test_cli_region_replaces_a_map_region(tmp_path):
    out = tmp_path / "out.yaml"
    result = invoke("from-elf", FIRMWARE, "-o", out, "--map", FIRMWARE_MAP, "--region", "flash=0x08000000:128K:Flash")
    assert result.exit_code == 0, result.output
    assert "memory regions from firmware.map and --region." in header(out)
    assert 'name: "Flash"\n      start: 0x08000000\n      size: 128K' in out.read_text()


def test_cli_regions_all_replaced_come_from_region(tmp_path):
    out = tmp_path / "out.yaml"
    result = invoke("from-elf", FIRMWARE, "-o", out, "--map", FIRMWARE_MAP, "--region", "flash=0x08000000:64K",
                    "--region", "ram=0x20000000:20K", "--strict")
    assert result.exit_code == 0, result.output
    assert "memory regions from --region. Rename" in header(out)


def test_cli_overlapping_regions_in_a_map(tmp_path):
    """One RAM declared twice loses nothing, so --strict passes."""
    map_file = write(tmp_path, "fw.map", gnu_map(
        "FLASH 0x08000000 0x00010000 xr", "RAM 0x20000000 0x00005000 xrw", "RAM_DBUS 0x20000000 0x00005000 rw"))
    result = invoke("from-elf", cortex_m(tmp_path), "--map", map_file, "--strict")
    assert result.exit_code == 0, result.output
    assert "# Left out of fw.map: RAM_DBUS (0x20000000, 20K): overlaps RAM." in header(tmp_path / "fw.yaml")


def test_cli_section_crossing_a_map_region(tmp_path):
    """A map region too small for a section is left out, and the import goes on."""
    map_file = write(tmp_path, "small.map", gnu_map("FLASH 0x08000000 0x00000100 xr", "RAM 0x20000000 0x5000 xrw"))
    result = invoke("from-elf", cortex_m(tmp_path), "--map", map_file, "--json")
    assert result.exit_code == 0, result.output
    assert warnings(result) == [
        ("W112", "flash", "give it with --region flash=0x08000000:SIZE, large enough to hold .text"),
        ("W110", "text", "check that the map belongs to this ELF, or add a --region that covers it"),
    ]
    note = "# Left out of small.map: FLASH (0x08000000, 256): .text (0x08000000 - 0x08000FFF) runs across its edge."
    assert note in header(tmp_path / "fw.yaml")


def test_cli_armlink_load_image_past_its_execution_region(tmp_path):
    """armlink places the RW load image after the RO code, inside the load region but past a
    tight execution region Max."""
    text = (
        "Memory Map of the image\n"
        "  Load Region LR_IROM1 (Base: 0x08000000, Size: 0x00001100, Max: 0x00080000, ABSOLUTE)\n"
        "    Execution Region ER_IROM1 (Exec base: 0x08000000, Load base: 0x08000000, Size: "
        "0x00000ff0, Max: 0x00001000, ABSOLUTE)\n"
        "    Execution Region RW_IRAM1 (Exec base: 0x20000000, Load base: 0x08000ff0, Size: "
        "0x00000100, Max: 0x00005000, ABSOLUTE)\n"
    )
    map_file = write(tmp_path, "keil.map", text)
    elf = tmp_path / "fw.elf"
    sections = [code(".text", 0x08000000, 0xFF0), code(".data", 0x20000000, 0x100, offset=0x2000)]
    elf.write_bytes(make_elf(sections, [(0x2000, 0x20000000, 0x08000FF0, 0x100, 0x100)]))  # .data loads at 0x08000FF0
    result = invoke("from-elf", elf, "--map", map_file, "--json")
    assert result.exit_code == 0, result.output
    assert [(code_, subject) for code_, subject, _ in warnings(result)] == [
        ("W112", "er_irom1"), ("W110", "text"), ("W110", "data_load"),
    ]


def test_cli_section_crossing_a_region_spec_still_stops(tmp_path):
    result = invoke("from-elf", cortex_m(tmp_path), "--region", "flash=0x08000000:256", "--json")
    message, hint = usage_problem(result)
    assert "crosses the edge of --region flash" in message and hint == "check its start and size"


def test_cli_map_of_another_image(tmp_path):
    """Every section outside the map's regions points to the wrong map."""
    elf = elf_file(tmp_path, [code(".text", 0x0, 0x1000), code(".data", 0x10000000, 0x100)])
    result = invoke("from-elf", elf, "--map", FIRMWARE_MAP, "--json")
    hint = "check that the map belongs to this ELF, or add a --region that covers it"
    assert warnings(result) == [("W110", "text", hint), ("W110", "data", hint)]
    assert invoke("from-elf", elf, "--map", FIRMWARE_MAP, "--force", "--strict").exit_code == 1


def test_sections_outside_the_iar_regions(tmp_path):
    """IAR lists only the ranges it placed content in, so a section placed at a fixed address
    outside them, such as an i.MX RT boot header, is outside every region."""
    elf = elf_file(tmp_path, [code(".flash_conf", 0x08000400, 0x200), code(".text", 0x08001000, 0x2000),
                              code(".data", 0x20080000, 0x100)])
    result = invoke("from-elf", elf, "--map", SNAPSHOTS / "iar8_rt685.map", "--json")
    assert [(code_, subject) for code_, subject, _ in warnings(result)] == [("W110", "flash_conf")]
    assert "# iar8_rt685.map is an IAR map, which names no regions" in header(tmp_path / "fw.yaml")


# Maps without enough to go on


def test_cli_lld_map_needs_region(tmp_path):
    map_file = write(tmp_path, "fw.map", LLD_HEADER)
    out = tmp_path / "out.yaml"
    message, hint = usage_problem(invoke("from-elf", cortex_m(tmp_path), "-o", out, "--map", map_file, "--json"))
    assert message == "fw.map is an lld map, which lists no memory regions"
    assert hint.startswith("give each region with --region ID=START:SIZE, copying ORIGIN and LENGTH")
    assert not out.exists()
    text = invoke("from-elf", tmp_path / "fw.elf", "-o", out, "--map", map_file)
    assert text.exit_code == 2
    assert text.output == f"memmap: {message}\n  hint: {hint}\n"
    result = invoke("from-elf", tmp_path / "fw.elf", "-o", out, "--map", map_file, "--region", "flash=0x08000000:64K",
                    "--region", "ram=0x20000000:20K", "--strict")
    assert result.exit_code == 0, result.output
    assert header(out).endswith(
        "memory regions from --region. Rename, merge or annotate blocks freely.\n"
        "# fw.map is an lld map, which lists no memory regions."
    )


def test_cli_map_without_memory_needs_region(tmp_path):
    map_file = write(tmp_path, "fw.map", gnu_map())
    message, hint = usage_problem(invoke("from-elf", cortex_m(tmp_path), "--map", map_file, "--json"))
    assert message.endswith("as its linker script has no MEMORY block") and hint == GIVE_EACH


def test_cli_map_without_sizes_suggests_the_regions_to_give(tmp_path):
    """Keil without a scatter file gives each region a start but no size. The suggested
    --region specs, with sizes filled in, are what it takes, and pass --strict."""
    elf = cortex_m(tmp_path)
    keil = SNAPSHOTS / "keil_no_scatter.map"
    _, hint = usage_problem(invoke("from-elf", elf, "--map", keil, "--json"))
    suggested = re.findall(r"--region (\S+):SIZE", hint)
    assert suggested == ["er_ro=0x08000000", "er_rw=0x20000000"]
    specs = [arg for spec, size in zip(suggested, ("64K", "20K")) for arg in ("--region", f"{spec}:{size}")]
    result = invoke("from-elf", elf, "--map", keil, *specs, "--strict")
    assert result.exit_code == 0, result.output
    assert "# Left out of keil_no_scatter.map: ER_ZI (0x20000000): no size." in header(tmp_path / "fw.yaml")


def test_cli_map_missing_one_size_names_it(tmp_path):
    """With flash given and RAM still without a size, W112 says which --region is missing."""
    result = invoke("from-elf", cortex_m(tmp_path), "--map", SNAPSHOTS / "keil_no_scatter.map",
                    "--region", "er_ro=0x08000000:64K", "--json")
    assert warnings(result) == [
        ("W112", "er_rw", "give it with --region er_rw=0x20000000:SIZE"),
        ("W110", "data", "add a --region that covers it, or check the start and size of yours"),
    ]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--map", "missing.map"], "cannot read missing.map"),
        (["--map", "tests"], "cannot read tests"),
        (["--map", "tests/fixtures/elf_src/firmware.ld"], "firmware.ld is a linker script"),
        (["--map", "tests/fixtures/firmware.map", "--region", "flash=0x08000000:64K", "--region",
          "flash=0x08000000:32K"], "--region flash is given twice"),
        (["--map", "tests/fixtures/firmware.map", "--region", "a=0x0:1K", "--region", "b=0x100:1K"],
         "--region a and --region b overlap"),
    ],
)
def test_cli_map_usage_problems(tmp_path, monkeypatch, args, message):
    monkeypatch.chdir(ROOT)
    result = invoke("from-elf", FIRMWARE, "-o", tmp_path / "out.yaml", *args, "--json")
    assert message in usage_problem(result)[0]
    assert not (tmp_path / "out.yaml").exists()


def test_cli_bad_iar_number_is_a_usage_problem(tmp_path):
    map_file = write(tmp_path, "fw.map", "# IAR ELF Linker\n*** PLACEMENT SUMMARY\n\"P1\": place in [from 0x' to 0x1] { rw };\n")
    message, _ = usage_problem(invoke("from-elf", FIRMWARE, "-o", tmp_path / "out.yaml", "--map", map_file, "--json"))
    assert message == "cannot read the address \"0x'\" in the placement summary"


def test_cli_crash_still_prints_one_json_object(tmp_path, monkeypatch):
    def broken(path):
        raise RuntimeError("boom")

    monkeypatch.setattr("memmap.cli.read_map", broken)
    result = invoke("from-elf", FIRMWARE, "-o", tmp_path / "out.yaml", "--map", FIRMWARE_MAP, "--json")
    assert result.exit_code == 3
    (diagnostic,) = json.loads(result.stdout)["diagnostics"]
    assert (diagnostic["code"], diagnostic["message"]) == ("E091", "internal error: boom")
