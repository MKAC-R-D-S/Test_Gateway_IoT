"""Danh mục gateway CỐ ĐỊNH: tra TÊN theo gateway UID.

Vì sao cần
----------
UID gateway (MAC từ `ESP.getEfuseMac()`) và tên gateway là **cố định, gắn với
nhau từ lúc xuất xưởng** — thể hiện trong file danh sách gateway của nhà máy
(`docs/Danh sach 55 gateway.csv`, cột `uuid` + `series`). Nhờ vậy, khi xem lại
một phiên cũ mà `meta.json` **thiếu tên** (phiên của bản tool cũ, hoặc phiên
chạy khi gateway chưa có tên trong `devices.json`), ta tra thẳng trong danh mục
này theo UID là ra tên đúng — **không** phải sửa tay từng phiên, và cũng **không**
phụ thuộc `devices.json` (danh sách gateway đang test, có thể bị xoá/đổi).

Vì sao phải khớp theo BIẾN THỂ UID
----------------------------------
Firmware publish gateway UID bằng `String(getEfuseMac(), HEX)` **không zero-pad**,
nên MAC `08F9199E139C` được publish thành `8F9199E139C` (11 hex). Danh mục và
`devices.json` có thể lưu dạng 12 hex. Vì vậy mọi phép khớp đều dùng
`gateway_uid_variants()` — `8F9199E139C` và `08F9199E139C` là CÙNG một gateway.

Định dạng file danh mục (CSV, UTF-8 hoặc UTF-8 BOM)
---------------------------------------------------
Cột UID: `uuid` (bản gốc nhà máy), hoặc `uid` / `gateway_uid` / `gateway_id` / `mac`.
Cột tên: `series` (bản gốc nhà máy), hoặc `name` / `gateway_name` / `label`, cuối
cùng mới tới `model` (kém đặc trưng: nhiều gateway cùng model).

Dòng thiếu UID hoặc UID không phải hex sẽ bị **bỏ qua** (không raise) — một file
danh mục hỏng không được làm sập giao diện.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .paths import resource_path
from .uid import gateway_name_index, gateway_uid_variants, normalize_hex_uid

#: Vị trí mặc định: `docs/Danh sach 55 gateway.csv` (nguồn do nhà máy cung cấp).
#:
#: Dùng `resource_path()` thay vì `__file__` để bản `.exe` vẫn đọc được danh mục
#: đi kèm (PyInstaller giải nén vào `sys._MEIPASS`); nếu người dùng đặt file
#: cùng tên cạnh exe thì bản đó được ưu tiên.
DEFAULT_CATALOG_PATH = resource_path("docs/Danh sach 55 gateway.csv")

#: Tên cột UID theo thứ tự ưu tiên.
_UID_KEYS = ("uuid", "uid", "gateway_uid", "gateway_id", "mac", "device_uuid")
#: Tên cột TÊN GATEWAY theo thứ tự ưu tiên (`series` đứng trước `model` vì
#: `model` chỉ là dòng sản phẩm, nhiều gateway trùng nhau).
_NAME_KEYS = ("series", "name", "gateway_name", "label", "model")


def catalog_path(path: Optional[object] = None) -> Path:
    """Đường dẫn file danh mục đang dùng (mặc định `docs/Danh sach 55 gateway.csv`)."""
    return Path(path) if path else DEFAULT_CATALOG_PATH


def _pick(row: Dict[str, str], keys: Iterable[str]) -> str:
    """Giá trị của cột đầu tiên có mặt và không rỗng (bỏ qua hoa/thường)."""
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for key in keys:
        value = str(lowered.get(key, "") or "").strip()
        if value:
            return value
    return ""


def load_gateway_catalog(path: Optional[object] = None) -> Dict[str, str]:
    """Đọc danh mục → `{gateway UID (in hoa) → tên}`.

    Trả `{}` khi file không tồn tại/không đọc được hoặc không có cột UID — nơi
    gọi tự quyết định báo cho người dùng. UID trùng lặp: dòng ĐẦU TIÊN thắng.
    """
    file_path = catalog_path(path)
    try:
        # `utf-8-sig` để nuốt BOM nếu file được lưu từ Excel.
        with open(file_path, "r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeDecodeError, csv.Error):
        return {}
    catalog: Dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        uid = normalize_hex_uid(_pick(row, _UID_KEYS))
        if not uid:
            continue
        name = _pick(row, _NAME_KEYS)
        # Không có cột tên thực sự khác UID thì vẫn ghi để `gateway_name_index`
        # tự hiển thị UID; ở đây bỏ trống để người gọi biết là "chưa có tên".
        catalog.setdefault(uid, name)
    return catalog


def _iter_uids(source: object) -> List[str]:
    """Danh sách UID đã chuẩn hoá từ nhiều kiểu đầu vào khác nhau.

    Nhận được: danh sách chuỗi UID, danh sách `GatewayDevice` (có `.uid`), hoặc
    danh sách cặp `(uid, tên)`.
    """
    out: List[str] = []
    for item in source or ():
        if isinstance(item, str):
            raw: Any = item
        else:
            raw = getattr(item, "uid", None)
            if raw is None and isinstance(item, (tuple, list)) and item:
                raw = item[0]
        uid = normalize_hex_uid(raw)
        if uid:
            out.append(uid)
    return out


def lookup_gateway_names(
    source: object,
    path: Optional[object] = None,
    catalog: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Tra tên cho từng UID trong `source` dựa vào danh mục cố định.

    `source` là danh sách UID (chuỗi), danh sách `GatewayDevice` hoặc cặp
    `(uid, tên)`. Kết quả chỉ chứa UID **tra ra được tên**; khoá giữ đúng cách
    viết của UID truyền vào (để ghép thẳng vào `gateway_names` của phiên).

    Khớp theo `gateway_uid_variants` nên danh mục lưu 12 hex vẫn tra được UID
    11 hex do firmware publish (và ngược lại).
    """
    if catalog is None:
        catalog = load_gateway_catalog(path)
    if not catalog:
        return {}
    index = gateway_name_index(catalog.items())
    out: Dict[str, str] = {}
    for uid in _iter_uids(source):
        for variant in gateway_uid_variants(uid):
            name = str(index.get(variant, "") or "").strip()
            if name:
                out[uid] = name
                break
    return out
