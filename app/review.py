"""复习调度：间隔重复（SM-2 简化版）与按添加日期排序两种模式。

间隔表（SM-2 简化）：
- 第 1 次记住 → 1 天后
- 第 2 次记住 → 2 天后
- 第 3 次起  → 上次间隔 × easiness，四舍五入取整天
- 没记住    → 间隔归零重新开始，easiness 下调 0.2（下限 1.3），lapses +1

easiness 初始 2.5，随连续记住小幅上调（+0.1，上限 2.8），
随遗忘下调（-0.2，下限 1.3）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from .db import DEFAULT_EASINESS
from .entries import EntryError, now_iso

# easiness 上下限
EASINESS_MIN = 1.3
EASINESS_MAX = 2.8
EASINESS_UP = 0.1
EASINESS_DOWN = 0.2

MODE_INTERVAL = "interval"
MODE_DATE_ORDER = "date_order"
MODES = (MODE_INTERVAL, MODE_DATE_ORDER)


def due_entries(
    conn: sqlite3.Connection, *, mode: str = MODE_INTERVAL, limit: int = 20, offset: int = 0
) -> list[dict]:
    """取待复习条目。

    - interval 模式：due_at <= now 的条目，按 due_at 升序（最该复习的在前）
    - date_order 模式：全部条目按 created_at 升序（最早添加的优先）
    """
    if mode not in MODES:
        raise EntryError(f"复习模式只能是 interval 或 date_order，收到：{mode}")
    if limit < 1:
        raise EntryError(f"limit 必须大于等于 1，收到：{limit}")

    now = now_iso()
    # 两处 SELECT 都带上 importance / usefulness：练习选词要按评分加权排序，
    # 缺了这两列后端就得回头再查一次库。
    if mode == MODE_DATE_ORDER:
        rows = conn.execute(
            """SELECT e.id, e.type, e.term, e.pos, e.meaning_zh, e.usage, e.source,
                      e.created_at, e.importance, e.usefulness,
                      r.due_at, r.reps, r.interval_days, r.easiness
               FROM entries e JOIN review_state r ON r.entry_id = e.id
               ORDER BY e.created_at ASC, e.id ASC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT e.id, e.type, e.term, e.pos, e.meaning_zh, e.usage, e.source,
                      e.created_at, e.importance, e.usefulness,
                      r.due_at, r.reps, r.interval_days, r.easiness
               FROM entries e JOIN review_state r ON r.entry_id = e.id
               WHERE r.due_at <= ?
               ORDER BY r.due_at ASC, e.id ASC
               LIMIT ? OFFSET ?""",
            (now, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def due_count(conn: sqlite3.Connection) -> int:
    """间隔重复模式下当前到期（含逾期）的条目数，用于侧栏提示与到点提醒。"""
    return conn.execute(
        "SELECT COUNT(*) FROM review_state WHERE due_at <= ?", (now_iso(),)
    ).fetchone()[0]


def new_entries_since(
    conn: sqlite3.Connection, since_iso: str, limit: int = 20
) -> list[dict]:
    """取某个时间点之后新增的条目（"新学的词"）。"""
    rows = conn.execute(
        """SELECT e.id, e.type, e.term, e.pos, e.meaning_zh, e.usage, e.source,
                  e.created_at, e.importance, e.usefulness
           FROM entries e WHERE e.created_at > ? ORDER BY e.created_at DESC LIMIT ?""",
        (since_iso, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_reviewed(conn: sqlite3.Connection, entry_id: int, remembered: bool) -> dict:
    """记录一次复习结果，更新间隔与 easiness，返回新的复习状态。"""
    row = conn.execute(
        """SELECT e.id, e.term, r.easiness, r.interval_days, r.reps, r.lapses
           FROM entries e JOIN review_state r ON r.entry_id = e.id WHERE e.id = ?""",
        (entry_id,),
    ).fetchone()
    if row is None:
        raise EntryError(f"条目不存在或没有复习状态（ID={entry_id}）")

    easiness = float(row["easiness"])
    interval = int(row["interval_days"])
    reps = int(row["reps"])

    if remembered:
        if reps == 0:
            interval = 1
        elif reps == 1:
            interval = 2
        else:
            interval = max(1, round(interval * easiness))
        reps += 1
        easiness = min(EASINESS_MAX, easiness + EASINESS_UP)
    else:
        interval = 0
        reps = 0
        easiness = max(EASINESS_MIN, easiness - EASINESS_DOWN)

    now = datetime.now()
    due_at = (now + timedelta(days=interval)).isoformat(timespec="seconds")
    now_iso_str = now.isoformat(timespec="seconds")

    conn.execute(
        """UPDATE review_state
           SET easiness = ?, interval_days = ?, reps = ?, due_at = ?, last_reviewed_at = ?
           WHERE entry_id = ?""",
        (easiness, interval, reps, due_at, now_iso_str, entry_id),
    )
    conn.execute(
        "INSERT INTO review_log (entry_id, reviewed_at, result) VALUES (?, ?, ?)",
        (entry_id, now_iso_str, "remembered" if remembered else "forgot"),
    )
    conn.commit()
    return {
        "entry_id": entry_id,
        "term": row["term"],
        "remembered": remembered,
        "interval_days": interval,
        "reps": reps,
        "easiness": round(easiness, 2),
        "due_at": due_at,
    }


def seed_review_state(conn: sqlite3.Connection, entry_id: int) -> None:
    """为单个条目播种复习状态（供新增条目时调用；幂等）。"""
    exists = conn.execute(
        "SELECT 1 FROM review_state WHERE entry_id = ?", (entry_id,)
    ).fetchone()
    if exists:
        return
    created = conn.execute(
        "SELECT created_at FROM entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if created is None:
        return
    conn.execute(
        """INSERT INTO review_state
           (entry_id, easiness, interval_days, reps, lapses, due_at, last_reviewed_at)
           VALUES (?, ?, 0, 0, 0, ?, NULL)""",
        (entry_id, DEFAULT_EASINESS, created["created_at"]),
    )
