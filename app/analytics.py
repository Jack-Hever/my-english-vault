"""学习进度分析：30 天趋势、热门标签、新词、新外刊。

设计要点：
- **只读**。这个模块不写任何表，全部是聚合查询。
- **统计在 SQL 里做**，不把整表拉到 Python 内存再数。
- **空数据返回结构完整的空结果**，前端不需要为"没有数据"写分支。
- **日期按本地时间口径**：库里存的是 `2026-10-17T22:01:46` 这种无时区的
  ISO 串，直接用 `substr(created_at, 1, 10)` 取日期，与库里写进去的
  时间戳保持同一口径（写入时也是本地时间，见 entries.now_iso）。
"""

from __future__ import annotations

import datetime

from . import db

#: 趋势窗口长度（天）。含今天，往前推 29 天，共 30 个点。
TREND_DAYS = 30

#: 新词列表最多返回多少条。词条是"逐条看"的列表，取太多就失去摘要意义。
MAX_RECENT_ENTRIES = 12

#: 新外刊列表最多返回多少篇。
MAX_RECENT_ARTICLES = 8


def _today() -> str:
    """服务器当天，YYYY-MM-DD。

    刻意不硬编码日期 —— 这个接口每次调用都要反映"到今天为止"的窗口。
    """
    return datetime.date.today().isoformat()


def _date_window(days: int = TREND_DAYS) -> list[str]:
    """返回 [today - days + 1, ..., today] 的连续日期序列（升序）。

    ★ 必须连续无缺口 ★
    前端把这份序列直接当 X 轴。如果中间缺日期，柱子的位置就会错位
    （第 3 根柱子被当成第 5 天），趋势图会读出完全错误的结论。
    """
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days - 1)
    return [(start + datetime.timedelta(days=i)).isoformat() for i in range(days)]


def _daily_practice(conn) -> dict[str, dict]:
    """按天聚合练习次数与平均分。

    平均分用 `AVG(score_total)`：没练过的日子根本不会有行，
    所以"有练习的日子"才有值 —— 空档期在趋势图上就是断点，不是 0 分。
    """
    rows = conn.execute(
        """SELECT substr(created_at, 1, 10) AS d,
                  COUNT(*) AS cnt,
                  AVG(score_total) AS avg_score
             FROM practices
            GROUP BY d"""
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        out[r["d"]] = {
            "count": int(r["cnt"]),
            # AVG 在 SQLite 里对全 NULL 样本返回 NULL；转成 None 让前端区分
            #「这天没练习」与「这天练了但总分为 0」
            "avg_score": (round(float(r["avg_score"]), 1)
                          if r["avg_score"] is not None else None),
        }
    return out


def _daily_entries(conn) -> dict[str, int]:
    rows = conn.execute(
        "SELECT substr(created_at, 1, 10) AS d, COUNT(*) AS cnt "
        "FROM entries GROUP BY d"
    ).fetchall()
    return {r["d"]: int(r["cnt"]) for r in rows}


def _daily_articles(conn) -> dict[str, int]:
    rows = conn.execute(
        "SELECT substr(added_at, 1, 10) AS d, COUNT(*) AS cnt "
        "FROM articles GROUP BY d"
    ).fetchall()
    return {r["d"]: int(r["cnt"]) for r in rows}


def build_analytics(conn, days: int = TREND_DAYS) -> dict:
    """汇总学习进度分析数据。

    返回结构固定，**空库也返回完整的键**（列表为空、数值为 0），
    这样前端只要按结构渲染，不用判断"字段在不在"。
    """
    window = _date_window(days)
    prac = _daily_practice(conn)
    ents = _daily_entries(conn)
    arts = _daily_articles(conn)

    trend = []
    for d in window:
        p = prac.get(d, {"count": 0, "avg_score": None})
        trend.append({
            "date": d,
            "practice_count": p["count"],
            # None 表示这天没练习（不是 0 分）。前端据此把折线断开，
            # 而不是把 0 分画进折线里 —— 那会让"没练"看起来像"考了 0 分"。
            "avg_score": p["avg_score"],
            "new_entries": ents.get(d, 0),
            "new_articles": arts.get(d, 0),
        })

    tags = [
        {"name": r["name"], "article_count": int(r["n"])}
        for r in conn.execute(
            """SELECT t.name AS name, COUNT(at.article_id) AS n
                 FROM tags t
                 JOIN article_tags at ON at.tag_id = t.id
                GROUP BY t.id, t.name
               HAVING COUNT(at.article_id) > 0
                ORDER BY n DESC, t.name ASC"""
        ).fetchall()
    ]

    recent_entries = [
        {
            "id": int(r["id"]),
            "term": r["term"],
            "meaning_zh": r["meaning_zh"] or "",
            "type": r["type"] or "word",
            "source": r["source"] or "",
            "created_at": r["created_at"] or "",
        }
        for r in conn.execute(
            """SELECT id, term, meaning_zh, type, source, created_at
                 FROM entries
                ORDER BY created_at DESC, id DESC
                LIMIT ?""",
            (MAX_RECENT_ENTRIES,),
        ).fetchall()
    ]

    # 句数用子查询一次取出，避免 N+1 查询
    recent_articles = [
        {
            "id": int(r["id"]),
            "title": r["title"] or "",
            "title_zh": r["title_zh"] or "",
            "source_name": r["source_name"] or "",
            "added_at": r["added_at"] or "",
            "sentence_count": int(r["sentence_count"] or 0),
        }
        for r in conn.execute(
            """SELECT a.id, a.title, a.title_zh, a.source_name, a.added_at,
                      (SELECT COUNT(*) FROM article_sentences s
                        WHERE s.article_id = a.id) AS sentence_count
                 FROM articles a
                ORDER BY a.added_at DESC, a.id DESC
                LIMIT ?""",
            (MAX_RECENT_ARTICLES,),
        ).fetchall()
    ]

    # 概览数字：与趋势窗口无关的累计值
    # ★ `SELECT COUNT(*) FROM practices` 只查一次 ★
    # 改造前这里查了两次（一次给 total_practices、一次给 avg_score_all 判空），
    # 同一个请求同一张表，第二次是纯浪费。
    total_practices = int(conn.execute("SELECT COUNT(*) FROM practices").fetchone()[0])
    overview = {
        "total_entries": int(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]),
        "total_articles": int(conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]),
        "total_practices": total_practices,
        # AVG 仍留在条件里：一条练习都没有时不查它（空集上算 AVG 没有意义）
        "avg_score_all": (
            round(float(conn.execute(
                "SELECT AVG(score_total) FROM practices").fetchone()[0]), 1)
            if total_practices else None
        ),
        "active_days": len([d for d in window if prac.get(d, {}).get("count", 0) > 0]),
        "window_days": days,
    }

    return {
        "today": _today(),
        "overview": overview,
        "trend": trend,
        "hot_tags": tags,
        "recent_entries": recent_entries,
        "recent_articles": recent_articles,
    }


def get_analytics(days: int = TREND_DAYS) -> dict:
    """对外入口：自己开连接、自己关。"""
    conn = db.connect()
    try:
        return build_analytics(conn, days)
    finally:
        conn.close()
