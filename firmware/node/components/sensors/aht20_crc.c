#include "aht20_crc.h"


uint8_t aht20_crc8(const uint8_t *data, size_t length)
{
    uint8_t crc = 0xFFU;
    if (data == NULL) {
        return crc;
    }
    for (size_t index = 0; index < length; ++index) {
        crc ^= data[index];
        for (uint8_t bit = 0; bit < 8; ++bit) {
            crc = (crc & 0x80U) != 0U ? (uint8_t)((crc << 1) ^ 0x31U) : (uint8_t)(crc << 1);
        }
    }
    return crc;
}


bool aht20_measurement_crc_valid(const uint8_t data[7])
{
    return data != NULL && aht20_crc8(data, 6) == data[6];
}
