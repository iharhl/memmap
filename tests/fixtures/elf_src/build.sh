#!/bin/sh
# Rebuilds the real ELF fixtures in tests/fixtures/. Tests use the committed files.
#   firmware.elf        32-bit little-endian, from arm-none-eabi-gcc
#   firmware_be.elf     32-bit big-endian, the same code built with -mbig-endian
#   firmware64.elf      64-bit little-endian, firmware.elf converted by GNU objcopy
#   firmware64_be.elf   64-bit big-endian, firmware_be.elf converted by GNU objcopy
# They come from binutils, not from memmap's own code, so they check the reader independently.
# Needs arm-none-eabi-gcc, and a GNU objcopy with the elf64 targets (Homebrew's binutils) as
# $OBJCOPY, default objcopy.
set -eu
cd "$(dirname "$0")"
OBJCOPY=${OBJCOPY:-objcopy}
FLAGS="-mcpu=cortex-m4 -mthumb -Os -nostdlib -ffreestanding -Wl,--build-id=none -T firmware.ld"
arm-none-eabi-gcc $FLAGS -o ../firmware.elf firmware.c
arm-none-eabi-gcc $FLAGS -mbig-endian -o ../firmware_be.elf firmware.c
arm-none-eabi-strip --strip-debug ../firmware.elf ../firmware_be.elf
# objcopy moves the empty ._stack load address and says so; that segment has no bytes in flash.
"$OBJCOPY" -O elf64-little ../firmware.elf ../firmware64.elf 2>/dev/null
"$OBJCOPY" -O elf64-big ../firmware_be.elf ../firmware64_be.elf 2>/dev/null
