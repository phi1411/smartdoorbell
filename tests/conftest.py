from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds: float):
        self.value += seconds


class FakeSettings:
    def __init__(self, **overrides):
        self.values = {
            "doorbell_cooldown_seconds": 5.0,
            "unknown_alert_seconds": 30.0,
            "unknown_track_lost_tolerance_seconds": 2.0,
            "unknown_warning_enabled": True,
            "save_event_images": True,
        } | overrides

    def get(self, key, fallback=None):
        return self.values.get(key, fallback)


class FakeEvents:
    def __init__(self):
        self.rows = []
        self.sent = []

    def create(self, event):
        row = dict(event, id=len(self.rows) + 1)
        self.rows.append(row)
        return row["id"]

    def mark_telegram_sent(self, event_id):
        self.sent.append(event_id)


class FakeImages:
    def save_event(self, frame, event_type, enabled):
        return f"/{event_type}.jpg" if enabled else None

    def jpeg_bytes(self, frame):
        return b"jpeg"


class FakeTelegram:
    def __init__(self, succeeds=True):
        self.calls = []
        self.succeeds = succeeds

    def send_photo(self, image, caption, on_success=None):
        self.calls.append((image, caption))
        if self.succeeds and on_success:
            on_success()
        return self.succeeds


@pytest.fixture
def frame():
    return np.zeros((100, 100, 3), dtype=np.uint8)

