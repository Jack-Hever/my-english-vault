# -*- coding: utf-8 -*-
"""按小节标题定位 + 按行替换，更新三份文档里的演示语料数字。

★ 为什么用脚本而不是 Edit 工具 ★
`docs/submission_draft.md` 与 `docs/去AI版_品牌文案_学英语的人手里常有两.txt`
的正文里有大量中文与全角标点，Edit 工具在这两个文件上反复报「字符串不匹配」
（内容其实逐字一致、行尾纯 LF、无 BOM）。改用 Python 按行号/小节定位，
每处替换都断言「命中且只命中一次」，命中数不对就直接抛错 —— 宁可失败，
也不能悄悄改错地方。

用法：python tools/_sync_docs_numbers.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: (相对路径, 行号(1-based), 期望旧内容片段, 新内容)
#: 行号是**当前**文件的行号；跑之前会用片段校验，片段对不上就报错。
EDITS: list[tuple[str, int, str, str]] = [
    # ---- docs/cloud_web_design_v2.md ----
    # §4.1 的实测代码块里那句「少于就报筛选太严」与代码不符（代码里只有
    # 0 个候选才抛）。那是**历史实测记录**，数字不该改，只补一句更正注记。
    (
        "docs/cloud_web_design_v2.md", 728,
        "评分筛选后剩余词数（MAX_TERMS=8，少于就报\"筛选太严\"）：",
        "评分筛选后剩余词数（★ 2026-10-24 更正：这句写「少于就报筛选太严」与代码不符 —— "
        "`app/practices.py` 是 `if not candidates:`，**只有 0 个候选**才抛，"
        "有 1~N 个时按实际数量给、不报错；且每轮词数现已可配 3/5/8/10/15/20）：",
    ),
    # 文件头：本文档是历史设计文档，正文的「7 篇 / 60 词 / 171 句对」是当时的规模，
    # 现已裁剪 —— 加注记说明，保留原数字作历史依据。
    (
        "docs/cloud_web_design_v2.md", 3,
        "> 修订依据：**已实跑 `app/seed_data.py` 的全部 7 篇文章 + 29 个词条**，",
        "> ⚠️ **2026-10-24 注记**：本文档是**历史设计文档**。正文里的「7 篇 / 60 词 / 171 句对」\n"
        "> 是当时的规模，**现已裁剪为「2 篇 / 5 词 / 41 句对」**；判据阈值也已按新规模重推\n"
        "> （C1 100% / C2 池深 15 / C3 新语义「筛选太严给人话提示，不静默返 0」/ C6 加了\n"
        "> 「常用标签非空且组合数 ≥1」的护栏 —— 见 `tools/check_seed_metrics.py`）。\n"
        "> 原数字保留作历史依据。\n"
        "> 修订依据：**已实跑 `app/seed_data.py` 的全部 7 篇文章 + 29 个词条**，",
    ),
]


def apply() -> int:
    # 按文件分组，保持文件内行号降序替换（避免前面的替换让后面的行号漂移）
    by_file: dict[str, list[tuple[int, str, str]]] = {}
    for rel, lineno, expect, new in EDITS:
        by_file.setdefault(rel, []).append((lineno, expect, new))

    total = 0
    for rel, items in by_file.items():
        path = REPO / rel
        lines = path.read_text(encoding="utf-8").split("\n")
        for lineno, expect, new in sorted(items, key=lambda x: -x[0]):
            idx = lineno - 1
            if idx < 0 or idx >= len(lines):
                print(f"[FAIL] {rel}:{lineno} 行号越界（文件只有 {len(lines)} 行）")
                return 1
            actual = lines[idx]
            if expect not in actual:
                print(f"[FAIL] {rel}:{lineno} 片段对不上")
                print(f"       期望包含：{expect[:70]}")
                print(f"       实际是  ：{actual[:70]}")
                return 1
            lines[idx] = actual.replace(expect, new)
            total += 1
            print(f"[OK] {rel}:{lineno} 已替换")
        path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n共替换 {total} 处")
    return 0


if __name__ == "__main__":
    sys.exit(apply())
