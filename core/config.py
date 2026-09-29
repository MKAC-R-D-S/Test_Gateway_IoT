"""Cấu hình & thiết bị của tool long-run monitor.

Files (đều nằm trong `config/` của tool):
  - `config.json`  : cấu hình broker + tham số test (tự sinh nếu chưa có)
  - `devices.json` : danh sách gateway cần theo dõi (Add/Edit/Delete từ GUI)

Thiết kế: dataclass + load/save nguyên tử (ghi file tạm rồi os.replace) để không
làm hỏng file cấu hình khi PC mất điện / tool crash.
"""

from __future__ import annotations

import json
import os
import re
import uuid as uuid_mod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

TOOL_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = TOOL_ROOT / "config"
DATA_DIR = TOOL_ROOT / "data"
CONFIG_PATH = CONFIG_DIR / "config.json"
DEVICES_PATH = CONFIG_DIR / "devices.json"

DEFAULT_MQTT_HOST = "45.117.170.179"
DEFAULT_MQTT_PORT = 1883
# Tool subscribe node/# (data + info + error). Bật thêm để lấy report hệ thống
# (status=online/offline) và Last Will của gateway.
DEFAULT_SUBSCRIBE_TOPICS = ["node/#"]
DEFAULT_LOG_SUBSCRIBE_TOPICS = ["+/+/log"]

PING_INTERVAL_CHOICES = [1, 2, 5, 10, 30, 60]

GATEWAY_UID_RE = re.compile(r"^[0-9A-Fa-f]{6,16}$")


# ---------------------------------------------------------------------------
# Gateway device
# ---------------------------------------------------------------------------
@dataclass
class GatewayDevice:
    """Một gateway cần theo dõi.

    `uid` = Gateway UID = `gateway_id` trong payload (`device_uuid` của firmware).
    """

    uid: str
    name: str = ""
    eth_ip: str = ""
    enabled: bool = True
    ping_enabled: bool = True
    notes: str = ""
    id: str = field(default_factory=lambda: uuid_mod.uuid4().hex)

    def display_name(self) -> str:
        return self.name or self.uid

    def validate(self) -> List[str]:
        errors: List[str] = []
        uid = (self.uid or "").strip()
        if not uid:
            errors.append("Gateway UID không được để trống.")
        elif not GATEWAY_UID_RE.match(uid):
            errors.append(
                "Gateway UID phải là hex 6-16 ký tự "
                "(firmware: String(getEfuseMac(), HEX) in hoa)."
            )
        ip = (self.eth_ip or "").strip()
        if ip and not is_valid_ipv4(ip):
            errors.append("Ethernet IP không hợp lệ (ví dụ 192.168.1.101).")
        if self.ping_enabled and not ip:
            errors.append("Bật ping thì bắt buộc nhập Ethernet IP.")
        return errors


def is_valid_ipv4(text: str) -> bool:
    parts = text.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit() or not 0 <= int(part) <= 255:
            return False
    return True


def normalize_device_uid(uid: str) -> str:
    return (uid or "").strip().upper()


# ---------------------------------------------------------------------------
# Cấu hình test
# ---------------------------------------------------------------------------
@dataclass
class TestConfig:
    # --- MQTT broker ---
    mqtt_host: str = DEFAULT_MQTT_HOST
    mqtt_port: int = DEFAULT_MQTT_PORT
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_tls: bool = False
    mqtt_tls_insecure: bool = True
    mqtt_client_id: str = ""  # rỗng -> tự sinh
    subscribe_topics: List[str] = field(default_factory=lambda: list(DEFAULT_SUBSCRIBE_TOPICS))
    extra_subscribe_topics: List[str] = field(default_factory=list)
    subscribe_gateway_log: bool = False
    log_subscribe_topics: List[str] = field(default_factory=lambda: list(DEFAULT_LOG_SUBSCRIBE_TOPICS))
    keepalive: int = 60
    reconnect_min_delay: int = 1
    reconnect_max_delay: int = 30

    # --- SEQ ---
    seq_max: int = 65535  # uint16 (main.h:245)
    seq_reset_threshold: int = 1024
    seq_out_of_order_window: int = 16
    # Coi 1 lần nhảy lùi là OUT_OF_ORDER nếu |previous - current| <= ngưỡng này,
    # ngược lại là SEQ_RESET.

    # --- Gộp bản tin trùng giữa các gateway ---
    # Nhiều gateway cùng nghe 1 node -> cùng 1 bản tin LoRa được publish N lần.
    # Các bản copy tới trong `packet_merge_window_seconds` được gộp thành 1 dòng
    # MQTT (1 bản tin logic), kèm `gateways_heard` + RSSI tốt nhất.
    # Đo trên dữ liệu thật (lệch giữa các gateway p50=31 ms, p90=43 ms):
    #   0 s -> 16 608 dòng (gần như không gộp); 2 s -> 3 478; 60 s -> 3 474
    #   (số bản tin logic thật = 3 470).
    packet_merge_window_seconds: float = 2.0
    packet_merge_enabled: bool = True
    # Số bản copy tối đa giữ lại cho 1 bản tin (chọn RSSI tốt nhất + đếm gateway).
    packet_copy_history: int = 64

    # --- Storage ---
    rotation: str = "daily"          # daily | hourly
    flush_interval_seconds: float = 2.0
    flush_max_records: int = 200
    max_queue_size: int = 200000
    gzip_old_files: bool = False

    # --- Ping ---
    ping_interval_seconds: int = 10
    ping_timeout_ms: int = 2000
    ping_count: int = 1
    ping_max_concurrent: int = 16
    # Số lần ping fail liên tiếp trước khi chính thức coi là DOWN (tránh false
    # positive do mất 1 gói ICMP đơn lẻ).
    ping_down_threshold: int = 2

    # --- Cảnh báo (alert) ---
    # Thời gian không nhận được bản tin nào từ node thì cảnh báo.
    node_silent_seconds: int = 600
    # Thời gian không nhận được BẤT KỲ bản tin nào từ gateway thì cảnh báo.
    gateway_silent_seconds: int = 300
    missing_rate_warn_percent: float = 1.0
    missing_rate_crit_percent: float = 5.0
    duplicate_rate_warn_percent: float = 2.0
    seq_reset_warn_count: int = 3
    min_packets_for_rate: int = 50
    node_silent_critical_multiplier: float = 3.0
    # Không lặp lại cùng một cảnh báo trong khoảng này.
    alert_cooldown_seconds: int = 300
    # Cảnh báo (thay vì im lặng) khi gặp gateway lạ / format UID lạ.
    alert_unknown_gateway: bool = True
    alert_unknown_node_format: bool = False

    # --- Tương quan MQTT ↔ ping ---
    # Độ dài window phân tích (mặc định 60 s = chu kỳ report hệ thống firmware).
    correlation_window_seconds: int = 60
    # Số window IM LIÊN TIẾP tối thiểu để coi là "sự kiện mất dữ liệu".
    silent_window_threshold: int = 2

    # --- Theo dõi 1 node trên nhiều gateway (bắt hụt / bắn hụt) ---
    # Bật phân tích chuyên sâu cho MỘT node: với mỗi gateway, đếm bao nhiêu bản
    # tin node đã phát mà gateway KHÔNG publish (bắt hụt) và bao nhiêu bản tin
    # gateway publish TRỄ (bắn hụt). Xem `analytics/focused_node.py`.
    focused_node_enabled: bool = False
    # UID node trọng tâm (16 hex node mới / 24 hex node cũ). Rỗng = không theo dõi.
    focused_node_uid: str = ""
    # Ngưỡng trễ (giây) cho "bắn hụt": `received_at − gateway_timestamp` vượt
    # ngưỡng này thì coi là bản tin không kịp lên MQTT (buffer SD / backfill).
    focused_node_late_seconds: float = 30.0
    # Số sự kiện trễ chi tiết giữ lại cho mỗi gateway trong report (giữ ca nặng nhất).
    focused_node_detail_limit: int = 200

    # --- GUI ---
    ui_refresh_ms: int = 500
    live_table_max_rows: int = 2000
    search_page_size: int = 500
    ui_max_alerts_shown: int = 500

    # --- Logging ---
    log_level: str = "INFO"
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 5

    def effective_subscribe_topics(self) -> List[str]:
        """Danh sách topic thực sự subscribe (đã bỏ trùng, giữ thứ tự)."""
        topics: List[str] = []
        for topic in list(self.subscribe_topics) + list(self.extra_subscribe_topics):
            topic = (topic or "").strip()
            if topic and topic not in topics:
                topics.append(topic)
        if self.subscribe_gateway_log:
            for topic in self.log_subscribe_topics:
                topic = (topic or "").strip()
                if topic and topic not in topics:
                    topics.append(topic)
        return topics or list(DEFAULT_SUBSCRIBE_TOPICS)

    @property
    def log_topic_patterns(self) -> List[str]:
        return [t.strip() for t in self.log_subscribe_topics if (t or "").strip()]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class ConfigStore:
    """Đọc/ghi `config.json` + `devices.json`."""

    def __init__(self, config_dir: Optional[Path] = None) -> None:
        self.config_dir = Path(config_dir) if config_dir else CONFIG_DIR
        self.config_path = self.config_dir / "config.json"
        self.devices_path = self.config_dir / "devices.json"

    # ---- config.json -----------------------------------------------------
    def load_config(self) -> TestConfig:
        data = _read_json(self.config_path, {})
        cfg = TestConfig()
        if isinstance(data, dict):
            for key, value in data.items():
                if not hasattr(cfg, key):
                    continue
                current = getattr(cfg, key)
                setattr(cfg, key, _coerce(value, current))
        cfg.subscribe_topics = [t for t in cfg.subscribe_topics if isinstance(t, str)]
        cfg.extra_subscribe_topics = [t for t in cfg.extra_subscribe_topics if isinstance(t, str)]
        cfg.log_subscribe_topics = [t for t in cfg.log_subscribe_topics if isinstance(t, str)]
        return cfg

    def save_config(self, cfg: TestConfig) -> None:
        _write_json(self.config_path, asdict(cfg))

    # ---- devices.json ----------------------------------------------------
    def load_devices(self) -> List[GatewayDevice]:
        data = _read_json(self.devices_path, [])
        devices: List[GatewayDevice] = []
        if not isinstance(data, list):
            return devices
        for item in data:
            if not isinstance(item, dict):
                continue
            uid = normalize_device_uid(str(item.get("uid", "")))
            if not uid:
                continue
            devices.append(
                GatewayDevice(
                    uid=uid,
                    name=str(item.get("name", "")),
                    eth_ip=str(item.get("eth_ip", "")).strip(),
                    enabled=bool(item.get("enabled", True)),
                    ping_enabled=bool(item.get("ping_enabled", True)),
                    notes=str(item.get("notes", "")),
                    id=str(item.get("id") or uuid_mod.uuid4().hex),
                )
            )
        return devices

    def save_devices(self, devices: List[GatewayDevice]) -> None:
        _write_json(self.devices_path, [asdict(d) for d in devices])

    # ---- thư mục dữ liệu -------------------------------------------------
    def data_dir(self) -> Path:
        directory = DATA_DIR
        directory.mkdir(parents=True, exist_ok=True)
        return directory


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _coerce(value: Any, template: Any) -> Any:
    """Ép kiểu giá trị đọc từ JSON về đúng kiểu của dataclass field."""
    if isinstance(template, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(template, int) and not isinstance(template, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return template
    if isinstance(template, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return template
    if isinstance(template, str):
        return value if isinstance(value, str) else str(value)
    if isinstance(template, list):
        if isinstance(value, list):
            return list(value)
        if isinstance(value, str):
            return [t.strip() for t in value.split(",") if t.strip()]
        return template
    return value


def _read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=4)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def read_json_dict(path: Path) -> Dict[str, Any]:
    data = _read_json(path, {})
    return data if isinstance(data, dict) else {}


def write_json_file(path: Path, payload: Any) -> None:
    _write_json(path, payload)
