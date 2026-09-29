"""Thread-safe CSV writer cho test dài ngày (3-4 ngày).

Đặc điểm bắt buộc:
  - Một writer thread duy nhất ghi ra disk; thread khác chỉ `enqueue()`.
  - Flush định kỳ (mặc định 2 s) hoặc khi đủ N record — KHÔNG flush từng packet.
  - Xoay file theo ngày (mặc định) hoặc theo giờ.
  - Áp lực ngược (back-pressure) khi queue đầy: bỏ record và ĐẾM số bị bỏ
    (thay vì để RAM phình vô hạn → crash sau vài ngày).
  - `durable=True` cho file ít ghi nhưng quan trọng (downtime, summary):
    ghi + flush + fsync ngay, không xoay file.
  - Không bao giờ để lỗi I/O làm chết thread: log + tiếp tục.
"""

from __future__ import annotations

import csv
import logging
import os
import queue
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

log = logging.getLogger(__name__)

# Giới hạn field CSV để tránh 1 record khổng lồ (Excel/Python đều khó chịu).
MAX_FIELD_CHARS = 20000


class CsvSink:
    """Một (hoặc nhiều) file CSV cùng schema, xoay theo thời gian."""

    def __init__(
        self,
        base_name: str,
        columns: Sequence[str],
        directory: Path,
        rotation: str = "daily",
        flush_interval: float = 2.0,
        flush_max_records: int = 200,
        durable: bool = False,
        max_queue_size: int = 200000,
        epoch: Optional[datetime] = None,
        use_date_in_name: bool = True,
    ) -> None:
        self.base_name = base_name
        self.columns = list(columns)
        self.directory = Path(directory)
        self.rotation = rotation if rotation in ("daily", "hourly") else "daily"
        self.flush_interval = max(0.1, float(flush_interval))
        self.flush_max_records = max(1, int(flush_max_records))
        self.durable = durable
        self.use_date_in_name = use_date_in_name

        self._epoch = epoch or datetime.now()
        self._queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(
            maxsize=max(1000, int(max_queue_size))
        )
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self._handle = None
        self._writer: Optional[csv.DictWriter] = None
        self._current_key: Optional[str] = None
        self._pending = 0
        self._last_flush = time.monotonic()
        self._header_ensured = False

        # Thống kê
        self.rows_written = 0
        self.rows_dropped = 0
        self.files_written: List[Path] = []
        self.errors = 0
        self.last_error = ""
        self.current_path: Optional[Path] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ API
    def start(self) -> None:
        if self._thread is not None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="csv-%s" % self.base_name, daemon=True
        )
        self._thread.start()

    def enqueue(self, row: Dict[str, Any]) -> bool:
        """Đưa 1 dòng vào queue. False nếu bị bỏ do queue đầy."""
        if self._stop.is_set():
            return False
        try:
            self._queue.put_nowait(row)
            return True
        except queue.Full:
            with self._lock:
                self.rows_dropped += 1
            return False

    def mark_epoch(self, epoch: datetime) -> None:
        self._epoch = epoch

    def stop(self, timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=timeout)
        self._thread = None
        self._close_file()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "rows_written": self.rows_written,
                "rows_dropped": self.rows_dropped,
                "errors": self.errors,
                "last_error": self.last_error,
                "current_file": str(self.current_path) if self.current_path else "",
                "queue_size": self._queue.qsize(),
            }

    # ------------------------------------------------------------- nội bộ
    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=self.flush_interval)
            except queue.Empty:
                with self._lock:
                    self._flush_locked(force=True)
                continue

            if item is None:
                break

            try:
                with self._lock:
                    self._write_locked(item)
                    self._pending += 1
                    if self._pending >= self.flush_max_records:
                        self._flush_locked(force=True)
            except Exception:  # noqa: BLE001 - không được để writer chết
                with self._lock:
                    self.errors += 1
                    self.last_error = traceback.format_exc(limit=3)
                log.error("CSV write error in %s:\n%s", self.base_name, self.last_error)

        # Drain phần còn lại rồi đóng file
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            try:
                with self._lock:
                    self._write_locked(item)
            except Exception:  # noqa: BLE001
                with self._lock:
                    self.errors += 1
        with self._lock:
            self._flush_locked(force=True)
        self._close_file()

    def _key_for(self, reference: Optional[datetime] = None) -> str:
        reference = reference or datetime.now()
        if self.rotation == "hourly":
            return reference.strftime("%Y%m%d_%H")
        return reference.strftime("%Y%m%d")

    def _path_for(self, key: str) -> Path:
        if self.use_date_in_name:
            return self.directory / ("%s_%s.csv" % (self.base_name, key))
        return self.directory / ("%s.csv" % self.base_name)

    def _write_locked(self, row: Dict[str, Any]) -> None:
        key = self._key_for()
        if self._writer is None or key != self._current_key:
            self._open_file(key)
        assert self._writer is not None
        self._writer.writerow(self._sanitize(row))
        self.rows_written += 1

    def _sanitize(self, row: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for column in self.columns:
            value = row.get(column, "")
            if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
                value = value[:MAX_FIELD_CHARS] + "...[truncated]"
            out[column] = "" if value is None else value
        return out

    def _open_file(self, key: str) -> None:
        self._flush_locked()
        self._close_file()
        path = self._path_for(key)
        exists = path.exists() and path.stat().st_size > 0
        # newline="" theo chuẩn csv module; encoding utf-8-sig để Excel mở đúng.
        new_file = not exists
        handle = open(path, "a", newline="", encoding="utf-8-sig")
        if not exists and path.stat().st_size == 0:
            handle.write("\ufeff") if False else None
        self._handle = handle
        self._writer = csv.DictWriter(
            handle, fieldnames=self.columns, extrasaction="ignore", restval=""
        )
        if not exists:
            self._writer.writeheader()
            if self.durable:
                handle.flush()
                os.fsync(handle.fileno())
        if new_file and path not in self.files_written:
            self.files_written.append(path)
        self._current_key = key
        self.current_path = path
        self._pending = 0
        self._last_flush = time.monotonic()
        self._header_ensured = True

    def _flush_locked(self, force: bool = False) -> None:
        if self._handle is None:
            return
        if not force and self._pending == 0:
            return
        if not force and (time.monotonic() - self._last_flush) < self.flush_interval:
            return
        try:
            self._handle.flush()
            if self.durable:
                os.fsync(self._handle.fileno())
        except (OSError, ValueError) as exc:
            self.errors += 1
            self.last_error = str(exc)
            log.error("CSV flush error in %s: %s", self.base_name, exc)
        self._pending = 0
        self._last_flush = time.monotonic()

    def _close_file(self) -> None:
        if self._handle is not None:
            try:
                self._handle.flush()
                if self.durable:
                    os.fsync(self._handle.fileno())
                self._handle.close()
            except (OSError, ValueError):
                pass
        self._handle = None
        self._writer = None
        self._current_key = None


class LogManager:
    """Quản lý toàn bộ sink của một phiên chạy + ghi summary."""

    DATA_DIRNAME = "mqtt"
    PING_DIRNAME = "ping"
    REPORT_DIRNAME = "reports"

    def __init__(
        self,
        run_dir: Path,
        rotation: str = "daily",
        flush_interval: float = 2.0,
        flush_max_records: int = 200,
        max_queue_size: int = 200000,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.mqtt_dir = self.run_dir / self.DATA_DIRNAME
        self.ping_dir = self.run_dir / self.PING_DIRNAME
        self.report_dir = self.run_dir / self.REPORT_DIRNAME
        for directory in (self.mqtt_dir, self.ping_dir, self.report_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self.epoch = datetime.now()
        kwargs = dict(
            rotation=rotation,
            flush_interval=flush_interval,
            flush_max_records=flush_max_records,
            max_queue_size=max_queue_size,
            epoch=self.epoch,
        )

        from .schema import (
            ALERT_COLUMNS,
            GATEWAY_DOWNTIME_COLUMNS,
            MQTT_ANOMALY_COLUMNS,
            MQTT_DATA_COLUMNS,
            MQTT_ERROR_COLUMNS,
            MQTT_GATEWAY_LOG_COLUMNS,
            MQTT_MISSING_COLUMNS,
            PING_LOG_COLUMNS,
        )

        self.data = CsvSink("mqtt_data", MQTT_DATA_COLUMNS, self.mqtt_dir, **kwargs)
        self.missing = CsvSink("mqtt_missing", MQTT_MISSING_COLUMNS, self.mqtt_dir, **kwargs)
        self.anomalies = CsvSink(
            "mqtt_anomalies", MQTT_ANOMALY_COLUMNS, self.mqtt_dir, **kwargs
        )
        self.errors = CsvSink("mqtt_errors", MQTT_ERROR_COLUMNS, self.mqtt_dir, **kwargs)
        self.gateway_log = CsvSink(
            "mqtt_gateway_log", MQTT_GATEWAY_LOG_COLUMNS, self.mqtt_dir, **kwargs
        )
        self.alerts = CsvSink("alerts", ALERT_COLUMNS, self.report_dir, **kwargs)
        self.ping = CsvSink("ping_log", PING_LOG_COLUMNS, self.ping_dir, **kwargs)
        self.downtime = CsvSink(
            "gateway_downtime",
            GATEWAY_DOWNTIME_COLUMNS,
            self.report_dir,
            rotation="daily",
            flush_interval=flush_interval,
            flush_max_records=1,
            durable=True,
            max_queue_size=50000,
            epoch=self.epoch,
            use_date_in_name=False,
        )

        self._sinks: List[CsvSink] = [
            self.data,
            self.missing,
            self.anomalies,
            self.errors,
            self.gateway_log,
            self.alerts,
            self.ping,
            self.downtime,
        ]

    def start(self) -> None:
        for sink in self._sinks:
            sink.start()

    def stop(self) -> None:
        for sink in self._sinks:
            sink.stop()

    def all_stats(self) -> Dict[str, Dict[str, Any]]:
        return {sink.base_name: sink.stats() for sink in self._sinks}

    def flush_all(self) -> None:
        for sink in self._sinks:
            sink.flush()

    def write_single_csv(
        self, filename: str, columns: Iterable[str], rows: Iterable[Dict[str, Any]]
    ) -> Path:
        path = self.report_dir / filename
        columns = list(columns)
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=columns, extrasaction="ignore", restval=""
            )
            writer.writeheader()
            for row in rows:
                writer.writerow({c: _small(row.get(c, "")) for c in columns})
            handle.flush()
            os.fsync(handle.fileno())
        return path


def _small(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS] + "...[truncated]"
    return "" if value is None else value
