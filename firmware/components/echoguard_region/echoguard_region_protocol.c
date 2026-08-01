#include "echoguard_region_protocol.h"

#include <string.h>

static const uint8_t REGION_MAGIC[4] = {'E', 'G', 'C', 'F'};

static void write_u16_le(uint8_t *output, uint16_t value)
{
    output[0] = (uint8_t)(value & 0xFFU);
    output[1] = (uint8_t)((value >> 8) & 0xFFU);
}

static void write_u32_le(uint8_t *output, uint32_t value)
{
    output[0] = (uint8_t)(value & 0xFFU);
    output[1] = (uint8_t)((value >> 8) & 0xFFU);
    output[2] = (uint8_t)((value >> 16) & 0xFFU);
    output[3] = (uint8_t)((value >> 24) & 0xFFU);
}

static uint16_t read_u16_le(const uint8_t *input)
{
    return (uint16_t)input[0] | ((uint16_t)input[1] << 8);
}

static uint32_t read_u32_le(const uint8_t *input)
{
    return (uint32_t)input[0] |
           ((uint32_t)input[1] << 8) |
           ((uint32_t)input[2] << 16) |
           ((uint32_t)input[3] << 24);
}

uint32_t echoguard_region_crc32(const uint8_t *data, size_t length)
{
    if (data == NULL) {
        return 0U;
    }

    uint32_t crc = 0xFFFFFFFFU;
    for (size_t i = 0; i < length; ++i) {
        crc ^= data[i];
        for (uint8_t bit = 0; bit < 8U; ++bit) {
            uint32_t mask = (uint32_t)-(int32_t)(crc & 1U);
            crc = (crc >> 1) ^ (0xEDB88320U & mask);
        }
    }
    return ~crc;
}

bool echoguard_region_packet_encode(
    const echoguard_region_packet_t *packet,
    uint8_t output[ECHOGUARD_REGION_PACKET_LEN]
)
{
    if (packet == NULL || output == NULL || packet->node_id == 0U ||
        packet->link_count > ECHOGUARD_REGION_MAX_LINKS) {
        return false;
    }

    memset(output, 0, ECHOGUARD_REGION_PACKET_LEN);
    memcpy(output, REGION_MAGIC, sizeof(REGION_MAGIC));
    output[4] = ECHOGUARD_REGION_PROTOCOL_VERSION;
    output[5] = packet->node_id;
    output[6] = packet->flags;
    output[7] = packet->link_count;
    write_u32_le(output + 8, packet->sequence);
    write_u32_le(output + 12, packet->epoch_ms);
    memcpy(output + 16, packet->node_mac, sizeof(packet->node_mac));

    size_t offset = ECHOGUARD_REGION_HEADER_LEN;
    for (uint8_t i = 0; i < ECHOGUARD_REGION_MAX_LINKS; ++i) {
        const echoguard_region_link_t *link = &packet->links[i];
        output[offset++] = link->source_id;
        output[offset++] = link->flags;
        write_u16_le(output + offset, link->sample_count);
        offset += 2;
        output[offset++] = (uint8_t)link->rssi_mean;
        output[offset++] = link->rssi_std;
        output[offset++] = link->active_ratio;
        output[offset++] = link->correlation_delta;
        memcpy(output + offset, link->mad_bands, ECHOGUARD_REGION_BAND_COUNT);
        offset += ECHOGUARD_REGION_BAND_COUNT;
        memcpy(output + offset, link->diff_bands, ECHOGUARD_REGION_BAND_COUNT);
        offset += ECHOGUARD_REGION_BAND_COUNT;
    }

    uint32_t crc = echoguard_region_crc32(output, ECHOGUARD_REGION_PACKET_LEN - 4U);
    write_u32_le(output + ECHOGUARD_REGION_PACKET_LEN - 4U, crc);
    return true;
}

bool echoguard_region_packet_decode(
    const uint8_t *payload,
    size_t length,
    echoguard_region_packet_t *packet
)
{
    if (payload == NULL || packet == NULL || length != ECHOGUARD_REGION_PACKET_LEN ||
        memcmp(payload, REGION_MAGIC, sizeof(REGION_MAGIC)) != 0 ||
        payload[4] != ECHOGUARD_REGION_PROTOCOL_VERSION || payload[5] == 0U ||
        payload[7] > ECHOGUARD_REGION_MAX_LINKS) {
        return false;
    }

    uint32_t expected_crc = read_u32_le(payload + ECHOGUARD_REGION_PACKET_LEN - 4U);
    uint32_t actual_crc = echoguard_region_crc32(payload, ECHOGUARD_REGION_PACKET_LEN - 4U);
    if (expected_crc != actual_crc) {
        return false;
    }

    memset(packet, 0, sizeof(*packet));
    packet->node_id = payload[5];
    packet->flags = payload[6];
    packet->link_count = payload[7];
    packet->sequence = read_u32_le(payload + 8);
    packet->epoch_ms = read_u32_le(payload + 12);
    memcpy(packet->node_mac, payload + 16, sizeof(packet->node_mac));

    size_t offset = ECHOGUARD_REGION_HEADER_LEN;
    for (uint8_t i = 0; i < ECHOGUARD_REGION_MAX_LINKS; ++i) {
        echoguard_region_link_t *link = &packet->links[i];
        link->source_id = payload[offset++];
        link->flags = payload[offset++];
        link->sample_count = read_u16_le(payload + offset);
        offset += 2;
        link->rssi_mean = (int8_t)payload[offset++];
        link->rssi_std = payload[offset++];
        link->active_ratio = payload[offset++];
        link->correlation_delta = payload[offset++];
        memcpy(link->mad_bands, payload + offset, ECHOGUARD_REGION_BAND_COUNT);
        offset += ECHOGUARD_REGION_BAND_COUNT;
        memcpy(link->diff_bands, payload + offset, ECHOGUARD_REGION_BAND_COUNT);
        offset += ECHOGUARD_REGION_BAND_COUNT;
    }
    return true;
}
