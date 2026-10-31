"""词汇关联库：把若干词条放进同一个「关联组」，用于展示近义 / 相关词。

数据模型是**组（entry_links）+ 成员（entry_link_items）**，而不是
「entry_a / entry_b」两列。原因：需求要「两个或以上词条」且支持一对多，
两列模型下 A↔B↔C 得存三行，且「解除其中一对」时判定该不该删组很别扭；
组模型下 N 个成员只存 N 行，语义唯一。

★ 双向是查询结果的自然属性，不是需要维护的冗余数据 ★
查某词条的关联词条用自连接找同组其他成员，A 关联 B 则查 A 得 B、查 B 得 A，
物理上只有一组对应关系，**天然双向一致，不存在反向行失步的可能**。
请不要「好心」去加反向写入——那才是失步风险的来源。
"""

from __future__ import annotations

import sqlite3

from . import entries

# 关系类型白名单。刻意不加 SQL CHECK：SQLite 改不了表约束，一改就得重建表
# （passages.origin 就是这么被坑过）。后续加「反义词 / 派生 / 搭配」只改这里。
RELATIONS: dict[str, str] = {
    "synonym": "近义词",
    # 反义词：与近义词同为**无向同伴**关系，同属 entry_links 组模型。
    # 画布上画成红色 OPP 标记的线（近似词是蓝色 SYM）。
    "antonym": "反义词",
}

# 单个关联组的成员上限。防止一次关联几百个词把释义栏撑爆
# （每个成员的释义栏都会列出同组其他全部成员，是 N² 级展示量）。
MAX_MEMBERS = 12

# 单页关联组数量默认/上限，与词条列表保持同一量级
MAX_PAGE_SIZE = 100


class LinkError(entries.AppError):
    """关联库业务错误，映射为 HTTP 400。"""


class LinkNotFoundError(LinkError):
    def __init__(self, link_id: int) -> None:
        super().__init__(f"关联组不存在或已被解散（ID={link_id}）")
        self.link_id = int(link_id)


def _label(relation: str) -> str:
    return RELATIONS.get(relation, relation)


def _normalize_relation(relation: object) -> str:
    text = "" if relation is None else str(relation).strip()
    if not text:
        raise LinkError("请选择关联关系")
    if text not in RELATIONS:
        raise LinkError(f"未知的关联关系：{text}（当前支持：{'、'.join(RELATIONS.values())}）")
    return text


def _normalize_member_ids(entry_ids: object) -> list[int]:
    """校验并去重成员 ID 列表：至少 2 个、至多 MAX_MEMBERS 个。"""
    if entry_ids is None:
        raise LinkError("请至少选择 2 个词条")
    if isinstance(entry_ids, (str, bytes)):
        raise LinkError("成员列表格式不正确")
    try:
        raw = list(entry_ids)
    except TypeError as exc:
        raise LinkError("成员列表格式不正确") from exc

    seen: set[int] = set()
    ids: list[int] = []
    for item in raw:
        if isinstance(item, bool):
            raise LinkError("成员列表格式不正确")
        try:
            value = int(item)
        except (TypeError, ValueError) as exc:
            raise LinkError(f"成员 ID 必须是整数，收到：{item}") from exc
        if value not in seen:
            seen.add(value)
            ids.append(value)

    if len(ids) < 2:
        raise LinkError("建立关联至少需要 2 个不同的词条")
    if len(ids) > MAX_MEMBERS:
        raise LinkError(f"单个关联最多 {MAX_MEMBERS} 个词条，当前 {len(ids)} 个")
    return ids


def _assert_entries_exist(conn: sqlite3.Connection, ids: list[int]) -> None:
    """成员必须都存在——否则关联组里会挂着已删除词条的幽灵 ID。"""
    for entry_id in ids:
        row = conn.execute("SELECT id FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            raise entries.EntryNotFoundError(entry_id)


def _members_of(conn: sqlite3.Connection, link_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT e.id AS id, e.term AS term, e.meaning_zh AS meaning_zh, e.type AS type "
        "FROM entry_link_items i JOIN entries e ON e.id = i.entry_id "
        "WHERE i.link_id = ? ORDER BY e.term",
        (link_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _link_to_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    link_id = row["id"]
    return {
        "id": link_id,
        "relation": row["relation"],
        "relation_label": _label(row["relation"]),
        "note": row["note"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"] if "updated_at" in row.keys() else row["created_at"],
        "members": _members_of(conn, link_id),
    }


def groups_within(conn: sqlite3.Connection, entry_ids: object) -> list[dict]:
    """给定一批词条 id，返回「至少有 2 个成员落在这批 id 里」的关联组。

    用途：层级块画布除了父子边，还要把树内成员之间的近义 / 反义**横向**画出来。
    由后端一次算好塞进 /api/nodes/{id}/tree 的响应，前端不必为每个节点各发一次
    /api/entries/{id}/links（20 个节点 = 20 次往返）。

    只保留 ≥2 个成员在集合内的组：只有 1 个成员在树里时连一条边都画不出，
    带上它纯属让前端多遍历一次。成员列表仍返回**全部**成员——「树里有没有这个
    节点」是前端的渲染判断，不该由后端替它决定。
    """
    ids = sorted({int(x) for x in (entry_ids or [])})
    if len(ids) < 2:
        return []
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT DISTINCT l.id AS id, l.relation AS relation, l.note AS note "
        "FROM entry_links l JOIN entry_link_items i ON i.link_id = l.id "
        "WHERE l.id IN ("
        "  SELECT link_id FROM entry_link_items WHERE entry_id IN (" + marks + ")"
        ") ORDER BY l.id",
        ids,
    ).fetchall()

    inside = set(ids)
    out: list[dict] = []
    for row in rows:
        members = _members_of(conn, int(row["id"]))
        if sum(1 for m in members if int(m["id"]) in inside) < 2:
            continue
        out.append(
            {
                "id": int(row["id"]),
                "relation": row["relation"],
                "relation_label": _label(row["relation"]),
                "note": row["note"] or "",
                "members": members,
            }
        )
    return out


def create_link(
    conn: sqlite3.Connection,
    entry_ids: object,
    relation: object = "synonym",
    note: object = "",
) -> dict:
    """新建一个关联组。成员重复由主键 (link_id, entry_id) 挡掉。"""
    ids = _normalize_member_ids(entry_ids)
    rel = _normalize_relation(relation)
    text = "" if note is None else str(note).strip()

    entries._begin_write(conn)
    try:
        _assert_entries_exist(conn, ids)
        now = entries.now_iso()
        cur = conn.execute(
            "INSERT INTO entry_links (relation, note, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (rel, text, now, now),
        )
        link_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO entry_link_items (link_id, entry_id) VALUES (?, ?)",
            [(link_id, eid) for eid in ids],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_link(conn, link_id)


def get_link(conn: sqlite3.Connection, link_id: int) -> dict:
    row = conn.execute("SELECT * FROM entry_links WHERE id = ?", (int(link_id),)).fetchone()
    if row is None:
        raise LinkNotFoundError(link_id)
    return _link_to_dict(conn, row)


def list_links(conn: sqlite3.Connection, page: int = 1, page_size: int = 20) -> dict:
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    total = conn.execute("SELECT COUNT(*) FROM entry_links").fetchone()[0]
    rows = conn.execute(
        "SELECT * FROM entry_links ORDER BY id DESC LIMIT ? OFFSET ?",
        (page_size, (page - 1) * page_size),
    ).fetchall()
    return {
        "items": [_link_to_dict(conn, r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def update_link(
    conn: sqlite3.Connection,
    link_id: int,
    relation: object = None,
    note: object = None,
    entry_ids: object = None,
) -> dict:
    """局部更新关联组。

    三个参数都是 **None = 不改动**（沿用条目编辑的同一套语义）。若把默认值写成
    "" 或 []，「只改关系」会把备注和成员一并抹掉——本项目已在 extra_info /
    terms 上踩过两次同样的坑。
    """
    link_id = int(link_id)
    if conn.execute("SELECT id FROM entry_links WHERE id = ?", (link_id,)).fetchone() is None:
        raise LinkNotFoundError(link_id)

    rel = None if relation is None else _normalize_relation(relation)
    text = None if note is None else str(note).strip()
    ids = None if entry_ids is None else _normalize_member_ids(entry_ids)

    entries._begin_write(conn)
    try:
        if ids is not None:
            _assert_entries_exist(conn, ids)
        now = entries.now_iso()
        if rel is not None or text is not None:
            cur_rel = conn.execute(
                "SELECT relation, note FROM entry_links WHERE id = ?", (link_id,)
            ).fetchone()
            conn.execute(
                "UPDATE entry_links SET relation = ?, note = ?, updated_at = ? WHERE id = ?",
                (
                    rel if rel is not None else cur_rel["relation"],
                    text if text is not None else cur_rel["note"],
                    now,
                    link_id,
                ),
            )
        if ids is not None:
            # 成员集合全量替换：先删后插，让「移除全部再重加」与「直接给新集合」等价
            conn.execute("DELETE FROM entry_link_items WHERE link_id = ?", (link_id,))
            conn.executemany(
                "INSERT INTO entry_link_items (link_id, entry_id) VALUES (?, ?)",
                [(link_id, eid) for eid in ids],
            )
            conn.execute(
                "UPDATE entry_links SET updated_at = ? WHERE id = ?", (now, link_id)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_link(conn, link_id)


def remove_member(conn: sqlite3.Connection, link_id: int, entry_id: object) -> dict:
    """从关联组移除一个成员。

    移除后若组内只剩不到 2 人，整组连带解散——只剩 1 人的「关联」没有任何
    展示意义，留着只是空壳数据，还会污染列表与统计。
    """
    link_id = int(link_id)
    if conn.execute("SELECT id FROM entry_links WHERE id = ?", (link_id,)).fetchone() is None:
        raise LinkNotFoundError(link_id)
    try:
        target = int(entry_id)
    except (TypeError, ValueError) as exc:
        raise LinkError(f"成员 ID 必须是整数，收到：{entry_id}") from exc

    # 必须先确认它真是这个组的成员。少了这一步，「移出一个不在组里的词条」会走到下面的
    # remaining <= 2 分支，把整组误删——那是一次不可逆的数据丢失，而且调用方看不出来。
    member = conn.execute(
        "SELECT 1 FROM entry_link_items WHERE link_id = ? AND entry_id = ?",
        (link_id, target),
    ).fetchone()
    if member is None:
        raise LinkError(f"该条目不在这个关联组内（组 ID={link_id}，条目 ID={target}）")

    entries._begin_write(conn)
    try:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM entry_link_items WHERE link_id = ?", (link_id,)
        ).fetchone()[0]
        if remaining <= 2:
            # 2 人组删掉 1 个就只剩 1 人，直接解散而不是留下空壳
            conn.execute("DELETE FROM entry_links WHERE id = ?", (link_id,))
            conn.commit()
            return {"link_id": link_id, "removed": target, "dissolved": True}
        conn.execute(
            "DELETE FROM entry_link_items WHERE link_id = ? AND entry_id = ?",
            (link_id, target),
        )
        conn.execute(
            "UPDATE entry_links SET updated_at = ? WHERE id = ?", (entries.now_iso(), link_id)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"link_id": link_id, "removed": target, "dissolved": False}


def delete_link(conn: sqlite3.Connection, link_id: int) -> int:
    """解散整组；成员行由外键 ON DELETE CASCADE 清理。"""
    link_id = int(link_id)
    row = conn.execute("SELECT id FROM entry_links WHERE id = ?", (link_id,)).fetchone()
    if row is None:
        raise LinkNotFoundError(link_id)
    conn.execute("DELETE FROM entry_links WHERE id = ?", (link_id,))
    conn.commit()
    return link_id


def links_of_entry(conn: sqlite3.Connection, entry_id: int) -> list[dict]:
    """取某词条的全部关联词条（自连接找同组其他成员）。

    这是「双向」的全部实现：A 与 B 同组，查 A 得 B、查 B 得 A，
    不需要任何反向行的维护。
    """
    rows = conn.execute(
        "SELECT DISTINCT l.id AS link_id, l.relation AS relation, "
        "e2.id AS id, e2.term AS term, e2.meaning_zh AS meaning_zh, e2.type AS type "
        "FROM entry_link_items i1 "
        "JOIN entry_link_items i2 ON i2.link_id = i1.link_id AND i2.entry_id <> i1.entry_id "
        "JOIN entry_links l ON l.id = i1.link_id "
        "JOIN entries e2 ON e2.id = i2.entry_id "
        "WHERE i1.entry_id = ? "
        "ORDER BY l.id DESC, e2.term",
        (int(entry_id),),
    ).fetchall()
    return [
        {
            "link_id": r["link_id"],
            "relation": r["relation"],
            "relation_label": _label(r["relation"]),
            "entry": {
                "id": r["id"],
                "term": r["term"],
                "meaning_zh": r["meaning_zh"],
                "type": r["type"],
            },
        }
        for r in rows
    ]


def prune_degenerate_links(conn: sqlite3.Connection) -> int:
    """清掉成员不足 2 人的关联组（删除词条后由外键 CASCADE 产生的残余）。

    返回清理掉的组数。删除词条时调用：成员行会被 CASCADE 清掉，但组本身还在，
    留着就是查不到任何同伴的死组。
    """
    rows = conn.execute(
        "SELECT id FROM entry_links WHERE id NOT IN ("
        "  SELECT link_id FROM entry_link_items GROUP BY link_id HAVING COUNT(*) >= 2"
        ")"
    ).fetchall()
    for r in rows:
        conn.execute("DELETE FROM entry_links WHERE id = ?", (r["id"],))
    if rows:
        conn.commit()
    return len(rows)
