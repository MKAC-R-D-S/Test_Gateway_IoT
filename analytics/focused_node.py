"""Theo dõi MỘT node trên NHIỀU gateway: bắt hụt (missed catch) vs bắn hụt (late/backfill).

Bối cảnh (xem `docs/MQTT_PROTOCOL_ANALYSIS.md` §7.1):
  - Một node phát **một** bản tin LoRa thì **mọi** gateway trong tầm đều publish
    lên MQTT. Vì vậy `(node_uid, seq)` là khoá của một bản tin logic, còn danh
    sách gateway đã publish chính là *ai đã nghe được bản tin đó*.
  - Khi chỉ quan tâm MỘT node, ta có thể hỏi ngược lại từng gateway:
    "node đã phát bản tin này (gateway khác nghe được) mà gateway mày không
    publish — mày bắt hụt bao nhiêu?"

Định nghĩa dùng trong tool (đã chốt với người dùng, tiêu chí A):

  Tập tham chiếu (reference)
    = hợp tất cả `seq` mà **bất kỳ** gateway nào đã publish cho node đó.
      Đây là bằng chứng mạnh nhất mà MQTT có thể cung cấp về việc node ĐÃ phát.
      (`seq` là uint16 → tối đa 65 536 giá trị → bộ nhớ bị chặn, không phình
      theo độ dài phiên 3–4 ngày.)

  ``missed_catch`` (BẮT HỤT)
    = |reference| − số seq mà gateway đó publish cho node.
      Tức node đã phát (có gateway khác nghe) nhưng gateway này không bắn lên.

  ``late_packets`` (BẮN HỤT / bắn trễ)
    = số **lần publish** của gateway bị trễ:
      ``received_at − gateway_timestamp > late_seconds``.
      Nguyên nhân thực tế: gateway mất MQTT → firmware buffer `/data` vào thẻ SD
      rồi gửi bù (backfill) sau khi reconnect (`mqtt_task.cpp:141-158`). Bản tin
      không mất trên đường LoRa nhưng KHÔNG kịp lên MQTT → đúng nghĩa "bắn hụt".

      Đo trên **mọi bản copy** (không chỉ bản đầu của mỗi `seq`) để tử số
      (`late_packets`) và mẫu số (`copies`) cùng đơn vị → `late_percent` là "tỉ lệ
      lần publish bị trễ" và không bị lệch.

      Giả định: pipeline đã gộp các bản copy của cùng một bản tin LoRa
      (`PacketDeduper`, `packet_merge_enabled`). Tắt gộp sẽ khiến giá trị này
      đếm quá (mỗi lần publish lặp tính là một lần trễ).

Lưu ý quan trọng về giới hạn của phương pháp (KHÔNG được ảo giác):
  - MQTT **không** nói được gateway bắt được sóng nhưng không publish vì lý do
    khác (hàng đợi nội bộ, lỗi publish). Vì vậy `missed_catch` là **chặn trên**
    của "bắt hụt" và **chặn dưới** của "tổng số hụt".
  - Một gateway bị mất mạng cả tiếng sẽ hiện `missed_catch` rất lớn — đó là
    *link down*, không phải lỗi thu sóng. Cột `notes` + `silence_*` giúp phân biệt.
  - `seq` trùng sau khi node reboot/SEQ_RESET được coi là cùng một giá trị (bitmap
    theo `seq`), nên thống kê mang tính xấp xỉ ở các mốc reset — giống cách
    `SequenceTracker` xử lý wrap.
  - Gateway đã CẤU HÌNH trong `devices.json` nhưng KHÔNG hề publish node trọng
    tâm sẽ **không tự xuất hiện** (không có gì để `feed()`). Vì vậy pipeline nạp
    thêm danh sách gateway đã cấu hình qua `set_known_gateways()` — nhờ đó ca
    "cả phiên không bắt được gì" hiện ra rõ ràng với `missed_catch` = 100%.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from core.uid import format_gateway_uid, normalize_uid

from .packet_deduper import parse_stamp

log = logging.getLogger(__name__)

#: Ngưỡng trễ mặc định (giây): `received_at − gateway_timestamp` vượt ngưỡng này
#: thì bản tin bị coi là "bắn hụt" (buffer SD / backfill sau khi mất MQTT).
DEFAULT_LATE_SECONDS = 30.0
#: Số sự kiện trễ chi tiết giữ lại cho mỗi gateway (giữ các ca trễ NẶNG nhất).
DEFAULT_DETAIL_LIMIT = 200
#: Số khoảng thiếu liên tiếp tối đa ghi chi tiết cho mỗi gateway.
DEFAULT_MAX_RANGES = 2000
#: Số khoảng thiếu LẤY MẪU cho mỗi gateway để đo "hụt liên tục hay rải rác".
#: Lấy mẫu trải đều từ đầu đến cuối phiên nên vẫn đại diện cho cả phiên dài.
DEFAULT_GAP_SAMPLE_LIMIT = 32
#: Chặn số gateway theo dõi cho 1 node (bảo vệ RAM nếu cấu hình sai / node bị
#: rất nhiều gateway nghe). 128 gateway × 64 KB bitmap ≈ 8 MB.
MAX_GATEWAYS = 128


def normalize_focus_node(text: Any) -> str:
    """Chuẩn hoá UID node người dùng nhập → hex in hoa. "" nếu không hợp lệ.

    Node dùng UID 16 hex (node mới) hoặc 24 hex (node cũ), firmware luôn
    zero-pad (`data_process.cpp:227`) nên yêu cầu **độ dài chẵn** (`normalize_uid`,
    KHÔNG dùng biến thể chấp nhận hex lẻ như gateway UID).
    """
    return normalize_uid(text)


@dataclass
class GatewayFocus:
    """Trạng thái theo dõi node trọng tâm của MỘT gateway."""

    gateway_uid: str
    gateway_name: str = ""
    #: Bitmap `seq → đã nghe` (1 byte/seq). Bị chặn ở `seq_max + 1` byte.
    heard: bytearray = field(default_factory=bytearray)
    packets_heard: int = 0          # số seq KHÁC NHAU đã publish cho node
    copies: int = 0                 # tổng số lần publish (gồm cả trùng)
    late_packets: int = 0           # số bản copy bị trễ (bắn hụt)
    delay_count: int = 0            # số bản copy đo được trễ (có timestamp)
    delay_missing: int = 0          # số bản copy KHÔNG có gateway_timestamp
    delay_sum: float = 0.0
    delay_min: Optional[float] = None
    delay_max: Optional[float] = None
    delay_max_at: str = ""          # `received_at` của ca trễ nặng nhất
    first_seen: str = ""
    last_seen: str = ""
    late_events: List[Dict[str, Any]] = field(default_factory=list)
    #: Thống kê kiểu ngắt quãng — chỉ cập nhật khi gọi `refresh_patterns()`
    #: (quét bitmap tốn ~ms/gateway nên KHÔNG tính realtime mỗi lần đọc).
    gap_runs: int = 0          #: số khoảng hụt rời rạc
    gap_total: int = 0         #: tổng số seq hụt (bằng `missed_catch`)
    gap_largest: int = 0       #: khoảng hụt dài nhất
    gap_pattern: str = ""      #: mô tả tiếng Việt, nhúng vào cột `notes`

    def late_percent(self) -> Optional[float]:
        if self.copies <= 0:
            return None
        return round(100.0 * self.late_packets / self.copies, 2)

    def delay_avg(self) -> Optional[float]:
        if self.delay_count <= 0:
            return None
        return round(self.delay_sum / self.delay_count, 3)


class FocusedNodeTracker:
    """Theo dõi 1 node trên mọi gateway đã nghe node đó.

    Chỉ `feed()` các bản copy có `packet_type == DATA` **và** `seq` hợp lệ
    (`0 < seq <= seq_max`) của đúng node đang theo dõi. Việc lọc xảy ra ở tầng
    pipeline; `feed()` cũng tự kiểm tra lại nên gọi sai cũng không hỏng dữ liệu.
    """

    def __init__(
        self,
        seq_max: int = 65535,
        late_seconds: float = DEFAULT_LATE_SECONDS,
        detail_limit: int = DEFAULT_DETAIL_LIMIT,
        max_ranges: int = DEFAULT_MAX_RANGES,
        max_gateways: int = MAX_GATEWAYS,
    ) -> None:
        self.seq_max = max(1, int(seq_max))
        self.size = self.seq_max + 1
        self.late_seconds = max(0.0, float(late_seconds))
        self.detail_limit = max(1, int(detail_limit))
        self.max_ranges = max(1, int(max_ranges))
        self.max_gateways = max(1, int(max_gateways))

        # `feed()` chạy trên thread phân tích, GUI đọc `*_rows()` trên UI thread
        # → cần lock. Chỉ 1 thread ghi nên lock hầu như không tranh chấp.
        self._lock = threading.Lock()
        self.node_uid: str = ""
        #: {gateway_uid → gateway_name} của MỌI gateway đã cấu hình (`devices.json`).
        #: Chỉ dùng để tạo sẵn dòng cho gateway KHÔNG publish node trọng tâm.
        self._known: Dict[str, str] = {}
        self._reset_state()

    # ------------------------------------------------------------------ API
    @property
    def active(self) -> bool:
        return bool(self.node_uid)

    def set_node(self, node_uid: str) -> bool:
        """Đổi node theo dõi. Trả True nếu node thực sự đổi (đã xoá số liệu cũ).

        Đổi node giữa phiên sẽ **xoá** số liệu của node cũ để không trộn hai node
        vào cùng một bảng.
        """
        normalized = normalize_focus_node(node_uid)
        with self._lock:
            if normalized == self.node_uid:
                return False
            self.node_uid = normalized
            self._reset_state()
            return True

    def reset(self) -> None:
        with self._lock:
            self._reset_state()

    def set_known_gateways(self, gateways: Iterable[Any]) -> None:
        """Nạp danh sách gateway ĐÃ CẤU HÌNH (để hiện cả ca hụt 100%).

        `gateways` là iterable các `GatewayDevice` (có `.uid` / `.name`) hoặc
        `(uid, name)` / dict. Chỉ gateway có UID hợp lệ mới được thêm. Gateway đã
        nghe bản tin nào rồi (đã có trong `_gateways`) sẽ được giữ nguyên số liệu.
        """
        known: Dict[str, str] = {}
        for device in gateways:
            uid = name = ""
            if isinstance(device, dict):
                uid = str(device.get("uid") or device.get("gateway_uid") or "")
                name = str(device.get("name") or device.get("gateway_name") or "")
            elif isinstance(device, (tuple, list)) and len(device) >= 2:
                uid, name = str(device[0] or ""), str(device[1] or "")
            else:
                uid = str(getattr(device, "uid", "") or "")
                name = str(getattr(device, "name", "") or "")
            formatted = format_gateway_uid(uid)
            if formatted:
                known.setdefault(formatted, name)
        with self._lock:
            self._known = known
            # Tạo sẵn dòng cho gateway chưa từng publish node này.
            for uid_, name_ in known.items():
                if uid_ in self._gateways or len(self._gateways) >= self.max_gateways:
                    continue
                self._gateways[uid_] = GatewayFocus(
                    gateway_uid=uid_, gateway_name=name_, heard=bytearray(self.size)
                )

    def reference_size(self) -> int:
        with self._lock:
            return self._ref_count

    def feed(self, record: Any) -> bool:
        """Nạp 1 bản copy MQTT. Trả True nếu bản tin thuộc node đang theo dõi."""
        if not self.node_uid:
            return False
        node = normalize_uid(getattr(record, "node_uid", ""))
        if not node or node != self.node_uid:
            return False
        seq = getattr(record, "seq", None)
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            return False
        if not 0 < seq <= self.seq_max:
            return False

        gateway = format_gateway_uid(getattr(record, "gateway_uid", ""))
        received_at = str(getattr(record, "received_at", "") or "")
        gateway_ts = str(getattr(record, "gateway_timestamp", "") or "")
        delay = _delay_seconds(received_at, gateway_ts)

        with self._lock:
            if not self._ref[seq]:
                self._ref[seq] = 1
                self._ref_at[seq] = received_at
                self._ref_count += 1
            if received_at:
                if not self.first_seen:
                    self.first_seen = received_at
                self.last_seen = received_at

            self.total_copies += 1
            if not gateway:
                # Bản tin không rõ gateway vẫn góp vào reference (bằng chứng node
                # đã phát) nhưng không thể quy cho gateway nào.
                return True

            state = self._gateways.get(gateway)
            if state is None:
                if len(self._gateways) >= self.max_gateways:
                    log.warning(
                        "Node %s được nghe bởi hơn %d gateway — bỏ qua gateway %s",
                        self.node_uid, self.max_gateways, gateway,
                    )
                    return True
                state = GatewayFocus(gateway_uid=gateway, heard=bytearray(self.size))
                self._gateways[gateway] = state
            if not state.gateway_name:
                state.gateway_name = (
                    str(getattr(record, "gateway_name", "") or "")
                    or self._known.get(gateway, "")
                )

            if not state.heard[seq]:
                state.heard[seq] = 1
                state.packets_heard += 1
            state.copies += 1
            if received_at:
                if not state.first_seen:
                    state.first_seen = received_at
                state.last_seen = received_at

            if delay is None:
                state.delay_missing += 1
                return True

            # Đo trên MỌI bản copy (không chỉ bản đầu của mỗi `seq`) vì mỗi lần
            # publish là một lần "bắn" — nhờ vậy `late_packets` / `copies` cùng
            # đơn vị và `late_percent` không bị lệch.
            state.delay_count += 1
            state.delay_sum += delay
            if state.delay_min is None or delay < state.delay_min:
                state.delay_min = delay
            if state.delay_max is None or delay > state.delay_max:
                state.delay_max = delay
                state.delay_max_at = received_at
            if delay <= self.late_seconds:
                return True

            # --- BẮN HỤT: gateway có publish nhưng trễ quá ngưỡng ---
            state.late_packets += 1
            self.total_late += 1
            self._remember_late(state, seq, received_at, gateway_ts, delay)
            return True

    # ------------------------------------------------------------ snapshot
    def summary_rows(self) -> List[Dict[str, Any]]:
        """1 dòng TỔNG + 1 dòng mỗi gateway, sắp theo số bắt hụt giảm dần."""
        with self._lock:
            reference = self._ref_count
            rows: List[Dict[str, Any]] = [self._overall_row(reference)]
            for state in self._sorted_states():
                rows.append(self._gateway_row(state, reference))
            return rows

    def gap_pattern(self, state: GatewayFocus) -> str:
        """Mô tả kiểu ngắt quãng của 1 gateway (từ cache `refresh_patterns()`).

        Chỉ đọc, KHÔNG quét bitmap — nhờ vậy gọi được từ GUI/console mà không
        chặn thread. Gọi `refresh_patterns()` trước để có số liệu mới.
        """
        with self._lock:
            return state.gap_pattern

    def refresh_patterns(self) -> None:
        """Quét bitmap để cập nhật thống kê kiểu ngắt quãng cho mọi gateway.

        Trả lời câu hỏi *gateway hụt LIÊN TỤC một mạch hay hụt RẢI RÁC?* —
        `missed_catch` chỉ cho biết hụt BAO NHIÊU, không cho biết kiểu mất. Một
        mạch dài = gateway offline/mất tầm sóng; rải rác = nghi mất gói RF.

        Tốn ~ms/gateway nên **không** gọi mỗi chu kỳ GUI: chỉ gọi khi ghi report
        và vài chục giây một lần từ GUI.
        """
        with self._lock:
            for state in self._gateways.values():
                if state.packets_heard >= self._ref_count:
                    state.gap_runs = state.gap_total = state.gap_largest = 0
                    state.gap_pattern = ""
                    continue
                counts = [count for _, _, count in self._missing_ranges(state)]
                state.gap_runs = len(counts)
                state.gap_total = sum(counts)
                state.gap_largest = max(counts) if counts else 0
                state.gap_pattern = _gap_pattern(counts)

    def missing_rows(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Các khoảng `seq` liên tiếp mà từng gateway KHÔNG publish (bắt hụt)."""
        limit = self.max_ranges if limit is None else max(1, int(limit))
        with self._lock:
            rows: List[Dict[str, Any]] = []
            for state in self._sorted_states():
                if state.packets_heard >= self._ref_count:
                    continue
                for start, end, count in self._missing_ranges(state):
                    rows.append(
                        {
                            "node_uid": self.node_uid,
                            "gateway_uid": state.gateway_uid,
                            "gateway_name": state.gateway_name,
                            "missing_from": start,
                            "missing_to": end,
                            "missing_count": count,
                            "window_start": self._ref_at[start],
                            "window_end": self._ref_at[end],
                        }
                    )
                    if len(rows) >= limit:
                        return rows
            return rows

    def late_rows(self) -> List[Dict[str, Any]]:
        """Chi tiết các bản copy bị trễ (bắn hụt) — trễ nặng nhất trước."""
        with self._lock:
            rows: List[Dict[str, Any]] = []
            for state in self._sorted_states():
                for event in state.late_events:
                    rows.append(dict(event))
            rows.sort(key=lambda r: float(r.get("delay_seconds") or 0.0), reverse=True)
            return rows

    def stats(self) -> Dict[str, Any]:
        """Số liệu gọn để nhét vào `PipelineStats` (console headless + GUI)."""
        with self._lock:
            reference = self._ref_count
            gateway_rows = [
                self._gateway_row(state, reference) for state in self._sorted_states()
            ]
            total_heard = sum(s.packets_heard for s in self._gateways.values())
            silent = sum(1 for s in self._gateways.values() if s.copies <= 0)
            return {
                "node_uid": self.node_uid,
                "active": self.active,
                "reference_packets": reference,
                #: Gateway ĐÃ publish ít nhất 1 bản tin của node.
                "gateways_heard": sum(1 for s in self._gateways.values() if s.copies > 0),
                #: Gateway đã cấu hình / đã biết (kể cả chưa publish lần nào).
                "gateways_known": len(self._gateways),
                #: Gateway KHÔNG publish bản tin nào của node (nghi mất tầm sóng).
                "gateways_silent": silent,
                "total_copies": self.total_copies,
                "late_packets": self.total_late,
                "total_missing_catch": sum(
                    max(0, reference - s.packets_heard) for s in self._gateways.values()
                ),
                "heard_sum": total_heard,
                "first_seen": self.first_seen,
                "last_seen": self.last_seen,
                "gateways": gateway_rows,
            }

    def summary_lines(self, top: int = 5) -> List[str]:
        """Vài dòng text để in ra console/log khi chạy headless."""
        data = self.stats()
        if not data["active"]:
            return []
        lines = [
            "NODE TRỌNG TÂM %s | node đã phát (theo mọi gateway): %d bản tin | "
            "gateway nghe được: %d/%d"
            % (
                data["node_uid"],
                data["reference_packets"],
                data["gateways_heard"],
                data["gateways_known"],
            ),
        ]
        silent = data.get("gateways_silent") or 0
        if silent:
            lines.append(
                "  ⚠ %d gateway KHÔNG publish bản tin nào của node (nghi mất tầm sóng)"
                % silent
            )
        rows = [r for r in data["gateways"] if r["missed_catch"] or r["late_packets"]]
        if not rows:
            lines.append("  → không gateway nào bắt hụt / bắn hụt.")
            return lines
        lines.append("  Gateway bắt hụt nhiều nhất (bắt hụt / bắn trễ):")
        for row in rows[:top]:
            lines.append(
                "    %-14s %-22s bắt hụt %5d (%.2f%%) | bắn trễ %4d (%.2f%%)"
                % (
                    row["gateway_uid"],
                    (row["gateway_name"] or "")[:22],
                    row["missed_catch"],
                    row["missed_catch_percent"] or 0.0,
                    row["late_packets"],
                    row["late_percent"] or 0.0,
                )
            )
        if len(rows) > top:
            lines.append("    … còn %d gateway khác (xem focused_node_summary.csv)" % (len(rows) - top))
        return lines

    # ------------------------------------------------------------- nội bộ
    def _reset_state(self) -> None:
        self._ref = bytearray(self.size)
        self._ref_at: List[str] = [""] * self.size
        self._ref_count = 0
        self._gateways: Dict[str, GatewayFocus] = {}
        # Dựng sẵn dòng cho gateway đã cấu hình để ca "cả phiên không publish
        # bản tin nào của node" vẫn hiện ra với bắt hụt 100%.
        for uid, name in self._known.items():
            if len(self._gateways) >= self.max_gateways:
                break
            self._gateways[uid] = GatewayFocus(
                gateway_uid=uid, gateway_name=name, heard=bytearray(self.size)
            )
        self.total_copies = 0
        self.total_late = 0
        self.first_seen = ""
        self.last_seen = ""

    def _sorted_states(self) -> List[GatewayFocus]:
        """Sắp xếp: hụt nhiều nhất trước, rồi trễ nhiều, rồi gateway im lặng."""
        return sorted(
            self._gateways.values(),
            key=lambda s: (
                -(self._ref_count - s.packets_heard),
                -s.late_packets,
                s.copies > 0,
                s.gateway_uid,
            ),
        )

    def _remember_late(
        self, state: GatewayFocus, seq: int, received_at: str, gateway_ts: str, delay: float
    ) -> None:
        """Giữ lại `detail_limit` ca trễ NẶNG nhất cho mỗi gateway."""
        event = {
            "node_uid": self.node_uid,
            "gateway_uid": state.gateway_uid,
            "gateway_name": state.gateway_name,
            "seq": seq,
            "received_at": received_at,
            "gateway_timestamp": gateway_ts,
            "delay_seconds": round(delay, 3),
        }
        if len(state.late_events) < self.detail_limit:
            state.late_events.append(event)
            return
        # Thay ca nhẹ nhất bằng ca nặng hơn (chỉ chạy khi đã đầy → hiếm).
        lightest_index = min(
            range(len(state.late_events)),
            key=lambda i: float(state.late_events[i].get("delay_seconds") or 0.0),
        )
        if delay > float(state.late_events[lightest_index].get("delay_seconds") or 0.0):
            state.late_events[lightest_index] = event

    def _missing_ranges(self, state: GatewayFocus) -> List[tuple]:
        """Các đoạn liên tiếp trong `reference` mà gateway này không có."""
        ranges: List[tuple] = []
        start = -1
        missing = 0
        for seq in range(1, self.size):
            if self._ref[seq] and not state.heard[seq]:
                if start < 0:
                    start = seq
                missing += 1
            elif start >= 0:
                ranges.append((start, seq - 1, missing))
                start = -1
                missing = 0
        if start >= 0:
            ranges.append((start, self.size - 1, missing))
        return ranges

    def _overall_row(self, reference: int) -> Dict[str, Any]:
        heard = sum(s.packets_heard for s in self._gateways.values())
        # `heard_sum` đếm trùng giữa các gateway nên chỉ dùng để tham khảo; dòng
        # TỔNG lấy "bắt hụt" theo nghĩa TỔNG SỐ LẦN HỤT của mọi gateway.
        missed = sum(max(0, reference - s.packets_heard) for s in self._gateways.values())
        late = sum(s.late_packets for s in self._gateways.values())
        copies = sum(s.copies for s in self._gateways.values())
        delays = [s.delay_max for s in self._gateways.values() if s.delay_max is not None]
        # Gộp kiểu ngắt quãng: đếm gateway hụt MỘT MẠCH vs hụt RẢI RÁC. Dùng
        # CÙNG hàm phân loại với từng dòng gateway để hai chỗ không mâu thuẫn.
        runs = sum(s.gap_runs for s in self._gateways.values())
        largest = max((s.gap_largest for s in self._gateways.values()), default=0)
        contiguous = sum(
            1 for s in self._gateways.values()
            if _gap_kind(s.gap_runs, s.gap_total, s.gap_largest) == "contiguous"
        )
        scatter = sum(
            1 for s in self._gateways.values()
            if _gap_kind(s.gap_runs, s.gap_total, s.gap_largest) == "scatter"
        )
        # Mẫu số = số gateway trong bảng, KỂ CẢ gateway "câm" — nhờ vậy dòng TỔNG
        # đọc như "tỉ lệ hụt trung bình trên mọi gateway đã cấu hình".
        tracked = len(self._gateways)
        return {
            "node_uid": self.node_uid,
            "gateway_uid": "ALL",
            "gateway_name": "TỔNG (mọi gateway)",
            "packets_heard": heard,
            "copies": copies,
            "reference_packets": reference,
            "missed_catch": missed,
            "missed_catch_percent": (
                round(100.0 * missed / (reference * tracked), 2)
                if reference and tracked
                else None
            ),
            "gap_runs": runs,
            "gap_largest": largest,
            "gap_pattern": (
                "%d gateway có hụt liên tục, %d gateway hụt rải rác"
                % (contiguous, scatter)
                if runs
                else ("Không gateway nào bắt hụt" if reference else "")
            ),
            "late_packets": late,
            "late_percent": round(100.0 * late / copies, 2) if copies else None,
            "delay_avg_seconds": _avg(
                [s.delay_sum for s in self._gateways.values()],
                [s.delay_count for s in self._gateways.values()],
            ),
            "delay_min_seconds": _min_of(
                [s.delay_min for s in self._gateways.values()]
            ),
            "delay_max_seconds": max(delays) if delays else None,
            "gateways": tracked,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "notes": "Tổng hợp mọi gateway đã cấu hình (mỗi gateway góp 1 dòng).",
        }

    def _gateway_row(self, state: GatewayFocus, reference: int) -> Dict[str, Any]:
        missed = max(0, reference - state.packets_heard)
        percent = round(100.0 * missed / reference, 2) if reference else None
        if state.copies <= 0 and reference <= 0:
            percent = None
        return {
            "node_uid": self.node_uid,
            "gateway_uid": state.gateway_uid,
            "gateway_name": state.gateway_name,
            "packets_heard": state.packets_heard,
            "copies": state.copies,
            "reference_packets": reference,
            "missed_catch": missed,
            "missed_catch_percent": percent,
            "gap_runs": state.gap_runs,
            "gap_largest": state.gap_largest,
            "gap_pattern": state.gap_pattern,
            "late_packets": state.late_packets,
            "late_percent": state.late_percent(),
            "delay_avg_seconds": state.delay_avg(),
            "delay_min_seconds": (
                round(state.delay_min, 3) if state.delay_min is not None else None
            ),
            "delay_max_seconds": (
                round(state.delay_max, 3) if state.delay_max is not None else None
            ),
            "gateways": 1,
            "first_seen": state.first_seen,
            "last_seen": state.last_seen,
            "notes": _interpret(state, reference, missed, percent),
        }


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _gap_kind(runs: int, total: int, largest: int) -> str:
    """Phân loại kiểu ngắt quãng: ``"contiguous"`` / ``"scatter"`` / ``""``.

    Hàm dùng CHUNG cho từng gateway và dòng TỔNG để hai nơi không mâu thuẫn.

      * **contiguous** (hụt LIÊN TỤC): chỉ 1 khoảng, hoặc khoảng lớn nhất đã
        chiếm ≥70% và gấp ≥3 lần trung bình ⇒ phần còn lại chỉ là hạt vụn.
      * **scatter** (hụt RẢI RÁC): nhiều khoảng ngắn chia đều.
    """
    if runs <= 0 or total <= 0:
        return ""
    if runs == 1:
        return "contiguous"
    if largest >= 0.7 * total and largest >= 3 * (total / runs):
        return "contiguous"
    return "scatter"


def _gap_pattern(counts: List[int]) -> str:
    """Diễn giải KIỂU ngắt quãng từ danh sách độ dài các khoảng hụt.

    Phân biệt hai cơ chế hỏng rất khác nhau, mà `missed_catch` (chỉ là con số tổng)
    không thể hiện được:

      * **Hụt liên tục** — vài khoảng nhưng MỘT khoảng chiếm gần hết số hụt
        ⇒ gateway mất tầm sóng / offline một mạch (link down).
      * **Hụt rải rác** — nhiều khoảng ngắn rải đều
        ⇒ nghi mất gói RF/biên, gateway vẫn sống.

    Trả ``""`` khi không hụt gì.
    """
    total = sum(counts)
    if total <= 0:
        return ""
    runs = len(counts)
    largest = max(counts)
    if _gap_kind(runs, total, largest) == "contiguous":
        if runs == 1:
            return "Hụt LIÊN TỤC 1 mạch %d seq (nghi mất tầm sóng/offline)" % total
        others = total - largest
        return (
            "Hụt LIÊN TỤC 1 mạch %d seq, còn %d seq rải rác (nghi mất tầm sóng)"
            % (largest, others)
        )
    avg = total / runs
    if avg < 5.0:
        return (
            "Hụt RẢI RÁC: %d khoảng, trung bình %.1f seq (nghi mất gói RF)"
            % (runs, avg)
        )
    return "Hụt thành %d khoảng, lớn nhất %d seq, trung bình %.1f seq" % (
        runs, largest, avg,
    )


def _interpret(
    state: GatewayFocus, reference: int, missed: int, percent: Optional[float]
) -> str:
    """Câu diễn giải ngắn — để đọc CSV là biết ngay ca nào đáng nghi."""
    if state.copies <= 0:
        if reference <= 0:
            return "Chưa có bản tin nào của node trong phiên"
        return (
            "KHÔNG publish bản tin nào của node (bắt hụt 100%) "
            "— kiểm tra gateway còn chạy / còn trong tầm sóng"
        )
    if reference <= 0:
        return "Chưa có bản tin nào của node trong phiên"
    parts: List[str] = []
    if missed <= 0:
        parts.append("Không bắt hụt")
    elif percent is not None and percent >= 50.0:
        parts.append("Bắt hụt rất cao (%.1f%%)" % percent)
    elif percent is not None and percent >= 10.0:
        parts.append("Bắt hụt cao (%.1f%%)" % percent)
    else:
        parts.append("Bắt hụt thấp (%.1f%%)" % (percent or 0.0))
    if state.gap_pattern:
        parts.append(state.gap_pattern)
    if state.copies > 0:
        late_percent = state.late_percent() or 0.0
        if late_percent >= 50.0:
            parts.append("bắn trễ chiếm ưu thế")
        elif state.late_packets:
            parts.append("có bắn trễ")
    if state.delay_missing:
        parts.append("%d bản tin không có timestamp" % state.delay_missing)
    return "; ".join(parts)


def _delay_seconds(received_at: str, gateway_timestamp: str) -> Optional[float]:
    """Độ trễ = giờ PC nhận − giờ gateway tạo bản tin (giây).

    Trả None nếu thiếu/không parse được một trong hai mốc. Giá trị ÂM là bình
    thường (đồng hồ gateway chạy trước PC) nên vẫn trả về để xem được lệch giờ.
    """
    received = parse_stamp(received_at)
    gateway = parse_stamp(gateway_timestamp)
    if received is None or gateway is None:
        return None
    try:
        return (received - gateway).total_seconds()
    except TypeError:  # phòng khi lệch aware/naive từ dữ liệu ngoài
        return None


def _avg(sums: List[float], counts: List[int]) -> Optional[float]:
    total = sum(sums)
    count = sum(counts)
    if count <= 0:
        return None
    return round(total / count, 3)


def _min_of(values: List[Optional[float]]) -> Optional[float]:
    present = [v for v in values if v is not None]
    return round(min(present), 3) if present else None
