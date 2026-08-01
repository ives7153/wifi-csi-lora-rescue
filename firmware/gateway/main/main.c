#include <inttypes.h>
#include <errno.h>
#include <stdbool.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "echoguard_protocol.h"
#include "echoguard_lora.h"
#include "echoguard_region_protocol.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"
#include "nvs_flash.h"

/* Gateway 基本信息：SSID 由本地 sdkconfig 选择，密码不打印到运行日志。 */
#ifndef CONFIG_ECHOGUARD_GATEWAY_SSID
#define CONFIG_ECHOGUARD_GATEWAY_SSID "EchoGuard-GW-01"
#endif
#ifndef CONFIG_ECHOGUARD_GATEWAY_ID
#define CONFIG_ECHOGUARD_GATEWAY_ID "GW-01"
#endif
#ifndef CONFIG_ECHOGUARD_WIFI_PASSWORD
#define CONFIG_ECHOGUARD_WIFI_PASSWORD "511511511"
#endif
#define WIFI_SOFTAP_SSID          CONFIG_ECHOGUARD_GATEWAY_SSID
#define WIFI_SOFTAP_PASSWORD      CONFIG_ECHOGUARD_WIFI_PASSWORD
#define GATEWAY_ID                CONFIG_ECHOGUARD_GATEWAY_ID
#define GATEWAY_FIRMWARE_VERSION  "v0.4.0"
#define WIFI_SOFTAP_CHANNEL       6
#define WIFI_SOFTAP_MAX_CONN      4

/* LoRa/SX1278 引脚定义：与 hardware/readme.md 中 Gateway 接线保持一致。 */
#define LORA_SPI_HOST             SPI2_HOST
#define LORA_PIN_CS               GPIO_NUM_10
#define LORA_PIN_SCK              GPIO_NUM_12
#define LORA_PIN_MOSI             GPIO_NUM_11
#define LORA_PIN_MISO             GPIO_NUM_13
#define LORA_PIN_DIO0             GPIO_NUM_4
#define LORA_PIN_RST              GPIO_NUM_21

/* LoRa 空口参数：433 MHz、BW 125 kHz、SF7、CR 4/5、TX power 17 dBm。 */
#define LORA_FREQ_HZ              433000000UL
#define LORA_SPI_CLOCK_HZ         (4 * 1000 * 1000)
#define LORA_EXPECTED_VERSION     0x12
#define LORA_PACKET_RSSI_OFFSET   164

/* 队列和任务参数：包很小，队列长度 16 足够覆盖短时突发。 */
#define LORA_QUEUE_LENGTH         16
#define LORA_RX_PAYLOAD_LEN       ECHOGUARD_LORA_PAYLOAD_LEN
#define WIFI_TASK_STACK_SIZE      4096
#define LORA_TASK_STACK_SIZE      4096
#define SERIAL_TASK_STACK_SIZE    4096
#define UDP_KEEPALIVE_TASK_STACK_SIZE 4096
#define REGION_RX_TASK_STACK_SIZE 4096

/* Gateway 主动给 SoftAP 在线节点发 UDP 小包，制造稳定 WiFi 下行帧用于节点 CSI 采样。 */
#define UDP_KEEPALIVE_PORT        33333
#define UDP_KEEPALIVE_INTERVAL_MS 20

#define WIFI_AP_STARTED_BIT       BIT0

/* SX1278 常用寄存器地址。 */
#define REG_FIFO                  0x00
#define REG_OP_MODE               0x01
#define REG_FRF_MSB               0x06
#define REG_FRF_MID               0x07
#define REG_FRF_LSB               0x08
#define REG_PA_CONFIG             0x09
#define REG_LNA                   0x0C
#define REG_FIFO_ADDR_PTR         0x0D
#define REG_FIFO_TX_BASE_ADDR     0x0E
#define REG_FIFO_RX_BASE_ADDR     0x0F
#define REG_FIFO_RX_CURRENT_ADDR  0x10
#define REG_IRQ_FLAGS             0x12
#define REG_RX_NB_BYTES           0x13
#define REG_PKT_RSSI_VALUE        0x1A
#define REG_MODEM_CONFIG_1        0x1D
#define REG_MODEM_CONFIG_2        0x1E
#define REG_PREAMBLE_MSB          0x20
#define REG_PREAMBLE_LSB          0x21
#define REG_PAYLOAD_LENGTH        0x22
#define REG_MODEM_CONFIG_3        0x26
#define REG_DIO_MAPPING_1         0x40
#define REG_VERSION               0x42
#define REG_PA_DAC                0x4D

#define MODE_LONG_RANGE           0x80
#define MODE_SLEEP                0x00
#define MODE_STDBY                0x01
#define MODE_RX_CONTINUOUS        0x05

#define IRQ_RX_DONE               0x40
#define IRQ_PAYLOAD_CRC_ERROR     0x20

typedef struct {
    uint8_t id;
    uint32_t seq;
    uint8_t presence;
    uint8_t motion;
    uint8_t bpm;
    uint8_t conf;
    uint16_t gas;
    int16_t temp_x10;
    uint8_t hum;
    int16_t rssi;
    int64_t ts_ms;
} rescue_lora_packet_t;

typedef struct {
    echoguard_region_packet_t packet;
    int64_t received_ms;
} region_rx_packet_t;

static const char *TAG = "rescue_gateway";

static EventGroupHandle_t s_wifi_event_group;
static QueueHandle_t s_lora_queue;
static QueueHandle_t s_region_queue;
static spi_device_handle_t s_lora_spi;
static esp_netif_t *s_softap_netif;
static volatile uint32_t s_lora_rx_ok;
static volatile uint32_t s_lora_crc_errors;
static volatile uint32_t s_lora_bad_length;
static volatile uint32_t s_lora_parse_errors;
static volatile uint32_t s_lora_queue_drops;
static volatile uint32_t s_region_rx_ok;
static volatile uint32_t s_region_rx_invalid;
static volatile uint32_t s_region_queue_drops;

static void wifi_event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data);
static void udp_keepalive_task(void *arg);
static void region_feature_receive_task(void *arg);
static esp_err_t lora_spi_bus_init_once(void);
static esp_err_t lora_chip_configure(void);
static void lora_set_op_mode(uint8_t mode);
static void lora_write_reg(uint8_t reg, uint8_t value);
static uint8_t lora_read_reg(uint8_t reg);
static bool lora_receive_once(rescue_lora_packet_t *packet);
static bool parse_lora_payload(const uint8_t *payload, size_t len, rescue_lora_packet_t *packet);
static void gateway_print_status(void);
static bool format_region_json(const region_rx_packet_t *received, char *output, size_t capacity);
static size_t append_format(char *output, size_t capacity, size_t offset, const char *format, ...);

/* stdio 控制台由当前 sdkconfig 选择；自制 GW-02 板使用 UART0/CH340，
 * 同时保留 USB Serial/JTAG 作为第二输出。 */
static void serial_console_init(void)
{
    setvbuf(stdout, NULL, _IONBF, 0);
#if defined(CONFIG_ESP_CONSOLE_UART) && defined(CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG)
    ESP_LOGI(TAG, "UART0 console ready, secondary USB Serial/JTAG enabled, baud=115200");
#elif defined(CONFIG_ESP_CONSOLE_UART)
    ESP_LOGI(TAG, "UART console ready, baud=115200");
#elif defined(CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG)
    ESP_LOGI(TAG, "USB Serial/JTAG console ready");
#else
    ESP_LOGI(TAG, "configured stdio console ready");
#endif
}

/* WiFi SoftAP 任务：启动 WPA2-PSK 热点，提供纯局域网调试入口，不做外网转发。 */
static void wifi_softap_task(void *arg)
{
    (void)arg;

    ESP_LOGI(TAG, "Starting WPA2-PSK SoftAP: %s", WIFI_SOFTAP_SSID);

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    s_softap_netif = esp_netif_create_default_wifi_ap();
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                        wifi_event_handler, NULL, NULL));

    wifi_init_config_t wifi_init_config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wifi_init_config));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

    wifi_config_t wifi_config = {
        .ap = {
            .ssid = WIFI_SOFTAP_SSID,
            .ssid_len = strlen(WIFI_SOFTAP_SSID),
            .channel = WIFI_SOFTAP_CHANNEL,
            .password = WIFI_SOFTAP_PASSWORD,
            .max_connection = WIFI_SOFTAP_MAX_CONN,
            .authmode = WIFI_AUTH_WPA2_PSK,
            .pmf_cfg = {
                .required = false,
            },
        },
    };

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_AP, WIFI_BW_HT20));
    xEventGroupSetBits(s_wifi_event_group, WIFI_AP_STARTED_BIT);

    ESP_LOGI(TAG, "SoftAP started, ssid=%s, channel=%d, auth=WPA2-PSK",
             WIFI_SOFTAP_SSID, WIFI_SOFTAP_CHANNEL);

    /* WiFi 驱动启动后由系统任务维护；本任务保留心跳，便于现场判断 SoftAP 仍在线。 */
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(30000));
        ESP_LOGI(TAG, "SoftAP alive");
    }
}

static void wifi_event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    (void)arg;

    if (event_base != WIFI_EVENT) {
        return;
    }

    if (event_id == WIFI_EVENT_AP_STACONNECTED) {
        wifi_event_ap_staconnected_t *event = (wifi_event_ap_staconnected_t *)event_data;
        ESP_LOGI(TAG, "SoftAP STA connected mac=%02x:%02x:%02x:%02x:%02x:%02x aid=%d",
                 event->mac[0], event->mac[1], event->mac[2],
                 event->mac[3], event->mac[4], event->mac[5],
                 event->aid);
        return;
    }

    if (event_id == WIFI_EVENT_AP_STADISCONNECTED) {
        wifi_event_ap_stadisconnected_t *event = (wifi_event_ap_stadisconnected_t *)event_data;
        ESP_LOGI(TAG, "SoftAP STA disconnected mac=%02x:%02x:%02x:%02x:%02x:%02x aid=%d",
                 event->mac[0], event->mac[1], event->mac[2],
                 event->mac[3], event->mac[4], event->mac[5],
                 event->aid);
    }
}

static void udp_keepalive_task(void *arg)
{
    (void)arg;

    xEventGroupWaitBits(s_wifi_event_group, WIFI_AP_STARTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);

    uint32_t seq = 0;
    uint32_t warn_throttle = 0;

    while (true) {
        int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
        if (sock < 0) {
            ESP_LOGW(TAG, "UDP keepalive socket create failed: errno=%d", errno);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        ESP_LOGI(TAG, "UDP CSI keepalive started: port=%d interval=%dms",
                 UDP_KEEPALIVE_PORT, UDP_KEEPALIVE_INTERVAL_MS);

        while (true) {
            wifi_sta_list_t wifi_sta_list = {0};
            esp_err_t ret = esp_wifi_ap_get_sta_list(&wifi_sta_list);

            if (ret == ESP_OK && wifi_sta_list.num > 0 && s_softap_netif != NULL) {
                esp_netif_pair_mac_ip_t mac_ip_pairs[WIFI_SOFTAP_MAX_CONN] = {0};
                uint8_t pair_count = wifi_sta_list.num;
                if (pair_count > WIFI_SOFTAP_MAX_CONN) {
                    pair_count = WIFI_SOFTAP_MAX_CONN;
                }

                uint32_t cycle_seq = seq++;
                uint8_t slot = (uint8_t)(cycle_seq % 3U) + 1U;
                char payload[32] = {0};
                int payload_len = snprintf(payload, sizeof(payload),
                                           "EGSYNC:%" PRIu32 ":%u", cycle_seq, slot);
                for (int i = 0; i < pair_count; ++i) {
                    memcpy(mac_ip_pairs[i].mac, wifi_sta_list.sta[i].mac, sizeof(mac_ip_pairs[i].mac));
                }

                ret = esp_netif_dhcps_get_clients_by_mac(s_softap_netif, pair_count, mac_ip_pairs);
                if (ret != ESP_OK) {
                    if ((warn_throttle++ % 50U) == 0U) {
                        ESP_LOGW(TAG, "UDP keepalive DHCP client lookup unavailable: %s", esp_err_to_name(ret));
                    }
                    vTaskDelay(pdMS_TO_TICKS(UDP_KEEPALIVE_INTERVAL_MS));
                    continue;
                }

                for (int i = 0; i < pair_count; ++i) {
                    uint32_t ip_addr = mac_ip_pairs[i].ip.addr;
                    if (ip_addr == 0) {
                        continue;
                    }

                    struct sockaddr_in dest = {
                        .sin_family = AF_INET,
                        .sin_port = htons(UDP_KEEPALIVE_PORT),
                        .sin_addr.s_addr = ip_addr,
                    };
                    int sent = sendto(sock, payload, payload_len, 0,
                                      (struct sockaddr *)&dest, sizeof(dest));
                    if (sent < 0 && (warn_throttle++ % 50U) == 0U) {
                        ESP_LOGW(TAG, "UDP keepalive send failed ip=" IPSTR " errno=%d",
                                 IP2STR(&mac_ip_pairs[i].ip), errno);
                    }
                }
            }
            else if (ret != ESP_OK && (warn_throttle++ % 50U) == 0U) {
                ESP_LOGW(TAG, "UDP keepalive station list unavailable: %s", esp_err_to_name(ret));
            }

            vTaskDelay(pdMS_TO_TICKS(UDP_KEEPALIVE_INTERVAL_MS));
        }
    }
}

/* Node 的多链路 CSI 特征通过 WiFi UDP 上行，避免占用 LoRa 空口。 */
static void region_feature_receive_task(void *arg)
{
    (void)arg;
    xEventGroupWaitBits(s_wifi_event_group, WIFI_AP_STARTED_BIT,
                        pdFALSE, pdTRUE, portMAX_DELAY);

    while (true) {
        int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
        if (sock < 0) {
            ESP_LOGW(TAG, "region UDP socket create failed: errno=%d", errno);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        struct sockaddr_in local_addr = {
            .sin_family = AF_INET,
            .sin_port = htons(ECHOGUARD_REGION_FEATURE_PORT),
            .sin_addr.s_addr = htonl(INADDR_ANY),
        };
        if (bind(sock, (struct sockaddr *)&local_addr, sizeof(local_addr)) < 0) {
            ESP_LOGW(TAG, "region UDP bind failed: errno=%d", errno);
            close(sock);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        struct timeval timeout = {.tv_sec = 1, .tv_usec = 0};
        setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        ESP_LOGI(TAG, "triangle region UDP receiver ready: port=%u packet_len=%u",
                 ECHOGUARD_REGION_FEATURE_PORT, ECHOGUARD_REGION_PACKET_LEN);

        while (true) {
            uint8_t payload[ECHOGUARD_REGION_PACKET_LEN + 1U] = {0};
            struct sockaddr_in source = {0};
            socklen_t source_len = sizeof(source);
            int length = recvfrom(sock, payload, sizeof(payload), 0,
                                  (struct sockaddr *)&source, &source_len);
            if (length < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    continue;
                }
                ESP_LOGW(TAG, "region UDP receive failed: errno=%d", errno);
                break;
            }

            region_rx_packet_t received = {.received_ms = esp_timer_get_time() / 1000};
            if (!echoguard_region_packet_decode(payload, (size_t)length, &received.packet)) {
                s_region_rx_invalid++;
                if ((s_region_rx_invalid % 20U) == 1U) {
                    ESP_LOGW(TAG, "invalid region feature packet len=%d from=" IPSTR,
                             length, IP2STR((esp_ip4_addr_t *)&source.sin_addr));
                }
                continue;
            }
            s_region_rx_ok++;
            if (xQueueSend(s_region_queue, &received, 0) != pdTRUE) {
                s_region_queue_drops++;
            }
        }
        close(sock);
    }
}

/* LoRa 接收任务：初始化 SX1278，轮询 DIO0/IRQ 标志，收到合法帧后送入队列。 */
static void lora_receive_task(void *arg)
{
    (void)arg;

    while (lora_spi_bus_init_once() != ESP_OK) {
        ESP_LOGE(TAG, "LoRa SPI init failed, retrying");
        vTaskDelay(pdMS_TO_TICKS(2000));
    }

    while (lora_chip_configure() != ESP_OK) {
        ESP_LOGE(TAG, "SX1278 init failed, check wiring/power/antenna, retrying");
        vTaskDelay(pdMS_TO_TICKS(3000));
    }

    ESP_LOGI(TAG, "SX1278 receive mode ready: 433MHz BW125 SF7 CR4/5");

    while (true) {
        rescue_lora_packet_t packet = {0};

        if (lora_receive_once(&packet)) {
            if (xQueueSend(s_lora_queue, &packet, 0) != pdTRUE) {
                ++s_lora_queue_drops;
                ESP_LOGE(TAG, "LoRa queue full, dropping packet id=%u seq=%" PRIu32,
                         packet.id, packet.seq);
            }
        }

        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

/* 串口转发任务：统一输出 LoRa 节点数据与区域特征 JSON Lines。 */
static void serial_forward_task(void *arg)
{
    (void)arg;

    int64_t last_status_ms = -10000;
    while (true) {
        rescue_lora_packet_t packet = {0};
        if (xQueueReceive(s_lora_queue, &packet, pdMS_TO_TICKS(100)) == pdTRUE) {
            printf("{\"id\":%u,\"seq\":%" PRIu32 ",\"presence\":%u,\"motion\":%u,"
                   "\"bpm\":%u,\"conf\":%u,\"gas\":%u,\"temp\":%.1f,"
                   "\"hum\":%u,\"rssi\":%d,\"ts\":%" PRId64 "}\n",
                   packet.id,
                   packet.seq,
                   packet.presence,
                   packet.motion,
                   packet.bpm,
                   packet.conf,
                   packet.gas,
                   (double)packet.temp_x10 / 10.0,
                   packet.hum,
                   packet.rssi,
                   packet.ts_ms);
            fflush(stdout);
        }

        region_rx_packet_t region = {0};
        while (xQueueReceive(s_region_queue, &region, 0) == pdTRUE) {
            char json_line[1536] = {0};
            if (format_region_json(&region, json_line, sizeof(json_line))) {
                printf("%s\n", json_line);
                fflush(stdout);
            }
        }

        int64_t now_ms = esp_timer_get_time() / 1000;
        if (now_ms - last_status_ms >= 10000) {
            last_status_ms = now_ms;
            gateway_print_status();
        }
    }
}

static size_t append_format(
    char *output,
    size_t capacity,
    size_t offset,
    const char *format,
    ...
)
{
    if (output == NULL || format == NULL || offset >= capacity) {
        return capacity;
    }
    va_list args;
    va_start(args, format);
    int written = vsnprintf(output + offset, capacity - offset, format, args);
    va_end(args);
    if (written < 0 || (size_t)written >= capacity - offset) {
        return capacity;
    }
    return offset + (size_t)written;
}

static bool format_region_json(
    const region_rx_packet_t *received,
    char *output,
    size_t capacity
)
{
    if (received == NULL || output == NULL || capacity == 0U) {
        return false;
    }
    const echoguard_region_packet_t *packet = &received->packet;
    size_t offset = append_format(
        output, capacity, 0U,
        "{\"type\":\"csi_features\",\"v\":%u,\"gateway_id\":\"%s\"," \
        "\"node\":%u,\"node_mac\":\"%02x:%02x:%02x:%02x:%02x:%02x\"," \
        "\"seq\":%" PRIu32 ",\"epoch_ms\":%" PRIu32 "," \
        "\"flags\":%u,\"links\":[",
        ECHOGUARD_REGION_PROTOCOL_VERSION, GATEWAY_ID, packet->node_id,
        packet->node_mac[0], packet->node_mac[1], packet->node_mac[2],
        packet->node_mac[3], packet->node_mac[4], packet->node_mac[5],
        packet->sequence, packet->epoch_ms, packet->flags
    );
    for (uint8_t i = 0U; i < packet->link_count && offset < capacity; ++i) {
        const echoguard_region_link_t *link = &packet->links[i];
        offset = append_format(
            output, capacity, offset,
            "%s{\"src\":%u,\"valid\":%s,\"n\":%u,\"rssi\":%d," \
            "\"rssi_std\":%u,\"active\":%u,\"corr\":%u,\"mad\":[",
            i == 0U ? "" : ",", link->source_id,
            (link->flags & ECHOGUARD_REGION_LINK_FLAG_VALID) != 0U ? "true" : "false",
            link->sample_count, link->rssi_mean, link->rssi_std,
            link->active_ratio, link->correlation_delta
        );
        for (uint8_t band = 0U; band < ECHOGUARD_REGION_BAND_COUNT; ++band) {
            offset = append_format(output, capacity, offset, "%s%u",
                                   band == 0U ? "" : ",", link->mad_bands[band]);
        }
        offset = append_format(output, capacity, offset, "],\"diff\":[");
        for (uint8_t band = 0U; band < ECHOGUARD_REGION_BAND_COUNT; ++band) {
            offset = append_format(output, capacity, offset, "%s%u",
                                   band == 0U ? "" : ",", link->diff_bands[band]);
        }
        offset = append_format(output, capacity, offset, "]}");
    }
    offset = append_format(output, capacity, offset,
                           "],\"ts\":%" PRId64 "}", received->received_ms);
    return offset < capacity;
}

static void gateway_print_status(void)
{
    wifi_sta_list_t clients = {0};
    uint16_t client_count = 0;
    if (esp_wifi_ap_get_sta_list(&clients) == ESP_OK) {
        client_count = clients.num;
    }
    printf("{\"type\":\"gateway_status\",\"protocol\":%d,\"firmware\":\"%s\","
           "\"gateway_id\":\"%s\",\"ssid\":\"%s\",\"uptime_ms\":%" PRId64 ","
           "\"rx_ok\":%" PRIu32 ",\"crc_errors\":%" PRIu32 ","
           "\"bad_length\":%" PRIu32 ",\"parse_errors\":%" PRIu32 ","
           "\"queue_drops\":%" PRIu32 ",\"queue_depth\":%u,\"wifi_clients\":%u," \
           "\"region_protocol\":%u,\"region_rx_ok\":%" PRIu32 "," \
           "\"region_invalid\":%" PRIu32 ",\"region_queue_drops\":%" PRIu32 "}\n",
           ECHOGUARD_PROTOCOL_VERSION,
           GATEWAY_FIRMWARE_VERSION,
           GATEWAY_ID,
           WIFI_SOFTAP_SSID,
           esp_timer_get_time() / 1000,
           s_lora_rx_ok,
           s_lora_crc_errors,
           s_lora_bad_length,
           s_lora_parse_errors,
           s_lora_queue_drops,
           (unsigned)uxQueueMessagesWaiting(s_lora_queue),
           (unsigned)client_count,
           ECHOGUARD_REGION_PROTOCOL_VERSION,
           s_region_rx_ok,
           s_region_rx_invalid,
           s_region_queue_drops);
    fflush(stdout);
}

static void nvs_init_for_wifi(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "NVS needs erase before WiFi init, erasing");
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
}

void app_main(void)
{
    serial_console_init();
    ESP_LOGI(TAG, "EchoGuard Gateway v0.4.0 LoRa + triangle region bridge starting");

    nvs_init_for_wifi();

    s_wifi_event_group = xEventGroupCreate();
    s_lora_queue = xQueueCreate(LORA_QUEUE_LENGTH, sizeof(rescue_lora_packet_t));
    s_region_queue = xQueueCreate(LORA_QUEUE_LENGTH, sizeof(region_rx_packet_t));
    if (s_wifi_event_group == NULL || s_lora_queue == NULL || s_region_queue == NULL) {
        ESP_LOGE(TAG, "Failed to create WiFi event group or LoRa packet queue");
        return;
    }

    BaseType_t wifi_task_ok = xTaskCreate(wifi_softap_task, "wifi_softap", WIFI_TASK_STACK_SIZE,
                                          NULL, 5, NULL);
    BaseType_t udp_task_ok = xTaskCreate(udp_keepalive_task, "udp_keepalive",
                                         UDP_KEEPALIVE_TASK_STACK_SIZE, NULL, 4, NULL);
    BaseType_t lora_task_ok = xTaskCreate(lora_receive_task, "lora_receive", LORA_TASK_STACK_SIZE,
                                          NULL, 6, NULL);
    BaseType_t serial_task_ok = xTaskCreate(serial_forward_task, "serial_forward",
                                            SERIAL_TASK_STACK_SIZE + 2048, NULL, 5, NULL);
    BaseType_t region_task_ok = xTaskCreate(region_feature_receive_task, "region_receive",
                                            REGION_RX_TASK_STACK_SIZE, NULL, 5, NULL);

    if (wifi_task_ok != pdPASS || udp_task_ok != pdPASS ||
        lora_task_ok != pdPASS || serial_task_ok != pdPASS || region_task_ok != pdPASS) {
        ESP_LOGE(TAG, "Failed to create one or more gateway tasks");
    }
}

static esp_err_t lora_spi_bus_init_once(void)
{
    if (s_lora_spi != NULL) {
        return ESP_OK;
    }

    spi_bus_config_t bus_config = {
        .mosi_io_num = LORA_PIN_MOSI,
        .miso_io_num = LORA_PIN_MISO,
        .sclk_io_num = LORA_PIN_SCK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 64,
    };

    esp_err_t ret = spi_bus_initialize(LORA_SPI_HOST, &bus_config, SPI_DMA_CH_AUTO);
    if (ret != ESP_OK && ret != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "spi_bus_initialize failed: %s", esp_err_to_name(ret));
        return ret;
    }

    spi_device_interface_config_t dev_config = {
        .clock_speed_hz = LORA_SPI_CLOCK_HZ,
        .mode = 0,
        .spics_io_num = LORA_PIN_CS,
        .queue_size = 1,
    };

    ret = spi_bus_add_device(LORA_SPI_HOST, &dev_config, &s_lora_spi);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "spi_bus_add_device failed: %s", esp_err_to_name(ret));
        return ret;
    }

    gpio_config_t rst_config = {
        .pin_bit_mask = 1ULL << LORA_PIN_RST,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&rst_config));

    gpio_config_t dio0_config = {
        .pin_bit_mask = 1ULL << LORA_PIN_DIO0,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&dio0_config));

    return ESP_OK;
}

static esp_err_t lora_chip_configure(void)
{
    /* SX1278 硬复位：RST 低电平保持后释放，高电平等待晶振稳定。 */
    gpio_set_level(LORA_PIN_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(LORA_PIN_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(20));

    uint8_t version = lora_read_reg(REG_VERSION);
    if (version != LORA_EXPECTED_VERSION) {
        ESP_LOGE(TAG, "Unexpected SX1278 version=0x%02x, expected=0x%02x",
                 version, LORA_EXPECTED_VERSION);
        return ESP_ERR_INVALID_RESPONSE;
    }

    lora_set_op_mode(MODE_SLEEP);
    vTaskDelay(pdMS_TO_TICKS(10));

    const uint32_t frf = echoguard_lora_frequency_register(LORA_FREQ_HZ);
    lora_write_reg(REG_FRF_MSB, (uint8_t)(frf >> 16));
    lora_write_reg(REG_FRF_MID, (uint8_t)(frf >> 8));
    lora_write_reg(REG_FRF_LSB, (uint8_t)frf);

    lora_write_reg(REG_FIFO_TX_BASE_ADDR, 0x00);
    lora_write_reg(REG_FIFO_RX_BASE_ADDR, 0x00);
    lora_write_reg(REG_LNA, 0x23);

    /* BW=125 kHz(0x7), CR=4/5(0x1), 显式头模式。 */
    lora_write_reg(REG_MODEM_CONFIG_1, 0x72);
    /* SF=7，开启 payload CRC，符号超时高位为 0。 */
    lora_write_reg(REG_MODEM_CONFIG_2, 0x74);
    /* SF7/BW125 不需要低速率优化；开启 AGC 自动增益。 */
    lora_write_reg(REG_MODEM_CONFIG_3, 0x04);
    lora_write_reg(REG_PREAMBLE_MSB, 0x00);
    lora_write_reg(REG_PREAMBLE_LSB, 0x08);
    lora_write_reg(REG_PAYLOAD_LENGTH, LORA_RX_PAYLOAD_LEN);

    /* PA_BOOST 输出 17 dBm：0x80 | (17 - 2)，PA_DAC 保持常规 20 dBm 以下模式。 */
    lora_write_reg(REG_PA_CONFIG, 0x8F);
    lora_write_reg(REG_PA_DAC, 0x84);

    /* DIO0 映射为 RxDone，清空历史中断，进入连续接收模式。 */
    lora_write_reg(REG_DIO_MAPPING_1, 0x00);
    lora_write_reg(REG_IRQ_FLAGS, 0xFF);
    lora_write_reg(REG_FIFO_ADDR_PTR, 0x00);
    lora_set_op_mode(MODE_RX_CONTINUOUS);

    return ESP_OK;
}

static void lora_set_op_mode(uint8_t mode)
{
    lora_write_reg(REG_OP_MODE, MODE_LONG_RANGE | mode);
}

static void lora_write_reg(uint8_t reg, uint8_t value)
{
    uint8_t tx_data[2] = { (uint8_t)(reg | 0x80), value };
    spi_transaction_t transaction = {
        .length = 16,
        .tx_buffer = tx_data,
    };
    esp_err_t ret = spi_device_transmit(s_lora_spi, &transaction);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "LoRa write reg 0x%02x failed: %s", reg, esp_err_to_name(ret));
    }
}

static uint8_t lora_read_reg(uint8_t reg)
{
    uint8_t tx_data[2] = { (uint8_t)(reg & 0x7F), 0x00 };
    uint8_t rx_data[2] = {0};
    spi_transaction_t transaction = {
        .length = 16,
        .tx_buffer = tx_data,
        .rx_buffer = rx_data,
    };

    esp_err_t ret = spi_device_transmit(s_lora_spi, &transaction);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "LoRa read reg 0x%02x failed: %s", reg, esp_err_to_name(ret));
        return 0;
    }

    return rx_data[1];
}

static bool lora_receive_once(rescue_lora_packet_t *packet)
{
    uint8_t irq_flags = lora_read_reg(REG_IRQ_FLAGS);
    if ((irq_flags & IRQ_RX_DONE) == 0 && gpio_get_level(LORA_PIN_DIO0) == 0) {
        return false;
    }

    if ((irq_flags & IRQ_PAYLOAD_CRC_ERROR) != 0) {
        ++s_lora_crc_errors;
        ESP_LOGW(TAG, "LoRa CRC error, irq=0x%02x", irq_flags);
        lora_write_reg(REG_IRQ_FLAGS, 0xFF);
        lora_set_op_mode(MODE_RX_CONTINUOUS);
        return false;
    }

    uint8_t payload_len = lora_read_reg(REG_RX_NB_BYTES);
    uint8_t current_addr = lora_read_reg(REG_FIFO_RX_CURRENT_ADDR);
    int16_t rssi = (int16_t)lora_read_reg(REG_PKT_RSSI_VALUE) - LORA_PACKET_RSSI_OFFSET;

    lora_write_reg(REG_FIFO_ADDR_PTR, current_addr);

    uint8_t payload[64] = {0};
    uint8_t read_len = payload_len;
    if (read_len > sizeof(payload)) {
        read_len = sizeof(payload);
    }

    for (uint8_t i = 0; i < read_len; ++i) {
        payload[i] = lora_read_reg(REG_FIFO);
    }

    lora_write_reg(REG_IRQ_FLAGS, 0xFF);
    lora_set_op_mode(MODE_RX_CONTINUOUS);

    if (payload_len != LORA_RX_PAYLOAD_LEN) {
        ++s_lora_bad_length;
        ESP_LOGW(TAG, "Unexpected LoRa payload length=%u, expected=%u",
                 payload_len, LORA_RX_PAYLOAD_LEN);
        return false;
    }

    if (!parse_lora_payload(payload, payload_len, packet)) {
        ++s_lora_parse_errors;
        ESP_LOGW(TAG, "Failed to parse LoRa payload");
        return false;
    }

    packet->rssi = rssi;
    packet->ts_ms = esp_timer_get_time() / 1000;
    ++s_lora_rx_ok;
    return true;
}

static bool parse_lora_payload(const uint8_t *payload, size_t len, rescue_lora_packet_t *packet)
{
    if (payload == NULL || packet == NULL || len != LORA_RX_PAYLOAD_LEN) {
        return false;
    }

    echoguard_payload_t decoded = {0};
    if (!echoguard_payload_decode(payload, len, &decoded)) {
        return false;
    }
    packet->id = decoded.id;
    packet->seq = decoded.seq;
    packet->presence = decoded.presence;
    packet->motion = decoded.motion;
    packet->bpm = decoded.bpm;
    packet->conf = decoded.conf;
    packet->gas = decoded.gas;
    packet->temp_x10 = decoded.temp_x10;
    packet->hum = decoded.hum;

    return true;
}
