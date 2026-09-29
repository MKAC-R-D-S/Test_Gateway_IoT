"""Chấm LẠI ĐẠT / KHÔNG ĐẠT cho một phiên ĐÃ CHẠY XONG (từ CSV thô trên đĩa).

Vì sao cần module riêng:
  - `core/pipeline.write_reports()` chấm điểm bằng TRẠNG THÁI TRONG RAM
    (`ping_tracker`, `focused_node`, `detector`) nên chỉ chấm được đúng lúc kết
    thúc phiên. Phiên chạy bằng bản tool CŨ (hoặc phiên bị tắt ngang trước khi
    chốt báo cáo) không có `reports/gateway_result.csv`.
  - Yêu cầu nghiệp vụ: tab "Lịch sử phiên" phải xem được ĐẠT/KHÔNG ĐẠT của
    phiên cũ mà không phải chạy lại test 3-4 ngày.

Nguyên tắc BẤT DI BẤT DỊCH: **CSV thô là source of truth (README §2)**.
  - CHỈ ĐỌC `mqtt/mqtt_data_*.csv`, `ping/ping_log_*.csv`,
    `mqtt/mqtt_gateway_log_*.csv` và các file trong `reports/`.
  - CHỈ GHI THÊM/ĐÈ `reports/gateway_result.csv` + 4 cột kết quả trong
    `reports/run_summary.csv`. Không sửa, không xoá bất cứ dữ liệu thô nào —
    nên chấm lại nhiều lần luôn cho cùng một kết quả và không phá dữ liệu gốc.

Khác biệt so với lúc chạy (và lý do):
  - **Tiêu chí "Bắn hụt" (c_late)**: cần bảng bitmap của node trọng tâm, thứ chỉ
    tồn tại trong RAM lúc chạy. Nếu phiên có `reports/focused_node_summary.csv`
    thì đọc lại; nếu KHÔNG có (phiên không bật node trọng tâm, hoặc bản tool cũ)
    thì để `N/A` — thà nói "không áp dụng" còn hơn bịa ra một tỉ lệ.
  - **Thiết bị**: KHÔNG đọc `devices.json` sống. Danh sách gateway lấy từ chính
    dữ liệu của phiên. Vì sao: (a) `devices.json` có thể đã xoá gateway của phiên
    cũ, (b) `meta.json` của bản tool cũ có thể thiếu `ping_enabled`, (c) mọi
    gateway có dữ liệu đều đã xuất hiện trong `ping_summary` / `mqtt_data` /
    `correlation_summary` nên không cần nguồn ngoài. Nhờ vậy chấm lại một phiên
    cũ không phụ thuộc vào cấu hình hôm nay.

Tham số chấm (ngưỡng + tham số tương quan) đọc từ `meta.json` khoá
`scoring_params` do phiên tự chốt lúc bắt đầu (xem
`analytics.gateway_result.scoring_params_snapshot`). Phiên cũ không có khoá này
→ quay về `TestConfig` hiện tại.

Lưu ý về phiên do BẢN TOOL CŨ tạo: `runtime/runner.stop()` bản cũ gọi
`write_reports()` TRƯỚC `logs.flush_all()`, nên báo cáo gốc được tính khi
`mqtt_data_*.csv` còn thiếu vài dòng cuối (đo trên thực tế: thiếu ~6 dòng cuối
phiên). Chấm lại đọc CSV đã đầy đủ → số liệu có thể CAO HƠN một chút so với file
`reports/` gốc. Đây là chủ ý: bản chấm lại đúng hơn bản gốc, và dữ liệu thô vẫn
nguyên vẹn. Bug đó đã được sửa nên phiên chạy từ nay về sau không lệch nữa.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from analytics.correlation import correlate
from analytics.gateway_result import (
    RESULT_FAIL,
    TOTAL_UID,
    GatewayResultEvaluator,
    ScoringParams,
    scoring_params_snapshot,
)
from core.uid import format_gateway_uid
from storage.reader import iter_csv_rows
from storage.schema import GATEWAY_RESULT_COLUMNS, RUN_SUMMARY_COLUMNS

log = logging.getLogger(__name__)

#: Tên file báo cáo mà việc chấm lại cần ĐỌC nếu có.
PING_SUMMARY_FILENAME = "ping_summary.csv"
FOCUSED_SUMMARY_FILENAME = "focused_node_summary.csv"
RUN_SUMMARY_FILENAME = "run_summary.csv"
GATEWAY_RESULT_FILENAME = "gateway_result.csv"


@dataclass
class RescoreResult:
    """Kết quả một lần chấm lại (đủ để GUI báo cho người dùng)."""

    run_dir: Path
    run_id: str = ""
    rows: List[Dict[str, Any]] = field(default_factory=list)
    gateways_scored: int = 0
    gateways_passed: int = 0
    gateways_failed: int = 0
    result: str = ""
    result_reason: str = ""
    elapsed_seconds: float = 0.0
    missing_late_data: bool = False
    params_from_session: bool = False
    warnings: List[str] = field(default_factory=list)
    written: List[Path] = field(default_factory=list)

    def summary_text(self) -> str:
        """Một câu tiếng Việt mô tả kết quả chấm lại."""
        return "%d gateway: %d ĐẠT, %d KHÔNG ĐẠT — kết luận phiên: %s" % (
            self.gateways_scored,
            self.gateways_passed,
            self.gateways_failed,
            self.result or "?",
        )


class RescoreError(RuntimeError):
    """Không chấm lại được (thiếu nguồn dữ liệu thô …)."""


# ---------------------------------------------------------------------------
# Đọc nguồn dữ liệu
# ---------------------------------------------------------------------------
def run_data_sources(run_dir: Path) -> Dict[str, Any]:
    """Kiểm tra phiên có ĐỦ dữ liệu thô để chấm lại không (KHÔNG đọc nội dung).

    Tách khỏi `rescore_run()` để GUI hỏi trước khi chạy — đọc `mqtt_data` của
    phiên 4 ngày tốn ~10 s, không nên "thử rồi mới biết là thiếu".
    """
    run_dir = Path(run_dir)
    mqtt_files = sorted((run_dir / "mqtt").glob("mqtt_data_*.csv"))
    ping_files = sorted((run_dir / "ping").glob("ping_log_*.csv"))
    log_files = sorted((run_dir / "mqtt").glob("mqtt_gateway_log_*.csv"))
    reports = run_dir / "reports"
    return {
        "run_dir": run_dir,
        "mqtt_files": mqtt_files,
        "ping_files": ping_files,
        "log_files": log_files,
        "ping_summary": reports / PING_SUMMARY_FILENAME,
        "focused_summary": reports / FOCUSED_SUMMARY_FILENAME,
        "run_summary": reports / RUN_SUMMARY_FILENAME,
        "has_mqtt": bool(mqtt_files),
        # Ping có thể lấy từ `ping_log_*.csv` (thô) HOẶC `ping_summary.csv` (chốt).
        "has_ping": bool(ping_files) or (reports / PING_SUMMARY_FILENAME).exists(),
    }


def _read_rows(paths: Sequence[Path]) -> List[Dict[str, Any]]:
    """Nối các file CSV cùng loại thành một danh sách dòng."""
    rows: List[Dict[str, Any]] = []
    for path in paths:
        rows.extend(iter_csv_rows(path))
    return rows


def _ping_rows(
    sources: Mapping[str, Any], down_threshold: int = 2
) -> List[Dict[str, Any]]:
    """Dòng tổng hợp ping theo gateway.

    Ưu tiên `reports/ping_summary.csv` vì đó là bản ĐÃ CHỐT của phiên (đúng số
    liệu người dùng đã xem), rẻ hơn nhiều so với dựng lại từ `ping_log_*.csv`.
    Chỉ khi file đó thiếu/rỗng mới tính lại từ log thô.

    `down_threshold` ảnh hưởng trực tiếp số lần / thời gian mất kết nối nên phải
    truyền đúng ngưỡng mà phiên đã dùng (lấy từ ảnh chụp tham số của phiên),
    không được để mặc định.
    """
    summary_path = Path(sources["ping_summary"])
    if summary_path.exists():
        rows = [dict(row) for row in iter_csv_rows(summary_path)]
        if rows:
            return rows
        log.info("Chấm lại: %s rỗng → dựng lại từ ping_log", summary_path)

    ping_files: List[Path] = list(sources["ping_files"])
    if not ping_files:
        return []
    from analytics.ping_stats import summarize_ping_csv

    tracker = summarize_ping_csv(_read_rows(ping_files), max(1, int(down_threshold)))
    return tracker.summary_rows()


def _focused_rows(sources: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Dòng node trọng tâm (để chấm tiêu chí "Bắn hụt"). Thiếu file → []."""
    path = Path(sources["focused_summary"])
    if not path.exists():
        return []
    return [dict(row) for row in iter_csv_rows(path)]


# ---------------------------------------------------------------------------
# Chấm lại
# ---------------------------------------------------------------------------
def _evaluator_config(config: Any, session_params: Mapping[str, Any]) -> Any:
    """Chọn tham số để chấm: ưu tiên ảnh chụp trong `meta.json` của phiên.

    Vì sao ưu tiên ảnh chụp: kết quả của một phiên đã chạy không được đổi chỉ vì
    `config.json` bị sửa sau đó. Ngưỡng nào còn thiếu trong ảnh chụp thì lấy nốt
    từ `config` truyền vào.
    """
    merged: Dict[str, Any] = {}
    if config is not None:
        merged.update(scoring_params_snapshot(config))
    merged.update({k: v for k, v in dict(session_params or {}).items() if v not in (None, "")})
    return ScoringParams(merged)


def rescore_run(
    run_dir: Path,
    config: Any = None,
    *,
    session_params: Optional[Mapping[str, Any]] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> RescoreResult:
    """Chấm lại một phiên và GHI `reports/gateway_result.csv`.

    Tham số:
      `run_dir`        thư mục phiên (`data/runs/<run_id>`).
      `config`         `TestConfig` hiện tại — chỉ dùng cho tham số mà `meta.json`
                       của phiên không có (phiên chạy bằng bản tool cũ).
      `session_params` ảnh chụp tham số của phiên (`RunSession.scoring_params`).
      `progress`       hàm nhận thông báo tiến trình (GUI hiện lên nhãn trạng thái).

    Ném `RescoreError` khi phiên không có dữ liệu MQTT thô (không thể dựng lại
    độ phủ bản tin) — nói rõ lý do thay vì ghi ra file toàn `N/A`.
    """
    started = time.monotonic()
    run_dir = Path(run_dir)
    notify = progress or (lambda _msg: None)
    result = RescoreResult(run_dir=run_dir, run_id=run_dir.name)

    sources = run_data_sources(run_dir)
    if not sources["has_mqtt"]:
        raise RescoreError(
            "Phiên không có mqtt/mqtt_data_*.csv nên không dựng lại được độ phủ "
            "bản tin — không chấm được."
        )
    if not sources["has_ping"]:
        result.warnings.append(
            "Phiên không có dữ liệu ping (ping_log_*.csv và ping_summary.csv đều "
            "thiếu) → các tiêu chí liên quan ping sẽ là N/A."
        )
    result.params_from_session = bool(session_params)
    if not result.params_from_session:
        result.warnings.append(
            "Phiên chạy bằng bản tool cũ nên meta.json không có ảnh chụp tham số "
            "(scoring_params) — phải dùng ngưỡng từ config.json hiện tại, kết quả "
            "có thể khác lần chấm gốc nếu bạn đã đổi ngưỡng."
        )
        result.warnings.append(
            "Bản tool cũ ghi báo cáo TRƯỚC khi chốt CSV nên báo cáo gốc thiếu vài "
            "dòng cuối phiên. Bản chấm lại đọc CSV đã đầy đủ nên số liệu có thể cao "
            "hơn một chút so với reports/ gốc — đây là số ĐÚNG hơn."
        )

    notify("Đang đọc dữ liệu MQTT thô…")
    mqtt_rows = _read_rows(sources["mqtt_files"])
    log_rows = _read_rows(sources["log_files"])
    params = _evaluator_config(config, session_params or {})
    ping_rows = _ping_rows(sources, getattr(params, "ping_down_threshold", 2))

    # Bảng node trọng tâm: thiếu file → `c_late = N/A` (xem docstring module).
    focused_rows = _focused_rows(sources)
    result.missing_late_data = not sources["focused_summary"].exists()
    if result.missing_late_data:
        result.warnings.append(
            "Phiên không có reports/focused_node_summary.csv → tiêu chí "
            "\"Bắn hụt (gửi trễ)\" để N/A (không thể dựng lại bitmap node trọng tâm "
            "từ CSV thô)."
        )

    # Luôn chạy `correlate()`: tiêu chí "Kết nối MQTT" / "Ổn định MQTT" phải phản
    # ánh LOG THÔ của phiên này, không phải file `correlation_summary.csv` (file
    # đó có thể do lần chấm trước ghi, hoặc thiếu với phiên tắt ngang).
    notify("Đang dựng lại tương quan ping ↔ MQTT…")
    ping_csv_rows = _read_rows(sources["ping_files"])
    report = correlate(
        ping_csv_rows,
        mqtt_rows,
        log_rows,
        window_seconds=getattr(params, "correlation_window_seconds", 5),
        silent_threshold=getattr(params, "silent_window_threshold", 2),
    )

    notify("Đang chấm điểm từng tiêu chí…")
    evaluator = GatewayResultEvaluator(params)
    rows = evaluator.evaluate(
        ping_rows=ping_rows,
        correlation_gateways=report.gateways,
        mqtt_rows=mqtt_rows,
        focused_rows=focused_rows,
        # Không dùng `devices.json` sống — xem docstring module.
        devices=(),
    )
    rows = _drop_duplicate_overall(rows)
    result.rows = rows

    gateway_rows = [row for row in rows if row.get("gateway_uid") != TOTAL_UID]
    overall = next((row for row in rows if row.get("gateway_uid") == TOTAL_UID), {})
    result.gateways_scored = len(gateway_rows)
    result.gateways_failed = sum(
        1 for row in gateway_rows if row.get("result") == RESULT_FAIL
    )
    result.gateways_passed = len(gateway_rows) - result.gateways_failed
    result.result = str(overall.get("result", "") or "")
    result.result_reason = str(overall.get("result_reason", "") or "")

    result.written = write_rescore_reports(run_dir, rows, result)
    result.elapsed_seconds = round(time.monotonic() - started, 3)
    return result


def _drop_duplicate_overall(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Giữ đúng MỘT dòng TỔNG (nếu bản tool cũ lỡ sinh nhiều dòng).

    `evaluate()` đã trả đúng một dòng TỔNG; hàm này chỉ để phòng dữ liệu lạ và
    bảo đảm bất biến "TỔNG là dòng CUỐI" mà tab Lịch sử phiên dựa vào.
    """
    gateways = [row for row in rows if row.get("gateway_uid") != TOTAL_UID]
    overall = [row for row in rows if row.get("gateway_uid") == TOTAL_UID]
    if len(overall) <= 1:
        return rows
    return gateways + [overall[-1]]


def write_rescore_reports(
    run_dir: Path, rows: Sequence[Mapping[str, Any]], result: Optional[RescoreResult] = None
) -> List[Path]:
    """Ghi `gateway_result.csv` + làm mới 4 cột kết quả trong `run_summary.csv`.

    Chỉ ghi vào `reports/`; dữ liệu thô trong `mqtt/` và `ping/` không bị chạm.
    `run_summary.csv` phải được cập nhật cùng lúc vì bảng "Tổng kết" đọc 4 cột
    đó — nếu để nguyên, một trang sẽ hiện kết quả mới còn trang kia kết quả cũ.
    """
    written: List[Path] = []
    from storage.csv_writer import LogManager

    # `LogManager` chỉ `mkdir` các thư mục con và tạo đối tượng `CsvSink` (không
    # start thread nào ở `__init__`) nên dùng nó để ghi là an toàn — và nhờ vậy
    # file ghi ra có ĐÚNG định dạng (utf-8-sig, extrasaction=ignore, fsync) như
    # mọi báo cáo khác.
    manager = LogManager(Path(run_dir))
    written.append(
        manager.write_single_csv(GATEWAY_RESULT_FILENAME, GATEWAY_RESULT_COLUMNS, rows)
    )
    written.extend(_refresh_run_summary(Path(run_dir), rows, manager))
    if result is not None:
        result.written = written
    return written


def _refresh_run_summary(
    run_dir: Path, rows: Sequence[Mapping[str, Any]], manager: Any
) -> List[Path]:
    """Cập nhật 4 cột kết quả trong `reports/run_summary.csv` (giữ mọi cột khác)."""
    path = run_dir / "reports" / RUN_SUMMARY_FILENAME
    if not path.exists():
        return []
    existing = [dict(row) for row in iter_csv_rows(path)]
    if not existing:
        return []
    overall = next((row for row in rows if row.get("gateway_uid") == TOTAL_UID), {})
    gateway_rows = [row for row in rows if row.get("gateway_uid") != TOTAL_UID]
    failed = sum(1 for row in gateway_rows if row.get("result") == RESULT_FAIL)

    existing[0].update(
        {
            "gateways_passed": len(gateway_rows) - failed,
            "gateways_failed": failed,
            "result": overall.get("result", ""),
            "result_reason": overall.get("result_reason", ""),
        }
    )
    # Giữ đúng thứ tự cột chuẩn; chỉ thêm cột lạ nếu `run_summary.csv` của phiên
    # cũ có cột ngoài schema (tránh làm mất dữ liệu khi ghi lại).
    columns: List[str] = list(RUN_SUMMARY_COLUMNS)
    for row in existing:
        for key in row:
            if key not in columns:
                columns.append(key)
    return [manager.write_single_csv(RUN_SUMMARY_FILENAME, columns, existing)]


# ---------------------------------------------------------------------------
# Truy vấn kết quả đã có trên đĩa (GUI dùng để hiện tóm tắt)
# ---------------------------------------------------------------------------
def read_result_rows(run_dir: Path) -> List[Dict[str, Any]]:
    """Đọc lại `reports/gateway_result.csv` của phiên (rỗng nếu chưa có)."""
    path = Path(run_dir) / "reports" / GATEWAY_RESULT_FILENAME
    if not path.exists():
        return []
    return [dict(row) for row in iter_csv_rows(path)]


def result_overall(run_dir: Path) -> Dict[str, Any]:
    """Dòng TỔNG của `gateway_result.csv` (rỗng nếu phiên chưa được chấm)."""
    for row in read_result_rows(run_dir):
        if row.get("gateway_uid") == TOTAL_UID:
            return row
    return {}


def rescore_device_names(run_dir: Path) -> List[str]:
    """Gateway UID của phiên theo thứ tự trong kết quả đã chấm (tiện cho test/GUI)."""
    return [
        format_gateway_uid(row.get("gateway_uid", ""))
        for row in read_result_rows(run_dir)
        if row.get("gateway_uid") != TOTAL_UID
    ]

