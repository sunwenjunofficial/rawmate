"""Lightroom 目录（.lrcat）读写。

说明：Adobe 没有公开「导入」接口，这里是在 LrC 关闭时，按它自己写入目录的
数据结构，把照片登记进目录库。每一步都在单一事务里完成，写入前强制备份，
出错自动回滚，并提供「恢复备份」。
"""
from __future__ import annotations

import datetime as dt
import math
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

CFA_EPOCH = 978307200  # 2001-01-01 与 1970-01-01 的秒差


def _uuid() -> str:
    return str(uuid.uuid4()).upper()


def _cfa(d: Optional[dt.datetime]) -> float:
    if d is None:
        return 0.0
    return d.timestamp() - CFA_EPOCH


# ---------------------------------------------------------------- 目录发现

@dataclass
class Root:
    id: int
    absolute_path: str   # 形如 D:/图片/2026/
    name: str


@dataclass
class Library:
    catalog: str
    roots: list[Root]

    def root_for(self, year: int) -> Optional[Root]:
        want = str(year)
        for r in self.roots:
            if r.name == want:
                return r
        for r in self.roots:
            if r.absolute_path.rstrip("/").endswith("/" + want):
                return r
        return None


def open_library(catalog: str) -> Library:
    con = sqlite3.connect(f"file:{catalog}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select id_local, absolutePath, name from AgLibraryRootFolder").fetchall()
    finally:
        con.close()
    roots = [Root(id=r[0], absolute_path=r[1], name=r[2] or "") for r in rows]
    return Library(catalog=catalog, roots=roots)


def is_lightroom_running() -> bool:
    return bool(lightroom_pids())


def lightroom_pids() -> list[int]:
    try:
        out = subprocess.run(["tasklist", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=0x08000000).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if parts and parts[0].lower() == "lightroom.exe":
            try:
                pids.append(int(parts[1]))
            except Exception:
                pass
    return pids


# ---------------------------------------------------------------- 备份

BACKUP_DIRNAME = "RAW配对助手_备份"


def backup_catalog(catalog: str) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(os.path.dirname(catalog), BACKUP_DIRNAME, stamp)
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.basename(catalog)
    for suffix in ("", "-wal", "-shm"):
        src = catalog + suffix
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(out_dir, base + suffix))
    return out_dir


def list_backups(catalog: str) -> list[str]:
    d = os.path.join(os.path.dirname(catalog), BACKUP_DIRNAME)
    if not os.path.isdir(d):
        return []
    return sorted((os.path.join(d, x) for x in os.listdir(d)), reverse=True)


def restore_backup(catalog: str, backup_dir: str) -> None:
    base = os.path.basename(catalog)
    src = os.path.join(backup_dir, base)
    if not os.path.exists(src):
        raise FileNotFoundError("备份里没有目录文件")
    for suffix in ("-wal", "-shm"):
        p = catalog + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
    shutil.copy2(src, catalog)


# ---------------------------------------------------------------- 写入

class IngestError(RuntimeError):
    pass


class DuplicateFile(Exception):
    """目录里已经有这张照片了。"""


@dataclass
class IngestResult:
    ok: int = 0
    skipped: list[str] = None
    failed: list[tuple[str, str]] = None
    backup_dir: str = ""
    image_ids: list[int] = None

    def __post_init__(self):
        self.skipped = self.skipped or []
        self.failed = self.failed or []
        self.image_ids = self.image_ids or []


def _norm(path: str) -> str:
    return os.path.abspath(path).replace("\\", "/").rstrip("/")


class CatalogWriter:
    """把已复制到位的 RAW 登记进 LrC 目录。"""

    def __init__(self, catalog: str, progress: Optional[Callable[[str], None]] = None):
        self.catalog = catalog
        self.progress = progress or (lambda msg: None)
        self.backup_dir = ""

    # ---------- 预检 ----------
    def preflight(self) -> None:
        pids = lightroom_pids()
        if pids:
            raise IngestError(
                "Lightroom 正在运行（进程号 %s）。写入目录必须先完全退出 Lightroom，"
                "否则可能损坏目录。" % ", ".join(map(str, pids)))
        if not os.path.exists(self.catalog):
            raise IngestError("找不到目录文件：%s" % self.catalog)

    # ---------- 主流程 ----------
    def ingest(self, files: list[str], dest_dir: str) -> IngestResult:
        """files: 已经复制到 dest_dir 的 RAW 绝对路径列表。"""
        self.preflight()
        self.progress("正在备份 Lightroom 目录…")
        self.backup_dir = backup_catalog(self.catalog)

        con = sqlite3.connect(self.catalog, timeout=30, isolation_level=None)
        res = IngestResult(backup_dir=self.backup_dir)
        try:
            con.execute("BEGIN IMMEDIATE")
            folder_id, root_id = self._ensure_folder(con, dest_dir)
            import_id = self._new_import(con, len(files))
            done = 0
            for path in files:
                if not os.path.exists(path):
                    res.failed.append((path, "文件不存在"))
                    continue
                try:
                    image_id = self._insert_image(con, path, folder_id, import_id)
                    res.image_ids.append(image_id)
                    done += 1
                    res.ok += 1
                except DuplicateFile:
                    res.skipped.append(path)
                except Exception as exc:  # 单张失败不拖垮整批
                    res.failed.append((path, str(exc)))
                self.progress(f"写入目录 {done}/{len(files)}：{os.path.basename(path)}")
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            con.close()
            raise
        finally:
            try:
                con.close()
            except Exception:
                pass

        self.progress("校验目录完整性…")
        self._verify()
        self._prune_backups()
        return res

    def _verify(self) -> None:
        con = sqlite3.connect(f"file:{self.catalog}?mode=ro", uri=True)
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                raise IngestError("目录完整性校验失败：%s" % (row[0] if row else "未知"))
        finally:
            con.close()

    def _prune_backups(self, keep: int = 5) -> None:
        for old in list_backups(self.catalog)[keep:]:
            shutil.rmtree(old, ignore_errors=True)

    # ---------- 修复 ----------
    def repair(self, progress: Optional[Callable[[str], None]] = None) -> int:
        """给已经登记过的照片补上合法的内部 XMP 包（老版本写的是空包）。"""
        from . import xmp
        from .exif import read_meta

        progress = progress or (lambda m: None)
        self.preflight()
        self.progress("正在备份 Lightroom 目录…")
        self.backup_dir = backup_catalog(self.catalog)

        con = sqlite3.connect(self.catalog, timeout=30, isolation_level=None)
        fixed = 0
        try:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "select i.id_local, r.absolutePath, fo.pathFromRoot, f.idx_filename "
                "from AgLibraryImportImage ii "
                "join AgLibraryImport imp on ii.import = imp.id_local "
                "join Adobe_images i on ii.image = i.id_local "
                "join AgLibraryFile f on i.rootFile = f.id_local "
                "join AgLibraryFolder fo on f.folder = fo.id_local "
                "join AgLibraryRootFolder r on fo.rootFolder = r.id_local "
                "where imp.name = 'RAW配对助手'").fetchall()
            for n, (image_id, root, folder, name) in enumerate(rows, 1):
                path = os.path.normpath(root.replace("/", "\\")
                                        + folder.replace("/", "\\") + name)
                if not os.path.exists(path):
                    continue
                meta = read_meta(path)
                blob, dig = xmp.build_blob(meta, name)
                cur = con.execute(
                    "update Adobe_AdditionalMetadata set xmp=?, additionalInfoSet=1, "
                    "internalXmpDigest=?, lastSynchronizedHash=?, metadataVersion=4.0 "
                    "where image=?", (blob, dig, dig, image_id))
                if cur.rowcount == 0:
                    con.execute(
                        "insert into Adobe_AdditionalMetadata (id_global, image, isRawFile, xmp, "
                        "additionalInfoSet, metadataVersion, internalXmpDigest, "
                        "lastSynchronizedHash) values (?,?,?,?,?,?,?,?)",
                        (_uuid(), image_id, 1, blob, 1, 4.0, dig, dig))
                con.execute("update Adobe_images set sidecarStatus=1 where id_local=? "
                            "and (sidecarStatus is null or sidecarStatus=0)", (image_id,))
                con.execute("update Adobe_images set pyramidIDCache='none' where id_local=? "
                            "and pyramidIDCache is null", (image_id,))
                dev = con.execute("select developSettingsIDCache from Adobe_images "
                                  "where id_local=?", (image_id,)).fetchone()
                if not dev or not dev[0]:
                    dev_id = self._default_develop_settings(con, image_id)
                    if dev_id:
                        con.execute("update Adobe_images set developSettingsIDCache=? "
                                    "where id_local=?", (dev_id, image_id))
                fixed += 1
                progress(f"修复 {n}/{len(rows)}：{name}")
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            con.close()
        self._verify()
        return fixed

    # ---------- 具体写入 ----------
    def _ensure_folder(self, con: sqlite3.Connection, dest_dir: str) -> tuple[int, int]:
        abs_dest = _norm(dest_dir)
        parent = os.path.dirname(abs_dest).replace("\\", "/")
        root_path = parent.rstrip("/") + "/"
        name = os.path.basename(parent)
        rel = abs_dest[len(parent):].strip("/")

        row = con.execute(
            "select id_local from AgLibraryRootFolder where lower(absolutePath)=lower(?)",
            (root_path,)).fetchone()
        if row:
            root_id = row[0]
        else:
            try:
                rel_from_cat = os.path.relpath(parent, os.path.dirname(self.catalog)).replace("\\", "/") + "/"
            except Exception:
                rel_from_cat = None
            cur = con.execute(
                "insert into AgLibraryRootFolder (id_global, absolutePath, name, relativePathFromCatalog) "
                "values (?,?,?,?)", (_uuid(), root_path, name, rel_from_cat))
            root_id = cur.lastrowid

        path_from_root = rel + "/"
        row = con.execute(
            "select id_local from AgLibraryFolder where rootFolder=? and lower(pathFromRoot)=lower(?)",
            (root_id, path_from_root)).fetchone()
        if row:
            folder_id = row[0]
        else:
            cur = con.execute(
                "insert into AgLibraryFolder (id_global, parentId, pathFromRoot, rootFolder, visibility) "
                "values (?,?,?,?,?)", (_uuid(), None, path_from_root, root_id, None))
            folder_id = cur.lastrowid
        return folder_id, root_id

    def _new_import(self, con: sqlite3.Connection, count: int) -> int:
        now = dt.datetime.now().astimezone().isoformat(timespec="milliseconds")
        cur = con.execute(
            "insert into AgLibraryImport (imageCount, importDate, name) values (?,?,?)",
            (count, now, "RAW配对助手"))
        return cur.lastrowid

    def _intern(self, con: sqlite3.Connection, table: str, value: str) -> Optional[int]:
        if not value:
            return None
        row = con.execute(f"select id_local from {table} where value=?", (value,)).fetchone()
        if row:
            return row[0]
        cur = con.execute(f"insert into {table} (value) values (?)", (value,))
        return cur.lastrowid

    # 目录里「未改过的默认调色设置」是同一份模板，克隆它最稳
    _DEV_TEMPLATE_SQL = (
        "select text, digest, aiEditStatus, aiUpdateRequiredStatus, hasDevelopAdjustmentsEx, "
        "validatedForVersion, croppedWidth, grayscale, hasAIMasks, hasBigData, hasLensBlur, "
        "hasMasks, hasPointColor, profileCorrections, removeChromaticAberration, "
        "processVersion, whiteBalance from Adobe_imageDevelopSettings "
        "where settingsID='default' limit 1")

    def _default_develop_settings(self, con: sqlite3.Connection,
                                  image_id: int) -> Optional[int]:
        """给新照片补一份默认调色设置，否则 Develop 模块要先在图库里点一遍才认。"""
        tpl = con.execute(self._DEV_TEMPLATE_SQL).fetchone()
        if not tpl:
            return None
        (text, digest, ai, ai2, hda, vfv, cw, gray, aim, hbd, hlb, hm, hpc,
         pc, rca, pv, wb) = tpl
        cur = con.execute(
            "insert into Adobe_imageDevelopSettings (aiEditStatus, aiUpdateRequiredStatus, "
            "croppedWidth, digest, grayscale, hasAIMasks, hasBigData, hasDevelopAdjustmentsEx, "
            "hasLensBlur, hasMasks, hasPointColor, historySettingsID, image, processVersion, "
            "profileCorrections, removeChromaticAberration, settingsID, text, "
            "validatedForVersion, whiteBalance) "
            "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ai, ai2, cw, digest, gray, aim, hbd, hda, hlb, hm, hpc, _uuid(), image_id,
             pv, pc, rca, "default", text, vfv, wb))
        return cur.lastrowid

    def _insert_image(self, con: sqlite3.Connection, path: str, folder_id: int,
                      import_id: int) -> int:
        from . import xmp
        from .exif import read_meta

        meta = read_meta(path)
        base, ext = os.path.splitext(os.path.basename(path))
        ext = ext.lstrip(".")
        st = os.stat(path)
        mtime = dt.datetime.fromtimestamp(st.st_mtime)
        taken = None
        if meta.capture_time:
            try:
                taken = dt.datetime.strptime(meta.capture_time, "%Y:%m:%d %H:%M:%S")
                if meta.subsec:
                    taken = taken.replace(microsecond=int(meta.subsec.ljust(6, "0")[:6]))
            except Exception:
                taken = None

        folder_id = folder_id
        exists = con.execute(
            "select id_local from AgLibraryFile where folder=? and lc_idx_filename=?",
            (folder_id, f"{base}.{ext}".lower())).fetchone()
        if exists:
            raise DuplicateFile(os.path.basename(path))

        file_id = con.execute(
            "insert into AgLibraryFile (id_global, baseName, extension, folder, idx_filename, "
            "lc_idx_filename, lc_idx_filenameExtension, originalFilename, externalModTime, "
            "modTime, importHash, sidecarExtensions) values (?,?,?,?,?,?,?,?,?,?,?,?)",
            (_uuid(), base, ext, folder_id, f"{base}.{ext}", f"{base}.{ext}".lower(),
             ext.lower(), f"{base}.{ext}", _cfa(mtime), _cfa(mtime),
             "%0.3f:%s.%s:%d" % (_cfa(taken or mtime), base, ext, st.st_size), "")
        ).lastrowid

        cap = None
        if taken:
            cap = taken.strftime("%Y-%m-%dT%H:%M:%S.") + f"{taken.microsecond // 1000:03d}"

        w, h = meta.width, meta.height
        aspect = (w / h) if (w and h) else -1
        image_id = con.execute(
            "insert into Adobe_images (id_global, aspectRatioCache, bitDepth, captureTime, "
            "colorChannels, colorLabels, colorMode, copyCreationTime, editLock, fileFormat, "
            "fileHeight, fileWidth, orientation, pick, positionInFolder, rootFile, "
            "sidecarStatus, touchCount, touchTime) "
            "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_uuid(), aspect, 16, cap, 3, "", 32803, -63113817600, 0, "RAW",
             h, w, "AB", 0, "z", file_id, 1, 0, 0)).lastrowid

        blob, xmp_digest = xmp.build_blob(meta, f"{base}.{ext}")

        dev_id = self._default_develop_settings(con, image_id)
        con.execute(
            "update Adobe_images set developSettingsIDCache=?, pyramidIDCache='none' "
            "where id_local=?", (dev_id, image_id))

        props_id = con.execute(
            "insert into Adobe_imageProperties (id_global, image, propertiesString) "
            "values (?,?,?)", (_uuid(), image_id, "properties = {\n}\n")).lastrowid
        con.execute("update Adobe_images set propertiesCache=? where id_local=?",
                    (props_id, image_id))

        model_ref = self._intern(con, "AgInternedExifCameraModel", meta.model)
        lens_ref = self._intern(con, "AgInternedExifLens", meta.lens)
        shutter = None
        if meta.exposure:
            try:
                shutter = math.log2(1.0 / float(meta.exposure))
            except Exception:
                shutter = None
        day = month = year = None
        if taken:
            day, month, year = taken.day, taken.month, taken.year
        con.execute(
            "insert into AgHarvestedExifMetadata (image, aperture, cameraModelRef, dateDay, "
            "dateMonth, dateYear, flashFired, focalLength, hasGPS, isoSpeedRating, lensRef, "
            "shutterSpeed, gpsSequence) values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (image_id, meta.fnumber, model_ref, day, month, year, 0, meta.focal, 0,
             meta.iso, lens_ref, shutter, 0))

        con.execute("insert into AgHarvestedIptcMetadata (image, locationDataOrigination, "
                    "locationGPSSequence) values (?,?,?)", (image_id, "unset", -1))
        con.execute("insert into AgLibraryIPTC (image) values (?)", (image_id,))
        con.execute("insert into AgHarvestedDNGMetadata (image, hasFastLoadData, "
                    "hasLossyCompression, isDNG, isHDR, isPano, isReducedResolution) "
                    "values (?,0,0,0,0,0,0)", (image_id,))
        con.execute("insert into AgDevelopAdditionalMetadata (image, hasCAISign, hasDepthMap) "
                    "values (?,0,0)", (image_id,))
        con.execute("insert into Adobe_AdditionalMetadata (id_global, image, isRawFile, xmp, "
                    "additionalInfoSet, metadataVersion, internalXmpDigest, "
                    "lastSynchronizedHash) values (?,?,?,?,?,?,?,?)",
                    (_uuid(), image_id, 1, blob, 1, 4.0, xmp_digest, xmp_digest))
        con.execute("insert into AgLibraryImageAttributes (image, lastExportTimestamp, "
                    "lastPublishTimestamp) values (?,0,0)", (image_id,))
        tz_secs = int(time.timezone if time.daylight == 0 else time.altzone) * -1
        con.execute("insert into AgLibraryImageChangeCounter (image, changeCounter, "
                    "lastSyncedChangeCounter, changedAtTime, localTimeOffsetSecs) "
                    "values (?,0,0,?,?)",
                    (image_id, dt.datetime.now().astimezone().isoformat(timespec="milliseconds"), tz_secs))
        con.execute("insert into AgSourceColorProfileConstants (image, profileName) "
                    "values (?,?)", (image_id, "sRGB IEC61966-2.1"))
        con.execute("insert into AgMetadataSearchIndex (image, exifSearchIndex, iptcSearchIndex, "
                    "otherSearchIndex, searchIndex) values (?,?,?,?,?)",
                    (image_id, "", "", "", ""))
        con.execute("insert into AgLibraryImportImage (image, import) values (?,?)",
                    (image_id, import_id))
        return image_id
