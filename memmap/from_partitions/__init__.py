"""`memmap from-partitions`: write a config from a flash partition table.

- `esp_idf` reads an ESP-IDF partition table, CSV or binary, and places its partitions.
- `sdkconfig` reads the flash size and offsets from an ESP-IDF project's sdkconfig.
- `importer` lays the partitions out in the flash, and `writer` writes the YAML.
"""

from .importer import Settings, import_table, settings
from .sdkconfig import SdkConfig, read_sdkconfig

__all__ = ["SdkConfig", "Settings", "import_table", "read_sdkconfig", "settings"]
