"""Tab "Cấu hình" — broker MQTT, topic, tham số SEQ/ping/storage.

Mọi giá trị ở đây đọc/ghi trực tiếp trên `TestConfig`. Thay đổi KHÔNG tự áp dụng
vào phiên đang chạy (tránh nửa chừng đổi ngưỡng làm sai thống kê) — GUI sẽ hỏi
người dùng nếu họ cố sửa khi đang chạy.
"""

from __future__ import annotations

from typing import List

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.config import PING_INTERVAL_CHOICES, TestConfig

from . import theme


class ConfigTab(QWidget):
    """Form cấu hình, chia nhóm theo chủ đề."""

    config_changed = pyqtSignal()
    test_connection_requested = pyqtSignal()

    def __init__(self, config: TestConfig, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.fields = {}
        self._build()
        self.load_from(config)

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._broker_page(), "MQTT broker")
        tabs.addTab(self._analysis_page(), "Phân tích SEQ")
        tabs.addTab(self._ping_page(), "Ping / mạng")
        tabs.addTab(self._storage_page(), "Lưu trữ & log")
        layout.addWidget(tabs, 1)

        bottom = QHBoxLayout()
        self.lbl_hint = QLabel(
            "Thay đổi được lưu vào config/config.json và áp dụng cho phiên test KẾ TIẾP."
        )
        self.lbl_hint.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_DIM)
        self.btn_test = QPushButton("🔌  Kiểm tra kết nối broker")
        self.btn_test.clicked.connect(self.test_connection_requested.emit)
        self.btn_save = QPushButton("💾  Lưu cấu hình")
        self.btn_save.setObjectName("primary")
        self.btn_save.clicked.connect(self.save)
        bottom.addWidget(self.lbl_hint, 1)
        bottom.addWidget(self.btn_test)
        bottom.addWidget(self.btn_save)
        layout.addLayout(bottom)

    def _broker_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        box = QGroupBox("Broker")
        form = QFormLayout(box)
        self.fields["mqtt_host"] = QLineEdit()
        self.fields["mqtt_port"] = QSpinBox()
        self.fields["mqtt_port"].setRange(1, 65535)
        self.fields["mqtt_username"] = QLineEdit()
        self.fields["mqtt_password"] = QLineEdit()
        self.fields["mqtt_password"].setEchoMode(QLineEdit.Password)
        self.fields["mqtt_client_id"] = QLineEdit()
        self.fields["mqtt_client_id"].setPlaceholderText("Để trống → tự sinh (longrun_<timestamp>)")
        form.addRow("Host *", self.fields["mqtt_host"])
        form.addRow("Port *", self.fields["mqtt_port"])
        form.addRow("Username", self.fields["mqtt_username"])
        form.addRow("Password", self.fields["mqtt_password"])
        form.addRow("Client ID", self.fields["mqtt_client_id"])
        layout.addWidget(box)

        tls = QGroupBox("Bảo mật / kết nối")
        tls_form = QFormLayout(tls)
        self.fields["mqtt_tls"] = QCheckBox("Bật TLS/SSL")
        self.fields["mqtt_tls_insecure"] = QCheckBox("Bỏ qua kiểm tra chứng chỉ (chỉ dùng khi test)")
        self.fields["keepalive"] = QSpinBox()
        self.fields["keepalive"].setRange(5, 600)
        self.fields["keepalive"].setSuffix(" s")
        self.fields["reconnect_min_delay"] = QSpinBox()
        self.fields["reconnect_min_delay"].setRange(1, 60)
        self.fields["reconnect_min_delay"].setSuffix(" s")
        self.fields["reconnect_max_delay"] = QSpinBox()
        self.fields["reconnect_max_delay"].setRange(1, 600)
        self.fields["reconnect_max_delay"].setSuffix(" s")
        tls_form.addRow("", self.fields["mqtt_tls"])
        tls_form.addRow("", self.fields["mqtt_tls_insecure"])
        tls_form.addRow("Keepalive", self.fields["keepalive"])
        tls_form.addRow("Backoff tối thiểu", self.fields["reconnect_min_delay"])
        tls_form.addRow("Backoff tối đa", self.fields["reconnect_max_delay"])
        layout.addWidget(tls)

        topics = QGroupBox("Topic subscribe")
        topics_form = QFormLayout(topics)
        self.ed_topics = QLineEdit()
        self.ed_topics.setPlaceholderText("node/#")
        self.ed_extra_topics = QLineEdit()
        self.ed_extra_topics.setPlaceholderText("topic bổ sung, phân cách dấu phẩy")
        self.chk_log_topics = QCheckBox("Subscribe report hệ thống của gateway (<model>/<uuid>/log)")
        self.ed_log_topics = QLineEdit()
        self.ed_log_topics.setPlaceholderText("+/+/log")
        topics_form.addRow("Topic chính *", self.ed_topics)
        topics_form.addRow("Topic bổ sung", self.ed_extra_topics)
        topics_form.addRow("", self.chk_log_topics)
        topics_form.addRow("Topic log", self.ed_log_topics)
        note = QLabel(
            "Firmware hiện tại là MQTT uplink-only: tool CHỈ subscribe, không publish. "
            "`node/#` bao gồm cả `node/<uuid>/data`, `node/<uuid>/info` và `node/error`."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_DIM)
        topics_form.addRow(note)
        layout.addWidget(topics)
        layout.addStretch(1)
        return page

    def _analysis_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        seq = QGroupBox("Quy tắc SEQ")
        form = QFormLayout(seq)
        self.fields["seq_max"] = QSpinBox()
        # QSpinBox dùng int32 CÓ DẤU -> trần là 2147483647. Đặt 4294967295
        # (uint32) sẽ làm Qt ném OverflowError và cửa sổ chính không dựng được.
        # Firmware dùng uint16 (65535) nên trần này là quá đủ.
        self.fields["seq_max"].setRange(255, 2147483647)
        self.fields["seq_max"].setToolTip(
            "Giá trị SEQ lớn nhất trước khi quay vòng về 0. "
            "Firmware dùng uint16 → 65535."
        )
        self.fields["seq_reset_threshold"] = QSpinBox()
        self.fields["seq_reset_threshold"].setRange(2, 1000000)
        self.fields["seq_reset_threshold"].setToolTip(
            "Nếu SEQ nhảy lùi quá giá trị này thì coi là RESET (reboot node), "
            "ngược lại coi là OUT_OF_ORDER."
        )
        self.fields["seq_out_of_order_window"] = QSpinBox()
        self.fields["seq_out_of_order_window"].setRange(1, 1000)
        self.fields["seq_out_of_order_window"].setToolTip(
            "Số bản tin gần nhất còn nhớ để nhận ra bản tin đến muộn (out-of-order)."
        )
        form.addRow("SEQ max", self.fields["seq_max"])
        form.addRow("Ngưỡng reset", self.fields["seq_reset_threshold"])
        form.addRow("Cửa sổ out-of-order", self.fields["seq_out_of_order_window"])
        layout.addWidget(seq)

        alerts = QGroupBox("Cảnh báo")
        form2 = QFormLayout(alerts)
        self.fields["node_silent_seconds"] = QSpinBox()
        self.fields["node_silent_seconds"].setRange(30, 86400)
        self.fields["node_silent_seconds"].setSuffix(" s")
        self.fields["gateway_silent_seconds"] = QSpinBox()
        self.fields["gateway_silent_seconds"].setRange(30, 86400)
        self.fields["gateway_silent_seconds"].setSuffix(" s")
        self.fields["missing_rate_warn_percent"] = QDoubleSpinBox()
        self.fields["missing_rate_warn_percent"].setRange(0.0, 100.0)
        self.fields["missing_rate_warn_percent"].setDecimals(3)
        self.fields["missing_rate_warn_percent"].setSuffix(" %")
        self.fields["missing_rate_crit_percent"] = QDoubleSpinBox()
        self.fields["missing_rate_crit_percent"].setRange(0.0, 100.0)
        self.fields["missing_rate_crit_percent"].setDecimals(3)
        self.fields["missing_rate_crit_percent"].setSuffix(" %")
        self.fields["duplicate_rate_warn_percent"] = QDoubleSpinBox()
        self.fields["duplicate_rate_warn_percent"].setRange(0.0, 100.0)
        self.fields["duplicate_rate_warn_percent"].setDecimals(3)
        self.fields["duplicate_rate_warn_percent"].setSuffix(" %")
        self.fields["seq_reset_warn_count"] = QSpinBox()
        self.fields["seq_reset_warn_count"].setRange(1, 100000)
        self.fields["min_packets_for_rate"] = QSpinBox()
        self.fields["min_packets_for_rate"].setRange(1, 1000000)
        self.fields["min_packets_for_rate"].setToolTip(
            "Chỉ đánh giá tỉ lệ mất/trùng khi node đã gửi đủ số gói này "
            "(tránh báo động sớm vài gói đầu)."
        )
        self.fields["alert_cooldown_seconds"] = QSpinBox()
        self.fields["alert_cooldown_seconds"].setRange(0, 86400)
        self.fields["alert_cooldown_seconds"].setSuffix(" s")
        self.fields["alert_unknown_gateway"] = QCheckBox("Cảnh báo khi nhận gateway lạ")
        self.fields["alert_unknown_node_format"] = QCheckBox("Cảnh báo UID node sai định dạng")
        form2.addRow("Node im lặng quá", self.fields["node_silent_seconds"])
        form2.addRow("Gateway im lặng quá", self.fields["gateway_silent_seconds"])
        form2.addRow("Tỉ lệ mất: cảnh báo", self.fields["missing_rate_warn_percent"])
        form2.addRow("Tỉ lệ mất: nghiêm trọng", self.fields["missing_rate_crit_percent"])
        form2.addRow("Tỉ lệ trùng: cảnh báo", self.fields["duplicate_rate_warn_percent"])
        form2.addRow("Số lần reset: cảnh báo", self.fields["seq_reset_warn_count"])
        form2.addRow("Số gói tối thiểu để tính tỉ lệ", self.fields["min_packets_for_rate"])
        form2.addRow("Giãn cách lặp cảnh báo", self.fields["alert_cooldown_seconds"])
        form2.addRow("", self.fields["alert_unknown_gateway"])
        form2.addRow("", self.fields["alert_unknown_node_format"])
        layout.addWidget(alerts)
        layout.addStretch(1)
        return page

    def _ping_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        box = QGroupBox("Ping giám sát kết nối Ethernet")
        form = QFormLayout(box)

        self.cmb_interval = QComboBox()
        for choice in PING_INTERVAL_CHOICES:
            self.cmb_interval.addItem("%d giây" % choice, choice)
        self.fields["ping_timeout_ms"] = QSpinBox()
        self.fields["ping_timeout_ms"].setRange(200, 30000)
        self.fields["ping_timeout_ms"].setSuffix(" ms")
        self.fields["ping_count"] = QSpinBox()
        self.fields["ping_count"].setRange(1, 10)
        self.fields["ping_max_concurrent"] = QSpinBox()
        self.fields["ping_max_concurrent"].setRange(1, 128)
        self.fields["ping_down_threshold"] = QSpinBox()
        self.fields["ping_down_threshold"].setRange(1, 100)
        self.fields["ping_down_threshold"].setToolTip(
            "Số lần ping fail liên tiếp mới coi là DOWN thật. "
            "Đặt 2 để tránh mất 1 gói ICMP đơn lẻ gây false positive."
        )
        form.addRow("Chu kỳ ping", self.cmb_interval)
        form.addRow("Timeout mỗi lần", self.fields["ping_timeout_ms"])
        form.addRow("Số gói mỗi lần ping", self.fields["ping_count"])
        form.addRow("Số gateway ping song song", self.fields["ping_max_concurrent"])
        form.addRow("Ngưỡng xác nhận DOWN", self.fields["ping_down_threshold"])

        note = QLabel(
            "Mỗi gateway chạy một thread ping riêng → gateway này không chặn gateway khác. "
            "Kết quả ping được ghi đầy đủ (`ping_log_*.csv`) kể cả khi thất bại; "
            "các khoảng downtime được tổng hợp riêng (`gateway_downtime.csv`)."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_DIM)
        form.addRow(note)
        layout.addWidget(box)

        corr = QGroupBox("Đối chiếu MQTT ↔ ping")
        corr_form = QFormLayout(corr)
        self.fields["correlation_window_seconds"] = QSpinBox()
        self.fields["correlation_window_seconds"].setRange(5, 3600)
        self.fields["correlation_window_seconds"].setSuffix(" s")
        self.fields["correlation_window_seconds"].setToolTip(
            "Độ dài một cửa sổ phân tích. 60 s khớp với chu kỳ report hệ thống của firmware."
        )
        self.fields["silent_window_threshold"] = QSpinBox()
        self.fields["silent_window_threshold"].setRange(1, 1440)
        self.fields["silent_window_threshold"].setToolTip(
            "Số cửa sổ im lặng liên tiếp tối thiểu để tính là một sự kiện mất dữ liệu."
        )
        corr_form.addRow("Độ dài cửa sổ", self.fields["correlation_window_seconds"])
        corr_form.addRow("Số cửa sổ im lặng tối thiểu", self.fields["silent_window_threshold"])
        layout.addWidget(corr)

        # --- Node trọng tâm: bắt hụt / bắn hụt ---
        # Chỉ cấu hình NGƯỠNG ở đây; UID node được chọn ở tab "Giám sát" (vì cần
        # đổi được ngay giữa phiên và thấy số liệu realtime).
        focus = QGroupBox("Node trọng tâm (bắt hụt / bắn hụt)")
        focus_form = QFormLayout(focus)
        self.fields["focused_node_late_seconds"] = QDoubleSpinBox()
        self.fields["focused_node_late_seconds"].setRange(0.0, 3600.0)
        self.fields["focused_node_late_seconds"].setDecimals(1)
        self.fields["focused_node_late_seconds"].setSuffix(" s")
        self.fields["focused_node_late_seconds"].setToolTip(
            "Ngưỡng trễ cho 'bắn hụt': `received_at − gateway_timestamp` vượt ngưỡng\n"
            "này thì coi bản tin không kịp lên MQTT (gateway buffer SD rồi gửi bù)."
        )
        self.fields["focused_node_detail_limit"] = QSpinBox()
        self.fields["focused_node_detail_limit"].setRange(10, 5000)
        self.fields["focused_node_detail_limit"].setToolTip(
            "Số ca bắn hụt chi tiết giữ lại cho MỖI gateway trong báo cáo\n"
            "(giữ các ca trễ nặng nhất)."
        )
        focus_form.addRow("Ngưỡng bắn hụt", self.fields["focused_node_late_seconds"])
        focus_form.addRow("Số ca chi tiết / gateway", self.fields["focused_node_detail_limit"])
        focus_note = QLabel(
            "Chọn UID node cần theo dõi ở tab “Giám sát” → “Node trọng tâm”.\n"
            "BẮT HỤT = node đã phát (gateway khác nghe được) mà gateway này không publish.\n"
            "BẮN HỤT = gateway CÓ publish nhưng trễ quá ngưỡng ở trên."
        )
        focus_note.setWordWrap(True)
        focus_note.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_DIM)
        focus_form.addRow(focus_note)
        layout.addWidget(focus)
        layout.addStretch(1)
        return page

    def _storage_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        box = QGroupBox("CSV & chống mất dữ liệu")
        form = QFormLayout(box)
        self.cmb_rotation = QComboBox()
        self.cmb_rotation.addItem("Theo ngày (khuyến nghị cho test 3-4 ngày)", "daily")
        self.cmb_rotation.addItem("Theo giờ (khi lưu lượng rất cao)", "hourly")
        self.fields["flush_interval_seconds"] = QDoubleSpinBox()
        self.fields["flush_interval_seconds"].setRange(0.2, 60.0)
        self.fields["flush_interval_seconds"].setDecimals(1)
        self.fields["flush_interval_seconds"].setSuffix(" s")
        self.fields["flush_max_records"] = QSpinBox()
        self.fields["flush_max_records"].setRange(1, 100000)
        self.fields["max_queue_size"] = QSpinBox()
        self.fields["max_queue_size"].setRange(1000, 5000000)
        self.fields["max_queue_size"].setSingleStep(10000)
        self.fields["max_queue_size"].setToolTip(
            "Số record tối đa chờ ghi cho MỘT file. Đầy thì record bị bỏ và ĐẾM "
            "(không để RAM phình vô hạn)."
        )
        form.addRow("Xoay file", self.cmb_rotation)
        form.addRow("Flush định kỳ", self.fields["flush_interval_seconds"])
        form.addRow("Flush sau N record", self.fields["flush_max_records"])
        form.addRow("Kích thước queue tối đa", self.fields["max_queue_size"])
        note = QLabel(
            "Ghi bằng thread riêng, flush định kỳ (không flush từng gói). "
            "Nếu tiến trình bị tắt đột ngột, tối đa mất phần chưa flush "
            "(<= vài giây dữ liệu) — phần đã flush luôn toàn vẹn."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: %s; font-size: 10px;" % theme.COLOR_DIM)
        form.addRow(note)
        layout.addWidget(box)

        gui = QGroupBox("Giao diện")
        form2 = QFormLayout(gui)
        self.fields["ui_refresh_ms"] = QSpinBox()
        self.fields["ui_refresh_ms"].setRange(200, 10000)
        self.fields["ui_refresh_ms"].setSuffix(" ms")
        self.fields["live_table_max_rows"] = QSpinBox()
        self.fields["live_table_max_rows"].setRange(100, 200000)
        self.fields["search_page_size"] = QSpinBox()
        self.fields["search_page_size"].setRange(50, 20000)
        self.fields["ui_max_alerts_shown"] = QSpinBox()
        self.fields["ui_max_alerts_shown"].setRange(50, 10000)
        form2.addRow("Chu kỳ làm mới bảng", self.fields["ui_refresh_ms"])
        form2.addRow("Số dòng bảng live", self.fields["live_table_max_rows"])
        form2.addRow("Số dòng mỗi trang tìm kiếm", self.fields["search_page_size"])
        form2.addRow("Số cảnh báo hiển thị", self.fields["ui_max_alerts_shown"])
        layout.addWidget(gui)

        log = QGroupBox("Nhật ký tool")
        form3 = QFormLayout(log)
        self.fields["log_level"] = QComboBox()
        self.fields["log_level"].addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.fields["log_max_bytes"] = QSpinBox()
        self.fields["log_max_bytes"].setRange(1024 * 100, 1024 * 1024 * 100)
        self.fields["log_max_bytes"].setSingleStep(1024 * 1024)
        self.fields["log_backup_count"] = QSpinBox()
        self.fields["log_backup_count"].setRange(1, 50)
        form3.addRow("Mức log", self.fields["log_level"])
        form3.addRow("Kích thước tối đa mỗi file", self.fields["log_max_bytes"])
        form3.addRow("Số file log giữ lại", self.fields["log_backup_count"])
        layout.addWidget(log)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------ data
    def load_from(self, config: TestConfig) -> None:
        self.config = config
        for name, widget in self.fields.items():
            value = getattr(config, name, None)
            if isinstance(widget, QLineEdit):
                widget.setText("" if value is None else str(value))
            elif isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QComboBox):
                index = widget.findData(value)
                if index < 0:
                    index = widget.findText(str(value))
                widget.setCurrentIndex(max(0, index))
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                try:
                    widget.setValue(value if value is not None else widget.minimum())
                except (TypeError, ValueError):
                    widget.setValue(widget.minimum())
        self.ed_topics.setText(", ".join(config.subscribe_topics))
        self.ed_extra_topics.setText(", ".join(config.extra_subscribe_topics))
        self.ed_log_topics.setText(", ".join(config.log_subscribe_topics))
        self.chk_log_topics.setChecked(bool(config.subscribe_gateway_log))
        index = self.cmb_interval.findData(int(config.ping_interval_seconds))
        self.cmb_interval.setCurrentIndex(max(0, index))

    def apply_to(self, config: TestConfig) -> TestConfig:
        """Ghi giá trị từ form vào `config` (trả về chính config đó)."""
        for name, widget in self.fields.items():
            if not hasattr(config, name):
                continue
            if isinstance(widget, QLineEdit):
                setattr(config, name, widget.text().strip())
            elif isinstance(widget, QCheckBox):
                setattr(config, name, widget.isChecked())
            elif isinstance(widget, QComboBox):
                data = widget.currentData()
                setattr(config, name, data if data is not None else widget.currentText())
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                current = getattr(config, name)
                setattr(config, name, type(current)(widget.value()) if not isinstance(current, bool)
                        else widget.value())
        config.subscribe_topics = _split_topics(self.ed_topics.text()) or ["node/#"]
        config.extra_subscribe_topics = _split_topics(self.ed_extra_topics.text())
        config.log_subscribe_topics = _split_topics(self.ed_log_topics.text()) or ["+/+/log"]
        config.subscribe_gateway_log = self.chk_log_topics.isChecked()
        config.ping_interval_seconds = int(self.cmb_interval.currentData() or 10)
        return config

    def save(self) -> None:
        self.apply_to(self.config)
        self.config_changed.emit()

    def set_enabled(self, enabled: bool) -> None:
        """Khoá form khi phiên test đang chạy."""
        for widget in self.fields.values():
            widget.setEnabled(enabled)
        for widget in (self.ed_topics, self.ed_extra_topics, self.ed_log_topics,
                       self.chk_log_topics, self.cmb_interval, self.btn_save):
            widget.setEnabled(enabled)
        self.lbl_hint.setText(
            "Thay đổi được lưu vào config/config.json và áp dụng cho phiên test KẾ TIẾP."
            if enabled
            else "Đang chạy test — dừng phiên rồi mới sửa được cấu hình."
        )


def _split_topics(text: str) -> List[str]:
    out: List[str] = []
    for part in (text or "").replace(";", ",").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out
