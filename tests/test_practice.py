"""P3 翻译练习测试：选词→选句→AI译文→回译点评→评分→记录，以及复习调度。

运行：python test_practice.py
说明：用 ENPRACTICE_HOME 指向临时目录，不触碰真实数据。
      未配置 AI 密钥，全流程走 mock 模式（这也正是要验证的降级路径）。
"""

import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

TMP = tempfile.mkdtemp(prefix="enpractice_p3_")
os.environ["ENPRACTICE_HOME"] = TMP
os.environ.pop("OPENAI_API_KEY", None)  # 确保 mock 模式

from fastapi.testclient import TestClient  # noqa: E402

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import ai, db, practices, review  # noqa: E402
from app.main import app  # noqa: E402

db.init_db()
client = TestClient(app)
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


print("[0] mock 模式确认")
r = client.get("/api/ai/status")
print("    ", r.json())
check("未配密钥时进入 mock 模式", r.json()["mock"] is True)

print("\n[1] 空库时创建练习应给出可操作提示")
r = client.post("/api/practices", json={"terms": []})
check("无词条 -> 400 且提示先加词", r.status_code == 400 and "词汇库" in r.json()["detail"], r.json()["detail"])

print("\n[2] 造数据：词汇 + 外刊文章")
words = [("abundant", "word", "adj."), ("resilient", "word", "adj."),
         ("take advantage of", "phrase", None)]
for term, typ, pos in words:
    payload = {"type": typ, "term": term, "pos": pos, "meaning_zh": "测试释义",
               "usage": ""}
    r = client.post("/api/entries", json=payload)
    check(f"建词条 {term} -> 201", r.status_code == 201, r.status_code)

ART = ("The abundant evidence in the report surprised nobody who had followed the case closely. "
       "We take advantage of every opportunity to verify these claims in practice. "
       "A resilient supply chain matters more than sheer volume of inventory on hand today. "
       "Dr. Smith reviewed the manuscript before it went to press last week for publication.")
r = client.post("/api/articles", json={"title": "练习素材文章", "source_name": "The Guardian",
                                       "content": ART})
check("建文章 -> 201", r.status_code == 201, r.json())

print("\n[3] 创建练习：自动取词 + 外刊库选句 + AI 译文")
r = client.post("/api/practices", json={"terms": ["abundant", "resilient", "take advantage of"]})
check("创建练习 -> 201", r.status_code == 201, r.json() if r.status_code != 201 else "")
if r.status_code == 201:
    prac = r.json()
    print(f"    origin={prac['origin']} is_mock={prac['is_mock']} 素材 {len(prac['items'])} 条")
    for it in prac["items"]:
        print(f"      词={it['term']:<18} 中文={it['text_zh'][:40]}")
    check("返回练习 id", isinstance(prac["id"], int))
    check("origin=library（来自外刊库）", prac["origin"] == "library")
    check("每条素材带英文原文", all(it["text_en"] for it in prac["items"]))
    check("每条素材带中文译文", all(it["text_zh"] for it in prac["items"]))
    check("标注了 mock 模式", prac["is_mock"] is True)
    check("素材覆盖 3 个目标词", {it["term"] for it in prac["items"]} ==
          {"abundant", "resilient", "take advantage of"})
    check("网络来源标记为 False", all(it["from_web"] is False for it in prac["items"]))

    print("\n[4] 提交回译：AI 点评 + 三维评分")
    r = client.post(f"/api/practices/{prac['id']}/submit",
                    json={"user_text": "The abundant evidence surprise nobody."})
    check("提交 -> 200", r.status_code == 200, r.json() if r.status_code != 200 else "")
    if r.status_code == 200:
        res = r.json()
        sc = res["scores"]
        print(f"    语法 {sc['grammar']['score']}/{sc['grammar']['max']}  "
              f"地道 {sc['fluency']['score']}/{sc['fluency']['max']}  "
              f"保真 {sc['fidelity']['score']}/{sc['fidelity']['max']}  "
              f"总分 {sc['total']}")
        check("三维分数齐备", all(k in sc for k in ("grammar", "fluency", "fidelity", "total")))
        check("语法满分 30", sc["grammar"]["max"] == 30)
        check("地道满分 30", sc["fluency"]["max"] == 30)
        check("保真满分 40", sc["fidelity"]["max"] == 40)
        check("总分 = 三维之和", sc["total"] == sc["grammar"]["score"] + sc["fluency"]["score"] + sc["fidelity"]["score"])
        check("总分不超 100", sc["total"] <= 100)
        check("带更优表达建议", bool(res["suggestion"]))
        check("回显原文与参考译文", bool(res["source_en"]) and bool(res["reference_zh"]))

    print("\n[5] 空回译被拒")
    r = client.post(f"/api/practices/{prac['id']}/submit", json={"user_text": "   "})
    check("空白回译 -> 400", r.status_code == 400)

    print("\n[6] 练习历史与统计")
    r = client.get("/api/practices")
    check("历史列表 -> 200", r.status_code == 200)
    check("记录 1 次练习", r.json()["total"] == 1, r.json()["total"])
    r = client.get("/api/practices/stats")
    st = r.json()
    print("    ", st)
    check("统计 count=1", st["count"] == 1)
    check("统计有平均分", st["avg_total"] is not None)

print("\n[7] 外刊库没命中时：给可操作提示，不静默失败")
r = client.post("/api/practices", json={"terms": ["nonexistentword"]})
check("库中无命中 -> 400 且提示去加文章", r.status_code == 400 and "外刊库" in r.json()["detail"], r.json()["detail"])

print("\n[8] 复习调度：SM-2 间隔推进")
conn = db.connect()
e = client.get("/api/entries", params={"q": "abundant"}).json()["items"][0]
eid = e["id"]
before = conn.execute("SELECT * FROM review_state WHERE entry_id = ?", (eid,)).fetchone()
print(f"    初始 reps={before['reps']} interval={before['interval_days']} due={before['due_at']}")
r1 = review.mark_reviewed(conn, eid, True)
print(f"    第1次记住 -> interval={r1['interval_days']} reps={r1['reps']} due={r1['due_at']}")
check("第1次记住间隔=1天", r1["interval_days"] == 1)
check("reps 递增", r1["reps"] == before["reps"] + 1)
r2 = review.mark_reviewed(conn, eid, True)
print(f"    第2次记住 -> interval={r2['interval_days']}")
check("第2次记住间隔=2天", r2["interval_days"] == 2)
r3 = review.mark_reviewed(conn, eid, True)
print(f"    第3次记住 -> interval={r3['interval_days']}（2×easiness {r3['easiness']} 取整）")
check("第3次按 easiness 放大", r3["interval_days"] >= 2)
r4 = review.mark_reviewed(conn, eid, False)
print(f"    没记住   -> interval={r4['interval_days']} reps={r4['reps']} easiness={r4['easiness']}")
check("没记住间隔归零", r4["interval_days"] == 0)
check("没记住 reps 归零", r4["reps"] == 0)
check("没记住 easiness 下调", r4["easiness"] < r3["easiness"])
log = conn.execute("SELECT COUNT(*) FROM review_log WHERE entry_id = ?", (eid,)).fetchone()[0]
check("复习日志写入 4 条", log == 4, log)
conn.close()

print("\n[9] 到期查询与两种模式")
r = client.get("/api/review/due", params={"mode": "interval"})
check("间隔模式到期查询 -> 200", r.status_code == 200)
r = client.get("/api/review/due", params={"mode": "date_order"})
check("日期模式查询 -> 200 且返回全部条目", r.status_code == 200 and len(r.json()) >= 3, len(r.json()))
r = client.get("/api/review/due", params={"mode": "bogus"})
check("非法模式 -> 400", r.status_code == 400)
r = client.get("/api/review/due-count")
check("到期计数 -> 200", r.status_code == 200 and "due" in r.json())

print("\n[10] rubric 边界：分数夹紧")
norm = ai._normalize_review({
    "grammar": {"score": 999}, "fluency": {"score": -5}, "fidelity": {"score": "abc"},
})
check("超上限夹到 30", norm["grammar"]["score"] == 30)
check("负值夹到 0", norm["fluency"]["score"] == 0)
check("非数字当 0", norm["fidelity"]["score"] == 0)
check("总分 = 三者之和", norm["total"] == 30 + 0 + 0)
mock = ai._mock_review("some text here")
check("mock 总分 <= 100", mock["total"] <= 100)

print("\n[11] AI 返回 JSON 抽取（容忍 ```json 包裹与杂文本）")
raw = '前言说明\n```json\n{"grammar": {"score": 25}, "fluency": {"score": 20}, "fidelity": {"score": 35}, "suggestion": "x"}\n```\n后记'
parsed = ai._extract_json(raw)
check("能提取围栏包裹的 JSON", parsed["grammar"]["score"] == 25)
try:
    ai._extract_json("没有 JSON")
    check("无 JSON 时报错", False)
except ai.AIError:
    check("无 JSON 时报错", True)

print("\n[12] 用户附带的译文优先于 AI 翻译")
r = client.post("/api/articles", json={
    "title": "用户译文文章",
    "content": "The pragmatic approach saved the company from a costly mistake in the long run.",
    "content_zh": "这种务实的做法使公司避免了一场代价高昂的错误。",
})
check("建带译文的文章 -> 201", r.status_code == 201, r.text)
r = client.post("/api/practices", json={"terms": ["pragmatic"]})
check("创建练习 -> 201", r.status_code == 201, r.text)
prac = r.json()
it = prac["items"][0]
print(f"    zh_from={it['zh_from']} 译文={it['text_zh']}")
check("优先使用用户附带的译文",
      it["zh_from"] == "user" and it["text_zh"] == "这种务实的做法使公司避免了一场代价高昂的错误。",
      str(it))
r = client.post("/api/practices", json={"terms": ["abundant"]})
it2 = r.json()["items"][0]
check("无译文的句子仍回退 AI 译文", it2["zh_from"] == "ai" and it2["text_zh"], str(it2))
r = client.post(f"/api/practices/{prac['id']}/submit",
                json={"user_text": "The pragmatic approach saved the company."})
check("提交 -> 200", r.status_code == 200, r.text)
check("保真评判依据用户译文", "这种务实的做法" in r.json()["reference_zh"], r.json().get("reference_zh"))

print("\n[6] 翻译即复习：排期写回与降级保护")
import json  # noqa: E402

from app import articles, entries  # noqa: E402

# 及格线必须由评分量表推导，而不是拍一个数
check("及格线随 RUBRIC 量表推导（满分 100 → 六成 60）",
      practices.SCORE_MAX == 100 and practices.PASS_SCORE == 60,
      f"max={practices.SCORE_MAX} pass={practices.PASS_SCORE}")

conn = db.connect()
# 用本文件未占用过的词（abundant/pragmatic 已在上文用过），避免撞重复录入
e1 = entries.create_entry(conn, {"type": "word", "term": "abundantly", "meaning_zh": "丰富地"})
e2 = entries.create_entry(conn, {"type": "word", "term": "schedule", "meaning_zh": "日程"})
articles.create_article(conn, {
    "title": "T2",
    "content": "The word is used abundantly in this report and we schedule a review daily.",
    "tags": [],
})
prac = practices.create_practice(conn, ["abundantly", "schedule"])
raw = conn.execute("SELECT focus_entry_ids FROM passages WHERE id=?",
                   (prac["id"],)).fetchone()["focus_entry_ids"]
check("focus_entry_ids 存真实 entry_id（不是词条文本）",
      isinstance(json.loads(raw), list) and all(
          isinstance(x, int) and not isinstance(x, bool) for x in json.loads(raw)), raw)
check("同句共现的两个词都进排期（不漏词）",
      set(practices._focus_entry_ids(
          conn.execute("SELECT focus_entry_ids FROM passages WHERE id=?",
                       (prac["id"],)).fetchone())) == {e1["id"], e2["id"]}, raw)

# mock 提交不得改动排期
snap = {e1["id"]: dict(conn.execute(
    "SELECT interval_days, reps, easiness FROM review_state WHERE entry_id=?",
    (e1["id"],)).fetchone())}
res = practices.submit_translation(conn, prac["id"], "Resources are abundant here.")
check("mock 提交 is_mock=True", res["is_mock"] is True)
check("mock 不更新排期（review_updates 为空）", res["review_updates"] == [],
      str(res["review_updates"]))
now = dict(conn.execute(
    "SELECT interval_days, reps, easiness FROM review_state WHERE entry_id=?",
    (e1["id"],)).fetchone())
check("mock 后复习状态逐字段未变", now == snap[e1["id"]], f"{snap[e1['id']]} -> {now}")

# 真实评分写回
for total, expect in ((85, True), (30, False), (60, True), (59, False)):
    conn.execute("UPDATE review_state SET interval_days=0, reps=0, easiness=2.5 "
                 "WHERE entry_id=?", (e1["id"],))
    fake = {"is_mock": False, "total": total, "grammar": {}, "fluency": {}, "fidelity": {}}
    upd = practices._apply_review_to_entries(conn, [e1["id"]], fake)
    check(f"总分 {total} → {'记住' if expect else '没记住'}",
          bool(upd) and upd[0]["remembered"] == expect,
          f"got={upd[0]['remembered'] if upd else None}")

# 脏数据与边界
class _R:
    def __init__(self, d):
        self._d = d

    def keys(self):
        return self._d.keys()

    def __getitem__(self, k):
        return self._d[k]


check("历史文本型 focus_entry_ids 被安全忽略",
      practices._focus_entry_ids(_R({"focus_entry_ids": '["abundant"]'})) == [])
check("脏 JSON 不抛异常",
      practices._focus_entry_ids(_R({"focus_entry_ids": "not-json"})) == [])
check("bool 不被误当 id",
      practices._focus_entry_ids(_R({"focus_entry_ids": "[true]"})) == [])
check("entry_id 不存在时跳过而非抛错",
      practices._apply_review_to_entries(
          conn, [99999], {"is_mock": False, "total": 90}) == [])
check("库里没有的词不写入排期",
      practices._resolve_entry_ids(conn, ["no-such-word"]) == [])
conn.close()

print("\n[7] 批量翻译：降低网络往返")

SRC3 = [
    "The parameters have to make sense to the animal.",
    "You kind of have to meet them where they are at.",
    "It will eventually stop recoiling.",
]

calls = []


def _fake_chat(system, user, *, temperature=0.3, max_tokens=None):
    calls.append(user)
    return "译文甲\n译文乙\n译文丙"


with patch.object(ai, "_chat", side_effect=_fake_chat), \
     patch.object(ai, "is_mock_mode", return_value=False):
    got = ai.translate_batch(SRC3)

check("3 句只发 1 次请求（原逐句要 3 次）", len(calls) == 1, f"实际 {len(calls)} 次")
check("返回条数与入参一致", len(got) == 3)
check("第 1 句按序对应", got[0]["text"] == "译文甲", got[0]["text"])
check("第 3 句按序对应", got[2]["text"] == "译文丙", got[2]["text"])
check("非 mock", all(not r["is_mock"] for r in got))

fallback = {"n": 0}


def _bad_chat(system, user, *, temperature=0.3, max_tokens=None):
    return "只有一行"          # 行数不对齐


def _single(text_en):
    fallback["n"] += 1
    return {"text": f"逐句{fallback['n']}", "is_mock": False}


with patch.object(ai, "_chat", side_effect=_bad_chat), \
     patch.object(ai, "is_mock_mode", return_value=False), \
     patch.object(ai, "translate_to_chinese", side_effect=_single):
    got = ai.translate_batch(SRC3)

check("行数不对齐时降级回逐句", fallback["n"] == 3, f"逐句 {fallback['n']} 次")
check("降级后译文不错位", [r["text"] for r in got] == ["逐句1", "逐句2", "逐句3"],
      str([r["text"] for r in got]))

with patch.object(ai, "is_mock_mode", return_value=True):
    got = ai.translate_batch(SRC3)
check("mock 模式保持 is_mock 标记", all(r["is_mock"] for r in got))

# 方舟深度思考关闭 —— 防止以后被误删导致点评又变慢
check("方舟域名下发 thinking disabled",
      ai._supports_thinking("https://ark.cn-beijing.volces.com/api/v3"))
check("DeepSeek 域名不下发 thinking（避免 400）",
      not ai._supports_thinking("https://api.deepseek.com/v1"))
check("thinking 白名单只含方舟域名",
      ai._THINKING_CAPABLE_HOST_HINTS == ("volces.com",),
      str(ai._THINKING_CAPABLE_HOST_HINTS))

print("\n[8] 逐句点评：一次只翻一句")

# 建 3 句文章 + 3 个词，逐题提交，验证点评只针对当前句
S3 = [
    "The quick brown fox jumps over the lazy dog near the river bank.",
    "A cautious investor will diversify the portfolio across sectors.",
    "She decided to postpone the meeting until next Thursday morning.",
]
T3 = ["fox", "investor", "postpone"]
for t in T3:
    client.post("/api/entries", json={"term": t, "meaning_zh": "测试"})

art = client.post("/api/articles", json={
    "title": "逐句点评测试", "content": "\n\n".join(S3),
}).json()
p3 = client.post("/api/practices", json={"terms": T3}).json()
items3 = p3["items"]

captured = []


def _fake_review(source_en, reference_zh, user_en):
    captured.append(source_en)
    return {
        "grammar": {"score": 25, "issues": [], "comment": "ok"},
        "fluency": {"score": 25, "issues": [], "comment": "ok"},
        "fidelity": {"score": 34, "issues": [], "comment": "ok"},
        "suggestion": "更优表达", "total": 84, "is_mock": False,
    }


with patch.object(ai, "review_translation", side_effect=_fake_review):
    r = client.post(f"/api/practices/{p3['id']}/submit", json={
        "user_text": "The fox jumps over the dog.", "seq": 1,
        "terms": items3[0]["terms"],
    }).json()

check("点评收到的原文只有第 1 句", captured and captured[-1] == items3[0]["text_en"],
      repr((captured[-1] if captured else "")[:50]))
check("不含其余句子",
      captured and all(s not in captured[-1] for s in items3[1:] for s in
                       [(s["text_en"])]))
check("结果页返回的原文也是本题那句", r["source_en"] == items3[0]["text_en"])
check("排期只写本句词条（非整轮）", len(r.get("review_updates") or []) <= 1,
      f"{len(r.get('review_updates') or [])} 个")

# terms 为空数组 = 本句没有词条 → 不该回退写整轮
captured.clear()
with patch.object(ai, "review_translation", side_effect=_fake_review):
    r_empty = client.post(f"/api/practices/{p3['id']}/submit", json={
        "user_text": "no term here", "seq": 1, "terms": [],
    }).json()
check("terms 为空数组时排期为空（不回退整轮）",
      (r_empty.get("review_updates") or []) == [],
      str(r_empty.get("review_updates")))

# 旧客户端不带 terms → 回退整轮（历史行为）
with patch.object(ai, "review_translation", side_effect=_fake_review):
    r_legacy = client.post(f"/api/practices/{p3['id']}/submit",
                           json={"user_text": "legacy"}).json()
check("旧客户端不带 seq/terms 仍可用", r_legacy.get("scores") is not None)

print("\n[9] 练习记录自动裁剪（只保留最近 25 条）")

check("上限常量为 25", practices.MAX_PRACTICE_RECORDS == 25)

n_entries = client.get("/api/entries").json().get("total")
n_articles = client.get("/api/articles").json().get("total")

# 造 30 条 > 上限 25，必须多造几条才能真的触发裁剪。
# terms 传空数组：本节只验「记录条数上限」，不必顺带把 30 次「记住」写进
# SM-2 复习排期（连续 30 次记住所累积的间隔会溢出 datetime，与本节无关）。
with patch.object(ai, "review_translation", side_effect=_fake_review):
    for i in range(30):
        client.post(f"/api/practices/{p3['id']}/submit", json={
            "user_text": f"round {i}", "seq": 1, "terms": [],
        })

hist = client.get("/api/practices?page_size=100").json()
check("练习记录裁剪到 25 条", hist.get("total") == 25, f"实际 {hist.get('total')}")
texts = [r.get("user_text") for r in hist.get("items", [])]
check("保留最近 25 条、最老的被删",
      "round 29" in texts and "round 0" not in texts,
      f"最新={texts[0] if texts else None} 最旧={texts[-1] if texts else None}")
check("词汇库未被自动删除",
      client.get("/api/entries").json().get("total") == n_entries,
      f"{n_entries} -> {client.get('/api/entries').json().get('total')}")
check("外刊库未被自动删除",
      client.get("/api/articles").json().get("total") == n_articles,
      f"{n_articles} -> {client.get('/api/articles').json().get('total')}")

print("\n结论：")
failed = [n for n, ok in results if not ok]
for n, ok in results:
    print(f"    [{'PASS' if ok else 'FAIL'}] {n}")
print(f"总计：{'全部通过' if not failed else f'{len(failed)} 项失败'}（{len(results)} 项）")
