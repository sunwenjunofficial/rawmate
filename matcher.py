"""配对引擎：先按文件名配，配不上的再用「拍摄时间+毫秒」反查。

这样绝大多数情况下只需要列目录，不必把卡上几千个 RAW 的 EXIF 全读一遍。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import detect
from .exif import ImageMeta, read_meta

ST_OK = "ok"
ST_MISSING = "missing"
ST_AMBIGUOUS = "ambiguous"


@dataclass
class Item:
    jpg_path: str
    jpg_meta: ImageMeta
    raw_path: str = ""
    method: str = ""
    status: str = ""
    note: str = ""
    size: int = 0
    action: str = ""
    error: str = ""

    @property
    def jpg_name(self) -> str:
        return os.path.basename(self.jpg_path)

    @property
    def raw_name(self) -> str:
        return os.path.basename(self.raw_path) if self.raw_path else ""

    @property
    def stem(self) -> str:
        return os.path.splitext(os.path.basename(self.jpg_path))[0]


@dataclass
class RawIndex:
    by_name: dict[str, list[str]] = field(default_factory=dict)
    by_key: dict[tuple, list[str]] = field(default_factory=dict)
    metas: dict[str, ImageMeta] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    total: int = 0
    scanned: int = 0
    meta_ready: bool = False

    @classmethod
    def build(cls, folders: list[str]) -> "RawIndex":
        """只列文件，不解析，很快。"""
        idx = cls()
        for folder in folders:
            for root, _dirs, names in os.walk(folder):
                for name in names:
                    if os.path.splitext(name)[1].lower() in detect.RAW_EXT:
                        path = os.path.join(root, name)
                        idx.files.append(path)
                        idx.by_name.setdefault(
                            os.path.splitext(name)[0].lower(), []).append(path)
        idx.total = len(idx.files)
        return idx

    def index_meta(self, progress: Optional[Callable[[str, int, int], None]] = None,
                   should_stop: Optional[Callable[[], bool]] = None) -> None:
        """读全部 RAW 的拍摄时间，供改名后的照片反查。"""
        if self.meta_ready:
            return
        for i, path in enumerate(self.files, 1):
            if should_stop and should_stop():
                break
            meta = read_meta(path)
            self.metas[path] = meta
            if meta.key:
                self.by_key.setdefault(meta.key, []).append(path)
            self.scanned = i
            if progress and (i % 20 == 0 or i == self.total):
                progress("读取卡上 RAW 的拍摄时间", i, self.total)
        self.meta_ready = True

    def find(self, jpg_meta: ImageMeta, stem: str) -> tuple[str, str, str, str]:
        """返回 (raw_path, method, status, note)。"""
        cands = self.by_name.get(stem.lower(), [])
        if len(cands) == 1:
            return cands[0], "同名", ST_OK, ""
        if len(cands) > 1:
            key = jpg_meta.key
            if key and self.meta_ready:
                narrowed = [c for c in cands
                            if self.metas.get(c) and self.metas[c].key == key]
                if len(narrowed) == 1:
                    return narrowed[0], "同名+时间", ST_OK, ""
            return "", "", ST_AMBIGUOUS, f"卡上有 {len(cands)} 个同名 RAW，无法确定"

        key = jpg_meta.key
        if key and self.meta_ready:
            hits = self.by_key.get(key, [])
            if len(hits) == 1:
                return hits[0], "拍摄时间", ST_OK, "文件名不同，按拍摄时间配对"
            if len(hits) > 1:
                return "", "", ST_AMBIGUOUS, f"同一时刻有 {len(hits)} 个 RAW，无法确定"
        if not jpg_meta.capture_time:
            return "", "", ST_MISSING, "JPEG 里没有拍摄时间，无法按时间匹配"
        return "", "", ST_MISSING, "卡上找不到对应的 RAW"


def collect_jpegs(folder: str) -> list[str]:
    out = []
    try:
        for name in sorted(os.listdir(folder)):
            if os.path.splitext(name)[1].lower() in (".jpg", ".jpeg"):
                out.append(os.path.join(folder, name))
    except Exception:
        pass
    return out


def scan(
    folder: str,
    raw_folders: list[str],
    progress: Optional[Callable[[str, int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> tuple[list[Item], RawIndex]:
    jpegs = collect_jpegs(folder)
    index = RawIndex.build(raw_folders)

    items: list[Item] = []
    pending: list[Item] = []
    for i, jp in enumerate(jpegs, 1):
        if should_stop and should_stop():
            break
        meta = read_meta(jp)
        stem = os.path.splitext(os.path.basename(jp))[0]
        raw, method, status, note = index.find(meta, stem)
        it = Item(jpg_path=jp, jpg_meta=meta, raw_path=raw, method=method,
                  status=status, note=note)
        items.append(it)
        if status != ST_OK and not (index.by_name.get(stem.lower())):
            pending.append(it)
        if progress:
            progress("配对", i, len(jpegs))

    # 有改过名的，才去读卡上 RAW 的拍摄时间
    if pending and not (should_stop and should_stop()):
        index.index_meta(progress, should_stop)
        for it in pending:
            stem = os.path.splitext(os.path.basename(it.jpg_path))[0]
            raw, method, status, note = index.find(it.jpg_meta, stem)
            it.raw_path, it.method, it.status, it.note = raw, method, status, note

    for it in items:
        if it.raw_path:
            try:
                it.size = os.path.getsize(it.raw_path)
            except Exception:
                it.size = 0
    return items, index
