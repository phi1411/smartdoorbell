import io
import re
import secrets
from dataclasses import dataclass
from threading import BoundedSemaphore, Condition, Lock, Timer
from time import monotonic

from flask import Blueprint, Response, abort, current_app, render_template, request, session, url_for
from PIL import Image

from rental.accounts import require_roles, scoped_house
from rental.flash import acknowledge_flash, claim_flash, direct_flash_status, queue_direct_flash
from rental.store import digest


LEASE_SECONDS = 12
FRAME_SECONDS = 4
MIN_FRAME_INTERVAL = 0.25
MIN_WS_FRAME_INTERVAL = 0.125
MAX_FRAME_BYTES = 96 * 1024
MAX_CAMERAS = 8
MAX_STREAMS = 2
SNAPSHOT_SECONDS = 14
DEVICE_ID = re.compile(r"[A-Za-z0-9_-]{1,48}")


@dataclass
class Preview:
    viewer_id: tuple[str, str] | None
    viewer_token: str
    channel: str
    expires: float
    frame: bytes | None = None
    updated: float = 0.0
    sequence: int = 0
    publisher: object | None = None
    snapshot_expires: float = 0.0


class LivePreviews:
    """Latest frame only. Render runs one web process with multiple threads."""

    def __init__(self):
        self.lock = Lock()
        self.changed = Condition(self.lock)
        self.previews: dict[str, Preview] = {}
        self.cleanup = None
        self.cleanup_deadline = None
        self.snapshot_slot = BoundedSemaphore(1)

    def _prune(self, now):
        for key in list(self.previews):
            preview = self.previews[key]
            if max(preview.expires, preview.snapshot_expires) <= now:
                del self.previews[key]
                continue
            if preview.expires <= now:
                preview.viewer_id = None
                preview.viewer_token = ''
            if preview.frame is not None and now - preview.updated >= FRAME_SECONDS:
                preview.frame = None

    def _schedule_cleanup(self, now):
        deadline = min((
            min(deadline for deadline in (preview.expires, preview.snapshot_expires,
                                          preview.updated + FRAME_SECONDS if preview.frame is not None else 0)
                if deadline > now)
            for preview in self.previews.values()
        ), default=None)
        if self.cleanup is not None:
            if deadline is not None and self.cleanup_deadline <= deadline:
                return
            self.cleanup.cancel()
            self.cleanup = None
        self.cleanup_deadline = deadline
        if deadline is not None:
            timer = Timer(max(0.0, deadline - now), lambda: self._expire(timer))
            timer.daemon = True
            self.cleanup = timer
            timer.start()

    def _expire(self, timer):
        with self.lock:
            if self.cleanup is not timer:
                return
            self.cleanup = None
            now = monotonic()
            self._prune(now)
            self._schedule_cleanup(now)
            self.changed.notify_all()

    def start(self, device, viewer):
        with self.lock:
            now = monotonic()
            self._prune(now)
            prior = self.previews.get(device)
            if prior and prior.viewer_id is not None and prior.viewer_id != viewer:
                return None, 409
            if not prior and len(self.previews) >= MAX_CAMERAS:
                return None, 429
            # Starting again refreshes the channel and discards any previous frame.
            preview = prior if prior and prior.snapshot_expires > now else Preview(None, "", secrets.token_hex(16), 0)
            preview.viewer_id = viewer
            preview.viewer_token = secrets.token_hex(16)
            preview.expires = now + LEASE_SECONDS
            preview.frame = None
            self.previews[device] = preview
            self._schedule_cleanup(now)
            self.changed.notify_all()
            return preview.viewer_token, 200

    def viewer(self, device, viewer, token, *, action=None, previous_frame=""):
        with self.lock:
            now = monotonic()
            self._prune(now)
            preview = self.previews.get(device)
            if not preview or preview.viewer_id != viewer or preview.viewer_token != token:
                return None, "", 409
            if action == "stop":
                preview.expires = 0
                self._prune(now)
                self._schedule_cleanup(now)
                self.changed.notify_all()
            elif action == "renew":
                preview.expires = now + LEASE_SECONDS
            elif preview.frame is not None and str(preview.sequence) != previous_frame:
                return preview.frame, str(preview.sequence), 200
            return None, "", 204

    def stream(self, device, viewer, token):
        sequence = 0
        yield b"\x00" * 4
        while True:
            with self.changed:
                now = monotonic()
                self._prune(now)
                preview = self.previews.get(device)
                if not preview or preview.viewer_id != viewer or preview.viewer_token != token:
                    return
                frame = preview.frame if preview.sequence != sequence else None
                if frame is not None:
                    sequence = preview.sequence
                elif self.changed.wait(min(1.0, preview.expires - now)):
                    continue
            # Empty records keep the connection open and let the browser detect a stalled feed.
            yield len(frame).to_bytes(4, "big") + frame if frame is not None else b"\x00" * 4

    def demand(self, device):
        with self.lock:
            self._prune(monotonic())
            preview = self.previews.get(device)
            return preview.channel if preview else None

    def snapshot(self, device):
        if not self.snapshot_slot.acquire(blocking=False):
            return None, 429
        try:
            with self.changed:
                requested = monotonic()
                self._prune(requested)
                preview = self.previews.get(device)
                if preview is None:
                    if len(self.previews) >= MAX_CAMERAS:
                        return None, 429
                    preview = Preview(None, "", secrets.token_hex(16), 0)
                    self.previews[device] = preview
                deadline = requested + SNAPSHOT_SECONDS
                sequence = preview.sequence
                preview.snapshot_expires = deadline
                self._schedule_cleanup(requested)
                try:
                    while (remaining := deadline - monotonic()) > 0:
                        if preview.frame is not None and preview.sequence > sequence:
                            return preview.frame, 200
                        self.changed.wait(remaining)
                    return None, 504
                finally:
                    now = monotonic()
                    preview.snapshot_expires = 0
                    self._prune(now)
                    self._schedule_cleanup(now)
                    self.changed.notify_all()
        finally:
            self.snapshot_slot.release()

    def claim_publisher(self, device, channel):
        with self.lock:
            self._prune(monotonic())
            preview = self.previews.get(device)
            if not preview or preview.channel != channel or preview.publisher is not None:
                return None
            preview.publisher = object()
            return preview.publisher

    def release_publisher(self, device, publisher):
        with self.lock:
            preview = self.previews.get(device)
            if preview and preview.publisher is publisher:
                preview.publisher = None

    def publish(self, device, channel, frame=None, *, publisher=None):
        with self.lock:
            now = monotonic()
            self._prune(now)
            preview = self.previews.get(device)
            if not preview or preview.channel != channel or preview.publisher is not publisher:
                return 409
            interval = MIN_WS_FRAME_INTERVAL if publisher is not None else MIN_FRAME_INTERVAL
            if preview.sequence and now - preview.updated < interval:
                return 429
            if frame is not None:
                preview.frame = frame
                preview.updated = now
                preview.sequence += 1
                self._schedule_cleanup(now)
                self.changed.notify_all()
            return 204


def validate_frame(payload, *, preserve=False):
    if not 4 <= len(payload) <= MAX_FRAME_BYTES:
        raise ValueError("Preview exceeds size limit or is incomplete")
    if preserve and not (payload.startswith(b"\xff\xd8") and payload.endswith(b"\xff\xd9")):
        raise ValueError("Incomplete JPEG")
    with Image.open(io.BytesIO(payload), formats=["JPEG"]) as source:
        if not 32 <= source.width <= 800 or not 32 <= source.height <= 600:
            raise ValueError("Unsupported preview dimensions")
        if preserve and source.mode in {"L", "RGB"} and "comment" not in source.info and all(
            marker == "APP0" and len(data) == 14 and data.startswith(b"JFIF\0") and data[-2:] == b"\0\0"
            for marker, data in source.applist
        ):
            source.load()
            return payload
        result = io.BytesIO()
        source.convert("RGB").save(result, format="JPEG", quality=75)
        frame = result.getvalue()
    if len(frame) > MAX_FRAME_BYTES:
        raise ValueError("Preview exceeds size limit")
    return frame


def authenticate_device(store, headers, house_id=None):
    device = headers.get("X-Device-ID", "")
    scheme, _, token = headers.get("Authorization", "").partition(" ")
    if (DEVICE_ID.fullmatch(device) and scheme.lower() == "bearer" and 32 <= len(token) <= 128
            and store.authenticates(device, digest(token.encode()), house_id)):
        return device
    return None


def create_live_blueprint(store):
    blueprint = Blueprint("live", __name__)
    previews = LivePreviews()
    stream_slots = BoundedSemaphore(MAX_STREAMS)

    @blueprint.record_once
    def register_live_state(state):
        state.app.extensions["rental_live"] = (store, previews)

    def operator():
        actor = require_roles("owner", "manager")
        if actor["role"] == "manager" and not actor["house_id"]:
            abort(403)
        return actor

    def authorize_viewer(device):
        actor = operator()
        with store.connect() as db:
            row = db.execute("SELECT house_id FROM devices WHERE id=?", (device,)).fetchone()
        if row is None or not scoped_house(actor, row["house_id"]):
            abort(404)
        csrf = session.get("csrf")
        if not csrf:
            abort(403)
        return str(actor["id"]), csrf

    def authenticated_device():
        device = authenticate_device(store, request.headers, current_app.config.get("RENTAL_HOUSE_A_ID"))
        if device is None:
            abort(401)
        return device

    @blueprint.get("/camera")
    def index():
        actor = operator()
        house = actor["house_id"]
        with store.connect() as db:
            devices = db.execute(
                """SELECT devices.id, houses.name FROM devices JOIN houses ON houses.id=devices.house_id
                   WHERE (? IS NULL OR devices.house_id=?) ORDER BY houses.name, devices.id""", (house, house),
            ).fetchall()
        return render_template("live_camera.html", devices=devices)

    @blueprint.post("/camera/<device>/start")
    def start(device):
        viewer = authorize_viewer(device)
        token, status = previews.start(device, viewer)
        return ({"viewer": token}, status) if token else ({"error": "camera_busy"}, status)

    @blueprint.post("/camera/<device>/snapshot")
    def snapshot(device):
        authorize_viewer(device)
        frame, status = previews.snapshot(device)
        if frame is None:
            return "", status
        return Response(frame, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @blueprint.post("/camera/<device>/flash")
    def direct_flash(device):
        authorize_viewer(device)
        status, command = queue_direct_flash(store, device, actor=operator())
        if command is None:
            abort(404)
        return {
            "status": status,
            "status_url": url_for("live.direct_status", device=device, command=command),
        }, 202

    @blueprint.get("/camera/<device>/flash/<command>")
    def direct_status(device, command):
        authorize_viewer(device)
        status = direct_flash_status(store, device, command)
        if status is None:
            abort(404)
        return {"status": status}

    @blueprint.post("/camera/<device>/renew")
    def renew(device):
        viewer = authorize_viewer(device)
        _, _, status = previews.viewer(device, viewer, request.headers.get("X-Live-Viewer", ""), action="renew")
        return "", status

    @blueprint.post("/camera/<device>/stop")
    def stop(device):
        viewer = authorize_viewer(device)
        previews.viewer(device, viewer, request.headers.get("X-Live-Viewer", ""), action="stop")
        return "", 204

    @blueprint.get("/camera/<device>/frame")
    def frame(device):
        viewer = authorize_viewer(device)
        jpeg, number, status = previews.viewer(device, viewer, request.headers.get("X-Live-Viewer", ""),
                                               previous_frame=request.headers.get("X-Live-Frame", ""))
        if jpeg is None:
            return "", status
        return Response(jpeg, mimetype="image/jpeg", headers={"X-Live-Frame": number})

    @blueprint.get("/camera/<device>/stream")
    def stream(device):
        viewer = authorize_viewer(device)
        token = request.headers.get("X-Live-Viewer", "")
        _, _, status = previews.viewer(device, viewer, token)
        if status == 409:
            return "", status
        if not stream_slots.acquire(blocking=False):
            return "", 429
        response = Response(previews.stream(device, viewer, token), mimetype="application/octet-stream",
                            headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"})
        response.call_on_close(stream_slots.release)
        return response

    @blueprint.get("/api/device/live/demand")
    def device_demand():
        device = authenticated_device()
        channel = previews.demand(device)
        response = Response(channel or '', status=200 if channel else 204, mimetype="text/plain")
        if request.method == 'GET' and request.headers.get('X-Doorbell-Flash-Capable') == '1':
            command = claim_flash(store, device)
            if command:
                response.headers['X-Doorbell-Flash'] = command
        return response

    @blueprint.post('/api/device/flash/<command_id>/ack')
    def device_flash_ack(command_id):
        device = authenticated_device()
        if not acknowledge_flash(store, device, command_id):
            abort(404)
        return '', 204

    @blueprint.post("/api/device/live/frame")
    def device_frame():
        device = authenticated_device()
        channel = request.headers.get("X-Live-Stream", "")
        status = previews.publish(device, channel)
        if status != 204:
            return "", status
        if request.mimetype != "image/jpeg":
            return "", 415
        # Read one extra byte so a chunked body cannot be silently truncated at the limit.
        request.max_content_length = MAX_FRAME_BYTES + 1
        payload = request.get_data(cache=False)
        if len(payload) > MAX_FRAME_BYTES:
            return "", 413
        try:
            frame = validate_frame(payload)
        except (OSError, ValueError, Image.DecompressionBombError):
            return "", 400
        return "", previews.publish(device, channel, frame)

    return blueprint
