"""`memmap from-partitions` on Zephyr devicetrees and nRF Connect SDK Partition Manager files.

The real builds are in tests/fixtures/zephyr_partitions/, see its refresh.py. For each one, the
header the build generated (devicetree_generated.h or pm_config.h) is the ground truth for the
addresses memmap reads. Configs and two drawings are compared with tests/golden/partitions/.
Regenerate after an intended change with:

    MEMMAP_UPDATE_GOLDEN=1 uv run pytest tests/test_from_zephyr.py
"""

import json
import os
import re

import pytest
from click.testing import CliRunner
from conftest import ROOT, codes, drawn

from memmap.cli import app
from memmap.from_elf import ImportProblem
from memmap.from_partitions import (
    dts,
    import_table,
    partition_manager,
    settings,
    zephyr,
)
from memmap.from_partitions.areas import Area, Device, lay_out
from memmap.from_partitions.dts import Reference
from memmap.from_partitions.partition_manager import (
    is_partition_manager,
    read_partition_manager,
)
from memmap.from_partitions.writer import write_regions
from memmap.pipeline import run
from memmap.render.svg import render_svg

FIXTURES = ROOT / "tests" / "fixtures" / "zephyr_partitions"
GOLDEN = ROOT / "tests" / "golden" / "partitions"
UPDATE = os.environ.get("MEMMAP_UPDATE_GOLDEN") == "1"
INDEX = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
BUILDS = {build["name"]: build for build in INDEX["builds"]}
TABLES = [table["name"] for table in INDEX["tables"]]
# Builds whose drawing is a golden too: one devicetree and one Partition Manager file. The
# drawing code is the same for every file, so more would only repeat them.
DRAWN = {"mcxn947_flexspi", "nrf5340_tfm_pm"}
# What each fixture's import reports. Everything else imports cleanly.
EXPECTED_CODES = {
    "pm_static_stale_app.yml": ["W114"],
}
runner = CliRunner()


def compare(golden, text):
    """Check `text` against a golden file, or rewrite it with MEMMAP_UPDATE_GOLDEN=1."""
    if UPDATE:
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(text, encoding="utf-8", newline="\n")
    assert text == golden.read_text(encoding="utf-8")


def invoke(*args):
    return runner.invoke(app, [str(arg) for arg in args])


def source_of(name):
    """The file a build fixture imports: its zephyr.dts or partitions.yml."""
    folder = FIXTURES / "builds" / name
    return next(folder / f for f in ("zephyr.dts", "partitions.yml") if (folder / f).exists())


def import_file(path, tmp_path):
    """The import of a file, the check of its config, and the codes of both."""
    imported = import_table(path.read_bytes(), path.name, settings(None, None))
    file = tmp_path / "layout.yaml"
    file.write_text(imported.text, encoding="utf-8")
    result = run(file)
    return imported, result, [d.code for d in imported.diagnostics] + codes(result)


def header_macros(path, pattern):
    """name: {ADDRESS or SIZE: value} from the #define lines of a generated header."""
    found = {}
    for name, field, value in re.findall(pattern, path.read_text(encoding="utf-8"), re.MULTILINE):
        found.setdefault(name, {})[field] = int(value, 0)
    return found


def macro_name(path):
    """The DT_N_... prefix Zephyr gives the node at `path`."""
    parts = path.strip("/").split("/")
    return "DT_N" + "".join("_S_" + re.sub(r"[^a-z0-9]", "_", part.lower()) for part in parts)


# The fixtures


def test_fixtures_are_listed():
    assert len(BUILDS) >= 4 and len(TABLES) >= 1
    for name, build in BUILDS.items():
        for file in build["files"]:
            assert (FIXTURES / "builds" / name / file).is_file()
    for name in TABLES:
        assert (FIXTURES / "tables" / name).is_file()


DEVICETREE_BUILDS = [n for n in BUILDS if "devicetree_generated.h" in BUILDS[n]["files"]]


@pytest.mark.parametrize("name", DEVICETREE_BUILDS)
def test_devicetree_reads_like_the_build(name):
    """Each partition and flash reg, mapped through ranges, is what the build generated."""
    folder = FIXTURES / "builds" / name
    pattern = r"^#define (DT_N\w*?)_REG_IDX_0_VAL_(ADDRESS|SIZE) (\d+)"
    macros = header_macros(folder / "devicetree_generated.h", pattern)
    root = dts.parse((folder / "zephyr.dts").read_text(encoding="utf-8"))
    checked = 0
    for node in root.walk():
        compatible = node.compatible()
        if zephyr.FIXED not in compatible and zephyr.SUBPARTITIONS not in compatible:
            continue
        cells = (node.cell_count("#address-cells", 1), node.cell_count("#size-cells", 1))
        nodes = [(child, cells) for child in node.children]
        flash, parent = node.parent, node.parent.parent
        if zephyr.FIXED in compatible:
            parent_cells = (parent.cell_count("#address-cells", 2), parent.cell_count("#size-cells", 1))
            nodes.append((flash, parent_cells))
        for child, (address_cells, size_cells) in nodes:
            reg = zephyr._reg(child, address_cells, size_cells)
            if reg is None:  # a flash on a bus: its reg is a chip select
                continue
            theirs = macros[macro_name(child.path)]
            assert (zephyr.translate(reg[0], child), reg[1]) == (theirs["ADDRESS"], theirs["SIZE"])
            checked += 1
    assert checked >= 4


def test_partition_manager_reads_like_the_build():
    folder = FIXTURES / "builds" / "nrf5340_tfm_pm"
    pattern = r"^#define PM_(\w+?)_(ADDRESS|SIZE) (0x[0-9a-f]+|\d+)$"
    macros = header_macros(folder / "pm_config.h", pattern)
    entries = partition_manager._load((folder / "partitions.yml").read_text(encoding="utf-8"))
    ranges = partition_manager._ranges(entries, [])
    assert len(ranges) == len(entries) == 23
    for name, (start, size) in ranges.items():
        theirs = macros[name.upper()]
        assert (start, size) == (theirs["ADDRESS"], theirs["SIZE"]), name


@pytest.mark.parametrize("name", sorted(BUILDS))
def test_build_imports_to_the_golden_config_and_drawing(name, tmp_path):
    imported, result, found = import_file(source_of(name), tmp_path)
    assert found == EXPECTED_CODES.get(name, [])
    compare(GOLDEN / f"{name}.yaml", imported.text)
    if name in DRAWN:
        compare(GOLDEN / f"{name}.svg", render_svg(drawn(result)))


@pytest.mark.parametrize("name", TABLES)
def test_table_imports_to_the_golden_config(name, tmp_path):
    imported, _, found = import_file(FIXTURES / "tables" / name, tmp_path)
    assert found == EXPECTED_CODES.get(name, [])
    compare(GOLDEN / f"{name.rsplit('.', 1)[0]}.yaml", imported.text)


def test_nrf5340_subpartitions_nest_inside_slot0(tmp_path):
    _, result, _ = import_file(source_of("nrf5340_subpartitions"), tmp_path)
    slot0 = result.tree.index["slot0_partition"]
    assert [(n.id, n.start, n.size) for n in slot0.children] == [
        ("slot0_s_partition", 0x10000, 0x40000),
        ("slot0_ns_partition", 0x50000, 0x30000),
    ]
    assert slot0.note == "MCUboot primary slot"
    ns_note = "MCUboot primary slot, non-secure image, code partition"  # an _ns build
    assert result.tree.index["slot0_ns_partition"].note == ns_note


def test_partition_manager_containers_nest(tmp_path):
    imported, result, _ = import_file(source_of("nrf5340_tfm_pm"), tmp_path)
    index = result.tree.index
    assert [n.id for n in index["mcuboot_primary"].children] == ["mcuboot_pad", "mcuboot_primary_app"]
    assert [n.id for n in index["mcuboot_primary_app"].children] == ["tfm", "app"]
    assert index["app"].note == "application; also tfm_nonsecure"
    spaces = (index["external_flash"].space, index["ram_flash"].space, index["flash_primary"].space)
    assert spaces == ("external_flash", "ram_flash", None)
    assert "tfm_secure (0x10000 - 0x1bfff) partly overlaps mcuboot_primary_app" in imported.text
    assert "ram_flash has size 0, so it is not drawn." in imported.text

# The devicetree parser


def test_parser_reads_what_zephyr_dts_holds():
    text = r"""
/dts-v1/;
/* a comment */ // and another
/ {
	#address-cells = < 0x1 >;
	model = "Board \"x\"\n", "second";
	chosen { zephyr,flash = &flash0; zephyr,code = &{/soc/flash@0}; };
	soc {
		empty;
		bytes = [ 01 2a ff ];
		wide = /bits/ 16 < 0x1 0x2 >;
		chars = < 'a' '\n' >;
		mixed = < &flash0 0x10 >, "s";
		big = < 0xffffffff 017 10UL >;
		a: b: flash0: flash@0 { reg = < 0x0 0x1000 >; };
	};
};
"""
    root = dts.parse(text)
    assert root.path == "/"
    assert root.properties["model"].strings() == ['Board "x"\n', "second"]
    chosen = root.child("chosen")
    assert chosen.properties["zephyr,flash"].values == [Reference("flash0")]
    assert chosen.properties["zephyr,code"].values == [Reference("{/soc/flash@0}")]
    soc = root.child("soc")
    assert soc.properties["empty"].values == []
    assert soc.properties["bytes"].values == [b"\x01\x2a\xff"]
    assert soc.properties["wide"].cells() == [1, 2]
    assert soc.properties["chars"].cells() == [97, 10]
    assert soc.properties["big"].cells() == [0xFFFFFFFF, 0o17, 10]
    assert soc.properties["mixed"].values[0] == [Reference("flash0"), 0x10]
    with pytest.raises(ValueError):
        soc.properties["mixed"].cells()
    with pytest.raises(ValueError):
        soc.properties["bytes"].strings()
    flash = soc.child("flash@0")
    assert flash.labels == ["a", "b", "flash0"] and flash.unit_address == "0"
    assert flash.path == "/soc/flash@0" and flash.line == 15
    assert root.cell_count("#address-cells", 2) == 1 and soc.cell_count("#size-cells", 1) == 1


def test_parser_merges_nodes_and_applies_deletes():
    text = """/dts-v1/;
/ { n: node { a = <1>; b = <2>; gone {}; }; other {}; };
&n { a = <3>; /delete-property/ b; /delete-node/ gone; extra {}; };
&{/other} { c; };
/delete-node/ &n;
/ { node2 { }; };
"""
    root = dts.parse(text)
    assert [child.name for child in root.children] == ["other", "node2"]
    assert "c" in root.child("other").properties
    text = text.replace("/delete-node/ &n;\n", "")
    node = dts.parse(text).child("node")
    assert node.properties["a"].cells() == [3] and "b" not in node.properties
    assert [child.name for child in node.children] == ["extra"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("/dts-v1/;\n#include <nordic.dtsi>\n/ { };", "line 2: #include, a preprocessor directive"),
        ('/dts-v1/;\n# 1 "board.dts"\n/ { };', "line 2: a line marker of the C preprocessor"),
        ("/dts-v1/;\n/ { a = < (1 + 2) >; };", "an expression in a cell list"),
        ("/dts-v1/;\n/ { a = < DT_SIZE_K >; };", "'DT_SIZE_K' is not a cell"),
        ("/dts-v1/;\n/ { a = <1> };", "expected ',' or ';'"),
        ("/dts-v1/;\n/ { a b; };", "expected '=' or ';' after a"),
        ("/dts-v1/;\n/ {\n a = <1>;\n", "line 3: the file ends inside a node"),
        ("/dts-v1/;\n&nowhere { };", "&nowhere names no node"),
        ("/dts-v1/;\n/ { a = [ 1 ]; };", "bad byte string"),
        ("/dts-v1/;\n/ { a = /bits/ 12 <1>; };", "/bits/ 12 is not 8, 16, 32 or 64"),
        ("/dts-v1/;\n/ { a = \x01; };", "unexpected '\\x01'"),
        ("/dts-v1/;\n/foo/;", "unexpected /foo/"),
        ("", "the file is empty"),
    ],
)
def test_parser_errors(text, message):
    with pytest.raises(ImportProblem) as caught:
        dts.parse(text)
    assert message in str(caught.value)


def test_preprocessor_errors_point_at_the_merged_file():
    with pytest.raises(ImportProblem) as caught:
        dts.parse("/dts-v1/;\n#include <x.dtsi>\n")
    assert caught.value.hint == dts.MERGED_HINT


def test_telling_devicetree_source_apart():
    assert dts.is_dts("/* header */\n// note\n/dts-v1/;\n/ { };")
    assert not dts.is_dts("&flash0 { };")
    assert dts.looks_like_source("&flash0 {\n};")
    assert dts.looks_like_source('#include "x.dtsi"\n')
    assert dts.looks_like_source("/ {\n};")
    assert not dts.looks_like_source("nvs, data, nvs, 0x9000, 0x6000,\n")


# Reading partitions from a devicetree

BOARD = """/dts-v1/;
/ {
	#address-cells = < 0x1 >;
	#size-cells = < 0x1 >;
	chosen { zephyr,code-partition = &slot0_partition; };
	soc {
		#address-cells = < 0x1 >;
		#size-cells = < 0x1 >;
		ranges;
		bus@50000000 {
			#address-cells = < 0x1 >;
			#size-cells = < 0x1 >;
			ranges = < 0x0 0x50000000 0x20000000 >;
			flash-controller@39000 {
				#address-cells = < 0x1 >;
				#size-cells = < 0x1 >;
				ranges = < 0x0 0x10000000 0x100000 >;
				flash0: flash@0 {
					compatible = "soc-nv-flash";
					reg = < 0x0 0x100000 >;
					erase-block-size = < 0x1000 >;
					partitions {
						compatible = "fixed-partitions";
						#address-cells = < 0x1 >;
						#size-cells = < 0x1 >;
						boot_partition: partition@0 { label = "mcuboot"; reg = < 0x0 0x10000 >; read-only; };
						slot0_partition: partition@10000 {
							compatible = "fixed-subpartitions";
							label = "image-0";
							reg = < 0x10000 0x70000 >;
							ranges = < 0x0 0x10000 0x70000 >;
							#address-cells = < 0x1 >;
							#size-cells = < 0x1 >;
							slot0_s_partition: partition@0 { label = "image-0-secure"; reg = < 0x0 0x40000 >; };
							slot0_ns_partition: partition@40000 { label = "image-0-nonsecure"; reg = < 0x40000 0x30000 >; };
						};
						slot1_partition: partition@80000 { label = "image-1"; reg = < 0x80000 0x70000 >; };
						scratch_partition: partition@f0000 { reg = < 0xf0000 0xa000 >; };
						slot2_partition: partition@fa000 { reg = < 0xfa000 0x2000 >; };
						no_reg: partition@fc000 { label = "nothing"; };
					};
				};
			};
		};
		spi@1000 {
			#address-cells = < 0x1 >;
			#size-cells = < 0x0 >;
			status = "disabled";
			mx25: nor@0 {
				compatible = "jedec,spi-nor";
				reg = < 0x0 >;
				size = < 0x4000000 >;
				partitions {
					compatible = "fixed-partitions";
					#address-cells = < 0x1 >;
					#size-cells = < 0x1 >;
					partition@0 { reg = < 0x0 0x100000 >; };
				};
			};
		};
	};
};
"""


def test_devicetree_flash_devices_and_partitions():
    devices, comments = zephyr.read_devicetree(BOARD)
    assert [(d.key, d.start, d.size, d.sector) for d in devices] == [
        ("flash0", 0x60000000, 0x100000, 0x1000),
        ("mx25", 0, 0x800000, None),
    ]
    areas = [(a.key, a.name, a.start, a.size, a.note, a.container) for a in devices[0].areas]
    assert areas == [
        ("boot_partition", "mcuboot", 0x60000000, 0x10000, "MCUboot, read-only", False),
        ("slot0_partition", "image-0", 0x60010000, 0x70000, "MCUboot primary slot, code partition", True),
        ("slot0_s_partition", "image-0-secure", 0x60010000, 0x40000, None, False),
        ("slot0_ns_partition", "image-0-nonsecure", 0x60050000, 0x30000, "MCUboot primary slot, non-secure image", False),
        ("slot1_partition", "image-1", 0x60080000, 0x70000, "MCUboot secondary slot", False),
        ("scratch_partition", "scratch_partition", 0x600F0000, 0xA000, "MCUboot scratch", False),
        ("slot2_partition", "slot2_partition", 0x600FA000, 0x2000, "MCUboot primary slot, image 1", False),
    ]
    assert devices[1].areas[0].key == "partition" and devices[1].areas[0].name == "partition"
    assert "from its size property in bits, drawn from 0" in devices[1].about
    assert any("no_reg (devicetree line" in comment and "not drawn" in comment for comment in comments)
    assert any(comment.startswith("mx25 has status disabled") for comment in comments)


def test_devicetree_sizes_from_partitions_and_errors():
    no_size = BOARD.replace("size = < 0x4000000 >;", "").replace("jedec,spi-nor", "vendor,flash")
    devices, _ = zephyr.read_devicetree(no_size)
    assert devices[1].size == 0x100000 and "its last partition" in devices[1].about
    custom = BOARD.replace("jedec,spi-nor", "vendor,flash")  # its size unit is not known
    assert zephyr.read_devicetree(custom)[0][1].size == 0x100000
    with pytest.raises(ImportProblem, match="no fixed-partitions node"):
        zephyr.read_devicetree("/dts-v1/;\n/ { soc { }; };")
    empty = "/dts-v1/;\n/ { f@0 { reg = <0 1>; p { compatible = \"fixed-partitions\"; }; }; };"
    with pytest.raises(ImportProblem, match="lists a partition"):
        zephyr.read_devicetree(empty)


def test_devicetree_two_cell_addresses():
    text = """/dts-v1/;
/ {
	#address-cells = < 0x2 >;
	#size-cells = < 0x2 >;
	flash@100000000 {
		reg = < 0x1 0x0 0x0 0x200000 >;
		partitions {
			compatible = "fixed-partitions";
			#address-cells = < 0x2 >;
			#size-cells = < 0x1 >;
			p: partition@0 { reg = < 0x0 0x100000 0x1000 >; };
		};
	};
};
"""
    devices, _ = zephyr.read_devicetree(text)
    assert (devices[0].start, devices[0].size) == (0x100000000, 0x200000)
    assert (devices[0].areas[0].start, devices[0].areas[0].size) == (0x100100000, 0x1000)


@pytest.mark.parametrize(
    ("labels", "role"),
    [
        (["boot_partition"], "MCUboot"),
        (["cpuapp_boot_partition"], "MCUboot"),
        (["slot0_partition"], "MCUboot primary slot"),
        (["slot1_partition"], "MCUboot secondary slot"),
        (["slot3_partition"], "MCUboot secondary slot, image 1"),
        (["slot1_ns_partition"], "MCUboot secondary slot, non-secure image"),
        (["cpurad_slot0_partition"], "MCUboot primary slot"),
        (["scratch_partition"], "MCUboot scratch"),
        (["storage_partition"], None),
        (["reboot_partition"], None),
    ],
)
def test_mcuboot_roles(labels, role):
    assert zephyr._role(labels) == role


# Reading a Partition Manager file

PM = """app:
  address: 0xc200
  end_address: 0xf0000
  region: flash_primary
  size: 0xe3e00
mcuboot:
  address: 0x0
  size: 0xc000
mcuboot_pad:
  address: 0xc000
  end_address: 0xc200
  size: 0x200
mcuboot_primary:
  orig_span: &id001
  - mcuboot_pad
  - app
  span: *id001
mcuboot_secondary:
  address: 0x0
  size: 0xe4000
  device: MX25R64
  region: external_flash
empty:
  address: 0xf0000
  size: 0x0
tfm_nonsecure:
  address: 0xc200
  size: 0xe3e00
  span: app
sram_primary:
  address: 0x20000000
  end_address: 0x20040000
  region: sram_primary
"""


def test_partition_manager_regions_and_containers():
    assert is_partition_manager(PM)
    devices, comments = read_partition_manager(PM)
    assert [(d.key, d.name, d.start, d.size) for d in devices] == [
        ("flash_primary", "Flash", 0, 0xF0000),
        ("external_flash", "External flash", 0, 0xE4000),
        ("sram_primary", "SRAM", 0x20000000, 0x40000),
    ]
    assert devices[1].about == "external_flash: 912 KB from 0x0, on MX25R64."
    flash = {area.key: area for area in devices[0].areas}
    assert (flash["mcuboot_primary"].start, flash["mcuboot_primary"].size) == (0xC000, 0xE4000)
    assert flash["mcuboot_primary"].container and flash["tfm_nonsecure"].container
    assert flash["app"].weak and flash["mcuboot_pad"].note == "MCUboot image header"
    assert comments == []


def test_partition_manager_problems_and_comments():
    text = "a:\n  address: 0x10\n  size: 0x10\n  end_address: 0x8\nb:\n  span: [c]\n"
    _, comments = read_partition_manager(text)
    assert comments == [
        "a: end_address 0x8 is not address + size, 0x20; drawn by its size.",
        "b gives no address and size, so it is not drawn.",
    ]
    with pytest.raises(ImportProblem, match="not a number"):
        read_partition_manager("a:\n  address: zero\n  size: 0x10\n")
    with pytest.raises(ImportProblem, match="not valid YAML"):
        read_partition_manager("a:\n  address: [\n")
    with pytest.raises(ImportProblem, match="not a Partition Manager file"):
        read_partition_manager("a:\n  name: x\n")
    with pytest.raises(ImportProblem, match="no partition with a size"):
        read_partition_manager("a:\n  address: 0x0\n  size: 0x0\n")
    assert not is_partition_manager("nvs, data, nvs, 0x9000, 0x6000,\n")


# Laying out


def area(key, start, size, **kwargs):
    return Area(key, key, start, size, key, **kwargs)


def test_nesting_merging_and_partial_overlaps():
    device = Device(
        "flash",
        "Flash",
        0,
        0x10000,
        [
            area("a", 0x0, 0x8000),
            area("b", 0x0, 0x4000),  # inside a
            area("c", 0x0, 0x4000, note="role"),  # the same range as b
            area("d", 0x6000, 0x4000),  # partly over a: left out
            area("box", 0x8000, 0x8000, container=True),
            area("e", 0xC000, 0x2000),
            area("view", 0x7000, 0x2000, container=True),  # partly over a: a comment
            area("zero", 0xF000, 0),
        ],
    )
    layout = lay_out([device])
    items = layout.regions[0].bucket.items
    assert [(i.id, i.start, i.size) for i in items] == [("a", 0, 0x8000), ("box", 0x8000, 0x8000)]
    assert [(i.id, i.note) for i in items[0].children] == [("b", "also c"), ("free", None)]
    assert [i.id for i in items[1].children] == ["free_2", "e", "free_3"]
    assert items[1].expanded and not items[0].children[0].expanded
    assert [d.message for d in layout.diagnostics] == [
        "d (0x6000 - 0x9fff) partly overlaps a (0x0 - 0x7fff), left out"
    ]
    assert layout.comments == [
        "zero has size 0, so it is not drawn.",
        "view (0x7000 - 0x8fff) partly overlaps a (0x0 - 0x7fff), so it is not drawn.",
    ]


def test_weak_partition_gives_way():
    device = Device("flash", "Flash", 0, 0x10000, [area("app", 0x0, 0xC000, weak="stale"), area("boot", 0x0, 0x2000), area("x", 0xA000, 0x4000)])
    layout = lay_out([device])
    assert [i.id for i in layout.regions[0].bucket.items] == ["boot", "free", "x", "free_2"]
    assert layout.diagnostics[0].hint == "stale" and "app (0x0 - 0xbfff)" in layout.diagnostics[0].message


def test_devices_get_address_spaces_and_unique_ids():
    devices = [
        Device("flash", "Flash", 0, 0x1000, [area("flash", 0, 0x1000)]),
        Device("ext", "Ext", 0, 0x2000, [area("p", 0, 0x1000)], own_space=True),
        Device("sim", "Sim", 0x800, 0x1000, [area("q", 0x800, 0x1000)]),
        Device("ram", "RAM", 0x20000000, 0x1000, [area("r", 0x20000000, 0x1000)]),
    ]
    layout = lay_out(devices)
    assert [(r.bucket.memory.id, r.space) for r in layout.regions] == [
        ("flash", None), ("ext", "ext"), ("sim", "sim"), ("ram", None)
    ]
    assert layout.regions[0].bucket.items[0].id == "flash_2"
    assert layout.comments == ["Sim overlaps another region, so it gets an address space of its own."]


def test_sectors_only_when_they_divide_the_device():
    for size, sector, kept in ((0x10000, 0x1000, 0x1000), (0x10800, 0x1000, None), (0x2000000, 0x100, None)):
        device = Device("f", "F", 0, size, [area("a", 0, 0x100)], sector=sector)
        assert lay_out([device]).regions[0].sector == kept


# The command


def test_command_on_a_devicetree(tmp_path):
    source = tmp_path / "zephyr.dts"
    source.write_bytes((FIXTURES / "builds" / "stm32_mcuboot_scratch" / "zephyr.dts").read_bytes())
    result = invoke("from-partitions", source, "--json", "--strict")
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["ok"] and data["output"] == str(tmp_path / "zephyr.yaml")
    assert invoke("render", tmp_path / "zephyr.yaml", "--strict").exit_code == 0


def test_command_warns_and_strict_fails(tmp_path):
    source = tmp_path / "pm_static.yml"
    source.write_bytes((FIXTURES / "tables" / "pm_static_stale_app.yml").read_bytes())
    assert invoke("from-partitions", source).exit_code == 0
    result = invoke("from-partitions", source, "--force", "--json", "--strict")
    assert result.exit_code == 1
    assert [d["code"] for d in json.loads(result.stdout)["diagnostics"]] == ["W114"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--flash-size", "4M"], "--flash-size is for ESP-IDF partition tables, but zephyr.dts is a Zephyr devicetree"),
        (["--table-offset", "0x9000", "--bootloader-offset", "0x1000"], "--table-offset and --bootloader-offset are for"),
    ],
)
def test_command_refuses_esp_options(tmp_path, args, message):
    source = tmp_path / "zephyr.dts"
    source.write_bytes((FIXTURES / "builds" / "stm32_mcuboot_scratch" / "zephyr.dts").read_bytes())
    result = invoke("from-partitions", source, "--json", *args)
    assert result.exit_code == 2
    diagnostic = json.loads(result.stdout)["diagnostics"][0]
    assert diagnostic["code"] == "E090" and message in diagnostic["message"]
    assert diagnostic["hint"].startswith("leave out")


def test_command_refuses_sdkconfig_for_partition_manager(tmp_path):
    source = tmp_path / "partitions.yml"
    source.write_text("app:\n  address: 0x0\n  size: 0x1000\n")
    (tmp_path / "sdkconfig").write_text("CONFIG_IDF_TARGET=\"esp32\"\n")
    result = invoke("from-partitions", source, "--sdkconfig", tmp_path / "sdkconfig")
    assert result.exit_code == 2
    assert "--sdkconfig is for ESP-IDF partition tables" in result.stderr


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("&flash0 {\n\tpartitions { };\n};\n", "the C preprocessor has not run on"),
        ("/dts-v1/;\n#include <st/f4/stm32f401.dtsi>\n/ { };\n", "a preprocessor directive"),
        ("/dts-v1/;\n/ { soc { }; };\n", "no fixed-partitions node"),
    ],
)
def test_command_on_devicetree_it_cannot_read(tmp_path, text, message):
    source = tmp_path / "board.overlay"
    source.write_text(text)
    result = invoke("from-partitions", source)
    assert result.exit_code == 2
    assert message in result.stderr
    assert "zephyr.dts" in result.stderr or "partitions.yml" in result.stderr


def test_partition_outside_its_flash_and_aliases_of_a_dropped_partition():
    device = Device(
        "flash",
        "Flash",
        0x8000000,
        0x20000,
        [
            area("boot", 0x8000000, 0x10000),
            area("slot0", 0x8020000, 0x20000),  # past the end of a 128 KB flash
            area("a", 0x800C000, 0x8000),  # partly over boot
            area("b", 0x800C000, 0x8000),  # the same range as a
        ],
    )
    layout = lay_out([device])
    messages = [d.message for d in layout.diagnostics]
    assert messages == [
        "slot0 (0x8020000 - 0x803ffff) lies outside Flash (0x8000000 - 0x801ffff), left out",
        ("a (and b, with the same range) (0x800c000 - 0x8013fff) partly overlaps boot "
        "(0x8000000 - 0x800ffff), left out"),
    ]
    assert {d.subject for d in layout.diagnostics} == {"flash"}


def test_stale_app_gives_way_to_a_container():
    text = """mcuboot:
  address: 0x0
  size: 0x8000
mcuboot_pad:
  address: 0x10000
  size: 0x200
mcuboot_primary:
  address: 0x10000
  size: 0x40000
  span: [mcuboot_pad, app]
app:
  address: 0x8000
  size: 0x20000
"""
    layout = lay_out(read_partition_manager(text)[0])
    items = layout.regions[0].bucket.items
    assert [i.id for i in items] == ["mcuboot", "free", "mcuboot_primary"]
    assert [i.id for i in items[2].children] == ["mcuboot_pad", "free_2"]
    assert len(layout.diagnostics) == 1 and layout.diagnostics[0].message.startswith("app (0x8000")
    assert layout.diagnostics[0].hint == partition_manager.APP_HINT


def test_containers_without_addresses_in_any_order():
    text = """outer:
  span: [inner, tail]
inner:
  span: [head, mid]
head: {address: 0x0, size: 0x1000}
mid: {address: 0x1000, size: 0x1000}
tail: {address: 0x2000, size: 0x1000}
loop_a:
  span: [loop_b]
loop_b:
  span: [loop_a]
"""
    comments: list[str] = []
    ranges = partition_manager._ranges(partition_manager._load(text), comments)
    assert ranges["outer"] == (0, 0x3000) and ranges["inner"] == (0, 0x2000)
    assert comments == [
        "loop_a gives no address and size, so it is not drawn.",
        "loop_b gives no address and size, so it is not drawn.",
    ]


def test_flow_style_partition_manager_file():
    text = "app: {address: 0x18200, size: 0x5ae00}\nmcuboot: {address: 0x0, size: 0x18200}\n"
    assert is_partition_manager(text)
    imported = import_table(text.encode(), "pm_static.yml", settings(None, None))
    assert "Partition Manager" in imported.text


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("a:\n  address: -0x1000\n  size: 0x10\n", "a: address -4096 is negative"),
        ("a:\n  address: 0x2000\n  end_address: 0x1000\n", "end_address 0x1000 is below its address 0x2000"),
        ("a:\n  address: 0x0\n  size: 0x10\n  region: [x, y]\n", "region ['x', 'y'] is not a name"),
    ],
)
def test_partition_manager_values_it_refuses(text, message):
    with pytest.raises(ImportProblem) as caught:
        read_partition_manager(text)
    assert message in str(caught.value)


def test_devicetree_odd_shapes():
    under_root = '/dts-v1/;\n/ { partitions { compatible = "fixed-partitions"; p@0 { reg = <0 0x1000>; }; }; };'
    with pytest.raises(ImportProblem, match="lists a partition"):
        zephyr.read_devicetree(under_root)
    zero = (
        '/dts-v1/;\n/ { #address-cells = <1>; #size-cells = <1>; f: flash@0 { reg = <0 0>;\n'
        'partitions { compatible = "fixed-partitions"; #address-cells = <1>; #size-cells = <1>;\n'
        "p: partition@0 { reg = <0 0x1000>; }; }; }; };"
    )
    devices, _ = zephyr.read_devicetree(zero)
    assert devices[0].size == 0x1000 and "its last partition" in devices[0].about
    data = zero.replace("/dts-v1/;", "/dts-v1/;\n/* \xe9 */").encode("latin-1")  # not UTF-8
    assert "flash layout" in import_table(data, "zephyr.dts", settings(None, None)).text


def test_space_comment_says_why():
    devices = [
        Device("flash", "Flash", 0, 0x1000, [area("a", 0, 0x1000)]),
        Device("ext", "Ext", 0, 0x1000, [area("b", 0, 0x1000)], own_space=True),
        Device("sim", "Sim", 0, 0x1000, [area("c", 0, 0x1000)]),
    ]
    text = import_layout_text(devices)
    assert 'space: "ext"  # offsets in the chip, not CPU addresses' in text
    assert 'space: "sim"  # overlaps another region' in text


def import_layout_text(devices):
    """The config text of devices laid out."""
    return write_regions(["header"], "title", lay_out(devices).regions)


def test_erase_blocks_that_give_no_sectors_are_explained():
    device = Device("f", "F", 0, 0x10800, [area("a", 0, 0x100)], sector=0x1000)
    assert lay_out([device]).comments == [
        ("F has no sectors: its 4 KB erase blocks do not divide it or are more than 4096, so "
        "partition edges are not checked against them.")
    ]
