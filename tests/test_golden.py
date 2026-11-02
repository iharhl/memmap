"""Golden SVGs. Regenerate with: MEMMAP_UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py"""

import hashlib
import os
import subprocess
import sys

import pytest
from conftest import MODES, ROOT, codes, drawn

from memmap.pipeline import run
from memmap.render.svg import render_svg

GOLDEN = ROOT / "tests" / "golden"
UPDATE = os.environ.get("MEMMAP_UPDATE_GOLDEN") == "1"
EXPANDED = ROOT / "examples" / "stm32f303_fig8.yaml"

CASES = {
    "example_f303": (EXPANDED, []),
    "example_f051": (ROOT / "examples" / "stm32f051_fig10.yaml", []),
    "example_custom": (ROOT / "examples" / "custom_bootloader.yaml", []),
    "example_f407_ota": (ROOT / "examples" / "stm32f407_ota.yaml", []),
    "example_nrf52840": (ROOT / "examples" / "nrf52840_boot_chain.yaml", []),
    "example_riscv64": (ROOT / "examples" / "riscv64_soc.yaml", []),
    "example_rtos_mpu": (ROOT / "examples" / "rtos_mpu_ram.yaml", []),
    "column_titles": (
        EXPANDED,
        ["view.columns[1].title=Peripherals", "view.columns[2].title=Code area"],
    ),
}
for mode in ("start", "both"):
    CASES[f"address_{mode}"] = (MODES, [f"view.defaults.labels.address={mode}"])
for mode in ("weighted", "equal", "linear"):
    CASES[f"scale_{mode}"] = (MODES, [f"view.defaults.scale.mode={mode}"])
CASES["size"] = (MODES, ["view.defaults.labels.size=true"])
CASES["palette_color"] = (MODES, ["style.palette=color"])


@pytest.mark.parametrize("name", CASES)
def test_golden(name):
    file, sets = CASES[name]
    result = run(file, sets)
    assert codes(result) == []
    svg = render_svg(drawn(result))
    golden = GOLDEN / f"{name}.svg"
    if UPDATE:
        golden.write_text(svg, encoding="utf-8", newline="\n")
    assert svg == golden.read_text(encoding="utf-8")


def test_output_is_identical_across_processes(tmp_path):
    digests = set()
    for seed in ("0", "1", "4242"):
        output = tmp_path / f"{seed}.svg"
        subprocess.run(
            [sys.executable, "-m", "memmap", "render", str(EXPANDED), "-o", str(output)],
            check=True,
            capture_output=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        digests.add(hashlib.sha256(output.read_bytes()).hexdigest())
    assert len(digests) == 1


def test_svg_has_sorted_attributes_and_no_timestamps():
    svg = render_svg(drawn(run(MODES)))
    first_rect = svg.splitlines()[1]
    names = [part.split("=")[0] for part in first_rect[len("<rect ") : -2].split('" ')]
    assert names == sorted(names)
    assert "date" not in svg.lower()
