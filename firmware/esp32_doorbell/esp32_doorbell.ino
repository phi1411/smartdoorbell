// Smart Doorbell: upload a fresh JPEG per press and stream live JPEGs on demand.
// Prepared for classic AI Thinker ESP32-CAM + OV2640. Verify your board first.
// The ESP32 uses its own device credential for HTTPS and WSS.

#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <esp_camera.h>
#include <esp_system.h>
#include <time.h>
#include <cstring>

#if __has_include("config.local.h")
#include "config.local.h"
#else
#include "config.example.h"
#endif
#include "camera_pins.h"
#include "button_state.h"
#include "live_camera.h"

constexpr uint32_t DEBOUNCE_MS = 50;
constexpr uint32_t COOLDOWN_MS = 5000;
constexpr uint32_t WIFI_RETRY_MS = 15000;
constexpr uint32_t CAMERA_RETRY_MS = 12000;
constexpr uint8_t MAX_CAMERA_RETRIES = 3;
constexpr uint32_t HTTP_CONNECT_TIMEOUT_MS = 10000;
constexpr uint16_t HTTP_READ_TIMEOUT_MS = 15000;
constexpr size_t MAX_IMAGE_BYTES = 512 * 1024;

bool cameraReady = false;
bool liveStarted = false;
bool wifiEnabled = false;
bool lastWifiConnected = false;
uint32_t lastWifiAttemptAt = 0;
uint32_t lastCameraAttemptAt = 0;
uint8_t cameraRetryCount = 0;
doorbell::ButtonState buttonState(DEBOUNCE_MS);
doorbell::Cooldown cooldown(COOLDOWN_MS);

bool validDeviceId() {
  const char *id = DOORBELL_DEVICE_ID;
  const size_t length = strlen(id);
  if (length == 0 || length > 48) return false;
  for (size_t i = 0; i < length; ++i) {
    const char c = id[i];
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '-' || c == '_')) return false;
  }
  return true;
}

bool uploadConfigured() {
  return strncmp(DOORBELL_API_URL, "https://", 8) == 0 &&
         strlen(DOORBELL_API_URL) > 8 && strlen(DOORBELL_API_TOKEN) >= 32 &&
         strchr(DOORBELL_API_TOKEN, '\r') == nullptr &&
         strchr(DOORBELL_API_TOKEN, '\n') == nullptr && validDeviceId() &&
         strstr(DOORBELL_ROOT_CA, "-----BEGIN CERTIFICATE-----") != nullptr;
}

bool clockReady() {
  // TLS verifies the certificate's validity dates. NTP must set the clock first.
  return time(nullptr) >= static_cast<time_t>(1704067200);  // 2024-01-01 UTC
}

bool initCamera() {
  if (!psramFound()) {
    Serial.println("[CAMERA] PSRAM unavailable. Check board selection and hardware.");
    return false;
  }

  camera_config_t cfg = {};
  cfg.ledc_channel = LEDC_CHANNEL_0;
  cfg.ledc_timer = LEDC_TIMER_0;
  cfg.pin_d0 = CAMERA_D0;
  cfg.pin_d1 = CAMERA_D1;
  cfg.pin_d2 = CAMERA_D2;
  cfg.pin_d3 = CAMERA_D3;
  cfg.pin_d4 = CAMERA_D4;
  cfg.pin_d5 = CAMERA_D5;
  cfg.pin_d6 = CAMERA_D6;
  cfg.pin_d7 = CAMERA_D7;
  cfg.pin_xclk = CAMERA_XCLK;
  cfg.pin_pclk = CAMERA_PCLK;
  cfg.pin_vsync = CAMERA_VSYNC;
  cfg.pin_href = CAMERA_HREF;
  cfg.pin_sccb_sda = CAMERA_SDA;
  cfg.pin_sccb_scl = CAMERA_SCL;
  cfg.pin_pwdn = CAMERA_PWDN;
  cfg.pin_reset = CAMERA_RESET;
  cfg.pixel_format = PIXFORMAT_JPEG;
  cfg.jpeg_quality = 10;
  cfg.fb_location = CAMERA_FB_IN_PSRAM;

  constexpr struct {
    uint32_t clockHz;
    framesize_t initialSize;
    framesize_t size;
    unsigned width;
    unsigned height;
    bool latest;
  } modes[] = {
    {10000000, FRAMESIZE_VGA, FRAMESIZE_VGA, 640, 480, true},
    {10000000, FRAMESIZE_VGA, FRAMESIZE_VGA, 640, 480, false},
    // Match CameraWebServer's buffer allocation before switching to SVGA.
    {20000000, FRAMESIZE_UXGA, FRAMESIZE_SVGA, 800, 600, true},
    {10000000, FRAMESIZE_CIF, FRAMESIZE_CIF, 400, 296, false},
    {10000000, FRAMESIZE_QVGA, FRAMESIZE_QVGA, 320, 240, false},
  };

  for (const auto &mode : modes) {
    if (mode.clockHz == 10000000) {
      pinMode(CAMERA_PWDN, OUTPUT);
      digitalWrite(CAMERA_PWDN, HIGH);
      delay(160);
      digitalWrite(CAMERA_PWDN, LOW);
      delay(100);
    }
    cfg.xclk_freq_hz = mode.clockHz;
    cfg.frame_size = mode.initialSize;
    cfg.fb_count = mode.latest ? 2 : 1;
    cfg.grab_mode = mode.latest ? CAMERA_GRAB_LATEST : CAMERA_GRAB_WHEN_EMPTY;
    const unsigned clockMHz = mode.clockHz / 1000000;
    Serial.printf("[CAMERA] Trying %u MHz %ux%u.\n", clockMHz, mode.width, mode.height);
    const esp_err_t error = esp_camera_init(&cfg);
    if (error != ESP_OK) {
      Serial.printf("[CAMERA] Configuration failed: 0x%x.\n", static_cast<unsigned>(error));
      continue;
    }
    if (mode.initialSize != mode.size) {
      sensor_t *sensor = esp_camera_sensor_get();
      // CameraWebServer starts in QVGA before the operator selects SVGA.
      if (!sensor || sensor->set_framesize(sensor, FRAMESIZE_QVGA) != 0 ||
          sensor->set_framesize(sensor, mode.size) != 0) {
        esp_camera_deinit();
        Serial.println("[CAMERA] Failed to set the preview resolution.");
        continue;
      }
    }
    delay(600);
    camera_fb_t *frame = esp_camera_fb_get();
    if (frame && frame->format == PIXFORMAT_JPEG && frame->len >= 4 &&
        frame->width == mode.width && frame->height == mode.height &&
        frame->buf[0] == 0xff && frame->buf[1] == 0xd8) {
      Serial.printf("[CAMERA] Ready: JPEG %ux%u at %u MHz (%u bytes).\n",
                    static_cast<unsigned>(frame->width), static_cast<unsigned>(frame->height),
                    clockMHz, static_cast<unsigned>(frame->len));
      esp_camera_fb_return(frame);
      return true;
    }
    if (frame) esp_camera_fb_return(frame);
    esp_camera_deinit();
    Serial.println("[CAMERA] Initialized but received no valid JPEG.");
  }

  Serial.println("[CAMERA] All camera configurations failed.");
  return false;
}

camera_fb_t *captureFreshFrame() {
  // Drop the frame buffered while waiting for a press before requesting another.
  camera_fb_t *stale = esp_camera_fb_get();
  if (stale == nullptr) return nullptr;
  esp_camera_fb_return(stale);
  return esp_camera_fb_get();
}

void sendFrame(camera_fb_t *frame) {
  if (!uploadConfigured()) {
    Serial.println("[TEST] JPEG captured; upload disabled. Configure HTTPS URL, token and root CA.");
    return;
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[UPLOAD] Wi-Fi offline. Image not queued or saved; press again after reconnection.");
    return;
  }
  if (!clockReady()) {
    Serial.println("[UPLOAD] Waiting for NTP time before verified TLS. Press again shortly.");
    return;
  }

  char eventId[80];
  snprintf(eventId, sizeof(eventId), "%s-%08lx%08lx", DOORBELL_DEVICE_ID,
           static_cast<unsigned long>(esp_random()),
           static_cast<unsigned long>(esp_random()));

  live_camera::NetworkLock network;
  WiFiClientSecure tls;
  tls.setCACert(DOORBELL_ROOT_CA);
  tls.setHandshakeTimeout(15);
  HTTPClient http;
  http.setConnectTimeout(HTTP_CONNECT_TIMEOUT_MS);
  http.setTimeout(HTTP_READ_TIMEOUT_MS);
  http.setReuse(false);
  // Do not forward a device credential to a redirect target.
  http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
  if (!http.begin(tls, DOORBELL_API_URL)) {
    Serial.println("[UPLOAD] Could not initialize HTTPS request.");
    return;
  }
  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("Authorization", String("Bearer ") + DOORBELL_API_TOKEN);
  http.addHeader("X-Device-ID", DOORBELL_DEVICE_ID);
  http.addHeader("X-Event-ID", eventId);
  http.addHeader("Accept", "application/json");
  http.setUserAgent("SmartDoorbellESP32/0.1");

  Serial.printf("[UPLOAD] Event %s, %u bytes.\n", eventId, static_cast<unsigned>(frame->len));
  // One attempt per press. A lost response can mean the server already accepted
  // the image, so automatic resends are intentionally avoided.
  const int status = http.POST(frame->buf, frame->len);
  if (status >= 200 && status < 300) {
    Serial.printf("[UPLOAD] Accepted, HTTP %d. AI/Telegram completion is tracked on the server.\n", status);
  } else if (status > 0) {
    Serial.printf("[UPLOAD] Rejected/unfinished, HTTP %d. Check API server logs.\n", status);
  } else {
    Serial.printf("[UPLOAD] Network/TLS error %d. Delivery uncertain; no automatic retry.\n", status);
  }
  // Never print the response body: it may contain private image analysis.
  http.end();
}

void ringDoorbell() {
  if (!cooldown.accept(millis())) {
    Serial.println("[BUTTON] Cooldown: wait 5 seconds between attempts.");
    return;
  }
  if (!cameraReady) {
    Serial.println("[CAMERA] Not ready. Correct the hardware/configuration, then reset.");
    return;
  }

  camera_fb_t *frame = captureFreshFrame();
  if (frame == nullptr) {
    Serial.println("[CAMERA] Capture failed.");
    return;
  }
  if (frame->format != PIXFORMAT_JPEG || frame->len < 4 || frame->len > MAX_IMAGE_BYTES ||
      frame->buf[0] != 0xff || frame->buf[1] != 0xd8) {
    Serial.println("[CAMERA] Invalid or oversized JPEG; upload skipped.");
  } else {
    Serial.printf("[CAMERA] Captured %ux%u, %u bytes.\n",
                  static_cast<unsigned>(frame->width), static_cast<unsigned>(frame->height),
                  static_cast<unsigned>(frame->len));
    sendFrame(frame);
  }
  esp_camera_fb_return(frame);
}

void resetButtonAfterRequest() {
  // Discard transitions during a blocking upload. Require a stable release
  // before another physical press can trigger a capture.
  buttonState.reset(digitalRead(DOORBELL_BUTTON_PIN) == LOW, millis());
}

void pollButton() {
  if (buttonState.update(digitalRead(DOORBELL_BUTTON_PIN) == LOW, millis())) {
    Serial.println("[BUTTON] Pressed.");
    ringDoorbell();
    resetButtonAfterRequest();
  }
}

void maintainWifi() {
  if (!wifiEnabled) return;
  const bool connected = WiFi.status() == WL_CONNECTED;
  if (connected != lastWifiConnected) {
    lastWifiConnected = connected;
    Serial.println(connected ? "[WIFI] Connected." : "[WIFI] Disconnected.");
  }
  if (!connected && static_cast<uint32_t>(millis() - lastWifiAttemptAt) >= WIFI_RETRY_MS) {
    lastWifiAttemptAt = millis();
    Serial.println("[WIFI] Reconnecting...");
    WiFi.reconnect();
  }
}

void maybeStartLive() {
  if (!liveStarted && wifiEnabled && cameraReady && uploadConfigured() &&
      live_camera::start(esp_camera_fb_get, clockReady)) {
    liveStarted = true;
    Serial.println("[CAMERA] Live preview available on demand.");
  }
}

void retryCameraInit(bool manual) {
  if (cameraReady) {
    if (manual) Serial.println("[CAMERA] Already ready.");
    return;
  }
  lastCameraAttemptAt = millis();
  if (!manual) ++cameraRetryCount;
  Serial.printf("[CAMERA] Retrying initialization (%s)...\n", manual ? "manual" : "auto");
  cameraReady = initCamera();
  maybeStartLive();
}

void setup() {
  pinMode(FLASH_LED_PIN, OUTPUT);
  digitalWrite(FLASH_LED_PIN, LOW);
  Serial.begin(115200);
  delay(300);
  Serial.println("\nSmart Doorbell ESP32-CAM / AI Thinker pin map");
  pinMode(DOORBELL_BUTTON_PIN, INPUT_PULLUP);
  buttonState.reset(digitalRead(DOORBELL_BUTTON_PIN) == LOW, millis());
  cameraReady = initCamera();
  lastCameraAttemptAt = millis();

  wifiEnabled = strlen(DOORBELL_WIFI_SSID) > 0;
  if (wifiEnabled) {
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.setSleep(false);
    WiFi.begin(DOORBELL_WIFI_SSID, DOORBELL_WIFI_PASSWORD);
    // Slightly reduce peak RF draw to lower brownout risk on marginal USB power.
    WiFi.setTxPower(WIFI_POWER_17dBm);
    lastWifiAttemptAt = millis();
    configTime(0, 0, "pool.ntp.org", "time.cloudflare.com");
    Serial.println("[WIFI] Connecting to configured 2.4 GHz network...");
  } else {
    Serial.println("[TEST] Wi-Fi not configured. Camera/button diagnostics only.");
  }
  Serial.println(uploadConfigured() ? "[CONFIG] HTTPS upload configured." :
                                    "[CONFIG] Upload disabled until server credentials/CA are configured.");
  maybeStartLive();
  Serial.println("Release the button, then press it. Serial C captures; R retries camera.");
}

void loop() {
  maintainWifi();
  if (!cameraReady && cameraRetryCount < MAX_CAMERA_RETRIES &&
      static_cast<uint32_t>(millis() - lastCameraAttemptAt) >= CAMERA_RETRY_MS) {
    retryCameraInit(false);
  }
  pollButton();
  if (Serial.available() > 0) {
    const char command = static_cast<char>(Serial.read());
    if (command == 'c' || command == 'C') {
      ringDoorbell();
      resetButtonAfterRequest();
    } else if (command == 'r' || command == 'R') {
      retryCameraInit(true);
    }
  }
  live_camera::serviceCapture();
  delay(5);
}
