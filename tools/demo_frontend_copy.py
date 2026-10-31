#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S4 前端文案实测驱动端：真起服务 → 真调后端 → 真浏览器渲染 → 读回文字。

★ 它干什么 ★
生成两份**真实**的 `POST /api/practices` 响应（云端一份、桌面一份），
交给 `tools/verify_p_missing_copy.js` 在真浏览器里渲染，读回 `#p-missing`
的实际文字，并拍 3 张截图。

★ 为什么非得真起服务 ★
「后端给了 web_search_note，前端会显示它」这句话，只有把两边都真跑起来
才算数。手写一段 HTML 快照再断言，等于自己证明自己。

★ 为什么选词是指定的 ★
种子语料 100% 可达，UI 的自动选词挑不出「缺失词」。所以直接给一个
**确实不在语料里**的词（zebra，实测 seed_data.py 里 0 处）。
「选哪个词」这一步是指定的，「后端返回什么」「前端渲染成什么」都是真的。

用法：
    python tools/demo_frontend_copy.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
NODE = "node"
NODE_PATH = "node_modules"
DRIVER = REPO / "tools" / "verify_p_missing_copy.js"

#: 实测 seed_data.py 里 0 处的词 → 建进词汇库后必然进 missing_terms。
MISSING_TERM = "zebra"

#: 一个**外刊库有句子**的词，与上面那个配对。
#: ★ 为什么必须配一个命中的词 ★
#   missing_terms 是「目标词在词汇库里、外刊库检索不到句子」。
#   若全部词都 missing，practices.py:329 `if not sentences: raise` 直接 400 ——
#   那样根本走不到 web_search_note 那一支，测的就不是我们要测的东西了。
#   探针 tools/find_missing_term.py 实测：种子 60 词**全部有句子**，
#   所以「缺失词」只能靠**新建一个**词条来造。
HIT_TERM = "habit"

# ★ 本机设了 HTTP_PROXY（沙箱代理），httpx 默认信任它。
#   对 127.0.0.1 的第 2 个及以后请求，代理会把 absolute-form 目标原样
#   转给源站 → uvicorn 百分号编码成路径 → 404。
#   现象极具迷惑性：第 1 个请求正常，第 2 个开始 404。
#   所以本地调用一律 trust_env=False。（已写进 MEMORY.md「探针与脚本纪律」）
LOCAL_CLIENT_KW = {"timeout": 30, "trust_env": False}


def _wait_ready(port: int, proc, log_path: Path, label: str) -> None:
    """等服务把 `/api/health` 答上。

    ★ 超时时把子进程自己的输出打出来 ★
    否则只会看到「等 20 秒未就绪」，完全不知道服务端报了什么。
    实测踩过：端口写错（服务在 8000、脚本等 8901）时，服务端日志里
    明明写着「Uvicorn running on http://0.0.0.0:8000」—— 早打出来
    就能一眼看出，不用再猜。

    ★ 子进程输出为什么落文件而不是 PIPE ★
    实测踩过：用 `stdout=PIPE` 且不读，子进程写满管道缓冲（约 64KB）
    就会**永久阻塞** —— 外层看起来是「脚本卡住」，实际是子进程在等
    有人把管道读空。uvicorn 的 access log 很快就写满。
    ⇒ 落临时文件，永远不阻塞；要读的时候再读。
    """
    import httpx
    url = f"http://127.0.0.1:{port}/api/health"
    for _ in range(80):
        if proc.poll() is not None:
            raise RuntimeError(f"[{label}] 进程已退出 rc={proc.returncode}"
                               + _drain(log_path))
        try:
            r = httpx.get(url, timeout=5, trust_env=False)
            if r.status_code == 200:
                print(f"    [INFO] {label} 就绪：{r.text[:160]}")
                return
        except Exception:                       # noqa: BLE001
            time.sleep(0.25)
    raise RuntimeError(f"[{label}] 等待 20 秒仍未就绪（端口 {port}）"
                       + _drain(log_path))


def _drain(log_path: Path) -> str:
    """把子进程日志文件读出来（尾部 1500 字）。"""
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
    except OSError:
        return ""
    return "\n---- 子进程输出 ----\n" + tail if tail else ""


def _seed_missing_entry(client, port: int) -> None:
    """先往词汇库里**真建**一个外刊库覆盖不到的词条。

    ★ 为什么非得先建词条 ★
    `missing_terms` 的语义是「目标词在词汇库里、外刊库检索不到句子」。
    探针实测（tools/find_missing_term.py）：种子 60 个词**全部有句子**
    ⇒ 只传一个外刊库没有的词（如 zebra）会撞上 practices.py:191
    「没有可用的练习词条」→ 400；传一个外刊库有的词 → missing_terms=[]。
    两条路都走不到 web_search_note 那一支，测的就不是要测的东西。
    ⇒ 必须先把它变成「词汇库里有、外刊库没有」的词。
    这一步走真实 API，与用户在界面上加词条完全同一条路径。
    """
    r = client.post(f"http://127.0.0.1:{port}/api/entries",
                    json={"type": "word", "term": MISSING_TERM,
                          "meaning_zh": "斑马（本词条由前端文案实测脚本新建，"
                                        "外刊库刻意不含它，用于触发缺失提示）"},
                    timeout=30)
    print(f"    [INFO] POST /api/entries({MISSING_TERM}) → {r.status_code}")


def _new_practice(client, port: int) -> dict:
    """真调一次后端，拿到**真实**的练习创建响应。

    ★ 两个坑都是实测踩出来的 ★
    #1 `cookies=` 那个参数要的是 cookie **罐**（`Cookies`），不是 Client；
        传 Client 进去会走到 `Cookies.__bool__` 去迭代 Client → TypeError。
        Client 本身自动带已收到的 cookie，直接 `client.post()` 就行。
    #2 `trust_env` 是 **Client 构造**参数，不是请求参数 —— 写在 `.post()`
        里会报 unexpected keyword argument。所以本脚本统一用
        `_local_client()` 造 Client，请求方法上只传 timeout。
    """
    url = f"http://127.0.0.1:{port}/api/practices"
    # ★ 入参是 `terms: list[str]`，不是 text ★
    #   实测踩过：传 {"text": ...} → 后端 422/400，
    #   而且因为 `terms` 有默认值 []，它会**自动取当日到期词** ——
    #   那些词全在外刊库里 → missing_terms=[] → web_search_note 为空
    #   → 一眼看过去像「后端没给说明」，其实是入参传错、根本没走到那一支。
    payload = {"terms": [HIT_TERM, MISSING_TERM]}
    r = client.post(url, json=payload, timeout=30)
    print(f"    [INFO] POST /api/practices → {r.status_code}")
    r.raise_for_status()
    return r.json()


def _run_browser(base: str, prac: dict, out_dir: Path) -> int:
    """把真实响应喂给真浏览器渲染。返回子进程退出码。"""
    prac_file = out_dir / "practice_response.json"
    prac_file.write_text(json.dumps(prac, ensure_ascii=False), encoding="utf-8")
    env = dict(os.environ)
    env["NODE_PATH"] = NODE_PATH
    proc = subprocess.run(
        [NODE, str(DRIVER), base, str(prac_file), str(out_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO), env=env)
    print(proc.stdout or "")
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    return proc.returncode


def _seed_desktop_home(home: Path) -> None:
    """桌面形态：起服务前先给临时 HOME 灌一次演示语料。

    ★ 为什么云端不用、桌面要 ★
    云端 `ensure_tenant_db` 每次给新访客建库时都会 seed_visitor，所以词汇库
    一上来就有 60 个词。
    桌面的 lifespan 只调 `db.init_db()` —— **建 schema，不灌语料**（桌面版
    的数据是用户自己的积累，首装就是空库，由用户自己往里加词）。
    ⇒ 直接起桌面服务，`habit` 根本不在库里 → 400「没有可用的练习词条」。

    ★ 怎么灌 ★
    不能用 `seed.seed_database()`：它有一道 `_assert_not_real_db` 闸门，
    拒绝任何叫 `data.db` 的路径 —— 那**正是**桌面库的名字。那道闸门是对的，
    绝不能绕过。所以这里直接用 `seed_visitor()`（与云端 provisioning
    同一个函数），配合 `db._open` 打开那个文件。
    """
    sys.path.insert(0, str(REPO))
    os.environ["ENPRACTICE_HOME"] = str(home)
    from app import db                     # noqa: PLC0415
    from app.seed import seed_visitor      # noqa: PLC0415

    db_path = home / "data.db"
    conn = db._open(db_path)
    try:
        db._apply_schema(conn)
        stat = seed_visitor(conn)
        conn.commit()
    finally:
        conn.close()
    print(f"    [INFO] 桌面临时 HOME 灌种子：{stat}")


def main() -> int:
    import httpx

    if not DRIVER.is_file():
        print(f"[FATAL] 找不到驱动脚本：{DRIVER}", file=sys.stderr)
        return 2

    home = tempfile.mkdtemp(prefix="enp-copy-home-")
    shots = REPO / "shots"
    log_dir = Path(tempfile.mkdtemp(prefix="enp-copy-logs-"))
    cloud_log = log_dir / "cloud.log"
    desk_log = log_dir / "desk.log"
    rc_all = 0
    procs = []
    try:
        # ---------------- 云端形态 ----------------
        os.environ["ENPRACTICE_MODE"] = "cloud"
        os.environ["ENPRACTICE_HOME"] = home
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("ENPRACTICE_WEB_SEARCH", None)
        cloud_port = 8901
        # ★ web_server.py 读 PORT 环境变量，不设就一律 8000 ★
        #   实测踩过：脚本在等 8901，服务却起在 8000 → 「等 20 秒未就绪」。
        #   症状看着像服务没起来，实际端口压根不是同一个。
        cloud_env = dict(os.environ)
        cloud_env["PORT"] = str(cloud_port)
        with cloud_log.open("w", encoding="utf-8") as fh:
            p_cloud = subprocess.Popen(
                [PY, str(REPO / "web_server.py")],
                cwd=str(REPO), env=cloud_env,
                stdout=fh, stderr=subprocess.STDOUT)
        procs.append(p_cloud)
        _wait_ready(cloud_port, p_cloud, cloud_log, "云端")

        jar = httpx.Client(trust_env=False, timeout=30)   # cookie 罐跨请求持久化
        _seed_missing_entry(jar, cloud_port)
        prac_cloud = _new_practice(jar, cloud_port)
        note_cloud = (prac_cloud.get("web_search_note") or "").strip()
        print(f"    [INFO] 云端 missing_terms={prac_cloud.get('missing_terms')}")
        print(f"    [INFO] 云端 web_search_note={json.dumps(note_cloud, ensure_ascii=False)}")
        if not note_cloud:
            print("    [FAIL] 云端没给 web_search_note —— 后端这一环就断了")
            rc_all = 1
        jar.close()

        print("\n" + "=" * 70)
        print("  云端形态：浏览器实跑")
        print("=" * 70)
        rc_cloud = _run_browser(f"http://127.0.0.1:{cloud_port}", prac_cloud,
                                shots / "pmissing_cloud")
        rc_all = rc_all or rc_cloud

        p_cloud.terminate()
        try:
            p_cloud.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p_cloud.kill()
        procs.remove(p_cloud)

        # ---------------- 桌面形态 ----------------
        # ★ 桌面下把联网搜例句关掉，走「用户自己关的」那条文案分支 ——
        #   这一支最容易串台（复用云端文案就会在桌面版冒出「云端」两个字）。
        os.environ.pop("ENPRACTICE_MODE", None)
        os.environ["ENPRACTICE_WEB_SEARCH"] = "0"
        desk_port = 8902
        _seed_desktop_home(home)
        with desk_log.open("w", encoding="utf-8") as fh:
            p_desk = subprocess.Popen(
                [PY, "-c",
                 "import uvicorn,app.main as m;"
                 f"uvicorn.run(m.app, host='127.0.0.1', port={desk_port},"
                 f" log_level='error')"],
                cwd=str(REPO), env=dict(os.environ),
                stdout=fh, stderr=subprocess.STDOUT)
        procs.append(p_desk)
        _wait_ready(desk_port, p_desk, desk_log, "桌面")

        jar2 = httpx.Client(trust_env=False, timeout=30)
        _seed_missing_entry(jar2, desk_port)
        prac_desk = _new_practice(jar2, desk_port)
        note_desk = (prac_desk.get("web_search_note") or "").strip()
        print(f"    [INFO] 桌面 missing_terms={prac_desk.get('missing_terms')}")
        print(f"    [INFO] 桌面 web_search_note={json.dumps(note_desk, ensure_ascii=False)}")
        if "云端" in note_desk:
            print("    [FAIL] 桌面文案里漏出了「云端」两个字 —— 串台了")
            rc_all = 1
        jar2.close()

        print("\n" + "=" * 70)
        print("  桌面形态：浏览器实跑")
        print("=" * 70)
        rc_desk = _run_browser(f"http://127.0.0.1:{desk_port}", prac_desk,
                               shots / "pmissing_desktop")
        rc_all = rc_all or rc_desk

        p_desk.terminate()
        try:
            p_desk.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p_desk.kill()
        procs.remove(p_desk)

        return rc_all
    finally:
        for p in procs:
            try:
                p.kill()
            except Exception:                   # noqa: BLE001
                pass
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(log_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                           # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("RESULT: TOOL CRASHED", file=sys.stderr)
        sys.exit(2)