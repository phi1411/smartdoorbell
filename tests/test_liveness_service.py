import numpy as np

from conftest import FakeClock
from smart_doorbell.services.liveness_service import BasicLivenessProvider


class SequenceEyeCascade:
    def __init__(self, counts):
        self.counts = iter(counts)

    def detectMultiScale(self, *args, **kwargs):
        count = next(self.counts)
        return [object()] * count


def test_moving_phone_photo_never_counts_as_real():
    clock = FakeClock()
    provider = BasicLivenessProvider(clock=clock, grace_seconds=4.0)
    provider._eye_cascade = SequenceEyeCascade([2, 2, 2, 2, 2])
    face = np.zeros((160, 160), dtype=np.uint8)
    statuses = []

    for index in range(5):
        statuses.append(provider.evaluate(face, (index * 12, 10, 100, 100))["status"])
        clock.advance(1.0)

    assert "real" not in statuses
    assert statuses[-1] == "fake"


def test_deliberate_open_closed_open_blink_counts_as_real():
    clock = FakeClock()
    provider = BasicLivenessProvider(clock=clock, grace_seconds=4.0)
    provider._eye_cascade = SequenceEyeCascade([2, 2, 0, 0, 2, 2])
    open_face = np.full((160, 160), 30, dtype=np.uint8)
    open_face[29:89] = 100
    closed_face = open_face.copy()
    closed_face[29:89] = 15
    frames = [open_face, open_face, closed_face, closed_face, open_face, open_face]
    result = None

    for face in frames:
        result = provider.evaluate(face, (10, 10, 100, 100))
        clock.advance(0.2)

    assert result["status"] == "real"
    assert result["signals"]["blink_verified"] is True


def test_eye_detector_flicker_on_same_photo_is_not_a_blink():
    clock = FakeClock()
    provider = BasicLivenessProvider(clock=clock, grace_seconds=4.0)
    provider._eye_cascade = SequenceEyeCascade([2, 2, 0, 0, 2, 2, 2])
    photo = np.full((160, 160), 60, dtype=np.uint8)
    statuses = []

    for _ in range(7):
        statuses.append(provider.evaluate(photo, (10, 10, 100, 100))["status"])
        clock.advance(0.8)

    assert "real" not in statuses
    assert statuses[-1] == "fake"
