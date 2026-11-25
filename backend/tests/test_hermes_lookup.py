import json

import pytest
from werkzeug.security import generate_password_hash

from rental.app import create_app
from rental.hermes_lookup import HermesVoiceAssistant, answer_telegram_lookup, telegram_lookup_allowed
from rental.store import RentalStore


PASSWORD = "temporary rental test password"


@pytest.fixture
def lookup(tmp_path, monkeypatch):
    monkeypatch.setattr("rental.hermes_lookup.local_today", lambda: "2026-11-24")
    monkeypatch.setattr(HermesVoiceAssistant, "complete", lambda *args, **kwargs: None)
    store = RentalStore(tmp_path / "rental.db")
    store.initialize()
    app = create_app(store, PASSWORD, single_house=False)
    with store.write() as db:
        db.executemany("INSERT INTO houses(id,name,telegram_chat_id) VALUES (?,?,?)",
                       [(1, "Dãy A", "123"), (2, "Dãy B", "456")])
        db.executemany("INSERT INTO rooms(id,house_id,code) VALUES (?,?,?)",
                       [(1, 1, "02"), (2, 2, "02"), (3, 1, "03"), (4, 2, "99")])
        db.executemany("INSERT INTO stays(id,room_id,name) VALUES (?,?,?)",
                       [(1, 1, "Khách A"), (2, 2, "Khách B"), (3, 1, "Cùng phòng với A")])
        db.executemany(
            """INSERT INTO rental_accounts(username,display_name,password_hash,role,house_id,stay_id)
               VALUES (?,?,?,?,?,?)""",
            [(name, name, generate_password_hash(PASSWORD), role, house, stay)
             for name, role, house, stay in [
                 ("manager-a", "manager", 1, None), ("tenant-a", "tenant", None, 1),
                 ("tenant-shared", "tenant", None, 3), ("technician-a", "technician", 1, None),
             ]],
        )
        db.executemany(
            """INSERT INTO rental_leases
               (stay_id,reference,starts_on,ends_on,rent_vnd,deposit_vnd,electricity_rate,water_rate)
               VALUES (?,'HD','2026-11-01','2027-03-01',?,0,0,0)""",
            [(1, 2_000_000), (2, 8_000_000)],
        )
        db.executemany(
            """INSERT INTO rental_invoices
               (id,stay_id,room_id,month,due_on,rent_vnd,electricity_start,electricity_end,electricity_rate,
                water_start,water_end,water_rate,other_vnd,total_vnd)
               VALUES (?,?,?,'2026-11','2026-12-01',?,0,0,0,0,0,0,0,?)""",
            [(1, 1, 1, 2_000_000, 2_000_000), (2, 2, 2, 8_000_000, 8_000_000)],
        )
        db.execute("""INSERT INTO rental_payments
                   (invoice_id,request_key,amount_vnd,paid_on,method,recorded_by)
                   VALUES (1,'sample',500000,'2026-11-24','cash','owner')""")
        db.execute("""INSERT INTO rental_tickets(stay_id,category,subject,description,status)
                      VALUES (1,'water','Vòi nước','Rỉ ở nhà tắm','in_progress')""")
    return store, app


def log_in(app, username=None):
    client = app.test_client()
    client.get("/login")
    with client.session_transaction() as state:
        csrf = state["csrf"]
    response = client.post("/login", data={"csrf": csrf, "username": username or "", "password": PASSWORD})
    assert response.status_code in {302, 303}
    with client.session_transaction() as state:
        csrf = state["csrf"]
    return client, csrf


def ask(client, csrf, question):
    return client.post("/hermes", data={"csrf": csrf, "question": question})


def test_telegram_answers_from_authorized_house_only(lookup):
    store, _ = lookup
    reply = answer_telegram_lookup(store, "123", "/hoi Dãy A còn phòng nào trống?")
    assert "Phòng 03" in reply and "Phòng 99" not in reply and "Phòng 02" not in reply
    reply = answer_telegram_lookup(store, 123, "Phòng 02 tháng này còn bao nhiêu tiền?")
    assert "1.500.000 ₫" in reply and "8.000.000 ₫" not in reply
    assert "8.000.000 ₫" not in answer_telegram_lookup(store, 123, "Dãy B phòng 02 còn bao nhiêu tiền?")
    assert answer_telegram_lookup(store, 321, "Phòng 02 còn bao nhiêu tiền?") is None
    assert answer_telegram_lookup(store, 456, "Dãy B còn phòng nào trống?", house_id=1) is None
    assert not telegram_lookup_allowed(store, -123)


def test_web_applies_auth_csrf_and_scoped_tenant_data(lookup):
    _, app = lookup
    assert app.test_client().get("/hermes").status_code == 302
    client, csrf = log_in(app, "tenant-a")
    assert ask(client, "", "Xem hóa đơn").status_code == 403
    assert "1.500.000 ₫" in ask(client, csrf, "Tháng này em còn phải đóng bao nhiêu?").get_data(as_text=True)
    denied = ask(client, csrf, "Hóa đơn dãy B phòng 02 tháng này?").get_data(as_text=True)
    assert "8.000.000 ₫" not in denied and "1.500.000 ₫" not in denied
    assert "01/03/2027" in ask(client, csrf, "Hợp đồng em hết hạn khi nào?").get_data(as_text=True)
    response = ask(client, csrf, "Yêu cầu sửa chữa của em tới đâu?")
    assert "Vòi nước" in response.get_data(as_text=True)
    shared, shared_csrf = log_in(app, "tenant-shared")
    assert "1.500.000 ₫" not in ask(shared, shared_csrf, "Hóa đơn phòng em?").get_data(as_text=True)

    # The chat sends the same request as the form and asks for JSON instead of a page.
    headers = {"Accept": "application/json"}
    data = {"csrf": csrf, "question": "Tháng này em còn phải đóng bao nhiêu?"}
    assert "1.500.000 ₫" in client.post("/hermes", data=data, headers=headers).json["reply"]
    other_house = {**data, "question": "Hóa đơn dãy B phòng 02 tháng này?"}
    assert "8.000.000 ₫" not in client.post("/hermes", data=other_house, headers=headers).json["reply"]
    invalid = client.post("/hermes", data={**data, "question": "   "}, headers=headers)
    assert invalid.status_code == 400 and "400 ký tự" in invalid.json["error"]
    assert client.post("/hermes", data={**data, "csrf": ""}, headers=headers).status_code == 403


def test_operators_resolve_room_ambiguity_and_deny_technicians(lookup):
    _, app = lookup
    owner, csrf = log_in(app)
    text = ask(owner, csrf, "Phòng 02 tháng này còn bao nhiêu tiền?").get_data(as_text=True)
    assert "Có nhiều phòng cùng mã" in text
    assert "8.000.000 ₫" in ask(owner, csrf, "Dãy B phòng 02 còn bao nhiêu tiền?").get_data(as_text=True)
    manager, manager_csrf = log_in(app, "manager-a")
    assert "8.000.000 ₫" not in ask(manager, manager_csrf, "Dãy B phòng 02 còn bao nhiêu tiền?").get_data(as_text=True)
    tech, tech_csrf = log_in(app, "technician-a")
    assert ask(tech, tech_csrf, "Dãy A còn phòng nào trống?").status_code == 403


def test_hermes_classifies_question_but_never_gets_ledger_data(lookup, monkeypatch):
    store, _ = lookup
    received = []

    def plan(self, messages, **kwargs):
        received.append(messages)
        return json.dumps({"kind": "invoice", "house": "", "room": "02", "month": "2026-11"})

    monkeypatch.setattr(HermesVoiceAssistant, "complete", plan)
    reply = answer_telegram_lookup(store, 123, "Phòng 02 còn thiếu mấy đồng?")
    assert "1.500.000 ₫" in reply
    assert received[0][1]["content"] == "Phòng 02 còn thiếu mấy đồng?"
    assert "Khách A" not in json.dumps(received)

    def misdirect(self, messages, **kwargs):
        return json.dumps({"kind": "invoice", "house": "B", "room": "02", "month": ""})

    monkeypatch.setattr(HermesVoiceAssistant, "complete", misdirect)
    denied = answer_telegram_lookup(store, 123, "Còn thiếu bao nhiêu?")
    assert "8.000.000 ₫" not in denied and "1.500.000 ₫" not in denied

    def revoke(self, messages, **kwargs):
        with store.write() as db:
            db.execute("UPDATE houses SET telegram_chat_id='' WHERE id=1")
        return json.dumps({"kind": "invoice", "house": "", "room": "02", "month": ""})

    monkeypatch.setattr(HermesVoiceAssistant, "complete", revoke)
    assert answer_telegram_lookup(store, 123, "Phòng 02 còn thiếu mấy đồng?") is None
