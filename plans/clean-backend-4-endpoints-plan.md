# Kế Hoạch Clean Dự Án Và Dựng Lại Backend 4 Endpoint

## Mục tiêu

Clean lại phần backend hiện tại, xóa các file không còn cần cho bài toán bridge hiện tại, và dựng lại backend theo hướng tối giản chỉ còn đúng 4 endpoint nghiệp vụ:

- `POST /v1/chat/gemini`
- `POST /v1/chat/gpt`
- `POST /v1/image/gemini`
- `POST /v1/image/gpt`

## Phạm vi bắt buộc

- Chỉ giữ backend phục vụ text và image cho Gemini/GPT.
- Không giữ alias route kiểu `/v1/chat`, `/v1/image`.
- Không giữ endpoint phụ nếu yêu cầu cuối cùng là đúng 4 endpoint public.
- Không mở rộng sang frontend, auth, queue, dashboard, docs ngoài phần thật sự cần để backend chạy.

## Ràng buộc kỹ thuật cần chốt ngay khi bắt đầu thực thi

- Nếu giữ đúng 4 endpoint public, phải bỏ cả `/generated-assets`, `/health`, `/docs`, `/openapi.json` khỏi public surface.
- Vì `/generated-assets` sẽ bị bỏ, endpoint image phải tự trả dữ liệu ảnh theo cùng response của 4 endpoint hiện hữu, thay vì dựa vào route tải file riêng.
- Chrome/CDP attach flow vẫn là nền tảng chính, không dựng hệ orchestration mới ngoài yêu cầu.

## Phase 1. Khóa Scope Và Chốt Bề Mặt Cần Giữ

Mục tiêu:
Xác định chính xác backend tối thiểu còn lại sau cleanup, để tránh xóa nhầm file còn đang tham gia vào runtime.

Việc làm:
- Rà lại entrypoint backend hiện tại, các model request/response, service CDP, script chạy dự án, và dependency trực tiếp.
- Liệt kê rõ file hoặc module bắt buộc phải giữ để phục vụ 4 endpoint.
- Liệt kê rõ file hoặc module đang chỉ phục vụ alias route, static file route, health route, debug helper, hoặc thử nghiệm cũ.
- Chốt trước target public surface: chỉ 4 endpoint, không route phụ.

Đầu ra:
- Danh sách `keep`.
- Danh sách `delete/move`.
- Danh sách contract backend mới.

Skill nên dùng khi thực thi:
- `fix-code`: đọc lại pipeline runtime hiện tại, xác định file nào đang thực sự tham gia vào luồng request và file nào là di sản thừa.
- `backend-production`: chốt bề mặt backend tối thiểu theo chuẩn production, tránh để business logic dạt lung tung sau cleanup.

## Phase 2. Clean File Và Cắt Hẳn Các Bề Mặt Không Còn Cần

Mục tiêu:
Xóa khỏi repo hoặc khỏi app runtime mọi file, route, config, và helper không còn phục vụ 4 endpoint mục tiêu.

Việc làm:
- Gỡ alias route `/v1/chat` và `/v1/image`.
- Gỡ static mount `/generated-assets` khỏi public API nếu giữ yêu cầu đúng 4 endpoint.
- Gỡ `/health`, `/health/gpt`, docs URL, openapi URL nếu cần ép đúng 4 endpoint public.
- Xóa helper, snapshot logic, config, và test cũ nào chỉ phục vụ bề mặt đã bị loại.
- Giữ lại duy nhất các file runtime còn cần cho setup, attach Chrome, request lock, và 4 luồng Gemini/GPT text/image.

Đầu ra:
- Cây backend gọn hơn, không còn route và file thừa.
- Danh sách file đã xóa hoặc bỏ tham chiếu.

Skill nên dùng khi thực thi:
- `fix-code`: đảm bảo xóa đúng bề mặt thừa mà không làm vỡ pipeline còn lại.

## Phase 3. Dựng Lại Backend Tối Giản Theo Đúng 4 Endpoint

Mục tiêu:
Tái tổ chức backend để contract, route, và service rõ ràng, chỉ còn đúng 4 endpoint cần thiết.

Việc làm:
- Tách route layer, request/response model, và service layer đủ gọn để bảo trì.
- Giữ một service chung cho attach Chrome/CDP và lock request, nhưng route surface chỉ còn 4 endpoint.
- Chuẩn hóa request/response cho text và image giữa Gemini/GPT.
- Thiết kế lại response image để không cần thêm endpoint phụ cho download file. Hướng ưu tiên:
  - trả binary/base64 ngay trong response contract của endpoint image, hoặc
  - trả metadata + data inline, nhưng không sinh thêm public route thứ 5.
- Tắt docs/openapi public nếu cần đảm bảo đúng 4 endpoint public thực tế.

Đầu ra:
- Backend mới có đúng 4 endpoint public.
- Response contract rõ ràng cho text và image.
- Không còn phụ thuộc vào static assets route để hoàn tất luồng image.

Skill nên dùng khi thực thi:
- `backend-production`: phase chính để tổ chức lại backend, tối ưu luồng request nặng, và giữ contract rõ ràng.

## Phase 4. Smoke Test, Regression Test, Và Chốt Runtime

Mục tiêu:
Xác nhận backend sau cleanup vẫn chạy được thật và đúng đúng 4 endpoint đã chốt.

Việc làm:
- Chạy lại script bring-up dự án.
- Gọi HTTP thật cho cả 4 endpoint trên các case tối thiểu:
  - chat Gemini
  - chat GPT
  - image Gemini
  - image GPT
- Kiểm tra không còn route phụ public ngoài 4 route đã chốt.
- Kiểm tra lại luồng image không bị treo event loop, không trả trùng ảnh, và vẫn xử lý được trường hợp provider sinh 2 ảnh.
- Rà log runtime để chắc không còn lỗi CDP/persist/download kiểu cũ.

Đầu ra:
- Bằng chứng chạy thật cho đúng 4 endpoint.
- Xác nhận cleanup không làm hỏng bring-up.
- Danh sách lỗi còn lại nếu có.

Skill nên dùng khi thực thi:
- `backend-production`: smoke test và validate contract runtime.
- `fix-code`: nếu cleanup gây regression hoặc lộ bug do cắt nhầm bề mặt.

## Không Làm Trong Plan Này

- Không thêm frontend hoặc trang quản trị.
- Không thêm endpoint trung gian, polling route, upload route, hoặc download route mới.
- Không thêm tài liệu dài dòng ngoài file plan này.
- Không chuẩn bị PR/push workflow trong phạm vi task này.

## Tiêu chí hoàn tất

Chỉ xem là xong khi:

- backend public chỉ còn đúng 4 endpoint đã nêu
- project không còn file runtime thừa liên quan tới bề mặt backend cũ
- text và image đều chạy được cho cả Gemini và GPT
- luồng image không cần route public phụ để lấy ảnh
- script chạy dự án vẫn bring-up được sau cleanup