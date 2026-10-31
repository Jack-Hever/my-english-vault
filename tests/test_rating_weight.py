"""评分影响练习选词的验证：频次倾斜、未评分兜底、下限筛选。

运行：python test_rating_weight.py
说明：用 ENPRACTICE_HOME 指向临时目录，不触碰真实数据。

验证的不是「函数存在」，而是**行为真的变了**：
- 同等到期条件下，高分词确实排在低分词前面
- 未评分按 3.0 中位兜底（不是 0，也不是一定达标）
- 下限筛选把低分词挡在外面，且筛空时报错而不是静默返回空练习
"""

import os
import tempfile
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix="enpractice_weight_")
os.environ["ENPRACTICE_HOME"] = TMP

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, entries, practices, review  # noqa: E402

db.init_db()
conn = db.connect()

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def seed(term, imp=None, use=None, overdue_days=1):
    """造一个词条，并把它的复习状态设成「已逾期 overdue_days 天」。"""
    eid = entries.create_entry(
        conn, {"type": "word", "term": term, "meaning_zh": "释义"}, commit=False
    )["id"]
    if imp is not None or use is not None:
        entries.update_ratings(conn, eid, {"importance": imp, "usefulness": use})
    due = (datetime.now() - timedelta(days=overdue_days)).isoformat(timespec="seconds")
    conn.execute("UPDATE review_state SET due_at = ? WHERE entry_id = ?", (due, eid))
    conn.commit()
    return eid


print("  -- 权重口径")
check("未评分兜底 = 3.0（量表 0–5 中位）", practices.DEFAULT_RATING == 3.0)
w_unrated = practices._practice_weight({"importance": None, "usefulness": None, "due_at": None})
w_zero = practices._practice_weight({"importance": 0, "usefulness": 0, "due_at": None})
w_full = practices._practice_weight({"importance": 5, "usefulness": 5, "due_at": None})
check("满分权重约为 0 分的 3 倍",
      abs(w_full / w_zero - 3.0) < 0.01, f"{w_full:.3f} / {w_zero:.3f} = {w_full/w_zero:.2f}")
check("未评分权重介于 0 分与满分之间（按中位）",
      w_zero < w_unrated < w_full, f"0分{w_zero:.2f} < 未评{w_unrated:.2f} < 满分{w_full:.2f}")

print("  -- 紧迫度主导，评分只在同紧迫度内决定胜负")
now = datetime.now().isoformat(timespec="seconds")
urg_1d = practices._practice_weight({"importance": 5, "usefulness": 5,
                                     "due_at": (datetime.now() - timedelta(days=1)).isoformat()})
urg_20d = practices._practice_weight({"importance": 0, "usefulness": 0,
                                      "due_at": (datetime.now() - timedelta(days=20)).isoformat()})
check("逾期 20 天的 0 分词 > 逾期 1 天的 5 分词（高分压不过严重逾期）",
      urg_20d > urg_1d, f"{urg_20d:.2f} vs {urg_1d:.2f}")
same_a = practices._practice_weight({"importance": 5, "usefulness": 5,
                                     "due_at": (datetime.now() - timedelta(days=2)).isoformat()})
same_b = practices._practice_weight({"importance": 1, "usefulness": 1,
                                     "due_at": (datetime.now() - timedelta(days=2)).isoformat()})
check("同等逾期下高分词优先", same_a > same_b, f"{same_a:.2f} vs {same_b:.2f}")

print("  -- 自动选词：高分词真的更常进练习")
# 同一逾期天数，只有评分不同；候选池上限 8 个，这里只放 4 个，全部都会入选，
# 因此改用「排序位置」验证权重方向
for t in ("w_low", "w_mid", "w_high", "w_none"):
    seed(t, *(None, None) if t == "w_none" else
         ((0.5, 0.5) if t == "w_low" else ((3.0, 3.0) if t == "w_mid" else (5.0, 5.0))))
picked = practices._resolve_terms(conn, [])
print("        选词顺序:", picked)
pos = {t: picked.index(t) for t in picked if t in ("w_low", "w_mid", "w_high", "w_none")}
# 只断言严格的大小关系：w_mid(3.0) 与 w_none(未评分→3.0 兜底) 权重完全相同，
# 并列时的先后取决于候选池顺序，不该写进断言
check("高分词（5分）排在未评分词之前",
      "w_high" in pos and "w_none" in pos and pos["w_high"] < pos["w_none"], str(pos))
check("高分词（5分）排在低分词（0.5分）之前",
      "w_high" in pos and "w_low" in pos and pos["w_high"] < pos["w_low"], str(pos))
check("低分词排在最后",
      "w_low" in pos and pos["w_low"] == max(pos.values()), str(pos))

print("  -- 下限筛选")
seed("s_high", 5.0, 5.0)
seed("s_low", 0.5, 0.5)
seed("s_none", None, None)
hi = practices._resolve_terms(conn, [], min_importance=4.0)
check("筛 importance>=4 时低分词被挡住", "s_low" not in hi, str(hi))
check("筛 importance>=4 时高分词入选", "s_high" in hi, str(hi))
check("未评分按 3.0 兜底 → 不达 4.0 被挡", "s_none" not in hi, str(hi))
mid = practices._resolve_terms(conn, [], min_importance=3.0)
check("筛 importance>=3 时未评分（3.0）入选", "s_none" in mid, str(mid))
check("筛 importance>=3 时低分词（0.5）仍被挡", "s_low" not in mid, str(mid))

print("  -- 筛空必须报错，不能静默返回空练习")
# 先把所有满分词的评分清掉，制造「没有任何词达得到 5.0」的真实场景
for t in ("s_high", "w_high"):
    rows = conn.execute("SELECT id FROM entries WHERE term = ?", (t,)).fetchall()
    for r in rows:
        entries.update_ratings(conn, r["id"], {"importance": None, "usefulness": None})
try:
    practices._resolve_terms(conn, [], min_importance=5.0, min_usefulness=5.0)
    check("全被筛掉 -> 抛错", False, "没抛错，会静默变成空练习")
except practices.PracticeError as exc:
    check("全被筛掉 -> 抛错", True, str(exc))
    check("报错文案说明是筛太严而非没词", "评分条件" in str(exc), str(exc))

print("  -- 用户显式指定词时不被评分改写")
explicit = practices._resolve_terms(conn, ["s_low"], min_importance=5.0)
check("显式指定词照原样返回（筛选不生效）", explicit == ["s_low"], str(explicit))

print("  -- 下限越界校验")
try:
    practices.create_practice(conn, [], min_importance=9)
    check("下限 9 -> 拒绝", False)
except practices.PracticeError as exc:
    check("下限 9 -> 拒绝", True, str(exc))

print("  -- review 查询已带评分列")
due = review.due_entries(conn, mode=review.MODE_INTERVAL, limit=5)
check("due_entries 返回 importance/usefulness 列",
      all("importance" in d and "usefulness" in d for d in due), str(due[:1]))
conn.close()

print("\n结论：")
failed = [n for n, ok in results if not ok]
for n, ok in results:
    print(f"    [{'PASS' if ok else 'FAIL'}] {n}")
print(f"总计：{'全部通过' if not failed else f'{len(failed)} 项失败'}（{len(results)} 项）")
