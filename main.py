"""Điểm khởi động tool Test Gateway IoT.

Chạy GUI (mặc định):

    python main.py

Chạy KHÔNG GUI (khuyến nghị cho test 3-4 ngày trên máy chỉ dùng để đo):

    python main.py --headless --hours 72

Chế độ headless vẫn ghi CSV + báo cáo đầy đủ, chỉ bỏ phần vẽ màn hình. Ít rủi
ro hơn (không phụ thuộc Qt) nếu PC chỉ để chạy test.

Ghi chú vận hành:
  - Ctrl+C trong chế độ headless sẽ DỪNG SẠCH (drain queue + ghi báo cáo) nhờ
    signal handler trong `MonitorRunner`.
  - Mọi dữ liệu nằm trong `data/<run_id>/`; thông tin phiên ở `meta.json` của
    từng run (cập nhật định kỳ 60 s để không mất dấu vết nếu PC tắt đột ngột).
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from core import __version__ as TOOL_VERSION  # noqa: E402
from core import paths as tool_paths  # noqa: E402
from core.config import (  # noqa: E402
    CONFIG_DIR,
    TOOL_ROOT as CORE_TOOL_ROOT,
    ConfigStore,
    ensure_user_config,
)

#: Trong bản `.exe`, PyInstaller giải nén code vào `sys._MEIPASS`; thêm vào
#: `sys.path` để chắc chắn import được các package đi kèm (core/ui/analytics/...).
_MEIPASS = getattr(sys, "_MEIPASS", None)
if _MEIPASS and _MEIPASS not in sys.path:
    sys.path.insert(0, _MEIPASS)

LOG_DIR = CORE_TOOL_ROOT / "logs"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def force_utf8_console() -> None:
    """Ép console ra UTF-8 để in được tiếng Việt.

    Vì sao cần: console Windows mặc định dùng cp1252 → mọi dòng log có dấu
    tiếng Việt làm `logging` ném `UnicodeEncodeError` (thấy rõ nhất ở bản
    `.exe`, khi người dùng không chạy qua `run.ps1` để đặt `PYTHONUTF8=1`).

    Phải gọi **trước** khi cài handler log, vì `logging.StreamHandler` giữ
    tham chiếu tới `sys.stdout` lúc tạo.
    """
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - phụ thuộc loại stream
            pass


def setup_logging(level: str = "INFO", to_file: bool = True, console: bool = True) -> None:
    """Log ra console + file xoay vòng (`logs/test_gateway_iot.log`)."""
    force_utf8_console()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setLevel(getattr(logging, str(level).upper(), logging.INFO))
        stream.setFormatter(fmt)
        root.addHandler(stream)

    if to_file:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            rotating = logging.handlers.RotatingFileHandler(
                LOG_DIR / "test_gateway_iot.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            rotating.setLevel(logging.DEBUG)
            rotating.setFormatter(fmt)
            root.addHandler(rotating)
        except OSError as exc:  # pragma: no cover - phụ thuộc quyền ghi đĩa
            print("Cảnh báo: không mở được file log — %s" % exc, file=sys.stderr)

    logging.getLogger("paho").setLevel(logging.WARNING)
    logging.getLogger("paho.mqtt").setLevel(logging.WARNING)


def check_dependencies(need_gui: bool) -> list:
    """Kiểm tra module cần thiết; trả về danh sách tên còn thiếu."""
    missing = []
    for module, package in (("paho.mqtt.client", "paho-mqtt"), ("requests", "requests")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if need_gui:
        try:
            __import__("PyQt5.QtWidgets")
        except ImportError:
            missing.append("PyQt5")
    return missing


# ---------------------------------------------------------------------------
# Chế độ headless
# ---------------------------------------------------------------------------
def run_headless(config, devices, hours: float) -> int:
    """Chạy không GUI tới khi hết `hours` (0 = chạy mãi) hoặc Ctrl+C."""
    from runtime.runner import MonitorRunner

    log = logging.getLogger("main")

    store = ConfigStore()
    runner = MonitorRunner(
        store=store,
        config=config,
        devices=[d for d in devices if d.enabled],
        tool_version=TOOL_VERSION,
    )

    session = runner.start()
    log.info("=" * 78)
    log.info("TEST GATEWAY IOT — chế độ KHÔNG GUI")
    log.info("Phiên      : %s", session.run_id)
    log.info("Thư mục    : %s", session.run_dir)
    log.info("Broker     : %s", session.broker)
    log.info("Topic      : %s", ", ".join(session.subscribed_topics))
    log.info("Gateway    : %d (%s)", session.gateways_monitored or 0,
             ", ".join(session.gateway_uids) or "chưa cấu hình")
    if getattr(config, "focused_node_enabled", False) and config.focused_node_uid:
        log.info(
            "Node tâm   : %s (ngưỡng bắn hụt %.1f s)",
            config.focused_node_uid,
            config.focused_node_late_seconds,
        )
    log.info("Thời lượng : %s", ("%.1f giờ" % hours) if hours else "không giới hạn")
    log.info("Dừng bằng  : Ctrl+C (dữ liệu sẽ được ghi nốt và chốt báo cáo)")
    log.info("=" * 78)

    started = time.time()
    next_report = started + 600.0   # mỗi 10 phút in tiến độ 1 lần
    try:
        while runner.is_running():
            time.sleep(1.0)
            if hours and (time.time() - started) >= hours * 3600.0:
                log.info("Đã đủ %.1f giờ → dừng theo kế hoạch", hours)
                break
            if time.time() >= next_report:
                next_report = time.time() + 600.0
                _log_progress(runner)
    except KeyboardInterrupt:
        log.warning("Ctrl+C — đang dừng sạch…")
    finally:
        reports = runner.stop("Hết thời lượng / người dùng dừng (headless)")

    _log_progress(runner, final=True)
    if reports:
        log.info("Báo cáo đã ghi:")
        for name, path in sorted(reports.items()):
            log.info("  %-24s %s", name, path)
    else:
        log.warning("Không có báo cáo nào được ghi.")
    log.info("Kết thúc. Dữ liệu đầy đủ trong: %s", session.run_dir)
    return 0


def _log_progress(runner, final: bool = False) -> None:
    """In vài chỉ số quan trọng — đủ để biết phiên còn khỏe hay không."""
    log = logging.getLogger("main")
    stats = runner.stats()
    counters = (stats.get("pipeline") or {}).get("counters") or {}
    session = stats.get("session") or {}
    ping = (stats.get("pipeline") or {}).get("ping") or {}
    mqtt = (stats.get("pipeline") or {}).get("mqtt") or {}

    log.info(
        "%s | %s | bản tin %s | thiếu %s | trùng %s | sai thứ tự %s | "
        "ping ok %s/%s | reconnect %s | queue %s",
        "TỔNG KẾT" if final else "TIẾN ĐỘ",
        _human_duration(session.get("elapsed_seconds")),
        counters.get("total_messages", 0),
        counters.get("missing_packets", 0),
        counters.get("duplicate_packets", 0),
        counters.get("out_of_order_packets", 0),
        (ping.get("total_pings", 0) or 0) - (ping.get("failed_pings", 0) or 0),
        ping.get("total_pings", 0),
        mqtt.get("reconnects", 0),
        (stats.get("pipeline") or {}).get("queue_size", 0),
    )

    # --- node trọng tâm: bắt hụt / bắn hụt per gateway ---
    focused = (stats.get("pipeline") or {}).get("focused_node") or {}
    if focused.get("active"):
        if not final:
            log.info(
                "  🎯 node tâm %s | node đã phát %s | %s/%s gateway nghe được | "
                "tổng hụt %s | bắn trễ %s",
                focused.get("node_uid", ""),
                focused.get("reference_packets", 0),
                focused.get("gateways_heard", 0),
                focused.get("gateways_known", 0),
                focused.get("total_missing_catch", 0),
                focused.get("late_packets", 0),
            )
            # Chỉ 5 gateway tệ nhất để log tiến độ không quá dài.
            rows = sorted(
                focused.get("gateways") or [],
                key=lambda r: (-(r.get("missed_catch") or 0), -(r.get("late_packets") or 0)),
            )
            for row in rows[:5]:
                if not (row.get("missed_catch") or row.get("late_packets")):
                    continue
                log.info(
                    "     %-12s %-20s bắt hụt %5d | bắn trễ %5d | trễ max %s s",
                    row.get("gateway_uid", ""),
                    (row.get("gateway_name") or "")[:20],
                    row.get("missed_catch", 0),
                    row.get("late_packets", 0),
                    row.get("delay_max_seconds", "-"),
                )
        else:
            lines = _focused_summary_lines(stats)
            for line in lines:
                log.info(line)


def _focused_summary_lines(stats: dict) -> list[str]:
    """Tóm tắt node trọng tâm cho lúc kết thúc phiên (headless)."""
    focused = (stats.get("pipeline") or {}).get("focused_node") or {}
    if not focused.get("active"):
        return []
    rows = sorted(
        focused.get("gateways") or [],
        key=lambda r: (-(r.get("missed_catch") or 0), -(r.get("late_packets") or 0)),
    )
    lines = [
        "=" * 78,
        "NODE TRỌNG TÂM %s — BẮT HỤT / BẮN HỤT THEO GATEWAY"
        % focused.get("node_uid", ""),
        "Node đã phát (theo mọi gateway): %s bản tin | %s/%s gateway nghe được"
        % (
            focused.get("reference_packets", 0),
            focused.get("gateways_heard", 0),
            focused.get("gateways_known", 0),
        ),
    ]
    silent = focused.get("gateways_silent") or 0
    if silent:
        lines.append(
            "  ⚠ %d gateway KHÔNG publish bản tin nào của node "
            "(nghi mất tầm sóng / gateway chết) — xem cột notes" % silent
        )
    lines.append(
        "  %-14s %-22s %8s %10s %8s %8s  %s" % (
            "Gateway", "Tên", "Bắt hụt",
            "Bắt hụt %", "Bắn hụt", "Bắn hụt %", "Kiểu ngắt quãng",
        )
    )
    for row in rows:
        lines.append(
            "  %-14s %-22s %8d %10s %8d %8s  %s" % (
                row.get("gateway_uid", ""),
                (row.get("gateway_name") or "")[:22],
                row.get("missed_catch", 0),
                _fmt_pct(row.get("missed_catch_percent")),
                row.get("late_packets", 0),
                _fmt_pct(row.get("late_percent")),
                row.get("gap_pattern") or "-",
            )
        )
    lines.append(
        "  → BẮT HỤT = node đã phát (gateway khác nghe) mà gateway này không publish; "
        "BẮN HỤT = có publish nhưng trễ quá ngưỡng (buffer SD / backfill)."
    )
    lines.append(
        "Chi tiết: focused_node_summary.csv / focused_node_missing.csv / "
        "focused_node_late.csv trong reports/"
    )
    lines.append("=" * 78)
    return lines


def _fmt_pct(value) -> str:
    try:
        return "%.2f%%" % float(value)
    except (TypeError, ValueError):
        return "-"


def _human_duration(seconds) -> str:
    try:
        seconds = int(max(0, float(seconds or 0)))
    except (TypeError, ValueError):
        return "-"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return "%dd %02d:%02d:%02d" % (days, hours, minutes, secs)
    return "%02d:%02d:%02d" % (hours, minutes, secs)


# ---------------------------------------------------------------------------
# Chế độ GUI
# ---------------------------------------------------------------------------
def run_gui(config, devices) -> int:
    from PyQt5.QtWidgets import QApplication

    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Test Gateway IoT")
    app.setApplicationVersion(TOOL_VERSION)

    window = MainWindow(
        store=ConfigStore(),
        config=config,
        devices=devices,
        tool_version=TOOL_VERSION,
    )
    window.show()
    return app.exec_()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="test_gateway_iot",
        description=(
            "Theo dõi MQTT gateway IoT Type0 trong nhiều ngày: ghi CSV đầy đủ, "
            "phân tích SEQ (uint16), ping song song và chốt báo cáo tổng hợp."
        ),
    )
    parser.add_argument("--headless", action="store_true",
                        help="Chạy không GUI (khuyến nghị cho test dài ngày).")
    parser.add_argument("--hours", type=float, default=0.0,
                        help="Tự dừng sau N giờ (chỉ dùng với --headless; 0 = không giới hạn).")
    parser.add_argument("--config-dir", type=Path, default=None,
                        help="Thư mục chứa config.json/devices.json (mặc định: tool_root/config).")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Mức log ra console (mặc định INFO).")
    parser.add_argument("--version", action="version", version="%(prog)s " + TOOL_VERSION)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level, to_file=True, console=True)
    log = logging.getLogger("main")

    need_gui = not args.headless
    missing = check_dependencies(need_gui)
    if missing:
        print("Thiếu thư viện: %s" % ", ".join(missing), file=sys.stderr)
        print("Cài đặt: pip install -r requirements.txt", file=sys.stderr)
        return 2

    # Bản .exe: đưa config.json/devices.json mẫu ra cạnh file exe (lần chạy đầu).
    # Chạy từ mã nguồn thì hàm này không làm gì.
    ensure_user_config()

    store = ConfigStore(args.config_dir) if args.config_dir else ConfigStore()
    config = store.load_config()
    devices = store.load_devices()

    log.info("Test Gateway IoT v%s — cấu hình: %s", TOOL_VERSION, store.config_path)
    log.info("Đường dẫn: %s", tool_paths.describe())
    log.info("Broker %s:%d | topic %s",
             config.mqtt_host, config.mqtt_port,
             ", ".join(config.effective_subscribe_topics()))
    log.info("Thiết bị: %d (bật %d)", len(devices), sum(1 for d in devices if d.enabled))
    if not devices:
        log.warning(
            "Chưa cấu hình gateway nào — tool vẫn CHẠY: mọi bản tin nhận được đều "
            "được ghi CSV, nhưng sẽ bị gắn cờ UNKNOWN_GATEWAY và không ping được. "
            "Thêm gateway trong tab “Thiết bị” (GUI) hoặc trong %s.",
            store.devices_path,
        )

    if args.headless:
        return run_headless(config, devices, args.hours)

    if args.hours:
        log.warning("--hours chỉ có tác dụng ở chế độ --headless; đang bỏ qua.")

    try:
        return run_gui(config, devices)
    except Exception:  # noqa: BLE001 - hiện lỗi khởi động cho người dùng
        log.exception("Lỗi khởi động GUI")
        print("\nKhông khởi động được GUI. Xem log tại: %s"
              % (LOG_DIR / "test_gateway_iot.log"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
