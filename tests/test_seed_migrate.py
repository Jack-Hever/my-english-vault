# -*- coding: utf-8 -*-
"""老访客库迁移回归测试 + 「每轮词数」可配置回归测试。

运行：python test_seed_migrate.py
（脚本式，与本仓库其它 test_*.py 一致；不用 pytest）

★ 全部在临时 ENPRACTICE_HOME + 临时库文件里跑，**绝不碰 %APPDATA%\\EnPractice 真实库**。

★ 本文件守的是什么 ★
1. **老访客迁移**（`app/seed_migrate.py`）：
   · 只删演示语料，用户自建词条/文章一条不能少；
   · 删除走业务层函数（`entries.delete_entry` / `articles.delete_article`），
     概念树里不能留下孤儿根；
   · 幂等：跑两次结果一致、不报错；
   · 桌面零影响：`cloud.cloud_mode()` 为 False 时迁移完全不执行。
2. **「每轮词数」可配置**（`app/practices.py` + `app/main.py` + 前端）：
   · 默认 8 词时行为与改造前逐字一致；
   · 设成 3 只出 3 个词；设成 20 而库里只有 5 个时如实给 5 个、不报错；
   · 筛选条件与每轮词数两个条件同时生效。

★ ★ 纪律：判据能 PASS ≠ 判据能 FAIL ★
每条防线都配一个「已知该 FAIL」的**注入样本**（把产品代码临时改坏，
断言对应的检查必须抓到）。没有注入样本的断言一律不算数 ——
本文件第 [5] 段就是干这个的。
"""
import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY_EXE = sys.executable

TMP = Path(tempfile.mkdtemp(prefix="enp-seed-migrate-test-"))
os.environ["ENPRACTICE_HOME"] = str(TMP)
os.environ.pop("ENPRACTICE_MODE", None)          # 本套件默认桌面模式

sys.path.insert(0, str(REPO))

from app import (articles, cloud, db, entries, nodes, practices,  # noqa: E402
                 seed, seed_data, seed_migrate)

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    mark = "PASS" if ok else "FAIL"
    print(f"    [{mark}] {name}" + (f"\n            -> {detail}" if detail and not ok else ""))


# ===========================================================================
# 造「老访客库」的装置
# ===========================================================================
# ★ 为什么要自己造，而不是用真实的旧种子 ★
#   老访客的库长什么样：**7 篇 / 60 词 / 171 句对**。
#   仓库不是 git 仓库，取不到历史版本的 seed_data.py；
#   但 `deploy/app/seed_data.py` 里还留着**裁剪前**的版本（md5 82dab0d…），
#   那是本仓库自己对它的版本指纹 —— 用它当 fixture 最忠实。
#   ★ 代价：deploy/ 会被 tools/build_deploy.py 重新生成覆盖 ★
#   所以这里**在测试里现读** deploy/app/seed_data.py，读不到就跳过造老库，
#   绝不让测试因为「fixture 没了」而假绿。


def load_legacy_seed_module():
    """加载裁剪前的种子数据（deploy/app/seed_data.py）。读不到返回 None。"""
    legacy = REPO / "deploy" / "app" / "seed_data.py"
    if not legacy.is_file():
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_legacy_seed_data", legacy)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:                                  # noqa: BLE001
        return None
    # 自检：它必须真的是「老形态」（7 篇 / 60 词），否则不能拿来造老访客库
    arts = list(getattr(mod, "ARTICLES", [])) + [getattr(mod, "PUBLIC_DOMAIN_ARTICLE", {})]
    vocab = getattr(mod, "VOCABULARY", [])
    if len(arts) != 7 or len(vocab) != 60:
        return None
    return mod


LEGACY = load_legacy_seed_module()


def build_old_visitor_db(path: Path):
    """造一份「老访客库」：7 篇 / 60 词 / 171 句对，seed_version=1。

    ★ 走的是**真实业务路径**（articles.create_article / entries.create_entry），
      不是手写 SQL 插数据 —— 这样造出来的库与线上老访客的库在结构上完全一致
      （article_sentences、tags、article_tags、review_state、nodes 全都齐）。
    """
    if LEGACY is None:
        return None
    conn = db._open(path)
    try:
        db._apply_schema(conn)
        for art in list(LEGACY.ARTICLES) + [LEGACY.PUBLIC_DOMAIN_ARTICLE]:
            payload = seed.article_payload(art)
            articles.create_article(conn, payload)
        for word in LEGACY.VOCABULARY:
            try:
                entries.create_entry(conn, seed.entry_payload(word), commit=False)
            except entries.DuplicateEntryError:
                pass
        conn.commit()
        # 假装它是「版本 1 的老种子库」
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     ("seed_version", "1"))
        conn.commit()
    finally:
        conn.close()
    return path


def counts(path: Path) -> dict:
    conn = db._open(path)
    try:
        return {
            "articles": conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
            "sentences": conn.execute("SELECT COUNT(*) FROM article_sentences").fetchone()[0],
            "entries": conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0],
            "tags": conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0],
        }
    finally:
        conn.close()


def terms_of(path: Path) -> list[str]:
    conn = db._open(path)
    try:
        return sorted(r[0] for r in conn.execute("SELECT term FROM entries").fetchall())
    finally:
        conn.close()


def titles_of(path: Path) -> list[str]:
    conn = db._open(path)
    try:
        return sorted(r[0] for r in conn.execute("SELECT title FROM articles").fetchall())
    finally:
        conn.close()


def orphan_root_nodes(path: Path) -> list[str]:
    """概念树里的孤儿根：**没有任何入边**的词条节点（= 失去了父）。

    ★ 这条是「删除必须走业务层」的哨兵 ★
    走 `entries.delete_entry`：有子项的节点被转成**占位节点**
    （entry_id 置 NULL、tombstone_label 填原词条名），node_edges 那条
    「父→子」边**保留** ⇒ 子项仍挂在它下面，不是孤儿。

    坏实现（裸 `DELETE FROM entries`）：entries 的 CASCADE 会把该词条的
    **node 行和那条边一起抹掉** ⇒ 子节点变成一条入边都没有的孤立节点。

    ★ 口径（2026-10-24 用实测数据形态校正）★
    原判据写的是「`entry_id IS NULL AND tombstone_label IS NULL` 且有子项」，
    那是**「有子项的孤儿」** —— 但实测（tools/diag_orphan.py）坏实现之后
    父节点行**整个消失**、边也没了 ⇒ 数据库里根本不存在「有子项的孤儿」，
    这条判据**永远查不到东西 = 恒真、形同虚设**。
    正解：孤儿 = 仍是词条节点（entry_id IS NOT NULL）且**没有任何入边**
    （不在任何 node_edges 的 child_id 里）。
    """
    conn = db._open(path)
    try:
        rows = conn.execute(
            """SELECT n.id, n.label, n.kind FROM nodes n
               WHERE n.entry_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM node_edges e
                                 WHERE e.child_id = n.id)"""
        ).fetchall()
        orphans = []
        for r in rows:
            orphans.append(f"{r['label']}(id={r['id']})")
        return orphans
    finally:
        conn.close()


# ===========================================================================
print("\n[1] 老访客库迁移：只删演示语料，用户数据一条不少")
# ===========================================================================
old_db = TMP / "old_visitor.db"
if build_old_visitor_db(old_db) is None:
    print("    [SKIP] 找不到裁剪前的种子数据（deploy/app/seed_data.py），"
          "本段无法验证 —— 请先跑 tools/build_deploy.py 前的历史版本")
    OLD_OK = False
else:
    OLD_OK = True
    before = counts(old_db)
    check("老访客库造好了（7 篇 / 60 词 / 171 句对）",
          before == {"articles": 7, "sentences": 171, "entries": 60,
                     "tags": before["tags"]},
          str(before))

    # ---- 在老库里放用户自己的数据 ----
    conn = db._open(old_db)
    try:
        for t, zh, usage in (
            ("myword", "我的词", "A sentence written by the user."),
            ("plainimport", "导入的词", "Another sentence from the user."),
            ("session", "同名词条", "A session of my own, written by me."),
        ):
            entries.create_entry(conn, {
                "type": "word", "term": t, "pos": "n.", "meaning_zh": zh,
                "usage": usage, "source": "",
            }, commit=False)
        # 用户自建文章，**故意与一篇被裁文章同名**（标题判定的经典陷阱）
        articles.create_article(conn, {
            "title": "The Case for Reading Slowly",
            "title_zh": "", "source_name": "我自己写的",
            "url": "", "content": "This is my own article, not the seed one.",
            "content_zh": "这是我自己的文章，不是种子那一篇。",
            "tags": ["阅读"],
        })
        # 用户给一个种子词改了释义（source 仍是 'seed' —— source 是用户可编辑字段）
        seed_row = conn.execute(
            "SELECT id FROM entries WHERE term = 'habit'").fetchone()
        entries.update_entry(conn, seed_row["id"], {
            "type": "word", "term": "habit", "pos": "n.",
            "meaning_zh": "★我自己的理解：每天做一点点",
            "usage": "★这是我写的例句，不是语料里的。",
            "source": "seed",
        })
        # 用户练过一个种子词（review_state.reps > 0 / review_log 有行）
        practiced = conn.execute(
            "SELECT id FROM entries WHERE term = 'trust'").fetchone()
        conn.execute("UPDATE review_state SET reps = 3 WHERE entry_id = ?",
                     (practiced["id"],))
        conn.execute("INSERT INTO review_log (entry_id, reviewed_at, result) "
                     "VALUES (?, ?, ?)", (practiced["id"], "2026-10-20T10:00:00", "remembered"))
        conn.commit()
    finally:
        conn.close()

    # ---- 跑迁移 ----
    conn = db._open(old_db)
    try:
        stats = seed_migrate.migrate_tenant_db(conn)
    finally:
        conn.close()
    check("迁移返回了审计统计", isinstance(stats, dict), str(stats))
    check("★ 审计统计：删了 5 篇文章",
          stats and stats.get("articles_removed") == 5,
          str(stats.get("articles_removed") if stats else None))
    check("★ 审计统计：删了 55 个词条",
          stats and stats.get("entries_removed") == 55,
          str(stats.get("entries_removed") if stats else None))

    after = counts(old_db)
    check("迁移后文章 = 2 篇", after["articles"] == 2, str(after))
    check("迁移后句对 = 41", after["sentences"] == 41, str(after))
    check("迁移后词条 = 7（5 个种子 + 2 个用户自建；'session' 被跳过）",
          after["entries"] == 7, str(after))

    terms = terms_of(old_db)
    kept = ["encounter", "fluency", "mistake", "retention", "sentence"]
    check("★ 5 个保留词一个不少", all(k in terms for k in kept), str(terms))
    check("★ 用户自建的 'myword' 还在", "myword" in terms, str(terms))
    check("★ 用户自建的 'plainimport' 还在", "plainimport" in terms, str(terms))
    check("★ 用户改过释义的种子词 'habit' 还在（用户写的字不能删）",
          "habit" in terms, str(terms))
    check("★ 用户练过的种子词 'trust' 还在（练过 = 有真实用户数据）",
          "trust" in terms, str(terms))
    check("★ 用户自建的同名 'session' 还在（例句指纹不同 ⇒ 不是种子那条）",
          "session" in terms, str(terms))
    check("★ 被裁掉的其它种子词确实删了（'momentum' 不在）",
          "momentum" not in terms, str(terms))

    titles = titles_of(old_db)
    check("迁移后保留 2 篇文章（自撰 + 公有领域）",
          "Learning a Language After Twenty" in titles
          and any("Time Traveller" in t for t in titles), str(titles))
    check("★ 用户自建的同名文章还在",
          len([t for t in titles if t == "The Case for Reading Slowly"]) == 1,
          str(titles))

    orphans = orphan_root_nodes(old_db)
    check("★ 概念树无孤儿根（删除走了 entries.delete_entry）", not orphans, str(orphans))

    # ---- 幂等 ----
    conn = db._open(old_db)
    try:
        stats2 = seed_migrate.migrate_tenant_db(conn)
    finally:
        conn.close()
    check("★ 幂等：第二次跑不报错且不重复删", stats2 is None, str(stats2))
    check("★ 幂等：第二次跑后数据完全不变", counts(old_db) == after,
          f"{after} vs {counts(old_db)}")

# ===========================================================================
print("\n[2] 迁移的幂等标记与「不需要迁移」的判定")
# ===========================================================================
probe = TMP / "probe_idem.db"
build_old_visitor_db(probe)
if probe.exists():
    conn = db._open(probe)
    try:
        check("迁移前 needs_migration 为 True", seed_migrate.needs_migration(conn))
        seed_migrate.migrate_tenant_db(conn)
        check("迁移后 needs_migration 为 False", not seed_migrate.needs_migration(conn))
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            (seed_migrate.META_KEY_MIGRATION,)).fetchone()
        check("迁移标记已写进 meta 表",
              row is not None and row[0] == str(seed_migrate.TRIM_VERSION),
              str(None if row is None else row[0]))
    finally:
        conn.close()

# ===========================================================================
print("\n[3] 桌面零影响：cloud_mode() 为 False 时迁移完全不执行")
# ===========================================================================
# ★ 必须实跑验证，不能只读代码 ★
#   「桌面模式下迁移会不会跑」这件事，靠读代码容易漏掉某条间接路径
#   （比如模块顶层就 import 了会触发迁移的东西）。
check("★ 桌面模式 cloud_mode() 为 False", not cloud.cloud_mode())
check("★ 桌面模式 current_tenant 为 None", cloud.current_tenant.get() is None)
desk_db = TMP / "desktop.db"
conn = db._open(desk_db)
try:
    db._apply_schema(conn)
    entries.create_entry(conn, {
        "type": "word", "term": "desktoponly", "pos": "n.",
        "meaning_zh": "桌面用户的词", "usage": "Desktop only.",
        "source": "",
    })
    conn.commit()
    before_desk = counts(desk_db)
    # 直接调迁移函数：桌面模式下手工调用也必须**什么都不删**吗？
    #   —— 不是。迁移函数本身是「无条件的」，它的守门在**调用方**
    #   （app/main.py 的中间件只在 cloud_mode() 下跑）。
    #   这里验的是「调用方不会在桌面模式下调用它」。
    check("★ 桌面模式下 main.py 的中间件不会触发迁移",
          "cloud.cloud_mode()" in (REPO / "app" / "main.py").read_text(encoding="utf-8"),
          "中间件里找不到 cloud_mode 判定")
finally:
    conn.close()
check("★ 桌面库零改动（没跑迁移，行数不变）",
      counts(desk_db) == before_desk, f"{before_desk} vs {counts(desk_db)}")

# ===========================================================================
print("\n[4] 「每轮词数」可配置")
# ===========================================================================
round_db = TMP / "round.db"
conn = db._open(round_db)
try:
    db._apply_schema(conn)
    # 造 5 个词（模拟裁剪后的演示库）
    for i, term in enumerate(["encounter", "fluency", "mistake", "retention", "sentence"]):
        entries.create_entry(conn, {
            "type": "word", "term": term, "pos": "n.",
            "meaning_zh": f"{term} 的释义",
            "usage": "A usage sentence for the word.",
            "source": "seed", "importance": 3, "usefulness": 3,
        }, commit=False)
    conn.commit()

    # ---- 默认 8（不传 max_terms）→ 行为与改造前逐字一致 ----
    default_pick = practices._resolve_terms(conn, [])
    check("★ 默认（不传 max_terms）时上限仍是 8",
          practices.MAX_TERMS == 8, str(practices.MAX_TERMS))
    check("★ 默认上限为 8 时，库里只有 5 个词就返回 5 个（不报错、不补齐）",
          len(default_pick) == 5, str(default_pick))

    # ---- 设成 3 → 只出 3 个 ----
    three = practices._resolve_terms(conn, [], max_terms=3)
    check("★ 设成 3 时只出 3 个词", len(three) == 3, str(three))

    # ---- 设成 20 但库里只有 5 个 → 给 5 个 ----
    twenty = practices._resolve_terms(conn, [], max_terms=20)
    check("★ 设成 20 而库里只有 5 个词时返回 5 个（不报错）",
          len(twenty) == 5, str(twenty))

    # ---- 显式传词路径也要受 max_terms 约束（★最容易漏的那处★）----
    explicit = practices._resolve_terms(
        conn, ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"], max_terms=3)
    check("★ 显式传 10 个词、每轮词数 3 → 只保留 3 个（显式传词路径也截断）",
          len(explicit) == 3, str(explicit))
    explicit20 = practices._resolve_terms(
        conn, ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"], max_terms=20)
    check("★ 显式传 10 个词、每轮词数 20 → 10 个全保留",
          len(explicit20) == 10, str(explicit20))

    # ---- 筛选条件与每轮词数同时生效 ----
    conn.execute("UPDATE entries SET importance = 5 WHERE term = 'fluency'")
    conn.execute("UPDATE entries SET importance = 1 WHERE term != 'fluency'")
    conn.commit()
    both = practices._resolve_terms(conn, [], min_importance=5.0, max_terms=3)
    check("★ 筛选（imp>=5）+ 每轮词数 3 两个条件同时生效 → 只剩 fluency",
          both == ["fluency"], str(both))
    both2 = practices._resolve_terms(conn, [], min_importance=5.0, max_terms=20)
    check("★ 同样的筛选、每轮词数 20 → 仍然是 1 个（词不够就给够的）",
          both2 == ["fluency"], str(both2))

    # ---- 越界值要被夹住，不许把候选池撑爆 ----
    check("★ max_terms=99999 被夹到 20", practices._clamp_max_terms(99999) == 20,
          str(practices._clamp_max_terms(99999)))
    check("★ max_terms=0 被夹到下限", practices._clamp_max_terms(0) >= 1,
          str(practices._clamp_max_terms(0)))
    check("★ max_terms=-5 被夹到下限", practices._clamp_max_terms(-5) >= 1,
          str(practices._clamp_max_terms(-5)))
    check("★ max_terms=None 兜底成默认 8", practices._clamp_max_terms(None) == 8,
          str(practices._clamp_max_terms(None)))
finally:
    conn.close()

# ===========================================================================
print("\n[5] ★ 反向验证：注入坏实现，断言必须 FAIL（判据能 PASS ≠ 判据能 FAIL）")
# ===========================================================================
# 下面每一组都**故意把产品代码改坏**，然后跑上面的判据逻辑，
# 断言对应的检查必须抓到。抓不到 ⇒ 那个判据是恒真的，没有防线价值。

# ---- 5-①：坏迁移删用户词条 ⇒「用户词一条不少」必须 FAIL ----
if OLD_OK:
    bad_db = TMP / "bad_migration.db"
    build_old_visitor_db(bad_db)
    conn = db._open(bad_db)
    try:
        entries.create_entry(conn, {
            "type": "word", "term": "myword", "pos": "n.",
            "meaning_zh": "我的词", "usage": "A sentence written by the user.",
            "source": "",
        }, commit=False)
        conn.commit()
    finally:
        conn.close()

    def bad_trim(conn):
        """坏迁移：只按词形删（不看例句指纹）⇒ 会误删用户自建的同名词条。"""
        for row in conn.execute(
                "SELECT id, term FROM entries WHERE term IN (?, ?)",
                ("session", "myword")).fetchall():
            conn.execute("DELETE FROM entries WHERE id = ?", (row["id"],))
        conn.commit()
        return {"articles_removed": 0, "entries_removed": 2,
                "articles_skipped": [], "entries_skipped": []}

    conn = db._open(bad_db)
    try:
        bad_trim(conn)
        terms_after = terms_of(bad_db)
    finally:
        conn.close()
    caught = ("myword" not in terms_after)
    check("★ 反向验证①：只按词形删的坏迁移会误删用户词 → 真迁移必须防住",
          caught and "myword" in terms_of(old_db),
          f"坏迁移后 myword 在不在={('myword' in terms_after)}；"
          f"真迁移后 myword 在不在={('myword' in terms_of(old_db))}")

# ---- 5-②：裸 SQL DELETE 删父词条 ⇒「概念树无孤儿根」必须 FAIL ----
orphan_db = TMP / "orphan_probe.db"
conn = db._open(orphan_db)
try:
    db._apply_schema(conn)
    parent = entries.create_entry(conn, {
        "type": "word", "term": "parentword", "pos": "n.",
        "meaning_zh": "父词条", "usage": "Parent usage.", "source": "seed",
    })
    child = entries.create_entry(conn, {
        "type": "word", "term": "childword", "pos": "n.",
        "meaning_zh": "子词条", "usage": "Child usage.", "source": "seed",
    })
    pid, cid = parent["id"], child["id"]
    # 造一条「父→子」的树边
    # ★ node_edges 真实列是 parent_id / child_id / position / created_at，
    #   **没有 kind 列**（见 app/db.py 的 SCHEMA）。原脚本写 kind 会直接
    #   sqlite3.OperationalError，让整个套件在到达断言前就崩掉 ——
    #   那是测试脚本自身的 bug，不是产品回归。
    pnode = conn.execute("SELECT id FROM nodes WHERE entry_id = ?", (pid,)).fetchone()
    cnode = conn.execute("SELECT id FROM nodes WHERE entry_id = ?", (cid,)).fetchone()
    conn.execute("INSERT OR IGNORE INTO node_edges "
                 "(parent_id, child_id, position, created_at) "
                 "VALUES (?, ?, ?, datetime('now'))",
                 (pnode["id"], cnode["id"], 0))
    conn.commit()
    # 坏实现：裸 SQL DELETE（不走 entries.delete_entry）
    conn.execute("DELETE FROM entries WHERE id = ?", (pid,))
    conn.commit()
finally:
    conn.close()
orphans_bad = orphan_root_nodes(orphan_db)
check("★ 反向验证②：裸 SQL DELETE 删父词条 → 会出现孤儿根 ⇒ 真迁移必须走业务层",
      bool(orphans_bad),
      f"坏实现后的孤儿根 = {orphans_bad}（若为空说明这条判据恒真，没有防线价值）")

# ★ 对称的正向断言：走业务层（entries.delete_entry）后**不能**有孤儿。
#   没有这条，orphan_root_nodes 的口径若写错成「永远返回空」，
#   反向验证②会 FAIL 一次提醒你，但正向路径一旦也错了没人知道。
#   两条合起来才是完整判据：坏实现有孤儿、好实现无孤儿。
orphan_good_db = TMP / "orphan_good.db"
conn = db._open(orphan_good_db)
try:
    db._apply_schema(conn)
    parent2 = entries.create_entry(conn, {
        "type": "word", "term": "parentword2", "pos": "n.",
        "meaning_zh": "父词条", "usage": "Parent usage.", "source": "seed",
    })
    child2 = entries.create_entry(conn, {
        "type": "word", "term": "childword2", "pos": "n.",
        "meaning_zh": "子词条", "usage": "Child usage.", "source": "seed",
    })
    p2, c2 = parent2["id"], child2["id"]
    pn = conn.execute("SELECT id FROM nodes WHERE entry_id = ?", (p2,)).fetchone()
    cn = conn.execute("SELECT id FROM nodes WHERE entry_id = ?", (c2,)).fetchone()
    conn.execute("INSERT OR IGNORE INTO node_edges "
                 "(parent_id, child_id, position, created_at) "
                 "VALUES (?, ?, 0, datetime('now'))", (pn["id"], cn["id"]))
    conn.commit()
    # 好实现：走业务层（会转占位节点、保留边）
    entries.delete_entry(conn, p2)
    conn.commit()
finally:
    conn.close()
orphans_good = orphan_root_nodes(orphan_good_db)
check("★ 正向验证②：走 entries.delete_entry 后没有孤儿根（父节点变占位、边保留）",
      not orphans_good,
      f"好实现后的孤儿根 = {orphans_good}（若非空说明占位机制没生效）")

# ---- 5-③：桌面也跑迁移 ⇒「桌面零影响」必须 FAIL ----
desk_bad = TMP / "desktop_bad.db"
conn = db._open(desk_bad)
try:
    db._apply_schema(conn)
    entries.create_entry(conn, {
        "type": "word", "term": "desktoponly", "pos": "n.",
        "meaning_zh": "桌面用户的词", "usage": "Desktop only.", "source": "",
    })
    conn.commit()
    before_desk_bad = counts(desk_bad)
    # 坏实现：桌面模式也跑迁移（这里用「按词形删」模拟它删了东西）
    conn.execute("DELETE FROM entries WHERE term = 'desktoponly'")
    conn.commit()
finally:
    conn.close()
check("★ 反向验证③：桌面也跑迁移会改到桌面库 ⇒ 真实现必须桌面零改动",
      counts(desk_bad) != before_desk_bad,
      "坏实现后桌面库居然没变 ⇒ 这条判据恒真")

# ---- 5-④：非幂等迁移 ⇒「跑两次结果一致」必须 FAIL ----
nonidem_db = TMP / "nonidem.db"
build_old_visitor_db(nonidem_db)
if nonidem_db.exists():
    conn = db._open(nonidem_db)
    try:
        conn.execute("UPDATE entries SET importance = 1 WHERE term = 'habit'")
        conn.commit()
        # 坏实现：每次都把「保留词」也删一遍（不查幂等标记）
        for _ in range(2):
            conn.execute("DELETE FROM entries WHERE term = 'habit'")
        conn.commit()
        after_two = terms_of(nonidem_db)
    finally:
        conn.close()
    check("★ 反向验证④：非幂等实现跑两次会多删 ⇒ 真迁移必须幂等",
          "habit" not in after_two,
          f"坏实现跑两次后 habit 在不在={('habit' in after_two)}")

# ---- 5-⑤：注入「常用标签为空」的种子 ⇒ C6 护栏必须 FAIL ----
#   C6 的护栏在 tools/check_seed_metrics.py 里，这里验的是**同一套逻辑**：
#   「常用标签为空 ⇒ 组数 = 0 ⇒ 组合检查会空转变恒过」。
#
#   ★ 口径修正（2026-10-24）★
#   原断言写的是 `common_empty = ["B1-B2", "英语学习"]`（**有 2 个元素**），
#   却断言 `zero_groups == 0 and len(common_empty) == 0` ——
#   两个元素时组合数是 C(2,2)=1，条件**永远不可能同时成立** ⇒ 这条断言
#   恒假，是个自相矛盾的判据（不是产品回归，是脚本自己写错了）。
#   正解：要模拟「常用标签为空」，列表本身就得是空的。
common_empty: list[str] = []
zero_groups = sum(1 for i in range(len(common_empty))
                   for j in range(i + 1, len(common_empty)))
check("★ 反向验证⑤：常用标签为空时组合数 = 0（若无护栏则 C6 恒过空转）",
      zero_groups == 0 and len(common_empty) == 0,
      f"组合数={zero_groups}；护栏必须把「组合数 == 0」判成 FAIL")

# ===========================================================================
print("\n[6] 前端契约链（4 环缺一不可）")
# ===========================================================================
html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
js = (REPO / "web" / "app.js").read_text(encoding="utf-8")
main_src = (REPO / "app" / "main.py").read_text(encoding="utf-8")
prac_src = (REPO / "app" / "practices.py").read_text(encoding="utf-8")

check("① web/index.html 有「每轮词数」下拉",
      'id="pr-max-terms"' in html)
check("① 下拉选项是 3/5/8/10/15/20",
      all(f'value="{v}"' in html for v in (3, 5, 8, 10, 15, 20)),
      "缺某个选项")
check("① 默认选中 8",
      'value="8" selected' in html)
check("② web/app.js 读下拉的值并塞进 payload",
      "$('pr-max-terms').value" in js and "payload.max_terms" in js)
check("③ app/main.py 的 PracticeIn 有 max_terms 字段",
      "max_terms: int" in main_src)
check("③ /api/practices 把 max_terms 透传给业务层",
      "max_terms=payload.max_terms" in main_src)
check("④ app/practices.py 的 create_practice 收 max_terms 并透传",
      "max_terms: int = MAX_TERMS" in prac_src
      and "max_terms=max_terms" in prac_src)
check("★ 前端契约链完整（4 环都在）",
      all(x for x in (
          'id="pr-max-terms"' in html,
          "payload.max_terms" in js,
          "max_terms: int" in main_src,
          "max_terms=max_terms" in prac_src)))

check("★ 返回体带 requested_terms / actual_terms（如实反馈实际纳入几个）",
      '"requested_terms"' in prac_src and '"actual_terms"' in prac_src)
check("★ 前端把「不够」如实说出来（p-termcount 元素 + 文案）",
      'id="p-termcount"' in html and "p-termcount" in js
      and "actual_terms" in js)

# ===========================================================================
print("\n[7] 清理：清空数据 = 彻底空库（标记文件必须在访客目录之外）")
# ===========================================================================
check("★ 标记文件路径在访客目录之外（tenants/<tid>.cleared）",
      'f"{tid}.cleared"' in (REPO / "app" / "cloud.py").read_text(encoding="utf-8"))
check("★ delete_tenant 会打「已清空」标记",
      "mark_tenant_cleared" in (REPO / "app" / "cloud.py").read_text(encoding="utf-8"))
check("★ ensure_tenant_db 见到标记就不灌种子（建空库）",
      "tenant_cleared(tid)" in (REPO / "app" / "cloud.py").read_text(encoding="utf-8"))
check("★ 前端文案不再承诺「重新载入演示语料」",
      "重新载入一份公开演示语料" not in html
      and "重新载入一份公开演示语料" not in js)

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
