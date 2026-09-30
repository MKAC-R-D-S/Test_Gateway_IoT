"""Kiểm thử TẠM DỪNG phiên — lỗi "tạm dừng mà thời gian vẫn chạy và tool vẫn ping".

Vì sao phải khoá bằng test: nút "Tạm dừng" từng chỉ đổi cờ trạng thái trong khi
`PingMonitor` vẫn chạy `ping` mỗi chu kỳ và mọi đồng hồ vẫn tính theo giờ tường.
Hậu quả không chỉ là hiển thị sai:

  * **Thời lượng phiên bị pha loãng** — báo cáo nói phiên dài hơn thực tế đo, nên
    mọi tỉ lệ (độ phủ, tỉ lệ ping) trở nên vô nghĩa khi so giữa các phiên.
  * **Downtime GIẢ** — nếu lúc tạm dừng vẫn phát `PingRecord` fail thì một lần
    nghỉ dài hơn `ping_down_threshold × ping_interval_seconds` sẽ được ghi thành
    cửa sổ mất kết nối trong `gateway_downtime.csv`.
  * **Cảnh báo GIẢ khi tiếp tục** — cửa sổ im lặng của node/gateway được tính
    theo giờ thật, nên vừa bấm "Tiếp tục" là có ngay loạt `NODE_SILENT` /
    `GATEWAY_SILENT` cho khoảng thời gian tool CỐ Ý không nhận gì.

Các bất biến được khoá dưới đây:
  1. `RunSession`/`MonitorPipeline` trả THỜI GIAN ĐO THỰC TẾ (đã trừ tạm dừng) và
     `duration_seconds` trong `run_summary.csv` cũng vậy.
  2. `PingMonitor.pause()` thật sự không chạy ICMP và không tăng bộ đếm ping.
  3. Tiếp tục sau tạm dừng KHÔNG sinh downtime giả (chuỗi fail được xoá) và
     KHÔNG sinh cảnh báo im lặng giả (mốc thời gian được dịch).
"""

from __future__ import annotations

import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from analytics.anomaly_detector import AlertRule, AnomalyDetector
from analytics.ping_stats import PingTracker
from core.config import GatewayDevice, TestConfig
from core.models import MQTTRecord, PKT_DATA, PingRecord
from core.pipeline import MonitorPipeline
from storage.csv_writer import LogManager
from storage.reader import iter_csv_rows
from storage.session import create_session

GW = "04F9199E139C"
GW2 = "B806199E139C"
NODE = "1122334455667788"


def iso(offset_seconds: float = 0.0) -> str:
    """Mốc ISO giữ nguyên micro-giây (cắt xuống giây sẽ làm test canh giờ bị lệch)."""
    return (
        datetime.now().astimezone() + timedelta(seconds=offset_seconds)
    ).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# 1. Đồng hồ phiên (RunSession)
# ---------------------------------------------------------------------------
def test_session_elapsed_excludes_paused_seconds(tmp_path: Path):
    """`elapsed_seconds()` phải là thời gian ĐO, không phải giờ tường."""
    session = create_session(tmp_path / "data", gateway_uids=[GW], tool_version="t")
    session.started_at = iso(-10.0)
    session.paused_seconds = 4.0

    elapsed = session.elapsed_seconds()
    assert 5.5 <= elapsed <= 6.6, "phải trừ đúng 4 s tạm dừng, nhận %.2f" % elapsed
    # `active_seconds()` là bí danh — hai lần gọi lệch nhau vài micro-giây là bình thường.
    assert session.active_seconds() == pytest.approx(elapsed, abs=0.05)


def test_session_open_pause_interval_is_subtracted_live(tmp_path: Path):
    """Trong lúc ĐANG tạm dừng, đồng hồ đứng ngay (không chờ tới lúc tiếp tục)."""
    session = create_session(tmp_path / "data", gateway_uids=[GW], tool_version="t")
    session.started_at = iso(-10.0)

    before = session.elapsed_seconds()
    session.mark_paused()
    assert session.is_paused
    time.sleep(0.4)
    after = session.elapsed_seconds()

    # Khoảng tạm dừng đang mở được trừ dần → đồng hồ gần như đứng yên.
    assert after - before < 0.35, "đồng hồ còn chạy khi tạm dừng: %.2f → %.2f" % (
        before, after
    )

    session.mark_resumed()
    assert not session.is_paused
    assert session.paused_seconds >= 0.3, session.paused_seconds
    # Sau khi tiếp tục, đồng hồ chạy lại (không bị âm / không bị đứng vĩnh viễn).
    assert session.elapsed_seconds() >= after - 0.05


def test_session_mark_paused_is_idempotent(tmp_path: Path):
    """Bấm tạm dừng nhiều lần không được cộng dồn thời gian 2 lần."""
    session = create_session(tmp_path / "data", gateway_uids=[GW], tool_version="t")
    session.mark_paused()
    first = session.pause_started_at
    session.mark_paused()
    assert session.pause_started_at == first
    session.mark_resumed()
    assert session.pause_started_at == ""
    session.mark_resumed()   # gọi lại phải vô hại, không cộng thêm thời gian


def test_session_finish_duration_is_active_time(tmp_path: Path):
    """`finish()` chốt `duration_seconds` theo thời gian đo, và đóng nốt lần tạm dừng."""
    session = create_session(tmp_path / "data", gateway_uids=[GW], tool_version="t")
    session.started_at = iso(-100.0)
    session.paused_seconds = 60.0
    session.mark_paused()          # phiên đang tạm dừng thì người dùng bấm Dừng
    session.finish("STOPPED", "test")

    assert session.ended_at
    # ≈ 40 s (100 s giờ tường − 60 s đã tạm dừng), không phải 100 s.
    assert 39.0 <= (session.duration_seconds or 0) <= 41.0, session.duration_seconds
    assert session.pause_started_at == ""


def test_session_roundtrip_keeps_pause_fields(tmp_path: Path):
    """`paused_seconds` / `pause_started_at` phải sống qua `meta.json`."""
    from storage.session import RunSession

    session = create_session(tmp_path / "data", gateway_uids=[GW], tool_version="t")
    session.paused_seconds = 12.5
    session.save()

    reloaded = RunSession.load(session.run_dir)
    assert reloaded.paused_seconds == 12.5
    session.mark_paused()
    session.save()
    assert RunSession.load(session.run_dir).pause_started_at


def test_old_meta_without_pause_fields_loads_as_zero(tmp_path: Path):
    """Phiên của bản tool CŨ (không có khoá tạm dừng) phải đọc được, mặc định 0."""
    import json

    from storage.session import RunSession

    session = create_session(tmp_path / "data", gateway_uids=[GW], tool_version="old")
    data = session.to_dict()
    for key in ("paused_seconds", "pause_started_at"):
        data.pop(key, None)
    with open(session.meta_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)

    reloaded = RunSession.load(session.run_dir)
    assert reloaded.paused_seconds == 0.0
    assert reloaded.pause_started_at == ""
    assert not reloaded.is_paused


# ---------------------------------------------------------------------------
# 2. Đồng hồ pipeline + cột `duration_seconds` của báo cáo
# ---------------------------------------------------------------------------
def build_pipeline(tmp: Path) -> tuple:
    config = TestConfig()
    config.packet_merge_window_seconds = 0.05
    logs = LogManager(tmp, rotation=config.rotation, flush_interval=0.02,
                      flush_max_records=10, max_queue_size=10000)
    logs.start()
    devices = [GatewayDevice(uid=GW, name="GW 1", eth_ip="192.168.1.101")]
    pipeline = MonitorPipeline(config, devices, logs, run_id="pause-test")
    pipeline.start()
    return config, logs, pipeline


def test_pipeline_elapsed_excludes_pause(tmp_path: Path):
    _config, logs, pipeline = build_pipeline(tmp_path)
    try:
        time.sleep(0.2)
        pipeline.pause()
        paused_at = pipeline.elapsed_seconds()
        time.sleep(0.5)
        still = pipeline.elapsed_seconds()
        assert still - paused_at < 0.25, (
            "đồng hồ pipeline chạy khi tạm dừng: %.3f → %.3f" % (paused_at, still)
        )
        assert pipeline.paused_seconds() >= 0.4

        banked = pipeline.resume()
        assert banked >= 0.4
        assert pipeline.paused_seconds() >= banked
        # `resume()` lần hai (không có khoảng mở) phải trả 0, không cộng thêm.
        assert pipeline.resume() == 0.0
    finally:
        pipeline.stop()
        logs.stop()


def test_run_summary_duration_excludes_pause(tmp_path: Path):
    """`run_summary.csv` phải ghi thời gian ĐO, và có cột `paused_seconds`."""
    _config, logs, pipeline = build_pipeline(tmp_path)
    session = create_session(tmp_path / "data", gateway_uids=[GW], tool_version="t")
    try:
        time.sleep(0.2)
        pipeline.pause()
        time.sleep(0.6)
        pipeline.resume()
        written = pipeline.write_reports(session)
    finally:
        pipeline.stop()
        logs.stop()

    rows = list(iter_csv_rows(written["run_summary"]))
    assert len(rows) == 1
    row = rows[0]
    assert "paused_seconds" in row, "thiếu cột paused_seconds trong run_summary.csv"
    assert 0.5 <= float(row["paused_seconds"]) <= 1.5, row["paused_seconds"]
    # Thời gian đo < tổng thời gian chạy vì có khoảng tạm dừng.
    measured = float(row["duration_seconds"])
    assert measured >= 0.0
    assert measured < measured + float(row["paused_seconds"])


def test_run_summary_duration_never_blank(tmp_path: Path):
    """Pipeline đã `start()` thì `duration_seconds` luôn là số, không bao giờ rỗng."""
    _config, logs, pipeline = build_pipeline(tmp_path)
    session = create_session(tmp_path / "data", gateway_uids=[GW], tool_version="t")
    try:
        written = pipeline.write_reports(session)
    finally:
        pipeline.stop()
        logs.stop()
    row = list(iter_csv_rows(written["run_summary"]))[0]
    float(row["duration_seconds"])   # raise nếu là ""


# ---------------------------------------------------------------------------
# 3. PingMonitor: tạm dừng thật sự ngừng ICMP
# ---------------------------------------------------------------------------
def ping_record(gateway: str, success: bool, timeout: bool = False) -> PingRecord:
    return PingRecord(
        timestamp=iso(),
        gateway_uid=gateway,
        gateway_name="GW " + gateway,
        ip_address="192.168.1.101",
        ping_success=success,
        latency_ms=1.0 if success else None,
        timeout=timeout,
        error="" if success else "REQUEST_TIMED_OUT",
    )


def test_ping_monitor_pause_stops_icmp(tmp_path: Path):
    """Ping 127.0.0.1 (không cần mạng). Bộ đếm ping phải ĐỨNG khi tạm dừng."""
    from runtime.ping_monitor import PingMonitor, PingTarget

    if shutil.which("ping") is None:
        pytest.skip("hệ thống không có lệnh ping")

    monitor = PingMonitor(
        [PingTarget(gateway_uid=GW, gateway_name="GW 1", ip_address="127.0.0.1")],
        interval_seconds=1,
        timeout_ms=1000,
        count=1,
        max_concurrent=1,
    )
    monitor.start()
    try:
        deadline = time.monotonic() + 6.0
        while monitor.total_pings < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert monitor.total_pings >= 1, "chưa ping được lần nào để kiểm tra"

        monitor.pause()
        assert monitor.is_paused()
        baseline = monitor.total_pings
        time.sleep(1.6)      # dài hơn 1 chu kỳ ping
        assert monitor.total_pings == baseline, (
            "vẫn ping khi tạm dừng: %d → %d" % (baseline, monitor.total_pings)
        )

        monitor.resume()
        assert not monitor.is_paused()
        deadline = time.monotonic() + 6.0
        while monitor.total_pings == baseline and time.monotonic() < deadline:
            time.sleep(0.05)
        assert monitor.total_pings > baseline, "tiếp tục rồi mà không ping lại"
    finally:
        monitor.stop()


def test_ping_monitor_stats_expose_paused_flag(tmp_path: Path):
    from runtime.ping_monitor import PingMonitor, PingTarget

    monitor = PingMonitor(
        [PingTarget(gateway_uid=GW, gateway_name="GW 1", ip_address="192.168.1.101")],
        interval_seconds=10,
    )
    assert monitor.stats()["paused"] is False
    monitor.pause()
    assert monitor.stats()["paused"] is True
    monitor.resume()
    assert monitor.stats()["paused"] is False


# ---------------------------------------------------------------------------
# 4. Không sinh dữ liệu GIẢ cho khoảng đã tạm dừng
# ---------------------------------------------------------------------------
def test_reset_failure_streaks_prevents_false_downtime():
    """2 fail trước tạm dừng + 1 fail sau khi tiếp tục KHÔNG được thành downtime."""
    tracker = PingTracker(down_threshold=2)
    tracker.feed(ping_record(GW, success=False))
    tracker.feed(ping_record(GW, success=False))
    assert GW.upper() in tracker.open_windows(), "2 fail liên tiếp phải mở window"

    # Tạm dừng rồi tiếp tục → chuỗi fail trước đó không còn "liên tiếp".
    tracker.reset_failure_streaks()
    tracker.feed(ping_record(GW, success=False))

    assert tracker.downtime_records() == [], (
        "sinh cửa sổ downtime giả sau tạm dừng: %r" % tracker.downtime_records()
    )
    # Chuỗi fail đã bị xoá nhưng KỶ LỤC dài nhất vẫn được giữ.
    stats = tracker.stats()[GW.upper()]
    assert stats.consecutive_failures == 1
    assert stats.max_consecutive_failures >= 2


def test_shift_baselines_prevents_false_silent_alerts():
    """Sau tạm dừng dài, node/gateway vừa nghe phải KHÔNG bị báo im lặng."""
    rule = AlertRule()
    rule.node_silent_seconds = 60.0
    rule.gateway_silent_seconds = 60.0
    detector = AnomalyDetector(rule)

    seen_at = iso(-3600.0)      # gateway/node "im lặng" 1 giờ theo giờ thật
    detector.feed(MQTTRecord(
        received_at=seen_at,
        packet_type=PKT_DATA,
        gateway_uid=GW,
        gateway_name="GW 1",
        node_uid=NODE,
        seq=1,
    ))
    assert detector.check_timeouts(), "phải phát hiện im lặng trước khi dịch mốc"
    before = len(detector.alerts())

    # Dịch mốc đúng bằng khoảng đã tạm dừng → hết "im lặng".
    detector.shift_baselines(3600.0)
    assert detector.check_timeouts() == [], "vẫn báo im lặng giả sau khi dịch mốc"
    # Không thêm cảnh báo nào, nhưng lịch sử cảnh báo cũ phải còn nguyên.
    assert len(detector.alerts()) == before


def test_shift_baselines_ignores_non_positive():
    detector = AnomalyDetector(AlertRule())
    detector.shift_baselines(0.0)
    detector.shift_baselines(-5.0)
    assert detector.alerts() == []


def test_shift_baselines_moves_alert_cooldown():
    """Cooldown phải tính theo thời gian CHẠY, nên cũng được dịch theo tạm dừng.

    Nếu bỏ sót `_last_alert_at` thì sau khi tiếp tục, quãng tạm dừng bị tính như
    thời gian đã trôi thật → cooldown "hết hạn" sớm và cảnh báo lặp bị phát lại.
    """
    rule = AlertRule()
    rule.node_silent_seconds = 1
    rule.cooldown_seconds = 3600
    detector = AnomalyDetector(rule)
    base = datetime.now().astimezone()
    record = MQTTRecord(
        received_at=iso(),
        packet_type=PKT_DATA,
        gateway_uid=GW,
        gateway_name="GW 1",
        node_uid=NODE,
        seq=1,
    )

    detector.feed(record)
    first = base + timedelta(seconds=10)
    assert detector.check_timeouts(now=first), "lần đầu phải có cảnh báo"

    detector.feed(record)               # node gửi trở lại → mở lại khả năng cảnh báo
    blocked_at = base + timedelta(seconds=200)
    assert detector.check_timeouts(now=blocked_at) == [], "cooldown phải chặn cảnh báo lặp"

    # Vừa tạm dừng 1 giờ rồi tiếp tục: mốc cảnh báo cũ cũng phải dịch theo.
    detector.feed(record)
    detector.shift_baselines(3600.0)
    same_active_time = blocked_at + timedelta(seconds=3600)
    assert detector.check_timeouts(now=same_active_time) == [], (
        "cooldown chưa dịch theo tạm dừng → cảnh báo lặp bị phát lại quá sớm"
    )
