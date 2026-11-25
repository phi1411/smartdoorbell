import math
import os
import struct

import requests


MAX_SAMPLES = 5
VECTOR = struct.Struct("<128f")
MATCH_THRESHOLD = 0.45
MIN_MARGIN = 0.05
PAD_LABELS = {
    "passed": "Không phát hiện dấu hiệu giả mạo.",
    "suspected": "Nghi giả mạo.",
    "uncertain": "Chưa đủ cơ sở xác minh.",
    "no_face": "Không thấy khuôn mặt.",
    "multiple_faces": "Có nhiều khuôn mặt, cần kiểm tra thủ công.",
    "small_face": "Khuôn mặt quá nhỏ, chưa xác minh được.",
    "unavailable": "Chưa xử lý được ảnh.",
}


class FaceProfiles:
    def __init__(self, http=None):
        self.http = http if http is not None else requests
        self.url = os.getenv("RENTAL_FACE_EMBED_URL", "").strip()
        self.pad_url = os.getenv("RENTAL_FACE_PAD_URL", "").strip()
        self.key = os.getenv("RENTAL_FACE_API_KEY", "").strip()
        if (self.url or self.pad_url) and len(self.key) < 32:
            raise ValueError("An internal face model URL requires RENTAL_FACE_API_KEY (at least 32 characters).")

    def extract(self, jpeg):
        return self._request_faces(self.url, jpeg)[0]

    def analyze_visit(self, jpeg, samples, *, demo=False):
        faces, response = self._request_faces(self.pad_url, jpeg, demo=demo)
        pad = response.get("pad")
        if (not isinstance(pad, str) or pad not in PAD_LABELS
                or (pad in {"passed", "suspected", "uncertain", "small_face"} and len(faces) != 1)):
            pad = "unavailable"
        identity, stay = self.match_visit(faces, samples)
        return identity, stay, pad

    def _request_faces(self, url, jpeg, *, demo=False):
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "image/jpeg"}
        if demo:
            headers["X-Pad-Demo"] = "1"
        response = self.http.post(
            url, data=jpeg, timeout=(4, 15), allow_redirects=False,
            headers=headers,
        )
        response.raise_for_status()
        result = response.json()
        vectors = result.get("vectors") if isinstance(result, dict) else None
        if response.status_code != 200 or not isinstance(vectors, list) or len(vectors) > 16:
            raise ValueError("Invalid face model response")
        packed = []
        for vector in vectors:
            if (not isinstance(vector, list) or len(vector) != 128
                    or any(type(value) not in (int, float) or not -1.001 <= value <= 1.001 for value in vector)
                    or not 0.99 <= math.hypot(*vector) <= 1.01):
                raise ValueError("Invalid face vector")
            packed.append(VECTOR.pack(*vector))
        return packed, result

    def describe_visit(self, jpeg, samples):
        if not samples:
            return "Nhận diện: Dãy trọ chưa có mẫu khuôn mặt đang sử dụng."
        return self.match_visit(self.extract(jpeg), samples)[0]

    def match_visit(self, faces, samples):
        if not faces:
            return "Nhận diện: Không thấy khuôn mặt.", None
        if not samples:
            return "Nhận diện: Dãy trọ chưa có mẫu khuôn mặt đang sử dụng.", None
        gallery = [(row, VECTOR.unpack(row["vector"])) for row in samples]
        recognized = {}
        unknown_count = 0
        for face in faces:
            query = VECTOR.unpack(face)
            best_by_stay = {}
            for row, vector in gallery:
                score = math.fsum(left * right for left, right in zip(query, vector))
                if row["stay_id"] not in best_by_stay or score > best_by_stay[row["stay_id"]][0]:
                    best_by_stay[row["stay_id"]] = (score, row)
            ranked = sorted(best_by_stay.values(), key=lambda match: match[0], reverse=True)
            score, resident = ranked[0]
            if score < MATCH_THRESHOLD or (len(ranked) > 1 and score - ranked[1][0] < MIN_MARGIN):
                unknown_count += 1
                continue
            recognized[resident["stay_id"]] = f"{resident['name']} · Phòng {resident['code']}"
        lines = [f"Khớp mẫu: {label}" for label in recognized.values()]
        if unknown_count:
            lines.append(f"Chưa nhận diện được: {unknown_count} khuôn mặt.")
        stay = next(iter(recognized)) if len(faces) == 1 and recognized else None
        return "\n".join(lines), stay
