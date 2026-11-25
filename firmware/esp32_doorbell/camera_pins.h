#pragma once

#if !defined(DOORBELL_CAMERA_AI_THINKER) || DOORBELL_CAMERA_AI_THINKER != 1
#error "Select a verified camera pin map before building for a different board."
#endif

// Classic AI Thinker ESP32-CAM, matching Espressif's CameraWebServer example.
constexpr int CAMERA_PWDN = 32;
constexpr int CAMERA_RESET = -1;
constexpr int CAMERA_XCLK = 0;
constexpr int CAMERA_SDA = 26;
constexpr int CAMERA_SCL = 27;
constexpr int CAMERA_D0 = 5;
constexpr int CAMERA_D1 = 18;
constexpr int CAMERA_D2 = 19;
constexpr int CAMERA_D3 = 21;
constexpr int CAMERA_D4 = 36;
constexpr int CAMERA_D5 = 39;
constexpr int CAMERA_D6 = 34;
constexpr int CAMERA_D7 = 35;
constexpr int CAMERA_VSYNC = 25;
constexpr int CAMERA_HREF = 23;
constexpr int CAMERA_PCLK = 22;

// The white flash on the camera module uses GPIO4. No microSD is in use.
constexpr int FLASH_LED_PIN = 4;

// Button between GPIO13 and GND, using the ESP32's internal pull-up.
// GPIO13 is shared with the microSD interface. This sketch does not use SD.
constexpr int DOORBELL_BUTTON_PIN = 13;
