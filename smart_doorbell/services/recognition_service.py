from __future__ import annotations

import copy
import logging
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from smart_doorbell.repositories.setting_repository import SettingRepository
from smart_doorbell.services.face_detector import FaceDetector
from smart_doorbell.services.face_recognizer import FaceRecognizer
from smart_doorbell.services.liveness_service import LivenessProvider

logger = logging.getLogger(__name__)


EMPTY_RESULT = {
    "status": "no_face",
    "name": None,
    "person_id": None,
    "confidence": 0.0,
    "bbox": None,
    "faces": [],
    "face_count": 0,
    "unknown_count": 0,
    "known_count": 0,
    "fake_count": 0,
    "liveness_status": "not_applicable",
    "liveness_score": None,
    "updated_at": None,
}


class RecognitionService:
    def __init__(
        self,
        detector: FaceDetector,
        recognizer: FaceRecognizer,
        liveness: LivenessProvider,
        settings: SettingRepository,
    ):
        self.detector = detector
        self.recognizer = recognizer
        self.liveness = liveness
        self.settings = settings
        self._current = dict(EMPTY_RESULT)
        self._lock = threading.RLock()
        self._analysis_lock = threading.Lock()
        self._overlay_font = self._load_overlay_font()
        self._face_tracks: dict[int, dict] = {}
        self._next_track_id = 1
        self._track_timeout_seconds = 2.0

    @staticmethod
    def _load_overlay_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        candidates = (
            Path("C:/Windows/Fonts/segoeuib.ttf"),
            Path("C:/Windows/Fonts/segoeui.ttf"),
            Path("C:/Windows/Fonts/arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        )
        for path in candidates:
            if path.is_file():
                try:
                    return ImageFont.truetype(str(path), 19)
                except OSError:
                    continue
        logger.warning("No Unicode overlay font found; using Pillow default font")
        return ImageFont.load_default()

    def _draw_unicode_labels(
        self, frame: np.ndarray, labels: list[tuple[str, int, int]]
    ) -> None:
        """Draw names with Vietnamese accents; cv2.putText only supports ASCII."""
        if not labels:
            return
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)
        draw = ImageDraw.Draw(image)
        for label, x, y in labels:
            draw.text((x, y), label, font=self._overlay_font, fill=(20, 20, 20))
        frame[:] = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)

    @staticmethod
    def _box_center(box: tuple[int, int, int, int]) -> tuple[float, float]:
        x, y, width, height = box
        return x + width / 2, y + height / 2

    def _new_liveness_provider(self):
        factory = getattr(self.liveness, "new_session", None)
        return factory() if callable(factory) else self.liveness

    def _reset_liveness_tracks(self) -> None:
        providers = {id(track["provider"]): track["provider"] for track in self._face_tracks.values()}
        providers[id(self.liveness)] = self.liveness
        for provider in providers.values():
            provider.reset()
        self._face_tracks.clear()

    def _expire_liveness_tracks(self, now: float) -> None:
        expired = [
            track_id for track_id, track in self._face_tracks.items()
            if now - track["last_seen"] > self._track_timeout_seconds
        ]
        for track_id in expired:
            self._face_tracks.pop(track_id)["provider"].reset()

    def _providers_for_boxes(
        self, boxes: list[tuple[int, int, int, int]], now: float
    ) -> list[LivenessProvider]:
        self._expire_liveness_tracks(now)

        assignments: list[LivenessProvider] = []
        used_tracks: set[int] = set()
        for box in boxes:
            center = self._box_center(box)
            width, height = box[2], box[3]
            best_id = None
            best_distance = float("inf")
            for track_id, track in self._face_tracks.items():
                if track_id in used_tracks:
                    continue
                distance = ((center[0] - track["center"][0]) ** 2 + (center[1] - track["center"][1]) ** 2) ** 0.5
                maximum = max(width, height, track["size"][0], track["size"][1]) * 2.0
                if distance <= maximum and distance < best_distance:
                    best_id, best_distance = track_id, distance
            if best_id is None:
                best_id = self._next_track_id
                self._next_track_id += 1
                self._face_tracks[best_id] = {
                    "provider": self._new_liveness_provider(),
                    "center": center,
                    "size": (width, height),
                    "last_seen": now,
                }
            else:
                self._face_tracks[best_id].update(
                    center=center, size=(width, height), last_seen=now
                )
            used_tracks.add(best_id)
            assignments.append(self._face_tracks[best_id]["provider"])
        return assignments

    @staticmethod
    def _aggregate_liveness(faces: list[dict]) -> str:
        statuses = [face.get("liveness_status") for face in faces]
        if "fake" in statuses:
            return "fake"
        if statuses and all(status == "disabled" for status in statuses):
            return "disabled"
        if statuses and all(status == "real" for status in statuses):
            return "real"
        return "pending"

    def current(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._current)

    def analyze(self, frame: np.ndarray) -> dict:
        with self._analysis_lock:
            return self._analyze_locked(frame)

    def _analyze_locked(self, frame: np.ndarray) -> dict:
        minimum = int(self.settings.get("face_min_size", 80))
        boxes = sorted(
            self.detector.detect(frame, min_size=minimum),
            key=lambda box: (box[0], box[1]),
        )
        now = time.time()
        if not boxes:
            result = {**EMPTY_RESULT, "updated_at": now}
            # Brief detection gaps are common on phone screens. Preserve the
            # challenge for the normal lost-track tolerance instead of letting a
            # spoof evade FAKE by flickering in and out of detection.
            self._expire_liveness_tracks(now)
        else:
            anti_spoof_enabled = bool(self.settings.get("anti_spoof_enabled", True))
            providers = self._providers_for_boxes(boxes, now)
            threshold = float(self.settings.get("face_match_threshold", 65.0))
            faces = []
            for box, provider in zip(boxes, providers):
                face_image = self.detector.crop(frame, box)
                live = provider.evaluate(face_image, box) if anti_spoof_enabled else {
                    "status": "disabled", "score": None, "signals": {}
                }
                prediction = self.recognizer.predict(face_image, threshold)
                if live["status"] == "fake":
                    prediction = {
                        "status": "unknown", "name": None,
                        "person_id": None, "confidence": 0.0,
                    }
                x, y, width, height = box
                faces.append({
                    **prediction,
                    "bbox": [int(x), int(y), int(x + width), int(y + height)],
                    "liveness_status": live["status"],
                    "liveness_score": live["score"],
                    "liveness_signals": live.get("signals", {}),
                })

            known_faces = [face for face in faces if face["status"] == "known"]
            unknown_faces = [
                face for face in faces
                if face["status"] == "unknown" and face["liveness_status"] != "fake"
            ]
            fake_faces = [face for face in faces if face["liveness_status"] == "fake"]
            if len(faces) == 1:
                result = {
                    **faces[0],
                    "faces": faces,
                    "face_count": 1,
                    "known_count": len(known_faces),
                    "unknown_count": len(unknown_faces),
                    "fake_count": len(fake_faces),
                    "updated_at": now,
                }
            else:
                known_names = list(dict.fromkeys(
                    face["name"] for face in known_faces if face.get("name")
                ))
                confidences = [face["confidence"] for face in known_faces]
                scores = [
                    face["liveness_score"] for face in faces
                    if face.get("liveness_score") is not None
                ]
                result = {
                    **EMPTY_RESULT,
                    "status": "multiple_faces",
                    "name": ", ".join(known_names) or None,
                    "confidence": sum(confidences) / len(confidences) if confidences else 0.0,
                    "bbox": [face["bbox"] for face in faces],
                    "faces": faces,
                    "face_count": len(faces),
                    "known_count": len(known_faces),
                    "unknown_count": len(unknown_faces),
                    "fake_count": len(fake_faces),
                    "liveness_status": self._aggregate_liveness(faces),
                    "liveness_score": sum(scores) / len(scores) if scores else None,
                    "updated_at": now,
                }
        with self._lock:
            self._current = result
        return result

    def annotate(self, frame: np.ndarray) -> np.ndarray:
        result = self.current()
        faces = result.get("faces") or []
        if not faces and result.get("bbox"):
            faces = [result]
        if not faces:
            return frame
        unicode_labels: list[tuple[str, int, int]] = []
        for face in faces:
            x1, y1, x2, y2 = face["bbox"]
            if face.get("liveness_status") == "fake":
                color, label = (0, 0, 255), "FAKE / SPOOF"
            elif face["status"] == "known":
                color, label = (70, 200, 70), face.get("name") or "KNOWN"
            else:
                color, label = (0, 200, 255), "UNKNOWN"
            text_box = self._overlay_font.getbbox(label)
            label_width = max(1, text_box[2] - text_box[0]) + 12
            label_right = min(frame.shape[1] - 1, max(x2, x1 + label_width))
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            label_top = max(0, y1 - 30)
            cv2.rectangle(frame, (x1, label_top), (label_right, y1), color, -1)
            unicode_labels.append((label, x1 + 5, max(0, y1 - 26)))
        self._draw_unicode_labels(frame, unicode_labels)
        return frame


class RecognitionWorker:
    def __init__(self, camera, recognition: RecognitionService, event_manager, settings: SettingRepository):
        self.camera = camera
        self.recognition = recognition
        self.event_manager = event_manager
        self.settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="recognition-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _run(self) -> None:
        last_sequence = -1
        while not self._stop.is_set():
            frame, sequence = self.camera.get_latest_frame()
            every_n = int(self.settings.get("recognition_every_n_frames", 3))
            if frame is None or sequence == last_sequence or sequence % every_n:
                self._stop.wait(0.025)
                continue
            last_sequence = sequence
            try:
                result = self.recognition.analyze(frame)
                self.event_manager.observe(result, frame)
            except Exception:
                logger.exception("Recognition frame processing failed")
            self._stop.wait(0.01)
