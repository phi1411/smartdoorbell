import json
import re
from pathlib import Path

import pytest
from flask import Flask, g, request
from jinja2 import ChoiceLoader, DictLoader

from rental.app import create_app
from rental.operations import create_operations_blueprint, initialize_operations
from rental.store import RentalStore


LEASE = {
    "reference": "HD-101",
    "starts_on": "2026-11-01",
    "rent_vnd": "2000000",
    "deposit_vnd": "500000",
    "electricity_rate": "3500",
    "water_rate": "15000",
}
BILL = {
    "month": "2026-11",
    "due_on": "2026-11-15",
    "electricity_start": "120",
    "electricity_end": "145",
    "water_start": "10",
    "water_end": "13",
    "other_vnd": "30000",
}
TOTAL = 2_162_500
INVOICE_PATH = "/operations/invoices/1"
PAYMENT = {
    "request_key": "a" * 32, "issue_number": "1", "amount_vnd": "500000", "paid_on": "2026-11-22",
    "method": "transfer", "note": "Nội bộ",
}


@pytest.fixture
def operations(tmp_path, monkeypatch):
    monkeypatch.setattr("rental.operations.local_today", lambda: "2026-11-22")
    store = RentalStore(tmp_path / "rental.db")
    store.initialize()
    initialize_operations(store)
    initialize_operations(store)
    with store.write() as db:
        db.executemany("INSERT INTO houses(id,name) VALUES (?,?)", [(1, "Dãy A"), (2, "Dãy B")])
        db.executemany("INSERT INTO rooms(id,house_id,code) VALUES (?,?,?)", [(1, 1, "101"), (2, 2, "201")])
        db.executemany("INSERT INTO stays(id,room_id,name) VALUES (?,?,?)",
                       [(1, 1, "Khách A"), (2, 2, "Khách B"), (3, 1, "Bạn cùng phòng")])
    template_dir = Path(__file__).resolve().parents[1] / "rental" / "templates"
    app = Flask(__name__, template_folder=template_dir)
    app.config.update(TESTING=True, SECRET_KEY="operations-tests-only")
    app.jinja_loader = ChoiceLoader([DictLoader({"base.html": "{% block content %}{% endblock %}"}), app.jinja_loader])
    app.register_blueprint(create_operations_blueprint(store))
    actors = {
        "owner": {"id": 1, "username": "owner", "role": "owner"},
        "manager-a": {"id": 2, "username": "manager-a", "role": "manager", "house_id": 1},
        "manager-b": {"id": 3, "username": "manager-b", "role": "manager", "house_id": 2},
        "tenant-a": {"id": 4, "username": "tenant-a", "role": "tenant", "stay_id": 1},
        "tenant-b": {"id": 5, "username": "tenant-b", "role": "tenant", "stay_id": 2},
        "technician-a": {"id": 6, "username": "technician-a", "role": "technician", "house_id": 1},
    }

    @app.before_request
    def inject_test_actor():
        g.actor = actors.get(request.headers.get("X-Test-Actor"))

    client = app.test_client()

    def call(path, actor="owner", data=None, follow_redirects=False):
        headers = {"X-Test-Actor": actor}
        if data is None:
            return client.get(path, headers=headers, follow_redirects=follow_redirects)
        return client.post(path, headers=headers, data=data, follow_redirects=follow_redirects)

    return store, call


def setup_bill(call):
    assert call("/operations/leases/1", "manager-a", LEASE).status_code == 303
    response = call("/operations/leases/1/invoices", "manager-a", BILL)
    assert response.status_code == 303 and response.location == "/operations/invoices/1"


def test_room_billing_snapshots_terms_and_blocks_duplicate_month(operations):
    store, call = operations
    setup_bill(call)
    duplicate = call("/operations/leases/1/invoices", data=BILL, follow_redirects=True)
    assert "đã có hóa đơn" in duplicate.text
    assert call("/operations/leases/3", data={**LEASE, "reference": "HD-101B"}).status_code == 303
    assert "đã có hóa đơn" in call("/operations/leases/3/invoices", data=BILL, follow_redirects=True).text
    assert call("/operations/leases/1", data={**LEASE, "rent_vnd": "3500000"}).status_code == 303
    assert "2.162.500" in call("/operations/invoices/1", "tenant-a").text
    assert "Quá hạn" in call("/operations?month=2026-11").text
    assert call("/operations/invoices/1", "tenant-b").status_code == 404
    assert call("/operations/invoices/1", "manager-b").status_code == 404
    assert call("/operations/leases/1", "manager-b").status_code == 404
    assert call("/operations/invoices/1", "technician-a").status_code == 403
    with store.connect() as db:
        assert db.execute("SELECT total_vnd FROM rental_invoices").fetchone()[0] == TOTAL
        assert db.execute("SELECT COUNT(*) FROM rental_invoices").fetchone()[0] == 1


@pytest.mark.parametrize(("contract_end", "last_month"), [(None, "2026-12"), ("2026-11-30", "2026-11")])
def test_checkout_allows_missing_bills_only_through_the_rental_period(operations, monkeypatch, contract_end, last_month):
    monkeypatch.setattr("rental.operations.local_today", lambda: "2026-12-05")
    store, call = operations
    contract = {**LEASE, "ends_on": contract_end or ""}
    assert call("/operations/leases/1", "manager-a", contract).status_code == 303
    with store.write() as db:
        # 00:30 on December 1 in Vietnam.
        db.execute("UPDATE stays SET ended_at='2026-11-30 17:30:00' WHERE id=1")

    archived = call("/operations/leases/1", "manager-a")
    assert archived.status_code == 200
    assert "Ngày 2026-12-01" in archived.text
    assert f'value="{last_month}" min="2026-11" max="{last_month}"' in archived.text
    assert "Lưu điều khoản" not in archived.text
    assert call("/operations/leases/1", "manager-a", contract).status_code == 404

    route = "/operations/leases/1/invoices"
    for month in ("2026-10", "2027-01"):
        result = call(route, "manager-a", {**BILL, "month": month, "due_on": "2027-01-15"}, follow_redirects=True)
        assert "Kỳ hóa đơn phải thuộc thời gian thuê" in result.text
    if contract_end:
        result = call(route, "manager-a", {**BILL, "month": "2026-12", "due_on": "2026-12-15"},
                      follow_redirects=True)
        assert "Kỳ hóa đơn phải thuộc thời gian thuê" in result.text

    bill = {**BILL, "month": last_month, "due_on": "2026-12-15"}
    assert call(route, "manager-b", bill).status_code == 404
    issued = call(route, "manager-a", bill)
    assert issued.status_code == 303 and issued.location == INVOICE_PATH
    assert "2.162.500" in call(INVOICE_PATH, "tenant-a").text
    assert call(INVOICE_PATH, "tenant-b").status_code == 404
    assert "đã có hóa đơn" in call(route, "manager-a", bill, follow_redirects=True).text
    with store.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT stay_id, month FROM rental_invoices")] == [(1, last_month)]


def test_payment_partial_full_and_idempotent_replay(operations):
    store, call = operations
    setup_bill(call)
    route = "/operations/invoices/1/payments"
    payment = PAYMENT
    assert call(route, "tenant-a", payment).status_code == 403
    assert call(route, "manager-b", payment).status_code == 404
    assert call(route, "manager-a", payment).status_code == 303
    assert call(route, "manager-a", payment).status_code == 303
    partial = call("/operations/invoices/1", "tenant-a").text
    assert "1.662.500" in partial and "Trả một phần" in partial and "Nội bộ" not in partial
    conflict = call(route, "manager-a", {**payment, "amount_vnd": "600000"}, follow_redirects=True)
    assert "đã có" in conflict.text
    assert "vượt" in call(route, "manager-a", {**payment, "request_key": "b" * 32,
                                              "amount_vnd": str(TOTAL)}, follow_redirects=True).text
    assert call(route, "manager-a", {**payment, "request_key": "b" * 32,
                                   "amount_vnd": str(TOTAL - 500_000)}).status_code == 303
    assert "Đã thanh toán" in call("/operations/invoices/1", "tenant-a").text
    with store.connect() as db:
        assert tuple(db.execute("SELECT COUNT(*), SUM(amount_vnd) FROM rental_payments").fetchone()) == (2, TOTAL)


def test_void_payment_restores_balance_and_preserves_receipt_history(operations):
    store, call = operations
    setup_bill(call)
    record = f"{INVOICE_PATH}/payments"
    void = f"{record}/1/void"
    assert call(record, "manager-a", PAYMENT).status_code == 303
    invoice_void = {"reason": "Sai chỉ số", "issue_number": "1"}
    blocked = call(f"{INVOICE_PATH}/void", "manager-a", invoice_void, follow_redirects=True)
    assert "khoản thu hiệu lực" in blocked.text
    assert "Hủy hóa đơn</summary>" not in blocked.text
    correction = {"reason": "Nhập trùng biên nhận"}
    assert call(void, "tenant-a", correction).status_code == 403
    assert call(void, "manager-b", correction).status_code == 404
    assert call(f"{record}/99/void", "manager-a", correction).status_code == 404
    assert "Kiểm tra lại lý do" in call(void, "manager-a", {}, follow_redirects=True).text
    assert call(void, "manager-a", correction).status_code == 303
    retry = call(void, "manager-a", {"reason": "Không được ghi đè"}, follow_redirects=True)
    assert "Khoản thu này đã hủy" in retry.text and correction["reason"] in retry.text
    tenant = call(INVOICE_PATH, "tenant-a").text
    assert "<dd>2.162.500 ₫</dd>" in tenant and "<dd>0 ₫</dd>" in tenant
    assert "Đã hủy" in tenant and correction["reason"] not in tenant
    portal = call("/operations?month=2026-11", "manager-a").text
    assert "<dt>Đã thanh toán</dt><dd>0 ₫</dd>" in portal
    assert "<dt>Còn phải thanh toán</dt><dd>2.162.500 ₫</dd>" in portal
    assert "Lần ghi nhận này đã có" in call(record, "manager-a", PAYMENT, follow_redirects=True).text
    assert call(record, "manager-a", {**PAYMENT, "request_key": "b" * 32}).status_code == 303
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM rental_payments").fetchone()[0] == 2
        assert tuple(db.execute("SELECT reason,voided_by FROM rental_payment_voids").fetchone()) == (
            correction["reason"], "manager-a",
        )


def test_void_and_reissue_invoice_preserves_snapshots_scope_and_issue_numbers(operations):
    store, call = operations
    setup_bill(call)
    issue_path = "/operations/leases/1/invoices"
    record_path = f"{INVOICE_PATH}/payments"
    void_path = f"{INVOICE_PATH}/void"
    assert call(record_path, "manager-a", PAYMENT).status_code == 303
    assert call(f"{record_path}/1/void", "manager-a", {"reason": "Nhập nhầm"}).status_code == 303
    first_void = {"reason": "Sai chỉ số điện", "issue_number": "1"}
    assert call(void_path, "tenant-a", first_void).status_code == 403
    assert call(void_path, "manager-b", first_void).status_code == 404
    assert call(void_path, "manager-a", first_void).status_code == 303
    cancelled = call(INVOICE_PATH, "manager-a").text
    assert "/operations/leases/1?reissue=1#monthly-invoice" in cancelled
    assert "Lịch sử phát hành" in cancelled and "Sai chỉ số điện" in cancelled
    tenant = call(INVOICE_PATH, "tenant-a").text
    assert "Đã hủy" in tenant and "Sai chỉ số điện" not in tenant and "Lập lại hóa đơn" not in tenant
    totals = call("/operations?month=2026-11", "manager-a").text
    assert "<dt>Tổng hóa đơn</dt><dd>0 ₫</dd>" in totals
    assert "<dt>Còn phải thanh toán</dt><dd>0 ₫</dd>" in totals
    payment = {**PAYMENT, "request_key": "c" * 32}
    assert "Hóa đơn đã thay đổi" in call(record_path, "manager-a", payment, follow_redirects=True).text
    assert "đã có hóa đơn đã hủy" in call(issue_path, "manager-a", BILL, follow_redirects=True).text
    assert call("/operations/leases/3?reissue=1", "manager-a").status_code == 404
    assert call("/operations/leases/1?reissue=1", "manager-b").status_code == 404

    assert call("/operations/leases/3", data={**LEASE, "reference": "HD-101B"}).status_code == 303
    assert call("/operations/leases/1", data={**LEASE, "rent_vnd": "3500000"}).status_code == 303
    with store.write() as db:
        db.execute("UPDATE stays SET ended_at=CURRENT_TIMESTAMP WHERE id=1")
    form = call("/operations/leases/1?reissue=1", "manager-a").text
    assert "Đã trả phòng" in form and 'value="2026-11" readonly' in form
    assert 'value="2000000"' in form and 'value="145"' in form and 'value="2026-11-15"' in form
    replacement = {**BILL, "reissue_id": "1", "reissue_void_id": "1", "rent_vnd": "2000000",
                   "electricity_rate": "3500", "water_rate": "15000", "electricity_end": "135"}
    assert call(issue_path, "manager-b", replacement).status_code == 404
    assert "Hóa đơn đã thay đổi" in call("/operations/leases/3/invoices", "manager-a",
                                        replacement, follow_redirects=True).text
    fixed = call(issue_path, "manager-a", replacement, follow_redirects=True).text
    assert "Đã lập lại hóa đơn." in fixed and "<dt>Lần phát hành</dt><dd>2</dd>" in fixed
    assert "<dd>2.127.500 ₫</dd>" in fixed and "Lần 1 · 2.162.500 ₫" in fixed
    assert "Hóa đơn đã thay đổi" in call(void_path, "manager-a", first_void, follow_redirects=True).text
    assert "Hóa đơn đã thay đổi" in call(record_path, "manager-a", payment, follow_redirects=True).text
    second_void = {**first_void, "reason": "Điều chỉnh phí bổ sung", "issue_number": "2"}
    assert call(void_path, "manager-a", second_void).status_code == 303
    stale = call(issue_path, "manager-a", replacement, follow_redirects=True).text
    assert "Hóa đơn đã thay đổi" in stale
    assert call(issue_path, "manager-a", {**replacement, "reissue_void_id": "2"}).status_code == 303
    assert "Lần ghi nhận này đã có" in call(record_path, "manager-a", {**PAYMENT, "issue_number": "3"},
                                          follow_redirects=True).text
    assert call(record_path, "manager-a", {**payment, "issue_number": "3"}).status_code == 303
    with store.connect() as db:
        assert tuple(db.execute("SELECT id,stay_id,total_vnd FROM rental_invoices").fetchone()) == (1, 1, 2_127_500)
        revisions = db.execute("SELECT snapshot,reissued_at FROM rental_invoice_voids ORDER BY id").fetchall()
        assert [json.loads(row["snapshot"])["electricity_end"] for row in revisions] == [145, 135]
        assert all(row["reissued_at"] for row in revisions)
        assert db.execute("SELECT COUNT(*) FROM rental_payments").fetchone()[0] == 2


def test_adding_void_history_preserves_existing_bills_and_payments(operations):
    store, call = operations
    setup_bill(call)
    assert call(f"{INVOICE_PATH}/payments", "manager-a", PAYMENT).status_code == 303
    with store.write() as db:
        db.execute("DROP TABLE rental_payment_voids")
        db.execute("DROP TABLE rental_invoice_voids")
    initialize_operations(store)
    initialize_operations(store)
    existing = call(INVOICE_PATH, "tenant-a").text
    assert "<dd>2.162.500 ₫</dd>" in existing and "<dd>1.662.500 ₫</dd>" in existing
    assert "<dt>Lần phát hành</dt><dd>1</dd>" in existing
    assert "Lịch sử phát hành" not in existing


def test_maintenance_access_and_checkout(operations):
    store, call = operations
    ticket = {"subject": "Vòi nước nhỏ giọt", "category": "water", "description": "Cần kiểm tra", "stay_id": "2"}
    assert call("/operations/tickets", "tenant-a", ticket).status_code == 303
    assert "Vòi nước" not in call("/operations", "tenant-b").text
    assert "Vòi nước" not in call("/operations", "manager-b").text
    assert "Vòi nước" in call("/operations", "technician-a").text
    status = {"status": "resolved", "resolution": "Đã thay ron."}
    assert call("/operations/tickets/1", "manager-b", status).status_code == 404
    assert call("/operations/tickets/1", "tenant-a", status).status_code == 403
    assert call("/operations/tickets/1", "technician-a", status).status_code == 303
    assert "Đã thay ron" in call("/operations", "tenant-a").text
    assert call("/operations/tickets/1", "technician-a", {"status": "in_progress"}).status_code == 403
    with store.write() as db:
        db.execute("UPDATE stays SET ended_at=CURRENT_TIMESTAMP WHERE id=1")
    assert call("/operations/tickets", "tenant-a", ticket).status_code == 403
    assert "Vòi nước" in call("/operations", "tenant-a").text


def test_stale_maintenance_form_cannot_overwrite_another_update(operations):
    store, call = operations
    ticket = {"subject": "Vòi nước nhỏ giọt", "category": "water", "description": "Cần kiểm tra"}
    assert call("/operations/tickets", "tenant-a", ticket).status_code == 303
    initial = call("/operations", "manager-a").text
    revision = re.search(r'name="revision" value="([0-9a-f]{64})"', initial)[1]
    first_update = {"status": "in_progress", "resolution": "Đã kiểm tra van.", "revision": revision}
    assert call("/operations/tickets/1", "technician-a", first_update).status_code == 303

    stale = call("/operations/tickets/1", "manager-a", {"status": "open", "revision": revision},
                 follow_redirects=True)
    assert "Yêu cầu vừa được cập nhật" in stale.text
    assert '<option value="in_progress" selected>' in stale.text
    assert ">Đã kiểm tra van.</textarea>" in stale.text
    with store.connect() as db:
        assert tuple(db.execute("SELECT status, resolution FROM rental_tickets").fetchone()) == (
            "in_progress", "Đã kiểm tra van.",
        )


def test_house_notice_is_visible_only_for_active_tenants_in_scope(operations):
    store, call = operations
    note = {"house_id": "1", "title": "Sửa cổng", "body": "Cổng bảo trì từ 9 giờ."}
    assert call("/operations/notices", "manager-b", note).status_code == 404
    assert call("/operations/notices", "manager-a", note).status_code == 303
    assert "Sửa cổng" in call("/operations", "tenant-a").text
    assert "Sửa cổng" not in call("/operations", "tenant-b").text
    assert "Sửa cổng" not in call("/operations", "technician-a").text
    assert call("/operations/notices/1/archive", "manager-b", {}).status_code == 404
    assert call("/operations/notices/1/archive", "manager-a", {}).status_code == 303
    assert "Sửa cổng" not in call("/operations", "tenant-a").text
    assert call("/operations/notices", "manager-a", note).status_code == 303
    with store.write() as db:
        db.execute("UPDATE stays SET ended_at=CURRENT_TIMESTAMP WHERE id=1")
    assert "Sửa cổng" not in call("/operations", "tenant-a").text
    assert call("/operations", "invalid-actor").status_code == 403


def test_existing_app_wires_portal_authentication_and_csrf(operations):
    store, _ = operations
    owner_password = "temporary owner test password"
    tenant_password = "temporary tenant test password"
    app = create_app(store, owner_password)
    owner = app.test_client()
    tenant = app.test_client()

    def token(client):
        with client.session_transaction() as web_session:
            return web_session["csrf"]

    assert tenant.get("/operations").status_code == 302
    owner.get("/login")
    assert owner.post("/login", data={"csrf": token(owner), "password": owner_password}).status_code == 303
    assert owner.get("/operations").status_code == 200
    account = {"csrf": token(owner), "username": "tenant-a", "password": tenant_password,
               "role": "tenant", "stay_id": "1", "display_name": "Người thuê A"}
    assert owner.post("/accounts", data=account).status_code == 303

    tenant.get("/login")
    assert tenant.post("/login", data={"csrf": token(tenant), "username": "tenant-a",
                                      "password": tenant_password}).status_code == 303
    portal = tenant.get("/operations")
    assert "Phòng của tôi" in portal.text and "Camera trực tiếp" not in portal.text
    ticket = {"category": "electricity", "subject": "Đèn hỏng", "description": "Đèn trước phòng không sáng."}
    assert tenant.post("/operations/tickets", data=ticket).status_code == 403
    assert tenant.post("/operations/tickets", data={**ticket, "csrf": token(tenant)}).status_code == 303
    assert "Đèn hỏng" in owner.get("/operations").text
