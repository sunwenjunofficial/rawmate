"""SD 卡相关操作：读卷信息、弹出、格式化。

原则：
  · 只对「当前识别到的卡」动手，绝不接受任意盘符
  · 系统盘、图库盘一律拒绝
  · 格式化走 Windows 自带的 format.com，并且要在控制台里再确认一次
"""
from __future__ import annotations

import ctypes
import os
import string
import subprocess
from ctypes import wintypes
from dataclasses import dataclass

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020
IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# 只允许格式化 128GB 及以下的卡（128GB 卡实际显示约 119GB）
MAX_FORMAT_GB = 130.0


@dataclass
class Volume:
    letter: str
    label: str
    fs: str
    total_gb: float
    free_gb: float
    removable: bool
    has_dcim: bool

    @property
    def root(self) -> str:
        return f"{self.letter}:\\"

    def describe(self) -> str:
        kind = "可移动磁盘" if self.removable else "本地磁盘"
        return (f"{self.letter}:  卷标“{self.label or '无'}”  {self.fs or '未知'}\n"
                f"{kind}  总容量 {self.total_gb:.0f} GB，可用 {self.free_gb:.0f} GB")


def volume_of(path: str) -> Volume:
    letter = _letter_of(path)
    return read_volume(letter) if letter else None


def _letter_of(path: str) -> str:
    if not path:
        return ""
    drive, _ = os.path.splitdrive(os.path.abspath(path))
    return drive[:1].upper() if drive else ""


def read_volume(letter: str) -> Volume:
    root = f"{letter}:\\"
    label = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    maxlen = wintypes.DWORD()
    flags = wintypes.DWORD()
    ctypes.windll.kernel32.GetVolumeInformationW(
        root, label, 261, ctypes.byref(serial), ctypes.byref(maxlen),
        ctypes.byref(flags), fs, 261)
    free = ctypes.c_ulonglong(0)
    total = ctypes.c_ulonglong(0)
    ctypes.windll.kernel32.GetDiskFreeSpaceExW(
        root, ctypes.byref(free), ctypes.byref(total), None)
    drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)
    return Volume(
        letter=letter,
        label=label.value or "",
        fs=fs.value or "",
        total_gb=total.value / 1024 ** 3,
        free_gb=free.value / 1024 ** 3,
        removable=(drive_type == 2),
        has_dcim=os.path.isdir(os.path.join(root, "DCIM")),
    )


def system_letter() -> str:
    return (os.environ.get("SystemDrive") or "C:")[:1].upper()


def check_target(path: str, library_drive: str = "",
                 for_format: bool = False) -> tuple[bool, str, Volume | None]:
    """确认这个路径确实指的是一张可以操作的卡。"""
    vol = volume_of(path)
    if not vol:
        return False, "请先选择 SD 卡所在的文件夹。", None
    if vol.letter == system_letter():
        return False, "这是系统盘，不能操作。", vol
    if library_drive and vol.letter == library_drive.upper():
        return False, "这是照片图库所在的盘，不能操作。", vol
    if not (vol.removable or vol.has_dcim):
        return False, (f"{vol.letter}: 既不是可移动磁盘，也没有 DCIM 文件夹，"
                       "看起来不像需要操作的卡。"), vol
    if for_format and vol.total_gb > MAX_FORMAT_GB:
        return False, (f"{vol.letter}: 总容量 {vol.total_gb:.0f} GB，超过 128 GB 上限。\n\n"
                       "为防止误格式化硬盘，软件只允许格式化 128GB 及以下的卡。"), vol
    return True, "", vol


def card_file_stats(path: str) -> tuple[int, int]:
    """卡上（递归）文件数和总字节。"""
    count = 0
    size = 0
    try:
        for root, _dirs, files in os.walk(path if os.path.isdir(path) else os.path.dirname(path)):
            for name in files:
                count += 1
                try:
                    size += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except Exception:
        pass
    return count, size


def eject(path: str) -> tuple[bool, str]:
    """弹出（安全删除）这张卡。"""
    letter = _letter_of(path)
    if not letter:
        return False, "没有取到盘符。"
    handle = _open_volume(letter)
    if handle is None:
        return _eject_by_mountvol(letter)
    try:
        returned = wintypes.DWORD()
        ok = ctypes.windll.kernel32.DeviceIoControl(
            handle, FSCTL_LOCK_VOLUME, None, 0, None, 0,
            ctypes.byref(returned), None)
        if not ok:
            return False, ("无法锁定该卷：可能有程序正在使用卡上的文件"
                           "（比如 Lightroom 或资源管理器还开着这个文件夹）。")
        ctypes.windll.kernel32.DeviceIoControl(
            handle, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0,
            ctypes.byref(returned), None)
        ok = ctypes.windll.kernel32.DeviceIoControl(
            handle, IOCTL_STORAGE_EJECT_MEDIA, None, 0, None, 0,
            ctypes.byref(returned), None)
        if not ok:
            return _eject_by_mountvol(letter)
        return True, "已弹出，可以拔卡了。"
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _open_volume(letter: str):
    path = f"\\\\.\\{letter}:"
    for access in (GENERIC_READ | GENERIC_WRITE, GENERIC_READ, 0):
        h = ctypes.windll.kernel32.CreateFileW(
            path, access, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
            OPEN_EXISTING, 0, None)
        if h not in (None, INVALID_HANDLE_VALUE, ctypes.c_void_p(-1).value):
            return h
    return None


def _eject_by_mountvol(letter: str) -> tuple[bool, str]:
    try:
        subprocess.run(["mountvol", f"{letter}:", "/P"], capture_output=True,
                       text=True, timeout=20, creationflags=0x08000000)
        return True, "已卸载该盘的盘符（设备可能需要手动拔出）。"
    except Exception as exc:
        return False, f"弹出失败：{exc}"


def format_card(vol: Volume) -> tuple[bool, str]:
    """用 Windows 自带的 format.com 快速格式化，弹控制台让用户再确认一次。"""
    fs = vol.fs or "exFAT"
    if fs.lower() not in ("exfat", "fat32", "ntfs"):
        fs = "exFAT"
    cmd = f'format {vol.letter}: /FS:{fs} /Q /Y'
    label = vol.label or "NO NAME"
    cmd += f' /V:"{label}"'
    try:
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "cmd.exe", f'/c {cmd} & echo. & pause', None, 1)
        if ret <= 32:
            return False, f"无法启动格式化（错误码 {ret}）。"
        return True, "已打开格式化窗口，请按提示确认。"
    except Exception as exc:
        return False, f"启动失败：{exc}"


# ---------------------------------------------------------------- 图形化格式化

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_HIDE = 0
ERROR_CANCELLED = 1223

_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def format_params(vol: Volume, log_path: str) -> str:
    """拼出提权后执行的 PowerShell 参数：静默格式化，结果写进日志文件。"""
    fs = vol.fs if vol.fs.lower() in ("exfat", "fat32", "ntfs") else "exFAT"
    label = (vol.label or "NO NAME").replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop'; try { "
        f"Format-Volume -DriveLetter {vol.letter} -FileSystem {fs} "
        f"-NewFileSystemLabel '{label}' -Confirm:$false -Force -ErrorAction Stop | Out-Null; "
        f"Set-Content -LiteralPath '{log_path}' -Value 'OK' -Encoding UTF8; exit 0 "
        "} catch { "
        f"Set-Content -LiteralPath '{log_path}' -Value ('ERR: ' + $_.Exception.Message) "
        "-Encoding UTF8; exit 1 }"
    )
    return f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "{script}"'


def run_elevated(exe: str, params: str, show: int = SW_HIDE) -> tuple[int, int]:
    """提权运行并返回 (进程句柄, 错误码)；句柄为 0 表示失败。"""
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params
    sei.nShow = show
    if not _shell32.ShellExecuteExW(ctypes.byref(sei)):
        return 0, ctypes.get_last_error()
    return sei.hProcess or 0, 0


def wait_process(handle: int, timeout_ms: int = 300000) -> int | None:
    """等进程结束，返回退出码；超时返回 None。"""
    if not handle:
        return None
    res = _kernel32.WaitForSingleObject(handle, timeout_ms)
    if res != 0:
        return None
    code = wintypes.DWORD()
    _kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    _kernel32.CloseHandle(handle)
    return int(code.value)
