"""Offline YuNet detection + SFace matching against a local SQLite gallery."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np

cv2.setNumThreads(0)

MODEL_DIR = Path(__file__).resolve().parent / "models"
DETECT_MODEL = MODEL_DIR / "yunet.onnx"
RECOG_MODEL = MODEL_DIR / "sface.onnx"

# YuNet defaults: 0.6 keeps real faces and drops background texture.
DETECT_THRESHOLD = 0.6
NMS_THRESHOLD = 0.3
TOP_K = 5000

# SFace cosine threshold from the OpenCV model card. 0.363 is the
# same-identity cutoff; raising it trades recall for precision.
MATCH_THRESHOLD = 0.363

MAX_SIDE = 1024  # downscale before detection; 1024 keeps 12 faces detectable at 2x the speed of 1600

_lock = threading.Lock()
_detector: Optional[cv2.FaceDetectorYN] = None
_recognizer: Optional[cv2.FaceRecognizerSF] = None
_det_size: tuple[int, int] = (0, 0)

_gallery: Optional[tuple[list[str], np.ndarray]] = None
_gallery_stamp: Optional[tuple[float, int]] = None


def _hermes_home() -> Path:
    return Path(os.environ.get("DOOR_DATA_DIR", "/opt/data"))


def db_path() -> Path:
    return _hermes_home() / "faceid.db"


def _connect() -> sqlite3.Connection:
    # The door service only reads a pre-enrolled gallery; never create or mutate it.
    return sqlite3.connect(f"file:{db_path()}?immutable=1", uri=True, timeout=10)


def _ensure_models() -> None:
    missing = [str(p) for p in (DETECT_MODEL, RECOG_MODEL) if not p.exists()]
    if missing:
        raise RuntimeError(
            "faceid models missing: " + ", ".join(missing)
            + " - download face_detection_yunet_2023mar.onnx and "
            "face_recognition_sface_2021dec.onnx into " + str(MODEL_DIR)
        )


def _detector_for(width: int, height: int) -> cv2.FaceDetectorYN:
    """Return the shared detector sized for this frame.

    setInputSize mutates shared state, so callers must hold _lock.
    """
    global _detector, _det_size
    if _detector is None:
        _ensure_models()
        _detector = cv2.FaceDetectorYN.create(
            str(DETECT_MODEL), "", (width, height), DETECT_THRESHOLD, NMS_THRESHOLD, TOP_K
        )
        _det_size = (width, height)
    elif _det_size != (width, height):
        _detector.setInputSize((width, height))
        _det_size = (width, height)
    return _detector


def _recognizer_singleton() -> cv2.FaceRecognizerSF:
    global _recognizer
    if _recognizer is None:
        _ensure_models()
        _recognizer = cv2.FaceRecognizerSF.create(str(RECOG_MODEL), "")
    return _recognizer


def warm() -> None:
    """Build both ONNX sessions so the first photo does not pay for it.

    Creating the detector and recognizer costs ~2 s cold (model parse +
    ORT thread pool). Doing it once at startup keeps a doorbell photo
    inside the ~110 ms steady-state budget.
    """
    _ensure_models()
    with _lock:
        _detector_for(320, 320)
    _recognizer_singleton()
    gallery()


def load_image(path: str) -> np.ndarray:
    """Read an image, tolerating non-ASCII Windows paths."""
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"image not found: {p}")
    buf = np.fromfile(str(p), dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"not a readable image: {p}")
    h, w = img.shape[:2]
    scale = MAX_SIDE / max(h, w)
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return img


def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


def detect(img: np.ndarray) -> np.ndarray:
    """Return an (N, 15) array of YuNet faces for a BGR image."""
    h, w = img.shape[:2]
    with _lock:
        det = _detector_for(w, h)
        _, faces = det.detect(img)
    if faces is None:
        return np.empty((0, 15), dtype=np.float32)
    return faces


def embed(img: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Embed each detected face; returns (N, 128) L2-normalized float32."""
    if len(faces) == 0:
        return np.empty((0, 128), dtype=np.float32)
    rec = _recognizer_singleton()
    out = np.empty((len(faces), 128), dtype=np.float32)
    for i, face in enumerate(faces):
        aligned = rec.alignCrop(img, face)
        out[i] = _normalize(rec.feature(aligned).reshape(-1).astype(np.float32))
    return out


def gallery() -> tuple[list[str], np.ndarray]:
    """Load (names, matrix) from SQLite, cached until the file changes."""
    global _gallery, _gallery_stamp
    path = db_path()
    try:
        st = path.stat()
        stamp = (st.st_mtime, st.st_size)
    except FileNotFoundError:
        return [], np.empty((0, 128), dtype=np.float32)

    if _gallery is not None and _gallery_stamp == stamp:
        return _gallery

    with _connect() as conn:
        rows = conn.execute("SELECT person, vector FROM embeddings").fetchall()
    if not rows:
        _gallery = ([], np.empty((0, 128), dtype=np.float32))
    else:
        names = [r[0] for r in rows]
        mat = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
        _gallery = (names, mat)
    _gallery_stamp = stamp
    return _gallery


def enrolled() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT person, COUNT(*), MIN(created_at) FROM embeddings GROUP BY person ORDER BY person"
        ).fetchall()
    return [{"person": r[0], "count": r[1], "created_at": r[2]} for r in rows]


def _close_peers(img: np.ndarray, faces: np.ndarray, vecs: np.ndarray) -> dict[int, list[dict[str, Any]]]:
    """For group photos, report which faces look like the same person.

    Turns an unidentified group shot into useful clusters ("faces 2 and 7
    are the same person") instead of a flat list of unknowns.
    """
    if len(faces) < 2:
        return {}
    sims = vecs @ vecs.T
    out: dict[int, list[dict[str, Any]]] = {}
    for i in range(len(faces)):
        peers = [
            {"index": j, "similarity": round(float(sims[i, j]), 3)}
            for j in range(len(faces))
            if j != i and sims[i, j] >= MATCH_THRESHOLD
        ]
        if peers:
            out[i] = sorted(peers, key=lambda p: -p["similarity"])
    return out


def recognize(image_path: str, threshold: float = MATCH_THRESHOLD) -> dict[str, Any]:
    """Detect, embed, and match every face in image_path against the gallery."""
    started = time.perf_counter()
    img = load_image(image_path)
    t_loaded = time.perf_counter()
    faces = detect(img)
    t_detected = time.perf_counter()
    vecs = embed(img, faces)
    t_embedded = time.perf_counter()

    names, mat = gallery()
    peers = _close_peers(img, faces, vecs)
    results = []

    for i, face in enumerate(faces):
        x, y, w, h = (int(v) for v in face[:4])
        entry: dict[str, Any] = {
            "index": i,
            "box": [x, y, w, h],
            "det_score": round(float(face[-1]), 3),
            "known": False,
            "name": None,
            "match": None,
        }
        if len(mat):
            scores = mat @ vecs[i]
            best = int(np.argmax(scores))
            best_score = float(scores[best])
            entry["match"] = round(best_score, 3)
            if best_score >= threshold:
                entry.update(
                    known=True,
                    name=names[best],
                    runner_up=round(float(np.sort(scores)[-2]), 3) if len(scores) > 1 else None,
                )
        if i in peers:
            entry["close_peers"] = peers[i]
        results.append(entry)

    elapsed = (time.perf_counter() - started) * 1000
    h, w = img.shape[:2]
    return {
        "ok": True,
        "image": str(image_path),
        "width": w,
        "height": h,
        "face_count": int(len(faces)),
        "known_count": sum(1 for r in results if r["known"]),
        "unknown_count": sum(1 for r in results if not r["known"]),
        "gallery_size": int(len(mat)),
        "threshold": threshold,
        "faces": results,
        "timing_ms": {
            "load": round((t_loaded - started) * 1000, 1),
            "detect": round((t_detected - t_loaded) * 1000, 1),
            "embed": round((t_embedded - t_detected) * 1000, 1),
            "match": round((elapsed - (t_embedded - started) * 1000), 1),
            "total": round(elapsed, 1),
        },
    }
