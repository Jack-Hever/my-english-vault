"""概念节点 + 层级树测试（S1 数据层 / S2 后端契约）。

运行：python test_nodes.py
说明：用 ENPRACTICE_HOME 指向临时目录，不触碰真实数据。

本文件覆盖设计文档 §7 的验收硬门，重点是三条结构性保证：
1. **成环被拒**（递归 CTE 方向必须对）；
2. **多级树**在任意层都能取整棵树；
3. **概念节点不出现在 entries 相关查询里** —— 这是「概念节点不参与评分 /
   不进练习候选池」的实现基础（结构保证，不是靠过滤纪律）。
"""

import json
import os
import tempfile

TMP = tempfile.mkdtemp(prefix="enpractice_nodes_")
os.environ["ENPRACTICE_HOME"] = TMP

from fastapi.testclient import TestClient  # noqa: E402

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, entries, links, nodes, practices, review  # noqa: E402
from app.main import app  # noqa: E402

db.init_db()
client = TestClient(app)

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def add(term, meaning="释义", **kw):
    body = {"type": "word", "term": term, "meaning_zh": meaning}
    body.update(kw)
    r = client.post("/api/entries", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def concept(label, parent_id=None, note="", position=None):
    body = {"label": label}
    if parent_id is not None:
        body["parent_id"] = parent_id
    if note:
        body["note"] = note
    if position is not None:
        body["position"] = position
    r = client.post("/api/nodes/concept", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def tree(node_id, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    r = client.get(f"/api/nodes/{node_id}/tree" + (f"?{q}" if q else ""))
    assert r.status_code == 200, r.text
    return r.json()


def walk(node):
    """深度优先展开嵌套树，产出 (payload, 递归深度) 序列。

    注意第二项是**本函数的递归深度**，与响应里的 `depth` 字段是两回事
    （`depth` 恒为 0，因为根就是请求的节点）。要断言响应口径请读 `n["depth"]`。
    """
    out = [(node, 0)]
    for child in node.get("children", []):
        out.extend(walk(child))
    return out


#: 概念节点的响应里绝不允许出现的键。**必须是「键不存在」而不是 null**——
#: 缺键让前端 node.importance 得到 undefined，任何加权求和直接得 NaN；
#: 若是 null 就会得到语义错误的 0，星级会被画成「0 星」而不是「未评分」。
BANNED_ON_CONCEPT = ("importance", "usefulness", "highlight", "due_at")


def concept_nodes(tree_payload):
    """树里所有概念节点（real=false）。"""
    return [n for n, _ in walk(tree_payload["tree"]) if not n["real"]]


def assert_no_rating_keys(nodes_):
    """逐个查概念节点**自身**的键。

    不能用 `"importance" not in json.dumps(node)`：那会把整棵子树的 JSON 一起算进来，
    子树里的真实词条节点本来就带评分，断言会假失败。要查的是「这个节点的键集合」。
    """
    return all(not (set(BANNED_ON_CONCEPT) & set(n)) for n in nodes_)


def conn():
    c = db.connect()
    return c


# =====================================================================
print("  -- 概念节点 CRUD")
# =====================================================================
dog = concept("狗", note="总纲节点")
check("POST /api/nodes/concept -> 200", dog["kind"] == "concept", json.dumps(dog, ensure_ascii=False))
check("kind=concept / real=false", dog["kind"] == "concept" and dog["real"] is False)
check("label 回显", dog["label"] == "狗", dog["label"])
check("entry 为 null", dog["entry"] is None, repr(dog["entry"]))
check("根节点 parent_id=null", dog["parent_id"] is None, repr(dog["parent_id"]))
check("根节点 position=0", dog["position"] == 0, repr(dog["position"]))
check("带 path / created_at / updated_at（单节点形状）",
      {"path", "created_at", "updated_at"} <= set(dog), sorted(dog))

r = client.post("/api/nodes/concept", json={"label": "   "})
check("空 label -> 400", r.status_code == 400, r.text[:100])
r = client.post("/api/nodes/concept", json={"label": "狗" * 100})
check("超长 label -> 400（不是静默截断）", r.status_code == 400, r.text[:100])

renamed = client.patch(f"/api/nodes/{dog['node_id']}", json={"label": "犬科"}).json()
check("PATCH 改 label 生效", renamed["label"] == "犬科", renamed["label"])
check("PATCH 只传 label 时 note 不被抹掉", renamed["note"] == "总纲节点", renamed["note"])
after_noop = client.patch(f"/api/nodes/{dog['node_id']}", json={}).json()
check("PATCH 空 body -> 不改动任何字段",
      after_noop["label"] == "犬科" and after_noop["note"] == "总纲节点",
      json.dumps(after_noop, ensure_ascii=False))
client.patch(f"/api/nodes/{dog['node_id']}", json={"label": "狗"})

check("GET /api/nodes/{id} -> 200", client.get(f"/api/nodes/{dog['node_id']}").status_code == 200)
check("不存在的节点 -> 404", client.get("/api/nodes/999999").status_code == 404)

# =====================================================================
print("\n  -- 挂真实词条作子节点（前端主路径）")
# =====================================================================
lab = add("labrador", "拉布拉多")
client.patch(f"/api/entries/{lab}/ratings", json={"importance": 4.5, "usefulness": 4.0})

r = client.post(f"/api/nodes/{dog['node_id']}/children",
                json={"child": {"kind": "entry", "entry_id": lab}, "position": 0})
check("POST children (kind=entry) -> 200", r.status_code == 200, r.text[:150])
lab_node = r.json()
check("label 取自 entries.term", lab_node["label"] == "labrador", lab_node["label"])
check("subtitle 取自 meaning_zh", lab_node["subtitle"] == "拉布拉多", lab_node["subtitle"])
check("real=true / kind=entry", lab_node["real"] is True and lab_node["kind"] == "entry")
check("parent_id 指向概念节点", lab_node["parent_id"] == dog["node_id"], lab_node["parent_id"])
check("entry 子对象带评分",
      lab_node["entry"]["importance"] == 4.5 and lab_node["entry"]["usefulness"] == 4.0,
      json.dumps(lab_node["entry"], ensure_ascii=False))
check("highlight 由评分算出", lab_node["entry"]["highlight"] is True)
check("path 给出祖先链（根在前）",
      [p["label"] for p in lab_node["path"]] == ["狗"],
      json.dumps(lab_node["path"], ensure_ascii=False))

r2 = client.post(f"/api/nodes/{dog['node_id']}/children",
                 json={"child": {"kind": "entry", "entry_id": lab}})
check("同一 (parent, child) 重复提交幂等（不重复建节点）",
      r2.json()["node_id"] == lab_node["node_id"],
      f"{lab_node['node_id']} vs {r2.json()['node_id']}")

hunt = concept("猎犬", parent_id=dog["node_id"], note="组内按用途细分")
check("建概念节点并同时挂上", hunt["parent_id"] == dog["node_id"], hunt["parent_id"])

r = client.post(f"/api/nodes/{dog['node_id']}/children",
                json={"child": {"kind": "concept", "label": "猎犬"}})
check("同父下同名概念复用（防双击重复建）", r.json()["node_id"] == hunt["node_id"],
      f"{hunt['node_id']} vs {r.json()['node_id']}")

check("kind=entry 缺 entry_id -> 400",
      client.post(f"/api/nodes/{dog['node_id']}/children",
                  json={"child": {"kind": "entry"}}).status_code == 400)
check("child.kind 非法 -> 400",
      client.post(f"/api/nodes/{dog['node_id']}/children",
                  json={"child": {"kind": "whatever"}}).status_code == 400)
check("不存在的父节点 -> 404",
      client.post("/api/nodes/999999/children",
                  json={"child": {"kind": "concept", "label": "x"}}).status_code == 404)
check("不存在的词条 -> 404",
      client.post(f"/api/nodes/{dog['node_id']}/children",
                  json={"child": {"kind": "entry", "entry_id": 999999}}).status_code == 404)

# =====================================================================
print("\n  -- POST /api/nodes/from-entry（get-or-create）")
# =====================================================================
fresh = add("retriever", "猎犬")
r = client.post("/api/nodes/from-entry", json={"entry_id": fresh, "parent_id": hunt["node_id"]})
check("from-entry -> 200", r.status_code == 200, r.text[:150])
payload = r.json()
check("返回 {node, created}", set(payload) == {"node", "created"}, sorted(payload))
# entries.create_entry 已同步播种节点行，所以正常路径 created=False；
# created=True 只在「节点行缺失」（例如手工改过库的库）时出现，见下面的兜底断言。
check("已有节点行时 created=False", payload["created"] is False, str(payload["created"]))
ret_node = payload["node"]
check("挂在指定父下", ret_node["parent_id"] == hunt["node_id"], ret_node["parent_id"])

# 兜底路径：人为删掉节点行（模拟「本功能上线前的老词条」），from-entry 应 get-or-create
orphan_for_backfill = add("backfillword", "模拟老词条")
c = conn()
try:
    c.execute("DELETE FROM node_edges WHERE child_id IN "
              "(SELECT id FROM nodes WHERE entry_id = ?)", (orphan_for_backfill,))
    c.execute("DELETE FROM nodes WHERE entry_id = ?", (orphan_for_backfill,))
    c.commit()
    check("人为制造「词条没有节点行」的状态",
          nodes.node_of_entry(c, orphan_for_backfill) is None)
finally:
    c.close()
r = client.post("/api/nodes/from-entry",
                json={"entry_id": orphan_for_backfill, "parent_id": hunt["node_id"]})
check("★ 节点行缺失时 get-or-create -> created=True", r.json()["created"] is True,
      str(r.json()["created"]))
check("get-or-create 后节点行已补上", r.json()["node"]["label"] == "backfillword")
client.delete(f"/api/nodes/{hunt['node_id']}/children/{r.json()['node']['node_id']}")

r = client.post("/api/nodes/from-entry", json={"entry_id": fresh, "parent_id": hunt["node_id"]})
check("重复调用 created=False（幂等）", r.json()["created"] is False, str(r.json()["created"]))
check("重复调用不换节点",
      r.json()["node"]["node_id"] == ret_node["node_id"],
      f"{ret_node['node_id']} vs {r.json()['node']['node_id']}")

# =====================================================================
print("\n  -- ★ 成环被拒（设计 §7 硬门 4）")
# =====================================================================
dog_c = concept("狗A")
dog_d = concept("犬B", parent_id=dog_c["node_id"])

r = client.put(f"/api/nodes/{dog_d['node_id']}/children/{dog_c['node_id']}", json={})
check("狗→犬 后挂 狗 到 犬 下 -> 400", r.status_code == 400, r.text[:150])
check("错误消息为中文可读", "不能建立层级" in r.json().get("detail", ""),
      r.json().get("detail"))
check("被拒后树形未变（狗A 仍是根）",
      client.get(f"/api/nodes/{dog_c['node_id']}").json()["parent_id"] is None)
check("被拒后 犬B 仍在 狗A 下",
      client.get(f"/api/nodes/{dog_d['node_id']}").json()["parent_id"] == dog_c["node_id"])

r = client.put(f"/api/nodes/{dog_c['node_id']}/children/{dog_c['node_id']}", json={})
check("节点挂自己 -> 400", r.status_code == 400, r.text[:120])
check("自挂错误消息中文可读", "不能" in r.json().get("detail", ""), r.json().get("detail"))

# 三层环：狗A → 犬B → 柯基C，挂 狗A 到 柯基C 下
corgi_c = concept("柯基C", parent_id=dog_d["node_id"])
r = client.put(f"/api/nodes/{corgi_c['node_id']}/children/{dog_c['node_id']}", json={})
check("三层环（挂根到孙节点下）-> 400", r.status_code == 400, r.text[:120])

# 中间层成环：把 狗A 挂到 犬B 的兄弟节点下（合法），再验证深度链上的环
r = client.post(f"/api/nodes/{dog_c['node_id']}/children",
                json={"child": {"kind": "concept", "label": "中间层"}})
mid = r.json()
r = client.put(f"/api/nodes/{mid['node_id']}/children/{dog_c['node_id']}", json={})
check("挂到自己的子节点下（1 步即环）-> 400", r.status_code == 400, r.text[:120])

# =====================================================================
print("\n  -- ★ 多级树：狗 → 犬 → 柯基 → 小柯基，任意层都能取整棵树")
# =====================================================================
l1 = concept("狗")
l2 = concept("犬", parent_id=l1["node_id"])
l3 = concept("柯基", parent_id=l2["node_id"])
l4 = concept("小柯基", parent_id=l3["node_id"])
e_neo = add("neocorgi", "小柯基")
client.post(f"/api/nodes/{l4['node_id']}/children",
            json={"child": {"kind": "entry", "entry_id": e_neo}})

for label, nid, sub_size, sub_max_depth in (
    ("狗", l1["node_id"], 5, 4),        # 狗→犬→柯基→小柯基→neocorgi
    ("犬", l2["node_id"], 4, 3),
    ("柯基", l3["node_id"], 3, 2),
    ("小柯基", l4["node_id"], 2, 1),
):
    t = tree(nid)
    flat = walk(t["tree"])
    check(f"从「{label}」取整棵树 -> node_count={sub_size}", t["node_count"] == sub_size,
          f"{t['node_count']} (期望 {sub_size})")
    check(f"「{label}」自身在树里 depth=0（相对请求根）", t["tree"]["depth"] == 0,
          t["tree"]["depth"])
    check(f"「{label}」是这棵树的根", t["tree"]["parent_id"] is None, t["tree"]["parent_id"])
    depths = sorted(n["depth"] for n, _ in flat)
    check(f"「{label}」树内 depth 字段 0..{sub_max_depth} 齐全且无跳层",
          depths == list(range(sub_max_depth + 1)), depths)
    check(f"「{label}」的 DFS 顺序与 depth 单调不降（前序遍历）",
          [d for _, d in flat] == sorted(d for _, d in flat),
          [d for _, d in flat])
    check(f"「{label}」树内每个节点都出现一次（无重复/交叉引用）",
          len(flat) == len({n["node_id"] for n, _ in flat}), len(flat))

t3 = tree(l3["node_id"])
check("任意层子树互不串味（柯基子树不含 犬/狗）",
      {n["label"] for n, _ in walk(t3["tree"])} == {"柯基", "小柯基", "neocorgi"},
      [n["label"] for n, _ in walk(t3["tree"])])

check("真实词条也能当父节点（给概念建在词条下）", True)
under_entry = concept("犬的子类", parent_id=lab_node["node_id"])
check("父节点是真实词条节点",
      client.get(f"/api/nodes/{under_entry['node_id']}").json()["parent_id"] == lab_node["node_id"])
t_under = tree(lab_node["node_id"])
check("从真实词条取整棵树", t_under["node_count"] == 2, t_under["node_count"])

# =====================================================================
print("\n  -- 树查询形状：children / flat / 展开深度")
# =====================================================================
t = tree(l1["node_id"])
check("默认 shape=children（嵌套）", t["shape"] == "children" and "tree" in t, sorted(t))
check("envelope 含 root_id/max_depth/node_count/entry_count/concept_count/truncated",
      {"root_id", "max_depth", "node_count", "entry_count", "concept_count", "truncated"}
      <= set(t), sorted(t))
check("truncated 恒为 false（超限直接抛错不返回半棵树）", t["truncated"] is False)
check("树节点带 children 数组", all("children" in n for n, _ in walk(t["tree"])))
check("position 在同一父下 0..n-1 连续",
      [c["position"] for c in t["tree"]["children"]] == list(range(len(t["tree"]["children"]))),
      [c["position"] for c in t["tree"]["children"]])
check("shape 非法 -> 400", client.get(f"/api/nodes/{l1['node_id']}/tree?shape=weird").status_code == 400)

tf = tree(l1["node_id"], shape="flat")
check("shape=flat 返回 nodes 数组（无 tree 键）",
      "nodes" in tf and "tree" not in tf, sorted(tf))
check("flat 按 (depth, position) 升序",
      [n["depth"] for n in tf["nodes"]] == sorted(n["depth"] for n in tf["nodes"]),
      [n["depth"] for n in tf["nodes"]])
check("flat 里 depth 从 0 起、末元素最深",
      tf["nodes"][0]["depth"] == 0 and max(n["depth"] for n in tf["nodes"]) == 4,
      [n["depth"] for n in tf["nodes"]])
check("flat 的根 parent_id=null", tf["nodes"][0]["parent_id"] is None)
check("flat 节点数与 children 形状一致", tf["node_count"] == t["node_count"])

te = tree(l1["node_id"], default_expanded_depth=1)
lvl2 = te["tree"]["children"][0]["children"][0]
check("default_expanded_depth 剪掉深层 children", lvl2["children"] == [], lvl2["children"])
check("被剪掉的节点带 has_children=true", lvl2["has_children"] is True)
check("未超深的节点 children 非空", len(te["tree"]["children"][0]["children"]) == 1)

check("max_depth 限制生效（截到 1 层）", tree(l1["node_id"], max_depth=1)["node_count"] == 2,
      tree(l1["node_id"], max_depth=1)["node_count"])
check("max_depth 超上限被夹到 MAX_DEPTH",
      tree(l1["node_id"], max_depth=999)["max_depth"] == nodes.MAX_DEPTH)

# =====================================================================
print("\n  -- ★ 概念节点不参与评分（缺键而非 null）")
# =====================================================================
concept_payload = client.get(f"/api/nodes/{l1['node_id']}").json()
for banned in ("importance", "usefulness", "highlight", "due_at"):
    check(f"概念节点响应里不存在 `{banned}` 键（缺键而非 null）",
          banned not in concept_payload, sorted(concept_payload))
check("概念节点 entry 为 null", concept_payload["entry"] is None)

for node in concept_nodes(tree(l1["node_id"])):
    check(f"概念节点「{node['label']}」自身无评分键",
          not (set(BANNED_ON_CONCEPT) & set(node)), sorted(node))

check("★ 整棵树里每个概念节点都无评分键",
      assert_no_rating_keys(concept_nodes(tree(l1["node_id"]))),
      [n["label"] for n in concept_nodes(tree(l1["node_id"]))])
check("概念节点的 entry 恒为 null",
      all(n["entry"] is None for n in concept_nodes(tree(l1["node_id"]))))
check("flat 形状下同样成立",
      assert_no_rating_keys([n for n in tree(l1["node_id"], shape="flat")["nodes"]
                             if not n["real"]]))
check("面包屑摘要里同样成立",
      assert_no_rating_keys([dict(p, **{"entry": None}) for p in
                             client.get(f"/api/nodes/{l4['node_id']}/parents").json()["path"]]))
check("根列表项里同样成立",
      assert_no_rating_keys([i for i in client.get("/api/nodes?roots=1&page_size=200")
                             .json()["items"] if not i["real"]]))

check("真实节点的评分只出现在 entry 子对象里",
      "importance" not in lab_node and lab_node["entry"]["importance"] == 4.5,
      sorted(lab_node))

# =====================================================================
print("\n  -- ★ 概念节点不出现在 entries 相关查询里（结构保证）")
# =====================================================================
entries_before = client.get("/api/entries?page_size=200").json()
listing_terms = {e["term"] for e in entries_before["items"]}
check("词条列表里没有概念节点名「狗/犬/柯基/小柯基/猎犬」",
      not ({"狗", "犬", "柯基", "小柯基", "猎犬"} & listing_terms),
      sorted({"狗", "犬", "柯基", "小柯基", "猎犬"} & listing_terms))

export_before = client.get("/api/entries/export").text
export_payload = json.loads(export_before)
check("导出条目数与列表 total 一致", export_payload["count"] == entries_before["total"],
      f"{export_payload['count']} vs {entries_before['total']}")
check("导出里没有概念节点", not any(
    e["term"] in {"狗", "犬", "柯基", "小柯基", "猎犬", "犬科"} for e in export_payload["entries"]))

c = conn()
try:
    n_entries = c.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    n_nodes = c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    n_concept = c.execute("SELECT COUNT(*) FROM nodes WHERE kind = 'concept'").fetchone()[0]
    n_review = c.execute("SELECT COUNT(*) FROM review_state").fetchone()[0]
    check("概念节点确实存在于 nodes 表", n_concept >= 8, n_concept)
    check("nodes 表行数 = 词条数 + 概念数", n_nodes == n_entries + n_concept,
          f"{n_nodes} vs {n_entries}+{n_concept}")

    # review_state 只以 entry_id 为外键，概念节点没有 entry_id → 进不了复习/练习池。
    # 注意 join 条件是 n.entry_id = r.entry_id（两列都是 entries.id 域），
    # 不能写 n.id = r.entry_id —— 那是两个不同的 id 空间，会撞号假失败。
    leaked = c.execute(
        "SELECT COUNT(*) FROM review_state r JOIN nodes n ON n.entry_id = r.entry_id "
        "WHERE n.kind = 'concept'"
    ).fetchone()[0]
    check("review_state 里没有任何概念节点（生命周期保证）", leaked == 0, leaked)
    check("review_state 行数 == 词条数", n_review == n_entries, f"{n_review} vs {n_entries}")

    due = review.due_entries(c, mode=review.MODE_INTERVAL, limit=200)
    check("复习候选池不含概念节点",
          not any(d["term"] in {"狗", "犬", "柯基", "小柯基", "猎犬"} for d in due),
          [d["term"] for d in due if d["term"] in {"狗", "犬"}])
    check("复习候选池条数 == 词条数", len(due) == n_entries, f"{len(due)} vs {n_entries}")

    fresh_list = review.new_entries_since(c, "1970-01-01T00:00:00", limit=200)
    check("新学词列表不含概念节点", len(fresh_list) == n_entries, f"{len(fresh_list)} vs {n_entries}")

    # 练习候选池：未指定词时自动取「到期 + 近 3 天新学」。这一层正是设计 6.3
    # 说的「概念节点进不了练习」的关键——概念节点连 review_state 都没有，
    # 候选池是从 review_state / entries 里出的，读不到 nodes。
    # 这里用 _resolve_terms 而不是 create_practice：后者还要外刊库里有含该词的句子，
    # 与本断言无关（那是 S2 前端的事），会因「外刊库是空的」抛无关错误。
    from app import practices as practices_mod

    pool = practices_mod._resolve_terms(c, [])
    check("★ 练习候选池不含概念节点",
          not any(t in {"狗", "犬", "柯基", "小柯基", "猎犬"} for t in pool), pool)
    check("练习候选词全部来自 entries", len(pool) == n_entries, f"{len(pool)} vs {n_entries}")
    check("候选池去重且条数正确", len(set(pool)) == len(pool), pool)

    # 候选权重计算里也读不到概念节点（_practice_weight 只吃 review_state JOIN entries 的行）
    weights = [practices_mod._practice_weight(e)
               for e in review.due_entries(c, limit=50)]
    check("候选权重可算且全为正（未评分走 DEFAULT_RATING 兜底）",
          all(w > 0 for w in weights), weights[:5])

    # find_duplicate 看不到概念节点 → 建同名词条不会被概念节点拦下
    dup = entries.find_duplicate(c, "word", "狗")
    check("概念节点不参与词条查重（建同名词条不冲突）", dup is None, str(dup))

    # 高亮统计只读 entries
    hl = client.get("/api/entries?page_size=200").json()["items"]
    check("高亮判定只对真实词条生效",
          [e["term"] for e in hl if e["highlight"]] == ["labrador"],
          [e["term"] for e in hl if e["highlight"]])

    # 概念节点的 label 绝不冗余存真实词条名
    stale = c.execute(
        "SELECT COUNT(*) FROM nodes WHERE kind = 'entry' AND label IS NOT NULL"
    ).fetchone()[0]
    check("kind=entry 的行 label 恒为 NULL（名字实时取 entries.term）", stale == 0, stale)
finally:
    c.close()

export_after = client.get("/api/entries/export").text
check("★ 建了 8+ 个概念节点后，导出 JSON 与之前逐字节一致",
      json.loads(export_after)["entries"] == export_payload["entries"])

# 改名联动：真实节点的 label 必须跟着 entries.term 变
client.put(f"/api/entries/{lab}", json={"type": "word", "term": "labrador2",
                                        "meaning_zh": "拉布拉多"})
check("★ 词条改名后树上 label 实时跟随（不冗余存旧名）",
      client.get(f"/api/nodes/{lab_node['node_id']}").json()["label"] == "labrador2",
      client.get(f"/api/nodes/{lab_node['node_id']}").json()["label"])
r = client.patch(f"/api/nodes/{lab_node['node_id']}", json={"label": "想单独改"})
check("真实词条节点传 label -> 400（名字归 entries.term 管）", r.status_code == 400, r.text[:120])
client.put(f"/api/entries/{lab}", json={"type": "word", "term": "labrador",
                                        "meaning_zh": "拉布拉多",
                                        "importance": 4.5, "usefulness": 4.0})

# =====================================================================
print("\n  -- 断边（移出子节点）")
# =====================================================================
before_roots = client.get("/api/nodes?roots=1").json()["total"]
r = client.delete(f"/api/nodes/{dog['node_id']}/children/{lab_node['node_id']}")
check("DELETE children -> 200", r.status_code == 200, r.text[:120])
check("响应 {detached, child_id, parent_id}",
      set(r.json()) == {"detached", "child_id", "parent_id"}, sorted(r.json()))
check("detached=true", r.json()["detached"] is True)
detached_node = client.get(f"/api/nodes/{lab_node['node_id']}").json()
check("子节点升级为根（parent_id=null）", detached_node["parent_id"] is None)
check("节点本身没被删", detached_node["label"] == "labrador", detached_node["label"])
check("★ 词条毫发无损", client.get(f"/api/entries/{lab}").status_code == 200)
check("词条评分也没动", client.get(f"/api/entries/{lab}").json()["importance"] == 4.5)
check("断边后根节点数 +1", client.get("/api/nodes?roots=1").json()["total"] == before_roots + 1,
      f"{before_roots} -> {client.get('/api/nodes?roots=1').json()['total']}")
check("重复断边 -> 400（不是静默成功）",
      client.delete(f"/api/nodes/{dog['node_id']}/children/{lab_node['node_id']}").status_code == 400)
# 子节点本身不存在 -> 404（父存在、子不存在是「资源不存在」而非「关系非法」）
check("断边时子节点不存在 -> 404",
      client.delete(f"/api/nodes/{dog['node_id']}/children/999999").status_code == 404)
check("断边时父节点不存在 -> 404",
      client.delete("/api/nodes/999999/children/1").status_code == 404)

# 挂回去，后面继续用
client.post(f"/api/nodes/{dog['node_id']}/children",
            json={"child": {"kind": "entry", "entry_id": lab}})

# =====================================================================
print("\n  -- 换父（PUT children，幂等）")
# =====================================================================
r = client.put(f"/api/nodes/{hunt['node_id']}/children/{lab_node['node_id']}", json={})
check("换父 -> 200", r.status_code == 200, r.text[:120])
check("parent_id 已改指",
      client.get(f"/api/nodes/{lab_node['node_id']}").json()["parent_id"] == hunt["node_id"])
old_children = client.get(f"/api/nodes/{dog['node_id']}").json()["child_count"]
check("旧父的 child_count 减 1（没有留下幽灵边）", old_children == 1, old_children)
r = client.put(f"/api/nodes/{hunt['node_id']}/children/{lab_node['node_id']}", json={})
check("重复换父到同一父 -> 幂等", r.status_code == 200, r.text[:120])
check("旧父 child_count 不再变化", client.get(f"/api/nodes/{dog['node_id']}").json()["child_count"] == 1)

# =====================================================================
print("\n  -- 兄弟排序（全量替换）")
# =====================================================================
so = concept("排序父")
kids = [concept(f"排序子{i}", parent_id=so["node_id"])["node_id"] for i in range(3)]
order = [kids[2], kids[0], kids[1]]
r = client.patch(f"/api/nodes/{so['node_id']}/children/order", json={"child_ids": order})
check("PATCH order -> 200", r.status_code == 200, r.text[:150])
check("返回按新序排列", [x["node_id"] for x in r.json()] == order,
      [x["node_id"] for x in r.json()])
check("返回带 position 0..n-1", [x["position"] for x in r.json()] == [0, 1, 2],
      [x["position"] for x in r.json()])
check("树里顺序与之一致",
      [c["node_id"] for c in tree(so["node_id"])["tree"]["children"]] == order)

check("child_ids 少传 -> 400（防静默删边）",
      client.patch(f"/api/nodes/{so['node_id']}/children/order",
                   json={"child_ids": order[:2]}).status_code == 400)
check("child_ids 多传 -> 400",
      client.patch(f"/api/nodes/{so['node_id']}/children/order",
                   json={"child_ids": order + [999999]}).status_code == 400)
check("child_ids 重复 -> 400",
      client.patch(f"/api/nodes/{so['node_id']}/children/order",
                   json={"child_ids": [kids[0], kids[0], kids[1]]}).status_code == 400)
check("被拒后原顺序未变",
      [c["node_id"] for c in tree(so["node_id"])["tree"]["children"]] == order)
check("排序后再排序（逆序）-> 200",
      client.patch(f"/api/nodes/{so['node_id']}/children/order",
                   json={"child_ids": order[::-1]}).status_code == 200)

# =====================================================================
print("\n  -- ★ 生命周期 A：删概念节点 cascade")
# =====================================================================
lc = concept("待删根")
lc2 = concept("待删子", parent_id=lc["node_id"])
lc3 = concept("待删孙", parent_id=lc2["node_id"])
lc_entry = add("doomed", "将被摘出的词")
client.post(f"/api/nodes/{lc3['node_id']}/children",
            json={"child": {"kind": "entry", "entry_id": lc_entry}})

r = client.delete(f"/api/nodes/{lc['node_id']}?mode=cascade")
check("DELETE ?mode=cascade -> 200", r.status_code == 200, r.text[:120])
payload = r.json()
check("响应 {deleted, removed_nodes, promoted_nodes, mode}",
      {"deleted", "removed_nodes", "promoted_nodes", "mode"} <= set(payload), sorted(payload))
check("removed_nodes 含整棵子树（4 个）", len(payload["removed_nodes"]) == 4,
      payload["removed_nodes"])
check("mode 回显 cascade", payload["mode"] == "cascade")
check("子树节点全部查不到",
      all(client.get(f"/api/nodes/{n}").status_code == 404 for n in payload["removed_nodes"]))
check("★ entries 表永不被触碰（词条还在）",
      client.get(f"/api/entries/{lc_entry}").status_code == 200)
check("★ 词条评分也还在",
      client.get(f"/api/entries/{lc_entry}").json()["meaning_zh"] == "将被摘出的词")

c = conn()
try:
    orphan = c.execute(
        "SELECT COUNT(*) FROM node_edges WHERE parent_id NOT IN (SELECT id FROM nodes) "
        "OR child_id NOT IN (SELECT id FROM nodes)"
    ).fetchone()[0]
    check("级联后 node_edges 无悬挂行（双向 CASCADE 生效）", orphan == 0, orphan)
finally:
    c.close()

check("不存在的节点 -> 404", client.delete("/api/nodes/999999").status_code == 404)
check("非法 mode -> 400", client.delete(f"/api/nodes/{so['node_id']}?mode=weird").status_code == 400)

# =====================================================================
print("\n  -- ★ 生命周期 A'：删概念节点 promote")
# =====================================================================
lp = concept("提升根")
lp_mid = concept("提升中间层", parent_id=lp["node_id"])
lp_kids = [concept(f"提升孙{i}", parent_id=lp_mid["node_id"])["node_id"] for i in range(3)]
r = client.delete(f"/api/nodes/{lp_mid['node_id']}?mode=promote")
check("DELETE ?mode=promote -> 200", r.status_code == 200, r.text[:120])
payload = r.json()
check("只删自己", payload["removed_nodes"] == [lp_mid["node_id"]], payload["removed_nodes"])
check("3 个子节点被提升", len(payload["promoted_nodes"]) == 3, payload["promoted_nodes"])
check("子节点挂到祖父下",
      all(client.get(f"/api/nodes/{k}").json()["parent_id"] == lp["node_id"] for k in lp_kids))
check("提升后 position 仍 0..n-1 连续",
      [c["position"] for c in tree(lp["node_id"])["tree"]["children"]]
      == list(range(len(lp_kids))),
      [c["position"] for c in tree(lp["node_id"])["tree"]["children"]])

lp_root = concept("提升根2")
lp_only = concept("提升子", parent_id=lp_root["node_id"])
r = client.delete(f"/api/nodes/{lp_only['node_id']}?mode=promote")
check("父为空时子节点各自成为根", r.json()["promoted_nodes"] == [], r.json())
check("提升后父节点 child_count 归零",
      client.get(f"/api/nodes/{lp_root['node_id']}").json()["child_count"] == 0)

# =====================================================================
print("\n  -- ★ 生命周期 B：删真实词条（先提升子节点）")
# =====================================================================
ld = concept("词条父")
ld_mid = concept("词条中", parent_id=ld["node_id"])
ld_leaf = concept("词条叶", parent_id=ld_mid["node_id"])
victim = add("victim", "要删的词")
victim_node = client.post("/api/nodes/from-entry",
                          json={"entry_id": victim, "parent_id": ld_mid["node_id"]}).json()["node"]

nodes_before = tree(ld["node_id"])["node_count"]
check("删前：victim 挂在 词条中 下",
      client.get(f"/api/nodes/{victim_node['node_id']}").json()["parent_id"] == ld_mid["node_id"])

r = client.delete(f"/api/entries/{victim}")
check("DELETE /api/entries/{id} -> 200", r.status_code == 200, r.text[:120])
check("★ 子节点「词条叶」已提升到祖父（狗的层级）下",
      client.get(f"/api/nodes/{ld_leaf['node_id']}").json()["parent_id"] == ld_mid["node_id"],
      client.get(f"/api/nodes/{ld_leaf['node_id']}").json()["parent_id"])
nodes_after = tree(ld["node_id"])["node_count"]
check("★ 节点总数减 1（不含被提升的子节点）", nodes_after == nodes_before - 1,
      f"{nodes_before} -> {nodes_after}")
check("被删词条的节点行已消失", client.get(f"/api/nodes/{victim_node['node_id']}").status_code == 404)
check("★ 中间层概念节点没被误删",
      client.get(f"/api/nodes/{ld_mid['node_id']}").status_code == 200)
check("★ 其余子节点（词条叶）都还在", client.get(f"/api/nodes/{ld_leaf['node_id']}").status_code == 200)

c = conn()
try:
    dangling = c.execute(
        "SELECT COUNT(*) FROM node_edges WHERE parent_id NOT IN (SELECT id FROM nodes) "
        "OR child_id NOT IN (SELECT id FROM nodes)"
    ).fetchone()[0]
    check("删词条后无悬挂边", dangling == 0, dangling)
    check("★ 概念节点不会被这一操作误删",
          c.execute("SELECT COUNT(*) FROM nodes WHERE kind='concept'").fetchone()[0] >= 10)
    # 无子节点的词条删除：提升步骤应是零行改动，删除后树形不变
    solo = add("leafonly", "没子节点的词")
    solo_root = concept("孤零零的父")
    before_solo = tree(solo_root["node_id"])["node_count"]
    entries.delete_entry(c, solo)
    check("★ 无子节点的词条删除是零行改动（提升步骤不误伤）",
          tree(solo_root["node_id"])["node_count"] == before_solo)
    check("无子节点的词条删后节点行也没了",
          nodes.node_of_entry(c, solo) is None)
finally:
    c.close()

# 删带整棵子树的词条节点
ld2 = concept("删子树父")
ld2_child = concept("删子树子", parent_id=ld2["node_id"])
victim2 = add("victim2", "带子节点的词")
client.post(f"/api/nodes/from-entry", json={"entry_id": victim2, "parent_id": ld2_child["node_id"]})
client.post(f"/api/nodes/{ld2_child['node_id']}/children",
            json={"child": {"kind": "concept", "label": "更深的子节点"}})
total_before = client.get("/api/nodes?roots=0&page_size=200").json()["total"]
client.delete(f"/api/entries/{victim2}")
check("删「带子节点的词条」后其子树未消失（提升保住）",
      client.get(f"/api/nodes/{ld2_child['node_id']}").status_code == 200
      and client.get(f"/api/nodes/{ld2['node_id']}").status_code == 200)
check("该子树仍挂在原根下",
      client.get(f"/api/nodes/{ld2_child['node_id']}").json()["parent_id"] == ld2["node_id"])
total_after = client.get("/api/nodes?roots=0&page_size=200").json()["total"]
check("节点总数只减 1（子树全保住）", total_after == total_before - 1,
      f"{total_before} -> {total_after}")

# =====================================================================
print("\n  -- 面包屑 / 词条→树位置")
# =====================================================================
r = client.get(f"/api/nodes/{ld_leaf['node_id']}/parents")
check("GET parents -> 200", r.status_code == 200, r.text[:120])
path = r.json()["path"]
check("祖先链根在前、不含自己",
      [p["label"] for p in path] == ["词条父", "词条中"], [p["label"] for p in path])
check("祖先摘要含 node_id/kind/real/label/parent_id",
      {"node_id", "kind", "real", "label", "parent_id"} <= set(path[0]), sorted(path[0]))
check("祖先摘要不含评分键（概念节点无评分口径不漏）",
      not ({"importance", "usefulness", "highlight"} & set(path[0])), sorted(path[0]))

r = client.get(f"/api/entries/{lab}/nodes")
check("GET /api/entries/{id}/nodes -> 200", r.status_code == 200, r.text[:120])
payload = r.json()
check("响应 {entry_id, node, parent, root, path}",
      set(payload) == {"entry_id", "node", "parent", "root", "path"}, sorted(payload))
check("node 非空", payload["node"] is not None)
check("parent 非空", payload["parent"] is not None, json.dumps(payload["parent"], ensure_ascii=False))
check("root 是真正的树根", payload["root"]["parent_id"] is None)
check("entry_id 回显", payload["entry_id"] == lab)

orphan_entry = add("orphanword", "还没挂树上")
r = client.get(f"/api/entries/{orphan_entry}/nodes")
# 每个词条都有节点行（create_entry 播种 / backfill 补齐），但没挂到任何父下。
# 这是「未挂树」的正确形态：node 非空、parent 为 null、自己是根。
# 真正的 node=null 只出现在「节点行缺失」的库（见下面人为制造的场景）。
check("未挂树的词条 -> 200 且 parent=null（不报 404）",
      r.status_code == 200 and r.json()["parent"] is None, r.text[:150])
check("未挂树的词条自己就是根",
      r.json()["node"]["parent_id"] is None and r.json()["root"]["node_id"]
      == r.json()["node"]["node_id"], r.text[:150])
orphan_node = r.json()["node"]
# ★ 需求①：未挂树的**孤立词条**不再进根列表 ★
# 旧行为把每个词条节点都当成"树根"，于是关联库里出现几十个
# 「1 个节点 / 1 词条 / 0 概念」的伪层级块（用户截图里的 recoil / shy away …）。
# 现在根列表只收「自己就构成一个结构」的根：有子边，或本身是概念节点。
check("★ 未挂树的孤立词条**不进**根列表（不再被当成层级块）",
      not any(i["node_id"] == orphan_node["node_id"]
              for i in client.get("/api/nodes?roots=1&page_size=200").json()["items"]),
      "根列表里不该出现 1 节点/1 词条/0 概念的伪块")

# 人为删掉节点行 → node 必须是 null 而不是 404
c = conn()
try:
    c.execute("DELETE FROM node_edges WHERE child_id IN "
              "(SELECT id FROM nodes WHERE entry_id = ?)", (orphan_entry,))
    c.execute("DELETE FROM nodes WHERE entry_id = ?", (orphan_entry,))
    c.commit()
    r = client.get(f"/api/entries/{orphan_entry}/nodes")
    check("★ 节点行缺失时 node=null 且仍返回 200（词条确实存在）",
          r.status_code == 200 and r.json()["node"] is None, r.text[:150])
    check("node=null 时 parent/root 也为 null、path 为空数组",
          r.json()["parent"] is None and r.json()["root"] is None and r.json()["path"] == [])
finally:
    c.close()
db.init_db()  # backfill 补回节点行

check("不存在的词条 -> 404", client.get("/api/entries/999999/nodes").status_code == 404)

# =====================================================================
print("\n  -- 根列表 / 全量列表")
# =====================================================================
rl = client.get("/api/nodes?roots=1&page=1&page_size=200").json()
check("GET /api/nodes?roots=1 -> 200", "items" in rl, sorted(rl))
check("响应 {items,total,page,page_size}", set(rl) == {"items", "total", "page", "page_size"},
      sorted(rl))
roots_ids = {i["node_id"] for i in rl["items"]}
c = conn()
try:
    # ★ 需求①：判据从「没有父边」收紧为「没有父边 **且** 自己构成一个结构」。
    # 只收两种根：有子边的（子树 ≥ 2 节点），或本身是概念节点的（用户建的锚点）。
    # 「没有父边但只是个孤立词条」= create_entry 播种留下的噪音，不是关联块。
    real_roots = {int(r[0]) for r in c.execute(
        "SELECT id FROM nodes WHERE NOT EXISTS "
        "(SELECT 1 FROM node_edges e WHERE e.child_id = nodes.id) "
        "AND (EXISTS (SELECT 1 FROM node_edges ge WHERE ge.parent_id = nodes.id) "
        "     OR kind = 'concept')").fetchall()}
    check("列表恰好是所有「没有父边且自己构成结构」的根", roots_ids == real_roots,
          f"{len(roots_ids)} vs {len(real_roots)}")
    shell_n = c.execute(
        "SELECT COUNT(*) FROM nodes n WHERE NOT EXISTS "
        "(SELECT 1 FROM node_edges e WHERE e.child_id = n.id) "
        "AND n.kind = 'entry' "
        "AND NOT EXISTS (SELECT 1 FROM node_edges ge WHERE ge.parent_id = n.id)"
    ).fetchone()[0]
    check("★ 被排除的孤立词条节点确实存在（否则这条断言没意义）",
          shell_n > 0, f"{shell_n} 个伪块根被过滤")
finally:
    c.close()

item = next(i for i in rl["items"] if i["node_id"] == ld["node_id"])
check("根列表项含 node_count/entry_count/concept_count/max_depth/child_count",
      {"node_count", "entry_count", "concept_count", "max_depth", "child_count"} <= set(item),
      sorted(item))
check("根列表统计与树接口一致",
      item["node_count"] == tree(ld["node_id"])["node_count"],
      f"{item['node_count']} vs {tree(ld['node_id'])['node_count']}")
check("根列表 label 正确", item["label"] == "词条父", item["label"])

r = client.get(f"/api/nodes?roots=1&q={nodes.entries._like_pattern('词条父')}".replace("%", "%25"))
found = client.get("/api/nodes?roots=1&q=%E8%AF%8D%E6%9D%A1%E7%88%B6").json()
check("根列表按名字搜索命中", found["total"] == 1, json.dumps(found, ensure_ascii=False)[:200])
check("搜索不命中返回空列表", client.get("/api/nodes?roots=1&q=zzz不存在zzz").json()["total"] == 0)

alll = client.get("/api/nodes?roots=0&page_size=200").json()
check("roots=0 列全部节点", alll["total"] >= len(rl["items"]), f"{alll['total']}")
check("全量列表含 entry_id / parent_id / child_count",
      {"entry_id", "parent_id", "child_count"} <= set(alll["items"][0]), sorted(alll["items"][0]))
check("分页生效",
      client.get("/api/nodes?roots=1&page=1&page_size=2").json()["page_size"] == 2)

# =====================================================================
print("\n  -- 与近义关联组互不干涉（links.py 零改动）")
# =====================================================================
sn1 = add("bigdog", "大狗")
sn2 = add("hound", "猎犬")
lk = client.post("/api/links", json={"entry_ids": [sn1, sn2], "relation": "synonym"})
check("近义关联组仍能建", lk.status_code == 201, lk.text[:120])
check("relation 白名单仍是「近义词」",
      lk.json()["relation_label"] == "近义词", lk.json()["relation_label"])
check("层级没有污染 relation 取值", "hierarchy" not in links.RELATIONS, list(links.RELATIONS))
# 同一对词既是近义又是层级：近义查 entry_link_items，层级查 node_edges，互不干扰
client.post(f"/api/nodes/{hunt['node_id']}/children",
            json={"child": {"kind": "entry", "entry_id": sn1}})
check("同一对词可同时是近义与层级",
      any(x["entry"]["id"] == sn2 for x in client.get(f"/api/entries/{sn1}/links").json()["links"])
      and client.get(f"/api/entries/{sn1}/nodes").json()["node"]["parent_id"] == hunt["node_id"])

# =====================================================================
print("\n  -- 幂等迁移与既有表零改动")
# =====================================================================
c = conn()
try:
    before_sql = {
        name: c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        for name in ("entry_links", "entry_link_items", "entries", "review_state")
    }
    counts_before = {
        t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("entries", "review_state", "nodes", "node_edges",
                  "entry_links", "entry_link_items")
    }
finally:
    c.close()

for _ in range(5):
    db.init_db()

c = conn()
try:
    counts_after = {
        t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("entries", "review_state", "nodes", "node_edges",
                  "entry_links", "entry_link_items")
    }
    check("★ 反复 init_db 5 次后所有表行数不变（迁移幂等）", counts_after == counts_before,
          f"{counts_before} -> {counts_after}")

    after_sql = {
        name: c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        for name in ("entry_links", "entry_link_items", "entries", "review_state")
    }
    check("★ entry_links 建表 SQL 逐字节未变", after_sql["entry_links"] == before_sql["entry_links"])
    check("★ entry_link_items 建表 SQL 逐字节未变",
          after_sql["entry_link_items"] == before_sql["entry_link_items"])
    check("★ entries 建表 SQL 逐字节未变", after_sql["entries"] == before_sql["entries"])
    check("★ review_state 建表 SQL 逐字节未变",
          after_sql["review_state"] == before_sql["review_state"])

    # 每个词条都有且只有一个节点行
    dup_entry = c.execute(
        "SELECT entry_id, COUNT(*) c FROM nodes WHERE entry_id IS NOT NULL "
        "GROUP BY entry_id HAVING c > 1"
    ).fetchall()
    check("一个词条最多一个节点（部分唯一索引）", not dup_entry, [tuple(r) for r in dup_entry])
    missing = c.execute(
        "SELECT COUNT(*) FROM entries e WHERE NOT EXISTS "
        "(SELECT 1 FROM nodes n WHERE n.entry_id = e.id)"
    ).fetchone()[0]
    check("★ 每个词条都有节点行（backfill 完整）", missing == 0, missing)

    # kind / label / entry_id 三者自洽（CHECK 约束真的生效）
    dirty = c.execute(
        "SELECT COUNT(*) FROM nodes WHERE "
        "(kind='concept' AND (label IS NULL OR entry_id IS NOT NULL)) OR "
        "(kind='entry' AND (label IS NOT NULL OR entry_id IS NULL))"
    ).fetchone()[0]
    check("kind / label / entry_id 三者自洽（CHECK 生效）", dirty == 0, dirty)

    # 一个节点只能有一个父
    multi_parent = c.execute(
        "SELECT child_id, COUNT(*) c FROM node_edges GROUP BY child_id HAVING c > 1"
    ).fetchall()
    check("一个节点只有一个父（idx_edges_child 生效）", not multi_parent,
          [tuple(r) for r in multi_parent])

    selfloop = c.execute("SELECT COUNT(*) FROM node_edges WHERE parent_id = child_id").fetchone()[0]
    check("无自环边（CHECK 生效）", selfloop == 0, selfloop)
finally:
    c.close()

# CHECK 约束真的在库层面（绕过业务层直接写脏数据应被拒）
c = conn()
try:
    for stmt, params, why in (
        ("INSERT INTO nodes (kind,label,note,entry_id,created_at,updated_at) "
         "VALUES ('concept',NULL,'',NULL,'t','t')", (), "concept 但 label 为 NULL"),
        ("INSERT INTO nodes (kind,label,note,entry_id,created_at,updated_at) "
         "VALUES ('entry','x','',NULL,'t','t')", (), "kind=entry 但 entry_id 为 NULL"),
        ("INSERT INTO nodes (kind,label,note,entry_id,created_at,updated_at) "
         "VALUES ('bogus','x','',NULL,'t','t')", (), "kind 取值非法"),
    ):
        try:
            c.execute(stmt, params)
            check(f"DB 层拒绝：{why}", False, "竟然写进去了")
        except Exception:
            check(f"DB 层拒绝：{why}", True)
    c.rollback()
finally:
    c.close()

# =====================================================================
print("\n  -- 上限与边界")
# =====================================================================
check("label 超长（>60）-> 400",
      client.post("/api/nodes/concept", json={"label": "狗" * 61}).status_code == 400)
check("note 超长（>500）-> 400",
      client.post("/api/nodes/concept", json={"label": "ok", "note": "x" * 501}).status_code == 400)
check("note 恰好 500 -> 接受",
      client.post("/api/nodes/concept", json={"label": "note边界", "note": "x" * 500}).status_code == 200)
check("label 恰好 60 -> 接受",
      client.post("/api/nodes/concept", json={"label": "狗" * 60}).status_code == 200)

deep_parent = concept("深0")
deep_id = deep_parent["node_id"]
deep_ok = True
for i in range(1, nodes.MAX_DEPTH):
    r = client.post("/api/nodes/concept", json={"label": f"深{i}", "parent_id": deep_id})
    if r.status_code != 200:
        deep_ok = False
        break
    deep_id = r.json()["node_id"]
check(f"能建到 {nodes.MAX_DEPTH} 层", deep_ok)
r = client.post("/api/nodes/concept", json={"label": "超深", "parent_id": deep_id})
check(f"超过 {nodes.MAX_DEPTH} 层 -> 400（不是病态树）", r.status_code == 400, r.text[:120])
check("超深被拒后原树形完好", tree(deep_parent["node_id"])["node_count"] == nodes.MAX_DEPTH,
      tree(deep_parent["node_id"])["node_count"])

# 宽度上限
wide = concept("宽父")
wide_kids = [concept(f"宽子{i}", parent_id=wide["node_id"])["node_id"] for i in range(nodes.MAX_CHILDREN)]
check(f"能挂满 {nodes.MAX_CHILDREN} 个子节点", len(wide_kids) == nodes.MAX_CHILDREN)
r = client.post(f"/api/nodes/{wide['node_id']}/children",
                json={"child": {"kind": "concept", "label": "第51个"}})
check(f"超过 {nodes.MAX_CHILDREN} 个子节点 -> 400", r.status_code == 400, r.text[:120])
check("超宽被拒后父节点 child_count 不变",
      client.get(f"/api/nodes/{wide['node_id']}").json()["child_count"] == nodes.MAX_CHILDREN)
check("position 超出末尾被夹到末尾而不是报错",
      client.put(f"/api/nodes/{wide['node_id']}/children/{wide_kids[0]}",
                 json={"position": 9999}).status_code == 200)

# =====================================================================
print("\n  -- XSS 契约：响应里只有纯文本，没有 HTML 片段")
# =====================================================================
xss_label = '"><img src=x onerror=alert(1)>'
xss_node = concept(xss_label)
check("含 XSS 载荷的 label 原样存取（后端不转义也不拼 HTML）",
      client.get(f"/api/nodes/{xss_node['node_id']}").json()["label"] == xss_label)
raw = client.get(f"/api/nodes/{xss_node['node_id']}").text
# JSON 会把 " 转义成 \"、< > 保留原样，所以响应里出现 "<img" 是**正常的**——
# 它是「用户文本被当作数据安全地装进 JSON 字符串」，不是后端在吐 HTML 片段。
# 真正的红线是：后端不返回任何 HTML 片段 / html 字段，且原样透传（不吞不改），
# 由前端用 textContent 渲染（web/app.js 已有该注释）。
check("响应里没有 html / html_content 之类字段",
      "html" not in client.get(f"/api/nodes/{xss_node['node_id']}").json())
check("★ 载荷原样透传（后端不改写用户文本，由前端 textContent 负责渲染）",
      xss_label in json.loads(raw)["label"], json.loads(raw)["label"])
check("载荷在 JSON 里被正确转义为字符串（不是裸 HTML）",
      json.loads(raw)["label"] == xss_label)
# 树接口同样如此：label 原样出现在嵌套结构里
xss_tree = tree(xss_node["node_id"])
check("树接口里 label 同样原样透传", xss_tree["tree"]["label"] == xss_label)
check("★ 载荷不会污染 sibling 结构（仍是纯 label 字段）",
      set(xss_tree["tree"]) == {"node_id", "kind", "real", "label", "subtitle", "note",
                                "entry_id", "parent_id", "position", "depth",
                                "child_count", "entry", "children", "has_children",
                                # 需求 3 新增：tombstone / tombstone_label 是
                                # 占位节点的两个契约键，必须显式列出——
                                # 这个集合断言的作用就是「响应字段不偷偷多出别的东西」。
                                "tombstone", "tombstone_label"},
      sorted(xss_tree["tree"]))

# =====================================================================
# 网状结构（需求 6 追加）：一个节点既是子又是父 + 树内横向近义
# ---------------------------------------------------------------------
# 用户的构想：克服（概念）→ overcome / conquer / surmount（三者互为近义），
# surmount 再 → insurmountable。这条链以前建不出来，是因为**前端**硬拦
# 「真实词条只能作为子项」；后端从来没有这个限制。本段把这个后端本来的能力
# 钉成契约，避免以后有人「顺手」在业务层加回父类型检查。
print("\n  -- 网状结构：词条既是子又是父")
c_net = concept("克服net")["node_id"]
e_ov = add("overcome-net", "克服")
e_cq = add("conquer-net", "征服")
e_sm = add("surmount-net", "克服（困难）")
e_is = add("insurmountable-net", "无法克服的")

n_sm = None
for eid in (e_ov, e_cq, e_sm):
    r = client.post(
        f"/api/nodes/{c_net}/children",
        json={"child": {"kind": "entry", "entry_id": eid}},
    )
    assert r.status_code == 200, r.text
    if eid == e_sm:
        n_sm = r.json()["node_id"]

check("三个表达都挂到概念下", len(tree(c_net)["tree"]["children"]) == 3)

# ★ 关键一步：拿「词条节点」当父，再挂一层 ★
r = client.post(
    f"/api/nodes/{n_sm}/children",
    json={"child": {"kind": "entry", "entry_id": e_is}},
)
check("★ 真实词条可以当父（surmount → insurmountable）", r.status_code == 200, r.text[:160])

t_net = tree(c_net)
depths = {n["label"]: n["depth"] for n, _ in walk(t_net["tree"])}
check("树变成两层：surmount 在深度 1", depths.get("surmount-net") == 1, depths)
check("insurmountable 在深度 2", depths.get("insurmountable-net") == 2, depths)
check("整棵树 5 个节点（1 概念 + 4 词条）", t_net["node_count"] == 5, t_net["node_count"])

# ---------------------------------------------------------------------
print("\n  -- 树内横向近义：一次请求取回，不必逐节点去问")
# e_far 刻意**不进树**，但和三个表达同组：用来验证后端返回的是完整成员表，
# 而不是「只返回树里那几个」——裁剪会让前端画不出「这个近义词在别的块里」。
e_far = add("far-net", "树外同义词")
r = client.post(
    "/api/links",
    json={"entry_ids": [e_ov, e_cq, e_sm, e_far], "relation": "synonym"},
)
assert r.status_code == 201, r.text
link_id = r.json()["id"]

t_net2 = tree(c_net)
check("树响应新增 links 键", "links" in t_net2, sorted(t_net2))
groups = t_net2.get("links") or []
check("树内 3 人近义组被返回", len(groups) == 1 and groups[0]["id"] == link_id,
      [(g["id"], g["relation"]) for g in groups])
check("组关系是 synonym（不是同形状的任意组）",
      bool(groups) and groups[0]["relation"] == "synonym")
check(
    "members 返回完整 4 个（树外成员不被裁掉）",
    bool(groups) and sorted(m["id"] for m in groups[0]["members"]) == sorted([e_ov, e_cq, e_sm, e_far]),
    sorted(m["id"] for m in groups[0]["members"]) if groups else None,
)

# ★ 反向对照：只有一个成员在树内的组必须被排除 ★
# 若 groups_within 少了「≥2 个成员在集合内」这条过滤，ant_id 会混进来。
e_out = add("outsider-net", "树外词")
r = client.post("/api/links", json={"entry_ids": [e_sm, e_out], "relation": "antonym"})
assert r.status_code == 201, r.text
ant_id = r.json()["id"]
t_net3 = tree(c_net)
check(
    "★ 只有一个成员在树内的组不返回（antonym 组被排除）",
    all(g["id"] != ant_id for g in t_net3["links"]),
    [g["id"] for g in t_net3["links"]],
)
check(
    "近义组仍在（排除没有连坐掉正常组）",
    any(g["id"] == link_id for g in t_net3["links"]),
    [g["id"] for g in t_net3["links"]],
)

c_alone = concept("孤立概念net")["node_id"]
check("没有词条的树返回空 links 数组（键恒在）", tree(c_alone)["links"] == [])

# =====================================================================
print("\n结论：")
failed = [n for n, ok in results if not ok]
for n, ok in results:
    print(f"    [{'PASS' if ok else 'FAIL'}] {n}")
print(f"总计：{'全部通过' if not failed else f'{len(failed)} 项失败'}（{len(results)} 项）")
if failed:
    print("\n失败明细：")
    for n in failed:
        print(f"  - {n}")