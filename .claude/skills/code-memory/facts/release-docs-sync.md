---
name: release-docs-sync
description: Sửa hành vi thì phải sửa docs ở 3 nơi; release.json có BOM UTF-8 và hash phải làm mới thủ công.
type: reference
updated: 2026-10-20
---

# Đồng bộ tài liệu & gói phát hành

Sửa hành vi thì phải cập nhật **cả ba** nơi:

1. `README.md` — người phát triển
2. `HUONG_DAN_SU_DUNG.md` — người dùng cuối, chỉ có file `.exe`
3. `release/1.0.0/README.md` + `release/1.0.0/HUONG_DAN_SU_DUNG.md` — **bản chụp phát hành, dễ quên nhất**

Hai bản trong `release/1.0.0/` là **bản sao byte-khít** của hai file gốc. Kiểm bằng cách so hash:

```python
import hashlib, pathlib
for n in ("README.md", "HUONG_DAN_SU_DUNG.md"):
    a = hashlib.sha256(pathlib.Path(n).read_bytes()).hexdigest()
    b = hashlib.sha256(pathlib.Path("release/1.0.0/" + n).read_bytes()).hexdigest()
    print(n, a == b)
```

## release.json — hai cái bẫy

- File có **BOM UTF-8**. Đọc/ghi phải dùng `encoding="utf-8-sig"`, nếu không `json.loads` ném
  `Unexpected UTF-8 BOM`. Khi ghi lại phải **giữ BOM**.
- Mục `files[]` ghi `bytes` + `sha256` của từng file trong `release/1.0.0/`. Sửa docs thì hash
  đổi và **phải làm mới thủ công** — không có gì tự sinh lại.

**Cách áp dụng:** sau khi sửa docs, làm mới hash rồi mới coi là xong. Việc build lại exe cần
`tools/build_release.ps1`, mà script đó **`Remove-Item -Recurse -Force` xoá sạch
`release/<version>/`** rồi dựng lại — **phải hỏi người dùng trước khi chạy**, không tự ý.
