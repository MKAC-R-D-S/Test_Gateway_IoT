"""Tab "Tìm kiếm" — đọc lại CSV để tra cứu (CSV là source of truth).

Vì phiên test có thể dài 3-4 ngày với hàng trăm nghìn bản tin, tab này:

  - **không load hết file**: dùng `storage.reader.search_mqtt()` với `limit` để
    chỉ đọc phần cần thiết;
  - **chạy trong QThread** để GUI không đứng khi quét file lớn;
  - **phân trang** (offset/limit) — người dùng có thể xem tiếp mà không phải
    nạp toàn bộ dataset;
  - cho phép chọn **một phiên chạy (run) cụ thể** hoặc toàn bộ dữ liệu.
"""

from __future__ import annotations

import csv
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import QThread, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from core.uid import gateway_name_index
from storage.reader import SearchQuery, SearchResult, list_csv_files, search_mqtt
from storage.session import (
    human_duration,
    human_size,
    list_run_dirs,
    open_session,
    session_gateway_names,
)

from . import theme
from .table_model import DictTableModel, fmt_float, fmt_int, fmt_sensor_values, fmt_time

COLUMNS = [
    "received_at", "gateway_uid", "gateway_name", "receiver_uid",
    "node_uid", "node_format", "packet_type", "seq", "seq_status",
    "previous_seq", "expected_seq", "missing_count", "rssi", "snr",
    "mcu_temp", "mcu_vdd", "sensor_values", "anomaly", "validation_error",
    "topic", "payload_json",
]
HEADERS = [
    "Giờ nhận", "ID gateway (11–12 hex)", "Tên gateway",
    "Receiver", "Node", "Định dạng UID", "Loại", "SEQ", "SEQ status",
    "SEQ trước", "SEQ mong đợi", "Thiếu", "RSSI", "SNR", "Nhiệt", "VDD",
    "Giá trị cảm biến", "Bất thường", "Lỗi validate", "Topic", "JSON gốc",
]


class SearchWorker(QThread):
    """Chạy `search_mqtt` ngoài UI thread."""

    finished_ok = pyqtSignal(object)    # SearchResult
    failed = pyqtSignal(str)

    def __init__(self, directory: Path, query: SearchQuery, parent=None) -> None:
        super().__init__(parent)
        self.directory = Path(directory)
        self.query = query

    def run(self) -> None:
        try:
            result = search_mqtt(self.directory, self.query)
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001 - báo lỗi ra GUI thay vì crash
            self.failed.emit("%s: %s" % (type(exc).__name__, exc))


class SearchTab(QWidget):
    """Form điều kiện + bảng kết quả + phân trang + xuất CSV."""

    def __init__(self, config, data_dir: Path, devices=None, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.data_dir = Path(data_dir)
        # Danh sách thiết bị (có thể cập nhật qua `set_devices`) — dùng để điền
        # lại TÊN gateway cho kết quả tìm kiếm, kể cả CSV của phiên bản cũ.
        self.devices: List[Any] = list(devices or [])
        self.gateway_names: Dict[str, str] = self._gateway_name_index(self.devices)
        self.page_size = int(getattr(config, "search_page_size", 500))
        self.offset = 0
        self.last_result: Optional[SearchResult] = None
        self.worker: Optional[SearchWorker] = None
        self._scanned_dir: Optional[Path] = None
        #: ẢNH CHỤP tên gateway của phiên đang chọn phạm vi (nếu có). Khi tìm
        #: trong MỘT phiên, tên gateway phải là tên LÚC CHẠY — xoá/đổi gateway
        #: trong tab Thiết bị sau đó không được làm đổi kết quả tìm kiếm của
        #: phiên cũ. Xem `storage/session.py::session_gateway_names`.
        self._scope_gateway_names: Dict[str, str] = {}
        self._run_dirs: List[Path] = []

        self.model = DictTableModel(
            COLUMNS,
            HEADERS,
            max_rows=100000,
            color_column="packet_type",
            color_map=theme.PACKET_COLORS,
            formatters={
                "received_at": fmt_time,
                "seq": fmt_int,
                "previous_seq": fmt_int,
                "expected_seq": fmt_int,
                "missing_count": fmt_int,
                "rssi": lambda v: fmt_float(v, 1),
                "snr": lambda v: fmt_float(v, 2),
                "mcu_temp": lambda v: fmt_float(v, 1),
                "mcu_vdd": lambda v: fmt_float(v, 2),
                "sensor_values": fmt_sensor_values,
            },
            align_right=("seq", "previous_seq", "expected_seq", "missing_count",
                         "rssi", "snr", "mcu_temp", "mcu_vdd"),
        )
        self._build()
        self.refresh_runs()

    # -------------------------------------------------------------- devices
    @staticmethod
    def _gateway_name_index(devices) -> Dict[str, str]:
        """Bảng tra {biến thể gateway UID → tên} (xem `core.uid.gateway_name_index`)."""
        return gateway_name_index(
            (getattr(d, "uid", ""), getattr(d, "name", "") or getattr(d, "uid", ""))
            for d in devices or ()
        )

    def set_devices(self, devices) -> None:
        """Cập nhật danh sách thiết bị (khi người dùng sửa trong tab Thiết bị)."""
        self.devices = list(devices or [])
        self.gateway_names = self._gateway_name_index(self.devices)

    def set_run(self, run_dir: Optional[Path]) -> None:
        """Chọn sẵn phạm vi = một phiên cụ thể (dùng từ tab "Lịch sử phiên").

        `run_dir=None` → trả về "Toàn bộ dữ liệu". Trả về True nếu tìm thấy phiên
        trong combobox (người dùng đã refresh gần đây), ngược lại thêm tạm mục đó
        để vẫn tìm được dù danh sách chưa được làm mới.
        """
        if run_dir is None:
            self.cmb_scope.setCurrentIndex(0)
            self.offset = 0
            self._on_scope_changed()
            return True
        run_dir = Path(run_dir)
        index = self.cmb_scope.findData(run_dir)
        if index < 0:
            # Phiên chưa có trong danh sách (vừa tạo / chưa refresh): thêm ngay.
            self.cmb_scope.addItem(run_dir.name, run_dir)
            index = self.cmb_scope.count() - 1
        self.cmb_scope.setCurrentIndex(index)
        self._on_scope_changed()
        return True

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(self._build_form())

        bar = QHBoxLayout()
        self.btn_search = QPushButton("🔍  Tìm kiếm")
        self.btn_search.setObjectName("primary")
        self.btn_search.clicked.connect(self.search_first_page)
        self.btn_prev = QPushButton("◀  Trang trước")
        self.btn_prev.clicked.connect(self.search_prev_page)
        self.btn_next = QPushButton("Trang sau  ▶")
        self.btn_next.clicked.connect(self.search_next_page)
        self.btn_export = QPushButton("💾  Xuất kết quả ra CSV")
        self.btn_export.clicked.connect(self._export)
        self.btn_open_dir = QPushButton("📂  Mở thư mục dữ liệu")
        self.btn_open_dir.clicked.connect(self._open_dir)
        bar.addWidget(self.btn_search)
        bar.addWidget(self.btn_prev)
        bar.addWidget(self.btn_next)
        bar.addStretch(1)
        bar.addWidget(self.btn_export)
        bar.addWidget(self.btn_open_dir)
        layout.addLayout(bar)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # Bảng phẳng, không sọc (xem ghi chú QTableView trong ui/theme.py).
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setFont(theme.mono_font(9))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        self.lbl_status = QLabel("Chưa tìm kiếm.")
        self.lbl_status.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_DIM)
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)
        self._update_buttons()

    def _build_form(self) -> QWidget:
        box = QGroupBox("Điều kiện tìm kiếm")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(10)

        self.cmb_scope = QComboBox()
        self.cmb_scope.currentIndexChanged.connect(self._on_scope_changed)
        grid.addWidget(QLabel("Phạm vi:"), 0, 0)
        grid.addWidget(self.cmb_scope, 0, 1, 1, 3)

        self.lbl_scope_info = QLabel("")
        self.lbl_scope_info.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_DIM)
        self.lbl_scope_info.setWordWrap(True)
        grid.addWidget(self.lbl_scope_info, 1, 0, 1, 4)

        self.ed_gateway = QLineEdit()
        self.ed_gateway.setPlaceholderText("4F9199E139C (khớp một phần cũng được)")
        self.ed_receiver = QLineEdit()
        self.ed_receiver.setPlaceholderText("16 hex, ví dụ 64B5B30615E18000")
        self.ed_node = QLineEdit()
        self.ed_node.setPlaceholderText("16 hoặc 24 hex")
        self.ed_seq = QLineEdit()
        self.ed_seq.setPlaceholderText("123  hoặc  100-200")
        grid.addWidget(QLabel("Gateway UID:"), 2, 0)
        grid.addWidget(self.ed_gateway, 2, 1)
        grid.addWidget(QLabel("Receiver UID:"), 2, 2)
        grid.addWidget(self.ed_receiver, 2, 3)
        grid.addWidget(QLabel("Node UID:"), 3, 0)
        grid.addWidget(self.ed_node, 3, 1)
        grid.addWidget(QLabel("SEQ:"), 3, 2)
        grid.addWidget(self.ed_seq, 3, 3)

        self.cmb_packet = QComboBox()
        self.cmb_packet.addItems(["(tất cả)", "DATA", "DATA_NO_SEQ", "INFO", "ERROR", "LOG", "UNKNOWN"])
        self.cmb_anomaly = QComboBox()
        self.cmb_anomaly.addItems([
            "(tất cả)", "MISSING", "DUPLICATE", "OUT_OF_ORDER", "SEQ_RESET",
            "SEQ_WRAP", "INVALID_JSON", "UNKNOWN_GATEWAY", "UNKNOWN_NODE_FORMAT",
        ])
        grid.addWidget(QLabel("Loại bản tin:"), 4, 0)
        grid.addWidget(self.cmb_packet, 4, 1)
        grid.addWidget(QLabel("Bất thường:"), 4, 2)
        grid.addWidget(self.cmb_anomaly, 4, 3)

        self.ed_error = QLineEdit()
        self.ed_error.setPlaceholderText("CRC ERROR, LENGTH, …")
        self.ed_free = QLineEdit()
        self.ed_free.setPlaceholderText("Từ khoá bất kỳ trong payload / topic")
        grid.addWidget(QLabel("Lỗi chứa:"), 5, 0)
        grid.addWidget(self.ed_error, 5, 1)
        grid.addWidget(QLabel("Từ khoá:"), 5, 2)
        grid.addWidget(self.ed_free, 5, 3)

        when = QWidget()
        when_layout = QHBoxLayout(when)
        when_layout.setContentsMargins(0, 0, 0, 0)
        self.chk_time = QCheckBox("Lọc theo thời gian")
        self.chk_time.toggled.connect(self._on_time_toggled)
        self.dt_from = QDateTimeEdit(datetime.now())
        self.dt_to = QDateTimeEdit(datetime.now())
        for widget in (self.dt_from, self.dt_to):
            widget.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            widget.setCalendarPopup(True)
            widget.setEnabled(False)
        when_layout.addWidget(self.chk_time)
        when_layout.addWidget(QLabel("từ"))
        when_layout.addWidget(self.dt_from)
        when_layout.addWidget(QLabel("đến"))
        when_layout.addWidget(self.dt_to)
        when_layout.addStretch(1)
        grid.addWidget(when, 6, 0, 1, 4)

        filters = QWidget()
        filters_layout = QHBoxLayout(filters)
        filters_layout.setContentsMargins(0, 0, 0, 0)
        self.chk_valid = QCheckBox("Chỉ bản tin hợp lệ")
        self.chk_invalid = QCheckBox("Chỉ bản tin KHÔNG hợp lệ")
        self.chk_valid.toggled.connect(
            lambda on: self.chk_invalid.setChecked(False) if on else None
        )
        self.chk_invalid.toggled.connect(
            lambda on: self.chk_valid.setChecked(False) if on else None
        )
        self.spin_limit = QSpinBox()
        self.spin_limit.setRange(10, 20000)
        self.spin_limit.setValue(self.page_size)
        self.spin_limit.setSingleStep(100)
        filters_layout.addWidget(self.chk_valid)
        filters_layout.addWidget(self.chk_invalid)
        filters_layout.addStretch(1)
        filters_layout.addWidget(QLabel("Số dòng / trang:"))
        filters_layout.addWidget(self.spin_limit)
        grid.addWidget(filters, 7, 0, 1, 4)
        return box

    # ------------------------------------------------------------------ scope
    def refresh_runs(self) -> None:
        """Nạp danh sách phiên chạy vào combobox phạm vi."""
        current = self.cmb_scope.currentData()
        self.cmb_scope.blockSignals(True)
        self.cmb_scope.clear()
        self.cmb_scope.addItem("Toàn bộ dữ liệu (data/runs/…)", None)
        self._run_dirs = list_run_dirs(self.data_dir)
        for run_dir in self._run_dirs:
            session = open_session(run_dir)
            label = "%s • %s • %s" % (
                run_dir.name,
                human_duration(session.duration_seconds or session.elapsed_seconds()),
                session.status,
            )
            self.cmb_scope.addItem(label, run_dir)
        self.cmb_scope.blockSignals(False)
        if current is not None:
            index = self.cmb_scope.findData(current)
            if index >= 0:
                self.cmb_scope.setCurrentIndex(index)
        self._on_scope_changed()

    def _on_scope_changed(self) -> None:
        run_dir = self.cmb_scope.currentData()
        if run_dir is None:
            self.lbl_scope_info.setText(
                "Sẽ quét mọi file mqtt_data_*.csv trong mọi phiên chạy."
            )
            self._scanned_dir = self.data_dir
            # Phạm vi "toàn bộ dữ liệu": không có phiên nào để chốt tên, dùng
            # danh sách thiết bị hiện tại.
            self._scope_gateway_names = {}
        else:
            session = open_session(Path(run_dir))
            self.lbl_scope_info.setText(
                "Phiên %s • %s → %s • %s • bật %d gateway • %d file / %s"
                % (
                    session.run_id,
                    session.started_at or "-",
                    session.ended_at or "(đang chạy)",
                    session.status,
                    session.gateways_monitored,
                    session.file_count(),
                    human_size(session.total_bytes()),
                )
            )
            self._scanned_dir = Path(run_dir)
            # Ưu tiên ẢNH CHỤP trong meta.json của phiên; phiên của bản tool cũ
            # (không có ảnh chụp) trả {} → quay về tên hiện tại.
            self._scope_gateway_names = session_gateway_names(self._scanned_dir)
        self.offset = 0
        self._update_buttons()

    def _on_time_toggled(self, on: bool) -> None:
        self.dt_from.setEnabled(on)
        self.dt_to.setEnabled(on)

    # ----------------------------------------------------------------- search
    def _build_query(self) -> SearchQuery:
        files: List[Path] = []
        run_dir = self.cmb_scope.currentData()
        directory = Path(run_dir) if run_dir is not None else self.data_dir
        files = self._files_for_scope(directory)
        query = SearchQuery(
            gateway_uid=self.ed_gateway.text().strip(),
            receiver_uid=self.ed_receiver.text().strip(),
            node_uid=self.ed_node.text().strip(),
            seq=self.ed_seq.text().strip(),
            anomaly="" if self.cmb_anomaly.currentIndex() == 0 else self.cmb_anomaly.currentText(),
            packet_type="" if self.cmb_packet.currentIndex() == 0 else self.cmb_packet.currentText(),
            error_contains=self.ed_error.text().strip(),
            free_text=self.ed_free.text().strip(),
            files=files,
            limit=int(self.spin_limit.value()),
            offset=self.offset,
            gateway_names=(self._scope_gateway_names or self.gateway_names),
        )
        if self.chk_time.isChecked():
            query.time_from = self.dt_from.dateTime().toPyDateTime()
            query.time_to = self.dt_to.dateTime().toPyDateTime()
        if self.chk_valid.isChecked():
            query.only_valid = True
        if self.chk_invalid.isChecked():
            query.only_invalid = True
        return query

    def _files_for_scope(self, directory: Path) -> List[Path]:
        if directory == self.data_dir:
            files: List[Path] = []
            for run_dir in self._run_dirs:
                files.extend(list_csv_files(run_dir / "mqtt", "mqtt_data"))
            return files
        return list_csv_files(directory / "mqtt", "mqtt_data")

    def search_first_page(self) -> None:
        self.offset = 0
        self._run_search()

    def search_next_page(self) -> None:
        if self.last_result is None or not self.last_result.truncated:
            return
        self.offset += int(self.spin_limit.value())
        self._run_search()

    def search_prev_page(self) -> None:
        if self.offset <= 0:
            return
        self.offset = max(0, self.offset - int(self.spin_limit.value()))
        self._run_search()

    def _run_search(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        query = self._build_query()
        directory = self.cmb_scope.currentData()
        self._scanned_dir = Path(directory) if directory is not None else self.data_dir
        self.btn_search.setEnabled(False)
        self.btn_search.setText("⏳  Đang tìm…")
        self.lbl_status.setText("Đang quét CSV, vui lòng đợi…")
        self.worker = SearchWorker(self._scanned_dir, query, self)
        self.worker.finished_ok.connect(self._on_result)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self._on_worker_done)
        self.worker.start()

    def _on_worker_done(self) -> None:
        self.btn_search.setEnabled(True)
        self.btn_search.setText("🔍  Tìm kiếm")
        self._update_buttons()

    def _on_failed(self, message: str) -> None:
        self.lbl_status.setText("Lỗi tìm kiếm: %s" % message)
        QMessageBox.warning(self, "Lỗi tìm kiếm", message)

    def _on_result(self, result: SearchResult) -> None:
        self.last_result = result
        self.model.reset_rows(result.rows)
        pages = "trang %d" % (self.offset // max(1, int(self.spin_limit.value())) + 1)
        text = (
            "Tìm thấy %s dòng khớp / đã quét %s dòng trong %d file — hiển thị %s (%s)."
            % (
                "{:,}".format(result.matched).replace(",", "."),
                "{:,}".format(result.scanned).replace(",", "."),
                result.files_scanned,
                "{:,}".format(len(result.rows)).replace(",", "."),
                pages,
            )
        )
        if result.truncated:
            text += " Còn dữ liệu phía sau — bấm 'Trang sau'."
        if result.errors:
            text += " • %d file lỗi đọc." % len(result.errors)
        self.lbl_status.setText(text)
        self._update_buttons()

    def _update_buttons(self) -> None:
        has_result = self.last_result is not None
        self.btn_prev.setEnabled(has_result and self.offset > 0)
        self.btn_next.setEnabled(bool(has_result and self.last_result.truncated))
        self.btn_export.setEnabled(bool(has_result and self.last_result.rows))

    # ----------------------------------------------------------------- action
    def _export(self) -> None:
        if self.last_result is None or not self.last_result.rows:
            return
        default = str(
            Path.home() / ("%s_ketqua_timkiem.csv" % datetime.now().strftime("%Y%m%d_%H%M%S"))
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Xuất kết quả tìm kiếm", default, "CSV (*.csv)"
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
                writer.writeheader()
                for row in self.last_result.rows:
                    writer.writerow(row)
        except OSError as exc:
            QMessageBox.warning(self, "Không ghi được file", str(exc))
            return
        QMessageBox.information(self, "Đã xuất", "Đã ghi %d dòng vào:\n%s"
                                % (len(self.last_result.rows), path))

    def _open_dir(self) -> None:
        directory = self._scanned_dir or self.data_dir
        if not directory.exists():
            QMessageBox.information(self, "Chưa có dữ liệu", "Thư mục chưa tồn tại:\n%s" % directory)
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(directory))  # noqa: S606 - mở explorer
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(directory)])
            else:
                subprocess.Popen(["xdg-open", str(directory)])
        except OSError as exc:
            QMessageBox.warning(self, "Không mở được", str(exc))
