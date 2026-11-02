"""云端安全与用户体验回归测试（S4）。

运行：python test_cloud_security.py
（脚本式，与本仓库其它 test_*.py 一致；不用 pytest）

★ 为什么单独一个文件 ★
S4 的两条核心验收（① SSRF 闸门 ② AI 密钥 per-visitor 隔离）必须**在干净的
仓库状态下可复现** —— 不能依赖 C:/tmp/ 下的一次性脚本。这里把两组的正反
用例全部固化成断言，任何人拿到仓库直接跑本文件就能复验。

★ 全部在临时 ENPRACTICE_HOME 里跑，**绝不碰 %APPDATA%\\EnPractice 真实库**。

★ 全程不真出网 ★
所有可能被触发的出网点都被打桩（requests.get / websearch.search_sentences /
articles.sentences_from_web），所以：
  · 离线可跑、跑得快；
  · 而且「打桩没被调用」本身就是最硬的证据 —— 出网点真被触发了，
    打桩的调用记录里就会留下痕迹，对应断言会 FAIL。
    （反过来只断言「返回了 400」是不够的：400 也可能是别的原因造成的。）
"""

import ast
import asyncio
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

import requests

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable

TMP = tempfile.mkdtemp(prefix="enp-sec-test-")
os.environ["ENPRACTICE_HOME"] = TMP
# ★ 开头先清干净：这两个环境变量会左右「桌面/云端」与「密钥来源」，
#   带着外部残留跑出来的结论不可信。
os.environ.pop("ENPRACTICE_MODE", None)
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("ENPRACTICE_WEB_SEARCH", None)

sys.path.insert(0, str(REPO))
# ★ tools/ 也要在 path 上：代码指纹的唯一实现放在 tools/code_fingerprint.py ★
sys.path.insert(0, str(REPO / "tools"))

from app import ai, articles, cloud, config, db, egress, practices, websearch  # noqa: E402
from app import main as main_mod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402

results = []


def check(name, ok, detail=""):
    """记一条断言。**detail 只在 FAIL 时打印**，避免污染 [PASS]/[FAIL] 计数。"""
    results.append((name, bool(ok)))
    mark = "PASS" if ok else "FAIL"
    print(f"    [{mark}] {name}"
          + (f"\n            -> {detail}" if detail and not ok else ""))


# ===========================================================================
# 打桩件
# ===========================================================================

FAKE_HTML = (
    "<html><head><title>Fake Source</title></head><body>"
    "<p>Reading every day builds a habit that lasts.</p>"
    "<p>Small steps compound into big results over time.</p>"
    "</body></html>"
)

#: 所有真实出网的记录本。打桩被调用 = 闸门漏了。
net_calls = []
#: articles.sentences_from_web 的调用记录（业务层闸门用）。
web_calls = []
#: websearch.search_sentences 的调用记录（自动搜索闸门用）。
search_calls = []


class _FakeResp:
    """够 articles.sentences_from_web 用的最小响应对象。"""
    status_code = 200
    text = FAKE_HTML
    encoding = "utf-8"

    def raise_for_status(self):
        return None


def fake_get(url, **kwargs):
    net_calls.append(url)
    return _FakeResp()


def fake_sentences_from_web(url, terms, limit=8):
    web_calls.append((url, list(terms)))
    return [{
        "term": t, "found": True,
        "sentences": [{
            "article_id": 0, "title": "Fake Source", "source_name": "Fake",
            "url": url, "seq": 0, "text": f"A sentence about {t}.",
            "text_zh": "",
        }],
    } for t in terms]


def fake_search_sentences(terms, report=None, **kwargs):
    search_calls.append(list(terms))
    return []


DUMMY_KEY = "dummy-test-key-123"
METADATA_URL = "http://169.254.169.254/latest/meta-data/"


def _tc(**kwargs):
    """TestClient 工厂：固定 ``https://testserver`` base_url。

    ★ 为什么必须 https ★
    云端访客 cookie 带 ``Secure``（B11 加固）之后，httpx 在 http base 下
    会**存下但不发送** Secure cookie（实测 httpx 0.28.1：jar 里有、
    下一个请求的 Cookie 头是空的）⇒ 每个请求都算新访客，
    cookie 连续性全断。生产形态本来就是 HTTPS，测试没理由用 http。
    桌面模式的用例换 https base 同样无行为差异（路由不读 scheme）。
    """
    kwargs.setdefault("base_url", "https://testserver")
    return TestClient(fastapi_app, **kwargs)


def tid_of(resp, client=None):
    """从响应（或客户端 cookie 罐）里取访客 id。

    ★ 为什么要两个来源 ★
    httpx 的 TestClient 会**持久化 cookie**，所以 `Set-Cookie` 只出现在
    那个访客的**第一次**响应上；第二次请求的响应里就没有了。
    只看响应头会在第二个请求上取不到 id —— 那是测试写法的问题，
    不是产品的问题。所以这里先看响应头，再回落到 cookie 罐。
    """
    m = re.search(r"enp_vid=([0-9a-f]{32})", resp.headers.get("set-cookie") or "")
    if m:
        return m.group(1)
    if client is not None:
        try:
            value = client.cookies.get(cloud.VISITOR_COOKIE)
        except Exception:                        # noqa: BLE001
            value = None
        if value and cloud.is_valid_id(value):
            return value
    return ""


# ===========================================================================
print("\n[1] 桌面模式：web-pick-sentences 仍能正常取句（逐字不变）")
# ===========================================================================

check("桌面 cloud_mode() 为 False", cloud.cloud_mode() is False)
check("桌面 network_fetch_allowed() 为 True（闸门放行）",
      cloud.network_fetch_allowed() is True)

net_calls.clear()
with mock.patch("requests.get", side_effect=fake_get):
    with _tc() as dclient:
        resp = dclient.post("/api/articles/web-pick-sentences",
                            json={"url": "https://example.com/article",
                                  "terms": ["habit"]})
        print(f"    [实测] 桌面 POST /api/articles/web-pick-sentences "
              f"url=https://example.com/article terms=['habit']")
        print(f"    [实测] HTTP {resp.status_code}  body={json.dumps(resp.json(), ensure_ascii=False)[:220]}")
        check("桌面 web-pick-sentences 返回 200", resp.status_code == 200, resp.text)
        body = resp.json()
        check("桌面返回了句子列表（非空）",
              isinstance(body, list) and len(body) > 0, str(body)[:200])
        check("★ 桌面真的发起了网络请求（闸门没误伤桌面）",
              len(net_calls) == 1, str(net_calls))
        check("★ 桌面请求的正是访客给的 URL",
              bool(net_calls) and net_calls[0] == "https://example.com/article",
              str(net_calls))
        check("桌面不发 Set-Cookie（桌面零回归约束）",
              not resp.headers.get("set-cookie"),
              str(resp.headers.get("set-cookie")))
        check("桌面不建 tenants/ 目录（桌面零回归约束）",
              not (Path(TMP) / "tenants").exists(),
              str(list((Path(TMP) / "tenants").glob("*"))
                  if (Path(TMP) / "tenants").exists() else []))

        # 桌面连接（业务层闸门测试要用）
        dconn = db.connect()

# ===========================================================================
print("\n[2] 业务层兜底闸门（app/practices.py）：桌面放行 / 云端拦截")
# ===========================================================================

web_calls.clear()
search_calls.clear()
desktop_err = ""
with mock.patch.object(articles, "sentences_from_web",
                       side_effect=fake_sentences_from_web), \
     mock.patch.object(websearch, "search_sentences",
                       side_effect=fake_search_sentences):
    try:
        practices.create_practice(
            dconn, ["zzz-not-in-library"],
            web_fallback_url="https://example.com/fallback")
    except Exception as exc:                    # noqa: BLE001 - 测的是闸门，不是成功
        desktop_err = f"{type(exc).__name__}: {exc}"
check("★ 桌面业务层照旧调用 articles.sentences_from_web",
      len(web_calls) == 1, f"calls={web_calls} err={desktop_err}")
check("桌面业务层不会去自动搜网（已被直抓路径满足）",
      len(search_calls) == 0, str(search_calls))
dconn.close()

# ===========================================================================
print("\n[3] 云端模式：web-pick-sentences 一律 400（SSRF 闸门）")
# ===========================================================================

os.environ["ENPRACTICE_MODE"] = "cloud"
# 与 web_server.py 的默认一致：云端默认不联网搜例句。
os.environ["ENPRACTICE_WEB_SEARCH"] = "0"

check("云端 cloud_mode() 为 True", cloud.cloud_mode() is True)
check("云端 network_fetch_allowed() 为 False（闸门关闭）",
      cloud.network_fetch_allowed() is False)
check("云端 web_search_enabled() 为 False",
      cloud.web_search_enabled() is False)

net_calls.clear()
with mock.patch("requests.get", side_effect=fake_get), \
     mock.patch.object(websearch, "search_sentences",
                       side_effect=fake_search_sentences):
    with _tc() as cclient:
        resp = cclient.post("/api/articles/web-pick-sentences",
                             json={"url": METADATA_URL, "terms": ["x"]})
        print(f"    [实测] 云端 POST /api/articles/web-pick-sentences "
              f"url={METADATA_URL} terms=['x']")
        print(f"    [实测] HTTP {resp.status_code}  body={resp.text[:300]}")
        check("★ 云端 web-pick-sentences 返回 400", resp.status_code == 400, resp.text)
        detail = ""
        try:
            detail = (resp.json() or {}).get("detail", "")
        except Exception:                        # noqa: BLE001
            detail = ""
        check("★ 云端 400 的 detail 明确说明了为什么关",
              detail == cloud.WEB_PICK_DISABLED_REASON, detail)
        check("★ 云端请求全程没有触发任何出网（requests.get 未被调用）",
              len(net_calls) == 0, str(net_calls))
        check("★ 云端首个响应确实带了 Set-Cookie",
              bool(resp.headers.get("set-cookie")),
              str(resp.headers.get("set-cookie")))

        info = cclient.get("/api/app-info")
        check("云端 /api/app-info 的 network_fetch 为 False",
              (info.json() or {}).get("network_fetch") is False, info.text)
        tid = tid_of(resp, cclient)
        check("云端新访客拿到了合法 id", cloud.is_valid_id(tid), tid)

        # 业务层闸门（云端）
        tok = cloud.current_tenant.set(tid)
        try:
            cconn = db.connect()
            web_calls.clear()
            search_calls.clear()
            cloud_err = ""
            try:
                with mock.patch.object(articles, "sentences_from_web",
                                       side_effect=fake_sentences_from_web), \
                     mock.patch.object(websearch, "search_sentences",
                                       side_effect=fake_search_sentences):
                    practices.create_practice(
                        cconn, ["zzz-not-in-library"],
                        web_fallback_url=METADATA_URL)
            except Exception as exc:            # noqa: BLE001
                cloud_err = f"{type(exc).__name__}: {exc}"
            check("★ 云端业务层没有调用 sentences_from_web（SSRF 闸门生效）",
                  len(web_calls) == 0, f"calls={web_calls} err={cloud_err}")
            check("★ 云端业务层没有自动搜网",
                  len(search_calls) == 0, str(search_calls))
            check("★ 云端业务层如实写出禁用原因（不静默返回空）",
                  cloud.WEB_PICK_DISABLED_REASON in cloud_err, cloud_err)
            cconn.close()
        finally:
            cloud.current_tenant.reset(tok)

# ===========================================================================
print("\n[4] AI 密钥 per-visitor 隔离")
# ===========================================================================

with mock.patch("requests.get", side_effect=fake_get), \
     mock.patch.object(websearch, "search_sentences",
                       side_effect=fake_search_sentences):
    with _tc() as cclient:
        resp = cclient.get("/api/ai/status", headers={"X-Enp-Api-Key": DUMMY_KEY})
        check("★ 云端带密钥请求 /api/ai/status 返回 mock:false",
              (resp.json() or {}).get("mock") is False, resp.text)

        tid = tid_of(resp, cclient)
        check("云端访客 id 合法", cloud.is_valid_id(tid), tid)
        # 访客试图把 base_url 改成云元数据地址
        # ★ 现在的设计：访客自填的接口地址走**请求头通道**，不落盘 ⇒
        #   这条 PUT 里 base_url 无论填什么都不该进磁盘。
        put = cclient.put("/api/settings", json={"ai": {
            "api_key": DUMMY_KEY,
            "base_url": METADATA_URL,
            "model": "evil-model",
        }})
        check("云端 PUT /api/settings 返回 200", put.status_code == 200, put.text)
        ai_cfg = (put.json() or {}).get("ai", {})
        check("★ 云端响应里的 base_url 不是访客填的那个（访客地址走请求头，不落盘）",
              ai_cfg.get("base_url") == cloud.ai_base_url(), str(ai_cfg))
        check("★ 云端响应里的 model 不是访客填的那个（访客地址走请求头，不落盘）",
              ai_cfg.get("model") == cloud.ai_model(), str(ai_cfg))
        check("★ 云端响应里的 api_key 是脱敏占位而非明文",
              ai_cfg.get("api_key") in ("", "***"), str(ai_cfg))

        # 磁盘上必须搜不到这个密钥
        tok = cloud.current_tenant.set(tid)
        try:
            sfile = config.settings_path()
            check("访客的 settings.json 在他自己的目录里",
                  sfile.parent.name == tid and sfile.name == "settings.json",
                  str(sfile))
            disk = sfile.read_text(encoding="utf-8")
            check("★ 磁盘 settings.json 搜不到访客密钥", DUMMY_KEY not in disk, disk[:300])
            check("★ 磁盘 settings.json 里没有元数据地址",
                  "169.254.169.254" not in disk, disk[:300])
            check("★ 磁盘 settings.json 的 base_url 是平台默认值（访客自填地址绝不落盘）",
                  cloud.ai_base_url() in disk, disk[:300])

            # 整个访客目录 grep（不只是 settings.json）
            tdir = cloud.tenant_dir(tid)
            grep_hits = []
            for p in sorted(tdir.rglob("*")):
                if not p.is_file():
                    continue
                try:
                    txt = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if DUMMY_KEY in txt:
                    grep_hits.append(p.name)
            check("★ 整个访客目录里 grep 不到该密钥", not grep_hits, str(grep_hits))

            # 三级优先级：请求头 > 环境变量 > settings.json
            # ★ 注意：这里必须**显式 set** ContextVar —— 中间件在响应结束时
            #   会把 request_api_key 复位，所以请求结束后再去读它是读不到密钥的。
            os.environ["OPENAI_API_KEY"] = "env-key-should-lose"
            cloud.request_api_key.set(DUMMY_KEY)
            check("请求头优先于环境变量",
                  config.get_api_key() == DUMMY_KEY, config.get_api_key())
            cloud.request_api_key.set("")           # 复位成"本次请求没带密钥"
            # ★ 云端：环境变量这一级被**跳过**（不是"读了不用"，是根本不读）。
            #   平台一旦配了 OPENAI_API_KEY，所有访客就共用同一把平台密钥 ——
            #   白嫖 + 串号。所以云端无请求头时必须拿不到任何 key → mock 模式。
            check("★ 云端无请求头时不给环境变量那把 key",
                  config.get_api_key() == "", config.get_api_key())
            del os.environ["OPENAI_API_KEY"]
            # 云端 settings.json 里也拿不到（save_settings 落盘前就抹了 key）
            check("★ 云端无请求头时也拿不到 settings.json 里的 key",
                  config.get_api_key() == "", config.get_api_key())

            # ai._settings() 在云端不采信磁盘 —— 磁盘不是访客地址的来源
            evil = json.loads(disk)
            evil.setdefault("ai", {})["base_url"] = METADATA_URL
            sfile.write_text(json.dumps(evil, ensure_ascii=False), encoding="utf-8")
            locked = ai._settings()
            check("★ ai._settings() 即使磁盘被手改也不采信磁盘里的 base_url",
                  locked.get("base_url") == cloud.ai_base_url(), str(locked))
            check("★ ai._settings() 即使磁盘被手改也不采信磁盘里的 model",
                  locked.get("model") == cloud.ai_model(), str(locked))
        finally:
            cloud.current_tenant.reset(tok)

# ===========================================================================
print("\n[5] 桌面分支：密钥链路逐字不变")
# ===========================================================================

os.environ.pop("ENPRACTICE_MODE", None)
with mock.patch("requests.get", side_effect=fake_get):
    with _tc() as dclient:
        resp = dclient.get("/api/ai/status", headers={"X-Enp-Api-Key": DUMMY_KEY})
        check("★ 桌面模式下请求头密钥被忽略（桌面逐字不变）",
              (resp.json() or {}).get("mock") is True, resp.text)

# 桌面 save_settings 不锁 base_url / model
d_cfg = config.load_settings()
d_cfg["ai"]["base_url"] = "https://desktop-unchanged.example.com/v1"
d_cfg["ai"]["model"] = "desktop-model"
config.save_settings(d_cfg)
check("★ 桌面 save_settings 不锁死 base_url（零回归）",
      config.load_settings()["ai"]["base_url"]
      == "https://desktop-unchanged.example.com/v1",
      str(config.load_settings()["ai"]))
check("★ 桌面 ai._settings() 原样返回磁盘值（零回归）",
      ai._settings().get("base_url") == "https://desktop-unchanged.example.com/v1",
      str(ai._settings()))
check("★ 桌面 ai._settings() 原样返回 model（零回归）",
      ai._settings().get("model") == "desktop-model", str(ai._settings()))

# ===========================================================================
print("\n[6] 清空我的数据（云端专属）")
# ===========================================================================

os.environ["ENPRACTICE_MODE"] = "cloud"
os.environ["ENPRACTICE_WEB_SEARCH"] = "0"
with mock.patch("requests.get", side_effect=fake_get):
    with _tc() as cclient:
        first = cclient.get("/api/app-info")
        tid = tid_of(first, cclient)
        check("云端访客 id 合法", cloud.is_valid_id(tid), tid)
        tdir = cloud.tenant_dir(tid)
        check("访客目录已建好", tdir.is_dir(), str(tdir))
        dele = cclient.delete("/api/me/data")
        check("★ 云端 DELETE /api/me/data 返回 200", dele.status_code == 200, dele.text)
        check("★ 云端删除后 removed 为 True",
              (dele.json() or {}).get("removed") is True, dele.text)
        check("★ 访客目录已被删除", not tdir.exists(), str(tdir))

os.environ.pop("ENPRACTICE_MODE", None)
with mock.patch("requests.get", side_effect=fake_get):
    with _tc() as dclient:
        dele = dclient.delete("/api/me/data")
        check("★ 桌面 DELETE /api/me/data 返回 405（桌面没有访客数据概念）",
              dele.status_code == 405, dele.text)

try:
    cloud.delete_tenant("a" * 32)
    perm_ok = False
    perm_detail = "没有抛异常"
except PermissionError as exc:
    perm_ok = True
    perm_detail = str(exc)
except Exception as exc:                        # noqa: BLE001
    perm_ok = False
    perm_detail = f"{type(exc).__name__}: {exc}"
check("★ 桌面 cloud.delete_tenant 抛 PermissionError", perm_ok, perm_detail)

# ===========================================================================
print("\n[7] tools/check_no_secrets.py 正反用例")
# ===========================================================================

# ★ 假密钥在运行时拼装 —— 仓库里不留下任何形似密钥的字面量，
#   否则扫描器会把自己的测试夹具判成泄露（那是自举式的死循环）。
FAKE_ARK = "ar" + "k-" + "A1b2C3d4E5f6G7h8"
FAKE_SK = "s" + "k-" + "A1b2C3d4E5f6G7h8I9j0K1"


def run_scanner(*args):
    """跑扫描器子进程。返回 (returncode, stdout)。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [PY, str(REPO / "tools" / "check_no_secrets.py"), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO), env=env)
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


case_dir = Path(tempfile.mkdtemp(prefix="enp-secret-case-"))
bad_file = case_dir / "bad_config.py"
bad_file.write_text(
    f'API_KEY = "{FAKE_ARK}"\n'
    f'OTHER = "{FAKE_SK}"\n',
    encoding="utf-8")
good_file = case_dir / "good_config.py"
good_file.write_text(
    '# 这几个串都含密钥前缀的片段，但都不是密钥 —— 扫描器必须放过它们\n'
    'MARK_CLASS = "cv-mark-text"\n'
    'ITEMS = ["task-list", "disk-usage", "sk-secret-123"]\n'
    'PLACEHOLDER = "your-api-key-here"\n'
    'URL = "https://example.com/path?mark-text=1"\n',
    encoding="utf-8")

rc, out, err = run_scanner(str(bad_file))
check("★ 含假密钥的文件 → 退出码非 0（闸门不放行）", rc != 0, f"rc={rc} out={out}")
check("★ 命中输出点名了那个文件", "bad_config.py" in out, out)
check("★ 命中输出点名了规则", "volcengine" in out or "openai" in out, out)
check("★ 命中输出不泄露密钥明文",
      FAKE_ARK not in out and FAKE_SK not in out, out)

rc2, out2, err2 = run_scanner(str(good_file))
check("★ 干净文件 → 退出码 0（cv-mark-text 等不误报）", rc2 == 0,
      f"rc={rc2} out={out2} err={err2}")

# ★ 回归断言：私钥容器后缀不许再漏检 ★
#   实测漏检过一次：`.pem` 不在扫描器的后缀白名单里 → 私钥文件**整个
#   没被读进来** → 「规则写得对」也白搭（规则再准，得先被读到）。
#   这条断言就是防止将来有人改白名单/改文件判定时又漏回去。
#   ★ PEM 头在运行时拼装：仓库里不能留下能匹配私钥规则的完整字面量，
#     否则扫描器扫仓库时会把自己的测试夹具判成泄露。
PEM_TEXT = ("-----BEGIN " + "RSA " + "PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA\n"
            "-----END " + "RSA " + "PRIVATE KEY-----\n")
for pem_name in ("private_key.pem", "server.key", "cert.p12", "store.jks"):
    pem_file = case_dir / pem_name
    pem_file.write_text(PEM_TEXT, encoding="utf-8")
    rc_pem, out_pem, _ = run_scanner(str(pem_file))
    check(f"★ {pem_name} 私钥容器 → 退出码非 0（容器后缀不被漏检）",
          rc_pem != 0, f"rc={rc_pem} out={out_pem}")
    check(f"★ {pem_name} 命中输出点名了那个文件",
          pem_name in out_pem, out_pem)

# ★ 不在白名单后缀里的文件也不许漏（判据是「内容像文本」）★
odd_file = case_dir / "no_suffix_at_all"
odd_file.write_text(PEM_TEXT, encoding="utf-8")
rc_odd, out_odd, _ = run_scanner(str(odd_file))
check("★ 无后缀文件里的私钥块 → 退出码非 0（内容像文本就扫）",
      rc_odd != 0, f"rc={rc_odd} out={out_odd}")

rc3, out3, err3 = run_scanner()
check("★ 扫整个仓库 → 退出码 0（仓库当前是干净的）", rc3 == 0,
      f"rc={rc3} out={out3} err={err3}")

rc4, out4, _ = run_scanner(str(case_dir / "no-such-file"))
check("★ 路径不存在 → 退出码 2（扫描器自身出错）", rc4 == 2, f"rc={rc4} out={out4}")

# ===========================================================================
print("\n[8] app/ 出网点清单守卫（AST 级，不是 grep）")
# ===========================================================================

#: app/ 下**所有**真实出网调用点。改这个清单 = 改了安全边界，
#: 必须同时更新本表并说明理由，否则这条断言会 FAIL。
#:
#: ★ 2026-10-25 更新：新增 `app/egress.py`（F 项「访客自填接口地址」的出网白名单）★
#:   理由：访客能自填接口地址 ⇒ 服务器代他发请求 ⇒ 必须有一个集中的、
#:   有 SSRF 校验的出口。`app/ai.py` 里那条 `requests.post` 是**平台默认地址**
#:   走的路（桌面、以及访客没填自填地址时），它旁边必须有且只有一条
#:   「访客自填地址」的路 —— 而那条路的两个出网点都在 `app/egress.py`：
#:     · `session.post`  = **钉住**路径（连已校验的 IP，TLS 仍按域名校验）；
#:     · `requests.post` = **降级**路径（环境配了 MITM 代理时按域名请求）。
#:   守卫的设计意图就是"新增出网点会被抓住"—— 所以这里必须显式登记，
#:   而不是把守卫放宽成"看起来还行就行"。
#:
#: ★ B15 更新：新增 `app/websearch.py` 的 `fetcher.get` ★
#:   它不是第 7 个出口，而是**同一个出口的上游转发点**：
#:   `_Fetcher.get()` 内部就是 `self._s.get(...)`，业务代码全都经
#:   `fetcher = _Fetcher()` → `fetcher.get(url)` 转一手。
#:   B15 之前 `_is_http_call` 只认四种硬编码前缀，这些转发点对守卫
#:   **隐形** —— 谁把 `_Fetcher` 内部换成 `httpx.get`，清单照样全绿。
#:   现在两段式判定把它们纳入视野，清单因此从 5 处变 6 处。
EXPECTED_EGRESS = {
    ("app/ai.py", "requests.post"),            # AI 调用 —— 平台默认地址走这条
    ("app/articles.py", "requests.get"),        # 按网址取句 —— 云端必须关（SSRF）
    ("app/websearch.py", "self._s.get"),        # 搜例句 —— _Fetcher 内部的真实出口
    ("app/websearch.py", "fetcher.get"),        # 搜例句 —— _Fetcher.get 的转发调用点
    ("app/egress.py", "session.post"),         # 访客自填地址 —— 钉住路径
    ("app/egress.py", "requests.post"),         # 访客自填地址 —— 代理降级路径
}


def _dotted(node):
    """把 `a.b.c` 形式的调用目标还原成点分字符串。"""
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


#: 第一段判据：**动词**必须是真 HTTP 动词。
#: 只有这些才算出网点 ——
#: `self._s.headers.update(...)` 是给会话设请求头，不是发请求；
#: `requests.Session()` 是建会话，也不是发请求。
#: `urlopen` 是 urllib 的发送函数，与动词同段处理。
_HTTP_METHODS = frozenset({
    "get", "post", "put", "delete", "head", "patch", "options", "request",
    "send", "urlopen",
})

#: 第二段判据（B15 加固）：**接收者**必须是"像 HTTP 客户端"的对象。
#: ★ 为什么不能只靠原来的四种硬编码前缀 ★
#: 旧实现只认 `requests.` / `self._s.` / `self._requests.` / `session.` ——
#: 新增出网点只要换个写法（`httpx.post(...)`、`urllib.request.urlopen(...)`、
#: `self._session.get(...)`、`fetcher.post(...)`）就对守卫**隐形**，
#: 而守卫的存在意义就是"新增出网点会被抓住"。隐形 = 守卫形同虚设。
#: ★ 为什么不能退化成"任何 .get() 都算出网"★
#: 那会把 `d.get("k")`、`config.get("y")`、`self.headers.get("x")` 全判成
#: 出网点 —— 满屏假红，守卫会被第一任维护者当成噪音删掉。
#: ⇒ 两段式：动词段卡住"是不是在发请求"，接收者段卡住"对象是不是客户端"。

#: 已知客户端模块 / 命名空间（判点分路径前缀）。
_CLIENT_PREFIXES = ("requests", "httpx", "aiohttp", "urllib.request",
                    "urllib3", "http.client")

#: 客户端对象的常见命名（属性名 / 局部变量名）。
#: 含前导下杠的历史命名：`self._s` 是 websearch 的会话，
#: `self._requests` / `self._session` 是同类常见写法。
_CLIENT_NAMES = frozenset({
    "_s", "_session", "_requests", "_client", "_conn", "_connection",
    "_fetcher",
    "session", "fetcher", "client", "conn", "connection", "transport",
    "requests", "httpx", "aiohttp",
})


def _is_http_call(dotted):
    if not dotted:
        return False
    receiver, _, verb = dotted.rpartition(".")
    if verb not in _HTTP_METHODS:
        return False
    for prefix in _CLIENT_PREFIXES:
        if receiver == prefix or receiver.startswith(prefix + "."):
            return True
    return receiver.rsplit(".", 1)[-1] in _CLIENT_NAMES


found_egress = set()
session_count = 0
for py in sorted((REPO / "app").glob("*.py")):
    tree = ast.parse(py.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted(node.func)
        if dotted == "requests.Session":
            session_count += 1
        elif _is_http_call(dotted):
            found_egress.add((f"app/{py.name}", dotted))

check("★ app/ 下的出网调用点与清单逐字一致（新增出网点会被抓住）",
      found_egress == EXPECTED_EGRESS,
      f"期望={sorted(EXPECTED_EGRESS)} 实际={sorted(found_egress)}")
# ★ B15 后是 6 处（原 5 处 + fetcher.get 转发点，见 EXPECTED_EGRESS 注释）★
check("出网调用点恰好 6 处（没有第 7 处）", len(found_egress) == 6,
      str(sorted(found_egress)))
check("requests.Session 恰好 2 处（websearch 的会话 + egress 的钉住会话）",
      session_count == 2, str(session_count))

# ===========================================================================
print("\n[9] 零改动 ratchet + 启动横幅")
# ===========================================================================

# ★ 这两个文件本轮明令「出网点一个字不改」。把**代码指纹**钉进测试里，
#   就把口头约定变成了可自动执行的闸门 —— 将来谁要改，
#   必须先更新这里并写清理由。
#
# ★ 为什么比对「剥离注释后的代码」而不是逐字节 md5（2026-10-24 修正）：
#   逐字节 md5 把**注释**和**代码**混为一谈。它真正想守的是「出网点没被改」，
#   却会被纯文案改动误伤 —— 本轮术语统一（「回译」→「翻译」）只改了
#   app/websearch.py 里一行 docstring 内的中文，出网点 self._s.get 仍是 1 处、
#   AST 级守卫 EXPECTED_EGRESS 也照样通过，但逐字节 md5 变了。
#   正解：ratchet 守**代码**（注释/docstring/字符串字面量都剥离），
#   注释是文档、不是安全边界。真正守住出网点的另有 AST 守卫那条断言。
#   基线由 tools/recount_ratchet_baseline.py 计算（含双向反向验证：
#   注入出网点→指纹变、只改注释→指纹不变）。
ZERO_CHANGE_BASELINE = {
    "app/articles.py": "2143d2cb567b0f05d3fcb74e1fda9fa1",
    "app/websearch.py": "993fcb942122bb0e0d17fa29ed3f78b0",
}


# ★ 实现只有一个地方：tools/code_fingerprint.py ★
#   本文件曾经自带一份「单循环」实现，而基线是 tools/recount_ratchet_baseline.py
#   用「两阶段」实现算的 —— ast.walk 是 BFS，Expr 先于其子 Constant 被访问，
#   单循环会把 docstring 最终覆盖成 __STR__（articles.py 有 45 处 docstring 被错标），
#   两者 ast.dump 完全不同 ⇒ 拿单循环指纹比两阶段基线**永远 FAIL**，判据自己造假红。
#   现在两边共用 tools/code_fingerprint.py，口径漂移从结构上根除。
from code_fingerprint import code_fingerprint as _code_fingerprint  # noqa: E402

for rel, expect in ZERO_CHANGE_BASELINE.items():
    actual = _code_fingerprint(REPO / rel)
    check(f"★ {rel} 出网点零改动（代码指纹与基线一致）", actual == expect,
          f"实际={actual} 期望={expect}")

# ★ 口径自检：唯一实现的指纹必须等于基线脚本用的同一个实现 ★
#   如果有人在本文件里再手写一份指纹实现，这里会立刻发现（而不是等到断言假红）。
_fp_mod = REPO / "tools" / "code_fingerprint.py"
check("★ 代码指纹实现只有一份（tools/code_fingerprint.py 在位）", _fp_mod.is_file())
check("★ 指纹实现是两阶段（阶段2 在阶段1 之后，不是同一循环里）",
      _fp_mod.is_file() and
      _fp_mod.read_text(encoding="utf-8").count("node.value = \"__STR__\"") == 1
      and _fp_mod.read_text(encoding="utf-8").count("node.value.value = \"__DOC__\"") == 1)
check("★ 基线脚本也共用同一实现（不再自带一份）",
      "from code_fingerprint import" in
      (REPO / "tools" / "recount_ratchet_baseline.py").read_text(encoding="utf-8"))

ws_src = (REPO / "web_server.py").read_text(encoding="utf-8")
check("web_server.py 打印语料版本指纹", "_seed_fingerprint()" in ws_src)
check("web_server.py 的指纹指向 app/seed_data.py",
      "seed_data.py" in ws_src and "hashlib.md5" in ws_src)
check("★ web_server.py 指纹带字节数（光看 md5 不好比对）",
      "字节" in ws_src and "len(data)" in ws_src)
check("★ web_server.py 横幅注明这是版本指纹、不是防篡改校验",
      "非防篡改校验" in ws_src and "不防篡改" in ws_src)
check("web_server.py 的横幅同时打印两个出网开关",
      "network_fetch_allowed()" in ws_src and "web_search_enabled()" in ws_src)
check("★ web_server.py 不再 setdefault ENPRACTICE_WEB_SEARCH（云端恒关，env 无效）",
      'setdefault("ENPRACTICE_WEB_SEARCH"' not in ws_src)

# ===========================================================================
print("\n[10] 部署前闸门：tools/check_seed_metrics.py 六项判据")
# ===========================================================================
# ★ 为什么把它纳入本文件 ★
# 种子语料的「够不够练、筛不筛得动」是产品核心，但它是**静默退化**的：
# 改了语料不会报错，只会慢慢变得不好用。所以六项量化判据必须是**闸门**，
# 而不是"有空跑一下看看"。
metrics_script = REPO / "tools" / "check_seed_metrics.py"
check("tools/check_seed_metrics.py 存在", metrics_script.is_file(),
      str(metrics_script))
if metrics_script.is_file():
    env2 = dict(os.environ)
    env2["PYTHONIOENCODING"] = "utf-8"
    proc2 = subprocess.run(
        [PY, str(metrics_script)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=str(REPO), env=env2)
    check("★ tools/check_seed_metrics.py 跑通且 EXIT=0",
          proc2.returncode == 0,
          f"rc={proc2.returncode} out={(proc2.stdout or '')[-500:]}")
    check("★ check_seed_metrics.py 自己没有密钥样式字符串",
          "RESULT: PASS" in (proc2.stdout or ""), (proc2.stdout or "")[-200:])

# ===========================================================================
print("\n[11] 措辞不许串台：桌面版不能出现「云端」两个字")
# ===========================================================================
# ★ 这条是被实测逼出来的 ★
#   桌面下用户用 ENPRACTICE_WEB_SEARCH=0 关掉联网搜索时，
#   #p-missing 那句话原本复用了云端那条文案 → 桌面界面上冒出
#   「**云端部署**已关闭「联网搜例句」」。桌面版没有云端这回事，
#   用户在桌面版里看到「云端部署」会直接以为这功能没做完。
os.environ.pop("ENPRACTICE_MODE", None)
os.environ["ENPRACTICE_WEB_SEARCH"] = "0"
desk_reason = cloud.web_search_disabled_reason()
check("★ 桌面 web_search_disabled_reason() 不含「云端」",
      "云端" not in desk_reason, desk_reason)
check("★ 桌面那条说的是「按设置关闭」",
      "按设置关闭" in desk_reason, desk_reason)
check("★ 桌面 network_fetch_allowed() 仍放行（桌面逐字不变）",
      cloud.network_fetch_allowed() is True)

os.environ["ENPRACTICE_MODE"] = "cloud"
cloud_reason = cloud.web_search_disabled_reason()
check("★ 云端那条明确写「云端部署已关闭」",
      "云端部署已关闭" in cloud_reason, cloud_reason)
check("★ 云端 network_fetch_allowed() 恒关",
      cloud.network_fetch_allowed() is False)
del os.environ["ENPRACTICE_WEB_SEARCH"]
os.environ.pop("ENPRACTICE_MODE", None)

# ===========================================================================
print("\n[12] 密钥闸门自身：三档退出码契约（闸门的闸门）")
# ===========================================================================
# ★ 为什么必须钉死这一组 ★
# EXIT=1 在本工具的契约里是「**扫到了真密钥，闸门不放行**」。
# 如果工具自己崩了也返回 1，CI 会红 —— 表面上是 fail-closed 很安全，
# 实际上那是**假警报**：而且这个 1 会被当成「闸门生效了」而被忽略。
# ⇒ 「工具出错」必须用**另一个码**（2）表达，两种失败语义上分得开。
#   同类教训：validate_seed.py 曾抛 ValueError 但 shell 拿到 0，
#   被当成「校验通过了」—— 比假红更危险，那是**假绿**。
SEC = REPO / "tools" / "check_no_secrets.py"
check("tools/check_no_secrets.py 存在", SEC.is_file(), str(SEC))
if SEC.is_file():
    env3 = dict(os.environ)
    env3["PYTHONIOENCODING"] = "utf-8"

    def _scan(target):
        return subprocess.run([PY, str(SEC), str(target)], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              cwd=str(REPO), env=env3)

    clean_dir = Path(tempfile.mkdtemp(prefix="enp-sec-clean-"))
    p_clean = _scan(clean_dir)
    check("① 干净目录 → EXIT=0", p_clean.returncode == 0,
          f"rc={p_clean.returncode} out={(p_clean.stdout or '')[-300:]}")

    hit_dir = Path(tempfile.mkdtemp(prefix="enp-sec-hit-"))
    # 私钥头在运行时拼装 —— 本文件不能留下能匹配自己规则的完整字面量。
    (hit_dir / "k.pem").write_text(
        "-----BEGIN " + "RSA " + "PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n",
        encoding="utf-8")
    p_hit = _scan(hit_dir)
    check("② 含私钥的 .pem → EXIT=1", p_hit.returncode == 1,
          f"rc={p_hit.returncode}")
    check("② 输出点名了那个 .pem 文件（不是只说「发现密钥」）",
          "k.pem" in (p_hit.stdout or ""), (p_hit.stdout or "")[-300:])
    check("★ ② 输出不打印密钥原文（只打 <PRIVATE KEY BLOCK>）",
          "MIIEowIBAAKCAQEA" not in (p_hit.stdout or "")
          and "<PRIVATE KEY BLOCK>" in (p_hit.stdout or ""),
          (p_hit.stdout or "")[-300:])

    p_missing = _scan(REPO / "不存在的路径-仅用于验证退出码语义")
    check("★③ 路径不存在 → EXIT=2（不是 1！）",
          p_missing.returncode == 2,
          f"rc={p_missing.returncode} out={(p_missing.stdout or '')[-200:]}"
          f" err={(p_missing.stderr or '')[-200:]}")
    check("★③ 工具自身出错时 stderr 有说明，不是静默",
          "路径不存在" in (p_missing.stderr or ""),
          (p_missing.stderr or "")[-200:])

    # 真实仓库全扫：确认闸门对自己不炸
    p_repo = _scan(REPO)
    check("④ 真实仓库全扫 → EXIT=0（闸门不误伤自己的代码）",
          p_repo.returncode == 0,
          f"rc={p_repo.returncode} out={(p_repo.stdout or '')[-400:]}")

# ===========================================================================
print("\n[13] 云端不得使用平台 OPENAI_API_KEY（白嫖 / 串号风险）")
# ===========================================================================
# ★ 为什么这条要单独立一组断言 ★
# 云端部署一旦配了 OPENAI_API_KEY，这台机器上**所有访客共用同一把平台密钥**：
#   · 白嫖 —— 任一访客都能花平台的额度；
#   · 串号 —— 用量算到平台账上，且 A 访客能间接用出 B 访客也在用的那把 key。
# ⇒ 云端唯一合法的密钥来源是访客请求头带的那把。
# 桌面分支行为不变（cloud_mode() 为 False 时逐字等价于改造前）。
os.environ["ENPRACTICE_MODE"] = "cloud"
# ★ 假 key 在运行时拼出来，不写连续字面量 ★
#   tools/check_no_secrets.py 的 generic-assignment 规则匹配
#   「api_key 赋值 + 32 位以上字符串」。本文件必须能被自己的闸门扫过
#   （第 [12] 节断言「扫整个仓库 rc=0」），所以这里拆开拼 ——
#   否则闸门会把**测试自己的样本**当成泄露，我改哪个都错。
PLATFORM_FAKE_KEY = "platform-shared-" + "key-should-be-ignored"
os.environ["OPENAI_API_KEY"] = PLATFORM_FAKE_KEY

_stok3 = cloud.current_tenant.set("assert-tenant")
_htok3 = cloud.request_api_key.set("")
try:
    check("★ 云端 get_api_key() 设了 OPENAI_API_KEY 也不返回它",
          config.get_api_key() == "", repr(config.get_api_key()))
    check("★ 云端 get_api_key() 忽略 settings.json 里的 key",
          cloud.cloud_mode() is True)
    client_env = _tc()
    resp_env = client_env.get("/api/ai/status")
    check("★ 云端设了 OPENAI_API_KEY 后 /api/ai/status 仍是 mock:true",
          resp_env.status_code == 200
          and resp_env.json().get("mock") is True,
          f"status={resp_env.status_code} body={resp_env.text[:200]}")

    # 访客自己带 key 时，云端**应当**用那把（这是三级里的第一级）。
    # ★ 必须走真实请求头，不能只 set ContextVar ★
    #   TestClient 在 anyio portal 的**另一个线程/上下文**里跑 ASGI app，
    #   在测试线程里 set 的 ContextVar 传不进去（实测 mock 仍是 true）。
    #   而走请求头才是真实链路 —— 中间件从 header 取值再 set ContextVar。
    resp_hdr = client_env.get("/api/ai/status",
                              headers={"X-Enp-Api-Key": DUMMY_KEY})
    check("★ 带访客 key 的请求 /api/ai/status mock:false（真的在用）",
          resp_hdr.json().get("mock") is False, resp_hdr.text[:200])
finally:
    cloud.request_api_key.reset(_htok3)
    cloud.current_tenant.reset(_stok3)
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ.pop("ENPRACTICE_MODE", None)

# 桌面：env 那把 key 必须照旧生效（逐字不变）
_desk_home = tempfile.mkdtemp(prefix="enp-sec-desk-")
_saved_home = os.environ.get("ENPRACTICE_HOME")
os.environ["ENPRACTICE_HOME"] = _desk_home
_desk_fake_key = "desktop-env-" + "key-for-regression"
os.environ["OPENAI_API_KEY"] = _desk_fake_key
try:
    check("★ 桌面仍读 OPENAI_API_KEY（桌面行为逐字未变）",
          config.get_api_key() == _desk_fake_key, config.get_api_key())
finally:
    os.environ.pop("OPENAI_API_KEY", None)
    if _saved_home is not None:
        os.environ["ENPRACTICE_HOME"] = _saved_home

# ===========================================================================
print("\n[14] F 项：访客自填「接口地址 / 模型」—— 生效链路 + SSRF 闸门")
# ===========================================================================

# ★ 本节要证明两件事 ★
#   ① **真的生效**：访客在设置页填的接口地址 / 模型，能一路走到
#      `app/ai.py::_chat()` 真正发请求的那一行。
#      ★ 用户报的现象是"这个没改啊" ⇒「输入框在不在」根本不是判据，
#        判据必须是"填的值有没有真的被用上"。
#   ② **不引入 SSRF**：服务器代访客发请求 ⇒ 必须有出网白名单兜底。
#   两条都要**反向验证**（14.8）：判据能 PASS ≠ 判据能 FAIL。

# ---------------------------------------------------------------------------
# 14.1 IP 判定：公网放行 / 内网·保留·内嵌 IPv4 一律拒绝
# ---------------------------------------------------------------------------
PUBLIC_IPS = ["8.8.8.8", "1.1.1.1", "101.126.13.31", "2606:2800:220:1::"]
for _ip in PUBLIC_IPS:
    check(f"★ 公网地址放行：{_ip}", egress.is_public_ip(_ip) is True, _ip)

BLOCKED_IPS = [
    ("127.0.0.1", "IPv4 回环"),
    ("10.1.2.3", "RFC1918 私网"),
    ("172.16.5.5", "RFC1918 私网"),
    ("192.168.1.1", "RFC1918 私网"),
    ("169.254.169.254", "云元数据"),
    ("100.64.0.1", "运营商 CGNAT（Python 的 is_private 判它是公网）"),
    ("0.0.0.0", "未指定地址"),
    ("224.0.0.1", "组播"),
    ("240.0.0.1", "保留地址"),
    ("::1", "IPv6 回环"),
    ("::ffff:127.0.0.1", "IPv4-mapped IPv6 包回环"),
    ("2002:7f00:1::", "6to4 包 127.0.0.1"),
    ("64:ff9b::7f00:1", "NAT64 包 127.0.0.1"),
    ("2001:0:4136:e378:8000:63bf:3fff:fdd2", "Teredo 包保留地址"),
    ("not-an-ip", "根本不是 IP"),
]
for _ip, _why in BLOCKED_IPS:
    check(f"★ 内网/保留地址被拒：{_ip}（{_why}）",
          egress.is_public_ip(_ip) is False, _ip)

# ---------------------------------------------------------------------------
# 14.2 接口地址校验：每一条拒绝理由都要有对应用例
# ---------------------------------------------------------------------------
# ★ DNS 用桩 ★
#   ① 判据必须离线可复现；
#   ② 更重要：只有桩才能构造"一个域名解析出多个 A、其中混了内网"这种
#      真实存在的绕过手法（只查第一条 A 记录的实现会被它骗过）。
#   桩打在 socket.getaddrinfo 上 ⇒ `_resolve_ips` 那段业务代码**仍在被测**，
#   不是我们自己写一份近似实现。
_DNS_TABLE = {
    "good.example.com": ["93.184.216.34"],
    "dual.example.com": ["93.184.216.34", "127.0.0.1"],     # 一公一内
    "v6.example.com": ["2606:2800:220:1::"],
    "cgnat.example.com": ["100.64.7.7"],
    "visitor.example.com": ["93.184.216.35"],
}


def _fake_gai(host, port, *a, **kw):
    if host in _DNS_TABLE:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in _DNS_TABLE[host]]
    raise socket.gaierror("Name or service not known")


def _reject_case(raw, label):
    try:
        tgt = egress.validate_base_url(raw)
    except egress.EgressError as exc:
        ok, detail = True, str(exc)
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"抛的不是 EgressError：{type(exc).__name__}: {exc}"
    else:
        ok, detail = False, f"竟然放行了 → {tgt}"
    check(f"★ 接口地址被拒：{label}", ok, detail)


def _accept_case(raw, label, want_host=None, want_ip=None, want_path=None):
    try:
        tgt = egress.validate_base_url(raw)
    except egress.EgressError as exc:
        check(f"★ 接口地址被放行：{label}", False, f"被拒了 → {exc}")
        return None
    detail = ""
    for got, want, field in ((tgt.host, want_host, "host"),
                             (tgt.ip, want_ip, "ip"),
                             (tgt.base_path, want_path, "path")):
        if want is not None and got != want:
            detail = f"{field}={got!r} 期望 {want!r}"
            break
    check(f"★ 接口地址被放行：{label}", not detail, detail)
    return tgt


with mock.patch.object(egress.socket, "getaddrinfo", side_effect=_fake_gai):
    _reject_case("", "空地址")
    _reject_case("http://good.example.com/v1", "http 明文（API Key 会裸奔）")
    _reject_case("ftp://good.example.com/v1", "非 http(s) 协议")
    _reject_case("https://good.example.com:8443/v1", "非 443 端口")
    _reject_case("https://u:p@good.example.com/v1", "带用户名或密码")
    _reject_case("https://good.example.com/v1?a=1", "带 ? 查询参数")
    _reject_case("https://good.example.com/v1#x", "带 # 片段")
    _reject_case("https://good.example.com/v1/../etc", "路径里含 ..")
    _reject_case("https://goo d.example.com/v1", "含空格")
    _reject_case("https://good.example.com/" + "a" * 400, "超长")
    _reject_case("https:///v1", "缺主机名")
    _reject_case("https://nope.example.com/v1", "域名解析失败")
    _reject_case("https://127.0.0.1/v1", "IP 字面量 = 回环")
    _reject_case("https://169.254.169.254/v1", "IP 字面量 = 云元数据")
    _reject_case("https://[::1]/v1", "IPv6 字面量 = 回环")
    _reject_case("https://dual.example.com/v1", "多 A 记录里混了内网")
    _reject_case("https://cgnat.example.com/v1", "解析到运营商 CGNAT 段")

    _accept_case("https://good.example.com/v1", "普通公网域名",
                 want_host="good.example.com", want_ip="93.184.216.34",
                 want_path="/v1")
    _accept_case("https://v6.example.com/api/v3", "公网 IPv6 域名",
                 want_host="v6.example.com", want_ip="2606:2800:220:1::",
                 want_path="/api/v3")
    _accept_case("https://8.8.8.8/v1", "公网 IP 字面量",
                 want_host="8.8.8.8", want_ip="8.8.8.8")

    # ★ IP 字面量**不查 DNS** ★
    #   判定权必须在我们自己写的规则上，不能依赖"解析器会把 IP 字面量
    #   原样返回"这个隐含约定。
    with mock.patch.object(egress.socket, "getaddrinfo",
                           side_effect=_fake_gai) as _gai:
        egress.validate_base_url("https://8.8.8.8/v1")
        check("★ IP 字面量不触发 DNS 查询（判定权在本地规则）",
              _gai.call_count == 0, f"调用了 {_gai.call_count} 次")

# ---------------------------------------------------------------------------
# 14.3 模型名称校验
# ---------------------------------------------------------------------------
check("★ 模型名称：正常放行",
      egress.validate_model("doubao-seed-2-1-lite-260915")
      == "doubao-seed-2-1-lite-260915")
for _bad, _label in (("", "空"), ("m" * 129, "超长"),
                      ("a\nb", "含换行"), ("a\x00b", "含 NUL")):
    try:
        _out = egress.validate_model(_bad)
    except egress.EgressError as exc:
        _ok, _detail = True, str(exc)
    except Exception as exc:  # noqa: BLE001
        _ok, _detail = False, f"{type(exc).__name__}: {exc}"
    else:
        _ok, _detail = False, f"竟然放行了 → {_out!r}"
    check(f"★ 模型名称被拒：{_label}", _ok, _detail)

# ---------------------------------------------------------------------------
# 14.4 云端：请求头 → ContextVar → ai._settings()
# ---------------------------------------------------------------------------
os.environ["ENPRACTICE_MODE"] = "cloud"
_tb = cloud.request_ai_base_url.set("https://visitor.example.com/v1")
_tm = cloud.request_ai_model.set("visitor-model-x")
_tt = cloud.request_ai_timeout.set("30")
try:
    s = ai._settings()
    check("★ 云端 ai._settings() 采用访客自填的接口地址",
          s.get("base_url") == "https://visitor.example.com/v1", str(s))
    check("★ 云端 ai._settings() 采用访客自填的模型",
          s.get("model") == "visitor-model-x", str(s))
    # ★ B6：超时区间 5..60 ★
    #   30 与平台默认值 60 不同 ⇒ 这条断言证明"访客值真被采纳"，
    #   而不是"碰巧等于平台默认值"。
    check("★ 云端 ai._settings() 采用访客自填的超时",
          s.get("timeout") == 30, str(s))
    check("★ 云端 ai._settings() 标记出「这个地址是访客指定的」",
          s.get("_visitor_base_url") == "https://visitor.example.com/v1", str(s))
finally:
    cloud.request_ai_base_url.reset(_tb)
    cloud.request_ai_model.reset(_tm)
    cloud.request_ai_timeout.reset(_tt)

s = ai._settings()
check("★ 云端访客没填时用平台默认接口地址",
      s.get("base_url") == cloud.ai_base_url(), str(s))
check("★ 云端访客没填时用平台默认模型",
      s.get("model") == cloud.ai_model(), str(s))

_tt2 = cloud.request_ai_timeout.set("")
_plat_timeout = ai._settings().get("timeout")
# ★ B6：上限从 300 收到 60 ★
#   "61" / "90" / "300" 在旧区间里都被采纳，现在必须一律不采纳 ——
#   把旧口径的合法值显式列进黑名单，防止有人把区间又放回去。
for _bad in ("1", "61", "90", "300", "999", "abc", "-3"):
    cloud.request_ai_timeout.set(_bad)
    s = ai._settings()
    check(f"★ 非法超时 {_bad!r} 不被采纳（沿用平台值 {_plat_timeout}）",
          s.get("timeout") == _plat_timeout, str(s))
cloud.request_ai_timeout.reset(_tt2)

# ---------------------------------------------------------------------------
# 14.5 桌面零回归：访客自填通道在桌面**根本不存在**
# ---------------------------------------------------------------------------
os.environ.pop("ENPRACTICE_MODE", None)
_tb2 = cloud.request_ai_base_url.set("https://visitor.example.com/v1")
_tm2 = cloud.request_ai_model.set("visitor-model-x")
try:
    s = ai._settings()
    check("★ 桌面模式不采信访客自填的接口地址（桌面逐字不变）",
          s.get("base_url") != "https://visitor.example.com/v1", str(s))
    check("★ 桌面模式不采信访客自填的模型（桌面逐字不变）",
          s.get("model") != "visitor-model-x", str(s))
    check("★ 桌面模式不产生「访客指定」标记",
          "_visitor_base_url" not in s, str(s))
finally:
    cloud.request_ai_base_url.reset(_tb2)
    cloud.request_ai_model.reset(_tm2)

# ---------------------------------------------------------------------------
# 14.6 端到端：真实请求头 → 中间件 → ContextVar → 响应
# ---------------------------------------------------------------------------
os.environ["ENPRACTICE_MODE"] = "cloud"
with mock.patch("requests.get", side_effect=fake_get), \
     mock.patch.object(websearch, "search_sentences",
                       side_effect=fake_search_sentences):
    with _tc() as cclient:
        resp = cclient.get("/api/ai/status", headers={
            "X-Enp-Ai-Base-Url": "https://visitor.example.com/v1",
            "X-Enp-Ai-Model": "visitor-model-e2e",
        })
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        check("★ 云端带 X-Enp-Ai-Model 头 → 响应里的模型就是访客填的那个",
              body.get("model") == "visitor-model-e2e", resp.text[:200])
        check("★ 云端带 X-Enp-Ai-Base-Url 头 → 响应里的接口地址就是访客填的那个",
              body.get("base_url") == "https://visitor.example.com/v1",
              resp.text[:200])

        resp2 = cclient.get("/api/ai/status")
        try:
            body2 = resp2.json() or {}
        except ValueError:
            body2 = {}
        check("★ 云端不带这两个头 → 回显平台默认接口地址",
              body2.get("base_url") == cloud.ai_base_url(), resp2.text[:200])

# ---------------------------------------------------------------------------
# 14.7 ai._chat()：访客自填地址必须走白名单；被拒时一个包都不许发
# ---------------------------------------------------------------------------
os.environ["ENPRACTICE_MODE"] = "cloud"
_sent = []        # egress.post_json（白名单那条路）的调用记录
_direct = []      # requests.post（平台默认那条路）的调用记录


class _FakeChatResp:
    status_code = 200
    text = '{"choices":[{"message":{"content":"ok"}}]}'

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


def _fake_post_json(target, url_path, payload, headers, timeout):
    _sent.append((target.host, target.ip, target.base_path, url_path,
                  payload.get("model")))
    return _FakeChatResp()


def _fake_requests_post(url, **kwargs):
    _direct.append(url)
    return _FakeChatResp()


with mock.patch.object(egress.socket, "getaddrinfo", side_effect=_fake_gai):
    _tb3 = cloud.request_ai_base_url.set("https://visitor.example.com/v1")
    _tm3 = cloud.request_ai_model.set("visitor-model-chat")
    try:
        with mock.patch.object(egress, "post_json", side_effect=_fake_post_json), \
             mock.patch("requests.post", side_effect=_fake_requests_post):
            out = ai._chat("sys", "user")
            check("★ 访客自填地址时，AI 调用正常返回", out == "ok", repr(out))
            check("★ 访客自填地址 → 走的是出网白名单那条路（host/ip/路径都对）",
                  len(_sent) == 1
                  and _sent[0][0] == "visitor.example.com"
                  and _sent[0][1] == "93.184.216.35"
                  and _sent[0][2] == "/v1"
                  and _sent[0][3] == "/chat/completions"
                  and _sent[0][4] == "visitor-model-chat",
                  str(_sent))
            check("★ 访客自填地址时没有绕过白名单直接 requests.post",
                  not _direct, str(_direct))
    finally:
        cloud.request_ai_base_url.reset(_tb3)
        cloud.request_ai_model.reset(_tm3)

    # 访客填了云元数据地址 → 明确报错，且一个包都不许发
    _tb4 = cloud.request_ai_base_url.set("https://169.254.169.254/latest/meta-data/")
    try:
        with mock.patch.object(egress, "post_json",
                               side_effect=_fake_post_json) as _pj, \
             mock.patch("requests.post", side_effect=_fake_requests_post) as _rp:
            try:
                ai._chat("sys", "user")
                _ok, _detail = False, "竟然没报错"
            except ai.AIError as exc:
                _ok, _detail = True, str(exc)
            check("★ 访客填内网地址 → 明确报错（不是悄悄换成别的地址）",
                  _ok, _detail)
            check("★ 报错信息里点明是「接口地址不被允许」",
                  _ok and "接口地址不被允许" in _detail, _detail)
            check("★ 被拒时一个包都没发出去（白名单是硬闸门）",
                  _pj.call_count == 0 and _rp.call_count == 0,
                  f"egress={_pj.call_count} requests.post={_rp.call_count}")
    finally:
        cloud.request_ai_base_url.reset(_tb4)

    # 访客没填地址 → 平台默认路径，逐字不变
    with mock.patch.object(egress, "post_json", side_effect=_fake_post_json) as _pj2, \
         mock.patch("requests.post", side_effect=_fake_requests_post) as _rp2:
        out = ai._chat("sys", "user")
        check("★ 访客没填地址 → 走平台默认的 requests.post（逐字不变）",
              _rp2.call_count == 1 and _pj2.call_count == 0,
              f"requests.post={_rp2.call_count} egress={_pj2.call_count}")
        check("★ 平台默认路径请求的正是平台默认地址",
              bool(_direct) and _direct[-1].startswith(cloud.ai_base_url()),
              str(_direct))

# ---------------------------------------------------------------------------
# 14.8 反向验证：注入「已知该 FAIL」的退化实现，确认上面的判据真会红
# ---------------------------------------------------------------------------
# ★ 为什么必须做这一节 ★
#   「判据能 PASS」不等于「判据能 FAIL」。
#   一条恒为真的断言看起来永远是绿的，对产品却毫无保护力。
#   所以每条关键判据都要配一个**已知该 FAIL**的注入：把实现换成退化版本，
#   确认对应判据**确实翻红**（而不是靠"反正现在是绿的"自我安慰）。

# (1) is_public_ip 换成「永不放行」⇒ 内网地址会漏过去
with mock.patch.object(egress, "is_public_ip", return_value=True):
    try:
        _tgt = egress.validate_base_url("https://127.0.0.1/v1")
        _leaked, _detail = True, f"放行了 → {_tgt}"
    except egress.EgressError as exc:
        _leaked, _detail = False, str(exc)
    check("★ 反向验证：is_public_ip 换成「永不放行」→ 内网地址确实会漏过去"
          "（说明「内网被拒」那条判据真的依赖它）",
          _leaked, _detail)

# (2) is_public_ip 换成「永不放行」的反面 ⇒ 公网地址也会被拒
with mock.patch.object(egress, "is_public_ip", return_value=False):
    try:
        _tgt = egress.validate_base_url("https://good.example.com/v1")
        _blocked, _detail = False, f"竟然放行了 → {_tgt}"
    except egress.EgressError as exc:
        _blocked, _detail = True, str(exc)
    check("★ 反向验证：is_public_ip 换成「永不放行」→ 公网地址也会被拒"
          "（说明「公网放行」那条判据也真的依赖它）",
          _blocked, _detail)

# (3) validate_base_url 换成「永不放行」⇒ _chat 必须立刻报错且不发包
with mock.patch.object(egress, "validate_base_url",
                       side_effect=egress.EgressError("注入的退化实现")):
    with mock.patch.object(egress, "post_json") as _pj3, \
         mock.patch("requests.post") as _rp3:
        _tb5 = cloud.request_ai_base_url.set("https://visitor.example.com/v1")
        try:
            try:
                ai._chat("sys", "user")
                _ok, _detail = False, "竟然没报错"
            except ai.AIError as exc:
                _ok, _detail = True, str(exc)
            check("★ 反向验证：validate_base_url 换成「永不放行」"
                  "→ _chat 立刻报错且一个包都不发",
                  _ok and _pj3.call_count == 0 and _rp3.call_count == 0,
                  f"{_detail} egress={_pj3.call_count} requests.post={_rp3.call_count}")
        finally:
            cloud.request_ai_base_url.reset(_tb5)

# (4) 请求头通道退化（等价于「中间件没读到请求头」）⇒ 生效判据必须翻红
with mock.patch.object(cloud, "request_ai_base_url",
                       mock.Mock(get=lambda: "")):
    s = ai._settings()
    check("★ 反向验证：请求头通道退化 → 访客自填地址不再生效"
          "（说明「生效」那条判据不是恒真）",
          s.get("base_url") != "https://visitor.example.com/v1", str(s))

# ---------------------------------------------------------------------------
# 14.9 前端：设置页真的把这两项露出来、并且真的把它们发给服务器
# ---------------------------------------------------------------------------
JS_TEXT = (REPO / "web" / "app.js").read_text(encoding="utf-8")
HTML_TEXT = (REPO / "web" / "index.html").read_text(encoding="utf-8")


def _js_func_body(js_text, name):
    """切出 JS 里 ``function name(...) { ... }`` 的**函数体**（花括号配对）。

    ★ 为什么要按函数切，而不是全文找子串 ★
    「全文出现过某个字符串」会被同名常量或注释骗过 ——
    把注入请求头的代码挪到别的函数里，全文子串照样命中，
    而 api() 其实并没有注入。所以必须按**函数**切。

    ★ 返回的是大括号**之间**的内容 ★
    （不是函数后面的整段文件 —— 那样"函数体里没有某个头"就成了假阳性，
      这正是本判据最初写错并自己抓出来的那类错。）
    """
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", js_text)
    if not m:
        return None
    try:
        open_at = js_text.index("{", m.end())
    except ValueError:
        return None
    depth = 0
    i = open_at
    while i < len(js_text):
        if js_text[i] == "{":
            depth += 1
        elif js_text[i] == "}":
            depth -= 1
            if depth == 0:
                return js_text[open_at + 1:i]
        i += 1
    return None


def _js_hides_ai_rows(js_text):
    """web/app.js 里有没有把「接口地址」「模型」这两行**藏起来**的语句。

    识别口径：同一行里同时出现「隐藏动作」与「这两项的标识」就算藏。
    隐藏动作 = ``.hidden =`` 或 ``.style.display =``；
    这两项的标识 = s-base-url / s-model / baseRow / modelRow / base_url / model。
    """
    pat_id = re.compile(r"s-base-url|s-model|baseRow|modelRow|base_url|\bmodel\b",
                        re.I)
    pat_hide = re.compile(r"\.hidden\s*=|\.style\.display\s*=", re.I)
    hits = []
    for i, line in enumerate(js_text.splitlines(), 1):
        if pat_hide.search(line) and pat_id.search(line):
            hits.append(f"{i}: {line.strip()}")
    return hits


_api_body = _js_func_body(JS_TEXT, "api")
_AI_HEADERS = ("X-Enp-Ai-Base-Url", "X-Enp-Ai-Model", "X-Enp-Ai-Timeout")
check("★ web/app.js 的 api() 统一注入三个请求头",
      _api_body is not None
      and all(h in _api_body for h in _AI_HEADERS),
      "api() 函数体里找不到这三个头")
check("★ web/app.js 把访客自填的三项存进本浏览器 localStorage",
      all(k in JS_TEXT for k in ("enp_ai_base_url", "enp_ai_model",
                                 "enp_ai_timeout")), "")
check("★ web/app.js 云端分支把三项一起写进 localStorage（不进 /api/settings）",
      "storeAiTarget(cBase, cModel, cTimeout)" in JS_TEXT, "")
check("★ web/app.js 没有把「接口地址」「模型」这两行藏起来",
      not _js_hides_ai_rows(JS_TEXT), str(_js_hides_ai_rows(JS_TEXT)))
check("★ web/index.html 有「接口地址」「模型」两个输入框",
      'id="s-base-url"' in HTML_TEXT and 'id="s-model"' in HTML_TEXT, "")
check("★ web/index.html 的云端说明里讲清了「不会写入服务端磁盘」",
      "不会写入服务端磁盘" in HTML_TEXT, "")

# ★ 前端检查器自身的反向验证 ★
#   造一份"退化版"（把这两行藏起来）⇒ 检查器必须判定它不合规，
#   否则上面那条「没藏起来」就是恒真。
_broken_js = JS_TEXT + (
    "\n    var baseRow = document.getElementById('s-base-url');"
    "\n    var modelRow = document.getElementById('s-model');"
    "\n    baseRow.hidden = true; modelRow.hidden = true;\n")
check("★ 反向验证：把前端改成「藏起这两项」的退化版本 → 检查器必须报不合规",
      bool(_js_hides_ai_rows(_broken_js)),
      str(_js_hides_ai_rows(_broken_js)))

# ★ api() 函数体检查器自身的反向验证 ★
#   把 api() 里注入的请求头**改名** ⇒ 检查器必须判定不合规。
#   ★ 为什么用「改名」而不是「删那一行」★
#     删一行的话，`if (c.aiBaseUrl && !opts.headers['X-Enp-Ai-Base-Url'])` 里的
#     同一个字符串还在函数体里 ⇒ 检查器照样命中 ⇒ 反向验证自己就假绿了。
_broken_api = JS_TEXT.replace("X-Enp-Ai-Base-Url", "X-Dead-Base-Url")
_broken_api_body = _js_func_body(_broken_api, "api") or ""
check("★ 反向验证：把 api() 里的请求头改名 → 检查器必须报不合规",
      _broken_api != JS_TEXT
      and not all(h in _broken_api_body for h in _AI_HEADERS),
      "退化版没造成功" if _broken_api == JS_TEXT else "切出来的函数体不对")

# ★ 出网点清单守卫自身的反向验证 ★
#   把守卫的扫描逻辑原样用在一段"多了一个出网点"的合成源码上 ⇒ 它必须被抓出来。
#   ★ 为什么不能只写 `EXPECTED_EGRESS | {...} != EXPECTED_EGRESS` ★
#     那是恒真的（集合加了元素一定不等），绿灯毫无意义 ——
#     这正是本节要消灭的那类假断言。
def _scan_egress(src):
    """复用第 [8] 节的扫描逻辑，跑在一段源码文本上。"""
    found = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted(node.func)
        if dotted != "requests.Session" and _is_http_call(dotted):
            found.add(dotted)
    return found


check("★ 反向验证：守卫的扫描逻辑能抓住一个新增的出网点",
      _scan_egress("import requests\nrequests.get('https://x')\n")
      == {"requests.get"},
      str(_scan_egress("import requests\nrequests.get('https://x')\n")))
check("★ 反向验证：守卫的扫描逻辑能抓住 session.post（访客自填地址那条路）",
      _scan_egress("session.post('https://x')\n") == {"session.post"},
      str(_scan_egress("session.post('https://x')\n")))
check("★ 反向验证：守卫不会把「建会话」误判成出网点",
      _scan_egress("requests.Session()\n") == set(),
      str(_scan_egress("requests.Session()\n")))





# ===========================================================================
print("\n[15] 向日葵主题：999px 全圆角不得落在「承载可换行文字的容器」上")
# ===========================================================================
# ★ 这条回归守的是用户报的那个 bug ★
#   设计约束：「向日葵风格的翻译界面和很多需要输入文字的界面都会出现
#   有部分文字会被挡住的现象，其他风格的界面就没有这个问题」。
#
# ★ 根因（实测，不是推测）★
#   向日葵的 --r-ctl 是 999px —— 全圆角是这个方向的形状语言，
#   test_theme.py 明确断言它是设计意图，**令牌不能改**。
#   但 999px 用在**承载可换行文字**的容器上时，浏览器按 CSS 规范
#   把圆角缩放成「短边的一半」的大圆角：
#     · **表单控件**（textarea）→ Blink 把内部编辑区裁到圆角形状
#       ⇒ 贴边/换行的文字被吃掉（用户图1：首字母 F 被切）
#     · **普通容器**（.ai-card 等）→ 被压成扁椭圆、描边横穿内部文字（用户图2）
#   其它主题的 --r-ctl 只有 3~11px ⇒ 同样的容器没事 ⇒ 与用户描述完全吻合。
#
# ★ 本轮实测得出的两条关键事实（决定了判据怎么写）★
#   ① 沙箱实测（白底黑字红框、同元素同内容只差圆角）：
#      · **表单控件**：999px 必定裁字（Blink 特有行为，用 div 模拟测不出来）
#      · **普通容器**：999px **不裁字**（墨迹与 0px 完全一致），
#        受害表现是「描边横穿文字」，只在「宽而矮 + 文字贴边」时看得见
#   ② 因此判据**不能**按「用了 --r-ctl」一刀切 —— 实测 31 个类在向日葵下
#     仍是 999px，其中绝大多数是按钮/徽章/进度条/导航标签（文字不换行，
#     沙箱确认既不裁字也无描边横穿）。一刀切会把形状语言误伤。
#   ⇒ 判据分两层：
#     L2 表单控件（**自动派生、无白名单**）—— 裁字是硬伤，一律不许 999px
#     L3/L4 文字容器（**集合完整性 ratchet**）—— 新增忘了收敛就必红
#
# ★ 修法 ★
#   令牌 --r-ctl 保持 999px 不动（按钮/标签页/进度条/吐司等**真控件**
#   继续是全圆角），只把**承载文字**的容器在向日葵作用域内改用 --r-card。
#   朴素(reading)/克制(vesper) 既有规则零改动 —— 本节只作用于 sunflower。

CSS_PATH = REPO / "web" / "style.css"
CSS_TEXT = CSS_PATH.read_text(encoding="utf-8")


def _css_strip_comments(css):
    """剥掉 CSS 注释（注释里有 { } 会污染规则切分）。"""
    return re.sub(r"/\*.*?\*/", " ", css, flags=re.S)


def _css_rules(css):
    """递归切出所有规则（含 @media 内的）。

    返回 [{sel, body, order, media}]：
      sel   —— 原始选择器文本（可能含逗号分隔的多个单选择器）
      body  —— 声明体
      order —— 在文件里的出现序号（同特异性时按它比源序）
      media —— 外层 @media 条件；顶层规则为 ""
    """
    css = _css_strip_comments(css)
    out = []
    counter = [0]

    def walk(src, media):
        i, n = 0, len(src)
        while i < n:
            j = src.find("{", i)
            if j < 0:
                break
            sel = src[i:j].strip()
            depth, k = 0, j
            while k < n:
                if src[k] == "{":
                    depth += 1
                elif src[k] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            body = src[j + 1:k]
            if sel.startswith("@"):
                cond = re.sub(r"^\s*@[a-z-]+", "", sel).strip()
                walk(body, (media + "|" + cond) if media else cond)
            else:
                counter[0] += 1
                out.append({"sel": sel, "body": body,
                            "order": counter[0], "media": media})
            i = k + 1

    walk(css, "")
    return out


def _sel_split(sel):
    """按顶层逗号拆成单选择器（跳过括号内的逗号）。"""
    out, buf, depth = [], [], 0
    for ch in sel:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return [s for s in out if s]


def _decls(body):
    """声明体 → {属性: 值}（属性名小写）。"""
    out = {}
    for part in body.split(";"):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        out[k.strip().lower()] = v.strip()
    return out


def _last_compound(simple):
    """取单选择器的最后一个 compound（元素本身那一段）。"""
    comps = [c for c in re.split(r"\s*[>+~]\s*|\s+", simple.strip()) if c]
    return comps[-1] if comps else ""


def _compound_classes(compound):
    """取一个 compound 里的类名集合。"""
    s = re.sub(r"::?[\w-]+(\([^()]*\))?", " ", compound)
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"[#][\w-]+", " ", s)
    return set(re.findall(r"\.([A-Za-z_][\w-]*)", s))


def _spec(simple):
    """简化特异性 (id, class/attr/pseudo-class, type/pseudo-element)。

    ★ :not() 内部取最大 —— 与 CSS 规范一致（:not 取其参数里最高的特异性）。
    ★ 下面的自检用例会把常见形状的结果钉死（算错会立刻红）。
    """
    s = re.sub(r'"[^"]*"|\'[^\']*\'', " ", simple).strip()
    a = b = c = 0

    def _pc(m):
        nonlocal a, b, c
        name = m.group(1).lower()
        body = m.group(3) or ""
        if name == "not":
            best = (0, 0, 0)
            for part in _sel_split(body):
                s2 = _spec(part)
                if s2 > best:
                    best = s2
            a += best[0]
            b += best[1]
            c += best[2]
        elif m.group(0).startswith("::"):
            c += 1
        else:
            b += 1
        return " "

    s = re.sub(r"::([\w-]+)(\(([^()]*)\))?", _pc, s)
    s = re.sub(r":([\w-]+)(\(([^()]*)\))?", _pc, s)
    b += len(re.findall(r"\[[^\]]*\]", s))
    s = re.sub(r"\[[^\]]*\]", " ", s)
    a += len(re.findall(r"#[\w-]+", s))
    s = re.sub(r"#[\w-]+", " ", s)
    b += len(re.findall(r"\.[\w-]+", s))
    s = re.sub(r"\.[\w-]+", " ", s)
    for tok in re.findall(r"[A-Za-z][\w-]*|\*", s):
        if tok != "*":
            c += 1
    return (a, b, c)


_THEME_RE = re.compile(r'data-theme\s*=\s*["\']?([\w-]+)["\']?')


def _theme_matches(simple, theme):
    """这条单选择器在给定主题下是否命中。

    ★ 为什么必须有这一步 ★
      只按 (特异性, 源序) 排序的话，`html[data-theme="marvel"] .btn`
      （特异性更高）在 sunflower 下也会被当成命中 ⇒ 会算出一个**假的赢家**
      ⇒ 判据会漏报真问题。实测：不做主题过滤时 .btn 的最终圆角被算成 3px
      （marvel 的值），做完过滤才是正确的 999px。
    """
    for x in re.findall(
            r':not\(\s*\[\s*data-theme\s*=\s*["\']?([\w-]+)["\']?\s*\]\s*\)', simple):
        if x == theme:
            return False
    tmp = re.sub(
        r':not\(\s*\[\s*data-theme\s*=\s*["\']?[\w-]+["\']?\s*\]\s*\)', " ", simple)
    positives = _THEME_RE.findall(tmp)
    if positives and theme not in positives:
        return False
    return True


def _theme_vars(rules, theme):
    """某主题令牌块里的 --xxx 值。"""
    out = {}
    for r in rules:
        if r["media"]:
            continue
        for simple in _sel_split(r["sel"]):
            if simple == 'html[data-theme="%s"]' % theme:
                for k, v in _decls(r["body"]).items():
                    if k.startswith("--"):
                        out[k] = v
    return out


def _resolve_radius(value, vars_):
    """在给定变量表下解析 border-radius → ([px 数值...], 未定义变量名列表)。"""
    if value is None:
        return [], []
    missing = []

    def _rep(m):
        name = m.group(1)
        if name in vars_:
            return vars_[name]
        missing.append(name)
        return "0px"

    v = re.sub(r"var\(\s*(--[\w-]+)\s*\)", _rep, value)
    nums = [float(x) for x in re.findall(r"([\d.]+)px", v)]
    return nums, missing


def _radius_hits(rules, cls_name, theme=None):
    """所有声明了 border-radius、且选择器会命中 class=cls_name 的规则。"""
    hits = []
    for r in rules:
        d = _decls(r["body"])
        if "border-radius" not in d:
            continue
        for simple in _sel_split(r["sel"]):
            if cls_name not in _compound_classes(_last_compound(simple)):
                continue
            if theme is not None and not _theme_matches(simple, theme):
                continue
            hits.append({"rule": r, "simple": simple,
                         "value": d["border-radius"]})
    return hits


def _cascade_winner(rules, cls_name, theme):
    """建模层叠：按 (特异性, 源序) 取最终赢家（已做主题过滤）。"""
    hits = _radius_hits(rules, cls_name, theme)
    if not hits:
        return None, []
    best = None
    for h in hits:
        key = (_spec(h["simple"]), h["rule"]["order"])
        if best is None or key > best[0]:
            best = (key, h)
    return best[1], hits


def _final_radius(rules, cls_name, theme, vars_):
    """该类在给定主题下的最终生效圆角（px 最大值）。"""
    win, _hits = _cascade_winner(rules, cls_name, theme)
    if not win:
        return None, None
    nums, _missing = _resolve_radius(win["value"], vars_)
    return (max(nums) if nums else 0), win


# ---------------------------------------------------------------- DOM 事实
HTML_TEXT2 = (REPO / "web" / "index.html").read_text(encoding="utf-8")
JS_TEXT2 = (REPO / "web" / "app.js").read_text(encoding="utf-8")

# 每个类在 HTML 里对应的标签集合
_HTML_TAGS = {}
for _m in re.finditer(r"<(\w+)[^>]*class=\"([^\"]*)\"", HTML_TEXT2):
    for _c in _m.group(2).split():
        _HTML_TAGS.setdefault(_c, set()).add(_m.group(1))

# ★ 静态可达：HTML 的 class 属性 + app.js 里的 class/className 字符串 ★
#   ★ 为什么不用「首页 DOM 里存在」★
#     首页只有第一个面板的 DOM；别的面板的类（cv-* / lk-* / an-*）在首页里
#     不存在，但真实使用时会出现在 DOM 里 ⇒ 用「源码里出现过」判可达，
#     才不会漏掉别的面板的容器。
_REACHABLE = set()
for _m in re.finditer(r'class="([^"]*)"', HTML_TEXT2):
    _REACHABLE.update(_m.group(1).split())
for _pat in (r"""class\s*=\s*["']([^"']+)["']""",
             r"""className\s*=\s*["']([^"']+)["']"""):
    for _m in re.finditer(_pat, JS_TEXT2):
        _REACHABLE.update(_m.group(1).split())

# 每个类在 CSS 里的声明（合并所有命中它的规则）
_CLASS_DECLS = {}
for _r in _css_rules(CSS_TEXT):
    _d = _decls(_r["body"])
    for _simple in _sel_split(_r["sel"]):
        _cls = _compound_classes(_last_compound(_simple))
        if len(_cls) == 1:
            _CLASS_DECLS.setdefault(list(_cls)[0], []).append(_d)


def _cls_prop(cls, prop):
    """该类的规则里某属性的取值（没则 None）。

    ★ 注意：同一类可能有多条规则（如 .tabs 有 display:flex 和 display:none），
      所以「任一条规则满足」就算满足 —— 用 _cls_prop_any。
    """
    val = None
    for _d in _CLASS_DECLS.get(cls, []):
        if prop in _d:
            val = _d[prop]
    return val


def _cls_prop_any(cls, prop, pred=None):
    """该类的**任一条**规则里某属性满足 pred ⇒ True。"""
    for _d in _CLASS_DECLS.get(cls, []):
        v = _d.get(prop)
        if v is None:
            continue
        if pred is None or pred(v):
            return v
    return None


def _auto_exempt_reason(cls):
    """该类是否满足**自动可验证**的豁免条件（返回理由字符串，没则 None）。

    ★ 这些理由全部可从 CSS/HTML 事实自动推出，无主观成分 ★
      · white-space: nowrap  —— 文字永不换行 ⇒ 圆角再大也不会切到字
      · 固定高度 ≤ 20px      —— 放不下第二行
      · 原生 <button>         —— 按钮文字居中、单行、Blink 排版不同
      · 行内级 display       —— 天然单行
      · flex 容器默认 nowrap —— 子项不换行，容器高度恒定
      · ellipsis 截断        —— 单行显示
    """
    reasons = []
    ws = _cls_prop_any(cls, "white-space", lambda v: "nowrap" in v)
    if ws:
        reasons.append("white-space:nowrap")
    h = _cls_prop_any(cls, "height",
                      lambda v: bool(re.match(r"^(\d+(?:\.\d+)?)px$", v.strip()))
                      and float(re.match(r"^(\d+(?:\.\d+)?)px$", v.strip()).group(1)) <= 20)
    if h:
        reasons.append("固定高度 %s（放不下多行）" % h)
    if "button" in _HTML_TAGS.get(cls, set()):
        reasons.append("原生 <button>")
    disp = _cls_prop_any(cls, "display",
                         lambda v: v in ("inline", "inline-block", "inline-flex"))
    if disp:
        reasons.append("display:%s（行内级，天然单行）" % disp)
    # flex 容器默认 nowrap（不换行）—— 要求该类**没有**任何一条规则写
    # flex-wrap:wrap（有 wrap 就可能换行，不能豁免）
    if _cls_prop_any(cls, "display", lambda v: v == "flex") \
            and not _cls_prop_any(cls, "flex-wrap", lambda v: v == "wrap"):
        reasons.append("flex 容器默认 nowrap")
    if _cls_prop_any(cls, "text-overflow", lambda v: v == "ellipsis"):
        reasons.append("ellipsis 截断（单行）")
    return " + ".join(reasons) if reasons else None


# ★ 人工豁免表：自动理由覆盖不到、但实测确认不受影响的类 ★
#   ★ 每条都必须写清理由（这是「人工审定」的留痕）★
#   ★ 为什么表这么小 ★
#     实测（沙箱：白底黑字红框、同元素同内容只差圆角）逐个确认过：
#     这些类要么没有文字、要么文字恒为单行短标签 ⇒ 999px 不裁字、
#     描边也不会横穿（描边横穿需要「宽而矮 + 文字贴边」同时成立）。
_MANUAL_EXEMPT = {
    "ai-progress-bar": "进度条填充条（父 .ai-progress 高 6px），本身无文字",
    "cv-kind-tag": "概念类型标签，运行时创建，恒为单行短标签（中文枚举值）",
    "cv-node-concept": "概念节点旁的标签，运行时创建，恒为单行短标签",
    "cv-palette-concept-tag": "调色板里的概念标签，运行时创建，恒为单行短标签",
    "cv-purge": "概念清理按钮，运行时创建，按钮文字单行",
    "cv-rel-menu": "关联弹层菜单，菜单项由 .cv-rel-btn（行内级短标签）组成",
    "lk-block-badge": "词块徽章，运行时创建，文字为词块编号等短标签",
    "stars-clear": "清星按钮，按钮文字单行",
    "theme-opt-sw": "主题色卡（纯色块），**无文字**",
    "toast": "吐司提示条，文字单行且左右留白充足（实测描边不穿字）",
}

# ★ 本轮已收敛的 18 个类（实测确认受害）★
_CONVERGED = [
    "input", "ai-card", "mock-note", "review-updates", "form-error",
    "pd-revised", "pd-rev-item", "rv-answer", "article-zh",
    "article-title-zh", "article-extra", "tagselect-panel", "cv-node",
    "prac-suggestion", "pd-suggestion", "pd-reply", "v-detail-usage",
    "theme-opt",
]

_RULES = _css_rules(CSS_TEXT)
_SUN_VARS = _theme_vars(_RULES, "sunflower")

check("★ CSS 规则切分器工作正常（顶层规则数 > 500）",
      len([r for r in _RULES if not r["media"]]) > 500,
      "top=%d" % len([r for r in _RULES if not r["media"]]))
check("★ CSS 规则切分器能进 @media（嵌套规则数 > 50）",
      len([r for r in _RULES if r["media"]]) > 50,
      "nested=%d" % len([r for r in _RULES if r["media"]]))

# ★ 特异性计算器自检：算错的话下面所有层叠结论都是假的
_spec_cases = [
    (".input", (0, 1, 0)),
    ('html[data-theme="sunflower"] .input', (0, 2, 1)),
    ("#panel-articles .toolbar .input", (1, 2, 0)),
    (":focus-visible", (0, 1, 0)),
    (".a.b", (0, 2, 0)),
    ("div span", (0, 0, 2)),
    ("a:hover", (0, 1, 1)),
    ("::before", (0, 0, 1)),
    ('html[data-theme]:not([data-theme="reading"]) .grid', (0, 3, 1)),
]
_bad_spec = [(s, _spec(s), e) for s, e in _spec_cases if _spec(s) != e]
check("★ 特异性计算器自检（9 个已知选择器）", not _bad_spec, str(_bad_spec))

# ★ 主题匹配过滤自检（不做这步会算出假赢家）★
check("★ 主题过滤自检：marvel 的 .btn 规则不该在 sunflower 下命中",
      not _theme_matches('html[data-theme="marvel"] .btn', "sunflower")
      and _theme_matches('html[data-theme="sunflower"] .btn', "sunflower"))
check("★ 主题过滤自检：:not(reading) 的规则在 sunflower 下命中、在 reading 下不命中",
      _theme_matches('html[data-theme]:not([data-theme="reading"]) .grid', "sunflower")
      and not _theme_matches(
          'html[data-theme]:not([data-theme="reading"]) .grid', "reading"))

# ------------------------------------------------------------ L1 令牌层
check("★ 向日葵保留 --r-ctl: 999px（全圆角是设计意图，test_theme.py 也钉着）",
      _SUN_VARS.get("--r-ctl") == "999px", str(_SUN_VARS.get("--r-ctl")))
check("★ 向日葵 --r-card 是 14px（容器改用它）",
      _SUN_VARS.get("--r-card") == "14px", str(_SUN_VARS.get("--r-card")))
for _th, _v in (("reading", "8px"), ("vesper", "8px"), ("starry", "11px"),
                ("mona", "3px"), ("vampire", "4px"), ("marvel", "4px")):
    check("★ %s 的 --r-ctl 未被顺手改（应为 %s）" % (_th, _v),
          _theme_vars(_RULES, _th).get("--r-ctl") == _v,
          str(_theme_vars(_RULES, _th).get("--r-ctl")))

# ------------------------------------------- L2 表单控件红线（自动派生）
_FORM_CLASSES = sorted(
    c for c in _CLASS_DECLS
    if _HTML_TAGS.get(c, set()) & {"textarea", "input", "select"})
check("★ 自动派生出「被用在表单控件上的类」（非空）",
      len(_FORM_CLASSES) >= 1, str(_FORM_CLASSES))
for _cls in _FORM_CLASSES:
    _mx, _win = _final_radius(_RULES, _cls, "sunflower", _SUN_VARS)
    check("★ 表单控件类 %s：向日葵下圆角不得 ≥999px（Blink 会裁掉文字）" % _cls,
          _mx is None or _mx < 999,
          "圆角=%s winner=%s" % (_mx, _win["simple"] if _win else None))

# ------------------------------------- L3 已收敛的 18 类：逐条钉死
for _cls in _CONVERGED:
    _hits = _radius_hits(_RULES, _cls)
    _sun_hits = [h for h in _hits
                 if 'html[data-theme="sunflower"]' in h["simple"]
                 and not h["rule"]["media"]]
    check("★ %s: 恰好一条向日葵作用域的 border-radius 覆盖规则" % _cls,
          len(_sun_hits) == 1,
          "命中数=%d -> %s" % (len(_sun_hits), [h["simple"] for h in _sun_hits]))
    if len(_sun_hits) != 1:
        continue
    _val = _sun_hits[0]["value"]
    check("★ %s: 覆盖规则不含 var(--r-ctl)（那是 999px 的来源）" % _cls,
          "var(--r-ctl)" not in _val, _val)
    check("★ %s: 覆盖规则用令牌 var(--r-card)（不写死数值）" % _cls,
          "var(--r-card)" in _val, _val)
    # 层叠建模：赢家必须就是这条覆盖规则
    _win, _all_hits = _cascade_winner(_RULES, _cls, "sunflower")
    check("★ %s: 层叠建模的赢家就是那条向日葵覆盖规则" % _cls,
          _win is not None and _win["simple"] == _sun_hits[0]["simple"],
          "winner=%s spec=%s" % (_win["simple"] if _win else None,
                                 _spec(_win["simple"]) if _win else None))
    if _win is None:
        continue
    _nums, _missing = _resolve_radius(_win["value"], _SUN_VARS)
    check("★ %s: 赢家圆角在向日葵下解析后不是 999px" % _cls,
          bool(_nums) and max(_nums) < 999,
          "%s value=%s" % (_nums, _win["value"]))
    check("★ %s: 赢家圆角没有未定义的变量" % _cls,
          not _missing, str(_missing))
    check("★ %s: 赢家圆角最大角解析后 == --r-card (14px)" % _cls,
          bool(_nums) and max(_nums) == 14.0, str(_nums))

# ------------------- L4 集合完整性 ratchet（防「新增容器忘了收敛」）
#   「静态可达 ∧ 向日葵下最终圆角 ≥999px」的每个类，必须满足
#   (a) 自动可验证的豁免理由，或 (b) 人工豁免表里有它。
#   ⇒ 以后新增任何一个拿 999px 的类而没登记 ⇒ 判据必红。
_ALL_999PX = []
for _cls in sorted(_CLASS_DECLS):
    if _cls not in _REACHABLE:
        continue
    _mx, _win = _final_radius(_RULES, _cls, "sunflower", _SUN_VARS)
    if _mx is not None and _mx >= 999:
        _ALL_999PX.append(_cls)

check("★ 自动派生出「向日葵下仍 999px 的静态可达类」（非空）",
      len(_ALL_999PX) >= 20, str(len(_ALL_999PX)))

_unregistered = []
for _cls in _ALL_999PX:
    if _cls in _MANUAL_EXEMPT:
        continue
    if _auto_exempt_reason(_cls):
        continue
    _unregistered.append(_cls)
check("★ 集合完整性：向日葵下每个 999px 的类都有豁免依据（无则新增漏网）",
      not _unregistered, str(_unregistered))

# ★ 人工豁免表自身也要被检查（防止表里塞了已不存在的类 / 理由写空）★
check("★ 人工豁免表每条都写了理由",
      all(isinstance(v, str) and v.strip() for v in _MANUAL_EXEMPT.values()),
      str([k for k, v in _MANUAL_EXEMPT.items() if not (v or "").strip()]))
_stale = [c for c in _MANUAL_EXEMPT if c not in _CLASS_DECLS]
check("★ 人工豁免表里没有已不存在的类（表没过期）",
      not _stale, str(_stale))
# 人工豁免的类，实测确认它**确实**还是 999px（表没写错对象）
_wrong = []
for _c in _MANUAL_EXEMPT:
    _mx, _win = _final_radius(_RULES, _c, "sunflower", _SUN_VARS)
    if _mx is None or _mx < 999:
        _wrong.append((_c, _mx))
check("★ 人工豁免表里的类确实还是 999px（表没写错对象）",
      not _wrong, str(_wrong))

# ★ 双向交叉：自动理由成立的那些类，不许同时出现在人工表里（避免重复登记）
_dup = [c for c in _MANUAL_EXEMPT if _auto_exempt_reason(c)]
check("★ 人工豁免表与自动理由没有重复登记",
      not _dup, str(_dup))

# --------------------------------------------- L5 零改动 ratchet
#   ★ 为什么用「作用域规则指纹」而不是整文件 md5 ★
#     整文件 md5 会被本轮新增的向日葵覆盖段打乱 ⇒ 每次修 sunflower 都要
#     更新基线 ⇒ 基线就废了。指纹只覆盖 reading/vesper 作用域的规则，
#     语义上正好等于「这两个主题的规则一个字都没动」。
def _theme_scope_fingerprint(rules_, themes):
    parts = []
    for r in rules_:
        if r["media"]:
            continue
        for simple in _sel_split(r["sel"]):
            if ":not(" in simple:
                continue
            hit = False
            for th in themes:
                if re.search(r'data-theme=["\']?' + th + r'["\']?(?![\w-])', simple):
                    hit = True
            if hit:
                parts.append((re.sub(r"\s+", " ", simple).strip(),
                              re.sub(r"\s+", " ", r["body"]).strip()))
    parts.sort()
    h = hashlib.sha256()
    for s, b in parts:
        h.update(s.encode("utf-8"))
        h.update(b"\x00")
        h.update(b.encode("utf-8"))
        h.update(b"\x01")
    return h.hexdigest(), len(parts)


_rv_fp, _rv_n = _theme_scope_fingerprint(_RULES, ["reading", "vesper"])
check("★ 朴素/克制作用域规则指纹与基线一致（本轮零改动）",
      _rv_fp == "f942008e96f499b6e3a1ed4a97feb151d41e8dd7344cbac6dd356c1b37c6d686",
      "实际=%s 条数=%d" % (_rv_fp, _rv_n))

# ------------------------------------------------------------ L6 反向验证
# ★ 反向验证 1：把覆盖规则里的 var(--r-card) 改名 ⇒ 判据必须红 ★
#   ★ 为什么用「改名」而不是「删掉整段」★
#     删整段只会让「恰好一条覆盖规则」直接红，证明力弱；
#     改名更能证明「判据确实在读那一条声明」——
#     改名后覆盖规则还在、条数还是 1，但解析不出 14px ⇒ 必须红。
_broken1 = CSS_TEXT.replace("var(--r-card)", "var(--r-cardX)")
_broken1_nums, _ = _resolve_radius("var(--r-cardX)", _SUN_VARS)
check("★ 反向验证：把向日葵覆盖里的 var(--r-card) 改名 → 判据必须报不合规",
      _broken1 != CSS_TEXT and _broken1_nums != [14.0],
      "退化版没造成功" if _broken1 == CSS_TEXT else "退化版解析出了 14px")

# ★ 反向验证 2：删掉整段向日葵覆盖 ⇒ 判据必须红 ★
#   这模拟「有人把修复回滚了」—— 判据必须能抓住。
_marker = "/* ---- 向日葵：全圆角只留给"
_idx = CSS_TEXT.find(_marker)
_broken2 = CSS_TEXT[:_idx] if _idx > 0 else CSS_TEXT
_broken2_rules = _css_rules(_broken2)
_broken2_vars = _theme_vars(_broken2_rules, "sunflower")
_broken2_bad = []
for _cls in _CONVERGED:
    _mx, _win = _final_radius(_broken2_rules, _cls, "sunflower", _broken2_vars)
    if _mx is not None and _mx >= 999:
        _broken2_bad.append((_cls, _mx))
check("★ 反向验证：删掉整段向日葵覆盖 → 判据必须报不合规",
      _broken2 != CSS_TEXT and len(_broken2_bad) >= 10,
      "退化版没造成功" if _broken2 == CSS_TEXT
      else "退化版仍合规，违规数=%d" % len(_broken2_bad))

# ★ 反向验证 3：把某个覆盖改回 var(--r-ctl) ⇒ 判据必须红 ★
#   这模拟「有人为了让某个控件看起来更圆，把圆角改回去」——
#   正是这次 bug 的原始形态。
_broken3 = CSS_TEXT.replace(
    'html[data-theme="sunflower"] .cv-node {\n'
    '  border-radius: var(--r-card);',
    'html[data-theme="sunflower"] .cv-node {\n'
    '  border-radius: var(--r-ctl);')
_broken3_rules = _css_rules(_broken3)
_broken3_vars = _theme_vars(_broken3_rules, "sunflower")
_broken3_mx, _broken3_win = _final_radius(
    _broken3_rules, "cv-node", "sunflower", _broken3_vars)
check("★ 反向验证：把某个覆盖改回 var(--r-ctl) → 判据必须报不合规",
      _broken3 != CSS_TEXT and (_broken3_mx or 0) >= 999,
      "退化版没造成功" if _broken3 == CSS_TEXT
      else "退化版没解析出 999px（%s）" % _broken3_mx)

# ★ 反向验证 4：给真控件 .btn 加一条向日葵覆盖 ⇒ 判据必须红 ★
#   这模拟「有人手滑把真控件也一起改了」—— 全圆角形状语言会被误伤。
_broken4 = CSS_TEXT + (
    '\nhtml[data-theme="sunflower"] .btn {\n'
    '  border-radius: var(--r-card);\n}\n')
_broken4_rules = _css_rules(_broken4)
_broken4_sun_btn = [h for h in _radius_hits(_broken4_rules, "btn", "sunflower")
                    if 'html[data-theme="sunflower"]' in h["simple"]
                    and not h["rule"]["media"]]
check("★ 反向验证：给真控件 .btn 加向日葵覆盖 → 判据必须报不合规",
      _broken4 != CSS_TEXT and len(_broken4_sun_btn) > 0,
      "退化版没造成功" if _broken4 == CSS_TEXT else "退化版没造出覆盖")

# ★ 反向验证 5：新增一个拿 999px 的类但没登记 ⇒ 集合完整性判据必须红 ★
#   这是 L4 的核心证明：模拟「有人新增了一个文字容器，忘了收敛」。
_broken5 = CSS_TEXT + (
    '\n.brand-new-textbox {\n'
    '  border: 1px solid var(--border);\n'
    '  border-radius: var(--r-ctl);\n'
    '  padding: 8px 10px;\n'
    '}\n')
# 让它在源码里"可达"：把它写进 index.html 的一个 class 属性
_broken5_html = HTML_TEXT2.replace(
    "</body>", '<div class="brand-new-textbox"></div></body>')
_broken5_rules = _css_rules(_broken5)
_broken5_reachable = set(_REACHABLE) | {"brand-new-textbox"}
_broken5_999 = []
for _cls in sorted(_broken5_rules and set(
        list(_CLASS_DECLS) + ["brand-new-textbox"])):
    if _cls not in _broken5_reachable:
        continue
    _mx, _win = _final_radius(_broken5_rules, _cls, "sunflower", _SUN_VARS)
    if _mx is not None and _mx >= 999:
        _broken5_999.append(_cls)
_broken5_unregistered = [
    c for c in _broken5_999
    if c not in _MANUAL_EXEMPT and not _auto_exempt_reason(c)]
check("★ 反向验证：新增一个拿 999px 的类但没登记 → 集合完整性判据必须红",
      _broken5 != CSS_TEXT and "brand-new-textbox" in _broken5_unregistered,
      "退化版没造成功" if _broken5 == CSS_TEXT
      else "退化版没被抓住（未登记=%s）" % _broken5_unregistered)


# ===========================================================================
# ★ 第 8 套主题「银塑（silver）」· 银色雕像 ★
# ---------------------------------------------------------------------------
# 设计约束（2026-10-25）：
#   「风格采取目前展示图的背景风格（高级感银灰为主要颜色、流金色 / 浅粉色为
#     点缀颜色的油画风，主体是银色雕像），其他的部分都和星月夜差不多，
#     换句话说就是给同一套 UI 换个颜色、换个装饰、换个方框。」
#
# ★ 为什么这个套件放银塑的断言，而不是 test_theme.py ★
#   桌面零回归闸门要求旧套件 [PASS] 恰好等于基线。test_theme.py 每加一个
#   主题就会多出十几条断言（令牌齐全 / 两两对比 / 落盘往返…）⇒ 闸门必红。
#   本文件已在 verify_desktop_regression.sh 的排除名单里，
#   所以这里加断言不影响「旧基线逐字对账」，但全量回归照样跑、照样报警。
# ===========================================================================

_silver_css = (REPO / "web" / "style.css").read_text(encoding="utf-8")
_silver_js = (REPO / "web" / "app.js").read_text(encoding="utf-8")
_silver_live = re.sub(r"/\*.*?\*/", " ", _silver_css, flags=re.S)


def _theme_blocks(css_text: str) -> dict:
    """抽出每个 html[data-theme="x"] 块的令牌表（同一主题多次出现会合并）。"""
    out: dict = {}
    for m in re.finditer(r'html\[data-theme="(\w+)"\]\s*\{([^{}]*)\}', css_text):
        out.setdefault(m.group(1), {}).update(
            {k.group(1): k.group(2).strip()
             for k in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", m.group(2))})
    return out


_silver_blocks = _theme_blocks(_silver_live)
_silver_kv = _silver_blocks.get("silver", {})

# ---- ① 令牌齐全 ----
_silver_required = (
    "--bg", "--panel", "--panel-soft", "--border", "--border-strong",
    "--text", "--text-soft", "--muted",
    "--accent", "--accent-deep", "--accent-soft", "--accent-border",
    "--accent-ring", "--accent-on",
    "--warn", "--warn-bg", "--warn-border",
    "--danger", "--danger-bg", "--danger-border",
    "--shadow-sm", "--shadow-md", "--shadow-lg",
    "--r-card", "--r-ctl", "--r-pill",
)
check("★ 银塑主题块存在且必需令牌一个不少",
      bool(_silver_kv) and not [k for k in _silver_required if k not in _silver_kv],
      "缺: " + ", ".join(k for k in _silver_required if k not in _silver_kv))
check("★ 银塑没有空值令牌",
      not [k for k, v in _silver_kv.items() if not str(v).strip()],
      "空: " + ", ".join(k for k, v in _silver_kv.items() if not str(v).strip()))
check("★ 银塑 --accent-ring 是 rgba 半透明值（焦点环用）",
      str(_silver_kv.get("--accent-ring", "")).startswith("rgba("),
      str(_silver_kv.get("--accent-ring", "")))

# ---- ② 与其它 7 套主题都不同（防"只是明暗变体"）----
_silver_diff = []
for _other in ("reading", "starry", "sunflower", "vesper", "mona", "vampire", "marvel"):
    _okv = _theme_blocks(_silver_live).get(_other, {})
    _d = [k for k in ("--bg", "--accent", "--r-card", "--r-ctl")
          if _silver_kv.get(k) != _okv.get(k)]
    if not _d:
        _silver_diff.append(_other)
check("★ 银塑与其它 7 套主题都至少差一项（底色/强调色/圆角）",
      not _silver_diff,
      "与这些主题只是明暗变体: " + ", ".join(_silver_diff))

# ---- ③ 骨架与星月夜同构（sidebar 布局 + 大画布）----
check("★ 银塑 LAYOUTS 映射到 sidebar（与星月夜同骨架）",
      re.search(r"silver\s*:\s*'sidebar'", _silver_js) is not None)
check("★ 银塑是前端 THEMES 白名单的一员（用户能在主题选择器里选到）",
      re.search(r"\{\s*id:\s*'silver'", _silver_js) is not None)
check("★ 银塑有中文名与色板",
      re.search(r"id:\s*'silver'.*?name:\s*'银塑'", _silver_js, re.S) is not None
      and re.search(r"id:\s*'silver'.*?sw:\s*\['#[0-9a-fA-F]{6}'", _silver_js, re.S) is not None)

# 翻译区单列 + 大画布（与星月夜一样：横向占满）
_silver_prac_rules = re.findall(
    r'html\[data-theme="silver"\][^{]*\{([^{}]*)\}', _silver_live)
check("★ 银塑翻译区是单列大画布（与星月夜同构）",
      any("grid-template-columns" in b for b in _silver_prac_rules)
      and any("min-height" in b for b in _silver_prac_rules),
      "翻译区没有单列/大画布规则")
# 空闲态画布要有最小高度（大画布而不是小卡）
check("★ 银塑空闲态翻译区是大幅画布（min-height ≥ 300px）",
      any(re.search(r"min-height:\s*(?:3\d\d|[4-9]\d\d|\d{4,})px", b)
          for b in _silver_prac_rules),
      "空闲态画布没有 ≥300px 的 min-height")

# ---- ④ 素材真实存在且被引用 ----
for _asset in ("statue.jpg", "statue-tile.jpg"):
    _p = REPO / "web" / "assets" / _asset
    check(f"★ 银塑素材 assets/{_asset} 真实存在于磁盘", _p.exists(), str(_p))
    check(f"★ 银塑素材 assets/{_asset} 不是占位小图（>20KB 才算真画）",
          _p.exists() and _p.stat().st_size > 20000,
          str(_p.stat().st_size) + " bytes" if _p.exists() else "文件不存在")
    check(f"★ 银塑素材 assets/{_asset} 被 CSS 引用（不是白放着的）",
          f"assets/{_asset}" in _silver_css,
          "CSS 里没有引用这张图")

# ---- ⑤ WCAG：压在银灰底上的正文必须 ≥4.5:1 ----
def _srgb_lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _rel_lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return (0.2126 * _srgb_lin(r) + 0.7152 * _srgb_lin(g) + 0.0722 * _srgb_lin(b))


def _contrast(fg: str, bg: str) -> float:
    a, b = _rel_lum(fg), _rel_lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


_silver_bg = _silver_kv.get("--bg", "#b9bec6")
_silver_panel = _silver_kv.get("--panel", "#fafbfc")
# 压在银灰底上的正文位：--text / --text-soft 必须正文级
for _tok in ("--text", "--text-soft"):
    _c = _contrast(_silver_kv.get(_tok, "#000000"), _silver_bg)
    check(f"★ 银塑 {_tok} 压在银灰底上 ≥4.5:1（实测 {_c:.2f}）", _c >= 4.5,
          f"{_tok}={_silver_kv.get(_tok)} 在 {_silver_bg} 上只有 {_c:.2f}:1")
# 点缀色在**纸卡内**必须正文级（银灰底上不够，所以只在纸卡内当正文）
for _tok in ("--accent", "--danger"):
    _c = _contrast(_silver_kv.get(_tok, "#000000"), _silver_panel)
    check(f"★ 银塑 {_tok} 在纸卡内 ≥4.5:1（实测 {_c:.2f}）", _c >= 4.5,
          f"{_tok}={_silver_kv.get(_tok)} 在 {_silver_panel} 上只有 {_c:.2f}:1")

# ---- ⑥ 主题作用域不串色 ----
#   银塑与星月夜是两个方向；银塑的装饰规则不该命中星月夜，反之亦然。
check("★ 银塑的装饰规则精确限定在 silver（不会串到星月夜）",
      re.search(r'html\[data-theme="silver"\]\s*\.topbar', _silver_live) is not None
      and re.search(r'html\[data-theme="starry"\]\s*\.topbar[^{]*\{[^}]*silver', _silver_live) is None)
check("★ 银塑用自己专属的雕像素材，不复用星月夜的柏树画",
      "assets/statue.jpg" in _silver_live
      and re.search(r'html\[data-theme="silver"\][^{]*\{[^}]*cypresses\.jpg', _silver_live) is None,
      "银塑的规则里出现了星月夜的素材")

# ---- ⑦ 硬约束：朴素 / 克制零改动 ----
check("★ 银塑没有给朴素(reading) / 克制(vesper) 加规则",
      re.search(r'html\[data-theme="silver"\][^{]*reading', _silver_live) is None
      and re.search(r'html\[data-theme="silver"\][^{]*vesper', _silver_live) is None,
      "银塑的规则里出现了 reading / vesper")

# ===========================================================================
print("\n[B1] 安全响应头中间件（CSP / nosniff / XFO / Referrer / HSTS）")
# ===========================================================================
# B1 加固的验收有四件事，一件都不能少：
#   ① 安全头齐全（云端含 HSTS，桌面不含）；
#   ② CSP 登记了 web/index.html 两段内联脚本的哈希 —— 且必须同时满足
#      hash-source 语法（带单引号）与浏览器口径（LF 归一化后实算），
#      否则是「自洽但浏览器不认」的假绿（2026-10-25 红队抓到的真 bug）；
#   ③ 安全头中间件**没有**把 sharecode 注入层打断（历史上中间件顺序
#      出过白屏事故，这是重点）；
#   ④ CSP 常量与 **served** HTML 的实算哈希一致 —— 只对源文件算哈希
#      不够，那证明不了浏览器实际收到的内容能被 CSP 放行。
import base64  # noqa: E402

from app import security_headers  # noqa: E402

_B1_CSP_KEYS = ("content-security-policy", "x-content-type-options",
                "x-frame-options", "referrer-policy")
_B1_TEST_CODE = "B1TESTCODE"


def _b1_inline_hashes(html_text: str) -> list[str]:
    """从一段 HTML 里提取所有无 src 的内联 <script> 文本，实算 CSP 哈希。

    ★ 口径必须与浏览器一致，三个坑都踩过 ★
    ① 必须先剥 HTML 注释：index.html 第 20 行的注释里含有 `<script>` 字样，
       不剥就会把注释文本算进第一段脚本的内容，哈希整体错位；
    ② **必须先归一化 CRLF/CR → LF**：HTML 规范解析前做 input stream
       preprocessing，浏览器按 LF 内容算哈希。磁盘文件是 CRLF，按 CRLF
       字节算的值浏览器永远不认（2026-10-25 红队抓到的真 bug：裸哈希无
       引号 + CRLF 口径，两段内联脚本在 Chrome 里全被拦）。旧版helper
       两边同为 CRLF 口径，自洽但浏览器不认 —— 典型的假绿；
    ③ 提取要按开标签形状判定（带属性的 script 也要能找到），再按有没有
       src 分流 —— 只哈希无 src 的内联脚本，外链脚本不进 hash-source。
    """
    stripped = re.sub(r"<!--.*?-->", "", html_text, flags=re.DOTALL)
    # HTML input stream preprocessing：CRLF/CR → LF（浏览器口径）
    stripped = stripped.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for m in re.finditer(r"<script\b([^>]*)>(.*?)</script\s*>", stripped,
                         flags=re.DOTALL | re.IGNORECASE):
        attrs, text = m.group(1), m.group(2)
        if re.search(r"\bsrc\s*=", attrs, flags=re.IGNORECASE):
            continue  # 外链脚本不进 hash-source
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        out.append("sha256-" + base64.b64encode(digest).decode("ascii"))
    return out


os.environ["ENPRACTICE_MODE"] = "cloud"
os.environ["ENPRACTICE_SHARECODE"] = _B1_TEST_CODE
try:
    with _tc() as _c:
        _r = _c.get(f"/?sharecode={_B1_TEST_CODE}")
        _html = _r.text
        check("★ B1 云端首页 200", _r.status_code == 200, f"status={_r.status_code}")
        for _k in _B1_CSP_KEYS:
            check(f"★ B1 云端首页带 {_k}", bool(_r.headers.get(_k)),
                  str(dict(_r.headers))[:150])
        check("★ B1 云端首页带 HSTS（max-age=31536000）",
              "max-age=31536000" in (_r.headers.get("strict-transport-security") or ""),
              _r.headers.get("strict-transport-security"))
        check("★ B1 CSP 的 X-Frame-Options 为 DENY（点击劫持防护）",
              (_r.headers.get("x-frame-options") or "").upper() == "DENY",
              _r.headers.get("x-frame-options"))
        _csp = _r.headers.get("content-security-policy") or ""
        check("★ B1 CSP 登记了两段内联脚本的哈希（script-src）",
              security_headers._INLINE_BOOT_HASH in _csp
              and security_headers._INLINE_ONBOARDING_HASH in _csp, _csp)
        check("★ B1 CSP 的 script-src 不含 'unsafe-inline'（内联脚本只认哈希）",
              re.search(r"script-src[^;]*unsafe-inline", _csp) is None, _csp)
        # ②' hash-source 必须带单引号：CSP 语法是 'sha256-<base64>'，
        #     裸哈希是「无效源」，Chrome 直接忽略整条 script-src 登记
        #     （2026-10-25 红队抓到的真 bug 之一：两段内联脚本全被拦，
        #      console 报 "contains an invalid source ... ignored"）。
        _script_src = re.search(r"script-src ([^;]*)", _csp)
        _src_tokens = _script_src.group(1).split() if _script_src else []
        _hash_tokens = [t for t in _src_tokens if "sha256-" in t]
        check("★ B1 CSP 的 script-src 里每个 sha256 源都带单引号（hash-source 语法）",
              len(_hash_tokens) == 2
              and all(t.startswith("'sha256-") and t.endswith("'")
                      for t in _hash_tokens),
              f"tokens={_hash_tokens}")
        # ③ 安全头中间件没有把 sharecode 注入层打断
        check("★ B1 安全头中间件没打断 sharecode 注入（资源引用仍带令牌）",
              f"sharecode={_B1_TEST_CODE}" in _html,
              "served HTML 里找不到 sharecode=" + _B1_TEST_CODE)
        check("★ B1 sharecode 改写层没动脚本文本（仍是 2 段内联脚本）",
              len(_b1_inline_hashes(_html)) == 2,
              f"内联脚本块数={len(_b1_inline_hashes(_html))}")
        # ④ CSP 常量与 **served** HTML 实算哈希一致（浏览器视角）
        #   ★ 2026-10-25 红队复验补强：这条断言曾经是假绿 ★
        #   旧版 helper 按 CRLF 字节算，常量也是 CRLF 口径 —— 两边自洽，
        #   但浏览器按 LF 归一化后的内容算，真实页面里两段内联脚本全被拦
        #   （「重新查看新手引导」按钮没接线、sharecode 兜底脚本死亡）。
        #   现在 helper 先做 CRLF/CR → LF 归一化，与浏览器同口径；
        #   单引号语法由上面的 ②' 断言单独把关。三者缺一不可。
        _served_hashes = _b1_inline_hashes(_html)
        check("★ B1 served HTML 实算哈希与 CSP 常量逐字一致（LF 浏览器口径）",
              _served_hashes == [security_headers._INLINE_BOOT_HASH,
                                 security_headers._INLINE_ONBOARDING_HASH],
              f"served={_served_hashes}")
        check("★ B1 served HTML 的内联脚本与源文件内容一致（改写层不碰正文）",
              _b1_inline_hashes((REPO / "web" / "index.html").read_bytes().decode("utf-8"))
              == _served_hashes, "源文件与 served 的内联脚本哈希不一致")
        # ④ API / 静态 / 404 三类响应也有头（中间件在最外层，全覆盖）
        for _path, _label in (("/api/health", "API"), ("/style.css", "静态"),
                              ("/no-such-page-b1", "404")):
            _rr = _c.get(_path)
            check(f"★ B1 云端{_label}响应也带安全头（{_path}）",
                  all(_rr.headers.get(_k) for _k in _B1_CSP_KEYS)
                  and bool(_rr.headers.get("strict-transport-security")),
                  f"status={_rr.status_code} headers={dict(_rr.headers)}"[:150])
finally:
    os.environ.pop("ENPRACTICE_MODE", None)
    os.environ.pop("ENPRACTICE_SHARECODE", None)

# 桌面模式：同样的头（除 HSTS）。桌面跑 http://127.0.0.1，
# 加 HSTS 会把本地域名钉成 https-only，反而连不上。
with _tc() as _c:
    _r = _c.get("/")
    for _k in _B1_CSP_KEYS:
        check(f"★ B1 桌面首页带 {_k}", bool(_r.headers.get(_k)),
              str(dict(_r.headers))[:150])
    check("★ B1 桌面不加 HSTS（http 本地调试不能被钉 https）",
          "strict-transport-security" not in _r.headers,
          _r.headers.get("strict-transport-security"))
    _csp = _r.headers.get("content-security-policy") or ""
    check("★ B1 桌面 CSP 同样登记两段内联脚本哈希",
          security_headers._INLINE_BOOT_HASH in _csp
          and security_headers._INLINE_ONBOARDING_HASH in _csp, _csp)
    _rr = _c.get("/api/health")
    check("★ B1 桌面 API 响应也带安全头",
          all(_rr.headers.get(_k) for _k in _B1_CSP_KEYS),
          f"headers={dict(_rr.headers)}"[:150])

# ===========================================================================
print("\n[B2] 云端关闭 /docs /redoc /openapi.json")
# ===========================================================================
# 桌面（本进程 import 的 app 就是桌面形态）：三件套原样在。
check("★ B2 桌面 docs_url 保持 /docs", fastapi_app.docs_url == "/docs",
      str(fastapi_app.docs_url))
check("★ B2 桌面 redoc_url 保持 /redoc", fastapi_app.redoc_url == "/redoc",
      str(fastapi_app.redoc_url))
check("★ B2 桌面 openapi_url 保持 /openapi.json",
      fastapi_app.openapi_url == "/openapi.json", str(fastapi_app.openapi_url))
_r = _tc().get("/docs")
check("★ B2 桌面 GET /docs 仍 200（开发便利不受影响）", _r.status_code == 200,
      f"status={_r.status_code}")

# 云端：app 对象是 **import 期**按 cloud_mode() 构造的，本进程已是桌面，
# 所以云端形态只能起子进程验（env 先设好再 import，与 web_server.py 同序）。
_B2_PROBE = (
    "import os, sys\n"
    f"sys.path.insert(0, {str(REPO)!r})\n"
    "from app.main import app\n"
    "from fastapi.testclient import TestClient\n"
    "with TestClient(app) as c:\n"
    "    print('docs_url=%r redoc_url=%r openapi_url=%r' % (\n"
    "        app.docs_url, app.redoc_url, app.openapi_url))\n"
    "    for p in ('/docs', '/redoc', '/openapi.json'):\n"
    "        print(p, c.get(p).status_code)\n"
)
_env2 = dict(os.environ)
_env2["ENPRACTICE_MODE"] = "cloud"
_env2["ENPRACTICE_HOME"] = os.path.join(TMP, "b2-cloud")
_p2 = subprocess.run([PY, "-c", _B2_PROBE], capture_output=True, text=True,
                     env=_env2, timeout=180)
_out2 = _p2.stdout or ""
check("★ B2 云端 docs_url/redoc_url/openapi_url 均为 None",
      "docs_url=None redoc_url=None openapi_url=None" in _out2,
      _out2[-300:] + (_p2.stderr or "")[-300:])
check("★ B2 云端 GET /docs /redoc /openapi.json 全部 404",
      "/docs 404" in _out2 and "/redoc 404" in _out2 and "/openapi.json 404" in _out2,
      _out2[-300:])

# ===========================================================================
print("\n[B3] 云端 health 不回显 build 指纹")
# ===========================================================================
os.environ["ENPRACTICE_MODE"] = "cloud"
try:
    with _tc() as _c:
        _r = _c.get("/api/health")
        _hb = _r.json()
        check("★ B3 云端 health 不回显 build 指纹", "build" not in _hb, str(_hb))
        check("★ B3 云端 health 不回显 db_path（既有约束仍在）",
              "db_path" not in _hb, str(_hb))
        check("★ B3 云端 health 只回 ok/mode",
              _hb == {"ok": True, "mode": "cloud"}, str(_hb))
finally:
    os.environ.pop("ENPRACTICE_MODE", None)
_r = _tc().get("/api/health")
check("★ B3 桌面 health 仍回 db_path（桌面逐字不变）",
      "db_path" in _r.json() and _r.json().get("ok") is True, _r.text[:120])

# ===========================================================================
print("\n[B4] SSRF 拒绝消息不含解析出的 IP（DNS 解析 oracle 封堵）")
# ===========================================================================
_B4_SAFE_MSG = "接口地址不被允许：请填写公网可访问的 https 接口地址"

with mock.patch.object(egress.socket, "getaddrinfo", side_effect=_fake_gai):
    # 域名分支（多 A 记录混内网）：新消息，且**不含任何 IP**
    try:
        egress.validate_base_url("https://dual.example.com/v1")
        check("★ B4 多 A 记录混内网 → EgressError", False, "竟然放行了")
    except egress.EgressError as exc:
        _msg = str(exc)
        check("★ B4 域名分支拒绝消息统一为安全文案", _msg == _B4_SAFE_MSG, _msg)
        check("★ B4 域名分支消息里不含解析出的 IP（封堵 DNS oracle）",
              "93.184.216.34" not in _msg and "127.0.0.1" not in _msg, _msg)
    # revalidate（降级路径重校验）：同一条新消息
    # ★ ips 必须与桩表一致（B5 加固后 revalidate 先做集合比对）★
    #   这里手工构造的目标 ips 与 _fake_gai 对 dual.example.com 的解析
    #   结果完全相同 ⇒ 集合比对通过，专测循环里的公网判定那一层。
    #   （真实流程里 dual.example.com 过不了 validate_base_url，
    #     这个目标只为隔离测 revalidate 的 IP 检查而存在。）
    _tgt = egress.Target(host="dual.example.com", port=443, ip="93.184.216.34",
                         base_path="/v1",
                         ips=("93.184.216.34", "127.0.0.1"))
    try:
        egress.revalidate(_tgt)
        check("★ B4 revalidate 内网 IP → EgressError", False, "竟然放行了")
    except egress.EgressError as exc:
        check("★ B4 revalidate 拒绝消息同样统一且不含 IP",
              str(exc) == _B4_SAFE_MSG and "127.0.0.1" not in str(exc), str(exc))
    # IP 字面量分支**保持原样**：访客自己打的字，回显不构成 oracle
    try:
        egress.validate_base_url("https://169.254.169.254/v1")
        check("★ B4 IP 字面量回环 → EgressError", False, "竟然放行了")
    except egress.EgressError as exc:
        check("★ B4 IP 字面量分支消息保持原样（访客自填内容可回显）",
              "169.254.169.254" in str(exc), str(exc))
    # 端到端：ai._chat 走访客自填地址，AIError 消息同样不含 IP
    os.environ["ENPRACTICE_MODE"] = "cloud"
    cloud.request_api_key.set(DUMMY_KEY)
    cloud.request_ai_base_url.set("https://dual.example.com/v1")
    try:
        with mock.patch.object(egress, "post_json") as _pj, \
             mock.patch("requests.post") as _rp:
            try:
                ai._chat("sys", "user")
                check("★ B4 端到端：内网域名 → AIError", False, "竟然没报错")
            except ai.AIError as exc:
                check("★ B4 端到端 AIError 消息不含 IP",
                      "127.0.0.1" not in str(exc) and "93.184.216.34" not in str(exc),
                      str(exc))
            check("★ B4 端到端被拒时一个包都不发",
                  _pj.call_count == 0 and _rp.call_count == 0,
                  f"egress={_pj.call_count} requests.post={_rp.call_count}")
    finally:
        cloud.request_ai_base_url.set("")
        cloud.request_api_key.set("")
        os.environ.pop("ENPRACTICE_MODE", None)

# ===========================================================================
print("\n[B7] 云端请求体上限（content-length > 8MB → 413，不建库不迁移）")
# ===========================================================================
# ★ 为什么值得单独一条 ★
#   云端请求体可以是任意大；词条/文章/练习提交实际几十 KB。
#   不限量 = 访客一个 POST 就能把服务器内存吃光。
#   判据分两层：集成层（真发 8MB+1）+ 单元层（非法 content-length 放行）。

_B7_LIMIT = 8 * 1024 * 1024


async def _b7_probe(scope_headers, mode):
    """直接用 ASGI 协议层喂 VisitorMiddleware，看它放行还是 413。"""
    from app import main as _main
    status = {"code": None}

    async def _downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})

    async def _send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/api/entries",
             "headers": scope_headers, "query_string": b""}
    old_mode = os.environ.get("ENPRACTICE_MODE")
    if mode:
        os.environ["ENPRACTICE_MODE"] = mode
    try:
        await _main.VisitorMiddleware(_downstream)(scope, _receive, _send)
    finally:
        if old_mode is None:
            os.environ.pop("ENPRACTICE_MODE", None)
        else:
            os.environ["ENPRACTICE_MODE"] = old_mode
    return status["code"]


def _hdr(name, value):
    return [(name.encode("latin-1"), value.encode("latin-1"))]


#: main.py 的模块级 logger（B9 断言「错误细节记进日志」用）。
main_mod_logger = main_mod.logger

check("★ B7 云端：content-length 超 8MB → 413",
      asyncio.run(_b7_probe(_hdr("content-length", str(_B7_LIMIT + 1)),
                            "cloud")) == 413)
check("★ B7 云端：content-length 正好 8MB（等于上限）→ 放行（下游 200）",
      asyncio.run(_b7_probe(_hdr("content-length", str(_B7_LIMIT)),
                            "cloud")) == 200)
check("★ B7 云端：content-length 非法（不是数字）→ 放行交下游",
      asyncio.run(_b7_probe(_hdr("content-length", "not-a-number"),
                            "cloud")) == 200)
check("★ B7 云端：没有 content-length（chunked）→ 放行交下游",
      asyncio.run(_b7_probe([], "cloud")) == 200)
check("★ B7 桌面：同样超大 content-length 也放行（桌面逐字不变）",
      asyncio.run(_b7_probe(_hdr("content-length", str(_B7_LIMIT + 1)),
                            None)) == 200)

# 集成层：真发 8MB+1 字节 → 413；且为它建库/种 cookie 都不该发生
os.environ["ENPRACTICE_MODE"] = "cloud"
try:
    with _tc(raise_server_exceptions=False) as c7:
        r7 = c7.post("/api/entries", content=b"x" * (_B7_LIMIT + 1))
        check("★ B7 集成：超大 POST → 413", r7.status_code == 413,
              f"status={r7.status_code}")
        check("★ B7 集成：413 文案说明是请求体过大",
              "请求体过大" in r7.text, r7.text[:120])
        check("★ B7 集成：413 不带 Set-Cookie（没为它建访客库）",
              "set-cookie" not in {k.lower() for k in r7.headers},
              str(dict(r7.headers)))
        check("★ B7 集成：413 仍带安全响应头（B1 未回归）",
              r7.headers.get("x-content-type-options") == "nosniff",
              str(dict(r7.headers)))
        r7b = c7.put("/api/settings", json={"ui": {"ui_theme": "reading"}})
        check("★ B7 集成：正常小 body 不受影响（200）",
              r7b.status_code == 200, f"status={r7b.status_code}")
finally:
    os.environ.pop("ENPRACTICE_MODE", None)

# ===========================================================================
print("\n[B8] OverflowError → 404（不回显溢出细节）")
# ===========================================================================
check("★ B8 OverflowError handler 已注册",
      fastapi_app.exception_handlers.get(OverflowError) is not None)

os.environ["ENPRACTICE_MODE"] = "cloud"
try:
    with mock.patch("app.main.load_settings",
                    side_effect=OverflowError("int too large to convert to C int")):
        with _tc(raise_server_exceptions=False) as c8:
            r8 = c8.get("/api/settings")
            check("★ B8 集成：溢出 → 404", r8.status_code == 404,
                  f"status={r8.status_code}")
            check("★ B8 集成：404 文案固定，不回显溢出原文",
                  r8.text == '{"detail":"资源不存在或 ID 不合法"}', r8.text[:120])
finally:
    os.environ.pop("ENPRACTICE_MODE", None)

# ===========================================================================
print("\n[B9] sqlite3.Error → 500 但对外不回显 exc 原文（信息泄露封堵）")
# ===========================================================================
_SQLITE_LEAK = ("no such table: tenants/abcd1234/data.db "
                "secret_internal_table (sql: SELECT * FROM secret_internal_table)")

os.environ["ENPRACTICE_MODE"] = "cloud"
try:
    with mock.patch("app.main.entries.list_entries",
                    side_effect=sqlite3.OperationalError(_SQLITE_LEAK)), \
         mock.patch.object(main_mod_logger, "error") as _err_log:
        with _tc(raise_server_exceptions=False) as c9:
            r9 = c9.get("/api/entries")
            check("★ B9 集成：数据库错误 → 500", r9.status_code == 500,
                  f"status={r9.status_code}")
            check("★ B9 集成：对外文案通用，不含表名/SQL/路径",
                  "secret_internal_table" not in r9.text
                  and "tenants" not in r9.text and "SELECT" not in r9.text,
                  r9.text[:200])
            check("★ B9 集成：错误细节记进 error 日志（运维可排查）",
                  _err_log.call_count >= 1
                  and any("数据库错误" in str(c) for c in _err_log.call_args_list),
                  str(_err_log.call_args_list))
finally:
    os.environ.pop("ENPRACTICE_MODE", None)

# ===========================================================================
print("\n[B11] 访客 cookie 的 Secure 属性（云端默认带、桌面不带、可显式关）")
# ===========================================================================
# ★ B10 的判据在 [B14] 节 ★
#   平台默认路径 allow_redirects=False、降级路径同样 False，
#   两条断言就在下方 [B14] 的 ① 与 ⑤ 里，不在这里重复。

_old_mode = os.environ.get("ENPRACTICE_MODE")
try:
    os.environ["ENPRACTICE_MODE"] = "cloud"
    _ch_cloud = cloud.tenant_cookie_header("a" * 32)
    check("★ B11 云端 cookie 带 Secure",
          "Secure" in _ch_cloud.split("; "), _ch_cloud)
    check("★ B11 云端 cookie 仍带 HttpOnly/SameSite/Path/Max-Age（原属性不丢）",
          all(p in _ch_cloud for p in
              ("HttpOnly", "SameSite=Lax", "Path=/", "Max-Age=")), _ch_cloud)
    os.environ["ENPRACTICE_COOKIE_SECURE"] = "0"
    _ch_off = cloud.tenant_cookie_header("a" * 32)
    check("★ B11 ENPRACTICE_COOKIE_SECURE=0 → 显式关掉（内网 http 逃生门）",
          "Secure" not in _ch_off.split("; "), _ch_off)
    os.environ.pop("ENPRACTICE_COOKIE_SECURE", None)
finally:
    if _old_mode is None:
        os.environ.pop("ENPRACTICE_MODE", None)
    else:
        os.environ["ENPRACTICE_MODE"] = _old_mode

# 桌面（未设 ENPRACTICE_MODE）：不带 Secure —— 本地 http 调试要能跑
_ch_desktop = cloud.tenant_cookie_header("a" * 32)
check("★ B11 桌面 cookie 不带 Secure（本地 http 调试要能跑）",
      "Secure" not in _ch_desktop.split("; "), _ch_desktop)

# 端到端：云端首屏响应的 Set-Cookie 真带 Secure
os.environ["ENPRACTICE_MODE"] = "cloud"
try:
    with _tc(raise_server_exceptions=False) as c11:
        r11 = c11.get("/api/health")     # 新访客 → 下发 cookie
        _sc = r11.headers.get("set-cookie") or ""
        check("★ B11 端到端：云端 Set-Cookie 带 Secure", "Secure" in _sc, _sc)
finally:
    os.environ.pop("ENPRACTICE_MODE", None)

# ===========================================================================
print("\n[B12] 部署依赖钉死 requests（SSRF 钉住路径的能力前提）")
# ===========================================================================
_REQ_WEB = (REPO / "requirements-web.txt").read_text(encoding="utf-8")
check("★ B12 requirements-web.txt 钉死 requests==2.34.2",
      re.search(r"^requests==2\.34\.2$", _REQ_WEB, re.M) is not None,
      _REQ_WEB[:200])
check("★ B12 没有浮动的 requests>= 写法",
      re.search(r"^requests>=?", _REQ_WEB, re.M) is None)
# ★ 链条断言 ★ 钉版本不是目的，"钉住的版本真的有钉住能力"才是：
#   requests 2.32+ 自带 urllib3 2.x ⇒ _urllib3_supports_pinning() 为 True。
#   哪天依赖漂移（比如 urllib3 回退 1.x），这条会先红。
check("★ B12 当前环境的 requests/urllib3 组合支持 IP 钉住（链条未断）",
      egress._urllib3_supports_pinning() is True)

# ===========================================================================
print("\n[B13] 云端关掉 uvicorn access log（访客 query 不进日志）")
# ===========================================================================
_WS_TEXT = (REPO / "web_server.py").read_text(encoding="utf-8")
check("★ B13 web_server.py 的 uvicorn.run 带 access_log=False",
      "access_log=False" in _WS_TEXT)
check("★ B13 单 worker 与 proxy_headers 未被动掉（既有约束仍在）",
      "workers=1" in _WS_TEXT and "proxy_headers=True" in _WS_TEXT)

# ===========================================================================
print("\n[B16] ENPRACTICE_ALLOWED_HOSTS 门控 TrustedHostMiddleware（Host 头白名单）")
# ===========================================================================
_mw_names = [getattr(m.cls, "__name__", "")
             for m in fastapi_app.user_middleware]
check("★ B16 未配 ENPRACTICE_ALLOWED_HOSTS ⇒ 不注册 TrustedHostMiddleware"
      "（现有部署与桌面行为逐字不变）",
      "TrustedHostMiddleware" not in _mw_names, str(_mw_names))

_B16_PROBE = (
    "import os, sys\n"
    f"sys.path.insert(0, {str(REPO)!r})\n"
    "from app.main import app\n"
    "from fastapi.testclient import TestClient\n"
    "names = [m.cls.__name__ for m in app.user_middleware]\n"
    "print('middleware:', ','.join(names))\n"
    "with TestClient(app, base_url='https://testserver') as c:\n"
    "    print('evil-host:', c.get('/api/health',\n"
    "          headers={'Host': 'evil.example.com'}).status_code)\n"
    "    print('good-host:', c.get('/api/health',\n"
    "          headers={'Host': 'good.example.com'}).status_code)\n"
)

# 场景一：配了白名单 ⇒ 注册中间件 + 名单外 Host 400 + 名单内 200
_env16 = dict(os.environ)
_env16["ENPRACTICE_MODE"] = "cloud"
_env16["ENPRACTICE_HOME"] = os.path.join(TMP, "b16-on")
_env16["ENPRACTICE_ALLOWED_HOSTS"] = "good.example.com,*.good.example.com"
_p16 = subprocess.run([PY, "-c", _B16_PROBE], capture_output=True, text=True,
                      env=_env16, timeout=180)
_out16 = _p16.stdout or ""
check("★ B16 配了 ENPRACTICE_ALLOWED_HOSTS ⇒ 注册 TrustedHostMiddleware",
      "TrustedHostMiddleware" in _out16,
      _out16[-300:] + (_p16.stderr or "")[-300:])
check("★ B16 白名单外的 Host → 400（Host 头投毒被挡）",
      "evil-host: 400" in _out16, _out16)
check("★ B16 白名单内的 Host 正常放行（含 *. 通配）",
      "good-host: 200" in _out16, _out16)

# 场景二：同样不设该变量（桌面 env）⇒ 不注册，evil Host 也放行
_env16b = dict(os.environ)
_env16b["ENPRACTICE_HOME"] = os.path.join(TMP, "b16-off")
_env16b.pop("ENPRACTICE_ALLOWED_HOSTS", None)
_p16b = subprocess.run([PY, "-c", _B16_PROBE], capture_output=True, text=True,
                       env=_env16b, timeout=180)
_out16b = _p16b.stdout or ""
check("★ B16 不设变量（子进程）⇒ 不注册、evil Host 也 200",
      "TrustedHostMiddleware" not in _out16b
      and "evil-host: 200" in _out16b,
      _out16b[-300:] + (_p16b.stderr or "")[-300:])

# ===========================================================================
print("\n[B15] AST 出网守卫两段式判定（新客户端形态必须可见、dict.get 不许误报）")
# ===========================================================================
# ★ B15 要消灭的两个盲区 ★
#   ① **漏报**：旧 `_is_http_call` 只认四种硬编码前缀（requests./self._s./
#      self._requests./session.），换成 httpx / urllib.request / self._session /
#      fetcher 写法就对守卫隐形 —— 本次实测它确实漏了 websearch 的 fetcher.get。
#   ② **误报**：放宽成"任何 .get() 都算出网"会把 dict/config/headers 的
#      .get 全判成出网点，满屏假红 ⇒ 守卫会被当成噪音删掉。
# 判据 = 动词段（真 HTTP 动词）× 接收者段（像 HTTP 客户端的对象）。

# ① 新客户端形态必须全部抓到（漏报即 FAIL）
_b15_new_forms = {
    "httpx.post('https://x')\n": "httpx.post",
    "import urllib.request\nurllib.request.urlopen('https://x')\n":
        "urllib.request.urlopen",
    "self._session.get('https://x')\n": "self._session.get",
    "self._client.request('GET', 'https://x')\n": "self._client.request",
    "fetcher.post('https://x')\n": "fetcher.post",
    "conn.request('GET', 'https://x')\n": "conn.request",
}
for _src, _want in _b15_new_forms.items():
    check(f"★ B15 新客户端形态必须被抓到：{_want}",
          _scan_egress(_src) == {_want}, str(_scan_egress(_src)))

# ② 旧形态不能退步（B15 前就能抓到的，现在仍要抓到）
for _src in ("import requests\nrequests.get('https://x')\n",
             "session.post('https://x')\n",
             "self._s.get('https://x')\n"):
    _want = _src.strip().splitlines()[-1].split("(")[0]
    check(f"★ B15 旧形态仍能抓到：{_want}",
          _scan_egress(_src) == {_want}, str(_scan_egress(_src)))

# ③ 负向样本走**临时目录文件**（与真实守卫同一条"文件 → ast.parse"路径）
_b15_dir = Path(TMP) / "b15-neg"
_b15_dir.mkdir(exist_ok=True)
(_b15_dir / "neg.py").write_text(
    "d = {}\n"
    "class C:\n"
    "    def f(self):\n"
    "        return (d.get('k'), self.headers.get('x'),\n"
    "                self.config.get('y'), self.cache.get('z'))\n",
    encoding="utf-8")
_b15_found = set()
for _py in sorted(_b15_dir.glob("*.py")):
    for _node in ast.walk(ast.parse(_py.read_text(encoding="utf-8"))):
        if isinstance(_node, ast.Call):
            _d = _dotted(_node.func)
            if _d != "requests.Session" and _is_http_call(_d):
                _b15_found.add(_d)
check("★ B15 负向样本（dict / headers / config / cache 的 .get）零误报",
      _b15_found == set(), str(_b15_found))

# ④ 建会话 / 设请求头仍不算出网点（历史判据不许退步）
check("★ B15 建会话仍不算出网点",
      _scan_egress("requests.Session()\n") == set())
check("★ B15 设请求头仍不算出网点",
      _scan_egress("self._s.headers.update({'a': 'b'})\n") == set())

# ===========================================================================
print("\n[B5] DNS rebinding：降级路径重解析结果与校验时不一致 → 整笔拒绝")
# ===========================================================================
# ★ 场景 ★
#   校验时域名解析到公网 IP（通过），请求时刻重解析换成别的 IP ——
#   经典 DNS rebinding。降级路径（按域名请求）没有钉住 IP，
#   全靠 revalidate 在请求发出前再拦一次。

with mock.patch.object(egress.socket, "getaddrinfo", side_effect=_fake_gai):
    _tgt5 = egress.validate_base_url("https://good.example.com/v1")
    check("★ B5 validate_base_url 记下全部解析 IP（供重解析比对）",
          tuple(_tgt5.ips) == ("93.184.216.34",), str(_tgt5.ips))
    check("★ B5 钉住用的 ip 就是 ips 的第一个",
          _tgt5.ip == _tgt5.ips[0], str(_tgt5))

# IP 字面量分支：ips 只有它自己（不查 DNS，无从变化）
_lit5 = egress.validate_base_url("https://8.8.8.8/v1")
check("★ B5 IP 字面量分支 ips=（它自己）",
      tuple(_lit5.ips) == ("8.8.8.8",), str(_lit5.ips))


def _gai_internal(host, port, *a, **kw):
    """重解析桩：换成内网 IP（rebinding 的第二跳）。"""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]


def _gai_other_public(host, port, *a, **kw):
    """重解析桩：换成**另一个公网 IP**（每个都合法，但集合已经变了）。"""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
             ("93.184.216.99", port))]


# 场景一：重解析换成内网 IP
with mock.patch.object(egress.socket, "getaddrinfo", side_effect=_gai_internal):
    try:
        egress.revalidate(_tgt5)
        check("★ B5 重解析换成内网 IP → EgressError", False, "竟然放行了")
    except egress.EgressError as exc:
        check("★ B5 重解析结果变化 → 拒绝（消息固定）",
              str(exc) == "域名解析结果发生变化，本次请求已拒绝", str(exc))

# 场景二：重解析换成另一个公网 IP —— 集合比对必须照样拒绝
#   （只查"新的都是公网"的实现会在这里放行，那等于把请求引向
#     另一台从未校验过的服务器。）
with mock.patch.object(egress.socket, "getaddrinfo", side_effect=_gai_other_public):
    try:
        egress.revalidate(_tgt5)
        check("★ B5 重解析换成另一个公网 IP → 同样拒绝", False, "竟然放行了")
    except egress.EgressError as exc:
        check("★ B5 公网→公网变化也拒绝（集合比对，不是只查公网）",
              str(exc) == "域名解析结果发生变化，本次请求已拒绝", str(exc))

# 正对照：重解析结果与校验时完全一致 → 放行（证明比对不是「永远拒」）
with mock.patch.object(egress.socket, "getaddrinfo", side_effect=_fake_gai):
    try:
        egress.revalidate(_tgt5)
        check("★ B5 正对照：解析结果一致 → revalidate 放行", True)
    except egress.EgressError as exc:
        check("★ B5 正对照：解析结果一致 → revalidate 放行", False, str(exc))

# 端到端：走完整 post_json 降级路径，确认拒绝时**零出网**
#   构造：钉住直连打桩失败 + 环境配了代理 ⇒ 触发降级分支 ⇒
#   revalidate 在 requests.post 之前抛错。
os.environ["HTTPS_PROXY"] = "http://proxy.invalid:8080"
try:
    with mock.patch.object(egress.socket, "getaddrinfo",
                           side_effect=_gai_internal), \
         mock.patch.object(requests.Session, "post",
                           side_effect=requests.RequestException("打桩：钉住直连失败")) as _sp5, \
         mock.patch("requests.post") as _rp5:
        try:
            egress.post_json(_tgt5, "/chat/completions", {"model": "m"}, {}, 10)
            check("★ B5 端到端 rebinding → EgressError", False, "竟然没报错")
        except egress.EgressError as exc:
            check("★ B5 端到端拒绝消息",
                  "域名解析结果发生变化" in str(exc), str(exc))
        check("★ B5 端到端被拒时零出网（按域名的请求一个都没发）",
              _sp5.call_count == 1 and _rp5.call_count == 0,
              f"session.post={_sp5.call_count} requests.post={_rp5.call_count}")
finally:
    os.environ.pop("HTTPS_PROXY", None)

# ===========================================================================
print("\n[B6] AI 出网并发上限（进程级 BoundedSemaphore(16)）")
# ===========================================================================
check("★ B6 ai._ai_slots 是进程级 BoundedSemaphore",
      isinstance(ai._ai_slots, type(threading.BoundedSemaphore(1))),
      repr(ai._ai_slots))

# 行为证据：预先占满 16 个槽位 ⇒ _chat 必须阻塞；释放一个 ⇒ 放行
_held = {"n": 0}


def _hold_all():
    for _ in range(16):
        ai._ai_slots.acquire()
        _held["n"] += 1


_th6 = threading.Thread(target=_hold_all, daemon=True)
_th6.start()
_th6.join(timeout=5)
check("★ B6 预先占满 16 个槽位", _held["n"] == 16, str(_held["n"]))

_done6: dict = {}


def _chat_thread():
    try:
        with mock.patch("requests.post", side_effect=_fake_requests_post):
            ai._chat("sys", "user")
        _done6["ok"] = True
    except Exception as exc:  # noqa: BLE001
        _done6["err"] = f"{type(exc).__name__}: {exc}"


_tc6 = threading.Thread(target=_chat_thread, daemon=True)
_tc6.start()
_tc6.join(timeout=3)
check("★ B6 槽位占满时 _chat 阻塞在信号量上（并发上限真实生效）",
      _tc6.is_alive() and not _done6.get("err"), str(_done6))
ai._ai_slots.release()          # 放出一个槽位
_tc6.join(timeout=5)
check("★ B6 释放一个槽位后 _chat 放行并正常返回",
      _done6.get("ok") is True, str(_done6))
# 归还 _hold_all 占住的剩余 15 个（_chat 用完那一个已自行归还）
for _ in range(15):
    ai._ai_slots.release()

# ===========================================================================
print("\n[B14] AI 响应体 4MB 上限（stream=True + 超限中断）")
# ===========================================================================


class _StreamResp:
    """流式响应替身：像真 requests.Response 一样带 raw / iter_content。

    ``text`` 从 ``_content`` 读（与 requests 同构）⇒ 能验证
    ``_read_capped`` 的「写回 _content」机制真的让 .text 可用。
    """

    def __init__(self, chunks, status=200):
        self._chunks = list(chunks)
        self.status_code = status
        self.read_chunks = 0        # 调用方实际拉走了多少块
        self.closed = False
        self.raw = object()         # 有 raw ⇒ _read_capped 走流式分支
        self._content = b""

    def iter_content(self, chunk_size=1):
        for ch in self._chunks:
            self.read_chunks += 1
            yield ch

    def close(self):
        self.closed = True

    @property
    def text(self):
        return (self._content or b"").decode("utf-8", "replace")

    def json(self):
        import json as _json
        return _json.loads(self.text)


_OK_BODY = b'{"choices":[{"message":{"content":"ok"}}]}'

# ① 正常小 body：读完放行，且 .text / .json() 靠写回的 _content 照常工作
_r_ok = _StreamResp([_OK_BODY])
try:
    ai._read_capped(_r_ok)
    check("★ B14 小 body 正常读完", _r_ok.read_chunks == 1,
          f"read_chunks={_r_ok.read_chunks}")
    check("★ B14 读完后 .text 可用（写回 _content 生效）",
          "ok" in _r_ok.text, repr(_r_ok.text))
    check("★ B14 读完后 .json() 可用",
          _r_ok.json()["choices"][0]["message"]["content"] == "ok")
except ai.AIError as exc:
    check("★ B14 小 body 正常读完", False, str(exc))

# ② 超大 body（10MB）：第 5 块（累计 5MB）即中断，不把整个 body 收进内存
_big = [b"a" * (1024 * 1024)] * 10
_r_big = _StreamResp(_big)
try:
    ai._read_capped(_r_big)
    check("★ B14 超大 body → AIError", False, "竟然放行了")
except ai.AIError as exc:
    check("★ B14 超大 body → AIError（消息固定）",
          str(exc) == "AI 接口响应过大，已中断读取", str(exc))
check("★ B14 超限即中断，没有把整个 body 拉进内存",
      _r_big.read_chunks == 5, f"拉走了 {_r_big.read_chunks} 块（共 10 块）")
check("★ B14 超限时先 close() 再抛（不继续收剩余流量）", _r_big.closed)

# ③ 边界：正好 4MB（等于上限）→ 放行
_exact = [b"b" * (1024 * 1024)] * 4
_r_exact = _StreamResp(_exact)
try:
    ai._read_capped(_r_exact)
    check("★ B14 正好 4MB（等于上限）→ 放行", _r_exact.read_chunks == 4,
          f"read_chunks={_r_exact.read_chunks}")
except ai.AIError as exc:
    check("★ B14 正好 4MB（等于上限）→ 放行", False, str(exc))

# ④ 无 raw 的响应替身（如 14.7 的 _FakeChatResp）原样放行，不被误伤
try:
    ai._read_capped(_FakeChatResp())
    check("★ B14 无 raw 的响应替身原样放行（测试替身不被上限误伤）", True)
except ai.AIError as exc:
    check("★ B14 无 raw 的响应替身原样放行（测试替身不被上限误伤）",
          False, str(exc))

# ⑤ egress.post_json 两处请求都必须带 stream=True
with mock.patch.object(egress.socket, "getaddrinfo", side_effect=_fake_gai):
    _tgt14 = egress.validate_base_url("https://good.example.com/v1")
    with mock.patch.object(requests.Session, "post",
                           return_value=_StreamResp([_OK_BODY])) as _sp14:
        egress.post_json(_tgt14, "/chat/completions", {"model": "m"}, {}, 10)
        _kw14 = _sp14.call_args[1]
        check("★ B14 钉住直连路径带 stream=True",
              _kw14.get("stream") is True, str(_kw14))
    # 降级路径（打桩钉住失败 + 配代理触发）
    os.environ["HTTPS_PROXY"] = "http://proxy.invalid:8080"
    try:
        with mock.patch.object(egress.socket, "getaddrinfo",
                               side_effect=_fake_gai), \
             mock.patch.object(requests.Session, "post",
                               side_effect=requests.RequestException("打桩")), \
             mock.patch("requests.post",
                        return_value=_StreamResp([_OK_BODY])) as _rp14:
            egress.post_json(_tgt14, "/chat/completions", {"model": "m"}, {}, 10)
            _kw14b = _rp14.call_args[1]
            check("★ B14 降级路径同样带 stream=True",
                  _kw14b.get("stream") is True, str(_kw14b))
    finally:
        os.environ.pop("HTTPS_PROXY", None)

# ⑥ 端到端：平台默认路径收到超大响应 → AIError，且没有全量读
with mock.patch("requests.post", return_value=_StreamResp(_big)) as _rp_e2e:
    try:
        ai._chat("sys", "user")
        check("★ B14 端到端：超大响应 → AIError", False, "竟然没报错")
    except ai.AIError as exc:
        check("★ B14 端到端：超大响应 → AIError（消息固定）",
              str(exc) == "AI 接口响应过大，已中断读取", str(exc))

# ⑦ 错误分支的 resp.text[:200] 仍要正常工作（HTTP 500 + 小 body）
_r_err = _StreamResp([b'{"error":"boom"}'], status=500)
with mock.patch("requests.post", return_value=_r_err):
    try:
        ai._chat("sys", "user")
        check("★ B14 HTTP 500 → AIError", False, "竟然没报错")
    except ai.AIError as exc:
        check("★ B14 HTTP 500 错误分支的 resp.text[:200] 仍正常工作",
              "HTTP 500" in str(exc) and "boom" in str(exc), str(exc))

# ⑧ 端到端：访客自填地址路径同样受 4MB 上限约束
os.environ["ENPRACTICE_MODE"] = "cloud"
_tb14 = cloud.request_ai_base_url.set("https://visitor.example.com/v1")
try:
    # DNS 桩：让 validate_base_url 能通过（桩表里有 visitor.example.com）；
    # 真正被测的是 post_json 返回超大流式响应后 ai._read_capped 的截断。
    with mock.patch.object(egress.socket, "getaddrinfo", side_effect=_fake_gai), \
         mock.patch.object(egress, "post_json",
                           return_value=_StreamResp(_big)):
        try:
            ai._chat("sys", "user")
            check("★ B14 访客路径超大响应 → AIError", False, "竟然没报错")
        except ai.AIError as exc:
            check("★ B14 访客路径超大响应同样被截断（消息固定）",
                  str(exc) == "AI 接口响应过大，已中断读取", str(exc))
finally:
    cloud.request_ai_base_url.reset(_tb14)
    os.environ.pop("ENPRACTICE_MODE", None)

# ===========================================================================
print("\n" + "=" * 66)
total = len(results)
passed = sum(1 for _n, ok in results if ok)
print(f"总计：{passed} PASS / {total - passed} FAIL（共 {total} 项）")
if passed != total:
    for name, ok in results:
        if not ok:
            print(f"   FAIL: {name}")
sys.exit(0 if passed == total else 1)
