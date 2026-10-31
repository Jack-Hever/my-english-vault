# -*- coding: utf-8 -*-
"""本地对比：GET / 与 GET /?sharecode=X 的字节数差异（查 rewrite_html 是否注入）。

背景：公网标准 URL 返回 59113（旧内容：含「提交回译」），加 &_=随机 参数返回
59107（本地新产物）。两个数字正好差 6。
假设：sharecode_mw 的 rewrite_html() 会往 HTML 里注入东西（sharecode 到某个
资源 URL 上）⇒ 带 sharecode 的响应比裸 HTML 多几字节 ⇒ 59107 + 6 = 59113。

★ 必须本地验证，不能靠推理 ★
"""
import http.client
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
PY = sys.executable


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    port = free_port()
    env = dict(os.environ)
    env["ENPRACTICE_MODE"] = "cloud"
    env["ENPRACTICE_HOME"] = tempfile.mkdtemp(prefix="enp-sc-")
    env["PORT"] = str(port)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("OPENAI_API_KEY", None)

    proc = subprocess.Popen([PY, "web_server.py"], cwd=str(DEPLOY), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        t0 = time.time()
        while time.time() - t0 < 60:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(0.3)

        cases = [
            ("GET /                      ", "/"),
            ("GET /?sharecode=IKDSXDUEGT ", "/?sharecode=IKDSXDUEGT"),
            ("GET /?sharecode=IKDSXDUEGT&_=1", "/?sharecode=IKDSXDUEGT&_=1"),
            ("GET /style.css             ", "/style.css"),
            ("GET /style.css?sharecode=X ", "/style.css?sharecode=IKDSXDUEGT"),
        ]
        bodies = {}
        for label, path in cases:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            c.request("GET", path)
            r = c.getresponse()
            data = r.read()
            bodies[path] = data
            txt = data.decode("utf-8", "replace")
            marks = []
            if "style.css?sharecode=" in txt:
                marks.append("html含style.css?sharecode=")
            if "回译" in txt:
                marks.append("★含「回译」（旧文案）")
            if "提交回译" in txt:
                marks.append("★含「提交回译」（旧文案）")
            print("  %-34s %6d 字节  %s"
                  % (label, len(data), "  ".join(marks)))
            c.close()

        a = bodies["/"]
        b = bodies["/?sharecode=IKDSXDUEGT"]
        print()
        print("裸 HTML %d 字节 / 带 sharecode %d 字节 / 差 %+d"
              % (len(a), len(b), len(b) - len(a)))
        if a != b:
            la = a.decode("utf-8", "replace").splitlines()
            lb = b.decode("utf-8", "replace").splitlines()
            import difflib
            d = [l for l in difflib.unified_diff(la, lb, "plain", "sharecode",
                                                 lineterm="", n=1)
                 if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
            print("差异行：")
            for l in d[:20]:
                print("   " + l[:180])
    finally:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
