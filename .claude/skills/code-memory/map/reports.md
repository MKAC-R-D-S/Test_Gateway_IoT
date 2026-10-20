---
name: reports
description: Báo cáo cuối phiên — thứ tự ghi, cái gì dựng lại được từ CSV thô, cái gì KHÔNG (phải để trống + warnings).
type: reference
updated: 2026-10-20
---

# Báo cáo: cái gì dựng lại được, cái gì không

`core/pipeline.py:write_reports()` ghi theo thứ tự chuẩn (README §4 "Thứ tự ghi báo cáo"):
`node_summary`, `ping_summary`, `focused_node_summary/missing/late`, `correlation_summary`,
`mqtt_silence`, `gateway_result`, `alerts_summary`, `run_summary`
(+ `gateway_downtime.csv` do phần khác ghi).

## Dựng lại được từ CSV thô (`analytics/refinalize.py`)

- `node_summary` — replay `SequenceTracker`
- `ping_summary` — `summarize_ping_csv`
- `correlation` / `mqtt_silence` — `correlate`
- `gateway_result` — `GatewayResultEvaluator`
- `focused_node_*`
- `run_summary` — các cột đếm được

## KHÔNG dựng lại được → để trống + `warnings`

`NODE_SILENT` / `GATEWAY_SILENT`, `NODE_RECOVERED` / `GATEWAY_RECOVERED`, `gateway_downtime.csv`,
`parse_errors`, `queue_dropped`, `reconnects`, `subscribed_topics`, `broker`,
`valid/invalid_messages`.

`alerts_summary.csv`: nếu đã có thì **giữ nguyên** (không dựng lại); nếu chưa có thì dựng từ
`seq_status` kèm cảnh báo.

## Mốc thời gian phiên khi chốt lại

`_session_span()` trong `refinalize.py` lấy min/max trên **mọi** nguồn thời gian:

| Nguồn | Cột thời gian |
|---|---|
| mqtt | `received_at` / `received_last_at` |
| ping | `first_ping` / `last_ping` |
| log | `received_at` / `gateway_timestamp` |

Cẩn thận: `ping_summary.csv` **không có** cột `timestamp`.

**Vì sao:** đây là ranh giới giữa "chốt lại báo cáo" (dựng lại được toàn bộ) và "chấm lại"
(chỉ ghi lại `gateway_result` + 4 cột `run_summary`). Vượt ranh giới này là bịa số — vi phạm
ràng buộc số 2 ở `SKILL.md`.
