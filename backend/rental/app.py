import hmac
import io
import os
import re
import secrets
import sqlite3
from datetime import timedelta
from pathlib import Path
from threading import Lock
from time import monotonic, time
from unicodedata import normalize

from dotenv import load_dotenv
from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for
from PIL import Image, ImageOps
from requests import RequestException
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import check_password_hash

from rental.accounts import MAX_RECORD_ID, ROLES, create_accounts_blueprint, initialize_accounts, lookup_account, require_roles
from rental.flash import FLASH_FIELDS, FLASH_JOIN, flash_status, get_web_visit, initialize_flash, queue_web_flash
from rental.store import RentalStore, digest, format_time
from rental.face_profiles import FaceProfiles, MAX_SAMPLES


ROOT = Path(__file__).resolve().parents[1]
DEVICE_ID = re.compile(r"[A-Za-z0-9_-]{1,48}")
MAX_BYTES = 512 * 1024
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_WEB_REQUEST_BYTES = MAX_UPLOAD_BYTES + 32 * 1024
MAX_FACE_REQUEST_BYTES = 1024 * 1024  # JPEG payload accepted by face_api.py.


def open_store():
    load_dotenv(ROOT / ".env.rental")
    store = RentalStore(ROOT / os.getenv("RENTAL_DB_PATH", "data/rental.db"))
    store.initialize()
    return store


def clean_jpeg(payload):
    with Image.open(io.BytesIO(payload), formats=["JPEG"]) as photo:
        if photo.width > 1600 or photo.height > 1200:
            raise ValueError("Image dimensions exceed the limit")
        ImageOps.exif_transpose(photo, in_place=True)
        photo.thumbnail((1600, 1200))
        result = io.BytesIO()
        photo.convert("RGB").save(result, format="JPEG", quality=85)
        return result.getvalue()


def read_enrollment_photo(file):
    if file is None:
        raise ValueError("Missing photo")
    payload = file.stream.read(MAX_UPLOAD_BYTES + 1)
    if len(payload) > MAX_UPLOAD_BYTES:
        raise RequestEntityTooLarge()
    with Image.open(io.BytesIO(payload), formats=["JPEG", "PNG", "WEBP"]) as source:
        if source.width * source.height > 24_000_000:
            raise ValueError("Photo exceeds pixel limit")
        photo = ImageOps.exif_transpose(source).convert("RGB")
        for bounds in ((1600, 1200), (1280, 960), (1024, 768), (800, 600)):
            photo.thumbnail(bounds)
            if min(photo.size) < 32:
                break
            result = io.BytesIO()
            photo.save(result, "JPEG", quality=85, optimize=True)
            if result.tell() <= MAX_FACE_REQUEST_BYTES:
                return result.getvalue()
        raise ValueError("Photo cannot fit the recognition service image limits")


def single_house_enabled():
    return os.getenv("RENTAL_SINGLE_HOUSE", "").strip().lower() in {"1", "true", "yes", "on"}


def comparable_resident_name(name):
    return " ".join(normalize("NFC", name).casefold().split())


def create_app(store=None, admin_password=None, *, single_house=None):
    from rental.hermes_lookup import create_lookup_blueprint
    from rental.hermes_voice import load_assistant_turns
    from rental.intercom import create_intercom_blueprint, incoming_visits, initialize_intercom, paired_device_ids
    from rental.live import create_live_blueprint
    from rental.operations import create_operations_blueprint, initialize_operations
    from rental.tenant_repair import create_tenant_repair_blueprint

    store = store if store is not None else open_store()
    password = admin_password if admin_password is not None else os.getenv("RENTAL_ADMIN_PASSWORD", "")
    if len(password) < 16:
        raise ValueError("Set RENTAL_ADMIN_PASSWORD to a strong password of at least 16 characters.")
    profiles = FaceProfiles()
    if single_house is None:
        single_house = single_house_enabled()
    house_a_id = store.ensure_house_a() if single_house else None
    initialize_accounts(store)
    initialize_operations(store)
    initialize_flash(store)
    initialize_intercom(store)
    session_secret = os.getenv('RENTAL_SESSION_SECRET', '')
    if session_secret and len(session_secret) < 32:
        raise ValueError('RENTAL_SESSION_SECRET must be at least 32 characters.')
    signing_key = session_secret.encode() if session_secret else secrets.token_bytes(32)
    owner_stamp = hmac.digest(signing_key, b'rental-owner\0' + password.encode(), 'sha256').hex()
    login_attempts = {}
    attempt_lock = Lock()

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=signing_key,
        RENTAL_HOUSE_A_ID=house_a_id,
        MAX_CONTENT_LENGTH=MAX_BYTES,
        SESSION_COOKIE_NAME="rental_session",
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("RENTAL_COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )
    app.jinja_env.filters["visit_time"] = format_time
    app.jinja_env.globals['role_labels'] = ROLES
    app.register_blueprint(create_accounts_blueprint(store))
    app.register_blueprint(create_operations_blueprint(store))
    app.register_blueprint(create_lookup_blueprint(store))
    app.register_blueprint(create_tenant_repair_blueprint(store))
    app.register_blueprint(create_live_blueprint(store))
    app.register_blueprint(create_intercom_blueprint(store))

    def landing():
        return url_for('dashboard' if g.actor and g.actor['role'] in {'owner','manager'} else 'operations.index')

    @app.before_request
    def protect_dashboard():
        g.actor = None
        g.house_a_id = house_a_id
        endpoint = request.endpoint
        if endpoint in {None, 'static', 'health', 'receive', 'live.device_demand', 'live.device_frame', 'live.device_flash_ack',
                        'intercom.station', 'intercom.station_pair', 'intercom.station_unpair', 'intercom.station_state',
                        'intercom.station_audio', 'intercom.station_clip', 'intercom.station_signal',
                        'intercom.station_assistant', 'intercom.station_assistant_speech'}:
            return
        if hmac.compare_digest(session.get('owner_stamp', ''), owner_stamp):
            g.actor = {'id': None, 'role': 'owner', 'username': 'chu-tro', 'display_name': 'Chủ trọ',
                       'house_id': house_a_id, 'stay_id': None}
        elif session.get('account_id') is not None:
            account = lookup_account(store, account_id=session['account_id'], house_id=house_a_id)
            if account and account['version'] == session.get('account_version'):
                g.actor = {key: account[key] for key in ('id','role','username','display_name','house_id','stay_id')}
            else:
                session.clear()
        if endpoint not in {'login', 'accounts.register'} and not g.actor:
            return redirect(url_for('login'))
        if request.method not in {'GET','HEAD','OPTIONS'}:
            if endpoint == 'face_samples':
                request.max_content_length = MAX_WEB_REQUEST_BYTES
            expected = session.get("csrf", "")
            supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf', '')
            if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
                abort(403)

    @app.after_request
    def restrict_response(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store, no-transform" if request.endpoint == "live.stream" else "no-store"
        return response

    @app.errorhandler(sqlite3.OperationalError)
    def storage_error(error):
        app.logger.error("Rental storage is unavailable: %s", type(error).__name__)
        return {"error": "storage_unavailable"}, 503

    @app.errorhandler(RequestEntityTooLarge)
    def image_too_large(error):
        if request.endpoint == "face_samples":
            flash("Ảnh tải lên vượt quá 8 MB.", "error")
            return redirect(url_for("face_samples", stay_id=request.view_args["stay_id"]), code=303)
        return {"error": "payload_too_large"}, 413

    @app.get("/healthz")
    def health():
        with store.connect() as db:
            db.execute("SELECT id FROM visits LIMIT 1").fetchone()
        return {"status": "ok"}

    @app.post("/api/device/doorbell")
    def receive():
        device_id = request.headers.get("X-Device-ID", "")
        event_id = request.headers.get("X-Event-ID", "")
        if not DEVICE_ID.fullmatch(device_id) or not re.fullmatch(re.escape(device_id) + r"-[0-9a-f]{16}", event_id):
            return {"error": "invalid_device_or_event_id"}, 400
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not 32 <= len(token) <= 128:
            return {"error": "unauthorized"}, 401
        token_hash = digest(token.encode())
        if not store.authenticates(device_id, token_hash, house_a_id):
            return {"error": "unauthorized"}, 401
        if request.mimetype != "image/jpeg":
            return {"error": "expected_jpeg"}, 415
        payload = request.get_data(cache=False)
        try:
            jpeg = clean_jpeg(payload)
        except (OSError, ValueError, Image.DecompressionBombError):
            return {"error": "invalid_jpeg"}, 400
        status, state = store.receive(device_id, token_hash, event_id, jpeg, digest(payload))
        if status >= 400:
            headers = {"Retry-After": "3"} if status == 429 else {}
            return {"error": state}, status, headers
        return {"accepted": True, "event_id": event_id, "state": state}, status

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.actor:
            return redirect(landing())
        if request.method == "POST":
            username = request.form.get('username', '').strip().lower() or 'chu-tro'
            supplied = request.form.get('password', '')
            if len(username) > 32 or (username != 'chu-tro' and len(supplied) > 128):
                return render_template('login.html', invalid=True), 401
            key = (request.remote_addr, username)
            now = monotonic()
            with attempt_lock:
                if len(login_attempts) >= 1024:
                    for expired in [k for k, (until, _) in login_attempts.items() if until <= now]:
                        del login_attempts[expired]
                    if len(login_attempts) >= 1024 and key not in login_attempts:
                        return render_template('login.html', throttled=True), 429
                until, count = login_attempts.get(key, (0, 0))
                if until <= now:
                    until, count = now + 120, 0
                if count >= 10:
                    return render_template('login.html', throttled=True), 429
                login_attempts[key] = (until, count + 1)
            if username == 'chu-tro' and hmac.compare_digest(password.encode(), supplied.encode()):
                with attempt_lock:
                    login_attempts.pop(key, None)
                session.clear()
                session.update(owner_stamp=owner_stamp, csrf=secrets.token_urlsafe(24))
                session.permanent = True
                return redirect(url_for("dashboard"), code=303)
            account = lookup_account(store, username=username, house_id=house_a_id) if username != 'chu-tro' else None
            if account and check_password_hash(account['password_hash'], supplied):
                with attempt_lock:
                    login_attempts.pop(key, None)
                session.clear()
                session.update(account_id=account['id'], account_version=account['version'], csrf=secrets.token_urlsafe(24))
                session.permanent = True
                endpoint = 'dashboard' if account['role'] == 'manager' else 'operations.index'
                return redirect(url_for(endpoint), code=303)
            return render_template("login.html", invalid=True, username=request.form.get('username', '')[:32]), 401
        session.setdefault("csrf", secrets.token_urlsafe(24))
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"), code=303)

    @app.get('/intercom/incoming')
    def intercom_incoming():
        scope = require_roles('owner', 'manager')['house_id']
        with store.connect() as db:
            visits = incoming_visits(db, scope, paired_device_ids(db))
        return render_template('intercom_incoming.html', incoming=visits)

    @app.get("/")
    def dashboard():
        if g.actor['role'] in {'tenant','technician'}:
            return redirect(url_for('operations.index'))
        actor = require_roles('owner','manager')
        selected = request.args.get('house', type=int)
        scope = actor['house_id']
        if selected is not None and scope is not None and selected != scope:
            abort(404)
        house_id = selected if selected is not None else scope
        with store.connect() as db:
            houses = db.execute('SELECT * FROM houses WHERE (? IS NULL OR id=?) ORDER BY name', (scope,scope)).fetchall()
            rooms = db.execute("""SELECT rooms.id, rooms.code, houses.name, houses.id AS house_id
                                  FROM rooms JOIN houses ON houses.id = rooms.house_id
                                  WHERE (? IS NULL OR houses.id=?) ORDER BY houses.name, rooms.code""", (scope,scope)).fetchall()
            occupants = db.execute('''SELECT s.id,s.room_id,s.name FROM stays s JOIN rooms r ON r.id=s.room_id
                                    WHERE s.ended_at IS NULL AND (? IS NULL OR r.house_id=?) ORDER BY s.name''', (scope,scope)).fetchall()
            devices = db.execute('SELECT id,house_id FROM devices WHERE (? IS NULL OR house_id=?) ORDER BY id', (scope,scope)).fetchall()
            paired = paired_device_ids(db)
            intercom_configured = any(device['id'] in paired for device in devices)
            incoming = incoming_visits(db, scope, paired)
            visits = db.execute(
                f"""SELECT v.id, event_id, received_at, description, state, analysis_status,
                           notification_status, h.name, device_id, {FLASH_FIELDS}
                    FROM visits v JOIN houses h ON h.id=v.house_id {FLASH_JOIN}
                    WHERE (? IS NULL OR v.house_id=?) ORDER BY v.id DESC LIMIT 50""", (house_id, house_id),
            ).fetchall()
            assistant_turns = load_assistant_turns(db, [visit['id'] for visit in visits])
        now = int(time())
        visits = [
            dict(visit) | {
                'flash_state': flash_status(visit, now),
                'assistant_turns': assistant_turns.get(visit['id'], []),
            }
            for visit in visits
        ]
        return render_template("dashboard.html", houses=houses, rooms=rooms, occupants=occupants,
                               devices=devices, visits=visits, house_id=house_id,
                               intercom_configured=intercom_configured, incoming=incoming)

    @app.route('/visits/<int:visit_id>/flash', methods=['GET', 'POST'])
    def visit_flash(visit_id):
        actor = require_roles('owner','manager')
        if visit_id > 2**63 - 1:
            abort(404)
        if request.method == 'GET':
            with store.connect() as db:
                row = get_web_visit(db, visit_id, actor)
            if row is None:
                abort(404)
            return {'status': flash_status(row, int(time()))}
        result = queue_web_flash(store, visit_id, actor)
        if result == 'denied':
            abort(404)
        if request.accept_mimetypes.best_match(('text/html', 'application/json')) == 'application/json':
            return {'status': result}, {'queued': 202, 'unavailable': 409}.get(result, 200)
        messages = {'queued': 'Đã gửi yêu cầu mở cửa mô phỏng.',
                    'existing': 'Lượt chuông này đã có yêu cầu mở cửa mô phỏng.',
                    'confirmed': 'ESP32 đã xác nhận bật đèn cho lượt chuông này.',
                    'unavailable': 'Lượt chuông đã hết hạn hoặc có lượt mới hơn.'}
        flash(messages[result], 'error' if result == 'unavailable' else 'success')
        return redirect(url_for('dashboard', _anchor=f'visit-{visit_id}'), code=303)

    @app.get("/stays/<int:stay_id>/checkout")
    def checkout_preview(stay_id):
        scope = require_roles('owner', 'manager')['house_id']
        if stay_id > MAX_RECORD_ID:
            abort(404)
        with store.connect() as db:
            resident = db.execute(
                """SELECT s.name, r.code, h.name AS house
                   FROM stays s JOIN rooms r ON r.id=s.room_id JOIN houses h ON h.id=r.house_id
                   WHERE s.id=? AND s.ended_at IS NULL AND (? IS NULL OR h.id=?)""",
                (stay_id, scope, scope),
            ).fetchone()
        if resident is None:
            abort(404)
        return render_template("checkout.html", resident=resident, stay_id=stay_id)

    @app.post("/manage/<action>")
    def manage(action):
        actor = require_roles('owner','manager')
        commands = {
            "house": ("INSERT INTO houses(name) VALUES (?)", ("name",)),
            "room": ("INSERT INTO rooms(house_id, code) VALUES (?, ?)", ("house_id", "code")),
            "stay": ("INSERT INTO stays(room_id, name) VALUES (?, ?)", ("room_id", "name")),
            "checkout": ("UPDATE stays SET ended_at=CURRENT_TIMESTAMP WHERE id=? AND ended_at IS NULL", ("stay_id",)),
            "telegram": ("UPDATE houses SET telegram_chat_id=? WHERE id=?", ("chat_id", "house_id")),
            'rename-house': ('UPDATE houses SET name=? WHERE id=?', ('name','house_id')),
            'rename-room': ('UPDATE rooms SET code=? WHERE id=?', ('code','room_id')),
            'rename-resident': ('UPDATE stays SET name=? WHERE id=? AND ended_at IS NULL', ('name','stay_id')),
        }
        if action not in commands:
            abort(404)
        if house_a_id is not None and action in {'house', 'rename-house'}:
            abort(404)
        if action in {'house','telegram','rename-house'}:
            require_roles('owner')
        sql, keys = commands[action]
        values = tuple(request.form.get(key, str(house_a_id) if key == 'house_id' and house_a_id else '').strip()
                       for key in keys)
        form = dict(zip(keys, values))
        for key, value in zip(keys, values):
            if key == "chat_id":
                valid = not value or re.fullmatch(r"-?[1-9][0-9]{0,15}", value)
            elif key.endswith("_id"):
                valid = re.fullmatch(r"[1-9][0-9]{0,8}", value)
            else:
                valid = 1 <= len(value) <= (32 if key == "code" else 100) and value.isprintable()
            if not valid:
                flash("Vui lòng kiểm tra lại thông tin nhập.", "error")
                return redirect(url_for("dashboard"), code=303)
        try:
            with store.write() as db:
                if actor['house_id'] is not None:
                    scope = actor['house_id']
                    for key, query in {
                        'house_id': 'SELECT 1 FROM houses WHERE id=? AND id=?',
                        'room_id': 'SELECT 1 FROM rooms WHERE id=? AND house_id=?',
                        'stay_id': 'SELECT 1 FROM stays s JOIN rooms r ON r.id=s.room_id WHERE s.id=? AND r.house_id=?',
                    }.items():
                        if key in form and not db.execute(query, (form[key],scope)).fetchone():
                            abort(404)
                if action in {'stay', 'rename-resident'}:
                    stay_id = int(form.get('stay_id', '0'))
                    room_id = form.get('room_id')
                    if action == 'rename-resident':
                        current = db.execute('SELECT room_id FROM stays WHERE id=? AND ended_at IS NULL',
                                             (stay_id,)).fetchone()
                        room_id = current['room_id'] if current else None
                    if room_id is not None:
                        name = comparable_resident_name(form['name'])
                        roommates = db.execute(
                            'SELECT name FROM stays WHERE room_id=? AND ended_at IS NULL AND id!=?',
                            (room_id, stay_id),
                        )
                        if any(comparable_resident_name(row['name']) == name for row in roommates):
                            flash('Phòng này đã có người đang thuê mang tên này.', 'error')
                            return redirect(url_for("dashboard"), code=303)
                changed = db.execute(sql, values).rowcount
        except sqlite3.IntegrityError:
            changed = 0
        flash("Đã lưu." if changed else "Không lưu được. Kiểm tra dữ liệu trùng hoặc mục đã thay đổi.",
              "success" if changed else "error")
        return redirect(url_for("dashboard"), code=303)

    @app.route("/stays/<int:stay_id>/face-samples", methods=["GET", "POST"])
    def face_samples(stay_id):
        scope = require_roles('owner','manager')['house_id']
        with store.connect() as db:
            resident = db.execute(
                """SELECT stays.name, rooms.code, houses.name AS house FROM stays
                   JOIN rooms ON rooms.id=stays.room_id JOIN houses ON houses.id=rooms.house_id
                   WHERE stays.id=? AND stays.ended_at IS NULL AND (? IS NULL OR houses.id=?)""", (stay_id,scope,scope),
            ).fetchone()
            if resident is None:
                abort(404)
            samples = db.execute("SELECT id, created_at FROM face_samples WHERE stay_id=? ORDER BY id", (stay_id,)).fetchall()
        if request.method == "GET":
            return render_template("face_samples.html", resident=resident, stay_id=stay_id, samples=samples,
                                   configured=bool(profiles.url), max_samples=MAX_SAMPLES)
        action = request.form.get("action")
        if action == "delete":
            with store.connect() as db:
                deleted = db.execute("DELETE FROM face_samples WHERE id=? AND stay_id=?",
                                     (request.form.get("sample_id", ""), stay_id)).rowcount
            if deleted:
                flash("Đã xóa mẫu khuôn mặt.", "success")
            else:
                flash("Mẫu khuôn mặt đã bị xóa hoặc không còn trong hồ sơ này.", "error")
        elif action == "enroll" and profiles.url:
            try:
                photo = read_enrollment_photo(request.files.get("photo"))
            except (OSError, ValueError, Image.DecompressionBombError):
                flash("Chọn ảnh JPG, PNG hoặc WebP hợp lệ, mặt rõ và đủ sáng.", "error")
            else:
                try:
                    vectors = profiles.extract(photo)
                except (RequestException, ValueError, TypeError, KeyError):
                    flash("Dịch vụ nhận diện chưa xử lý được ảnh này. Bạn thử lại sau.", "error")
                else:
                    if len(vectors) != 1:
                        flash("Ảnh đăng ký cần có đúng một khuôn mặt.", "error")
                    else:
                        status = store.save_face_sample(stay_id, vectors[0], MAX_SAMPLES)
                        if status == "inactive":
                            abort(404)
                        messages = {"saved": "Đã đăng ký mẫu khuôn mặt.", "duplicate": "Mẫu này đã có.",
                                    "full": "Đã đủ 5 mẫu. Xóa một mẫu trước khi tải ảnh mới."}
                        flash(messages[status], "success" if status == "saved" else "error")
        else:
            abort(400)
        return redirect(url_for("face_samples", stay_id=stay_id), code=303)

    @app.post("/manage/device")
    def device_setup():
        require_roles('owner')
        house_id = request.form.get("house_id", str(house_a_id) if house_a_id else "")
        device_id = request.form.get("device_id", "").strip()
        if not re.fullmatch(r"[1-9][0-9]{0,8}", house_id) or not DEVICE_ID.fullmatch(device_id):
            flash("Kiểm tra lại dãy trọ và mã thiết bị.", "error")
            return redirect(url_for("dashboard"), code=303)
        if house_a_id is not None and int(house_id) != house_a_id:
            abort(404)
        try:
            token = store.register_device(int(house_id), device_id)
        except sqlite3.IntegrityError:
            flash("Dãy trọ không tồn tại hoặc mã thiết bị đã có.", "error")
            return redirect(url_for("dashboard"), code=303)
        return render_template("device_setup.html", device_id=device_id, token=token), 201

    @app.get("/visits/<int:visit_id>/photo")
    def visit_photo(visit_id):
        scope = require_roles('owner','manager')['house_id']
        with store.connect() as db:
            row = db.execute('SELECT jpeg FROM visits WHERE id=? AND (? IS NULL OR house_id=?)', (visit_id,scope,scope)).fetchone()
        if row is None:
            abort(404)
        return Response(row["jpeg"], mimetype="image/jpeg")

    return app
