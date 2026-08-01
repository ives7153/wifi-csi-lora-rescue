#include "echoguard_protocol.h"


bool echoguard_payload_encode(
    const echoguard_payload_t *packet,
    uint8_t output[ECHOGUARD_LORA_PAYLOAD_LEN]
)
{
    if (packet == NULL || output == NULL) {
        return false;
    }

    output[0] = packet->id;
    output[1] = (uint8_t)(packet->seq & 0xFFU);
    output[2] = (uint8_t)((packet->seq >> 8) & 0xFFU);
    output[3] = (uint8_t)((packet->seq >> 16) & 0xFFU);
    output[4] = (uint8_t)((packet->seq >> 24) & 0xFFU);
    output[5] = packet->presence;
    output[6] = packet->motion;
    output[7] = packet->bpm;
    output[8] = packet->conf;
    output[9] = (uint8_t)(packet->gas & 0xFFU);
    output[10] = (uint8_t)((packet->gas >> 8) & 0xFFU);
    output[11] = (uint8_t)((uint16_t)packet->temp_x10 & 0xFFU);
    output[12] = (uint8_t)(((uint16_t)packet->temp_x10 >> 8) & 0xFFU);
    output[13] = packet->hum;
    return true;
}


bool echoguard_payload_decode(
    const uint8_t *payload,
    size_t length,
    echoguard_payload_t *packet
)
{
    if (payload == NULL || packet == NULL || length != ECHOGUARD_LORA_PAYLOAD_LEN) {
        return false;
    }

    packet->id = payload[0];
    packet->seq = ((uint32_t)payload[1]) |
                  ((uint32_t)payload[2] << 8) |
                  ((uint32_t)payload[3] << 16) |
                  ((uint32_t)payload[4] << 24);
    packet->presence = payload[5];
    packet->motion = payload[6];
    packet->bpm = payload[7];
    packet->conf = payload[8];
    packet->gas = (uint16_t)payload[9] | ((uint16_t)payload[10] << 8);
    packet->temp_x10 = (int16_t)((uint16_t)payload[11] | ((uint16_t)payload[12] << 8));
    packet->hum = payload[13];
    return true;
}
