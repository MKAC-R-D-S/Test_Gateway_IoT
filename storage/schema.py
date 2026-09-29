"""Định nghĩa cột CSV (thứ tự cố định) cho mọi file output của tool."""

from __future__ import annotations

from typing import List

# ---------------------------------------------------------------------------
# mqtt_data_<YYYYMMDD[_HH]>.csv — mọi bản tin MQTT nhận được (mọi loại)
#
# LƯU Ý: mỗi dòng là **một bản tin logic**, KHÔNG phải một lần publish.
# Một node phát 1 bản tin LoRa thì mọi gateway trong tầm đều publish lên MQTT;
# tool gộp các bản copy đó thành 1 dòng (xem `analytics/packet_deduper.py`).
# Vì vậy KHÔNG được lấy số dòng của file này làm số bản tin LoRa.
# ---------------------------------------------------------------------------
MQTT_DATA_COLUMNS: List[str] = [
    "received_at",        # giờ PC nhận bản copy sớm nhất (nguồn: tool)
    "received_last_at",   # giờ PC nhận bản copy muộn nhất trong cửa sổ gộp
    "mqtt_timestamp",     # giờ broker nếu có (firmware không gửi -> = received_at)
    "gateway_uid",        # gateway có RSSI TỐT NHẤT (không phải gateway duy nhất nghe)
    "gateway_name",       # tên cấu hình, hoặc UNKNOWN_GATEWAY
    "gateways_heard",     # SỐ gateway đã nghe bản tin này
    "gateways",           # danh sách gateway đã nghe, phân cách '|'
    "receiver_uid",       # = receiver_id (16 hex) của gateway tốt nhất
    "node_uid",           # = sensor_id (16 hoặc 24 hex)
    "node_format",        # NEW_NODE_MSG / NEW_NODE_OLD_MSG / OLD_NODE / UNKNOWN_FORMAT
    "topic",
    "packet_type",        # DATA / DATA_NO_SEQ / INFO / ERROR / LOG / UNKNOWN
    "seq",
    "rssi",               # RSSI tốt nhất trong cụm gateway
    "snr",
    "mcu_temp",
    "mcu_vdd",
    "sensor_count",
    "sensor_values",      # phẳng, phân cách '|'
    "gateway_timestamp",  # = payload["timestamp"]
    "status",             # log gateway: online/offline
    "error",              # node/error: CRC ERROR / ...
    "raw_hex",            # node/error: raw_data -> hex
    "valid",
    "validation_error",
    # --- kết quả phân tích SEQ ---
    "previous_seq",
    "expected_seq",
    "seq_delta",
    "seq_status",         # NORMAL/MISSING/DUPLICATE/OUT_OF_ORDER/RESET/FIRST
    "missing_count",
    "duplicate",
    "out_of_order",
    "reboot_detected",
    "anomaly",
    "payload_size",
    "payload_json",       # JSON gốc (forensic)
]

# ---------------------------------------------------------------------------
# mqtt_missing_<YYYYMMDD>.csv — khoảng thiếu SEQ (1 dòng / lần nhảy)
#
# LƯU Ý: 1 dòng = 1 khoảng thiếu của 1 NODE, không phải 1 báo cáo của 1 gateway.
# Khoảng thiếu được tính trên luồng bản tin ĐÃ GỘP nên nếu gateway B nghe được
# packet mà gateway A bỏ sót thì khoảng thiếu đó KHÔNG bị tính là mất.
# ---------------------------------------------------------------------------
MQTT_MISSING_COLUMNS: List[str] = [
    "timestamp",
    "gateway_uid",           # gateway tốt nhất trong cụm nghe được packet này
    "gateways_heard",
    "gateways",
    "receiver_uid",
    "node_uid",
    "previous_seq",
    "current_seq",
    "missing_from",
    "missing_to",
    "missing_count",
    "reason",
]

# ---------------------------------------------------------------------------
# mqtt_anomalies_<YYYYMMDD>.csv — DUPLICATE / OUT_OF_ORDER / SEQ_RESET / ...
# 1 dòng = 1 bất thường logic (đã gộp mọi gateway cùng nghe).
# ---------------------------------------------------------------------------
MQTT_ANOMALY_COLUMNS: List[str] = [
    "timestamp",
    "gateway_uid",           # gateway tốt nhất trong cụm nghe được packet này
    "gateways_heard",
    "gateways",
    "receiver_uid",
    "node_uid",
    "seq",
    "previous_seq",
    "anomaly_type",
    "details",
]

# ---------------------------------------------------------------------------
# mqtt_errors_<YYYYMMDD>.csv — node/error (KHÔNG tính vào packet hợp lệ)
#
# LƯU Ý: 1 dòng = 1 KHUNG LỖI, không phải 1 lần publish. Khung lỗi cũng được mọi
# gateway trong tầm publish lại; tool gộp theo (error, raw_hex) — xem
# `analytics/error_deduper.py`. Vì vậy số dòng nhỏ hơn số bản tin MQTT đã nhận.
# ---------------------------------------------------------------------------
MQTT_ERROR_COLUMNS: List[str] = [
    "received_at",         # lần publish sớm nhất
    "received_last_at",    # lần publish muộn nhất trong cửa sổ gộp
    "gateway_uid",         # gateway có RSSI tốt nhất
    "gateway_name",
    "gateways_heard",      # số gateway đã nghe khung lỗi này
    "gateways",
    "copies",              # số lần publish đã gộp (bằng số dòng trước khi gộp)
    "receiver_uid",
    "topic",
    "error",
    "gateway_timestamp",
    "rssi",
    "snr",
    "raw_hex",
    "payload_json",
]

# ---------------------------------------------------------------------------
# mqtt_gateway_log_<YYYYMMDD>.csv — <model>/<uuid>/log (60s report + Last Will)
# ---------------------------------------------------------------------------
MQTT_GATEWAY_LOG_COLUMNS: List[str] = [
    "received_at",
    "gateway_uid",
    "gateway_name",
    "topic",
    "status",
    "gateway_timestamp",
    "payload_json",
]

# ---------------------------------------------------------------------------
# ping_log_<YYYYMMDD>.csv
# ---------------------------------------------------------------------------
PING_LOG_COLUMNS: List[str] = [
    "timestamp",
    "gateway_uid",
    "gateway_name",
    "ip_address",
    "ping_success",
    "latency_ms",
    "timeout",
    "error",
]

# ---------------------------------------------------------------------------
# gateway_downtime.csv — 1 dòng / khoảng downtime (ONGOING khi chưa recover)
# ---------------------------------------------------------------------------
GATEWAY_DOWNTIME_COLUMNS: List[str] = [
    "gateway_uid",
    "gateway_name",
    "ip_address",
    "down_start",
    "down_end",
    "duration_seconds",
    "failed_ping_count",
    "status",
    "recovered",
    "last_down_ping",
]

# ---------------------------------------------------------------------------
# correlation_summary.csv — đối chiếu ping vs MQTT (1 dòng / gateway)
# ---------------------------------------------------------------------------
CORRELATION_COLUMNS: List[str] = [
    "gateway_uid",
    "gateway_name",
    "ip_address",
    "windows_total",
    "windows_ping_ok",
    "windows_ping_down",
    "windows_ping_down_mqtt_silent",
    "windows_ping_ok_mqtt_silent",
    "mqtt_packets",
    "verdict",
    "notes",
]

# ---------------------------------------------------------------------------
# gateway_result.csv — ĐẠT / KHÔNG ĐẠT cho TỪNG gateway theo TỪNG tiêu chí.
#
# 1 dòng / gateway + 1 dòng TỔNG ở cuối (`gateway_uid = "TỔNG"`).
#   `c_*`        : PASS / FAIL / N/A cho từng tiêu chí. `N/A` = không áp dụng
#                  được (gateway không bật ping, mẫu quá nhỏ, không bật node
#                  trọng tâm) — KHÁC với FAIL.
#   `result`     : ĐẠT khi MỌI tiêu chí áp dụng được đều PASS, ngược lại KHÔNG ĐẠT.
#   `result_reason`: nêu đúng tiêu chí không đạt kèm giá trị thực vs ngưỡng.
#
# LƯU Ý: chỉ còn 4 tiêu chí, tất cả đều là bằng chứng TRỰC TIẾP. Các cột
# `downtime_*`, `verdict`, `windows_*`, `silence_*` VẪN được xuất ra để CHẨN ĐOÁN
# (xem tab "MQTT ↔ Ping") nhưng KHÔNG vào kết luận ĐẠT/KHÔNG ĐẠT.
# `packets_published` / `coverage_percent` tính trên luồng bản tin ĐÃ GỘP nên độ
# phủ là **chặn trên** (xem `analytics/gateway_result.py`).
# ---------------------------------------------------------------------------
GATEWAY_RESULT_COLUMNS: List[str] = [
    "gateway_uid",
    "gateway_name",
    "ip_address",
    "mqtt_packets",              # bản tin gateway này đã publish (từ correlation)
    "packets_published",         # bản tin gateway này publish cho các node nó nghe
    "packets_expected",          # mẫu số: chỉ tính các node gateway này ĐÃ nghe
    "coverage_percent",          # độ phủ = published / expected
    "missed_catch_percent",      # 100 − độ phủ (bắt hụt)
    "ping_total",
    "ping_success_rate_percent",
    "downtime_events",            # tham khảo — KHÔNG vào kết luận ĐẠT/KHÔNG ĐẠT
    "downtime_seconds",           # tham khảo — KHÔNG vào kết luận ĐẠT/KHÔNG ĐẠT
    "windows_total",             # tham khảo — dựng bởi analytics/correlation.py
    "silence_windows",           # tham khảo — window MQTT im lặng
    "silence_percent",           # tham khảo
    "verdict",                   # tham khảo — kết luận MQTT↔ping (correlation_summary)
    "late_percent",              # bắn hụt — chỉ có khi bật node trọng tâm
    "c_valid",                   # 1. Có dữ liệu
    "c_coverage",                # 2. Độ phủ bản tin
    "c_ping",                    # 3. Tỉ lệ ping
    "c_late",                    # 4. Bắn hụt (gửi trễ)
    "criteria_passed",
    "criteria_failed",
    "criteria_na",
    "result",                    # ĐẠT / KHÔNG ĐẠT
    "result_reason",
]

# ---------------------------------------------------------------------------
# alerts_<YYYYMMDD>.csv — cảnh báo mức sự kiện (node silent, lỗi chất lượng...)
# ---------------------------------------------------------------------------
ALERT_COLUMNS: List[str] = [
    "timestamp",
    "level",
    "alert_type",
    "gateway_uid",
    "gateway_name",
    "receiver_uid",
    "node_uid",
    "message",
    "details",
    "value",
]

# ---------------------------------------------------------------------------
# mqtt_silence_<YYYYMMDD>.csv — các khoảng MQTT im lặng >= ngưỡng
# ---------------------------------------------------------------------------
SILENCE_COLUMNS: List[str] = [
    "gateway_uid",
    "kind",
    "start",
    "end",
    "duration_seconds",
    "windows",
    "ping_ok_windows",
    "ping_down_windows",
    "interpretation",
]

# ---------------------------------------------------------------------------
# run_summary.csv — tổng kết một lần chạy (1 dòng)
# ---------------------------------------------------------------------------
RUN_SUMMARY_COLUMNS: List[str] = [
    "run_id",
    "session_start",
    "session_end",
    "duration_seconds",
    "broker",
    "subscribed_topics",
    "total_messages",
    "valid_messages",
    "invalid_messages",
    "data_packets",
    "data_no_seq_packets",   # node cũ 22B (info frame không có SEQ)
    "info_packets",
    "error_packets",
    "log_messages",
    "unknown_gateway_messages",
    "gateways_monitored",
    "gateways_seen",
    "nodes_seen",
    "missing_packets",
    "duplicate_packets",
    "out_of_order_packets",
    "seq_reset_events",
    "seq_wrap_events",
    # Cảnh báo sức khoẻ pipeline: >0 nghĩa là bản tin đã nhận nhưng BỊ MẤT do
    # lỗi khi xử lý (không phải lỗi dữ liệu). Xem `Counters.as_summary_fields`.
    "parse_errors",
    "queue_dropped",
    "ping_total",
    "ping_failed",
    "downtime_events",
    "downtime_seconds",
    "reconnects",
    # --- kết luận ĐẠT / KHÔNG ĐẠT của cả phiên (`gateway_result.csv` dòng TỔNG) ---
    "gateways_passed",
    "gateways_failed",
    "result",
    "result_reason",
]

# ---------------------------------------------------------------------------
# node_summary.csv — tổng hợp per NODE (1 dòng / node)
#
# Khoá là `node_uid` — KHÔNG tách theo gateway: nhiều gateway cùng nghe một node
# nên nếu tách theo gateway thì 1 node thật bị nhân thành N dòng và `missing` bị
# cộng dồn sai (đo trên dữ liệu thật: 389 dòng thay vì 52, missing 13 644 thay
# vì 1 381).
# ---------------------------------------------------------------------------
NODE_SUMMARY_COLUMNS: List[str] = [
    "node_uid",
    "node_format",
    "gateways_heard",       # số gateway khác nhau đã từng nghe node
    "gateways_heard_avg",   # số gateway nghe trung bình trên mỗi bản tin
    "gateways_heard_max",   # số gateway nghe nhiều nhất cho 1 bản tin
    "packets",
    "first_seq",
    "last_seq",
    "missing",
    "duplicate",
    "out_of_order",
    "seq_reset",
    "seq_wrap",
    "delivery_rate_percent",
    "first_seen",
    "last_seen",
]

# ---------------------------------------------------------------------------
# ping_summary.csv — tổng hợp per gateway
# ---------------------------------------------------------------------------
PING_SUMMARY_COLUMNS: List[str] = [
    "gateway_uid",
    "gateway_name",
    "ip_address",
    "ping_total",
    "ping_success",
    "ping_failed",
    "success_rate_percent",
    "latency_avg_ms",
    "latency_min_ms",
    "latency_max_ms",
    "downtime_events",
    "downtime_seconds",
    "first_ping",
    "last_ping",
]

# ---------------------------------------------------------------------------
# focused_node_summary.csv — theo dõi MỘT node trên NHIỀU gateway.
#
# 1 dòng TỔNG (`gateway_uid = ALL`) + 1 dòng mỗi gateway đã nghe node.
#   `missed_catch`  (BẮT HỤT)   : node đã phát (gateway khác nghe) mà gateway này
#                                 không publish.
#   `late_packets`  (BẮN HỤT)   : gateway CÓ publish nhưng trễ
#                                 (`received_at − gateway_timestamp > ngưỡng`),
#                                 tức buffer SD / backfill sau khi mất MQTT.
# Xem `analytics/focused_node.py` để biết định nghĩa và giới hạn của phương pháp.
# ---------------------------------------------------------------------------
FOCUSED_NODE_SUMMARY_COLUMNS: List[str] = [
    "node_uid",
    "gateway_uid",
    "gateway_name",
    "packets_heard",          # số seq KHÁC NHAU gateway này đã publish cho node
    "copies",                 # tổng số lần publish (gồm cả trùng)
    "reference_packets",      # số seq mà BẤT KỲ gateway nào đã publish (mẫu số)
    "missed_catch",           # BẮT HỤT
    "missed_catch_percent",
    "gap_runs",               # số khoảng hụt rời rạc (1 = hụt một mạch)
    "gap_largest",            # khoảng hụt dài nhất (đơn vị: seq)
    "gap_pattern",            # diễn giải: hụt LIÊN TỤC hay RẢI RÁC
    "late_packets",           # BẮN HỤT (publish trễ)
    "late_percent",
    "delay_avg_seconds",
    "delay_min_seconds",
    "delay_max_seconds",
    "gateways",               # dòng TỔNG: số gateway; dòng gateway: 1
    "first_seen",
    "last_seen",
    "notes",
]

# ---------------------------------------------------------------------------
# focused_node_missing.csv — chi tiết từng KHOẢNG seq bị bắt hụt.
# Các seq liên tiếp cùng thiếu được gộp thành một dòng (`missing_from..missing_to`)
# để file không phình khi một gateway im lặng cả tiếng.
# ---------------------------------------------------------------------------
FOCUSED_NODE_MISSING_COLUMNS: List[str] = [
    "node_uid",
    "gateway_uid",
    "gateway_name",
    "missing_from",
    "missing_to",
    "missing_count",
    "window_start",
    "window_end",
]

# ---------------------------------------------------------------------------
# focused_node_late.csv — chi tiết từng bản copy bị bắn hụt (publish trễ),
# trễ nặng nhất trước. Số dòng mỗi gateway bị chặn
# (`focused_node_detail_limit`, mặc định 200).
# ---------------------------------------------------------------------------
FOCUSED_NODE_LATE_COLUMNS: List[str] = [
    "node_uid",
    "gateway_uid",
    "gateway_name",
    "seq",
    "received_at",
    "gateway_timestamp",
    "delay_seconds",
]

MQTT_TABLE_COLUMNS = MQTT_DATA_COLUMNS
