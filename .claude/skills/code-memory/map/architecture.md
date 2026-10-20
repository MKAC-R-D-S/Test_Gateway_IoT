---
name: architecture
description: Kiến trúc 1 phút của Test_many_gateway — module nào làm gì, dữ liệu 1 phiên gồm những gì, đóng gói .exe ra sao.
type: reference
updated: 2026-10-20
---

# Kiến trúc 1 phút

```
main.py              → entry: parse_args (--headless/--hours/--config-dir/--log-level/--version)
                       run_gui 334 | run_headless 135 | setup_logging 78
runtime/runner.py    → MonitorRunner: vòng đời 1 phiên (start/pause/resume/stop), phát event cho UI
core/pipeline.py     → MonitorPipeline: xử lý bản tin trên 1 luồng nền (dedupe, SEQ, ping, alert)
runtime/mqtt_subscriber.py, runtime/ping_monitor.py  → I/O mạng
storage/             → session.py (meta.json + trạng thái), schema.py (tên cột CSV), csv_writer.py,
                       reader.py (iter_csv_rows), __init__.py (re-export)
analytics/           → toàn bộ tính toán: sequence_tracker, packet_deduper, ping_stats, correlation,
                       focused_node, anomaly_detector, error_deduper, gateway_result, gateway_rescore,
                       refinalize
ui/                  → PyQt5. main_window.py (khung + menu + thanh trạng thái), 5 tab:
                       monitor_tab, devices_tab, search_tab, history_tab, config_tab.
                       theme.py, table_model.py (DictTableModel + CheckableDictTableModel
                       cho bảng có cột tick), log_viewer.py
core/config.py       → AppConfig + load/save config.json; core/paths.py → thư mục data/logs
core/uid.py          → chuẩn hoá/định dạng UID gateway (normalize_uid, format_gateway_uid)
core/device_import.py → nhập danh sách gateway từ CSV người dùng đưa vào (đọc nhiều
                       bảng mã, dò cột 4 tầng, tra UID qua SN → danh mục nhà máy)
core/gateway_directory.py → đọc docs/Danh sach 55 gateway.csv → {UID: series}
```

## Dữ liệu 1 phiên

`data/runs/<run_id>/{meta.json, mqtt/, ping/, reports/, tool.log}`

Xem README §2 để biết đúng tên cột từng file.

## Đóng gói

`TestGatewayIoT.spec` (PyInstaller) + `release/` + `release.json`. Chạy **LAN, không dùng 4G**.
Quy trình build lại exe: xem README §9 và HUONG_DAN_SU_DUNG.md.

Cảnh báo: `tools/build_release.ps1` **`Remove-Item -Recurse -Force` xoá sạch `release/<version>/`**
rồi dựng lại — phải hỏi người dùng trước khi chạy.

## Bản đồ README.md (đọc theo § thay vì đọc cả file)

§1 Chạy (1.1 exe · 1.2 source · 1.3 CLI) · §2 Dữ liệu được tạo ra · §3 Cấu hình ·
§4 Ý nghĩa các chỉ số (Gộp bản tin trùng · Nhận dạng gateway · Tỉ lệ ping · Delivery rate ·
SEQ status · Tương quan · Node trọng tâm · Kết quả ĐẠT/KHÔNG ĐẠT · Chấm lại phiên CŨ ·
**Chốt lại báo cáo tắt đột ngột** · Thứ tự ghi báo cáo · Tạm dừng) ·
§5 Ràng buộc thiết kế · §6 Giới hạn đã biết · §7 Kiểm thử · §8 Lịch sử phiên · §9 Đóng gói.
