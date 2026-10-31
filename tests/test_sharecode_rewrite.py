# -*- coding: utf-8 -*-
"""★ 分享令牌（sharecode）注入的回归测试 ★

运行：python test_sharecode_rewrite.py
（脚本式，与本仓库其它 test_*.py 一致；不用 pytest）

★ 这套测试防的是三个真实根因（2026-10-24 公网白屏 / 首屏 500）★
  ① 网关**消费**掉 query 里的 sharecode ⇒ 后端收不到令牌
     ⇒ 无法给 HTML/CSS 里的资源引用注入 ⇒ 浏览器请求裸名资源
     ⇒ 网关 403「链接不完整」⇒ 页面无样式、JS 全挂 ⇒ 白屏。
  ② ASGI 中间件缓冲改写：「body 先于 start」在部分 uvicorn 上抛异常
     ⇒ 公网首屏 500；且不同 starlette 版本的 body 帧序列不同，
     用「more_body=False」判定收齐会提前收齐（内容被截断）。
  ③ 新版 starlette 的 http.response.pathsend 扩展让 FileResponse
     绕过 http.response.body ⇒ 中间件收不到内容 ⇒ 永远改写不了。

★ 现在的实现：注入在**静态文件读取层**（app/sharecode_static.py）★
  文件读完 → 改写 → 交回普通 Response ⇒ start / content-length
  都由 starlette 正常生成 ⇒ ② ③ 两个坑从结构上消失。

★ 三层验证 ★
  L1 单元级：rewrite_html / rewrite_css / resolve_code 的行为
  L2 集成级：真实起服务，HTML 与 CSS 里的相对引用都带令牌
  L3 反向验证：注入旧写法（去掉 env 兜底 / 去掉注入）⇒ 判据必须 FAIL
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# ★ 必须先设云端模式再 import app.* ★
os.environ["ENPRACTICE_MODE"] = "cloud"
os.environ.setdefault("ENPRACTICE_SHARECODE", "IKDSXDUEGT")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    d = str(detail) if detail else ""
    print("    [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                            ("  —— " + d) if d else ""))


print("=" * 68)
print("[L1] 单元级：rewrite_html / rewrite_css / resolve_code")
print("=" * 68)

from app import sharecode  # noqa: E402

html = ('<link rel="stylesheet" href="style.css">'
        '<img src="assets/x.jpg">'
        '<script src="app.js"></script>'
        '<script>var s = \'<img src="y.jpg">\';</script>')
out = sharecode.rewrite_html(html, "ABCDEFGH")
check("L1-① href 注入令牌", 'href="style.css?sharecode=ABCDEFGH"' in out, out[:120])
check("L1-① img src 注入令牌", 'src="assets/x.jpg?sharecode=ABCDEFGH"' in out)
check("L1-① script src 注入令牌", 'src="app.js?sharecode=ABCDEFGH"' in out)
check("L1-① 内联脚本正文不被污染", "var s = '<img src=\"y.jpg\">';" in out, out[-90:])
check("L1-① 绝对 URL 不动",
      sharecode.rewrite_html('<img src="https://x.com/a.jpg">', "T")
      == '<img src="https://x.com/a.jpg">')
check("L1-① 已带令牌的不重复注入",
      sharecode.rewrite_html('<img src="a.jpg?sharecode=Z">', "T")
      == '<img src="a.jpg?sharecode=Z">')

css = 'body{background:url(assets/a.jpg)} .b{background:url("assets/b.jpg")}'
cout = sharecode.rewrite_css(css, "ABCDEFGH")
check("L1-② CSS url() 注入令牌（无引号）",
      "url(assets/a.jpg?sharecode=ABCDEFGH)" in cout, cout[:120])
check("L1-② CSS url() 注入令牌（带引号）",
      'url("assets/b.jpg?sharecode=ABCDEFGH")' in cout, cout[-60:])
check("L1-② data: URI 不动",
      sharecode.rewrite_css('x{background:url(data:image/png;base64,AA)}', "T")
      == 'x{background:url(data:image/png;base64,AA)}')

code, source = sharecode.resolve_code(
    {"query_string": b"sharecode=FROMQUERY", "headers": []})
check("L1-③ resolve_code 从 query 取码", code == "FROMQUERY" and source == "query",
      "%s/%s" % (code, source))
code, source = sharecode.resolve_code({
    "query_string": b"",
    "headers": [(b"x-original-uri", b"/?sharecode=FROMHEADER")]})
check("L1-③ resolve_code 从网关透传头取码",
      code == "FROMHEADER" and source == "header", "%s/%s" % (code, source))
code, source = sharecode.resolve_code({"query_string": b"", "headers": []})
check("L1-③ resolve_code 从环境变量兜底",
      code == "IKDSXDUEGT" and source == "env", "%s/%s" % (code, source))
code, source = sharecode.resolve_code(
    {"query_string": b"", "headers": []})
os.environ.pop("ENPRACTICE_SHARECODE", None)
code, source = sharecode.resolve_code({"query_string": b"", "headers": []})
check("L1-③ 三路都无 ⇒ 明确返回 none", code == "" and source == "none",
      "%s/%s" % (code, source))
os.environ["ENPRACTICE_SHARECODE"] = "IKDSXDUEGT"


# ═══════════════════════════════════════════════════════════════════════════
# L2  集成级：真实起服务
# ═══════════════════════════════════════════════════════════════════════════
print()
print("=" * 68)
print("[L2] 集成级：真实起服务，检查 HTML/CSS 的资源引用都带令牌")
print("=" * 68)

import http.client  # noqa: E402

PY = sys.executable


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _req(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request("GET", path)
    r = c.getresponse()
    data = r.read()
    st = r.status
    hd = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return st, hd, data


def _serve(directory):
    port = free_port()
    env = dict(os.environ)
    env["ENPRACTICE_MODE"] = "cloud"
    env["ENPRACTICE_HOME"] = tempfile.mkdtemp(prefix="enp-sc-")
    env["PORT"] = str(port)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("OPENAI_API_KEY", None)
    env.setdefault("ENPRACTICE_SHARECODE", "IKDSXDUEGT")
    proc = subprocess.Popen([PY, "web_server.py"], cwd=str(directory), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    t0 = time.time()
    while time.time() - t0 < 60:
        if proc.poll() is not None:
            return None, proc
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return port, proc
        except OSError:
            time.sleep(0.3)
    return None, proc


for label, directory in (("源码目录", REPO), ("部署目录", REPO / "deploy")):
    if not (directory / "web_server.py").is_file():
        check("%s 存在 web_server.py" % label, False)
        continue
    port, proc = _serve(directory)
    try:
        if not port:
            check("%s 服务能起来" % label, False)
            continue
        st, hd, body = _req(port, "/")
        html = body.decode("utf-8", "replace")
        check("%s 首屏 200" % label, st == 200, "status=%s" % st)
        check("★ %s HTML 的 style.css 引用带令牌" % label,
              "style.css?sharecode=" in html,
              next((l.strip()[:90] for l in html.splitlines()
                    if "style.css" in l), ""))
        check("★ %s HTML 的 app.js 引用带令牌" % label,
              "app.js?sharecode=" in html)
        check("★ %s 首屏带 enp_vid cookie" % label,
              "enp_vid=" in (hd.get("set-cookie") or ""),
              (hd.get("set-cookie") or "")[:110])
        check("★ %s 诊断头标出令牌来源" % label,
              hd.get("x-enp-sc-source") in ("query", "header", "env"),
              str(hd.get("x-enp-sc-source")))
        check("★ %s 资源 URL 带版本戳（绕开 CDN 旧缓存）" % label,
              "&v=" in html,
              next((l.strip()[:100] for l in html.splitlines()
                    if "style.css" in l), ""))
        check("★ %s HTML 不许被 CDN 缓存（no-store）" % label,
              "no-store" in (hd.get("cache-control") or ""),
              str(hd.get("cache-control")))
        check("★ %s 响应没有旧 etag（否则会拿 304 继续用旧内容）" % label,
              "etag" not in hd, str(hd.get("etag")))
        check("★ %s content-length 与实际字节一致" % label,
              int(hd.get("content-length") or -1) == len(body),
              "头=%s 实际=%d" % (hd.get("content-length"), len(body)))

        st2, hd2, css = _req(port, "/style.css")
        check("★ %s CSS 的 url() 带令牌" % label,
              b"sharecode=" in css, "字节=%d" % len(css))
        check("★ %s CSS 的 url() 也带版本戳" % label,
              b"&v=" in css, "字节=%d" % len(css))
        check("★ %s CSS 的 content-length 一致" % label,
              int(hd2.get("content-length") or -1) == len(css),
              "头=%s 实际=%d" % (hd2.get("content-length"), len(css)))

        # ★ build 指纹：云端**不再回显**（B3 加固）★
        #   旧契约「health 的 build == 本地指纹」已作废：指纹是部署对账
        #   用的版本戳，公网回显它等于免费告诉攻击者「这是哪一版代码」。
        #   新契约 = 云端 health 响应里根本不出现 build 键（mode 仍在）。
        #   「跑的哪一版代码」的对账改道：web_server.py 启动横幅的语料
        #   指纹 + tools/verify_public_deploy.py 的本地指纹比对。
        st3, hd3, body3 = _req(port, "/api/health")
        try:
            hj = json.loads(body3)
        except Exception:
            hj = {}
        check("★ %s 云端 health 不再回显 build 指纹（B3）" % label,
              "build" not in hj and hj.get("mode") == "cloud",
              "health=%s" % body3[:120])
        check("★ %s 云端 health 只回 ok/mode（最小面）" % label,
              set(hj.keys()) <= {"ok", "mode"} and hj.get("ok") is True,
              "keys=%s" % sorted(hj.keys()))
    finally:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# L3  反向验证：注入旧写法 ⇒ 判据必须 FAIL
# ═══════════════════════════════════════════════════════════════════════════
print()
print("=" * 68)
print("[L3] 反向验证：注入旧写法，判据必须 FAIL")
print("=" * 68)

sp = REPO / "app" / "sharecode.py"
orig = sp.read_bytes()
patched = orig.replace(
    b'code = normalize_code(os.environ.get("ENPRACTICE_SHARECODE", ""))',
    b'code = ""')
if patched == orig:
    check("L3 env 兜底锚点找到", False, "sharecode.py 已改版，锚点失效")
else:
    sp.write_bytes(patched)
    try:
        for m in list(sys.modules):
            if m.startswith("app.sharecode"):
                del sys.modules[m]
        # ★ 必须用 importlib.import_module，不能写 `from app import sharecode` ★
        #   后者取的是 **app 包对象上的属性**，而那个属性还指着旧模块对象
        #   ⇒ 注入的源码根本没被执行 ⇒ 反向验证就是假的。
        import importlib
        sc2 = importlib.import_module("app.sharecode")
        code, source = sc2.resolve_code({"query_string": b"", "headers": []})
        check("★ L3-① 旧写法下 env 兜底消失 ⇒ 取不到令牌（判据能 FAIL）",
              code == "" and source == "none", "%s/%s" % (code, source))
    finally:
        sp.write_bytes(orig)
        for m in list(sys.modules):
            if m.startswith("app.sharecode"):
                del sys.modules[m]
        importlib.import_module("app.sharecode")
    print("    （已还原 app/sharecode.py）")

# L3-②：把静态文件层的注入整个去掉 ⇒ 集成级判据必须 FAIL
st_p = REPO / "app" / "sharecode_static.py"
orig2 = st_p.read_bytes()
patched2 = orig2.replace(
    b'        code, source = sharecode.resolve_code(scope)',
    b'        code, source = "", "none"')
if patched2 == orig2:
    check("L3 注入锚点找到", False, "sharecode_static.py 已改版，锚点失效")
else:
    st_p.write_bytes(patched2)
    try:
        port, proc = _serve(REPO)
        if not port:
            check("★ L3-② 去掉注入后服务能起来（判据能 FAIL）", False)
        else:
            st, hd, body = _req(port, "/")
            html = body.decode("utf-8", "replace")
            check("★ L3-② 去掉注入后 HTML 不再带令牌（判据能 FAIL）",
                  "style.css?sharecode=" not in html,
                  "仍带令牌 ⇒ 判据无效")
    finally:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:
            pass
        st_p.write_bytes(orig2)
    print("    （已还原 app/sharecode_static.py）")

print()
print("=" * 68)
print("总计：%d PASS / %d FAIL（共 %d 项）"
      % (len(PASS), len(FAIL), len(PASS) + len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
print("=" * 68)
sys.exit(1 if FAIL else 0)
