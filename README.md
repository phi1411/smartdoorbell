# Smart Doorbell AI

Demo chuông cửa thông minh chạy hoàn toàn trên laptop Windows: webcam realtime, đăng ký/nhận diện khuôn mặt, liveness cơ bản, cảnh báo Telegram và lịch sử SQLite.

Camera có thể nhận diện nhiều khuôn mặt trong cùng khung hình: mỗi người có khung và nhãn Known/Unknown riêng. Khi bấm chuông, Telegram liệt kê các tên đã nhận diện, số người chưa xác định và khuôn mặt nghi giả mạo. Enrollment vẫn yêu cầu đúng một người trong khung để tránh gán nhầm mẫu.

## Công nghệ và lý do lựa chọn

- **Flask**: nhẹ, dễ giải thích khi bảo vệ, phù hợp dashboard local và MJPEG stream.
- **OpenCV Contrib (Haar + LBPH)**: có wheel cho Windows/Python hiện đại, không cần build `dlib`, không tải model cloud. LBPH học từ nhiều ảnh xám của mỗi người và phù hợp demo có ánh sáng tương đối ổn định.
- **SQLite WAL**: dữ liệu giữ lại sau restart; mỗi thread mở connection riêng để tránh dùng chung connection sai cách.
- **Telegram Bot API**: gửi/poll trong background thread để Internet chậm không làm treo camera hoặc web.

> LBPH không phải face embedding neural hiện đại. Đây là lựa chọn ưu tiên khả năng cài và demo ổn định trên Windows. Nếu cần độ chính xác cao hơn, có thể thay `FaceRecognizer` bằng SFace/InsightFace mà không đổi event manager hay API.

## Cài đặt trên Windows

Khuyến nghị Python 3.11–3.13 64-bit.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Mở `.env`, đổi ít nhất:

```env
SECRET_KEY=mot_chuoi_ngau_nhien
TELEGRAM_BOT_TOKEN=token_tu_BotFather
TELEGRAM_ALLOWED_CHAT_ID=chat_id_cua_chu_nha
```

Không đưa `.env` lên Git. Nếu chưa cấu hình Telegram, toàn bộ phần camera/nhận diện/lịch sử vẫn chạy; sự kiện được lưu với `telegram_sent=false`.

## Chạy

```powershell
python app.py
```

Mở [http://127.0.0.1:5000](http://127.0.0.1:5000). Camera được mở đúng một lần trong background. Nếu camera đang bị Zoom/Teams/ứng dụng Camera giữ, hãy đóng ứng dụng đó; hệ thống sẽ tự thử kết nối lại.

## Sử dụng

1. Vào **Faces**, nhập tên, nhìn thẳng camera và chọn **Start scan**. Hệ thống lấy 5 mẫu, tự bỏ ảnh mờ/không có mặt/nhiều mặt.
2. Trở lại **Dashboard** để xem Known/Unknown và liveness.
3. Nhấn **Ring Doorbell**. Lần nhấn trong cooldown mặc định 5 giây trả HTTP 429 và không tạo/gửi sự kiện mới.
4. Vào **History** để lọc theo ngày/loại, xem ảnh, hoặc xuất CSV.
5. Vào **Settings** để chỉnh cooldown, dwell-time, lost tolerance, threshold và bật/tắt module.

Trên **Dashboard** có công tắc bật/tắt nhanh cảnh báo “Người chưa xác định lưu lại thời gian dài”. Khi tắt, hệ thống xóa phiên theo dõi hiện tại và không tạo sự kiện hoặc gửi Telegram cho loại cảnh báo này.

Bot chỉ xử lý command từ `TELEGRAM_ALLOWED_CHAT_ID`:

- `/status`: camera, recognition, anti-spoof và số người đã đăng ký.
- `/photo`: ảnh webcam mới nhất.
- `/history`: gửi lần lượt 5 sự kiện gần nhất thành 5 tin riêng; mỗi tin có ảnh, loại sự kiện, định danh và thời gian. Sự kiện mất ảnh vẫn được gửi dưới dạng văn bản.

## Hiệu chỉnh nhận diện

`face_match_threshold` là khoảng cách LBPH, **càng thấp càng nghiêm**. Mặc định `65`:

- Nhận nhầm người lạ thành Known: giảm dần về 55 hoặc 45.
- Người đã đăng ký thường bị Unknown: tăng dần lên 70–80 và đăng ký lại trong ánh sáng demo.
- Nên đăng ký đúng vị trí, khoảng cách và ánh sáng sẽ dùng khi bảo vệ.

## Anti-spoof/liveness đã cam kết

`BasicLivenessProvider` dùng thử thách chớp mắt theo chuỗi **mở → nhắm → mở**. Chuyển động của khuôn mặt chỉ là tín hiệu theo dõi và không còn đủ để kết luận REAL, vì ảnh trên điện thoại cũng có thể được cầm di chuyển. Nếu không chớp mắt hợp lệ trong thời gian kiểm tra, khuôn mặt được đánh dấu `fake`.

Trạng thái `FAKE / SPOOF` chỉ hiển thị trực tiếp trên camera, không tự gửi Telegram trong nền. Khi người dùng bấm chuông để chụp, hệ thống mới lưu một sự kiện `SPOOF_ATTEMPT` và gửi đúng một ảnh Telegram; cooldown nút chuông vẫn áp dụng.

Đây là **liveness mức demo**, không phải bảo mật sinh trắc học production. Ảnh được di chuyển khéo hoặc video có thể vượt qua; người thật đứng quá im có thể bị false positive. Interface `LivenessProvider` đã tách riêng để thay bằng model anti-spoof pretrained sau này mà không phá hệ thống đang chạy.

Kịch bản người thật: nhìn vào webcam và chớp mắt rõ ràng một lần (nhắm khoảng nửa giây). Kịch bản spoof: đưa ảnh in/ảnh trên màn hình trước webcam; dù di chuyển điện thoại, ảnh không hoàn thành được thử thách chớp mắt và sau khoảng 4 giây sẽ hiện `FAKE / SPOOF`.

## Test

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

Test tự động bao phủ cooldown, bấm chuông không có mặt, Unknown dưới/ngang threshold, một alert mỗi session, mất dấu ngắn/dài, Known đứng lâu, spoof suppression, SQLite persistence và Telegram command authorization.

Kiểm tra nhanh API:

```powershell
Invoke-RestMethod http://127.0.0.1:5000/api/status
```

## API cho frontend

| Method | Path | Chức năng |
|---|---|---|
| GET | `/api/status` | Trạng thái toàn hệ thống + recognition hiện tại |
| POST | `/api/doorbell` | Bấm chuông; `202`, `429` cooldown hoặc `503` camera |
| GET | `/api/camera/stream` | MJPEG stream có bounding box |
| GET | `/api/camera/frame` | Một JPEG mới nhất |
| GET | `/api/faces` | Danh sách người và số mẫu |
| POST | `/api/faces/enroll` | JSON `{ "name": "...", "sample_count": 5 }` |
| POST | `/api/faces/{id}/enroll` | Thu lại mẫu |
| DELETE | `/api/faces/{id}` | Xóa người và mẫu |
| GET | `/api/events?limit=50&type=&date=` | Lọc lịch sử |
| GET | `/api/events?format=csv` | Xuất CSV |
| GET | `/api/events/{id}/image` | Ảnh sự kiện |
| GET | `/api/settings` | Cấu hình runtime |
| PUT | `/api/settings` | Cập nhật một hoặc nhiều setting |

## Cấu trúc

```text
app.py
smart_doorbell/
  app_factory.py, config.py, database.py
  routes/          # HTML pages + JSON/camera API
  repositories/    # persons, events, settings
  services/        # camera, CV, liveness, enrollment, event, Telegram
templates/         # UI MVP, có thể thay hoàn toàn mà giữ API
static/            # CSS/JS không cần CDN
data/              # DB, face samples, event images (không commit dữ liệu thật)
tests/
```

## Lỗi thường gặp

- `cv2.face` không tồn tại: gỡ `opencv-python`, sau đó cài lại `opencv-contrib-python` trong đúng virtual environment.
- Camera Offline: đóng app đang dùng webcam, kiểm tra `CAMERA_INDEX=0` (thử 1 nếu có nhiều camera).
- Telegram không gửi: kiểm tra token/chat ID và đã nhắn `/start` cho bot; xem `/api/status` trường `telegram.last_error`.
- Enrollment timeout: chỉ để một người trong khung, tăng sáng, giữ mặt lớn hơn 80 px và tương đối yên.
