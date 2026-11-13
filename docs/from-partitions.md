# Drawing an ESP-IDF partition table

`memmap from-partitions` writes a config from an ESP-IDF partition table: one flash column with
the bootloader, the partition table, every partition and the free space between them.

```bash
memmap from-partitions partitions.csv -o layout.yaml --sdkconfig sdkconfig
```

![The flash of an ESP32 HomeKit project, drawn by from-partitions](../tests/golden/partitions/esp32_homekit_custom_type.svg)

- [What it reads](#what-it-reads)
- [Where the settings come from](#where-the-settings-come-from)
- [What is drawn](#what-is-drawn)
- [Checks](#checks)
- [Limits](#limits)
- [Testing](#testing)

## What it reads

The table is either of the files ESP-IDF works from, told apart by their content:

| File | Where | Offsets |
| --- | --- | --- |
| Partition table CSV | the project, named by `CONFIG_PARTITION_TABLE_FILENAME` | worked out by memmap, as the build does |
| `partition-table.bin` | `build/partition_table/` | as the build wrote them |

A CSV has the columns `Name, Type, SubType, Offset, Size, Flags`, and memmap reads it the way
ESP-IDF's `gen_esp32part.py` does:

- Lines starting with `#` and blank lines are skipped. A header line without `#` is an error,
  as it is for the build.
- Numbers are Python integers, so `0x9000`, `36864`, `0o110000` and `0x9_000` all work, with an
  optional `K` or `M` suffix in either case.
- Types are `app`, `data`, `bootloader`, `partition_table` or a number. Subtypes are ESP-IDF's
  names, such as `ota_0`, `nvs` or `spiffs`, or a number. A subtype name that a component
  registers is kept as written.
- A blank offset follows the partition above it, aligned to 64K for an app and to 4K for
  anything else. The first one starts after the partition table. Partitions with offsets must
  be listed in address order, as the build places them in file order.
- A negative size gives the offset the partition ends at: `-0x400000` runs to the end of 4 MB.
- `bootloader` and `partition_table` entries with the `primary` subtype sit at the bootloader
  and partition table offsets, whatever their offset and size columns say.
- Flags are `encrypted` and `readonly`, separated by `:`.
- A byte order mark selects UTF-8, UTF-16 or UTF-32.

A binary table is a list of 32-byte entries with an MD5 entry and `0xFF` padding. memmap checks
the MD5 and reads the entries as they are.

## Where the settings come from

The table gives the partitions, but not how big the flash is, where the partition table sits or
where the ROM loads the bootloader from. memmap takes each from the options, else from the
`sdkconfig` given with `--sdkconfig`, else from a default:

| Setting | Option | sdkconfig | Default |
| --- | --- | --- | --- |
| Flash size | `--flash-size 4M` | `CONFIG_ESPTOOLPY_FLASHSIZE` | none: memmap stops and says what to give |
| Partition table offset | `--table-offset 0x8000` | `CONFIG_PARTITION_TABLE_OFFSET` | 0x8000 |
| Bootloader offset | `--bootloader-offset 0x1000` | `CONFIG_BOOTLOADER_OFFSET_IN_FLASH`, else the chip in `CONFIG_IDF_TARGET` | not known |
| Recovery bootloader | | `CONFIG_BOOTLOADER_RECOVERY_OFFSET` | none |
| Secure boot V1 | | `CONFIG_SECURE_BOOT_V1_ENABLED` | off |

The bootloader starts at 0x1000 on the ESP32 and ESP32-S2, at 0x2000 on the ESP32-C5, ESP32-H4,
ESP32-P4 and ESP32-S31, and at 0 on the other chips. A binary table with `primary` bootloader
or partition table entries gives those offsets itself; an option or sdkconfig that says
otherwise belongs to another build, and memmap stops. The header comments of the config say
which source gave each setting.

Projects without an sdkconfig, such as Arduino-ESP32 or PlatformIO ones, give the flash size
and, for a drawing that places the bootloader, `--bootloader-offset`.

When the flash size is missing and an `sdkconfig` lies next to the table, the hint names it.

## What is drawn

One region, `flash`, from 0 to the flash size. Offsets are positions in the flash chip, not the
addresses the CPU reads the flash through. Its blocks, in address order:

- `Reserved` below the bootloader, on a chip whose bootloader does not start at 0.
- `Bootloader`, from the bootloader offset up to the partition table. Without a known offset it
  starts at 0, with a note saying it starts at 0x0, 0x1000 or 0x2000 by chip.
- `Partition table`, one 4K sector.
- Each partition, named as in the table, with a note giving its type, subtype and flags, such as
  `app, ota_0` or `data, nvs_keys, encrypted`. A subtype left blank in a CSV is not shown, as
  its value depends on the ESP-IDF version.
- `Free` for the space between them, with its size.

A table that has its own `primary` bootloader or partition table entry is drawn with that entry
in place of the generated block. The region has 4K `sectors`, so `check` reports a partition
edge inside an erase sector as W100; the tick marks are turned off in `style`. The column title
gives how much of the flash the bootloader, the table and the partitions take.

## Checks

memmap stops, writing nothing, when the table cannot be drawn:

- the CSV cannot be read: an unknown type or flag, a blank type or size, a bad number;
- a partition starts before the end of the partition table, or overlaps one listed before it;
- a partition runs past the end of the flash, or has size 0;
- the partition table offset is 0, is not a multiple of 0x1000, or runs past the end of the
  flash, or the bootloader is not below it;
- a binary table's own bootloader or partition table entry disagrees with the options or the
  sdkconfig.

The message names the CSV line, and the hint says what to change. With `--json` it is E090.

A CSV that can be drawn but breaks a rule of ESP-IDF's own table check gets W113, which
`--strict` turns into a failure:

- an app starts off a 64K boundary, or anything else off a 4K one;
- an app's size is not a multiple of 4K, or of 64K with secure boot V1;
- a read-write NVS partition is smaller than 12K;
- `ota` or `coredump` data is marked `readonly`;
- there is more than one `otadata` or TEE `otadata` partition, or it is not 8K;
- two partitions share a name;
- a subtype name is not one ESP-IDF defines. The build accepts it only when a component
  registers it, so check the spelling.

Some of these rules are newer than others: a table that ESP-IDF 5.2 built can get W113 from a
rule added later. A binary table is not checked, as the build that wrote it checked it already.

## Limits

- **Environment variables.** The build expands `$NAME` and `${NAME}` in a CSV from the
  environment. memmap stops at any `$` followed by a name in a partition line, and asks for
  the value, so that the drawing does not depend on the shell it ran in.
- **Blank subtypes.** ESP-IDF 5 reads a blank subtype as `0x06`, undefined. Earlier versions
  read it as 0, which is also `otadata`'s subtype. memmap follows ESP-IDF 5.
- **Flash size `detect`.** Older sdkconfigs can leave the size to be detected when flashing.
  Give `--flash-size` then.
- **Other formats.** Only ESP-IDF tables are read. Arduino-ESP32, PlatformIO and MicroPython use
  the same CSV format, so their tables work too.
- **Table length.** ESP-IDF's binary table holds at most 95 entries. memmap does not check this.

## Testing

`tests/test_from_partitions.py` runs on real tables, kept in `tests/fixtures/esp_partitions/`
with their sources and SHA-256 in `index.json`:

- **Builds.** Five ESP-IDF projects that committed their partition table, their `sdkconfig` and
  the `partition-table.bin` their build wrote. Between them: a partition table moved to 0x18000,
  a custom partition type with a blank subtype, blank offsets, rules that newer ESP-IDF added,
  a 1K partition, and bootloaders at 0x0, 0x1000 and 0x2000. The `.bin` is the ground truth:
  memmap's placement of the CSV must match it entry for entry. The sdkconfig keeps only the
  lines memmap reads.
- **Tables.** Two tables built into ESP-IDF, an ESP-IDF example with `bootloader` and
  `partition_table` entries, and the default tables of Arduino-ESP32 and MicroPython.

Each one is imported to a golden config in `tests/golden/partitions/`, and two builds are also
drawn to a golden SVG, as the drawing code is the same for every table. Each fixture shows
something the others do not; keep it that way. To add a table, add it to `refresh.py` in the
fixtures folder, then:

```bash
uv run python tests/fixtures/esp_partitions/refresh.py --download
```

```bash
MEMMAP_UPDATE_GOLDEN=1 uv run pytest tests/test_from_partitions.py
```

Before the fixtures were chosen, the same comparison ran on 121 ESP-IDF builds found on GitHub.
memmap placed 117 exactly as their `partition-table.bin`. Two differed only in a blank subtype,
which their older ESP-IDF read as 0. The `.bin` of the other two came from a different table
than the one committed, a build left stale.
