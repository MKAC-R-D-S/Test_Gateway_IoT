"""Kiểu dữ liệu dùng chung của tool long-run monitor."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Loại bản tin trên luồng MQTT — suy ra từ topic + payload (xem
# docs/MQTT_PROTOCOL_ANALYSIS.md §4).
# ---------------------------------------------------------------------------
PKT_DATA = "DATA"                  # node/<uuid>/data, có seq != 0 (telemetry)
PKT_DATA_NO_SEQ = "DATA_NO_SEQ"    # node/<uuid>/data, KHÔNG có seq (info node cũ 22B)
PKT_INFO = "INFO"                  # node/<uuid>/info hoặc seq == 0 (info node mới)
PKT_ERROR = "ERROR"                # node/error (CRC/length/format/uuid)
PKT_LOG = "LOG"                    # <model>/<uuid>/log (report hệ thống / LWT)
PKT_UNKNOWN = "UNKNOWN"            # topic lạ hoặc payload không nhận dạng được

# ---------------------------------------------------------------------------
# Kết quả validation
# ---------------------------------------------------------------------------
V_OK = "OK"
V_INVALID_JSON = "INVALID_JSON"
V_NOT_OBJECT = "NOT_JSON_OBJECT"
V_MISSING_GATEWAY = "MISSING_GATEWAY_ID"
V_MISSING_RECEIVER = "MISSING_RECEIVER_ID"
V_MISSING_SENSOR = "MISSING_SENSOR_ID"
V_INVALID_UID = "INVALID_UID_FORMAT"
V_INVALID_SEQ = "INVALID_SEQ"
V_UNKNOWN_TOPIC = "UNKNOWN_TOPIC"

# ---------------------------------------------------------------------------
# Loại bất thường (anomaly) — ghi vào mqtt_anomalies_*.csv
# ---------------------------------------------------------------------------
AN_DUPLICATE = "DUPLICATE"
AN_MISSING = "MISSING"
AN_OUT_OF_ORDER = "OUT_OF_ORDER"
AN_SEQ_RESET = "SEQ_RESET"
AN_SEQ_WRAP = "SEQ_WRAP"
AN_INVALID_JSON = "INVALID_JSON"
AN_UNKNOWN_GATEWAY = "UNKNOWN_GATEWAY"
AN_UNKNOWN_NODE_FORMAT = "UNKNOWN_NODE_FORMAT"

ANOMALY_TYPES = [
    AN_DUPLICATE,
    AN_MISSING,
    AN_OUT_OF_ORDER,
    AN_SEQ_RESET,
    AN_SEQ_WRAP,
    AN_INVALID_JSON,
    AN_UNKNOWN_GATEWAY,
    AN_UNKNOWN_NODE_FORMAT,
]

# Verdict của SequenceTracker
SQ_NORMAL = "NORMAL"
SQ_MISSING = "MISSING"
SQ_DUPLICATE = "DUPLICATE"
SQ_OUT_OF_ORDER = "OUT_OF_ORDER"
SQ_RESET = "RESET"
SQ_FIRST = "FIRST"

# Trạng thái kết nối MQTT (dùng cho GUI + CSV)
MQTT_DISCONNECTED = "DISCONNECTED"
MQTT_CONNECTING = "CONNECTING"
MQTT_CONNECTED = "CONNECTED"
MQTT_RECONNECTING = "RECONNECTING"


@dataclass
class MQTTRecord:
    """Một bản tin MQTT đã parse — đây là đơn vị ghi xuống CSV.

    Quan trọng: `received_at` (giờ PC) và `gateway_timestamp` (giờ gateway) là
    hai trường KHÁC NHAU, không được thay thế nhau (xem analysis §7).
    """

    received_at: str = ""
    mqtt_timestamp: str = ""
    topic: str = ""
    packet_type: str = PKT_UNKNOWN
    gateway_uid: str = ""
    gateway_name: str = ""
    receiver_uid: str = ""
    node_uid: str = ""
    node_format: str = ""
    seq: Optional[int] = None
    rssi: Optional[float] = None
    snr: Optional[float] = None
    mcu_temp: Optional[float] = None
    mcu_vdd: Optional[float] = None
    sensor_count: Optional[int] = None
    sensor_values: List[float] = field(default_factory=list)
    gateway_timestamp: str = ""
    status: str = ""
    error: str = ""
    raw_hex: str = ""
    valid: bool = False
    validation_error: str = ""
    payload_json: str = ""
    payload_size: int = 0
    # Kết quả phân tích SEQ (điền sau, ở thread phân tích)
    previous_seq: Optional[int] = None
    expected_seq: Optional[int] = None
    seq_delta: Optional[int] = None
    seq_status: str = ""
    missing_count: int = 0
    duplicate: bool = False
    out_of_order: bool = False
    reboot_detected: bool = False
    anomaly: str = ""

    def to_row(self) -> Dict[str, Any]:
        """Chuyển thành dict 1 dòng CSV (fieldnames do storage.schema định nghĩa)."""
        row = dict(self.__dict__)
        row["sensor_values"] = "|".join(_fmt_num(v) for v in self.sensor_values)
        row["duplicate"] = int(bool(self.duplicate))
        row["out_of_order"] = int(bool(self.out_of_order))
        row["reboot_detected"] = int(bool(self.reboot_detected))
        row["valid"] = int(bool(self.valid))
        return row

    # Tiện ích cho GUI
    def device_key(self) -> str:
        """Khoá **node** (KHÔNG gồm gateway/receiver).

        Nhiều gateway cùng nghe một node, nên đếm node theo
        `gateway|receiver|node` sẽ nhân số node lên theo số gateway.
        """
        return self.node_uid or ""

    def packet_key(self) -> str:
        """Khoá bản tin logic = `node_uid|seq` (xem `analytics/packet_deduper.py`)."""
        return "%s|%s" % (self.node_uid or "", self.seq if self.seq is not None else "")


def _fmt_num(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return ("%.4f" % value).rstrip("0").rstrip(".")
    return str(value)


@dataclass
class MissingRecord:
    """Một khoảng thiếu SEQ giữa 2 packet của cùng một node.

    `gateway_uid` là gateway có RSSI tốt nhất trong cụm đã nghe packet kết thúc
    khoảng thiếu; `gateways_heard` / `gateways` cho biết độ phủ LoRa của bản tin.
    """

    timestamp: str
    gateway_uid: str
    node_uid: str
    previous_seq: int
    current_seq: int
    missing_from: int
    missing_to: int
    missing_count: int
    reason: str = "SEQ_GAP"
    receiver_uid: str = ""
    gateways_heard: int = 0
    gateways: str = ""

    def to_row(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class AnomalyRecord:
    """Một bất thường đơn lẻ (duplicate / out-of-order / reset / wrap / ...)."""

    timestamp: str
    gateway_uid: str
    node_uid: str
    seq: Optional[int]
    previous_seq: Optional[int]
    anomaly_type: str
    details: str = ""
    receiver_uid: str = ""
    gateways_heard: int = 0
    gateways: str = ""

    def to_row(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ErrorRecord:
    """node/error — tách riêng khỏi luồng packet hợp lệ."""

    received_at: str
    gateway_uid: str
    gateway_name: str
    receiver_uid: str
    topic: str
    error: str
    gateway_timestamp: str
    rssi: Optional[float]
    snr: Optional[float]
    raw_hex: str
    payload_json: str

    def to_row(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class LogRecord:
    """<model>/<uuid>/log — report hệ thống 60s hoặc Last Will status=offline."""

    received_at: str
    gateway_uid: str
    gateway_name: str
    topic: str
    status: str
    series: str
    model: str
    eth_ip: str
    eth_mac: str
    power_on_time: str
    gateway_timestamp: str
    payload_json: str

    def to_row(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class PingRecord:
    """Một lần ping."""

    timestamp: str
    gateway_uid: str
    gateway_name: str
    ip_address: str
    ping_success: bool
    latency_ms: Optional[float]
    timeout: bool
    error: str = ""

    def to_row(self) -> Dict[str, Any]:
        row = dict(self.__dict__)
        row["ping_success"] = int(bool(self.ping_success))
        row["timeout"] = int(bool(self.timeout))
        return row


@dataclass
class DowntimeRecord:
    """Một khoảng downtime đã đóng (hoặc ONGOING khi tool kết thúc)."""

    gateway_uid: str
    gateway_name: str
    ip_address: str
    down_start: str
    down_end: str
    duration_seconds: Optional[float]
    failed_ping_count: int
    status: str = "CLOSED"
    recovered: bool = True
    last_down_ping: str = ""

    def to_row(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def json_dumps_safe(obj: Any) -> str:
    """JSON 1 dòng, an toàn cho CSV (không newline)."""
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
