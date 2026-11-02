"""SVG to PNG or PDF through cairosvg, the optional `memmap[raster]` extra."""

from __future__ import annotations

from typing import cast


class RasterUnavailable(Exception):
    """cairosvg or the cairo library is missing, so `render` exits with code 3."""


PNG_SCALE = 2  # output pixels per drawing pixel


def convert(svg: str, output_format: str) -> bytes:
    """Convert an SVG string to PNG or PDF bytes. RasterUnavailable means cairo support is
    missing."""
    try:
        import cairosvg
    except ImportError as exc:
        raise RasterUnavailable(
            f"{output_format} output needs cairosvg: install memmap[raster]"
        ) from exc
    except OSError as exc:
        raise RasterUnavailable(
            f"{output_format} output needs the cairo library, which was not found. "
            "On macOS: brew install cairo, then set "
            "DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib"
        ) from exc
    data = svg.encode("utf-8")
    # cairosvg returns None only when given write_to.
    if output_format == "png":
        return cast(bytes, cairosvg.svg2png(bytestring=data, scale=PNG_SCALE))
    return cast(bytes, cairosvg.svg2pdf(bytestring=data))
