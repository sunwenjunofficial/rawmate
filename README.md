# RAW 配对助手（rawmate）

把 SD 卡里的 **RAW + JPG** 按拍摄信息配成对，安全拷贝到选片目录，并同步进 Lightroom 目录。
面向「拍 RAW+JPG 双格式、后期只用 Lightroom、每次拷卡都怕漏文件」的场景。

**零第三方依赖**：纯 Python 标准库 + tkinter，拷到任何装了 Python 3.9+ 的 Windows 机器上就能跑。

## 它解决什么问题

拍完回来，卡里同一张照片有两个文件：`DSC01234.ARW` 和 `DSC01234.JPG`。
网上的拷卡工具要么只认文件名、改名后就配不上，要么拷到一半中断留下半截文件，要么不认 Lightroom。
这个工具的三条底线：

- **配对要准**：先按文件名配；配不上的再用「拍摄时间 + 毫秒」反查，改名过的也能找回来
- **拷贝要稳**：先写临时文件、校验大小、再原子改名，**绝不留下半截文件**
- **状态要清**：能判断卡上哪些 DCIM 文件夹已经拷过，不会重复拷、也不会漏拷

## 功能

| 模块 | 做什么 |
|---|---|
| 配对 | 文件名优先，失败回退到「拍摄时间 + 毫秒」反查 |
| 拷卡 | 临时文件 → 校验大小 → 原子改名；支持只拷 RAW、只拷 JPG、或成对拷 |
| 卡状态 | 按 DCIM 文件夹记录已拷状态，重复插卡不会重复处理 |
| EXIF | 纯标准库解析，支持 JPEG 与 TIFF 系 RAW（ARW / NEF / CR2 / DNG / ORF / PEF / SRW） |
| Lightroom | 直接读写 `.lrcat`（SQLite），生成 Lightroom 内部 XMP 包 |
| 磁盘 | 读卷信息、弹出 SD 卡、格式化 |
| 界面 | tkinter 图形界面，带进度与日志 |

## 运行

```bash
python -m rawmate
```

或在项目上级目录：

```bash
python -c "from rawmate.ui import main; main()"
```

拷卡状态与报告默认写到 `%APPDATA%\RAW配对助手`，可用环境变量 `RAWMATE_HOME` 改到别处。

## 代码结构

```
rawmate/
  ui.py         图形界面（tkinter）与主流程
  matcher.py    配对引擎
  copier.py     复制与校验（原子写入）
  exif.py       EXIF 解析（JPEG / TIFF 系 RAW）
  catalog.py    Lightroom .lrcat 读写
  xmp.py        Lightroom 内部 XMP 生成
  detect.py     盘符/文件夹识别，找卡、找目录
  cardstatus.py DCIM 已拷状态
  removable.py  卷信息、弹出、格式化
  config.py     本地设置与报告输出
```

## 环境

- Windows 10/11
- Python 3.9+（需自带 tkinter，官方安装包默认包含）

## 版本

`1.0.0`
