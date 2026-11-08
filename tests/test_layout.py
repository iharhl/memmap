import itertools
import math

import pytest
from conftest import MODES, ROOT, codes, drawn, regions

from memmap.describe import describe_data
from memmap.layout.columns import LANE_STACK_GAP, MAX_STRETCH
from memmap.layout.primitives import Line, Polyline
from memmap.layout.routing import _clear, column_box
from memmap.layout.text import text_width, wrap
from memmap.options import OptionResolver
from memmap.pipeline import run


def blocks(result, column=0):
    return drawn(result).columns[column].blocks


def block(result, region, column=0):
    return next(b for b in blocks(result, column) if b.node.id == region)


def test_blocks_stack_upward_without_gaps():
    stack = blocks(run(MODES))
    assert [b.start for b in stack] == sorted(b.start for b in stack)
    for lower, upper in itertools.pairwise(stack):
        assert upper.y + upper.h == pytest.approx(lower.y)


def test_equal_scale_gives_min_height_unless_text_needs_more(load):
    text = regions('    - id: m\n      size: 8K\n      children:\n'
                   '        - { id: a, size: 4K }\n        - { id: b, name: "1\\n2\\n3\\n4", size: 4K }')
    text += "view:\n  defaults:\n    scale: { mode: equal, min_height: 30 }\n  columns:\n    - { id: c, root: m }\n"
    result = load(text)
    assert block(result, "a").h == 30
    assert block(result, "b").h > 30


def test_weighted_scale_follows_log2():
    result = run(MODES, ["view.defaults.scale={mode: weighted, min_height: 1}"])
    assert block(result, "a").h == pytest.approx(math.log2(4096) * 3)
    assert block(result, "c").h == pytest.approx(max(math.log2(1024) * 3, 19.2))


def test_linear_scale_is_proportional_then_clamped():
    result = run(MODES, ["view.defaults.scale={mode: linear, min_height: 10}"])
    assert block(result, "a").h == pytest.approx(120)
    assert block(result, "c").h == pytest.approx(30)
    clamped = run(MODES, ["view.defaults.scale={mode: linear, max_height: 90}"])
    assert block(clamped, "a").h == 90


@pytest.mark.parametrize("mode", ["weighted", "equal", "linear"])
def test_region_heights_win_in_every_scale_mode(mode):
    sets = [
        f"view.defaults.scale.mode={mode}",
        "view.overrides=[{region: a, height: 77}, {region: b, height: 2x}]",
    ]
    result = run(MODES, sets)
    assert block(result, "a").h == 77
    assert block(result, "b").h == 48


def test_reserved_gaps_collapse_with_a_break_mark():
    result = run(MODES, ["view.defaults.scale={mode: linear, collapse_reserved_over: 2K, min_height: 24}"])
    gap = blocks(result)[1]
    assert gap.collapsed and gap.h == 24
    assert any(t.role == "break" for t in drawn(result).texts())


def test_sizes_print_below_the_name_of_normal_blocks_only():
    result = run(MODES, ["view.defaults.labels.size=true"])
    assert block(result, "a").lines == ["Alpha", "4 KB"]
    assert blocks(result)[1].lines == []
    assert next(t.text for t in drawn(result).texts() if t.role == "size") == "4 KB"


def test_reserved_label_can_be_shown_and_renamed():
    result = run(MODES, ["style.reserved={show_label: true, label: Free}"])
    assert blocks(result)[1].lines == ["Free"]


def test_bands_are_equal_and_split_reserved_space():
    result = run(ROOT / "examples" / "stm32f303_fig8.yaml")
    layout = drawn(result).columns[0]
    heights = {
        band: sum(b.h for b in group) for band, group in layout.band_groups.items()
    }
    assert sorted(heights) == list(range(8))
    assert max(heights.values()) == pytest.approx(min(heights.values()))
    # SRAM and Peripherals are drawn at min_height with reserved above them.
    assert block(result, "sram").h == 34
    assert block(result, "periph").h == 34
    assert len([b for b in layout.blocks if b.reserved and b.band in (3, 4, 5, 6)]) == 4


def test_band_indices_are_drawn():
    result = run(ROOT / "examples" / "stm32f303_fig8.yaml")
    assert [t.text for t in drawn(result).texts() if t.role == "band"] == list("01234567")


def test_region_spanning_bands_is_named_once(load):
    text = regions("    - { id: m, name: Big, size: 1G }") + (
        "view:\n  columns:\n    - { id: c, root: '/', range: [0, 0x3FFFFFFF],"
        " bands: 256M }\n"
    )
    result = load(text)
    pieces = [b for b in blocks(result) if b.node.id == "m"]
    assert len(pieces) == 4
    assert sum(1 for b in pieces if b.lines) == 1


def test_expansion_shortcut_creates_chained_detail_columns(load):
    text = regions(
        """    - id: code
      size: 512M
      children:
        - id: flash
          start: 0x08000000
          size: 64K
          children:
            - { id: boot, size: 16K }"""
    ) + "view:\n  expand: [code, flash]\n"
    result = load(text)
    assert codes(result) == []
    data = describe_data(result)
    assert {c["id"]: c["lane"] for c in data["columns"]} == {
        "main": 0, "code_detail": 1, "flash_detail": 2,
    }  # fmt: skip
    assert [(c["from"], c["to"]) for c in data["connectors"]] == [
        ("main", "code_detail"),
        ("code_detail", "flash_detail"),
    ]


def test_depth_flattens_levels(load):
    text = regions(
        """    - id: m
      size: 8K
      children:
        - id: n
          size: 8K
          children:
            - { id: a, size: 4K }
            - { id: b, size: 4K }"""
    ) + "view:\n  columns:\n    - { id: c, root: m, depth: 2 }\n"
    assert [b.node.id for b in blocks(load(text))] == ["a", "b"]


def test_range_clips_blocks_and_labels():
    result = run(MODES, ["view.columns[0].range=[0x3000, 0x3FFF]"])
    assert [(b.start, b.end) for b in blocks(result)] == [(0x3000, 0x3FFF)]


def test_columns_in_one_lane_do_not_overlap():
    result = run(ROOT / "examples" / "stm32f303_fig8.yaml")
    periph, code = drawn(result).columns[1:3]
    assert periph.y + periph.height <= code.y


def test_free_anchor_uses_offset():
    result = run(ROOT / "examples" / "stm32f051_fig10.yaml")
    columns = {layout.id: layout for layout in drawn(result).columns}
    assert columns["code_detail"].y - columns["periph_detail"].y == pytest.approx(270)


def test_negative_offset_stays_on_the_canvas():
    sets = ["view.columns[0].anchor=free", "view.columns[0].offset=-40"]
    result = run(MODES, sets)
    assert blocks(result)[-1].y > 0


def test_connectors_run_corner_to_corner():
    from memmap.layout.primitives import Polyline

    result = run(ROOT / "examples" / "stm32f303_fig8.yaml")
    lines = [p for p in drawn(result).primitives if isinstance(p, Polyline)]
    assert len(lines) == 4
    main, periph = drawn(result).columns[0], drawn(result).columns[1]
    source = block(result, "periph")
    assert lines[0].points == [
        (main.x + main.width, source.y),
        (periph.x, periph.y),
    ]
    assert lines[1].points == [
        (main.x + main.width, source.y + source.h),
        (periph.x, periph.y + periph.height),
    ]


def titles(result):
    return [(t.column, t.text) for t in drawn(result).texts() if t.role == "column_title"]


def title_of(result, column_id):
    return next(layout for layout in drawn(result).columns if layout.id == column_id)


def test_columns_can_have_titles():
    result = run(ROOT / "examples" / "custom_bootloader.yaml")
    assert titles(result) == [("flash_col", "Flash"), ("sram_col", "SRAM")]


def test_no_title_draws_nothing():
    assert titles(run(ROOT / "examples" / "stm32f303_fig8.yaml")) == []


def test_title_sits_centred_above_the_column():
    result = run(MODES, ["view.columns[0].title=Main"])
    layout = drawn(result).columns[0]
    text = next(t for t in drawn(result).texts() if t.role == "column_title")
    assert text.x == pytest.approx(layout.x + layout.width / 2)
    assert text.y < layout.y
    assert text.weight == "bold"


def test_expansion_title_labels_an_auto_created_column(load):
    text = regions("    - id: m\n      size: 8K\n      children:\n        - { id: a, size: 8K }")
    text += "view:\n  expand:\n    - { region: m, title: Zoomed }\n"
    assert titles(load(text)) == [("m_detail", "Zoomed")]


def test_expansion_title_beats_the_column_title():
    sets = [
        "view.columns[1].title=From column",
        "view.expand[0].title=From expansion",
    ]
    result = run(ROOT / "examples" / "stm32f303_fig8.yaml", sets)
    assert titles(result) == [("periph_detail", "From expansion")]


def test_long_titles_wrap_to_the_column_width():
    result = run(MODES, ["view.columns[0].title=A rather long title for a narrow column"])
    lines = [text for _, text in titles(result)]
    assert len(lines) > 1
    layout = drawn(result).columns[0]
    assert all(text_width(line, 11) * 1.08 <= layout.width for line in lines)


def test_multi_line_titles_keep_their_order_and_clear_the_blocks():
    result = run(MODES, ['view.columns[0].title="One\\nTwo"'])
    lines = [t for t in drawn(result).texts() if t.role == "column_title"]
    assert [t.text for t in lines] == ["One", "Two"]
    assert lines[0].y < lines[1].y < drawn(result).columns[0].y


def test_titled_and_untitled_columns_line_up():
    sets = ["view.columns[0].title=Flash", "view.columns[1].title=null"]
    result = run(ROOT / "examples" / "custom_bootloader.yaml", sets)
    first, second = drawn(result).columns
    assert first.y == second.y


def test_titles_of_columns_stacked_in_a_lane_do_not_hit_the_column_above():
    sets = ["view.columns[1].title=Peripherals", "view.columns[2].title=Code"]
    result = run(ROOT / "examples" / "stm32f303_fig8.yaml", sets)
    periph = title_of(result, "periph_detail")
    code = title_of(result, "code_detail")
    assert periph.y + periph.height <= code.y - code.title_height


def test_band_column_still_spans_titled_detail_columns():
    sets = ["view.columns[1].title=Peripherals", "view.columns[2].title=Code"]
    result = run(ROOT / "examples" / "stm32f303_fig8.yaml", sets)
    main = title_of(result, "main")
    code = title_of(result, "code_detail")
    assert main.y == pytest.approx(title_of(result, "periph_detail").y)
    assert main.y + main.height == pytest.approx(code.y + code.height)


def test_title_is_listed_by_describe():
    data = describe_data(run(ROOT / "examples" / "custom_bootloader.yaml"))
    assert [column["title"] for column in data["columns"]] == ["Flash", "SRAM"]
    assert {"column_title"} <= {label["role"] for label in data["labels"]}


def test_option_inheritance_order():
    config = run(ROOT / "examples" / "stm32f303_fig8.yaml").config
    assert config is not None
    resolver = OptionResolver(config)
    main = config.view.columns[0]
    options = resolver.options(main)
    assert options.origins["style.reserved.show_label"] == "column:main"
    assert options.origins["labels.address"] == "view.defaults"
    assert options.origins["style.reserved.fill"] == "style"
    assert options.origins["style.sector_ticks"] == "built-in"
    # The column beats the global style.
    assert options.style.reserved.show_label is False
    assert resolver.options(config.view.columns[1]).style.reserved.show_label is True


def test_override_can_be_limited_to_one_column():
    file = ROOT / "examples" / "stm32f303_fig8.yaml"
    result = run(file, ["view.overrides=[{region: periph, column: main, height: 77}]"])
    assert block(result, "periph").h >= 77
    other = run(file, ["view.overrides=[{region: periph, column: periph_detail, height: 77}]"])
    assert block(other, "periph").h == 34


@pytest.mark.parametrize(
    "overrides",
    [
        "[{region: periph, column: main, height: 77}, {region: periph, height: 99}]",
        "[{region: periph, height: 99}, {region: periph, column: main, height: 77}]",
    ],
)
def test_column_override_beats_a_region_wide_one_in_any_order(overrides):
    result = run(ROOT / "examples" / "stm32f303_fig8.yaml", [f"view.overrides={overrides}"])
    assert block(result, "periph").h == 77


def test_columns_that_expand_into_each_other_keep_separate_lanes(load):
    text = regions("    - { id: m, size: 4K }") + """view:
  columns:
    - { id: a, root: m }
    - { id: b, root: m }
  expand:
    - { region: m, from: a, to: b }
    - { region: m, from: b, to: a }
"""
    first, second = drawn(load(text)).columns
    assert first.lane != second.lane


def test_sector_ticks_follow_sectors_and_can_be_disabled():
    from memmap.layout.primitives import Line

    file = ROOT / "examples" / "custom_bootloader.yaml"
    ticks = [p for p in drawn(run(file)).primitives if isinstance(p, Line)]
    assert len(ticks) == 28  # 31 inner sector boundaries, less the 3 between regions
    off = drawn(run(file, ["style.sector_ticks=false"])).primitives
    assert not any(isinstance(p, Line) for p in off)


def test_color_palette_keeps_explicit_colors():
    from memmap.layout.primitives import Rect, Text

    result = run(ROOT / "examples" / "custom_bootloader.yaml")
    fills = {p.fill for p in drawn(result).primitives if isinstance(p, (Rect, Text))}
    assert {"#e07a5f", "#81b29a"} <= fills
    assert len(fills) > 4


def test_legend_needs_reserved_blocks():
    custom = run(ROOT / "examples" / "custom_bootloader.yaml")
    assert not any(t.role == "legend" for t in drawn(custom).texts())
    f303 = run(ROOT / "examples" / "stm32f303_fig8.yaml")
    assert [t.text for t in drawn(f303).texts() if t.role == "legend"] == ["Reserved"]


def test_figure_text_is_drawn():
    roles = {t.role: t.text for t in drawn(run(ROOT / "examples" / "stm32f303_fig8.yaml")).texts()}
    assert roles["title"] == "Figure 8. STM32F303x6/8 memory map"
    assert roles["footer"] == "MSv33150V1"


def test_wrap_honours_breaks_and_width():
    assert wrap("a\nb", 100, 11) == ["a", "b"]
    assert wrap("Flash memory", 45, 11) == ["Flash", "memory"]
    lines = wrap("x" * 40, 30, 11)
    assert len(lines) > 1
    assert all(text_width(line, 11) <= 30 for line in lines)


def test_long_names_wrap_and_grow_the_block(load):
    text = regions(f"    - {{ id: m, name: \"{'long words ' * 10}\", size: 4K }}")
    text += "view:\n  columns:\n    - { id: c, root: m, width: 60 }\n"
    result = load(text)
    assert len(block(result, "m").lines) > 3
    assert block(result, "m").h > 24


def test_long_notes_wrap_and_the_block_grows_to_hold_them(load):
    note = "copied to 0x2000 0000: datas, devices, log_dynamic_sections, k_timer_area, k_sem_area"
    text = regions(
        "    - id: m\n      size: 8K\n      children:\n"
        f'        - {{ id: a, size: 4K, note: "{note}" }}\n        - {{ id: b, size: 4K, note: short }}'
    )
    view = "view:\n  defaults:\n    scale: { mode: equal }\n  columns:\n    - { id: c, root: m }\n"
    result = load(text + view)
    assert codes(result) == []
    notes = [t for t in drawn(result).texts() if t.role == "note" and t.region == "a"]
    assert len(notes) > 1 and " ".join(t.text for t in notes) == note
    a = block(result, "a")
    assert all(a.y < t.y < a.y + a.h for t in notes)  # every line sits beside its own block
    assert a.h > block(result, "b").h  # equal scale, so only the note made it taller
    rules = [p for p in drawn(result).primitives if isinstance(p, Line) and p.x1 == p.x2]
    assert [(r.y1 > a.y, r.y2 < a.y + a.h) for r in rules if a.x + a.w < r.x1 < notes[0].x] == [
        (True, True)
    ]


def test_a_note_line_break_is_kept(load):
    text = regions('    - id: m\n      size: 4K\n      note: "one\\ntwo"')
    notes = [t.text for t in drawn(load(text)).texts() if t.role == "note"]
    assert notes == ["one", "two"]


def _zoom_config(lanes=""):
    """Two regions with 16 peripherals each, `low` at 0 and `high` at 64K, both zoomed."""

    def region(name, start):
        children = "\n".join(f"        - {{ id: {name}{i}, size: 1K }}" for i in range(16))
        head = f"    - id: {name}\n      start: {start:#x}\n      size: 16K\n"
        return f"{head}      children:\n{children}"

    body = regions(region("low", 0) + "\n" + region("high", 0x10000))
    return body + "view:\n  defaults:\n    scale: { mode: equal, min_height: 24 }\n" + lanes


def test_stacked_zooms_follow_address_order_not_definition_order(load):
    """Both detail columns are taller than their sources allow, so both want the top of the
    drawing; the one zoomed from higher addresses still goes on top."""
    result = load(_zoom_config("  expand: [low, high]\n"))
    assert codes(result) == []
    columns = {layout.id: layout for layout in drawn(result).columns}
    assert columns["high_detail"].y < columns["low_detail"].y


def test_a_zoom_two_lanes_away_goes_around_the_column_between(load):
    lanes = (
        "  columns:\n    - { id: main, root: '/' }\n"
        "    - { id: high_detail, root: high, lane: 1, anchor: top }\n"
        "    - { id: low_detail, root: low, lane: 2, anchor: bottom }\n"
        "  expand:\n"
        "    - { region: high, from: main, to: high_detail }\n"
        "    - { region: low, from: main, to: low_detail }\n"
    )
    result = load(_zoom_config(lanes))
    assert codes(result) == []
    columns = {layout.id: layout for layout in drawn(result).columns}
    between = column_box(columns["high_detail"])
    assert columns["low_detail"].y >= between.bottom
    fans = [p.points for p in drawn(result).primitives if isinstance(p, Polyline)]
    assert [len(points) for points in fans] == [2, 2, 3, 3]
    assert _clear(fans[2:], [between])  # the first two end at the column between
    link = describe_data(result)["connectors"][1]
    assert link["to"] == "low_detail" and len(link["lines"][0]) == 3


def test_fan_lines_through_a_column_warn_when_the_detail_column_is_pinned(load):
    lanes = (
        "  columns:\n    - { id: main, root: '/' }\n"
        "    - { id: high_detail, root: high, lane: 1, anchor: top }\n"
        "    - { id: low_detail, root: low, lane: 2, anchor: free }\n"
        "  expand:\n"
        "    - { region: high, from: main, to: high_detail }\n"
        "    - { region: low, from: main, to: low_detail }\n"
    )
    result = load(_zoom_config(lanes))
    assert codes(result) == ["W107"]
    assert "lane 1" in result.diagnostics[0].hint


def test_a_column_zooming_into_a_much_taller_one_is_stretched(load):
    """The overview has three 24 px blocks and its zoom sixteen: the overview grows to
    height of the zoom, capped at MAX_STRETCH times its own height."""
    result = load(_zoom_config("  columns:\n    - { id: main, root: '/' }\n  expand: [low]\n"))
    assert codes(result) == []
    columns = {layout.id: layout for layout in drawn(result).columns}
    main, detail = columns["main"], columns["low_detail"]
    natural = 24 * len(main.blocks)
    assert main.height == pytest.approx(min(MAX_STRETCH * natural, detail.height))
    assert len({round(b.h, 6) for b in main.blocks}) == 1  # equal scale stays equal
    assert detail.height == 16 * 24  # the zoom itself keeps its scale


@pytest.mark.parametrize(
    "expand",
    ["[low, high]", "[{ region: low, title: Low }, { region: high, title: High }]"],
    ids=["untitled", "titled"],
)
def test_a_stack_of_zooms_that_runs_past_the_bottom_moves_up(load, expand):
    """Both zooms hang from small blocks in a short overview, so stacked from where they come
    from they would run past the bottom of the drawing: the lane moves up to the header row,
    where the top one's title sits beside the overview's."""
    result = load(_zoom_config(f"  expand: {expand}\n"))
    columns = {layout.id: layout for layout in drawn(result).columns}
    main, high, low = columns["main"], columns["high_detail"], columns["low_detail"]
    assert high.y == pytest.approx(main.y)
    assert low.y == pytest.approx(high.y + high.height + LANE_STACK_GAP + low.title_height)


def test_a_column_is_only_stretched_for_its_own_zooms(load):
    """The overview zooms into a short column; a tall column elsewhere, which it does not zoom
    into, leaves it at its own height."""
    children = "\n".join(f"        - {{ id: b{i}, size: 1K }}" for i in range(30))
    body = regions(
        "    - id: small\n      start: 0\n      size: 2K\n      children:\n"
        "        - { id: s0, size: 1K }\n        - { id: s1, size: 1K }\n"
        f"    - id: big\n      start: 0x10000\n      size: 30K\n      children:\n{children}"
    )
    text = body + (
        "view:\n  defaults:\n    scale: { mode: equal, min_height: 24 }\n"
        "  columns:\n    - { id: main, root: '/' }\n    - { id: big_col, root: big, lane: 2 }\n"
        "  expand: [small]\n"
    )
    result = load(text)
    assert codes(result) == []
    columns = {layout.id: layout for layout in drawn(result).columns}
    assert columns["big_col"].height == 30 * 24
    assert columns["main"].height == 24 * len(columns["main"].blocks)


def _small_regions(*specs):
    """Regions at 64K steps from 0, each with `count` 1K children: specs are (id, count)."""
    parts = []
    for index, (name, count) in enumerate(specs):
        children = "\n".join(f"        - {{ id: {name}{i}, size: 1K }}" for i in range(count))
        head = f"    - id: {name}\n      start: {index * 0x10000:#x}\n      size: {count}K\n"
        parts.append(f"{head}      children:\n{children}")
    return regions("\n".join(parts))


def test_a_stack_with_an_offset_is_not_moved_up(load):
    """The offset pushes the zoom past the bottom of the drawing on purpose: it stays there."""
    view = (
        "view:\n  defaults:\n    scale: { mode: equal, min_height: 40 }\n"
        "  columns:\n    - { id: main, root: '/' }\n"
        "    - { id: flash_d, root: flash, anchor: bottom, offset: 60 }\n"
        "  expand:\n    - { region: flash, from: main, to: flash_d }\n"
    )
    result = load(_small_regions(("flash", 2), ("ram", 1)) + view)
    columns = {layout.id: layout for layout in drawn(result).columns}
    main, detail = columns["main"], columns["flash_d"]
    assert detail.y == pytest.approx(main.y + main.height - detail.height + 60)


def test_a_detour_stays_clear_below_the_columns_stacked_above_it(load):
    """`b_d` takes the room above `m_d`, so `a_d` cannot go around above it as well and goes
    around below instead of being pushed back into its way."""
    view = (
        "view:\n  defaults:\n    scale: { mode: equal, min_height: 24 }\n"
        "  columns:\n    - { id: main, root: '/' }\n"
        "    - { id: m_d, root: m, lane: 1, anchor: top, offset: 160 }\n"
        "    - { id: a_d, root: a, lane: 2 }\n    - { id: b_d, root: b, lane: 2 }\n"
        "    - { id: t, root: tall, lane: 3 }\n"
        "  expand:\n    - { region: a, from: main, to: a_d }\n"
        "    - { region: m, from: main, to: m_d }\n    - { region: b, from: main, to: b_d }\n"
    )
    result = load(_small_regions(("a", 1), ("m", 3), ("b", 1), ("tall", 24)) + view)
    assert codes(result) == []
    columns = {layout.id: layout for layout in drawn(result).columns}
    assert columns["b_d"].y < columns["m_d"].y < columns["a_d"].y


def test_a_chain_of_zooms_stretches_the_overview_to_the_grown_detail(load):
    """main zooms into flash, flash into app: flash grows first, then main grows to match it."""
    apps = "\n".join(f"            - {{ id: a{i}, size: 1K }}" for i in range(12))
    body = regions(
        "    - id: flash\n      start: 0\n      size: 1M\n      children:\n"
        "        - { id: boot, size: 64K }\n"
        f"        - id: app\n          size: 256K\n          children:\n{apps}\n"
        "            - { id: rest, size: auto }\n"
        "        - { id: cfg, size: 16K }\n"
        "    - { id: ram, start: 0x20000000, size: 64K }\n"
    )
    view = (
        "view:\n  defaults:\n    scale: { mode: equal, min_height: 28 }\n"
        "  columns:\n    - { id: main, root: '/' }\n  expand: [flash, app]\n"
    )
    result = load(body + view)
    assert codes(result) == []
    columns = {layout.id: layout for layout in drawn(result).columns}
    assert columns["flash_detail"].height == pytest.approx(columns["app_detail"].height)
    assert columns["main"].height == pytest.approx(columns["flash_detail"].height)
