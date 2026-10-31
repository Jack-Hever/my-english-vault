# -*- coding: utf-8 -*-
"""种子语料构建期校验器（tools/validate_seed.py）。

★ 它解决什么问题 ★
`app/seed_data.py` 里的 41 对句子、5 个词条是人手写的。它们同时被两条路消费：
  · 访客首屏看到的外刊库 / 词汇库；
  · 练习出题时的参考译文（`text_zh` 来自种子，所以不消耗 AI 调用）。
任何一处悄悄漂移 —— 某句多了一个句号、某个词在它引用的文章里根本不存在、
某篇的句对不是 1:1 —— 用户看到的都是「这个词没法出题」或者「译文对不上」。
本脚本把「它应该没问题」变成 25 条可执行断言，改完语料跑一遍就知道有没有踩雷。

★ 怎么跑 ★
    python tools/validate_seed.py            # 从仓库根目录跑
    python tools/validate_seed.py --verbose  # 顺便打印实测数字

★ 它不碰真实用户库 ★
全程在 `tempfile.mkdtemp()` 里建库，走的是 `app/seed.py` 的 `seed_database()`
—— 与云端 `cloud.ensure_tenant_db` 里调的是同一个 `seed_visitor()`，
所以验的就是线上真跑的那条路径。退出时目录整个删掉。

★ 为什么落库层（B 段）要真灌一遍 ★
「句对是 1:1 ⇒ DP 对齐必然 exact」是推理；B1~B4 是把这个推理**实跑**一遍。
中间隔着 `split_sentences` 的长度过滤、`_dp_align` 的代价函数、
`_insert_sentences` 的 seq 分配 —— 任何一环改了，推理就可能不再成立。
"""

from __future__ import annotations

import argparse
import ast
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import articles, db, seed  # noqa: E402
from app import seed_data           # noqa: E402

# ---------------------------------------------------------------------------
# 判据阈值（全部来自实测值，见文件末尾「实测基线」注释；改动请同步更新那里）
# ---------------------------------------------------------------------------
EXPECT_ARTICLE_COUNT = 2              # 1 篇自撰 + 1 篇公有领域
EXPECT_SELF_AUTHORED = 1
MIN_PAIRS_SELF = 20                   # 自撰篇每篇句对数下限（实测 26）
MIN_PAIRS_ANY = 12                    # 公有领域选段句对数下限（实测 15）
MIN_WORDS_SELF = 300                  # 自撰篇英文词数下限（实测 471）
MAX_WORDS_SELF = 600                  # 自撰篇英文词数上限（实测 471）
VOCAB_MIN, VOCAB_MAX = 5, 8           # 词条数区间（实测 5；用户说「大约 5 个」）
MIN_TAGS_PER_ARTICLE = 5              # S3 端到端判据：每篇 ≥5 个标签

#: 版权白名单：种子只允许这两种来源。
ALLOWED_SOURCE_NAMES = {"本项目原创", "Project Gutenberg（公有领域）"}

#: 真实媒体黑名单。命中即失败 —— 演示语料里出现第三方媒体内容是版权红线。
FORBIDDEN_MEDIA = (
    "Economist", "The Economist", "经济学人",
    "New York Times", "NYT", "纽约时报",
    "Esquire", "Psychology Today", "Sixth Tone", "山bay", "Shanbay", "端传媒",
    "Financial Times", "Guardian", "Reuters", "Bloomberg", "Nature",
)

#: 隐私 / 密钥样式。种子里一个都不该有。
_SECRET_PATTERNS = (
    ("邮箱", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("身份证号", re.compile(r"(?<!\d)\d{17}[\dXx](?!\w)")),
    ("ark- 密钥", re.compile(r"ark-[A-Za-z0-9_\-]{8,}")),
    ("sk- 密钥", re.compile(r"sk-[A-Za-z0-9_\-]{16,}")),
)


class Checker:
    """极简断言收集器：打印每条结果，末尾给总数与退出码。

    刻意不用 unittest —— 这是给人和 CI 看结果的一次性脚本，不是测试套件；
    真回归请写进 `test_*.py`（run_tests.sh 才会跑）。
    """

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.passed = 0
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def section(self, title: str) -> None:
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)

    def ok(self, cond: bool, label: str, detail: str = "") -> bool:
        """记一条断言。cond 为假 → FAIL；否则 PASS（detail 只在 verbose 时打印）。"""
        if cond:
            self.passed += 1
            print(f"  [PASS] {label}" + (f"  —— {detail}" if self.verbose and detail else ""))
        else:
            self.failures.append(label)
            print(f"  [FAIL] {label}" + (f"  —— {detail}" if detail else ""))
        return bool(cond)

    def warn(self, label: str, detail: str = "") -> None:
        """记一条提醒。**不算失败** —— 留给"已知取舍"用，别拿它当断言的橡皮擦。"""
        self.warnings.append(f"{label}{('：' + detail) if detail else ''}")
        print(f"  [WARN] {label}" + (f"  —— {detail}" if detail else ""))

    def note(self, text: str) -> None:
        if self.verbose:
            print(f"         · {text}")


# ---------------------------------------------------------------------------
# A 段：种子数据本身（不落库，静态 + 切句器实跑）
# ---------------------------------------------------------------------------


def code_string_literals(path: Path) -> list[str]:
    """取源码里所有字符串字面量，**排除模块 docstring**。

    为什么必须排除：`seed_data.py` 的文件头 docstring 里就写着「严禁把
    Economist / NYT / ... 放进本文件」——那是**禁止清单的说明文字**，不是数据。
    连注释一起扫就会把这行自己判成违规。用 ast 取值天然跳过 `#` 注释，
    再手动去掉模块 docstring，剩下的就是真正会被灌进库的内容。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    body = tree.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        doc = body[0].value.value
        out = [s for s in out if s is not doc]
    return out


def check_static(c: Checker) -> list[dict]:
    """A 段：文章与词条的静态形态、切句对齐前提、版权与隐私红线。"""
    arts = seed.all_articles()
    by_title = {a["title"]: a for a in arts}
    c.section("A 段 · 种子数据静态校验（不落库）")

    c.ok(len(arts) == EXPECT_ARTICLE_COUNT,
         f"A1 文章总数 == {EXPECT_ARTICLE_COUNT}", f"实际 {len(arts)}")
    c.ok(VOCAB_MIN <= len(seed_data.VOCABULARY) <= VOCAB_MAX,
         f"A2 词条总数 ∈ [{VOCAB_MIN}, {VOCAB_MAX}]", f"实际 {len(seed_data.VOCABULARY)}")

    self_authored = [a for a in arts if a["title"] != seed_data.PUBLIC_DOMAIN_ARTICLE["title"]]
    c.ok(len(self_authored) == EXPECT_SELF_AUTHORED,
         f"A3 自撰文章 {EXPECT_SELF_AUTHORED} 篇", f"实际 {len(self_authored)}")

    # A4/A5/A6：句对形态与「1:1 ⇒ exact」的可执行前提
    pairs_bad: list[str] = []
    min_pairs_self = min(len(a["pairs"]) for a in self_authored)
    min_pairs_any = min(len(a["pairs"]) for a in arts)
    word_counts: dict[str, int] = {}
    for a in arts:
        for i, pair in enumerate(a["pairs"], start=1):
            en, zh = pair[0], pair[1]
            if not en.rstrip().endswith((".", "!", "?")):
                pairs_bad.append(f"{a['title']} 第{i}句 英文结尾={en[-12:]!r}")
            if not zh.rstrip().endswith("。"):
                pairs_bad.append(f"{a['title']} 第{i}句 中文结尾={zh[-12:]!r}")
    c.ok(not pairs_bad,
         "A4 每句英文以 .!? 结尾、中文以 。 结尾", f"违规 {len(pairs_bad)} 处：{pairs_bad[:3]}")
    c.ok(min_pairs_self >= MIN_PAIRS_SELF and min_pairs_any >= MIN_PAIRS_ANY,
         f"A5 每篇句对数 ≥ {MIN_PAIRS_SELF}（自撰）/ ≥ {MIN_PAIRS_ANY}（全部）",
         f"自撰最少 {min_pairs_self}、全部最少 {min_pairs_any}")

    for a in arts:
        en = " ".join(p[0] for p in a["pairs"])
        word_counts[a["title"]] = len(en.split())
    bad_words = {t: n for t, n in word_counts.items()
                 if t in by_title and by_title[t]["title"] != seed_data.PUBLIC_DOMAIN_ARTICLE["title"]
                 and not (MIN_WORDS_SELF <= n <= MAX_WORDS_SELF)}
    c.ok(not bad_words,
         f"A6 自撰文章英文词数 ∈ [{MIN_WORDS_SELF}, {MAX_WORDS_SELF}]",
         f"越界 {bad_words}")

    split_bad: list[str] = []
    align_bad: list[str] = []
    for a in arts:
        pairs = a["pairs"]
        en = " ".join(p[0] for p in pairs)
        zh = "".join(p[1] for p in pairs)
        en_s = articles.split_sentences(en)
        zh_s = articles.split_zh_sentences(zh)
        if len(en_s) != len(pairs) or len(zh_s) != len(pairs):
            split_bad.append(f"{a['title']} pairs={len(pairs)} en={len(en_s)} zh={len(zh_s)}")
        _aligned, mode, merged, _strict = articles._align_detail(en_s, zh)
        if mode != "exact" or merged != 0:
            align_bad.append(f"{a['title']} mode={mode} merged={merged}")
        c.note(f"{a['title'][:38]:<40} pairs={len(pairs):>3} words={word_counts[a['title']]:>4} "
               f"split(en/zh)={len(en_s)}/{len(zh_s)} mode={mode} merged={merged}")
    c.ok(not split_bad,
         "A7 split_sentences / split_zh_sentences 句数 == len(pairs)（对齐前提）",
         f"不一致：{split_bad}")
    c.ok(not align_bad,
         "A8 _align_detail 实跑 → mode 全 exact、merged 全 0",
         f"异常：{align_bad}")

    # A9/A10 来源与版权
    sources = {a["source_name"] for a in arts}
    c.ok(sources <= ALLOWED_SOURCE_NAMES,
         f"A9 source_name ⊆ {sorted(ALLOWED_SOURCE_NAMES)}", f"实际 {sorted(sources)}")
    pd = seed_data.PUBLIC_DOMAIN_ARTICLE
    c.ok("gutenberg.org" in (pd.get("url") or "").lower()
         and "Gutenberg" in pd["source_name"],
         "A10 公有领域篇标注明确（source_name 含 Gutenberg 且 url 指向 gutenberg.org）",
         f"source_name={pd['source_name']!r} url={pd.get('url')!r}")

    # A11 评分区间
    rating_bad = [f"{v['term']}({v['importance']}/{v['usefulness']})"
                  for v in seed_data.VOCABULARY
                  if not (0 <= v["importance"] <= 5 and 0 <= v["usefulness"] <= 5)
                  or (v["importance"] * 2) != int(v["importance"] * 2)
                  or (v["usefulness"] * 2) != int(v["usefulness"] * 2)]
    c.ok(not rating_bad, "A11 importance/usefulness ∈ [0,5] 且是 0.5 的倍数",
         f"异常 {len(rating_bad)} 条：{rating_bad[:5]}")

    # A12 词条引用的文章存在 + usage 逐字命中（A 段静态版，B6 是落库后版本）
    orphan = [v["term"] for v in seed_data.VOCABULARY if v["article"] not in by_title]
    c.ok(not orphan, "A12 每个词条的 article 字段指向存在的标题", f"孤儿词条 {orphan}")
    not_literal = [v["term"] for v in seed_data.VOCABULARY
                   if v["article"] in by_title
                   and v["usage"] not in " ".join(p[0] for p in by_title[v["article"]]["pairs"])]
    c.ok(not not_literal, "A13 usage 逐字出现在它引用文章的正文里", f"未命中 {not_literal}")

    # A14 per-word tags 是「校验元数据」：必须与引用文章标签有交集，否则它就是死数据
    tagless = [v["term"] for v in seed_data.VOCABULARY if not (v.get("tags") or [])]
    c.ok(not tagless, "A14 每个词条都带 tags（词条级标签当前只作校验元数据，不落库）",
         f"缺 tags：{tagless}")
    no_overlap = []
    for v in seed_data.VOCABULARY:
        a = by_title.get(v["article"])
        if a is None:
            continue
        if not (set(v.get("tags") or []) & set(a.get("tags") or [])):
            no_overlap.append(v["term"])
    c.ok(not no_overlap, "A15 词条 tags 与引用文章标签有交集（校验元数据不是死字段）",
         f"无交集 {no_overlap}")

    # A16/A17 版权黑名单与隐私/密钥样式：扫**会落库的数据**，不扫文件注释
    literals = code_string_literals(REPO_ROOT / "app" / "seed_data.py")
    media_hits: list[str] = []
    for s in literals:
        for name in FORBIDDEN_MEDIA:
            if name in s:
                media_hits.append(f"{name} @ {s[:40]!r}")
    c.ok(not media_hits, "A16 无真实媒体名（Economist/NYT/Esquire/…）", f"命中 {media_hits[:5]}")

    secret_hits: list[str] = []
    for s in literals:
        for kind, pattern in _SECRET_PATTERNS:
            m = pattern.search(s)
            if m:
                secret_hits.append(f"{kind}: {m.group(0)!r} @ {s[:40]!r}")
    c.ok(not secret_hits, "A17 无邮箱/手机号/身份证/ark-/sk- 样式", f"命中 {secret_hits[:5]}")
    return arts


# ---------------------------------------------------------------------------
# B 段：真灌一遍临时库（走线上同一条 seed_visitor 路径）
# ---------------------------------------------------------------------------


def check_seeded(c: Checker, arts: list[dict], db_file: Path) -> None:
    c.section("B 段 · 落库校验（临时库，走 app/seed.py 的真实路径）")

    result = seed.seed_database(db_file)
    c.ok(result["seeded"], "B0 seed_database 首次执行返回 True", f"{result['stats']}")

    conn = db._open(db_file)
    try:
        # ---- B1 对齐模式（team-lead 判据①）----
        modes: list[str] = []
        merged_bad: list[str] = []
        for a in arts:
            en = " ".join(p[0] for p in a["pairs"])
            zh = "".join(p[1] for p in a["pairs"])
            en_s = articles.split_sentences(en)
            _al, mode, merged, _st = articles._align_detail(en_s, zh)
            modes.append(mode)
            if merged:
                merged_bad.append(f"{a['title']} merged={merged}")
        c.ok(set(modes) == {"exact"}, "B1 每篇 align_mode 全为 exact", f"实际 {sorted(set(modes))}")
        c.ok(not merged_bad, "B2 每篇 merged 全为 0", f"异常 {merged_bad}")

        # ---- B3 句数一致（team-lead 判据⑤）----
        count_bad: list[str] = []
        for a in arts:
            row = conn.execute(
                "SELECT COUNT(s.id) n FROM articles a "
                "JOIN article_sentences s ON s.article_id = a.id WHERE a.title = ?",
                (a["title"],),
            ).fetchone()
            if row["n"] != len(a["pairs"]):
                count_bad.append(f"{a['title']} db={row['n']} pairs={len(a['pairs'])}")
        c.ok(not count_bad, "B3 每篇句子数 == len(pairs)", f"不一致 {count_bad}")

        # ---- B4/B5 译文非空 + 逐句等于种子中文句（team-lead 判据②）----
        empty = conn.execute(
            "SELECT COUNT(*) FROM article_sentences WHERE text_zh IS NULL OR TRIM(text_zh) = ''"
        ).fetchone()[0]
        c.ok(empty == 0, "B4 article_sentences.text_zh 非空率 100%", f"空行 {empty}")
        mismatch: list[str] = []
        for a in arts:
            rows = conn.execute(
                "SELECT s.seq, s.text, s.text_zh FROM article_sentences s "
                "JOIN articles a ON a.id = s.article_id WHERE a.title = ? ORDER BY s.seq",
                (a["title"],),
            ).fetchall()
            if len(rows) != len(a["pairs"]):
                mismatch.append(f"{a['title']} 行数 {len(rows)} != pairs {len(a['pairs'])}")
                continue
            for r, (en, zh) in zip(rows, a["pairs"]):
                if r["text"] != en:
                    mismatch.append(f"{a['title']}#{r['seq']} 英文不符")
                elif (r["text_zh"] or "") != zh:
                    mismatch.append(f"{a['title']}#{r['seq']} 译文不符 {r['text_zh']!r}")
        c.ok(not mismatch, "B5 每句 text_zh 逐句等于种子中文句", f"不符 {len(mismatch)} 处：{mismatch[:3]}")

        # ---- B6 usage 逐字（team-lead 判据③）----
        by_title = {a["title"]: a for a in arts}
        stored = {r["term"]: r for r in conn.execute(
            "SELECT id, term, usage, source FROM entries").fetchall()}
        not_literal, not_one_sentence = [], []
        for v in seed_data.VOCABULARY:
            row = stored.get(v["term"])
            if row is None:
                not_literal.append(f"{v['term']}(未入库)")
                continue
            if row["usage"] != v["usage"]:
                not_literal.append(f"{v['term']}(落库 usage 与种子不同)")
            sent_set = set(p[0] for p in by_title[v["article"]]["pairs"])
            if row["usage"] not in sent_set:
                not_one_sentence.append(v["term"])
        c.ok(not not_literal, "B6 每个词条的 usage 逐字出现在其 article 正文里",
             f"异常 {not_literal[:5]}")
        c.ok(not not_one_sentence, "B7 usage 恰好是引用文章里的一整句（无截断）",
             f"非整句 {not_one_sentence}")

        # ---- B8 逐词可达（team-lead 判据④）----
        missing = [v["term"] for v in seed_data.VOCABULARY
                   if not articles.pick_sentences_for_term(conn, v["term"], limit=5)]
        rate = (len(seed_data.VOCABULARY) - len(missing)) / len(seed_data.VOCABULARY) * 100
        c.ok(not missing, f"B8 每词 pick_sentences_for_term 可达率 100%（实测 {rate:.1f}%）",
             f"取不到：{missing}")

        # ---- B9/B10 复习状态与节点播种 ----
        n_entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        n_review = conn.execute(
            "SELECT COUNT(*) FROM review_state WHERE entry_id IN (SELECT id FROM entries)"
        ).fetchone()[0]
        n_nodes = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind = 'entry' AND entry_id IS NOT NULL"
        ).fetchone()[0]
        c.ok(n_entries == len(seed_data.VOCABULARY),
             "B9 词条数与种子一致", f"db={n_entries} 种子={len(seed_data.VOCABULARY)}")
        c.ok(n_review == n_entries, "B10 review_state 行数 == 词条数（自动进复习队列）",
             f"review={n_review} entries={n_entries}")
        c.ok(n_nodes == n_entries, "B11 nodes(kind='entry') 行数 == 词条数（自动进层级结构）",
             f"nodes={n_nodes} entries={n_entries}")

        # ---- B12 标签（S3 端到端判据：每篇 ≥5、总数 ≥5、且不超业务上限）----
        tag_rows = conn.execute(
            "SELECT a.title, COUNT(t.tag_id) n FROM articles a "
            "LEFT JOIN article_tags t ON t.article_id = a.id GROUP BY a.id"
        ).fetchall()
        low = [f"{r['title']}({r['n']})" for r in tag_rows if r["n"] < MIN_TAGS_PER_ARTICLE]
        over = [f"{r['title']}({r['n']})" for r in tag_rows
                if r["n"] > articles.MAX_TAGS_PER_ARTICLE]
        c.ok(not low, f"B12 每篇文章标签数 ≥ {MIN_TAGS_PER_ARTICLE}", f"不足：{low}")
        c.ok(not over, f"B13 每篇文章标签数 ≤ {articles.MAX_TAGS_PER_ARTICLE}（业务上限）",
             f"超限：{over}")
        total_tags = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        c.ok(total_tags >= MIN_TAGS_PER_ARTICLE,
             f"B14 去重后标签总数 ≥ {MIN_TAGS_PER_ARTICLE}", f"实际 {total_tags}")

        # ---- B15 幂等判据落库 ----
        row = conn.execute("SELECT value FROM meta WHERE key = ?",
                           (seed.META_KEY_SEED_VERSION,)).fetchone()
        c.ok(row is not None and row["value"] == str(seed.SEED_VERSION),
             "B15 meta.seed_version == SEED_VERSION（幂等判据已落库）",
             f"实际 {None if row is None else row['value']} 期望 {seed.SEED_VERSION}")

        # ---- B16 重复执行幂等 ----
        before = _row_counts(conn)
    finally:
        conn.close()

    again = seed.seed_database(db_file)
    conn = db._open(db_file)
    try:
        after = _row_counts(conn)
    finally:
        conn.close()
    c.ok(again["seeded"] is False, "B16 重复执行 seed_database 返回 False（幂等）",
         f"实际 {again['seeded']}")
    c.ok(before == after, "B17 重复执行后各表行数不变（只新增不删除）",
         f"{before} vs {after}")

    # ---- B18/B19 灌库路径零裁剪：写进去的关联数必须等于种子声明的标签总数 ----
    #   这两条是给「有人在灌库路径上悄悄裁标签」设的哨兵：
    #   一旦 app/seed.py 重新引入静默裁剪，tag_total 就会少于种子声明值。
    #   注意统计取**首次**灌入的那次（result）—— 第二次是幂等跳过，没有 articles 段。
    declared = sum(len(articles.normalize_tag_names(a.get("tags"))) for a in arts)
    written = int(result["stats"].get("articles", {}).get("tag_total", -1))
    c.ok(written == declared,
         "B18 灌入的标签关联数 == 种子声明的标签总数（灌库路径零裁剪）",
         f"种子声明 {declared} 个，灌入统计 {written} 个")
    c.ok("dropped_tags" not in result["stats"].get("articles", {}),
         "B19 seed_articles 的统计里已不存在 dropped_tags（裁剪机制已移除）",
         sorted(result["stats"].get("articles", {}).keys()))


def _row_counts(conn) -> dict:
    """落库行数快照（幂等对比用）。"""
    out = {}
    for table in ("articles", "article_sentences", "entries", "review_state", "nodes", "tags"):
        out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return out


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """跑完整校验。

    ★ 退出码契约（三档，含义互不重叠）★
      · 0 = 全部断言通过
      · 1 = 校验跑完了，但有断言不通过（FAIL >= 1）
      · 2 = **校验器自己崩了**（代码 bug / 环境缺失 / 语料把代码搞炸）

    ★ 为什么必须把「崩了」单独占一档 ★
    未捕获异常时 Python 也会给非 0，但那是**异常类型的退出码**，和 1 混在一起，
    调用方分不清「语料有问题」和「工具本身坏了」。更糟的是——如果外面套了管道
    （`python tools/validate_seed.py | tail`），`$?` 拿到的是 **tail 的退出码 0**，
    崩溃会被当成通过。所以这里显式 catch、显式打印、显式 exit(2)。
    """
    try:
        return _run(argv)
    except Exception:                          # noqa: BLE001 - 兜底要有兜底
        print()
        print("=" * 78)
        print("[FATAL] 校验器自身异常终止 —— 这不是「校验不通过」，是工具坏了。")
        print("=" * 78)
        traceback.print_exc()
        return 2


def _run(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(description="校验 app/seed_data.py 的演示语料与灌入结果")
    parser.add_argument("--verbose", action="store_true", help="打印每篇文章的实测数字")
    args = parser.parse_args(argv)

    print("种子语料校验器 · repo =", REPO_ROOT)
    print("数据源 = app/seed_data.py   灌入路径 = app/seed.py:seed_database（= 云端同一路径）")

    c = Checker(verbose=args.verbose)
    arts = check_static(c)

    tmp_dir = Path(tempfile.mkdtemp(prefix="enp-validate-seed-"))
    try:
        check_seeded(c, arts, tmp_dir / "data.db")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print()
    print("=" * 78)
    print(f"SEED CHECK  PASS={c.passed}  FAIL={len(c.failures)}  WARN={len(c.warnings)}")
    if c.failures:
        for f in c.failures:
            print(f"  · {f}")
    for w in c.warnings:
        print(f"  [WARN] {w}")
    print("=" * 78)
    print("实测基线（seed_data.py，2026-10-24 裁剪后）：\n"
      "      2 篇 / 41 句对 / 5 词条 / 自撰 26 句·471 词 /\n"
      "      公有领域 15 句·308 词 / 每篇 5 个标签·去重 8 个（已卡满 MAX_TAGS_PER_ARTICLE）\n"
      "      练习池深 15 句（每词 3 句）≈ 3 轮不重复")
    return 1 if c.failures else 0


if __name__ == "__main__":
    # 外层再兜一层：main() 内部只兜住了它自己执行期间的异常；万一模块级/更外层的
    # 东西炸了（导入失败、参数拼装崩了），这里保证仍然是 2 而不是"看起来像 1"。
    try:
        _rc = main()
    except Exception:                          # noqa: BLE001
        traceback.print_exc()
        _rc = 2
    raise SystemExit(_rc)