"""Rewrite the Zephyr and Partition Manager fixtures in this folder from the real files in
build/zephyr_partitions/.

Two kinds of fixture:
- builds/NAME/: a build's zephyr.dts or partitions.yml, committed with the header its build
  generated from it: devicetree_generated.h or pm_config.h. The header is the ground truth for
  the addresses memmap reads. It keeps only the address and size macros the test reads.
- tables/NAME: a pm_static.yml on its own, which no build generates a header from.

Each file keeps its bytes, but the headers. index.json lists them with the SHA-256 of the full
files. docs/from-partitions.md explains how the tests use them.

    uv run python tests/fixtures/zephyr_partitions/refresh.py              # all fixtures
    uv run python tests/fixtures/zephyr_partitions/refresh.py --download   # fetch missing files

After a refresh, regenerate the goldens with
MEMMAP_UPDATE_GOLDEN=1 uv run pytest tests/test_from_zephyr.py and review their diff.
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
SOURCE_DIR = ROOT / "build" / "zephyr_partitions"
GITHUB = "https://raw.githubusercontent.com/"

# The header lines the test reads: the reg of each node, and the address and size of each
# Partition Manager partition. Of the reg lines, only those of partitions and flashes are kept.
_HEADER_KEEP = re.compile(
    r"^(#define (DT_N\w*)_REG_IDX_0_VAL_(?:ADDRESS|SIZE) .*"
    r"|#define PM_\w+_(?:ADDRESS|SIZE) .*)$"
)


def _files(repo: str, sha: str, files: dict[str, str]) -> dict[str, str]:
    """File name: URL, for paths in `repo` at commit `sha`."""
    return {name: f"{GITHUB}{repo}/{sha}/{path}" for name, path in files.items()}


def _devicetree(repo: str, sha: str, build: str, header: str = "zephyr/devicetree_generated.h") -> dict[str, str]:
    """A Zephyr build's zephyr.dts and devicetree_generated.h, in `build`, the build folder."""
    return _files(
        repo,
        sha,
        {
            "zephyr.dts": f"{build}/zephyr/zephyr.dts",
            "devicetree_generated.h": f"{build}/zephyr/include/generated/{header}",
        },
    )


# name: what it shows, and where each file comes from.
BUILDS: dict[str, dict[str, object]] = {
    "nrf5340_subpartitions": {
        "about": "nRF5340: slot 0 split into secure and non-secure fixed-subpartitions by ranges",
        "files": _devicetree(
            "CookieChemist/Ryhma9-tietoliikenteen_sovellusprojekti",
            "0052294f54ecac0061b199aa7fa35f950a0ab824",
            "createCM/build/TL_Project_Week6",
        ),
    },
    "mcxn947_flexspi": {
        "about": "NXP MCX N947: flash at 0x10000000 through two ranges, FlexSPI NOR on a bus",
        "files": _devicetree(
            "Caleb-Balboni/frdm_mcxn947_hn-testing",
            "79df8da6b6d43b65ce6100da07385004d97c58e2",
            "i3c-test/build",
        ),
    },
    "stm32_mcuboot_scratch": {
        "about": "STM32 at 0x08000000: MCUboot slots and scratch, QSPI NOR with its size in bits",
        "files": _devicetree(
            "castrorazo/KOHLER-SIngleRelay-Testing",
            "cb785a82804f1381cd9e5f6a46bf57b1eda92ecf",
            "tests/app/build",
            "devicetree_generated.h",  # older Zephyr wrote it one folder up
        ),
    },
    "nrf5340_tfm_pm": {
        "about": "nRF Connect SDK 2.5, nRF5340 with TF-M: nested containers, external flash, OTP",
        "files": _files(
            "manu897/Project-Carl",
            "7e80b563b48d63d939d8e879ff309dc88697572e",
            {
                "partitions.yml": "build/partitions.yml",
                "pm_config.h": "build/zephyr/include/generated/pm_config.h",
            },
        ),
    },
}

# name: what it shows and where the file comes from.
TABLES: dict[str, dict[str, str]] = {
    "pm_static_stale_app.yml": {
        "about": "pm_static.yml whose app entry overlaps MCUboot: the build places app (W114)",
        "url": f"{GITHUB}Anvayt24/omi/cf54e09856c0073c0adc7209b2e18e2ef756ed64/omi/firmware/boards/omi/pm_static.yml",
    },
}


def _fetch(url: str, source: Path, download: bool) -> bytes | None:
    """The original file, downloaded first when allowed and missing."""
    if not source.exists() and download:
        print(f"downloading {url}")
        source.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, source)
    return source.read_bytes() if source.exists() else None


def trim_header(url: str, data: bytes) -> bytes:
    """The address and size macros of a generated header, under a line saying where they come
    from."""
    text = data.decode("utf-8", "replace").replace("\r\n", "\n")
    flashes = set(re.findall(r"\b(DT_N\w*?)_S_partitions\b", text))
    kept = []
    for line in text.split("\n"):
        match = _HEADER_KEEP.match(line)
        node = match.group(2) if match else None
        if match and (node is None or "_S_partitions_S_" in node or node in flashes):
            kept.append(line)
    return "\n".join([f"/* Excerpt of {url} */", *kept, ""]).encode("utf-8")


def main() -> int:
    """Refresh every fixture whose file is in build/zephyr_partitions/. Returns the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--download", action="store_true", help="fetch missing files first")
    args = parser.parse_args()
    missing: list[str] = []
    builds = []
    for name, build in BUILDS.items():
        files = {}
        urls = build["files"]
        assert isinstance(urls, dict)
        for file, url in urls.items():
            data = _fetch(url, SOURCE_DIR / "builds" / name / file, args.download)
            if data is None:
                missing.append(f"builds/{name}/{file}")
                continue
            target = HERE / "builds" / name / file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(trim_header(url, data) if file.endswith(".h") else data)
            files[file] = {"url": url, "sha256": hashlib.sha256(data).hexdigest()}
        builds.append({"name": name, "about": build["about"], "files": files})
        print(f"wrote builds/{name}")
    tables = []
    for name, table in TABLES.items():
        data = _fetch(table["url"], SOURCE_DIR / "tables" / name, args.download)
        if data is None:
            missing.append(f"tables/{name}")
            continue
        target = HERE / "tables" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        tables.append({"name": name, **table, "sha256": hashlib.sha256(data).hexdigest()})
        print(f"wrote tables/{name}")
    for name in missing:
        print(f"skipped {name}: it is missing from build/zephyr_partitions/", file=sys.stderr)
    if missing:
        return 1
    text = json.dumps({"builds": builds, "tables": tables}, indent=2) + "\n"
    (HERE / "index.json").write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
