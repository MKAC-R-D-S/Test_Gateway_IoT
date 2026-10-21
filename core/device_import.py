"""Nhập danh sách gateway cần test từ file CSV "report thi công mạng".

Vì sao cần
----------
Ngoài công trình có hàng chục gateway; nhập tay từng dòng vào tab Thiết bị vừa
cực vừa dễ sai. Bản thi công mạng đã có sẵn đủ thông số (UID/SN, tên, IP) nên
chỉ cần đọc file là đổ được vào `devices.json`.

Vì sao KHÔNG hardcode tên cột
-----------------------------
File report thật (`docs/REPORT_Thi_Cong_Mang_Gateway_IoT_*.csv`) có tiêu đề đã
**mất dấu ngay trong file** — byte thật là
`b'STT,M\\xe3 Gateway,SN,M\\xe3 qu?n l\\xfd GDO,\\xd0?a ch? IP'`, tức có byte `0x3F`
(`?`) thật, không bảng mã nào khôi phục được. Vì vậy không thể chỉ khớp tên cột;
phải dò thêm **theo giá trị** (cột nào toàn IPv4 hợp lệ thì là cột IP).

Vì sao đọc được nhiều bảng mã
-----------------------------
File thật là **cp1252** (byte `0xE3` = `ã`), không phải UTF-8: `utf-8-sig` ném
`UnicodeDecodeError` ngay dòng tiêu đề. Các chỗ khác trong tool đều dùng
`utf-8-sig` vì đó là định dạng tool TỰ GHI RA; còn đây là file người dùng đưa
vào nên phải chấp nhận cả ANSI. `latin-1` là chốt hạ: nó ánh xạ mọi byte nên
không bao giờ ném lỗi (kể cả file nhị phân hỏng vẫn đọc được thành chuỗi rác và
bị loại ở bước kiểm tra dữ liệu, thay vì làm sập giao diện).

Chỉ chọn **một** bảng mã ANSI (cp1252), không phải cả cp1258 — vì hai bảng mã
này giải mã cùng một byte thành hai dấu Việt khác nhau mà không có cách nào phân
biệt. Lý do đầy đủ ở chú thích `ENCODING_CANDIDATES` bên dưới.

Quy ước chung (giống `core/gateway_directory.py`): hàm thuần, **không raise**,
thiếu thông tin thì trả rỗng + ghi lý do vào báo cáo — không bịa giá trị.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import GatewayDevice, is_valid_ipv4, normalize_device_uid
from .gateway_directory import load_gateway_catalog
from .uid import gateway_uid_variants, normalize_hex_uid

#: Bảng mã thử lần lượt khi giải mã file người dùng đưa vào.
#:
#: Thứ tự quan trọng: UTF-8 trước (chuẩn, phân biệt hoa/thường rõ ràng), rồi mới
#: tới ANSI. `latin-1` đứng cuối vì nó "thành công" với MỌI byte — nếu đặt trước
#: thì file UTF-8 sẽ bị giải mã sai thành mojibake.
#:
#: **Vì sao KHÔNG có cp1258** (dù tên file Excel hay ghi là "ANSI"): đã đo trên
#: toàn bộ dải byte 0x80-0xFF — **không có byte nào cp1258 giải mã được mà cp1252
#: từ chối**. Nghĩa là cp1258 bị cp1252 "bao trùm" hoàn toàn: đặt nó sau cp1252
#: thì không bao giờ chạy tới, còn đặt trước thì nó cướp file cp1252 và làm sai
#: dấu một cách âm thầm (`0xE3` → `ă` thay vì `ã`, `0xFD` → `ư` thay vì `ý`).
#: Không thuật toán nào phân biệt được hai bảng mã này — chỉ có thể chọn một.
#: File thi công thật (`docs/REPORT_Thi_Cong_Mang_Gateway_IoT_*.csv`) qua đo là
#: **cp1252** (đọc ra đúng `Mã`/`lý`), nên chọn cp1252.
#:
#: ⚠ Nếu gặp file do Excel lưu bằng **cp1258** mà dấu bị sai (`Mă` thay vì `Mã`):
#: đổi `"cp1252"` bên dưới thành `"cp1258"`. Không thể hỗ trợ cả hai cùng lúc.
ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

#: Dấu phân cách thử khi `csv.Sniffer` không đoán được.
DELIMITER_CANDIDATES = (",", ";", "\t", "|")

#: Tên cột nhận ra được theo tiêu đề (đã bỏ dấu + hạ chữ thường).
#:
#: THỨ TỰ QUAN TRỌNG: hàm dò duyệt theo đúng thứ tự này và lấy cột khớp ĐẦU TIÊN,
#: nên khoá đứng trước được ưu tiên hơn. Với cột TÊN, các khoá mô tả vị trí
#: (`ma quan ly gdo`, `gdo`) đứng TRƯỚC `name`/`label`/`model` — bản thi công mạng
#: có nhiều cột chuỗi tự do, người dùng muốn tên gợi nhớ là mã GDO (mô tả khu/tầng).
#: Riêng `ma gateway` (`GW-90`, nhãn thứ tự thi công) đã bị hạ xuống
#: `_PARTIAL_ONLY_KEYS` — xem chú thích ở đó.
_HEADER_KEYS: Dict[str, Tuple[str, ...]] = {
    "uid": ("uuid", "uid", "gateway uid", "gateway uuid", "gateway id", "mac",
            "device uuid", "dia chi mac"),
    "serial": ("sn", "serial", "serial number", "so seri", "so serial", "series"),
    "ip": ("dia chi ip", "ethernet ip", "eth ip", "ip address",
           "dia chi ethernet", "ip"),
    "name": ("ten goi nho", "ten gateway", "gateway name", "ten",
             "ma quan ly gdo", "gdo", "ma quan ly", "vi tri",
             "name", "label", "model"),
    # Đứng CUỐI vì đây là vai trò ít quan trọng nhất: nếu để lẫn lên trên, khoá
    # `mo ta`/`description` có thể cướp mất cột mà `name` cần.
    "notes": ("ghi chu", "note", "notes", "remark", "comment",
              "mo ta", "description"),
}

#: Khoá **chỉ** dùng ở tầng khớp MỘT PHẦN, không dùng ở tầng khớp ĐÚNG.
#:
#: Vì sao cần: `Mã Gateway` (`GW-90`) là **nhãn thứ tự thi công**, không phải vị
#: trí lắp đặt. Trong bản thi công thật, cột này còn nguyên dấu nên khớp ĐÚNG,
#: trong khi `Mã qu?n lý GDO` (thứ người dùng thực sự muốn làm tên) bị hỏng dấu
#: nên chỉ khớp MỘT PHẦN. Vì tầng khớp đúng chạy TRƯỚC, `ma gateway` sẽ thắng oan
#: và tên hiện ra là `GW-90` thay vì `GDO-…` — đảo thứ tự khoá trong
#: `_HEADER_KEYS` KHÔNG cứu được (thứ tự chỉ có tác dụng giữa các khoá CÙNG khớp).
#: Nên hạ `ma gateway` xuống chỉ còn là phương án cuối ở tầng khớp một phần.
_PARTIAL_ONLY_KEYS: Dict[str, Tuple[str, ...]] = {
    "name": ("ma gateway",),
}

#: Chuỗi hex 6-16 ký tự = ứng viên gateway UID.
_HEX_UID_RE = re.compile(r"^[0-9A-Fa-f]{6,16}$")

#: Tỉ lệ tối thiểu để một cột được coi là "cột IP" khi dò theo giá trị.
_VALUE_DETECT_RATIO = 0.6


class ImportError_(Exception):
    """Lỗi không thể phục hồi khi đọc file (không mở được, rỗng, thiếu cột)."""


@dataclass
class ImportPreviewRow:
    """Một dòng sau khi phân giải — đủ để hiển thị ở bảng xem trước."""

    line_no: int                  # số dòng trong file (1-based, tính cả header)
    uid: str = ""
    name: str = ""
    eth_ip: str = ""
    notes: str = ""               # ghi chú: từ cột trong file, hoặc "Nhap tu <file>"
    status: str = "new"           # new | exists | error
    reason: str = ""              # lý do khi status == "error"
    uid_source: str = ""          # file | catalog | ""
    device: Optional[GatewayDevice] = None


@dataclass
class ImportPreview:
    """Kết quả phân giải cả file, CHƯA ghi gì vào `devices.json`."""

    path: str = ""
    encoding: str = ""            # bảng mã thực sự đọc được
    delimiter: str = ","
    header: List[str] = field(default_factory=list)
    columns: Dict[str, int] = field(default_factory=dict)  # vai trò → chỉ số cột
    rows: List[ImportPreviewRow] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for row in self.rows if row.status == status)

    def new_devices(self) -> List[GatewayDevice]:
        """Thiết bị sẽ được THÊM (bỏ qua dòng lỗi và dòng đã có)."""
        return [r.device for r in self.rows
                if r.status == "new" and r.device is not None]


# ---------------------------------------------------------------------------
# Đọc file
# ---------------------------------------------------------------------------
def _detect_utf16_bom(data: bytes) -> str:
    """BOM UTF-16 đầu file → tên codec, hoặc "" nếu không phải.

    Trả `"utf-16"` (KHÔNG phải `utf-16-le`/`be`): codec trần tự nhận byte order từ
    BOM và **bỏ luôn BOM** khi giải mã. Dùng `utf-16-le` sẽ giữ BOM lại thành ký tự
    `\\ufeff` dính vào ô tiêu đề đầu tiên, làm hỏng tên cột.
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    return ""


def _looks_like_utf16_without_bom(data: bytes) -> str:
    """Đoán UTF-16 **không** BOM qua mẫu byte NUL → codec, hoặc "".

    Vì sao cần: Excel luôn ghi BOM cho *Unicode Text*, nhưng BOM có thể đã bị cắt
    (qua git, qua công cụ khác, hoặc do người dùng sửa). Không nhận ra thì file rơi
    xuống `cp1252`, đọc "thành công" nhưng ra văn bản đầy `\\x00` — mọi dòng báo
    "Không xác định được Gateway UID" mà không nói vì sao.

    Cách đoán: ký tự BMP (kể cả tiếng Việt) có byte cao bằng 0, nên ở UTF-16 byte
    `0x00` rơi đều đặn về một phía (chẵn hoặc lẻ). Nếu ≥80% số byte ở một phía là
    `0x00` thì gần như chắc chắn là UTF-16. CSV thật hầu như không chứa `0x00` nên
    không lo nhận nhầm.
    """
    sample = data[:512]
    if len(sample) < 8:
        return ""
    even_nul = sum(1 for i in range(0, len(sample) - 1, 2) if sample[i] == 0)
    odd_nul = sum(1 for i in range(1, len(sample), 2) if sample[i] == 0)
    even_total = len(range(0, len(sample) - 1, 2))
    odd_total = len(range(1, len(sample), 2))
    if even_total and even_nul / even_total >= 0.8:
        return "utf-16-be"
    if odd_total and odd_nul / odd_total >= 0.8:
        return "utf-16-le"
    return ""


def decode_csv_bytes(data: bytes) -> Tuple[str, str]:
    """Giải mã bytes CSV → `(nội dung, tên bảng mã)`. Không bao giờ ném lỗi.

    **UTF-16 phải xử lý TRƯỚC mọi ứng viên khác.** Excel có mục *Save As →
    Unicode Text* xuất ra UTF-16 (mỗi ký tự ASCII kèm byte `\\x00`). Nếu không
    nhận ra, chuỗi đó rơi xuống `cp1252` và **vẫn đọc được** (byte `0x00` hợp
    lệ với cp1252) nhưng thành văn bản đầy `\\x00`: tiêu đề vỡ vụn, mọi dòng báo
    "Không xác định được Gateway UID", kèm cảnh báo **sai** đổ lỗi cho bảng mã.
    Người dùng chỉ thấy "file không nhập được" mà không hiểu vì sao.

    `latin-1` là chốt hạ nên luôn có ít nhất một bảng mã "thành công".
    """
    for guess in (_detect_utf16_bom(data), _looks_like_utf16_without_bom(data)):
        if not guess:
            continue
        try:
            return data.decode(guess), guess
        except UnicodeDecodeError:
            continue                   # BOM/đoán sai → rơi xuống các ứng viên dưới
    for encoding in ENCODING_CANDIDATES:
        try:
            return data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"


def read_csv_tolerant(path: object) -> Tuple[List[str], List[List[str]], str, str]:
    """Đọc file CSV người dùng đưa vào → `(header, rows, encoding, delimiter)`.

    Chịu được: BOM, ANSI (cp1252), dấu phân cách `, ; \\t |`, dòng trống,
    dòng `sep=,` do Excel chèn. Ném `ImportError_` khi không mở được / file rỗng.
    """
    file_path = Path(path)
    try:
        data = file_path.read_bytes()
    except OSError as exc:
        # Chữ do module này sinh thì bỏ dấu; `exc` giữ nguyên vì trong đó có
        # ĐƯỜNG DẪN THẬT của người dùng — bỏ dấu nó là chỉ sai đường dẫn đi.
        raise ImportError_("Khong mo duoc file: %s" % exc) from exc

    if not data.strip():
        raise ImportError_("File rong.")

    text, encoding = decode_csv_bytes(data)
    # Cắt dòng `sep=X` TRƯỚC khi tách cột — xem `_split_sep_line`.
    text, sep_delimiter = _split_sep_line(text)
    delimiter = sep_delimiter or _sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        raw_rows = [row for row in reader]
    except csv.Error as exc:
        raise ImportError_("File CSV khong hop le: %s" % exc) from exc

    # Bỏ dòng rỗng.
    rows = [row for row in raw_rows if any(str(cell).strip() for cell in row)]
    if not rows:
        raise ImportError_("File khong co dong du lieu nao.")

    header = [str(cell).strip() for cell in rows[0]]
    return header, rows[1:], encoding, delimiter


def _split_sep_line(text: str) -> Tuple[str, str]:
    """Tách dòng `sep=X` Excel chèn ở đầu file → `(phần còn lại, X hoặc "")`.

    Vì sao phải cắt TRƯỚC khi gọi `csv.reader`: dòng `sep=,` khi đã tách cột sẽ
    thành `['sep=', '']` — **2 ô**, nên nó lọt qua mọi bộ lọc "dòng chỉ có 1 ô"
    và bị hiểu nhầm là dòng tiêu đề, làm mọi cột lệch đi một dòng. Cắt ở tầng
    văn bản là cách duy nhất chắc chắn, và còn cho luôn dấu phân cách đúng ý
    người lưu file (Excel ghi dòng này chính là để báo dấu phân cách).
    """
    lines = text.splitlines(True)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("sep=") and len(stripped) <= 5:
            found = stripped[4:5]
            # Chỉ nhận dấu phân cách tool hỗ trợ; `sep=` cụt (vd `sep=<TAB>` bị
            # `strip()` ăn mất) trả "" để rơi về `_sniff_delimiter`.
            if found in DELIMITER_CANDIDATES:
                return "".join(lines[index + 1:]), found
            return "".join(lines[index + 1:]), ""
        break
    return text, ""


def _sniff_delimiter(text: str) -> str:
    """Đoán dấu phân cách; fallback là đếm dấu xuất hiện ở dòng đầu."""
    sample = text[:4096]
    try:
        return csv.Sniffer().sniff(sample, delimiters="".join(DELIMITER_CANDIDATES)).delimiter
    except csv.Error:
        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        counts = {d: first_line.count(d) for d in DELIMITER_CANDIDATES}
        best = max(counts, key=lambda d: counts[d])
        return best if counts[best] > 0 else ","


# ---------------------------------------------------------------------------
# Dò cột
# ---------------------------------------------------------------------------
def strip_accents(text: object) -> str:
    """Bỏ dấu tiếng Việt + hạ chữ thường + gộp khoảng trắng, để so tên cột.

    Xử lý riêng `đ`/`Đ` vì NFD không tách được ký tự này. Cũng quy `ð`/`Ð` về `d`
    để nhận ra tiêu đề hỏng kiểu cp1252 (`Ð?a ch? IP`).
    """
    value = str(text or "")
    for src, dst in (("đ", "d"), ("Đ", "D"), ("ð", "d"), ("Ð", "D")):
        value = value.replace(src, dst)
    decomposed = unicodedata.normalize("NFD", value)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_only.lower()).strip()


def to_ascii_text(text: object) -> str:
    """Bỏ dấu tiếng Việt cho **văn bản hiển thị** — giữ nguyên hoa/thường.

    Vì sao cần: chữ có dấu đi qua bảng mã ANSI (cp1252) sẽ **mất chữ** chứ không
    ném lỗi — `Mã quản lý` thành `Mã qu?n lý` vì `ả`/`ầ` không có trong cp1252.
    Chữ đã bỏ dấu thì thuần ASCII nên **mọi bảng mã đều đọc đúng**, kể cả khi
    người dùng mở lại `devices.json` hay báo cáo bằng Excel/Notepad hệ ANSI.

    Khác `strip_accents` ở hai điểm, và đó là lý do phải có hàm riêng:
      - **giữ hoa/thường** (`Mã GDO` → `Ma GDO`, không phải `ma gdo`);
      - **giữ dấu câu** (`GDO-EMS-1F` → `GDO-EMS-1F`, không thành `gdo ems 1f`).

    `strip_accents` dùng cho việc SO TÊN CỘT nên hạ chữ thường + bỏ dấu câu là
    đúng; dùng nó cho tên gợi nhớ sẽ phá dữ liệu người dùng.
    """
    value = str(text or "")
    for src, dst in (("đ", "d"), ("Đ", "D"), ("ð", "d"), ("Ð", "D")):
        value = value.replace(src, dst)
    decomposed = unicodedata.normalize("NFD", value)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    # Dấu câu không thuộc ASCII bị `ignore` ăn mất, có thể để lại khoảng trắng
    # đôi (`"lý — Cổng"` → `"ly  Cong"`); gộp lại cho gọn.
    return re.sub(r"\s+", " ", ascii_only).strip()


def has_non_ascii(text: object) -> bool:
    """True nếu chuỗi có ký tự ngoài ASCII (tức sẽ bị `to_ascii_text` đổi khác)."""
    try:
        str(text or "").encode("ascii")
        return False
    except UnicodeEncodeError:
        return True


def detect_columns(
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    catalog: Optional[Dict[str, str]] = None,
) -> Dict[str, int]:
    """Đoán chỉ số cột cho từng vai trò → `{"uid": i, "name": i, "ip": i, "serial": i,
    "notes": i}`.

    Bốn tầng, mỗi cột chỉ thuộc MỘT vai trò:

      1. **Khớp đúng tên cột** sau khi bỏ dấu (`Mã Gateway` → `ma gateway`).
      2. **Dò theo GIÁ TRỊ** cho `ip` và `uid`: cột có đa số giá trị là IPv4 hợp lệ
         ⇒ cột IP; đa số là hex 6-16 ⇒ cột UID. Đây là tầng bắt buộc vì tiêu đề
         file thi công thật đã hỏng (xem docstring đầu file) — đo trên file thật
         cột IP đạt 100% nên nhận diện chắc chắn.
      3. **Khớp MỘT PHẦN** (substring) cho các vai trò còn thiếu. Cần tầng này vì
         tiêu đề hỏng làm mất dấu: `Mã qu?n lý GDO` → `ma qu n ly gdo`, không còn
         khớp đúng `ma quan ly gdo`, nhưng vẫn chứa `gdo` nên nhận ra được.
      4. **Suy đoán cuối** cho `name`: cột còn trống đầu tiên mà giá trị KHÔNG
         phải số nguyên thuần (để bỏ qua cột thứ tự kiểu `STT`).

    Tầng 3+4 chỉ là **đoán tốt nhất** — ba cột tên trong file thật đều là chuỗi
    tự do nên không có tín hiệu nào bảo đảm. Hộp thoại xem trước cho người dùng
    chọn lại, đó mới là bảo đảm.
    """
    found: Dict[str, int] = {}
    normalized = [strip_accents(col) for col in header]
    used: set = set()

    # --- tầng 1: khớp đúng tên cột
    for role, keys in _HEADER_KEYS.items():
        for key in keys:
            if key in normalized:
                index = normalized.index(key)
                if index not in used:
                    found[role] = index
                    used.add(index)
                    break

    # --- tầng 2: dò theo giá trị
    if "ip" not in found:
        index = _column_by_value(rows, header, lambda v: is_valid_ipv4(v), used)
        if index is not None:
            found["ip"] = index
            used.add(index)
    if "uid" not in found:
        index = _column_by_value(
            rows, header, lambda v: bool(_HEX_UID_RE.match(v)), used
        )
        if index is not None:
            found["uid"] = index
            used.add(index)

    # --- tầng 3: khớp một phần (tiêu đề hỏng mất dấu)
    #
    # Chỉ dùng cho `name`/`ip`/`serial`. Riêng `uid` KHÔNG khớp một phần: khoá
    # `gateway` quá dễ dãi, sẽ nhận nhầm cột `Mã Gateway` (nhãn GW-90, không phải
    # UID) làm cột UID. Cột UID chỉ được nhận khi có tín hiệu thật (`uid`/`uuid`/
    # `mac`) hoặc khi dò theo giá trị hex ở tầng 2.
    #
    # Với mỗi vai trò: thử khoá thường TRƯỚC, chỉ khi không còn gì mới tới
    # `_PARTIAL_ONLY_KEYS` — nhờ vậy `Mã qu?n lý GDO` (`gdo`) thắng `Mã Gateway`.
    for role, keys in _HEADER_KEYS.items():
        if role in found or role == "uid":
            continue
        for key in tuple(keys) + _PARTIAL_ONLY_KEYS.get(role, ()):
            if len(key) < 3:      # khoá quá ngắn dễ khớp bừa
                continue
            for index, text in enumerate(normalized):
                if index not in used and key in text:
                    found[role] = index
                    used.add(index)
                    break
            if role in found:
                break

    # --- tầng 4: suy đoán cột TÊN = cột còn trống đầu tiên không phải số thứ tự
    if "name" not in found:
        for index in range(len(header)):
            if index in used:
                continue
            values = [str(row[index]).strip() for row in rows if index < len(row)]
            if values and all(v.isdigit() for v in values):
                continue          # cột STT
            found["name"] = index
            used.add(index)
            break

    return found


def _column_by_value(
    rows: Sequence[Sequence[str]],
    header: Sequence[str],
    predicate: Any,
    used: Optional[set] = None,
) -> Optional[int]:
    """Chỉ số cột đầu tiên có tỉ lệ giá trị khớp `predicate` ≥ ngưỡng."""
    skip = used or set()
    best_index: Optional[int] = None
    best_ratio = 0.0
    for index in range(len(header)):
        if index in skip:
            continue
        values = [str(row[index]).strip() for row in rows if index < len(row)]
        if not values:
            continue
        ratio = sum(1 for value in values if predicate(value)) / len(values)
        if ratio >= _VALUE_DETECT_RATIO and ratio > best_ratio:
            best_index, best_ratio = index, ratio
    return best_index


# ---------------------------------------------------------------------------
# Dựng thiết bị
# ---------------------------------------------------------------------------
def _cell(row: Sequence[str], index: Optional[int]) -> str:
    if index is None or index < 0 or index >= len(row):
        return ""
    return str(row[index]).strip()


def _has_diacritics_in_column(
    raw_rows: Sequence[Sequence[str]], index: Optional[int]
) -> bool:
    """True nếu cột `index` của file có ít nhất một ô chứa ký tự ngoài ASCII.

    Phải soi Ô GỐC trong file, không soi `ImportPreviewRow`: giá trị trong đó đã
    qua `to_ascii_text()` rồi, mà hàm đó idempotent — so lại thì không bao giờ
    thấy khác, cảnh báo sẽ không bao giờ hiện.
    """
    if index is None or int(index) < 0:
        return False
    return any(has_non_ascii(_cell(row, int(index))) for row in raw_rows)


def _catalog_by_serial(catalog: Dict[str, str]) -> Dict[str, str]:
    """Đảo danh mục thành bảng tra `{SN (in hoa) → UID}` để ghép khi file thiếu UID.

    `load_gateway_catalog()` trả `{UID → tên}`, trong đó tên lấy từ cột `series`
    của danh mục nhà máy (`uuid,model,series`). Bản thi công mạng lại chỉ có cột
    `SN` = chính giá trị `series` đó, nên phải **đảo chiều** mới ghép được.
    """
    inverted: Dict[str, str] = {}
    for uid, name in (catalog or {}).items():
        serial = str(name or "").strip().upper()
        if serial:
            inverted.setdefault(serial, uid)
    return inverted


def build_preview(
    path: object,
    existing_devices: Sequence[GatewayDevice] = (),
    columns: Optional[Dict[str, int]] = None,
    catalog: Optional[Dict[str, str]] = None,
) -> ImportPreview:
    """Đọc file + phân giải từng dòng → `ImportPreview`. **Không ghi gì.**

    `columns` cho phép người dùng ghi đè kết quả dò cột (từ hộp thoại xem trước).
    `catalog` là bảng tra `{SN → UID}`; mặc định lấy từ
    `docs/Danh sach 55 gateway.csv` (xem `core/gateway_directory.py`).
    """
    header, raw_rows, encoding, delimiter = read_csv_tolerant(path)
    if catalog is None:
        catalog = load_gateway_catalog()

    preview = ImportPreview(
        path=str(path), encoding=encoding, delimiter=delimiter, header=list(header)
    )
    # `columns` từ hộp thoại chỉ chứa các vai trò NGƯỜI DÙNG chọn (Tên/IP/UID).
    # Phải hợp nhất với kết quả dò tự động, nếu không sẽ mất vai trò `serial` —
    # mà đó chính là đường tra UID chính của bản thi công mạng (file không có cột
    # UID, phải tra SN → danh mục).
    detected = detect_columns(header, raw_rows)
    if columns is None:
        preview.columns = detected
    else:
        merged = dict(detected)
        for role, index in columns.items():
            if index is None or int(index) < 0:
                merged.pop(role, None)      # người dùng chọn "(không dùng)"
            else:
                merged[role] = int(index)
        preview.columns = merged

    # Cảnh báo khi KHÔNG có đường nào ra UID. Lưu ý: có cột SN vẫn vô ích nếu
    # không có danh mục để tra — khi đó MỌI dòng đều lỗi, nên phải cảnh báo.
    if "uid" not in preview.columns and not catalog:
        preview.warnings.append(
            "File khong co cot UID va khong doc duoc danh muc "
            "`docs/Danh sach 55 gateway.csv` de tra UID theo SN - se khong dong "
            "nao nhap duoc."
        )
    # Chú ý: mọi chuỗi cảnh báo ở đây phải THUẦN ASCII. Ngoặc kép cong “ ” và
    # gạch dài — tuy chỉ là dấu câu nhưng vẫn ngoài ASCII, nên cũng bị bảng mã
    # ANSI ăn mất — đúng cái đang muốn tránh.
    if encoding.startswith("utf-16"):
        preview.warnings.append(
            "File la UTF-16 (Excel 'Unicode Text'). Da doc duoc, nhung hay luu lai "
            "dang 'CSV UTF-8' - Excel chi mo dung dinh dang do."
        )
    elif encoding not in ("utf-8-sig", "utf-8"):
        preview.warnings.append(
            "File dung bang ma %s (khong phai UTF-8). Da doc duoc, nhung nen luu "
            "lai dang 'CSV UTF-8' neu muon mo bang Excel khong loi dau." % encoding
        )
    if "ip" not in preview.columns:
        preview.warnings.append("Khong nhan ra cot dia chi IP - hay chon cot o tren.")

    # Bảng tra theo biến thể UID để 11 hex và 12 hex không bị coi là 2 gateway.
    existing_variants = set()
    for device in existing_devices or ():
        existing_variants |= gateway_uid_variants(getattr(device, "uid", ""))

    seen: Dict[str, int] = {}
    serial_index = _catalog_by_serial(catalog)
    default_notes = to_ascii_text("Nhập từ %s" % Path(str(path)).name)
    notes_from_file = "notes" in preview.columns

    for offset, row in enumerate(raw_rows):
        line_no = offset + 2  # +1 header, +1 vì 1-based
        item = _resolve_row(
            row, preview.columns, serial_index, existing_variants, seen, line_no,
            default_notes,
        )
        preview.rows.append(item)

    # Cảnh báo khi việc bỏ dấu THẬT SỰ làm đổi chữ, để người dùng không tưởng
    # tool đọc sai file. Soi ô GỐC trong file (`_has_diacritics_in_column`), chứ
    # không soi `row.notes` đã bỏ dấu — so lại trên đầu ra thì không bao giờ khác.
    if (notes_from_file
            and _has_diacritics_in_column(raw_rows, preview.columns.get("notes"))):
        preview.warnings.append(
            "Co chu co dau trong cot Ghi chu da duoc doi sang khong dau."
        )

    return preview


def _resolve_row(
    row: Sequence[str],
    columns: Dict[str, int],
    catalog_by_serial: Dict[str, str],
    existing_variants: set,
    seen: Dict[str, int],
    line_no: int,
    default_notes: str = "",
) -> ImportPreviewRow:
    """Phân giải MỘT dòng thành thiết bị (hoặc ghi lý do lỗi).

    Mọi chữ lấy từ file (tên gợi nhớ, ghi chú) đều đi qua `to_ascii_text()`: chữ có
    dấu khi ghi ra bảng mã ANSI sẽ mất chữ (`Mã` → `M?`), còn chữ không dấu là
    ASCII nên bảng mã nào cũng đọc đúng.
    """
    item = ImportPreviewRow(line_no=line_no)

    raw_uid = _cell(row, columns.get("uid"))
    serial = _cell(row, columns.get("serial"))
    # Tên hiển thị: bỏ dấu để không phụ thuộc bảng mã của người mở file sau này.
    name = to_ascii_text(_cell(row, columns.get("name")))
    eth_ip = _cell(row, columns.get("ip"))
    notes = to_ascii_text(_cell(row, columns.get("notes"))) or default_notes
    item.notes = notes

    # --- UID: ưu tiên cột UID trong file, sau đó tra danh mục theo SN
    uid = normalize_hex_uid(raw_uid)
    if uid:
        item.uid_source = "file"
    elif serial:
        catalog_uid = catalog_by_serial.get(serial.upper()) or catalog_by_serial.get(serial)
        uid = normalize_hex_uid(catalog_uid)
        if uid:
            item.uid_source = "catalog"
    if not uid:
        item.status = "error"
        item.reason = ("Khong xac dinh duoc Gateway UID"
                       + (" (SN %s khong co trong danh muc)" % serial if serial else ""))
        item.name, item.eth_ip = name, eth_ip
        return item

    # Zero-pad 12 hex: firmware publish luôn 12 hex, danh mục có thể thiếu nibble 0.
    uid = uid.rjust(12, "0")
    item.uid, item.name, item.eth_ip = uid, name, eth_ip

    # Ba thông báo dưới đây do MODULE NÀY sinh nên bỏ dấu được. Riêng lỗi từ
    # `device.validate()` (cuối hàm) GIỮ NGUYÊN dấu — hàm đó dùng chung với hộp
    # thoại thêm/sửa tay, không thuộc phạm vi "bỏ dấu phần nhập CSV".
    if not is_valid_ipv4(eth_ip):
        item.status = "error"
        item.reason = "Dia chi IP khong hop le: %r" % eth_ip
        return item

    if uid in seen:
        item.status = "error"
        item.reason = "Trung voi dong %d trong cung file" % seen[uid]
        return item
    seen[uid] = line_no

    if existing_variants & gateway_uid_variants(uid):
        item.status = "exists"
        item.reason = "Da co trong danh sach - giu nguyen"
        return item

    device = GatewayDevice(
        uid=normalize_device_uid(uid),
        name=name,
        eth_ip=eth_ip,
        enabled=True,
        ping_enabled=True,
        notes=notes,
    )
    errors = device.validate()
    if errors:
        item.status = "error"
        item.reason = "; ".join(errors)
        return item

    item.status = "new"
    item.device = device
    return item
