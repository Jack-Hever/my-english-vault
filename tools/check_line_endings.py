# -*- coding: utf-8 -*-
"""★ 字节级行尾体检 ★

背景（两个真实风险）：
① tools/recount_ratchet_baseline.py 的 finally 块用
   `orig = p.read_text()` → `p.write_text(orig)` 还原被注入的文件。
   read_text 做通用换行转换（CRLF→LF），write_text 在 Windows 上又把
   LF 转回 CRLF（newline=None → os.linesep）⇒ 理论上往返无损，
   但如果原文件里有**孤立 \r**（旧 Mac 换行）或混合行尾，
   往返会静默改变字节。必须用字节级比对确认，不能靠推理。
② 本轮我用 Edit 工具改了 test_cloud_security.py / tools/*.py，
   Edit 写回时用的是 LF 还是 CRLF，必须实测。

判据：
  · 报告每个文件的 CRLF / 裸LF / 孤立CR 计数
  · 对关键文件与 deploy/ 旧产物做**字节级**比对（只允许已知的文案差异）
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

TARGETS = [
    "app/main.py", "app/ai.py", "app/practices.py", "app/websearch.py",
    "app/articles.py", "web/app.js", "web/index.html", "web/style.css",
    "test_cloud_security.py", "test_seed_migrate.py",
    "tools/code_fingerprint.py", "tools/recount_ratchet_baseline.py",
]


def line_endings(data: bytes):
    crlf = data.count(b"\r\n")
    lone_cr = data.count(b"\r") - crlf
    lf = data.count(b"\n") - crlf
    return crlf, lone_cr, lf


def main() -> int:
    print("=== 行尾体检 ===")
    print("%-38s %8s %8s %8s %10s" % ("文件", "CRLF", "裸CR", "裸LF", "字节"))
    bad = []
    for rel in TARGETS:
        p = REPO / rel
        if not p.is_file():
            print("%-38s  <不存在>" % rel)
            continue
        data = p.read_bytes()
        crlf, lrc, lf = line_endings(data)
        flag = ""
        if lrc:
            flag = "  ★孤立CR"
            bad.append(rel)
        elif lf:
            flag = "  ★裸LF"
            bad.append(rel)
        print("%-38s %8d %8d %8d %10d%s"
              % (rel, crlf, lrc, lf, len(data), flag))

    print()
    print("=== 与 deploy/ 旧产物的字节级比对（允许的差异已列明）===")
    pairs = [
        ("app/websearch.py", "app/websearch.py",
         "术语统一：第 712 行 docstring 内 1 行中文"),
        ("app/ai.py", "app/ai.py", "术语统一：数处「回译」→「翻译」"),
        ("app/practices.py", "app/practices.py", "术语统一：数处"),
        ("web/app.js", "web/app.js", "术语统一 + 病句修正"),
        ("web/index.html", "web/index.html", "术语统一 + 按钮文案"),
        ("web/style.css", "web/style.css", "仅注释文案"),
        ("app/main.py", "app/main.py", "★ cookie 下发时机修复（关键）"),
    ]
    for src_rel, dep_rel, note in pairs:
        s = REPO / src_rel
        d = REPO / "deploy" / dep_rel
        if not s.is_file() or not d.is_file():
            print("%-22s <缺文件>" % src_rel)
            continue
        sb = s.read_bytes()
        db = d.read_bytes()
        if sb == db:
            print("%-22s SAME（与 deploy 一致）  ← %s" % (src_rel, note))
            continue
        # 逐行 diff（按 \n 切，容忍 CRLF/LF）
        sl = sb.replace(b"\r\n", b"\n").split(b"\n")
        dl = db.replace(b"\r\n", b"\n").split(b"\n")
        import difflib
        diff = [l for l in difflib.unified_diff(
            [x.decode("utf-8", "replace") for x in dl],
            [x.decode("utf-8", "replace") for x in sl],
            "deploy", "src", lineterm="", n=0)]
        changed = [l for l in diff if l.startswith(("+", "-"))
                   and not l.startswith(("+++", "---"))]
        print("%-22s DIFF %d 行（字节 %d → %d）  ← %s"
              % (src_rel, len(changed), len(db), len(sb), note))
        for l in changed[:24]:
            print("      " + l[:160])

    print()
    print("行尾体检结论：%s"
          % ("全部文件行尾统一为 CRLF" if not bad else "★ 异常文件: %s" % bad))
    return 0


if __name__ == "__main__":
    sys.exit(main())
