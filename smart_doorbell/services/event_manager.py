from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

import numpy as np

from smart_doorbell.repositories.event_repository import EventRepository
from smart_doorbell.repositories.setting_repository import SettingRepository
from smart_doorbell.services.image_service import ImageService

logger = logging.getLogger(__name__)


class EventManager:
    def __init__(
        self,
        events: EventRepository,
        settings: SettingRepository,
        images: ImageService,
        telegram=None,
        clock=time.monotonic,
    ):
        self.events = events
        self.settings = settings
        self.images = images
        self.telegram = telegram
        self.clock = clock
        self._lock = threading.RLock()
        self._last_doorbell_at: float | None = None
        self._unknown_started_at: float | None = None
        self._unknown_last_seen_at: float | None = None
        self._unknown_alerted = False

    def set_telegram(self, telegram) -> None:
        self.telegram = telegram

    def doorbell(self, frame: np.ndarray | None, recognition: dict) -> dict:
        now = self.clock()
        cooldown = float(self.settings.get("doorbell_cooldown_seconds", 5.0))
        with self._lock:
            remaining = 0.0 if self._last_doorbell_at is None else cooldown - (now - self._last_doorbell_at)
            if remaining > 0:
                return {"accepted": False, "reason": "cooldown", "retry_after_seconds": round(remaining, 2)}
            if frame is None:
                return {"accepted": False, "reason": "camera_unavailable", "retry_after_seconds": 0}
            self._last_doorbell_at = now
            faces = self._face_results(recognition)
            spoof_face = next(
                (face for face in faces if face.get("liveness_status") == "fake"),
                None,
            )

        # Spoof is passive on the live camera. It becomes a persisted/notified
        # event only when the user explicitly captures by pressing the doorbell.
        event_type = "SPOOF_ATTEMPT" if spoof_face is not None else "DOORBELL"
        event = self._build_event(event_type, recognition, frame)
        event_id = self.events.create(event)
        caption = self._doorbell_caption(recognition)
        queued = self._send_photo(event_id, frame, caption)
        logger.info(
            "Doorbell capture created (id=%s, type=%s, status=%s)",
            event_id, event_type, recognition.get("status"),
        )
        return {
            "accepted": True,
            "event_id": event_id,
            "event_type": event_type,
            "telegram_queued": queued,
            "cooldown_seconds": cooldown,
            "recognition": recognition,
        }

    def observe(self, recognition: dict, frame: np.ndarray) -> None:
        now = self.clock()
        faces = self._face_results(recognition)
        unknown_faces = [
            face for face in faces
            if face.get("status") == "unknown"
            and face.get("liveness_status") != "fake"
        ]
        with self._lock:
            if not bool(self.settings.get("unknown_warning_enabled", True)):
                self._reset_unknown()
                return
            if unknown_faces:
                if self._unknown_started_at is None:
                    self._unknown_started_at = now
                    logger.info("Unknown tracking started")
                self._unknown_last_seen_at = now
                threshold = float(self.settings.get("unknown_alert_seconds", 30.0))
                if not self._unknown_alerted and now - self._unknown_started_at >= threshold:
                    self._unknown_alerted = True
                    alert_recognition = dict(recognition)
                    alert_recognition["unknown_count"] = len(unknown_faces)
                    self._create_security_event(
                        "UNKNOWN_LOITERING", alert_recognition, frame
                    )
                    logger.info("Unknown loitering alert created")
            else:
                tolerance = float(self.settings.get("unknown_track_lost_tolerance_seconds", 2.0))
                if self._unknown_last_seen_at is not None and now - self._unknown_last_seen_at > tolerance:
                    self._reset_unknown()

    def unknown_state(self) -> dict:
        with self._lock:
            enabled = bool(self.settings.get("unknown_warning_enabled", True))
            if not enabled:
                return {
                    "enabled": False,
                    "state": "DISABLED",
                    "elapsed_seconds": 0.0,
                    "alerted": False,
                }
            now = self.clock()
            if self._unknown_started_at is None:
                state, elapsed = "IDLE", 0.0
            else:
                state = "ALERTED" if self._unknown_alerted else "TRACKING_UNKNOWN"
                elapsed = max(0.0, now - self._unknown_started_at)
            return {
                "enabled": True,
                "state": state,
                "elapsed_seconds": round(elapsed, 1),
                "alerted": self._unknown_alerted,
            }

    def reset_unknown_tracking(self) -> None:
        """Clear the active unknown session immediately when the warning is disabled."""
        with self._lock:
            self._reset_unknown()

    def _reset_unknown(self) -> None:
        if self._unknown_started_at is not None:
            logger.info("Unknown tracking reset")
        self._unknown_started_at = None
        self._unknown_last_seen_at = None
        self._unknown_alerted = False

    @staticmethod
    def _face_results(recognition: dict) -> list[dict]:
        faces = recognition.get("faces")
        if isinstance(faces, list) and faces:
            return faces
        if recognition.get("status") in {"known", "unknown"}:
            return [recognition]
        return []

    @classmethod
    def _recognized_summary(cls, recognition: dict) -> str | None:
        faces = cls._face_results(recognition)
        if len(faces) <= 1:
            return recognition.get("name")
        known_names = list(dict.fromkeys(
            face.get("name") for face in faces
            if face.get("status") == "known" and face.get("name")
        ))
        unknown_count = sum(
            face.get("status") == "unknown"
            and face.get("liveness_status") != "fake"
            for face in faces
        )
        fake_count = sum(face.get("liveness_status") == "fake" for face in faces)
        parts = known_names
        if unknown_count:
            parts.append(f"{unknown_count} người chưa xác định")
        if fake_count:
            parts.append(f"{fake_count} nghi giả mạo")
        return ", ".join(parts) or None

    def _build_event(self, event_type: str, recognition: dict, frame: np.ndarray) -> dict:
        image_path = None
        try:
            image_path = self.images.save_event(
                frame, event_type, bool(self.settings.get("save_event_images", True))
            )
        except Exception:
            logger.exception("Could not save event image")
        return {
            "event_type": event_type,
            "person_id": recognition.get("person_id") if len(self._face_results(recognition)) <= 1 else None,
            "recognized_name": self._recognized_summary(recognition),
            "recognition_status": recognition.get("status"),
            "liveness_status": recognition.get("liveness_status"),
            "confidence": recognition.get("confidence"),
            "image_path": image_path,
            "telegram_sent": False,
        }

    def _create_security_event(self, event_type: str, recognition: dict, frame: np.ndarray) -> int:
        event_id = self.events.create(self._build_event(event_type, recognition, frame))
        if event_type == "UNKNOWN_LOITERING":
            unknown_count = int(recognition.get("unknown_count") or 1)
            count_line = (
                f"Hiện có {unknown_count} người chưa xác định trong khung hình.\n"
                if unknown_count > 1 else ""
            )
            caption = (
                "⚠️ NGƯỜI CHƯA XÁC ĐỊNH LƯU LẠI THỜI GIAN DÀI\n"
                "Phát hiện người chưa xác định lưu lại trước cửa trong thời gian dài.\n"
                f"{count_line}"
                f"🕒 {datetime.now():%H:%M:%S}"
            )
        else:
            caption = (
                "🚨 CẢNH BÁO GIẢ MẠO KHUÔN MẶT\n"
                "Phát hiện khuôn mặt có dấu hiệu giả mạo trước camera.\n"
                f"🕒 {datetime.now():%H:%M:%S}"
            )
        self._send_photo(event_id, frame, caption)
        return event_id

    def _send_photo(self, event_id: int, frame: np.ndarray, caption: str) -> bool:
        if self.telegram is None:
            return False
        try:
            image = self.images.jpeg_bytes(frame)
            return self.telegram.send_photo(
                image, caption, on_success=lambda: self.events.mark_telegram_sent(event_id)
            )
        except Exception:
            logger.exception("Could not queue Telegram notification")
            return False

    @staticmethod
    def _doorbell_caption(recognition: dict) -> str:
        now = datetime.now().strftime("%H:%M:%S")
        faces = EventManager._face_results(recognition)
        if len(faces) > 1:
            known_names = list(dict.fromkeys(
                face.get("name") for face in faces
                if face.get("status") == "known" and face.get("name")
            ))
            unknown_count = sum(
                face.get("status") == "unknown"
                and face.get("liveness_status") != "fake"
                for face in faces
            )
            fake_count = sum(
                face.get("liveness_status") == "fake" for face in faces
            )
            lines = [f"👥 Phát hiện {len(faces)} người"]
            if known_names:
                lines.append(f"✅ Đã nhận diện: {', '.join(known_names)}")
            if unknown_count:
                lines.append(f"⚠️ Chưa xác định: {unknown_count} người")
            if fake_count:
                lines.append(f"🚨 Nghi giả mạo: {fake_count} khuôn mặt")
            identity = "\n".join(lines)
        elif recognition.get("liveness_status") == "fake":
            identity = "🚨 Có dấu hiệu giả mạo"
        elif recognition.get("status") == "known":
            identity = f"👤 {recognition.get('name')}\n✅ Người đã đăng ký"
        elif recognition.get("status") == "unknown":
            identity = "👤 Người chưa xác định\n⚠️ Unknown"
        elif recognition.get("status") == "multiple_faces":
            identity = "👥 Phát hiện nhiều khuôn mặt"
        else:
            identity = "👤 Không phát hiện rõ khuôn mặt"
        return f"🔔 CÓ NGƯỜI BẤM CHUÔNG\n{identity}\n🕒 {now}"
