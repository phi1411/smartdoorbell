"""Lease terms, rent ledger, maintenance and announcements for the rental portal."""

import json
import re
import secrets
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from rental.accounts import require_roles, scoped_house
from rental.store import digest


OPERATORS = ("owner", "manager")
TICKET_HANDLERS = (*OPERATORS, "technician")
MONEY_LIMIT = 1_000_000_000
TICKET_STATES = {"open": "Mới gửi", "in_progress": "Đang xử lý", "resolved": "Đã xử lý"}
TICKET_CATEGORIES = {
    "electricity": "Điện", "water": "Nước", "equipment": "Thiết bị", "other": "Khác",
}
PAYMENT_METHODS = {"cash": "Tiền mặt", "transfer": "Chuyển khoản", "other": "Khác"}
MONTH_PATTERN = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")
DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rental_leases (
    stay_id INTEGER PRIMARY KEY REFERENCES stays(id),
    reference TEXT NOT NULL,
    starts_on TEXT NOT NULL,
    ends_on TEXT,
    rent_vnd INTEGER NOT NULL CHECK (rent_vnd >= 0),
    deposit_vnd INTEGER NOT NULL CHECK (deposit_vnd >= 0),
    electricity_rate INTEGER NOT NULL CHECK (electricity_rate >= 0),
    water_rate INTEGER NOT NULL CHECK (water_rate >= 0),
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (ends_on IS NULL OR ends_on >= starts_on)
);
CREATE TABLE IF NOT EXISTS rental_invoices (
    id INTEGER PRIMARY KEY,
    stay_id INTEGER NOT NULL REFERENCES rental_leases(stay_id),
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    month TEXT NOT NULL,
    due_on TEXT NOT NULL,
    rent_vnd INTEGER NOT NULL CHECK (rent_vnd >= 0),
    electricity_start INTEGER NOT NULL CHECK (electricity_start >= 0),
    electricity_end INTEGER NOT NULL CHECK (electricity_end >= electricity_start),
    electricity_rate INTEGER NOT NULL CHECK (electricity_rate >= 0),
    water_start INTEGER NOT NULL CHECK (water_start >= 0),
    water_end INTEGER NOT NULL CHECK (water_end >= water_start),
    water_rate INTEGER NOT NULL CHECK (water_rate >= 0),
    other_vnd INTEGER NOT NULL CHECK (other_vnd >= 0),
    notes TEXT NOT NULL DEFAULT '',
    total_vnd INTEGER NOT NULL CHECK (total_vnd > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (room_id, month)
);
CREATE INDEX IF NOT EXISTS rental_invoices_stay ON rental_invoices(stay_id, month DESC);
CREATE TABLE IF NOT EXISTS rental_payments (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES rental_invoices(id),
    request_key TEXT NOT NULL,
    amount_vnd INTEGER NOT NULL CHECK (amount_vnd > 0),
    paid_on TEXT NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('cash','transfer','other')),
    note TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (invoice_id, request_key)
);
CREATE TABLE IF NOT EXISTS rental_payment_voids (
    payment_id INTEGER PRIMARY KEY REFERENCES rental_payments(id),
    reason TEXT NOT NULL,
    voided_by TEXT NOT NULL,
    voided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS rental_invoice_voids (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES rental_invoices(id),
    snapshot TEXT NOT NULL,
    reason TEXT NOT NULL,
    voided_by TEXT NOT NULL,
    voided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reissued_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS rental_invoice_current_void
    ON rental_invoice_voids(invoice_id) WHERE reissued_at IS NULL;
CREATE INDEX IF NOT EXISTS rental_invoice_void_history ON rental_invoice_voids(invoice_id, id);
CREATE TABLE IF NOT EXISTS rental_tickets (
    id INTEGER PRIMARY KEY,
    stay_id INTEGER NOT NULL REFERENCES stays(id),
    category TEXT NOT NULL CHECK (category IN ('electricity','water','equipment','other')),
    subject TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','in_progress','resolved')),
    resolution TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS rental_tickets_stay ON rental_tickets(stay_id, id DESC);
CREATE TABLE IF NOT EXISTS rental_notices (
    id INTEGER PRIMARY KEY,
    house_id INTEGER NOT NULL REFERENCES houses(id),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archived_at TEXT
);
CREATE INDEX IF NOT EXISTS rental_notices_house ON rental_notices(house_id, id DESC);
"""

STAY_QUERY = """SELECT s.id, s.name, s.started_at, s.ended_at, r.id AS room_id, r.code,
                       date(s.started_at, '+7 hours') AS started_on, date(s.ended_at, '+7 hours') AS ended_on,
                       h.id AS house_id, h.name AS house_name
                FROM stays AS s JOIN rooms AS r ON r.id=s.room_id JOIN houses AS h ON h.id=r.house_id"""

INVOICE_QUERY = """SELECT i.*, s.name AS resident_name, r.code, h.id AS house_id, h.name AS house_name,
                          COALESCE((SELECT SUM(p.amount_vnd) FROM rental_payments AS p
                                    LEFT JOIN rental_payment_voids AS pv ON pv.payment_id=p.id
                                    WHERE p.invoice_id=i.id AND pv.payment_id IS NULL), 0) AS paid_vnd,
                          (SELECT iv.id FROM rental_invoice_voids AS iv
                           WHERE iv.invoice_id=i.id AND iv.reissued_at IS NULL) AS void_id,
                          1 + (SELECT COUNT(*) FROM rental_invoice_voids AS iv
                               WHERE iv.invoice_id=i.id AND iv.reissued_at IS NOT NULL) AS issue_number
                   FROM rental_invoices AS i JOIN stays AS s ON s.id=i.stay_id
                   JOIN rooms AS r ON r.id=i.room_id JOIN houses AS h ON h.id=r.house_id"""

INVOICE_TERMS = ("due_on", "rent_vnd", "electricity_start", "electricity_end", "electricity_rate",
                 "water_start", "water_end", "water_rate", "other_vnd", "notes", "total_vnd")


class FormError(ValueError):
    """An expected validation failure that can be shown to the user."""


def initialize_operations(store):
    with store.connect() as db:
        db.executescript(SCHEMA)


def local_today():
    return datetime.now(timezone(timedelta(hours=7))).date().isoformat()


def read_text(key, label, limit, *, required=True):
    value = request.form.get(key, "").strip()
    if (required and not value) or len(value) > limit or any(
        not character.isprintable() and character not in "\r\n\t" for character in value
    ):
        raise FormError(f"Kiểm tra lại {label} (tối đa {limit} ký tự).")
    return value


def read_number(key, label, *, minimum=0, blank=0, maximum=MONEY_LIMIT):
    value = request.form.get(key, "").strip()
    if not value and blank is not None:
        return blank
    if not re.fullmatch(r"[0-9]{1,10}", value) or not minimum <= int(value) <= maximum:
        raise FormError(f"Kiểm tra lại {label}. Hãy nhập số nguyên hợp lệ.")
    return int(value)


def read_date(key, label, *, optional=False):
    value = request.form.get(key, "").strip()
    if not value and optional:
        return None
    try:
        if not DATE_PATTERN.fullmatch(value) or date.fromisoformat(value).year < 1900:
            raise ValueError
    except ValueError:
        raise FormError(f"Kiểm tra lại {label}.") from None
    return value


def visible_stay(db, actor, stay_id, *, active=False):
    row = db.execute(STAY_QUERY + " WHERE s.id=?", (stay_id,)).fetchone()
    if row is None or not scoped_house(actor, row["house_id"]) or (active and row["ended_at"]):
        abort(404)
    return row


def visible_invoice(db, actor, invoice_id):
    row = db.execute(INVOICE_QUERY + " WHERE i.id=?", (invoice_id,)).fetchone()
    if row is None:
        abort(404)
    if actor["role"] == "tenant":
        allowed = actor.get("stay_id") == row["stay_id"]
    else:
        allowed = scoped_house(actor, row["house_id"])
    if not allowed:
        abort(404)
    return row


def invoice_status(invoice, today):
    if invoice["void_id"]:
        return "Đã hủy"
    if invoice["paid_vnd"] >= invoice["total_vnd"]:
        return "Đã thanh toán"
    if invoice["due_on"] < today:
        return "Quá hạn" if not invoice["paid_vnd"] else "Trả một phần · Quá hạn"
    return "Chưa thanh toán" if not invoice["paid_vnd"] else "Đã trả một phần"


def invoice_month_bounds(lease, resident):
    end_dates = [day for day in (lease["ends_on"], resident["ended_on"]) if day]
    return lease["starts_on"][:7], min(end_dates)[:7] if end_dates else None


def ticket_revision(ticket):
    return digest(json.dumps((ticket["status"], ticket["resolution"])).encode())


def read_maintenance_fields():
    subject = read_text("subject", "tiêu đề", 100)
    description = read_text("description", "mô tả", 1500)
    category = request.form.get("category", "")
    if category not in TICKET_CATEGORIES:
        raise FormError("Chọn loại sự cố.")
    return category, subject, description


def insert_maintenance_ticket(db, stay_id, fields):
    active = db.execute("SELECT 1 FROM stays WHERE id=? AND ended_at IS NULL", (stay_id,)).fetchone()
    if not active:
        abort(403)
    ticket = db.execute("INSERT INTO rental_tickets(stay_id,category,subject,description) VALUES (?, ?, ?, ?)",
                        (stay_id, *fields))
    return ticket.lastrowid


def create_operations_blueprint(store):
    bp = Blueprint("operations", __name__, url_prefix="/operations")

    @bp.app_template_filter("rental_money")
    def format_money(amount):
        return f"{amount:,}".replace(",", ".") + " ₫"

    @bp.get("")
    def index():
        actor = require_roles("owner", "manager", "tenant", "technician")
        role = actor["role"]
        today = local_today()
        default_month = "" if role == "tenant" else today[:7]
        month = request.args.get("month", default_month)
        if month and not MONTH_PATTERN.fullmatch(month):
            abort(400)
        houses, house_choices, stays, invoices, tickets, notices = [], [], [], [], [], []
        resident, lease = None, None
        with store.connect() as db:
            if role == "tenant":
                resident = db.execute(STAY_QUERY + " WHERE s.id=?", (actor.get("stay_id"),)).fetchone()
                if resident is None:
                    abort(404)
                lease = db.execute("SELECT * FROM rental_leases WHERE stay_id=?", (resident["id"],)).fetchone()
                invoices = db.execute(
                    INVOICE_QUERY + " WHERE i.stay_id=? AND (?='' OR i.month=?) ORDER BY i.month DESC",
                    (resident["id"], month, month),
                ).fetchall()
                tickets = db.execute(
                    "SELECT * FROM rental_tickets WHERE stay_id=? ORDER BY id DESC", (resident["id"],)
                ).fetchall()
                if not resident["ended_at"]:
                    notices = db.execute("""SELECT n.*, h.name AS house_name FROM rental_notices AS n
                                            JOIN houses AS h ON h.id=n.house_id
                                            WHERE n.house_id=? AND n.archived_at IS NULL ORDER BY n.id DESC""",
                                         (resident["house_id"],)).fetchall()
            else:
                house_id = actor.get("house_id")
                if role == "owner":
                    selected = request.args.get("house", type=int)
                    if selected is not None and not scoped_house(actor, selected):
                        abort(404)
                    house_id = selected if selected is not None else house_id
                if role != "owner" and house_id is None:
                    abort(403)
                houses = db.execute("SELECT id, name FROM houses WHERE (? IS NULL OR id=?) ORDER BY name",
                                    (house_id, house_id)).fetchall()
                if role == "owner" and current_app.config.get('RENTAL_HOUSE_A_ID') is None:
                    house_choices = db.execute("SELECT id, name FROM houses ORDER BY name").fetchall()
                tickets = db.execute(
                    """SELECT t.*, h.name AS house_name, r.code FROM rental_tickets AS t
                       JOIN stays AS s ON s.id=t.stay_id JOIN rooms AS r ON r.id=s.room_id
                       JOIN houses AS h ON h.id=r.house_id WHERE (? IS NULL OR h.id=?)
                       ORDER BY t.status='resolved', t.id DESC""", (house_id, house_id),
                ).fetchall()
                if role in OPERATORS:
                    stays = db.execute(
                        STAY_QUERY + """ WHERE (? IS NULL OR h.id=?) AND (s.ended_at IS NULL OR EXISTS (
                                            SELECT 1 FROM rental_leases AS l WHERE l.stay_id=s.id
                                        )) ORDER BY s.ended_at IS NOT NULL,h.name,r.code,s.name""",
                        (house_id, house_id),
                    ).fetchall()
                    invoices = db.execute(
                        INVOICE_QUERY + " WHERE (? IS NULL OR h.id=?) AND (?='' OR i.month=?) ORDER BY i.month DESC,i.id DESC",
                        (house_id, house_id, month, month),
                    ).fetchall()
                    notices = db.execute("""SELECT n.*, h.name AS house_name FROM rental_notices AS n
                                            JOIN houses AS h ON h.id=n.house_id
                                            WHERE (? IS NULL OR h.id=?) AND n.archived_at IS NULL ORDER BY n.id DESC""",
                                         (house_id, house_id)).fetchall()

        active_invoices = [invoice for invoice in invoices if not invoice["void_id"]]
        totals = {"billed": sum(row["total_vnd"] for row in active_invoices),
                  "paid": sum(row["paid_vnd"] for row in active_invoices),
                  "outstanding": sum(row["total_vnd"] - row["paid_vnd"] for row in active_invoices)}
        return render_template(
            "operations_index.html", actor=actor, role=role, today=today, month=month, houses=houses,
            house_choices=house_choices, selected_house=request.args.get("house", type=int), stays=stays,
            invoices=invoices, totals=totals,
            tickets=tickets, ticket_categories=TICKET_CATEGORIES, ticket_states=TICKET_STATES,
            ticket_revision=ticket_revision,
            notices=notices, resident=resident, lease=lease, invoice_status=invoice_status,
            voice_repair_available="tenant_repair" in current_app.blueprints,
        )

    @bp.route("/leases/<int:stay_id>", methods=["GET", "POST"])
    def lease_terms(stay_id):
        actor = require_roles(*OPERATORS)
        if request.method == "POST":
            try:
                reference = read_text("reference", "mã hợp đồng", 80)
                starts_on = read_date("starts_on", "ngày bắt đầu")
                ends_on = read_date("ends_on", "ngày kết thúc", optional=True)
                if ends_on and ends_on < starts_on:
                    raise FormError("Ngày kết thúc phải bằng hoặc sau ngày bắt đầu.")
                rent = read_number("rent_vnd", "tiền thuê", blank=None)
                deposit = read_number("deposit_vnd", "tiền cọc")
                electricity_rate = read_number("electricity_rate", "đơn giá điện")
                water_rate = read_number("water_rate", "đơn giá nước")
                notes = read_text("notes", "ghi chú", 1000, required=False)
                with store.write() as db:
                    visible_stay(db, actor, stay_id, active=True)
                    db.execute(
                        """INSERT INTO rental_leases
                           (stay_id, reference, starts_on, ends_on, rent_vnd, deposit_vnd, electricity_rate, water_rate, notes)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(stay_id) DO UPDATE SET reference=excluded.reference, starts_on=excluded.starts_on,
                               ends_on=excluded.ends_on, rent_vnd=excluded.rent_vnd, deposit_vnd=excluded.deposit_vnd,
                               electricity_rate=excluded.electricity_rate, water_rate=excluded.water_rate,
                               notes=excluded.notes, updated_at=CURRENT_TIMESTAMP""",
                        (stay_id, reference, starts_on, ends_on, rent, deposit, electricity_rate, water_rate, notes),
                    )
            except FormError as error:
                flash(str(error), "error")
            else:
                flash("Đã lưu điều khoản thuê.", "success")
            return redirect(url_for("operations.lease_terms", stay_id=stay_id), code=303)

        reissue_id = request.args.get("reissue", type=int)
        with store.connect() as db:
            resident = visible_stay(db, actor, stay_id)
            lease = db.execute("SELECT * FROM rental_leases WHERE stay_id=?", (stay_id,)).fetchone()
            reissue = visible_invoice(db, actor, reissue_id) if reissue_id is not None else None
            if reissue and (reissue["stay_id"] != stay_id or not reissue["void_id"]):
                abort(404)
        first_month, last_month = invoice_month_bounds(lease, resident) if lease else (None, None)
        default_month = None
        if first_month and (last_month is None or first_month <= last_month):
            default_month = max(local_today()[:7], first_month)
            if last_month:
                default_month = min(default_month, last_month)
        return render_template("operations_lease.html", resident=resident, lease=lease,
                               reissue=reissue, default_month=default_month, first_month=first_month,
                               last_month=last_month)

    @bp.post("/leases/<int:stay_id>/invoices")
    def issue_invoice(stay_id):
        actor = require_roles(*OPERATORS)
        reissue_id = request.form.get("reissue_id", type=int)
        if "reissue_id" in request.form and reissue_id is None:
            abort(400)
        try:
            month = request.form.get("month", "").strip()
            if not MONTH_PATTERN.fullmatch(month) or int(month[:4]) < 1900:
                raise FormError("Kiểm tra lại kỳ hóa đơn.")
            due_on = read_date("due_on", "hạn thanh toán")
            if due_on[:7] < month:
                raise FormError("Hạn thanh toán phải nằm trong hoặc sau kỳ hóa đơn.")
            electricity_start = read_number("electricity_start", "chỉ số điện đầu kỳ")
            electricity_end = read_number("electricity_end", "chỉ số điện cuối kỳ", blank=electricity_start)
            water_start = read_number("water_start", "chỉ số nước đầu kỳ")
            water_end = read_number("water_end", "chỉ số nước cuối kỳ", blank=water_start)
            other = read_number("other_vnd", "phí khác")
            notes = read_text("notes", "diễn giải hóa đơn", 500, required=False)
            if electricity_end < electricity_start or water_end < water_start:
                raise FormError("Chỉ số cuối kỳ phải bằng hoặc lớn hơn chỉ số đầu kỳ.")
            with store.write() as db:
                resident = visible_stay(db, actor, stay_id)
                lease = db.execute("SELECT * FROM rental_leases WHERE stay_id=?", (stay_id,)).fetchone()
                if lease is None:
                    raise FormError("Hãy lưu điều khoản thuê trước khi lập hóa đơn.")
                existing = db.execute(INVOICE_QUERY + " WHERE i.room_id=? AND i.month=?",
                                      (resident["room_id"], month)).fetchone()
                if reissue_id is not None:
                    if (not existing or existing["id"] != reissue_id or existing["stay_id"] != stay_id
                            or not existing["void_id"]
                            or str(existing["void_id"]) != request.form.get("reissue_void_id")):
                        raise FormError("Hóa đơn đã thay đổi. Mở lại chi tiết hóa đơn trước khi lập lại.")
                    rent = read_number("rent_vnd", "tiền thuê", blank=None)
                    electricity_rate = read_number("electricity_rate", "đơn giá điện", blank=None)
                    water_rate = read_number("water_rate", "đơn giá nước", blank=None)
                else:
                    if existing is not None:
                        message = ("Phòng này đã có hóa đơn đã hủy cho kỳ đã chọn. Mở chi tiết để lập lại."
                                   if existing["void_id"] else "Phòng này đã có hóa đơn cho kỳ đã chọn.")
                        raise FormError(message)
                    first_month, last_month = invoice_month_bounds(lease, resident)
                    if month < first_month or (last_month and month > last_month):
                        raise FormError("Kỳ hóa đơn phải thuộc thời gian thuê đã ghi nhận.")
                    rent, electricity_rate, water_rate = lease["rent_vnd"], lease["electricity_rate"], lease["water_rate"]
                total = (rent + (electricity_end - electricity_start) * electricity_rate
                         + (water_end - water_start) * water_rate + other)
                if not 0 < total <= MONEY_LIMIT:
                    raise FormError("Tổng tiền hóa đơn phải lớn hơn 0 và không vượt 1 tỷ đồng.")
                terms = (due_on, rent, electricity_start, electricity_end,
                         electricity_rate, water_start, water_end, water_rate, other, notes, total)
                if reissue_id is None:
                    result = db.execute(
                        """INSERT INTO rental_invoices
                           (stay_id, room_id, month, due_on, rent_vnd, electricity_start, electricity_end, electricity_rate,
                            water_start, water_end, water_rate, other_vnd, notes, total_vnd)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (stay_id, resident["room_id"], month, *terms),
                    )
                    invoice_id = result.lastrowid
                else:
                    invoice_id = existing["id"]
                    db.execute(
                        """UPDATE rental_invoices SET due_on=?,rent_vnd=?,electricity_start=?,electricity_end=?,
                               electricity_rate=?,water_start=?,water_end=?,water_rate=?,other_vnd=?,notes=?,total_vnd=?,
                               created_at=CURRENT_TIMESTAMP WHERE id=?""", (*terms, invoice_id),
                    )
                    db.execute("UPDATE rental_invoice_voids SET reissued_at=CURRENT_TIMESTAMP WHERE id=?",
                               (existing["void_id"],))
        except FormError as error:
            flash(str(error), "error")
            if reissue_id is not None:
                return redirect(url_for("operations.invoice_detail", invoice_id=reissue_id), code=303)
            return redirect(url_for("operations.lease_terms", stay_id=stay_id), code=303)
        flash("Đã lập lại hóa đơn." if reissue_id is not None else "Đã lập hóa đơn.", "success")
        return redirect(url_for("operations.invoice_detail", invoice_id=invoice_id), code=303)

    @bp.get("/invoices/<int:invoice_id>")
    def invoice_detail(invoice_id):
        actor = require_roles(*OPERATORS, "tenant")
        today = local_today()
        with store.connect() as db:
            db.execute("BEGIN")
            invoice = visible_invoice(db, actor, invoice_id)
            payments = db.execute(
                """SELECT p.*, pv.reason AS void_reason, pv.voided_by, pv.voided_at
                   FROM rental_payments AS p LEFT JOIN rental_payment_voids AS pv ON pv.payment_id=p.id
                   WHERE p.invoice_id=? ORDER BY p.paid_on DESC,p.id DESC""", (invoice_id,),
            ).fetchall()
            revisions = [dict(row) for row in db.execute(
                "SELECT * FROM rental_invoice_voids WHERE invoice_id=? ORDER BY id", (invoice_id,)
            )]
        for revision in revisions:
            revision["snapshot"] = json.loads(revision["snapshot"])
        return render_template("operations_invoice.html", invoice=invoice, payments=payments, today=today,
                               status=invoice_status(invoice, today), may_record=actor["role"] in OPERATORS,
                               payment_methods=PAYMENT_METHODS, request_key=secrets.token_hex(16), revisions=revisions)

    @bp.post("/invoices/<int:invoice_id>/payments")
    def record_payment(invoice_id):
        actor = require_roles(*OPERATORS)
        try:
            amount = read_number("amount_vnd", "số tiền đã nhận", minimum=1, blank=None)
            paid_on = read_date("paid_on", "ngày nhận tiền")
            request_key = request.form.get("request_key", "")
            method = request.form.get("method", "")
            note = read_text("note", "ghi chú", 500, required=False)
            if paid_on > local_today() or method not in PAYMENT_METHODS or not re.fullmatch(r"[0-9a-f]{32}", request_key):
                raise FormError("Kiểm tra lại ngày và hình thức nhận tiền.")
            with store.write() as db:
                invoice = visible_invoice(db, actor, invoice_id)
                if invoice["void_id"] or str(invoice["issue_number"]) != request.form.get("issue_number"):
                    raise FormError("Hóa đơn đã thay đổi. Tải lại hóa đơn trước khi ghi nhận khoản thu.")
                existing = db.execute(
                    """SELECT p.amount_vnd, p.paid_on, p.method, p.note, pv.payment_id AS voided
                       FROM rental_payments AS p LEFT JOIN rental_payment_voids AS pv ON pv.payment_id=p.id
                       WHERE p.invoice_id=? AND p.request_key=?""", (invoice_id, request_key),
                ).fetchone()
                payment = (amount, paid_on, method, note)
                if existing is not None:
                    if existing["voided"] or tuple(existing)[:4] != payment:
                        raise FormError("Lần ghi nhận này đã có. Tải lại hóa đơn để nhập giao dịch mới.")
                else:
                    if amount > invoice["total_vnd"] - invoice["paid_vnd"]:
                        raise FormError("Số tiền nhận vượt phần còn phải thu.")
                    db.execute(
                        """INSERT INTO rental_payments(invoice_id,request_key,amount_vnd,paid_on,method,note,recorded_by)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""", (invoice_id, request_key, *payment, actor["username"]),
                    )
        except FormError as error:
            flash(str(error), "error")
        else:
            flash("Đã ghi nhận khoản thu.", "success")
        return redirect(url_for("operations.invoice_detail", invoice_id=invoice_id), code=303)

    @bp.post("/invoices/<int:invoice_id>/payments/<int:payment_id>/void")
    def void_payment(invoice_id, payment_id):
        actor = require_roles(*OPERATORS)
        try:
            reason = read_text("reason", "lý do hủy khoản thu", 500)
            with store.write() as db:
                visible_invoice(db, actor, invoice_id)
                if not db.execute("SELECT 1 FROM rental_payments WHERE id=? AND invoice_id=?",
                                  (payment_id, invoice_id)).fetchone():
                    abort(404)
                result = db.execute(
                    """INSERT INTO rental_payment_voids(payment_id,reason,voided_by) VALUES (?,?,?)
                       ON CONFLICT(payment_id) DO NOTHING""", (payment_id, reason, actor["username"]),
                )
                if not result.rowcount:
                    raise FormError("Khoản thu này đã hủy.")
        except FormError as error:
            flash(str(error), "error")
        else:
            flash("Đã hủy ghi nhận khoản thu. Lịch sử khoản thu vẫn được lưu.", "success")
        return redirect(url_for("operations.invoice_detail", invoice_id=invoice_id), code=303)

    @bp.post("/invoices/<int:invoice_id>/void")
    def void_invoice(invoice_id):
        actor = require_roles(*OPERATORS)
        try:
            reason = read_text("reason", "lý do hủy hóa đơn", 500)
            with store.write() as db:
                invoice = visible_invoice(db, actor, invoice_id)
                if invoice["void_id"]:
                    raise FormError("Hóa đơn này đã hủy.")
                if str(invoice["issue_number"]) != request.form.get("issue_number"):
                    raise FormError("Hóa đơn đã thay đổi. Tải lại hóa đơn trước khi hủy.")
                if invoice["paid_vnd"]:
                    raise FormError("Hóa đơn vẫn có khoản thu hiệu lực. Hãy kiểm tra lại các khoản thu trước khi hủy.")
                snapshot = {key: invoice[key] for key in (*INVOICE_TERMS, "created_at", "issue_number", "month")}
                db.execute(
                    "INSERT INTO rental_invoice_voids(invoice_id,snapshot,reason,voided_by) VALUES (?,?,?,?)",
                    (invoice_id, json.dumps(snapshot, ensure_ascii=False), reason, actor["username"]),
                )
        except FormError as error:
            flash(str(error), "error")
        else:
            flash("Đã hủy hóa đơn. Có thể lập lại cùng kỳ từ hồ sơ người thanh toán ban đầu.", "success")
        return redirect(url_for("operations.invoice_detail", invoice_id=invoice_id), code=303)

    @bp.post("/tickets")
    def create_ticket():
        actor = require_roles("tenant")
        try:
            fields = read_maintenance_fields()
            with store.write() as db:
                insert_maintenance_ticket(db, actor.get("stay_id"), fields)
        except FormError as error:
            flash(str(error), "error")
        else:
            flash("Đã gửi yêu cầu bảo trì.", "success")
        return redirect(url_for("operations.index", _anchor="maintenance"), code=303)

    @bp.post("/tickets/<int:ticket_id>")
    def update_ticket(ticket_id):
        actor = require_roles(*TICKET_HANDLERS)
        try:
            status = request.form.get("status", "")
            if status not in TICKET_STATES:
                raise FormError("Chọn trạng thái xử lý.")
            resolution = read_text("resolution", "phản hồi bảo trì", 1500, required=status == "resolved")
            with store.write() as db:
                ticket = db.execute("""SELECT t.status, t.resolution, r.house_id FROM rental_tickets AS t
                                       JOIN stays AS s ON s.id=t.stay_id JOIN rooms AS r ON r.id=s.room_id
                                       WHERE t.id=?""", (ticket_id,)).fetchone()
                if ticket is None or not scoped_house(actor, ticket["house_id"]):
                    abort(404)
                revision = request.form.get("revision")
                if revision is not None and revision != ticket_revision(ticket):
                    raise FormError("Yêu cầu vừa được cập nhật. Kiểm tra trạng thái và phản hồi mới trước khi lưu.")
                if actor["role"] == "technician" and (status == "open" or ticket["status"] == "resolved"):
                    abort(403)
                db.execute("""UPDATE rental_tickets SET status=?, resolution=?, updated_by=?, updated_at=CURRENT_TIMESTAMP
                             WHERE id=?""", (status, resolution, actor["username"], ticket_id))
        except FormError as error:
            flash(str(error), "error")
        else:
            flash("Đã cập nhật yêu cầu bảo trì.", "success")
        return redirect(url_for("operations.index", _anchor="maintenance"), code=303)

    @bp.post("/notices")
    def publish_notice():
        actor = require_roles(*OPERATORS)
        try:
            house_id = current_app.config.get('RENTAL_HOUSE_A_ID')
            if house_id is None or 'house_id' in request.form:
                house_id = read_number("house_id", "dãy trọ", minimum=1, blank=None)
            title = read_text("title", "tiêu đề", 100)
            body = read_text("body", "nội dung", 2000)
            with store.write() as db:
                if not scoped_house(actor, house_id) or not db.execute("SELECT 1 FROM houses WHERE id=?", (house_id,)).fetchone():
                    abort(404)
                db.execute("INSERT INTO rental_notices(house_id,title,body) VALUES (?, ?, ?)", (house_id, title, body))
        except FormError as error:
            flash(str(error), "error")
        else:
            flash("Đã đăng thông báo cho dãy trọ.", "success")
        return redirect(url_for("operations.index", _anchor="notices"), code=303)

    @bp.post("/notices/<int:notice_id>/archive")
    def archive_notice(notice_id):
        actor = require_roles(*OPERATORS)
        with store.write() as db:
            notice = db.execute("SELECT house_id FROM rental_notices WHERE id=? AND archived_at IS NULL",
                                (notice_id,)).fetchone()
            if notice is None or not scoped_house(actor, notice["house_id"]):
                abort(404)
            db.execute("UPDATE rental_notices SET archived_at=CURRENT_TIMESTAMP WHERE id=?", (notice_id,))
        flash("Đã ẩn thông báo.", "success")
        return redirect(url_for("operations.index", _anchor="notices"), code=303)

    return bp
