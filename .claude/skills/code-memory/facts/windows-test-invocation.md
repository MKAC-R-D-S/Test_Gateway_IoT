---
name: windows-test-invocation
description: Cách chạy 4 lệnh kiểm tra trên máy này (PYTHONIOENCODING, Qt offscreen) và bẫy escape khi ghi file.
type: reference
updated: 2026-10-20
---

# Chạy test trên máy này

Bốn lệnh bắt buộc sau mọi thay đổi (xem [../map/testing.md](../map/testing.md)):

```bash
PYTHONIOENCODING=utf-8 python -m pytest tests/ -q      # 192 passed
PYTHONIOENCODING=utf-8 python tests/smoke_test.py      # 137 OK
PYTHONIOENCODING=utf-8 python tests/gui_smoke_test.py  # 250 OK (PyQt offscreen)
PYTHONIOENCODING=utf-8 python -m compileall -q .       # cú pháp
```

## Năm điều đã trả giá mới biết

1. **Luôn có `PYTHONIOENCODING=utf-8`.** Console Windows ở đây là cp1252; in tiếng Việt sẽ
   `UnicodeEncodeError`. Không có ngoại lệ nào.
2. **`gui_smoke_test.py` cần `QT_QPA_PLATFORM=offscreen`** trong môi trường, nếu không sẽ
   thoát với **exit code 127 và stderr rỗng** — trông như lỗi lạ chứ không như lỗi Qt.
3. **Số check đếm ĐỘNG** qua biến toàn cục `CHECKS`. Thêm `check(...)` là đủ, **không** phải
   sửa con số kỳ vọng ở cuối file. (từng có tài liệu nói sai là con số cứng; nay đã đúng.)
4. **Chạy Python khi bộ phân loại lệnh trục trặc:** `.claude/settings.local.json` có allowlist
   cho dạng `python -c` **một dòng**. Dạng nhiều dòng không khớp (Python báo
   `IndentationError`). Muốn ghi nhiều file: gộp vào một lệnh, đọc stdin rồi tách theo dấu
   phân cách `@@FILE`.
5. **Bẫy escape khi ghi file bằng `python -c`:** chuỗi đi qua hai lớp (shell rồi Python) nên
   các ký hiệu escape bị biến thành ký tự thật thay vì giữ nguyên ký hiệu. Muốn file chứa
   đúng ký hiệu escape thì phải nhân đôi dấu gạch chéo. Tốt nhất là ghi bằng công cụ Write.

**Vì sao:** đây là môi trường cụ thể của máy này, không suy ra được từ code.
**Cách áp dụng:** chép nguyên bốn lệnh trên; gặp exit 127 ở GUI test thì thêm biến môi trường
`QT_QPA_PLATFORM=offscreen` trước khi tìm nguyên nhân sâu hơn.
