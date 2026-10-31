#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/check_no_secrets.py` 的正反用例集。

★ 为什么每条用例一个独立目录 ★
放同一个目录里跑，命中会被**互相掩盖**（扫到第一个就退出，后面的用例根本没跑）。
一条一个目录，每条都单独起一次扫描器进程，结论互不干扰。

★ 退出码约定 ★
    0 = 全部用例符合预期
    1 = 有用例不符预期（该命中的没命中 / 该放过的被误报）
    2 = 本脚本自身崩了

用法：python tools/check_no_secrets_cases.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
SCANNER = REPO_ROOT / "tools" / "check_no_secrets.py"


def _k(*parts: str) -> str:
    """把密钥形状的假串**运行时拼**出来。

    ★ 为什么这么别扭地拆 ★
    仓库里不能留下任何**能匹配扫描器规则**的完整字面量 —— 否则扫描器
    扫仓库时会把自己的用例集判成泄露（自举式的死循环）。
    拆成几段拼起来，字面量在源码里就不是连续的，规则匹配不到。
    """
    return "".join(parts)


#: (用例名, 文件名, 文件内容, 期望是否命中)
#: 期望命中 = True → 扫描器必须返回非 0；False → 必须返回 0。
CASES: list[tuple[str, str, str, bool]] = [
    # ---------------- 必须命中（真出了网 / 真泄露了） ----------------
    ("火山方舟 ark-", "ark_key.txt",
     f'API_KEY = "{_k("ar", "k-", "A1b2C3d4E5f6G7h8")}"\n', True),
    ("OpenAI sk-", "openai_key.py",
     f'OPENAI_API_KEY = "{_k("s", "k-", "A1b2C3d4E5f6G7h8I9j0K1L2")}"\n', True),
    ("OpenAI 项目键 sk-proj-", "openai_project_key.txt",
     f'KEY = "{_k("s", "k-proj-", "A1b2C3d4E5f6G7h8I9j0K1L2m3")}"\n', True),
    ("Anthropic sk-ant-", "anthropic_key.js",
     f'const key = "{_k("s", "k-ant-", "A1b2C3d4E5f6G7h8I9j0K1L2")}";\n', True),
    # Google 的真实 key 是 `AIza` + **恰好 35 位** `[0-9A-Za-z_-]`。
    # ★ 这里的样本必须够长 ★
    #   规则 `AIza[0-9A-Za-z_\-]{35}` 是按真实格式写的；样本短一位就匹配不上。
    #   实测踩过：只给 34 位 → 扫描器 rc=0，看起来像"规则写错了"，
    #   其实是**样本不合格**。所以这里数好位数再写。
    ("Google AIza", "google_key.json",
     f'{{"key": "{_k("AI", "za", "A1b2C3d4E5f6G7h8I9j0K1L2m3N4o5P6q7R8")}"}}\n', True),
    ("GitHub token ghp_", "github_token.txt",
     f'TOKEN={_k("gh", "p_", "A1b2C3d4E5f6G7h8I9j0K1L2m3N4o5P6q7")}\n', True),
    ("Slack token xoxb-", "slack_token.txt",
     f'TOKEN={_k("xox", "b-", "1234567890-abcdefghijkl")}\n', True),
    ("AWS AKIA", "aws_key.txt",
     f'AWS_ACCESS_KEY_ID={_k("AK", "IA", "A1B2C3D4E5F6G7H8")}\n', True),
    ("通用赋值型（api_key = 长串）", "generic_secret.py",
     f'api_key = "{_k("A1b2C3d4E5f6G7h8", "I9j0K1L2m3N4o5P6")}"\n', True),
    # ★ 这条也必须拆开拼 ★
    #   `_URI_CREDENTIAL_RE` 匹配「scheme://user:pass@host」整串。
    #   上一版这里写的是完整字面量，结果**扫描器扫自己的用例集时命中了**
    #   （第 [12] 节断言「扫整个仓库 rc=0」直接 FAIL）。
    #   ⇒ 用例集自己必须满足闸门的判据，否则闸门永远红 —— 那等于闸门失效。
    ("带口令的数据库连接串", "db_uri.txt",
     'DSN = "' + _k("postgres://appuser:", "Sup3rS3cretP4ss",
                   "@db.internal:5432/appdb") + '"\n', True),
    ("PEM 私钥块（.pem）", "private_key.pem",
     f'{_k("-----BEGIN ", "RSA ", "PRIVATE KEY-----")}\n'
     'MIIEowIBAAKCAQEA\n'
     f'{_k("-----END ", "RSA ", "PRIVATE KEY-----")}\n', True),
    ("私钥容器（.key）", "server.key",
     f'{_k("-----BEGIN ", "RSA ", "PRIVATE KEY-----")}\n'
     'MIIEowIBAAKCAQEA\n'
     f'{_k("-----END ", "RSA ", "PRIVATE KEY-----")}\n', True),
    ("私钥容器（.p12）", "cert.p12",
     f'{_k("-----BEGIN ", "RSA ", "PRIVATE KEY-----")}\n'
     'MIIEowIBAAKCAQEA\n'
     f'{_k("-----END ", "RSA ", "PRIVATE KEY-----")}\n', True),
    ("私钥容器（.jks）", "store.jks",
     f'{_k("-----BEGIN ", "RSA ", "PRIVATE KEY-----")}\n'
     'MIIEowIBAAKCAQEA\n'
     f'{_k("-----END ", "RSA ", "PRIVATE KEY-----")}\n', True),
    ("无后缀文件里的私钥块", "no_suffix_secret",
     f'{_k("-----BEGIN ", "RSA ", "PRIVATE KEY-----")}\n'
     'MIIEowIBAAKCAQEA\n'
     f'{_k("-----END ", "RSA ", "PRIVATE KEY-----")}\n', True),

    # ---------------- 必须放过（历史上真出过误报的形态） ----------------
    ("含密钥前缀的普通词 cv-mark-text / task-list", "cv_mark.txt",
     'MARK_CLASS = "cv-mark-text"\n'
     'ITEMS = ["task-list", "disk-usage", "sk-secret-123"]\n', False),
    ("占位符 your-api-key / example / changeme", "placeholder.py",
     'API_KEY = "your-api-key-here"\n'
     'OTHER = "example"\n'
     'THIRD = "changeme"\n'
     'LONG = "your-api-key-here-placeholder-value"\n', False),
    ("dummy-test-key-123（短）", "dummy_key.txt",
     'api_key = "dummy-test-key-123"\n', False),
    ("sk- 开头但长度不足 20 位", "short_sk.py",
     'KEY = "sk-tooshort"\n'
     'KEY2 = "sk-abc123"\n', False),
    ("普通说明文档", "readme.md",
     '# 说明\n\n这是一个普通的说明文件，里面没有任何密钥。\n', False),
    ("普通代码", "normal_code.py",
     'def add(a, b):\n    return a + b\n\n'
     'API_URL = "https://api.example.com/v1"\n', False),
    ("普通 yaml", "config.yaml",
     'name: demo\nversion: 1\nendpoint: https://api.example.com/v1\n', False),
]


def run_case(name: str, filename: str, content: str, expect_hit: bool) -> bool:
    """跑一条用例，返回是否符合预期。"""
    case_dir = Path(tempfile.mkdtemp(prefix="enp-case-"))
    try:
        (case_dir / filename).write_text(content, encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [PY, str(SCANNER), str(case_dir / filename)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=str(REPO_ROOT), env=env)
        hit = proc.returncode != 0
        ok = (hit == expect_hit)
        mark = "OK  " if ok else "FAIL"
        flag = "命中" if hit else "未命中"
        want = "必须命中" if expect_hit else "必须放过"
        print(f"[{mark}] {name:<42} rc={proc.returncode}  {flag}（{want}）")
        if not ok:
            print(f"        输出：{(proc.stdout or '').strip()[:400]}")
            print(f"        stderr：{(proc.stderr or '').strip()[:200]}")
        return ok
    finally:
        shutil.rmtree(case_dir, ignore_errors=True)


def main() -> int:
    if not SCANNER.is_file():
        print(f"[FATAL] 找不到扫描器：{SCANNER}", file=sys.stderr)
        return 2
    print("=" * 74)
    print("  check_no_secrets.py 正反用例（每条独立目录，互不掩盖）")
    print("=" * 74)
    results = [(name, run_case(name, fn, content, expect))
               for name, fn, content, expect in CASES]
    passed = sum(1 for _n, ok in results if ok)
    print("-" * 74)
    print(f"用例：{passed} 符合预期 / {len(results) - passed} 不符（共 {len(results)} 条）")
    if passed != len(results):
        for name, ok in results:
            if not ok:
                print(f"   FAIL: {name}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                      # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("RESULT: TOOL CRASHED", file=sys.stderr)
        sys.exit(2)
