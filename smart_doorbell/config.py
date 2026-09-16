from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _as_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class AppConfig:
    base_dir: Path
    secret_key: str
    host: str
    port: int
    camera_index: int
    camera_width: int
    camera_height: int
    telegram_bot_token: str
    telegram_allowed_chat_id: str
    database_path: Path
    event_image_dir: Path
    face_data_dir: Path
    defaults: dict[str, str]

    @classmethod
    def from_env(cls, base_dir: Path | None = None) -> "AppConfig":
        root = (base_dir or Path(__file__).resolve().parents[1]).resolve()
        load_dotenv(root / ".env")

        def path_value(name: str, default: str) -> Path:
            raw = Path(os.getenv(name, default))
            return raw.resolve() if raw.is_absolute() else (root / raw).resolve()

        defaults = {
            "doorbell_cooldown_seconds": os.getenv("DOORBELL_COOLDOWN_SECONDS", "5"),
            "unknown_alert_seconds": os.getenv("UNKNOWN_ALERT_SECONDS", "30"),
            "unknown_track_lost_tolerance_seconds": os.getenv(
                "UNKNOWN_TRACK_LOST_TOLERANCE_SECONDS", "2"
            ),
            "unknown_warning_enabled": str(
                _as_bool(os.getenv("UNKNOWN_WARNING_ENABLED"), True)
            ).lower(),
            "telegram_enabled": str(_as_bool(os.getenv("TELEGRAM_ENABLED"), True)).lower(),
            "anti_spoof_enabled": str(_as_bool(os.getenv("ANTI_SPOOF_ENABLED"), True)).lower(),
            "save_event_images": str(_as_bool(os.getenv("SAVE_EVENT_IMAGES"), True)).lower(),
            # LBPH distance: lower is a closer match. Typical useful range is 35-90.
            "face_match_threshold": os.getenv("FACE_MATCH_THRESHOLD", "65"),
            "face_min_size": os.getenv("FACE_MIN_SIZE", "80"),
            "recognition_every_n_frames": os.getenv("RECOGNITION_EVERY_N_FRAMES", "3"),
        }
        return cls(
            base_dir=root,
            secret_key=os.getenv("SECRET_KEY", "dev-only-change-me"),
            host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "5000")),
            camera_index=int(os.getenv("CAMERA_INDEX", "0")),
            camera_width=int(os.getenv("CAMERA_WIDTH", "640")),
            camera_height=int(os.getenv("CAMERA_HEIGHT", "480")),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_allowed_chat_id=os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip(),
            database_path=path_value("DATABASE_PATH", "data/smart_doorbell.db"),
            event_image_dir=path_value("EVENT_IMAGE_DIR", "data/events"),
            face_data_dir=path_value("FACE_DATA_DIR", "data/faces"),
            defaults=defaults,
        )

    def ensure_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.event_image_dir.mkdir(parents=True, exist_ok=True)
        self.face_data_dir.mkdir(parents=True, exist_ok=True)

