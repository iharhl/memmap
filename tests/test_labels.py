import itertools

import pytest
from conftest import MODES, ROOT, drawn

from memmap.describe import describe_data
from memmap.layout.text import ADDRESS_FONT_SIZE
from memmap.pipeline import run


def address_labels(result, column=None):
    return [
        label["text"]
        for label in describe_data(result)["labels"]
        if label["role"] == "address" and column in (None, label["column"])
    ]


# Blocks of modes.yaml, bottom to top: a, gap, b, c, gap.
EXPECTED = {
    "start": [
        "0x0000 1000", "0x0000 2000", "0x0000 3000", "0x0000 4000", "0x0000 4400", "0x0000 4FFF",
    ],
    "both": [
        "0x0000 1000",
        "0x0000 2000", "0x0000 1FFF",
        "0x0000 3000", "0x0000 2FFF",
        "0x0000 4000", "0x0000 3FFF",
        "0x0000 4400", "0x0000 43FF",
        "0x0000 4FFF",
    ],
    "none": [],
}  # fmt: skip


@pytest.mark.parametrize("mode", EXPECTED)
def test_address_modes_print_exactly_the_described_strings(mode):
    result = run(MODES, [f"view.defaults.labels.address={mode}"])
    assert address_labels(result) == EXPECTED[mode]
    svg_texts = [t.text for t in drawn(result).texts() if t.role == "address"]
    assert svg_texts == EXPECTED[mode]


def test_both_places_start_above_and_end_below_the_line():
    result = run(MODES, ["view.defaults.labels.address=both"])
    layout = drawn(result).columns[0]
    labels = {label.text: label for label in layout.labels}
    line = next(block for block in layout.blocks if block.node.id == "b").y
    assert labels["0x0000 4000"].y < line < labels["0x0000 3FFF"].y


def test_labels_never_overlap_in_a_column():
    result = run(MODES, ["view.defaults.labels.address=both", "view.defaults.scale.mode=equal"])
    layout = drawn(result).columns[0]
    ys = sorted(label.y for label in layout.labels)
    spacing = ADDRESS_FONT_SIZE + 1
    assert all(b - a >= spacing - 1e-9 for a, b in itertools.pairwise(ys))


def test_bands_print_band_boundaries_and_the_top():
    result = run(ROOT / "examples" / "stm32f303_fig8.yaml")
    assert address_labels(result, "main") == [
        "0x0000 0000", "0x2000 0000", "0x4000 0000", "0x6000 0000", "0x8000 0000",
        "0xA000 0000", "0xC000 0000", "0xE000 0000", "0xFFFF FFFF",
    ]  # fmt: skip


def test_f303_peripheral_labels_match_the_datasheet():
    result = run(ROOT / "examples" / "stm32f303_fig8.yaml")
    assert address_labels(result, "periph_detail") == [
        "0x4000 0000", "0x4000 A000", "0x4001 0000", "0x4001 6C00", "0x4002 0000",
        "0x4002 43FF", "0x4800 0000", "0x4800 1800", "0x5000 0000", "0x5000 03FF",
    ]  # fmt: skip


@pytest.mark.parametrize(
    "top_label, strings",
    [
        ("end", ["0x0000 3FFF"]),
        ("start_of_next", ["0x0000 4000"]),
    ],
)
def test_top_label_override(top_label, strings):
    sets = [
        f"view.overrides=[{{region: b, top_label: {top_label}}}]",
        "view.defaults.labels.address=start",
    ]
    labels = address_labels(run(MODES, sets))
    expected = list(EXPECTED["start"])
    expected[3:4] = strings
    assert labels == expected


def test_none_keeps_only_overridden_edges():
    sets = ["view.defaults.labels.address=none", "view.overrides=[{region: b, top_label: end}]"]
    assert address_labels(run(MODES, sets)) == ["0x0000 3FFF"]


def test_none_leaves_no_room_for_addresses():
    layout = drawn(run(MODES, ["view.defaults.labels.address=none"])).columns[0]
    assert layout.left_gutter == 0


def test_none_hides_addresses_in_one_column_only():
    result = run(ROOT / "examples" / "stm32f303_fig8.yaml", ["view.columns[2].labels.address=none"])
    assert address_labels(result, "code_detail") == []
    assert address_labels(result, "periph_detail")


def test_override_forces_a_label_on_an_edge_bands_would_skip():
    sets = ["view.columns[0].bands=16K", "view.overrides=[{region: b, top_label: end}]"]
    assert address_labels(run(MODES, sets)) == ["0x0000 1000", "0x0000 3FFF", "0x0000 4FFF"]


def test_override_at_the_column_top_of_the_address_space_stays_in_range(load):
    text = """version: 1
memory:
  regions:
    - { id: m, start: 0xFFFFF000, size: 4K }
view:
  columns:
    - { id: c, root: "/", range: [0xFFFFF000, 0xFFFFFFFF] }
  overrides:
    - { region: m, top_label: start_of_next }
"""
    assert address_labels(load(text))[-1] == "0xFFFF FFFF"


def test_unfilled_space_shows_the_end_below_it(load):
    text = """version: 1
memory:
  fill_gaps: none
  regions:
    - id: m
      size: 64K
      children:
        - { id: a, size: 1K }
        - { id: b, start: 0x8000, size: 1K }
view:
  columns:
    - { id: c, root: m }
"""
    assert sorted(address_labels(load(text))) == [
        "0x0000 0000", "0x0000 03FF", "0x0000 8000", "0x0000 83FF",
    ]  # fmt: skip


def test_addresses_sit_left_of_the_column():
    result = run(MODES)
    layout = drawn(result).columns[0]
    texts = [t for t in drawn(result).texts() if t.role == "address"]
    assert all(text.x < layout.x for text in texts)
    assert all(text.anchor == "end" for text in texts)
