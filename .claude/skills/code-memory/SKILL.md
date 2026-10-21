---
name: code-memory
description: Bản đồ + bộ nhớ dự án Test_many_gateway. Đọc ĐẦU TIÊN mỗi phiên. Bản đồ - kiến trúc, sửa cái gì thì vào đâu, báo cáo nào dựng lại được từ CSV thô, 4 lệnh test bắt buộc. Bộ nhớ - số liệu đã ĐO (bảng mã file thi công, zero-pad UID, đồng bộ docs phát hành, file report thật). Dùng khi sửa tool, khi gặp lỗi lạ về tiếng Việt/UID/encoding, hoặc khi cần biết vì sao một quyết định đã chốt.
---

# code-memory — bản đồ + bộ nhớ dự án

Mục đích: phiên Claude sau biết **sửa ở đâu, chạy test nào, số thật là bao nhiêu, và các ràng
buộc không được vi phạm** mà không phải đọc lại 23k dòng code (tiết kiệm token).

Gồm hai phần:

- **`map/` — BẢN ĐỒ**: sửa cái gì thì vào đâu. Ít thay đổi.
- **`facts/` — BỘ NHỚ**: những điều đã đo / đã trả giá mới biết. Đọc code không suy ra được.

---

## 0. RÀNG BUỘC BẤT DI BẤT DỊCH (không được vi phạm)

1. **Dữ liệu thô trong `data/runs/<run_id>/mqtt/` và `ping/` TUYỆT ĐỐI KHÔNG được sửa, xoá hay
   di chuyển.** Đây là bằng chứng gốc của phiên đo. Chỉ được ghi vào `reports/`, `meta.json`,
   `tool.log`. Mọi tính năng "tính lại / chấm lại / chốt lại" đều phải chỉ-đọc raw.
   → Test canh ràng buộc này: `tests/test_refinalize.py` (so sánh byte của raw trước/sau).
2. **Không bịa số.** Nếu thông tin không thể dựng lại từ CSV thô (xem `map/reports.md`) thì để
   **ô CSV trống + ghi vào `warnings`**, không được ghi 0 hay giá trị suy diễn.
3. Ngưỡng chấm điểm và node trọng tâm của một phiên được **đóng băng trong
   `meta.json:scoring_params`** lúc bắt đầu phiên — mọi lần chấm lại đọc từ đó, KHÔNG đọc
   `config.json` hiện tại (chống trôi ngưỡng). Chỉ khi phiên quá cũ (không có snapshot) mới
   fallback về config hiện tại.

---

## 1. Chỉ mục — BẢN ĐỒ (`map/`)

| File | Nội dung |
|---|---|
| [map/architecture.md](map/architecture.md) | Kiến trúc 1 phút · dữ liệu 1 phiên · đóng gói `.exe` |
| [map/where-to-change.md](map/where-to-change.md) | Bảng "muốn thay đổi X thì vào đâu" |
| [map/reports.md](map/reports.md) | Báo cáo: cái gì dựng lại được từ CSV thô, cái gì không |
| [map/testing.md](map/testing.md) | 4 lệnh test bắt buộc + quy tắc sửa docs |

## 2. Chỉ mục — BỘ NHỚ (`facts/`)

| File | Nội dung |
|---|---|
| [facts/csv-import-encoding.md](facts/csv-import-encoding.md) | Vì sao chỉ hỗ trợ **một** bảng mã ANSI (cp1252, không phải cp1258); xử lý UTF-16 |
| [facts/gateway-uid-padding.md](facts/gateway-uid-padding.md) | UID firmware 12 hex nhưng danh mục có 9/10/11 hex — luật zero-pad và `GATEWAY_UID_MIN` |
| [facts/device-import-real-data.md](facts/device-import-real-data.md) | Số đo trên file thi công thật (40 dòng: 39 dùng được + 1 dòng để trống); luật lưu lựa chọn tick theo `line_no` |
| [facts/ascii-unaccent-why.md](facts/ascii-unaccent-why.md) | Vì sao bỏ dấu chữ lấy từ CSV (mất chữ qua ANSI, không phải crash) + 2 bẫy khi viết test |
| [facts/release-docs-sync.md](facts/release-docs-sync.md) | 3 nơi phải sửa docs; `release.json` có BOM và hash phải làm mới |
| [facts/windows-test-invocation.md](facts/windows-test-invocation.md) | Cách chạy test trên máy này (PYTHONIOENCODING, Qt offscreen) và bẫy escape |
| [facts/raw-data-immutable.md](facts/raw-data-immutable.md) | Bất biến dữ liệu thô — không sửa `mqtt/`, `ping/` |

---

## 3. Quy ước đọc

- **Số dòng trong bảng sẽ lệch sau mỗi lần sửa** → dùng `Grep` theo tên hàm/class để định vị
  lại, đừng tin số dòng tuyệt đối.
- Tài liệu người dùng ở `README.md` / `HUONG_DAN_SU_DUNG.md` theo **§ số**. Cần giải thích
  nghiệp vụ thì đọc đúng § đó, đừng đọc cả file.
- **Đừng đọc hết một lượt** — mở đúng file theo chỉ mục ở trên.

## 4. Khi nào GHI THÊM một mục mới

Ghi khi **cả ba** điều đúng:

1. Điều đó **không suy ra được** từ việc đọc code — là kết quả đo, kết quả thử, hoặc một quyết
   định đã chốt kèm lý do.
2. Nó **còn đúng ở phiên sau**, không phải chi tiết riêng của việc đang làm.
3. Chưa có file nào nói về nó.

**Vị trí:** kiến trúc · sửa ở đâu · báo cáo · test → `map/`. Số liệu đo · lý do quyết định → `facts/`.

**KHÔNG ghi** lịch sử git hay thứ đã có trong `README.md` — những cái đó đã có nguồn chân lý
riêng, chép lại chỉ tạo hai bản lệch nhau.

## 5. Cách ghi

1. Tạo file theo đúng khuôn:

```markdown
---
name: <slug-kebab-case>
description: <một dòng — dùng để quyết định có cần đọc file này hay không>
type: project | reference
updated: <YYYY-MM-DD>
---

<Nội dung. Nêu SỐ ĐO cụ thể, đừng nói chung chung.>

**Vì sao:** <lý do của quyết định>
**Cách áp dụng:** <phiên sau cần làm gì với thông tin này>
```

2. Thêm **một dòng** vào bảng chỉ mục tương ứng ở mục 1 hoặc mục 2.
3. Nếu mục mới mâu thuẫn với thứ đang có thì **sửa file cũ**, đừng tạo file thứ hai. Nếu file
   cũ đã sai hẳn thì xoá nó và bỏ dòng chỉ mục đi.

## 6. Cách tra

- Có từ khoá cụ thể (tên hàm, tên file, thông báo lỗi) → `Grep` trong `map/` và `facts/`.
- Cần biết sửa ở đâu → [map/where-to-change.md](map/where-to-change.md).
- Lịch sử thay đổi → `git log --oneline -20` (commit viết bằng tiếng Việt, có tiền tố
  `fix(...)`/`feat(...)`/`docs(...)`/`release(...)`).
