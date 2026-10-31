# -*- coding: utf-8 -*-
"""统计每个测试套件的**自报断言条数**（不是 [PASS] 行数）。

为什么要另算一遍：run_regression.py 数的是输出里的 [PASS] 标记，而部分套件
的 check() 会在过程中打印一次、结尾总结再打印一次 —— 标记行数约为真实断言数的
两倍。对外展示的「断言 N 项」必须是真实条数，所以这里解析各套件自报的总计。

用法： python tools/count_assertions.py [desktop|all]
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
PY = sys.executable

CLOUD_SUITES = {
    "test_cloud_tenant.py", "test_seed.py", "test_cloud_security.py",
    "test_seed_migrate.py", "test_sharecode_rewrite.py",
}

PATTERNS = [
    re.compile(r"总计：[^\n]*?（(\d+)\s*项）"),
    re.compile(r"总计：[^\n]*?共\s*(\d+)\s*项"),
    re.compile(r"TOTAL\s*=\s*(\d+)"),
    # test_ai_ratings.py 的收尾格式：「结果：168 通过 / 0 失败」
    re.compile(r"结果：(\d+)\s*通过\s*/\s*(\d+)\s*失败"),
    re.compile(r"PASS\s*=\s*(\d+)[^\n]*?FAIL\s*=\s*(\d+)[^\n]*?TOTAL\s*=\s*(\d+)"),
]


def self_reported(out: str):
    for i, pat in enumerate(PATTERNS[:3]):
        m = pat.search(out)
        if m:
            return int(m.group(1)), None
    m = PATTERNS[3].search(out)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = PATTERNS[4].search(out)
    if m:
        return int(m.group(3)), int(m.group(2))
    return None, None


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "desktop"
    suites = sorted(f for f in os.listdir(TESTS)
                    if f.startswith("test_") and f.endswith(".py"))
    tmp = tempfile.mkdtemp(prefix="enp-count-")
    total_assert = 0
    total_fail = 0
    rows = []
    for suite in suites:
        if mode == "desktop" and suite in CLOUD_SUITES:
            continue
        env = dict(os.environ, ENPRACTICE_HOME=os.path.join(tmp, suite[:-3]))
        proc = subprocess.run([PY, suite], cwd=TESTS, env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        out = (proc.stdout or "") + (proc.stderr or "")
        n, fails = self_reported(out)
        if n is None:
            rows.append((suite, "?", out.count("[PASS]"), "无法解析自报总计"))
            continue
        fails = out.count("[FAIL]") if fails is None else fails
        total_assert += n
        total_fail += fails
        rows.append((suite, n, out.count("[PASS]"), f"{fails} FAIL"))

    print(f"{'套件':32} {'自报断言':>8} {'[PASS]行':>8}   失败")
    for r in rows:
        print(f"{r[0]:32} {str(r[1]):>8} {str(r[2]):>8}   {r[3]}")
    print("-" * 66)
    print(f"模式 {mode}　套件 {len(rows)}　真实断言 {total_assert}　失败 {total_fail}")
    print(f"临时库 {tmp}（真实库 %APPDATA%\\EnPractice 未触碰）")


if __name__ == "__main__":
    main()
