# -*- coding: utf-8 -*-
"""★ 唯一的「剥离注释后的代码指纹」实现（两处必须共用本模块）★

★ 为什么必须抽成唯一模块 ★
ratchet 基线值是「两阶段」算法算出来的，而测试里曾经是「单循环」写法，
两者的 ast.dump 完全不同（docstring 最终标记一个是 __DOC__、一个是 __STR__）
⇒ 拿单循环指纹比两阶段基线，**永远 FAIL**，判据自己制造假红。
这类「同一口径两份实现」的漂移必须从结构上根除，不能靠人记住对齐。

★ 口径定义（两阶段，顺序不可交换）★
  阶段1：所有 ast.Constant(str) 的值 → "__STR__"
  阶段2：所有 Expr(Constant(str))（即 docstring 位置）的值 → "__DOC__"

★ 为什么必须两阶段、不能合成一个循环 ★
ast.walk 是 BFS：Expr 先于其子 Constant 被访问。
单循环里顺序是「Expr → 置 __DOC__」然后「Constant → 置 __STR__」，
后者覆盖前者 ⇒ docstring 最终全是 __STR__（实测 articles.py 有 45 处 docstring
被错标，__DOC__ 个数为 0）。
两阶段把「全部置 __STR__」和「再把 docstring 位置改回 __DOC__」分开，
语义才稳定。

★ 指纹守什么、不守什么 ★
  · 守：出网点所在的**可执行代码**没被改（注释/docstring/字符串字面量改动不算）。
  · 不守：注释与 docstring 的文字本身。
  · 配套的 AST 级出网点清单守卫（test_cloud_security.py 里的 EXPECTED_EGRESS）
    才是真正守住出网点的闸门，它与本指纹互补、互不替代。

用法：
    from code_fingerprint import code_fingerprint, strip_comments_and_docstrings
    md5 = code_fingerprint(Path("app/websearch.py"))
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

__all__ = ["strip_comments_and_docstrings", "code_fingerprint", "fingerprint_text"]


def strip_comments_and_docstrings(src: str) -> str:
    """把源码里的注释/docstring/字符串字面量换成占位，返回可执行代码的 ast.dump。

    ★ 用 ast 而不是正则 ★
    正则无法区分「字符串里的 //」和真注释，也无法可靠识别 docstring。
    """
    tree = ast.parse(src)
    # 阶段1：所有字符串常量 → __STR__
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = "__STR__"
    # 阶段2：docstring 位置 → __DOC__（必须放在阶段1之后）
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            node.value.value = "__DOC__"
    return ast.dump(tree)


def fingerprint_text(src: str) -> str:
    """对源码**文本**求代码指纹。"""
    return hashlib.md5(strip_comments_and_docstrings(src).encode("utf-8")).hexdigest()


def code_fingerprint(path: Path) -> str:
    """对文件求代码指纹（read_text 会做 CRLF→LF 归一，不影响 ast 指纹）。"""
    return fingerprint_text(Path(path).read_text(encoding="utf-8"))
