"""Chuẩn hoá & phân loại UID theo đúng quy ước firmware.

Nguồn (../IoT_Type0_Gateway/src):
  - gateway_id  = String(ESP.getEfuseMac(), HEX).toUpperCase()  (config_manager.cpp:7)
                  -> hex in hoa, 11-12 ký tự, KHÔNG zero-pad.
  - receiver_id = bytes_to_hex(packet.receiver_id, 8)           (data_process.cpp:366)
                  -> 16 hex, luôn zero-pad.
  - sensor_id   = parsed.uuid, 16 hex (node mới) hoặc 24 hex (node cũ)
                                                                (data_process.cpp:227)
  - Quy ước format ID nhà máy:                                  (data_process.cpp:129)
      node mới /data    : hex[8:16]  == "15E18000"
      node mới 12B      : hex[8:24]  == "15E18000FC8B3004"
      node cũ  (12B)    : hex[0:6]=="343947", hex[8:16]=="33303031",
                          hex[18:20]=="00", hex[22:24]=="00"

Phân loại này CHỈ dùng để hiển thị/thống kê. KHÔNG dùng để drop dữ liệu:
firmware có whitelist cho phép nhận frame lệch format (`packet.force`).
"""

from __future__ import annotations

import re

HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")

#: Độ dài gateway UID mà firmware THƯỜNG in ra: `String(getEfuseMac(), HEX)` bỏ
#: các nibble 0 ở đầu nên thường còn 11 hoặc 12 ký tự. Chỉ để tham chiếu — việc
#: zero-pad không dựa vào hằng số này.
GATEWAY_UID_LENGTHS = (11, 12)

#: Độ dài tối thiểu của một gateway UID (khớp `GATEWAY_UID_RE` ở `core/config.py`).
#: Ngắn hơn mức này không thể là MAC nên `format_gateway_uid` giữ nguyên.
GATEWAY_UID_MIN = 6
# Quy ước nhà máy (main.h:100-106)
UUID_MID = "15E18000"
UUID_NEW_TAIL = "15E18000FC8B3004"
UUID_OLD_PREFIX = "343947"
UUID_OLD_MID = "33303031"

NODE_FORMAT_NEW_MESSAGE = "NEW_NODE_MSG"      # UID 8B, đuôi 15E18000
NODE_FORMAT_NEW_OLD_MSG = "NEW_NODE_OLD_MSG"  # UUID 12B, đuôi 15E18000FC8B3004
NODE_FORMAT_OLD_NODE = "OLD_NODE"             # node STM32L1 12B
NODE_FORMAT_UNKNOWN = "UNKNOWN_FORMAT"        # không khớp 3 quy ước (whitelist/force)

NODE_FORMAT_LABELS = {
    NODE_FORMAT_NEW_MESSAGE: "Node mới (UID 8B)",
    NODE_FORMAT_NEW_OLD_MSG: "Node mới (bản tin 12B)",
    NODE_FORMAT_OLD_NODE: "Node cũ (12B)",
    NODE_FORMAT_UNKNOWN: "Không rõ format",
}


def is_hex(value: object) -> bool:
    """True nếu value là chuỗi hex không rỗng, độ dài chẵn."""
    if not isinstance(value, str):
        return False
    value = value.strip()
    return len(value) > 0 and len(value) % 2 == 0 and HEX_RE.match(value) is not None


def is_hex_uid(value: object) -> bool:
    """True nếu value là chuỗi hex không rỗng, cho phép độ dài LẺ.

    Chỉ dùng cho gateway UID / log topic UUID (xem `normalize_hex_uid`). Dữ liệu
    node (`receiver_id`, `sensor_id`) vẫn phải kiểm bằng `is_hex()`.
    """
    if not isinstance(value, str):
        return False
    value = value.strip()
    return len(value) > 0 and HEX_RE.match(value) is not None


def normalize_uid(value: object) -> str:
    """Chuẩn hoá UID về hex IN HOA, bỏ khoảng trắng. Trả "" nếu không phải hex."""
    if not is_hex(value):
        return ""
    return value.strip().upper()


def normalize_hex_uid(value: object) -> str:
    """Chuẩn hoá một UID hex, chấp nhận cả hex ĐỘ DÀI LẺ.

    Vì sao cần: firmware sinh gateway UID bằng
    `String(ESP.getEfuseMac(), HEX).toUpperCase()` (`config_manager.cpp:7`) nên
    **không zero-pad**. MAC có nibble 0 đầu (ví dụ `08F9199E139C`) được publish
    thành `8F9199E139C` — 11 ký tự, hex độ dài LẺ. Nếu áp `is_hex()` cho những
    giá trị này thì UID bị coi là rỗng và bản tin rơi vào `UNKNOWN_GATEWAY`.

    Dùng hàm này cho **gateway UID** (và log topic). Các UID khác (`receiver_id`,
    `sensor_id`) do firmware zero-pad (`bytes_to_hex`, `data_process.cpp:366/227`)
    nên vẫn dùng `normalize_uid()` để không nới lỏng kiểm tra dữ liệu.
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or HEX_RE.match(text) is None:
        return ""
    return text.upper()


def filter_gateway_uid(value: object) -> str:
    """Giá trị ô tìm kiếm "Gateway UID": hex không rỗng, in hoa.

    Trả "" nếu value không thể là một mảnh gateway UID (vd dán nhầm cả dòng văn
    bản). Người gọi phải phân biệt **rỗng** (không lọc) với **không hợp lệ**
    (không dòng nào khớp) — xem `storage/reader.search_mqtt`.

    KHÔNG giới hạn độ dài: ô tìm kiếm cho phép khớp một phần
    (`ui/search_tab.py` gợi ý "4F9199E139C (khớp một phần cũng được)"), nên
    `9199E1` là giá trị hợp lệ.
    """
    return normalize_hex_uid(value)


def gateway_uid_variants(value: object) -> set:
    """Các cách viết có thể có của CÙNG một gateway UID.

    Gateway UID 12 hex (`08F9199E139C`) và dạng 11 hex do firmware in ra
    (`8F9199E139C`) là **cùng một gateway**; cấu hình `devices.json` có thể lưu
    dạng nào cũng được (dạng 12 hex là MAC đầy đủ). Vì vậy mọi phép so khớp
    gateway phải dùng tập biến thể này, không so chuỗi trực tiếp.

    Lưu ý: dạng 12 hex hex ĐỘ DÀI CHẴN nên cũng hợp lệ với `is_hex()` — chỉ
    dạng 11 hex mới cần hàm này để không bị coi là rỗng.
    """
    uid = normalize_hex_uid(value)
    if not uid:
        return set()
    out = {uid}
    stripped = uid.lstrip("0")
    if stripped:
        out.add(stripped)
        out.add(stripped.rjust(12, "0"))
    return out


def normalize_gateway_uid(value: object) -> str:
    """Chuẩn hoá gateway UID để so khớp (không zero-pad vì firmware cũng vậy)."""
    return normalize_hex_uid(value)


def classify_node_uid(uid: str) -> str:
    """Phân loại UID node theo quy ước nhà máy (data_process.cpp:129-165)."""
    if not is_hex(uid):
        return NODE_FORMAT_UNKNOWN
    n = len(uid)
    if n == 16 and uid[8:16] == UUID_MID:
        return NODE_FORMAT_NEW_MESSAGE
    if n == 24:
        if uid[8:24] == UUID_NEW_TAIL:
            return NODE_FORMAT_NEW_OLD_MSG
        if (
            uid[0:6] == UUID_OLD_PREFIX
            and uid[8:16] == UUID_OLD_MID
            and uid[18:20] == "00"
            and uid[22:24] == "00"
        ):
            return NODE_FORMAT_OLD_NODE
    return NODE_FORMAT_UNKNOWN


def uid_variants(uid: str) -> set:
    """Các biến thể có thể có của cùng một UID để phục vụ tìm kiếm.

    Chỉ áp dụng cho gateway UID (vì `String(...,HEX)` có thể mất nibble 0 đầu).
    """
    out = set()
    for variant in gateway_uid_variants(uid):
        out.add(variant)
        out.add(variant.lower())
    return out


def format_mac_from_gateway_uid(uid: str) -> str:
    """Gợi ý dạng MAC `AA:BB:CC:DD:EE:FF` từ gateway UID (chỉ để hiển thị)."""
    u = normalize_hex_uid(uid)
    if not u:
        return ""
    padded = u.rjust(12, "0")[-12:]
    return ":".join(padded[i : i + 2] for i in range(0, 12, 2))


def format_gateway_uid(uid: str) -> str:
    """Gateway UID dạng hiển thị/so khớp: 12 hex MAC zero-pad.

    Dùng khi cần ghi ra CSV hoặc so với danh sách thiết bị, để gateway 11 hex
    (`8F9199E139C`) và 12 hex (`08F9199E139C`) không bị coi là 2 gateway khác nhau.

    Zero-pad MỌI UID ngắn hơn 12 hex (trong khoảng độ dài hợp lệ), không chỉ
    11/12. Vì sao: MAC đầy đủ là 12 hex, còn firmware chỉ bỏ đi các nibble 0 ở
    ĐẦU khi in `String(mac, HEX)` — nibble 0 đầu có thể mất 1, 2, 3… ký tự tuỳ
    giá trị MAC. Danh mục nhà máy (`docs/Danh sach 55 gateway.csv`) có 1 dòng 9
    hex và 1 dòng 10 hex; nếu chỉ pad độ dài 11/12 thì những UID đó **không bao
    giờ** khớp bản tin MQTT thật (firmware publish luôn 12 hex). Đã đối chiếu với
    tên gateway trong `meta.json` của các phiên cũ: pad các độ dài 6-11 tái tạo
    đúng UID thật 39/39 dòng.

    **Ngưỡng dưới 6 hex**: chuỗi ngắn hơn 6 hex không thể là gateway UID
    (`GATEWAY_UID_RE` đòi 6-16), nên giữ nguyên thay vì pad. Quan trọng vì hàm
    này còn được gọi với những chuỗi không phải UID thật (giá trị thử, tên rút
    gọn); pad bừa chúng sẽ đổi khoá tra cứu ở `focused_node`/`correlation` và làm
    lệch kết quả đối chiếu.

    UID dài hơn 12 giữ nguyên (không phải MAC — vd UID node/log 16-24 hex; cắt
    bớt sẽ làm 2 node khác nhau trùng nhau).
    """
    u = normalize_hex_uid(uid)
    if not u:
        return ""
    if GATEWAY_UID_MIN <= len(u) <= 12:
        return u.rjust(12, "0")
    return u


def gateway_uid_index(rows: object, uid_key: str = "gateway_uid") -> dict:
    """Bảng tra {mọi cách viết của gateway UID → dòng dữ liệu}.

    Dùng cho các bảng "1 dòng / gateway" (`ping_summary`, danh sách thiết bị, …)
    để tra theo UID mà **không** phụ thuộc `devices.json` lưu 11 hay 12 hex:
    `gateway_uid_variants` sinh mọi biến thể nên `8F9199E139C` và `08F9199E139C`
    cùng trỏ về một dòng.

    `rows` là iterable các dict (dòng đã đọc từ CSV hoặc `PingStats.to_summary_row()`).
    Dòng không có UID hợp lệ bị bỏ qua; khoá trùng thì dòng ĐẦU TIÊN thắng.
    """
    index: dict = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        uid = normalize_hex_uid(row.get(uid_key))
        if not uid:
            continue
        for variant in gateway_uid_variants(uid):
            index.setdefault(variant, row)
    return index


def gateway_name_index(items: object) -> dict:
    """Bảng tra {biến thể gateway UID → tên} để gắn tên gateway cho bản tin.

    `items` là iterable các cặp `(uid, name)` (hoặc `(uid, name, ...)`).

    Vì sao cần bảng tra chứ không so chuỗi trực tiếp: `devices.json` có thể lưu
    MAC đầy đủ 12 hex (`08F9199E139C`) trong khi firmware publish dạng 11 hex
    (`8F9199E139C`). Nếu chỉ index theo đúng chuỗi đã lưu thì gateway đó luôn bị
    gắn `UNKNOWN_GATEWAY` dù đã có trong danh sách thiết bị.
    """
    index: dict = {}
    for item in items or ():
        try:
            uid, name = item[0], item[1]
        except (IndexError, TypeError):
            continue
        uid = normalize_hex_uid(uid)
        if not uid:
            continue
        name = str(name or "").strip() or uid
        for variant in gateway_uid_variants(uid):
            index.setdefault(variant, name)
    return index
