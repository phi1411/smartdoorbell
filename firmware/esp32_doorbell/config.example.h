#pragma once

// Copy this file to config.local.h and edit ONLY the local copy.
// This pin map is for the classic AI Thinker ESP32-CAM with OV2640.
// A label reading only "ESP32-CAM" does not confirm the board variant.
#define DOORBELL_CAMERA_AI_THINKER 1

// Leave empty to test the button/camera without uploading any images.
#define DOORBELL_WIFI_SSID ""
#define DOORBELL_WIFI_PASSWORD ""

// A custom project endpoint, NOT a Telegram URL or the Hermes dashboard.
// Full HTTPS URL ending in /api/device/doorbell. Live preview uses the same host.
#define DOORBELL_API_URL ""
#define DOORBELL_API_TOKEN ""
#define DOORBELL_DEVICE_ID "doorbell-01"

// PEM root CA certificate(s) trusted for your API's HTTPS hostname.
// Obtain the correct CA after the server hostname is known.
// Do not replace certificate verification with setInsecure().
static constexpr char DOORBELL_ROOT_CA[] = R"PEM(
)PEM";
