# -*- coding: utf-8 -*-
"""学习进度分析（/api/analytics）的后端测试。

★ 核心红线 ★
这个页面的价值全在"数字是真的"。所以测试的重点不是"接口能返回 200"，
而是：**返回的每个数字都必须和直接查库得到的一致**。
一个"看起来能跑"的假分析页比没有这个页面更糟 —— 用户会照着它做判断。
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ★ 临时库 ★ 绝不碰用户真实库
HOME = pathlib.Path(tempfile.mkdtemp(prefix="enqa_analytics_"))
os.environ["ENPRACTICE_HOME"] = str(HOME)

from app import analytics, db  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if not ok else ""))


def section(t):
    print("\n" + t)


# ---------------------------------------------------------------------------
# 造一份可预期的数据
# ---------------------------------------------------------------------------
db.init_db()
conn = db.connect()


def add_entries(n, day, prefix="w"):
    for i in range(n):
        conn.execute(
            "INSERT INTO entries (type, term, meaning_zh, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            ("word", f"{prefix}{i}", f"释义{i}", f"{day}T10:00:00", f"{day}T10:00:00"),
        )


def add_article(title, day, sentences=3, tags=()):
    aid = conn.execute(
        "INSERT INTO articles (title, content, added_at, updated_at) VALUES (?,?,?,?)",
        (title, "正文内容", f"{day}T09:00:00", f"{day}T09:00:00"),
    ).lastrowid
    for s in range(sentences):
        conn.execute(
            "INSERT INTO article_sentences (article_id, seq, text) VALUES (?,?,?)",
            (aid, s + 1, f"Sentence {s + 1}."),
        )
    for name in tags:
        row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        if row:
            tid = row["id"]
        else:
            tid = conn.execute(
                "INSERT INTO tags (name) VALUES (?)", (name,)
            ).lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO article_tags (article_id, tag_id) VALUES (?,?)",
            (aid, tid),
        )
    return aid


def add_practice(passage_id, total, day, g=0, f=0, fid=0):
    conn.execute(
        "INSERT INTO practices (passage_id, user_text, ai_feedback, score_grammar, "
        "score_fluency, score_fidelity, score_total, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (passage_id, "x", "{}", g, f, fid, total, f"{day}T12:00:00"),
    )


today = datetime.date.today()
d0 = (today - datetime.timedelta(days=1)).isoformat()   # 昨天
d1 = (today - datetime.timedelta(days=3)).isoformat()   # 三天前
d9 = (today - datetime.timedelta(days=9)).isoformat()   # 九天前

pid = conn.execute(
    "INSERT INTO passages (title, origin, text_en, text_zh, created_at) "
    "VALUES (?,?,?,?,?)",
    ("测试素材", "paste", "A test sentence for practice.", "参考译文。", f"{d0}T08:00:00"),
).lastrowid
conn.commit()

# 昨天：3 次练习（77 / 80 / 93），10 个词条
add_practice(pid, 77, d0, 24, 21, 32)
add_practice(pid, 80, d0, 25, 25, 30)
add_practice(pid, 93, d0, 30, 28, 35)
add_entries(10, d0, "y")
add_article("昨天导入的外刊", d0, sentences=5, tags=("经济", "科技"))

# 三天前：1 次练习（60），20 个词条，1 篇外刊
add_practice(pid, 60, d1, 20, 20, 20)
add_entries(20, d1, "t")
add_article("三天前的外刊", d1, sentences=2, tags=("经济",))

# 九天前：只入库不练习
add_entries(7, d9, "n")
add_article("九天前的外刊", d9, sentences=1, tags=("经济", "科学", "科技"))
conn.commit()

data = analytics.build_analytics(conn)
conn.close()

# ---------------------------------------------------------------------------
section("[1] 结构：字段齐全，窗口正好 30 天")
# ---------------------------------------------------------------------------
for key in ("today", "overview", "trend", "hot_tags", "recent_entries", "recent_articles"):
    check(f"返回含 {key}", key in data, str(list(data.keys())))
check("trend 长度正好 30", len(data["trend"]) == 30, str(len(data["trend"])))
check("overview 字段齐全",
      all(k in data["overview"] for k in
          ("total_entries", "total_articles", "total_practices",
           "avg_score_all", "active_days", "window_days")),
      str(list(data["overview"].keys())))
check("最近词条上限 12", len(data["recent_entries"]) == 12, str(len(data["recent_entries"])))
check("最近外刊上限 8", len(data["recent_articles"]) == 3, str(len(data["recent_articles"])))

# ---------------------------------------------------------------------------
section("[2] 30 天序列：日期连续、无缺口、含今天")
# ---------------------------------------------------------------------------
dates = [t["date"] for t in data["trend"]]
check("第一天 = 今天减 29 天", dates[0] == (today - datetime.timedelta(days=29)).isoformat(), dates[0])
check("最后一天 = 今天", dates[-1] == today.isoformat(), dates[-1])
check("日期严格递增", all(dates[i] < dates[i + 1] for i in range(len(dates) - 1)))
# ★ 连续性是硬要求：中间缺日期，前端 X 轴就会错位 ★
gaps = [(dates[i], dates[i + 1]) for i in range(len(dates) - 1)
        if (datetime.date.fromisoformat(dates[i + 1])
            - datetime.date.fromisoformat(dates[i])).days != 1]
check("日期连续无缺口（缺一天就会让柱子错位）", not gaps, str(gaps))
check("每项都含四个量纲",
      all(all(k in t for k in ("date", "practice_count", "avg_score",
                               "new_entries", "new_articles")) for t in data["trend"]))

# ---------------------------------------------------------------------------
section("[3] 数值：与直接查库一致")
# ---------------------------------------------------------------------------
by_date = {t["date"]: t for t in data["trend"]}

check("昨天练习次数 = 3", by_date[d0]["practice_count"] == 3, str(by_date[d0]))
check("昨天平均分 = (77+80+93)/3 = 83.3",
      abs(by_date[d0]["avg_score"] - 83.3) < 0.05, str(by_date[d0]["avg_score"]))
check("昨天新增词条 = 10", by_date[d0]["new_entries"] == 10, str(by_date[d0]["new_entries"]))
check("昨天新增外刊 = 1", by_date[d0]["new_articles"] == 1, str(by_date[d0]["new_articles"]))

check("三天前练习次数 = 1", by_date[d1]["practice_count"] == 1, str(by_date[d1]))
check("三天前平均分 = 60", by_date[d1]["avg_score"] == 60, str(by_date[d1]["avg_score"]))
check("三天前新增词条 = 20", by_date[d1]["new_entries"] == 20, str(by_date[d1]["new_entries"]))

check("九天前：新增词条 = 7", by_date[d9]["new_entries"] == 7, str(by_date[d9]["new_entries"]))
check("九天前新增外刊 = 1", by_date[d9]["new_articles"] == 1, str(by_date[d9]["new_articles"]))

check("没练习的日子 avg_score 是 None 而不是 0（None=没练，0=考了0分）",
      by_date[d9]["avg_score"] is None, str(by_date[d9]["avg_score"]))
check("没练习的日子 practice_count = 0", by_date[d9]["practice_count"] == 0)

# 独立复算：不看 build_analytics，直接用 SQL 手算一遍再比
conn2 = db.connect()
manual = {}
for r in conn2.execute(
    "SELECT substr(created_at,1,10) d, COUNT(*) c, AVG(score_total) a "
    "FROM practices GROUP BY d"
).fetchall():
    manual[r["d"]] = (r["c"], round(float(r["a"]), 1))
conn2.close()
mismatch = [d for d, (c, a) in manual.items()
            if by_date[d]["practice_count"] != c or abs(by_date[d]["avg_score"] - a) > 0.05]
check("每个有练习的日期都与手算 SQL 一致", not mismatch, str(mismatch))

# ---------------------------------------------------------------------------
section("[4] 概览数字")
# ---------------------------------------------------------------------------
ov = data["overview"]
check("词条总量 = 37（10+20+7）", ov["total_entries"] == 37, str(ov["total_entries"]))
check("练习总数 = 4", ov["total_practices"] == 4, str(ov["total_practices"]))
check("外刊总数 = 3", ov["total_articles"] == 3, str(ov["total_articles"]))
check("平均分 = (77+80+93+60)/4 = 77.5",
      abs(ov["avg_score_all"] - 77.5) < 0.05, str(ov["avg_score_all"]))
check("有练习的天数 = 2（昨天 + 三天前）", ov["active_days"] == 2, str(ov["active_days"]))
check("窗口天数 = 30", ov["window_days"] == 30, str(ov["window_days"]))

# ---------------------------------------------------------------------------
section("[5] 热门标签：按真实关联计数")
# ---------------------------------------------------------------------------
tag_map = {t["name"]: t["article_count"] for t in data["hot_tags"]}
check("经济 = 3 篇（3 篇文章都打了经济）", tag_map.get("经济") == 3, str(tag_map))
check("科技 = 2 篇", tag_map.get("科技") == 2, str(tag_map))
check("科学 = 1 篇", tag_map.get("科学") == 1, str(tag_map))
check("按篇数降序", [t["article_count"] for t in data["hot_tags"]]
      == sorted([t["article_count"] for t in data["hot_tags"]], reverse=True),
      str(data["hot_tags"]))
check("没有关联文章的标签不出现（0 篇的不该占位）",
      all(t["article_count"] > 0 for t in data["hot_tags"]))

# ---------------------------------------------------------------------------
section("[6] 新词 / 新外刊 列表内容")
# ---------------------------------------------------------------------------
terms = [e["term"] for e in data["recent_entries"]]
check("新词按创建时间倒序（最新的在前）",
      data["recent_entries"][0]["created_at"] >= data["recent_entries"][-1]["created_at"])
check("新词含 term/meaning_zh/type/created_at",
      all(all(k in e for k in ("term", "meaning_zh", "type", "created_at"))
          for e in data["recent_entries"]))
check("释义真的带回来了（不是空串）",
      any(e["meaning_zh"] for e in data["recent_entries"]))
check("新外刊含句数",
      all("sentence_count" in a for a in data["recent_articles"]))
check("新外刊句数正确（昨天那篇 5 句）",
      any(a["title"] == "昨天导入的外刊" and a["sentence_count"] == 5
          for a in data["recent_articles"]),
      str([(a["title"], a["sentence_count"]) for a in data["recent_articles"]]))
check("新外刊按导入时间倒序",
      data["recent_articles"][0]["added_at"] >= data["recent_articles"][-1]["added_at"])

# ---------------------------------------------------------------------------
section("[7] 空库：结构完整、不崩")
# ---------------------------------------------------------------------------
empty_home = pathlib.Path(tempfile.mkdtemp(prefix="enqa_analytics_empty_"))
os.environ["ENPRACTICE_HOME"] = str(empty_home)
import importlib  # noqa: E402
importlib.reload(analytics)
importlib.reload(db)
db.init_db()
c3 = db.connect()
empty = analytics.build_analytics(c3)
c3.close()

check("空库不抛异常", True)
check("空库 trend 仍是 30 个点", len(empty["trend"]) == 30, str(len(empty["trend"])))
check("空库 hot_tags 为空列表", empty["hot_tags"] == [], str(empty["hot_tags"]))
check("空库 recent_entries 为空列表", empty["recent_entries"] == [])
check("空库 recent_articles 为空列表", empty["recent_articles"] == [])
check("空库 avg_score_all 是 None 不是 0",
      empty["overview"]["avg_score_all"] is None, str(empty["overview"]["avg_score_all"]))
check("空库 active_days = 0", empty["overview"]["active_days"] == 0)
check("空库 overview 各计数为 0",
      empty["overview"]["total_entries"] == 0
      and empty["overview"]["total_practices"] == 0
      and empty["overview"]["total_articles"] == 0)
check("空库 trend 每项结构完整（前端不用判断字段在不在）",
      all(set(t) == {"date", "practice_count", "avg_score",
                     "new_entries", "new_articles"} for t in empty["trend"]))

# 还原环境
os.environ["ENPRACTICE_HOME"] = str(HOME)

# ---------------------------------------------------------------------------
section("[8] JSON 可序列化（FastAPI 要能直接吐出去）")
# ---------------------------------------------------------------------------
try:
    s = json.dumps(data, ensure_ascii=False)
    check("build_analytics 的返回值可直接 JSON 序列化", True)
    check("序列化后不含 NaN / Infinity（那会让前端 JSON.parse 失败）",
          "NaN" not in s and "Infinity" not in s)
except (TypeError, ValueError) as exc:
    check("build_analytics 的返回值可直接 JSON 序列化", False, repr(exc))

print("\n" + "=" * 70)
failed = [n for n, ok in results if not ok]
for n, ok in results:
    if not ok:
        print(f"  FAIL: {n}")
print(f"总计：{'全部通过' if not failed else f'{len(failed)} 项失败'}（{len(results)} 项）")
print("=" * 70)
sys.exit(1 if failed else 0)