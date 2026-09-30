"""Kiểm thử `core.paths` — gốc đường dẫn cho CẢ mã nguồn lẫn bản `.exe`.

Vì sao cần test riêng: đây là thứ dễ làm mất dữ liệu test nhất khi đóng gói.
Trong bản onefile của PyInstaller, `__file__` trỏ vào thư mục tạm `_MEIPASS` bị
xoá khi thoát — nếu `config/`, `data/`, `logs/` vẫn tính theo `__file__` thì
người dùng chạy xong là mất sạch cấu hình và kết quả.

Vì `core.paths` tính `APP_ROOT`/`RESOURCE_ROOT` **một lần lúc import**, các test
dưới đây kiểm hành vi qua việc gọi lại các hàm nội bộ sau khi vá `sys.frozen` /
`sys.executable` / `sys._MEIPASS` — tức là kiểm chính logic quyết định đường dẫn,
không phải kiểm giá trị hằng số đã đóng băng.
"""

import sys

import pytest

from core import paths


@pytest.fixture
def restore_sys_state():
    """Khôi phục `sys.frozen` / `sys.executable` / `sys._MEIPASS` sau mỗi test."""
    saved = {}
    for name in ("frozen", "executable", "_MEIPASS"):
        saved[name] = getattr(sys, name, None)
        if hasattr(sys, name):
            delattr(sys, name)
    yield
    for name, value in saved.items():
        if hasattr(sys, name):
            delattr(sys, name)
        if value is not None:
            setattr(sys, name, value)


# --------------------------------------------------------------------------
# Chạy từ mã nguồn
# --------------------------------------------------------------------------
def test_source_mode_goc_la_thu_muc_du_an():
    """Không frozen → `APP_ROOT`/`RESOURCE_ROOT` = thư mục dự án (`core/` ở dưới nó)."""
    assert paths.IS_FROZEN is False
    assert (paths.APP_ROOT / "core" / "paths.py").exists()
    assert paths.APP_ROOT == paths.RESOURCE_ROOT


# --------------------------------------------------------------------------
# Quyết định đường dẫn khi frozen (kiểm trực tiếp hàm nội bộ)
# --------------------------------------------------------------------------
def test_frozen_goc_ghi_la_thu_muc_chua_exe(restore_sys_state, tmp_path):
    """Frozen → gốc GHI là thư mục chứa `.exe`, KHÔNG phải thư mục giải nén tạm."""
    exe_dir = tmp_path / "release" / "1.0.0"
    exe_dir.mkdir(parents=True)
    exe = exe_dir / "TestGatewayIoT.exe"
    exe.write_bytes(b"")
    meipass = tmp_path / "_MEI12345"
    meipass.mkdir()

    sys.frozen = True
    sys.executable = str(exe)
    sys._MEIPASS = str(meipass)

    assert paths._app_root() == exe_dir.resolve()
    # Gốc ĐỌC tài nguyên là thư mục tạm của PyInstaller.
    assert paths._resource_root() == meipass
    # Đây chính là điều dễ sai: ghi vào _MEIPASS thì dữ liệu sẽ bị xoá.
    assert paths._app_root() != paths._resource_root()


def test_frozen_khong_co_meipass_thi_doc_tu_thu_muc_du_an(restore_sys_state, tmp_path):
    """Frozen nhưng thiếu `_MEIPASS` (onedir cũ) → không được nổ, rơi về thư mục dự án."""
    exe = tmp_path / "TestGatewayIoT.exe"
    exe.write_bytes(b"")
    sys.frozen = True
    sys.executable = str(exe)

    assert paths._app_root() == tmp_path.resolve()
    assert paths._resource_root() == paths.SOURCE_ROOT


def test_frozen_executable_loi_thi_roi_ve_cwd(restore_sys_state, tmp_path, monkeypatch):
    """`sys.executable` hỏng/không hợp lệ → dùng cwd thay vì ném lỗi khởi động."""
    sys.frozen = True
    sys.executable = "\x00khong-hop-le\x00"
    monkeypatch.chdir(tmp_path)

    assert paths._app_root() == tmp_path.resolve()


# --------------------------------------------------------------------------
# app_dir / resource_path
# --------------------------------------------------------------------------
def test_app_dir_tao_thu_muc_khi_duoc_yeu_cau(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "APP_ROOT", tmp_path)
    assert paths.app_dir("logs").exists() is False
    created = paths.app_dir("logs", create=True)
    assert created == tmp_path / "logs"
    assert created.is_dir()


def test_resource_path_uu_tien_ban_dat_canh_exe(monkeypatch, tmp_path):
    """File người dùng đặt cạnh exe phải THẮNG bản đi kèm bản build (đọc dễ, sửa được)."""
    app_root = tmp_path / "app"
    resource_root = tmp_path / "bundle"
    (app_root / "docs").mkdir(parents=True)
    (resource_root / "docs").mkdir(parents=True)
    (app_root / "docs" / "catalog.csv").write_text("user", encoding="utf-8")
    (resource_root / "docs" / "catalog.csv").write_text("bundled", encoding="utf-8")

    monkeypatch.setattr(paths, "APP_ROOT", app_root)
    monkeypatch.setattr(paths, "RESOURCE_ROOT", resource_root)

    assert paths.resource_path("docs/catalog.csv") == app_root / "docs" / "catalog.csv"
    # Bật prefer_app_root=False để buộc lấy bản đi kèm.
    assert (paths.resource_path("docs/catalog.csv", prefer_app_root=False)
            == resource_root / "docs" / "catalog.csv")


def test_resource_path_roi_ve_ban_di_kem_khi_chua_co(monkeypatch, tmp_path):
    app_root = tmp_path / "app"
    resource_root = tmp_path / "bundle"
    app_root.mkdir()
    (resource_root / "docs").mkdir(parents=True)
    (resource_root / "docs" / "catalog.csv").write_text("bundled", encoding="utf-8")

    monkeypatch.setattr(paths, "APP_ROOT", app_root)
    monkeypatch.setattr(paths, "RESOURCE_ROOT", resource_root)

    assert (paths.resource_path("docs/catalog.csv")
            == resource_root / "docs" / "catalog.csv")


def test_resource_path_giu_nguyen_duong_dan_tuyet_doi(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "APP_ROOT", tmp_path / "app")
    monkeypatch.setattr(paths, "RESOURCE_ROOT", tmp_path / "bundle")
    absolute = tmp_path / "ngoai" / "file.csv"
    assert paths.resource_path(str(absolute)) == absolute


# --------------------------------------------------------------------------
# seed_file — copy config mẫu ra cạnh exe
# --------------------------------------------------------------------------
def test_seed_file_copy_va_khong_bao_gio_ghi_de(monkeypatch, tmp_path):
    """Lần chạy đầu copy config mẫu ra; lần sau người dùng đã sửa thì PHẢI giữ nguyên."""
    app_root = tmp_path / "app"
    resource_root = tmp_path / "bundle"
    app_root.mkdir()
    (resource_root / "config").mkdir(parents=True)
    (resource_root / "config" / "config.json").write_text("MAU", encoding="utf-8")

    monkeypatch.setattr(paths, "APP_ROOT", app_root)
    monkeypatch.setattr(paths, "RESOURCE_ROOT", resource_root)

    seeded = paths.seed_file("config/config.json")
    assert seeded == app_root / "config" / "config.json"
    assert seeded.read_text(encoding="utf-8") == "MAU"

    # Người dùng sửa -> lần sau KHÔNG được ghi đè.
    seeded.write_text("NGUOI-DUNG-SUA", encoding="utf-8")
    paths.seed_file("config/config.json")
    assert seeded.read_text(encoding="utf-8") == "NGUOI-DUNG-SUA"


def test_seed_file_tra_none_khi_khong_co_nguon(monkeypatch, tmp_path):
    app_root = tmp_path / "app"
    resource_root = tmp_path / "bundle"
    app_root.mkdir()
    resource_root.mkdir()
    monkeypatch.setattr(paths, "APP_ROOT", app_root)
    monkeypatch.setattr(paths, "RESOURCE_ROOT", resource_root)
    assert paths.seed_file("config/khong-ton-tai.json") is None


def test_seed_file_nguon_va_dich_trung_nhau_khong_lam_gi(monkeypatch, tmp_path):
    """Chạy từ mã nguồn: nguồn = đích → no-op, không tự copy đè lên chính nó."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.json").write_text("goc", encoding="utf-8")
    monkeypatch.setattr(paths, "APP_ROOT", tmp_path)
    monkeypatch.setattr(paths, "RESOURCE_ROOT", tmp_path)

    result = paths.seed_file("config/config.json")
    assert result == tmp_path / "config" / "config.json"
    assert result.read_text(encoding="utf-8") == "goc"


def test_seed_file_loi_copy_khong_lam_sap_tool(monkeypatch, tmp_path):
    """Copy hỏng (ổ chỉ đọc, quyền hạn) → trả về None chứ KHÔNG ném lỗi."""
    app_root = tmp_path / "app"
    resource_root = tmp_path / "bundle"
    app_root.mkdir()
    (resource_root / "config").mkdir(parents=True)
    (resource_root / "config" / "config.json").write_text("MAU", encoding="utf-8")
    monkeypatch.setattr(paths, "APP_ROOT", app_root)
    monkeypatch.setattr(paths, "RESOURCE_ROOT", resource_root)

    def boom(*args, **kwargs):
        raise OSError("o chi doc")

    monkeypatch.setattr(paths.shutil, "copy2", boom)
    assert paths.seed_file("config/config.json") is None


# --------------------------------------------------------------------------
# describe — dòng log giúp gỡ rối bản đóng gói
# --------------------------------------------------------------------------
def test_describe_neu_ro_che_do_va_hai_goc():
    text = paths.describe()
    assert "mode=" in text
    assert "app_root=" in text
    assert "resource_root=" in text
    assert "source" in text or "frozen" in text
