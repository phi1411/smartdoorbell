from __future__ import annotations

import atexit
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask

from smart_doorbell.config import AppConfig
from smart_doorbell.database import Database
from smart_doorbell.repositories.event_repository import EventRepository
from smart_doorbell.repositories.person_repository import PersonRepository
from smart_doorbell.repositories.setting_repository import SettingRepository
from smart_doorbell.routes.api import api
from smart_doorbell.routes.web import web
from smart_doorbell.services.camera_service import CameraService
from smart_doorbell.services.enrollment_service import EnrollmentService
from smart_doorbell.services.event_manager import EventManager
from smart_doorbell.services.face_detector import FaceDetector
from smart_doorbell.services.face_recognizer import FaceRecognizer
from smart_doorbell.services.image_service import ImageService
from smart_doorbell.services.liveness_service import BasicLivenessProvider
from smart_doorbell.services.recognition_service import RecognitionService, RecognitionWorker
from smart_doorbell.services.telegram_service import TelegramService
from smart_doorbell.time_utils import utc_iso_to_local_time

logger = logging.getLogger(__name__)


def create_app(config: AppConfig | None = None, *, start_services: bool = True) -> Flask:
    cfg = config or AppConfig.from_env()
    cfg.ensure_directories()
    app = Flask(
        __name__,
        template_folder=str(cfg.base_dir / "templates"),
        static_folder=str(cfg.base_dir / "static"),
    )
    app.config.update(SECRET_KEY=cfg.secret_key, JSON_SORT_KEYS=False)
    app.json.ensure_ascii = False

    database = Database(cfg.database_path)
    database.initialize()
    settings = SettingRepository(database, cfg.defaults)
    settings.seed_defaults()
    persons = PersonRepository(database)
    events = EventRepository(database)
    images = ImageService(cfg.event_image_dir, cfg.face_data_dir)
    camera = CameraService(cfg.camera_index, cfg.camera_width, cfg.camera_height)
    detector = FaceDetector()
    recognizer = FaceRecognizer(persons)
    liveness = BasicLivenessProvider()
    recognition = RecognitionService(detector, recognizer, liveness, settings)
    event_manager = EventManager(events, settings, images)
    enrollment = EnrollmentService(camera, detector, recognizer, persons, settings, images)

    services: dict = {
        "config": cfg,
        "database": database,
        "settings": settings,
        "persons": persons,
        "events": events,
        "images": images,
        "camera": camera,
        "detector": detector,
        "recognizer": recognizer,
        "liveness": liveness,
        "recognition": recognition,
        "event_manager": event_manager,
        "enrollment": enrollment,
    }

    def telegram_status() -> str:
        return (
            "🟢 Smart Doorbell Online\n"
            f"📷 Camera: {'Connected' if camera.connected else 'Offline'}\n"
            f"🤖 Recognition: {'Running' if recognizer.available else 'Unavailable'}\n"
            f"🛡 Anti-Spoof: {'Enabled' if settings.get('anti_spoof_enabled', True) else 'Disabled'}\n"
            f"👤 Registered Faces: {persons.count()}"
        )

    def telegram_photo() -> tuple[bytes | None, str]:
        frame, _ = camera.get_latest_frame()
        if frame is None:
            return None, "📷 Camera hiện không khả dụng."
        return images.jpeg_bytes(recognition.annotate(frame)), f"📷 Ảnh cửa ra vào lúc {datetime.now():%H:%M:%S}"

    def telegram_history() -> str | dict:
        recent = events.list(limit=5)
        if not recent:
            return "Chưa có sự kiện nào."
        labels = {
            "DOORBELL": "Doorbell",
            "UNKNOWN_LOITERING": "Người chưa xác định lưu lại thời gian dài",
            "SPOOF_ATTEMPT": "Spoof Attempt",
        }
        history_items: list[tuple[bytes | None, str]] = []
        event_root = cfg.event_image_dir.resolve()
        for index, item in enumerate(recent, 1):
            timestamp = utc_iso_to_local_time(item.get("created_at"))
            label = labels.get(item["event_type"], item["event_type"])
            identity = item.get("recognized_name") or label
            liveness = item.get("liveness_status") or "Không áp dụng"
            caption = (
                f"🕘 SỰ KIỆN {index}/5\n"
                f"📌 Loại: {label}\n"
                f"👤 Định danh: {identity}\n"
                f"🛡 Liveness: {liveness}\n"
                f"🕒 Thời gian: {timestamp}"
            )
            image_data = None
            raw_path = item.get("image_path")
            if raw_path:
                try:
                    path = Path(raw_path).resolve()
                    if path.is_relative_to(event_root) and path.is_file():
                        image_data = path.read_bytes()
                except OSError:
                    logger.warning("Could not read history image for event %s", item.get("id"))
            history_items.append((image_data, caption))
        return {
            "items": history_items,
        }

    telegram = TelegramService(
        cfg.telegram_bot_token,
        cfg.telegram_allowed_chat_id,
        settings,
        telegram_status,
        telegram_photo,
        telegram_history,
    )
    event_manager.set_telegram(telegram)
    worker = RecognitionWorker(camera, recognition, event_manager, settings)
    services.update({"telegram": telegram, "worker": worker})
    app.extensions["doorbell"] = services

    app.register_blueprint(web)
    app.register_blueprint(api)

    @app.errorhandler(sqlite3.DatabaseError)
    def handle_database_error(exc):
        logger.exception("Database request failed")
        return {"error": "Database is temporarily unavailable"}, 503

    if start_services:
        camera.start()
        telegram.start()
        worker.start()
        logger.info("Smart Doorbell services started")

        def stop_services() -> None:
            worker.stop()
            telegram.stop()
            camera.stop()

        atexit.register(stop_services)
        app.extensions["stop_doorbell_services"] = stop_services
    return app
