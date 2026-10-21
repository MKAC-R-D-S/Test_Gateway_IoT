---
name: device-import-real-data
description: Số đo trên file thi công thật (40 dòng, 39 phân giải được + 1 dòng để trống) và luật lưu lựa chọn tick theo line_no ở hộp thoại nhập CSV.
type: project
updated: 2026-10-20
---

# Dữ liệu thật & hộp thoại nhập CSV

## Số đo trên file thi công thật

File `docs/REPORT_Thi_Cong_Mang_Gateway_IoT_2026-10-01_v2_HUY(Gateway_List).csv`:
**40 dòng dữ liệu**, bảng mã cp1252, tên gợi nhớ lấy từ cột `Mã qu?n lý GDO`
(ví dụ `GDO-EMS-1F-GW90`).

**39 dòng phân giải được** (UID 12 hex, không trùng nhau) + **1 dòng LỖI có chủ ý**:
dòng 38 = `GW-126` (`GDO-PCB4-4F-GW126`) để **trống cả cột `SN` lẫn cột `Ð?a ch? IP`**
vì gateway đó **chưa thi công**. Không có SN thì không tra được danh mục, không có IP
thì không có gì để dò theo giá trị ⇒ `_resolve_row()` báo
`"Khong xac dinh duoc Gateway UID"`. Đây là hành vi ĐÚNG, không phải lỗi cần sửa.

**Vì sao quan trọng — bài học đã trả giá:** test `test_file_thi_cong_that` từng khoá
cứng `38 mới / 2 đã có`, tức khoá vào `config/devices.json`. File đó **đổi mỗi lần
người dùng nhập thiết bị thật**, nên test vỡ oan sau khi nhập 39 gateway vào cấu hình.
Nay test chỉ khẳng định các bất biến **của chính file báo cáo** (40 dòng, đúng 1 dòng
lỗi ở line 38, 39 UID hợp lệ không trùng, `mới + đã có = 39`) — đúng với mọi trạng
thái cấu hình. **Đừng khoá số phụ thuộc `config/devices.json` vào test.**

Cũng vì vậy mà số dòng dữ liệu trong file này có thể bị người dùng sửa tay (họ đã sửa
SN dòng GW-102 `…005`→`…003`, IP dòng GW-113 `.35`→`.51`). Sửa file thật thì phải
chạy lại `pytest tests/test_device_import.py` và cập nhật lại mục này.

## Luật lưu lựa chọn tick (hộp thoại nhập CSV)

Trạng thái tick lưu ở `ImportDevicesDialog._ticked`, khoá theo **`line_no`** (số dòng trong file),
**KHÔNG** theo chỉ số bảng.

**Vì sao:** bảng xem trước có `max_rows = 5000`, nên file dài hơn sẽ bị cắt bớt và **chỉ số bảng
lệch đi** — `line_no` thì không đổi. Nhờ vậy đổi combo ghép cột vẫn giữ nguyên lựa chọn đã tick.

**Cách áp dụng:** `Chọn hết` / `Bỏ chọn hết` phải ghi vào `_ticked` (đủ mọi dòng) rồi mới đẩy
xuống bảng qua `_push_checks()`. Nếu chỉ sửa những dòng ĐANG HIỆN thì các dòng bị cắt vẫn giữ
trạng thái cũ, và dòng đếm dưới bảng sẽ nói dối. Có test riêng cho ca này trong `[6b]` của
`tests/gui_smoke_test.py`. Cẩn thận vòng lặp: `_push_checks()` phát `dataChanged` mà
`_on_check_changed` lại nghe chính tín hiệu đó — có cờ `_syncing` để chặn.
