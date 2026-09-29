"""Tab "Thiết bị" — quản lý danh sách gateway cần theo dõi.

Gateway UID ở đây = `gateway_id` trong payload MQTT = `device_uuid` của firmware
(`String(ESP.getEfuseMac(), HEX).toUpperCase()`). Xem docs §5.

Tab này KHÔNG tự ghi file: mọi thay đổi được đẩy lên `main_window` qua signal
`devices_changed` để nơi đó lưu `devices.json` một lần duy nhất — tránh 2 nguồn
ghi cùng lúc.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from core.config import GatewayDevice, GATEWAY_UID_RE, is_valid_ipv4, normalize_device_uid
from core.uid import gateway_uid_variants

from . import theme
from .table_model import DictTableModel, fmt_int

COLUMNS = ["enabled", "uid", "name", "eth_ip", "ping_enabled", "ping_rate", "notes"]
HEADERS = [
    "Bật", "Gateway UID", "Tên gợi nhớ", "Ethernet IP", "Ping",
    "Tỉ lệ ping (%)", "Ghi chú",
]


class DeviceDialog(QDialog):
    """Hộp thoại thêm/sửa gateway, có validate ngay khi gõ."""

    def __init__(self, device: Optional[GatewayDevice] = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sửa gateway" if device else "Thêm gateway")
        self.setMinimumWidth(520)
        self._device = device

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        self.ed_uid = QLineEdit(device.uid if device else "")
        self.ed_uid.setPlaceholderText("Ví dụ 4F9199E139C (hex 6-16 ký tự, in hoa)")
        self.ed_name = QLineEdit(device.name if device else "")
        self.ed_name.setPlaceholderText("Ví dụ GW-Tầng2-Khu A")
        self.ed_ip = QLineEdit(device.eth_ip if device else "")
        self.ed_ip.setPlaceholderText("Ví dụ 192.168.1.101")
        self.chk_enabled = QCheckBox("Đưa gateway này vào phiên test")
        self.chk_enabled.setChecked(device.enabled if device else True)
        self.chk_ping = QCheckBox("Ping định kỳ để phát hiện mất kết nối")
        self.chk_ping.setChecked(device.ping_enabled if device else True)
        self.ed_notes = QLineEdit(device.notes if device else "")
        self.ed_notes.setPlaceholderText("Vị trí, người phụ trách, …")

        form.addRow("Gateway UID *", self.ed_uid)
        form.addRow("Tên gợi nhớ", self.ed_name)
        form.addRow("Ethernet IP", self.ed_ip)
        form.addRow("", self.chk_enabled)
        form.addRow("", self.chk_ping)
        form.addRow("Ghi chú", self.ed_notes)
        layout.addLayout(form)

        self.lbl_hint = QLabel(
            "Gateway UID = gateway_id trong payload MQTT = device_uuid của firmware.\n"
            "Lấy từ nhật ký khởi động gateway ([INFO] DEVICE INFO → UUID) hoặc từ "
            "bản tin MQTT đã nhận (cột gateway_uid trong CSV)."
        )
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_DIM)
        layout.addWidget(self.lbl_hint)

        self.lbl_error = QLabel("")
        self.lbl_error.setStyleSheet("color: %s;" % theme.COLOR_CRIT)
        self.lbl_error.setWordWrap(True)
        layout.addWidget(self.lbl_error)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.ed_uid.textChanged.connect(self._validate_live)
        self.ed_ip.textChanged.connect(self._validate_live)
        self.chk_ping.toggled.connect(self._validate_live)
        self._validate_live()

    # ------------------------------------------------------------------ build
    def device(self) -> GatewayDevice:
        base = self._device or GatewayDevice(uid="")
        return GatewayDevice(
            uid=normalize_device_uid(self.ed_uid.text()),
            name=self.ed_name.text().strip(),
            eth_ip=self.ed_ip.text().strip(),
            enabled=self.chk_enabled.isChecked(),
            ping_enabled=self.chk_ping.isChecked(),
            notes=self.ed_notes.text().strip(),
            id=base.id,
        )

    # -------------------------------------------------------------- validate
    def _errors(self) -> List[str]:
        errors: List[str] = []
        uid = normalize_device_uid(self.ed_uid.text())
        if not uid:
            errors.append("Gateway UID không được để trống.")
        elif not GATEWAY_UID_RE.match(uid):
            errors.append("Gateway UID phải là hex 6-16 ký tự (0-9, A-F).")
        ip = self.ed_ip.text().strip()
        if ip and not _valid_ip(ip):
            errors.append("Ethernet IP không hợp lệ (ví dụ 192.168.1.101).")
        if self.chk_ping.isChecked() and not ip:
            errors.append("Bật ping thì bắt buộc nhập Ethernet IP.")
        return errors

    def _validate_live(self) -> None:
        errors = self._errors()
        self.lbl_error.setText("\n".join(errors))
        box = self.findChild(QDialogButtonBox)
        if box is not None:
            box.button(QDialogButtonBox.Ok).setEnabled(not errors)

    def _on_accept(self) -> None:
        errors = self._errors()
        if errors:
            self.lbl_error.setText("\n".join(errors))
            return
        self.accept()


def _valid_ip(text: str) -> bool:
    if is_valid_ipv4(text):
        return True
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def _row_rate(row: Optional[Dict[str, Any]]) -> Any:
    """Tỉ lệ ping (%) của một dòng `ping_summary`, "" nếu chưa có dữ liệu."""
    if not row:
        return ""
    for key in ("success_rate_percent", "success_rate"):
        value = row.get(key)
        if value not in (None, ""):
            return value
    return ""


def _summary_index(source: Any) -> Dict[str, Dict[str, Any]]:
    """Chuẩn hoá mọi nguồn số liệu ping về {biến thể gateway UID → dòng}.

    Nhận: `None`, `PingTracker` (phiên đang chạy), list[dòng dict] (vừa đọc CSV),
    dict đã index sẵn, hoặc `Path`/`str` trỏ tới `ping_summary.csv`.
    """
    from analytics.ping_stats import (
        PingTracker,
        ping_summary_index,
        read_ping_summary_file,
    )

    if source is None:
        return {}
    if isinstance(source, PingTracker):
        return source.rows_by_uid()
    if isinstance(source, dict):
        return source
    if isinstance(source, (str, Path)):
        return ping_summary_index(rows=read_ping_summary_file(Path(source)))
    return ping_summary_index(rows=source)


def _ms(value: Any) -> str:
    """`3.2` → `"3.2"`; None/"" → `"-"` (dùng trong tooltip)."""
    if value in (None, ""):
        return "-"
    try:
        return "%.1f" % float(value)
    except (TypeError, ValueError):
        return str(value)


def _ping_tooltip(row: Dict[str, Any]) -> str:
    """Chi tiết tỉ lệ ping của 1 gateway (ô hiển thị chỉ đủ chỗ cho con số)."""
    rate = row.get("ping_rate")
    if rate in (None, ""):
        return (
            "%s — chưa có dữ liệu ping.\n"
            "Số liệu có sau khi phiên test chạy (bảng dựa trên reports/ping_summary.csv)."
            % (row.get("name") or row.get("uid") or "Gateway")
        )
    return (
        "%s: %s\n%s / %s ping thành công • %s lần thất bại\n"
        "RTT trung bình %s ms • RTT lớn nhất %s ms\n"
        "Sự kiện mất kết nối: %s"
        % (
            row.get("name") or row.get("uid") or "Gateway",
            theme.pct(rate, 1),
            theme.human_count(row.get("ping_success", 0)),
            theme.human_count(row.get("ping_total", 0)),
            theme.human_count(row.get("ping_failed", 0)),
            _ms(row.get("latency_avg_ms")),
            _ms(row.get("latency_max_ms")),
            theme.human_count(row.get("downtime_events", 0)),
        )
    )


class DevicesTab(QWidget):
    """Bảng gateway + nút Thêm/Sửa/Xoá/Bật-tắt."""

    devices_changed = pyqtSignal(list)   # List[GatewayDevice]

    def __init__(self, devices: List[GatewayDevice], parent=None) -> None:
        super().__init__(parent)
        self._devices: List[GatewayDevice] = list(devices)
        #: {biến thể gateway UID → dòng ping_summary} — cập nhật qua
        #: `set_ping_stats()`. Rỗng = chưa có số liệu (cột hiện "-").
        self._ping_rows: Dict[str, Dict[str, Any]] = {}
        self.model = DictTableModel(
            COLUMNS,
            HEADERS,
            max_rows=1000,
            formatters={
                "enabled": lambda v: "✔" if str(v).strip().lower() in ("1", "true") else "",
                "ping_enabled": lambda v: "✔" if str(v).strip().lower() in ("1", "true") else "",
                # Chưa ping lần nào → "-", KHÔNG phải "0.0 %" (0 % là mất hết).
                "ping_rate": lambda v: theme.pct(v, 1) if v not in (None, "") else "-",
            },
            align_right=("ping_rate",),
            row_tooltips={"ping_rate": _ping_tooltip},
        )
        self._build()
        self.reload()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self.btn_add = QPushButton("➕  Thêm gateway")
        self.btn_add.clicked.connect(self._on_add)
        self.btn_edit = QPushButton("✎  Sửa")
        self.btn_edit.clicked.connect(self._on_edit)
        self.btn_delete = QPushButton("🗑  Xoá")
        self.btn_delete.setObjectName("danger")
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_toggle = QPushButton("⏻  Bật/Tắt")
        self.btn_toggle.clicked.connect(self._on_toggle)
        toolbar.addWidget(self.btn_add)
        toolbar.addWidget(self.btn_edit)
        toolbar.addWidget(self.btn_delete)
        toolbar.addWidget(self.btn_toggle)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # Không bật sọc xen kẽ: nền đè của stylesheet làm sọc thành khó đọc.
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.doubleClicked.connect(lambda _: self._on_edit())
        layout.addWidget(self.table, 1)

        self.lbl_info = QLabel("")
        self.lbl_info.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_DIM)
        self.lbl_info.setWordWrap(True)
        layout.addWidget(self.lbl_info)
        self._update_info()

    # ------------------------------------------------------------------ data
    def devices(self) -> List[GatewayDevice]:
        return list(self._devices)

    def set_devices(self, devices: List[GatewayDevice]) -> None:
        self._devices = list(devices)
        self.reload()

    # ------------------------------------------------------------- ping rate
    def set_ping_stats(self, source: Any) -> None:
        """Nạp tỉ lệ ping cho TỪNG gateway.

        `source` nhận `PingTracker` (phiên đang chạy), list/dict dòng
        `ping_summary`, hoặc `Path` tới `ping_summary.csv` (phiên đã dừng).
        `None` = xoá số liệu cũ (hiện "-").
        """
        index = _summary_index(source)
        if index == self._ping_rows:
            return
        self._ping_rows = index
        self._repaint_ping_column()

    def clear_ping_stats(self) -> None:
        """Xoá số liệu ping (dùng khi bắt đầu phiên mới để không lẫn phiên cũ)."""
        self.set_ping_stats(None)

    def ping_rate_for(self, uid: str) -> Any:
        """Tỉ lệ ping (%) của 1 gateway theo MỌI cách viết UID; "" nếu chưa có."""
        for variant in gateway_uid_variants(uid):
            row = self._ping_rows.get(variant)
            if row is not None:
                return _row_rate(row)
        return ""

    def _repaint_ping_column(self) -> None:
        """Cập nhật lại các cột số liệu ping mà KHÔNG reset bảng (giữ scroll/selection).

        `reset_rows()` sẽ làm mất vị trí cuộn của người dùng nên chỉ dùng khi số
        lượng gateway thay đổi; ở đây số dòng không đổi nên cập nhật từng dòng.
        """
        if "ping_rate" not in self.model.columns:
            return
        for row_index, device in enumerate(self._devices):
            row = self._row_for(device)
            self.model.update_row(row_index, row)
        self._update_info()

    def reload(self) -> None:
        rows = []
        for device in self._devices:
            rows.append(self._row_for(device))
        self.model.reset_rows(rows)
        self._update_info()

    def _row_for(self, device: GatewayDevice) -> Dict[str, Any]:
        """Một dòng bảng: thông tin cấu hình + số liệu ping tra theo biến thể UID."""
        summary = None
        for variant in gateway_uid_variants(device.uid):
            summary = self._ping_rows.get(variant)
            if summary is not None:
                break
        return {
            "enabled": device.enabled,
            "uid": device.uid,
            "name": device.name,
            "eth_ip": device.eth_ip,
            "ping_enabled": device.ping_enabled,
            "ping_rate": _row_rate(summary),
            "ping_total": (summary or {}).get("ping_total", ""),
            "ping_success": (summary or {}).get("ping_success", ""),
            "ping_failed": (summary or {}).get("ping_failed", ""),
            "latency_avg_ms": (summary or {}).get("latency_avg_ms", ""),
            "latency_max_ms": (summary or {}).get("latency_max_ms", ""),
            "downtime_events": (summary or {}).get("downtime_events", ""),
            "notes": device.notes,
        }

    def _update_info(self) -> None:
        enabled = [d for d in self._devices if d.enabled]
        ping = [d for d in enabled if d.ping_enabled and d.eth_ip]
        text = (
            "Tổng %d gateway • đang bật %d • sẽ ping %d. "
            "Cảnh báo: gateway không nằm trong danh sách vẫn được GHI vào CSV "
            "nhưng sẽ bị gắn cờ UNKNOWN_GATEWAY."
            % (len(self._devices), len(enabled), len(ping))
        )
        if self._ping_rows:
            with_data = sum(
                1 for d in self._devices if self.ping_rate_for(d.uid) != ""
            )
            text += (
                " Cột “Tỉ lệ ping” dựa trên reports/ping_summary.csv — "
                "có số liệu cho %d/%d gateway (mới nhất: phiên đang chạy, "
                "nếu chưa chạy thì lấy phiên gần nhất)."
                % (with_data, len(self._devices))
            )
        else:
            text += (
                " Cột “Tỉ lệ ping” sẽ có số liệu sau khi phiên test chạy "
                "(đọc từ reports/ping_summary.csv)."
            )
        self.lbl_info.setText(text)

    def _selected_index(self) -> int:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return -1
        index = rows[0].row()
        return index if 0 <= index < len(self._devices) else -1

    def _commit(self, message: str) -> None:
        self.model.reset_rows([self._row_for(d) for d in self._devices])
        self._update_info()
        self.devices_changed.emit(list(self._devices))

    def _on_add(self) -> None:
        dialog = DeviceDialog(None, self)
        if dialog.exec_() != QDialog.Accepted:
            return
        device = dialog.device()
        if any(d.uid == device.uid for d in self._devices):
            QMessageBox.warning(self, "Trùng UID",
                                "Gateway UID %s đã có trong danh sách." % device.uid)
            return
        self._devices.append(device)
        self._commit("Đã thêm %s" % device.uid)

    def _on_edit(self) -> None:
        index = self._selected_index()
        if index < 0:
            QMessageBox.information(self, "Chưa chọn", "Hãy chọn một gateway trong bảng.")
            return
        current = self._devices[index]
        dialog = DeviceDialog(current, self)
        if dialog.exec_() != QDialog.Accepted:
            return
        updated = dialog.device()
        if any(d.uid == updated.uid and d.id != current.id for d in self._devices):
            QMessageBox.warning(self, "Trùng UID",
                                "Gateway UID %s đã có trong danh sách." % updated.uid)
            return
        self._devices[index] = updated
        self._commit("Đã cập nhật %s" % updated.uid)

    def _on_delete(self) -> None:
        index = self._selected_index()
        if index < 0:
            QMessageBox.information(self, "Chưa chọn", "Hãy chọn một gateway trong bảng.")
            return
        device = self._devices[index]
        answer = QMessageBox.question(
            self,
            "Xoá gateway",
            "Xoá %s khỏi danh sách theo dõi?\n\n"
            "Dữ liệu CSV đã ghi vẫn được giữ nguyên." % device.display_name(),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._devices.pop(index)
        self._commit("Đã xoá %s" % device.uid)

    def _on_toggle(self) -> None:
        index = self._selected_index()
        if index < 0:
            QMessageBox.information(self, "Chưa chọn", "Hãy chọn một gateway trong bảng.")
            return
        self._devices[index].enabled = not self._devices[index].enabled
        self._commit("Đã đổi trạng thái %s" % self._devices[index].uid)
