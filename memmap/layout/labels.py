"""Address labels: which strings go on which edge, and where they sit."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..units import format_address
from .text import ADDRESS_FONT_SIZE, text_width

if TYPE_CHECKING:
    from .columns import Block, ColumnLayout

FAN_CLEARANCE = 1  # px, extra distance between a column-end address and its fan line


@dataclass
class AddressLabel:
    """One address printed left of a column, vertically centred on `y`."""

    value: int
    role: str  # start | end
    edge: int  # 0 = column bottom, i = below block i, len(blocks) = column top
    placement: str  # center | above | below
    region: str
    text: str = ""
    y: float = 0
    width: float = 0


def internal_values(lower: Block, upper: Block, mode: str) -> list[tuple[int, str, str]]:
    """(value, role, placement) for the edge between `lower` (A) and `upper` (B). Blocks with
    unfilled space between them always get both, as one address cannot describe that edge."""
    if mode == "both" or lower.end + 1 != upper.start:
        return [(upper.start, "start", "above"), (lower.end, "end", "below")]
    return [(upper.start, "start", "center")]


def _override_values(
    lower: Block, upper: Block | None, top_label: str, top_of_space: bool
) -> list[tuple[int, str, str]]:
    """Values for a per-edge override of the top of `lower`; `upper` is None at the column top."""
    if top_label == "end" or top_of_space:
        return [(lower.end, "end", "center")]
    next_start = upper.start if upper is not None else lower.end + 1
    return [(next_start, "start", "center")]


def build_labels(
    blocks: list[Block], band_starts: set[int], mode: str, address_bits: int
) -> list[AddressLabel]:
    """All address labels of one column, bottom to top. In a column with bands only the band
    boundaries, the column ends and overridden edges are labelled."""
    labels: list[AddressLabel] = []
    last_address = 2**address_bits - 1

    def add(values: list[tuple[int, str, str]], edge: int, region: str) -> None:
        """Turn (value, role, placement) entries into labels on `edge`."""
        for value, role, placement in values:
            label = AddressLabel(value, role, edge, placement, region)
            label.text = format_address(value, address_bits // 4)
            label.width = text_width(label.text, ADDRESS_FONT_SIZE)
            labels.append(label)

    if not blocks:
        return labels
    add([(blocks[0].start, "start", "center")], 0, blocks[0].node.id)

    for index, lower in enumerate(blocks):
        upper = blocks[index + 1] if index + 1 < len(blocks) else None
        top_of_space = upper is None and lower.end == last_address
        if lower.top_label is not None:
            values = _override_values(lower, upper, lower.top_label, top_of_space)
        elif upper is None:
            values = [(lower.end, "end", "center")]
        elif band_starts and upper.start not in band_starts:
            values = []
        else:
            values = internal_values(lower, upper, mode)
        add(values, index + 1, lower.node.id)
    return labels


def place_labels(layout: ColumnLayout, fan_on_label_side: bool) -> None:
    """Set each label's y, then push labels up until none overlap."""
    blocks = layout.blocks
    if not blocks:
        return
    spacing = ADDRESS_FONT_SIZE + 1
    last = len(blocks)
    for label in layout.labels:
        if label.edge == last:
            edge_y = blocks[-1].y
        else:
            edge_y = blocks[label.edge].y + blocks[label.edge].h
        placement = label.placement
        clearance = spacing / 2
        if fan_on_label_side and placement == "center" and label.edge in (0, last):
            # Keep the column's end addresses clear of the fan line reaching that corner.
            placement = "below" if label.edge == 0 else "above"
            clearance += FAN_CLEARANCE
        offset = {"center": 0, "above": -clearance, "below": clearance}[placement]
        label.y = edge_y + offset
    ordered = sorted(layout.labels, key=lambda item: -item.y)
    for below, above in itertools.pairwise(ordered):
        above.y = min(above.y, below.y - spacing)
