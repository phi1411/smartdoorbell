# Dịch vụ phân tích khuôn mặt

YuNet phát hiện khuôn mặt, SFace trích xuất vector và anti-spoof-mn3 phân loại PAD 2D từ ảnh màu. Web so khớp vector theo các hồ sơ đang thuê cùng dãy. Dịch vụ mô hình xử lý ảnh trong thư mục tạm và xóa tệp sau khi xong.

Lệnh `python run_rental_local.py` tự chạy dịch vụ trên loopback. Khi triển khai riêng, chạy các lệnh sau ở thư mục gốc.

```sh
python -m pip install -r face_service/requirements.txt
python -m face_service.face_api
```

Trước khi chạy riêng, đặt `DOOR_FACE_API_KEY` thành khóa ngẫu nhiên tối thiểu 32 ký tự. Web dùng cùng khóa trong `RENTAL_FACE_API_KEY`. Dịch vụ mặc định lắng nghe tại `127.0.0.1:5051`.

| Endpoint | Vai trò |
| --- | --- |
| `GET /healthz` | Kiểm tra dịch vụ đang chạy. |
| `POST /internal/embeddings` | Trích xuất vector từ ảnh JPEG. |
| `POST /internal/visit-analysis` | Trả vector và kết quả PAD. |

Các endpoint POST dùng Bearer token. Xem [thiết lập kết nối](../backend/README.md#triển-khai-trên-render) và [nguồn mô hình](faceid/models/README.md).
