"""Command line interface. Every command is non-interactive."""

from __future__ import annotations

import json
from collections.abc import Iterable
from enum import Enum
from pathlib import Path

import click
from click.exceptions import Exit

from . import SCHEMA_VERSIONS, __version__
from .config import ConfigError
from .describe import describe_data, describe_text
from .diagnostics import Diagnostic
from .elf import ElfError, read_elf
from .from_elf import ImportProblem, import_elf, parse_region_spec
from .pipeline import Result, run
from .render.raster import RasterUnavailable, convert
from .render.svg import render_svg
from .schema import json_schema
from .units import parse_size

# Process exit codes, documented in AGENTS.md. Success is 0.
EXIT_VALIDATION = 1
EXIT_USAGE = 2
EXIT_RENDER = 3

# With --json, failures outside the config are reported as diagnostics under these codes.
FAILURE_CODES = {EXIT_USAGE: "E090", EXIT_RENDER: "E091"}

# %(prog)s and %(version)s are left for click to fill in.
VERSION_MESSAGE = "%(prog)s %(version)s (schema versions: {})".format(
    ", ".join(str(version) for version in SCHEMA_VERSIONS)
)


class OrderedGroup(click.Group):
    """Lists commands in the order they are defined, not alphabetically."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        """The commands, in definition order."""
        return list(self.commands)


@click.group(name="memmap", cls=OrderedGroup, no_args_is_help=True)
@click.version_option(
    __version__, prog_name="memmap", message=VERSION_MESSAGE, help="Show the version."
)
def app() -> None:
    """Turn a YAML memory layout into a datasheet-style memory map image.

    Agents: run `memmap guide` first.
    """


# Shared by the commands that read a config.
config_argument = click.argument("config", type=click.Path(path_type=Path))
set_option = click.option(
    "--set", "sets", multiple=True, help="Override any key: key.path=value (repeatable)."
)
verbose_option = click.option(
    "--verbose", is_flag=True, help="Also report gaps filled as reserved (W101)."
)
json_option = click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
strict_option = click.option("--strict", is_flag=True, help="Treat warnings as errors.")


class OutputFormat(str, Enum):
    """What `render` can write. PNG and PDF need the `raster` extra."""

    svg = "svg"
    png = "png"
    pdf = "pdf"


def _fail(
    message: str, code: int, as_json: bool = False, result: Result | None = None
) -> Exit:
    """Report a failure and return the Exit to raise. With `as_json` the message joins
    `result`'s diagnostics, under the code FAILURE_CODES gives for `code`."""
    if as_json:
        result = result or Result()
        result.diagnostics.append(Diagnostic(FAILURE_CODES[code], message))
        _print_json(False, result)
    else:
        click.echo(f"memmap: {message}", err=True)
    return Exit(code)


def _load(
    config: Path, sets: Iterable[str], verbose: bool = False, as_json: bool = False
) -> Result:
    """Run the pipeline, turning a usage problem or a crash into the matching exit."""
    try:
        return run(config, sets, verbose)
    except ConfigError as exc:
        raise _fail(str(exc), EXIT_USAGE, as_json)
    except Exception as exc:  # noqa: BLE001  # any crash must still exit 3 with a message
        raise _fail(f"internal error: {exc}", EXIT_RENDER, as_json)


def _passes(result: Result, strict: bool) -> bool:
    """Whether the run counts as a success. `strict` also fails it on warnings."""
    return result.ok and not (strict and result.diagnostics)


def _print_diagnostics(result: Result, config: Path) -> None:
    """Print each diagnostic as text on stderr."""
    for diagnostic in result.diagnostics:
        click.echo(diagnostic.to_text(config.name), err=True)


def _print_json(ok: bool, result: Result, **extra: str) -> None:
    """Print the JSON result object, with `extra` as more top-level keys."""
    diagnostics = [d.to_json() for d in result.diagnostics]
    click.echo(json.dumps({"ok": ok, **extra, "diagnostics": diagnostics}, indent=2))


def _output_target(
    config: Path, output: Path | None, fmt: OutputFormat | None, as_json: bool
) -> tuple[Path, OutputFormat]:
    """The path and format to write. Without -o the file goes next to the config, as svg
    unless --format says otherwise; with -o and no --format its extension picks."""
    if output is None:
        chosen = fmt or OutputFormat.svg
        return config.with_suffix(f".{chosen.value}"), chosen
    if fmt is not None:
        return output, fmt
    try:
        return output, OutputFormat(output.suffix.lstrip(".").lower())
    except ValueError:
        raise _fail(f"cannot infer a format from {output}, use --format", EXIT_USAGE, as_json)


def _write(path: Path, data: bytes, as_json: bool = False) -> None:
    """Write the output file, turning an OS error into a usage failure."""
    try:
        path.write_bytes(data)
    except OSError as exc:
        raise _fail(f"cannot write {path}: {exc}", EXIT_USAGE, as_json)


@app.command(
    epilog="\b\nExample:\n  memmap render layout.yaml -o layout.svg --set view.defaults.labels.size=true"
)
@config_argument
@click.option("-o", "--output", type=click.Path(path_type=Path), help="Output file.")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(OutputFormat),
    help="Output format. Default: from the -o extension, else svg.",
)
@json_option
@strict_option
@set_option
@verbose_option
def render(
    config: Path,
    output: Path | None,
    fmt: OutputFormat | None,
    as_json: bool,
    strict: bool,
    sets: tuple[str, ...],
    verbose: bool,
) -> None:
    """Validate a config, then draw it.

    CONFIG is the YAML config file.
    """
    target, chosen = _output_target(config, output, fmt, as_json)
    if target.resolve() == config.resolve():
        raise _fail(f"output {target} would overwrite the config", EXIT_USAGE, as_json)
    result = _load(config, sets, verbose, as_json)
    if as_json and not _passes(result, strict):
        _print_json(False, result)
    elif not as_json:
        _print_diagnostics(result, config)
    if not _passes(result, strict):
        raise Exit(EXIT_VALIDATION)
    try:
        assert result.drawing is not None
        svg = render_svg(result.drawing)
        data = svg.encode("utf-8") if chosen is OutputFormat.svg else convert(svg, chosen.value)
    except RasterUnavailable as exc:
        raise _fail(str(exc), EXIT_RENDER, as_json, result)
    except Exception as exc:  # noqa: BLE001  # any crash must still exit 3 with a message
        raise _fail(f"render failed: {exc}", EXIT_RENDER, as_json, result)
    _write(target, data, as_json)
    if as_json:
        _print_json(True, result, output=str(target), format=chosen.value)
    else:
        click.echo(str(target))


@app.command(epilog="\b\nExample:\n  memmap check layout.yaml --json --strict")
@config_argument
@json_option
@strict_option
@set_option
@verbose_option
def check(
    config: Path, as_json: bool, strict: bool, sets: tuple[str, ...], verbose: bool
) -> None:
    """Validate a config without drawing it.

    CONFIG is the YAML config file.
    """
    result = _load(config, sets, verbose, as_json)
    ok = _passes(result, strict)
    if as_json:
        _print_json(ok, result)
    else:
        _print_diagnostics(result, config)
        if ok and not result.diagnostics:
            click.echo(f"{config.name}: ok")
    if not ok:
        raise Exit(EXIT_VALIDATION)


@app.command(epilog="\b\nExample:\n  memmap describe layout.yaml --json")
@config_argument
@json_option
@set_option
def describe(config: Path, as_json: bool, sets: tuple[str, ...]) -> None:
    """Print the resolved regions, columns, labels and option origins.

    CONFIG is the YAML config file.
    """
    result = _load(config, sets, as_json=as_json)
    if not result.ok:
        if as_json:
            _print_json(False, result)
        else:
            _print_diagnostics(result, config)
        raise Exit(EXIT_VALIDATION)
    if as_json:
        click.echo(json.dumps(describe_data(result), indent=2))
    else:
        _print_diagnostics(result, config)
        click.echo(describe_text(result))


@app.command(
    name="from-elf",
    epilog=(
        "\b\nExample:\n  memmap from-elf firmware.elf -o layout.yaml "
        "--region flash=0x08000000:64K --region ram=0x20000000:20K"
    ),
)
@click.argument("elf", type=click.Path(path_type=Path))
@click.option(
    "-o", "--output", type=click.Path(path_type=Path), help="Config to write. Default: ELF.yaml."
)
@click.option(
    "--region",
    "regions",
    multiple=True,
    metavar="ID=START:SIZE[:NAME]",
    help="A memory region, such as flash=0x08000000:64K:Flash (repeatable).",
)
@click.option(
    "--min-size",
    default="64",
    show_default=True,
    help="Fold runs of smaller sections into one block; smaller gaps get no Free block.",
)
@click.option("--force", is_flag=True, help="Overwrite the output if it exists.")
@json_option
@strict_option
def from_elf(
    elf: Path,
    output: Path | None,
    regions: tuple[str, ...],
    min_size: str,
    force: bool,
    as_json: bool,
    strict: bool,
) -> None:
    """Write a config from the allocated sections of an ELF file.

    ELF is a linked executable. Give each memory region with --region, as the ELF does not say
    how big flash and RAM are; without any, sections are drawn on the whole address space. The
    written config is then checked like `memmap check`.
    """
    target = output or elf.with_suffix(".yaml")
    if target.resolve() == elf.resolve():
        raise _fail(f"output {target} would overwrite the ELF", EXIT_USAGE, as_json)
    if target.exists() and not force:
        raise _fail(f"{target} exists, use --force to overwrite it", EXIT_USAGE, as_json)
    try:
        threshold = 0 if min_size.strip() == "0" else parse_size(min_size)
    except ValueError as exc:
        raise _fail(f"bad --min-size: {exc}", EXIT_USAGE, as_json)
    try:
        memories = [parse_region_spec(spec) for spec in regions]
    except ImportProblem as exc:
        raise _fail(str(exc), EXIT_USAGE, as_json)
    try:
        data = elf.read_bytes()
    except OSError as exc:
        raise _fail(f"cannot read {elf}: {exc}", EXIT_USAGE, as_json)
    try:
        imported = import_elf(read_elf(data), elf.name, memories, threshold)
    except (ElfError, ImportProblem) as exc:
        raise _fail(f"{elf}: {exc}", EXIT_USAGE, as_json)
    _write(target, imported.text.encode("utf-8"), as_json)
    result = _load(target, (), as_json=as_json)
    result.diagnostics[:0] = imported.diagnostics
    ok = _passes(result, strict)
    if as_json:
        _print_json(ok, result, output=str(target))
    else:
        _print_diagnostics(result, target)
        click.echo(str(target))
    if not ok:
        raise Exit(EXIT_VALIDATION)


@app.command(epilog="\b\nExample:\n  memmap schema > memmap.schema.json")
def schema() -> None:
    """Print the JSON Schema of the config."""
    click.echo(json_schema())


def _packaged_text(name: str) -> str:
    """Read a file from the installed package, or from the repo root in a checkout."""
    package = Path(__file__).parent
    for folder in (package, package.parent):
        file = folder / name
        if file.is_file():
            return file.read_text(encoding="utf-8")
    raise _fail(f"{name} is missing from this install", EXIT_USAGE)


@app.command(epilog="\b\nExample:\n  memmap guide > memmap-guide.md")
def guide() -> None:
    """Print the agent guide: workflow, recipes and diagnostic codes."""
    click.echo(_packaged_text("AGENTS.md"), nl=False)
