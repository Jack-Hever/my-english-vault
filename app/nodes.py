"""概念节点 + 层级 / 从属关系（需求 6）。

数据模型是**两张新表**：`nodes`（节点身份超集）+ `node_edges`（层级邻接表）。

★ 为什么概念节点不放在 entries 表里 ★
这是本模块最核心的设计取舍，也是「概念节点不参与评分 / 不进练习」的实现基础：
概念节点单独一张表，于是「它不在 entries 里」成为**结构保证**——`list_entries` /
`export_entries` / `find_duplicate` / `review.due_entries` / `review.new_entries_since` /
`is_highlight` / `practices._practice_weight` 全部只读 entries，**一行代码不改也读不到
概念节点**。零过滤 = 零漏网。（对比：若在 entries 里插 `is_concept` 占位行，就得在每个
读 entries 的地方都记得加过滤，漏一处就穿帮；而 `entries.type` 的 CHECK 还要重建全库
最大的表。）

★ 为什么层级不进 links 的关联组模型 ★
组模型是**无向同伴关系**（同组即互为同伴），层级是**有向父子关系**：塞进组模型后父子
方向只能靠成员插入顺序猜、环检测彻底失效（无向图里"成环"和"多人互相认识"无法区分）、
兄弟排序无处安放（entry_link_items 没有序数列）、外键也表达不了。所以 `links.py` 与
7 条 `/api/links` 路由**零改动**，层级是独立子系统。

★ 为什么真实节点的 label 不落库 ★
`nodes.label` 只在 `kind='concept'` 时有意义。真实词条的显示名实时取 `entries.term`，
**不冗余存一份**——否则用户改了词条名，树上的显示就成了旧词这种失步。

★ 概念节点不参与评分 ★
三重保证：① DDL 不给 nodes 表评分列；② 响应里概念节点**不存在** `importance` /
`usefulness` / `highlight` / `due_at` 键（缺键而非 null，前端读到 undefined 得 NaN，
好过得到错误的 0）；③ 概念节点没有 entry_id，永远不会被 `_seed_review_state` 播种出
`review_state` 行，进不了复习与练习候选池。

★ 占位节点（tombstone）：为什么概念树留、近似词不留 ★
用户对两处的要求看起来相反，实则相反是对的，因为**两处的成员语义根本不同**：

  - 概念树（nodes / node_edges）：成员是**结构**。父节点下面挂着子节点，
    删掉父节点，那条「xx 下面是 yy、zz」的从属关系就没了主语——把子节点直接提到
    祖父下，用户看到的是「词条们平白无故挤到了上一层」，分不清是"故意摘除"还是
    "词没了"。所以**有子项就留占位**，让"这里原本有个词"这件事可见。

  - 关联库（entry_links / entry_link_items）：成员是**引用本身**。组里记的是
    "A 和 B 是近义词"，成员行就是这句话的本体，没有独立的结构需要保留。
    词条删了，这句话就不成立了；留一个占位成员反而会让「2 人成组」的判定失真
    （一个死成员能把一个只剩 1 人的残组撑成合法组，用户点进去发现是空的）。
    所以**清干净**：外键 CASCADE 清成员行 + prune_degenerate_links 清残组。

  ⚠ 请不要"好心"把两边统一掉 —— 统一成留占位，关联库会出现查不到同伴的死组；
  统一成清干净，概念树会丢掉从属关系的语义。两侧的差异是设计，不是遗漏。
"""

from __future__ import annotations

import sqlite3

from . import entries, links
from .entries import AppError

# ---------------- 常量 ----------------

#: 节点种类。kind='entry' 即对外的 real=true（由 db.py 的 CHECK 保证自洽）。
KIND_CONCEPT = "concept"
KIND_ENTRY = "entry"

#: 单条链最多 MAX_DEPTH 层（含根）。前端画放射图默认只画 2 层，
#: 上限存在的意义是「合法但深到没法画」的病态树有一条硬边界，
#: 且 SQL 递归 CTE 也因此有了保险丝（见 _depth_of_parent 的 cap 参数）。
MAX_DEPTH = 8

#: 单个父节点最多 MAX_CHILDREN 个直接子节点。
MAX_CHILDREN = 50

#: 单棵树（一个连通分量）最多 MAX_TREE_NODES 个节点。超限直接报业务错而不是把
#: 上千个节点塞进响应——半棵树会被用户误解成"这就是全部"。
MAX_TREE_NODES = 2000

#: 概念节点 label 长度上限（与 entries.FIELD_MAX_LEN 同量级风格）。
MAX_TREE_LABEL = 60

#: 概念节点 note 长度上限。label 是名字（短），note 是备注（可以长一点）。
MAX_TREE_NOTE = 500

#: 根列表单页上限，与 links.MAX_PAGE_SIZE 同量级
MAX_PAGE_SIZE = 100

#: 树形状取值
SHAPE_CHILDREN = "children"
SHAPE_FLAT = "flat"
SHAPES = (SHAPE_CHILDREN, SHAPE_FLAT)

#: 删除节点的两种模式（见 delete_node）
DELETE_CASCADE = "cascade"
DELETE_PROMOTE = "promote"
DELETE_MODES = (DELETE_CASCADE, DELETE_PROMOTE)


# ---------------- 异常定义 ----------------


class NodeError(AppError):
    """层级树业务错误，映射为 HTTP 400（与 links.LinkError 同一套做法）。"""


class NodeNotFoundError(NodeError):
    def __init__(self, node_id: int) -> None:
        super().__init__(f"节点不存在或已被删除（ID={node_id}）")
        self.node_id = int(node_id)


class CycleError(NodeError):
    """层级成环。「狗 → 犬 → 狗」在语义上无意义，且会让前端递归布局爆栈。"""


# ---------------- SQL 片段（仅引用本模块常量，不含任何用户输入） ----------------

#: 取节点 + 其词条字段 + 父 / 兄弟序 / 子节点数。
#: parent_id 与 position 走 (SELECT ... WHERE child_id = n.id)：
#: idx_edges_child 是唯一索引，这就是单点查「唯一父」的全部实现。
#: root 没有父边，两列都是 NULL —— 对外统一呈现为 parent_id=null / position=0
#: （见 3.2 契约：根的 position 就是 0）。
_SELECT_NODE = (
    "SELECT n.id AS node_id, n.kind AS kind, n.label AS concept_label, n.note AS note, "
    "n.entry_id AS entry_id, n.created_at AS created_at, n.updated_at AS updated_at, "
    "n.tombstone_label AS tombstone_label, n.tombstoned_at AS tombstoned_at, "
    "e.type AS entry_type, e.term AS entry_term, e.pos AS entry_pos, "
    "e.meaning_zh AS entry_meaning_zh, e.usage AS entry_usage, e.source AS entry_source, "
    "e.importance AS entry_importance, e.usefulness AS entry_usefulness, "
    "e.created_at AS entry_created_at, e.updated_at AS entry_updated_at, "
    "(SELECT ne.parent_id FROM node_edges ne WHERE ne.child_id = n.id) AS parent_id, "
    "(SELECT ne.position FROM node_edges ne WHERE ne.child_id = n.id) AS position, "
    "(SELECT COUNT(*) FROM node_edges ce WHERE ce.parent_id = n.id) AS child_count "
    "FROM nodes n LEFT JOIN entries e ON e.id = n.entry_id"
)

#: 取子树（递归 CTE）。UNION 去重：万一库里已经有环（用户手工改过库），
#: 递归也会终止，不会卡死。s.depth < cap 是硬性保险丝。
_CTE_SUBTREE = (
    "WITH RECURSIVE sub(id, parent_id, position, depth) AS ("
    "  SELECT n.id, NULL, 0, 0 FROM nodes n WHERE n.id = ?"
    "  UNION"
    "  SELECT e.child_id, e.parent_id, e.position, s.depth + 1"
    "  FROM node_edges e JOIN sub s ON e.parent_id = s.id"
    "  WHERE s.depth < ?"
    ")"
)

#: 子树节点数预检（MAX_TREE_NODES 判定），与 _CTE_SUBTREE 同一条递归路径。
_SQL_COUNT_SUBTREE = (
    "WITH RECURSIVE sub(id, depth) AS ("
    "  SELECT n.id, 0 FROM nodes n WHERE n.id = ?"
    "  UNION"
    "  SELECT e.child_id, s.depth + 1"
    "  FROM node_edges e JOIN sub s ON e.parent_id = s.id"
    "  WHERE s.depth < ?"
    ") SELECT COUNT(*) FROM sub"
)

#: ★ 「这个根节点算不算一个真正的层级块」判据（需求①）★
#:
#: 背景：词汇库里的**每一个**词条在 nodes 表都有一行（create_entry 播种，
#: kind='entry'），而根的定义只是「没有父边」。于是任何还没被挂进树的
#: 普通词条都成了"树根"，在关联库里被渲染成绿色「层级」块，显示
#: 「1 个节点 / 1 词条 / 0 概念」—— 用户截图里 recoil / shy away /
#: uncanny 等几十个词条全被误当成关联块。
#:
#: 判据：根节点必须**自己就构成一个结构**，满足任一条即算真关联块：
#:   (a) 它下面挂了子节点（有子边 → 子树 ≥ 2 个节点）；或
#:   (b) 它本身是概念节点（用户显式建的锚点，即便暂时没挂东西也算）。
#: 两者都不满足 = 子树只有它自己、且它是个普通词条 = create_entry 播种
#: 留下的孤立节点行，不可能是用户建的关联，跳过。
#:
#: 为什么不用「有没有子边」单独判断（那样会藏掉 (b)）：用户建了但清空到
#: 只剩一个概念节点的关联，是用户建的东西，应该看得见、也该由用户自己
#: 决定删不删 —— 清理走 purge-empty 那条**用户手动触发**的路，不在显示层偷删。
#:
#: 验证过不会误伤真树：「狗的种类」是 concept 根且有 4 个子节点（(a)(b) 都过）；
#: 两个 SYM/OPP 组的成员各自成树、子树 ≥ 2 个节点（(a) 过）。三者都保留。
_SQL_ROOT_IS_STRUCTURE = (
    "(EXISTS (SELECT 1 FROM node_edges ge WHERE ge.parent_id = n.id)"
    " OR n.kind = 'concept')"
)

#: 根列表 + 每棵树的统计（一条 SQL 出 entry_count / concept_count / max_depth）。
_SQL_ROOTS = (
    "WITH RECURSIVE root(id) AS ("
    "  SELECT n.id FROM nodes n LEFT JOIN entries e ON e.id = n.entry_id"
    "  WHERE NOT EXISTS (SELECT 1 FROM node_edges x WHERE x.child_id = n.id)"
    # ★ 排除「1 节点 / 1 词条 / 0 概念」的伪层级块（判据见 _SQL_ROOT_IS_STRUCTURE）
    "    AND " + _SQL_ROOT_IS_STRUCTURE +
    "    AND (? = '' OR COALESCE(n.label, e.term, '') LIKE ?)"
    "),"
    " sub(root_id, id, kind, depth, tombstone_label) AS ("
    "  SELECT root.id, root.id, n.kind, 0, n.tombstone_label FROM root JOIN nodes n ON n.id = root.id"
    "  UNION"
    "  SELECT s.root_id, e.child_id, n.kind, s.depth + 1, n.tombstone_label"
    "  FROM sub s"
    "  JOIN node_edges e ON e.parent_id = s.id"
    "  JOIN nodes n ON n.id = e.child_id"
    "  WHERE s.depth < ?"
    ")"
    " SELECT r.id AS node_id, rn.kind AS kind, rn.label AS concept_label, rn.note AS note, "
    "       rn.entry_id AS entry_id, rn.updated_at AS updated_at, e.term AS entry_term, "
    "       rn.tombstone_label AS tombstone_label, rn.tombstoned_at AS tombstoned_at, "
    "       (SELECT COUNT(*) FROM node_edges c WHERE c.parent_id = r.id) AS child_count, "
    "       COUNT(*) AS node_count, "
    "       SUM(CASE WHEN s.kind = 'entry' THEN 1 ELSE 0 END) AS entry_count, "
    "       SUM(CASE WHEN s.kind = 'concept' THEN 1 ELSE 0 END) AS concept_count, "
    "       SUM(CASE WHEN s.kind = 'concept' AND s.tombstone_label IS NOT NULL "
    "           THEN 1 ELSE 0 END) AS tombstone_count, "
    "       MAX(s.depth) AS max_depth "
    "FROM root r "
    "JOIN nodes rn ON rn.id = r.id "
    "LEFT JOIN entries e ON e.id = rn.entry_id "
    "JOIN sub s ON s.root_id = r.id "
    "GROUP BY r.id "
    "ORDER BY rn.updated_at DESC, r.id DESC"
)

#: 根列表的 total（与 _SQL_ROOTS 的 root CTE 同一过滤条件，含①的伪块排除）
_SQL_COUNT_ROOTS = (
    "SELECT COUNT(*) FROM nodes n LEFT JOIN entries e ON e.id = n.entry_id "
    "WHERE NOT EXISTS (SELECT 1 FROM node_edges x WHERE x.child_id = n.id) "
    "AND " + _SQL_ROOT_IS_STRUCTURE +
    "AND (? = '' OR COALESCE(n.label, e.term, '') LIKE ?)"
)


# ---------------- 内部工具 ----------------


def _is_tombstone(row) -> bool:
    """这个节点是不是「已删除词条」留下的占位。

    判定口径只有一个：nodes.tombstone_label IS NOT NULL。
    之所以不看 kind，是因为占位节点刻意复用 kind='concept'（见模块注释），
    单看 kind 分不出「用户建的概念」和「删掉的词条」。
    """
    try:
        return row["tombstone_label"] is not None
    except (IndexError, KeyError):
        return False


def _opt_float(value: object) -> float | None:
    """列值转 float；NULL 保持 None（「未评分」不能被压成 0）。"""
    if value is None:
        return None
    return float(value)


def _clean_label(value: object) -> str:
    """概念节点名：去空白 + 必填 + 长度上限。超长报错而不是静默截断。"""
    text = "" if value is None else str(value).strip()
    if not text:
        raise NodeError("概念节点名不能为空")
    if len(text) > MAX_TREE_LABEL:
        raise NodeError(f"概念节点名长度不能超过 {MAX_TREE_LABEL} 个字符（当前 {len(text)} 个）")
    return text


def _clean_note(value: object) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) > MAX_TREE_NOTE:
        raise NodeError(f"备注长度不能超过 {MAX_TREE_NOTE} 个字符（当前 {len(text)} 个）")
    return text


def _clean_position(value: object, upper: int) -> int:
    """把 position 归一到 0..upper。None = 追加到末尾（由调用方给 upper）。"""
    if value is None:
        return upper
    try:
        pos = int(value)
    except (TypeError, ValueError) as exc:
        raise NodeError(f"position 必须是整数，收到：{value}") from exc
    if pos < 0:
        raise NodeError(f"position 不能为负，收到：{pos}")
    return min(pos, upper)


def _fetch_node_row(conn: sqlite3.Connection, node_id: int):
    """取节点行；不存在抛 NodeNotFoundError（404）。"""
    row = conn.execute(f"{_SELECT_NODE} WHERE n.id = ?", (int(node_id),)).fetchone()
    if row is None:
        raise NodeNotFoundError(node_id)
    return row


def _entry_payload(row) -> dict | None:
    """词条摘要。概念节点返回 None（它根本没有词条）。"""
    if row["entry_id"] is None:
        return None
    ratings = {
        "importance": _opt_float(row["entry_importance"]),
        "usefulness": _opt_float(row["entry_usefulness"]),
    }
    return {
        "id": int(row["entry_id"]),
        "type": row["entry_type"],
        "term": row["entry_term"],
        "pos": row["entry_pos"] or "",
        "meaning_zh": row["entry_meaning_zh"],
        "usage": row["entry_usage"] or "",
        "source": row["entry_source"] or "",
        "importance": ratings["importance"],
        "usefulness": ratings["usefulness"],
        "highlight": entries.is_highlight(ratings),
        "created_at": row["entry_created_at"],
        "updated_at": row["entry_updated_at"],
    }


def _display_label(row) -> str:
    """永远可用的显示名：概念节点 = nodes.label；真实节点 = entries.term。

    前端画图只认这一个字段，不需要按 kind 分支。
    """
    if row["kind"] == KIND_CONCEPT:
        return row["concept_label"] or ""
    return row["entry_term"] or ""


def _subtitle(row) -> str:
    """第二行说明：概念节点恒为 ''；真实节点 = entries.meaning_zh。"""
    if row["kind"] == KIND_CONCEPT:
        return ""
    return row["entry_meaning_zh"] or ""


def _summary(row) -> dict:
    """祖先链 / 面包屑用的精简形状（不含评分，避免把「概念节点无评分」的口径漏出去）。"""
    return {
        "node_id": int(row["node_id"]),
        "kind": row["kind"],
        "real": row["kind"] == KIND_ENTRY,
        "tombstone": _is_tombstone(row),
        "label": _display_label(row),
        "parent_id": None if row["parent_id"] is None else int(row["parent_id"]),
    }


def _node_payload(row, *, depth: int, path: list[dict] | None, with_meta: bool) -> dict:
    """节点对象（3.2 契约的统一形状）。

    with_meta=True  → 单节点 / 建树响应：带 path / created_at / updated_at
    with_meta=False → 树 / 扁平响应：按 3.4 与 6.1 省掉这三项，响应更小

    ★ 概念节点的响应里**不存在** importance / usefulness / highlight / due_at 键 ★
    评分只出现在 entry 子对象里。这是「概念节点不参与评分」在数据契约上的落地点：
    缺键（而非 null）让前端 `node.importance` 得到 undefined，任何加权求和直接得 NaN，
    而不是拿到一个语义错误的 0。

    ★ tombstone 字段 ★
    占位节点的判定写进响应（`tombstone: true` + `tombstone_label` 给出原词条名），
    前端据此渲染「已删除的词条」标记与清理按钮。占位节点 real 恒为 false、
    entry 恒为 null，所以「不可跳详情 / 不可打分」是**结构保证**，前端不必各自防御。
    """
    tombstone = _is_tombstone(row)
    payload = {
        "node_id": int(row["node_id"]),
        "kind": row["kind"],
        "real": row["kind"] == KIND_ENTRY,
        "tombstone": tombstone,
        "tombstone_label": (row["tombstone_label"] or "") if tombstone else "",
        "label": _display_label(row),
        "subtitle": _subtitle(row),
        "note": row["note"] or "",
        "entry_id": None if row["entry_id"] is None else int(row["entry_id"]),
        "parent_id": None if row["parent_id"] is None else int(row["parent_id"]),
        "position": 0 if row["position"] is None else int(row["position"]),
        "depth": int(depth),
        "child_count": int(row["child_count"] or 0),
        "entry": _entry_payload(row),
    }
    if with_meta:
        payload["path"] = list(path or [])
        payload["created_at"] = row["created_at"]
        payload["updated_at"] = row["updated_at"]
        payload["tombstoned_at"] = (row["tombstoned_at"] or "") if tombstone else ""
    return payload


# ---------------- 祖先链 / 深度 / 环检测 ----------------


def _ancestors(conn: sqlite3.Connection, node_id: int) -> list[sqlite3.Row]:
    """取从根到「自己的父」这一段的祖先链（根在前，不含自己）。

    走 idx_edges_child 单点查逐级上溯（链长 ≤ MAX_DEPTH，量级可忽略），
    比递归 CTE 更好读，且天然带出正确的根→叶顺序。带 seen 防脏环死循环。

    每级都走 _SELECT_NODE 而不是精简列：祖先里可能混着真实词条节点，
    它的显示名在 entries.term 里，精简列没带上 entry_term 会在取 label 时炸。
    """
    chain: list[sqlite3.Row] = []
    seen = {int(node_id)}
    current = int(node_id)
    while True:
        row = conn.execute(
            _SELECT_NODE + " WHERE n.id = ?", (current,)
        ).fetchone()
        if row is None or row["parent_id"] is None:
            break
        parent_id = int(row["parent_id"])
        if parent_id in seen:
            break
        parent = conn.execute(
            _SELECT_NODE + " WHERE n.id = ?", (parent_id,)
        ).fetchone()
        if parent is None:
            break
        chain.append(parent)
        seen.add(parent_id)
        current = parent_id
    chain.reverse()
    return chain


def _assert_no_cycle(conn: sqlite3.Connection, parent_id: int, child_id: int) -> None:
    """写入前校验：给 child 挂 parent 之前，判断 child 是否已在 parent 的子树内。

    语义：新增边 parent → child 后若成环，等价于「从 parent 向上能走到 child」。
    所以是**从 parent 起走上溯、找 child**。

    ★ 这里与设计文档 §2.2 的 SQL 参数绑定方向相反，是刻意修正 ★
    文档原文是「判断 parent 是否已经是 child 的祖先」并给出
        SELECT parent_id FROM node_edges WHERE child_id = :child  ... WHERE id = :parent
    但那是**无害的重复挂载**情形（把子节点挂到它已有的祖 grandparent 下），
    不是成环。真正的环是反向的：把 狗 挂到 犬 下时，成环等价于「从 犬 向上能走到 狗」。
    按文档原文绑定会放过这个环（实测：狗→犬 建好后挂 狗 到 犬 下返回 200 并真的成了环），
    且与文档自己的验收硬门 §7.4「建 狗→犬 后尝试挂 狗 到 犬 下 → 400」直接矛盾。
    以验收硬门为准：锚点走 parent、目标比对 child。

    用 UNION（去重）而不是 UNION ALL：万一库里已经有环，递归也会终止，不会卡死。
    """
    if int(parent_id) == int(child_id):
        raise CycleError("不能建立层级：节点不能作为自己的子节点")
    row = conn.execute(
        "WITH RECURSIVE anc(id) AS ("
        "  SELECT parent_id FROM node_edges WHERE child_id = ?"
        "  UNION"
        "  SELECT e.parent_id FROM node_edges e JOIN anc a ON e.child_id = a.id"
        ") SELECT 1 FROM anc WHERE id = ? LIMIT 1",
        (int(parent_id), int(child_id)),
    ).fetchone()
    if row is not None:
        raise CycleError("不能建立层级：目标父节点在自身子树内")


def _depth_of_parent(conn: sqlite3.Connection, parent_id: int, cap: int) -> int:
    """算「挂到这个父下之后，子节点会处在第几层」（根 = 第 1 层）。

    同一条递归 CTE 顺带算出父链深度；`WHERE a.depth < :cap` 是**硬性保险丝**：
    即使前面的环检测被绕过（比如将来有人绕过本模块直接写库），递归也最多跑 cap 层。
    """
    row = conn.execute(
        "WITH RECURSIVE anc(id, depth) AS ("
        "  SELECT ?, 1"
        "  UNION ALL"
        "  SELECT e.parent_id, a.depth + 1"
        "  FROM node_edges e JOIN anc a ON e.child_id = a.id"
        "  WHERE a.depth < ?"
        ") SELECT MAX(depth) FROM anc",
        (int(parent_id), int(cap)),
    ).fetchone()
    return int(row[0] or 1) + 1


def _child_ids_of(conn: sqlite3.Connection, parent_id: int, *, exclude: int | None = None) -> list[int]:
    """取父节点下按 position 升序的子节点 id 列表。"""
    rows = conn.execute(
        "SELECT child_id FROM node_edges WHERE parent_id = ? ORDER BY position, child_id",
        (int(parent_id),),
    ).fetchall()
    ids = [int(r["child_id"]) for r in rows]
    if exclude is not None:
        ids = [i for i in ids if i != int(exclude)]
    return ids


def _apply_order(
    conn: sqlite3.Connection, parent_id: int, ordered_child_ids: list[int]
) -> None:
    """把父节点下的子节点顺序落库，保证 position 是 0..n-1 连续。

    连续序号让 ORDER BY position 的结果稳定，且不需要事后压缩空洞。
    """
    now = entries.now_iso()
    for index, child_id in enumerate(ordered_child_ids):
        conn.execute(
            "INSERT INTO node_edges (parent_id, child_id, position, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(child_id) DO UPDATE SET parent_id = excluded.parent_id, "
            "position = excluded.position",
            (int(parent_id), int(child_id), index, now),
        )


# ---------------- 节点写操作 ----------------


def _link(
    conn: sqlite3.Connection,
    parent_id: int,
    child_id: int,
    position: object = None,
) -> int:
    """建边（幂等）。返回子节点 id。

    幂等由 idx_edges_child 唯一索引 + ON CONFLICT(child_id) DO UPDATE 保证：
    同一 (parent, child) 重复提交不报错、不重复插边；换一个父则边改指（拖拽换父）。
    """
    _fetch_node_row(conn, parent_id)
    _fetch_node_row(conn, child_id)
    _assert_no_cycle(conn, parent_id, child_id)

    target_depth = _depth_of_parent(conn, parent_id, MAX_DEPTH)
    if target_depth > MAX_DEPTH:
        raise NodeError(f"层级最多 {MAX_DEPTH} 层，已达到上限（父节点自身处在第 {target_depth - 1} 层）")

    # 兄弟序：把 child 从旧父的名单里摘掉，再按请求的 position 插入新父的名单。
    # 「摘掉」必须做——否则 child 在旧父下留着一个占位 position，新父下又会插一条，
    # 出现同一 child_id 两行（唯一索引会挡住，但语义已经乱了）。
    old_parent_row = conn.execute(
        "SELECT parent_id FROM node_edges WHERE child_id = ?", (int(child_id),)
    ).fetchone()
    old_parent = int(old_parent_row["parent_id"]) if old_parent_row else None

    current = _child_ids_of(conn, parent_id, exclude=child_id)
    if len(current) >= MAX_CHILDREN:
        raise NodeError(f"单个节点最多 {MAX_CHILDREN} 个直接子节点")

    index = _clean_position(position, len(current))
    new_order = current[:index] + [int(child_id)] + current[index:]

    now = entries.now_iso()
    entries._begin_write(conn)
    try:
        _apply_order(conn, parent_id, new_order)
        if old_parent is not None and old_parent != int(parent_id):
            # 换父：把旧父下的兄弟序压平，否则那里会留一个空洞
            _apply_order(conn, old_parent, _child_ids_of(conn, old_parent))
        _touch(conn, parent_id, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return int(child_id)


def _touch(conn: sqlite3.Connection, node_id: int, now: str) -> None:
    """父节点「最近动过」——放射图根列表按 updated_at 倒序排。"""
    conn.execute("UPDATE nodes SET updated_at = ? WHERE id = ?", (now, int(node_id)))


def create_concept(
    conn: sqlite3.Connection,
    label: object,
    note: object = "",
    parent_id: object = None,
    position: object = None,
) -> dict:
    """建概念节点；parent_id 给了就同时挂上（前端最常用的一条路径）。"""
    text = _clean_label(label)
    note_text = _clean_note(note)
    parent = None if parent_id is None else int(parent_id)

    entries._begin_write(conn)
    try:
        now = entries.now_iso()
        cur = conn.execute(
            "INSERT INTO nodes (kind, label, note, entry_id, created_at, updated_at) "
            "VALUES (?, ?, ?, NULL, ?, ?)",
            (KIND_CONCEPT, text, note_text, now, now),
        )
        node_id = int(cur.lastrowid)
        if parent is not None:
            # 校验放在插入之后：环 / 深度 / 宽度不通过时外层会 rollback，
            # 新节点不会留下孤儿行。
            _link(conn, parent, node_id, position)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_node(conn, node_id)


def node_of_entry(conn: sqlite3.Connection, entry_id: int) -> int | None:
    """词条 → node_id；还没有节点行则返回 None（由调用方决定是否 get-or-create）。"""
    row = conn.execute(
        "SELECT id FROM nodes WHERE entry_id = ?", (int(entry_id),)
    ).fetchone()
    return int(row["id"]) if row else None


def ensure_node_of_entry(conn: sqlite3.Connection, entry_id: int) -> tuple[int, bool]:
    """get-or-create 词条的节点行，返回 (node_id, created)。

    为什么需要：层级边的两端都是 node_id，真实词条要能当子节点就必须有 node 行。
    新建词条时由 entries.create_entry 播种；**本功能上线前就存在的词条**靠
    db._backfill_entry_nodes 补齐。这里是最后一道兜底（比如手工改过库的库）。
    """
    existing = node_of_entry(conn, entry_id)
    if existing is not None:
        return existing, False
    entries.get_entry(conn, entry_id)  # 词条必须存在 → 否则 EntryNotFoundError(404)
    now = entries.now_iso()
    cur = conn.execute(
        "INSERT INTO nodes (kind, label, note, entry_id, created_at, updated_at) "
        "VALUES (?, NULL, '', ?, ?, ?)",
        (KIND_ENTRY, int(entry_id), now, now),
    )
    return int(cur.lastrowid), True


def attach_entry(
    conn: sqlite3.Connection,
    entry_id: int,
    parent_id: object = None,
    position: object = None,
) -> dict:
    """把真实词条挂到某节点下；节点不存在则 get-or-create。

    返回 {"node": 节点对象, "created": 节点行是否本次新建}。
    created=False（节点已存在）时这次调用等价于「换父」。
    """
    entries._begin_write(conn)
    try:
        node_id, created = ensure_node_of_entry(conn, entry_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if parent_id is not None:
        _link(conn, int(parent_id), node_id, position)
    return {"node": get_node(conn, node_id), "created": created}


def find_concept_by_label(conn: sqlite3.Connection, label: object) -> dict | None:
    """按归一化名查找概念节点，供「要不要复用已有概念」提示用。

    走 SQL 的 norm_term()，它是 Python 注册的 UDF，**命中不了索引**，每次调用一次
    全表扫描。单条新增 / 编辑调一次无妨（与 entries.find_duplicate 同理）；
    不要在列表页逐行调用。刻意不加唯一约束——「狗(动物)」与「狗(食品)」都合法。
    """
    text = "" if label is None else str(label).strip()
    if not text:
        return None
    # tombstone_label IS NOT NULL 的行必须排除：占位节点虽然复用 kind='concept'，
    # 但它不是「用户建的概念」。让用户新建的概念复用/命中一个占位壳，会得到一个
    # 带着「已删除」标记、又没有真实词条的节点——那比直接新建还糟。
    row = conn.execute(
        "SELECT id, label FROM nodes WHERE kind = ? AND tombstone_label IS NULL "
        "AND norm_term(label) = ? ORDER BY id LIMIT 1",
        (KIND_CONCEPT, text),
    ).fetchone()
    return {"node_id": int(row["id"]), "label": row["label"]} if row else None


def attach_child(
    conn: sqlite3.Connection,
    parent_id: int,
    child: object,
    position: object = None,
) -> dict:
    """一次完成「找 / 建子节点 + 建边」，返回子节点对象。前端最常用的一条路径。

    child 内联定义（3.1 契约 #3）：
        {"kind": "entry",   "entry_id": 42}
        {"kind": "concept", "label": "猎犬", "note": "组内按用途细分"}

    幂等口径：
      - entry：节点行 get-or-create，重复提交只是把边指向同一个位置；
      - concept：**同一父下已有同名概念**时复用该节点（防双击重复建同名子节点），
        其余情况新建——刻意不跨父复用，因为「狗(动物)」「狗(食品)」都应当允许。
    """
    if not isinstance(child, dict):
        raise NodeError("child 必须是 JSON 对象")
    kind = str(child.get("kind") or KIND_CONCEPT).strip().lower()
    if kind not in (KIND_CONCEPT, KIND_ENTRY):
        raise NodeError(f"节点类型只能是 concept 或 entry，收到：{kind or '空值'}")

    _fetch_node_row(conn, parent_id)

    if kind == KIND_ENTRY:
        if child.get("entry_id") is None:
            raise NodeError("kind=entry 时必须提供 entry_id")
        return attach_entry(conn, int(child["entry_id"]), parent_id, position)["node"]

    label = _clean_label(child.get("label"))
    existing = _find_child_concept(conn, int(parent_id), label)
    if existing is not None:
        # 复用：把 position 调整到请求的位置，其余语义与新建完全一致
        _link(conn, int(parent_id), existing, position)
        return get_node(conn, existing)

    return create_concept(conn, label, child.get("note", ""), parent_id, position)


def _find_child_concept(conn: sqlite3.Connection, parent_id: int, label: str) -> int | None:
    """父节点下是否已有同名（归一化后）的概念子节点。**排除占位节点**。

    排除的理由同 find_concept_by_label：占位节点是「某个词被删了」的遗址，
    不是用户建的概念。复用它会让新建的概念莫名带上「已删除」标记。
    """
    row = conn.execute(
        "SELECT n.id AS node_id FROM nodes n "
        "JOIN node_edges ne ON ne.child_id = n.id "
        "WHERE ne.parent_id = ? AND n.kind = ? AND n.tombstone_label IS NULL "
        "AND norm_term(n.label) = ? "
        "ORDER BY n.id LIMIT 1",
        (int(parent_id), KIND_CONCEPT, label),
    ).fetchone()
    return int(row["node_id"]) if row else None


def detach_child(conn: sqlite3.Connection, parent_id: int, child_id: int) -> dict:
    """移出子节点：只断边，子节点升级为根。**不删节点、不删词条。**

    这是比「删词条」轻得多的摘除操作，应优先引导用户用它。
    """
    parent_id, child_id = int(parent_id), int(child_id)
    _fetch_node_row(conn, parent_id)
    _fetch_node_row(conn, child_id)
    edge = conn.execute(
        "SELECT 1 FROM node_edges WHERE parent_id = ? AND child_id = ?",
        (parent_id, child_id),
    ).fetchone()
    if edge is None:
        raise NodeError(f"节点 {child_id} 不在节点 {parent_id} 的子节点下")

    entries._begin_write(conn)
    try:
        now = entries.now_iso()
        conn.execute(
            "DELETE FROM node_edges WHERE parent_id = ? AND child_id = ?",
            (parent_id, child_id),
        )
        _apply_order(conn, parent_id, _child_ids_of(conn, parent_id))
        _touch(conn, parent_id, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"detached": True, "child_id": child_id, "parent_id": parent_id}


def reorder_children(conn: sqlite3.Connection, parent_id: int, child_ids: object) -> list[dict]:
    """兄弟序全量替换（拖拽排序）。

    child_ids 必须与「当前父节点的全部子节点集合」**完全一致**（多 / 少都 400）：
    少传会被静默删边，多传说明前端与后端对不上——两种都该让用户看见，而不是猜。
    """
    parent_id = int(parent_id)
    row = _fetch_node_row(conn, parent_id)
    if isinstance(child_ids, (str, bytes)):
        raise NodeError("child_ids 必须是数组")
    try:
        incoming = list(child_ids or [])
    except TypeError as exc:
        raise NodeError("child_ids 必须是数组") from exc

    ordered: list[int] = []
    seen: set[int] = set()
    for item in incoming:
        if isinstance(item, bool):
            raise NodeError("child_ids 只能包含节点 ID（整数）")
        try:
            value = int(item)
        except (TypeError, ValueError) as exc:
            raise NodeError(f"子节点 ID 必须是整数，收到：{item}") from exc
        if value in seen:
            raise NodeError(f"子节点 ID 重复：{value}")
        seen.add(value)
        ordered.append(value)

    current = set(_child_ids_of(conn, parent_id))
    missing = sorted(current - seen)
    unknown = sorted(seen - current)
    if missing or unknown:
        parts = []
        if missing:
            parts.append(f"缺少子节点 {missing}")
        if unknown:
            parts.append(f"多出子节点 {unknown}")
        raise NodeError(
            "child_ids 必须与该节点的子节点集合完全一致（" + "；".join(parts) + "）"
        )

    entries._begin_write(conn)
    try:
        _apply_order(conn, parent_id, ordered)
        _touch(conn, parent_id, entries.now_iso())
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return [_summary(_fetch_node_row(conn, cid)) | {"position": i} for i, cid in enumerate(ordered)]


def update_node(
    conn: sqlite3.Connection,
    node_id: int,
    label: object = None,
    note: object = None,
) -> dict:
    """改概念节点名 / 备注。**None = 不改动**（与 links.update_link 同一套语义）。

    kind='entry' 传 label 直接 400：真实词条的名字归 entries.term 管，
    在这里改 label 会出现「树上显示与词条名不一致」的失步。
    """
    node_id = int(node_id)
    row = _fetch_node_row(conn, node_id)
    if label is not None and row["kind"] != KIND_CONCEPT:
        raise NodeError("真实词条节点不能单独改名，请修改词条本身")
    new_label = None if label is None else _clean_label(label)
    new_note = None if note is None else _clean_note(note)
    if new_label is None and new_note is None:
        return get_node(conn, node_id)

    entries._begin_write(conn)
    try:
        now = entries.now_iso()
        conn.execute(
            "UPDATE nodes SET label = ?, note = ?, updated_at = ? WHERE id = ?",
            (
                new_label if new_label is not None else row["concept_label"],
                new_note if new_note is not None else (row["note"] or ""),
                now,
                node_id,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_node(conn, node_id)


def delete_node(conn: sqlite3.Connection, node_id: int, mode: object = DELETE_CASCADE) -> dict:
    """删节点。两种模式，**entries 表永不被触碰**。

    - cascade（默认）：删该节点 + **全部后代节点**。适用于「这个分类不要了」——
      概念节点是树的结构锚点，「删掉锚点还留着结构」只会留下一堆没有主人的孤儿根，
      用户在放射图列表里看到它们只会困惑。
    - promote：只删该节点，子节点接到它的**父节点**下；父为空则各自成为根。
      适用于「中间层要去掉，但下面挂的东西要留下」。

    两种模式下，被删节点若对应真实词条，**词条本身毫发无损**（可「按词条找回」重新挂）。
    """
    node_id = int(node_id)
    selected = str(mode or DELETE_CASCADE).strip().lower()
    if selected not in DELETE_MODES:
        raise NodeError(f"删除模式只能是 cascade 或 promote，收到：{mode}")
    _fetch_node_row(conn, node_id)

    entries._begin_write(conn)
    try:
        if selected == DELETE_CASCADE:
            rows = conn.execute(
                _CTE_SUBTREE + " SELECT s.id AS id FROM sub s ORDER BY s.depth DESC",
                (node_id, MAX_DEPTH),
            ).fetchall()
            removed = [int(r["id"]) for r in rows]
            if removed:
                placeholders = ",".join("?" * len(removed))
                conn.execute(f"DELETE FROM nodes WHERE id IN ({placeholders})", removed)
            promoted: list[int] = []
        else:
            promoted = _child_ids_of(conn, node_id)
            parent_row = conn.execute(
                "SELECT parent_id FROM node_edges WHERE child_id = ?", (node_id,)
            ).fetchone()
            parent_id = int(parent_row["parent_id"]) if parent_row else None
            if parent_id is not None and promoted:
                # 祖父节点容不下这么多子节点时**报错而不是静默超限**：
                # 超限会让放射图扇区挤成一团，且破坏 MAX_CHILDREN 这个不变量。
                if len(_child_ids_of(conn, parent_id)) + len(promoted) > MAX_CHILDREN:
                    raise NodeError(
                        f"父节点 {parent_id} 的子节点会超过 {MAX_CHILDREN} 个上限，"
                        "请先移出部分子节点再删除"
                    )
                conn.execute(
                    "UPDATE node_edges SET parent_id = ? WHERE parent_id = ?",
                    (parent_id, node_id),
                )
                # ★ 必须 exclude 自己 ★
                # 本节点此刻仍是祖父的子节点（它作为 child 的那条边要等下面 DELETE
                # 才被 CASCADE 清掉）。若把它算进排序名单，它会占掉 position=0，
                # DELETE 之后留下 1,2,3 的空洞，破坏「每个父下 0..n-1 连续」这个不变量
                # （表现为放射图第一个扇区角度错位）。
                _apply_order(
                    conn, parent_id, _child_ids_of(conn, parent_id, exclude=node_id)
                )
                _touch(conn, parent_id, entries.now_iso())
            # 父为空时无需动边：删除 node 行会由 CASCADE 清掉残余边，子节点自然成为根
            conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
            removed = [node_id]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "deleted": node_id,
        "removed_nodes": removed,
        "promoted_nodes": promoted,
        "mode": selected,
    }


def handle_entry_deleted(conn: sqlite3.Connection, entry_id: int) -> dict:
    """删真实词条**之前**调用：决定这个节点该「留占位」还是「直接消失」。

    ★ 语义（需求 3：被删除者有子项就保留占位 / 提示）★
      - 该词条节点**有子项** → 原地转成占位节点（label = 原词条名，entry_id 置 NULL），
        子节点**继续挂在它下面**。用户能看到「这里原本有个词，下面挂着这些」。
      - 该词条节点**没有子项** → 什么都不做。nodes.entry_id 上的 CASCADE 会把节点行
        连带删掉；留一个空占位没有任何信息价值，只会让根列表越来越脏。

    ★ 为什么必须在 DELETE FROM entries 之前做 ★
    `nodes.entry_id` 上有 ON DELETE CASCADE：直接 DELETE 会把节点行连同「父→子」的
    边一起抹掉，子节点直接变孤儿根，且这一步不可逆——用户分不清"故意摘除"和
    "意外丢失"。占位节点靠的是**先把 entry_id 置 NULL 断开外键**，让 CASCADE 打不着它。

    ★ 转换只发一条 UPDATE ★
    nodes 的 CHECK 允许「kind='concept' AND label IS NOT NULL AND entry_id IS NULL」，
    这正是占位节点需要的形状。于是 kind / label / entry_id 三处一起改就是合法的占位
    节点——不重建表、不动 CHECK、不新增 kind 取值。顺带白拿概念节点的既有保证：
    它不在 entries 表里 ⇒ 结构上不可能参与评分、不可能跳词条详情。

    由 entries.delete_entry 在 DELETE 之前调用（复用同一个写事务，不单独 commit）。
    概念节点永远不会被这一步误伤：CHECK 保证 kind='concept' 行的 entry_id IS NULL，
    node_of_entry 查不到它们。

    返回 {"tombstoned": node_id|None, "tombstone_label": str, "children_kept": int}，
    供 entries.delete_entry 透出给前端做提示（「已删除，树中保留占位」）。
    """
    empty = {"tombstoned": None, "tombstone_label": "", "children_kept": 0}
    node_id = node_of_entry(conn, entry_id)
    if node_id is None:
        return empty

    # 词条名要在删之前取：转换后 entries 那一行就没了。
    row = conn.execute(
        "SELECT e.term AS term FROM nodes n JOIN entries e ON e.id = n.entry_id WHERE n.id = ?",
        (node_id,),
    ).fetchone()
    term = (row["term"] if row else "") or ""

    children = _child_ids_of(conn, node_id)
    if not children:
        # 无子项 → 不留占位。零行改动，交由 CASCADE 清掉节点行。
        return empty

    now = entries.now_iso()
    entries._begin_write(conn)
    try:
        conn.execute(
            "UPDATE nodes SET kind = ?, label = ?, entry_id = NULL, "
            "tombstone_label = ?, tombstoned_at = ?, updated_at = ? WHERE id = ?",
            (KIND_CONCEPT, term, term, now, now, node_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "tombstoned": int(node_id),
        "tombstone_label": term,
        "children_kept": len(children),
    }


# ---------------- 占位节点的读取与清理 ----------------


def _is_tombstone_id(conn: sqlite3.Connection, node_id: int) -> bool:
    row = conn.execute(
        "SELECT tombstone_label FROM nodes WHERE id = ?", (int(node_id),)
    ).fetchone()
    if row is None:
        raise NodeNotFoundError(node_id)
    return row["tombstone_label"] is not None


def purge_tombstone(conn: sqlite3.Connection, node_id: int) -> dict:
    """清理**一个**占位节点：子节点提升到它的父节点下，然后删掉这个空壳。

    为什么默认是「提升」而不是 delete_node 的 cascade：占位节点的子节点是**真实的、
    仍被用户维护着**的结构（真实词条 / 用户建的概念）。把它们一起 cascade 掉等于
    「清理占位」顺手删了用户的树——那不是清理，是二次数据丢失。

    刻意**不**支持对占位节点 cascade：万一调用方硬传 mode=cascade，这里直接报错。
    """
    node_id = int(node_id)
    if not _is_tombstone_id(conn, node_id):
        raise NodeError("该节点不是占位节点，无需清理")

    return delete_node(conn, node_id, DELETE_PROMOTE)


def list_tombstones(conn: sqlite3.Connection, q: object = "") -> dict:
    """列全部占位节点（用户手动清理的入口）。

    刻意支持按词条名搜索：占位节点多起来之后，用户会记得「我删过某个词」，
    想找它的占位清掉，而不是翻页找。
    """
    keyword = str(q or "").strip()
    pattern = f"%{entries._like_pattern(keyword)}%" if keyword else ""
    total = int(
        conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE tombstone_label IS NOT NULL "
            "AND (? = '' OR tombstone_label LIKE ?)",
            (keyword, pattern),
        ).fetchone()[0]
    )
    rows = conn.execute(
        "SELECT n.id AS node_id, n.tombstone_label AS tombstone_label, "
        "n.tombstoned_at AS tombstoned_at, n.updated_at AS updated_at, "
        "(SELECT parent_id FROM node_edges WHERE child_id = n.id) AS parent_id, "
        "(SELECT COUNT(*) FROM node_edges c WHERE c.parent_id = n.id) AS child_count "
        "FROM nodes n WHERE n.tombstone_label IS NOT NULL "
        "AND (? = '' OR n.tombstone_label LIKE ?) "
        "ORDER BY n.tombstoned_at DESC, n.id DESC",
        (keyword, pattern),
    ).fetchall()
    return {
        "items": [
            {
                "node_id": int(row["node_id"]),
                "kind": KIND_CONCEPT,
                "real": False,
                "tombstone": True,
                "tombstone_label": row["tombstone_label"] or "",
                "label": row["tombstone_label"] or "",
                "tombstoned_at": row["tombstoned_at"] or "",
                "parent_id": None if row["parent_id"] is None else int(row["parent_id"]),
                "child_count": int(row["child_count"] or 0),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ],
        "total": total,
    }


def purge_all_tombstones(conn: sqlite3.Connection) -> dict:
    """一次性清理**全部**占位节点，返回清理掉的节点数与被提升的子节点数。

    为什么需要它：占位节点会随着「删词条」不断堆积，而每个都得手动点一次删除很烦。
    这个动作是幂等且安全的——没有占位时是零行改动。

    ★ 顺序很要紧：从叶子往根清 ★
    若先清父占位，父的子节点（可能也是占位）会被提升上来，接着又要被当成
    「子节点」提升一次，兄弟序会被反复重排。这里用**深度倒序**（先叶后根），
    保证每个节点被处理时，它的子树里已经没有占位节点了。
    """
    # 深度倒序 = 从叶子往根清。用递归 CTE 一次拿到有序的占位节点列表。
    rows = conn.execute(
        "WITH RECURSIVE anc(id, depth) AS ("
        "  SELECT id, 0 FROM nodes WHERE tombstone_label IS NOT NULL"
        "  UNION"
        "  SELECT e.parent_id, a.depth + 1"
        "  FROM node_edges e JOIN anc a ON e.child_id = a.id"
        "  WHERE a.depth < ?"
        ") SELECT id FROM anc ORDER BY depth DESC",
        (MAX_DEPTH,),
    ).fetchall()

    purged: list[int] = []
    promoted_total = 0
    for row in rows:
        node_id = int(row["id"])
        # 中途可能已被级联清掉（清父时子节点跟着走了），跳过即可。
        if conn.execute(
            "SELECT 1 FROM nodes WHERE id = ? AND tombstone_label IS NOT NULL",
            (node_id,),
        ).fetchone() is None:
            continue
        # delete_node 自带事务（_begin_write + commit），逐个提交而不是包一个大事务：
        # 万一某一个占位节点的父节点子节点已满（MAX_CHILDREN）而报错，
        # 前面已经清掉的那些不该跟着回滚——清理是幂等的，回滚反而让用户困惑。
        result = delete_node(conn, node_id, DELETE_PROMOTE)
        purged.append(node_id)
        promoted_total += len(result.get("promoted_nodes") or [])
    return {"purged": purged, "purged_count": len(purged), "promoted_count": promoted_total}


# ---------------- 节点读操作 ----------------


def get_node(conn: sqlite3.Connection, node_id: int) -> dict:
    """单节点。depth 相对**整棵树的根**计算，path 给完整祖先链（根在前）。"""
    node_id = int(node_id)
    row = _fetch_node_row(conn, node_id)
    ancestors = _ancestors(conn, node_id)
    path = [_summary(a) for a in ancestors]
    return _node_payload(row, depth=len(path), path=path, with_meta=True)


def node_parents(conn: sqlite3.Connection, node_id: int) -> dict:
    """面包屑：根 → 自己的祖先链（不含自己）。"""
    node_id = int(node_id)
    _fetch_node_row(conn, node_id)
    return {"path": [_summary(a) for a in _ancestors(conn, node_id)]}


def entry_nodes(conn: sqlite3.Connection, entry_id: int) -> dict:
    """词条 → 树上的位置（对称于既有 /api/entries/{id}/links）。

    {"entry_id", "node", "parent", "root", "path"}。词条还没有节点行时 node=null
    （**不报 404**——词条确实存在，只是还没挂到任何树上）。
    """
    entry_id = int(entry_id)
    node_id = node_of_entry(conn, entry_id)
    if node_id is None:
        return {"entry_id": entry_id, "node": None, "parent": None, "root": None, "path": []}

    row = _fetch_node_row(conn, node_id)
    ancestors = _ancestors(conn, node_id)
    parent_row = _fetch_node_row(conn, ancestors[-1]["node_id"]) if ancestors else None
    root_row = ancestors[0] if ancestors else row
    root_full = _fetch_node_row(conn, int(root_row["node_id"]))
    return {
        "entry_id": entry_id,
        "node": _node_payload(row, depth=len(ancestors), path=[], with_meta=True),
        "parent": _summary(parent_row) if parent_row is not None else None,
        "root": _summary(root_full),
        "path": [_summary(a) for a in ancestors],
    }


def _normalize_shape(shape: object) -> str:
    text = str(shape or SHAPE_CHILDREN).strip().lower()
    if text not in SHAPES:
        raise NodeError(f"shape 只能是 {' 或 '.join(SHAPES)}，收到：{shape or '空值'}")
    return text


def get_tree(
    conn: sqlite3.Connection,
    node_id: int,
    shape: object = SHAPE_CHILDREN,
    max_depth: object = None,
    default_expanded_depth: object = None,
) -> dict:
    """取整棵树（放射图数据源）。

    shape=children（默认）：嵌套 children，DFS 顺序直接由嵌套结构给出，
        前端不必先建 parent_id → children[] 索引（那是后端该做的事）。
    shape=flat：扁平数组按 (depth ASC, position ASC) 输出，给跨树搜索 / 全局排序用。

    ★ 层级深度 ≤ MAX_DEPTH，递归渲染不会爆栈 ★
    ★ 节点总数超 MAX_TREE_NODES 直接抛错，不返回半棵树 ★
      半棵树会被用户误解成"这就是全部"，比报错更糟。
    """
    node_id = int(node_id)
    shape_value = _normalize_shape(shape)
    _fetch_node_row(conn, node_id)

    depth_cap = MAX_DEPTH if max_depth is None else max(1, min(int(max_depth), MAX_DEPTH))
    expanded_depth = (
        None
        if default_expanded_depth is None
        else max(0, min(int(default_expanded_depth), MAX_DEPTH))
    )

    count = int(
        conn.execute(_SQL_COUNT_SUBTREE, (node_id, MAX_DEPTH)).fetchone()[0]
    )
    if count > MAX_TREE_NODES:
        raise NodeError(
            f"这棵树有 {count} 个节点，超过 {MAX_TREE_NODES} 个上限，请拆成多棵树"
        )

    rows = conn.execute(
        _CTE_SUBTREE
        + " SELECT s.id AS node_id, s.parent_id AS cte_parent_id, s.position AS cte_position, "
        "s.depth AS cte_depth, n.kind AS kind, n.label AS concept_label, n.note AS note, "
        "n.entry_id AS entry_id, n.created_at AS created_at, n.updated_at AS updated_at, "
        "n.tombstone_label AS tombstone_label, n.tombstoned_at AS tombstoned_at, "
        "e.type AS entry_type, e.term AS entry_term, e.pos AS entry_pos, "
        "e.meaning_zh AS entry_meaning_zh, e.usage AS entry_usage, e.source AS entry_source, "
        "e.importance AS entry_importance, e.usefulness AS entry_usefulness, "
        "e.created_at AS entry_created_at, e.updated_at AS entry_updated_at, "
        "(SELECT COUNT(*) FROM node_edges ce WHERE ce.parent_id = s.id) AS child_count "
        "FROM sub s "
        "JOIN nodes n ON n.id = s.id "
        "LEFT JOIN entries e ON e.id = n.entry_id "
        "ORDER BY s.depth ASC, s.position ASC, s.id ASC",
        (node_id, depth_cap),
    ).fetchall()

    # 行里同时带 CTE 的 parent/position（树的真实拓扑）与节点的其余字段；
    # 转成 sqlite3.Row 之外的普通 dict 不好做，直接用 dict(row) 再补两个键。
    normalized = []
    for row in rows:
        item = dict(row)
        item["parent_id"] = item.pop("cte_parent_id")
        item["position"] = item.pop("cte_position")
        item["depth"] = int(item.pop("cte_depth"))
        normalized.append(item)

    entry_count = sum(1 for r in normalized if r["kind"] == KIND_ENTRY)
    concept_count = len(normalized) - entry_count
    tombstone_count = sum(1 for r in normalized if _is_tombstone(r))
    envelope = {
        "root_id": node_id,
        "shape": shape_value,
        "max_depth": depth_cap,
        "node_count": len(normalized),
        "entry_count": entry_count,
        "concept_count": concept_count,
        "tombstone_count": tombstone_count,
        "truncated": False,
    }

    # 树内成员之间的近义 / 反义组：层级块画布要拿它画横向虚线。
    # 放在这里而不是前端逐个节点去问，是因为打开层级块只需要一次往返。
    # ⚠ 只有横向关系、**不参与拓扑**：父子边仍由下面的 children 嵌套唯一决定，
    # 两者互不影响（这是 links 与 nodes 两套模型「不合并」的直接体现）。
    envelope["links"] = links.groups_within(
        conn, [r["entry_id"] for r in normalized if r["entry_id"] is not None]
    )

    if shape_value == SHAPE_FLAT:
        envelope["nodes"] = [
            _node_payload(r, depth=r["depth"], path=None, with_meta=False) for r in normalized
        ]
        return envelope

    # 嵌套组装：行已按 (depth, position) 排序，父一定先于子出现，一次遍历即可。
    by_id: dict[int, dict] = {}
    for row in normalized:
        by_id[int(row["node_id"])] = _node_payload(
            row, depth=row["depth"], path=None, with_meta=False
        )
    for row in normalized:
        payload = by_id[int(row["node_id"])]
        payload["children"] = []
    for row in normalized:
        payload = by_id[int(row["node_id"])]
        parent_id = payload["parent_id"]
        if parent_id is not None and parent_id in by_id:
            by_id[parent_id]["children"].append(payload)
    for payload in by_id.values():
        # has_children 让前端不用自己数 children：被 default_expanded_depth 剪掉的
        # 子树这里是 children=[] + has_children=true（见 6.2）。
        payload["has_children"] = payload["child_count"] > 0
        if (
            expanded_depth is not None
            and payload["depth"] > expanded_depth
            and payload["children"]
        ):
            payload["children"] = []

    root = by_id.get(node_id)
    if root is None:  # 理论不可达（前面已 fetch 过），但别让前端拿到 tree=null
        raise NodeNotFoundError(node_id)
    envelope["default_expanded_depth"] = expanded_depth
    envelope["tree"] = root
    return envelope


def list_all_nodes(
    conn: sqlite3.Connection,
    q: object = "",
    page: object = 1,
    page_size: object = 20,
) -> dict:
    """列全部节点（含非根），供「所有概念」选择器 / 按名字搜索用。

    与 list_roots 的区别只在 WHERE：**没有** NOT EXISTS 子句，其余分页与统计口径一致。
    排序取 (kind, label/term) 让概念节点聚在一起、真实词条按名字排——
    这个视图的用途是「挑一个节点」，不是「挑一棵树」。
    """
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    keyword = str(q or "").strip()
    pattern = f"%{entries._like_pattern(keyword)}%" if keyword else ""
    where = "WHERE (? = '' OR COALESCE(n.label, e.term, '') LIKE ?)"

    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM nodes n LEFT JOIN entries e ON e.id = n.entry_id {where}",
            (keyword, pattern),
        ).fetchone()[0]
    )
    rows = conn.execute(
        "SELECT n.id AS node_id, n.kind AS kind, n.label AS concept_label, n.note AS note, "
        "n.entry_id AS entry_id, n.updated_at AS updated_at, e.term AS entry_term, "
        "n.tombstone_label AS tombstone_label, n.tombstoned_at AS tombstoned_at, "
        "(SELECT parent_id FROM node_edges WHERE child_id = n.id) AS parent_id, "
        "(SELECT COUNT(*) FROM node_edges c WHERE c.parent_id = n.id) AS child_count "
        "FROM nodes n LEFT JOIN entries e ON e.id = n.entry_id "
        f"{where} "
        "ORDER BY n.kind ASC, COALESCE(n.label, e.term, '') COLLATE NOCASE ASC, n.id ASC "
        "LIMIT ? OFFSET ?",
        (keyword, pattern, page_size, (page - 1) * page_size),
    ).fetchall()

    items = []
    for row in rows:
        kind = row["kind"]
        items.append(
            {
                "node_id": int(row["node_id"]),
                "kind": kind,
                "real": kind == KIND_ENTRY,
                "tombstone": _is_tombstone(row),
                "tombstone_label": (row["tombstone_label"] or "")
                if _is_tombstone(row)
                else "",
                "label": row["concept_label"]
                if kind == KIND_CONCEPT
                else (row["entry_term"] or ""),
                "note": row["note"] or "",
                "entry_id": None if row["entry_id"] is None else int(row["entry_id"]),
                "parent_id": None if row["parent_id"] is None else int(row["parent_id"]),
                "child_count": int(row["child_count"] or 0),
                "updated_at": row["updated_at"],
            }
        )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def list_roots(
    conn: sqlite3.Connection,
    q: object = "",
    page: object = 1,
    page_size: object = 20,
) -> dict:
    """列根节点（＝放射图列表），带每棵树的统计。

    为什么要「根列表」而不是一张 concept_trees 表：因为「一个节点只有一个父」，
    森林的每个连通分量恰好是一棵以根为顶的树，**根节点自己就是那棵树**。
    少一张表 = 少一处需要维护的一致性。
    """
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    keyword = str(q or "").strip()
    pattern = f"%{entries._like_pattern(keyword)}%" if keyword else ""

    total = int(
        conn.execute(_SQL_COUNT_ROOTS, (keyword, pattern)).fetchone()[0]
    )
    rows = conn.execute(
        _SQL_ROOTS + " LIMIT ? OFFSET ?", (keyword, pattern, MAX_DEPTH, page_size, (page - 1) * page_size)
    ).fetchall()

    items = []
    for row in rows:
        kind = row["kind"]
        items.append(
            {
                "node_id": int(row["node_id"]),
                "kind": kind,
                "real": kind == KIND_ENTRY,
                "tombstone": _is_tombstone(row),
                "tombstone_label": (row["tombstone_label"] or "") if _is_tombstone(row) else "",
                "label": row["concept_label"] if kind == KIND_CONCEPT else (row["entry_term"] or ""),
                "note": row["note"] or "",
                "child_count": int(row["child_count"] or 0),
                "node_count": int(row["node_count"] or 0),
                "entry_count": int(row["entry_count"] or 0),
                "concept_count": int(row["concept_count"] or 0),
                "tombstone_count": int(row["tombstone_count"] or 0),
                "max_depth": int(row["max_depth"] or 0),
                "updated_at": row["updated_at"],
            }
        )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def purge_empty_nodes(conn: sqlite3.Connection) -> dict:
    """清理「从没被挂进任何树」的孤立词条节点行（需求①的数据层）。

    ★ 为什么要有这个函数 ★
    词汇库每个词条在 nodes 表都有一行（create_entry 播种）。其中绝大多数
    永远不会被挂进概念树，于是 nodes 表里堆了一大片纯噪音：不影响任何
    功能，但让「节点」相关的统计与查询都变重。用户要求清掉它们。

    ★ 删的是什么 / 不删什么 ★
    删：**nodes 表里既不是任何边的 parent、也不是任何边的 child** 的行，
    且这些行 kind='entry'（即 create_entry 播种的词条节点）。
    不删：
      - 参与任何一条边的行（父节点、子节点都留着）；
      - kind='concept' 的行 —— 即便它现在没挂任何东西，那也是**用户
        亲手建的**关联（可能正在编辑、或刚清空子项），不该由清理替他决定；
      - 占位节点（tombstone_label 非空）—— 它代表树上真实存在的结构。
    所以「用户从没建过的孤立词条节点」与「用户建了但现在是空的关联」
    被这条口径干净地区分开了。

    ★ 幂等 ★
    全部靠 SQL 条件判定，不依赖任何状态：第二次调用时剩下的行都参与边，
    命中 0 行，返回 deleted=0。绝不「启动时自动跑」——自动删会让用户觉得
    数据莫名消失，所以只由用户在关联库点「清理空壳」按钮触发（见 main.py
    的 POST /api/nodes/purge-empty）。
    """
    # 先数一遍：UI 要把「清掉了几个」如实报给用户（main.py 原样透传）。
    before = int(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])

    # 只删 nodes 表的行，不碰 entries（词条本身一个都不能少）。
    # 「既不是 parent 也不是 child」用两个 NOT EXISTS 表达 —— node_edges
    # 里 parent_id / child_id 任一命中就说明它参与了一棵树。
    cur = conn.execute(
        "DELETE FROM nodes WHERE kind = 'entry' "
        "  AND tombstone_label IS NULL "
        "  AND NOT EXISTS (SELECT 1 FROM node_edges x WHERE x.child_id = nodes.id) "
        "  AND NOT EXISTS (SELECT 1 FROM node_edges x WHERE x.parent_id = nodes.id)"
    )
    deleted = int(cur.rowcount or 0)
    conn.commit()

    after = int(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
    return {
        "deleted": deleted,
        "remaining": after,
        "before": before,
    }