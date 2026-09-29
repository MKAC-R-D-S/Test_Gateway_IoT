"""Quản lý phiên chạy (run) — thư mục log, metadata, đường dẫn file.

Cấu trúc thư mục một lần chạy (nằm trong `data/runs/`):

```
data/runs/20260918_142530_4F9199E139C/
  meta.json                     # metadata phiên (bắt đầu/kết thúc, broker, tham số)
  mqtt/
    mqtt_data_20260918.csv
    mqtt_missing_20260918.csv
    mqtt_anomalies_20260918.csv
    mqtt_errors_20260918.csv
    mqtt_gateway_log_20260918.csv
  ping/
    ping_log_20260918.csv
  reports/
    gateway_downtime.csv
    run_summary.csv
    node_summary.csv
    ping_summary.csv
    correlation_summary.csv
  tool.log
```

Tên thư mục: `<YYYYMMDD_HHMMSS>_<gateway_uid_đầu_tiên>`. Nếu chạy nhiều gateway,
vẫn đặt theo gateway đầu tiên trong danh sách đang bật — giúp nhìn tên là biết
phiên nào (giống cách gateway_message_counter đặt `logs/<ts>_<uid>`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.uid import gateway_name_index, gateway_uid_variants, normalize_hex_uid

RUN_DIR_RE = re.compile(r"^(\d{8}_\d{6})_(.+)$")

#: Khoảng trắng/điều khiển trong TÊN PHIÊN và TÊN FILE xuất — thay bằng gạch dưới.
_UNSAFE_NAME_RE = re.compile(r"[\s\x00-\x1f\x7f]+")
#: Tên phiên tối đa bao nhiêu ký tự (đủ để người dùng đặt câu mô tả ngắn).
SESSION_LABEL_MAX = 120

STATUS_RUNNING = "RUNNING"
STATUS_STOPPED = "STOPPED"
STATUS_CRASHED = "CRASHED"


@dataclass
class RunSession:
    """Metadata + đường dẫn của một phiên chạy."""

    run_dir: Path
    run_id: str = ""
    started_at: str = ""
    ended_at: str = ""
    duration_seconds: Optional[float] = None
    planned_hours: Optional[float] = None
    broker: str = ""
    subscribed_topics: List[str] = field(default_factory=list)
    gateways_monitored: int = 0
    gateway_uids: List[str] = field(default_factory=list)
    #: ẢNH CHỤP {gateway UID → tên gọi nhớ} tại thời điểm BẮT ĐẦU phiên.
    #: Vì sao cần: tab "Lịch sử phiên" phải xem lại được "phiên đó chạy những
    #: gateway nào" đúng như lúc chạy. Nếu tra tên từ `devices.json` hiện tại thì
    #: việc XOÁ gateway hoặc ĐỔI gateway khác trong tab Thiết bị sẽ làm phiên cũ
    #: hiện "(chưa có tên)" — tức sửa hôm nay lại đổi cả dữ liệu lịch sử.
    #: Phiên không có ảnh chụp (meta.json của bản tool cũ) sẽ quay về tra tên
    #: sống trong `devices.json` — xem `ui/history_tab.py`.
    gateway_names: Dict[str, str] = field(default_factory=dict)
    #: TÊN PHIÊN do người dùng đặt (rỗng = dùng `run_id`). Sửa được BẤT CỨ LÚC NÀO
    #: qua tab "Lịch sử phiên" → ghi thẳng vào `meta.json`; không ảnh hưởng tên
    #: thư mục, `run_id` hay dữ liệu CSV nên sửa/xoá gateway hay chạy lại phiên
    #: cũng không làm mất tên đã đặt.
    label: str = ""
    status: str = STATUS_RUNNING
    tool_version: str = ""
    stop_reason: str = ""
    notes: str = ""

    # ------------------------------------------------------------ đường dẫn
    @property
    def meta_path(self) -> Path:
        return self.run_dir / "meta.json"

    @property
    def mqtt_dir(self) -> Path:
        return self.run_dir / "mqtt"

    @property
    def ping_dir(self) -> Path:
        return self.run_dir / "ping"

    @property
    def report_dir(self) -> Path:
        return self.run_dir / "reports"

    @property
    def log_path(self) -> Path:
        return self.run_dir / "tool.log"

    def ensure_dirs(self) -> None:
        for directory in (
            self.run_dir,
            self.mqtt_dir,
            self.ping_dir,
            self.report_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- meta.json
    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["run_dir"] = str(self.run_dir)
        return data

    def save(self) -> Path:
        self.ensure_dirs()
        path = self.meta_path
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return path

    def finish(self, status: str = STATUS_STOPPED, reason: str = "") -> None:
        self.ended_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.status = status
        if reason:
            self.stop_reason = reason
        started = parse_iso(self.started_at)
        ended = parse_iso(self.ended_at)
        if started and ended:
            try:
                self.duration_seconds = round((ended - started).total_seconds(), 3)
            except TypeError:
                self.duration_seconds = None
        self.save()

    def elapsed_seconds(self) -> float:
        started = parse_iso(self.started_at)
        if not started:
            return 0.0
        try:
            return max(0.0, (datetime.now(started.tzinfo) - started).total_seconds())
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def load(cls, run_dir: Path) -> "RunSession":
        run_dir = Path(run_dir)
        path = run_dir / "meta.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, json.JSONDecodeError):
                data = {}
        else:
            data = {}
        data = data if isinstance(data, dict) else {}
        session = cls(run_dir=run_dir)
        session.run_id = str(data.get("run_id", "") or run_dir.name)
        session.started_at = str(data.get("started_at", ""))
        session.ended_at = str(data.get("ended_at", ""))
        duration = data.get("duration_seconds")
        session.duration_seconds = float(duration) if isinstance(duration, (int, float)) else None
        planned = data.get("planned_hours")
        session.planned_hours = float(planned) if isinstance(planned, (int, float)) else None
        session.broker = str(data.get("broker", ""))
        topics = data.get("subscribed_topics")
        session.subscribed_topics = [str(t) for t in topics] if isinstance(topics, list) else []
        session.gateways_monitored = int(data.get("gateways_monitored") or 0)
        uids = data.get("gateway_uids")
        session.gateway_uids = [str(u) for u in uids] if isinstance(uids, list) else []
        names = data.get("gateway_names")
        session.gateway_names = (
            {str(k): str(v) for k, v in names.items()}
            if isinstance(names, dict) else {}
        )
        session.status = str(data.get("status", STATUS_RUNNING))
        session.tool_version = str(data.get("tool_version", ""))
        session.stop_reason = str(data.get("stop_reason", ""))
        session.notes = str(data.get("notes", ""))
        # TÊN PHIÊN người dùng đặt. `meta.json` của bản tool cũ không có khoá này
        # → rỗng, GUI tự hiện `run_id` như trước.
        session.label = str(data.get("label", "") or "")
        if not session.run_id:
            session.run_id = run_dir.name
        return session

    # ------------------------------------------------------------- hiển thị
    def file_count(self) -> int:
        return sum(1 for _ in self.run_dir.rglob("*") if _.is_file())

    def total_bytes(self) -> int:
        total = 0
        for path in self.run_dir.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    def gateways_label(self) -> str:
        if not self.gateway_uids:
            return "(không có)"
        if len(self.gateway_uids) <= 3:
            return ", ".join(self.gateway_uids)
        return "%s +%d" % (", ".join(self.gateway_uids[:3]), len(self.gateway_uids) - 3)

    def display_name(self) -> str:
        """Tên hiển thị của phiên: tên người dùng đặt, nếu không có thì `run_id`."""
        return self.label.strip() or self.run_id


# ---------------------------------------------------------------------------
# Factory / discovery
# ---------------------------------------------------------------------------
def create_run_dir(
    data_dir: Path,
    gateway_uids: Optional[List[str]] = None,
    prefix: str = "",
    run_id: Optional[str] = None,
) -> Path:
    """Tạo thư mục run chưa tồn tại, trả về Path đã tạo."""
    data_dir = Path(data_dir)
    runs_root = data_dir / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    uids = [u for u in (gateway_uids or []) if u]
    suffix = (uids[0] if uids else "no_gateway")
    if prefix:
        suffix = "%s_%s" % (prefix, suffix)
    name = run_id or ("%s_%s" % (stamp, suffix))
    candidate = runs_root / name
    index = 1
    while candidate.exists():
        candidate = runs_root / ("%s_%d" % (name, index))
        index += 1
    candidate.mkdir(parents=True)
    return candidate


def snapshot_gateway_names(devices: object) -> Dict[str, str]:
    """{gateway UID (in hoa) → tên gọi nhớ} để CHỐT vào `meta.json`.

    Nhận danh sách `GatewayDevice` (hoặc bất cứ object nào có `.uid` / `.name`).
    UID giữ đúng cách viết trong `devices.json` (đã chuẩn hoá in hoa) nên khớp
    từng ký tự với `gateway_uids` — cùng nguồn, cùng thời điểm.
    """
    out: Dict[str, str] = {}
    for device in devices or ():
        uid = normalize_hex_uid(getattr(device, "uid", ""))
        if not uid:
            continue
        out[uid] = str(getattr(device, "name", "") or "").strip() or uid
    return out


def normalize_session_label(text: object) -> str:
    """Chuẩn hoá TÊN PHIÊN người dùng nhập.

    Chỉ gọt khoảng trắng và cắt độ dài; **KHÔNG** nhét tên vào tên thư mục nên
    tiếng Việt, khoảng trắng, dấu `/`… đều an toàn (chỉ là dữ liệu trong JSON).
    """
    return " ".join(str(text or "").split())[:SESSION_LABEL_MAX]


def safe_name_slug(text: object) -> str:
    """Chuẩn hoá tên để ghép vào TÊN FILE xuất (bỏ ký tự Windows không cho phép).

    Dùng cho đuôi file CSV xuất từ GUI — khác `normalize_session_label` (tên
    phiên lưu trong meta.json, không cần an toàn cho filesystem).
    """
    slug = _UNSAFE_NAME_RE.sub("_", str(text or "").strip())
    for bad in '\\/:*?"<>|':
        slug = slug.replace(bad, "_")
    slug = slug.strip(". _")
    return slug[:SESSION_LABEL_MAX]


def set_session_label(run_dir: Path, label: object) -> RunSession:
    """Đặt/đổi TÊN PHIÊN của một phiên đã chạy và lưu ngay vào `meta.json`.

    Vì sao sửa trực tiếp trong thư mục phiên: tên phiên là DỮ LIỆU của phiên, phải
    đi theo phiên khi copy/sao lưu thư mục, và không được phụ thuộc file cấu hình
    nào bên ngoài (cùng lý do với ảnh chụp `gateway_names`). Truyền chuỗi rỗng để
    XOÁ tên → giao diện quay lại hiển thị `run_id`.
    """
    session = RunSession.load(Path(run_dir))
    session.label = normalize_session_label(label)
    session.save()
    return session


def session_gateway_names(run_dir: Path) -> Dict[str, str]:
    """Index {mọi cách viết gateway UID → tên} từ ẢNH CHỤP trong `meta.json`.

    Dùng cho tab Lịch sử / Tìm kiếm khi xem lại một phiên cụ thể: tên gateway
    phải là tên LÚC CHẠY, không phụ thuộc việc người dùng đã xoá/đổi gateway
    trong tab Thiết bị sau đó. Phiên của bản tool cũ (không có `gateway_names`)
    trả {} — nơi gọi tự quyết định lấy tên sống từ `devices.json`.
    """
    session = RunSession.load(Path(run_dir))
    return gateway_name_index(session.gateway_names.items())


def fill_session_gateway_names(
    run_dir: Path, mapping: Dict[str, str], overwrite: bool = False
) -> Dict[str, Any]:
    """Bổ sung TÊN GATEWAY cho một phiên đã chạy, dựa vào UID.

    Vì sao cần: UID ↔ tên gateway là CỐ ĐỊNH (tra trong `docs/Danh sach 55
    gateway.csv`), còn phiên của bản tool cũ lại không ghi tên vào `meta.json`.
    Hàm này ghép tên vào **ảnh chụp `gateway_names`** của phiên để tab Lịch sử
    hiện đúng tên mà KHÔNG phải sửa tay JSON.

    Quy tắc an toàn:
      - Mặc định (`overwrite=False`) **chỉ điền khi phiên đang THIẾU tên** —
        tên người dùng đã tự đặt sẽ được giữ nguyên.
      - Chỉ nhận đúng UID có trong phiên: tên tra được cho gateway KHÔNG thuộc
        `gateway_uids` của phiên sẽ bị bỏ qua (không làm phiên "mọc thêm" gateway).
      - Khoá lưu vào `gateway_names` giữ đúng cách viết trong `gateway_uids` của
        phiên (cùng nguồn, cùng thời điểm), y như `snapshot_gateway_names`.

    Trả về: `{"added", "skipped", "unmatched", "changed"}` (số liệu để GUI báo
    lại cho người dùng). Không ghi file khi `changed == 0`.
    """
    run_dir = Path(run_dir)
    session = RunSession.load(run_dir)
    names = dict(session.gateway_names or {})
    # Index ảnh chụp hiện có để biết UID nào ĐÃ có tên (theo mọi biến thể).
    existing = gateway_name_index(names.items())
    mapping = mapping or {}
    added: List[str] = []
    skipped = 0
    unmatched = 0
    for uid in session.gateway_uids:
        key = normalize_hex_uid(uid)
        if not key:
            continue
        if any(existing.get(variant) for variant in gateway_uid_variants(key)):
            skipped += 1
            if not overwrite:
                continue
        # Tra tên của CHÍNH UID này trong bảng tra (đã khớp biến thể sẵn).
        name = ""
        for variant in gateway_uid_variants(key):
            name = str(mapping.get(variant, "") or "").strip()
            if name:
                break
        if not name:
            unmatched += 1
            continue
        names[key] = name
        for variant in gateway_uid_variants(key):
            existing.setdefault(variant, name)
        added.append(key)
    if not added:
        return {"added": 0, "skipped": skipped, "unmatched": unmatched, "changed": 0}
    session.gateway_names = names
    session.save()
    return {
        "added": len(added),
        "skipped": skipped,
        "unmatched": unmatched,
        "changed": len(added),
    }


def create_session(
    data_dir: Path,
    gateway_uids: Optional[List[str]] = None,
    broker: str = "",
    subscribed_topics: Optional[List[str]] = None,
    planned_hours: Optional[float] = None,
    tool_version: str = "",
    gateway_names: Optional[Dict[str, str]] = None,
    label: str = "",
) -> RunSession:
    """Tạo + lưu `RunSession` mới."""
    run_dir = create_run_dir(data_dir, gateway_uids)
    session = RunSession(
        run_dir=run_dir,
        run_id=run_dir.name,
        started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        planned_hours=planned_hours,
        broker=broker,
        subscribed_topics=list(subscribed_topics or []),
        gateways_monitored=len([u for u in (gateway_uids or []) if u]),
        gateway_uids=[u for u in (gateway_uids or []) if u],
        gateway_names=dict(gateway_names or {}),
        label=normalize_session_label(label),
        status=STATUS_RUNNING,
        tool_version=tool_version,
    )
    session.ensure_dirs()
    session.save()
    return session


def list_run_dirs(data_dir: Path) -> List[Path]:
    """Tất cả thư mục run, mới nhất trước (theo tên vì tên bắt đầu bằng timestamp)."""
    runs_root = Path(data_dir) / "runs"
    if not runs_root.exists():
        return []
    dirs = [p for p in runs_root.iterdir() if p.is_dir()]
    return sorted(dirs, key=lambda p: p.name, reverse=True)


def latest_run_dir(data_dir: Path) -> Optional[Path]:
    dirs = list_run_dirs(data_dir)
    return dirs[0] if dirs else None


def open_session(run_dir: Path) -> RunSession:
    """Mở session cũ để xem lại (không đổi trạng thái)."""
    return RunSession.load(Path(run_dir))


def delete_run_dir(run_dir: Path) -> bool:
    """Xoá 1 thư mục run (dùng trong GUI khi dọn dữ liệu cũ)."""
    run_dir = Path(run_dir)
    if not run_dir.exists():
        return False
    try:
        shutil.rmtree(run_dir)
        return True
    except OSError:
        return False


def parse_iso(text: str) -> Optional[datetime]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[: len(fmt)], fmt)
        except ValueError:
            continue
    return None


def human_duration(seconds: Optional[float]) -> str:
    """`3d 04:12:30` — dễ đọc cho test chạy 3-4 ngày."""
    if seconds is None:
        return ""
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return "%dd %02d:%02d:%02d" % (days, hours, minutes, secs)
    return "%02d:%02d:%02d" % (hours, minutes, secs)


def human_size(num_bytes: int) -> str:
    size = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return "%.1f %s" % (size, unit)
        size /= 1024
    return "%.1f TB" % size


# ---------------------------------------------------------------------------
# Lịch sử phiên (tab "Lịch sử phiên")
#
# MỤC ĐÍCH: sau khi dừng phiên test, người dùng phải xem lại được "phiên đó chạy
# những gateway nào và kết quả ra sao" mà KHÔNG cần chạy lại. Toàn bộ dữ liệu đã
# nằm trên đĩa (`meta.json` + `reports/`), các hàm dưới đây chỉ ĐỌC và gộp lại
# thành 1 dòng/phiên cho bảng lịch sử.
#
# LƯU Ý quan trọng (không được ảo giác): một phiên có `status == RUNNING` trong
# `meta.json` KHÔNG có nghĩa là nó đang chạy — nếu tiến trình bị tắt ngang (mất
# điện, kill process) thì `ended_at` rỗng và `reports/` có thể thiếu. Hàm
# `describe_status` phân biệt hai ca này bằng `active_run_id` do GUI truyền vào.
# ---------------------------------------------------------------------------

#: Nhãn hiển thị của cột `status` trong bảng lịch sử.
STATUS_LABELS = {
    STATUS_RUNNING: "Đang chạy",
    STATUS_STOPPED: "Đã dừng",
    STATUS_CRASHED: "Tắt ngang",
}


def describe_status(session: "RunSession", active_run_id: str = "") -> str:
    """Nhãn trạng thái TIẾNG VIỆT, phân biệt "đang chạy" với "bị tắt ngang".

    `active_run_id` = `run_id` của phiên mà tool đang thực sự chạy (GUI truyền
    vào). Chỉ phiên đó mới được gọi là "Đang chạy"; mọi phiên còn `RUNNING` trong
    `meta.json` đều là dấu vết của lần bị tắt ngang.
    """
    if active_run_id and session.run_id == active_run_id:
        return "Đang chạy"
    if session.status == STATUS_STOPPED:
        return "Đã dừng" if session.ended_at else "Đã dừng (thiếu mốc kết thúc)"
    if session.status == STATUS_CRASHED:
        return "Tắt ngang"
    return "Tắt ngang (không chốt báo cáo)"


def report_files(run_dir: Path) -> List[Path]:
    """Các file trong `reports/` của phiên (rỗng nếu chưa chốt báo cáo)."""
    directory = Path(run_dir) / "reports"
    if not directory.exists():
        return []
    return sorted(
        (path for path in directory.iterdir() if path.is_file()),
        key=lambda path: path.name,
    )


def run_summary_row(run_dir: Path, columns: Optional[List[str]] = None) -> Dict[str, Any]:
    """Dòng đầu của `reports/run_summary.csv` (rỗng nếu phiên chưa chốt báo cáo).

    `run_summary.csv` là bản chốt 1 dòng do `pipeline.write_reports()` ghi, nên
    đọc nó rẻ hơn nhiều so với quét lại `mqtt_data_*.csv` (có thể hàng trăm MB).
    """
    from .reader import iter_csv_rows

    path = Path(run_dir) / "reports" / "run_summary.csv"
    for row in iter_csv_rows(path, columns):
        return dict(row)
    return {}


def _as_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def run_history_row(
    session: "RunSession",
    active_run_id: str = "",
    live_devices: object = None,
) -> Dict[str, Any]:
    """Gộp `meta.json` + `run_summary.csv` + kích thước thư mục → 1 dòng lịch sử.

    `live_devices` (tuỳ chọn) là danh sách `GatewayDevice` HIỆN TẠI, chỉ dùng để
    điền tên CHỈ KHI phiên không có ảnh chụp `gateway_names` (phiên của bản tool
    cũ). Phiên có ảnh chụp luôn dùng tên LÚC CHẠY — xoá/đổi gateway trong tab
    Thiết bị hôm nay KHÔNG được làm đổi dữ liệu lịch sử.
    """
    summary = run_summary_row(session.run_dir)
    reports = report_files(session.run_dir)
    size = session.total_bytes()
    duration = session.duration_seconds
    if duration is None:
        # Phiên bị tắt ngang: `duration_seconds` không được chốt. Vẫn có mốc bắt
        # đầu nên suy ra được thời lượng ĐÃ CHẠY cho tới nay (chỉ để tham khảo).
        duration = session.elapsed_seconds()
    if session.ended_at:
        duration_label = human_duration(duration) if duration else "-"
    else:
        # Dấu "≈" để người đọc KHÔNG nhầm đây là thời lượng thật của phiên: phiên
        # bị tắt ngang không có mốc kết thúc nên con số này chỉ là "từ lúc bắt đầu
        # tới hiện tại".
        duration_label = ("≈ " + human_duration(duration)) if duration else "-"
    return {
        "run_id": session.run_id,
        "run_dir": str(session.run_dir),
        # TÊN PHIÊN người dùng đặt (rỗng = chưa đặt) + tên hiển thị đã tính sẵn để
        # GUI không phải tự quyết định "label hay run_id".
        "label": session.label,
        "label_text": session.display_name(),
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "status": session.status,
        "status_label": describe_status(session, active_run_id),
        "finalized": bool(session.ended_at),
        "duration_seconds": duration,
        "duration_label": duration_label,
        "duration_hours": round(duration / 3600.0, 2) if duration else "",
        "duration_estimated": not bool(session.ended_at),
        "broker": session.broker,
        "subscribed_topics": "|".join(session.subscribed_topics),
        "stop_reason": session.stop_reason,
        "gateways_monitored": session.gateways_monitored,
        "gateway_uids": list(session.gateway_uids),
        "gateways_label": session.gateways_label(),
        # Ảnh chụp tên LÚC CHẠY (rỗng với phiên của bản tool cũ) + bảng tra theo
        # mọi cách viết UID — nguồn tên ĐÚNG cho phiên cũ.
        "session_gateway_names": session.gateway_names,
        "gateway_names": _session_name_index(session, live_devices),
        "nodes_seen": _as_int(summary.get("nodes_seen")),
        "gateways_seen": _as_int(summary.get("gateways_seen")),
        "total_messages": _as_int(summary.get("total_messages")),
        "data_packets": _as_int(summary.get("data_packets")),
        "missing_packets": _as_int(summary.get("missing_packets")),
        "duplicate_packets": _as_int(summary.get("duplicate_packets")),
        "ping_failed": _as_int(summary.get("ping_failed")),
        "downtime_seconds": _as_float(summary.get("downtime_seconds")),
        "reconnects": _as_int(summary.get("reconnects")),
        "reports": len(reports),
        "report_dir": str(session.run_dir / "reports"),
        "report_names": "|".join(path.stem for path in reports),
        "files": session.file_count(),
        "size_bytes": size,
        "size_label": human_size(size),
        "tool_version": session.tool_version,
        "notes": session.notes,
        # Toàn bộ `run_summary.csv` giữ nguyên để tab "Tổng kết" hiển thị đủ cột
        # mà KHÔNG phải đọc lại file khi người dùng bấm chọn phiên.
        "summary": summary,
    }


def _session_name_index(
    session: "RunSession", live_devices: object = None
) -> Dict[str, str]:
    """Bảng tra {biến thể gateway UID → tên} cho MỘT phiên.

    Ưu tiên ảnh chụp trong `meta.json`. Chỉ khi phiên không có ảnh chụp (bản tool
    cũ) mới lấy tên sống từ `live_devices`, và khi đó gateway đã bị xoá khỏi
    `devices.json` sẽ rơi về "(chưa có tên)" trong GUI — đúng thực tế: phiên cũ
    đó không ghi lại tên nào.
    """
    if session.gateway_names:
        return gateway_name_index(session.gateway_names.items())
    return gateway_name_index(
        (getattr(d, "uid", ""), getattr(d, "name", "") or getattr(d, "uid", ""))
        for d in (live_devices or ())
    )


def collect_run_history(
    data_dir: Path, active_run_id: str = "", live_devices: object = None
) -> List[Dict[str, Any]]:
    """Danh sách phiên để hiển thị trong tab "Lịch sử phiên" (mới nhất trước)."""
    return [
        run_history_row(open_session(run_dir), active_run_id, live_devices)
        for run_dir in list_run_dirs(data_dir)
    ]
