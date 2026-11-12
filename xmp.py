"""生成 Lightroom 内部 XMP 包。

LrC 的 Adobe_AdditionalMetadata.xmp 存的是：
    4 字节大端「解压后长度」 + zlib 压缩的 XMP XML
这里按它的格式生成，字段取自照片自己的 EXIF，避免出现空包导致 LrC 写元数据报错。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import struct
import uuid
import zlib
from typing import Optional

from .exif import ImageMeta

TEMPLATE = """<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 7.0-c000 1.000000, 0000/00/00-00:00:00        ">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/"
    xmlns:tiff="http://ns.adobe.com/tiff/1.0/"
    xmlns:exif="http://ns.adobe.com/exif/1.0/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:aux="http://ns.adobe.com/exif/1.0/aux/"
    xmlns:exifEX="http://cipa.jp/exif/1.0/"
    xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
    xmlns:crd="http://ns.adobe.com/camera-raw-defaults/1.0/"
{attrs}>
{seqs}
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
"""


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _tz() -> str:
    off = dt.datetime.now().astimezone().utcoffset() or dt.timedelta(0)
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def _capture_iso(meta: ImageMeta) -> Optional[str]:
    if not meta.capture_time:
        return None
    try:
        d = dt.datetime.strptime(meta.capture_time, "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None
    ms = (meta.subsec or "0").ljust(3, "0")[:3]
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + ms + _tz()


def _fmt_fraction(value: Optional[float], denom: int = 1) -> Optional[str]:
    if not value:
        return None
    return f"{int(round(value * denom))}/{denom}"


def _exposure_str(exposure: Optional[float]) -> Optional[str]:
    if not exposure:
        return None
    if exposure < 1:
        return f"1/{int(round(1 / exposure))}"
    return f"{exposure:g}/1"


def build_xml(meta: ImageMeta, file_name: str) -> str:
    iso = _capture_iso(meta)
    attrs = [
        ('xmp:Rating', "0"),
        ('xmp:CreatorTool', meta.model or ""),
    ]
    if iso:
        attrs += [("xmp:ModifyDate", iso), ("xmp:CreateDate", iso)]
    attrs += [
        ("xmpMM:DocumentID", "urn:uuid:" + str(uuid.uuid4())),
        ("xmpMM:InstanceID", "urn:uuid:" + str(uuid.uuid4())),
        ("xmpMM:PreservedFileName", file_name),
    ]
    if meta.make:
        attrs.append(("tiff:Make", meta.make))
    if meta.model:
        attrs.append(("tiff:Model", meta.model))
    attrs.append(("tiff:Orientation", str(meta.orientation or 1)))
    if meta.width:
        attrs.append(("tiff:ImageWidth", str(meta.width)))
    if meta.height:
        attrs.append(("tiff:ImageLength", str(meta.height)))
    exp = _exposure_str(meta.exposure)
    if exp:
        attrs.append(("exif:ExposureTime", exp))
    if meta.fnumber:
        attrs.append(("exif:FNumber", _fmt_fraction(meta.fnumber, 10)))
    if meta.focal:
        attrs.append(("exif:FocalLength", _fmt_fraction(meta.focal, 10)))
    if iso:
        attrs.append(("exif:DateTimeOriginal", iso))
    if meta.width:
        attrs.append(("exif:PixelXDimension", str(meta.width)))
    if meta.height:
        attrs.append(("exif:PixelYDimension", str(meta.height)))
    fmt = "image/x-" + ("sony-arw" if (meta.make or "").upper() == "SONY" else "raw")
    attrs.append(("dc:format", fmt))
    if meta.lens:
        attrs += [("aux:Lens", meta.lens), ("exifEX:LensModel", meta.lens)]
    if iso:
        attrs.append(("photoshop:DateCreated", iso))

    attr_text = "".join(f'\n   {k}="{_esc(str(v))}"' for k, v in attrs if str(v) != "")

    seqs = ""
    if meta.iso:
        seqs += ("\n   <exif:ISOSpeedRatings>\n    <rdf:Seq>\n"
                 f"     <rdf:li>{int(meta.iso)}</rdf:li>\n    </rdf:Seq>\n   </exif:ISOSpeedRatings>")
    return TEMPLATE.format(attrs=attr_text, seqs=seqs)


def pack(xml: str) -> bytes:
    """打包成 LrC 内部格式：4 字节大端长度 + zlib。"""
    raw = xml.encode("utf-8")
    comp = zlib.compress(raw, 9)
    return struct.pack(">I", len(raw)) + comp


def unpack(blob: bytes) -> str:
    if not blob or len(blob) < 4:
        raise ValueError("空的 XMP 包")
    return zlib.decompress(blob[4:]).decode("utf-8", "ignore")


def digest(xml: str) -> str:
    return hashlib.md5(xml.encode("utf-8")).hexdigest()


def build_blob(meta: ImageMeta, file_name: str) -> tuple[bytes, str]:
    """返回 (blob, digest)。"""
    xml = build_xml(meta, file_name)
    return pack(xml), digest(xml)
