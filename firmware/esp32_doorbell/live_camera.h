#pragma once

#include <esp_camera.h>

namespace live_camera {

using Capture = camera_fb_t *(*)();
using ClockReady = bool (*)();

bool start(Capture capture, ClockReady clockReady);
void serviceCapture();

class NetworkLock {
 public:
  NetworkLock();
  ~NetworkLock();
  NetworkLock(const NetworkLock &) = delete;
  NetworkLock &operator=(const NetworkLock &) = delete;
};

}  // namespace live_camera
