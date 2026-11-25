import re
import secrets
from time import time

from rental.accounts import scoped_house


VISIT_SECONDS = 300
QUEUE_SECONDS = 20
ACK_SECONDS = 25
DIRECT_COOLDOWN = 6
PAD_DEMO_SECONDS = 20
PAD_DEMO_REQUESTER = "PAD 2D · mô phỏng"
COMMAND_ID = re.compile(r"[0-9a-f]{32}")

FLASH_FIELDS = """
    f.requested_at AS flash_requested_at, f.delivered_at AS flash_delivered_at,
    f.confirmed_at AS flash_confirmed_at,
    v.id = (SELECT newest.id FROM visits newest WHERE newest.device_id=v.device_id
            ORDER BY newest.id DESC LIMIT 1) AS flash_latest
"""
FLASH_JOIN = "LEFT JOIN doorbell_flash_commands f ON f.visit_id=v.id"
FLASH_VISIT = f"""
    SELECT v.id, v.received_at, v.house_id, v.recipient_chat_id, v.telegram_message_id,
           v.notification_status, h.telegram_chat_id, {FLASH_FIELDS}
    FROM visits v JOIN houses h ON h.id=v.house_id {FLASH_JOIN}
    WHERE v.id=?
"""


def initialize_flash(store):
    with store.connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS doorbell_flash_commands (
                id TEXT PRIMARY KEY NOT NULL,
                visit_id INTEGER NOT NULL UNIQUE REFERENCES visits(id),
                source TEXT NOT NULL CHECK (source IN ('web', 'telegram')),
                requested_by TEXT NOT NULL,
                requested_at INTEGER NOT NULL,
                delivered_at INTEGER,
                confirmed_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS visits_device_latest ON visits(device_id, id DESC);
            CREATE TABLE IF NOT EXISTS direct_flash_commands (
                id TEXT PRIMARY KEY NOT NULL,
                device_id TEXT NOT NULL REFERENCES devices(id),
                source TEXT NOT NULL CHECK (source IN ('web', 'telegram')),
                requested_by TEXT NOT NULL,
                requested_at INTEGER NOT NULL,
                delivered_at INTEGER,
                confirmed_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS direct_flash_device_recent
                ON direct_flash_commands(device_id, requested_at DESC);
            CREATE TABLE IF NOT EXISTS direct_flash_requests (
                request_key TEXT PRIMARY KEY NOT NULL,
                command_id TEXT NOT NULL REFERENCES direct_flash_commands(id),
                requested_by TEXT NOT NULL
            );
        """)


def flash_status(visit, now):
    if visit['flash_confirmed_at'] is not None:
        return 'confirmed'
    if visit['flash_delivered_at'] is not None:
        return 'sent' if now - visit['flash_delivered_at'] < ACK_SECONDS else 'expired'
    actionable = visit['flash_latest'] and 0 <= now - visit['received_at'] < VISIT_SECONDS
    if visit['flash_requested_at'] is not None:
        return 'queued' if actionable and now - visit['flash_requested_at'] < QUEUE_SECONDS else 'expired'
    return 'ready' if actionable else 'unavailable'


def get_web_visit(db, visit_id, actor):
    row = db.execute(FLASH_VISIT, (visit_id,)).fetchone()
    if row is not None and actor['role'] in {'owner', 'manager'} and scoped_house(actor, row['house_id']):
        return row
    return None


def _queue_flash(db, row, source, actor, now):
    status = flash_status(row, now)
    if status == 'confirmed':
        return status
    if row['flash_requested_at'] is not None:
        return 'existing'
    if status != 'ready':
        return 'unavailable'
    db.execute('''INSERT INTO doorbell_flash_commands(id, visit_id, source, requested_by, requested_at)
                  VALUES (?, ?, ?, ?, ?)''', (secrets.token_hex(16), row['id'], source, actor, now))
    return 'queued'


def queue_web_flash(store, visit_id, actor):
    with store.write() as db:
        row = get_web_visit(db, visit_id, actor)
        return _queue_flash(db, row, 'web', actor['username'], int(time())) if row else 'denied'


def queue_pad_demo_flash(store, job, stay_id, device):
    with store.write() as db:
        now = int(time())
        eligible = db.execute("""SELECT 1 FROM visits v
                                 JOIN devices d ON d.id=v.device_id AND d.house_id=v.house_id
                                 JOIN rooms r ON r.house_id=v.house_id
                                 JOIN stays s ON s.room_id=r.id
                                 WHERE v.id=? AND v.state='processing' AND v.claim=? AND d.id=?
                                   AND v.received_at>? AND s.id=? AND s.ended_at IS NULL
                                   AND EXISTS (SELECT 1 FROM face_samples f WHERE f.stay_id=s.id)""",
                              (job['id'], job['claim'], device, now - PAD_DEMO_SECONDS, stay_id)).fetchone()
        if not eligible:
            return 'unavailable'
        row = db.execute(FLASH_VISIT, (job['id'],)).fetchone()
        return _queue_flash(db, row, 'web', PAD_DEMO_REQUESTER, now)


def queue_telegram_flash(store, visit_id, chat_id, message_id):
    chat_id = str(chat_id)
    if not re.fullmatch(r"[1-9][0-9]{0,15}", chat_id):
        return 'denied'
    with store.write() as db:
        row = db.execute(FLASH_VISIT, (visit_id,)).fetchone()
        if (not row or row['recipient_chat_id'] != chat_id or row['telegram_chat_id'] != chat_id
                or row['notification_status'] != 'sent' or row['telegram_message_id'] != message_id):
            return 'denied'
        return _queue_flash(db, row, 'telegram', chat_id, int(time()))


def _direct_status(row, now):
    if row['confirmed_at'] is not None:
        return 'confirmed'
    if row['delivered_at'] is not None:
        return 'sent' if now - row['delivered_at'] < ACK_SECONDS else 'expired'
    return 'queued' if now - row['requested_at'] < QUEUE_SECONDS else 'expired'


def queue_direct_flash(store, device, *, actor=None, chat_id=None, request_key=None, house_id=None):
    with store.write() as db:
        target = db.execute('''SELECT d.house_id, h.telegram_chat_id FROM devices d
                               JOIN houses h ON h.id=d.house_id WHERE d.id=?''', (device,)).fetchone()
        if not target or (house_id is not None and house_id != target['house_id']):
            return 'denied', None
        if actor is not None:
            if actor['role'] not in {'owner', 'manager'} or not scoped_house(actor, target['house_id']):
                return 'denied', None
            source, requester = 'web', actor['username']
        elif chat_id and re.fullmatch(r'[1-9][0-9]{0,15}', chat_id) and chat_id == target['telegram_chat_id']:
            source, requester = 'telegram', chat_id
        else:
            return 'denied', None

        now = int(time())
        if request_key:
            duplicate = db.execute('''SELECT c.*, r.requested_by AS request_owner
                                      FROM direct_flash_requests r JOIN direct_flash_commands c ON c.id=r.command_id
                                      WHERE r.request_key=?''', (request_key,)).fetchone()
            if duplicate:
                if (duplicate['device_id'], duplicate['request_owner']) != (device, requester):
                    return 'denied', None
                state = _direct_status(duplicate, now)
                return ('existing' if state in {'queued', 'sent'} else state), duplicate['id']
        last = db.execute('''SELECT * FROM direct_flash_commands WHERE device_id=?
                             ORDER BY requested_at DESC, rowid DESC LIMIT 1''', (device,)).fetchone()
        if last and (last['source'] == 'web' or last['requested_by'] == target['telegram_chat_id']):
            state = _direct_status(last, now)
            if state in {'queued', 'sent'} or now - last['requested_at'] < DIRECT_COOLDOWN:
                if request_key:
                    db.execute('INSERT INTO direct_flash_requests VALUES (?,?,?)', (request_key, last['id'], requester))
                return 'existing', last['id']

        command = secrets.token_hex(16)
        db.execute('''INSERT INTO direct_flash_commands
                      (id, device_id, source, requested_by, requested_at) VALUES (?,?,?,?,?)''',
                   (command, device, source, requester, now))
        if request_key:
            db.execute('INSERT INTO direct_flash_requests VALUES (?,?,?)', (request_key, command, requester))
        return 'queued', command


def direct_flash_status(store, device, command):
    if not COMMAND_ID.fullmatch(command):
        return None
    with store.connect() as db:
        row = db.execute('''SELECT c.* FROM direct_flash_commands c
                            JOIN devices d ON d.id=c.device_id JOIN houses h ON h.id=d.house_id
                            WHERE c.id=? AND c.device_id=? AND (c.source='web' OR c.requested_by=h.telegram_chat_id)''',
                         (command, device)).fetchone()
    return _direct_status(row, int(time())) if row else None


CLAIMABLE_COMMAND = """
    SELECT f.id FROM doorbell_flash_commands f JOIN visits v ON v.id=f.visit_id
    JOIN houses h ON h.id=v.house_id
    WHERE v.id=(SELECT id FROM visits WHERE device_id=? ORDER BY id DESC LIMIT 1)
      AND v.received_at>? AND f.requested_at>? AND f.delivered_at IS NULL
      AND (f.source='web' OR f.requested_by=h.telegram_chat_id)
"""
CLAIMABLE_DIRECT = """
    SELECT c.id FROM direct_flash_commands c
    JOIN devices d ON d.id=c.device_id JOIN houses h ON h.id=d.house_id
    WHERE c.device_id=? AND c.requested_at>? AND c.delivered_at IS NULL
      AND (c.source='web' OR c.requested_by=h.telegram_chat_id)
    ORDER BY c.requested_at, c.rowid LIMIT 1
"""


def claim_flash(store, device):
    now = int(time())
    condition = (device, now - VISIT_SECONDS, now - QUEUE_SECONDS)
    with store.connect() as db:
        if (not db.execute(CLAIMABLE_COMMAND, condition).fetchone()
                and not db.execute(CLAIMABLE_DIRECT, (device, now - QUEUE_SECONDS)).fetchone()):
            return None
    with store.write() as db:
        now = int(time())
        row = db.execute(f"""UPDATE doorbell_flash_commands SET delivered_at=?
                         WHERE id=({CLAIMABLE_COMMAND}) RETURNING id""",
                         (now, device, now - VISIT_SECONDS, now - QUEUE_SECONDS)).fetchone()
        if not row:
            row = db.execute(f"""UPDATE direct_flash_commands SET delivered_at=?
                                 WHERE id=({CLAIMABLE_DIRECT}) RETURNING id""",
                             (now, device, now - QUEUE_SECONDS)).fetchone()
    return row['id'] if row else None


def acknowledge_flash(store, device, command_id):
    if not COMMAND_ID.fullmatch(command_id):
        return False
    with store.write() as db:
        now = int(time())
        updated = db.execute('''UPDATE doorbell_flash_commands SET confirmed_at=COALESCE(confirmed_at, ?)
                             WHERE id=? AND delivered_at IS NOT NULL
                               AND (confirmed_at IS NOT NULL OR delivered_at>?)
                               AND visit_id IN (SELECT id FROM visits WHERE device_id=?)''',
                             (now, command_id, now - ACK_SECONDS, device)).rowcount
        if not updated:
            updated = db.execute('''UPDATE direct_flash_commands SET confirmed_at=COALESCE(confirmed_at, ?)
                                    WHERE id=? AND device_id=? AND delivered_at IS NOT NULL
                                    AND (confirmed_at IS NOT NULL OR delivered_at>?)''',
                                 (now, command_id, device, now - ACK_SECONDS)).rowcount
    return bool(updated)
