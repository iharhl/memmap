import json
import re
import shutil

import pytest
from click.testing import CliRunner
from conftest import MODES, ROOT

from memmap import __version__
from memmap.cli import app

runner = CliRunner()
CUSTOM = str(ROOT / "examples" / "custom_bootloader.yaml")
F303 = str(ROOT / "examples" / "stm32f303_fig8.yaml")


def invoke(*args):
    return runner.invoke(app, [str(arg) for arg in args])


def bad_file(tmp_path, old="size: 16K, color", new="size: 15K, color"):
    text = (ROOT / "examples" / "custom_bootloader.yaml").read_text().replace(old, new)
    file = tmp_path / "layout.yaml"
    file.write_text(text)
    return file


def test_version():
    result = invoke("--version")
    assert result.exit_code == 0
    assert __version__ in result.stdout and "schema versions: 1" in result.stdout


def test_version_matches_pyproject():
    # `uv version --bump` updates pyproject.toml but not memmap/__init__.py.
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert re.search(r'^version = "(.+)"$', pyproject, re.MULTILINE).group(1) == __version__


def test_render_defaults_to_config_name_with_svg(tmp_path):
    file = tmp_path / "layout.yaml"
    shutil.copy(CUSTOM, file)
    result = invoke("render", file)
    assert result.exit_code == 0
    assert (tmp_path / "layout.svg").read_text().startswith("<svg ")


def test_render_infers_format_from_extension(tmp_path):
    target = tmp_path / "out.svg"
    assert invoke("render", CUSTOM, "-o", target).exit_code == 0
    assert target.exists()


def test_render_rejects_unknown_extension(tmp_path):
    assert invoke("render", CUSTOM, "-o", tmp_path / "out.bmp").exit_code == 2


def test_render_set_overrides_any_option(tmp_path):
    target = tmp_path / "out.svg"
    assert invoke("render", MODES, "-o", target, "--set", "view.defaults.labels.size=true").exit_code == 0
    assert "4 KB" in target.read_text()


def test_render_will_not_overwrite_the_config(tmp_path):
    file = tmp_path / "layout.yaml"
    shutil.copy(CUSTOM, file)
    original = file.read_text()
    result = invoke("render", file, "--format", "svg", "-o", file, "--json")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "E090"
    assert file.read_text() == original


def test_render_will_not_overwrite_a_config_with_the_output_suffix(tmp_path):
    file = tmp_path / "layout.svg"
    shutil.copy(CUSTOM, file)
    original = file.read_text()
    assert invoke("render", file).exit_code == 2
    assert file.read_text() == original


@pytest.mark.parametrize(
    "bad", ["no_equals", "=value", "[0].a=1", "view.columns[9].width=1"]
)
def test_bad_set_is_a_usage_error(bad):
    assert invoke("check", MODES, "--set", bad).exit_code == 2


def test_render_stops_on_validation_errors(tmp_path):
    file = bad_file(tmp_path, "size: 16K, color", "size: 7Q, color")
    target = tmp_path / "out.svg"
    assert invoke("render", file, "-o", target).exit_code == 1
    assert not target.exists()


def test_render_reports_a_raster_failure_as_exit_3(tmp_path, monkeypatch):
    def unavailable(svg, output_format):
        from memmap.render.raster import RasterUnavailable

        raise RasterUnavailable("install memmap[raster]")

    monkeypatch.setattr("memmap.cli.convert", unavailable)
    result = invoke("render", CUSTOM, "-o", tmp_path / "out.png")
    assert result.exit_code == 3
    assert "memmap[raster]" in result.stderr


def test_render_json_reports_the_output(tmp_path):
    target = tmp_path / "out.svg"
    result = invoke("render", CUSTOM, "-o", target, "--json")
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "ok": True,
        "output": str(target),
        "format": "svg",
        "diagnostics": [],
    }
    assert target.exists()


def test_render_json_keeps_warnings_on_success(tmp_path):
    result = invoke("render", bad_file(tmp_path), "-o", tmp_path / "out.svg", "--json")
    assert result.exit_code == 0
    assert result.stderr == ""
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "W100"


def test_render_json_on_validation_errors(tmp_path):
    file = bad_file(tmp_path, "size: 16K, color", "size: 7Q, color")
    target = tmp_path / "out.svg"
    result = invoke("render", file, "-o", target, "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "E002"
    assert not target.exists()


def test_render_strict_stops_on_warnings(tmp_path):
    file = bad_file(tmp_path)
    target = tmp_path / "out.svg"
    assert invoke("render", file, "-o", target, "--strict").exit_code == 1
    assert not target.exists()
    assert invoke("render", file, "-o", target).exit_code == 0


def test_render_json_reports_a_raster_failure_as_e091(tmp_path, monkeypatch):
    def unavailable(svg, output_format):
        from memmap.render.raster import RasterUnavailable

        raise RasterUnavailable("install memmap[raster]")

    monkeypatch.setattr("memmap.cli.convert", unavailable)
    result = invoke("render", CUSTOM, "-o", tmp_path / "out.png", "--json")
    assert result.exit_code == 3
    diagnostic = json.loads(result.stdout)["diagnostics"][-1]
    assert diagnostic["code"] == "E091"
    assert "memmap[raster]" in diagnostic["message"]


@pytest.mark.parametrize(
    "args",
    [
        ("check", "nope.yaml"),
        ("describe", "nope.yaml"),
        ("render", "nope.yaml"),
        ("render", CUSTOM, "-o", "out.bmp"),
        ("check", CUSTOM, "--set", "no_equals"),
    ],
)
def test_json_covers_usage_failures(args):
    result = invoke(*args, "--json")
    assert result.exit_code == 2
    data = json.loads(result.stdout)
    assert data["ok"] is False
    assert data["diagnostics"][0]["code"] == "E090"


def test_check_ok():
    result = invoke("check", CUSTOM)
    assert result.exit_code == 0
    assert "ok" in result.stdout


def test_check_warning_only_fails_with_strict(tmp_path):
    file = bad_file(tmp_path)
    assert invoke("check", file).exit_code == 0
    assert invoke("check", file, "--strict").exit_code == 1


def test_check_json_shape(tmp_path):
    file = bad_file(tmp_path, "size: 40K, color", "start: 0x08003800, size: 40K, color")
    result = invoke("check", file, "--json")
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["ok"] is False
    assert data["diagnostics"][0] == {
        "code": "E011",
        "level": "error",
        "path": "memory.regions[0].children[1]",
        "line": 15,
        "column": 11,
        "message": "overlaps memory.regions[0].children[0] (boot)",
        "hint": "remove 'start' from app to place it right after boot",
        "subject": "app",
        "details": [
            "app: 0x0800_3800 - 0x0800_D7FF",
            "boot: 0x0800_0000 - 0x0800_3FFF",
        ],
    }


def test_check_human_format(tmp_path):
    file = bad_file(tmp_path, "size: 40K, color", "start: 0x08003800, size: 40K, color")
    lines = invoke("check", file).stderr.splitlines()
    assert lines[0] == "layout.yaml:15:11  E011  memory.regions[0].children[1] (app)"
    assert lines[-1] == "  hint: remove 'start' from app to place it right after boot"


def test_check_verbose_reports_gaps():
    result = invoke("check", MODES, "--verbose")
    assert "W101" in result.stderr


def test_missing_file_is_a_usage_error():
    assert invoke("check", "nope.yaml").exit_code == 2


def test_describe_lists_ranges_and_origins():
    result = invoke("describe", F303)
    assert result.exit_code == 0
    assert "0x5000 03FF" in result.stdout
    assert "scale.mode = 'equal'  [view.defaults]" in result.stdout
    assert "top_label = 'end'  [override:ahb1]" in result.stdout


def test_describe_json_has_blocks_with_boxes_and_labels():
    data = json.loads(invoke("describe", F303, "--json").stdout)
    flash = next(block for block in data["blocks"] if block["id"] == "flash")
    assert flash["column"] == "code_detail"
    assert (flash["start"], flash["end"], flash["size"]) == (0x08000000, 0x0800FFFF, 65536)
    assert set(flash["box"]) == {"x", "y", "w", "h"}
    roles = {label["role"] for label in data["labels"]}
    assert {"title", "footer", "legend", "band", "address", "name"} <= roles
    main = next(column for column in data["columns"] if column["id"] == "main")
    assert main["options"]["style.reserved.show_label"] == {
        "value": False,
        "origin": "column:main",
    }


def test_describe_json_on_errors(tmp_path):
    file = bad_file(tmp_path, "size: 16K, color", "size: 7Q, color")
    result = invoke("describe", file, "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "E002"


def test_describe_json_gives_every_label_a_box():
    data = json.loads(invoke("describe", F303, "--json").stdout)
    assert all(set(label["box"]) == {"x", "y", "w", "h"} for label in data["labels"])
    label = next(label for label in data["labels"] if label["role"] == "address")
    assert label["box"]["x"] + label["box"]["w"] == pytest.approx(label["x"], abs=0.02)


def test_schema_prints_json():
    assert json.loads(invoke("schema").stdout)["title"] == "Config"


@pytest.mark.parametrize("example", sorted((ROOT / "examples").glob("*.yaml")), ids=lambda path: path.stem)
def test_examples_are_valid_commented_starters(example):
    assert example.read_text().startswith("#")
    assert invoke("check", example).exit_code == 0


@pytest.mark.parametrize("command", ["render", "check", "describe", "from-elf", "schema", "guide"])
def test_help_ends_with_an_example(command):
    lines = invoke(command, "--help").stdout.rstrip().splitlines()
    assert lines[-1].strip().startswith("memmap ")
    assert lines[-2].strip() == "Example:"


def test_guide_prints_the_agents_file():
    result = invoke("guide")
    assert result.exit_code == 0
    assert result.stdout == (ROOT / "AGENTS.md").read_text()


def test_group_help_points_to_the_guide():
    assert "memmap guide" in invoke("--help").stdout


@pytest.mark.parametrize(
    "sets",
    [
        ["view.defaults.labels.size=true"],
        ["view.defaults.labels.address=both"],
        ["view.defaults.labels.address=none"],
        ["view.columns[0]={id: a, root: /}", "view.expand=[flash]"],
        ["view.columns[0]={id: a, root: flash, title: Flash}"],
        ["view.columns[0]={id: a, root: /}", "view.expand=[{region: flash, title: Flash}]"],
        ["style.palette=datasheet", "view.defaults.scale.mode=equal"],
        ["view.columns[0]={id: a, root: /}"],
        ["view.columns[0]={id: a, root: /, bands: 512M}"],
        ["memory.regions[0].sectors={size: 2K, count: 32}"],
        ["view.columns[0].width=150"],
        ["view.overrides=[{region: boot, top_label: end}]"],
        ["view.overrides=[{region: boot, height: 48}]"],
        ['memory.regions[0].note="line one\\nline two"'],
    ],
)
def test_agents_guide_recipes_are_valid(sets, tmp_path):
    file = tmp_path / "layout.yaml"
    file.write_text(
        "version: 1\nmemory:\n  regions:\n    - id: flash\n      start: 0x08000000\n"
        "      size: 64K\n      children:\n        - { id: boot, size: 16K }\n"
        "        - { id: app, size: auto }\nview:\n  columns:\n    - { id: a, root: flash }\n"
    )
    options = [part for assignment in sets for part in ("--set", assignment)]
    result = invoke("check", file, "--strict", *options)
    assert result.exit_code == 0, result.stderr


def test_agents_guide_recipe_works_end_to_end(tmp_path):
    config = tmp_path / "layout.yaml"
    shutil.copy(CUSTOM, config)
    assert json.loads(invoke("check", config, "--json").stdout)["ok"] is True
    assert invoke("describe", config).exit_code == 0
    assert invoke("render", config, "-o", tmp_path / "layout.svg").exit_code == 0
