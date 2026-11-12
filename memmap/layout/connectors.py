"""Fan lines from a block's corners to a detail column's corners."""

from __future__ import annotations

from .columns import Block, ColumnLayout
from .primitives import INK, Polyline
from .routing import Box, make_fan, route, straight

DASH = "4 3"  # px on, px off, for style.connectors: dashed


def connector_lines(
    source_blocks: list[Block], target: ColumnLayout, between: list[Box], dashed: bool = False
) -> tuple[list[Polyline], bool]:
    """One line for the top edge and one for the bottom edge of the expanded region. Each is
    straight, or bent at a corner to go around `between`, the columns in the lanes the lines
    cross. The flag is False when they cannot go around and run straight through them. With
    `dashed` the lines are drawn dashed."""
    top = min(block.y for block in source_blocks)
    bottom = max(block.y + block.h for block in source_blocks)
    left = source_blocks[0].x
    fan = make_fan(left, left + source_blocks[0].w, top, bottom, target)
    lines = route(fan, between)
    clear = lines is not None
    if lines is None:
        lines = straight(fan)
    dash = DASH if dashed else None
    return [Polyline(points, INK, dash=dash) for points in lines], clear
