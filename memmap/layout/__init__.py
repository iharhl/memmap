"""Layout: turn the resolved tree and view into drawing primitives."""

from __future__ import annotations

from itertools import combinations, pairwise

from ..diagnostics import Diagnostic, Path
from ..options import OptionResolver
from ..resolve import ROOT_ID, Tree
from ..schema import Config, Style
from ..view import ViewPlan
from .columns import (
    COLUMN_TITLE_GAP,
    LABEL_GAP,
    Block,
    ColumnLayout,
    assign_lanes,
    build_column,
    content_height,
    header_height,
    note_indent,
    note_size,
    place_horizontally,
    place_vertically,
    stretch_bands,
    stretch_sources,
)
from .connectors import connector_lines
from .labels import place_labels
from .primitives import INK, PAPER, Drawing, Line, Primitive, Rect, Text
from .routing import column_box, lanes_between
from .text import ADDRESS_FONT_SIZE, BASELINE_SHIFT, line_height, text_width

MARGIN = 10  # px, between the canvas edge and the frame
FRAME_PADDING = 20  # px, between the frame and the columns
TITLE_HEIGHT = 28  # px, row above the frame for the figure title
TITLE_FACTOR = 1.2  # figure title size, relative to style.font_size
FOOTER_ROW = 24  # px, row at the bottom of the frame for the legend and footer
FOOTER_FACTOR = 0.8  # footer size, relative to style.font_size
TICK_LENGTH = 5  # px, sector tick marks
MIN_TICK_SPACING = 3  # px, a block's ticks are dropped when any two would be closer than this
MASK_PADDING = 2  # px around the patch of paper that hides a fan line behind an address label
COLLISION_TOLERANCE = 1  # px, since text widths are approximate
NOTE_RULE_INSET = 3  # px, the rule beside a multi-line note stops this short of the block edges
LEGEND_SWATCH = (16, 10)  # px, width and height

# Fills of the `color` palette, handed out in tree order and reused when they run out.
COLORS = [
    "#a8c5da", "#f2c6a0", "#b7d8b5", "#e8b4b8", "#cdbfe0",
    "#f5e0a3", "#a9d6d1", "#d9c2a7", "#c4cfe8", "#e3c9dc",
]  # fmt: skip


def _palette_colors(tree: Tree) -> dict[str, str]:
    """Give every normal region a fill from COLORS, in tree order."""
    colors: dict[str, str] = {}
    for node in tree.root.walk():
        if node.id != ROOT_ID and not node.reserved:
            colors[node.id] = COLORS[len(colors) % len(COLORS)]
    return colors


def _block_fill(block: Block, style: Style, colors: dict[str, str]) -> str:
    """A block's fill: its region's own colour, else the reserved or palette one."""
    if block.node.color:
        return block.node.color
    if block.reserved:
        return style.reserved.fill
    if style.palette == "color":
        return colors.get(block.node.id, PAPER)
    return PAPER


def _shift(layouts: list[ColumnLayout], dx: float, dy: float) -> None:
    """Move columns, with their blocks and labels, by (dx, dy)."""
    for layout in layouts:
        layout.x += dx
        layout.y += dy
        for block in layout.blocks:
            block.x += dx
            block.y += dy
        for label in layout.labels:
            label.y += dy


def _draw_address_labels(layout: ColumnLayout, masked: bool) -> list[Primitive]:
    """A column's address labels. With `masked`, each gets a patch of paper to hide the fan
    lines behind it."""
    x = layout.x - LABEL_GAP
    primitives: list[Primitive] = []
    for label in layout.labels:
        if masked:
            primitives.append(
                Rect(
                    x - label.width - MASK_PADDING,
                    label.y - ADDRESS_FONT_SIZE / 2,
                    label.width + 2 * MASK_PADDING,
                    ADDRESS_FONT_SIZE,
                    PAPER,
                )
            )
        primitives.append(
            Text(
                x,
                label.y + BASELINE_SHIFT * ADDRESS_FONT_SIZE,
                label.text,
                ADDRESS_FONT_SIZE,
                anchor="end",
                role="address",
                column=layout.id,
                region=label.region,
            )
        )
    return primitives


def _draw_column_title(layout: ColumnLayout) -> list[Primitive]:
    """A column's title lines, bold and centred above it."""
    size = layout.options.style.font_size
    spacing = line_height(size)
    last = len(layout.title_lines) - 1
    return [
        Text(
            layout.x + layout.width / 2,
            layout.y - COLUMN_TITLE_GAP - spacing * (last - index),
            line,
            size,
            weight="bold",
            role="column_title",
            column=layout.id,
        )
        for index, line in enumerate(layout.title_lines)
    ]


def _draw_band_indices(layout: ColumnLayout) -> list[Primitive]:
    """The band numbers, left of the vertical middle of each band."""
    primitives: list[Primitive] = []
    for band, group in sorted(layout.band_groups.items()):
        top = min(block.y for block in group)
        bottom = max(block.y + block.h for block in group)
        primitives.append(
            Text(
                layout.x - LABEL_GAP,
                (top + bottom) / 2 + BASELINE_SHIFT * ADDRESS_FONT_SIZE,
                str(band),
                ADDRESS_FONT_SIZE,
                anchor="end",
                role="band",
                column=layout.id,
            )
        )
    return primitives


def _draw_ticks(block: Block, style: Style) -> list[Primitive]:
    """Tick marks at the sector boundaries inside a block, unless too dense."""
    if not style.sector_ticks:
        return []
    edges = block.node.sector_edges()
    inside = [edge for edge in edges if block.start < edge <= block.end]
    span = block.end - block.start + 1
    if not inside or block.h * min(b - a for a, b in pairwise(edges)) / span < MIN_TICK_SPACING:
        return []
    right = block.x + block.w
    bottom = block.y + block.h
    lines: list[Primitive] = []
    for edge in inside:
        y = bottom - (edge - block.start) / span * block.h
        lines.append(Line(right - TICK_LENGTH, y, right, y, INK))
    return lines


def _draw_block(block: Block, layout: ColumnLayout, colors: dict[str, str]) -> list[Primitive]:
    """A block's rectangle and ticks, the text inside it and the note to its right. A
    collapsed block also gets a break mark."""
    options = layout.options
    style = options.style
    size = style.font_size
    primitives: list[Primitive] = [
        Rect(block.x, block.y, block.w, block.h, _block_fill(block, style, colors), INK)
    ]
    primitives += _draw_ticks(block, style)
    spacing = line_height(size)
    first = block.y + block.h / 2 - spacing * (len(block.lines) - 1) / 2
    for index, line in enumerate(block.lines):
        is_size = options.labels.size and not block.reserved and index == len(block.lines) - 1
        primitives.append(
            Text(
                block.x + block.w / 2,
                first + index * spacing + BASELINE_SHIFT * size,
                line,
                size,
                role="size" if is_size else "name",
                column=layout.id,
                region=block.node.id,
            )
        )
    if block.collapsed:
        primitives.append(
            Text(
                block.x + block.w - LABEL_GAP,
                block.y + block.h / 2 + BASELINE_SHIFT * size,
                "≈",
                size,
                role="break",
                column=layout.id,
                region=block.node.id,
            )
        )
    primitives += _draw_note(block, layout)
    return primitives


def _draw_note(block: Block, layout: ColumnLayout) -> list[Primitive]:
    """The note right of a block, one Text per line, centred on the block. A multi-line note
    gets a rule along the block's right side, so it is clear which block it belongs to."""
    if not block.note_lines:
        return []
    size = note_size(layout.options)
    spacing = line_height(size)
    count = len(block.note_lines)
    first = block.y + block.h / 2 - spacing * (count - 1) / 2
    primitives: list[Primitive] = []
    if count > 1:
        x = block.x + block.w + LABEL_GAP
        inset = min(NOTE_RULE_INSET, block.h / 4)
        primitives.append(Line(x, block.y + inset, x, block.y + block.h - inset, INK))
    for index, line in enumerate(block.note_lines):
        primitives.append(
            Text(
                block.x + block.w + note_indent(block),
                first + index * spacing + BASELINE_SHIFT * size,
                line,
                size,
                anchor="start",
                role="note",
                column=layout.id,
                region=block.node.id,
            )
        )
    return primitives


def _collide(first: Text, second: Text) -> bool:
    """Whether two strings overlap by more than COLLISION_TOLERANCE both ways."""
    ax, ay, aw, ah = first.box()
    bx, by, bw, bh = second.box()
    across = min(ax + aw, bx + bw) - max(ax, bx)
    down = min(ay + ah, by + bh) - max(ay, by)
    return across > COLLISION_TOLERANCE and down > COLLISION_TOLERANCE


def _text_path(text: Text, region_paths: dict[str, Path]) -> Path:
    """The YAML path that a collision on `text` is reported at."""
    if text.region in region_paths:
        return region_paths[text.region]
    return ("figure", text.role) if text.role in ("title", "footer", "legend") else ("view",)


def _text_collisions(drawing: Drawing) -> list[Diagnostic]:
    """W106 for every pair of strings drawn on top of each other."""
    region_paths = {
        block.node.id: block.node.path for layout in drawing.columns for block in layout.blocks
    }
    return [
        Diagnostic(
            "W106",
            f"text {first.text!r} overlaps {second.text!r}",
            _text_path(first, region_paths),
            "shorten the text, or move a column with `offset` or `lane`",
            first.region or first.column,
        )
        for first, second in combinations(drawing.texts(), 2)
        if _collide(first, second)
    ]


def _through_columns(
    region: str, path: Path, target: ColumnLayout, between: list[ColumnLayout]
) -> Diagnostic:
    """W107 for fan lines that run through the columns between a zoom's source and target."""
    lanes = sorted({column.lane for column in between})
    rightward = target.lane > lanes[0]
    nearest = lanes[0] if rightward else lanes[-1]  # the lane between closest to the source
    columns = "column" if len(between) == 1 else "columns"
    return Diagnostic(
        "W107",
        f"the fan lines of {region} run through {columns} "
        + ", ".join(repr(column.id) for column in between),
        path,
        f"put {target.id!r} in lane {nearest}, or move it with `offset` or `anchor`",
        region,
    )


def layout_figure(
    config: Config, tree: Tree, plan: ViewPlan
) -> tuple[Drawing, list[Diagnostic]]:
    """Lay out every column and emit the primitives in paint order: canvas, frame, title,
    fan lines, labels, blocks, legend and footer."""
    resolver = OptionResolver(config)
    style = resolver.options().style
    out: list[Diagnostic] = []
    by_id = {
        column.id: build_column(column, resolver, tree.address_bits, out)
        for column in plan.columns
    }
    layouts = list(by_id.values())

    assign_lanes(plan, by_id)
    place_horizontally(layouts)
    stretch_sources(plan, by_id)
    height = content_height(layouts)
    header = header_height(layouts)
    for layout in layouts:
        stretch_bands(layout, height - header)
    place_vertically(plan, by_id, height, header)

    # Fan lines cross the left gutter of a detail column, where its labels are.
    fan_targets = {link.target.id for link in plan.links}
    for layout in layouts:
        place_labels(layout, layout.id in fan_targets)

    tops, bottoms = [], []
    for layout in layouts:
        top = layout.y - layout.title_height
        tops.append(min([top] + [label.y - ADDRESS_FONT_SIZE for label in layout.labels]))
        bottoms.append(
            max(
                [layout.y + layout.height]
                + [label.y + ADDRESS_FONT_SIZE for label in layout.labels]
            )
        )
    left = min(layout.x - layout.left_gutter for layout in layouts)
    right = max(layout.x + layout.width + layout.right_gutter for layout in layouts)
    content_w = right - left
    content_h = max(bottoms) - min(tops)

    title = config.figure.title
    title_size = style.font_size * TITLE_FACTOR
    title_h = TITLE_HEIGHT if title else 0
    title_w = text_width(title, title_size) if title else 0
    has_reserved = any(block.reserved for layout in layouts for block in layout.blocks)
    legend = config.figure.legend and has_reserved
    footer_h = FOOTER_ROW if legend or config.figure.footer else 0

    frame_w = max(content_w + 2 * FRAME_PADDING, title_w)
    frame_h = content_h + 2 * FRAME_PADDING + footer_h
    frame_x, frame_y = MARGIN, MARGIN + title_h
    width = frame_w + 2 * MARGIN
    canvas_height = frame_y + frame_h + MARGIN
    centre = (frame_w - content_w) / 2
    _shift(layouts, frame_x + centre - left, frame_y + FRAME_PADDING - min(tops))

    drawing = Drawing(width, canvas_height, columns=layouts)
    primitives = drawing.primitives
    primitives.append(Rect(0, 0, width, canvas_height, PAPER))
    if style.frame:
        primitives.append(Rect(frame_x, frame_y, frame_w, frame_h, "none", INK))
    if title:
        primitives.append(
            Text(width / 2, MARGIN + title_size, title, title_size, weight="bold", role="title")
        )

    for link in plan.links:
        source = by_id[link.source.id]
        target = by_id[link.target.id]
        pieces = [block for block in source.blocks if block.node is link.region]
        between = lanes_between(layouts, source.lane, target.lane)
        boxes = [column_box(other) for other in between]
        lines, clear = connector_lines(pieces, target, boxes, style.connectors == "dashed")
        primitives += lines
        if not clear:
            out.append(_through_columns(link.region.id, link.region.path, target, between))
        drawing.links.append(
            {
                "region": link.region.id,
                "from": link.source.id,
                "to": link.target.id,
                "lines": [[(round(x, 2), round(y, 2)) for x, y in line.points] for line in lines],
            }
        )

    for layout in layouts:
        primitives += _draw_address_labels(layout, layout.id in fan_targets)
        primitives += _draw_band_indices(layout)
        primitives += _draw_column_title(layout)
    colors = _palette_colors(tree)
    for layout in layouts:
        for block in layout.blocks:
            primitives += _draw_block(block, layout, colors)

    row_y = frame_y + frame_h - FOOTER_ROW / 2
    if legend:
        swatch_w, swatch_h = LEGEND_SWATCH
        x = frame_x + FRAME_PADDING
        primitives.append(
            Rect(x, row_y - swatch_h / 2, swatch_w, swatch_h, style.reserved.fill, INK)
        )
        primitives.append(
            Text(
                x + swatch_w + 6,
                row_y + BASELINE_SHIFT * ADDRESS_FONT_SIZE,
                style.reserved.label,
                ADDRESS_FONT_SIZE,
                anchor="start",
                role="legend",
            )
        )
    if config.figure.footer:
        footer_size = style.font_size * FOOTER_FACTOR
        primitives.append(
            Text(
                frame_x + frame_w - FRAME_PADDING / 2,
                row_y + BASELINE_SHIFT * footer_size,
                config.figure.footer,
                footer_size,
                anchor="end",
                role="footer",
            )
        )
    return drawing, out + _text_collisions(drawing)
