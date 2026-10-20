---
name: gateway-uid-padding
description: UID firmware publish luôn 12 hex nhưng danh mục nhà máy có dòng 9/10/11 hex — luật zero-pad và vì sao có GATEWAY_UID_MIN.
type: project
updated: 2026-10-20
---

# Zero-pad Gateway UID

## Sự thật đã đo

- Firmware publish `String(getEfuseMac(), HEX).toUpperCase()` — **không zero-pad**, nên các
  nibble `0` ở ĐẦU bị mất tuỳ giá trị MAC. MAC đầy đủ luôn là **12 hex**.
- Phân bố độ dài trong danh mục thật `docs/Danh sach 55 gateway.csv`:
  `{9: 1, 10: 1, 11: 7, 12: 46}`. Tức có thật những UID 9 và 10 hex.
- Đã đối chiếu với `gateway_name` trong `meta.json` của các phiên cũ: pad các độ dài **6–11**
  tái tạo đúng UID thật **39/39** dòng. Không có ca nào sai.

## Luật đang dùng

`format_gateway_uid()` (`core/uid.py`): nếu `GATEWAY_UID_MIN <= len(u) <= 12` thì `u.rjust(12, "0")`;
ngược lại **giữ nguyên**. `GATEWAY_UID_MIN = 6`.

**Vì sao có `GATEWAY_UID_MIN`:** bản sửa đầu tiên pad **mọi** UID ngắn hơn 12, và làm đỏ 21 test
— các fixture 3 hex kiểu `AAA` biến thành `000000000AAA`, đổi khoá tra cứu ở `focused_node` và
`correlation`. Chuỗi dưới 6 hex **không thể là MAC**, nên giữ nguyên.

**Cách áp dụng:** khi so khớp UID giữa hai nguồn, luôn dùng `gateway_uid_variants()`
(sinh `{uid, uid.lstrip("0"), stripped.rjust(12, "0")}`) thay vì so chuỗi thô — `8F9199E139C`
và `08F9199E139C` là **cùng một gateway**. Sửa `format_gateway_uid` thì phải chạy lại toàn bộ
test: hàm này dùng chung ở `analytics/` (correlation, focused_node, gateway_result,
gateway_rescore, refinalize).
