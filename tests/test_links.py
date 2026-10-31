"""关联库 + 评分接口测试：双向一致性、级联清理、评分边界。

运行：python test_links.py
说明：用 ENPRACTICE_HOME 指向临时目录，不触碰真实数据。
"""

import os
import tempfile

TMP = tempfile.mkdtemp(prefix="enpractice_links_")
os.environ["ENPRACTICE_HOME"] = TMP

from fastapi.testclient import TestClient  # noqa: E402

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, entries, links  # noqa: E402
from app.main import app  # noqa: E402

db.init_db()
client = TestClient(app)

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def add(term, meaning="释义"):
    r = client.post("/api/entries", json={"type": "word", "term": term, "meaning_zh": meaning})
    assert r.status_code == 201, r.text
    return r.json()["id"]


print("  -- 建词条")
a = add("abundant", "丰富的")
b = add("ample", "充足的")
c = add("plentiful", "大量的")
check("三个词条建好", len({a, b, c}) == 3, f"{a},{b},{c}")

print("  -- 创建关联组")
r = client.post("/api/links", json={"entry_ids": [a, b], "relation": "synonym", "note": "近义"})
check("POST /api/links -> 201", r.status_code == 201, r.text)
link1 = r.json()
check("成员数 2", len(link1["members"]) == 2, str(link1["members"]))
check("relation_label 中文", link1["relation_label"] == "近义词", link1["relation_label"])
check("响应带 updated_at", bool(link1.get("updated_at")), str(link1.get("updated_at")))

print("  -- 双向一致性（自连接，不存反向行）")
la = client.get(f"/api/entries/{a}/links").json()["links"]
lb = client.get(f"/api/entries/{b}/links").json()["links"]
check("A 能看到 B", any(x["entry"]["id"] == b for x in la), str(la))
check("B 能看到 A", any(x["entry"]["id"] == a for x in lb), str(lb))
check("A 看不到自己", not any(x["entry"]["id"] == a for x in la), str(la))

print("  -- 一对多 / 多成员")
r3 = client.post("/api/links", json={"entry_ids": [a, b, c]})
check("三成员组 -> 201", r3.status_code == 201, r3.text)
lc = client.get(f"/api/entries/{c}/links").json()["links"]
check("C 能同时看到 A 和 B",
      {x["entry"]["id"] for x in lc} >= {a, b}, str(lc))

print("  -- 创建时的非法输入")
check("只有 1 个成员被拒",
      client.post("/api/links", json={"entry_ids": [a]}).status_code in (400, 422))
check("重复成员去重后只 1 个 -> 被拒",
      client.post("/api/links", json={"entry_ids": [a, a]}).status_code == 400)
check("成员不存在 -> 404",
      client.post("/api/links", json={"entry_ids": [a, 999999]}).status_code == 404)
check("未知关系类型 -> 400",
      client.post("/api/links", json={"entry_ids": [a, b], "relation": "whatever"}).status_code == 400)
check("成员超上限 -> 400",
      client.post("/api/links", json={"entry_ids": list(range(1, 40))}).status_code == 400)
check("不存在的组 -> 404", client.get("/api/links/999999").status_code == 404)

print("  -- 移除成员与连带解散")
before = len(client.get("/api/links").json()["items"])
rm = client.post(f"/api/links/{r3.json()['id']}/remove", json={"entry_id": c})
check("三成员组移除一人 -> 不解散", rm.json()["dissolved"] is False, rm.text)
rm2 = client.post(f"/api/links/{r3.json()['id']}/remove", json={"entry_id": b})
check("剩 1 人 -> 整组连带解散", rm2.json()["dissolved"] is True, rm2.text)
after = len(client.get("/api/links").json()["items"])
check("解散后组数减少", after == before - 1, f"{before} -> {after}")
check("组已查不到", client.get(f"/api/links/{r3.json()['id']}").status_code == 404)

print("  -- 解散整组")
d = client.delete(f"/api/links/{link1['id']}")
check("DELETE 成功", d.status_code == 200, d.text)
check("解散后 A 无关联", client.get(f"/api/entries/{a}/links").json()["links"] == [])

print("  -- 删词条的级联清理")
r4 = client.post("/api/links", json={"entry_ids": [a, b]})
check("重建一组", r4.status_code == 201, r4.text)
n_before = len(client.get("/api/links").json()["items"])
client.delete(f"/api/entries/{a}")
check("删词条后退化组被清理",
      len(client.get("/api/links").json()["items"]) == n_before - 1)
check("幸存词条无残留关联", client.get(f"/api/entries/{b}/links").json()["links"] == [])

print("  -- 评分接口 PATCH /api/entries/{id}/ratings")
t = add("graded", "待打分")
for value, ok in ((0, True), (0.5, True), (5, True), (4.5, True), (5.5, False),
                  (-1, False), (0.3, False)):
    r = client.patch(f"/api/entries/{t}/ratings", json={"importance": value})
    good = r.status_code == 200
    check(f"importance={value} -> {'接受' if ok else '拒绝'}", good == ok,
          f"{r.status_code} {r.text[:80]}")

check("半星能存回 4.5",
      client.patch(f"/api/entries/{t}/ratings", json={"importance": 4.5}).json()["importance"] == 4.5)
check("只改实用性不动重要性",
      client.patch(f"/api/entries/{t}/ratings", json={"usefulness": 2.0}).json()["importance"] == 4.5)
check("传 null 清除评分",
      client.patch(f"/api/entries/{t}/ratings", json={"usefulness": None}).json()["usefulness"] is None)
check("空 body 不改任何值",
      client.patch(f"/api/entries/{t}/ratings", json={}).json()["importance"] == 4.5)

print("  -- 高亮判定：任一维达到 4.0 即高亮")
# 契约：收条目字典（前端拿到的就是整个 entry 对象）
check("仅重要性高 -> 高亮", entries.is_highlight({"importance": 4.5, "usefulness": None}) is True)
check("仅实用性高 -> 高亮", entries.is_highlight({"importance": None, "usefulness": 4.0}) is True)
check("两维都低 -> 不高亮", entries.is_highlight({"importance": 3.0, "usefulness": 3.5}) is False)
check("两维都未评分 -> 不高亮", entries.is_highlight({}) is False)
check("一维高一维未评 -> 高亮", entries.is_highlight({"importance": 5.0}) is True)
h = add("highlighted", "高分")
client.patch(f"/api/entries/{h}/ratings", json={"importance": 4.5})
check("接口输出 highlight=True",
      client.get(f"/api/entries/{h}").json()["highlight"] is True)

print("  -- 排序白名单不含评分列（需求：不用于排序）")
check("importance 不在排序白名单", "importance" not in entries.SORT_COLUMNS)
check("usefulness 不在排序白名单", "usefulness" not in entries.SORT_COLUMNS)

# ---------------------------------------------------------------------------
# PUT 局部更新：None = 不改动。这一段专治「只改一个字段把其它字段抹掉」——
# 本项目已在 extra_info / 标签上踩过两次同样的坑，这里是第三处同类语义。
# ---------------------------------------------------------------------------
print("\n  -- PUT /api/links/{id}：None = 不改动")

a1 = add("put-a", "甲")
a2 = add("put-b", "乙")
a3 = add("put-c", "丙")
grp = client.post(
    "/api/links", json={"entry_ids": [a1, a2], "relation": "synonym", "note": "原始备注"}
).json()
gid = grp["id"]

r = client.put(f"/api/links/{gid}", json={"relation": "synonym", "note": "改过的备注"})
check("只改备注 -> 200", r.status_code == 200, r.text[:120])
after_note = r.json()
check("备注已更新", after_note["note"] == "改过的备注", after_note["note"])
check(
    "只改备注时成员保持不变（不能被抹掉）",
    sorted(m["id"] for m in after_note["members"]) == sorted([a1, a2]),
    [m["id"] for m in after_note["members"]],
)

r = client.put(f"/api/links/{gid}", json={"entry_ids": [a1, a2, a3]})
check("只改成员 -> 200", r.status_code == 200, r.text[:120])
after_members = r.json()
check(
    "成员已替换成 3 个",
    sorted(m["id"] for m in after_members["members"]) == sorted([a1, a2, a3]),
    [m["id"] for m in after_members["members"]],
)
check(
    "只改成员时备注保持不变（不能被抹掉）",
    after_members["note"] == "改过的备注",
    after_members["note"],
)

r = client.put(f"/api/links/{gid}", json={"entry_ids": [a1]})
check("替换成不足 2 人 -> 400（而不是留下空壳组）", r.status_code == 400, r.text[:120])
still3 = client.get(f"/api/links/{gid}").json()
check(
    "被拒后原成员未被删光（校验必须在动手之前）",
    sorted(m["id"] for m in still3["members"]) == sorted([a1, a2, a3]),
    [m["id"] for m in still3["members"]],
)

r = client.put(f"/api/links/{gid}", json={"entry_ids": [a1, 999999]})
check("替换成含不存在成员的集合 -> 不改动", r.status_code in (400, 404), r.text[:120])

r = client.put(f"/api/links/{gid}", json={})
check("空 body -> 200 且不改动任何字段", r.status_code == 200, r.text[:120])
noop = r.json()
check(
    "空 body 后备注与成员都原样",
    noop["note"] == "改过的备注"
    and sorted(m["id"] for m in noop["members"]) == sorted([a1, a2, a3]),
    f"{noop['note']} / {[m['id'] for m in noop['members']]}",
)

# ★ 本次改动（关联库画布化）修的断言 ★
# 这两条原本拿 "antonym" 当「未知关系类型」的样本。现在 antonym 已经是
# 白名单里的合法关系（画布上的红色 OPP 线走的就是它），所以样本换成
# 真正不在白名单里的 "whatever"。断言的**意图不变**：非白名单关系必须被拒，
# 且被拒后不能改动原有关系。同一文件 line 70 那条一直用的就是 "whatever"。
r = client.put(f"/api/links/{gid}", json={"relation": "whatever"})
check("未知关系类型 -> 400", r.status_code == 400, r.text[:120])
check("未知关系被拒后原关系不变", client.get(f"/api/links/{gid}").json()["relation"] == "synonym")

# 新增：antonym 现在是合法关系，能建组也能改
r_ant = client.put(f"/api/links/{gid}", json={"relation": "antonym"})
check("反义词是合法关系（画布 OPP 线依赖它）",
      r_ant.status_code == 200 and r_ant.json()["relation"] == "antonym",
      r_ant.text[:120])
check("反义词组的中文标签是「反义词」",
      r_ant.status_code == 200 and r_ant.json()["relation_label"] == "反义词",
      r_ant.status_code == 200 and r_ant.json().get("relation_label", ""))
# 改回近义词，免得影响后面用例的初始状态
client.put(f"/api/links/{gid}", json={"relation": "synonym"})

# 移除不在组内的成员：不能误伤，更不能误解散整组
before = client.get("/api/links").json()["total"]
r = client.post(f"/api/links/{gid}/remove", json={"entry_id": 999999})
check("移出非成员 -> 400（不是静默成功）", r.status_code == 400, r.text[:120])
check(
    "移出非成员后整组还在（不能误解散）",
    client.get("/api/links").json()["total"] == before,
    f"{client.get('/api/links').json()['total']} vs {before}",
)

# GET /api/entries/{id}/links 的响应结构
r = client.get(f"/api/entries/{a1}/links")
check("GET 条目关联 -> 200", r.status_code == 200, r.text[:120])
payload = r.json()
check("响应带 entry_id", payload.get("entry_id") == a1, payload.get("entry_id"))
check("响应带 links 数组", isinstance(payload.get("links"), list), type(payload.get("links")))
first = payload["links"][0] if payload["links"] else {}
check(
    "每个元素含 link_id / relation / relation_label / entry",
    {"link_id", "relation", "relation_label", "entry"} <= set(first),
    sorted(first),
)
check(
    "entry 含 id / term / meaning_zh / type",
    {"id", "term", "meaning_zh", "type"} <= set(first.get("entry", {})),
    sorted(first.get("entry", {})),
)
check(
    "三成员组里查 A 得到 2 个同伴",
    len(payload["links"]) == 2,
    [l["entry"]["term"] for l in payload["links"]],
)
check(
    "同伴里不含自己",
    a1 not in [l["entry"]["id"] for l in payload["links"]],
    [l["entry"]["id"] for l in payload["links"]],
)
r404 = client.get("/api/entries/999999/links")
check("不存在的条目查关联 -> 404", r404.status_code == 404, r404.text[:120])

print("\n结论：")
failed = [n for n, ok in results if not ok]
for n, ok in results:
    print(f"    [{'PASS' if ok else 'FAIL'}] {n}")
print(f"总计：{'全部通过' if not failed else f'{len(failed)} 项失败'}（{len(results)} 项）")
