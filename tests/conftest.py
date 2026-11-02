from pathlib import Path

import pytest

from memmap.layout.primitives import Drawing
from memmap.pipeline import Result, run

ROOT = Path(__file__).parent.parent
MODES = ROOT / "tests" / "fixtures" / "modes.yaml"


@pytest.fixture
def load(tmp_path):
    """Run the pipeline on YAML text."""

    def _load(text, sets=(), verbose=False):
        file = tmp_path / "layout.yaml"
        file.write_text(text, encoding="utf-8")
        return run(file, sets, verbose)

    return _load


def codes(result):
    return [diagnostic.code for diagnostic in result.diagnostics]


def drawn(result: Result) -> Drawing:
    """The drawing of a run that must have reached layout."""
    assert result.drawing is not None, codes(result)
    return result.drawing


def regions(body):
    """A minimal config around a `memory.regions` body, indented by four spaces."""
    return f"version: 1\nmemory:\n  regions:\n{body}\n"
