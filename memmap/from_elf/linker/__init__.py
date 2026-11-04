"""Memory regions from the map file a linker writes.

| Toolchain | Map | Module |
| --- | --- | --- |
| GNU ld | `-Map`: the Memory Configuration table | `gnu_map` |
| Arm armlink (Keil MDK) | `--map`: each execution region's base and Max | `armlink_map` |
| IAR ILINK | `--map`: the ranges of the placement summary | `iar_map` |
| LLVM lld | `-Map`, but it lists no regions: `--region` gives them | `files` tells it apart |

`files` tells the formats apart, and `resolve` turns the regions into MemorySpecs.
"""

from .files import read_map
from .model import LinkerFile, LinkerRegion
from .resolve import Resolved, resolve_memories

__all__ = ["LinkerFile", "LinkerRegion", "Resolved", "read_map", "resolve_memories"]
