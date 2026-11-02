"""Semantic rules on the resolved tree (E010, E011, W100, W103, W104)."""

from __future__ import annotations

from .diagnostics import Diagnostic, format_path
from .resolve import ROOT_ID, Node, Tree


def _describe_range(node: Node, tree: Tree) -> str:
    """A node's range, for a diagnostic's details."""
    return f"{tree.address(node.start)} - {tree.address(node.end)}"


def _check_containment(node: Node, tree: Tree, out: list[Diagnostic]) -> None:
    """E010 for each child reaching outside `node`."""
    for child in node.children:
        if child.generated or (child.start >= node.start and child.end <= node.end):
            continue
        over = max(child.end - node.end, node.start - child.start, 0)
        out.append(
            Diagnostic(
                "E010",
                f"extends outside parent {node.id}",
                child.path,
                f"shrink {child.id} by {over} bytes or grow {node.id}",
                child.id,
                [
                    f"{child.id}: {_describe_range(child, tree)}",
                    f"{node.id}: {_describe_range(node, tree)}",
                ],
            )
        )


def _check_overlap(node: Node, tree: Tree, out: list[Diagnostic]) -> None:
    """E011 for each pair of overlapping children."""
    seen: list[Node] = []
    # Children are sorted by address; report the one that comes later in the file.
    for child in node.children:
        if child.generated:
            continue
        for other in seen:
            if child.start > other.end or other.start > child.end:
                continue
            first, second = sorted((other, child), key=lambda n: n.path)
            if second.explicit_start:
                hint = f"remove 'start' from {second.id} to place it right after {first.id}"
            else:
                hint = f"shrink {first.id} or move {second.id}"
            out.append(
                Diagnostic(
                    "E011",
                    f"overlaps {format_path(first.path)} ({first.id})",
                    second.path,
                    hint,
                    second.id,
                    [
                        f"{second.id}: {_describe_range(second, tree)}",
                        f"{first.id}: {_describe_range(first, tree)}",
                    ],
                )
            )
        seen.append(child)


def _nearest_edges(address: int, edges: list[int]) -> tuple[int, int]:
    """The sector boundaries around `address`, below then above."""
    below = max((edge for edge in edges if edge <= address), default=edges[0])
    above = min((edge for edge in edges if edge >= address), default=edges[-1])
    return below, above


def _check_sectors(node: Node, tree: Tree, out: list[Diagnostic]) -> None:
    """W104 for a sector list that does not cover the region, W100 for each block boundary
    inside it that is off a sector boundary."""
    edges = node.sector_edges()
    edge_set = set(edges)
    if sum(node.sectors) != node.size:
        out.append(
            Diagnostic(
                "W104",
                f"sectors add up to {sum(node.sectors)} bytes but the region is {node.size} bytes",
                node.path + ("sectors",),
                "make the sector list cover the whole region",
                node.id,
            )
        )
    # The end of each block is one boundary and the next block's start is the same boundary, so
    # only a start with no adjacent previous sibling is checked on its own.
    # A block outside the region is E010's to report, there is no sector to compare it with.
    def misaligned(address: int) -> bool:
        """Whether `address` is inside the sector range but not on a boundary."""
        return edges[0] <= address <= edges[-1] and address not in edge_set

    for parent in node.walk():
        previous = None
        for child in parent.children:
            if child.generated:
                previous = None
                continue
            adjacent = previous is not None and previous.end + 1 == child.start
            if not adjacent and misaligned(child.start):
                _report_sector(child, "start", child.start, edges, tree, out)
            if misaligned(child.end + 1):
                _report_sector(child, "end", child.end + 1, edges, tree, out)
            previous = child


def _report_sector(
    node: Node, which: str, address: int, edges: list[int], tree: Tree, out: list[Diagnostic]
) -> None:
    """W100 for one boundary, with the nearest sector boundaries in the hint."""
    below, above = _nearest_edges(address, edges)
    out.append(
        Diagnostic(
            "W100",
            f"{which} of {node.id} is not on a flash sector boundary",
            node.path,
            f"nearest boundaries are {tree.address(below)} and {tree.address(above)}; "
            f"move this boundary down {address - below} or up {above - address} bytes",
            node.id,
            [f"boundary at {tree.address(address)}"],
        )
    )


def _check_natural_alignment(tree: Tree, out: list[Diagnostic]) -> None:
    """W103 for each region whose start is not a multiple of its size."""
    for node in tree.root.walk():
        if node.id == ROOT_ID or node.generated or node.start % node.size == 0:
            continue
        out.append(
            Diagnostic(
                "W103",
                f"start {tree.address(node.start)} is not aligned to the region size "
                f"({node.size} bytes)",
                node.path,
                "move the start to a multiple of the size",
                node.id,
            )
        )


def check_tree(tree: Tree, check_alignment: bool = False) -> list[Diagnostic]:
    """Run every rule on the tree. W103 only runs with `check_alignment`."""
    out: list[Diagnostic] = []
    for node in tree.root.walk():
        _check_containment(node, tree, out)
        _check_overlap(node, tree, out)
        if node.sectors:
            _check_sectors(node, tree, out)
    if check_alignment:
        _check_natural_alignment(tree, out)
    return out
