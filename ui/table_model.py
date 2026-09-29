"""Model bảng dùng chung: dict → QAbstractTableModel, giới hạn số dòng.

Dùng cho mọi bảng trong GUI (live message, node summary, alert, search, ...).
Điểm quan trọng:

  - **Giới hạn dòng** (`max_rows`): bảng live thêm liên tục trong 3-4 ngày, nếu
    không cắt bớt thì RAM tăng vô hạn và Qt bắt đầu lag. Dòng cũ bị bỏ khỏi GUI
    (dữ liệu đầy đủ vẫn nằm trong CSV).
  - **Cập nhật theo lô**: `append_many()` rồi `flush()` một lần cho mỗi chu kỳ
    refresh → ít `dataChanged` hơn nhiều so với thêm từng dòng.
  - **Tô màu theo cột**: `color_column` + bảng màu (theme.PACKET_COLORS, ...).
"""

from __future__ import annotations

from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt5.QtGui import QColor

from . import theme


class DictTableModel(QAbstractTableModel):
    """Bảng đọc-only với dữ liệu là list[dict]."""

    def __init__(
        self,
        columns: Sequence[str],
        headers: Optional[Sequence[str]] = None,
        max_rows: int = 2000,
        color_column: str = "",
        color_map: Optional[Dict[str, str]] = None,
        formatters: Optional[Dict[str, Callable[[Any], str]]] = None,
        align_right: Sequence[str] = (),
        row_tooltips: Optional[Dict[str, Callable[[Dict[str, Any]], str]]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.columns = list(columns)
        self.headers = list(headers) if headers else [c.replace("_", " ") for c in columns]
        self.max_rows = max(1, int(max_rows))
        self.color_column = color_column
        self.color_map = dict(color_map or {})
        self.formatters = dict(formatters or {})
        self.align_right = set(align_right)
        #: Tooltip dựng từ CẢ DÒNG (không chỉ giá trị ô) — dùng khi ô hiển thị gọn
        #: nhưng người dùng cần xem chi tiết (ví dụ "Tỉ lệ ping (%)").
        self.row_tooltips = dict(row_tooltips or {})
        self._rows: Deque[Dict[str, Any]] = deque(maxlen=self.max_rows)
        # Con trỏ số dòng đã bỏ khỏi đầu (để biết bảng đang cắt bớt bao nhiêu)
        self._truncated = 0

    # ---------------------------------------------------------------- Qt API
    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return self.headers[section] if 0 <= section < len(self.headers) else ""
        if role == Qt.ToolTipRole:
            return self.columns[section] if 0 <= section < len(self.columns) else ""
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        try:
            row = self._rows[index.row()]
        except IndexError:
            return None
        column = self.columns[index.column()]
        value = row.get(column, "")

        if role == Qt.DisplayRole:
            formatter = self.formatters.get(column)
            if formatter is not None:
                try:
                    return formatter(value)
                except Exception:  # noqa: BLE001 - formatter lỗi không được làm chết GUI
                    return str(value)
            return "" if value is None else str(value)

        if role == Qt.TextAlignmentRole and column in self.align_right:
            return int(theme.right_align())

        if role == Qt.ForegroundRole and self.color_column:
            key = str(row.get(self.color_column, "") or "").upper()
            color = self.color_map.get(key)
            if color:
                return theme.brush(color)

        if role == Qt.ToolTipRole:
            tooltip = self.row_tooltips.get(column)
            if tooltip is not None:
                try:
                    text = tooltip(row)
                except Exception:  # noqa: BLE001 - tooltip lỗi không được làm chết GUI
                    text = ""
                if text:
                    return text
            return str(row.get(column, ""))

        if role == Qt.BackgroundRole:
            level = str(row.get("level", "") or "").upper()
            if level == "CRITICAL":
                return theme.brush("#3a1c1c")
            if level == "WARNING":
                return theme.brush("#3a331c")
        return None

    # --------------------------------------------------------------- dữ liệu
    def append(self, row: Dict[str, Any]) -> None:
        if len(self._rows) == self.max_rows:
            self._truncated += 1
        self._rows.append(row)

    def append_many(self, rows: Sequence[Dict[str, Any]]) -> int:
        """Thêm nhiều dòng, KHÔNG thông báo view (gọi `flush()` sau)."""
        count = 0
        for row in rows:
            if len(self._rows) == self.max_rows:
                self._truncated += 1
            self._rows.append(row)
            count += 1
        return count

    def flush(self) -> None:
        """Phát tín hiệu để view vẽ lại toàn bộ (dùng sau `append_many`)."""
        if not self._rows:
            return
        top = self.index(0, 0)
        bottom = self.index(len(self._rows) - 1, max(0, len(self.columns) - 1))
        self.dataChanged.emit(top, bottom)

    def reset_rows(self, rows: Optional[Sequence[Dict[str, Any]]] = None) -> None:
        self.beginResetModel()
        self._rows = deque(rows or [], maxlen=self.max_rows)
        self._truncated = 0
        self.endResetModel()

    def update_row(self, row_index: int, values: Dict[str, Any]) -> None:
        if not 0 <= row_index < len(self._rows):
            return
        self._rows[row_index].update(values)
        left = self.index(row_index, 0)
        right = self.index(row_index, max(0, len(self.columns) - 1))
        self.dataChanged.emit(left, right)

    def rows(self) -> List[Dict[str, Any]]:
        return list(self._rows)

    def row_at(self, row_index: int) -> Optional[Dict[str, Any]]:
        try:
            return self._rows[row_index]
        except IndexError:
            return None

    def truncated_count(self) -> int:
        return self._truncated

    def set_max_rows(self, max_rows: int) -> None:
        max_rows = max(1, int(max_rows))
        if max_rows == self.max_rows:
            return
        dropped = max(0, len(self._rows) - max_rows)
        self.beginResetModel()
        self.max_rows = max_rows
        self._rows = deque(list(self._rows)[dropped:], maxlen=max_rows)
        self._truncated += dropped
        self.endResetModel()


# ---------------------------------------------------------------------------
# Định dạng dùng chung cho bảng
# ---------------------------------------------------------------------------
def fmt_int(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return "{:,}".format(int(float(value))).replace(",", ".")
    except (TypeError, ValueError):
        return str(value)


def fmt_float(value: Any, digits: int = 2) -> str:
    if value in (None, ""):
        return ""
    try:
        return ("%%.%df" % digits) % float(value)
    except (TypeError, ValueError):
        return str(value)


def fmt_bool(value: Any) -> str:
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y"):
        return "Có"
    if text in ("0", "false", "no", "n", ""):
        return "Không"
    return str(value)


def fmt_time(value: Any) -> str:
    return theme.short_time(value)


def fmt_age(value: Any) -> str:
    return theme.age_text(value)


def fmt_sensor_values(value: Any, max_items: int = 4) -> str:
    """`1|2|3|4|5` → `1, 2, 3, 4 (+1)` cho bảng live (tránh 60 cột số)."""
    text = str(value or "")
    if not text:
        return ""
    parts = text.split("|")
    if len(parts) <= max_items:
        return ", ".join(parts)
    return "%s (+%d)" % (", ".join(parts[:max_items]), len(parts) - max_items)
