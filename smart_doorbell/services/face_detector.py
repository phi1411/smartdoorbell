from __future__ import annotations

import cv2
import numpy as np


class FaceDetector:
    FACE_SIZE = (160, 160)

    def __init__(self):
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        self._cascade = cv2.CascadeClassifier(cascade_path)
        if self._cascade.empty():
            raise RuntimeError("OpenCV face detector model could not be loaded")

    def detect(self, frame: np.ndarray, min_size: int = 80) -> list[tuple[int, int, int, int]]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        boxes = self._cascade.detectMultiScale(
            gray, scaleFactor=1.12, minNeighbors=5, minSize=(min_size, min_size)
        )
        return [tuple(int(value) for value in box) for box in boxes]

    def crop(self, frame: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
        x, y, width, height = box
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        face = gray[max(0, y):y + height, max(0, x):x + width]
        if face.size == 0:
            raise ValueError("Invalid face crop")
        face = cv2.resize(face, self.FACE_SIZE, interpolation=cv2.INTER_AREA)
        return cv2.equalizeHist(face)

    @staticmethod
    def blur_score(face: np.ndarray) -> float:
        return float(cv2.Laplacian(face, cv2.CV_64F).var())

