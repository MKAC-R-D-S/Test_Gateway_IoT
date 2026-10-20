"""Kiểm thử `analytics.gateway_rescore` — chấm LẠI phiên cũ từ CSV thô.

Vì sao phải khoá bằng test: chức năng "Chấm lại kết quả" dựng lại ĐẠT/KHÔNG ĐẠT
của một phiên đã chạy rồi GHI vào `reports/` của chính phiên đó. Sai ở đây nghĩa
là ghi kết quả sai vào dữ liệu lịch sử của khách. Các bất biến phải được khoá:

  * **Bất biến quan trọng nhất: dữ liệu thô không bị chạm.** Chấm lại chỉ ĐỌC
    `mqtt/`, `ping/` và chỉ GHI THÊM vào `reports/`.
  * **Chỉ một dòng TỔNG, và luôn là dòng CUỐI** — tab Lịch sử dựa vào bất biến
    này để tách "dòng kết luận" khỏi "dòng gateway".
  * **Ảnh chụp tham số trong `meta.json` thắng `config.json` hiện tại** — kết quả
    của một phiên đã chạy không được đổi chỉ vì ai đó sửa ngưỡng sau đó.
  * **Thiếu nguồn thì `N/A`, không bịa số**: không có `focused_node_summary.csv`
    → `c_late = N/A`; không có ping → tiêu chí ping `N/A`.
  * **Không có `mqtt_data_*.csv` → `RescoreError`**, nói rõ lý do thay vì ghi ra
    file toàn `N/A` (người dùng sẽ tưởng gateway trượt).
"""

from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

import pytest

from analytics.gateway_rescore import (
    GATEWAY_RESULT_FILENAME,
    RUN_SUMMARY_FILENAME,
    RescoreError,
    read_result_rows,
    rescore_device_names,
    rescore_run,
    result_overall,
    run_data_sources,
    write_rescore_reports,
)
from analytics.gateway_result import (
    FAIL,
    NA,
    PASS,
    RESULT_FAIL,
    RESULT_PASS,
    TOTAL_UID,
    ScoringParams,
    scoring_params_snapshot,
)
from storage.reader import iter_csv_rows
from storage.schema import (
    GATEWAY_RESULT_COLUMNS,
    MQTT_DATA_COLUMNS,
    MQTT_GATEWAY_LOG_COLUMNS,
    PING_LOG_COLUMNS,
    RUN_SUMMARY_COLUMNS,
)

GW1 = "04F9199E139C"
GW2 = "B806199E139C"
NODE_A = "1122334455667788"
NODE_B = "AABBCCDDEEFF0011"


# --------------------------------------------------------------------------
# Trợ giúp: dựng một thư mục phiên tối thiểu trên đĩa
# --------------------------------------------------------------------------
def write_csv(path: Path, columns, rows) -> Path:
    """Ghi CSV đúng định dạng tool (`utf-8-sig`, cột theo schema)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns),
                                extrasaction="ignore", restval="")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def mqtt_row(gateways, node=NODE_A, packet_type="DATA", seq=1) -> dict:
    row = {col: "" for col in MQTT_DATA_COLUMNS}
    row.update({
        "packet_type": packet_type,
        "node_uid": node,
        "seq": str(seq),
        "gateways": "|".join(gateways),
        "gateway_uid": gateways[0] if gateways else "",
        "received_at": "2026-01-01T12:00:%02d+07:00" % (seq % 60),
    })
    return row


def ping_log_row(uid, success=True, index=0) -> dict:
    # Tên cột phải ĐÚNG schema: `analytics.ping_stats.summarize_ping_csv` đọc
    # `ping_success` / `latency_ms`, nên dùng tên khác là dữ liệu rơi vào hư không
    # (DictWriter dùng extrasaction="ignore") và tỉ lệ ping thành 0 %.
    row = {col: "" for col in PING_LOG_COLUMNS}
    row.update({
        "gateway_uid": uid,
        "ip_address": "10.0.0.1",
        "timestamp": "2026-01-01T12:00:%02d+07:00" % (index % 60),
        "ping_success": "1" if success else "0",
        "latency_ms": "1.5" if success else "",
        "timeout": "0" if success else "1",
    })
    return row


def log_row(uid, status="CONNECTED", index=0) -> dict:
    row = {col: "" for col in MQTT_GATEWAY_LOG_COLUMNS}
    row.update({
        "gateway_uid": uid,
        "received_at": "2026-01-01T12:00:%02d+07:00" % (index % 60),
        "gateway_timestamp": "2026-01-01T12:00:%02d+07:00" % (index % 60),
        "status": status,
    })
    return row


def build_run(
    root: Path,
    gateways=(GW1,),
    mqtt_packets=200,
    ping_total=100,
    ping_failed=0,
    with_ping_log=True,
    with_log=True,
    with_focused=False,
    with_run_summary=True,
    run_summary_extra=None,
) -> Path:
    """Dựng một phiên tối thiểu: đủ dữ liệu thô để chấm ra ĐẠT.

    Mỗi bản tin được CẢ các gateway cùng nghe, nên độ phủ = 100 % và các tiêu
    chí chấm điểm đều đạt.
    """
    run_dir = root / "20260101_120000_AAAABBBBCCCC"
    (run_dir / "mqtt").mkdir(parents=True, exist_ok=True)
    (run_dir / "ping").mkdir(parents=True, exist_ok=True)
    (run_dir / "reports").mkdir(parents=True, exist_ok=True)

    write_csv(
        run_dir / "mqtt" / "mqtt_data_20260101.csv",
        MQTT_DATA_COLUMNS,
        [mqtt_row(list(gateways), node=NODE_A, seq=index + 1)
         for index in range(mqtt_packets)],
    )
    if with_ping_log:
        rows = []
        for uid in gateways:
            for index in range(ping_total):
                rows.append(ping_log_row(
                    uid, success=index >= ping_failed, index=index
                ))
        write_csv(run_dir / "ping" / "ping_log_20260101.csv", PING_LOG_COLUMNS, rows)
    if with_log:
        write_csv(
            run_dir / "mqtt" / "mqtt_gateway_log_20260101.csv",
            MQTT_GATEWAY_LOG_COLUMNS,
            [log_row(uid, index=index) for uid in gateways for index in range(3)],
        )
    if with_run_summary:
        summary = {col: "" for col in RUN_SUMMARY_COLUMNS}
        summary.update({
            "run_id": run_dir.name,
            "session_start": "2026-01-01T12:00:00+07:00",
            "session_end": "2026-01-01T14:00:00+07:00",
            "duration_seconds": 7200,
            "total_messages": mqtt_packets,
            "gateways_passed": 0,
            "gateways_failed": 0,
            "result": "",
            "result_reason": "",
        })
        summary.update(run_summary_extra or {})
        write_csv(run_dir / "reports" / RUN_SUMMARY_FILENAME,
                  RUN_SUMMARY_COLUMNS, [summary])
    return run_dir


def snapshot(run_dir: Path, **keys):
    """Ghi `scoring_params` vào meta.json của phiên (như lúc bắt đầu phiên)."""
    import json

    meta_path = run_dir / "meta.json"
    data = {"run_id": run_dir.name, "status": "STOPPED", "ended_at": ""}
    if meta_path.exists():
        data.update(json.loads(meta_path.read_text(encoding="utf-8")))
    data["scoring_params"] = dict(keys)
    meta_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return meta_path


# --------------------------------------------------------------------------
# run_data_sources
# --------------------------------------------------------------------------
def test_run_data_sources_detects_available_sources(tmp_path):
    run_dir = build_run(tmp_path, gateways=(GW1, GW2))

    sources = run_data_sources(run_dir)

    assert sources["has_mqtt"] is True
    assert sources["has_ping"] is True
    assert sources["run_dir"] == run_dir
    assert [p.name for p in sources["mqtt_files"]] == ["mqtt_data_20260101.csv"]
    assert [p.name for p in sources["ping_files"]] == ["ping_log_20260101.csv"]
    assert sources["focused_summary"] == (run_dir / "reports"
                                          / "focused_node_summary.csv")


def test_run_data_sources_reports_missing_mqtt(tmp_path):
    run_dir = build_run(tmp_path)
    (run_dir / "mqtt" / "mqtt_data_20260101.csv").unlink()

    sources = run_data_sources(run_dir)

    assert sources["has_mqtt"] is False


def test_run_data_sources_counts_locked_ping_summary_as_ping(tmp_path):
    """`ping_summary.csv` (bản chốt) cũng đủ để chấm tiêu chí ping."""
    run_dir = build_run(tmp_path, with_ping_log=False)
    write_csv(run_dir / "reports" / "ping_summary.csv", ["gateway_uid"],
              [{"gateway_uid": GW1}])

    sources = run_data_sources(run_dir)

    assert sources["has_ping"] is True


# --------------------------------------------------------------------------
# Chấm ra kết quả
# --------------------------------------------------------------------------
def test_rescore_run_writes_result_and_overall_row_is_last(tmp_path):
    run_dir = build_run(tmp_path, gateways=(GW1, GW2))

    result = rescore_run(run_dir, SimpleNamespace())

    assert result.gateways_scored == 2
    assert result.result == RESULT_PASS
    assert result.gateways_passed == 2 and result.gateways_failed == 0
    path = run_dir / "reports" / GATEWAY_RESULT_FILENAME
    assert path.exists()
    assert result.written and path in result.written

    rows = list(iter_csv_rows(path))
    assert list(rows[0].keys()) == GATEWAY_RESULT_COLUMNS
    assert rows[-1]["gateway_uid"] == TOTAL_UID
    assert [r["gateway_uid"] for r in rows].count(TOTAL_UID) == 1


def test_rescore_run_never_touches_raw_data(tmp_path):
    """Bất biến: chấm lại chỉ ĐỌC `mqtt/` + `ping/`, không sửa byte nào."""
    run_dir = build_run(tmp_path, gateways=(GW1,))
    raw = sorted((run_dir / "mqtt").glob("*.csv")) + sorted(
        (run_dir / "ping").glob("*.csv")
    )
    before = {path: path.read_bytes() for path in raw}

    rescore_run(run_dir, SimpleNamespace())

    assert {path: path.read_bytes() for path in raw} == before


def test_rescore_run_raises_without_mqtt_data(tmp_path):
    run_dir = build_run(tmp_path)
    (run_dir / "mqtt" / "mqtt_data_20260101.csv").unlink()

    with pytest.raises(RescoreError) as excinfo:
        rescore_run(run_dir, SimpleNamespace())

    assert "mqtt_data" in str(excinfo.value)
    assert not (run_dir / "reports" / GATEWAY_RESULT_FILENAME).exists()


def test_rescore_run_is_repeatable(tmp_path):
    """Chấm lại lần hai phải cho CÙNG kết quả (dữ liệu thô không bị sửa)."""
    run_dir = build_run(tmp_path, gateways=(GW1, GW2))
    first = rescore_run(run_dir, SimpleNamespace())
    after_first = (run_dir / "reports" / GATEWAY_RESULT_FILENAME).read_bytes()

    second = rescore_run(run_dir, SimpleNamespace())

    assert second.gateways_scored == first.gateways_scored
    assert second.result == first.result
    assert second.result_reason == first.result_reason
    assert (run_dir / "reports" / GATEWAY_RESULT_FILENAME).read_bytes() == after_first


def test_rescore_late_is_na_without_focused_summary(tmp_path):
    run_dir = build_run(tmp_path, with_focused=False)

    result = rescore_run(run_dir, SimpleNamespace())

    assert result.missing_late_data is True
    assert any("Bắn hụt" in w for w in result.warnings)
    gateway = [r for r in result.rows if r["gateway_uid"] != TOTAL_UID][0]
    assert gateway["c_late"] == NA


def test_rescore_marks_missing_session_params_and_warns(tmp_path):
    """Phiên của bản tool cũ (không có ảnh chụp tham số) phải được cảnh báo."""
    run_dir = build_run(tmp_path)

    result = rescore_run(run_dir, SimpleNamespace())

    assert result.params_from_session is False
    assert any("scoring_params" in w for w in result.warnings)


def test_rescore_reports_params_from_session_when_present(tmp_path):
    run_dir = build_run(tmp_path)
    snapshot(run_dir, min_coverage_percent=95.0)
    result = rescore_run(run_dir, SimpleNamespace(),
                         session_params={"min_coverage_percent": 95.0})

    assert result.params_from_session is True
    assert not any("scoring_params" in w for w in result.warnings)


def test_rescore_without_ping_warns_and_ping_criteria_are_na(tmp_path):
    run_dir = build_run(tmp_path, with_ping_log=False)

    result = rescore_run(run_dir, SimpleNamespace())

    assert any("ping" in w.lower() for w in result.warnings)
    gateway = [r for r in result.rows if r["gateway_uid"] != TOTAL_UID][0]
    assert gateway["c_ping"] == NA


# --------------------------------------------------------------------------
# Tham số: ảnh chụp của phiên phải THẮNG config hiện tại
# --------------------------------------------------------------------------
def test_session_params_win_over_current_config(tmp_path):
    """Ngưỡng chốt trong phiên thắng `config.json` — kết quả không được đổi."""
    run_dir = build_run(tmp_path, gateways=(GW1,), mqtt_packets=200)
    # Gateway chỉ gửi 3/4 số bản tin → độ phủ 75 % → trượt nếu ngưỡng là 95 %.
    write_csv(
        run_dir / "mqtt" / "mqtt_data_20260101.csv",
        MQTT_DATA_COLUMNS,
        [mqtt_row([GW1], node=NODE_A, seq=index + 1) for index in range(150)]
        + [mqtt_row([GW2], node=NODE_A, seq=index + 151) for index in range(50)],
    )
    strict = rescore_run(run_dir, SimpleNamespace(),
                         session_params={"min_coverage_percent": 95.0})
    lenient = rescore_run(run_dir, SimpleNamespace(),
                          session_params={"min_coverage_percent": 10.0})

    strict_row = [r for r in strict.rows if r["gateway_uid"] == GW1][0]
    lenient_row = [r for r in lenient.rows if r["gateway_uid"] == GW1][0]
    # Cột tiêu chí (`c_*`) dùng hằng ASCII PASS/FAIL/NA; chỉ cột `result` mới
    # chứa chữ Việt ĐẠT/KHÔNG ĐẠT — xem `analytics/gateway_result.py`.
    assert strict_row["c_coverage"] == FAIL
    assert lenient_row["c_coverage"] == PASS


def test_config_used_when_session_has_no_snapshot(tmp_path):
    """Phiên cũ: ngưỡng lấy từ config hiện tại (không có ảnh chụp nào khác)."""
    run_dir = build_run(tmp_path, gateways=(GW1,))
    write_csv(
        run_dir / "mqtt" / "mqtt_data_20260101.csv",
        MQTT_DATA_COLUMNS,
        [mqtt_row([GW1], node=NODE_A, seq=index + 1) for index in range(150)]
        + [mqtt_row([GW2], node=NODE_A, seq=index + 151) for index in range(50)],
    )

    result = rescore_run(
        run_dir, SimpleNamespace(criteria_min_coverage_percent=10.0)
    )

    row = [r for r in result.rows if r["gateway_uid"] == GW1][0]
    assert row["c_coverage"] == PASS


# --------------------------------------------------------------------------
# async def write_rescore_reports / làm mới run_summary.csv
# --------------------------------------------------------------------------
def test_write_rescore_reports_updates_only_result_columns(tmp_path):
    run_dir = build_run(tmp_path)
    write_csv(run_dir / "reports" / RUN_SUMMARY_FILENAME, RUN_SUMMARY_COLUMNS, [{
        "run_id": run_dir.name, "session_start": "S", "session_end": "E",
        "duration_seconds": 7200, "broker": "1.2.3.4:1885", "total_messages": 777,
        "gateways_passed": 0, "gateways_failed": 0, "result": "",
        "result_reason": "",
    }])
    rows = [{"gateway_uid": GW1, "result": RESULT_PASS,
             "result_reason": "ok"},
            {"gateway_uid": TOTAL_UID, "result": RESULT_PASS,
             "result_reason": "1/1 gateway đạt"}]

    written = write_rescore_reports(run_dir, rows)

    summary = list(iter_csv_rows(run_dir / "reports" / RUN_SUMMARY_FILENAME))[0]
    assert (run_dir / "reports" / RUN_SUMMARY_FILENAME) in written
    assert summary["gateways_passed"] == "1"
    assert summary["gateways_failed"] == "0"
    assert summary["result"] == RESULT_PASS
    assert summary["result_reason"] == "1/1 gateway đạt"
    # Mọi cột KHÁC phải được giữ nguyên.
    assert summary["total_messages"] == "777"
    assert summary["broker"] == "1.2.3.4:1885"
    assert summary["duration_seconds"] == "7200"


def test_write_rescore_reports_keeps_extra_columns_of_old_runs(tmp_path):
    """Bản tool mới thêm cột → chấm lại KHÔNG được làm mất cột đó."""
    run_dir = build_run(tmp_path)
    columns = list(RUN_SUMMARY_COLUMNS) + ["cot_moi_cua_ban_sau"]
    write_csv(run_dir / "reports" / RUN_SUMMARY_FILENAME, columns,
              [{"run_id": run_dir.name, "cot_moi_cua_ban_sau": "giu-toi"}])

    write_rescore_reports(run_dir, [{"gateway_uid": TOTAL_UID,
                                     "result": RESULT_PASS}])

    rows = list(iter_csv_rows(run_dir / "reports" / RUN_SUMMARY_FILENAME))
    assert list(rows[0].keys()) == columns
    assert rows[0]["cot_moi_cua_ban_sau"] == "giu-toi"


def test_write_rescore_reports_without_run_summary_writes_only_result(tmp_path):
    run_dir = build_run(tmp_path, with_run_summary=False)

    written = write_rescore_reports(run_dir, [{"gateway_uid": TOTAL_UID}])

    assert [p.name for p in written] == [GATEWAY_RESULT_FILENAME]
    assert not (run_dir / "reports" / RUN_SUMMARY_FILENAME).exists()
    assert (run_dir / "reports" / GATEWAY_RESULT_FILENAME).exists()


# --------------------------------------------------------------------------
# Đọc lại kết quả
# --------------------------------------------------------------------------
def test_read_result_rows_empty_when_never_scored(tmp_path):
    run_dir = build_run(tmp_path)

    assert read_result_rows(run_dir) == []
    assert result_overall(run_dir) == {}


def test_read_result_rows_and_overall_after_rescore(tmp_path):
    run_dir = build_run(tmp_path, gateways=(GW1, GW2))
    rescore_run(run_dir, SimpleNamespace())

    rows = read_result_rows(run_dir)
    overall = result_overall(run_dir)

    assert len(rows) == 3
    assert overall["gateway_uid"] == TOTAL_UID
    assert overall["result"] == RESULT_PASS


def test_rescore_device_names_are_human_readable(tmp_path):
    run_dir = build_run(tmp_path, gateways=(GW1,))
    rescore_run(run_dir, SimpleNamespace())

    names = rescore_device_names(run_dir)

    assert GW1 in names or any(GW1 in key for key in names)


# --------------------------------------------------------------------------
# Ảnh chụp tham số
# --------------------------------------------------------------------------
def test_scoring_params_snapshot_reads_criteria_prefix():
    config = SimpleNamespace(
        criteria_min_coverage_percent=90.0,
        criteria_min_ping_ok_percent=80.0,
        criteria_max_late_percent=3.0,
        criteria_min_packets_for_rate=10,
        ping_down_threshold=4,
        correlation_window_seconds=7,
        silent_window_threshold=3,
    )

    snapshot = scoring_params_snapshot(config)

    assert snapshot["min_coverage_percent"] == 90.0
    assert snapshot["min_packets_for_rate"] == 10
    assert snapshot["ping_down_threshold"] == 4
    assert snapshot["correlation_window_seconds"] == 7
    assert snapshot["silent_window_threshold"] == 3


def test_scoring_params_snapshot_falls_back_to_defaults():
    snapshot = scoring_params_snapshot(SimpleNamespace())

    assert snapshot["min_coverage_percent"] == 80.0
    assert snapshot["ping_down_threshold"] == 2
    # `focused_node_*` cũng nằm trong ảnh chụp: node trọng tâm của phiên phải
    # chốt lại được, nếu không thì nút "Chốt lại báo cáo" sẽ dựng bảng bắt hụt /
    # bắn hụt theo node HIỆN TẠI trong config.json (có thể đã đổi).
    assert snapshot["focused_node_uid"] == ""
    assert set(snapshot) == {
        "min_coverage_percent", "min_ping_ok_percent",
        "max_late_percent", "min_packets_for_rate",
        "ping_down_threshold", "correlation_window_seconds",
        "silent_window_threshold",
        "focused_node_uid", "focused_node_late_seconds",
    }


def test_scoring_params_exposes_criteria_prefix_and_analysis_names():
    params = ScoringParams({
        "min_coverage_percent": 90,
        "min_packets_for_rate": 10,
        "ping_down_threshold": 3,
    })

    assert params.criteria_min_coverage_percent == 90.0
    assert params.criteria_min_packets_for_rate == 10
    assert params.ping_down_threshold == 3


def test_scoring_params_ignores_blank_values():
    params = ScoringParams({"min_coverage_percent": "", "ping_down_threshold": None})

    assert not hasattr(params, "criteria_min_coverage_percent")
    assert not hasattr(params, "ping_down_threshold")


def test_scoring_params_carries_focused_node_snapshot():
    params = ScoringParams({
        "focused_node_uid": "0011223344556677",
        "focused_node_late_seconds": 45,
    })

    assert params.focused_node_uid == "0011223344556677"
    assert params.focused_node_late_seconds == 45.0


def test_ping_down_threshold_changes_reference_downtime_columns(tmp_path):
    """Ngưỡng ping phải được dùng cho các CỘT downtime tham khảo.

    Từ khi bỏ tiêu chí downtime, thay đổi này KHÔNG còn làm đổi kết luận — nhưng
    các cột `downtime_events` / `downtime_seconds` vẫn phải phản ánh đúng ngưỡng
    để người đọc CSV không bị hiểu nhầm.
    """
    run_dir = build_run(tmp_path, gateways=(GW1,), ping_total=60, ping_failed=0)
    rows = [ping_log_row(GW1, success=True, index=index) for index in range(60)]
    del rows[30]                        # tạo 1 lần fail đơn lẻ
    rows.insert(30, ping_log_row(GW1, success=False, index=99))
    write_csv(run_dir / "ping" / "ping_log_20260101.csv", PING_LOG_COLUMNS, rows)

    lenient = rescore_run(run_dir, SimpleNamespace(),
                          session_params={"ping_down_threshold": 2})
    strict = rescore_run(run_dir, SimpleNamespace(),
                         session_params={"ping_down_threshold": 1})

    lenient_row = [r for r in lenient.rows if r["gateway_uid"] == GW1][0]
    strict_row = [r for r in strict.rows if r["gateway_uid"] == GW1][0]
    assert int(lenient_row["downtime_events"] or 0) == 0
    assert int(strict_row["downtime_events"] or 0) == 1
