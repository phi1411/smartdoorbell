import json

import pytest

from rental.app import create_app
from rental.store import RentalStore


ROOT = "/operations/voice-repair"
TOKEN = "tenant-repair-test-csrf"
CSRF = {"X-CSRF-Token": TOKEN}


@pytest.fixture
def service(tmp_path):
    store = RentalStore(tmp_path / "repair.db")
    store.initialize()
    app = create_app(store, "local tenant test password", single_house=False)
    with store.write() as db:
        db.executemany("INSERT INTO houses(id,name) VALUES (?,?)", [(1, "Dãy A"), (2, "Dãy B")])
        db.executemany("INSERT INTO rooms(id,house_id,code) VALUES (?,?,?)", [(1, 1, "101"), (2, 2, "202")])
        db.executemany("INSERT INTO stays(id,room_id,name) VALUES (?,?,?)", [(1, 1, "Khách A"), (2, 2, "Khách B")])
        db.executemany(
            """INSERT INTO rental_accounts(id,username,display_name,password_hash,role,stay_id)
               VALUES (?, ?, ?, 'unused', 'tenant', ?)""",
            [(1, "tenant-a", "A", 1), (2, "tenant-b", "B", 2)],
        )

    def client_for(account_id):
        client = app.test_client()
        with client.session_transaction() as account:
            account.update(account_id=account_id, account_version=1, csrf=TOKEN)
        return client

    return store, client_for


def test_repair_draft_requires_confirmation_and_replays_safely(service, monkeypatch):
    store, client_for = service
    client = client_for(1)
    page = client.get(ROOT)
    assert page.status_code == 200
    assert "Phòng 101" in page.text and "Phòng 202" not in page.text
    assert ROOT in client.get("/operations").text
    assert client.post(ROOT + "/draft", json={"issue": "Vòi bị rò"}).status_code == 403
    assert client.post(ROOT + "/transcribe", data=b"not audio", content_type="audio/webm",
                       headers=CSRF).status_code == 415

    def transcribe(_, stay_id, audio, media_type):
        assert stay_id == 1 and audio.startswith(b"\x1a\x45\xdf\xa3") and media_type == "audio/webm"
        return "Vòi nước bị rò", 200

    monkeypatch.setattr("rental.tenant_repair.HermesVoiceAssistant.transcribe", transcribe)
    transcript = client.post(ROOT + "/transcribe", data=b"\x1a\x45\xdf\xa3test audio",
                             content_type="audio/webm;codecs=opus", headers=CSRF)
    assert transcript.json == {"text": "Vòi nước bị rò"}

    def model(_, messages, **_options):
        note = messages[-1]["content"]
        assert "Khách A" not in note and "101" not in note
        return json.dumps({"subject": "Vòi nước rò", "category": "water",
                           "question": "Vòi ở khu vực nào?" if "nhà vệ sinh" not in note else ""})

    monkeypatch.setattr("rental.tenant_repair.HermesVoiceAssistant.complete", model)
    follow_up = client.post(ROOT + "/draft", json={"issue": "Vòi nước bị rò"}, headers=CSRF)
    assert follow_up.json == {"question": "Vòi ở khu vực nào?"}
    proposal = client.post(ROOT + "/draft", json={"issue": "Vòi nước nhà vệ sinh bị rò"}, headers=CSRF)
    assert proposal.json["category"] == "water"
    assert proposal.json["description"] == "Vòi nước nhà vệ sinh bị rò"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM rental_tickets").fetchone()[0] == 0

    fields = {**proposal.json, "subject": "Rò vòi nước nhà vệ sinh", "csrf": TOKEN, "stay_id": 2}
    assert client_for(2).post(ROOT + "/confirm", data=fields).status_code == 403
    for _ in range(2):
        result = client.post(ROOT + "/confirm", data=fields)
        assert result.status_code == 303 and result.location == "/operations#maintenance"
    with store.connect() as db:
        rows = db.execute("SELECT stay_id, category, subject FROM rental_tickets").fetchall()
    assert [tuple(row) for row in rows] == [(1, "water", "Rò vòi nước nhà vệ sinh")]


def test_tenant_repair_falls_back_to_editable_draft_and_blocks_after_checkout(service, monkeypatch):
    store, client_for = service
    client = client_for(1)
    monkeypatch.setattr("rental.tenant_repair.HermesVoiceAssistant.complete", lambda *_args, **_kw: None)
    response = client.post(ROOT + "/draft", json={"issue": "Quạt hỏng"}, headers=CSRF)
    assert response.json["subject"] == "Quạt hỏng" and response.json["notice"]
    with store.write() as db:
        db.execute("UPDATE stays SET ended_at=CURRENT_TIMESTAMP WHERE id=1")
    assert client.post(ROOT + "/confirm", data={**response.json, "csrf": TOKEN}).status_code == 403
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM rental_tickets").fetchone()[0] == 0
