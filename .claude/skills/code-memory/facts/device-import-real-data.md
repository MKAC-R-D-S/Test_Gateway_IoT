---
name: device-import-real-data
description: Số đo trên file thi công thật (40 dòng, 38 mới / 2 đã có) và luật lưu lựa chọn tick theo line_no ở hộp thoại nhập CSV.
type: project
updated: 2026-10-20
---

# Dữ liệu thật & hộp thoại nhập CSV

## Số đo trên file thi công thật

File `docs/REPORT_Thi_Cong_Mang_Gateway_IoT_2026-10-01_v2_HUY(Gateway_List).csv`:
**40 dòng dữ liệu**, bảng mã cp1252, **0 lỗi**, mọi UID 12 hex, 40 UID duy nhất,
tên gợi nhớ lấy từ cột `Mã qu?n lý GDO` (ví dụ `GDO-EMS-1F-GW90`).

Đối chiếu `config/devices.json`: **38 mới / 2 đã có**. Hai dòng bị bỏ qua là `34F9199E139C`
(GDO-PCB4-4F-GW128) và `50F9199E139C` (GDO-PCB4-4F-GW129).

**Vì sao quan trọng:** đây là mốc để biết một thay đổi có làm hỏng việc đọc file thật hay không.
Test `test_file_thi_cong_that` trong `tests/test_device_import.py` khoá lại đúng các con số này.

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
