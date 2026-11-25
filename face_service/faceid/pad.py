"""Passive RGB presentation-attack classification using anti-spoof-mn3."""

from math import ceil, floor
from pathlib import Path
from threading import Lock

import cv2
import numpy as np


MODEL_PATH = Path(__file__).resolve().parent / "models" / "anti-spoof-mn3.onnx"
MIN_FACE_PIXELS = 80
LIVE_THRESHOLD = 0.95
DEMO_LIVE_THRESHOLD = 0.50
SPOOF_THRESHOLD = 0.90
RGB_MEAN = np.asarray([151.2405, 119.5950, 107.8395], dtype=np.float32)
RGB_SCALE = np.asarray([63.0105, 56.4570, 55.0035], dtype=np.float32)


class PassivePad:
    def __init__(self):
        self.lock = Lock()
        self.net = None

    def classify(self, pixels, faces, *, demo=False):
        if len(faces) != 1:
            return "no_face" if len(faces) == 0 else "multiple_faces"
        x, y, width, height = map(float, faces[0][:4])
        left, top = max(0, floor(x)), max(0, floor(y))
        right = min(pixels.shape[1], ceil(x + width))
        bottom = min(pixels.shape[0], ceil(y + height))
        if min(right - left, bottom - top) < MIN_FACE_PIXELS:
            return "small_face"
        crop = pixels[top:bottom, left:right]
        resized = cv2.resize(crop, (128, 128))
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32)
        blob = np.ascontiguousarray(((rgb - RGB_MEAN) / RGB_SCALE).transpose(2, 0, 1)[None])
        with self.lock:
            if self.net is None:
                self.net = cv2.dnn.readNetFromONNX(str(MODEL_PATH))
            self.net.setInput(blob)
            scores = self.net.forward().reshape(-1)
        if (scores.size != 2 or not np.isfinite(scores).all()
                or (scores < 0).any() or (scores > 1).any() or abs(float(scores.sum()) - 1) > 0.002):
            raise ValueError("Unexpected anti-spoof-mn3 output")
        if scores[1] >= SPOOF_THRESHOLD:
            return "suspected"
        threshold = DEMO_LIVE_THRESHOLD if demo else LIVE_THRESHOLD
        return "passed" if scores[0] >= threshold else "uncertain"
