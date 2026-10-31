#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仓库密钥扫描器 —— **闸门**，不是警告器。

★ 为什么必须是「闸门」而不是「提示」 ★
云端部署产物（deploy/）或仓库里一旦混进一把**真实**的 API 密钥，
它就会跟着 git 仓库、评审截图、投稿材料一起流出去。密钥泄露的代价不是
「改一下就好」，而是「必须轮换」。所以这里的约定是：

    命中 → 退出码非 0 → 构建中断。

★ 误报这个坑（实测踩过） ★
朴素的 `grep -rl "ark-"` 会把 `cv-mark-text` 这类类名片段判成命中 ——
因为「m**ark-**text」里就含「ark-」。同理「ta**sk-**list」含「sk-」。
所以本文件所有规则都带**负向前瞻**：

    (?<![A-Za-z0-9_])ark-...

左边必须是非标识符字符（或行首）才算命中。这条前瞻是把上面两个反例
钉死的关键，也是本文件与 `grep` 的本质差别。

★ 扫描范围 ★
默认扫仓库根目录（deploy/ 在仓库内，一并覆盖）；deploy/ 还不存在时
自动跳过、不报错 —— S5 还没做，那不是错误。

★ 不会打印密钥明文 ★
命中只打印「前缀 + 长度」，不打印原文。扫描器的输出会进 CI 日志，
日志本身不能成为新的泄露源。

用法：
    python tools/check_no_secrets.py                        # 扫默认范围
    python tools/check_no_secrets.py <路径> [<路径>...]     # 扫指定路径
    python tools/check_no_secrets.py --json                 # 机器可读输出

退出码：
    0 = 干净
    1 = 命中疑似真实密钥
    2 = 扫描器自身出错（路径不存在等）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

#: 仓库根目录（本文件在 <repo>/tools/ 下）
REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 扫描范围
# ---------------------------------------------------------------------------

#: 整目录跳过：版本库元数据、依赖目录、字节码缓存、工具工作区。
_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".workbuddy",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode",
    ".venv", "venv", "site-packages", "tenants", ".eggs",
})

#: 按后缀认作文本的文件类型。
#: ★ 密钥 / 证书容器类后缀也必须在里面 ★
#:   `.pem` / `.key` 里装的就是私钥本体，是最该抓的一类。
#:   （`.key` 有歧义 —— 可能是 SSH 私钥，也可能是普通配置文件。
#:     宁可多读一个文件，也别漏一把私钥：漏报成本远高于误报成本。）
_TEXT_SUFFIXES = frozenset({
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".env", ".txt", ".md", ".markdown", ".rst", ".html", ".htm", ".css",
    ".scss", ".less", ".sh", ".bash", ".bat", ".cmd", ".ps1", ".spec",
    ".sql", ".csv", ".lock", ".properties", ".xml", ".svg", ".plist",
    ".pem", ".key", ".crt", ".cer", ".p12", ".pfx", ".jks", ".asc",
    ".gpg", ".ppk", ".kdbx", ".keystore", ".der", ".csr",
})

#: 无后缀但按文本处理的文件名。
_TEXT_NAMES = frozenset({
    "Dockerfile", "Makefile", "Procfile", ".env", ".gitignore",
    ".dockerignore", ".npmrc", ".pypirc", ".editorconfig",
})

#: 单文件大小上限：超过这个值基本是二进制资源，不必再读。
_MAX_FILE_BYTES = 4 * 1024 * 1024

# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------

#: 负向前瞻：左边必须不是标识符字符。
#: 这一条把 `cv-mark-text` / `task-list` 这类**含密钥前缀的普通词**挡在外面。
_WORD_GUARD = r"(?<![A-Za-z0-9_])"

#: (规则名, 正则源)。规则源里出现的前缀后面紧跟 `[`，所以本文件自身
#: 不会被自己的规则命中（自扫描安全）。
_RULE_SPECS = (
    ("volcengine-ark", r"ark-[A-Za-z0-9]{16,}"),
    ("openai", r"sk-[A-Za-z0-9]{20,}"),
    ("openai-project", r"sk-proj-[A-Za-z0-9_\-]{20,}"),
    ("anthropic", r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    ("google-api-key", r"AIza[0-9A-Za-z_\-]{35}"),
    ("github-token", r"gh[pousr]_[A-Za-z0-9]{30,}"),
    ("slack-token", r"xox[baprs]-[A-Za-z0-9\-]{12,}"),
    ("aws-access-key-id", r"AKIA[0-9A-Z]{16}"),
    ("private-key-block",
     r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"),
)

#: 已编译的「厂商特征」规则。
VENDOR_RULES = tuple(
    (name, re.compile(_WORD_GUARD + pattern))
    for name, pattern in _RULE_SPECS
)

#: 通用规则：形如 `api_key = "<32 位以上>"` 的赋值。
#: 厂商规则抓不到的形态由它兜底（比如一把自建服务的 base64 token）。
_GENERIC_RE = re.compile(
    r"(?i)(?:api[_-]?key|apikey|secret[_-]?key|access[_-]?token|"
    r"auth[_-]?token|client[_-]?secret|app[_-]?secret|password|passwd|"
    r"bearer|private[_-]?key)"
    r"[^A-Za-z0-9\n]{0,8}?[:=]{1,2}\s*"
    r"[\"']([A-Za-z0-9_\-\.\/\+]{32,})[\"']"
)

#: 形如 `postgres://user:pass@host` 的带口令连接串。
_URI_CREDENTIAL_RE = re.compile(
    r"(?i)\b(?:postgres|postgresql|mysql|mongodb(?:\+srv)?|redis|amqp)"
    r"://[^\s:@/\"']{1,64}:[^\s:@/\"']{6,}@[^\s/\"']+"
)

#: 明显是占位符的值，不算命中（避免把文档里的示例值当泄露）。
_PLACEHOLDER_HINTS = (
    "your", "example", "placeholder", "changeme", "change-me", "change_me",
    "dummy", "fake", "stub", "sample", "xxxx", "todo", "insert",
    "redacted", "removed", "masked", "notarealkey",
)


def _is_placeholder(value: str) -> bool:
    low = value.lower()
    return any(hint in low for hint in _PLACEHOLDER_HINTS)


def _redact(token: str) -> str:
    """把命中内容脱敏成「前缀 + 长度」。**绝不打印明文。**"""
    if token.startswith("-----BEGIN"):
        return "<PRIVATE KEY BLOCK>"
    idx = token.find("-")
    if 0 <= idx <= 8:
        prefix = token[:idx + 1]
    else:
        prefix = token[:4]
    return f"{prefix}****(len={len(token)})"


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------

def _looks_text(path: Path) -> bool:
    """这个文件**看起来**是不是文本。

    判据：读前 4KB —— 含 NUL 字节（真二进制）或不是合法 UTF-8 → 不算文本。
    """
    try:
        with path.open("rb") as fh:
            chunk = fh.read(4096)
    except OSError:
        return False
    if not chunk:
        return True
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _is_text_candidate(path: Path) -> bool:
    """这个文件要不要读进来扫？

    ★ 为什么不只看后缀白名单（这是实测踩过的坑） ★
    漏检过一次：`.pem` 不在后缀白名单里 → 私钥文件**整个没被读进来**，
    于是「规则写得对」也白搭 —— 规则再准，得先被读到才能用。
    所以这里的判据是 **后缀白名单 OR 内容看起来像文本**：
      · 白名单里的，一律当文本读；
      · 不在白名单里的，只要能解成 UTF-8 且不含 NUL 字节，也当文本读。
    这样以后新增任何文本格式（.pem / .key / .p12 / .toml …）都不会再漏。
    代价是多读几个文件 —— `_MAX_FILE_BYTES` 兜住上限。
    """
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return False
    except OSError:
        return False
    if path.name in _TEXT_NAMES or path.suffix.lower() in _TEXT_SUFFIXES:
        return True
    return _looks_text(path)


def _iter_text_files(roots):
    """产出待扫描的文本文件（跳过二进制、依赖目录、超大文件）。"""
    for root in roots:
        if root.is_file():
            if _is_text_candidate(root):
                yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in _SKIP_DIRS and not d.startswith(".git")
            )
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if _is_text_candidate(path):
                    yield path



def scan_text(text: str, source: str) -> list[dict]:
    """扫一段文本，返回命中列表。每条：行号 / 列号 / 规则名 / 脱敏串。"""
    hits: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line_hits: list[dict] = []
        for name, pattern in VENDOR_RULES:
            for match in pattern.finditer(line):
                line_hits.append({
                    "file": source, "line": lineno, "col": match.start() + 1,
                    "rule": name, "token": match.group(0),
                })
        uri = _URI_CREDENTIAL_RE.search(line)
        if uri:
            line_hits.append({
                "file": source, "line": lineno, "col": uri.start() + 1,
                "rule": "credential-in-uri", "token": uri.group(0),
            })
        # 通用规则：同一行若已被厂商规则命中就不再重复报（避免一条泄露报两遍）
        if not line_hits:
            generic = _GENERIC_RE.search(line)
            if generic:
                value = generic.group(1)
                if not _is_placeholder(value):
                    line_hits.append({
                        "file": source, "line": lineno,
                        "col": generic.start(1) + 1,
                        "rule": "generic-assignment", "token": value,
                    })
        hits.extend(line_hits)
    return hits


def scan_paths(roots) -> tuple[list[dict], int]:
    """扫描若干根路径，返回 (命中列表, 扫描文件数)。"""
    hits: list[dict] = []
    scanned = 0
    for path in _iter_text_files(roots):
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            display = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            display = path.as_posix()
        hits.extend(scan_text(text, display))
    return hits, scanned


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="扫描仓库里是否混入了疑似真实的 API 密钥（命中即非零退出）。")
    parser.add_argument("paths", nargs="*",
                        help="要扫描的路径（默认：仓库根目录）")
    parser.add_argument("--json", action="store_true",
                        help="以 JSON 输出结果（给 CI 用）")
    parser.add_argument("--quiet", action="store_true",
                        help="不输出扫描概况，只在失败时打印命中")
    args = parser.parse_args(argv)

    roots = [Path(p).resolve() for p in args.paths] or [REPO_ROOT]
    missing = [r for r in roots if not r.exists()]
    if missing:
        print("[FATAL] 路径不存在：" + ", ".join(str(m) for m in missing),
              file=sys.stderr)
        return 2

    hits, scanned = scan_paths(roots)

    if args.json:
        print(json.dumps({
            "scanned_files": scanned,
            "roots": [str(r) for r in roots],
            "hit_count": len(hits),
            "hits": [{
                "file": h["file"], "line": h["line"], "col": h["col"],
                "rule": h["rule"], "redacted": _redact(h["token"]),
            } for h in hits],
        }, ensure_ascii=False, indent=2))
        return 1 if hits else 0

    if hits:
        print(f"[FAIL] 发现 {len(hits)} 处疑似真实密钥"
              f"（扫描 {scanned} 个文本文件）")
        for h in hits:
            print(f"  {h['file']}:{h['line']}:{h['col']}  "
                  f"[{h['rule']}]  {_redact(h['token'])}")
        print("[FAIL] 闸门不放行：把这些密钥换成占位值 / 环境变量，"
              "真实密钥请轮换后从仓库里清干净。")
        return 1

    if not args.quiet:
        deploy = REPO_ROOT / "deploy"
        note = "（deploy/ 已一并扫描）" if deploy.is_dir() else \
               "（deploy/ 尚不存在，已跳过）"
        print(f"[OK] 未发现疑似真实密钥。扫描 {scanned} 个文本文件{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
