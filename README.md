# longrun_monitor — theo dõi MQTT gateway IoT Type0 trong 3–4 ngày

Tool độc lập, **không sửa** source firmware và không sửa các tool khác trong repo.
Nguồn chân lý về protocol là `docs/MQTT_PROTOCOL_ANALYSIS.md` (đọc trực tiếp từ
firmware `../IoT_Type0_Gateway/src`).

```
longrun_monitor/
├── main.py                  # entry point: GUI (mặc định) hoặc --headless
├── run.ps1                  # chạy nhanh trên Windows
├── requirements.txt
├── config/
│   ├── config.json          # broker, topic, ngưỡng SEQ/ping/cảnh báo, storage
│   └── devices.json         # danh sách gateway cần theo dõi
├── core/                    # config, models, parser, UID, pipeline
├── storage/                 # schema CSV, CsvSink, reader (search), RunSession
├── analytics/               # SEQ tracker, ping stats, correlation, anomaly
├── runtime/                 # MQTT subscriber, ping monitor, MonitorRunner
├── ui/                      # PyQt5: 5 tab (Giám sát / Thiết bị / Tìm kiếm / Lịch sử phiên / Cấu hình)
├── docs/                    # phân tích protocol firmware
├── logs/                    # log của chính tool (xoay vòng 5×5 MB)
└── data/<run_id>/           # dữ liệu từng phiên test
```

## 1. Chạy

```powershell
# GUI
python main.py
.\run.ps1

# Không GUI (khuyến nghị cho test dài ngày, ít rủi ro hơn)
python main.py --headless
python main.py --headless --hours 72
.\run.ps1 -Headless -Hours 72
```

| Tuỳ chọn | Ý nghĩa |
|---|---|
| `--headless` | Không mở GUI; vẫn nhận MQTT, ghi CSV, ping, chốt báo cáo |
| `--hours N` | Tự dừng sau N giờ (chỉ dùng cùng `--headless`) |
| `--config-dir PATH` | Dùng thư mục cấu hình khác |
| `--log-level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `--version` | In phiên bản tool |

Trong chế độ headless, **Ctrl+C dừng sạch**: xử lý nốt hàng đợi → ghi báo cáo →
cập nhật `meta.json`.

## 2. Dữ liệu được tạo ra

Mỗi lần chạy tạo `data/<YYYYMMDD_HHMMSS>_<suffix>/`:

| Đường dẫn | Nội dung |
|---|---|
| `meta.json` | Thông tin phiên: run_id, broker, topic, gateway, thời điểm bắt đầu/kết thúc, trạng thái. Cập nhật định kỳ **60 s** → PC tắt đột ngột vẫn biết phiên chạy tới đâu (`status` còn `RUNNING` = bị tắt đột ngột). Gồm cả **`label`** (tên phiên do người dùng đặt — xem §8) và **`gateway_names`** (ảnh chụp `{UID → tên}` lúc bắt đầu phiên). |
| `mqtt/mqtt_data_*.csv` | Mọi bản tin tool nhận được (DATA / INFO / ERROR / LOG / UNKNOWN) — 36 cột, kèm kết quả phân tích SEQ. **Một bản tin logic = một dòng**, không phải một dòng cho mỗi lần publish (xem §4 “Gộp bản tin trùng giữa nhiều gateway”). |
| `mqtt/mqtt_missing_*.csv` | Từng khoảng SEQ bị thiếu (node, seq từ → đến, số gói thiếu). |
| `mqtt/mqtt_anomalies_*.csv` | Bất thường dữ liệu (trùng, sai thứ tự, reset SEQ, giá trị cảm biến lạ…). |
| `mqtt/mqtt_errors_*.csv` | Khung `node/error` **đã gộp** theo `(error, raw_data)`: 1 dòng cho cả cụm gateway, kèm `copies`, `gateways_heard`, `gateways`. |
| `mqtt/gateway_log_*.csv` | Report hệ thống `<model>/<uuid>/log` và Last Will (`status=offline`) nếu bật. |
| `mqtt/alerts_*.csv` | Cảnh báo do tool sinh (node im lặng, gateway im lặng, tỉ lệ thiếu cao…). |
| `ping/ping_*.csv` | Từng lần ping: RTT, thành công/thất bại, mất gói. |
| `ping/downtime_*.csv` | Sự kiện gateway mất/hồi phục (bắt đầu, kết thúc, thời lượng, số ping fail). |
| `reports/node_summary.csv` | Tổng hợp theo node: số gói, SEQ đầu/cuối, thiếu/trùng/sai thứ tự, tỉ lệ nhận. |
| `reports/ping_summary.csv` | Tổng hợp theo gateway: tỉ lệ thành công, RTT trung bình/min/max/p95, jitter. |
| `reports/correlation_summary.csv` | Tương quan MQTT ↔ ping theo window 60 s (phát hiện "mạng vẫn tốt mà mất dữ liệu"). |
| `reports/mqtt_silence.csv` | Các window im lặng liên tiếp (nghi ngờ mất dữ liệu). |
| `reports/alerts_summary.csv` | Đếm cảnh báo theo loại/mức. |
| `reports/run_summary.csv` | Chốt 1 dòng: tổng bản tin, tỉ lệ thiếu, số reconnect, downtime, thời lượng. |
| `reports/focused_node_summary.csv` | **Node trọng tâm**: 1 dòng TỔNG + 1 dòng mỗi gateway — bắt hụt / bắn hụt, kèm **kiểu ngắt quãng** (`gap_runs`, `gap_largest`, `gap_pattern`). Chỉ sinh khi có node trọng tâm (§4 “Node trọng tâm”). |
| `reports/focused_node_missing.csv` | Node trọng tâm: từng khoảng SEQ liên tiếp mà gateway không publish (bắt hụt), kèm mốc thời gian. |
| `reports/focused_node_late.csv` | Node trọng tâm: từng lần publish bị trễ (bắn hụt) — nặng nhất trước, kèm `delay_seconds`. |

CSV chốt theo ngày (`rotation = "daily"`) hoặc theo giờ, ghi theo lô
(`flush_interval_seconds = 2`, `flush_max_records = 200`) và `fsync` → mất điện
chỉ mất tối đa vài giây dữ liệu cuối.

## 3. Cấu hình

Sửa trong tab **Cấu hình** (GUI) hoặc trực tiếp `config/config.json`.
Thay đổi **không** áp dụng vào phiên đang chạy (đổi ngưỡng giữa phiên sẽ làm sai
thống kê) — GUI khoá form trong lúc chạy.

Điểm cần lưu ý:

| Khoá | Mặc định | Ghi chú |
|---|---|---|
| `mqtt_host` / `mqtt_port` | `45.117.170.179` / `1883` | Theo cấu hình factory. Một số tool khác trong repo dùng 1885 — kiểm tra trước khi test dài. |
| `subscribe_topics` | `["node/#"]` | Phủ `node/<uuid>/data`, `node/<uuid>/info`, `node/error`. |
| `subscribe_gateway_log` | `false` | Bật thì subscribe thêm `+/+/log` (retain, 60 s/lần). |
| `seq_max` | `65535` | SEQ của node là `uint16_t` trong firmware. |
| `seq_out_of_order_window` | `16` | Nhảy lùi ≤ 16 coi là sai thứ tự; lớn hơn coi là reset SEQ. |
| `ping_interval_seconds` | `10` | Ping song song, tối đa `ping_max_concurrent` gateway cùng lúc. |
| `packet_merge_window_seconds` | `2.0` | Cửa sổ gộp các bản copy của cùng một bản tin LoRa (`packet_merge_enabled = true`). Xem §4. |
| `ping_down_threshold` | `2` | Ping fail liên tiếp mới chốt là DOWN (tránh false positive 1 gói ICMP). |
| `node_silent_seconds` | `600` | Không nhận bản tin từ node trong 10 phút → cảnh báo. |
| `focused_node_uid` | `""` | UID node cần theo dõi xuyên nhiều gateway (§4 “Node trọng tâm”). Đổi được giữa phiên ở tab **Giám sát → Node trọng tâm**. |
| `focused_node_enabled` | `false` | Bật theo dõi node trọng tâm ngay từ đầu phiên (không cần thao tác GUI). |
| `focused_node_late_seconds` | `30` | Ngưỡng “bắn hụt”: `received_at − gateway_timestamp` vượt ngưỡng này thì bị coi là publish trễ. |
| `focused_node_detail_limit` | `200` | Số ca trễ chi tiết giữ lại **mỗi gateway** trong `focused_node_late.csv`. |
| `rotation` | `daily` | Đổi sang `hourly` nếu lưu lượng rất cao. |

## 4. Ý nghĩa các chỉ số

### Gộp bản tin trùng giữa nhiều gateway

Một node phát **một** bản tin LoRa thì **mọi** gateway trong tầm đều publish lên
MQTT. Nếu không xử lý, một bản tin sẽ bị đếm 7–8 lần và làm sai toàn bộ thống kê
(đo thực tế trên log 3 ngày: 24 788 dòng chỉ ứng với 3 470 bản tin logic).

Tool gộp như sau, cửa sổ `packet_merge_window_seconds` (mặc định **2.0 s**, đo
bằng `time.monotonic()` – thời điểm **tool nhận**, không phải giờ trong payload):

| Loại | Khoá gộp | Cột phủ sóng |
|---|---|---|
| DATA | `(node_uid, seq)` | `gateways_heard` (số gateway), `gateways` (danh sách UID), `received_at` → `received_last_at` |
| ERROR | `(error, raw_hex, payload_json)` | như trên + `copies` |

Bản copy “đại diện” là gateway có **RSSI tốt nhất**; các cột khác lấy từ bản copy
đó. Vì vậy:

- **`seq_status`, `missing_count`, `delivery_rate` đều tính theo bản tin logic** —
  không còn bị nhân theo số gateway.
- Một dòng CSV **không** tương ứng một lần publish. Muốn biết mức phủ sóng, đọc
  `gateways_heard` / `gateways`.
- Bản tin **ra khỏi cửa sổ gộp mới được ghi**, nên lúc đang chạy các bản tin mới
  nhất có thể còn nằm trong bộ gộp (đã flush khi dừng phiên).
- Cửa sổ quá ngắn (đo thực tế: 0 s) làm số dòng tăng vọt (**13 138** khoảng thiếu
  thay vì **1 381** nếu gộp). Cửa sổ 2 s là điểm cân bằng đã đo.

Payload JSON **hỏng** (không parse được) được xếp `packet_type = UNKNOWN` và
`valid = 0`, không tính vào `data_packets`.

### Nhận dạng gateway & tra cứu

**Gateway UID có thể dài 11 hoặc 12 hex.** Firmware in
`String(ESP.getEfuseMac(), HEX).toUpperCase()` (`config_manager.cpp:7`) — ESP32
**bỏ nibble 0 đầu**, nên MAC efuse có nibble đầu bằng 0 cho ra UID 11 ký tự
(ví dụ `8F9199E139C`), các MAC khác cho 12 ký tự (`4CF9199E139C`). **Độ dài lẻ
là hợp lệ**, không phải dữ liệu lỗi.

Do đó `devices.json` có thể ghi `8F9199E139C` **hoặc** `08F9199E139C` — tool nhận
ra cùng một gateway. Mọi phép so khớp gateway đi qua `core/uid.py`
(`normalize_gateway_uid`, `gateway_uid_variants`, `format_gateway_uid`), **không
bao giờ** so chuỗi trực tiếp: `gateway_uid_variants()` sinh cả dạng 11 hex (nguyên
bản) và 12 hex (zero-pad) để hai dạng luôn khớp nhau.

Điều này ảnh hưởng tới 3 chỗ:

1. **Tên gateway** — tra qua `gateway_name_index(devices)` (`core/uid.py`), trả về
   tên theo mọi biến thể UID, nên `8F9199E139C` vẫn ra đúng
   `MGWI-V1-26070009` chứ không phải `UNKNOWN_GATEWAY`.
2. **Lọc theo "Gateway UID"** (tab Tìm kiếm) — khớp cả cột `gateway_uid` **và**
   cột `gateways` (mọi gateway đã nghe bản tin, không chỉ gateway RSSI tốt nhất),
   cho phép nhập **một phần** UID (`456`, `8F91`, `08F91`) và tự nhận dạng.
3. **Tương quan ping ↔ MQTT** — `analytics/correlation.py` chuẩn hoá UID về 12 hex
   trước khi so, nên một gateway **không** bị tách thành 2 UID và **không** bị báo
   sai là `GATEWAY_SILENT` (mqtt = 0).

**CSV của phiên bản cũ** ghi trước khi có bản sửa này có thể để trống
`gateway_uid` / `gateways` (vì bản tin 11-hex từng bị xếp `CRC ERROR` /
`MISSING_GATEWAY_ID` / `INVALID UUID`) — khi đó thông tin gateway **chỉ còn trong
`payload_json`**. Reader (`storage/reader.py`) khi tìm thấy dòng khớp qua
`payload_json` sẽ **tính lại** `gateway_uid` + `gateway_name` từ payload và bảng
tên thiết bị trước khi trả về, nên kết quả tìm kiếm **không bao giờ** hiện ô
"ID gateway" trống hay tên `unknown`.

Cột "Gateway cùng nghe" **đã bỏ khỏi bảng kết quả** (tab Tìm kiếm) vì mỗi
dòng vốn đã có `gateway_uid` (gateway có RSSI tốt nhất) và `gateway_name`; danh
sách đầy đủ gateway đã nghe vẫn nằm trong CSV (`gateways`) và vẫn được dùng để
**lọc theo "Gateway UID"** (xem mục 2 ở trên).

### Tỉ lệ ping theo từng gateway (tab Thiết bị)

Tab **Thiết bị** có cột `Tỉ lệ ping (%)` — **mỗi dòng một gateway**, để nhìn
ngay gateway nào đang mất kết nối mà không phải mở CSV:

- Cột lấy số từ `reports/ping_summary.csv` của phiên, tra theo UID qua
  `gateway_uid_variants()` nên khớp cả dạng 11 hex lẫn 12 hex.
- **Đang chạy phiên** → đọc trực tiếp `PingTracker` trong RAM, cập nhật mỗi ~5 s
  (cột chỉ để nhìn xu hướng, không cần mịn tới 1 s).
- **Chưa chạy phiên** → hiển thị số liệu của phiên **gần nhất** (đọc một lần lúc
  mở tool); bắt đầu phiên mới sẽ **xoá** số liệu cũ để không lẫn hai phiên.
- Chưa ping lần nào → hiện `-`, **không** hiện `0.0 %` (0 % là mất hết, khác hẳn
  "chưa có dữ liệu"). Tooltip ô có `số thành công / tổng`, số lần thất bại và RTT
trung bình.

### Delivery rate (tỉ lệ nhận) — **đọc kỹ trước khi kết luận**

Firmware **không** cung cấp tổng số packet kỳ vọng và không có số thứ tự message
MQTT (xem `docs/MQTT_PROTOCOL_ANALYSIS.md` §9). Vì vậy tool định nghĩa:

```
delivery_rate_percent = 100 * packets / (packets + missing)
```

Trong đó `missing` = tổng số SEQ bị nhảy cóc giữa **hai bản tin liên tiếp mà
cùng một node đã gửi**:

```
missing = Σ max(0, seq_sau − seq_trước − 1)        # không tính wrap-around
```

Hệ quả:

- Node **ngừng gửi** (hết pin, mất sóng) **không** làm giảm tỉ lệ nhận — chỉ sinh
  cảnh báo "node im lặng". Muốn biết node có ngừng gửi hay không, xem cột
  `last_seen` và cảnh báo `NODE_SILENT`.
- `SEQ = 0` là **info frame** của node mới (`data_process.cpp:253`), không phải
  gói bị thiếu và không phải reboot.
- Wrap-around `65534 → 65535 → 0 → 1` là **hợp lệ**; chỉ nhảy lùi > ngưỡng mới
  tính là `SEQ_RESET`.

### SEQ status

| Giá trị | Nghĩa |
|---|---|
| `FIRST` | Bản tin đầu tiên của node trong phiên (chưa có mốc so sánh). |
| `NORMAL` | `seq = seq_trước + 1` (hoặc wrap hợp lệ). |
| `MISSING` | Nhảy tiến → có gói không tới; ghi kèm `missing_count`. |
| `DUPLICATE` | Lặp lại đúng SEQ trước đó. |
| `OUT_OF_ORDER` | Nhảy lùi trong khoảng `seq_out_of_order_window`. |
| `RESET` | Nhảy lùi xa hơn ngưỡng → nghi node/gateway khởi động lại. |
| `N/A` | Bản tin không mang SEQ (info frame, node cũ 22 B, error, log). |

### Tương quan ping ↔ MQTT

`correlation_summary.csv` chia thời gian thành window 60 s (chu kỳ report của
firmware) và đối chiếu: gateway trả lời ping nhưng không có bản tin nào, hay
gateway mất ping và cũng mất dữ liệu. Dùng để phân biệt:

- **mất cả ping và dữ liệu** → lỗi mạng / gateway treo;
- **ping tốt nhưng mất dữ liệu** → lỗi phía node, LoRa, hoặc gateway không
  forward bản tin.

### Node trọng tâm — bắt hụt / bắn hụt trên nhiều gateway

Tính năng này trả lời câu hỏi: **với MỘT node, mỗi gateway bắt hụt / bắn hụt bao
nhiêu bản tin?**

Chọn node ở tab **Giám sát → Node trọng tâm** (ô nhập UID 16 hex, hoặc 24 hex với
node cũ) rồi bấm **Theo dõi**; bảng cập nhật realtime. Khi chạy headless, đặt
`focused_node_enabled = true` + `focused_node_uid` trong `config/config.json` —
tóm tắt được in cả lúc chạy và lúc kết thúc.

**Cách suy luận.** Một node phát một bản tin LoRa thì **mọi** gateway trong tầm
đều publish lên MQTT (`(node_uid, seq)` là khoá của bản tin logic). Vì vậy:

- **Tập tham chiếu** = hợp của mọi `seq` mà **bất kỳ** gateway nào đã publish cho
  node đó. Đây là bằng chứng mạnh nhất mà MQTT cung cấp về việc **node ĐÃ phát**.
- **BẮT HỤT** (`missed_catch`) = `|tham chiếu| − số seq gateway đó publish`.
  Tức node đã phát (gateway khác nghe được) mà gateway này **không bắn lên MQTT**.
- **BẮN HỤT** (`late_packets`) = số lần publish của gateway **trễ**:
  `received_at − gateway_timestamp > focused_node_late_seconds`. Nguyên nhân thực
  tế: gateway mất MQTT → firmware buffer `/data` vào thẻ SD rồi gửi bù (backfill)
  sau khi reconnect. Bản tin không mất trên đường LoRa nhưng **không kịp lên MQTT**.

Ngoài ra mỗi gateway còn có cột `delay_avg/min/max_seconds` (độ trễ
`received_at − gateway_timestamp`; **giá trị âm là bình thường** khi đồng hồ
gateway chạy trước PC) và cột `notes` diễn giải bằng tiếng Việt.

Gateway **đã cấu hình trong `devices.json` nhưng không publish bản tin nào** của
node vẫn được ghi 1 dòng với bắt hụt 100% (`gateways_silent` trong log) — đây là
ca đáng nghi nhất: gateway chết hoặc ra khỏi tầm sóng.

**Kiểu ngắt quãng (`gap_pattern`).** Con số `missed_catch` một mình không phân
biệt được hai nguyên nhân rất khác nhau. Tool quét lại bitmap `seq` của từng
gateway và tách các `seq` hụt thành từng **khoảng liên tiếp** (gap run):

- `gap_runs` = số khoảng hụt rời nhau; `gap_total` = tổng số `seq` hụt;
  `gap_largest` = khoảng hụt dài nhất (số `seq`).
- **1 khoảng duy nhất** (`gap_runs == 1`, đặc biệt khi `gap_largest` lớn) →
  *“Hụt LIÊN TỤC N seq một mạch”* ⇒ nghi **mất tầm sóng / gateway offline / mất
  MQTT một mạch**, không phải lỗi thu từng gói.
- **Nhiều khoảng** (`gap_runs >= 2`) → *“Hụt RẢI RÁC: N khoảng, trung bình M
  seq”* ⇒ nghi **mất gói RF lẻ tẻ** hoặc nhiễu, link vẫn sống.
- Dòng **TỔNG** cũng phân loại cùng tiêu chí: `“X gateway có hụt liên tục,
  Y gateway hụt rải rác”`.

Cột `gap_pattern` chứa nguyên câu kết luận tiếng Việt này (được nối thêm vào
`notes`); tab **Giám sát → Node trọng tâm** có cột **“Kiểu ngắt quãng”** tương
ứng. Việc quét bitmap tốn ~0,4 s với cấu hình lớn nên tool **cache** kết quả và
chỉ tính lại khi xuất báo cáo / theo chu kỳ, **không** tính realtime mỗi gói.

**Giới hạn cần nhớ trước khi kết luận:**

- MQTT **không** nói được gateway bắt được sóng nhưng không publish vì lý do khác
  (hàng đợi nội bộ, lỗi publish). Vì vậy `missed_catch` là **chặn trên** của
  “bắt hụt”, và bắt hụt + bắn hụt là **chặn dưới** của tổng số hụt.
- Một gateway mất mạng cả tiếng sẽ hiện `missed_catch` rất lớn — đó là *link down*,
  không phải lỗi thu sóng. Cột `gap_pattern` giúp chỉ ra ngay ca này (hụt **liên
  tục** một mạch); đối chiếu tiếp với `reports/ping_summary.csv` /
  `correlation_summary.csv` (ping fail cùng lúc) để phân biệt.
- Thống kê theo **bitmap `seq`** nên sau khi node reboot / reset SEQ, `seq` trùng
  được coi là cùng một giá trị — kết quả mang tính xấp xỉ ở các mốc reset.
- Bộ đếm dùng chung `packet_merge_window_seconds`; tắt gộp bản tin sẽ làm số
  “bắn hụt” đếm quá (mỗi lần publish lặp tính là một lần trễ).

## 5. Ràng buộc thiết kế (vì sao tool "chịu" được 3–4 ngày)

1. **CSV là source of truth**, GUI chỉ là lớp xem. Đóng cửa sổ không làm mất dữ liệu.
2. **GUI không nằm trên đường đi của dữ liệu**: MQTT callback chỉ đẩy bytes vào
   queue; một thread phân tích duy nhất xử lý SEQ; GUI đọc bằng `QTimer` 500 ms.
   GUI lag/treo hoàn toàn không ảnh hưởng tới việc ghi.
3. **Một sink = một writer thread** cho mỗi file CSV, ghi theo lô + `fsync`, có
   đếm `rows_dropped` (back-pressure) để không âm thầm mất dữ liệu.
4. **Bảng live bị giới hạn dòng** (`live_table_max_rows`) — lịch sử đầy đủ vẫn ở CSV.
5. **Tìm kiếm phân trang + chạy trong QThread** — không nạp cả file lớn vào RAM.
6. **`meta.json` heartbeat 60 s** + `stop()` luôn cố ghi báo cáo kể cả khi một
   phần lỗi (test 3–4 ngày thì "luôn có báo cáo" quan trọng hơn "báo cáo hoàn hảo").
7. **Không tự kiểm CRC**: gateway đã kiểm CRC trước khi publish
   (`data_process.cpp:208-241`); payload JSON không chứa CRC nên kiểm lại là
   kiểm tra giả (`docs/MQTT_PROTOCOL_ANALYSIS.md` §8).

## 6. Giới hạn đã biết

- `mqtt_timestamp` hiện lấy bằng `received_at` vì firmware không gửi metadata
  broker. Lệch nhiều giữa `received_at` và `gateway_timestamp` ⇒ nghi backfill từ
  buffer SD sau khi gateway reconnect (`mqtt_task.cpp:141-158`).
- Chưa biết node có reset SEQ về 0 sau reboot hay không
  (`MQTT_PROTOCOL_ANALYSIS.md` A2) → tool dùng ngưỡng cấu hình được
  (`seq_reset_threshold`).
- Gateway lạ (không có trong `devices.json`) vẫn được **ghi đầy đủ** và gắn cờ
  `UNKNOWN_GATEWAY`; bật `alert_unknown_gateway` để biết ngay từ đầu.
- **Gateway UID có thể là 11 hoặc 12 hex** (firmware in
  `String(ESP.getEfuseMac(), HEX)` **không** zero-pad — `config_manager.cpp:7`).
  Tool chuẩn hoá qua `core/uid.py` nên hai dạng luôn nhận ra **cùng một gateway**;
  chi tiết xem §4 "Nhận dạng gateway & tra cứu".
- Node cũ 22 B báo info ngay trên `/data` không có SEQ → phân loại `DATA_NO_SEQ`
  và **không** tính vào thống kê delivery (cấu hình được — A6).

## 7. Kiểm thử

Chạy **trước khi** mang tool ra chạy 3-4 ngày. Cả hai đều KHÔNG cần mạng, KHÔNG
cần broker MQTT và tự dọn thư mục tạm sau khi xong.

```powershell
# 1) Logic: config, parse payload thật, phân tích SEQ, CSV, báo cáo, session
python tests/smoke_test.py

# 2) Unit test (pytest) — gồm tests/test_focused_node.py cho bắt hụt / bắn hụt
python -m pytest tests/ -q

# 3) GUI: import mọi module ui/*, dựng MainWindow rồi đóng (offscreen)
python tests/gui_smoke_test.py

# 4) Kiểm tra cú pháp toàn bộ file
python -m compileall -q .
```

Trên Windows, console mặc định là `cp1252` nên script in tiếng Việt có thể lỗi
`UnicodeEncodeError`. Đặt biến môi trường trước khi chạy:

```powershell
$env:PYTHONUTF8 = "1"
```

Cả hai trả `exit code 0` khi mọi kiểm tra đạt. `gui_smoke_test.py` tự đặt
`QT_QPA_PLATFORM=offscreen`; PyQt5 không có trong máy thì nó **SKIP** (vẫn exit 0),
nên chạy được cả trên máy chỉ chạy `--headless`.

Lưu ý về console Windows: script in tiếng Việt nên cần UTF-8

```powershell
$env:PYTHONUTF8 = '1'
```

Ghi chú khi gặp lỗi thật (đã từng xảy ra):

- `OverflowError` khi dựng cửa sổ = có widget `QSpinBox` bị đặt trần vượt int32
  CÓ DẤU (tối đa 2147483647). `seq_max` của firmware là uint16 nên không cần
  vượt int32.
- Cảnh báo `QFontDatabase: Cannot find font directory` khi chạy offscreen là **vô hại**.

## 8. Lịch sử phiên (xem lại kết quả phiên đã chạy)

Tab **📜 Lịch sử phiên** (tab thứ 4) đọc mọi thư mục con của `data/runs/` và
liệt kê **tất cả** phiên đã chạy — **mới nhất trước** — kể cả phiên bị **tắt
ngang** (mất điện, `kill`, đóng console), mà không cần mở CSV bằng Excel. Danh
sách tự làm mới khi phiên bắt đầu / kết thúc; bấm **🔄 Làm mới danh sách** để
quét lại bất cứ lúc nào. Có ô **tìm theo gateway** (nhập **UID** hoặc **tên**
gateway) để lọc nhanh ra các phiên đã chạy một gateway nào đó.

**Bảng danh sách phiên** (bên trên) — một dòng mỗi phiên, các cột: **Tên phiên**,
**Mã phiên**, **Bắt đầu**, **Trạng thái**, **Thời lượng**, **Số GW**, **Gateway
của phiên**, **Bản tin MQTT**, **Node**, **Thiếu SEQ**, **Ping fail**, **Số báo
cáo**, **Dung lượng**. Cột “Trạng thái” được tô màu theo nhãn (xanh = đang chạy,
vàng = tắt ngang, xám = đã dừng). Cột **Tên phiên** hiện tên người dùng đặt, nếu
chưa đặt thì hiện chính **Mã phiên**.

**Nhãn trạng thái** ([`storage/session.py`](storage/session.py) → `describe_status`):

| Trạng thái | Ý nghĩa |
|---|---|
| **Đang chạy** | Chỉ khi `run_id` trùng với phiên GUI đang mở (`active_run_id`); các phiên khác còn sót `RUNNING` trong `meta.json` **không** bị gán nhãn này |
| **Đã dừng** | `stop()` đã chốt báo cáo bình thường |
| **Đã dừng (thiếu mốc kết thúc)** | `status = STOPPED` nhưng không có `ended_at` |
| **Tắt ngang** | `status = CRASHED` → dữ liệu thô vẫn còn, nhưng **không có báo cáo** |
| **Tắt ngang (không chốt báo cáo)** | Còn `RUNNING` trong `meta.json` hoặc `reports/` rỗng |

**Nhấn một dòng phiên** ở bảng trên để nạp kết quả của phiên đó (đọc trong
`QThread`, không treo GUI). Phần dưới hiện khối thông tin phiên (run_id, trạng
thái, thời điểm bắt đầu/kết thúc, thời lượng, broker, dung lượng, số file, danh
sách gateway) rồi một `QTabWidget` gồm các trang:

| Trang | Nguồn / nội dung |
|---|---|
| **Tổng kết** | `meta.json` + `reports/run_summary.csv` — cùng bộ chỉ số đã dùng khi chốt phiên |
| **Gateway của phiên** | danh sách gateway trong `meta.json` + **tên chốt lúc BẮT ĐẦU phiên** (`gateway_names`, xem ghi chú bên dưới) |
| **Node** | `reports/node_summary.csv` (§4 “Delivery rate”) |
| **Ping** | `reports/ping_summary.csv` |
| **MQTT ↔ Ping** | `reports/correlation_summary.csv` |
| **Node trọng tâm** | `reports/focused_node_summary.csv` — bắt hụt / bắn hụt + `gap_pattern` (§4) |
| **Bắt hụt theo SEQ** | `reports/focused_node_missing.csv` |
| **Bắn hụt (trễ)** | `reports/focused_node_late.csv` |
| **Downtime** | `reports/gateway_downtime.csv` |
| **MQTT im lặng** | `reports/mqtt_silence.csv` |
| **Cảnh báo** | `reports/alerts_summary.csv` |

Trang nào không có file tương ứng sẽ trống và có dòng ghi chú riêng ngay dưới
bảng. Mỗi bảng chỉ nạp tối đa **20 000 dòng** (hằng `MAX_REPORT_ROWS`) để GUI
không bị treo khi phiên có hàng chục nghìn cảnh báo — CSV trong `reports/` vẫn
đầy đủ.

Nếu phiên **không có báo cáo** (tắt ngang), các trang sẽ trống và tool hiện ngay
một dòng cảnh báo màu vàng ở khối thông tin phiên — *“⚠ Phiên này không có (hoặc
thiếu) báo cáo trong `reports/` — có thể tiến trình bị tắt ngang. Bản tin thô vẫn
còn trong `mqtt/` và tra được ở tab Tìm kiếm.”* — dữ liệu thô **không mất**, chỉ
là chưa được chốt thành báo cáo.

**Tên gateway của phiên đã chốt — xoá/đổi gateway trong tab Thiết bị KHÔNG ảnh
hưởng lịch sử.** Khi bắt đầu phiên, tool ghi vào `meta.json` một **ảnh chụp**
`gateway_names = {gateway UID → tên gọi nhớ}` (`snapshot_gateway_names`) đúng như
lúc chạy. Tab Lịch sử phiên **luôn đọc ảnh chụp này** (cột **Gateway của phiên**,
tiêu đề kết quả, trang *Gateway của phiên*), nên việc **xoá gateway**, **đổi sang
gateway khác** hay sửa tên trong `devices.json` sau đó **không** làm mất/đổi tên
gateway trong các phiên đã chạy. Chỉ những phiên của bản tool **cũ** (chưa có
`gateway_names` trong `meta.json`) mới quay về tra tên **sống** trong `devices.json`
— và khi đó gateway đã xoá sẽ hiện `(chưa có tên)`, đúng thực tế vì phiên cũ
không ghi lại tên nào.

**Đặt / sửa TÊN PHIÊN thủ công.** Tên phiên (`label`) chỉ là **dữ liệu của
phiên**, ghi thẳng vào `meta.json` của chính thư mục phiên đó — **không** đổi
`run_id`, tên thư mục hay bất kỳ CSV nào, nên có thể đặt/sửa **bất cứ lúc nào**,
kể cả khi phiên **đang chạy**, và tên vẫn đi theo phiên khi bạn copy/sao lưu thư
mục. Có 2 chỗ đặt tên:

| Chỗ đặt tên | Khi nào |
|---|---|
| Ô **“Tên phiên (không bắt buộc)”** ở tab **📡 Giám sát** (cạnh nút *Bắt đầu test*) | Đặt tên ngay khi **chuẩn bị chạy** — ví dụ `test lại GW 4F9199`. Để trống cũng được; tên phiên sẽ là mã phiên tự sinh |
| Nút **✏ Đặt / đổi tên phiên** ở tab **📜 Lịch sử phiên** | Đặt tên cho phiên **đã chạy xong**, **sửa lại tên cũ**, hoặc đổi tên phiên **đang chạy**. Để trống rồi bấm OK = **xoá tên**, quay về hiện mã phiên |

Tên phiên được gọt khoảng trắng và cắt tối đa **120 ký tự**
(`normalize_session_label`); vì là dữ liệu JSON nên tiếng Việt có dấu, khoảng
trắng, dấu `/`… đều an toàn. Trong dòng lịch sử, `label` là tên người dùng đặt
(rỗng nếu chưa đặt) còn `label_text` là **tên hiển thị** (`label` nếu có, ngược
lại là `run_id`) để GUI không phải tự quyết định.

**Tìm phiên theo GATEWAY (UID hoặc tên).** Ô **Gateway:** trên thanh công cụ của
tab Lịch sử phiên lọc bảng xuống còn **những phiên đã test gateway đó**, gõ tới
đâu lọc tới đó (không cần bấm Enter):

- Tìm theo **một phần UID** (`4F9199`, `AAAABBBB`) **hoặc một phần tên gateway**
  (`MGWI-V1-26070019`, `GW Test 02`); không phân biệt HOA/thường, khớp cả khi từ
  khoá nằm giữa chuỗi.
- Khớp theo **biến thể UID**, nên gõ UID **12 hex** (`08F9199E139C`) vẫn ra phiên
  ghi UID **11 hex** mà firmware publish (`8F9199E139C`) và ngược lại.
- Tra trên **ảnh chụp `gateway_names`/`gateway_uids` của phiên**, nên vẫn tìm
  được phiên đã test một gateway **nay đã xoá khỏi tab Thiết bị**.
- **Giao (∩) với combo Lọc** — ví dụ chọn `Đã chốt báo cáo` rồi tìm một UID sẽ
  chỉ ra các phiên vừa chạy gateway đó vừa có báo cáo. Dòng đếm bên phải hiện
  `x phiên (tổng y) • … • tìm gateway: '<từ khoá>'` để luôn biết đang lọc gì.
- Bấm **✖ Xoá tìm kiếm** (hoặc nút ✖ trong ô) để bỏ từ khoá, hiện lại mọi phiên.

**Test lại một gateway trong phiên mới luôn an toàn.** Chạy lại cùng gateway
(cùng thời điểm, cùng cấu hình) **không đè** phiên cũ: `create_run_dir` luôn tạo
**thư mục phiên riêng**, tự thêm hậu tố `_1`, `_2`, … khi trùng mã phiên. Lịch sử
liệt kê **đủ mọi phiên** và mỗi phiên giữ **tên riêng** của nó — đổi tên một phiên
không ảnh hưởng phiên khác (xem `tests/smoke_test.py` §7).

**Bổ sung TÊN GATEWAY cho phiên cũ dựa vào UID (danh mục nhà máy).** UID ↔ tên
gateway là **cố định** nên phiên cũ thiếu tên có thể tra thẳng trong
danh mục nhà máy `docs/Danh sach 55 gateway.csv` (cột `uuid` = UID, cột
`series` = tên, ví dụ `MGWI-V1-26070009`) rồi ghi vào `meta.json` của phiên. Nút
**🏷 Bổ sung tên gateway theo UID** ở tab Lịch sử phiên làm việc này:

- **Chỉ ĐIỀN THIẾU:** gateway đã có tên (do bạn tự đặt hoặc do ảnh chụp cũ) được
  **giữ nguyên** — không bao giờ bị ghi đè.
- Chỉ xét **gateway có trong phiên đó** (`gateway_uids`) và **chỉ tra theo UID**,
  nên số gateway, `run_id`, tên thư mục và mọi file CSV trong `mqtt/`, `ping/`,
  `reports/` **không đổi**. Muốn quay lại như cũ chỉ cần sửa `meta.json`;
  bấm nút lần hai cũng **không ghi gì** vì đã đủ tên.
- Khớp theo **biến thể UID** (`gateway_uid_variants`), nên UID **11 hex** mà
  firmware publish (`8F9199E139C`) vẫn tra được tên trong danh mục lưu **12 hex**
  (`08F9199E139C`). UID không có trong danh mục thì **không ghi bừa**, chỉ báo lại.
- Nếu bấm nút mà **không đọc được danh mục** (thiếu file / sai cột), tool chỉ hiện
  cảnh báo chứ không sửa gì vào phiên.

Danh mục được đọc bằng `core/gateway_directory.py` (`load_gateway_catalog`,
`lookup_gateway_names`); ghi vào phiên bằng `fill_session_gateway_names(run_dir,
mapping)` trong `storage/session.py`.

**Các nút:**

| Nút | Tác dụng |
|---|---|
| **🔄 Làm mới danh sách** | Quét lại `data/runs/`, cập nhật danh sách + trạng thái (dùng khi phiên đang chạy chuyển sang “Đã dừng”) |
| **Lọc** (combo) | `Tất cả phiên` / `Đã chốt báo cáo` / `Tắt ngang / thiếu báo cáo` — lọc ngay trên bảng |
| **Gateway:** (ô tìm kiếm) | Lọc bảng theo **UID hoặc tên gateway** (khớp một phần, không phân biệt hoa/thường, khớp cả biến thể UID 11/12 hex); **giao với** combo Lọc |
| **✖ Xoá tìm kiếm** | Xoá từ khoá gateway, hiện lại mọi phiên |
| **✏ Đặt / đổi tên phiên** | Đặt/sửa/xoá **tên phiên** của phiên đang chọn (kể cả phiên đang chạy) — ghi vào `meta.json` |
| **🏷 Bổ sung tên gateway theo UID** | Tra tên trong danh mục nhà máy (`docs/Danh sach 55 gateway.csv`) và **bổ sung tên còn thiếu** vào `meta.json` của phiên đang chọn — không ghi đè tên đã có, không đổi `run_id`/CSV |
| **📂 Mở thư mục phiên** | Mở `data/runs/<run_id>/` bằng Explorer (nhấp đúp trên bảng cũng mở) |
| **📊 Mở thư mục báo cáo** | Mở `data/runs/<run_id>/reports/` (chỉ bật khi phiên có báo cáo) |
| **🔍 Xem bản tin thô trong Tìm kiếm** | Chuyển sang tab Tìm kiếm và **chọn sẵn** phiên này (`SearchTab.set_run`) |
| **🗑 Xoá phiên này** | Xoá vĩnh viễn thư mục phiên (có hỏi xác nhận). Tool **không** tự xoá dữ liệu theo thời gian — phải xoá tay. Không cho xoá phiên đang chạy |

API đọc lịch sử nằm trong `storage/session.py`:

- `collect_run_history(data_dir, active_run_id="", live_devices=None)` → `list[dict]` (một dict mỗi phiên).
- `run_history_row(session, active_run_id="", live_devices=None)` → một dict cho **một** phiên.
- `report_files(run_dir)` → danh sách file trong `reports/`.
- `snapshot_gateway_names(devices)` → `{gateway UID → tên}` để chốt vào `meta.json`.
- `session_gateway_names(run_dir)` → index `{mọi biến thể UID → tên}` từ ảnh chụp trong `meta.json`.
- `fill_session_gateway_names(run_dir, mapping, overwrite=False)` → bổ sung tên gateway theo UID vào `meta.json` của phiên; mặc định **chỉ điền chỗ thiếu**, trả `{"added", "skipped", "unmatched", "changed"}`.
- `normalize_session_label(text)` → gọt khoảng trắng + cắt còn 120 ký tự (tên phiên).
- `set_session_label(run_dir, label)` → đặt/sửa/xoá **tên phiên** của một phiên đã chạy (lưu ngay vào `meta.json`).

Danh mục UID → tên nằm trong `core/gateway_directory.py`:

- `load_gateway_catalog(path=None)` → `{UID → tên}` đọc từ CSV (cột `uuid` + `series`); mặc định `docs/Danh sach 55 gateway.csv`, trả `{}` nếu không đọc được.
- `lookup_gateway_names(source, path=None, catalog=None)` → tra tên cho một/nhiều UID (chuỗi, `GatewayDevice`, hoặc cặp `(uid, tên)`), khớp theo mọi biến thể UID.

Trong mỗi dict lịch sử, `session_gateway_names` là **ảnh chụp thật** trong
`meta.json` còn `gateway_names` là **bảng tra ưu tiên ảnh chụp** (chỉ rơi về
`live_devices` khi phiên không có ảnh chụp) — nhờ vậy giao diện không bao giờ lấy
tên từ `devices.json` để đè lên tên đã chốt của phiên cũ. `label` / `label_text`
là **tên phiên** người dùng đặt (khác hoàn toàn với tên gateway).

Mỗi dict gồm cả số liệu đã diễn giải sẵn cho GUI
(`duration_label`, `gateways_label`, `size_label`, `status_label`, …) nên GUI
không phải đọc lại `meta.json` / CSV.

