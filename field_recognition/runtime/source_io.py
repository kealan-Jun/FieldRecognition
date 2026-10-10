"""Bounded, read-only NAS I/O. Child processes never import the application/GPU."""

import base64
import hashlib
import json
import os
from pathlib import Path
from field_recognition.runtime.paths import PROJECT_ROOT
import re
import subprocess
import sys
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
import time

_LOCK = threading.Lock()
_ORPHANS = []
_SLOTS = threading.BoundedSemaphore(4)


class SourceError(Exception):
    def __init__(self, detail, status=503):
        super().__init__(detail)
        self.status = status


def call(request, *, timeout=None):
    if not _SLOTS.acquire(blocking=False):
        raise SourceError("NAS 读取槽位已满，稍后重试")
    try:
        return _call(request, timeout=timeout)
    finally:
        _SLOTS.release()


def _call(request, *, timeout=None):
    timeout = timeout or float(os.environ.get("FIELD_PHOTO_IO_TIMEOUT_SECONDS", "4"))
    with _LOCK:
        _ORPHANS[:] = [p for p in _ORPHANS if p.poll() is None]
        if len(_ORPHANS) >= 2:
            raise SourceError("NAS 读取进程尚未退出，等待存储恢复")
    proc = subprocess.Popen(
        [sys.executable, "-m", "field_recognition.runtime.source_io", "--worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=PROJECT_ROOT,
    )
    try:
        raw, _ = proc.communicate(json.dumps(request), timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        # Do not wait on an uninterruptible filesystem syscall. Bound orphan count.
        with _LOCK:
            _ORPHANS.append(proc)
        for pipe in (proc.stdin, proc.stdout):
            if pipe:
                pipe.close()
        raise SourceError("NAS 读取超时，文件留在持久化队列") from None
    if proc.returncode:
        raise SourceError("NAS 读取进程异常退出")
    reply = json.loads(raw)
    if "error" in reply:
        raise SourceError(reply["error"], reply.get("status", 503))
    return reply


def safe_path(root, supplied):
    root = Path(root).resolve(strict=True)
    path = Path(supplied)
    path = path if path.is_absolute() else root / path
    # Reject symlink components, including same-root links which could change targets.
    relative = path.relative_to(root)
    for i in range(1, len(relative.parts) + 1):
        if (root.joinpath(*relative.parts[:i])).is_symlink():
            raise SourceError("照片路径不能包含符号链接", 422)
    path = path.resolve(strict=True)
    path.relative_to(root)
    return path, relative


def read_stable(path, limit):
    before = path.stat()
    if not path.is_file() or not 0 < before.st_size <= limit:
        raise SourceError("文件为空或超过大小上限", 413)
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(
        raw
    ) != before.st_size:
        raise SourceError("照片或回执仍在写入", 409)
    return raw, after


def read(request):
    path, relative = safe_path(request["root"], request["path"])
    if len(relative.parts) != 4:
        raise SourceError("照片目录不匹配", 422)
    if relative.parts[0] != request["camera"]:
        raise SourceError("照片相机不匹配", 409)
    raw, info = read_stable(path, request["max_bytes"])
    receipt = path.parent / "PhotoReceipt.json"
    metadata = None
    receipt_basis = "nas_photo_receipt"
    if receipt.exists():
        if receipt.is_symlink():
            raise SourceError("连拍回执不能为符号链接", 422)
        content, _ = read_stable(receipt, 128 * 1024)
        try:
            metadata = json.loads(content)
        except (ValueError, UnicodeError):
            raise SourceError("连拍回执格式无效", 422) from None
    elif request.get("batch_mode") == "directory_triplet":
        # Explicit deployment contract: ONE capture directory contains EXACTLY
        # three photos. Opt-in only; never apply a same-second heuristic by default.
        metadata = directory_triplet(request, path, relative)
        receipt_basis = "configured_directory_triplet"
    elif request.get("require_receipt"):
        raise SourceError("等待连拍回执 PhotoReceipt.json", 503)
    return {
        "relative": relative.as_posix(),
        "path": str(path),
        "mtime": info.st_mtime,
        "signature": f"{info.st_size}:{info.st_mtime_ns}",
        "image_base64": base64.b64encode(raw).decode(),
        "receipt": metadata,
        "receipt_basis": receipt_basis if metadata else None,
    }


def directory_triplet(request, path, relative):
    def members():
        return sorted(
            p for p in path.parent.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"}
        )

    paths = members()
    if len(paths) < 3:
        raise SourceError("语音三连拍尚未收齐，保留队列等待", 503)
    if len(paths) != 3:
        raise SourceError("目录超过三张照片，不能确定一次拍照边界", 409)
    photos, signatures = [], []
    for member in paths:
        safe, rel = safe_path(request["root"], member)
        match = re.fullmatch(r"(\d{8})_(\d{6})(?:_\d+)?\.(?:jpg|jpeg|png)", safe.name, re.I)
        if not match:
            raise SourceError("三连拍文件名不符合已配置的目录协议", 422)
        captured = datetime.strptime(match[1] + match[2], "%Y%m%d%H%M%S").replace(
            tzinfo=ZoneInfo(request["zone"])
        )
        if rel.parts[:3] != relative.parts[:3] or rel.parts[1:3] != (
            captured.strftime("%Y-%m-%d"),
            captured.strftime("%H-%M-%S"),
        ):
            raise SourceError("三连拍目录与采集时间不一致", 409)
        raw, info = read_stable(safe, request["max_bytes"])
        if time.time() - info.st_mtime < 0.5:
            raise SourceError("语音三连拍仍在写入", 503)
        signatures.append((info.st_size, info.st_mtime_ns))
        photos.append(
            {
                "filename": safe.name,
                "capture_id": rel.as_posix(),
                "captured_at": captured.isoformat(),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    if paths != members() or signatures != [
        (p.stat().st_size, p.stat().st_mtime_ns) for p in paths
    ]:
        raise SourceError("语音三连拍目录发生变化，等待稳定", 503)
    batch = hashlib.sha256(
        ("directory-triplet/1:" + relative.parent.as_posix()).encode()
    ).hexdigest()
    from field_recognition.measurements.photo_experiment_context import resolve_context

    try:
        context_ref = resolve_context(request["camera"], photos)
    except (ValueError, KeyError, TypeError):
        raise SourceError("实验关联配置无效，请核对相机、时间范围和实验编号", 422) from None
    return {
        "schema_version": "field-photo-receipt/1",
        "camera_id": request["camera"],
        "measurement": {
            "burst_id": "directory-triplet-" + batch,
            "expected_photos": 3,
            **({"experiment_context_ref": context_ref} if context_ref else {}),
        },
        "photos": photos,
    }


def discover(request):
    root = Path(request["root"])
    camera = request["camera"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", camera):
        raise SourceError("相机目录无效", 422)
    if not root.is_dir():
        raise SourceError("照片存储未连接")
    folder = root / camera
    if not folder.exists():
        return {"files": [], "missing_camera": True}
    if folder.is_symlink():
        raise SourceError("相机目录不能为符号链接", 422)
    zone = ZoneInfo(request["zone"])
    started = datetime.fromisoformat(request["since"]).astimezone(zone)
    today = datetime.now(zone).strftime("%Y-%m-%d")
    rows, errors = [], []
    # Recent pass is independent of historical traversal, so a bad old directory
    # cannot block fresh files. Full passes are scheduled separately.
    for day in sorted(folder.iterdir(), reverse=True):
        if (
            not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.name)
            or not started.strftime("%Y-%m-%d") <= day.name <= today
        ):
            continue
        if request.get("recent") and day.name != today:
            continue
        if day.is_symlink() or not day.is_dir():
            continue
        try:
            moments = sorted(day.iterdir(), reverse=True)
        except OSError:
            errors.append(day.name)
            continue
        # Full pass eventually visits every folder; recent pass avoids old scans.
        if request.get("recent"):
            moments = moments[:32]
        for moment in moments:
            if (
                not re.fullmatch(r"\d{2}-\d{2}-\d{2}", moment.name)
                or moment.is_symlink()
                or not moment.is_dir()
            ):
                continue
            try:
                for path in sorted(moment.iterdir()):
                    match = re.fullmatch(
                        r"(\d{8})_(\d{6})(?:_\d+)?\.(?:jpg|jpeg|png)", path.name, re.I
                    )
                    if not match or path.is_symlink() or not path.is_file():
                        continue
                    try:
                        captured = datetime.strptime(match[1] + match[2], "%Y%m%d%H%M%S").replace(
                            tzinfo=zone
                        )
                    except ValueError:
                        continue
                    if (
                        captured < started
                        or day.name != captured.strftime("%Y-%m-%d")
                        or moment.name != captured.strftime("%H-%M-%S")
                    ):
                        continue
                    stat = path.stat()
                    rows.append(
                        [
                            path.relative_to(root).as_posix(),
                            f"{stat.st_size}:{stat.st_mtime_ns}",
                            stat.st_mtime,
                        ]
                    )
            except OSError:
                errors.append(day.name + "/" + moment.name)
    return {"files": rows, "errors": errors}


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
        reply = {"read": read, "discover": discover}[request["action"]](request)
    except SourceError as exc:
        reply = {"error": str(exc), "status": exc.status}
    except (ValueError, KeyError):
        reply = {"error": "照片路径或回执无效", "status": 422}
    except OSError:
        reply = {"error": "照片存储暂不可读", "status": 503}
    print(json.dumps(reply))
