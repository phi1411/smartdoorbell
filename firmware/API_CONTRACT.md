# Giao kèo API nhận ảnh chuông cửa

Máy chủ trong thư mục [`backend/rental`](../backend/rental/README.md) triển khai giao kèo này.
Firmware gửi JPEG đến máy chủ đồ án. Tiến trình xử lý phía máy chủ nối tiếp
luồng Hermes và Telegram khi đã cấu hình.

Mỗi thiết bị thuộc một dãy trọ và được gắn ở cửa chung. Máy chủ lấy dãy trọ
từ cấu hình `X-Device-ID` đã đăng ký. Firmware giữ nguyên định dạng gửi ảnh.

## Yêu cầu từ ESP32-CAM

- Phương thức: `POST`.
- Đường dẫn đề xuất: `/api/device/doorbell` trên tên miền HTTPS của nhóm.
- Body: **toàn bộ byte JPEG**, không phải JSON, base64 hoặc multipart/form-data.
- Ảnh mặc định: 640 × 480; firmware từ chối ảnh lớn hơn 512 KiB.
- Header `Content-Type: image/jpeg`.
- Header `Authorization: Bearer <khóa dành riêng cho thiết bị>`.
- Header `X-Device-ID: doorbell-01`.
- Header `X-Event-ID: doorbell-01-<16 ký tự hex ngẫu nhiên>`.
- Header `Accept: application/json`.

Khóa xác thực tối thiểu 32 ký tự; tạo ngẫu nhiên, không dùng token Telegram,
khóa nhà cung cấp AI hoặc khóa quản trị Hermes làm khóa của thiết bị.
Mỗi thiết bị nên có khóa riêng, gắn với đúng `X-Device-ID` trên máy chủ.

## Xử lý phía máy chủ

1. Xác thực khóa, đối chiếu thiết bị và giới hạn tần suất nhận ảnh.
2. Giới hạn kích thước body trước khi đọc/giải mã; xác minh đúng JPEG và giới
   hạn kích thước ảnh sau giải mã. Không chỉ tin header do bên gửi cung cấp.
3. Dùng cặp `(device_id, event_id)` làm khóa duy nhất để chống xử lý trùng.
4. Ghi ảnh và sự kiện vào nơi lưu bền vững; ghi công việc xử lý vào hàng đợi
   bền vững rồi trả `202`. Không chỉ tạo một thread trong request và coi là đã lưu.
5. Worker gọi AI/Hermes bản dành cho đồ án và gửi kết quả Telegram. Không cấp
   endpoint này quyền chạy lệnh hoặc điều khiển máy cá nhân.
6. Lưu trạng thái xử lý/thất bại để người quản lý xem lại trên máy chủ.

Phân tích mô tả ảnh bằng mô hình AI và xác minh danh tính một người là hai yêu
cầu khác nhau. Chất lượng nhận diện cần được kiểm chứng riêng. Một ảnh JPEG
đơn lẻ cũng chưa thực hiện được thử thách chớp mắt như demo webcam hiện tại.

## Phản hồi

Sau khi máy chủ đã lưu bền vững sự kiện và công việc chờ xử lý:

```http
HTTP/1.1 202 Accepted
Content-Type: application/json

{"accepted":true,"event_id":"doorbell-01-0123456789abcdef","state":"queued"}
```

Nếu đã nhận cùng event trước đó, trả `200` kèm ID/trạng thái đã có và không
gửi Telegram lần nữa. `202` chỉ xác nhận nhận việc, chưa xác nhận AI hoặc
Telegram hoàn tất. Firmware chấp nhận mã `2xx` và không phân tích body.

| HTTP | Ý nghĩa |
|---|---|
| 400 | Header/ID/ảnh không hợp lệ |
| 401 / 403 | Khóa thiếu/sai hoặc không đúng thiết bị |
| 409 | Mã sự kiện đã tồn tại với nội dung ảnh khác |
| 413 | Body ảnh quá lớn |
| 415 | Không hỗ trợ loại nội dung |
| 429 | Thiết bị gửi quá nhanh |
| 503 | Chưa thể lưu/nhận công việc; không báo thành công |

Firmware có thời gian chờ kết nối 10 giây, đọc phản hồi 15 giây và bắt tay
TLS 15 giây. Máy chủ nên phản hồi nhanh sau khi xếp hàng, không chờ AI xử lý
xong trong request. Dịch vụ Render nhận ảnh nên dùng compute trả phí để tránh
độ trễ đánh thức dịch vụ Free.

Firmware chỉ gửi **một lần cho mỗi lần bấm**, không tự gửi lại khi timeout vì
có thể máy chủ đã nhận ảnh. Nếu mạng mất, không lưu ảnh chờ gửi trên thiết bị.
Trong lúc chụp/gửi, những lần bấm thêm không được xếp hàng. Có thể bổ sung
retry/hàng đợi trên thiết bị sau khi xác nhận cơ chế chống trùng phía máy chủ.

## Cấu hình trên thiết bị

1. URL HTTPS đầy đủ, không chuyển hướng.
2. Khóa riêng và ID thiết bị do lệnh `python -m rental device` cấp.
3. CA gốc phù hợp để ESP32 xác minh chứng chỉ tên miền.
