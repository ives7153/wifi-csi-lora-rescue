#include "echoguard_lora.h"


uint32_t echoguard_lora_frequency_register(uint32_t frequency_hz)
{
    return (uint32_t)(((uint64_t)frequency_hz << 19) / ECHOGUARD_LORA_FXOSC_HZ);
}
