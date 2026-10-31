"""种子灌入回归测试（S2）。

运行：python test_seed.py
（脚本式，与本仓库其它 test_*.py 一致；不用 pytest）

★ 全部在临时 ENPRACTICE_HOME + 临时库文件里跑，**绝不碰 %APPDATA%\\EnPractice 真实库**。

★ 与 `tools/validate_seed.py` 的分工 ★
  · 本文件守的是**代码不变量**：join 契约、标签合规硬校验、幂等、真库守卫、
    云端 provisioning 自带种子、**校验器的退出码契约**。这些回归了就是代码坏了，
    必须进 run_tests.sh。
  · 词表内容层面的校验（每句结尾标点、词条标签与文章标签是否一致…）在
    `tools/validate_seed.py` 里。语料是人维护的数据，改词表不该让
    run_tests.sh 变红 —— 那是构建期校验该报的事，不是回归。

★ 纪律：改完 app/seed.py 必须重跑本文件 + tools/validate_seed.py ★
本轮踩过：把 `article_payload` 的返回值从 tuple 改成 dict 时，只改了被调用的那一处，
没同步改 `seed_articles` 的解包 —— 中间那几分钟仓库是「跑不起来的半成品」。
所以规矩是：**任何一次改动之后，三个校验（validate_seed / test_seed /
verify_desktop_regression）全部重跑，不许引用上一次的结论。**
"""
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY_EXE = sys.executable

TMP = tempfile.mkdtemp(prefix="enp-seed-test-")
os.environ["ENPRACTICE_HOME"] = TMP
os.environ.pop("ENPRACTICE_MODE", None)          # 本套件默认桌面模式

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import articles, cloud, config, db, seed, seed_data  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    mark = "PASS" if ok else "FAIL"
    print(f"    [{mark}] {name}" + (f"\n            -> {detail}" if detail and not ok else ""))


ARTS = seed.all_articles()
VOCAB = seed_data.VOCABULARY
EXPECT_PAIRS = sum(len(a["pairs"]) for a in ARTS)

# ===========================================================================
print("\n[1] declared_tags：标签合规性**硬校验**，不合规就炸（不裁剪）")
# ===========================================================================
tags = seed.declared_tags({"title": "T", "tags": ["A", "A", "B", "C"]})
check("重复标签按业务层规则去重", tags == ["A", "B", "C"], tags)
check("None / 空列表安全", seed.declared_tags({"title": "T"}) == []
      and seed.declared_tags({"title": "T", "tags": []}) == [])

#★ 反向判据：塞 6 个标签（= MAX+1）必须抛 SeedDataError，且报错要指出是哪篇文章
over = {"title": "超限测试文", "tags": [f"T{i}" for i in range(articles.MAX_TAGS_PER_ARTICLE + 1)]}
try:
    seed.declared_tags(over)
    raised, msg = False, "没有抛错"
except seed.SeedDataError as exc:
    raised, msg = True, str(exc)
check(f"★ 超过 {articles.MAX_TAGS_PER_ARTICLE} 个标签时抛 SeedDataError", raised, msg)
check("★ 报错里指出是哪篇文章（否则 7 篇一批灌根本查不出来）",
      "超限测试文" in msg and str(articles.MAX_TAGS_PER_ARTICLE + 1) in msg, msg)
check("★ 不再存在 fit_tags（静默裁剪已移除）", not hasattr(seed, "fit_tags"))

# ===========================================================================
print("\n[2] article_payload 的 join 契约（1:1 对齐的前提）")
# ===========================================================================
bad_join = []
for a in ARTS:
    payload = seed.article_payload(a)
    pairs = a["pairs"]
    if payload["content"] != " ".join(p[0] for p in pairs):
        bad_join.append(f"{a['title']} 英文 join 不符")
    if payload["content_zh"] != "".join(p[1] for p in pairs):
        bad_join.append(f"{a['title']} 中文 join 不符")
    en_s = articles.split_sentences(payload["content"])
    zh_s = articles.split_zh_sentences(payload["content_zh"])
    if len(en_s) != len(pairs) or len(zh_s) != len(pairs):
        bad_join.append(f"{a['title']} 切句 {len(en_s)}/{len(zh_s)} != {len(pairs)}")
    _al, mode, merged, _st = articles._align_detail(en_s, payload["content_zh"])
    if mode != "exact" or merged:
        bad_join.append(f"{a['title']} mode={mode} merged={merged}")
check(f"★ {len(ARTS)} 篇的 content/content_zh join 方式与切句器对齐前提全部成立",
      not bad_join, bad_join[:4])
check("article_payload 不吞掉 title_zh / url / source_name",
      seed.article_payload(ARTS[0])["title_zh"] == ARTS[0]["title_zh"]
      and seed.article_payload(ARTS[0])["source_name"] == ARTS[0]["source_name"])
check("公有领域篇的 url 被带上（版权出处要能在界面上点开）",
      seed.article_payload(seed_data.PUBLIC_DOMAIN_ARTICLE)["url"]
      == seed_data.PUBLIC_DOMAIN_ARTICLE["url"])

# ===========================================================================
print("\n[3] entry_payload：词条级 tags 是死字段，不能进 payload")
# ===========================================================================
p = seed.entry_payload(VOCAB[0])
check("payload 不含 tags（词条级标签无表承载，传了也会被丢弃）", "tags" not in p, p.keys())
check("payload 含 type/term/pos/meaning_zh/usage/source",
      all(k in p for k in ("type", "term", "pos", "meaning_zh", "usage", "source")), p.keys())
check(f"source 统一为 {seed.SOURCE_SEED!r}", p["source"] == seed.SOURCE_SEED)
check("评分字段透传", p["importance"] == VOCAB[0]["importance"]
      and p["usefulness"] == VOCAB[0]["usefulness"])

# ===========================================================================
print("\n[4] seed_database：真灌一遍临时库")
# ===========================================================================
db_file = os.path.join(TMP, "seeded.db")
result = seed.seed_database(db_file)
check("首次执行返回 seeded=True", result["seeded"] is True, result)
check("落库统计：文章 inserted == 篇数",
      result["stats"]["articles"]["inserted"] == len(ARTS), result["stats"])
check("落库统计：词条 inserted == 词表条数",
      result["stats"]["entries"]["inserted"] == len(VOCAB), result["stats"])

conn = db._open(db_file)
try:
    n_art = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    n_sent = conn.execute("SELECT COUNT(*) FROM article_sentences").fetchone()[0]
    n_empty = conn.execute(
        "SELECT COUNT(*) FROM article_sentences WHERE text_zh IS NULL OR TRIM(text_zh)=''"
    ).fetchone()[0]
    n_ent = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    n_rev = conn.execute("SELECT COUNT(*) FROM review_state").fetchone()[0]
    n_node = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE kind='entry' AND entry_id IS NOT NULL"
    ).fetchone()[0]
    n_tags = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    n_article_tags = conn.execute("SELECT COUNT(*) FROM article_tags").fetchone()[0]
    max_tags = conn.execute(
        "SELECT MAX(n) FROM (SELECT COUNT(*) n FROM article_tags GROUP BY article_id)"
    ).fetchone()[0]
    row = conn.execute("SELECT value FROM meta WHERE key=?",
                       (seed.META_KEY_SEED_VERSION,)).fetchone()

    check(f"articles == {len(ARTS)}", n_art == len(ARTS), n_art)
    check(f"article_sentences == {EXPECT_PAIRS}（每篇句数 == len(pairs)）",
          n_sent == EXPECT_PAIRS, n_sent)
    check("★ text_zh 非空率 100%（没密钥也能跑通核心闭环）", n_empty == 0, n_empty)
    check(f"entries == {len(VOCAB)}", n_ent == len(VOCAB), n_ent)
    check("★ review_state 行数 == 词条数（种子词自动进复习队列）", n_rev == n_ent, (n_rev, n_ent))
    check("★ nodes(kind='entry') 行数 == 词条数（自动进层级结构）", n_node == n_ent,
          (n_node, n_ent))
    check("★ 去重标签数 ≥ 5", n_tags >= 5, n_tags)
    check(f"每篇标签数 ≤ {articles.MAX_TAGS_PER_ARTICLE}", max_tags is not None
          and max_tags <= articles.MAX_TAGS_PER_ARTICLE, max_tags)
    # ★ team-lead 要求的哨兵：灌库路径一旦重新引入"静默裁标签"，这条立刻红。
    declared_tags = sum(len(articles.normalize_tag_names(a.get("tags"))) for a in ARTS)
    check(f"★ seed_visitor 产出的 article_tags 关联数 == 种子声明的标签总数（{declared_tags}）",
          n_article_tags == declared_tags, f"库 {n_article_tags} vs 种子声明 {declared_tags}")
    check("★ seed_articles 统计里的 tag_total 与库里的关联数一致（两处口径同源）",
          result["stats"]["articles"]["tag_total"] == n_article_tags,
          f"统计 {result['stats']['articles'].get('tag_total')} vs 库 {n_article_tags}")
    check(f"meta.seed_version == {seed.SEED_VERSION}",
          row is not None and row["value"] == str(seed.SEED_VERSION),
          None if row is None else row["value"])

    # 逐词可达：核心闭环的地基，词条取不到例句 = 练习出不了题
    missing = [v["term"] for v in VOCAB
               if not articles.pick_sentences_for_term(conn, v["term"], limit=5)]
    check(f"★ 逐词 pick_sentences_for_term 可达率 100%（{len(VOCAB) - len(missing)}/{len(VOCAB)}）",
          not missing, missing)

    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("articles", "article_sentences", "entries", "review_state", "nodes")}
finally:
    conn.close()

# ===========================================================================
print("\n[5] 幂等：重复灌不写任何东西")
# ===========================================================================
again = seed.seed_database(db_file)
check("★ 第二次返回 seeded=False（meta.seed_version 判重）",
      again["seeded"] is False, again)
conn = db._open(db_file)
try:
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in ("articles", "article_sentences", "entries", "review_state", "nodes")}
finally:
    conn.close()
check("★ 重复执行后各表行数不变", before == after, f"{before} vs {after}")

# ===========================================================================
print("\n[6] force=True 仍然只新增不删除（补灌路径的安全边界）")
# ===========================================================================
conn = db._open(db_file)
try:
    conn.execute("INSERT INTO articles (title, source_name, content, content_zh, "
                 "added_at, updated_at) VALUES ('访客自己加的文章', NULL, 'x', '', "
                 "datetime('now'), datetime('now'))")
    conn.commit()
    user_rows = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
finally:
    conn.close()
forced = seed.seed_database(db_file, force=True)
conn = db._open(db_file)
try:
    n_art2 = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    kept = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE title='访客自己加的文章'").fetchone()[0]
    dup_seed = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE title=?", (ARTS[0]["title"],)
    ).fetchone()[0]
    n_ent2 = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
finally:
    conn.close()
check("force=True 返回 seeded=True", forced["seeded"] is True)
check("★ force 不删访客自己的数据", kept == 1, kept)
check("★ force 不产生重复种子文章", dup_seed == 1, dup_seed)
check(f"文章总数 = {user_rows} + 0（种子按 title 去重后不再新增）",
      n_art2 == user_rows, (n_art2, user_rows))
check(f"★ force 下词条仍不重复（{len(VOCAB)}）", n_ent2 == len(VOCAB), n_ent2)

# ===========================================================================
print("\n[7] 真库守卫")
# ===========================================================================
try:
    seed.seed_database(config.db_path())
    guarded = False
    detail = "没有抛错"
except RuntimeError as exc:
    guarded = "拒绝把演示种子写进真实用户库" in str(exc)
    detail = str(exc)
except Exception as exc:                       # noqa: BLE001
    guarded, detail = False, f"抛了非 RuntimeError：{exc!r}"
check("★ 拒绝把演示种子写进 db_path()（桌面模式下那就是用户自己的库）", guarded, detail)

# ===========================================================================
print("\n[8] 云端 provisioning：新访客库天生带种子（无需二次灌）")
# ===========================================================================
os.environ["ENPRACTICE_MODE"] = "cloud"
tid = cloud.new_tenant_id()
path = cloud.ensure_tenant_db(tid)
check("库文件已落位", path.exists(), str(path))
check("路径在 tenants/<id>/ 下", path.parent.name == tid and path.parent.parent.name == "tenants",
      str(path))
conn = db._open(path)
try:
    n_art = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    n_ent = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    n_zh = conn.execute(
        "SELECT COUNT(*) FROM article_sentences WHERE text_zh IS NULL OR TRIM(text_zh)=''"
    ).fetchone()[0]
    sv = conn.execute("SELECT value FROM meta WHERE key=?",
                      (seed.META_KEY_SEED_VERSION,)).fetchone()
finally:
    conn.close()
check(f"★ 新访客库 articles == {len(ARTS)}（种子与 schema 一起原子落位）",
      n_art == len(ARTS), n_art)
check(f"★ 新访客库 entries == {len(VOCAB)}", n_ent == len(VOCAB), n_ent)
check("★ 新访客库译文齐全（首屏不是空列表）", n_zh == 0, n_zh)
check("★ 新访客库带 seed_version（下次请求直接跳过灌种）",
      sv is not None and sv["value"] == str(seed.SEED_VERSION))
check("★ 临时库文件无残留",
      not list(path.parent.glob("data.db.tmp-*")), list(path.parent.iterdir()))

# 第二个访客必须是**另一份**库，且各自都带种子
tid2 = cloud.new_tenant_id()
path2 = cloud.ensure_tenant_db(tid2)
check("两个访客拿到不同的库文件", path != path2 and path.exists() and path2.exists())
conn = db._open(path2)
try:
    n_ent2 = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
finally:
    conn.close()
check(f"第二个访客同样自带 {len(VOCAB)} 条种子", n_ent2 == len(VOCAB), n_ent2)
os.environ.pop("ENPRACTICE_MODE", None)

# ===========================================================================
print("\n[9] 语料不合规时：当场炸 + 不留残骸")
# ===========================================================================
# 注入一篇超限文章（标签数 = MAX+1），验证「不静默裁剪、不半成品落盘」。
# 只 monkeypatch 运行时的函数对象，不改任何产品文件。
_orig_all_articles = seed.all_articles
OVER_ARTICLE = {
    "title": "【测试用】超限文",
    "title_zh": "测试用",
    "source_name": "本项目原创",
    "tags": [f"T{i}" for i in range(articles.MAX_TAGS_PER_ARTICLE + 1)],
    "pairs": [("This sentence exists only for the test.", "这句话只用于测试。")],
}
seed.all_articles = lambda: _orig_all_articles() + [OVER_ARTICLE]
bad_db = os.path.join(TMP, "should_not_exist.db")
try:
    seed.seed_database(bad_db)
    raised, msg = False, "不合规语料被悄悄灌成功了"
except seed.SeedDataError as exc:
    raised, msg = True, str(exc).splitlines()[0]
check("★ 语料超限时 seed_database 抛 SeedDataError（不静默裁剪）", raised, msg)
check("★ 报错点名是哪篇文章 + 说明上限值",
      OVER_ARTICLE["title"] in msg and str(articles.MAX_TAGS_PER_ARTICLE) in msg, msg)
check("★ 失败后不留半截库文件（sqlite3.connect 会先建出空文件）",
      not os.path.exists(bad_db), bad_db)

# 云端 provisioning 侧同样不许给访客建出一个"有 schema 没种子"的空壳库
os.environ["ENPRACTICE_MODE"] = "cloud"
tid3 = cloud.new_tenant_id()
try:
    cloud.ensure_tenant_db(tid3)
    raised2, msg2 = False, "provisioning 没有抛错"
except seed.SeedDataError as exc:
    raised2, msg2 = True, str(exc).splitlines()[0]
check("★ 云端 provisioning 在语料不合规时抛错", raised2, msg2)
check("★ 失败后 tenants/<id>/data.db 没有落位",
      not (Path(TMP) / "tenants" / tid3 / "data.db").exists())
seed.all_articles = _orig_all_articles
os.environ.pop("ENPRACTICE_MODE", None)

# ===========================================================================
print("\n[10] tools/validate_seed.py 的退出码契约（0 通过 / 1 不通过 / 2 工具崩了）")
# ===========================================================================
VALIDATOR = REPO / "tools" / "validate_seed.py"


def _run_validator(path: Path) -> tuple[int, str]:
    """跑校验器并**取它自己的**退出码。

    ⚠️ 不能用 `... | tail` 再读 `$?` —— 那样拿到的是 tail 的 0，崩溃会被当成通过。
    这里用 subprocess 直接拿 returncode。
    """
    env = dict(os.environ)
    env["ENPRACTICE_HOME"] = TMP                    # 绝不碰真实用户库
    proc = subprocess.run([PY_EXE, str(path)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, cwd=str(REPO),
                          timeout=600)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


rc_ok, out_ok = _run_validator(VALIDATOR)
check("正常运行时退出码 == 0", rc_ok == 0, f"rc={rc_ok}")
check("正常运行时确实跑完了（输出里有 SEED CHECK 汇总）",
      "SEED CHECK" in out_ok, out_ok[-300:])

# 注入一个必崩的场景：把副本里 main() 的第一行换成 raise，跑完立刻删副本。
# 副本必须与原文件同目录（tools/），否则它按 __file__ 推出来的 REPO_ROOT 会错。
crash_copy = VALIDATOR.with_name("_tmp_rev_crash_validate.py")
src = VALIDATOR.read_text(encoding="utf-8")
anchor = "    parser = argparse.ArgumentParser("
assert anchor in src, "注入锚点找不到（validate_seed.py 结构变了？）"
assert crash_copy != VALIDATOR
try:
    crash_copy.write_text(
        src.replace(anchor,
                    '    raise RuntimeError("注入的崩溃：用于验证退出码契约")\n' + anchor,
                    1),
        encoding="utf-8")
    rc_crash, out_crash = _run_validator(crash_copy)
    check("★ 注入崩溃后退出码 == 2（与「校验不通过」的 1 区分开）",
          rc_crash == 2, f"rc={rc_crash}")
    check("★ 崩溃时打印了 FATAL 提示与 traceback",
          "[FATAL]" in out_crash and "注入的崩溃" in out_crash,
          out_crash[-400:])
finally:
    try:
        crash_copy.unlink(missing_ok=True)
    except OSError:
        pass
check("临时副本已删除（不留残骸）", not crash_copy.exists(), str(crash_copy))

# ===========================================================================
total = len(results)
passed = sum(1 for _n, ok in results if ok)
print("\n" + "=" * 66)
print(f"总计：{passed} PASS / {total - passed} FAIL（共 {total} 项）")
if passed != total:
    for name, ok in results:
        if not ok:
            print(f"   FAIL: {name}")
else:
    atexit.register(shutil.rmtree, TMP, True)
    print(f"（临时目录 {TMP} 将在进程退出时清理）")

if passed != total:
    sys.exit(1)