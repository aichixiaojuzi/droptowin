#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DropToWin（随手传）服务保活守护。

每 30 秒检查一次 8899 端口，服务不在了就拉起来。
自身用 8897 端口做单实例锁，重复启动会直接退出。
重启记录写到同目录的 watchdog.log，方便排查。

用 pythonw 运行无窗口。
"""

import os
import socket
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "droptowin.py")
LOGFILE = os.path.join(HERE, "watchdog.log")

SERVICE_PORT = 8899
LOCK_PORT = 8897
CHECK_INTERVAL = 30
STARTUP_WAIT = 12      # 开机后磁盘繁忙，服务启动可能要十几秒
RESTART_COOLDOWN = 40  # 距上次拉起不足这么久，就再等等，别重复拉起


def log(msg: str):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n"
    try:
        # 超过 200KB 就只保留最后 200 行，防止长期运行把日志撑爆
        if os.path.exists(LOGFILE) and os.path.getsize(LOGFILE) > 200_000:
            with open(LOGFILE, "r", encoding="utf-8", errors="ignore") as f:
                tail = f.readlines()[-200:]
            with open(LOGFILE, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def port_open(port: int, timeout: float = 1.5) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def acquire_lock():
    """绑定本地端口作为单实例锁。拿到就返回 socket（需一直持有），否则 None。

    注意：Windows 上 SO_REUSEADDR 会允许重复绑定同一端口，起不到互斥作用，
    必须改用 SO_EXCLUSIVEADDRUSE 才是真正的独占。
    """
    s = socket.socket()
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except OSError:
            pass
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def start_service():
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    # 把子进程的输出留下来，不然崩了完全看不到原因
    outpath = os.path.join(HERE, "droptowin.out")
    try:
        # 超过 2MB 就只留最后 500KB，防止反复崩溃把磁盘写满
        if os.path.exists(outpath) and os.path.getsize(outpath) > 2_000_000:
            with open(outpath, "rb") as f:
                f.seek(-500_000, os.SEEK_END)
                tail = f.read()
            with open(outpath, "wb") as f:
                f.write(tail)
        out = open(outpath, "ab", buffering=0)
    except OSError:
        out = subprocess.DEVNULL
    env = dict(os.environ)
    # 双保险：强制子进程标准流用 UTF-8，避免中文 Windows GBK 环境下
    # 启动横幅里的特殊字符触发 UnicodeEncodeError 导致服务秒崩
    env["PYTHONIOENCODING"] = "utf-8"
    subprocess.Popen(
        [sys.executable, TARGET],
        cwd=HERE,
        creationflags=flags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=out,
        env=env,
    )


def main():
    lock = acquire_lock()
    if lock is None:
        log("已有守护在运行，本次启动退出")
        return

    log(f"守护启动 pid={os.getpid()}")
    fails = 0
    last_start = 0.0

    while True:
        if port_open(SERVICE_PORT):
            if fails:
                log("服务健康，重置失败计数")
            fails = 0
            time.sleep(CHECK_INTERVAL)
            continue

        # 服务没在监听。刚拉起过就先给一轮时间，别急着重复拉起
        if last_start and (time.time() - last_start) < RESTART_COOLDOWN:
            time.sleep(STARTUP_WAIT)
            if port_open(SERVICE_PORT):
                log("服务已恢复")
                fails = 0
            else:
                fails += 1
                log(f"启动仍未完成，继续等待（第 {fails} 轮）")
            time.sleep(CHECK_INTERVAL)
            continue

        log("检测到服务不在，正在拉起")
        try:
            start_service()
            last_start = time.time()
        except Exception as exc:  # noqa: BLE001
            log(f"拉起失败: {exc}")

        time.sleep(STARTUP_WAIT)
        if port_open(SERVICE_PORT):
            log("服务已恢复")
            fails = 0
        else:
            fails += 1
            log(f"拉起后仍未监听，连续失败 {fails} 次")

        # 连续失败时逐步退避，避免疯狂重启
        time.sleep(CHECK_INTERVAL + min(fails, 5) * 30)


if __name__ == "__main__":
    main()
