"""判断卡上各个 DCIM 文件夹有没有被拷过。

两种判定：
  快速（默认）—— 文件名 + 精确字节数。列目录就能拿到这两个值，
                一个文件都不用打开，几千张也是秒级。
  精确（按需）—— 拍摄时间 + 毫秒，需要读每张的文件头；能认出台改过名的照片。

文件名单独用是不够的：索尼卡格式化后编号会从 0001 重新开始，会和库里的旧照片撞名。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

from .exif import read_meta

PHOTO_EXT = {".arw", ".cr3", ".cr2", ".nef", ".nrw", ".raf", ".orf", ".rw2", ".dng",
             ".pef", ".srw", ".jpg", ".jpeg", ".heic", ".hif"}
RAW_EXT = {".arw", ".cr3", ".cr2", ".nef", ".nrw", ".raf", ".orf", ".rw2", ".dng",
           ".pef", ".srw", ".3fr", ".iiq", ".mos", ".mrw", ".x3f", ".rwl", ".raw"}


@dataclass
class FolderStatus:
    name: str
    path: str
    count: int = 0
    copied: int = 0

    @property
    def done(self) -> bool:
        return self.copied > 0

    @property
    def status_text(self) -> str:
        if self.count == 0:
            return "空文件夹"
        if self.done:
            return f"已拷贝（{self.copied}/{self.count}）"
        return f"未拷贝（0/{self.count}）"


def _dcim_root(path: str) -> str:
    """不管传进来的是卡根、DCIM 还是 DCIM 下的子文件夹，都归到 DCIM。"""
    norm = os.path.normpath(path) if path else ""
    parts = norm.split(os.sep)
    for i, part in enumerate(parts):
        if part.upper() == "DCIM":
            return os.sep.join(parts[:i + 1])
    if norm and os.path.isdir(os.path.join(norm, "DCIM")):
        return os.path.join(norm, "DCIM")
    return norm


# ---------------------------------------------------------------- 快速：名字+大小

def _catalog_keys(catalog: str) -> set[tuple[str, int]]:
    """目录库里的 (文件名, 字节数)。

    只认磁盘上真的存在的文件——目录里可能还留着已经删掉/移走的记录，
    拿这种"幽灵记录"去比对会把没拷过的照片误判成已拷贝。
    """
    keys: set[tuple[str, int]] = set()
    if not catalog:
        return keys
    try:
        con = sqlite3.connect(f"file:{catalog}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "select f.idx_filename, f.baseName, r.absolutePath, fo.pathFromRoot "
                "from AgLibraryFile f "
                "join AgLibraryFolder fo on f.folder = fo.id_local "
                "join AgLibraryRootFolder r on fo.rootFolder = r.id_local").fetchall()
        finally:
            con.close()
    except Exception:
        return keys

    for idx_name, base, root, folder in rows:
        name = (base or os.path.splitext(str(idx_name or ""))[0])
        if not name:
            continue
        try:
            path = os.path.normpath(str(root).replace("/", "\\")
                                    + str(folder).replace("/", "\\")
                                    + str(idx_name))
        except Exception:
            continue
        try:
            size = os.stat(path).st_size
        except OSError:
            continue
        keys.add((str(name).lower(), size))
    return keys


def _library_roots(catalog: str) -> list[str]:
    roots: list[str] = []
    if not catalog:
        return roots
    try:
        con = sqlite3.connect(f"file:{catalog}?mode=ro", uri=True)
        try:
            for (p,) in con.execute("select absolutePath from AgLibraryRootFolder"):
                if p:
                    roots.append(os.path.normpath(str(p).replace("/", "\\")))
        finally:
            con.close()
    except Exception:
        pass
    return roots


def _disk_keys(roots: list[str], progress=None) -> set[tuple[str, int]]:
    """扫图库目录，补上库里没有 importHash 的那些（也让"拷了没导入"能识别）。"""
    keys: set[tuple[str, int]] = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                for fn in filenames:
                    stem, ext = os.path.splitext(fn)
                    if ext.lower() not in PHOTO_EXT:
                        continue
                    try:
                        size = os.stat(os.path.join(dirpath, fn)).st_size
                    except OSError:
                        continue
                    keys.add((stem.lower(), size))
        except Exception:
            continue
        if progress:
            progress("已读取图库文件名")
    return keys


def library_index(catalog: str, walk: bool = True, progress=None) -> set[tuple[str, int]]:
    keys = _catalog_keys(catalog)
    if walk:
        keys |= _disk_keys(_library_roots(catalog), progress)
    return keys


# ---------------------------------------------------------------- 精确：时间+毫秒

def catalog_times(catalog: str) -> set[str]:
    out: set[str] = set()
    if not catalog:
        return out
    try:
        con = sqlite3.connect(f"file:{catalog}?mode=ro", uri=True)
        try:
            for (ct,) in con.execute("select captureTime from Adobe_images "
                                     "where captureTime is not null"):
                if not ct:
                    continue
                t = str(ct).strip()
                out.add(t[:23])
                out.add(t[:19])
        finally:
            con.close()
    except Exception:
        pass
    return out


_META_CACHE: dict[tuple, str | None] = {}


def _cached_iso(path: str) -> str | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_size, int(st.st_mtime))
    if key in _META_CACHE:
        return _META_CACHE[key]
    iso = read_meta(path).iso_datetime
    _META_CACHE[key] = iso
    return iso


def _exists_in_library(roots: list[str], iso: str | None, name: str) -> bool:
    if not iso or len(iso) < 10:
        return False
    y, m, d = iso[0:4], iso[5:7], iso[8:10]
    day = f"{y}-{m}-{d}"
    for root in roots:
        for cand in (os.path.join(root, y, day, name),
                     os.path.join(root, day, name)):
            if os.path.exists(cand):
                return True
    return False


# ---------------------------------------------------------------- 主流程

def _folder_files(entry) -> tuple[list[str], bool]:
    """返回 (要统计的文件名, 是否只统计 RAW)。文件夹里有 RAW 时只算 RAW，
    避免 RAW+JPEG 成对时把张数算成两倍。"""
    names: list[str] = []
    has_raw = False
    try:
        for f in os.scandir(entry.path):
            if not f.is_file():
                continue
            ext = os.path.splitext(f.name)[1].lower()
            if ext in RAW_EXT:
                has_raw = True
                names.append(f.name)
            elif ext in PHOTO_EXT:
                names.append(f.name)
    except Exception:
        pass
    if has_raw:
        names = [n for n in names if os.path.splitext(n)[1].lower() in RAW_EXT]
    return names, has_raw


def scan_card(card_root: str, catalog: str, exact: bool = False,
              progress=None) -> list[FolderStatus]:
    dcim = _dcim_root(card_root)
    if not os.path.isdir(dcim):
        return []

    keys: set[tuple[str, int]] = set()
    times: set[str] = set()
    roots = _library_roots(catalog)
    if exact:
        if progress:
            progress("读取已导入照片的时间戳…")
        times = catalog_times(catalog)
    else:
        if progress:
            progress("读取图库里已有的照片…")
        keys = library_index(catalog, walk=True, progress=progress)

    try:
        entries = sorted(os.scandir(dcim), key=lambda e: e.name)
    except Exception:
        return []

    out: list[FolderStatus] = []
    for entry in entries:
        if not entry.is_dir():
            continue
        st = FolderStatus(name=entry.name, path=entry.path)
        names, _ = _folder_files(entry)
        st.count = len(names)
        for i, name in enumerate(names, 1):
            full = os.path.join(entry.path, name)
            if exact:
                iso = _cached_iso(full)
                hit = bool(iso) and (iso in times or iso[:19] in times)
                if not hit:
                    hit = _exists_in_library(roots, iso, name)
            else:
                try:
                    size = os.stat(full).st_size
                except OSError:
                    size = -1
                hit = (os.path.splitext(name)[0].lower(), size) in keys
            if hit:
                st.copied += 1
            if progress and i % 200 == 0:
                progress(f"检查 {entry.name}：{i}/{len(names)}")
        out.append(st)
    if progress:
        done = sum(1 for r in out if r.copied)
        progress(f"共 {len(out)} 个文件夹：{done} 个已拷贝，{len(out) - done} 个未拷贝")
    return out
