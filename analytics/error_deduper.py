"""Gộp các bản copy của CÙNG một khung lỗi LoRa thành 1 dòng `mqtt_errors`.

Vấn đề:
  `node/error` (CRC ERROR / INVALID UUID / PACKET LENGTH ERROR) cũng được **mọi**
  gateway trong tầm publish lại như packet DATA. Một khung lỗi phát 1 lần → nhiều
  dòng CSV lỗi, gateway nào cũng báo.

Đo trên phiên chạy thật (8 gateway):
  - 3 367 dòng `mqtt_errors` / 3 142 khung lỗi khác nhau (1.07×).
  - 120 nhóm trùng, có nhóm **9 bản copy** (đủ 9 gateway đều nghe).
  - `raw_hex` KHÔNG rỗng trong 100 % dòng, nhưng vẫn dự phòng `payload_json`.

Khoá gộp: `(error, raw_hex)` — *KHÔNG* dùng `gateway_uid` vì chính nhiều gateway
cùng nghe một khung lỗi. Lý do quan trọng: khung lỗi **không có seq** nên không
thể dùng `(node_uid, seq)` như `PacketDeduper`.

Cách dùng: `add()` gom bản copy, `flush_due()` / `flush_all()` chốt 1 dòng/nhóm
(với `gateways_heard` + RSSI tốt nhất), giống hệt `PacketDeduper`.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Số bản copy tối đa giữ lại cho 1 khung lỗi (chọn RSSI tốt nhất + đếm gateway).
COPY_HISTORY = 32
# Số khung lỗi đang chờ gộp tối đa (chống phình RAM).
MAX_PENDING_FRAMES = 20000

#: Khoá khung lỗi: (loại lỗi, nội dung khung).
FrameKey = Tuple[str, str]


def frame_key(error: str, raw_hex: str, payload_json: str = "") -> FrameKey:
    """Khoá nhận dạng khung lỗi — dự phòng `payload_json` khi thiếu `raw_hex`."""
    body = (raw_hex or "").strip() or (payload_json or "").strip()
    return ((error or "").strip(), body)


@dataclass
class FrameCopy:
    """Một lần publish khung lỗi (từ một gateway)."""

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
class MergedFrame:
    """1 khung lỗi logic = 1 dòng `mqtt_errors`."""

    key: FrameKey
    error: str
    copies: List[FrameCopy]
    #: Mốc tool nhận bản copy đầu tiên (`time.monotonic()`, giây).
    arrival: float = 0.0

    @property
    def primary(self) -> FrameCopy:
        return min(self.copies, key=lambda c: c.rank())

    def gateways(self) -> List[str]:
        return sorted({c.gateway_uid.upper() for c in self.copies if c.gateway_uid})

    @property
    def gateways_heard(self) -> int:
        return len(self.gateways())

    def earliest_received_at(self) -> str:
        stamps = [c.received_at for c in self.copies if c.received_at]
        return min(stamps) if stamps else ""

    def latest_received_at(self) -> str:
        stamps = [c.received_at for c in self.copies if c.received_at]
        return max(stamps) if stamps else ""

    def to_row(self) -> Dict[str, Any]:
        """Dòng `mqtt_errors` cho khung lỗi logic (cột theo `MQTT_ERROR_COLUMNS`)."""
        primary = self.primary
        record = primary.record
        if record is None:
            return {}
        return {
            "received_at": self.earliest_received_at() or primary.received_at,
            "received_last_at": self.latest_received_at() or primary.received_at,
            "gateway_uid": primary.gateway_uid,
            "gateway_name": primary.gateway_name,
            "gateways_heard": self.gateways_heard,
            "gateways": "|".join(self.gateways()),
            "copies": len(self.copies),
            "receiver_uid": primary.receiver_uid,
            "topic": getattr(record, "topic", ""),
            "error": self.error,
            "gateway_timestamp": getattr(record, "gateway_timestamp", ""),
            "rssi": "" if not isinstance(primary.rssi, (int, float)) else primary.rssi,
            "snr": "" if not isinstance(primary.snr, (int, float)) else primary.snr,
            "raw_hex": getattr(record, "raw_hex", ""),
            "payload_json": getattr(record, "payload_json", ""),
        }

    def to_data_row(self) -> Dict[str, Any]:
        """Dòng `mqtt_data` (schema MQTT_DATA_COLUMNS) cho khung lỗi đã gộp.

        `mqtt_data` là file forensic GỒM MỌI loại bản tin, nên khung lỗi cũng
        phải xuất hiện ở đó — nhưng với cột của schema DATA (có `packet_type`,
        `gateways_heard`, `received_last_at`), không phải cột của `mqtt_errors`.
        """
        primary = self.primary
        record = primary.record
        if record is None:
            return {}
        row = record.to_row()
        row["received_at"] = self.earliest_received_at() or primary.received_at
        row["received_last_at"] = self.latest_received_at() or primary.received_at
        row["gateway_uid"] = primary.gateway_uid
        row["gateway_name"] = primary.gateway_name
        row["receiver_uid"] = primary.receiver_uid
        row["rssi"] = "" if not isinstance(primary.rssi, (int, float)) else primary.rssi
        row["snr"] = "" if not isinstance(primary.snr, (int, float)) else primary.snr
        row["gateways_heard"] = self.gateways_heard
        row["gateways"] = "|".join(self.gateways())
        return row


class ErrorFrameCoalescer:
    """Gộp bản copy của cùng một khung lỗi trong `merge_window_seconds`."""

    def __init__(
        self,
        merge_window_seconds: float,
        copy_history: int = COPY_HISTORY,
        max_pending: int = MAX_PENDING_FRAMES,
    ) -> None:
        self.merge_window = max(0.0, float(merge_window_seconds))
        self.copy_history = max(2, int(copy_history))
        self.max_pending = max(100, int(max_pending))

        self._pending: Dict[FrameKey, MergedFrame] = {}
        self._order: Deque[FrameKey] = deque()
        self.merged_frames = 0
        self.copies_merged = 0
        self.evicted = 0

    # ------------------------------------------------------------------ API
    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def stats(self) -> Dict[str, Any]:
        return {
            "merged_frames": self.merged_frames,
            "copies_merged": self.copies_merged,
            "pending": len(self._pending),
            "evicted": self.evicted,
        }

    def reset(self) -> None:
        self._pending.clear()
        self._order.clear()
        self.merged_frames = 0
        self.copies_merged = 0
        self.evicted = 0

    def add(self, key: FrameKey, copy: FrameCopy, arrival: Optional[float] = None) -> None:
        """Gom 1 bản copy khung lỗi vào nhóm `key`.

        `arrival` là mốc `time.monotonic()` lúc tool nhận bản tin (mặc định: bây giờ).
        """
        frame = self._pending.get(key)
        if frame is not None:
            frame.copies.append(copy)
            if len(frame.copies) > self.copy_history:
                frame.copies.sort(key=lambda c: c.rank())
                del frame.copies[self.copy_history:]
            self.copies_merged += 1
            return
        self._pending[key] = MergedFrame(
            key=key,
            error=key[0],
            copies=[copy],
            arrival=time.monotonic() if arrival is None else float(arrival),
        )
        self._order.append(key)
        self._evict_if_needed()

    def flush_due(self, now: Optional[float] = None) -> List[MergedFrame]:
        """Chốt các khung lỗi đã ra khỏi cửa sổ gộp (duyệt theo thứ tự chèn).

        `now` là mốc `time.monotonic()` (mặc định: bây giờ).
        """
        now = time.monotonic() if now is None else float(now)
        closed: List[MergedFrame] = []
        while self._order:
            key = self._order[0]
            frame = self._pending.get(key)
            if frame is None:
                self._order.popleft()
                continue
            if (now - frame.arrival) < self.merge_window:
                break
            self._order.popleft()
            del self._pending[key]
            closed.append(frame)
        return self._finalise(closed)

    def flush_all(self) -> List[MergedFrame]:
        """Chốt toàn bộ khung lỗi đang chờ (lúc dừng phiên)."""
        closed = list(self._pending.values())
        self._pending.clear()
        self._order.clear()
        return self._finalise(closed)

    # ------------------------------------------------------------- nội bộ
    def _finalise(self, closed: List[MergedFrame]) -> List[MergedFrame]:
        closed.sort(key=lambda f: (f.arrival, f.key))
        self.merged_frames += len(closed)
        return closed

    def _evict_if_needed(self) -> None:
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
            "Bộ gộp khung lỗi vượt %d khung đang chờ — chốt sớm %d khung cũ nhất",
            self.max_pending, overflow,
        )


__all__ = [
    "COPY_HISTORY",
    "MAX_PENDING_FRAMES",
    "ErrorFrameCoalescer",
    "FrameCopy",
    "FrameKey",
    "MergedFrame",
    "frame_key",
]
