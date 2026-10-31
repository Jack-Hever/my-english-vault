#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""找一个「在词汇库里、但外刊库检索不到句子」的词。

★ 为什么需要它 ★
`POST /api/practices` 传 terms=["zebra"] 会 400 —— 因为 zebra 连词汇库
都没有（practices.py:191 `if not candidates: raise`）。
而 missing_terms 的语义是**目标词在词汇库里、外刊库没句子**。
而且要求是「至少命中一个」：全部 missing 会撞上 practices.py:329
`if not sentences: raise`，还是 400。

⇒ 必须传「一个能命中的 + 一个缺失的」混合列表。

用法：python tools/find_missing_term.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ["ENPRACTICE_HOME"] = tempfile.mkdtemp(prefix="enp-findterm-")
os.environ.pop("ENPRACTICE_MODE", None)
os.environ["ENPRACTICE_WEB_SEARCH"] = "0"     # 不许出网，纯查库

from app import articles, db  # noqa: E402
from app.seed import seed_database  # noqa: E402


def main() -> int:
    # ★ init_db() 只建 schema，不灌语料 ★
    # 所以查「有没有缺失词」必须先 seed_database —— 否则 entries 表是空的，
    # 探针会报「词条数 0」，看着像「语料没命中」，其实是压根没数据。
    # ★ 库名不能叫 data.db ★
    #   app/seed.py 的 _assert_not_real_db 会拒绝任何名字像用户真库的路径
    #   （%APPDATA%\EnPractice\data.db）—— 那道闸门是对的，别绕过它。
    #   这里用一个明显是临时的名字，既过闸门又不会跟真库混淆。
    db_path = Path(os.environ["ENPRACTICE_HOME"]) / "probe-tmp.db"
    stat = seed_database(db_path)
    print(f"[INFO] 灌种子：{stat}")
    conn = db._open(db_path)
    try:
        # 词汇库里全部词条
        rows = conn.execute(
            "SELECT term FROM entries ORDER BY term").fetchall()
        terms = [r[0] for r in rows]
        print(f"[INFO] 词汇库词条数：{len(terms)}")

        found, missing = [], []
        for t in terms:
            hit = articles.pick_sentences_for_terms(conn, [t])
            if hit and hit[0].get("found"):
                found.append(t)
            else:
                missing.append(t)
        print(f"[INFO] 有句子的：{len(found)}   没句子的：{len(missing)}")
        print(f"[INFO] 没句子的词（前 40 个）：{missing[:40]}")
        if not found:
            print("[FATAL] 没有一个有句子的词 —— 无法构造混合列表")
            return 2
        if not missing:
            print("[INFO] 没有缺失词（语料 100% 可达）—— 这次没法验 p-missing")
            return 0
        print(f"\n可用组合：terms={found[0]!r}（命中） + {missing[0]!r}（缺失）")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                           # noqa: BLE001
        import traceback
        traceback.print_exc()
        sys.exit(2)