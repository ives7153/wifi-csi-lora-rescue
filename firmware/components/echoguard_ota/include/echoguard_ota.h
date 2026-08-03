#pragma once

#include <stdbool.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Initialize the boot rollback guard before Gateway worker tasks are started. */
esp_err_t echoguard_ota_init(const char *gateway_id);

/** Start the LAN HTTP OTA service after the SoftAP is ready. */
esp_err_t echoguard_ota_start(void);

/** Report that the SX1278 receive path initialized successfully. */
void echoguard_ota_notify_lora_ready(void);

/** Return whether the OTA HTTP server is accepting requests. */
bool echoguard_ota_server_ready(void);

#ifdef __cplusplus
}
#endif
