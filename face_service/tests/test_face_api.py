import io
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
import numpy as np

from face_service import face_api


KEY = "face-api-test-key-" * 3


class FaceApiTests(unittest.TestCase):
    def setUp(self):
        self.client = face_api.create_app(KEY).test_client()
        out = io.BytesIO()
        Image.new("RGB", (640, 480), "white").save(out, "JPEG")
        self.image = out.getvalue()

    def post(self, data=None, key=KEY, content_type="image/jpeg"):
        return self.client.post("/internal/recognize", data=self.image if data is None else data,
                                content_type=content_type, headers={"Authorization": f"Bearer {key}"})

    def test_rejects_unauthorized_and_invalid_images(self):
        with patch.object(face_api.face_core, "recognize") as model:
            self.assertEqual(self.post(key="wrong").status_code, 401)
            self.assertEqual(self.post(data=b"not jpeg").status_code, 400)
            self.assertEqual(self.post(data=self.image[:1000]).status_code, 400)
            self.assertEqual(self.post(content_type="image/png").status_code, 415)
            self.assertEqual(self.post(data=b"x" * (face_api.MAX_BYTES + 1)).status_code, 413)
            model.assert_not_called()

    def test_reports_only_counts_and_removes_temp_image(self):
        def predict(filename, threshold):
            self.assertEqual(Path(filename).read_bytes(), self.image)
            self.assertEqual(threshold, 0.45)
            return {"face_count": 2, "known_count": 1, "unknown_count": 1,
                    "gallery_size": 1, "faces": [{"name": "Sample"}]}

        with patch.object(face_api.face_core, "recognize", side_effect=predict) as model:
            response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"face_count": 2, "known_count": 1, "unknown_count": 1})
        self.assertFalse(Path(model.call_args.args[0]).exists())

    def test_empty_gallery_returns_unavailable(self):
        with patch.object(face_api.face_core, "recognize", return_value={"gallery_size": 0}):
            self.assertEqual(self.post().status_code, 503)

    def test_extracts_face_vectors_without_a_gallery(self):
        model = face_api.face_core
        vector = np.zeros((1, 128), dtype=np.float32)
        vector[0, 0] = 1
        with (patch.object(model, "load_image"), patch.object(model, "detect", return_value=["face"]),
              patch.object(model, "embed", return_value=vector), patch.object(model, "gallery") as gallery):
            response = self.client.post("/internal/embeddings", data=self.image, content_type="image/jpeg",
                                        headers={"Authorization": f"Bearer {KEY}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"vectors": vector.tolist()})
        gallery.assert_not_called()

    def test_pad_demo_relaxes_only_the_live_threshold(self):
        model = face_api.face_core
        pixels = np.zeros((150, 150, 3), dtype=np.uint8)
        faces = [[10, 10, 100, 100]]
        vectors = np.zeros((1, 128), dtype=np.float32)
        vectors[0, 0] = 1
        net = Mock()
        with (patch.object(model, "load_image", return_value=pixels),
              patch.object(model, "detect", return_value=faces),
              patch.object(model, "embed", return_value=vectors),
              patch("face_service.faceid.pad.cv2.dnn.readNetFromONNX", return_value=net)):
            cases = [
                (0.97, ("passed", "passed")),
                (0.50, ("uncertain", "passed")),
                (0.49, ("uncertain", "uncertain")),
                (0.08, ("suspected", "suspected")),
            ]
            for live_score, expected in cases:
                with self.subTest(live_score=live_score):
                    net.forward.return_value = np.asarray([live_score, 1 - live_score], dtype=np.float32)
                    actual = []
                    for demo in ("", "1"):
                        result = self.client.post(
                            "/internal/visit-analysis", data=self.image, content_type="image/jpeg",
                            headers={"Authorization": f"Bearer {KEY}", "X-Pad-Demo": demo},
                        )
                        self.assertEqual(result.status_code, 200)
                        actual.append(result.json["pad"])
                    self.assertEqual(tuple(actual), expected)
