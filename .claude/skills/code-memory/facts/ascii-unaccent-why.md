---
name: ascii-unaccent-why
description: Vì sao chữ lấy từ CSV bị bỏ dấu (mất chữ qua ANSI, không phải crash) và hai cái bẫy khi viết test cho nó.
type: project
updated: 2026-10-20
---

# Vì sao bỏ dấu chữ lấy từ CSV

## Số đo

Yêu cầu gốc của người dùng là "tránh lỗi UTF-8". **Đo lại thì không có `UnicodeEncodeError`
nào trong luồng thật của tool** — cái xảy ra là **mất chữ** khi văn bản có dấu đi qua bảng
mã ANSI (cp1252):

```
gốc   : Mã quản lý GDO — Cổng tầng 2
cp1252: Mã qu?n lý GDO — C?ng t?ng 2     # ả, ổ → ? vì cp1252 không có chúng
ascii : Ma quan ly GDO  Cong tang 2       # chữ đã bỏ dấu
```

Chữ đã bỏ dấu là **ASCII thuần**, nên **bảng mã nào cũng đọc đúng** — đó là lý do chọn
bỏ dấu thay vì chỉ đi sửa bảng mã.

Bốn lý do không có đường crash thật:
1. `decode_csv_bytes()` đọc bằng bytes với chuỗi bảng mã dự phòng, **không bao giờ ném lỗi**.
2. Mọi file tool tự ghi là `utf-8-sig`/`utf-8` — hai bảng mã này mã hoá được mọi ký tự Unicode.
3. Console đã bị ép UTF-8 sẵn (`main.py:force_utf8_console`).
4. `devices.json` ghi bằng `ensure_ascii=False, encoding="utf-8"` (`core/config.py`).

## Quyết định đã chốt

- Bỏ dấu **cả dữ liệu lẫn nhãn giao diện**, nhưng nhãn **chỉ trong tính năng nhập CSV**.
- Bỏ dấu **luôn luôn**, không có ô tick tắt.
- Bỏ dấu **cả tên gợi nhớ** (`GDO-Trạm Bơm` → `GDO-Tram Bom`). Đây là **mất thông tin thật**,
  đã cân nhắc và chấp nhận; tài liệu người dùng phải nói rõ để không ai tưởng là lỗi.
- **Ranh giới có chủ ý:** chỉ chữ do `core/device_import.py` sinh ra mới bỏ dấu. Lỗi từ
  `device.validate()` (`core/config.py`) **giữ nguyên dấu** — hàm đó dùng chung với hộp
  thoại thêm/sửa tay, nằm ngoài phạm vi "bỏ dấu phần nhập CSV".

## Hai cái bẫy khi viết test (đã vấp cả hai)

1. **`str.encode("cp1252")` NÉM LỖI ngay lúc GHI** với `ạ`, `ơ`, `Đ` — cp1252 không có chúng.
   Test muốn kiểm việc bỏ dấu mà ghi file bằng `cp1252` sẽ hỏng vì lý do chẳng liên quan.
   → Phải ghi bằng `write_bytes(..., "utf-8")`.
2. **Cảnh báo "có chữ có dấu" phải soi Ô GỐC trong file**, không soi `ImportPreviewRow`.
   Giá trị trong row đã qua `to_ascii_text()` rồi, mà hàm đó **idempotent** — đem so lại thì
   không bao giờ thấy khác, cảnh báo không bao giờ hiện. → Dùng `_has_diacritics_in_column()`
   soi `raw_rows`.

## Cách áp dụng

Hàm cần dùng: `to_ascii_text()` (giữ hoa/thường + dấu câu, khác `strip_accents()` — hàm kia
hạ chữ thường và gộp dấu câu thành khoảng trắng, chỉ đúng cho việc so tên cột) và
`has_non_ascii()`. Cảnh báo ở `build_preview()` **cũng phải thuần ASCII**: ngoặc kép cong
`“ ”` và gạch dài `—` tuy chỉ là dấu câu nhưng vẫn ngoài ASCII nên cũng bị ANSI ăn mất.
