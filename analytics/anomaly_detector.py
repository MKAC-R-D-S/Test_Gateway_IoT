"""Phát hiện & cảnh báo bất thường ở mức "sự kiện" (event), không phải per-packet.

Ba nhóm cảnh báo:

1. **Node im lặng** — node đã từng gửi nhưng không gửi trong `node_silent_seconds`
   (mặc định 600 s). Đây là cảnh báo quan trọng nhất của test dài ngày: node chết
   pin / mất LoRa / gateway bỏ sót đều biểu hiện giống nhau ở phía MQTT.
2. **Tỷ lệ mất SEQ cao** — node có `missing / (packets + missing)` vượt ngưỡng
   trong cửa sổ quan sát.
3. **Mất gateway** — không nhận được BẤT KỲ bản tin nào từ gateway trong
   `gateway_silent_seconds`.

Mỗi cảnh báo được **dedupe**: một sự kiện đang mở sẽ không tạo cảnh báo mới cho
tới khi nó đóng lại (tránh spam khi node im lặng suốt 3 ngày).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.models import MQTTRecord

log = logging.getLogger(__name__)

LEVEL_INFO = "INFO"
LEVEL_WARNING = "WARNING"
LEVEL_CRITICAL = "CRITICAL"

LEVEL_ORDER = {LEVEL_INFO: 0, LEVEL_WARNING: 1, LEVEL_CRITICAL: 2}


@dataclass
class AlertRule:
    """Ngưỡng cấu hình cho AnomalyDetector."""

    node_silent_seconds: int = 600
    gateway_silent_seconds: int = 300
    missing_rate_warn_percent: float = 1.0
    missing_rate_crit_percent: float = 5.0
    duplicate_rate_warn_percent: float = 2.0
    seq_reset_warn_count: int = 3
    min_packets_for_rate: int = 50
    node_silent_critical_multiplier: float = 3.0
    cooldown_seconds: int = 300

    @classmethod
    def from_config(cls, cfg: Any) -> "AlertRule":
        rule = cls()
        for name in cls.__dataclass_fields__:
            if hasattr(cfg, name):
                try:
                    setattr(rule, name, getattr(cfg, name))
                except (TypeError, ValueError):
                    continue
        return rule


@dataclass
class AlertRecord:
    """Một cảnh báo đã chốt (ghi CSV `alerts_*.csv` + hiện trên GUI)."""

    timestamp: str
    level: str
    alert_type: str
    gateway_uid: str = ""
    gateway_name: str = ""
    receiver_uid: str = ""
    node_uid: str = ""
    message: str = ""
    details: str = ""
    value: Optional[float] = None

    def to_row(self) -> Dict[str, Any]:
        row = dict(self.__dict__)
        return row

    def key(self) -> Tuple[str, str, str]:
        return (self.alert_type, self.gateway_uid, self.node_uid or self.receiver_uid)


@dataclass
class _NodeWatch:
    """Trạng thái theo dõi một node để phát hiện im lặng."""

    key: str
    gateway_uid: str
    gateway_name: str
    receiver_uid: str
    node_uid: str
    last_seen_dt: datetime
    last_seen_text: str
    packets: int = 0
    silent_open: bool = False
    silent_started: str = ""


class AnomalyDetector:
    """Nhận record theo thời gian thực, sinh cảnh báo mức sự kiện."""

    def __init__(self, rule: Optional[AlertRule] = None) -> None:
        self.rule = rule or AlertRule()
        self._nodes: Dict[str, _NodeWatch] = {}
        self._last_gateway_seen: Dict[str, Tuple[datetime, str, str]] = {}
        self._alerts: List[AlertRecord] = []
        self._last_alert_at: Dict[Tuple[str, str, str], datetime] = {}
        self._gateway_silent_open: Dict[str, bool] = {}

    # ------------------------------------------------------------------ API
    def feed(self, record: MQTTRecord, now: Optional[datetime] = None) -> List[AlertRecord]:
        """Cập nhật theo dõi với 1 record. Trả danh sách cảnh báo MỚI (nếu có)."""
        stamp = now or _parse(record.received_at) or datetime.now().astimezone()
        emitted: List[AlertRecord] = []
        emitted.extend(self._track_gateway(record, stamp))
        emitted.extend(self._track_node(record, stamp))
        self._alerts.extend(emitted)
        return emitted

    def check_timeouts(self, now: Optional[datetime] = None) -> List[AlertRecord]:
        """Quét node/gateway im lặng — gọi định kỳ (mỗi vài giây) từ pipeline."""
        stamp = now or datetime.now().astimezone()
        emitted: List[AlertRecord] = []
        for watch in self._nodes.values():
            idle = (stamp - _align(watch.last_seen_dt, stamp)).total_seconds()
            limit = self.rule.node_silent_seconds
            if idle < limit:
                if watch.silent_open:
                    watch.silent_open = False
                continue
            if watch.silent_open:
                continue
            critical = idle >= limit * max(1.0, self.rule.node_silent_critical_multiplier)
            alert = AlertRecord(
                timestamp=stamp.isoformat(timespec="seconds"),
                level=LEVEL_CRITICAL if critical else LEVEL_WARNING,
                alert_type="NODE_SILENT",
                gateway_uid=watch.gateway_uid,
                gateway_name=watch.gateway_name,
                receiver_uid=watch.receiver_uid,
                node_uid=watch.node_uid,
                message="Node không gửi dữ liệu %.1f phút" % (idle / 60.0),
                details="Packet cuối lúc %s (tổng %d packet)"
                % (watch.last_seen_text, watch.packets),
                value=round(idle, 3),
            )
            watch.silent_open = True
            watch.silent_started = watch.last_seen_text
            if self._should_emit(alert, stamp):
                emitted.append(alert)

        for gateway, (seen_dt, name, uid) in list(self._last_gateway_seen.items()):
            idle = (stamp - _align(seen_dt, stamp)).total_seconds()
            if idle < self.rule.gateway_silent_seconds:
                self._gateway_silent_open[gateway] = False
                continue
            if self._gateway_silent_open.get(gateway):
                continue
            self._gateway_silent_open[gateway] = True
            alert = AlertRecord(
                timestamp=stamp.isoformat(timespec="seconds"),
                level=LEVEL_CRITICAL,
                alert_type="GATEWAY_SILENT",
                gateway_uid=gateway,
                gateway_name=name,
                message="Gateway không gửi bản tin nào trong %.1f phút" % (idle / 60.0),
                details="Bản tin cuối lúc %s" % seen_dt.isoformat(timespec="seconds"),
                value=round(idle, 3),
            )
            if self._should_emit(alert, stamp):
                emitted.append(alert)

        self._alerts.extend(emitted)
        return emitted

    def check_node_quality(self, seq_states: Dict[str, Any]) -> List[AlertRecord]:
        """Đánh giá chất lượng SEQ của từng node → cảnh báo tỷ lệ mất/lặp."""
        now = datetime.now().astimezone()
        emitted: List[AlertRecord] = []
        for key, state in seq_states.items():
            packets = int(getattr(state, "packets", 0) or 0)
            if packets < self.rule.min_packets_for_rate:
                continue
            missing = int(getattr(state, "missing", 0) or 0)
            duplicate = int(getattr(state, "duplicate", 0) or 0)
            seq_reset = int(getattr(state, "seq_reset", 0) or 0)
            total = packets + missing
            missing_pct = (100.0 * missing / total) if total else 0.0
            dup_pct = (100.0 * duplicate / packets) if packets else 0.0

            if missing_pct >= self.rule.missing_rate_crit_percent:
                emitted.append(
                    self._quality_alert(
                        state, now, "MISSING_RATE_CRITICAL", LEVEL_CRITICAL,
                        missing_pct, "Tỷ lệ mất packet cao: %.2f%% (mất %d / %d)"
                        % (missing_pct, missing, total),
                    )
                )
            elif missing_pct >= self.rule.missing_rate_warn_percent:
                emitted.append(
                    self._quality_alert(
                        state, now, "MISSING_RATE_HIGH", LEVEL_WARNING,
                        missing_pct, "Tỷ lệ mất packet: %.2f%% (mất %d / %d)"
                        % (missing_pct, missing, total),
                    )
                )
            if dup_pct >= self.rule.duplicate_rate_warn_percent:
                emitted.append(
                    self._quality_alert(
                        state, now, "DUPLICATE_RATE_HIGH", LEVEL_WARNING,
                        dup_pct, "Tỷ lệ packet lặp: %.2f%% (%d packet)"
                        % (dup_pct, duplicate),
                    )
                )
            if seq_reset >= self.rule.seq_reset_warn_count:
                emitted.append(
                    self._quality_alert(
                        state, now, "SEQ_RESET_REPEATED", LEVEL_WARNING,
                        float(seq_reset), "SEQ reset %d lần (nghi node reboot liên tục)"
                        % seq_reset,
                    )
                )
        accepted = [a for a in emitted if self._should_emit(a, now)]
        self._alerts.extend(accepted)
        return accepted

    def alerts(self) -> List[AlertRecord]:
        return list(self._alerts)

    def alerts_by_level(self) -> Dict[str, int]:
        counts = {LEVEL_INFO: 0, LEVEL_WARNING: 0, LEVEL_CRITICAL: 0}
        for alert in self._alerts:
            counts[alert.level] = counts.get(alert.level, 0) + 1
        return counts

    def latest(self, count: int = 50) -> List[AlertRecord]:
        return self._alerts[-count:]

    def reset(self) -> None:
        self._nodes.clear()
        self._last_gateway_seen.clear()
        self._alerts.clear()
        self._last_alert_at.clear()
        self._gateway_silent_open.clear()

    def shift_baselines(self, seconds: float) -> None:
        """Đẩy mọi mốc "lần cuối thấy" tới `seconds` giây về tương lai.

        Dùng khi phiên được TIẾP TỤC sau một khoảng TẠM DỪNG. Trong lúc tạm dừng
        tool không nhận bản tin nào (đúng chủ ý), nhưng bộ phát hiện vẫn so mốc
        cuối với đồng hồ thật — nếu không đẩy mốc thì vừa bấm "Tiếp tục" là có
        ngay loạt cảnh báo `NODE_SILENT` / `GATEWAY_SILENT` GIẢ, và các cảnh báo
        đó đi thẳng vào `alerts_summary.csv` của báo cáo.

        Không đụng tới danh sách cảnh báo đã phát (dữ liệu lịch sử phải giữ
        nguyên) — cờ `*_silent_open` tự được xoá ở lần quét kế tiếp vì `idle`
        tụt về ~0.
        """
        if seconds <= 0:
            return
        delta = timedelta(seconds=float(seconds))
        for watch in self._nodes.values():
            watch.last_seen_dt = watch.last_seen_dt + delta
        for gateway, (seen_dt, name, uid) in list(self._last_gateway_seen.items()):
            self._last_gateway_seen[gateway] = (seen_dt + delta, name, uid)
        for key, stamp in list(self._last_alert_at.items()):
            self._last_alert_at[key] = stamp + delta

    # ------------------------------------------------------------- nội bộ
    def _track_gateway(self, record: MQTTRecord, stamp: datetime) -> List[AlertRecord]:
        if not record.gateway_uid:
            return []
        gateway = record.gateway_uid.upper()
        previous = self._last_gateway_seen.get(gateway)
        self._last_gateway_seen[gateway] = (stamp, record.gateway_name, gateway)
        if not self._gateway_silent_open.get(gateway):
            return []
        # Gateway hoạt động trở lại → ghi cảnh báo RECOVERED
        self._gateway_silent_open[gateway] = False
        gap = ""
        if previous is not None:
            seconds = (stamp - _align(previous[0], stamp)).total_seconds()
            gap = "Sau %.1f phút im lặng" % (seconds / 60.0)
        alert = AlertRecord(
            timestamp=stamp.isoformat(timespec="seconds"),
            level=LEVEL_INFO,
            alert_type="GATEWAY_RECOVERED",
            gateway_uid=gateway,
            gateway_name=record.gateway_name,
            message="Gateway đã gửi trở lại",
            details=gap,
        )
        return [alert] if self._should_emit(alert, stamp) else []

    def _track_node(self, record: MQTTRecord, stamp: datetime) -> List[AlertRecord]:
        if not record.node_uid:
            return []
        # Khoá theo NODE (không theo gateway): nhiều gateway cùng nghe một node,
        # nếu tách theo gateway thì cùng một node im lặng sẽ sinh N cảnh báo.
        key = record.node_uid.upper()
        watch = self._nodes.get(key)
        if watch is None:
            self._nodes[key] = _NodeWatch(
                key=key,
                gateway_uid=record.gateway_uid,
                gateway_name=record.gateway_name,
                receiver_uid=record.receiver_uid,
                node_uid=record.node_uid,
                last_seen_dt=stamp,
                last_seen_text=record.received_at,
                packets=1,
            )
            return []
        watch.last_seen_dt = stamp
        watch.last_seen_text = record.received_at
        watch.gateway_name = record.gateway_name or watch.gateway_name
        watch.packets += 1
        if not watch.silent_open:
            return []
        watch.silent_open = False
        alert = AlertRecord(
            timestamp=stamp.isoformat(timespec="seconds"),
            level=LEVEL_INFO,
            alert_type="NODE_RECOVERED",
            gateway_uid=watch.gateway_uid,
            gateway_name=watch.gateway_name,
            receiver_uid=watch.receiver_uid,
            node_uid=watch.node_uid,
            message="Node đã gửi trở lại",
            details="Trước đó im lặng từ %s" % watch.silent_started,
        )
        return [alert] if self._should_emit(alert, stamp) else []

    def _quality_alert(
        self,
        state: Any,
        now: datetime,
        alert_type: str,
        level: str,
        value: float,
        message: str,
    ) -> AlertRecord:
        return AlertRecord(
            timestamp=now.isoformat(timespec="seconds"),
            level=level,
            alert_type=alert_type,
            gateway_uid=str(getattr(state, "gateway_uid", "")),
            receiver_uid=str(getattr(state, "receiver_uid", "")),
            node_uid=str(getattr(state, "node_uid", "")),
            message=message,
            details="first=%s last=%s" % (
                getattr(state, "first_seq", ""),
                getattr(state, "last_seq", ""),
            ),
            value=round(value, 4),
        )

    def _should_emit(self, alert: AlertRecord, now: datetime) -> bool:
        """Chống spam: cùng (loại, gateway, node) chỉ cảnh báo lại sau cooldown."""
        key = alert.key()
        last = self._last_alert_at.get(key)
        if last is not None:
            if (now - _align(last, now)).total_seconds() < self.rule.cooldown_seconds:
                return False
        self._last_alert_at[key] = now
        return True


def _align(value: datetime, reference: datetime) -> datetime:
    """Đưa 2 datetime về cùng 'loại' tz để trừ nhau không lỗi."""
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)
    if value.tzinfo is not None and reference.tzinfo is None:
        return value.replace(tzinfo=None)
    return value


def _parse(text: Any) -> Optional[datetime]:
    text = str(text or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_since(text: str, default_seconds: int = 300) -> timedelta:
    """Helper cho GUI: '10m' / '2h' / '90s' → timedelta."""
    text = (text or "").strip().lower()
    if not text:
        return timedelta(seconds=default_seconds)
    try:
        if text.endswith("s"):
            return timedelta(seconds=float(text[:-1]))
        if text.endswith("m"):
            return timedelta(minutes=float(text[:-1]))
        if text.endswith("h"):
            return timedelta(hours=float(text[:-1]))
        if text.endswith("d"):
            return timedelta(days=float(text[:-1]))
        return timedelta(seconds=float(text))
    except ValueError:
        return timedelta(seconds=default_seconds)


LEVEL_LABELS = {
    LEVEL_INFO: "Thông tin",
    LEVEL_WARNING: "Cảnh báo",
    LEVEL_CRITICAL: "Nghiêm trọng",
}

ALERT_LABELS: Dict[str, str] = {
    "NODE_SILENT": "Node im lặng",
    "NODE_RECOVERED": "Node hoạt động lại",
    "GATEWAY_SILENT": "Gateway im lặng",
    "GATEWAY_RECOVERED": "Gateway hoạt động lại",
    "MISSING_RATE_HIGH": "Tỷ lệ mất packet cao",
    "MISSING_RATE_CRITICAL": "Tỷ lệ mất packet rất cao",
    "DUPLICATE_RATE_HIGH": "Tỷ lệ packet lặp cao",
    "SEQ_RESET_REPEATED": "SEQ reset lặp lại",
}


__all__ = [
    "AlertRule",
    "AlertRecord",
    "AnomalyDetector",
    "LEVEL_INFO",
    "LEVEL_WARNING",
    "LEVEL_CRITICAL",
    "LEVEL_LABELS",
    "ALERT_LABELS",
    "LEVEL_ORDER",
    "parse_since",
]
