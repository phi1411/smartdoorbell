from io import BytesIO

import pytest
import requests
from PIL import Image

from rental.face_profiles import PAD_LABELS, VECTOR
from rental.flash import PAD_DEMO_REQUESTER
from rental.worker import VisitWorker
from test_rental import FakeProviders, FakeResponse, TOKEN, post_image, rental


MATCH = [1] + [0] * 127
STRANGER = [0, 1] + [0] * 126


def test_doorbell_preserves_orientation_when_cleaning_jpeg(rental):
    store, client = rental
    source = Image.new("RGB", (640, 480), "navy")
    exif = source.getexif()
    exif[274] = 6
    payload = BytesIO()
    source.save(payload, "JPEG", exif=exif)
    assert post_image(client, data=payload.getvalue()).status_code == 202
    with store.connect() as db:
        sanitized = db.execute("SELECT jpeg FROM visits").fetchone()[0]
    with Image.open(BytesIO(sanitized)) as photo:
        assert photo.size == (480, 640)
        assert photo.getexif().get(274) is None


@pytest.mark.parametrize(("pad", "vectors", "device", "departure", "expect_flash"), [
    ("passed", [MATCH], "doorbell-01", False, True),
    ("passed", [MATCH], "", False, False),
    ("passed", [MATCH], "doorbell-02", False, False),
    ("passed", [MATCH], "doorbell-01", True, False),
    ("passed", [STRANGER], "doorbell-01", False, False),
    ("passed", [MATCH, STRANGER], "doorbell-01", False, False),
    ("suspected", [MATCH], "doorbell-01", False, False),
    ("uncertain", [MATCH], "doorbell-01", False, False),
    ("small_face", [MATCH], "doorbell-01", False, False),
    ("unavailable", [MATCH], "doorbell-01", False, False),
    ("no_face", [], "doorbell-01", False, False),
    ("transport_failure", [], "doorbell-01", False, False),
])
def test_pad_worker_labels_and_guards_demo_flash(rental, monkeypatch, pad, vectors, device, departure, expect_flash):
    store, client = rental
    with store.write() as db:
        db.execute("INSERT INTO stays(id,room_id,name) VALUES (1,1,'Phi')")
    assert store.save_face_sample(1, VECTOR.pack(*MATCH), 5) == "saved"
    assert post_image(client).status_code == 202
    monkeypatch.setenv("RENTAL_FACE_PAD_URL", "http://face.test/internal/visit-analysis")
    monkeypatch.setenv("RENTAL_FACE_API_KEY", "pad-key" * 6)
    monkeypatch.setenv("RENTAL_PAD_DEMO_DEVICE", device)
    monkeypatch.setenv("RENTAL_HERMES_URL", "https://hermes.test/v1")
    monkeypatch.setenv("RENTAL_HERMES_KEY", "test-hermes")
    monkeypatch.setenv("RENTAL_TELEGRAM_BOT_TOKEN", "test-bot")

    class Providers(FakeProviders):
        def post(self, url, **kwargs):
            if url.endswith("/internal/visit-analysis"):
                if departure:
                    with store.write() as db:
                        db.execute("UPDATE stays SET ended_at=CURRENT_TIMESTAMP WHERE id=1")
                if pad == "transport_failure":
                    raise requests.ConnectionError()
                assert kwargs["headers"]["Content-Type"] == "image/jpeg"
                assert kwargs["headers"].get("X-Pad-Demo") == ("1" if device == "doorbell-01" else None)
                return FakeResponse({"vectors": vectors, "pad": pad})
            return super().post(url, **kwargs)

    http = Providers()
    worker = VisitWorker(store, http)
    assert worker.run_one()
    assert http.notifications and not http.analyses
    assert worker.run_description()
    with store.connect() as db:
        description = db.execute("SELECT description FROM visits").fetchone()[0]
        request = db.execute("SELECT requested_by FROM doorbell_flash_commands").fetchone()
    expected_pad = "unavailable" if pad == "transport_failure" or len(vectors) > 1 else pad
    label = "PAD 2D (demo)" if device == "doorbell-01" else "PAD 2D"
    expected_label = f"{label}: {PAD_LABELS[expected_pad]}"
    assert expected_label in description
    assert expected_label in http.notifications[0]["data"]["caption"]
    assert expected_label in http.caption_edits[0]["data"]["caption"]
    assert PAD_LABELS[expected_pad] in http.analyses[0]["messages"][0]["content"][0]["text"]
    assert request is None or request["requested_by"] == PAD_DEMO_REQUESTER
    assert bool(request) == expect_flash
    demand = client.get("/api/device/live/demand", headers={"X-Device-ID": "doorbell-01",
                        "Authorization": f"Bearer {TOKEN}", "X-Doorbell-Flash-Capable": "1"})
    assert ("X-Doorbell-Flash" in demand.headers) == expect_flash
