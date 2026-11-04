"""Rewrite the linker map snapshots in this folder from the real files in build/linker/.

A snapshot keeps only the lines that `memmap from-elf --map` reads: the Memory Configuration
table of a GNU ld map, the region lines of an armlink map, the placement summary of an IAR map.
Each starts with where it came from. index.json lists them with the SHA-256 of the full file.
docs/from-elf.md explains how the tests use them.

    uv run python tests/fixtures/linker_snapshots/refresh.py              # all snapshots
    uv run python tests/fixtures/linker_snapshots/refresh.py --download   # fetch missing files

After a refresh, regenerate the expected regions with
MEMMAP_UPDATE_GOLDEN=1 uv run pytest tests/test_linker.py and review their diff.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent.parent.parent
SOURCE_DIR = ROOT / "build" / "linker"
GITHUB = "https://raw.githubusercontent.com/"

# name: where the map comes from, what it shows, and which reader's lines to keep of it.
MANIFEST: dict[str, dict[str, object]] = {
    "zephyr_nrf52dk.map": {
        "url": GITHUB + "9enki/nrf52dk_blinky/264f67e741e9d2c86f1137b8bf16c6e9c0509d7e"
        "/build/zephyr/zephyr.map",
        "about": "Zephyr on nRF52832: Windows line ends, IDT_LIST ending one byte below the top",
        "keep": "gnu",
    },
    "zephyr_esp32.map": {
        "url": GITHUB + "faarodriguezpi/esp32_threads_i2c_gpio/"
        "1becd99e164caac04780676c3f9ceb380e94fe10/build/zephyr/zephyr.map",
        "about": "Zephyr on ESP32 (Xtensa), seven regions printed at 64-bit width",
        "keep": "gnu",
    },
    "zephyr_qemu_x86.map": {
        "url": GITHUB + "granikus/challenge-zephyr-hello/4baeb53ab2fc012cb6eefa9e27d2864c1186a356"
        "/build/zephyr/zephyr.map",
        "about": "Zephyr on x86, one RAM region and an IDT_LIST near the top of memory",
        "keep": "gnu",
    },
    "keil_f103_ac505.map": {
        "url": GITHUB + "yohanes-erwin/stm32f103-keil/53967360e8acf6d0bd495a03bf252202dc902aa7"
        "/dma/Listings/dma.map",
        "about": "Keil, Arm Compiler 5.05 on STM32F103: Base: and Max:",
        "keep": "armlink",
    },
    "keil_f103_ac506.map": {
        "url": GITHUB + "shengqing1/openmv-stm32/5b658bced8abda61daee102530fe6ce9ede03eed"
        "/Listings/Project.map",
        "about": "Keil, Arm Compiler 5.06 on STM32F103: Exec base: and Load base:",
        "keep": "armlink",
    },
    "keil_f407_compressed.map": {
        "url": GITHUB + "Caesar037/STM32_Intelligent_Lock/b7f4329a3402a85b2fd9bdaa3be9ec2345527b9e"
        "/Listings/Template.map",
        "about": "Keil, Arm Compiler 5.06 on STM32F407: compressed RW data, and an empty CCM region",
        "keep": "armlink",
    },
    "keil_no_scatter.map": {
        "url": GITHUB + "PacktPublishing/ARM-Assembly-Language-from-Ground-Up-1/"
        "567c112da1f3374594da0fff4c6d50d2ff5ad2d7/Swap/Listings/Swap.map",
        "about": "Keil, Arm Compiler 5.06 without a scatter file: regions with no Max",
        "keep": "armlink",
    },
    "iar6_stm32f103.map": {
        "url": GITHUB + "Seeed-Studio/DSO_Nano/1feaea30263e1383a93cd345724396bf7a7c6aed"
        "/DS0201_APP/project/EWARM/FLASH/List/DS0201_APP.map",
        "about": "IAR 6.10: plain hex, an application above a bootloader",
        "keep": "iar",
    },
    "iar8_stm32f030.map": {
        "url": GITHUB + "SergeyMax/SolderingStation/68c87792b5ed257ba0caaac4c897abc205cd0a69"
        "/Firmware/EWARM/Firmware/List/Firmware.map",
        "about": "IAR 8.20: a ROM and a RAM placement",
        "keep": "iar",
    },
    "iar8_stm32f407_ccm.map": {
        "url": GITHUB + "Andy-Big/MKS-DLP-alter/a26593ad8b9ac251a6cdc8b4420646ff02667b7b"
        "/MKSDLP_Alter_debug_2_0/List/mksDLP.map",
        "about": "IAR 8.32: digit groups with ', and RAM in CCM and SRAM",
        "keep": "iar",
    },
    "iar8_rt685.map": {
        "url": GITHUB + "JayHeng/NXP-MCUBootUtility/bab2c4d3d7e691c4415cada60934ab80fa073b44"
        "/apps/NXP_MIMXRT685-EVK_Rev.E/led_blinky_0x08001000_fdcb_iar.map",
        "about": "IAR 8.40 on i.MX RT685: unions of ranges, and a stack placed apart",
        "keep": "iar",
    },
    "iar9_stm32f407.map": {
        "url": GITHUB + "ApratimPhadke/KRONOS/c15e8d004f4156a60f7dbed7ceb27c92e44d2fb2"
        "/Debug/List/kronos.map",
        "about": "IAR 9.70: RAM as two adjoining ranges, and placements that matched nothing",
        "keep": "iar",
    },
    "iar_stm8.map": {
        "url": GITHUB + "Emile666/stc1000_stm8/96cc63ffbaf0012f32bf441f4bf91d71f1e20182"
        "/src/Debug/List/stc1000p.map",
        "about": "IAR for STM8: [0x0-0xff] ranges, and EEPROM holding named sections only",
        "keep": "iar",
    },
}


def _gnu(lines: list[str]) -> list[str]:
    """The Memory Configuration table of a GNU ld map."""
    start = next(i for i, line in enumerate(lines) if line.strip() == "Memory Configuration")
    end = next(i for i, line in enumerate(lines) if line.startswith("Linker script and memory map"))
    return lines[start:end]


def _armlink(lines: list[str]) -> list[str]:
    """The heading and region lines of an armlink map's memory map."""
    keep = ("Memory Map of the image", "Load Region ", "Execution Region ")
    return [line for line in lines if any(word in line for word in keep)]


def _iar(lines: list[str]) -> list[str]:
    """The linker version line and the placement summary of an IAR map."""
    version = next(line for line in lines if "IAR ELF Linker" in line)
    start = next(i for i, line in enumerate(lines) if line.startswith("*** PLACEMENT SUMMARY"))
    end = next(i for i in range(start, len(lines)) if re.match(r"\s+Section\s+Kind", lines[i]))
    return [version, "", *lines[start : end + 1]]


EXCERPTS = {"gnu": _gnu, "armlink": _armlink, "iar": _iar}


def snapshot(entry: dict[str, object], data: bytes) -> str:
    """The snapshot text of one map: where it came from, as a plain line the readers skip,
    then the lines its reader uses."""
    lines = data.decode("utf-8", "replace").replace("\r\n", "\n").split("\n")
    kept = EXCERPTS[str(entry["keep"])](lines)
    return "\n".join([f"Excerpt of {entry['url']}", "", *kept]).rstrip("\n") + "\n"


def main() -> int:
    """Refresh every snapshot whose file is in build/linker/. Returns the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--download", action="store_true", help="fetch missing files first")
    args = parser.parse_args()
    index = []
    missing = []
    for name, entry in MANIFEST.items():
        source = SOURCE_DIR / name
        if not source.exists() and args.download:
            print(f"downloading {name}")
            source.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(str(entry["url"]), source)
        if not source.exists():
            missing.append(name)
            continue
        data = source.read_bytes()
        target = HERE / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(snapshot(entry, data), encoding="utf-8", newline="\n")
        index.append(
            {
                "name": name,
                "about": entry["about"],
                "url": entry["url"],
                "sha256": hashlib.sha256(data).hexdigest(),
                "kept": entry["keep"],
            }
        )
        print(f"wrote {name}")
    for name in missing:
        print(f"skipped {name}: build/linker/{name} is missing", file=sys.stderr)
    if missing:
        return 1
    text = json.dumps(index, indent=2) + "\n"
    (HERE / "index.json").write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
