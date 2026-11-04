"""`memmap from-elf`: write a config from a linked firmware image.

- `elf` reads the section and program headers of an ELF file.
- `regions` holds the memory regions sections are placed in, from `--region` specs.
- `linker` reads memory regions from a linker map instead, or as well.
- `importer` places the sections in the regions, and `writer` writes the YAML.
"""

from .elf import Elf, ElfError, read_elf
from .importer import FOLD_OVER, Imported, import_elf
from .linker import LinkerFile, LinkerRegion, Resolved, read_map, resolve_memories
from .regions import ImportProblem, LeftOut, MemorySpec, parse_region_spec

__all__ = [
    "FOLD_OVER",
    "Elf",
    "ElfError",
    "ImportProblem",
    "Imported",
    "LeftOut",
    "LinkerFile",
    "LinkerRegion",
    "MemorySpec",
    "Resolved",
    "import_elf",
    "parse_region_spec",
    "read_elf",
    "read_map",
    "resolve_memories",
]
