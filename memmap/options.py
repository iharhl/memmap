"""Option inheritance: built-in < global < column, with origins."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .schema import Config, Labels, Override, Scale, Style

# Origin of an option that no layer set. `describe` leaves these out of its text report.
BUILTIN = "built-in"


@dataclass
class Options:
    """One column's merged options, or the figure's. `origins` names the layer that set each
    dotted path, and `values` holds that path's value."""

    labels: Labels
    scale: Scale
    style: Style
    origins: dict[str, str]
    values: dict[str, Any]


def _leaves(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts to dotted paths."""
    flat: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            flat.update(_leaves(value, f"{prefix}{key}."))
        else:
            flat[f"{prefix}{key}"] = value
    return flat


def _merge(
    base: dict[str, Any],
    patch: dict[str, Any],
    origin: str,
    origins: dict[str, str],
    prefix: str = "",
) -> None:
    """Merge `patch` into `base`, recording `origin` for every leaf it sets."""
    for key, value in patch.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value, origin, origins, f"{path}.")
        else:
            base[key] = value
            origins[path] = origin


def _patch(model: Any) -> dict[str, Any]:
    """The keys actually present in the file, or nothing for a missing model."""
    return model.model_dump(exclude_unset=True) if model is not None else {}


class OptionResolver:
    """Merges the option layers once per column, and looks up `view.overrides`."""

    def __init__(self, config: Config) -> None:
        """Take the built-in layer from the models' defaults."""
        self.config = config
        self.builtin = {
            "labels": Labels().model_dump(),
            "scale": Scale().model_dump(),
            "style": Style().model_dump(),
        }
        self.cache: dict[str | None, Options] = {}

    def _matching(self, region_id: str, column_id: str) -> Iterator[Override]:
        """The overrides for this region, global or for this column."""
        for override in self.config.view.overrides:
            if override.region == region_id and override.column in (None, column_id):
                yield override

    def options(self, column: Any = None) -> Options:
        """Resolve for a column spec, or for the figure when there is none."""
        column_id = column.id if column is not None else None
        if column_id in self.cache:
            return self.cache[column_id]
        merged = copy.deepcopy(self.builtin)
        origins = {path: BUILTIN for path in _leaves(merged)}
        defaults = self.config.view.defaults

        def layer(section: str, model: Any, origin: str) -> None:
            """Merge one model into a section of the options."""
            _merge(merged[section], _patch(model), origin, origins, f"{section}.")

        layer("style", self.config.style, "style")
        layer("labels", defaults.labels, "view.defaults")
        layer("scale", defaults.scale, "view.defaults")
        if column is not None:
            origin = f"column:{column.id}"
            layer("labels", column.labels, origin)
            layer("scale", column.scale, origin)
            layer("style", column.style, origin)
        result = Options(
            Labels.model_validate(merged["labels"]),
            Scale.model_validate(merged["scale"]),
            Style.model_validate(merged["style"]),
            origins,
            _leaves(merged),
        )
        self.cache[column_id] = result
        return result

    def override_value(self, name: str, region_id: str, column_id: str) -> Any:
        """The most specific non-empty value of an override field such as top_label: one limited
        to the column beats one that is not, then the last in the file wins."""
        value = None
        matching = self._matching(region_id, column_id)
        for override in sorted(matching, key=lambda item: item.column is not None):
            if getattr(override, name) is not None:
                value = getattr(override, name)
        return value
