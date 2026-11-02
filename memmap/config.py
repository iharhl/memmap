"""Load YAML into plain dicts and lists, keeping a table of source positions."""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from pathlib import Path as FilePath
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import MarkedYAMLError, YAMLError
from ruamel.yaml.scalarbool import ScalarBoolean

from .diagnostics import Diagnostic, Path, Positions


class ConfigError(Exception):
    """A usage or IO problem (exit code 2), as opposed to a problem inside the config."""


@dataclass
class Loaded:
    """A config as read from disk, before validation. After a YAML problem `raw` is empty."""

    raw: dict[str, Any]
    positions: Positions
    diagnostics: list[Diagnostic] = field(default_factory=list)


def _plain(node: Any, path: Path, positions: Positions) -> Any:
    """Copy a ruamel tree to plain Python types, recording each key's and item's position."""
    if isinstance(node, CommentedMap):
        mapping: dict[str, Any] = {}
        for key, value in node.items():
            position = node.lc.data.get(key) if node.lc.data else None
            if position:
                positions.table[path + (key,)] = (position[0] + 1, position[1] + 1)
            mapping[str(key)] = _plain(value, path + (key,), positions)
        return mapping
    if isinstance(node, CommentedSeq):
        items: list[Any] = []
        for index, value in enumerate(node):
            position = node.lc.data.get(index) if node.lc.data else None
            if position:
                positions.table[path + (index,)] = (position[0] + 1, position[1] + 1)
            items.append(_plain(value, path + (index,), positions))
        return items
    if isinstance(node, (bool, ScalarBoolean)):  # an anchored bool is an int subclass in ruamel
        return bool(node)
    if isinstance(node, int):
        return int(node)
    if isinstance(node, float):
        return float(node)
    if isinstance(node, str):
        return str(node)
    return node


def load_yaml(file: FilePath) -> Loaded:
    """Read a config file. YAML problems become diagnostics; IO problems raise ConfigError."""
    positions = Positions()
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read {file}: {exc}") from exc
    try:
        data = YAML(typ="rt").load(text)
    except YAMLError as exc:
        diagnostic = Diagnostic("E000", f"invalid YAML: {_yaml_problem(exc)}")
        mark = getattr(exc, "problem_mark", None)
        if isinstance(exc, MarkedYAMLError) and mark is not None:
            diagnostic.line, diagnostic.column = mark.line + 1, mark.column + 1
        return Loaded({}, positions, [diagnostic])
    if not isinstance(data, CommentedMap):
        problem = Diagnostic("E004", "config must be a mapping with keys version, memory, ...")
        return Loaded({}, positions, [problem])
    return Loaded(_plain(data, (), positions), positions)


def _yaml_problem(exc: YAMLError) -> str:
    """The YAML error's message, on one line."""
    return " ".join(str(getattr(exc, "problem", None) or exc).split())


# One step of a --set path: a key, or a list index in brackets.
_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _split_key_path(text: str) -> list[Any]:
    """Split a --set path such as view.columns[0].width into keys and indices."""
    tokens: list[Any] = []
    position = 0
    while position < len(text):
        if text[position] == "." and tokens:
            position += 1
        match = _TOKEN_RE.match(text, position)
        if not match:
            raise ConfigError(f"bad --set path {text!r}")
        tokens.append(match.group(1) if match.group(1) is not None else int(match.group(2)))
        position = match.end()
    if not tokens or not isinstance(tokens[0], str):
        raise ConfigError(f"bad --set path {text!r}")
    return tokens


def _parse_value(text: str) -> Any:
    """A --set value as YAML, or the text itself when it does not parse."""
    try:
        return YAML(typ="safe").load(text)
    except YAMLError:
        return text


def apply_set(raw: dict[str, Any], assignment: str) -> None:
    """Apply one key.path=value override, e.g. view.columns[0].width=150."""
    key_path, separator, value = assignment.partition("=")
    if not separator:
        raise ConfigError(f"bad --set value {assignment!r}, expected key.path=value")
    tokens = _split_key_path(key_path.strip())
    node: Any = raw
    for token, following in itertools.pairwise(tokens):
        if isinstance(token, int):
            if not isinstance(node, list) or token >= len(node):
                raise ConfigError(f"--set {key_path}: index {token} does not exist")
            node = node[token]
        else:
            if not isinstance(node, dict):
                raise ConfigError(f"--set {key_path}: {token!r} is not inside a mapping")
            node = node.setdefault(token, [] if isinstance(following, int) else {})
    last = tokens[-1]
    if isinstance(last, int):
        if not isinstance(node, list) or last >= len(node):
            raise ConfigError(f"--set {key_path}: index {last} does not exist")
    elif not isinstance(node, dict):
        raise ConfigError(f"--set {key_path}: {last!r} is not inside a mapping")
    node[last] = _parse_value(value)
