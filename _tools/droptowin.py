#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
随手传 DropToWin - iPhone <-> PC 局域网互传
--------------------------------
用途：iPhone Safari 打开 http://<电脑IP>:8899
     1) 粘贴文本 -> 直接写进 Windows 剪贴板（同时存档到 文本/ 目录）
     2) 上传图片/文件 -> 直接落到 收件箱/ 目录
     3) 浏览/下载电脑上共享目录里的文件

核心零依赖，只用 Python 标准库。两个可选依赖：
  Pillow       读电脑剪贴板里的图片
  pillow-heif  把 iPhone 的 HEIC 转成 JPG（微信「原图」也走这条路）
没装也能跑，只是对应功能自动关闭。Ctrl+C 停止。
"""

VERSION = "1.0.0"

import base64
import ctypes
import datetime
import hashlib
import html
import http.server
import io
import json
import mimetypes
import os
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse

# 中文 Windows 下 stdout/stderr 默认是 GBK，启动横幅里的 "⇄" 等字符会直接
# UnicodeEncodeError 让服务秒崩（被 pythonw/wd 拉起时表现为"无法启动"）。
# 统一重配为 UTF-8，编码不了的字符用 replace 兜底，print 永不再炸。
for _s in (sys.stdout, sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# 可选依赖：pillow-heif。注册后 Pillow 就能直接 Image.open 一个 HEIC。
# 没装（或装坏了）不影响任何核心功能，只关掉 HEIC→JPG 这一步。
try:
    import pillow_heif as _pillow_heif

    _pillow_heif.register_heif_opener()
    HEIC_SUPPORT = True
except Exception:
    HEIC_SUPPORT = False

PORT = 8899
# 打包成 exe 后 __file__ 指向临时解压目录，要用 sys.executable 才拿得到真实位置。
# Nuitka 走 __compiled__、PyInstaller 走 sys.frozen，两个都认。
if getattr(sys, "frozen", False) or "__compiled__" in globals():
    HERE = os.path.dirname(os.path.abspath(sys.executable))
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
# 数据目录 = 程序目录(_tools)的上一级。整个项目文件夹拷到任何位置都能直接跑，
# 不用改代码。想自定义位置，就在同级放一个 config.json：{"base_dir": "D:\\xxx"}
BASE_DIR = os.path.dirname(HERE)

CFG = {}

try:
    _cfg_path = os.path.join(HERE, "config.json")
    if os.path.isfile(_cfg_path):
        with open(_cfg_path, "r", encoding="utf-8-sig") as _f:
            _cfg = json.load(_f)
        if isinstance(_cfg, dict):
            CFG = _cfg
            if _cfg.get("base_dir"):
                BASE_DIR = str(_cfg["base_dir"])
            if _cfg.get("port"):
                PORT = int(_cfg["port"])
except Exception:
    pass

INBOX_DIR = os.path.join(BASE_DIR, "收件箱")
TEXT_DIR = os.path.join(BASE_DIR, "文本")
SHARE_DIRS = [INBOX_DIR, TEXT_DIR, os.path.join(BASE_DIR, "发给我")]

for d in SHARE_DIRS:
    os.makedirs(d, exist_ok=True)

# ---------------------------------------------------------------- 剪贴板

def _open_clipboard() -> bool:
    """剪贴板同一时间只能被一个进程打开，资源管理器/输入法常抢，重试几次。"""
    u32 = ctypes.windll.user32
    for _ in range(8):
        if u32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def set_clipboard_text(text: str) -> bool:
    """把文本写进 Windows 剪贴板（Unicode）。"""
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    GMEM_ZEROINIT = 0x0040

    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32

    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k32.GlobalFree.argtypes = [ctypes.c_void_p]
    u32.OpenClipboard.argtypes = [ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p
    u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    if not u32.OpenClipboard(None):
        return False
    try:
        u32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        handle = k32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, size)
        if not handle:
            return False
        ptr = k32.GlobalLock(handle)
        if not ptr:
            k32.GlobalFree(handle)
            return False
        ctypes.memmove(ptr, buf, size)
        k32.GlobalUnlock(handle)
        if not u32.SetClipboardData(CF_UNICODETEXT, handle):
            k32.GlobalFree(handle)
            return False
        return True
    finally:
        u32.CloseClipboard()


def append_text_log(text: str):
    name = datetime.datetime.now().strftime("%Y-%m-%d") + ".md"
    path = os.path.join(TEXT_DIR, name)
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n--- {stamp} ---\n{text}\n")


def get_clipboard_text():
    """读取 Windows 剪贴板里的文本，没有则返回 None。"""
    CF_UNICODETEXT = 13
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32

    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u32.GetClipboardData.restype = ctypes.c_void_p
    u32.GetClipboardData.argtypes = [ctypes.c_uint]

    if not _open_clipboard():
        return None
    try:
        handle = u32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = k32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            k32.GlobalUnlock(handle)
    finally:
        u32.CloseClipboard()


def get_clipboard_files():
    """读剪贴板里被复制的文件/文件夹路径（资源管理器 Ctrl+C）。只读路径，不读内容。"""
    CF_HDROP = 15
    u32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32

    shell32.DragQueryFileW.restype = ctypes.c_uint
    shell32.DragQueryFileW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint,
    ]
    u32.GetClipboardData.restype = ctypes.c_void_p
    u32.GetClipboardData.argtypes = [ctypes.c_uint]
    u32.OpenClipboard.argtypes = [ctypes.c_void_p]

    if not _open_clipboard():
        return []
    try:
        handle = u32.GetClipboardData(CF_HDROP)
        if not handle:
            return []
        count = shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
        if count <= 0 or count > 500:
            return []
        paths = []
        for i in range(count):
            need = shell32.DragQueryFileW(handle, i, None, 0)
            if need <= 0:
                continue
            buf = ctypes.create_unicode_buffer(need + 1)
            shell32.DragQueryFileW(handle, i, buf, need + 1)
            if buf.value:
                paths.append(buf.value)
        return paths
    finally:
        u32.CloseClipboard()


_clip_lock = threading.Lock()
_clip_cache = {"seq": -1, "png": b"", "size": (0, 0), "files": [], "text": ""}


def clipboard_snapshot():
    """剪贴板快照。核心思想：先用序列号判断有没有变化，变了才真正去读。

    - 没变化 -> 直接返回缓存，一次轮询只花一个计数器调用的时间
    - 变了   -> 只读"文字""路径字符串"或"图片像素"，绝不主动读文件内容
    - 文件   -> 只有手机点下载时才会真的读字节并流式发送
    """
    try:
        seq = ctypes.windll.user32.GetClipboardSequenceNumber()
    except Exception:
        seq = 0

    with _clip_lock:
        if _clip_cache["seq"] == seq:
            return _clip_cache

    text = get_clipboard_text() or ""
    png, size, files = b"", (0, 0), []

    paths = get_clipboard_files()
    if paths:
        for p in paths[:20]:
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append({
                "name": os.path.basename(p.rstrip("\\/")) or p,
                "path": p,
                "bytes": st.st_size,
                "size": human_size(st.st_size),
                "is_dir": os.path.isdir(p),
            })
    else:
        try:
            from PIL import ImageGrab

            got = ImageGrab.grabclipboard()
            if got is not None and hasattr(got, "save"):
                buf = io.BytesIO()
                got.save(buf, format="PNG")
                png = buf.getvalue()
                size = got.size
        except Exception:
            pass

    with _clip_lock:
        _clip_cache.update({
            "seq": seq, "png": png, "size": size, "files": files, "text": text,
        })
        return _clip_cache

# ---------------------------------------------------------------- 双向同步

SYNC_STATE_PATH = os.path.join(HERE, "sync_state.json")
_sync_lock = threading.Lock()
_sync_state = {"pc_hash": "", "phone_hash": ""}


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _load_sync_state():
    try:
        with open(SYNC_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _sync_state["pc_hash"] = str(data.get("pc_hash") or "")
            _sync_state["phone_hash"] = str(data.get("phone_hash") or "")
    except Exception:
        pass


def _save_sync_state():
    try:
        with open(SYNC_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(_sync_state, f)
    except Exception:
        pass


def _record_sync(final_text: str):
    """同步完成后两边内容一致，把双方 hash 都记成同一个。"""
    h = _hash(final_text)
    with _sync_lock:
        _sync_state["pc_hash"] = h
        _sync_state["phone_hash"] = h
    _save_sync_state()


def sync_clipboard(phone_text: str):
    """双向同步裁决。返回 (手机剪贴板最终应该变成的内容, action)。

    判断依据是"上次同步时两边各是什么"，而不是时间戳 —— iOS 拿不到剪贴板的
    修改时间，只能靠差分：谁跟上次不一样，就是谁改了。
    """
    pc_text = get_clipboard_text() or ""
    pc_hash = _hash(pc_text)
    phone_hash = _hash(phone_text)

    if pc_hash == phone_hash:
        _record_sync(pc_text)
        return pc_text, "same"

    # 手机剪贴板为空时绝不推送，否则会把电脑剪贴板清空
    if not phone_text.strip():
        _record_sync(pc_text)
        return pc_text, "pull"

    with _sync_lock:
        last_pc = _sync_state["pc_hash"]
        last_phone = _sync_state["phone_hash"]

    phone_changed = (not last_phone) or (phone_hash != last_phone)
    pc_changed = (not last_pc) or (pc_hash != last_pc)

    if phone_changed and not pc_changed:
        action = "push"
    elif pc_changed and not phone_changed:
        action = "pull"
    else:
        # 两边都变了（或首次同步）：用户正拿着手机操作，默认按"发送"处理。
        # 想从电脑拉到手机，手机就别先改，保持上次同步时的内容即可。
        action = "push"

    if action == "push":
        # 电脑原来的内容会被覆盖，先存档，方便找回
        if pc_text.strip() and pc_hash != last_pc:
            append_text_log(f"[被手机覆盖前，电脑剪贴板内容]\n{pc_text}")
        if set_clipboard_text(phone_text):
            append_text_log(phone_text)
            _record_sync(phone_text)
            file_log(f"    << 同步 手机->电脑 ({len(phone_text)} 字)")
            return phone_text, "push"
        # 写不进电脑剪贴板（被别的程序占着），退化成拉取
        _record_sync(pc_text)
        return pc_text, "pull"

    # 手机会被电脑内容覆盖，但如果手机里有上次同步之后新复制的东西，先存档防丢
    if phone_text.strip() and phone_hash != last_phone and phone_hash != pc_hash:
        append_text_log(f"[被电脑覆盖前，手机剪贴板内容]\n{phone_text}")

    _record_sync(pc_text)
    file_log(f"    << 同步 电脑->手机 ({len(pc_text)} 字)")
    return pc_text, "pull"


# ---------------------------------------------------------------- 工具

BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(name: str) -> str:
    name = os.path.basename((name or "").replace("\\", "/"))
    name = BAD_CHARS.sub("_", name).strip().strip(".")
    return name[:120] or "未命名"


def sniff_ext(path: str) -> str:
    """靠文件头魔数判断真实类型，给没有扩展名的文件补上。比 Content-Type 可靠。"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return ""

    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:4] == b"GIF8":
        return ".gif"
    if head[:4] == b"%PDF":
        return ".pdf"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"mif1", b"msf1"):
            return ".heic"
        if brand in (b"avif", b"avis"):
            return ".avif"
        if brand == b"qt  ":
            return ".mov"
        return ".mp4"
    if head[:4] == b"PK\x03\x04":
        # zip 容器，再细分 Office 格式
        try:
            import zipfile

            with zipfile.ZipFile(path) as z:
                names = z.namelist()
            if any(n.startswith("word/") for n in names):
                return ".docx"
            if any(n.startswith("xl/") for n in names):
                return ".xlsx"
            if any(n.startswith("ppt/") for n in names):
                return ".pptx"
        except Exception:
            pass
        return ".zip"
    if head[:4] == b"\xd0\xcf\x11\xe0":
        return ".doc"
    return ""


def unique_path(folder: str, name: str) -> str:
    target = os.path.join(folder, name)
    if not os.path.exists(target):
        return target
    stem, ext = os.path.splitext(name)
    i = 2
    while True:
        cand = os.path.join(folder, f"{stem} ({i}){ext}")
        if not os.path.exists(cand):
            return cand
        i += 1


def fix_ext(folder: str, target: str) -> str:
    """文件没扩展名就按魔数补一个，返回（可能改名后的）路径。

    iOS 相册和微信分享出来的文件经常连扩展名都没有，Content-Type 又不可信，
    只能读文件头。改名失败就保持原样，不打断上传。
    """
    if os.path.splitext(target)[1]:
        return target
    guess = sniff_ext(target)
    if not guess:
        return target
    renamed = unique_path(folder, os.path.basename(target) + guess)
    try:
        os.replace(target, renamed)
        return renamed
    except OSError:
        return target


def heic_to_jpg(target: str) -> str:
    """给落盘的 HEIC/HEIF 另存一份同名 JPG，返回 JPG 路径；没转成就返回 ""。

    为什么放在服务端：微信「原图」发出来的照片，到快捷指令手里时类型是「文件」、
    文件名连扩展名都没有（实测 name=4281），指令里没法判断，只能等落盘后用
    魔数认出 .heic 再转。相册直传的照片快捷指令已转好，不会走到这里。

    - 原文件保留：只是多一份，删不删由用户决定
    - 同名 JPG 已存在（重传）就加 (2)，绝不覆盖已有文件
    - 任何失败都只写 file_log，不影响上传本身
    """
    if not HEIC_SUPPORT or not CFG.get("heic_to_jpg", True):
        return ""
    stem, ext = os.path.splitext(target)
    if ext.lower() not in (".heic", ".heif"):
        return ""

    out = ""
    try:
        from PIL import Image, ImageOps

        with Image.open(target) as im:
            im = ImageOps.exif_transpose(im)  # 按 EXIF 方向摆正，不然手机竖拍会躺着
            if im.mode != "RGB":
                im = im.convert("RGB")  # HEIC 可能带透明通道，JPEG 存不了
            out = unique_path(os.path.dirname(target), os.path.basename(stem) + ".jpg")
            im.save(out, "JPEG", quality=92)
        file_log(f"    .. heic→jpg {os.path.basename(out)} ({human_size(os.path.getsize(out))})")
        return out
    except Exception as exc:  # noqa: BLE001
        # 写到一半失败会留下半张图，删掉，别让用户以为转换成功了
        if out and os.path.exists(out):
            try:
                os.remove(out)
            except OSError:
                pass
        file_log(f"    !! heic→jpg 失败 {os.path.basename(target)}: {exc}")
        return ""


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


# ---------------------------------------------------------------- 到达提醒

LOG_PATH = os.path.join(HERE, "access.log")


def file_log(line: str):
    """请求日志落盘，带简单轮转。pythonw 无控制台，只能靠文件排错。"""
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 500_000:
            with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                tail = f.readlines()[-300:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {line}\n")
    except OSError:
        pass


_alert_lock = threading.Lock()
_alert_state = {"count": 0, "last": "", "stamp": 0.0}


_reveal_state = {"time": 0.0, "folder": ""}


def _reveal(path: str):
    """在资源管理器里打开并选中该文件。同一文件夹 10 秒内只开一次窗口。"""
    if not os.path.exists(path):
        return
    folder = os.path.dirname(os.path.abspath(path))
    now = time.time()
    if _reveal_state["folder"] == folder and (now - _reveal_state["time"]) < 10:
        file_log("    .. 跳过开窗（本批已开过）")
        return
    _reveal_state.update({"time": now, "folder": folder})
    file_log(f"    .. 开窗定位 {os.path.basename(path)}")
    try:
        subprocess.Popen(
            f'explorer /select,"{path}"',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception:
        pass


# 提醒的合并窗口。调小 = 通知更及时，但批量传输时会连弹几条；
# 窗口只靠下面的"同文件夹 10 秒内只开一次"保证不重复。
ALERT_QUIET = 1.2


def schedule_alert(path: str):
    """排队一次到达处理。只负责开一个文件夹窗口，不弹系统通知。"""
    with _alert_lock:
        _alert_state["count"] += 1
        _alert_state["last"] = path
        _alert_state["stamp"] = time.time()
    t = threading.Timer(ALERT_QUIET, _fire_alert)
    t.daemon = True
    t.start()


def _fire_alert():
    with _alert_lock:
        if time.time() - _alert_state["stamp"] < 1.0:
            return  # 还有新文件在进来，交给后面那轮处理
        count = _alert_state["count"]
        last = _alert_state["last"]
        _alert_state["count"] = 0
        _alert_state["last"] = ""
        _alert_state["stamp"] = 0.0

    if not last:
        return

    if count > 1:
        file_log(f"    .. 本批 {count} 个文件")

    # 到达反馈就是弹一个资源管理器窗口并选中文件；不弹系统通知。
    if CFG.get("open_folder", True):
        _reveal(last)


def render_setup_page(ip: str, port: int) -> str:
    """把 config.json 里的 shortcuts 渲染成安装按钮。"""
    items = CFG.get("shortcuts") or []
    parts = []
    for idx, it in enumerate(items, 1):
        if not isinstance(it, dict):
            continue
        name = html.escape(str(it.get("name") or "快捷指令"))
        url = str(it.get("url") or "").strip()
        desc = html.escape(str(it.get("note") or ""))
        # name_en / note_en 可选：填了就支持英文界面，不填则回退到中文
        name_en = html.escape(str(it.get("name_en") or it.get("name") or ""), quote=True)
        desc_en = html.escape(str(it.get("note_en") or it.get("note") or ""), quote=True)
        # url_en 可选：英文页面指到英文版指令的链接，不填则和中文共用同一个 url
        url_en = str(it.get("url_en") or url).strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        parts.append(
            '<a class="linkbtn" href="{0}" data-href-zh="{0}" data-href-en="{1}" '
            'target="_blank" rel="noopener">'
            '<span class="num">{2}</span>'
            '<span class="body"><span class="name" data-en="{3}">{4}</span>'
            '<span class="desc" data-en="{5}">{6}</span></span>'
            '<span class="go" data-i18n="btn_add">添加</span></a>'.format(
                html.escape(url, quote=True), html.escape(url_en, quote=True),
                idx, name_en, name, desc_en, desc
            )
        )

    if parts:
        block = "".join(parts)
    else:
        block = (
            '<p class="muted" data-i18n="noshort">还没有配置快捷指令链接。在 '
            "<code>config.json</code> 里加上 <code>shortcuts</code> 字段，"
            "这里就会出现添加按钮。</p>"
        )

    page = SETUP_PAGE.replace("{{SHORTCUTS}}", block)
    return page.replace("{{IP}}", ip).replace("{{PORT}}", str(port))


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

# ---------------------------------------------------------------- 页面

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="theme-color" content="#f6f7f9">
<title>随手传 · DropToWin</title>
<style>
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  [hidden]{display:none !important}
  :root{
    --bg:#f6f7f9; --card:#ffffff; --line:#e6e8ec; --txt:#1c1e21;
    --sub:#8a8f98; --brand:#2f6bff; --ok:#18a058; --warn:#f0883e;
    --hdr:#3a4250; --hdrsub:#98a0ad; --sect:#6b7280;
  }
  @media (prefers-color-scheme:dark){
    :root{--bg:#111315;--card:#1a1d21;--line:#2a2f36;--txt:#eef0f3;--sub:#8d949e;--brand:#5b8cff;
      --hdr:#e8ebf0;--hdrsub:#8d949e;--sect:#a3a9b3;}
  }
  body{margin:0;background:var(--bg);color:var(--txt);
    font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
      "Microsoft YaHei",sans-serif;
    padding:20px 16px calc(28px + env(safe-area-inset-bottom));
    -webkit-user-select:none;-moz-user-select:none;user-select:none;
    -webkit-touch-callout:none}
  /* 这些内容要能长按选中/复制：剪贴板文字、输入框、文件名 */
  .cliptext,textarea,input,.fname{-webkit-user-select:text;-moz-user-select:text;
    user-select:text;-webkit-touch-callout:default}
  .wrap{max-width:600px;margin:0 auto;width:100%}
  /* 顶栏规格与配置页完全一致：高 30px、下留 16px，切页时不会跳动 */
  .topbar{display:flex;align-items:center;justify-content:space-between;gap:12px;
    height:30px;margin:0 0 16px}
  .brand{display:inline-flex;align-items:center;
    font-size:13.5px;font-weight:600;color:var(--hdr);letter-spacing:.2px;
    line-height:1}
  /* 用 CSS 画点：中文下的「·」和英文下的「·」字体回退不同，字形会变 */
  .brand .sep{flex:0 0 auto;display:inline-block;width:4px;height:4px;
    border-radius:50%;background:var(--hdrsub);margin:0 7px}
  .brand .brand-en{font-weight:500;color:var(--hdrsub)}
  .langbtn{transition:transform .14s cubic-bezier(.3,.8,.4,1),box-shadow .14s;
    flex:0 0 auto;width:auto;height:30px;padding:0 14px;border-radius:15px;
    border:1px solid var(--line);background:var(--card);color:var(--brand);
    font-family:inherit;font-size:12.5px;font-weight:600;line-height:1;
    display:inline-flex;align-items:center;justify-content:center;
    min-width:56px;cursor:pointer;
    box-shadow:0 3px 8px -3px rgba(18,32,64,.18),0 1px 2px rgba(18,32,64,.05)}
  .langbtn:active{transform:scale(.94)}
  h1{font-size:20px;margin:0 0 4px;letter-spacing:-.02em;text-align:center}
  .hint{color:var(--sub);font-size:12.5px;margin-bottom:14px;text-align:center}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;
    padding:15px;margin-bottom:12px}
  .sec-t{font-size:13px;font-weight:600;color:var(--sect);letter-spacing:.04em;
    margin:0 0 9px}
  .secnote{font-size:12px;color:var(--sub);margin:-3px 0 10px;line-height:1.55}
  textarea{width:100%;min-height:86px;border:1px solid var(--line);border-radius:12px;
    background:var(--bg);color:var(--txt);padding:12px 13px;font-size:16px;
    font-family:inherit;resize:vertical;outline:none}
  textarea:focus{border-color:var(--brand)}
  button{border:0;border-radius:12px;font-size:16px;font-weight:600;font-family:inherit;
    padding:13px;width:100%;cursor:pointer;
    transition:transform .14s cubic-bezier(.3,.8,.4,1),opacity .15s,
      box-shadow .14s cubic-bezier(.3,.8,.4,1);
    touch-action:manipulation;
    box-shadow:0 5px 14px -6px rgba(18,32,64,.28),0 1px 2px rgba(18,32,64,.06)}
  button:active{transform:scale(.96);
    box-shadow:0 2px 6px -3px rgba(18,32,64,.22),0 1px 1px rgba(18,32,64,.05)}
  button:disabled{opacity:.55}
  .primary{background:var(--brand);color:#fff;
    box-shadow:0 6px 16px -5px rgba(47,107,255,.55),0 1px 2px rgba(47,107,255,.22)}
  .primary:active{box-shadow:0 3px 8px -3px rgba(47,107,255,.45)}
  /* 与文件行的「下载」同一套：淡蓝底、品牌色文字、9px 圆角 */
  .ghost{background:rgba(47,107,255,.09);color:var(--brand);border:0;border-radius:9px;
    box-shadow:0 4px 12px -5px rgba(47,107,255,.45),0 1px 2px rgba(47,107,255,.10)}
  .ghost:active{box-shadow:0 2px 6px -3px rgba(47,107,255,.35)}
  .mt10{margin-top:10px}
  .pickbtn{width:100%;padding:16px 8px}
  .pickbtn .sub{display:block;font-size:11.5px;color:var(--brand);opacity:.72;
    font-weight:400;margin-top:3px;letter-spacing:0}
  .hotzone{outline:2px dashed var(--brand);outline-offset:6px;border-radius:16px}
  .cliptext{max-height:116px;overflow:auto;background:var(--bg);border:1px solid var(--line);
    border-radius:12px;padding:12px;font-size:14.5px;white-space:pre-wrap;
    word-break:break-word;-webkit-user-select:text;user-select:text}
  .cliptext.empty{color:var(--sub)}
  .clipimg{width:100%;max-height:58vh;object-fit:contain;border-radius:12px;
    display:block;border:1px solid var(--line);background:var(--bg);
    -webkit-touch-callout:default}
  .cliprow{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
  .cliprow button{padding:12px 8px;font-size:15px}
  .clipmeta{font-size:12px;color:var(--sub);margin-top:8px;display:none}
  .files{margin-top:12px;font-size:14px}
  .frow{display:flex;align-items:center;gap:10px;padding:9px 0;border-top:1px solid var(--line)}
  .frow:first-child{border-top:0}
  .fmeta{flex:1;min-width:0}
  .fname{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:14.5px}
  .fsize{font-size:12px;color:var(--sub)}
  .fdl{color:var(--brand);font-size:13.5px;font-weight:600;text-decoration:none;
    padding:6px 10px;border-radius:9px;background:rgba(47,107,255,.09);flex-shrink:0;
    transition:transform .14s cubic-bezier(.3,.8,.4,1);touch-action:manipulation;
    box-shadow:0 3px 9px -4px rgba(47,107,255,.5),0 1px 2px rgba(47,107,255,.10);
    -webkit-user-select:none;-moz-user-select:none;user-select:none;
    -webkit-touch-callout:none}
  .fdl:active{transform:scale(.92);box-shadow:0 1px 4px -2px rgba(47,107,255,.4)}
  .empty{color:var(--sub);font-size:13.5px;padding:8px 0}
  .ghead{display:flex;align-items:baseline;justify-content:space-between;
    font-size:12px;color:var(--sub);letter-spacing:.03em;
    padding:14px 0 2px;margin-top:4px}
  .ghead:first-child{padding-top:4px}
  .gname{font-weight:600}
  .gcount{font-size:11.5px;opacity:.8}
  .gwrap{display:block}
  .gwrap + .gwrap .frow:first-child{border-top:1px solid var(--line)}
  .scrollhint{position:fixed;left:0;right:0;bottom:0;z-index:60;pointer-events:none;
    padding:26px 0 14px;text-align:center;font-size:12px;color:var(--sub);
    background:linear-gradient(to top,var(--bg) 42%,rgba(0,0,0,0));
    opacity:0;transition:opacity .35s;-webkit-user-select:none;user-select:none}
  .scrollhint.show{opacity:1}
  .scrollhint span{display:inline-block;padding:5px 14px;border-radius:99px;
    background:var(--card);border:1px solid var(--line);
    box-shadow:0 4px 14px rgba(18,32,64,.12),0 1px 3px rgba(18,32,64,.08)}
  .toast{position:fixed;left:50%;bottom:34px;transform:translate(-50%,20px);
    background:#1c1e21;color:#fff;padding:11px 20px;border-radius:99px;font-size:14px;
    opacity:0;pointer-events:none;transition:.25s;z-index:99;max-width:86vw;text-align:center}
  .toast.show{opacity:1;transform:translate(-50%,0)}
  .toast.err{background:#d03050}
  .url{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;
    color:var(--sub);word-break:break-all}
  /* 深色模式下 9% 的蓝几乎看不见，按钮和「下载」一起提到 18% */
  @media (prefers-color-scheme:dark){
    .ghost,.fdl{background:rgba(91,140,255,.18)}
    button,.fdl,.langbtn{box-shadow:0 6px 16px -8px rgba(0,0,0,.9)}
    .primary{box-shadow:0 6px 18px -6px rgba(91,140,255,.5)}
    .ghost,.fdl{box-shadow:0 4px 12px -6px rgba(0,0,0,.85)}
  }
</style>
</head>
<body>
<div class="wrap">

<div class="topbar">
  <span class="brand">随手传<span class="sep"></span><span class="brand-en">DropToWin</span></span>
  <button type="button" class="langbtn" id="langBtn" onclick="toggleLang()">EN</button>
</div>

<h1>iPhone ⇄ PC</h1>
<div class="hint" data-i18n="hint">文本直进电脑剪贴板，文件直落共享文件夹</div>

<div class="card">
  <p class="sec-t" data-i18n="clipT">电脑剪贴板</p>
  <p class="secnote" data-i18n="clipNote">电脑复制的内容会显示在这里，图片可长按保存，文件可点下载。</p>
  <div class="cliptext empty" id="clipText">读取中…</div>
  <img id="clipImg" class="clipimg" hidden alt="电脑剪贴板图片">
  <div class="files" id="clipFiles" hidden></div>
  <div class="clipmeta" id="clipMeta"></div>
  <div class="cliprow">
    <button class="ghost" id="copyBtn">复制到手机</button>
    <button class="ghost" id="clipRefresh" data-i18n="refresh">刷新</button>
  </div>
</div>

<div class="card">
  <p class="sec-t" data-i18n="txtT">文本 / 剪贴板</p>
  <textarea id="txt" placeholder="粘贴或输入要发到电脑的文字…"></textarea>
  <button class="primary mt10" id="send">写入电脑剪贴板</button>
</div>

<div class="card">
  <p class="sec-t" data-i18n="uploadT">发送图片 / 文件</p>
  <button class="ghost pickbtn" id="btnPick"><span data-i18n="btnPick">选择照片或文件</span><span class="sub" data-i18n="btnPickSub">可多选</span></button>
  <input type="file" id="pickAny" multiple hidden>
  <div class="files" id="upl"></div>
</div>

<div class="card">
  <p class="sec-t" data-i18n="filesT">共享文件夹</p>
  <div class="files" id="list"><div class="empty">加载中…</div></div>
  <button class="ghost mt10" id="reload" data-i18n="reload">刷新列表</button>
</div>

</div>

<div class="scrollhint" id="scrollHint"><span data-i18n="scrollHint">上滑查看更多</span></div>
<div class="toast" id="toast"></div>

<script>
var $ = function(s){ return document.querySelector(s); };
document.addEventListener("touchstart", function(){}, {passive:true});

var T = {
  hint:        {zh: "文本直进电脑剪贴板，文件直落共享文件夹", en: "Text goes straight to the PC clipboard, files land in the shared folder"},
  clipT:       {zh: "电脑剪贴板", en: "PC clipboard"},
  clipNote:    {zh: "电脑复制的内容会显示在这里，图片可长按保存，文件可点下载。", en: "Anything you copy on the PC shows up here \u2014 long-press images, download files."},
  refresh:     {zh: "刷新", en: "Refresh"},
  txtT:        {zh: "文本 / 剪贴板", en: "Text / clipboard"},
  ph:          {zh: "粘贴或输入要发到电脑的文字…", en: "Paste or type text to send to the PC…"},
  send:        {zh: "写入电脑剪贴板", en: "Write to PC clipboard"},
  sending:     {zh: "发送中…", en: "Sending…"},
  uploadT:     {zh: "发送图片 / 文件", en: "Send photos / files"},
  btnPick:     {zh: "选择照片或文件", en: "Choose photos or files"},
  btnPickSub:  {zh: "可多选", en: "multi-select"},
  filesT:      {zh: "共享文件夹", en: "Shared folders"},
  reload:      {zh: "刷新列表", en: "Refresh list"},
  loading:     {zh: "加载中…", en: "Loading…"},
  reading:     {zh: "读取中…", en: "Reading…"},
  clipAlt:     {zh: "电脑剪贴板图片", en: "Image from the PC clipboard"},

  clipEmpty:   {zh: "剪贴板是空的", en: "The clipboard is empty"},
  truncated:   {zh: "内容较长，仅显示前 2 万字", en: "Long content — showing the first 20,000 characters"},
  folder:      {zh: "文件夹", en: "Folder"},
  tooBig:      {zh: "较大，建议改用 LocalSend", en: "Large — LocalSend may be a better fit"},
  download:    {zh: "下载", en: "Download"},
  copyToPhone: {zh: "复制到手机", en: "Copy to phone"},
  copyName:    {zh: "复制文件名", en: "Copy file name"},
  copyText:    {zh: "复制文本", en: "Copy text"},
  longPress:   {zh: "长按图片 → 存储到照片", en: "long-press the image → Save to Photos"},

  toastCopied:  {zh: "已复制到手机剪贴板", en: "Copied to the phone clipboard"},
  toastCopyErr: {zh: "复制失败，请长按文字手动选择", en: "Copy failed — long-press to select manually"},
  toastRefresh: {zh: "已读取电脑剪贴板", en: "Clipboard refreshed"},
  toastEmpty:   {zh: "电脑剪贴板是空的", en: "The PC clipboard is empty"},
  toastNeed:    {zh: "先粘点东西吧", en: "Paste something first"},
  toastSent:    {zh: "✓ 已写入电脑剪贴板", en: "✓ Written to the PC clipboard"},
  toastFail:    {zh: "失败：", en: "Failed: "},
  uploading:    {zh: "上传中…", en: "Uploading…"},
  saved:        {zh: "✓ 已保存", en: "✓ Saved"},
  toastDone:    {zh: "传输完成", en: "Transfer complete"},
  emptyFolders: {zh: "文件夹是空的", en: "These folders are empty"},
  readFail:     {zh: "读取失败", en: "Could not read the folder"},
  scrollHint:   {zh: "上滑查看更多", en: "Swipe up for more"},

  fInbox:  {zh: "收件箱", en: "Inbox"},
  fText:   {zh: "文本", en: "Text clips"},
  fToPhone:{zh: "发给我", en: "For my phone"}
};

var LANG = "zh";

function t(k){
  var o = T[k];
  if (!o) return k;
  return o[LANG] || o.zh;
}

function applyLang(lang, persist){
  LANG = (lang === "en") ? "en" : "zh";
  document.documentElement.lang = (LANG === "en") ? "en" : "zh-CN";
  document.querySelectorAll("[data-i18n]").forEach(function(el){
    el.textContent = t(el.getAttribute("data-i18n"));
  });
  var b = document.getElementById("langBtn");
  if (b) {
    b.textContent = (LANG === "en") ? "中文" : "EN";
    b.setAttribute("aria-label", (LANG === "en") ? "Switch to Chinese" : "Switch to English");
  }
  var tx = document.getElementById("txt");
  if (tx) tx.placeholder = t("ph");
  var sb = document.getElementById("send");
  if (sb && !sb.disabled) sb.textContent = t("send");
  var im = document.getElementById("clipImg");
  if (im) im.alt = t("clipAlt");
  if (persist) {
    try { localStorage.setItem("dtw_lang", LANG); } catch(e) {}
  }
  if (typeof pollClip === "function") pollClip(true);
  if (typeof loadList === "function") loadList();
}

function toggleLang(){
  applyLang(LANG === "en" ? "zh" : "en", true);
}

function detectLang(){
  var q = "";
  try { q = new URLSearchParams(location.search).get("lang") || ""; } catch(e) {}
  if (q) return q.indexOf("en") === 0 ? "en" : "zh";
  try {
    var s = localStorage.getItem("dtw_lang");
    if (s) return s === "en" ? "en" : "zh";
  } catch(e) {}
  var n = (navigator.language || navigator.userLanguage || "zh").toLowerCase();
  return n.indexOf("zh") === 0 ? "zh" : "en";
}

var tTimer;
function toast(msg, err){
  var t0 = document.getElementById("toast");
  t0.textContent = msg;
  t0.className = "toast show" + (err ? " err" : "");
  clearTimeout(tTimer);
  tTimer = setTimeout(function(){ t0.className = "toast"; }, 2400);
}

var clipText = "", clipSeq = -1, clipTimer = null;

function copyToPhone(txt){
  var ta = document.createElement("textarea");
  ta.value = txt;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;pointer-events:none";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  ta.setSelectionRange(0, 999999);
  var ok = false;
  try { ok = document.execCommand("copy"); } catch(e){}
  document.body.removeChild(ta);
  if (ok) toast(t("toastCopied"));
  else toast(t("toastCopyErr"), true);
}

async function pollClip(force){
  try{
    var r = await fetch("/api/clipboard", {cache:"no-store"});
    var d = await r.json();
    if (!force && d.seq === clipSeq) return;
    clipSeq = d.seq;
    clipText = d.has_text ? d.text : "";

    var tEl = document.getElementById("clipText"),
        im = document.getElementById("clipImg"),
        meta = document.getElementById("clipMeta"),
        btn = document.getElementById("copyBtn"),
        fl = document.getElementById("clipFiles");
    var LIMIT = 100 * 1024 * 1024;

    if (d.files && d.files.length){
      fl.hidden = false;
      fl.innerHTML = d.files.map(function(f, i){
        var sub;
        if (f.is_dir) sub = t("folder");
        else if (f.bytes > LIMIT) sub = f.size + " · " + t("tooBig");
        else sub = f.size;
        var act = f.is_dir
          ? '<span class="fdl" style="opacity:.4">—</span>'
          : '<a class="fdl" href="/clip/file?i=' + i + '&s=' + d.seq + '">' + t("download") + "</a>";
        return '<div class="frow"><div class="fmeta">' +
          '<div class="fname">' + escapeHtml(f.name) + "</div>" +
          '<div class="fsize">' + sub + "</div>" +
          "</div>" + act + "</div>";
      }).join("");
      im.hidden = true;
      im.removeAttribute("src");
      tEl.hidden = true;
      meta.style.display = "none";
      btn.hidden = false;
      btn.textContent = d.has_text ? t("copyName") : t("copyToPhone");
    } else if (d.has_image){
      fl.hidden = true;
      fl.innerHTML = "";
      im.hidden = false;
      im.src = "/clip/img.png?v=" + d.seq;
      tEl.hidden = true;
      meta.style.display = "block";
      meta.textContent = d.image_size + " · " + d.image_kb + " KB · " + t("longPress");
      // 网页没法把图片写进相册（iOS 只有长按的「存储到照片」），所以纯图片时不摆按钮，
      // 免得点一下又弹一句"请长按"，多一层没用的动作。有文字时按钮还是"复制文本"。
      btn.hidden = !d.has_text;
      if (d.has_text) btn.textContent = t("copyText");
    } else {
      fl.hidden = true;
      fl.innerHTML = "";
      im.hidden = true;
      im.removeAttribute("src");
      tEl.hidden = false;
      meta.style.display = d.truncated ? "block" : "none";
      if (d.truncated) meta.textContent = t("truncated");
      tEl.textContent = d.has_text ? d.text : t("clipEmpty");
      tEl.className = d.has_text ? "cliptext" : "cliptext empty";
      btn.hidden = false;
      btn.textContent = t("copyToPhone");
    }
  }catch(e){}
}

function startPoll(){
  if (clipTimer) return;
  clipTimer = setInterval(function(){ pollClip(false); }, 2000);
}
function stopPoll(){
  clearInterval(clipTimer);
  clipTimer = null;
}

document.getElementById("clipRefresh").onclick = function(){
  pollClip(true); toast(t("toastRefresh"));
};
document.getElementById("copyBtn").onclick = function(){
  if (clipText){
    copyToPhone(clipText);
  } else {
    toast(t("toastEmpty"), true);
  }
};
document.addEventListener("visibilitychange", function(){
  if (document.hidden) stopPoll();
  else { pollClip(true); startPoll(); }
});

document.getElementById("send").onclick = async function(){
  var text = document.getElementById("txt").value;
  if (!text.trim()) return toast(t("toastNeed"), true);
  var btn = document.getElementById("send");
  btn.disabled = true; btn.textContent = t("sending");
  try{
    var r = await fetch("/api/text", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text: text})
    });
    if (!r.ok) throw new Error(await r.text());
    toast(t("toastSent"));
    document.getElementById("txt").value = "";
  }catch(e){
    toast(t("toastFail") + e.message, true);
  }finally{
    btn.disabled = false; btn.textContent = t("send");
  }
};

var pAny = document.getElementById("pickAny");
document.getElementById("btnPick").onclick = function(){ pAny.click(); };
pAny.onchange = function(){ upload([].slice.call(pAny.files)); pAny.value = ""; };

document.addEventListener("dragover", function(e){
  e.preventDefault(); document.body.classList.add("hotzone");
});
document.addEventListener("dragleave", function(e){
  if (e.relatedTarget === null) document.body.classList.remove("hotzone");
});
document.addEventListener("drop", function(e){
  e.preventDefault(); document.body.classList.remove("hotzone");
  if (e.dataTransfer.files.length) upload([].slice.call(e.dataTransfer.files));
});

async function upload(files){
  if (!files.length) return;
  var box = document.getElementById("upl");
  for (var i = 0; i < files.length; i++){
    var f = files[i];
    var row = document.createElement("div");
    row.className = "frow";
    row.innerHTML = '<div class="fmeta"><div class="fname">' + escapeHtml(f.name) +
      '</div><div class="fsize">' + t("uploading") + "</div></div>";
    box.prepend(row);
    var sizeEl = row.querySelector(".fsize");
    try{
      var r = await fetch("/api/file?name=" + encodeURIComponent(f.name), {
        method: "POST",
        headers: {"Content-Type": "application/octet-stream"},
        body: f
      });
      if (!r.ok) throw new Error(await r.text());
      sizeEl.textContent = t("saved");
      sizeEl.style.color = "var(--ok)";
    }catch(e){
      sizeEl.textContent = "✗ " + e.message;
      sizeEl.style.color = "var(--warn)";
    }
  }
  toast(t("toastDone"));
  loadList();
}

function escapeHtml(s){
  return s.replace(/[&<>"']/g, function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}

function folderLabel(name){
  if (name === "收件箱") return t("fInbox");
  if (name === "文本")   return t("fText");
  if (name === "发给我") return t("fToPhone");
  return name;
}

async function loadList(){
  var box = document.getElementById("list");
  try{
    var r = await fetch("/api/list");
    var data = await r.json();
    var groups = data.folders || [];
    var total = groups.reduce(function(n, g){ return n + g.files.length; }, 0);
    if (!total){
      box.innerHTML = '<div class="empty">' + t("emptyFolders") + "</div>";
      return;
    }
    box.innerHTML = groups.map(function(g){
      if (!g.files.length) return "";
      var rows = g.files.map(function(f){
        return '<div class="frow">' +
          '<div class="fmeta"><div class="fname">' + escapeHtml(f.name) + "</div>" +
          '<div class="fsize">' + f.size + " · " + f.time + "</div></div>" +
          '<a class="fdl" href="/download?path=' + encodeURIComponent(f.path) + '">' +
          t("download") + "</a></div>";
      }).join("");
      return '<div class="gwrap"><div class="ghead"><span class="gname">' +
        folderLabel(g.name) + '</span><span class="gcount">' + g.files.length +
        "</span></div>" + rows + "</div>";
    }).join("");
  }catch(e){
    box.innerHTML = '<div class="empty">' + t("readFail") + "</div>";
  }
  updateScrollHint();
}

function updateScrollHint(){
  var el = document.getElementById("scrollHint");
  if (!el) return;
  var doc = document.documentElement;
  var more = doc.scrollHeight - window.innerHeight;
  var y = window.pageYOffset || doc.scrollTop || 0;
  if (more > 80 && y < 40) el.classList.add("show");
  else el.classList.remove("show");
}
window.addEventListener("scroll", updateScrollHint, {passive:true});
window.addEventListener("resize", updateScrollHint);

document.getElementById("reload").onclick = loadList;

applyLang(detectLang(), false);
startPoll();
setTimeout(updateScrollHint, 500);
setTimeout(updateScrollHint, 1600);
</script>
</body>
</html>
"""
SETUP_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>安装完成 · 随手传</title>
<style>
  :root{--bg:#f6f7f9;--card:#fff;--line:#e6e8ec;--txt:#1a1c20;--sub:#777d87;--brand:#2f6bff}
  *{box-sizing:border-box;min-width:0;max-width:100%}
  html,body{max-width:100%;overflow-x:hidden;width:100%}
  [hidden]{display:none !important}
  body{margin:0;padding:20px 16px 60px;background:var(--bg);color:var(--txt);
    font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;
    overflow-wrap:break-word;word-break:break-word;
    -webkit-user-select:none;-moz-user-select:none;user-select:none;
    -webkit-touch-callout:none}
  .wrap{max-width:600px;margin:0 auto;width:100%}
  /* 顶栏规格与主页完全一致：高 30px、下留 16px，切页时不会跳动 */
  .topbar{display:flex;align-items:center;justify-content:space-between;gap:12px;
    height:30px;margin:0 0 16px}
  .brand{display:inline-flex;align-items:center;
    font-size:13.5px;font-weight:600;color:#3a4250;letter-spacing:.2px;line-height:1}
  /* 用 CSS 画点：中文下的「·」和英文下的「·」字体回退不同，字形会变 */
  .brand .sep{flex:0 0 auto;display:inline-block;width:4px;height:4px;
    border-radius:50%;background:#98a0ad;margin:0 7px}
  .brand .brand-en{font-weight:500;color:#98a0ad}
  .langbtn{transition:transform .14s cubic-bezier(.3,.8,.4,1);
    flex:0 0 auto;height:30px;padding:0 14px;border-radius:15px;
    border:1px solid #dfe4ec;background:#fff;color:#2f6bff;
    font-family:inherit;font-size:12.5px;font-weight:600;line-height:1;
    display:inline-flex;align-items:center;justify-content:center;
    min-width:56px;cursor:pointer;-webkit-tap-highlight-color:transparent;
    box-shadow:0 3px 8px -3px rgba(18,32,64,.18),0 1px 2px rgba(18,32,64,.05)}
  .langbtn:active{background:#eef3ff;transform:scale(.94)}
  .ok{text-align:center;margin-bottom:26px}
  .ok .dot{display:inline-block;width:46px;height:46px;border-radius:50%;
    background:#e8f7ee;color:#1a9c50;font-size:26px;line-height:46px}
  h1{font-size:21px;margin:14px 0 6px}
  .sub{color:var(--sub);font-size:14px;margin:0}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;
    padding:20px;margin-bottom:14px;width:100%;max-width:100%;overflow:hidden}
  .sec{font-size:12.5px;color:var(--sub);margin:0 0 12px;letter-spacing:.4px}
  .url{font:600 15px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;
    white-space:nowrap;overflow-x:auto;width:100%;max-width:100%;
    letter-spacing:-.2px;-webkit-overflow-scrolling:touch}
  .ipbox{font:700 19px/1.3 ui-monospace,Menlo,monospace;color:var(--brand);
    background:#eef3ff;border:1px solid #d6e0f5;border-radius:12px;
    padding:14px;text-align:center;letter-spacing:.5px}
  .wide{width:100%;margin-top:10px;display:block}
  .example{font:12.5px/1.75 ui-monospace,Menlo,monospace;color:#555;
    margin:0;word-break:break-all;background:#f6f7f9;
    padding:11px 12px;border-radius:9px}
  .example mark{background:#ffe9a8;color:#7a5300;padding:1px 4px;
    border-radius:4px;font-weight:700}
  .exhint{font-size:12px;color:var(--sub);margin:9px 0 0;line-height:1.5}
  .btnrow{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}
  button{border:0;border-radius:12px;padding:13px 10px;font-size:15px;cursor:pointer;
    font-family:inherit;touch-action:manipulation;
    transition:transform .14s cubic-bezier(.3,.8,.4,1),background .14s,
      box-shadow .14s cubic-bezier(.3,.8,.4,1);
    box-shadow:0 5px 14px -6px rgba(18,32,64,.28),0 1px 2px rgba(18,32,64,.06)}
  button:active{transform:scale(.96);
    box-shadow:0 2px 6px -3px rgba(18,32,64,.22),0 1px 1px rgba(18,32,64,.05)}
  .primary{background:var(--brand);color:#fff;
    box-shadow:0 6px 16px -5px rgba(47,107,255,.55),0 1px 2px rgba(47,107,255,.22)}
  .primary:active{box-shadow:0 3px 8px -3px rgba(47,107,255,.45)}
  .ghost{background:rgba(47,107,255,.09);color:var(--brand);border-radius:9px;
    box-shadow:0 4px 12px -5px rgba(47,107,255,.45),0 1px 2px rgba(47,107,255,.10)}
  .ghost:active{box-shadow:0 2px 6px -3px rgba(47,107,255,.35)}
  ol{margin:0;padding-left:19px}
  li{margin-bottom:13px;font-size:14.5px}
  li b{display:block;margin-bottom:2px;font-size:15px}
  .tip{background:#fff8e6;border:1px solid #f5e2b0;border-radius:12px;
    padding:13px 15px;font-size:13.5px;color:#8a6310;line-height:1.6}
  code{background:#eef1f6;padding:1.5px 5px;border-radius:5px;
    font:13px/1 ui-monospace,Menlo,monospace}
  .linkbtn{display:flex;align-items:center;gap:12px;padding:15px 16px;
    border-radius:14px;background:#fff;border:1.5px solid #d6e0f5;
    box-shadow:0 6px 16px -7px rgba(47,107,255,.45),0 2px 5px rgba(47,107,255,.06);
    text-decoration:none;margin-bottom:10px;
    transition:transform .12s ease,background .12s ease,box-shadow .12s ease}
  .linkbtn:last-of-type{margin-bottom:0}
  .linkbtn:active{transform:scale(.97);background:#f4f7ff}
  .linkbtn .num{flex:0 0 26px;width:26px;height:26px;border-radius:50%;
    background:var(--brand);color:#fff;font-size:13.5px;font-weight:600;
    display:flex;align-items:center;justify-content:center}
  .linkbtn .body{flex:1;min-width:0}
  .linkbtn .name{display:block;font-size:15px;font-weight:600;color:var(--txt)}
  .linkbtn .desc{display:block;font-size:12.5px;color:var(--sub);margin-top:2px}
  .linkbtn .go{flex:0 0 auto;font-size:14.5px;font-weight:600;color:#fff;
    background:var(--brand);border-radius:9px;padding:8px 16px;
    white-space:nowrap;line-height:1.25;
    box-shadow:0 2px 6px rgba(47,107,255,.25)}
  .muted{color:var(--sub);font-size:13.5px;margin:0}
  .hintline{font-size:12.5px;color:var(--sub);margin:12px 0 0;line-height:1.6}
  .hl{font-style:normal;color:#1a4fd6;background:#eef3ff;padding:1px 5px;
    border-radius:4px;font-weight:500}
  .hintline .shico{width:15px;height:15px;vertical-align:-2px;margin:0 2px}
  .tag{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.6px;
    padding:3px 9px;border-radius:6px;background:#e8efff;color:#2f6bff;
    margin-bottom:9px}
  .tag.green{background:#e4f7eb;color:#159a4d}
  .tagrow{display:flex;align-items:center;gap:8px;margin-bottom:9px}
  .tagrow .tag{margin-bottom:0}
  .taghint{font-size:11.5px;color:#8a93a2;letter-spacing:.2px}
  .ctitle{font-size:17.5px;font-weight:600;margin:0 0 7px;color:var(--txt)}
  .cdesc{font-size:13px;color:var(--sub);margin:0 0 16px;line-height:1.65}
  .steplabel{font-size:12.5px;font-weight:600;color:#4a5262;margin:18px 0 10px;
    letter-spacing:.2px}
  .subhint{font-size:12.5px;color:var(--sub);margin:-5px 0 12px;line-height:1.6}
  .guide{margin-top:12px;padding:14px 15px;background:#f7f9fc;
    border:1px solid #e4e9f2;border-radius:12px}
  .guide h4{font-size:13px;font-weight:600;color:#3a4250;margin:0 0 9px;
    line-height:1.5}
  .guide .opt{font-size:10.5px;font-weight:600;color:#8e96a3;background:#eceff4;
    padding:1.5px 6px;border-radius:4px;margin-left:5px;vertical-align:1px}
  .guide .must{font-size:10.5px;font-weight:600;color:#c25b00;background:#fff0dd;
    padding:1.5px 6px;border-radius:4px;margin-left:5px;vertical-align:1px}
  .guide .row{font-size:13px;color:#4c5460;line-height:1.85;margin:0}
  .guide .row em{font-style:normal;color:#1a4fd6;background:#eef3ff;
    padding:1px 5px;border-radius:4px;font-weight:500}
  .guide .arrow{color:#b8c0cc;margin:0 3px;font-size:12px}
  .guide .note{font-size:12px;color:#8e96a3;margin:7px 0 0;line-height:1.6}
  .guide .plus{display:inline-block;width:15px;height:15px;line-height:15px;
    text-align:center;background:#34c759;color:#fff;border-radius:4px;
    font-weight:700;font-size:12px;vertical-align:-1px;margin:0 1px}
  .celebrate{margin-top:20px;padding:28px 20px 26px;border-radius:16px;
    background:linear-gradient(135deg,#eef4ff 0%,#f5f0ff 100%);
    border:1px solid #dde5f7;text-align:center;
    -webkit-user-select:none;-moz-user-select:none;user-select:none;
    -webkit-touch-callout:none;-webkit-tap-highlight-color:transparent;
    cursor:default}
  .celebrate .em{display:inline-block;font-size:40px;line-height:1;margin-bottom:12px;
    cursor:pointer;-webkit-tap-highlight-color:transparent;
    transition:transform .18s cubic-bezier(.34,1.56,.64,1)}
  .celebrate .em:active{transform:scale(1.28)}
  .celebrate h3{font-size:17.5px;font-weight:600;margin:0 0 7px;color:#242b39}
  .celebrate p{font-size:13.5px;color:#6b7484;margin:0;line-height:1.6}
  .guide .shico{vertical-align:-3px;margin:0 3px}
</style>
</head>
<body>
<div class="wrap">

  <div class="topbar">
    <span class="brand">随手传<span class="sep"></span><span class="brand-en">DropToWin</span></span>
    <button type="button" class="langbtn" id="langBtn" onclick="toggleLang()">EN</button>
  </div>

  <div class="ok">
    <span class="dot">&#10003;</span>
    <h1 data-i18n="h1">电脑端已就绪</h1>
    <p class="sub" data-i18n="sub">手机连上同一个 Wi-Fi 后有两种传法<br>方式一打开就能用，方式二设置一次更顺手</p>
  </div>

  <div class="card">
    <div class="tagrow">
      <span class="tag" data-i18n="tag1">方式一</span>
      <span class="taghint" data-i18n="tag1hint">打开就能用</span>
    </div>
    <h2 class="ctitle" data-i18n="t1">网页传文件</h2>
    <p class="cdesc" data-i18n="d1">打开网页就能用，不用装任何东西。适合临时传个文件、看看电脑剪贴板。</p>
    <div class="url" id="addr">http://{{IP}}:{{PORT}}</div>
    <div class="btnrow">
      <button class="primary" onclick="location.href='/'"><span data-i18n="open">打开网页</span></button>
      <button class="ghost" onclick="doCopy('addr')"><span data-i18n="copyaddr">复制地址</span></button>
    </div>
    <p class="hintline"><span data-i18n="w1a">点上面蓝色<span class="hl">打开网页</span>，在 Safari 浏览器里点底部这个</span><svg class="shico" viewBox="0 0 24 24" aria-hidden="true"><path d="M9.5 9.6 H6.7 A1.7 1.7 0 0 0 5 11.3 V18.9 A1.7 1.7 0 0 0 6.7 20.6 H17.3 A1.7 1.7 0 0 0 19 18.9 V11.3 A1.7 1.7 0 0 0 17.3 9.6 H14.5" fill="none" stroke="#0a84ff" stroke-width="1.9" stroke-linecap="round"/><path d="M12 3.2 V14.6" stroke="#0a84ff" stroke-width="1.9" stroke-linecap="round"/><path d="M8.7 6.4 L12 3.1 L15.3 6.4" fill="none" stroke="#0a84ff" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg><span data-i18n="w1b">共享按钮</span></p>
    <p class="hintline" data-i18n="w1c">选<span class="hl">添加到主屏幕</span>，以后像个 App 一样用</p>
  </div>

  <div class="card">
    <div class="tagrow">
      <span class="tag green" data-i18n="tag2">方式二</span>
      <span class="taghint" data-i18n="tag2hint">进阶 · 设置一次，之后更顺手</span>
    </div>
    <h2 class="ctitle" data-i18n="t2">快捷指令</h2>
    <p class="cdesc" data-i18n="d2">装一次，以后从微信、相册直接分享过来，或者一键同步剪贴板。适合天天用。</p>

    <p class="steplabel" data-i18n="s1">① 添加这两个快捷指令</p>
    <p class="subhint" data-i18n="s1hint">点「添加」会跳到 iCloud 页面，再点「添加快捷指令」。</p>
    {{SHORTCUTS}}

    <p class="steplabel" data-i18n="s2">② 把里面的 IP 换成这台电脑的</p>
    <div class="ipbox" id="ipv">{{IP}}</div>
    <button class="primary wide" onclick="doCopy('ipv')"><span data-i18n="copyip">复制 IP</span></button>
    <p class="example">http://<mark>{{IP}}</mark>:{{PORT}}/api/&hellip;</p>
    <p class="exhint" data-i18n="s2hint">只换黄色那段，<code>http://</code> 和 <code>/api/&hellip;</code> 保持原样。两个快捷指令各改一次。</p>

    <div class="guide">
      <h4><span data-i18n="g3t">③ 让「发到电脑」出现在分享菜单</span><span class="must" data-i18n="must">不做这步找不到</span></h4>
      <p class="row" data-i18n="g3r1">回到快捷指令<em>首页</em>，长按刚装好的<em>发到电脑</em></p>
      <p class="row" data-i18n="g3r2">点<em>详细信息</em><span class="arrow">&rarr;</span>打开<em>在共享表单中显示</em><span class="arrow">&rarr;</span>点<em>完成</em></p>
      <p class="note" data-i18n="g3n">打开后，在微信或相册里点分享，就能看到「发到电脑」。</p>
    </div>

    <div class="guide">
      <h4><span data-i18n="g4t">④ 把它顶到分享菜单第一屏</span><span class="opt" data-i18n="opt">可选</span></h4>
      <p class="row"><span data-i18n="g4r1a">就在这个网页，点底部中间这个</span><svg class="shico" viewBox="0 0 24 24" width="19" height="19" aria-hidden="true"><path d="M9.5 9.6 H6.7 A1.7 1.7 0 0 0 5 11.3 V18.9 A1.7 1.7 0 0 0 6.7 20.6 H17.3 A1.7 1.7 0 0 0 19 18.9 V11.3 A1.7 1.7 0 0 0 17.3 9.6 H14.5" fill="none" stroke="#0a84ff" stroke-width="1.9" stroke-linecap="round"/><path d="M12 3.2 V14.6" stroke="#0a84ff" stroke-width="1.9" stroke-linecap="round"/><path d="M8.7 6.4 L12 3.1 L15.3 6.4" fill="none" stroke="#0a84ff" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg><span data-i18n="g4r1b">共享按钮</span></p>
      <p class="row" data-i18n="g4r2">面板划到最底点<em>编辑操作</em><span class="arrow">&rarr;</span>找到<em>发到电脑</em></p>
      <p class="row" data-i18n="g4r3">点左边绿色<span class="plus">+</span><span class="arrow">&rarr;</span>点<em>完成</em></p>
      <p class="note" data-i18n="g4n">设置完，以后分享时第一屏就能直接看到「发到电脑」，不用往下滑。</p>
    </div>
  </div>

  <div class="tip" data-i18n="tip">
    <b>两种方式互不影响，可以都用。</b>手机打不开网页时，先确认连的是同一个 Wi-Fi，再检查电脑上的服务（双击 <code>_tools\start.vbs</code>）。
  </div>

  <div class="celebrate">
    <span class="em" id="confettiBtn">&#127881;</span>
    <h3 data-i18n="cdone">大功告成</h3>
    <p data-i18n="cdonep">传个文件或照片试试吧</p>
  </div>
</div>

<div id="L-en" hidden>
  <div data-k="h1">The PC side is ready</div>
  <div data-k="sub">Once your phone is on the same Wi-Fi, you can pick either way<br>Way 1 works right now &middot; Way 2 needs a one-time setup</div>
  <div data-k="tag1">Way 1</div>
  <div data-k="tag1hint">No setup needed</div>
  <div data-k="t1">Transfer in the browser</div>
  <div data-k="d1">Open a web page and you are set &mdash; nothing to install. Handy for a quick file or for checking the PC clipboard.</div>
  <div data-k="open">Open page</div>
  <div data-k="copyaddr">Copy address</div>
  <div data-k="w1a">Tap the blue <span class="hl">Open page</span> above, then in Safari tap this</div>
  <div data-k="w1b">share button</div>
  <div data-k="w1c">Choose <span class="hl">Add to Home Screen</span> and it works like an app</div>
  <div data-k="tag2">Way 2</div>
  <div data-k="tag2hint">Advanced &middot; set up once, smoother after</div>
  <div data-k="t2">Shortcuts</div>
  <div data-k="d2">Add them once, then share straight from WeChat or Photos, or sync the clipboard in one tap. Made for daily use.</div>
  <div data-k="s1">1. Add both shortcuts</div>
  <div data-k="s1hint">&ldquo;Add&rdquo; opens an iCloud page &mdash; tap &ldquo;Add Shortcut&rdquo; there.</div>
  <div data-k="btn_add">Add</div>
  <div data-k="noshort">No shortcut links configured yet. Add a <code>shortcuts</code> field to <code>config.json</code> and the add buttons will show up here.</div>
  <div data-k="s2">2. Swap the IP for this computer&rsquo;s</div>
  <div data-k="copyip">Copy IP</div>
  <div data-k="s2hint">Replace only the yellow part. Leave <code>http://</code> and <code>/api/&hellip;</code> untouched. Do this once per shortcut.</div>
  <div data-k="g3t">3. Make &ldquo;Send to PC&rdquo; appear in the share sheet</div>
  <div data-k="must">Required</div>
  <div data-k="g3r1">Go back to the Shortcuts <em>home screen</em> and long-press <em>Send to PC</em></div>
  <div data-k="g3r2">Tap <em>Details</em><span class="arrow">&rarr;</span>turn on <em>Show in Share Sheet</em><span class="arrow">&rarr;</span>tap <em>Done</em></div>
  <div data-k="g3n">After that, tapping Share in WeChat or Photos will show &ldquo;Send to PC&rdquo;.</div>
  <div data-k="g4t">4. Pin it to the first screen of the share sheet</div>
  <div data-k="opt">Optional</div>
  <div data-k="g4r1a">Right here on this page, tap this</div>
  <div data-k="g4r1b">share button</div>
  <div data-k="g4r2">Scroll to the bottom and tap <em>Edit Actions</em><span class="arrow">&rarr;</span>find <em>Send to PC</em></div>
  <div data-k="g4r3">Tap the green <span class="plus">+</span><span class="arrow">&rarr;</span>tap <em>Done</em></div>
  <div data-k="g4n">Next time &ldquo;Send to PC&rdquo; sits on the first screen &mdash; no more scrolling.</div>
  <div data-k="tip"><b>The two ways are independent &mdash; use either or both.</b> If the phone cannot open the page, make sure it is on the same Wi-Fi, then check the service on the PC (double-click <code>_tools\start.vbs</code>).</div>
  <div data-k="cdone">All done</div>
  <div data-k="cdonep">Try sending a file or photo</div>
</div>

<script>
var LANG = 'zh';
var ZH = {}, EN = {}, SC = [];

function collectI18n(){
  document.querySelectorAll('[data-i18n]').forEach(function(el){
    ZH[el.getAttribute('data-i18n')] = el.innerHTML;
  });
  var box = document.getElementById('L-en');
  if (box) {
    box.querySelectorAll('[data-k]').forEach(function(n){
      EN[n.getAttribute('data-k')] = n.innerHTML;
    });
  }
  document.querySelectorAll('[data-en]').forEach(function(el){
    SC.push([el, el.innerHTML]);
  });
}

function applyLang(lang, persist){
  var dict = (lang === 'en') ? EN : ZH;
  document.querySelectorAll('[data-i18n]').forEach(function(el){
    var k = el.getAttribute('data-i18n');
    if (dict[k] != null) el.innerHTML = dict[k];
  });
  SC.forEach(function(pair){
    pair[0].innerHTML = (lang === 'en')
      ? (pair[0].getAttribute('data-en') || pair[1])
      : pair[1];
  });
  document.documentElement.lang = (lang === 'en') ? 'en' : 'zh-CN';
  document.title = (lang === 'en') ? 'Setup complete - DropToWin' : '安装完成 · 随手传';
  // 快捷指令按钮：中文页面指中文版指令，英文页面指英文版指令
  document.querySelectorAll('a[data-href-en]').forEach(function(a){
    a.href = (lang === 'en')
      ? a.getAttribute('data-href-en')
      : (a.getAttribute('data-href-zh') || a.getAttribute('href'));
  });
  var b = document.getElementById('langBtn');
  if (b) {
    b.textContent = (lang === 'en') ? '中文' : 'EN';
    b.setAttribute('aria-label', (lang === 'en') ? 'Switch to Chinese' : 'Switch to English');
  }
  LANG = (lang === 'en') ? 'en' : 'zh';
  if (persist) {
    try { localStorage.setItem('dtw_lang', LANG); } catch(e) {}
  }
}

function toggleLang(){
  applyLang(LANG === 'en' ? 'zh' : 'en', true);
}

function detectLang(){
  var q = '';
  try { q = new URLSearchParams(location.search).get('lang') || ''; } catch(e) {}
  if (q) { return q.indexOf('en') === 0 ? 'en' : 'zh'; }
  try {
    var s = localStorage.getItem('dtw_lang');
    if (s) { return s === 'en' ? 'en' : 'zh'; }
  } catch(e) {}
  var n = (navigator.language || navigator.userLanguage || 'zh').toLowerCase();
  return n.indexOf('zh') === 0 ? 'zh' : 'en';
}

function doCopy(id){
  var t = document.getElementById(id).textContent.trim();
  var ta = document.createElement('textarea');
  ta.value = t;
  ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
  document.body.appendChild(ta);
  ta.select();
  var ok = false;
  try { ok = document.execCommand('copy'); } catch(e) {}
  document.body.removeChild(ta);
  if (ok) {
    alert(LANG === 'en' ? 'Copied: ' + t : '\u5df2\u590d\u5236\uff1a' + t);
  } else {
    alert(LANG === 'en' ? 'Copy failed - please select it manually' : '\u590d\u5236\u5931\u8d25\uff0c\u8bf7\u624b\u52a8\u9009\u4e2d');
  }
}

var __cv = null, __ctx = null, __parts = [], __raf = null;
var __W = 0, __H = 0, __dpr = 1;
var COLORS = ['#ff3b30','#ff9500','#ffcc00','#34c759','#00c7be',
              '#007aff','#af52de','#ff2d55'];
var PER_CLICK = 130, MAX_PARTS = 750;

function ensureCanvas(){
  if (__cv) return;
  __dpr = window.devicePixelRatio || 1;
  __W = window.innerWidth;
  __H = window.innerHeight;
  __cv = document.createElement('canvas');
  __cv.style.cssText = 'position:fixed;left:0;top:0;width:100%;height:100%;'
    + 'pointer-events:none;z-index:99999';
  __cv.width = __W * __dpr;
  __cv.height = __H * __dpr;
  document.body.appendChild(__cv);
  __ctx = __cv.getContext('2d');
  __ctx.scale(__dpr, __dpr);
}

function spawn(n, ox, oy){
  ensureCanvas();
  var now = performance.now();
  for (var i = 0; i < n; i++) {
    var ang = -Math.PI / 2 + (Math.random() - 0.5) * 1.7;
    var sp  = 9 + Math.random() * 13;
    __parts.push({
      x: ox + (Math.random() - 0.5) * 90,
      y: oy,
      vx: Math.cos(ang) * sp,
      vy: Math.sin(ang) * sp,
      w: 5 + Math.random() * 6,
      h: 7 + Math.random() * 7,
      rot: Math.random() * Math.PI * 2,
      vr: (Math.random() - 0.5) * 0.34,
      c: COLORS[(Math.random() * COLORS.length) | 0],
      born: now
    });
  }
  if (__parts.length > MAX_PARTS) {
    __parts.splice(0, __parts.length - MAX_PARTS);
  }
  if (!__raf) __raf = requestAnimationFrame(loop);
}

function loop(now){
  __ctx.clearRect(0, 0, __W, __H);

  for (var i = __parts.length - 1; i >= 0; i--) {
    var p = __parts[i];
    p.vy += 0.42;
    p.vx *= 0.992;
    p.vy *= 0.995;
    p.x += p.vx;
    p.y += p.vy;
    p.rot += p.vr;

    var age = (now - p.born) / 1000;
    var fade = age > 2.3 ? Math.max(0, 1 - (age - 2.3) / 0.9) : 1;

    if (p.y > __H + 60 || fade <= 0) {
      __parts.splice(i, 1);
      continue;
    }

    __ctx.save();
    __ctx.globalAlpha = fade;
    __ctx.translate(p.x, p.y);
    __ctx.rotate(p.rot);
    __ctx.fillStyle = p.c;
    __ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
    __ctx.restore();
  }

  if (__parts.length > 0) {
    __raf = requestAnimationFrame(loop);
  } else {
    cancelAnimationFrame(__raf);
    __raf = null;
    __cv.remove();
    __cv = null;
    __ctx = null;
  }
}

function fireConfetti(){
  var el = document.getElementById('confettiBtn');
  var r = el.getBoundingClientRect();
  spawn(PER_CLICK, r.left + r.width / 2, r.top + r.height / 2);
}

var __cb = document.getElementById('confettiBtn');
if (__cb) __cb.addEventListener('click', fireConfetti);

document.addEventListener('touchstart', function(){}, {passive:true});
collectI18n();
applyLang(detectLang(), false);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- 服务

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DropToWin/1.0"

    def log_message(self, fmt, *args):
        line = f"{self.address_string()} {fmt % args}"
        sys.stdout.write(f"[{datetime.datetime.now():%H:%M:%S}] {line}\n")
        file_log(line)

    def handle_error(self, request, client_address):
        """客户端提前断开是常态（iOS 发完就走），不要刷一堆 traceback。"""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)



    # -------- 响应助手
    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    # -------- GET
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
            return
        if path == "/setup":
            self._send(200, render_setup_page(lan_ip(), PORT),
                       "text/html; charset=utf-8")
            return
        if path == "/api/list":
            self._json({"folders": self._collect()})
            return
        if path == "/api/clipboard":
            self._json(self._clip_info())
            return
        if path == "/clip/img.png":
            snap = clipboard_snapshot()
            if not snap["png"]:
                self._send(404, "no image")
            else:
                self._send(200, snap["png"], "image/png")
            return
        if path == "/clip/file":
            q = urllib.parse.parse_qs(parsed.query)
            self._clip_file(q.get("i", ["0"])[0], q.get("s", ["0"])[0])
            return
        if path == "/download":
            self._download(urllib.parse.parse_qs(parsed.query).get("path", [""])[0])
            return
        self._send(404, "Not found")

    def do_HEAD(self):
        self.do_GET()

    # -------- POST
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/text":
                self._post_text()
            elif path == "/api/clipboard/sync":
                self._post_sync()
            elif path == "/api/file64":
                self._post_file64(parsed.query)
            elif path == "/api/file":
                self._post_file(parsed.query)
            else:
                self._send(404, "Not found")
        except Exception as e:  # noqa: BLE001
            self._send(500, f"服务端错误: {e}")

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        remaining, chunks = length, []
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _post_text(self):
        raw = self._read_body()
        if not raw:
            self._send(400, "内容为空")
            return
        text = ""
        try:
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                text = (data.get("text") or "").strip()
            elif isinstance(data, str):
                text = data.strip()
        except Exception:
            # 不是 JSON 就当纯文本收，快捷指令可以直接把剪贴板丢过来
            try:
                text = raw.decode("utf-8").strip()
            except UnicodeDecodeError:
                text = raw.decode("gbk", errors="replace").strip()
        if not text:
            self._send(400, "内容为空")
            return
        ok = set_clipboard_text(text)
        append_text_log(text)
        print(f"  ↳ 收到文本 {len(text)} 字，剪贴板写入{'成功' if ok else '失败'}")
        if ok:
            self._send(200, "ok")
        else:
            self._send(500, "剪贴板被其他程序占用，稍后再试")

    def _post_sync(self):
        """双向剪贴板同步：直接返回手机剪贴板应该变成的纯文本。"""
        raw = self._read_body()
        text = ""
        try:
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                text = data.get("text") or ""
                if not isinstance(text, str):
                    text = str(text)
            elif isinstance(data, str):
                text = data
        except Exception:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("gbk", errors="replace")

        result, action = sync_clipboard(text)
        self._send(200, result, "text/plain; charset=utf-8",
                   {"X-Sync-Action": action})

    def _save_stream(self, target):
        """流式写盘。同时兼容 Content-Length 和 chunked —— iOS 快捷指令常用后一种。"""
        received = 0
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        with open(target, "wb") as f:
            if "chunked" in te:
                while True:
                    line = self.rfile.readline(128).strip()
                    if not line:
                        break  # 连接已断，不能 continue，否则死循环
                    try:
                        size = int(line.split(b";")[0], 16)
                    except ValueError:
                        break
                    if size == 0:
                        while True:
                            trailer = self.rfile.readline(256)
                            if trailer in (b"\r\n", b"\n", b""):
                                break
                        break
                    remaining = size
                    while remaining > 0:
                        chunk = self.rfile.read(min(remaining, 1 << 20))
                        if not chunk:
                            break
                        f.write(chunk)
                        received += len(chunk)
                        remaining -= len(chunk)
                    # 用 readline 吃掉块尾换行：read(2) 在客户端只发 \n 时会多吞一个字节，
                    # 导致后续复用连接的请求全部错位
                    self.rfile.readline(8)
            else:
                length = int(self.headers.get("Content-Length") or 0)
                while received < length:
                    chunk = self.rfile.read(min(length - received, 1 << 20))
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
        return received

    def _post_file(self, query):
        params = urllib.parse.parse_qs(query)
        raw_name = (params.get("name", [""])[0] or "").strip()
        if not raw_name:
            # 快捷指令没带文件名时的兜底：用时间戳，扩展名等落盘后靠魔数补
            raw_name = "share_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = safe_name(raw_name)
        target = unique_path(INBOX_DIR, name)
        received = self._save_stream(target)

        # 没有扩展名的补一个，认出 HEIC 就顺手存份 JPG（Windows 双击才打得开）
        jpg = ""
        if received > 0:
            target = fix_ext(INBOX_DIR, target)
            jpg = heic_to_jpg(target)

        print(f"  ↳ 收到文件 {os.path.basename(target)} ({human_size(received)})")
        file_log(f"    >> saved {os.path.basename(target)} ({human_size(received)})")
        self._json({"ok": True, "saved": os.path.basename(target), "bytes": received})

        # 先回响应再提醒，手机端不会被弹窗拖慢
        if received > 0:
            try:
                schedule_alert(jpg or target)
            except Exception:
                pass

    def _post_file64(self, query):
        """接收 Base64 编码的文件，走 JSON 传递。

        iOS 快捷指令在「请求体 = 文件」时无法把循环项绑进去，改用 Base64
        塞进 JSON 字段可以完全绕开那个变量槽。
        """
        raw = self._read_body()
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, "不是合法 JSON")
            return
        if not isinstance(data, dict):
            self._send(400, "JSON 必须是对象")
            return

        b64 = str(data.get("data") or "")
        if not b64:
            self._send(400, "data 为空")
            return
        if b64.startswith("data:") and "," in b64[:200]:
            b64 = b64.split(",", 1)[1]

        try:
            payload = base64.b64decode(b64, validate=False)
        except Exception as exc:  # noqa: BLE001
            self._send(400, f"base64 解码失败: {exc}")
            return
        if not payload:
            self._send(400, "解码后内容为空")
            return

        params = urllib.parse.parse_qs(query)
        raw_name = (params.get("name", [""])[0] or "").strip()
        if not raw_name:
            raw_name = str(data.get("name") or "").strip()
        if not raw_name:
            raw_name = "share_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        target = unique_path(INBOX_DIR, safe_name(raw_name))
        try:
            with open(target, "wb") as f:
                f.write(payload)
        except OSError as exc:
            self._send(500, f"写入失败: {exc}")
            return

        # 没有扩展名的补一个，认出 HEIC 就顺手存份 JPG
        target = fix_ext(INBOX_DIR, target)
        jpg = heic_to_jpg(target)

        size = os.path.getsize(target)
        print(f"  ↳ 收到文件(b64) {os.path.basename(target)} ({human_size(size)})")
        file_log(f"    >> saved {os.path.basename(target)} ({human_size(size)}) [b64]")
        self._json({"ok": True, "saved": os.path.basename(target), "bytes": size})

        try:
            schedule_alert(jpg or target)
        except Exception:
            pass

    # -------- 剪贴板
    def _clip_info(self):
        snap = clipboard_snapshot()
        text = snap["text"]
        w, h = snap["size"]
        clipped = text[:20000]
        return {
            "seq": snap["seq"],
            "has_text": bool(text.strip()),
            "text": clipped,
            "truncated": len(text) > len(clipped),
            "length": len(text),
            "has_image": bool(snap["png"]),
            "image_size": f"{w} × {h}" if snap["png"] else "",
            "image_kb": round(len(snap["png"]) / 1024, 1) if snap["png"] else 0,
            "files": [
                {
                    "name": f["name"],
                    "size": f["size"],
                    "bytes": f["bytes"],
                    "is_dir": f["is_dir"],
                }
                for f in snap["files"]
            ],
        }

    def _clip_file(self, idx_str, seq_str):
        """仅在手机点击下载时才真正读取文件字节。"""
        snap = clipboard_snapshot()
        try:
            if int(seq_str) != snap["seq"]:
                self._send(409, "剪贴板已经变了，请刷新页面后再试")
                return
            item = snap["files"][int(idx_str)]
        except (ValueError, IndexError):
            self._send(404, "文件不存在或已失效")
            return

        target = item["path"]
        if item["is_dir"]:
            self._send(400, "这是文件夹，请用 LocalSend 发送")
            return
        if not os.path.isfile(target):
            self._send(404, "文件已被移动或删除")
            return
        print(f"  ↳ 手机取走剪贴板文件 {item['name']} ({item['size']})")
        self._stream_file(target, item["name"])

    def _stream_file(self, target: str, filename: str = ""):
        size = os.path.getsize(target)
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition",
            f"inline; filename*=UTF-8''{urllib.parse.quote(filename or os.path.basename(target))}",
        )
        self.end_headers()
        with open(target, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # -------- 文件列表 / 下载
    def _collect(self):
        """按文件夹分组返回，前端分段显示。每组内部按修改时间倒序。"""
        groups = []
        for folder in SHARE_DIRS:
            if not os.path.isdir(folder):
                continue
            tag = os.path.basename(folder)
            files = []
            for entry in os.scandir(folder):
                if not entry.is_file():
                    continue
                st = entry.stat()
                files.append({
                    "name": entry.name,
                    "size": human_size(st.st_size),
                    "mtime": st.st_mtime,
                    "time": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M"),
                    "path": f"{tag}/{entry.name}",
                })
            files.sort(key=lambda x: x["mtime"], reverse=True)
            for it in files:
                it.pop("mtime", None)
            groups.append({"name": tag, "files": files[:40]})
        return groups

    def _download(self, rel):
        rel = urllib.parse.unquote(rel)
        parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".", "..")]
        if len(parts) != 2:
            self._send(400, "路径不合法")
            return
        folder = os.path.join(BASE_DIR, parts[0])
        target = os.path.join(folder, parts[1])
        if os.path.commonpath([os.path.abspath(folder), os.path.abspath(target)]) != os.path.abspath(folder):
            self._send(403, "拒绝访问")
            return
        if not os.path.isfile(target):
            self._send(404, "文件不存在")
            return
        self._stream_file(target)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    _load_sync_state()

    ip = lan_ip()
    print("=" * 52)
    print(f"  iPhone ⇄ PC 互传服务 v{VERSION} 已启动")
    print("=" * 52)
    print(f"  手机浏览器打开:  http://{ip}:{PORT}")
    print(f"  共享目录:        {BASE_DIR}")
    print(f"  收件箱:          {INBOX_DIR}")
    # 装没装可选依赖，看这一行就知道；没装也不用管，其他功能都正常
    if not HEIC_SUPPORT:
        heic = "关（未装 pillow-heif，HEIC 原样保留）"
    elif not CFG.get("heic_to_jpg", True):
        heic = "关（config.json 里 heic_to_jpg 为 false）"
    else:
        heic = "开"
    print(f"  HEIC→JPG:        {heic}")
    print("  停止: Ctrl+C")
    print("=" * 52)
    try:
        with Server(("0.0.0.0", PORT), Handler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
