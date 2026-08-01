#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define ECHOGUARD_GATEWAY_PRIMARY 0
#define ECHOGUARD_GATEWAY_SECONDARY 1
#define ECHOGUARD_GATEWAY_NONE (-1)

typedef struct {
    bool visible;
    uint8_t channel;
    int8_t rssi;
} echoguard_gateway_candidate_t;

int echoguard_select_gateway(
    const echoguard_gateway_candidate_t candidates[2],
    uint8_t primary_failures,
    uint8_t primary_failure_limit
);

#ifdef __cplusplus
}
#endif
