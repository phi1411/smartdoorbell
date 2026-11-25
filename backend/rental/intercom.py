"""Short, two-way voice messages between a paired entrance and one operator."""

import hmac
import re
import secrets
from dataclasses import dataclass
from threading import Lock, Timer
from time import monotonic, time, time_ns

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, session, url_for

from rental.accounts import MAX_RECORD_ID, require_roles, scoped_house
from rental.flash import flash_status, get_web_visit
from rental.hermes_voice import (
    ASSISTANT_MAX_TURNS,
    DEFAULT_GREETING,
    HermesVoiceAssistant,
    end_assistant_session,
    handoff_seconds,
    initialize_assistant,
)
from rental.intercom_signaling import Signaling, ice_servers
from rental.store import digest


RING_SECONDS = 120
CALL_SECONDS = 300
PAIR_SECONDS = 300
CLIP_SECONDS = 60
CLIP_BYTES = 256 * 1024
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_CLIPS = 96
MAX_CALL_CLIPS = 12
CLIP_INTERVAL = 0.6
PAIR_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
PAIR_PATTERN = re.compile(f'[{PAIR_ALPHABET}]{{8}}')
STATION_COOKIE = 'rental_intercom_station'
STATION_PATH = '/intercom/station'
STATION_COOKIE_AGE = 30 * 24 * 60 * 60
AUTH_TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')
AUDIO_TYPES = {
    'audio/webm': 'audio/webm',
    'audio/ogg': 'audio/ogg',
    'audio/mp4': 'audio/mp4',
    'audio/x-m4a': 'audio/mp4',
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS intercom_stations (
    device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    token_hash TEXT UNIQUE,
    pair_code_hash TEXT UNIQUE,
    pair_until INTEGER
);
CREATE TABLE IF NOT EXISTS intercom_calls (
    visit_id INTEGER PRIMARY KEY REFERENCES visits(id) ON DELETE CASCADE,
    accepted_at INTEGER NOT NULL,
    ended_at INTEGER,
    answered_by TEXT NOT NULL,
    operator_hash TEXT NOT NULL
);
"""

VISIT_COLUMNS = """
    SELECT v.id, v.device_id, v.house_id, v.received_at, v.recipient_chat_id, h.name AS house_name,
           s.token_hash IS NOT NULL AS paired, c.accepted_at, c.ended_at, c.operator_hash,
           v.id=(SELECT latest.id FROM visits latest WHERE latest.device_id=v.device_id
                 ORDER BY latest.id DESC LIMIT 1) AS latest
    FROM visits v JOIN houses h ON h.id=v.house_id
    LEFT JOIN intercom_stations s ON s.device_id=v.device_id
    LEFT JOIN intercom_calls c ON c.visit_id=v.id
"""


def initialize_intercom(store):
    with store.connect() as db:
        db.executescript(SCHEMA)
    initialize_assistant(store)


def paired_device_ids(db):
    return {row['device_id'] for row in db.execute(
        'SELECT device_id FROM intercom_stations WHERE token_hash IS NOT NULL'
    )}


def incoming_visits(db, scope, paired):
    if not paired:
        return []
    now = int(time())
    handoff = handoff_seconds()
    latest = db.execute("""SELECT v.id, v.device_id, v.received_at, h.name, c.answered_by,
                               c.operator_hash=? AS can_rejoin
                        FROM visits v JOIN houses h ON h.id=v.house_id
                        LEFT JOIN intercom_calls c ON c.visit_id=v.id
                        WHERE (? IS NULL OR h.id=?)
                          AND ((c.visit_id IS NULL AND v.received_at>?) OR
                               (c.ended_at IS NULL AND c.accepted_at>?))
                          AND v.id=(SELECT id FROM visits WHERE device_id=v.device_id ORDER BY id DESC LIMIT 1)
                        ORDER BY v.id DESC""", (digest(session.get('csrf', '').encode()), scope, scope,
                                              now - RING_SECONDS, now - CALL_SECONDS))
    return [
        dict(visit) | {'assistant_active': visit['answered_by'] is None and now - visit['received_at'] >= handoff}
        for visit in latest if visit['device_id'] in paired
    ]


def call_status(visit, now):
    if visit is None:
        return 'idle'
    if not visit['latest'] or not visit['paired'] or visit['ended_at'] is not None:
        return 'ended'
    if visit['accepted_at'] is not None:
        return 'active' if 0 <= now - visit['accepted_at'] < CALL_SECONDS else 'ended'
    age = now - visit['received_at']
    if 0 <= age < RING_SECONDS:
        return 'assistant' if age >= handoff_seconds() else 'ringing'
    return 'ended'


@dataclass(frozen=True)
class Clip:
    visit_id: int
    side: str
    media_type: str
    data: bytes
    started: float


class VoiceClips:
    def __init__(self):
        self.lock = Lock()
        self.clips = {}
        self.size = 0
        self.last_id = 0
        self.timer = None

    def _discard(self, clip_id):
        self.size -= len(self.clips.pop(clip_id).data)

    def _prune(self, now):
        for clip_id, clip in list(self.clips.items()):
            if now - clip.started >= CLIP_SECONDS:
                self._discard(clip_id)

    def _schedule_cleanup(self, now):
        if self.timer:
            self.timer.cancel()
            self.timer = None
        first = next(iter(self.clips.values()), None)
        if first:
            timer = Timer(max(0, first.started + CLIP_SECONDS - now), lambda: self._expire(timer))
            timer.daemon = True
            self.timer = timer
            timer.start()

    def _expire(self, timer):
        with self.lock:
            if self.timer is timer:
                now = monotonic()
                self._prune(now)
                self._schedule_cleanup(now)

    def save(self, visit_id, side, media_type, data):
        with self.lock:
            now = monotonic()
            self._prune(now)
            local = [clip_id for clip_id, clip in self.clips.items() if clip.visit_id == visit_id]
            if any(self.clips[clip_id].side == side and now - self.clips[clip_id].started < CLIP_INTERVAL
                   for clip_id in local):
                return None
            old_id = local[0] if len(local) >= MAX_CALL_CLIPS else None
            freed = len(self.clips[old_id].data) if old_id is not None else 0
            if len(self.clips) - bool(old_id) >= MAX_CLIPS or self.size + len(data) - freed > MAX_AUDIO_BYTES:
                return None
            if old_id is not None:
                self._discard(old_id)
            self.last_id = max(self.last_id + 1, time_ns() // 1000)
            self.clips[self.last_id] = Clip(visit_id, side, media_type, data, now)
            self.size += len(data)
            self._schedule_cleanup(now)
            return self.last_id

    def peer_ids(self, visit_id, side, after):
        with self.lock:
            self._prune(monotonic())
            return [clip_id for clip_id, clip in self.clips.items()
                    if clip.visit_id == visit_id and clip.side != side and clip_id > after]

    def get_peer(self, visit_id, side, clip_id):
        with self.lock:
            self._prune(monotonic())
            clip = self.clips.get(clip_id)
            if clip and clip.visit_id == visit_id and clip.side != side:
                return clip
            return None

    def remove_calls(self, visits):
        with self.lock:
            for clip_id, clip in list(self.clips.items()):
                if clip.visit_id in visits:
                    self._discard(clip_id)
            self._schedule_cleanup(monotonic())


class PairAttempts:
    def __init__(self):
        self.lock = Lock()
        self.attempts = {}

    def allow(self, address):
        with self.lock:
            minute = int(monotonic() // 60)
            key = (minute, address)
            self.attempts = {k: n for k, n in self.attempts.items() if k[0] == minute}
            count = self.attempts.get(key, 0)
            if count >= 8 or sum(self.attempts.values()) >= 64:
                return False
            self.attempts[key] = count + 1
            return True


def _end_device_calls(db, device, now):
    return {row['visit_id'] for row in db.execute(
        """UPDATE intercom_calls SET ended_at=? WHERE ended_at IS NULL
           AND visit_id IN (SELECT id FROM visits WHERE device_id=?) RETURNING visit_id""", (now, device)
    )}


def _audio_signature(data, media_type):
    if len(data) < 12:
        return False
    if media_type == 'audio/webm':
        return data.startswith(b'\x1a\x45\xdf\xa3')
    if media_type == 'audio/ogg':
        return data.startswith(b'OggS')
    return data[4:8] == b'ftyp'


def create_intercom_blueprint(store):
    blueprint = Blueprint('intercom', __name__)
    clips = VoiceClips()
    signaling = Signaling()
    attempts = PairAttempts()
    assistant = HermesVoiceAssistant(store)

    @blueprint.record_once
    def register_assistant(state):
        state.app.extensions['rental_assistant'] = assistant

    def devices_for(db, actor):
        house = actor['house_id']
        return db.execute(
            """SELECT d.id, h.name, s.token_hash IS NOT NULL AS paired
               FROM devices d JOIN houses h ON h.id=d.house_id
               LEFT JOIN intercom_stations s ON s.device_id=d.id
               WHERE (? IS NULL OR d.house_id=?) ORDER BY h.name,d.id""", (house, house)
        ).fetchall()

    def station_token():
        value = request.cookies.get(STATION_COOKIE, '')
        return value if AUTH_TOKEN.fullmatch(value) else None

    def station_csrf(token):
        return digest(b'intercom station csrf\x00' + token.encode()) if token else session.setdefault(
            'station_csrf', secrets.token_urlsafe(24)
        )

    def protect_station(token):
        submitted = request.headers.get('X-CSRF-Token') or request.form.get('csrf', '')
        if not hmac.compare_digest(station_csrf(token).encode(), submitted.encode()):
            abort(403)

    def paired_station(db, token):
        if not token:
            return None
        scope = current_app.config.get('RENTAL_HOUSE_A_ID')
        return db.execute('''SELECT s.device_id, h.name FROM intercom_stations s
                             JOIN devices d ON d.id=s.device_id JOIN houses h ON h.id=d.house_id
                             WHERE s.token_hash=? AND (? IS NULL OR d.house_id=?)''',
                          (digest(token.encode()), scope, scope)).fetchone()

    def get_operator_visit(db, visit_id):
        actor = require_roles('owner', 'manager')
        if not 1 <= visit_id <= MAX_RECORD_ID:
            abort(404)
        visit = db.execute(VISIT_COLUMNS + ' WHERE v.id=?', (visit_id,)).fetchone()
        if visit is None or not scoped_house(actor, visit['house_id']):
            abort(404)
        return visit

    def operator_hash():
        return digest(session.get('csrf', '').encode())

    def operator_call(db, visit_id):
        visit = get_operator_visit(db, visit_id)
        status = call_status(visit, int(time()))
        if status == 'active' and visit['operator_hash'] != operator_hash():
            abort(409)
        return visit, status

    def station_call(token):
        with store.connect() as db:
            station = paired_station(db, token)
            if not station:
                abort(401)
            visit = db.execute(VISIT_COLUMNS + ' WHERE v.device_id=? ORDER BY v.id DESC LIMIT 1',
                               (station['device_id'],)).fetchone()
        return visit, call_status(visit, int(time()))

    def state_response(visit, status, side):
        visit_id = visit['id'] if visit else None
        available = []
        if status == 'active':
            after = max(0, request.args.get('after', 0, int))
            args = {'visit_id': visit_id} if side == 'operator' else {}
            available = [{'id': clip_id, 'url': url_for(f'intercom.{side}_clip', clip_id=clip_id, **args)}
                         for clip_id in clips.peer_ids(visit_id, side, after)]
        result = {'status': status, 'visit_id': visit_id, 'clips': available}
        if status == 'active' and request.args.get('ice') == '1':
            result['ice_servers'] = ice_servers(visit_id)
        if request.args.get('assistant') == '1' and visit is not None:
            now = int(time())
            delay = handoff_seconds()
            if status == 'ringing':
                result['assistant_in'] = max(0, delay - max(0, now - visit['received_at']))
            elif status == 'assistant':
                with store.connect() as db:
                    turns = assistant.turn_count(db, visit_id)
                result['assistant_greeting'] = DEFAULT_GREETING
                result['assistant_turns'] = turns
                result['assistant_max_turns'] = ASSISTANT_MAX_TURNS
        return result

    def audio_upload(visit_id, side):
        media_type = AUDIO_TYPES.get(request.mimetype)
        if media_type is None:
            abort(415)
        request.max_content_length = CLIP_BYTES
        data = request.get_data(cache=False)
        if not _audio_signature(data, media_type):
            abort(415)
        clip_id = clips.save(visit_id, side, media_type, data)
        if clip_id is None:
            abort(429)
        return {'id': clip_id}, 201

    def audio_response(visit_id, side, clip_id):
        clip = clips.get_peer(visit_id, side, clip_id)
        if clip is None:
            abort(404)
        response = Response(clip.data, mimetype=clip.media_type,
                            headers={'Cross-Origin-Resource-Policy': 'same-origin', 'Cache-Control': 'no-store'})
        return response.make_conditional(request, accept_ranges=True, complete_length=len(clip.data))

    def setup_page(actor, *, code=None, device_name=None):
        with store.connect() as db:
            devices = devices_for(db, actor)
            incoming = incoming_visits(db, actor['house_id'], {device['id'] for device in devices if device['paired']})
        return render_template('intercom_setup.html', devices=devices, incoming=incoming,
                               code=code, device_name=device_name)

    @blueprint.get('/intercom')
    def index():
        return setup_page(require_roles('owner', 'manager'))

    @blueprint.post('/intercom/stations/<device>/pair')
    def pair(device):
        actor = require_roles('owner')
        code = ''.join(secrets.choice(PAIR_ALPHABET) for _ in range(8))
        with store.write() as db:
            row = db.execute('''SELECT d.id, d.house_id, h.name FROM devices d JOIN houses h ON h.id=d.house_id
                                WHERE d.id=?''', (device,)).fetchone()
            if row is None or not scoped_house(actor, row['house_id']):
                abort(404)
            db.execute('''INSERT INTO intercom_stations(device_id,pair_code_hash,pair_until) VALUES(?,?,?)
                          ON CONFLICT(device_id) DO UPDATE SET pair_code_hash=excluded.pair_code_hash,
                                                            pair_until=excluded.pair_until''',
                       (device, digest(code.encode()), int(time()) + PAIR_SECONDS))
        return setup_page(actor, code=code, device_name=row['name'])

    @blueprint.post('/intercom/stations/<device>/revoke')
    def revoke(device):
        actor = require_roles('owner')
        with store.write() as db:
            if actor['house_id'] is not None and not db.execute(
                'SELECT 1 FROM devices WHERE id=? AND house_id=?', (device, actor['house_id'])
            ).fetchone():
                abort(404)
            db.execute('DELETE FROM intercom_stations WHERE device_id=?', (device,))
            ended = _end_device_calls(db, device, int(time()))
        clips.remove_calls(ended)
        signaling.remove(ended)
        flash('Đã ngắt điện thoại ở cửa.', 'success')
        return redirect(url_for('intercom.index'), code=303)

    @blueprint.get('/intercom/station')
    def station():
        token = station_token()
        with store.connect() as db:
            paired = paired_station(db, token)
        return render_template('intercom_station.html', paired=bool(paired),
                               station_name=paired['name'] if paired else None,
                               station_csrf=station_csrf(token if paired else None))

    @blueprint.post('/intercom/station/pair')
    def station_pair():
        protect_station(None)
        code = request.form.get('code', '').strip().upper()
        if not PAIR_PATTERN.fullmatch(code):
            flash('Nhập mã ghép nối gồm 8 ký tự.', 'error')
            return redirect(url_for('intercom.station'), code=303)
        if not attempts.allow(request.remote_addr or ''):
            return 'Bạn đã thử nhiều lần. Vui lòng đợi một phút.', 429
        token = secrets.token_urlsafe(32)
        with store.write() as db:
            now = int(time())
            scope = current_app.config.get('RENTAL_HOUSE_A_ID')
            row = db.execute(
                """UPDATE intercom_stations SET token_hash=?,pair_code_hash=NULL,pair_until=NULL
                   WHERE pair_code_hash=? AND pair_until>?
                     AND (? IS NULL OR device_id IN (SELECT id FROM devices WHERE house_id=?))
                   RETURNING device_id""",
                (digest(token.encode()), digest(code.encode()), now, scope, scope)
            ).fetchone()
            ended = _end_device_calls(db, row['device_id'], now) if row else set()
        if row is None:
            flash('Mã ghép nối chưa đúng hoặc đã hết hạn.', 'error')
            return redirect(url_for('intercom.station'), code=303)
        clips.remove_calls(ended)
        signaling.remove(ended)
        session.clear()
        response = redirect(url_for('intercom.station'), code=303)
        response.set_cookie(STATION_COOKIE, token, path=STATION_PATH, max_age=STATION_COOKIE_AGE, httponly=True,
                            samesite='Strict', secure=current_app.config['SESSION_COOKIE_SECURE'])
        return response

    @blueprint.post('/intercom/station/unpair')
    def station_unpair():
        token = station_token()
        protect_station(token)
        if token is None:
            abort(401)
        with store.write() as db:
            row = db.execute('UPDATE intercom_stations SET token_hash=NULL WHERE token_hash=? RETURNING device_id',
                             (digest(token.encode()),)).fetchone()
            ended = _end_device_calls(db, row['device_id'], int(time())) if row else set()
        clips.remove_calls(ended)
        signaling.remove(ended)
        response = redirect(url_for('intercom.station'), code=303)
        response.delete_cookie(STATION_COOKIE, path=STATION_PATH, httponly=True, samesite='Strict',
                               secure=current_app.config['SESSION_COOKIE_SECURE'])
        return response

    @blueprint.get('/intercom/station/state')
    def station_state():
        visit, status = station_call(station_token())
        return state_response(visit, status, 'station')

    @blueprint.post('/intercom/station/signal')
    def station_signal():
        token = station_token()
        protect_station(token)
        visit, status = station_call(token)
        if status != 'active' or request.headers.get('X-Intercom-Visit') != str(visit['id']):
            abort(409)
        return signaling.exchange(visit['id'], 'station')

    @blueprint.post('/intercom/station/audio')
    def station_audio():
        token = station_token()
        visit, status = station_call(token)
        protect_station(token)
        if status != 'active' or request.headers.get('X-Intercom-Visit') != str(visit['id']):
            abort(409)
        return audio_upload(visit['id'], 'station')

    @blueprint.get('/intercom/station/clips/<int:clip_id>')
    def station_clip(clip_id):
        visit, status = station_call(station_token())
        if status != 'active':
            abort(404)
        return audio_response(visit['id'], 'station', clip_id)

    @blueprint.post('/intercom/station/assistant')
    def station_assistant():
        token = station_token()
        protect_station(token)
        visit, status = station_call(token)
        if status != 'assistant' or request.headers.get('X-Intercom-Visit') != str(visit['id']):
            abort(409)
        if request.is_json:
            payload = request.get_json(silent=True)
            text = payload.get('text', '') if isinstance(payload, dict) else ''
        else:
            media_type = AUDIO_TYPES.get(request.mimetype)
            if not media_type:
                abort(415)
            with store.connect() as db:
                if assistant.turn_count(db, visit['id']) >= ASSISTANT_MAX_TURNS:
                    return {'done': True}, 429
            request.max_content_length = CLIP_BYTES
            clip = request.get_data(cache=False)
            if not _audio_signature(clip, media_type):
                abort(415)
            text, transcription_code = assistant.transcribe(visit['id'], clip, media_type)
            if transcription_code != 200:
                return {'transcription': 'no_speech' if transcription_code == 422 else 'unavailable'}, transcription_code
            current, status = station_call(token)
            if status != 'assistant' or current['id'] != visit['id']:
                abort(409)
        body, code = assistant.reply(
            visit['id'], visit['house_name'], visit['recipient_chat_id'], text
        )
        if body is None:
            abort(code)
        if not request.is_json:
            body['transcript'] = text
        return body, code

    @blueprint.post('/intercom/station/assistant/speech/<int:reply_id>')
    def station_assistant_speech(reply_id):
        token = station_token()
        protect_station(token)
        visit, status = station_call(token)
        if status != 'assistant' or request.headers.get('X-Intercom-Visit') != str(visit['id']):
            abort(409)
        with store.connect() as db:
            last = db.execute(
                '''SELECT id, text FROM intercom_assistant_turns
                   WHERE visit_id=? AND speaker='hermes' ORDER BY id DESC LIMIT 1''', (visit['id'],),
            ).fetchone()
        if last is None or last['id'] != reply_id:
            abort(404)
        audio = assistant.synthesize_reply(reply_id, last['text'])
        if audio is None:
            abort(503)
        current, status = station_call(token)
        if status != 'assistant' or current['id'] != visit['id']:
            abort(409)
        return Response(audio, mimetype='audio/wav', headers={'Cache-Control': 'no-store'})

    @blueprint.post('/intercom/visits/<int:visit_id>/answer')
    def answer(visit_id):
        actor = require_roles('owner', 'manager')
        with store.write() as db:
            visit = get_operator_visit(db, visit_id)
            now = int(time())
            status = call_status(visit, now)
            if status in {'ringing', 'assistant'}:
                db.execute('''INSERT INTO intercom_calls(visit_id,accepted_at,answered_by,operator_hash)
                              VALUES(?,?,?,?)''', (visit_id, now, actor['username'], operator_hash()))
                if status == 'assistant':
                    end_assistant_session(db, visit_id, now)
            elif status != 'active' or visit['operator_hash'] != operator_hash():
                flash('Lượt chuông đã hết hạn, điện thoại ở cửa chưa ghép nối hoặc đã có người trả lời.', 'error')
                return redirect(url_for('dashboard', _anchor=f'visit-{visit_id}'), code=303)
        return redirect(url_for('intercom.operator', visit_id=visit_id), code=303)

    @blueprint.get('/intercom/visits/<int:visit_id>')
    def operator(visit_id):
        with store.connect() as db:
            visit, status = operator_call(db, visit_id)
            actor = require_roles('owner', 'manager')
            flash_visit = get_web_visit(db, visit_id, actor)
        return render_template('intercom_call.html', visit_id=visit_id, house_name=visit['house_name'],
                               device_id=visit['device_id'], status=status,
                               flash_state=flash_status(flash_visit, int(time())))

    @blueprint.get('/intercom/visits/<int:visit_id>/state')
    def operator_state(visit_id):
        with store.connect() as db:
            visit, status = operator_call(db, visit_id)
        return state_response(visit, status, 'operator')

    @blueprint.post('/intercom/visits/<int:visit_id>/signal')
    def operator_signal(visit_id):
        with store.connect() as db:
            _, status = operator_call(db, visit_id)
        if status != 'active':
            abort(409)
        return signaling.exchange(visit_id, 'operator')

    @blueprint.post('/intercom/visits/<int:visit_id>/audio')
    def operator_audio(visit_id):
        with store.connect() as db:
            _, status = operator_call(db, visit_id)
        if status != 'active':
            abort(409)
        return audio_upload(visit_id, 'operator')

    @blueprint.get('/intercom/visits/<int:visit_id>/clips/<int:clip_id>')
    def operator_clip(visit_id, clip_id):
        with store.connect() as db:
            _, status = operator_call(db, visit_id)
        if status != 'active':
            abort(404)
        return audio_response(visit_id, 'operator', clip_id)

    @blueprint.post('/intercom/visits/<int:visit_id>/end')
    def operator_end(visit_id):
        with store.write() as db:
            _, status = operator_call(db, visit_id)
            if status == 'active':
                db.execute('UPDATE intercom_calls SET ended_at=? WHERE visit_id=?', (int(time()), visit_id))
        clips.remove_calls({visit_id})
        signaling.remove({visit_id})
        return redirect(url_for('intercom.operator', visit_id=visit_id), code=303)

    return blueprint
