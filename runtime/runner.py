"""Điều phối vòng đời một phiên test dài ngày.

`MonitorRunner` là tầng duy nhất mà GUI cần gọi:

```
start()  → tạo RunSession + LogManager + pipeline + MQTT + ping, tất cả chạy nền
pause()  → ngừng nhận dữ liệu mới nhưng KHÔNG kết thúc phiên (giữ kết nối)
resume() → nhận lại
stop()   → dừng sạch: drain queue, đóng downtime, ghi reports, cập nhật meta.json
```

Điểm quan trọng cho test 3-4 ngày:
  - `stop()` phải **luôn** ghi được `meta.json` + reports, kể cả khi 1 phần lỗi.
  - Có thread heartbeat ghi `meta.json` mỗi 60 s → nếu PC/mất điện đột ngột thì
    vẫn biết phiên chạy tới đâu (và `status` còn `RUNNING` = bị tắt đột ngột).
  - Xử lý SIGINT để Ctrl+C (chạy headless) thoát sạch.

Cầu nối tới GUI (đều là callback; module này KHÔNG import Qt):
  - `subscribe_records(handler)` → handler(row_dict) cho từng bản tin (bảng live)
  - `subscribe_alerts(handler)`  → handler(row_dict) cho từng cảnh báo
  - `subscribe_events(handler)`  → handler(event, message) cho connect/reconnect/error
  - `subscribe_state(handler)`   → handler(RunnerState) khi trạng thái đổi
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from analytics.anomaly_detector import AlertRecord
from analytics.focused_node import normalize_focus_node
from analytics.gateway_result import scoring_params_snapshot
from core.config import ConfigStore, GatewayDevice, TestConfig
from core.models import MQTTRecord, PingRecord
from core.pipeline import MonitorPipeline
from storage.csv_writer import LogManager
from storage.session import (
    STATUS_CRASHED,
    STATUS_RUNNING,
    STATUS_STOPPED,
    RunSession,
    create_session,
    snapshot_gateway_names,
)

from .mqtt_subscriber import MqttSubscriber
from .ping_monitor import PingMonitor, targets_from_devices

log = logging.getLogger(__name__)

STATE_IDLE = "IDLE"
STATE_RUNNING = "RUNNING"
STATE_PAUSED = "PAUSED"
STATE_STOPPING = "STOPPING"
STATE_STOPPED = "STOPPED"
STATE_ERROR = "ERROR"


@dataclass
class RunnerState:
    state: str = STATE_IDLE
    started_at: str = ""
    error: str = ""
    run_dir: str = ""
    reports: Dict[str, str] = field(default_factory=dict)


class MonitorRunner:
    """Bọc toàn bộ vòng đời: MQTT + ping + pipeline + session + reports."""

    HEARTBEAT_SECONDS = 60.0

    def __init__(
        self,
        store: ConfigStore,
        config: TestConfig,
        devices: List[GatewayDevice],
        tool_version: str = "",
        on_alert: Optional[Callable[[AlertRecord], None]] = None,
        on_record: Optional[Callable[[MQTTRecord], None]] = None,
        on_state: Optional[Callable[[RunnerState], None]] = None,
    ) -> None:
        self.store = store
        self.config = config
        self.devices = list(devices)
        self.tool_version = tool_version
        self.on_alert = on_alert
        self.on_record = on_record
        self.on_state = on_state

        self.session: Optional[RunSession] = None
        self.logs: Optional[LogManager] = None
        self.pipeline: Optional[MonitorPipeline] = None
        self.mqtt: Optional[MqttSubscriber] = None
        self.pinger: Optional[PingMonitor] = None

        self._state = RunnerState()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._pump_thread: Optional[threading.Thread] = None
        self._consumers: List[Callable[[str, str], None]] = []
        self._record_consumers: List[Callable[[Dict[str, Any]], None]] = []
        self._alert_consumers: List[Callable[[Dict[str, Any]], None]] = []
        self._state_consumers: List[Callable[[RunnerState], None]] = []
        self._reconnects_at_stop = 0

    # ------------------------------------------------------------------ API
    @property
    def state(self) -> str:
        return self._state.state

    def is_running(self) -> bool:
        return self._state.state in (STATE_RUNNING, STATE_PAUSED)

    def snapshot(self) -> RunnerState:
        with self._lock:
            return RunnerState(
                state=self._state.state,
                started_at=self._state.started_at,
                error=self._state.error,
                run_dir=self._state.run_dir,
                reports=dict(self._state.reports),
            )

    def subscribe_events(self, handler: Callable[[str, str], None]) -> None:
        """Đăng ký nhận sự kiện (connect/reconnect/error) để hiển thị lên GUI."""
        if handler not in self._consumers:
            self._consumers.append(handler)

    def subscribe_records(self, handler: Callable[[Dict[str, Any]], None]) -> None:
        """Nhận từng dòng CSV của bản tin mới. Callback chạy trên thread phân tích
        → chỉ được đẩy vào buffer, KHÔNG chạm widget Qt."""
        if handler not in self._record_consumers:
            self._record_consumers.append(handler)

    def subscribe_alerts(self, handler: Callable[[Dict[str, Any]], None]) -> None:
        if handler not in self._alert_consumers:
            self._alert_consumers.append(handler)

    def subscribe_state(self, handler: Callable[[RunnerState], None]) -> None:
        if handler not in self._state_consumers:
            self._state_consumers.append(handler)

    def start(self, label: str = "") -> RunSession:
        """Bắt đầu phiên mới. `label` là TÊN PHIÊN người dùng đặt (không bắt buộc).

        """
        if self.is_running():
            raise RuntimeError("Phiên test đang chạy")
        self._stop_event.clear()
        self._reconnects_at_stop = 0

        enabled = [d for d in self.devices if getattr(d, "enabled", True)]
        gateway_uids = [d.uid.upper() for d in enabled if d.uid]
        topics = self.config.effective_subscribe_topics()
        broker = "%s:%d" % (self.config.mqtt_host, self.config.mqtt_port)

        data_dir = self.store.data_dir()
        self.session = create_session(
            data_dir,
            gateway_uids=gateway_uids,
            broker=broker,
            subscribed_topics=topics,
            planned_hours=None,
            tool_version=self.tool_version,
            # Chốt ẢNH CHỤP tên gateway ngay lúc bắt đầu phiên. Nhờ vậy việc xoá
            # gateway (hoặc đổi sang gateway khác) trong tab Thiết bị SAU ĐÓ
            # không làm đổi tên gateway của phiên trong "Lịch sử phiên".
            # Ảnh chụp gồm cả gateway đang TẮT (`enabled = False`) để phần
            # "Gateway của phiên" không mất tên của các gateway đã cấu hình.
            gateway_names=snapshot_gateway_names(self.devices),
            # Chốt NGƯỠNG chấm ĐẠT/KHÔNG ĐẠT đang dùng. Nhờ vậy khi CHẤM LẠI
            # phiên này (nút "⚖ Chấm lại kết quả" ở tab Lịch sử phiên), kết quả
            # vẫn theo đúng ngưỡng của lúc chạy dù `config.json` đã bị sửa.
            scoring_params=scoring_params_snapshot(self.config),
            # TÊN PHIÊN người dùng đặt (rỗng = dùng `run_id`). Ghi vào meta.json nên
            # đổi được về sau mà không ảnh hưởng dữ liệu của phiên.
            label=label,
        )
        self.logs = LogManager(
            self.session.run_dir,
            rotation=self.config.rotation,
            flush_interval=self.config.flush_interval_seconds,
            flush_max_records=self.config.flush_max_records,
            max_queue_size=self.config.max_queue_size,
        )
        self.logs.start()

        self.pipeline = MonitorPipeline(
            self.config,
            enabled,
            self.logs,
            run_id=self.session.run_id,
            on_alert=self._on_alert_internal,
            on_record=self._on_record_internal,
        )
        self.pipeline.start()

        self.mqtt = MqttSubscriber(
            host=self.config.mqtt_host,
            port=self.config.mqtt_port,
            client_id=self.config.mqtt_client_id,
            username=self.config.mqtt_username,
            password=self.config.mqtt_password,
            use_tls=self.config.mqtt_tls,
            tls_insecure=self.config.mqtt_tls_insecure,
            keepalive=self.config.keepalive,
            topics=topics,
            queue_max=self.config.max_queue_size,
            reconnect_min=self.config.reconnect_min_delay,
            reconnect_max=self.config.reconnect_max_delay,
        )
        self.mqtt.on_event = self._on_broker_event
        self.mqtt.start()

        self.pinger = PingMonitor(
            targets_from_devices(enabled),
            interval_seconds=self.config.ping_interval_seconds,
            timeout_ms=self.config.ping_timeout_ms,
            count=self.config.ping_count,
            max_concurrent=self.config.ping_max_concurrent,
            on_result=self._on_ping_result,
        )
        self.pinger.start()

        self._pump_thread = threading.Thread(
            target=self._pump_loop, name="mqtt-pump", daemon=True
        )
        self._pump_thread.start()

        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="heartbeat", daemon=True
        )
        self._heartbeat_thread.start()

        self._install_signal_handlers()
        self._set_state(STATE_RUNNING, started_at=self.session.started_at,
                       run_dir=str(self.session.run_dir))
        log.info("Phiên test bắt đầu: %s (broker %s, %d gateway)",
                 self.session.run_dir, broker, len(enabled))
        return self.session

    def pause(self) -> None:
        if self._state.state != STATE_RUNNING:
            return
        self._set_state(STATE_PAUSED)
        log.info("Tạm dừng ghi dữ liệu (vẫn giữ kết nối broker)")

    def resume(self) -> None:
        if self._state.state != STATE_PAUSED:
            return
        self._set_state(STATE_RUNNING)
        log.info("Tiếp tục ghi dữ liệu")

    def stop(self, reason: str = "Người dùng dừng") -> Dict[str, str]:
        """Dừng sạch và ghi báo cáo. Idempotent — gọi nhiều lần vẫn an toàn."""
        with self._lock:
            if self._state.state in (STATE_STOPPING, STATE_STOPPED):
                return dict(self._state.reports)
            self._state.state = STATE_STOPPING
        self._stop_event.set()

        # 1) Ngừng nguồn dữ liệu trước để không sinh thêm record
        if self.mqtt is not None:
            try:
                self._reconnects_at_stop = self.mqtt.stats.reconnects
                self.mqtt.stop()
            except Exception:  # noqa: BLE001
                log.exception("Lỗi khi dừng MQTT subscriber")
        if self.pinger is not None:
            try:
                self.pinger.stop()
            except Exception:  # noqa: BLE001
                log.exception("Lỗi khi dừng ping monitor")

        # 2) Đợi pump lấy hết message còn trong queue rồi dừng pipeline
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=15.0)
            self._pump_thread = None
        if self.pipeline is not None:
            try:
                self.pipeline.stop()
            except Exception:  # noqa: BLE001
                log.exception("Lỗi khi dừng pipeline")

        # 3) Flush CSV TRƯỚC khi ghi báo cáo: write_reports() đọc lại chính các
        #    file mqtt_data_*.csv / ping_log_*.csv từ đĩa, nên nếu còn dòng nằm
        #    trong queue của sink thì báo cáo sẽ thiếu phần đuôi phiên.
        #    (Bản giữa phiên write_reports_now() đã làm đúng thứ tự này.)
        if self.logs is not None:
            try:
                self.logs.flush_all()
            except Exception:  # noqa: BLE001
                log.exception("Lỗi khi flush CSV sink trước khi ghi báo cáo")

        # 4) Ghi báo cáo (write_single_csv ghi trực tiếp vào reports/)
        reports: Dict[str, str] = {}
        if self.pipeline is not None and self.logs is not None:
            try:
                written = self.pipeline.write_reports(
                    self.session, extra={"reconnects": self._reconnects_at_stop}
                )
                reports = {name: str(path) for name, path in written.items()}
            except Exception as exc:  # noqa: BLE001 - vẫn phải dừng sạch
                log.exception("Lỗi khi ghi báo cáo")
                self._state.error = "Ghi báo cáo lỗi: %s" % exc

        # 5) Đóng tất cả sink
        if self.logs is not None:
            try:
                self.logs.stop()
            except Exception:  # noqa: BLE001
                log.exception("Lỗi khi đóng CSV sink")

        # 6) Cập nhật meta.json — luôn ghi, kể cả khi bước trên lỗi
        if self.session is not None:
            try:
                self.session.finish(STATUS_STOPPED, reason)
            except Exception:  # noqa: BLE001
                log.exception("Lỗi khi ghi meta.json")

        self._set_state(STATE_STOPPED, reports=reports)
        log.info("Phiên test đã dừng. Báo cáo: %s", ", ".join(reports) or "(không có)")
        return reports

    def stats(self) -> Dict[str, Any]:
        """Snapshot số liệu cho GUI (không ném exception)."""
        out: Dict[str, Any] = {"state": self._state.state, "error": self._state.error}
        try:
            if self.pipeline is not None:
                mqtt_stats = vars(self.mqtt.stats) if self.mqtt is not None else {}
                ping_stats = self.pinger.stats() if self.pinger is not None else {}
                stats = self.pipeline.stats(mqtt_stats, ping_stats)
                out["pipeline"] = vars(stats)
            if self.session is not None:
                out["session"] = {
                    "run_id": self.session.run_id,
                    "run_dir": str(self.session.run_dir),
                    "started_at": self.session.started_at,
                    "elapsed_seconds": self.session.elapsed_seconds(),
                    "planned_hours": self.session.planned_hours,
                    "gateways_monitored": self.session.gateways_monitored,
                    "broker": self.session.broker,
                }
                if self.logs is not None:
                    out["csv"] = self.logs.all_stats()
        except Exception as exc:  # noqa: BLE001 - GUI không được crash vì stats
            out["stats_error"] = str(exc)
        return out

    def write_reports_now(self) -> Dict[str, str]:
        """Ghi lại báo cáo GIỮA phiên mà không dừng test.

        Hữu ích cho test 3-4 ngày: nếu PC có nguy cơ tắt, người dùng bấm
        'Ghi báo cáo ngay' để chốt số liệu hiện có.
        """
        if self.pipeline is None or self.logs is None:
            return {}
        try:
            self.logs.flush_all()
            written = self.pipeline.write_reports(
                self.session,
                extra={"reconnects": self.mqtt.stats.reconnects if self.mqtt else 0},
            )
            out = {name: str(path) for name, path in written.items()}
            log.info("Đã ghi báo cáo giữa phiên: %s", ", ".join(out))
            return out
        except Exception:  # noqa: BLE001
            log.exception("Lỗi ghi báo cáo giữa phiên")
            return {}

    def current_run_dir(self) -> Optional[Path]:
        return self.session.run_dir if self.session is not None else None

    def set_focused_node(self, node_uid: str) -> bool:
        """Đặt/đổi node trọng tâm (gọi từ GUI thread — tracker có lock riêng).

        Gọi được cả khi chưa chạy phiên (ghi vào config để phiên sau dùng), nhưng
        khi đó chỉ lưu; số liệu chỉ bắt đầu được thu khi phiên đang chạy.
        """
        normalized = normalize_focus_node(node_uid)
        self.config.focused_node_uid = normalized
        self.config.focused_node_enabled = bool(normalized)
        try:
            self.store.save_config(self.config)
        except Exception:  # noqa: BLE001 - lưu thất bại không được làm hỏng phiên
            log.exception("Không lưu được node trọng tâm vào config.json")
        if self.pipeline is not None:
            return self.pipeline.set_focused_node(normalized)
        return True

    def focused_node_rows(self) -> List[Dict[str, Any]]:
        """Bảng bắt hụt / bắn hụt hiện tại (rỗng nếu không theo dõi node nào)."""
        if self.pipeline is None:
            return []
        return self.pipeline.focused_node_rows()

    def focused_node_stats(self) -> Dict[str, Any]:
        if self.pipeline is None:
            return {}
        return self.pipeline.focused_node_stats()

    def focused_node_missing_rows(self) -> List[Dict[str, Any]]:
        if self.pipeline is None:
            return []
        return self.pipeline.focused_node_missing_rows()

    def refresh_focused_patterns(self) -> None:
        """Cập nhật kiểu ngắt quãng (liên tục vs rải rác) cho bảng node trọng tâm.

        Quét bitmap tốn ~ms/gateway nên chỉ gọi thưa (vài chục giây/lần từ GUI),
        không gọi mỗi chu kỳ refresh.
        """
        if self.pipeline is None:
            return
        try:
            self.pipeline.focused_node.refresh_patterns()
        except Exception:  # noqa: BLE001 - không được làm chết GUI
            log.debug("refresh_patterns lỗi", exc_info=True)

    def focused_node_late_rows(self) -> List[Dict[str, Any]]:
        if self.pipeline is None:
            return []
        return self.pipeline.focused_node_late_rows()

    # ------------------------------------------------------------- nội bộ
    def _pump_loop(self) -> None:
        """Chuyển message thô từ MQTT queue → pipeline (bỏ qua khi PAUSED).

        Đây là thread DUY NHẤT gọi `pipeline.submit_raw` → không cần lock.
        Khi PAUSED ta vẫn LẤY message ra khỏi queue (nếu không queue sẽ đầy
        và MQTT bắt đầu drop) nhưng KHÔNG ghi — ghi rõ vào counter drop để
        không âm thầm mất dữ liệu.
        """
        assert self.mqtt is not None and self.pipeline is not None
        paused_logged = False
        while not self._stop_event.is_set():
            item = self.mqtt.get(timeout=0.5)
            if item is None:
                continue
            paused_logged = self._forward(item, paused_logged)

        # Lấy nốt phần còn lại trước khi pipeline dừng (nếu không phải PAUSED)
        for item in self.mqtt.drain(max_items=200000):
            paused_logged = self._forward(item, paused_logged)

    def _forward(self, item: Any, paused_logged: bool = False) -> bool:
        """Đẩy 1 message vào pipeline. Khi PAUSED: không ghi, nhưng ĐẾM là drop.

        Vẫn phải lấy message ra khỏi queue MQTT, nếu không queue đầy và paho sẽ
        bắt đầu bỏ message ở tầng network (ít kiểm soát hơn).
        """
        if self.pipeline is None:
            return paused_logged
        if self._state.state == STATE_PAUSED:
            if not paused_logged:
                log.warning(
                    "Đang TẠM DỪNG — bản tin vẫn lấy khỏi queue nhưng KHÔNG ghi CSV"
                )
            self.pipeline.counters.queue_dropped += 1
            return True
        received_at, topic, payload = item
        self.pipeline.submit_raw(received_at, topic, payload)
        return False

    def _on_ping_result(self, record: PingRecord) -> None:
        if self._state.state == STATE_PAUSED or self.pipeline is None:
            return
        self.pipeline.submit_ping(record)

    def _on_record_internal(self, record: MQTTRecord) -> None:
        """Chạy trên thread phân tích → fan-out row dict cho consumer (GUI).

        Không bao giờ để callback của consumer làm gãy pipeline.
        """
        if not self._record_consumers:
            return
        try:
            row = record.to_row()
        except Exception:  # noqa: BLE001
            return
        for handler in list(self._record_consumers):
            try:
                handler(row)
            except Exception:  # noqa: BLE001
                log.debug("record consumer lỗi", exc_info=True)

    def _on_alert_internal(self, alert: AlertRecord) -> None:
        if not self._alert_consumers:
            return
        try:
            row = alert.to_row()
        except Exception:  # noqa: BLE001
            return
        for handler in list(self._alert_consumers):
            try:
                handler(row)
            except Exception:  # noqa: BLE001
                log.debug("alert consumer lỗi", exc_info=True)

    def _on_broker_event(self, event: str, message: str) -> None:
        for handler in list(self._consumers):
            try:
                handler(event, message)
            except Exception:  # noqa: BLE001
                log.debug("consumer lỗi", exc_info=True)

    def _heartbeat_loop(self) -> None:
        """Ghi meta.json định kỳ để không mất dấu vết nếu PC tắt đột ngột."""
        while not self._stop_event.wait(self.HEARTBEAT_SECONDS):
            session = self.session
            if session is None:
                continue
            try:
                session.save()
            except Exception:  # noqa: BLE001
                log.debug("Heartbeat save lỗi", exc_info=True)

    def _set_state(self, state: str, **fields: Any) -> None:
        with self._lock:
            self._state.state = state
            for key, value in fields.items():
                if hasattr(self._state, key):
                    setattr(self._state, key, value)
            snapshot = RunnerState(
                state=self._state.state,
                started_at=self._state.started_at,
                error=self._state.error,
                run_dir=self._state.run_dir,
                reports=dict(self._state.reports),
            )
        for handler in list(self._state_consumers):
            try:
                handler(snapshot)
            except Exception:  # noqa: BLE001
                log.debug("state consumer lỗi", exc_info=True)
        consumer = self.on_state
        if consumer is not None:
            try:
                consumer(snapshot)
            except Exception:  # noqa: BLE001
                log.debug("on_state lỗi", exc_info=True)

    def _install_signal_handlers(self) -> None:
        """Bắt SIGINT/SIGTERM để chạy headless vẫn dừng sạch (Ctrl+C)."""
        def handler(signum, frame):  # noqa: ARG001
            log.warning("Nhận tín hiệu %s → dừng phiên test…", signum)
            threading.Thread(target=self.stop, args=("Tín hiệu hệ thống",), daemon=True).start()

        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                # Không ở main thread (ví dụ chạy trong GUI) → bỏ qua an toàn.
                pass

