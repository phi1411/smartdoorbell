#include "live_camera.h"

#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <atomic>
#include <cstring>
#include <esp_log.h>
#include <esp_timer.h>
#include <esp_transport_ssl.h>
#include <esp_transport_ws.h>
#include <http_parser.h>

#if __has_include("config.local.h")
#include "config.local.h"
#else
#include "config.example.h"
#endif
#include "camera_pins.h"

namespace live_camera {
namespace {

constexpr char EVENT_PATH[] = "/api/device/doorbell";
constexpr size_t MAX_FRAME_BYTES = 96 * 1024;
constexpr uint32_t CAPTURE_TIMEOUT_MS = 1300;
constexpr int64_t MAX_FRAME_AGE_US = 750000;
constexpr uint32_t IDLE_INTERVAL_MS = 2500;
constexpr uint32_t SOCKET_FRAME_INTERVAL_MS = 200;
constexpr uint32_t HTTP_FRAME_INTERVAL_MS = 300;
constexpr uint32_t ACTIVE_DEMAND_INTERVAL_MS = 2000;
constexpr uint32_t DEMAND_VALIDITY_MS = 4500;
constexpr uint32_t RATE_LIMIT_INTERVAL_MS = 900;
constexpr uint32_t SOCKET_RETRY_INTERVAL_MS = 12000;
constexpr uint32_t LIVE_LOG_INTERVAL_MS = 5000;
constexpr int SOCKET_CONNECT_MS = 1800;
constexpr int SOCKET_WRITE_MS = 550;
constexpr uint32_t SOCKET_FRAME_WRITE_MS = 1500;
constexpr size_t SOCKET_FRAGMENT_BYTES = 4096;
constexpr int SOCKET_READ_MS = 150;
constexpr size_t MIN_DUAL_TLS_FREE_HEAP = 28 * 1024;
constexpr uint32_t FLASH_DURATION_US = 3000000;
constexpr uint32_t MAX_FLASH_RESPONSE_MS = 7000;
constexpr uint32_t FLASH_ACK_RETRY_MS = 15000;
constexpr char FLASH_HEADER[] = "X-Doorbell-Flash";
constexpr size_t LIVE_CHANNEL_LENGTH = 32;

class LiveChannelBody : public Stream {
 public:
  int available() override { return 0; }
  int read() override { return -1; }
  int peek() override { return -1; }

  size_t write(uint8_t byte) override { return write(&byte, 1); }
  size_t write(const uint8_t *data, size_t size) override {
    if (size > LIVE_CHANNEL_LENGTH - length) return 0;
    memcpy(bytes + length, data, size);
    length += size;
    return size;
  }

  char bytes[LIVE_CHANNEL_LENGTH + 1] = {};

 private:
  size_t length = 0;
};

struct Demand {
  bool confirmed = false;
  String channel;
  String flash;
};

enum class CaptureResult { Invalid, Stale, Ready };

struct Frame {
  uint32_t requestTime;
  uint8_t *data;
  size_t length;
  int64_t capturedAtUs;
  uint32_t captureMs;
  CaptureResult result;
};

struct Timing {
  unsigned count = 0;
  uint32_t totalMs = 0;
  uint32_t longestMs = 0;

  void add(uint32_t duration) {
    ++count;
    totalMs += duration;
    if (duration > longestMs) longestMs = duration;
  }

  uint32_t average() const { return count ? totalMs / count : 0; }
};

struct PreviewStats {
  Timing demand;
  Timing capture;
  Timing send;
  unsigned captureTimeouts = 0;
  unsigned invalid = 0;
  unsigned stale = 0;
  unsigned demandErrors = 0;
  unsigned socketFrames = 0;
  unsigned httpFrames = 0;
  unsigned sendErrors = 0;
  int lastHttpStatus = 0;
  size_t largestFrame = 0;
};

// All access to both transports is serialized by the network semaphore.
class PreviewSocket {
 public:
  void setOrigin(const String &eventUrl) {
    http_parser_url url{};
    if (http_parser_parse_url(eventUrl.c_str(), eventUrl.length(), 0, &url) != 0 ||
        !(url.field_set & (1 << UF_HOST)) || !(url.field_set & (1 << UF_PATH)) ||
        (url.field_set & ((1 << UF_USERINFO) | (1 << UF_QUERY) | (1 << UF_FRAGMENT)))) return;
    const auto server = url.field_data[UF_HOST];
    const auto endpoint = url.field_data[UF_PATH];
    host = eventUrl.substring(server.off, server.off + server.len);
    const size_t prefixLength = endpoint.len - (sizeof(EVENT_PATH) - 1);
    path = eventUrl.substring(endpoint.off, endpoint.off + prefixLength) + "/api/device/live/ws";
    port = url.port ? url.port : 443;
    // The ESP-IDF handshake can log request headers on write errors.
    esp_log_level_set("transport_ws", ESP_LOG_NONE);
  }

  bool openIfNeeded(const String &wanted) {
    if (ws && channel != wanted) close();
    if (ws) {
      if (!serviceControls()) {
        fail();
        Serial.println("[LIVE] WebSocket closed; using HTTPS frames.");
      }
      return ws != nullptr;
    }
    if (!canConnect(wanted)) return false;

    tls = esp_transport_ssl_init();
    if (tls) ws = esp_transport_ws_init(tls);
    if (ws) {
      esp_transport_ssl_set_cert_data(tls, DOORBELL_ROOT_CA, strlen(DOORBELL_ROOT_CA) + 1);
      const String auth = String("Bearer ") + DOORBELL_API_TOKEN;
      const String extraHeaders = String("X-Device-ID: ") + DOORBELL_DEVICE_ID + "\r\nX-Live-Stream: " + wanted + "\r\n";
      esp_transport_ws_config_t config{};
      config.ws_path = path.c_str();
      config.auth = auth.c_str();
      config.headers = extraHeaders.c_str();
      config.propagate_control_frames = false;
      if (esp_transport_ws_set_config(ws, &config) == ESP_OK &&
          esp_transport_connect(ws, host.c_str(), port, SOCKET_CONNECT_MS) >= 0 &&
          esp_transport_ws_get_upgrade_request_status(ws) == 101) {
        channel = wanted;
        failedBefore = false;
        Serial.printf("[LIVE] WebSocket connected, free heap %lu bytes.\n", static_cast<unsigned long>(ESP.getFreeHeap()));
        return true;
      }
    }
    const int status = ws ? esp_transport_ws_get_upgrade_request_status(ws) : -1;
    fail();
    Serial.printf("[LIVE] WebSocket unavailable (upgrade %d); using HTTPS frames.\n", status);
    return false;
  }

  bool send(const Frame &frame) {
    const uint32_t startedAt = millis();
    size_t offset = 0;
    while (offset < frame.length) {
      const size_t length = min(SOCKET_FRAGMENT_BYTES, frame.length - offset);
      const bool final = offset + length == frame.length;
      const auto opcode = static_cast<ws_transport_opcodes_t>(
          (offset ? WS_TRANSPORT_OPCODES_CONT : WS_TRANSPORT_OPCODES_BINARY) |
          (final ? WS_TRANSPORT_OPCODES_FIN : 0));
      const uint32_t elapsed = millis() - startedAt;
      // One binary message, split into WS fragments so a partial TLS write cannot
      // truncate a large JPEG spanning several TLS records.
      const int sent = elapsed < SOCKET_FRAME_WRITE_MS ? esp_transport_ws_send_raw(
          ws, opcode, reinterpret_cast<const char *>(frame.data + offset), length,
          min(SOCKET_WRITE_MS, static_cast<int>(SOCKET_FRAME_WRITE_MS - elapsed))) : -1;
      if (sent != static_cast<int>(length)) {
        Serial.printf("[LIVE] WebSocket send stopped at %u/%u bytes (fragment %u returned %d, %lu ms).\n",
                      static_cast<unsigned>(offset), static_cast<unsigned>(frame.length),
                      static_cast<unsigned>(length), sent, static_cast<unsigned long>(millis() - startedAt));
        fail();
        return false;
      }
      offset += length;
    }
    return true;
  }

  bool active() const { return ws != nullptr; }

  bool canConnect(const String &wanted) const {
    return channel != wanted && host.length() && (!failedBefore || millis() - failedAt >= SOCKET_RETRY_INTERVAL_MS);
  }

  void missingDemand() {
    if (!ws) return;
    fail();
    Serial.println("[LIVE] Preview demand timed out; using HTTPS frames after reconnecting.");
  }

  void close() {
    if (ws) {
      esp_transport_close(ws);
      esp_transport_destroy(ws);
      ws = nullptr;
    }
    if (tls) {
      esp_transport_destroy(tls);
      tls = nullptr;
    }
    channel.clear();
  }

 private:
  bool serviceControls() {
    const int pending = esp_transport_poll_read(ws, 0);
    if (pending <= 0) return pending == 0;
    char data[125]; // The protocol only expects server-side control frames.
    return esp_transport_read(ws, data, sizeof(data), SOCKET_READ_MS) == 0;
  }

  void fail() {
    close();
    failedAt = millis();
    failedBefore = true;
  }

  esp_transport_handle_t tls = nullptr;
  esp_transport_handle_t ws = nullptr;
  String host;
  String path;
  String channel;
  uint16_t port = 443;
  uint32_t failedAt = 0;
  bool failedBefore = false;
};

SemaphoreHandle_t network = nullptr;
QueueHandle_t frames = nullptr;
WiFiClientSecure https;
PreviewSocket previewSocket;
std::atomic<uint32_t> pendingCapture{0};
Capture readCamera = nullptr;
ClockReady verifyClock = nullptr;
String demandUrl;
String frameUrl;
String flashUrl;
esp_timer_handle_t flashTimer = nullptr;
String latestFlash;
bool needsFlashAck = false;
uint32_t flashStartedAt = 0;
std::atomic<bool> flashFinished{false};

void releaseHttpIfLowOnMemory() {
  if (previewSocket.active() && ESP.getFreeHeap() < MIN_DUAL_TLS_FREE_HEAP) https.stop();
}

bool beginRequest(HTTPClient &http, WiFiClientSecure &tls, const String &url) {
  http.setConnectTimeout(1800);
  http.setTimeout(2200);
  http.setReuse(true);
  http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
  if (!http.begin(tls, url)) return false;
  http.addHeader("Authorization", String("Bearer ") + DOORBELL_API_TOKEN);
  http.addHeader("X-Device-ID", DOORBELL_DEVICE_ID);
  return true;
}

bool isHexId(const String &id) {
  if (id.length() != LIVE_CHANNEL_LENGTH) return false;
  for (size_t i = 0; i < id.length(); ++i) {
    if (strchr("0123456789abcdef", id[i]) == nullptr || id[i] == '\0') return false;
  }
  return true;
}

Demand fetchDemand(HTTPClient &http) {
  Demand demand;
  if (xSemaphoreTake(network, 0) != pdTRUE) return demand;
  if (beginRequest(http, https, demandUrl)) {
    const bool canFlash = flashTimer && !needsFlashAck;
    if (canFlash) {
      const char *headers[] = {FLASH_HEADER};
      http.addHeader("X-Doorbell-Flash-Capable", "1");
      http.collectHeaders(headers, sizeof(headers) / sizeof(headers[0]));
    }
    const uint32_t requestTime = millis();
    const int status = http.GET();
    if (canFlash && (status == HTTP_CODE_OK || status == HTTP_CODE_NO_CONTENT) &&
        static_cast<uint32_t>(millis() - requestTime) <= MAX_FLASH_RESPONSE_MS) {
      demand.flash = http.header(FLASH_HEADER);
    }
    if (status == HTTP_CODE_OK) {
      // Render may send a chunked response without a Content-Length header.
      LiveChannelBody body;
      if (http.writeToStream(&body) == LIVE_CHANNEL_LENGTH) {
        demand.channel = body.bytes;
        demand.confirmed = isHexId(demand.channel);
      } else {
        http.setReuse(false);
      }
    } else if (status == HTTP_CODE_NO_CONTENT) {
      demand.confirmed = true;
    } else {
      http.setReuse(false);
    }
    http.end();
  }
  releaseHttpIfLowOnMemory();
  xSemaphoreGive(network);
  if (!isHexId(demand.channel)) demand.channel.clear();
  if (!isHexId(demand.flash)) demand.flash.clear();
  return demand;
}

void finishFlash(void *) {
  digitalWrite(FLASH_LED_PIN, LOW);
  flashFinished.store(true);
}

void startFlash(const String &id) {
  if (!flashTimer || needsFlashAck || id == latestFlash) return;
  latestFlash = id;
  flashFinished.store(false);
  flashStartedAt = millis();
  digitalWrite(FLASH_LED_PIN, HIGH);
  if (esp_timer_start_once(flashTimer, FLASH_DURATION_US) != ESP_OK) {
    digitalWrite(FLASH_LED_PIN, LOW);
    Serial.println("[FLASH] Timer could not start.");
    return;
  }
  needsFlashAck = true;
  Serial.println("[FLASH] Built-in flash on for 3 seconds.");
}

void sendFlashAck(HTTPClient &http) {
  if (!needsFlashAck || !flashFinished.load()) return;
  if (static_cast<uint32_t>(millis() - flashStartedAt) >= FLASH_ACK_RETRY_MS) {
    needsFlashAck = false;
    Serial.println("[FLASH] Confirming the completed pulse timed out.");
    return;
  }
  if (xSemaphoreTake(network, 0) != pdTRUE) return;
  if (beginRequest(http, https, flashUrl + latestFlash + "/ack")) {
    if (http.POST(String()) == HTTP_CODE_NO_CONTENT) {
      needsFlashAck = false;
      Serial.println("[FLASH] Flash off and pulse confirmed by server.");
    } else {
      http.setReuse(false);
    }
    http.end();
  }
  releaseHttpIfLowOnMemory();
  xSemaphoreGive(network);
}

int postPreview(HTTPClient &http, const String &channel, const Frame &frame) {
  int status = 0;
  if (beginRequest(http, https, frameUrl)) {
    http.addHeader("Content-Type", "image/jpeg");
    http.addHeader("X-Live-Stream", channel);
    status = http.POST(frame.data, frame.length);
    if (status != HTTP_CODE_NO_CONTENT) http.setReuse(false);
    http.end();
  }
  return status;
}

bool prepareSocket(const String &channel, bool demandTimedOut = false) {
  if (xSemaphoreTake(network, 0) != pdTRUE) return false;
  if (!channel.length()) {
    if (demandTimedOut) previewSocket.missingDemand();
    previewSocket.close();
    https.stop();
  } else if (previewSocket.canConnect(channel)) {
    https.stop();
  }
  const bool ready = channel.length() && previewSocket.openIfNeeded(channel);
  releaseHttpIfLowOnMemory();
  xSemaphoreGive(network);
  return ready;
}

enum PreviewStatus { NoFrame, SocketSent, HttpSent, RateLimited, LeaseGone };

PreviewStatus transmitPreview(HTTPClient &http, const String &channel, const Frame &frame,
                              uint32_t confirmedAt, uint32_t &sendStartedAt, PreviewStats &stats) {
  if (xSemaphoreTake(network, 0) != pdTRUE) return NoFrame;
  PreviewStatus status = NoFrame;
  const bool fresh = esp_timer_get_time() - frame.capturedAtUs <= MAX_FRAME_AGE_US;
  if (!fresh) ++stats.stale;
  if (fresh && millis() - confirmedAt < DEMAND_VALIDITY_MS) {
    sendStartedAt = millis();
    if (frame.length > stats.largestFrame) stats.largestFrame = frame.length;
    if (previewSocket.active()) {
      // The transport masks the writable JPEG in place. Discard it if sending fails.
      if (previewSocket.send(frame)) {
        status = SocketSent;
        ++stats.socketFrames;
      }
    } else {
      const int response = postPreview(http, channel, frame);
      stats.lastHttpStatus = response;
      if (response == HTTP_CODE_NO_CONTENT) {
        status = HttpSent;
        ++stats.httpFrames;
      }
      if (response == HTTP_CODE_TOO_MANY_REQUESTS) status = RateLimited;
      if (response == HTTP_CODE_CONFLICT) status = LeaseGone;
    }
    stats.send.add(millis() - sendStartedAt);
    if (status != SocketSent && status != HttpSent) ++stats.sendErrors;
  }
  xSemaphoreGive(network);
  return status;
}

PreviewStatus sendPreview(HTTPClient &http, const String &channel, uint32_t confirmedAt,
                          uint32_t &sendStartedAt, PreviewStats &stats) {
  Frame frame{};
  while (xQueueReceive(frames, &frame, 0) == pdTRUE) free(frame.data);
  const uint32_t startTime = millis();
  const uint32_t requestTime = startTime ? startTime : 1;
  pendingCapture.store(requestTime);
  PreviewStatus status = NoFrame;
  bool captured = false;
  while (static_cast<uint32_t>(millis() - startTime) < CAPTURE_TIMEOUT_MS) {
    if (xQueueReceive(frames, &frame, pdMS_TO_TICKS(60)) != pdTRUE) continue;
    if (frame.requestTime == requestTime) {
      captured = true;
      stats.capture.add(frame.captureMs);
      if (frame.result == CaptureResult::Stale) ++stats.stale;
      if (frame.result == CaptureResult::Invalid) ++stats.invalid;
      if (frame.data) status = transmitPreview(http, channel, frame, confirmedAt, sendStartedAt, stats);
      free(frame.data);
      break;
    }
    free(frame.data);
  }
  auto expired = requestTime;
  pendingCapture.compare_exchange_strong(expired, 0);
  if (!captured) ++stats.captureTimeouts;
  return status;
}

void run(void *) {
  HTTPClient http;
  https.setCACert(DOORBELL_ROOT_CA);
  https.setHandshakeTimeout(3);
  String channel;
  uint32_t lastDemandAttempt = 0;
  uint32_t confirmedAt = 0;
  uint32_t statsAt = millis();
  PreviewStats stats;
  for (;;) {
    PreviewStatus status = NoFrame;
    const uint32_t cycleAt = millis();
    uint32_t sendStartedAt = cycleAt;
    bool socketReady = false;
    if (WiFi.status() == WL_CONNECTED && verifyClock()) {
      sendFlashAck(http);
      const uint32_t now = millis();
      if (!channel.length() || now - lastDemandAttempt >= ACTIVE_DEMAND_INTERVAL_MS) {
        const Demand demand = fetchDemand(http);
        lastDemandAttempt = millis();
        stats.demand.add(lastDemandAttempt - now);
        if (demand.confirmed) {
          channel = demand.channel;
          confirmedAt = lastDemandAttempt;
        } else {
          ++stats.demandErrors;
        }
        if (demand.flash.length()) startFlash(demand.flash);
      }
      const bool demandTimedOut = channel.length() && millis() - confirmedAt >= DEMAND_VALIDITY_MS;
      if (demandTimedOut) channel.clear();
      socketReady = prepareSocket(channel, demandTimedOut);
      if (channel.length() && millis() - confirmedAt < DEMAND_VALIDITY_MS) {
        status = sendPreview(http, channel, confirmedAt, sendStartedAt, stats);
      }
    } else if (channel.length()) {
      channel.clear();
      prepareSocket(channel);
    }

    if (status == LeaseGone) channel.clear();
    const uint32_t now = millis();
    if (now - statsAt >= LIVE_LOG_INTERVAL_MS) {
      if (channel.length() || stats.capture.count || stats.captureTimeouts) {
        Serial.printf("[LIVE] %u JPEG via WSS, %u via HTTPS, %u send errors in %lu ms (max JPEG %u bytes; HTTP %d).\n",
                      stats.socketFrames, stats.httpFrames, stats.sendErrors, static_cast<unsigned long>(now - statsAt),
                      static_cast<unsigned>(stats.largestFrame), stats.lastHttpStatus);
        Serial.printf("[LIVE] Capture %u returned, %u invalid, %u stale, %u timed out; %u demand errors. Avg/max ms: capture %lu/%lu, demand %lu/%lu, send %lu/%lu.\n",
                      stats.capture.count, stats.invalid, stats.stale, stats.captureTimeouts, stats.demandErrors,
                      static_cast<unsigned long>(stats.capture.average()), static_cast<unsigned long>(stats.capture.longestMs),
                      static_cast<unsigned long>(stats.demand.average()), static_cast<unsigned long>(stats.demand.longestMs),
                      static_cast<unsigned long>(stats.send.average()), static_cast<unsigned long>(stats.send.longestMs));
      }
      stats = {};
      statsAt = now;
    }

    uint32_t interval = IDLE_INTERVAL_MS;
    if (needsFlashAck || channel.length()) interval = socketReady ? SOCKET_FRAME_INTERVAL_MS : HTTP_FRAME_INTERVAL_MS;
    if (status == RateLimited) interval = RATE_LIMIT_INTERVAL_MS;
    const uint32_t cycleMs = now - sendStartedAt;
    // Count work in the cadence instead of adding a second wait after each send.
    const TickType_t wait = interval > cycleMs ? pdMS_TO_TICKS(interval - cycleMs) : 0;
    vTaskDelay(wait ? wait : 1);
  }
}

}  // namespace

bool start(Capture capture, ClockReady clockReady) {
  String eventUrl(DOORBELL_API_URL);
  if (!eventUrl.endsWith(EVENT_PATH)) return false;
  previewSocket.setOrigin(eventUrl);
  eventUrl.remove(eventUrl.length() - sizeof(EVENT_PATH) + 1);
  demandUrl = eventUrl + "/api/device/live/demand";
  frameUrl = eventUrl + "/api/device/live/frame";
  flashUrl = eventUrl + "/api/device/flash/";
  readCamera = capture;
  verifyClock = clockReady;

  network = xSemaphoreCreateMutex();
  frames = xQueueCreate(1, sizeof(Frame));
  esp_timer_create_args_t flashArgs{};
  flashArgs.callback = finishFlash;
  flashArgs.name = "door_flash";
  if (esp_timer_create(&flashArgs, &flashTimer) != ESP_OK) flashTimer = nullptr;
  if (network && frames && xTaskCreate(run, "camera_live", 8192, nullptr, 1, nullptr) == pdPASS) return true;
  if (flashTimer) esp_timer_delete(flashTimer);
  if (frames) vQueueDelete(frames);
  if (network) vSemaphoreDelete(network);
  flashTimer = nullptr;
  frames = nullptr;
  network = nullptr;
  return false;
}

void serviceCapture() {
  const uint32_t requestTime = pendingCapture.exchange(0);
  if (!requestTime) return;
  const auto elapsed = static_cast<int32_t>(millis() - requestTime);
  if (elapsed > static_cast<int32_t>(CAPTURE_TIMEOUT_MS)) return;
  Frame result{requestTime, nullptr, 0, 0, 0, CaptureResult::Invalid};
  const uint32_t startedAt = millis();
  camera_fb_t *source = readCamera();
  if (source) {
    const int64_t capturedAtUs = static_cast<int64_t>(source->timestamp.tv_sec) * 1000000 + source->timestamp.tv_usec;
    const int64_t ageUs = esp_timer_get_time() - capturedAtUs;
    if (ageUs < 0 || ageUs > MAX_FRAME_AGE_US) {
      result.result = CaptureResult::Stale;
    } else if (source->format == PIXFORMAT_JPEG &&
        source->len >= 4 && source->len <= MAX_FRAME_BYTES && source->buf[0] == 0xff && source->buf[1] == 0xd8) {
      result.data = static_cast<uint8_t *>(ps_malloc(source->len));
      if (result.data) {
        result.result = CaptureResult::Ready;
        result.length = source->len;
        result.capturedAtUs = capturedAtUs;
        memcpy(result.data, source->buf, source->len);
      }
    }
    esp_camera_fb_return(source);
  }
  result.captureMs = millis() - startedAt;
  if (xQueueSend(frames, &result, 0) != pdTRUE) free(result.data);
}

NetworkLock::NetworkLock() {
  if (!network) return;
  xSemaphoreTake(network, portMAX_DELAY);
  // A doorbell snapshot takes priority and needs its own TLS client.
  previewSocket.close();
  https.stop();
}

NetworkLock::~NetworkLock() {
  if (network) xSemaphoreGive(network);
}

}  // namespace live_camera
