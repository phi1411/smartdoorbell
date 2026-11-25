import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from unittest.mock import Mock

import pytest
import requests

from rental.daily_report import initialize_daily_reports, run_report_cycle, start_daily_reports
from rental.store import RentalStore


MORNING_UTC = datetime(2026, 11, 24, 1, tzinfo=timezone.utc)
TOKEN = "fake-report-bot"


@pytest.fixture
def report_store(tmp_path):
    store = RentalStore(tmp_path / "report.db")
    store.initialize()
    initialize_daily_reports(store)
    with store.write() as db:
        db.executemany("INSERT INTO houses(id,name,telegram_chat_id) VALUES (?,?,?)", [
            (1, "Dãy A", "111"), (2, "Dãy B", "222"), (3, "Nhóm lớn", "-100333"), (4, "Dãy D", ""),
        ])
        db.executemany("INSERT INTO rooms(id,house_id,code) VALUES (?,?,?)", [
            (1, 1, "101"), (2, 1, "102"), (3, 1, "103"), (4, 1, "104"), (5, 1, "105"), (6, 2, "901"),
        ])
        db.execute("INSERT INTO stays(id,room_id,name) SELECT id,id,'Tên cư dân riêng tư' FROM rooms")
        db.execute(
            """INSERT INTO rental_leases(stay_id,reference,starts_on,rent_vnd,deposit_vnd,electricity_rate,water_rate)
               SELECT id,'HD','2026-11-01',1000000,0,0,0 FROM stays"""
        )
        db.executemany(
            """INSERT INTO rental_invoices(id,stay_id,room_id,month,due_on,rent_vnd,electricity_start,
                         electricity_end,electricity_rate,water_start,water_end,water_rate,other_vnd,total_vnd)
               VALUES (?,?,?,'2026-11',?,?,0,0,0,0,0,0,0,?)""",
            [
                (1, 1, 1, "2026-11-22", 2_000_000, 2_000_000),
                (2, 2, 2, "2026-11-24", 1_000_000, 1_000_000),
                (3, 3, 3, "2026-11-23", 900_000, 900_000),
                (4, 4, 4, "2026-11-23", 800_000, 800_000),
                (5, 5, 5, "2026-11-25", 700_000, 700_000),
                (6, 6, 6, "2026-11-24", 123_456, 123_456),
            ],
        )
        db.executemany(
            """INSERT INTO rental_payments(id,invoice_id,request_key,amount_vnd,paid_on,method,recorded_by)
               VALUES (?,?,?,?,'2026-11-23','cash','owner')""",
            [(1, 1, "part", 500_000), (2, 1, "voided", 250_000), (3, 3, "full", 900_000)],
        )
        db.execute("INSERT INTO rental_payment_voids(payment_id,reason,voided_by) VALUES (2,'nhập nhầm','owner')")
        db.execute(
            """INSERT INTO rental_invoice_voids(invoice_id,snapshot,reason,voided_by)
               VALUES (4,'{}','đã hủy','owner')"""
        )
        db.executemany(
            """INSERT INTO rental_tickets(stay_id,category,subject,description,status)
               VALUES (?,'water','Nội dung riêng tư','Chi tiết riêng tư',?)""",
            [(1, "open"), (1, "in_progress"), (1, "resolved"), (6, "open")],
        )
        db.executemany("INSERT INTO devices(id,house_id,token_hash) VALUES (?,?,'key')", [("esp-a", 1), ("esp-b", 2)])
        morning = int(MORNING_UTC.timestamp())
        db.executemany(
            """INSERT INTO visits(id,device_id,house_id,event_id,received_at,recipient_chat_id,jpeg,image_hash)
               VALUES (?,?,?,?,?,'old-chat',x'00','sha')""",
            [
                (1, "esp-a", 1, "missed", morning - 60),
                (2, "esp-a", 1, "answered", morning - 90),
                (3, "esp-a", 1, "boundary included", morning - 86_400),
                (4, "esp-a", 1, "too old", morning - 86_401),
                (5, "esp-a", 1, "next period", morning),
                (6, "esp-b", 2, "other house", morning - 30),
            ],
        )
        db.execute(
            "INSERT INTO intercom_calls(visit_id,accepted_at,answered_by,operator_hash) VALUES (2,?,'owner','hash')",
            (morning - 82,),
        )
    return store


def telegram_mock():
    http = Mock()
    http.post.return_value.status_code = 200
    http.post.return_value.json.return_value = {"ok": True}
    return http


def test_report_scopes_destination_and_uses_actual_balances(report_store):
    http = telegram_mock()
    run_report_cycle(report_store, TOKEN, Event(), MORNING_UTC, http)
    messages = {call.kwargs["json"]["chat_id"]: call.kwargs["json"]["text"] for call in http.post.call_args_list}
    assert set(messages) == {"111", "222"}
    for call in http.post.call_args_list:
        assert call.args == (f"https://api.telegram.org/bot{TOKEN}/sendMessage",)
        assert call.kwargs["allow_redirects"] is False
        assert "parse_mode" not in call.kwargs["json"]
    assert "Dãy A · 24/11/2026" in messages["111"]
    assert "1 đến hạn hôm nay, 1 quá hạn" in messages["111"]
    assert "2.500.000 ₫" in messages["111"]
    assert "Phòng 101 · 22/11 · còn 1.500.000 ₫" in messages["111"]
    assert "Phòng 102 · 24/11 · còn 1.000.000 ₫" in messages["111"]
    assert "Bảo trì · 1 phiếu mới, 1 đang xử lý" in messages["111"]
    assert "trong 24 giờ · 2 lượt" in messages["111"]
    assert all(omitted not in messages["111"] for omitted in (
        "Phòng 103", "Phòng 104", "Phòng 105", "Phòng 901", "riêng tư", "old-chat",
    ))
    assert "123.456 ₫" in messages["222"]


def test_report_time_and_rotation(report_store):
    http = telegram_mock()
    stop = Event()
    run_report_cycle(report_store, TOKEN, stop, MORNING_UTC - timedelta(minutes=1), http)
    run_report_cycle(report_store, TOKEN, stop, MORNING_UTC + timedelta(minutes=30), http)
    http.post.assert_not_called()
    run_report_cycle(report_store, TOKEN, stop, MORNING_UTC + timedelta(minutes=29), http, house_id=1)
    with report_store.write() as db:
        db.execute("UPDATE houses SET telegram_chat_id='999' WHERE id=1")
    run_report_cycle(report_store, TOKEN, stop, MORNING_UTC + timedelta(days=1), http, house_id=1)
    assert [call.kwargs["json"]["chat_id"] for call in http.post.call_args_list] == ["111", "999"]


def test_report_redeploy_and_concurrent_timeout_never_duplicate(report_store):
    http = telegram_mock()
    http.post.side_effect = requests.Timeout()
    stop = Event()
    restarted_store = RentalStore(report_store.path)
    initialize_daily_reports(restarted_store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = [
            pool.submit(run_report_cycle, store, TOKEN, stop, MORNING_UTC, http, house_id=1)
            for store in (report_store, restarted_store)
        ]
        for run in runs:
            run.result(timeout=5)
    run_report_cycle(restarted_store, TOKEN, stop, MORNING_UTC + timedelta(minutes=1), http, house_id=1)
    http.post.assert_called_once()
    with report_store.connect() as db:
        assert list(map(tuple, db.execute(
            "SELECT house_id, report_on, status FROM rental_report_deliveries"
        ))) == [(1, "2026-11-24", "unknown")]


def test_hermes_can_choose_only_available_priorities_from_counts(report_store):
    http = telegram_mock()
    assistant = Mock()
    assistant.complete.side_effect = ["maintenance", "bịa số tiền mới"]
    run_report_cycle(report_store, TOKEN, Event(), MORNING_UTC, http, assistant=assistant)
    model_input = assistant.complete.call_args_list[0].args[0][-1]["content"]
    assert json.loads(model_input) == {"overdue": 1, "maintenance": 2, "due": 1, "missed": 2}
    messages = [call.kwargs["json"]["text"] for call in http.post.call_args_list]
    assert "Hermes gợi ý · Xem các phiếu bảo trì đang chờ." in messages[0]
    assert "Việc nên xem trước · Xem các phiếu bảo trì đang chờ." in messages[1]
    assert "bịa số tiền mới" not in messages[1]


def test_rechecks_chat_binding_after_hermes_call(report_store):
    http = telegram_mock()

    def change_binding(*_args, **_kwargs):
        with report_store.write() as db:
            db.execute("UPDATE houses SET telegram_chat_id='999' WHERE id=1")
        return "due"

    assistant = Mock()
    assistant.complete.side_effect = change_binding
    run_report_cycle(report_store, TOKEN, Event(), MORNING_UTC, http, house_id=1, assistant=assistant)
    http.post.assert_not_called()
    with report_store.connect() as db:
        assert db.execute("SELECT status FROM rental_report_deliveries WHERE house_id=1").fetchone()[0] == "skipped"


def test_report_scheduler_respects_shutdown_without_sending(report_store):
    stop = Event()
    assert start_daily_reports(report_store, "", stop) is None
    stop.set()
    worker = start_daily_reports(report_store, TOKEN, stop)
    worker.join(timeout=2)
    assert not worker.is_alive()
