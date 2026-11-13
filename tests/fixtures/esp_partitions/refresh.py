"""Rewrite the ESP-IDF partition table fixtures in this folder from the real files in
build/esp_partitions/.

Two kinds of fixture:
- builds/NAME/: a project's partition table CSV, its sdkconfig and the partition-table.bin its
  ESP-IDF build wrote, committed together. The .bin is the ground truth for placing the CSV.
  The sdkconfig keeps only the lines memmap reads.
- tables/NAME.csv: partition tables on their own, from ESP-IDF, Arduino-ESP32 and MicroPython.

Each file keeps its bytes, but the sdkconfig. index.json lists them with the SHA-256 of the full
files. docs/from-partitions.md explains how the tests use them.

    uv run python tests/fixtures/esp_partitions/refresh.py              # all fixtures
    uv run python tests/fixtures/esp_partitions/refresh.py --download   # fetch missing files

After a refresh, regenerate the goldens with
MEMMAP_UPDATE_GOLDEN=1 uv run pytest tests/test_from_partitions.py and review their diff.
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
SOURCE_DIR = ROOT / "build" / "esp_partitions"
GITHUB = "https://raw.githubusercontent.com/"
IDF = GITHUB + "espressif/esp-idf/4109c8923a51a5234801c324b39240d74e4cc379/"
ARDUINO = GITHUB + "espressif/arduino-esp32/3ed7b779f2b4c797b3512f3fc10a7a978a78fb18/tools/partitions/"
MICROPYTHON = GITHUB + "micropython/micropython/d2d095d0fe6e83c53fd0ef90643f7d2b8b1c1645/ports/esp32/"

# The sdkconfig lines memmap reads, and the header line naming the ESP-IDF version.
_SDKCONFIG_KEEP = re.compile(
    r"^(# Espressif IoT Development Framework.*"
    r"|CONFIG_IDF_TARGET=.*|CONFIG_IDF_INIT_VERSION=.*"
    r"|CONFIG_ESPTOOLPY_FLASHSIZE=.*|CONFIG_PARTITION_TABLE_\w+=.*"
    r"|CONFIG_BOOTLOADER_OFFSET_IN_FLASH=.*|CONFIG_BOOTLOADER_RECOVERY_OFFSET=.*"
    r"|CONFIG_SECURE_BOOT\w*=.*)$"
)


def _build(repo: str, sha: str, folder: str, table: str | None, about: str) -> dict[str, object]:
    """A build: the table (None for a table built into ESP-IDF), sdkconfig and .bin, all in
    `folder` of `repo` at commit `sha`."""
    base = f"{GITHUB}{repo}/{sha}/{folder + '/' if folder else ''}"
    files = {"sdkconfig": base + "sdkconfig", "partition-table.bin": base + "build/partition_table/partition-table.bin"}
    if table is not None:
        files["partitions.csv"] = base + table
    return {"about": about, "files": files}


# name: what it shows, and where each file comes from.
BUILDS: dict[str, dict[str, object]] = {
    "esp32_table_at_0x18000": _build(
        "energietransitie/needforheat-p1-reader-firmware", "1798396832ab69dc3f3978e237e3f7a1dc9abbf5",
        "", None, "ESP32: the built-in single app table, moved up by CONFIG_PARTITION_TABLE_OFFSET",
    ),
    "esp32_homekit_custom_type": _build(
        "UsefulElectronics/esp32-homekit-lightbulb", "2e78c6427890729625384b0307f206dd09ec6f85",
        "", "partitions_hap.csv",
        "ESP32, older ESP-IDF: a custom type with a blank subtype, then 0 and now 0x06",
    ),
    "esp32_small_nvs": _build(
        "majorBien/Extremum---Beta", "0460770d954b62d80888a4d9843d583cfdcdc39d", "", "partitions.csv",
        "ESP32, ESP-IDF 5.2: blank offsets, an 8 KB read-write NVS that ESP-IDF now rejects",
    ),
    "esp32h2_zigbee": _build(
        "aimpertro/esp-zigbee-plant-monitor", "45b69c3edd0149132c4f37c57e90d63f68fc27bf", "",
        "partitions.csv",
        "ESP32-H2: an app size that is not a multiple of 4K, a 1K partition off a sector edge",
    ),
    "esp32p4_singleapp": _build(
        "serfereanyang-create/my_toolkit", "1eaae36df31fe0c2bb7e3c035dbeff371e56e5dd", "hello_p4",
        None, "ESP32-P4, ESP-IDF 6.0: the bootloader at 0x2000",
    ),
}

# name: where the table comes from, what it shows and the flash size to draw it in.
TABLES: dict[str, dict[str, str]] = {
    **{
        f"idf_{name}": {
            "url": f"{IDF}components/partition_table/{name}.csv",
            "about": "built into ESP-IDF",
            "flash_size": "4M",
        }
        for name in ("partitions_singleapp", "partitions_two_ota")
    },
    "idf_ota_efuse_emul": {
        "url": f"{IDF}examples/system/ota/partitions_ota/test/partitions_efuse_emul_2.csv",
        "about": "ESP-IDF example: bootloader and partition_table entries, an encrypted partition",
        "flash_size": "4M",
    },
    "arduino_default": {
        "url": f"{ARDUINO}default.csv",
        "about": "Arduino-ESP32 default: explicit offsets, no spaces after some commas",
        "flash_size": "4M",
    },
    "micropython_4MiBplus": {
        "url": f"{MICROPYTHON}partitions-4MiBplus.csv",
        "about": "MicroPython: blank offsets, $IDF_PATH in a comment, lowercase k sizes",
        "flash_size": "4M",
    },
}


def _fetch(url: str, source: Path, download: bool) -> bytes | None:
    """The original file, downloaded first when allowed and missing."""
    if not source.exists() and download:
        print(f"downloading {url}")
        source.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, source)
    return source.read_bytes() if source.exists() else None


def trim_sdkconfig(url: str, data: bytes) -> bytes:
    """The sdkconfig lines memmap reads, under a line saying where they come from."""
    lines = data.decode("utf-8", "replace").replace("\r\n", "\n").split("\n")
    kept = [line for line in lines if _SDKCONFIG_KEEP.match(line)]
    return "\n".join([f"# Excerpt of {url}", *kept, ""]).encode("utf-8")


def main() -> int:
    """Refresh every fixture whose file is in build/esp_partitions/. Returns the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--download", action="store_true", help="fetch missing files first")
    args = parser.parse_args()
    missing: list[str] = []
    builds = []
    for name, build in BUILDS.items():
        files = {}
        for file, url in build["files"].items():
            data = _fetch(url, SOURCE_DIR / "builds" / name / file, args.download)
            if data is None:
                missing.append(f"builds/{name}/{file}")
                continue
            target = HERE / "builds" / name / file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(trim_sdkconfig(url, data) if file == "sdkconfig" else data)
            files[file] = {"url": url, "sha256": hashlib.sha256(data).hexdigest()}
        builds.append({"name": name, "about": build["about"], "files": files})
        print(f"wrote builds/{name}")
    tables = []
    for name, table in TABLES.items():
        data = _fetch(table["url"], SOURCE_DIR / "tables" / f"{name}.csv", args.download)
        if data is None:
            missing.append(f"tables/{name}.csv")
            continue
        target = HERE / "tables" / f"{name}.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        tables.append({"name": name, **table, "sha256": hashlib.sha256(data).hexdigest()})
        print(f"wrote tables/{name}.csv")
    for name in missing:
        print(f"skipped {name}: it is missing from build/esp_partitions/", file=sys.stderr)
    if missing:
        return 1
    text = json.dumps({"builds": builds, "tables": tables}, indent=2) + "\n"
    (HERE / "index.json").write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
