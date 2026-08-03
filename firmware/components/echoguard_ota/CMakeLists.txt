idf_component_register(
    SRCS "echoguard_ota.c"
    INCLUDE_DIRS "include"
    REQUIRES app_update esp_http_server esp_partition esp_app_format mbedtls esp_system
)
