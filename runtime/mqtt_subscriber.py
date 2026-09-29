"""MQTT subscriber cho Test Gateway IoT (paho-mqtt v2).

Đặc điểm bắt buộc cho test 3-4 ngày KHÔNG giám sát:

- **Chỉ đẩy raw message vào queue trong callback** — không parse nặng, không I/O
  (callback paho chạy trên network thread; block ở đây sẽ làm broker ngắt kết nối).
- **Tự reconnect vô hạn** với backoff (1 s → 30 s), không bao giờ bỏ cuộc.
- **Đếm reconnect** để báo cáo độ ổn định đường truyền.
- **Resubscribe** sau mỗi lần reconnect (paho không tự làm khi clean_session).
- **Callback API v2** nhưng tương thích cả v1 (paho 1.x/2.x).

Tool KHÔNG publish gì — firmware là MQTT uplink-only (docs §2).
"""

from __future__ import annotations

import logging
import queue
import ssl
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

try:  # paho-mqtt >= 2.0
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion

    PAHO_V2 = hasattr(mqtt, "CallbackAPIVersion")
except ImportError:  # pragma: no cover - thiếu dependency
    mqtt = None  # type: ignore[assignment]
    PAHO_V2 = False

from core.models import (
    MQTT_CONNECTED,
    MQTT_CONNECTING,
    MQTT_DISCONNECTED,
    MQTT_RECONNECTING,
)

# Một message thô: (received_at, topic, payload_bytes)
RawMessage = Tuple[str, str, bytes]


@dataclass
class MqttStats:
    connected: bool = False
    state: str = MQTT_DISCONNECTED
    connects: int = 0
    disconnects: int = 0
    reconnects: int = 0
    messages: int = 0
    bytes_received: int = 0
    last_message_at: str = ""
    last_error: str = ""
    connected_since: str = ""
    subscriptions: List[str] = field(default_factory=list)

    def uptime_seconds(self) -> float:
        if not self.connected or not self.connected_since:
            return 0.0
        try:
            started = datetime.fromisoformat(self.connected_since)
        except ValueError:
            return 0.0
        try:
            return max(0.0, (datetime.now(started.tzinfo) - started).total_seconds())
        except (TypeError, ValueError):
            return 0.0


class MqttSubscriber:
    """Bọc paho-mqtt với reconnect vô hạn + hàng đợi message thô."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        client_id: str = "",
        username: str = "",
        password: str = "",
        use_tls: bool = False,
        tls_insecure: bool = True,
        keepalive: int = 60,
        topics: Optional[List[str]] = None,
        queue_max: int = 200000,
        reconnect_min: int = 1,
        reconnect_max: int = 30,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.username = username or ""
        self.password = password or ""
        self.use_tls = bool(use_tls)
        self.tls_insecure = bool(tls_insecure)
        self.keepalive = max(5, int(keepalive))
        self.topics = list(topics or ["node/#"])
        self.reconnect_min = max(1, int(reconnect_min))
        self.reconnect_max = max(self.reconnect_min, int(reconnect_max))
        self.client_id = client_id or ("testgw_%d" % int(time.time()))

        self.queue: "queue.Queue[RawMessage]" = queue.Queue(maxsize=max(1000, queue_max))
        self.stats = MqttStats()
        self.dropped = 0
        self._client: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._first_connect_done = False
        self.on_event: Optional[Callable[[str, str], None]] = None

    # ------------------------------------------------------------------ API
    def start(self) -> None:
        if mqtt is None:
            raise RuntimeError(
                "Thiếu paho-mqtt. Cài đặt: pip install 'paho-mqtt>=2.1.0'"
            )
        if self._thread is not None:
            return
        self._stop.clear()
        self._client = self._build_client()
        self.stats.state = MQTT_CONNECTING
        self._thread = threading.Thread(
            target=self._run, name="mqtt-network", daemon=True
        )
        self._thread.start()
        log.info("MQTT subscriber khởi động tới %s:%d", self.host, self.port)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        client, self._client = self._client, None
        if client is not None:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001 - shutdown phải luôn thành công
                pass
            try:
                client.loop_stop()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self.stats.connected = False
        self.stats.state = MQTT_DISCONNECTED

    def get(self, timeout: float = 1.0) -> Optional[RawMessage]:
        """Lấy 1 message thô từ queue (thread-safe)."""
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self, max_items: int = 10000) -> List[RawMessage]:
        """Lấy tối đa `max_items` message đang chờ (dùng khi GUI bật lại)."""
        items: List[RawMessage] = []
        for _ in range(max_items):
            try:
                items.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return items

    def subscribe(self, topic: str) -> None:
        """Thêm topic và resubscribe ngay (nếu đang kết nối)."""
        topic = (topic or "").strip()
        if not topic or topic in self.topics:
            return
        self.topics.append(topic)
        client = self._client
        if client is not None and self.stats.connected:
            try:
                client.subscribe(topic, qos=0)
                self.stats.subscriptions = list(self.topics)
            except Exception as exc:  # noqa: BLE001
                log.warning("subscribe(%s) thất bại: %s", topic, exc)

    def set_topics(self, topics: List[str]) -> None:
        self.topics = [t for t in topics if (t or "").strip()]
        client = self._client
        if client is None or not self.stats.connected:
            return
        try:
            client.unsubscribe("#")
        except Exception:  # noqa: BLE001
            pass
        for topic in self.topics:
            try:
                client.subscribe(topic, qos=0)
            except Exception as exc:  # noqa: BLE001
                log.warning("subscribe(%s) thất bại: %s", topic, exc)
        self.stats.subscriptions = list(self.topics)

    def is_connected(self) -> bool:
        return bool(self.stats.connected)

    # ------------------------------------------------------- paho callbacks
    def _build_client(self) -> Any:
        if PAHO_V2:
            client = mqtt.Client(
                callback_api_version=CallbackAPIVersion.VERSION2,
                client_id=self.client_id,
                protocol=mqtt.MQTTv311,
                clean_session=True,
            )
        else:  # pragma: no cover - paho 1.x
            client = mqtt.Client(
                client_id=self.client_id, protocol=mqtt.MQTTv311, clean_session=True
            )
        if self.username:
            client.username_pw_set(self.username, self.password)
        if self.use_tls:
            client.tls_set(cert_reqs=ssl.CERT_NONE if self.tls_insecure else ssl.CERT_REQUIRED)
            client.tls_insecure_set(self.tls_insecure)
        # Backoff do paho quản lý (đơn vị giây)
        client.reconnect_delay_set(min_delay=self.reconnect_min, max_delay=self.reconnect_max)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.on_log = None
        # Không dùng on_socket_open... để giảm phụ thuộc version.
        return client

    def _on_connect(self, client, userdata, flags=None, reason_code=None, properties=None):
        # paho v1: (client, userdata, flags, rc) ; v2: (client, userdata, flags, reason_code, properties)
        code = reason_code if reason_code is not None else flags
        if _is_success(code):
            with self._lock:
                self.stats.connected = True
                self.stats.state = MQTT_CONNECTED
                self.stats.connects += 1
                if self._first_connect_done:
                    self.stats.reconnects += 1
                self._first_connect_done = True
                self.stats.connected_since = datetime.now().astimezone().isoformat(
                    timespec="seconds"
                )
                self.stats.last_error = ""
            for topic in self.topics:
                try:
                    client.subscribe(topic, qos=0)
                except Exception as exc:  # noqa: BLE001
                    log.warning("subscribe(%s) thất bại: %s", topic, exc)
            self.stats.subscriptions = list(self.topics)
            log.info("MQTT đã kết nối %s:%d, subscribe %s", self.host, self.port, self.topics)
            self._emit("connect", "Đã kết nối broker, subscribe %s" % ", ".join(self.topics))
        else:
            self.stats.last_error = "CONNECT_REFUSED: %s" % (code,)
            log.error("MQTT bị từ chối kết nối: %s", code)
            self._emit("error", "Broker từ chối kết nối: %s" % (code,))

    def _on_disconnect(self, client, userdata, *args):
        with self._lock:
            self.stats.connected = False
            self.stats.disconnects += 1
            self.stats.state = MQTT_DISCONNECTED if self._stop.is_set() else MQTT_RECONNECTING
        if self._stop.is_set():
            log.info("MQTT đã ngắt kết nối (theo yêu cầu)")
            self._emit("disconnect", "Đã ngắt kết nối broker")
        else:
            log.warning("MQTT mất kết nối — paho sẽ tự reconnect (backoff %ds→%ds)",
                        self.reconnect_min, self.reconnect_max)
            self._emit("reconnecting", "Mất kết nối — đang thử lại…")

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = message.payload if isinstance(message.payload, (bytes, bytearray)) else bytes(message.payload or b"")
        except (TypeError, ValueError):
            payload = b""
        item = (datetime.now().astimezone().isoformat(timespec="milliseconds"),
                str(message.topic), bytes(payload))
        with self._lock:
            self.stats.messages += 1
            self.stats.bytes_received += len(payload)
            self.stats.last_message_at = item[0]
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            # Back-pressure: pipeline không theo kịp → bỏ và ĐẾM (không chặn callback)
            self.dropped += 1
            if self.dropped % 1000 == 1:
                log.error("MQTT queue đầy — đã bỏ %d message", self.dropped)

    # ------------------------------------------------------------- network
    def _run(self) -> None:
        client = self._client
        if client is None:
            return
        backoff = self.reconnect_min
        while not self._stop.is_set():
            try:
                client.connect(self.host, self.port, keepalive=self.keepalive)
                client.loop_forever(retry_first_connection=True)
                # loop_forever() chỉ trả về khi client.disconnect() được gọi
                if self._stop.is_set():
                    break
            except Exception as exc:  # noqa: BLE001 - phải sống sót mọi lỗi mạng
                self.stats.last_error = "%s: %s" % (type(exc).__name__, exc)
                log.error("MQTT lỗi kết nối (%s) — thử lại sau %ds", exc, backoff)
                self._emit("error", "Lỗi kết nối: %s" % exc)

            if self._stop.is_set():
                break
            # paho tự reconnect bên trong loop_forever; tới đây nghĩa là vòng loop
            # đã kết thúc → nghỉ theo backoff rồi dựng lại client sạch.
            time.sleep(backoff)
            backoff = min(self.reconnect_max, backoff * 2)
            with self._lock:
                if self.stats.connected:
                    backoff = self.reconnect_min
            self._rebuild_client()

        try:
            client.loop_stop()
        except Exception:  # noqa: BLE001
            pass

    def _rebuild_client(self) -> None:
        """Dựng lại client sau lỗi nặng (socket treo, DNS fail kéo dài)."""
        old, self._client = self._client, None
        if old is not None:
            try:
                old.loop_stop()
                old.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if self._stop.is_set():
            return
        self._client = self._build_client()
        self.stats.state = MQTT_RECONNECTING

    def _emit(self, event: str, message: str) -> None:
        handler = self.on_event
        if handler is None:
            return
        try:
            handler(event, message)
        except Exception:  # noqa: BLE001 - callback UI không được làm chết network
            log.debug("on_event lỗi", exc_info=True)


def _is_success(reason_code: Any) -> bool:
    """Chuẩn hoá kết quả on_connect giữa paho v1 (int) và v2 (ReasonCode)."""
    if reason_code is None:
        return False
    if isinstance(reason_code, bool):
        return reason_code
    if isinstance(reason_code, int):
        return reason_code == 0
    value = getattr(reason_code, "value", None)
    if isinstance(value, int):
        return value == 0
    text = str(reason_code).lower()
    return "success" in text or text == "0"
