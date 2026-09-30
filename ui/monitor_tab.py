"""Tab "Giám sát" — trái tim của GUI: trạng thái + bản tin live + node + cảnh báo.

Dữ liệu KHÔNG đến từ signal MQTT (sẽ nghẽn UI khi lưu lượng cao) mà từ
`QTimer` polling `MonitorRunner.stats()` mỗi `ui_refresh_ms`. Cách này:

  - tách hoàn toàn thread nền khỏi UI thread;
  - nếu GUI lag/tạm treo thì dữ liệu vẫn được ghi (không mất gì);
  - người dùng vẫn thấy số liệu "gần thời gian thực" (mặc định 500 ms).

Bảng live lấy dữ liệu qua `MonitorRunner.subscribe_records()` — một hàng đợi
có giới hạn riêng cho GUI, để bảng không làm chậm pipeline phân tích.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .log_viewer import LogViewer
from .table_model import (
    DictTableModel,
    fmt_age,
    fmt_bool,
    fmt_float,
    fmt_int,
    fmt_sensor_values,
    fmt_time,
)

# Cột hiển thị của bảng live (đã lọc từ 35 cột CSV cho dễ đọc).
LIVE_COLUMNS = [
    "received_at", "gateway_uid", "receiver_uid", "node_uid", "packet_type",
    "seq", "seq_status", "missing_count", "rssi", "snr", "mcu_temp", "mcu_vdd",
    "sensor_values", "anomaly", "validation_error",
]
LIVE_HEADERS = [
    "Giờ nhận", "Gateway", "Receiver", "Node", "Loại", "SEQ", "SEQ status",
    "Thiếu", "RSSI", "SNR", "Nhiệt", "VDD", "Giá trị cảm biến", "Bất thường",
    "Lỗi validate",
]

NODE_COLUMNS = [
    "node_uid", "node_format", "gateways_heard", "gateways_heard_avg",
    "packets", "first_seq", "last_seq",
    "missing", "duplicate", "out_of_order", "seq_reset", "seq_wrap",
    "delivery_rate_percent", "last_seen",
]
NODE_HEADERS = [
    "Node", "Định dạng UID", "Số GW nghe", "GW/bản tin",
    "Gói", "SEQ đầu", "SEQ cuối", "Thiếu", "Trùng",
    "Sai thứ tự", "Reset", "Wrap", "Tỉ lệ nhận (%)", "Lần cuối",
]

ALERT_COLUMNS = ["timestamp", "level", "alert_type", "gateway_uid", "node_uid", "message"]
ALERT_HEADERS = ["Thời điểm", "Mức", "Loại", "Gateway", "Node", "Nội dung"]

# --- Node trọng tâm: bắt hụt / bắn hụt per gateway ---
FOCUS_SUMMARY_COLUMNS = [
    "gateway_uid", "gateway_name", "packets_heard", "reference_packets",
    "missed_catch", "missed_catch_percent", "gap_pattern",
    "late_packets", "late_percent", "delay_max_seconds", "last_seen", "notes",
]
FOCUS_SUMMARY_HEADERS = [
    "Gateway", "Tên", "Đã bắt", "Node đã phát",
    "BẮT HỤT", "Bắt hụt (%)", "Kiểu ngắt quãng",
    "BẮN HỤT", "Bắn hụt (%)", "Trễ max (s)", "Lần cuối", "Ghi chú",
]
FOCUS_MISSING_COLUMNS = [
    "gateway_uid", "missing_from", "missing_to", "missing_count",
    "window_start", "window_end",
]
FOCUS_MISSING_HEADERS = [
    "Gateway", "SEQ từ", "SEQ đến", "Số bản tin",
    "Từ thời điểm", "Đến thời điểm",
]
FOCUS_LATE_COLUMNS = [
    "gateway_uid", "seq", "received_at", "gateway_timestamp", "delay_seconds",
]
FOCUS_LATE_HEADERS = [
    "Gateway", "SEQ", "Giờ PC nhận", "Giờ gateway", "Trễ (s)",
]


class StatusCard(QGroupBox):
    """Ô hiển thị 1 chỉ số lớn (giá trị + nhãn phụ)."""

    def __init__(self, title: str, value: str = "-", parent=None) -> None:
        super().__init__(title, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 6)
        layout.setSpacing(0)
        self.value_label = QLabel(value)
        self.value_label.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: %s;" % theme.COLOR_TEXT
        )
        self.sub_label = QLabel("")
        self.sub_label.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_DIM)
        layout.addWidget(self.value_label)
        layout.addWidget(self.sub_label)

    def set(self, value: str, sub: str = "", color: str = "") -> None:
        self.value_label.setText(value)
        if color:
            self.value_label.setStyleSheet(
                "font-size: 18px; font-weight: bold; color: %s;" % color
            )
        self.sub_label.setText(sub)


class MonitorTab(QWidget):
    """Tab giám sát: 6 thẻ chỉ số + 3 bảng + log."""

    # Phát ra khi người dùng bấm nút trong tab (main_window xử lý).
    start_requested = pyqtSignal()
    pause_requested = pyqtSignal(bool)   # True = pause, False = resume
    stop_requested = pyqtSignal()
    open_run_dir_requested = pyqtSignal()
    export_requested = pyqtSignal()    # UID node trọng tâm người dùng muốn theo dõi ("" = tắt theo dõi).
    focused_node_requested = pyqtSignal(str)

    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.runner = None

        # Hàng đợi record cho GUI (giới hạn để không tốn RAM)
        self._pending: Deque[Dict[str, Any]] = deque(maxlen=100000)
        self._pending_alerts: Deque[Dict[str, Any]] = deque(maxlen=5000)
        self._node_rows: Dict[str, Dict[str, Any]] = {}
        self._dropped_for_ui = 0
        self._focus_uid = ""

        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(max(200, int(getattr(config, "ui_refresh_ms", 500))))
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)
        root.addWidget(self._build_toolbar())
        root.addWidget(self._build_cards())

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._build_tables())
        splitter.addWidget(self._build_log())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

    def _build_toolbar(self) -> QWidget:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)

        self.btn_start = QPushButton("▶  Bắt đầu test")
        self.btn_start.setObjectName("primary")
        # `clicked` phát kèm `False` (trạng thái "checked") nên KHÔNG nối thẳng vào
        # `start_requested` (không tham số) — nối qua handler để đọc được tên phiên.
        self.btn_start.clicked.connect(self._on_start_clicked)
        self.btn_pause = QPushButton("⏸  Tạm dừng")
        self.btn_pause.setCheckable(True)
        self.btn_pause.clicked.connect(self._on_pause_toggled)
        self.btn_stop = QPushButton("■  Dừng & ghi báo cáo")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.clicked.connect(self.stop_requested.emit)
        self.btn_open = QPushButton("📂  Mở thư mục log")
        self.btn_open.clicked.connect(self.open_run_dir_requested.emit)
        self.btn_export = QPushButton("📊  Ghi báo cáo ngay")
        self.btn_export.clicked.connect(self.export_requested.emit)

        # TÊN PHIÊN (không bắt buộc): người dùng đặt sẵn cho phiên SẮP chạy, ví dụ
        # "test lại GW 4F9199". Để trống thì tên phiên = mã phiên tự sinh. Sửa lại
        # được bất cứ lúc nào ở tab Lịch sử phiên.
        self.ed_session_name = QLineEdit()
        self.ed_session_name.setPlaceholderText(
            "Tên phiên (không bắt buộc) — vd: test lại GW 4F9199"
        )
        self.ed_session_name.setMaxLength(120)
        self.ed_session_name.setClearButtonEnabled(True)
        self.ed_session_name.setToolTip(
            "Đặt tên cho phiên để dễ nhớ khi xem lại trong tab “Lịch sử phiên”.\n"
            "Để trống cũng được — có thể đặt/sửa tên sau khi phiên đã chạy."
        )
        self.ed_session_name.setMinimumWidth(240)

        self.lbl_state = QLabel("IDLE")
        self.lbl_state.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: %s; padding: 0 10px;"
            % theme.COLOR_DIM
        )

        for widget in (self.btn_start, self.btn_pause, self.btn_stop):
            layout.addWidget(widget)
        layout.addWidget(self.ed_session_name)
        layout.addWidget(self.lbl_state)
        layout.addStretch(1)
        layout.addWidget(self.btn_export)
        layout.addWidget(self.btn_open)

        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        return box

    def _build_cards(self) -> QWidget:
        box = QWidget()
        grid = QGridLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)

        self.card_state = StatusCard("Trạng thái")
        self.card_elapsed = StatusCard("Thời gian chạy")
        self.card_mqtt = StatusCard("Kết nối MQTT")
        self.card_packets = StatusCard("Bản tin nhận được")
        self.card_nodes = StatusCard("Node / Gateway")
        self.card_integrity = StatusCard("Chất lượng dữ liệu")
        self.card_ping = StatusCard("Ping gateway")
        self.card_disk = StatusCard("Ghi xuống đĩa")

        cards = [
            self.card_state, self.card_elapsed, self.card_mqtt, self.card_packets,
            self.card_nodes, self.card_integrity, self.card_ping, self.card_disk,
        ]
        for index, card in enumerate(cards):
            grid.addWidget(card, index // 4, index % 4)
        return box

    def _build_tables(self) -> QWidget:
        tabs = QTabWidget()

        # --- bản tin live ---
        self.model_live = DictTableModel(
            LIVE_COLUMNS,
            LIVE_HEADERS,
            max_rows=int(getattr(self.config, "live_table_max_rows", 2000)),
            color_column="packet_type",
            color_map=theme.PACKET_COLORS,
            formatters={
                "received_at": fmt_time,
                "seq": fmt_int,
                "missing_count": fmt_int,
                "rssi": lambda v: fmt_float(v, 1),
                "snr": lambda v: fmt_float(v, 2),
                "mcu_temp": lambda v: fmt_float(v, 1),
                "mcu_vdd": lambda v: fmt_float(v, 2),
                "sensor_values": fmt_sensor_values,
            },
            align_right=("seq", "missing_count", "rssi", "snr", "mcu_temp", "mcu_vdd"),
        )
        view_live = self._make_view(self.model_live)
        page_live = QWidget()
        layout_live = QVBoxLayout(page_live)
        layout_live.setContentsMargins(0, 0, 0, 0)
        layout_live.addWidget(view_live)
        self.lbl_live_info = QLabel("")
        self.lbl_live_info.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_DIM)
        layout_live.addWidget(self.lbl_live_info)
        tabs.addTab(page_live, "Bản tin trực tiếp")

        # --- tổng hợp node ---
        self.model_nodes = DictTableModel(
            NODE_COLUMNS,
            NODE_HEADERS,
            max_rows=20000,
            color_column="node_format",
            color_map={
                "NEW_NODE_MSG": theme.COLOR_OK,
                "NEW_NODE_OLD_MSG": theme.COLOR_INFO,
                "OLD_NODE": theme.COLOR_WARN,
                "UNKNOWN_FORMAT": theme.COLOR_CRIT,
                "UNKNOWN": theme.COLOR_CRIT,
            },
            formatters={
                "packets": fmt_int,
                "missing": fmt_int,
                "duplicate": fmt_int,
                "out_of_order": fmt_int,
                "seq_reset": fmt_int,
                "seq_wrap": fmt_int,
                "first_seq": fmt_int,
                "last_seq": fmt_int,
                "delivery_rate_percent": lambda v: theme.pct(v, 2),
                "last_seen": fmt_age,
            },
            align_right=("packets", "missing", "duplicate", "out_of_order",
                         "seq_reset", "seq_wrap", "delivery_rate_percent"),
        )
        tabs.addTab(self._make_view(self.model_nodes), "Tổng hợp node")

        # --- cảnh báo ---
        self.model_alerts = DictTableModel(
            ALERT_COLUMNS,
            ALERT_HEADERS,
            max_rows=int(getattr(self.config, "ui_max_alerts_shown", 500)),
            color_column="level",
            color_map=theme.LEVEL_COLORS,
            formatters={"timestamp": fmt_time},
        )
        tabs.addTab(self._make_view(self.model_alerts), "Cảnh báo")

        # --- downtime ---
        self.model_down = DictTableModel(
            ["down_start", "down_end", "gateway_uid", "ip_address",
             "duration_seconds", "failed_ping_count", "status"],
            ["Bắt đầu mất", "Hồi phục", "Gateway", "IP", "Thời lượng (s)",
             "Số ping fail", "Trạng thái"],
            max_rows=5000,
            color_column="status",
            color_map={"ONGOING": theme.COLOR_CRIT, "CLOSED": theme.COLOR_OK},
            formatters={
                "down_start": fmt_time,
                "down_end": fmt_time,
                "duration_seconds": lambda v: fmt_float(v, 1),
                "failed_ping_count": fmt_int,
            },
            align_right=("duration_seconds", "failed_ping_count"),
        )
        tabs.addTab(self._make_view(self.model_down), "Downtime")

        # --- node trọng tâm (bắt hụt / bắn hụt) ---
        tabs.addTab(self._build_focus_page(), "Node trọng tâm")

        self.tables_tabs = tabs
        return tabs

    def _build_focus_page(self) -> QWidget:
        """Trang theo dõi 1 node trên nhiều gateway (bắt hụt / bắn hụt).

        Xem `analytics/focused_node.py` để hiểu định nghĩa hai khái niệm:
          - BẮT HỤT  : node đã phát (gateway khác nghe) mà gateway này không publish
          - BẮN HỤT  : gateway CÓ publish nhưng trễ (buffer SD / backfill)
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        bar = QWidget()
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.addWidget(QLabel("UID node theo dõi:"))
        self.edit_focus = QLineEdit()
        self.edit_focus.setPlaceholderText("VD: 0011223344556677 (16 hex) hoặc UID 24 hex của node cũ")
        self.edit_focus.setMaximumWidth(420)
        self.edit_focus.returnPressed.connect(self._apply_focus)
        bar_layout.addWidget(self.edit_focus)
        self.btn_focus_apply = QPushButton("Theo dõi")
        self.btn_focus_apply.setObjectName("primary")
        self.btn_focus_apply.clicked.connect(self._apply_focus)
        bar_layout.addWidget(self.btn_focus_apply)
        self.btn_focus_clear = QPushButton("Bỏ theo dõi")
        self.btn_focus_clear.clicked.connect(lambda: self._request_focus(""))
        bar_layout.addWidget(self.btn_focus_clear)
        bar_layout.addStretch(1)
        self.lbl_focus = QLabel("— chưa chọn node nào")
        self.lbl_focus.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_DIM)
        bar_layout.addWidget(self.lbl_focus)
        layout.addWidget(bar)

        self.model_focus = DictTableModel(
            FOCUS_SUMMARY_COLUMNS,
            FOCUS_SUMMARY_HEADERS,
            max_rows=500,
            color_column="missed_catch_percent",
            formatters={
                "packets_heard": fmt_int,
                "reference_packets": fmt_int,
                "missed_catch": fmt_int,
                "late_packets": fmt_int,
                "missed_catch_percent": lambda v: theme.pct(v, 2),
                "late_percent": lambda v: theme.pct(v, 2),
                "delay_max_seconds": lambda v: fmt_float(v, 1),
                "last_seen": fmt_age,
            },
            align_right=("packets_heard", "reference_packets", "missed_catch",
                         "late_packets", "missed_catch_percent", "late_percent",
                         "delay_max_seconds"),
        )
        self.model_focus_missing = DictTableModel(
            FOCUS_MISSING_COLUMNS,
            FOCUS_MISSING_HEADERS,
            max_rows=5000,
            formatters={
                "missing_from": fmt_int,
                "missing_to": fmt_int,
                "missing_count": fmt_int,
                "window_start": fmt_time,
                "window_end": fmt_time,
            },
            align_right=("missing_from", "missing_to", "missing_count"),
        )
        self.model_focus_late = DictTableModel(
            FOCUS_LATE_COLUMNS,
            FOCUS_LATE_HEADERS,
            max_rows=5000,
            formatters={
                "seq": fmt_int,
                "received_at": fmt_time,
                "gateway_timestamp": fmt_time,
                "delay_seconds": lambda v: fmt_float(v, 1),
            },
            align_right=("seq", "delay_seconds"),
        )

        inner = QTabWidget()
        inner.addTab(self._make_view(self.model_focus), "Tổng hợp per gateway")
        inner.addTab(self._make_view(self.model_focus_missing), "Chi tiết bắt hụt (theo SEQ)")
        inner.addTab(self._make_view(self.model_focus_late), "Chi tiết bắn hụt (publish trễ)")
        layout.addWidget(inner, 1)
        return page

    def _apply_focus(self) -> None:
        self._request_focus(self.edit_focus.text())

    def _request_focus(self, node_uid: str) -> None:
        """Chuẩn hoá UID rồi phát signal cho main_window (nơi có runner/config)."""
        from analytics.focused_node import normalize_focus_node

        raw = (node_uid or "").strip()
        normalized = normalize_focus_node(raw)
        if raw and not normalized:
            self.lbl_focus.setText("⚠ UID không hợp lệ (chỉ nhận hex, độ dài chẵn)")
            self.lbl_focus.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_CRIT)
            return
        self.edit_focus.setText(normalized)
        self.focused_node_requested.emit(normalized)

    def set_focused_node_display(self, node_uid: str) -> None:
        """Đồng bộ ô nhập khi node trọng tâm được đặt từ nơi khác (config lúc mở)."""
        self._focus_uid = node_uid or ""
        if self.edit_focus.text().strip().upper() != self._focus_uid:
            self.edit_focus.setText(self._focus_uid)
        if not self._focus_uid:
            self.lbl_focus.setText("— chưa chọn node nào")
            self.lbl_focus.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_DIM)

    def _build_log(self) -> QWidget:
        box = QGroupBox("Nhật ký tool")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(6, 6, 6, 6)
        self.log_view = LogViewer(max_lines=5000)
        layout.addWidget(self.log_view)
        return box

    def _make_view(self, model: DictTableModel) -> QTableView:
        view = QTableView()
        view.setModel(model)
        # Bảng phẳng, không sọc (xem ghi chú QTableView trong ui/theme.py).
        view.setAlternatingRowColors(False)
        view.setSelectionBehavior(QAbstractItemView.SelectRows)
        view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        view.setSortingEnabled(False)   # dữ liệu vào liên tục, sort sẽ gây lag
        view.setWordWrap(False)
        view.setFont(theme.mono_font(9))
        view.verticalHeader().setDefaultSectionSize(20)
        view.verticalHeader().setVisible(False)
        view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        view.horizontalHeader().setStretchLastSection(True)
        return view

    # ------------------------------------------------------------------ wiring
    def attach(self, runner) -> None:
        """Gắn MonitorRunner: đăng ký nhận record, cảnh báo, sự kiện, trạng thái."""
        self.runner = runner
        runner.subscribe_records(self._on_record)
        runner.subscribe_alerts(self._on_alert_row)
        runner.subscribe_events(self._on_event)
        runner.subscribe_state(self._on_state)

    def log(self, text: str, level: str = "INFO") -> None:
        self.log_view.append_line(text, level)

    # ------------------------------------------------------------ start/name
    def session_name(self) -> str:
        """TÊN PHIÊN người dùng nhập (đã gọt khoảng trắng, rỗng nếu bỏ trống)."""
        return self.ed_session_name.text().strip()

    def set_session_name(self, text: str) -> None:
        """Điền sẵn tên phiên (dùng khi MainWindow muốn gợi ý tên cho lần chạy sau)."""
        self.ed_session_name.setText(str(text or ""))

    def _on_start_clicked(self) -> None:
        """Nút Bắt đầu kèm TÊN PHIÊN đã nhập (xem `start_requested`)."""
        self.start_requested.emit()

    # ---------------------------------------------------- callback từ runner
    def _on_record(self, row: Dict[str, Any]) -> None:
        """Chạy trên thread phân tích → chỉ đẩy vào deque, không chạm Qt."""
        if len(self._pending) == self._pending.maxlen:
            self._dropped_for_ui += 1
        self._pending.append(row)

    def _on_alert_row(self, row: Dict[str, Any]) -> None:
        self._pending_alerts.append(row)

    def _on_event(self, event: str, message: str) -> None:
        level = {"error": "ERROR", "reconnecting": "WARN", "disconnect": "WARN"}.get(
            event, "EVENT"
        )
        self.log("[%s] %s" % (event.upper(), message), level)

    def _on_state(self, state) -> None:
        self._apply_state(state.state, error=getattr(state, "error", ""))

    def _on_pause_toggled(self, checked: bool) -> None:
        self.btn_pause.setText("▶  Tiếp tục" if checked else "⏸  Tạm dừng")
        self.pause_requested.emit(checked)

    # ------------------------------------------------------------------ refresh
    def _refresh(self) -> None:
        self._drain_records()
        self._drain_alerts()
        self._refresh_stats()

    def _drain_records(self) -> None:
        if not self._pending:
            return
        batch: List[Dict[str, Any]] = []
        for _ in range(min(len(self._pending), 5000)):
            batch.append(self._pending.popleft())
        self.model_live.append_many(batch)
        self.model_live.flush()
        info = "Hiển thị %d dòng mới nhất (đã bỏ %d dòng cũ khỏi GUI, dữ liệu đầy đủ trong CSV)" % (
            self.model_live.rowCount(), self.model_live.truncated_count(),
        )
        if self._dropped_for_ui:
            info += " • %d dòng không kịp hiển thị (đã ghi CSV)" % self._dropped_for_ui
        self.lbl_live_info.setText(info)

    def _drain_alerts(self) -> None:
        if not self._pending_alerts:
            return
        rows = []
        while self._pending_alerts:
            rows.append(self._pending_alerts.popleft())
        self.model_alerts.append_many(rows)
        self.model_alerts.flush()

    def _refresh_stats(self) -> None:
        if self.runner is None:
            return
        stats = self.runner.stats()
        pipeline = stats.get("pipeline") or {}
        counters = pipeline.get("counters") or {}
        session = stats.get("session") or {}
        mqtt = pipeline.get("mqtt") or {}
        ping = pipeline.get("ping") or {}
        csv = stats.get("csv") or {}

        elapsed = session.get("elapsed_seconds") or pipeline.get("elapsed_seconds") or 0
        self.card_elapsed.set(theme.human_duration(elapsed),
                              "bắt đầu %s" % (session.get("started_at") or "-"))

        connected = bool(mqtt.get("connected"))
        self.card_mqtt.set(
            "ĐÃ KẾT NỐI" if connected else str(mqtt.get("state") or "—"),
            "reconnect: %s • uptime: %s" % (
                fmt_int(mqtt.get("reconnects", 0)),
                theme.human_duration(mqtt.get("uptime_seconds") if isinstance(
                    mqtt.get("uptime_seconds"), (int, float)) else None),
            ),
            theme.COLOR_OK if connected else theme.COLOR_CRIT,
        )

        total = counters.get("total_messages", 0) or 0
        self.card_packets.set(
            theme.human_count(total),
            "DATA %s • INFO %s • ERROR %s • LOG %s" % (
                theme.human_count(counters.get("data_packets", 0)),
                theme.human_count(counters.get("info_packets", 0)),
                theme.human_count(counters.get("error_packets", 0)),
                theme.human_count(counters.get("log_messages", 0)),
            ),
        )

        self.card_nodes.set(
            "%s node" % theme.human_count(pipeline.get("nodes_seen", 0)),
            "gateway cấu hình: %s • hàng đợi: %s" % (
                theme.human_count(session.get("gateways_monitored")),
                fmt_int(pipeline.get("queue_size", 0)),
            ),
        )

        missing = counters.get("missing_packets", 0) or 0
        duplicates = counters.get("duplicate_packets", 0) or 0
        ooo = counters.get("out_of_order_packets", 0) or 0
        rate = (100.0 * missing / total) if total else 0.0
        color = theme.COLOR_OK
        if rate >= 5.0:
            color = theme.COLOR_CRIT
        elif rate >= 1.0:
            color = theme.COLOR_WARN
        self.card_integrity.set(
            "Thiếu %.3f %%" % rate,
            "thiếu %s • trùng %s • sai thứ tự %s" % (
                theme.human_count(missing), theme.human_count(duplicates),
                theme.human_count(ooo),
            ),
            color,
        )

        ping_total = ping.get("total_pings", 0) or 0
        ping_failed = ping.get("failed_pings", 0) or 0
        ping_ok = ping_total - ping_failed
        down = self._ping_downtime_totals()
        # Vẫn hiển thị downtime ngay trên thẻ này để người dùng biết tổng khoảng
        # thời gian ngắt kết nối ping (kể cả cửa sổ còn đang mất kết nối).
        ping_sub = "%s/%s thành công • %d gateway" % (
            fmt_int(ping_ok), fmt_int(ping_total), ping.get("targets", 0) or 0,
        )
        ping_sub += " • downtime %s (%s lần)" % (
            theme.human_duration(down["seconds"]),
            theme.human_count(down["events"]),
        )
        self.card_ping.set(
            "%s%%" % (round(100.0 * ping_ok / ping_total, 1) if ping_total else "—"),
            ping_sub,
            theme.COLOR_OK if ping_total and ping_failed == 0 else
            (theme.COLOR_WARN if ping_total else theme.COLOR_DIM),
        )

        written = sum((s or {}).get("rows_written", 0) for s in csv.values())
        dropped = sum((s or {}).get("rows_dropped", 0) for s in csv.values())
        errors = sum((s or {}).get("errors", 0) for s in csv.values())
        self.card_disk.set(
            theme.human_count(written),
            "bỏ %s • lỗi ghi %s" % (theme.human_count(dropped), theme.human_count(errors)),
            theme.COLOR_CRIT if (dropped or errors) else theme.COLOR_TEXT,
        )

        self._update_node_table()
        self._update_downtime(stats)
        self._update_focus(stats)

    def _update_focus(self, stats: Dict[str, Any]) -> None:
        """Cập nhật bảng node trọng tâm (thưa hơn: 1 lần / ~4 chu kỳ refresh).

        Quét toàn bộ bitmap trong `summary_rows()` tốn O(số gateway × 65536) nên
        không làm mỗi 500 ms; người dùng vẫn thấy số liệu gần thực.
        """
        if self.runner is None:
            return
        self._focus_tick = getattr(self, "_focus_tick", 0) + 1
        if self._focus_tick % 4 != 0:
            return
        # Quét bitmap để có "kiểu ngắt quãng" tốn ~ms/gateway → chỉ làm thưa
        # (≈ mỗi 60 lần cập nhật, tức vài chục giây) thay vì mỗi chu kỳ GUI.
        if self._focus_tick % 240 == 0:
            self.runner.refresh_focused_patterns()
        try:
            rows = self.runner.focused_node_rows()
            missing = self.runner.focused_node_missing_rows()
            late = self.runner.focused_node_late_rows()
            focus_stats = self.runner.focused_node_stats()
        except Exception:  # noqa: BLE001 - GUI không được crash vì stats
            return

        node_uid = (focus_stats or {}).get("node_uid") or ""
        self._focus_uid = node_uid
        if node_uid:
            known = (focus_stats or {}).get("gateways_known", 0) or 0
            silent = (focus_stats or {}).get("gateways_silent", 0) or 0
            text = "Đang theo dõi %s • node đã phát %d bản tin • %d/%d gateway nghe được" % (
                node_uid,
                (focus_stats or {}).get("reference_packets", 0) or 0,
                (focus_stats or {}).get("gateways_heard", 0) or 0,
                known,
            )
            if silent:
                text += " • ⚠ %d gateway KHÔNG publish bản tin nào" % silent
            self.lbl_focus.setText(text)
            self.lbl_focus.setStyleSheet(
                "color: %s; font-size: 10px;"
                % (theme.COLOR_WARN if silent else theme.COLOR_INFO)
            )
        elif not self.edit_focus.text().strip():
            self.lbl_focus.setText("— chưa chọn node nào")
            self.lbl_focus.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_DIM)

        self.model_focus.reset_rows(rows)
        self.model_focus_missing.reset_rows(missing)
        self.model_focus_late.reset_rows(late)

    def _update_node_table(self) -> None:
        if self.runner is None or self.runner.pipeline is None:
            return
        try:
            rows = self.runner.pipeline.node_state_rows()
        except Exception:  # noqa: BLE001 - GUI không được crash vì stats
            return
        signature = len(rows)
        if signature == self._node_rows.get("__count__") and not self._node_dirty():
            return
        self._node_rows["__count__"] = signature
        self.model_nodes.reset_rows(rows)

    def _node_dirty(self) -> bool:
        # Cho phép cập nhật lại mỗi ~5 chu kỳ refresh để không reset bảng liên tục
        # (reset làm mất vị trí scroll/selection của người dùng).
        self._node_tick = getattr(self, "_node_tick", 0) + 1
        return self._node_tick % 10 == 0

    def _ping_downtime_totals(self) -> Dict[str, Any]:
        """Tổng thời gian downtime + số lần mất kết nối ping của cả phiên.

        Lấy đúng dữ liệu đang hiển thị ở bảng "Downtime": các cửa sổ đã đóng
        (`downtime_records`) cộng với cửa sổ còn đang mất kết nối
        (`open_windows`) — nên con số này gồm cả khoảng đang ngắt hiện tại.
        """
        empty = {"seconds": 0.0, "events": 0, "ongoing": False}
        if self.runner is None or self.runner.pipeline is None:
            return empty
        tracker = self.runner.pipeline.ping_tracker
        seconds = 0.0
        events = 0
        try:
            for record in tracker.downtime_records():
                seconds += float(record.duration_seconds or 0.0)
                events += 1
            open_windows = tracker.open_windows()
            for window in open_windows.values():
                seconds += float(window.get("ongoing_seconds") or 0.0)
                if window.get("confirmed"):
                    events += 1
        except Exception:  # noqa: BLE001 - thẻ chỉ số không được làm GUI crash
            return empty
        return {"seconds": seconds, "events": events,
                "ongoing": bool(open_windows)}

    def _update_downtime(self, stats: Dict[str, Any]) -> None:
        if self.runner is None or self.runner.pipeline is None:
            return
        tracker = self.runner.pipeline.ping_tracker
        rows: List[Dict[str, Any]] = []
        for record in tracker.downtime_records():
            rows.append(record.to_row())
        for gateway_uid, window in tracker.open_windows().items():
            rows.append(
                {
                    "down_start": window.get("first_down_ping", ""),
                    "down_end": "",
                    "gateway_uid": gateway_uid,
                    "gateway_name": "",
                    "ip_address": "",
                    "duration_seconds": window.get("ongoing_seconds", ""),
                    "failed_ping_count": window.get("failed_count", 0),
                    "status": "ONGOING" if window.get("confirmed") else "CHƯA XÁC NHẬN",
                }
            )
        self.model_down.reset_rows(rows)

    # ------------------------------------------------------------------ state
    def _apply_state(self, state: str, error: str = "") -> None:
        running = state in ("RUNNING", "PAUSED")
        self.btn_start.setEnabled(not running)
        self.btn_pause.setEnabled(running)
        self.btn_stop.setEnabled(state in ("RUNNING", "PAUSED"))
        self.btn_export.setEnabled(state in ("RUNNING", "PAUSED"))
        if state != "PAUSED" and self.btn_pause.isChecked():
            self.btn_pause.setChecked(False)
            self.btn_pause.setText("⏸  Tạm dừng")
        self.lbl_state.setText(state)
        self.lbl_state.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: %s; padding: 0 10px;"
            % theme.STATE_COLORS.get(state, theme.COLOR_DIM)
        )
        self.card_state.set(state, error or "", theme.STATE_COLORS.get(state, theme.COLOR_DIM))

    def reset_view(self) -> None:
        """Xoá bảng khi bắt đầu phiên mới (không xoá log)."""
        self.model_live.reset_rows([])
        self.model_nodes.reset_rows([])
        self.model_alerts.reset_rows([])
        self.model_down.reset_rows([])
        self.model_focus.reset_rows([])
        self.model_focus_missing.reset_rows([])
        self.model_focus_late.reset_rows([])
        self._pending.clear()
        self._pending_alerts.clear()
        self._dropped_for_ui = 0
        self._node_rows.clear()
