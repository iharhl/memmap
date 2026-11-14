"""Resolve `view`: normalise columns and expansions against the region tree (E020-E024)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .diagnostics import Diagnostic, Path
from .resolve import ROOT_ID, Node, Tree
from .schema import Column, Config, Expansion

MAX_BANDS = 256  # per column (E024), so a small band size cannot ask for millions of blocks


@dataclass
class ColumnView:
    """One column of the view, resolved: `nodes` are the regions it stacks, and `start` and `end`
    the address range it shows."""

    spec: Column
    root: Node
    start: int
    end: int
    nodes: list[Node]
    path: Path
    title: str | None = None

    @property
    def id(self) -> str:
        """The column's id."""
        return self.spec.id

    def draws(self, node: Node) -> bool:
        """Whether the node gets a block, which also needs it to reach into the column's range."""
        return node in self.nodes and node.start <= self.end and node.end >= self.start


@dataclass
class Link:
    """An expansion: fan lines from `region`'s block in `source` to the detail column `target`."""

    region: Node
    source: ColumnView
    target: ColumnView
    path: Path


@dataclass
class ViewPlan:
    """Every column to draw, including those expansions created, and the links between them."""

    columns: list[ColumnView] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)

    def column(self, column_id: str) -> ColumnView | None:
        """The column with this id, if any."""
        return next((c for c in self.columns if c.id == column_id), None)


def flatten(root: Node, depth: int) -> list[Node]:
    """The nodes a column stacks: children of root, descending `depth` levels. The implicit
    root stacks only the regions of the default address space."""
    children = [child for child in root.children if root.id != ROOT_ID or child.space is None]
    if depth == 0 or not children:
        return [root]
    nodes: list[Node] = []
    for child in children:
        nodes += flatten(child, depth - 1)
    return nodes


def _add_column(
    spec: Column, path: Path, tree: Tree, plan: ViewPlan, out: list[Diagnostic]
) -> ColumnView | None:
    """Resolve one column spec into the plan, or report why it cannot be drawn."""
    if plan.column(spec.id):
        out.append(
            Diagnostic(
                "E023", f"duplicate column id '{spec.id}'", path, "column ids are unique", spec.id
            )
        )
        return None
    root = tree.index.get(spec.root)
    if root is None:
        out.append(
            Diagnostic(
                "E020",
                f"column root '{spec.root}' does not exist",
                path + ("root",),
                "use a region id or `/`",
                spec.id,
            )
        )
        return None
    start, end = spec.range if spec.range else (root.start, root.end)
    if start > end or start < root.start or end > root.end:
        out.append(
            Diagnostic(
                "E010",
                f"column range is not inside {root.id}",
                path + ("range",),
                f"use a range within {tree.address(root.start)} - {tree.address(root.end)}",
                spec.id,
            )
        )
        return None
    if spec.bands and (end - start + 1) / spec.bands > MAX_BANDS:
        out.append(
            Diagnostic(
                "E024",
                f"bands would split the column into more than {MAX_BANDS} parts",
                path + ("bands",),
                "use a larger band size",
                spec.id,
            )
        )
        return None
    if root.id == ROOT_ID and root.children and all(child.space is not None for child in root.children):
        out.append(
            Diagnostic(
                "E020",
                "column root '/' draws only regions without a `space`, and every region has one",
                path + ("root",),
                "use the id of a region with a `space` as the root",
                spec.id,
            )
        )
        return None
    column = ColumnView(spec, root, start, end, flatten(root, spec.depth), path, spec.title)
    plan.columns.append(column)
    return column


def _default_columns(tree: Tree) -> list[Column]:
    """The columns of a view that lists none: the whole default address space, then a column
    for each top-level region of a named space, in file order, which that column does not
    draw."""
    columns = []
    if any(child.space is None for child in tree.root.children) or not tree.root.children:
        columns.append(Column(id="main", root=ROOT_ID))
    named = [child for child in tree.root.children if child.space is not None and not child.generated]
    for child in sorted(named, key=lambda node: node.path):  # in file order
        columns.append(Column(id=f"{child.id}_space", root=child.id))
    return columns


def _find_source(plan: ViewPlan, region: Node) -> ColumnView | None:
    """The first column that draws `region`."""
    return next((c for c in plan.columns if c.draws(region)), None)


def resolve_view(config: Config, tree: Tree) -> tuple[ViewPlan, list[Diagnostic]]:
    """Resolve the columns, then the expansions, then check override targets."""
    plan = ViewPlan()
    out: list[Diagnostic] = []
    view = config.view

    specs = view.columns or _default_columns(tree)
    for index, spec in enumerate(specs):
        _add_column(spec, ("view", "columns", index), tree, plan, out)

    for index, entry in enumerate(view.expand):
        path: Path = ("view", "expand", index)
        region = tree.index.get(entry.region)
        if region is None or region.id == ROOT_ID:
            out.append(
                Diagnostic(
                    "E020",
                    f"expansion region '{entry.region}' does not exist",
                    path,
                    "use the id of a region from `memory`",
                )
            )
            continue
        source = plan.column(entry.from_) if entry.from_ else _find_source(plan, region)
        if source is None:
            if entry.from_:
                message = f"expansion `from` column '{entry.from_}' does not exist"
            else:
                message = f"region '{entry.region}' is not drawn in any column"
            hint = "name an existing column, or list the parent region's expansion first"
            out.append(Diagnostic("E020", message, path, hint))
            continue
        if not source.draws(region):
            out.append(
                Diagnostic(
                    "E022",
                    f"region '{entry.region}' is not drawn in column '{source.id}'",
                    path,
                    "expand a block of that column, or change the column's `depth` or `range`",
                )
            )
            continue
        target = _target_column(entry, region, tree, plan, path, out)
        if target is source:
            out.append(
                Diagnostic(
                    "E025",
                    f"column '{source.id}' would expand into itself",
                    path,
                    "give the region children to zoom into, or remove this expansion",
                )
            )
        elif target is not None:
            target.title = entry.title or target.title
            plan.links.append(Link(region, source, target, path))

    for index, override in enumerate(view.overrides):
        path = ("view", "overrides", index)
        if override.region not in tree.index:
            out.append(
                Diagnostic(
                    "E020",
                    f"override region '{override.region}' does not exist",
                    path,
                    "use the id of a region from `memory`",
                )
            )
        if override.column and not plan.column(override.column):
            out.append(
                Diagnostic("E020", f"override column '{override.column}' does not exist", path)
            )
    return plan, out


def _target_column(
    entry: Expansion, region: Node, tree: Tree, plan: ViewPlan, path: Path, out: list[Diagnostic]
) -> ColumnView | None:
    """The detail column of an expansion: the named one, else an existing column with that
    root, else a new one."""
    if entry.to is None:
        existing = next((c for c in plan.columns if c.root is region), None)
        if existing:
            return existing
        spec = Column(id=f"{region.id}_detail", root=region.id)
        return _add_column(spec, path, tree, plan, out)
    target = plan.column(entry.to)
    if target is None:
        out.append(
            Diagnostic(
                "E020",
                f"expansion target column '{entry.to}' does not exist",
                path + ("to",),
                "add the column to view.columns, or omit `to` to create it",
            )
        )
        return None
    if target.root is not region:
        out.append(
            Diagnostic(
                "E021",
                f"column '{target.id}' has root '{target.root.id}', expected '{region.id}'",
                path + ("to",),
                f"set `root: {region.id}` on the column",
            )
        )
        return None
    return target
