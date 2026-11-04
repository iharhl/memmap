"""Sizes and addresses as an import writes them: in names, titles and YAML literals."""

from __future__ import annotations

import math

_UNITS = ((1024**3, "GB"), (1024**2, "MB"), (1024, "KB"))


def approx_size(size: int) -> str:
    """A size rounded to one decimal, for usage figures: 1.6 KB, 59.5 KB, 512 B."""
    for factor, suffix in _UNITS:
        if size >= factor:
            return f"{size / factor:.1f}".removesuffix(".0") + f" {suffix}"
    return f"{size} B"


def floor_size(size: int) -> str:
    """A free size: exact when some unit shows it with one decimal (1920 KB, 4.5 KB), else
    rounded down to three significant digits with ≈, so it never overstates (≈1.99 GB)."""
    for factor, suffix in _UNITS:
        value = size / factor
        if value >= 1 and round(value, 1) == value:
            return f"{value:.1f}".removesuffix(".0") + f" {suffix}"
    for factor, suffix in _UNITS:
        if size >= factor:
            value = size / factor
            decimals = 0 if value >= 100 else 1 if value >= 10 else 2
            floored = math.floor(value * 10**decimals) / 10**decimals
            text = f"{floored:.{decimals}f}"
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            exact = floored * factor == size
            return f"{'' if exact else '≈'}{text} {suffix}"
    return f"{size} B"


def size_literal(size: int) -> str:
    """A size in the shortest exact form: 64K, 1M, or plain bytes."""
    for factor, suffix in ((1024**3, "G"), (1024**2, "M"), (1024, "K")):
        if size and size % factor == 0:
            return f"{size // factor}{suffix}"
    return str(size)


def address_literal(value: int, bits: int) -> str:
    """An address as zero-padded hex, such as 0x08000000."""
    return f"0x{value:0{bits // 4}X}"
