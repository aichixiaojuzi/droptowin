# _tools 目录文件说明

> 每个文件是干什么的、哪些能删、哪些别动。排错先看最下面"运行产物"一节。

## 代码与安装（6 个）

| 文件 | 作用 |
|---|---|
| `droptowin.py` | **服务主程序**（单文件，约 2000 行）：HTTP 服务 0.0.0.0:8899、网页界面、剪贴板读写（ctypes）、文件落盘、快捷指令配置页 |
| `watchdog.py` | **守护进程**：每 30 秒探测 8899 端口，服务挂了自动拉起；用 8897 端口做单实例锁，重复启动直接退出 |
| `start.vbs` | **手动启动入口**（双击无窗口）：自动在 `%LOCALAPPDATA%\Programs\Python`、`Program Files`、`C:\Python*` 里找最新的 `pythonw.exe`（找不到再退回 PATH），然后拉起 watchdog.py。文件保持纯 ASCII + CRLF 是刻意的——WSH 按 GBK 读 .vbs，混入中文会编译报错 |
| `install.bat` | **安装入口**（双击）：调 install.ps1。支持 `install.bat -DryRun`（只看不动）和 `-Lang en/zh`（强制界面语言） |
| `install.ps1` | **安装的实际逻辑**：建数据目录（收件箱/文本/发给我）、写 config、放行防火墙 TCP 8899、配开机自启、首次启动服务 |
| `allow-firewall.bat` | **单独放行防火墙 TCP 8899**（备用）。正常安装时已自动做，只有防火墙规则丢了才需要手动跑 |

## 配置（2 个）

| 文件 | 作用 |
|---|---|
| `config.json` | **本机配置**：数据目录 `base_dir`、端口 `port`、HEIC 转 JPG 开关、两条快捷指令的中英链接（`url` 中文页用 / `url_en` 英文页用）。含个人信息，**不进仓库**。改完需重启服务（启动时只读一次） |
| `config.example.json` | **仓库里的配置模板**：新用户复制成 `config.json` 再改 |

## 运行产物（4 个，删了会自动重建）

| 文件 | 作用 | 排错用法 |
|---|---|---|
| `access.log` | HTTP 访问日志，每次请求一行，超 500KB 自动轮转 | "传了但电脑没收到"：看对应时间有没有 `POST /api/file`。有记录 → 去 `收件箱\` 找；没记录 → 手机没发出去，查快捷指令里的 IP |
| `watchdog.log` | 守护进程日志：拉起服务的记录 | "服务怎么老不在"：看这里有没有反复「检测到服务不在，正在拉起」 |
| `droptowin.out` | 服务崩溃输出，正常为空 | **服务起不来第一时间看这里**，崩溃时有真实 traceback。2026-11-24 的 GBK 启动崩溃就是靠它定位的 |
| `sync_state.json` | 剪贴板同步状态：手机/电脑两侧内容的哈希 | 用来判断"哪边是新内容"。删掉只会导致下次同步按"两边都新"处理（以手机为准） |

## 快速记忆

- **前 6 个是代码/安装**：日常别动；`config.json` 只有加改快捷指令链接时才碰
- **后 6 个是配置和产物**：`config.json` 别删；4 个运行产物随时可删
- 服务不响应的标准排查顺序：`droptowin.out` → `watchdog.log` → `access.log` → 双击 `allow-firewall.bat`

## 已知的编码坑（改代码前看）

- 中文 Windows 下 stdout/stderr 默认 GBK。`droptowin.py` 文件头已把输出流重配为 UTF-8
  （errors=replace），`watchdog.py` 拉起子进程时也加了 `PYTHONIOENCODING=utf-8` 双保险——
  新加 print 含特殊字符也不会崩，但别删这两处
- `.vbs` 必须纯 ASCII + CRLF，原因见上表 start.vbs 行
- `config.json` 读取用 `utf-8-sig`（兼容 BOM），保存保持 UTF-8 即可
