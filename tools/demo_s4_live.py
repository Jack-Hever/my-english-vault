"""S4 现场演示：真实起服务、真实 HTTP、真实磁盘 grep。

★ 为什么它进仓库 ★
S4 的两条核心验收（SSRF 闸门 / 密钥 per-visitor 隔离）必须能在**干净的仓库
状态**下复现。test_cloud_security.py 是「断言版」（可自动跑、可进 CI）；
本脚本是「现场版」——真起桌面服务与云端服务、真发 HTTP、真在磁盘上 grep，
用来在评审前亲眼看一遍结论。

★ 桌面那一次取句是**真的出网** ★
脚本在本机回环上起一个假的「外刊站」，桌面版对它发起**真实** requests.get
（没有任何打桩）→ 200 + 真句子；然后用**同一个 URL** 打云端 → 400。
同一个地址，桌面能取、云端被拦 —— 这比「打桩证明」强得多。

★ 用法 ★
    python tools/demo_s4_live.py
（本机若设了 HTTP_PROXY 也不影响：脚本对本地请求显式 trust_env=False。）

"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

# ★★★ 关键：本机环境设了 HTTP_PROXY / HTTPS_PROXY（沙箱代理）★★★
#   httpx 默认信任这些环境变量，于是对 127.0.0.1 的请求也会走代理；
#   代理把 absolute-form 的请求目标原样转给源站，uvicorn/httptools
#   会把它百分号编码成路径（http%3A//127.0.0.1%3A8897/...）→ 404。
#   现象是"第 1 个请求正常、第 2 个开始 404"（第 2 个开始复用了代理连接）。
#   跟产品无关：跟本机对话必须 trust_env=False（不走代理）。
LOCAL_CLIENT_KW = {"timeout": 20, "trust_env": False}

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
TMP = Path(r"C:\tmp")
DEMO_HOME = TMP / "enp-s4-demo"
CLOUD_HOME = DEMO_HOME / "cloud"
DESKTOP_HOME = DEMO_HOME / "desktop"
DUMMY_KEY = "dummy-test-key-123"
METADATA_URL = "http://169.254.169.254/latest/meta-data/"

ARTICLE_HTML = (
    "<html><head><title>Demo Foreign Article</title></head><body>"
    "<p>Reading every day builds a habit that lasts a lifetime.</p>"
    "<p>Small steps compound into big results over a long time.</p>"
    "</body></html>"
)


def banner(title):
    print("\n" + "=" * 74)
    print(f"  {title}")
    print("=" * 74)


def wait_port(port, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def start_server(cmd, env_extra, port, log_path):
    env = dict(os.environ)
    env.update(env_extra)
    env["PYTHONIOENCODING"] = "utf-8"
    log = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(cmd, cwd=str(REPO), env=env, stdout=log,
                            stderr=subprocess.STDOUT)
    if not wait_port(port):
        proc.kill()
        raise SystemExit(f"服务没起来：{cmd}\n{log_path} 内容：\n"
                         f"{Path(log_path).read_text(encoding='utf-8', errors='replace')}")
    return proc, log


class _ArticleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = ARTICLE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def main():
    if DEMO_HOME.exists():
        shutil.rmtree(DEMO_HOME, ignore_errors=True)
    DEMO_HOME.mkdir(parents=True, exist_ok=True)

    # ---------- 0. 起一个"外刊站"（本机回环） ----------
    article_srv = ThreadingHTTPServer(("127.0.0.1", 0), _ArticleHandler)
    article_port = article_srv.server_address[1]
    import threading
    threading.Thread(target=article_srv.serve_forever, daemon=True).start()
    article_url = f"http://127.0.0.1:{article_port}/article.html"
    print(f"[准备] 外刊站已起：{article_url}（本机回环，代表一个真实外部站点）")

    # ---------- 1. 桌面服务 ----------
    banner("① 桌面模式：web-pick-sentences 真实取句（逐字不变）")
    d_log = DEMO_HOME / "desktop.log"
    d_proc, d_logh = start_server(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", "8896", "--log-level", "warning"],
        {"ENPRACTICE_HOME": str(DESKTOP_HOME), "PORT": "8896"},
        8896, d_log)

    with httpx.Client(**LOCAL_CLIENT_KW) as client:
        r = client.post("http://127.0.0.1:8896/api/articles/web-pick-sentences",
                        json={"url": article_url, "terms": ["habit"]})
        print(f"请求：POST /api/articles/web-pick-sentences")
        print(f"      url    = {article_url}")
        print(f"      terms  = ['habit']")
        print(f"响应：HTTP {r.status_code}")
        body = r.json()
        print(f"      正文   = {json.dumps(body, ensure_ascii=False)[:400]}")
        print(f"结论：桌面 {'✔ 正常取句（真实出网，未被拦截）' if r.status_code == 200 else '✘ 异常'}")
        print(f"      Set-Cookie = {r.headers.get('set-cookie')!r}  (桌面必须为空)")
        print(f"      桌面 tenants/ 存在 = {(DESKTOP_HOME / 'tenants').exists()}  (桌面必须为 False)")

    # ---------- 2. 云端服务 ----------
    banner("② 云端模式：web-pick-sentences 一律 400（SSRF 闸门）")
    c_log = DEMO_HOME / "cloud.log"
    c_proc, c_logh = start_server(
        [PY, "web_server.py"],
        {"ENPRACTICE_HOME": str(CLOUD_HOME), "PORT": "8897",
         "ENPRACTICE_MODE": "cloud", "ENPRACTICE_WEB_SEARCH": "0"},
        8897, c_log)

    tid = ""
    with httpx.Client(**LOCAL_CLIENT_KW) as client:
        for url, label in ((article_url, "内网地址（同一个桌面能用的 URL）"),
                           (METADATA_URL, "云厂商元数据地址")):
            r = client.post("http://127.0.0.1:8897/api/articles/web-pick-sentences",
                            json={"url": url, "terms": ["habit"]})
            print(f"请求：POST /api/articles/web-pick-sentences   [{label}]")
            print(f"      url    = {url}")
            print(f"响应：HTTP {r.status_code}")
            try:
                detail = r.json().get("detail", "")
            except Exception:
                detail = r.text
            print(f"      detail = {detail}")
            print(f"结论：云端 {'✔ 被拦下' if r.status_code == 400 else '✘ 没拦住！'}")

        # ---------- 3. 密钥隔离 ----------
        banner("③ AI 密钥 per-visitor 隔离")
        r = client.get("http://127.0.0.1:8897/api/ai/status",
                       headers={"X-Enp-Api-Key": DUMMY_KEY})
        print(f"请求：GET /api/ai/status   headers: X-Enp-Api-Key: {DUMMY_KEY}")
        print(f"响应：HTTP {r.status_code}   body = {r.text}")
        sc = r.headers.get("set-cookie") or ""
        import re
        m = re.search(r"enp_vid=([0-9a-f]{32})", sc)
        tid = m.group(1) if m else ""
        if not tid:
            # httpx 会持久化 cookie，Set-Cookie 只出现在该访客的**第一次**响应上，
            # 所以回落到 cookie 罐 / 租户目录。
            try:
                tid = client.cookies.get("enp_vid") or ""
            except Exception:
                tid = ""
        if not tid:
            subs = [d.name for d in (CLOUD_HOME / "tenants").glob("*") if d.is_dir()]
            tid = subs[0] if subs else ""
        print(f"      访客 id = {tid}")

        r2 = client.put("http://127.0.0.1:8897/api/settings",
                        json={"ai": {"api_key": DUMMY_KEY,
                                     "base_url": METADATA_URL,
                                     "model": "evil-model"}})
        print(f"\n请求：PUT /api/settings")
        print(f"      ai.base_url = {METADATA_URL}   ai.model = evil-model   ai.api_key = {DUMMY_KEY}")
        print(f"响应：HTTP {r2.status_code}")
        ai_cfg = r2.json().get("ai", {})
        print(f"      生效 base_url = {ai_cfg.get('base_url')}   ← 必须不是元数据地址")
        print(f"      生效 model    = {ai_cfg.get('model')}   ← 必须不是 evil-model")
        print(f"      回显 api_key  = {ai_cfg.get('api_key')!r}   ← 必须是脱敏占位")

    # ---------- 4. 磁盘 grep ----------
    banner("④ 密钥落盘核查：grep 整个访客目录")
    tdir = CLOUD_HOME / "tenants" / tid
    print(f"访客目录：{tdir}")
    print(f"目录内文件：{[p.name for p in sorted(tdir.rglob('*')) if p.is_file()]}")
    print(f"\n$ grep -r {DUMMY_KEY!r} {tdir}")
    grep_proc = subprocess.run(
        ["grep", "-r", DUMMY_KEY, str(tdir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = grep_proc.stdout.strip()
    print(out if out else "(无任何输出 —— 密钥没有落盘)")
    print(f"grep 退出码 = {grep_proc.returncode}（1 = 没找到，正是我们要的结果）")

    print(f"\n$ grep -r '169.254.169.254' {tdir}")
    g2 = subprocess.run(["grep", "-r", "169.254.169.254", str(tdir)],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace")
    o2 = g2.stdout.strip()
    print(o2 if o2 else "(无任何输出 —— 访客指定的出网地址也没有落盘)")
    print(f"grep 退出码 = {g2.returncode}")

    print(f"\n访客自己的 settings.json 内容：")
    print((tdir / "settings.json").read_text(encoding="utf-8"))

    # ---------- 5. 启动横幅 ----------
    banner("⑤ web_server.py 启动横幅（种子 md5）")
    c_logh.flush()
    print((DEMO_HOME / "cloud.log").read_text(encoding="utf-8", errors="replace"))

    # ---------- 清理 ----------
    banner("清理")
    for proc, logh, name in ((d_proc, d_logh, "桌面"), (c_proc, c_logh, "云端")):
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        logh.close()
        print(f"[清理] {name}服务已停止")
    article_srv.shutdown()
    print("[清理] 外刊站已停止")
    shutil.rmtree(DEMO_HOME, ignore_errors=True)
    print(f"[清理] 临时目录已删除：{DEMO_HOME}")


if __name__ == "__main__":
    main()
