#include "wifi_gateway_selector.h"

#include <stddef.h>


int echoguard_select_gateway(
    const echoguard_gateway_candidate_t candidates[2],
    uint8_t primary_failures,
    uint8_t primary_failure_limit
)
{
    if (candidates == NULL) {
        return ECHOGUARD_GATEWAY_NONE;
    }
    if (candidates[ECHOGUARD_GATEWAY_PRIMARY].visible &&
        (primary_failures < primary_failure_limit ||
         !candidates[ECHOGUARD_GATEWAY_SECONDARY].visible)) {
        return ECHOGUARD_GATEWAY_PRIMARY;
    }
    if (candidates[ECHOGUARD_GATEWAY_SECONDARY].visible) {
        return ECHOGUARD_GATEWAY_SECONDARY;
    }
    return ECHOGUARD_GATEWAY_NONE;
}
