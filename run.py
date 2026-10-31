"""启动入口：后台起 FastAPI，再用 pywebview 打开单窗口桌面界面。

打包为单文件 exe 后无控制台，任何启动失败都会表现为「双击没反应」。
因此本入口把所有关键步骤写入日志文件，并在致命失败时弹窗告警，
保证问题可见、可诊断。
"""

from __future__ import annotations

import ctypes
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8765
PORT_TRIES = 20
WINDOW_TITLE = "我的英文库 MyEnglishVault"

LOG_PATH = Path(os.environ.get("TEMP", str(Path.home()))) / "EnPractice-startup.log"


def _open_log():
    """打开日志追加写。记事本按 ANSI 解析无 BOM 的 UTF-8 会乱码，
    首次创建时带 BOM，之后纯 UTF-8 追加（避免行中嵌 BOM）。"""
    encoding = "utf-8-sig" if not LOG_PATH.exists() else "utf-8"
    return LOG_PATH.open("a", encoding=encoding, buffering=1)


def _log(message: str) -> None:
    """写入启动日志；若带控制台（源码运行）同时打印。"""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    try:
        with _open_log() as f:
            f.write(line + "\n")
    except OSError:
        pass
    if sys.stdout is not None and not getattr(sys, "frozen", False):
        print(line, flush=True)


def _alert(title: str, text: str) -> None:
    """Windows 弹窗告警（无控制台环境下唯一的用户可见通道）。"""
    _log(f"ALERT {title}: {text}")
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x30)  # MB_ICONWARNING
    except Exception:
        pass


def _ensure_std_streams() -> None:
    """windowed 打包双击启动（无控制台）时 sys.stdout/stderr 为 None，
    uvicorn 初始化日志格式器会检查 sys.stdout.isatty() 直接崩溃；
    把它们接到日志文件，同时让后续所有库的 std 访问都有落点。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        sink = _open_log()
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = sink
    if sys.stderr is None:
        sys.stderr = sink


def pick_port(host: str, start: int, tries: int = PORT_TRIES) -> int | None:
    """从 start 开始找第一个未被占用的端口，避免硬编码端口冲突。"""
    for port in range(start, start + tries):
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex((host, port)) != 0:
                return port
    return None


def wait_port(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def serve(port: int) -> None:
    import uvicorn

    from app.main import app

    try:
        uvicorn.run(app, host=HOST, port=port, log_level="warning")
    except Exception:
        _log("后端线程异常：\n" + traceback.format_exc())


def main() -> int:
    _ensure_std_streams()
    _log(f"===== 启动 {WINDOW_TITLE}（frozen={getattr(sys, 'frozen', False)}）=====")

    # 1. 初始化数据库（首次运行/旧库迁移都可能在这里失败）
    try:
        from app import db
        from app.config import db_path

        db.init_db()
        _log(f"数据库就绪：{db_path()}")
    except Exception:
        _log("数据库初始化失败：\n" + traceback.format_exc())
        _alert(
            f"{WINDOW_TITLE} - 启动失败",
            "数据库初始化失败，软件无法启动。\n\n"
            f"详细原因已写入日志文件：\n{LOG_PATH}\n\n"
            "可尝试删除该文件所在目录的数据文件后重试；"
            "若反复失败，请把日志内容反馈给开发者。",
        )
        return 1

    # 2. 选择端口
    port = pick_port(HOST, PORT)
    if port is None:
        _alert(
            f"{WINDOW_TITLE} - 启动失败",
            f"端口 {PORT}-{PORT + PORT_TRIES - 1} 均被占用，无法启动本地服务。\n\n"
            "请先关闭可能占用端口的程序（如其他本地服务、VPN 工具）后重试。",
        )
        return 1
    _log(f"选定端口：{port}")

    # 3. 启动后端线程并等待就绪
    threading.Thread(target=serve, args=(port,), daemon=True).start()
    if not wait_port(HOST, port, timeout=20.0):
        _log("后端 20 秒未就绪")
        _alert(
            f"{WINDOW_TITLE} - 启动失败",
            "本地服务启动超时，软件无法打开。\n\n"
            f"详细原因已写入日志文件：\n{LOG_PATH}\n\n"
            "常见原因：杀毒软件拦截、端口被其他程序占用。",
        )
        return 1
    _log("后端就绪")

    url = f"http://{HOST}:{port}"
    _log(f"服务地址：{url}")

    # 4. 打开桌面窗口；失败则降级系统浏览器，并明确告知用户
    try:
        import webview

        _log(f"webview 后端：{getattr(webview, 'render_mode', 'unknown')}")
        webview.create_window(
            WINDOW_TITLE,
            url,
            width=1160,
            height=760,
            min_size=(900, 600),
            # pywebview 的 text_select 默认 False，会在页面加载时往 document.head
            # 注入 body{-webkit-user-select:none;user-select:none;cursor:default}，
            # 于是英文正文、例句、译文一个字都选不中，用户没法划词复制。
            # 置 True 关掉这次注入，改由 web/style.css 的「文本选择策略」段自己管：
            # 默认不可选（避免拖动时把按钮、标签页一起刷蓝），只放开内容区。
            # 自己管的好处是规则落在仓库里可见可改，不依赖第三方注入时机。
            text_select=True,
        )
        # ★ private_mode=False + 显式 storage_path ★
        #   pywebview 默认 private_mode=True（隐私模式）：WebView2 的 localStorage
        #   不落盘，且每次加载前 DeleteAllCookies。新手引导的完成标记
        #   （web/onboarding.js 的 enp_onboarded）恰恰存在 localStorage，
        #   于是每次启动都被清空 → 引导每次都弹（用户实测反馈）。
        #   关掉隐私模式后必须显式钉住数据目录：不传 storage_path 会落到
        #   %APPDATA%\pywebview（与本项目数据目录脱节）；这里钉到
        #   数据目录下的 webview/ 子目录，与 data.db、settings.json 同级。
        #   安全性：桌面分支的 AI 配置只走 settings.json（app.js 的 saveSettings
        #   按 isCloud() 分流，localStorage 只有云端分支才写），持久化不会让
        #   任何浏览器侧状态反过来影响桌面配置。
        storage = Path(db_path()).parent / "webview"
        try:
            storage.mkdir(parents=True, exist_ok=True)
        except OSError:
            storage = None  # 目录建不出来就退回 pywebview 默认位置，别挡启动
        _log(f"webview 用户数据目录：{storage or '（默认）'}")
        if storage is not None:
            webview.start(private_mode=False, storage_path=str(storage))
        else:
            webview.start(private_mode=False)
        _log("窗口正常关闭，退出")
        return 0
    except Exception as exc:
        _log("桌面窗口启动失败：\n" + traceback.format_exc())
        opened = False
        try:
            opened = webbrowser.open(url)
        except Exception:
            opened = False
        if opened:
            _alert(
                f"{WINDOW_TITLE}",
                "桌面窗口无法启动（可能缺少 WebView2 运行时），\n"
                f"已改用系统浏览器打开：\n{url}\n\n"
                "软件窗口关闭后，本地服务也随之停止。",
            )
        else:
            _alert(
                f"{WINDOW_TITLE} - 启动失败",
                "桌面窗口与浏览器均无法打开界面。\n\n"
                f"请手动在浏览器访问：{url}\n"
                f"日志文件：{LOG_PATH}",
            )
            return 1
        # 浏览器兜底：保持进程存活以维持本地服务
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            _log("收到退出信号")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        _log("启动器顶层异常：\n" + traceback.format_exc())
        _alert(
            f"{WINDOW_TITLE} - 启动失败",
            "软件启动时发生未预期的错误。\n\n"
            f"详细原因已写入日志文件：\n{LOG_PATH}\n\n"
            "请把日志内容反馈给开发者。",
        )
        raise SystemExit(1)
