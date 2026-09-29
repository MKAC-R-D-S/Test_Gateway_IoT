"""Đối chiếu MQTT ↔ ping để phân biệt lỗi ở gateway hay ở đường mạng.

Mục tiêu của test dài ngày: trả lời câu hỏi
  "Gateway mất/không gửi dữ liệu là do gateway chết, hay do đường mạng/điện?"

Cách làm: chia trục thời gian thành các **window** cố định (mặc định 60 s — bằng
chu kỳ report hệ thống của firmware). Với mỗi gateway, mỗi window phân loại:

| Window | Diễn giải |
|---|---|
| ping OK + có MQTT | bình thường |
| ping OK + KHÔNG có MQTT | **gateway treo / node không gửi** (mạng còn sống) |
| ping FAIL + KHÔNG có MQTT | **mất mạng/điện** (không kết luận được về gateway) |
| ping FAIL + VẪN có MQTT | **ping bị chặn/lỗi công cụ** (gateway vẫn hoạt động!) |

Đây là lý do KHÔNG được dùng ping FAIL đơn thuần làm bằng chứng gateway chết
(xem docs §3.5). `verdict` là kết luận tổng hợp, không phải chẩn đoán tuyệt đối.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.uid import format_gateway_uid, normalize_hex_uid

VERDICT_OK = "OK"
VERDICT_LINK_DOWN = "LINK_DOWN"            # mất mạng (ping fail + mqtt im)
VERDICT_GATEWAY_SILENT = "GATEWAY_SILENT"  # mạng sống, gateway im
VERDICT_PING_BLOCKED = "PING_BLOCKED"      # ping fail nhưng mqtt vẫn về
VERDICT_PARTIAL = "PARTIAL"                # lẫn lộn nhiều loại
VERDICT_NO_DATA = "NO_DATA"                # không đủ dữ liệu kết luận

VERDICT_LABELS = {
    VERDICT_OK: "Bình thường",
    VERDICT_LINK_DOWN: "Mất kết nối mạng/điện",
    VERDICT_GATEWAY_SILENT: "Gateway không gửi dữ liệu (mạng vẫn sống)",
    VERDICT_PING_BLOCKED: "Ping bị chặn (gateway vẫn gửi MQTT)",
    VERDICT_PARTIAL: "Bất thường hỗn hợp",
    VERDICT_NO_DATA: "Không đủ dữ liệu",
}


@dataclass
class WindowCell:
    """Một window 60 s của một gateway."""

    start: datetime
    ping_total: int = 0
    ping_ok: int = 0
    mqtt_packets: int = 0
    gateway_log_online: bool = False
    gateway_log_offline: bool = False

    @property
    def ping_state(self) -> str:
        if self.ping_total == 0:
            return "UNKNOWN"
        if self.ping_ok == 0:
            return "DOWN"
        if self.ping_ok == self.ping_total:
            return "OK"
        return "FLAKY"

    @property
    def mqtt_state(self) -> str:
        return "SILENT" if self.mqtt_packets == 0 else "ACTIVE"

    def classify(self) -> str:
        ping = self.ping_state
        mqtt = self.mqtt_state
        if ping == "UNKNOWN" and mqtt == "SILENT":
            return "UNKNOWN"
        if mqtt == "ACTIVE" and ping in ("OK", "FLAKY"):
            return "NORMAL"
        if mqtt == "SILENT" and ping == "OK":
            return "GATEWAY_SILENT"
        if mqtt == "SILENT" and ping in ("DOWN", "FLAKY"):
            return "LINK_DOWN"
        if mqtt == "ACTIVE" and ping == "DOWN":
            return "PING_BLOCKED"
        return "UNKNOWN"


@dataclass
class GatewayCorrelation:
    gateway_uid: str = ""
    gateway_name: str = ""
    ip_address: str = ""
    windows_total: int = 0
    windows_ping_ok: int = 0
    windows_ping_down: int = 0
    windows_ping_down_mqtt_silent: int = 0
    windows_ping_ok_mqtt_silent: int = 0
    windows_ping_blocked: int = 0
    windows_normal: int = 0
    windows_unknown: int = 0
    mqtt_packets: int = 0
    offline_markers: int = 0
    verdict: str = VERDICT_NO_DATA
    notes: str = ""
    silent_windows: List[Tuple[str, str]] = field(default_factory=list)

    def to_row(self) -> Dict[str, Any]:
        return {
            "gateway_uid": self.gateway_uid,
            "gateway_name": self.gateway_name,
            "ip_address": self.ip_address,
            "windows_total": self.windows_total,
            "windows_ping_ok": self.windows_ping_ok,
            "windows_ping_down": self.windows_ping_down,
            "windows_ping_down_mqtt_silent": self.windows_ping_down_mqtt_silent,
            "windows_ping_ok_mqtt_silent": self.windows_ping_ok_mqtt_silent,
            "mqtt_packets": self.mqtt_packets,
            "verdict": self.verdict,
            "notes": self.notes,
        }


@dataclass
class CorrelationReport:
    window_seconds: int = 60
    gateways: List[GatewayCorrelation] = field(default_factory=list)
    windows: Dict[str, List[WindowCell]] = field(default_factory=dict)
    # Khoảng "MQTT im lặng" phát hiện được (>= silent_threshold windows liên tiếp)
    silence_events: List[Dict[str, Any]] = field(default_factory=list)

    def overall_verdict(self) -> str:
        if not self.gateways:
            return VERDICT_NO_DATA
        verdicts = {g.verdict for g in self.gateways}
        if verdicts == {VERDICT_OK}:
            return VERDICT_OK
        if len(verdicts) == 1:
            return next(iter(verdicts))
        if VERDICT_GATEWAY_SILENT in verdicts or VERDICT_LINK_DOWN in verdicts:
            return VERDICT_PARTIAL
        return VERDICT_PARTIAL

    def summary_rows(self) -> List[Dict[str, Any]]:
        return [g.to_row() for g in self.gateways]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
def _floor(value: datetime, window_seconds: int) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=value.tzinfo)
    delta = (value - epoch).total_seconds()
    return epoch + timedelta(seconds=(int(delta) // window_seconds) * window_seconds)


def _payload_gateway_uid(payload: Any) -> str:
    """Gateway UID lấy từ `payload_json` (dùng cho CSV ghi bởi phiên bản cũ).

    Phiên bản cũ để trống `gateway_uid` / `gateways` khi gateway UID dài 11 ký tự,
    nhưng payload gốc vẫn nguyên trong CSV — đó là nguồn sự thật cuối cùng.
    """
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("gateway_id", "uuid"):
        value = normalize_hex_uid(data.get(key))
        if value:
            return value
    return ""


def row_gateways(row: Dict[str, Any]) -> List[str]:
    """Danh sách gateway đã nghe bản tin mà dòng `mqtt_data` này đại diện.

    Sau khi gộp bản tin trùng (`analytics/packet_deduper.py`), cột `gateway_uid`
    chỉ là gateway có **RSSI tốt nhất**, không phải gateway duy nhất. Nếu chỉ
    dùng `gateway_uid` thì mọi gateway khác đều bị coi là "im lặng" oan, nên phải
    quy bản tin cho **tất cả** gateway trong cột `gateways` (phân cách '|').

    UID được chuẩn hoá về dạng 12 hex để `8F9199E139C` (firmware in, không
    zero-pad) và `08F9199E139C` (MAC đầy đủ) không bị đếm thành 2 gateway.

    Các dòng CSV cũ có thể để trống cột `gateways`/`gateway_uid`; khi đó lấy
    gateway thật từ `payload_json` (xem `storage.reader`) để bản tin vấn được
    quy cho đúng gateway thay vì bị coi là không xác định.
    """
    found: List[str] = []
    raw = str(row.get("gateways", "") or "").strip()
    for part in raw.split("|"):
        uid = format_gateway_uid(part)
        if uid and uid not in found:
            found.append(uid)
    # Cột `gateways` của CSV cũ có thể THIẾU gateway UID 11 hex (lỗi đã sửa), nên
    # phải hợp nhất thêm gateway ghi trong `gateway_uid` và trong `payload_json`.
    # Với CSV mới đây là các giá trị đã có sẵn nên kết quả không đổi.
    for candidate in (row.get("gateway_uid", ""), _payload_gateway_uid(row.get("payload_json", ""))):
        uid = format_gateway_uid(candidate)
        if uid and uid not in found:
            found.append(uid)
    return found


def correlate(
    ping_rows: Sequence[Dict[str, Any]],
    mqtt_rows: Sequence[Dict[str, Any]],
    log_rows: Sequence[Dict[str, Any]] = (),
    window_seconds: int = 60,
    silent_threshold: int = 2,
) -> CorrelationReport:
    """Đối chiếu ping / mqtt / gateway-log thành report.

    Tham số:
      ping_rows : dòng `ping_log_*.csv`
      mqtt_rows : dòng `mqtt_data_*.csv` (chỉ dùng packet_type DATA/DATA_NO_SEQ)
      log_rows  : dòng `mqtt_gateway_log_*.csv` (status online/offline)
      silent_threshold : số window IM LIÊN TIẾP tối thiểu coi là sự kiện im lặng
    """
    window_seconds = max(1, int(window_seconds))
    cells: Dict[str, Dict[datetime, WindowCell]] = {}
    meta: Dict[str, Dict[str, str]] = {}
    packet_counts: Dict[str, int] = {}
    offline_markers: Dict[str, int] = {}

    def ensure(gateway: str, start: datetime) -> WindowCell:
        gateway = format_gateway_uid(gateway)
        per_gateway = cells.setdefault(gateway, {})
        cell = per_gateway.get(start)
        if cell is None:
            cell = WindowCell(start=start)
            per_gateway[start] = cell
        return cell

    # ---- ping ----
    for row in ping_rows:
        gateway = format_gateway_uid(row.get("gateway_uid", ""))
        stamp = _parse(row.get("timestamp"))
        if not gateway or stamp is None:
            continue
        info = meta.setdefault(gateway, {})
        info.setdefault("name", str(row.get("gateway_name", "")))
        info.setdefault("ip", str(row.get("ip_address", "")))
        cell = ensure(gateway, _floor(stamp, window_seconds))
        cell.ping_total += 1
        if str(row.get("ping_success", "")).strip().lower() in ("1", "true", "yes"):
            cell.ping_ok += 1

    # ---- mqtt data packets ----
    for row in mqtt_rows:
        packet_type = str(row.get("packet_type", "")).strip().upper()
        if packet_type not in ("DATA", "DATA_NO_SEQ", "INFO"):
            continue
        gateways = row_gateways(row)
        stamp = _parse(row.get("received_at"))
        if not gateways or stamp is None:
            continue
        for gateway in gateways:
            info = meta.setdefault(gateway, {})
            info.setdefault("name", str(row.get("gateway_name", "")))
            packet_counts[gateway] = packet_counts.get(gateway, 0) + 1
            ensure(gateway, _floor(stamp, window_seconds)).mqtt_packets += 1

    # ---- gateway log (online/offline markers) ----
    for row in log_rows:
        gateway = format_gateway_uid(row.get("gateway_uid", ""))
        stamp = _parse(row.get("received_at"))
        if not gateway or stamp is None:
            continue
        info = meta.setdefault(gateway, {})
        info.setdefault("name", str(row.get("gateway_name", "")))
        status = str(row.get("status", "")).strip().lower()
        cell = ensure(gateway, _floor(stamp, window_seconds))
        if status == "offline":
            cell.gateway_log_offline = True
            offline_markers[gateway] = offline_markers.get(gateway, 0) + 1
        elif status == "online":
            cell.gateway_log_online = True

    # ---- gom kết quả ----
    report = CorrelationReport(window_seconds=window_seconds)
    for gateway, per_gateway in cells.items():
        info = meta.get(gateway, {})
        summary = GatewayCorrelation(
            gateway_uid=gateway,
            gateway_name=info.get("name", ""),
            ip_address=info.get("ip", ""),
            mqtt_packets=packet_counts.get(gateway, 0),
            offline_markers=offline_markers.get(gateway, 0),
        )
        ordered = [per_gateway[key] for key in sorted(per_gateway.keys())]
        report.windows[gateway] = ordered

        silent_run: List[WindowCell] = []
        for cell in ordered:
            summary.windows_total += 1
            kind = cell.classify()
            if cell.ping_total:
                if cell.ping_ok == cell.ping_total:
                    summary.windows_ping_ok += 1
                elif cell.ping_ok == 0:
                    summary.windows_ping_down += 1
            if kind == "NORMAL":
                summary.windows_normal += 1
            elif kind == "GATEWAY_SILENT":
                summary.windows_ping_ok_mqtt_silent += 1
            elif kind == "LINK_DOWN":
                summary.windows_ping_down_mqtt_silent += 1
            elif kind == "PING_BLOCKED":
                summary.windows_ping_blocked += 1
            else:
                summary.windows_unknown += 1

            # Theo dõi chuỗi window MQTT im lặng
            if cell.mqtt_packets == 0 and cell.ping_total > 0:
                silent_run.append(cell)
            else:
                if len(silent_run) >= silent_threshold:
                    report.silence_events.append(
                        _silence_event(gateway, silent_run, window_seconds, "MQTT_SILENT")
                    )
                silent_run = []
        if len(silent_run) >= silent_threshold:
            report.silence_events.append(
                _silence_event(gateway, silent_run, window_seconds, "MQTT_SILENT")
            )

        summary.verdict = _verdict(summary)
        summary.notes = _notes(summary)
        report.gateways.append(summary)

    report.gateways.sort(key=lambda g: g.gateway_uid)
    report.silence_events.sort(key=lambda e: (e["gateway_uid"], e["start"]))
    return report


def _silence_event(
    gateway: str, cells: List[WindowCell], window_seconds: int, kind: str
) -> Dict[str, Any]:
    start = cells[0].start
    end = cells[-1].start + timedelta(seconds=window_seconds)
    ping_ok_windows = sum(1 for c in cells if c.ping_state == "OK")
    ping_down_windows = sum(1 for c in cells if c.ping_state in ("DOWN", "FLAKY"))
    return {
        "gateway_uid": gateway,
        "kind": kind,
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "duration_seconds": len(cells) * window_seconds,
        "windows": len(cells),
        "ping_ok_windows": ping_ok_windows,
        "ping_down_windows": ping_down_windows,
        "interpretation": (
            "Mạng vẫn sống nhưng gateway không gửi → nghi gateway treo / node ngừng gửi"
            if ping_down_windows == 0
            else "Không ping được và không có MQTT → nghi mất mạng hoặc mất điện"
        ),
    }


def _verdict(summary: GatewayCorrelation) -> str:
    has_ping = summary.windows_ping_ok + summary.windows_ping_down > 0
    if summary.windows_total == 0 or (not has_ping and summary.mqtt_packets == 0):
        return VERDICT_NO_DATA
    concerns = (
        summary.windows_ping_down_mqtt_silent
        + summary.windows_ping_ok_mqtt_silent
        + summary.windows_ping_blocked
    )
    if concerns == 0:
        return VERDICT_OK
    if not has_ping:
        return VERDICT_NO_DATA
    # Ưu tiên kết luận mạnh nhất khi chỉ có 1 loại vấn đề chiếm ưu thế
    kinds = {
        VERDICT_LINK_DOWN: summary.windows_ping_down_mqtt_silent,
        VERDICT_GATEWAY_SILENT: summary.windows_ping_ok_mqtt_silent,
        VERDICT_PING_BLOCKED: summary.windows_ping_blocked,
    }
    dominant = max(kinds, key=lambda k: kinds[k])
    if kinds[dominant] > 0 and kinds[dominant] >= 0.7 * concerns:
        return dominant
    return VERDICT_PARTIAL


def _notes(summary: GatewayCorrelation) -> str:
    parts: List[str] = []
    if summary.windows_ping_ok_mqtt_silent:
        parts.append(
            "%d window ping OK nhưng MQTT im lặng" % summary.windows_ping_ok_mqtt_silent
        )
    if summary.windows_ping_down_mqtt_silent:
        parts.append(
            "%d window ping fail + MQTT im lặng" % summary.windows_ping_down_mqtt_silent
        )
    if summary.windows_ping_blocked:
        parts.append(
            "%d window ping fail nhưng MQTT vẫn về (nghi ping bị chặn)"
            % summary.windows_ping_blocked
        )
    if summary.offline_markers:
        parts.append("gateway báo offline %d lần (Last Will)" % summary.offline_markers)
    if not parts:
        parts.append("Không phát hiện bất thường ở mức window %ds")
    return "; ".join(parts)


def _parse(text: Any) -> Optional[datetime]:
    if isinstance(text, datetime):
        return text
    text = str(text or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[: len(fmt)], fmt)
        except ValueError:
            continue
    return None
