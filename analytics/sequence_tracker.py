"""SequenceTracker — phân tích SEQ cho từng node.

Khoá theo **node**: `node_uid`.

Vì sao KHÔNG dùng `gateway_uid` / `receiver_uid` trong khoá (đo trên phiên chạy
thật 8 gateway / 51 node):
  - Mọi gateway trong tầm đều nghe cùng một bản tin LoRa rồi publish lên MQTT.
    Khoá theo gateway làm 1 bản tin logic bị đếm N lần (24 788 dòng thay vì
    3 470) và `missing` bị nhân lên sai (13 644 thay vì 1 381).
  - 48/51 node được nghe bởi **9 `receiver_uid`** khác nhau → `receiver_uid`
    cũng không định danh được node.
  - `gateway_uid` có thể RỖNG với bản tin node cũ.

Việc gộp các bản copy của cùng một bản tin do `analytics.packet_deduper`
`PacketDeduper` làm; tracker chỉ nhận **luồng bản tin logic** (1 bản tin = 1 lần
`feed`) và ghi lại thông tin phủ sóng (`gateways_heard`).

Quy tắc (xem docs/MQTT_PROTOCOL_ANALYSIS.md §6):
  - SEQ là uint16 (0..65535) → wrap-around hợp lệ: 65534 → 65535 → 0 → 1.
  - `seq == 0` KHÔNG BAO GIỜ được đưa vào tracker khi là info frame
    (`packet_type == INFO`). Chỉ DATA / DATA_NO_SEQ mới đi qua đây.
  - Không dùng công thức `current == previous + 1`:
      * `delta == 1`                → NORMAL
      * `delta == 0`                → DUPLICATE
      * `1 < delta <= threshold`    → MISSING (missing_count = delta - 1)
      * `previous < current` nhưng `delta > threshold` → SEQ_RESET (nhảy tiến quá xa)
      * `current < previous`:
          - `|delta| <= out_of_order_window` → OUT_OF_ORDER (tới muộn)
          - ngược lại (hoặc seq mới nhỏ, ví dụ về 1/0) → SEQ_RESET (node reboot)
      * vượt mốc seq_max → SEQ_WRAP (không tính missing)
  - `SEQ_RESET` / `SEQ_WRAP` KHÔNG làm tăng `missing`.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from core.models import (
    AN_DUPLICATE,
    AN_MISSING,
    AN_OUT_OF_ORDER,
    AN_SEQ_RESET,
    AN_SEQ_WRAP,
    SQ_DUPLICATE,
    SQ_FIRST,
    SQ_MISSING,
    SQ_NORMAL,
    SQ_OUT_OF_ORDER,
    SQ_RESET,
    AnomalyRecord,
    MissingRecord,
)

log = logging.getLogger(__name__)

DEFAULT_SEQ_MAX = 65535
DEFAULT_RESET_THRESHOLD = 1024
DEFAULT_OOO_WINDOW = 16
# Số lần lặp lại đã thấy giữ lại cho mỗi node để nhận diện out-of-order.
REPLAY_HISTORY = 512


@dataclass
class SeqState:
    """Trạng thái SEQ của một **node** (key = node_uid)."""

    key: str
    node_uid: str = ""
    node_format: str = ""
    last_seq: Optional[int] = None
    expected_seq: Optional[int] = None
    previous_raw_seq: Optional[int] = None
    packets: int = 0
    missing: int = 0
    duplicate: int = 0
    out_of_order: int = 0
    seq_reset: int = 0
    seq_wrap: int = 0
    first_seq: Optional[int] = None
    last_anomaly: str = ""
    last_status: str = ""
    first_seen: str = ""
    last_seen: str = ""
    last_gap_from: Optional[int] = None
    last_gap_to: Optional[int] = None
    seen: "OrderedDict[int, int]" = field(default_factory=OrderedDict)
    # --- thông tin phủ sóng (nhiều gateway cùng nghe 1 node) ---
    gateways: set = field(default_factory=set)
    gateways_heard_max: int = 0
    gateways_heard_sum: int = 0
    last_gateway: str = ""  # gateway báo bản tin gần nhất (ghi vào record thống kê)

    def counters(self) -> Dict[str, int]:
        return {
            "packets": self.packets,
            "missing": self.missing,
            "duplicate": self.duplicate,
            "out_of_order": self.out_of_order,
            "seq_reset": self.seq_reset,
            "seq_wrap": self.seq_wrap,
        }

    @property
    def gateways_heard(self) -> int:
        """Số gateway khác nhau đã từng nghe node này."""
        return len(self.gateways)

    def gateways_heard_avg(self) -> Optional[float]:
        """Số gateway nghe trung bình trên mỗi bản tin (độ phủ sóng LoRa)."""
        return round(self.gateways_heard_sum / self.packets, 2) if self.packets else None

    def observe_coverage(
        self,
        gateways: Iterable[str],
        gateway_uid: str = "",
        rssi: Optional[float] = None,
        snr: Optional[float] = None,
    ) -> None:
        """Cập nhật thông tin phủ sóng từ 1 bản tin logic đã gộp.

        Chữ ký cũ từng nhận thêm `gateway_uid`, `rssi`, `snr` để ghi nhãn bản tin
        theo gateway nghe tốt nhất. Sau này `SeqState` chỉ lưu tập hợp gateway đã
        nghe và các thống kê phủ sóng, nên các tham số extra này được giữ lại
        cho tương thích ngược và bị bỏ qua an toàn.
        """
        heard = 0
        for gateway in gateways or ():
            if gateway:
                cleaned = str(gateway).strip().upper()
                if cleaned:
                    self.gateways.add(cleaned)
                    heard += 1
        if gateway_uid:
            self.last_gateway = str(gateway_uid).strip().upper()
        self.gateways_heard_sum += heard
        if heard > self.gateways_heard_max:
            self.gateways_heard_max = heard

    def delivery_rate(self) -> Optional[float]:
        """Tỉ lệ nhận (%) theo định nghĩa của tool (xem README §4).

        `delivery_rate_percent = 100 * packets / (packets + missing)`

        `missing` chỉ đếm các SEQ bị nhảy cóc giữa hai bản tin liên tiếp của CÙNG
        một node, nên node ngừng gửi KHÔNG làm giảm chỉ số này (chỉ sinh cảnh báo
        NODE_SILENT). Trả `None` khi chưa có dữ liệu để không ghi số vô nghĩa.
        """
        total = self.packets + self.missing
        if total <= 0:
            return None
        return round(100.0 * self.packets / total, 2)

    def to_state_dict(self) -> Dict[str, object]:
        return {
            "node_uid": self.node_uid,
            "node_format": self.node_format,
            "last_seq": self.last_seq,
            "expected_seq": self.expected_seq,
            "previous_raw_seq": self.previous_raw_seq,
            "packets": self.packets,
            "missing": self.missing,
            "duplicate": self.duplicate,
            "out_of_order": self.out_of_order,
            "seq_reset": self.seq_reset,
            "seq_wrap": self.seq_wrap,
            "first_seq": self.first_seq,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "last_status": self.last_status,
            "last_anomaly": self.last_anomaly,
            "last_gap_from": self.last_gap_from,
            "last_gap_to": self.last_gap_to,
            "gateways_heard": self.gateways_heard,
            "gateways_heard_max": self.gateways_heard_max,
            "last_gateway": self.last_gateway,
            "gateways": sorted(self.gateways),
            "seen": list(self.seen.keys())[-REPLAY_HISTORY:],
        }


@dataclass
class SeqOutcome:
    """Kết quả phân tích một packet."""

    status: str = SQ_FIRST
    previous_seq: Optional[int] = None
    expected_seq: Optional[int] = None
    seq_delta: Optional[int] = None
    missing_count: int = 0
    duplicate: bool = False
    out_of_order: bool = False
    reset: bool = False
    wrap: bool = False
    anomaly: str = ""
    missing_record: Optional[MissingRecord] = None
    anomaly_record: Optional[AnomalyRecord] = None

    def apply_to(self, record) -> None:
        """Điền kết quả vào MQTTRecord (dùng ở pipeline, chạy trong analyse thread)."""
        record.previous_seq = self.previous_seq
        record.expected_seq = self.expected_seq
        record.seq_delta = self.seq_delta
        record.seq_status = self.status
        record.missing_count = self.missing_count
        record.duplicate = self.duplicate
        record.out_of_order = self.out_of_order
        record.reboot_detected = self.reset
        record.anomaly = self.anomaly


class SequenceTracker:
    """Theo dõi SEQ độc lập cho từng node."""

    def __init__(
        self,
        seq_max: int = DEFAULT_SEQ_MAX,
        reset_threshold: int = DEFAULT_RESET_THRESHOLD,
        out_of_order_window: int = DEFAULT_OOO_WINDOW,
    ) -> None:
        self.seq_max = int(seq_max) if int(seq_max) > 0 else DEFAULT_SEQ_MAX
        self.reset_threshold = max(1, int(reset_threshold))
        self.out_of_order_window = max(1, int(out_of_order_window))
        self._states: Dict[str, SeqState] = {}

    # ------------------------------------------------------------------ API
    @staticmethod
    def make_key(node_uid: str) -> str:
        """Khoá trạng thái SEQ = `node_uid` (KHÔNG gồm gateway/receiver).

        Nhiều gateway cùng nghe một node, nên nếu đưa gateway vào khoá thì mỗi
        bản tin logic bị đếm N lần và `missing` sai theo (xem docstring đầu file).
        """
        return (node_uid or "").upper()

    def states(self) -> Dict[str, SeqState]:
        return self._states

    def state_count(self) -> int:
        return len(self._states)

    def reset(self) -> None:
        self._states.clear()

    def feed(
        self,
        node_uid: str,
        seq: int,
        timestamp: str = "",
        node_format: str = "",
        gateways: Optional[Iterable[str]] = None,
        gateway_uid: str = "",
    ) -> SeqOutcome:
        """Phân tích 1 **bản tin logic** (đã gộp mọi bản copy). Trả `SeqOutcome`.

        `seq` phải là int (0..seq_max). Việc loại bỏ info frame / packet không có
        seq là trách nhiệm của caller (`pipeline`).

        `gateways` là độ phủ sóng của bản tin (nhiều gateway cùng nghe) — chỉ
        dùng để hiển thị/thống kê, không ảnh hưởng kết luận SEQ. `gateway_uid`
        là gateway báo bản tin, ghi vào dòng missing/anomaly cho biết ai đã nghe.
        """
        key = self.make_key(node_uid)
        state = self._states.get(key)
        if state is None:
            state = SeqState(key=key, node_uid=node_uid or "")
            self._states[key] = state

        if node_format:
            state.node_format = node_format
        if timestamp:
            if not state.first_seen:
                state.first_seen = timestamp
            state.last_seen = timestamp

        outcome = self._analyse(state, int(seq), timestamp)
        state.packets += 1
        state.last_status = outcome.status
        if outcome.anomaly:
            state.last_anomaly = outcome.anomaly
        if outcome.previous_seq is None:
            state.first_seq = int(seq)

        heard = sorted({g.upper() for g in (gateways or ()) if g})
        if heard:
            state.observe_coverage(heard)
        if gateway_uid:
            state.last_gateway = gateway_uid.upper()
        # Ghi độ phủ của BẢN TIN NÀY vào record bất thường/thiếu: nhiều gateway
        # cùng nghe nên CSV phải nói rõ bản tin đã được bao nhiêu gateway nghe.
        for record in (outcome.missing_record, outcome.anomaly_record):
            if record is not None:
                record.gateways_heard = len(heard)
                record.gateways = "|".join(heard)
        return outcome

    # ------------------------------------------------------------- nội bộ
    def _analyse(self, state: SeqState, seq: int, timestamp: str) -> SeqOutcome:
        outcome = SeqOutcome()

        if state.last_seq is None:
            state.last_seq = seq
            state.expected_seq = self._next(seq)
            state.previous_raw_seq = seq
            self._remember(state, seq)
            outcome.status = SQ_FIRST
            outcome.expected_seq = state.expected_seq
            return outcome

        previous = state.last_seq
        outcome.previous_seq = previous
        outcome.expected_seq = state.expected_seq
        delta = seq - previous
        outcome.seq_delta = delta

        # ---- DUPLICATE -----------------------------------------------------
        if seq == previous:
            state.duplicate += 1
            state.last_seq = seq  # không đổi
            self._remember(state, seq)
            outcome.status = SQ_DUPLICATE
            outcome.duplicate = True
            outcome.anomaly = AN_DUPLICATE
            outcome.anomaly_record = AnomalyRecord(
                timestamp=timestamp,
                gateway_uid=state.last_gateway,
                node_uid=state.node_uid,
                seq=seq,
                previous_seq=previous,
                anomaly_type=AN_DUPLICATE,
                details="seq lặp lại liên tiếp (prev=%d, cur=%d)" % (previous, seq),
            )
            return outcome

        # ---- nhảy tiến ------------------------------------------------------
        if seq > previous:
            if delta == 1:
                self._accept(state, seq)
                outcome.status = SQ_NORMAL
                return outcome

            if seq <= self.seq_max:
                # Có thể là wrap: prev gần trần, seq nhỏ
                if dt_is_wrap(previous, seq, self.seq_max):
                    state.seq_wrap += 1
                    self._accept(state, seq)
                    outcome.status = SQ_NORMAL
                    outcome.wrap = True
                    outcome.anomaly = AN_SEQ_WRAP
                    outcome.anomaly_record = AnomalyRecord(
                        timestamp=timestamp,
                        gateway_uid=state.last_gateway,
                        node_uid=state.node_uid,
                        seq=seq,
                        previous_seq=previous,
                        anomaly_type=AN_SEQ_WRAP,
                        details="wrap-around SEQ (prev=%d, cur=%d, seq_max=%d)"
                        % (previous, seq, self.seq_max),
                    )
                    return outcome

                if delta <= self.reset_threshold:
                    missing = delta - 1
                    first = self._next(previous)
                    last = seq - 1
                    state.missing += missing
                    state.last_gap_from = first
                    state.last_gap_to = last
                    self._accept(state, seq)
                    outcome.status = SQ_MISSING
                    outcome.missing_count = missing
                    outcome.anomaly = AN_MISSING
                    outcome.missing_record = MissingRecord(
                        timestamp=timestamp,
                        gateway_uid=state.last_gateway,
                        node_uid=state.node_uid,
                        previous_seq=previous,
                        current_seq=seq,
                        missing_from=first,
                        missing_to=last,
                        missing_count=missing,
                        reason="SEQ_GAP",
                    )
                    outcome.anomaly_record = AnomalyRecord(
                        timestamp=timestamp,
                        gateway_uid=state.last_gateway,
                        node_uid=state.node_uid,
                        seq=seq,
                        previous_seq=previous,
                        anomaly_type=AN_MISSING,
                        details="thiếu %d packet (%d..%d)" % (missing, first, last),
                    )
                    return outcome

                # nhảy tiến quá xa (delta > threshold): nghi node đổi phiên/bộ đếm
                # → coi là RESET, KHÔNG tính missing (tránh false positive lớn).
                state.seq_reset += 1
                self._accept(state, seq, reset_seen=True)
                outcome.status = SQ_RESET
                outcome.reset = True
                outcome.anomaly = AN_SEQ_RESET
                outcome.anomaly_record = AnomalyRecord(
                    timestamp=timestamp,
                    gateway_uid=state.last_gateway,
                    node_uid=state.node_uid,
                    seq=seq,
                    previous_seq=previous,
                    anomaly_type=AN_SEQ_RESET,
                    details="nhảy tiến %d (> ngưỡng reset %d) — coi là reset bộ đếm"
                    % (delta, self.reset_threshold),
                )
                return outcome

            # seq > seq_max: dữ liệu vượt kiểu uint16 -> chỉ nhận, cảnh báo.
            state.seq_wrap += 1
            self._accept(state, seq)
            outcome.status = SQ_NORMAL
            outcome.wrap = True
            outcome.anomaly = AN_SEQ_WRAP
            outcome.anomaly_record = AnomalyRecord(
                timestamp=timestamp,
                gateway_uid=state.last_gateway,
                node_uid=state.node_uid,
                seq=seq,
                previous_seq=previous,
                anomaly_type=AN_SEQ_WRAP,
                details="seq=%d vượt seq_max=%d (không phải uint16)" % (seq, self.seq_max),
            )
            return outcome

        # ---- nhảy lùi (seq < previous) --------------------------------------
        back = previous - seq

        # 0 đã dành cho info frame; data seq = 0 sau khi đã chạy > 0 => reset.
        if seq == 0:
            state.seq_reset += 1
            self._remember(state, seq)
            state.last_seq = seq
            state.expected_seq = self._next(seq)
            state.previous_raw_seq = seq
            outcome.status = SQ_RESET
            outcome.reset = True
            outcome.anomaly = AN_SEQ_RESET
            outcome.anomaly_record = AnomalyRecord(
                timestamp=timestamp,
                gateway_uid=state.last_gateway,
                node_uid=state.node_uid,
                seq=seq,
                previous_seq=previous,
                anomaly_type=AN_SEQ_RESET,
                details="SEQ quay về 0 sau %d (nghi node reboot/reset)" % previous,
            )
            return outcome

        if seq in state.seen:
            # đã từng thấy → tới muộn (out-of-order / re-publish cũ)
            state.out_of_order += 1
            self._remember(state, seq)
            outcome.status = SQ_OUT_OF_ORDER
            outcome.out_of_order = True
            outcome.anomaly = AN_OUT_OF_ORDER
            outcome.anomaly_record = AnomalyRecord(
                timestamp=timestamp,
                gateway_uid=state.last_gateway,
                node_uid=state.node_uid,
                seq=seq,
                previous_seq=previous,
                anomaly_type=AN_OUT_OF_ORDER,
                details="packet tới muộn (seq=%d đã nhận trước đó, last=%d)" % (seq, previous),
            )
            return outcome

        if back <= self.out_of_order_window:
            state.out_of_order += 1
            self._remember(state, seq)
            outcome.status = SQ_OUT_OF_ORDER
            outcome.out_of_order = True
            outcome.anomaly = AN_OUT_OF_ORDER
            outcome.anomaly_record = AnomalyRecord(
                timestamp=timestamp,
                gateway_uid=state.last_gateway,
                node_uid=state.node_uid,
                seq=seq,
                previous_seq=previous,
                anomaly_type=AN_OUT_OF_ORDER,
                details="ngược thứ tự (prev=%d, cur=%d, lùi %d <= cửa sổ %d)"
                % (previous, seq, back, self.out_of_order_window),
            )
            return outcome

        # lùi xa (ví dụ 500 -> 3): reset bộ đếm
        state.seq_reset += 1
        self._remember(state, seq)
        state.last_seq = seq
        state.expected_seq = self._next(seq)
        state.previous_raw_seq = seq
        outcome.status = SQ_RESET
        outcome.reset = True
        outcome.anomaly = AN_SEQ_RESET
        outcome.anomaly_record = AnomalyRecord(
            timestamp=timestamp,
            gateway_uid=state.last_gateway,
            node_uid=state.node_uid,
            seq=seq,
            previous_seq=previous,
            anomaly_type=AN_SEQ_RESET,
            details="SEQ lùi %d bậc (prev=%d, cur=%d, ngưỡng %d) — nghi reboot"
            % (back, previous, seq, self.out_of_order_window),
        )
        return outcome

    def _accept(self, state: SeqState, seq: int, reset_seen: bool = False) -> None:
        state.last_seq = seq
        state.expected_seq = self._next(seq)
        state.previous_raw_seq = seq
        if reset_seen:
            state.seen.clear()
        self._remember(state, seq)

    def _next(self, seq: int) -> int:
        return 0 if seq >= self.seq_max else seq + 1

    @staticmethod
    def _remember(state: SeqState, seq: int) -> None:
        state.seen[seq] = state.seen.get(seq, 0) + 1
        while len(state.seen) > REPLAY_HISTORY:
            state.seen.popitem(last=False)

    # --------------------------------------------------------- state (JSON)
    def export_state(self) -> Dict[str, object]:
        return {
            "seq_max": self.seq_max,
            "reset_threshold": self.reset_threshold,
            "out_of_order_window": self.out_of_order_window,
            "nodes": {key: state.to_state_dict() for key, state in self._states.items()},
        }

    def import_state(self, payload: Dict[str, object]) -> int:
        """Nạp lại state đã lưu. Trả số node nạp được."""
        if not isinstance(payload, dict):
            return 0
        nodes = payload.get("nodes")
        if not isinstance(nodes, dict):
            return 0
        count = 0
        for key, data in nodes.items():
            if not isinstance(data, dict):
                continue
            state = SeqState(key=str(key))
            state.node_uid = str(data.get("node_uid", ""))
            state.node_format = str(data.get("node_format", ""))
            state.last_seq = _opt_int(data.get("last_seq"))
            state.expected_seq = _opt_int(data.get("expected_seq"))
            state.previous_raw_seq = _opt_int(data.get("previous_raw_seq"))
            state.packets = _safe_int(data.get("packets"))
            state.missing = _safe_int(data.get("missing"))
            state.duplicate = _safe_int(data.get("duplicate"))
            state.out_of_order = _safe_int(data.get("out_of_order"))
            state.seq_reset = _safe_int(data.get("seq_reset"))
            state.seq_wrap = _safe_int(data.get("seq_wrap"))
            state.first_seq = _opt_int(data.get("first_seq"))
            state.first_seen = str(data.get("first_seen", ""))
            state.last_seen = str(data.get("last_seen", ""))
            state.last_status = str(data.get("last_status", ""))
            state.last_anomaly = str(data.get("last_anomaly", ""))
            state.last_gap_from = _opt_int(data.get("last_gap_from"))
            state.last_gap_to = _opt_int(data.get("last_gap_to"))
            state.gateways_heard_max = _safe_int(data.get("gateways_heard_max"))
            state.last_gateway = str(data.get("last_gateway", "") or "")
            for gateway in data.get("gateways", []) or []:
                if gateway:
                    state.gateways.add(str(gateway).upper())
            seen = data.get("seen")
            if isinstance(seen, list):
                for item in seen:
                    value = _opt_int(item)
                    if value is not None:
                        state.seen[value] = 1
            self._states[state.key] = state
            count += 1
        return count

    # ------------------------------------------------------- rebuild từ CSV
    @staticmethod
    def rebuild_from_rows(
        rows: Iterable[dict],
        seq_max: int = DEFAULT_SEQ_MAX,
        reset_threshold: int = DEFAULT_RESET_THRESHOLD,
        out_of_order_window: int = DEFAULT_OOO_WINDOW,
    ) -> Tuple["SequenceTracker", int]:
        """Dựng lại state từ các dòng CSV `mqtt_data_*` (nguồn sự thật).

        Chỉ dùng các dòng `packet_type == DATA` và `seq` hợp lệ. Bỏ qua
        DUPLICATE trong CSV (đã được tính ở lần chạy trước) để counters không
        bị đếm 2 lần? — KHÔNG: counters được dựng lại từ đầu, nên phải feed lại
        toàn bộ để tái tạo đúng chuỗi (kể cả duplicate).
        """
        tracker = SequenceTracker(seq_max, reset_threshold, out_of_order_window)
        used = 0
        for row in rows:
            if str(row.get("packet_type", "")).strip().upper() != "DATA":
                continue
            seq_text = str(row.get("seq", "")).strip()
            if seq_text == "":
                continue
            try:
                seq = int(float(seq_text))
            except ValueError:
                continue
            gateway = str(row.get("gateway_uid", "")).strip().upper()
            receiver = str(row.get("receiver_uid", "")).strip().upper()
            node = str(row.get("node_uid", "")).strip().upper()
            if not node:
                continue
            tracker.feed(
                gateway,
                receiver,
                node,
                seq,
                timestamp=str(row.get("received_at", "")),
                node_format=str(row.get("node_format", "")),
            )
            used += 1
        tracker._rebuild_reset_dedup()  # noqa: SLF001 - dọn counter do feed lại
        return tracker, used

    def _rebuild_reset_dedup(self) -> None:
        """Giữ counters nguyên trạng (đã đúng vì feed lại đúng chuỗi từ đầu).

        Hàm để trống về mặt logic nhưng giữ chỗ cho mọi tinh chỉnh sau này và
        đảm bảo `seen` không phình khi rebuild dataset lớn.
        """
        for state in self._states.values():
            while len(state.seen) > REPLAY_HISTORY:
                state.seen.popitem(last=False)


def dt_is_wrap(previous: int, current: int, seq_max: int) -> bool:
    """True nếu `previous → current` là wrap-around hợp lệ của bộ đếm uint16."""
    if current >= previous:
        return False
    span = seq_max + 1
    # Nếu coi `current` là `previous + 1` (đã wrap quanh 0) thì chỉ còn 1 bước.
    forward = (current - previous) % span
    return forward <= 32 and previous >= seq_max - 32


def _opt_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _safe_int(value) -> int:
    value = _opt_int(value)
    return 0 if value is None else value


def _opt_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
