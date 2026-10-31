"""和 AI 讨论本题 + 采纳修正评分回写（后端）。

运行：python test_practice_discuss.py
说明：用 ENPRACTICE_HOME 指向临时目录，不触碰真实数据。不依赖真实 API key：
      所有 AI 调用都被 monkeypatch 掉，只验我们自己的逻辑（校验、回写、留痕、并发）。

覆盖需求里的硬约束：
  A. 纯讨论**不写库** —— 调 discuss 后四个分数一字未动。
  B. 点采纳 → 三维 + total 正确写回。
  C. 非法分数（grammar=50 / fidelity=-5 / total 对不上）→ **拒绝写入并报错，
     不被静默夹取**（夹取会让用户以为 AI 真给过那个分）。
  D. revised=false → 不接受修正，前端拿不到采纳入口所需数据。
  E. 并发冲突 → 409，不覆盖别人的写入。
  F. 留痕：ai_feedback.revisions 追加一条，旧的三维评分与 issues 仍完好可解析。
  G. 无 API key → 明确报错，不静默降级成编造回答。
  H. 脏数据（ai_feedback 非法 JSON / passage 被删）不阻断讨论与采纳。
"""

import json
import os
import tempfile

TMP = tempfile.mkdtemp(prefix="enpractice_discuss_")
os.environ["ENPRACTICE_HOME"] = TMP
os.environ.pop("OPENAI_API_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import ai, db, practices  # noqa: E402
from app.main import app  # noqa: E402

db.init_db()
client = TestClient(app)
results = []

FEEDBACK = {
    "grammar": {
        "score": 18, "max": 30, "label": "语法正确",
        "issues": ["冠词缺失：in report 应为 in the report"],
        "comment": "细节扣分",
    },
    "fluency": {
        "score": 24, "max": 30, "label": "表达地道",
        "issues": [],
        "comment": "整体能读懂",
    },
    "fidelity": {
        "score": 36, "max": 40, "label": "语义保真",
        "issues": ["漏译后半句"],
        "comment": "语义基本对齐",
    },
    "total": 78,
    "suggestion": "The abundant evidence in the report surprised nobody.",
    "is_mock": False,
}


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def seed(user_text="The abundant evidences in report surprised nobody.",
        feedback=None, passage=True, raw_feedback=None):
    """造一条练习记录，返回 (practice_id, passage_id)。"""
    conn = db.connect()
    passage_id = None
    if passage:
        cur = conn.execute(
            """INSERT INTO passages (title, source_url, origin, text_en, text_zh,
                                     focus_entry_ids, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("翻译练习素材", None, "library",
             "The abundant evidence in the report surprised nobody.",
             "报告中大量的证据并未让任何人感到惊讶。",
             "[]", "2026-01-01T09:00:00"),
        )
        passage_id = cur.lastrowid
    cur = conn.execute(
        """INSERT INTO practices
           (passage_id, user_text, ai_feedback, score_grammar, score_fluency,
            score_fidelity, score_total, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (passage_id, user_text,
         raw_feedback if raw_feedback is not None
         else json.dumps(feedback if feedback is not None else FEEDBACK, ensure_ascii=False),
         18, 24, 36, 78, "2026-01-01T09:30:00"),
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid, passage_id


def scores_of(pid):
    conn = db.connect()
    row = conn.execute(
        "SELECT score_grammar, score_fluency, score_fidelity, score_total, ai_feedback"
        " FROM practices WHERE id = ?", (pid,)).fetchone()
    conn.close()
    return dict(row)


class FakeChat:
    """替换 ai._chat，记录收到的 prompt，并返回预设文本。"""

    def __init__(self, text):
        self.text = text
        self.calls = []

    def __call__(self, system, user, **kw):
        self.calls.append({"system": system, "user": user})
        return self.text

    @property
    def last(self):
        return self.calls[-1]


def with_chat(text, fn):
    """临时换掉 ai._chat 并保证还原（讨论走 ai._chat，不走 chat()）。"""
    orig = ai._chat
    fake = FakeChat(text)
    ai._chat = fake
    try:
        return fn(fake), fake
    finally:
        ai._chat = orig


# 给 AI 配上一个假 key，否则 discuss 会在 is_mock_mode() 处直接报错
_orig_key = ai.get_api_key
ai.get_api_key = lambda: "test-key-for-discuss"
_orig_ai_cfg = ai._settings
ai._settings = lambda: {"base_url": "http://fake.local/v1", "model": "fake", "timeout": 1}

print("[1] 新增的讨论用 system prompt：独立常量，且没动 _REVIEW_SYSTEM")
check("_DISCUSS_SYSTEM 是独立常量", isinstance(getattr(ai, "_DISCUSS_SYSTEM", None), str))
check("_REVIEW_SYSTEM 仍是独立常量（未被讨论口径污染）",
      isinstance(getattr(ai, "_REVIEW_SYSTEM", None), str)
      and ai._REVIEW_SYSTEM != ai._DISCUSS_SYSTEM)
check("_DISCUSS_SYSTEM 提到先判断质疑是否成立",
      "质疑是否成立" in ai._DISCUSS_SYSTEM)
check("_DISCUSS_SYSTEM 含三维合法区间 0-30/0-30/0-40",
      "0-30" in ai._DISCUSS_SYSTEM and "0-40" in ai._DISCUSS_SYSTEM)
check("_DISCUSS_SYSTEM 要求 total 等于三维之和",
      "total 必须等于三维之和" in ai._DISCUSS_SYSTEM)
check("_DISCUSS_SYSTEM 禁止迎合虚高",
      "迎合用户而虚高" in ai._DISCUSS_SYSTEM)
check("_DISCUSS_SYSTEM 禁止无理由推翻用户已确认判断",
      "不得推翻用户已确认的判断" in ai._DISCUSS_SYSTEM)
check("_DISCUSS_SYSTEM 允许半星", "半星" in ai._DISCUSS_SYSTEM)
check("_DISCUSS_SYSTEM 沿用 cut down 纠错口径",
      "cut down on" in ai._DISCUSS_SYSTEM)

print("\n[2] 纯讨论：不写库")
pid, _ = seed()
before = scores_of(pid)
good = json.dumps({
    "reply": "你说得对，冠词属于细节档，单次只应扣 1-3 分，这里扣多了。",
    "revised": True,
    "scores": {"grammar": 27, "fluency": 24, "fidelity": 36},
    "total": 87,
}, ensure_ascii=False)


def _call(fake):
    return client.post(f"/api/practices/{pid}/discuss", json={"question": "冠词真的算错吗？"})


r, fake = with_chat(good, _call)
check("讨论接口 -> 200", r.status_code == 200, r.text[:200])
if r.status_code == 200:
    d = r.json()
    check("带 reply", d.get("reply", "").startswith("你说得对"), str(d.get("reply"))[:40])
    check("revised=true", d.get("revised") is True)
    check("带三维新分", (d["scores"]["grammar"], d["scores"]["fluency"],
                          d["scores"]["fidelity"]) == (27, 24, 36), str(d.get("scores")))
    check("带 total", d.get("total") == 87, str(d.get("total")))

after = scores_of(pid)
check("★ 讨论后数据库分数完全没变",
      (after["score_grammar"], after["score_fluency"],
       after["score_fidelity"], after["score_total"]) == (18, 24, 36, 78),
      str((after["score_grammar"], after["score_fluency"],
           after["score_fidelity"], after["score_total"])))
check("★ 讨论后 ai_feedback 也没被改", after["ai_feedback"] == before["ai_feedback"])

# 上下文完整性：原句/译文/回译/首评 issues/suggestion/新提问都要在 prompt 里
prompt = fake.last["user"]
for label, needle in [
    ("英文原句", "The abundant evidence in the report surprised nobody."),
    ("中文参考译文", "报告中大量的证据并未让任何人感到惊讶。"),
    ("用户当时的回译", "abundant evidences"),
    ("首次评分维度", "语法正确"),
    ("首次 issues", "in the report"),
    ("首次 suggestion", "surprised nobody."),
    ("用户新提问", "冠词真的算错吗？"),
]:
    check(f"讨论上下文含{label}", needle in prompt, prompt[:0])
check("用的是讨论专用 system prompt", fake.last["system"] == ai._DISCUSS_SYSTEM)
check("讨论 prompt 独立于首次点评口径（未整段复制 _REVIEW_SYSTEM）",
      ai._DISCUSS_SYSTEM != ai._REVIEW_SYSTEM
      and "严格" not in ai._DISCUSS_SYSTEM[:40])

print("\n[3] 采纳：三维 + total 正确写回")
expected_old = {"score_grammar": 18, "score_fluency": 24,
                "score_fidelity": 36, "score_total": 78}
r = client.patch(f"/api/practices/{pid}/revise-scores", json={
    "scores": {"grammar": 27, "fluency": 24, "fidelity": 36},
    "total": 87,
    "question": "冠词真的算错吗？",
    "reply": "你说得对，冠词属于细节档。",
    "expected_old": expected_old,
})
check("采纳 -> 200", r.status_code == 200, r.text[:200])
if r.status_code == 200:
    d = r.json()
    check("返回 before（旧值）", d["before"]["score_total"] == 78, str(d.get("before")))
    check("返回 after（新值）", d["scores"]["total"] == 87, str(d.get("scores")))

after = scores_of(pid)
check("★ score_grammar 写回 27", after["score_grammar"] == 27, str(after["score_grammar"]))
check("★ score_fluency 写回 24", after["score_fluency"] == 24, str(after["score_fluency"]))
check("★ score_fidelity 写回 36", after["score_fidelity"] == 36, str(after["score_fidelity"]))
check("★ score_total 写回 87", after["score_total"] == 87, str(after["score_total"]))

print("\n[4] 留痕：ai_feedback.revisions 追加，且首评内容仍完好")
fb = json.loads(after["ai_feedback"])
check("首评三维评分仍在", fb["grammar"]["score"] == 18 and fb["total"] == 78,
      str(fb.get("total")))
check("首评 issues 仍在", fb["grammar"]["issues"] == FEEDBACK["grammar"]["issues"])
check("首评 suggestion 仍在", fb.get("suggestion") == FEEDBACK["suggestion"])
revs = fb.get("revisions")
check("有 revisions 数组", isinstance(revs, list) and len(revs) == 1, str(type(revs)))
if isinstance(revs, list) and revs:
    rev = revs[0]
    check("留痕有时间戳", bool(rev.get("at")), str(rev.get("at")))
    check("留痕记了用户质疑", rev.get("question") == "冠词真的算错吗？", str(rev.get("question")))
    check("留痕记了 AI 回复", "细节档" in (rev.get("reply") or ""), str(rev.get("reply"))[:40])
    check("留痕记了改前分数", rev["before"]["score_grammar"] == 18, str(rev.get("before")))
    check("留痕记了改后分数", rev["after"]["grammar"] == 27 and rev["after"]["total"] == 87,
          str(rev.get("after")))

r = client.get(f"/api/practices/{pid}")
check("详情接口回出 revisions 数组", isinstance(r.json().get("revisions"), list)
      and len(r.json()["revisions"]) == 1, str(r.json().get("revisions"))[:80])

print("\n[5] 非法分数：拒绝写入并报错，不静默夹取")
pid2, _ = seed()
base2 = scores_of(pid2)
bad_cases = [
    ("grammar=50 越界", {"grammar": 50, "fluency": 24, "fidelity": 36}, 104),
    ("fidelity=-5 越界", {"grammar": 18, "fluency": 24, "fidelity": -5}, 37),
    ("fluency=31 越界", {"grammar": 18, "fluency": 31, "fidelity": 36}, 85),
    ("缺 fidelity 维度", {"grammar": 18, "fluency": 24}, 42),
    ("grammar 不是数字", {"grammar": "很好", "fluency": 24, "fidelity": 36}, 78),
    ("grammar 是 true（bool 陷阱）", {"grammar": True, "fluency": 24, "fidelity": 36}, 61),
    ("total 与三维之和不符", {"grammar": 18, "fluency": 24, "fidelity": 36}, 999),
]
for label, sc, total in bad_cases:
    r = client.patch(f"/api/practices/{pid2}/revise-scores", json={"scores": sc, "total": total})
    ok = r.status_code == 400
    detail = ""
    if ok:
        detail = r.json().get("detail", "")[:60]
    else:
        detail = f"HTTP {r.status_code} {r.text[:80]}"
    check(f"非法分数被拒（{label}）-> 400", ok, detail)

after2 = scores_of(pid2)
check("★ 全部非法尝试后数据库分数一字未动",
      (after2["score_grammar"], after2["score_fluency"],
       after2["score_fidelity"], after2["score_total"]) == (18, 24, 36, 78),
      str((after2["score_grammar"], after2["score_fluency"],
           after2["score_fidelity"], after2["score_total"])))
check("★ 非法尝试没有留下 revisions 留痕",
      "revisions" not in (after2["ai_feedback"] or ""), (after2["ai_feedback"] or "")[:60])

# 直接验「不夹取」这条：grammar=50 必须报越界，而不是变成 30 写进去
try:
    ai._validate_discuss_scores({"grammar": 50, "fluency": 24, "fidelity": 36, "total": 110})
    check("★ grammar=50 抛错而不是被夹成 30", False, "没抛错")
except ai.DiscussScoreError as exc:
    check("★ grammar=50 抛错而不是被夹成 30", "越界" in str(exc), str(exc))

# 半星合法，且 total 允许 0.5 以内的差
sc, tt = ai._validate_discuss_scores(
    {"grammar": 23.5, "fluency": 24, "fidelity": 36, "total": 83})
check("半星分数被接受", sc["grammar"] == 23.5 and tt == 83, f"{sc} {tt}")
try:
    ai._validate_discuss_scores({"grammar": 23.5, "fluency": 24, "fidelity": 36, "total": 84.5})
    check("total 与半星之和差 1.0 → 拒绝", False, "没抛错")
except ai.DiscussScoreError as exc:
    check("total 与半星之和差 1.0 → 拒绝", "不一致" in str(exc), str(exc))

print("\n[6] revised=false：纯讨论，不给采纳入口")
pid3, _ = seed()
norevise = json.dumps({"reply": "首评没问题：just 对应译文里的'正是'，按口径不扣分。",
                       "revised": False}, ensure_ascii=False)
r3, _ = with_chat(norevise, lambda f: (
    client.post(f"/api/practices/{pid3}/discuss", json={"question": "just 到底扣不扣分？"})))
check("讨论 -> 200", r3.status_code == 200, r3.text[:160])
if r3.status_code == 200:
    d = r3.json()
    check("revised=false", d.get("revised") is False)
    check("scores 为 None（前端据此隐藏采纳按钮）", d.get("scores") is None, str(d.get("scores")))
    check("total 为 None", d.get("total") is None, str(d.get("total")))
    check("仍带 reply", "just" in d.get("reply", ""), str(d.get("reply"))[:40])
after3 = scores_of(pid3)
check("★ revised=false 讨论后分数不变",
      (after3["score_grammar"], after3["score_total"]) == (18, 78))

print("\n[7] 并发冲突：expected_old 对不上 → 409，不覆盖")
pid4, _ = seed()
client.patch(f"/api/practices/{pid4}/revise-scores", json={
    "scores": {"grammar": 20, "fluency": 24, "fidelity": 36}, "total": 80})
# 另一个窗口仍拿着 78 的旧值再来采纳
r = client.patch(f"/api/practices/{pid4}/revise-scores", json={
    "scores": {"grammar": 28, "fluency": 24, "fidelity": 36}, "total": 88,
    "expected_old": {"score_grammar": 18, "score_fluency": 24,
                     "score_fidelity": 36, "score_total": 78},
})
check("旧值对不上 -> 409", r.status_code == 409, f"HTTP {r.status_code} {r.text[:120]}")
after4 = scores_of(pid4)
check("★ 冲突时没有覆盖别人的写入", after4["score_grammar"] == 20, str(after4["score_grammar"]))
check("★ 冲突时没有追加第二条留痕",
      len(json.loads(after4["ai_feedback"])["revisions"]) == 1,
      str(len(json.loads(after4["ai_feedback"])["revisions"])))

r = client.patch(f"/api/practices/{pid4}/revise-scores", json={
    "scores": {"grammar": 28, "fluency": 24, "fidelity": 36}, "total": 88,
    "expected_old": {"score_grammar": 20, "score_fluency": 24,
                     "score_fidelity": 36, "score_total": 80},
})
check("旧值对得上 -> 200（连续两次采纳）", r.status_code == 200, f"HTTP {r.status_code}")
check("第二次采纳后 total=88", scores_of(pid4)["score_total"] == 88)
check("两次采纳留下两条留痕",
      len(json.loads(scores_of(pid4)["ai_feedback"])["revisions"]) == 2)

print("\n[8] 无 API key：明确报错，不编造")
ai.get_api_key = _orig_key
pid5, _ = seed()
r = client.post(f"/api/practices/{pid5}/discuss", json={"question": "这样对吗？"})
check("无 key 讨论 -> 400（不是 502）", r.status_code == 400, f"HTTP {r.status_code} {r.text[:120]}")
check("错误里提示去设置里填密钥", "API Key" in r.json().get("detail", ""),
      r.json().get("detail", "")[:80])
check("★ 无 key 时分数不变",
      (scores_of(pid5)["score_grammar"], scores_of(pid5)["score_total"]) == (18, 78))
ai.get_api_key = lambda: "test-key-for-discuss"

print("\n[9] 空提问 / 脏数据 / 素材被删")
r = client.post(f"/api/practices/{pid5}/discuss", json={"question": "   "})
check("空提问 -> 400", r.status_code == 400, f"HTTP {r.status_code}")

r = client.post("/api/practices/999999/discuss", json={"question": "在吗"})
check("记录不存在 -> 404", r.status_code == 404, str(r.status_code))
r = client.patch("/api/practices/999999/revise-scores",
                 json={"scores": {"grammar": 20, "fluency": 20, "fidelity": 20}, "total": 60})
check("采纳时记录不存在 -> 404", r.status_code == 404, str(r.status_code))

# 脏 ai_feedback（**真正解析不了的 JSON**）：讨论要能跑（首评降级成 {}）
pid6, _ = seed(raw_feedback='{"grammar": {"score": 20, "issues": [')


def _d6(fake):
    return client.post(f"/api/practices/{pid6}/discuss", json={"question": "这题怎么改？"})


r6, _ = with_chat(json.dumps({"reply": "整体没问题。", "revised": False}), _d6)
check("脏 ai_feedback 不阻断讨论 -> 200", r6.status_code == 200, r6.text[:160])

# 脏 ai_feedback 也不阻断采纳：原文被保留，修正仍能写入
r = client.patch(f"/api/practices/{pid6}/revise-scores", json={
    "scores": {"grammar": 25, "fluency": 24, "fidelity": 36}, "total": 85})
check("脏 ai_feedback 不阻断采纳 -> 200", r.status_code == 200, r.text[:160])
fb6 = json.loads(scores_of(pid6)["ai_feedback"])
check("脏数据的原文被保留下来",
      fb6.get("_raw_feedback") == '{"grammar": {"score": 20, "issues": [',
      str(fb6.get("_raw_feedback")))
check("脏数据也能留下修正留痕", len(fb6.get("revisions") or []) == 1)

# 素材（passage）被删后仍可讨论：原句变 None，模型依据"（无记录）"作答
pid7, passage7 = seed()
conn = db.connect()
conn.execute("DELETE FROM passages WHERE id = ?", (passage7,))
conn.commit()
conn.close()


def _d7(fake):
    return client.post(f"/api/practices/{pid7}/discuss", json={"question": "扣分合理吗？"})


r7, fake7 = with_chat(json.dumps({"reply": "扣分可以接受。", "revised": False}), _d7)
check("素材被删仍可讨论 -> 200", r7.status_code == 200, r7.text[:160])
check("素材被删时原句显示为（无记录）", "（无记录）" in fake7.last["user"],
      fake7.last["user"][:80])
r = client.patch(f"/api/practices/{pid7}/revise-scores", json={
    "scores": {"grammar": 20, "fluency": 24, "fidelity": 36}, "total": 80})
check("素材被删仍可采纳 -> 200", r.status_code == 200, r.text[:160])

print("\n[10] 其他接口没被牵连")
r = client.get("/api/practices?page=1&page_size=25")
check("练习列表仍可用", r.status_code == 200, str(r.status_code))
r = client.get(f"/api/practices/{pid}")
check("详情仍可用且带 revisions 字段", r.status_code == 200 and "revisions" in r.json())
r = client.get("/api/practices/stats")
check("统计仍可用", r.status_code == 200, str(r.status_code))

ai.get_api_key = _orig_key
ai._settings = _orig_ai_cfg

print("\n结论：")
failed = [n for n, ok in results if not ok]
for n, ok in results:
    print(f"    [{'PASS' if ok else 'FAIL'}] {n}")
print(f"总计：{'全部通过' if not failed else f'{len(failed)} 项失败'}（{len(results)} 项）")
raise SystemExit(1 if failed else 0)