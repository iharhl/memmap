"""Primitives to SVG. Output is deterministic: sorted attributes and fixed float precision."""

from __future__ import annotations

from typing import Any
from xml.sax.saxutils import escape, quoteattr

from ..layout.primitives import (
    FONT_FAMILY,
    Drawing,
    Line,
    Polyline,
    Primitive,
    Rect,
    Text,
)


def _number(value: float) -> str:
    """Format with at most two decimals and no trailing zeros."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _attributes(values: dict[str, Any]) -> str:
    """XML attributes, sorted by name."""
    parts = []
    for key in sorted(values):
        value = values[key]
        if isinstance(value, (int, float)):
            value = _number(value)
        parts.append(f"{key}={quoteattr(str(value))}")
    return " ".join(parts)


def _element(primitive: Primitive) -> str:
    """One primitive as an SVG element."""
    if isinstance(primitive, Rect):
        values = {
            "x": primitive.x,
            "y": primitive.y,
            "width": primitive.w,
            "height": primitive.h,
            "fill": primitive.fill,
        }
        if primitive.stroke:
            values["stroke"] = primitive.stroke
            values["stroke-width"] = primitive.stroke_width
        return f"<rect {_attributes(values)}/>"
    if isinstance(primitive, Line):
        values = {
            "x1": primitive.x1,
            "y1": primitive.y1,
            "x2": primitive.x2,
            "y2": primitive.y2,
            "stroke": primitive.stroke,
            "stroke-width": primitive.width,
        }
        return f"<line {_attributes(values)}/>"
    if isinstance(primitive, Polyline):
        points = " ".join(f"{_number(x)},{_number(y)}" for x, y in primitive.points)
        values = {
            "points": points,
            "fill": "none",
            "stroke": primitive.stroke,
            "stroke-width": primitive.width,
        }
        if primitive.dash is not None:
            values["stroke-dasharray"] = primitive.dash
        return f"<polyline {_attributes(values)}/>"
    if isinstance(primitive, Text):
        values = {
            "x": primitive.x,
            "y": primitive.y,
            "fill": primitive.fill,
            "font-family": FONT_FAMILY,
            "font-size": primitive.size,
            "text-anchor": primitive.anchor,
        }
        if primitive.weight != "normal":
            values["font-weight"] = primitive.weight
        return f"<text {_attributes(values)}>{escape(primitive.text)}</text>"
    raise TypeError(f"unknown primitive {primitive!r}")


def render_svg(drawing: Drawing) -> str:
    """The whole drawing as an SVG document."""
    root = {
        "xmlns": "http://www.w3.org/2000/svg",
        "width": drawing.width,
        "height": drawing.height,
        "viewBox": f"0 0 {_number(drawing.width)} {_number(drawing.height)}",
    }
    lines = [f"<svg {_attributes(root)}>"]
    lines += [_element(primitive) for primitive in drawing.primitives]
    lines.append("</svg>")
    return "\n".join(lines) + "\n"
