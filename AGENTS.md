# memmap: agent guide

memmap turns a YAML memory layout into a datasheet-style memory map. You usually cannot look at
the image, so check your work with `check` and `describe`, which report everything as text.

Run commands as `memmap ...` when it is installed, or `uv run memmap ...` inside this repo.
`memmap guide` prints this file.

## Typical task: draw a memory layout
1. Copy the closest config from the repo's `examples/` folder to `layout.yaml`, such as
   `custom_bootloader.yaml`. Without a checkout, write one from `memmap schema`.
2. Edit `memory.regions` (sizes like 16K; omit `start` to place sequentially).
3. `memmap check layout.yaml --json --strict` -> fix every diagnostic using its `hint`.
4. `memmap describe layout.yaml` -> confirm ranges and labels match intent.
5. `memmap render layout.yaml -o layout.svg --json --strict`

## Rules
- Never guess keys; run `memmap schema` when unsure.
- Ranges are inclusive: end = start + size - 1.
- Prefer `size` over `end`, and sequential placement over explicit `start`.
- Display tweaks go in `view`, never by changing `memory`.
- The look is fixed on purpose: font family, address format, outlines, connectors and label side
  have no keys.
- Use `--strict` on `check` and `render` to fail on warnings. Without it they exit 0 with
  warnings, and a flash sector misalignment (W100) or colliding text (W106) is a warning.
- PNG and PDF need the `raster` extra and the system cairo library. Without cairo, `render`
  exits 3. SVG always works.

## Recipes
- Show sizes:            view.defaults.labels.size: true
- Boundary + prev end:   view.defaults.labels.address: both
- Zoom into a region:    view.expand: [region_id]
- Title a column:        view.columns: [{ id: a, root: flash, title: Flash }]
- Title a zoom:          view.expand: [{ region: flash, title: Flash }]
- Datasheet look:        style.palette: datasheet, view.defaults.scale.mode: equal
- Whole address space:   a column with root: "/", or no columns at all
- Numbered overview:     a column with root: "/", bands: 512M
- Flash pages:           sectors: { size: 2K, count: 32 } on the flash region
- One-off change:        memmap render layout.yaml --set 'view.columns[0].width=150'
- Label one edge:        view.overrides: [{ region: ahb1, top_label: end }]
- Block height:          view.overrides: [{ region: boot, height: 48 }]

Quote `--set` values that contain `[` or `{`, since shells expand them.

## JSON output
With `--json`, `check` and `render` always print one object on stdout, for validation errors
and for usage and IO failures alike: `{"ok": ..., "diagnostics": [...]}`. `render` adds `output`
and `format` on success. `describe --json` prints the same object on failure. Only a mistake in
the command line itself, such as an unknown flag, is reported as plain text on stderr.

## Checking the result
`memmap describe layout.yaml --json` returns:
- `regions`: the resolved tree. Generated gaps have `generated: true`.
- `blocks`: every block with `id`, `column`, `start`, `end`, `size` and pixel `box`.
- `labels`: every string drawn, with `role` (address, name, size, note, band, title,
  column_title, footer, legend), `column` and an approximate pixel `box`.
- `columns`: placement, plus every option with its value and its `origin`.

Output is deterministic: the same input gives a byte-identical SVG, so diffing is reliable.

W105 and W106 catch text that does not fit, using a built-in width table, so they are close but
not exact. Connector lines are not checked against text.

## Diagnostics
Every diagnostic has a stable code, a YAML path, a line and usually a `hint`. In JSON it also has
`subject` (the region or column id) and `details` (extra lines, such as the ranges that overlap).

| Code | Meaning |
| --- | --- |
| E000 | Invalid YAML |
| E001 | Unknown key |
| E002 | Bad address or size literal |
| E003 | Duplicate region id |
| E004 | Missing key or invalid value |
| E005 | Unsupported schema version |
| E010 | Child outside parent, or column range outside its root |
| E011 | Siblings overlap |
| E012 | `end` before `start`, or `size`, `end` and `align` conflict |
| E013 | More than one `auto` size, or nothing left for `auto` |
| E014 | Gap inside a region with `fill_gaps: error` |
| E015 | Too many flash sectors |
| E020 | Column root, expansion or override target does not exist |
| E021 | Expansion `to` column has a different root than `region` |
| E022 | Expanded region is not drawn in the `from` column |
| E023 | Duplicate column id |
| E024 | Too many bands |
| E025 | Column expands into itself |
| W100 | Region boundary not on a flash sector boundary |
| W101 | Gap filled as reserved (only with `--verbose`) |
| W102 | Label text forced a block over `max_height` |
| W103 | Region start not aligned to its size (needs `memory.check_alignment: true`) |
| W104 | Sector list does not add up to the region size |
| W105 | Block name was split inside a word to fit the column |
| W106 | Text overlaps other text, or is wider than its block |
| E090 | Usage or IO failure, such as a missing file or bad `--set` (`--json` only) |
| E091 | Render or internal failure, such as missing cairo (`--json` only) |

Exit codes: `0` ok, `1` validation errors, `2` usage or IO error, `3` render error.

## Working on memmap
- `uv run pytest`, `uv run mypy` and `uv run ruff check` must all pass. CI runs them on Python
  3.10 and the latest supported version.
- After an intended drawing change, review the SVG diff, then run
  `MEMMAP_UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py`.
- A new diagnostic code or recipe goes in this file too. `memmap guide` ships it to users, and
  `tests/test_cli.py` covers the recipes.
