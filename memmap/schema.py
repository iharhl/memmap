"""Pydantic models of the config. The published JSON Schema is generated from these."""

from __future__ import annotations

import difflib
import json
import math
import re
import typing
from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    WithJsonSchema,
)
from pydantic_core import ErrorDetails, PydanticCustomError

from .diagnostics import Diagnostic, Path
from .units import ADDRESS_PATTERN, SIZE_PATTERN, parse_address, parse_size

# A relative height such as 1.5x. The lookahead rejects a zero one.
_HEIGHT_PATTERN = r"^(?=[0.]*[1-9])[0-9]+(\.[0-9]+)?x$"


def _literal(parser: Callable[[object], object]) -> Callable[[object], object]:
    """Wrap a literal parser so its failure is recognisable as E002."""

    def validate(value: object) -> object:
        """Run `parser`, reporting a bad value as `bad_literal`."""
        try:
            return parser(value)
        except ValueError as exc:
            raise PydanticCustomError("bad_literal", "{reason}", {"reason": str(exc)}) from None

    return validate


def _size_or_auto(value: object) -> object:
    """Parse a size, letting `auto` through."""
    return value if value == "auto" else parse_size(value)


def _height(value: object) -> object:
    """Check a height: a positive finite number, or a relative one like 2x."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"expected a height, got {value!r}")
    if isinstance(value, str):
        if not re.fullmatch(_HEIGHT_PATTERN, value):
            raise ValueError(f"bad height {value!r}, expected pixels or a relative value like 2x")
    elif not 0 < value < math.inf:  # written this way round so that nan fails too
        raise ValueError(f"height must be positive and finite, got {value}")
    return value


def _version(value: object) -> object:
    """Reject booleans. Literal[1] does the rest."""
    if isinstance(value, bool):  # True == 1, so Literal[1] would accept it
        raise PydanticCustomError("literal_error", "expected a schema version number")
    return value


# Written by hand, as the generated schema would describe the parsed value and not what a config
# may hold.
_ADDRESS_SCHEMA = {
    "anyOf": [
        {"type": "integer", "minimum": 0},
        {"type": "string", "pattern": ADDRESS_PATTERN},
    ]
}
_SIZE_SCHEMA = {
    "anyOf": [
        {"type": "integer", "minimum": 1},
        {"type": "string", "pattern": SIZE_PATTERN},
    ]
}
_HEIGHT_SCHEMA = {
    "anyOf": [
        {"type": "number", "exclusiveMinimum": 0},
        {"type": "string", "pattern": _HEIGHT_PATTERN},
    ]
}

# Field types for literals. Each runs its own parser first, as the strict models would reject a
# string where a number is wanted.
Address = Annotated[int, BeforeValidator(_literal(parse_address)), WithJsonSchema(_ADDRESS_SCHEMA)]
Size = Annotated[int, BeforeValidator(_literal(parse_size)), WithJsonSchema(_SIZE_SCHEMA)]
SizeOrAuto = Annotated[
    int | Literal["auto"],
    BeforeValidator(_literal(_size_or_auto)),
    WithJsonSchema({"anyOf": [*_SIZE_SCHEMA["anyOf"], {"const": "auto"}]}),
]
Height = Annotated[
    float | str,
    BeforeValidator(_literal(_height)),
    WithJsonSchema(_HEIGHT_SCHEMA),
]

# Keep in step with SCHEMA_VERSIONS.
Version = Annotated[Literal[1], BeforeValidator(_version)]


class Model(BaseModel):
    """Base of every config model: strict types, and unknown keys are errors (E001)."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


# Figure


class Figure(Model):
    """Text around the drawing."""

    title: str | None = Field(
        default=None,
        description="Title above the drawing.",
        examples=["Figure 1. Custom memory map"],
    )
    footer: str | None = Field(
        default=None, description="Small ID text at the bottom right.", examples=["MM-0001"]
    )
    legend: bool = Field(
        default=False, description="Draw a legend for reserved areas.", examples=[True]
    )


# Memory


class SectorRun(Model):
    """A run of equal flash erase sectors."""

    size: Size = Field(description="Size of one sector.", examples=["2K"])
    count: int = Field(default=1, ge=1, description="Number of sectors in the run.", examples=[32])


def _sectors(value: object) -> object:
    """Normalise the `sectors` shorthands to a list of run mappings."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item if isinstance(item, dict) else {"size": item} for item in value]
    return value


# One run, or a list of runs and bare sizes. Always a list of runs once parsed.
Sectors = Annotated[
    list[SectorRun],
    BeforeValidator(
        _sectors,
        json_schema_input_type=SectorRun | list[SectorRun | Size],
    ),
]


class Region(Model):
    """One memory region. Unspecified addresses are placed sequentially."""

    id: str = Field(
        pattern=r"^[a-z0-9_]+$",
        description="Unique id across the whole tree, used by views.",
        examples=["bootloader"],
    )
    name: str | None = Field(
        default=None,
        description="Display text, \\n forces a line break. Defaults to the id.",
        examples=["Flash memory"],
    )
    start: Address | None = Field(
        default=None,
        description="First address. Omitted means right after the previous sibling.",
        examples=["0x0800_0000"],
    )
    end: Address | None = Field(
        default=None,
        description="Last address, inclusive. Give end or size.",
        examples=["0x0800_FFFF"],
    )
    size: SizeOrAuto | None = Field(
        default=None,
        description="Size. `auto` fills to the next sibling's start or the parent's end.",
        examples=["16K", "auto"],
    )
    align: Literal["start", "end"] = Field(
        default="start",
        description="`end` pins the region to the top of its parent; several such regions stack.",
        examples=["end"],
    )
    kind: Literal["normal", "reserved"] = Field(
        default="normal", description="`reserved` uses the reserved style.", examples=["reserved"]
    )
    color: str | None = Field(default=None, description="Fill override.", examples=["#81b29a"])
    note: str | None = Field(
        default=None,
        description="Small text right of the block. Long text wraps, \\n forces a line break.",
        examples=["2 pages, A/B copy"],
    )
    sectors: Sectors | None = Field(
        default=None,
        description="Flash erase sectors: enables alignment checks and tick marks.",
        examples=[{"size": "2K", "count": 32}, ["16K", "16K", "64K"]],
    )
    children: list[Region] = Field(
        default_factory=list,
        description="Sub-regions, placed inside this region.",
        examples=[[{"id": "boot", "size": "16K"}]],
    )


class Memory(Model):
    """What the memory is."""

    address_bits: Literal[32, 64] = Field(
        default=32, description="Address width, sets the hex digit count.", examples=[32]
    )
    fill_gaps: Literal["reserved", "none", "error"] = Field(
        default="reserved",
        description=(
            "What to do with unspecified space inside a region. `error` does not apply at the "
            "top level, where gaps are simply unused address space."
        ),
        examples=["reserved"],
    )
    check_alignment: bool = Field(
        default=False,
        description="Warn (W103) when a region start is not aligned to its own size.",
        examples=[True],
    )
    regions: list[Region] = Field(
        min_length=1,
        description="Top-level regions.",
        examples=[[{"id": "flash", "start": "0x08000000", "size": "64K"}]],
    )


# Display options. Fields carry their built-in default; only keys present in the file take part
# in option inheritance (see options.py).


class Labels(Model):
    """Address and size labels."""

    address: Literal["start", "both", "none"] = Field(
        default="start",
        description=(
            "What an internal edge prints: the boundary, or boundary and previous end. An edge "
            "with unfilled space at it always prints both. none prints no addresses, except on "
            "edges with a top_label override."
        ),
        examples=["both"],
    )
    size: bool = Field(
        default=False, description="Print the size of normal blocks.", examples=[True]
    )


class Scale(Model):
    """Block heights."""

    mode: Literal["weighted", "equal", "linear"] = Field(
        default="weighted",
        description=(
            "weighted: grows with log2(size). equal: all blocks min_height. linear: proportional "
            "to size. A view.overrides height wins in every mode."
        ),
        examples=["linear"],
    )
    min_height: float = Field(
        default=24, gt=0, description="Smallest block height, px.", examples=[30]
    )
    max_height: float | None = Field(
        default=None, gt=0, description="Largest scaled block height, px.", examples=[120]
    )
    collapse_reserved_over: Size | None = Field(
        default=None,
        description="Draw larger reserved blocks at min_height with a break mark.",
        examples=["256M"],
    )


class ReservedStyle(Model):
    """Reserved block look."""

    fill: str = Field(default="#d9d9d9", description="Fill colour.", examples=["#d9d9d9"])
    show_label: bool = Field(
        default=False, description="Print a label in the block.", examples=[True]
    )
    label: str = Field(default="Reserved", description="Default label text.", examples=["Reserved"])


class Style(Model):
    """Figure-wide look."""

    font_size: float = Field(default=11, gt=0, description="Block text size, px.", examples=[11])
    reserved: ReservedStyle = Field(
        default_factory=ReservedStyle,
        description="Reserved blocks.",
        examples=[{"show_label": True}],
    )
    frame: bool = Field(
        default=True, description="Draw a frame around the figure.", examples=[True]
    )
    palette: Literal["datasheet", "color"] = Field(
        default="datasheet",
        description="datasheet is black, white and grey; color picks a fill per region.",
        examples=["color"],
    )
    sector_ticks: bool = Field(
        default=True, description="Draw tick marks at flash sector boundaries.", examples=[False]
    )


class ColumnStyle(Model):
    """The part of the style a column can change."""

    reserved: ReservedStyle = Field(
        default_factory=ReservedStyle,
        description="Reserved blocks.",
        examples=[{"show_label": False}],
    )


# View


class Defaults(Model):
    """View-wide display options."""

    labels: Labels | None = Field(
        default=None, description="Label defaults.", examples=[{"address": "both"}]
    )
    scale: Scale | None = Field(
        default=None, description="Scale defaults.", examples=[{"mode": "equal"}]
    )


class Column(Model):
    """A stack of the direct children of one region."""

    id: str = Field(description="Column id.", examples=["main"])
    root: str = Field(description="Region whose children are drawn, or `/`.", examples=["/", "flash"])
    title: str | None = Field(
        default=None,
        description="Title above the column, \\n forces a line break.",
        examples=["Flash"],
    )
    depth: int = Field(
        default=1, ge=1, description="Levels flattened into the stack.", examples=[2]
    )
    range: tuple[Address, Address] | None = Field(
        default=None,
        strict=False,  # YAML gives a list
        description="Draw only part of the root.",
        examples=[["0x08000000", "0x0800FFFF"]],
    )
    bands: Size | None = Field(
        default=None,
        description="Split into equal numbered bands, labelling only band boundaries.",
        examples=["512M"],
    )
    width: float = Field(default=120, gt=0, description="Block width, px.", examples=[120])
    lane: int | None = Field(
        default=None,
        ge=0,
        description="Horizontal lane, 0 = leftmost. Omit for auto.",
        examples=[1],
    )
    anchor: Literal["source", "top", "bottom", "free"] = Field(
        default="source",
        description=(
            "source centres on the expanded block; top and bottom align with the drawing's top "
            "or bottom; free places the column at `offset` from the top, ignoring collisions."
        ),
        examples=["top"],
    )
    offset: float = Field(default=0, description="Extra vertical shift, px.", examples=[40])
    labels: Labels | None = Field(
        default=None, description="Label overrides.", examples=[{"address": "both"}]
    )
    scale: Scale | None = Field(
        default=None, description="Scale overrides.", examples=[{"mode": "equal"}]
    )
    style: ColumnStyle | None = Field(
        default=None, description="Style overrides.", examples=[{"reserved": {"show_label": False}}]
    )


class Expansion(Model):
    """A zoom from a block in one column to a detail column."""

    region: str = Field(description="The block being expanded.", examples=["periph"])
    from_: str | None = Field(
        default=None,
        alias="from",
        description="Column where the block is drawn. Omit to find it.",
        examples=["main"],
    )
    to: str | None = Field(
        default=None,
        description="Detail column, its root must equal region. Omit to create one.",
        examples=["periph_detail"],
    )
    title: str | None = Field(
        default=None,
        description="Title above the detail column. Takes precedence over the column's own.",
        examples=["Peripherals"],
    )


def _expansion(value: object) -> object:
    """Expand a bare region id to a mapping."""
    return {"region": value} if isinstance(value, str) else value


# A bare region id is shorthand for an expansion with only `region` set.
ExpandEntry = Annotated[
    Expansion, BeforeValidator(_expansion, json_schema_input_type=str | Expansion)
]


class Override(Model):
    """A tweak to one region's block."""

    region: str = Field(description="Region this applies to.", examples=["ahb1"])
    column: str | None = Field(
        default=None, description="Limit to one column.", examples=["periph_detail"]
    )
    top_label: Literal["start_of_next", "end"] | None = Field(
        default=None, description="Label on the region's top edge.", examples=["end"]
    )
    height: Height | None = Field(
        default=None,
        description="Block height: px, or relative to min_height.",
        examples=[48, "2x"],
    )


class View(Model):
    """How to draw the memory."""

    defaults: Defaults = Field(
        default_factory=Defaults,
        description="Defaults for all columns.",
        examples=[{"labels": {"address": "both"}}],
    )
    columns: list[Column] = Field(
        default_factory=list,
        description="Columns. Empty means one column of the whole address space.",
        examples=[[{"id": "main", "root": "/"}]],
    )
    expand: list[ExpandEntry] = Field(
        default_factory=list,
        description="Expansions. A plain region id creates a detail column in the next lane.",
        examples=[["periph", {"region": "code", "from": "main", "to": "code_detail"}]],
    )
    overrides: list[Override] = Field(
        default_factory=list,
        description="Per-region tweaks.",
        examples=[[{"region": "ahb1", "top_label": "end"}]],
    )


class Config(Model):
    """A memmap figure: what the memory is (`memory`) and how to draw it (`view`)."""

    version: Version = Field(description="Schema version.", examples=[1])
    figure: Figure = Field(
        default_factory=Figure, description="Figure text.", examples=[{"title": "Memory map"}]
    )
    memory: Memory = Field(
        description="The memory.", examples=[{"regions": [{"id": "flash", "size": "64K"}]}]
    )
    view: View = Field(
        default_factory=View,
        description="The view.",
        examples=[{"columns": [{"id": "main", "root": "/"}]}],
    )
    style: Style = Field(
        default_factory=Style, description="Global style.", examples=[{"palette": "color"}]
    )


def json_schema() -> str:
    """The config's JSON Schema, as the text `memmap schema` prints."""
    schema = Config.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return json.dumps(schema, indent=2, sort_keys=True)


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    """The model inside a field's annotation, looking through unions and lists."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for argument in typing.get_args(annotation):
        found = _nested_model(argument)
        if found:
            return found
    return None


def _keys_at(location: Path) -> list[str]:
    """Valid keys of the model reached by walking `location` from Config."""
    model: type[BaseModel] = Config
    for part in location:
        if isinstance(part, int):
            continue
        fields = {field.alias or name: field for name, field in model.model_fields.items()}
        if part not in fields:
            return []
        nested = _nested_model(fields[part].annotation)
        if nested is None:
            return []
        model = nested
    return [field.alias or name for name, field in model.model_fields.items()]


def _diagnostic(error: ErrorDetails) -> Diagnostic:
    """Turn a pydantic error into a diagnostic with a stable code."""
    location = tuple(error["loc"])
    kind = error["type"]
    if kind == "extra_forbidden":
        key = str(location[-1])
        known = _keys_at(location[:-1])
        close = difflib.get_close_matches(key, known, n=1)
        hint: str | None
        if close:
            hint = f"did you mean '{close[0]}'?"
        else:
            hint = f"valid keys: {', '.join(sorted(known))}" if known else None
        return Diagnostic("E001", f"unknown key '{key}'", location, hint)
    if kind == "bad_literal":
        return Diagnostic("E002", error["msg"], location)
    if kind == "literal_error" and location == ("version",):
        return Diagnostic(
            "E005",
            f"unsupported schema version {error['input']!r}",
            location,
            "this memmap supports version 1; set `version: 1` and check `memmap schema`",
        )
    if kind == "missing":
        return Diagnostic(
            "E004",
            f"missing required key '{location[-1]}'",
            location,
            "run `memmap schema` for the list of keys",
        )
    if kind == "bool_type":
        return Diagnostic("E004", error["msg"], location, "use true or false")
    return Diagnostic("E004", error["msg"], location)


def parse_config(raw: dict[str, Any]) -> tuple[Config | None, list[Diagnostic]]:
    """Validate the raw config. Returns the model, or None and the diagnostics."""
    try:
        return Config.model_validate(raw), []
    except ValidationError as exc:
        return None, [_diagnostic(error) for error in exc.errors()]
