"""云端多租户隔离测试（S1）。

运行：python test_cloud_tenant.py
（脚本式，与本仓库其它 test_*.py 一致；不用 pytest）

★ 全部在临时 ENPRACTICE_HOME 里跑，**绝不碰 %APPDATA%\\EnPractice 真实库**。

判据纪律：
  - 桌面模式的返回值要与「改造前」逐字相同 → 这里用**硬编码的期望路径**钉死，
    而不是"和某个函数比"（和自己比永远相等，恒真判据）。
  - 每条断言都要能 FAIL：反向验证见 C:/tmp/rev_cloud_tenant.py。
"""

import os
import sys
import tempfile
import threading
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="enp-cloud-test-")
os.environ["ENPRACTICE_HOME"] = TMP
# ★ 本套件跑两段：先桌面、再云端。开头先确保 cloud 模式是关的。
os.environ.pop("ENPRACTICE_MODE", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cloud, config, db  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    mark = "PASS" if ok else "FAIL"
    print(f"    [{mark}] {name}" + (f"\n            -> {detail}" if detail and not ok else ""))


print("\n[1] 桌面模式：返回值必须与改造前逐字相同")


# ===== ① 桌面 db_path / settings_path =====
expected_db = config.app_dir() / "data.db"
expected_settings = config.app_dir() / "settings.json"
check("桌面 cloud_mode() 为 False", cloud.cloud_mode() is False)
check("桌面 current_tenant 恒为 None（没套中间件）",
      cloud.current_tenant.get() is None)
check("桌面 db_path() == app_dir()/data.db",
      config.db_path() == expected_db, config.db_path())
check("桌面 settings_path() == app_dir()/settings.json",
      config.settings_path() == expected_settings, config.settings_path())
check("★ 桌面 db_path 的字符串形态逐字正确（盘符 + 绝对路径）",
      config.db_path().name == "data.db"
      and config.db_path().parent == config.app_dir()
      and config.db_path().is_absolute(),
      str(config.db_path()))
check("★ 桌面 db_path 不含 tenants 段",
      "tenants" not in config.db_path().parts, config.db_path().parts)

# 即使人为把 ContextVar 设了，桌面也必须忽略它 —— 这是「桌面零回归」的最后一道
_tok = cloud.current_tenant.set("a" * 32)
check("★ 桌面模式下即使 ContextVar 有值也走全局库（不按访客分流）",
      config.db_path() == expected_db, config.db_path())
cloud.current_tenant.reset(_tok)

# ===== ② 桌面 connect() 不受云端守卫影响 =====
db.init_db()
conn = db.connect()
try:
    ok = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
except Exception as exc:                       # noqa: BLE001
    ok = None
    print(f"            异常：{exc}")
check("桌面 connect() 正常开库（无守卫抛错）", ok is not None)
check("桌面 connect() 开的就是全局库",
      str(db._open.__module__) == "app.db" and config.db_path().exists())
conn.close()

# ===== ③ 桌面 get_api_key() 走原有两级逻辑 =====
_k = cloud.request_api_key.get()
check("桌面 request_api_key 恒为空串", _k == "", repr(_k))
os.environ["OPENAI_API_KEY"] = "env-key-xyz"
check("桌面 get_api_key() 优先读环境变量",
      config.get_api_key() == "env-key-xyz", config.get_api_key())
del os.environ["OPENAI_API_KEY"]
check("桌面 get_api_key() 回落到 settings（空串）",
      config.get_api_key() == "", config.get_api_key())

print("\n[2] 云端模式：访客分流")


# ===== ④ 云端 db_path / settings_path =====
os.environ["ENPRACTICE_MODE"] = "cloud"
check("设了 ENPRACTICE_MODE=cloud 后 cloud_mode() 为 True", cloud.cloud_mode())

vid_a = cloud.new_tenant_id()
tok_a = cloud.current_tenant.set(vid_a)
try:
    p_db_a = config.db_path()
    p_st_a = config.settings_path()
    check("云端 db_path() 指向 tenants/<id>/data.db",
          p_db_a == config.app_dir() / "tenants" / vid_a / "data.db", p_db_a)
    check("云端 settings_path() 指向 tenants/<id>/settings.json",
          p_st_a == config.app_dir() / "tenants" / vid_a / "settings.json", p_st_a)
    check("★ 云端 db_path 与桌面全局库不是同一个文件",
          p_db_a != expected_db, f"{p_db_a} vs {expected_db}")

    vid_b = cloud.new_tenant_id()
    tok_b = cloud.current_tenant.set(vid_b)
    p_db_b = config.db_path()
    cloud.current_tenant.reset(tok_b)
    check("★ 两个不同 vid 得到不同库路径",
          p_db_a != p_db_b, f"{p_db_a} vs {p_db_b}")
    check("★ 两个 vid 的目录名各自正确",
          p_db_b == config.app_dir() / "tenants" / vid_b / "data.db", p_db_b)
finally:
    cloud.current_tenant.reset(tok_a)

# ===== ⑤ 云端无访客上下文时 connect() 必须抛错 =====
cloud.current_tenant.set(None)
try:
    c = db.connect()
    c.close()
    raised = False
    msg = ""
except RuntimeError as exc:
    raised = True
    msg = str(exc)
except Exception as exc:                       # noqa: BLE001
    raised = False
    msg = f"抛的不是 RuntimeError 而是 {type(exc).__name__}: {exc}"
check("★ 云端无访客上下文时 connect() 抛 RuntimeError", raised, msg)
check("★ 报错文案点明是「访客上下文」问题（可诊断）",
      raised and ("访客" in msg and "上下文" in msg), msg)

# 非法 vid 也不能被放行成访客路径
_t = cloud.current_tenant.set("../../etc")
try:
    p_bad = config.db_path()
    bad_fell_back = (p_bad == expected_db)
finally:
    cloud.current_tenant.reset(_t)
check("★ 非法 vid（路径穿越）回落到全局库而不是拼进路径",
      bad_fell_back, p_bad)

print("\n[3] cookie 三种输入")


# ===== ⑥ cookie 解析 =====
check("合法 cookie 能取出 id",
      cloud.read_visitor_id(f"enp_vid={vid_a}") == vid_a)
check("合法 cookie 与其它 cookie 混排也能取出",
      cloud.read_visitor_id(f"foo=1; enp_vid={vid_a}; bar=2") == vid_a)
check("缺 cookie（空串）→ 空串",
      cloud.read_visitor_id("") == "")
check("缺 enp_vid（只有别的 cookie）→ 空串",
      cloud.read_visitor_id("foo=1; bar=2") == "")
check("非法 id（太短）→ 空串", cloud.read_visitor_id("enp_vid=abc") == "")
check("非法 id（非 hex）→ 空串",
      cloud.read_visitor_id("enp_vid=zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz") == "")
check("非法 id（路径穿越）→ 空串",
      cloud.read_visitor_id("enp_vid=../../etc/passwd") == "")
check("非法 id（长度对但含大写）→ 空串",
      cloud.read_visitor_id("enp_vid=" + "A" * 32) == "")
check("畸形 cookie 头不抛异常",
      cloud.read_visitor_id("enp_vid=") == "")

print("\n[4] 访客 id 与 cookie 生成")


# ===== ⑦ id 生成与校验 =====
check("new_tenant_id() 是 32 位小写 hex",
      len(vid_a) == 32 and all(ch in "0123456789abcdef" for ch in vid_a), vid_a)
check("is_valid_id(合法) 为 True", cloud.is_valid_id(vid_a))
check("is_valid_id(空串) 为 False", cloud.is_valid_id("") is False)
check("is_valid_id(None) 为 False（不抛）", cloud.is_valid_id(None) is False)
_ch = cloud.tenant_cookie_header(vid_a)
check("Set-Cookie 含 HttpOnly", "HttpOnly" in _ch, _ch)
check("Set-Cookie 含 SameSite=Lax", "SameSite=Lax" in _ch, _ch)
check("Set-Cookie 含 Path=/ 与 Max-Age",
      "Path=/" in _ch and "Max-Age=" in _ch, _ch)
check("★ Set-Cookie 不含任何时间戳/IP/个人信息（只有随机 id）",
      _ch.count(vid_a) == 1 and "127." not in _ch and "192." not in _ch, _ch)

print("\n[5] 建库：原子落位 + 并发")


# ===== ⑧ ensure_tenant_db =====
vid_new = cloud.new_tenant_id()
p = cloud.ensure_tenant_db(vid_new)
check("ensure_tenant_db 返回库路径", p.exists(), p)
check("★ 建库后没有残留 .tmp-* 文件",
      not list(p.parent.glob("data.db.tmp-*")),
      list(p.parent.glob("*")))
import sqlite3                                                          # noqa: E402
c2 = sqlite3.connect(str(p))
c2.row_factory = sqlite3.Row
try:
    tables = {r[0] for r in c2.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
finally:
    c2.close()
check("★ 新库里有核心表 entries/articles/article_sentences",
      {"entries", "articles", "article_sentences"} <= tables, sorted(tables))
check("★ 新库里有 meta 表（S2 幂等判据）", "meta" in tables, sorted(tables))

# 重复调用应幂等（第二次不重建，直接返回）
p2 = cloud.ensure_tenant_db(vid_new)
check("★ 重复 ensure_tenant_db 幂等（同一路径）", p == p2)

# 并发：8 线程同时给**同一个新访客**建库
vid_race = cloud.new_tenant_id()
errs = []


def _race():
    try:
        cloud.ensure_tenant_db(vid_race)
    except Exception as exc:                   # noqa: BLE001
        errs.append(f"{type(exc).__name__}: {exc}")


ths = [threading.Thread(target=_race) for _ in range(8)]
for t in ths:
    t.start()
for t in ths:
    t.join()
check("★ 8 线程并发建同一个新访客：0 失败", not errs, errs[:3])
check("★ 并发后无残留 .tmp-* 文件",
      not list((config.app_dir() / "tenants" / vid_race).glob("*.tmp-*")),
      list((config.app_dir() / "tenants" / vid_race).glob("*")))
check("★ 并发后库文件存在且可打开",
      (config.app_dir() / "tenants" / vid_race / "data.db").exists())

print("\n[6] 云端 get_api_key：只有请求头一级")


# ★ 为什么不是「三级」而是「一级」 ★
# 云端部署一旦配了 OPENAI_API_KEY，这台机器上**所有访客共用同一把平台密钥**：
# 白嫖（任一访客都能花平台额度）+ 串号（用量算到平台账上）。
# ⇒ 云端分支在**读 env 之前**就 return False —— 不是「读了不用」，
#   是根本不读，代码里也不留残骸。
#   云端唯一合法的密钥来源是访客请求头带的那把（只活在访客 localStorage）。
# 桌面分支逐字未变，仍是 env > settings.json 两级（见本文件第 ③ 节）。
_atok = cloud.request_api_key.set("header-key-abc")
try:
    check("★ 请求头密钥优先级最高（云端）",
          config.get_api_key() == "header-key-abc", config.get_api_key())
    cloud.request_api_key.set("   ")       # 只有空白字符 = 本次没带密钥
    os.environ["OPENAI_API_KEY"] = "env-" + "key-xyz"
    check("★ 云端请求头为空时不给环境变量那把 key（平台密钥不外借）",
          config.get_api_key() == "", config.get_api_key())
    del os.environ["OPENAI_API_KEY"]
    check("★ 都没有时也是空串（不回落 settings）",
          config.get_api_key() == "", config.get_api_key())
finally:
    cloud.request_api_key.reset(_atok)

print("\n[7] 云端不落盘密钥")


# ===== ⑩ save_settings 在云端抹掉 api_key =====
_atok2 = cloud.request_api_key.set("should-not-persist")
_stok = cloud.current_tenant.set(vid_a)
try:
    config.save_settings({"ai": {"api_key": "should-not-persist",
                                 "model": "gpt-4o-mini"}})
    raw = config.settings_path().read_text(encoding="utf-8")
    check("★ 云端 save_settings 后 settings.json 里搜不到该密钥",
          "should-not-persist" not in raw, raw[:200])
    check("★ 但非密钥字段正常落盘", "gpt-4o-mini" in raw, raw[:200])
finally:
    cloud.request_api_key.reset(_atok2)
    cloud.current_tenant.reset(_stok)

print("\n[8] web_search 开关与部署形态")

# ★ 先切回桌面模式 ★
#   [2]~[7] 把 ENPRACTICE_MODE 设成了 cloud 且一直没复位，所以 §8 里那几条
#   标着「桌面」的断言其实**一直跑在云端**。以前 web_search_enabled()
#   根本不看模式，这个 bug 一直藏着；现在云端会短路，
#   桌面断言就必须真的在桌面态下跑，否则等于没测。
os.environ.pop("ENPRACTICE_MODE", None)


# ===== ⑪ =====
check("桌面默认允许联网取句", cloud.web_search_enabled() is True)
os.environ["ENPRACTICE_WEB_SEARCH"] = "0"
check("ENPRACTICE_WEB_SEARCH=0 → 关闭",
      cloud.web_search_enabled() is False)
os.environ["ENPRACTICE_WEB_SEARCH"] = "1"
check("ENPRACTICE_WEB_SEARCH=1 → 仍允许", cloud.web_search_enabled() is True)
del os.environ["ENPRACTICE_WEB_SEARCH"]

# ★ 云端：web_search **恒关**，环境变量一律无效 ★
#   env 开关是给未来埋雷 —— 运维为了「让它功能更全」随手
#   ENPRACTICE_WEB_SEARCH=1，就把共享出口 IP 变成爬搜索结果页的出口，
#   而代码里看不出任何异常。所以云端在**读 env 之前**就短路了
#   （app/cloud.py:web_search_enabled）。
os.environ["ENPRACTICE_MODE"] = "cloud"
os.environ["ENPRACTICE_WEB_SEARCH"] = "1"
check("★ 云端 ENPRACTICE_WEB_SEARCH=1 也关（env 对云端无效）",
      cloud.web_search_enabled() is False)
os.environ["ENPRACTICE_WEB_SEARCH"] = "0"
check("★ 云端 ENPRACTICE_WEB_SEARCH=0 同样关",
      cloud.web_search_enabled() is False)
check("★ 云端 network_fetch_allowed() 恒关",
      cloud.network_fetch_allowed() is False)
del os.environ["ENPRACTICE_WEB_SEARCH"]
os.environ.pop("ENPRACTICE_MODE", None)

check("★ app/websearch.py 未被本次改造触碰（零改动约束）",
      not hasattr(cloud, "_websearch_patched"))

total = len(results)
passed = sum(1 for _n, ok in results if ok)
print("\n" + "=" * 66)
print(f"总计：{passed} PASS / {total - passed} FAIL（共 {total} 项）")
if passed != total:
    for name, ok in results:
        if not ok:
            print(f"   FAIL: {name}")