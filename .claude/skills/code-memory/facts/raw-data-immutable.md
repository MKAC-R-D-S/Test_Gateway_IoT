---
name: raw-data-immutable
description: Bất biến số 1 của dự án — dữ liệu thô mqtt/ và ping/ không bao giờ được sửa, xoá hay di chuyển.
type: project
updated: 2026-10-20
---

# Bất biến dữ liệu thô

`data/runs/<run_id>/mqtt/` và `data/runs/<run_id>/ping/` là **bằng chứng gốc** của phiên đo.
**TUYỆT ĐỐI KHÔNG** sửa, xoá hay di chuyển. Chỉ được ghi vào `reports/`, `meta.json`, `tool.log`.

Mọi tính năng tính lại / chấm lại / chốt lại (`analytics/refinalize.py`,
`analytics/gateway_rescore.py`) đều phải **chỉ-đọc raw**.

**Vì sao:** nếu raw bị đụng vào thì không còn gì đối chiếu lại được, và mọi báo cáo cũ mất giá trị.

**Cách áp dụng:** viết test so sánh **byte** của raw trước/sau khi chạy tính năng — mẫu có sẵn ở
`tests/test_refinalize.py`. Kèm theo đó, nguyên tắc số 2: **không bịa số** — thông tin không dựng
lại được từ CSV thô thì để **ô trống + ghi vào `warnings`**, tuyệt đối không ghi 0 hay giá trị
suy diễn.
