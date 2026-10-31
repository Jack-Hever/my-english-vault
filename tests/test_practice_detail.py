"""练习记录详情接口 + 练习历史分页上限测试。

运行：python test_practice_detail.py
说明：用 ENPRACTICE_HOME 指向临时目录，不触碰真实数据。

覆盖三件本轮改动的事：
  A. GET /api/practices/{id} 返回**完整**字段（原句 / 参考译文 / 用户的回译 /
     ai_feedback 原文 / 四项分数），而不是列表里的摘要。
  B. 记录的 id 语义是 practices 行 id（= 列表 items[].id），不是 passage id；
     passage 被删后（ON DELETE SET NULL）详情仍能打开，只少原句与译文。
  C. 练习历史列表的 page_size 有上限（100），超限 400、下限 400，
     且底层 list_practices() 被直接调用时也会自己夹住。
"""

import json
import os
import tempfile

TMP = tempfile.mkdtemp(prefix="enpractice_detail_")
os.environ["ENPRACTICE_HOME"] = TMP
os.environ.pop("OPENAI_API_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, practices  # noqa: E402
from app.main import app  # noqa: E402

db.init_db()
client = TestClient(app)
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


FEEDBACK = {
    "grammar": {
        "score": 22, "max": 30, "label": "语法正确",
        "issues": ["主谓不一致：abundant evidence 用了 surprise", "冠词缺失"],
        "comment": "时态有问题",
    },
    "fluency": {
        "score": 26, "max": 30, "label": "表达地道",
        "issues": ["表达略生硬", "可用更地道的搭配"],
        "comment": "整体能读懂",
    },
    "fidelity": {
        "score": 38, "max": 40, "label": "语义保真",
        "issues": [],
        "comment": "语义基本对齐，仅漏译后半句",
    },
    "total": 86,
    "suggestion": "The abundant evidence surprised nobody.",
    "is_mock": False,
}


def seed():
    """直接写库造一条干净的练习记录（绕开 AI 调用）。"""
    conn = db.connect()
    cur = conn.execute(
        """INSERT INTO passages (title, source_url, origin, text_en, text_zh,
                                 focus_entry_ids, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            "翻译练习素材", None, "library",
            "The abundant evidence in the report surprised nobody.",
            "报告中大量的证据并未让任何人感到惊讶。",
            "[]", "2026-01-01T09:00:00",
        ),
    )
    passage_id = cur.lastrowid
    cur = conn.execute(
        """INSERT INTO practices
           (passage_id, user_text, ai_feedback, score_grammar, score_fluency,
            score_fidelity, score_total, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            passage_id,
            "The abundant evidences in report surprised nobody.",
            json.dumps(FEEDBACK, ensure_ascii=False),
            22, 26, 38, 86, "2026-01-01T09:30:00",
        ),
    )
    record_id = cur.lastrowid
    conn.commit()
    conn.close()
    return passage_id, record_id


print("[1] 造数据")
passage_id, record_id = seed()
check("练习记录已入库", bool(record_id))

print("\n[2] 详情接口返回完整字段")
r = client.get(f"/api/practices/{record_id}")
check("详情 -> 200", r.status_code == 200, r.text[:200] if r.status_code != 200 else "")
if r.status_code == 200:
    d = r.json()
    for field in ("id", "created_at", "text_en", "text_zh", "user_text",
                  "ai_feedback", "score_grammar", "score_fluency",
                  "score_fidelity", "score_total"):
        check(f"详情含字段 {field}", field in d, str(list(d.keys())))
    check("id 是 practices 行 id（与列表一致）", d["id"] == record_id, f"{d.get('id')} vs {record_id}")
    check("原句英文正确", d.get("text_en") == "The abundant evidence in the report surprised nobody.",
          repr(d.get("text_en"))[:80])
    check("参考中文译文正确", d.get("text_zh") == "报告中大量的证据并未让任何人感到惊讶。",
          repr(d.get("text_zh"))[:40])
    check("用户当时的回译正确", "abundant evidences" in (d.get("user_text") or ""),
          repr(d.get("user_text"))[:80])
    check("四项分数齐备",
          (d.get("score_grammar"), d.get("score_fluency"),
           d.get("score_fidelity"), d.get("score_total")) == (22, 26, 38, 86),
          str((d.get("score_grammar"), d.get("score_fluency"),
               d.get("score_fidelity"), d.get("score_total"))))
    # ai_feedback 必须是**模型原话原文**：后端不解析、不改写，
    # 解析与按维度展开 issues 是前端的事（前端要逐条渲染且必须防 XSS）。
    fb_raw = d.get("ai_feedback") or ""
    check("ai_feedback 是 JSON 字符串", isinstance(fb_raw, str) and fb_raw.strip() != "")
    try:
        fb = json.loads(fb_raw)
        parsed = True
    except (TypeError, ValueError):
        fb, parsed = {}, False
    check("ai_feedback 可被前端解析成对象", parsed, fb_raw[:80])
    if parsed:
        check("三个维度都在", all(k in fb for k in ("grammar", "fluency", "fidelity")))
        check("grammar 有 2 条 issues", len(fb["grammar"]["issues"]) == 2,
              str(fb["grammar"]["issues"]))
        check("fidelity 无 issues 但有 comment",
              fb["fidelity"]["issues"] == [] and bool(fb["fidelity"]["comment"]))
        check("带 suggestion", fb.get("suggestion") == FEEDBACK["suggestion"])

print("\n[3] 详情 id 的语义：不是 passage id，且 passage 被删也能打开")
r = client.get(f"/api/practices/{passage_id}")
check("拿 passage id 当记录 id 查得到别的记录（id 确实不是 passage id）",
      r.status_code == 200 and r.json()["id"] == record_id,
      f"status={r.status_code} id={r.json().get('id') if r.status_code == 200 else None}")

# 删掉 passage：practices.passage_id 是 ON DELETE SET NULL，
# 详情必须还能打开（用户回译与评分是用户自己的成果，不该跟着素材一起消失）。
conn = db.connect()
conn.execute("DELETE FROM passages WHERE id = ?", (passage_id,))
conn.commit()
conn.close()
r = client.get(f"/api/practices/{record_id}")
check("素材删除后详情仍可打开", r.status_code == 200, str(r.status_code))
if r.status_code == 200:
    d = r.json()
    check("回译与评分仍在", bool(d.get("user_text")) and d.get("score_total") == 86)
    check("原句变成 None（前端要能兜底显示）", d.get("text_en") is None, repr(d.get("text_en")))

print("\n[4] 详情不存在时 404")
r = client.get("/api/practices/999999")
check("不存在的 id -> 404", r.status_code == 404, str(r.status_code))
check("404 提示里有 id", "999999" in r.json().get("detail", ""), r.text[:120])

print("\n[5] 脏数据：ai_feedback 不是合法 JSON 也要能返回")
conn = db.connect()
cur = conn.execute(
    """INSERT INTO practices (passage_id, user_text, ai_feedback, created_at)
       VALUES (NULL, ?, ?, ?)""",
    ("坏数据回译", "{不是合法 JSON", "2026-01-02T09:00:00"),
)
bad_id = cur.lastrowid
conn.commit()
conn.close()
r = client.get(f"/api/practices/{bad_id}")
check("脏 ai_feedback 的记录仍返回 200", r.status_code == 200, str(r.status_code))
if r.status_code == 200:
    check("ai_feedback 原样透传（后端不吞不改）",
          r.json().get("ai_feedback") == "{不是合法 JSON", repr(r.json().get("ai_feedback")))
    check("空 passage 时 text_zh 为 None", r.json().get("text_zh") is None)

r = client.get(f"/api/practices/{bad_id}")
check("重复请求结果一致（详情接口无副作用）", r.status_code == 200 and r.json()["id"] == bad_id)

print("\n[6] 分页上限：page_size 超过 100 直接 400")
check("练习单页上限常量为 100", practices.MAX_PAGE_SIZE == 100, str(practices.MAX_PAGE_SIZE))
r = client.get("/api/practices?page=1&page_size=101")
check("page_size=101 -> 400", r.status_code == 400, str(r.status_code))
check("400 提示说明了允许范围", "100" in r.json().get("detail", ""), r.text[:160])

r = client.get("/api/practices?page=1&page_size=99999")
check("page_size=99999 -> 400（挡下整表拉取）", r.status_code == 400, str(r.status_code))

r = client.get("/api/practices?page=1&page_size=0")
check("page_size=0 -> 400", r.status_code == 400, str(r.status_code))
r = client.get("/api/practices?page=0&page_size=10")
check("page=0 -> 400", r.status_code == 400, str(r.status_code))

r = client.get("/api/practices?page=1&page_size=100")
check("page_size=100（等于上限）-> 200", r.status_code == 200, str(r.status_code))
check("返回体回显归一后的 page_size", r.json().get("page_size") == 100,
      str(r.json().get("page_size")))

r = client.get("/api/practices?page=1&page_size=25")
check("前端实际使用的 page_size=25 -> 200", r.status_code == 200, str(r.status_code))
check("列表带出摘要字段（不含 ai_feedback）",
      "ai_feedback" not in (r.json()["items"][0] if r.json()["items"] else {}),
      str(list(r.json()["items"][0].keys())) if r.json()["items"] else "no items")
check("列表项的 id 与详情接口同源（可直接拿去查详情）",
      client.get(f"/api/practices/{r.json()['items'][0]['id']}").status_code == 200)

print("\n[7] 底层函数自夹：直接调 list_practices 也挡得住超大 page_size")
conn = db.connect()
direct = practices.list_practices(conn, page=1, page_size=99999)
conn.close()
check("list_practices 直接调用时夹到 100", direct["page_size"] == 100, str(direct["page_size"]))
conn = db.connect()
direct0 = practices.list_practices(conn, page=1, page_size=0)
conn.close()
check("page_size=0 被夹到 1（不返回空列表）", direct0["page_size"] == 1, str(direct0["page_size"]))
conn = db.connect()
directneg = practices.list_practices(conn, page=-3, page_size=10)
conn.close()
check("page=-3 被归一到 1", directneg["page"] == 1, str(directneg["page"]))

print("\n[8] 其他模块的分页上限没被牵连")
r = client.get("/api/entries?page=1&page_size=150")
check("词条 page_size=150 仍可用（上限 200）", r.status_code == 200, str(r.status_code))

print("\n结论：")
failed = [n for n, ok in results if not ok]
for n, ok in results:
    print(f"    [{'PASS' if ok else 'FAIL'}] {n}")
print(f"总计：{'全部通过' if not failed else f'{len(failed)} 项失败'}（{len(results)} 项）")