"""Tab "Lịch sử phiên" — xem lại kết quả của MỌI phiên test đã chạy.

Mục đích (yêu cầu người dùng): "lưu phiên test những con gateway đó để sau xem lại
lịch sử sau khi dừng phiên test". Tab này:

  - **liệt kê mọi phiên** trong `data/runs/` (mới nhất trước) kèm trạng thái, thời
    lượng, số gateway + danh sách gateway của phiên, số bản tin, dung lượng;
    có ô **tìm theo gateway** (UID hoặc tên) để lọc nhanh ra các phiên đã chạy
    một gateway nào đó;
  - **nhấn vào một phiên** → nạp `reports/*.csv` của phiên đó và hiển thị thành
    các trang: Tổng kết / Gateway / Node / Ping / MQTT↔Ping / Node trọng tâm
    (tổng hợp + bắt hụt + bắn hụt) / Downtime / MQTT im lặng / Cảnh báo;
  - cho phép **mở thư mục phiên**, **nhảy sang tab Tìm kiếm với đúng phạm vi
    phiên đó**, và **xoá phiên** (mỗi phiên có thể chiếm hàng trăm MB).

Nguyên tắc:
  - Mọi thao tác đọc đĩa chạy trong `QThread` (một phiên có thể có CSV hàng trăm
    MB) — UI không bao giờ đứng.
  - **Chỉ đọc `reports/`** khi xem kết quả: `run_summary.csv` là bản chốt 1 dòng
    do `pipeline.write_reports()` ghi, nên rẻ hơn nhiều so với quét lại
    `mqtt_data_*.csv`. Chi tiết thô vẫn tra ở tab "Tìm kiếm".
  - Phiên còn `status = RUNNING` trong `meta.json` mà KHÔNG phải phiên đang chạy
    thật → là dấu vết của lần bị tắt ngang (`storage.session.describe_status`).
"""

from __future__ import annotations

import html
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PyQt5.QtCore import QThread, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from analytics.gateway_rescore import (
    RescoreError,
    rescore_run,
    run_data_sources,
)
from core.gateway_directory import load_gateway_catalog, lookup_gateway_names
from core.uid import gateway_name_index, gateway_uid_variants
from storage.reader import iter_csv_rows
from storage.schema import (
    ALERT_COLUMNS,
    CORRELATION_COLUMNS,
    FOCUSED_NODE_LATE_COLUMNS,
    FOCUSED_NODE_MISSING_COLUMNS,
    FOCUSED_NODE_SUMMARY_COLUMNS,
    GATEWAY_DOWNTIME_COLUMNS,
    GATEWAY_RESULT_COLUMNS,
    NODE_SUMMARY_COLUMNS,
    PING_SUMMARY_COLUMNS,
    SILENCE_COLUMNS,
)
from storage.session import (
    SESSION_LABEL_MAX,
    collect_run_history,
    delete_run_dir,
    fill_session_gateway_names,
    human_size,
    normalize_session_label,
    report_files,
    set_session_label,
)

from . import theme
from .table_model import DictTableModel, fmt_bool, fmt_float, fmt_int

# ---------------------------------------------------------------------------
# Bảng danh sách phiên
# ---------------------------------------------------------------------------
RUN_COLUMNS = [
    "label_text", "run_id", "started_at", "status_label", "duration_label",
    "gateways_monitored", "gateways_label", "total_messages", "nodes_seen",
    "missing_packets", "ping_failed", "reports", "size_label",
]
RUN_HEADERS = [
    "Tên phiên", "Mã phiên", "Bắt đầu", "Trạng thái", "Thời lượng", "Số GW",
    "Gateway của phiên", "Bản tin MQTT", "Node", "Thiếu SEQ", "Ping fail",
    "Số báo cáo", "Dung lượng",
]

#: Chặn số dòng nạp cho mỗi báo cáo — một phiên 3-4 ngày có thể có hàng chục
#: nghìn cảnh báo; bảng GUI không cần hết (CSV vẫn đủ, xem ở tab Tìm kiếm).
MAX_REPORT_ROWS = 20000

#: Các trang kết quả: (khoá, tiêu đề tab, tên file trong `reports/`, cột, header).
#: Cột/header lấy đúng từ `storage/schema.py` nên tên cột CSV luôn khớp nhau.
REPORT_PAGES: List[Tuple[str, str, str, Sequence[str], Sequence[str]]] = [
    (
        "result",
        "Đạt / Không đạt",
        "gateway_result.csv",
        GATEWAY_RESULT_COLUMNS,
        [
            "Gateway", "Tên", "IP", "Bản tin MQTT", "Bản tin đã gửi",
            "Mẫu số (node đã nghe)", "Độ phủ (%)", "Bắt hụt (%)", "Tổng ping",
            "Ping OK (%)", "Số lần mất", "Mất (s)", "Số cửa sổ",
            "Cửa sổ im lặng", "Im lặng (%)", "Kết luận MQTT↔ping (tham khảo)",
            "Bắn hụt (%)", "1. Có dữ liệu", "2. Độ phủ",
            "3. Ping", "4. Bắn hụt",
            "Số tiêu chí đạt", "Số tiêu chí trượt", "Không áp dụng",
            "KẾT QUẢ", "Lý do",
        ],
    ),
    (
        "node",
        "Node",
        "node_summary.csv",
        NODE_SUMMARY_COLUMNS,
        [
            "Node", "Định dạng UID", "Số GW nghe", "GW/bản tin", "GW nhiều nhất",
            "Gói", "SEQ đầu", "SEQ cuối", "Thiếu", "Trùng", "Sai thứ tự",
            "Reset SEQ", "Wrap SEQ", "Tỉ lệ nhận (%)", "Lần đầu", "Lần cuối",
        ],
    ),
    (
        "ping",
        "Ping",
        "ping_summary.csv",
        PING_SUMMARY_COLUMNS,
        [
            "Gateway", "Tên", "IP", "Tổng ping", "Thành công", "Thất bại",
            "Tỉ lệ OK (%)", "RTT TB (ms)", "RTT min", "RTT max",
            "Số lần downtime", "Downtime (s)", "Ping đầu", "Ping cuối",
        ],
    ),
    (
        "correlation",
        "MQTT ↔ Ping",
        "correlation_summary.csv",
        CORRELATION_COLUMNS,
        [
            "Gateway", "Tên", "IP", "Số window", "Window ping OK",
            "Window ping DOWN", "DOWN + MQTT im lặng", "OK nhưng MQTT im lặng",
            "Số bản tin MQTT", "Kết luận", "Ghi chú",
        ],
    ),
    (
        "focus",
        "Node trọng tâm",
        "focused_node_summary.csv",
        FOCUSED_NODE_SUMMARY_COLUMNS,
        [
            "Node", "Gateway", "Tên", "Đã bắt", "Số lần publish", "Node đã phát",
            "BẮT HỤT", "Bắt hụt (%)", "Số khoảng hụt", "Khoảng dài nhất",
            "Kiểu ngắt quãng", "BẮN HỤT", "Bắn hụt (%)", "Trễ TB (s)",
            "Trễ min", "Trễ max", "Số GW", "Lần đầu", "Lần cuối", "Ghi chú",
        ],
    ),
    (
        "focus_missing",
        "Bắt hụt theo SEQ",
        "focused_node_missing.csv",
        FOCUSED_NODE_MISSING_COLUMNS,
        [
            "Node", "Gateway", "Tên", "SEQ từ", "SEQ đến", "Số bản tin",
            "Từ thời điểm", "Đến thời điểm",
        ],
    ),
    (
        "focus_late",
        "Bắn hụt (trễ)",
        "focused_node_late.csv",
        FOCUSED_NODE_LATE_COLUMNS,
        [
            "Node", "Gateway", "Tên", "SEQ", "Giờ PC nhận", "Giờ gateway",
            "Trễ (s)",
        ],
    ),
    (
        "downtime",
        "Downtime",
        "gateway_downtime.csv",
        GATEWAY_DOWNTIME_COLUMNS,
        [
            "Gateway", "Tên", "IP", "Bắt đầu mất", "Hồi phục", "Thời lượng (s)",
            "Số ping fail", "Trạng thái", "Đã hồi phục", "Ping fail cuối",
        ],
    ),
    (
        "silence",
        "MQTT im lặng",
        "mqtt_silence.csv",
        SILENCE_COLUMNS,
        [
            "Gateway", "Kiểu", "Bắt đầu", "Kết thúc", "Thời lượng (s)",
            "Số window", "Window ping OK",
        ],
    ),
    (
        "alerts",
        "Cảnh báo",
        "alerts_summary.csv",
        ALERT_COLUMNS,
        [
            "Thời điểm", "Mức", "Loại", "Gateway", "Tên GW", "Receiver",
            "Node", "Nội dung", "Chi tiết", "Giá trị",
        ],
    ),
]

#: Trang "Tổng kết" — đọc từ `meta.json` + `run_summary.csv` (không cần file riêng).
SUMMARY_FIELDS: List[Tuple[str, str]] = [
    ("run_id", "Mã phiên"),
    ("session_start", "Bắt đầu"),
    ("session_end", "Kết thúc"),
    ("duration_seconds", "Thời lượng (giây)"),
    ("broker", "Broker"),
    ("subscribed_topics", "Topic đã subscribe"),
    ("total_messages", "Tổng bản tin MQTT nhận được"),
    ("valid_messages", "Bản tin hợp lệ"),
    ("invalid_messages", "Bản tin KHÔNG hợp lệ"),
    ("data_packets", "Bản tin DATA (có SEQ)"),
    ("data_no_seq_packets", "DATA không có SEQ (node cũ)"),
    ("info_packets", "Bản tin INFO"),
    ("error_packets", "Bản tin ERROR"),
    ("log_messages", "Bản tin LOG"),
    ("unknown_gateway_messages", "Bản tin từ gateway lạ"),
    ("gateways_monitored", "Gateway đã cấu hình"),
    ("gateways_seen", "Gateway thực sự có bản tin"),
    ("nodes_seen", "Node phát hiện được"),
    ("missing_packets", "Thiếu SEQ"),
    ("duplicate_packets", "Trùng SEQ"),
    ("out_of_order_packets", "Sai thứ tự SEQ"),
    ("seq_reset_events", "Lần reset SEQ"),
    ("seq_wrap_events", "Lần wrap SEQ"),
    ("parse_errors", "Lỗi parse"),
    ("queue_dropped", "Bản tin bị bỏ do hàng đợi đầy"),
    ("ping_total", "Số lần ping"),
    ("ping_failed", "Ping thất bại"),
    ("downtime_events", "Số lần mất/hồi phục"),
    ("downtime_seconds", "Tổng thời gian downtime (giây)"),
    ("reconnects", "Số lần reconnect MQTT"),
    ("gateways_passed", "Gateway ĐẠT"),
    ("gateways_failed", "Gateway KHÔNG ĐẠT"),
    ("result", "KẾT QUẢ PHIÊN"),
    ("result_reason", "Lý do"),
]


def _fmt_dt(text: Any) -> str:
    """`2026-09-21T16:27:27+07:00` → `2026-09-21 16:27:27` (giữ cả ngày)."""
    text = str(text or "").strip()
    if not text:
        return "-"
    candidate = text.replace("Z", "+00:00")
    for sep in ("T", " "):
        if sep in candidate:
            head = candidate.split("+")[0].split(".")[0]
            return head.replace("T", " ")
    return text


#: Header tiếng Việt cho từng trang báo cáo. Cột nào không có ở đây sẽ tự
#: chuyển từ tên cột CSV (`packets_heard` → `packets heard`). Cột nào KHÔNG có
#: trong header nhưng có trong dữ liệu vẫn được hiện — không bỏ im lặng dữ liệu.
PAGE_HEADERS: Dict[Tuple[str, str], str] = {
    ("node", "node_uid"): "Node",
    ("node", "node_format"): "Định dạng",
    ("node", "gateways_heard"): "Số GW nghe",
    ("node", "gateways_heard_avg"): "GW/bản tin (TB)",
    ("node", "gateways_heard_max"): "GW/bản tin (max)",
    ("node", "packets"): "Gói",
    ("node", "first_seq"): "SEQ đầu",
    ("node", "last_seq"): "SEQ cuối",
    ("node", "missing"): "Thiếu",
    ("node", "duplicate"): "Trùng",
    ("node", "out_of_order"): "Sai thứ tự",
    ("node", "seq_reset"): "Reset SEQ",
    ("node", "seq_wrap"): "Wrap SEQ",
    ("node", "delivery_rate_percent"): "Tỉ lệ nhận (%)",
    ("node", "first_seen"): "Lần đầu",
    ("node", "last_seen"): "Lần cuối",
    ("ping", "gateway_uid"): "Gateway",
    ("ping", "gateway_name"): "Tên",
    ("ping", "ip_address"): "IP",
    ("ping", "ping_total"): "Tổng ping",
    ("ping", "ping_success"): "Thành công",
    ("ping", "ping_failed"): "Thất bại",
    ("ping", "success_rate_percent"): "Tỉ lệ OK (%)",
    ("ping", "latency_avg_ms"): "RTT TB (ms)",
    ("ping", "latency_min_ms"): "RTT min (ms)",
    ("ping", "latency_max_ms"): "RTT max (ms)",
    ("ping", "downtime_events"): "Số lần mất kết nối",
    ("ping", "downtime_seconds"): "Downtime (s)",
    ("ping", "first_ping"): "Ping đầu",
    ("ping", "last_ping"): "Ping cuối",
    ("correlation", "gateway_uid"): "Gateway",
    ("correlation", "gateway_name"): "Tên",
    ("correlation", "ip_address"): "IP",
    ("correlation", "windows_total"): "Số window",
    ("correlation", "windows_ping_ok"): "Window ping OK",
    ("correlation", "windows_ping_down"): "Window ping DOWN",
    ("correlation", "windows_ping_down_mqtt_silent"): "Ping DOWN + MQTT im",
    ("correlation", "windows_ping_ok_mqtt_silent"): "Ping OK + MQTT im",
    ("correlation", "mqtt_packets"): "Bản tin MQTT",
    ("correlation", "verdict"): "Kết luận",
    ("correlation", "notes"): "Ghi chú",
    ("focus", "node_uid"): "Node",
    ("focus", "gateway_uid"): "Gateway",
    ("focus", "gateway_name"): "Tên",
    ("focus", "packets_heard"): "Đã bắt",
    ("focus", "copies"): "Số lần publish",
    ("focus", "reference_packets"): "Node đã phát",
    ("focus", "missed_catch"): "BẮT HỤT",
    ("focus", "missed_catch_percent"): "Bắt hụt (%)",
    ("focus", "gap_runs"): "Số khoảng hụt",
    ("focus", "gap_largest"): "Khoảng hụt dài nhất",
    ("focus", "gap_pattern"): "Kiểu ngắt quãng",
    ("focus", "late_packets"): "BẮN HỤT",
    ("focus", "late_percent"): "Bắn hụt (%)",
    ("focus", "delay_avg_seconds"): "Trễ TB (s)",
    ("focus", "delay_min_seconds"): "Trễ min (s)",
    ("focus", "delay_max_seconds"): "Trễ max (s)",
    ("focus", "gateways"): "Số GW",
    ("focus", "first_seen"): "Lần đầu",
    ("focus", "last_seen"): "Lần cuối",
    ("focus", "notes"): "Ghi chú",
    ("focus_missing", "node_uid"): "Node",
    ("focus_missing", "gateway_uid"): "Gateway",
    ("focus_missing", "gateway_name"): "Tên",
    ("focus_missing", "missing_from"): "SEQ từ",
    ("focus_missing", "missing_to"): "SEQ đến",
    ("focus_missing", "missing_count"): "Số bản tin hụt",
    ("focus_missing", "window_start"): "Từ thời điểm",
    ("focus_missing", "window_end"): "Đến thời điểm",
    ("focus_late", "node_uid"): "Node",
    ("focus_late", "gateway_uid"): "Gateway",
    ("focus_late", "gateway_name"): "Tên",
    ("focus_late", "seq"): "SEQ",
    ("focus_late", "received_at"): "Giờ PC nhận",
    ("focus_late", "gateway_timestamp"): "Giờ gateway",
    ("focus_late", "delay_seconds"): "Trễ (s)",
    ("downtime", "gateway_uid"): "Gateway",
    ("downtime", "gateway_name"): "Tên",
    ("downtime", "ip_address"): "IP",
    ("downtime", "down_start"): "Bắt đầu mất",
    ("downtime", "down_end"): "Hồi phục",
    ("downtime", "duration_seconds"): "Thời lượng (s)",
    ("downtime", "failed_ping_count"): "Số ping fail",
    ("downtime", "status"): "Trạng thái",
    ("downtime", "recovered"): "Đã hồi phục",
    ("downtime", "last_down_ping"): "Ping fail cuối",
    ("silence", "gateway_uid"): "Gateway",
    ("silence", "kind"): "Kiểu",
    ("silence", "start"): "Bắt đầu",
    ("silence", "end"): "Kết thúc",
    ("silence", "duration_seconds"): "Thời lượng (s)",
    ("silence", "windows"): "Số window",
    ("silence", "ping_ok_windows"): "Window ping OK",
    ("silence", "ping_down_windows"): "Window ping DOWN",
    ("silence", "interpretation"): "Diễn giải",
    (
        "result",
        "gateway_uid",
    ): "Gateway",
    ("result", "gateway_name"): "Tên",
    ("result", "ip_address"): "IP",
    ("result", "mqtt_packets"): "Bản tin MQTT",
    ("result", "packets_published"): "Bản tin đã gửi",
    ("result", "packets_expected"): "Mẫu số (node đã nghe)",
    ("result", "coverage_percent"): "Độ phủ (%)",
    ("result", "missed_catch_percent"): "Bắt hụt (%)",
    ("result", "ping_total"): "Tổng ping",
    ("result", "ping_success_rate_percent"): "Ping OK (%)",
    ("result", "downtime_events"): "Số lần mất kết nối",
    ("result", "downtime_seconds"): "Mất kết nối (s)",
    ("result", "windows_total"): "Số cửa sổ",
    ("result", "silence_windows"): "Cửa sổ im lặng",
    ("result", "silence_percent"): "Im lặng (%)",
    ("result", "verdict"): "Kết luận MQTT↔ping (tham khảo)",
    ("result", "late_percent"): "Bắn hụt (%)",
    ("result", "c_valid"): "1. Có dữ liệu",
    ("result", "c_coverage"): "2. Độ phủ",
    ("result", "c_ping"): "3. Ping",
    ("result", "c_late"): "4. Bắn hụt",
    ("result", "criteria_passed"): "Số tiêu chí đạt",
    ("result", "criteria_failed"): "Số tiêu chí trượt",
    ("result", "criteria_na"): "Không áp dụng",
    ("result", "result"): "KẾT QUẢ",
    ("result", "result_reason"): "Lý do",
    ("alerts", "timestamp"): "Thời điểm",
    ("alerts", "level"): "Mức",
    ("alerts", "alert_type"): "Loại",
    ("alerts", "gateway_uid"): "Gateway",
    ("alerts", "gateway_name"): "Tên GW",
    ("alerts", "receiver_uid"): "Receiver",
    ("alerts", "node_uid"): "Node",
    ("alerts", "message"): "Nội dung",
    ("alerts", "details"): "Chi tiết",
    ("alerts", "value"): "Giá trị",
}

#: Định dạng số/giờ dùng chung cho mọi trang kết quả.
PAGE_FORMATTERS = {
    "window_start": _fmt_dt, "window_end": _fmt_dt,
    "first_seen": _fmt_dt, "last_seen": _fmt_dt,
    "first_ping": _fmt_dt, "last_ping": _fmt_dt,
    "down_start": _fmt_dt, "down_end": _fmt_dt,
    "last_down_ping": _fmt_dt,
    "start": _fmt_dt, "end": _fmt_dt, "timestamp": _fmt_dt,
    "received_at": _fmt_dt, "gateway_timestamp": _fmt_dt,
    "success_rate_percent": lambda v: fmt_float(v, 2),
    "ping_success_rate_percent": lambda v: fmt_float(v, 2),
    "coverage_percent": lambda v: fmt_float(v, 2),
    "silence_percent": lambda v: fmt_float(v, 2),
    "missed_catch_percent": lambda v: fmt_float(v, 2),
    "late_percent": lambda v: fmt_float(v, 2),
    "delivery_rate_percent": lambda v: fmt_float(v, 2),
    "gateways_heard_avg": lambda v: fmt_float(v, 2),
    "latency_avg_ms": lambda v: fmt_float(v, 1),
    "latency_min_ms": lambda v: fmt_float(v, 1),
    "latency_max_ms": lambda v: fmt_float(v, 1),
    "delay_seconds": lambda v: fmt_float(v, 1),
    "delay_avg_seconds": lambda v: fmt_float(v, 1),
    "delay_min_seconds": lambda v: fmt_float(v, 1),
    "delay_max_seconds": lambda v: fmt_float(v, 1),
    "duration_seconds": lambda v: fmt_float(v, 1),
    "downtime_seconds": lambda v: fmt_float(v, 1),
    "recovered": fmt_bool,
}

#: Cột dấu phẩy hàng nghìn.
PAGE_INT_COLUMNS = (
    "packets_heard", "copies", "reference_packets", "missed_catch",
    "gap_runs", "gap_largest", "late_packets", "gateways", "packets",
    "missing", "duplicate", "out_of_order", "seq_reset", "seq_wrap",
    "gateways_heard", "missing_from", "missing_to", "missing_count", "seq",
    "ping_total", "ping_success", "ping_failed", "downtime_events",
    "failed_ping_count", "windows", "windows_total", "windows_ping_ok",
    "windows_ping_down", "windows_ping_down_mqtt_silent",
    "windows_ping_ok_mqtt_silent", "mqtt_packets", "ping_ok_windows",
    "ping_down_windows",
    # --- trang "Đạt / Không đạt" ---
    "packets_published", "packets_expected", "silence_windows",
    "criteria_passed", "criteria_failed", "criteria_na",
)


#: Tô màu theo cột cho từng trang: `khoá trang → (cột tô màu, bảng màu)`.
#: Trang nào không có ở đây vẫn được tô theo cột `level` (bảng màu mức cảnh báo)
#: như trước — nhiều bảng không có cột `level` nên mặc định này vô hại.
PAGE_COLORS: Dict[str, Tuple[str, Dict[str, str]]] = {
    "result": ("result", dict(theme.RESULT_COLORS)),
    "alerts": ("level", dict(theme.LEVEL_COLORS)),
}


def _page_headers(key: str, columns: Sequence[str]) -> List[str]:
    return [PAGE_HEADERS.get((key, col), col.replace("_", " ")) for col in columns]


def _safe_size(path: Path) -> int:
    """Kích thước file, 0 nếu không đọc được (chỉ để ước lượng thời gian)."""
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


class HistoryWorker(QThread):
    """Nạp danh sách phiên (đọc `meta.json` + `run_summary.csv` của MỌI phiên)."""

    loaded = pyqtSignal(object)   # List[dict]
    failed = pyqtSignal(str)

    def __init__(self, data_dir: Path, active_run_id: str = "", devices=None,
                 parent=None) -> None:
        super().__init__(parent)
        self.data_dir = Path(data_dir)
        self.active_run_id = active_run_id or ""
        # Danh sách thiết bị HIỆN TẠI — chỉ dùng cho phiên KHÔNG có ảnh chụp
        # `gateway_names` trong meta.json (phiên của bản tool cũ).
        self.devices = list(devices or [])

    def run(self) -> None:
        try:
            self.loaded.emit(
                collect_run_history(self.data_dir, self.active_run_id, self.devices)
            )
        except Exception as exc:  # noqa: BLE001 - báo ra GUI thay vì crash
            self.failed.emit("%s: %s" % (type(exc).__name__, exc))


class ReportWorker(QThread):
    """Nạp `reports/*.csv` của MỘT phiên."""

    loaded = pyqtSignal(str, object)   # run_id, {khoá trang: list[dict]}
    failed = pyqtSignal(str)

    def __init__(self, run_id: str, run_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.run_id = run_id
        self.run_dir = Path(run_dir)

    def run(self) -> None:
        try:
            pages: Dict[str, List[Dict[str, Any]]] = {}
            for key, _title, filename, _columns, _headers in REPORT_PAGES:
                pages[key] = self._read(self.run_dir / "reports" / filename)
            self.loaded.emit(self.run_id, pages)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit("%s: %s" % (type(exc).__name__, exc))

    @staticmethod
    def _read(path: Path) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for row in iter_csv_rows(path):
            rows.append(dict(row))
            if len(rows) >= MAX_REPORT_ROWS:
                break
        return rows


class RescoreWorker(QThread):
    """Chấm LẠI ĐẠT/KHÔNG ĐẠT cho một phiên đã chạy (dựng lại từ CSV thô).

    Phiên 3-4 ngày có `mqtt_data_*.csv` hàng trăm MB nên phải đọc ở thread riêng;
    bước này còn chạy lại cả `correlate()` (dựng tương quan ping ↔ MQTT) nên có
    thể mất vài giây. Phát `progress` để nhãn trạng thái báo được tiến trình
    thay vì để người dùng tưởng tool đứng.
    """

    progress = pyqtSignal(str)      # thông báo tiến trình
    done = pyqtSignal(object)       # RescoreResult
    failed = pyqtSignal(str)

    def __init__(self, run_id: str, run_dir: Path, config: Any = None,
                 session_params: Optional[Dict[str, Any]] = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.run_id = run_id
        self.run_dir = Path(run_dir)
        self.config = config
        self.session_params = dict(session_params or {})

    def run(self) -> None:
        try:
            result = rescore_run(
                self.run_dir,
                self.config,
                session_params=self.session_params,
                progress=self.progress.emit,
            )
            self.done.emit(result)
        except RescoreError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - báo ra GUI thay vì crash
            self.failed.emit("%s: %s" % (type(exc).__name__, exc))


class HistoryTab(QWidget):
    """Bảng danh sách phiên + vùng xem kết quả của phiên được chọn."""

    #: Người dùng bấm "Xem trong Tìm kiếm" → MainWindow chuyển tab + đặt phạm vi.
    open_in_search_requested = pyqtSignal(object)   # Path (run_dir)

    def __init__(self, data_dir: Path, devices=None, parent=None,
                 catalog_path: Optional[Path] = None, config: Any = None) -> None:
        super().__init__(parent)
        self.data_dir = Path(data_dir)
        #: File danh mục UID → tên do nhà máy cung cấp (`docs/Danh sach 55
        #: gateway.csv`). Chỉ dùng để BỔ SUNG tên còn thiếu, không ghi đè.
        self.catalog_path = Path(catalog_path) if catalog_path else None
        #: `TestConfig` hiện tại — CHỈ dùng khi chấm lại phiên của bản tool cũ
        #: (phiên chưa có ảnh chụp `scoring_params` trong meta.json). Phiên mới
        #: luôn chấm bằng tham số đã chốt của chính phiên đó.
        self.config = config
        self.devices: List[Any] = list(devices or [])
        self.gateway_names = self._name_index(self.devices)
        self._rows: List[Dict[str, Any]] = []
        self._current: Optional[Dict[str, Any]] = None
        self._list_worker: Optional[HistoryWorker] = None
        self._report_worker: Optional[ReportWorker] = None
        self._rescore_worker: Optional[RescoreWorker] = None
        self._pending: Optional[Dict[str, Any]] = None
        self._models: Dict[str, DictTableModel] = {}
        self._notes: Dict[str, QLabel] = {}
        #: run_id của phiên mà tool ĐANG chạy thật (do MainWindow cập nhật).
        self.active_run_id: str = ""
        #: Kết luận của lần chấm lại gần nhất — giữ lại để nhãn trạng thái không
        #: bị thay bằng "Đang đọc reports/…" ngay sau khi chấm xong.
        self._rescore_note: str = ""

        # LƯU Ý: `DictTableModel` tra màu bằng `str(...).upper()` nên khoá color_map
        # phải VIẾT HOA (tiếng Việt vẫn lên hoa đúng nhờ Unicode).
        self.model = DictTableModel(
            RUN_COLUMNS,
            RUN_HEADERS,
            max_rows=500,
            color_column="status_label",
            color_map={
                "ĐANG CHẠY": theme.COLOR_OK,
                "ĐÃ DỪNG": theme.COLOR_DIM,
                "ĐÃ DỪNG (THIẾU MỐC KẾT THÚC)": theme.COLOR_WARN,
                "TẮT NGANG": theme.COLOR_WARN,
                "TẮT NGANG (KHÔNG CHỐT BÁO CÁO)": theme.COLOR_CRIT,
            },
            formatters={
                "started_at": _fmt_dt,
                "total_messages": fmt_int,
                "nodes_seen": fmt_int,
                "missing_packets": fmt_int,
                "ping_failed": fmt_int,
                "reports": fmt_int,
                "gateways_monitored": fmt_int,
            },
            align_right=("gateways_monitored", "total_messages", "nodes_seen",
                         "missing_packets", "ping_failed", "reports"),
        )
        self._build()
        self.refresh()

    # -------------------------------------------------------------- devices
    @staticmethod
    def _name_index(devices) -> Dict[str, str]:
        return gateway_name_index(
            (getattr(d, "uid", ""), getattr(d, "name", "") or getattr(d, "uid", ""))
            for d in devices or ()
        )

    def set_devices(self, devices) -> None:
        """Cập nhật danh sách thiết bị (để hiện TÊN gateway của phiên cũ).

        LƯU Ý: tên gateway của một phiên được lấy từ ẢNH CHỤP `gateway_names`
        trong `meta.json` của phiên đó (ghi lúc BẮT ĐẦU phiên), nên xoá gateway
        hoặc đổi sang gateway khác trong tab Thiết bị KHÔNG làm đổi dữ liệu lịch
        sử. Danh sách này chỉ dùng cho phiên của bản tool cũ (chưa có ảnh chụp).
        """
        self.devices = list(devices or [])
        self.gateway_names = self._name_index(self.devices)
        # Danh sách phiên đã quét giữ nguyên tên trong `row["gateway_names"]`,
        # nhưng cột "Gateway của phiên" trong BẢNG chỉ hiện UID nên không cần
        # quét lại; phần tiêu đề/trang dùng lại dữ liệu đã có.
        if self._current is not None:
            self._render_header(self._current)
            self._draw_meta_pages(self._current)

    def _name_for(self, uid: str, row: Optional[Dict[str, Any]] = None) -> str:
        """Tên gateway của MỘT phiên: ảnh chụp trong phiên trước, tên hiện tại sau."""
        if not uid:
            return ""
        names = (row or {}).get("gateway_names")
        if names:
            for variant in gateway_uid_variants(uid):
                name = names.get(variant)
                if name:
                    return str(name)
            return ""
        return self.gateway_names.get(str(uid).upper(), "")

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        layout = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.btn_refresh = QPushButton("🔄  Làm mới danh sách")
        self.btn_refresh.clicked.connect(self.refresh)
        self.cmb_filter = QComboBox()
        self.cmb_filter.addItems([
            "Tất cả phiên", "Đã chốt báo cáo", "Tắt ngang / thiếu báo cáo",
        ])
        self.cmb_filter.currentIndexChanged.connect(self._apply_filter)
        bar.addWidget(self.btn_refresh)
        bar.addWidget(QLabel("Lọc:"))
        bar.addWidget(self.cmb_filter)
        bar.addWidget(QLabel("Gateway:"))
        self.ed_gateway = QLineEdit()
        self.ed_gateway.setPlaceholderText(
            "Tìm theo UID hoặc tên gateway — ví dụ 4F9199 hoặc MGWI-V1-26070019"
        )
        self.ed_gateway.setClearButtonEnabled(True)
        self.ed_gateway.setToolTip(
            "Nhập một phần UID (khớp cả khi firmware publish thiếu số 0 đầu,\n"
            "ví dụ 8F9199E139C ≈ 08F9199E139C) hoặc một phần tên gateway.\n"
            "Bảng chỉ hiện những phiên ĐÃ TEST gateway đó."
        )
        self.ed_gateway.textChanged.connect(self._apply_filter)
        bar.addWidget(self.ed_gateway)
        self.btn_clear_search = QPushButton("✖  Xoá tìm kiếm")
        self.btn_clear_search.setToolTip("Bỏ từ khoá gateway, hiện lại mọi phiên")
        self.btn_clear_search.clicked.connect(self._clear_search)
        bar.addWidget(self.btn_clear_search)
        bar.addStretch(1)
        self.lbl_count = QLabel("")
        self.lbl_count.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_DIM)
        bar.addWidget(self.lbl_count)
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Vertical)

        # --- nửa trên: danh sách phiên ---
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(QLabel(
            "Nhấn vào một phiên để xem kết quả test của phiên đó."
        ))
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setFont(theme.mono_font(9))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.clicked.connect(self._on_clicked)
        self.table.doubleClicked.connect(lambda _index: self._open_run_dir())
        top_layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.btn_rename = QPushButton("✏  Đặt / đổi tên phiên")
        self.btn_rename.clicked.connect(self._rename_current)
        self.btn_fill_names = QPushButton("🏷  Bổ sung tên gateway theo UID")
        self.btn_fill_names.setToolTip(
            "Tra tên gateway trong danh mục UID → tên\n"
            "(docs/Danh sach 55 gateway.csv) rồi bổ sung vào phiên đang chọn.\n"
            "Chỉ điền những gateway ĐANG THIẾU tên; tên đã có được giữ nguyên."
        )
        self.btn_fill_names.clicked.connect(self._fill_gateway_names)
        self.btn_open_dir = QPushButton("📂  Mở thư mục phiên")
        self.btn_open_dir.clicked.connect(self._open_run_dir)
        self.btn_open_reports = QPushButton("📊  Mở thư mục báo cáo")
        self.btn_open_reports.clicked.connect(self._open_report_dir)
        self.btn_rescore = QPushButton("⚖  Chấm lại kết quả")
        self.btn_rescore.setToolTip(
            "Dựng lại ĐẠT / KHÔNG ĐẠT của phiên đang chọn từ DỮ LIỆU THÔ\n"
            "(mqtt_data_*.csv, ping_log_*.csv, mqtt_gateway_log_*.csv) rồi\n"
            "ghi thêm reports/gateway_result.csv.\n\n"
            "Dùng cho phiên chạy bằng bản tool cũ hoặc phiên bị tắt ngang nên\n"
            "chưa có báo cáo kết quả. Dữ liệu thô KHÔNG bị sửa hay xoá.\n"
            "Chấm bằng đúng ngưỡng đã chốt trong meta.json của phiên; phiên cũ\n"
            "quá thì lấy ngưỡng từ config.json hiện tại."
        )
        self.btn_rescore.clicked.connect(self._rescore_current)
        self.btn_search = QPushButton("🔍  Xem bản tin thô trong Tìm kiếm")
        self.btn_search.clicked.connect(self._open_in_search)
        self.btn_delete = QPushButton("🗑  Xoá phiên này")
        self.btn_delete.setObjectName("danger")
        self.btn_delete.clicked.connect(self._delete_current)
        actions.addWidget(self.btn_rename)
        actions.addWidget(self.btn_fill_names)
        actions.addWidget(self.btn_rescore)
        actions.addWidget(self.btn_open_reports)
        actions.addWidget(self.btn_open_dir)
        actions.addWidget(self.btn_search)
        actions.addStretch(1)
        actions.addWidget(self.btn_delete)
        top_layout.addLayout(actions)
        splitter.addWidget(top)

        # --- nửa dưới: kết quả của phiên được chọn ---
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        box = QGroupBox("Kết quả phiên được chọn")
        box_layout = QVBoxLayout(box)
        self.lbl_header = QLabel("Chưa chọn phiên nào.")
        self.lbl_header.setWordWrap(True)
        self.lbl_header.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box_layout.addWidget(self.lbl_header)
        self.inner = QTabWidget()
        self._build_pages()
        box_layout.addWidget(self.inner, 1)
        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_DIM)
        box_layout.addWidget(self.lbl_status)
        bottom_layout.addWidget(box)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        layout.addWidget(splitter, 1)

        self._update_buttons()

    def _build_pages(self) -> None:
        # Trang "Tổng kết" (không đọc file báo cáo).
        self._add_page(
            "summary", "Tổng kết", ["metric", "value"], ["Chỉ số", "Giá trị"]
        )
        # Trang "Gateway của phiên" (từ meta.json — chính là "những con gateway đó").
        self._add_page(
            "gateways", "Gateway của phiên",
            ["gateway_uid", "gateway_name"], ["Gateway UID", "Tên gọi nhớ"],
        )
        for key, title, _filename, columns, headers in REPORT_PAGES:
            self._add_page(key, title, columns, headers)

    def _add_page(
        self,
        key: str,
        title: str,
        columns: Sequence[str],
        headers: Sequence[str],
    ) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)

        formatters = dict(PAGE_FORMATTERS)
        for col in PAGE_INT_COLUMNS:
            formatters.setdefault(col, fmt_int)
        # Cột nào không có nhãn tiếng Việt riêng thì tự chuyển từ tên cột CSV.
        labels = list(headers) if headers else _page_headers(key, columns)
        # Màu: trang "Đạt / Không đạt" tô theo cột `result`, trang Cảnh báo tô
        # theo `level`; còn lại giữ mặc định cũ (`level`) cho tương thích.
        color_column, color_map = PAGE_COLORS.get(
            key, ("level", dict(theme.LEVEL_COLORS))
        )
        model = DictTableModel(
            list(columns),
            labels,
            max_rows=MAX_REPORT_ROWS,
            formatters=formatters,
            align_right=PAGE_INT_COLUMNS,
            color_column=color_column,
            color_map=dict(color_map),
        )
        table = QTableView()
        table.setModel(model)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(False)
        table.verticalHeader().setVisible(False)
        table.setFont(theme.mono_font(9))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table, 1)

        note = QLabel("Chưa nạp.")
        note.setWordWrap(True)
        note.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_DIM)
        layout.addWidget(note)

        self._models[key] = model
        self._notes[key] = note
        self.inner.addTab(page, title)

    # ------------------------------------------------------------------ refresh
    def set_active_run_id(self, run_id: str) -> None:
        """MainWindow báo phiên đang chạy thật để không gọi nó là "tắt ngang"."""
        self.active_run_id = run_id or ""

    def refresh(self) -> None:
        if self._list_worker is not None and self._list_worker.isRunning():
            return
        self.btn_refresh.setEnabled(False)
        self.lbl_count.setText("Đang quét data/runs/…")
        self._list_worker = HistoryWorker(
            self.data_dir, self.active_run_id, self.devices, self
        )
        self._list_worker.loaded.connect(self._on_runs_loaded)
        self._list_worker.failed.connect(self._on_failed)
        self._list_worker.finished.connect(
            lambda: self.btn_refresh.setEnabled(True)
        )
        self._list_worker.start()

    def _on_runs_loaded(self, rows: object) -> None:
        self._rows = list(rows or [])
        self._apply_filter()

    def _on_failed(self, message: str) -> None:
        self.lbl_count.setText("Lỗi đọc danh sách phiên: %s" % message)

    def _apply_filter(self) -> None:
        shown = self._filtered_rows()
        self.model.reset_rows(shown)
        self._refresh_count(len(shown))
        self._current = None
        self._pending = None
        self._clear_pages("Chưa chọn phiên nào.")
        self.lbl_header.setText("Chưa chọn phiên nào.")
        self.lbl_status.setText("")
        self._update_buttons()

    def _refresh_count(self, shown: int) -> None:
        """Dòng "x phiên (tổng y) • dung lượng", kèm từ khoá đang tìm."""
        text = "%d phiên (tổng %d) • %s" % (
            shown, len(self._rows),
            human_size(sum(int(row.get("size_bytes") or 0) for row in self._rows)),
        )
        keyword = self._search_text()
        if keyword:
            text = "%s • tìm gateway: %r" % (text, keyword)
        self.lbl_count.setText(text)

    def _filtered_rows(self) -> List[Dict[str, Any]]:
        """Các phiên đang hiện theo BỘ LỌC và TỪ KHÓA GATEWAY hiện tại.

        Không sửa `self._rows` (đó là danh sách đầy đủ đã quét từ đĩa).
        """
        keyword = self._search_text()
        rows = self._rows
        mode = self.cmb_filter.currentIndex()
        if mode == 1:      # đã chốt báo cáo
            rows = [row for row in rows if row.get("finalized")]
        elif mode == 2:    # tắt ngang / thiếu báo cáo
            rows = [row for row in rows
                    if not row.get("finalized") or not row.get("reports")]
        if not keyword:
            return list(rows)
        return [row for row in rows if self._row_matches_gateway(row, keyword)]

    def _search_text(self) -> str:
        """Từ khoá người dùng gõ vào ô tìm gateway (đã gọt khoảng trắng)."""
        return str(self.ed_gateway.text() or "").strip()

    def _clear_search(self) -> None:
        if not self.ed_gateway.text():
            return
        self.ed_gateway.clear()          # textChanged → _apply_filter tự chạy

    @staticmethod
    def _row_matches_gateway(row: Dict[str, Any], keyword: str) -> bool:
        """Phiên có gateway khớp `keyword` (một phần UID hoặc một phần tên)?

        Tra trên CẢ UID lẫn tên của từng gateway trong phiên, dùng ẢNH CHỤP
        `gateway_names`/`gateway_uids` của meta.json nên tìm được cả gateway đã
        bị xoá khỏi tab Thiết bị. UID được so qua `gateway_uid_variants` để khớp
        dù firmware publish thiếu số 0 đầu (`8F9199E139C` ≈ `08F9199E139C`).
        """
        needle = keyword.upper()
        names = {str(k).upper(): str(v or "")
                 for k, v in (row.get("session_gateway_names") or {}).items()}
        for uid in row.get("gateway_uids") or []:
            text = str(uid or "").upper()
            if not text:
                continue
            if needle in text:
                return True
            # Khớp khi gõ UID đủ/thiếu số 0 đầu (ví dụ `08F9199E139C`).
            for variant in gateway_uid_variants(text):
                if needle in variant.upper():
                    return True
                name = names.get(variant.upper(), "")
                if name and needle in name.upper():
                    return True
        return False

    def _clear_pages(self, note: str) -> None:
        for key, model in self._models.items():
            model.reset_rows([])
            self._notes[key].setText(note)

    # ---------------------------------------------------------------- selection
    def _selected_row(self) -> Optional[Dict[str, Any]]:
        index = self.table.currentIndex()
        if not index.isValid():
            return None
        return self.model.row_at(index.row())

    def _on_clicked(self, _index) -> None:
        """Nháy chuột vào một phiên → hiện kết quả của phiên đó.

        Phiên có thể có CSV rất lớn nên việc đọc chạy ở thread riêng. Nếu người
        dùng bấm liên tiếp nhiều phiên, phiên bấm SAU cùng được ghi vào `_pending`
        và tự động nạp ngay khi lượt đọc hiện tại xong (không bị nuốt mất click).
        """
        row = self._selected_row()
        if row is None:
            return
        self._current = row
        self._render_header(row)
        self._draw_meta_pages(row)        # hiện ngay, không cần đọc file
        self._pending = row
        self._start_pending_load()
        self._update_buttons()

    def _start_pending_load(self) -> None:
        if self._pending is None:
            return
        if self._report_worker is not None and self._report_worker.isRunning():
            self.lbl_status.setText(
                "Đang đọc phiên khác, sẽ nạp tiếp %s sau…"
                % str(self._pending.get("run_id") or "")
            )
            return
        row = self._pending
        self._pending = None
        run_id = str(row.get("run_id") or "")
        run_dir = Path(str(row.get("run_dir") or ""))
        if not run_dir.exists():
            self.lbl_status.setText("Thư mục phiên không tồn tại: %s" % run_dir)
            return
        self.lbl_status.setText("Đang đọc reports/ của phiên %s…" % run_id)
        self._report_worker = ReportWorker(run_id, run_dir, self)
        self._report_worker.loaded.connect(self._on_reports_loaded)
        self._report_worker.failed.connect(self._on_failed)
        self._report_worker.finished.connect(self._on_report_load_finished)
        self._report_worker.start()

    def _on_report_load_finished(self) -> None:
        self._report_worker = None
        # Người dùng có thể đã bấm phiên khác trong lúc đang đọc → nạp tiếp.
        self._start_pending_load()

    def _draw_meta_pages(self, row: Dict[str, Any]) -> None:
        """Dựng các trang chỉ cần dự liệu đã có trong dòng lịch sử.

        "Tổng kết" và "Gateway của phiên" đọc từ `meta.json` + `run_summary.csv`
        (đã nạp khi quét danh sách) nên hiện NGAY, không phải chờ đọc file.
        """
        self._models["summary"].reset_rows(self._summary_rows(row))
        self._notes["summary"].setText(
            "Đọc từ meta.json + reports/run_summary.csv."
            if row.get("summary")
            else "⚠ Không tìm thấy reports/run_summary.csv — phiên chưa chốt báo cáo."
        )
        gateways = list(row.get("gateway_uids") or [])
        self._models["gateways"].reset_rows([
            {"gateway_uid": uid, "gateway_name": self._name_for(uid, row) or "(chưa có tên)"}
            for uid in gateways
        ])
        self._notes["gateways"].setText(
            "%d gateway được cấu hình cho phiên này." % len(gateways)
        )
        for key, _title, _filename, _columns, _headers in REPORT_PAGES:
            self._models[key].reset_rows([])
            self._notes[key].setText("Đang nạp…")

    def _render_header(self, row: Dict[str, Any]) -> None:
        uids = list(row.get("gateway_uids") or [])
        if uids:
            parts = []
            for uid in uids:
                name = self._name_for(uid, row)
                parts.append(
                    "%s (%s)" % (html.escape(uid), html.escape(name))
                    if name else html.escape(uid)
                )
            gateway_html = ", ".join(parts)
        else:
            gateway_html = "(không có gateway nào được cấu hình)"
        reports = row.get("report_names") or ""
        title = str(row.get("label_text") or row.get("run_id") or "")
        label = str(row.get("label") or "")
        lines = [
            "<b>%s</b> — %s" % (html.escape(title),
                                html.escape(str(row.get("status_label") or ""))),
        ]
        # Chỉ khi người dùng ĐÃ đặt tên phiên mới hiện thêm mã phiên để đối chiếu
        # với tên thư mục (tên phiên không phải là `run_id`).
        if label:
            lines.append("Mã phiên: %s" % html.escape(str(row.get("run_id") or "")))
        # KẾT QUẢ ĐẠT / KHÔNG ĐẠT của phiên (đọc từ `reports/run_summary.csv`) —
        # tô theo `theme.RESULT_COLORS` như trang "Đạt / Không đạt". Phiên chưa
        # chấm lần nào thì không có dòng này.
        verdict = str(row.get("result")
                      or (row.get("summary") or {}).get("result") or "").strip()
        if verdict:
            lines.append(
                "<span style='color:%s;'>Kết quả: <b>%s</b></span>"
                % (theme.RESULT_COLORS.get(verdict.upper(), theme.COLOR_TEXT),
                   html.escape(verdict))
            )
        lines += [
            "Bắt đầu: %s &nbsp;•&nbsp; Kết thúc: %s &nbsp;•&nbsp; Thời lượng: %s"
            % (html.escape(_fmt_dt(row.get("started_at"))),
               html.escape(_fmt_dt(row.get("ended_at"))),
               html.escape(str(row.get("duration_label") or "-"))),
            "Broker: %s &nbsp;•&nbsp; Dung lượng: %s &nbsp;•&nbsp; %d file"
            % (html.escape(str(row.get("broker") or "-")),
               html.escape(str(row.get("size_label") or "-")),
               int(row.get("files") or 0)),
            "Gateway của phiên (%d): %s" % (len(uids), gateway_html),
        ]
        if row.get("stop_reason"):
            lines.append("Lý do dừng: %s" % html.escape(str(row["stop_reason"])))
        if not row.get("finalized") or not row.get("reports"):
            lines.append(
                "<span style='color:%s;'>⚠ Phiên này không có (hoặc thiếu) "
                "báo cáo trong reports/ — có thể tiến trình bị tắt ngang. "
                "Bản tin thô vẫn còn trong mqtt/ và tra được ở tab Tìm kiếm."
                % theme.COLOR_WARN
            )
        if not reports:
            lines.append(
                "<span style='color:%s;'>Ghi chú: chưa có báo cáo nào được chốt "
                "cho phiên này.</span>" % theme.COLOR_WARN
            )
        self.lbl_header.setText("<br>".join(lines))

    @staticmethod
    def _summary_rows(row: Dict[str, Any]) -> List[Dict[str, Any]]:
        summary = row.get("summary") or {}
        rows: List[Dict[str, Any]] = [
            {"metric": "Trạng thái (tool đọc được)", "value": row.get("status_label") or ""},
            {"metric": "Chốt báo cáo", "value": "Có" if row.get("finalized") else "Không"},
            {"metric": "Số báo cáo trong reports/", "value": str(row.get("reports") or 0)},
            {"metric": "Danh sách báo cáo", "value": row.get("report_names") or ""},
            {"metric": "Số file trong phiên", "value": str(row.get("files") or 0)},
            {"metric": "Dung lượng phiên", "value": row.get("size_label") or ""},
            {"metric": "Phiên bản tool", "value": row.get("tool_version") or ""},
            {"metric": "Thư mục phiên", "value": row.get("run_dir") or ""},
        ]
        known = {field for field, _label in SUMMARY_FIELDS}
        for field, label in SUMMARY_FIELDS:
            if field in ("run_id", "session_start", "session_end"):
                continue
            value = summary.get(field)
            if value in (None, ""):
                value = "-"
            rows.append({"metric": label, "value": str(value)})
        # Cột lạ (bản tool mới thêm sau này) vẫn hiện, không bị bỏ im lặng.
        for field in sorted(summary):
            if field in known:
                continue
            rows.append({"metric": "[%s]" % field, "value": str(summary[field])})
        return rows

    def _on_reports_loaded(self, run_id: str, pages: object) -> None:
        data = dict(pages or {})
        empty = 0
        for key, _title, filename, _columns, _headers in REPORT_PAGES:
            rows = data.get(key) or []
            self._models[key].reset_rows(rows)
            if rows:
                self._notes[key].setText(
                    "%d dòng (từ reports/%s)%s"
                    % (len(rows), filename,
                       " — đã cắt bớt, xem CSV để đủ"
                       if len(rows) >= MAX_REPORT_ROWS else "")
                )
            else:
                empty += 1
                self._notes[key].setText(
                    "Không có dữ liệu: reports/%s trống hoặc chưa được ghi." % filename
                )
        # Kết luận của lần chấm lại vẫn được giữ trong nhãn trạng thái (nếu không
        # nó sẽ bị thay bằng thông báo nạp báo cáo ngay sau khi chấm xong).
        note = self._rescore_note
        self._rescore_note = ""
        self.lbl_status.setText(
            "Đã nạp kết quả phiên %s. %d/%d trang không có dữ liệu.%s"
            % (run_id, empty, len(REPORT_PAGES), (" " + note) if note else "")
        )

    # ------------------------------------------------------------------ actions
    def _update_buttons(self) -> None:
        row = self._selected_row()
        has = row is not None
        running_rescore = (
            self._rescore_worker is not None and self._rescore_worker.isRunning()
        )
        # Phiên ĐANG CHẠY THẬT không được chấm lại: rescorer ghi đè file trong
        # reports/ của chính phiên đó, chạy song song với lúc ghi báo cáo là hỏng
        # dữ liệu. Phiên bị TẮT NGANG cũng còn `status = RUNNING` trong meta.json
        # nên chỉ tin `active_run_id` (xem `_is_active_status`).
        can_rescore = bool(
            has and not self._is_active_status(row) and not running_rescore
        )
        self.btn_rename.setEnabled(has)
        self.btn_fill_names.setEnabled(has)
        self.btn_open_dir.setEnabled(has)
        self.btn_open_reports.setEnabled(bool(has and row.get("reports")))
        self.btn_rescore.setEnabled(can_rescore)
        self.btn_search.setEnabled(has)
        self.btn_delete.setEnabled(has)

    def _rename_current(self) -> None:
        """Đặt/đổi TÊN PHIÊN của phiên đang chọn (kể cả phiên ĐANG CHẠY).

        Tên phiên chỉ là dữ liệu trong `meta.json` của phiên — KHÔNG đổi tên thư
        mục, `run_id` hay bất kỳ CSV nào, nên sửa được bất cứ lúc nào và không
        làm hỏng dữ liệu đã ghi. Để trống rồi bấm OK = XOÁ tên (quay về mã phiên).
        """
        row = self._selected_row()
        if row is None:
            return
        run_id = str(row.get("run_id") or "")
        current = str(row.get("label") or "")
        text, ok = QInputDialog.getText(
            self, "Đặt tên phiên",
            "TÊN PHIÊN cho %s\n\n"
            "(Tên chỉ để dễ nhớ khi xem lịch sử — không đổi mã phiên hay thư mục.\n"
            " Bỏ trống rồi bấm OK để xoá tên, quay về hiện mã phiên.\n"
            " Tối đa %d ký tự.)" % (run_id, SESSION_LABEL_MAX),
            QLineEdit.Normal, current,
        )
        if not ok:
            return
        # Gọt khoảng trắng + giới hạn độ dài y như lúc tạo phiên.
        label = normalize_session_label(text)
        if label == current:
            return
        try:
            set_session_label(Path(str(row.get("run_dir") or "")), label)
        except OSError as exc:
            QMessageBox.warning(self, "Không lưu được tên phiên", str(exc))
            return
        # Cập nhật dòng trong `_rows` (nguồn của bảng) rồi vẽ lại — KHÔNG quét lại
        # đĩa để các trang báo cáo đang mở không bị xoá trắng.
        row["label"] = label
        row["label_text"] = label or run_id
        self._redraw_list()
        if not any(shown is row for shown in self.model.rows()):
            # Bộ lọc/từ khoá đang ẩn phiên này (ví dụ vừa đổi tên nên không còn
            # khớp từ khoá) → đưa ra thông báo thay vì im lặng.
            self.lbl_status.setText(
                "Đã %s phiên %s nhưng phiên này không khớp từ khoá/bộ lọc hiện tại."
                % ("xoá tên của" if not label else "đổi tên", run_id)
            )
            return
        if row is self._current:
            self._render_header(row)
            self._draw_meta_pages(row)
        self.lbl_status.setText(
            "%s phiên %s." % (
                ("Đã xoá tên của" if not label else "Đã đổi tên"), run_id
            )
        )

    # ------------------------------------------------------------- chấm lại
    def _is_active_status(self, row: Optional[Dict[str, Any]]) -> bool:
        """Phiên có phải phiên tool ĐANG CHẠY THẬT? (không được chấm lại).

        Phải so với `active_run_id` (do MainWindow báo vào), KHÔNG chỉ đọc
        `status` trong meta.json: phiên bị TẮT NGANG để lại `status = RUNNING`
        như một vết tích, nên chỉ tin `status` sẽ khoá luôn nút chấm lại của
        những phiên cũ — đúng loại phiên cần chấm lại nhất. Cùng cách hiểu với
        `storage.session.describe_status()`.
        """
        if row is None:
            return False
        if not self.active_run_id:
            return False
        if str(row.get("run_id") or "") != self.active_run_id:
            return False
        return str(row.get("status") or "").strip().upper() == "RUNNING"

    def _rescore_current(self) -> None:
        """Chấm lại ĐẠT/KHÔNG ĐẠT cho phiên đang chọn, dựng lại từ CSV thô.

        Đây là thao tác CHỈ GHI THÊM vào `reports/` — dữ liệu thô trong `mqtt/`
        và `ping/` không bị chạm, nên có thể chạy lại nhiều lần. Vì phải đọc lại
        `mqtt_data_*.csv` (phiên 4 ngày có thể ~900k dòng) và dựng lại cả tương
        quan ping ↔ MQTT, thao tác chạy trong `QThread` và có hỏi xác nhận trước.
        """
        row = self._selected_row()
        if row is None:
            return
        run_id = str(row.get("run_id") or "")
        run_dir = Path(str(row.get("run_dir") or ""))
        if self._rescore_worker is not None and self._rescore_worker.isRunning():
            self.lbl_status.setText("Đang chấm lại một phiên khác, vui lòng đợi…")
            return
        if self._is_active_status(row):
            QMessageBox.warning(
                self, "Không thể chấm lại",
                "Phiên %s đang CHẠY — dừng phiên trước khi chấm lại.\n"
                "(Chấm lại sẽ ghi đè reports/gateway_result.csv nên không được\n"
                " chạy song song với việc ghi dữ liệu của phiên.)" % run_id
            )
            return
        sources = run_data_sources(run_dir)
        if not sources["has_mqtt"]:
            QMessageBox.warning(
                self, "Không chấm lại được",
                "Phiên %s không có mqtt/mqtt_data_*.csv nên không dựng lại được\n"
                "độ phủ bản tin — không chấm được ĐẠT / KHÔNG ĐẠT." % run_id
            )
            return
        notes = [
            "Chấm lại kết quả cho phiên %s?" % run_id,
            "",
            "Tool sẽ DỰNG LẠI kết quả từ dữ liệu thô rồi ghi thêm:",
            "  • reports/gateway_result.csv",
            "  • 4 cột kết quả trong reports/run_summary.csv",
            "",
            "Dữ liệu thô trong mqtt/ và ping/ KHÔNG bị sửa hay xoá.",
        ]
        if not sources["focused_summary"].exists():
            notes.append(
                "\n⚠ Phiên không có reports/focused_node_summary.csv nên tiêu chí\n"
                "  \"Bắn hụt\" sẽ để N/A."
            )
        if not sources["has_ping"]:
            notes.append("\n⚠ Phiên không có dữ liệu ping → tiêu chí ping để N/A.")
        if not self._session_params(row):
            notes.append(
                "\n⚠ Phiên chạy bằng bản tool cũ (meta.json không có ảnh chụp tham\n"
                "  số) → dùng ngưỡng từ config.json hiện tại."
            )
        if len(sources["mqtt_files"]) > 1 or sources["has_mqtt"]:
            total = sum(_safe_size(path) for path in sources["mqtt_files"])
            if total > 5 * 1024 * 1024:
                notes.append(
                    "\nPhiên có %s dữ liệu MQTT thô — có thể mất vài giây."
                    % human_size(total)
                )
        notes.append("\nTiếp tục chấm lại?")
        answer = QMessageBox.question(
            self, "Chấm lại kết quả",
            "\n".join(notes),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.lbl_status.setText("Đang chấm lại phiên %s…" % run_id)
        self.btn_rescore.setEnabled(False)
        self._rescore_worker = RescoreWorker(
            run_id, run_dir, self.config, self._session_params(row), self
        )
        self._rescore_worker.progress.connect(self._on_rescore_progress)
        self._rescore_worker.done.connect(self._on_rescore_done)
        self._rescore_worker.failed.connect(self._on_rescore_failed)
        self._rescore_worker.start()

    def _session_params(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Ảnh chụp tham số chấm của phiên (rỗng với phiên của bản tool cũ)."""
        params = row.get("scoring_params")
        return dict(params) if isinstance(params, dict) else {}

    def _on_rescore_progress(self, message: str) -> None:
        self.lbl_status.setText(str(message))

    def _on_rescore_done(self, result: object) -> None:
        self._rescore_worker = None
        run_id = str(getattr(result, "run_id", "") or "")
        row = self._current
        if row is not None and str(row.get("run_id") or "") == run_id:
            self._refresh_result_row(row, result)
        parts = []
        # Kết luận ("x gateway: y ĐẠT, z KHÔNG ĐẠT — kết luận phiên: …") ĐỨNG ĐẦU
        # câu để người dùng thấy ngay, không bị lẫn vào thông báo phụ.
        summary = getattr(result, "summary_text", None)
        if callable(summary):
            parts.append(str(summary()))
        warnings = list(getattr(result, "warnings", []) or [])
        if warnings:
            parts.append("⚠ " + " | ".join(warnings))
        if getattr(result, "written", None):
            parts.append("Đã ghi: %s" % ", ".join(
                Path(str(p)).name for p in result.written
            ))
        note = " ".join(p for p in parts if p)
        self.lbl_status.setText(note)
        # Nạp lại reports/ để trang "Đạt / Không đạt" hiện số vừa chấm. Lượt nạp
        # đó sẽ gọi `_on_reports_loaded` và GHI ĐÈ nhãn trạng thái, nên kết luận
        # được chuyển sang để nối vào cuối câu thay vì bị mất.
        self._rescore_note = note
        if row is not None:
            self._pending = row
            self._start_pending_load()
        self._update_buttons()

    def _on_rescore_failed(self, message: str) -> None:
        self._rescore_worker = None
        self.lbl_status.setText("⚠ Chấm lại thất bại: %s" % message)
        QMessageBox.warning(self, "Chấm lại thất bại", str(message))
        self._update_buttons()

    def _refresh_result_row(self, row: Dict[str, Any], result: object) -> None:
        """Cập nhật dòng lịch sử tại chỗ để trang "Tổng kết" khớp số vừa chấm.

        `run_summary.csv` đã được rescorer ghi lại 4 cột kết quả, nhưng danh sách
        phiên trong RAM vẫn giữ bản cũ (nạp lúc quét). Sửa tại chỗ để người dùng
        thấy ngay mà KHÔNG phải quét lại toàn bộ `data/runs/`.
        """
        summary = dict(row.get("summary") or {})
        passed = int(getattr(result, "gateways_passed", 0) or 0)
        failed = int(getattr(result, "gateways_failed", 0) or 0)
        overall = str(getattr(result, "result", "") or "")
        reason = str(getattr(result, "result_reason", "") or "")
        summary.update({
            "gateways_passed": passed,
            "gateways_failed": failed,
            "result": overall,
            "result_reason": reason,
        })
        row["summary"] = summary
        row["gateways_passed"] = passed
        row["gateways_failed"] = failed
        row["result"] = overall
        # Đếm LẠI file trong `reports/` (không quét lại cả `data/runs/`): lần chấm
        # lại vừa ghi thêm `gateway_result.csv` nên cột "Số báo cáo", tiêu đề và
        # trang "Tổng kết" phải khớp với đĩa ngay, không chờ lần làm mới sau.
        try:
            names = [path.stem for path in report_files(Path(str(row.get("run_dir") or "")))]
        except OSError:
            names = []
        if names:
            row["reports"] = len(names)
            row["report_names"] = "|".join(names)
        self._render_header(row)
        self._draw_meta_pages(row)

    def _redraw_list(self) -> None:
        """Vẽ lại bảng theo bộ lọc hiện tại, giữ nguyên vùng kết quả đang xem."""
        shown = self._filtered_rows()
        self.model.reset_rows(shown)
        self._refresh_count(len(shown))
        self._update_buttons()

    def _fill_gateway_names(self) -> None:
        """Bổ sung TÊN GATEWAY cho phiên đang chọn theo UID (danh mục nhà máy).

        UID ↔ tên gateway là cố định nên phiên cũ thiếu tên có thể tra thẳng
        trong `docs/Danh sach 55 gateway.csv`. Chỉ ĐIỀN THIẾU: gateway đã có tên
        được giữ nguyên; gateway không có trong phiên hoặc không tra được tên sẽ
        được báo lại chứ không ghi bừa.
        """
        row = self._selected_row()
        if row is None:
            return
        run_id = str(row.get("run_id") or "")
        catalog = load_gateway_catalog(self.catalog_path)
        if not catalog:
            QMessageBox.warning(
                self, "Không đọc được danh mục",
                "Không đọc được file danh mục UID → tên:\n%s\n\n"
                "File phải là CSV có cột `uuid` (UID) và `series` (tên)."
                % (self.catalog_path or "docs/Danh sach 55 gateway.csv")
            )
            return
        missing = self._missing_name_uids(row)
        if not missing:
            self.lbl_status.setText(
                "Phiên %s đã có tên cho cả %d gateway — không cần bổ sung."
                % (run_id, len(row.get("gateway_uids") or []))
            )
            return
        mapping = lookup_gateway_names(missing, catalog=catalog)
        if not mapping:
            QMessageBox.information(
                self, "Không tra được tên",
                "Không tra được tên cho %d gateway còn thiếu của phiên %s.\n"
                "Có thể các UID này không có trong danh mục."
                % (len(missing), run_id)
            )
            return
        try:
            result = fill_session_gateway_names(Path(str(row.get("run_dir") or "")),
                                                mapping)
        except OSError as exc:
            QMessageBox.warning(self, "Không lưu được", str(exc))
            return
        if not result.get("changed"):
            self.lbl_status.setText(
                "Không có tên nào được bổ sung cho phiên %s." % run_id
            )
            return
        # Cập nhật lại dòng đang hiển thị (KHÔNG quét lại đĩa để giữ trang kết quả).
        names = dict(row.get("session_gateway_names") or {})
        names.update(mapping)
        row["session_gateway_names"] = names
        row["gateway_names"] = gateway_name_index(names.items())
        if row is self._current:
            self._render_header(row)
            self._draw_meta_pages(row)
        self.lbl_status.setText(
            "Đã bổ sung tên cho %d gateway của phiên %s%s."
            % (result["changed"], run_id,
               " (bỏ qua %d gateway đã có tên)" % result["skipped"]
               if result.get("skipped") else "")
        )

    def _missing_name_uids(self, row: Dict[str, Any]) -> List[str]:
        """Danh sách UID của phiên CHƯA có tên trong ảnh chụp `gateway_names`."""
        names = row.get("session_gateway_names") or {}
        index = gateway_name_index(dict(names).items()) if names else {}
        missing: List[str] = []
        for uid in row.get("gateway_uids") or []:
            key = str(uid or "").upper()
            if not key:
                continue
            if any(index.get(variant) for variant in gateway_uid_variants(key)):
                continue
            missing.append(key)
        return missing

    def _open_in_search(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        self.open_in_search_requested.emit(Path(str(row.get("run_dir") or "")))

    def _open_dir(self, path: Path) -> None:
        path = Path(path)
        if not path.exists():
            QMessageBox.information(
                self, "Không mở được", "Thư mục không tồn tại:\n%s" % path
            )
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # noqa: S606 - mở explorer
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            QMessageBox.warning(self, "Không mở được", str(exc))

    def _open_run_dir(self) -> None:
        row = self._selected_row()
        if row is not None:
            self._open_dir(Path(str(row.get("run_dir") or "")))

    def _open_report_dir(self) -> None:
        row = self._selected_row()
        if row is not None:
            self._open_dir(Path(str(row.get("report_dir") or "")))

    def _delete_current(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        run_id = str(row.get("run_id") or "")
        if run_id and run_id == self.active_run_id:
            QMessageBox.warning(
                self, "Không thể xoá",
                "Phiên %s đang chạy — dừng phiên trước khi xoá." % run_id,
            )
            return
        answer = QMessageBox.question(
            self, "Xoá phiên test",
            "Xoá VĨNH VIỄN phiên này?\n\n"
            "%s\n%s\n\n"
            "Toàn bộ CSV thô, báo cáo và meta.json sẽ bị xoá — không khôi phục được."
            % (run_id, row.get("size_label") or ""),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if not delete_run_dir(Path(str(row.get("run_dir") or ""))):
            QMessageBox.warning(self, "Không xoá được",
                                "Không xoá được thư mục phiên (file đang mở?).")
            return
        self.refresh()
