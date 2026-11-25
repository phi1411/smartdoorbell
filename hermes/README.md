# Hermes Agent

Dịch vụ Hermes dùng Gemini để mô tả ảnh bấm chuông, trả lời hội thoại ở cửa và hỗ trợ tra cứu dữ liệu nhà trọ. Dịch vụ giọng nói trong cùng container cung cấp chuyển âm thanh thành chữ và đọc câu trả lời. Web xác định quyền xem dữ liệu và điều khiển đèn.

## Chạy với Docker

Trong thư mục `hermes/`, sao chép `.env.example` thành `.env`, rồi điền hai biến bên dưới.

```sh
docker build -t smartdoorbell-hermes .
docker run --rm --env-file .env -p 127.0.0.1:10000:10000 smartdoorbell-hermes
```

- `GEMINI_API_KEY` là khóa của dự án Gemini.
- `API_SERVER_KEY` là khóa ngẫu nhiên tối thiểu 32 ký tự để web gọi Hermes.
- Trên web, đặt `RENTAL_HERMES_KEY` bằng `API_SERVER_KEY` và đặt `RENTAL_HERMES_URL` thành địa chỉ Hermes kết thúc bằng `/v1`.

## Triển khai trên Render

Dùng Private Service Docker cùng workspace và region với web. Chọn nhánh `main`, Root Directory `hermes`, Dockerfile và Build Context lần lượt là `./Dockerfile` và `.`. Dùng cổng `10000` và health check `/health`. Đặt khóa trong mục Environment của dịch vụ.

Sau khi dịch vụ chạy, dùng khóa nội bộ để kiểm tra `/v1/models` và `/v1/toolsets`. Các công cụ của API được tắt trong cấu hình của đồ án. Gọi thử `/v1/chat/completions` bằng ảnh đã được phép sử dụng. [Tài liệu Hermes Docker](https://hermes-agent.nousresearch.com/docs/user-guide/docker) · [Tích hợp trên web nhà trọ](../backend/rental/README.md#hermes-và-telegram)
