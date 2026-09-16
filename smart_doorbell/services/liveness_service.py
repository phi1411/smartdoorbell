from __future__ import annotations

import math
import threading
import time
from abc import ABC, abstractmethod

import cv2
import numpy as np


class LivenessProvider(ABC):
    @abstractmethod
    def evaluate(self, face_gray: np.ndarray, bbox: tuple[int, int, int, int]) -> dict:
        raise NotImplementedError

    @abstractmethod
    def reset(self) -> None:
        raise NotImplementedError


class BasicLivenessProvider(LivenessProvider):
    """Lightweight temporal heuristic; intentionally replaceable by a trained model.

    REAL requires a deliberate open -> closed -> open eye sequence. Head/phone
    movement is diagnostic only and can never prove liveness by itself. A face that
    does not complete the blink challenge becomes FAKE after a grace period. This is
    suitable for classroom demos, not security-grade presentation-attack detection.
    """

    def __init__(self, clock=time.monotonic, grace_seconds: float = 4.0):
        self.clock = clock
        self.grace_seconds = grace_seconds
        self._eye_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_eye_tree_eyeglasses.xml"
        )
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "_lock", threading.RLock()):
            self._started_at: float | None = None
            self._last_center: tuple[float, float] | None = None
            self._blink_stage = "waiting_open"
            self._open_streak = 0
            self._closed_streak = 0
            self._blink_verified = False
            self._open_eye_reference: np.ndarray | None = None
            self._closed_eye_reference: np.ndarray | None = None
            self._real_until = 0.0

    @staticmethod
    def _blink_region_change(
        reference: np.ndarray | None, current: np.ndarray
    ) -> tuple[float, float, bool]:
        if reference is None or reference.shape != current.shape:
            return 0.0, 0.0, False
        height = current.shape[0]
        eye_start, eye_end = int(height * 0.18), int(height * 0.56)
        lower_start, lower_end = int(height * 0.60), int(height * 0.90)
        difference = cv2.absdiff(reference, current).astype(np.float32)
        eye_change = float(difference[eye_start:eye_end].mean())
        lower_change = float(difference[lower_start:lower_end].mean())
        # A real blink changes the eye area substantially more than the lower face.
        # Phone movement/screen flicker tends to alter the whole crop together.
        localized = eye_change >= 7.0 and eye_change >= (lower_change * 1.35 + 2.0)
        return eye_change, lower_change, localized

    def new_session(self) -> "BasicLivenessProvider":
        """Create independent temporal state for another face track."""
        return BasicLivenessProvider(
            clock=self.clock,
            grace_seconds=self.grace_seconds,
        )

    def evaluate(self, face_gray: np.ndarray, bbox: tuple[int, int, int, int]) -> dict:
        now = self.clock()
        x, y, width, height = bbox
        center = (x + width / 2, y + height / 2)
        eyes = self._eye_cascade.detectMultiScale(
            face_gray[: int(face_gray.shape[0] * 0.65)], scaleFactor=1.1, minNeighbors=4,
            minSize=(14, 14)
        )
        eye_count = len(eyes)
        texture = float(cv2.Laplacian(face_gray, cv2.CV_64F).var())
        with self._lock:
            if self._started_at is None:
                self._started_at = now
            motion = 0.0
            if self._last_center is not None:
                motion = math.dist(center, self._last_center) / max(width, height, 1)
            self._last_center = center

            # A phone can be moved exactly like a head, so motion is never enough
            # to establish REAL. Requiring consecutive observations also prevents
            # a single unstable Haar-eye detection from looking like a blink.
            eye_change = 0.0
            lower_change = 0.0
            blink_change_valid = False
            if eye_count >= 1:
                self._open_streak += 1
                self._closed_streak = 0
                if self._blink_stage == "waiting_open" and self._open_streak >= 2:
                    self._blink_stage = "waiting_closed"
                    self._open_eye_reference = face_gray.copy()
                elif self._blink_stage == "waiting_closed" and self._open_streak >= 2:
                    # Refresh the open reference while eyes remain visible so small
                    # head movements do not masquerade as the closed part of a blink.
                    self._open_eye_reference = face_gray.copy()
                elif self._blink_stage == "waiting_reopen":
                    eye_change, lower_change, blink_change_valid = self._blink_region_change(
                        self._closed_eye_reference, face_gray
                    )
                    if self._open_streak >= 2 and blink_change_valid:
                        self._blink_verified = True
                        self._real_until = now + 8.0
                        self._blink_stage = "waiting_closed"
                        self._open_eye_reference = face_gray.copy()
                        self._closed_eye_reference = None
                        self._open_streak = 0
            else:
                self._closed_streak += 1
                self._open_streak = 0
                if self._blink_stage == "waiting_closed":
                    eye_change, lower_change, blink_change_valid = self._blink_region_change(
                        self._open_eye_reference, face_gray
                    )
                    if self._closed_streak >= 2 and blink_change_valid:
                        self._blink_stage = "waiting_reopen"
                        self._closed_eye_reference = face_gray.copy()
                        self._closed_streak = 0

            elapsed = now - self._started_at
            if self._blink_verified and now <= self._real_until:
                status = "real"
            elif elapsed >= self.grace_seconds:
                status = "fake"
            else:
                status = "pending"
        return {
            "status": status,
            "score": round(min(1.0, texture / 300.0), 3),
            "signals": {
                "eyes": eye_count,
                "motion": round(motion, 4),
                "texture": round(texture, 1),
                "blink_stage": self._blink_stage,
                "blink_verified": self._blink_verified,
                "eye_change": round(eye_change, 2),
                "lower_change": round(lower_change, 2),
                "blink_change_valid": blink_change_valid,
            },
        }
