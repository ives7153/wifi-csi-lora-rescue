#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define ECHOGUARD_REGION_PROTOCOL_VERSION 1U
#define ECHOGUARD_REGION_FEATURE_PORT 33334U
#define ECHOGUARD_REGION_BAND_COUNT 8U
#define ECHOGUARD_REGION_MAX_LINKS 3U
#define ECHOGUARD_REGION_LINK_ENCODED_LEN 24U
#define ECHOGUARD_REGION_HEADER_LEN 22U
#define ECHOGUARD_REGION_PACKET_LEN \
    (ECHOGUARD_REGION_HEADER_LEN + \
     ECHOGUARD_REGION_MAX_LINKS * ECHOGUARD_REGION_LINK_ENCODED_LEN + 4U)

#define ECHOGUARD_REGION_SOURCE_GATEWAY 0U
#define ECHOGUARD_REGION_FLAG_GW02_CONNECTED 0x01U
#define ECHOGUARD_REGION_FLAG_SYNC_OK 0x02U
#define ECHOGUARD_REGION_LINK_FLAG_VALID 0x01U

typedef struct {
    uint8_t source_id;
    uint8_t flags;
    uint16_t sample_count;
    int8_t rssi_mean;
    uint8_t rssi_std;
    uint8_t active_ratio;
    uint8_t correlation_delta;
    uint8_t mad_bands[ECHOGUARD_REGION_BAND_COUNT];
    uint8_t diff_bands[ECHOGUARD_REGION_BAND_COUNT];
} echoguard_region_link_t;

typedef struct {
    uint8_t node_id;
    uint8_t node_mac[6];
    uint8_t flags;
    uint32_t sequence;
    uint32_t epoch_ms;
    uint8_t link_count;
    echoguard_region_link_t links[ECHOGUARD_REGION_MAX_LINKS];
} echoguard_region_packet_t;

uint32_t echoguard_region_crc32(const uint8_t *data, size_t length);

bool echoguard_region_packet_encode(
    const echoguard_region_packet_t *packet,
    uint8_t output[ECHOGUARD_REGION_PACKET_LEN]
);

bool echoguard_region_packet_decode(
    const uint8_t *payload,
    size_t length,
    echoguard_region_packet_t *packet
);

#ifdef __cplusplus
}
#endif
