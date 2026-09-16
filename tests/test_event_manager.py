from smart_doorbell.services.event_manager import EventManager

from conftest import FakeClock, FakeEvents, FakeImages, FakeSettings, FakeTelegram


UNKNOWN = {
    "status": "unknown", "name": None, "person_id": None, "confidence": 0.0,
    "liveness_status": "real",
}
KNOWN = {
    "status": "known", "name": "An", "person_id": 1, "confidence": 0.8,
    "liveness_status": "real",
}
NO_FACE = {
    "status": "no_face", "name": None, "person_id": None, "confidence": 0.0,
    "liveness_status": "not_applicable",
}
FAKE = {**UNKNOWN, "liveness_status": "fake"}
MULTI = {
    "status": "multiple_faces",
    "name": "An",
    "person_id": None,
    "confidence": 0.8,
    "liveness_status": "real",
    "face_count": 2,
    "known_count": 1,
    "unknown_count": 1,
    "fake_count": 0,
    "faces": [KNOWN, UNKNOWN],
}
MULTI_KNOWN = {
    **MULTI,
    "name": "An, Bình",
    "known_count": 2,
    "unknown_count": 0,
    "faces": [KNOWN, {**KNOWN, "name": "Bình", "person_id": 2}],
}


def manager(**settings):
    clock = FakeClock()
    events = FakeEvents()
    telegram = FakeTelegram()
    instance = EventManager(events, FakeSettings(**settings), FakeImages(), telegram, clock)
    return instance, clock, events, telegram


def test_doorbell_cooldown_and_retry(frame):
    subject, clock, events, telegram = manager()
    first = subject.doorbell(frame, KNOWN)
    clock.advance(1)
    second = subject.doorbell(frame, KNOWN)
    clock.advance(4.01)
    third = subject.doorbell(frame, KNOWN)
    assert first["accepted"] is True
    assert first["cooldown_seconds"] == 5.0
    assert second["accepted"] is False and second["reason"] == "cooldown"
    assert third["accepted"] is True
    assert len(events.rows) == 2
    assert len(telegram.calls) == 2
    assert events.sent == [1, 2]


def test_no_face_doorbell_still_sends_full_frame(frame):
    subject, _, events, telegram = manager()
    result = subject.doorbell(frame, NO_FACE)
    assert result["accepted"] is True
    assert events.rows[0]["recognition_status"] == "no_face"
    assert "Không phát hiện rõ" in telegram.calls[0][1]


def test_multiple_people_doorbell_lists_known_and_unknown(frame):
    subject, _, events, telegram = manager()
    result = subject.doorbell(frame, MULTI)
    caption = telegram.calls[0][1]
    assert result["accepted"] is True
    assert events.rows[0]["recognized_name"] == "An, 1 người chưa xác định"
    assert events.rows[0]["person_id"] is None
    assert "Phát hiện 2 người" in caption
    assert "Đã nhận diện: An" in caption
    assert "Chưa xác định: 1 người" in caption


def test_unknown_in_group_triggers_alert_but_all_known_group_does_not(frame):
    subject, clock, events, telegram = manager()
    subject.observe(MULTI, frame)
    clock.advance(30.1)
    subject.observe(MULTI, frame)
    assert [row["event_type"] for row in events.rows] == ["UNKNOWN_LOITERING"]
    assert len(telegram.calls) == 1

    known_subject, known_clock, known_events, _ = manager()
    known_subject.observe(MULTI_KNOWN, frame)
    known_clock.advance(60)
    known_subject.observe(MULTI_KNOWN, frame)
    assert known_events.rows == []


def test_unknown_under_threshold_does_not_alert(frame):
    subject, clock, events, _ = manager()
    subject.observe(UNKNOWN, frame)
    clock.advance(29.9)
    subject.observe(UNKNOWN, frame)
    assert events.rows == []
    assert subject.unknown_state()["state"] == "TRACKING_UNKNOWN"


def test_unknown_alerts_once_and_does_not_spam(frame):
    subject, clock, events, telegram = manager()
    subject.observe(UNKNOWN, frame)
    clock.advance(30.1)
    subject.observe(UNKNOWN, frame)
    clock.advance(60)
    subject.observe(UNKNOWN, frame)
    assert [row["event_type"] for row in events.rows] == ["UNKNOWN_LOITERING"]
    assert len(telegram.calls) == 1
    assert "NGƯỜI CHƯA XÁC ĐỊNH LƯU LẠI THỜI GIAN DÀI" in telegram.calls[0][1]
    assert "quá 30 giây" not in telegram.calls[0][1]
    assert subject.unknown_state()["state"] == "ALERTED"


def test_unknown_warning_toggle_disables_tracking_and_notification(frame):
    subject, clock, events, telegram = manager(unknown_warning_enabled=False)
    subject.observe(UNKNOWN, frame)
    clock.advance(60)
    subject.observe(UNKNOWN, frame)
    assert events.rows == []
    assert telegram.calls == []
    assert subject.unknown_state() == {
        "enabled": False,
        "state": "DISABLED",
        "elapsed_seconds": 0.0,
        "alerted": False,
    }


def test_short_track_loss_does_not_reset_but_long_loss_does(frame):
    subject, clock, events, _ = manager()
    subject.observe(UNKNOWN, frame)
    clock.advance(20)
    subject.observe(UNKNOWN, frame)
    clock.advance(1.9)
    subject.observe(NO_FACE, frame)
    clock.advance(8.2)
    subject.observe(UNKNOWN, frame)
    assert len(events.rows) == 1
    clock.advance(2.1)
    subject.observe(NO_FACE, frame)
    assert subject.unknown_state()["state"] == "IDLE"


def test_known_never_starts_unknown_timer(frame):
    subject, clock, events, _ = manager()
    subject.observe(KNOWN, frame)
    clock.advance(60)
    subject.observe(KNOWN, frame)
    assert events.rows == []
    assert subject.unknown_state()["state"] == "IDLE"


def test_spoof_only_sends_when_doorbell_captures(frame):
    subject, clock, events, telegram = manager()
    subject.observe(FAKE, frame)
    clock.advance(10)
    subject.observe(FAKE, frame)
    assert events.rows == []
    assert telegram.calls == []

    result = subject.doorbell(frame, FAKE)
    assert result["accepted"] is True
    assert result["event_type"] == "SPOOF_ATTEMPT"
    assert [row["event_type"] for row in events.rows] == ["SPOOF_ATTEMPT"]
    assert len(telegram.calls) == 1
    assert "Có dấu hiệu giả mạo" in telegram.calls[0][1]
