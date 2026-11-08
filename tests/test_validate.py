import pytest
from conftest import ROOT, codes, drawn, regions

from memmap.resolve import MAX_SECTORS

BASE =regions("    - { id: m, size: 4K }")


def only(result, code):
    found = [d for d in result.diagnostics if d.code == code]
    assert found, f"{code} not in {codes(result)}"
    return found[0]


def test_e000_broken_yaml(load):
    diagnostic = only(load("version: 1\nmemory: [\n"), "E000")
    assert diagnostic.line is not None


def test_e000_duplicate_yaml_key(load):
    assert codes(load("version: 1\nversion: 1\n")) == ["E000"]


def test_e001_unknown_key_with_suggestion(load):
    result = load(regions("    - { id: m, size: 4K, nam: x }"))
    diagnostic = only(result, "E001")
    assert diagnostic.hint == "did you mean 'name'?"
    assert (diagnostic.line, diagnostic.column) == (4, 26)


def test_e001_at_every_level(load):
    assert "E001" in codes(load(BASE + "styel: {}\n"))
    assert "E001" in codes(load(BASE + "view:\n  defaults:\n    labels:\n      adress: both\n"))
    text = BASE + "view:\n  columns:\n    - { id: c, root: m, widht: 10 }\n"
    assert only(load(text), "E001").hint == "did you mean 'width'?"
    text = BASE + "view:\n  expand:\n    - { region: m, form: x }\n"
    assert only(load(text), "E001").hint == "did you mean 'from'?"


@pytest.mark.parametrize(
    "key",
    [
        "view.defaults.connector={width: 2}",
        "view.expand=[{region: m, connector: {width: 2}}]",
        "view.defaults.labels.format={group: 0}",
        "view.defaults.labels.side=right",
        "view.defaults.labels.edges=regions",
        "view.defaults.scale.k=3",
        "view.defaults.scale.total_height=480",
        "view.overrides=[{region: m, labels: {address: both}}]",
        "view.overrides=[{region: m, scale: {min_height: 40}}]",
        "view.overrides=[{region: m, style: {frame: false}}]",
        "view.columns=[{id: c, root: m, style: {frame: false}}]",
        "style.font={size: 12}",
        "style.address_font={size: 10}",
        "style.block={fill: '#ffffff'}",
        "style.background='#ffffff'",
        "style.column_gap=100",
    ],
)
def test_e001_options_that_are_not_configurable(load, key):
    assert codes(load(BASE, [key])) == ["E001"]


@pytest.mark.parametrize(
    "assignment",
    [
        "view.defaults.labels.address=smart",
        "view.defaults.labels.size={show: true}",
        "view.defaults.scale.mode=manual",
        "view.overrides=[{region: m, top_label: both}]",
        "memory.regions[0].kind=alias",
    ],
)
def test_e004_values_that_were_narrowed(load, assignment):
    assert codes(load(BASE, [assignment])) == ["E004"]


@pytest.mark.parametrize(
    "body",
    [
        "    - { id: m, size: 7Q }",
        "    - { id: m, start: banana, size: 4K }",
        "    - { id: m, size: 4K, sectors: { size: nope, count: 2 } }",
        "    - { id: m, size: 0 }",
    ],
)
def test_e002_bad_literals(load, body):
    assert "E002" in codes(load(regions(body)))


def test_e003_duplicate_id(load):
    result = load(regions("    - { id: m, size: 4K }\n    - { id: m, size: 4K }"))
    diagnostic = only(result, "E003")
    assert diagnostic.path == ("memory", "regions", 1)
    assert "memory.regions[0]" in diagnostic.hint


def test_e004_missing_key_and_bad_value(load):
    assert "E004" in codes(load("version: 1\n"))
    assert "E004" in codes(load(regions("    - { id: m }")))
    assert "E004" in codes(load(regions("    - { id: Bad-Id, size: 4K }")))
    assert "E004" in codes(load(BASE + "view:\n  defaults:\n    labels:\n      address: maybe\n"))


def test_e005_unsupported_version(load):
    assert codes(load(BASE.replace("version: 1", "version: 2"))) == ["E005"]


def test_e010_child_outside_parent(load):
    result = load(
        regions(
            """    - id: m
      start: 0x1000
      size: 4K
      children:
        - { id: a, size: 8K }"""
        )
    )
    diagnostic = only(result, "E010")
    assert diagnostic.path == ("memory", "regions", 0, "children", 0)
    assert diagnostic.line == 8
    assert "shrink a by 4096 bytes" in diagnostic.hint


def test_e010_top_level_outside_address_space(load):
    assert "E010" in codes(load(regions("    - { id: m, start: 0xFFFFF000, size: 8K }")))


def test_e011_overlap_matches_proposal_format(load):
    text = regions(
        """    - id: flash
      start: 0x08000000
      size: 64K
      children:
        - { id: pad, size: 2K }
        - { id: boot, start: 0x08000800, size: 14K }
        - { id: app, start: 0x08003800, size: 40K }"""
    )
    result = load(text)
    diagnostic = only(result, "E011")
    assert diagnostic.path == ("memory", "regions", 0, "children", 2)
    assert diagnostic.message == "overlaps memory.regions[0].children[1] (boot)"
    assert diagnostic.hint == "remove 'start' from app to place it right after boot"
    assert diagnostic.line == 10
    assert diagnostic.details == [
        "app: 0x0800_3800 - 0x0800_D7FF",
        "boot: 0x0800_0800 - 0x0800_3FFF",
    ]
    assert diagnostic.to_text("layout.yaml").splitlines()[0] == (
        "layout.yaml:10:11  E011  memory.regions[0].children[2] (app)"
    )


@pytest.mark.parametrize(
    "body",
    [
        "    - { id: m, start: 0x2000, end: 0x1000 }",
        "    - { id: m, start: 0x1000, size: 4K, end: 0x1FFE }",
        """    - id: m
      size: 4K
      children:
        - { id: a, start: 0, size: 1K, align: end }""",
    ],
)
def test_e012_inconsistent_ranges(load, body):
    assert "E012" in codes(load(regions(body)))


def test_e013_two_autos(load):
    result = load(
        regions(
            """    - id: m
      size: 4K
      children:
        - { id: a, size: auto }
        - { id: b, size: 1K }
        - { id: c, size: auto }"""
        )
    )
    assert only(result, "E013").path[-1] == 2


def test_e013_nothing_left_for_auto(load):
    result = load(
        regions(
            """    - id: m
      size: 4K
      children:
        - { id: a, size: 4K }
        - { id: b, size: auto }"""
        )
    )
    assert "E013" in codes(result)


def test_e014_fill_gaps_error(load):
    text = regions(
        """    - id: m
      size: 4K
      children:
        - { id: a, size: 1K }"""
    ).replace("memory:\n", "memory:\n  fill_gaps: error\n")
    assert codes(load(text)) == ["E014"]


def test_fill_gaps_error_ignores_top_level(load):
    assert codes(load(BASE.replace("memory:\n", "memory:\n  fill_gaps: error\n"))) == []


def view(text):
    return BASE + "view:\n" + text


def test_e020_missing_references(load):
    assert "E020" in codes(load(view("  columns:\n    - { id: c, root: nope }\n")))
    assert "E020" in codes(load(view("  expand: [nope]\n")))
    assert "E020" in codes(load(view("  overrides:\n    - { region: nope, top_label: end }\n")))
    text = view("  columns:\n    - { id: c, root: '/' }\n  expand:\n    - { region: m, from: x }\n")
    assert "E020" in codes(load(text))
    text = view("  columns:\n    - { id: c, root: '/' }\n  expand:\n    - { region: m, to: x }\n")
    assert "E020" in codes(load(text))


def test_e021_detail_root_mismatch(load):
    text = regions("    - { id: m, size: 4K }\n    - { id: n, size: 4K }") + (
        "view:\n  columns:\n    - { id: a, root: '/' }\n    - { id: b, root: n }\n"
        "  expand:\n    - { region: m, from: a, to: b }\n"
    )
    assert codes(load(text)) == ["E021"]


def test_e022_expanded_region_not_in_column(load):
    text = regions("    - { id: m, size: 4K }\n    - { id: n, size: 4K }") + (
        "view:\n  columns:\n    - { id: a, root: m }\n    - { id: b, root: n }\n"
        "  expand:\n    - { region: n, from: a, to: b }\n"
    )
    assert codes(load(text)) == ["E022"]


def clipped(expansion):
    """A column that shows only `a`, and an expansion of `b`, which the range leaves out."""
    return regions(
        """    - id: m
      size: 8K
      children:
        - { id: a, size: 4K }
        - { id: b, size: 4K }"""
    ) + (
        "view:\n  columns:\n    - { id: c, root: m, range: [0, 0xFFF] }\n"
        f"  expand:\n    - {expansion}\n"
    )


def test_e022_expanded_region_outside_the_column_range(load):
    assert codes(load(clipped("{ region: b, from: c }"))) == ["E022"]


def test_e020_expanded_region_outside_every_column_range(load):
    assert codes(load(clipped("b"))) == ["E020"]


def test_expansion_finds_the_column_whose_range_shows_the_region(load):
    text = clipped("b").replace("  expand:", "    - { id: d, root: m }\n  expand:")
    result = load(text)
    assert codes(result) == []
    links = [{key: link[key] for key in ("region", "from", "to")} for link in drawn(result).links]
    assert links == [{"region": "b", "from": "d", "to": "b_detail"}]


def test_e023_duplicate_column(load):
    text = view("  columns:\n    - { id: c, root: m }\n    - { id: c, root: m }\n")
    assert codes(load(text)) == ["E023"]


def test_e024_too_many_bands(load):
    text = view("  columns:\n    - { id: c, root: '/', bands: 1K }\n")
    assert codes(load(text)) == ["E024"]


@pytest.mark.parametrize("expansion", ["m", "{ region: m, from: c, to: c }"])
def test_e025_column_cannot_expand_into_itself(load, expansion):
    text = view(f"  columns:\n    - {{ id: c, root: m }}\n  expand:\n    - {expansion}\n")
    assert codes(load(text)) == ["E025"]


def test_e015_too_many_sectors(load):
    text = regions("    - { id: m, size: 4G, sectors: { size: 1, count: 4294967296 } }")
    diagnostic = only(load(text), "E015")
    assert diagnostic.path == ("memory", "regions", 0, "sectors")


def test_e015_counts_every_run(load):
    run = "{ size: 1, count: 600000 }"
    text = regions(f"    - {{ id: m, size: 1200000, sectors: [{run}, {run}] }}")
    assert codes(load(text)) == ["E015"]


def test_sector_count_at_the_limit_is_allowed(load):
    sectors = f"{{ size: 1, count: {MAX_SECTORS} }}"
    text = regions(f"    - {{ id: m, size: {MAX_SECTORS}, sectors: {sectors} }}")
    assert codes(load(text)) == []


@pytest.mark.parametrize(
    "assignment",
    [
        "view.columns=[{id: c, root: m, width: .inf}]",
        "view.columns=[{id: c, root: m, offset: .nan}]",
        "view.defaults.scale.min_height=.inf",
        "view.defaults.scale.max_height=.inf",
        "style.font_size=.inf",
        "view.overrides=[{region: m, height: .inf}]",
        "view.overrides=[{region: m, height: .nan}]",
    ],
)
def test_numbers_must_be_finite(load, assignment):
    result = load(BASE, [assignment])
    assert result.drawing is None
    assert len(result.diagnostics) == 1
    assert codes(result)[0] in ("E002", "E004")


def test_e001_the_python_name_of_from_is_not_a_key(load):
    text = view("  expand:\n    - { region: m, from_: main }\n")
    assert only(load(text), "E001").hint == "did you mean 'from'?"


def test_column_range_outside_root(load):
    text = view("  columns:\n    - { id: c, root: m, range: [0x0, 0xFFFFF] }\n")
    assert codes(load(text)) == ["E010"]


def example_b(size="16K"):
    text = (ROOT / "examples" / "custom_bootloader.yaml").read_text()
    return text.replace("size: 16K, color", f"size: {size}, color")


def test_w100_names_the_bootloader_when_it_misses_a_page_boundary(load):
    result = load(example_b("15K"))
    first = result.diagnostics[0]
    assert first.code == "W100"
    assert first.path == ("memory", "regions", 0, "children", 0)
    assert first.line is not None
    assert "0x0800_3800" in first.hint and "0x0800_4000" in first.hint


def test_w100_is_quiet_on_good_layouts(load):
    assert codes(load(example_b())) == []


def test_w100_skips_regions_past_the_sectors(load):
    text = regions(
        """    - id: m
      size: 4K
      sectors: { size: 2K, count: 2 }
      children:
        - { id: a, size: 8K }"""
    )
    assert codes(load(text)) == ["E010"]


def test_w100_checks_a_start_with_unfilled_space_below_it(load):
    text = """version: 1
memory:
  fill_gaps: none
  regions:
    - id: m
      size: 64K
      sectors: { size: 4K, count: 16 }
      children:
        - { id: a, size: 4K }
        - { id: b, start: 0x8100, size: 0x0F00 }
"""
    diagnostic = only(load(text), "W100")
    assert diagnostic.subject == "b"
    assert "start of b" in diagnostic.message


def test_w101_only_when_verbose(load):
    text = regions(
        """    - id: m
      size: 4K
      children:
        - { id: a, size: 1K }"""
    )
    assert "W101" not in codes(load(text))
    assert "W101" in codes(load(text, verbose=True))


def test_w102_label_forces_block_over_max_height(load):
    text = regions('    - { id: m, name: "a\\nb\\nc\\nd", size: 4K }') + (
        "view:\n  defaults:\n    scale: { mode: equal, max_height: 30 }\n"
    )
    result = load(text)
    assert only(result, "W102").path == ("memory", "regions", 0)


NARROW = "view:\n  columns:\n    - { id: c, root: m, width: %d }\n"


def test_w105_name_split_inside_a_word(load):
    text = regions('    - { id: m, name: "Bootloaderpartitiontable", size: 4K }') + NARROW % 60
    result = load(text)
    assert only(result, "W105").path == ("memory", "regions", 0)
    assert only(result, "W105").subject == "m"


def test_w105_not_for_a_name_that_wraps_between_words(load):
    text = regions('    - { id: m, name: "Boot loader", size: 4K }') + NARROW % 60
    assert "W105" not in codes(load(text))


def test_w105_once_per_region_across_bands(load):
    text = regions('    - { id: m, name: "Bootloaderpartitiontable", size: 4K }') + (
        "view:\n  columns:\n    - { id: c, root: m, width: 60, bands: 1K }\n"
    )
    assert codes(load(text)).count("W105") == 1


def test_w106_size_line_wider_than_its_block(load):
    text = regions("    - { id: m, name: A, size: 4K }") + (
        "view:\n  defaults: { labels: { size: true } }\n"
        "  columns:\n    - { id: c, root: m, width: 20 }\n"
    )
    assert "wider than" in only(load(text), "W106").message


def test_w106_overlapping_columns(load):
    text = regions("    - { id: m, name: Bootloader, size: 4K }") + (
        "view:\n  columns:\n"
        "    - { id: a, root: m, lane: 0, anchor: free }\n"
        "    - { id: b, root: m, lane: 0, anchor: free }\n"
    )
    assert "overlaps" in only(load(text), "W106").message


def test_w106_not_for_ordinary_figures(load):
    assert "W106" not in codes(load(BASE))


def test_w103_is_opt_in(load):
    text = regions("    - { id: m, start: 0x1000, size: 8K }")
    assert codes(load(text)) == []
    opted_in = text.replace("memory:\n", "memory:\n  check_alignment: true\n")
    assert codes(load(opted_in)) == ["W103"]


def test_w104_sectors_do_not_cover_the_region(load):
    text = regions("    - { id: m, size: 4K, sectors: { size: 1K, count: 2 } }")
    assert codes(load(text)) == ["W104"]


def test_sector_list_forms(load):
    text = regions("    - { id: m, size: 6K, sectors: [2K, { size: 1K, count: 4 }] }")
    result = load(text)
    assert codes(result) == []
    assert result.tree.index["m"].sectors == [2048, 1024, 1024, 1024, 1024]
