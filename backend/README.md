# Nhà trọ và chuông cửa chung

## Chạy trên máy

Ở thư mục gốc của repo `smartdoorbell`:

```powershell
python -m pip install -r requirements.txt
Copy-Item backend/.env.rental.example backend/.env.rental
# Trong backend/.env.rental, đặt RENTAL_ADMIN_PASSWORD tối thiểu 16 ký tự.
python run_rental_local.py
```

Mở bảng quản lý và đăng nhập bằng tên `chu-tro` với mật khẩu quản trị. Khi chạy cục bộ, thêm cờ `--single-house` hoặc đặt `RENTAL_SINGLE_HOUSE=1` để tự tạo và chỉ quản lý **Dãy A**. Thêm phòng và người thuê, sau đó vào **Tài khoản → Mời người thuê đăng ký**. Gửi riêng mã để người thuê nhập tại **Đăng ký tài khoản người thuê**. Mã gắn với đúng hồ sơ và dùng một lần trong 24 giờ. Chủ trọ cấp tài khoản quản lý và kỹ thuật viên trong mục **Cấp tài khoản trực tiếp**. [Bảng quản lý cục bộ](http://127.0.0.1:5050)

Trong **Chuông cửa chung**, đăng ký mã ESP32 để lấy khóa riêng. Trong **Phòng và người thuê**, mở **Mẫu khuôn mặt** dưới tên người thuê để thêm ảnh đối chiếu. Khi người thuê trả phòng, hệ thống gỡ mẫu của lượt thuê đó khỏi bộ nhận diện.

## Vai trò và quy trình

| Vai trò | Phạm vi | Thao tác chính |
| --- | --- | --- |
| Chủ trọ | Dãy A | Quản lý tài khoản, phòng, người thuê, camera, nơi nhận Telegram và vận hành. |
| Quản lý | Dãy A | Quản lý phòng và lượt thuê, hợp đồng, hóa đơn, ghi nhận thanh toán, thông báo, xử lý bảo trì và xem camera cửa chung. |
| Kỹ thuật viên | Dãy A | Xem và cập nhật tiến độ yêu cầu sửa chữa. |
| Người thuê | Lượt thuê được gán | Xem hợp đồng, hóa đơn và khoản đã đóng. Tạo yêu cầu sửa chữa và xem thông báo dãy khi còn ở. |

1. **Vào ở.** Tạo hồ sơ ở đúng phòng, điền điều khoản hợp đồng, tiền cọc, tiền phòng và đơn giá điện nước trong mục **Vận hành**. Hồ sơ lưu các điều khoản để theo dõi nội bộ.
2. **Lập hóa đơn.** Mỗi phòng có một hóa đơn mỗi tháng. Chọn hồ sơ chịu trách nhiệm thanh toán. Hóa đơn lưu số cũ, số mới, đơn giá và phí đi kèm tại thời điểm lập. Ghi nhận từng khoản tiền mặt hoặc chuyển khoản đã nhận để theo dõi đã thu và số còn lại. Nếu nhập nhầm, mở chi tiết hóa đơn và ghi lý do hủy khoản thu. Khi hóa đơn đã hết khoản thu hợp lệ, có thể hủy hóa đơn và lập lại cho người thanh toán ban đầu. Lịch sử giữ các khoản thu và các lần phát hành đã hủy.
3. **Bảo trì.** Người thuê gửi yêu cầu; quản lý hoặc kỹ thuật viên phụ trách dãy cập nhật tiến độ và ghi kết quả.
4. **Trả phòng.** Kết thúc lượt thuê và xóa mẫu khỏi bộ đối chiếu. Tài khoản người thuê còn hoạt động có thể xem chứng từ và yêu cầu sửa chữa của chính lượt thuê đã kết thúc.

Tham khảo các quy trình [Honavi](https://apps.apple.com/vn/app/honavi-qu%E1%BA%A3n-l%C3%BD-nh%C3%A0-tr%E1%BB%8D/id6806406961), [Buildium Resident Center](https://www.buildium.com/features/resident-center/) và [hướng dẫn phân quyền OWASP](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html). Đơn giá điện và nước do chủ trọ nhập theo điều kiện thực tế của dãy. Hệ thống ghi sổ thu tiền nội bộ.

## Triển khai trên Render

### Một Web Service

Chọn nhánh `main`, runtime Python và để trống Root Directory. Web, worker và mô hình nhận diện chạy chung. API mô hình chỉ lắng nghe trên máy chủ qua loopback. Khi nâng cấp dịch vụ đang có, giữ ổ `/var/data`. Hệ thống tự tạo các bảng còn thiếu.

| Trường | Giá trị |
|---|---|
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python run_rental_local.py --render` |
| Health Check Path | `/healthz` |
| Ổ lưu bền vững | Gắn tại `/var/data` |

Đặt `PYTHON_VERSION=3.13.9` và tạo giá trị ngẫu nhiên cho `RENTAL_ADMIN_PASSWORD` có ít nhất 16 ký tự. Lệnh chạy lưu SQLite tại `/var/data/rental.db`, bật cookie bảo mật và tự cấu hình API nhận diện nội bộ. Render mặc định dùng Dãy A. Với dự án khác cần quản lý nhiều dãy, đặt `RENTAL_SINGLE_HOUSE=0`. Các biến Telegram và Hermes bên dưới dùng được cho cả hai cách triển khai.

Có thể đặt thêm `RENTAL_SESSION_SECRET` ngẫu nhiên, độc lập với mật khẩu và dài tối thiểu 32 ký tự, để giữ phiên qua lần khởi động lại. Khi bỏ trống, web tạo khóa ký mới mỗi lần chạy.

### Hai dịch vụ

Tạo hai dịch vụ từ cùng repo và nhánh đã chọn, trong cùng workspace và region. [Mạng nội bộ Render](https://render.com/docs/private-network)

| Trường | Dịch vụ mô hình | Dịch vụ nhà trọ |
|---|---|---|
| Loại | Private Service | Web Service |
| Runtime | Python | Python |
| Root Directory | Để trống | `backend` |
| Build Command | `pip install -r face_service/requirements.txt` | `pip install -r requirements.txt` |
| Start Command | `python -m face_service.face_api` | `python -m rental web --host 0.0.0.0 --with-worker` |
| Health Check Path | `/healthz` | `/healthz` |

Chọn gói hỗ trợ ổ lưu bền vững cho dịch vụ nhà trọ, gắn ổ tại `/var/data` và đặt `RENTAL_DB_PATH=/var/data/rental.db`. Web và worker chạy chung một máy để dùng cùng cơ sở dữ liệu. [Persistent Disks](https://render.com/docs/disks)

**Environment của dịch vụ mô hình**

- `PYTHON_VERSION=3.13.9`
- `DOOR_FACE_HOST=0.0.0.0`
- `DOOR_FACE_API_KEY` là khóa ngẫu nhiên tối thiểu 32 ký tự dành riêng cho hai dịch vụ.

**Environment của dịch vụ nhà trọ**

- `PYTHON_VERSION=3.13.9`
- `RENTAL_ADMIN_PASSWORD` là mật khẩu ngẫu nhiên tối thiểu 16 ký tự.
- `RENTAL_COOKIE_SECURE=1`
- `RENTAL_DB_PATH=/var/data/rental.db`
- `RENTAL_FACE_EMBED_URL=http://<địa-chỉ-nội-bộ-của-private-service>/internal/embeddings` với địa chỉ gồm cả cổng trong Render.
- `RENTAL_FACE_PAD_URL=http://<địa-chỉ-nội-bộ-của-private-service>/internal/visit-analysis` để phân tích PAD 2D cùng với đối chiếu mẫu.
- `RENTAL_FACE_API_KEY` bằng `DOOR_FACE_API_KEY`.
- `RENTAL_TELEGRAM_BOT_TOKEN` lấy từ bot của nhóm. Thêm bot vào nhóm nhận thông báo, cấu hình Chat ID dạng số ở mục **Thông báo Telegram** của dãy. Luồng gửi ảnh dùng `sendPhoto`.
- Nếu đồ án dùng Hermes để mô tả ảnh, đặt `RENTAL_HERMES_URL` và `RENTAL_HERMES_KEY` của bản Hermes riêng dành cho đồ án. Xem [cấu hình Hermes](rental/README.md#hermes-và-telegram).

## Kết nối ESP32-CAM

1. Kiểm tra `https://<web-service>.onrender.com/healthz`. Đăng nhập và đăng ký chuông cho dãy tương ứng.
2. Điền `DOORBELL_API_URL` bằng URL HTTPS của web nối thêm `/api/device/doorbell`, dùng đúng `DOORBELL_DEVICE_ID`, khóa thiết bị và CA phù hợp trong `config.local.h` của firmware. Xem [định dạng yêu cầu](../firmware/API_CONTRACT.md).
3. Nạp firmware, kiểm tra Serial Monitor ở `115200 baud` báo camera sẵn sàng và Wi-Fi đã kết nối. Bấm nút cửa thật một lần và kiểm tra lịch sử. Đối chiếu vị trí cửa, thời gian và kết quả nhận diện.
4. Gửi thử một lượt khớp và một lượt chưa nhận diện được, kiểm tra trạng thái Telegram, rồi thử tải lại trang để mở ảnh từ lịch sử.

Kết quả nhận diện ảnh tĩnh dùng để thông báo và xem lại. Quy trình gỡ mẫu khi trả phòng được xử lý tại web.

## Mở cửa mô phỏng

Chủ trọ hoặc quản lý đúng dãy bấm **Mở cửa mô phỏng** ở lượt chuông mới nhất của thiết bị trên web. Nút tương tự xuất hiện dưới ảnh báo chuông trong chat riêng của bot Telegram đã cấu hình nếu chat đó đang được chủ trọ đặt làm nơi nhận của dãy.

ESP32-CAM AI Thinker dùng đèn flash trên GPIO4 để mô phỏng khóa. Khi nhận lệnh, bo bật đèn 3 giây, tự tắt rồi gửi xác nhận. Mỗi lượt chuông có tối đa một lệnh dùng chung cho web và Telegram. Web hiển thị kết quả xác nhận. Bấm lại trên Telegram chỉ kiểm tra yêu cầu đã có. Lượt chuông mới hơn sẽ thay thế lượt cũ, và lệnh chưa được ESP32 nhận sẽ hết hạn sau 20 giây.

Firmware mới tự hỏi máy chủ qua HTTPS và cần được nạp trước khi thử. Mô phỏng điều khiển đèn trên bo. Telegram dùng một tiến trình đọc callback cho bot riêng của đồ án. [Cấu hình Telegram](rental/README.md#hermes-và-telegram)

## Kiểm tra giả mạo trên ảnh chuông

Bản chạy chung tự gọi **anti-spoof-mn3**, mô hình PAD 2D dùng ảnh màu, trên máy chủ cùng với YuNet và SFace. Khách đứng nhìn camera trong lượt bấm chuông. Lịch sử và thông báo Telegram hiện riêng kết quả đối chiếu khuôn mặt, kết quả PAD và mô tả Hermes. Nếu ảnh có nhiều mặt, mặt quá nhỏ, hoặc mô hình không cho kết quả rõ, hệ thống chuyển kiểm tra thủ công. Hermes nhận kết quả PAD để mô tả và giải thích, quyền điều khiển đèn nằm ở máy chủ.

Mặc định chủ trọ vẫn xác nhận. Để thử **tự bật flash 3 giây** trong buổi demo có giám sát, đặt `RENTAL_PAD_DEMO_DEVICE` bằng đúng mã của ESP32 được dùng, ví dụ `doorbell-01`. Cần đủ một khuôn mặt khớp mẫu của người đang thuê cùng dãy và PAD vượt ngưỡng. Lượt chuông đã quá 20 giây hoặc đã có lượt mới hơn không được phát lệnh tự động. Lệnh dùng chung cơ chế xác nhận và chặn lặp với web và Telegram.

Mô hình gốc phân loại ảnh 2D. Ngưỡng điểm người thật là 0,95 ở chế độ thường và 0,50 cho riêng thiết bị demo. Ngưỡng điểm nghi giả mạo là 0,90 ở cả hai chế độ. Lịch sử và Telegram ghi rõ lượt nào dùng chế độ demo. Điểm mô hình là điểm phân loại trên ảnh. Khi thử trên bo thật, ghi lại kết quả với người đã đăng ký, người chưa đăng ký, ảnh in và ảnh trên màn hình. Chế độ tự động dùng đèn tích hợp để mô phỏng mở cửa. [Thông số và giấy phép mô hình](https://github.com/openvinotoolkit/open_model_zoo/tree/master/models/public/anti-spoof-mn3)

## Liên lạc cửa chung

1. Chủ trọ mở **Liên lạc cửa chung**, chọn ESP32-CAM và tạo mã ghép nối. Trên điện thoại đặt ở cửa, mở trang dành cho điện thoại, nhập mã một lần, rồi bấm **Bật mic và loa**. Giữ trang này mở khi dùng. Điện thoại cần truy cập bản HTTPS và cấp quyền mic. [Quyền mic trên trình duyệt](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia)
2. Khi khách bấm nút, ESP32 gửi ảnh lên web theo luồng chuông đã có. Điện thoại ở cửa hiện trạng thái đang chờ, còn trang tổng quan của chủ trọ hoặc quản lý đúng dãy hiện **Trả lời** ở mục **Chuông đến**. Chủ trọ có thể mở trang tổng quan sau khi nhận thông báo Telegram.
3. Trang chủ trọ mở mic khi trả lời và xin quyền nếu trình duyệt yêu cầu. Sau khi web báo **Đã kết nối âm thanh trực tiếp**, hai bên nói và nghe như cuộc gọi. Có nút **Tắt mic** và **Bật loa** nếu trình duyệt chặn âm thanh tự phát. Chủ trọ có thể kết thúc cuộc gọi hoặc gửi yêu cầu mở cửa mô phỏng trong cùng trang.
4. **Trợ lý thoại Hermes tự kích hoạt sau 15 giây:** Nếu chủ trọ chưa bấm **Trả lời** sau 15 giây (cấu hình qua `RENTAL_HERMES_HANDOFF_SECONDS`, mặc định `15`), điện thoại ở cửa tự chuyển sang chế độ trợ lý Hermes: đọc lời chào qua loa, tự nghe lời khách nói theo lượt trên Chrome Android (có nút **Nói với trợ lý** và ô nhập lời nhắn dự phòng), gọi Hermes trả lời ngắn gọn, lưu hội thoại vào lịch sử lượt chuông và gửi tóm tắt sang Telegram (không tự kích hoạt mở cửa). Nếu chủ trọ bấm **Trả lời ngay** muộn hơn trong thời gian chờ chuông, hệ thống lập tức ngắt Hermes và chuyển sang cuộc gọi WebRTC trực tiếp.

Cuộc gọi dùng WebRTC để truyền tiếng giữa hai thiết bị. Web nhà trọ trao đổi thông tin kết nối trong phiên đang hoạt động, với một đầu điện thoại và một đầu chủ trọ hoặc quản lý. Màn hình cuộc gọi không thu và lưu các đoạn thoại. Quyền ghép nối điện thoại thuộc chủ trọ; có thể thu hồi quyền trong **Liên lạc cửa chung**.

Trình duyệt thử kết nối trực tiếp khi hai thiết bị cùng mạng. Khi gọi qua các mạng khác nhau, cấu hình `RENTAL_INTERCOM_STUN_URL` cho tìm đường kết nối, và `RENTAL_INTERCOM_TURN_URLS` cùng `RENTAL_INTERCOM_TURN_SECRET` nếu cần máy chủ chuyển tiếp. Danh sách TURN phân cách bằng dấu phẩy. TURN cần hỗ trợ thông tin xác thực ngắn hạn bằng shared secret (tối thiểu 32 ký tự). Chỉ hai đầu đã xác thực và trong cuộc gọi mới nhận thông tin kết nối. [Giải thích máy chủ TURN của WebRTC](https://webrtc.org/getting-started/turn-server)

## Camera trực tiếp

Nạp [firmware trong repo](../firmware/esp32_doorbell) cho AI Thinker ESP32-CAM. Sau khi bo lên Wi-Fi, chủ trọ hoặc quản lý được phân công mở **Camera trực tiếp**, chọn thiết bị ở cửa chung và bấm **Bắt đầu xem**. ESP32 hỏi web qua HTTPS rồi gửi các khung JPEG trên một kết nối WSS trong lúc phiên xem còn hiệu lực. Render chuyển các khung mới đến trình duyệt qua luồng HTTP. Cách này dùng được khi thiết bị và trình duyệt ở hai mạng khác nhau. Chạy dịch vụ với một tiến trình web để phiên xem và khung hình mới nhất dùng chung bộ nhớ.

Ảnh chuông là luồng xử lý riêng lên lịch sử và Telegram. Camera trực tiếp dành cho cửa chung và người có quyền vận hành. Khi dùng ở nhà trọ thật, cần thông báo mục đích ghi hình và thống nhất ai được xem, thời hạn lưu ảnh chuông và cách tiếp nhận yêu cầu xóa. [Hướng dẫn về ghi hình tại nơi công cộng](https://xaydungchinhsach.chinhphu.vn/quy-dinh-bao-ve-du-lieu-ca-nhan-thu-duoc-tu-hoat-dong-ghi-am-ghi-hinh-tai-noi-cong-cong-119250730154729554.htm) · [Thư viện camera Espressif](https://github.com/espressif/esp32-camera)

## Hermes cho nhà trọ

- **Hỏi Hermes.** Chủ trọ hoặc quản lý có thể tra phòng trống, hóa đơn, hợp đồng và yêu cầu bảo trì trên web. Tài khoản người thuê tra được dữ liệu của lượt thuê được gán. Trong chat riêng đã cấu hình nhận thông báo của dãy, chủ trọ có thể gửi trực tiếp câu hỏi hoặc bắt đầu bằng `/hoi`. Hermes phân loại câu hỏi và máy chủ lấy số liệu theo quyền của tài khoản hoặc chat.
- **Báo sửa chữa bằng giọng nói.** Tài khoản người thuê đang ở mở **Phòng của tôi**, tìm mục **Bảo trì** và mở trang báo sửa chữa. Người thuê có thể ghi âm trên HTTPS hoặc gõ mô tả, xem lại nội dung và xác nhận gửi phiếu.
- **Báo cáo sáng.** Tiến trình worker gửi báo cáo theo dãy lúc 08:00 giờ Việt Nam tới chat Telegram riêng đã đặt cho dãy. Báo cáo tổng hợp hóa đơn đến hạn và quá hạn, phiếu sửa chữa còn mở, cùng chuông chưa được trả lời trong 24 giờ.
