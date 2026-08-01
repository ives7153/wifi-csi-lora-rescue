#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

uint8_t aht20_crc8(const uint8_t *data, size_t length);
bool aht20_measurement_crc_valid(const uint8_t data[7]);

#ifdef __cplusplus
}
#endif
