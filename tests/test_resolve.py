from conftest import ROOT, codes, regions

from memmap.describe import describe_data, describe_text
from memmap.pipeline import run


def spans(result, parent):
    return {
        child.id: (child.start, child.end)
        for child in result.tree.index[parent].children
        if not child.generated
    }


def test_example_b_placement():
    result = run(ROOT / "examples" / "custom_bootloader.yaml")
    assert codes(result) == []
    assert spans(result, "flash") == {
        "boot": (0x08000000, 0x08003FFF),
        "app": (0x08004000, 0x0800DFFF),
        "cfg": (0x0800E000, 0x0800EFFF),
        "log": (0x0800F000, 0x0800FFFF),
    }
    assert spans(result, "sram") == {
        "data": (0x20000000, 0x20000FFF),
        "heap": (0x20001000, 0x200027FF),
        "stack": (0x20002800, 0x20002FFF),
    }


def test_sequential_start_defaults_to_parent_start():
    result = run(ROOT / "examples" / "stm32f303_fig8.yaml")
    assert spans(result, "code")["boot_alias"] == (0, 0xFFFF)


def test_gaps_are_filled_as_reserved(load):
    result = load(
        regions(
            """    - id: m
      start: 0x1000
      size: 16K
      children:
        - { id: a, start: 0x2000, size: 4K }"""
        )
    )
    children = result.tree.index["m"].children
    assert [(c.start, c.end, c.reserved) for c in children] == [
        (0x1000, 0x1FFF, True),
        (0x2000, 0x2FFF, False),
        (0x3000, 0x4FFF, True),
    ]
    assert all(c.generated for c in children if c.reserved)


def test_fill_gaps_none_leaves_gaps(load):
    text = regions(
        """    - id: m
      size: 16K
      children:
        - { id: a, start: 0x1000, size: 4K }"""
    ).replace("memory:\n", "memory:\n  fill_gaps: none\n")
    assert [c.id for c in load(text).tree.index["m"].children] == ["a"]


def test_top_level_gaps_cover_the_address_space(load):
    result = load(regions("    - { id: m, start: 0x1000, size: 4K }"))
    top = result.tree.root.children
    assert (top[0].start, top[-1].end) == (0, 0xFFFFFFFF)


def test_address_bits_64(load):
    text = regions("    - { id: m, start: 0x1_0000_0000, size: 4K }").replace(
        "memory:\n", "memory:\n  address_bits: 64\n"
    )
    result = load(text)
    assert result.ok
    assert result.tree.root.end == 2**64 - 1


def test_auto_stops_before_next_explicit_start(load):
    result = load(
        regions(
            """    - id: m
      size: 16K
      children:
        - { id: a, size: auto }
        - { id: b, start: 0x3000, size: 4K }"""
        )
    )
    assert spans(result, "m") == {"a": (0, 0x2FFF), "b": (0x3000, 0x3FFF)}


def test_auto_leaves_room_for_sequential_siblings(load):
    result = load(
        regions(
            """    - id: m
      size: 16K
      children:
        - { id: a, size: auto }
        - { id: b, size: 4K }"""
        )
    )
    assert spans(result, "m") == {"a": (0, 0x2FFF), "b": (0x3000, 0x3FFF)}


def test_align_end_regions_stack_from_the_top(load):
    result = load(
        regions(
            """    - id: m
      size: 16K
      children:
        - { id: low, size: 1K, align: end }
        - { id: high, size: 2K, align: end }"""
        )
    )
    assert spans(result, "m") == {"low": (0x3400, 0x37FF), "high": (0x3800, 0x3FFF)}


def test_children_may_be_written_out_of_order(load):
    result = load(
        regions(
            """    - id: m
      size: 16K
      children:
        - { id: b, start: 0x2000, size: 4K }
        - { id: a, start: 0, size: 4K }"""
        )
    )
    assert codes(result) == []
    assert [c.id for c in result.tree.index["m"].children if not c.generated] == ["a", "b"]


def test_generated_gap_keeps_clear_of_a_region_with_the_same_id(load):
    result = load(
        regions("    - { id: _gap_100, start: 0, size: 256 }\n    - { id: x, start: 0x800, size: 256 }")
    )
    assert codes(result) == []
    assert result.tree.index["_gap_100"].end == 0xFF
    gap = result.tree.index["_gap_100_"]
    assert gap.generated
    assert (gap.start, gap.end) == (0x100, 0x7FF)


def test_size_and_end_may_agree(load):
    result = load(regions("    - { id: m, start: 0x1000, size: 4K, end: 0x1FFF }"))
    assert codes(result) == []


SPACES = """version: 1
memory:
  regions:
    - id: flash
      start: 0x0
      size: 1M
      children:
        - { id: boot, size: 64K }
    - { id: ram, start: 0x20000000, size: 256K }
    - id: qspi
      space: ext
      start: 0x0
      size: 8M
      children:
        - { id: slot1, size: 1M }
    - { id: more, space: ext, size: 1M }
    - { id: otp, space: otp, size: 4K }
"""


def test_regions_in_other_spaces_may_overlap_and_place_on_their_own(load):
    result = load(SPACES)
    assert codes(result) == []
    assert spans(result, "/") == {
        "flash": (0, 0xFFFFF),
        "ram": (0x20000000, 0x2003FFFF),
        "qspi": (0, 0x7FFFFF),
        "more": (0x800000, 0x8FFFFF),  # placed after qspi, not after ram
        "otp": (0, 0xFFF),
    }
    index = result.tree.index
    assert (index["qspi"].space, index["slot1"].space, index["flash"].space) == ("ext", "ext", None)
    assert [child.space for child in result.tree.root.children if not child.generated] == [
        None, None, "ext", "ext", "otp"
    ]


def test_spaces_overlap_only_within_a_space(load):
    clash = "- { id: more, space: ext, start: 0x100, size: 1M }"
    result = load(SPACES.replace("- { id: more, space: ext, size: 1M }", clash))
    assert codes(result) == ["E011"]
    assert result.diagnostics[0].subject == "more"


def test_space_on_a_child_is_an_error(load):
    result = load(SPACES.replace("- { id: boot, size: 64K }", "- { id: boot, size: 64K, space: x }"))
    assert codes(result) == ["E004"]
    assert result.diagnostics[0].path[-1] == "space"


def test_gaps_are_filled_per_space(load):
    result = load(SPACES.replace("memory:\n", "memory:\n  fill_gaps: reserved\n"))
    gaps = [(c.space, c.start, c.end) for c in result.tree.root.children if c.generated]
    assert (None, 0x100000, 0x1FFFFFFF) in gaps
    assert ("ext", 0x900000, 0xFFFFFFFF) in gaps and ("otp", 0x1000, 0xFFFFFFFF) in gaps


def test_root_column_draws_the_default_space_and_default_view_adds_the_others(load):
    result = load(SPACES)
    columns = {layout.id: [n.id for n in layout.view.nodes] for layout in result.drawing.columns}
    assert [n for n in columns["main"] if not n.startswith("_gap")] == ["flash", "ram"]
    assert columns["qspi_space"][0] == "slot1"
    assert "more_space" in columns and "otp_space" in columns
    view = SPACES + "view:\n  columns:\n    - { id: q, root: qspi }\n  expand: [slot1]\n"
    result = load(view)
    assert codes(result) == [] and [c.id for c in result.drawing.columns] == ["q", "slot1_detail"]


def test_describe_names_the_space(load):
    result = load(SPACES)
    data = describe_data(result)
    by_id = {region["id"]: region for region in data["regions"]}
    assert by_id["qspi"]["space"] == "ext" and "space" not in by_id["flash"]
    assert "space" not in by_id["qspi"]["children"][0]
    assert '"qspi" [space ext]' in describe_text(result)


def test_root_column_needs_a_region_without_a_space(load):
    text = "version: 1\nmemory:\n  regions:\n    - { id: q, space: qspi, start: 0, size: 8M }\n"
    assert codes(load(text)) == []  # the default view draws q in a column of its own
    result = load(text + "view:\n  columns:\n    - { id: all, root: / }\n")
    assert codes(result) == ["E020"] and "every region has one" in result.diagnostics[0].message


def test_default_columns_of_named_spaces_follow_the_file(load):
    text = (
        "version: 1\nmemory:\n  regions:\n    - { id: q1, space: qspi, start: 0, size: 8M }\n"
        "    - { id: q2, space: qspi, size: 8M }\n    - { id: q3, space: other, start: 0, size: 1M }\n"
    )
    result = load(text)
    assert [c.id for c in result.drawing.columns] == ["q1_space", "q2_space", "q3_space"]
