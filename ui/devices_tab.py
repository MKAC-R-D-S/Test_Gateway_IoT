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
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
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
from core.device_import import ImportError_, ImportPreview, build_preview
from core.gateway_directory import load_gateway_catalog
from core.uid import gateway_uid_variants

from . import theme
from .table_model import CheckableDictTableModel, DictTableModel, fmt_int

COLUMNS = ["enabled", "uid", "name", "eth_ip", "ping_enabled", "ping_rate", "notes"]
HEADERS = [
    "Bật", "Gateway UID", "Tên gợi nhớ", "Ethernet IP", "Ping",
    "Tỉ lệ ping (%)", "Ghi chú",
]

#: Bề rộng cột ô tick ở hộp thoại nhập CSV — vừa đủ một ô vuông, không hơn.
_CHECK_COL_WIDTH = 28


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


class ImportDevicesDialog(QDialog):
    """Xem trước kết quả nhập danh sách thiết bị từ CSV, cho sửa lại ghép cột.

    Vì sao phải có hộp thoại xem trước chứ không nhập thẳng: tiêu đề file thi
    công thật đã **mất dấu ngay trong file** (xem `core/device_import.py`), nên
    không thể dò chắc chắn đâu là cột TÊN — cả `Mã Gateway`, `SN`,
    `Mã qu?n lý GDO` đều là chuỗi tự do. Bảng xem trước là lưới an toàn: nếu dò
    sai, người dùng thấy ngay (`GW-90` thay vì `GDO-…`) và sửa bằng combo.

    Mỗi dòng `Thêm mới` có **ô tick** để chọn con nào đưa vào danh sách, kèm nút
    *Chọn hết* / *Bỏ chọn hết*. Báo cáo thi công thường dài hơn số gateway cần đo
    trong một phiên, nên nhập hết cả 40 rồi phải đi tắt từng con là việc thừa.
    Dòng `Đã có` và dòng `Lỗi` **không tick được** — nhập cũng vô ích.
    """

    def __init__(
        self,
        path: object,
        existing: List[GatewayDevice],
        catalog: Dict[str, str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Nhập danh sách thiết bị từ CSV")
        self.setMinimumSize(880, 560)
        self._path = path
        self._existing = list(existing)
        self._catalog = dict(catalog)
        self._preview = build_preview(path, self._existing, catalog=self._catalog)
        self._devices: List[GatewayDevice] = []
        #: Trạng thái tick, khoá theo **số dòng trong file** (`line_no`) chứ không
        #: theo chỉ số bảng: `_recompute()` dựng lại bảng nên chỉ số đổi, còn số
        #: dòng thì không. Dòng nào không có trong đây ⇒ chưa ai đụng vào.
        self._ticked: Dict[int, bool] = {}
        #: Cờ chặn vòng lặp: `_push_checks()` phát `dataChanged`, mà
        #: `_on_check_changed` lại nghe chính tín hiệu đó.
        self._syncing = False

        layout = QVBoxLayout(self)

        header = QLabel(
            "File: <b>%s</b><br>Bảng mã đọc được: <b>%s</b> · dấu phân cách <b>%r</b> · "
            "<b>%d</b> dòng dữ liệu"
            % (Path(str(path)).name, self._preview.encoding,
               self._preview.delimiter, len(self._preview.rows))
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        # --- ghép cột (người dùng sửa được nếu dò sai) ---------------------
        mapping = QHBoxLayout()
        mapping.addWidget(QLabel("Cột Tên:"))
        self.cmb_name = _ColumnCombo(self._preview, "name")
        mapping.addWidget(self.cmb_name)
        mapping.addWidget(QLabel("Ethernet IP:"))
        self.cmb_ip = _ColumnCombo(self._preview, "ip")
        mapping.addWidget(self.cmb_ip)
        mapping.addWidget(QLabel("Gateway UID / SN:"))
        self.cmb_uid = _ColumnCombo(self._preview, "uid")
        mapping.addWidget(self.cmb_uid)
        mapping.addStretch(1)
        layout.addLayout(mapping)

        self.lbl_hint = QLabel(
            "Tool tự dò cột theo tiêu đề và theo giá trị (cột toàn IPv4 hợp lệ = cột IP). "
            "Nếu dò sai, chọn lại cột ở trên — bảng dưới cập nhật ngay.<br>"
            "Tick ở cột đầu để chọn gateway đưa vào danh sách; chỉ dòng "
            "<b>Thêm mới</b> mới tick được."
        )
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_DIM)
        layout.addWidget(self.lbl_hint)

        # --- nút chọn nhanh -------------------------------------------------
        pick_bar = QHBoxLayout()
        self.btn_check_all = QPushButton("Chọn hết")
        self.btn_uncheck_all = QPushButton("Bỏ chọn hết")
        for btn in (self.btn_check_all, self.btn_uncheck_all):
            btn.setAutoDefault(False)      # Enter không được kích hoạt nút này
            pick_bar.addWidget(btn)
        pick_bar.addStretch(1)
        layout.addLayout(pick_bar)

        # --- bảng xem trước -------------------------------------------------
        # Cột đầu rỗng: chỗ của ô tick (`CheckableDictTableModel` vẽ vào cột 0).
        self.model = CheckableDictTableModel(
            ["pick", "uid", "name", "eth_ip", "status", "reason"],
            ["", "Gateway UID", "Tên gợi nhớ", "Ethernet IP", "Trạng thái", "Ghi chú"],
            max_rows=5000,
            color_column="status",
            color_map={
                # `DictTableModel` viết HOA giá trị ô trước khi tra bảng màu.
                "NEW": theme.COLOR_OK,
                "EXISTS": theme.COLOR_DIM,
                "ERROR": theme.COLOR_CRIT,
            },
            formatters={"status": _status_label},
            checkable=lambda row: row.get("status") == "new",
        )
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, _CHECK_COL_WIDTH)
        layout.addWidget(self.table, 1)

        self.lbl_summary = QLabel("")
        self.lbl_summary.setWordWrap(True)
        layout.addWidget(self.lbl_summary)

        for warning in self._preview.warnings:
            label = QLabel("⚠  %s" % warning)
            label.setWordWrap(True)
            label.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_WARN)
            layout.addWidget(label)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.Ok).setText("Nhập vào danh sách")
        layout.addWidget(self.buttons)

        for combo in (self.cmb_name, self.cmb_ip, self.cmb_uid):
            combo.currentIndexChanged.connect(self._recompute)
        self.btn_check_all.clicked.connect(lambda: self._set_all(True))
        self.btn_uncheck_all.clicked.connect(lambda: self._set_all(False))
        self.model.dataChanged.connect(self._on_check_changed)
        self._recompute()

    # ------------------------------------------------------------------ data
    def devices(self) -> List[GatewayDevice]:
        """Thiết bị sẽ được thêm — CHỈ những dòng người dùng đã tick."""
        return list(self._devices)

    # ------------------------------------------------------------ chọn dòng
    def _set_all(self, checked: bool) -> None:
        """Tick/bỏ tick MỌI dòng `Thêm mới` (nút *Chọn hết* / *Bỏ chọn hết*).

        Ghi thẳng vào `_ticked` (đủ mọi dòng) rồi mới đẩy xuống bảng — nếu chỉ
        sửa những dòng đang hiện, file dài hơn `max_rows` sẽ còn sót dòng đã bị
        cắt khỏi bảng mà vẫn tick, và dòng đếm dưới bảng nói dối.
        """
        for row in self._preview.rows:
            if row.status == "new" and row.device is not None:
                self._ticked[row.line_no] = bool(checked)
        self._push_checks()
        self._collect()

    def _push_checks(self) -> None:
        """Đẩy trạng thái `_ticked` xuống các dòng ĐANG HIỆN trên bảng."""
        self._syncing = True
        try:
            for index, item in enumerate(self.model.rows()):
                line_no = item.get("line_no")
                if line_no is None:
                    continue
                want = bool(self._ticked.get(int(line_no), False))
                if bool(item.get(CheckableDictTableModel.CHECK_KEY)) == want:
                    continue
                item[CheckableDictTableModel.CHECK_KEY] = want
                cell = self.model.index(index, 0)
                self.model.dataChanged.emit(cell, cell)
        finally:
            self._syncing = False

    def _on_check_changed(self, _top, _bottom, _roles=None) -> None:
        """Người dùng vừa tick/bỏ tick một dòng → chép về `_ticked` rồi tính lại."""
        if self._syncing:
            return          # thay đổi do `_push_checks()` gây ra, không phải người dùng
        for item in self.model.rows():
            line_no = item.get("line_no")
            if line_no is not None:
                self._ticked[int(line_no)] = bool(
                    item.get(CheckableDictTableModel.CHECK_KEY)
                )
        self._collect()

    def _collect(self) -> None:
        """Dựng lại danh sách chờ nhập từ `_ticked` (nguồn đủ, không bị cắt bớt)."""
        self._devices = [
            row.device
            for row in self._preview.rows
            if row.status == "new" and row.device is not None
            and self._ticked.get(row.line_no, False)
        ]
        self._update_summary()

    def _update_summary(self) -> None:
        # Đếm từ `_ticked`/`_preview` chứ không từ bảng: bảng có thể đã cắt bớt
        # dòng khi file dài, còn `_ticked` thì luôn đủ.
        self.lbl_summary.setText(
            "Sẽ thêm <b>%d</b>/%d gateway đang tick · bỏ qua <b>%d</b> đã có · "
            "<b>%d</b> dòng lỗi (không nhập)"
            % (len(self._devices), self._preview.count("new"),
               self._preview.count("exists"), self._preview.count("error"))
        )
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(bool(self._devices))

    def _recompute(self) -> None:
        """Dò lại theo cột người dùng chọn rồi làm mới bảng xem trước."""
        try:
            preview = build_preview(
                self._path,
                self._existing,
                columns={
                    "name": self.cmb_name.column_index(),
                    "ip": self.cmb_ip.column_index(),
                    "uid": self.cmb_uid.column_index(),
                },
                catalog=self._catalog,
            )
        except ImportError_ as exc:
            self.lbl_summary.setText("Không đọc được file: %s" % exc)
            return
        self._preview = preview
        # Giữ lựa chọn người dùng đã đặt cho những dòng còn nhận diện được; dòng
        # chưa ai đụng thì mặc định tick nếu là "Thêm mới". Dòng "Đã có"/"Lỗi"
        # luôn ghi False — chúng không tick được nên không cần hỏi ý người dùng.
        self._ticked = {
            row.line_no: (
                self._ticked.get(row.line_no, True)
                if row.status == "new" else False
            )
            for row in preview.rows
        }
        self.model.reset_rows([
            {
                # Cột 0 là chỗ vẽ ô tick; không có chữ nào để hiện.
                "pick": "",
                # Giữ số dòng trong file để `_sync_ticks` tra ngược lại được.
                "line_no": row.line_no,
                "uid": row.uid or "—",
                "name": row.name,
                "eth_ip": row.eth_ip,
                "status": row.status,
                "reason": row.reason,
                CheckableDictTableModel.CHECK_KEY: self._ticked[row.line_no],
            }
            for row in preview.rows
        ])
        self._collect()


class _ColumnCombo(QComboBox):
    """Combo chọn cột, có mục "(không dùng)" trả về chỉ số -1."""

    def __init__(self, preview: "ImportPreview", role: str, parent=None) -> None:
        super().__init__(parent)
        self._role = role
        self.addItem("(không dùng)", -1)
        for index, title in enumerate(preview.header):
            self.addItem("%d. %s" % (index + 1, title or "(trống)"), index)
        self.setColumn(preview.columns.get(role))

    def setColumn(self, index: Optional[int]) -> None:
        target = -1 if index is None else index
        position = self.findData(target)
        self.setCurrentIndex(position if position >= 0 else 0)

    def column_index(self) -> int:
        data = self.currentData()
        return -1 if data is None else int(data)


def _status_label(value: Any) -> str:
    return {"new": "Thêm mới", "exists": "Đã có", "error": "Lỗi"}.get(
        str(value), str(value)
    )


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
        self.btn_import = QPushButton("📥  Nhập từ CSV")
        self.btn_import.setToolTip(
            "Nhập danh sách gateway từ file CSV (bản thi công mạng, hoặc danh mục "
            "nhà máy). Đọc được cả UTF-8 lẫn ANSI."
        )
        self.btn_import.clicked.connect(self._on_import)
        toolbar.addWidget(self.btn_add)
        toolbar.addWidget(self.btn_edit)
        toolbar.addWidget(self.btn_delete)
        toolbar.addWidget(self.btn_toggle)
        toolbar.addWidget(self.btn_import)
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

    def _on_import(self) -> None:
        """Nhập danh sách gateway từ file CSV (bản thi công mạng / danh mục)."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Nhập danh sách thiết bị từ CSV", str(Path.home()), "CSV (*.csv)"
        )
        if not path:
            return
        try:
            dialog = ImportDevicesDialog(
                path, self._devices, load_gateway_catalog(), self
            )
        except ImportError_ as exc:
            QMessageBox.warning(self, "Không đọc được file", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Không đọc được file",
                                "%s: %s" % (type(exc).__name__, exc))
            return

        if dialog.exec_() != QDialog.Accepted:
            return

        added = dialog.devices()
        if not added:
            QMessageBox.information(
                self, "Không có gì để nhập",
                "Không có gateway mới nào để thêm vào danh sách.",
            )
            return

        # Chốt chặn cuối: không để lọt UID trùng dù hộp thoại đã lọc.
        known: Dict[str, GatewayDevice] = {}
        for device in self._devices:
            for variant in gateway_uid_variants(device.uid):
                known.setdefault(variant, device)
        fresh = [
            device for device in added
            if not (known.keys() & gateway_uid_variants(device.uid))
        ]
        # Khử trùng ngay trong danh sách vừa nhập (hai biến thể của cùng gateway).
        unique: List[GatewayDevice] = []
        seen_variants: set = set()
        for device in fresh:
            variants = gateway_uid_variants(device.uid)
            if variants & seen_variants:
                continue
            seen_variants |= variants
            unique.append(device)

        if not unique:
            QMessageBox.information(
                self, "Không có gì để nhập",
                "Toàn bộ gateway trong file đã có trong danh sách.",
            )
            return

        self._devices.extend(unique)
        self._commit("Đã nhập %d gateway từ CSV" % len(unique))
        QMessageBox.information(
            self, "Đã nhập",
            "Đã thêm %d gateway vào danh sách theo dõi.\n\n"
            "Danh sách đã được lưu vào config/devices.json." % len(unique),
        )
