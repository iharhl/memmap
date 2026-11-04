"""Real firmware layouts through `from-elf`, from the header snapshots in
tests/fixtures/elf_snapshots/. See docs/from-elf.md.

Each snapshot is rebuilt into a minimal ELF, imported with its regions, and the config and its
drawing are compared with tests/golden/elf/. Regenerate after an intended change with:

    MEMMAP_UPDATE_GOLDEN=1 uv run pytest tests/test_elf_snapshots.py
"""

import json
import os

import pytest
from conftest import ROOT, codes, drawn
from elf_builder import rebuild_snapshot as rebuild

from memmap.from_elf import import_elf, parse_region_spec, read_elf
from memmap.pipeline import run
from memmap.render.svg import render_svg

SNAPSHOTS = ROOT / "tests" / "fixtures" / "elf_snapshots"
GOLDEN = ROOT / "tests" / "golden" / "elf"
UPDATE = os.environ.get("MEMMAP_UPDATE_GOLDEN") == "1"
NAMES = sorted(path.stem for path in SNAPSHOTS.glob("*.json"))


def load(name):
    """A snapshot as a dict."""
    return json.loads((SNAPSHOTS / f"{name}.json").read_text(encoding="utf-8"))


def compare(golden, text):
    """Check `text` against a golden file, or rewrite it with MEMMAP_UPDATE_GOLDEN=1."""
    if UPDATE:
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(text, encoding="utf-8", newline="\n")
    assert text == golden.read_text(encoding="utf-8")


def test_there_are_snapshots():
    assert len(NAMES) >= 8


@pytest.mark.parametrize("name", NAMES)
def test_snapshot_rebuilds_to_the_same_headers(name):
    snapshot = load(name)
    elf = read_elf(rebuild(snapshot))
    wanted = [s for s in snapshot["sections"] if s["name"] != ".shstrtab"]
    assert [(s.name, s.kind, s.flags, s.address, s.size) for s in elf.sections[: len(wanted)]] == [
        (s["name"], s["type"], s["flags"], int(s["address"], 16), s["size"]) for s in wanted
    ]
    assert elf.bits == snapshot["bits"]


@pytest.mark.parametrize("name", NAMES)
def test_snapshot_imports_to_the_golden_config_and_drawing(name, tmp_path):
    snapshot = load(name)
    memories = [parse_region_spec(spec) for spec in snapshot["regions"]]
    imported = import_elf(read_elf(rebuild(snapshot)), f"{name}.elf", memories)
    assert imported.diagnostics == []
    compare(GOLDEN / f"{name}.yaml", imported.text)

    config = tmp_path / f"{name}.yaml"
    config.write_text(imported.text, encoding="utf-8")
    result = run(config)
    assert codes(result) == []  # passes check --strict
    compare(GOLDEN / f"{name}.svg", render_svg(drawn(result)))
