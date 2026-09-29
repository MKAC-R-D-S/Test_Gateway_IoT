"""Pipeline trung tâm: MQTT → parse → SEQ/phân tích → CSV, + ping, + cảnh báo.

Kiến trúc luồng (quan trọng cho test 3-4 ngày không giám sát):

```
[paho network thread]                (KHÔNG được block)
        │  on_message: chỉ put (received_at, topic, payload) vào queue
        ▼
   raw message queue
        │
        ▼
[analyse thread]  ← 1 thread duy nhất, sở hữu toàn bộ state phân tích
        │  parse → classify → SequenceTracker.feed() → AnomalyDetector.feed()
        │  → ghi ra CSV bằng cách enqueue (non-blocking)
        ▼
   CsvSink queues  →  [writer threads] → disk
```

Vì sao 1 analyse thread + nhiều writer thread:
  - **Không cần lock** cho state SEQ (chỉ 1 thread chạm vào).
  - CSV ghi bằng thread riêng → nếu disk chậm thì analyse thread không bị chặn,
    và queue có back-pressure rõ ràng (đếm `dropped`) thay vì RAM phình vô hạn.
  - Ping monitor là thread riêng, gửi kết quả vào cùng analyse thread qua queue
    (giữ đúng nguyên tắc "một người sở hữu state").

Tool KHÔNG kiểm CRC: firmware đã kiểm trước khi publish (docs §8).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from analytics.anomaly_detector import AlertRecord, AlertRule, AnomalyDetector
from analytics.error_deduper import ErrorFrameCoalescer, FrameCopy, frame_key
from analytics.focused_node import FocusedNodeTracker, normalize_focus_node
from analytics.packet_deduper import (
    PacketCopy,
    PacketDeduper,
    parse_stamp,
)
from analytics.ping_stats import PingTracker
from analytics.sequence_tracker import SequenceTracker
from core.config import GatewayDevice, TestConfig
from core.models import (
    AN_INVALID_JSON,
    AN_UNKNOWN_GATEWAY,
    AN_UNKNOWN_NODE_FORMAT,
    MQTTRecord,
    PKT_DATA,
    PKT_DATA_NO_SEQ,
    PKT_ERROR,
    PKT_INFO,
    PKT_LOG,
    PKT_UNKNOWN,
    PingRecord,
    V_INVALID_JSON,
    V_NOT_OBJECT,
)
from core.parser import MessageParser
from core.uid import NODE_FORMAT_UNKNOWN, gateway_name_index, normalize_uid
from storage.csv_writer import LogManager

log = logging.getLogger(__name__)

# Sự kiện đẩy vào queue nội bộ
EV_MESSAGE = "message"
EV_PING = "ping"

# Chu kỳ tối thiểu giữa hai lần quét bộ gộp bản tin (giây). Quét mỗi message
# cũng đúng nhưng tốn vô ích; 0.5 s vẫn nhỏ hơn nhiều so với cửa sổ gộp 2 s nên
# độ trễ ghi CSV không đáng kể.
MERGE_FLUSH_INTERVAL_SECONDS = 0.5


@dataclass
class PipelineCounters:
    """Bộ đếm tổng hợp — nguồn dữ liệu cho run_summary + GUI."""

    total_messages: int = 0
    valid_messages: int = 0
    invalid_messages: int = 0
    data_packets: int = 0
    data_no_seq_packets: int = 0
    info_packets: int = 0
    error_packets: int = 0
    log_messages: int = 0
    unknown_messages: int = 0
    unknown_gateway_messages: int = 0
    unknown_format_messages: int = 0
    missing_packets: int = 0
    duplicate_packets: int = 0
    out_of_order_packets: int = 0
    seq_reset_events: int = 0
    seq_wrap_events: int = 0
    parse_errors: int = 0
    queue_dropped: int = 0
    started_at: str = ""
    last_message_at: str = ""
    bytes_received: int = 0

    def as_summary_fields(self) -> Dict[str, Any]:
        return {
            "total_messages": self.total_messages,
            "valid_messages": self.valid_messages,
            "invalid_messages": self.invalid_messages,
            "data_packets": self.data_packets,
            "data_no_seq_packets": self.data_no_seq_packets,
            "info_packets": self.info_packets,
            "error_packets": self.error_packets,
            "log_messages": self.log_messages,
            "unknown_gateway_messages": self.unknown_gateway_messages,
            "missing_packets": self.missing_packets,
            "duplicate_packets": self.duplicate_packets,
            "out_of_order_packets": self.out_of_order_packets,
            "seq_reset_events": self.seq_reset_events,
            "seq_wrap_events": self.seq_wrap_events,
            # PHẢI xuất ra báo cáo: `parse_errors > 0` nghĩa là có bản tin MQTT đã
            # nhận nhưng bị mất do lỗi khi xử lý (xem `_run`/`_drain_remaining`).
            # Trước đây bộ đếm này chỉ nằm trong RAM nên lỗi kiểu
            # `observe_coverage()` sai chữ ký làm mất bản tin mà KHÔNG báo cáo nào
            # cho thấy — người vận hành tưởng phiên chạy sạch.
            "parse_errors": self.parse_errors,
            "queue_dropped": self.queue_dropped,
        }


@dataclass
class PipelineStats:
    """Snapshot trạng thái để GUI hiển thị (thread-safe read)."""

    counters: Dict[str, Any] = field(default_factory=dict)
    seq_nodes: int = 0
    queue_size: int = 0
    queue_max: int = 0
    mqtt: Dict[str, Any] = field(default_factory=dict)
    ping: Dict[str, Any] = field(default_factory=dict)
    alerts_by_level: Dict[str, int] = field(default_factory=dict)
    #: Node trọng tâm đang theo dõi (xem `analytics/focused_node.py`); rỗng nếu tắt.
    focused_node: Dict[str, Any] = field(default_factory=dict)
    nodes_seen: int = 0
    elapsed_seconds: float = 0.0
    analysed: int = 0
    throughput_per_second: float = 0.0


class MonitorPipeline:
    """Ghép MQTT + parser + SEQ + alert + ping + CSV thành một khối."""

    def __init__(
        self,
        config: TestConfig,
        devices: List[GatewayDevice],
        log_manager: LogManager,
        run_id: str = "",
        on_alert: Optional[Callable[[AlertRecord], None]] = None,
        on_record: Optional[Callable[[MQTTRecord], None]] = None,
    ) -> None:
        self.config = config
        self.devices = list(devices)
        self.logs = log_manager
        self.run_id = run_id
        self.on_alert = on_alert
        self.on_record = on_record

        self.gateway_names: Dict[str, str] = self._name_index(self.devices)
        self.parser = MessageParser(config.log_topic_patterns)
        self.seq = SequenceTracker(
            seq_max=config.seq_max,
            reset_threshold=config.seq_reset_threshold,
            out_of_order_window=config.seq_out_of_order_window,
        )
        # Gộp các bản copy của cùng một bản tin LoRa (nhiều gateway cùng nghe).
        self.deduper = PacketDeduper(
            merge_window_seconds=(
                config.packet_merge_window_seconds if config.packet_merge_enabled else 0.0
            ),
            copy_history=config.packet_copy_history,
        )
        # Khung lỗi không có seq nên gộp theo (error, raw_hex) — xem error_deduper.
        self.error_deduper = ErrorFrameCoalescer(
            merge_window_seconds=(
                config.packet_merge_window_seconds if config.packet_merge_enabled else 0.0
            ),
            copy_history=config.packet_copy_history,
        )
        self.detector = AnomalyDetector(AlertRule.from_config(config))
        self.ping_tracker = PingTracker(down_threshold=config.ping_down_threshold)
        # Theo dõi MỘT node trên nhiều gateway (bắt hụt / bắn hụt). Tắt mặc định
        # (`focused_node_enabled`) vì `feed()` chỉ là fast-path return khi rỗng.
        self.focused_node = FocusedNodeTracker(
            seq_max=config.seq_max,
            late_seconds=config.focused_node_late_seconds,
            detail_limit=config.focused_node_detail_limit,
        )
        # Nạp mọi gateway đã cấu hình để gateway "câm" (không publish bản tin nào
        # của node trọng tâm) vẫn hiện thành 1 dòng với bắt hụt 100%.
        self.focused_node.set_known_gateways(self.devices)
        if config.focused_node_enabled:
            self.focused_node.set_node(config.focused_node_uid)
        self.counters = PipelineCounters()

        self._queue: "queue.Queue[tuple]" = queue.Queue(maxsize=200000)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._alert_queue: "queue.Queue[AlertRecord]" = queue.Queue(maxsize=50000)
        self._last_timeout_check = 0.0
        self._last_quality_check = 0.0
        self._last_merge_flush = 0.0
        self._merge_flush_interval = MERGE_FLUSH_INTERVAL_SECONDS
        self._started_monotonic = 0.0
        self._analysed = 0
        self._node_keys: set = set()
        self.last_alert: Optional[AlertRecord] = None

    # ------------------------------------------------------------------ API
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._started_monotonic = time.monotonic()
        self.counters.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self._thread = threading.Thread(target=self._run, name="analyse", daemon=True)
        self._thread.start()
        log.info("Pipeline phân tích đã khởi động (run_id=%s)", self.run_id or "-")

    def stop(self, timeout: float = 15.0) -> None:
        """Dừng pipeline: xử lý nốt queue rồi đóng downtime window còn mở."""
        if self._thread is None:
            return
        self._stop.set()
        try:
            self._queue.put_nowait((None, None))
        except queue.Full:
            pass
        self._thread.join(timeout=timeout)
        self._thread = None
        # Đóng các window downtime đang mở -> ghi ONGOING
        for record in self.ping_tracker.close_all():
            self.logs.downtime.enqueue(record.to_row())
        log.info("Pipeline đã dừng (đã xử lý %d message)", self._analysed)

    def submit_raw(self, received_at: str, topic: str, payload: bytes) -> bool:
        """Đẩy 1 message thô vào pipeline (gọi từ MQTT thread — non-blocking)."""
        try:
            self._queue.put_nowait((EV_MESSAGE, (received_at, topic, payload)))
            return True
        except queue.Full:
            self.counters.queue_dropped += 1
            return False

    def submit_ping(self, record: PingRecord) -> bool:
        try:
            self._queue.put_nowait((EV_PING, record))
            return True
        except queue.Full:
            self.counters.queue_dropped += 1
            return False

    def update_devices(self, devices: List[GatewayDevice]) -> None:
        """Cập nhật tên gateway (khi người dùng đổi tên trong GUI)."""
        self.devices = list(devices)
        self.gateway_names = self._name_index(self.devices)
        self.focused_node.set_known_gateways(self.devices)

    def set_focused_node(self, node_uid: str) -> bool:
        """Đặt/đổi node trọng tâm (gọi từ UI thread hoặc lúc khởi động).

        Trả True nếu node thực sự đổi. Đổi node sẽ **xoá** số liệu của node cũ
        (xem `FocusedNodeTracker.set_node`). Truyền chuỗi rỗng để tắt theo dõi.
        """
        changed = self.focused_node.set_node(normalize_focus_node(node_uid))
        if changed:
            # `set_node` xoá sạch gateway -> phải dựng lại danh sách đã cấu hình.
            self.focused_node.set_known_gateways(self.devices)
            log.info(
                "Node trọng tâm: %s",
                self.focused_node.node_uid or "(tắt)",
            )
        return changed

    def focused_node_rows(self) -> List[Dict[str, Any]]:
        """Bảng tổng hợp bắt hụt / bắn hụt per gateway (dùng cho GUI)."""
        return self.focused_node.summary_rows()

    def focused_node_stats(self) -> Dict[str, Any]:
        return self.focused_node.stats()

    def focused_node_missing_rows(self) -> List[Dict[str, Any]]:
        return self.focused_node.missing_rows()

    def focused_node_late_rows(self) -> List[Dict[str, Any]]:
        return self.focused_node.late_rows()

    @staticmethod
    def _name_index(devices: List[GatewayDevice]) -> Dict[str, str]:
        """Map {gateway UID → tên} cho phép tra theo MỌI cách viết của UID.

        `devices.json` có thể lưu gateway UID dạng 12 hex zero-pad (MAC đầy đủ)
        trong khi firmware publish dạng 11 hex (`String(getEfuseMac(), HEX)` không
        zero-pad). Nếu index chỉ theo đúng chuỗi đã lưu thì gateway đó luôn bị
        gắn `UNKNOWN_GATEWAY` dù đã có trong danh sách thiết bị.
        """
        return gateway_name_index(
            (device.uid, device.name or device.uid)
            for device in devices
            if device.uid
        )

    def stats(self, mqtt_stats: Optional[Dict[str, Any]] = None,
              ping_stats: Optional[Dict[str, Any]] = None) -> PipelineStats:
        elapsed = time.monotonic() - self._started_monotonic if self._started_monotonic else 0.0
        return PipelineStats(
            counters=dict(vars(self.counters)),
            seq_nodes=self.seq.state_count(),
            queue_size=self._queue.qsize(),
            queue_max=self._queue.maxsize,
            mqtt=dict(mqtt_stats or {}),
            ping=dict(ping_stats or {}),
            alerts_by_level=self.detector.alerts_by_level(),
            nodes_seen=len(self._node_keys),
            elapsed_seconds=round(elapsed, 3),
            analysed=self._analysed,
            throughput_per_second=round(self._analysed / elapsed, 2) if elapsed > 0 else 0.0,
            focused_node=self.focused_node.stats(),
        )

    def drain_alerts(self, max_items: int = 500) -> List[AlertRecord]:
        out: List[AlertRecord] = []
        for _ in range(max_items):
            try:
                out.append(self._alert_queue.get_nowait())
            except queue.Empty:
                break
        return out

    def node_state_rows(self) -> List[Dict[str, Any]]:
        """Bảng node hiện tại (dùng cho GUI + node_summary.csv).

        Một dòng = một **node** (không phải một cặp gateway-node): nhiều gateway
        cùng nghe một node nên tách theo gateway sẽ nhân số dòng lên N lần.
        Thông tin "gateway nào nghe" nằm ở `gateways_heard` / `gateways_heard_avg` /
        `gateways_heard_max` (chi tiết UID từng gateway ở cột `gateways` của mqtt_data).
        """
        rows: List[Dict[str, Any]] = []
        for key, state in sorted(self.seq.states().items()):
            rate = state.delivery_rate()
            rows.append(
                {
                    "node_uid": state.node_uid,
                    "node_format": state.node_format,
                    "gateways_heard": state.gateways_heard,
                    "gateways_heard_avg": state.gateways_heard_avg(),
                    "gateways_heard_max": state.gateways_heard_max,
                    "packets": state.packets,
                    "first_seq": state.first_seq if state.first_seq is not None else "",
                    "last_seq": state.last_seq if state.last_seq is not None else "",
                    "missing": state.missing,
                    "duplicate": state.duplicate,
                    "out_of_order": state.out_of_order,
                    "seq_reset": state.seq_reset,
                    "seq_wrap": state.seq_wrap,
                    "delivery_rate_percent": rate if rate is not None else "",
                    "first_seen": state.first_seen,
                    "last_seen": state.last_seen,
                }
            )
        return rows

    # ------------------------------------------------------- analyse thread
    def _run(self) -> None:
        get = self._queue.get
        while True:
            try:
                event, payload = get(timeout=0.5)
            except queue.Empty:
                self._periodic_checks()
                continue

            if event is None:
                # Drain nốt phần còn lại trước khi thoát
                self._drain_remaining()
                break

            try:
                if event == EV_MESSAGE:
                    self._handle_raw(*payload)
                elif event == EV_PING:
                    self._handle_ping(payload)
            except Exception:  # noqa: BLE001 - 1 record lỗi không được giết pipeline
                self.counters.parse_errors += 1
                log.exception("Lỗi xử lý sự kiện %s", event)

            if self._stop.is_set():
                self._drain_remaining()
                break

        # Chốt hết bản tin còn trong cửa sổ gộp -> không mất dòng nào khi dừng.
        self._flush_packets(force=True)

        for record in self.ping_tracker.close_all():
            self.logs.downtime.enqueue(record.to_row())

    def _drain_remaining(self, max_items: int = 200000) -> None:
        for _ in range(max_items):
            try:
                event, payload = self._queue.get_nowait()
            except queue.Empty:
                return
            if event is None:
                continue
            try:
                if event == EV_MESSAGE:
                    self._handle_raw(*payload)
                elif event == EV_PING:
                    self._handle_ping(payload)
            except Exception:  # noqa: BLE001
                self.counters.parse_errors += 1

    def _periodic_checks(self) -> None:
        now = time.monotonic()
        # Chốt bản tin đã ra khỏi cửa sổ gộp, kể cả khi tạm thời không có message
        # mới (nếu chỉ chốt lúc có message thì bản tin cuối phiên bị treo).
        self._flush_packets(force=False)
        # Quét node/gateway im lặng mỗi 5 s
        if now - self._last_timeout_check >= 5.0:
            self._last_timeout_check = now
            for alert in self.detector.check_timeouts():
                self._emit_alert(alert)
        # Đánh giá chất lượng SEQ mỗi 60 s
        if now - self._last_quality_check >= 60.0:
            self._last_quality_check = now
            for alert in self.detector.check_node_quality(self.seq.states()):
                self._emit_alert(alert)

    # -------------------------------------------------------------- handlers
    def _handle_raw(self, received_at: str, topic: str, payload: bytes) -> None:
        self.counters.total_messages += 1
        self.counters.bytes_received += len(payload)
        self.counters.last_message_at = received_at

        record = self.parser.parse(
            topic, payload, received_at=received_at, known_gateways=self.gateway_names
        )

        # --- phân tích SEQ (chỉ DATA có seq) ---
        # Một node phát 1 bản tin thì MỌI gateway trong tầm đều publish. Vì vậy
        # DATA không được ghi/đếm ngay: đưa vào bộ gộp để N bản copy thành 1 bản
        # tin logic `(node_uid, seq)` (xem analytics/packet_deduper.py).
        if record.packet_type == PKT_DATA and record.seq is not None and record.node_uid:
            self.deduper.add(
                record.node_uid,
                record.seq,
                PacketCopy(
                    gateway_uid=record.gateway_uid,
                    gateway_name=record.gateway_name,
                    receiver_uid=record.receiver_uid,
                    rssi=record.rssi,
                    snr=record.snr,
                    received_at=record.received_at,
                    at=parse_stamp(record.received_at),
                    record=record,
                ),
            )
            # Bản tin sẽ được ghi (và phân tích SEQ) khi ra khỏi cửa sổ gộp —
            # KHÔNG ghi ngay ở đây, nếu không mỗi bản copy thành 1 dòng.
            self._flush_packets(force=False)
            return

        # INFO / DATA_NO_SEQ / LOG / UNKNOWN: không có bản tin trùng để gộp, nhưng
        # vẫn ghi nhận số gateway đã nghe nếu node đã biết.
        #
        # ERROR: cũng bị MỌI gateway publish lại nhưng KHÔNG có seq nên gộp theo
        # (error, raw_hex) — xem `analytics/error_deduper.py`.
        if record.packet_type == PKT_ERROR:
            self.error_deduper.add(
                frame_key(record.error, record.raw_hex, record.payload_json),
                FrameCopy(
                    gateway_uid=record.gateway_uid,
                    gateway_name=record.gateway_name,
                    receiver_uid=record.receiver_uid,
                    rssi=record.rssi,
                    snr=record.snr,
                    received_at=record.received_at,
                    at=parse_stamp(record.received_at),
                    record=record,
                ),
            )
            self._flush_packets(force=False)
            return

        self._emit_raw(record, received_at, topic, payload)

    def _emit_raw(self, record: MQTTRecord, received_at: str, topic: str, payload: bytes) -> None:
        """Định tuyến 1 record KHÔNG qua bộ gộp (mọi loại trừ DATA).

        Với INFO / DATA_NO_SEQ của node đã có trạng thái, chỉ ghi nhận phủ sóng
        (gateway đã nghe) chứ KHÔNG đụng tới con trỏ SEQ — tránh phá logic SEQ.
        """
        if (record.packet_type in (PKT_INFO, PKT_DATA_NO_SEQ)) and record.node_uid:
            state = self.seq.states().get(SequenceTracker.make_key(record.node_uid))
            if state is not None:
                state.observe_coverage(
                    [record.gateway_uid] if record.gateway_uid else (),
                )
                if record.gateway_uid:
                    state.last_gateway = record.gateway_uid.upper()
                if record.received_at:
                    state.last_seen = record.received_at
        if record.node_uid:
            # Node đã xuất hiện (kể cả node cũ chỉ gửi info frame, không có SEQ).
            self._node_keys.add(record.device_key())
        self._count_and_route(record)

        # --- cảnh báo mức sự kiện ---
        for alert in self.detector.feed(record):
            self._emit_alert(alert)

        self._analysed += 1
        if self.on_record is not None:
            try:
                self.on_record(record)
            except Exception:  # noqa: BLE001 - callback GUI không được chặn
                log.debug("on_record lỗi", exc_info=True)

    def _flush_packets(self, force: bool = False) -> None:
        """Chốt các bản tin/khung lỗi đã ra khỏi cửa sổ gộp.

        Một bản tin chỉ được ghi CSV khi đã ra khỏi cửa sổ gộp, để mọi gateway
        cùng nghe có cơ hội "góp" vào cùng một dòng. `force=True` dùng lúc dừng
        phiên (chốt hết, không mất dòng nào).

        Đồng hồ ở đây là `time.monotonic()` — thời điểm TOOL nhận bản tin, không
        phải giờ trong payload (gateway có thể lệch giờ / phát lại dữ liệu cũ).
        """
        now = time.monotonic()
        if not force and (now - self._last_merge_flush) < self._merge_flush_interval:
            return
        self._last_merge_flush = now
        packets = self.deduper.flush_all() if force else self.deduper.flush_due(now)
        for packet in packets:
            self._publish_packet(packet)
        frames = self.error_deduper.flush_all() if force else self.error_deduper.flush_due(now)
        for frame in frames:
            self._publish_frame(frame)

    def _publish_packet(self, packet) -> None:
        """Chốt 1 bản tin logic: phân tích SEQ + ghi mqtt_data."""
        primary = packet.primary
        record = primary.record
        if record is None:
            return

        # Node trọng tâm: nạp TỪNG bản copy (mỗi gateway 1 lần) chứ không chỉ bản
        # đại diện — cần biết gateway nào publish bản tin nào (`packet.copies`).
        self._feed_focused_node(packet)

        outcome = self.seq.feed(
            packet.node_uid,
            packet.seq,
            timestamp=packet.earliest_received_at() or record.received_at,
            node_format=record.node_format,
            gateways=packet.gateways(),
            gateway_uid=primary.gateway_uid,
        )
        outcome.apply_to(record)
        self._node_keys.add(record.device_key())

        if outcome.missing_record is not None:
            self.logs.missing.enqueue(outcome.missing_record.to_row())
            self.counters.missing_packets += outcome.missing_record.missing_count
        if outcome.anomaly_record is not None:
            self.logs.anomalies.enqueue(outcome.anomaly_record.to_row())
        if outcome.duplicate:
            self.counters.duplicate_packets += 1
        elif outcome.out_of_order:
            self.counters.out_of_order_packets += 1
        elif outcome.reset:
            self.counters.seq_reset_events += 1
        elif outcome.wrap:
            self.counters.seq_wrap_events += 1

        # Ghi mqtt_data bằng dòng ĐÃ GỘP (có `gateways_heard` / `gateways` /
        # `received_last_at`), không phải `record.to_row()` của bản copy đại diện.
        self._count_and_route(record, row=packet.to_row())

        # Cảnh báo mức sự kiện dùng bản tin ĐÃ GỘP (1 lần/node, không N lần).
        for alert in self.detector.feed(record):
            self._emit_alert(alert)

        self._analysed += 1
        if self.on_record is not None:
            try:
                self.on_record(record)
            except Exception:  # noqa: BLE001 - callback GUI không được chặn
                log.debug("on_record lỗi", exc_info=True)

    def _feed_focused_node(self, packet) -> None:
        """Nạp node trọng tâm từ 1 bản tin ĐÃ GỘP (mỗi bản copy = 1 gateway).

        Vì sao lấy từ bản tin đã gộp chứ không phải từng message thô: chỉ khi đã
        gộp mới biết chắc *tất cả* gateway nào đã publish bản tin `(node_uid, seq)`
        đó, và mốc `received_at` sớm nhất. Bản tin còn phải chờ ra khỏi cửa sổ gộp
        (2 s) nên số liệu bắt hụt/bắn hụt trễ hơn thực tế ~2 s — không đáng kể.
        """
        if not self.focused_node.active:
            return
        if normalize_uid(packet.node_uid) != self.focused_node.node_uid:
            return
        for copy in packet.copies:
            if copy.record is not None:
                self.focused_node.feed(copy.record)

    def _publish_frame(self, frame) -> None:
        """Chốt 1 khung lỗi logic: ghi mqtt_errors (1 dòng/khung) + mqtt_data."""
        primary = frame.primary
        record = primary.record
        if record is None:
            return
        row = frame.to_row()
        if row:
            self.logs.errors.enqueue(row)
        # mqtt_data dùng schema DATA (có `packet_type`) nhưng vẫn kèm độ phủ gộp.
        self._count_and_route(record, row=frame.to_data_row())

        for alert in self.detector.feed(record):
            self._emit_alert(alert)

        self._analysed += 1
        if self.on_record is not None:
            try:
                self.on_record(record)
            except Exception:  # noqa: BLE001 - callback GUI không được chặn
                log.debug("on_record lỗi", exc_info=True)

    def _count_and_route(self, record: MQTTRecord,
                         row: Optional[Dict[str, Any]] = None) -> None:
        """Đếm + định tuyến 1 record.

        `row` là dòng `mqtt_data` đã chuẩn bị sẵn (bản tin/khung lỗi đã gộp, có
        `gateways_heard`). Truyền `None` thì dùng `record.to_row()`.
        """
        if record.valid:
            self.counters.valid_messages += 1
        else:
            self.counters.invalid_messages += 1

        packet_type = record.packet_type
        if packet_type == PKT_DATA:
            self.counters.data_packets += 1
        elif packet_type == PKT_DATA_NO_SEQ:
            self.counters.data_no_seq_packets += 1
        elif packet_type == PKT_INFO:
            self.counters.info_packets += 1
        elif packet_type == PKT_ERROR:
            # Đếm KHUNG LỖI (đã gộp), không phải số lần publish: mọi gateway
            # trong tầm đều báo cùng một khung lỗi. Chi tiết ghi ở `_publish_frame`.
            self.counters.error_packets += 1
        elif packet_type == PKT_LOG:
            self.counters.log_messages += 1
            self.logs.gateway_log.enqueue(
                {
                    "received_at": record.received_at,
                    "gateway_uid": record.gateway_uid,
                    "gateway_name": record.gateway_name,
                    "topic": record.topic,
                    "status": record.status,
                    "gateway_timestamp": record.gateway_timestamp,
                    "payload_json": record.payload_json,
                }
            )
        else:
            self.counters.unknown_messages += 1

        if record.gateway_uid and record.gateway_uid not in self.gateway_names:
            self.counters.unknown_gateway_messages += 1
            if self.config.alert_unknown_gateway:
                self._emit_note_anomaly(record, AN_UNKNOWN_GATEWAY)
        if record.node_format == NODE_FORMAT_UNKNOWN and record.node_uid:
            self.counters.unknown_format_messages += 1
            if self.config.alert_unknown_node_format:
                self._emit_note_anomaly(record, AN_UNKNOWN_NODE_FORMAT)
        if record.validation_error == V_INVALID_JSON and not record.payload_json.strip():
            self._emit_note_anomaly(record, AN_INVALID_JSON)

        # Mọi bản tin (kể cả log/error) đều vào mqtt_data để forensic đầy đủ.
        self.logs.data.enqueue(row if row is not None else record.to_row())

    def _emit_note_anomaly(self, record: MQTTRecord, anomaly_type: str) -> None:
        self.logs.anomalies.enqueue(
            {
                "timestamp": record.received_at,
                "gateway_uid": record.gateway_uid,
                "receiver_uid": record.receiver_uid,
                "node_uid": record.node_uid,
                "seq": record.seq if record.seq is not None else "",
                "previous_seq": record.previous_seq if record.previous_seq is not None else "",
                "anomaly_type": anomaly_type,
                "details": record.validation_error or anomaly_type,
            }
        )

    def _handle_ping(self, record: PingRecord) -> None:
        self.logs.ping.enqueue(record.to_row())
        closed = self.ping_tracker.feed(record)
        if closed is not None:
            self.logs.downtime.enqueue(closed.to_row())
            alert = AlertRecord(
                timestamp=closed.down_end or record.timestamp,
                level="WARNING",
                alert_type="GATEWAY_DOWN_RECOVERED",
                gateway_uid=closed.gateway_uid,
                gateway_name=closed.gateway_name,
                message="Gateway mất kết nối %.1f phút rồi hồi phục"
                % ((closed.duration_seconds or 0) / 60.0),
                details="Từ %s đến %s (%d lần ping fail)"
                % (closed.down_start, closed.down_end, closed.failed_ping_count),
                value=closed.duration_seconds,
            )
            self._emit_alert(alert)

    def _emit_alert(self, alert: AlertRecord) -> None:
        self.last_alert = alert
        self.logs.alerts.enqueue(alert.to_row())
        try:
            self._alert_queue.put_nowait(alert)
        except queue.Full:
            pass
        if self.on_alert is not None:
            try:
                self.on_alert(alert)
            except Exception:  # noqa: BLE001
                log.debug("on_alert lỗi", exc_info=True)

    # ------------------------------------------------------------ finalize
    def write_reports(self, session: Any = None, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Path]:
        """Ghi các file tổng kết vào `reports/`. Gọi khi kết thúc phiên."""
        from analytics.correlation import correlate
        from storage.reader import iter_csv_rows
        from storage.schema import (
            CORRELATION_COLUMNS,
            FOCUSED_NODE_LATE_COLUMNS,
            FOCUSED_NODE_MISSING_COLUMNS,
            FOCUSED_NODE_SUMMARY_COLUMNS,
            NODE_SUMMARY_COLUMNS,
            PING_SUMMARY_COLUMNS,
            RUN_SUMMARY_COLUMNS,
            SILENCE_COLUMNS,
        )

        written: Dict[str, Path] = {}
        node_rows = self.node_state_rows()
        written["node_summary"] = self.logs.write_single_csv(
            "node_summary.csv", NODE_SUMMARY_COLUMNS, node_rows
        )
        ping_rows = [s.to_summary_row() for s in self.ping_tracker.stats_sorted()]
        written["ping_summary"] = self.logs.write_single_csv(
            "ping_summary.csv", PING_SUMMARY_COLUMNS, ping_rows
        )

        # --- node trọng tâm: bắt hụt / bắn hụt per gateway ---
        if self.focused_node.active:
            # Quét bitmap một lần để có kiểu ngắt quãng (liên tục vs rải rác)
            # trước khi ghi — chi phí ~ms/gateway, chỉ chạy lúc kết thúc phiên.
            self.focused_node.refresh_patterns()
            written["focused_node_summary"] = self.logs.write_single_csv(
                "focused_node_summary.csv",
                FOCUSED_NODE_SUMMARY_COLUMNS,
                self.focused_node.summary_rows(),
            )
            written["focused_node_missing"] = self.logs.write_single_csv(
                "focused_node_missing.csv",
                FOCUSED_NODE_MISSING_COLUMNS,
                self.focused_node.missing_rows(),
            )
            written["focused_node_late"] = self.logs.write_single_csv(
                "focused_node_late.csv",
                FOCUSED_NODE_LATE_COLUMNS,
                self.focused_node.late_rows(),
            )

        # --- correlation: đọc lại CSV (source of truth) ---
        from analytics.correlation import correlate, row_gateways

        mqtt_rows: List[Dict[str, Any]] = []
        for path in sorted(self.logs.mqtt_dir.glob("mqtt_data_*.csv")):
            mqtt_rows.extend(iter_csv_rows(path))
        ping_csv_rows: List[Dict[str, Any]] = []
        for path in sorted(self.logs.ping_dir.glob("ping_log_*.csv")):
            ping_csv_rows.extend(iter_csv_rows(path))
        log_rows: List[Dict[str, Any]] = []
        for path in sorted(self.logs.mqtt_dir.glob("mqtt_gateway_log_*.csv")):
            log_rows.extend(iter_csv_rows(path))

        report = correlate(
            ping_csv_rows,
            mqtt_rows,
            log_rows,
            window_seconds=self.config.correlation_window_seconds,
            silent_threshold=self.config.silent_window_threshold,
        )
        written["correlation_summary"] = self.logs.write_single_csv(
            "correlation_summary.csv", CORRELATION_COLUMNS, report.summary_rows()
        )
        written["mqtt_silence"] = self.logs.write_single_csv(
            "mqtt_silence.csv", SILENCE_COLUMNS, report.silence_events
        )

        # --- alert summary ---
        from storage.schema import ALERT_COLUMNS  # noqa: WPS433 - tránh vòng import

        alert_rows = [a.to_row() for a in self.detector.alerts()]
        written["alerts"] = self.logs.write_single_csv(
            "alerts_summary.csv", ALERT_COLUMNS, alert_rows
        )

        # --- run summary ---
        summary = dict(self.counters.as_summary_fields())
        summary.update(
            {
                "run_id": self.run_id,
                "session_start": self.counters.started_at,
                "session_end": datetime.now().astimezone().isoformat(timespec="seconds"),
                "duration_seconds": round(
                    time.monotonic() - self._started_monotonic, 3
                )
                if self._started_monotonic
                else "",
                "broker": "%s:%d" % (self.config.mqtt_host, self.config.mqtt_port),
                "subscribed_topics": "|".join(self.config.effective_subscribe_topics()),
                # `gateway_names` là index biến thể (nhiều khoá / 1 gateway) nên
                # phải đếm theo danh sách thiết bị, không theo số khoá.
                "gateways_monitored": len(self.devices),
                "gateways_seen": len(
                    {
                        gateway
                        for row in mqtt_rows
                        for gateway in row_gateways(row)
                    }
                ),
                "nodes_seen": len(self._node_keys),
                "ping_total": self.ping_tracker.ping_records,
                "ping_failed": sum(s.failed for s in self.ping_tracker.stats().values()),
                "downtime_events": sum(s.downtime_events for s in self.ping_tracker.stats().values()),
                "downtime_seconds": round(
                    sum(s.downtime_seconds for s in self.ping_tracker.stats().values()), 3
                ),
                "reconnects": 0,
            }
        )
        if extra:
            summary.update(extra)
        written["run_summary"] = self.logs.write_single_csv(
            "run_summary.csv", RUN_SUMMARY_COLUMNS, [summary]
        )
        return written
