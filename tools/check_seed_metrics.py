# -*- coding: utf-8 -*-
"""种子语料的 6 项量化判据（C1~C6）—— 用**业务代码实跑**。

★ 为什么必须走业务代码 ★
这 6 项判据量的是「产品实际会怎么用这份语料」，不是「语料文件长什么样」。
所以每一条都必须调 `app/` 里真正的函数，否则测的就是「我以为的规则」：

  C1/C4 用 `articles.pick_sentences_for_term` —— 它的匹配是 **LIKE 粗筛 + `_term_pattern`
       变体精筛**（recoil 能命中 recoiling）。用严格词边界正则自己数会**漏算**
       （实测漏算把 C1 从 98.3% 误报成 70%）。
  C2 的池深 = Σmin(每词句数, 3)，其中「每词句数」就是 C1 的同一个命中数。
  C3 的语义是「UI 下拉框可选的阈值组合剩余词数」—— 阈值就是 `pr-min-imp` /
       `pr-min-use` 的 1~5 分选项，语义是 imp>=X **且** use>=Y。
  C5 的单位是**单篇英文词数**（不是「词条覆盖该篇的个数」）。
  C6 是常用标签两两 AND 筛选后每组 >= 1 篇。

★ 与 `tools/validate_seed.py` 的分工 ★
`validate_seed.py` 管**形态与红线**（切句对齐、版权来源、标签上限、幂等）；
本工具管**规模与可用性**（够不够练、筛不筛得动）。两者互补，都不可省。

退出码：0 = 六项全过；1 = 有项不达标；2 = 工具自身崩了。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# C1/C2/C3/C4/C5/C6 的阈值
# ★ 全部来自裁剪后的实测值（2026-10-24），依据见下方各处注释
C1_MIN_RATIO = 1.0           # ≥3 句的词占比（实测 5/5 = 100%）
C2_MIN_POOL = 15             # Σmin(句数,3)（实测 15）
# ★ C3 的语义已变（2026-10-24）★
#   原判据「每个阈值组合剩余词数 >= MAX_TERMS(8)」是 60 词库的口径，
#   裁到 5 词后不可能满足，也不是用户的要求（用户说「大约 5 个词」）。
#   新语义 = 「筛选太严时给人话提示，不静默返 0」，
#   落地成两条可执行判据（见下面 C3 段）：
#     ① 筛完至少剩 1 个
#     ② 筛空时抛可读错误，而不是静默返回一个空练习
C3_MIN_TERMS = 1             # 每个阈值组合筛选后至少剩 1 个词
C4_MIN_RATIO = 1.0           # 词↔文章可达率（实测 100%）
C5_MIN_WORDS, C5_MAX_WORDS = 300, 600   # 自撰单篇英文词数（实测 471）
C6_MIN_ARTICLES = 1          # 常用标签两两 AND 的结果篇数
UI_THRESHOLDS = (1, 2, 3, 4, 5)         # pr-min-imp / pr-min-use 的选项值


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def ok(self, cond: bool, label: str, detail: str = "") -> bool:
        if cond:
            self.passed += 1
            print(f"  [PASS] {label}" + (f"  ({detail})" if detail else ""))
        else:
            self.failed += 1
            print(f"  [FAIL] {label}" + (f"  ({detail})" if detail else ""))
        return bool(cond)

    def note(self, text: str) -> None:
        print(f"         {text}")

    def section(self, title: str) -> None:
        print()
        print(title)
        print("-" * 72)


def main() -> int:
    tmp_home = tempfile.mkdtemp(prefix="enp-seed-metrics-")
    os.environ["ENPRACTICE_HOME"] = tmp_home

    from app import articles, db, entries, practices, seed, seed_data

    c = Checker()
    db_file = Path(tmp_home) / "metrics.db"
    result = seed.seed_database(db_file)
    conn = db._open(db_file)
    try:
        arts = seed.all_articles()
        by_title = {a["title"]: a for a in arts}
        self_authored = [a for a in arts
                         if a["title"] != seed_data.PUBLIC_DOMAIN_ARTICLE["title"]]
        vocab = seed_data.VOCABULARY

        c.ok(bool(result["seeded"]), "M0 临时库首次灌入成功", str(result["stats"]))

        # ---- 每词的命中句数（C1/C2/C4 共用同一口径）----
        hits: dict[str, int] = {}
        for v in vocab:
            hits[v["term"]] = len(articles.pick_sentences_for_term(conn, v["term"], limit=999))

        # ---- C1 每词例句数 ----
        c.section("C1 · 每词例句数（pick_sentences_for_term 实跑）")
        ge3 = [t for t, n in hits.items() if n >= 3]
        ratio = len(ge3) / len(vocab)
        c.ok(ratio >= C1_MIN_RATIO,
             f"C1 每词>=3句的词占比 >= {C1_MIN_RATIO:.0%}",
             f"{len(ge3)}/{len(vocab)} = {ratio:.1%}")
        low = sorted((n, t) for t, n in hits.items() if n < 3)
        if low:
            c.note(f"不足 3 句的词（练习轮次会少，但不影响达标）：{low}")

        # ---- C2 练习池深 ----
        c.section("C2 · 练习池深")
        pool = sum(min(n, 3) for n in hits.values())
        c.ok(pool >= C2_MIN_POOL,
             f"C2 池深 Σmin(每词句数,3) >= {C2_MIN_POOL}", f"实际 {pool}")
        # ★ 每轮词数现在可自选（3/5/8/10/15/20，默认 8）★
        #   「一轮 8 词」在 5 词库里不成立 ⇒ 池深除数用**实际能练的轮数**。
        usable = min(practices.MAX_TERMS, len(vocab))
        c.note(f"一次练习 {usable} 词 x 3 句 = {usable * 3} 句 → "
               f"可不重复轮次 ≈ {pool / (usable * 3):.1f}")

        # ---- C3 评分阈值组合 ----
        # ★ 语义（2026-10-24 改）★
        #   「筛选太严时给人话提示，不静默返 0」——
        #   而不是旧口径「每个阈值组合都要剩 >= 8 个词」
        #   （那是 60 词库的口径；裁到 5 词后不可能满足，也不是用户的要求）。
        c.section("C3 · 评分阈值组合（筛选太严时要给人话提示，不静默返 0）")
        bad: list[tuple[int, int, int]] = []
        for x in UI_THRESHOLDS:
            for y in UI_THRESHOLDS:
                n = sum(1 for v in vocab
                        if v["importance"] >= x and v["usefulness"] >= y)
                if n < C3_MIN_TERMS:
                    bad.append((x, y, n))
        c.ok(not bad,
             f"C3 每个 UI 阈值组合筛选后至少剩 {C3_MIN_TERMS} 个词",
             f"共 {len(UI_THRESHOLDS) ** 2} 个组合，不达标 {len(bad)} 个"
             + (f"：{bad}" if bad else ""))

        # C3-② 筛不出词时必须抛**可读**错误，而不是静默返回一个空练习。
        # ★ 为什么要另造一个隔离的临时库来测 ★
        #   当前语料里 fluency 的评分是 (5,5) —— 任何 (x<=5, y<=5) 的阈值组合
        #   都会留下它 ⇒ 用正式种子库**永远触发不到**「筛空」这条路径。
        #   所以这里另建一个只含一个低分词条的临时库，把这条路径单独验出来。
        #   ★ 判据要调业务代码，不能自己写近似实现 ★
        probe_db = Path(tmp_home) / "probe_c3.db"
        pconn = db._open(probe_db)
        try:
            db._apply_schema(pconn)
            entries.create_entry(pconn, {
                "type": "word", "term": "lowrated", "pos": "n.",
                "meaning_zh": "低分词", "usage": "A low rated word.",
                "source": "probe", "importance": 1, "usefulness": 1,
            })
            pconn.commit()
            err_msg = ""
            try:
                practices._resolve_terms(pconn, [], min_importance=5.0,
                                         min_usefulness=5.0, max_terms=8)
            except Exception as exc:                   # noqa: BLE001
                err_msg = str(exc)
        finally:
            pconn.close()
        c.ok(bool(err_msg) and ("放宽" in err_msg or "评分" in err_msg),
             "C3-② 筛不出词时抛的是可读错误（不是静默返 0）",
             (err_msg[:56] + "…") if err_msg
             else "★ 没有抛错 ⇒ 可能静默返回了空练习")

        # ---- C4 词↔文章可达 ----
        c.section("C4 · 词↔文章可达（能出题）")
        missing = [v["term"] for v in vocab if hits[v["term"]] == 0]
        r4 = (len(vocab) - len(missing)) / len(vocab)
        c.ok(r4 >= C4_MIN_RATIO, "C4 每词都能取到例句", f"{r4:.1%}"
             + (f"，取不到：{missing}" if missing else ""))

        # ---- C5 单篇篇幅 ----
        c.section("C5 · 自撰单篇英文词数")
        bad5 = []
        for a in self_authored:
            n = len(" ".join(p[0] for p in a["pairs"]).split())
            if not (C5_MIN_WORDS <= n <= C5_MAX_WORDS):
                bad5.append((a["title"], n))
            c.note(f"《{a['title'][:40]:<42}》{n:>4} 词")
        c.ok(not bad5,
             f"C5 自撰单篇词数 ∈ [{C5_MIN_WORDS},{C5_MAX_WORDS}]",
             f"越界 {bad5}" if bad5 else f"{len(self_authored)} 篇全部达标")
        pub_words = len(" ".join(p[0] for p in seed_data.PUBLIC_DOMAIN_ARTICLE["pairs"]).split())
        c.note(f"公有领域选段 {pub_words} 词（标为「选段」，不纳入 C5 约束）")

        # ---- C6 标签组合 ----
        c.section("C6 · 常用标签两两 AND")
        cnt: dict[str, int] = {}
        for a in arts:
            for t in a["tags"]:
                cnt[t] = cnt.get(t, 0) + 1
        common = sorted(t for t, n in cnt.items() if n >= 2)
        c.note(f"常用标签(>=2篇) = {common}")
        # ★ 护栏：常用标签为空 ⇒ C6 会「无组可查 → 恒过」空转 ★
        #   2 篇口径下实测常用标签 = ['B1-B2', '英语学习']（非空），
        #   但将来语料一变可能变空 —— 空集时必须显式判 FAIL，
        #   不能让「没组可查」被当成「全部通过」。
        c.ok(bool(common), "C6 护栏：常用标签非空（否则 C6 会恒过空转）",
             f"常用标签 = {common}")
        zero = []
        for i in range(len(common)):
            for j in range(i + 1, len(common)):
                pair = {common[i], common[j]}
                n = sum(1 for a in arts if pair <= set(a["tags"]))
                if n < C6_MIN_ARTICLES:
                    zero.append((common[i], common[j], n))
        c.ok(not zero,
             f"C6 常用标签两两 AND 每组 >= {C6_MIN_ARTICLES} 篇",
             f"返 0 组数 {len(zero)}" + (f"：{zero}" if zero else ""))
    finally:
        conn.close()

    print()
    print("-" * 72)
    print(f"SEED METRICS PASS={c.passed} FAIL={c.failed}")
    if c.failed:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                      # noqa: BLE001 - 工具崩了必须与「断言不通过」区分
        import traceback
        traceback.print_exc()
        print("RESULT: TOOL CRASHED", file=sys.stderr)
        sys.exit(2)
