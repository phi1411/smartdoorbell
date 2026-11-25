import base64
import io
import random

import pytest
import requests
from PIL import Image

from rental.app import MAX_BYTES, MAX_FACE_REQUEST_BYTES, create_app
from rental.face_profiles import FaceProfiles, VECTOR
from rental.store import DEVICE_SCHEMA, SCHEMA, VISIT_SCHEMA, RentalStore, digest
from rental.worker import VisitWorker


PASSWORD = "a long local test password"
TOKEN = "devtoken-" * 5


def jpeg(color="white"):
    output = io.BytesIO()
    Image.new("RGB", (640, 480), color).save(output, "JPEG")
    return output.getvalue()


@pytest.fixture
def rental(tmp_path):
    store = RentalStore(tmp_path / "rental.db")
    store.initialize()
    with store.write() as db:
        db.execute("INSERT INTO houses (id, name, telegram_chat_id) VALUES (1, 'Dãy A', '-100008')")
        db.execute("INSERT INTO rooms (id, house_id, code) VALUES (1, 1, 'P101')")
        db.execute("INSERT INTO devices (id, house_id, token_hash) VALUES ('doorbell-01', 1, ?)",
                   (digest(TOKEN.encode()),))
    return store, create_app(store, PASSWORD).test_client()


def post_image(client, data=None, event="1", token=TOKEN, content_type="image/jpeg"):
    return client.post("/api/device/doorbell", data=jpeg() if data is None else data, content_type=content_type,
                       headers={"X-Device-ID": "doorbell-01", "X-Event-ID": f"doorbell-01-{event * 16}",
                                "Authorization": f"Bearer {token}"})


def owner_csrf(client):
    assert client.get('/login').status_code == 200
    with client.session_transaction() as state:
        csrf = state['csrf']
    assert client.post('/login', data={'csrf': csrf, 'password': PASSWORD}).status_code == 303
    with client.session_transaction() as state:
        return state['csrf']


def test_accepts_valid_jpeg_and_never_queues_duplicate(rental):
    store, client = rental
    assert post_image(client, token="x" * 40).status_code == 401
    assert post_image(client, content_type="text/plain").status_code == 415
    assert post_image(client, data=b"\xff\xd8not a photo").status_code == 400
    assert post_image(client, data=jpeg()[:1000]).status_code == 400
    assert post_image(client, data=b"x" * (MAX_BYTES + 1)).status_code == 413
    assert post_image(client).status_code == 202
    repeated = post_image(client)
    assert repeated.status_code == 200 and repeated.json["state"] == "queued"
    assert post_image(client, data=jpeg("black")).status_code == 409
    assert post_image(client, event="2").status_code == 429
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == 1


def test_shared_entrance_works_for_house_without_rooms(rental):
    store, client = rental
    with store.connect() as db:
        db.execute("DELETE FROM rooms")
    photo = jpeg()
    assert post_image(client, data=photo).status_code == 202
    with store.connect() as db:
        assert db.execute("SELECT house_id FROM visits").fetchone()[0] == 1
    assert store.claim_visit()["name"] == "Dãy A"


def test_existing_room_events_are_migrated_to_the_samenamed_house(tmp_path):
    store = RentalStore(tmp_path / "old_rental.db")
    old_device_schema = DEVICE_SCHEMA.format(devices="devices").replace("house_id", "room_id").replace("houses(id)", "rooms(id)")
    old_visit_schema = VISIT_SCHEMA.format(devices="devices", visits="visits").replace("house_id", "room_id").replace("houses(id)", "rooms(id)")
    photo = jpeg()
    event_id = "doorbell-01-" + "1" * 16
    with store.connect() as db:
        db.executescript(SCHEMA)
        db.execute(old_device_schema)
        db.execute(old_visit_schema)
        db.execute("INSERT INTO houses VALUES (3, 'Dãy cũ', '-100008')")
        db.execute("INSERT INTO rooms VALUES (7, 3, 'P107')")
        db.execute("INSERT INTO devices VALUES ('doorbell-01', 7, ?)", (digest(TOKEN.encode()),))
        db.execute("""INSERT INTO visits (device_id, room_id, event_id, recipient_chat_id, jpeg, image_hash,
                       state, notification_status, telegram_message_id)
                      VALUES ('doorbell-01', 7, ?, '-100008', ?, ?, 'done', 'sent', 42)""",
                   (event_id, photo, digest(photo)))

    store.initialize()
    store.initialize()
    with store.connect() as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("SELECT house_id FROM devices").fetchone()[0] == 3
        assert tuple(db.execute("SELECT house_id, jpeg, notification_status, telegram_message_id FROM visits").fetchone()) == (3, photo, "sent", 42)
    assert post_image(create_app(store, PASSWORD).test_client(), data=photo).status_code == 200


def test_dashboard_requires_login_and_validates_csrf(rental):
    _, client = rental
    assert post_image(client).status_code == 202
    assert client.get("/visits/1/photo").status_code == 302
    assert client.get("/login").status_code == 200
    with client.session_transaction() as login_session:
        csrf = login_session["csrf"]
    assert client.post("/login", data={"csrf": csrf, "password": PASSWORD}).status_code == 303
    assert client.post("/manage/stay", data={"room_id": "1", "name": "Người thuê thử"}).status_code == 403
    with client.session_transaction() as admin_session:
        csrf = admin_session["csrf"]
    assert client.post("/manage/stay", data={"csrf": csrf, "room_id": "1", "name": "Người thuê thử"}).status_code == 303
    dashboard = client.get("/").get_data(as_text=True)
    assert "Người thuê thử" in dashboard and "Dãy A · Cửa chung" in dashboard
    assert "doorbell-01" not in client.get("/?house=2").get_data(as_text=True).split('id="visits"')[1]
    photo = client.get("/visits/1/photo")
    assert photo.content_type == "image/jpeg" and photo.headers["Cache-Control"] == "no-store"
    assert client.post("/manage/checkout", data={"csrf": csrf, "stay_id": "1"}).status_code == 303
    assert "Phòng trống" in client.get("/").get_data(as_text=True)


class FakeResponse:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class FakeProviders:
    def __init__(self, unsafe=False, timeout=False):
        self.unsafe = unsafe
        self.timeout = timeout
        self.analyses = []
        self.notifications = []
        self.caption_edits = []

    def get(self, url, **kwargs):
        assert url.endswith("/v1/toolsets")
        return FakeResponse({"data": [{"enabled": self.unsafe}]})

    def post(self, url, **kwargs):
        if url.endswith("/v1/chat/completions"):
            self.analyses.append(kwargs["json"])
            return FakeResponse({"choices": [{"message": {"content": "Có một kiện hàng."}}]})
        if url.endswith("/editMessageCaption"):
            self.caption_edits.append(kwargs)
            return FakeResponse({"ok": True})
        assert url.endswith("/sendPhoto")
        self.notifications.append(kwargs)
        if self.timeout:
            raise requests.Timeout()
        return FakeResponse({"ok": True, "result": {"message_id": 42}})


@pytest.mark.parametrize(("unsafe", "timeout", "relocated", "expected"), [
    (False, False, False, ("done", "done", "sent", 42)),
    (True, True, False, ("attention", "failed", "delivery_unknown", None)),
    (False, False, True, ("attention", "done", "recipient_changed", None)),
])
def test_worker_persists_hermes_and_telegram_results(rental, monkeypatch, unsafe, timeout, relocated, expected):
    store, client = rental
    assert post_image(client).status_code == 202
    if relocated:
        with store.connect() as db:
            db.execute("UPDATE houses SET telegram_chat_id='-100009'")
    monkeypatch.setenv("RENTAL_HERMES_URL", "https://hermes.test/v1")
    monkeypatch.setenv("RENTAL_HERMES_KEY", "test-api")
    monkeypatch.setenv("RENTAL_TELEGRAM_BOT_TOKEN", "test-bot")
    providers = FakeProviders(unsafe, timeout)
    worker = VisitWorker(store, providers)
    assert worker.run_one() and not worker.run_one()
    assert not providers.analyses and not providers.caption_edits
    assert worker.run_description() and not worker.run_description()
    with store.connect() as db:
        row = db.execute("SELECT state, analysis_status, notification_status, telegram_message_id, description FROM visits").fetchone()
    assert tuple(row)[:4] == expected
    if not unsafe:
        payload = providers.analyses[0]
        image_url = payload["messages"][0]["content"][1]["image_url"]["url"]
        assert payload["tool_choice"] == "none"
        with Image.open(io.BytesIO(base64.b64decode(image_url.split(",", 1)[1]))) as sent:
            assert sent.size == (640, 480)
    else:
        assert not providers.analyses
    if not relocated:
        assert len(providers.notifications) == 1
        assert providers.notifications[0]["data"]["chat_id"] == "-100008"
        caption = providers.notifications[0]["data"]["caption"]
        assert "Dãy A · Cửa chung" in caption
        assert "Hermes:" not in caption
    else:
        assert not providers.notifications
    expected_description = "Hermes: Chưa lấy được mô tả ảnh." if unsafe else "Hermes: Có một kiện hàng."
    assert expected_description in row['description']
    if expected[3] is not None:
        assert len(providers.caption_edits) == 1
        edit = providers.caption_edits[0]["data"]
        assert edit["message_id"] == 42 and edit["chat_id"] == "-100008"
        assert expected_description in edit["caption"]
    else:
        assert not providers.caption_edits


def test_interrupted_telegram_send_can_finish_description_without_sending_again(rental, monkeypatch):
    store, client = rental
    assert post_image(client).status_code == 202
    monkeypatch.setenv("RENTAL_HERMES_URL", "https://hermes.test/v1")
    monkeypatch.setenv("RENTAL_HERMES_KEY", "test-api")
    monkeypatch.setenv("RENTAL_TELEGRAM_BOT_TOKEN", "test-bot")
    job = store.claim_visit()
    assert store.record_analysis(job, "not_configured", None, True, describe=True)
    with store.write() as db:
        db.execute("UPDATE visits SET claimed_at=unixepoch() - 301")

    providers = FakeProviders(False, False)
    worker = VisitWorker(store, providers)
    assert not worker.run_one()
    assert worker.run_description() and not worker.run_description()
    with store.connect() as db:
        row = db.execute("SELECT state, analysis_status, notification_status FROM visits").fetchone()
    assert tuple(row) == ("attention", "done", "delivery_unknown")
    assert not providers.notifications and not providers.caption_edits


def test_worker_sends_jpeg_to_face_api_and_uses_result(rental, monkeypatch):
    store, client = rental
    assert post_image(client).status_code == 202
    monkeypatch.setenv("RENTAL_HERMES_URL", "")
    monkeypatch.setenv("RENTAL_HERMES_KEY", "")
    monkeypatch.setenv("RENTAL_FACE_API_URL", "http://face.test/internal/recognize")
    monkeypatch.setenv("RENTAL_FACE_API_KEY", "face-key" * 5)
    monkeypatch.setenv("RENTAL_TELEGRAM_BOT_TOKEN", "test-bot")

    class FaceProviders(FakeProviders):
        def post(self, url, **kwargs):
            if url == "http://face.test/internal/recognize":
                assert kwargs["headers"]["Authorization"] == "Bearer " + "face-key" * 5
                assert kwargs["headers"]["Content-Type"] == "image/jpeg"
                with Image.open(io.BytesIO(kwargs["data"])) as sent:
                    assert sent.size == (640, 480)
                return FakeResponse({"face_count": 2, "known_count": 1, "unknown_count": 1})
            return super().post(url, **kwargs)

    http = FaceProviders()
    assert VisitWorker(store, http).run_one()
    assert "1/2 khuôn mặt" in http.notifications[0]["data"]["caption"]
    with store.connect() as db:
        visit = db.execute("SELECT state, description FROM visits").fetchone()
        assert visit["state"] == "done"
        assert "1/2 khuôn mặt" in visit["description"]


def test_enrollment_updates_stay_and_is_removed_on_checkout(rental, monkeypatch):
    store, _ = rental
    with store.connect() as db:
        db.execute("INSERT INTO stays (room_id, name) VALUES (1, 'Phi')")
    monkeypatch.setenv("RENTAL_FACE_EMBED_URL", "http://face.test/internal/embeddings")
    monkeypatch.setenv("RENTAL_FACE_API_KEY", "a" * 40)
    responses = iter([[], [VECTOR.pack(1, *([0] * 127))]])
    monkeypatch.setattr(FaceProfiles, "extract", lambda profiles, photo: next(responses))
    client = create_app(store, PASSWORD).test_client()
    assert client.get("/stays/1/face-samples").status_code == 302
    csrf = owner_csrf(client)
    page = client.get("/stays/1/face-samples").get_data(as_text=True)
    assert "Chụp bằng camera" in page and "face_samples.js" in page
    assert 'name="photo"' in page

    def enroll():
        return client.post("/stays/1/face-samples", data={"action": "enroll", "csrf": csrf,
                           "photo": (io.BytesIO(jpeg()), "photo.jpg")}, follow_redirects=True)

    assert "cần có đúng một khuôn mặt" in enroll().get_data(as_text=True)
    assert "Đã đăng ký mẫu khuôn mặt" in enroll().get_data(as_text=True)
    assert store.house_face_samples(1)[0]["name"] == "Phi"
    assert client.post("/manage/checkout", data={"stay_id": 1, "csrf": csrf}).status_code == 303
    assert not store.house_face_samples(1)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM face_samples").fetchone()[0] == 0


def test_enrollment_fits_a_detailed_photo_into_the_recognition_request(rental, monkeypatch):
    store, _ = rental
    with store.connect() as db:
        db.execute("INSERT INTO stays (room_id, name) VALUES (1, 'Phi')")
    monkeypatch.setenv("RENTAL_FACE_EMBED_URL", "http://face.test/internal/embeddings")
    monkeypatch.setenv("RENTAL_FACE_API_KEY", "a" * 40)

    def extract(profiles, payload):
        assert len(payload) <= MAX_FACE_REQUEST_BYTES
        with Image.open(io.BytesIO(payload), formats=["JPEG"]) as photo:
            assert 32 <= photo.width <= 1600 and 32 <= photo.height <= 1200
            photo.load()
        return [VECTOR.pack(1, *([0] * 127))]

    monkeypatch.setattr(FaceProfiles, "extract", extract)
    client = create_app(store, PASSWORD).test_client()
    csrf = owner_csrf(client)
    pixels = random.Random(0).randbytes(1600 * 1200 * 3)
    upload = io.BytesIO()
    Image.frombytes("RGB", (1600, 1200), pixels).save(upload, "PNG")
    upload.seek(0)
    response = client.post("/stays/1/face-samples", data={
        "action": "enroll", "csrf": csrf, "photo": (upload, "detailed.png"),
    }, follow_redirects=True)
    assert "Đã đăng ký mẫu khuôn mặt" in response.get_data(as_text=True)


def test_deleting_a_stale_face_sample_reports_that_nothing_was_removed(rental):
    store, client = rental
    with store.connect() as db:
        db.execute("INSERT INTO stays (room_id, name) VALUES (1, 'Phi')")
    assert store.save_face_sample(1, VECTOR.pack(1, *([0] * 127)), 5) == "saved"
    request = {"action": "delete", "csrf": owner_csrf(client), "sample_id": "1"}
    first = client.post("/stays/1/face-samples", data=request, follow_redirects=True)
    assert "Đã xóa mẫu khuôn mặt." in first.get_data(as_text=True)
    repeated = client.post("/stays/1/face-samples", data=request, follow_redirects=True)
    assert "Mẫu khuôn mặt đã bị xóa hoặc không còn trong hồ sơ này." in repeated.get_data(as_text=True)


def test_worker_shows_room_of_the_matching_resident_at_the_shared_gate(rental, monkeypatch):
    store, client = rental
    vector = [1] + [0] * 127
    with store.write() as db:
        db.execute("INSERT INTO houses (id, name) VALUES (2, 'Dãy B')")
        db.execute("INSERT INTO rooms VALUES (2, 2, 'B01')")
        db.execute("INSERT INTO stays (id, room_id, name) VALUES (1, 1, 'Phi'), (2, 2, 'An')")
    for stay in (1, 2):
        assert store.save_face_sample(stay, VECTOR.pack(*vector), 5) == "saved"
    assert post_image(client).status_code == 202
    monkeypatch.setenv("RENTAL_FACE_EMBED_URL", "http://face.test/internal/embeddings")
    monkeypatch.setenv("RENTAL_FACE_API_KEY", "a" * 40)
    monkeypatch.setenv("RENTAL_TELEGRAM_BOT_TOKEN", "test-bot")

    class FaceProviders(FakeProviders):
        def post(self, url, **kwargs):
            if url.endswith("/internal/embeddings"):
                return FakeResponse({"vectors": [vector]})
            return super().post(url, **kwargs)

    http = FaceProviders()
    assert VisitWorker(store, http).run_one()
    caption = http.notifications[0]["data"]["caption"]
    assert "Dãy A · Cửa chung" in caption and "Phi · Phòng P101" in caption and "Phòng B01" not in caption
    with store.connect() as db:
        assert db.execute("SELECT description FROM visits").fetchone()[0] == "Khớp mẫu: Phi · Phòng P101"


def test_admin_registers_shared_gate_and_gets_private_token(rental):
    store, client = rental
    data = {"house_id": 1, "device_id": "gate-02"}
    assert client.post("/manage/device", data=data).status_code == 302
    data['csrf'] = owner_csrf(client)
    result = client.post("/manage/device", data=data)
    assert result.status_code == 201 and result.headers["Cache-Control"] == "no-store"
    from re import search
    token = search(r'<input value="([A-Za-z0-9_-]{40,})" readonly', result.get_data(as_text=True))[1]
    assert store.authenticates("gate-02", digest(token.encode()))
    assert token not in client.get("/").get_data(as_text=True)
