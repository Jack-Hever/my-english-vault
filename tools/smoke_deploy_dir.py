# -*- coding: utf-8 -*-
"""★ 本地冒烟：验证 deploy/ 目录能按云端方式起服务 ★

背景：部署失败，报 `/workspace/main.py` 不存在 —— 部署器自动探测选了
`python main.py`，而 deploy/ 的真实入口是 `web_server.py`。
「带 startCmd 重新部署」之前必须先证明这个启动方式真的能起来。

★ 本脚本自身踩过的两个判据坑（已修，防复发）★
 ① 响应头是**小写 `set-cookie`**：Starlette 的 MutableHeaders 用
    `key.lower()` 存 raw header。原先查 `"Set-Cookie"` 大小写不匹配
    ⇒ 误判「首屏没下发 cookie」⇒ 后续提交没带 cookie ⇒ 连锁报 404
    （判据假红，产品是好的）。取值一律用 `resp.getheader("set-cookie")`
    （http.client 的 getheader 大小写不敏感）。
 ② 云端 `/api/health` **刻意不返回 db_path**（app/main.py:262-267，
    避免把服务器目录结构泄露给访客）。原判据要求「必须含 db_path」
    ⇒ 假红。正确判据：云端返回 {ok:true, mode:"cloud"} 且**不含 db_path**。

用法：
  python tools/smoke_deploy_dir.py                # 跑 deploy/
  python tools/smoke_deploy_dir.py --dir <源码目录>  # 跑源码目录
  python tools/smoke_deploy_dir.py --inject       # 反向验证：把 cookie
        # 下发条件改回 `if is_api and is_new:`，确认判据会 FAIL，跑完自动还原
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DIR = REPO / "deploy"
PY = sys.executable

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("    [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                            ("  —— " + detail) if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port, proc, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def inject_bug(target_dir: Path) -> bool:
    """把 cookie 下发条件改回旧写法（只在 is_api 时下发）⇒ 首屏不再种 cookie。"""
    p = target_dir / "app" / "main.py"
    if not p.is_file():
        return False
    raw = p.read_bytes()
    needle = b"            if is_new:\r\n                cookie_value = cloud.tenant_cookie_header(tid)"
    alt = b"            if is_api and is_new:\r\n                cookie_value = cloud.tenant_cookie_header(tid)"
    if needle not in raw:
        needle = needle.replace(b"\r\n", b"\n")
        alt = alt.replace(b"\r\n", b"\n")
    if needle not in raw:
        print("    [注入失败] 没找到锚点")
        return False
    p.write_bytes(raw.replace(needle, alt, 1))
    print("    已注入旧写法：if is_api and is_new:")
    return True


def restore(target_dir: Path):
    p = target_dir / "app" / "main.py"
    bak = p.with_suffix(".py.bak_smoke")
    if bak.exists():
        p.write_bytes(bak.read_bytes())
        bak.unlink()
        print("    已还原 app/main.py")


def main() -> int:
    args = sys.argv[1:]
    target = Path(args[args.index("--dir") + 1]) if "--dir" in args else DEFAULT_DIR
    inject = "--inject" in args

    port = free_port()
    home = tempfile.mkdtemp(prefix="enp-smoke-")
    env = dict(os.environ)
    env["ENPRACTICE_MODE"] = "cloud"
    env["ENPRACTICE_HOME"] = home
    env["PORT"] = str(port)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("OPENAI_API_KEY", None)

    print("=== 起服务（cwd=%s, PORT=%d）===" % (target, port))
    if inject:
        bak = target / "app" / "main.py"
        (target / "app" / "main.py.bak_smoke").write_bytes(bak.read_bytes())
        if not inject_bug(target):
            return 1

    proc = subprocess.Popen([PY, "web_server.py"], cwd=str(target), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        if not wait_port(port, proc):
            proc.kill()
            print("    服务没起来，输出：")
            print(proc.stdout.read()[-3000:].decode("utf-8", "replace"))
            return 1
        print("    端口已监听\n")

        def req(method, path, body=None, cookie=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            h = {}
            if cookie:
                h["Cookie"] = cookie
            if body is not None:
                h["Content-Type"] = "application/json"
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            data = r.read()
            out = (r.status, r, data)
            c.close()
            return out

        print("[1] /api/health（云端刻意不返回 db_path）")
        st, r, body = req("GET", "/api/health")
        check("health 200", st == 200, "status=%s" % st)
        try:
            hj = json.loads(body)
        except Exception:
            hj = {}
        check("health 返回 ok:true", hj.get("ok") is True, body[:120].decode("utf-8", "replace"))
        check("health 返回 mode=cloud", hj.get("mode") == "cloud", str(hj.get("mode")))
        check("★ 云端 health 不泄露 db_path（刻意）", "db_path" not in hj, str(hj.keys()))

        print("\n[2] ★ 首屏 GET / 必须下发 enp_vid cookie")
        st, r, body = req("GET", "/")
        check("首屏 200", st == 200, "status=%s" % st)
        # ★ 取值口径：Starlette 的 MutableHeaders 用 key.lower() ⇒ 头名是小写
        raw_names = [k for k, _ in r.getheaders()]
        check("响应头键名是小写 set-cookie（口径记录）",
              "set-cookie" in raw_names and "Set-Cookie" not in raw_names,
              str(raw_names))
        sc = r.getheader("set-cookie") or ""      # getheader 大小写不敏感
        check("★ 首屏带 Set-Cookie", "enp_vid=" in sc, sc[:130])
        check("cookie 有 HttpOnly", "HttpOnly" in sc, sc[:160])
        cookie = ""
        for part in sc.split(";"):
            if part.strip().startswith("enp_vid="):
                cookie = part.strip()
        check("能解析出 cookie 值", bool(cookie), cookie[:60])

        print("\n[3] ★ 建练习 → 立刻提交必须 200")
        st, r, body = req("POST", "/api/practices", body=b"{}", cookie=cookie)
        check("POST /api/practices 200/201", st in (200, 201),
              "status=%s body=%s" % (st, body[:160].decode("utf-8", "replace")))
        pid = None
        try:
            pid = json.loads(body).get("id")
        except Exception:
            pass
        check("返回了 practice id", pid is not None, "id=%s" % pid)
        if pid:
            # ★ 必须带 user_text：空翻译会被业务层拒（400「翻译内容不能为空」），
            #   那说明记录找得到了（cookie 对了），不是 404。
            payload = json.dumps(
                {"user_text": "This is a test translation for the smoke check."},
                ensure_ascii=False).encode("utf-8")
            st2, r2, body2 = req("POST", "/api/practices/%d/submit" % pid,
                                 body=payload, cookie=cookie)
            check("★ POST submit 200（不是 404）", st2 == 200,
                  "status=%s body=%s" % (st2, body2[:200].decode("utf-8", "replace")))
            check("★ submit 返回里没有假评分字段缺失（能拿到 result 结构）",
                  st2 == 200 and b"id" in body2, body2[:120].decode("utf-8", "replace"))

        print("\n[4] 不带 cookie 的提交必须被拒（租户隔离）")
        if pid:
            st3, r3, body3 = req("POST", "/api/practices/%d/submit" % pid, body=b"{}")
            check("无 cookie 提交被拒", st3 in (400, 404), "status=%s" % st3)
        else:
            check("无 cookie 提交被拒（跳过：没有 id）", False, "前置步骤失败")

        print("\n[5] 部署目录不含敏感文件")
        for name in ("settings.json", "data.db"):
            check("无 %s" % name, not (target / name).exists())
        check("无 tenants/", not (target / "tenants").exists())

    finally:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:
            pass
        if inject:
            restore(target)

    print("\n" + "=" * 60)
    print("总计：%d PASS / %d FAIL（共 %d 项）"
          % (len(PASS), len(FAIL), len(PASS) + len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - " + f)
    print("=" * 60)
    if inject:
        # 反向验证：注入旧写法后，判据必须 FAIL
        if FAIL:
            print("\n★ 反向验证成功：注入旧写法后判据 FAIL（判据有效）")
            return 0
        print("\n★ 反向验证失败：注入了旧写法，判据却全绿 ⇒ 判据无效！")
        return 1
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
