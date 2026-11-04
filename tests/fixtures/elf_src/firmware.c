/* Test firmware for the from-elf fixture. Built by build.sh; the code never runs. */
#include <stdint.h>

extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss, _estack;

static const char banner[] = "memmap from-elf fixture firmware, version 1.0";
static const uint16_t table[600] = {1, 2, 3};
uint32_t counters[16] = {1};
uint8_t rx_buffer[4096];

void reset_handler(void);

__attribute__((section(".isr_vector"), used))
static void (*const vectors[48])(void) = {(void (*)(void))&_estack, reset_handler};

static uint32_t mix(uint32_t value) {
    for (int i = 0; i < 600; i++) {
        value = value * 31u + table[i] + (uint8_t)banner[i % sizeof banner];
    }
    return value;
}

void reset_handler(void) {
    uint32_t *src = &_sidata;
    for (uint32_t *dst = &_sdata; dst < &_edata;) *dst++ = *src++;
    for (uint32_t *dst = &_sbss; dst < &_ebss;) *dst++ = 0;
    for (;;) {
        counters[0] = mix(counters[0]);
        rx_buffer[counters[0] % sizeof rx_buffer]++;
    }
}
