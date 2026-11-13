"""Read the settings a partition table drawing needs from an ESP-IDF project's sdkconfig."""

from __future__ import annotations

import re
from dataclasses import dataclass

_LINE = re.compile(r"^(CONFIG_[A-Za-z0-9_]+)=(.*)$", re.MULTILINE)
_FLASH_SIZE = re.compile(r"^(\d+)\s*(KB|MB)$", re.IGNORECASE)

# Where the ROM looks for the second-stage bootloader, for sdkconfigs from before ESP-IDF wrote
# CONFIG_BOOTLOADER_OFFSET_IN_FLASH. From ESP_ROM_BOOTLOADER_OFFSET_FLASH in esp_rom; chips not
# listed use 0.
BOOTLOADER_OFFSETS = {
    "esp32": 0x1000,
    "esp32s2": 0x1000,
    "esp32c5": 0x2000,
    "esp32h4": 0x2000,
    "esp32p4": 0x2000,
    "esp32s31": 0x2000,
}


@dataclass(frozen=True)
class SdkConfig:
    """The settings that place the partition table, each None when the sdkconfig lacks it.
    `flash_size_text` keeps CONFIG_ESPTOOLPY_FLASHSIZE as written, for a value such as
    `detect` that is not a size."""

    flash_size: int | None = None
    flash_size_text: str | None = None
    table_offset: int | None = None
    bootloader_offset: int | None = None
    bootloader_from: str | None = None  # the setting that gave bootloader_offset
    recovery_offset: int | None = None
    target: str | None = None
    secure_boot_v1: bool = False  # app sizes then have to be multiples of 64K


def _value(raw: str) -> str:
    """A value without its quotes and escapes."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return re.sub(r"\\(.)", r"\1", raw[1:-1])
    return raw


def _int(values: dict[str, str], key: str) -> int | None:
    """A numeric setting, or None when it is missing or not a number."""
    try:
        return int(values[key], 0)
    except (KeyError, ValueError):
        return None


def read_sdkconfig(text: str) -> SdkConfig:
    """The settings in an sdkconfig's text. Lines that are not `CONFIG_...=value` are ignored."""
    values = {match.group(1): _value(match.group(2)) for match in _LINE.finditer(text)}
    flash_text = values.get("CONFIG_ESPTOOLPY_FLASHSIZE")
    flash_size = None
    if flash_text is not None:
        match = _FLASH_SIZE.match(flash_text.strip())
        if match:
            factor = 1024 if match.group(2).upper() == "KB" else 1024 * 1024
            flash_size = int(match.group(1)) * factor or None
    target = values.get("CONFIG_IDF_TARGET") or None
    bootloader = _int(values, "CONFIG_BOOTLOADER_OFFSET_IN_FLASH")
    bootloader_from = "CONFIG_BOOTLOADER_OFFSET_IN_FLASH" if bootloader is not None else None
    if bootloader is None and target is not None:
        bootloader = BOOTLOADER_OFFSETS.get(target, 0)
        bootloader_from = f"CONFIG_IDF_TARGET {target}"
    secure = values.get("CONFIG_SECURE_BOOT") == "y"
    v1 = values.get("CONFIG_SECURE_BOOT_V1_ENABLED") == "y"
    short = values.get("CONFIG_SECURE_BOOT_ALLOW_SHORT_APP_PARTITION") == "y"
    return SdkConfig(
        flash_size=flash_size,
        flash_size_text=flash_text,
        table_offset=_int(values, "CONFIG_PARTITION_TABLE_OFFSET"),
        bootloader_offset=bootloader,
        bootloader_from=bootloader_from,
        recovery_offset=_int(values, "CONFIG_BOOTLOADER_RECOVERY_OFFSET") or None,
        target=target,
        secure_boot_v1=secure and v1 and not short,
    )
