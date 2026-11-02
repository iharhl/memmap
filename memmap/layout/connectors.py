"""Fan lines from a block's corners to a detail column's corners."""

from __future__ import annotations

from .columns import Block, ColumnLayout
from .primitives import INK, Polyline


def connector_lines(source_blocks: list[Block], target: ColumnLayout) -> list[Polyline]:
    """One straight line for the top edge and one for the bottom edge of the expanded region."""
    top = min(block.y for block in source_blocks)
    bottom = max(block.y + block.h for block in source_blocks)
    left = source_blocks[0].x
    right = left + source_blocks[0].w
    rightward = target.x >= left
    source_x = right if rightward else left
    target_x = target.x if rightward else target.x + target.width
    return [
        Polyline([(source_x, source_y), (target_x, target_y)], INK)
        for source_y, target_y in ((top, target.y), (bottom, target.y + target.height))
    ]
