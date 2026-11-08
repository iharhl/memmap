"""Fan lines that skip a lane: keeping them clear of the columns in the lanes they cross.

A zoom whose detail column is two or more lanes from its source crosses the lanes between. A
straight fan line through a column there reads as if that column's block were the one expanded,
so the detail column is moved up or down until its fan lines can go around the columns between,
bending at a corner beside them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

from .text import ADDRESS_FONT_SIZE

if TYPE_CHECKING:
    from .columns import ColumnLayout

CLEARANCE = 8  # px, between a fan line that goes around a column and that column's labels
LINE_GAP = 6  # px, between the two fan lines where they bend around a column

Point = tuple[float, float]


@dataclass(frozen=True)
class Box:
    """The area a column covers, with its title, address labels and notes."""

    left: float
    top: float
    right: float
    bottom: float


@dataclass(frozen=True)
class Fan:
    """Where the two fan lines of a zoom start and end. The source side is the expanded
    block's edge facing the detail column, the target side the detail column's edge."""

    source_x: float
    source_top: float
    source_bottom: float
    target_x: float
    target_top: float
    target_bottom: float

    def moved(self, target_top: float) -> Fan:
        """The same fan with the detail column's top at `target_top`."""
        height = self.target_bottom - self.target_top
        return Fan(
            self.source_x,
            self.source_top,
            self.source_bottom,
            self.target_x,
            target_top,
            target_top + height,
        )


def column_box(layout: ColumnLayout) -> Box:
    """The area of a placed column, from its title down to below its bottom address label.
    Address labels are centred on block edges, so the top one reaches half a line above the
    column; the bottom one, under the last edge, a whole line below it."""
    return Box(
        layout.x - layout.left_gutter,
        layout.y - layout.title_height - ADDRESS_FONT_SIZE / 2,
        layout.x + layout.width + layout.right_gutter,
        layout.y + layout.height + ADDRESS_FONT_SIZE,
    )


def lanes_between(
    layouts: Iterable[ColumnLayout], source_lane: int, target_lane: int
) -> list[ColumnLayout]:
    """The columns in the lanes strictly between a source and a target lane."""
    low, high = sorted((source_lane, target_lane))
    return [layout for layout in layouts if low < layout.lane < high]


def make_fan(
    source_left: float, source_right: float, top: float, bottom: float, target: ColumnLayout
) -> Fan:
    """The fan from an expanded block's y range to a placed detail column."""
    rightward = target.x >= source_left
    return Fan(
        source_right if rightward else source_left,
        top,
        bottom,
        target.x if rightward else target.x + target.width,
        target.y,
        target.y + target.height,
    )


def straight(fan: Fan) -> list[list[Point]]:
    """The two fan lines drawn straight, top edge first."""
    return [
        [(fan.source_x, fan.source_top), (fan.target_x, fan.target_top)],
        [(fan.source_x, fan.source_bottom), (fan.target_x, fan.target_bottom)],
    ]


def _crosses(start: Point, end: Point, box: Box) -> bool:
    """Whether the segment from `start` to `end` runs through `box`."""
    (x0, y0), (x1, y1) = sorted((start, end))
    left, right = max(x0, box.left), min(x1, box.right)
    if left > right:
        return False
    if x1 == x0:
        ys = [y0, y1]
    else:
        ys = [y0 + (y1 - y0) * (x - x0) / (x1 - x0) for x in (left, right)]
    return min(ys) < box.bottom and max(ys) > box.top


def _clear(lines: list[list[Point]], boxes: list[Box]) -> bool:
    """Whether no segment of `lines` runs through any of `boxes`."""
    return not any(
        _crosses(start, end, box)
        for line in lines
        for start, end in pairwise(line)
        for box in boxes
    )


def route(fan: Fan, boxes: list[Box]) -> list[list[Point]] | None:
    """The two fan lines, top edge first, as point lists: straight when that is clear of
    `boxes`, else bent at a corner beside them to pass below or above them. None when the
    detail column sits where neither works."""
    lines = straight(fan)
    if _clear(lines, boxes):
        return lines
    rightward = fan.target_x >= fan.source_x
    x = (
        min(box.left for box in boxes) - CLEARANCE
        if rightward
        else max(box.right for box in boxes) + CLEARANCE
    )
    if (x - fan.source_x) * (fan.target_x - x) <= 0:  # no room before the first column
        return None
    below = max(box.bottom for box in boxes) + CLEARANCE
    above = min(box.top for box in boxes) - CLEARANCE
    if fan.target_top >= below:
        corners = (below, below + LINE_GAP)
    elif fan.target_bottom <= above - LINE_GAP:
        corners = (above - LINE_GAP, above)
    else:
        return None
    lines = [
        [
            (fan.source_x, fan.source_top),
            (x, corners[0]),
            (fan.target_x, fan.target_top),
        ],
        [
            (fan.source_x, fan.source_bottom),
            (x, corners[1]),
            (fan.target_x, fan.target_bottom),
        ],
    ]
    return lines if _clear(lines, boxes) else None


def clear_top(fan: Fan, boxes: list[Box], header: float) -> float:
    """The detail column top nearest to `fan.target_top` from which its fan lines can go
    around `boxes`: unchanged when they already can, else just below or just above them,
    whichever moves it less. Above is only an option when the column still fits under the
    header row."""
    if not boxes or route(fan, boxes) is not None:
        return fan.target_top
    height = fan.target_bottom - fan.target_top
    below = max(box.bottom for box in boxes) + CLEARANCE
    above = min(box.top for box in boxes) - CLEARANCE - LINE_GAP - height
    options = [max(fan.target_top, below)]
    if header <= above < fan.target_top:
        options.append(above)
    workable = [top for top in options if route(fan.moved(top), boxes) is not None]
    return min(workable or options, key=lambda top: abs(top - fan.target_top))
