"""Cửa sổ chính — ghép 4 tab và điều khiển vòng đời phiên test dài ngày.

Trách nhiệm của MainWindow (và CHỈ ở đây):

  - sở hữu `MonitorRunner` (bắt đầu / tạm dừng / dừng phiên);
  - lưu `config.json` + `devices.json` mỗi khi người dùng sửa;
  - nối tín hiệu giữa các tab (thiết bị đổi → cập nhật runner; phiên dừng →
    làm mới danh sách run ở tab Tìm kiếm);
  - chuyển log của Python `logging` lên khung nhật ký trong tab Giám sát.

Không có logic phân tích nào ở đây — mọi thứ nằm ở tầng `core/`, `analytics/`,
`storage/`, `runtime/` để có thể chạy headless (không GUI) khi cần.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import QObject, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QCloseEvent, QKeySequence
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QLabel,
    QMainWindow,
    QMessageBox,
    QTabWidget,
    QWidget,
)

from core.config import ConfigStore, GatewayDevice, TestConfig
from runtime.ping_monitor import targets_from_devices
from runtime.runner import (
    STATE_ERROR,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_RUNNING,
    STATE_STOPPED,
    STATE_STOPPING,
    MonitorRunner,
    RunnerState,
)

from analytics.ping_stats import latest_ping_summary_index

from . import theme
from .config_tab import ConfigTab
from .devices_tab import DevicesTab
from .history_tab import HistoryTab
from .monitor_tab import MonitorTab
from .search_tab import SearchTab

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cầu nối logging → GUI (thread-safe nhờ signal của Qt)
# ---------------------------------------------------------------------------
class LogBridge(QObject):
    """Nhận bản ghi log từ bất kỳ thread nào và phát signal trên UI thread."""

    message = pyqtSignal(str, str)   # text, level


class QtLogHandler(logging.Handler):
    """Đẩy log của Python lên khung nhật ký của GUI."""

    def __init__(self, bridge: LogBridge) -> None:
        super().__init__()
        self.bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = "[%s] %s: %s" % (
                record.levelname, record.name, self.format(record),
            )
            self.bridge.message.emit(text, record.levelname)
        except Exception:  # noqa: BLE001 - logging không được làm chết app
            pass


# ---------------------------------------------------------------------------
# Kiểm tra kết nối broker (TCP) ngoài UI thread
# ---------------------------------------------------------------------------
class BrokerCheckWorker(QThread):
    """Thử mở TCP tới broker để báo "có tới được hay không" mà không đứng GUI."""

    done = pyqtSignal(bool, str)   # ok, message

    def __init__(self, host: str, port: int, timeout: float = 5.0, parent=None) -> None:
        super().__init__(parent)
        self.host = host
        self.port = int(port)
        self.timeout = timeout

    def run(self) -> None:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout):
                self.done.emit(True, "Kết nối TCP tới %s:%d thành công." % (self.host, self.port))
        except OSError as exc:
            self.done.emit(False, "Không kết nối được %s:%d — %s" % (self.host, self.port, exc))


# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    """Cửa sổ chính của tool Test Gateway IoT."""

    STATUS_REFRESH_MS = 1000

    def __init__(
        self,
        store: ConfigStore,
        config: TestConfig,
        devices: List[GatewayDevice],
        tool_version: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.config = config
        self.devices = list(devices)
        self.tool_version = tool_version
        self.runner: Optional[MonitorRunner] = None
        self._broker_worker: Optional[BrokerCheckWorker] = None
        self._log_handler: Optional[QtLogHandler] = None
        #: Đếm chu kỳ statusbar để không cập nhật cột "Tỉ lệ ping" mỗi giây
        #: (bảng Thiết bị đọc số liệu ping của mọi gateway — không cần mịn tới 1 s).
        self._ping_refresh_tick = 0

        self.setWindowTitle(
            "Test Gateway IoT — IoT Type0 Gateway%s"
            % (" (v%s)" % tool_version if tool_version else "")
        )
        self.resize(1500, 950)
        self.setStyleSheet(theme.STYLESHEET)

        self._build_tabs()
        self._build_menu()
        self._build_statusbar()
        self._build_runner()
        self._install_log_bridge()

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(self.STATUS_REFRESH_MS)
        self._status_timer.timeout.connect(self._refresh_statusbar)
        self._status_timer.start()

    # ------------------------------------------------------------------ build
    def _build_tabs(self) -> None:
        self.tabs = QTabWidget()
        self.monitor_tab = MonitorTab(self.config, self)
        self.devices_tab = DevicesTab(self.devices, self)
        self.search_tab = SearchTab(self.config, self.store.data_dir(), self.devices, self)
        # Tab lịch sử đứng NGAY TRƯỚC tab Cấu hình: người dùng xem lại phiên cũ
        # thường xuyên hơn sửa cấu hình. `data_dir` là nguồn duy nhất của tab này
        # (đọc `meta.json` + `reports/*.csv`), không giữ trạng thái nào khác.
        self.history_tab = HistoryTab(self.store.data_dir(), self.devices, self,
                                      config=self.config)
        self.config_tab = ConfigTab(self.config, self)

        self.tabs.addTab(self.monitor_tab, "📡  Giám sát")
        self.tabs.addTab(self.devices_tab, "🛰  Thiết bị")
        self.tabs.addTab(self.search_tab, "🔍  Tìm kiếm")
        self.tabs.addTab(self.history_tab, "📜  Lịch sử phiên")
        self.tabs.addTab(self.config_tab, "⚙  Cấu hình")
        self.setCentralWidget(self.tabs)

        # --- nối tín hiệu tab Giám sát ---
        self.monitor_tab.start_requested.connect(self.start_session)
        self.monitor_tab.pause_requested.connect(self.toggle_pause)
        self.monitor_tab.stop_requested.connect(self.stop_session)
        self.monitor_tab.open_run_dir_requested.connect(self.open_run_dir)
        self.monitor_tab.export_requested.connect(self.write_reports_now)
        self.monitor_tab.focused_node_requested.connect(self.set_focused_node)

        # --- nối tín hiệu tab Thiết bị ---
        self.devices_tab.devices_changed.connect(self._on_devices_changed)
        # Chưa chạy phiên nào trong lần mở tool này → vẫn hiện tỉ lệ ping của
        # phiên GẦN NHẤT để người dùng đối chiếu ngay khi mở tool.
        self.devices_tab.set_ping_stats(self._historical_ping_summary())

        # --- nối tín hiệu tab Lịch sử phiên ---
        # Người dùng chọn 1 phiên trong lịch sử rồi bấm "Xem bản tin thô": nhảy
        # sang tab Tìm kiếm với phạm vi ĐÚNG phiên đó (không quét cả data/runs/).
        self.history_tab.open_in_search_requested.connect(self.open_run_in_search)

        # --- nối tín hiệu tab Cấu hình ---
        self.config_tab.config_changed.connect(self._on_config_changed)
        self.config_tab.test_connection_requested.connect(self.test_broker)

    def open_run_in_search(self, run_dir: Path) -> None:
        """Mở tab Tìm kiếm với phạm vi đặt sẵn = một phiên trong lịch sử."""
        self.search_tab.refresh_runs()
        self.search_tab.set_run(Path(run_dir))
        self.tabs.setCurrentWidget(self.search_tab)

    def _build_menu(self) -> None:
        bar = self.menuBar()

        m_session = bar.addMenu("&Phiên test")
        self.act_start = QAction("▶  Bắt đầu test", self)
        self.act_start.setShortcut(QKeySequence("Ctrl+R"))
        self.act_start.triggered.connect(self.start_session)
        self.act_stop = QAction("■  Dừng && ghi báo cáo", self)
        self.act_stop.setShortcut(QKeySequence("Ctrl+D"))
        self.act_stop.triggered.connect(self.stop_session)
        self.act_reports = QAction("📊  Ghi báo cáo ngay", self)
        self.act_reports.triggered.connect(self.write_reports_now)
        self.act_open = QAction("📂  Mở thư mục log của phiên", self)
        self.act_open.triggered.connect(self.open_run_dir)
        act_quit = QAction("Thoát", self)
        act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        act_quit.triggered.connect(self.close)
        for action in (self.act_start, self.act_stop, self.act_reports, self.act_open):
            m_session.addAction(action)
        m_session.addSeparator()
        m_session.addAction(act_quit)

        m_tools = bar.addMenu("&Công cụ")
        act_broker = QAction("🔌  Kiểm tra kết nối broker", self)
        act_broker.triggered.connect(self.test_broker)
        act_data = QAction("📁  Mở thư mục dữ liệu", self)
        act_data.triggered.connect(lambda: self._open_path(self.store.data_dir()))
        act_cfg = QAction("🗂  Mở thư mục cấu hình", self)
        act_cfg.triggered.connect(lambda: self._open_path(self.store.config_dir))
        m_tools.addAction(act_broker)
        m_tools.addSeparator()
        m_tools.addAction(act_data)
        m_tools.addAction(act_cfg)

        m_help = bar.addMenu("&Trợ giúp")
        act_about = QAction("Giới thiệu", self)
        act_about.triggered.connect(self._show_about)
        m_help.addAction(act_about)

    def _build_statusbar(self) -> None:
        bar = self.statusBar()
        self.lbl_sb_state = QLabel("IDLE")
        self.lbl_sb_broker = QLabel("")
        self.lbl_sb_run = QLabel("")
        self.lbl_sb_elapsed = QLabel("")
        self.lbl_sb_warn = QLabel("")
        self.lbl_sb_warn.setStyleSheet("color: %s;" % theme.COLOR_WARN)
        for widget in (self.lbl_sb_state, self.lbl_sb_broker, self.lbl_sb_elapsed):
            bar.addWidget(widget)
        bar.addWidget(self.lbl_sb_warn)
        bar.addPermanentWidget(self.lbl_sb_run)
        self.lbl_sb_broker.setText(
            "Broker: %s:%d" % (self.config.mqtt_host, self.config.mqtt_port)
        )

    def _build_runner(self) -> None:
        self.runner = MonitorRunner(
            store=self.store,
            config=self.config,
            devices=self.devices,
            tool_version=self.tool_version,
        )
        self.monitor_tab.attach(self.runner)
        # Hiện lại node trọng tâm đã lưu từ phiên trước (nếu có) để người dùng
        # không phải nhập lại UID.
        saved_focus = (
            (self.config.focused_node_uid or "").strip().upper()
            if getattr(self.config, "focused_node_enabled", False)
            else ""
        )
        self.monitor_tab.set_focused_node_display(saved_focus)

    def set_focused_node(self, node_uid: str) -> None:
        """Đặt node trọng tâm (từ tab Giám sát) — lưu config + cập nhật pipeline."""
        if self.runner is None:
            return
        try:
            self.runner.set_focused_node(node_uid)
        except Exception as exc:  # noqa: BLE001
            log.exception("Không đặt được node trọng tâm")
            QMessageBox.warning(self, "Không đặt được node trọng tâm", str(exc))
            return
        if node_uid:
            self.monitor_tab.log(
                "🎯 Theo dõi node trọng tâm %s (số liệu bắt hụt/bắn hụt bắt đầu từ giờ)"
                % node_uid,
                "OK",
            )
        else:
            self.monitor_tab.log("Đã bỏ theo dõi node trọng tâm", "INFO")

    def _install_log_bridge(self) -> None:
        self._log_bridge = LogBridge()
        self._log_bridge.message.connect(self.monitor_tab.log)
        handler = QtLogHandler(self._log_bridge)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        # Chỉ hiện log từ tầng của tool (bớt nhiễu từ thư viện)
        logging.getLogger("paho").setLevel(logging.WARNING)
        self._log_handler = handler

    # -------------------------------------------------------------- vòng đời
    def start_session(self) -> None:
        if self.runner is None or self.runner.is_running():
            return
        enabled = [d for d in self.devices if d.enabled]
        if not enabled:
            QMessageBox.warning(
                self, "Chưa có thiết bị",
                "Chưa có gateway nào được BẬT để theo dõi.\n"
                "Vào tab “Thiết bị” để thêm gateway (có thể thêm sau khi chạy).",
            )
            return
        errors = [d.validate() for d in enabled]
        bad = [(d, errs) for d, errs in zip(enabled, errors) if errs]
        if bad:
            detail = "\n".join(
                "• %s: %s" % (d.display_name(), "; ".join(errs)) for d, errs in bad
            )
            QMessageBox.warning(self, "Cấu hình thiết bị chưa hợp lệ", detail)
            return

        self.monitor_tab.reset_view()
        # Số liệu ping của phiên CŨ không được hiện lẫn vào phiên mới.
        self.devices_tab.clear_ping_stats()
        self._ping_refresh_tick = 0
        session_name = self.monitor_tab.session_name()
        try:
            session = self.runner.start(label=session_name)
        except Exception as exc:  # noqa: BLE001 - báo lỗi rõ ràng cho người dùng
            log.exception("Không bắt đầu được phiên test")
            QMessageBox.critical(self, "Không bắt đầu được", str(exc))
            return

        self.monitor_tab.log(
            "▶ Bắt đầu phiên %s — broker %s — %d gateway"
            % (session.display_name(), session.broker, len(enabled)),
            "OK",
        )
        self.monitor_tab.log("Thư mục log: %s" % session.run_dir, "INFO")
        # Dòng dữ liệu là bất đồng bộ: cảnh báo đúng ngay từ đầu để tránh hiểu nhầm.
        self.monitor_tab.log(
            "Lưu ý: bản tin sẽ xuất hiện sau khi broker gửi (có thể vài giây).", "WARN"
        )
        self._apply_state(STATE_RUNNING)
        self._on_devices_changed(self.devices)   # đẩy danh sách sang pipeline/ping
        self.config_tab.set_enabled(False)
        # Phiên mới đang chạy thật → tab Lịch sử phải biết để dán nhãn "Đang chạy"
        # cho nó, thay vì tưởng nhầm là phiên bị tắt ngang.
        self.history_tab.set_active_run_id(session.run_id)
        self.history_tab.refresh()
        self.tabs.setCurrentWidget(self.monitor_tab)

    def toggle_pause(self, pause: bool) -> None:
        if self.runner is None:
            return
        if pause:
            self.runner.pause()
            self.monitor_tab.log(
                "⏸ TẠM DỪNG: KHÔNG ghi bản tin, KHÔNG chạy ping (ICMP), "
                "đồng hồ thời lượng đứng. Vẫn giữ kết nối broker. "
                "Khoảng thời gian này KHÔNG tính vào thời lượng phiên.", "WARN"
            )
        else:
            self.runner.resume()
            self.monitor_tab.log(
                "▶ TIẾP TỤC: ghi bản tin và chạy lại ping. "
                "Thời gian đã tạm dừng không được tính vào thời lượng phiên.", "OK"
            )

    def stop_session(self) -> None:
        if self.runner is None or not self.runner.is_running():
            return
        answer = QMessageBox.question(
            self, "Dừng phiên test",
            "Dừng phiên test hiện tại?\n\n"
            "Tool sẽ xử lý nốt dữ liệu trong hàng đợi, ghi báo cáo tổng hợp "
            "(node_summary, ping_summary, correlation, alerts…) rồi đóng phiên.\n"
            "Thao tác này có thể mất vài giây.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return
        self.config_tab.set_enabled(False)
        QApplication.setOverrideCursor(theme.wait_cursor())
        try:
            self.monitor_tab.log("■ Đang dừng và ghi báo cáo…", "WARN")
            reports = self.runner.stop()
        finally:
            QApplication.restoreOverrideCursor()
        self._after_stop(reports, "")

    def write_reports_now(self) -> None:
        if self.runner is None or not self.runner.is_running():
            return
        QApplication.setOverrideCursor(theme.wait_cursor())
        try:
            reports = self.runner.write_reports_now()
        finally:
            QApplication.restoreOverrideCursor()
        if reports:
            self.monitor_tab.log(
                "📊 Đã ghi báo cáo giữa phiên: %s" % ", ".join(sorted(reports)), "OK"
            )
            if self.runner.current_run_dir() is not None:
                for name, path in sorted(reports.items()):
                    self.monitor_tab.log("   %s → %s" % (name, path), "INFO")
        else:
            self.monitor_tab.log("⚠ Chưa ghi được báo cáo nào.", "WARN")

    def _after_stop(self, reports: Dict[str, str], error: str) -> None:
        if reports:
            for name, path in sorted(reports.items()):
                self.monitor_tab.log("   %s → %s" % (name, path), "OK")
        else:
            self.monitor_tab.log("Không có báo cáo nào được ghi.", "WARN")
        self.monitor_tab.log("Phiên test đã dừng.", "INFO")
        self._apply_state(STATE_ERROR if error else STATE_STOPPED, error)
        self.config_tab.set_enabled(True)
        self.search_tab.refresh_runs()
        # Phiên vừa dừng giờ đã chốt báo cáo → nạp lại danh sách lịch sử để cột
        # "Trạng thái" / "Số báo cáo" khớp ngay, không cần người dùng bấm Làm mới.
        self.history_tab.set_active_run_id("")
        self.history_tab.refresh()
        # Phiên vừa dừng đã ghi `reports/ping_summary.csv` → đọc lại để cột
        # "Tỉ lệ ping" trong tab Thiết bị khớp đúng số liệu đã chốt.
        self.devices_tab.set_ping_stats(self._historical_ping_summary())

    # ------------------------------------------------------- sự kiện cấu hình
    def _on_devices_changed(self, devices: List[GatewayDevice]) -> None:
        self.devices = list(devices)
        # Tab Tìm kiếm đọc lại CSV (kể cả CSV của phiên bản cũ) nên cần danh sách
        # thiết bị MỚI NHẤT để gắn tên gateway cho kết quả tìm kiếm. Tab Lịch sử
        # cũng cần để hiện TÊN của gateway trong các phiên cũ.
        self.search_tab.set_devices(self.devices)
        self.history_tab.set_devices(self.devices)
        try:
            self.store.save_devices(self.devices)
        except Exception:  # noqa: BLE001
            log.exception("Không lưu được devices.json")
        if self.runner is not None:
            enabled = [d for d in self.devices if d.enabled]
            if self.runner.pipeline is not None:
                self.runner.pipeline.update_devices(enabled)
            if self.runner.pinger is not None:
                self.runner.pinger.update_targets(targets_from_devices(enabled))
            self.runner.devices = enabled
        self.monitor_tab.log(
            "Danh sách thiết bị đã cập nhật (%d gateway)." % len(self.devices), "INFO"
        )

    def _on_config_changed(self) -> None:
        try:
            self.store.save_config(self.config)
        except Exception:  # noqa: BLE001
            log.exception("Không lưu được config.json")
            QMessageBox.warning(self, "Lỗi lưu cấu hình",
                                "Không ghi được config/config.json. Xem nhật ký.")
            return
        self.lbl_sb_broker.setText(
            "Broker: %s:%d" % (self.config.mqtt_host, self.config.mqtt_port)
        )
        self.monitor_tab.log("💾 Đã lưu config/config.json", "OK")
        if self.runner is not None and self.runner.is_running():
            self.monitor_tab.log(
                "Cấu hình mới sẽ áp dụng cho phiên KẾ TIẾP "
                "(phiên đang chạy giữ nguyên tham số để không sai thống kê).", "WARN"
            )

    def test_broker(self) -> None:
        if self._broker_worker is not None and self._broker_worker.isRunning():
            return
        self.monitor_tab.log(
            "🔌 Đang kiểm tra %s:%d …" % (self.config.mqtt_host, self.config.mqtt_port), "INFO"
        )
        worker = BrokerCheckWorker(
            self.config.mqtt_host, self.config.mqtt_port, timeout=5.0, parent=self
        )
        worker.done.connect(self._on_broker_checked)
        worker.finished.connect(lambda: setattr(self, "_broker_worker", None))
        self._broker_worker = worker
        worker.start()

    def _on_broker_checked(self, ok: bool, message: str) -> None:
        self.monitor_tab.log(message, "OK" if ok else "ERROR")
        if ok:
            QMessageBox.information(self, "Broker OK", message)
        else:
            QMessageBox.warning(self, "Không kết nối được broker", message)

    # ----------------------------------------------------------- trạng thái
    def _apply_state(self, state: str, error: str = "") -> None:
        color = theme.STATE_COLORS.get(state, theme.COLOR_DIM)
        self.lbl_sb_state.setText(state)
        self.lbl_sb_state.setStyleSheet("color: %s; font-weight: bold;" % color)
        self.lbl_sb_warn.setText(error or "")
        running = state in (STATE_RUNNING, STATE_PAUSED)
        self.act_start.setEnabled(not running)
        self.act_stop.setEnabled(running)
        self.act_reports.setEnabled(running)

    def _refresh_statusbar(self) -> None:
        if self.runner is None:
            return
        snapshot: RunnerState = self.runner.snapshot()
        self._apply_state(snapshot.state, snapshot.error)
        if snapshot.run_dir:
            self.lbl_sb_run.setText(Path(snapshot.run_dir).name)
        stats = self.runner.stats()
        session = stats.get("session") or {}
        if session:
            elapsed = session.get("elapsed_seconds")
            text = "Đã chạy: %s" % theme.human_duration(elapsed)
            if session.get("paused"):
                # Đồng hồ đang ĐỨNG vì phiên tạm dừng — nói rõ để người dùng không
                # tưởng tool bị treo, và cho biết đã nghỉ bao lâu.
                paused_seconds = session.get("paused_seconds") or 0
                text += "  ⏸ (đang tạm dừng, đã nghỉ %s)" % theme.human_duration(
                    paused_seconds
                )
            self.lbl_sb_elapsed.setText(text)
        pipeline = stats.get("pipeline") or {}
        counters = pipeline.get("counters") or {}
        total = counters.get("total_messages")
        if total is not None and self.runner.is_running():
            self.lbl_sb_warn.setText(
                "đã nhận %s bản tin • %s/s"
                % (theme.human_count(total),
                   theme.human_count(pipeline.get("throughput_per_second", 0)))
            )
        self._refresh_devices_ping()

    # ------------------------------------------------- ping rate (tab Thiết bị)
    def _historical_ping_summary(self) -> Dict[str, Any]:
        """Index `ping_summary.csv` của phiên gần nhất (rỗng nếu chưa có phiên)."""
        try:
            return latest_ping_summary_index(self.store.data_dir())
        except Exception:  # noqa: BLE001 - thiếu/hỏng CSV không được làm chết GUI
            log.exception("Không đọc được ping_summary.csv của phiên gần nhất")
            return {}

    def _refresh_devices_ping(self, force: bool = False) -> None:
        """Đẩy tỉ lệ ping vào tab Thiết bị.

        Đang chạy → lấy trực tiếp `PingTracker` trong RAM (số liệu sống). Chưa
        chạy → giữ số liệu của `ping_summary.csv` phiên gần nhất đã nạp lúc khởi
        động. Giới hạn 5 s/lần vì cột này chỉ để nhìn xu hướng.
        """
        self._ping_refresh_tick += 1
        if not force and self._ping_refresh_tick % 5 != 0:
            return
        if self.runner is None or not self.runner.is_running():
            return
        pipeline = getattr(self.runner, "pipeline", None)
        tracker = getattr(pipeline, "ping_tracker", None)
        if tracker is None:
            return
        self.devices_tab.set_ping_stats(tracker)

    # --------------------------------------------------------------- tiện ích
    def open_run_dir(self) -> None:
        if self.runner is None:
            return
        directory = self.runner.current_run_dir()
        if directory is None:
            latest = self.store.data_dir()
            if not latest.exists():
                QMessageBox.information(self, "Chưa có dữ liệu", "Chưa có phiên chạy nào.")
                return
            directory = latest
        self._open_path(directory)

    def _open_path(self, path: Path) -> None:
        path = Path(path)
        if not path.exists():
            QMessageBox.information(self, "Không tìm thấy", "Không tồn tại: %s" % path)
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system('open "%s"' % path)
            else:
                os.system('xdg-open "%s"' % path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Không mở được", str(exc))

    def _show_about(self) -> None:
        QMessageBox.information(
            self,
            "Giới thiệu",
            "Test Gateway IoT — theo dõi MQTT gateway IoT Type0 trong 3-4 ngày.\n\n"
            "• Dữ liệu thô được ghi CSV liên tục, có thể tra cứu lại sau.\n"
            "• Phân tích SEQ (uint16) để phát hiện mất gói / trùng / sai thứ tự.\n"
            "• Ping gateway song song để tách lỗi mạng khỏi lỗi thiết bị.\n"
            "• Báo cáo tổng hợp khi dừng phiên.\n\n"
            "Phiên bản tool: %s" % (self.tool_version or "?"),
        )

    # ------------------------------------------------------------- kết thúc
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.runner is not None and self.runner.is_running():
            answer = QMessageBox.question(
                self, "Thoát khi đang chạy",
                "Phiên test đang chạy. Dừng phiên, ghi báo cáo rồi thoát?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes,
            )
            if answer == QMessageBox.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.Yes:
                QApplication.setOverrideCursor(theme.wait_cursor())
                try:
                    reports = self.runner.stop("Đóng tool khi đang chạy")
                finally:
                    QApplication.restoreOverrideCursor()
                self._after_stop(reports, "")
        self._status_timer.stop()
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None
        event.accept()
