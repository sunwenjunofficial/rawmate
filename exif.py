"""EXIF 读取：纯标准库实现，支持 JPEG 与 TIFF 系 RAW（ARW/NEF/CR2/DNG/ORF/PEF/SRW）。

只需要照片拍摄时间（含毫秒）、相机、镜头、曝光参数和像素尺寸这几项，
所以没有引第三方库，也不解析厂商私有段。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

# TIFF 标签
TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_ORIENTATION = 0x0112
TAG_DATETIME = 0x0132
TAG_EXIF_IFD = 0x8769
TAG_DATETIME_ORIGINAL = 0x9003
TAG_DATETIME_DIGITIZED = 0x9004
TAG_SUBSEC_ORIGINAL = 0x9291
TAG_EXPOSURE_TIME = 0x829A
TAG_FNUMBER = 0x829D
TAG_ISO = 0x8827
TAG_FOCAL_LENGTH = 0x920A
TAG_PIXEL_X = 0xA002
TAG_PIXEL_Y = 0xA003
TAG_LENS_MODEL = 0xA434


@dataclass
class ImageMeta:
    path: str = ""
    capture_time: Optional[str] = None      # "2026:11:12 09:06:49"
    subsec: Optional[str] = None            # "888"
    make: str = ""
    model: str = ""
    lens: str = ""
    orientation: int = 1
    width: Optional[int] = None
    height: Optional[int] = None
    iso: Optional[float] = None
    fnumber: Optional[float] = None
    exposure: Optional[float] = None
    focal: Optional[float] = None
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> Optional[tuple]:
        """用于配对的唯一键：拍摄时间 + 毫秒。"""
        if not self.capture_time:
            return None
        return (self.capture_time, self.subsec or "")

    @property
    def iso_datetime(self) -> Optional[str]:
        """转成 LrC 目录里用的 "2026-11-12T09:06:49.888" 格式。"""
        if not self.capture_time:
            return None
        try:
            d, t = self.capture_time.split(" ")
            y, mo, rest = d.split(":")
            # EXIF 里是 YYYY:MM:DD
            ds = f"{y}-{mo}-{rest}"
            ms = (self.subsec or "0").ljust(3, "0")[:3]
            return f"{ds}T{t}.{ms}"
        except Exception:
            return None


def _type_size(t: int) -> int:
    return {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}.get(t, 1)


def _read_value(buf: bytes, e: str, typ: int, count: int, raw: bytes):
    if typ == 2:  # ASCII
        return raw
    n = count
    fmt = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 11: "f", 12: "d"}.get(typ)
    if typ in (5, 10):  # RATIONAL / SRATIONAL
        out = []
        for i in range(n):
            a, b = struct.unpack(e + ("II" if typ == 5 else "ii"), raw[i * 8:i * 8 + 8])
            out.append(a / b if b else None)
        return out
    if fmt is None:
        return None
    size = struct.calcsize(fmt)
    if size * n > len(raw):
        n = len(raw) // size
    vals = struct.unpack(e + fmt * n, raw[:size * n])
    return list(vals)


def _parse_ifd(data: bytes, e: str, off: int, depth: int = 0) -> dict:
    """读一个 IFD，返回 {tag: value}。"""
    out = {}
    if off <= 0 or off + 2 > len(data) or depth > 3:
        return out
    try:
        n = struct.unpack(e + "H", data[off:off + 2])[0]
        n = min(n, 512)
        base = off + 2
        for i in range(n):
            p = base + i * 12
            if p + 12 > len(data):
                break
            tag, typ, count = struct.unpack(e + "HHI", data[p:p + 8])
            raw4 = data[p + 8:p + 12]
            size = _type_size(typ) * count
            if size <= 4:
                raw = raw4[:size]
            else:
                voff = struct.unpack(e + "I", raw4)[0]
                if voff + size > len(data):
                    continue
                raw = data[voff:voff + size]
            try:
                out[tag] = _read_value(data, e, typ, count, raw)
            except Exception:
                continue
    except Exception:
        pass
    return out


def _ascii(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    if isinstance(v, bytes):
        return v.split(b"\x00")[0].decode("utf-8", "ignore").strip()
    return str(v).strip()


def _first(v):
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v


def _tiff_block_from_jpeg(data: bytes) -> Optional[bytes]:
    """从 JPEG 里取出 APP1/Exif 的 TIFF 段。"""
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xDA:  # SOS 之后没有元数据了
            break
        if i + 4 > n:
            break
        seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
        seg = data[i + 4:i + 2 + seglen]
        if marker == 0xE1 and seg[:6] == b"Exif\x00\x00":
            return seg[6:]
        i += 2 + seglen
    return None


def _meta_from_tiff(tiff: bytes, path: str) -> ImageMeta:
    m = ImageMeta(path=path)
    if len(tiff) < 8:
        return m
    if tiff[:2] == b"II":
        e = "<"
    elif tiff[:2] == b"MM":
        e = ">"
    else:
        return m
    magic = struct.unpack(e + "H", tiff[2:4])[0]
    if magic != 42:
        return m
    ifd0 = struct.unpack(e + "I", tiff[4:8])[0]

    d0 = _parse_ifd(tiff, e, ifd0)
    exif_off = _first(d0.get(TAG_EXIF_IFD)) or 0
    d1 = _parse_ifd(tiff, e, int(exif_off)) if exif_off else {}

    m.make = _ascii(d0.get(TAG_MAKE))
    m.model = _ascii(d0.get(TAG_MODEL))
    m.lens = _ascii(d1.get(TAG_LENS_MODEL))
    try:
        m.orientation = int(_first(d0.get(TAG_ORIENTATION)) or 1)
    except Exception:
        m.orientation = 1

    m.capture_time = (_ascii(d1.get(TAG_DATETIME_ORIGINAL))
                      or _ascii(d0.get(TAG_DATETIME)) or None)
    m.subsec = _ascii(d1.get(TAG_SUBSEC_ORIGINAL)) or None
    if m.subsec:
        m.subsec = m.subsec.strip()
    m.iso = _first(d1.get(TAG_ISO))
    m.fnumber = _first(d1.get(TAG_FNUMBER))
    m.exposure = _first(d1.get(TAG_EXPOSURE_TIME))
    m.focal = _first(d1.get(TAG_FOCAL_LENGTH))
    w = _first(d1.get(TAG_PIXEL_X))
    h = _first(d1.get(TAG_PIXEL_Y))
    m.width = int(w) if isinstance(w, (int, float)) and w else None
    m.height = int(h) if isinstance(h, (int, float)) and h else None
    return m


def read_meta(path: str) -> ImageMeta:
    """读取任意支持格式的元数据；失败时返回只带 path 的空对象。

    先只读文件头，解析不出来再读大一点的块 —— 卡上几千个文件时这点 IO 差别很大。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(4)
            if head[:2] == b"\xff\xd8":
                f.seek(0)
                tiff = _tiff_block_from_jpeg(f.read(128 * 1024))
                if not tiff:
                    return ImageMeta(path=path)
                m = _meta_from_tiff(tiff, path)
                if m.capture_time:
                    return m
                f.seek(0)
                big = _tiff_block_from_jpeg(f.read(1024 * 1024))
                return _meta_from_tiff(big, path) if big else m
            if head[:2] in (b"II", b"MM"):
                f.seek(0)
                m = _meta_from_tiff(f.read(64 * 1024), path)
                if m.capture_time or m.model:
                    return m
                f.seek(0)
                return _meta_from_tiff(f.read(512 * 1024), path)
    except Exception:
        pass
    return ImageMeta(path=path)
