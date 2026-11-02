"""Wire the stages together: load, validate, resolve, check, layout."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .config import apply_set, load_yaml
from .diagnostics import Diagnostic, Positions, has_errors
from .layout import layout_figure
from .layout.primitives import Drawing
from .options import OptionResolver
from .resolve import Tree, resolve_tree
from .schema import Config, parse_config
from .validate import check_tree
from .view import ViewPlan, resolve_view


@dataclass
class Result:
    """What a pipeline run produced. A stage that did not run leaves its field as None."""

    diagnostics: list[Diagnostic] = field(default_factory=list)
    config: Config | None = None
    tree: Tree | None = None
    plan: ViewPlan | None = None
    drawing: Drawing | None = None
    resolver: OptionResolver | None = None

    @property
    def ok(self) -> bool:
        """Whether no errors were reported so far."""
        return not has_errors(self.diagnostics)


def _finish(result: Result, positions: Positions, verbose: bool) -> Result:
    """Drop W101 unless verbose, add source positions, and sort by line."""
    kept = [d for d in result.diagnostics if verbose or d.code != "W101"]
    for diagnostic in kept:
        if diagnostic.line is None:
            diagnostic.line, diagnostic.column = positions.locate(diagnostic.path)
    kept.sort(key=lambda d: (d.line is None, d.line or 0, d.code))
    result.diagnostics = kept
    return result


def run(file: Path, sets: Iterable[str] = (), verbose: bool = False) -> Result:
    """Run every stage that the diagnostics so far allow. ConfigError means usage or IO."""
    loaded = load_yaml(file)
    result = Result(diagnostics=loaded.diagnostics)
    positions = loaded.positions
    if not result.ok:
        return _finish(result, positions, verbose)

    raw = loaded.raw
    for assignment in sets:
        apply_set(raw, assignment)

    config, diagnostics = parse_config(raw)
    result.diagnostics += diagnostics
    if config is None:
        return _finish(result, positions, verbose)
    result.config = config

    tree, diagnostics = resolve_tree(config)
    result.tree = tree
    result.diagnostics += diagnostics
    placed = result.ok  # a region in error is missing from the tree
    result.diagnostics += check_tree(tree, config.memory.check_alignment)
    if not placed:
        return _finish(result, positions, verbose)

    plan, diagnostics = resolve_view(config, tree)
    result.plan = plan
    result.diagnostics += diagnostics

    if result.ok:
        result.resolver = OptionResolver(config)
        result.drawing, diagnostics = layout_figure(config, tree, plan)
        result.diagnostics += diagnostics
    return _finish(result, positions, verbose)
