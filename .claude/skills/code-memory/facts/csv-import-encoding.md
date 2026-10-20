---
name: csv-import-encoding
description: Vì sao trình nhập CSV chỉ hỗ trợ MỘT bảng mã ANSI (cp1252, không phải cp1258) và cách xử lý UTF-16 — đo trên file thi công thật.
type: project
updated: 2026-10-20
---

# Bảng mã của file thi công (trình nhập CSV)

## Sự thật đã đo

- File thi công thật (`docs/REPORT_Thi_Cong_Mang_Gateway_IoT_2026-10-01_v2_HUY(Gateway_List).csv`)
  là **cp1252**, KHÔNG phải UTF-8. `utf-8-sig` ném `UnicodeDecodeError` ngay dòng tiêu đề.
- Tiêu đề trong file **đã mất dấu trước khi lưu** — byte thật là
  `b'STT,M\xe3 Gateway,SN,M\xe3 qu?n l\xfd GDO,\xd0?a ch? IP'`: có **byte `0x3F` (`?`) thật**,
  nên không bảng mã nào khôi phục được. Đây là lý do bắt buộc phải dò cột **theo giá trị**,
  không chỉ theo tên.
- **cp1258 bị cp1252 bao trùm hoàn toàn.** Đã quét toàn dải byte `0x80`–`0xFF`: không byte nào
  cp1258 giải mã được mà cp1252 từ chối. Hệ quả: **không thể hỗ trợ cả hai**.
  - Đặt cp1258 SAU cp1252 thì không bao giờ chạy tới (vô nghĩa).
  - Đặt TRƯỚC thì cướp file cp1252 và **làm sai dấu âm thầm**: `0xE3` thành `ă` thay vì `ã`,
    `0xFD` thành `ư` thay vì `ý`. Không báo lỗi gì cả, nên rất khó phát hiện.
  - Vì vậy `ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "cp1252", "latin-1")`, **không có cp1258**.
- **UTF-16 phải nhận trước ANSI.** Excel *Save As sang Unicode Text* ghi UTF-16. Nếu để nó rơi
  xuống cp1252 thì vẫn "đọc được" (byte `0x00` hợp lệ với cp1252) nhưng ra văn bản đầy `\x00`:
  tiêu đề vỡ vụn, mọi dòng báo "Không xác định được Gateway UID" — **hỏng âm thầm**.
  - Có BOM thì dùng codec trần `"utf-16"` (nó tự nhận byte order **và bỏ BOM**). Dùng
    `utf-16-le`/`utf-16-be` sẽ **giữ BOM** thành `\ufeff` dính vào ô tiêu đề đầu tiên.
  - Mất BOM thì đoán qua mẫu byte NUL: ký tự BMP có byte cao `0x00`, nên từ 80% byte ở một
    phía (chẵn/lẻ) là `0x00` thì đó là UTF-16-be / utf-16-le tương ứng.
- `latin-1` là **chốt hạ**, đứng cuối: nó ánh xạ mọi byte nên không bao giờ ném lỗi.

## Đã biết là còn hạn chế, KHÔNG sửa

- File UTF-16 dùng dấu phân cách **TAB** thì không dò được dấu phân cách.
- File CSV **không có dòng tiêu đề** sẽ mất luôn dòng dữ liệu đầu tiên.

**Vì sao:** file này là file NGƯỜI DÙNG đưa vào, khác mọi chỗ khác trong tool (vốn chỉ đọc/ghi
`utf-8-sig` do tool tự sinh). Nới rộng vừa đủ để không sập, nhưng không thể đúng cho mọi bảng mã.

**Cách áp dụng:** gặp tên gateway sai dấu kiểu `Mă`/`lư` nghĩa là file đang là cp1258. Đổi
`"cp1252"` thành `"cp1258"` trong `ENCODING_CANDIDATES` (`core/device_import.py`) —
chỉ chọn được một, không thể cả hai. Cách chắc chắn nhất vẫn là bảo người dùng lưu lại
thành `CSV UTF-8`. Test canh: `tests/test_device_import.py`, ca
`test_cp1258_khong_the_phan_biet_voi_cp1252` (đỏ nghĩa là giả định đã đổi).
