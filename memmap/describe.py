"""The `describe` output: everything that will be drawn, as text or JSON."""

from __future__ import annotations

from typing import Any

from .layout.columns import Block
from .layout.primitives import Text
from .options import BUILTIN
from .pipeline import Result
from .resolve import Node, Tree
from .units import format_size


def _region(node: Node, tree: Tree) -> dict[str, Any]:
    """A node and its children as JSON data."""
    data: dict[str, Any] = {
        "id": node.id,
        "name": node.name,
        "kind": node.kind,
        "start": node.start,
        "end": node.end,
        "start_hex": tree.address(node.start),
        "end_hex": tree.address(node.end),
        "size": node.size,
        "generated": node.generated,
    }
    if node.space is not None and node.parent is not None and node.parent.parent is None:
        data["space"] = node.space
    if node.children:
        data["children"] = [_region(child, tree) for child in node.children]
    return data


def _box(item: Block) -> dict[str, float]:
    """A block's pixel box, rounded."""
    return {"x": round(item.x, 2), "y": round(item.y, 2), "w": round(item.w, 2), "h": round(item.h, 2)}


def _label(text: Text) -> dict[str, Any]:
    """A drawn string as JSON data, with its approximate box."""
    x, y, w, h = text.box()
    return {
        "text": text.text,
        "role": text.role,
        "column": text.column,
        "region": text.region,
        "x": round(text.x, 2),
        "y": round(text.y, 2),
        "box": {"x": round(x, 2), "y": round(y, 2), "w": round(w, 2), "h": round(h, 2)},
    }


def describe_data(result: Result) -> dict[str, Any]:
    """The `describe --json` object. Needs a run that reached layout."""
    tree = result.tree
    drawing = result.drawing
    assert result.config is not None and tree is not None and drawing is not None
    columns = []
    blocks = []
    for layout in drawing.columns:
        options = {
            path: {"value": layout.options.values[path], "origin": origin}
            for path, origin in sorted(layout.options.origins.items())
        }
        columns.append(
            {
                "id": layout.id,
                "title": layout.view.title,
                "root": layout.view.root.id,
                "lane": layout.lane,
                "box": {
                    "x": round(layout.x, 2),
                    "y": round(layout.y, 2),
                    "w": round(layout.width, 2),
                    "h": round(layout.height, 2),
                },
                "options": options,
            }
        )
        for block in layout.blocks:
            blocks.append(
                {
                    "id": block.node.id,
                    "name": block.node.name,
                    "kind": block.node.kind,
                    "column": layout.id,
                    "start": block.start,
                    "end": block.end,
                    "start_hex": tree.address(block.start),
                    "end_hex": tree.address(block.end),
                    "size": block.end - block.start + 1,
                    "band": block.band,
                    "box": _box(block),
                    "text": block.lines,
                }
            )
    return {
        "ok": True,
        "diagnostics": [d.to_json() for d in result.diagnostics],
        "figure": {
            "title": result.config.figure.title,
            "footer": result.config.figure.footer,
            "width": round(drawing.width, 2),
            "height": round(drawing.height, 2),
        },
        "regions": [_region(child, tree) for child in tree.root.children],
        "columns": columns,
        "blocks": blocks,
        "connectors": drawing.links,
        "labels": [_label(text) for text in drawing.texts()],
    }


def _tree_lines(node: Node, tree: Tree, depth: int = 0) -> list[str]:
    """One text line per region, indented by depth."""
    tag = " [generated]" if node.generated else ""
    if depth == 1 and node.space is not None:
        tag += f" [space {node.space}]"
    kind = "" if node.kind == "normal" else f" {node.kind}"
    name = node.name.replace("\n", " ")
    lines = [
        (
            f"{'  ' * depth}{tree.address(node.start)} - {tree.address(node.end)}  "
            f"{format_size(node.size)}  {node.id} \"{name}\"{kind}{tag}"
        )
    ]
    for child in node.children:
        lines += _tree_lines(child, tree, depth + 1)
    return lines


def describe_text(result: Result) -> str:
    """The plain text `describe` report. Needs a run that reached layout."""
    tree = result.tree
    drawing = result.drawing
    assert result.config is not None and tree is not None and drawing is not None
    out = []
    title = result.config.figure.title or "(untitled)"
    out.append(f"Figure: {title}, {drawing.width:.0f} x {drawing.height:.0f} px")
    out.append("")
    out.append("Regions:")
    for child in tree.root.children:
        out += _tree_lines(child, tree, 1)
    for layout in drawing.columns:
        out.append("")
        out.append(
            f"Column {layout.id}: root {layout.view.root.id}, lane {layout.lane}, "
            f"box x={layout.x:.1f} y={layout.y:.1f} w={layout.width:.1f} h={layout.height:.1f}"
        )
        if layout.view.title:
            title_text = layout.view.title.replace("\n", " ")
            out.append(f"  title: {title_text}")
        out.append("  options (all others are built-in defaults):")
        for path, origin in sorted(layout.options.origins.items()):
            if origin != BUILTIN:
                out.append(f"    {path} = {layout.options.values[path]!r}  [{origin}]")
        out.append("  blocks, top to bottom:")
        for block in reversed(layout.blocks):
            name = block.node.name.replace("\n", " ")
            out.append(
                f"    {tree.address(block.start)} - {tree.address(block.end)}  {block.node.id} "
                f"\"{name}\"  y={block.y:.1f} h={block.h:.1f}"
            )
            if block.top_label:
                out.append(f"      top_label = {block.top_label!r}  [override:{block.node.id}]")
        out.append("  address labels, top to bottom:")
        for label in sorted(layout.labels, key=lambda item: item.y):
            edge = "bottom" if label.edge == 0 else "top"
            out.append(f"    {label.text}  ({label.role}, at {edge} of {label.region})")
    if drawing.links:
        out.append("")
        out.append("Connectors:")
        for link in drawing.links:
            out.append(f"  {link['region']}: {link['from']} -> {link['to']}")
    return "\n".join(out)
