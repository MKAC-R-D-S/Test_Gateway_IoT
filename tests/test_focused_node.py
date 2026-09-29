"""Kiểm thử `FocusedNodeTracker` — theo dõi 1 node trên nhiều gateway.

Định nghĩa hai khái niệm đang được kiểm (xem `analytics/focused_node.py`):

  * **BẮT HỤT** (`missed_catch`): tập tham chiếu = hợp mọi `seq` mà BẤT KỲ gateway
    nào đã publish cho node này (bằng chứng node đã phát LoRa). Một gateway bị
    coi là bắt hụt những `seq` thuộc tập tham chiếu mà nó không publish.
  * **BẮN HỤT** (`late_packets`): gateway CÓ publish nhưng
    `received_at − gateway_timestamp > late_seconds` (buffer thẻ SD / backfill).

Các phép kiểm dưới đây bám đúng những ranh giới dễ sai: hợp tham chiếu giữa nhiều
gateway, ngưỡng trễ ở ĐÚNG biên, xoá số liệu khi đổi node, và việc gộp khoảng
`seq` bị hụt thành từng đoạn liên tiếp.
"""

import json

import pytest

from analytics.focused_node import (
    DEFAULT_LATE_SECONDS,
    FocusedNodeTracker,
    normalize_focus_node,
)
from core.models import PKT_DATA, MQTTRecord

NODE = "0011223344556677"
OTHER_NODE = "AABBCCDDEEFF0011"
GW_A = "AAA"
GW_B = "BBB"
GW_C = "CCC"


def record(gateway: str, seq: int, received_at: str, gateway_timestamp: str,
           node_uid: str = NODE, packet_type: str = PKT_DATA) -> MQTTRecord:
    """1 bản copy MQTT tối thiểu, đủ trường cho `FocusedNodeTracker.feed()`."""
    return MQTTRecord(
        received_at=received_at,
        packet_type=packet_type,
        gateway_uid=gateway,
        gateway_name="GW " + gateway,
        node_uid=node_uid,
        seq=seq,
        gateway_timestamp=gateway_timestamp,
    )


def stamp(second: int) -> str:
    """Mốc ISO trong cùng một ngày, `second` tính bằng giây kể từ 10:00:00."""
    minute, sec = divmod(second, 60)
    return "2026-09-22T10:%02d:%02d+07:00" % (minute, sec)


def tracker(late_seconds: float = DEFAULT_LATE_SECONDS, seq_max: int = 65535,
            detail_limit: int = 200) -> FocusedNodeTracker:
    node = FocusedNodeTracker(seq_max=seq_max, late_seconds=late_seconds,
                              detail_limit=detail_limit)
    assert node.set_node(NODE) is True
    return node


def by_gateway(rows) -> dict:
    """Bỏ dòng TỔNG ('ALL') để tra cứu theo gateway."""
    return {row["gateway_uid"]: row for row in rows if row["gateway_uid"] != "ALL"}


# --------------------------------------------------------------------------
# normalize_focus_node
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    (NODE, NODE),
    (" 0011223344556677 ", NODE),        # khoảng trắng hai đầu
    ("0011223344556677".lower(), NODE),  # chuẩn hoá hoa
    ("343947AABBCC", "343947AABBCC"),    # node cũ 24 hex? -> 12 hex vẫn hợp lệ
    ("", ""),                            # tắt theo dõi
    ("zzz", ""),                         # không phải hex
    ("ABC", ""),                         # độ dài lẻ -> không hợp lệ cho node
])
def test_normalize_focus_node(text, expected):
    assert normalize_focus_node(text) == expected


# --------------------------------------------------------------------------
# Tập tham chiếu + bắt hụt
# --------------------------------------------------------------------------
def test_reference_is_union_across_gateways_and_missed_is_difference():
    node = tracker()
    # GW_A nghe đủ 1..5; GW_B hụt 3 và 4.
    for seq in range(1, 6):
        node.feed(record(GW_A, seq, stamp(seq), stamp(seq)))
        if seq not in (3, 4):
            node.feed(record(GW_B, seq, stamp(seq), stamp(seq)))

    rows = by_gateway(node.summary_rows())
    assert rows[GW_A]["reference_packets"] == 5
    assert rows[GW_A]["packets_heard"] == 5
    assert rows[GW_A]["missed_catch"] == 0
    assert rows[GW_A]["missed_catch_percent"] == 0.0

    assert rows[GW_B]["reference_packets"] == 5
    assert rows[GW_B]["packets_heard"] == 3
    assert rows[GW_B]["missed_catch"] == 2
    assert rows[GW_B]["missed_catch_percent"] == 40.0


def test_gateway_publishing_nothing_reports_100_percent():
    node = tracker()
    node.feed(record(GW_A, 1, stamp(0), stamp(0)))
    node.feed(record(GW_B, 1, stamp(0), stamp(0)))
    # GW_C chỉ xuất hiện ở bản tin thứ hai, nhưng hụt bản tin đầu.
    node.feed(record("CCC", 2, stamp(2), stamp(2)))

    rows = by_gateway(node.summary_rows())
    assert rows["CCC"]["packets_heard"] == 1
    assert rows["CCC"]["missed_catch"] == 1
    assert rows["CCC"]["notes"].startswith("Bắt hụt")


def test_duplicate_copy_counts_once_for_coverage_but_twice_for_copies():
    node = tracker()
    node.feed(record(GW_A, 7, stamp(0), stamp(0)))
    node.feed(record(GW_A, 7, stamp(1), stamp(1)))

    state = node._gateways[GW_A]  # noqa: SLF001 - kiểm chi tiết nội bộ
    assert state.packets_heard == 1     # phủ sóng chỉ đếm 1 lần
    assert state.copies == 2            # số bản copy vẫn đếm đủ
    assert node.stats()["reference_packets"] == 1


# --------------------------------------------------------------------------
# Bắn hụt — ranh giới ngưỡng
# --------------------------------------------------------------------------
def test_late_threshold_is_strictly_greater_than_limit():
    node = tracker(late_seconds=30.0)
    node.feed(record(GW_A, 1, stamp(30), stamp(0)))   # đúng 30 s -> KHÔNG trễ
    assert node._gateways[GW_A].late_packets == 0    # noqa: SLF001

    node.feed(record(GW_A, 2, stamp(31), stamp(0)))   # 30 s + 1 giây -> trễ
    assert node._gateways[GW_A].late_packets == 1     # noqa: SLF001
    assert node.stats()["late_packets"] == 1


def test_delay_is_measured_per_copy_not_per_seq():
    """Độ trễ đo trên MỌI bản copy: đúng bằng "số lần publish bị trễ".

    Nhờ vậy `late_percent` (tử = `late_packets`) cùng mẫu số `copies` → không
    bị lệch khi gateway publish lặp cùng một bản tin.
    """
    node = tracker(late_seconds=30.0)
    node.feed(record(GW_A, 1, stamp(100), stamp(0)))    # trễ 100 s
    node.feed(record(GW_A, 1, stamp(101), stamp(0)))    # bản copy lại, tính tiếp

    state = node._gateways[GW_A]                        # noqa: SLF001
    assert state.packets_heard == 1                     # phủ sóng chỉ đếm 1 lần
    assert state.copies == 2
    assert state.late_packets == 2
    assert state.delay_count == 2
    assert state.delay_max == 101.0


def test_late_percent_uses_copies_not_unique_packets():
    node = tracker(late_seconds=30.0)
    node.feed(record(GW_A, 1, stamp(0), stamp(0)))      # đúng giờ
    node.feed(record(GW_A, 2, stamp(100), stamp(0)))    # trễ 100 s

    row = by_gateway(node.summary_rows())[GW_A]
    assert row["late_packets"] == 1
    assert row["copies"] == 2
    assert row["late_percent"] == 50.0
    assert row["delay_max_seconds"] == 100.0
    assert row["delay_min_seconds"] == 0.0


def test_negative_delay_is_kept_as_clock_skew():
    """Gateway nhanh hơn PC → delay âm; cố ý KHÔNG kẹp về 0."""
    node = tracker(late_seconds=30.0)
    node.feed(record(GW_A, 1, stamp(0), stamp(10)))   # gateway đi trước 10 s

    row = by_gateway(node.summary_rows())[GW_A]
    assert row["late_packets"] == 0
    assert row["delay_min_seconds"] == -10.0


def test_duplicate_copy_does_not_double_count_coverage():
    node = tracker(late_seconds=30.0)
    node.feed(record(GW_A, 1, stamp(0), stamp(0)))
    node.feed(record(GW_A, 1, stamp(100), stamp(0)))   # cùng seq -> không tăng phủ sóng

    state = node._gateways[GW_A]                       # noqa: SLF001
    assert state.packets_heard == 1
    assert state.copies == 2
    assert node.stats()["reference_packets"] == 1


def test_missing_timestamp_is_counted_separately_and_not_late():
    node = tracker(late_seconds=30.0)
    node.feed(record(GW_A, 1, stamp(0), ""))   # không có gateway_timestamp

    state = node._gateways[GW_A]              # noqa: SLF001
    assert state.packets_heard == 1
    assert state.delay_missing == 1
    assert state.late_packets == 0
    assert by_gateway(node.summary_rows())[GW_A]["delay_avg_seconds"] is None


# --------------------------------------------------------------------------
# Chi tiết bắt hụt theo khoảng SEQ
# --------------------------------------------------------------------------
def test_missing_rows_coalesce_consecutive_seq_into_ranges():
    node = tracker()
    for seq in range(1, 9):
        node.feed(record(GW_A, seq, stamp(seq), stamp(seq)))
        if seq not in (3, 4, 5, 8):
            node.feed(record(GW_B, seq, stamp(seq), stamp(seq)))

    rows = node.missing_rows()
    assert len(rows) == 2                      # 3-5 và 8 (không phải 4 dòng lẻ)
    assert (rows[0]["missing_from"], rows[0]["missing_to"], rows[0]["missing_count"]) == (3, 5, 3)
    assert (rows[1]["missing_from"], rows[1]["missing_to"], rows[1]["missing_count"]) == (8, 8, 1)
    assert rows[0]["gateway_uid"] == GW_B
    assert rows[0]["window_start"] == stamp(3)
    assert rows[0]["window_end"] == stamp(5)


def test_missing_rows_respect_limit():
    node = FocusedNodeTracker(seq_max=100)
    node.set_node(NODE)
    node.feed(record(GW_A, 1, stamp(0), stamp(0)))
    # Gateway thứ hai chỉ xuất hiện qua một bản tin rồi biến mất -> nhiều khoảng hụt.
    node.feed(record(GW_B, 2, stamp(2), stamp(2)))
    # 20 khoảng rời rạc mà GW_B không có.
    for seq in range(1, 42, 2):
        node.feed(record(GW_A, seq, stamp(seq), stamp(seq)))

    all_rows = node.missing_rows()
    assert len(all_rows) >= 10
    assert len(node.missing_rows(limit=3)) == 3


# --------------------------------------------------------------------------
# Kiểu ngắt quãng: hụt LIÊN TỤC (mất tầm sóng) vs hụt RẢI RÁC (mất gói RF)
# --------------------------------------------------------------------------
def test_gap_pattern_detects_one_continuous_outage():
    """Gateway mất sóng một mạch: 1 khoảng chiếm hết số hụt."""
    node = tracker()
    for seq in range(1, 601):
        node.feed(record(GW_A, seq, stamp(seq), stamp(seq)))
    for seq in list(range(1, 100)) + list(range(400, 601)):
        node.feed(record(GW_B, seq, stamp(seq), stamp(seq)))

    node.refresh_patterns()
    row = by_gateway(node.summary_rows())[GW_B]
    assert row["missed_catch"] == 300
    assert row["gap_runs"] == 1            # MỘT mạch, không phải 300 khoảng
    assert row["gap_largest"] == 300
    assert "LIÊN TỤC" in row["gap_pattern"]
    assert "LIÊN TỤC" in row["notes"]


def test_gap_pattern_detects_scattered_misses():
    """Cùng số seq hụt nhưng rải rác → phải phân loại KHÁC ca mất một mạch."""
    node = tracker()
    for seq in range(1, 601):
        node.feed(record(GW_A, seq, stamp(seq), stamp(seq)))
        if seq % 2 == 0:                   # GW_B hụt mọi seq lẻ
            node.feed(record(GW_B, seq, stamp(seq), stamp(seq)))

    node.refresh_patterns()
    row = by_gateway(node.summary_rows())[GW_B]
    assert row["missed_catch"] == 300      # BẰNG ca hụt một mạch ở trên
    assert row["gap_runs"] == 300          # nhưng 300 khoảng rời rạc
    assert row["gap_largest"] == 1
    assert "RẢI RÁC" in row["gap_pattern"]
    assert "mất gói RF" in row["notes"]


def test_gap_pattern_empty_when_gateway_misses_nothing():
    node = tracker()
    for seq in range(1, 21):
        node.feed(record(GW_A, seq, stamp(seq), stamp(seq)))
    node.refresh_patterns()
    row = by_gateway(node.summary_rows())[GW_A]
    assert row["gap_runs"] == 0
    assert row["gap_largest"] == 0
    assert row["gap_pattern"] == ""
    assert "Không bắt hụt" in row["notes"]


def test_gap_pattern_is_not_computed_until_refresh():
    """Không quét bitmap khi đọc bảng → tránh treo GUI (refresh thưa, có chủ đích)."""
    node = tracker()
    for seq in range(1, 51):
        node.feed(record(GW_A, seq, stamp(seq), stamp(seq)))
        if seq > 25:
            node.feed(record(GW_B, seq, stamp(seq), stamp(seq)))

    row = by_gateway(node.summary_rows())[GW_B]     # chưa refresh_patterns()
    assert row["gap_runs"] == 0                     # vẫn 0, KHÔNG tự quét
    assert row["missed_catch"] == 25                # nhưng số hụt vẫn đúng

    node.refresh_patterns()
    assert by_gateway(node.summary_rows())[GW_B]["gap_runs"] == 1


def test_gap_pattern_treats_late_joining_gateway_as_continuous():
    """Gateway chỉ vào tầm giữa phiên = hụt một mạch từ seq đầu tới lúc vào."""
    node = tracker()
    for seq in range(1, 201):
        node.feed(record(GW_A, seq, stamp(seq), stamp(seq)))
    for seq in range(150, 201):
        node.feed(record(GW_B, seq, stamp(seq), stamp(seq)))

    node.refresh_patterns()
    row = by_gateway(node.summary_rows())[GW_B]
    assert row["gap_runs"] == 1
    assert row["gap_largest"] == 149
    assert "LIÊN TỤC" in row["gap_pattern"]
    ranges = [r for r in node.missing_rows() if r["gateway_uid"] == GW_B]
    assert (ranges[0]["missing_from"], ranges[0]["missing_to"]) == (1, 149)


def test_overall_row_classifies_every_gateway_consistently():
    """Dòng TỔNG phải dùng CÙNG tiêu chí với từng gateway (lỗi cũ: lệch nhau)."""
    node = tracker()
    for seq in range(1, 41):
        node.feed(record(GW_A, seq, stamp(seq), stamp(seq)))
    # Cả GW_B lẫn GW_C đều hụt MỘT mạch, chỉ khác độ dài (< 50 seq).
    for seq in range(1, 21):
        node.feed(record(GW_B, seq, stamp(seq), stamp(seq)))
    for seq in range(1, 31):
        node.feed(record(GW_C, seq, stamp(seq), stamp(seq)))

    node.refresh_patterns()
    rows = by_gateway(node.summary_rows())
    assert "LIÊN TỤC" in rows[GW_B]["gap_pattern"]
    assert "LIÊN TỤC" in rows[GW_C]["gap_pattern"]
    overall = node.summary_rows()[0]
    # Trước đây dòng TỔNG đòi `gap_largest >= 50` nên báo sai "0 ... liên tục".
    assert "2 gateway có hụt liên tục" in overall["gap_pattern"]
    assert "0 gateway hụt rải rác" in overall["gap_pattern"]


def test_overall_row_summarises_contiguous_and_scattered_counts():
    node = tracker()
    for seq in range(1, 101):
        node.feed(record(GW_A, seq, stamp(seq), stamp(seq)))
    # GW_B: mất một mạch 51..100
    for seq in range(1, 51):
        node.feed(record(GW_B, seq, stamp(seq), stamp(seq)))
    # GW_C: hụt rải rác các seq chẵn
    for seq in range(1, 101):
        if seq % 2 == 1:
            node.feed(record(GW_C, seq, stamp(seq), stamp(seq)))

    node.refresh_patterns()
    overall = node.summary_rows()[0]
    assert overall["gateway_uid"] == "ALL"
    assert overall["gap_runs"] == 1 + 50           # 1 mạch + 50 khoảng lẻ
    assert overall["gap_largest"] == 50
    assert "1 gateway có hụt liên tục" in overall["gap_pattern"]
    assert "1 gateway hụt rải rác" in overall["gap_pattern"]


# --------------------------------------------------------------------------
# Chi tiết bắn hụt
# --------------------------------------------------------------------------
def test_late_rows_sorted_heaviest_first():
    node = tracker(late_seconds=10.0)
    node.feed(record(GW_A, 1, stamp(15), stamp(0)))    # trễ 15
    node.feed(record(GW_A, 2, stamp(99), stamp(0)))    # trễ 99
    node.feed(record(GW_B, 3, stamp(50), stamp(0)))    # trễ 50

    rows = node.late_rows()
    assert [r["delay_seconds"] for r in rows] == [99.0, 50.0, 15.0]
    assert node.stats()["late_packets"] == 3


def test_late_rows_keep_heaviest_when_detail_buffer_is_full():
    node = tracker(late_seconds=10.0, detail_limit=2)
    node.feed(record(GW_A, 1, stamp(11), stamp(0)))    # 11
    node.feed(record(GW_A, 2, stamp(12), stamp(0)))    # 12
    node.feed(record(GW_A, 3, stamp(500), stamp(0)))   # 500 -> phải đẩy 11 ra

    delays = [r["delay_seconds"] for r in node.late_rows()]
    assert delays == [500.0, 12.0]


# --------------------------------------------------------------------------
# Lọc bản tin không hợp lệ / không thuộc node
# --------------------------------------------------------------------------
@pytest.mark.parametrize("seq", [0, -1, 101])
def test_seq_out_of_range_is_ignored(seq):
    node = tracker(seq_max=100)
    assert node.feed(record(GW_A, seq, stamp(0), stamp(0))) is False
    assert node.stats()["gateways_heard"] == 0
    assert node.stats()["reference_packets"] == 0


def test_other_node_and_bad_seq_type_are_ignored():
    node = tracker()
    assert node.feed(record(GW_A, 1, stamp(0), stamp(0), node_uid=OTHER_NODE)) is False
    assert node.feed(record(GW_A, None, stamp(0), stamp(0))) is False
    assert node.stats()["reference_packets"] == 0


def test_feed_is_noop_when_no_node_selected():
    node = FocusedNodeTracker()
    assert node.active is False
    assert node.feed(record(GW_A, 1, stamp(0), stamp(0))) is False
    assert node.summary_lines() == []


def test_unknown_gateway_still_proves_the_node_transmitted():
    """Bản tin không có gateway_uid vẫn góp vào tập tham chiếu."""
    node = tracker()
    node.feed(record("", 1, stamp(0), stamp(0)))
    node.feed(record(GW_A, 2, stamp(1), stamp(1)))

    stats = node.stats()
    assert stats["reference_packets"] == 2
    assert stats["gateways_heard"] == 1        # không tạo gateway rỗng
    assert stats["total_copies"] == 2


# --------------------------------------------------------------------------
# set_node / reset
# --------------------------------------------------------------------------
def test_set_node_resets_state_and_reports_change():
    node = tracker()
    node.feed(record(GW_A, 1, stamp(0), stamp(0)))
    assert node.stats()["reference_packets"] == 1

    assert node.set_node(NODE) is False          # cùng node -> không đổi
    assert node.stats()["reference_packets"] == 1

    assert node.set_node(OTHER_NODE) is True     # đổi node -> xoá sạch
    stats = node.stats()
    assert stats["reference_packets"] == 0
    assert stats["gateways_heard"] == 0
    assert stats["last_seen"] == ""
    assert node.node_uid == OTHER_NODE


def test_set_node_empty_string_disables_tracking():
    node = tracker()
    node.feed(record(GW_A, 1, stamp(0), stamp(0)))

    assert node.set_node("") is True
    assert node.active is False
    assert node.feed(record(GW_A, 2, stamp(1), stamp(1))) is False
    assert node.stats()["reference_packets"] == 0


def test_reset_keeps_node_selection():
    node = tracker()
    node.feed(record(GW_A, 1, stamp(0), stamp(0)))
    node.reset()

    assert node.active is True
    assert node.node_uid == NODE
    assert node.stats()["reference_packets"] == 0


# --------------------------------------------------------------------------
# Dòng TỔNG + text cho console headless
# --------------------------------------------------------------------------
def test_overall_row_sums_missed_catch_across_gateways():
    node = tracker()
    for seq in range(1, 6):
        node.feed(record(GW_A, seq, stamp(seq), stamp(seq)))
        if seq not in (3, 4):
            node.feed(record(GW_B, seq, stamp(seq), stamp(seq)))

    overall = node.summary_rows()[0]
    assert overall["gateway_uid"] == "ALL"
    assert overall["missed_catch"] == 2                    # 0 (GW_A) + 2 (GW_B)
    assert overall["reference_packets"] == 5
    assert overall["gateways"] == 2
    assert overall["missed_catch_percent"] == 20.0         # 2 hụt / (5 × 2)
    # Notes phải là text đã nội suy — KHÔNG được lọt dấu `%` đôi của chuỗi format.
    assert "%%" not in overall["notes"]


def test_overall_percent_counts_silent_gateways_in_denominator():
    node = FocusedNodeTracker(seq_max=100)
    node.set_node(NODE)
    node.set_known_gateways([(GW_A, "GW A"), (GW_B, "GW B")])
    node.feed(record(GW_A, 1, stamp(0), stamp(0)))   # GW_B câm hoàn toàn

    overall = node.summary_rows()[0]
    assert overall["missed_catch"] == 1
    assert overall["gateways"] == 2
    assert overall["missed_catch_percent"] == 50.0   # 1 hụt / (1 bản tin × 2 gateway)

    notes = by_gateway(node.summary_rows())[GW_B]["notes"]
    assert "(bắt hụt 100%)" in notes                 # dấu % đơn, không phải '100%%'


def test_summary_lines_rank_gateways_and_warn_about_silent_ones():
    node = FocusedNodeTracker(seq_max=100)
    node.set_node(NODE)
    node.set_known_gateways([(GW_A, "GW A"), (GW_B, "GW B"), ("CCC", "GW C")])
    for seq in (1, 2):
        node.feed(record(GW_A, seq, stamp(seq), stamp(seq)))   # hụt 0
    node.feed(record(GW_B, 1, stamp(1), stamp(1)))             # hụt 1

    lines = node.summary_lines(top=5)
    assert lines[0].startswith("NODE TRỌNG TÂM %s" % NODE)
    assert "2/3" in lines[0]                                   # nghe 2 trên 3 đã biết
    assert "1 gateway KHÔNG publish" in lines[1]               # CCC câm
    # Gateway hụt nhiều nhất phải đứng trước: CCC (2) > BBB (1).
    # AAA không hụt / không trễ nên không xuất hiện trong bảng xếp hạng.
    ranked = [line.split()[0] for line in lines[3:5]]
    assert ranked == ["CCC", GW_B]


def test_summary_lines_empty_when_nothing_is_missed():
    node = tracker()
    node.feed(record(GW_A, 1, stamp(0), stamp(0)))
    node.feed(record(GW_B, 1, stamp(0), stamp(0)))

    lines = node.summary_lines()
    assert any("không gateway nào bắt hụt" in line for line in lines)


# --------------------------------------------------------------------------
# Gateway đã cấu hình nhưng "câm" (không publish bản tin nào của node)
# --------------------------------------------------------------------------
def test_configured_gateway_that_never_publishes_is_reported_as_100_percent():
    node = FocusedNodeTracker(seq_max=100)
    node.set_node(NODE)
    node.set_known_gateways([(GW_A, "GW A"), (GW_B, "GW B")])
    node.feed(record(GW_A, 1, stamp(0), stamp(0)))

    row = by_gateway(node.summary_rows())[GW_B]
    assert row["packets_heard"] == 0
    assert row["copies"] == 0
    assert row["missed_catch"] == 1
    assert row["missed_catch_percent"] == 100.0
    assert row["notes"].startswith("KHÔNG publish bản tin nào")

    stats = node.stats()
    assert stats["gateways_known"] == 2
    assert stats["gateways_heard"] == 1
    assert stats["gateways_silent"] == 1


def test_known_gateways_accept_devices_tuples_and_dicts():
    class Device:
        def __init__(self, uid, name):
            self.uid = uid
            self.name = name

    node = FocusedNodeTracker(seq_max=100)
    node.set_node(NODE)
    node.set_known_gateways([
        Device(GW_A, "GW A"),
        (GW_B, "GW B"),
        {"uid": "CCC", "name": "GW C"},
        {"uid": "not-hex", "name": "bỏ qua"},
    ])

    assert node.stats()["gateways_known"] == 3


def test_set_known_gateways_does_not_erase_collected_data():
    node = FocusedNodeTracker(seq_max=100)
    node.set_node(NODE)
    node.feed(record(GW_A, 1, stamp(0), stamp(0)))

    node.set_known_gateways([(GW_A, "GW A"), (GW_B, "GW B")])

    stats = node.stats()
    assert stats["reference_packets"] == 1
    assert stats["gateways_heard"] == 1
    assert stats["gateways_known"] == 2


def test_unknown_gateway_that_publishes_is_still_counted():
    """Gateway không có trong `devices.json` vẫn được tạo dòng khi nó publish."""
    node = FocusedNodeTracker(seq_max=100)
    node.set_node(NODE)
    node.set_known_gateways([(GW_A, "GW A")])
    node.feed(record("DDD", 1, stamp(0), stamp(0)))

    rows = by_gateway(node.summary_rows())
    assert set(rows) == {GW_A, "DDD"}
    assert rows["DDD"]["packets_heard"] == 1
    assert rows["DDD"]["missed_catch"] == 0
    assert rows[GW_A]["missed_catch"] == 1


# --------------------------------------------------------------------------
# Giới hạn số gateway + bộ nhớ
# --------------------------------------------------------------------------
def test_gateway_cap_stops_creating_new_states():
    node = FocusedNodeTracker(seq_max=100, late_seconds=5.0, max_gateways=2)
    node.set_node(NODE)
    for index in range(5):
        gateway = "%02XAA00" % index          # UID phải là hex mới hợp lệ
        assert node.feed(record(gateway, 1, stamp(0), stamp(0))) is True

    stats = node.stats()
    assert stats["gateways_known"] == 2            # chỉ 2 gateway đầu được tạo dòng
    assert stats["reference_packets"] == 1
    assert stats["total_copies"] == 5              # vẫn đếm đủ copy


def test_bitmap_memory_is_bounded_by_seq_max_not_by_session_length():
    node = tracker(seq_max=4096)
    for seq in range(1, 4097):
        node.feed(record(GW_A, seq, "%s" % stamp(seq % 600), "%s" % stamp(0)))

    assert node.stats()["reference_packets"] == 4096
    # 1 byte cho mỗi giá trị seq → kích thước cố định, không phụ thuộc độ dài phiên.
    assert len(node._ref) == 4097                  # noqa: SLF001
    assert len(node._gateways[GW_A].heard) == 4097  # noqa: SLF001


# --------------------------------------------------------------------------
# stats() đủ khoá cho PipelineStats / console
# --------------------------------------------------------------------------
def test_stats_shape_is_stable_for_console_and_gui():
    node = tracker()
    stats = node.stats()
    for key in ("node_uid", "active", "reference_packets", "gateways_heard",
                "gateways_known", "gateways_silent", "total_copies",
                "late_packets", "total_missing_catch", "heard_sum", "first_seen",
                "last_seen", "gateways"):
        assert key in stats, key
    assert stats["gateways"] == []

    # Mọi khoá trong `stats()` phải serialize được sang JSON (console headless).
    json.dumps(stats)
