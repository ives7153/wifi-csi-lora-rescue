#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define ECHOGUARD_PROTOCOL_VERSION 1
#define ECHOGUARD_LORA_PAYLOAD_LEN 14

typedef struct {
    uint8_t id;
    uint32_t seq;
    uint8_t presence;
    uint8_t motion;
    uint8_t bpm;
    uint8_t conf;
    uint16_t gas;
    int16_t temp_x10;
    uint8_t hum;
} echoguard_payload_t;

bool echoguard_payload_encode(
    const echoguard_payload_t *packet,
    uint8_t output[ECHOGUARD_LORA_PAYLOAD_LEN]
);

bool echoguard_payload_decode(
    const uint8_t *payload,
    size_t length,
    echoguard_payload_t *packet
);

#ifdef __cplusplus
}
#endif
