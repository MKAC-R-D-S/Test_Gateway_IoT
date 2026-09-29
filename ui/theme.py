"""Bảng màu, style và helper định dạng dùng chung cho mọi tab GUI.

Tách riêng để màu sắc/định dạng nhất quán và dễ đổi một chỗ.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QBrush, QColor, QFont

# ---------------------------------------------------------------------------
# Màu
# ---------------------------------------------------------------------------
COLOR_BG = "#1e1f22"
COLOR_BG_ALT = "#26282c"
# Nền dòng cho bảng: CÙNG màu với nền chung nên bảng phẳng, không sọc.
# Xem ghi chú ở `QTableView` trong STYLESHEET bên dưới.
COLOR_ROW = COLOR_BG
COLOR_TEXT = "#e6e6e6"
COLOR_DIM = "#9aa0a6"
COLOR_OK = "#4caf50"
COLOR_WARN = "#ffb300"
COLOR_CRIT = "#e53935"
COLOR_INFO = "#29b6f6"
COLOR_ACCENT = "#7e57c2"
COLOR_DISABLED = "#5f6368"

# Màu theo packet_type (cột "Loại")
PACKET_COLORS = {
    "DATA": COLOR_OK,
    "DATA_NO_SEQ": COLOR_INFO,
    "INFO": COLOR_INFO,
    "ERROR": COLOR_CRIT,
    "LOG": COLOR_DIM,
    "UNKNOWN": COLOR_DISABLED,
}

# Màu theo seq_status
SEQ_COLORS = {
    "NORMAL": COLOR_OK,
    "FIRST": COLOR_DIM,
    "MISSING": COLOR_WARN,
    "DUPLICATE": COLOR_WARN,
    "OUT_OF_ORDER": COLOR_ACCENT,
    "RESET": COLOR_INFO,
}

# Màu theo mức cảnh báo
LEVEL_COLORS = {
    "INFO": COLOR_INFO,
    "WARNING": COLOR_WARN,
    "CRITICAL": COLOR_CRIT,
}

# Màu theo trạng thái runner
STATE_COLORS = {
    "IDLE": COLOR_DIM,
    "RUNNING": COLOR_OK,
    "PAUSED": COLOR_WARN,
    "STOPPING": COLOR_WARN,
    "STOPPED": COLOR_DIM,
    "ERROR": COLOR_CRIT,
}

# Màu cho kết quả đạt / không đạt (`reports/gateway_result.csv`).
# Khoá phải là chữ HOA vì `DictTableModel` tra màu bằng `str(...).upper()`.
RESULT_COLORS = {
    "ĐẠT": COLOR_OK,
    "KHÔNG ĐẠT": COLOR_CRIT,
    "PASS": COLOR_OK,
    "FAIL": COLOR_CRIT,
    # `N/A` = không áp dụng được (không bật ping, mẫu quá nhỏ, không bật node
    # trọng tâm) — khác hẳn FAIL nên KHÔNG dùng màu đỏ.
    "N/A": COLOR_DIM,
}

STYLESHEET = """
QWidget { background: %(bg)s; color: %(text)s; }
QGroupBox {
    border: 1px solid #3c4043; border-radius: 6px; margin-top: 10px; padding-top: 8px;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: %(dim)s; }
QTabWidget::pane { border: 1px solid #3c4043; }
QTabBar::tab { background: %(alt)s; padding: 6px 14px; border: 1px solid #3c4043; }
QTabBar::tab:selected { background: %(bg)s; border-bottom-color: %(bg)s; }
QHeaderView::section { background: %(alt)s; padding: 4px; border: 0;
    border-right: 1px solid #3c4043; border-bottom: 1px solid #3c4043; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {
    background: %(alt)s; border: 1px solid #3c4043; border-radius: 4px; padding: 3px 6px;
}
QPushButton {
    background: %(alt)s; border: 1px solid #3c4043; border-radius: 4px; padding: 5px 14px;
}
QPushButton:hover { border-color: %(accent)s; }
QPushButton:disabled { color: %(disabled)s; }
QPushButton#primary { background: %(accent)s; color: white; font-weight: bold; }
QPushButton#danger { background: %(crit)s; color: white; }
QCheckBox::indicator, QRadioButton::indicator { width: 14px; height: 14px; }
QStatusBar { background: %(alt)s; }
/* --- Bảng ---------------------------------------------------------------
   KHÔNG dùng sọc xen kẽ (alternating rows) ở tool này.
   Lý do: rule `QWidget { background: ... }` ngay trên đây áp một nền đè lên mọi
   widget, làm Qt tính màu "alternate" sai -> sọc gần như vô hình, và ở một số
   theme còn cho ra hàng nền đen chữ trắng xen hàng nền trắng chữ trắng.
   Nên đặt minh bạch cả `background` LẪN `alternate-background-color` bằng nhau;
   backstop này giữ bảng phẳng kể cả khi palette hệ thống là theme sáng. */
QTableView {
    background: %(row)s;
    alternate-background-color: %(row)s;
    gridline-color: #33363a;
    selection-background-color: #3d5afe;
    selection-color: #ffffff;
}
QTableView::item { padding: 2px 4px; }
QTableView::item:selected { background: #3d5afe; color: #ffffff; }
""" % {
    "bg": COLOR_BG,
    "alt": COLOR_BG_ALT,
    "text": COLOR_TEXT,
    "dim": COLOR_DIM,
    "accent": COLOR_ACCENT,
    "crit": COLOR_CRIT,
    "disabled": COLOR_DISABLED,
    "row": COLOR_ROW,
}


def mono_font(size: int = 9) -> QFont:
    font = QFont("Consolas")
    font.setStyleHint(QFont.Monospace)
    font.setPointSize(size)
    return font


def brush(color: str) -> QBrush:
    return QBrush(QColor(color))


# ---------------------------------------------------------------------------
# Định dạng
# ---------------------------------------------------------------------------
def human_duration(seconds: Optional[float]) -> str:
    """`3d 04:12:30` — phù hợp test dài ngày."""
    if seconds is None:
        return "-"
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return "%dd %02d:%02d:%02d" % (days, hours, minutes, secs)
    return "%02d:%02d:%02d" % (hours, minutes, secs)


def human_size(num_bytes: Optional[float]) -> str:
    size = float(max(0.0, num_bytes or 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return "%.1f %s" % (size, unit)
        size /= 1024
    return "%.1f TB" % size


def human_count(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value if value not in (None, "") else "-")
    return "{:,}".format(number).replace(",", ".")


def pct(value: Any, digits: int = 2) -> str:
    """`99.0` → `"99.0 %"`; None/"" → `"-"`.

    LUÔN kèm ký hiệu `%`: mọi header dùng hàm này đều ghi "(%)", nên con số trần
    không có `%` là dễ đọc sai nhất là khi cạnh cột đếm số (ví dụ "Số GW nghe").

    Giá trị không đổi được sang số (data lỗi) → trả nguyên chuỗi gốc thay vì
    văng lỗi ra GUI.
    """
    if value in (None, ""):
        return "-"
    try:
        # `%.*f` lấy độ chính xác từ tham số: CHỈ 1 conversion nên `%%` phía sau
        # vẫn là ký tự `%` nguyên văn. (Không dùng `"%%.%df %%" % digits` —
        # biểu thức đó lồng 2 lần % và luôn văng ValueError.)
        return "%.*f %%" % (digits, float(value))
    except (TypeError, ValueError):
        return str(value)


def short_time(text: Any) -> str:
    """`2026-09-18T14:25:30.123+07:00` → `14:25:30.123` (gọn cho bảng live)."""
    parsed = parse_stamp(text)
    if parsed is None:
        return str(text or "")
    if parsed.microsecond:
        return parsed.strftime("%H:%M:%S.") + "%03d" % (parsed.microsecond // 1000)
    return parsed.strftime("%H:%M:%S")


def parse_stamp(text: Any) -> Optional[datetime]:
    text = (text or "")
    if not isinstance(text, str) or not text.strip():
        return None
    candidate = text.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(candidate[: len(fmt) + 6], fmt)
        except ValueError:
            continue
    return None


def age_seconds(text: Any) -> Optional[float]:
    """Số giây kể từ `text` tới hiện tại (None nếu không parse được)."""
    stamp = parse_stamp(text)
    if stamp is None:
        return None
    now = datetime.now(stamp.tzinfo) if stamp.tzinfo else datetime.now()
    try:
        return max(0.0, (now - stamp).total_seconds())
    except TypeError:
        return None


def age_text(text: Any) -> str:
    seconds = age_seconds(text)
    if seconds is None:
        return "-"
    if seconds < 60:
        return "%.0fs trước" % seconds
    if seconds < 3600:
        return "%.0f phút trước" % (seconds / 60.0)
    if seconds < 86400:
        return "%.1f giờ trước" % (seconds / 3600.0)
    return "%.1f ngày trước" % (seconds / 86400.0)


def right_align() -> int:
    return Qt.AlignRight | Qt.AlignVCenter


def center_align() -> int:
    return Qt.AlignCenter


def wait_cursor():
    """Con trỏ chờ — dùng cho thao tác có thể mất vài giây (dừng phiên, ghi báo cáo)."""
    from PyQt5.QtCore import Qt as _Qt
    from PyQt5.QtGui import QCursor

    return QCursor(_Qt.WaitCursor)
