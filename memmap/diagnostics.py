"""Diagnostics: stable codes, YAML paths, source positions and hints."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Path = tuple[str | int, ...]  # str keys and int indices, e.g. ("memory", "regions", 0)


def format_path(path: Path) -> str:
    """Render a path tuple as memory.regions[0].children[2]."""
    text = ""
    for part in path:
        if isinstance(part, int):
            text += f"[{part}]"
        else:
            text += f".{part}" if text else str(part)
    return text


@dataclass
class Diagnostic:
    """One finding about a config. A code starting with E is an error, the rest are warnings."""

    code: str
    message: str
    path: Path = ()
    hint: str | None = None
    subject: str | None = None  # region or column id, printed after the path
    details: list[str] = field(default_factory=list)
    line: int | None = None
    column: int | None = None

    @property
    def level(self) -> str:
        """`error` or `warning`, from the code."""
        return "error" if self.code.startswith("E") else "warning"

    def to_json(self) -> dict[str, Any]:
        """The object printed by --json."""
        return {
            "code": self.code,
            "level": self.level,
            "path": format_path(self.path),
            "line": self.line,
            "column": self.column,
            "message": self.message,
            "hint": self.hint,
            "subject": self.subject,
            "details": self.details,
        }

    def to_text(self, filename: str) -> str:
        """The text form: location and code, then message, details and hint."""
        where = filename
        if self.line is not None:
            where += f":{self.line}:{self.column}"
        head = f"{where}  {self.code}  {format_path(self.path)}"
        if self.subject:
            head += f" ({self.subject})"
        lines = [head.rstrip(), f"  {self.message}"]
        lines += [f"  {detail}" for detail in self.details]
        if self.hint:
            lines.append(f"  hint: {self.hint}")
        return "\n".join(lines)


class Positions:
    """Source positions by path; a path without an entry falls back to its nearest parent."""

    def __init__(self) -> None:
        """Start with no positions."""
        self.table: dict[Path, tuple[int, int]] = {}

    def locate(self, path: Path) -> tuple[int | None, int | None]:
        """Line and column of `path` or its nearest recorded parent, else None for both."""
        path = tuple(path)
        while path:
            if path in self.table:
                return self.table[path]
            path = path[:-1]
        return None, None


def has_errors(diagnostics: list[Diagnostic]) -> bool:
    """Whether any diagnostic is an error, as opposed to a warning."""
    return any(d.level == "error" for d in diagnostics)
