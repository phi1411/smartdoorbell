from pathlib import Path
import unicodedata

import pytest

from smart_doorbell.database import Database
from smart_doorbell.repositories.event_repository import EventRepository
from smart_doorbell.repositories.person_repository import PersonRepository
from smart_doorbell.repositories.setting_repository import SettingRepository


@pytest.fixture
def database(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    return db


def test_person_samples_survive_new_connection_and_cascade_delete(database):
    people = PersonRepository(database)
    decomposed_name = unicodedata.normalize("NFD", "Nguyễn Văn A")
    person_id = people.create_with_samples(decomposed_name, [(b"one", "a.png"), (b"two", "b.png")])
    assert people.list()[0]["name"] == "Nguyễn Văn A"
    assert people.list()[0]["sample_count"] == 2
    assert people.list()[0]["created_at"].endswith("Z")
    paths = people.delete(person_id)
    assert paths == ["a.png", "b.png"]
    assert people.list() == []
    assert people.training_samples() == []


def test_event_defaults_to_unsent_and_can_be_marked(database):
    events = EventRepository(database)
    event_id = events.create({"event_type": "DOORBELL", "recognition_status": "no_face"})
    assert events.get(event_id)["telegram_sent"] == 0
    assert "T" in events.get(event_id)["created_at"]
    assert events.get(event_id)["created_at"].endswith("Z")
    events.mark_telegram_sent(event_id)
    assert events.get(event_id)["telegram_sent"] == 1


def test_settings_seed_validation_and_persistence(database):
    settings = SettingRepository(database, {"doorbell_cooldown_seconds": "5", "telegram_enabled": "true"})
    settings.seed_defaults()
    updated = settings.update({"doorbell_cooldown_seconds": 7, "telegram_enabled": False})
    assert updated["doorbell_cooldown_seconds"] == 7.0
    assert updated["telegram_enabled"] is False
    with pytest.raises(ValueError):
        settings.update({"doorbell_cooldown_seconds": -1})
    with pytest.raises(ValueError):
        settings.update({"secret_setting": "oops"})
