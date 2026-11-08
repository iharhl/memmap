"""Drawing primitives, in pixels. Backends consume these and know nothing about memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .text import BASELINE_SHIFT, BOLD_WIDTH_FACTOR, text_width

# Fixed on purpose, these have no config keys.
FONT_FAMILY = "Arial, Helvetica, sans-serif"
INK = "#000000"  # outlines, lines and text
PAPER = "#ffffff"  # the canvas, and blocks without a fill of their own


@dataclass
class Rect:
    """A rectangle with its top left corner at (x, y). `fill` may be "none"."""

    x: float
    y: float
    w: float
    h: float
    fill: str
    stroke: str | None = None
    stroke_width: float = 1


@dataclass
class Line:
    """A straight line between two points."""

    x1: float
    y1: float
    x2: float
    y2: float
    stroke: str
    width: float = 1


@dataclass
class Polyline:
    """A line through a list of points, as used for fan lines."""

    points: list[tuple[float, float]]
    stroke: str
    width: float = 1


@dataclass
class Text:
    """One string on the canvas. Backends ignore `role`, `column` and `region`, which are there
    for `describe` and the collision check."""

    x: float
    y: float  # baseline
    text: str
    size: float
    anchor: str = "middle"  # start | middle | end
    fill: str = INK
    weight: str = "normal"
    # title | column_title | footer | legend | band | address | name | size | note | break
    role: str = ""
    column: str | None = None
    region: str | None = None

    def box(self) -> tuple[float, float, float, float]:
        """Approximate bounds as x, y, w, h, from the width table and the baseline."""
        width = text_width(self.text, self.size)
        if self.weight == "bold":
            width *= BOLD_WIDTH_FACTOR
        left = self.x - {"start": 0, "middle": width / 2, "end": width}[self.anchor]
        return left, self.y - (BASELINE_SHIFT + 0.5) * self.size, width, self.size


# Everything a backend must be able to draw.
Primitive = Rect | Line | Polyline | Text


@dataclass
class Drawing:
    """A finished figure: `primitives` in paint order, plus the column layouts and links that
    `describe` reports."""

    width: float
    height: float
    primitives: list[Primitive] = field(default_factory=list)
    columns: list[Any] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)

    def texts(self) -> list[Text]:
        """Every string, in paint order."""
        return [p for p in self.primitives if isinstance(p, Text)]
