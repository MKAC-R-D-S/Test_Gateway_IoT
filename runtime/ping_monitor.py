"""Ping monitor chạy nền theo lịch.

Thiết kế cho test 3-4 ngày:
  - Một thread cho mỗi gateway, nhưng giới hạn tổng số thread đồng thời
    (`max_concurrent`) — tránh mở 200 tiến trình `ping` cùng lúc.
  - Dùng `ping` của hệ điều hành (`ping -n 1 -w <ms>` trên Windows,
    `ping -c 1 -W <s>` trên Linux) vì đơn giản, ổn định và không cần quyền admin.
  - Mọi lỗi (timeout, host unreachable, DNS fail) đều được ghi nhận — KHÔNG
    ném exception ra ngoài thread.
  - Ghi **từng lần ping** xuống CSV kể cả khi fail (đây là dữ liệu gốc để
    đối chiếu sau này), việc tổng hợp downtime do `PingTracker` làm.
"""

from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from core.models import PingRecord

log = logging.getLogger(__name__)

IS_WINDOWS = platform.system().lower().startswith("win")

# `Reply from 192.168.1.1: bytes=32 time<1ms TTL=64` / `time=3ms`
_WIN_TIME_RE = re.compile(r"time[=<]\s*(\d+(?:[.,]\d+)?)\s*ms", re.IGNORECASE)
_NIX_TIME_RE = re.compile(r"time[=<]?\s*(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)
_UNREACHABLE_MARKERS = (
    "destination host unreachable",
    "host unreachable",
    "network is unreachable",
    "request timed out",
    "100% packet loss",
    "100% loss",
    "general failure",
    "could not find host",
    "name or service not known",
    "transmit failed",
    "bad address",
)


@dataclass
class PingTarget:
    gateway_uid: str
    gateway_name: str
    ip_address: str
    enabled: bool = True

    def label(self) -> str:
        return "%s (%s)" % (self.gateway_name or self.gateway_uid, self.ip_address)


@dataclass
class PingOutcome:
    success: bool
    latency_ms: Optional[float] = None
    timeout: bool = False
    error: str = ""
    raw_output: str = ""


class PingMonitor:
    """Chạy ping định kỳ cho danh sách gateway, gọi callback mỗi kết quả."""

    def __init__(
        self,
        targets: List[PingTarget],
        interval_seconds: int = 10,
        timeout_ms: int = 2000,
        count: int = 1,
        max_concurrent: int = 16,
        on_result: Optional[Callable[[PingRecord], None]] = None,
    ) -> None:
        self.targets = {t.gateway_uid.upper(): t for t in targets if t.enabled and t.ip_address}
        self.interval = max(1, int(interval_seconds))
        self.timeout_ms = max(200, int(timeout_ms))
        self.count = max(1, int(count))
        self.max_concurrent = max(1, int(max_concurrent))
        self.on_result = on_result

        self._threads: List[threading.Thread] = []
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._semaphore = threading.Semaphore(self.max_concurrent)
        self._lock = threading.Lock()
        self.total_pings = 0
        self.failed_pings = 0
        self.last_error = ""
        self.available = shutil.which("ping") is not None

    # ------------------------------------------------------------------ API
    def start(self) -> None:
        if self._threads:
            return
        if not self.available:
            self.last_error = "Không tìm thấy lệnh 'ping' trên hệ thống"
            log.error(self.last_error)
            return
        if not self.targets:
            log.warning("Không có gateway nào bật ping (thiếu/không có IP)")
            return
        self._stop.clear()
        self._paused.clear()
        for target in self.targets.values():
            thread = threading.Thread(
                target=self._run_target,
                name="ping-%s" % target.gateway_uid,
                args=(target,),
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()
        log.info("PingMonitor khởi động: %d gateway, chu kỳ %ds",
                 len(self._threads), self.interval)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        # Đánh thức mọi thread đang chờ lịch để chúng thoát ngay, không phải đợi
        # hết chu kỳ ping (mặc định 10 s) trong lúc GUI đứng chờ.
        self._paused.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()

    def pause(self) -> None:
        """Tạm dừng ping: thread còn sống nhưng KHÔNG chạy ICMP nào nữa.

        Cố ý không phát `PingRecord` fail khi tạm dừng: nếu phát thì một lần tạm
        dừng dài hơn `ping_down_threshold × ping_interval_seconds` sẽ được
        `PingTracker` ghi thành một cửa sổ DOWNTIME giả trong báo cáo.
        """
        if self._paused.is_set():
            return
        self._paused.set()
        log.info("PingMonitor: TẠM DỪNG — không chạy ICMP cho %d gateway",
                 len(self._threads) or len(self.targets))

    def resume(self) -> None:
        """Chạy lại ICMP. Thread không tự "dồn" các lần ping đã lỡ → nhịp bình thường."""
        if not self._paused.is_set():
            return
        self._paused.clear()
        log.info("PingMonitor: TIẾP TỤC — chạy lại ICMP cho %d gateway",
                 len(self._threads) or len(self.targets))

    def is_running(self) -> bool:
        return bool(self._threads) and not self._stop.is_set()

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def update_targets(self, targets: List[PingTarget]) -> None:
        """Nạp lại danh sách mục tiêu (đổi IP/thêm gateway) — cần stop/start."""
        new_map = {t.gateway_uid.upper(): t for t in targets if t.enabled and t.ip_address}
        with self._lock:
            changed = set(new_map) != set(self.targets)
            self.targets = new_map
        return changed

    def stats(self) -> Dict[str, Any]:
        return {
            "running": self.is_running(),
            "paused": self.is_paused(),
            "available": self.available,
            "targets": len(self.targets),
            "interval_seconds": self.interval,
            "total_pings": self.total_pings,
            "failed_pings": self.failed_pings,
            "last_error": self.last_error,
        }

    # --------------------------------------------------------------- nội bộ
    def _run_target(self, target: PingTarget) -> None:
        # Lệch pha nhẹ giữa các gateway để không dồn ICMP cùng lúc.
        jitter = (hash(target.gateway_uid) % 1000) / 1000.0
        if self._sleep(self.interval * jitter):
            return
        while not self._stop.is_set():
            if self._paused.is_set():
                # Tạm dừng: ngủ ngắn rồi kiểm lại. Dùng `_stop.wait` để lệnh stop()
                # vẫn đánh thức được ngay lập tức.
                if self._stop.wait(0.25):
                    return
                continue
            started = time.monotonic()
            outcome = self._ping_once(target.ip_address)
            # Khoảng tạm dừng xen vào giữa lúc bắt đầu ping và lúc có kết quả:
            # bỏ kết quả đó, KHÔNG ghi counter/CSV.
            if self._paused.is_set() or self._stop.is_set():
                continue
            record = PingRecord(
                timestamp=datetime.now().astimezone().isoformat(timespec="milliseconds"),
                gateway_uid=target.gateway_uid.upper(),
                gateway_name=target.gateway_name,
                ip_address=target.ip_address,
                ping_success=outcome.success,
                latency_ms=outcome.latency_ms,
                timeout=outcome.timeout,
                error=outcome.error,
            )
            with self._lock:
                self.total_pings += 1
                if not outcome.success:
                    self.failed_pings += 1
                    self.last_error = outcome.error
            self._dispatch(record)
            elapsed = time.monotonic() - started
            remaining = max(0.0, self.interval - elapsed)
            if self._sleep(remaining):
                break

    def _sleep(self, seconds: float) -> bool:
        """Ngủ `seconds`, thoát ngay khi bị stop HOẶC bị tạm dừng.

        Nhờ thoát khi tạm dừng, thời điểm kết thúc tạm dừng không phải chờ hết
        chu kỳ ping mới có lần ping kế tiếp.
        """
        if seconds <= 0:
            return self._stop.is_set()
        deadline = time.monotonic() + seconds
        while not self._stop.is_set() and not self._paused.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._stop.wait(min(0.25, remaining)):
                return True
        return self._stop.is_set()

    def _dispatch(self, record: PingRecord) -> None:
        handler = self.on_result
        if handler is None:
            return
        try:
            handler(record)
        except Exception:  # noqa: BLE001 - callback không được làm chết monitor
            log.debug("on_result lỗi", exc_info=True)

    def _ping_once(self, ip_address: str) -> PingOutcome:
        """Ping 1 lần. Không bao giờ raise."""
        acquired = False
        try:
            # Chờ slot (nhưng vẫn tôn trọng stop)
            while not self._stop.is_set():
                if self._semaphore.acquire(timeout=0.5):
                    acquired = True
                    break
            if not acquired:
                return PingOutcome(False, timeout=True, error="STOPPED")

            timeout_arg = str(self.timeout_ms)
            if IS_WINDOWS:
                args = ["ping", "-n", str(self.count), "-w", timeout_arg, ip_address]
            else:
                seconds = str(max(1, int(round(self.timeout_ms / 1000.0))))
                args = ["ping", "-c", str(self.count), "-W", seconds, ip_address]

            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=(self.timeout_ms / 1000.0) * self.count + 3.0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0,
            )
            output = ((proc.stdout or "") + (proc.stderr or "")).strip()
            return self._interpret(proc.returncode, output)
        except subprocess.TimeoutExpired:
            return PingOutcome(False, timeout=True, error="PING_TIMEOUT")
        except FileNotFoundError:
            self.available = False
            return PingOutcome(False, error="PING_NOT_FOUND")
        except OSError as exc:
            return PingOutcome(False, error="OS_ERROR: %s" % exc)
        except Exception as exc:  # noqa: BLE001 - tuyệt đối không để thread chết
            return PingOutcome(False, error="%s: %s" % (type(exc).__name__, exc))
        finally:
            if acquired:
                try:
                    self._semaphore.release()
                except ValueError:
                    pass

    def _interpret(self, returncode: int, output: str) -> PingOutcome:
        lowered = output.lower()
        unreachable = next((m for m in _UNREACHABLE_MARKERS if m in lowered), "")
        if unreachable:
            timeout = "timed out" in unreachable or "packet loss" in unreachable
            return PingOutcome(False, timeout=timeout, error=unreachable.upper().replace(" ", "_"),
                               raw_output=output)
        if returncode != 0:
            return PingOutcome(False, timeout=True, error="PING_FAILED_RC_%d" % returncode,
                               raw_output=output)

        latency: Optional[float] = None
        for pattern in (_WIN_TIME_RE, _NIX_TIME_RE):
            match = pattern.search(output)
            if match:
                try:
                    latency = float(match.group(1).replace(",", "."))
                except ValueError:
                    latency = None
                break
        if latency is None and "time<1ms" in lowered:
            latency = 0.5
        return PingOutcome(True, latency_ms=latency, raw_output=output)


def targets_from_devices(devices: List[Any]) -> List[PingTarget]:
    """Chuyển `GatewayDevice` (core.config) → `PingTarget`."""
    out: List[PingTarget] = []
    for device in devices:
        if not getattr(device, "enabled", True):
            continue
        if not getattr(device, "ping_enabled", True):
            continue
        ip_address = (getattr(device, "eth_ip", "") or "").strip()
        if not ip_address:
            continue
        out.append(
            PingTarget(
                gateway_uid=str(getattr(device, "uid", "")).strip().upper(),
                gateway_name=str(getattr(device, "display_name", lambda: "")()),
                ip_address=ip_address,
                enabled=True,
            )
        )
    return out
