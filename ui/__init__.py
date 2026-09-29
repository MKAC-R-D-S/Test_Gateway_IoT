"""Tầng GUI (PyQt5) của Test Gateway IoT.

Nguyên tắc thiết kế (bắt buộc cho test 3-4 ngày chạy liên tục):

1. **GUI không bao giờ giữ chức năng "phải có"**: mọi thứ quan trọng (nhận MQTT,
   ghi CSV, ping, phân tích) nằm ở thread riêng trong `runtime/`. Đóng cửa sổ
   KHÔNG làm mất dữ liệu — tool hỏi người dùng trước khi dừng phiên.
2. **UI thread chỉ vẽ**: dữ liệu vào GUI qua `QTimer` polling (500 ms) chứ
   không qua signal từng message → tránh nghẽn event loop khi lưu lượng cao.
3. **Bảng live có giới hạn dòng** (`live_table_max_rows`): lịch sử đầy đủ nằm
   trong CSV, GUI chỉ hiển thị phần mới nhất.
4. **Mọi thao tác nặng (search CSV, đọc run cũ) chạy trong QThread**.

Module:
  - `theme.py`        : bảng màu + helper định dạng (dùng chung mọi tab)
  - `table_model.py`  : model dict→QAbstractTableModel, có giới hạn dòng
  - `log_viewer.py`   : QPlainTextEdit giới hạn dòng
  - `monitor_tab.py`  : tab "Giám sát" (live + node summary + cảnh báo)
  - `devices_tab.py`  : tab "Thiết bị" (thêm/sửa/xoá gateway, kiểm tra UID/IP)
  - `search_tab.py`   : tab "Tìm kiếm" (đọc lại CSV, phân trang)
  - `history_tab.py`  : tab "Lịch sử phiên" (danh sách phiên + xem lại kết quả)
  - `config_tab.py`   : tab "Cấu hình" (broker/topic/SEQ/ping/storage)
  - `main_window.py`  : QMainWindow ghép 5 tab, sở hữu MonitorRunner

Trạng thái các tab do `MainWindow` quản lý:
  - đang chạy  : tab Cấu hình bị khoá (đổi ngưỡng giữa phiên sẽ làm sai thống kê)
  - dừng xong  : tab Tìm kiếm + tab Lịch sử phiên tự làm mới danh sách phiên chạy

  - `config_tab.py`   : tab "Cấu hình" (broker, tham số test)
  - `main_window.py`  : cửa sổ chính, ghép mọi tab + vòng đời MonitorRunner
"""

__all__ = ["theme", "table_model", "log_viewer", "monitor_tab", "devices_tab",
           "search_tab", "history_tab", "config_tab", "main_window"]
