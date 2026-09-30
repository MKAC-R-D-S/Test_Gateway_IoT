"""Đường dẫn chuẩn cho tool — chạy được CẢ khi đóng gói `.exe` (PyInstaller).

Vì sao cần file này
-------------------
Khi chạy từ mã nguồn, mọi thứ tương đối với `__file__` đều đúng. Nhưng khi
đóng gói bằng PyInstaller (chế độ onefile), `__file__` trỏ vào thư mục tạm
`_MEIxxxx` được **giải nén lúc chạy và XOÁ lúc thoát**. Nếu vẫn tính đường dẫn
theo `__file__` thì `config/`, `data/`, `logs/` sẽ nằm trong thư mục tạm đó →
cấu hình và kết quả test biến mất sau khi đóng tool.

Cách giải quyết: tách đôi "gốc ĐỌC" và "gốc GHI".

  - **Gốc ghi (`APP_ROOT`)** — nơi cấu hình/dữ liệu/log phải nằm lại lâu dài:
      * chạy mã nguồn : thư mục chứa `main.py`
      * chạy `.exe`    : thư mục chứa file `.exe` (`Path(sys.executable).parent`)
    Nhờ vậy người dùng copy `TestGatewayIoT.exe` vào đâu, `config/`, `data/`,
    `logs/` được tạo ngay cạnh đó — không cần cài Python, không mất dữ liệu.

  - **Gốc đọc tài nguyên (`RESOURCE_ROOT`)** — dữ liệu chỉ đọc đi kèm bản build
    (danh mục gateway nhà máy, `config.json`/`devices.json` mẫu):
      * chạy mã nguồn : thư mục chứa `main.py`
      * chạy `.exe`    : `sys._MEIPASS` (thư mục giải nén tạm của PyInstaller)

Dùng `resource_path()` để đọc tài nguyên: hàm này tự chọn `_MEIPASS` khi có,
và **ưu tiên bản do người dùng sửa trong `APP_ROOT`** nếu tồn tại — nhờ vậy bản
danh mục nhà máy đi kèm exe vẫn có thể bị thay thế bằng file nằm cạnh exe.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Optional

#: True khi đang chạy trong bản đóng gói (PyInstaller/Nuitka dạng frozen).
IS_FROZEN: bool = bool(getattr(sys, "frozen", False))

#: Gốc mã nguồn (chỉ đúng khi chạy từ file `.py`).
SOURCE_ROOT: Path = Path(__file__).resolve().parent.parent


def _is_frozen() -> bool:
    """Đọc cờ frozen **tại thời điểm gọi** (không dùng hằng số đã chụp lúc import).

    Đọc động để (a) test được hành vi bản đóng gói ngay trên máy dev, và (b) an
    toàn nếu công cụ đóng gói khác đặt cờ muộn hơn lúc import module này.
    """
    return bool(getattr(sys, "frozen", False))


def _app_root() -> Path:
    """Thư mục phải chứa `config/`, `data/`, `logs/` (gốc GHI)."""
    if _is_frozen():
        # `sys.executable` = đường dẫn file .exe -> lấy thư mục chứa nó.
        try:
            parent = Path(sys.executable).resolve().parent
            if parent.is_dir():
                return parent
        except (OSError, ValueError, TypeError):
            pass
        # `sys.executable` rỗng/hỏng: thà ghi vào cwd còn hơn không khởi động được.
        return Path.cwd()
    return SOURCE_ROOT


def _resource_root() -> Path:
    """Thư mục chứa tài nguyên chỉ đọc đi kèm bản build (gốc ĐỌC)."""
    if _is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
    return SOURCE_ROOT


#: Gốc GHI — cấu hình, dữ liệu test, log đều nằm dưới đây.
APP_ROOT: Path = _app_root()

#: Gốc ĐỌC — tài nguyên đi kèm bản build (danh mục gateway, config mẫu).
RESOURCE_ROOT: Path = _resource_root()


def app_dir(name: str, create: bool = False) -> Path:
    """Đường dẫn `<APP_ROOT>/<name>`; `create=True` thì tạo luôn (kiểu mkdir -p)."""
    directory = APP_ROOT / name
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def resource_path(relative: str, prefer_app_root: bool = True) -> Path:
    """Đường dẫn tài nguyên `<relative>` (dạng `docs/foo.csv`).

    Thứ tự tìm:
      1. `<APP_ROOT>/<relative>`  — bản người dùng đặt/sửa cạnh exe (nếu có)
      2. `<RESOURCE_ROOT>/<relative>` — bản đi kèm bản build

    Luôn trả về một `Path` (có thể chưa tồn tại); người gọi tự kiểm tra
    `.exists()` hoặc chấp nhận giá trị mặc định.
    """
    rel = Path(relative)
    if rel.is_absolute():
        return rel
    app_candidate = APP_ROOT / rel
    if prefer_app_root and app_candidate.exists():
        return app_candidate
    return RESOURCE_ROOT / rel


def seed_file(
    relative: str,
    overwrite: bool = False,
    source_root: Optional[Path] = None,
) -> Optional[Path]:
    """Copy tài nguyên mẫu từ gốc ĐỌC sang gốc GHI nếu đích chưa có.

    Dùng cho lần chạy đầu tiên của bản `.exe`: đưa `config/config.json` +
    `config/devices.json` đi kèm ra thư mục cạnh exe để người dùng sửa được.

    Trả về đường dẫn đích nếu file tồn tại sau khi gọi, `None` nếu không có
    nguồn để copy. **Không bao giờ ghi đè** trừ khi `overwrite=True`.
    """
    rel = Path(relative)
    destination = APP_ROOT / rel
    if destination.exists() and not overwrite:
        return destination

    root = source_root if source_root is not None else RESOURCE_ROOT
    source = root / rel
    if not source.exists() or source.resolve() == destination.resolve():
        return destination if destination.exists() else None

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    except OSError:
        # Không copy được (ổ chỉ đọc, quyền hạn...) — tool vẫn phải chạy tiếp,
        # người dùng có thể tự tạo config từ GUI.
        return destination if destination.exists() else None
    return destination


def describe() -> str:
    """Chuỗi mô tả ngắn để ghi vào log lúc khởi động (giúp debug bản đóng gói)."""
    mode = "frozen(.exe)" if _is_frozen() else "source"
    return "mode=%s app_root=%s resource_root=%s" % (mode, APP_ROOT, RESOURCE_ROOT)
