# -*- coding: utf-8 -*-
"""模型漏score 字段时的处置：**不许静默变0 分**。

★ 这个 bug 是怎么发现的 ★
练习 #9 用真实模型（豆包 doubao-seed-2-1-lite）重跑 3 次，其中 1 次模型返回
`{"grammar": {"issues": [...], "comment": "..."}}` —— **三个维度全都没给 score**。
旧代码 _clamp_score(None, 30)走 except 分支返回 0，于是用户会看到
「语法 0 / 地道 0 / 保真 0，总分 0」，而 practices.py:495 把这个 0 直接写进数据库。
这比评分偏松严重得多：偏松用户还能自己判断，0 分会被当成真实成绩，
还会连带污染词条复习排期。

处置口径（已定）：**缺 score 就重试一次；仍缺则报错，绝不写0 分入库**。
"""

from __future__ import annotations

import pathlib
import sys
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8")

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import ai  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if not ok else ""))


print("\n[1] _has_score：区分「漏字段」与「真的给 0 分」")
check("dict 带score=0 -> True（0 分是合法分数，不是缺失）",
      ai._has_score({"score": 0}) is True)
check("dict 带 score=27 -> True", ai._has_score({"score": 27}) is True)
check("dict 带 score='25'（字符串数字）-> True",
      ai._has_score({"score": "25"}) is True)
check("缺 score 键 -> False", ai._has_score({"issues": [], "comment": "c"}) is False)
check("score=None -> False", ai._has_score({"score": None}) is False)
check("score 是文字 -> False", ai._has_score({"score": "很好"}) is False)
check("item 不是 dict -> False", ai._has_score(None) is False)
check("item 是 list -> False", ai._has_score([1, 2]) is False)

print("\n[2] _normalize_review(strict=True)：缺分数抛错而非补0")
_OK = {"grammar": {"score": 27}, "fluency": {"score": 24}, "fidelity": {"score": 29}}

try:
    ai._normalize_review(_OK, strict=True)
    check("分数齐全时不抛错", True)
except Exception as exc:  # noqa: BLE001
    check("分数齐全时不抛错", False, repr(exc))

_MISS = {"grammar": {"issues": [], "comment": "c"},
         "fluency": {"issues": [], "comment": "c"},
         "fidelity": {"issues": [], "comment": "c"}}
try:
    ai._normalize_review(_MISS, strict=True)
    check("三个维度全缺 -> 抛 ReviewScoreMissingError", False, "居然没抛")
except ai.ReviewScoreMissingError as exc:
    check("三个维度全缺 -> 抛 ReviewScoreMissingError", True)
    check("异常信息点明缺哪几项（用户看得懂）",
          "语法正确" in str(exc) and "表达地道" in str(exc) and "语义保真" in str(exc),
          str(exc))
except Exception as exc:  # noqa: BLE001
    check("三个维度全缺 -> 抛 ReviewScoreMissingError", False, repr(exc))

_ONE = {"grammar": {"score": 27}, "fluency": {"score": None}, "fidelity": {"score": 29}}
try:
    ai._normalize_review(_ONE, strict=True)
    check("只有一维缺 -> 也抛错（不许只补一维）", False, "居然没抛")
except ai.ReviewScoreMissingError:
    check("只有一维缺 -> 也抛错（不许只补一维）", True)

print("\n[3] strict=False（mock 路径）保持旧行为，不受影响")
_r = ai._normalize_review(_MISS)
check("strict=False 时缺分数仍补0（mock 演示数据要能跑通）",
      _r["total"] == 0, f'total={_r["total"]}')
check("mock 三维分数齐全时 total 正确",
      ai._normalize_review(_OK)["total"] == 27 + 24 + 29)

print("\n[4] review_translation：漏字段会重试一次，**绝不返回 0 分**")
calls = {"n": 0}


def _reply_missing_then_ok(system, user, **kw):
    calls["n"] += 1
    if calls["n"] == 1:          # 第一次：复现真实模型漏字段
        return ('{"grammar": {"issues": ["People 大写"], "comment": "c"},'
                ' "fluency": {"issues": [], "comment": "c"},'
                ' "fidelity": {"issues": [], "comment": "c"},'
                ' "suggestion": "s"}')
    return ('{"grammar": {"score": 27, "issues": ["People 大写"], "comment": "c"},'
            ' "fluency": {"score": 24, "issues": [], "comment": "c"},'
            ' "fidelity": {"score": 29, "issues": [], "comment": "c"},'
            ' "suggestion": "s"}')


calls["n"] = 0
with patch.object(ai, "_chat", side_effect=_reply_missing_then_ok), \
     patch.object(ai, "is_mock_mode", return_value=False):
    r = ai.review_translation("For anyone who is not a ferocious gearhead, ...",
                              "对于并非狂热汽车爱好者的人而言，...",
                              "It is tempting for People who are not car enthusiasts ...")
check("第一次漏字段后自动重试", calls["n"] == 2, f"实际调用 {calls['n']} 次")
check("重试后拿到真实分数，不是 0", r["total"] == 80, f'total={r["total"]}')
check("issues 在重试后仍保留（不因重试丢失模型给的问题列表）",
      r["grammar"]["issues"] == ["People 大写"], str(r["grammar"]["issues"]))


def _reply_always_missing(system, user, **kw):
    calls["n"] += 1
    return ('{"grammar": {"issues": [], "comment": "c"},'
            ' "fluency": {"issues": [], "comment": "c"},'
            ' "fidelity": {"issues": [], "comment": "c"}}')


calls["n"] = 0
with patch.object(ai, "_chat", side_effect=_reply_always_missing), \
     patch.object(ai, "is_mock_mode", return_value=False):
    try:
        r2 = ai.review_translation("a b c", "测试", "d e f")
        check("两次都漏 -> 抛错而不是返回 0 分", False,
              f"居然返回了 total={r2.get('total')}")
    except ai.ReviewScoreMissingError:
        check("两次都漏 -> 抛错而不是返回 0 分", True)
    except Exception as exc:  # noqa: BLE001
        check("两次都漏 -> 抛错而不是返回 0 分", False, repr(exc))
check("失败时只重试了 1 次（共 2 次调用），不无限重试", calls["n"] == 2, f"{calls['n']}")

print("\n[5] ReviewScoreMissingError 是 AIError 子类（调用方可统一捕获）")
check("继承自 AIError", issubclass(ai.ReviewScoreMissingError, ai.AIError))

print("\n" + "=" * 70)
failed = [n for n, ok in results if not ok]
for n, ok in results:
    if not ok:
        print(f"  FAIL: {n}")
print(f"总计：{'全部通过' if not failed else f'{len(failed)} 项失败'}（{len(results)} 项）")
print("=" * 70)
sys.exit(1 if failed else 0)