# -*- coding: utf-8 -*-
"""诊断：两种「代码指纹」实现是否等价？

背景：test_cloud_security.py 用单循环剥注释，tools/recount_ratchet_baseline.py
用两阶段剥注释，基线值是后者算的。若两者不等价 ⇒ 测试拿单循环算出来的值
去比两阶段算出来的基线 ⇒ 恒假红（判据自己制造假红）。

ast.walk 是 BFS：Expr(Constant(str)) 先于其子 Constant 被访问。
单循环里第二个 if 把 docstring 设成 "__DOC__"，随后 walk 到该 Constant 时
第一个 if 又把它覆盖成 "__STR__" ⇒ docstring 最终是 "__STR__"。
两阶段里循环1 先把所有 Constant 设 "__STR__"，循环2 再把 Expr 的值设 "__DOC__"
⇒ docstring 最终是 "__DOC__"。两者 ast.dump 不同 ⇒ md5 不同。

本脚本打印两种实现对同一文件的指纹，并逐条比对差异节点。
"""
import ast
import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FILES = ["app/articles.py", "app/websearch.py"]


def fp_single(path: Path) -> str:
    """test_cloud_security.py 里的实现：单循环。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = "__STR__"
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            node.value.value = "__DOC__"
    return hashlib.md5(ast.dump(tree).encode("utf-8")).hexdigest()


def fp_two_stage(path: Path) -> str:
    """tools/recount_ratchet_baseline.py 里的实现：两阶段。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = "__STR__"
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            node.value.value = "__DOC__"
    return hashlib.md5(ast.dump(tree).encode("utf-8")).hexdigest()


def docstring_values(path: Path, impl):
    """把该实现处理后仍以 __DOC__ 标记的 docstring 个数打印出来。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    if impl == "single":
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                node.value = "__STR__"
            if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                node.value.value = "__DOC__"
    else:
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                node.value = "__STR__"
        for node in ast.walk(tree):
            if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                node.value.value = "__DOC__"
    docs = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and node.value.value == "__DOC__"):
            docs.append(node.value)
    strs = sum(1 for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and n.value == "__STR__")
    return len(docs), strs


def main() -> int:
    print("=== 两种实现的指纹对比 ===")
    for rel in FILES:
        p = REPO / rel
        a = fp_single(p)
        b = fp_two_stage(p)
        nd_s, ns_s = docstring_values(p, "single")
        nd_t, ns_t = docstring_values(p, "two")
        print("%-22s 单循环=%s  两阶段=%s  %s"
              % (rel, a, b, "同" if a == b else "★不同★"))
        print("%-22s   __DOC__ 个数: 单循环=%d / 两阶段=%d   __STR__ 个数: 单循环=%d / 两阶段=%d"
              % ("", nd_s, nd_t, ns_s, ns_t))
    print()
    print("=== 结论 ===")
    same = all(fp_single(REPO / r) == fp_two_stage(REPO / r) for r in FILES)
    print("两种实现等价" if same else
          "★ 两种实现不等价 ⇒ 测试拿「单循环指纹」比「两阶段基线」必然 FAIL（判据假红）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
