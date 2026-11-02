"""Address and size literals: parsing "16K" / "0x0800 0000", formatting for display."""

from __future__ import annotations

import re

# Size suffixes, all binary.
_FACTORS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
# Also published in the JSON Schema, so limited to syntax that Python and editors both accept.
ADDRESS_PATTERN = r"^\s*(0[xX][ _]*[0-9a-fA-F][0-9a-fA-F_ ]*|[ _]*[0-9][0-9_ ]*)\s*$"
SIZE_PATTERN = r"^\s*(0[xX][0_]*[1-9a-fA-F][0-9a-fA-F_]*|[0_]*[1-9][0-9_]*)\s*([kKmMgGtT]?)[bB]?\s*$"
_ADDRESS_RE = re.compile(ADDRESS_PATTERN)
_SIZE_RE = re.compile(SIZE_PATTERN)
_GROUP = 4  # hex digits between separators


def _parse_int(text: str) -> int:
    """Parse digits as hex with an 0x prefix, otherwise as decimal."""
    return int(text, 16) if text.lower().startswith("0x") else int(text, 10)


def parse_address(value: object) -> int:
    """Parse an address: an integer, or a string where spaces and underscores are ignored."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"expected an address, got {value!r}")
    if isinstance(value, str):
        match = _ADDRESS_RE.fullmatch(value)
        if not match:
            raise ValueError(f"bad address literal {value!r}")
        value = _parse_int(match.group(1).replace("_", "").replace(" ", ""))
    if value < 0:
        raise ValueError(f"address must not be negative, got {value}")
    return value


def parse_size(value: object) -> int:
    """Parse a size: an integer or a string with a binary unit, such as 16K or 512M."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"expected a size, got {value!r}")
    if isinstance(value, str):
        match = _SIZE_RE.fullmatch(value)
        if not match:
            raise ValueError(
                f"bad size literal {value!r}, expected a positive size like 512, 16K, 1M, 4G"
            )
        value = _parse_int(match.group(1).replace("_", "")) * _FACTORS[match.group(2).upper()]
    if value <= 0:
        raise ValueError(f"size must be positive, got {value}")
    return value


def format_address(value: int, digits: int, separator: str = " ") -> str:
    """Format an address with zero padding and groups of four digits counted from the right."""
    text = f"{value:0{digits}X}"
    head = len(text) % _GROUP
    chunks = [text[:head]] if head else []
    chunks += [text[i : i + _GROUP] for i in range(head, len(text), _GROUP)]
    return "0x" + separator.join(chunks)


def diagnostic_address(value: int, address_bits: int = 32) -> str:
    """The fixed address style used in diagnostics, such as 0x0800_3800."""
    return format_address(value, address_bits // 4, separator="_")


def format_size(size: int) -> str:
    """Format a size in the largest unit that represents it exactly with one decimal."""
    for factor, suffix in ((1024**4, "TB"), (1024**3, "GB"), (1024**2, "MB"), (1024, "KB")):
        value = size / factor
        if value >= 1 and round(value, 1) == value:
            return f"{value:.1f}".removesuffix(".0") + f" {suffix}"
    return f"{size} B"
