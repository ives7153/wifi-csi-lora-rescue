#include "echoguard_region_csi.h"

#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_now.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/portmacro.h"

#define REGION_NODE_MAX 3U
#define REGION_SOURCE_COUNT 4U
#define REGION_SUBCARRIER_COUNT 52U
#define REGION_PROBE_LEN 16U
#define REGION_ACTIVE_CV_THRESHOLD 0.06f
#define REGION_SYNC_TIMEOUT_MS 500U

typedef struct {
    uint32_t sample_count;
    int32_t rssi_sum;
    uint32_t rssi_sq_sum;
    uint64_t band_sum[ECHOGUARD_REGION_BAND_COUNT];
    uint64_t band_sq_sum[ECHOGUARD_REGION_BAND_COUNT];
    uint64_t band_diff_sum[ECHOGUARD_REGION_BAND_COUNT];
    uint64_t sub_sum[REGION_SUBCARRIER_COUNT];
    uint64_t sub_sq_sum[REGION_SUBCARRIER_COUNT];
    uint32_t correlation_delta_sum;
    uint16_t previous_bands[ECHOGUARD_REGION_BAND_COUNT];
    uint16_t previous_subcarriers[REGION_SUBCARRIER_COUNT];
    bool previous_valid;
} region_bucket_t;

static const char *TAG = "region_csi";
static const uint8_t BROADCAST_MAC[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static uint8_t s_node_id;
static uint8_t s_node_mac[6];
static bool s_initialized;
static bool s_enabled;
static bool s_gateway_bssid_valid;
static uint8_t s_gateway_bssid[6];
static uint8_t s_peer_mac[REGION_SOURCE_COUNT][6];
static bool s_peer_mac_valid[REGION_SOURCE_COUNT];
static uint32_t s_last_sync_ms;
static uint32_t s_probe_sequence;
static uint32_t s_packet_sequence;
static region_bucket_t s_current[REGION_SOURCE_COUNT];
static region_bucket_t s_previous[REGION_SOURCE_COUNT];

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000ULL);
}

static uint8_t clamp_u8(float value)
{
    if (value <= 0.0f) {
        return 0U;
    }
    if (value >= 255.0f) {
        return 255U;
    }
    return (uint8_t)lroundf(value);
}

static bool is_usable_subcarrier(int subcarrier)
{
    int absolute = abs(subcarrier);
    return subcarrier != 0 && absolute <= 28 &&
           absolute != 7 && absolute != 21;
}

static bool extract_normalized_subcarriers(
    const wifi_csi_info_t *info,
    uint16_t normalized[REGION_SUBCARRIER_COUNT]
)
{
    if (info == NULL || info->buf == NULL || info->len < 128U) {
        return false;
    }

    size_t pair_total = info->len / 2U;
    if (pair_total < 64U) {
        return false;
    }
    size_t ltf_start = pair_total - 64U;
    uint16_t amplitudes[REGION_SUBCARRIER_COUNT] = {0};
    uint32_t amplitude_sum = 0U;
    size_t output_index = 0U;

    for (size_t pair = 0; pair < 64U; ++pair) {
        int subcarrier = pair <= 31U ? (int)pair : (int)pair - 64;
        if (!is_usable_subcarrier(subcarrier)) {
            continue;
        }

        size_t byte_index = (ltf_start + pair) * 2U;
        int imaginary = info->buf[byte_index];
        int real = info->buf[byte_index + 1U];
        uint16_t amplitude = (uint16_t)(abs(imaginary) + abs(real));
        if (output_index >= REGION_SUBCARRIER_COUNT) {
            return false;
        }
        amplitudes[output_index++] = amplitude;
        amplitude_sum += amplitude;
    }

    if (output_index != REGION_SUBCARRIER_COUNT || amplitude_sum == 0U) {
        return false;
    }

    float mean = (float)amplitude_sum / (float)REGION_SUBCARRIER_COUNT;
    for (size_t i = 0; i < REGION_SUBCARRIER_COUNT; ++i) {
        float value = ((float)amplitudes[i] * 256.0f) / mean;
        normalized[i] = (uint16_t)fminf(1023.0f, fmaxf(0.0f, value));
    }
    return true;
}

static uint8_t source_id_for_mac(const uint8_t mac[6])
{
    if (s_gateway_bssid_valid && memcmp(mac, s_gateway_bssid, 6) == 0) {
        return ECHOGUARD_REGION_SOURCE_GATEWAY;
    }
    for (uint8_t id = 1U; id <= REGION_NODE_MAX; ++id) {
        if (id != s_node_id && s_peer_mac_valid[id] &&
            memcmp(mac, s_peer_mac[id], 6) == 0) {
            return id;
        }
    }
    return UINT8_MAX;
}

static void accumulate_frame(
    region_bucket_t *bucket,
    const uint16_t subcarriers[REGION_SUBCARRIER_COUNT],
    int8_t rssi
)
{
    uint16_t bands[ECHOGUARD_REGION_BAND_COUNT] = {0};
    uint16_t band_counts[ECHOGUARD_REGION_BAND_COUNT] = {0};
    for (size_t i = 0; i < REGION_SUBCARRIER_COUNT; ++i) {
        size_t band = (i * ECHOGUARD_REGION_BAND_COUNT) / REGION_SUBCARRIER_COUNT;
        if (band >= ECHOGUARD_REGION_BAND_COUNT) {
            band = ECHOGUARD_REGION_BAND_COUNT - 1U;
        }
        bands[band] = (uint16_t)(bands[band] + subcarriers[i]);
        band_counts[band]++;
        bucket->sub_sum[i] += subcarriers[i];
        bucket->sub_sq_sum[i] += (uint64_t)subcarriers[i] * subcarriers[i];
    }
    for (size_t band = 0; band < ECHOGUARD_REGION_BAND_COUNT; ++band) {
        if (band_counts[band] > 0U) {
            bands[band] = (uint16_t)(bands[band] / band_counts[band]);
        }
        bucket->band_sum[band] += bands[band];
        bucket->band_sq_sum[band] += (uint64_t)bands[band] * bands[band];
        if (bucket->previous_valid) {
            bucket->band_diff_sum[band] += (uint64_t)abs(
                (int)bands[band] - (int)bucket->previous_bands[band]
            );
        }
        bucket->previous_bands[band] = bands[band];
    }

    if (bucket->previous_valid) {
        uint64_t dot = 0U;
        uint64_t current_sq = 0U;
        uint64_t previous_sq = 0U;
        for (size_t i = 0; i < REGION_SUBCARRIER_COUNT; ++i) {
            dot += (uint64_t)subcarriers[i] * bucket->previous_subcarriers[i];
            current_sq += (uint64_t)subcarriers[i] * subcarriers[i];
            previous_sq += (uint64_t)bucket->previous_subcarriers[i] *
                           bucket->previous_subcarriers[i];
        }
        if (current_sq > 0U && previous_sq > 0U) {
            float cosine = (float)dot / sqrtf((float)current_sq * (float)previous_sq);
            float delta = (1.0f - fminf(1.0f, fmaxf(0.0f, cosine))) * 255.0f;
            bucket->correlation_delta_sum += clamp_u8(delta);
        }
    }
    memcpy(bucket->previous_subcarriers, subcarriers,
           sizeof(bucket->previous_subcarriers));
    bucket->previous_valid = true;
    bucket->sample_count++;
    bucket->rssi_sum += rssi;
    bucket->rssi_sq_sum += (uint32_t)((int32_t)rssi * rssi);
}

static region_bucket_t combine_buckets(
    const region_bucket_t *older,
    const region_bucket_t *newer
)
{
    region_bucket_t combined = {0};
    combined.sample_count = older->sample_count + newer->sample_count;
    combined.rssi_sum = older->rssi_sum + newer->rssi_sum;
    combined.rssi_sq_sum = older->rssi_sq_sum + newer->rssi_sq_sum;
    combined.correlation_delta_sum = older->correlation_delta_sum +
                                     newer->correlation_delta_sum;
    for (size_t band = 0; band < ECHOGUARD_REGION_BAND_COUNT; ++band) {
        combined.band_sum[band] = older->band_sum[band] + newer->band_sum[band];
        combined.band_sq_sum[band] = older->band_sq_sum[band] + newer->band_sq_sum[band];
        combined.band_diff_sum[band] = older->band_diff_sum[band] +
                                       newer->band_diff_sum[band];
    }
    for (size_t i = 0; i < REGION_SUBCARRIER_COUNT; ++i) {
        combined.sub_sum[i] = older->sub_sum[i] + newer->sub_sum[i];
        combined.sub_sq_sum[i] = older->sub_sq_sum[i] + newer->sub_sq_sum[i];
    }
    return combined;
}

static echoguard_region_link_t bucket_to_link(uint8_t source_id, const region_bucket_t *bucket)
{
    echoguard_region_link_t link = {.source_id = source_id};
    if (bucket->sample_count == 0U) {
        return link;
    }

    link.flags = ECHOGUARD_REGION_LINK_FLAG_VALID;
    link.sample_count = bucket->sample_count > UINT16_MAX ? UINT16_MAX :
                        (uint16_t)bucket->sample_count;
    float count = (float)bucket->sample_count;
    float rssi_mean = (float)bucket->rssi_sum / count;
    float rssi_variance = (float)bucket->rssi_sq_sum / count - rssi_mean * rssi_mean;
    link.rssi_mean = (int8_t)lroundf(fminf(0.0f, fmaxf(-127.0f, rssi_mean)));
    link.rssi_std = clamp_u8(sqrtf(fmaxf(0.0f, rssi_variance)) * 4.0f);

    for (size_t band = 0; band < ECHOGUARD_REGION_BAND_COUNT; ++band) {
        float mean = (float)bucket->band_sum[band] / count;
        float variance = (float)bucket->band_sq_sum[band] / count - mean * mean;
        float cv = sqrtf(fmaxf(0.0f, variance)) / fmaxf(1.0f, mean);
        link.mad_bands[band] = clamp_u8(cv * 800.0f);

        float diff_count = (float)(bucket->sample_count > 1U ?
                                   bucket->sample_count - 1U : 1U);
        float diff = (float)bucket->band_diff_sum[band] / diff_count;
        link.diff_bands[band] = clamp_u8((diff / fmaxf(1.0f, mean)) * 400.0f);
    }

    uint32_t active = 0U;
    for (size_t i = 0; i < REGION_SUBCARRIER_COUNT; ++i) {
        float mean = (float)bucket->sub_sum[i] / count;
        float variance = (float)bucket->sub_sq_sum[i] / count - mean * mean;
        float cv = sqrtf(fmaxf(0.0f, variance)) / fmaxf(1.0f, mean);
        if (cv >= REGION_ACTIVE_CV_THRESHOLD) {
            active++;
        }
    }
    link.active_ratio = clamp_u8(((float)active / REGION_SUBCARRIER_COUNT) * 255.0f);
    uint32_t correlation_count = bucket->sample_count > 1U ?
                                 bucket->sample_count - 1U : 1U;
    link.correlation_delta = (uint8_t)fminf(
        255.0f,
        (float)bucket->correlation_delta_sum / (float)correlation_count
    );
    return link;
}

static void espnow_receive_callback(
    const esp_now_recv_info_t *receive_info,
    const uint8_t *data,
    int length
)
{
    if (receive_info == NULL || receive_info->src_addr == NULL || data == NULL ||
        length != (int)REGION_PROBE_LEN || memcmp(data, "EGNP", 4) != 0 || data[4] != 1U) {
        return;
    }
    uint8_t source_id = data[5];
    if (source_id == 0U || source_id > REGION_NODE_MAX || source_id == s_node_id) {
        return;
    }
    uint32_t expected_crc = (uint32_t)data[12] |
                            ((uint32_t)data[13] << 8) |
                            ((uint32_t)data[14] << 16) |
                            ((uint32_t)data[15] << 24);
    if (expected_crc != echoguard_region_crc32(data, 12U)) {
        return;
    }

    portENTER_CRITICAL(&s_lock);
    bool changed = !s_peer_mac_valid[source_id] ||
                   memcmp(s_peer_mac[source_id], receive_info->src_addr, 6) != 0;
    memcpy(s_peer_mac[source_id], receive_info->src_addr, 6);
    s_peer_mac_valid[source_id] = true;
    portEXIT_CRITICAL(&s_lock);
    if (changed) {
        ESP_LOGI(TAG, "learned Node %u ESP-NOW MAC %02x:%02x:%02x:%02x:%02x:%02x",
                 source_id,
                 receive_info->src_addr[0], receive_info->src_addr[1],
                 receive_info->src_addr[2], receive_info->src_addr[3],
                 receive_info->src_addr[4], receive_info->src_addr[5]);
    }
}

esp_err_t echoguard_region_csi_init(uint8_t node_id)
{
    if (node_id == 0U || node_id > REGION_NODE_MAX) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_initialized) {
        return ESP_OK;
    }
    s_node_id = node_id;
    esp_err_t ret = esp_wifi_get_mac(WIFI_IF_STA, s_node_mac);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "esp_wifi_get_mac failed: %s", esp_err_to_name(ret));
        return ret;
    }

    ret = esp_now_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "esp_now_init failed: %s", esp_err_to_name(ret));
        return ret;
    }
    ret = esp_now_register_recv_cb(espnow_receive_callback);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "esp_now_register_recv_cb failed: %s", esp_err_to_name(ret));
        return ret;
    }

    esp_now_peer_info_t peer = {0};
    memcpy(peer.peer_addr, BROADCAST_MAC, sizeof(peer.peer_addr));
    peer.channel = 0U;
    peer.ifidx = WIFI_IF_STA;
    peer.encrypt = false;
    ret = esp_now_add_peer(&peer);
    if (ret != ESP_OK && ret != ESP_ERR_ESPNOW_EXIST) {
        ESP_LOGE(TAG, "esp_now_add_peer failed: %s", esp_err_to_name(ret));
        return ret;
    }

    esp_now_rate_config_t rate = {
        .phymode = WIFI_PHY_MODE_HT20,
        .rate = WIFI_PHY_RATE_MCS0_LGI,
        .ersu = false,
        .dcm = false,
    };
    ret = esp_now_set_peer_rate_config(BROADCAST_MAC, &rate);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "ESP-NOW MCS0 rate configuration failed: %s", esp_err_to_name(ret));
    }
    s_initialized = true;
    ESP_LOGI(TAG, "multi-link CSI initialized: node=%u HT20 MCS0", s_node_id);
    return ESP_OK;
}

void echoguard_region_csi_set_gateway(const uint8_t bssid[6], bool gateway_connected)
{
    if (bssid == NULL) {
        return;
    }
    portENTER_CRITICAL(&s_lock);
    memcpy(s_gateway_bssid, bssid, 6);
    s_gateway_bssid_valid = true;
    s_enabled = gateway_connected;
    s_last_sync_ms = 0U;
    memset(s_current, 0, sizeof(s_current));
    memset(s_previous, 0, sizeof(s_previous));
    portEXIT_CRITICAL(&s_lock);
    ESP_LOGI(TAG, "Gateway CSI source set, triangle mode=%s",
             gateway_connected ? "enabled" : "disabled");
}

void echoguard_region_csi_set_disconnected(void)
{
    portENTER_CRITICAL(&s_lock);
    s_enabled = false;
    s_gateway_bssid_valid = false;
    s_last_sync_ms = 0U;
    memset(s_current, 0, sizeof(s_current));
    memset(s_previous, 0, sizeof(s_previous));
    portEXIT_CRITICAL(&s_lock);
}

bool echoguard_region_csi_handle_sync(const char *payload, size_t length)
{
    if (!s_initialized || payload == NULL || length == 0U || length >= 40U) {
        return false;
    }
    char text[40] = {0};
    memcpy(text, payload, length);
    uint32_t sequence = 0U;
    unsigned slot = 0U;
    if (sscanf(text, "EGSYNC:%" SCNu32 ":%u", &sequence, &slot) != 2 ||
        slot == 0U || slot > REGION_NODE_MAX) {
        return false;
    }

    bool enabled;
    portENTER_CRITICAL(&s_lock);
    enabled = s_enabled;
    s_last_sync_ms = now_ms();
    portEXIT_CRITICAL(&s_lock);
    if (!enabled || slot != s_node_id) {
        return true;
    }

    uint8_t probe[REGION_PROBE_LEN] = {0};
    memcpy(probe, "EGNP", 4);
    probe[4] = 1U;
    probe[5] = s_node_id;
    uint32_t probe_sequence = s_probe_sequence++;
    probe[8] = (uint8_t)(probe_sequence & 0xFFU);
    probe[9] = (uint8_t)((probe_sequence >> 8) & 0xFFU);
    probe[10] = (uint8_t)((probe_sequence >> 16) & 0xFFU);
    probe[11] = (uint8_t)((probe_sequence >> 24) & 0xFFU);
    uint32_t crc = echoguard_region_crc32(probe, 12U);
    probe[12] = (uint8_t)(crc & 0xFFU);
    probe[13] = (uint8_t)((crc >> 8) & 0xFFU);
    probe[14] = (uint8_t)((crc >> 16) & 0xFFU);
    probe[15] = (uint8_t)((crc >> 24) & 0xFFU);
    esp_err_t ret = esp_now_send(BROADCAST_MAC, probe, sizeof(probe));
    if (ret != ESP_OK && (sequence % 100U) == 0U) {
        ESP_LOGW(TAG, "ESP-NOW probe send failed: %s", esp_err_to_name(ret));
    }
    return true;
}

int echoguard_region_csi_ingest(const wifi_csi_info_t *info)
{
    if (info == NULL) {
        return -1;
    }

    portENTER_CRITICAL(&s_lock);
    uint8_t source_id = source_id_for_mac(info->mac);
    bool enabled = s_enabled;
    portEXIT_CRITICAL(&s_lock);
    if (source_id == UINT8_MAX) {
        return -1;
    }
    if (!enabled) {
        return source_id;
    }

    uint16_t subcarriers[REGION_SUBCARRIER_COUNT] = {0};
    if (!extract_normalized_subcarriers(info, subcarriers)) {
        return source_id;
    }

    portENTER_CRITICAL(&s_lock);
    accumulate_frame(&s_current[source_id], subcarriers, info->rx_ctrl.rssi);
    portEXIT_CRITICAL(&s_lock);
    return source_id;
}

bool echoguard_region_csi_snapshot(echoguard_region_packet_t *packet)
{
    if (packet == NULL || !s_initialized) {
        return false;
    }

    region_bucket_t windows[REGION_SOURCE_COUNT] = {0};
    bool enabled;
    uint32_t last_sync;
    portENTER_CRITICAL(&s_lock);
    enabled = s_enabled;
    last_sync = s_last_sync_ms;
    for (uint8_t source = 0U; source < REGION_SOURCE_COUNT; ++source) {
        windows[source] = combine_buckets(&s_previous[source], &s_current[source]);
        s_previous[source] = s_current[source];
        memset(&s_current[source], 0, sizeof(s_current[source]));
    }
    portEXIT_CRITICAL(&s_lock);
    if (!enabled) {
        return false;
    }

    memset(packet, 0, sizeof(*packet));
    packet->node_id = s_node_id;
    memcpy(packet->node_mac, s_node_mac, sizeof(packet->node_mac));
    packet->flags = ECHOGUARD_REGION_FLAG_GATEWAY_CONNECTED;
    uint32_t timestamp = now_ms();
    if (last_sync != 0U && timestamp - last_sync <= REGION_SYNC_TIMEOUT_MS) {
        packet->flags |= ECHOGUARD_REGION_FLAG_SYNC_OK;
    }
    packet->sequence = s_packet_sequence++;
    packet->epoch_ms = timestamp;
    packet->link_count = ECHOGUARD_REGION_MAX_LINKS;

    uint8_t output_index = 0U;
    packet->links[output_index++] = bucket_to_link(
        ECHOGUARD_REGION_SOURCE_GATEWAY,
        &windows[ECHOGUARD_REGION_SOURCE_GATEWAY]
    );
    for (uint8_t source = 1U; source <= REGION_NODE_MAX; ++source) {
        if (source == s_node_id) {
            continue;
        }
        packet->links[output_index++] = bucket_to_link(source, &windows[source]);
    }
    return true;
}
