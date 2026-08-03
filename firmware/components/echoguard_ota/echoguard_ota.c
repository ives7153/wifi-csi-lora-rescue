#include "echoguard_ota.h"

#include <ctype.h>
#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "sdkconfig.h"

#ifdef CONFIG_ECHOGUARD_OTA_ENABLED

#include "esp_app_desc.h"
#include "esp_app_format.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mbedtls/sha256.h"

#define OTA_HTTP_PORT 80
#define OTA_BUFFER_SIZE 4096U
#define OTA_SHA256_LEN 32U
#define OTA_SHA256_HEX_LEN (OTA_SHA256_LEN * 2U)
#define OTA_MIN_TOKEN_LEN 16U
#define OTA_RECV_TIMEOUT_RETRIES 3
#define OTA_HEALTH_CONFIRM_DELAY_MS 5000U
#define OTA_PROJECT_NAME "wifi_csi_lora_gateway"

typedef struct {
    uint32_t major;
    uint32_t minor;
    uint32_t patch;
} ota_semver_t;

typedef struct {
    char state[24];
    char last_error[96];
    size_t received;
    size_t total;
    bool upload_busy;
    bool pending_verify;
    bool softap_ready;
    bool server_ready;
    bool lora_ready;
    bool health_task_started;
} ota_runtime_state_t;

static const char *TAG = "echoguard_ota";
static SemaphoreHandle_t s_lock;
static httpd_handle_t s_http_server;
static char s_gateway_id[16];
static ota_runtime_state_t s_runtime = {
    .state = "disabled",
};

static esp_err_t ota_status_handler(httpd_req_t *req);
static esp_err_t ota_upload_handler(httpd_req_t *req);
static void maybe_start_health_confirmation(void);

static void state_lock(void)
{
    if (s_lock != NULL) {
        xSemaphoreTake(s_lock, portMAX_DELAY);
    }
}

static void state_unlock(void)
{
    if (s_lock != NULL) {
        xSemaphoreGive(s_lock);
    }
}

static void copy_text(char *destination, size_t capacity, const char *source)
{
    if (destination == NULL || capacity == 0U) {
        return;
    }
    snprintf(destination, capacity, "%s", source == NULL ? "" : source);
}

static void set_runtime_state(
    const char *state,
    const char *error,
    size_t received,
    size_t total,
    bool upload_busy
)
{
    state_lock();
    copy_text(s_runtime.state, sizeof(s_runtime.state), state);
    copy_text(s_runtime.last_error, sizeof(s_runtime.last_error), error);
    s_runtime.received = received;
    s_runtime.total = total;
    s_runtime.upload_busy = upload_busy;
    state_unlock();
}

static bool secure_text_equal(const char *left, const char *right)
{
    if (left == NULL || right == NULL) {
        return false;
    }
    size_t left_len = strlen(left);
    size_t right_len = strlen(right);
    size_t compared_len = left_len > right_len ? left_len : right_len;
    unsigned int difference = (unsigned int)(left_len ^ right_len);
    for (size_t i = 0; i < compared_len; ++i) {
        unsigned char a = i < left_len ? (unsigned char)left[i] : 0U;
        unsigned char b = i < right_len ? (unsigned char)right[i] : 0U;
        difference |= (unsigned int)(a ^ b);
    }
    return difference == 0U;
}

static bool parse_semver(const char *text, ota_semver_t *version)
{
    if (text == NULL || version == NULL) {
        return false;
    }
    const char *cursor = text;
    if (*cursor == 'v' || *cursor == 'V') {
        ++cursor;
    }

    uint32_t values[3] = {0U, 0U, 0U};
    for (size_t index = 0; index < 3U; ++index) {
        if (!isdigit((unsigned char)*cursor)) {
            return false;
        }
        errno = 0;
        char *end = NULL;
        unsigned long parsed = strtoul(cursor, &end, 10);
        if (errno != 0 || end == cursor || parsed > UINT32_MAX) {
            return false;
        }
        values[index] = (uint32_t)parsed;
        cursor = end;
        if (index < 2U) {
            if (*cursor != '.') {
                return false;
            }
            ++cursor;
        }
    }
    if (*cursor != '\0') {
        return false;
    }
    version->major = values[0];
    version->minor = values[1];
    version->patch = values[2];
    return true;
}

static int compare_semver(const ota_semver_t *left, const ota_semver_t *right)
{
    if (left->major != right->major) {
        return left->major > right->major ? 1 : -1;
    }
    if (left->minor != right->minor) {
        return left->minor > right->minor ? 1 : -1;
    }
    if (left->patch != right->patch) {
        return left->patch > right->patch ? 1 : -1;
    }
    return 0;
}

static bool get_required_header(
    httpd_req_t *req,
    const char *name,
    char *output,
    size_t capacity
)
{
    size_t length = httpd_req_get_hdr_value_len(req, name);
    if (length == 0U || length >= capacity) {
        return false;
    }
    return httpd_req_get_hdr_value_str(req, name, output, capacity) == ESP_OK;
}

static bool parse_sha256_hex(const char *hex, uint8_t output[OTA_SHA256_LEN])
{
    if (hex == NULL || output == NULL || strlen(hex) != OTA_SHA256_HEX_LEN) {
        return false;
    }
    for (size_t i = 0; i < OTA_SHA256_LEN; ++i) {
        int high = isdigit((unsigned char)hex[i * 2U])
            ? hex[i * 2U] - '0'
            : tolower((unsigned char)hex[i * 2U]) - 'a' + 10;
        int low = isdigit((unsigned char)hex[i * 2U + 1U])
            ? hex[i * 2U + 1U] - '0'
            : tolower((unsigned char)hex[i * 2U + 1U]) - 'a' + 10;
        if (high < 0 || high > 15 || low < 0 || low > 15) {
            return false;
        }
        output[i] = (uint8_t)((high << 4) | low);
    }
    return true;
}

static bool digest_equal(
    const uint8_t left[OTA_SHA256_LEN],
    const uint8_t right[OTA_SHA256_LEN]
)
{
    uint8_t difference = 0U;
    for (size_t i = 0; i < OTA_SHA256_LEN; ++i) {
        difference |= left[i] ^ right[i];
    }
    return difference == 0U;
}

static esp_err_t send_json(
    httpd_req_t *req,
    const char *http_status,
    const char *payload
)
{
    httpd_resp_set_status(req, http_status);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_sendstr(req, payload);
}

static esp_err_t send_json_error(
    httpd_req_t *req,
    const char *http_status,
    const char *code,
    const char *message
)
{
    char payload[256] = {0};
    snprintf(payload, sizeof(payload),
             "{\"ok\":false,\"error\":\"%s\",\"message\":\"%s\"}",
             code, message);
    return send_json(req, http_status, payload);
}

static esp_err_t fail_upload(
    httpd_req_t *req,
    esp_ota_handle_t update_handle,
    bool ota_started,
    mbedtls_sha256_context *sha_context,
    bool sha_started,
    const char *http_status,
    const char *code,
    const char *message,
    size_t received,
    size_t total
)
{
    if (ota_started) {
        esp_ota_abort(update_handle);
    }
    if (sha_started && sha_context != NULL) {
        mbedtls_sha256_free(sha_context);
    }
    set_runtime_state("failed", message, received, total, false);
    ESP_LOGE(TAG, "OTA failed: %s (%s)", code, message);
    return send_json_error(req, http_status, code, message);
}

static bool validate_image_descriptor(
    const uint8_t *prefix,
    size_t length,
    const char *requested_version,
    char image_version[sizeof(((esp_app_desc_t *)0)->version)]
)
{
    const size_t descriptor_offset = sizeof(esp_image_header_t) +
                                     sizeof(esp_image_segment_header_t);
    if (prefix == NULL || length < descriptor_offset + sizeof(esp_app_desc_t) ||
        prefix[0] != ESP_IMAGE_HEADER_MAGIC) {
        return false;
    }

    esp_app_desc_t descriptor = {0};
    memcpy(&descriptor, prefix + descriptor_offset, sizeof(descriptor));
    if (descriptor.magic_word != ESP_APP_DESC_MAGIC_WORD) {
        return false;
    }

    char project_name[sizeof(descriptor.project_name) + 1U] = {0};
    char descriptor_version[sizeof(descriptor.version) + 1U] = {0};
    memcpy(project_name, descriptor.project_name, sizeof(descriptor.project_name));
    memcpy(descriptor_version, descriptor.version, sizeof(descriptor.version));
    if (strcmp(project_name, OTA_PROJECT_NAME) != 0) {
        return false;
    }

    ota_semver_t requested = {0};
    ota_semver_t embedded = {0};
    if (!parse_semver(requested_version, &requested) ||
        !parse_semver(descriptor_version, &embedded) ||
        compare_semver(&requested, &embedded) != 0) {
        return false;
    }

    copy_text(image_version, sizeof(descriptor.version), descriptor_version);
    return true;
}

static int receive_chunk_with_retry(
    httpd_req_t *req,
    char *buffer,
    size_t capacity
)
{
    int timeout_count = 0;
    while (timeout_count <= OTA_RECV_TIMEOUT_RETRIES) {
        int received = httpd_req_recv(req, buffer, capacity);
        if (received != HTTPD_SOCK_ERR_TIMEOUT) {
            return received;
        }
        ++timeout_count;
    }
    return HTTPD_SOCK_ERR_TIMEOUT;
}

static void reboot_task(void *arg)
{
    (void)arg;
    vTaskDelay(pdMS_TO_TICKS(1500));
    esp_restart();
    vTaskDelete(NULL);
}

static void health_confirmation_task(void *arg)
{
    (void)arg;
    vTaskDelay(pdMS_TO_TICKS(OTA_HEALTH_CONFIRM_DELAY_MS));

    state_lock();
    bool healthy = s_runtime.pending_verify && s_runtime.softap_ready &&
                   s_runtime.server_ready && s_runtime.lora_ready;
    state_unlock();
    if (!healthy) {
        state_lock();
        s_runtime.health_task_started = false;
        state_unlock();
        vTaskDelete(NULL);
        return;
    }

    esp_err_t result = esp_ota_mark_app_valid_cancel_rollback();
    state_lock();
    if (result == ESP_OK) {
        s_runtime.pending_verify = false;
        copy_text(s_runtime.state, sizeof(s_runtime.state), "idle");
        s_runtime.last_error[0] = '\0';
        ESP_LOGI(TAG, "OTA health check passed; rollback cancelled");
    } else {
        copy_text(s_runtime.state, sizeof(s_runtime.state), "failed");
        copy_text(s_runtime.last_error, sizeof(s_runtime.last_error),
                  "failed to confirm boot health");
        ESP_LOGE(TAG, "Failed to confirm OTA image: %s", esp_err_to_name(result));
    }
    state_unlock();
    vTaskDelete(NULL);
}

static void maybe_start_health_confirmation(void)
{
    state_lock();
    bool should_start = s_runtime.pending_verify && s_runtime.softap_ready &&
                        s_runtime.server_ready && s_runtime.lora_ready &&
                        !s_runtime.health_task_started;
    if (should_start) {
        s_runtime.health_task_started = true;
    }
    state_unlock();

    if (should_start) {
        if (xTaskCreate(health_confirmation_task, "ota_health", 3072,
                        NULL, 4, NULL) != pdPASS) {
            state_lock();
            s_runtime.health_task_started = false;
            copy_text(s_runtime.state, sizeof(s_runtime.state), "failed");
            copy_text(s_runtime.last_error, sizeof(s_runtime.last_error),
                      "failed to start boot health task");
            state_unlock();
        }
    }
}

static esp_err_t ota_status_handler(httpd_req_t *req)
{
    ota_runtime_state_t runtime = {0};
    state_lock();
    runtime = s_runtime;
    state_unlock();

    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *next = esp_ota_get_next_update_partition(NULL);
    esp_app_desc_t description = {0};
    if (running == NULL ||
        esp_ota_get_partition_description(running, &description) != ESP_OK) {
        return send_json_error(req, HTTPD_500, "status_unavailable",
                               "running application description unavailable");
    }

    char target[32] = {0};
    snprintf(target, sizeof(target), "gateway:%s", s_gateway_id);
    char payload[768] = {0};
    snprintf(payload, sizeof(payload),
             "{\"ok\":true,\"enabled\":true,\"gateway_id\":\"%s\"," \
             "\"target\":\"%s\",\"project\":\"%s\",\"version\":\"%s\"," \
             "\"idf_version\":\"%s\",\"state\":\"%s\",\"pending_verify\":%s," \
             "\"softap_ready\":%s,\"server_ready\":%s,\"lora_ready\":%s," \
             "\"running_partition\":\"%s\",\"next_partition\":\"%s\"," \
             "\"next_size\":%" PRIu32 ",\"received\":%u,\"total\":%u," \
             "\"last_error\":\"%s\"}",
             s_gateway_id, target, description.project_name, description.version,
             description.idf_ver, runtime.state,
             runtime.pending_verify ? "true" : "false",
             runtime.softap_ready ? "true" : "false",
             runtime.server_ready ? "true" : "false",
             runtime.lora_ready ? "true" : "false",
             running->label, next == NULL ? "" : next->label,
             next == NULL ? 0U : next->size,
             (unsigned)runtime.received, (unsigned)runtime.total,
             runtime.last_error);
    return send_json(req, "200 OK", payload);
}

static esp_err_t ota_upload_handler(httpd_req_t *req)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *update_partition = esp_ota_get_next_update_partition(NULL);
    if (running == NULL || update_partition == NULL) {
        return send_json_error(req, HTTPD_500, "ota_partition_missing",
                               "dual OTA partitions are not available");
    }

    state_lock();
    bool upload_busy = s_runtime.upload_busy;
    bool pending_verify = s_runtime.pending_verify;
    state_unlock();
    if (upload_busy) {
        return send_json_error(req, "409 Conflict", "ota_busy",
                               "another OTA upload is active");
    }
    if (pending_verify) {
        return send_json_error(req, "409 Conflict", "boot_not_confirmed",
                               "current application health is not confirmed");
    }

    if (req->content_len <= 0) {
        return send_json_error(req, "411 Length Required", "length_required",
                               "Content-Length is required");
    }
    size_t total_size = (size_t)req->content_len;
    const size_t minimum_image_size = sizeof(esp_image_header_t) +
                                      sizeof(esp_image_segment_header_t) +
                                      sizeof(esp_app_desc_t);
    if (total_size < minimum_image_size || total_size > update_partition->size) {
        return send_json_error(req, "413 Payload Too Large", "invalid_image_size",
                               "firmware does not fit the inactive OTA partition");
    }

    char content_type[48] = {0};
    char token[96] = {0};
    char sha_hex[OTA_SHA256_HEX_LEN + 1U] = {0};
    char requested_version[40] = {0};
    char requested_target[40] = {0};
    if (!get_required_header(req, "Content-Type", content_type, sizeof(content_type)) ||
        strcmp(content_type, "application/octet-stream") != 0) {
        return send_json_error(req, "415 Unsupported Media Type", "invalid_content_type",
                               "Content-Type must be application/octet-stream");
    }
    if (!get_required_header(req, "X-EchoGuard-OTA-Token", token, sizeof(token)) ||
        !secure_text_equal(token, CONFIG_ECHOGUARD_OTA_TOKEN)) {
        return send_json_error(req, "401 Unauthorized", "unauthorized",
                               "invalid OTA token");
    }
    if (!get_required_header(req, "X-EchoGuard-SHA256", sha_hex, sizeof(sha_hex)) ||
        !get_required_header(req, "X-EchoGuard-Version", requested_version,
                             sizeof(requested_version)) ||
        !get_required_header(req, "X-EchoGuard-Target", requested_target,
                             sizeof(requested_target))) {
        return send_json_error(req, HTTPD_400, "missing_headers",
                               "SHA256, version and target headers are required");
    }

    char expected_target[40] = {0};
    snprintf(expected_target, sizeof(expected_target), "gateway:%s", s_gateway_id);
    if (strcmp(requested_target, expected_target) != 0) {
        return send_json_error(req, "422 Unprocessable Entity", "target_mismatch",
                               "firmware target does not match this Gateway");
    }

    uint8_t expected_sha[OTA_SHA256_LEN] = {0};
    if (!parse_sha256_hex(sha_hex, expected_sha)) {
        return send_json_error(req, HTTPD_400, "invalid_sha256",
                               "X-EchoGuard-SHA256 must contain 64 hex characters");
    }

    state_lock();
    if (s_runtime.upload_busy) {
        state_unlock();
        return send_json_error(req, "409 Conflict", "ota_busy",
                               "another OTA upload is active");
    }
    s_runtime.upload_busy = true;
    s_runtime.received = 0U;
    s_runtime.total = total_size;
    copy_text(s_runtime.state, sizeof(s_runtime.state), "receiving");
    s_runtime.last_error[0] = '\0';
    state_unlock();

    uint8_t *buffer = malloc(OTA_BUFFER_SIZE);
    if (buffer == NULL) {
        set_runtime_state("failed", "out of memory", 0U, total_size, false);
        return send_json_error(req, HTTPD_500, "out_of_memory",
                               "failed to allocate OTA receive buffer");
    }

    esp_ota_handle_t update_handle = 0;
    bool ota_started = false;
    mbedtls_sha256_context sha_context;
    mbedtls_sha256_init(&sha_context);
    bool sha_started = false;
    size_t received_total = 0U;

    while (received_total < minimum_image_size) {
        size_t capacity = OTA_BUFFER_SIZE - received_total;
        size_t remaining = total_size - received_total;
        if (capacity > remaining) {
            capacity = remaining;
        }
        int received = receive_chunk_with_retry(
            req, (char *)buffer + received_total, capacity
        );
        if (received <= 0) {
            free(buffer);
            return fail_upload(req, update_handle, ota_started, &sha_context, sha_started,
                               HTTPD_408, "receive_failed", "firmware upload was interrupted",
                               received_total, total_size);
        }
        received_total += (size_t)received;
    }

    char image_version[32] = {0};
    if (!validate_image_descriptor(buffer, received_total, requested_version,
                                   image_version)) {
        free(buffer);
        return fail_upload(req, update_handle, ota_started, &sha_context, sha_started,
                           "422 Unprocessable Entity", "invalid_gateway_image",
                           "image project or embedded version is invalid",
                           received_total, total_size);
    }

    esp_app_desc_t running_description = {0};
    ota_semver_t running_version = {0};
    ota_semver_t incoming_version = {0};
    if (esp_ota_get_partition_description(running, &running_description) != ESP_OK ||
        !parse_semver(running_description.version, &running_version) ||
        !parse_semver(image_version, &incoming_version) ||
        compare_semver(&incoming_version, &running_version) <= 0) {
        free(buffer);
        return fail_upload(req, update_handle, ota_started, &sha_context, sha_started,
                           "409 Conflict", "version_not_newer",
                           "firmware version must be newer than the running version",
                           received_total, total_size);
    }

    const esp_partition_t *last_invalid = esp_ota_get_last_invalid_partition();
    if (last_invalid != NULL) {
        esp_app_desc_t invalid_description = {0};
        if (esp_ota_get_partition_description(last_invalid, &invalid_description) == ESP_OK &&
            strncmp(invalid_description.version, image_version,
                    sizeof(invalid_description.version)) == 0) {
            free(buffer);
            return fail_upload(req, update_handle, ota_started, &sha_context, sha_started,
                               "409 Conflict", "previously_rolled_back",
                               "this firmware version previously failed boot validation",
                               received_total, total_size);
        }
    }

    if (mbedtls_sha256_starts(&sha_context, 0) != 0) {
        free(buffer);
        return fail_upload(req, update_handle, ota_started, &sha_context, sha_started,
                           HTTPD_500, "sha_init_failed", "failed to initialize SHA-256",
                           received_total, total_size);
    }
    sha_started = true;

    esp_err_t result = esp_ota_begin(update_partition, total_size, &update_handle);
    if (result != ESP_OK) {
        free(buffer);
        return fail_upload(req, update_handle, ota_started, &sha_context, sha_started,
                           HTTPD_500, "ota_begin_failed", esp_err_to_name(result),
                           received_total, total_size);
    }
    ota_started = true;
    if (mbedtls_sha256_update(&sha_context, buffer, received_total) != 0 ||
        esp_ota_write(update_handle, buffer, received_total) != ESP_OK) {
        free(buffer);
        return fail_upload(req, update_handle, ota_started, &sha_context, sha_started,
                           HTTPD_500, "ota_write_failed", "failed to write firmware prefix",
                           received_total, total_size);
    }
    set_runtime_state("receiving", "", received_total, total_size, true);

    while (received_total < total_size) {
        size_t remaining = total_size - received_total;
        size_t requested = remaining > OTA_BUFFER_SIZE ? OTA_BUFFER_SIZE : remaining;
        int received = receive_chunk_with_retry(req, (char *)buffer, requested);
        if (received <= 0) {
            free(buffer);
            return fail_upload(req, update_handle, ota_started, &sha_context, sha_started,
                               HTTPD_408, "receive_failed", "firmware upload was interrupted",
                               received_total, total_size);
        }
        if (mbedtls_sha256_update(&sha_context, buffer, (size_t)received) != 0 ||
            esp_ota_write(update_handle, buffer, (size_t)received) != ESP_OK) {
            received_total += (size_t)received;
            free(buffer);
            return fail_upload(req, update_handle, ota_started, &sha_context, sha_started,
                               HTTPD_500, "ota_write_failed", "failed to write firmware chunk",
                               received_total, total_size);
        }
        received_total += (size_t)received;
        set_runtime_state("receiving", "", received_total, total_size, true);
    }
    free(buffer);
    set_runtime_state("verifying", "", received_total, total_size, true);

    uint8_t actual_sha[OTA_SHA256_LEN] = {0};
    if (mbedtls_sha256_finish(&sha_context, actual_sha) != 0) {
        return fail_upload(req, update_handle, ota_started, &sha_context, sha_started,
                           HTTPD_500, "sha_finish_failed", "failed to finish SHA-256",
                           received_total, total_size);
    }
    mbedtls_sha256_free(&sha_context);
    sha_started = false;
    if (!digest_equal(actual_sha, expected_sha)) {
        return fail_upload(req, update_handle, ota_started, &sha_context, sha_started,
                           "422 Unprocessable Entity", "sha256_mismatch",
                           "uploaded firmware SHA-256 does not match the request",
                           received_total, total_size);
    }

    result = esp_ota_end(update_handle);
    ota_started = false;
    if (result != ESP_OK) {
        return fail_upload(req, update_handle, ota_started, &sha_context, sha_started,
                           "422 Unprocessable Entity", "image_validation_failed",
                           esp_err_to_name(result), received_total, total_size);
    }
    result = esp_ota_set_boot_partition(update_partition);
    if (result != ESP_OK) {
        return fail_upload(req, update_handle, ota_started, &sha_context, sha_started,
                           HTTPD_500, "boot_partition_failed", esp_err_to_name(result),
                           received_total, total_size);
    }

    set_runtime_state("rebooting", "", received_total, total_size, true);
    char response[256] = {0};
    snprintf(response, sizeof(response),
             "{\"ok\":true,\"state\":\"rebooting\",\"version\":\"%s\"," \
             "\"partition\":\"%s\",\"bytes\":%u}",
             image_version, update_partition->label, (unsigned)received_total);
    esp_err_t send_result = send_json(req, "200 OK", response);
    ESP_LOGI(TAG, "OTA image accepted: version=%s partition=%s bytes=%u",
             image_version, update_partition->label, (unsigned)received_total);
    if (xTaskCreate(reboot_task, "ota_reboot", 2048, NULL, 5, NULL) != pdPASS) {
        ESP_LOGE(TAG, "Failed to create OTA reboot task");
        esp_restart();
    }
    return send_result;
}

esp_err_t echoguard_ota_init(const char *gateway_id)
{
    if (gateway_id == NULL || gateway_id[0] == '\0' ||
        strlen(gateway_id) >= sizeof(s_gateway_id)) {
        return ESP_ERR_INVALID_ARG;
    }
    if (strlen(CONFIG_ECHOGUARD_OTA_TOKEN) < OTA_MIN_TOKEN_LEN) {
        ESP_LOGE(TAG, "OTA token must contain at least %u characters",
                 (unsigned)OTA_MIN_TOKEN_LEN);
        return ESP_ERR_INVALID_STATE;
    }
    if (strcmp(gateway_id, "GW-01") != 0) {
        ESP_LOGE(TAG, "LAN OTA is restricted to GW-01 builds");
        return ESP_ERR_NOT_SUPPORTED;
    }

    s_lock = xSemaphoreCreateMutex();
    if (s_lock == NULL) {
        return ESP_ERR_NO_MEM;
    }
    copy_text(s_gateway_id, sizeof(s_gateway_id), gateway_id);
    copy_text(s_runtime.state, sizeof(s_runtime.state), "idle");
    s_runtime.last_error[0] = '\0';

    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t image_state = ESP_OTA_IMG_UNDEFINED;
    if (running != NULL &&
        esp_ota_get_state_partition(running, &image_state) == ESP_OK &&
        image_state == ESP_OTA_IMG_PENDING_VERIFY) {
        s_runtime.pending_verify = true;
        copy_text(s_runtime.state, sizeof(s_runtime.state), "pending_health");
        ESP_LOGW(TAG, "New OTA image is pending SoftAP, HTTP and LoRa health confirmation");
    }
    return ESP_OK;
}

esp_err_t echoguard_ota_start(void)
{
    if (s_lock == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_http_server != NULL) {
        return ESP_OK;
    }

    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = OTA_HTTP_PORT;
    config.stack_size = 8192;
    config.max_uri_handlers = 2;
    config.recv_wait_timeout = 15;
    config.send_wait_timeout = 10;
    config.lru_purge_enable = true;

    esp_err_t result = httpd_start(&s_http_server, &config);
    if (result != ESP_OK) {
        return result;
    }

    const httpd_uri_t status_uri = {
        .uri = "/api/ota/status",
        .method = HTTP_GET,
        .handler = ota_status_handler,
        .user_ctx = NULL,
    };
    const httpd_uri_t upload_uri = {
        .uri = "/api/ota/upload",
        .method = HTTP_POST,
        .handler = ota_upload_handler,
        .user_ctx = NULL,
    };
    result = httpd_register_uri_handler(s_http_server, &status_uri);
    if (result == ESP_OK) {
        result = httpd_register_uri_handler(s_http_server, &upload_uri);
    }
    if (result != ESP_OK) {
        httpd_stop(s_http_server);
        s_http_server = NULL;
        return result;
    }

    state_lock();
    s_runtime.softap_ready = true;
    s_runtime.server_ready = true;
    state_unlock();
    ESP_LOGI(TAG, "Gateway LAN OTA ready on http://192.168.4.1:%d", OTA_HTTP_PORT);
    maybe_start_health_confirmation();
    return ESP_OK;
}

void echoguard_ota_notify_lora_ready(void)
{
    if (s_lock == NULL) {
        return;
    }
    state_lock();
    s_runtime.lora_ready = true;
    state_unlock();
    maybe_start_health_confirmation();
}

bool echoguard_ota_server_ready(void)
{
    state_lock();
    bool ready = s_runtime.server_ready;
    state_unlock();
    return ready;
}

#else

esp_err_t echoguard_ota_init(const char *gateway_id)
{
    (void)gateway_id;
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t echoguard_ota_start(void)
{
    return ESP_ERR_NOT_SUPPORTED;
}

void echoguard_ota_notify_lora_ready(void)
{
}

bool echoguard_ota_server_ready(void)
{
    return false;
}

#endif
