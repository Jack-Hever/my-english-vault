# -*- coding: utf-8 -*-
"""云端访客种子灌入（app/seed.py）。

★ 为什么不直接 `python run.py` 让人手点种子 ★
用户打开网页的第一眼就是「外刊库是空的、词汇库是空的」→ 直接判定功能没做完。
种子必须**在访客库落位的那一刻**一起写进去，不能是「库建好了，种子过一会儿
再异步灌」——那个窗口里访客看到的就是空列表。

★ 原子性 ★
调用方（`cloud.ensure_tenant_db`）把本函数跑在一个**临时库文件**上，
跑完才 `os.replace` 原子落位。于是「文件存在」⟺「schema 与种子都在」，
不存在半初始化库，也没有"有库没种子"的中间态。

★ 幂等 ★
判据是 `meta.seed_version`（见 app/db.py 的 meta 表）：
  · 等于 `SEED_VERSION` → 直接返回 False，一个字节都不写；
  · 不等 / 没有 → 走自然键去重再灌一遍（文章按 `title`、词条按 `(type, term)`）。
天然支持"改了种子内容后给老访客补灌"：只新增不删除，重复的自然键被跳过。

★ ★ 只新增不删除 —— 这是它的能力边界，不是 bug ★
`SEED_VERSION` 升到 2 能让**新访客**拿到裁剪后的 2 篇 / 5 词，
但**老访客**库里那 55 个被裁掉的词、5 篇被裁的文章**不会**消失
（版本号只触发「补灌」，不触发「删除」）。
⇒ 删除型的裁剪在 `app/seed_migrate.py`，触发点挂在 `app/main.py` 的中间件
  （每个 /api/ 请求都走）—— 因为 `cloud.ensure_tenant_db` 见到已存在的库
  会直接 return，把迁移挂在那里等于永远不执行。

★ 一行不改 articles.py ★
种子灌入完全走既有业务 API：`articles.create_article` 与 `entries.create_entry`。
好处不只是省代码 —— 走同一条路径就意味着「种子里看到的字段与用户在界面上
新建的完全一致」，不会出现"演示数据走了另一条捷径"。

★ 关于词条的 `tags` 字段 ★
拍板决策：词条级标签在当前数据模型里**没有表承载**（`entries` 表无 tags 列，
`article_tags` 只服务文章），所以 per-word `tags` 是**校验元数据**：
  · 灌入时**不落库**（`entries.create_entry` 也不接这个键）；
  · 由 `tools/validate_seed.py` 断言「这个词的主题标签确实出现在它引用文章的标签里」，
    防止这个字段悄悄变成没人看的死数据。
若将来加 `entry_tags` 表，只需在这里多传一次即可，本文件的其余部分不用动。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from . import articles, db, entries, seed_data
from .entries import now_iso

#: 种子版本号。**改了 seed_data.py 的内容就 +1**，老访客库里的版本号对不上
#: 就会自动补灌；不改种子内容就别动它，否则每次建库都要重灌一遍。
#:
#: ★ v2（2026-10-24）：演示语料从「7 篇 / 60 词 / 171 句对」裁到
#:   「2 篇 / 5 词 / 41 句对」—— 设计约束「词和文章又不是产品内容」，
#:   演示语料只是让人上手的样例，不该喧宾夺主。
#: ★ 但这个版本号只管「新增」★
#:   老访客库里那 55 个被裁掉的词、5 篇被裁的文章**不会**因为版本号 +1 而消失
#:   （种子机制是「只新增不删除」）。删除型的裁剪由 `app/seed_migrate.py` 负责，
#:   触发点在 `app/main.py` 的中间件（每个 /api/ 请求都走）。
SEED_VERSION = 2

#: meta 表里记版本号的键。
META_KEY_SEED_VERSION = "seed_version"
#: meta 表里记「什么时候灌的」的键，纯审计用，便于排查"这库看着不对是不是没灌全"。
META_KEY_SEED_AT = "seed_at"

#: 词条 `source` 字段统一填这个值（界面上据此区分"自建"与"演示语料"）。
SOURCE_SEED = "seed"


class SeedDataError(RuntimeError):
    """种子语料本身不合规（缺句对、标签超限…）。

    ★ 为什么不复用 `articles.ArticleError` ★
    这类错误的责任方是 `app/seed_data.py`（人维护的数据），不是请求。
    单独一个类型，日志里一眼能分清「用户提交的文章不合法」与「我们的语料坏了」。
    """


# ---------------------------------------------------------------------------
# 种子内容 → 业务层 payload
# ---------------------------------------------------------------------------


def all_articles() -> list[dict]:
    """全部种子文章：1 篇自撰 + 1 篇 Project Gutenberg 公有领域选段（共 2 篇）。

    ★ 2026-10-24 裁剪 ★
    原来是 6 篇自撰 + 1 篇公有领域 = 7 篇。裁到 2 篇：
      · 保留《Learning a Language After Twenty》（自撰，26 句对）
      · 保留《The Time Traveller》（公有领域，15 句对）——
        它是项目版权声明章节明确引用的素材。
    """
    return list(seed_data.ARTICLES) + [seed_data.PUBLIC_DOMAIN_ARTICLE]


def declared_tags(article: dict) -> list[str]:
    """取这篇文章声明的标签（用业务层自己的归一化规则去重），并**硬校验**数量。

    ★ 为什么是「报错」而不是「裁剪」 ★
    曾经的做法是超限时悄悄裁到 5 个 —— 那是把「语料不合规」伪装成「灌库成功」：
    构建期没人报错、跑起来也没人报错，等到用户在界面上发现标签少了一个，
    已经查不出是哪一步丢的了。所以现在改成**当场炸**，并把文章标题和超限个数
    一起带出来，指到具体那篇文章。

    ★ 校验放这里、而不是等 `create_article` 抛 ArticleError ★
    `create_article` 的报错只说「最多 5 个（当前 6 个）」，不说**是哪篇文章**。
    种子是一批灌的，没有文章名的话只能自己回去数 —— 那正是当初该拦住的地方。

    ★ 用 `articles.normalize_tag_names` 而不是自己再写一套去重 ★
    去重口径必须与灌库时用的完全一致（精确重复合并、保留大小写形态）。
    两处各写一套，将来改了一处另一处就会静默偏掉。
    """
    tags = articles.normalize_tag_names(article.get("tags"))
    limit = articles.MAX_TAGS_PER_ARTICLE
    if len(tags) > limit:
        title = str(article.get("title") or "(无标题)")
        raise SeedDataError(
            f"种子文章《{title}》声明了 {len(tags)} 个标签，超过 articles."
            f"MAX_TAGS_PER_ARTICLE={limit}：{tags}。\n"
            f"修法：改 app/seed_data.py 把标签删到 {limit} 个以内（不要靠灌库路径裁剪 —— "
            f"那会让不合规的语料悄悄通过）。"
        )
    return tags


def article_payload(article: dict) -> dict:
    """把种子文章转成 `articles.create_article` 的 payload。

    ★ 句对的拼法是 1:1 契约的核心 ★
      英文用空格 join、中文用空串 join —— 拼接后切句器切出来的句数恰好等于
      `len(pairs)`，于是 DP 对齐必然给出 `align_mode="exact"` / `merged=0`，
      练习时每句都有独立参考译文（不需要 AI 密钥）。
      **改这里的 join 方式会让整个对齐前提失效**，validate_seed.py 的 ④⑤⑥ 会立刻红。
    """
    pairs = article.get("pairs") or []
    if not pairs:
        raise SeedDataError(f"种子文章缺少句对：{article.get('title') or '(无标题)'}")
    return {
        "title": article.get("title") or "",
        "title_zh": article.get("title_zh") or "",
        "source_name": article.get("source_name") or "",
        "url": article.get("url") or "",
        "content": " ".join(p[0] for p in pairs),
        "content_zh": "".join(p[1] for p in pairs),
        "tags": declared_tags(article),
    }


def entry_payload(word: dict) -> dict:
    """把种子词条转成 `entries.create_entry` 的 payload。

    ★ `tags` 刻意不传 ★
    词条级标签当前无表承载（见模块 docstring），传了也会被
    `normalize_entry_payload` 丢掉；它是校验元数据，由 validate_seed.py 消费。
    """
    return {
        "type": "word",
        "term": word.get("term") or "",
        "pos": word.get("pos") or "",
        "meaning_zh": word.get("meaning_zh") or "",
        "usage": word.get("usage") or "",
        "source": SOURCE_SEED,
        "importance": word.get("importance"),
        "usefulness": word.get("usefulness"),
    }


# ---------------------------------------------------------------------------
# 灌入
# ---------------------------------------------------------------------------


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    """读 meta 表的值；表不存在（旧库没跑过 _apply_schema）时返回 None。"""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None                          # 没有 meta 表 = 还没建 schema
    return None if row is None else row[0]


def already_seeded(conn: sqlite3.Connection) -> bool:
    """这个库是否已经灌过当前版本的种子。"""
    return _meta_get(conn, META_KEY_SEED_VERSION) == str(SEED_VERSION)


def seed_articles(conn: sqlite3.Connection) -> dict:
    """灌 2 篇文章，返回统计。

    去重键是 `title`：`create_article` 本身不查重（产品的文章允许同名），
    所以幂等由这里兜住。已有同标题文章 → 跳过（只新增不删除）。
    """
    inserted, skipped = 0, 0
    tag_total = 0
    for art in all_articles():
        title = str(art.get("title") or "")
        exists = conn.execute(
            "SELECT 1 FROM articles WHERE title = ? LIMIT 1", (title,)
        ).fetchone()
        if exists:
            skipped += 1
            continue
        payload = article_payload(art)
        tag_total += len(payload["tags"])
        articles.create_article(conn, payload)
        inserted += 1
    # `tag_total` 是「本次声明并写入的标签关联数」，与 test_seed.py 里
    # 「article_tags 行数 == 种子声明的标签总数」那条断言对账用 ——
    # 一旦有人在灌库路径上偷偷裁标签，这两个数就会对不上。
    return {"inserted": inserted, "skipped": skipped, "tag_total": tag_total}


def seed_entries(conn: sqlite3.Connection) -> dict:
    """灌全部词条，返回统计。

    去重由业务层自己负责：`entries.create_entry` 查到重名会抛
    `DuplicateEntryError`，我们捕获后跳过 —— 用业务层的查重规则而不是自己
    再写一套 `SELECT`，避免两套口径将来跑偏。
    """
    inserted, skipped = 0, 0
    for word in seed_data.VOCABULARY:
        payload = entry_payload(word)
        try:
            entries.create_entry(conn, payload, commit=False)
            inserted += 1
        except entries.DuplicateEntryError:
            skipped += 1
    return {"inserted": inserted, "skipped": skipped}


def seed_visitor(conn: sqlite3.Connection, *, force: bool = False) -> bool:
    """给一个库灌种子。幂等：已灌过当前版本就返回 False（一个字都不写）。

    ★ `force=True` 的用途 ★
    给「改了 seed_data.py 但 SEED_VERSION 没动」的救急场景用；正常路径永远
    走默认的幂等分支。force 依然只新增不删除，不会覆盖访客已有的数据。
    """
    if not force and already_seeded(conn):
        seed_visitor.last_stats = {"version": SEED_VERSION, "already_seeded": True}
        return False
    art_stats = seed_articles(conn)
    ent_stats = seed_entries(conn)
    # 显式重跑回填（幂等兜底）：create_entry 已经播种过 review_state 与 nodes，
    # 这里再跑一次是为了覆盖"补灌"路径 —— 那次进来的条目可能来自旧版本 create_entry。
    db._backfill_entry_nodes(conn)
    conn.execute(db.BACKFILL_REVIEW_STATE, (db.DEFAULT_EASINESS,))
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (META_KEY_SEED_VERSION, str(SEED_VERSION)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (META_KEY_SEED_AT, now_iso()),
    )
    conn.commit()
    seed_visitor.last_stats = {           # type: ignore[attr-defined]
        "version": SEED_VERSION,
        "already_seeded": False,
        "articles": art_stats,
        "entries": ent_stats,
    }
    return True


#: 最近一次 seed_visitor 的统计（仅供日志/校验器读取，不参与业务判定）。
#: 每次调用都会重写它 —— 跳过分支也会写 `already_seeded: True`，
#: 否则调用方会读到上一次灌入的陈旧数字。
seed_visitor.last_stats = {}              # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 给指定路径建库并灌种子（校验器 / 手工排查用）
# ---------------------------------------------------------------------------


def _assert_not_real_db(path: Path) -> None:
    """拒绝把演示种子灌进**真实用户库**。

    桌面模式下真实库在 %APPDATA%/EnPractice/data.db。往里灌演示数据 = 把用户
    自己的词汇库和外刊库污染成别人的内容，且没有任何界面提示能分辨。
    这道守卫让「手滑敲错路径」在第一行就炸掉，而不是悄悄写进去。
    """
    from . import config

    try:
        real = Path(config.db_path()).resolve()
    except Exception:                        # noqa: BLE001 - 拿不到就放弃这道校验
        return
    if os.path.normcase(str(path.resolve())) == os.path.normcase(str(real)):
        raise RuntimeError(
            f"拒绝把演示种子写进真实用户库：{real}。\n"
            "要给临时/访客库灌种子，请换一个 --db 路径。"
        )


def seed_database(db_path: str | os.PathLike, *, force: bool = False) -> dict:
    """在**指定路径**建库 + 灌种子，返回统计。校验器与手工排查用。

    ★ 与云端 provisioning 的区别 ★
    云端走 `cloud.ensure_tenant_db`（tmp 文件 + os.replace 原子落位 + per-visitor 锁）；
    这里刻意不做原子落位 —— 它服务的是构建期校验与一次性排查，路径由人指定，
    不存在"两个访客抢同一个库"的并发场景。两者共用同一个 `seed_visitor`，
    所以校验器验的就是线上跑的那条路径。
    """
    target = Path(db_path)
    _assert_not_real_db(target)
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = db._open(target)
    try:
        db._apply_schema(conn)
        did = seed_visitor(conn, force=force)
        conn.commit()
    except BaseException:
        # ★ 失败不留残骸 ★
        #   sqlite3.connect 会**立刻**把空文件创建出来，接着 _apply_schema 还会写进
        #   一堆表。若语料不合规在这里抛错，磁盘上就留下一个"有 schema、没有种子"的
        #   半成品库 —— 下一个跑这个工具的人会以为"库已经建好了呀"，排查方向直接跑偏。
        #   只删本次新建的：本来就存在的库绝不碰（那是别人的数据）。
        conn.close()
        if not existed:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        conn.close()
    return {
        "seeded": did,
        "path": str(target),
        "version": SEED_VERSION,
        "stats": dict(seed_visitor.last_stats),
    }


# ---------------------------------------------------------------------------
# CLI：python -m app.seed --db <路径>
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    `--db` 是**必填**的：没有默认值可以忘。因为唯一"正确"的默认路径就是真实
    用户库，而那正是这道工具最不该碰的地方。
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.seed",
        description="把 app/seed_data.py 的演示语料灌进指定数据库（幂等）。",
    )
    parser.add_argument("--db", required=True, help="目标 SQLite 文件路径（禁止指向真实用户库）")
    parser.add_argument("--force", action="store_true",
                        help="忽略 meta.seed_version 判重，强制再走一遍（仍然只新增不删除）")
    args = parser.parse_args(argv)

    try:
        result = seed_database(args.db, force=args.force)
    except Exception as exc:                # noqa: BLE001 - CLI 要给人话
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] {result['path']}  seed_version={result['version']}  "
          f"是否执行={result['seeded']}")
    print(f"     {result['stats']}")
    return 0


if __name__ == "__main__":                   # pragma: no cover - CLI 入口
    raise SystemExit(main())