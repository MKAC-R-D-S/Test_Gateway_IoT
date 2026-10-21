---
name: where-to-change
description: Bảng tra "muốn thay đổi X thì sửa file/hàm nào" — cửa vào chính khi cần sửa tool.
type: reference
updated: 2026-10-20
---

# Sửa cái gì thì vào đâu

| Muốn thay đổi | Ở đâu | Ghi chú |
|---|---|---|
| Thêm/bớt cột CSV, đổi tên cột | `storage/schema.py` | Nguồn chân lý duy nhất. Sửa ở đây thì phải rà `reports/` + GUI đọc cột đó |
| Ghi bộ báo cáo lúc kết thúc phiên | `core/pipeline.py:write_reports` · `runtime/runner.py:write_reports_now` | Xem `reports.md` |
| Logic phát hiện mất/gửi lại/trùng SEQ | `analytics/sequence_tracker.py` | `SequenceTracker.feed()`: mỗi dòng mqtt_data = 1 gói logic (đã dedupe) |
| Gộp bản tin trùng nhiều gateway | `analytics/packet_deduper.py` | README §4 "Gộp bản tin trùng" |
| Thống kê ping / delivery rate | `analytics/ping_stats.py` | README §4 |
| Tương quan MQTT↔ping, im lặng gateway | `analytics/correlation.py` | README §4 "Tương quan" |
| Node trọng tâm: bắt hụt / bắn hụt | `analytics/focused_node.py` | `FocusedNodeTracker`; `DEFAULT_DETAIL_LIMIT=200`, `MAX_GATEWAYS=128` |
| Ngưỡng & tiêu chí ĐẠT/KHÔNG ĐẠT | `analytics/gateway_result.py` | `ScoringParams`, `scoring_params_snapshot()`, `GatewayResultEvaluator`. Lưu ý `TOTAL_UID="TỔNG"` (KHÔNG phải "ALL"; row tổng của focused dùng "ALL"). README §4 |
| Nút "⚖ Chấm lại kết quả" (chỉ ghi lại gateway_result + 4 cột run_summary) | `analytics/gateway_rescore.py:rescore_run` + `ui/history_tab.py:_rescore_current` | README §4 "Chấm lại phiên CŨ" |
| Nút "🧾 Chốt lại báo cáo" (dựng lại TOÀN BỘ reports từ CSV thô) | `analytics/refinalize.py:refinalize_run` + `ui/history_tab.py:_refinalize_current` + `RefinalizeWorker` | README §4 "Chốt lại báo cáo cho phiên bị TẮT ĐỘT NGỘT" |
| Trạng thái phiên & nhãn hiển thị | `storage/session.py` | `STATUS_RUNNING/STOPPED/CRASHED/REFINALIZED`, `STATUS_LABELS`, `describe_status()`, `refinalize_session()`, `run_history_row()` |
| Tạm dừng / tiếp tục | `runtime/runner.py:pause/resume`, `core/pipeline.py:pause/resume/shift_baselines` | README §4 "Tạm dừng"; test `tests/test_pause.py` |
| Tab Lịch sử (nút, cột, worker nền) | `ui/history_tab.py` | File lớn nhất trong `ui/`. Worker là `QThread` + signal `progress/done/failed` |
| Tab giám sát / thiết bị / tìm kiếm / cấu hình | `ui/monitor_tab.py` / `ui/devices_tab.py` / `ui/search_tab.py` / `ui/config_tab.py` | |
| Nút "📥 Nhập từ CSV" (tab Thiết bị) | `ui/devices_tab.py:_on_import` + `ImportDevicesDialog` · `core/device_import.py:build_preview` | Test `tests/test_device_import.py`. Đọc file NGƯỜI DÙNG đưa vào → khác mọi chỗ khác (vốn chỉ dùng `utf-8-sig`). **Chỉ hỗ trợ 1 bảng mã ANSI (cp1252)** |
| Tick chọn từng gateway ở hộp thoại nhập CSV | `ui/devices_tab.py:ImportDevicesDialog` (`_set_all` / `_push_checks` / `_on_check_changed` / `_collect`) | Lựa chọn lưu ở `_ticked` khoá theo **`line_no`** (KHÔNG theo chỉ số bảng — bảng bị `max_rows` cắt bớt). `CheckableDictTableModel` ở `ui/table_model.py` |
| Khung cửa sổ, menu, thanh trạng thái, đăng ký tab | `ui/main_window.py` | `_build_tabs` nối 5 tab; `_build_menu`; `_apply_state` |
| Bỏ dấu chữ lấy từ CSV (chống mất chữ qua bảng mã ANSI) | `core/device_import.py:to_ascii_text` · `has_non_ascii` · `_has_diacritics_in_column` · `_resolve_row` | Xem `../facts/ascii-unaccent-why.md`. **Ranh giới có chủ ý:** chỉ chữ do module nhập CSV sinh ra; lỗi từ `device.validate()` (`core/config.py`) GIỮ NGUYÊN dấu vì dùng chung với hộp thoại thêm/sửa tay |
| Cột "Ghi chú" riêng từng gateway khi nhập CSV | `core/device_import.py:_HEADER_KEYS["notes"]` (đặt **CUỐI** dict) · `ImportPreviewRow.notes` · `ui/devices_tab.py:ImportDevicesDialog.cmb_notes` | Đứng cuối để `mo ta`/`description` không cướp cột của `name`. Không có cột trong file → mặc định `"Nhap tu <tên file>"` đã bỏ dấu |
| Xoá NHIỀU gateway một lượt ở bảng chính (tab Thiết bị) | `ui/devices_tab.py:_on_delete_picked` · `_picked_uids` · `_set_all_picked` | Tra theo **`uid`** trong dict dòng, KHÔNG theo chỉ số dòng (`deque(maxlen=…)` làm chỉ số lệch). Chỉ sửa `devices.json`; dữ liệu CSV thô của phiên cũ không đụng tới |
| `CheckableDictTableModel` — ô tick + tín hiệu tick | `ui/table_model.py` | Lớp con **chỉ vẽ ô tick ở cột 0** ⇒ bảng nào dùng nó cũng phải chèn cột `pick` rỗng lên đầu. `__init__` phải forward `align_right`/`row_tooltips` xuống lớp cha (bảng chính dùng cả hai cho cột "Tỉ lệ ping"). Có tín hiệu riêng `check_toggled` |
| Khoá/mở khoá nút theo trạng thái phiên | `ui/history_tab.py:_update_buttons` | Thêm nút mới thì cập nhật hàm này |
| Khoá cấu hình / đường dẫn data | `core/config.py`, `core/paths.py` | README §3 |
| Chuẩn hoá/zero-pad UID gateway | `core/uid.py` | Xem `../facts/gateway-uid-padding.md` — dùng chung ở `analytics/`, sửa là phải chạy lại toàn bộ test |

Số dòng đã bỏ khỏi bảng này (dễ lệch sau mỗi lần sửa) — hãy `Grep` theo tên hàm/class
để định vị lại.
