import hashlib
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from smart_doorbell.database import Database


SCHEMA = """
CREATE TABLE IF NOT EXISTS houses (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    telegram_chat_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY,
    house_id INTEGER NOT NULL REFERENCES houses(id),
    code TEXT NOT NULL,
    UNIQUE (house_id, code)
);
CREATE TABLE IF NOT EXISTS stays (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    name TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT
);
CREATE INDEX IF NOT EXISTS stays_current ON stays(room_id) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS face_samples (
    id INTEGER PRIMARY KEY,
    stay_id INTEGER NOT NULL REFERENCES stays(id) ON DELETE CASCADE,
    vector BLOB NOT NULL CHECK (length(vector) = 512),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (stay_id, vector)
);
CREATE TRIGGER IF NOT EXISTS face_samples_checkout
AFTER UPDATE OF ended_at ON stays
WHEN NEW.ended_at IS NOT NULL
BEGIN
    DELETE FROM face_samples WHERE stay_id = NEW.id;
END;
"""

DEVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS {devices} (
    id TEXT PRIMARY KEY,
    house_id INTEGER NOT NULL REFERENCES houses(id),
    token_hash TEXT NOT NULL
)
"""
VISIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS {visits} (
    id INTEGER PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES {devices}(id),
    house_id INTEGER NOT NULL REFERENCES houses(id),
    event_id TEXT NOT NULL,
    recipient_chat_id TEXT NOT NULL,
    jpeg BLOB NOT NULL,
    image_hash TEXT NOT NULL,
    received_at INTEGER NOT NULL DEFAULT (unixepoch()),
    state TEXT NOT NULL DEFAULT 'queued',
    analysis_status TEXT NOT NULL DEFAULT 'pending',
    description TEXT,
    pad_result TEXT,
    notification_status TEXT NOT NULL DEFAULT 'pending',
    telegram_message_id INTEGER,
    claim TEXT,
    claimed_at INTEGER,
    UNIQUE (device_id, event_id)
)
"""


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def format_time(timestamp: int) -> str:
    # The project uses Vietnam time. UTC is retained in storage.
    local = timezone(timedelta(hours=7))
    return datetime.fromtimestamp(timestamp, local).strftime("%H:%M:%S %d/%m/%Y")


class RentalStore(Database):
    def ensure_house_a(self):
        with self.write() as db:
            db.execute("INSERT OR IGNORE INTO houses(name) VALUES ('Dãy A')")
            return db.execute("SELECT id FROM houses WHERE name='Dãy A'").fetchone()['id']

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
        with self.write() as db:
            if any(column["name"] == "room_id" for column in db.execute("PRAGMA table_info(devices)")):
                self.migrate_room_devices(db)
            db.execute(DEVICE_SCHEMA.format(devices="devices"))
            db.execute(VISIT_SCHEMA.format(devices="devices", visits="visits"))
            if not any(column['name'] == 'pad_result' for column in db.execute('PRAGMA table_info(visits)')):
                db.execute('ALTER TABLE visits ADD COLUMN pad_result TEXT')
            db.execute("CREATE INDEX IF NOT EXISTS visits_device_time ON visits(device_id, received_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS visits_work ON visits(state, id)")

    @staticmethod
    def migrate_room_devices(db) -> None:
        db.execute(DEVICE_SCHEMA.format(devices="devices_new"))
        db.execute(VISIT_SCHEMA.format(devices="devices_new", visits="visits_new"))
        db.execute("""INSERT INTO devices_new (id, house_id, token_hash)
                      SELECT devices.id, rooms.house_id, devices.token_hash FROM devices
                      LEFT JOIN rooms ON rooms.id=devices.room_id""")
        db.execute("""INSERT INTO visits_new (
                          id, device_id, house_id, event_id, recipient_chat_id, jpeg, image_hash,
                          received_at, state, analysis_status, description, notification_status,
                          telegram_message_id, claim, claimed_at)
                      SELECT visits.id, visits.device_id, rooms.house_id, visits.event_id,
                          visits.recipient_chat_id, visits.jpeg, visits.image_hash, visits.received_at,
                          visits.state, visits.analysis_status, visits.description, visits.notification_status,
                          visits.telegram_message_id, visits.claim, visits.claimed_at
                      FROM visits LEFT JOIN rooms ON rooms.id=visits.room_id""")
        db.execute("DROP TABLE visits")
        db.execute("DROP TABLE devices")
        db.execute("ALTER TABLE devices_new RENAME TO devices")
        db.execute("ALTER TABLE visits_new RENAME TO visits")

    @contextmanager
    def write(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            with db:
                yield db

    def authenticates(self, device_id: str, token_hash: str, house_id=None) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT token_hash FROM devices WHERE id=? AND (? IS NULL OR house_id=?)",
                             (device_id, house_id, house_id)).fetchone()
            return bool(row and secrets.compare_digest(row["token_hash"], token_hash))

    def register_device(self, house_id, device_id):
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("INSERT INTO devices (id, house_id, token_hash) VALUES (?, ?, ?)",
                       (device_id, house_id, digest(token.encode())))
        return token

    def save_face_sample(self, stay_id, vector, max_samples):
        with self.write() as db:
            if not db.execute("SELECT 1 FROM stays WHERE id=? AND ended_at IS NULL", (stay_id,)).fetchone():
                return "inactive"
            if db.execute("SELECT 1 FROM face_samples WHERE stay_id=? AND vector=?", (stay_id, vector)).fetchone():
                return "duplicate"
            count = db.execute("SELECT count(*) FROM face_samples WHERE stay_id=?", (stay_id,)).fetchone()[0]
            if count >= max_samples:
                return "full"
            db.execute("INSERT INTO face_samples (stay_id, vector) VALUES (?, ?)", (stay_id, vector))
            return "saved"

    def house_face_samples(self, house_id):
        with self.connect() as db:
            return db.execute(
                """SELECT stays.id AS stay_id, stays.name, rooms.code, face_samples.vector FROM face_samples
                   JOIN stays ON stays.id = face_samples.stay_id JOIN rooms ON rooms.id = stays.room_id
                   WHERE rooms.house_id=? AND stays.ended_at IS NULL""", (house_id,),
            ).fetchall()

    def receive(self, device_id: str, token_hash: str, event_id: str, jpeg: bytes, image_hash: str):
        with self.write() as db:
            device = db.execute("SELECT house_id, token_hash FROM devices WHERE id = ?", (device_id,)).fetchone()
            if not device or not secrets.compare_digest(device["token_hash"], token_hash):
                return 401, "unauthorized"
            previous = db.execute(
                "SELECT state, image_hash FROM visits WHERE device_id = ? AND event_id = ?",
                (device_id, event_id),
            ).fetchone()
            if previous:
                if previous["image_hash"] != image_hash:
                    return 409, "event_id_conflict"
                return 200, previous["state"]
            recent = db.execute(
                "SELECT 1 FROM visits WHERE device_id = ? AND received_at > unixepoch() - 3 LIMIT 1",
                (device_id,),
            ).fetchone()
            if recent:
                return 429, "rate_limited"
            db.execute(
                """INSERT INTO visits (device_id, house_id, event_id, jpeg, image_hash, recipient_chat_id)
                   SELECT ?, id, ?, ?, ?, telegram_chat_id FROM houses WHERE id=?""",
                (device_id, event_id, jpeg, image_hash, device["house_id"]),
            )
            return 202, "queued"

    def claim_visit(self):
        claim = secrets.token_hex(16)
        with self.write() as db:
            # A previous send may have reached Telegram before the process stopped.
            db.execute(
                """UPDATE visits SET state=CASE WHEN state='sending_before_description'
                          THEN 'awaiting_description' ELSE 'attention' END,
                          notification_status='delivery_unknown', claim=NULL
                   WHERE state IN ('sending', 'sending_before_description') AND claimed_at <= unixepoch() - 300"""
            )
            job = db.execute(
                """UPDATE visits SET state='processing', claim=?, claimed_at=unixepoch()
                   WHERE id = (
                       SELECT id FROM visits WHERE state='queued'
                           OR (state='processing' AND claimed_at <= unixepoch() - 300)
                       ORDER BY id LIMIT 1
                   ) RETURNING id, house_id, device_id, event_id, jpeg, received_at, claim, recipient_chat_id""",
                (claim,),
            ).fetchone()
            if not job:
                return None
            name = db.execute("SELECT name FROM houses WHERE id=?", (job["house_id"],)).fetchone()[0]
            return dict(job) | {"name": name}

    def record_analysis(self, job, status, description, send, *, describe=False, pad=None):
        with self.write() as db:
            destination = db.execute(
                "SELECT telegram_chat_id FROM houses WHERE id=?", (job["house_id"],),
            ).fetchone()[0]
            changed = send and destination != job["recipient_chat_id"]
            send = send and not changed
            notification = "recipient_changed" if changed else "sending" if send else "not_configured"
            if describe:
                state = 'sending_before_description' if send else 'awaiting_description'
            else:
                state = 'sending' if send else 'attention' if changed or status == 'failed' else 'done'
            saved = db.execute(
                """UPDATE visits SET state=?, analysis_status=?, description=?, pad_result=?, notification_status=?,
                          claimed_at=unixepoch(), claim=?
                   WHERE id=? AND state='processing' AND claim=?""",
                (state, status, description, pad, notification,
                 job["claim"] if send else None, job["id"], job["claim"]),
            ).rowcount == 1
            return saved and send

    def record_notification(self, job, status, message_id):
        with self.connect() as db:
            db.execute(
                """UPDATE visits SET state=CASE WHEN state='sending_before_description' THEN 'awaiting_description'
                          WHEN ?='sent' AND analysis_status != 'failed'
                          THEN 'done' ELSE 'attention' END,
                          notification_status=?, telegram_message_id=?, claim=NULL
                   WHERE id=? AND state IN ('sending', 'sending_before_description') AND claim=?""",
                (status, status, message_id, job["id"], job["claim"]),
            )

    def claim_description(self):
        with self.write() as db:
            job = db.execute(
                """UPDATE visits SET state='describing', claim=?, claimed_at=unixepoch()
                   WHERE id = (SELECT id FROM visits WHERE state='awaiting_description'
                       OR (state='describing' AND claimed_at <= unixepoch() - 300) ORDER BY id LIMIT 1)
                   RETURNING id, house_id, event_id, jpeg, received_at, claim, recipient_chat_id,
                       telegram_message_id, analysis_status, description, pad_result""",
                (secrets.token_hex(16),),
            ).fetchone()
            if job is None:
                return None
            name = db.execute('SELECT name FROM houses WHERE id=?', (job['house_id'],)).fetchone()[0]
            return dict(job) | {'name': name}

    def record_description(self, job, status, description):
        with self.write() as db:
            row = db.execute(
                """UPDATE visits SET state=CASE WHEN ?='failed' OR notification_status NOT IN ('sent', 'not_configured')
                           THEN 'attention' ELSE 'done' END,
                       analysis_status=?, description=?, claim=NULL
                   WHERE id=? AND state='describing' AND claim=?
                   RETURNING notification_status""",
                (status, status, description, job['id'], job['claim']),
            ).fetchone()
            if row is None or row['notification_status'] != 'sent':
                return False
            destination = db.execute('SELECT telegram_chat_id FROM houses WHERE id=?', (job['house_id'],)).fetchone()[0]
            return destination == job['recipient_chat_id']
