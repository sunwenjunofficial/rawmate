"""盘符/文件夹识别：找 SD 卡、找 Lightroom 目录、猜选片文件夹。"""
from __future__ import annotations

import glob
import os
import string
from dataclasses import dataclass
from typing import Iterable

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".hif", ".tif", ".tiff"}
RAW_EXT = {".arw", ".cr3", ".cr2", ".nef", ".nrw", ".raf", ".orf", ".rw2", ".dng",
           ".pef", ".srw", ".3fr", ".iiq", ".mos", ".mrw", ".x3f", ".rwl", ".raw"}


@dataclass
class Drive:
    letter: str
    root: str
    label: str = ""
    free_gb: float = 0.0
    total_gb: float = 0.0
    is_removable: bool = False
    has_dcim: bool = False

    @property
    def dcim(self) -> str:
        return os.path.join(self.root, "DCIM")

    def describe(self) -> str:
        tag = "SD卡" if self.has_dcim else ("可移动" if self.is_removable else "本地盘")
        return f"{self.letter}:  [{tag}] {self.label}  可用 {self.free_gb:.0f} GB"


def _volume_info(root: str) -> tuple[str, float, float, bool]:
    """返回 (卷标, 可用GB, 总GB, 是否可移动)，失败时给空值。"""
    try:
        import ctypes
        from ctypes import wintypes

        drive = root.rstrip("\\") + "\\"
        label = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        maxlen = wintypes.DWORD()
        flags = wintypes.DWORD()
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            drive, label, 261, ctypes.byref(serial), ctypes.byref(maxlen),
            ctypes.byref(flags), fs, 261)
        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            drive, ctypes.byref(free), ctypes.byref(total), None)
        removable = bool(flags.value & 0x2) or bool(flags.value & 0x1)
        return (label.value if ok else ""), free.value / 1024 ** 3, total.value / 1024 ** 3, removable
    except Exception:
        try:
            import shutil
            total, used, free = shutil.disk_usage(root)
            return "", free / 1024 ** 3, total / 1024 ** 3, False
        except Exception:
            return "", 0.0, 0.0, False


def list_drives() -> list[Drive]:
    out = []
    for ch in string.ascii_uppercase:
        root = f"{ch}:\\"
        if not os.path.exists(root):
            continue
        label, free, total, rem = _volume_info(root)
        d = Drive(letter=ch, root=root, label=label, free_gb=free,
                  total_gb=total, is_removable=rem)
        d.has_dcim = os.path.isdir(os.path.join(root, "DCIM"))
        out.append(d)
    return out


def find_cards() -> list[Drive]:
    cards = [d for d in list_drives() if d.has_dcim]
    cards.sort(key=lambda d: (not d.is_removable, d.letter))
    return cards


_SKIP_DIRS = {
    "windows", "program files", "program files (x86)", "programdata", "appdata",
    "$recycle.bin", "system volume information", "recovery", "perflogs", "msocache",
    "intel", "nvidia", "amd", "temp", "tmp", "node_modules", ".git", "lightroom",
    "adobe", "downloads",
}


def guess_selection_folder(library_drive: str = "", max_dirs: int = 900,
                           days: int = 30) -> str:
    """猜选片夹：非卡、非图库盘上，最近改动过、且里面直接放着 JPEG 的文件夹（最多两层）。"""
    import time as _t

    best = ("", 0.0)
    scanned = 0
    now = _t.time()
    for d in list_drives():
        if d.has_dcim:
            continue
        if library_drive and d.letter.upper() == library_drive.upper():
            continue
        stack = [(d.root, 0)]
        while stack and scanned < max_dirs:
            cur, depth = stack.pop()
            try:
                names = os.listdir(cur)
            except Exception:
                continue
            for name in names:
                p = os.path.join(cur, name)
                if not os.path.isdir(p) or name.startswith(".") or name.lower() in _SKIP_DIRS:
                    continue
                scanned += 1
                try:
                    entries = os.listdir(p)
                    mt = os.path.getmtime(p)
                except Exception:
                    continue
                has = any(os.path.splitext(e)[1].lower() in IMAGE_EXT for e in entries[:400])
                if has and (now - mt) < days * 86400 and mt > best[1]:
                    best = (p, mt)
                if depth < 1:
                    stack.append((p, depth + 1))
    return best[0]


_CATALOG_CANDIDATES = [
    r"{home}\Pictures\Lightroom",
    r"{home}\Pictures",
    r"D:\图片\Lightroom",
    r"E:\图片\Lightroom",
    r"F:\图片\Lightroom",
]


def find_catalogs() -> list[str]:
    """找 .lrcat。先看常见位置，再受限深度扫一遍盘。"""
    home = os.path.expanduser("~")
    found: list[str] = []
    for pat in _CATALOG_CANDIDATES:
        d = pat.format(home=home)
        found += glob.glob(os.path.join(d, "*.lrcat"))
    if found:
        return sorted(set(found))

    for ch in string.ascii_uppercase:
        root = f"{ch}:\\"
        if not os.path.exists(root):
            continue
        for depth in ("*", os.path.join("*", "*")):
            try:
                found += glob.glob(os.path.join(root, depth, "*.lrcat"))
            except Exception:
                pass
        if found:
            break
    return sorted(set(found))


def find_lightroom() -> str:
    """找 Lightroom Classic 的可执行文件。"""
    for path in _LR_CANDIDATES:
        if path and os.path.exists(path):
            return path
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                key = winreg.OpenKey(
                    hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Lightroom.exe")
                val = winreg.QueryValue(key, None)
                if val and os.path.exists(val):
                    return val
            except Exception:
                continue
    except Exception:
        pass
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if not base:
            continue
        adobe = os.path.join(base, "Adobe")
        try:
            for name in sorted(os.listdir(adobe)):
                if name.lower().startswith("adobe lightroom"):
                    exe = os.path.join(adobe, name, "Lightroom.exe")
                    if os.path.exists(exe):
                        return exe
        except Exception:
            pass
    return ""


_LR_CANDIDATES = [
    r"C:\Program Files\Adobe\Adobe Lightroom Classic\Lightroom.exe",
    r"C:\Program Files (x86)\Adobe\Adobe Lightroom Classic\Lightroom.exe",
]


def count_files(folder: str, exts: Iterable[str]) -> int:
    exts = {e.lower() for e in exts}
    n = 0
    try:
        for name in os.listdir(folder):
            if os.path.splitext(name)[1].lower() in exts:
                n += 1
    except Exception:
        pass
    return n
