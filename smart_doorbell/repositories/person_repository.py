from __future__ import annotations

from datetime import datetime, timezone
import unicodedata

from smart_doorbell.database import Database
from smart_doorbell.time_utils import sqlite_utc_to_iso


class PersonRepository:
    def __init__(self, database: Database):
        self.database = database

    def list(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT p.id, p.name, p.created_at, p.updated_at,
                          COUNT(e.id) AS sample_count,
                          MIN(e.sample_image_path) AS thumbnail_path
                   FROM persons p
                   LEFT JOIN face_embeddings e ON e.person_id = p.id
                   GROUP BY p.id ORDER BY p.name COLLATE NOCASE"""
            ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            item["created_at"] = sqlite_utc_to_iso(item.get("created_at"))
            item["updated_at"] = sqlite_utc_to_iso(item.get("updated_at"))
        return items

    def count(self) -> int:
        with self.database.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM persons").fetchone()
        return int(row["count"])

    def create_with_samples(self, name: str, samples: list[tuple[bytes, str | None]]) -> int:
        clean_name = unicodedata.normalize("NFC", " ".join(name.strip().split()))
        if not clean_name:
            raise ValueError("Name is required")
        if not samples:
            raise ValueError("At least one face sample is required")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute("INSERT INTO persons(name) VALUES (?)", (clean_name,))
                person_id = int(cursor.lastrowid)
                connection.executemany(
                    "INSERT INTO face_embeddings(person_id, embedding, sample_image_path) VALUES (?, ?, ?)",
                    [(person_id, sample, path) for sample, path in samples],
                )
                connection.commit()
                return person_id
            except Exception:
                connection.rollback()
                raise

    def replace_samples(self, person_id: int, samples: list[tuple[bytes, str | None]]) -> None:
        if not samples:
            raise ValueError("At least one face sample is required")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                exists = connection.execute("SELECT id FROM persons WHERE id = ?", (person_id,)).fetchone()
                if not exists:
                    raise LookupError("Person not found")
                connection.execute("DELETE FROM face_embeddings WHERE person_id = ?", (person_id,))
                connection.executemany(
                    "INSERT INTO face_embeddings(person_id, embedding, sample_image_path) VALUES (?, ?, ?)",
                    [(person_id, sample, path) for sample, path in samples],
                )
                connection.execute(
                    "UPDATE persons SET updated_at = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), person_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def delete(self, person_id: int) -> list[str]:
        with self.database.connect() as connection:
            paths = [
                row["sample_image_path"]
                for row in connection.execute(
                    "SELECT sample_image_path FROM face_embeddings WHERE person_id = ?", (person_id,)
                ).fetchall()
                if row["sample_image_path"]
            ]
            cursor = connection.execute("DELETE FROM persons WHERE id = ?", (person_id,))
            if cursor.rowcount == 0:
                raise LookupError("Person not found")
        return paths

    def training_samples(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT e.person_id, p.name, e.embedding
                   FROM face_embeddings e JOIN persons p ON p.id = e.person_id
                   ORDER BY e.id"""
            ).fetchall()
        return [dict(row) for row in rows]
