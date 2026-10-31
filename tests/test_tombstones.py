"""占位节点（tombstone）测试 —— 需求 3：删词条时保留占位 / 提示。

运行：python test_tombstones.py
说明：用 ENPRACTICE_HOME 指向临时目录，不触碰真实数据。

覆盖用户拍板的五条语义：
  1. 删一个**在概念树里有子项**的词条 → 节点不消失，转成占位，子节点继续挂在它下面；
  2. 删一个**没有子项**的词条 → 直接清掉，不留占位（没有信息价值）；
  3. 占位节点**不能**跳词条详情、**不能**打分（结构保证，不是前端自觉）；
  4. 用户能主动清理占位（单个 + 全部）；
  5. 迁移幂等，反复 init_db 结果一致；存量节点不误变占位。

外加两条边界：
  - 关联库（近似词）侧**保持现状**：不留占位，靠外键 CASCADE + prune 清干净；
  - 「属性变更后展示一致」本来就不需要做（JOIN entries 实时取值），
    这里加一条回归断言钉住这个前提，防止后人「好心」加冗余 label 存回去。
"""

import json
import os
import shutil
import sqlite3
import tempfile

TMP = tempfile.mkdtemp(prefix="enpractice_tomb_")
os.environ["ENPRACTICE_HOME"] = TMP

from fastapi.testclient import TestClient  # noqa: E402

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, entries, links, nodes  # noqa: E402
from app.main import app  # noqa: E402

db.init_db()
client = TestClient(app)

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def section(title):
    print(f"\n  -- {title}")


def add(term, meaning=None, **kw):
    body = {"type": "word", "term": term, "meaning_zh": meaning or f"{term}的释义"}
    body.update(kw)
    r = client.post("/api/entries", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def concept(label, parent_id=None):
    payload = {"label": label}
    if parent_id is not None:
        payload["parent_id"] = parent_id
    r = client.post("/api/nodes/concept", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def get_node(node_id):
    r = client.get(f"/api/nodes/{node_id}")
    assert r.status_code == 200, r.text
    return r.json()


def tombstones():
    r = client.get("/api/nodes/tombstones")
    assert r.status_code == 200, r.text
    return r.json()


def conn():
    return db.connect()


# =====================================================================
section("语义 1：有子项的词条被删 → 留占位，子项仍挂在它下面")
# =====================================================================
root = concept("动物")
parent_entry = add("犬科动物")
parent_node = client.post(
    "/api/nodes/from-entry", json={"entry_id": parent_entry, "parent_id": root["node_id"]}
).json()["node"]
kid_entry = add("家犬")
kid_node = client.post(
    "/api/nodes/from-entry", json={"entry_id": kid_entry, "parent_id": parent_node["node_id"]}
).json()["node"]
grand_entry = add("拉布拉多")
grand_node = client.post(
    "/api/nodes/from-entry", json={"entry_id": grand_entry, "parent_id": kid_node["node_id"]}
).json()["node"]

check("删前：该词条节点是真实词条节点",
      parent_node["real"] is True and parent_node["kind"] == "entry" and parent_node["entry_id"] == parent_entry,
      json.dumps({k: parent_node[k] for k in ("kind", "real", "entry_id")}, ensure_ascii=False))
check("删前：不是占位", parent_node["tombstone"] is False)

tree_before = client.get(f"/api/nodes/{root['node_id']}/tree").json()["node_count"]

r = client.delete(f"/api/entries/{parent_entry}")
check("DELETE /api/entries/{id} -> 200", r.status_code == 200, r.text[:160])
payload = r.json()
check("响应里带回 tombstone 供前端提示",
      payload["tombstone"]["tombstoned"] == parent_node["node_id"]
      and payload["tombstone"]["tombstone_label"] == "犬科动物"
      and payload["tombstone"]["children_kept"] == 1,
      json.dumps(payload["tombstone"], ensure_ascii=False))

node = get_node(parent_node["node_id"])
check("★ 原节点行仍在（没有消失）",
      client.get(f"/api/nodes/{parent_node['node_id']}").status_code == 200)
check("★ 标记为占位节点", node["tombstone"] is True)
check("★ 保留原词条名用于显示",
      node["tombstone_label"] == "犬科动物" and node["label"] == "犬科动物",
      node["label"])
check("★ 有 tombstoned_at 时间戳（供 UI 提示何时删的）",
      bool(node["tombstoned_at"]), node["tombstoned_at"])
check("★ 子节点仍然挂在它下面（没被上提）",
      get_node(kid_node["node_id"])["parent_id"] == parent_node["node_id"],
      get_node(kid_node["node_id"])["parent_id"])
check("★ 孙节点也在（整棵子树完好）",
      get_node(grand_node["node_id"])["parent_id"] == kid_node["node_id"])
check("★ 树节点总数不变（留占位 = 什么都没少）",
      client.get(f"/api/nodes/{root['node_id']}/tree").json()["node_count"] == tree_before,
      f"{tree_before} -> {client.get(f'/api/nodes/{root['node_id']}/tree').json()['node_count']}")
check("词条本身确实没了",
      client.get(f"/api/entries/{parent_entry}").status_code == 404)
check("占位进了列表接口（用户能找到、也能清理）",
      tombstones()["total"] >= 1
      and any(t["node_id"] == parent_node["node_id"] for t in tombstones()["items"]))

# =====================================================================
section("语义 2：没有子项的词条被删 → 直接清掉，不留占位")
# =====================================================================
solo_entry = add("独行词")
solo_node = client.post("/api/nodes/from-entry", json={"entry_id": solo_entry}).json()["node"]
solo_node_id = solo_node["node_id"]

r = client.delete(f"/api/entries/{solo_entry}")
check("删无子项词条 -> 200", r.status_code == 200)
check("★ 后端明确告知「没有留占位」",
      r.json()["tombstone"] == {"tombstoned": None, "tombstone_label": "", "children_kept": 0},
      json.dumps(r.json()["tombstone"], ensure_ascii=False))
check("★ 节点行被 CASCADE 清掉（不留空壳）",
      client.get(f"/api/nodes/{solo_node_id}").status_code == 404)
check("★ 不在占位列表里",
      all(t["node_id"] != solo_node_id for t in tombstones()["items"]))

# 反向边界：删「子项」不会波及「父项」。
# 父词条此刻已经没有子项了，但它自己还是活的词条，节点必须保持真实词条节点，
# 既不能变成占位（那是删父才有的行为），也不能凭空消失。
mid_entry = add("临时词")
mid_node = client.post(
    "/api/nodes/from-entry", json={"entry_id": mid_entry, "parent_id": root["node_id"]}
).json()["node"]
leaf_entry = add("临时词的子项")
leaf_node = client.post(
    "/api/nodes/from-entry", json={"entry_id": leaf_entry, "parent_id": mid_node["node_id"]}
).json()["node"]
client.delete(f"/api/entries/{leaf_entry}")
mid2 = get_node(mid_node["node_id"])
check("★ 删子项不会把父项变成占位（占位只在删父时产生）",
      mid2["tombstone"] is False and mid2["real"] is True and mid2["entry_id"] == mid_entry,
      json.dumps({k: mid2[k] for k in ("tombstone", "real", "entry_id")}, ensure_ascii=False))
check("★ 子项节点行随外键 CASCADE 消失，父的 child_count 归零",
      mid2["child_count"] == 0
      and client.get(f"/api/nodes/{leaf_node['node_id']}").status_code == 404,
      str(mid2["child_count"]))
check("★ 此时再删父：已无子项 → 按规格直接删掉，不留占位",
      client.delete(f"/api/entries/{mid_entry}").json()["tombstone"]["tombstoned"] is None)
check("★ 父节点也随之消失", client.get(f"/api/nodes/{mid_node['node_id']}").status_code == 404)

# =====================================================================
section("语义 3：占位节点不可跳详情、不可打分（结构保证）")
# =====================================================================
check("★ 占位节点 real=false（前端不会去开词条详情）", node["real"] is False)
check("★ 占位节点 entry=null（没有任何词条数据可展示）", node["entry"] is None)
check("★ 占位节点 entry_id=null（没有可跳转的词条 id）", node["entry_id"] is None)
check("★ 占位节点 kind=concept（不在 entries 表里，评分/练习/复习全都读不到它）",
      node["kind"] == "concept", node["kind"])
banned = ("importance", "usefulness", "highlight", "due_at")
check("★ 响应里不存在任何评分键（缺键而非 null）",
      not [k for k in banned if k in node], sorted(node))
tree_node = client.get(f"/api/nodes/{root['node_id']}/tree").json()["tree"]


def find_in_tree(n, target):
    if n["node_id"] == target:
        return n
    for c in n.get("children", []):
        hit = find_in_tree(c, target)
        if hit:
            return hit
    return None


tn = find_in_tree(tree_node, parent_node["node_id"])
check("★ 树接口里占位节点同样 real=false 且无评分键",
      tn is not None and tn["real"] is False and tn["tombstone"] is True
      and not [k for k in banned if k in tn],
      json.dumps({k: tn[k] for k in ("real", "tombstone", "entry")}, ensure_ascii=False))
check("★ 面包屑（path）里也标了占位，祖先链不会把它说成正常概念",
      isinstance(node["path"], list)
      and all("tombstone" in p for p in node["path"]),
      json.dumps(node["path"], ensure_ascii=False))

# 评分硬约束：占位节点永远不会进复习 / 练习候选池（它们只在 nodes 表里）
c = conn()
try:
    check("★ nodes 表里占位行没有 entry_id（外键已断开，CASCADE 打不着）",
          c.execute("SELECT entry_id FROM nodes WHERE id = ?", (parent_node["node_id"],)
                    ).fetchone()[0] is None)
    check("★ 占位节点不会出现在 entries 表里（结构性保证，非过滤）",
          c.execute("SELECT COUNT(*) FROM entries WHERE id = ?", (parent_entry,)).fetchone()[0] == 0)
    check("★ 也不会被 _seed_review_state 播种出 review_state 行",
          c.execute("SELECT COUNT(*) FROM review_state WHERE entry_id = ?", (parent_entry,)
                    ).fetchone()[0] == 0)
finally:
    c.close()

# 对占位节点改名 = 允许的（用户可以把它整理成一个真正的概念节点），
# 但「改成真实词条」这条路不存在 —— 没有 from-entry 之外的复活入口。
r = client.patch(f"/api/nodes/{parent_node['node_id']}", json={"label": "犬科（已删）"})
check("占位节点可改名（用户可把它整理成普通概念节点）",
      r.status_code == 200 and r.json()["label"] == "犬科（已删）", r.text[:120])
check("改名后仍是占位（不会因为改名就变成真实节点）",
      r.json()["tombstone"] is True and r.json()["real"] is False)

# =====================================================================
section("语义 4：用户能主动清理占位（单个 / 全部）")
# =====================================================================
# 先构造一个「占位套占位」的结构，验证清理顺序不会把用户的树吃掉
chain_root = concept("清理测试根")
chain_parent_entry = add("将被清理的父词")
chain_parent = client.post(
    "/api/nodes/from-entry",
    json={"entry_id": chain_parent_entry, "parent_id": chain_root["node_id"]},
).json()["node"]
chain_mid_entry = add("将被清理的子词")
chain_mid = client.post(
    "/api/nodes/from-entry",
    json={"entry_id": chain_mid_entry, "parent_id": chain_parent["node_id"]},
).json()["node"]
chain_leaf_entry = add("要被保住的孙词")
chain_leaf = client.post(
    "/api/nodes/from-entry",
    json={"entry_id": chain_leaf_entry, "parent_id": chain_mid["node_id"]},
).json()["node"]

client.delete(f"/api/entries/{chain_mid_entry}")
mid_state = get_node(chain_mid["node_id"])
check("构造出「占位下面挂着一个真实词条」",
      mid_state["tombstone"] is True
      and get_node(chain_leaf["node_id"])["parent_id"] == chain_mid["node_id"],
      json.dumps({k: mid_state[k] for k in ("tombstone", "child_count")}, ensure_ascii=False))

# 4a. 对**非占位**节点调 purge 必须 400（防手滑清掉用户真实结构）
r = client.post(f"/api/nodes/{chain_leaf['node_id']}/purge")
check("★ 对正常节点调 purge -> 400（不会误清真实结构）",
      r.status_code == 400, f"{r.status_code} {r.text[:120]}")

# 4b. 单个清理：子项提升，占位消失
r = client.post(f"/api/nodes/{chain_mid['node_id']}/purge")
check("清理单个占位 -> 200", r.status_code == 200, r.text[:160])
check("★ 占位子节点被提升（promoted_nodes 有一个）",
      r.json()["promoted_nodes"] == [chain_leaf["node_id"]], json.dumps(r.json()["promoted_nodes"]))
check("★ 占位行已删除", client.get(f"/api/nodes/{chain_mid['node_id']}").status_code == 404)
check("★ 被提升的子节点挂到了祖父下（没有丢失）",
      get_node(chain_leaf["node_id"])["parent_id"] == chain_parent["node_id"],
      get_node(chain_leaf["node_id"])["parent_id"])
check("★ 被提升的子节点**没有被删**（清理占位 ≠ 删用户的树）",
      client.get(f"/api/entries/{chain_leaf_entry}").status_code == 200)

# 4c. 批量清理：先造出「占位套占位」的多层结构，验证批量清理的顺序正确
bulk_root = concept("批量清理根")
bulk_prev = bulk_root["node_id"]
bulk_nodes = []
for i in range(3):
    e_id = add(f"批量占位词{i}")
    n = client.post("/api/nodes/from-entry",
                    json={"entry_id": e_id, "parent_id": bulk_prev}).json()["node"]
    bulk_nodes.append(n)
    bulk_prev = n["node_id"]
# 最后一个词条再挂一个子项，确保最深处也有真实数据要保住
bulk_keep = add("批量清理要保住的词")
client.post("/api/nodes/from-entry",
            json={"entry_id": bulk_keep, "parent_id": bulk_nodes[-1]["node_id"]})
for n in bulk_nodes:
    client.delete(f"/api/entries/{n['entry_id']}")
check("★ 三个占位层层相套（父子都是占位）",
      all(get_node(n["node_id"])["tombstone"] for n in bulk_nodes)
      and all(get_node(n["node_id"])["child_count"] >= 1 for n in bulk_nodes[:-1]))

before_total = tombstones()["total"]
check("批量清理前确实有多个占位", before_total >= 3, str(before_total))
r = client.post("/api/nodes/tombstones/purge-all")
check("purge-all -> 200", r.status_code == 200, r.text[:160])
check("★ 清理数量与清理前一致",
      r.json()["purged_count"] == before_total,
      f"before={before_total} purged={r.json()['purged_count']}")
check("★ 清完列表为空", tombstones()["total"] == 0, str(tombstones()["total"]))
check("★ 真实词条节点全部还在（批量清理没伤用户数据）",
      client.get(f"/api/entries/{chain_leaf_entry}").status_code == 200
      and client.get(f"/api/entries/{kid_entry}").status_code == 200
      and client.get(f"/api/entries/{grand_entry}").status_code == 200
      and client.get(f"/api/entries/{bulk_keep}").status_code == 200)
check("★ 最深处那个真实词条一路被提升到了根下（没被级联吃掉）",
      client.get(f"/api/entries/{bulk_keep}").status_code == 200)
c_bulk = conn()
try:
    bulk_keep_node_id = nodes.node_of_entry(c_bulk, bulk_keep)
    check("★ 最深处真实词条节点的父是最初的根（层级一路提升到底）",
          bulk_keep_node_id is not None
          and get_node(bulk_keep_node_id)["parent_id"] == bulk_root["node_id"],
          str(get_node(bulk_keep_node_id)["parent_id"]) if bulk_keep_node_id else "节点不存在")
finally:
    c_bulk.close()
check("★ 幂等：再清一次是零行改动",
      client.post("/api/nodes/tombstones/purge-all").json()["purged_count"] == 0)

# 兄弟序不能因清理出现空洞
sibs = client.get(f"/api/nodes/{bulk_root['node_id']}/tree").json()["tree"]["children"]
check("★ 清理后父节点子节点 position 连续无空洞",
      [s["position"] for s in sibs] == list(range(len(sibs))),
      str([s["position"] for s in sibs]))

# =====================================================================
section("对照：关联库（近似词）侧保持现状 —— 不留占位，清干净")
# =====================================================================
a = add("同义甲")
b = add("同义乙")
c_entry = add("同义丙")
r = client.post("/api/links", json={"entry_ids": [a, b, c_entry], "relation": "synonym"})
check("建三成员关联组 -> 201", r.status_code == 201, r.text[:120])
group_id = r.json()["id"]

client.delete(f"/api/entries/{c_entry}")
check("★ 关联组里不再有已删词条的残留成员",
      all(m["id"] != c_entry for m in client.get(f"/api/links/{group_id}").json()["members"]))
check("★ 组仍在（还剩 2 人，合法）",
      client.get(f"/api/links/{group_id}").status_code == 200
      and len(client.get(f"/api/links/{group_id}").json()["members"]) == 2)

# 删到只剩 1 人 -> 整组解散（不留死组，更不留占位）
client.delete(f"/api/entries/{b}")
check("★ 组解散（查不到同伴的死组不留）",
      client.get(f"/api/links/{group_id}").status_code == 404)
c2 = conn()
try:
    check("★ 关联表里没有残留成员行",
          c2.execute("SELECT COUNT(*) FROM entry_link_items WHERE entry_id IN (?, ?, ?)",
                     (a, b, c_entry)).fetchone()[0] == 0)
    check("★ nodes 表里那几个词条没有变成占位（新建词条本来就有节点行，但 real=true）",
          c2.execute(
              "SELECT COUNT(*) FROM nodes WHERE tombstone_label IS NOT NULL "
              "AND entry_id IN (?, ?, ?)", (a, b, c_entry)
          ).fetchone()[0] == 0)
finally:
    c2.close()
check("★ list_tombstones 里也没有关联库侧的残留",
      all(t["tombstone_label"] not in ("同义甲", "同义乙", "同义丙") for t in tombstones()["items"]))

# =====================================================================
section("前提回归：属性变更后展示一致（本来就不需要做，钉住它）")
# =====================================================================
e = add("改名测试词", meaning="旧释义")
n = client.post("/api/nodes/from-entry", json={"entry_id": e}).json()["node"]
check("建树时显示的是词条名", get_node(n["node_id"])["label"] == "改名测试词")
client.put(f"/api/entries/{e}", json={"type": "word", "term": "改名后", "meaning_zh": "新释义"})
check("★ 改词条名后树上显示同步（JOIN 实时取值，无冗余 label）",
      get_node(n["node_id"])["label"] == "改名后", get_node(n["node_id"])["label"])
check("★ 改释义后副行同步",
      get_node(n["node_id"])["subtitle"] == "新释义", get_node(n["node_id"])["subtitle"])
client.patch(f"/api/entries/{e}/ratings", json={"importance": 4.5})
check("★ 打分后树上星级读得到（评分也是实时 JOIN）",
      get_node(n["node_id"])["entry"]["importance"] == 4.5)
c3 = conn()
try:
    check("★ 数据库里没有冗余存 label 的列（label 列仅概念节点在用）",
          c3.execute(
              "SELECT label FROM nodes WHERE entry_id = ?", (e,)
          ).fetchone()[0] is None)
finally:
    c3.close()

# =====================================================================
section("迁移幂等 + 存量节点不误变占位")
# =====================================================================
# 造一个「旧库」形态：nodes 表没有 tombstone 两列
LEGACY_HOME = tempfile.mkdtemp(prefix="enpractice_tomb_legacy_")
os.environ["ENPRACTICE_HOME"] = LEGACY_HOME
shutil.rmtree(LEGACY_HOME, ignore_errors=True)
db.init_db()

c4 = conn()
try:
    c4.executescript(
        """
        DROP TABLE IF EXISTS node_edges;
        DROP TABLE IF EXISTS nodes;
        CREATE TABLE nodes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            kind        TEXT    NOT NULL CHECK(kind IN ('concept', 'entry')),
            label       TEXT,
            note        TEXT    NOT NULL DEFAULT '',
            entry_id    INTEGER REFERENCES entries(id) ON DELETE CASCADE,
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL,
            CHECK (
                (kind = 'concept' AND label IS NOT NULL AND entry_id IS NULL) OR
                (kind = 'entry'   AND label IS NULL     AND entry_id IS NOT NULL)
            )
        );
        CREATE UNIQUE INDEX idx_nodes_entry ON nodes(entry_id) WHERE entry_id IS NOT NULL;
        """
    )
    legacy_entry = entries.create_entry(
        c4, {"type": "word", "term": "旧库词", "meaning_zh": "旧库的释义"}
    )
    c4.commit()
    # 模拟旧库真实状态：存量词条 + 存量概念节点，且**没有** tombstone 两列
    c4.execute(
        "INSERT INTO nodes (kind, label, note, entry_id, created_at, updated_at) "
        "VALUES ('concept', '旧概念', '', NULL, '2024-01-01T00:00:00', '2024-01-01T00:00:00')"
    )
    c4.commit()
    cols_before = [r[1] for r in c4.execute("PRAGMA table_info(nodes)").fetchall()]
    c4.close()

    check("★ 旧库的 nodes 表确实没有 tombstone 两列",
          "tombstone_label" not in cols_before and "tombstoned_at" not in cols_before,
          str(cols_before))
finally:
    pass

# 反复 init_db：迁移必须可重复执行
for i in range(3):
    db.init_db()
check("★ 反复 init_db 不抛错（迁移幂等）", True, "连续执行 3 次")

c5 = conn()
try:
    cols_after = [r[1] for r in c5.execute("PRAGMA table_info(nodes)").fetchall()]
    check("★ 迁移后两列都在", "tombstone_label" in cols_after and "tombstoned_at" in cols_after,
          str(cols_after))
    info = c5.execute("PRAGMA table_info(nodes)").fetchall()
    notnull = {r[1]: r[3] for r in info}
    check("★ 两列都是可空（ADD COLUMN 原地扩展，不重建表）",
          notnull.get("tombstone_label") == 0 and notnull.get("tombstoned_at") == 0,
          json.dumps({k: notnull.get(k) for k in ("tombstone_label", "tombstoned_at")}))
    check("★ 列顺序在原有列之后（追加而非插入，老库数据偏移不变）",
          [r[1] for r in info][:7] ==
          ["id", "kind", "label", "note", "entry_id", "created_at", "updated_at"],
          str([r[1] for r in info]))

    legacy_rows = c5.execute(
        "SELECT kind, label, tombstone_label, tombstoned_at FROM nodes ORDER BY id"
    ).fetchall()
    check("★ 存量节点一条都没变成占位（tombstone_label 恒为 NULL）",
          all(r["tombstone_label"] is None for r in legacy_rows),
          json.dumps([dict(r) for r in legacy_rows], ensure_ascii=False))
    check("★ 存量数据行数与内容没被动过（不重建表 = 不丢数据）",
          len(legacy_rows) == 2
          and any(r["kind"] == "concept" and r["label"] == "旧概念" for r in legacy_rows)
          and any(r["kind"] == "entry" for r in legacy_rows),
          f"{len(legacy_rows)} 行")
    check("★ 旧库里的词条仍然能建树（迁移没破坏既有功能）",
          nodes.attach_entry(c5, legacy_entry["id"])["node"]["real"] is True)
    c5.close()
finally:
    pass

# 新库天然带这两列，且建表即幂等
NEW_HOME = tempfile.mkdtemp(prefix="enpractice_tomb_new_")
shutil.rmtree(NEW_HOME, ignore_errors=True)
os.environ["ENPRACTICE_HOME"] = NEW_HOME
db.init_db()
db.init_db()
c6 = conn()
try:
    check("★ 全新库也带这两列（SCHEMA 已含，迁移判重后跳过）",
          "tombstone_label" in [r[1] for r in c6.execute("PRAGMA table_info(nodes)").fetchall()])
    check("★ 全新库里没有任何占位", nodes.list_tombstones(c6)["total"] == 0)
finally:
    c6.close()

# 回到主库做收尾断言
os.environ["ENPRACTICE_HOME"] = TMP

# =====================================================================
section("XSS 红线：占位显示原词条名时后端原样透传")
# =====================================================================
XSS = '<img src=x onerror="alert(1)">'
x_entry = add(XSS, meaning="恶意词条")
x_root = concept("xss根")
x_parent = client.post(
    "/api/nodes/from-entry", json={"entry_id": x_entry, "parent_id": x_root["node_id"]}
).json()["node"]
x_child = concept("<b>子</b>", parent_id=x_parent["node_id"])
client.delete(f"/api/entries/{x_entry}")
x_state = get_node(x_parent["node_id"])
check("★ 载荷词条被删后占位原样保留该文本（后端不转义也不改写）",
      x_state["tombstone_label"] == XSS and x_state["label"] == XSS, x_state["label"])
check("★ 载荷在 JSON 里被正确转义（不是裸 HTML）",
      json.loads(json.dumps(x_state))["tombstone_label"] == XSS)
check("★ 响应里没有任何 html / html_content 字段",
      not [k for k in x_state if "html" in k.lower()], sorted(x_state))

# 前端源码层面：占位渲染不得出现 innerHTML拼接
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "app.js"),
          encoding="utf-8") as fh:
    appjs = fh.read()
# ★ 本次改动（关联库改成「块列表 + 画布编辑器」）修的定位 ★
# 原来这段切的是放射图的 renderNode()…onNodeClick()，占位节点的渲染在那里。
# 放射图连同整个旧面板一起被画布取代后，renderNode 已不存在，
# str.find 找不到会返回 -1，切片会变成「最后一个字符往后」的一段垃圾文本 ——
# 第一条断言会**假通过**（垃圾片段里当然没有 innerHTML），第二条必然失败。
# 现在占位节点渲染在画布的 renderEntryNode / renderConceptNode / buildPurgeButton 里，
# 切片范围相应改成这几个函数（到关系菜单为止）。
# 断言的**意图不变**：占位标记与清理按钮的文案只能走 textContent。
seg_start = appjs.find("function renderEntryNode(")
seg_end = appjs.find("function openRelationMenu(", seg_start)
assert seg_start != -1, "找不到画布节点渲染函数 renderEntryNode"
assert seg_end != -1, "找不到关系菜单函数 openRelationMenu"
render_node_src = appjs[seg_start:seg_end]
check("★ 画布节点渲染里没有 innerHTML（占位名只能走 textContent）",
      "innerHTML" not in render_node_src)
check("★ 画布节点里占位标记与清理按钮都走 textContent",
      "badge.textContent" in render_node_src and "btn.textContent" in render_node_src)
check("★ 占位节点有专门的 class 与 data 标记（CSS 与测试都靠它识别）",
      "cv-tombstone" in render_node_src)

# =====================================================================
print("\n" + "=" * 60)
failed = [name for name, ok in results if not ok]
print(f"总计：{'全部通过' if not failed else '有失败'}（{len(results)} 项）")
if failed:
    for name in failed:
        print("  FAIL:", name)
    raise SystemExit(1)
print("=" * 60)
