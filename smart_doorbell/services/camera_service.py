from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class CameraService:
    """Owns the only VideoCapture instance and publishes copies of the newest frame."""

    def __init__(self, camera_index: int = 0, width: int = 640, height: int = 480):
        self.camera_index = camera_index
        self.width = width
        self.height = height
        self._frame: np.ndarray | None = None
        self._sequence = 0
        self._connected = False
        self._last_error: str | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._capture: cv2.VideoCapture | None = None

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="camera-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._release()

    def get_latest_frame(self) -> tuple[np.ndarray | None, int]:
        with self._lock:
            return (self._frame.copy() if self._frame is not None else None, self._sequence)

    def mjpeg_stream(self, frame_transform=None) -> Iterator[bytes]:
        last_sequence = -1
        while not self._stop.is_set():
            frame, sequence = self.get_latest_frame()
            if frame is None or sequence == last_sequence:
                time.sleep(0.04)
                continue
            last_sequence = sequence
            if frame_transform:
                frame = frame_transform(frame)
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
            if ok:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + encoded.tobytes() + b"\r\n"

    def _open(self) -> bool:
        backend = cv2.CAP_DSHOW if hasattr(cv2, "CAP_DSHOW") else cv2.CAP_ANY
        capture = cv2.VideoCapture(self.camera_index, backend)
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not capture.isOpened():
            capture.release()
            with self._lock:
                self._connected = False
                self._last_error = f"Cannot open camera index {self.camera_index}"
            return False
        self._capture = capture
        with self._lock:
            self._connected = True
            self._last_error = None
        logger.info("Camera connected (index=%s)", self.camera_index)
        return True

    def _release(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            capture.release()
        with self._lock:
            was_connected = self._connected
            self._connected = False
        if was_connected:
            logger.info("Camera disconnected")

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            if self._capture is None and not self._open():
                logger.warning("Camera unavailable; retrying")
                self._stop.wait(2)
                continue
            ok, frame = self._capture.read()
            if not ok or frame is None:
                failures += 1
                if failures >= 10:
                    with self._lock:
                        self._last_error = "Camera stopped returning frames"
                    logger.warning("Camera read failed; reconnecting")
                    self._release()
                    failures = 0
                self._stop.wait(0.05)
                continue
            failures = 0
            with self._lock:
                self._frame = frame
                self._sequence += 1
                self._connected = True
        self._release()

