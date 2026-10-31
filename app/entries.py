"""词汇 / 短语条目的业务逻辑层（P1）。

设计约定：
- 所有 SQL 一律参数化查询，禁止把用户输入拼进 SQL 文本；
- 表名、列名、排序方向只能取自本模块的常量白名单；
- 业务校验失败抛 AppError 子类，其 `str()` 可直接展示给终端用户，
  由 main.py 的异常处理器映射成对应的 HTTP 状态码；
- 数据访问函数接收外部传入的 sqlite3.Connection，便于单测与事务复用。
"""

from __future__ import annotations

import math
from datetime import datetime

from .db import DEFAULT_EASINESS, normalize_term

# ---------------- 白名单常量（SQL 文本只允许引用这里的值） ----------------

ENTRY_TYPES = ("word", "phrase")
TYPE_LABELS = {"word": "单词", "phrase": "短语"}

#: 关键词模糊搜索覆盖的字段：词条 / 中文释义 / 例句
SEARCH_COLUMNS = ("term", "meaning_zh", "usage")

SORT_COLUMNS = {
    "id": "id",
    "term": "term",
    "created_at": "created_at",
    "updated_at": "updated_at",
}
SORT_ORDERS = {"asc": "ASC", "desc": "DESC"}
# 注意：评分列 importance / usefulness 刻意不进排序白名单。
# 一是用户尚未提出「按评分排序」的需求；二是这两列可为 NULL，而 SQLite 里 NULL 恒小于
# 任何实数，升序时未评分条目会全部堆在最前——要放开就得显式写
# ORDER BY ... NULLS LAST（需 SQLite 3.30+），属于一个独立决策，留到有需求时再做。

#: 条目评分的两维字段（顺序固定，批量处理时按此遍历）
RATING_FIELDS = ("importance", "usefulness")
RATING_LABELS = {"importance": "重要度", "usefulness": "有用度"}

#: 评分区间与步进（半星：0 / 0.5 / 1.0 … 5.0）
RATING_MIN = 0.0
RATING_MAX = 5.0
RATING_STEP = 0.5

#: 高分档位阈值：任一维度评分达到该值即参与高亮展示（OR 语义）
HIGHLIGHT_THRESHOLD = 4.0

#: 未评分条目参与「按评分加权练习频次」时的兜底取值（区间的中位数）。
#: 用户已确认评分影响练习频次约 3 倍差；未评分按中位数走，既不奖励也不惩罚——
#: 否则「还没评过」会被当成 0 分，刚录入的词反而永远排不上练习。
#: 具体加权公式属 P2 练习模块，本批次只把口径定在这里，不接线。
DEFAULT_RATING = 3.0

#: 导入重复项处理策略
DUPLICATE_POLICIES = ("skip", "overwrite")

FIELD_MAX_LEN = {"term": 200, "pos": 60, "meaning_zh": 2000, "usage": 4000, "source": 500}
FIELD_LABELS = {
    "term": "词条",
    "pos": "词性",
    "meaning_zh": "中文释义",
    "usage": "例句",
    "source": "来源",
}

EXPORT_APP = "EnPractice"
EXPORT_FORMAT_VERSION = 1

#: 列表默认分页
DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200

#: 导入结果里最多回传多少条错误明细，避免响应体过大
MAX_IMPORT_ERRORS = 20

# SQL 片段（仅使用白名单列名拼装，不含任何用户输入）
_ENTRY_SELECT = (
    "SELECT e.id, e.type, e.term, e.pos, e.meaning_zh, e.usage, e.source, "
    "e.importance, e.usefulness, "
    "e.created_at, e.updated_at FROM entries e"
)
_SEARCH_SQL = " OR ".join(f"e.{col} LIKE ? ESCAPE '\\'" for col in SEARCH_COLUMNS)


# ---------------- 异常定义 ----------------


class AppError(ValueError):
    """业务校验失败基类。消息面向终端用户，可直接回显。"""


class EntryError(AppError):
    """条目相关校验失败。"""


class DuplicateEntryError(EntryError):
    """同类型下已存在相同词条（忽略大小写与首尾空格）。"""

    def __init__(self, term: str, entry_type: str, entry_id: int) -> None:
        label = TYPE_LABELS.get(entry_type, entry_type)
        super().__init__(f"已存在相同的{label}「{term}」（ID={entry_id}），请勿重复录入")
        self.term = term
        self.type = entry_type
        self.entry_id = int(entry_id)


class EntryNotFoundError(EntryError):
    def __init__(self, entry_id: int) -> None:
        super().__init__(f"条目不存在或已被删除（ID={entry_id}）")
        self.entry_id = int(entry_id)


# ---------------- 工具函数 ----------------


def now_iso() -> str:
    """本地时间 ISO-8601 字符串（秒精度），字典序即时间序。"""
    return datetime.now().isoformat(timespec="seconds")


def normalize_date_bound(value: object, *, upper: bool) -> str:
    """把日期边界补全为可与 created_at 直接比较的字符串。

    upper=True 时表示上界（补到当天 23:59:59.999999），否则补到 00:00:00。
    只生成参数值，不参与 SQL 文本拼装。
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    if len(text) == 10:  # YYYY-MM-DD
        # 必须校验：非法日期（如 2024-13-45）若直接参与字符串比较会静默返回错误结果
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError as exc:
            raise EntryError(f"日期格式不正确，应为 YYYY-MM-DD：{text}") from exc
        return text + ("T23:59:59.999999" if upper else "T00:00:00")
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise EntryError(f"日期格式不正确，应为 YYYY-MM-DD 或 ISO-8601：{text}") from exc
    return text


def _like_pattern(keyword: str) -> str:
    """转义 LIKE 通配符后生成 %kw% 模式串（仅作为参数值使用）。"""
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _begin_write(conn) -> None:
    """开启 IMMEDIATE 写事务，让「查重 + 写入」在同一个 SQLite 写锁内原子完成。

    并发下（例如前端双击「保存」发出两个 POST）若少了这一步，两个请求可能都查不到
    重复项而各插一条。嵌套调用（如 import_entries 以 commit=False 复用同一连接）
    会沿用已打开的事务，不会重复 BEGIN。
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def _clean(value: object, field: str) -> str:
    """去空白并做长度校验，超长直接报错而不是静默截断。"""
    text = "" if value is None else str(value).strip()
    max_len = FIELD_MAX_LEN[field]
    if len(text) > max_len:
        raise EntryError(
            f"{FIELD_LABELS[field]}长度不能超过 {max_len} 个字符（当前 {len(text)} 个）"
        )
    return text


def _opt_float(value: object) -> float | None:
    """把列值转成 float；NULL 保持 None（「未评分」不能被压成 0）。"""
    if value is None:
        return None
    return float(value)


def normalize_rating(value: object, field: str) -> float | None:
    """校验并规范化一个评分字段，返回 float 或 None（未评分 / 显式清除）。

    - None 与空串 → None（清除评分）
    - 非数字、越界、非半星步进 → 抛 EntryError，消息里带上当前值便于定位
    """
    label = RATING_LABELS.get(field, field)
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool):  # bool 是 int 的子类，不能当评分收
        raise EntryError(f"{label}必须是数字，收到：{value}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise EntryError(f"{label}必须是数字，收到：{value}") from exc
    if not math.isfinite(number):
        raise EntryError(f"{label}必须是有限数字，收到：{value}")
    if number < RATING_MIN or number > RATING_MAX:
        raise EntryError(
            f"{label}应在 {RATING_MIN:g} ~ {RATING_MAX:g} 之间，当前 {number:g}"
        )
    # 半星判定：乘 2 后应为整数。用 1e-9 容差吸收浮点误差（如 0.5*2=1.0 精确，
    # 但 2.5 经 JSON 往返仍是精确的，容差只为防御累积误差）。
    if abs(number * 2 - round(number * 2)) > 1e-9:
        raise EntryError(f"{label}只能是半星（{RATING_STEP:g} 的倍数），当前 {number:g}")
    return float(number)


def is_highlight(entry: dict) -> bool:
    """高亮判定：任一维度达到高分档位即参与高亮。

    用 OR 而非 AND —— 需求原文是「单个属性获得高分（如达到设定档位）的词条
    即可参与高亮展示」。写成 AND 会让「重要性 5 星、实用性未评」这类条目
    落不到高亮，与「单项够高就该被看见」的意图相反。

    未评分（None）不参与判定：它代表「还没评价过」，既不能当高分，
    也不该因为另一维未评分就把已有的高分抹掉。
    """
    imp, use = entry.get("importance"), entry.get("usefulness")
    return (imp is not None and imp >= HIGHLIGHT_THRESHOLD) or (
        use is not None and use >= HIGHLIGHT_THRESHOLD
    )


def normalize_entry_payload(raw: object) -> dict:
    """校验并规范化条目入参（新增 / 编辑 / 导入共用）。

    评分字段只在**请求里显式出现**时才放进返回字典——「键不存在」与「显式 null」
    必须区分得开，否则导入一份没有评分字段的旧版 JSON 去覆盖已有条目时，
    会把用户已经打好的分静默清成「未评分」。清除评分请显式传 null。
    """
    if not isinstance(raw, dict):
        raise EntryError("条目数据格式不正确，应为 JSON 对象")

    entry_type = str(raw.get("type") or "word").strip().lower()
    if entry_type not in ENTRY_TYPES:
        raise EntryError(
            f"条目类型只能是 word（单词）或 phrase（短语），收到：{entry_type or '空值'}"
        )

    term = _clean(raw.get("term"), "term")
    if not term:
        raise EntryError("词条不能为空")

    meaning_zh = _clean(raw.get("meaning_zh"), "meaning_zh")
    if not meaning_zh:
        raise EntryError("中文释义不能为空")

    pos = _clean(raw.get("pos"), "pos")
    if entry_type == "phrase":
        pos = ""  # 短语没有词性

    data = {
        "type": entry_type,
        "term": term,
        "pos": pos,
        "meaning_zh": meaning_zh,
        "usage": _clean(raw.get("usage"), "usage"),
        "source": _clean(raw.get("source"), "source"),
    }
    for field in RATING_FIELDS:
        if field in raw:
            data[field] = normalize_rating(raw.get(field), field)
    return data


def resolve_ratings(data: dict, existing: dict | None = None) -> tuple[float | None, float | None]:
    """算出最终要落库的两个评分值。

    - data 里显式给了（含显式 null）→ 用它，null 表示清除；
    - data 里没这个键 → 编辑时沿用原值（部分更新不改动），新增时视为未评分。
    """
    importance = data["importance"] if "importance" in data else (
        existing["importance"] if existing else None
    )
    usefulness = data["usefulness"] if "usefulness" in data else (
        existing["usefulness"] if existing else None
    )
    return importance, usefulness


# ---------------- 查询构建 ----------------


def build_filters(
    *,
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    entry_type: str = "",
) -> tuple[str, list]:
    """构建 WHERE 子句与参数列表。返回 ("a = ? AND b = ?", [...])。"""
    clauses: list[str] = []
    params: list = []

    keyword = (q or "").strip()
    if keyword:
        pattern = _like_pattern(keyword)
        clauses.append(f"({_SEARCH_SQL})")
        params.extend([pattern] * len(SEARCH_COLUMNS))

    entry_type = (entry_type or "").strip().lower()
    if entry_type:
        if entry_type not in ENTRY_TYPES:
            raise EntryError(
                f"类型筛选只能是 word（单词）或 phrase（短语），收到：{entry_type}"
            )
        clauses.append("e.type = ?")
        params.append(entry_type)

    date_from = normalize_date_bound(date_from, upper=False)
    if date_from:
        clauses.append("e.created_at >= ?")
        params.append(date_from)

    date_to = normalize_date_bound(date_to, upper=True)
    if date_to:
        clauses.append("e.created_at <= ?")
        params.append(date_to)

    return " AND ".join(clauses), params


def _where_sql(clauses: str) -> str:
    return f" WHERE {clauses}" if clauses else ""


def _entry_to_dict(row) -> dict:
    # 老连接可能来自未跑迁移的库（例如测试里手造的旧表），用 row.keys() 兜底取列
    keys = set(row.keys())
    importance = _opt_float(row["importance"]) if "importance" in keys else None
    usefulness = _opt_float(row["usefulness"]) if "usefulness" in keys else None
    ratings = {"importance": importance, "usefulness": usefulness}
    return {
        "id": int(row["id"]),
        "type": row["type"],
        "term": row["term"],
        "pos": row["pos"] or "",
        "meaning_zh": row["meaning_zh"],
        "usage": row["usage"] or "",
        "source": row["source"] or "",
        "importance": importance,
        "usefulness": usefulness,
        "highlight": is_highlight(ratings),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _rows_to_dicts(rows: list) -> list[dict]:
    return [_entry_to_dict(r) for r in rows]


def _order_sql(sort: str, order: str) -> str:
    column = SORT_COLUMNS.get(str(sort or "").lower(), "created_at")
    direction = SORT_ORDERS.get(str(order or "").lower(), "DESC")
    return f" ORDER BY e.{column} {direction}, e.id DESC"


# ---------------- 条目读写 ----------------


def list_entries(
    conn,
    *,
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    entry_type: str = "",
    page: int = DEFAULT_PAGE,
    page_size: int = DEFAULT_PAGE_SIZE,
    sort: str = "created_at",
    order: str = "desc",
) -> dict:
    """分页查询条目，返回 {"items": [...], "total": n, "page": p, "page_size": n}。"""
    page = max(1, int(page))
    page_size = min(MAX_PAGE_SIZE, max(1, int(page_size)))

    clauses, params = build_filters(
        q=q, date_from=date_from, date_to=date_to, entry_type=entry_type
    )
    where = _where_sql(clauses)

    total = int(conn.execute(f"SELECT COUNT(*) FROM entries e{where}", params).fetchone()[0])
    rows = conn.execute(
        f"{_ENTRY_SELECT}{where}{_order_sql(sort, order)} LIMIT ? OFFSET ?",
        params + [page_size, (page - 1) * page_size],
    ).fetchall()

    return {
        "items": _rows_to_dicts(rows),
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def get_entry(conn, entry_id: int) -> dict:
    row = conn.execute(f"{_ENTRY_SELECT} WHERE e.id = ?", (int(entry_id),)).fetchone()
    if row is None:
        raise EntryNotFoundError(entry_id)
    return _entry_to_dict(row)


def find_duplicate(conn, entry_type: str, term: str, exclude_id: int | None = None):
    """按「同类型 + 归一化后词条相同」查重。命中返回 (id, term)，否则返回 None。

    注意：这里走 SQL 的 norm_term()，它是 Python 注册的 UDF，无法命中 idx_entries_term
    索引，每次调用都是一次全表扫描。单条新增/编辑调用一次无妨，批量导入逐行调用会退化成
    O(n²)——属已知问题，治理方案与注意事项见 import_entries 上方的说明（留给 P2）。
    """
    sql = "SELECT id, term FROM entries WHERE type = ? AND norm_term(term) = ?"
    params: list = [entry_type, normalize_term(term)]
    if exclude_id is not None:
        sql += " AND id <> ?"
        params.append(int(exclude_id))
    sql += " ORDER BY id LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return (int(row["id"]), row["term"]) if row else None




def _seed_review_state(conn, entry_id: int, due_at: str) -> None:
    """新增条目时同步播种复习状态，为 P2 复习模块铺路。"""
    conn.execute(
        "INSERT OR IGNORE INTO review_state "
        "(entry_id, easiness, interval_days, reps, lapses, due_at, last_reviewed_at) "
        "VALUES (?, ?, 0, 0, 0, ?, NULL)",
        (int(entry_id), DEFAULT_EASINESS, due_at),
    )


def create_entry(conn, payload: object, *, commit: bool = True) -> dict:
    data = normalize_entry_payload(payload)
    _begin_write(conn)  # 查重与插入必须原子，否则并发下会出现同名条目
    duplicate = find_duplicate(conn, data["type"], data["term"])
    if duplicate:
        raise DuplicateEntryError(data["term"], data["type"], duplicate[0])

    importance, usefulness = resolve_ratings(data)  # 新增：没给就是「未评分」
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO entries (type, term, pos, meaning_zh, usage, source, "
        "importance, usefulness, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            data["type"],
            data["term"],
            data["pos"],
            data["meaning_zh"],
            data["usage"],
            data["source"],
            importance,
            usefulness,
            now,
            now,
        ),
    )
    entry_id = int(cur.lastrowid)
    _seed_review_state(conn, entry_id, now)
    # 同步播种节点行（需求 6）。层级边的两端都是 node_id，真实词条要能当子节点
    # 就必须有 node 行。OR IGNORE 兜住与 db._backfill_entry_nodes 撞车的情况
    # （唯一索引会挡住重复，OR IGNORE 让它变成无害）。
    conn.execute(
        "INSERT OR IGNORE INTO nodes (kind, label, note, entry_id, created_at, updated_at) "
        "VALUES ('entry', NULL, '', ?, ?, ?)",
        (entry_id, now, now),
    )
    if commit:
        conn.commit()
    return get_entry(conn, entry_id)


def update_entry(conn, entry_id: int, payload: object, *, commit: bool = True) -> dict:
    entry_id = int(entry_id)
    existing = get_entry(conn, entry_id)  # 不存在则抛 EntryNotFoundError(404)

    data = normalize_entry_payload(payload)
    # 部分更新语义：文本字段仍是全量替换（与导入 overwrite 一致），
    # 但评分字段若请求里没出现就沿用原值——避免"只改释义"把分数清成未评分。
    importance, usefulness = resolve_ratings(data, existing)
    _begin_write(conn)
    duplicate = find_duplicate(conn, data["type"], data["term"], exclude_id=entry_id)
    if duplicate:
        raise DuplicateEntryError(data["term"], data["type"], duplicate[0])

    now = now_iso()
    conn.execute(
        "UPDATE entries SET type = ?, term = ?, pos = ?, meaning_zh = ?, "
        "usage = ?, source = ?, importance = ?, usefulness = ?, updated_at = ? WHERE id = ?",
        (
            data["type"],
            data["term"],
            data["pos"],
            data["meaning_zh"],
            data["usage"],
            data["source"],
            importance,
            usefulness,
            now,
            entry_id,
        ),
    )
    if commit:
        conn.commit()
    return get_entry(conn, entry_id)


def update_ratings(conn, entry_id: int, ratings: dict, *, commit: bool = True) -> dict:
    """只改评分列，不动其它字段。

    为什么不能复用 update_entry：它走 normalize_entry_payload 的全量校验，
    term / type 是必填项，只传评分会被判「词条不能为空」。打分期望的就是
    「只动这两列」，所以要有一条独立的窄接口。

    ratings 的**键**表示本次要改的字段（不在键里的保持原值），值为 None 表示
    清除评分回到「未评分」。调用方必须自己区分「没传」与「显式传 null」
    （路由层用 model_dump(exclude_unset=True)），这里两者语义不同。
    """
    entry_id = int(entry_id)
    existing = get_entry(conn, entry_id)  # 不存在则抛 EntryNotFoundError(404)
    if not ratings:
        return existing

    sets: list[str] = []
    params: list[object] = []
    for field in RATING_FIELDS:  # 列名来自固定白名单，不接受外部输入
        if field not in ratings:
            continue
        sets.append(f"{field} = ?")
        params.append(normalize_rating(ratings[field], field))
    if not sets:
        return existing

    params.append(now_iso())
    params.append(entry_id)
    _begin_write(conn)
    conn.execute(
        "UPDATE entries SET " + ", ".join(sets) + ", updated_at = ? WHERE id = ?", params
    )
    if commit:
        conn.commit()
    return get_entry(conn, entry_id)


def delete_entry(conn, entry_id: int) -> dict:
    """删除条目；review_state / review_log 与关联成员行由外键 ON DELETE CASCADE 清理。

    返回 {"deleted", "tombstone"}：
      - tombstone.tombstoned 非 None → 该词条在概念树里有子项，节点已转成**占位节点**
        留着，子项仍挂在它下面（前端据此提示「已保留占位」）。见 nodes.handle_entry_deleted。
      - tombstone 为 None → 要么它在树上没有子项（不留无信息量的空壳），
        要么它压根没进过概念树。

    ★ 概念树留占位、近似词关联不留 ★
    两侧语义不同（树的成员是「结构」，关联组的成员是「引用本身」），详见 nodes.py
    模块注释里那一节。这里只负责把关联组那边清干净（prune）。
    """
    entry_id = int(entry_id)
    row = conn.execute("SELECT id FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise EntryNotFoundError(entry_id)
    # 概念树：必须在 DELETE 之前处理该词条的节点——nodes.entry_id 上有 CASCADE，
    # 直接 DELETE 会把节点行连同「父→子」的边一起抹掉，子项就变成孤儿根了。
    # 有子项时这里把它转成占位节点（entry_id 置 NULL，断开外键让 CASCADE 打不着），
    # 子项继续挂在它下面；无子项则零行改动，由 CASCADE 清掉。
    # nodes 依赖 entries（复用异常与 now_iso），模块级互相 import 会成环，
    # 所以与 links 一样放到函数内。
    from . import nodes as nodes_mod

    tombstone = nodes_mod.handle_entry_deleted(conn, entry_id)
    conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    conn.commit()
    # 条目没了，它所在的关联组可能只剩不到 2 人——那样的组查不到任何同伴，
    # 留着只是死数据。这里延迟导入：links 依赖 entries（复用异常与 now_iso），
    # 模块级互相 import 会成环，所以放到函数内。
    from . import links

    links.prune_degenerate_links(conn)
    return {"deleted": entry_id, "tombstone": tombstone}


# ---------------- 导入 / 导出 ----------------


def _to_export_item(entry: dict) -> dict:
    # 评分是**可选**附加字段：旧版导出没有这两键，旧版导入器不认识也会忽略，
    # 因此 format_version 保持 1 不变（新增的是可缺省字段，不是破坏性变更）。
    return {
        "type": entry["type"],
        "term": entry["term"],
        "pos": entry["pos"],
        "meaning_zh": entry["meaning_zh"],
        "usage": entry["usage"],
        "source": entry["source"],
        "importance": entry["importance"],
        "usefulness": entry["usefulness"],
        "created_at": entry["created_at"],
    }


def export_entries(
    conn,
    *,
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    entry_type: str = "",
    page: int | None = None,
    page_size: int | None = None,
) -> dict:
    """导出为可再次导入的 JSON 结构（支持复用当前筛选条件）。"""
    clauses, params = build_filters(
        q=q, date_from=date_from, date_to=date_to, entry_type=entry_type
    )
    sql = f"{_ENTRY_SELECT}{_where_sql(clauses)} ORDER BY e.created_at ASC, e.id ASC"
    if page is not None and page_size is not None:
        page = max(1, int(page))
        page_size = min(MAX_PAGE_SIZE, max(1, int(page_size)))
        sql += " LIMIT ? OFFSET ?"
        params += [page_size, (page - 1) * page_size]
    rows = conn.execute(sql, params).fetchall()
    items = _rows_to_dicts(rows)
    return {
        "app": EXPORT_APP,
        "format_version": EXPORT_FORMAT_VERSION,
        "exported_at": now_iso(),
        "count": len(items),
        "entries": [_to_export_item(item) for item in items],
    }


def parse_import_payload(payload: object) -> list:
    """接受 {"entries": [...]} 信封，也接受裸数组。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        entries = payload.get("entries")
        if isinstance(entries, list):
            return entries
    raise EntryError("导入文件格式不正确：需要 entries 数组，或直接提供条目数组")


# ---------------------------------------------------------------------------
# 已知性能问题（团队决定留到 P2 治理，勿在 P1 阶段"顺手修"）：本函数是 O(n²)。
#
# 根因：逐行调用 find_duplicate()，其 SQL 走 norm_term(term) = ?，而 norm_term 是 Python
# 注册的 UDF，无法命中 idx_entries_term 索引 → 每行一次全表扫描。
# 实测：空库导入 3000 条约 4.1s；库内已有 3000 条时再导入 3000 条约 12.1s（QA 测得 6.8s）。
# 后果：导入期间写锁被长时间持有，其他写操作要排队等待。已在 db.connect() 把 timeout
# 抬到 15s 兜底（避免直接报 "database is locked"），并在前端导入期间禁用保存/导入按钮。
# 即"慢，但不产生错误后果"，故降级为体验优化。
#
# P2 治理方案：导入前一次性把已有的 (type, 归一化 term) -> id 取到内存 dict，改逐行 O(1)
# 查表，复杂度降为 O(n)。两个必须注意的点：
#   1. key 必须用 db.normalize_term() 生成 —— 它与 SQL 侧注册的 norm_term 是同一个函数，
#      若另写一套归一化规则，两边漂移会**静默改变查重行为**（该拦的没拦、不该拦的被拦）。
#   2. 每成功创建/更新一条，必须同步回写这个 dict。否则**同一批次内部**的重复条目
#      （一个 JSON 文件里同一个词出现两次）会检测不到——当前逐行查库之所以能拦住，是因为
#      find_duplicate 能看到本事务内刚插入的行；换成内存 dict 后这份可见性要自己维护。
#      （P1 阶段实测过：3 个同名词条同批导入 → created=1、skipped=2、库中 1 行。）
# ---------------------------------------------------------------------------
def import_entries(conn, payload: object, *, on_duplicate: str = "skip") -> dict:
    """批量导入。on_duplicate=skip 跳过重复项，overwrite 覆盖已有条目。"""
    policy = str(on_duplicate or "skip").strip().lower()
    if policy not in DUPLICATE_POLICIES:
        raise EntryError(
            f"重复项处理策略只能是 skip 或 overwrite，收到：{on_duplicate or '空值'}"
        )

    raw_items = parse_import_payload(payload)
    stats = {
        "total": len(raw_items),
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "on_duplicate": policy,
        "errors": [],
    }

    try:
        for index, raw in enumerate(raw_items, start=1):
            try:
                data = normalize_entry_payload(raw)
            except AppError as exc:
                stats["failed"] += 1
                if len(stats["errors"]) < MAX_IMPORT_ERRORS:
                    stats["errors"].append({"index": index, "reason": str(exc)})
                continue

            duplicate = find_duplicate(conn, data["type"], data["term"])
            if duplicate is not None:
                if policy == "overwrite":
                    # 与 PUT /api/entries/{id} 语义一致：全量替换而非合并，
                    # 未提供的字段一律按空处理。
                    update_entry(conn, duplicate[0], data, commit=False)
                    stats["updated"] += 1
                else:
                    stats["skipped"] += 1
                continue

            create_entry(conn, data, commit=False)
            stats["created"] += 1
    except Exception:
        conn.rollback()
        raise

    conn.commit()
    return stats
