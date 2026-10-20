---
name: testing
description: 4 lệnh test bắt buộc sau mọi thay đổi + quy tắc phải sửa docs ở 3 nơi.
type: reference
updated: 2026-10-20
---

# Test & kiểm tra (chạy sau MỌI thay đổi)

```bash
PYTHONIOENCODING=utf-8 python -m pytest tests/ -q      # 192 passed  (unit)
PYTHONIOENCODING=utf-8 python tests/smoke_test.py      # 137 OK      (harness tự viết)
PYTHONIOENCODING=utf-8 python tests/gui_smoke_test.py  # 250 OK      (PyQt offscreen)
PYTHONIOENCODING=utf-8 python -m compileall -q .       # cú pháp
```

## Quy tắc

- **Luôn prefix `PYTHONIOENCODING=utf-8`** — console Windows mặc định cp1252 sẽ
  `UnicodeEncodeError` khi in tiếng Việt.
- `tests/gui_smoke_test.py` chạy Qt **offscreen**; muốn giả lập hộp thoại thì dùng pattern
  `class _FakeMsgBox` + `mock.patch` với `ui.history_tab.QMessageBox` (đã có sẵn trong file).
- Số lượng check trong smoke test đếm **động** qua biến toàn cục `CHECKS` — thêm
  `check(...)` là đủ, **không** phải sửa con số kỳ vọng ở cuối file.
- Thêm tính năng mới: đặt test cạnh tính năng (`tests/test_<tên>.py`).

## Khi sửa hành vi, phải cập nhật ĐỒNG THỜI

1. `README.md` — người phát triển
2. `HUONG_DAN_SU_DUNG.md` — người dùng cuối, chỉ có file `.exe`
3. `release/1.0.0/` — bản chụp docs phát hành (**dễ bị quên nhất**)

Chi tiết hai cái bẫy của `release.json` (BOM + hash thủ công): xem
→ [../facts/release-docs-sync.md](../facts/release-docs-sync.md)

Chi tiết cách chạy trên máy này (Qt offscreen, exit 127, allowlist): xem
→ [../facts/windows-test-invocation.md](../facts/windows-test-invocation.md)
