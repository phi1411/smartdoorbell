#pragma once

namespace doorbell {

using Tick = unsigned int;
static_assert(sizeof(Tick) == 4, "Button timing requires a 32-bit unsigned type.");

// Pure timing logic, shared by the firmware and native regression tests.
class ButtonState {
 public:
  explicit ButtonState(Tick debounceMs) : debounceMs_(debounceMs) {}

  void reset(bool pressed, Tick now) {
    rawPressed_ = pressed;
    stablePressed_ = pressed;
    armed_ = false;
    changedAt_ = now;
  }

  bool update(bool pressed, Tick now) {
    if (pressed != rawPressed_) {
      rawPressed_ = pressed;
      changedAt_ = now;
    }
    if (static_cast<Tick>(now - changedAt_) < debounceMs_) return false;
    if (!rawPressed_) armed_ = true;
    if (rawPressed_ == stablePressed_) return false;
    stablePressed_ = rawPressed_;
    if (stablePressed_ && armed_) {
      armed_ = false;
      return true;
    }
    return false;
  }

 private:
  const Tick debounceMs_;
  bool rawPressed_ = false;
  bool stablePressed_ = false;
  bool armed_ = false;
  Tick changedAt_ = 0;
};

class Cooldown {
 public:
  explicit Cooldown(Tick intervalMs) : intervalMs_(intervalMs) {}

  bool accept(Tick now) {
    if (hasEvent_ && static_cast<Tick>(now - lastAt_) < intervalMs_) return false;
    hasEvent_ = true;
    lastAt_ = now;
    return true;
  }

 private:
  const Tick intervalMs_;
  bool hasEvent_ = false;
  Tick lastAt_ = 0;
};

}  // namespace doorbell
