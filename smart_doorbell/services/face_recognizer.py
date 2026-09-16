from __future__ import annotations

import logging
import threading

import cv2
import numpy as np

from smart_doorbell.repositories.person_repository import PersonRepository

logger = logging.getLogger(__name__)


class FaceRecognizer:
    """OpenCV LBPH recognizer trained from multiple persisted samples per person."""

    def __init__(self, repository: PersonRepository):
        self.repository = repository
        self._lock = threading.RLock()
        self._model = None
        self._names: dict[int, str] = {}
        self._available = hasattr(cv2, "face") and hasattr(cv2.face, "LBPHFaceRecognizer_create")
        if not self._available:
            logger.error("cv2.face is unavailable; install opencv-contrib-python")
        self.reload()

    @property
    def available(self) -> bool:
        return self._available

    @property
    def trained(self) -> bool:
        with self._lock:
            return self._model is not None

    def reload(self) -> None:
        if not self._available:
            return
        rows = self.repository.training_samples()
        images: list[np.ndarray] = []
        labels: list[int] = []
        names: dict[int, str] = {}
        for row in rows:
            data = np.frombuffer(row["embedding"], dtype=np.uint8)
            image = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
            if image is None:
                logger.warning("Skipping corrupt face sample for person %s", row["person_id"])
                continue
            images.append(image)
            labels.append(int(row["person_id"]))
            names[int(row["person_id"])] = row["name"]
        with self._lock:
            if not images:
                self._model = None
                self._names = {}
                return
            model = cv2.face.LBPHFaceRecognizer_create(radius=1, neighbors=8, grid_x=8, grid_y=8)
            model.train(images, np.asarray(labels, dtype=np.int32))
            self._model = model
            self._names = names
        logger.info("Recognition model loaded with %s samples for %s people", len(images), len(names))

    def predict(self, face: np.ndarray, threshold: float) -> dict:
        with self._lock:
            if self._model is None:
                return {"status": "unknown", "name": None, "person_id": None, "confidence": 0.0}
            person_id, distance = self._model.predict(face)
            name = self._names.get(int(person_id))
        confidence = max(0.0, min(1.0, 1.0 - (float(distance) / (threshold * 1.5))))
        if name is None or float(distance) > threshold:
            return {"status": "unknown", "name": None, "person_id": None, "confidence": confidence}
        return {
            "status": "known",
            "name": name,
            "person_id": int(person_id),
            "confidence": confidence,
        }

    @staticmethod
    def encode_sample(face: np.ndarray) -> bytes:
        ok, encoded = cv2.imencode(".png", face)
        if not ok:
            raise ValueError("Could not encode face sample")
        return encoded.tobytes()

