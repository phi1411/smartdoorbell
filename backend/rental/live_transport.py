import asyncio
import logging
import sqlite3
from time import monotonic

import uvicorn
from a2wsgi import WSGIMiddleware
from PIL import Image
from werkzeug.datastructures import Headers
from werkzeug.exceptions import RequestEntityTooLarge

from rental.app import MAX_WEB_REQUEST_BYTES
from rental.live import MAX_FRAME_BYTES, authenticate_device, validate_frame


DEVICE_STREAM_PATH = "/api/device/live/ws"
STREAM_HEADERS = ("Authorization", "X-Device-ID", "X-Live-Stream")
logger = logging.getLogger("uvicorn.error")


async def receive_camera(store, previews, headers, receive, send, *, house_id=None):
    if (await receive())["type"] != "websocket.connect":
        return
    device = None
    if all(len(headers.getlist(name)) == 1 for name in STREAM_HEADERS):
        try:
            device = await asyncio.to_thread(authenticate_device, store, headers, house_id)
        except sqlite3.OperationalError:
            await send({"type": "websocket.close", "code": 1013})
            return
    channel = headers.get("X-Live-Stream", "")
    publisher = previews.claim_publisher(device, channel) if device else None
    if publisher is None:
        await send({"type": "websocket.close", "code": 1008})
        return

    started = monotonic()
    accepted_frames = dropped_frames = invalid_bytes = 0
    close_code = 1006
    try:
        await send({"type": "websocket.accept"})
        while True:
            try:
                event = await asyncio.wait_for(receive(), timeout=1.0)
            except asyncio.TimeoutError:
                event = None
            if event and event["type"] == "websocket.disconnect":
                close_code = event.get("code", 1006)
                return
            status = previews.publish(device, channel, publisher=publisher)
            if status == 409:
                close_code = 1008
                await send({"type": "websocket.close", "code": close_code})
                return
            if event is None:
                continue
            frame = event.get("bytes")
            if frame is None:
                close_code = 1003
                await send({"type": "websocket.close", "code": close_code})
                return
            if len(frame) > MAX_FRAME_BYTES:
                invalid_bytes = len(frame)
                close_code = 1009
                await send({"type": "websocket.close", "code": close_code})
                return

            if status == 429:
                dropped_frames += 1
                continue
            frame_size = len(frame)
            try:
                frame = await asyncio.to_thread(validate_frame, frame, preserve=True)
            except (OSError, ValueError, Image.DecompressionBombError):
                invalid_bytes = frame_size
                close_code = 1003
                await send({"type": "websocket.close", "code": close_code})
                return
            if previews.publish(device, channel, frame, publisher=publisher) == 204:
                accepted_frames += 1
    finally:
        previews.release_publisher(device, publisher)
        logger.info("Camera WebSocket ended code=%d accepted=%d rate_limited=%d invalid_bytes=%d seconds=%.1f",
                    close_code, accepted_frames, dropped_frames, invalid_bytes, monotonic() - started)


def create_asgi_app(app):
    store, previews = app.extensions["rental_live"]
    house_id = app.config.get("RENTAL_HOUSE_A_ID")

    def wsgi_app(environ, start_response):
        if not environ.get("CONTENT_LENGTH"):
            environ["wsgi.input_terminated"] = True
        return app(environ, start_response)

    http = WSGIMiddleware(wsgi_app, workers=4, send_queue_size=2)

    async def application(scope, receive, send):
        if scope["type"] == "websocket" and scope["path"] == DEVICE_STREAM_PATH:
            headers = Headers((name.decode("latin-1"), value.decode("latin-1"))
                              for name, value in scope["headers"])
            return await receive_camera(store, previews, headers, receive, send, house_id=house_id)
        if scope["type"] != "http":
            return await http(scope, receive, send)

        if any(name == b"content-length" and value.isdigit() and int(value) > MAX_WEB_REQUEST_BYTES
               for name, value in scope["headers"]):
            await send({"type": "http.response.start", "status": 413, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return

        received = 0

        async def receive_limited():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > MAX_WEB_REQUEST_BYTES:
                raise RequestEntityTooLarge()
            return message

        return await http(scope, receive_limited, send)

    return application


def serve_web(app, host, port):
    uvicorn.run(create_asgi_app(app), host=host, port=port, workers=1, ws="websockets",
                ws_max_size=MAX_FRAME_BYTES, ws_max_queue=2, ws_ping_interval=10, ws_ping_timeout=10,
                ws_per_message_deflate=False)
