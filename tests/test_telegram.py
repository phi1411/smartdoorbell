from smart_doorbell.services.telegram_service import TelegramService

from conftest import FakeSettings


def make_service():
    return TelegramService(
        "token", "123", FakeSettings(telegram_enabled=True),
        lambda: "status", lambda: (b"image", "photo"), lambda: "history",
    )


def test_commands_only_accept_allowed_chat(monkeypatch):
    service = make_service()
    messages = []
    photos = []
    monkeypatch.setattr(service, "send_message", lambda text: messages.append(text) or True)
    monkeypatch.setattr(service, "send_photo", lambda image, caption: photos.append((image, caption)) or True)
    service._handle_update({"message": {"chat": {"id": 999}, "text": "/status"}})
    assert messages == []
    service._handle_update({"message": {"chat": {"id": 123}, "text": "/status"}})
    service._handle_update({"message": {"chat": {"id": 123}, "text": "/photo"}})
    service._handle_update({"message": {"chat": {"id": 123}, "text": "/history"}})
    assert messages == ["status", "history"]
    assert photos == [(b"image", "photo")]


def test_history_command_sends_available_photos(monkeypatch):
    service = TelegramService(
        "token", "123", FakeSettings(telegram_enabled=True),
        lambda: "status",
        lambda: (b"image", "photo"),
        lambda: {
            "items": [(b"one", "Sự kiện 1"), (b"two", "Sự kiện 2")],
        },
    )
    calls = []
    monkeypatch.setattr(
        service, "send_history",
        lambda items: calls.append(items) or True,
    )

    service._handle_update({"message": {"chat": {"id": 123}, "text": "/history"}})

    assert calls == [[(b"one", "Sự kiện 1"), (b"two", "Sự kiện 2")]]


def test_history_is_queued_as_separate_items_in_order():
    service = make_service()
    assert service.send_history(
        [(b"one", "Một"), (None, "Hai"), (b"three", "Ba")]
    ) is True
    first = service._outbox.get_nowait()
    second = service._outbox.get_nowait()
    third = service._outbox.get_nowait()
    assert first[:3] == ("photo", b"one", "Một")
    assert second[0] == "message"
    assert second[2] == "Hai\n📷 Ảnh không còn được lưu"
    assert third[:3] == ("photo", b"three", "Ba")
