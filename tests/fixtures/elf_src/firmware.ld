/* Minimal Cortex-M layout for the from-elf test fixture. */
MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 64K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 20K
}

ENTRY(reset_handler)

SECTIONS
{
  .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
  .text       : { *(.text*) } > FLASH
  .rodata     : { . = ALIGN(4); *(.rodata*) } > FLASH
  .ARM.exidx  : { *(.ARM.exidx*) } > FLASH
  .data       : { . = ALIGN(4); _sdata = .; *(.data*) _edata = .; } > RAM AT > FLASH
  _sidata = LOADADDR(.data);
  .bss (NOLOAD) : { . = ALIGN(4); _sbss = .; *(.bss*) *(COMMON) _ebss = .; } > RAM
  ._stack (NOLOAD) : { . = ALIGN(8); . = . + 2K; _estack = .; } > RAM
}
