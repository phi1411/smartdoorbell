from __future__ import annotations

from smart_doorbell.database import Database


BOOL_KEYS = {
    "unknown_warning_enabled",
    "telegram_enabled",
    "anti_spoof_enabled",
    "save_event_images",
}

NUMERIC_RULES: dict[str, tuple[float, float, type]] = {
    "doorbell_cooldown_seconds": (0.5, 300.0, float),
    "unknown_alert_seconds": (1.0, 3600.0, float),
    "unknown_track_lost_tolerance_seconds": (0.1, 30.0, float),
    "face_match_threshold": (1.0, 200.0, float),
    "face_min_size": (30, 500, int),
    "recognition_every_n_frames": (1, 60, int),
}


class SettingRepository:
    def __init__(self, database: Database, defaults: dict[str, str]):
        self.database = database
        self.defaults = defaults

    def seed_defaults(self) -> None:
        with self.database.connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                list(self.defaults.items()),
            )

    def all(self) -> dict[str, object]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT key, value FROM settings").fetchall()
        raw = {row["key"]: row["value"] for row in rows}
        return {key: self._coerce(key, value) for key, value in raw.items()}

    def get(self, key: str, fallback: object | None = None) -> object:
        with self.database.connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return fallback
        return self._coerce(key, row["value"])

    def update(self, values: dict[str, object]) -> dict[str, object]:
        unknown = set(values) - (BOOL_KEYS | set(NUMERIC_RULES))
        if unknown:
            raise ValueError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
        normalized = {key: self._validate(key, value) for key, value in values.items()}
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany(
                    "INSERT INTO settings(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    list(normalized.items()),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.all()

    @staticmethod
    def _coerce(key: str, value: str) -> object:
        if key in BOOL_KEYS:
            return value.lower() == "true"
        rule = NUMERIC_RULES.get(key)
        if rule:
            caster = rule[2]
            return caster(float(value)) if caster is int else caster(value)
        return value

    @staticmethod
    def _validate(key: str, value: object) -> str:
        if key in BOOL_KEYS:
            if isinstance(value, bool):
                return str(value).lower()
            if str(value).lower() in {"true", "false"}:
                return str(value).lower()
            raise ValueError(f"{key} must be true or false")
        minimum, maximum, caster = NUMERIC_RULES[key]
        number = caster(value)
        if not minimum <= number <= maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}")
        return str(number)

