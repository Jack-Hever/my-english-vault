"""练习选词加权测试（需求 3-①②）：权重公式、排序、未评分兜底、下限筛选为空报错。

运行：python test_practice_weight.py
说明：用 ENPRACTICE_HOME 指向临时目录，不触碰真实数据。
"""

import os
import tempfile
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix="enpractice_weight_")
os.environ["ENPRACTICE_HOME"] = TMP  # 必须在导入 app 之前设置

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, entries, practices  # noqa: E402

db.init_db()

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def add_entry(term, importance=None, usefulness=None):
    payload = {"type": "word", "term": term, "meaning_zh": f"{term} 的释义"}
    if importance is not None:
        payload["importance"] = importance
    if usefulness is not None:
        payload["usefulness"] = usefulness
    conn = db.connect()
    try:
        return entries.create_entry(conn, payload)
    finally:
        conn.close()


def set_due(entry_id, when):
    conn = db.connect()
    try:
        conn.execute("UPDATE review_state SET due_at = ? WHERE entry_id = ?", (when, entry_id))
        conn.commit()
    finally:
        conn.close()


def iso(days_ago):
    return (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
print("[1] 权重公式：满分约为 0 分的 3 倍，未评分按中位数兜底")
# ---------------------------------------------------------------------------

same_due = iso(2)  # 统一到期时间 → 紧迫度相同，权重差异只来自评分
full = {"due_at": same_due, "importance": 5.0, "usefulness": 5.0}
zero = {"due_at": same_due, "importance": 0.0, "usefulness": 0.0}
unrated = {"due_at": same_due, "importance": None, "usefulness": None}

w_full = practices._practice_weight(full)
w_zero = practices._practice_weight(zero)
w_unrated = practices._practice_weight(unrated)

check("满分权重 > 未评分权重 > 0 分权重", w_full > w_unrated > w_zero,
      f"{w_full:.4f} / {w_unrated:.4f} / {w_zero:.4f}")
# ★ 容差 1e-6 而不是 1e-9 ★
#   这两个比值里 urgency 会被约掉，但浮点除法 (u*1.5)/(u*0.5) 不保证
#   精确等于 3.0 —— 误差 ~1e-10~1e-9，取决于运行时刻（_due_urgency 用
#   datetime.now()，而 iso(2) 是「两天前的当前时刻」，days_overdue 带小数）。
#   判据写 1e-9 会在某些时刻**假红**（实测：单独跑 1.04e-10 通过，
#   全量回归那一刻越界挂掉）。业务断言本身是「约 3 倍」，1e-6 绰绰有余。
check("满分约为 0 分的 3 倍（用户确认的强度）",
      abs(w_full / w_zero - 3.0) < 1e-6, f"{w_full / w_zero:.6f} 倍")
check("未评分按 3.0 兜底：权重 = 紧迫度 × (0.5 + 0.6)",
      abs(w_unrated / w_zero - 1.1 / 0.5) < 1e-6, f"{w_unrated / w_zero:.6f} 倍（期望 2.2）")
check("未评分不等于 0 分（否则新词永远排不进练习）", w_unrated > w_zero,
      f"未评分 {w_unrated:.4f} vs 0 分 {w_zero:.4f}")
check("DEFAULT_RATING 取量表（0-5）中位 3.0", practices.DEFAULT_RATING == 3.0,
      practices.DEFAULT_RATING)
check("两维取均值：(5, 0) 与 (2.5, 2.5) 权重相同",
      abs(practices._practice_weight({"due_at": same_due, "importance": 5.0, "usefulness": 0.0})
          - practices._practice_weight({"due_at": same_due, "importance": 2.5, "usefulness": 2.5})) < 1e-6)

print("\n[2] 乘法而非加法：严重逾期的低分词压过刚学的满分词（高分词挤不掉到期词）")
overdue_low = practices._practice_weight({"due_at": iso(10), "importance": 0.0, "usefulness": 0.0})
fresh_full = practices._practice_weight({"due_at": iso(0), "importance": 5.0, "usefulness": 5.0})
check("逾期 10 天的 0 分词 > 刚到期的满分词", overdue_low > fresh_full,
      f"{overdue_low:.3f} vs {fresh_full:.3f}")
check("逾期越久权重越大", practices._practice_weight({"due_at": iso(5), "importance": None, "usefulness": None})
      > practices._practice_weight({"due_at": iso(1), "importance": None, "usefulness": None}))

# ---------------------------------------------------------------------------
print("\n[3] 自动选词：同等到期时高分词排前，未评分排在 0 分之前")
# ---------------------------------------------------------------------------

low = add_entry("lowly", importance=0.0, usefulness=0.0)
mid = add_entry("middling", importance=3.0, usefulness=3.0)
high = add_entry("highly", importance=5.0, usefulness=5.0)
fresh = add_entry("freshly")  # 完全未评分
for e in (low, mid, high, fresh):
    set_due(e["id"], same_due)

conn = db.connect()
try:
    picked = practices._resolve_terms(conn, [])
finally:
    conn.close()

order = [t.lower() for t in picked]
check("4 个候选都被选出", len(order) == 4, order)
check("满分词排第一", order[0] == "highly", order)
check("未评分排在 0 分之前（未评分 ≠ 0 分）",
      order.index("freshly") < order.index("lowly"), order)
check("0 分词排在最后", order[-1] == "lowly", order)
check("3.0 分词排在未评分之后、0 分之前",
      order.index("middling") < order.index("lowly"), order)

# ---------------------------------------------------------------------------
print("\n[4] 下限筛选：筛完为空必须报错，不能静默返回空练习")
# ---------------------------------------------------------------------------

conn = db.connect()
try:
    ok = practices._resolve_terms(conn, [], min_importance=4.0)
    check("下限 4.0 时只剩满分词（5.0 用 >= 判定，属达标）",
          [t.lower() for t in ok] == ["highly"], ok)

    # 把唯一的满分词清成「未评分」后再次筛：未评分按 DEFAULT_RATING=3.0 参与判定，
    # 3.0 < 4.0 所以无人达标 —— 这正是「未评分不被当成达标」的口径证据。
    entries.update_ratings(conn, high["id"], {"importance": None, "usefulness": None})
    too_high = None
    try:
        practices._resolve_terms(conn, [], min_importance=4.0)
    except practices.PracticeError as exc:
        too_high = str(exc)
    check("无人达标时抛 PracticeError（而不是静默返回空练习）", too_high is not None, too_high)
    check("报错是中文且说明了怎么办",
          bool(too_high) and "评分" in too_high and ("放宽" in too_high or "打分" in too_high),
          too_high)
    entries.update_ratings(conn, high["id"], {"importance": 5.0, "usefulness": 5.0})

    # 下限是合法边界值时不该报错
    edge = practices._resolve_terms(conn, [], min_importance=0.0)
    check("下限 0.0 时全部保留", len(edge) == 4, edge)
finally:
    conn.close()

conn = db.connect()
try:
    err = None
    try:
        practices.create_practice(conn, [], min_importance=99.0)
    except practices.PracticeError as exc:
        err = str(exc)
    check("create_practice 传非法下限（>5）先被拦住", err is not None and "0 ~ 5" in err, err)
finally:
    conn.close()

# ---------------------------------------------------------------------------
print("\n[5] 显式指定词时不按评分改写（用户点名优先）")
# ---------------------------------------------------------------------------

conn = db.connect()
try:
    picked = practices._resolve_terms(conn, ["lowly", "highly"], min_importance=5.0)
finally:
    conn.close()
check("显式传词时保持用户给的顺序，不被下限筛掉",
      [t.lower() for t in picked] == ["lowly", "highly"], picked)

# ---------------------------------------------------------------------------
print("\n[6] 排序白名单仍不含评分列（需求：评分不用于排序）")
# ---------------------------------------------------------------------------

check("importance 不在排序白名单", "importance" not in entries.SORT_COLUMNS, sorted(entries.SORT_COLUMNS))
check("usefulness 不在排序白名单", "usefulness" not in entries.SORT_COLUMNS, sorted(entries.SORT_COLUMNS))

passed = sum(1 for _, ok in results if ok)
failed = len(results) - passed
print("\n" + "=" * 60)
print(f"PASS={passed}  FAIL={failed}  TOTAL={len(results)}")
if failed:
    print("\n失败项：")
    for name, ok in results:
        if not ok:
            print("  - " + name)
raise SystemExit(1 if failed else 0)
