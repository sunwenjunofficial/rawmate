"""本地设置与报告输出。"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os

APP_NAME = "RAW配对助手"


def app_dir() -> str:
    override = os.environ.get("RAWMATE_HOME")
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    candidates = []
    base = os.environ.get("APPDATA") or ""
    if base:
        candidates.append(os.path.join(base, APP_NAME))
    candidates.append(os.path.join(os.path.expanduser("~"), "." + APP_NAME))
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".w")
            with open(probe, "w") as f:
                f.write("1")
            os.remove(probe)
            return d
        except Exception:
            continue
    import tempfile
    return tempfile.mkdtemp(prefix="rawmate-")


SETTINGS_PATH = os.path.join(app_dir(), "settings.json")

DEFAULTS = {
    "selection_dir": "",
    "card_folder": "",
    "dest_dir": "",
    "catalog": "",
    "auto_dest": True,
    "ingest": True,
    "verify": True,
    "success_count": 0,
    "notice_shown": False,
}


def load() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in data.items() if k in DEFAULTS})
    return out


def save(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def report_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), "Documents", APP_NAME, "报告")
    os.makedirs(d, exist_ok=True)
    return d


def write_report(items, dest_dir: str, title: str = "配对报告") -> tuple[str, str]:
    """输出 txt + csv，返回两个路径。"""
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(report_dir(), f"{title}_{stamp}")
    txt_path, csv_path = base + ".txt", base + ".csv"

    total = len(items)
    ok = sum(1 for i in items if i.status == "ok")
    miss = total - ok
    size = sum(i.size for i in items if i.status == "ok")

    from .copier import human
    lines = [
        f"{APP_NAME} — {title}",
        f"时间：{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"输出位置：{dest_dir}",
        f"合计 {total} 张，配对成功 {ok} 张，未配对 {miss} 张，待复制 {human(size)}",
        "",
        f"{'状态':<12}{'JPEG':<28}{'RAW':<28}{'方式'}",
        "-" * 100,
    ]
    for it in items:
        state = {"ok": "已配对", "missing": "找不到RAW", "ambiguous": "多个候选"}.get(it.status, it.status)
        if it.action:
            state += "/" + {"copied": "已复制", "skipped": "跳过", "failed": "失败"}.get(it.action, it.action)
        lines.append(f"{state:<12}{it.jpg_name:<28}{it.raw_name or '-':<28}{it.method}")
        if it.note:
            lines.append(f"{'':<12}说明：{it.note}")
    with open(txt_path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines))

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["JPEG", "RAW", "配对方式", "状态", "动作", "说明"])
        for it in items:
            w.writerow([it.jpg_name, it.raw_name, it.method, it.status, it.action, it.note])
    return txt_path, csv_path
