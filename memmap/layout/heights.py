"""Block heights per scale mode."""

from __future__ import annotations

import math

from ..schema import Scale
from .text import line_height

TEXT_PADDING = 6  # px added to the height of a block's text
WEIGHT_FACTOR = 3  # px per log2(size) in weighted mode
LINEAR_HEIGHT = 480  # target column height in linear mode


def _clamp(value: float, scale: Scale) -> float:
    """Limit a height to the scale's min_height and max_height."""
    value = max(value, scale.min_height)
    if scale.max_height is not None:
        value = min(value, scale.max_height)
    return value


def explicit_height(value: float | str, scale: Scale) -> float:
    """A per-region height: pixels, or a multiple of min_height such as "2x"."""
    if isinstance(value, str):
        return float(value[:-1]) * scale.min_height
    return float(value)


def scaled_height(
    size: int, scale: Scale, total_bytes: int, override: float | str | None = None
) -> float:
    """The height a block gets from its scale mode, before text is taken into account."""
    if override is not None:
        return explicit_height(override, scale)
    if scale.mode == "weighted":
        return _clamp(max(math.log2(size), 1) * WEIGHT_FACTOR, scale)
    if scale.mode == "linear":
        return _clamp(size / total_bytes * LINEAR_HEIGHT, scale)
    return scale.min_height


def text_height(line_count: int, font_size: float) -> float:
    """The height of `line_count` lines of text with padding, 0 for no lines."""
    return line_count * line_height(font_size) + TEXT_PADDING if line_count else 0
