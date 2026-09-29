"""Smoke test cho Test Gateway IoT — chạy được KHÔNG cần mạng, KHÔNG cần GUI.

Mục đích: bắt lỗi import, sai chữ ký hàm, sai tên cột CSV… chỉ trong vài giây,
trước khi mang tool ra chạy 3-4 ngày.

    python tests/smoke_test.py

Cách làm: nạp vài payload MẪU ĐÚNG NHƯ FIRMWARE (lấy từ
`docs/MQTT_PROTOCOL_ANALYSIS.md`), đẩy thẳng vào `MonitorPipeline` (không qua MQTT
thật), rồi kiểm tra:

  1. phân loại packet đúng (DATA / INFO / ERROR / LOG / DATA_NO_SEQ);
  2. SEQ được phân tích đúng (missing / duplicate / out-of-order / wrap);
  3. CSV sinh ra đủ file và đủ cột;
  4. `write_reports()` ghi được toàn bộ báo cáo tổng hợp;
  5. `RunSession` ghi/đọc lại `meta.json` được;
  6. **gộp bản tin trùng giữa nhiều gateway**: 1 node phát 1 bản tin → mọi
     gateway trong tầm đều publish, nhưng `mqtt_data` chỉ được **1 dòng**
     (`gateways_heard` = số gateway), `mqtt_missing` không bị cộng dồn sai, và
     khung lỗi trùng cũng gộp thành 1 dòng trong `mqtt_errors`.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent.parent
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from core.config import ConfigStore, GatewayDevice, TestConfig  # noqa: E402
from core.models import (  # noqa: E402
    PKT_DATA,
    PKT_DATA_NO_SEQ,
    PKT_ERROR,
    PKT_INFO,
    PKT_LOG,
    PingRecord,
    MQTTRecord,
    V_MISSING_GATEWAY,
)
from core.gateway_directory import (  # noqa: E402
    load_gateway_catalog,
    lookup_gateway_names,
)
from core.parser import MessageParser  # noqa: E402
from core.pipeline import MonitorPipeline  # noqa: E402
from core.uid import (  # noqa: E402
    filter_gateway_uid,
    gateway_name_index,
    gateway_uid_variants,
    is_hex,
    normalize_gateway_uid,
    normalize_hex_uid,
    normalize_uid,
)
from storage.csv_writer import LogManager  # noqa: E402
from storage.reader import SearchQuery, search_mqtt  # noqa: E402
from storage.schema import GATEWAY_RESULT_COLUMNS, MQTT_DATA_COLUMNS  # noqa: E402
from storage.session import (  # noqa: E402
    STATUS_STOPPED,
    collect_run_history,
    create_session,
    fill_session_gateway_names,
    open_session,
)

import csv as csv_module  # noqa: E402

GATEWAY_UID = "4F9199E139C"
NODE_UID = "1122334455667788"
OLD_NODE_UID = "343947AABBCC"           # node cụ: prefix 343947
RECEIVER_UID = "64B5B30615E18000"
# Gateway có MAC bắt đầu bằng nibble 0 → firmware in bằng
# `String(ESP.getEfuseMac(), HEX)` KHÔNG zero-pad nên chỉ còn 11 hex
# (`config_manager.cpp:7`). `devices.json` lại lưu MAC đầy đủ 12 hex.
NEAR_MISS_UID_11 = "8F9199E139C"
NEAR_MISS_UID_12 = "08F9199E139C"
# Các gateway khác cùng nghe một node (bản tin bị publish lặp trên MQTT).
# UID phải là hex ĐỘ DÀI CHẨN, nếu không parser coi bản tin là invalid
# (`core/uid.py: is_hex`).
GATEWAY_UIDS = [
    "04F9199E139C",
    "B806199E139C",
    "2007199E139C",
]
# Thời gian chờ tối đa cho mỗi kịch bản (giây).
WAIT_TIMEOUT = 15.0

FAILURES = []
CHECKS = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    """Ghi nhận 1 kiểm tra. Không raise để chạy hết được mọi trường hợp."""
    global CHECKS
    CHECKS += 1
    if condition:
        print("  [ OK ] %s" % label)
    else:
        print("  [FAIL] %s%s" % (label, (" — " + detail) if detail else ""))
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# Payload mẫu (theo docs/MQTT_PROTOCOL_ANALYSIS.md §3)
# ---------------------------------------------------------------------------
def data_payload(
    seq: int,
    node_uid: str = NODE_UID,
    receiver: str = RECEIVER_UID,
    gateway: str = GATEWAY_UID,
    rssi: int = 38,
) -> bytes:
    return json.dumps(
        {
            "gateway_id": gateway,
            "receiver_id": receiver,
            "sensor_id": node_uid,
            "rssi": rssi,
            "snr": 12,
            "timestamp": "2026-09-18T13:48:08+07:00",
            "seq": seq,
            "mcu_temp": 32.5,
            "mcu_vdd": 33.0,
            "sensor_count": 2,
            "sensor_data": [1234, 5678],
        },
        ensure_ascii=False,
    ).encode("utf-8")


def info_payload(seq0: bool = True) -> bytes:
    return json.dumps(
        {
            "gateway_id": GATEWAY_UID,
            "receiver_id": RECEIVER_UID,
            "sensor_id": NODE_UID,
            "rssi": 40,
            "snr": 11,
            "timestamp": "2026-09-18T13:48:00+07:00",
            "seq": 0 if seq0 else None,
            "mcu_temp": 31.0,
            "mcu_vdd": 33.1,
            "sensor_count": 1,
            "sensor_data": [777],
        },
        ensure_ascii=False,
    ).encode("utf-8")


def error_payload(
    gateway: str = GATEWAY_UID,
    raw_data=None,
    receiver: str = RECEIVER_UID,
) -> bytes:
    """Payload `node/error` — dùng `raw_data` (mảng byte) ĐÚNG như firmware
    (`build_error_packet()`, xem docs/MQTT_PROTOCOL_ANALYSIS.md §3.4).

    `raw_data` giống nhau giữa các gateway (cùng một khung lỗi LoRa) nên đây
    cũng là khoá để bộ gộp gom các bản copy lỗi lại thành 1 dòng.
    """
    return json.dumps(
        {
            "gateway_id": gateway,
            "receiver_id": receiver,
            "error": "CRC ERROR",
            "timestamp": "2026-09-18T13:48:09+07:00",
            "raw_data": list(raw_data if raw_data is not None else [16, 0, 0, 73, 5, 18]),
        },
        ensure_ascii=False,
    ).encode("utf-8")


def log_payload() -> bytes:
    return json.dumps(
        {
            "series": "MGWI-V1",
            "status": "online",
            "eth_ip": "192.168.1.101",
            "eth_mac": "AA:BB:CC:DD:EE:FF",
            "power_on_time": 87234,
            "timestamp": "2026-09-18T13:48:10+07:00",
        },
        ensure_ascii=False,
    ).encode("utf-8")


# ---------------------------------------------------------------------------
def gateway_devices() -> list:
    """Danh sách gateway cấu hình (nhiều gateway cùng nghe 1 vùng phủ LoRa)."""
    return [
        GatewayDevice(uid=uid, name="GW %d" % (index + 1),
                      eth_ip="192.168.1.%d" % (101 + index))
        for index, uid in enumerate(GATEWAY_UIDS)
    ]


def build_pipeline(tmp: Path, gateways=None, run_id: str = "smoke"):
    config = TestConfig()
    config.subscribe_gateway_log = True
    config.flush_interval_seconds = 0.2
    config.rotation = "daily"
    # Cửa sổ gộp rất ngắn + quét thường xuyên → test nhanh mà vẫn đi đúng đường
    # gộp bản tin (1 bản tin logic = 1 dòng) như khi chạy thật.
    config.packet_merge_window_seconds = 0.3

    devices = list(gateways or [GatewayDevice(uid=GATEWAY_UID, name="GW test",
                                               eth_ip="192.168.1.101")])
    logs = LogManager(tmp, rotation=config.rotation, flush_interval=0.02,
                      flush_max_records=10, max_queue_size=10000)
    logs.start()
    pipeline = MonitorPipeline(config, devices, logs, run_id=run_id)
    pipeline._merge_flush_interval = 0.01   # chốt bản tin gần như ngay
    pipeline.start()
    return config, logs, pipeline


def feed(pipeline: MonitorPipeline, gateways=None) -> None:
    """Đẩy các message mẫu. Thứ tự cố ý để bao phủ mọi nhánh SEQ.

    MỖI bản tin DATA được đẩy **một lần cho mỗi gateway** — đúng như thực tế: một
    node phát 1 bản tin LoRa thì mọi gateway trong tầm đều publish lên MQTT. Bộ
    gộp (`PacketDeduper`) phải gom lại thành đúng 1 dòng `mqtt_data`.

    Bản copy của gateway thứ 2 cố tình đẩy SAU một nhịp nhỏ: nhịp này ngắn hơn
    `packet_merge_window_seconds` nhưng làm mốc `received_at` khác nhau, nên nếu
    bộ gộp so sánh bằng giờ trong payload (sai) thì sẽ nổ ra nhiều dòng.
    """
    import time

    stamp = "2026-09-18T13:48:00.000+07:00"
    devices = list(gateways or [GATEWAY_UID])

    def emit(topic: str, payload, copies: int = 1) -> None:
        for index in range(copies):
            gateway = devices[index % len(devices)]
            value = payload(index, gateway) if callable(payload) else payload
            pipeline.submit_raw(stamp, topic, value)
            if index + 1 < copies:
                # 15 ms < cửa sổ gộp (0.3 s) -> vẫn phải gộp thành 1 dòng.
                time.sleep(0.015)

    copies = len(devices)

    def telemetry(seq: int):
        """Payload DATA, RSSI khác nhau theo gateway (để kiểm chọn RSSI tốt nhất)."""
        return lambda index, gw: data_payload(seq, gateway=gw, rssi=30 + 5 * index)

    # Bản tin DATA: mỗi bản tin được `copies` gateway publish lại.
    emit("node/%s/info" % NODE_UID, info_payload())                    # INFO, seq 0
    emit("node/%s/data" % NODE_UID, telemetry(1), copies)              # FIRST
    emit("node/%s/data" % NODE_UID, telemetry(2), copies)              # NORMAL
    emit("node/%s/data" % NODE_UID, telemetry(5), copies)              # MISSING 2 gói (3,4)
    emit("node/%s/data" % NODE_UID, telemetry(5), copies)              # gộp vào bản tin trên
    # Chờ hết cửa sổ gộp rồi mới gửi lại seq 5 -> bản gửi lại đi đường riêng và
    # phải được phân loại DUPLICATE (không bị gộp vào bản tin trước).
    time.sleep(0.6)
    emit("node/%s/data" % NODE_UID, telemetry(5), copies)              # DUPLICATE
    emit("node/%s/data" % NODE_UID, telemetry(4), copies)              # OUT_OF_ORDER
    # Đủ điều kiện phân tích lỗi. Khung lỗi cũng bị mọi gateway publish lại.
    emit("node/error", lambda index, gw: error_payload(gateway=gw), copies)
    emit("MGWI-V1/%s/log" % GATEWAY_UID, log_payload())                # LOG
    # node cũ info frame (22B) đi topic /data và KHÔNG có key "seq"
    # (data_process.cpp:394) -> phải phân loại DATA_NO_SEQ.
    # Nếu để "seq": 0 thì parser coi là INFO (node mới) và phép kiểm sẽ sai.
    emit("node/%s/data" % OLD_NODE_UID, json.dumps({
        "gateway_id": GATEWAY_UIDS[0], "receiver_id": RECEIVER_UID,
        "sensor_id": OLD_NODE_UID, "mcu_temp": 30.0, "mcu_vdd": 33.0,
    }, ensure_ascii=False).encode("utf-8"))
    # 1 gói không phải JSON → phải vào errors.csv, không được làm chết pipeline
    emit("node/%s/data" % NODE_UID, b"{{{ not json")


def collect_records(pipeline, logs, timeout: float = WAIT_TIMEOUT,
                    min_messages: int = 10) -> list:
    """Đợi pipeline xử lý hết rồi đọc lại CSV để kiểm tra."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if (pipeline.stats().queue_size == 0
                and pipeline.counters.total_messages >= min_messages):
            break
        time.sleep(0.02)
    # Dừng pipeline trước để nó chốt hết bản tin còn trong cửa sổ gộp.
    pipeline.stop()
    logs.flush_all()
    time.sleep(0.2)   # để writer thread ghi nốt lô cuối
    result = search_mqtt(logs.mqtt_dir, SearchQuery(limit=10000, offset=0))
    return result.rows


def read_rows(logs, name: str, limit: int = 10000) -> list:
    """Đọc mọi dòng của 1 file CSV trong thư mục mqtt (không kể header)."""
    import csv as _csv

    rows: list = []
    for path in sorted(logs.mqtt_dir.glob("%s_*.csv" % name)):
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            rows.extend(_csv.DictReader(handle))
        if len(rows) >= limit:
            break
    return rows[:limit]


# ---------------------------------------------------------------------------
def main() -> int:
    print("=" * 78)
    print("SMOKE TEST — Test Gateway IoT (không cần mạng, không cần GUI)")
    print("=" * 78)

    tmp_root = Path(tempfile.mkdtemp(prefix="testgw_smoke_"))
    try:
        # --- 1. config store round-trip ------------------------------------
        print("\n[1] ConfigStore: ghi/đọc config.json + devices.json")
        cfg_dir = tmp_root / "config"
        store = ConfigStore(cfg_dir)
        cfg = TestConfig()
        cfg.mqtt_host = "127.0.0.1"
        store.save_config(cfg)
        check(store.config_path.exists(), "config.json được tạo")
        loaded = store.load_config()
        check(loaded.mqtt_host == "127.0.0.1", "mqtt_host đọc lại đúng",
              "nhận %r" % loaded.mqtt_host)
        check(loaded.seq_max == 65535, "seq_max giữ mặc định uint16")

        # eth_ip bắt buộc khi ping_enabled (mặc định True) — thiếu IP là lỗi validate
        # có chủ đích, nên fixture hợp lệ phải có IP như devices.json mẫu.
        store.save_devices([GatewayDevice(uid=GATEWAY_UID, name="GW test",
                                          eth_ip="192.168.1.101")])
        devices = store.load_devices()
        check(len(devices) == 1 and devices[0].uid == GATEWAY_UID,
              "devices.json đọc lại đúng 1 gateway")
        check(not devices[0].validate(), "thiết bị hợp lệ không có lỗi validate",
              "nhận %s" % devices[0].validate())

        # --- 2. session ------------------------------------------------------
        print("\n[2] RunSession: tạo thư mục phiên + meta.json")
        session = create_session(
            tmp_root / "data",
            gateway_uids=[GATEWAY_UID],
            broker="127.0.0.1:1883",
            subscribed_topics=["node/#"],
            tool_version="smoke",
            gateway_names={GATEWAY_UID: "GW chốt lúc chạy"},
        )
        check(session.meta_path.exists(), "meta.json được tạo")
        check(session.run_id in session.run_dir.name, "tên thư mục chứa run_id")
        reopened = open_session(session.run_dir)
        check(reopened.broker == "127.0.0.1:1883", "meta.json đọc lại đúng broker",
              "nhận %r" % reopened.broker)
        # Yêu cầu: "xoá gateway hoặc thay thế gateway khác trong tab Thiết bị
        # thì không ảnh hưởng dữ liệu trong lịch sử phiên". ẢNH CHỤP tên
        # gateway phải được lưu vào meta.json và đọc lại nguyên vẹn.
        check(reopened.gateway_names == {GATEWAY_UID: "GW chốt lúc chạy"},
              "meta.json lưu + đọc lại ẢNH CHỤP tên gateway của phiên",
              "nhận %r" % (reopened.gateway_names,))

        # Dòng lịch sử phải dùng tên trong ảnh chụp, kể cả khi danh sách thiết bị
        # HIỆN TẠI đã rỗng (người dùng vừa xoá hết gateway) hay đã đổi sang
        # gateway khác. Đây chính là chốt chống tái phát của yêu cầu trên.
        from storage.session import run_history_row  # noqa: E402

        row_no_devices = run_history_row(reopened)
        check(row_no_devices["gateway_names"].get(GATEWAY_UID) == "GW chốt lúc chạy",
              "lịch sử giữ tên gateway dù danh sách thiết bị hiện tại RỖNG",
              "nhận %r" % (row_no_devices["gateway_names"],))
        row_replaced = run_history_row(reopened, "", [
            GatewayDevice(uid="111111111111", name="GW mới thay thế")
        ])
        check(row_replaced["gateway_names"].get(GATEWAY_UID) == "GW chốt lúc chạy",
              "lịch sử giữ tên gateway dù tab Thiết bị đã ĐỔI gateway khác",
              "nhận %r" % (row_replaced["gateway_names"],))
        check("111111111111" not in row_replaced["gateway_names"],
              "gateway mới không chen vào lịch sử phiên cũ")

        # `session_gateway_names(run_dir)` phải tra được theo CẢ biến thể UID
        # (11 hex / 12 hex) từ ảnh chụp trong meta.json.
        from storage.session import session_gateway_names  # noqa: E402

        snap_index = session_gateway_names(session.run_dir)
        variants = gateway_uid_variants(GATEWAY_UID)
        check(all(snap_index.get(v) == "GW chốt lúc chạy" for v in variants),
              "session_gateway_names tra tên theo mọi biến thể UID",
              "nhận %r" % (snap_index,))

        # --- 2b. TÊN PHIÊN người dùng đặt (label) ----------------------------
        # Yêu cầu: "có thể đặt tên phiên / sửa tên phiên thủ công". Tên phiên chỉ
        # là DỮ LIỆU trong meta.json → phải sống trong thư mục phiên, đổi được bất
        # cứ lúc nào và KHÔNG ảnh hưởng run_id / tên thư mục / dữ liệu CSV.
        from storage.session import (  # noqa: E402
            normalize_session_label,
            set_session_label,
        )

        check(reopened.display_name() == reopened.run_id,
              "phiên chưa đặt tên thì tên hiển thị = run_id")
        check(normalize_session_label("  test   lại  GW  ") == "test lại GW",
              "normalize_session_label gọt khoảng trắng",
              "nhận %r" % normalize_session_label("  test   lại  GW  "))
        check(len(normalize_session_label("x" * 500)) == 120,
              "normalize_session_label cắt đúng 120 ký tự")

        named = set_session_label(session.run_dir, "phiên test lại GW 4F9199")
        check(named.label == "phiên test lại GW 4F9199",
              "set_session_label lưu tên phiên vào meta.json",
              "nhận %r" % named.label)
        reloaded = open_session(session.run_dir)
        check(reloaded.label == "phiên test lại GW 4F9199",
              "tên phiên đọc lại nguyên vẹn từ meta.json",
              "nhận %r" % reloaded.label)
        check(reloaded.run_id == session.run_id
              and reloaded.run_dir == session.run_dir,
              "đặt tên phiên KHÔNG đổi run_id / tên thư mục")
        row_named = run_history_row(reloaded)
        check(row_named["label"] == "phiên test lại GW 4F9199",
              "dòng lịch sử mang đúng tên phiên",
              "nhận %r" % row_named.get("label"))
        check(row_named["label_text"] == "phiên test lại GW 4F9199",
              "label_text = tên phiên khi đã đặt tên")
        # Tên phiên KHÔNG được lẫn vào cột tên gateway (2 khái niệm khác nhau).
        check(row_named["gateway_names"].get(GATEWAY_UID) == "GW chốt lúc chạy",
              "đặt tên phiên không ảnh hưởng ảnh chụp tên gateway")

        # Xoá tên (chuỗi rỗng) → quay về hiển thị run_id, KHÔNG xoá dữ liệu phiên.
        cleared = set_session_label(session.run_dir, "   ")
        check(cleared.label == "" and cleared.display_name() == cleared.run_id,
              "đặt tên rỗng = xoá tên phiên, quay về run_id",
              "nhận %r" % cleared.label)
        check(cleared.meta_path.exists() and cleared.run_dir == session.run_dir,
              "xoá tên phiên không làm mất meta.json / thư mục phiên")
        # Khôi phục tên để các mục sau (nếu có) vẫn thấy dữ liệu như cũ.
        set_session_label(session.run_dir, "")

        # --- 3. pipeline: parse + SEQ (3 gateway cùng nghe) -----------------
        print("\n[3] Pipeline: parse payload thật + phân tích SEQ + gộp nhiều gateway")
        config, logs, pipeline = build_pipeline(
            session.run_dir, gateways=gateway_devices())
        try:
            feed(pipeline, GATEWAY_UIDS)
            rows = collect_records(pipeline, logs,
                                   min_messages=len(GATEWAY_UIDS) * 9)
        finally:
            pipeline.stop()
            logs.stop()

        by_type = {}
        for row in rows:
            by_type.setdefault(row.get("packet_type", ""), []).append(row)
        print("      phân loại được: %s" % {
            k: len(v) for k, v in sorted(by_type.items())
        }.__repr__())

        check(len(by_type.get(PKT_INFO, [])) == 1, "INFO frame nhận diện đúng",
              "có %d" % len(by_type.get(PKT_INFO, [])))
        # 5 bản tin logic (seq 1, 2, 5, 5, 4) — KHÔNG phải 6×3 = 18 lần publish.
        check(len(by_type.get(PKT_DATA, [])) == 5, "DATA frame nhận diện đúng",
              "có %d" % len(by_type.get(PKT_DATA, [])))
        check(len(by_type.get(PKT_ERROR, [])) == 1, "node/error nhận diện đúng",
              "có %d" % len(by_type.get(PKT_ERROR, [])))
        check(len(by_type.get(PKT_LOG, [])) == 1, "log gateway nhận diện đúng",
              "có %d" % len(by_type.get(PKT_LOG, [])))
        check(len(by_type.get(PKT_DATA_NO_SEQ, [])) >= 1,
              "node cũ (không SEQ) nhận diện đúng",
              "có %d" % len(by_type.get(PKT_DATA_NO_SEQ, [])))

        statuses = {row.get("seq_status") for row in by_type.get(PKT_DATA, [])}
        for expected in ("FIRST", "NORMAL", "MISSING", "DUPLICATE", "OUT_OF_ORDER"):
            check(expected in statuses, "SEQ status có %s" % expected,
                  "chỉ thấy %s" % sorted(statuses))

        missing_rows = [r for r in by_type.get(PKT_DATA, [])
                        if r.get("seq_status") == "MISSING"]
        check(bool(missing_rows) and int(missing_rows[0].get("missing_count") or 0) == 2,
              "phát hiện đúng 2 gói bị thiếu (seq 3,4)",
              "missing_count=%s" % (missing_rows[0].get("missing_count") if missing_rows else "?"))

        errors_rows = by_type.get(PKT_ERROR, [])
        check(all("CRC" in str(r.get("error", "")).upper() or
                  "CRC" in str(r.get("payload_json", "")).upper()
                  for r in errors_rows),
              "nội dung node/error lưu được")

        invalid = [r for r in rows if str(r.get("valid", "")).lower() in ("false", "0")]
        check(bool(invalid), "payload JSON hỏng bị đánh dấu invalid",
              "không có dòng invalid nào")

        # --- 3b. gộp bản copy từ nhiều gateway ------------------------------
        print("\n[3b] Gộp bản tin: %d gateway cùng nghe → 1 dòng/ bản tin"
              % len(GATEWAY_UIDS))
        data_rows = by_type.get(PKT_DATA, [])
        # Trong nhóm DATA còn có 1 gói JSON hỏng nằm trên topic /data → vẫn được
        # ghi vào mqtt_data để forensic, nhưng KHÔNG có cụm gateway nào. Chỉ bản
        # tin hợp lệ (đã gộp, `gateways_heard` > 0) mới mang thông tin phủ sóng.
        merged_rows = [r for r in data_rows
                       if int(r.get("gateways_heard") or 0) > 0]
        check(len(merged_rows) == 5,
              "5 bản tin DATA hợp lệ — 1 dòng cho cả cụm gateway",
              "có %d dòng gộp" % len(merged_rows))
        heard = sorted({int(r.get("gateways_heard") or 0) for r in merged_rows})
        check(heard == [len(GATEWAY_UIDS)],
              "mỗi bản tin DATA ghi nhận đúng %d gateway đã nghe" % len(GATEWAY_UIDS),
              "các giá trị gateways_heard: %s" % heard)
        check(all(
            sorted((r.get("gateways") or "").upper().split("|")) == sorted(GATEWAY_UIDS)
            for r in merged_rows
        ), "cột gateways liệt kê đủ mọi gateway đã nghe")
        # RSSI tăng dần theo chỉ số gateway (30/35/40) → gateway cuối phải thắng.
        best_ok = all(
            r.get("gateway_uid") == GATEWAY_UIDS[-1]
            and abs(float(r.get("rssi") or 0) - 40.0) < 1e-9
            for r in merged_rows
        )
        check(best_ok, "chọn gateway có RSSI tốt nhất làm đại diện",
              "nhận %s" % [(r.get("gateway_uid"), r.get("rssi")) for r in merged_rows][:3])
        check(all(str(r.get("received_at")) <= str(r.get("received_last_at"))
                  for r in merged_rows),
              "received_at ≤ received_last_at trên mọi bản tin")

        error_csv = read_rows(logs, "mqtt_errors")
        check(len(error_csv) == 1,
              "khung lỗi trùng chỉ còn 1 dòng trong mqtt_errors",
              "có %d dòng" % len(error_csv))
        if error_csv:
            check(int(error_csv[0].get("copies") or 0) == len(GATEWAY_UIDS),
                  "mqtt_errors ghi đúng số bản copy đã gộp (%d)" % len(GATEWAY_UIDS),
                  "copies=%s" % error_csv[0].get("copies"))

        missing_csv = read_rows(logs, "mqtt_missing")
        check(len(missing_csv) == 1, "khoảng thiếu SEQ không bị nhân theo gateway",
              "có %d dòng" % len(missing_csv))
        check(sum(int(r.get("missing_count") or 0) for r in missing_csv) == 2,
              "tổng số gói thiếu = 2 (một lần duy nhất cho cả cụm gateway)",
              "tổng = %d" % sum(int(r.get("missing_count") or 0) for r in missing_csv))
        check(int(getattr(pipeline, "counters").missing_packets) == 2,
              "bộ đếm missing_packets không bị nhân theo số gateway",
              "nhận %s" % getattr(pipeline, "counters").missing_packets)

        # --- 3c. gateway UID ĐỘ DÀI LẺ (firmware không zero-pad) -------------
        # `gateway_id = String(ESP.getEfuseMac(), HEX).toUpperCase()`
        # (config_manager.cpp:7) -> MAC có nibble 0 đầu bị in thiếu 1 ký tự:
        # 08F9199E139C -> "8F9199E139C" (11 hex). Trước đây `is_hex()` (bắt buộc
        # độ dài CHẴN) coi UID này là rỗng -> gateway_name = UNKNOWN_GATEWAY và
        # lọc theo "Gateway UID" không ra kết quả.
        print("\n[3c] Gateway UID độ dài lẻ (11 hex, firmware không zero-pad)")
        check(normalize_gateway_uid(NEAR_MISS_UID_11) == NEAR_MISS_UID_11,
              "normalize_gateway_uid giữ nguyên UID 11 hex",
              "nhận %r" % normalize_gateway_uid(NEAR_MISS_UID_11))
        check(normalize_hex_uid(NEAR_MISS_UID_11.lower()) == NEAR_MISS_UID_11,
              "normalize_hex_uid chấp nhận hex độ dài lẻ + in hoa",
              "nhận %r" % normalize_hex_uid(NEAR_MISS_UID_11.lower()))
        check(normalize_uid(NEAR_MISS_UID_11) == "" and not is_hex(NEAR_MISS_UID_11),
              "normalize_uid/is_hex vẫn CHẶT (không nới lỏng cho node/receiver)",
              "normalize_uid=%r is_hex=%s" % (normalize_uid(NEAR_MISS_UID_11),
                                               is_hex(NEAR_MISS_UID_11)))
        check(normalize_uid(NEAR_MISS_UID_12) == NEAR_MISS_UID_12,
              "UID 12 hex vẫn hợp lệ với normalize_uid")
        check(gateway_uid_variants(NEAR_MISS_UID_11) == {
                  NEAR_MISS_UID_11, NEAR_MISS_UID_12},
              "gateway_uid_variants gom 11 hex và 12 hex về cùng một tập",
              "nhận %s" % sorted(gateway_uid_variants(NEAR_MISS_UID_11)))
        check(filter_gateway_uid("not-a-uid") == "" and filter_gateway_uid("") == "",
              "ô tìm kiếm 'Gateway UID' chuẩn hoá giá trị không phải hex về rỗng",
              "nhận %r" % filter_gateway_uid("not-a-uid"))
        check(filter_gateway_uid(NEAR_MISS_UID_11) == NEAR_MISS_UID_11
              and filter_gateway_uid(NEAR_MISS_UID_12) == NEAR_MISS_UID_12,
              "ô tìm kiếm 'Gateway UID' nhận cả dạng 11 và 12 hex")
        check(filter_gateway_uid(NEAR_MISS_UID_11.lower()[:6]) == NEAR_MISS_UID_11[:6],
              "ô tìm kiếm 'Gateway UID' vẫn cho phép khớp một phần",
              "nhận %r" % filter_gateway_uid(NEAR_MISS_UID_11.lower()[:6]))

        # Parser + bảng tên gateway: devices.json lưu 12 hex, payload 11 hex.
        parser = MessageParser()
        odd_devices = [GatewayDevice(uid=NEAR_MISS_UID_12, name="GW pad",
                                     eth_ip="192.168.1.150")]
        name_index = MonitorPipeline._name_index(odd_devices)
        check(name_index.get(NEAR_MISS_UID_11) == "GW pad",
              "_name_index tra được tên theo dạng UID 11 hex",
              "nhận %r" % name_index.get(NEAR_MISS_UID_11))
        odd_record = parser.parse(
            "node/%s/data" % NODE_UID,
            data_payload(7, gateway=NEAR_MISS_UID_11),
            known_gateways=name_index,
        )
        check(odd_record.gateway_uid == NEAR_MISS_UID_11,
              "parser giữ gateway_uid 11 hex (không rơi về chuỗi rỗng)",
              "nhận %r" % odd_record.gateway_uid)
        check(odd_record.gateway_name == "GW pad",
              "bản tin từ gateway 11 hex KHÔNG bị gắn UNKNOWN_GATEWAY",
              "nhận %r" % odd_record.gateway_name)
        check(odd_record.valid and V_MISSING_GATEWAY not in odd_record.validation_error,
              "payload có gateway_id 11 hex là HỢP LỆ (không MISSING_GATEWAY_ID)",
              "valid=%s error=%r" % (odd_record.valid,
                                      odd_record.validation_error))

        # --- 3d. tìm kiếm phải khớp cả cột phủ sóng `gateways` ----------------
        print("\n[3d] Tìm kiếm theo 'Gateway UID' (khớp cả cột gateways)")
        search_dir = tmp_root / "search_csv"
        search_dir.mkdir(parents=True, exist_ok=True)
        search_path = search_dir / "mqtt_data_20260918.csv"
        with open(search_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv_module.writer(handle)
            writer.writerow(list(MQTT_DATA_COLUMNS))
            # Dòng gộp: gateway RSSI tốt nhất là 3CF9199E139C, NHƯNG gateway
            # 11 hex "8F9199E139C" cũng đã nghe bản tin này.
            base = {col: "" for col in MQTT_DATA_COLUMNS}
            base.update({
                "received_at": "2026-09-18T13:48:08+07:00",
                "packet_type": PKT_DATA,
                "gateway_uid": "3CF9199E139C",
                "gateway_name": "GW best",
                "gateways": "3CF9199E139C|%s" % NEAR_MISS_UID_11,
                "gateways_heard": "2",
                "node_uid": NODE_UID,
                "seq": "7",
                "valid": "1",
            })
            writer.writerow([base.get(col, "") for col in MQTT_DATA_COLUMNS])

        by_odd = search_mqtt(search_dir, SearchQuery(gateway_uid=NEAR_MISS_UID_11,
                                                     limit=10, offset=0))
        by_padded = search_mqtt(search_dir, SearchQuery(gateway_uid=NEAR_MISS_UID_12,
                                                        limit=10, offset=0))
        by_best = search_mqtt(search_dir, SearchQuery(gateway_uid="3CF9199E139C",
                                                      limit=10, offset=0))
        by_bogus = search_mqtt(search_dir, SearchQuery(gateway_uid="not-a-uid",
                                                       limit=10, offset=0))
        by_partial = search_mqtt(search_dir, SearchQuery(gateway_uid="9199E1",
                                                         limit=10, offset=0))
        check(by_odd.matched == 1,
              "lọc theo UID 11 hex tìm thấy bản tin trong cột gateways",
              "matched=%d" % by_odd.matched)
        check(by_padded.matched == 1,
              "lọc theo UID 12 hex (cách người dùng hay nhập) cũng tìm thấy",
              "matched=%d" % by_padded.matched)
        check(by_best.matched == 1, "lọc theo gateway RSSI tốt nhất vẫn đúng",
              "matched=%d" % by_best.matched)
        check(by_partial.matched == 1,
              "lọc theo một PHẦN UID vẫn khớp (placeholder của form hứa vậy)",
              "matched=%d" % by_partial.matched)
        check(by_bogus.matched == 0,
              "giá trị tìm kiếm không phải hex KHÔNG bị bỏ qua âm thầm "
              "(trước đây trả về mọi dòng)",
              "matched=%d" % by_bogus.matched)
        check(by_bogus.scanned == 1,
              "vẫn quét file bình thường khi bộ lọc không khớp",
              "scanned=%d" % by_bogus.scanned)

        # CSV cũ (ghi trước khi sửa lỗi UID 11 hex) để TRỐNG gateway_uid /
        # gateways nhưng vẫn giữ nguyên payload gốc -> phải tra được tiếp.
        legacy_dir = tmp_root / "search_legacy"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        legacy_path = legacy_dir / "mqtt_data_20260918.csv"
        with open(legacy_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv_module.writer(handle)
            writer.writerow(list(MQTT_DATA_COLUMNS))
            legacy = {col: "" for col in MQTT_DATA_COLUMNS}
            legacy.update({
                "received_at": "2026-09-18T13:48:09+07:00",
                "packet_type": PKT_DATA,
                "gateway_uid": "",
                "gateway_name": "UNKNOWN_GATEWAY",
                "gateways": "",
                "gateways_heard": "0",
                "node_uid": NODE_UID,
                "seq": "9",
                "payload_json": json.dumps({
                    "gateway_id": NEAR_MISS_UID_11,
                    "receiver_id": RECEIVER_UID,
                    "sensor_id": NODE_UID,
                    "seq": 9,
                }),
            })
            writer.writerow([legacy.get(col, "") for col in MQTT_DATA_COLUMNS])
        legacy_odd = search_mqtt(legacy_dir, SearchQuery(
            gateway_uid=NEAR_MISS_UID_11, limit=10, offset=0))
        legacy_padded = search_mqtt(legacy_dir, SearchQuery(
            gateway_uid=NEAR_MISS_UID_12, limit=10, offset=0))
        check(legacy_odd.matched == 1 and legacy_padded.matched == 1,
              "CSV của phiên bản CŨ (cột gateway rỗng) vẫn lọc được "
              "nhờ payload_json",
              "11hex=%d 12hex=%d" % (legacy_odd.matched, legacy_padded.matched))

        # --- 3e. dòng khớp phải HIỂN THỊ được gateway (không trống/unknown) ----
        # Lỗi người dùng báo: tìm "456" ra một loạt dòng nhưng cột "ID gateway"
        # trống và "Tên gateway" là unknown, vì bộ lọc tra `payload_json` còn
        # phần hiển thị thì không được điền lại.
        print("\n[3e] Kết quả tìm kiếm hiển thị đúng gateway (kể cả CSV cũ)")
        odd_name = "GW odd name"
        name_index = gateway_name_index([(NEAR_MISS_UID_12, odd_name)])
        check(name_index.get(NEAR_MISS_UID_11) == odd_name
              and name_index.get(NEAR_MISS_UID_12) == odd_name,
              "gateway_name_index tra tên theo cả dạng 11 hex và 12 hex",
              "nhận %r / %r" % (name_index.get(NEAR_MISS_UID_11),
                                name_index.get(NEAR_MISS_UID_12)))

        named = search_mqtt(legacy_dir, SearchQuery(
            gateway_uid=NEAR_MISS_UID_12, limit=10, offset=0,
            gateway_names=name_index))
        row = named.rows[0] if named.rows else {}
        check(row.get("gateway_uid") == NEAR_MISS_UID_11,
              "dòng CSV cũ được ĐIỀN LẠI `gateway_uid` từ payload_json",
              "nhận %r" % row.get("gateway_uid"))
        check(row.get("gateway_name") == odd_name,
              "dòng CSV cũ được ĐIỀN LẠI tên gateway từ danh sách thiết bị",
              "nhận %r" % row.get("gateway_name"))

        # Không có danh sách thiết bị → vẫn phải điền UID, và tên rơi về
        # UNKNOWN_GATEWAY (đúng nghĩa "gateway lạ"), KHÔNG được để trống.
        anon = search_mqtt(legacy_dir, SearchQuery(
            gateway_uid=NEAR_MISS_UID_11, limit=10, offset=0))
        anon_row = anon.rows[0] if anon.rows else {}
        check(anon_row.get("gateway_uid") == NEAR_MISS_UID_11
              and anon_row.get("gateway_name") == "UNKNOWN_GATEWAY",
              "không có cấu hình thiết bị: vẫn có ID gateway, tên = UNKNOWN",
              "uid=%r name=%r" % (anon_row.get("gateway_uid"),
                                  anon_row.get("gateway_name")))

        browse = search_mqtt(legacy_dir, SearchQuery(limit=10, offset=0,
                                                     gateway_names=name_index))
        browse_row = browse.rows[0] if browse.rows else {}
        check(browse_row.get("gateway_uid") == NEAR_MISS_UID_11
              and browse_row.get("gateway_name") == odd_name,
              "duyệt CSV cũ KHÔNG lọc gateway cũng thấy đủ ID + tên gateway",
              "uid=%r name=%r" % (browse_row.get("gateway_uid"),
                                  browse_row.get("gateway_name")))

        partial_padded = search_mqtt(legacy_dir, SearchQuery(
            gateway_uid=NEAR_MISS_UID_12[:6], limit=10, offset=0))
        check(partial_padded.matched == 1,
              "khớp một phần dạng zero-pad ('08F919') vẫn ra kết quả",
              "matched=%d" % partial_padded.matched)

        from analytics.correlation import row_gateways  # noqa: E402

        merged_gateways = row_gateways({
            "gateways": "3CF9199E139C",
            "gateway_uid": "",
            "payload_json": json.dumps({"gateway_id": NEAR_MISS_UID_11}),
        })
        check(merged_gateways == ["3CF9199E139C", NEAR_MISS_UID_12],
              "row_gateways hợp nhất cột gateways + gateway_uid + payload_json "
              "và chuẩn hoá về 12 hex",
              "nhận %s" % merged_gateways)

        # --- 3f. INFO / DATA_NO_SEQ của node ĐÃ có trạng thái SEQ ------------
        # LỖI THẬT đã gặp: `SeqState.observe_coverage()` được rút gọn còn 1 tham
        # số `gateways` (bỏ rssi/gateway_uid/snr sau khi bỏ cột "RSSI tốt nhất"),
        # nhưng `pipeline._emit_raw()` vẫn gọi theo chữ ký CŨ 4 tham số:
        #     TypeError: SeqState.observe_coverage() takes 2 positional
        #                arguments but 5 were given
        # Mỗi lần một gateway publish lại frame INFO của node đã có trạng thái
        # SEQ thì pipeline ném TypeError (bị `_run` bắt, tăng `parse_errors`, và
        # bản tin đó KHÔNG được ghi CSV). Nhánh này chỉ chạy SAU khi node đã gửi
        # DATA (lúc đó mới tồn tại `SeqState`), nên mục [3] ở trên — vốn phát
        # INFO TRƯỚC mọi DATA — không chạm tới.
        print("\n[3f] INFO / DATA_NO_SEQ của node đã có trạng thái SEQ")
        cov_dir = tmp_root / "coverage"
        _cov_config, cov_logs, cov_pipeline = build_pipeline(
            cov_dir, gateways=gateway_devices(), run_id="coverage")
        try:
            import time as _time

            _stamp = "2026-09-18T13:48:00.000+07:00"
            # 1) DATA trước. BẮT BUỘC đợi nó RA KHỎI cửa sổ gộp: chỉ khi đó
            #    `seq.feed()` mới chạy và tạo `SeqState` — chính là điều kiện
            #    kích hoạt nhánh `observe_coverage()` bên dưới. (Nếu đẩy INFO
            #    ngay lập tức thì `state is None` và nhánh lỗi không chạy.)
            cov_pipeline.submit_raw(_stamp, "node/%s/data" % NODE_UID,
                                    data_payload(1))
            deadline = _time.time() + WAIT_TIMEOUT
            while _time.time() < deadline:
                if cov_pipeline.seq.states().get(NODE_UID) is not None:
                    break
                _time.sleep(0.02)
            check(cov_pipeline.seq.states().get(NODE_UID) is not None,
                  "SeqState được tạo sau khi bản tin DATA ra khỏi cửa sổ gộp "
                  "(điều kiện kích hoạt nhánh observe_coverage)",
                  "state vẫn None sau %.0fs" % WAIT_TIMEOUT)

            # 2) INFO (seq 0) cho CHÍNH node đó → nhánh `observe_coverage()`.
            cov_pipeline.submit_raw(_stamp, "node/%s/info" % NODE_UID,
                                    info_payload())
            # 3) DATA_NO_SEQ (node cũ 22B) cho node CŨNG đã có trạng thái.
            cov_pipeline.submit_raw(_stamp, "node/%s/data" % NODE_UID,
                                    json.dumps({
                                        "gateway_id": GATEWAY_UIDS[0],
                                        "receiver_id": RECEIVER_UID,
                                        "sensor_id": NODE_UID,
                                        "mcu_temp": 30.0, "mcu_vdd": 33.0,
                                    }, ensure_ascii=False).encode("utf-8"))
            # 4) DATA_NO_SEQ cho node CHƯA có trạng thái → `state is None`.
            cov_pipeline.submit_raw(_stamp, "node/%s/data" % OLD_NODE_UID,
                                    json.dumps({
                                        "gateway_id": GATEWAY_UIDS[0],
                                        "receiver_id": RECEIVER_UID,
                                        "sensor_id": OLD_NODE_UID,
                                        "mcu_temp": 30.0, "mcu_vdd": 33.0,
                                    }, ensure_ascii=False).encode("utf-8"))

            deadline = _time.time() + WAIT_TIMEOUT
            while _time.time() < deadline:
                if (cov_pipeline.stats().queue_size == 0
                        and cov_pipeline.counters.total_messages >= 5):
                    break
                _time.sleep(0.02)
            # Bản tin DATA phải ra khỏi cửa sổ gộp mới được đếm.
            _time.sleep(max(0.5, _cov_config.packet_merge_window_seconds + 0.2))

            # Dừng pipeline trước để nó chốt hết bản tin còn trong cửa sổ gộp,
            # rồi flush writer — nếu không CSV còn nằm trong bộ đệm.
            cov_pipeline.stop()
            cov_logs.flush_all()
            _time.sleep(0.2)

            cov_counters = cov_pipeline.counters
            check(cov_counters.parse_errors == 0,
                  "node đã có SEQ nhận INFO/DATA_NO_SEQ → không lỗi xử lý "
                  "(observe_coverage đúng chữ ký)",
                  "parse_errors=%d (TypeError trong _emit_raw?)"
                  % cov_counters.parse_errors)
            check(cov_counters.total_messages >= 4,
                  "cả 4 bản tin đều được pipeline xử lý",
                  "chỉ nhận %d" % cov_counters.total_messages)

            cov_rows = read_rows(cov_logs, "mqtt_data")
            cov_types = {}
            for row in cov_rows:
                cov_types[row.get("packet_type", "")] = \
                    cov_types.get(row.get("packet_type", ""), 0) + 1
            check(cov_types.get(PKT_INFO) == 1 and cov_types.get(PKT_DATA_NO_SEQ) == 2,
                  "INFO + 2 DATA_NO_SEQ đều được GHI vào mqtt_data",
                  "phân loại: %s" % cov_types)

            state = cov_pipeline.seq.states().get(NODE_UID)
            check(state is not None and state.gateways_heard >= 1,
                  "phủ sóng của frame INFO/DATA_NO_SEQ vẫn được ghi nhận",
                  "state=%s" % (sorted(state.gateways) if state else None))
            check(state is not None and state.last_gateway != "",
                  "last_gateway được gán khi node đã có trạng thái",
                  "last_gateway=%r" % (state.last_gateway if state else None))
        finally:
            cov_pipeline.stop()
            cov_logs.stop()

        # --- 4. cột CSV khớp schema -----------------------------------------
        print("\n[4] CSV: đủ file, đủ cột theo schema")
        for name in ("mqtt_data", "mqtt_errors", "mqtt_gateway_log", "mqtt_missing",
                     "mqtt_anomalies"):
            files = sorted(logs.mqtt_dir.glob("%s_*.csv" % name))
            check(bool(files) or name in ("mqtt_missing", "mqtt_anomalies"),
                  "%s_*.csv có file" % name,
                  "không thấy file nào")
        data_files = sorted(logs.mqtt_dir.glob("mqtt_data_*.csv"))
        if data_files:
            with open(data_files[0], "r", encoding="utf-8-sig", newline="") as handle:
                header = next(csv_read(handle), [])
            check(list(header) == list(MQTT_DATA_COLUMNS),
                  "header mqtt_data khớp storage/schema.py (%d cột)" % len(MQTT_DATA_COLUMNS),
                  "nhận %d cột: %s" % (len(list(header)), list(header)))

        # --- 5. reports -------------------------------------------------------
        print("\n[5] Reports: node_summary / ping_summary / correlation / run_summary")
        config2, logs2, pipeline2 = build_pipeline(
            session.run_dir, gateways=gateway_devices())
        try:
            feed(pipeline2, GATEWAY_UIDS)
            collect_records(pipeline2, logs2, min_messages=len(GATEWAY_UIDS) * 9)
            # 1 ping thành công + 2 ping fail liên tiếp (đủ ngưỡng DOWN) để có
            # dữ liệu cho ping_summary / downtime / correlation.
            pipeline2.submit_ping(PingRecord(
                timestamp="2026-09-18T13:48:00.000+07:00", gateway_uid=GATEWAY_UID,
                gateway_name="GW test", ip_address="192.168.1.101",
                ping_success=True, latency_ms=3.2, timeout=False,
            ))
            pipeline2.submit_ping(PingRecord(
                timestamp="2026-09-18T13:48:10.000+07:00", gateway_uid=GATEWAY_UID,
                gateway_name="GW test", ip_address="192.168.1.101",
                ping_success=False, latency_ms=None, timeout=True,
                error="timeout",
            ))
            pipeline2.submit_ping(PingRecord(
                timestamp="2026-09-18T13:48:20.000+07:00", gateway_uid=GATEWAY_UID,
                gateway_name="GW test", ip_address="192.168.1.101",
                ping_success=False, latency_ms=None, timeout=True,
                error="timeout",
            ))
            import time
            time.sleep(0.5)
            reports = pipeline2.write_reports(session, extra={"reconnects": 0})
        finally:
            pipeline2.stop()
            logs2.stop()

        for report in ("node_summary", "ping_summary", "correlation_summary",
                       "mqtt_silence", "alerts", "run_summary", "gateway_result"):
            check(report in reports and Path(reports[report]).exists(),
                  "báo cáo %s được ghi" % report,
                  "có %s" % sorted(reports))

        # --- 5b. gateway_result.csv: ĐẠT/KHÔNG ĐẠT theo TỪNG tiêu chí ----------
        # Đây là thứ người dùng đọc để kết luận "gateway có đạt hay không", nên
        # phải kiểm cả HÌNH DẠNG (đủ cột, đúng thứ tự) lẫn NỘI DUNG (mọi tiêu chí
        # là PASS/FAIL/N/A, dòng TỔNG nằm cuối và khớp với các dòng gateway).
        if "gateway_result" in reports:
            with open(reports["gateway_result"], "r", encoding="utf-8-sig",
                      newline="") as handle:
                result_rows = list(csv_read(handle))
            check(len(result_rows) >= 2,
                  "gateway_result có ít nhất 1 dòng gateway + 1 dòng TỔNG",
                  "có %d dòng" % len(result_rows))
            if len(result_rows) >= 2:
                header = result_rows[0]
                check(header == GATEWAY_RESULT_COLUMNS,
                      "gateway_result đủ %d cột và ĐÚNG THỨ TỰ schema"
                      % len(GATEWAY_RESULT_COLUMNS),
                      "nhận %d cột: %s" % (len(header), header))
                body = [dict(zip(header, row)) for row in result_rows[1:]]
                check(body[-1].get("gateway_uid") == "TỔNG",
                      "dòng cuối gateway_result là dòng TỔNG",
                      "nhận %r" % body[-1].get("gateway_uid"))
                criteria_cols = [c for c in header if c.startswith("c_")]
                allowed = {"PASS", "FAIL", "N/A"}
                bad = [
                    (row.get("gateway_uid"), key, row.get(key))
                    for row in body
                    for key in criteria_cols
                    if row.get(key) not in allowed
                ]
                check(not bad,
                      "mọi tiêu chí đều là PASS / FAIL / N/A (7 tiêu chí)",
                      "giá trị lạ: %s" % bad[:5])
                check(len(criteria_cols) == 7,
                      "gateway_result có đúng 7 cột tiêu chí",
                      "có %d: %s" % (len(criteria_cols), criteria_cols))
                per_gateway = [r for r in body if r.get("gateway_uid") != "TỔNG"]
                total = body[-1]
                failed = sum(1 for r in per_gateway if r.get("result") == "KHÔNG ĐẠT")
                check(
                    total.get("result")
                    == ("KHÔNG ĐẠT" if failed else "ĐẠT"),
                    "dòng TỔNG = ĐẠT chỉ khi MỌI gateway ĐẠT",
                    "%d/%d gateway trượt, TỔNG=%r"
                    % (failed, len(per_gateway), total.get("result")),
                )
                check(
                    all(
                        r.get("result") == ("KHÔNG ĐẠT" if r.get("criteria_failed") not in ("", "0") else "ĐẠT")
                        for r in per_gateway
                    ),
                    "result từng gateway khớp số tiêu chí KHÔNG ĐẠT",
                    "%s" % [(r.get("gateway_uid"), r.get("criteria_failed"),
                             r.get("result")) for r in per_gateway][:5],
                )

        if "run_summary" in reports:
            with open(reports["run_summary"], "r", encoding="utf-8-sig",
                      newline="") as handle:
                run_rows = list(csv_read(handle))
            check(len(run_rows) == 2, "run_summary có đúng 1 dòng dữ liệu",
                  "có %d dòng" % len(run_rows))
            if len(run_rows) == 2:
                header, values = run_rows[0], dict(zip(run_rows[0], run_rows[1]))
                check(values.get("nodes_seen") == "2",
                      "run_summary đếm 2 node (mới + cũ), không nhân theo gateway",
                      "nodes_seen=%s" % values.get("nodes_seen"))
                check(values.get("gateways_seen") == str(len(GATEWAY_UIDS)),
                      "run_summary đếm đủ %d gateway đã nghe" % len(GATEWAY_UIDS),
                      "gateways_seen=%s" % values.get("gateways_seen"))
                # `gateway_names` là index biến thể (11 hex + 12 hex cho cùng 1
                # gateway) nên KHÔNG được đếm theo số khoá của index.
                check(values.get("gateways_monitored") == str(len(GATEWAY_UIDS)),
                      "run_summary đếm %d gateway đã CẤU HÌNH (không nhân theo "
                      "biến thể UID)" % len(GATEWAY_UIDS),
                      "gateways_monitored=%s" % values.get("gateways_monitored"))
                check(values.get("data_packets") == "5",
                      "run_summary đếm 5 bản tin DATA logic (không phải 15 lần publish)",
                      "data_packets=%s" % values.get("data_packets"))
                check(values.get("result") in ("ĐẠT", "KHÔNG ĐẠT"),
                      "run_summary có kết luận ĐẠT / KHÔNG ĐẠT của cả phiên",
                      "result=%r" % values.get("result"))
                check(bool(values.get("result_reason")),
                      "run_summary có lý do kết luận (result_reason)",
                      "result_reason=%r" % values.get("result_reason"))
                passed = int(values.get("gateways_passed") or 0)
                failed = int(values.get("gateways_failed") or 0)
                check(passed + failed == int(values.get("gateways_monitored") or 0),
                      "run_summary: gateway ĐẠT + KHÔNG ĐẠT = gateway đã cấu hình",
                      "%d + %d vs %s"
                      % (passed, failed, values.get("gateways_monitored")))

        # --- 6. session finish ------------------------------------------------
        print("\n[6] RunSession: chốt phiên")
        session.finish(STATUS_STOPPED, "smoke test")
        reopened = open_session(session.run_dir)
        check(reopened.status == STATUS_STOPPED, "status = STOPPED trong meta.json",
              "nhận %r" % reopened.status)
        check(bool(reopened.ended_at), "ended_at được ghi")

        # --- 7. TEST LẠI 1 GATEWAY TRONG PHIÊN MỚI (không được lỗi) -----------
        # Yêu cầu: "tránh trường hợp nếu test lại 1 gateway trong phiên mới sẽ
        # không bị lỗi". Phiên mới phải tạo THƯ MỤC RIÊNG (không ghi đè phiên cũ)
        # dù chạy cùng giây cùng gateway — tool tự thêm hậu tố `_1`, `_2`.
        print("\n[7] Test lại 1 gateway trong PHIÊN MỚI (không đè phiên cũ)")
        retest_a = create_session(
            tmp_root / "data",
            gateway_uids=[GATEWAY_UID],
            broker="127.0.0.1:1883",
            tool_version="smoke",
            gateway_names={GATEWAY_UID: "GW chốt lúc chạy"},
            label="test lại lần 1",
        )
        retest_b = create_session(
            tmp_root / "data",
            gateway_uids=[GATEWAY_UID],
            broker="127.0.0.1:1883",
            tool_version="smoke",
            gateway_names={GATEWAY_UID: "GW chốt lúc chạy"},
            label="test lại lần 2",
        )
        check(retest_a.run_dir != retest_b.run_dir,
              "chạy lại cùng gateway tạo THƯ MỤC PHIÊN RIÊNG (không đè phiên cũ)",
              "%s vs %s" % (retest_a.run_dir.name, retest_b.run_dir.name))
        check(retest_a.run_dir.exists() and retest_b.run_dir.exists(),
              "cả hai phiên test lại đều tồn tại trên đĩa")
        check(retest_a.gateway_uids == [GATEWAY_UID]
              and retest_a.gateway_names.get(GATEWAY_UID) == "GW chốt lúc chạy",
              "phiên test lại vẫn ghi đúng gateway + ảnh chụp tên")

        history = collect_run_history(tmp_root / "data")
        by_id = {row["run_id"]: row for row in history}
        check(retest_a.run_id in by_id and retest_b.run_id in by_id,
              "lịch sử liệt kê ĐỦ cả hai phiên test lại",
              "nhận %r" % sorted(by_id))
        check(by_id[retest_a.run_id]["label"] == "test lại lần 1"
              and by_id[retest_b.run_id]["label"] == "test lại lần 2",
              "mỗi phiên test lại giữ TÊN PHIÊN riêng, không lẫn nhau",
              "nhận %r / %r" % (by_id[retest_a.run_id]["label"],
                                by_id[retest_b.run_id]["label"]))
        # Đổi tên phiên cũ KHÔNG được ảnh hưởng phiên mới.
        set_session_label(retest_a.run_dir, "đổi tên phiên cũ")
        check(open_session(retest_b.run_dir).label == "test lại lần 2",
              "sửa tên 1 phiên không ảnh hưởng tên phiên khác")

        # --- 8. BỔ SUNG TÊN GATEWAY THEO UID (danh mục cố định) ---------------
        # Yêu cầu: "UID và tên gateway là cố định liên kết với nhau trong
        # docs/danh sach 55 gateway ... nên có thể bổ sung tên gateway dựa vào UID".
        # Danh mục là CSV cột `uuid` (UID) + `series` (tên); khớp theo biến thể UID
        # nên UID 11 hex do firmware publish vẫn tra được (danh mục lưu 12 hex).
        print("\n[8] Bổ sung TÊN GATEWAY theo UID (danh mục cố định)")
        catalog_file = tmp_root / "catalog.csv"
        catalog_file.write_text(
            "uuid,model,series\n"
            "08F9199E139C,MGWI-V1,MGWI-V1-26070009\n"
            "1007199E139C,MGWI-V1,MGWI-V1-26070052\n",
            encoding="utf-8",
        )
        catalog = load_gateway_catalog(catalog_file)
        check(catalog.get("08F9199E139C") == "MGWI-V1-26070009",
              "đọc danh mục CSV: cột uuid → cột series",
              "nhận %r" % catalog.get("08F9199E139C"))
        check(lookup_gateway_names([NEAR_MISS_UID_11], catalog=catalog)
              == {NEAR_MISS_UID_11: "MGWI-V1-26070009"},
              "tra tên khớp dù firmware publish %d hex còn danh mục lưu %d hex"
              " (dùng biến thể UID)" % (len(NEAR_MISS_UID_11), len(NEAR_MISS_UID_12)),
              "nhận %r" % lookup_gateway_names([NEAR_MISS_UID_11], catalog=catalog))
        check(load_gateway_catalog(tmp_root / "khong_ton_tai.csv") == {},
              "file danh mục không tồn tại → {} (không lỗi)")
        check(len(load_gateway_catalog()) >= 50,
              "danh mục mặc định (docs/Danh sach 55 gateway.csv) đọc được",
              "nhận %d gateway" % len(load_gateway_catalog()))

        legacy = create_session(
            tmp_root / "data",
            gateway_uids=[NEAR_MISS_UID_11, "1007199E139C", "DEADBEEF"],
            broker="127.0.0.1:1883",
            tool_version="smoke",
            gateway_names={NEAR_MISS_UID_11: "Tên người dùng tự đặt"},
        )
        result = fill_session_gateway_names(
            legacy.run_dir,
            lookup_gateway_names([NEAR_MISS_UID_11, "1007199E139C"], catalog=catalog),
        )
        check(result["changed"] == 1,
              "chỉ điền cho gateway ĐANG THIẾU tên (bỏ qua gateway đã có tên)",
              "nhận %r" % result)
        names = open_session(legacy.run_dir).gateway_names
        check(names.get(NEAR_MISS_UID_11) == "Tên người dùng tự đặt",
              "KHÔNG ghi đè tên người dùng đã tự đặt",
              "nhận %r" % names.get(NEAR_MISS_UID_11))
        check(names.get("1007199E139C") == "MGWI-V1-26070052",
              "gateway thiếu tên được điền từ danh mục",
              "nhận %r" % names.get("1007199E139C"))
        check("DEADBEEF" not in names,
              "gateway không tra được tên KHÔNG bị ghi bừa",
              "nhận %r" % names)
        reopened_legacy = open_session(legacy.run_dir)
        check(reopened_legacy.run_id == legacy.run_id
              and legacy.run_dir.name == legacy.run_id,
              "bổ sung tên KHÔNG đổi mã phiên / tên thư mục",
              "run_id=%r dir=%r" % (reopened_legacy.run_id, legacy.run_dir.name))
        history_row = {row["run_id"]: row
                       for row in collect_run_history(tmp_root / "data")}[legacy.run_id]
        check(history_row["gateway_names"].get(NEAR_MISS_UID_11)
              == "Tên người dùng tự đặt"
              and history_row["gateway_names"].get("1007199E139C")
              == "MGWI-V1-26070052",
              "lịch sử hiện đủ tên (tên riêng giữ nguyên + tên vừa điền)")
        check(fill_session_gateway_names(
                  legacy.run_dir,
                  lookup_gateway_names(["1007199E139C"], catalog=catalog),
              )["changed"] == 0,
              "bổ sung lần hai không ghi gì (đã đủ tên)")

    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    print("\n" + "=" * 78)
    if FAILURES:
        print("KẾT QUẢ: %d/%d kiểm tra THẤT BẠI" % (len(FAILURES), CHECKS))
        for name in FAILURES:
            print("  - %s" % name)
        print("=" * 78)
        return 1
    print("KẾT QUẢ: TẤT CẢ %d KIỂM TRA ĐỀU ĐẠT ✔" % CHECKS)
    print("=" * 78)
    return 0


def csv_read(handle):
    """Đọc header/dòng CSV bằng module csv (tránh phụ thuộc chữ ký search_mqtt)."""
    import csv as _csv

    return _csv.reader(handle)


if __name__ == "__main__":
    sys.exit(main())
