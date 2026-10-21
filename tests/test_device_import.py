"""Kiểm thử nhập danh sách thiết bị từ CSV (`core/device_import.py`).

Vì sao phải kiểm bằng test chứ không chỉ đọc code: trình nhập này đọc file NGƯỜI
DÙNG đưa vào, không phải file do tool tự ghi — nên nó phải chịu được những thứ
mà phần còn lại của tool chưa bao giờ gặp:

  * **Bảng mã**: mọi chỗ khác trong tool đọc/ghi `utf-8-sig` (định dạng tool tự
    sinh). File thi công thật lại là **cp1252** (byte `0xE3` = `ã`), và
    `utf-8-sig` ném `UnicodeDecodeError` ngay dòng tiêu đề. Bộ test cũ KHÔNG phủ
    ca này vì chưa bao giờ thử file ANSI.
  * **Tiêu đề hỏng**: byte thật của file thật là
    `b'STT,M\\xe3 Gateway,SN,M\\xe3 qu?n l\\xfd GDO,\\xd0?a ch? IP'` — có byte `0x3F`
    (`?`) thật, tức dấu mất TRƯỚC khi lưu, không bảng mã nào cứu được. Vì vậy
    phải nhận diện cột theo giá trị, không chỉ theo tên.
  * **UID ngắn**: UID firmware publish luôn 12 hex, nhưng danh mục nhà máy có
    dòng chỉ còn 9-10 hex (mất nibble 0 đầu). Không zero-pad là gateway đó không
    bao giờ khớp bản tin MQTT.
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

import pytest

TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from core.config import GatewayDevice  # noqa: E402
from core.device_import import (  # noqa: E402
    ENCODING_CANDIDATES,
    ImportError_,
    build_preview,
    decode_csv_bytes,
    detect_columns,
    has_non_ascii,
    read_csv_tolerant,
    strip_accents,
    to_ascii_text,
)

#: Tiêu đề đúng như file thi công thật (đã mất dấu thành byte `?`).
REPORT_HEADER = 'STT,Mã Gateway,SN,Mã qu?n lý GDO,Ð?a ch? IP'
REPORT_ROWS = [
    '1,GW-90,MGWI-V1-26070034,GDO-EMS-1F-GW90,192.84.91.10',
    '2,GW-91,MGWI-V1-26070004,GDO-EMS-1F-GW91,192.84.91.11',
]

#: Tiêu đề dùng riêng cho test bảng mã: thay `Ð` (U+00D0) bằng `D` ASCII.
#:
#: Vì sao: cp1258 **không mã hoá được** U+00D0 — ô 0xD0 của nó là `Đ` (U+0110).
#: Ghi file test bằng cp1258 mà chứa `Ð` sẽ ném `UnicodeEncodeError` NGAY LÚC GHI,
#: tức test hỏng vì lý do không liên quan tới tính năng đang kiểm.
ENC_SAFE_HEADER = 'STT,Mã Gateway,SN,Mã qu?n lý GDO,D?a ch? IP'

#: Danh mục giả: `{UID → series}` — giống `load_gateway_catalog()` trả về.
#: Cố ý có UID 9 hex và 10 hex để khoá lại hành vi zero-pad.
FAKE_CATALOG = {
    "A006199E139C": "MGWI-V1-26070034",
    "F9199E139C": "MGWI-V1-26070004",     # 10 hex — mất 2 nibble 0 đầu
    "7199E139C": "MGWI-V1-26070023",      # 9 hex — mất 3 nibble 0
}


def write_bytes(path: Path, text: str, encoding: str) -> Path:
    path.write_bytes(text.encode(encoding))
    return path


def write_report(path: Path, encoding: str = "cp1252", header: str = REPORT_HEADER,
                 rows=None) -> Path:
    body = "\r\n".join([header] + list(rows if rows is not None else REPORT_ROWS))
    return write_bytes(path, body + "\r\n", encoding)


# ---------------------------------------------------------------------------
# Bảng mã
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "cp1252"])
def test_doc_duoc_moi_bang_ma(tmp_path, encoding):
    """Cùng nội dung ở các bảng mã ⇒ cùng kết quả phân giải, chữ Việt còn nguyên.

    KHÔNG tham số hoá `cp1258`: byte `0xE3` của bộ ký tự này là `ă` (không phải
    `ã`), nên nội dung tiếng Việt viết theo cp1258 **không thể** mã hoá được
    (`UnicodeEncodeError` ngay lúc ghi file test). cp1258 cũng không nằm trong
    danh sách đọc — xem `test_cp1258_khong_the_phan_biet_voi_cp1252`.
    """
    path = write_report(tmp_path / "report.csv", encoding, header=ENC_SAFE_HEADER)
    header, rows, detected, _delim = read_csv_tolerant(path)
    assert detected in ENCODING_CANDIDATES
    assert detected != "latin-1", "không được rơi xuống chốt hạ latin-1"
    assert len(rows) == 2
    assert "SN" in header
    # Chữ Việt phải sống sót (đây mới là điều quan trọng, không phải tên bảng mã).
    assert header[1] == "Mã Gateway", header


def test_cp1252_duoc_chon_thay_vi_cp1258_de_khong_hong_dau(tmp_path):
    """Khoá lại lựa chọn bảng mã ANSI: file cp1252 PHẢI ra `Mã`/`lý`, không phải `Mă`/`lư`.

    Đây là ca dễ hỏng nhất và hỏng ÂM THẦM: cp1258 cũng giải mã được file cp1252
    (byte `0xE3` hợp lệ ở cả hai) nên nếu chọn cp1258, tool **không báo lỗi** mà
    lặng lẽ đổi `Mã` thành `Mă`. Tên gateway nhập vào vì thế sai dấu.
    """
    path = write_report(tmp_path / "report.csv", encoding="cp1252")
    header, _rows, detected, _delim = read_csv_tolerant(path)
    assert detected == "cp1252"
    assert header[1] == "Mã Gateway", header


def test_file_utf16_doc_duoc_khong_thanh_rac(tmp_path):
    """Excel *Save As → Unicode Text* xuất UTF-16 — phải đọc ra CSV bình thường.

    Ca này từng hỏng ÂM THẦM: UTF-16 rơi xuống cp1252 (byte `0x00` hợp lệ với
    cp1252) nên **không báo lỗi**, chỉ ra văn bản đầy `\\x00`, tiêu đề vỡ vụn và
    mọi dòng báo "Không xác định được Gateway UID".
    """
    path = tmp_path / "u16.csv"
    path.write_bytes("ten,uid,ip\r\nA,50F9199E139C,192.84.91.10\r\n".encode("utf-16"))
    header, rows, detected, _delim = read_csv_tolerant(path)
    assert detected.startswith("utf-16")
    # BOM KHÔNG được dính vào ô tiêu đề đầu tiên.
    assert header == ["ten", "uid", "ip"], header
    assert rows == [["A", "50F9199E139C", "192.84.91.10"]], rows

    preview = build_preview(path, [], catalog=FAKE_CATALOG)
    assert preview.count("new") == 1, [(r.uid, r.reason) for r in preview.rows]
    assert any("UTF-16" in w for w in preview.warnings), preview.warnings


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_file_utf16_mat_bom_van_doc_duoc(tmp_path, encoding):
    """UTF-16 bị mất BOM (qua git/công cụ khác) vẫn phải nhận ra, nhờ mẫu byte NUL."""
    path = tmp_path / "nobom.csv"
    path.write_bytes("ten,uid,ip\r\nA,50F9199E139C,192.84.91.10\r\n".encode(encoding))
    header, _rows, detected, _delim = read_csv_tolerant(path)
    assert detected == encoding, detected
    assert header == ["ten", "uid", "ip"], header


def test_cp1258_khong_the_phan_biet_voi_cp1252():
    """Khoá lại sự thật đã đo: cp1258 bị cp1252 bao trùm hoàn toàn.

    Không byte nào trong 0x80-0xFF mà cp1258 giải mã được lại bị cp1252 từ chối.
    Hệ quả: **không thể** hỗ trợ cả hai — đặt cp1258 ở đâu cũng vô nghĩa (sau
    cp1252 thì không bao giờ tới, trước thì cướp file cp1252 và làm sai dấu âm
    thầm). Test này đỏ nghĩa là giả định đó đã đổi và phải xem lại thứ tự.
    """
    only_1258 = []
    for byte in range(0x80, 0x100):
        raw = bytes([byte])
        try:
            raw.decode("cp1252")
            ok_1252 = True
        except UnicodeDecodeError:
            ok_1252 = False
        try:
            raw.decode("cp1258")
        except UnicodeDecodeError:
            continue
        if not ok_1252:
            only_1258.append(hex(byte))
    assert only_1258 == [], only_1258

    # Hệ quả trực tiếp: file cp1258 chỉ chứa byte "chung" sẽ bị nhận là cp1252.
    raw = "Mă".encode("cp1258")           # `ă` — chỉ cp1258 mã hoá được
    text, encoding = decode_csv_bytes(raw)
    assert encoding == "cp1252"                # bị cp1252 nhận trước
    assert text == "Mã"                   # `ă` biến thành `ã`


def test_file_that_la_cp1252_khong_phai_utf8():
    """Khoá lại sự thật: file thi công thật là cp1252, utf-8-sig ném lỗi.

    Nếu một ngày file thật đổi sang UTF-8, test này sẽ đỏ và nhắc cập nhật lại
    tài liệu — đó là mục đích (chống trôi nhận định về dữ liệu đầu vào).
    """
    raw = REPORT_HEADER.encode("cp1252")
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8-sig")
    assert b"\xe3" in raw


def test_latin1_khong_bao_gio_nem_loi():
    """`latin-1` là chốt hạ: bytes không bảng mã nào khác đọc được vẫn phải ra chuỗi.

    Byte `0x81` được chọn có chủ đích: nó **không định nghĩa** ở CẢ cp1252 lẫn
    cp1258, nên mọi ứng viên trước đều thất bại và chỉ `latin-1` đỡ được. (Nếu
    dùng byte như `0x80` thì cp1252 đọc được — nó là dấu `€` — và ta sẽ vô tình
    kiểm tra cp1252 chứ không phải chốt hạ.)
    """
    text, encoding = decode_csv_bytes(b"\xff\x81\xfe")
    assert encoding == "latin-1"
    assert isinstance(text, str)


# ---------------------------------------------------------------------------
# Dò cột
# ---------------------------------------------------------------------------
def test_bo_dau_tieng_viet():
    assert strip_accents("Mã Gateway") == "ma gateway"
    assert strip_accents("Địa chỉ IP") == "dia chi ip"
    # Tiêu đề hỏng kiểu cp1252: `Ð` phải quy về `d`.
    assert strip_accents("Ð?a ch? IP") == "d a ch ip"


def test_do_duoc_cot_ip_theo_gia_tri_khi_tieu_de_hong():
    """Tiêu đề mất dấu (`Ð?a ch? IP`) vẫn phải nhận ra cột IP nhờ giá trị."""
    header = REPORT_HEADER.split(",")
    rows = [r.split(",") for r in REPORT_ROWS]
    columns = detect_columns(header, rows)
    assert columns.get("ip") == 4, columns


def test_do_duoc_cot_ten_trong_tieu_de_hong():
    """`Mã qu?n lý GDO` mất dấu nhưng còn chữ `gdo` ⇒ khớp một phần."""
    header = REPORT_HEADER.split(",")
    rows = [r.split(",") for r in REPORT_ROWS]
    columns = detect_columns(header, rows)
    assert columns.get("name") == 3, columns


def test_khong_chon_cot_stt_lam_ten():
    """Cột toàn số nguyên (STT) không được chọn làm cột tên."""
    header = ["STT", "khong ro", "khong ro 2"]
    rows = [["1", "abc", "xyz"], ["2", "def", "uvw"]]
    columns = detect_columns(header, rows)
    assert columns.get("name") != 0


def test_moi_cot_chi_thuoc_mot_vai_tro():
    header = REPORT_HEADER.split(",")
    rows = [r.split(",") for r in REPORT_ROWS]
    columns = detect_columns(header, rows)
    assert len(set(columns.values())) == len(columns), columns


# ---------------------------------------------------------------------------
# Zero-pad UID
# ---------------------------------------------------------------------------
def test_uid_ngan_duoc_zero_pad_ve_12_hex(tmp_path):
    """UID 9/10 hex trong danh mục phải thành 12 hex — nếu không sẽ không khớp MQTT."""
    path = write_report(tmp_path / "r.csv", rows=[
        "1,GW-91,MGWI-V1-26070004,GDO-A,192.84.91.11",   # catalog: F9199E139C (10 hex)
        "2,GW-23,MGWI-V1-26070023,GDO-B,192.84.91.12",   # catalog: 7199E139C (9 hex)
    ])
    preview = build_preview(path, [], catalog=FAKE_CATALOG)
    uids = [row.uid for row in preview.rows]
    assert "00F9199E139C" in uids, uids
    assert "0007199E139C" in uids, uids
    assert all(len(uid) == 12 for uid in uids), uids


def test_uid_tu_file_khong_bi_doi(tmp_path):
    """File đã có cột UID 12 hex thì giữ nguyên, không tra danh mục."""
    path = write_report(
        tmp_path / "r.csv",
        header="ten,uid,ip",
        rows=["GW A,50F9199E139C,192.84.91.10"],
    )
    preview = build_preview(path, [], catalog=FAKE_CATALOG)
    assert preview.rows[0].uid == "50F9199E139C"
    assert preview.rows[0].uid_source == "file"


# ---------------------------------------------------------------------------
# Ghép qua danh mục + trạng thái
# ---------------------------------------------------------------------------
def test_tra_uid_qua_danh_muc_theo_sn(tmp_path):
    path = write_report(tmp_path / "r.csv", rows=[
        "1,GW-90,MGWI-V1-26070034,GDO-EMS-1F-GW90,192.84.91.10",
    ])
    preview = build_preview(path, [], catalog=FAKE_CATALOG)
    assert preview.rows[0].uid == "A006199E139C"
    assert preview.rows[0].uid_source == "catalog"
    assert preview.rows[0].status == "new"


def test_sn_khong_co_trong_danh_muc_bao_loi(tmp_path):
    path = write_report(tmp_path / "r.csv", rows=[
        "1,GW-99,MGWI-V1-99999999,GDO-X,192.84.91.99",
    ])
    preview = build_preview(path, [], catalog=FAKE_CATALOG)
    row = preview.rows[0]
    assert row.status == "error"
    assert "UID" in row.reason


def test_gateway_da_co_thi_bo_qua_khong_ghi_de(tmp_path):
    """Đã có trong devices.json ⇒ status `exists`, KHÔNG có device để ghi đè."""
    path = write_report(tmp_path / "r.csv", rows=[
        "1,GW-90,MGWI-V1-26070034,GDO-MOI,192.84.91.10",
    ])
    existing = [GatewayDevice(uid="A006199E139C", name="TÊN CŨ", eth_ip="10.0.0.1")]
    preview = build_preview(path, existing, catalog=FAKE_CATALOG)
    assert preview.rows[0].status == "exists"
    assert preview.new_devices() == []
    # Tên/IP cũ không bị đụng tới.
    assert existing[0].name == "TÊN CŨ"
    assert existing[0].eth_ip == "10.0.0.1"


def test_khop_gateway_da_co_qua_bien_the_11_hex(tmp_path):
    """devices.json lưu 11 hex vẫn phải nhận ra gateway 12 hex (cùng một gateway)."""
    path = write_report(tmp_path / "r.csv", rows=[
        "1,GW-91,MGWI-V1-26070004,GDO-A,192.84.91.11",
    ])
    existing = [GatewayDevice(uid="F9199E139C", name="cũ", eth_ip="10.0.0.1")]
    preview = build_preview(path, existing, catalog=FAKE_CATALOG)
    assert preview.rows[0].status == "exists"


# ---------------------------------------------------------------------------
# Ca biên
# ---------------------------------------------------------------------------
def test_file_khong_ton_tai():
    with pytest.raises(ImportError_):
        read_csv_tolerant(Path("khong/ton/tai.csv"))


def test_file_rong(tmp_path):
    path = tmp_path / "rong.csv"
    path.write_bytes(b"")
    with pytest.raises(ImportError_):
        read_csv_tolerant(path)


def test_chi_co_header(tmp_path):
    path = write_report(tmp_path / "h.csv", rows=[])
    header, rows, _enc, _delim = read_csv_tolerant(path)
    assert header and rows == []


def test_dau_cham_phay_kiem(tmp_path):
    """Excel ở locale Việt Nam đôi khi ghi `;`."""
    path = write_bytes(
        tmp_path / "semi.csv",
        "ten;uid;ip\r\nGW A;50F9199E139C;192.84.91.10\r\n",
        "utf-8",
    )
    header, rows, _enc, delimiter = read_csv_tolerant(path)
    assert delimiter == ";"
    assert rows[0][1] == "50F9199E139C"


def test_bo_qua_dong_trong_va_dong_sep(tmp_path):
    path = write_bytes(
        tmp_path / "x.csv",
        "sep=,\r\nten,uid,ip\r\n\r\nGW A,50F9199E139C,192.84.91.10\r\n\r\n",
        "utf-8",
    )
    header, rows, _enc, _delim = read_csv_tolerant(path)
    assert header[0] == "ten"
    assert len(rows) == 1


def test_ip_sai_dinh_dang_bao_loi(tmp_path):
    path = write_report(tmp_path / "r.csv", rows=[
        "1,GW-90,MGWI-V1-26070034,GDO-A,999.1.1.1",
    ])
    preview = build_preview(path, [], catalog=FAKE_CATALOG)
    row = preview.rows[0]
    assert row.status == "error"
    assert "IP" in row.reason


def test_uid_trung_trong_cung_file(tmp_path):
    path = write_report(tmp_path / "r.csv", rows=[
        "1,GW-90,MGWI-V1-26070034,GDO-A,192.84.91.10",
        "2,GW-90,MGWI-V1-26070034,GDO-B,192.84.91.11",
    ])
    preview = build_preview(path, [], catalog=FAKE_CATALOG)
    assert preview.rows[0].status == "new"
    assert preview.rows[1].status == "error"
    assert "Trung" in preview.rows[1].reason


def test_thieu_cot_uid_va_khong_co_danh_muc_thi_canh_bao(tmp_path):
    path = write_report(tmp_path / "r.csv", rows=[
        "1,GW-90,MGWI-V1-26070034,GDO-A,192.84.91.10",
    ])
    preview = build_preview(path, [], catalog={})
    assert preview.rows[0].status == "error"
    assert any("danh muc" in w for w in preview.warnings)


def test_ten_trong_van_nhap_duoc(tmp_path):
    """Thiếu cột tên không được làm hỏng cả dòng — UID và IP vẫn dùng được."""
    path = write_bytes(
        tmp_path / "noname.csv",
        "uid,ip\r\n50F9199E139C,192.84.91.10\r\n",
        "utf-8",
    )
    preview = build_preview(path, [], catalog={})
    row = preview.rows[0]
    assert row.status == "new"
    assert row.uid == "50F9199E139C"
    assert row.name == ""


def test_ghi_chu_co_ten_file_nguon(tmp_path):
    path = write_report(tmp_path / "r.csv", rows=[
        "1,GW-90,MGWI-V1-26070034,GDO-A,192.84.91.10",
    ])
    preview = build_preview(path, [], catalog=FAKE_CATALOG)
    assert "r.csv" in preview.rows[0].device.notes


def test_canh_bao_khi_khong_phai_utf8(tmp_path):
    """File ANSI ⇒ có cảnh báo nhắc lưu lại dạng UTF-8 cho lần sau."""
    path = write_report(tmp_path / "r.csv", encoding="cp1252")
    preview = build_preview(path, [], catalog=FAKE_CATALOG)
    assert any("khong phai UTF-8" in w for w in preview.warnings), preview.warnings


# ---------------------------------------------------------------------------
# File thi công thật (nếu có trong repo)
# ---------------------------------------------------------------------------
REAL_REPORT = TOOL_ROOT / "docs" / "REPORT_Thi_Cong_Mang_Gateway_IoT_2026-10-01_v2_HUY(Gateway_List).csv"


@pytest.mark.skipif(not REAL_REPORT.exists(), reason="không có file report thật")
def test_file_thi_cong_that():
    """Khoá lại kết quả đo trên file thi công thật (đọc lại khi file đổi).

    File thật có 40 dòng. Dòng 38 (`GW-126`) **để trống cả cột `SN` lẫn cột
    `Ð?a ch? IP`** vì gateway đó chưa thi công — không có đường nào ra UID nên
    dòng đó phải báo lỗi, và lý do phải nêu đúng là không xác định được UID.
    39 dòng còn lại đều phân giải được, UID 12 hex và không trùng nhau.

    **Không** khoá số `mới`/`đã có`: hai số đó phụ thuộc `config/devices.json`,
    mà file ấy đổi mỗi lần người dùng nhập thiết bị thật. Test khoá vào đó sẽ
    vỡ oan sau vài lần dùng tool (đã từng xảy ra). Ở đây chỉ khẳng định quan hệ
    `mới + đã có = số dòng phân giải được` — đúng với mọi trạng thái cấu hình.
    """
    import json

    devices_path = TOOL_ROOT / "config" / "devices.json"
    existing = []
    if devices_path.exists():
        existing = [
            GatewayDevice(**{k: v for k, v in item.items()
                             if k in ("uid", "name", "eth_ip", "enabled",
                                      "ping_enabled", "notes", "id")})
            for item in json.loads(devices_path.read_text(encoding="utf-8"))
        ]

    preview = build_preview(REAL_REPORT, existing)

    assert len(preview.rows) == 40

    # Dòng để trống SN + IP: đúng MỘT dòng, và là dòng GW-126.
    errors = [r for r in preview.rows if r.status == "error"]
    assert len(errors) == 1, [(r.line_no, r.reason) for r in errors]
    assert errors[0].line_no == 38
    assert "Khong xac dinh duoc Gateway UID" in errors[0].reason, errors[0].reason

    resolved = [r for r in preview.rows if r.status != "error"]
    assert len(resolved) == 39
    assert all(len(r.uid) == 12 for r in resolved), "mọi UID phải là 12 hex"
    assert len({r.uid for r in resolved}) == 39, "không được trùng UID"
    assert preview.count("new") + preview.count("exists") == 39


# ---------------------------------------------------------------------------
# Bỏ dấu chữ lấy từ CSV (`to_ascii_text`)
#
# Vì sao bỏ dấu chứ không chỉ sửa bảng mã: chữ có dấu đi qua bảng mã ANSI
# (cp1252) KHÔNG ném lỗi mà **mất chữ** — `Mã` ghi ra cp1252 thành `M?` vì
# `ã` không có trong bảng mã đó. Chữ đã bỏ dấu là ASCII nên bảng mã nào cũng
# đọc đúng. Đo được: `"Mã quản lý GDO"` → ascii `"Ma quan ly GDO"`,
# cp1252 `"Mã qu?n lý GDO"`.
# ---------------------------------------------------------------------------
def test_to_ascii_text_giu_hoa_thuong():
    """Phải GIỮ hoa/thường (khác `strip_accents`) — đây là lý do có hàm riêng."""
    assert to_ascii_text("Mã quản lý GDO") == "Ma quan ly GDO"


def test_to_ascii_text_giu_dau_cau():
    """Dấu gạch nối/underscore phải còn nguyên: nó là một phần tên gateway."""
    assert to_ascii_text("GDO-EMS-1F-GW90") == "GDO-EMS-1F-GW90"


def test_to_ascii_text_xu_ly_d_gach_ngang():
    """`đ` NFD không tách được nên phải thay tay; cả hoa lẫn thường."""
    assert to_ascii_text("Đường dẫn") == "Duong dan"
    assert to_ascii_text("đèn") == "den"


def test_to_ascii_text_voi_none_va_rong():
    """Không bao giờ ném lỗi — đầu vào rỗng/None trả chuỗi rỗng."""
    assert to_ascii_text(None) == ""
    assert to_ascii_text("") == ""


def test_to_ascii_text_ket_qua_thuan_ascii():
    """Chốt hạ của cả tính năng: đầu ra phải mã hoá được bằng MỌI bảng mã."""
    ket_qua = to_ascii_text("Mã quản lý — Cổng tầng 2 (Đông)")
    ket_qua.encode("ascii")          # ném UnicodeEncodeError nếu còn dấu
    assert ket_qua == "Ma quan ly Cong tang 2 (Dong)"


def test_has_non_ascii():
    assert has_non_ascii("Mã") is True
    assert has_non_ascii("Ma") is False
    assert has_non_ascii("") is False
    assert has_non_ascii(None) is False


def test_ten_lay_tu_csv_duoc_bo_dau(tmp_path):
    """Tên gợi nhớ trong file có dấu ⇒ tên lưu vào devices.json phải không dấu.

    Ghi file bằng **utf-8** chứ không phải cp1252: cp1252 KHÔNG mã hoá được `ạ`,
    `ơ`, `Đ` nên `str.encode("cp1252")` ném `UnicodeEncodeError` ngay lúc ghi, và
    test sẽ hỏng vì lý do không liên quan tới tính năng đang kiểm.
    """
    path = write_bytes(
        tmp_path / "r.csv",
        "uid,ip,ten\r\n50F9199E139C,192.84.91.10,Trạm Bơm Đông\r\n",
        "utf-8",
    )
    preview = build_preview(path, [], catalog={})
    row = preview.rows[0]
    assert row.status == "new", row.reason
    assert row.name == "Tram Bom Dong"
    assert to_ascii_text(row.name) == row.name, "phải idempotent"
    row.device.name.encode("ascii")


# ---------------------------------------------------------------------------
# Cột Ghi chú
# ---------------------------------------------------------------------------
NOTES_HEADER = 'uid,ip,ten,ghi chú'
NOTES_ROWS = [
    '50F9199E139C,192.84.91.10,GW-90,Tầng 2 khu A',
    '50F9199E139D,192.84.91.11,GW-91,Tầng 3 khu B',
]


def test_cot_ghi_chu_duoc_do_va_gan_rieng_tung_dong(tmp_path):
    """Mỗi dòng một ghi chú riêng, KHÔNG dùng chung một chuỗi cho cả lô."""
    path = write_bytes(
        tmp_path / "notes.csv",
        "\r\n".join([NOTES_HEADER] + NOTES_ROWS) + "\r\n",
        "utf-8",
    )
    preview = build_preview(path, [], catalog={})
    assert preview.columns.get("notes") is not None, "phải dò ra cột ghi chú"
    assert [r.notes for r in preview.rows] == ["Tang 2 khu A", "Tang 3 khu B"]


def test_ghi_chu_bo_dau_nhung_giu_dau_cau(tmp_path):
    path = write_bytes(
        tmp_path / "notes.csv",
        NOTES_HEADER + "\r\n50F9199E139C,192.84.91.10,GW,Đông-Bắc (tầng 2)\r\n",
        "utf-8",
    )
    preview = build_preview(path, [], catalog={})
    assert preview.rows[0].notes == "Dong-Bac (tang 2)"


def test_khong_co_cot_ghi_chu_thi_dung_mac_dinh(tmp_path):
    """File không có cột ghi chú ⇒ mặc định “Nhap tu <tên file>”, cũng bỏ dấu."""
    path = write_report(tmp_path / "r.csv", rows=[
        "1,GW-90,MGWI-V1-26070034,GDO-A,192.84.91.10",
    ])
    preview = build_preview(path, [], catalog=FAKE_CATALOG)
    row = preview.rows[0]
    assert row.notes == "Nhap tu r.csv"
    assert row.device.notes == "Nhap tu r.csv"
    row.notes.encode("ascii")


def test_ghi_chu_mac_dinh_bo_dau_ten_file(tmp_path):
    """Tên file có dấu cũng phải bỏ dấu — nó cũng là chữ hiển thị."""
    path = write_report(tmp_path / "Bản thi công.csv", rows=[
        "1,GW-90,MGWI-V1-26070034,GDO-A,192.84.91.10",
    ])
    preview = build_preview(path, [], catalog=FAKE_CATALOG)
    with_diacritics = preview.rows[0].notes
    assert to_ascii_text(with_diacritics) == with_diacritics
    with_diacritics.encode("ascii")


def test_cot_ghi_chu_khong_bi_nhan_nham_lam_cot_ten(tmp_path):
    """`Ghi chú` đứng sau `ten` nên không được cướp vai trò `name`."""
    path = write_bytes(
        tmp_path / "notes.csv",
        "\r\n".join([NOTES_HEADER] + NOTES_ROWS) + "\r\n",
        "utf-8",
    )
    preview = build_preview(path, [], catalog={})
    ten_index = preview.columns["name"]
    ghi_chu_index = preview.columns["notes"]
    assert ten_index != ghi_chu_index
    assert preview.rows[0].name == "GW-90"


def test_ghi_chu_tu_file_co_dau_thi_co_canh_bao(tmp_path):
    """Người dùng phải biết VÌ SAO chữ khác file, không tưởng tool đọc sai."""
    path = write_bytes(
        tmp_path / "notes.csv",
        NOTES_HEADER + "\r\n50F9199E139C,192.84.91.10,GW,Tầng 2\r\n",
        "utf-8",
    )
    preview = build_preview(path, [], catalog={})
    assert any("khong dau" in w for w in preview.warnings), preview.warnings


def test_warning_khong_phat_khi_ghi_chu_thuan_ascii(tmp_path):
    """Ghi chú vốn đã không dấu thì không được cảnh báo (tránh nhiễu)."""
    path = write_bytes(
        tmp_path / "notes.csv",
        NOTES_HEADER + "\r\n50F9199E139C,192.84.91.10,GW,Tang 2\r\n",
        "utf-8",
    )
    preview = build_preview(path, [], catalog={})
    assert not any("khong dau" in w for w in preview.warnings), preview.warnings


def test_nhan_loi_do_module_nay_sinh_da_bo_dau(tmp_path):
    """`reason` và `warnings` do module này sinh ⇒ phải thuần ASCII."""
    path = write_report(tmp_path / "r.csv", rows=[
        "1,GW-90,MGWI-V1-26070034,GDO-A,192.84.91.10",
        "2,GW-90,MGWI-V1-26070034,GDO-B,999.1.1.1",
        "3,GW-90,MGWI-V1-26070034,GDO-C,192.84.91.10",
    ])
    preview = build_preview(path, [], catalog=FAKE_CATALOG)
    for row in preview.rows:
        if row.reason:
            row.reason.encode("ascii")     # ném lỗi nếu còn dấu
    for warning in preview.warnings:
        warning.encode("ascii")
