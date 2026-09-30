# -*- mode: python ; coding: utf-8 -*-
"""Cấu hình đóng gói PyInstaller cho Test Gateway IoT.

Build:
    pyinstaller --clean --noconfirm TestGatewayIoT.spec

Kết quả: `dist/TestGatewayIoT.exe` — chạy được trên Windows **không cần Python**.

Quyết định thiết kế
-------------------
* **console=True** (không phải `--windowed`): tool có cả chế độ GUI *và*
  `--headless --hours N`. Nếu tắt console thì các dòng log tiến độ của chế độ
  headless biến mất, đồng thời không đọc được thông báo lỗi khi khởi động hỏng.
  Console đen đi kèm cửa sổ GUI là cái giá nhỏ để giữ tính năng.
* **onefile** (`EXE(...)` với tất cả trong một file): người dùng chỉ cần copy
  *một* file `.exe` — đúng yêu cầu "xuất ra file .exe để up lên git".
* **Tài nguyên đi kèm** (`datas`): `config/*.json` mẫu, danh mục gateway nhà máy
  và hướng dẫn sử dụng. Chúng được giải nén vào `sys._MEIPASS`; lần chạy đầu
  `core/paths.py::seed_file()` copy `config/*.json` + hướng dẫn ra **cạnh exe**
  để người dùng sửa được và không mất khi đóng tool.
* **Bỏ 4G**: tool chỉ dùng TCP/IP thường (MQTT tới broker + ICMP ping). Không có
  thư viện modem/cellular nào trong build, nên bản exe **không thể** và **không
  cần** dùng 4G — chỉ cần mạng LAN/Ethernet thấy được broker và các gateway
  (`192.168.x.x`). Xem ghi chú cạnh `mqtt_host` trong `config/config.json`.
* **`excludes`**: cắt các module Qt/thư viện khoa học không dùng tới để giảm
  kích thước (PyQt5 kéo theo rất nhiều thứ). Danh sách dưới đây chỉ loại những
  module mà mã nguồn **chưa bao giờ** import; `QtCore/QtGui/QtWidgets` được giữ
  nguyên.
"""

from pathlib import Path

# --------------------------------------------------------------------------
# Đường dẫn dự án — spec được chạy với cwd = thư mục chứa spec.
# --------------------------------------------------------------------------
PROJECT_ROOT = Path(SPECPATH).resolve()
SRC_DIR = PROJECT_ROOT / "src"  # không tồn tại ở tool này, giữ để tương thích

hiddenimports = [
    # Các package nội bộ được import BÊN TRONG hàm (dynamic-ish) — khai báo tường
    # minh để chắc chắn PyInstaller đưa vào build.
    "analytics",
    "analytics.anomaly_detector",
    "analytics.correlation",
    "analytics.error_deduper",
    "analytics.focused_node",
    "analytics.gateway_rescore",
    "analytics.gateway_result",
    "analytics.packet_deduper",
    "analytics.ping_stats",
    "analytics.sequence_tracker",
    "core",
    "core.config",
    "core.gateway_directory",
    "core.models",
    "core.parser",
    "core.paths",
    "core.pipeline",
    "core.uid",
    "runtime",
    "runtime.mqtt_subscriber",
    "runtime.ping_monitor",
    "runtime.runner",
    "storage",
    "storage.csv_writer",
    "storage.reader",
    "storage.schema",
    "storage.session",
    "ui",
    "ui.config_tab",
    "ui.devices_tab",
    "ui.history_tab",
    "ui.log_viewer",
    "ui.main_window",
    "ui.monitor_tab",
    "ui.search_tab",
    "ui.table_model",
    "ui.theme",
    # Phụ thuộc runtime của paho-mqtt (được import theo tên động).
    "paho.mqtt.client",
    "paho.mqtt.publish",
    "paho.mqtt.subscribe",
    # `main.check_dependencies()` gọi `__import__("requests")` bằng CHUỖI nên
    # PyInstaller không thấy → phải khai báo tay, kèm phụ thuộc của nó.
    "requests",
    "urllib3",
    "certifi",
    "idna",
    "charset_normalizer",
]

excludes = [
    # Thư viện khoa học / đồ họa tool không dùng — cắt mạnh kích thước.
    "matplotlib",
    "numpy",
    "scipy",
    "pandas",
    "PIL",
    "IPython",
    "notebook",
    "jupyter",
    "pytest",
    "tkinter",
    "test",
    "unittest.mock",
    # Module Qt không có trong mã nguồn tool.
    "PyQt5.QtWebEngineWidgets",
    "PyQt5.QtWebEngineCore",
    "PyQt5.QtWebEngine",
    "PyQt5.QtWebKit",
    "PyQt5.QtWebKitWidgets",
    "PyQt5.QtWebSockets",
    "PyQt5.QtWebChannel",
    "PyQt5.QtQml",
    "PyQt5.QtQuick",
    "PyQt5.QtQuickWidgets",
    "PyQt5.QtMultimedia",
    "PyQt5.QtMultimediaWidgets",
    "PyQt5.QtBluetooth",
    "PyQt5.QtNfc",
    "PyQt5.QtDesigner",
    "PyQt5.QtHelp",
    "PyQt5.QtLocation",
    "PyQt5.QtPositioning",
    "PyQt5.QtSensors",
    "PyQt5.QtSerialPort",
    "PyQt5.QtSql",
    "PyQt5.QtTest",
    "PyQt5.QtXml",
    "PyQt5.QtXmlPatterns",
    "PyQt5.QtCharts",
    "PyQt5.QtDataVisualization",
    "PyQt5.QtGamepad",
    "PyQt5.QtPurchasing",
    "PyQt5.QtSpeech",
    "PyQt5.QtTextToSpeech",
    "PyQt5.QtRemoteObjects",
    "PyQt5.QtNetworkAuth",
    "PyQt5.Qt3DCore",
    "PyQt5.Qt3DRender",
    "PyQt5.Qt3DInput",
    "PyQt5.Qt3DLogic",
    "PyQt5.Qt3DAnimation",
    "PyQt5.Qt3DExtras",
    "PyQt5.QtScript",
    "PyQt5.QtScriptTools",
    "PyQt5.QtX11Extras",
    "PyQt5.QtAndroidExtras",
    "PyQt5.QtDBus",
    # pyserial có trong requirements.txt nhưng KHÔNG được import ở đâu.
    "serial",
    "serial.tools",
]

# Tài nguyên đi kèm bản build (giải nén vào sys._MEIPASS).
datas = [
    (str(PROJECT_ROOT / "config" / "config.json"), "config"),
    (str(PROJECT_ROOT / "config" / "devices.json"), "config"),
    (str(PROJECT_ROOT / "docs" / "Danh sach 55 gateway.csv"), "docs"),
    (str(PROJECT_ROOT / "HUONG_DAN_SU_DUNG.md"), "."),
    (str(PROJECT_ROOT / "README.md"), "."),
]


a = Analysis(
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="TestGatewayIoT",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX hay bị antivirus gạt nhầm — để False cho an toàn.
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
