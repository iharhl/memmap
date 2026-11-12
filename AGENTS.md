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

## Typical task: map a linked firmware image
1. The ELF does not say how big flash and RAM are. Find the map file the linker wrote:

   | Toolchain | Map | Found in |
   | --- | --- | --- |
   | GNU ld: gcc, Zephyr, ESP-IDF, Pico SDK | `firmware.map` (`-Wl,-Map=`) | next to the ELF |
   | Arm armlink: Keil MDK | `project.map` | `Listings/` |
   | IAR ILINK | `project.map` | `List/` |

   lld (Rust, LLVM Embedded) maps list no regions, and memmap does not read linker scripts:
   give each region with `--region flash=0x08000000:64K`, copying `ORIGIN` and `LENGTH` from
   the script's `MEMORY` block.
2. `memmap from-elf firmware.elf -o layout.yaml --map firmware.map --json --strict` -> writes
   the config, then checks it like `check`. A `--region` with the id of a map region replaces
   it, and one with a new id adds a region. To split a map region, give one part its id.
3. When the map gives no region it can draw, `from-elf` writes nothing and exits 2 with E090.
   Its `hint` names the `--region` specs to give, such as `--region er_ro=0x08000000:SIZE`:
   fill in the sizes and run it again.
4. Fix W112 (a map region left out) and W110 (a section outside every region) by their hints:
   give the `--region` the hint names, check that the map belongs to this ELF, or add a
   `--region` that covers the section.
5. Continue from step 4 of the first task. Rename or merge blocks in `layout.yaml` as needed.
   `--force` overwrites an existing output.

Regions take their names from the map: FLASH gets the id `flash`. IAR maps do not name their
regions, so they are called ROM, RAM or Memory by what is placed there, numbered in address
order (`ram`, `ram_2`). A region that holds no section stays in `memory` without a column. One
without a size, of size 0, past the address space, overlapping a lower or larger region, or
too small for a section is left out, with W112 when the drawing loses something. Comments in
`layout.yaml` list both. https://github.com/iharhl/memmap/blob/main/docs/from-elf.md has the
details and limits.

In a region of more than 8 blocks, small sections fold into groups such as `small_sections`,
which keep the sections as `children` and each get a detail column. Zooms go one level deep, so
when W110 puts the regions in zooms of their own, their groups get a note instead.
`--min-size 0` turns folding off.

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
- Hide addresses:        view.defaults.labels.address: none
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
- Multi-line note:       note: "line one\nline two" (long notes also wrap)

Quote `--set` values that contain `[` or `{`, since shells expand them.

## JSON output
With `--json`, `check`, `render` and `from-elf` always print one object on stdout, for
validation errors and for usage and IO failures alike: `{"ok": ..., "diagnostics": [...]}`.
`render` adds `output` and `format` on success, `from-elf` adds `output` once it has written the
config. `describe --json` prints the same object on failure. Only a mistake in the command line
itself, such as an unknown flag, is reported as plain text on stderr.

## Checking the result
`memmap describe layout.yaml --json` returns:
- `regions`: the resolved tree. Generated gaps have `generated: true`.
- `blocks`: every block with `id`, `column`, `start`, `end`, `size` and pixel `box`.
- `labels`: every string drawn, with `role` (address, name, size, note, band, title,
  column_title, footer, legend), `column` and an approximate pixel `box`.
- `columns`: placement, plus every option with its value and its `origin`.
- `connectors`: one per zoom, with its `region`, `from` and `to` columns and the points of its
  two fan `lines`.

Output is deterministic: the same input gives a byte-identical SVG, so diffing is reliable.

W105 and W106 catch text that does not fit, using a built-in width table, so they are close but
not exact. Connector lines are not checked against text, but they are kept out of columns:
a zoom whose detail column is two or more lanes to the right of its source moves that column up
or down so its fan lines bend around the columns between, and W107 reports when it cannot. One
to the left is not moved, and gets W107 when its lines cross a column. Detail columns stacked in
one lane go in the order of the blocks they zoom into, highest address on top. A column that
zooms into columns that stack up taller than it is gets stretched to their height, up to 5
times its own, so its fan lines do not start crammed together. A stack of zooms that would run
past the bottom of the drawing moves up, unless a column in it has an `offset`, and columns
stacked in one lane keep room between them for both end addresses.

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
| W102 | Label or note text forced a block over `max_height` |
| W103 | Region start not aligned to its size (needs `memory.check_alignment: true`) |
| W104 | Sector list does not add up to the region size |
| W105 | Block name was split inside a word to fit the column |
| W106 | Text overlaps other text, or is wider than its block |
| W107 | Fan lines run through a column between a zoom's source and its detail column |
| W110 | `from-elf`: section outside every memory region, placed at the top level |
| W111 | `from-elf`: section overlaps another one, left out |
| W112 | `from-elf`: map region left out (no size, past the address space, overlap, too small) |
| E090 | Usage or IO failure, such as a missing file or bad `--set` (`--json` only) |
| E091 | Render or internal failure, such as missing cairo (`--json` only) |

Exit codes: `0` ok, `1` validation errors, `2` usage or IO error, `3` render error.

## Working on memmap
- `uv run pytest`, `uv run mypy` and `uv run ruff check` must all pass. CI runs them on Python
  3.10 and the latest supported version.
- After an intended drawing change, review the SVG diff, then run
  `MEMMAP_UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py`.
- After an intended change to `from-elf` or the drawing, also review and regenerate the
  real-firmware goldens: `MEMMAP_UPDATE_GOLDEN=1 uv run pytest tests/test_elf_snapshots.py
  tests/test_linker.py`. docs/from-elf.md describes them and how to add an image or a
  map.
- A new diagnostic code or recipe goes in this file too. `memmap guide` ships it to users, and
  `tests/test_cli.py` covers the recipes.
