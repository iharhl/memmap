"""Column layout: blocks, band splitting, heights, lanes and vertical placement."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..diagnostics import Diagnostic
from ..options import OptionResolver, Options
from ..resolve import Node
from ..units import format_size
from ..view import ColumnView, Link, ViewPlan
from .heights import scaled_height, text_height
from .labels import AddressLabel, build_labels
from .text import ADDRESS_FONT_SIZE, BOLD_WIDTH_FACTOR, line_height, text_width, wrap

TEXT_INSET = 8  # px, taken off the column width when wrapping text inside a block
LABEL_GAP = 8  # px, between a column and the labels or notes beside it
NOTE_FONT_FACTOR = 0.85  # note size, relative to style.font_size
LANE_STACK_GAP = 24  # px, between columns stacked in one lane
MIN_FAN_ROOM = 16  # px of fan line kept clear of the next lane's left gutter
COLUMN_TITLE_GAP = 6  # px, between a column title and the column
COLUMN_GAP = 160  # px, least distance between lanes, where the fan lines run


@dataclass
class Block:
    """One rectangle of a column. Bands can cut a region into several blocks that share a `node`;
    `display_size` is the size before that cut. `y` is the top edge, in pixels."""

    node: Node
    column: str
    start: int
    end: int
    top_label: str | None = None
    height_override: float | str | None = None
    display_size: int = 0
    show_name: bool = True
    band: int | None = None
    collapsed: bool = False
    lines: list[str] = field(default_factory=list)
    note: str | None = None
    x: float = 0
    y: float = 0
    w: float = 0
    h: float = 0

    @property
    def reserved(self) -> bool:
        """Whether the block's region is reserved."""
        return self.node.reserved


@dataclass
class ColumnLayout:
    """A column as layout works on it. `build_column` sets the blocks and measurements, then
    `assign_lanes` and the place_ functions set `lane`, `x` and `y`. The gutters are the room for
    labels and notes either side of the blocks."""

    view: ColumnView
    options: Options
    blocks: list[Block] = field(default_factory=list)
    band_groups: dict[int, list[Block]] = field(default_factory=dict)
    labels: list[AddressLabel] = field(default_factory=list)
    title_lines: list[str] = field(default_factory=list)
    title_height: float = 0
    lane: int = 0
    x: float = 0
    y: float = 0
    width: float = 0
    height: float = 0
    left_gutter: float = 0
    right_gutter: float = 0

    @property
    def id(self) -> str:
        """The column's id."""
        return self.view.id


def _split_bands(blocks: list[Block], low: int, band_size: int) -> list[Block]:
    """Cut blocks at band boundaries. A reserved block keeps its label on every piece; other
    blocks print their name once, on the middle piece."""
    pieces: list[Block] = []
    for block in blocks:
        parts = []
        start = block.start
        while start <= block.end:
            band = (start - low) // band_size
            end = min(block.end, low + (band + 1) * band_size - 1)
            parts.append((start, end, band))
            start = end + 1
        for index, (start, end, band) in enumerate(parts):
            pieces.append(
                Block(
                    node=block.node,
                    column=block.column,
                    start=start,
                    end=end,
                    top_label=block.top_label if end == block.end else None,
                    height_override=block.height_override,
                    display_size=block.display_size,
                    show_name=block.reserved or index == len(parts) // 2,
                    band=band,
                )
            )
    return pieces


def _block_name(block: Block, options: Options) -> str | None:
    """The name printed in a block. None for a reserved block without a label."""
    style = options.style
    if not block.reserved:
        return block.node.name
    if style.reserved.show_label:
        return block.node.name if block.node.has_name else style.reserved.label
    return None


def _block_text(block: Block, width: float, options: Options) -> None:
    """Work out the lines inside the block and the note to its right."""
    if not block.show_name:
        return
    name = _block_name(block, options)
    block.lines = wrap(name, width, options.style.font_size) if name else []
    if options.labels.size and not block.reserved:
        block.lines.append(format_size(block.display_size))
    block.note = block.node.note


def _check_fit(blocks: list[Block], width: float, options: Options, out: list[Diagnostic]) -> None:
    """Warn, once per region, about text that wrapping could not fit."""
    size = options.style.font_size
    for block in {block.node.id: block for block in blocks if block.show_name}.values():
        name = _block_name(block, options) or ""
        hint = "shorten the name or widen the column"
        if any(text_width(word, size) > width - TEXT_INSET for word in name.split()):
            message = f"name is split inside a word to fit a column {width:g} px wide"
            out.append(Diagnostic("W105", message, block.node.path, hint, block.node.id))
        if any(text_width(line, size) > width for line in block.lines):
            message = f"text is wider than the {width:g} px block"
            out.append(Diagnostic("W106", message, block.node.path, hint, block.node.id))


def _assign_heights(column: ColumnLayout, out: list[Diagnostic]) -> None:
    """Set each block's height from its scale, growing it to fit its text (W102 past
    max_height). A reserved block over collapse_reserved_over collapses to min_height."""
    blocks = column.blocks
    scale = column.options.scale
    total = sum(block.end - block.start + 1 for block in blocks) or 1
    for block in blocks:
        size = block.end - block.start + 1
        override = block.height_override
        threshold = scale.collapse_reserved_over
        if block.reserved and threshold is not None and size > threshold:
            block.collapsed = True
            override = override if override is not None else scale.min_height
        wanted = scaled_height(size, scale, total, override)
        needed = text_height(len(block.lines), column.options.style.font_size)
        block.h = max(wanted, needed)
        if scale.max_height is not None and needed > scale.max_height:
            out.append(
                Diagnostic(
                    "W102",
                    f"label text forced the block to {needed:g} px, over max_height "
                    f"{scale.max_height:g}",
                    block.node.path,
                    "shorten the name, widen the column, or raise max_height",
                    block.node.id,
                )
            )


def _slack_block(group: list[Block]) -> Block:
    """The block that absorbs a band's spare height: the largest reserved one, if any."""
    ranked = sorted(
        enumerate(group),
        key=lambda item: (item[1].reserved, item[1].end - item[1].start, item[0]),
    )
    return ranked[-1][1]


def _level_bands(column: ColumnLayout) -> None:
    """Make every band the same height."""
    groups = column.band_groups
    if not groups:
        return
    height = max(sum(block.h for block in group) for group in groups.values())
    for group in groups.values():
        _slack_block(group).h += height - sum(block.h for block in group)


def stretch_bands(column: ColumnLayout, height: float) -> None:
    """Grow a band column to `height`, so it spans the detail columns next to it."""
    groups = column.band_groups
    if not groups or height <= column.height:
        return
    extra = (height - column.height) / len(groups)
    for group in groups.values():
        _slack_block(group).h += extra
    column.height = sum(block.h for block in column.blocks)


def build_column(
    view: ColumnView, resolver: OptionResolver, address_bits: int, out: list[Diagnostic]
) -> ColumnLayout:
    """Build a column up to its measurements: blocks, bands, text, heights and labels.
    Positions come later."""
    spec = view.spec
    layout = ColumnLayout(view, resolver.options(spec), width=spec.width)
    blocks: list[Block] = []
    for node in view.nodes:
        start, end = max(node.start, view.start), min(node.end, view.end)
        if start > end:
            continue
        blocks.append(
            Block(
                node=node,
                column=spec.id,
                start=start,
                end=end,
                top_label=resolver.override_value("top_label", node.id, spec.id),
                height_override=resolver.override_value("height", node.id, spec.id),
                display_size=end - start + 1,
            )
        )

    band_starts: set[int] = set()
    if spec.bands:
        blocks = _split_bands(blocks, view.start, spec.bands)
        band_starts = set(range(view.start, view.end + 1, spec.bands))
        for block in blocks:
            assert block.band is not None
            layout.band_groups.setdefault(block.band, []).append(block)
    layout.blocks = blocks

    for block in blocks:
        _block_text(block, spec.width - TEXT_INSET, layout.options)
    _check_fit(blocks, spec.width, layout.options, out)
    _assign_heights(layout, out)
    _level_bands(layout)
    layout.height = sum(block.h for block in blocks)

    layout.labels = build_labels(blocks, band_starts, layout.options.labels.address, address_bits)
    _measure_gutters(layout)
    _measure_title(layout)
    return layout


def _measure_title(layout: ColumnLayout) -> None:
    """Wrap the column's title and set the height it takes."""
    title = layout.view.title
    if not title:
        return
    size = layout.options.style.font_size
    layout.title_lines = wrap(title, layout.width / BOLD_WIDTH_FACTOR, size)
    layout.title_height = len(layout.title_lines) * line_height(size) + COLUMN_TITLE_GAP


def _measure_gutters(layout: ColumnLayout) -> None:
    """Set the room for labels and band numbers on the left, and for notes on the right."""
    width = max((label.width for label in layout.labels), default=0)
    left = width + LABEL_GAP if width else 0
    if layout.view.spec.bands:
        left = max(left, text_width("00", ADDRESS_FONT_SIZE) + LABEL_GAP)
    note_size = layout.options.style.font_size * NOTE_FONT_FACTOR
    notes = [text_width(block.note, note_size) for block in layout.blocks if block.note]
    layout.left_gutter = left
    layout.right_gutter = max(notes) + LABEL_GAP if notes else 0


def dependency_order(plan: ViewPlan) -> list[ColumnView]:
    """Columns with each source before its targets, otherwise in definition order."""
    targets: dict[str, list[ColumnView]] = {}
    has_source = set()
    for link in plan.links:
        targets.setdefault(link.source.id, []).append(link.target)
        has_source.add(link.target.id)
    ordered: list[ColumnView] = []

    def visit(column: ColumnView) -> None:
        """Add `column` and, depth first, everything it expands into."""
        if column in ordered:
            return
        ordered.append(column)
        for target in targets.get(column.id, []):
            visit(target)

    # Columns with no source go first; anything left is part of a cycle.
    for column in sorted(plan.columns, key=lambda column: column.id in has_source):
        visit(column)
    return ordered


def assign_lanes(plan: ViewPlan, layouts: dict[str, ColumnLayout]) -> None:
    """Set each column's lane: its own `lane`, else one right of its source, else the next
    free one."""
    source_of: dict[str, str] = {}
    for link in plan.links:
        source_of.setdefault(link.target.id, link.source.id)
    next_free = 0
    for column in dependency_order(plan):
        layout = layouts[column.id]
        if column.spec.lane is not None:
            layout.lane = column.spec.lane
        elif column.id in source_of:
            layout.lane = layouts[source_of[column.id]].lane + 1
        else:
            layout.lane = next_free
        next_free = max(next_free, layout.lane + 1)


def place_horizontally(layouts: list[ColumnLayout]) -> None:
    """Set each column's x. Lanes run left to right and the columns of a lane share an x."""
    lanes = sorted({layout.lane for layout in layouts})
    x = 0.0
    for position, lane in enumerate(lanes):
        members = [layout for layout in layouts if layout.lane == lane]
        left = max(layout.left_gutter for layout in members)
        if position == 0:
            x = left
        else:
            x += max(COLUMN_GAP, left + MIN_FAN_ROOM)
        for layout in members:
            layout.x = x
        x += max(layout.width + layout.right_gutter for layout in members)


def header_height(layouts: list[ColumnLayout]) -> float:
    """The row above the blocks that holds column titles, so untitled columns line up too."""
    return max(layout.title_height for layout in layouts)


def content_height(layouts: list[ColumnLayout]) -> float:
    """The tallest lane including the header row, counting columns in one lane as stacked. The
    first column's title sits in the header row, every other one takes room of its own."""
    header = header_height(layouts)
    height = header
    for lane in {layout.lane for layout in layouts}:
        stacked = [
            layout
            for layout in layouts
            if layout.lane == lane and layout.view.spec.anchor != "free"
        ]
        if not stacked:
            continue
        titles = [layout.title_height for layout in stacked]
        blocks = sum(layout.height for layout in stacked)
        gaps = LANE_STACK_GAP * (len(stacked) - 1)
        height = max(height, header + blocks + sum(titles) - min(titles) + gaps)
    return height


def _desired_y(
    layout: ColumnLayout,
    source: tuple[float, float] | None,
    height: float,
    header: float,
) -> float:
    """Where a column wants its top, from its anchor and offset. `source` is the y range of
    the expanded block, when that is known."""
    spec = layout.view.spec
    if spec.anchor == "bottom":
        y = height - layout.height
    elif spec.anchor == "source" and source is not None:
        y = (source[0] + source[1]) / 2 - layout.height / 2
        y = max(header, min(y, height - layout.height))
    else:
        y = header
    return y + spec.offset


def set_y(layout: ColumnLayout, y: float) -> None:
    """Move a column's top to `y` and stack its blocks from there, last block on top."""
    layout.y = y
    cursor = y
    for block in reversed(layout.blocks):
        block.y = cursor
        block.x = layout.x
        block.w = layout.width
        cursor += block.h


def place_vertically(
    plan: ViewPlan, layouts: dict[str, ColumnLayout], height: float, header: float
) -> None:
    """Set each column's y, lane by lane from the left. The columns of a lane stack without
    overlapping, apart from `free` ones."""
    first_link: dict[str, Link] = {}
    for link in plan.links:
        first_link.setdefault(link.target.id, link)
    placed: set[str] = set()
    for lane in sorted({layout.lane for layout in layouts.values()}):
        members = [column for column in plan.columns if layouts[column.id].lane == lane]
        wanted = []
        for column in members:
            layout = layouts[column.id]
            incoming = first_link.get(column.id)
            source = None
            if incoming is not None and incoming.source.id in placed:
                pieces = [
                    b for b in layouts[incoming.source.id].blocks if b.node is incoming.region
                ]
                if pieces:
                    source = (
                        min(block.y for block in pieces),
                        max(block.y + block.h for block in pieces),
                    )
            wanted.append((_desired_y(layout, source, height, header), layout))
        floor = None
        for y, layout in sorted(wanted, key=lambda item: item[0]):
            if layout.view.spec.anchor == "free":
                set_y(layout, y)
            else:
                if floor is not None:
                    y = max(y, floor + layout.title_height)
                set_y(layout, y)
                floor = y + layout.height + LANE_STACK_GAP
            placed.add(layout.id)
