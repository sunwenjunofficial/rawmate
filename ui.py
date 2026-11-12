"""图形界面（tkinter）。"""
from __future__ import annotations

import datetime as dt
import os
import queue
import sys
import threading
import tkinter as tk
from collections import Counter
from tkinter import filedialog, messagebox, ttk

from . import cardstatus, config, copier, detect, matcher, removable
from .catalog import (CatalogWriter, IngestError, is_lightroom_running,
                      list_backups, open_library, restore_backup)

APP_TITLE = "RAW 配对助手"
BG = "#f7f7f8"
FG = "#1d1d1f"
MUTED = "#6b6b70"
ACCENT = "#0a6cff"
OK = "#127a3d"
WARN = "#b26a00"
ERR = "#c02b2b"


class PathLine(ttk.Frame):
    """一行路径：编号 + 名称 + 输入框 + 浏览。"""

    def __init__(self, master, index: str, label: str, on_browse):
        super().__init__(master, style="Card.TFrame")
        self.columnconfigure(2, weight=1)
        ttk.Label(self, text=index, style="Badge.TLabel").grid(row=0, column=0, padx=(0, 10))
        ttk.Label(self, text=label, style="CardTitle.TLabel",
                  width=11).grid(row=0, column=1, sticky="w", padx=(0, 10))
        self.var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.var, style="Path.TEntry")
        self.entry.grid(row=0, column=2, sticky="ew")
        self.btn = ttk.Button(self, text="浏览…", command=on_browse, width=7)
        self.btn.grid(row=0, column=3, padx=(8, 0))
        self.hint = ttk.Label(self, text="", style="Hint.TLabel")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1120x720")
        self.minsize(940, 600)
        self.configure(bg=BG)

        self.settings = config.load()
        self.items: list[matcher.Item] = []
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.busy = False
        self.catalog_path = self.settings.get("catalog", "")
        self.dest_dir = ""

        self._setup_style()
        self._set_icon()
        self._build()
        self._bootstrap()
        self.after(80, self._poll)

    def _set_icon(self):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        p = os.path.join(base, "icon.ico")
        if os.path.exists(p):
            try:
                self.iconbitmap(p)
            except Exception:
                pass

    def _first_notice(self):
        pass

    def _show_help(self):
        top = tk.Toplevel(self)
        top.title("使用说明")
        top.transient(self)
        top.configure(bg="white")
        wrap = ttk.Frame(top, style="Card.TFrame", padding=20)
        wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="RAW 配对助手 · 怎么用", style="Title.TLabel").pack(anchor="w")
        text = tk.Text(wrap, width=72, height=20, wrap="word", relief="flat",
                       font=("Microsoft YaHei UI", 10), bg="white", fg=FG,
                       spacing1=2, spacing3=6, padx=2, pady=8)
        body = (
            "① 选片文件夹：你挑出来、复制到移动硬盘上的那堆 JPEG\n"
            "② SD 卡：递归找同名 RAW；改过名的按「拍摄时间+毫秒」自动找回\n"
            "③ 放到：按拍摄日期对应到图库里的日期文件夹，例如 D:\\图片\\2026\\2026-11-12\n\n"
            "点【扫描配对】先看清单，确认后点【开始执行】。\n\n"
            "· 软件只复制 RAW，绝不移动或删除卡里的原文件\n"
            "· 勾选「登记到 Lightroom 目录」后，写库前会自动备份 .lrcat，"
            "打开 LrC 就是已导入状态\n"
            "· 目标已有同名同大小的文件会跳过，重复执行不会变成两份\n\n"
            "「卡内文件夹」选项卡：列出 DCIM 下每个文件夹，标出已拷贝 / 未拷贝。"
            "判定按拍摄时间+毫秒比对，不靠文件名，格式化过的卡也不会误判。\n\n"
            "内存卡两个按钮：弹出内存卡、格式化内存卡（只允许 128GB 及以下的盘）。\n\n"
            "出问题可从「更多」里选【恢复目录备份…】退回上一次的状态。"
        )
        text.insert("1.0", body)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True, pady=(10, 12))
        ttk.Button(wrap, text="知道了", command=top.destroy).pack(anchor="e")
        top.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - top.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - top.winfo_reqheight()) // 2
        top.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        top.grab_set()

    # ------------------------------------------------------------ 样式
    def _setup_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except Exception:
            pass
        base = ("Microsoft YaHei UI", 10)
        style.configure(".", font=base, background=BG, foreground=FG)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background="white")
        style.configure("CardTitle.TLabel", background="white", foreground=FG,
                        font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Hint.TLabel", background="white", foreground=MUTED,
                        font=("Microsoft YaHei UI", 9))
        style.configure("Badge.TLabel", background=ACCENT, foreground="white",
                        font=("Microsoft YaHei UI", 10, "bold"), padding=(9, 3))
        style.configure("Title.TLabel", background=BG, foreground=FG,
                        font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED,
                        font=("Microsoft YaHei UI", 9))
        style.configure("Path.TEntry", padding=6)
        style.configure("Card.TCheckbutton", background="white", foreground=FG)
        style.configure("Danger.TButton", foreground="#c02b2b")
        style.map("Card.TCheckbutton",
                  background=[("active", "white"), ("selected", "white")])
        style.configure("Go.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(16, 8))
        style.configure("Treeview", rowheight=26, font=("Microsoft YaHei UI", 9),
                        background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TLabel", background=BG)
        style.configure("Status.TLabel", background=BG, foreground=MUTED,
                        font=("Microsoft YaHei UI", 9))

    def _card(self, parent, pady=(0, 10)):
        outer = ttk.Frame(parent, style="Card.TFrame", padding=16)
        outer.pack(fill="x", pady=pady)
        return outer

    # ------------------------------------------------------------ 布局
    def _build(self):
        root = ttk.Frame(self, padding=(20, 16, 20, 12))
        root.pack(fill="both", expand=True)

        head = ttk.Frame(root)
        head.pack(fill="x", pady=(0, 12))
        ttk.Label(head, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="选中的 JPEG → 对应的 RAW → 直接进 Lightroom",
                  style="Sub.TLabel").pack(side="left", padx=(12, 0), pady=(6, 0))

        card = ttk.Frame(root, style="Card.TFrame", padding=(14, 10, 14, 8))
        card.pack(fill="x")
        self.row_sel = PathLine(card, "①", "选片文件夹", self._browse_sel)
        self.row_sel.pack(fill="x", pady=3)
        self.row_card = PathLine(card, "②", "SD 卡 / DCIM", self._browse_card)
        self.row_card.pack(fill="x", pady=3)
        self.row_dest = PathLine(card, "③", "放到", self._browse_dest)
        self.row_dest.pack(fill="x", pady=3)

        opt = ttk.Frame(card, style="Card.TFrame")
        opt.pack(fill="x", padx=(34, 0), pady=(6, 2))
        self.auto_dest = tk.BooleanVar(value=bool(self.settings.get("auto_dest", True)))
        ttk.Checkbutton(opt, text="按拍摄日期自动确定文件夹", variable=self.auto_dest,
                        command=self._recompute_dest,
                        style="Card.TCheckbutton").pack(side="left")
        self.ingest_var = tk.BooleanVar(value=bool(self.settings.get("ingest", True)))
        ttk.Checkbutton(opt, text="登记到 Lightroom 目录（打开 LrC 就已导入）",
                        variable=self.ingest_var,
                        style="Card.TCheckbutton").pack(side="left", padx=(20, 0))
        ttk.Button(opt, text="格式化内存卡…", command=self._format_card,
                   style="Danger.TButton").pack(side="right")
        ttk.Button(opt, text="弹出内存卡", command=self._eject_card).pack(side="right", padx=(0, 8))

        bar = ttk.Frame(root)
        bar.pack(fill="x", pady=(10, 8))
        self.btn_scan = ttk.Button(bar, text="扫描配对", command=self._start_scan, width=13)
        self.btn_scan.pack(side="left")
        self.btn_run = ttk.Button(bar, text="开始执行", command=self._start_run,
                                  style="Go.TButton", width=13, state="disabled")
        self.btn_run.pack(side="left", padx=8)
        self.btn_stop = ttk.Button(bar, text="停止", command=self._stop, width=7, state="disabled")
        self.btn_stop.pack(side="left")
        more = ttk.Menubutton(bar, text="更多 ▾")
        menu = tk.Menu(more, tearoff=False)
        menu.add_command(label="修复已登记照片", command=self._repair)
        menu.add_command(label="恢复目录备份…", command=self._restore_backup)
        menu.add_separator()
        menu.add_command(label="使用说明…", command=self._show_help)
        menu.add_command(label="打开数据目录", command=self._open_data_dir)
        more["menu"] = menu
        more.pack(side="right")
        ttk.Button(bar, text="打开输出文件夹", command=self._open_dest).pack(side="right", padx=(0, 8))
        ttk.Button(bar, text="打开 Lightroom", command=self._open_lightroom).pack(side="right", padx=(0, 8))

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True)

        table_wrap = ttk.Frame(self.notebook, style="Card.TFrame", padding=(1, 1))
        self.notebook.add(table_wrap, text="  配对结果  ")
        cols = ("idx", "jpg", "raw", "method", "status")
        self.tree = ttk.Treeview(table_wrap, columns=cols, show="headings", height=8)
        heads = {"idx": ("#", 42), "jpg": ("JPEG 文件", 250), "raw": ("对应的 RAW", 250),
                 "method": ("配对方式", 96), "status": ("状态", 240)}
        for c in cols:
            self.tree.heading(c, text=heads[c][0])
            self.tree.column(c, width=heads[c][1], anchor="w",
                             stretch=(c in ("jpg", "raw", "status")))
        vs = ttk.Scrollbar(table_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.tag_configure("ok", foreground=FG)
        self.tree.tag_configure("bad", foreground=ERR)
        self.tree.tag_configure("warn", foreground=WARN)
        self.tree.bind("<Double-1>", self._manual_assign)

        # —— 卡内文件夹 ——
        card_tab = ttk.Frame(self.notebook, style="Card.TFrame", padding=(12, 8))
        self.notebook.add(card_tab, text="  卡内文件夹  ")
        head = ttk.Frame(card_tab, style="Card.TFrame")
        head.pack(fill="x", pady=(0, 6))
        self.card_summary = ttk.Label(head, text="插入 SD 卡后这里会列出 DCIM 下的文件夹",
                                      style="CardTitle.TLabel")
        self.card_summary.pack(side="left")
        ttk.Button(head, text="刷新", command=self._refresh_card_folders,
                   width=6).pack(side="right")
        ttk.Button(head, text="精确检查", command=lambda: self._refresh_card_folders(exact=True),
                   width=9).pack(side="right", padx=(0, 6))
        body = ttk.Frame(card_tab, style="Card.TFrame")
        body.pack(fill="both", expand=True)
        ccols = ("name", "count", "status")
        self.card_tree = ttk.Treeview(body, columns=ccols, show="headings", height=6)
        for c, (title, width) in {"name": ("DCIM 文件夹", 320),
                                  "count": ("照片数", 90),
                                  "status": ("状态", 220)}.items():
            self.card_tree.heading(c, text=title)
            self.card_tree.column(c, width=width, anchor="w",
                                  stretch=(c != "count"))
        cvs = ttk.Scrollbar(body, orient="vertical", command=self.card_tree.yview)
        self.card_tree.configure(yscrollcommand=cvs.set)
        self.card_tree.pack(side="left", fill="both", expand=True)
        cvs.pack(side="right", fill="y")
        self.card_tree.tag_configure("ok", foreground=OK)
        self.card_tree.tag_configure("bad", foreground=ERR)
        self.card_tree.tag_configure("empty", foreground=MUTED)
        self.card_tree.bind("<<TreeviewSelect>>", self._pick_card_folder)
        self._card_rows = []
        self._card_thread = None
        self._card_timer = None

        bottom = ttk.Frame(root)
        bottom.pack(fill="x", pady=(8, 0))
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.pack(fill="x")
        self.status = ttk.Label(bottom, text="准备就绪", style="Status.TLabel")
        self.status.pack(anchor="w", pady=(5, 0))

    # ------------------------------------------------------------ 启动
    def _bootstrap(self):
        self.row_sel.var.set(self.settings.get("selection_dir", ""))
        self.row_card.var.set(self.settings.get("card_folder", ""))
        if not self.settings.get("auto_dest", True):
            self.row_dest.var.set(self.settings.get("dest_dir", ""))

        if not self.row_card.var.get():
            cards = detect.find_cards()
            if cards:
                self.row_card.var.set(cards[0].dcim)
                self._card_count = len(cards)
        catalogs = detect.find_catalogs()
        if catalogs:
            self.catalog_path = self.settings.get("catalog") or catalogs[0]

        if not self.row_sel.var.get():
            lib_drive = os.path.splitdrive(self.catalog_path)[0][:1] if self.catalog_path else ""
            guess = detect.guess_selection_folder(lib_drive)
            if guess:
                self.row_sel.var.set(guess)

        self._recompute_dest()
        self.row_card.var.trace_add("write", self._schedule_card_refresh)
        self._refresh_card_folders()
        if self.row_card.var.get():
            try:
                self.notebook.select(1)
            except Exception:
                pass
        if self.catalog_path:
            self.log(f"Lightroom 目录：{os.path.basename(self.catalog_path)}")
        else:
            self.log("没有找到 Lightroom 目录，可以只复制文件。")

    def _set_hint(self, row, text):
        row.hint.configure(text=text)

    # ------------------------------------------------------------ 目标文件夹
    def _library(self):
        if not self.catalog_path or not os.path.exists(self.catalog_path):
            return None
        try:
            return open_library(self.catalog_path)
        except Exception:
            return None

    def _recompute_dest(self):
        if not self.auto_dest.get():
            return
        lib = self._library()
        when = None
        if self.items:
            dates = [i.jpg_meta.capture_time[:10] for i in self.items if i.jpg_meta.capture_time]
            if dates:
                when = Counter(dates).most_common(1)[0][0]
        if when is None:
            when = dt.date.today().strftime("%Y:%m:%d")
        try:
            y, m, d = when.split(":")
            when_d = dt.date(int(y), int(m), int(d))
        except Exception:
            when_d = dt.date.today()
        if not lib:
            self.row_dest.var.set(self.settings.get("dest_dir", ""))
            return
        root = lib.root_for(when_d.year)
        if root:
            base = root.absolute_path.replace("/", "\\")
            path = os.path.join(base, when_d.strftime("%Y-%m-%d"))
        else:
            # 这一年还没有图库根目录：放在最近一个根目录的上一层，写入时会自动建根目录
            parents = [os.path.dirname(r.absolute_path.replace("/", "\\")) for r in lib.roots]
            base = parents[-1] if parents else os.path.dirname(self.catalog_path)
            path = os.path.join(base, str(when_d.year), when_d.strftime("%Y-%m-%d"))
        self.row_dest.var.set(os.path.normpath(path))

    def _card_volume(self):
        path = self.row_card.var.get().strip()
        return removable.volume_of(path) if path else None

    def _library_drive(self) -> str:
        if not self.catalog_path:
            return ""
        return os.path.splitdrive(self.catalog_path)[0][:1]

    def _eject_card(self):
        if self.busy:
            return
        path = self.row_card.var.get().strip()
        ok, msg, vol = removable.check_target(path, self._library_drive())
        if not ok:
            messagebox.showwarning(APP_TITLE, msg)
            return
        if not messagebox.askokcancel(
                APP_TITLE,
                f"弹出这张卡？\n\n{vol.describe()}\n\n"
                "弹出后等系统提示再拔卡。若 Explorer 或 Lightroom 正开着卡上的文件夹，"
                "可能会弹出失败。"):
            return
        ok, msg = removable.eject(path)
        if ok:
            messagebox.showinfo(APP_TITLE, msg)
            self.row_card.var.set("")
        else:
            messagebox.showerror(APP_TITLE, msg)

    def _format_card(self):
        if self.busy:
            return
        path = self.row_card.var.get().strip()
        ok, msg, vol = removable.check_target(path, self._library_drive(), for_format=True)
        if not ok:
            messagebox.showwarning(APP_TITLE, msg)
            return
        count, size = removable.card_file_stats(path)
        if not messagebox.askokcancel(
                APP_TITLE,
                f"确定要格式化这张卡吗？\n\n{vol.describe()}\n"
                f"卡上现有 {count} 个文件，共 {copier.human(size)}\n\n"
                "⚠ 会清空卡上的全部内容，无法撤销。\n"
                "接下来系统会弹一次授权窗口，请点「是」。"):
            return

        log = os.path.join(config.app_dir(), "format.log")
        try:
            if os.path.exists(log):
                os.remove(log)
        except Exception:
            pass
        self._show_busy_dialog(
            f"正在格式化 {vol.letter}: …",
            f"文件系统 {vol.fs or 'exFAT'}，容量 {vol.total_gb:.0f} GB\n\n"
            "如果弹出「用户账户控制」，请点「是」，然后等这一步结束。")
        threading.Thread(target=self._format_worker, args=(vol, log), daemon=True).start()

    def _format_worker(self, vol, log):
        try:
            handle, err = removable.run_elevated(
                "powershell.exe", removable.format_params(vol, log))
            if not handle:
                if err == 1223:
                    self.queue.put(("format_done", False, "已取消授权，卡没有被格式化。"))
                else:
                    self.queue.put(("format_done", False, f"无法提权（错误码 {err}）。"))
                return
            code = removable.wait_process(handle, 300000)
            if code is None:
                self.queue.put(("format_done", False,
                                "等待超时：格式化可能还在进行，请到资源管理器里确认。"))
                return
            text = ""
            try:
                with open(log, "r", encoding="utf-8-sig") as f:
                    text = f.read().strip()
            except Exception:
                pass
            if code == 0 and text.startswith("OK"):
                self.queue.put(("format_done", True, f"{vol.letter}: 已格式化完成。"))
            else:
                detail = text[4:].strip() if text.startswith("ERR:") else (text or f"退出码 {code}")
                self.queue.put(("format_done", False, f"格式化失败：\n{detail}"))
        except Exception as exc:
            self.queue.put(("format_done", False, f"格式化失败：{exc}"))

    def _show_busy_dialog(self, title, note):
        top = tk.Toplevel(self)
        top.title("请稍候")
        top.transient(self)
        top.resizable(False, False)
        top.configure(bg="white", padx=26, pady=22)
        ttk.Label(top, text=title, style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(top, text=note, style="Hint.TLabel", justify="left").pack(anchor="w", pady=(8, 16))
        bar = ttk.Progressbar(top, mode="indeterminate", length=340)
        bar.pack()
        bar.start(12)
        top.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - top.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - top.winfo_reqheight()) // 2
        top.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        try:
            top.grab_set()
        except Exception:
            pass
        self._busy_dialog = top
        return top

    def _open_lightroom(self):
        exe = detect.find_lightroom()
        if not exe:
            messagebox.showinfo(APP_TITLE, "没找到 Lightroom 的安装位置，请手动打开。")
            return
        try:
            os.startfile(exe)
            self.status.configure(text="已启动 Lightroom")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"启动失败：{exc}")

    def _repair(self):
        if self.busy:
            return
        if not self.catalog_path:
            messagebox.showinfo(APP_TITLE, "还没找到 Lightroom 目录。")
            return
        if is_lightroom_running():
            messagebox.showwarning(APP_TITLE, "请先退出 Lightroom 再修复。")
            return
        if not messagebox.askokcancel(
                APP_TITLE,
                "为本软件登记过的照片补齐元数据（修复 Lightroom 保存元数据时报错的问题）。\n\n"
                "会先自动备份目录库，不会改动照片文件。继续吗？"):
            return
        self._set_busy(True, "正在修复…")

        def work():
            try:
                writer = CatalogWriter(
                    self.catalog_path,
                    progress=lambda m: self.queue.put(("log", m)))
                n = writer.repair()
                self.queue.put(("repaired", n, writer.backup_dir))
            except Exception as exc:
                self.queue.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _restore_backup(self):
        if not self.catalog_path:
            messagebox.showinfo(APP_TITLE, "还没找到 Lightroom 目录。")
            return
        if is_lightroom_running():
            messagebox.showwarning(APP_TITLE, "请先退出 Lightroom。")
            return
        backups = list_backups(self.catalog_path)
        if not backups:
            messagebox.showinfo(APP_TITLE, "还没有备份。")
            return
        latest = backups[0]
        if not messagebox.askokcancel(
                APP_TITLE,
                f"把目录恢复到这次备份？\n\n{latest}\n\n当前目录会被替换，建议先确认没有未保存的工作。"):
            return
        try:
            restore_backup(self.catalog_path, latest)
            messagebox.showinfo(APP_TITLE, "已恢复。")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"恢复失败：{exc}")

    # ------------------------------------------------------------ 浏览
    def _browse_sel(self):
        d = filedialog.askdirectory(title="选择放 JPEG 的选片文件夹",
                                    initialdir=self.row_sel.var.get() or "\\")
        if d:
            self.row_sel.var.set(os.path.normpath(d))
            self.items = []
            self._fill_tree()

    def _browse_card(self):
        d = filedialog.askdirectory(title="选择 SD 卡里的 DCIM 文件夹（或直接选卡根目录）",
                                    initialdir=self.row_card.var.get() or "\\")
        if d:
            self.row_card.var.set(os.path.normpath(d))

    def _browse_dest(self):
        d = filedialog.askdirectory(title="选择 RAW 要放到的文件夹",
                                    initialdir=self.row_dest.var.get() or "\\")
        if d:
            self.auto_dest.set(False)
            self.row_dest.var.set(os.path.normpath(d))

    def _open_data_dir(self):
        try:
            os.startfile(config.app_dir())
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"打不开：{exc}")

    def _open_dest(self):
        d = self.row_dest.var.get()
        if d and os.path.isdir(d):
            os.startfile(d)

    # ------------------------------------------------------------ 扫描
    def _start_scan(self):
        if self.busy:
            return
        sel = self.row_sel.var.get().strip()
        card = self.row_card.var.get().strip()
        if not sel or not os.path.isdir(sel):
            messagebox.showwarning(APP_TITLE, "请先选择选片文件夹。")
            return
        if not card or not os.path.isdir(card):
            messagebox.showwarning(APP_TITLE, "请先选择 SD 卡上的 DCIM 文件夹。")
            return
        self.stop_flag.clear()
        self.items = []
        self._fill_tree()
        self._set_busy(True, "正在扫描…")
        self.queue.put(("progress", 0, 0, "准备中"))

        def work():
            try:
                items, _idx = matcher.scan(
                    sel, [card],
                    progress=lambda stage, done, total: self.queue.put(("progress", done, total, stage)),
                    should_stop=self.stop_flag.is_set)
                self.queue.put(("scan_done", items))
            except Exception as exc:
                self.queue.put(("error", str(exc)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _fill_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, it in enumerate(self.items, 1):
            state = {"ok": "已配对", "missing": "找不到 RAW", "ambiguous": "多个候选，双击指定"}[it.status]
            tag = "ok" if it.status == "ok" else ("bad" if it.status == "missing" else "warn")
            self.tree.insert("", "end", iid=str(i - 1),
                             values=(i, it.jpg_name, it.raw_name or "—", it.method or "—", state),
                             tags=(tag,))

    def _pick_card_folder(self, _e=None):
        sel = self.card_tree.selection()
        if not sel:
            return
        try:
            st = self._card_rows[int(sel[0])]
        except Exception:
            return
        self.row_card.var.set(st.path)

    def _schedule_card_refresh(self, *_):
        if self._card_timer:
            try:
                self.after_cancel(self._card_timer)
            except Exception:
                pass
        self._card_timer = self.after(600, self._refresh_card_folders)

    def _refresh_card_folders(self, exact: bool = False):
        path = self.row_card.var.get().strip()
        if not path or not os.path.isdir(path):
            self.card_tree.delete(*self.card_tree.get_children())
            self._card_rows = []
            self.card_summary.configure(text="插入 SD 卡后这里会列出 DCIM 下的文件夹")
            return
        if self._card_thread and self._card_thread.is_alive():
            return
        self.card_summary.configure(
            text="正在精确检查（读每张的拍摄时间，稍慢）…" if exact else "正在检查卡上的文件夹…")
        self._card_thread = threading.Thread(target=self._card_worker, args=(path, exact),
                                             daemon=True)
        self._card_thread.start()

    def _card_worker(self, path, exact=False):
        try:
            rows = cardstatus.scan_card(
                path, self.catalog_path, exact=exact,
                progress=lambda m: self.queue.put(("card_progress", m)))
        except Exception:
            rows = []
        self.queue.put(("card_folders", rows))

    def _fill_card_tree(self, rows):
        self.card_tree.delete(*self.card_tree.get_children())
        self._card_rows = rows
        if not rows:
            self.card_summary.configure(text="没找到 DCIM 下的文件夹")
            return
        for i, st in enumerate(rows):
            if st.count == 0:
                tag = "empty"
            else:
                tag = "ok" if st.done else "bad"
            self.card_tree.insert("", "end", iid=str(i), tags=(tag,),
                                  values=(st.name, st.count, st.status_text))
        done = sum(1 for r in rows if r.done)
        self.card_summary.configure(
            text=f"共 {len(rows)} 个文件夹：{done} 个已拷贝，{len(rows) - done} 个未拷贝")

    def _manual_assign(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        it = self.items[int(sel[0])]
        start = self.row_card.var.get() or "\\"
        p = filedialog.askopenfilename(
            title=f"为 {it.jpg_name} 指定对应的 RAW",
            initialdir=start,
            filetypes=[("RAW 文件", " ".join("*" + e for e in sorted(detect.RAW_EXT))), ("所有文件", "*.*")])
        if p:
            it.raw_path = os.path.normpath(p)
            it.method = "手动指定"
            it.status = "ok"
            it.size = os.path.getsize(p)
            self._fill_tree()
            self._refresh_buttons()

    # ------------------------------------------------------------ 执行
    def _start_run(self):
        if self.busy:
            return
        todo = [i for i in self.items if i.status == "ok" and i.raw_path]
        if not todo:
            messagebox.showinfo(APP_TITLE, "没有可以执行的照片，请先扫描。")
            return

        dest = self.row_dest.var.get().strip()
        if not dest:
            messagebox.showwarning(APP_TITLE, "请先指定输出文件夹。")
            return

        total_bytes = sum(i.size for i in todo)
        conflicts = []
        for i in todo:
            target = os.path.join(dest, i.raw_name)
            if os.path.exists(target) and os.path.getsize(target) != i.size:
                conflicts.append(i.raw_name)

        msg = (f"共 {len(todo)} 张 RAW，合计 {copier.human(total_bytes)}。\n\n"
               f"输出到：\n{dest}\n\n")
        if self.ingest_var.get():
            if is_lightroom_running():
                messagebox.showwarning(
                    APP_TITLE,
                    "Lightroom 正在运行。写入目录前必须完全退出 Lightroom。\n\n"
                    "请先关闭 Lightroom，再回来点「开始执行」。")
                return
            msg += "执行后会把这些照片登记到 Lightroom 目录，打开 LrC 就是已导入状态。\n"
            msg += "写入前会自动备份目录文件。\n"
        if conflicts:
            msg += f"\n注意：有 {len(conflicts)} 个同名文件已存在且大小不同，将跳过（不覆盖）。\n"
        if not messagebox.askokcancel(APP_TITLE, msg + "\n确定开始吗？"):
            return

        free = copier.free_bytes(dest if os.path.isdir(dest) else os.path.dirname(dest))
        if free and free < total_bytes * 1.02:
            messagebox.showwarning(APP_TITLE, f"目标盘剩余空间不足（{copier.human(free)}）。")
            return

        self.stop_flag.clear()
        self._set_busy(True, "正在复制…")
        ingest_on = bool(self.ingest_var.get())

        def work():
            copied, skipped, failed = [], [], []
            done_bytes = 0
            try:
                try:
                    os.makedirs(dest, exist_ok=True)
                    probe = os.path.join(dest, ".rawmate_write_test")
                    with open(probe, "w") as _f:
                        _f.write("ok")
                    os.remove(probe)
                except OSError as exc:
                    raise RuntimeError(
                        f"无法创建输出文件夹：\n{dest}\n\n"
                        f"系统提示：{exc}\n\n"
                        "多半是软件被以受限权限启动了。请直接在「资源管理器」里双击 exe 运行；\n"
                        "或者先在资源管理器里手动建好这个日期文件夹，再回来点执行。")
                for n, it in enumerate(todo, 1):
                    if self.stop_flag.is_set():
                        break
                    row_idx = self.items.index(it)
                    target = os.path.join(dest, it.raw_name)
                    if os.path.exists(target):
                        if os.path.getsize(target) == it.size:
                            it.action = "skipped"
                            skipped.append(it)
                            self.queue.put(("row", row_idx, it))
                            continue
                        it.action = "skipped"
                        it.note = (it.note + "；" if it.note else "") + "目标已有同名但大小不同的文件，未覆盖"
                        skipped.append(it)
                        self.queue.put(("row", row_idx, it))
                        continue
                    self.queue.put(("progress", done_bytes, total_bytes,
                                    f"复制 {n}/{len(todo)}：{it.raw_name}"))

                    def cb(delta, _it=it, _n=n):
                        nonlocal done_bytes
                        done_bytes += delta
                        self.queue.put(("progress", done_bytes, total_bytes,
                                        f"复制 {_n}/{len(todo)}：{_it.raw_name}"))

                    copier.copy_file(it.raw_path, target, cb, self.stop_flag.is_set)
                    it.action = "copied"
                    copied.append(it)
                    self.queue.put(("row", row_idx, it))

                # 无论这次是复制还是之前就存在，只要文件在位就登记进目录
                present = []
                for it in todo:
                    target = os.path.join(dest, it.raw_name)
                    if os.path.exists(target) and os.path.getsize(target) == it.size:
                        present.append(target)

                backup_dir, added, dup, ing_fail = "", 0, 0, []
                if ingest_on and present:
                    self.queue.put(("progress", total_bytes, total_bytes,
                                    "正在写入 Lightroom 目录…"))
                    writer = CatalogWriter(self.catalog_path,
                                           progress=lambda m: self.queue.put(("log", m)))
                    res = writer.ingest(present, dest)
                    backup_dir = res.backup_dir
                    added, dup = res.ok, len(res.skipped)
                    ing_fail = [(os.path.basename(p), e) for p, e in res.failed]
                    failed.extend(ing_fail)
                self.queue.put(("done", copied, skipped, failed, dest, backup_dir, added, dup))
            except Exception as exc:
                import traceback
                if isinstance(exc, RuntimeError):
                    self.queue.put(("error", str(exc)))
                else:
                    self.queue.put(("error", f"{exc}\n\n{traceback.format_exc()}"))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _stop(self):
        self.stop_flag.set()
        self.status.configure(text="正在停止…")

    # ------------------------------------------------------------ 队列
    def _poll(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    done, total, stage = msg[1], msg[2], msg[3]
                    if total:
                        self.progress["value"] = min(100, done * 100 / total)
                    self.status.configure(text=stage)
                elif kind == "log":
                    self.log(msg[1])
                elif kind == "row":
                    self._update_row(msg[1], msg[2])
                elif kind == "scan_done":
                    self._on_scan_done(msg[1])
                elif kind == "done":
                    self._on_done(*msg[1:])
                elif kind == "card_progress":
                    self.card_summary.configure(text=msg[1])
                elif kind == "card_folders":
                    self._fill_card_tree(msg[1])
                elif kind == "repaired":
                    self._set_busy(False, "")
                    n, backup = msg[1], msg[2]
                    messagebox.showinfo(
                        APP_TITLE,
                        f"修复完成：{n} 张照片的元数据已补齐。\n\n备份：{backup}\n\n"
                        "现在打开 Lightroom 再试一次编辑，应该不会再报写入元数据失败。")
                elif kind == "format_done":
                    dlg = getattr(self, "_busy_dialog", None)
                    if dlg is not None:
                        try:
                            dlg.grab_release()
                            dlg.destroy()
                        except Exception:
                            pass
                        self._busy_dialog = None
                    success, text = msg[1], msg[2]
                    if success:
                        self.status.configure(text=text)
                        messagebox.showinfo(APP_TITLE, text)
                    else:
                        messagebox.showerror(APP_TITLE, text)
                elif kind == "error":
                    self._set_busy(False, "出错了")
                    messagebox.showerror(APP_TITLE, msg[1])
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _update_row(self, idx: int, it):
        iid = str(idx)
        if not self.tree.exists(iid):
            return
        state = "已复制" if it.action == "copied" else ("跳过" if it.action == "skipped" else "失败")
        self.tree.set(iid, "status", state)

    def _on_scan_done(self, items):
        self.items = items
        self._fill_tree()
        self._set_busy(False, "")
        ok = sum(1 for i in items if i.status == "ok")
        bad = len(items) - ok
        self._recompute_dest()
        try:
            self.notebook.select(0)
        except Exception:
            pass
        self.status.configure(
            text=f"扫描完成：{len(items)} 张，已配对 {ok} 张，待处理 {bad} 张"
                 + ("（双击红色行可手动指定 RAW）" if bad else ""))
        self._refresh_buttons()
        if bad:
            misses = [i for i in items if i.status != "ok"]
            sample = "\n".join(f"· {i.jpg_name}" for i in misses[:8])
            more = "" if len(misses) <= 8 else f"\n… 还有 {len(misses) - 8} 张"
            messagebox.showinfo(
                APP_TITLE,
                f"有 {bad} 张没找到对应的 RAW：\n\n{sample}{more}\n\n"
                "可以在表格里双击这些行，手动指定 RAW 文件。")

    def _on_done(self, copied, skipped, failed, dest, backup_dir, added, dup):
        self._set_busy(False, "")
        self.progress["value"] = 100
        self.settings["success_count"] = self.settings.get("success_count", 0) + len(copied)
        self.settings["selection_dir"] = self.row_sel.var.get()
        self.settings["card_folder"] = self.row_card.var.get()
        self.settings["dest_dir"] = self.row_dest.var.get()
        self.settings["auto_dest"] = self.auto_dest.get()
        self.settings["ingest"] = self.ingest_var.get()
        self.settings["catalog"] = self.catalog_path
        config.save(self.settings)

        try:
            txt, _csv = config.write_report(self.items, dest)
        except Exception:
            txt = ""
        lines = [f"处理 {len(copied) + len(skipped)} 张：复制 {len(copied)}，跳过 {len(skipped)}"]
        if failed:
            lines.append(f"失败 {len(failed)} 张")
            for name, err in failed[:5]:
                lines.append(f"  · {name}：{err}")
        if backup_dir:
            lines.append(f"已登记到 Lightroom 目录 {added} 张"
                         + (f"，{dup} 张本来就在目录里" if dup else ""))
            lines.append(f"\n目录备份：{backup_dir}")
        lines.append(f"\n输出：{dest}")
        if txt:
            lines.append(f"报告：{txt}")
        if self.ingest_var.get():
            lines.append("\n现在打开 Lightroom，左边就能看到这些照片了。")
        self.status.configure(text=f"完成：复制 {len(copied)}，跳过 {len(skipped)}，失败 {len(failed)}")
        messagebox.showinfo(APP_TITLE, "\n".join(lines))

    # ------------------------------------------------------------ 杂项
    def _set_busy(self, busy: bool, text: str):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.btn_scan.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")
        self._refresh_buttons()
        if text:
            self.status.configure(text=text)
        if not busy:
            self.progress["value"] = 0

    def _refresh_buttons(self):
        if self.busy:
            self.btn_run.configure(state="disabled")
            return
        ok = any(i.status == "ok" and i.raw_path for i in self.items)
        self.btn_run.configure(state="normal" if ok else "disabled")

    def log(self, text: str):
        self.status.configure(text=text)


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = App()
    app.mainloop()
