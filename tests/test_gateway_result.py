"""Kiểm thử `GatewayResultEvaluator` — chấm ĐẠT/KHÔNG ĐẠT theo TỪNG tiêu chí.

Vì sao phải kiểm bằng test chứ không chỉ đọc code: kết luận ĐẠT/KHÔNG ĐẠT là
thứ người dùng đọc để quyết định gateway có đạt hay không, nên mọi ranh giới dễ
sai đều phải bị khoá lại:

  * **Chỉ còn 4 tiêu chí TRỰC TIẾP**: có dữ liệu, độ phủ bản tin, tỉ lệ ping,
    bắn hụt. `c_downtime`, `c_link` (nhãn `verdict` ping↔MQTT) và `c_stable`
    (% cửa sổ im lặng) ĐÃ BỎ — chúng chỉ là suy luận gián tiếp, phụ thuộc
    `correlation_window_seconds` nên chấm theo là trượt oan.
  * **Cột tham khảo không được ảnh hưởng kết quả** — `verdict`, `windows_*`,
    `silence_*`, `downtime_*` vẫn xuất ra CSV nhưng mọi giá trị của chúng đều
    phải cho CÙNG một kết luận.
  * **`N/A` ≠ `FAIL`** — không bật ping thì `c_ping` là `N/A`, không phải trượt.
  * **Chặn theo cỡ mẫu** — mẫu quá nhỏ phải ra `N/A` chứ không ra `PASS`.
  * **Mẫu số độ phủ theo TỪNG gateway** — gateway không nghe được node nào đó
    (ngoài tầm sóng) không được bị trừ điểm oan.
"""

from __future__ import annotations

from types import SimpleNamespace

from analytics.correlation import (
    VERDICT_GATEWAY_SILENT,
    VERDICT_LINK_DOWN,
    VERDICT_NO_DATA,
    VERDICT_OK,
    VERDICT_PARTIAL,
    VERDICT_PING_BLOCKED,
    GatewayCorrelation,
)
from analytics.gateway_result import (
    CRITERIA,
    FAIL,
    NA,
    PASS,
    RESULT_FAIL,
    RESULT_PASS,
    TOTAL_UID,
    GatewayResultEvaluator,
    compute_coverage,
    expected_for_gateway,
)
from core.config import GatewayDevice
from storage.schema import GATEWAY_RESULT_COLUMNS

GW1 = "04F9199E139C"
GW2 = "B806199E139C"
GW3 = "2007199E139C"
NODE_A = "1122334455667788"
NODE_B = "AABBCCDDEEFF0011"


# --------------------------------------------------------------------------
# Trợ giúp
# --------------------------------------------------------------------------
def config(**overrides) -> SimpleNamespace:
    """Config tối thiểu — có `hasattr` nên `SimpleNamespace` là đủ."""
    return SimpleNamespace(**overrides)


def mqtt_row(gateways, node=NODE_A, packet_type="DATA") -> dict:
    """1 dòng `mqtt_data_*.csv` đã gộp, `gateways` phân cách bởi '|'."""
    return {
        "packet_type": packet_type,
        "node_uid": node,
        "gateways": "|".join(gateways),
        "gateway_uid": gateways[0] if gateways else "",
        "payload_json": "",
    }


def ping_row(uid, total=100, rate=100.0, downtime_seconds=0.0, downtime_events=0,
             ip="10.0.0.1") -> dict:
    return {
        "gateway_uid": uid,
        "gateway_name": "GW " + uid,
        "ip_address": ip,
        "ping_total": total,
        "success_rate_percent": rate,
        "downtime_events": downtime_events,
        "downtime_seconds": downtime_seconds,
    }


def context(uid, **overrides) -> GatewayCorrelation:
    base = dict(
        gateway_uid=uid,
        gateway_name="GW " + uid,
        ip_address="10.0.0.1",
        windows_total=0,
        mqtt_packets=0,
        verdict=VERDICT_NO_DATA,
    )
    base.update(overrides)
    return GatewayCorrelation(**base)


def by_uid(rows) -> dict:
    return {row["gateway_uid"]: row for row in rows}


def one(rows) -> dict:
    """Dòng gateway duy nhất (bỏ dòng TỔNG)."""
    found = [row for row in rows if row["gateway_uid"] != TOTAL_UID]
    assert len(found) == 1
    return found[0]


# --------------------------------------------------------------------------
# compute_coverage / expected_for_gateway
# --------------------------------------------------------------------------
def test_compute_coverage_counts_packets_per_gateway_and_node():
    rows = [
        mqtt_row([GW1, GW2], NODE_A),
        mqtt_row([GW1], NODE_A),
        mqtt_row([GW1], NODE_B),
    ]

    heard, reference = compute_coverage(rows)

    assert heard == {GW1: {NODE_A: 2, NODE_B: 1}, GW2: {NODE_A: 1}}
    assert reference == {NODE_A: 2, NODE_B: 1}


def test_compute_coverage_ignores_non_data_packet_types():
    """LOG/ERROR không phải bản tin node phát nên không được tính vào độ phủ."""
    rows = [
        mqtt_row([GW1], NODE_A, packet_type="DATA"),
        mqtt_row([GW1], NODE_A, packet_type="LOG"),
        mqtt_row([GW1], NODE_A, packet_type="ERROR"),
    ]

    heard, reference = compute_coverage(rows)

    assert heard == {GW1: {NODE_A: 1}}
    assert reference == {NODE_A: 1}


def test_compute_coverage_normalizes_node_uid_case_and_skips_blank():
    rows = [
        mqtt_row([GW1], node=NODE_A.lower()),
        mqtt_row([GW1], node=""),
    ]

    heard, reference = compute_coverage(rows)

    assert reference == {NODE_A: 1}
    assert heard == {GW1: {NODE_A: 1}}


def test_compute_coverage_normalizes_11_hex_gateway_to_12_hex():
    """`8F9199E139C` (firmware in) và `08F9199E139C` (devices.json) là MỘT gateway."""
    heard, _reference = compute_coverage(
        [mqtt_row(["8F9199E139C"]), mqtt_row(["08F9199E139C"])]
    )

    assert list(heard) == ["08F9199E139C"]
    assert heard["08F9199E139C"] == {NODE_A: 2}


def test_expected_for_gateway_only_counts_nodes_it_heard():
    """Gateway không nghe node nào đó thì node đó KHÔNG vào mẫu số của nó."""
    heard, reference = compute_coverage(
        [
            mqtt_row([GW1], NODE_A),
            mqtt_row([GW1, GW2], NODE_A),
            mqtt_row([GW2], NODE_B),  # GW1 không nghe NODE_B
        ]
    )

    assert heard == {GW1: {NODE_A: 2}, GW2: {NODE_A: 1, NODE_B: 1}}
    assert reference == {NODE_A: 2, NODE_B: 1}
    assert expected_for_gateway(heard[GW1], reference) == 2   # chỉ NODE_A
    assert expected_for_gateway(heard[GW2], reference) == 3   # NODE_A + NODE_B
    assert expected_for_gateway({}, reference) == 0


def test_distant_gateway_is_not_penalized_for_inaudible_node():
    """GW1 không nghe NODE_B (ngoài tầm) → độ phủ vẫn 100%, không bị trừ oan."""
    evaluator = GatewayResultEvaluator(config())

    rows = evaluator.evaluate(
        mqtt_rows=[
            mqtt_row([GW1, GW2], NODE_A),
            mqtt_row([GW2], NODE_B),
            mqtt_row([GW2], NODE_B),
        ],
        correlation_gateways=[context(GW1), context(GW2)],
    )

    assert one([r for r in rows if r["gateway_uid"] == GW1])["coverage_percent"] == 100.0
    assert one([r for r in rows if r["gateway_uid"] == GW2])["coverage_percent"] == 100.0


# --------------------------------------------------------------------------
# Tiêu chí "Kết nối MQTT" / "Ổn định MQTT" ĐÃ BỎ khỏi kết luận
# --------------------------------------------------------------------------
def test_mqtt_link_and_stability_columns_are_reference_only():
    """`verdict` / `windows_*` / `silence_*` vẫn xuất ra nhưng KHÔNG chấm điểm.

    Dữ liệu thật: cùng một gateway ra `PING_BLOCKED` ở phiên này và `PARTIAL` ở
    phiên khác dù hành vi gửi y hệt → chấm theo `verdict` là trượt oan.
    """
    evaluator = GatewayResultEvaluator(config())

    rows = evaluator.evaluate(
        mqtt_rows=[mqtt_row([GW1]) for _ in range(100)],
        correlation_gateways=[
            context(
                GW1,
                mqtt_packets=620678,
                windows_total=3821,
                windows_ping_blocked=3821,
                verdict=VERDICT_PING_BLOCKED,
            )
        ],
    )

    row = one(rows)
    assert row["verdict"] == VERDICT_PING_BLOCKED     # cột tham khảo vẫn có
    assert row["windows_total"] == 3821
    assert row["silence_windows"] == 0
    assert row["silence_percent"] == 0.0
    assert "c_link" not in row                        # nhưng không còn tiêu chí
    assert "c_stable" not in row
    assert row["c_valid"] == PASS
    assert row["c_coverage"] == PASS
    assert row["c_ping"] == NA                        # ping bị chặn → không đo được
    assert row["result"] == RESULT_PASS


def test_verdict_and_silence_never_change_the_result():
    """Mọi nhãn `verdict` và mọi mức im lặng đều cho CÙNG một kết luận.

    Đây là bất biến quan trọng nhất sau khi bỏ 2 tiêu chí: không còn đường nào
    để kết quả đổi chỉ vì dữ liệu tương quan ping↔MQTT.
    """
    evaluator = GatewayResultEvaluator(config())
    cases = [
        (VERDICT_OK, 0, 100),
        (VERDICT_GATEWAY_SILENT, 98, 100),   # im lặng 98% → vẫn phải ĐẠT
        (VERDICT_LINK_DOWN, 100, 100),
        (VERDICT_PARTIAL, 60, 100),
        (VERDICT_PING_BLOCKED, 0, 100),
        (VERDICT_NO_DATA, 0, 0),
    ]
    for verdict, silent, windows in cases:
        rows = evaluator.evaluate(
            mqtt_rows=[mqtt_row([GW1]) for _ in range(100)],
            correlation_gateways=[
                context(GW1, verdict=verdict, windows_total=windows,
                        windows_ping_ok_mqtt_silent=silent)
            ],
        )
        row = one(rows)
        assert row["criteria_failed"] == 0, (verdict, row["result_reason"])
        assert row["result"] == RESULT_PASS, (verdict, row["result_reason"])


def test_no_correlation_row_still_scores_from_mqtt_and_ping():
    """Thiếu `correlation_summary` vẫn chấm được từ chính dữ liệu MQTT + ping."""
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(
        mqtt_rows=[mqtt_row([GW1])],
        ping_rows=[ping_row(GW1)],
    )

    row = one(rows)
    assert row["verdict"] == ""          # cột tham khảo để trống
    assert row["c_valid"] == PASS        # từ mqtt_data_*.csv
    assert row["c_ping"] == PASS         # từ ping_summary.csv (100 mẫu, 100 %)
    assert row["c_coverage"] == NA       # mẫu < min_packets_for_rate
    assert row["result"] == RESULT_PASS


# --------------------------------------------------------------------------
# Tiêu chí 1 — Có dữ liệu
# --------------------------------------------------------------------------
def test_gateway_without_any_packet_fails_valid_criterion():
    evaluator = GatewayResultEvaluator(config())

    rows = evaluator.evaluate(
        correlation_gateways=[context(GW1, windows_total=50, verdict=VERDICT_OK)]
    )

    row = one(rows)
    assert row["c_valid"] == FAIL
    assert row["packets_published"] == 0
    assert row["packets_expected"] == ""     # để trống, KHÔNG phải 0
    assert row["coverage_percent"] == ""     # không chia cho 0
    assert row["result"] == RESULT_FAIL
    assert "Có dữ liệu" in row["result_reason"]


def test_configured_but_silent_device_still_appears_and_fails():
    """Gateway có trong `devices.json` mà 'câm' vẫn phải hiện thành 1 dòng trượt."""
    evaluator = GatewayResultEvaluator(config())

    rows = evaluator.evaluate(devices=[GatewayDevice(uid=GW3, name="GW ba")])

    row = one(rows)
    assert row["gateway_uid"] == GW3
    assert row["gateway_name"] == "GW ba"
    assert row["result"] == RESULT_FAIL
    assert row["c_valid"] == FAIL


# --------------------------------------------------------------------------
# Tiêu chí 2 — Độ phủ bản tin
# --------------------------------------------------------------------------
def test_coverage_below_threshold_fails():
    evaluator = GatewayResultEvaluator(config())
    # GW1 nghe 75/100 bản tin → hụt 25% > 20% cho phép.
    mqtt = [mqtt_row([GW1, GW2]) for _ in range(75)]
    mqtt += [mqtt_row([GW2]) for _ in range(25)]

    rows = evaluator.evaluate(mqtt_rows=mqtt, correlation_gateways=[context(GW1)])

    row = by_uid(rows)[GW1]
    assert row["coverage_percent"] == 75.0
    assert row["missed_catch_percent"] == 25.0
    assert row["c_coverage"] == FAIL
    assert row["result"] == RESULT_FAIL


def test_coverage_at_exact_threshold_passes():
    """Biên: độ phủ ĐÚNG 80% (ngưỡng) phải ĐẠT."""
    evaluator = GatewayResultEvaluator(config())
    mqtt = [mqtt_row([GW1, GW2]) for _ in range(80)]
    mqtt += [mqtt_row([GW2]) for _ in range(20)]

    rows = evaluator.evaluate(mqtt_rows=mqtt, correlation_gateways=[context(GW1)])

    row = by_uid(rows)[GW1]
    assert row["coverage_percent"] == 80.0
    assert row["missed_catch_percent"] == 20.0
    assert row["c_coverage"] == PASS


def test_small_sample_yields_not_applicable_instead_of_pass():
    """10 bản tin không đủ để kết luận 'tốt' → N/A, không phải PASS."""
    evaluator = GatewayResultEvaluator(config())
    mqtt = [mqtt_row([GW1, GW2]) for _ in range(10)]

    rows = evaluator.evaluate(mqtt_rows=mqtt, correlation_gateways=[context(GW1)])

    assert by_uid(rows)[GW1]["c_coverage"] == NA


def test_guard_threshold_is_configurable():
    evaluator = GatewayResultEvaluator(config(min_packets_for_rate=5))
    mqtt = [mqtt_row([GW1, GW2]) for _ in range(10)]

    rows = evaluator.evaluate(mqtt_rows=mqtt, correlation_gateways=[context(GW1)])

    assert by_uid(rows)[GW1]["c_coverage"] == PASS


def test_criteria_prefixed_keys_take_precedence():
    """Khoá `criteria_<tên>` trong `config.json` thắng giá trị mặc định."""
    evaluator = GatewayResultEvaluator(config(criteria_min_coverage_percent=50.0))
    # Hụt 40% > 20% (mặc định 80%) nhưng ≤ 50% (ghi đè) → ĐẠT.
    mqtt = [mqtt_row([GW1, GW2]) for _ in range(60)]
    mqtt += [mqtt_row([GW2]) for _ in range(40)]

    rows = evaluator.evaluate(mqtt_rows=mqtt, correlation_gateways=[context(GW1)])

    assert by_uid(rows)[GW1]["missed_catch_percent"] == 40.0
    assert by_uid(rows)[GW1]["c_coverage"] == PASS


# --------------------------------------------------------------------------
# Tiêu chí 3 — Tỉ lệ ping
# --------------------------------------------------------------------------
def test_ping_disabled_is_not_applicable_not_failed():
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(
        mqtt_rows=[mqtt_row([GW1])],
        correlation_gateways=[context(GW1, verdict=VERDICT_OK, windows_total=10)],
    )

    row = one(rows)
    assert row["ping_total"] == 0
    assert row["c_ping"] == NA
    assert row["result"] == RESULT_PASS


def test_low_ping_rate_fails():
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(
        ping_rows=[ping_row(GW1, total=100, rate=80.0)],
        correlation_gateways=[context(GW1, verdict=VERDICT_OK, windows_total=10)],
    )

    row = one(rows)
    assert row["c_ping"] == FAIL
    assert "Tỉ lệ ping" in row["result_reason"]
    assert "80.00" in row["result_reason"]


def test_ping_rate_at_exact_threshold_passes():
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(
        ping_rows=[ping_row(GW1, total=100, rate=90.0)],
        correlation_gateways=[context(GW1, verdict=VERDICT_OK, windows_total=10)],
    )
    assert one(rows)["c_ping"] == PASS


def test_downtime_no_longer_affects_verdict():
    """Tiêu chí downtime ĐÃ BỎ: downtime lớn KHÔNG làm gateway trượt.

    Cột `downtime_seconds` vẫn được xuất ra CSV để tham khảo, nhưng không còn
    là tiêu chí chấm điểm (xem docstring `analytics/gateway_result.py`).
    """
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(
        mqtt_rows=[mqtt_row([GW1]) for _ in range(100)],
        ping_rows=[ping_row(GW1, downtime_seconds=9999.0, downtime_events=50)],
        correlation_gateways=[context(GW1, verdict=VERDICT_OK, windows_total=10)],
    )

    row = one(rows)
    assert row["downtime_seconds"] == 9999.0     # vẫn xuất ra để tham khảo
    assert "c_downtime" not in row
    assert row["result"] == RESULT_PASS
    assert "Thời gian mất kết nối" not in row["result_reason"]


def test_small_ping_sample_is_not_applicable():
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(
        ping_rows=[ping_row(GW1, total=3, rate=0.0)],
        correlation_gateways=[context(GW1, verdict=VERDICT_OK, windows_total=10)],
    )
    assert one(rows)["c_ping"] == NA


# --------------------------------------------------------------------------
# Tiêu chí 4 — Bắn hụt (chỉ có khi bật node trọng tâm)
# --------------------------------------------------------------------------
def test_late_criterion_is_not_applicable_without_focused_node():
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(
        mqtt_rows=[mqtt_row([GW1])],
        correlation_gateways=[context(GW1, verdict=VERDICT_OK, windows_total=10)],
    )
    assert one(rows)["c_late"] == NA


def test_late_criterion_uses_focused_node_summary_and_ignores_total_row():
    evaluator = GatewayResultEvaluator(config())

    rows = evaluator.evaluate(
        mqtt_rows=[mqtt_row([GW1])],
        correlation_gateways=[context(GW1, verdict=VERDICT_OK, windows_total=10)],
        focused_rows=[
            {"gateway_uid": "ALL", "late_percent": 99.0},   # dòng TỔNG phải bỏ qua
            {"gateway_uid": GW1, "late_percent": 6.0},
        ],
    )

    row = one(rows)
    assert row["late_percent"] == 6.0
    assert row["c_late"] == FAIL
    assert "Bắn hụt" in row["result_reason"]


def test_late_criterion_at_exact_threshold_passes():
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(
        mqtt_rows=[mqtt_row([GW1])],
        correlation_gateways=[context(GW1, verdict=VERDICT_OK, windows_total=10)],
        focused_rows=[{"gateway_uid": GW1, "late_percent": 5.0}],
    )
    assert one(rows)["c_late"] == PASS


def test_empty_late_value_falls_back_to_not_applicable():
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(
        mqtt_rows=[mqtt_row([GW1])],
        correlation_gateways=[context(GW1, verdict=VERDICT_OK, windows_total=10)],
        focused_rows=[{"gateway_uid": GW1, "late_percent": ""}],
    )
    assert one(rows)["c_late"] == NA


# --------------------------------------------------------------------------
# Dòng TỔNG
# --------------------------------------------------------------------------
def test_total_row_is_last_and_covers_every_gateway():
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(
        mqtt_rows=[mqtt_row([GW1]), mqtt_row([GW2])],
        correlation_gateways=[context(GW1), context(GW2)],
    )

    assert rows[-1]["gateway_uid"] == TOTAL_UID
    assert rows[-1]["gateway_name"] == "2 gateway"


def test_total_fails_when_any_gateway_fails():
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(
        mqtt_rows=[mqtt_row([GW1, GW2]) for _ in range(100)],
        ping_rows=[ping_row(GW1), ping_row(GW2, total=100, rate=10.0)],
        correlation_gateways=[context(GW1), context(GW2)],
    )

    total = by_uid(rows)[TOTAL_UID]
    assert total["result"] == RESULT_FAIL
    assert GW2 in total["result_reason"]
    assert total["c_ping"] == FAIL        # có gateway trượt → tiêu chí chung FAIL


def test_total_passes_only_when_all_gateways_pass():
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(
        mqtt_rows=[mqtt_row([GW1, GW2]) for _ in range(100)],
        ping_rows=[ping_row(GW1), ping_row(GW2)],
        correlation_gateways=[
            context(GW1, verdict=VERDICT_OK, windows_total=100),
            context(GW2, verdict=VERDICT_OK, windows_total=100),
        ],
    )

    total = by_uid(rows)[TOTAL_UID]
    assert total["result"] == RESULT_PASS
    assert total["criteria_failed"] == 0
    assert "đều đạt" in total["result_reason"]


def test_total_ping_rate_is_weighted_by_sample_size():
    """Bình quân GIA QUYỀN: gateway ping 10 lần không được nặng bằng 90 lần."""
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(
        mqtt_rows=[mqtt_row([GW1, GW2])],
        ping_rows=[
            ping_row(GW1, total=10, rate=50.0),
            ping_row(GW2, total=90, rate=100.0),
        ],
        correlation_gateways=[context(GW1), context(GW2)],
    )

    # Bình quân cộng sẽ là 75.0 — con số SAI.
    assert by_uid(rows)[TOTAL_UID]["ping_success_rate_percent"] == 95.0


def test_total_late_rate_is_weighted_by_published_packets():
    """Bình quân GIA QUYỀN theo số bản tin: 10 bản tin không nặng bằng 990."""
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(
        mqtt_rows=(
            [mqtt_row([GW1]) for _ in range(980)]          # GW1: 990 bản tin
            + [mqtt_row([GW1, GW2]) for _ in range(10)]    # GW2: 10 bản tin
        ),
        correlation_gateways=[context(GW1), context(GW2)],
        focused_rows=[
            {"gateway_uid": GW1, "late_percent": 0.0},
            {"gateway_uid": GW2, "late_percent": 100.0},
        ],
    )

    total = by_uid(rows)[TOTAL_UID]
    assert total["late_percent"] == 1.0    # (0×990 + 100×10) / 1000
    # Bình quân cộng sẽ là 50.0 — con số SAI.
    assert total["late_percent"] != 50.0


def test_total_skips_expected_count_and_sums_the_rest():
    """Mỗi gateway có mẫu số riêng nên dòng TỔNG không được cộng `expected`."""
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(
        mqtt_rows=[
            mqtt_row([GW1, GW2], NODE_A),
            mqtt_row([GW2], NODE_B),
        ],
        ping_rows=[ping_row(GW1, total=10, downtime_seconds=2.0)],
        correlation_gateways=[context(GW1), context(GW2)],
    )

    total = by_uid(rows)[TOTAL_UID]
    assert total["packets_expected"] == ""
    assert total["packets_published"] == 3
    assert total["ping_total"] == 10
    assert total["downtime_seconds"] == 2.0


def test_total_verdict_lists_unique_gateway_verdicts():
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(
        mqtt_rows=[mqtt_row([GW1, GW2]), mqtt_row([GW3])],
        correlation_gateways=[
            context(GW1, verdict=VERDICT_OK),
            context(GW2, verdict=VERDICT_OK),
            context(GW3, verdict=VERDICT_PING_BLOCKED),
        ],
    )

    assert by_uid(rows)[TOTAL_UID]["verdict"] == "OK|PING_BLOCKED"


def test_empty_input_yields_one_failing_total_row():
    rows = GatewayResultEvaluator(config()).evaluate()

    assert [row["gateway_uid"] for row in rows] == [TOTAL_UID]
    assert rows[0]["result"] == RESULT_FAIL
    assert "Không có gateway nào" in rows[0]["result_reason"]


def test_total_reason_shortens_long_gateway_list():
    uids = ["%012X" % (0x1000 + index) for index in range(7)]
    evaluator = GatewayResultEvaluator(config())

    rows = evaluator.evaluate(devices=[GatewayDevice(uid=uid) for uid in uids])

    reason = by_uid(rows)[TOTAL_UID]["result_reason"]
    assert reason.startswith("7/7 gateway KHÔNG ĐẠT")
    assert reason.endswith("…")
    assert reason.count(",") == 5      # 5 UID đầu + "…"


# --------------------------------------------------------------------------
# Hợp đồng với schema CSV
# --------------------------------------------------------------------------
def test_row_keys_match_schema_exactly_and_in_order():
    """Thứ tự cột phải khớp `GATEWAY_RESULT_COLUMNS`, nếu không CSV lệch cột."""
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(
        mqtt_rows=[mqtt_row([GW1])],
        ping_rows=[ping_row(GW1)],
        correlation_gateways=[context(GW1, verdict=VERDICT_OK, windows_total=10)],
        focused_rows=[{"gateway_uid": GW1, "late_percent": 0.0}],
    )

    for row in rows:
        assert list(row.keys()) == GATEWAY_RESULT_COLUMNS


def test_criteria_columns_are_exactly_the_declared_criteria():
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(devices=[GatewayDevice(uid=GW1)])

    row = one(rows)
    keys = [key for key, _label in CRITERIA]
    assert len(keys) == 4
    assert all(row[key] in (PASS, FAIL, NA) for key in keys)
    assert row["criteria_passed"] + row["criteria_failed"] + row["criteria_na"] == 4


def test_counts_and_reason_agree_with_failed_keys():
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(
        ping_rows=[ping_row(GW1, total=100, rate=10.0, downtime_seconds=999.0)]
    )

    row = one(rows)
    assert row["c_valid"] == FAIL
    assert row["c_ping"] == FAIL
    assert row["criteria_failed"] == 2          # valid + ping (downtime không chấm)
    assert row["result"] == RESULT_FAIL


def test_result_is_pass_only_when_no_criterion_fails():
    evaluator = GatewayResultEvaluator(config())
    rows = evaluator.evaluate(mqtt_rows=[mqtt_row([GW1])])

    row = one(rows)
    assert row["criteria_failed"] == 0
    assert row["result"] == RESULT_PASS
    assert "tiêu chí đạt" in row["result_reason"]
