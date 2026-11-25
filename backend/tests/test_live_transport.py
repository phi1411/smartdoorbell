import io
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from threading import Thread
from time import monotonic, sleep
from urllib.parse import urlsplit

import pytest
import requests
import uvicorn
from PIL import Image
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.sync.client import connect

from rental import live
from rental.app import MAX_WEB_REQUEST_BYTES, create_app
from rental.live_transport import DEVICE_STREAM_PATH, create_asgi_app
from rental.store import RentalStore


PASSWORD = "test-only owner password"
DEVICE = "doorbell-01"


@pytest.fixture
def service(tmp_path):
    store = RentalStore(tmp_path / "stream.sqlite")
    store.initialize()
    with store.connect() as db:
        db.execute("INSERT INTO houses(id, name) VALUES(1, 'Camera test')")
    key = store.register_device(1, DEVICE)
    app = create_app(store, PASSWORD)
    config = uvicorn.Config(create_asgi_app(app), ws="websockets", log_level="critical",
                            ws_max_size=live.MAX_FRAME_BYTES, ws_max_queue=2,
                            ws_per_message_deflate=False, timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 4
            while not server.started and thread.is_alive() and monotonic() < deadline:
                sleep(0.01)
            assert server.started
            headers = {"Authorization": f"Bearer {key}", "X-Device-ID": DEVICE}
            yield f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}{DEVICE_STREAM_PATH}", headers
        finally:
            server.should_exit = True
            thread.join(timeout=5)


def start_lease(session, base, device_headers):
    camera = f"{base}/camera/{DEVICE}"
    page = session.get(f"{base}/camera", allow_redirects=False, timeout=3)
    if page.status_code == 302:
        login = session.get(f"{base}/login", timeout=3)
        csrf = re.search(r'name="csrf" value="([^"]+)"', login.text)[1]
        logged_in = session.post(f"{base}/login", data={"password": PASSWORD, "csrf": csrf},
                                 allow_redirects=False, timeout=3)
        assert logged_in.status_code == 303
        page = session.get(f"{base}/camera", timeout=3)
    assert page.status_code == 200
    csrf = re.search(r'data-csrf="([^"]+)"', page.text)[1]
    browser_headers = {"X-CSRF-Token": csrf}
    lease = session.post(f"{camera}/start", headers=browser_headers, timeout=3)
    assert lease.status_code == 200
    browser_headers["X-Live-Viewer"] = lease.json()["viewer"]
    demand = requests.get(f"{base}/api/device/live/demand", headers=device_headers, timeout=3)
    assert demand.status_code == 200
    return camera, browser_headers, {**device_headers, "X-Live-Stream": demand.text}


def jpeg():
    stream = io.BytesIO()
    Image.new("RGB", (640, 480), "white").save(stream, format="JPEG", quality=91)
    return stream.getvalue()


def test_device_ws_delivers_original_jpeg_to_http_stream(service):
    base, ws_url, headers = service
    with requests.Session() as session:
        camera, viewer, publisher = start_lease(session, base, headers)
        with pytest.raises(InvalidStatus) as invalid:
            connect(ws_url, additional_headers={**publisher, "Authorization": "Bearer invalid"}, open_timeout=3)
        assert invalid.value.response.status_code == 403
        with connect(ws_url, additional_headers=publisher, open_timeout=3) as ws:
            with pytest.raises(InvalidStatus):
                connect(ws_url, additional_headers=publisher, open_timeout=3)
            with session.get(f"{camera}/stream", headers=viewer, stream=True, timeout=3) as feed:
                assert feed.status_code == 200
                assert feed.headers["Cache-Control"] == "no-store, no-transform"
                sample = jpeg()
                ws.send([sample[offset:offset + 2048] for offset in range(0, len(sample), 2048)])
                for _ in range(4):
                    length = int.from_bytes(feed.raw.read(4), "big")
                    if length:
                        break
                assert length == len(sample)
                assert feed.raw.read(length) == sample
                assert session.get(f"{base}/healthz", timeout=3).status_code == 200
                assert session.post(f"{camera}/stop", headers=viewer, timeout=3).status_code == 204
                with pytest.raises(ConnectionClosedError) as expired:
                    ws.recv(timeout=3)
                assert expired.value.rcvd.code == 1008
                assert not feed.raw.read().strip(b"\x00")
        with pytest.raises(InvalidStatus):
            connect(ws_url, additional_headers=publisher, open_timeout=3)


def test_device_ws_releases_bad_publishers_and_closes_expired_lease(service, monkeypatch):
    base, ws_url, headers = service
    with requests.Session() as session:
        _, _, publisher = start_lease(session, base, headers)
        with connect(ws_url, additional_headers=publisher, open_timeout=3) as ws:
            ws.send(b"\xff\xd8invalid-jpeg\xff\xd9")
            with pytest.raises(ConnectionClosedError) as malformed:
                ws.recv(timeout=3)
            assert malformed.value.rcvd.code == 1003
        with connect(ws_url, additional_headers=publisher, open_timeout=3) as ws:
            ws.send(b"x" * (live.MAX_FRAME_BYTES + 1))
            with pytest.raises(ConnectionClosedError) as oversized:
                ws.recv(timeout=3)
            assert oversized.value.rcvd.code == 1009

        monkeypatch.setattr(live, "LEASE_SECONDS", 1.5)
        _, _, short_lease = start_lease(session, base, headers)
        with connect(ws_url, additional_headers=short_lease, open_timeout=3) as ws:
            with pytest.raises(ConnectionClosedError) as expired:
                ws.recv(timeout=4)
            assert expired.value.rcvd.code == 1008


def test_standalone_photo_and_flash_share_idle_firmware_connection(service):
    base, ws_url, headers = service
    demand_url = f"{base}/api/device/live/demand"
    with requests.Session() as session:
        camera, viewer, _ = start_lease(session, base, headers)
        assert session.post(f"{camera}/stop", headers=viewer, timeout=3).status_code == 204
        assert requests.get(demand_url, headers=headers, timeout=3).status_code == 204
        opening = session.post(f"{camera}/flash", headers=viewer, timeout=3)
        assert opening.status_code == 202
        with ThreadPoolExecutor(max_workers=1) as pool:
            photo = pool.submit(session.post, f"{camera}/snapshot", headers=viewer, timeout=5)
            deadline = monotonic() + 2
            while monotonic() < deadline:
                demand = requests.get(demand_url, headers=headers, timeout=3)
                if demand.status_code == 200:
                    break
                sleep(0.01)
            else:
                pytest.fail("Snapshot did not activate the idle camera")
            device = {**headers, "X-Doorbell-Flash-Capable": "1"}
            instruction = requests.get(demand_url, headers=device, timeout=3)
            assert instruction.text == demand.text
            command = instruction.headers["X-Doorbell-Flash"]
            with connect(ws_url, additional_headers={**headers, "X-Live-Stream": demand.text},
                         open_timeout=3) as ws:
                sample = jpeg()
                ws.send(sample)
                assert photo.result(timeout=3).content == sample
            assert requests.post(f"{base}/api/device/flash/{command}/ack", headers=headers, timeout=3).status_code == 204
        assert session.get(base + opening.json()["status_url"], timeout=3).json() == {"status": "confirmed"}
        assert requests.get(demand_url, headers=device, timeout=3).status_code == 204


def test_asgi_preserves_legacy_posts_and_request_limits(service):
    base, ws_url, headers = service
    with pytest.raises(InvalidStatus) as missing:
        connect(ws_url + "/no-route", additional_headers=headers, open_timeout=3)
    assert missing.value.response.status_code == 403

    path = "/api/device/live/frame"
    url = base + path
    with requests.Session() as session:
        _, _, publisher = start_lease(session, base, headers)
    publisher["Content-Type"] = "image/jpeg"

    def chunks(data):
        yield data[:1024]
        yield data[1024:]

    assert requests.post(url, data=chunks(b"x" * (live.MAX_FRAME_BYTES + 1)), headers=publisher,
                         timeout=3).status_code == 413
    assert requests.post(url, data=chunks(jpeg()), headers=publisher, timeout=3).status_code == 204

    destination = urlsplit(base)
    http = HTTPConnection(destination.hostname, destination.port, timeout=3)
    try:
        http.putrequest("POST", path)
        http.putheader("Content-Length", str(MAX_WEB_REQUEST_BYTES + 1))
        http.endheaders()
        assert http.getresponse().status == 413
    finally:
        http.close()
