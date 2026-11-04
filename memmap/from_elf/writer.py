"""Write an import as config text, laid out like the examples: one line per section."""

from __future__ import annotations

import json

from .blocks import Bucket, Item
from .sizes import address_literal, approx_size, size_literal

COLUMN_WIDTH = 150  # px, room for section names such as .ARM.exidx and the usage title


def _quote(text: str) -> str:
    """A YAML double-quoted scalar. JSON strings are valid YAML."""
    return json.dumps(text, ensure_ascii=False)


def comment_safe(text: str) -> str:
    """`text` for a YAML comment: anything that could end the line becomes ?."""
    return "".join(char if char.isprintable() else "?" for char in text)


def _item_fields(item: Item, bits: int) -> list[str]:
    """The keys of one region, as `key: value` strings."""
    fields = [f"id: {_quote(item.id)}", f"name: {_quote(item.name)}"]
    if item.free:
        fields.append("kind: reserved")
    fields += [f"start: {address_literal(item.start, bits)}", f"size: {size_literal(item.size)}"]
    if item.note:
        fields.append(f"note: {_quote(item.note)}")
    return fields


def _item_lines(item: Item, bits: int, indent: str) -> list[str]:
    """One region as list entry lines: a flow mapping on one line, or a block mapping with its
    children one per line."""
    fields = _item_fields(item, bits)
    if not item.children:
        return [f"{indent}- {{ " + ", ".join(fields) + " }"]
    lines = [f"{indent}- {fields[0]}"]
    lines += [f"{indent}  {field}" for field in fields[1:]]
    lines.append(f"{indent}  children:")
    for child in item.children:
        lines += _item_lines(child, bits, indent + "    ")
    return lines


def _usage_title(bucket: Bucket) -> str:
    """A column title: the region name, then how much of it the sections use."""
    memory = bucket.memory
    assert memory is not None
    used = bucket.used
    percent = round(100 * used / memory.size)
    if used < memory.size:
        percent = min(percent, 99)  # 99.6% is not full
    shown = "<1" if percent == 0 and used else str(percent)
    return f"{memory.name}\n{approx_size(used)} of {approx_size(memory.size)} ({shown}%)"


def _expansions(buckets: list[Bucket], top: Bucket) -> list[tuple[str, str]]:
    """Region ids with a detail column of their own, with their titles."""
    found = []
    for bucket in [*buckets, top]:
        for item in bucket.items:
            if item.expanded:
                where = bucket.memory.name if bucket.memory else "memory"
                found.append((item.id, f"{item.name} in {where}"))
    return found


def _memory_lines(bits: int, buckets: list[Bucket], top: Bucket) -> list[str]:
    """The `memory` section: each region with its blocks, and the blocks outside every region,
    in address order."""
    lines = ["memory:"]
    if bits == 64:
        lines.append("  address_bits: 64")
    lines += [
        "  fill_gaps: none                   # gaps under --min-size show as a split edge",
        "  regions:",
    ]
    entries: list[tuple[int, list[str]]] = []
    for bucket in buckets:
        memory = bucket.memory
        assert memory is not None
        block = [
            f"    - id: {_quote(memory.id)}",
            f"      name: {_quote(memory.name)}",
            f"      start: {address_literal(memory.start, bits)}",
            f"      size: {size_literal(memory.size)}",
        ]
        if bucket.items:
            block.append("      children:")
            for item in bucket.items:
                block += _item_lines(item, bits, "        ")
        entries.append((memory.start, block))
    entries += [(item.start, _item_lines(item, bits, "    ")) for item in top.items]
    for _, block in sorted(entries, key=lambda entry: entry[0]):
        lines += block
    return lines


def _view_lines(buckets: list[Bucket], top: Bucket) -> list[str]:
    """The `view` section: a column per drawn region, or one for the whole address space with
    the regions zoomed out of it, plus a detail column per folded group."""
    lines = ["view:", "  defaults:", "    labels: { size: true }", "  columns:"]
    drawn = [bucket for bucket in buckets if bucket.drawn]
    expand: list[str] = []
    if drawn and not top.items:
        for bucket in drawn:
            memory = bucket.memory
            assert memory is not None
            title = _quote(_usage_title(bucket))
            lines.append(
                f"    - {{ id: {_quote(memory.id + '_col')}, root: {_quote(memory.id)}, title: {title}, "
                f"width: {COLUMN_WIDTH} }}"
            )
    else:  # no regions, or sections outside them: show the whole address space
        lines.append(f'    - {{ id: all, root: "/", width: {COLUMN_WIDTH} }}')
        for bucket in drawn:
            memory = bucket.memory
            assert memory is not None
            title = _quote(_usage_title(bucket))
            expand.append(f"    - {{ region: {_quote(memory.id)}, title: {title} }}")
    empty = [bucket.memory for bucket in buckets if not bucket.drawn and bucket.memory]
    if empty:
        names = ", ".join(comment_safe(memory.name) for memory in empty)
        lines.append(f"    # No section lies in {names}. To draw one, add a column with its id as root.")
    for region, title in _expansions(buckets, top):  # a column of its own, for its width
        root, column = _quote(region), _quote(f"{region}_col")
        lines.append(f"    - {{ id: {column}, root: {root}, width: {COLUMN_WIDTH} }}")
        expand.append(f"    - {{ region: {root}, to: {column}, title: {_quote(title)} }}")
    if expand:
        lines += ["  expand:", *expand]
    return lines


def write_yaml(
    source: str,
    bits: int,
    buckets: list[Bucket],
    top: Bucket,
    regions_from: str = "--region",
    notes: tuple[str, ...] = (),
) -> str:
    """The config text. `regions_from` says where the memory regions came from, and each of
    `notes` becomes a comment line under the header."""
    lines = [
        f"# Generated by `memmap from-elf` from {comment_safe(source)}. Section ranges come from",
        (
            f"# the ELF and memory regions from {comment_safe(regions_from)}. Rename, merge or "
            "annotate blocks freely."
        ),
        *(f"# {comment_safe(note)}" for note in notes),
        "version: 1",
        "",
        "figure:",
        f"  title: {_quote(f'{source} memory map')}",
        "",
        *_memory_lines(bits, buckets, top),
        "",
        *_view_lines(buckets, top),
        "",
        "style:",
        "  reserved: { show_label: true }  # names the Free blocks",
        "",
    ]
    return "\n".join(lines)
