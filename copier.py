"""复制与校验：先写临时文件、校验大小、再原子改名，绝不产生半截文件。"""
from __future__ import annotations

import os
import shutil
from typing import Callable, Optional

CHUNK = 4 * 1024 * 1024
TMP_SUFFIX = ".rawmate-tmp"


def free_bytes(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except Exception:
        return 0


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} PB"


def copy_file(
    src: str,
    dst: str,
    progress: Optional[Callable[[int], None]] = None,
    stop: Optional[Callable[[], bool]] = None,
) -> int:
    """复制单个文件，返回写入字节数。失败会清理临时文件。"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + TMP_SUFFIX
    written = 0
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                if stop and stop():
                    raise InterruptedError("已取消")
                buf = fi.read(CHUNK)
                if not buf:
                    break
                fo.write(buf)
                written += len(buf)
                if progress:
                    progress(len(buf))
            fo.flush()
            os.fsync(fo.fileno())
        if os.path.getsize(tmp) != os.path.getsize(src):
            raise IOError("复制后大小不一致")
        os.replace(tmp, dst)
        return written
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise
