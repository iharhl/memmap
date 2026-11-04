"""Rewrite the ELF header snapshots in this folder from the real ELF files in build/elf/.

A snapshot keeps only what `memmap from-elf` reads: the section headers and the loadable
segments, plus the `--region` specs to import with. No code or data from the firmware is kept,
so the snapshots are small and carry none of the firmware's own files. docs/elf-testing.md
explains how the tests use them.

    uv run python tests/fixtures/elf_snapshots/refresh.py              # all snapshots
    uv run python tests/fixtures/elf_snapshots/refresh.py --download   # fetch missing ELFs first

After a refresh, regenerate the goldens with
MEMMAP_UPDATE_GOLDEN=1 uv run pytest tests/test_elf_snapshots.py and review their diff.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

from memmap.elf import PT_LOAD, read_elf

HERE = Path(__file__).parent
ROOT = HERE.parent.parent.parent
ELF_DIR = ROOT / "build" / "elf"
RENODE = "https://dl.antmicro.com/projects/renode/"

# name: where to download the ELF, what it shows, and the memory regions to import it with.
# Region sizes are the chip's real flash and RAM, from its datasheet or Renode's platform file.
MANIFEST: dict[str, dict[str, object]] = {
    "nrf52840_zephyr_shell": {
        "url": RENODE + "renode-nrf52840-zephyr_shell_module.elf-gf8d05cf-s_1310072"
        "-c00fbffd6b65c6238877c4fe52e8228c2a38bf1f",
        "about": "Zephyr shell on nRF52840 (Cortex-M4): many small sections, one RAM init image",
        "regions": ["flash=0x0:1M:Flash", "ram=0x20000000:256K:RAM"],
    },
    "fe310_zephyr_shell": {
        "url": RENODE + "zephyr-fe310-shell.elf-s_323068-cf87169150ecdb30ad5a14c87ae209c53dd3eca2",
        "about": "Zephyr shell on SiFive FE310 (RISC-V), running from SPI flash",
        "regions": ["flash=0x20400000:12M:SPI flash (user)", "dtim=0x80000000:16K:DTIM"],
    },
    "msp430f2619_hello": {
        "url": RENODE + "msp430f2619-hello_world.elf-s_7912-e951b1bdd3bb562397ca9da8da88722c503507a3",
        "about": "Hello world on MSP430F2619 (16-bit), reset vector at the top of flash",
        "regions": ["flash=0x2100:0x1DF00:Flash", "ram=0x1100:4K:RAM"],
    },
    "stm32f412_tock_kernel": {
        "url": RENODE + "stm32f412gdiscovery--tock_kernel.elf-s_3392340"
        "-6da12cfcd5c4180b60ce7bf2ad32f019c9e8216e",
        "about": "Tock (Rust) kernel on STM32F412, with an app area far up flash",
        "regions": ["flash=0x08000000:1M:Flash", "sram=0x20000000:256K:SRAM"],
    },
    "r52_zephyr_philosophers": {
        "url": RENODE + "fvp_baser_aemv8r_aarch32--zephyr-philosophers.elf-s_500280"
        "-b9bbb31c64dec3f3273535be657b8e4d7ca182f9",
        "about": "Zephyr philosophers on Cortex-R52, all in one 2 GB DRAM",
        "regions": ["dram=0x0:2G:DRAM"],
    },
    "stm32f4discovery": {
        "url": RENODE + "stm32f4discovery.elf-s_445441-827a0dedd3790f4559d7518320006613768b5e72",
        "about": "STM32F4 Discovery demo (STM32F407), a classic CubeMX-style layout",
        "regions": ["flash=0x08000000:1M:Flash", "sram=0x20000000:128K:SRAM"],
    },
    "stm32f746_dartino_lines": {
        "url": RENODE + "dartino-lines.elf-s_486816-cd8876ab9de60af779f4429dfe16c79bf831b84d",
        "about": "Dartino (Dart VM) lines demo on STM32F746",
        "regions": ["flash=0x08000000:1M:Flash", "sram=0x20000000:320K:SRAM"],
    },
    "apollo4_hello_uart": {
        "url": RENODE + "hello_world_uart.axf-s_307536-899c2682fa35d4bf27992bba5a0b5845ae331ba3",
        "about": "AmbiqSuite hello world on Ambiq Apollo4 (Cortex-M4F): three banks, with data in "
        "TCM and shared SRAM, each copied from MRAM",
        # The 2 MB MRAM starts at 0, the application at 0x18000.
        "regions": [
            "mram=0x18000:1952K:MRAM (app)",
            "tcm=0x10000000:384K:TCM",
            "sram=0x10060000:1M:Shared SRAM",
        ],
    },
}


def snapshot(name: str, entry: dict[str, object], data: bytes) -> dict[str, object]:
    """The snapshot of one ELF: its source, regions, header fields, sections and segments."""
    elf = read_elf(data)
    return {
        "name": name,
        "about": entry["about"],
        "url": entry["url"],
        "sha256": hashlib.sha256(data).hexdigest(),
        "regions": entry["regions"],
        "bits": elf.bits,
        "little_endian": data[5] == 1,
        "file_type": int.from_bytes(data[16:18], "little" if data[5] == 1 else "big"),
        "sections": [
            {
                "name": s.name,
                "type": s.kind,
                "flags": s.flags,
                "address": f"0x{s.address:X}",
                "size": s.size,
                "offset": s.offset,
            }
            for s in elf.sections
        ],
        "segments": [  # only loadable ones, the importer reads no others
            {
                "offset": s.offset,
                "virtual": f"0x{s.virtual:X}",
                "physical": f"0x{s.physical:X}",
                "file_size": s.file_size,
                "memory_size": s.memory_size,
            }
            for s in elf.segments
            if s.kind == PT_LOAD
        ],
    }


def main() -> int:
    """Refresh every snapshot whose ELF is in build/elf/. Returns the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--download", action="store_true", help="fetch missing ELFs first")
    args = parser.parse_args()
    ELF_DIR.mkdir(parents=True, exist_ok=True)
    missing = []
    for name, entry in MANIFEST.items():
        file = ELF_DIR / f"{name}.elf"
        if not file.exists() and args.download:
            print(f"downloading {name}")
            urllib.request.urlretrieve(str(entry["url"]), file)
        if not file.exists():
            missing.append(name)
            continue
        text = json.dumps(snapshot(name, entry, file.read_bytes()), indent=2) + "\n"
        (HERE / f"{name}.json").write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {name}.json")
    for name in missing:
        print(f"skipped {name}: build/elf/{name}.elf is missing", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
