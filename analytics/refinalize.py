"""CHỐT LẠI báo cáo cho phiên bị TẮT ĐỘT NGỘT, dựng lại từ dữ liệu thô trên đĩa.

Bối cảnh
--------
Phiên test 3-4 ngày có thể bị kết thúc ngoài ý muốn: mất điện, Windows Update
khởi động lại, Task Manager kill tiến trình. Khi đó:

  - CSV thô vẫn còn nguyên (`mqtt/`, `ping/`) vì writer đã flush + fsync định kỳ;
  - nhưng `reports/` **thiếu**, và `meta.json` còn `status = RUNNING`,
    `ended_at` rỗng (xem `storage.session.describe_status`).

Tab "Lịch sử phiên" đã có nút "⚖ Chấm lại kết quả", nhưng nút đó CHỈ ghi
`gateway_result.csv`. Module này dựng lại **toàn bộ** `reports/` — đúng bộ file
mà `MonitorPipeline.write_reports()` ghi lúc phiên dừng sạch.

Phạm vi GHI (hẹp, có chủ ý)
---------------------------
  - CHỈ ghi trong `reports/` của CHÍNH phiên đó.
  - CHỈ `meta.json` của chính phiên đó (qua `refinalize_session`).
  - **KHÔNG** sửa/xoá/di chuyển bất cứ file nào trong `mqtt/` và `ping/` — dữ
    liệu thô là bằng chứng gốc, còn phải dùng để chấm lại về sau.

Vì sao phải ĐỌC LẠI CSV: tiến trình đã chết nên state trong RAM không còn. Mọi
số liệu ở đây suy ra từ đĩa, và những gì KHÔNG suy ra được sẽ được nói rõ trong
`warnings` thay vì ghi số 0 gây hiểu nhầm:

  - `node_summary.csv`: tái lập được ĐẦY ĐỦ kể cả `missing` / `duplicate` /
    `seq_reset`. Lý do: mỗi dòng `mqtt_data_*.csv` là **một bản tin logic** đã
    gộp mọi bản copy của các gateway cùng nghe, nên nạp lại đúng thứ tự thời
    gian vào `SequenceTracker` cho ra đúng chuỗi SEQ như lúc chạy.
  - `alerts_summary.csv`: KHÔNG tái lập đầy đủ. Ba loại cảnh báo SEQ
    (mất gói / trùng / sai thứ tự) dựng lại được từ cột `seq_status` của
    `mqtt_data_*.csv`; nhưng cảnh báo và cảnh báo hồi phục thì không (chúng phụ
    thuộc trạng thái "lần cuối thấy node/gateway" trong RAM). Nếu phiên đã có
    file này thì GIỮ NGUYÊN bản cũ (bản của lúc chạy, đầy đủ hơn).
  - `gateway_downtime.csv`: cần trạng thái downtime trong RAM → chỉ giữ bản cũ,
    không dựng lại. Số liệu downtime vẫn đủ trong `ping_summary.csv` và
    `run_summary.csv` vì tính được từ ping thô.
  - `focused_node_*.csv`: dựng lại từ chính dòng `mqtt_data` của node trọng tâm
    (định nghĩa BẮT HỤT / BẮN HỤT chỉ cần dữ liệu MQTT, xem
    `analytics/focused_node.py`).
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from storage.reader import iter_csv_rows

log = logging.getLogger(__name__)

#: Hàm ghi 1 file CSV — cùng chữ ký với `LogManager.write_single_csv`.
WriteSingleCsv = Callable[[str, Sequence[str], Sequence[Dict[str, Any]]], Path]

#: Chỉ bản tin nào được đưa vào phân tích chuỗi SEQ (giống lúc chạy).
SEQ_PACKET_TYPES = ("DATA",)
#: Bản tin nào đi qua bộ phát hiện/dựng lại cảnh báo.
ALERT_PACKET_TYPES = ("DATA", "INFO", "DATA_NO_SEQ")
#: Trạng thái SEQ trong `mqtt_data` tương ứng một cảnh báo (mất gói/trùng/sai thứ tự).
SEQ_ALERT_STATUSES: Dict[str, str] = {
    "MISSING": "MISSING_PACKETS",
    "DUPLICATE": "DUPLICATE_PACKETS",
    "OUT_OF_ORDER": "OUT_OF_ORDER_PACKETS",
}


class RefinalizeError(RuntimeError):
    """Không chốt lại được (thiếu dữ liệu thô để dựng báo cáo)."""


@dataclass
class RefinalizeResult:
    """Kết quả chốt lại: file đã ghi + mốc thời gian + cảnh báo cho người dùng."""

    run_id: str = ""
    run_dir: Optional[Path] = None
    written: List[Path] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    #: Mốc kết thúc SUY RA từ dữ liệu (bản tin/ping/log cuối cùng), isoformat.
    ended_at: str = ""
    duration_seconds: Optional[float] = None
    counts: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    #: File GIỮ NGUYÊN bản cũ vì bản dựng lại không đầy đủ hơn.
    kept_existing: List[str] = field(default_factory=list)
    #: Số gateway của phiên (để chỗ gọi ghi vào `meta.json` khi thiếu).
    gateways_monitored: int = 0

    def summary_text(self) -> str:
        """Một dòng kết luận cho GUI/nhật ký."""
        parts = ["Đã chốt lại %d báo cáo cho phiên %s từ dữ liệu thô"
                 % (len(self.written), self.run_id)]
        if self.counts.get("mqtt_rows"):
            parts.append("• %s bản tin MQTT" % _fmt_int(self.counts["mqtt_rows"]))
        if self.counts.get("nodes"):
            parts.append("• %s node" % _fmt_int(self.counts["nodes"]))
        if self.duration_seconds:
            parts.append("• thời lượng đo %s" % _human(self.duration_seconds))
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Nguồn dữ liệu thô
# ---------------------------------------------------------------------------
def raw_sources(run_dir: Path) -> Dict[str, Any]:
    """Các file thô dùng để dựng lại (KHÔNG đọc nội dung — rẻ để gọi trước)."""
    run_dir = Path(run_dir)
    return {
        "run_dir": run_dir,
        "mqtt_files": sorted((run_dir / "mqtt").glob("mqtt_data_*.csv")),
        "ping_files": sorted((run_dir / "ping").glob("ping_log_*.csv")),
        "log_files": sorted((run_dir / "mqtt").glob("mqtt_gateway_log_*.csv")),
        "reports": run_dir / "reports",
    }


def has_raw_data(run_dir: Path) -> bool:
    """Phiên có dữ liệu MQTT thô để chốt lại không?"""
    return bool(raw_sources(run_dir)["mqtt_files"])


def _read_rows(paths: Sequence[Path]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in paths:
        rows.extend(iter_csv_rows(path))
    return rows


def _to_int(value: Any) -> Optional[int]:
    text = str(value if value is not None else "").strip()
    if text == "":
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    text = str(value if value is not None else "").strip()
    if text == "":
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _packet_type(row: Dict[str, Any]) -> str:
    return str(row.get("packet_type", "") or "").strip().upper()


def _session_span(
    *sources: Tuple[Iterable[Dict[str, Any]], Sequence[str]],
) -> Tuple[str, str]:
    """`(mốc sớm nhất, mốc muộn nhất)` của MỌI bằng chứng thời gian trên đĩa.

    Mỗi nguồn là `(các dòng, tên những cột chứa mốc)`. Cố ý nhận NHIỀU tên cột
    cho mỗi nguồn vì tên cột khác nhau tuỳ file: `mqtt_data` dùng `received_at`,
    `ping_log` dùng `timestamp`, còn `ping_summary` (đã tổng hợp) chỉ có
    `first_ping` / `last_ping` — quên cột nào là mốc kết thúc bị hụt đúng phần
    dữ liệu đó. So sánh dạng CHUỖI ISO được vì mọi mốc trong tool đều cùng định
    dạng có offset (`...+07:00`), và giá trị chỉ dùng làm nhãn/thời lượng.
    """
    stamps: List[str] = []
    for rows, columns in sources:
        for row in rows:
            for column in columns:
                value = str(row.get(column) or "").strip()
                if value:
                    stamps.append(value)
    if not stamps:
        return "", ""
    return min(stamps), max(stamps)


# ---------------------------------------------------------------------------
# node_summary: chạy lại SequenceTracker trên các bản tin ĐÃ GỘP
# ---------------------------------------------------------------------------
def _build_node_summary(
    mqtt_rows: Sequence[Dict[str, Any]], params: Any
) -> Tuple[List[Dict[str, Any]], Counter]:
    """Tái lập `node_summary.csv` bằng cách nạp lại `SequenceTracker`.

    Trả `(rows, counters)`. Điều kiện để `missing` / `duplicate` khớp lúc chạy:
    nạp đúng thứ tự thời gian (sắp theo `received_at`) và MỘT lần cho mỗi dòng
    (mỗi dòng đã là một bản tin logic đã gộp).
    """
    from analytics.sequence_tracker import SequenceTracker

    tracker = SequenceTracker(
        seq_max=int(getattr(params, "seq_max", 65535)),
        reset_threshold=int(getattr(params, "seq_reset_threshold", 1024)),
        out_of_order_window=int(getattr(params, "seq_out_of_order_window", 16)),
    )
    counters: Counter = Counter()

    packets = [
        row
        for row in mqtt_rows
        if _packet_type(row) in SEQ_PACKET_TYPES
        and str(row.get("node_uid", "") or "").strip()
        and _to_int(row.get("seq")) is not None
    ]
    packets.sort(
        key=lambda row: (
            str(row.get("received_at") or ""),
            str(row.get("node_uid") or ""),
            _to_int(row.get("seq")) or 0,
        )
    )
    for row in packets:
        outcome = tracker.feed(
            str(row.get("node_uid", "") or ""),
            int(_to_int(row.get("seq"))),
            timestamp=str(row.get("received_at", "") or ""),
            node_format=str(row.get("node_format", "") or ""),
            gateways=_gateway_list(row),
            gateway_uid=str(row.get("gateway_uid", "") or ""),
        )
        if outcome.missing_record is not None:
            counters["missing"] += outcome.missing_record.missing_count
        if outcome.anomaly_record is not None:
            counters["anomaly"] += 1
        if outcome.duplicate:
            counters["duplicate"] += 1
        elif outcome.out_of_order:
            counters["out_of_order"] += 1
        elif outcome.reset:
            counters["reset"] += 1
        elif outcome.wrap:
            counters["wrap"] += 1

    rows: List[Dict[str, Any]] = []
    for _key, state in sorted(tracker.states().items()):
        rate = state.delivery_rate()
        rows.append(
            {
                "node_uid": state.node_uid,
                "node_format": state.node_format,
                "gateways_heard": state.gateways_heard,
                "gateways_heard_avg": state.gateways_heard_avg(),
                "gateways_heard_max": state.gateways_heard_max,
                "packets": state.packets,
                "first_seq": state.first_seq if state.first_seq is not None else "",
                "last_seq": state.last_seq if state.last_seq is not None else "",
                "missing": state.missing,
                "duplicate": state.duplicate,
                "out_of_order": state.out_of_order,
                "seq_reset": state.seq_reset,
                "seq_wrap": state.seq_wrap,
                "delivery_rate_percent": rate if rate is not None else "",
                "first_seen": state.first_seen,
                "last_seen": state.last_seen,
            }
        )
    return rows, counters


def _gateway_list(row: Dict[str, Any]) -> List[str]:
    """Danh sách gateway đã nghe bản tin (cột `gateways`, phân cách `|`)."""
    raw = str(row.get("gateways", "") or "")
    return [part.strip().upper() for part in raw.split("|") if part.strip()]


# ---------------------------------------------------------------------------
# focused_node_*: dựng lại bảng node trọng tâm từ dòng mqtt_data của node đó
# ---------------------------------------------------------------------------
def _build_focused_node(
    mqtt_rows: Sequence[Dict[str, Any]],
    node_uid: str,
    late_seconds: float,
    devices: Sequence[Any] = (),
) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """Dựng lại 3 bảng node trọng tâm. Trả `None` nếu phiên không theo dõi node.

    Định nghĩa (khớp `analytics/focused_node.py`):
      - ``reference`` = tập `seq` mà BẤT KỲ gateway nào đã publish cho node.
      - ``missed_catch`` (BẮT HỤT) = |reference| − số `seq` gateway đó publish.
      - ``late_packets`` (BẮN HỤT) = số LẦN publish có
        ``received_at − gateway_timestamp > late_seconds`` (đo trên mọi bản copy
        để tử số/mẫu số cùng đơn vị).
    """
    from analytics.focused_node import (
        DEFAULT_DETAIL_LIMIT,
        DEFAULT_MAX_RANGES,
        MAX_GATEWAYS,
        normalize_focus_node,
    )
    from analytics.sequence_tracker import DEFAULT_SEQ_MAX
    from core.uid import format_gateway_uid, normalize_uid

    node_uid = normalize_focus_node(node_uid)
    if not node_uid:
        return None

    # Cùng `seq_max` với `SequenceTracker` và `FocusedNodeTracker` lúc chạy.
    seq_max = DEFAULT_SEQ_MAX
    reference: Dict[int, str] = {}
    gateways: Dict[str, Dict[str, Any]] = {}
    first_seen = ""
    last_seen = ""

    for row in mqtt_rows:
        if normalize_uid(row.get("node_uid", "")) != node_uid:
            continue
        seq = _to_int(row.get("seq"))
        if seq is None or not 0 < seq <= seq_max:
            continue
        received_at = str(row.get("received_at", "") or "")
        if received_at:
            if not first_seen or received_at < first_seen:
                first_seen = received_at
            if received_at > last_seen:
                last_seen = received_at
        # Mốc của `seq` trong tập tham chiếu: lần nghe SỚM NHẤT (như lúc chạy).
        if not reference.get(seq) or (
            received_at and received_at < reference[seq]
        ):
            reference[seq] = received_at

        gateway = format_gateway_uid(row.get("gateway_uid", ""))
        if not gateway:
            continue
        state = gateways.get(gateway)
        if state is None:
            if len(gateways) >= MAX_GATEWAYS:
                continue
            state = {
                "gateway_uid": gateway,
                "gateway_name": str(row.get("gateway_name", "") or ""),
                "heard": set(),
                "copies": 0,
                "late": 0,
                "delays": [],
                "late_events": [],
                "first_seen": "",
                "last_seen": "",
            }
            gateways[gateway] = state
        if not state["gateway_name"]:
            state["gateway_name"] = str(row.get("gateway_name", "") or "")
        state["heard"].add(seq)
        state["copies"] += 1
        if received_at:
            if not state["first_seen"] or received_at < state["first_seen"]:
                state["first_seen"] = received_at
            if received_at > state["last_seen"]:
                state["last_seen"] = received_at
        delay = _delay_seconds(received_at, row.get("gateway_timestamp", ""))
        if delay is None:
            continue
        state["delays"].append(delay)
        if delay > late_seconds:
            state["late"] += 1
            _remember_late(state, seq, received_at, row, delay, DEFAULT_DETAIL_LIMIT)

    # Gateway đã CẤU HÌNH mà không publish node trọng tâm vẫn phải xuất hiện với
    # bắt hụt 100% — cùng hành vi `set_known_gateways()` lúc chạy.
    for uid, name in _known_gateways(devices):
        if uid in gateways or len(gateways) >= MAX_GATEWAYS:
            continue
        gateways[uid] = {
            "gateway_uid": uid,
            "gateway_name": name,
            "heard": set(),
            "copies": 0,
            "late": 0,
            "delays": [],
            "late_events": [],
            "first_seen": "",
            "last_seen": "",
        }

    reference_count = len(reference)
    summary_rows: List[Dict[str, Any]] = [_overall_focus_row(node_uid, gateways, reference_count)]
    missing_rows: List[Dict[str, Any]] = []
    late_rows: List[Dict[str, Any]] = []
    # Hụt nhiều nhất trước, rồi trễ nhiều, rồi gateway "câm" — cùng thứ tự lúc chạy.
    for gateway in sorted(
        gateways,
        key=lambda uid: (
            -(reference_count - len(gateways[uid]["heard"])),
            -gateways[uid]["late"],
            gateways[uid]["copies"] > 0,
            uid,
        ),
    ):
        state = gateways[gateway]
        missing = _missing_ranges(state["heard"], reference, seq_max)
        missing_counts = [count for _start, _end, count in missing]
        summary_rows.append(
            _focus_gateway_row(
                node_uid, state, reference, reference_count, missing, missing_counts
            )
        )
        for start, end, count in missing[:DEFAULT_MAX_RANGES]:
            missing_rows.append(
                {
                    "node_uid": node_uid,
                    "gateway_uid": state["gateway_uid"],
                    "gateway_name": state["gateway_name"],
                    "missing_from": start,
                    "missing_to": end,
                    "missing_count": count,
                    "window_start": reference.get(start, ""),
                    "window_end": reference.get(end, ""),
                }
            )
        for event in state["late_events"]:
            late_rows.append(dict(event))

    summary_rows[0]["first_seen"] = first_seen
    summary_rows[0]["last_seen"] = last_seen
    late_rows.sort(
        key=lambda row: float(row.get("delay_seconds") or 0.0), reverse=True
    )
    return {
        "summary": summary_rows,
        "missing": missing_rows,
        "late": late_rows,
    }


def _known_gateways(devices: Sequence[Any]) -> List[Tuple[str, str]]:
    """`[(uid_đã_chuẩn_hoá, tên)]` của gateway đã cấu hình (bỏ UID rỗng)."""
    from core.uid import format_gateway_uid

    out: List[Tuple[str, str]] = []
    seen = set()
    for device in devices or ():
        if isinstance(device, dict):
            uid = str(device.get("uid") or device.get("gateway_uid") or "")
            name = str(device.get("name") or device.get("gateway_name") or "")
        else:
            uid = str(getattr(device, "uid", "") or "")
            name = str(getattr(device, "name", "") or "")
        formatted = format_gateway_uid(uid)
        if formatted and formatted not in seen:
            seen.add(formatted)
            out.append((formatted, name))
    return out


def _missing_ranges(
    heard: set, reference: Dict[int, str], seq_max: int
) -> List[Tuple[int, int, int]]:
    """Các đoạn `seq` liên tiếp CÓ trong reference mà gateway này không publish."""
    ranges: List[Tuple[int, int, int]] = []
    start = -1
    count = 0
    for seq in range(1, seq_max + 1):
        if seq in reference and seq not in heard:
            if start < 0:
                start = seq
            count += 1
        elif start >= 0:
            ranges.append((start, seq - 1, count))
            start = -1
            count = 0
    if start >= 0:
        ranges.append((start, seq_max, count))
    return ranges


def _remember_late(
    state: Dict[str, Any],
    seq: int,
    received_at: str,
    row: Dict[str, Any],
    delay: float,
    limit: int,
) -> None:
    """Giữ lại `limit` ca trễ NẶNG nhất của mỗi gateway (như lúc chạy)."""
    event = {
        "node_uid": row.get("node_uid", ""),
        "gateway_uid": state["gateway_uid"],
        "gateway_name": state["gateway_name"],
        "seq": seq,
        "received_at": received_at,
        "gateway_timestamp": str(row.get("gateway_timestamp", "") or ""),
        "delay_seconds": round(delay, 3),
    }
    events = state["late_events"]
    if len(events) < limit:
        events.append(event)
        return
    lightest = min(range(len(events)), key=lambda i: float(events[i]["delay_seconds"]))
    if delay > float(events[lightest]["delay_seconds"]):
        events[lightest] = event


def _focus_gateway_row(
    node_uid: str,
    state: Dict[str, Any],
    reference: Dict[int, str],
    reference_count: int,
    missing: List[Tuple[int, int, int]],
    missing_counts: Sequence[int],
) -> Dict[str, Any]:
    """1 dòng gateway của `focused_node_summary.csv` (khớp cột bản dựng lúc chạy)."""
    heard = len(state["heard"])
    missed = max(0, reference_count - heard)
    delays = state["delays"]
    gap_runs = len(missing_counts)
    gap_largest = max(missing_counts, default=0)
    # `state` không có sẵn kiểu ngắt quãng vì bitmap của `FocusedNodeTracker` chỉ
    # tồn tại lúc chạy — suy ra từ chính các khoảng hụt vừa dựng lại.
    gap_pattern = _gap_pattern(missing_counts)
    return {
        "node_uid": node_uid,
        "gateway_uid": state["gateway_uid"],
        "gateway_name": state["gateway_name"],
        "packets_heard": heard,
        "copies": state["copies"],
        "reference_packets": reference_count,
        "missed_catch": missed,
        "missed_catch_percent": (
            round(100.0 * missed / reference_count, 2) if reference_count else None
        ),
        "gap_runs": gap_runs,
        "gap_largest": gap_largest,
        "gap_pattern": gap_pattern,
        "late_packets": state["late"],
        "late_percent": (
            round(100.0 * state["late"] / state["copies"], 2)
            if state["copies"] else None
        ),
        "delay_avg_seconds": _round(_mean(delays)),
        "delay_min_seconds": _round(min(delays)) if delays else None,
        "delay_max_seconds": _round(max(delays)) if delays else None,
        "gateways": 1,
        "first_seen": state["first_seen"],
        "last_seen": state["last_seen"],
        "notes": _focus_notes(
            state["copies"], reference_count, missed,
            gap_pattern, state["late"], bool(delays),
        ),
    }


def _overall_focus_row(
    node_uid: str, gateways: Dict[str, Dict[str, Any]], reference_count: int
) -> Dict[str, Any]:
    """Dòng TỔNG của `focused_node_summary.csv` (mẫu số = số gateway trong bảng)."""
    tracked = len(gateways)
    missed = sum(
        max(0, reference_count - len(state["heard"])) for state in gateways.values()
    )
    late = sum(state["late"] for state in gateways.values())
    copies = sum(state["copies"] for state in gateways.values())
    delays = [delay for state in gateways.values() for delay in state["delays"]]
    runs = sum(state.get("gap_runs", 0) for state in gateways.values())
    largest = max((state.get("gap_largest", 0) for state in gateways.values()), default=0)
    return {
        "node_uid": node_uid,
        "gateway_uid": "ALL",
        "gateway_name": "TỔNG (mọi gateway)",
        "packets_heard": sum(len(state["heard"]) for state in gateways.values()),
        "copies": copies,
        "reference_packets": reference_count,
        "missed_catch": missed,
        "missed_catch_percent": (
            round(100.0 * missed / (reference_count * tracked), 2)
            if reference_count and tracked else None
        ),
        "gap_runs": runs,
        "gap_largest": largest,
        "gap_pattern": (
            "Tổng hợp mọi gateway đã cấu hình" if runs else
            ("Không gateway nào bắt hụt" if reference_count else "")
        ),
        "late_packets": late,
        "late_percent": round(100.0 * late / copies, 2) if copies else None,
        "delay_avg_seconds": _round(_mean(delays)),
        "delay_min_seconds": _round(min(delays)) if delays else None,
        "delay_max_seconds": _round(max(delays)) if delays else None,
        "gateways": tracked,
        "first_seen": "",
        "last_seen": "",
        "notes": "Tổng hợp mọi gateway đã cấu hình (mỗi gateway góp 1 dòng).",
    }


def _focus_notes(
    copies: int,
    reference_count: int,
    missed: int,
    gap_pattern: str,
    late_packets: int,
    has_delays: bool,
) -> str:
    """Câu diễn giải cho dòng node trọng tâm — cùng logic `_interpret()` lúc chạy."""
    if copies <= 0:
        if reference_count <= 0:
            return "Chưa có bản tin nào của node trong phiên"
        return (
            "KHÔNG publish bản tin nào của node (bắt hụt 100%) "
            "— kiểm tra gateway còn chạy / còn trong tầm sóng"
        )
    if reference_count <= 0:
        return "Chưa có bản tin nào của node trong phiên"
    parts: List[str] = []
    if missed <= 0:
        parts.append("Không bắt hụt")
    elif reference_count and missed / reference_count >= 0.5:
        parts.append("Bắt hụt rất cao")
    elif reference_count and missed / reference_count >= 0.1:
        parts.append("Bắt hụt cao")
    else:
        parts.append("Bắt hụt thấp")
    if gap_pattern:
        parts.append(gap_pattern)
    if late_packets:
        parts.append("có bắn trễ")
    if not has_delays:
        parts.append("không có bản tin nào kèm timestamp để đo trễ")
    return "; ".join(parts)


def _gap_pattern(counts: Sequence[int]) -> str:
    from analytics.focused_node import _gap_pattern as impl  # noqa: PLC2701

    if not counts:
        return ""
    return impl(list(counts))


def _delay_seconds(received_at: Any, gateway_timestamp: Any) -> Optional[float]:
    from analytics.focused_node import _delay_seconds as impl  # noqa: PLC2701

    return impl(received_at, gateway_timestamp)


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 3)


# ---------------------------------------------------------------------------
# run_summary + alerts
# ---------------------------------------------------------------------------
def _rebuild_run_summary(
    *,
    run_id: str,
    node_rows: Sequence[Dict[str, Any]],
    ping_rows: Sequence[Dict[str, Any]],
    mqtt_rows: Sequence[Dict[str, Any]],
    log_rows: Sequence[Dict[str, Any]],
    gateway_result_rows: Sequence[Dict[str, Any]],
    pause_started_at: str,
    paused_seconds: float,
    now_stamp: str,
) -> Dict[str, Any]:
    """Dòng `run_summary.csv` dựng lại (thay cho bộ đếm trong RAM đã mất)."""
    from analytics.correlation import row_gateways
    from analytics.gateway_result import TOTAL_UID

    def count_type(name: str) -> int:
        return sum(1 for row in mqtt_rows if _packet_type(row) == name)

    total = len(mqtt_rows)
    session_start, session_end = _session_span(
        (mqtt_rows, ("received_at", "received_last_at")),
        (ping_rows, ("first_ping", "last_ping")),
        (log_rows, ("received_at", "gateway_timestamp")),
    )
    session_end = session_end or now_stamp

    # Thời lượng đo = từ mốc đầu tới mốc cuối, TRỪ thời gian tạm dừng đã biết.
    # Với phiên bị tắt ngang thì khoảng tạm dừng đang mở không đo được, nên chỉ
    # trừ phần đã chốt trong meta.json (và ghi rõ việc này khi chốt lại).
    duration: Any = ""
    if session_start and session_end:
        from storage.session import parse_iso

        start_dt, end_dt = parse_iso(session_start), parse_iso(session_end)
        if start_dt and end_dt:
            gap = max(0.0, (end_dt - start_dt).total_seconds())
            # Nếu còn khoảng tạm dừng ĐANG MỞ lúc tắt ngang thì mốc `end` nằm trong
            # khoảng nghỉ → phần nghỉ đó phải bị trừ nốt.
            if pause_started_at:
                pause_dt = parse_iso(pause_started_at)
                if pause_dt and end_dt > pause_dt:
                    gap -= (end_dt - pause_dt).total_seconds()
            duration = round(max(0.0, gap - float(paused_seconds or 0.0)), 3)

    gateway_rows = [
        row for row in gateway_result_rows if row.get("gateway_uid") != TOTAL_UID
    ]
    overall = next(
        (row for row in gateway_result_rows if row.get("gateway_uid") == TOTAL_UID), {}
    )
    failed = sum(1 for row in gateway_rows if row.get("result") == "KHÔNG ĐẠT")

    ping_total = sum(int(_to_int(row.get("ping_total")) or 0) for row in ping_rows)
    ping_success = sum(int(_to_int(row.get("ping_success")) or 0) for row in ping_rows)

    return {
        "run_id": run_id,
        "session_start": session_start,
        # Mốc MUỘN NHẤT có bằng chứng trên đĩa (không phải mốc kết thúc thật —
        # phiên bị tắt ngang không có mốc đó; nơi gọi ghi rõ điều này vào meta).
        "session_end": session_end,
        "duration_seconds": duration,
        "paused_seconds": round(float(paused_seconds or 0.0), 3),
        "broker": "",
        "subscribed_topics": "",
        "total_messages": total,
        "valid_messages": "",
        "invalid_messages": "",
        "data_packets": count_type("DATA"),
        "data_no_seq_packets": count_type("DATA_NO_SEQ"),
        "info_packets": count_type("INFO"),
        "error_packets": count_type("ERROR"),
        "log_messages": count_type("LOG"),
        "unknown_gateway_messages": "",
        "gateways_monitored": "",
        "gateways_seen": len(
            {gateway for row in mqtt_rows for gateway in row_gateways(row)}
        ),
        "nodes_seen": len(node_rows),
        "missing_packets": _sum_column(node_rows, "missing"),
        "duplicate_packets": _sum_column(node_rows, "duplicate"),
        "out_of_order_packets": _sum_column(node_rows, "out_of_order"),
        "seq_reset_events": _sum_column(node_rows, "seq_reset"),
        "seq_wrap_events": _sum_column(node_rows, "seq_wrap"),
        # Không tái lập được: hai bộ đếm này chỉ sống trong RAM của pipeline.
        "parse_errors": "",
        "queue_dropped": "",
        "ping_total": ping_total,
        "ping_failed": max(0, ping_total - ping_success),
        "downtime_events": sum(
            int(_to_int(row.get("downtime_events")) or 0) for row in ping_rows
        ),
        "downtime_seconds": round(
            sum(_to_float(row.get("downtime_seconds")) or 0.0 for row in ping_rows), 3
        ),
        "reconnects": "",
        "gateways_passed": len(gateway_rows) - failed,
        "gateways_failed": failed,
        "result": overall.get("result", ""),
        "result_reason": overall.get("result_reason", ""),
    }


def _sum_column(rows: Sequence[Dict[str, Any]], column: str) -> int:
    return sum(int(_to_int(row.get(column)) or 0) for row in rows)


def _rebuild_alerts(mqtt_rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Cảnh báo dựng lại được từ CSV: mất gói / trùng / sai thứ tự.

    Đọc thẳng cột `seq_status` + `previous_seq` / `expected_seq` / `missing_count`
    do bản thân phiên đã phân tích — nhờ vậy KHÔNG cần dựng lại state detector và
    vẫn trung thực với những gì phiên thật sự đã thấy.

    KHÔNG dựng lại được (và được nêu rõ trong `warnings`): cảnh báo node/gateway
    im lặng và cảnh báo hồi phục — chúng cần trạng thái "lần cuối thấy" trong RAM.
    """
    rows: List[Dict[str, Any]] = []
    for row in mqtt_rows:
        alert_type = SEQ_ALERT_STATUSES.get(
            str(row.get("seq_status", "") or "").strip().upper()
        )
        if not alert_type:
            continue
        rows.append(
            {
                "timestamp": row.get("received_at", ""),
                "level": "WARNING",
                "alert_type": alert_type,
                "gateway_uid": row.get("gateway_uid", ""),
                "gateway_name": row.get("gateway_name", ""),
                "receiver_uid": row.get("receiver_uid", ""),
                "node_uid": row.get("node_uid", ""),
                "message": _alert_message(alert_type, row),
                "details": "previous_seq=%s expected_seq=%s seq=%s"
                % (
                    row.get("previous_seq", ""),
                    row.get("expected_seq", ""),
                    row.get("seq", ""),
                ),
                "value": row.get("missing_count", ""),
            }
        )
    return rows


def _alert_message(alert_type: str, row: Dict[str, Any]) -> str:
    if alert_type == "MISSING_PACKETS":
        count = _to_int(row.get("missing_count")) or 1
        return "Mất %d bản tin (SEQ %s → %s)" % (
            count,
            row.get("previous_seq", "?"),
            row.get("seq", "?"),
        )
    if alert_type == "DUPLICATE_PACKETS":
        return "Bản tin trùng (SEQ %s)" % row.get("seq", "?")
    return "Bản tin sai thứ tự (SEQ %s)" % row.get("seq", "?")


# ---------------------------------------------------------------------------
# Chốt lại
# ---------------------------------------------------------------------------
def refinalize_run(
    run_dir: Path,
    *,
    write_single_csv: WriteSingleCsv,
    devices: Sequence[Any] = (),
    focus_node: str = "",
    late_seconds: float = 30.0,
    params: Any = None,
    paused_seconds: float = 0.0,
    pause_started_at: str = "",
    now_stamp: str = "",
    progress: Optional[Callable[[str], None]] = None,
) -> RefinalizeResult:
    """Dựng lại toàn bộ `reports/` của một phiên bị tắt đột ngột từ dữ liệu thô.

    `write_single_csv` là hàm ghi 1 file (thường bọc `LogManager.write_single_csv`
    để CHẶN ghi đè `alerts_summary.csv` / `gateway_downtime.csv` đã có). Không
    nhận `LogManager` trực tiếp để module này không phụ thuộc vòng vào `storage`.

    Ném `RefinalizeError` nếu phiên không có `mqtt/mqtt_data_*.csv`.
    """
    started = time.monotonic()
    run_dir = Path(run_dir)
    notify = progress or (lambda _message: None)
    result = RefinalizeResult(run_id=run_dir.name, run_dir=run_dir)

    from analytics.correlation import correlate
    from analytics.gateway_result import GatewayResultEvaluator
    from analytics.ping_stats import summarize_ping_csv
    from storage.schema import (
        ALERT_COLUMNS,
        CORRELATION_COLUMNS,
        FOCUSED_NODE_LATE_COLUMNS,
        FOCUSED_NODE_MISSING_COLUMNS,
        FOCUSED_NODE_SUMMARY_COLUMNS,
        GATEWAY_RESULT_COLUMNS,
        NODE_SUMMARY_COLUMNS,
        PING_SUMMARY_COLUMNS,
        RUN_SUMMARY_COLUMNS,
        SILENCE_COLUMNS,
    )

    sources = raw_sources(run_dir)
    if not sources["mqtt_files"]:
        raise RefinalizeError(
            "Phiên không có mqtt/mqtt_data_*.csv — không có dữ liệu thô để dựng "
            "lại báo cáo."
        )

    notify("Đang đọc dữ liệu MQTT thô…")
    mqtt_rows = _read_rows(sources["mqtt_files"])
    notify("Đang đọc log gateway và ping…")
    log_rows = _read_rows(sources["log_files"])
    ping_csv_rows = _read_rows(sources["ping_files"])
    if not ping_csv_rows:
        result.warnings.append(
            "Phiên không có ping/ping_log_*.csv → chỉ số ping trống và tiêu chí "
            "ping để N/A."
        )

    result.gateways_monitored = len(_known_gateways(devices)) or len(
        {gw for row in mqtt_rows for gw in _gateway_list(row)}
    )

    notify("Đang dựng lại bảng node (SEQ)…")
    node_rows, seq_counters = _build_node_summary(mqtt_rows, params)
    result.counts.update(
        {
            "mqtt_rows": len(mqtt_rows),
            "nodes": len(node_rows),
            "missing": seq_counters.get("missing", 0),
            "duplicate": seq_counters.get("duplicate", 0),
            "out_of_order": seq_counters.get("out_of_order", 0),
        }
    )

    notify("Đang dựng lại tương quan ping ↔ MQTT…")
    correlation = correlate(
        ping_csv_rows,
        mqtt_rows,
        log_rows,
        window_seconds=int(getattr(params, "correlation_window_seconds", 60)),
        silent_threshold=int(getattr(params, "silent_window_threshold", 2)),
    )

    notify("Đang dựng lại bảng ping…")
    ping_tracker = summarize_ping_csv(
        ping_csv_rows, max(1, int(getattr(params, "ping_down_threshold", 2)))
    )
    ping_rows = ping_tracker.summary_rows()

    # Ngưỡng trễ: ưu tiên tham số của PHIÊN (`params.focused_node_late_seconds`,
    # lấy từ ảnh chụp trong meta.json) — tham số truyền vào chỉ là phương án dự
    # phòng cho phiên của bản tool cũ.
    late_seconds = float(
        getattr(params, "focused_node_late_seconds", None) or late_seconds
    )
    focused = None
    if focus_node:
        notify("Đang dựng lại bảng node trọng tâm…")
        focused = _build_focused_node(mqtt_rows, focus_node, late_seconds, devices)
    focused_rows = focused["summary"] if focused else []

    notify("Đang chấm ĐẠT / KHÔNG ĐẠT…")
    gateway_result_rows = GatewayResultEvaluator(params).evaluate(
        ping_rows=ping_rows,
        correlation_gateways=correlation.gateways,
        mqtt_rows=mqtt_rows,
        focused_rows=focused_rows,
        devices=devices,
    )

    notify("Đang ghi báo cáo…")
    written: List[Path] = [
        write_single_csv("node_summary.csv", NODE_SUMMARY_COLUMNS, node_rows),
        write_single_csv("ping_summary.csv", PING_SUMMARY_COLUMNS, ping_rows),
    ]
    if focused is not None:
        written.extend(
            [
                write_single_csv(
                    "focused_node_summary.csv",
                    FOCUSED_NODE_SUMMARY_COLUMNS,
                    focused["summary"],
                ),
                write_single_csv(
                    "focused_node_missing.csv",
                    FOCUSED_NODE_MISSING_COLUMNS,
                    focused["missing"],
                ),
                write_single_csv(
                    "focused_node_late.csv",
                    FOCUSED_NODE_LATE_COLUMNS,
                    focused["late"],
                ),
            ]
        )
    written.extend(
        [
            write_single_csv(
                "correlation_summary.csv",
                CORRELATION_COLUMNS,
                correlation.summary_rows(),
            ),
            write_single_csv(
                "mqtt_silence.csv", SILENCE_COLUMNS, correlation.silence_events
            ),
            write_single_csv(
                "gateway_result.csv", GATEWAY_RESULT_COLUMNS, gateway_result_rows
            ),
        ]
    )

    # --- alerts_summary.csv: chỉ dựng lại khi phiên CHƯA có (bản cũ đầy đủ hơn).
    if (sources["reports"] / "alerts_summary.csv").exists():
        result.kept_existing.append("alerts_summary.csv")
    else:
        written.append(
            write_single_csv(
                "alerts_summary.csv", ALERT_COLUMNS, _rebuild_alerts(mqtt_rows)
            )
        )
        result.warnings.append(
            "Phiên chưa có reports/alerts_summary.csv → đã dựng lại phần suy được "
            "từ SEQ (mất gói / trùng / sai thứ tự). Cảnh báo IM LẶNG và cảnh báo "
            "hồi phục không dựng lại được (cần trạng thái trong RAM lúc chạy)."
        )

    # --- gateway_downtime.csv: cần state downtime trong RAM → không dựng lại.
    if not (sources["reports"] / "gateway_downtime.csv").exists():
        result.warnings.append(
            "Không dựng lại được reports/gateway_downtime.csv (cần trạng thái "
            "downtime trong RAM). Số liệu downtime vẫn đủ trong ping_summary.csv "
            "và run_summary.csv vì tính lại được từ ping thô."
        )

    summary = _rebuild_run_summary(
        run_id=run_dir.name,
        node_rows=node_rows,
        ping_rows=ping_rows,
        mqtt_rows=mqtt_rows,
        log_rows=log_rows,
        gateway_result_rows=gateway_result_rows,
        pause_started_at=pause_started_at,
        paused_seconds=paused_seconds,
        now_stamp=now_stamp or _stamp_now(),
    )
    written.append(write_single_csv("run_summary.csv", RUN_SUMMARY_COLUMNS, [summary]))

    result.written = written
    result.ended_at = str(summary.get("session_end") or "")
    duration = summary.get("duration_seconds")
    result.duration_seconds = float(duration) if duration != "" else None
    result.elapsed_seconds = round(time.monotonic() - started, 3)
    notify("Đã chốt xong sau %.1f s." % result.elapsed_seconds)
    return result


def _stamp_now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


def _human(seconds: Any) -> str:
    try:
        seconds = int(max(0, float(seconds or 0)))
    except (TypeError, ValueError):
        return ""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return "%dd %02d:%02d:%02d" % (days, hours, minutes, secs)
    return "%02d:%02d:%02d" % (hours, minutes, secs)


def _fmt_int(value: Any) -> str:
    try:
        return "{:,}".format(int(value)).replace(",", ".")
    except (TypeError, ValueError):
        return str(value)
