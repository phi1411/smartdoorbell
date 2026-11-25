"""Internal HTTP interface for the doorbell face matcher."""

import hmac
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import Flask, request
from PIL import Image
from waitress import serve

from .faceid import face_core
from .faceid.pad import PassivePad


MAX_BYTES = 1024 * 1024  # The rental backend may re-encode a 512 KiB input.


def create_app(key: str) -> Flask:
    if len(key) < 32:
        raise ValueError("Set DOOR_FACE_API_KEY to a random key of at least 32 characters")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BYTES
    pad = PassivePad()

    @app.errorhandler(413)
    def image_too_large(_error):
        return {"error": "image_too_large"}, 413

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.post("/internal/recognize")
    @app.post("/internal/embeddings")
    @app.post("/internal/visit-analysis")
    def recognize():
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), key.encode()):
            return {"error": "unauthorized"}, 401
        if request.mimetype != "image/jpeg":
            return {"error": "expected_jpeg"}, 415
        body = request.get_data(cache=False)
        try:
            with Image.open(io.BytesIO(body), formats=["JPEG"]) as image:
                if not (32 <= image.width <= 1600 and 32 <= image.height <= 1200):
                    return {"error": "invalid_image_dimensions"}, 400
                image.load()
        except (OSError, ValueError, Image.DecompressionBombError):
            return {"error": "invalid_jpeg"}, 400

        try:
            with TemporaryDirectory() as folder:
                path = Path(folder) / "doorbell.jpg"
                path.write_bytes(body)
                if request.path in {"/internal/embeddings", "/internal/visit-analysis"}:
                    pixels = face_core.load_image(str(path))
                    faces = face_core.detect(pixels)
                    if len(faces) > 16:
                        return {"error": "too_many_faces"}, 422
                    result = {"vectors": face_core.embed(pixels, faces).tolist()}
                    if request.path == "/internal/visit-analysis":
                        try:
                            result["pad"] = pad.classify(
                                pixels, faces, demo=request.headers.get("X-Pad-Demo") == "1",
                            )
                        except Exception as exc:
                            app.logger.warning("Doorbell PAD failed: %s", type(exc).__name__)
                            result["pad"] = "unavailable"
                    return result
                result = face_core.recognize(str(path), threshold=0.45)
            if not result["gallery_size"]:
                return {"error": "face_gallery_unavailable"}, 503
            # Only share counts with the rental server, rather than raw matches or gallery records.
            return {field: result[field] for field in ("face_count", "known_count", "unknown_count")}
        except Exception as exc:
            app.logger.warning("Doorbell face match failed: %s", type(exc).__name__)
            return {"error": "recognition_unavailable"}, 503

    return app


if __name__ == "__main__":
    app = create_app(os.environ.get("DOOR_FACE_API_KEY", ""))
    face_core.warm()
    serve(app, host=os.getenv("DOOR_FACE_HOST", "127.0.0.1"),
          port=int(os.getenv("PORT", "5051")), threads=1, max_request_body_size=MAX_BYTES)
