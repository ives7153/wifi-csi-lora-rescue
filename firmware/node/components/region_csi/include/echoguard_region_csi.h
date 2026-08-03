#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "echoguard_region_protocol.h"
#include "esp_err.h"
#include "esp_wifi.h"

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t echoguard_region_csi_init(uint8_t node_id);

void echoguard_region_csi_set_gateway(const uint8_t bssid[6], bool gateway_connected);

void echoguard_region_csi_set_disconnected(void);

bool echoguard_region_csi_handle_sync(const char *payload, size_t length);

/* 返回来源：0=Gateway，1~3=Node，-1=非 EchoGuard 区域链路。 */
int echoguard_region_csi_ingest(const wifi_csi_info_t *info);

bool echoguard_region_csi_snapshot(echoguard_region_packet_t *packet);

#ifdef __cplusplus
}
#endif
