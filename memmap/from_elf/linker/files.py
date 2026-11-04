"""Read a linker map, telling the formats apart by their content.

GNU ld and armlink list each region with its size. IAR lists the ranges it placed content in.
An lld map has no region table at all, so it is read as a map without regions. A linker script
is turned away: memmap reads what the linker worked out, not what it was told.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..regions import ImportProblem
from . import armlink_map, gnu_map, iar_map
from .model import LLD_MAP, LinkerFile

_MAPS = (gnu_map, armlink_map, iar_map)
# lld writes a column header first: VMA LMA Size Align Out In Symbol, or Address without LMA.
_LLD_MAP = re.compile(r"^\s*(?:VMA\s+LMA|Address)\s+Size\s+Align\s+Out\s+In\s+Symbol\b")
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_SCRIPT = re.compile(r"\b(?:MEMORY|SECTIONS)\s*\{")
_MEMORY = re.compile(r"\bMEMORY\s*\{")


def _text(path: Path) -> str:
    """The file's text with Unix line ends and without a byte order mark. Maps from Windows
    tools are not always UTF-8."""
    try:
        text = path.read_bytes().decode("utf-8-sig", "replace")
    except OSError as exc:
        raise ImportProblem(f"cannot read {path}: {exc.strerror or exc}") from None
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_map(path: Path) -> LinkerFile:
    """The regions of a GNU ld, armlink or IAR map file, or none for an lld map."""
    text = _text(path)
    for reader in _MAPS:
        if reader.matches(text):
            return LinkerFile(path.name, reader.NAME, reader.read(text))
    if _LLD_MAP.match(text):
        return LinkerFile(path.name, LLD_MAP)
    script = _COMMENT.sub(" ", text)
    if _SCRIPT.search(script):
        hint = "pass the map file the linker writes, or give each region with --region ID=START:SIZE"
        if _MEMORY.search(script):
            hint += " from the script's MEMORY block"
        raise ImportProblem(f"{path.name} is a linker script, which memmap does not read", hint)
    raise ImportProblem(
        f"{path.name} is not a map file memmap can read",
        hint="pass the map file of a GNU ld, armlink or IAR link",
    )
