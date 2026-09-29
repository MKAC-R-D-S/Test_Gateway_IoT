"""Gộp N bản copy của CÙNG một bản tin LoRa thành 1 bản ghi logic.

Vấn đề (đo trên phiên chạy thật 8 gateway / 51 node):
  - 1 node phát 1 bản tin LoRa → **mọi** gateway trong tầm đều nghe và publish
    lên MQTT. Vì vậy `mqtt_data` có N bản ghi cho 1 bản tin logic
    (24 788 dòng thay vì 3 470).
  - Một gateway có thể publish lại cùng bản tin (buffer/retry), nên bản copy
    không chỉ đến từ các gateway khác nhau.

Giải pháp:
  - Bản tin logic được định danh bằng `(node_uid, seq)`.
    *KHÔNG* dùng `gateway_uid` / `receiver_uid` làm khoá: `gateway_uid` có thể
    rỗng (bản tin node cũ) và 48/51 node được nghe bởi 9 `receiver_uid` khác
    nhau, nên 2 trường này không định danh được bản tin.
  - Bản copy tới trong `merge_window_seconds` được **gộp** thành 1 bản ghi, kèm
    `gateways_heard` (số gateway đã nghe) và gateway có **RSSI tốt nhất**.
  - Bản copy tới muộn hơn cửa sổ (gateway buffer SD rồi publish lại hàng phút
    sau) **không bị mất**: nó ra thành dòng riêng và `SequenceTracker` tự phân
    loại đúng là `OUT_OF_ORDER` / `RESET` — tool không âm thầm bỏ dữ liệu.

Vì sao cần cửa sổ gộp chứ không chỉ khớp khoá tuyệt đối: các gateway nghe cùng
một bản tin lệch nhau vài chục ms (p50 = 31 ms, p90 = 43 ms, p99 = 83 ms — đo
trên dữ liệu thật). Nếu khớp tuyệt đối (cửa sổ 0 s) thì mỗi bản tin vẫn tách
thành nhiều dòng. Đo trên dữ liệu thật:

| Cửa sổ | Dòng   | Bản tin logic | Bản copy tách riêng |
|---|---:|---:|---:|
| 0 s    | 16 608 | 3 470 | 13 138 |
| 2 s    | 3 478  | 3 470 | 8 (0.03 %) |
| 60 s   | 3 474  | 3 470 | 4 |

→ mặc định 2 s (cấu hình `packet_merge_window_seconds`).

**Đồng hồ đo cửa sổ là `time.monotonic()` — lúc tool NHẬN bản tin**, KHÔNG phải
`received_at` trong payload. Lý do: gateway có thể lệch giờ với PC, hoặc publish
lại dữ liệu cũ từ buffer SD; nếu so với giờ trong payload thì cửa sổ gộp sẽ vô
hiệu (thậm chí treo bản tin vô hạn vì mốc nằm ở quá khứ). `received_at` chỉ dùng
để chọn bản copy "đại diện" và ghi lại mốc thời gian.

Chi phí bộ nhớ: mỗi bản tin chỉ sống trong cửa sổ gộp (vài giây) nên RAM không
tăng theo độ dài phiên chạy 3-4 ngày.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Cửa sổ gộp mặc định (giây) — xem bảng đo trong docstring ở trên.
DEFAULT_MERGE_WINDOW_SECONDS = 2.0
# Số bản copy tối đa giữ lại cho 1 bản tin (để chọn RSSI tốt nhất + đếm gateway).
COPY_HISTORY = 64
# Số bản tin đang chờ gộp tối đa (chống phình RAM nếu dữ liệu dồn dập).
MAX_PENDING_PACKETS = 20000

# Khoá bản tin logic.
PacketKey = Tuple[str, int]


def parse_stamp(text: str) -> Optional[datetime]:
    """Parse `received_at` → datetime **naive local** để so sánh nhất quán.

    Mọi `received_at` do tool sinh ra đều theo cùng một múi giờ
    (`datetime.now().astimezone()`), nhưng vẫn quy hết về naive local để không
    bao giờ so sánh aware với naive (lỗi TypeError).
    """
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value


@dataclass
class PacketCopy:
    """Một bản copy của bản tin (do 1 gateway publish)."""

    gateway_uid: str = ""
    gateway_name: str = ""
    receiver_uid: str = ""
    rssi: Optional[float] = None
    snr: Optional[float] = None
    received_at: str = ""
    at: Optional[datetime] = None
    record: Any = None

    def rank(self) -> tuple:
        """RSSI cao nhất trước; cùng RSSI thì bản tới sớm hơn trước."""
        rssi = self.rssi if isinstance(self.rssi, (int, float)) else float("-inf")
        return (-rssi, self.at or datetime.max)


@dataclass
class MergedPacket:
    """1 bản tin logic = 1 dòng CSV."""

    node_uid: str
    seq: int
    copies: List[PacketCopy] = field(default_factory=list)
    #: Mốc tool nhận bản copy đầu tiên (`time.monotonic()`, giây).
    arrival: float = 0.0

    # ------------------------------------------------------------- thuộc tính
    @property
    def primary(self) -> PacketCopy:
        """Bản copy 'đại diện': gateway có RSSI tốt nhất."""
        return min(self.copies, key=lambda c: c.rank())

    def gateways(self) -> List[str]:
        """Gateway đã nghe bản tin (đã sắp xếp, bỏ trùng, bỏ rỗng)."""
        return sorted({c.gateway_uid.upper() for c in self.copies if c.gateway_uid})

    @property
    def gateways_heard(self) -> int:
        return len(self.gateways())

    @property
    def best_rssi(self) -> Optional[float]:
        best = self.primary.rssi
        return best if isinstance(best, (int, float)) else None

    @property
    def best_snr(self) -> Optional[float]:
        best = self.primary.snr
        return best if isinstance(best, (int, float)) else None

    def earliest_received_at(self) -> str:
        stamps = [c.received_at for c in self.copies if c.received_at]
        return min(stamps) if stamps else ""

    def latest_received_at(self) -> str:
        stamps = [c.received_at for c in self.copies if c.received_at]
        return max(stamps) if stamps else ""

    def to_row(self) -> Dict[str, Any]:
        """Dòng `mqtt_data` cho bản tin logic.

        Payload giống hệt nhau giữa các gateway (chỉ RSSI/SNR/thời điểm nhận là
        khác), nên lấy bản copy tốt nhất làm nội dung rồi ghi đè bằng số liệu
        tổng hợp của cả cụm.
        """
        primary = self.primary
        if primary.record is None:
            return {}
        row = primary.record.to_row()
        row["received_at"] = self.earliest_received_at() or primary.received_at
        row["received_last_at"] = self.latest_received_at() or primary.received_at
        row["gateway_uid"] = primary.gateway_uid
        row["gateway_name"] = primary.gateway_name
        row["receiver_uid"] = primary.receiver_uid
        row["rssi"] = "" if self.best_rssi is None else self.best_rssi
        row["snr"] = "" if self.best_snr is None else self.best_snr
        row["gateways_heard"] = self.gateways_heard
        row["gateways"] = "|".join(self.gateways())
        return row


class PacketDeduper:
    """Gộp các bản copy trong `merge_window_seconds` thành 1 bản tin logic.

    `add()` chỉ *gom* bản copy; bản tin chỉ được chốt (và trả về) khi đã ra khỏi
    cửa sổ gộp qua `flush_due()` / `flush_all()`.
    """

    def __init__(
        self,
        merge_window_seconds: float = DEFAULT_MERGE_WINDOW_SECONDS,
        copy_history: int = COPY_HISTORY,
        max_pending: int = MAX_PENDING_PACKETS,
    ) -> None:
        self.merge_window = max(0.0, float(merge_window_seconds))
        self.copy_history = max(2, int(copy_history))
        self.max_pending = max(100, int(max_pending))

        self._pending: Dict[PacketKey, MergedPacket] = {}
        self._order: Deque[PacketKey] = deque()
        self.merged_packets = 0
        self.copies_merged = 0
        self.evicted = 0

    # ------------------------------------------------------------------ API
    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def stats(self) -> Dict[str, Any]:
        return {
            "merged_packets": self.merged_packets,
            "copies_merged": self.copies_merged,
            "pending": len(self._pending),
            "evicted": self.evicted,
        }

    def reset(self) -> None:
        self._pending.clear()
        self._order.clear()
        self.merged_packets = 0
        self.copies_merged = 0
        self.evicted = 0

    def add(
        self,
        node_uid: str,
        seq: int,
        copy: PacketCopy,
        arrival: Optional[float] = None,
    ) -> None:
        """Gom 1 bản copy vào bản tin `(node_uid, seq)`.

        `arrival` là mốc `time.monotonic()` lúc tool nhận bản tin (mặc định: bây giờ).
        """
        key: PacketKey = (node_uid, int(seq))
        packet = self._pending.get(key)
        if packet is not None:
            packet.copies.append(copy)
            if len(packet.copies) > self.copy_history:
                packet.copies.sort(key=lambda c: c.rank())
                del packet.copies[self.copy_history:]
            self.copies_merged += 1
            return
        self._pending[key] = MergedPacket(
            node_uid=node_uid,
            seq=int(seq),
            copies=[copy],
            arrival=time.monotonic() if arrival is None else float(arrival),
        )
        self._order.append(key)
        self._evict_if_needed()

    def flush_due(self, now: Optional[float] = None) -> List[MergedPacket]:
        """Chốt các bản tin đã ra khỏi cửa sổ gộp.

        `now` là mốc `time.monotonic()` (mặc định: bây giờ).

        Duyệt theo **thứ tự chèn** (deque) nên chi phí bậc O(số bản tin vừa chốt)
        chứ không phải O(tổng số bản tin đang chờ) — quan trọng vì hàm này được
        gọi liên tục trong analyse thread.
        """
        now = time.monotonic() if now is None else float(now)
        closed: List[MergedPacket] = []
        while self._order:
            key = self._order[0]
            packet = self._pending.get(key)
            if packet is None:
                self._order.popleft()
                continue
            if (now - packet.arrival) < self.merge_window:
                break
            self._order.popleft()
            del self._pending[key]
            closed.append(packet)
        return self._finalise(closed)

    def flush_all(self) -> List[MergedPacket]:
        """Chốt toàn bộ bản tin đang chờ (lúc dừng phiên)."""
        closed = list(self._pending.values())
        self._pending.clear()
        self._order.clear()
        return self._finalise(closed)

    # ------------------------------------------------------------- nội bộ
    def _finalise(self, closed: List[MergedPacket]) -> List[MergedPacket]:
        closed.sort(key=lambda p: (p.arrival, p.node_uid, p.seq))
        self.merged_packets += len(closed)
        return closed

    def _evict_if_needed(self) -> None:
        """Chốt sớm bản tin cũ nhất nếu số bản tin đang chờ vượt ngưỡng."""
        overflow = len(self._pending) - self.max_pending
        if overflow <= 0:
            return
        for _ in range(overflow):
            try:
                key = self._order.popleft()
            except IndexError:
                break
            if self._pending.pop(key, None) is not None:
                self.evicted += 1
        log.warning(
            "Bộ gộp bản tin vượt %d bản tin đang chờ — chốt sớm %d bản tin cũ nhất",
            self.max_pending, overflow,
        )
