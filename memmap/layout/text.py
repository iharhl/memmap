"""Text measurement from a built-in width table, and word wrapping."""

from __future__ import annotations

# Helvetica/Arial advance widths in 1/1000 em for ASCII 32-126.
_WIDTHS = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]  # fmt: skip
_FALLBACK = 600  # for characters outside the table
_LINE_FACTOR = 1.2  # line spacing, in em

ADDRESS_FONT_SIZE = 10  # px, also used for band indices and the legend
BASELINE_SHIFT = 0.35  # em, from a line's vertical centre down to its baseline
BOLD_WIDTH_FACTOR = 1.08  # the table is for regular weight, bold runs this much wider


def text_width(text: str, size: float) -> float:
    """The width of `text` at font size `size`, from the width table."""
    units = 0
    for char in text:
        code = ord(char) - 32
        units += _WIDTHS[code] if 0 <= code < len(_WIDTHS) else _FALLBACK
    return units * size / 1000


def line_height(size: float) -> float:
    """The distance between baselines at font size `size`."""
    return size * _LINE_FACTOR


def _break_word(word: str, width: float, size: float) -> list[str]:
    """Split a word into pieces that each fit `width`, or one character if not."""
    pieces: list[str] = []
    current = ""
    for char in word:
        if current and text_width(current + char, size) > width:
            pieces.append(current)
            current = char
        else:
            current += char
    pieces.append(current)
    return pieces


def wrap(text: str, width: float, size: float) -> list[str]:
    """Greedy word wrap. `\\n` forces a break; a word wider than a line is broken."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
            for piece in _break_word(word, width, size):
                candidate = f"{current} {piece}" if current else piece
                if text_width(candidate, size) <= width:
                    current = candidate
                else:
                    lines.append(current)
                    current = piece
        lines.append(current)
    return lines
