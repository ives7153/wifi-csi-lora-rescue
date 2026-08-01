#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define ECHOGUARD_LORA_FXOSC_HZ 32000000UL

uint32_t echoguard_lora_frequency_register(uint32_t frequency_hz);

#ifdef __cplusplus
}
#endif
