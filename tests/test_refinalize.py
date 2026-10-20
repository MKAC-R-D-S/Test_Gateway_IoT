"""Kiểm thử `analytics.refinalize` — CHỐT LẠI báo cáo cho phiên bị tắt đột ngột.

Vì sao phải khoá bằng test: nút "🧾 Chốt lại báo cáo" ghi LẠI cả bộ `reports/`
của một phiên đã chạy (khác "Chấm lại kết quả" chỉ ghi `gateway_result.csv`) và
cập nhật `meta.json`. Sai ở đây nghĩa là dữ liệu lịch sử của khách bị bịa. Các
bất biến phải được khoá:

  * **Bất biến quan trọng nhất: dữ liệu thô không bị chạm.** Chốt lại chỉ ĐỌC
    `mqtt/` và `ping/`, chỉ GHI vào `reports/` + `meta.json` — nội dung và
    kích thước của mọi file CSV thô phải y nguyên sau khi chạy.
  * **Chỉ một dòng TỔNG, và luôn là dòng CUỐI** của `gateway_result.csv` và
    `focused_node_summary.csv` — tab Lịch sử dựa vào bất biến này để tách "dòng
    kết luận" khỏi "dòng gateway".
  * **Thiếu nguồn thì để TRỐNG, không bịa số 0.** Bộ đếm chỉ sống trong RAM lúc
    chạy (`parse_errors`, `queue_dropped`, `reconnects`) không được ghi `0` —
    `0` là một khẳng định "phiên này không có lỗi parse", hoàn toàn khác "không
    biết". Việc không dựng lại được phải được NÓI RA trong `warnings`.
  * **Báo cáo dựng lại KHÔNG đầy đủ hơn bản cũ thì giữ bản cũ**
    (`alerts_summary.csv`, `gateway_downtime.csv`).
  * **Không có `mqtt_data_*.csv` → `RefinalizeError`**, không ghi ra file rỗng.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from analytics.refinalize import (
    RefinalizeError,
    has_raw_data,
    raw_sources,
    refinalize_run,
)
from analytics.gateway_result import (
    TOTAL_UID,
    ScoringParams,
    scoring_params_snapshot,
)
from storage.reader import iter_csv_rows
from storage.schema import (
    FOCUSED_NODE_SUMMARY_COLUMNS,
    GATEWAY_RESULT_COLUMNS,
    MQTT_DATA_COLUMNS,
    MQTT_GATEWAY_LOG_COLUMNS,
    NODE_SUMMARY_COLUMNS,
    PING_LOG_COLUMNS,
    RUN_SUMMARY_COLUMNS,
)
from storage.session import RunSession, refinalize_session

GW1 = "04F9199E139C"
GW2 = "B806199E139C"
NODE_A = "1122334455667788"
NODE_B = "AABBCCDDEEFF0011"


# --------------------------------------------------------------------------
# Trợ giúp: dựng thư mục phiên + hàm ghi CSV
# --------------------------------------------------------------------------
def write_csv(path: Path, columns, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(columns), extrasaction="ignore", restval=""
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def make_writer(run_dir: Path):
    """Hàm ghi CSV tối thiểu, cùng chữ ký `LogManager.write_single_csv`."""
    written = []

    def write_single_csv(filename, columns, rows):
        path = run_dir / "reports" / filename
        write_csv(path, columns, rows)
        written.append(path)
        return path

    write_single_csv.written = written
    return write_single_csv


def mqtt_row(
    gateways,
    node=NODE_A,
    packet_type="DATA",
    seq=1,
    timestamp="2026-01-01T12:00:00+07:00",
    seq_status="NORMAL",
):
    row = {col: "" for col in MQTT_DATA_COLUMNS}
    row.update({
        "packet_type": packet_type,
        "node_uid": node,
        "seq": str(seq),
        "gateways": "|".join(gateways),
        "gateways_heard": str(len(gateways)),
        "gateway_uid": gateways[0] if gateways else "",
        "received_at": timestamp,
        "gateway_timestamp": timestamp,
        "seq_status": seq_status,
    })
    return row


def ping_log_row(uid, success=True, index=0) -> dict:
    row = {col: "" for col in PING_LOG_COLUMNS}
    row.update({
        "gateway_uid": uid,
        "ip_address": "10.0.0.1",
        "timestamp": "2026-01-01T12:%02d:00+07:00" % (index % 60),
        "ping_success": "1" if success else "0",
        "latency_ms": "1.5" if success else "",
        "timeout": "0" if success else "1",
    })
    return row


def log_row(uid, index=0) -> dict:
    row = {col: "" for col in MQTT_GATEWAY_LOG_COLUMNS}
    row.update({
        "gateway_uid": uid,
        "received_at": "2026-01-01T12:00:%02d+07:00" % (index % 60),
        "gateway_timestamp": "2026-01-01T12:00:%02d+07:00" % (index % 60),
        "status": "CONNECTED",
    })
    return row


def build_crashed_run(
    root: Path,
    gateways=(GW1, GW2),
    packets=10,
    missing_at=(),
    with_ping=True,
    with_log=True,
) -> Path:
    """Dựng một phiên BỊ TẮT NGANG: đủ CSV thô nhưng `reports/` rỗng.

    `missing_at` = các `seq` cố tình bỏ qua (để sinh `missing` trong node_summary).
    """
    run_dir = root / "20260101_120000_AAAABBBBCCCC"
    (run_dir / "mqtt").mkdir(parents=True, exist_ok=True)
    (run_dir / "ping").mkdir(parents=True, exist_ok=True)
    (run_dir / "reports").mkdir(parents=True, exist_ok=True)

    rows = []
    for index in range(1, packets + 1):
        if index in missing_at:
            continue
        status = "MISSING" if (index - 1) in missing_at else "NORMAL"
        rows.append(mqtt_row(
            list(gateways),
            node=NODE_A,
            seq=index,
            timestamp="2026-01-01T12:%02d:00+07:00" % index,
            seq_status=status,
        ))
    write_csv(run_dir / "mqtt" / "mqtt_data_20260101.csv", MQTT_DATA_COLUMNS, rows)
    if with_ping:
        # Mốc cuối của phiên lấy từ bằng chứng MUỘN NHẤT trên đĩa, nên dữ liệu
        # ping phải kéo dài SAU bản tin MQTT cuối cùng — nếu không thì `ended_at`
        # chỉ bằng mốc bản tin cuối và không kiểm chứng được gì.
        ping_rows = [
            ping_log_row(uid, index=index)
            for uid in gateways for index in range(90)
        ]
        write_csv(run_dir / "ping" / "ping_log_20260101.csv",
                  PING_LOG_COLUMNS, ping_rows)
    if with_log:
        write_csv(
            run_dir / "mqtt" / "mqtt_gateway_log_20260101.csv",
            MQTT_GATEWAY_LOG_COLUMNS,
            [log_row(uid, index=index) for uid in gateways for index in range(3)],
        )
    return run_dir


def raw_fingerprint(run_dir: Path):
    """Ảnh chụp (đường dẫn tương đối → số byte + hash nội dung) của dữ liệu thô."""
    import hashlib

    items = {}
    for folder in ("mqtt", "ping"):
        for path in sorted((run_dir / folder).glob("*.csv")):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            items[path.relative_to(run_dir).as_posix()] = digest
    return items


def run_refinalize(run_dir, *, focus_node="", devices=(), **kwargs):
    writer = make_writer(run_dir)
    params = kwargs.pop("params", None) or ScoringParams({})
    result = refinalize_run(
        run_dir,
        write_single_csv=writer,
        devices=list(devices),
        focus_node=focus_node,
        params=params,
        now_stamp="2026-01-01T13:00:00+07:00",
        **kwargs,
    )
    return result, writer.written


def read_rows(path: Path):
    return [dict(row) for row in iter_csv_rows(path)]


# --------------------------------------------------------------------------
# Nguồn dữ liệu thô
# --------------------------------------------------------------------------
def test_raw_sources_lists_raw_files_only(tmp_path):
    run_dir = build_crashed_run(tmp_path)

    sources = raw_sources(run_dir)

    assert len(sources["mqtt_files"]) == 1
    assert len(sources["ping_files"]) == 1
    assert len(sources["log_files"]) == 1
    assert has_raw_data(run_dir) is True


def test_has_raw_data_false_without_mqtt(tmp_path):
    run_dir = tmp_path / "20260101_120000_EMPTYRUN"
    (run_dir / "ping").mkdir(parents=True)
    assert has_raw_data(run_dir) is False


def test_missing_mqtt_raises_refinalize_error(tmp_path):
    run_dir = tmp_path / "20260101_120000_EMPTYRUN"
    run_dir.mkdir(parents=True)
    writer = make_writer(run_dir)

    with pytest.raises(RefinalizeError):
        refinalize_run(run_dir, write_single_csv=writer)

    # Không được ghi file rỗng nào ra để người dùng tưởng phiên đã chốt.
    assert not writer.written


# --------------------------------------------------------------------------
# Bất biến số 1: dữ liệu thô không bị chạm
# --------------------------------------------------------------------------
def test_raw_data_is_never_touched(tmp_path):
    run_dir = build_crashed_run(tmp_path, missing_at=(4, 5))
    before = raw_fingerprint(run_dir)

    run_refinalize(run_dir)

    assert raw_fingerprint(run_dir) == before


def test_written_files_all_live_in_reports(tmp_path):
    run_dir = build_crashed_run(tmp_path)

    result, written = run_refinalize(run_dir)

    assert written
    assert all(path.parent == run_dir / "reports" for path in written)
    assert all(path.exists() for path in result.written)


# --------------------------------------------------------------------------
# Nội dung báo cáo dựng lại
# --------------------------------------------------------------------------
def test_rebuilds_full_report_set(tmp_path):
    run_dir = build_crashed_run(tmp_path)

    result, _written = run_refinalize(run_dir)

    names = {path.name for path in result.written}
    assert {
        "node_summary.csv",
        "ping_summary.csv",
        "correlation_summary.csv",
        "mqtt_silence.csv",
        "gateway_result.csv",
        "run_summary.csv",
    } <= names
    # Không theo dõi node trọng tâm → KHÔNG tạo file focused_node_* rỗng.
    assert not any(name.startswith("focused_node_") for name in names)


def test_node_summary_rebuilds_missing_from_raw_csv(tmp_path):
    # Bỏ seq 4 và 5 → đúng MỘT khoảng thiếu 2 gói phải dựng lại được.
    run_dir = build_crashed_run(tmp_path, packets=10, missing_at=(4, 5))

    run_refinalize(run_dir)

    rows = read_rows(run_dir / "reports" / "node_summary.csv")
    node = next(row for row in rows if row["node_uid"] == NODE_A)
    assert int(node["missing"]) == 2
    assert int(node["packets"]) == 8


def test_run_summary_keeps_unknown_counters_blank(tmp_path):
    run_dir = build_crashed_run(tmp_path)

    run_refinalize(run_dir)

    rows = read_rows(run_dir / "reports" / "run_summary.csv")
    assert len(rows) == 1
    summary = rows[0]
    assert summary["run_id"] == run_dir.name
    assert int(summary["total_messages"]) == 10
    # Ba bộ đếm này chỉ sống trong RAM lúc chạy → phải TRỐNG, không được là "0".
    assert summary["parse_errors"] == ""
    assert summary["queue_dropped"] == ""
    assert summary["reconnects"] == ""


def test_gateway_result_has_single_total_row_last(tmp_path):
    run_dir = build_crashed_run(tmp_path)

    run_refinalize(run_dir)

    rows = read_rows(run_dir / "reports" / "gateway_result.csv")
    totals = [row for row in rows if row["gateway_uid"] == TOTAL_UID]
    assert len(totals) == 1
    assert rows[-1]["gateway_uid"] == TOTAL_UID


def test_focused_node_tables_rebuilt_for_tracked_node(tmp_path):
    run_dir = build_crashed_run(tmp_path)

    result, _written = run_refinalize(
        run_dir, focus_node=NODE_A, late_seconds=30.0,
    )

    names = {path.name for path in result.written}
    assert {
        "focused_node_summary.csv",
        "focused_node_missing.csv",
        "focused_node_late.csv",
    } <= names
    rows = read_rows(run_dir / "reports" / "focused_node_summary.csv")
    assert len(rows) >= 2
    assert sum(1 for row in rows if row["gateway_uid"] == "ALL") == 1


def test_focused_node_summary_has_no_late_when_timestamps_equal(tmp_path):
    # `received_at == gateway_timestamp` → trễ 0 s → không ca bắn hụt nào.
    run_dir = build_crashed_run(tmp_path)

    run_refinalize(run_dir, focus_node=NODE_A, late_seconds=30.0)

    rows = read_rows(run_dir / "reports" / "focused_node_late.csv")
    assert rows == []


def test_session_without_ping_warns_instead_of_failing(tmp_path):
    run_dir = build_crashed_run(tmp_path, with_ping=False)

    result, _written = run_refinalize(run_dir)

    assert any("ping" in warning.lower() for warning in result.warnings)
    assert (run_dir / "reports" / "ping_summary.csv").exists()


# --------------------------------------------------------------------------
# Báo cáo KHÔNG dựng lại được: giữ bản cũ, và nói rõ
# --------------------------------------------------------------------------
def test_existing_alerts_summary_is_kept_untouched(tmp_path):
    run_dir = build_crashed_run(tmp_path)
    alerts = run_dir / "reports" / "alerts_summary.csv"
    alerts.write_text("cột-cũ\nbản-của-phiên\n", encoding="utf-8")
    original = alerts.read_text(encoding="utf-8")

    result, _written = run_refinalize(run_dir)

    assert alerts.read_text(encoding="utf-8") == original
    assert "alerts_summary.csv" in result.kept_existing


def test_rebuilt_alerts_warn_about_unrebuildable_kinds(tmp_path):
    run_dir = build_crashed_run(tmp_path, missing_at=(4,))

    result, _written = run_refinalize(run_dir)

    assert (run_dir / "reports" / "alerts_summary.csv").exists()
    assert any("im lặng" in warning.lower() for warning in result.warnings)


def test_missing_gateway_downtime_is_reported_not_faked(tmp_path):
    run_dir = build_crashed_run(tmp_path)

    result, _written = run_refinalize(run_dir)

    assert not (run_dir / "reports" / "gateway_downtime.csv").exists()
    assert any(
        "gateway_downtime" in warning for warning in result.warnings
    )


# --------------------------------------------------------------------------
# Chốt lại meta.json
# --------------------------------------------------------------------------
def test_ended_at_and_duration_come_from_raw_data(tmp_path):
    run_dir = build_crashed_run(tmp_path)

    result, _written = run_refinalize(run_dir)

    # Mốc cuối lấy từ bằng chứng trên đĩa: ping chạy tới phút thứ 89.
    assert result.ended_at.startswith("2026-01-01T12:29")
    rows = read_rows(run_dir / "reports" / "run_summary.csv")
    assert rows[0]["session_start"].startswith("2026-01-01T12:00")
    assert result.duration_seconds is not None
    assert result.duration_seconds > 0


def test_open_pause_is_subtracted_from_duration(tmp_path):
    run_dir = build_crashed_run(tmp_path)

    result, _written = run_refinalize(run_dir, paused_seconds=120.0)

    rows = read_rows(run_dir / "reports" / "run_summary.csv")
    assert float(rows[0]["paused_seconds"]) == 120.0
    assert result.duration_seconds is not None


def test_refinalize_session_marks_status_and_keeps_raw(tmp_path):
    run_dir = build_crashed_run(tmp_path)
    before = raw_fingerprint(run_dir)
    RunSession(
        run_dir=run_dir, run_id=run_dir.name, status="RUNNING",
        started_at="2026-01-01T12:00:00+07:00",
    ).save()

    result, _written = run_refinalize(run_dir)
    session = refinalize_session(
        run_dir,
        ended_at=result.ended_at,
        duration_seconds=result.duration_seconds,
    )

    assert session.status == "REFINALIZED"
    assert session.ended_at == result.ended_at
    assert raw_fingerprint(run_dir) == before

    reloaded = RunSession.load(run_dir)
    assert reloaded.status == "REFINALIZED"
    assert reloaded.stop_reason


def test_refinalize_session_closes_open_pause(tmp_path):
    run_dir = build_crashed_run(tmp_path)
    session = RunSession(run_dir=run_dir, run_id=run_dir.name, status="RUNNING")
    session.mark_paused()
    session.save()
    assert session.pause_started_at

    refinalize_session(run_dir)

    reloaded = RunSession.load(run_dir)
    assert reloaded.pause_started_at == ""
    assert reloaded.status == "REFINALIZED"


# --------------------------------------------------------------------------
# Với tham số chấm thật (`scoring_params_snapshot`)
# --------------------------------------------------------------------------
def test_runs_with_session_scoring_params(tmp_path):
    from core.config import TestConfig

    run_dir = build_crashed_run(tmp_path)
    params = ScoringParams(scoring_params_snapshot(TestConfig()))

    result, _written = run_refinalize(run_dir, params=params)

    rows = read_rows(run_dir / "reports" / "run_summary.csv")
    assert rows[0]["result"] in ("ĐẠT", "KHÔNG ĐẠT")
    assert result.counts["mqtt_rows"] == 10
