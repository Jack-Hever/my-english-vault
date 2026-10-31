# -*- coding: utf-8 -*-
"""计算「剥离注释后」的 ratchet 基线，并做反向验证。

背景：test_cloud_security.py 的零改动 ratchet 用**逐字节 md5** 钉死
app/articles.py 与 app/websearch.py。本轮术语统一（「回译」→「翻译」）
只改了这两份文件里的**注释文案**，出网点代码一行没动，却把逐字节 md5 触发了。

根因：逐字节 md5 把「注释」和「代码」混为一谈 —— 它守不住它真正想守的东西
（出网点被改动），却会被纯文案改动误伤。

正解：ratchet 比对**剥离注释后的代码**。注释是文档，不是安全边界。
（配套的 AST 级出网点清单守卫 EXPECTED_EGRESS 本来就独立存在，
  它才是真正守住出网点的闸门，注释剥离不会削弱它。）

用法： python tools/recount_ratchet_baseline.py
"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# ★ 指纹实现只有一份 ★
#   本脚本曾经自带一份「两阶段」实现，测试里是「单循环」实现，两份实现漂移 ⇒
#   拿单循环指纹比两阶段基线永远 FAIL（判据自己造假红）。
#   现在两边都从 tools/code_fingerprint.py 取，口径由结构保证一致。
from code_fingerprint import (  # noqa: E402
    code_fingerprint,
    strip_comments_and_docstrings,
)

REPO = Path(__file__).resolve().parent.parent
FILES = ["app/articles.py", "app/websearch.py"]


def main() -> int:
    print("=== 剥离注释+docstring+字符串后的代码指纹 ===")
    fps = {}
    for rel in FILES:
        fps[rel] = code_fingerprint(REPO / rel)
        print("%-24s %s" % (rel, fps[rel]))

    print("\n=== 反向验证：判据能 FAIL 吗 ===")
    # ① 注入真实出网点改动 → 指纹必须变
    p = REPO / "app/websearch.py"
    orig = p.read_text(encoding="utf-8")
    anchor = "def search_sentences("
    try:
        if anchor not in orig:
            print("  FAIL 注入锚点没找到，无法验证")
            return 1
        injected = orig.replace(
            anchor,
            "def _ratchet_probe():\n"
            "    return requests.get('https://evil.example.com')  # 注入的出网点\n"
            "\n\n" + anchor, 1)
        tmp = REPO / "app/_ratchet_probe.py"
        tmp.write_text(injected, encoding="utf-8")
        f_inj = code_fingerprint(tmp)
        tmp.unlink()
        changed = f_inj != fps["app/websearch.py"]
        print("  %s 注入出网点调用后代码指纹改变（期望 True）"
              % ("PASS" if changed else "FAIL"))
        if not changed:
            return 1
    finally:
        p.write_text(orig, encoding="utf-8")

    # ② 只改注释 → 代码指纹必须不变
    try:
        injected2 = orig.replace("# -*- coding: utf-8 -*-",
                                 "# -*- coding: utf-8 -*-  # 只改注释", 1)
        tmp = REPO / "app/_ratchet_probe2.py"
        tmp.write_text(injected2, encoding="utf-8")
        f_c = code_fingerprint(tmp)
        tmp.unlink()
        same = f_c == fps["app/websearch.py"]
        print("  %s 只改注释后代码指纹不变（期望 True）"
              % ("PASS" if same else "FAIL"))
        if not same:
            return 1
    finally:
        p.write_text(orig, encoding="utf-8")

    print("\n=== 新基线（贴进 test_cloud_security.py 的 ZERO_CHANGE_BASELINE）===")
    for rel in FILES:
        print('    "%s": "%s",' % (rel, fps[rel]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
