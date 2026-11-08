"""Fan lines that skip a lane: the geometry helpers in memmap.layout.routing."""

import pytest

from memmap.layout.routing import (
    CLEARANCE,
    LINE_GAP,
    Box,
    Fan,
    _crosses,
    clear_top,
    route,
)

# A column between source and target, spanning y 100..500 at x 300..400.
BETWEEN = [Box(300, 100, 400, 500)]


def fan(target_top, height=100, source=(200, 300, 340), target_x=600):
    """A fan from a block's right edge at source[0], y source[1]..source[2], to a detail
    column whose left edge is at target_x."""
    x, top, bottom = source
    return Fan(x, top, bottom, target_x, target_top, target_top + height)


def test_fan_lines_stay_straight_when_they_miss_the_columns_between():
    lines = route(fan(0, height=50, source=(200, 0, 40)), BETWEEN)
    assert lines == [[(200, 0), (600, 0)], [(200, 40), (600, 50)]]


def test_fan_lines_bend_below_the_columns_between():
    lines = route(fan(520), BETWEEN)
    corner = 300 - CLEARANCE
    assert lines == [
        [(200, 300), (corner, 500 + CLEARANCE), (600, 520)],
        [(200, 340), (corner, 500 + CLEARANCE + LINE_GAP), (600, 620)],
    ]


def test_fan_lines_bend_above_the_columns_between():
    lines = route(fan(-30, height=100), BETWEEN)
    assert lines is not None
    assert [line[1] for line in lines] == [
        (300 - CLEARANCE, 100 - CLEARANCE - LINE_GAP),
        (300 - CLEARANCE, 100 - CLEARANCE),
    ]


def test_fan_lines_bend_on_the_far_side_for_a_zoom_to_the_left():
    lines = route(Fan(700, 300, 340, 100, 520, 620), BETWEEN)
    assert lines is not None
    assert lines[0][1] == (400 + CLEARANCE, 500 + CLEARANCE)


@pytest.mark.parametrize(
    "lanes_fan",
    [
        fan(250),  # the detail column sits level with the column between
        fan(520, source=(350, 300, 340)),  # the source edge is inside the column between
    ],
    ids=["level", "no_room"],
)
def test_fan_lines_that_cannot_go_around_give_none(lanes_fan):
    assert route(lanes_fan, BETWEEN) is None


def test_a_detail_column_moves_the_shorter_way_round():
    assert clear_top(fan(250), BETWEEN, header=-50) == 500 + CLEARANCE  # 258 down vs 264 up
    up = 100 - CLEARANCE - LINE_GAP - 100
    assert clear_top(fan(150), BETWEEN, header=-50) == up  # 164 up vs 358 down
    assert clear_top(fan(150), BETWEEN, header=up + 1) == 500 + CLEARANCE  # no room above


def test_a_detail_column_that_is_already_clear_stays():
    assert clear_top(fan(520), BETWEEN, header=0) == 520
    assert clear_top(fan(250), [], header=0) == 250


def test_a_vertical_segment_crosses_a_box_it_runs_through():
    assert _crosses((350, 0), (350, 600), BETWEEN[0])
    assert not _crosses((350, 0), (350, 90), BETWEEN[0])
