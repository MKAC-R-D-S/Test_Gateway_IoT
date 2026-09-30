"""Theo dõi ping & phát hiện downtime gateway.

Nguyên tắc quan trọng:
  - 1 lần ping FAIL **không** phải downtime. Mất 1 gói ICMP là chuyện bình
    thường (WiFi/switch bận). Chỉ coi là DOWN sau `down_threshold` lần fail
    LIÊN TIẾP (mặc định 2) — cấu hình trong `config.json`.
  - Downtime WINDOW = khoảng từ lần fail đầu tiên tới lần ping thành công kế
    tiếp. Ghi 1 dòng vào `gateway_downtime.csv` khi window đóng, hoặc khi tool
    dừng mà window còn mở (`status = ONGOING`).
  - `first_down_ping` / `last_down_ping` giữ lại để biết lần fail đầu/cuối —
    vì khi mở window, ta chưa chắc chắn (mới fail lần đầu) nên phải ghi nhớ.
  - Đây là bằng chứng CẤP ĐƯỜNG MẠNG. Bằng chứng cấp gateway (mqtt LWT
    `status=offline`) được theo dõi riêng — xem docs §3.5.
"""

from __future__ import annotations

import csv
import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.models import DowntimeRecord, PingRecord
from core.uid import gateway_uid_index, gateway_uid_variants

log = logging.getLogger(__name__)


@dataclass
class PingStats:
    """Tổng hợp một gateway."""

    gateway_uid: str = ""
    gateway_name: str = ""
    ip_address: str = ""
    total: int = 0
    success: int = 0
    failed: int = 0
    timeouts: int = 0
    latency_samples: List[float] = field(default_factory=list)
    downtime_events: int = 0
    downtime_seconds: float = 0.0
    first_ping: str = ""
    last_ping: str = ""
    consecutive_failures: int = 0
    max_consecutive_failures: int = 0

    def success_rate(self) -> Optional[float]:
        if self.total <= 0:
            return None
        return round(100.0 * self.success / self.total, 4)

    def latency_avg(self) -> Optional[float]:
        if not self.latency_samples:
            return None
        return round(statistics.fmean(self.latency_samples), 3)

    def latency_min(self) -> Optional[float]:
        if not self.latency_samples:
            return None
        return round(min(self.latency_samples), 3)

    def latency_max(self) -> Optional[float]:
        if not self.latency_samples:
            return None
        return round(max(self.latency_samples), 3)

    def latency_p95(self) -> Optional[float]:
        """p95 — chỉ số hữu ích hơn trung bình cho mạng chập chờn."""
        if len(self.latency_samples) < 5:
            return self.latency_max()
        ordered = sorted(self.latency_samples)
        index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
        return round(ordered[index], 3)

    def jitter(self) -> Optional[float]:
        """Độ lệch chuẩn latency — dấu hiệu mạng không ổn định."""
        if len(self.latency_samples) < 2:
            return None
        try:
            return round(statistics.pstdev(self.latency_samples), 3)
        except statistics.StatisticsError:
            return None

    def to_summary_row(self) -> Dict[str, Any]:
        return {
            "gateway_uid": self.gateway_uid,
            "gateway_name": self.gateway_name,
            "ip_address": self.ip_address,
            "ping_total": self.total,
            "ping_success": self.success,
            "ping_failed": self.failed,
            # `success_rate()` trả None khi chưa ping lần nào; ở đây đổi thành 0.0
            # vì cột này đi vào bảng GUI dạng số (`` để trống thì không sắp xếp /
            # định dạng được). Cột `success_rate_percent` trong ping_summary.csv
            # vẫn giữ "" cho dòng chưa có dữ liệu để CSV không nói dối.
            "success_rate_percent": self.success_rate() if self.success_rate() is not None else "",
            "success_rate": self.success_rate() or 0.0,
            "latency_avg_ms": self.latency_avg() if self.latency_avg() is not None else "",
            "latency_min_ms": self.latency_min() if self.latency_min() is not None else "",
            "latency_max_ms": self.latency_max() if self.latency_max() is not None else "",
            "latency_p95_ms": self.latency_p95() if self.latency_p95() is not None else "",
            "jitter_ms": self.jitter() if self.jitter() is not None else "",
            "downtime_events": self.downtime_events,
            "downtime_seconds": round(self.downtime_seconds, 3),
            "first_ping": self.first_ping,
            "last_ping": self.last_ping,
        }


@dataclass
class _OpenWindow:
    """Downtime window đang mở (chưa recover)."""

    first_down_ping: str
    last_down_ping: str
    first_down_dt: datetime
    failed_count: int
    confirmed: bool = False


class PingTracker:
    """Nhận từng kết quả ping, phát hiện downtime window, xuất summary."""

    def __init__(self, down_threshold: int = 2) -> None:
        self.down_threshold = max(1, int(down_threshold))
        self._stats: Dict[str, PingStats] = {}
        self._open: Dict[str, _OpenWindow] = {}
        self._closed: List[DowntimeRecord] = []
        self.ping_records: int = 0

    # ------------------------------------------------------------------ API
    def register(self, gateway_uid: str, gateway_name: str = "", ip_address: str = "") -> PingStats:
        key = (gateway_uid or "").upper()
        stats = self._stats.get(key)
        if stats is None:
            stats = PingStats(
                gateway_uid=key, gateway_name=gateway_name, ip_address=ip_address
            )
            self._stats[key] = stats
        else:
            if gateway_name:
                stats.gateway_name = gateway_name
            if ip_address:
                stats.ip_address = ip_address
        return stats

    def feed(self, result: PingRecord) -> Optional[DowntimeRecord]:
        """Nhận 1 kết quả ping. Trả `DowntimeRecord` nếu một window vừa ĐÓNG."""
        key = (result.gateway_uid or "").upper()
        stats = self.register(key, result.gateway_name, result.ip_address)
        self.ping_records += 1
        stats.total += 1
        if not stats.first_ping:
            stats.first_ping = result.timestamp
        stats.last_ping = result.timestamp

        if result.ping_success:
            stats.success += 1
            if result.latency_ms is not None and result.latency_ms >= 0:
                stats.latency_samples.append(float(result.latency_ms))
            stats.consecutive_failures = 0
            return self._close_window(key, result.timestamp)
        return self._register_failure(stats, result)

    def close_all(self, end_timestamp: str = "") -> List[DowntimeRecord]:
        """Đóng mọi window còn mở (gọi khi tool dừng) → status ONGOING."""
        out: List[DowntimeRecord] = []
        for key in list(self._open.keys()):
            record = self._close_window(key, end_timestamp, ongoing=True)
            if record is not None:
                out.append(record)
        return out

    def stats(self) -> Dict[str, PingStats]:
        return self._stats

    def reset_failure_streaks(self) -> None:
        """Xoá chuỗi fail LIÊN TIẾP của mọi gateway (giữ `max_consecutive_failures`).

        Gọi khi PHIÊN ĐƯỢC TIẾP TỤC sau tạm dừng. Chuỗi fail của lần ping trước
        không còn "liên tiếp" nữa vì đã có một khoảng tool KHÔNG ping. Nếu không
        xoá, hai lần fail trước khi tạm dừng cộng với lần fail đầu sau khi tiếp
        tục sẽ đủ ngưỡng và sinh một cửa sổ DOWNTIME giả trong báo cáo.
        """
        for stats in self._stats.values():
            stats.consecutive_failures = 0

    def stats_sorted(self) -> List[PingStats]:
        return sorted(self._stats.values(), key=lambda s: s.gateway_uid)

    def summary_rows(self) -> List[Dict[str, Any]]:
        """Một dòng / gateway (đúng cột của `ping_summary.csv`)."""
        return [s.to_summary_row() for s in self.stats_sorted()]

    def rows_by_uid(self) -> Dict[str, Dict[str, Any]]:
        """Bảng tra {mọi cách viết gateway UID → dòng tổng hợp}.

        Tab "Thiết bị" cần tỉ lệ ping của TỪNG gateway trong `devices.json`; UID
        trong đó có thể là 12 hex còn firmware publish 11 hex, nên phải tra qua
        biến thể UID (xem `core.uid.gateway_uid_variants`) chứ không so chuỗi.
        """
        return gateway_uid_index(self.summary_rows())

    def success_rate_by_uid(self) -> Dict[str, Optional[float]]:
        """{mọi cách viết gateway UID → tỉ lệ ping (%)}. Chưa ping lần nào → None."""
        out: Dict[str, Optional[float]] = {}
        for variant, row in self.rows_by_uid().items():
            value = row.get("success_rate_percent")
            out[variant] = None if value in (None, "") else float(value)
        return out

    def downtime_records(self) -> List[DowntimeRecord]:
        return list(self._closed)

    def open_windows(self) -> Dict[str, Dict[str, Any]]:
        """Snapshot các window đang mở (để hiển thị / lưu vào meta)."""
        out: Dict[str, Dict[str, Any]] = {}
        now = datetime.now().astimezone()
        for key, window in self._open.items():
            out[key] = {
                "first_down_ping": window.first_down_ping,
                "last_down_ping": window.last_down_ping,
                "failed_count": window.failed_count,
                "confirmed": window.confirmed,
                "ongoing_seconds": self._elapsed(window, now),
            }
        return out

    # ------------------------------------------------------------- nội bộ
    def _register_failure(self, stats: PingStats, result: PingRecord) -> None:
        key = stats.gateway_uid
        stats.failed += 1
        if result.timeout:
            stats.timeouts += 1
        stats.consecutive_failures += 1
        stats.max_consecutive_failures = max(
            stats.max_consecutive_failures, stats.consecutive_failures
        )

        window = self._open.get(key)
        if window is None:
            self._open[key] = _OpenWindow(
                first_down_ping=result.timestamp,
                last_down_ping=result.timestamp,
                first_down_dt=_parse_dt(result.timestamp) or datetime.now().astimezone(),
                failed_count=1,
                confirmed=self.down_threshold <= 1,
            )
            if self.down_threshold <= 1:
                stats.downtime_events += 1
            return

        window.last_down_ping = result.timestamp
        window.failed_count += 1
        if not window.confirmed and window.failed_count >= self.down_threshold:
            window.confirmed = True
            stats.downtime_events += 1

    def _close_window(
        self, key: str, end_timestamp: str, ongoing: bool = False
    ) -> Optional[DowntimeRecord]:
        window = self._open.pop(key, None)
        if window is None:
            return None
        stats = self._stats.get(key)
        if stats is None or not window.confirmed:
            # Chưa đủ ngưỡng để coi là DOWN → không ghi (tránh false positive).
            if stats is not None and not window.confirmed:
                stats.downtime_events = max(0, stats.downtime_events)
            return None

        if ongoing:
            end_dt = datetime.now().astimezone()
            end_text = end_timestamp or end_dt.isoformat(timespec="seconds")
        else:
            end_dt = _parse_dt(end_timestamp) or datetime.now().astimezone()
            end_text = end_timestamp

        duration = self._elapsed(window, end_dt)
        if stats is not None:
            stats.downtime_seconds += duration

        record = DowntimeRecord(
            gateway_uid=key,
            gateway_name=stats.gateway_name if stats else "",
            ip_address=stats.ip_address if stats else "",
            down_start=window.first_down_ping,
            down_end=end_text,
            duration_seconds=round(duration, 3),
            failed_ping_count=window.failed_count,
            status="ONGOING" if ongoing else "CLOSED",
            recovered=not ongoing,
            last_down_ping=window.last_down_ping,
        )
        self._closed.append(record)
        return record

    @staticmethod
    def _elapsed(window: _OpenWindow, end_dt: datetime) -> float:
        try:
            if end_dt.tzinfo is None and window.first_down_dt.tzinfo is not None:
                end_dt = end_dt.replace(tzinfo=window.first_down_dt.tzinfo)
            if end_dt.tzinfo is not None and window.first_down_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=None)
            return max(0.0, (end_dt - window.first_down_dt).total_seconds())
        except (TypeError, ValueError):
            return 0.0


def _parse_dt(text: str) -> Optional[datetime]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def summarize_ping_csv(rows: List[Dict[str, Any]], down_threshold: int = 2) -> "PingTracker":
    """Dựng lại PingTracker từ các dòng `ping_log_*.csv` (CSV = source of truth)."""
    tracker = PingTracker(down_threshold)
    for row in rows:
        success = str(row.get("ping_success", "")).strip().lower() in ("1", "true", "yes")
        latency = _to_float(row.get("latency_ms"))
        tracker.feed(
            PingRecord(
                timestamp=str(row.get("timestamp", "")),
                gateway_uid=str(row.get("gateway_uid", "")).strip().upper(),
                gateway_name=str(row.get("gateway_name", "")),
                ip_address=str(row.get("ip_address", "")),
                ping_success=success,
                latency_ms=latency,
                timeout=str(row.get("timeout", "")).strip().lower() in ("1", "true", "yes"),
                error=str(row.get("error", "")),
            )
        )
    return tracker


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_ping_summary_file(path: Path) -> List[Dict[str, Any]]:
    """Đọc `reports/ping_summary.csv` → list[dòng]. File thiếu/hỏng → [].

    Dùng khi phiên KHÔNG chạy: `PingTracker` trong RAM đã mất, số liệu duy nhất
    còn lại là `ping_summary.csv` của phiên gần nhất (CSV = source of truth,
    xem README §2). Không ném exception để GUI chỉ hiện "-" chứ không sập.
    """
    path = Path(path)
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                rows.append(dict(row))
    except (OSError, csv.Error) as exc:  # noqa: BLE001 - CSV cũ có thể hỏng
        log.warning("Không đọc được %s: %s", path, exc)
        return []
    return rows


def ping_summary_index(rows: object = None, path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Bảng tra {mọi cách viết gateway UID → dòng `ping_summary`}.

    Truyền `rows` (đã đọc sẵn) hoặc `path` (file CSV). Trả {} nếu không có dữ
    liệu — người gọi hiển thị "-" thay vì giả định tỉ lệ 0 %.
    """
    if rows is None:
        rows = read_ping_summary_file(path) if path is not None else []
    return gateway_uid_index(rows)


def latest_ping_summary_index(data_dir: Path) -> Dict[str, Dict[str, Any]]:
    """`ping_summary.csv` của phiên MỚI NHẤT trong `data_dir/runs/` → index UID.

    Cho phép tab "Thiết bị" vẫn hiện được tỉ lệ ping của phiên vừa chạy sau khi
    tool khởi động lại (lúc đó `PingTracker` trong RAM chưa có gì).
    """
    try:
        from storage.session import latest_run_dir
    except ImportError:  # pragma: no cover - chỉ xảy ra khi thiếu package
        return {}
    run_dir = latest_run_dir(Path(data_dir))
    if run_dir is None:
        return {}
    return ping_summary_index(path=Path(run_dir) / "reports" / "ping_summary.csv")


def success_rate_index(source: object) -> Dict[str, Optional[float]]:
    """{mọi cách viết gateway UID → tỉ lệ ping (%)} từ CSV/PingTracker/dict.

    `source` nhận: `PingTracker`, list[dòng dict], dict đã index, hoặc `None`.
    Dòng thiếu `success_rate_percent` → None (GUI hiện "-").
    """
    if source is None:
        index: Dict[str, Dict[str, Any]] = {}
    elif isinstance(source, PingTracker):
        return source.success_rate_by_uid()
    elif isinstance(source, dict):
        index = source
    else:
        index = ping_summary_index(rows=source)
    out: Dict[str, Optional[float]] = {}
    for variant, row in index.items():
        if not isinstance(row, dict):
            continue
        value = row.get("success_rate_percent")
        if value in (None, ""):
            value = row.get("success_rate")
        out[variant] = None if value in (None, "") else _to_float(value)
    return out


def format_success_rate(value: Any, digits: int = 1) -> str:
    """`99.75` → `"99.8 %"`; None/"" → `"-"` (chưa ping lần nào ≠ 0 %)."""
    number = _to_float(value)
    if number is None:
        return "-"
    return ("%%.%df %%" % digits) % number


def uid_variants(*values: object) -> set:
    """Hợp các biến thể UID của nhiều giá trị (tiện tra index 1 lần)."""
    out: set = set()
    for value in values:
        out |= gateway_uid_variants(value)
    return out
