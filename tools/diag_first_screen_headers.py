# -*- coding: utf-8 -*-
"""最小复现：首屏到底有没有下发 Set-Cookie？打印全部响应头。"""
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
    env["ENPRACTICE_HOME"] = tempfile.mkdtemp(prefix="enp-hdr-")
    env["PORT"] = str(port)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("OPENAI_API_KEY", None)

    proc = subprocess.Popen([PY, "web_server.py"], cwd=str(DEPLOY), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        t0 = time.time()
        up = False
        while time.time() - t0 < 60:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    up = True
                    break
            except OSError:
                time.sleep(0.3)
        if not up:
            print("服务没起来")
            return 1

        for label, path in (("首屏 GET /", "/"),
                            ("health GET /api/health", "/api/health"),
                            ("首屏 GET / (第二次)", "/")):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            c.request("GET", path)
            r = c.getresponse()
            r.read()
            print("\n=== %s ===" % label)
            print("  status:", r.status, r.reason)
            for k, v in r.getheaders():
                print("    %-22s %s" % (k, v[:160]))
            c.close()
    finally:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
