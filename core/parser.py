"""Parser bản tin MQTT → MQTTRecord, bám đúng format firmware.

Nguyên tắc (xem docs/MQTT_PROTOCOL_ANALYSIS.md):
  - MQTT callback CHỈ parse tối thiểu rồi đẩy record vào queue. Không I/O.
  - Không tự tính CRC: firmware đã kiểm CRC trước khi publish (data_process.cpp:214).
  - Bản tin hợp lệ = nhận được + JSON parse được + có field bắt buộc.
  - Field thiếu → để trống, KHÔNG drop bản ghi (trừ khi JSON lỗi/không phải object).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import (
    PKT_DATA,
    PKT_DATA_NO_SEQ,
    PKT_ERROR,
    PKT_INFO,
    PKT_LOG,
    PKT_UNKNOWN,
    V_INVALID_JSON,
    V_INVALID_SEQ,
    V_INVALID_UID,
    V_MISSING_GATEWAY,
    V_MISSING_RECEIVER,
    V_MISSING_SENSOR,
    V_NOT_OBJECT,
    V_OK,
    V_UNKNOWN_TOPIC,
    MQTTRecord,
    json_dumps_safe,
)
from .uid import (
    classify_node_uid,
    format_mac_from_gateway_uid,
    is_hex,
    is_hex_uid,
    normalize_gateway_uid,
    normalize_uid,
)

# Topic dữ liệu: node/<uuid>/data | node/<uuid>/info ; lỗi: node/error
TOPIC_ERROR = "node/error"
TOPIC_PREFIX = "node/"

# Số kênh cảm biến tối đa (MAX_SENSOR_COUNT = 60, main.h:232)
MAX_SENSOR_CHANNELS = 60

_HEX_BYTE_PRINT_LIMIT = 256


def now_iso() -> str:
    """Giờ PC (local) dạng ISO 8601 có offset — dùng cho `received_at`."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def parse_topic(topic: str) -> Tuple[str, str]:
    """Trả (kind, node_uid) từ topic.

    kind ∈ {data, info, error, log, other}
    """
    topic = (topic or "").strip()
    if topic == TOPIC_ERROR:
        return "error", ""
    if topic.startswith(TOPIC_PREFIX):
        rest = topic[len(TOPIC_PREFIX) :]
        parts = rest.split("/")
        if len(parts) >= 2:
            node_uid = normalize_uid(parts[0])
            leaf = parts[1].lower()
            if leaf == "data":
                return "data", node_uid
            if leaf == "info":
                return "info", node_uid
            return "other", node_uid
        return "other", ""
    return "other", ""


def is_log_topic(topic: str, patterns: Optional[Iterable[str]] = None) -> bool:
    """True nếu topic khớp pattern log gateway `<model>/<uuid>/log`."""
    topic = (topic or "").strip()
    if not topic:
        return False
    if topic.startswith(TOPIC_PREFIX):
        return False
    patterns = list(patterns or ["+/+/log"])
    parts = topic.split("/")
    for pattern in patterns:
        if _topic_match(pattern, parts):
            return True
    # Mặc định: bất kỳ topic 3 tầng kết thúc bằng "log"
    return len(parts) == 3 and parts[2].lower() == "log"


def _topic_match(pattern: str, parts: List[str]) -> bool:
    pparts = pattern.split("/")
    if len(pparts) != len(parts):
        return False
    for p, v in zip(pparts, parts):
        if p in ("#", "+"):
            continue
        if p != v:
            return False
    return True


# ---------------------------------------------------------------------------
# Parser chính
# ---------------------------------------------------------------------------
class MessageParser:
    """Parse payload thô → `MQTTRecord`.

    Tham số:
      known_gateways: map {gateway_uid → gateway_name}; dùng để gắn tên + đánh
                      dấu UNKNOWN_GATEWAY. Không chặn bản ghi.
      log_patterns  : pattern topic log (mặc định `+/+/log`).
    """

    def __init__(self, log_patterns: Optional[Iterable[str]] = None) -> None:
        self.log_patterns = list(log_patterns or ["+/+/log"])

    def parse(
        self,
        topic: str,
        payload: bytes,
        received_at: Optional[str] = None,
        known_gateways: Optional[Dict[str, str]] = None,
    ) -> MQTTRecord:
        received_at = received_at or now_iso()
        raw_text = self._decode(payload)

        record = MQTTRecord(
            received_at=received_at,
            mqtt_timestamp=received_at,  # firmware không gửi metadata broker
            topic=topic or "",
            payload_json=_clean_for_csv(raw_text),
            payload_size=len(payload or b""),
        )

        kind, topic_node_uid = parse_topic(topic)

        if kind == "error":
            record.packet_type = PKT_ERROR
        elif kind == "data":
            record.packet_type = PKT_DATA
        elif kind == "info":
            record.packet_type = PKT_INFO
        elif is_log_topic(topic, self.log_patterns):
            record.packet_type = PKT_LOG
        else:
            record.packet_type = PKT_UNKNOWN

        try:
            data = json.loads(raw_text) if raw_text.strip() else None
        except (json.JSONDecodeError, ValueError):
            # Payload không phải JSON → KHÔNG thể là bản tin dữ liệu hợp lệ. Nếu
            # giữ nguyên `packet_type` suy từ topic (/data) thì rác sẽ bị đếm là
            # DATA và làm sai mọi thống kê (xem smoke_test §3: 1 gói rác).
            record.packet_type = PKT_UNKNOWN
            record.valid = False
            record.validation_error = V_INVALID_JSON
            return record

        if not isinstance(data, dict):
            record.valid = False
            record.validation_error = V_NOT_OBJECT
            return record

        if record.packet_type == PKT_LOG:
            self._fill_log(record, data)
            record.valid = True
            record.validation_error = V_OK
            self._attach_gateway(record, known_gateways)
            return record

        if record.packet_type == PKT_UNKNOWN:
            record.validation_error = V_UNKNOWN_TOPIC

        self._fill_common(record, data)

        # --- Xác định node_uid: ưu tiên payload, fallback topic ---
        sensor_id = normalize_uid(data.get("sensor_id"))
        if sensor_id:
            record.node_uid = sensor_id
        elif topic_node_uid:
            record.node_uid = topic_node_uid
        record.node_format = classify_node_uid(record.node_uid) if record.node_uid else ""

        # --- ERROR packet: không có sensor_id/seq ---
        if record.packet_type == PKT_ERROR:
            record.error = str(data.get("error", "") or "")
            record.raw_hex = _raw_data_to_hex(data.get("raw_data"))
            record.valid = False
            record.validation_error = record.error or "ERROR_PACKET"
            self._attach_gateway(record, known_gateways)
            return record

        # --- seq ---
        seq_present, seq_value = _extract_int(data, "seq")
        if seq_present:
            if seq_value is None or seq_value < 0:
                record.validation_error = V_INVALID_SEQ
            else:
                record.seq = seq_value

        # --- Phân loại DATA vs INFO ---
        if record.packet_type == PKT_INFO:
            record.packet_type = PKT_INFO
        elif record.packet_type == PKT_DATA:
            if record.seq is None:
                # node cũ info frame 22B: gửi trên /data nhưng KHÔNG có seq
                # (data_processing_task.cpp:552-557, data_process.cpp:394)
                record.packet_type = PKT_DATA_NO_SEQ
            elif record.seq == 0:
                record.packet_type = PKT_INFO
            else:
                record.packet_type = PKT_DATA

        # --- Cảm biến ---
        count_present, count_value = _extract_int(data, "sensor_count")
        if count_present and count_value is not None and count_value >= 0:
            record.sensor_count = min(count_value, MAX_SENSOR_CHANNELS)
        for channel in range(MAX_SENSOR_CHANNELS):
            key = "sensor_value_%d" % channel
            if key not in data:
                continue
            values = _as_float_list(data.get(key))
            if values:
                record.sensor_values.extend(values)
                if record.sensor_count is None:
                    record.sensor_count = channel + 1

        # --- Validation các field bắt buộc ---
        errors: List[str] = []
        if not record.gateway_uid:
            errors.append(V_MISSING_GATEWAY)
        elif not is_hex_uid(record.gateway_uid):
            # Gateway UID do firmware in bằng `String(getEfuseMac(), HEX)` nên có
            # thể LẺ ký tự (mất nibble 0 đầu) — xem `core/uid.normalize_gateway_uid`.
            errors.append(V_INVALID_UID)
        if not record.receiver_uid:
            errors.append(V_MISSING_RECEIVER)
        elif not is_hex(record.receiver_uid):
            errors.append(V_INVALID_UID)
        if record.packet_type in (PKT_DATA, PKT_INFO) and not record.node_uid:
            errors.append(V_MISSING_SENSOR)
        if record.seq is None and record.packet_type == PKT_DATA:
            errors.append(V_INVALID_SEQ)

        self._attach_gateway(record, known_gateways)

        if errors or record.validation_error == V_UNKNOWN_TOPIC:
            record.valid = False
            record.validation_error = ",".join(
                [e for e in errors if e] + ([record.validation_error] if record.validation_error and record.validation_error != V_OK else [])
            ) or "INVALID"
        else:
            record.valid = True
            # CRC được kiểm ở gateway TRƯỚC khi publish (data_process.cpp:214-241)
            # -> tool không kiểm lại, chỉ ghi nhận nguồn hợp lệ.
            record.validation_error = V_OK
        return record

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _decode(payload: Any) -> str:
        if payload is None:
            return ""
        if isinstance(payload, str):
            return payload
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload).decode("utf-8", errors="replace")
        return str(payload)

    def _fill_common(self, record: MQTTRecord, data: Dict[str, Any]) -> None:
        # `gateway_id` KHÔNG zero-pad (firmware `config_manager.cpp:7`) → dùng
        # normalizer chấp nhận hex độ dài lẻ, nếu không gateway MAC có nibble 0
        # đầu sẽ thành UNKNOWN_GATEWAY.
        record.gateway_uid = normalize_gateway_uid(data.get("gateway_id"))
        record.receiver_uid = normalize_uid(data.get("receiver_id"))
        record.rssi = _as_float(data.get("rssi"))
        record.snr = _as_float(data.get("snr"))
        record.mcu_temp = _as_float(data.get("mcu_temp"))
        record.mcu_vdd = _as_float(data.get("mcu_vdd"))
        ts = data.get("timestamp")
        if isinstance(ts, str):
            record.gateway_timestamp = ts.strip()
        elif ts is not None:
            record.gateway_timestamp = str(ts)

    def _fill_log(self, record: MQTTRecord, data: Dict[str, Any]) -> None:
        gid = normalize_gateway_uid(data.get("uuid"))
        if not gid:
            # fallback: lấy UID từ topic <model>/<uuid>/log (UUID = gateway UID)
            parts = record.topic.split("/")
            if len(parts) == 3:
                gid = normalize_gateway_uid(parts[1])
        record.gateway_uid = gid
        record.status = str(data.get("status", "") or "")
        record.gateway_timestamp = str(data.get("timestamp", "") or "")
        record.payload_json = _clean_for_csv(json_dumps_safe(data))

    def _attach_gateway(
        self, record: MQTTRecord, known_gateways: Optional[Dict[str, str]]
    ) -> None:
        if known_gateways is None:
            record.gateway_name = ""
            return
        if record.gateway_uid and record.gateway_uid in known_gateways:
            record.gateway_name = known_gateways[record.gateway_uid]
        elif record.gateway_uid:
            record.gateway_name = "UNKNOWN_GATEWAY"
        else:
            record.gateway_name = "UNKNOWN_GATEWAY"


# ---------------------------------------------------------------------------
# Log record builder (dùng ở tầng pipeline)
# ---------------------------------------------------------------------------
def to_log_record(record: MQTTRecord) -> Dict[str, Any]:
    """Tách các field log gateway thành dict cho `mqtt_gateway_log_*.csv`."""
    return {
        "received_at": record.received_at,
        "gateway_uid": record.gateway_uid,
        "gateway_name": record.gateway_name,
        "topic": record.topic,
        "status": record.status,
        "gateway_timestamp": record.gateway_timestamp,
        "payload_json": record.payload_json,
    }


def gateway_uid_hint(uid: str) -> str:
    """Chuỗi MAC gợi ý từ gateway UID (chỉ để hiển thị/hỗ trợ nhập liệu)."""
    return format_mac_from_gateway_uid(uid)


# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------
def _extract_int(data: Dict[str, Any], key: str) -> Tuple[bool, Optional[int]]:
    """Trả (có_mặt, giá_trị). Chấp nhận cả số và chuỗi số."""
    if key not in data:
        return False, None
    value = data.get(key)
    if isinstance(value, bool):
        return True, int(value)
    if isinstance(value, int):
        return True, value
    if isinstance(value, float):
        return True, int(value)
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return True, None
        try:
            return True, int(float(text))
        except ValueError:
            return True, None
    return True, None


def _as_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _as_float_list(value: Any) -> List[float]:
    """Chuẩn hoá `sensor_value_<ch>`.

    Firmware luôn gửi mảng (`data_process.cpp:401-412`), nhưng node cũ / forward
    tool có thể gửi số đơn → chấp nhận cả hai.
    """
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: List[float] = []
    for item in items:
        number = _as_float(item)
        if number is not None:
            out.append(number)
    return out


def _raw_data_to_hex(raw: Any) -> str:
    """`raw_data` trong node/error là mảng byte (int) → hex in hoa."""
    if not isinstance(raw, (list, tuple)):
        return ""
    parts: List[str] = []
    for item in raw[: _HEX_BYTE_PRINT_LIMIT]:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            parts.append("%02X" % (item & 0xFF))
        elif isinstance(item, str) and item.strip():
            parts.append(item.strip().upper())
    return "".join(parts)


def _clean_for_csv(text: str) -> str:
    """1 dòng duy nhất, không newline (giữ nguyên JSON gốc để forensic)."""
    if not text:
        return ""
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
