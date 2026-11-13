"""`memmap from-partitions`: ESP-IDF partition tables, CSV and binary.

The real tables are in tests/fixtures/esp_partitions/, see its refresh.py. For each build there,
the partition-table.bin that ESP-IDF wrote is the ground truth for placing its CSV. Configs and
two drawings are compared with tests/golden/partitions/. Regenerate after an intended change
with:

    MEMMAP_UPDATE_GOLDEN=1 uv run pytest tests/test_from_partitions.py
"""

import hashlib
import json
import os
import struct

import pytest
from click.testing import CliRunner
from conftest import ROOT, codes, drawn

from memmap.cli import app
from memmap.from_elf import ImportProblem
from memmap.from_partitions import esp_idf, import_table, read_sdkconfig, settings
from memmap.from_partitions.esp_idf import read_binary, read_csv
from memmap.pipeline import run
from memmap.render.svg import render_svg

FIXTURES = ROOT / "tests" / "fixtures" / "esp_partitions"
GOLDEN = ROOT / "tests" / "golden" / "partitions"
UPDATE = os.environ.get("MEMMAP_UPDATE_GOLDEN") == "1"
INDEX = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
BUILDS = [build["name"] for build in INDEX["builds"]]
TABLES = {table["name"]: table for table in INDEX["tables"]}
FOUR_MB = 4 * 1024 * 1024
# Builds whose drawing is a golden too: the one docs/from-partitions.md shows, and one with
# warnings. The drawing code is the same for every table, so more would only repeat them.
DRAWN = {"esp32_homekit_custom_type", "esp32h2_zigbee"}
runner = CliRunner()

# What each fixture's import reports. Everything else imports cleanly.
EXPECTED_CODES = {
    "esp32_small_nvs": ["W113"],  # an 8 KB read-write NVS, accepted by ESP-IDF 5.2
    "esp32h2_zigbee": ["W113", "W100", "W100"],  # app size 550K, a 1K partition
}


def compare(golden, text):
    """Check `text` against a golden file, or rewrite it with MEMMAP_UPDATE_GOLDEN=1."""
    if UPDATE:
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(text, encoding="utf-8", newline="\n")
    assert text == golden.read_text(encoding="utf-8")


def invoke(*args):
    return runner.invoke(app, [str(arg) for arg in args])


def entries(partitions):
    return [(p.name, p.type, p.subtype, p.offset, p.size, p.flags) for p in partitions]


def build_csv(name):
    """The CSV a build used: its own, or the one built into ESP-IDF that its sdkconfig names."""
    own = FIXTURES / "builds" / name / "partitions.csv"
    if own.exists():
        return own
    sdkconfig = (FIXTURES / "builds" / name / "sdkconfig").read_text()
    builtin = next(line for line in sdkconfig.splitlines() if "PARTITION_TABLE_FILENAME=" in line)
    stem = builtin.split("=")[1].strip('"').removesuffix(".csv")
    return FIXTURES / "tables" / f"idf_{stem}.csv"


def table_bytes(*rows):
    """A binary table of (name, type, subtype, offset, size, flags) rows, as ESP-IDF writes it."""
    data = b"".join(
        struct.pack("<2sBBLL16sL", b"\xaa\x50", t, s, o, size, n.encode(), f)
        for n, t, s, o, size, f in rows
    )
    data += b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(data).digest()
    return data + b"\xff" * (0xC00 - len(data))


def check_strict(text, tmp_path):
    """The codes the pipeline reports for generated text, so that `== []` passes --strict."""
    file = tmp_path / "layout.yaml"
    file.write_text(text, encoding="utf-8")
    return codes(run(file))


# The fixtures


def test_fixtures_are_listed():
    assert len(BUILDS) >= 5 and len(TABLES) >= 5
    for build in INDEX["builds"]:
        for file in build["files"]:
            assert (FIXTURES / "builds" / build["name"] / file).is_file()
    for name in TABLES:
        assert (FIXTURES / "tables" / f"{name}.csv").is_file()


@pytest.mark.parametrize("name", BUILDS)
def test_csv_places_like_the_build(name):
    """memmap places the CSV where ESP-IDF's build put it in partition-table.bin."""
    folder = FIXTURES / "builds" / name
    config = read_sdkconfig((folder / "sdkconfig").read_text())
    placed = read_csv(
        build_csv(name).read_text(), config.table_offset, config.bootloader_offset
    )
    built = read_binary((folder / "partition-table.bin").read_bytes())
    if name == "esp32_homekit_custom_type":
        # ESP-IDF before 5.x read a blank subtype as 0; it is now 0x06, undefined.
        assert placed[0].subtype == 0x06 and built[0].subtype == 0x00
        built[0].subtype = 0x06
    assert entries(placed) == entries(built)


@pytest.mark.parametrize("name", BUILDS)
@pytest.mark.parametrize("source", ["csv", "bin"])
def test_build_imports_to_the_golden_config_and_drawing(name, source, tmp_path):
    folder = FIXTURES / "builds" / name
    config = read_sdkconfig((folder / "sdkconfig").read_text())
    path = build_csv(name) if source == "csv" else folder / "partition-table.bin"
    imported = import_table(path.read_bytes(), path.name, settings(None, None, config))
    found = [d.code for d in imported.diagnostics] + check_strict(imported.text, tmp_path)
    expected = EXPECTED_CODES.get(name, [])
    if source == "bin":  # the build checked the binary, so memmap only adds its sector check
        expected = [code for code in expected if code != "W113"]
    assert found == expected
    if source == "csv":
        compare(GOLDEN / f"{name}.yaml", imported.text)
        if name in DRAWN:
            compare(GOLDEN / f"{name}.svg", render_svg(drawn(run(tmp_path / "layout.yaml"))))


@pytest.mark.parametrize("name", BUILDS)
def test_csv_and_binary_draw_the_same_blocks(name, tmp_path):
    folder = FIXTURES / "builds" / name
    config = read_sdkconfig((folder / "sdkconfig").read_text())
    blocks = []
    for path in (build_csv(name), folder / "partition-table.bin"):
        imported = import_table(path.read_bytes(), "table", settings(None, None, config))
        file = tmp_path / "layout.yaml"
        file.write_text(imported.text)
        result = run(file)
        blocks.append([(n.id, n.start, n.end) for n in result.tree.index["flash"].children])
    assert blocks[0] == blocks[1]


@pytest.mark.parametrize("name", sorted(TABLES))
def test_table_imports_to_the_golden_config(name, tmp_path):
    table = TABLES[name]
    path = FIXTURES / "tables" / f"{name}.csv"
    flash = esp_idf.parse_int(table["flash_size"])
    # The bootloader offset of an ESP32, which the efuse example's bootloader entry needs.
    config = read_sdkconfig('CONFIG_IDF_TARGET="esp32"\n')
    imported = import_table(path.read_bytes(), path.name, settings(flash, None, config))
    found = [d.code for d in imported.diagnostics] + check_strict(imported.text, tmp_path)
    assert found == EXPECTED_CODES.get(name, [])
    compare(GOLDEN / f"{name}.yaml", imported.text)


def test_two_ota_table_in_detail():
    partitions = read_csv((FIXTURES / "tables" / "idf_partitions_two_ota.csv").read_text(), 0x8000)
    assert [(p.name, p.type_name, p.subtype_name, p.offset, p.size) for p in partitions] == [
        ("nvs", "data", "nvs", 0x9000, 0x4000),
        ("otadata", "data", "ota", 0xD000, 0x2000),
        ("phy_init", "data", "phy", 0xF000, 0x1000),
        ("factory", "app", "factory", 0x10000, 0x100000),
        ("ota_0", "app", "ota_0", 0x110000, 0x100000),
        ("ota_1", "app", "ota_1", 0x210000, 0x100000),
    ]


def test_bootloader_and_table_entries_take_their_fixed_offsets():
    text = (FIXTURES / "tables" / "idf_ota_efuse_emul.csv").read_text()
    partitions = read_csv(text, 0xD000, bootloader=0x2000)
    by_name = {p.name: p for p in partitions}
    assert (by_name["PrimaryBTLDR"].offset, by_name["PrimaryBTLDR"].size) == (0x2000, 0xB000)
    assert (by_name["PrimaryPrtTable"].offset, by_name["PrimaryPrtTable"].size) == (0xD000, 0x1000)
    assert by_name["nvs"].offset == 0xE000  # follows the table, not the bootloader entry
    assert by_name["storage"].flags == ("encrypted",)


# Reading a CSV


def place(text, table_offset=0x8000, **offsets):
    return [(p.name, p.offset, p.size) for p in read_csv(text, table_offset, **offsets)]


def test_blank_offsets_align_apps_to_64k_and_the_rest_to_4k():
    text = "a, data, nvs, , 0x3000\nb, app, factory, , 1M\nc, 0x40, 0x01, , 100\nd, data, , , 4K\n"
    assert place(text) == [
        ("a", 0x9000, 0x3000),
        ("b", 0x10000, 0x100000),
        ("c", 0x110000, 100),
        ("d", 0x111000, 0x1000),
    ]


@pytest.mark.parametrize(
    ("size", "value"),
    [
        ("0x1000", 0x1000),
        ("4096", 4096),
        ("4K", 4096),
        ("4k", 4096),
        ("1M", 0x100000),
        ("1m", 0x100000),
        ("0x10K", 0x4000),
        ("1_024", 1024),
        ("0o10000", 4096),
    ],
)
def test_numbers_read_like_the_build(size, value):
    assert place(f"a, data, nvs, 0x9000, {size}\n") == [("a", 0x9000, value)]


def test_negative_size_gives_the_end():
    assert place("a, data, nvs, 0x9000, -0x10000\n") == [("a", 0x9000, 0x7000)]


def test_comments_blank_lines_byte_order_mark_and_line_ends():
    data = b"\xef\xbb\xbf# Name, Type\r\n\r\n   # indented comment\r\nnvs,data,nvs,0x9000,0x6000,\r\n"
    assert place(esp_idf.decode(data)) == [("nvs", 0x9000, 0x6000)]
    for encoding in ("utf-16", "utf-32", "utf-8-sig"):
        decoded = esp_idf.decode("nvs, data, nvs, , 0x6000\n".encode(encoding))
        assert place(decoded) == [("nvs", 0x9000, 0x6000)]


def test_names_flags_and_subtypes():
    text = (
        "keys, data, nvs_keys, , 4K, encrypted:readonly\n"
        "Ota, APP, OTA_3, , 64K\n"
        "custom, data, mine, , 4K\n"
        "raw, 0x40, 0x07, , 4K\n"
        "blank, data, , , 4K\n"
        "extra, data, fat, , 4K, , ignored\n"
    )
    partitions = read_csv(text, 0x8000)
    assert [(p.type_name, p.subtype_name, p.flags) for p in partitions] == [
        ("data", "nvs_keys", ("encrypted", "readonly")),
        ("app", "ota_3", ()),
        ("data", "mine", ()),  # registered by a component: kept as written
        ("0x40", "0x07", ()),
        ("data", "undefined", ()),
        ("data", "fat", ()),
    ]
    assert partitions[2].subtype is None


@pytest.mark.parametrize(
    ("text", "message", "hint"),
    [
        ("nvs, , nvs, , 4K\n", "line 1: the type is blank", "Name, Type"),
        ("nvs, code, nvs, , 4K\n", "unknown type 'code'", "app, data"),
        ("Name, Type, SubType, Offset, Size\n", "unknown type 'Type'", "header line with #"),
        ("a, 0x100, 0, , 4K\n", "not between 0 and 0xFF", None),
        ("a, data, 0x100, , 4K\n", "not between 0 and 0xFF", "Name, Type"),
        ("app, app, , , 64K\n", "an app partition needs a subtype", None),
        ("a, data, nvs, 0x9000, \n", "line 1: the size is blank", None),
        ("a, data, nvs, 9000h, 4K\n", "bad offset '9000h'", None),
        ("a, data, nvs, , 4 KB\n", "bad size '4 KB'", None),
        ("a, data, nvs, , 4K, secret\n", "unknown flag 'secret'", "encrypted and readonly"),
        ("a, data, nvs, , $SIZE\n", "$SIZE is an environment variable", "replace it"),
        ("a, data, nvs, , ${SIZE}\n", "${SIZE} is an environment variable", None),
        ("a, data, nvs, 0x8000, 4K\n", "starts at 0x8000, but the partition table ends at 0x9000", "0x9000 or higher"),
        (
            "a, data, nvs, 0x9000, 8K\nb, data, nvs, 0xa000, 4K\n",
            "b starts at 0xa000, but a, listed before it, runs to 0xafff",
            "make a smaller",
        ),
        (
            "a, data, nvs, 0x10000, 4K\nb, data, nvs, 0x9000, 4K\n",
            "it is listed after a, which starts higher",
            "address order",
        ),
        ("a, data, nvs, 0x9000, 0\n", "has size 0", None),
        ("b, bootloader, primary, , \n", "bootloader offset, which is not known", "--sdkconfig"),
        ("b, bootloader, recovery, , \n", "recovery bootloader offset", "RECOVERY_OFFSET"),
    ],
)
def test_bad_csv(text, message, hint):
    with pytest.raises(ImportProblem) as caught:
        read_csv(text, 0x8000, bootloader=0x1000 if "recovery" in text else None)
    assert message in str(caught.value)
    if hint:
        assert hint in caught.value.hint


def test_an_escaped_dollar_and_a_comment_with_a_variable_are_kept():
    assert place("# see $IDF_PATH\na, data, nvs, , 4K\n") == [("a", 0x9000, 4096)]
    assert place("a\\$b, data, nvs, , 4K\n") == [("a\\$b", 0x9000, 4096)]


def test_text_that_is_not_utf8():
    with pytest.raises(ImportProblem, match="neither UTF-8"):
        esp_idf.decode(b"nvs, data, \xff\xfe\xfa")


# Reading a binary table


def test_binary_round_trip_of_names_types_and_flags():
    data = table_bytes(
        ("nvs", 1, 2, 0x9000, 0x6000, 0),
        ("sixteen_chars_ab", 0, 0x10, 0x10000, 0x100000, 0b11),
        ("raw", 0x40, 0x99, 0x110000, 0x1000, 0b100),  # an unknown flag bit is ignored
    )
    assert esp_idf.is_binary(data)
    partitions = read_binary(data)
    assert [(p.name, p.type_name, p.subtype_name, p.flags) for p in partitions] == [
        ("nvs", "data", "nvs", ()),
        ("sixteen_chars_ab", "app", "ota_0", ("encrypted", "readonly")),
        ("raw", "0x40", "0x99", ()),
    ]


def test_binary_without_checksum_is_read():
    row = struct.pack("<2sBBLL16sL", b"\xaa\x50", 1, 2, 0x9000, 0x6000, b"nvs", 0)
    assert [p.name for p in read_binary(row + b"\xff" * 32)] == ["nvs"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda data: data[:0x30] + b"x" + data[0x31:], "MD5 checksum does not match"),
        (lambda data: data[:0x40], "no end marker"),
        (lambda data: data[:0x50], "ends inside an entry"),
        (lambda data: data[:0x20] + b"\x12\x34" + data[0x22:], "entry 2 of the binary table"),
    ],
)
def test_bad_binary(change, message):
    data = table_bytes(("nvs", 1, 2, 0x9000, 0x6000, 0), ("app", 0, 0, 0x10000, 0x10000, 0))
    with pytest.raises(ImportProblem, match=message):
        read_binary(change(data))


# The sdkconfig


def test_sdkconfig_values():
    config = read_sdkconfig(
        "# comment\n"
        'CONFIG_IDF_TARGET="esp32s3"\n'
        'CONFIG_ESPTOOLPY_FLASHSIZE="16MB"\n'
        "CONFIG_PARTITION_TABLE_OFFSET=0x10000\n"
        "# CONFIG_BOOTLOADER_OFFSET_IN_FLASH is not set\n"
        "CONFIG_BOOTLOADER_RECOVERY_OFFSET=0x3f0000\n"
    )
    assert (config.flash_size, config.table_offset, config.recovery_offset) == (
        16 * 1024 * 1024,
        0x10000,
        0x3F0000,
    )
    assert (config.bootloader_offset, config.bootloader_from) == (0, "CONFIG_IDF_TARGET esp32s3")


@pytest.mark.parametrize(
    ("lines", "offset"),
    [
        ('CONFIG_IDF_TARGET="esp32"', 0x1000),
        ('CONFIG_IDF_TARGET="esp32s2"', 0x1000),
        ('CONFIG_IDF_TARGET="esp32c3"', 0),
        ('CONFIG_IDF_TARGET="esp32p4"', 0x2000),
        ('CONFIG_IDF_TARGET="esp32"\nCONFIG_BOOTLOADER_OFFSET_IN_FLASH=0x0', 0),
        ("", None),
    ],
)
def test_sdkconfig_bootloader_offset(lines, offset):
    assert read_sdkconfig(lines).bootloader_offset == offset


@pytest.mark.parametrize(
    ("value", "size"),
    [('"4MB"', FOUR_MB), ('"512KB"', 512 * 1024), ('"detect"', None), ("", None)],
)
def test_sdkconfig_flash_size(value, size):
    line = f"CONFIG_ESPTOOLPY_FLASHSIZE={value}\n" if value else ""
    assert read_sdkconfig(line).flash_size == size


def test_sdkconfig_secure_boot_v1():
    v1 = "CONFIG_SECURE_BOOT=y\nCONFIG_SECURE_BOOT_V1_ENABLED=y\n"
    assert read_sdkconfig(v1).secure_boot_v1
    assert not read_sdkconfig(v1 + "CONFIG_SECURE_BOOT_ALLOW_SHORT_APP_PARTITION=y\n").secure_boot_v1
    assert not read_sdkconfig("CONFIG_SECURE_BOOT=y\nCONFIG_SECURE_BOOT_V2_ENABLED=y\n").secure_boot_v1


# Importing


def import_text(text, flash=FOUR_MB, table=None, sdkconfig="", **kwargs):
    config = read_sdkconfig(sdkconfig) if sdkconfig else None
    return import_table(text.encode(), "partitions.csv", settings(flash, table, config), **kwargs)


def children(imported, tmp_path):
    """(id, name, start, size, kind) of each block in the flash region, in address order."""
    file = tmp_path / "layout.yaml"
    file.write_text(imported.text)
    result = run(file)
    assert result.ok, codes(result)
    flash = result.tree.index["flash"]
    return [(n.id, n.name, n.start, n.size, n.kind) for n in flash.children]


def test_layout_without_sdkconfig(tmp_path):
    imported = import_text("nvs, data, nvs, , 0x6000\nfactory, app, factory, , 1M\n")
    assert children(imported, tmp_path) == [
        ("bootloader", "Bootloader", 0, 0x8000, "normal"),
        ("partition_table", "Partition table", 0x8000, 0x1000, "normal"),
        ("nvs", "nvs", 0x9000, 0x6000, "normal"),
        ("free", "Free\n4 KB", 0xF000, 0x1000, "reserved"),
        ("factory", "factory", 0x10000, 0x100000, "normal"),
        ("free_2", "Free\n3008 KB", 0x110000, 0x2F0000, "reserved"),
    ]
    assert "from 0x0, 0x1000 or 0x2000, by chip" in imported.text
    assert "Bootloader offset not known" in imported.text
    assert "Flash size 4 MB, from --flash-size." in imported.text
    assert "Partition table at 0x8000, from the default." in imported.text


def test_layout_of_an_esp32_reserves_the_space_below_the_bootloader(tmp_path):
    imported = import_text("nvs, data, nvs, , 0x6000\n", sdkconfig='CONFIG_IDF_TARGET="esp32"\n')
    found = children(imported, tmp_path)
    assert found[:2] == [
        ("reserved", "Reserved", 0, 0x1000, "reserved"),
        ("bootloader", "Bootloader", 0x1000, 0x7000, "normal"),
    ]
    assert "Bootloader at 0x1000, from CONFIG_IDF_TARGET esp32 in sdkconfig." in imported.text


def test_partition_named_like_a_block_gets_its_own_id(tmp_path):
    imported = import_text("flash, data, nvs, , 0x6000\nbootloader, data, fat, , 4K\nfree, data, fat, , 4K\n")
    ids = [block[0] for block in children(imported, tmp_path)]
    assert len(ids) == len(set(ids))
    assert {"flash_2", "bootloader_2", "free_2"} <= set(ids)


def test_full_flash_has_no_free_block(tmp_path):
    imported = import_text("a, data, fat, , -0x400000\n")  # a negative size gives the end
    ids = [block[0] for block in children(imported, tmp_path)]
    assert ids == ["bootloader", "partition_table", "a"]
    assert '"Flash\\n4 MB of 4 MB (100%)"' in imported.text


def test_table_offset_option_and_sdkconfig(tmp_path):
    text = "nvs, data, nvs, , 0x6000\n"
    sdkconfig = 'CONFIG_ESPTOOLPY_FLASHSIZE="2MB"\nCONFIG_PARTITION_TABLE_OFFSET=0xF000\n'
    imported = import_text(text, flash=None, sdkconfig=sdkconfig)
    assert "nvs\", start: 0x00010000" in imported.text
    assert "Flash size 2 MB, from CONFIG_ESPTOOLPY_FLASHSIZE in sdkconfig." in imported.text
    imported = import_text(text, flash=FOUR_MB, table=0x6000, sdkconfig=sdkconfig)
    assert "nvs\", start: 0x00007000" in imported.text  # the options win
    assert "Partition table at 0x6000, from --table-offset." in imported.text


@pytest.mark.parametrize(
    ("text", "flash", "table", "sdkconfig", "message", "hint"),
    [
        ("a, data, nvs, , 4K\n", None, None, "", "the flash size is not known", "--sdkconfig"),
        ("a, data, nvs, , 4K\n", None, None, 'CONFIG_IDF_TARGET="esp32"\n', "does not set CONFIG_ESPTOOLPY_FLASHSIZE", "--flash-size"),
        ("a, data, nvs, , 4K\n", None, None, 'CONFIG_ESPTOOLPY_FLASHSIZE="detect"\n', "'detect', which is not a size", "--flash-size"),
        ("a, app, factory, , 4M\n", FOUR_MB, None, "", "past the end of the 4 MB flash", "flash size"),
        ("a, data, nvs, , 4K\n", FOUR_MB, 0x8800, "", "0x8800 from --table-offset is not a multiple", None),
        ("a, data, nvs, , 4K\n", FOUR_MB, 0x8000, "CONFIG_BOOTLOADER_OFFSET_IN_FLASH=0x9000\n", "not below the partition table", None),
    ],
)
def test_import_problems(text, flash, table, sdkconfig, message, hint):
    with pytest.raises(ImportProblem) as caught:
        import_text(text, flash, table, sdkconfig)
    assert message in str(caught.value)
    if hint:
        assert hint in caught.value.hint


def test_missing_flash_size_suggests_the_sdkconfig_nearby():
    with pytest.raises(ImportProblem) as caught:
        import_text("a, data, nvs, , 4K\n", None, near_sdkconfig="project/sdkconfig")
    assert caught.value.hint.startswith("pass --sdkconfig project/sdkconfig")


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([("a", 1, 2, 0x8000, 0x1000, 0)], "starts at 0x8000, below the end of the partition table"),
        ([("a", 1, 2, 0x9000, 0x2000, 0), ("b", 1, 2, 0xA000, 0x1000, 0)], "b (0xa000 - 0xafff) overlaps a"),
    ],
)
def test_binary_that_cannot_be_drawn(rows, message):
    with pytest.raises(ImportProblem, match=message.replace("(", r"\(").replace(")", r"\)")):
        import_table(table_bytes(*rows), "t.bin", settings(FOUR_MB, None))


def test_binary_gives_its_own_table_and_bootloader_offsets():
    data = table_bytes(
        ("boot", 2, 0, 0x2000, 0xE000, 0),
        ("table", 3, 0, 0x10000, 0x1000, 0),
        ("nvs", 1, 2, 0x11000, 0x6000, 0),
    )
    imported = import_table(data, "t.bin", settings(FOUR_MB, None))
    assert "Partition table at 0x10000, from its own entry in the table." in imported.text
    assert "Bootloader at 0x2000, from its own entry in the table." in imported.text
    assert '"Reserved", kind: reserved, start: 0x00000000, size: 8K' in imported.text
    assert '"Bootloader"' not in imported.text and '"Partition table"' not in imported.text


@pytest.mark.parametrize(
    ("text", "problem", "hint"),
    [
        ("a, data, nvs, 0x9800, 0x3000\n", "starts at 0x9800, not on a 4 KB boundary", "0xa000"),
        ("a, app, factory, 0x18000, 64K\n", "not on a 64 KB boundary", "0x20000"),
        ("a, app, factory, , 0x10800\n", "has size 0x10800, not a multiple of 4 KB", "0x11000"),
        ("n, data, nvs, , 8K\n", "read-write NVS partition of 8 KB", "readonly"),
        ("o, data, ota, , 8K, readonly\n", "is ota data marked readonly", "remove"),
        ("c, data, coredump, , 64K, readonly\n", "is coredump data marked readonly", "remove"),
        ("o, data, ota, , 4K\n", "otadata (data, ota) partition of 0x1000 bytes", "0x2000"),
        ("o, data, ota, , 8K\np, data, ota, , 8K\n", "one of 2 otadata", "only one"),
        ("t, data, tee_ota, , 4K\n", "TEE otadata", "0x2000"),
        ("a, data, fat, , 4K\na, data, fat, , 4K\n", "shares its name with 1 more", "own name"),
    ],
)
def test_esp_idf_rules(text, problem, hint):
    diagnostics = import_text(text).diagnostics
    assert diagnostics, text
    assert diagnostics[0].code == "W113"
    assert problem in diagnostics[0].message and "which current ESP-IDF rejects" in diagnostics[0].message
    assert hint in diagnostics[0].hint
    assert diagnostics[0].subject


def test_rules_that_hold():
    text = "n, data, nvs, , 8K, readonly\no, data, ota, , 8K\na, app, factory, , 0x10800\n"
    assert [d.message for d in import_text(text).diagnostics] == [
        "line 3 (a) has size 0x10800, not a multiple of 4 KB, which current ESP-IDF rejects"
    ]


def test_secure_boot_v1_needs_app_sizes_in_64k():
    sdkconfig = "CONFIG_SECURE_BOOT=y\nCONFIG_SECURE_BOOT_V1_ENABLED=y\n"
    diagnostics = import_text("a, app, factory, , 68K\n", sdkconfig=sdkconfig).diagnostics
    assert "not a multiple of 64 KB" in diagnostics[0].message


def test_binary_skips_the_rules():
    data = table_bytes(("n", 1, 2, 0x9000, 0x2000, 0))  # an 8 KB read-write NVS
    assert import_table(data, "t.bin", settings(FOUR_MB, None)).diagnostics == []


# The command


def write_project(tmp_path, sdkconfig='CONFIG_ESPTOOLPY_FLASHSIZE="4MB"\nCONFIG_IDF_TARGET="esp32c3"\n'):
    table = tmp_path / "partitions.csv"
    table.write_text("nvs, data, nvs, , 0x6000\nfactory, app, factory, , 1M\n")
    if sdkconfig is not None:
        (tmp_path / "sdkconfig").write_text(sdkconfig)
    return table


def test_command_writes_next_to_the_table(tmp_path):
    table = write_project(tmp_path)
    result = invoke("from-partitions", table, "--sdkconfig", tmp_path / "sdkconfig")
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == str(tmp_path / "partitions.yaml")
    text = (tmp_path / "partitions.yaml").read_text()
    assert "Bootloader at 0x0, from CONFIG_IDF_TARGET esp32c3 in sdkconfig." in text
    assert invoke("check", tmp_path / "partitions.yaml", "--strict").exit_code == 0
    again = invoke("from-partitions", table, "--sdkconfig", tmp_path / "sdkconfig")
    assert again.exit_code == 2 and "use --force" in again.stderr
    assert invoke("from-partitions", table, "--flash-size", "4M", "--force").exit_code == 0


def test_command_json(tmp_path):
    table = write_project(tmp_path)
    table.write_text("n, data, nvs, , 8K\n")
    out = tmp_path / "layout.yaml"
    result = invoke("from-partitions", table, "-o", out, "--flash-size", "2M", "--json", "--strict")
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["ok"] is False and data["output"] == str(out)
    assert [d["code"] for d in data["diagnostics"]] == ["W113"]
    assert data["diagnostics"][0]["subject"] == "n"


def test_command_hints_at_the_sdkconfig_next_to_the_table(tmp_path):
    table = write_project(tmp_path)
    result = invoke("from-partitions", table, "--json")
    assert result.exit_code == 2
    diagnostic = json.loads(result.stdout)["diagnostics"][0]
    assert diagnostic["code"] == "E090"
    assert "the flash size is not known" in diagnostic["message"]
    assert diagnostic["hint"].startswith(f"pass --sdkconfig {tmp_path / 'sdkconfig'}")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--flash-size", "four"], "bad --flash-size"),
        (["--table-offset", "-1"], "bad --table-offset"),
        (["--sdkconfig", "missing"], "cannot read missing"),
        (["-o", "partitions.csv"], "would overwrite the partition table"),
    ],
)
def test_command_usage_problems(tmp_path, monkeypatch, args, message):
    monkeypatch.chdir(tmp_path)
    write_project(tmp_path)
    result = invoke("from-partitions", "partitions.csv", *args)
    assert result.exit_code == 2
    assert message in result.stderr


def test_command_on_a_missing_table(tmp_path):
    result = invoke("from-partitions", tmp_path / "nope.csv", "--flash-size", "4M")
    assert result.exit_code == 2 and "cannot read" in result.stderr


def test_command_on_a_binary_table(tmp_path):
    table = tmp_path / "partition-table.bin"
    table.write_bytes(table_bytes(("nvs", 1, 2, 0x9000, 0x6000, 0)))
    result = invoke("from-partitions", table, "--flash-size", "4M", "--strict")
    assert result.exit_code == 0, result.output
    text = (tmp_path / "partition-table.yaml").read_text()
    assert "an ESP-IDF binary partition table" in text


# Found in review


def test_binary_entries_agree_with_the_sdkconfig():
    data = table_bytes(("table", 3, 0, 0x10000, 0x1000, 0), ("nvs", 1, 2, 0x11000, 0x6000, 0))
    agrees = read_sdkconfig("CONFIG_PARTITION_TABLE_OFFSET=0x10000\n")
    imported = import_table(data, "t.bin", settings(FOUR_MB, None, agrees))
    assert "Partition table at 0x10000, from CONFIG_PARTITION_TABLE_OFFSET in sdkconfig." in imported.text
    disagrees = read_sdkconfig("CONFIG_PARTITION_TABLE_OFFSET=0x8000\n")
    with pytest.raises(ImportProblem) as caught:
        import_table(data, "t.bin", settings(FOUR_MB, None, disagrees))
    assert "own partition table entry is at 0x10000, but CONFIG_PARTITION_TABLE_OFFSET" in str(caught.value)
    assert "belongs to the build" in caught.value.hint


def test_binary_bootloader_entry_against_the_option():
    data = table_bytes(("boot", 2, 0, 0x1000, 0x7000, 0), ("nvs", 1, 2, 0x9000, 0x6000, 0))
    with pytest.raises(ImportProblem, match="own bootloader entry is at 0x1000, but --bootloader-offset says 0x0"):
        import_table(data, "t.bin", settings(FOUR_MB, None, bootloader_offset=0))


def test_unknown_subtype_name_is_a_warning_and_a_bad_number_an_error():
    diagnostics = import_text("nvs, data, nvss, , 24K\n").diagnostics
    assert [d.code for d in diagnostics] == ["W113"]
    assert diagnostics[0].message == (
        "line 1 (nvs) has subtype 'nvss', which current ESP-IDF rejects unless a component "
        "registers it"
    )
    with pytest.raises(ImportProblem, match="bad subtype '0xZZ'"):
        read_csv("a, data, 0xZZ, , 4K\n", 0x8000)


@pytest.mark.parametrize(
    ("text", "flash", "table", "message"),
    [
        ("# empty\n", 8 * 1024, None, "partition table at 0x8000 runs past the end of the 8 KB flash"),
        ("", FOUR_MB, 0x400000, "runs past the end of the 4 MB flash"),
        ("a, data, nvs, , 24K\n", FOUR_MB, 0, "leaves no room for the bootloader"),
    ],
)
def test_generated_blocks_must_fit(text, flash, table, message):
    with pytest.raises(ImportProblem, match=message):
        import_text(text, flash, table)


def test_binary_entry_of_size_zero():
    with pytest.raises(ImportProblem, match="has size 0") as caught:
        import_table(table_bytes(("z", 1, 2, 0x9000, 0, 0)), "t.bin", settings(FOUR_MB, None))
    assert "give it a size" in caught.value.hint


def test_negative_offset_in_a_message():
    with pytest.raises(ImportProblem, match="starts at -0x1000"):
        read_csv("a, data, nvs, -0x1000, 4K\n", 0x8000)


def test_recovery_bootloader_slot():
    text = "nvs, data, nvs, , 24K\nrecovery, bootloader, recovery, , \n"
    partitions = read_csv(text, 0x8000, bootloader=0x1000, recovery=0x3F0000)
    assert (partitions[1].offset, partitions[1].size) == (0x3F0000, 0x7000)


def test_bootloader_option_wins_over_the_sdkconfig():
    imported = import_text("nvs, data, nvs, , 24K\n", sdkconfig='CONFIG_IDF_TARGET="esp32"\n')
    assert "Bootloader at 0x1000" in imported.text
    config = settings(FOUR_MB, None, read_sdkconfig('CONFIG_IDF_TARGET="esp32"\n'), bootloader_offset=0x2000)
    imported = import_table(b"nvs, data, nvs, , 24K\n", "t.csv", config)
    assert "Bootloader at 0x2000, from --bootloader-offset." in imported.text


def test_sdkconfig_with_windows_line_ends():
    config = read_sdkconfig('CONFIG_ESPTOOLPY_FLASHSIZE="8MB"\r\nCONFIG_PARTITION_TABLE_OFFSET=0x9000\r\n')
    assert (config.flash_size, config.table_offset) == (8 * 1024 * 1024, 0x9000)


@pytest.mark.parametrize("size", ["4M", "4MB", "4m", "0x400000", "4194304"])
def test_command_flash_size_spellings(tmp_path, size):
    table = write_project(tmp_path, sdkconfig=None)
    result = invoke("from-partitions", table, "--flash-size", size, "--force")
    assert result.exit_code == 0, result.output
    assert "Flash size 4 MB, from --flash-size." in (tmp_path / "partitions.yaml").read_text()


def test_command_with_a_bootloader_entry_and_the_option(tmp_path):
    table = tmp_path / "partitions.csv"
    table.write_text("boot, bootloader, primary, , \nnvs, data, nvs, , 24K\n")
    result = invoke("from-partitions", table, "--flash-size", "4M")
    assert result.exit_code == 2 and "--bootloader-offset" in result.stderr
    result = invoke("from-partitions", table, "--flash-size", "4M", "--bootloader-offset", "0x1000")
    assert result.exit_code == 0, result.output
    text = (tmp_path / "partitions.yaml").read_text()
    assert '"boot", start: 0x00001000, size: 28K' in text


@pytest.mark.parametrize("path", [".", "folder"])
def test_command_on_a_path_that_is_not_a_file(tmp_path, monkeypatch, path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "folder").mkdir()
    result = invoke("from-partitions", path, "--flash-size", "4M", "--json")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "E090"


def test_command_reports_a_crash_as_exit_3(tmp_path, monkeypatch):
    def crash(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("memmap.cli.import_table", crash)
    table = write_project(tmp_path, sdkconfig=None)
    result = invoke("from-partitions", table, "--flash-size", "4M")
    assert result.exit_code == 3 and "internal error: boom" in result.stderr
