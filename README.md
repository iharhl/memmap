# memmap

Turn a YAML description of an MCU memory layout into a datasheet-style memory map image. The look
follows the STM32 reference manual figures, for your own layouts: bootloaders, app slots, config
sectors, shared RAM.

- One YAML file holds the memory (`memory`) and how to draw it (`view`), kept separate.
- Validation catches overlaps, overflow and flash sector misalignment, with YAML path, line and hint.
- Output is deterministic, and `describe` lists everything drawn, so agents and CI can verify it
  as text.

Below find an example with four memory maps drawn by **memmap**.

![](docs/examples.svg)

## Install

Each [GitHub release](https://github.com/iharhl/memmap/releases) has a wheel attached. To install
it with Python 3.10 or newer and no uv or checkout, using the version you want:

```bash
pipx install https://github.com/iharhl/memmap/releases/download/v0.2.0/memmap-0.2.0-py3-none-any.whl
```

To work from a checkout:

```bash
uv sync
```

PNG and PDF output needs the `raster` extra and the system cairo library:

```bash
uv sync --extra raster
```

On macOS with Homebrew's cairo, the library is not on the default search path, so set this before
rendering PNG or PDF:

```bash
export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib
```

## Use

Start from the closest config in [examples/](examples/), listed under [Examples](#examples):

```bash
cp examples/custom_bootloader.yaml layout.yaml
```

```bash
uv run memmap check layout.yaml
```

```bash
uv run memmap describe layout.yaml
```

```bash
uv run memmap render layout.yaml -o layout.svg
```

| Command | Purpose |
| --- | --- |
| `memmap render CONFIG [-o OUT] [--format svg\|png\|pdf] [--json] [--strict] [--set key=value]` | Validate, then draw; `--strict` fails on warnings |
| `memmap check CONFIG [--json] [--strict]` | Validate only; `--strict` fails on warnings |
| `memmap describe CONFIG [--json]` | Resolved regions, blocks, labels and option origins |
| `memmap from-elf ELF [-o OUT] [--region id=start:size[:name]] [--min-size N] [--force] [--json] [--strict]` | Write a config from a linked ELF's sections, then check it |
| `memmap schema` | JSON Schema of the config |
| `memmap guide` | Print the agent guide, for agents using an installed copy |

### From an ELF file

`from-elf` writes a config from the allocated sections of a linked firmware image: `.text`,
`.data`, `.bss` and so on, each at its real address and size. The ELF does not record how big
flash and RAM are, so give them with `--region`, copying `ORIGIN` and `LENGTH` from the
linker script's `MEMORY` block, or from the `Memory Configuration` table of a GNU ld map file:

```bash
uv run memmap from-elf build/firmware.elf -o layout.yaml --region flash=0x08000000:64K --region ram=0x20000000:20K
```

You get one column per region, titled with how much of it is used:

- Initialised data is drawn twice: in RAM where it runs, and in flash where startup code copies
  it from. Each loadable segment is one flash block: `.data (load)`, with a note such as
  `copied to RAM at 0x2000 0000`, or `RAM init data` when it holds several sections, as Zephyr's
  do (`code and data` when it includes code).
- Gaps of at least `--min-size` (default 64 bytes) become Free blocks with their size, rounded
  down and marked `≈` when not exact.
- In a region of more than 8 blocks, runs of smaller sections fold into one block that holds
  them as children, and each such group gets a detail column beside it. Zooms go one level
  deep: when sections outside every `--region` put the regions in a zoom of their own, their
  groups list the section names in a note instead.
- Without `--region`, sections are drawn on the whole address space. Object files converted
  from a `.hex` or `.bin` image, which keep their real addresses, are read too.

The output is an ordinary config: edit it, then `render` it.

A minimal config:

```yaml
version: 1
figure: { title: "Flash layout" }
memory:
  regions:
    - id: flash
      start: 0x08000000
      size: 64K
      sectors: { size: 2K, count: 32 }
      children:
        - { id: boot, name: Bootloader, size: 16K }
        - { id: app, name: Application, size: auto }
view:
  defaults:
    labels:
      address: both
      size: true
    scale: { mode: linear }
  columns:
    - { id: flash_col, root: flash, title: Flash }
```

Columns take an optional `title`, drawn above them. For a detail column created by `view.expand`,
set it on the expansion: `expand: [{ region: flash, title: Flash }]`.

## Learn more

- [AGENTS.md](AGENTS.md): task guide, recipes and the full diagnostic list. `memmap guide` prints it.
- [examples/](examples/): see the table below.
- `memmap schema`: every key with a description and an example. Point your editor's YAML extension
  at it for completion.

## Examples

| Example | Shows |
| --- | --- |
| [custom_bootloader.yaml](examples/custom_bootloader.yaml) | Sequential placement, `auto` and `align: end`, sector checks, linear scale, colour palette |
| [stm32f303_fig8.yaml](examples/stm32f303_fig8.yaml) | Datasheet replica: numbered bands, zooms anchored to the top and bottom, `top_label`, legend |
| [stm32f051_fig10.yaml](examples/stm32f051_fig10.yaml) | Datasheet replica: reserved labels, staggered zooms with `anchor: free` and `offset` |
| [stm32f407_ota.yaml](examples/stm32f407_ota.yaml) | Mixed sector sizes, `fill_gaps: error`, weighted scale with `max_height`, notes |
| [nrf52840_boot_chain.yaml](examples/nrf52840_boot_chain.yaml) | Chained zooms, column `range`, per-column labels and scale, `start_of_next` |
| [riscv64_soc.yaml](examples/riscv64_soc.yaml) | 64-bit addresses, `collapse_reserved_over`, relative heights, style options |
| [rtos_mpu_ram.yaml](examples/rtos_mpu_ram.yaml) | `check_alignment`, `fill_gaps: none`, `depth`, two columns of one memory |

To render them all into `build/`:

```bash
mkdir -p build
```

```bash
for f in examples/*.yaml; do uv run memmap render "$f" -o "build/$(basename "${f%.yaml}").svg"; done
```

## Develop

```bash
uv run pytest
```

Check types and style. mypy finds type errors, ruff finds missing annotations and lint issues:

```bash
uv run mypy
```

```bash
uv run ruff check
```

Golden SVGs live in `tests/golden/`. After an intended drawing change, review the diff and
regenerate them:

```bash
MEMMAP_UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py
```

`from-elf` is also tested on eight real firmware images, kept as small header snapshots with
golden configs and drawings in `tests/golden/elf/`. After an intended change to the importer or
the drawing, review and regenerate those too:

```bash
MEMMAP_UPDATE_GOLDEN=1 uv run pytest tests/test_elf_snapshots.py
```

[docs/elf-testing.md](docs/elf-testing.md) explains the approach, lists the images and shows
their maps.

Text is measured with a built-in Helvetica/Arial width table, so wrapping is slightly approximate
for other fonts.

To cut a release, see [RELEASING.md](RELEASING.md).
