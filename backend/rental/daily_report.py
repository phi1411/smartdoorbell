"""Send a house's morning report only to its configured private Telegram chat."""

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from threading import Thread

import requests

from rental.hermes_voice import HermesVoiceAssistant
from rental.intercom import initialize_intercom
from rental.operations import INVOICE_QUERY, initialize_operations


logger = logging.getLogger(__name__)
VIETNAM_TIME = timezone(timedelta(hours=7))
REPORT_HOUR = 8
REPORT_WINDOW_MINUTES = 30
INVOICE_LINES = 5
PRIVATE_CHAT = re.compile(r"[1-9][0-9]{0,15}")
PRIORITIES = {
    "overdue": "Rà soát các hóa đơn quá hạn.",
    "maintenance": "Xem các phiếu bảo trì đang chờ.",
    "due": "Đối chiếu các hóa đơn đến hạn hôm nay.",
    "missed": "Xem lại các lượt chuông chưa trả lời.",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS rental_report_deliveries (
    house_id INTEGER NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
    report_on TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('reserved','sent','failed','unknown','skipped')),
    reserved_at INTEGER NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY (house_id, report_on)
);
CREATE INDEX IF NOT EXISTS visits_house_received ON visits(house_id, received_at);
"""


def initialize_daily_reports(store):
    # The worker can run with or without the Flask process that normally creates these tables.
    initialize_operations(store)
    initialize_intercom(store)
    with store.connect() as db:
        db.executescript(SCHEMA)


def money(amount):
    return f"{amount:,}".replace(",", ".") + " ₫"


def report_content(db, house, morning):
    report_on = morning.date().isoformat()
    invoices = db.execute(
        f"""SELECT code, due_on, total_vnd - paid_vnd AS remaining_vnd
            FROM ({INVOICE_QUERY})
            WHERE house_id=? AND due_on<=? AND void_id IS NULL AND paid_vnd<total_vnd
            ORDER BY due_on, code""",
        (house["id"], report_on),
    )
    due, overdue, amount = 0, 0, 0
    invoice_details = []
    for invoice in invoices:
        if invoice["due_on"] == report_on:
            due += 1
        else:
            overdue += 1
        amount += invoice["remaining_vnd"]
        if len(invoice_details) < INVOICE_LINES:
            code = " ".join(invoice["code"].split())[:32]
            day = invoice["due_on"][8:10] + "/" + invoice["due_on"][5:7]
            invoice_details.append(f"• Phòng {code} · {day} · còn {money(invoice['remaining_vnd'])}")

    new_tickets, in_progress = db.execute(
        """SELECT count(*) FILTER (WHERE t.status='open'),
                  count(*) FILTER (WHERE t.status='in_progress')
           FROM rental_tickets t JOIN stays s ON s.id=t.stay_id
           JOIN rooms r ON r.id=s.room_id
           WHERE r.house_id=? AND t.status IN ('open','in_progress')""",
        (house["id"],),
    ).fetchone()
    missed = db.execute(
        """SELECT count(*) FROM visits v LEFT JOIN intercom_calls c ON c.visit_id=v.id
           WHERE v.house_id=? AND v.received_at>=? AND v.received_at<? AND c.visit_id IS NULL""",
        (house["id"], int((morning - timedelta(days=1)).timestamp()), int(morning.timestamp())),
    ).fetchone()[0]
    name = " ".join(house["name"].split())[:80]
    lines = [
        f"Báo cáo sáng · {name} · {morning:%d/%m/%Y}",
        f"Hóa đơn chưa thu đủ · {due} đến hạn hôm nay, {overdue} quá hạn",
        f"Số tiền còn lại · {money(amount)}",
        *invoice_details,
    ]
    if due + overdue > len(invoice_details):
        lines.append(f"Xem thêm {due + overdue - len(invoice_details)} hóa đơn trong mục Vận hành.")
    lines.extend((
        f"Bảo trì · {new_tickets} phiếu mới, {in_progress} đang xử lý",
        f"Chuông chưa được chủ trọ trả lời qua web trong 24 giờ · {missed} lượt",
    ))
    return "\n".join(lines), {"overdue": overdue, "maintenance": new_tickets + in_progress,
                              "due": due, "missed": missed}


def report_priority(counts, assistant):
    available = {category: counts[category] for category in PRIORITIES if counts[category] > 0}
    if not available:
        return ""
    if assistant is not None:
        try:
            choice = assistant.complete(
                [
                    {"role": "system", "content": (
                        "Chọn đúng một mã có trong JSON cho việc chủ trọ nên xem trước. "
                        "overdue là hóa đơn quá hạn, maintenance là phiếu bảo trì chưa xử lý xong, "
                        "due là hóa đơn đến hạn hôm nay, missed là chuông chủ trọ chưa trả lời. "
                        "Trả duy nhất mã trong JSON."
                    )},
                    {"role": "user", "content": json.dumps(available)},
                ],
                max_reply=24, timeout=(3, 7),
            )
            if choice in available:
                return f"\nHermes gợi ý · {PRIORITIES[choice]}"
        except (requests.RequestException, ValueError, KeyError, TypeError, IndexError, AttributeError):
            pass
    return f"\nViệc nên xem trước · {PRIORITIES[next(iter(available))]}"


def send_report(http, bot_token, chat_id, message):
    try:
        response = http.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "protect_content": True},
            timeout=(4, 8), allow_redirects=False,
        )
        if response.status_code == 200:
            result = response.json()
            if isinstance(result, dict) and result.get("ok") is True:
                return "sent"
        if 400 <= response.status_code < 500:
            return "failed"
    except (requests.RequestException, ValueError):
        pass
    # Telegram may already have accepted the request. The reservation prevents a retry.
    return "unknown"


def run_report_cycle(store, bot_token, stop, now, http, *, house_id=None, assistant=None):
    current = now.astimezone(VIETNAM_TIME)
    if current.hour != REPORT_HOUR or current.minute >= REPORT_WINDOW_MINUTES or stop.is_set():
        return
    morning = current.replace(minute=0, second=0, microsecond=0)
    report_on = morning.date().isoformat()
    with store.connect() as db:
        targets = db.execute(
            """SELECT h.id, h.name, h.telegram_chat_id FROM houses h
               WHERE (? IS NULL OR h.id=?) AND h.telegram_chat_id<>''
                 AND NOT EXISTS (SELECT 1 FROM rental_report_deliveries d
                                 WHERE d.house_id=h.id AND d.report_on=?)
               ORDER BY h.id""",
            (house_id, house_id, report_on),
        ).fetchall()
    for house in targets:
        chat_id = house["telegram_chat_id"]
        if stop.is_set():
            return
        if not PRIVATE_CHAT.fullmatch(chat_id):
            continue
        with store.write() as db:
            reserved = db.execute(
                """INSERT INTO rental_report_deliveries(house_id, report_on, status)
                   SELECT id, ?, 'reserved' FROM houses WHERE id=? AND telegram_chat_id=?
                   ON CONFLICT(house_id, report_on) DO NOTHING""",
                (report_on, house["id"], chat_id),
            ).rowcount
            if not reserved:
                continue
            message, counts = report_content(db, house, morning)
        message += report_priority(counts, assistant)
        with store.connect() as db:
            still_linked = db.execute(
                "SELECT 1 FROM houses WHERE id=? AND telegram_chat_id=?", (house["id"], chat_id),
            ).fetchone()
        status = "skipped"
        if still_linked and not stop.is_set():
            status = send_report(http, bot_token, chat_id, message)
        with store.write() as db:
            db.execute(
                "UPDATE rental_report_deliveries SET status=? WHERE house_id=? AND report_on=?",
                (status, house["id"], report_on),
            )
        if status in {"failed", "unknown"}:
            logger.warning("Rental daily report delivery %s for house %d.", status, house["id"])


def start_daily_reports(store, bot_token, stop, *, house_id=None):
    if not bot_token:
        return None

    def run():
        initialized = False
        with requests.Session() as http:
            assistant = HermesVoiceAssistant(store, http)
            while not stop.is_set():
                try:
                    if not initialized:
                        initialize_daily_reports(store)
                        initialized = True
                    run_report_cycle(store, bot_token, stop, datetime.now(VIETNAM_TIME), http,
                                     house_id=house_id, assistant=assistant)
                except sqlite3.OperationalError:
                    logger.warning("Rental daily reports could not reach storage.")
                stop.wait(30)

    worker = Thread(target=run, daemon=True, name="rental-daily-report")
    worker.start()
    return worker
