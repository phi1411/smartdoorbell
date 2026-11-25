# Firmware ESP32-CAM cho cửa chung

Sketch `esp32_doorbell/esp32_doorbell.ino` dùng cho **AI Thinker ESP32-CAM và OV2640**, xung XCLK 10 MHz, ảnh JPEG 640 × 480. Cấu hình 10 MHz đã được dùng để chụp ảnh thật trên thiết bị đồ án ở bản chuông cửa.

1. Mở sketch trong Arduino IDE và chọn **AI Thinker ESP32-CAM** thuộc Arduino ESP32 core 3.3.12.
2. Sao chép `esp32_doorbell/config.example.h` thành `esp32_doorbell/config.local.h` trên máy nạp. Điền Wi-Fi 2,4 GHz, URL HTTPS đầy đủ kết thúc bằng `/api/device/doorbell`, mã thiết bị, khóa riêng của thiết bị và chứng chỉ CA tin cậy của máy chủ. Với thiết bị đã cấp khóa, tái sử dụng cấu hình riêng của thiết bị đó. Giữ file này và các bản biên dịch chứa cấu hình ngoài Git.
3. Nối nút thường hở giữa **GPIO13 và GND**. Dùng GPIO13 cho nút hoặc microSD theo thiết kế phần cứng. Sketch dành GPIO13 cho nút và không dùng microSD.
4. Cắm đế nạp và chọn cổng COM. Nếu cần chế độ nạp tay, giữ IO0, bấm rồi thả RST và thả IO0. Sau khi nạp, bấm RST với IO0 đã thả để chạy chương trình.
5. Mở Serial Monitor ở 115200 baud. Chờ camera sẵn sàng, Wi-Fi kết nối và đồng bộ giờ. Bấm nút hoặc gõ `C`, chờ web báo nhận ảnh rồi đối chiếu lịch sử và Telegram.

## Xem camera khi cần

1. Đăng nhập web bằng tài khoản chủ trọ hoặc quản lý được giao đúng dãy.
2. Vào **Camera trực tiếp**, chọn camera rồi bấm **Bắt đầu xem**. ESP32 chủ động kết nối đến web nên camera và trình duyệt có thể ở khác mạng.
3. Bấm **Dừng xem** khi xong. Phiên xem hết hạn sau 12 giây nếu trình duyệt ngừng gia hạn. Mỗi camera có một phiên xem tại một thời điểm.

ESP32 gửi các khung JPEG qua một kết nối WebSocket bảo mật (WSS) trong phiên xem. Tốc độ phụ thuộc mạng và thời gian chụp. Máy chủ giữ khung gần nhất trong RAM tối đa 4 giây. Các kết nối HTTPS và WSS dùng chung khóa thiết bị và xác minh chứng chỉ. Tác vụ mạng của chế độ xem chạy riêng, còn ảnh được lấy trên cùng vòng lặp camera để tránh hai tác vụ dùng bộ đệm đồng thời.

| Hành vi của thiết bị | Hợp đồng |
|---|---|
| Kiểm tra phiên xem | `GET /api/device/live/demand` với `Authorization: Bearer ...` và `X-Device-ID`. Máy chủ trả `204` khi rảnh hoặc `200` cùng mã phiên 32 ký tự. |
| Truyền hình trực tiếp | `WSS /api/device/live/ws` với hai header xác thực và `X-Live-Stream` chứa mã phiên. Mỗi tin nhắn nhị phân chứa một JPEG tối đa 96 KiB. Máy chủ đóng kết nối khi phiên xem kết thúc. |
| Gửi khung hình dự phòng | `POST /api/device/live/frame` với cùng các header và một phần thân JPEG tối đa 96 KiB. Máy chủ trả `204` nếu nhận, `409` nếu phiên đã đổi hoặc hết hạn. |
| Gửi ảnh bấm chuông | Giữ nguyên [hợp đồng ảnh sự kiện](API_CONTRACT.md). |

## Mở cửa mô phỏng

Từ web hoặc tin báo chuông trên Telegram, chọn **Mở cửa mô phỏng**. Đèn flash trắng có sẵn trên bo AI Thinker dùng GPIO4. Khi máy chủ gửi lệnh, firmware bật đèn trong 3 giây rồi tắt bằng bộ định thời riêng. Serial Monitor báo `[FLASH]` khi đèn bật và khi máy chủ nhận xác nhận đã tắt.

Firmware báo hỗ trợ bằng `X-Doorbell-Flash-Capable: 1` khi hỏi `/api/device/live/demand`. Máy chủ có thể trả `X-Doorbell-Flash` chứa mã lệnh 32 ký tự hex trên phản hồi `200` hoặc `204`. Sau khi tắt đèn, thiết bị gửi `POST /api/device/flash/<mã-lệnh>/ack` với hai header xác thực như yêu cầu xem camera. Thiết bị bỏ qua mã sai định dạng và phản hồi về quá chậm. Lệnh xác nhận có thể gửi lại trong thời gian ngắn, còn đèn chỉ bật một lần cho lệnh đã nhận.
