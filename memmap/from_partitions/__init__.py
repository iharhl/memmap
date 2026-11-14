"""`memmap from-partitions`: write a config from a flash partition layout.

- `esp_idf` reads an ESP-IDF partition table, CSV or binary, and places its partitions.
- `sdkconfig` reads the flash size and offsets from an ESP-IDF project's sdkconfig.
- `dts` parses a devicetree source file, and `zephyr` reads the flash partitions of a Zephyr
  build's merged zephyr.dts.
- `partition_manager` reads an nRF Connect SDK Partition Manager file.
- `areas` nests Zephyr and Partition Manager partitions into one region per flash or region.
- `importer` tells the formats apart and lays the partitions out, and `writer` writes the YAML.
"""

from .importer import Settings, import_table, settings
from .sdkconfig import SdkConfig, read_sdkconfig

__all__ = ["SdkConfig", "Settings", "import_table", "read_sdkconfig", "settings"]
