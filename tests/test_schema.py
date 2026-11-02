import json
from pathlib import Path

import jsonschema
import pytest

from memmap.config import load_yaml
from memmap.schema import Config, json_schema, parse_config

EXAMPLES = sorted((Path(__file__).parent.parent / "examples").glob("*.yaml"))
VALIDATOR = jsonschema.Draft202012Validator(json.loads(json_schema()))


def with_region(**fields):
    return {"version": 1, "memory": {"regions": [{"id": "a", **fields}]}}


def with_height(height):
    config = with_region(size="1K")
    config["view"] = {"overrides": [{"region": "a", "height": height}]}
    return config


# Whole-number floats are left out: JSON Schema counts 1.0 as an integer, memmap rejects it.
SIZES = [
    "16K", "16 K", "16KB", "1_024", "0x10K", " 16K ", "\t16K", "0010", 16,
    "1 6K", "0", "0K", "00", "0x0", "_", "__K", "0x", "0x_", "+16", "16KiB", "", 0, -1, 16.5, True,
]  # fmt: skip
ADDRESSES = [
    "0x0800_0000", "0x0800 0000", "0X10", "16", "\t16", "0", 16, 0,
    "0x", "0x_", "0x ", "_", " ", "", "+16", "-16", "banana", -1, 16.5, True,
]  # fmt: skip
HEIGHTS = ["2x", "1.5x", "0.5x", 48, 0.5, "x", ".x", ".5x", "2.x", "1.2.3x", "0x", "0.0x", 0, -5, True]
RANGES = [[0, 16], ["0x0", "0x10"], [0], [0, 1, 2], "ab", {"a": 1, "b": 2}, 16]


def with_column(**fields):
    return {**with_region(size="1K"), "view": {"columns": [{"id": "c", "root": "/", **fields}]}}


def with_section(section, **fields):
    return {**with_region(size="1K"), section: fields}


TYPED = [
    {**with_region(size="1K"), "version": "1"},
    {**with_region(size="1K"), "version": True},
    with_section("figure", legend=True),
    with_section("figure", legend="yes"),
    with_section("figure", legend=1),
    with_section("figure", title=123),
    with_section("style", frame="no"),
    with_section("style", font_size=11),
    with_section("style", font_size=11.5),
    with_section("style", font_size="11"),
    with_column(width=120),
    with_column(width="120"),
    with_column(depth=2),
    with_column(depth="2"),
    with_column(lane="1"),
    with_column(offset="5"),
    with_column(scale={"min_height": "30"}),
    with_region(size="1K", sectors={"size": "2K", "count": "32"}),
    {"version": 1, "memory": {"check_alignment": "on", "regions": [{"id": "a", "size": "1K"}]}},
]


@pytest.mark.parametrize(
    "config",
    [
        *[with_region(size=value) for value in SIZES],
        *[with_region(size="1K", start=value) for value in ADDRESSES],
        *[with_height(value) for value in HEIGHTS],
        *[with_region(size="1K", sectors=[value]) for value in SIZES],
        *[with_column(range=value) for value in RANGES],
        *TYPED,
    ],
)
def test_schema_and_validator_agree(config):
    assert VALIDATOR.is_valid(config) == (parse_config(config)[0] is not None)


LAYOUT = "version: 1\nmemory:\n  regions:\n    - { id: a, size: 1K }\n"


def test_anchored_bool_stays_a_bool(tmp_path):
    file = tmp_path / "layout.yaml"
    file.write_text(LAYOUT + "figure:\n  legend: &on true\nstyle:\n  frame: *on\n")
    config, _ = parse_config(load_yaml(file).raw)
    assert config is not None
    assert config.figure.legend is True
    assert config.style.frame is True


def test_yaml_yes_is_a_string_and_gets_a_hint(tmp_path):
    file = tmp_path / "layout.yaml"
    file.write_text(LAYOUT + "figure:\n  legend: yes\n")
    _, diagnostics = parse_config(load_yaml(file).raw)
    assert [(item.code, item.hint) for item in diagnostics] == [("E004", "use true or false")]


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda path: path.stem)
def test_schema_and_validator_accept_examples(example):
    raw = load_yaml(example).raw
    assert VALIDATOR.is_valid(raw)
    assert parse_config(raw)[0] is not None


def test_every_field_has_a_description_and_an_example():
    schema = json.loads(json_schema())
    missing = []
    models = {"Config": schema, **schema["$defs"]}
    for model_name, model in models.items():
        for field_name, field in model.get("properties", {}).items():
            if "description" not in field or "examples" not in field:
                missing.append(f"{model_name}.{field_name}")
    assert missing == []


def test_every_model_has_a_description():
    schema = json.loads(json_schema())
    assert "description" in schema
    assert all("description" in model for model in schema["$defs"].values())


def test_schema_forbids_unknown_keys():
    schema = json.loads(json_schema())
    assert schema["additionalProperties"] is False
    assert all(
        model["additionalProperties"] is False
        for model in schema["$defs"].values()
        if "properties" in model
    )


def test_schema_uses_yaml_key_names():
    schema = json.loads(json_schema())
    assert "from" in schema["$defs"]["Expansion"]["properties"]
    assert "from_" not in schema["$defs"]["Expansion"]["properties"]


def test_schema_accepts_expand_shortcut_and_sector_forms():
    text = json_schema()
    expand_items = json.loads(text)["$defs"]["View"]["properties"]["expand"]["items"]
    assert {"type": "string"} in expand_items["anyOf"]
    sectors = json.loads(text)["$defs"]["Region"]["properties"]["sectors"]
    assert "anyOf" in sectors


def test_models_generate_the_schema():
    assert set(Config.model_fields) == {"version", "figure", "memory", "view", "style"}
