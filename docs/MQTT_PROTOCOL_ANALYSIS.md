# MQTT_PROTOCOL_ANALYSIS.md

Phân tích protocol MQTT THỰC TẾ của firmware `IoT_Type0_Gateway` (ESP32-S3),
làm cơ sở cho tool `longrun_monitor`.

> **Nguồn dữ liệu duy nhất = source firmware** (`../IoT_Type0_Gateway/src/*`).
> Mọi kết luận dưới đây đều kèm file:line. Phần nào source không định nghĩa rõ
> thì ghi `UNKNOWN – NEED USER CONFIRMATION`, **không tự suy đoán**.

---

## 1. Broker / kết nối

| Mục | Giá trị | Nguồn |
|---|---|---|
| Host | NVS key `mqtt_host` (`MQTT_HOST_KEY`) | `config_manager.cpp:57` |
| Port | NVS `mqtt_port`, default **1883** | `config_manager.cpp:58` |
| SSL | NVS `mqtt_ssl`, default false | `config_manager.cpp:59` |
| Username / Password | NVS `mqtt_user` / `mqtt_pass`, default rỗng | `config_manager.cpp:60-61` |
| ClientID | `device_uuid` | `mqtt_task.cpp:128` |
| Keepalive | 60 s | `mqtt_task.cpp:34` |
| Buffer | 2048 B in/out | `main.h:87` |
| Last Will | topic `mqtt_info.log_topic`, QoS 0, **retain = true**, payload `{"series":"<series>","status":"offline"}` | `mqtt_task.cpp:122-128` |

Hai broker độc lập trong firmware:

1. **Broker chính** (`mqtt_host`) → dữ liệu node (`node/#`) + log gateway (`<model>/<uuid>/log`).
2. **Broker ThingsBoard** (`tb_host`, mặc định 1884) → quản trị từ xa, telemetry 60 s,
   attribute, RPC, OTA, provision. **Tool long-run KHÔNG dùng broker này** (khác
   giao thức/topic, chỉ để quản trị).

Giá trị thực tế trong các tool hiện có (factory): host `45.117.170.179`,
port `1883` (`gateway_fct_test/factory_config.json`), riêng `node_forward`/`node_log`
dùng port `1885`.

> **UNKNOWN – NEED USER CONFIRMATION**: port chính thức dùng cho long-run test
> (1883 hay 1885) và tài khoản/password production.

---

## 2. Toàn bộ topic Gateway publish

Firmware **chỉ publish, không subscribe** topic nghiệp vụ (MQTT uplink-only;
ThingsBoard mới có RPC/OTA subscribe).

| # | Topic | Nội dung | Retain | QoS | Nguồn |
|---|---|---|---|---|---|
| 1 | `node/<node_uuid>/data` | Data frame node mới **và** node cũ (đã chuẩn hoá) | false | 0 | `data_process.cpp:379-380` |
| 2 | `node/<node_uuid>/info` | Info frame node mới (28 B, `seq == 0`) | false | 0 | `data_process.cpp:423-424` |
| 3 | `node/error` | Gói lỗi: CRC / length / format / invalid UUID | false | 0 | `main.h:94`, `data_processing_task.cpp:523-525` |
| 4 | `<device_model>/<device_uuid>/log` | Report hệ thống 60 s + Last Will | **true** | 0 | `config_manager.cpp:63`, `mqtt_task.cpp:122-128`, `system_monitoring_task.cpp:326` |

`<device_model>` là chuỗi trong NVS (ví dụ `MGWI-V1`) → topic log ví dụ
`MGWI-V1/4F9199E139C/log`.

**Topic subscribe cho tool: `node/#`** (bao phủ cả 3 topic dữ liệu). Nếu muốn
theo dõi cả log gateway thì subscribe thêm `<model>/+/log` — nhưng model là giá
trị runtime, nên tool dùng pattern `+/+/log` có bật/tắt trong config.

---

## 3. Payload thực tế

### 3.1 `node/<uuid>/data` — data frame (node mới)

Sinh bởi `build_new_data_msg()` (`data_process.cpp:373-418`), header ở
`set_new_header()` (`data_process.cpp:363-371`).

```json
{
  "gateway_id":  "4F9199E139C",
  "receiver_id": "64B5B30615E18000",
  "sensor_id":   "1122334455667788",
  "rssi": 38,
  "snr": 12,
  "timestamp": "2026-08-21T13:48:08+07:00",
  "seq": 51,
  "mcu_temp": 32.5,
  "mcu_vdd": 33.0,
  "sensor_count": 1,
  "sensor_value_0": [28.5, 30.1, 27.8, 31.2]
}
```

Chi tiết:

- `sensor_value_<ch>` là **mảng** (channel-major, mỗi kênh `ch_samples` mẫu,
  `ch_samples = N / sensor_count`) — `data_process.cpp:401-412`.
- `sensor_count` và các key `sensor_value_*` **chỉ xuất hiện khi có dữ liệu cảm
  biến** (`parsed.sensor_count > 0`) — `data_process.cpp:394`.
- `seq` là **uint16 little-endian** đọc từ byte 8-9 frame (`data_process.cpp:252`).
- `timestamp` = giờ gateway lấy NTP, format `%Y-%m-%dT%H:%M:%S+07:00` (`system_monitoring_task.cpp:246`).

### 3.2 Node cũ (UUID 12 byte) trên `node/<uuid>/data`

Node cũ dùng **cùng schema mới** (đã forward). Khác biệt: `sensor_id` dài 24 hex;
frame 26 B → `sensor_count = 1`; frame 42 B → `sensor_count = 4`;
frame 22 B (info node cũ) → **không có** `sensor_count`/`sensor_value_*`,
chỉ `mcu_temp`/`mcu_vdd`, và topic vẫn là `/data` (`data_processing_task.cpp:552-557`).

### 3.3 `node/<uuid>/info` — info frame node mới (28 B)

Sinh bởi `build_new_info_msg()` (`data_process.cpp:420-461`):

```json
{
  "gateway_id": "4F9199E139C",
  "receiver_id": "64B5B30615E18000",
  "sensor_id": "1122334455667788",
  "rssi": 38, "snr": 12,
  "timestamp": "2026-08-21T13:48:08+07:00",
  "seq": 0,
  "fw_version": 3,
  "frequency": 923000000,
  "bandwidth": 0,
  "tx_power": 20,
  "rx_mode": 0,
  "rx_window": 0,
  "warning_duty": 0,
  "acquire_interval": 0,
  "send_interval": 0,
  "distance": 0
}
```

`seq == 0` ⇒ info frame (`data_process.cpp:253`, `parsed.is_info_frame`).

### 3.4 `node/error`

Sinh bởi `build_error_packet()` (`data_process.cpp:462-509`). **KHÔNG có `seq`**,
**KHÔNG có `sensor_id`** (ID đã lỗi nên bỏ):

```json
{
  "gateway_id": "4F9199E139C",
  "receiver_id": "64B5B30615E18000",
  "rssi": -45, "snr": 12,
  "timestamp": "2026-08-21T13:48:08+07:00",
  "error": "CRC ERROR",
  "raw_data": [16, 0, 0, ...]
}
```

Giá trị `error` (`data_processing_task.cpp:512-517`):

| `error` | Ý nghĩa | Nguồn |
|---|---|---|
| `CRC ERROR` | CRC sai (XOR-fold với node cũ, CRC-16 Modbus với node mới) | `data_process.cpp:100-118`, `:224` |
| `PACKET LENGTH ERROR` | Độ dài frame không thuộc tập hợp lệ | `data_process.cpp:230` |
| `INVALID UUID` | ID không khớp format nhà máy (chống node "ma") | `data_process.cpp:239` |
| `INVALID FORMAT` | Frame đúng độ dài nhưng field nội bộ sai (vd `data[18] > 7`) | `data_process.cpp:307`, `:319` |

`raw_data` bị cắt tối đa **128 byte** (`main.h:91`).
**Gói lỗi có `retry = false`** và **có `timestamp`** → `decode: `data_process.cpp:472`.

### 3.5 `<model>/<uuid>/log` — report hệ thống (mỗi 60 s, retain = true)

`system_report()` (`system_monitoring_task.cpp:261-336`):

```json
{
  "uuid": "4F9199E139C",
  "model": "MGWI-V1",
  "series": "MGWI-V1-2606-01-002",
  "fw_version": "…",
  "status": "online",
  "power_on_time": "2026-09-16T08:21:50+07:00",
  "timestamp": "2026-09-18T10:00:00+07:00",
  "eth_dhcp": true, "eth_mac": "…", "eth_ip": "192.168.1.100",
  "eth_subnet": "…", "eth_gateway": "…", "eth_dns": "…",
  "wifi_*": "… (nếu dùng wifi)",
  "free_heap_%": 71.5,
  "free_psram_%": 88.2
}
```

Khoá chính xác (`config_manager.h:10-33`): `uuid`, `model`, `series`,
`fw_version`, `eth_dhcp`, `eth_mac`, `eth_ip`, `eth_subnet`, `eth_gateway`,
`eth_dns`, và bộ `wifi_*` tương ứng.

**Offline detection:** Last Will đẩy `{"series":"<series>","status":"offline"}`
(retain) lên chính topic này khi gateway mất kết nối (`mqtt_task.cpp:122-128`).
Report định kỳ đẩy `"status":"online"`.

> `status == "offline"` trong file CSV ping là **bằng chứng cấp gateway** cho
> downtime (khác với ping FAIL chỉ là bằng chứng đường mạng).

Lưu ý: report `retry = false` (`main.cpp:232`) → khi MQTT mất kết nối thì bị bỏ,
không ghi buffer offline. Các bản tin `/data` có `retry = true` → khi MQTT
offline gateway **buffer vào SD** rồi gửi lại sau → **có thể xuất hiện burst
dữ liệu trễ (backfill) sau khi reconnect**; `timestamp` trong payload vẫn là giờ
gốc gateway, còn giờ PC nhận sẽ bị lệch. Tool phải ghi cả 2 (xem §7).

---

## 4. Cách phân biệt DATA / INFO / ERROR / LOG

| Loại | Cách phân biệt (thứ tự ưu tiên) |
|---|---|
| ERROR | `topic == "node/error"` |
| INFO | topic kết thúc `/info` **hoặc** `seq == 0` trên `/data` (info node mới) |
| DATA | topic kết thúc `/data` **và** `seq != 0` **hoặc** frame info node cũ 22 B (có `/data` nhưng không có `seq`) |
| LOG | topic khớp `<model>/<uuid>/log` (có key `status`) |

Điểm cần chú ý: **node cũ gửi info frame 22 B lên `/data` không có `seq`**
(`data_processing_task.cpp:552-557` + `data_process.cpp:394`). Vì vậy *không thể*
coi mọi message `/data` đều có `seq`. Những bản tin này:

- vẫn là telemetry hợp lệ (CRC đã pass ở gateway) → ghi CSV, `packet_type = DATA_NO_SEQ`;
- **không đưa vào** `SequenceTracker` (không có `seq`);
- không tính vào delivery/missing.

---

## 5. Xác định các UID (bắt buộc đúng, không đoán)

| UID | Định nghĩa | Format | Nguồn |
|---|---|---|---|
| **Gateway UID** | `String(ESP.getEfuseMac(), HEX).toUpperCase()` | Hex in hoa, 11–12 ký tự (thực tế 11 vì ESP32 in HEX bỏ nibble 0 đầu) | `config_manager.cpp:7-8`, `data_process.cpp:365` (`gateway_id = device_uuid`) |
| **Receiver UID** | UID 8 byte của module thu LoRa Type1 (RAK3172 / Lierda WB26) | **16 hex** in hoa | `main.h:220` (`uint8_t receiver_id[8]`), `data_process.cpp:366` (`bytes_to_hex(packet.receiver_id, 8)`) |
| **Node/Sensor UID** | `parsed.uuid` — 8 byte (node mới) hoặc 12 byte (node cũ) lấy từ đầu frame LoRa | **16 hex** hoặc **24 hex** in hoa | `data_process.cpp:227`, `:228-230` |

`device_uuid` **không** phải MAC chuẩn hoá: `ESP.getEfuseMac()` trả UID 6 byte
dạng số → `String(..., HEX)` cắt nibble 0 đầu. Ví dụ MAC efuse
`04:F9:19:9E:13:9C` → `4F9199E139C` (11 ký tự).
→ Tool so khớp gateway **case-insensitive** và chấp nhận **6–16 hex**.

> ⚠️ **Độ dài LẺ là hợp lệ** — đây không phải dữ liệu lỗi. `04:F9:19:9E:13:9C`
> → 11 ký tự; MAC có nibble 0 đầu như `08:F9:19:9E:13:9C` → `8F9199E139C`.
> Vì vậy `core/uid.py` dùng `normalize_gateway_uid()` / `normalize_hex_uid()`
> (chấp nhận hex độ dài lẻ) cho **gateway UID**, còn `normalize_uid()` /
> `is_hex()` (bắt buộc độ dài **chẵn**) chỉ dùng cho `receiver_id` / `sensor_id`
> — hai UID này do firmware zero-pad (`bytes_to_hex`, `data_process.cpp:366/227`)
> nên nếu lẻ ký tự thì đúng là dữ liệu hỏng.
> Khi cần khớp `8F9199E139C` (firmware in) với `08F9199E139C` (MAC đầy đủ trong
> `devices.json`) thì dùng `gateway_uid_variants()` / `format_gateway_uid()` —
> **không so chuỗi trực tiếp**.

Ba chỗ trong tool phụ thuộc trực tiếp vào quy tắc trên (đã kiểm bằng test):

1. **Tên gateway** — `gateway_name_index(devices)` (`core/uid.py`) tra tên theo
   *mọi* biến thể UID, nên UID 11 hex vẫn ra tên thật, không rơi vào
   `UNKNOWN_GATEWAY`.
2. **Tìm kiếm** (`storage/reader.py`) — lọc "Gateway UID" khớp `gateway_uid`,
   `gateways` (danh sách pipe-joined) **và** `payload_json`; dòng khớp qua
   `payload_json` được **tính lại** `gateway_uid`/`gateway_name` trước khi hiển
   thị. Nhờ vậy CSV của phiên bản cũ (ghi trước khi có bản sửa 11-hex) — nơi
   `gateway_uid`/`gateways` bị để trống do bản tin 11-hex từng bị xếp `CRC ERROR`
   / `MISSING_GATEWAY_ID` / `INVALID UUID` — vẫn tra cứu được, và cột "ID gateway"
   / "Tên gateway" **không** còn trống hay `unknown`.
3. **Tương quan ping ↔ MQTT** (`analytics/correlation.py`) — `row_gateways()`
   gộp cột `gateways` + `gateway_uid` + `payload_json` rồi chuẩn hoá về 12 hex, nên
   cùng một gateway ở hai dạng 11/12 hex **không** bị đếm thành hai và không bị báo
   sai `GATEWAY_SILENT`.

Quy ước format ID nhà máy — dùng để phát hiện node "ma" và để tool phân loại
(`data_process.cpp:129-165`):

- Node mới, bản tin mới: `UID 8B`, trong đó `hex[8:16] == "15E18000"`.
- Node mới, bản tin cũ: `UUID 12B`, trong đó `hex[8:24] == "15E18000FC8B3004"`.
- Node cũ (STM32L1): `UUID 12B`, `hex[0:6] == "343947"`, `hex[8:16] == "33303031"`,
  `hex[18:20] == "00"`, `hex[22:24] == "00"`.

> **UNKNOWN – NEED USER CONFIRMATION**: whitelist (`wl`) cho phép nhận frame có
> ID lệch format (`packet.force`, `data_process.cpp:236-238`) ⇒ có thể xuất hiện
> UID không khớp 3 quy ước trên. Tool phân loại các UID này là
> `UNKNOWN_FORMAT` nhưng **vẫn ghi dữ liệu**, không drop.

---

## 6. Quy tắc SEQ

| Thuộc tính | Kết luận | Nguồn |
|---|---|---|
| Kiểu dữ liệu | **uint16** | `LoRa_Packet`/`ParsedNodeData.seq` là `uint16_t` (`main.h:245`), đọc LE 2 byte (`data_process.cpp:252`) |
| Phạm vi | 0 … 65535 **bao gồm 0** | như trên |
| `seq == 0` nghĩa là gì | **Info frame** (`is_info_frame = (parsed.seq == 0)`) — node mới | `data_process.cpp:253` |
| Node cũ | `seq` đọc từ byte 12-13, **cũng là uint16**; info 22 B bị gán `seq = 0` | `data_process.cpp:310`, `:295-297` |
| Reset / reboot behavior | **UNKNOWN – NEED USER CONFIRMATION**. Firmware không reset/khởi tạo SEQ; giá trị do node sinh. Không có tài liệu mô tả node reset SEQ về 0 hay tiếp tục wrap. | không tìm thấy trong `src/` |

Hệ quả thiết kế (bắt buộc):

1. **SEQ=0 không được coi là missing/reboot** — nó là info frame (§ Phần 12).
2. 0 chỉ dùng được như một giá trị data seq **nếu** node cũ 26/42 B (không có
   info frame) → phải phân biệt theo `packet_type`, không theo `seq == 0` một mình.
3. `SEQ_MAX = 65535` là mặc định; wrap-around `65534 → 65535 → 0 → 1` **hợp lệ**.

---

## 7. Semantics khi ghi CSV (data integrity)

- `received_at` = giờ **PC** khi nhận MQTT (nguồn: `MQTTMessageResult.received_at`).
- `gateway_timestamp` = `payload["timestamp"]` (giờ gateway) — **không được dùng
  thay** `received_at`.
- `mqtt_timestamp` = giờ broker nếu có (helper giữ chỗ; hiện tại lấy bằng
  `received_at` vì firmware không gửi metadata broker).

Hai giá trị lệch nhau nhiều ⇒ network delay hoặc **backfill từ buffer SD** sau
khi gateway reconnect (`mqtt_task.cpp:141-158`).

### 7.1 Một bản tin logic = một dòng CSV

Vì **nhiều gateway thu cùng một vùng phủ LoRa**, một bản tin node phát ra được
**mọi** gateway trong tầm publish lại lên MQTT (nội dung trùng nhau, chỉ khác
`gateway_id`, `rssi`, `snr`). Tool vì thế gộp các bản copy lại:

- **DATA** → khoá `(node_uid, seq)`; **ERROR** → khoá `(error, raw_data)`.
- Cửa sổ gộp `packet_merge_window_seconds` (mặc định **2.0 s**), đo bằng
  `time.monotonic()` — **thời điểm tool nhận**, không phải `timestamp` trong
  payload (gateway có thể lệch giờ / backfill dữ liệu cũ).
- Bản copy “đại diện” = gateway có **RSSI tốt nhất**; cụm gateway ghi ở
  `gateways_heard` (số lượng) và `gateways` (danh sách UID, cách nhau `|`).
- `received_at` = bản copy **sớm nhất**, `received_last_at` = bản copy **muộn nhất**.

Hệ quả về mặt đọc số liệu:

- **Một dòng CSV không phải một lần publish.** Đừng dùng số dòng để suy ra số bản
  tin node đã phát.
- `seq`, `seq_status`, `missing_count`, `delivery_rate` và `node_summary` đều
  tính trên **bản tin logic** nên không nhân theo số gateway.
- Bản tin chỉ được ghi khi **ra khỏi cửa sổ gộp** → lúc đang chạy, các bản tin
  mới nhất có thể chưa xuất hiện (đã `flush_all()` khi dừng phiên).
- Payload JSON **hỏng** (không parse được) → `packet_type = UNKNOWN`,
  `valid = 0`; vẫn ghi vào `mqtt_data` để forensic nhưng **không** tính vào
  `data_packets`.

### 7.2 Node trọng tâm — bắt hụt / bắn hụt (`focused_node_*.csv`)

Tool có thể khoá vào **một node** và đo, **theo từng gateway**, mức độ bắt hụt
và bắn hụt của gateway đó. Cơ chế dựa hoàn toàn trên MQTT (node không gửi thêm
gì):

- **Tập tham chiếu**: hợp của mọi `seq` mà **bất kỳ** gateway nào publish cho
  node đó (bitmap ≤ 65536 byte/gateway). Đây là **chặn dưới** số bản tin node
  thực sự phát — nếu *tất cả* gateway đều hụt cùng một seq, tool không thấy.
- **BẮT HỤT** (`missed_catch`) = `|tham chiếu| − số seq gateway đó publish`.
  Nghĩa: node đã phát (vì gateway khác nghe) mà gateway này **không** publish.
  Là **chặn trên** của bắt hụt thật: link down / gateway đang backfill SD cũng
  làm tăng con số này (không phải mất RF).
- **BẮN HỤT** (`late_packets`) = số bản copy có
  `received_at − gateway_timestamp > focused_node_late_seconds` (mặc định 30 s).
  Nghĩa: gateway **có** publish nhưng **trễ** (buffer SD / backfill, không kịp
  gửi realtime). Đo **theo từng lần publish**, không theo seq.
- `delay_seconds` âm được **giữ nguyên** = đồng hồ gateway lệch về phía trước,
  không phải lỗi.
- Gateway được cấu hình nhưng **không** publish bản tin nào của node vẫn được in
  ra với bắt hụt 100% + cờ `gateways_silent` (nghi mất tầm sóng) — tránh
  false-negative "vắng mặt = không có vấn đề".
- **Kiểu ngắt quãng** (`gap_pattern`): tool quét lại bitmap `seq` và tách phần hụt
  thành từng **khoảng liên tiếp** — `gap_runs` (số khoảng), `gap_largest` (khoảng
  dài nhất), `gap_pattern` (câu kết luận tiếng Việt). Một khoảng duy nhất ⇒ hụt
  **liên tục một mạch** (nghi mất tầm sóng / offline / mất MQTT một mạch); nhiều
  khoảng ⇒ hụt **rải rác** (nghi mất gói RF lẻ tẻ, link vẫn sống). Dòng TỔNG cũng
  phân loại bằng cùng tiêu chí. Phân loại này dùng chung cho mọi gateway nên dòng
  TỔNG và dòng chi tiết luôn nhất quán. Quét bitmap đắt (~0,4 s cấu hình lớn) nên
  kết quả được **cache** và chỉ tính lại lúc xuất báo cáo / theo chu kỳ.

**Giới hạn cốt lõi**: MQTT **không** chứng minh được "gateway nghe nhưng chọn
không publish"; vì vậy bắt hụt luôn là **chặn trên** và bắt hụt + bắn hụt là
**chặn dưới** của tổng số hụt. Cách đọc đầy đủ ở `README.md` §Node trọng tâm.

---

## 8. CRC — tool KHÔNG kiểm tra lại

Gateway đã kiểm CRC **trước khi** publish (`parse_node_packet()`,
`data_process.cpp:214-241`):

- Node cũ 22/26/42 B: XOR-fold 16 bit (`LoRa_CRC_check`, `data_process.cpp:100-118`) —
  thử cả 2 thứ tự byte CRC.
- Node mới data (17 + 4N B): **CRC-16 Modbus** trên `len-2` byte đầu, CRC 2 byte LE
  (`data_process.cpp:208-212`, `calc_crc16_modbus` `:122-137`).
- Node mới info 28 B: CRC-16 Modbus trên 26 byte đầu (`data_process.cpp:196-199`).

CRC fail ⇒ **không publish `/data`**, publish `node/error`.

**Kết luận: MQTT payload KHÔNG chứa raw LoRa frame cho data frame, không chứa
cờ `crc_ok`, và không chứa CRC của payload JSON.**
⇒ Tool **KHÔNG tự tính CRC** (sẽ là kiểm tra giả). Tool chỉ:

1. nhận message thành công,
2. JSON parse thành công,
3. có `gateway_id` + `receiver_id` + `sensor_id`,
4. → `valid = true` với lý do `CRC_VERIFIED_UPSTREAM`.

`node/error` với `error == "CRC ERROR"` được đếm riêng ở
`mqtt_errors_*.csv` và **không** tính vào `valid_packets`/delivery.

---

## 9. Những gì firmware KHÔNG cung cấp (để tool không ảo giác)

- Không có raw frame trong `/data` (chỉ trong `/error`, cắt 128 B).
- Không có số thứ tự message MQTT (không dùng để đếm missing).
- Không có trường `lost`/`retry` trong payload.
- Không có topic dạng `node/<uuid>/status`.
- Không có danh sách node dự kiến ⇒ **không thể** biết trước tổng số packet kỳ
  vọng; missing chỉ phát hiện được **tương quan giữa 2 seq liên tiếp** mà node gửi.
  Vì vậy delivery rate phải định nghĩa rõ (xem `README.md` §Delivery rate).
- Không có tín hiệu "gateway đã nghe nhưng không publish" ⇒ **bắt hụt của một
  gateway chỉ là chặn trên**; chỉ chắc chắn khi có gateway khác publish cùng seq
  (xem §7.2).

---

## 10. Tổng hợp assumption cần xác nhận

| # | Nội dung | Trạng thái |
|---|---|---|
| A1 | Broker host/port/credential cho long-run test | UNKNOWN – NEED USER CONFIRMATION |
| A2 | Node reset SEQ về 0 hay wrap liên tục sau reboot | UNKNOWN – NEED USER CONFIRMATION |
| A3 | Chu kỳ gửi (`send_interval`) mong đợi của từng node để tính "missing do mất packet" so với "missing do node ngừng gửi" | UNKNOWN – NEED USER CONFIRMATION |
| A4 | Ngưỡng phân biệt OUT_OF_ORDER vs SEQ_RESET (mặc định tool: khoảng cách ≤ 16 coi là out-of-order trước) | Configurable, mặc định theo tool |
| A5 | Có dùng topic log `+/+/log` để lấy `status=offline` không | Security/traffic decision |
| A6 | Node cũ 22 B info trên `/data` có tính vào thống kê data không (tool mặc định: **không**) | Mặc định `DATA_NO_SEQ`, có thể đổi qua config |
| A7 | Ngưỡng bắn hụt `focused_node_late_seconds` (mặc định **30 s**) — phụ thuộc chu kỳ gửi và mức chịu trễ backfill | Configurable `focused_node_late_seconds` |
| A8 | Đồng hồ của các gateway có được đồng bộ (NTP) không — ảnh hưởng trực tiếp `delay_seconds` và số bắn hụt | UNKNOWN – NEED USER CONFIRMATION |
