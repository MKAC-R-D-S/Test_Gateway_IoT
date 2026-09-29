"""Chấm **ĐẠT / KHÔNG ĐẠT** cho TỪNG gateway theo TỪNG tiêu chí rồi tổng hợp.

Vì sao cần module riêng thay vì thêm cột vào `correlation_summary.csv`:
  - `correlation_summary.verdict` chỉ trả lời *"ping ↔ MQTT có khớp nhau không"*
    (6 giá trị OK / LINK_DOWN / GATEWAY_SILENT / PING_BLOCKED / PARTIAL /
    NO_DATA), **không** trả lời *"gateway này có đạt yêu cầu test hay không"*.
  - Ba chỉ số cần cho kết luận (độ phủ bản tin, thời gian mất kết nối, mức im
    lặng) nằm ở ba file khác nhau → phải gom về MỘT chỗ mới so được với ngưỡng.

Nguyên tắc thiết kế (đã chốt với người dùng):

  - **Tách "mức" khỏi "lý do"**: `result` ∈ {ĐẠT, KHÔNG ĐẠT} là mức; mỗi tiêu chí
    có cột `PASS`/`FAIL`/`N/A` riêng; `result_reason` nêu đúng tiêu chí không đạt
    kèm giá trị thực vs ngưỡng (không chỉ nói "trượt").
  - **Tất cả phải đạt mới ĐẠT** — không có "đạt một phần".
  - **`N/A` ≠ `FAIL`**: gateway không bật ping thì `c_ping` là `N/A`; nếu coi là
    `FAIL` thì mọi gateway không ping đều bị đánh trượt oan.
  - **Chặn theo cỡ mẫu** (`min_packets_for_rate`): mẫu quá nhỏ → `N/A` thay vì
    `PASS`, để không kết luận "tốt" từ vài bản tin.
  - **`PING_BLOCKED` là ĐẠT, không phải trượt.** `verdict = PING_BLOCKED` nghĩa
    là ICMP bị chặn nhưng gateway VẪN gửi MQTT → gateway khoẻ, chỉ công cụ ping
    bị chặn (xem `analytics/correlation.py`). Đo trên dữ liệu thật: 3 gateway
    với ~620 000 bản tin MQTT mỗi con đều ra `PING_BLOCKED` — nếu coi đây là
    trượt thì cả phiên bị kết luận sai.
  - **`PARTIAL` là trượt.** `PARTIAL` = bất thường HỖN HỢP, thường là vừa có
    giai đoạn `PING_BLOCKED` (bình thường — ping bị chặn) vừa có giai đoạn mất
    mạng thật. Vì đã có dấu hiệu mất kết nối THẬT nên tiêu chí 3 trượt, còn
    *mức nặng* được đo bằng con số ở tiêu chí 4 và 5 (tỉ lệ ping, thời gian mất
    kết nối) chứ không đoán từ nhãn này.

Giới hạn của phương pháp (KHÔNG được ảo giác):
  - **Độ phủ bản tin** dựa trên bằng chứng MQTT: một dòng `mqtt_data_*.csv` là
    một bản tin logic ĐÃ GỘP các bản copy (xem `analytics/packet_deduper.py`),
    và cột `gateways` cho biết những gateway nào đã publish bản tin đó. Vì vậy
    `coverage_percent` là **chặn trên** của độ phủ thật: gateway bắt được sóng
    nhưng không publish vì lý do nội bộ (hàng đợi, lỗi publish) vẫn bị tính là
    hụt. Cùng giới hạn với `missed_catch` của `analytics/focused_node.py`.
  - **Mẫu số của độ phủ chỉ tính các node mà gateway ĐÓ có nghe** (ít nhất 1 bản
    tin). Nếu lấy hợp của cả cụm làm mẫu số thì một gateway đặt ở phòng khác
    không nghe được node nào đó sẽ bị trừ điểm OAN — gateway không có lỗi gì khi
    node nằm ngoài tầm sóng. Đổi lại, gateway chỉ nghe lỏm được 1/10 000 bản tin
    của một node vẫn bị tính là "biết node đó" nên tỉ lệ độ phủ rất thấp — đúng
    ý nghĩa (không "lách" được bằng cách nghe 1 bản tin rồi thôi).
  - Nếu tắt gộp bản tin (`packet_merge_enabled = False`) thì cùng một bản tin bị
    tính nhiều lần; `coverage_percent` vẫn đúng về tỉ lệ nhưng số tuyệt đối phồng.
  - **Không** phân biệt được "gateway mất mạng" với "node không phát": nếu cả
    cụm gateway cùng hụt thì bản tin đó không có trong `reference` và không ai bị
    trừ điểm. Cột `verdict` + `mqtt_silence.csv` giúp phân biệt các ca đó.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from analytics.correlation import (
    VERDICT_LABELS,
    VERDICT_NO_DATA,
    VERDICT_OK,
    VERDICT_PING_BLOCKED,
    row_gateways,
)
from core.uid import format_gateway_uid

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hằng số kết quả
# ---------------------------------------------------------------------------
PASS = "PASS"
FAIL = "FAIL"
NA = "N/A"

RESULT_PASS = "ĐẠT"
RESULT_FAIL = "KHÔNG ĐẠT"

#: `gateway_uid` của dòng tổng hợp cuối file (`reports/gateway_result.csv`).
TOTAL_UID = "TỔNG"

#: Loại bản tin tính vào độ phủ — giống `correlate()` để hai báo cáo không lệch.
COVERAGE_PACKET_TYPES = ("DATA", "DATA_NO_SEQ", "INFO")

#: (khoá cột, nhãn tiếng Việt) — thứ tự này dùng cho cả cột CSV lẫn lý do.
CRITERIA: List[Tuple[str, str]] = [
    ("c_valid", "Có dữ liệu"),
    ("c_coverage", "Độ phủ bản tin"),
    ("c_link", "Kết nối MQTT"),
    ("c_ping", "Tỉ lệ ping"),
    ("c_downtime", "Thời gian mất kết nối"),
    ("c_stable", "Ổn định MQTT"),
    ("c_late", "Bắn hụt (gửi trễ)"),
]

#: Ngưỡng mặc định — dùng khi `TestConfig` cũ chưa có khoá tương ứng.
DEFAULTS: Dict[str, Any] = {
    "min_coverage_percent": 95.0,
    "min_ping_ok_percent": 90.0,
    "max_downtime_seconds": 300.0,
    "max_silence_percent": 2.0,
    "max_late_percent": 5.0,
    "min_packets_for_rate": 50,
    # Tham số phân tích tương quan (không có tiền tố `criteria_`).
    "ping_down_threshold": 2,
    "correlation_window_seconds": 5,
    "silent_window_threshold": 2,
}


def _cfg(config: Any, name: str, default: Any) -> Any:
    """Đọc ngưỡng từ `TestConfig`, chịu được config cũ thiếu khoá."""
    value = getattr(config, "criteria_" + name, None)
    if value in (None, ""):
        value = getattr(config, name, None)
    if value in (None, ""):
        value = default
    return value


#: Các ngưỡng tiêu chí cần chốt vào `meta.json` của phiên (tên KHÔNG kèm `criteria_`).
THRESHOLD_KEYS: Tuple[str, ...] = (
    "min_coverage_percent",
    "min_ping_ok_percent",
    "max_downtime_seconds",
    "max_silence_percent",
    "max_late_percent",
    "min_packets_for_rate",
)

#: Tham số phân tích ảnh hưởng kết quả nên phải chốt cùng ngưỡng tiêu chí.
#: - `ping_down_threshold`: số lần ping fail liên tiếp mới tính là 1 lần mất kết
#:   nối → quyết định trực tiếp tiêu chí "Thời gian mất kết nối".
#: - `correlation_window_seconds` / `silent_window_threshold`: dựng lại
#:   `correlation_summary` (tiêu chí "Kết nối MQTT", "Ổn định MQTT").
ANALYSIS_KEYS: Tuple[str, ...] = (
    "ping_down_threshold",
    "correlation_window_seconds",
    "silent_window_threshold",
)


def _analysis_param(config: Any, name: str, default: int) -> int:
    """Đọc tham số phân tích từ `TestConfig` (tên KHÔNG có tiền tố `criteria_`)."""
    value = getattr(config, name, None)
    if value in (None, ""):
        value = default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def scoring_params_snapshot(config: Any) -> Dict[str, Any]:
    """Ảnh chụp MỌI tham số ảnh hưởng kết quả chấm → chốt vào `meta.json`.

    Vì sao phải chốt: `config.json` có thể bị sửa SAU khi phiên chạy xong. Nếu
    lúc CHẤM LẠI mà đọc tham số hiện tại thì một phiên đã đạt có thể bị kết luận
    trượt (hoặc ngược lại) chỉ vì ai đó đổi ngưỡng — phiên test 3-4 ngày không
    được phép "tự đổi kết quả". Chốt vào `meta.json` giữ kết quả bất biến.

    Ngưỡng tiêu chí nằm ở tiền tố `criteria_`; tham số tương quan KHÔNG có tiền
    tố đó (xem `analysis/correlation.py`), nên phải chốt riêng.
    """
    snapshot: Dict[str, Any] = {
        name: _cfg(config, name, DEFAULTS[name]) for name in THRESHOLD_KEYS
    }
    for name in ANALYSIS_KEYS:
        snapshot[name] = _analysis_param(
            config, name, int(DEFAULTS.get(name, 5))
        )
    return snapshot


class ScoringParams:
    """Config giả để chấm lại theo tham số đã chốt trong `meta.json`.

    Dùng đúng giao diện mà `_cfg` tìm (`criteria_<tên>` rồi `<tên>`) nên
    `GatewayResultEvaluator` không cần biết nó đang đọc config thật hay ảnh chụp.
    """

    def __init__(self, values: Mapping[str, Any]) -> None:
        for name in THRESHOLD_KEYS:
            raw = values.get(name)
            if raw in (None, ""):
                continue
            if name == "min_packets_for_rate":
                setattr(self, "criteria_" + name, int(float(raw)))
            else:
                setattr(self, "criteria_" + name, float(raw))
        for name in ANALYSIS_KEYS:
            raw = values.get(name)
            if raw in (None, ""):
                continue
            setattr(self, name, int(float(raw)))


# ---------------------------------------------------------------------------
# Độ phủ bản tin theo từng gateway (tính từ `mqtt_data_*.csv`)
# ---------------------------------------------------------------------------
def compute_coverage(
    mqtt_rows: Iterable[Mapping[str, Any]],
) -> Tuple[Dict[str, Dict[str, int]], Dict[str, int]]:
    """Đếm bản tin theo gateway và theo node trên luồng MQTT ĐÃ GỘP.

    Trả `(heard, reference)`:

      ``heard[gateway][node]``  số bản tin gateway đó đã publish cho node
      ``reference[node]``       số bản tin node đã phát (theo bằng chứng MQTT)

    KHÔNG cần khử trùng lặp ở đây: mỗi dòng `mqtt_data_*.csv` là **một bản tin
    logic** đã gộp mọi bản copy của các gateway cùng nghe, nên cặp
    `(gateway, node, seq)` không lặp giữa các dòng. Nhờ vậy chỉ cần bộ đếm —
    không phải giữ bitmap/set `seq` cho từng gateway (tiết kiệm hàng trăm MB khi
    có hàng chục gateway và 3-4 ngày dữ liệu).

    Chi phí: O(số dòng), bộ nhớ O(số gateway × số node).
    """
    heard: Dict[str, Dict[str, int]] = {}
    reference: Dict[str, int] = {}
    for row in mqtt_rows:
        packet_type = str(row.get("packet_type", "") or "").strip().upper()
        if packet_type not in COVERAGE_PACKET_TYPES:
            continue
        node = str(row.get("node_uid", "") or "").strip().upper()
        if not node:
            continue
        reference[node] = reference.get(node, 0) + 1
        for gateway in row_gateways(row):
            per_node = heard.setdefault(gateway, {})
            per_node[node] = per_node.get(node, 0) + 1
    return heard, reference


def expected_for_gateway(
    per_node: Mapping[str, int], reference: Mapping[str, int]
) -> int:
    """Mẫu số độ phủ cho MỘT gateway: chỉ tính các node gateway đó có nghe.

    Vì sao không dùng tổng `reference` cho mọi gateway: một gateway không nghe
    được node nào đó (ngoài tầm sóng, khác phòng) sẽ bị trừ điểm oan. Giới hạn
    mẫu số theo node mà gateway ĐÃ nghe làm tiêu chí đo đúng thứ cần đo — *khi
    nghe được node, gateway có gửi đủ bản tin lên MQTT không*.
    """
    return sum(int(reference.get(node, 0)) for node in per_node)


# ---------------------------------------------------------------------------
# Kết quả một gateway
# ---------------------------------------------------------------------------
@dataclass
class GatewayResult:
    """Kết quả ĐẠT/KHÔNG ĐẠT của MỘT gateway (hoặc dòng TỔNG)."""

    gateway_uid: str = ""
    gateway_name: str = ""
    ip_address: str = ""
    #: Số bản tin gateway này đã publish lên MQTT (từ `correlation_summary`).
    mqtt_packets: int = 0
    #: Số bản tin gateway này publish cho các node nó nghe (tử số độ phủ).
    packets_published: int = 0
    #: Mẫu số độ phủ — chỉ tính các node gateway này ĐÃ nghe (xem
    #: `expected_for_gateway`). 0 nghĩa là không tính được độ phủ → `N/A`.
    packets_expected: int = 0
    ping_total: int = 0
    ping_success_rate_percent: Optional[float] = None
    downtime_events: int = 0
    downtime_seconds: float = 0.0
    windows_total: int = 0
    #: Window MQTT im lặng (ping OK hoặc ping DOWN nhưng không có bản tin nào).
    silence_windows: int = 0
    verdict: str = ""
    #: Tỉ lệ publish bị trễ (chỉ có khi `focused_node_enabled`).
    late_percent: Optional[float] = None
    criteria: Dict[str, str] = field(default_factory=dict)
    result: str = RESULT_FAIL
    result_reason: str = ""

    # ------------------------------------------------------------- dẫn xuất
    def coverage_percent(self) -> Optional[float]:
        if self.packets_expected <= 0:
            return None
        return round(100.0 * self.packets_published / self.packets_expected, 4)

    def missed_catch_percent(self) -> Optional[float]:
        value = self.coverage_percent()
        return None if value is None else round(100.0 - value, 4)

    def silence_percent(self) -> Optional[float]:
        if self.windows_total <= 0:
            return None
        return round(100.0 * self.silence_windows / self.windows_total, 4)

    def criteria_passed(self) -> int:
        return sum(1 for value in self.criteria.values() if value == PASS)

    def criteria_failed(self) -> int:
        return sum(1 for value in self.criteria.values() if value == FAIL)

    def criteria_na(self) -> int:
        return sum(1 for value in self.criteria.values() if value == NA)

    def failed_keys(self) -> List[str]:
        return [key for key, _label in CRITERIA if self.criteria.get(key) == FAIL]

    def to_row(self) -> Dict[str, Any]:
        return {
            "gateway_uid": self.gateway_uid,
            "gateway_name": self.gateway_name,
            "ip_address": self.ip_address,
            "mqtt_packets": self.mqtt_packets,
            "packets_published": self.packets_published,
            # 0 = không đo được độ phủ (không nghe node nào) → để trống cho khỏi
            # hiểu nhầm là "kỳ vọng 0 bản tin". Dòng TỔNG cũng để trống vì mỗi
            # gateway có mẫu số riêng.
            "packets_expected": self.packets_expected if self.packets_expected > 0 else "",
            "coverage_percent": self.coverage_percent() if self.coverage_percent() is not None else "",
            "missed_catch_percent": self.missed_catch_percent() if self.missed_catch_percent() is not None else "",
            "ping_total": self.ping_total,
            "ping_success_rate_percent": (
                self.ping_success_rate_percent
                if self.ping_success_rate_percent is not None
                else ""
            ),
            "downtime_events": self.downtime_events,
            "downtime_seconds": round(self.downtime_seconds, 3),
            "windows_total": self.windows_total,
            "silence_windows": self.silence_windows,
            "silence_percent": self.silence_percent() if self.silence_percent() is not None else "",
            "verdict": self.verdict,
            "late_percent": self.late_percent if self.late_percent is not None else "",
            "c_valid": self.criteria.get("c_valid", NA),
            "c_coverage": self.criteria.get("c_coverage", NA),
            "c_link": self.criteria.get("c_link", NA),
            "c_ping": self.criteria.get("c_ping", NA),
            "c_downtime": self.criteria.get("c_downtime", NA),
            "c_stable": self.criteria.get("c_stable", NA),
            "c_late": self.criteria.get("c_late", NA),
            "criteria_passed": self.criteria_passed(),
            "criteria_failed": self.criteria_failed(),
            "criteria_na": self.criteria_na(),
            "result": self.result,
            "result_reason": self.result_reason,
        }


# ---------------------------------------------------------------------------
# Bộ chấm điểm
# ---------------------------------------------------------------------------
class GatewayResultEvaluator:
    """Gom dữ liệu nhiều nguồn → chấm từng tiêu chí → kết luận từng gateway."""

    def __init__(self, config: Any = None) -> None:
        self.min_coverage_percent = float(_cfg(config, "min_coverage_percent", DEFAULTS["min_coverage_percent"]))
        self.min_ping_ok_percent = float(_cfg(config, "min_ping_ok_percent", DEFAULTS["min_ping_ok_percent"]))
        self.max_downtime_seconds = float(_cfg(config, "max_downtime_seconds", DEFAULTS["max_downtime_seconds"]))
        self.max_silence_percent = float(_cfg(config, "max_silence_percent", DEFAULTS["max_silence_percent"]))
        self.max_late_percent = float(_cfg(config, "max_late_percent", DEFAULTS["max_late_percent"]))
        self.min_packets_for_rate = int(_cfg(config, "min_packets_for_rate", DEFAULTS["min_packets_for_rate"]))
        self.max_missed_percent = round(100.0 - self.min_coverage_percent, 4)

    # ------------------------------------------------------------------ API
    def evaluate(
        self,
        *,
        ping_rows: Sequence[Mapping[str, Any]] = (),
        correlation_gateways: Sequence[Any] = (),
        mqtt_rows: Iterable[Mapping[str, Any]] = (),
        focused_rows: Sequence[Mapping[str, Any]] = (),
        devices: Sequence[Any] = (),
    ) -> List[Dict[str, Any]]:
        """Trả danh sách dòng sẵn sàng ghi CSV: 1 dòng/gateway + 1 dòng TỔNG.

        Tham số:
          ping_rows            dòng `ping_summary.csv` (1 dòng/gateway)
          correlation_gateways đối tượng `GatewayCorrelation` (có `verdict`)
          mqtt_rows            dòng `mqtt_data_*.csv` — dùng tính độ phủ
          focused_rows         dòng `focused_node_summary.csv` (bỏ dòng TỔNG)
          devices              `GatewayDevice` — để gateway cấu hình mà "câm"
                               vẫn hiện thành 1 dòng KHÔNG ĐẠT
        """
        heard, reference = compute_coverage(mqtt_rows)

        ping_index = self._ping_index(ping_rows)
        corr_index = self._correlation_index(correlation_gateways)
        late_index = self._late_index(focused_rows)

        uids = self._collect_uids(
            heard=heard,
            ping_index=ping_index,
            corr_index=corr_index,
            late_index=late_index,
            devices=devices,
        )

        results: List[GatewayResult] = []
        for uid in uids:
            result = self._evaluate_one(
                uid,
                per_node=heard.get(uid) or {},
                reference=reference,
                ping=ping_index.get(uid),
                correlation=corr_index.get(uid),
                late=late_index.get(uid),
                devices=devices,
            )
            results.append(result)
        return [r.to_row() for r in results] + [self._overall(results).to_row()]

    # ------------------------------------------------------------- nội bộ
    @staticmethod
    def _collect_uids(
        *,
        heard: Mapping[str, Mapping[str, int]],
        ping_index: Mapping[str, Any],
        corr_index: Mapping[str, Any],
        late_index: Mapping[str, Any],
        devices: Sequence[Any],
    ) -> List[str]:
        uids: List[str] = []
        for source in (heard.keys(), ping_index.keys(), corr_index.keys(), late_index.keys()):
            for uid in source:
                if uid and uid not in uids:
                    uids.append(uid)
        for device in devices or ():
            uid = format_gateway_uid(getattr(device, "uid", ""))
            if uid and uid not in uids:
                uids.append(uid)
        return sorted(uids)

    @staticmethod
    def _ping_index(ping_rows: Sequence[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
        index: Dict[str, Mapping[str, Any]] = {}
        for row in ping_rows or ():
            uid = format_gateway_uid(row.get("gateway_uid", ""))
            if uid:
                index.setdefault(uid, row)
        return index

    @staticmethod
    def _correlation_index(gateways: Sequence[Any]) -> Dict[str, Any]:
        index: Dict[str, Any] = {}
        for item in gateways or ():
            uid = format_gateway_uid(getattr(item, "gateway_uid", ""))
            if uid:
                index.setdefault(uid, item)
        return index

    @staticmethod
    def _late_index(focused_rows: Sequence[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
        """Bảng tra trễ từ `focused_node_summary.csv`, bỏ dòng TỔNG."""
        index: Dict[str, Mapping[str, Any]] = {}
        for row in focused_rows or ():
            uid = format_gateway_uid(row.get("gateway_uid", ""))
            if not uid or uid == "ALL":
                continue
            value = row.get("late_percent")
            if value in (None, ""):
                continue
            index.setdefault(uid, row)
        return index

    def _evaluate_one(
        self,
        uid: str,
        *,
        per_node: Mapping[str, int],
        reference: Mapping[str, int],
        ping: Optional[Mapping[str, Any]],
        correlation: Any,
        late: Optional[Mapping[str, Any]],
        devices: Sequence[Any],
    ) -> GatewayResult:
        result = GatewayResult(gateway_uid=uid)
        result.gateway_name = self._name_for(uid, ping=ping, correlation=correlation, devices=devices)

        result.packets_published = int(sum(per_node.values()))
        # Mẫu số chỉ tính node gateway này ĐÃ nghe (xem `expected_for_gateway`).
        result.packets_expected = expected_for_gateway(per_node, reference)

        if ping is not None:
            result.ip_address = str(ping.get("ip_address", "") or "")
            result.ping_total = int(_as_int(ping.get("ping_total")))
            rate = ping.get("success_rate_percent")
            result.ping_success_rate_percent = None if rate in (None, "") else float(rate)
            result.downtime_events = int(_as_int(ping.get("downtime_events")))
            result.downtime_seconds = float(_as_float(ping.get("downtime_seconds")))

        if correlation is not None:
            result.mqtt_packets = int(_as_int(getattr(correlation, "mqtt_packets", 0)))
            result.verdict = str(getattr(correlation, "verdict", "") or "")
            result.windows_total = int(_as_int(getattr(correlation, "windows_total", 0)))
            result.silence_windows = int(
                _as_int(getattr(correlation, "windows_ping_ok_mqtt_silent", 0))
            ) + int(_as_int(getattr(correlation, "windows_ping_down_mqtt_silent", 0)))
            if not result.gateway_name:
                result.gateway_name = str(getattr(correlation, "gateway_name", "") or "")
            if not result.ip_address:
                result.ip_address = str(getattr(correlation, "ip_address", "") or "")

        if late is not None:
            value = late.get("late_percent")
            result.late_percent = None if value in (None, "") else float(value)

        result.criteria = self._criteria(result)
        result.result = RESULT_FAIL if result.criteria_failed() else RESULT_PASS
        result.result_reason = self._reason(result)
        return result

    # ---------------------------------------------------------- tiêu chí
    def _criteria(self, gw: GatewayResult) -> Dict[str, str]:
        guard = max(1, int(self.min_packets_for_rate))
        out: Dict[str, str] = {}

        # 1. Có dữ liệu — gateway phải publish ít nhất 1 bản tin.
        out["c_valid"] = PASS if (gw.packets_published > 0 or gw.mqtt_packets > 0) else FAIL

        # 2. Độ phủ bản tin — hụt so với mẫu số của CHÍNH gateway này (các node
        #    nó đã nghe). Chỉ chấm khi mẫu đủ lớn.
        missed = gw.missed_catch_percent()
        if missed is None or gw.packets_expected < guard:
            out["c_coverage"] = NA
        else:
            out["c_coverage"] = PASS if missed <= self.max_missed_percent else FAIL

        # 3. Kết nối MQTT — dựa trên `verdict` của `correlation_summary`:
        #      OK           → gateway vừa ping được vừa gửi dữ liệu: PASS
        #      PING_BLOCKED → ping không được nhưng MQTT ĐẦY ĐỦ mọi window:
        #                     PASS (ICMP bị chặn, gateway khoẻ). Đo trên dữ liệu
        #                     thật: 3 gateway × ~620 000 bản tin, mọi window
        #                     đều PING_BLOCKED.
        #      LINK_DOWN    → ping fail VÀ MQTT im lặng: mất mạng/điện → FAIL
        #      GATEWAY_SILENT → ping OK nhưng MQTT im lặng: gateway bị treo →
        #                     FAIL (tiêu chí 6 chắc chắn cũng trượt)
        #      PARTIAL      → bất thường HỖN HỢP: có giai đoạn PING_BLOCKED
        #                     (bình thường khi ping bị chặn) lẫn giai đoạn mất
        #                     mạng thật. Có dấu hiệu mất kết nối thật nên FAIL.
        #      NO_DATA / "" → chưa kết luận được → N/A
        #    Ranh giới "bất thường nặng hay nhẹ" KHÔNG chấm ở đây mà ở tiêu chí 4
        #    và 5 (tỉ lệ ping + thời gian mất kết nối) — nơi có con số so ngưỡng.
        verdict = gw.verdict
        if verdict in (VERDICT_OK, VERDICT_PING_BLOCKED):
            out["c_link"] = PASS
        elif verdict in (VERDICT_NO_DATA, ""):
            out["c_link"] = NA
        else:
            out["c_link"] = FAIL

        # 4. Tỉ lệ ping — N/A khi không bật ping hoặc mẫu quá nhỏ.
        if gw.ping_total <= 0 or gw.ping_success_rate_percent is None:
            out["c_ping"] = NA
        elif gw.ping_total < guard:
            out["c_ping"] = NA
        else:
            out["c_ping"] = (
                PASS if gw.ping_success_rate_percent >= self.min_ping_ok_percent else FAIL
            )

        # 5. Thời gian mất kết nối — chỉ đo được khi có ping.
        if gw.ping_total <= 0:
            out["c_downtime"] = NA
        else:
            out["c_downtime"] = (
                PASS if gw.downtime_seconds <= self.max_downtime_seconds else FAIL
            )

        # 6. Ổn định MQTT — tỉ lệ window im lặng trên tổng số window.
        silence = gw.silence_percent()
        if gw.windows_total <= 0 or silence is None:
            out["c_stable"] = NA
        else:
            out["c_stable"] = PASS if silence <= self.max_silence_percent else FAIL

        # 7. Bắn hụt — chỉ có số liệu khi bật node trọng tâm.
        if gw.late_percent is None:
            out["c_late"] = NA
        else:
            out["c_late"] = PASS if gw.late_percent <= self.max_late_percent else FAIL

        return out

    def _reason(self, gw: GatewayResult) -> str:
        failed = gw.failed_keys()
        if not failed:
            passed = gw.criteria_passed()
            na = gw.criteria_na()
            text = "%d/%d tiêu chí đạt" % (passed, len(CRITERIA))
            if na:
                text += " (%d không áp dụng)" % na
            return text
        parts = []
        for key in failed:
            label = dict(CRITERIA).get(key, key)
            parts.append("%s: %s" % (label, self._detail(gw, key)))
        return "; ".join(parts)

    def _detail(self, gw: GatewayResult, key: str) -> str:
        if key == "c_valid":
            return "không nhận được bản tin nào"
        if key == "c_coverage":
            missed = gw.missed_catch_percent() or 0.0
            return "hụt %.2f%% (cho phép ≤ %.2f%%)" % (missed, self.max_missed_percent)
        if key == "c_link":
            label = VERDICT_LABELS.get(gw.verdict, gw.verdict)
            return "kết luận MQTT↔ping: %s" % label
        if key == "c_ping":
            rate = gw.ping_success_rate_percent or 0.0
            return "ping OK %.2f%% (cần ≥ %.2f%%)" % (rate, self.min_ping_ok_percent)
        if key == "c_downtime":
            # Lưu ý: `downtime_seconds` là TỔNG thời gian ping fail chứ KHÔNG
            # phải tổng thời gian MQTT im lặng — nói rõ để không đọc nhầm.
            return "tổng %d lần ping fail %.1fs (cho phép ≤ %.1fs)" % (
                gw.downtime_events,
                gw.downtime_seconds,
                self.max_downtime_seconds,
            )
        if key == "c_stable":
            silence = gw.silence_percent() or 0.0
            return "MQTT im lặng %.2f%% window (cho phép ≤ %.2f%%)" % (
                silence,
                self.max_silence_percent,
            )
        if key == "c_late":
            late = gw.late_percent or 0.0
            return "gửi trễ %.2f%% (cho phép ≤ %.2f%%)" % (late, self.max_late_percent)
        return "không đạt"

    # ---------------------------------------------------------- tổng hợp
    def _overall(self, results: Sequence[GatewayResult]) -> GatewayResult:
        total = GatewayResult(gateway_uid=TOTAL_UID)
        if not results:
            total.gateway_name = "Không có gateway nào"
            total.criteria = {key: NA for key, _label in CRITERIA}
            total.result = RESULT_FAIL
            total.result_reason = "Không có gateway nào để đánh giá"
            return total

        total.gateway_name = "%d gateway" % len(results)
        for item in results:
            total.mqtt_packets += item.mqtt_packets
            total.packets_published += item.packets_published
            total.ping_total += item.ping_total
            total.downtime_events += item.downtime_events
            total.downtime_seconds += item.downtime_seconds
            total.windows_total += item.windows_total
            total.silence_windows += item.silence_windows
        # KHÔNG cộng `packets_expected` của các gateway: mỗi gateway có mẫu số
        # riêng (chỉ tính node nó nghe) nên cộng lại là vô nghĩa. Cột này của
        # dòng TỔNG để trống — con số tổng nằm ở `packets_published`.

        # Tỉ lệ tổng phải là bình quân GIA QUYỀN (theo mẫu số), không phải bình
        # quân cộng — nếu không gateway ping 1 lần sẽ có trọng số bằng gateway
        # ping 25 000 lần.
        weighted_ping = [
            (r.ping_total, r.ping_success_rate_percent)
            for r in results
            if r.ping_total > 0 and r.ping_success_rate_percent is not None
        ]
        if weighted_ping:
            denominator = sum(count for count, _rate in weighted_ping)
            total.ping_success_rate_percent = round(
                sum(count * rate for count, rate in weighted_ping) / denominator, 4
            )

        weighted_late = [
            (r.packets_published, r.late_percent)
            for r in results
            if r.late_percent is not None and r.packets_published > 0
        ]
        if weighted_late:
            denominator = sum(count for count, _rate in weighted_late)
            total.late_percent = round(
                sum(count * rate for count, rate in weighted_late) / denominator, 4
            )

        failed = [r for r in results if r.result == RESULT_FAIL]
        failed_names = [r.gateway_uid for r in failed]

        # Tiêu chí của dòng TỔNG: FAIL nếu có gateway trượt, PASS nếu có gateway
        # đạt, còn lại N/A. Nhờ vậy nhìn dòng TỔNG biết ngay tiêu chí nào là vấn đề
        # chung của cả phiên.
        for key, _label in CRITERIA:
            values = {r.criteria.get(key, NA) for r in results}
            if FAIL in values:
                total.criteria[key] = FAIL
            elif PASS in values:
                total.criteria[key] = PASS
            else:
                total.criteria[key] = NA

        # `verdict` của dòng TỔNG = các kết luận khác nhau, gộp lại để đối chiếu.
        verdicts: List[str] = []
        for item in results:
            if item.verdict and item.verdict not in verdicts:
                verdicts.append(item.verdict)
        total.verdict = "|".join(verdicts)

        total.result = RESULT_FAIL if failed else RESULT_PASS
        if failed:
            preview = ", ".join(failed_names[:5])
            if len(failed_names) > 5:
                preview += ", …"
            total.result_reason = "%d/%d gateway KHÔNG ĐẠT: %s" % (
                len(failed),
                len(results),
                preview,
            )
        else:
            total.result_reason = "Cả %d gateway đều đạt mọi tiêu chí áp dụng được" % len(results)
        return total

    # ---------------------------------------------------------- tên gateway
    @staticmethod
    def _name_for(
        uid: str,
        *,
        ping: Optional[Mapping[str, Any]],
        correlation: Any,
        devices: Sequence[Any],
    ) -> str:
        for source in (ping, correlation):
            if source is None:
                continue
            if isinstance(source, Mapping):
                name = str(source.get("gateway_name", "") or "").strip()
            else:
                name = str(getattr(source, "gateway_name", "") or "").strip()
            if name:
                return name
        for device in devices or ():
            if format_gateway_uid(getattr(device, "uid", "")) == uid:
                return str(getattr(device, "name", "") or "").strip() or uid
        return uid


# ---------------------------------------------------------------------------
# Trợ giúp ép kiểu (CSV luôn là chuỗi)
# ---------------------------------------------------------------------------
def _as_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
