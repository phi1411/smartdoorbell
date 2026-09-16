import numpy as np

from smart_doorbell.services.recognition_service import RecognitionService


class FakeDetector:
    def detect(self, frame, min_size=80):
        # Deliberately right-to-left; the service must return stable left-to-right results.
        return [(180, 20, 90, 90), (20, 30, 90, 90)]

    def crop(self, frame, box):
        return np.full((160, 160), box[0], dtype=np.uint8)


class IntermittentDetector(FakeDetector):
    def __init__(self):
        self.calls = 0

    def detect(self, frame, min_size=80):
        self.calls += 1
        return [(20, 30, 90, 90)] if self.calls != 2 else []


class FakeRecognizer:
    available = True
    trained = True

    def predict(self, face, threshold):
        if int(face[0, 0]) < 100:
            return {
                "status": "known", "name": "Nguyễn Văn An",
                "person_id": 1, "confidence": 0.9,
            }
        return {
            "status": "unknown", "name": None,
            "person_id": None, "confidence": 0.2,
        }


class FakeLiveness:
    def new_session(self):
        return FakeLiveness()

    def evaluate(self, face, bbox):
        return {"status": "real", "score": 0.8, "signals": {}}

    def reset(self):
        pass


class FakeSettings:
    values = {
        "face_min_size": 80,
        "face_match_threshold": 65.0,
        "anti_spoof_enabled": True,
    }

    def get(self, key, fallback=None):
        return self.values.get(key, fallback)


def test_multiple_faces_are_recognized_individually_and_annotated():
    service = RecognitionService(
        FakeDetector(), FakeRecognizer(), FakeLiveness(), FakeSettings()
    )
    frame = np.zeros((240, 320, 3), dtype=np.uint8)

    result = service.analyze(frame)

    assert result["status"] == "multiple_faces"
    assert result["face_count"] == 2
    assert result["known_count"] == 1
    assert result["unknown_count"] == 1
    assert result["fake_count"] == 0
    assert result["name"] == "Nguyễn Văn An"
    assert result["faces"][0]["name"] == "Nguyễn Văn An"
    assert result["faces"][0]["bbox"] == [20, 30, 110, 120]
    assert result["faces"][1]["status"] == "unknown"

    annotated = service.annotate(frame.copy())
    assert annotated.shape == frame.shape
    assert np.any(annotated != frame)


def test_short_detection_gap_preserves_liveness_track():
    service = RecognitionService(
        IntermittentDetector(), FakeRecognizer(), FakeLiveness(), FakeSettings()
    )
    frame = np.zeros((240, 320, 3), dtype=np.uint8)

    service.analyze(frame)
    first_provider = next(iter(service._face_tracks.values()))["provider"]
    no_face = service.analyze(frame)

    assert no_face["status"] == "no_face"
    assert len(service._face_tracks) == 1

    service.analyze(frame)
    assert next(iter(service._face_tracks.values()))["provider"] is first_provider
