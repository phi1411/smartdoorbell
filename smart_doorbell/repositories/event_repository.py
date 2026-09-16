from __future__ import annotations

from smart_doorbell.database import Database
from smart_doorbell.time_utils import sqlite_utc_to_iso


class EventRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, event: dict) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO events(
                       event_type, person_id, recognized_name, recognition_status,
                       liveness_status, confidence, image_path, telegram_sent
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event["event_type"], event.get("person_id"), event.get("recognized_name"),
                    event.get("recognition_status"), event.get("liveness_status"),
                    event.get("confidence"), event.get("image_path"),
                    1 if event.get("telegram_sent") else 0,
                ),
            )
            return int(cursor.lastrowid)

    def mark_telegram_sent(self, event_id: int) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE events SET telegram_sent = 1 WHERE id = ?", (event_id,))

    def list(self, limit: int = 50, event_type: str | None = None, date: str | None = None) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if date:
            clauses.append("date(created_at, 'localtime') = date(?)")
            params.append(date)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        return [self._serialize(row) for row in rows]

    def get(self, event_id: int) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._serialize(row) if row else None

    @staticmethod
    def _serialize(row) -> dict:
        item = dict(row)
        item["created_at"] = sqlite_utc_to_iso(item.get("created_at"))
        return item
