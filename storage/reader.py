"""Đọc lại CSV để phục vụ search / thống kê — CSV là source of truth.

Không load toàn bộ file vào GUI: chỉ đọc tối đa `limit` dòng khớp điều kiện,
có phân trang (`offset`) và có thể chạy trong thread nền (xem `ui/search_tab`).
"""

from __future__ import annotations

import csv
import glob
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from .schema import MQTT_DATA_COLUMNS

from core.uid import (
    filter_gateway_uid,
    gateway_uid_variants,
    normalize_hex_uid,
)

log = logging.getLogger(__name__)

TRUE_VALUES = {"1", "true", "yes", "y", "t", "ok"}

#: Giá trị `gateway_name` khi bản tin không thuộc gateway nào trong cấu hình
#: (do `core/parser.py` sinh ra).
UNKNOWN_GATEWAY_NAME = "UNKNOWN_GATEWAY"


@dataclass
class SearchQuery:
    """Điều kiện tìm kiếm trên `mqtt_data_*.csv`."""

    gateway_uid: str = ""
    receiver_uid: str = ""
    node_uid: str = ""
    seq: str = ""
    anomaly: str = ""
    packet_type: str = ""
    error_contains: str = ""
    time_from: Optional[datetime] = None
    time_to: Optional[datetime] = None
    only_valid: Optional[bool] = None
    only_invalid: bool = False
    free_text: str = ""
    files: Sequence[Path] = field(default_factory=list)
    limit: int = 500
    offset: int = 0
    #: index {biến thể gateway UID → tên} để điền lại tên gateway cho kết quả
    #: (xem `_enrich_gateway_row`). Rỗng = không tra được tên.
    gateway_names: Dict[str, str] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not any(
            [
                self.gateway_uid.strip(),
                self.receiver_uid.strip(),
                self.node_uid.strip(),
                self.seq.strip(),
                self.anomaly.strip(),
                self.packet_type.strip(),
                self.error_contains.strip(),
                self.time_from,
                self.time_to,
                self.only_valid is not None,
                self.only_invalid,
                self.free_text.strip(),
            ]
        )


@dataclass
class SearchResult:
    rows: List[Dict[str, Any]] = field(default_factory=list)
    matched: int = 0
    scanned: int = 0
    files_scanned: int = 0
    truncated: bool = False
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Liệt kê file
# ---------------------------------------------------------------------------
def list_csv_files(directory: Path, prefix: str) -> List[Path]:
    """Các file `<prefix>_*.csv` (hoặc `<prefix>.csv`), sắp xếp theo tên."""
    directory = Path(directory)
    if not directory.exists():
        return []
    found = set()
    for pattern in ("%s_*.csv" % prefix, "%s.csv" % prefix):
        for path in glob.glob(str(directory / pattern)):
            found.add(Path(path))
    return sorted(found, key=lambda p: p.name)


def _key_from_filename(path: Path, prefix: str) -> str:
    """Lấy phần key thời gian trong tên file để lọc theo ngày."""
    stem = path.stem
    if stem == prefix:
        return ""
    if stem.startswith(prefix + "_"):
        return stem[len(prefix) + 1 :]
    return ""


def files_for_date_range(
    directory: Path, prefix: str, start: Optional[datetime], end: Optional[datetime]
) -> List[Path]:
    paths = list_csv_files(directory, prefix)
    if start is None and end is None:
        return paths
    out: List[Path] = []
    for path in paths:
        key = _key_from_filename(path, prefix)
        if not key:
            out.append(path)  # file không có key (vd gateway_downtime.csv)
            continue
        if not _key_in_range(key, start, end):
            continue
        out.append(path)
    return out


def _key_in_range(key: str, start: Optional[datetime], end: Optional[datetime]) -> bool:
    if len(key) not in (8, 11):
        return True
    try:
        # Key là giờ LOCAL của PC khi ghi file → so sánh naive.
        if len(key) == 8:
            file_start = datetime.strptime(key, "%Y%m%d")
            file_end = file_start + timedelta(days=1)
        else:
            file_start = datetime.strptime(key, "%Y%m%d_%H")
            file_end = file_start + timedelta(hours=1)
    except ValueError:
        return True
    if start is not None and file_end <= _naive(start):
        return False
    if end is not None and file_start >= _naive(end):
        return False
    return True


def _naive(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def search_mqtt(directory: Path, query: SearchQuery) -> SearchResult:
    result = SearchResult()
    files = list(query.files) or files_for_date_range(
        directory, "mqtt_data", query.time_from, query.time_to
    )
    if not files:
        return result

    gateway = _norm_gateway(query.gateway_uid)
    # Người dùng ĐÃ nhập gì đó nhưng không thể là gateway UID (vd "abc-xyz"):
    # không dòng nào khớp. Phải xử lý tường minh, nếu không `gateway` rỗng sẽ bị
    # hiểu là "không lọc" và trả về TOÀN BỘ dữ liệu — người dùng tưởng bộ lọc
    # đang chạy mà thực ra nó đã bị bỏ qua.
    gateway_unmatchable = bool((query.gateway_uid or "").strip()) and not gateway
    # Các cách viết của cùng UID (11 hex ↔ 12 hex) để tra cả `payload_json`.
    gateway_variants = gateway_uid_variants(gateway) if gateway else set()
    receiver = _norm_uid(query.receiver_uid)
    node = _norm_uid(query.node_uid)
    seq_filter = query.seq.strip()
    anomaly = query.anomaly.strip().upper()
    packet_type = query.packet_type.strip().upper()
    error_contains = query.error_contains.strip().upper()
    free_text = query.free_text.strip().lower()
    tf = _naive(query.time_from) if query.time_from else None
    tt = _naive(query.time_to) if query.time_to else None

    matched_skipped = 0
    for path in files:
        result.files_scanned += 1
        try:
            with open(path, "r", newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    result.scanned += 1
                    if gateway_unmatchable:
                        continue
                    if not _row_matches(
                        row,
                        gateway,
                        gateway_variants,
                        receiver,
                        node,
                        seq_filter,
                        anomaly,
                        packet_type,
                        error_contains,
                        free_text,
                        tf,
                        tt,
                        query.only_valid,
                        query.only_invalid,
                    ):
                        continue
                    result.matched += 1
                    if matched_skipped < max(0, query.offset):
                        matched_skipped += 1
                        continue
                    if len(result.rows) >= max(1, query.limit):
                        result.truncated = True
                        continue
                    # CSV của phiên bản CŨ có thể để TRỐNG `gateway_uid` /
                    # `gateway_name`; bộ lọc đã tra trong `payload_json` nên phần
                    # hiển thị cũng phải được điền lại, nếu không người dùng thấy
                    # "dòng khớp mà cột Gateway trống / tên unknown".
                    _enrich_gateway_row(row, query.gateway_names)
                    result.rows.append(row)
        except (OSError, csv.Error) as exc:
            message = "%s: %s" % (path.name, exc)
            log.warning("search read error %s", message)
            result.errors.append(message)
    return result


def _row_matches(
    row: Dict[str, Any],
    gateway: str,
    gateway_variants: Sequence[str],
    receiver: str,
    node: str,
    seq_filter: str,
    anomaly: str,
    packet_type: str,
    error_contains: str,
    free_text: str,
    time_from: Optional[datetime],
    time_to: Optional[datetime],
    only_valid: Optional[bool],
    only_invalid: bool,
) -> bool:
    if gateway and not _gateway_hit(row, gateway, gateway_variants):
        return False
    if receiver and not _uid_hit(row.get("receiver_uid", ""), receiver):
        return False
    if node and not _uid_hit(row.get("node_uid", ""), node):
        return False
    if seq_filter and not _seq_hit(row.get("seq", ""), seq_filter):
        return False
    if anomaly and row.get("anomaly", "").upper() != anomaly:
        return False
    if packet_type and row.get("packet_type", "").upper() != packet_type:
        return False
    if error_contains and error_contains not in str(row.get("validation_error", "")).upper():
        if error_contains not in str(row.get("error", "")).upper():
            return False
    if only_invalid:
        if str(row.get("valid", "")).strip() in TRUE_VALUES:
            return False
    elif only_valid is not None:
        is_valid = str(row.get("valid", "")).strip() in TRUE_VALUES
        if is_valid != only_valid:
            return False
    if time_from or time_to:
        stamp = _parse_iso(row.get("received_at", ""))
        if stamp is None:
            return False
        if time_from and stamp < time_from:
            return False
        if time_to and stamp > time_to:
            return False
    if free_text:
        haystack = " ".join(
            str(row.get(k, ""))
            for k in (
                "gateway_uid",
                "gateway_name",
                "receiver_uid",
                "node_uid",
                "topic",
                "payload_json",
                "anomaly",
                "error",
                "validation_error",
            )
        ).lower()
        if free_text not in haystack:
            return False
    return True


def _norm_uid(value: str) -> str:
    return (value or "").strip().upper()


def _norm_gateway(value: str) -> str:
    """Chuẩn hoá ô "Gateway UID" trong form tìm kiếm.

    Khác `_norm_uid`: chấp nhận hex ĐỘ DÀI LẺ (11) vì firmware in gateway UID
    bằng `String(ESP.getEfuseMac(), HEX)` KHÔNG zero-pad
    (`config_manager.cpp:7`), và vẫn cho phép khớp MỘT PHẦN UID.
    """
    return filter_gateway_uid(value)


def _gateway_hit(
    row: Dict[str, Any], needle: str, variants: Sequence[str]
) -> bool:
    """True nếu gateway `needle` CÓ tham gia vào bản tin của dòng này.

    Một bản tin LoRa được MỌI gateway trong tầm publish lại, nên tool gộp thành
    1 dòng với `gateway_uid` = gateway RSSI tốt nhất và `gateways` = danh sách
    đầy đủ. Tra theo một gateway phải xét CẢ HAI, nếu không sẽ bỏ sót những dòng
    mà gateway đó chỉ nghe được (không phải gateway tốt nhất).
    """
    if _uid_hit(row.get("gateway_uid", ""), needle):
        return True
    for uid in str(row.get("gateways", "")).split("|"):
        if _uid_hit(uid, needle):
            return True
    return _payload_gateway_hit(row.get("payload_json", ""), needle, variants)


def _payload_gateway_uids(payload: Any) -> List[str]:
    """Các gateway UID có thật trong `payload_json` (`gateway_id` / `uuid`)."""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    found: List[str] = []
    for key in ("gateway_id", "uuid"):
        value = normalize_hex_uid(data.get(key))
        if value and value not in found:
            found.append(value)
    return found


def _payload_gateway_hit(
    payload: Any, needle: str, variants: Sequence[str]
) -> bool:
    """Tra gateway trong `payload_json` — dùng cho các phiên bản CSV cũ.

    CSV sinh bởi bản tool cũ có thể để TRỐNG `gateway_uid` / `gateways` vì lỗi
    UID 11 hex (xem `core/uid.normalize_hex_uid`). Nguồn sự thật của bản tin vẫn
    nằm nguyên trong `payload_json`, nên bộ lọc phải tra tiếp ở đó — nếu không,
    người dùng mở lại phiên chạy cũ sẽ thấy "mất" hết bản tin của gateway đó.

    `variants` là tiền kiểm rẻ tiền (so chuỗi) trước khi parse JSON: chỉ những
    dòng thực sự chứa UID mới bị `json.loads`, nên quét file lớn không chậm.
    """
    text = str(payload or "").upper()
    if not text or not any(v in text for v in variants):
        return False
    return any(_uid_hit(uid, needle) for uid in _payload_gateway_uids(payload))


def _name_for_gateway(uid: str, names: Dict[str, str]) -> str:
    """Tên gateway theo MỌI cách viết của UID (11 hex ↔ 12 hex)."""
    if not names or not uid:
        return ""
    for variant in gateway_uid_variants(uid):
        name = names.get(variant)
        if name:
            return name
    return names.get(uid) or ""


def _enrich_gateway_row(row: Dict[str, Any], names: Dict[str, str]) -> None:
    """Đồng bộ phần HIỂN THỊ gateway với phần LỌC cho các dòng CSV cũ.

    Hai việc:
      1. `gateway_uid` trống → lấy lại từ `payload_json` (gateway thật đã
         publish bản tin). Nhờ vậy cột "ID gateway" không còn để trống.
      2. `gateway_name` trống hoặc `UNKNOWN_GATEWAY` → tra trong danh sách thiết
         bị (theo biến thể UID). Nếu vẫn không có trong cấu hình thì giữ
         `UNKNOWN_GATEWAY` — đúng nghĩa "gateway lạ".

    Không sửa gì khi dòng đã có đủ thông tin, nên chi phí bằng 0 với CSV mới.
    """
    uid = normalize_hex_uid(row.get("gateway_uid"))
    if not uid:
        found = _payload_gateway_uids(row.get("payload_json", ""))
        if not found:
            return
        uid = found[0]
        row["gateway_uid"] = uid
    current = str(row.get("gateway_name", "") or "").strip()
    if current and current.upper() != UNKNOWN_GATEWAY_NAME:
        return
    name = _name_for_gateway(uid, names)
    if name:
        row["gateway_name"] = name
    elif not current:
        row["gateway_name"] = UNKNOWN_GATEWAY_NAME


def _uid_hit(cell: str, needle: str) -> bool:
    cell = normalize_hex_uid(cell)
    if not cell:
        return False
    if needle in cell:
        return True
    # Người dùng thường dán UID 12 hex zero-pad (từ devices.json) trong khi
    # firmware publish 11 hex; khớp MỘT PHẦN cũng phải chịu được nibble 0 đầu.
    stripped = needle.lstrip("0")
    if stripped and stripped != needle and stripped in cell:
        return True
    # cho phép tìm gateway UID thiếu nibble 0 đầu (firmware in HEX không pad)
    if cell.lstrip("0") == needle.lstrip("0") and cell.lstrip("0"):
        return True
    # ... và cả trường hợp ngược lại: cột lưu 11 hex, người dùng nhập 12 hex.
    return bool(gateway_uid_variants(needle) & gateway_uid_variants(cell))


def _seq_hit(cell: str, needle: str) -> bool:
    cell = (cell or "").strip()
    needle = needle.strip()
    if not cell:
        return False
    if needle.isdigit():
        return cell == needle
    match = re.match(r"^(\d+)\s*[-\u2013]\s*(\d+)$", needle)
    if match:
        low, high = int(match.group(1)), int(match.group(2))
        try:
            value = int(float(cell))
        except ValueError:
            return False
        return low <= value <= high
    return needle in cell


def _parse_iso(text: str) -> Optional[datetime]:
    text = (text or "").strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    for parser in (_fromisoformat, _from_common_formats):
        value = parser(candidate)
        if value is not None:
            return _naive(value)
    return None


def _fromisoformat(text: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _from_common_formats(text: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text[: len(fmt) + 6], fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Thống kê nhẹ (không dùng pandas)
# ---------------------------------------------------------------------------
def count_csv_rows(path: Path) -> int:
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as handle:
            return max(0, sum(1 for _ in handle) - 1)
    except OSError:
        return 0


def iter_csv_rows(path: Path, columns: Optional[Sequence[str]] = None) -> Iterator[Dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if columns is not None:
                reader = csv.DictReader(handle, fieldnames=list(columns))
            for row in reader:
                yield row
    except (OSError, csv.Error):
        return


def dataset_overview(directory: Path) -> Dict[str, Any]:
    """Thông tin nhẹ về dataset để hiển thị trong tab Search."""
    out: Dict[str, Any] = {"files": [], "total_rows": 0}
    for prefix in ("mqtt_data",):
        for path in list_csv_files(directory, prefix):
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            rows = count_csv_rows(path)
            out["files"].append({"name": path.name, "rows": rows, "size": size})
            out["total_rows"] += rows
    return out
