from __future__ import annotations

import logging
import time
import unicodedata
from pathlib import Path
from uuid import uuid4

from smart_doorbell.repositories.person_repository import PersonRepository
from smart_doorbell.repositories.setting_repository import SettingRepository
from smart_doorbell.services.face_detector import FaceDetector
from smart_doorbell.services.face_recognizer import FaceRecognizer
from smart_doorbell.services.image_service import ImageService

logger = logging.getLogger(__name__)


class EnrollmentError(ValueError):
    pass


class EnrollmentService:
    def __init__(
        self,
        camera,
        detector: FaceDetector,
        recognizer: FaceRecognizer,
        persons: PersonRepository,
        settings: SettingRepository,
        images: ImageService,
    ):
        self.camera = camera
        self.detector = detector
        self.recognizer = recognizer
        self.persons = persons
        self.settings = settings
        self.images = images

    def enroll(self, name: str, sample_count: int = 5, person_id: int | None = None) -> dict:
        clean_name = unicodedata.normalize("NFC", " ".join(str(name).strip().split()))
        if person_id is None and not clean_name:
            raise EnrollmentError("Vui lòng nhập tên")
        sample_count = max(3, min(int(sample_count), 10))
        token = f"person_{person_id or uuid4().hex[:12]}_{int(time.time())}"
        samples: list[tuple[bytes, str]] = []
        reasons = {"no_face": 0, "multiple_faces": 0, "blur": 0, "duplicate_frame": 0}
        deadline = time.monotonic() + 15
        last_sequence = -1
        last_capture_at = 0.0
        while len(samples) < sample_count and time.monotonic() < deadline:
            frame, sequence = self.camera.get_latest_frame()
            if frame is None or sequence == last_sequence:
                reasons["duplicate_frame"] += 1
                time.sleep(0.05)
                continue
            last_sequence = sequence
            if time.monotonic() - last_capture_at < 0.35:
                time.sleep(0.04)
                continue
            boxes = self.detector.detect(frame, int(self.settings.get("face_min_size", 80)))
            if len(boxes) == 0:
                reasons["no_face"] += 1
                time.sleep(0.08)
                continue
            if len(boxes) > 1:
                reasons["multiple_faces"] += 1
                time.sleep(0.08)
                continue
            face = self.detector.crop(frame, boxes[0])
            if self.detector.blur_score(face) < 45:
                reasons["blur"] += 1
                time.sleep(0.08)
                continue
            sample_path = self.images.save_face_sample(face, token, len(samples))
            samples.append((self.recognizer.encode_sample(face), sample_path))
            last_capture_at = time.monotonic()

        if len(samples) < sample_count:
            self._remove_files(samples)
            dominant = max(reasons, key=reasons.get)
            messages = {
                "no_face": "Không phát hiện đúng một khuôn mặt. Hãy nhìn thẳng vào camera.",
                "multiple_faces": "Có nhiều khuôn mặt trong khung hình.",
                "blur": "Ảnh quá mờ. Hãy giữ yên và tăng ánh sáng.",
                "duplicate_frame": "Camera không cung cấp đủ frame mới.",
            }
            raise EnrollmentError(messages[dominant])
        try:
            if person_id is None:
                person_id = self.persons.create_with_samples(clean_name, samples)
            else:
                self.persons.replace_samples(person_id, samples)
            self.recognizer.reload()
            logger.info("Person enrolled (id=%s, samples=%s)", person_id, len(samples))
            return {"person_id": person_id, "sample_count": len(samples)}
        except Exception:
            self._remove_files(samples)
            raise

    @staticmethod
    def _remove_files(samples: list[tuple[bytes, str]]) -> None:
        for _, raw_path in samples:
            path = Path(raw_path)
            try:
                path.unlink(missing_ok=True)
                path.parent.rmdir()
            except OSError:
                pass
