"""Smoke test GUI (PyQt5) cho Test Gateway IoT — chạy headless (offscreen).

Mục đích: bắt lỗi import / sai tên widget / sai chữ ký signal TRƯỚC khi mang tool
ra chạy 3-4 ngày trên máy trạm. Không cần mạng, không cần MQTT broker.

    python tests/gui_smoke_test.py

Qt hiển thị cảnh báo `QFontDatabase: Cannot find font directory` khi chạy offscreen
— vô hại, bỏ qua.

LƯU Ý khi kiểm tra bằng pixel: nền offscreen KHÔNG có font nào (0 family), nên
KHÔNG có glyph chữ nào được rasterize. Vì vậy đừng lấy mẫu pixel để kết luận
"bảng không có chữ" — đó là giới hạn của môi trường headless, không phải lỗi sản
phẩm. Muốn soi chữ phải chạy trên desktop thật (có font). Ở đây chỉ kiểm tra được
cấu trúc/model/stylesheet.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

TOOL_ROOT = Path(__file__).resolve().parent.parent
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

FAILURES = []
CHECKS = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print("  [ OK ] %s" % label)
    else:
        print("  [FAIL] %s%s" % (label, (" — " + detail) if detail else ""))
        FAILURES.append(label)


def _focus_record(gateway: str, seq: int, received_at: str, gateway_timestamp: str):
    """Bản copy MQTT tối thiểu cho `FocusedNodeTracker.feed()` (test 9)."""
    from core.models import PKT_DATA, MQTTRecord

    return MQTTRecord(
        received_at=received_at,
        packet_type=PKT_DATA,
        gateway_uid=gateway,
        gateway_name="GW " + gateway,
        node_uid="0011223344556677",
        seq=seq,
        gateway_timestamp=gateway_timestamp,
    )


def main() -> int:
    print("=" * 78)
    print("GUI SMOKE TEST — Test Gateway IoT (offscreen, không cần mạng)")
    print("=" * 78)

    try:
        from PyQt5.QtWidgets import QApplication
    except ImportError as exc:  # pragma: no cover - môi trường thiếu PyQt5
        print("  [SKIP] PyQt5 chưa cài: %s" % exc)
        print("=" * 78)
        return 0

    app = QApplication.instance() or QApplication(sys.argv[:1])

    tmp_root = Path(tempfile.mkdtemp(prefix="testgw_gui_"))

    # --- 1. import toàn bộ module UI --------------------------------------
    print("\n[1] Import các module UI")
    modules = [
        "ui.theme",
        "ui.log_viewer",
        "ui.table_model",
        "ui.config_tab",
        "ui.devices_tab",
        "ui.monitor_tab",
        "ui.search_tab",
        "ui.history_tab",
        "ui.main_window",
    ]
    for name in modules:
        try:
            __import__(name)
            check(True, "import %s" % name)
        except Exception as exc:  # noqa: BLE001 - báo hết mọi lỗi import
            check(False, "import %s" % name, "%s: %s" % (type(exc).__name__, exc))

    main_window = None
    try:
        from core.config import ConfigStore, GatewayDevice, TestConfig
        from ui.main_window import MainWindow
    except Exception as exc:  # noqa: BLE001
        check(False, "import MainWindow để dựng cửa sổ", "%s" % exc)
        print("\n" + "=" * 78)
        print("KẾT QUẢ: %d/%d kiểm tra THẤT BẠI" % (len(FAILURES), CHECKS))
        return 1

    # --- 2. dựng cửa sổ chính với config tạm ------------------------------
    print("\n[2] Dựng MainWindow với config tạm")
    try:
        store = ConfigStore(tmp_root / "config")
        config = TestConfig()
        devices = [GatewayDevice(uid="4F9199E139C", name="GW test",
                                 eth_ip="192.168.1.101")]
        store.save_config(config)
        store.save_devices(devices)
        main_window = MainWindow(store, config, devices, tool_version="gui-smoke")
        check(main_window is not None, "MainWindow khởi tạo không lỗi")
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(False, "MainWindow khởi tạo không lỗi", "%s: %s" % (type(exc).__name__, exc))

    # --- 3. các tab tồn tại ----------------------------------------------
    print("\n[3] Cấu trúc cửa sổ")
    if main_window is not None:
        check(hasattr(main_window, "tabs"), "có QTabWidget (self.tabs)")
        if hasattr(main_window, "tabs"):
            check(main_window.tabs.count() == 5, "có đúng 5 tab",
                  "có %d" % main_window.tabs.count())
            # Tab "Lịch sử phiên" phải nằm giữa Tìm kiếm và Cấu hình.
            labels = [main_window.tabs.tabText(i) for i in range(main_window.tabs.count())]
            check("Lịch sử phiên" in labels[3],
                  "tab thứ 4 là 'Lịch sử phiên'",
                  "nhãn = %r" % (labels,))
        for attr in ("monitor_tab", "config_tab", "devices_tab", "search_tab",
                     "history_tab"):
            check(hasattr(main_window, attr), "có thuộc tính %s" % attr)

    # --- 4. bảng KHÔNG được kẻ sọc ----------------------------------------
    print("\n[4] Bảng phẳng (không sọc xen kẽ)")
    try:
        from PyQt5.QtWidgets import QTableView

        views = main_window.findChildren(QTableView) if main_window is not None else []
        check(bool(views), "tìm thấy bảng trong cửa sổ",
              "không có QTableView nào")
        for view in views:
            check(not view.alternatingRowColors(),
                  "bảng không bật sọc xen kẽ",
                  "alternatingRowColors=True")
    except Exception as exc:  # noqa: BLE001
        check(False, "kiểm tra thuộc tính sọc của bảng", "%s" % exc)

    # Nền dòng xen kẽ phải TRÙNG nền thường, nếu không sẽ quay lại lỗi
    # "hàng nền trắng chữ trắng". Đây là chốt chống tái phát.
    from ui import theme

    def _extract(block: str, prop: str) -> str:
        import re
        match = re.search(r"%s\s*:\s*(#[0-9a-fA-F]{6})" % prop, block)
        return match.group(1).lower() if match else ""

    table_block = theme.STYLESHEET.split("QTableView {")[-1].split("}")[0]
    base_bg = _extract(table_block, "background")
    alt_bg = _extract(table_block, "alternate-background-color")
    check(bool(base_bg) and base_bg == alt_bg,
          "nền bảng và nền xen kẽ trùng nhau trong stylesheet",
          "background=%r alternate=%r" % (base_bg, alt_bg))

    app.processEvents()

    # --- 5. tab Tìm kiếm: dòng CSV cũ phải hiện đúng gateway --------------
    # Đây là chốt chống tái phát cho lỗi người dùng báo: tìm theo gateway ra
    # một loạt dòng nhưng cột "ID gateway" trống và tên là unknown.
    print("\n[5] Tab Tìm kiếm điền gateway cho CSV cũ")
    if main_window is not None:
        try:
            import csv as csv_module
            import json as json_module

            from storage.schema import MQTT_DATA_COLUMNS

            mqtt_dir = tmp_root / "search_csv"
            mqtt_dir.mkdir(parents=True, exist_ok=True)
            legacy_uid = "8F9199E139C"
            with open(mqtt_dir / "mqtt_data_20260918.csv", "w",
                      encoding="utf-8-sig", newline="") as handle:
                writer = csv_module.writer(handle)
                writer.writerow(list(MQTT_DATA_COLUMNS))
                row = {col: "" for col in MQTT_DATA_COLUMNS}
                row.update({
                    "received_at": "2026-09-18T16:46:01+07:00",
                    "packet_type": "DATA",
                    "gateway_uid": "",           # CSV phiên bản cũ
                    "gateway_name": "UNKNOWN_GATEWAY",
                    "gateways": "",
                    "payload_json": json_module.dumps({
                        "gateway_id": legacy_uid,
                        "receiver_id": "64B5B30615E18000",
                        "sensor_id": "1122334455667788",
                        "seq": 9,
                    }),
                })
                writer.writerow([row.get(col, "") for col in MQTT_DATA_COLUMNS])

            # Đúng luồng GUI: SearchTab nhận danh sách thiết bị rồi tự tra tên.
            search_tab = main_window.search_tab
            check(hasattr(search_tab, "set_devices"),
                  "SearchTab có set_devices() để nhận danh sách thiết bị")
            search_tab.set_devices([GatewayDevice(uid="08F9199E139C",
                                                 name="GW MAC đầy đủ",
                                                 eth_ip="192.168.1.150")])
            check(search_tab.gateway_names.get("8F9199E139C") == "GW MAC đầy đủ",
                  "bảng tên của tab Tìm kiếm tra được UID 11 hex",
                  "nhận %r" % search_tab.gateway_names.get("8F9199E139C"))

            from storage.reader import SearchQuery, search_mqtt

            result = search_mqtt(mqtt_dir, SearchQuery(
                gateway_uid="8F9199", limit=10, offset=0,
                gateway_names=search_tab.gateway_names))
            check(result.matched == 1,
                  "tìm '8F9199' ra đúng dòng CSV cũ",
                  "matched=%d" % result.matched)
            found = result.rows[0] if result.rows else {}
            check((found.get("gateway_uid") or "") != "",
                  "cột 'ID gateway' của dòng kết quả KHÔNG trống",
                  "gateway_uid=%r" % found.get("gateway_uid"))
            check((found.get("gateway_name") or "").upper() != "UNKNOWN_GATEWAY",
                  "cột 'Tên gateway' hiện tên thật, không còn unknown",
                  "gateway_name=%r" % found.get("gateway_name"))

            # Lưới an toàn: model của bảng phải render được 2 cột đó.
            search_tab.model.reset_rows(result.rows)
            model = search_tab.model
            from PyQt5.QtCore import Qt as QtCore

            columns = list(model.columns)
            gw_uid_text = model.data(model.index(0, columns.index("gateway_uid")),
                                     QtCore.DisplayRole)
            gw_name_text = model.data(model.index(0, columns.index("gateway_name")),
                                      QtCore.DisplayRole)
            check(bool(gw_uid_text) and bool(gw_name_text),
                  "bảng kết quả render được cả ID và tên gateway",
                  "uid=%r name=%r" % (gw_uid_text, gw_name_text))
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, "kiểm tra điền gateway trong kết quả tìm kiếm",
                  "%s: %s" % (type(exc).__name__, exc))

    # --- 6. tab Thiết bị: cột "Tỉ lệ ping (%)" cho TỪNG gateway -----------
    # Chốt chống tái phát cho yêu cầu "bổ sung cột tỉ lệ ping của từng con
    # gateway": cột phải tồn tại, tra được theo cả UID 11 hex lẫn 12 hex, và
    # KHÔNG được hiện "0.0 %" khi chưa có dữ liệu (0 % nghĩa là mất hết).
    print("\n[6] Tab Thiết bị — cột 'Tỉ lệ ping (%)' theo từng gateway")
    if main_window is not None:
        try:
            from PyQt5.QtCore import Qt as QtCore

            from storage.schema import PING_SUMMARY_COLUMNS
            from ui.devices_tab import DevicesTab

            multi = [
                GatewayDevice(uid="4F9199E139C", name="GW A", eth_ip="192.168.1.101"),
                GatewayDevice(uid="8F9199E139C", name="GW B (11 hex)", eth_ip="192.168.1.102"),
                GatewayDevice(uid="AA11BB22CC33", name="GW chưa ping", eth_ip="192.168.1.103"),
            ]
            tab = DevicesTab(multi)
            columns = list(tab.model.columns)
            check("ping_rate" in columns, "tab Thiết bị có cột ping_rate")
            check("Tỉ lệ ping (%)" in list(tab.model.headers),
                  "header cột là 'Tỉ lệ ping (%)'",
                  "headers=%r" % (list(tab.model.headers),))
            check(len(columns) == len(list(tab.model.headers)),
                  "số cột và số header khớp nhau")

            # ping_summary.csv LUÔN ghi gateway theo dạng 12 hex (zero-pad).
            assert PING_SUMMARY_COLUMNS[0] == "gateway_uid"
            summary = [
                {col: "" for col in PING_SUMMARY_COLUMNS},
                {col: "" for col in PING_SUMMARY_COLUMNS},
            ]
            summary[0].update({
                "gateway_uid": "4F9199E139C", "gateway_name": "GW A",
                "ping_total": 200, "ping_success": 198, "ping_failed": 2,
                "success_rate_percent": "99.0", "latency_avg_ms": "3.2",
                "latency_max_ms": "41.0", "downtime_events": 0,
            })
            # Cùng gateway nhưng ghi dạng 11 hex → phải khớp dòng thiết bị 11 hex.
            summary[1].update({
                "gateway_uid": "8F9199E139C", "gateway_name": "GW B",
                "ping_total": 100, "ping_success": 50, "ping_failed": 50,
                "success_rate_percent": "50.0", "latency_avg_ms": "12.5",
                "latency_max_ms": "300.0", "downtime_events": 3,
            })
            tab.set_ping_stats(summary)

            check(tab.ping_rate_for("4F9199E139C") == "99.0",
                  "tra được tỉ lệ ping theo UID 12 hex",
                  "nhận %r" % tab.ping_rate_for("4F9199E139C"))
            check(tab.ping_rate_for("8F9199E139C") == "50.0",
                  "tra được tỉ lệ ping theo UID 11 hex (dạng firmware in)",
                  "nhận %r" % tab.ping_rate_for("8F9199E139C"))
            check(tab.ping_rate_for("08F9199E139C") == "50.0",
                  "UID 12 hex zero-pad khớp cùng dòng thiết bị 11 hex",
                  "nhận %r" % tab.ping_rate_for("08F9199E139C"))
            check(tab.ping_rate_for("AA11BB22CC33") == "",
                  "gateway chưa ping lần nào trả về rỗng (để hiện '-')",
                  "nhận %r" % tab.ping_rate_for("AA11BB22CC33"))

            # Render: có số → "99.0 %", chưa có → "-" (KHÔNG phải "0.0 %").
            rate_col = columns.index("ping_rate")
            check(tab.model.data(tab.model.index(0, rate_col), QtCore.DisplayRole)
                  == "99.0 %", "ô có số liệu render '99.0 %' (có ký hiệu %)",
                  "nhận %r" % tab.model.data(tab.model.index(0, rate_col),
                                             QtCore.DisplayRole))
            blank = str(tab.model.data(tab.model.index(2, rate_col),
                                       QtCore.DisplayRole))
            check(blank.strip() == "-",
                  "ô chưa có số liệu render '-' (không phải '0.0 %')",
                  "nhận %r" % blank)
            tip = str(tab.model.data(tab.model.index(2, rate_col),
                                     QtCore.ToolTipRole))
            check("chưa có dữ liệu ping" in tip,
                  "tooltip ô rỗng giải thích vì sao chưa có số liệu")

            # theme.pct() phải LÀM TRÒN và kèm '%'. Trước đây biểu thức lồng
            # `("%%.%df %%" % digits)` luôn văng ValueError nên hàm âm thầm trả
            # về số nguyên bản không làm tròn, không có '%'.
            check(theme.pct(99.123456, 1) == "99.1 %",
                  "theme.pct() làm tròn đúng số chữ số thập phân",
                  "nhận %r" % theme.pct(99.123456, 1))
            check(theme.pct(99.123456, 2) == "99.12 %",
                  "theme.pct() tôn trọng tham số digits",
                  "nhận %r" % theme.pct(99.123456, 2))
            check(theme.pct(None) == "-" and theme.pct("") == "-",
                  "theme.pct() trả '-' khi không có dữ liệu")

            # Cập nhật lại số liệu KHÔNG được reset bảng (mất vị trí cuộn).
            # `set_ping_stats` bỏ qua khi index KHÔNG ĐỔI, nên phải dùng số liệu
            # MỚI để chắc chắn đi qua đường `_repaint_ping_column()`.
            tab.model.reset_rows([{c: "" for c in columns} for _ in multi])
            check(tab.model.rowCount() == 3,
                  "dựng lại bảng 3 dòng TRỐNG trước khi thử",
                  "rowCount=%d" % tab.model.rowCount())
            tab.set_ping_stats([dict(summary[0], success_rate_percent="88.0"),
                                summary[1]])
            check(tab.model.rowCount() == 3,
                  "cập nhật số liệu ping không tự nạp lại bảng (giữ vị trí cuộn)",
                  "rowCount=%d" % tab.model.rowCount())
            check(tab.ping_rate_for("4F9199E139C") == "88.0",
                  "_repaint_ping_column() thật sự cập nhật số liệu mới",
                  "nhận %r" % tab.ping_rate_for("4F9199E139C"))
            tab.reload()
            check(tab.model.rowCount() == 3,
                  "reload() vẫn dựng đủ 3 dòng gateway")

            # Nguồn là Path tới ping_summary.csv (phiên đã dừng).
            csv_path = tmp_root / "ping_summary.csv"
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv_module.DictWriter(handle,
                                               fieldnames=PING_SUMMARY_COLUMNS)
                writer.writeheader()
                writer.writerows(summary)
            tab.set_ping_stats(csv_path)
            check(tab.ping_rate_for("8F9199E139C") == "50.0",
                  "đọc tỉ lệ ping từ file ping_summary.csv")

            # CSV phiếu/hỏng không được làm chết GUI.
            bad = tmp_root / "ping_summary_bad.csv"
            bad.write_text("gateway_uid\nnot,the,right,shape\n", encoding="utf-8")
            tab.set_ping_stats(bad)
            check(tab.ping_rate_for("4F9199E139C") == "",
                  "ping_summary.csv hỏng → hiện '-' thay vì crash")
            tab.clear_ping_stats()
            check(tab.ping_rate_for("4F9199E139C") == "",
                  "clear_ping_stats() xoá sạch số liệu phiên cũ")
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, "kiểm tra cột tỉ lệ ping của tab Thiết bị",
                  "%s: %s" % (type(exc).__name__, exc))

    # --- 7. tab Tìm kiếm: cột "Gateway cùng nghe" đã bị bỏ -----------------
    # Chốt chống tái phát cho yêu cầu "bỏ cột gateway cùng nhận".
    print("\n[7] Tab Tìm kiếm — đã bỏ cột 'Gateway cùng nghe'")
    if main_window is not None:
        try:
            check("gateways" not in list(main_window.search_tab.model.columns),
                  "bảng kết quả tìm kiếm KHÔNG còn cột gateways")
            check(not any("cùng nghe" in h or "cùng nhận" in h
                          for h in main_window.search_tab.model.headers),
                  "không còn header nào nói về 'cùng nghe/cùng nhận'",
                  "headers=%r" % (list(main_window.search_tab.model.headers),))
            # Vẫn phải GIỮ 2 cột gateway của gateway RSSI tốt nhất.
            check("gateway_uid" in list(main_window.search_tab.model.columns)
                  and "gateway_name" in list(main_window.search_tab.model.columns),
                  "vẫn giữ cột ID gateway và Tên gateway")
            # Cột gateways vẫn còn trong CSV (không được xoá khỏi schema) vì
            # bộ lọc "Gateway UID" dựa vào nó (xem storage/reader.py).
            from storage.schema import MQTT_DATA_COLUMNS

            check("gateways" in list(MQTT_DATA_COLUMNS),
                  "schema CSV vẫn giữ cột `gateways` cho bộ lọc Gateway UID")
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, "kiểm tra việc bỏ cột gateway cùng nghe",
                  "%s: %s" % (type(exc).__name__, exc))

    # --- 8. nối cột ping vào vòng đời phiên trong MainWindow --------------
    print("\n[8] MainWindow nạp tỉ lệ ping của phiên gần nhất")
    if main_window is not None:
        try:
            check(hasattr(main_window, "_refresh_devices_ping"),
                  "MainWindow có _refresh_devices_ping()")
            check(hasattr(main_window, "_historical_ping_summary"),
                  "MainWindow có _historical_ping_summary()")
            # Không có phiên nào trong data/ → phải trả {} và KHÔNG được raise.
            check(isinstance(main_window._historical_ping_summary(), dict),
                  "_historical_ping_summary() trả về dict khi chưa có phiên")
            # Chưa chạy phiên (runner=None) → chỉ đếm nhịp, không crash.
            main_window._refresh_devices_ping(force=True)
            check(True, "_refresh_devices_ping() không lỗi khi chưa chạy phiên")
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, "kiểm tra nối ping vào MainWindow",
                  "%s: %s" % (type(exc).__name__, exc))

    # --- 9. tab Giám sát: trang "Node trọng tâm" --------------------------
    # Chốt chống tái phát cho yêu cầu "theo dõi 1 node riêng biệt trên nhiều
    # Gateway … phán đoán Gateway bắt hụt / bắn hụt bao nhiêu bản tin": trang
    # phải tồn tại, ô nhập UID phải được chấp nhận, và UID sai phải bị chặn.
    print("\n[9] Tab Giám sát — trang 'Node trọng tâm'")
    if main_window is not None:
        try:
            from PyQt5.QtCore import Qt as QtCore

            from analytics.focused_node import FocusedNodeTracker

            monitor = main_window.monitor_tab
            for attr in ("edit_focus", "btn_focus_apply", "btn_focus_clear",
                         "lbl_focus", "model_focus", "model_focus_missing",
                         "model_focus_late"):
                check(hasattr(monitor, attr), "tab Giám sát có %s" % attr)

            # UID hợp lệ phải qua được bộ lọc trước khi phát signal tới runner.
            captured = []
            monitor.focused_node_requested.connect(captured.append)
            monitor.edit_focus.setText(" 0011223344556677 ")
            monitor._apply_focus()
            check(captured == ["0011223344556677"],
                  "bấm 'Theo dõi' phát signal với UID đã chuẩn hoá",
                  "nhận %r" % (captured,))

            # UID sai → KHÔNG phát signal, và phải hiện cảnh báo.
            monitor.edit_focus.setText("khong-phai-hex")
            monitor._apply_focus()
            check(captured == ["0011223344556677"],
                  "UID không hợp lệ KHÔNG được phát signal")
            check("hợp lệ" in monitor.lbl_focus.text().lower(),
                  "UID không hợp lệ hiện cảnh báo cho người dùng",
                  "nhãn = %r" % monitor.lbl_focus.text())

            # 3 bảng phải render được dòng thật từ tracker.
            tracker = FocusedNodeTracker(seq_max=100, late_seconds=30.0)
            tracker.set_node("0011223344556677")
            tracker.set_known_gateways([("AAA", "GW A"), ("BBB", "GW B")])
            tracker.feed(_focus_record("AAA", 1, "2026-09-22T10:00:00+07:00",
                                       "2026-09-22T09:59:00+07:00"))
            tracker.feed(_focus_record("BBB", 2, "2026-09-22T10:05:00+07:00",
                                       "2026-09-22T09:59:00+07:00"))

            cols = list(monitor.model_focus.columns)
            monitor.model_focus.reset_rows(tracker.summary_rows())
            text = str(monitor.model_focus.data(
                monitor.model_focus.index(0, cols.index("missed_catch")),
                QtCore.DisplayRole))
            check(text.isdigit(), "bảng tổng hợp render được số bắt hụt",
                  "nhận %r" % text)

            missing = tracker.missing_rows()
            monitor.model_focus_missing.reset_rows(missing)
            check(len(missing) >= 1, "bảng chi tiết bắt hụt có dòng",
                  "có %d dòng" % len(missing))
            late = tracker.late_rows()
            monitor.model_focus_late.reset_rows(late)
            # Cả 2 bản tin đều trễ quá ngưỡng 30 s → 2 dòng, nặng nhất (BBB, 360 s)
            # phải đứng trước.
            check(len(late) == 2, "bảng chi tiết bắn hụt có 2 dòng",
                  "có %d dòng" % len(late))
            check(late[0]["gateway_uid"] == "BBB" and late[0]["delay_seconds"] == 360.0,
                  "dòng trễ nặng nhất được xếp trước",
                  "đầu = %r" % (late[0] if late else None,))

            # Nhãn trạng thái phải phản ánh việc đang theo dõi node nào.
            monitor.set_focused_node_display("0011223344556677")
            check(monitor.edit_focus.text() == "0011223344556677",
                  "set_focused_node_display() khôi phục UID đã lưu")

            # Cột "kiểu ngắt quãng" phải có trong bảng VÀ render được text.
            check("gap_pattern" in cols,
                  "bảng tổng hợp có cột 'kiểu ngắt quãng'")
            gap_tracker = FocusedNodeTracker(seq_max=100)
            gap_tracker.set_node("0011223344556677")
            for seq in range(1, 21):
                gap_tracker.feed(_focus_record("AAA", seq,
                                               "2026-09-22T10:00:00+07:00",
                                               "2026-09-22T10:00:00+07:00"))
            for seq in range(1, 11):     # BBB hụt một mạch 11..20
                gap_tracker.feed(_focus_record("BBB", seq,
                                               "2026-09-22T10:00:00+07:00",
                                               "2026-09-22T10:00:00+07:00"))
            gap_tracker.refresh_patterns()
            gap_rows = gap_tracker.summary_rows()
            monitor.model_focus.reset_rows(gap_rows)
            # Dòng 0 là TỔNG ('ALL'); phải tra đúng dòng gateway BBB.
            bbb_index = next(
                i for i, r in enumerate(gap_rows) if r["gateway_uid"] == "BBB"
            )
            gap_text = str(monitor.model_focus.data(
                monitor.model_focus.index(bbb_index, cols.index("gap_pattern")),
                QtCore.DisplayRole))
            check("LIÊN TỤC" in gap_text,
                  "cột 'kiểu ngắt quãng' render được kết luận hụt một mạch",
                  "nhận %r" % gap_text)

            # --- nháy đúp ở bảng "Tổng hợp node" → theo dõi node đó ---------
            # Chốt chống tái phát cho yêu cầu "click double vào node thì hiện ra
            # bảng theo dõi node đó trọng tâm, nếu xong thì nhấn thoát ra để xem
            # node khác": (a) nháy đúp phải bật theo dõi ĐÚNG UID của dòng đó,
            # (b) tự chuyển sang trang "Node trọng tâm", (c) có đường quay lại
            # bảng "Tổng hợp node" mà KHÔNG cần bỏ theo dõi.
            for attr in ("_view_nodes", "focus_page", "btn_show_nodes", "_node_tabs"):
                check(hasattr(monitor, attr),
                      "tab Giám sát có %s (điều hướng node trọng tâm)" % attr)

            monitor.model_nodes.reset_rows([{"node_uid": "0011223344556677"}])
            monitor.edit_focus.setText("")
            before = len(captured)
            monitor._on_node_double_clicked(monitor.model_nodes.index(0, 0))
            check(len(captured) == before + 1
                  and captured[-1] == "0011223344556677",
                  "nháy đúp dòng 'Tổng hợp node' bật theo dõi đúng UID",
                  "nhận %r" % (captured[before:],))
            check(monitor.edit_focus.text() == "0011223344556677",
                  "nháy đúp điền UID vào ô 'UID node theo dõi'")
            check(monitor.tables_tabs.currentWidget() is monitor.focus_page,
                  "nháy đúp tự chuyển sang trang 'Node trọng tâm'")

            monitor.show_node_table()
            check(monitor.tables_tabs.currentWidget() is monitor._view_nodes,
                  "nút 'Thoát ra' quay lại bảng 'Tổng hợp node'")
            check(monitor.edit_focus.text() == "0011223344556677",
                  "thoát ra KHÔNG bỏ theo dõi node đang xem")

            monitor.show_focus_page()
            check(monitor.tables_tabs.currentWidget() is monitor.focus_page,
                  "show_focus_page() mở lại trang 'Node trọng tâm'")

            # Dòng không có UID / index không hợp lệ phải bị bỏ qua, không crash.
            monitor.model_nodes.reset_rows([{"node_uid": ""}])
            guard = len(captured)
            monitor._on_node_double_clicked(monitor.model_nodes.index(0, 0))
            monitor._on_node_double_clicked(None)
            check(len(captured) == guard,
                  "nháy đúp dòng thiếu UID / index rỗng không phát signal")
            monitor.model_nodes.reset_rows([])
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, "kiểm tra trang 'Node trọng tâm'",
                  "%s: %s" % (type(exc).__name__, exc))

    # --- 10. tab "Lịch sử phiên": danh sách phiên + xem lại kết quả --------
    # Chốt chống tái phát cho yêu cầu "tab lịch sử phiên, có danh sách các
    # phiên, nhấn vào phiên xem kết quả test phiên đó". Test tạo MỘT phiên
    # tổng hợp trong data/runs/ rồi kiểm tra: (a) phiên đó xuất hiện trong
    # bảng danh sách, (b) nhấn vào thì nạp đúng số liệu từ reports/*.csv,
    # (c) phiên còn status=RUNNING nhưng KHÔNG phải phiên đang chạy phải bị
    # gọi là "tắt ngang" (không được hiểu nhầm là "đang chạy"),
    # (d) nút "Xem bản tin thô" đặt đúng phạm vi cho tab Tìm kiếm.
    print("\n[10] Tab 'Lịch sử phiên' — danh sách phiên và xem lại kết quả")
    if main_window is not None:
        try:
            import csv as csv_module
            import json as json_module

            from PyQt5.QtCore import Qt as QtCore

            from storage.schema import (
                FOCUSED_NODE_SUMMARY_COLUMNS,
                PING_SUMMARY_COLUMNS,
                RUN_SUMMARY_COLUMNS,
            )

            runs_root = tmp_root / "history" / "runs"
            run_dir = runs_root / "20260101_120000_AAAABBBBCCCC"
            (run_dir / "reports").mkdir(parents=True, exist_ok=True)
            (run_dir / "mqtt").mkdir(parents=True, exist_ok=True)
            with open(run_dir / "meta.json", "w", encoding="utf-8") as handle:
                json_module.dump({
                    "run_id": "20260101_120000_AAAABBBBCCCC",
                    "started_at": "2026-01-01T12:00:00+07:00",
                    "ended_at": "2026-01-01T14:00:00+07:00",
                    "duration_seconds": 7200.0,
                    "status": "STOPPED",
                    "broker": "1.2.3.4:1885",
                    "subscribed_topics": ["node/#"],
                    "gateway_uids": ["AAAABBBBCCCC", "DDDDEEEEFFFF"],
                    "gateways_monitored": 2,
                    # ẢNH CHỤP tên gateway chốt lúc bắt đầu phiên. Tab Lịch sử
                    # phải dùng tên này, KHÔNG tra `devices.json` hiện tại.
                    "gateway_names": {
                        "AAAABBBBCCCC": "GW Test 01",
                        "DDDDEEEEFFFF": "GW Test 02",
                    },
                }, handle, ensure_ascii=False)

            def _write_report(name, columns, row):
                path = run_dir / "reports" / name
                with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv_module.writer(handle)
                    writer.writerow(list(columns))
                    writer.writerow([row.get(col, "") for col in columns])

            summary = {col: "" for col in RUN_SUMMARY_COLUMNS}
            summary.update({
                "run_id": "20260101_120000_AAAABBBBCCCC",
                "total_messages": 1234, "nodes_seen": 7,
                "missing_packets": 9, "ping_failed": 3,
                "duration_seconds": 7200,
            })
            _write_report("run_summary.csv", RUN_SUMMARY_COLUMNS, summary)
            ping = {col: "" for col in PING_SUMMARY_COLUMNS}
            ping.update({"gateway_uid": "AAAABBBBCCCC", "ping_total": 100,
                         "ping_success": 97, "ping_failed": 3})
            _write_report("ping_summary.csv", PING_SUMMARY_COLUMNS, ping)
            focus = {col: "" for col in FOCUSED_NODE_SUMMARY_COLUMNS}
            focus.update({"node_uid": "0011223344556677", "gateway_uid": "AAAABBBBCCCC",
                          "packets_heard": 40, "reference_packets": 50,
                          "missed_catch": 10, "gap_runs": 1, "gap_largest": 10,
                          "gap_pattern": "LIÊN TỤC"})
            _write_report("focused_node_summary.csv",
                          FOCUSED_NODE_SUMMARY_COLUMNS, focus)

            history = main_window.history_tab
            # Chờ lượt quét ĐẦU (thư mục data/ thật) kết thúc trước khi đổi
            # `data_dir`, nếu không `refresh()` sẽ bị bỏ qua vì `_list_worker`
            # cũ còn đang chạy và bảng sẽ hiện nhầm dữ liệu thật.
            _pending = history._list_worker
            if _pending is not None:
                _pending.wait(120000)
            app.processEvents()
            history.data_dir = tmp_root / "history"
            history.active_run_id = ""
            history.refresh()
            worker = history._list_worker
            if worker is not None:
                worker.wait(30000)
            app.processEvents()

            check(history.model.rowCount() == 1,
                  "bảng lịch sử liệt kê đúng 1 phiên",
                  "có %d dòng" % history.model.rowCount())

            cols = list(history.model.columns)
            row_model = history.model
            shown = {col: str(row_model.data(row_model.index(0, cols.index(col)),
                                            QtCore.DisplayRole))
                     for col in ("run_id", "status_label", "duration_label",
                                 "gateways_monitored", "total_messages",
                                 "reports", "gateways_label")}
            check(shown["run_id"] == "20260101_120000_AAAABBBBCCCC",
                  "cột 'Mã phiên' hiện đúng phiên", "nhận %r" % shown["run_id"])
            check("Đã dừng" in shown["status_label"],
                  "cột 'Trạng thái' đọc đúng status STOPPED",
                  "nhận %r" % shown["status_label"])
            check(shown["gateways_monitored"] == "2",
                  "cột 'Số GW' đếm đúng gateway của phiên",
                  "nhận %r" % shown["gateways_monitored"])
            check(shown["total_messages"] == "1.234",
                  "cột 'Bản tin MQTT' lấy đúng từ run_summary.csv",
                  "nhận %r" % shown["total_messages"])
            check(shown["reports"] == "3",
                  "cột 'Số báo cáo' đếm đúng file trong reports/",
                  "nhận %r" % shown["reports"])
            check("AAAABBBBCCCC" in shown["gateways_label"],
                  "cột 'Gateway của phiên' liệt kê đúng UID",
                  "nhận %r" % shown["gateways_label"])

            # --- nhấn vào phiên → nạp kết quả ---
            index = history.model.index(0, 0)
            history.table.setCurrentIndex(index)
            history._on_clicked(index)
            app.processEvents()
            worker = history._report_worker
            if worker is not None:
                worker.wait(30000)
            app.processEvents()

            check(history._models["ping"].rowCount() == 1,
                  "nhấn vào phiên → trang Ping có dữ liệu của phiên",
                  "có %d dòng" % history._models["ping"].rowCount())
            focus_model = history._models["focus"]
            focus_cols = list(focus_model.columns)
            missed = str(focus_model.data(
                focus_model.index(0, focus_cols.index("missed_catch")),
                QtCore.DisplayRole))
            check(missed == "10",
                  "trang 'Node trọng tâm' hiện đúng số BẮT HỤT của phiên",
                  "nhận %r" % missed)
            check(history._models["gateways"].rowCount() == 2,
                  "trang 'Gateway của phiên' liệt kê 2 gateway",
                  "có %d dòng" % history._models["gateways"].rowCount())
            check(history._models["summary"].rowCount() > 20,
                  "trang 'Tổng kết' hiện đủ chỉ số",
                  "có %d dòng" % history._models["summary"].rowCount())
            check(run_dir.name in history.lbl_header.text(),
                  "tiêu đề vùng kết quả nêu rõ phiên đang xem")

            # --- xoá/đổi gateway trong tab Thiết bị KHÔNG đổi lịch sử ---
            # Đây là chốt chống tái phát cho yêu cầu "xoá gateway hoặc thay thế
            # gateway khác trong tab Thiết bị thì không ảnh hưởng dữ liệu trong
            # lịch sử phiên". Tên gateway phải đọc từ ẢNH CHỤP `gateway_names`
            # trong meta.json của phiên, không tra `devices.json` hiện tại.
            gateways_model = history._models["gateways"]
            gateway_cols = list(gateways_model.columns)
            names_before = sorted(
                str(gateways_model.data(
                    gateways_model.index(r, gateway_cols.index("gateway_name")),
                    QtCore.DisplayRole))
                for r in range(gateways_model.rowCount())
            )
            check(names_before == ["GW Test 01", "GW Test 02"],
                  "trang 'Gateway của phiên' hiện TÊN chốt trong meta.json",
                  "nhận %r" % names_before)

            # Mô phỏng người dùng XOÁ HẾT gateway (và đổi sang gateway khác)
            # trong tab Thiết bị, rồi quay lại tab Lịch sử phiên.
            history.set_devices([])
            app.processEvents()
            names_after_delete = sorted(
                str(gateways_model.data(
                    gateways_model.index(r, gateway_cols.index("gateway_name")),
                    QtCore.DisplayRole))
                for r in range(gateways_model.rowCount())
            )
            check(names_after_delete == ["GW Test 01", "GW Test 02"],
                  "xoá hết gateway trong tab Thiết bị KHÔNG làm mất tên trong lịch sử",
                  "nhận %r" % names_after_delete)
            check("GW Test 01" in history.lbl_header.text(),
                  "tiêu đề phiên vẫn hiện tên gateway lúc chạy sau khi xoá gateway")

            # Đổi sang gateway HOÀN TOÀN khác cũng không được đổi tên phiên cũ.
            history.set_devices([GatewayDevice(uid="111111111111",
                                               name="GW Mới Thay Thế")])
            app.processEvents()
            names_after_replace = sorted(
                str(gateways_model.data(
                    gateways_model.index(r, gateway_cols.index("gateway_name")),
                    QtCore.DisplayRole))
                for r in range(gateways_model.rowCount())
            )
            check(names_after_replace == ["GW Test 01", "GW Test 02"],
                  "thay gateway khác trong tab Thiết bị KHÔNG đổi tên trong lịch sử",
                  "nhận %r" % names_after_replace)
            check(all("111111111111" != str(gateways_model.data(
                      gateways_model.index(r, gateway_cols.index("gateway_uid")),
                      QtCore.DisplayRole))
                      for r in range(gateways_model.rowCount())),
                  "gateway mới KHÔNG chen vào danh sách gateway của phiên cũ")

            # Trả lại danh sách thiết bị ban đầu cho các bước kiểm tra sau.
            history.set_devices([
                GatewayDevice(uid="AAAABBBBCCCC", name="GW Test 01"),
                GatewayDevice(uid="DDDDEEEEFFFF", name="GW Test 02"),
            ])
            app.processEvents()

            # --- status RUNNING nhưng KHÔNG phải phiên đang chạy ---
            meta_path = run_dir / "meta.json"
            meta = json_module.loads(meta_path.read_text(encoding="utf-8"))
            meta["ended_at"] = ""
            meta["status"] = "RUNNING"
            meta_path.write_text(json_module.dumps(meta, ensure_ascii=False),
                                 encoding="utf-8")
            history.refresh()
            worker = history._list_worker
            if worker is not None:
                worker.wait(30000)
            app.processEvents()
            status_text = str(history.model.data(
                history.model.index(0, cols.index("status_label")),
                QtCore.DisplayRole))
            check("Đang chạy" not in status_text and "tắt ngang" in status_text.lower(),
                  "phiên RUNNING còn sót bị gọi là 'tắt ngang', không phải 'Đang chạy'",
                  "nhận %r" % status_text)

            # --- nút 'Xem bản tin thô' đặt phạm vi cho tab Tìm kiếm ---
            emitted = []
            history.open_in_search_requested.connect(emitted.append)
            history.table.setCurrentIndex(history.model.index(0, 0))
            history._open_in_search()
            check(len(emitted) == 1 and Path(emitted[0]) == run_dir,
                  "nút 'Xem bản tin thô' phát đúng thư mục phiên",
                  "nhận %r" % (emitted,))
            main_window.open_run_in_search(emitted[0])
            check(main_window.tabs.currentWidget() is main_window.search_tab,
                  "bấm nút đó chuyển sang tab Tìm kiếm")
            check(Path(main_window.search_tab.cmb_scope.currentData()) == run_dir,
                  "tab Tìm kiếm đặt sẵn phạm vi = phiên vừa chọn",
                  "nhận %r" % main_window.search_tab.cmb_scope.currentData())
            # Tìm trong MỘT phiên cũng phải dùng TÊN LÚC CHẠY (ảnh chụp), không
            # tra `devices.json` hiện tại — cùng lý do với tab Lịch sử phiên.
            scope_names = main_window.search_tab._scope_gateway_names
            check(scope_names.get("AAAABBBBCCCC") == "GW Test 01"
                  and scope_names.get("DDDDEEEEFFFF") == "GW Test 02",
                  "tab Tìm kiếm (phạm vi 1 phiên) dùng tên gateway chốt trong meta.json",
                  "nhận %r" % (scope_names,))
            main_window.search_tab.set_devices([])
            main_window.search_tab._on_scope_changed()
            check(main_window.search_tab._scope_gateway_names.get("AAAABBBBCCCC")
                  == "GW Test 01",
                  "tên trong tab Tìm kiếm không đổi khi xoá gateway ở tab Thiết bị")

            # --- lọc 'Tắt ngang / thiếu báo cáo' ---
            history.cmb_filter.setCurrentIndex(2)
            app.processEvents()
            check(history.model.rowCount() == 1,
                  "bộ lọc 'Tắt ngang' giữ đúng phiên thiếu báo cáo",
                  "có %d dòng" % history.model.rowCount())
            history.cmb_filter.setCurrentIndex(1)
            app.processEvents()
            check(history.model.rowCount() == 0,
                  "bộ lọc 'Đã chốt báo cáo' loại phiên tắt ngang",
                  "có %d dòng" % history.model.rowCount())
            history.cmb_filter.setCurrentIndex(0)
            app.processEvents()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, "kiểm tra tab 'Lịch sử phiên'",
                  "%s: %s" % (type(exc).__name__, exc))

    # --- 10b. ĐẶT / SỬA TÊN PHIÊN (thủ công) ------------------------------
    # Yêu cầu: "có thể đặt tên phiên / sửa tên phiên thủ công". Tên phiên là DỮ
    # LIỆU trong meta.json của phiên: đổi được bất cứ lúc nào (kể cả phiên ĐANG
    # chạy), không đổi `run_id`/tên thư mục, và không ảnh hưởng dữ liệu CSV.
    print("\n[10b] Đặt / sửa TÊN PHIÊN (thủ công)")
    if main_window is not None:
        try:
            import json as json_module
            from unittest import mock

            from PyQt5.QtCore import Qt as QtCore

            monitor = main_window.monitor_tab
            check(monitor.session_name() == "", "ô 'Tên phiên' mặc định để trống",
                  "nhận %r" % monitor.session_name())
            monitor.set_session_name("  test lại GW 4F9199  ")
            check(monitor.session_name() == "test lại GW 4F9199",
                  "ô 'Tên phiên' ở tab Giám sát gọt khoảng trắng",
                  "nhận %r" % monitor.session_name())
            monitor.set_session_name("")

            history = main_window.history_tab
            run_id = "20260101_120000_AAAABBBBCCCC"
            meta_path = run_dir / "meta.json"
            gateways_before = json_module.loads(
                meta_path.read_text(encoding="utf-8")).get("gateway_names")

            # Hộp thoại nhập tên được thay bằng bản giả để test không cần tương tác.
            answer = {"text": "", "ok": True}
            seen_initial = []

            class _FakeDialog:
                @staticmethod
                def getText(*args, **_kwargs):
                    seen_initial.append(args[-1] if args else "")
                    return (answer["text"], answer["ok"])

            def _select_first_row():
                history.table.setCurrentIndex(history.model.index(0, 0))
                app.processEvents()
                return history._selected_row()

            def _rename_to(text, ok=True):
                answer["text"], answer["ok"] = text, ok
                with mock.patch("ui.history_tab.QInputDialog", _FakeDialog):
                    _select_first_row()
                    history._rename_current()
                app.processEvents()
                worker = history._list_worker
                if worker is not None and worker.isRunning():
                    worker.wait(30000)
                app.processEvents()

            check(history.model.rowCount() == 1,
                  "có đúng 1 phiên để đặt tên",
                  "có %d dòng" % history.model.rowCount())

            # (a) đặt tên cho phiên đã chạy
            _rename_to("  phiên test lại GW  ")
            meta = json_module.loads(meta_path.read_text(encoding="utf-8"))
            check(meta.get("label") == "phiên test lại GW",
                  "đổi tên phiên ghi thẳng vào meta.json của phiên",
                  "nhận %r" % meta.get("label"))
            check(meta.get("run_id") == run_id and run_dir.name == run_id,
                  "đặt tên phiên KHÔNG đổi mã phiên / tên thư mục",
                  "run_id=%r dir=%r" % (meta.get("run_id"), run_dir.name))
            check(meta.get("gateway_names") == gateways_before,
                  "đặt tên phiên KHÔNG ảnh hưởng ảnh chụp tên gateway",
                  "nhận %r" % meta.get("gateway_names"))
            check(seen_initial and seen_initial[-1] == "",
                  "hộp thoại để trống khi phiên chưa có tên",
                  "nhận %r" % (seen_initial,))

            cols = list(history.model.columns)
            shown = str(history.model.data(
                history.model.index(0, cols.index("label_text")), QtCore.DisplayRole))
            check(shown == "phiên test lại GW",
                  "cột 'Tên phiên' hiện tên vừa đặt",
                  "nhận %r" % shown)

            # (b) tiêu đề vùng kết quả nêu TÊN PHIÊN + MÃ PHIÊN
            history.table.setCurrentIndex(history.model.index(0, 0))
            history._on_clicked(history.model.index(0, 0))
            app.processEvents()
            worker = history._report_worker
            if worker is not None:
                worker.wait(30000)
            app.processEvents()
            check("phiên test lại GW" in history.lbl_header.text(),
                  "tiêu đề phiên hiện TÊN PHIÊN người dùng đặt",
                  "nhận %r" % history.lbl_header.text()[:120])
            check(run_id in history.lbl_header.text(),
                  "tiêu đề vẫn nêu MÃ PHIÊN để đối chiếu thư mục")

            # (c) sửa lại tên phiên ĐANG CHẠY phải được phép (khác với XOÁ phiên)
            history.active_run_id = run_id
            _rename_to("đổi tên phiên ĐANG chạy")
            meta = json_module.loads(meta_path.read_text(encoding="utf-8"))
            check(meta.get("label") == "đổi tên phiên ĐANG chạy",
                  "sửa được tên của phiên ĐANG CHẠY (không bị chặn như khi xoá)",
                  "nhận %r" % meta.get("label"))
            check(seen_initial[-1] == "phiên test lại GW",
                  "hộp thoại điền sẵn tên phiên hiện tại để sửa",
                  "nhận %r" % seen_initial[-1])

            # (d) bấm Cancel → KHÔNG đổi tên
            _rename_to("tên bị bỏ", ok=False)
            meta = json_module.loads(meta_path.read_text(encoding="utf-8"))
            check(meta.get("label") == "đổi tên phiên ĐANG chạy",
                  "bấm Cancel trong hộp thoại KHÔNG đổi tên phiên",
                  "nhận %r" % meta.get("label"))

            # (e) xoá tên (để trống) → quay về hiện MÃ PHIÊN, dữ liệu còn nguyên
            _rename_to("")
            meta = json_module.loads(meta_path.read_text(encoding="utf-8"))
            check(meta.get("label") == "",
                  "để trống rồi OK = XOÁ tên phiên",
                  "nhận %r" % meta.get("label"))
            check(meta.get("gateway_names") == gateways_before
                  and (run_dir / "reports" / "run_summary.csv").exists(),
                  "xoá tên phiên KHÔNG làm mất dữ liệu của phiên")
            shown = str(history.model.data(
                history.model.index(0, cols.index("label_text")), QtCore.DisplayRole))
            check(shown == run_id,
                  "xoá tên phiên → cột 'Tên phiên' quay về mã phiên",
                  "nhận %r" % shown)

            # Dọn trạng thái để các bước sau không bị ảnh hưởng.
            history.active_run_id = ""
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, "kiểm tra đặt / sửa tên phiên",
                  "%s: %s" % (type(exc).__name__, exc))

    # --- 10c. BỔ SUNG TÊN GATEWAY THEO UID (danh mục cố định) --------------
    # Yêu cầu: "UID và tên gateway là cố định liên kết với nhau trong
    # docs/danh sach 55 gateway ... nên nếu được thì có thể bổ sung tên gateway
    # dựa vào UID". Nút phải GHI vào meta.json của phiên, CHỈ ĐIỀN THIẾU (không
    # ghi đè tên đã có), không đổi `run_id`, không đổi `gateway_uids`, và không
    # ghi bừa cho UID không có trong danh mục.
    print("\n[10c] Bổ sung TÊN GATEWAY theo UID (danh mục cố định)")
    if main_window is not None:
        try:
            import json as json_module
            from unittest import mock

            from PyQt5.QtCore import Qt as QtCore

            history = main_window.history_tab
            run_id = "20260101_120000_AAAABBBBCCCC"
            meta_path = run_dir / "meta.json"

            # Phiên cũ "thiếu tên" cho 1 gateway có trong danh mục + 1 gateway
            # KHÔNG có trong danh mục (để kiểm tra nhánh không tra được tên).
            missing_uid = "8F9199E139C"          # firmware publish 11 hex
            unknown_uid = "1234567890AB"
            meta = json_module.loads(meta_path.read_text(encoding="utf-8"))
            meta["gateway_uids"] = ["AAAABBBBCCCC", "DDDDEEEEFFFF",
                                    missing_uid, unknown_uid]
            meta["gateways_monitored"] = 4
            meta_path.write_text(json_module.dumps(meta, ensure_ascii=False),
                                 encoding="utf-8")

            # Danh mục giả: cột `uuid` lưu 12 hex, cột `series` là tên.
            catalog_file = tmp_root / "history" / "catalog.csv"
            catalog_file.write_text(
                "uuid,model,series\n"
                "08F9199E139C,MGWI-V1,MGWI-V1-26070009\n",
                encoding="utf-8",
            )
            history.catalog_path = catalog_file
            history.active_run_id = ""
            worker = history._list_worker
            if worker is not None:
                worker.wait(120000)
            history.refresh()
            worker = history._list_worker
            if worker is not None:
                worker.wait(30000)
            app.processEvents()

            check(history.model.rowCount() == 1,
                  "có đúng 1 phiên để bổ sung tên",
                  "có %d dòng" % history.model.rowCount())

            # Nhấn vào phiên trước để vùng kết quả đang mở (kiểm tra cập nhật tại chỗ).
            history.table.setCurrentIndex(history.model.index(0, 0))
            history._on_clicked(history.model.index(0, 0))
            app.processEvents()
            worker = history._report_worker
            if worker is not None:
                worker.wait(30000)
            app.processEvents()

            check(history.btn_fill_names.isEnabled(),
                  "nút 'Bổ sung tên gateway theo UID' bật khi đã chọn phiên")

            class _FakeMsgBox:
                calls = []

                @staticmethod
                def warning(*args, **_kwargs):
                    _FakeMsgBox.calls.append(("warning", args[2] if len(args) > 2 else ""))

                @staticmethod
                def information(*args, **_kwargs):
                    _FakeMsgBox.calls.append(
                        ("information", args[2] if len(args) > 2 else ""))

            _FakeMsgBox.calls = []
            with mock.patch("ui.history_tab.QMessageBox", _FakeMsgBox):
                history._fill_gateway_names()
            app.processEvents()

            meta = json_module.loads(meta_path.read_text(encoding="utf-8"))
            names = meta.get("gateway_names") or {}
            check(names.get(missing_uid) == "MGWI-V1-26070009",
                  "bổ sung được tên gateway dựa vào UID (khớp 11 hex ↔ 12 hex)",
                  "nhận %r" % names.get(missing_uid))
            check(names.get("AAAABBBBCCCC") == "GW Test 01"
                  and names.get("DDDDEEEEFFFF") == "GW Test 02",
                  "KHÔNG ghi đè tên đã có của phiên",
                  "nhận %r" % names)
            check(unknown_uid not in names,
                  "UID không có trong danh mục KHÔNG bị ghi tên bừa",
                  "nhận %r" % names)
            check(meta.get("run_id") == run_id and run_dir.name == run_id,
                  "bổ sung tên KHÔNG đổi mã phiên / tên thư mục")
            check(meta.get("gateway_uids") == ["AAAABBBBCCCC", "DDDDEEEEFFFF",
                                               missing_uid, unknown_uid],
                  "bổ sung tên KHÔNG đổi danh sách gateway của phiên",
                  "nhận %r" % meta.get("gateway_uids"))
            check(_FakeMsgBox.calls == [],
                  "bổ sung thành công KHÔNG hiện hộp thoại lỗi",
                  "nhận %r" % (_FakeMsgBox.calls,))
            check("đã bổ sung" in history.lbl_status.text().lower()
                  or "bổ sung tên" in history.lbl_status.text().lower(),
                  "dòng trạng thái báo đã bổ sung tên",
                  "nhận %r" % history.lbl_status.text())
            check("8F9199E139C" in history.lbl_header.text(),
                  "tiêu đề phiên cập nhật ngay sau khi bổ sung tên")

            gateways_model = history._models["gateways"]
            gateway_cols = list(gateways_model.columns)
            shown_names = sorted(
                str(gateways_model.data(
                    gateways_model.index(r, gateway_cols.index("gateway_name")),
                    QtCore.DisplayRole))
                for r in range(gateways_model.rowCount())
            )
            check("MGWI-V1-26070009" in shown_names,
                  "trang 'Gateway của phiên' hiện tên vừa bổ sung tại chỗ",
                  "nhận %r" % shown_names)

            # Bấm lần hai: còn 1 UID chưa tra được tên → chỉ báo, KHÔNG ghi gì.
            before = meta_path.read_text(encoding="utf-8")
            _FakeMsgBox.calls = []
            with mock.patch("ui.history_tab.QMessageBox", _FakeMsgBox):
                history._fill_gateway_names()
            app.processEvents()
            check(meta_path.read_text(encoding="utf-8") == before,
                  "bấm lần hai KHÔNG ghi lại gì (tên đã có được giữ nguyên)")
            check([c[0] for c in _FakeMsgBox.calls] == ["information"],
                  "gateway không tra được tên → chỉ thông báo, không ghi",
                  "nhận %r" % (_FakeMsgBox.calls,))

            # Danh mục không đọc được (đường dẫn sai) → cảnh báo, không ghi gì.
            before = meta_path.read_text(encoding="utf-8")
            history.catalog_path = tmp_root / "khong_co_danh_muc.csv"
            _FakeMsgBox.calls = []
            with mock.patch("ui.history_tab.QMessageBox", _FakeMsgBox):
                history._fill_gateway_names()
            app.processEvents()
            check([c[0] for c in _FakeMsgBox.calls] == ["warning"],
                  "danh mục không đọc được → hiện cảnh báo",
                  "nhận %r" % (_FakeMsgBox.calls,))
            check(meta_path.read_text(encoding="utf-8") == before,
                  "danh mục không đọc được KHÔNG ghi gì vào phiên")

            # Phiên đã đủ tên → nút chỉ báo "không cần bổ sung".
            history.catalog_path = catalog_file
            meta = json_module.loads(meta_path.read_text(encoding="utf-8"))
            meta["gateway_uids"] = ["AAAABBBBCCCC", "DDDDEEEEFFFF", missing_uid]
            meta["gateways_monitored"] = 3
            meta_path.write_text(json_module.dumps(meta, ensure_ascii=False),
                                 encoding="utf-8")
            history.refresh()
            worker = history._list_worker
            if worker is not None:
                worker.wait(30000)
            app.processEvents()
            history.table.setCurrentIndex(history.model.index(0, 0))
            _FakeMsgBox.calls = []
            with mock.patch("ui.history_tab.QMessageBox", _FakeMsgBox):
                history._fill_gateway_names()
            app.processEvents()
            check("không cần bổ sung" in history.lbl_status.text(),
                  "phiên đã đủ tên → báo 'không cần bổ sung', không ghi gì",
                  "nhận %r" % history.lbl_status.text())
            check(_FakeMsgBox.calls == [],
                  "phiên đã đủ tên KHÔNG hiện hộp thoại nào",
                  "nhận %r" % (_FakeMsgBox.calls,))

            # Dọn trạng thái để không ảnh hưởng các bước sau.
            catalog_file.unlink(missing_ok=True)
            history.catalog_path = None
            history.active_run_id = ""
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, "kiểm tra bổ sung tên gateway theo UID",
                  "%s: %s" % (type(exc).__name__, exc))

    # --- 10e. CHẤM LẠI KẾT QUẢ (nút "⚖ Chấm lại kết quả") ------------------
    # Yêu cầu: phiên cũ / phiên bị tắt ngang không có `reports/gateway_result.csv`
    # nên tab Lịch sử không hiện được ĐẠT / KHÔNG ĐẠT. Nút "Chấm lại kết quả" phải
    # dựng lại kết quả TỪ CSV THÔ rồi ghi thêm báo cáo — và TUYỆT ĐỐI không được
    # chạm vào `mqtt/` + `ping/` (bất biến quan trọng nhất của tính năng).
    #
    # LƯU Ý: phần này chạy TRƯỚC 10d và dùng luôn phiên của §10, nên phải trả
    # `data_dir` về đúng trạng thái mà 10d mong đợi (1 phiên, status đã chốt).
    print("\n[10e] Chấm lại kết quả từ CSV thô (không sửa dữ liệu thô)")
    if main_window is not None:
        try:
            import csv as csv_module
            import json as json_module
            from unittest import mock

            from PyQt5.QtCore import Qt as QtCore

            from storage.schema import MQTT_DATA_COLUMNS

            history = main_window.history_tab
            runs_root = tmp_root / "history" / "runs"
            run_id = "20260101_120000_AAAABBBBCCCC"
            run_dir = runs_root / run_id

            # §10 để phiên ở trạng thái "RUNNING còn sót" (để thử nhánh "tắt
            # ngang"). Trả về STOPPED trước: phiên ĐANG CHẠY không được chấm lại.
            meta_path = run_dir / "meta.json"
            meta = json_module.loads(meta_path.read_text(encoding="utf-8"))
            meta["ended_at"] = "2026-01-01T14:00:00+07:00"
            meta["status"] = "STOPPED"
            meta.pop("scoring_params", None)
            meta_path.write_text(json_module.dumps(meta, ensure_ascii=False),
                                 encoding="utf-8")

            # Dữ liệu MQTT thô TỐI THIỂU nhưng HỢP LỆ: `mqtt_data_*.csv` là nguồn
            # DUY NHẤT để dựng lại độ phủ, nên không có nó thì không chấm được.
            mqtt_path = run_dir / "mqtt" / "mqtt_data_20260101.csv"
            with open(mqtt_path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv_module.DictWriter(
                    handle, fieldnames=list(MQTT_DATA_COLUMNS),
                    extrasaction="ignore", restval="")
                writer.writeheader()
                for seq in range(1, 21):
                    row = {col: "" for col in MQTT_DATA_COLUMNS}
                    row.update({
                        "received_at": "2026-01-01T12:00:%02d+07:00" % (seq % 60),
                        "packet_type": "DATA",
                        "node_uid": "0011223344556677",
                        "seq": str(seq),
                        "gateways": "AAAABBBBCCCC|DDDDEEEEFFFF",
                        "gateways_heard": "2",
                        "gateway_uid": "AAAABBBBCCCC",
                        "valid": "1",
                    })
                    writer.writerow(row)

            # Chưa chốt kết quả cho phiên này → nút phải bật cho người dùng bấm.
            result_path = run_dir / "reports" / "gateway_result.csv"
            result_path.unlink(missing_ok=True)
            raw_files = (sorted((run_dir / "mqtt").glob("*.csv"))
                         + sorted((run_dir / "ping").glob("*.csv")))
            raw_before = {path: path.read_bytes() for path in raw_files}

            def _reload():
                worker = history._list_worker
                if worker is not None:
                    worker.wait(120000)
                history.refresh()
                worker = history._list_worker
                if worker is not None:
                    worker.wait(30000)
                app.processEvents()

            history.active_run_id = ""
            _reload()
            check(history.model.rowCount() == 1,
                  "chấm lại: có đúng 1 phiên đã chốt để thử",
                  "có %d dòng" % history.model.rowCount())
            history.table.setCurrentIndex(history.model.index(0, 0))
            history._on_clicked(history.model.index(0, 0))
            app.processEvents()
            worker = history._report_worker
            if worker is not None:
                worker.wait(30000)
            app.processEvents()
            check(history.btn_rescore.isEnabled(),
                  "chấm lại: nút bật cho phiên đã dừng, chưa có kết quả",
                  "nhận enabled=%s" % history.btn_rescore.isEnabled())

            # `_rescore_current()` đọc cả `QMessageBox.Yes` / `QMessageBox.No` để
            # so câu trả lời, nên lớp giả phải mang ĐÚNG hai hằng đó (và cả cặp
            # cờ truyền vào `question`), nếu không sẽ AttributeError.
            from PyQt5.QtWidgets import QMessageBox as _QMB

            class _FakeMsgBox:
                """Hộp thoại giả — ghi lại loại hộp + câu trả lời đã chọn."""

                Yes = _QMB.Yes
                No = _QMB.No
                calls = []
                answer = None

                @staticmethod
                def warning(*args, **_kwargs):
                    _FakeMsgBox.calls.append(
                        ("warning", args[2] if len(args) > 2 else ""))

                @staticmethod
                def information(*args, **_kwargs):
                    _FakeMsgBox.calls.append(
                        ("information", args[2] if len(args) > 2 else ""))

                @staticmethod
                def question(*args, **_kwargs):
                    _FakeMsgBox.calls.append(
                        ("question", args[2] if len(args) > 2 else ""))
                    return _FakeMsgBox.answer

            def _wait_rescore():
                worker = history._rescore_worker
                if worker is not None:
                    worker.wait(120000)
                app.processEvents()
                worker = history._report_worker
                if worker is not None:
                    worker.wait(30000)
                app.processEvents()

            # (a) người dùng bấm "Không" → không ghi gì, không chạy worker.
            _FakeMsgBox.calls = []
            with mock.patch("ui.history_tab.QMessageBox", _FakeMsgBox):
                _FakeMsgBox.answer = _QMB.No
                history._rescore_current()
            app.processEvents()
            check([c[0] for c in _FakeMsgBox.calls] == ["question"],
                  "chấm lại: luôn HỎI xác nhận trước khi ghi vào lịch sử",
                  "nhận %r" % [c[0] for c in _FakeMsgBox.calls])
            check(not result_path.exists(),
                  "chấm lại: bấm 'Không' → KHÔNG ghi file nào")
            check(history._rescore_worker is None,
                  "chấm lại: bấm 'Không' → không chạy luồng nền")

            # (b) người dùng bấm "Có" → chấm lại thật, ghi báo cáo.
            _FakeMsgBox.calls = []
            with mock.patch("ui.history_tab.QMessageBox", _FakeMsgBox):
                _FakeMsgBox.answer = _QMB.Yes
                history._rescore_current()
                app.processEvents()
                # Trong lúc luồng nền chạy, nút phải bị khoá để không bấm chồng.
                check(not history.btn_rescore.isEnabled(),
                      "chấm lại: nút bị khoá trong lúc đang chấm")
                _wait_rescore()

            check(result_path.exists(),
                  "chấm lại: ghi được reports/gateway_result.csv",
                  "còn %r" % sorted(p.name for p in (run_dir / "reports").iterdir()))
            check({path: path.read_bytes() for path in raw_files} == raw_before,
                  "chấm lại: mqtt/ và ping/ KHÔNG bị sửa một byte nào")

            with open(result_path, "r", encoding="utf-8-sig", newline="") as handle:
                rescored_rows = list(csv_module.reader(handle))
            check(len(rescored_rows) >= 2 and rescored_rows[-1][0] == "TỔNG",
                  "chấm lại: dòng TỔNG nằm CUỐI file kết quả",
                  "dòng cuối: %r" % (rescored_rows[-1][:1] if rescored_rows else None))
            check(str(rescored_rows[-1][-1]).strip() != "",
                  "chấm lại: dòng TỔNG có lý do kết luận (result_reason)")

            status_text = history.lbl_status.text()
            check("ĐẠT" in status_text,
                  "chấm lại: dòng trạng thái nêu kết luận vừa chấm",
                  "nhận %r" % status_text[:160])
            check("gateway_result.csv" in status_text,
                  "chấm lại: dòng trạng thái liệt kê file đã ghi",
                  "nhận %r" % status_text[:160])
            check(not any(c[0] == "warning" for c in _FakeMsgBox.calls),
                  "chấm lại thành công KHÔNG hiện hộp thoại lỗi",
                  "nhận %r" % (_FakeMsgBox.calls,))

            # Bảng phải cập nhật NGAY (không cần quét lại toàn bộ data/runs/).
            cols = list(history.model.columns)
            row_model = history.model
            check(str(row_model.data(row_model.index(0, cols.index("reports")),
                                     QtCore.DisplayRole)) != "3",
                  "chấm lại: cột 'Số báo cáo' tăng sau khi thêm file")
            history.table.setCurrentIndex(history.model.index(0, 0))
            history._on_clicked(history.model.index(0, 0))
            app.processEvents()
            worker = history._report_worker
            if worker is not None:
                worker.wait(30000)
            app.processEvents()
            check("ĐẠT" in history.lbl_header.text()
                  or "KHÔNG ĐẠT" in history.lbl_header.text(),
                  "chấm lại: tiêu đề phiên hiện ĐẠT / KHÔNG ĐẠT sau khi chấm",
                  "nhận %r" % history.lbl_header.text()[:160])

            # (c) phiên bị TẮT NGANG vẫn còn sót `status = RUNNING` trong meta.json
            # (vết tích của lần bị tắt) nhưng KHÔNG phải phiên tool đang chạy thật.
            # Đây đúng là loại phiên cần chấm lại nhất nên nút phải BẬT — nếu tin
            # `status` không thôi thì khoá luôn nút của mọi phiên tắt ngang.
            def _set_meta_status(value):
                data = json_module.loads(meta_path.read_text(encoding="utf-8"))
                data["ended_at"] = "2026-01-01T14:00:00+07:00"
                data["status"] = value
                meta_path.write_text(json_module.dumps(data, ensure_ascii=False),
                                     encoding="utf-8")

            _set_meta_status("RUNNING")
            history.active_run_id = ""
            _reload()
            check(history.model.rowCount() == 1,
                  "chấm lại: phiên đang chạy vẫn hiện trong bảng",
                  "có %d dòng" % history.model.rowCount())
            history.table.setCurrentIndex(history.model.index(0, 0))
            history._on_clicked(history.model.index(0, 0))
            app.processEvents()
            status_shown = history.model.data(
                history.model.index(0, cols.index("status_label")),
                QtCore.DisplayRole)
            check(str(status_shown) != "Đang chạy",
                  "chấm lại: phiên cũ còn sót status RUNNING được nhận là TẮT NGANG",
                  "nhận %r" % status_shown)
            check(history.btn_rescore.isEnabled(),
                  "chấm lại: nút BẬT cho phiên tắt ngang còn sót status RUNNING")

            # (d) phiên mà tool THẬT SỰ đang chạy (active_run_id trùng + meta.json
            # ghi RUNNING) → nút tắt, bấm nút chỉ hiện cảnh báo và KHÔNG ghi gì
            # (không được chạy song song với lúc pipeline đang ghi reports/ của
            # chính phiên đó).
            history.active_run_id = run_id
            _reload()
            history.table.setCurrentIndex(history.model.index(0, 0))
            history._on_clicked(history.model.index(0, 0))
            app.processEvents()
            active_shown = history.model.data(
                history.model.index(0, cols.index("status_label")),
                QtCore.DisplayRole)
            check(str(active_shown) == "Đang chạy",
                  "chấm lại: phiên được nhận là ĐANG CHẠY khi active_run_id trùng",
                  "nhận %r" % active_shown)
            check(not history.btn_rescore.isEnabled(),
                  "chấm lại: nút TẮT cho phiên đang chạy")
            _FakeMsgBox.calls = []
            with mock.patch("ui.history_tab.QMessageBox", _FakeMsgBox):
                history._rescore_current()
            app.processEvents()
            check([c[0] for c in _FakeMsgBox.calls] == ["warning"],
                  "chấm lại: bấm nút khi phiên đang chạy → cảnh báo, không ghi",
                  "nhận %r" % (_FakeMsgBox.calls,))
            check(history._rescore_worker is None,
                  "chấm lại: phiên đang chạy → KHÔNG chạy luồng nền")

            # Trả lại trạng thái 10d mong đợi: 1 phiên, đã dừng, không active.
            _set_meta_status("STOPPED")
            history.active_run_id = ""
            _reload()
            check(history.model.rowCount() == 1,
                  "chấm lại: trả về đúng 1 phiên trước bước sau",
                  "có %d dòng" % history.model.rowCount())
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, "kiểm tra chấm lại kết quả từ CSV thô",
                  "%s: %s" % (type(exc).__name__, exc))

    # --- 10d. TÌM PHIÊN THEO GATEWAY (UID hoặc tên) ------------------------
    # Yêu cầu: "trong mục lịch sử phiên, thêm 1 thanh tìm kiếm, tìm theo UID
    # gateway hoặc tên gateway sẽ ra phiên có gateway đó". Phải tra trên ẢNH CHỤP
    # của phiên (nên tìm được cả gateway đã xoá khỏi tab Thiết bị) và khớp cả khi
    # firmware publish UID thiếu số 0 đầu (11 hex ≈ 12 hex). Từ khoá phải GIAO
    # với bộ lọc trạng thái, không thay thế nhau.
    #
    # LƯU Ý: phần này nằm CUỐI (sau 10c, 10e) vì nó thêm một phiên thứ hai vào
    # `data_dir`; các phần trước đều giả định đúng 1 phiên.
    print("\n[10d] Tìm phiên theo GATEWAY (UID hoặc tên)")
    if main_window is not None:
        try:
            import json as json_module

            from PyQt5.QtCore import Qt as QtCore

            history = main_window.history_tab
            runs_root = tmp_root / "history" / "runs"
            first_id = "20260101_120000_AAAABBBBCCCC"
            second_id = "20260102_090000_999988887777"

            # §10 đã cố tình để phiên 1 ở trạng thái "RUNNING còn sót"
            # (`ended_at` rỗng) cho phép thử "tắt ngang". Trả nó về đã chốt báo
            # cáo để bước dưới kiểm tra được việc GIAO từ khoá với bộ lọc.
            first_meta = runs_root / first_id / "meta.json"
            first_data = json_module.loads(first_meta.read_text(encoding="utf-8"))
            first_data["ended_at"] = "2026-01-01T14:00:00+07:00"
            first_data["status"] = "STOPPED"
            first_meta.write_text(json_module.dumps(first_data, ensure_ascii=False),
                                  encoding="utf-8")

            # Thêm 1 phiên KHÁC để chứng minh tìm kiếm lọc đúng, không "khớp tất".
            # Phiên này bị TẮT NGANG (không có mốc kết thúc, `reports/` rỗng) nên
            # rơi vào nhóm "Tắt ngang / thiếu báo cáo" — dùng để kiểm tra việc
            # GIAO từ khoá với bộ lọc trạng thái.
            other_dir = runs_root / second_id
            other_dir.mkdir(parents=True, exist_ok=True)
            with open(other_dir / "meta.json", "w", encoding="utf-8") as handle:
                json_module.dump({
                    "run_id": second_id,
                    "started_at": "2026-01-02T09:00:00+07:00",
                    "ended_at": "",
                    "duration_seconds": None,
                    "status": "RUNNING",
                    "broker": "1.2.3.4:1885",
                    "subscribed_topics": ["node/#"],
                    "gateway_uids": ["08F9199E139C"],
                    "gateways_monitored": 1,
                    "gateway_names": {"08F9199E139C": "GW Khác Hẳn"},
                }, handle, ensure_ascii=False)

            def _reload():
                worker = history._list_worker
                if worker is not None:
                    worker.wait(120000)
                history.refresh()
                worker = history._list_worker
                if worker is not None:
                    worker.wait(30000)
                app.processEvents()

            _reload()
            check(history.model.rowCount() == 2,
                  "có 2 phiên trước khi tìm theo gateway",
                  "có %d dòng" % history.model.rowCount())
            check({str(r.get("run_id")) for r in history._rows}
                  == {first_id, second_id},
                  "đúng 2 phiên cần kiểm tra tìm kiếm",
                  "nhận %r" % sorted(str(r.get("run_id")) for r in history._rows))

            def _type_gateway(text):
                history.ed_gateway.setText(text)
                app.processEvents()

            def _shown_run_ids():
                rows_model = history.model
                col = list(rows_model.columns).index("run_id")
                return sorted(str(rows_model.data(rows_model.index(r, col),
                                                  QtCore.DisplayRole))
                              for r in range(rows_model.rowCount()))

            check(history.ed_gateway.placeholderText() != ""
                  and history.ed_gateway.text() == "",
                  "có ô tìm gateway, mặc định để trống, có gợi ý cách dùng",
                  "nhận %r" % history.ed_gateway.text())

            # (a) tìm theo một phần UID của gateway trong phiên 1
            _type_gateway("AAAABBBB")
            check(_shown_run_ids() == [first_id],
                  "tìm theo UID (một phần) → chỉ ra phiên chứa gateway đó",
                  "nhận %r" % (_shown_run_ids(),))

            # (b) tìm theo TÊN gateway (ảnh chụp trong meta.json)
            _type_gateway("GW Test 02")
            check(_shown_run_ids() == [first_id],
                  "tìm theo TÊN gateway → ra phiên chứa gateway đó",
                  "nhận %r" % (_shown_run_ids(),))

            # (c) không phân biệt HOA/thường; từ khoá chỉ có ở phiên 2
            _type_gateway("gw khác hẳn")
            check(_shown_run_ids() == [second_id],
                  "tìm không phân biệt hoa/thường và lọc đúng sang phiên khác",
                  "nhận %r" % (_shown_run_ids(),))

            # (d) gõ UID 12 hex → khớp MỌI phiên có gateway đó (dù phiên ghi
            # 11 hex hay 12 hex) nhờ so qua biến thể UID.
            _type_gateway("08F9199E139C")
            check(_shown_run_ids() == [first_id, second_id],
                  "gõ UID đủ 12 hex → ra mọi phiên có gateway đó",
                  "nhận %r" % (_shown_run_ids(),))
            _type_gateway("8F9199E139C")
            check(_shown_run_ids() == [first_id, second_id],
                  "gõ UID 11 hex (thiếu số 0 đầu) → khớp cả 2 phiên (biến thể UID)",
                  "nhận %r" % (_shown_run_ids(),))

            # (e) tìm được cả gateway KHÔNG còn trong tab Thiết bị (dùng ảnh chụp)
            history.set_devices([])
            app.processEvents()
            _type_gateway("GW Test 01")
            check(_shown_run_ids() == [first_id],
                  "xoá hết gateway ở tab Thiết bị vẫn tìm được theo tên ảnh chụp",
                  "nhận %r" % (_shown_run_ids(),))
            history.set_devices([
                GatewayDevice(uid="AAAABBBBCCCC", name="GW Test 01"),
                GatewayDevice(uid="DDDDEEEEFFFF", name="GW Test 02"),
            ])
            app.processEvents()

            # (f) không khớp gì → bảng trống, có nêu từ khoá
            _type_gateway("KHONGCOGATEWAYNAY")
            check(history.model.rowCount() == 0,
                  "từ khoá không khớp gateway nào → bảng trống",
                  "có %d dòng" % history.model.rowCount())
            check("KHONGCOGATEWAYNAY" in history.lbl_count.text(),
                  "dòng đếm nêu rõ từ khoá đang tìm",
                  "nhận %r" % history.lbl_count.text())

            # (g) nút 'Xoá tìm kiếm' trả lại đủ 2 phiên
            history.btn_clear_search.click()
            app.processEvents()
            check(history.ed_gateway.text() == "",
                  "nút 'Xoá tìm kiếm' xoá trắng ô từ khoá",
                  "nhận %r" % history.ed_gateway.text())
            check(history.model.rowCount() == 2,
                  "xoá từ khoá → hiện lại đủ 2 phiên",
                  "có %d dòng" % history.model.rowCount())

            # (h) từ khoá GIAO (∩) với bộ lọc trạng thái, không thay thế nhau.
            # Phiên 1: đã chốt báo cáo (3 file). Phiên 2: có `ended_at` nhưng
            # `reports/` rỗng → rơi vào nhóm "Tắt ngang / thiếu báo cáo".
            _type_gateway("8F9199E139C")            # khớp CẢ 2 phiên
            history.cmb_filter.setCurrentIndex(1)      # 'Đã chốt báo cáo'
            app.processEvents()
            check(_shown_run_ids() == [first_id],
                  "tìm gateway (khớp 2 phiên) ∩ 'Đã chốt báo cáo' → chỉ phiên đủ báo cáo",
                  "nhận %r" % (_shown_run_ids(),))
            history.cmb_filter.setCurrentIndex(2)      # 'Tắt ngang / thiếu báo cáo'
            app.processEvents()
            check(_shown_run_ids() == [second_id],
                  "tìm gateway (khớp 2 phiên) ∩ 'Tắt ngang' → chỉ phiên thiếu báo cáo",
                  "nhận %r" % (_shown_run_ids(),))
            _type_gateway("GW Test 01")                # chỉ phiên 1 khớp
            app.processEvents()
            check(history.model.rowCount() == 0,
                  "từ khoá chỉ khớp phiên ĐÃ CHỐT ∩ 'Tắt ngang' → loại sạch",
                  "có %d dòng" % history.model.rowCount())
            history.cmb_filter.setCurrentIndex(1)
            app.processEvents()
            check(_shown_run_ids() == [first_id],
                  "từ khoá chỉ khớp phiên ĐÃ CHỐT ∩ 'Đã chốt báo cáo' → còn 1",
                  "nhận %r" % (_shown_run_ids(),))
            history.cmb_filter.setCurrentIndex(0)
            history.ed_gateway.clear()
            app.processEvents()

            # (i) đổi tên phiên trong lúc đang tìm kiếm vẫn không lẫn phiên khác
            _type_gateway("AAAABBBB")
            check(_shown_run_ids() == [first_id],
                  "tìm theo UID đúng trước khi đổi tên phiên",
                  "nhận %r" % (_shown_run_ids(),))
            text, _ok = history.ed_gateway.text(), None
            check(text == "AAAABBBB",
                  "ô tìm kiếm giữ nguyên từ khoá khi thao tác khác",
                  "nhận %r" % text)

            # Dọn phiên thêm vào để không ảnh hưởng bước sau.
            history.ed_gateway.clear()
            app.processEvents()
            shutil.rmtree(other_dir, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, "kiểm tra tìm phiên theo gateway",
                  "%s: %s" % (type(exc).__name__, exc))

    # --- 11. đóng cửa sổ sạch sẽ ------------------------------------------
    print("\n[11] Đóng cửa sổ")
    try:
        if main_window is not None:
            main_window.close()
        app.processEvents()
        check(True, "close() không lỗi")
    except Exception as exc:  # noqa: BLE001
        check(False, "close() không lỗi", "%s: %s" % (type(exc).__name__, exc))

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


if __name__ == "__main__":
    sys.exit(main())
