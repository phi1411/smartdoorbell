from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np


class ImageService:
    def __init__(self, event_dir: Path, face_dir: Path):
        self.event_dir = event_dir
        self.face_dir = face_dir

    @staticmethod
    def jpeg_bytes(frame: np.ndarray) -> bytes:
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            raise ValueError("Could not encode image")
        return encoded.tobytes()

    def save_event(self, frame: np.ndarray, event_type: str, enabled: bool) -> str | None:
        if not enabled:
            return None
        day_dir = self.event_dir / datetime.now().strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{datetime.now():%H%M%S}_{event_type.lower()}_{uuid4().hex[:8]}.jpg"
        if not cv2.imwrite(str(path), frame):
            raise OSError(f"Could not save event image to {path}")
        return str(path.resolve())

    def save_face_sample(self, face: np.ndarray, person_token: str, index: int) -> str:
        person_dir = self.face_dir / person_token
        person_dir.mkdir(parents=True, exist_ok=True)
        path = person_dir / f"sample_{index + 1}.png"
        if not cv2.imwrite(str(path), face):
            raise OSError(f"Could not save face sample to {path}")
        return str(path.resolve())

