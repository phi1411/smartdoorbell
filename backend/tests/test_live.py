import io
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from flask import Flask, g, request
from PIL import Image

from rental import live
from rental.store import RentalStore


def jpeg(size=(640, 480)):
    data = io.BytesIO()
    Image.new("RGB", size, "white").save(data, format="JPEG")
    return data.getvalue()


@pytest.fixture
def camera(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(live, "monotonic", lambda: now[0])
    store = RentalStore(tmp_path / "live.sqlite")
    store.initialize()
    with store.connect() as db:
        db.executemany("INSERT INTO houses(id, name) VALUES (?, ?)", [(1, "Dãy A"), (2, "Dãy B")])
    token = store.register_device(1, "doorbell-01")
    store.register_device(2, "doorbell-02")
    app = Flask(__name__, template_folder="../rental/templates")
    app.config.update(SECRET_KEY="test-live", TESTING=True)

    @app.before_request
    def demo_actor():
        role = request.headers.get("X-Test-Role", "owner")
        g.actor = {"role": role, "house_id": 1, "id": 5, "username": "demo"}

    app.register_blueprint(live.create_live_blueprint(store))
    client = app.test_client()
    with client.session_transaction() as session:
        session["csrf"] = "viewer-session"
    headers = {"Authorization": f"Bearer {token}", "X-Device-ID": "doorbell-01"}
    return store, client, headers, now


def open_camera(client, headers):
    session = client.post("/camera/doorbell-01/start")
    assert session.status_code == 200
    viewer = {"X-Live-Viewer": session.json["viewer"]}
    demand = client.get("/api/device/live/demand", headers=headers)
    assert demand.status_code == 200
    publisher = {**headers, "X-Live-Stream": demand.get_data(as_text=True)}
    return viewer, publisher


def test_demand_only_after_operator_starts_and_discards_frames(camera):
    store, client, headers, _ = camera
    assert client.get("/api/device/live/demand").status_code == 401
    assert client.get("/api/device/live/demand", headers=headers).status_code == 204
    assert client.post("/api/device/live/frame", headers=headers,
                       data=jpeg(), content_type="image/jpeg").status_code == 409
    viewer, publisher = open_camera(client, headers)
    assert client.get("/camera/doorbell-01/frame", headers=viewer).status_code == 204
    assert client.post("/api/device/live/frame", headers=publisher,
                       data=jpeg(), content_type="image/jpeg").status_code == 204
    frame = client.get("/camera/doorbell-01/frame", headers=viewer)
    assert frame.content_type == "image/jpeg" and frame.data.startswith(b"\xff\xd8")
    assert client.get("/camera/doorbell-01/frame", headers={**viewer, "X-Live-Frame": "1"}).status_code == 204
    assert client.post("/api/device/live/frame", headers=publisher,
                       data=jpeg(), content_type="image/jpeg").status_code == 429
    assert client.post("/camera/doorbell-01/stop", headers=viewer).status_code == 204
    assert client.get("/camera/doorbell-01/frame", headers=viewer).status_code == 409
    assert client.get("/api/device/live/demand", headers=headers).status_code == 204
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == 0


def test_preview_authorization_is_scoped_to_house_and_viewer(camera):
    _, client, headers, _ = camera
    assert client.post("/camera/doorbell-01/start", headers={"X-Test-Role": "tenant"}).status_code == 403
    assert client.post("/camera/doorbell-02/start", headers={"X-Test-Role": "manager"}).status_code == 404
    viewer, publisher = open_camera(client, headers)
    assert client.get("/camera/doorbell-02/frame", headers={**viewer, "X-Test-Role": "manager"}).status_code == 404
    assert client.post("/api/device/live/frame", headers={**publisher, "X-Device-ID": "doorbell-02"},
                       data=jpeg(), content_type="image/jpeg").status_code == 401
    second = client.application.test_client()
    with second.session_transaction() as session:
        session["csrf"] = "another-session"
    assert second.post("/camera/doorbell-01/start").status_code == 409
    assert second.get("/camera/doorbell-01/frame", headers=viewer).status_code == 409


@pytest.mark.parametrize("watching", [False, True])
def test_snapshot_waits_for_a_fresh_frame_without_creating_a_visit(camera, watching):
    store, operator, headers, now = camera
    device = operator.application.test_client()
    previews = operator.application.extensions["rental_live"][1]
    endpoint = "/camera/doorbell-01/snapshot"
    assert operator.post(endpoint, headers={"X-Test-Role": "tenant"}).status_code == 403
    assert operator.post("/camera/doorbell-02/snapshot", headers={"X-Test-Role": "manager"}).status_code == 404
    if watching:
        _, publisher = open_camera(operator, headers)
        assert device.post("/api/device/live/frame", headers=publisher,
                           data=jpeg((320, 240)), content_type="image/jpeg").status_code == 204
        now[0] += 1

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(operator.post, endpoint)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with previews.lock:
                preview = previews.previews.get("doorbell-01")
                if preview and preview.snapshot_expires:
                    break
            time.sleep(0.005)
        else:
            now[0] += live.SNAPSHOT_SECONDS + 1
            with previews.changed:
                previews.changed.notify_all()
            pytest.fail("Snapshot request did not register")
        assert not pending.done()
        channel = device.get("/api/device/live/demand", headers=headers).get_data(as_text=True)
        assert len(channel) == 32
        assert device.post("/api/device/live/frame", headers={**headers, "X-Live-Stream": channel},
                           data=jpeg((640, 480)), content_type="image/jpeg").status_code == 204
        result = pending.result(timeout=2)
    assert result.status_code == 200 and result.headers["Cache-Control"] == "no-store"
    with Image.open(io.BytesIO(result.data)) as shot:
        assert shot.size == (640, 480)
    assert device.get("/api/device/live/demand", headers=headers).status_code == (200 if watching else 204)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == 0


def test_stream_delivers_two_frames_and_closes_when_viewer_stops(camera):
    _, client, headers, now = camera
    viewer, publisher = open_camera(client, headers)
    assert client.get("/camera/doorbell-01/stream").status_code == 409
    assert client.get("/camera/doorbell-01/stream", headers={**viewer, "X-Test-Role": "tenant"}).status_code == 403

    def publish(size):
        response = client.post("/api/device/live/frame", headers=publisher,
                               data=jpeg(size), content_type="image/jpeg")
        assert response.status_code == 204

    with client.get("/camera/doorbell-01/stream", headers=viewer, buffered=False) as stream:
        assert stream.content_type == "application/octet-stream"
        chunks = iter(stream.response)
        assert next(chunks) == b"\x00" * 4
        for size in ((320, 240), (800, 600)):
            publish(size)
            frame = next(chunks)
            assert int.from_bytes(frame[:4], "big") == len(frame[4:])
            with Image.open(io.BytesIO(frame[4:])) as image:
                assert image.size == size
            now[0] += 0.3
        assert client.post("/camera/doorbell-01/stop", headers=viewer).status_code == 204
        assert next(chunks, None) is None


def test_preview_rejects_bad_payload_and_expires_without_renewal(camera):
    _, client, headers, now = camera
    viewer, publisher = open_camera(client, headers)

    def post(data, content_type):
        return client.post("/api/device/live/frame", headers=publisher, data=data, content_type=content_type)

    assert post(b"bad", "text/plain").status_code == 415
    assert post(b"x" * (live.MAX_FRAME_BYTES + 1), "image/jpeg").status_code == 413
    assert post(jpeg((801, 600)), "image/jpeg").status_code == 400
    assert post(b"\xff\xd8invalid jpeg", "image/jpeg").status_code == 400
    assert post(jpeg(), "image/jpeg").status_code == 204
    now[0] += live.FRAME_SECONDS + 0.1
    assert client.get("/camera/doorbell-01/frame", headers=viewer).status_code == 204
    assert client.post("/camera/doorbell-01/renew", headers=viewer).status_code == 204
    now[0] += live.LEASE_SECONDS - 0.5
    assert client.get("/api/device/live/demand", headers=headers).status_code == 200
    now[0] += 1
    assert client.get("/api/device/live/demand", headers=headers).status_code == 204
    assert post(jpeg(), "image/jpeg").status_code == 409


def test_preview_clears_ram_at_frame_and_lease_deadlines_without_requests(monkeypatch):
    class ManualTimer:
        def __init__(self, interval, callback):
            self.interval = interval
            self.callback = callback

        def start(self):
            pass

        def cancel(self):
            pass

    now = [100.0]
    monkeypatch.setattr(live, "monotonic", lambda: now[0])
    monkeypatch.setattr(live, "Timer", ManualTimer)
    previews = live.LivePreviews()
    device = "doorbell-01"
    previews.start(device, ("owner", "session"))
    lease_timer = previews.cleanup
    assert lease_timer.interval == live.LEASE_SECONDS

    now[0] += 1
    previews.publish(device, previews.demand(device), jpeg())
    frame_timer = previews.cleanup
    assert frame_timer.interval == live.FRAME_SECONDS
    lease_timer.callback()
    assert previews.cleanup is frame_timer

    now[0] += live.FRAME_SECONDS
    frame_timer.callback()
    assert previews.previews[device].frame is None
    assert previews.cleanup.interval == live.LEASE_SECONDS - 1 - live.FRAME_SECONDS

    now[0] = 100 + live.LEASE_SECONDS
    previews.cleanup.callback()
    assert previews.previews == {}
    assert previews.cleanup is None


def test_real_app_allows_device_token_and_requires_browser_csrf(camera):
    from rental.app import create_app

    store, _, headers, _ = camera
    password = "test-only owner password"
    client = create_app(store, password).test_client()
    assert client.get("/camera").status_code == 302
    assert client.get("/api/device/live/demand", headers=headers).status_code == 204
    client.get("/login")
    with client.session_transaction() as session:
        csrf = session["csrf"]
    assert client.post("/login", data={"password": password, "csrf": csrf}).status_code == 303
    assert b"doorbell-01" in client.get("/camera").data
    assert client.post("/camera/doorbell-01/start").status_code == 403
    with client.session_transaction() as session:
        csrf = session["csrf"]
    opened = client.post("/camera/doorbell-01/start", headers={"X-CSRF-Token": csrf})
    assert opened.status_code == 200
    assert opened.headers["Cache-Control"] == "no-store"
    assert client.get("/api/device/live/demand", headers=headers).status_code == 200
