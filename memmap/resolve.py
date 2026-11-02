"""Resolve the region tree to absolute inclusive [start, end] ranges, and fill gaps."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from .diagnostics import Diagnostic, Path, format_path
from .schema import Config, Region
from .units import diagnostic_address

ROOT_ID = "/"  # the implicit root, which spans the whole address space
MAX_SECTORS = 2**20  # per region (E015), as every sector gets a list entry


@dataclass
class Node:
    """A placed region, with an absolute inclusive range. Reserved gap fillers are nodes too,
    marked `generated`. `has_name` and `explicit_start` record what the config spelled out."""

    id: str
    name: str
    start: int
    end: int
    path: Path
    kind: str = "normal"
    color: str | None = None
    note: str | None = None
    sectors: list[int] = field(default_factory=list)
    children: list[Node] = field(default_factory=list)
    parent: Node | None = None
    generated: bool = False
    has_name: bool = False
    explicit_start: bool = False

    @property
    def size(self) -> int:
        """Size in bytes."""
        return self.end - self.start + 1

    @property
    def reserved(self) -> bool:
        """Whether this is a reserved region."""
        return self.kind == "reserved"

    def walk(self) -> Iterator[Node]:
        """This node and every descendant, parents first."""
        yield self
        for child in self.children:
            yield from child.walk()

    def sector_edges(self) -> list[int]:
        """Sector boundaries from the nearest region with sectors, as start addresses."""
        node: Node | None = self
        while node is not None and not node.sectors:
            node = node.parent
        if node is None:
            return []
        edges = [node.start]
        for size in node.sectors:
            edges.append(edges[-1] + size)
        return edges


@dataclass
class Tree:
    """The placed regions: `root` spans the whole address space, `index` finds any node by id."""

    root: Node
    index: dict[str, Node]
    address_bits: int

    def address(self, value: int) -> str:
        """`value` in the diagnostic address style, at this tree's width."""
        return diagnostic_address(value, self.address_bits)


class _Resolver:
    """Turns `memory.regions` into nodes, collecting diagnostics as it goes."""

    def __init__(self, config: Config) -> None:
        """Take the memory settings and start with nothing placed."""
        self.fill_mode = config.memory.fill_gaps
        self.bits = config.memory.address_bits
        self.index: dict[str, Node] = {}
        self.first_path: dict[str, Path] = {}
        self.diagnostics: list[Diagnostic] = []

    def report(
        self,
        code: str,
        message: str,
        path: Path,
        hint: str | None = None,
        subject: str | None = None,
    ) -> None:
        """Record a diagnostic."""
        self.diagnostics.append(Diagnostic(code, message, path, hint, subject))

    def place(self, specs: list[Region], parent: Node) -> None:
        """Place the children of `parent`, then recurse. Regions in error are left out."""
        paths = [parent_child_path(parent, i) for i in range(len(specs))]
        spans: dict[int, tuple[int, int]] = {}

        autos = [i for i, spec in enumerate(specs) if spec.size == "auto"]
        for extra in autos[1:]:
            self.report(
                "E013",
                f"more than one `size: auto` in {parent.id}",
                paths[extra],
                "give all but one of these regions a fixed size",
                specs[extra].id,
            )

        # align: end regions stack down from the top of the parent.
        top = parent.end
        for i in range(len(specs) - 1, -1, -1):
            spec = specs[i]
            if spec.align != "end":
                continue
            if spec.start is not None or spec.end is not None:
                self.report(
                    "E012",
                    "`align: end` cannot be combined with `start` or `end`",
                    paths[i],
                    "remove `start` and `end`, the position comes from the parent's end",
                    spec.id,
                )
            elif spec.size == "auto":
                self.report(
                    "E013",
                    "`align: end` needs a fixed size",
                    paths[i],
                    "give the region a size, or remove `align: end`",
                    spec.id,
                )
            elif spec.size is None:
                self.report("E004", "region needs `size` or `end`", paths[i], subject=spec.id)
            else:
                spans[i] = (top - spec.size + 1, top)
                top -= spec.size

        cursor = parent.start
        for i, spec in enumerate(specs):
            if spec.align == "end":
                continue
            start = spec.start if spec.start is not None else cursor
            if i in autos[1:]:
                continue
            if spec.size == "auto":
                limit = self.auto_limit(specs, i, top + 1)
                end = limit - 1
                if spec.end is not None:
                    self.report(
                        "E012", "`size: auto` cannot be combined with `end`", paths[i], None, spec.id
                    )
                    continue
                if end < start:
                    self.report(
                        "E013",
                        f"nothing left for `size: auto` in {parent.id}",
                        paths[i],
                        "shrink the siblings, or grow the parent",
                        spec.id,
                    )
                    continue
            elif spec.end is not None:
                end = spec.end
                if spec.size is not None and start + spec.size - 1 != end:
                    self.report(
                        "E012",
                        f"`size` and `end` disagree: start {self.hex(start)} + size "
                        f"{self.hex(spec.size)} ends at {self.hex(start + spec.size - 1)}, "
                        f"not {self.hex(end)}",
                        paths[i],
                        "keep only `size`",
                        spec.id,
                    )
                    continue
            elif spec.size is not None:
                end = start + spec.size - 1
            else:
                self.report(
                    "E004",
                    "region needs `size` or `end`",
                    paths[i],
                    "add `size`, for example `size: 16K`",
                    spec.id,
                )
                continue
            if end < start:
                self.report(
                    "E012",
                    f"end {self.hex(end)} is before start {self.hex(start)}",
                    paths[i],
                    "ranges are inclusive: end = start + size - 1",
                    spec.id,
                )
                continue
            spans[i] = (start, end)
            cursor = end + 1

        for i, spec in enumerate(specs):
            self.add_id(spec, paths[i])
            if i not in spans:
                continue
            node = self.make_node(spec, spans[i], parent, paths[i])
            parent.children.append(node)
            self.place(spec.children, node)

    def auto_limit(self, specs: list[Region], index: int, default: int) -> int:
        """The address an auto region must stop before: next explicit start or the top, less
        the size of any sequential siblings that still have to fit after it."""
        anchor = default
        tail = 0
        for spec in specs[index + 1 :]:
            if spec.align == "end":
                continue
            if spec.start is not None:
                anchor = spec.start
                break
            if isinstance(spec.size, int):
                tail += spec.size
        return anchor - tail

    def add_id(self, spec: Region, path: Path) -> None:
        """Note the first use of a region id, reporting any later one as E003."""
        if spec.id in self.first_path:
            self.report(
                "E003",
                f"duplicate region id '{spec.id}'",
                path,
                f"ids are unique across the tree; first used at "
                f"{format_path(self.first_path[spec.id])}",
                spec.id,
            )
        else:
            self.first_path[spec.id] = path

    def make_node(self, spec: Region, span: tuple[int, int], parent: Node, path: Path) -> Node:
        """Build the node for a placed spec, expanding its sectors to one size per sector."""
        sectors: list[int] = []
        runs = spec.sectors or []
        count = sum(run.count for run in runs)
        if count > MAX_SECTORS:
            self.report(
                "E015",
                f"{count} sectors is more than the limit of {MAX_SECTORS}",
                path + ("sectors",),
                "use a larger sector size, or leave `sectors` out",
                spec.id,
            )
        else:
            for run in runs:
                sectors += [run.size] * run.count
        node = Node(
            id=spec.id,
            name=spec.name if spec.name is not None else spec.id,
            start=span[0],
            end=span[1],
            path=path,
            kind=spec.kind,
            color=spec.color,
            note=spec.note,
            sectors=sectors,
            parent=parent,
            has_name=spec.name is not None,
            explicit_start=spec.start is not None,
        )
        self.index.setdefault(spec.id, node)
        return node

    def fill(self, node: Node) -> None:
        """Sort the children of `node` and below, handling their gaps as `fill_gaps` says."""
        for child in node.children:
            self.fill(child)
        node.children.sort(key=lambda child: (child.start, child.end))
        if not node.children:
            return
        mode = "reserved" if node.id == ROOT_ID and self.fill_mode == "error" else self.fill_mode
        spans = []
        cursor = node.start
        for child in node.children:
            if child.start > cursor:
                spans.append((cursor, child.start - 1))
            cursor = max(cursor, child.end + 1)
        if cursor <= node.end:
            spans.append((cursor, node.end))
        for start, end in spans:
            text = f"gap {self.hex(start)} - {self.hex(end)} in {node.id}"
            if mode == "error":
                self.report(
                    "E014",
                    f"{text} with `fill_gaps: error`",
                    node.path,
                    "cover the gap with a region, or set `memory.fill_gaps: reserved`",
                    node.id,
                )
            if mode == "reserved":
                self.report("W101", f"{text} filled as reserved", node.path, None, node.id)
                gap_id = f"_gap_{start:x}"
                while gap_id in self.index:  # a user id may have the same form
                    gap_id += "_"
                gap = Node(
                    id=gap_id,
                    name="Reserved",
                    start=start,
                    end=end,
                    path=node.path,
                    kind="reserved",
                    parent=node,
                    generated=True,
                )
                node.children.append(gap)
                self.index[gap.id] = gap
        node.children.sort(key=lambda child: (child.start, child.end))

    def hex(self, value: int) -> str:
        """`value` in the diagnostic address style."""
        return diagnostic_address(value, self.bits)


def parent_child_path(parent: Node, index: int) -> Path:
    """The YAML path of child `index` of `parent`."""
    key = "regions" if parent.id == ROOT_ID else "children"
    return parent.path + (key, index)


def resolve_tree(config: Config) -> tuple[Tree, list[Diagnostic]]:
    """Place every region under an implicit root and fill the gaps."""
    bits = config.memory.address_bits
    root = Node(ROOT_ID, ROOT_ID, 0, 2**bits - 1, ("memory",))
    resolver = _Resolver(config)
    resolver.place(config.memory.regions, root)
    resolver.fill(root)
    resolver.index[ROOT_ID] = root
    return Tree(root, resolver.index, bits), resolver.diagnostics
