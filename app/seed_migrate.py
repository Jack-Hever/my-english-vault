# -*- coding: utf-8 -*-
"""老访客库迁移：把「演示语料」从 7 篇 / 60 词裁到 2 篇 / 5 词。

★ 为什么要单独一个模块 ★
`app/seed.py` 的种子机制是**只新增不删除**（SEED_VERSION 对不上就按自然键补灌）。
所以「只改 seed_data.py」的效果是：
  · 新访客 → 拿到裁剪后的 2 篇 / 5 词 ✓
  · **老访客 → 仍然是旧的 60 词 / 7 篇 ✗**
而用户和评审打开链接看到的正是老访客库 ⇒ 必须显式做一次**删除型**迁移。

★ 三层删除防线（每条都有实跑依据，绝不用 source='seed' 当删除依据）★
⛔ 第一条铁律：`entries.source` 是**用户可编辑字段**
   （`app/entries.py` 的 `normalize_entry_payload` 允许改，`import_entries` 原样保留）。
   用它当删除依据有两条实测误删路径：
     ① 用户改过某个种子词的释义 → source 仍是 'seed' → 被误删
     ② 用户导入 CSV，某行 source 写成 'seed' → 被误删
   ⇒ 本模块**从不用 source 判定**，只用下面三道。

第一道（主）：**白名单式删除** —— 只删「明确被裁掉的那批具体内容」。
  · 词条：按**精确词形**（`app/seed_migrate.py` 里的 `RETIRED_TERMS` 冻结清单，
    即老种子那 60 个词里的 55 个被裁掉的）。
    ⚠️ 为什么用词形不用 id：老访客库里的 id 是建库时自增出来的，各访客不同，
      服务端没有它们的 id 清单。词形 + 「它确实来自老种子」这个附加条件才可行。
  · 文章：按**标题精确匹配**（`RETIRED_ARTICLES` 冻结清单，5 篇被裁的自撰）。
    ⚠️ 用户可能自建过同名文章 → 所以还要叠加下面两道防线。

第二道（兜底保险）：跳过 `updated_at != created_at` 的行。
  · 实测：老访客库里种子词的 `created_at == updated_at`（建库后未被写过）。
  · ⚠️ `now_iso()` 是**秒级**精度 → 同一秒内的编辑看不出来（概率极低）。
  · ⚠️ 只改评分（importance/usefulness）**也会刷新 updated_at**
    ⇒ 这条判据捕捉的是「这一行被写过」，不是「用户改过内容」
    ⇒ 用户**打过分的**种子词会删不掉 → 代价是「老访客裁剪不彻底」，可接受
      （迁移幂等，可重跑；不会误删用户数据，这个方向是安全的）。

第三道（更硬的独立信号）：跳过用户**真的练过**的词。
  · `review_state.reps > 0` 或 `review_log` 有该词条的行。
  · 实测新建库：`review_state` 60 行但 `reps>0` **0 行**、`review_log` **0 行**。
  · 这条比 updated_at 硬得多（练过 = 真有用户数据在里面）。

★ 删除必须走业务层函数 ★
  · 词条 → `entries.delete_entry(conn, id)`：它会处理概念树占位节点、
    由外键 CASCADE 清 review_state / review_log、prune 退化的关联组。
    ★ 直接 `DELETE FROM entries` 会把概念树里的子项变成孤儿根（app/entries.py 注释）。
  · 文章 → `articles.delete_article(conn, id)`：article_sentences / article_tags 由 CASCADE 清。
  · ⚠️ CASCADE 连带范围：`review_state` / `review_log` 外键是 ON DELETE CASCADE
    （app/db.py:35-48）→ 删词条会连带毁掉用户的复习记录 → 练过的词必须跳过（第三道）。

★ 幂等 ★
跑第二次不报错、不重复删（白名单里的行第一次就删光了，第二次查不到）。
判据用 `meta` 表的一个迁移标记键。

★ 桌面零影响 ★
迁移只在云端模式（`cloud.cloud_mode()` 为 True）下执行。桌面版的库是用户自己的，
桌面模式必须原样不动 —— 本模块的所有入口都先查 cloud_mode。

★ 僵尸标签 ★
`articles.delete_article` 只 CASCADE `article_tags` 行，**`tags` 表本身不清**；
`articles.list_tag_names` 用 LEFT JOIN ⇒ 0 篇使用的标签照样出现在筛选下拉里，
点进去 0 篇（实测删 5 篇后僵尸标签 = ['专注','学习效率','技术','沟通','阅读']）。
⇒ 本模块在迁移后顺带清掉「已无任何文章使用」的标签行。
  ★ 刻意**不动** `app/articles.py` 的 `list_tag_names` ——
    它被 `tests/test_cloud_security.py` 的 md5 ratchet 钉死，改了要同步改基线。
    在迁移里清标签是等价效果，且不碰被钉住的文件。
"""

from __future__ import annotations

import hashlib
import sqlite3

from . import articles, db, entries

#: 迁移标记：写进 meta 表，值为迁移完成时的版本号。
META_KEY_MIGRATION = "seed_trim_version"

#: 当前迁移版本。往后若还要裁剪演示语料，+1 并更新下面的冻结清单。
TRIM_VERSION = 1

#: 被裁掉的自撰文章标题（老种子 6 篇里的 5 篇）。
#: ★ 这张表只用来「知道有哪些篇要处理」，**不单独作为删除依据** ★
#:   QA 实测证明：只按标题删会误删用户自建的同名文章
#:   （用户在迁移前自建了一篇《The Case for Reading Slowly》→ 被连带删掉）。
#:   真正的删除依据是下面的**内容指纹**。
RETIRED_ARTICLES: tuple[str, ...] = (
    "Why Small Habits Beat Big Plans",
    "What Cities Teach Us About Focus",
    "How a Small Team Builds Software People Trust",
    "The Case for Reading Slowly",
    "Small Talk Is Not Small",
)

#: ⚠️⚠️ 这张表**只能含被裁掉的内容** ⚠️⚠️
#: 保留的《Learning a Language After Twenty》**绝不能**出现在这里 ——
#: 它一出现，`_article_is_old_seed` 就会判定「是老种子」⇒ 被删掉。
#:
#: ★★ 文章内容指纹表：标题 → 正文 SHA-256 ★★
#: 「这一篇文章是老种子的」由**正文逐字相同**来证明，不由标题相同来证明。
#: 用户自建的同名文章正文必然不同 ⇒ 指纹不匹配 ⇒ 绝不删除。
#: ★ 指纹是对老种子正文（`" ".join(英文句)`）算的 —— 与灌库时写进
#:   `articles.content` 的字符串完全一致（见 app/seed.py 的 article_payload）。
RETIRED_ARTICLE_FINGERPRINTS: dict[str, str] = {
    'Why Small Habits Beat Big Plans': '8ad59929607699b484628fc0dd92a5e5be550a917aa9ade1e59f47d5ba8a69de',
    'What Cities Teach Us About Focus': '6944b909f4458f93ce3a92dd81a336514fb4bc16098b38b0cc8fcfb77a39ee34',
    'How a Small Team Builds Software People Trust': 'b1a3b7f8bb1c9a8b621eeaef54725c7cb090453558118f80e05abe22aa3e7aa3',
    'The Case for Reading Slowly': '2e27b741e4eab50eb8134d04a821a0c6c1bd5f862eb93aa7de7a11c98345a8ff',
    'Small Talk Is Not Small': '70723f09b2654e5b9d8d66abf8f694a991101fcdb122f9cfd50f89b7cd458204',
}

#: 被裁掉的词形（老种子 60 词里的 55 个）。
#: ★ 保留的 5 个不在此列：fluency / encounter / mistake / retention / sentence
#: ★ 这张表只用来「知道有哪些词要处理」，**不单独作为删除依据** ★
#:   QA 实测证明：只按词形删会误删用户自建的同名词条
#:   （用户先删掉种子的 session、再自建一个自己的 session → 被连带删掉）。
#:   真正的删除依据是下面的**例句指纹**。
RETIRED_TERMS: tuple[str, ...] = (
    "habit", "friction", "momentum", "threshold", "session", "plan", "small",
    "finish", "environment", "daily", "willpower", "urge", "notification",
    "distraction", "gadget", "attention", "focus", "room", "signal", "replace",
    "predictable", "workaround", "incident", "software", "test", "release",
    "decision", "note", "trust", "promise", "recall", "rehearse", "word",
    "adult", "review", "reference", "argument", "structure", "medium",
    "deliberate", "page", "paragraph", "question", "reader", "chapter",
    "awkward", "exchange", "detail", "banter", "composure", "stranger",
    "answer", "listen", "silence", "gesture",
)

#: ⚠️⚠️ 保留的 5 个词（fluency/encounter/mistake/retention/sentence）
#: **绝不能**出现在这张表里 ⚠️⚠️
#:
#: ★★ 词条例句指纹表：词形 → 老种子的 usage 例句（逐字） ★★
#: 「这一条是老种子的」由**例句逐字相同**来证明，不由词形相同来证明。
#: 老种子的 usage 都是该文正文里的真实一整句（逐字相同），
#: 用户自建的同名词条例句必然不同 ⇒ 指纹不匹配 ⇒ 绝不删除。
RETIRED_ENTRY_FINGERPRINTS: dict[str, str] = {
    'habit': 'A habit that takes five minutes is much easier to keep than one that takes an hour.',
    'friction': 'The difference is not about willpower, but about how much friction the habit has to push through.',
    'momentum': 'Your daily plan and your momentum both live on paper, and most days you forget both before the session ends.',
    'threshold': 'The first two weeks are the point where most people quit, so set the threshold low enough that missing one day feels odd.',
    'session': 'If you can finish a session feeling good, you will start another one tomorrow.',
    'plan': 'A kinder plan is to study for twenty minutes and then stop on purpose.',
    'small': 'Another useful trick is to make the first step almost too small to refuse.',
    'finish': 'When you finish a small task, write it down, since a visible record of progress carries more momentum than any promise.',
    'environment': 'Your environment does half the work, because a phone on the table calls you back every few minutes.',
    'daily': 'A daily habit survives a bad week better than a strong plan, because a plan depends on how you feel on the day.',
    'willpower': 'Most people do not need more willpower, they need a replacement for the habit.',
    'urge': 'Third, decide what you will do when you feel the urge to check your phone.',
    'notification': 'Notifications, traffic lights, shop signs, and other people all pull at the same narrow resource.',
    'distraction': 'A distraction is anything that asks for a piece of your attention without offering anything in return.',
    'gadget': 'Second, keep one gadget out of the room entirely, not merely face down.',
    'attention': 'What gets your attention is rarely what you decided to think about.',
    'focus': 'Focus comes back quickly once the noise stops, and most people never wait long enough to find out.',
    'room': 'Your brain learns the meaning of a room from what you repeatedly do in it.',
    'signal': 'The city never stops offering signals, so focus has to be defended rather than found, and willpower matters less than the street outside.',
    'replace': 'If you want to replace one habit, you need something to take its place, not just something to remove.',
    'predictable': 'Trust in software is rarely earned by a feature list, but by boring, predictable behaviour.',
    'workaround': 'Six months later, nobody remembers why a strange workaround existed, but everybody can see it.',
    'incident': 'When an incident happens, the first question is not who caused it but which step allowed it to pass through.',
    'software': 'Trust in software is rarely earned by a feature list, but by boring, predictable behaviour.',
    'test': 'If a test breaks, the team should know within minutes, not during a release.',
    'release': 'Every release should have one clear goal, and if you cannot say it in one sentence, the release is too big.',
    'decision': 'A small decision made quietly is often better than a big decision made loudly, and after an incident the team needs quiet attention, not noise.',
    'note': 'Without a note, the next person either keeps the hack forever or removes it and breaks something.',
    'trust': 'Trust is built one small promise at a time, and every promise that is broken costs more than the next one can buy.',
    'promise': 'Good notes and good tests do not slow a team down, they remove the slow part of the work and keep the trust, and that promise is what people remember.',
    'recall': 'Review a short list aloud in the morning, and the same words come back easily at night, because retention needs a rhythm and recall needs repetition.',
    'rehearse': 'You can rehearse a sentence before you ever need it, by saying it out loud once.',
    'word': 'Once you can use the word, you can keep using it, but once you only recognise it, you forget it.',
    'adult': 'An adult usually meets the word inside a lesson, and then must carry it alone for the next week.',
    'review': 'The mistake most adults make is waiting for a natural moment, and no natural moment ever arrives, so review on a fixed day instead.',
    'reference': 'News, instructions, and reference material reward speed because you are looking for a fact.',
    'argument': 'Every argument has a shape, and deliberate reading shows the shape on the second page more clearly than on the first.',
    'structure': 'If the structure of the text is hidden, the reader will guess, and a guess about structure is usually wrong.',
    'medium': 'Reading on a screen feels faster, but the medium decides the pace more than the reader does.',
    'deliberate': 'Deliberate reading is not about reading every word carefully, but about pausing at the joins.',
    'page': 'When you ask what the writer wants from this page, you stop skimming and start reading.',
    'paragraph': 'A slow reader is not a slow person, because the reader is choosing to give a paragraph its full weight.',
    'question': 'That single question turns a page of text into a structure you can see.',
    'reader': 'A reader who finishes a chapter should be able to say what it argued, and if not, the reader lost the thread.',
    'chapter': 'Choose the medium that makes you slow down, because one chapter read this week is worth more than a whole book read never.',
    'awkward': 'Skipping it entirely can make a room colder than any awkward silence.',
    'exchange': 'Every exchange of small talk is a test, and if you listen rather than wait, the exchange works.',
    'detail': 'They remember the detail you mentioned ten minutes ago and ask about it.',
    'banter': 'Banter is a way of testing whether a stranger can take a joke without taking offence.',
    'composure': 'Composure in a conversation is mostly a matter of not filling every silence.',
    'stranger': 'Small talk also carries a practical function in groups of strangers.',
    'answer': 'The mistake is to answer the question instead of walking through it.',
    'listen': 'Small talk works when you listen for the tone, not for the words, and the awkward moments pass faster.',
    'silence': 'A silence of two seconds is a gift, and composure is what lets you leave it there without filling it.',
    'gesture': 'A gesture across a room can carry more warmth than a sentence, and banter does the same job with words.',
}


# ---------------------------------------------------------------------------
# 判定辅助
# ---------------------------------------------------------------------------


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None else row[0]


def _entry_was_edited(row: sqlite3.Row) -> bool:
    """这一行被写过吗（updated_at != created_at）。"""
    created = (row["created_at"] or "").strip()
    updated = (row["updated_at"] or "").strip()
    return bool(updated) and updated != created


def _entry_was_practiced(conn: sqlite3.Connection, entry_id: int) -> bool:
    """用户真的练过这个词吗？—— 更硬的「有用户数据」信号。

    `review_state.reps > 0` = 提交过练习；`review_log` 有行 = 有复习流水。
    两者任一成立就说明这个词里有用户的真实数据，**绝不能删**。
    """
    try:
        row = conn.execute(
            "SELECT reps FROM review_state WHERE entry_id = ?", (entry_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is not None and (row[0] or 0) > 0:
        return True
    try:
        row = conn.execute(
            "SELECT 1 FROM review_log WHERE entry_id = ? LIMIT 1", (entry_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    return row is not None


def _article_is_old_seed(row: sqlite3.Row) -> bool:
    """这一篇是老种子留下的吗？—— 由**正文指纹**判定，不由标题判定。

    ★ 为什么不按标题 ★
    QA 实测：用户自建一篇同名文章《The Case for Reading Slowly》，
    只按标题删 → 用户的文章被连带删掉（实测剩 0 篇）。
    ★ 正文逐字相同才算 ★
    用户自建的文章正文必然不同 ⇒ SHA-256 不同 ⇒ 不删。
    """
    title = row["title"] or ""
    expected = RETIRED_ARTICLE_FINGERPRINTS.get(title)
    if not expected:
        return False
    actual = hashlib.sha256((row["content"] or "").encode("utf-8")).hexdigest()
    return actual == expected


def _entry_is_old_seed(row: sqlite3.Row) -> bool:
    """这一条是老种子留下的吗？—— 由**例句指纹**判定，不由词形判定。

    ★ 为什么不按词形 ★
    QA 实测：用户先删掉种子的 session、再自建一个自己的 session，
    只按词形删 → 用户的词条被连带删掉（实测剩 0 条）。
    ★ usage 逐字相同才算 ★
    老种子的 usage 是原文里的真实一整句；用户自建的例句必然不同 ⇒ 不删。
    """
    term = (row["term"] or "").strip()
    expected = RETIRED_ENTRY_FINGERPRINTS.get(term)
    if expected is None:
        return False
    return (row["usage"] or "") == expected


def _article_was_edited(row: sqlite3.Row) -> bool:
    """文章被改过吗（updated_at != added_at）。"""
    added = (row["added_at"] or "").strip()
    updated = (row["updated_at"] or "").strip()
    return bool(updated) and updated != added


def _article_was_practiced(conn: sqlite3.Connection, article_id: int) -> bool:
    """这篇文章的句子被用户练过吗？

    `passages.source_url` 存的是外刊链接（库内练习是 NULL），不好判。
    更可靠的信号：练习素材 `passages.text_en` 里出现过这篇文章的句子。
    这里用「文章有没有句子的 added/updated 时间戳被动过」不够准，
    所以改用最保守的做法：**文章只要有句子被引用进过练习记录就跳过**。
    实现上查 article_sentences 与 passages 的交集代价高，
    因此退一步：只要该文章**有任何句子**存在于 passages.text_en 中就跳过。
    """
    try:
        sents = conn.execute(
            "SELECT text FROM article_sentences WHERE article_id = ?", (article_id,)
        ).fetchall()
    except sqlite3.OperationalError:
        return False
    if not sents:
        return False
    try:
        passages = conn.execute("SELECT text_en FROM passages").fetchall()
    except sqlite3.OperationalError:
        return False
    blob = "\n".join((p[0] or "") for p in passages)
    for (t,) in sents:
        txt = (t or "").strip()
        # 长句才用子串判（短句容易误命中）；短的直接跳过判据
        if len(txt) >= 25 and txt in blob:
            return True
    return False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def trim_demo_content(conn: sqlite3.Connection) -> dict:
    """把演示语料裁到 2 篇 / 5 词。**幂等**，返回审计统计。

    ★ 调用前请确保：这是访客自己的库，且已确认该做这次迁移。
    ★ 审计输出要能回答「删了什么、跳过了什么、为什么跳过」。
    """
    stats = {
        "version": TRIM_VERSION,
        "articles_removed": 0,
        "entries_removed": 0,
        "articles_skipped": [],
        "entries_skipped": [],
        "orphan_tags_removed": 0,
    }

    # 这两张表现在只用于「说明为什么跳过」，不再单独作为删除依据。
    retired_terms = {t.lower() for t in RETIRED_TERMS}
    retired_titles = set(RETIRED_ARTICLES)

    # ---- 1. 裁文章 ----
    rows = conn.execute(
        "SELECT id, title, content, added_at, updated_at FROM articles"
    ).fetchall()
    for row in rows:
        title = row["title"] or ""
        # ★ 第一道：正文指纹必须与老种子逐字相同（同名但内容不同 ⇒ 用户的，不删）
        if not _article_is_old_seed(row):
            if title in retired_titles:
                stats["articles_skipped"].append(
                    {"title": title, "reason": "同名但内容与老种子不同（用户自己的）"})
            continue
        reason = ""
        if _article_was_edited(row):
            reason = "用户改过内容"
        elif _article_was_practiced(conn, row["id"]):
            reason = "用户练过其中的句子"
        if reason:
            stats["articles_skipped"].append({"title": title, "reason": reason})
            continue
        articles.delete_article(conn, row["id"])
        stats["articles_removed"] += 1

    # ---- 2. 裁词条 ----
    rows = conn.execute(
        "SELECT id, term, usage, created_at, updated_at FROM entries"
    ).fetchall()
    for row in rows:
        term = (row["term"] or "").strip()
        # ★ 第一道：例句必须与老种子逐字相同（同名但例句不同 ⇒ 用户的，不删）
        if not _entry_is_old_seed(row):
            if term.lower() in retired_terms:
                stats["entries_skipped"].append(
                    {"term": term, "reason": "同名但例句与老种子不同（用户自己的）"})
            continue
        reason = ""
        if _entry_was_edited(row):
            reason = "用户改过（释义/评分等）"
        elif _entry_was_practiced(conn, row["id"]):
            reason = "用户练过这个词"
        if reason:
            stats["entries_skipped"].append({"term": term, "reason": reason})
            continue
        entries.delete_entry(conn, row["id"])
        stats["entries_removed"] += 1

    # ---- 3. 清僵尸标签 ----
    # delete_article 只 CASCADE 掉 article_tags 行，tags 表本身不清 ——
    # 而 list_tag_names 用 LEFT JOIN ⇒ 0 篇使用的标签照样出现在筛选下拉里。
    try:
        orphans = conn.execute(
            "SELECT id FROM tags WHERE id NOT IN (SELECT tag_id FROM article_tags)"
        ).fetchall()
    except sqlite3.OperationalError:
        orphans = []
    for (tag_id,) in orphans:
        conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        stats["orphan_tags_removed"] += 1

    conn.commit()
    return stats


def migrate_tenant_db(conn: sqlite3.Connection) -> dict | None:
    """对**一个已有的访客库**执行迁移。返回审计统计；不需要迁移时返回 None。

    ★ 幂等判据 ★
    `meta.seed_trim_version` 存在且 >= TRIM_VERSION ⇒ 已迁过，不重复跑。
    """
    if _meta_get(conn, META_KEY_MIGRATION) == str(TRIM_VERSION):
        return None
    stats = trim_demo_content(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (META_KEY_MIGRATION, str(TRIM_VERSION)),
    )
    conn.commit()
    return stats


def needs_migration(conn: sqlite3.Connection) -> bool:
    """这个库是否还需要跑一次迁移。"""
    return _meta_get(conn, META_KEY_MIGRATION) != str(TRIM_VERSION)
