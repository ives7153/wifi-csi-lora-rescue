#include <inttypes.h>
#include <errno.h>
#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/spi_master.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_check.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "echoguard_protocol.h"
#include "echoguard_region_csi.h"
#include "echoguard_region_protocol.h"
#include "aht20_crc.h"
#include "echoguard_lora.h"
#include "wifi_gateway_selector.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"
#include "nvs_flash.h"

/* 节点基本信息：GW-01 优先，GW-02 回退；两者使用相同 WPA2-PSK 密码。 */
#ifndef CONFIG_RESCUE_NODE_ID
#define CONFIG_RESCUE_NODE_ID           1
#endif
#ifndef CONFIG_ECHOGUARD_PRIMARY_SSID
#define CONFIG_ECHOGUARD_PRIMARY_SSID   "EchoGuard-GW-01"
#endif
#ifndef CONFIG_ECHOGUARD_SECONDARY_SSID
#define CONFIG_ECHOGUARD_SECONDARY_SSID "EchoGuard-GW-02"
#endif
#ifndef CONFIG_ECHOGUARD_WIFI_PASSWORD
#define CONFIG_ECHOGUARD_WIFI_PASSWORD  "511511511"
#endif
#define NODE_ID                         CONFIG_RESCUE_NODE_ID
#define WIFI_PRIMARY_SSID               CONFIG_ECHOGUARD_PRIMARY_SSID
#define WIFI_SECONDARY_SSID             CONFIG_ECHOGUARD_SECONDARY_SSID
#define WIFI_STA_PASSWORD               CONFIG_ECHOGUARD_WIFI_PASSWORD
#define WIFI_SCAN_MAX_APS               16
#define WIFI_RESELECT_DELAY_MS          2000
#define WIFI_CONNECT_TIMEOUT_MS         12000
#define WIFI_PRIMARY_FAILURE_LIMIT      3

/* FreeRTOS 任务参数：本固件只创建 3 个业务任务，优先级与栈大小集中写在这里便于现场调整。 */
#define WIFI_TASK_STACK_SIZE            6144
#define UDP_KEEPALIVE_TASK_STACK_SIZE   4096
#define CSI_SENSOR_TASK_STACK_SIZE      6144
#define LORA_SEND_TASK_STACK_SIZE       4096
#define REGION_UPLOAD_TASK_STACK_SIZE   4096
#define WIFI_TASK_PRIORITY              5
#define UDP_KEEPALIVE_TASK_PRIORITY     4
#define CSI_SENSOR_TASK_PRIORITY        6
#define LORA_SEND_TASK_PRIORITY         5
#define REGION_UPLOAD_TASK_PRIORITY     4

/* Gateway 向该端口发送轻量 UDP keepalive，用于制造稳定 WiFi 下行包并触发 CSI 回调。 */
#define UDP_KEEPALIVE_PORT              33333

/* LoRa/SX1278 引脚定义：必须与 Gateway 和 hardware/readme.md 完全一致。 */
#define LORA_SPI_HOST                   SPI2_HOST
#define LORA_PIN_CS                     GPIO_NUM_10
#define LORA_PIN_SCK                    GPIO_NUM_12
#define LORA_PIN_MOSI                   GPIO_NUM_11
#define LORA_PIN_MISO                   GPIO_NUM_13
#define LORA_PIN_DIO0                   GPIO_NUM_4
#define LORA_PIN_RST                    GPIO_NUM_21

/* LoRa 空口参数：433 MHz、BW 125 kHz、SF7、CR 4/5、显式头、payload CRC、TX power 17 dBm。 */
#define LORA_FREQ_HZ                    433000000UL
#define LORA_SPI_CLOCK_HZ               (4 * 1000 * 1000)
#define LORA_EXPECTED_VERSION           0x12
#define LORA_TX_PAYLOAD_LEN             ECHOGUARD_LORA_PAYLOAD_LEN
#define LORA_TX_TIMEOUT_MS              1000

/* 节点传感器引脚：AHT20 与 MPU6050 共用 I2C，MQ-135 使用 GPIO6 ADC 输入。 */
#define I2C_PORT_NUM                    I2C_NUM_0
#define I2C_PIN_SDA                     GPIO_NUM_8
#define I2C_PIN_SCL                     GPIO_NUM_9
#define I2C_FREQ_HZ                     100000
#define AHT20_ADDR                      0x38
#define AHT20_CMD_INIT                  0xBE
#define AHT20_CMD_INIT_LEGACY           0xE1
#define AHT20_CMD_SOFT_RESET            0xBA
#define AHT20_CMD_TRIGGER               0xAC
#define AHT20_POWER_ON_DELAY_MS         100
#define AHT20_RESET_DELAY_MS            20
#define AHT20_INIT_DELAY_MS             40
#define AHT20_MEASURE_DELAY_MS          120
#define AHT20_CMD_STATUS                0x71
#define AHT20_STATUS_BUSY               0x80
#define AHT20_STATUS_CALIBRATED         0x08
#define MPU6050_ADDR                    0x68
#define MQ135_ADC_GPIO                  GPIO_NUM_6

/* CSI 滑动窗口：callback 只写入轻量幅度统计，复杂特征在 csi_sensor_task 每秒计算。 */
#define CSI_WINDOW_SIZE                 64
#define CSI_MIN_VALID_SAMPLES           6
#define CSI_MIN_MEAN_AMP                1.0f
#define CSI_BASELINE_ALPHA_CALM         0.08f
#define CSI_BASELINE_ALPHA_ACTIVE       0.015f
#define CSI_BASELINE_ACTIVE_MARGIN      1.4f

/* WiFi 事件位：任务间只传递状态，不额外创建 WiFi 管理任务。 */
#define WIFI_STARTED_BIT                BIT0
#define WIFI_CONNECTED_BIT              BIT1
#define WIFI_RESELECT_BIT               BIT2

/* SX1278 常用寄存器地址。 */
#define REG_FIFO                        0x00
#define REG_OP_MODE                     0x01
#define REG_FRF_MSB                     0x06
#define REG_FRF_MID                     0x07
#define REG_FRF_LSB                     0x08
#define REG_PA_CONFIG                   0x09
#define REG_LNA                         0x0C
#define REG_FIFO_ADDR_PTR               0x0D
#define REG_FIFO_TX_BASE_ADDR           0x0E
#define REG_FIFO_RX_BASE_ADDR           0x0F
#define REG_IRQ_FLAGS                   0x12
#define REG_MODEM_CONFIG_1              0x1D
#define REG_MODEM_CONFIG_2              0x1E
#define REG_PREAMBLE_MSB                0x20
#define REG_PREAMBLE_LSB                0x21
#define REG_PAYLOAD_LENGTH              0x22
#define REG_MODEM_CONFIG_3              0x26
#define REG_DIO_MAPPING_1               0x40
#define REG_VERSION                     0x42
#define REG_PA_DAC                      0x4D

#define MODE_LONG_RANGE                 0x80
#define MODE_SLEEP                      0x00
#define MODE_STDBY                      0x01
#define MODE_TX                         0x03

#define IRQ_TX_DONE                     0x08

/* Node 与 Gateway 共用显式 14 字节协议编解码，避免结构体 padding 或字段漂移。 */
typedef echoguard_payload_t rescue_lora_packet_t;

typedef struct {
    uint8_t presence_score;
    uint8_t motion_score;
    uint8_t breath_bpm;
    uint8_t confidence;
    uint16_t gas_raw;
    int16_t temp_x10;
    uint8_t humidity;
    bool aht20_ok;
    bool mpu6050_ok;
    bool mq135_ok;
    bool csi_ok;
    int64_t updated_ms;
} node_fusion_sample_t;

typedef struct {
    float mean_amp;
    float abs_dev;
    float range;
    uint16_t sample_count;
    uint32_t total_packets;
    int8_t last_rssi;
} csi_window_features_t;

typedef struct {
    const char *ssid;
    const char *password;
} wifi_gateway_profile_t;

#define WIFI_GATEWAY_PRIMARY ECHOGUARD_GATEWAY_PRIMARY
#define WIFI_GATEWAY_SECONDARY ECHOGUARD_GATEWAY_SECONDARY
#define WIFI_GATEWAY_NONE ECHOGUARD_GATEWAY_NONE

static const wifi_gateway_profile_t s_gateway_profiles[] = {
    [WIFI_GATEWAY_PRIMARY] = {
        .ssid = WIFI_PRIMARY_SSID,
        .password = WIFI_STA_PASSWORD,
    },
    [WIFI_GATEWAY_SECONDARY] = {
        .ssid = WIFI_SECONDARY_SSID,
        .password = WIFI_STA_PASSWORD,
    },
};

static const char *TAG = "rescue_node";

static EventGroupHandle_t s_wifi_event_group;
static SemaphoreHandle_t s_sample_mutex;
static spi_device_handle_t s_lora_spi;
static i2c_master_bus_handle_t s_i2c_bus;
static i2c_master_dev_handle_t s_aht20_dev;
static i2c_master_dev_handle_t s_mpu6050_dev;
static adc_oneshot_unit_handle_t s_mq135_adc;
static adc_channel_t s_mq135_channel;
static bool s_i2c_scan_done;
static int64_t s_last_i2c_scan_ms;
static bool s_aht20_initialized;
static bool s_aht20_logged_first_read;
static uint32_t s_aht20_crc_errors;
static volatile int8_t s_selected_gateway = WIFI_GATEWAY_NONE;
static volatile int8_t s_connected_gateway = WIFI_GATEWAY_NONE;
static volatile uint8_t s_primary_connect_failures;
static volatile uint32_t s_gateway_ip_addr;

static portMUX_TYPE s_csi_lock = portMUX_INITIALIZER_UNLOCKED;
static float s_csi_window[CSI_WINDOW_SIZE];
static uint16_t s_csi_head;
static uint16_t s_csi_count;
static uint32_t s_csi_total_packets;
static int8_t s_csi_last_rssi;
static float s_csi_noise_floor;
static bool s_csi_noise_ready;

static node_fusion_sample_t s_latest_sample = {
    .presence_score = 0,
    .motion_score = 0,
    .breath_bpm = 0,
    .confidence = 0,
    .gas_raw = 0,
    .temp_x10 = 250,
    .humidity = 50,
};

static esp_err_t nvs_init_for_wifi(void);
static void wifi_sta_task(void *arg);
static void udp_keepalive_rx_task(void *arg);
static void csi_sensor_task(void *arg);
static void lora_send_task(void *arg);
static void region_feature_upload_task(void *arg);
static void wifi_event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data);
static esp_err_t wifi_sta_init(void);
static esp_err_t wifi_select_and_connect(void);
static const char *wifi_gateway_name(int gateway_index);
static esp_err_t csi_init_once(void);
static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *data);
static csi_window_features_t csi_window_snapshot(void);
static void csi_features_to_scores(const csi_window_features_t *features,
                                   float accel_delta_g,
                                   node_fusion_sample_t *sample);
static esp_err_t sensors_init_once(void);
static void i2c_scan_bus_once(void);
static esp_err_t aht20_init(void);
static esp_err_t aht20_send_init_command(uint8_t command);
static esp_err_t aht20_read_status(uint8_t *status);
static esp_err_t aht20_read(float *temperature_c, float *humidity_percent);
static esp_err_t mpu6050_read_accel(float *accel_g, float *accel_delta_g);
static esp_err_t mq135_read_raw(uint16_t *raw);
static esp_err_t lora_spi_bus_init_once(void);
static esp_err_t lora_chip_configure(void);
static esp_err_t lora_send_packet(const rescue_lora_packet_t *packet);
static void build_lora_payload(const rescue_lora_packet_t *packet, uint8_t payload[LORA_TX_PAYLOAD_LEN]);
static void lora_set_op_mode(uint8_t mode);
static void lora_write_reg(uint8_t reg, uint8_t value);
static uint8_t lora_read_reg(uint8_t reg);
static uint8_t clamp_u8_int(int value);

void app_main(void)
{
    setvbuf(stdout, NULL, _IONBF, 0);
    ESP_LOGI(TAG, "EchoGuard Node v0.4.0 WiFi multi-link CSI + Sensor Fusion starting");
    ESP_LOGI(TAG, "任务优先级: wifi_sta=%d, csi_sensor=%d, lora_send=%d",
             WIFI_TASK_PRIORITY, CSI_SENSOR_TASK_PRIORITY, LORA_SEND_TASK_PRIORITY);

    ESP_ERROR_CHECK(nvs_init_for_wifi());

    s_wifi_event_group = xEventGroupCreate();
    s_sample_mutex = xSemaphoreCreateMutex();
    if (s_wifi_event_group == NULL || s_sample_mutex == NULL) {
        ESP_LOGE(TAG, "创建 FreeRTOS 同步对象失败，系统无法继续启动");
        return;
    }

    BaseType_t wifi_ok = xTaskCreate(wifi_sta_task, "wifi_sta", WIFI_TASK_STACK_SIZE,
                                     NULL, WIFI_TASK_PRIORITY, NULL);
    BaseType_t udp_ok = xTaskCreate(udp_keepalive_rx_task, "udp_keepalive_rx",
                                    UDP_KEEPALIVE_TASK_STACK_SIZE, NULL,
                                    UDP_KEEPALIVE_TASK_PRIORITY, NULL);
    BaseType_t csi_ok = xTaskCreate(csi_sensor_task, "csi_sensor", CSI_SENSOR_TASK_STACK_SIZE,
                                    NULL, CSI_SENSOR_TASK_PRIORITY, NULL);
    BaseType_t lora_ok = xTaskCreate(lora_send_task, "lora_send", LORA_SEND_TASK_STACK_SIZE,
                                     NULL, LORA_SEND_TASK_PRIORITY, NULL);
    BaseType_t region_ok = xTaskCreate(region_feature_upload_task, "region_upload",
                                       REGION_UPLOAD_TASK_STACK_SIZE, NULL,
                                       REGION_UPLOAD_TASK_PRIORITY, NULL);

    if (wifi_ok != pdPASS || udp_ok != pdPASS || csi_ok != pdPASS ||
        lora_ok != pdPASS || region_ok != pdPASS) {
        ESP_LOGE(TAG, "创建核心任务失败，请增大 heap 或检查 FreeRTOS 配置");
    }
}

static esp_err_t nvs_init_for_wifi(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "NVS 版本或空间异常，擦除后重新初始化");
        ESP_RETURN_ON_ERROR(nvs_flash_erase(), TAG, "nvs_flash_erase failed");
        ret = nvs_flash_init();
    }
    return ret;
}

/* wifi_sta_task：扫描两个 WPA2 SoftAP，按 GW-01 优先规则连接并在断线后重新选择。 */
static void wifi_sta_task(void *arg)
{
    (void)arg;

    while (wifi_sta_init() != ESP_OK) {
        ESP_LOGE(TAG, "WiFi STA 初始化失败，2 秒后重试");
        vTaskDelay(pdMS_TO_TICKS(2000));
    }

    xEventGroupWaitBits(s_wifi_event_group, WIFI_STARTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);

    bool first_selection = true;
    while (true) {
        EventBits_t bits = xEventGroupGetBits(s_wifi_event_group);
        if ((bits & WIFI_CONNECTED_BIT) != 0) {
            ESP_LOGI(TAG, "WiFi STA 心跳: ssid=%s, started=1, connected=1",
                     wifi_gateway_name(s_connected_gateway));
            (void)xEventGroupWaitBits(s_wifi_event_group, WIFI_RESELECT_BIT,
                                      pdTRUE, pdFALSE, pdMS_TO_TICKS(30000));
            continue;
        }

        xEventGroupClearBits(s_wifi_event_group, WIFI_RESELECT_BIT);
        if (!first_selection) {
            vTaskDelay(pdMS_TO_TICKS(WIFI_RESELECT_DELAY_MS));
        }
        first_selection = false;

        esp_err_t ret = wifi_select_and_connect();
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "未连接到 Gateway，%d ms 后重新扫描: %s",
                     WIFI_RESELECT_DELAY_MS, esp_err_to_name(ret));
            continue;
        }

        bits = xEventGroupWaitBits(s_wifi_event_group,
                                   WIFI_CONNECTED_BIT | WIFI_RESELECT_BIT,
                                   pdFALSE, pdFALSE,
                                   pdMS_TO_TICKS(WIFI_CONNECT_TIMEOUT_MS));
        if ((bits & WIFI_CONNECTED_BIT) != 0) {
            continue;
        }
        if ((bits & WIFI_RESELECT_BIT) != 0) {
            xEventGroupClearBits(s_wifi_event_group, WIFI_RESELECT_BIT);
            continue;
        }

        ESP_LOGW(TAG, "连接 %s 超时，重新选择 Gateway",
                 wifi_gateway_name(s_selected_gateway));
        (void)esp_wifi_disconnect();
        xEventGroupSetBits(s_wifi_event_group, WIFI_RESELECT_BIT);
    }
}

static esp_err_t wifi_sta_init(void)
{
    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "esp_netif_init failed");
    esp_err_t ret = esp_event_loop_create_default();
    if (ret != ESP_OK && ret != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "esp_event_loop_create_default failed: %s", esp_err_to_name(ret));
        return ret;
    }

    esp_netif_t *sta_netif = esp_netif_create_default_wifi_sta();
    if (sta_netif == NULL) {
        ESP_LOGE(TAG, "创建默认 STA netif 失败");
        return ESP_FAIL;
    }

    wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_RETURN_ON_ERROR(esp_wifi_init(&init_config), TAG, "esp_wifi_init failed");
    ESP_RETURN_ON_ERROR(esp_wifi_set_storage(WIFI_STORAGE_RAM), TAG, "esp_wifi_set_storage failed");
    ESP_RETURN_ON_ERROR(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                            wifi_event_handler, NULL, NULL),
                        TAG, "register WIFI_EVENT handler failed");
    ESP_RETURN_ON_ERROR(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                            wifi_event_handler, NULL, NULL),
                        TAG, "register IP_EVENT handler failed");

    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG, "esp_wifi_set_mode STA failed");
    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "esp_wifi_start failed");
    ESP_RETURN_ON_ERROR(esp_wifi_set_ps(WIFI_PS_NONE), TAG, "disable WiFi power save failed");
    ESP_RETURN_ON_ERROR(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20),
                        TAG, "set STA HT20 failed");
    ESP_LOGI(TAG, "WiFi STA 已启动，WPA2-PSK Gateway 优先级: %s -> %s",
             WIFI_PRIMARY_SSID, WIFI_SECONDARY_SSID);

    return ESP_OK;
}

static const char *wifi_gateway_name(int gateway_index)
{
    if (gateway_index < WIFI_GATEWAY_PRIMARY || gateway_index > WIFI_GATEWAY_SECONDARY) {
        return "none";
    }
    return s_gateway_profiles[gateway_index].ssid;
}

static esp_err_t wifi_select_and_connect(void)
{
    wifi_scan_config_t scan_config = {
        .show_hidden = false,
        .scan_type = WIFI_SCAN_TYPE_ACTIVE,
    };
    esp_err_t ret = esp_wifi_scan_start(&scan_config, true);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "Gateway 扫描启动失败: %s", esp_err_to_name(ret));
        return ret;
    }

    uint16_t found_count = 0;
    ESP_RETURN_ON_ERROR(esp_wifi_scan_get_ap_num(&found_count), TAG, "scan_get_ap_num failed");

    uint16_t record_count = found_count < WIFI_SCAN_MAX_APS ? found_count : WIFI_SCAN_MAX_APS;
    wifi_ap_record_t *records = NULL;
    if (record_count > 0) {
        records = calloc(record_count, sizeof(*records));
        if (records == NULL) {
            (void)esp_wifi_clear_ap_list();
            ESP_LOGE(TAG, "分配 Gateway 扫描结果失败，count=%u", record_count);
            return ESP_ERR_NO_MEM;
        }
        ret = esp_wifi_scan_get_ap_records(&record_count, records);
        if (ret != ESP_OK) {
            free(records);
            ESP_LOGW(TAG, "读取 Gateway 扫描结果失败: %s", esp_err_to_name(ret));
            return ret;
        }
    } else {
        (void)esp_wifi_clear_ap_list();
    }

    echoguard_gateway_candidate_t candidates[2] = {0};
    for (uint16_t i = 0; i < record_count; ++i) {
        for (int gateway = WIFI_GATEWAY_PRIMARY; gateway <= WIFI_GATEWAY_SECONDARY; ++gateway) {
            if (strcmp((const char *)records[i].ssid, s_gateway_profiles[gateway].ssid) == 0 &&
                records[i].authmode >= WIFI_AUTH_WPA2_PSK) {
                candidates[gateway].visible = true;
                candidates[gateway].channel = records[i].primary;
                candidates[gateway].rssi = records[i].rssi;
            }
        }
    }
    free(records);

    int selected = echoguard_select_gateway(
        candidates,
        s_primary_connect_failures,
        WIFI_PRIMARY_FAILURE_LIMIT
    );

    if (selected == WIFI_GATEWAY_NONE) {
        ESP_LOGW(TAG, "扫描未发现可用 Gateway: primary=%d secondary=%d",
                 candidates[WIFI_GATEWAY_PRIMARY].visible,
                 candidates[WIFI_GATEWAY_SECONDARY].visible);
        return ESP_ERR_NOT_FOUND;
    }

    wifi_config_t wifi_config = {0};
    snprintf((char *)wifi_config.sta.ssid, sizeof(wifi_config.sta.ssid), "%s",
             s_gateway_profiles[selected].ssid);
    snprintf((char *)wifi_config.sta.password, sizeof(wifi_config.sta.password), "%s",
             s_gateway_profiles[selected].password);
    wifi_config.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
    wifi_config.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;
    wifi_config.sta.channel = candidates[selected].channel;
    wifi_config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    wifi_config.sta.pmf_cfg.capable = true;
    wifi_config.sta.pmf_cfg.required = false;

    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_STA, &wifi_config),
                        TAG, "esp_wifi_set_config STA failed");
    s_selected_gateway = selected;
    ESP_LOGI(TAG, "选择 Gateway: ssid=%s channel=%u rssi=%d primary_failures=%u",
             wifi_gateway_name(selected), candidates[selected].channel, candidates[selected].rssi,
             s_primary_connect_failures);

    ret = esp_wifi_connect();
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "连接 %s 调用失败: %s", wifi_gateway_name(selected), esp_err_to_name(ret));
    }
    return ret;
}

static void wifi_event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    (void)arg;

    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        xEventGroupSetBits(s_wifi_event_group, WIFI_STARTED_BIT);
        xEventGroupSetBits(s_wifi_event_group, WIFI_RESELECT_BIT);
        return;
    }

    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_CONNECTED) {
        wifi_event_sta_connected_t *connected = (wifi_event_sta_connected_t *)event_data;
        echoguard_region_csi_set_gateway(
            connected->bssid,
            s_selected_gateway == WIFI_GATEWAY_SECONDARY
        );
        return;
    }

    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
        wifi_event_sta_disconnected_t *disconnected = (wifi_event_sta_disconnected_t *)event_data;
        if (s_selected_gateway == WIFI_GATEWAY_PRIMARY &&
            s_primary_connect_failures < UINT8_MAX) {
            ++s_primary_connect_failures;
        }
        ESP_LOGW(TAG, "WiFi 断开: ssid=%s reason=%d primary_failures=%u，准备重新选择",
                 wifi_gateway_name(s_selected_gateway), disconnected->reason,
                 s_primary_connect_failures);
        s_connected_gateway = WIFI_GATEWAY_NONE;
        s_gateway_ip_addr = 0U;
        echoguard_region_csi_set_disconnected();
        xEventGroupSetBits(s_wifi_event_group, WIFI_RESELECT_BIT);
        return;
    }

    if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        s_connected_gateway = s_selected_gateway;
        s_gateway_ip_addr = event->ip_info.gw.addr;
        s_primary_connect_failures = 0;
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
        ESP_LOGI(TAG, "WiFi 已连接 Gateway: ssid=%s IP=" IPSTR,
                 wifi_gateway_name(s_connected_gateway), IP2STR(&event->ip_info.ip));
    }
}

static void udp_keepalive_rx_task(void *arg)
{
    (void)arg;

    uint32_t received = 0;

    while (true) {
        xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);

        int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
        if (sock < 0) {
            ESP_LOGW(TAG, "UDP keepalive socket create failed: errno=%d", errno);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        struct sockaddr_in local_addr = {
            .sin_family = AF_INET,
            .sin_port = htons(UDP_KEEPALIVE_PORT),
            .sin_addr.s_addr = htonl(INADDR_ANY),
        };
        if (bind(sock, (struct sockaddr *)&local_addr, sizeof(local_addr)) < 0) {
            ESP_LOGW(TAG, "UDP keepalive bind failed: errno=%d", errno);
            close(sock);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        struct timeval timeout = {
            .tv_sec = 1,
            .tv_usec = 0,
        };
        setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        ESP_LOGI(TAG, "UDP keepalive listener ready on port %d", UDP_KEEPALIVE_PORT);

        while ((xEventGroupGetBits(s_wifi_event_group) & WIFI_CONNECTED_BIT) != 0) {
            char buffer[32];
            struct sockaddr_in source_addr = {0};
            socklen_t source_len = sizeof(source_addr);
            int len = recvfrom(sock, buffer, sizeof(buffer) - 1, 0,
                               (struct sockaddr *)&source_addr, &source_len);
            if (len > 0) {
                buffer[len] = '\0';
                (void)echoguard_region_csi_handle_sync(buffer, (size_t)len);
                received++;
                if (received == 1 || (received % 100U) == 0U) {
                    esp_ip4_addr_t source_ip = {
                        .addr = source_addr.sin_addr.s_addr,
                    };
                    ESP_LOGI(TAG, "UDP keepalive received count=%" PRIu32 " from=" IPSTR,
                             received, IP2STR(&source_ip));
                }
                continue;
            }

            if (errno != EAGAIN && errno != EWOULDBLOCK) {
                ESP_LOGW(TAG, "UDP keepalive recv failed: errno=%d", errno);
                break;
            }
        }

        close(sock);
        ESP_LOGI(TAG, "UDP keepalive listener stopped, waiting WiFi reconnect");
    }
}

/* 区域特征走 WiFi UDP 上行，LoRa 继续保持原 14 字节传感器协议。 */
static void region_feature_upload_task(void *arg)
{
    (void)arg;
    uint32_t sent_count = 0U;

    while (true) {
        xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT,
                            pdFALSE, pdTRUE, portMAX_DELAY);
        if (s_connected_gateway != WIFI_GATEWAY_SECONDARY || s_gateway_ip_addr == 0U) {
            vTaskDelay(pdMS_TO_TICKS(500));
            continue;
        }

        int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
        if (sock < 0) {
            ESP_LOGW(TAG, "region UDP socket create failed: errno=%d", errno);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        ESP_LOGI(TAG, "triangle region feature uplink ready: gateway=%s port=%u",
                 wifi_gateway_name(s_connected_gateway), ECHOGUARD_REGION_FEATURE_PORT);
        while ((xEventGroupGetBits(s_wifi_event_group) & WIFI_CONNECTED_BIT) != 0 &&
               s_connected_gateway == WIFI_GATEWAY_SECONDARY) {
            echoguard_region_packet_t packet = {0};
            if (!echoguard_region_csi_snapshot(&packet)) {
                vTaskDelay(pdMS_TO_TICKS(500));
                continue;
            }

            uint8_t payload[ECHOGUARD_REGION_PACKET_LEN] = {0};
            if (!echoguard_region_packet_encode(&packet, payload)) {
                ESP_LOGW(TAG, "region feature encode rejected");
                vTaskDelay(pdMS_TO_TICKS(500));
                continue;
            }

            struct sockaddr_in destination = {
                .sin_family = AF_INET,
                .sin_port = htons(ECHOGUARD_REGION_FEATURE_PORT),
                .sin_addr.s_addr = s_gateway_ip_addr,
            };
            int sent = sendto(sock, payload, sizeof(payload), 0,
                              (struct sockaddr *)&destination, sizeof(destination));
            if (sent == (int)sizeof(payload)) {
                sent_count++;
                if (sent_count == 1U || (sent_count % 20U) == 0U) {
                    ESP_LOGI(TAG, "region feature sent count=%" PRIu32
                             " seq=%" PRIu32 " links=%u sync=%u",
                             sent_count, packet.sequence, packet.link_count,
                             (packet.flags & ECHOGUARD_REGION_FLAG_SYNC_OK) != 0U);
                }
            } else {
                ESP_LOGW(TAG, "region feature send failed: sent=%d errno=%d", sent, errno);
            }
            vTaskDelay(pdMS_TO_TICKS(500));
        }
        close(sock);
    }
}

/* csi_sensor_task：初始化 CSI/I2C/ADC，每秒融合 CSI 与传感器读数，写入 LoRa 发送快照。 */
static void csi_sensor_task(void *arg)
{
    (void)arg;

    xEventGroupWaitBits(s_wifi_event_group, WIFI_STARTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);

    uint32_t last_csi_total_packets = 0;

    bool csi_ready = (csi_init_once() == ESP_OK);
    bool sensors_ready = (sensors_init_once() == ESP_OK);
    if (!csi_ready) {
        ESP_LOGW(TAG, "CSI 初始化失败，后续特征将主要依赖传感器并持续重试");
    }
    if (!sensors_ready) {
        ESP_LOGW(TAG, "传感器初始化存在失败项，LoRa 仍会持续发送默认/上次有效值");
    }

    while (true) {
        if (!csi_ready) {
            csi_ready = (csi_init_once() == ESP_OK);
        }
        if (!sensors_ready) {
            sensors_ready = (sensors_init_once() == ESP_OK);
        }

        int64_t now_ms = esp_timer_get_time() / 1000;
        if (s_i2c_bus != NULL && now_ms - s_last_i2c_scan_ms >= 10000) {
            i2c_scan_bus_once();
        }

        node_fusion_sample_t sample = s_latest_sample;
        float temperature_c = 25.0f;
        float humidity_percent = 50.0f;
        float accel_g = 1.0f;
        float accel_delta_g = 0.0f;
        uint16_t gas_raw = sample.gas_raw;

        esp_err_t aht_ret = aht20_read(&temperature_c, &humidity_percent);
        esp_err_t mpu_ret = mpu6050_read_accel(&accel_g, &accel_delta_g);
        esp_err_t mq_ret = mq135_read_raw(&gas_raw);

        csi_window_features_t csi_features = csi_window_snapshot();
        uint32_t csi_delta_packets = csi_features.total_packets - last_csi_total_packets;
        last_csi_total_packets = csi_features.total_packets;
        csi_features_to_scores(&csi_features, accel_delta_g, &sample);

        if (aht_ret == ESP_OK) {
            sample.temp_x10 = (int16_t)lroundf(temperature_c * 10.0f);
            sample.humidity = clamp_u8_int((int)lroundf(humidity_percent));
            sample.aht20_ok = true;
        } else {
            sample.aht20_ok = false;
            ESP_LOGW(TAG, "AHT20 read failed: %s, keeping last temperature/humidity", esp_err_to_name(aht_ret));
        }

        if (mpu_ret == ESP_OK) {
            sample.mpu6050_ok = true;
        } else {
            sample.mpu6050_ok = false;
            ESP_LOGW(TAG, "MPU6050 读取失败: %s，motion_score 仅使用 CSI", esp_err_to_name(mpu_ret));
        }

        if (mq_ret == ESP_OK) {
            sample.gas_raw = gas_raw;
            sample.mq135_ok = true;
        } else {
            sample.mq135_ok = false;
            ESP_LOGW(TAG, "MQ-135 ADC 读取失败: %s，保留上次 gas_raw", esp_err_to_name(mq_ret));
        }

        sample.csi_ok = csi_features.sample_count >= CSI_MIN_VALID_SAMPLES;
        sample.updated_ms = esp_timer_get_time() / 1000;

        int confidence = sample.confidence;
        confidence += sample.aht20_ok ? 8 : 0;
        confidence += sample.mpu6050_ok ? 8 : 0;
        confidence += sample.mq135_ok ? 6 : 0;
        confidence += (xEventGroupGetBits(s_wifi_event_group) & WIFI_CONNECTED_BIT) ? 10 : 0;
        sample.confidence = clamp_u8_int(confidence);

        if (xSemaphoreTake(s_sample_mutex, pdMS_TO_TICKS(50)) == pdTRUE) {
            s_latest_sample = sample;
            xSemaphoreGive(s_sample_mutex);
        } else {
            ESP_LOGW(TAG, "更新融合快照超时，本周期数据丢弃");
        }

        ESP_LOGI(TAG, "融合特征: presence=%u motion=%u bpm=%u conf=%u gas=%u temp=%.1f hum=%u csi_n=%u accel=%.2fg",
                 sample.presence_score,
                 sample.motion_score,
                 sample.breath_bpm,
                 sample.confidence,
                 sample.gas_raw,
                 (double)sample.temp_x10 / 10.0,
                 sample.humidity,
                 csi_features.sample_count,
                 (double)accel_g);

        EventBits_t wifi_bits = xEventGroupGetBits(s_wifi_event_group);
        bool wifi_connected = (wifi_bits & WIFI_CONNECTED_BIT) != 0;
        int8_t wifi_rssi = 0;
        if (wifi_connected) {
            wifi_ap_record_t ap_info = {0};
            if (esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK) {
                wifi_rssi = ap_info.rssi;
            }
        }
        ESP_LOGI(TAG,
                 "诊断: wifi=%d wifi_rssi=%d csi_total=%" PRIu32 " csi_delta=%" PRIu32
                 " csi_rssi=%d mean=%.2f range=%.2f abs_dev=%.2f noise=%.2f aht20=%d mpu6050=%d mq135=%d",
                 wifi_connected ? 1 : 0,
                 wifi_rssi,
                 csi_features.total_packets,
                 csi_delta_packets,
                 csi_features.last_rssi,
                 (double)csi_features.mean_amp,
                 (double)csi_features.range,
                 (double)csi_features.abs_dev,
                 (double)s_csi_noise_floor,
                 sample.aht20_ok ? 1 : 0,
                 sample.mpu6050_ok ? 1 : 0,
                 sample.mq135_ok ? 1 : 0);

        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

static esp_err_t csi_init_once(void)
{
    EventBits_t bits = xEventGroupGetBits(s_wifi_event_group);
    if ((bits & WIFI_STARTED_BIT) == 0) {
        return ESP_ERR_INVALID_STATE;
    }

    wifi_csi_config_t csi_config = {
        .lltf_en = true,
        .htltf_en = true,
        .stbc_htltf2_en = false,
        .ltf_merge_en = false,
        .channel_filter_en = false,
        .manu_scale = false,
        .shift = 0,
        .dump_ack_en = false,
    };

    ESP_RETURN_ON_ERROR(echoguard_region_csi_init(NODE_ID), TAG,
                        "multi-link region CSI init failed");
    ESP_RETURN_ON_ERROR(esp_wifi_set_promiscuous(true), TAG,
                        "enable CSI promiscuous mode failed");
    ESP_RETURN_ON_ERROR(esp_wifi_set_csi_config(&csi_config), TAG, "esp_wifi_set_csi_config failed");
    ESP_RETURN_ON_ERROR(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL), TAG, "esp_wifi_set_csi_rx_cb failed");
    ESP_RETURN_ON_ERROR(esp_wifi_set_csi(true), TAG, "esp_wifi_set_csi enable failed");
    ESP_LOGI(TAG, "WiFi CSI 已启用：分来源多链路 + 52 子载波区域特征");
    return ESP_OK;
}

static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *data)
{
    (void)ctx;

    if (data == NULL || data->buf == NULL || data->len < 8) {
        return;
    }

    int region_source = echoguard_region_csi_ingest(data);
    if (region_source != ECHOGUARD_REGION_SOURCE_GATEWAY) {
        return;
    }

    int start = data->first_word_invalid ? 4 : 0;
    uint32_t magnitude_sum = 0;
    uint16_t pair_count = 0;

    for (int i = start; i + 1 < data->len; i += 2) {
        int i_part = data->buf[i];
        int q_part = data->buf[i + 1];
        magnitude_sum += (uint32_t)(abs(i_part) + abs(q_part));
        pair_count++;
    }

    if (pair_count == 0) {
        return;
    }

    float mean_magnitude = (float)magnitude_sum / (float)pair_count;

    portENTER_CRITICAL(&s_csi_lock);
    s_csi_window[s_csi_head] = mean_magnitude;
    s_csi_head = (s_csi_head + 1) % CSI_WINDOW_SIZE;
    if (s_csi_count < CSI_WINDOW_SIZE) {
        s_csi_count++;
    }
    s_csi_total_packets++;
    s_csi_last_rssi = data->rx_ctrl.rssi;
    portEXIT_CRITICAL(&s_csi_lock);
}

static csi_window_features_t csi_window_snapshot(void)
{
    float local_window[CSI_WINDOW_SIZE] = {0};
    uint16_t local_count = 0;
    uint32_t total_packets = 0;
    int8_t last_rssi = 0;

    portENTER_CRITICAL(&s_csi_lock);
    local_count = s_csi_count;
    total_packets = s_csi_total_packets;
    last_rssi = s_csi_last_rssi;
    for (uint16_t i = 0; i < local_count; ++i) {
        local_window[i] = s_csi_window[i];
    }
    portEXIT_CRITICAL(&s_csi_lock);

    csi_window_features_t features = {
        .sample_count = local_count,
        .total_packets = total_packets,
        .last_rssi = last_rssi,
    };

    if (local_count == 0) {
        return features;
    }

    float sum = 0.0f;
    float min_value = local_window[0];
    float max_value = local_window[0];
    for (uint16_t i = 0; i < local_count; ++i) {
        float value = local_window[i];
        sum += value;
        min_value = fminf(min_value, value);
        max_value = fmaxf(max_value, value);
    }

    features.mean_amp = sum / (float)local_count;
    features.range = max_value - min_value;

    float abs_dev_sum = 0.0f;
    for (uint16_t i = 0; i < local_count; ++i) {
        abs_dev_sum += fabsf(local_window[i] - features.mean_amp);
    }
    features.abs_dev = abs_dev_sum / (float)local_count;

    return features;
}

static void csi_features_to_scores(const csi_window_features_t *features,
                                   float accel_delta_g,
                                   node_fusion_sample_t *sample)
{
    if (features == NULL || sample == NULL) {
        return;
    }

    if (features->sample_count < CSI_MIN_VALID_SAMPLES) {
        sample->presence_score = 0;
        sample->motion_score = clamp_u8_int((int)lroundf(accel_delta_g * 95.0f));
        sample->breath_bpm = 0;
        sample->confidence = 10;
        return;
    }

    float mean_amp = fmaxf(features->mean_amp, CSI_MIN_MEAN_AMP);
    float norm_abs_dev = (features->abs_dev / mean_amp) * 100.0f;
    float norm_range = (features->range / mean_amp) * 100.0f;
    float disturbance = norm_abs_dev * 0.7f + norm_range * 0.3f;
    bool baseline_ready_before_update = s_csi_noise_ready;

    if (!s_csi_noise_ready) {
        s_csi_noise_floor = disturbance;
        s_csi_noise_ready = true;
    } else {
        float active_limit = s_csi_noise_floor * CSI_BASELINE_ACTIVE_MARGIN + 1.0f;
        float alpha = disturbance <= active_limit ? CSI_BASELINE_ALPHA_CALM : CSI_BASELINE_ALPHA_ACTIVE;
        s_csi_noise_floor = s_csi_noise_floor * (1.0f - alpha) + disturbance * alpha;
    }

    float excess = fmaxf(0.0f, disturbance - s_csi_noise_floor);
    float range_over_noise = fmaxf(0.0f, norm_range - s_csi_noise_floor * 1.2f);
    int csi_presence = (int)lroundf(excess * 14.0f + norm_abs_dev * 3.0f + norm_range * 0.8f);
    int csi_motion = (int)lroundf(excess * 7.0f + range_over_noise * 1.8f + norm_abs_dev * 1.0f);
    int imu_motion = (int)lroundf(accel_delta_g * 95.0f);
    int fused_motion = (int)lroundf(csi_motion * 0.75f + imu_motion * 0.25f);

    bool imu_quiet = imu_motion <= 12;
    bool csi_spiky_but_stable = imu_quiet && norm_abs_dev < 16.0f && norm_range > s_csi_noise_floor * 1.8f;
    if (csi_spiky_but_stable) {
        csi_presence = csi_presence > 55 ? 55 : csi_presence;
        fused_motion = fused_motion > 35 ? 35 : fused_motion;
    }

    sample->presence_score = clamp_u8_int(csi_presence);
    if ((!baseline_ready_before_update || excess < 0.4f) && imu_motion <= 35) {
        fused_motion = fused_motion > 35 ? 35 : fused_motion;
    }
    sample->motion_score = clamp_u8_int(fused_motion);

    /*
     * 呼吸率 v1：用 CSI 幅度扰动强弱给出粗略稳定范围。
     * 后续迭代可在这里替换为带通滤波 + FFT/峰值间隔估计，不影响 LoRa 包格式。
     */
    bool breath_candidate = sample->presence_score >= 18 && sample->motion_score <= 70 && excess > 0.2f;
    if (!breath_candidate) {
        sample->breath_bpm = 0;
    } else {
        int bpm = 12 + (int)lroundf(fminf(16.0f, excess * 1.8f + norm_abs_dev * 0.7f));
        sample->breath_bpm = clamp_u8_int(bpm > 28 ? 28 : bpm);
    }

    float sample_quality = fminf(1.0f, (float)features->sample_count / (float)CSI_WINDOW_SIZE);
    float rssi_quality = fmaxf(0.0f, fminf(1.0f, ((float)features->last_rssi + 95.0f) / 35.0f));
    float stability_quality = 1.0f - fminf(1.0f, s_csi_noise_floor / 18.0f);
    int confidence = (int)lroundf(sample_quality * 45.0f + rssi_quality * 20.0f + stability_quality * 15.0f);
    if (sample->presence_score >= 18 && sample->motion_score <= 70) {
        confidence += 10;
    }
    if (sample->motion_score > 85) {
        confidence -= 12;
    }
    sample->confidence = clamp_u8_int(confidence);
}

static esp_err_t sensors_init_once(void)
{
    esp_err_t overall = ESP_OK;

    if (s_i2c_bus == NULL) {
        i2c_master_bus_config_t bus_config = {
            .i2c_port = I2C_PORT_NUM,
            .sda_io_num = I2C_PIN_SDA,
            .scl_io_num = I2C_PIN_SCL,
            .clk_source = I2C_CLK_SRC_DEFAULT,
            .glitch_ignore_cnt = 7,
            .flags = {
                .enable_internal_pullup = true,
            },
        };

        esp_err_t ret = i2c_new_master_bus(&bus_config, &s_i2c_bus);
        if (ret != ESP_OK && ret != ESP_ERR_INVALID_STATE) {
            ESP_LOGE(TAG, "I2C 总线初始化失败 SDA=%d SCL=%d: %s",
                     I2C_PIN_SDA, I2C_PIN_SCL, esp_err_to_name(ret));
            overall = ret;
        } else {
            ESP_LOGI(TAG, "I2C 总线已初始化 SDA=GPIO%d SCL=GPIO%d freq=%dHz",
                     I2C_PIN_SDA, I2C_PIN_SCL, I2C_FREQ_HZ);
        }
    }

    int64_t now_ms = esp_timer_get_time() / 1000;
    if (s_i2c_bus != NULL && (!s_i2c_scan_done || now_ms - s_last_i2c_scan_ms >= 10000)) {
        i2c_scan_bus_once();
    }

    if (s_i2c_bus != NULL && s_aht20_dev == NULL) {
        i2c_device_config_t aht20_config = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address = AHT20_ADDR,
            .scl_speed_hz = I2C_FREQ_HZ,
        };
        esp_err_t ret = i2c_master_bus_add_device(s_i2c_bus, &aht20_config, &s_aht20_dev);
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "AHT20 I2C device add failed addr=0x%02x: %s",
                     AHT20_ADDR, esp_err_to_name(ret));
            overall = ret;
        }
    }

    if (s_aht20_dev != NULL && !s_aht20_initialized) {
        esp_err_t ret = aht20_init();
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "AHT20 init failed addr=0x%02x: %s", AHT20_ADDR, esp_err_to_name(ret));
            overall = ret;
        } else {
            s_aht20_initialized = true;
            ESP_LOGI(TAG, "AHT20 init ok addr=0x%02x", AHT20_ADDR);
        }
    }

    if (s_i2c_bus != NULL && s_mpu6050_dev == NULL) {
        i2c_device_config_t mpu_config = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address = MPU6050_ADDR,
            .scl_speed_hz = I2C_FREQ_HZ,
        };
        esp_err_t ret = i2c_master_bus_add_device(s_i2c_bus, &mpu_config, &s_mpu6050_dev);
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "添加 MPU6050 I2C 设备失败 addr=0x%02x: %s",
                     MPU6050_ADDR, esp_err_to_name(ret));
            overall = ret;
        } else {
            uint8_t wake_cmd[] = {0x6B, 0x00};
            ret = i2c_master_transmit(s_mpu6050_dev, wake_cmd, sizeof(wake_cmd), 100);
            if (ret != ESP_OK) {
                ESP_LOGW(TAG, "MPU6050 唤醒失败: %s", esp_err_to_name(ret));
                overall = ret;
            }
        }
    }

    if (s_mq135_adc == NULL) {
        adc_unit_t unit = ADC_UNIT_1;
        adc_channel_t channel = ADC_CHANNEL_0;
        esp_err_t ret = adc_oneshot_io_to_channel(MQ135_ADC_GPIO, &unit, &channel);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "GPIO%d 不能映射到 ADC 通道: %s", MQ135_ADC_GPIO, esp_err_to_name(ret));
            overall = ret;
        } else {
            adc_oneshot_unit_init_cfg_t unit_config = {
                .unit_id = unit,
                .clk_src = 0,
                .ulp_mode = ADC_ULP_MODE_DISABLE,
            };
            ret = adc_oneshot_new_unit(&unit_config, &s_mq135_adc);
            if (ret != ESP_OK && ret != ESP_ERR_NOT_FOUND) {
                ESP_LOGE(TAG, "ADC oneshot unit 初始化失败: %s", esp_err_to_name(ret));
                overall = ret;
            } else if (ret == ESP_OK) {
                adc_oneshot_chan_cfg_t chan_config = {
                    .atten = ADC_ATTEN_DB_12,
                    .bitwidth = ADC_BITWIDTH_DEFAULT,
                };
                ret = adc_oneshot_config_channel(s_mq135_adc, channel, &chan_config);
                if (ret != ESP_OK) {
                    ESP_LOGE(TAG, "MQ-135 ADC 通道配置失败 GPIO%d: %s",
                             MQ135_ADC_GPIO, esp_err_to_name(ret));
                    overall = ret;
                } else {
                    s_mq135_channel = channel;
                    ESP_LOGI(TAG, "MQ-135 ADC 已初始化 GPIO%d unit=%d channel=%d",
                             MQ135_ADC_GPIO, unit, channel);
                }
            }
        }
    }

    return overall;
}

static void i2c_scan_bus_once(void)
{
    if (s_i2c_bus == NULL) {
        return;
    }

    bool found_any = false;
    bool found_aht20 = false;
    bool found_mpu6050 = false;
    ESP_LOGI(TAG, "I2C_SCAN_START sda=GPIO%d scl=GPIO%d expected_aht20=0x%02x expected_mpu6050=0x%02x",
             I2C_PIN_SDA, I2C_PIN_SCL, AHT20_ADDR, MPU6050_ADDR);
    for (uint8_t addr = 0x03; addr <= 0x77; ++addr) {
        esp_err_t ret = i2c_master_probe(s_i2c_bus, addr, 50);
        if (ret == ESP_OK) {
            ESP_LOGI(TAG, "I2C_FOUND addr=0x%02x", addr);
            found_any = true;
            found_aht20 = found_aht20 || (addr == AHT20_ADDR);
            found_mpu6050 = found_mpu6050 || (addr == MPU6050_ADDR);
        }
    }

    if (!found_any) {
        ESP_LOGW(TAG, "I2C_NONE_FOUND check_3v3_gnd_sda_scl_pullups");
    }
    if (!found_aht20) {
        ESP_LOGW(TAG, "I2C_EXPECTED_MISSING device=AHT20 addr=0x%02x", AHT20_ADDR);
    }
    if (!found_mpu6050) {
        ESP_LOGW(TAG, "I2C_EXPECTED_MISSING device=MPU6050 addr=0x%02x", MPU6050_ADDR);
    }
    s_i2c_scan_done = true;
    s_last_i2c_scan_ms = esp_timer_get_time() / 1000;
}

static esp_err_t aht20_init(void)
{
    if (s_aht20_dev == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    vTaskDelay(pdMS_TO_TICKS(AHT20_POWER_ON_DELAY_MS));

    uint8_t status = 0;
    esp_err_t ret = aht20_read_status(&status);
    if (ret == ESP_OK && (status & AHT20_STATUS_CALIBRATED) != 0) {
        ESP_LOGI(TAG, "AHT20 already calibrated status=0x%02x", status);
        return ESP_OK;
    }

    uint8_t reset_cmd = AHT20_CMD_SOFT_RESET;
    ret = i2c_master_transmit(s_aht20_dev, &reset_cmd, 1, 100);
    if (ret != ESP_OK) {
        return ret;
    }
    vTaskDelay(pdMS_TO_TICKS(AHT20_RESET_DELAY_MS));

    ret = aht20_send_init_command(AHT20_CMD_INIT);
    if (ret != ESP_OK) {
        return ret;
    }

    ret = aht20_read_status(&status);
    if (ret == ESP_OK && (status & AHT20_STATUS_CALIBRATED) != 0) {
        ESP_LOGI(TAG, "AHT20 calibrated status=0x%02x", status);
        return ESP_OK;
    }

    ret = aht20_send_init_command(AHT20_CMD_INIT_LEGACY);
    if (ret != ESP_OK) {
        return ret;
    }

    ret = aht20_read_status(&status);
    if (ret != ESP_OK) {
        return ret;
    }
    if ((status & AHT20_STATUS_CALIBRATED) == 0) {
        ESP_LOGW(TAG, "AHT20 not calibrated status=0x%02x", status);
        return ESP_ERR_INVALID_STATE;
    }

    ESP_LOGI(TAG, "AHT20 calibrated with legacy init status=0x%02x", status);
    return ESP_OK;
}

static esp_err_t aht20_send_init_command(uint8_t command)
{
    uint8_t init_cmd[] = {command, 0x08, 0x00};
    esp_err_t ret = i2c_master_transmit(s_aht20_dev, init_cmd, sizeof(init_cmd), 100);
    if (ret != ESP_OK) {
        return ret;
    }
    vTaskDelay(pdMS_TO_TICKS(AHT20_INIT_DELAY_MS));
    return ESP_OK;
}

static esp_err_t aht20_read_status(uint8_t *status)
{
    if (s_aht20_dev == NULL || status == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    uint8_t command = AHT20_CMD_STATUS;
    uint8_t value = 0;
    esp_err_t ret = i2c_master_transmit_receive(s_aht20_dev, &command, 1, &value, 1, 100);
    if (ret != ESP_OK) {
        return ret;
    }

    *status = value;
    return ESP_OK;
}

static esp_err_t aht20_read(float *temperature_c, float *humidity_percent)
{
    if (s_aht20_dev == NULL || !s_aht20_initialized || temperature_c == NULL || humidity_percent == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    uint8_t trigger_cmd[] = {AHT20_CMD_TRIGGER, 0x33, 0x00};
    esp_err_t ret = i2c_master_transmit(s_aht20_dev, trigger_cmd, sizeof(trigger_cmd), 100);
    if (ret != ESP_OK) {
        return ret;
    }

    vTaskDelay(pdMS_TO_TICKS(AHT20_MEASURE_DELAY_MS));

    uint8_t data[7] = {0};
    ret = i2c_master_receive(s_aht20_dev, data, sizeof(data), 100);
    if (ret != ESP_OK) {
        return ret;
    }

    if ((data[0] & AHT20_STATUS_BUSY) != 0 || (data[0] & AHT20_STATUS_CALIBRATED) == 0) {
        ESP_LOGW(TAG, "AHT20 invalid status=0x%02x", data[0]);
        s_aht20_initialized = false;
        return ESP_ERR_INVALID_STATE;
    }

    if (!aht20_measurement_crc_valid(data)) {
        ++s_aht20_crc_errors;
        if (s_aht20_crc_errors <= 3 || (s_aht20_crc_errors % 30) == 0) {
            ESP_LOGW(TAG, "AHT20 CRC mismatch count=%" PRIu32 " expected=0x%02x actual=0x%02x",
                     s_aht20_crc_errors, aht20_crc8(data, 6), data[6]);
        }
        return ESP_ERR_INVALID_CRC;
    }

    uint32_t raw_hum = ((uint32_t)data[1] << 12) | ((uint32_t)data[2] << 4) | ((uint32_t)data[3] >> 4);
    uint32_t raw_temp = (((uint32_t)data[3] & 0x0F) << 16) | ((uint32_t)data[4] << 8) | data[5];
    if (raw_hum == 0 && raw_temp == 0) {
        ESP_LOGW(TAG, "AHT20 returned zero sample status=0x%02x raw=%02x %02x %02x %02x %02x %02x %02x",
                 data[0], data[0], data[1], data[2], data[3], data[4], data[5], data[6]);
        return ESP_ERR_INVALID_STATE;
    }

    *humidity_percent = (float)raw_hum * 100.0f / 1048576.0f;
    *temperature_c = (float)raw_temp * 200.0f / 1048576.0f - 50.0f;

    if (!s_aht20_logged_first_read) {
        s_aht20_logged_first_read = true;
        ESP_LOGI(TAG, "AHT20 first read ok temp=%.1f hum=%.1f",
                 (double)*temperature_c, (double)*humidity_percent);
    }

    return ESP_OK;
}
static esp_err_t mpu6050_read_accel(float *accel_g, float *accel_delta_g)
{
    static bool has_last = false;
    static float last_accel_g = 1.0f;

    if (s_mpu6050_dev == NULL || accel_g == NULL || accel_delta_g == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    uint8_t reg = 0x3B;
    uint8_t data[7] = {0};
    esp_err_t ret = i2c_master_transmit_receive(s_mpu6050_dev, &reg, 1, data, sizeof(data), 100);
    if (ret != ESP_OK) {
        return ret;
    }

    int16_t ax = (int16_t)(((uint16_t)data[0] << 8) | data[1]);
    int16_t ay = (int16_t)(((uint16_t)data[2] << 8) | data[3]);
    int16_t az = (int16_t)(((uint16_t)data[4] << 8) | data[5]);

    float ax_g = (float)ax / 16384.0f;
    float ay_g = (float)ay / 16384.0f;
    float az_g = (float)az / 16384.0f;
    float magnitude = sqrtf(ax_g * ax_g + ay_g * ay_g + az_g * az_g);

    *accel_g = magnitude;
    *accel_delta_g = has_last ? fabsf(magnitude - last_accel_g) : 0.0f;
    last_accel_g = magnitude;
    has_last = true;
    return ESP_OK;
}

static esp_err_t mq135_read_raw(uint16_t *raw)
{
    if (s_mq135_adc == NULL || raw == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    uint32_t sum = 0;
    for (int i = 0; i < 8; ++i) {
        int value = 0;
        esp_err_t ret = adc_oneshot_read(s_mq135_adc, s_mq135_channel, &value);
        if (ret != ESP_OK) {
            return ret;
        }
        sum += (uint32_t)value;
        vTaskDelay(pdMS_TO_TICKS(2));
    }

    *raw = (uint16_t)(sum / 8);
    return ESP_OK;
}

/* lora_send_task：初始化 SX1278 后，每秒读取最新融合快照并发送到 Gateway。 */
static void lora_send_task(void *arg)
{
    (void)arg;

    while (lora_spi_bus_init_once() != ESP_OK) {
        ESP_LOGE(TAG, "LoRa SPI 初始化失败，2 秒后重试");
        vTaskDelay(pdMS_TO_TICKS(2000));
    }

    while (lora_chip_configure() != ESP_OK) {
        ESP_LOGE(TAG, "SX1278 初始化失败，请检查 GPIO/SPI/RST/天线，3 秒后重试");
        vTaskDelay(pdMS_TO_TICKS(3000));
    }

    ESP_LOGI(TAG, "SX1278 发送模式就绪：433MHz BW125 SF7 CR4/5");

    uint32_t seq = 0;
    while (true) {
        node_fusion_sample_t sample = {0};
        if (xSemaphoreTake(s_sample_mutex, pdMS_TO_TICKS(50)) == pdTRUE) {
            sample = s_latest_sample;
            xSemaphoreGive(s_sample_mutex);
        } else {
            ESP_LOGW(TAG, "读取融合快照超时，本次 LoRa 使用空数据");
        }

        rescue_lora_packet_t packet = {
            .id = NODE_ID,
            .seq = seq++,
            .presence = sample.presence_score,
            .motion = sample.motion_score,
            .bpm = sample.breath_bpm,
            .conf = sample.confidence,
            .gas = sample.gas_raw,
            .temp_x10 = sample.temp_x10,
            .hum = sample.humidity,
        };

        esp_err_t ret = lora_send_packet(&packet);
        if (ret == ESP_OK) {
            ESP_LOGI(TAG, "LoRa sent id=%u seq=%" PRIu32 " presence=%u motion=%u bpm=%u conf=%u gas=%u temp_x10=%d hum=%u",
                     packet.id, packet.seq, packet.presence, packet.motion, packet.bpm,
                     packet.conf, packet.gas, packet.temp_x10, packet.hum);
        } else {
            ESP_LOGW(TAG, "LoRa 发送失败 seq=%" PRIu32 ": %s", packet.seq, esp_err_to_name(ret));
            if (ret == ESP_ERR_TIMEOUT) {
                lora_chip_configure();
            }
        }

        vTaskDelay(pdMS_TO_TICKS(1000));
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
    ESP_RETURN_ON_ERROR(gpio_config(&rst_config), TAG, "LoRa RST gpio_config failed");

    gpio_config_t dio0_config = {
        .pin_bit_mask = 1ULL << LORA_PIN_DIO0,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&dio0_config), TAG, "LoRa DIO0 gpio_config failed");

    return ESP_OK;
}

static esp_err_t lora_chip_configure(void)
{
    gpio_set_level(LORA_PIN_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(LORA_PIN_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(20));

    uint8_t version = lora_read_reg(REG_VERSION);
    if (version != LORA_EXPECTED_VERSION) {
        ESP_LOGE(TAG, "SX1278 version=0x%02x，期望 0x%02x", version, LORA_EXPECTED_VERSION);
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

    /* 与 Gateway 保持一致：BW=125kHz(0x7)、CR=4/5(0x1)、显式头模式。 */
    lora_write_reg(REG_MODEM_CONFIG_1, 0x72);
    /* SF=7，开启 payload CRC。 */
    lora_write_reg(REG_MODEM_CONFIG_2, 0x74);
    /* SF7/BW125 不需要低速率优化，AGC 自动增益打开。 */
    lora_write_reg(REG_MODEM_CONFIG_3, 0x04);
    lora_write_reg(REG_PREAMBLE_MSB, 0x00);
    lora_write_reg(REG_PREAMBLE_LSB, 0x08);
    lora_write_reg(REG_PAYLOAD_LENGTH, LORA_TX_PAYLOAD_LEN);

    /* PA_BOOST 17 dBm：0x80 | (17 - 2)，PA_DAC 保持 20 dBm 以下常规模式。 */
    lora_write_reg(REG_PA_CONFIG, 0x8F);
    lora_write_reg(REG_PA_DAC, 0x84);

    /* DIO0 映射 TxDone，清空历史中断并停在 Standby，等待发送任务触发。 */
    lora_write_reg(REG_DIO_MAPPING_1, 0x40);
    lora_write_reg(REG_IRQ_FLAGS, 0xFF);
    lora_set_op_mode(MODE_STDBY);
    return ESP_OK;
}

static esp_err_t lora_send_packet(const rescue_lora_packet_t *packet)
{
    if (packet == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t payload[LORA_TX_PAYLOAD_LEN] = {0};
    build_lora_payload(packet, payload);

    lora_set_op_mode(MODE_STDBY);
    lora_write_reg(REG_DIO_MAPPING_1, 0x40);
    lora_write_reg(REG_IRQ_FLAGS, 0xFF);
    lora_write_reg(REG_FIFO_ADDR_PTR, 0x00);

    for (size_t i = 0; i < LORA_TX_PAYLOAD_LEN; ++i) {
        lora_write_reg(REG_FIFO, payload[i]);
    }
    lora_write_reg(REG_PAYLOAD_LENGTH, LORA_TX_PAYLOAD_LEN);
    lora_set_op_mode(MODE_TX);

    int64_t start_ms = esp_timer_get_time() / 1000;
    while (((esp_timer_get_time() / 1000) - start_ms) < LORA_TX_TIMEOUT_MS) {
        uint8_t irq_flags = lora_read_reg(REG_IRQ_FLAGS);
        if ((irq_flags & IRQ_TX_DONE) != 0 || gpio_get_level(LORA_PIN_DIO0) == 1) {
            lora_write_reg(REG_IRQ_FLAGS, 0xFF);
            lora_set_op_mode(MODE_STDBY);
            return ESP_OK;
        }
        vTaskDelay(pdMS_TO_TICKS(5));
    }

    lora_write_reg(REG_IRQ_FLAGS, 0xFF);
    lora_set_op_mode(MODE_STDBY);
    return ESP_ERR_TIMEOUT;
}

static void build_lora_payload(const rescue_lora_packet_t *packet, uint8_t payload[LORA_TX_PAYLOAD_LEN])
{
    if (!echoguard_payload_encode(packet, payload)) {
        memset(payload, 0, LORA_TX_PAYLOAD_LEN);
    }
}

static void lora_set_op_mode(uint8_t mode)
{
    lora_write_reg(REG_OP_MODE, MODE_LONG_RANGE | mode);
}

static void lora_write_reg(uint8_t reg, uint8_t value)
{
    uint8_t tx_data[2] = {(uint8_t)(reg | 0x80), value};
    spi_transaction_t transaction = {
        .length = 16,
        .tx_buffer = tx_data,
    };
    esp_err_t ret = spi_device_transmit(s_lora_spi, &transaction);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "LoRa 写寄存器 0x%02x 失败: %s", reg, esp_err_to_name(ret));
    }
}

static uint8_t lora_read_reg(uint8_t reg)
{
    uint8_t tx_data[2] = {(uint8_t)(reg & 0x7F), 0x00};
    uint8_t rx_data[2] = {0};
    spi_transaction_t transaction = {
        .length = 16,
        .tx_buffer = tx_data,
        .rx_buffer = rx_data,
    };

    esp_err_t ret = spi_device_transmit(s_lora_spi, &transaction);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "LoRa 读寄存器 0x%02x 失败: %s", reg, esp_err_to_name(ret));
        return 0;
    }
    return rx_data[1];
}

static uint8_t clamp_u8_int(int value)
{
    if (value < 0) {
        return 0;
    }
    if (value > 100) {
        return 100;
    }
    return (uint8_t)value;
}
