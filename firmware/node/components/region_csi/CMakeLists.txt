idf_component_register(
    SRCS "echoguard_region_csi.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_wifi esp_timer echoguard_region
)
