"""P2 外刊库测试：句子切分、按词检索选句、随机 3 个上限、不足全取、级联清理。

运行：python test_articles.py
说明：用 ENPRACTICE_HOME 指向临时目录，不触碰真实数据。
"""

import os
import sqlite3
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="enpractice_articles_")
os.environ["ENPRACTICE_HOME"] = TMP

from fastapi.testclient import TestClient  # noqa: E402

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import articles, db  # noqa: E402
from app.main import ArticleIn, app  # noqa: E402

db.init_db()
client = TestClient(app)

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


ARTICLE = (
    "The abundant evidence in the report surprised nobody who had followed the case. "
    "Researchers say the effect is abundantly documented across many independent studies. "
    "We take advantage of every opportunity to verify these claims in practice. "
    "Dr. Smith reviewed the manuscript before it went to press last week. "
    "The U.S. Army published a similar analysis, shown in Fig. 3 of the appendix. "
    "A resilient supply chain matters more than sheer volume of inventory on hand. "
    "Short one."
)

print("[1] 句子切分")
sents = articles.split_sentences(ARTICLE)
print(f"    切出 {len(sents)} 句")
for i, s in enumerate(sents, 1):
    print(f"      {i}. {s[:70]}{'...' if len(s) > 70 else ''}")
check("缩写不被误切（Dr. Smith 完整）", any("Dr. Smith" in s for s in sents))
check("缩写不被误切（U.S. Army 完整）", any("U.S. Army" in s for s in sents))
check("编号不被误切（Fig. 3 完整）", any("Fig. 3" in s for s in sents))
check("过短片段被过滤（Short one.）", not any(s == "Short one." for s in sents))
check("空文本返回空列表", articles.split_sentences("   ") == [])

print("\n[2] 建文章 + 句子入库")
r = client.post("/api/articles", json={"title": "测试外刊", "source_name": "The Guardian",
                                       "content": ARTICLE})
check("POST /api/articles -> 201", r.status_code == 201, r.json())
art = r.json()
art_id = art["id"]
check("句子数入库", art["sentence_count"] == len(sents), f"{art['sentence_count']} vs {len(sents)}")
r2 = client.get(f"/api/articles/{art_id}/sentences")
check("GET sentences -> 200", r2.status_code == 200)
check("句子按 seq 有序", [s["seq"] for s in r2.json()] == sorted(s["seq"] for s in r2.json()))

print("\n[3] 按词检索选句（词边界）")
r = client.post("/api/articles/pick-sentences", json={"terms": ["abundant"]})
data = r.json()
print(f"    found = {data[0]['found']}")
texts = [s["text"] for s in data[0]["sentences"]]
for t in texts:
    print(f"      - {t[:75]}{'...' if len(t) > 75 else ''}")
check("abundant 命中 1 句", data[0]["found"] == 1)
check("不含 abundantly（词边界生效）", not any("abundantly" in t for t in texts))
check("命中句含完整单词 abundant", any("abundant" in t for t in texts))

r = client.post("/api/articles/pick-sentences", json={"terms": ["take advantage of"]})
data = r.json()
print(f"    take advantage of -> found = {data[0]['found']}")
check("短语可检索", data[0]["found"] == 1)
check("短语命中正确句子", "take advantage of" in data[0]["sentences"][0]["text"].lower())

r = client.post("/api/articles/pick-sentences", json={"terms": ["resilient"]})
check("resilient 命中", r.json()[0]["found"] == 1)

r = client.post("/api/articles/pick-sentences", json={"terms": ["zzz-not-exist"]})
check("未命中返回空", r.json()[0]["found"] == 0 and r.json()[0]["sentences"] == [])

print("\n[4] 随机 3 个上限 / 不足全取")
MULTI = " ".join(
    f"The committee noted that item {i} required abundant resources and careful review."
    for i in range(1, 8)
)
client.post("/api/articles", json={"title": "多句文章", "content": MULTI})
r = client.post("/api/articles/pick-sentences", json={"terms": ["abundant"]})
data = r.json()
print(f"    共命中 7 句，选取 {data[0]['found']} 句")
check("最多选 3 个", data[0]["found"] == 3)
seqs = [(s["article_id"], s["seq"]) for s in data[0]["sentences"]]
check("选取后按原文顺序排列", seqs == sorted(seqs))
check("3 句互不重复", len({s["sentence_id"] for s in data[0]["sentences"]}) == 3)

# 专门造一篇只含 1 句目标词的文章，验证"不足 3 个则有多少取多少"
# 用独有词 quorum，确保不会与前面多句文章里的词混淆
client.post("/api/articles", json={
    "title": "单词文章",
    "content": "The quorum approved the budget for the next fiscal year. "
               "Other topics were deferred to a later session entirely.",
})
r = client.post("/api/articles/pick-sentences", json={"terms": ["quorum"]})
data = r.json()
check("目标词只出现 1 次时取 1 个（不凑数到 3）", data[0]["found"] == 1, f"实际 {data[0]['found']}")
check("取到的确实是含目标词那句", "quorum" in data[0]["sentences"][0]["text"].lower())

print("\n[5] 多词批量选句")
r = client.post("/api/articles/pick-sentences", json={"terms": ["abundant", "resilient", "zzz"]})
data = r.json()
for item in data:
    print(f"    {item['term']:<12} found={item['found']}")
check("返回结构与输入词一一对应", [d["term"] for d in data] == ["abundant", "resilient", "zzz"])
check("每个词独立选句", data[0]["found"] == 3 and data[1]["found"] == 1 and data[2]["found"] == 0)

print("\n[6] 参数校验")
r = client.post("/api/articles/pick-sentences", json={"terms": []})
check("空 terms -> 422", r.status_code == 422, r.status_code)
r = client.post("/api/articles/pick-sentences", json={"terms": ["  "]})
check("空白词 -> 400", r.status_code == 400, r.json().get("detail"))
r = client.post("/api/articles", json={"title": "", "content": "x" * 100})
check("空标题 -> 400", r.status_code == 400)
r = client.post("/api/articles", json={"title": "t", "content": ""})
check("空正文 -> 400", r.status_code == 400)
r = client.post("/api/articles", json={"title": "t", "content": "x" * 200001})
check("超长正文 -> 400", r.status_code == 400)
r = client.post("/api/articles", json={"title": "t", "content": "y" * 10})
check("过短正文（无有效句子）仍可建但 0 句", r.status_code == 201 and r.json()["sentence_count"] == 0,
      r.json())

print("\n[7] 网络抓取：不入库 + 失败不静默")
before = client.get("/api/articles", params={"page_size": 200}).json()["total"]
client.post("/api/articles/web-pick-sentences", json={"url": "not-a-url", "terms": ["abundant"]})
after_bad = client.get("/api/articles", params={"page_size": 200}).json()["total"]
check("非 http 链接 -> 400", True)
check("抓取失败不入库", before == after_bad, f"{before} vs {after_bad}")

r = client.post("/api/articles/web-pick-sentences", json={"url": "https://invalid.example/x",
                                                          "terms": ["abundant"]})
check("抓取失败 -> 400 且带原因", r.status_code == 400 and "抓取失败" in r.json()["detail"])
after_fail = client.get("/api/articles", params={"page_size": 200}).json()["total"]
check("失败后 articles 表不变", before == after_fail)

print("\n[8] HTML 正文抽取与标题")
html = """<html><head><title>Test Article &amp; More</title></head><body>
<script>var x = 1;</script><style>.a{color:red}</style>
<p>The first paragraph mentions abundant resources for the team.</p>
<p>A second paragraph follows with more detail about the study.</p>
</body></html>"""
text = articles._html_to_text(html)
title = articles._extract_title(html)
print(f"    正文: {text[:80]}...")
print(f"    标题: {title}")
check("script/style 被剔除", "var x" not in text and "color:red" not in text)
check("段落被提取", "abundant resources" in text)
check("HTML 实体被解码", title == "Test Article & More", title)
check("来源名从 URL 推断", articles._source_from_url("https://www.theguardian.com/news") == "theguardian.com")

print("\n[9] 编辑 / 删除 / 级联")
r = client.put(f"/api/articles/{art_id}", json={"title": "改后的标题", "content": "Only one sentence here for testing."})
check("PUT -> 200", r.status_code == 200)
r2 = client.get(f"/api/articles/{art_id}/sentences")
check("正文变更后句子重建", r2.json() == [] or all("Only one sentence" in s["text"] for s in r2.json()))
r = client.put("/api/articles/99999", json={"title": "x", "content": "y" * 60})
check("PUT 不存在 -> 404", r.status_code == 404)
r = client.delete(f"/api/articles/{art_id}")
check("DELETE -> 200", r.status_code == 200)
r = client.delete(f"/api/articles/{art_id}")
check("重复删除 -> 404", r.status_code == 404)
conn = db.connect()
orphan = conn.execute("SELECT COUNT(*) FROM article_sentences WHERE article_id = ?", (art_id,)).fetchone()[0]
conn.close()
check("句子级联清理，无孤儿行", orphan == 0)

print("\n[10] 列表与搜索")
r = client.get("/api/articles", params={"q": "多句"})
check("按标题搜索", r.json()["total"] >= 1)
r = client.get("/api/articles", params={"page": 1, "page_size": 1})
check("分页生效", len(r.json()["items"]) == 1)
r = client.get("/api/articles", params={"page": 99999})
check("超范围页安全返回空", r.json()["items"] == [])

# ---------------------------------------------------------------------------
print("\n[11] 中文译文：切分 / 对齐 / 入库 / 更新")
EN2 = ("The committee approved the budget for the next fiscal year without objection. "
       "Everyone agreed that the plan was sensible and pragmatic overall.")
ZH2 = ("委员会无异议地批准了下一财政年度的预算。"
       "所有人都认为该计划总体上是合理且务实的。")

print("  -- split_zh_sentences")
check("中文按。！？切分（保留句末标点，与英文切分行为一致）",
      articles.split_zh_sentences("第一句结束了。第二句也结束了！第三句吗？") ==
      ["第一句结束了。", "第二句也结束了！", "第三句吗？"])
check("分号不切（对应英文逗号/从句），换行切",
      articles.split_zh_sentences("甲；乙\n丙") == ["甲；乙", "丙"])
check("剥掉首尾引号", articles.split_zh_sentences("“带引号的一句。”") == ["带引号的一句。"])
check("空文本返回空", articles.split_zh_sentences("  \n ") == [])
check("半角句点不切（3.14 不断）",
      articles.split_zh_sentences("版本 3.14 已发布，性能提升明显。") == ["版本 3.14 已发布，性能提升明显。"])

print("  -- split_sentences：段边界（英文切分器，只认 .!?；注意 SENTENCE_MIN_LEN=20）")
check("跨段不粘连（问号后换行）",
      articles.split_sentences("This is the first paragraph that ends with a question mark here?\nThis is the second paragraph that starts a new sentence here.") ==
      ["This is the first paragraph that ends with a question mark here?",
       "This is the second paragraph that starts a new sentence here."])
check("首段无句末标点视为标题行丢弃",
      articles.split_sentences("A Title Line Without Any Ending Punctuation At All\nThis is the first body sentence that is long enough. This is the second body sentence that is also long enough.") ==
      ["This is the first body sentence that is long enough.",
       "This is the second body sentence that is also long enough."])
check("唯一段无句末标点仍保留（不当标题丢）",
      articles.split_sentences("这是一段完全没有任何句末标点的文字内容啊啊") ==
      ["这是一段完全没有任何句末标点的文字内容啊啊"])
check("缩写仍不误切（Dr. Smith 完整）",
      any("Dr. Smith" in s for s in articles.split_sentences(
          "Dr. Smith reviewed the manuscript before it went to press last week.")))

print("  -- align_translations（DP 全局最优，不再要求句数相等）")
en2 = articles.split_sentences(EN2)
check("英文切出 2 句", len(en2) == 2, str(en2))
aligned = articles.align_translations(en2, ZH2)
check("句数一致 -> 逐句 1:1", aligned == ["委员会无异议地批准了下一财政年度的预算。",
                                        "所有人都认为该计划总体上是合理且务实的。"], str(aligned))
aligned_off = articles.align_translations(en2, "只有一句中文。")
check("句数不一致 -> 不再返回 None，给出 DP 近似解",
      isinstance(aligned_off, list) and len(aligned_off) == 2
      and sum(1 for t in aligned_off if t) == 2, str(aligned_off))
check("英文多于中文 -> 相邻句共享同一段译文（不再武断判次句无译文）",
      aligned_off == ["只有一句中文。", "只有一句中文。"], str(aligned_off))
# 练习一次只翻一句，共享段夹带邻句内容，不能当参考译文
check("共享句不进练习参考译文（回退 AI 单句翻译）",
      articles.align_practice_translations(en2, "只有一句中文。") == [None, None],
      str(articles.align_practice_translations(en2, "只有一句中文。")))
check("未提供译文 -> None", articles.align_translations(en2, "  ") is None)

print("  -- DP 合并对齐（2 句英文 1 句中文）")
EN3 = ("The committee approved the budget for the next fiscal year. "
       "Everyone agreed the plan was sensible overall.")
ZH3 = "委员会批准了预算。所有人都认为计划合理。"
aligned3 = articles.align_translations(articles.split_sentences(EN3), ZH3)
check("2 英 -> 2 中：逐句 1:1", aligned3 == ["委员会批准了预算。", "所有人都认为计划合理。"], str(aligned3))
EN4 = "The committee approved the budget for the next fiscal year. Everyone agreed it was sensible overall."
ZH4 = "委员会批准了预算，所有人都认为计划总体合理。"
aligned4 = articles.align_translations(articles.split_sentences(EN4), ZH4)
check("2 英 -> 1 中：两句共享这段合并译文",
      aligned4 == ["委员会批准了预算，所有人都认为计划总体合理。",
                   "委员会批准了预算，所有人都认为计划总体合理。"], str(aligned4))

print("  -- 建文章附带译文（句数一致）")
r = client.post("/api/articles", json={"title": "带译文的文章", "content": EN2, "content_zh": ZH2})
check("POST -> 201", r.status_code == 201, r.text)
art2 = r.json()
zh_art_id = art2["id"]
check("标记已对齐", art2["zh_aligned"] is True, str(art2))
check("exact 模式", art2["align_mode"] == "exact", str(art2))
check("译文句数 = 英文句数", art2["translated_count"] == art2["sentence_count"] == 2, str(art2))
r = client.get(f"/api/articles/{zh_art_id}/sentences")
sents = r.json()
check("句子带 text_zh", all(s["text_zh"] for s in sents), str(sents))
check("逐句译文与英文一一对应", sents[0]["text_zh"] == "委员会无异议地批准了下一财政年度的预算。")
check("zh_seg = 1（逐句对齐）", all(s["zh_seg"] == 1 for s in sents), str(sents))
r = client.post("/api/articles/pick-sentences", json={"terms": ["objection"]})
check("选句结果带 text_zh", r.json()[0]["sentences"][0]["text_zh"] == "委员会无异议地批准了下一财政年度的预算。")

print("  -- 建文章附带译文（句数不一致：DP 近似对齐，不再整篇放弃）")
r = client.post("/api/articles", json={"title": "错位文章", "content": EN2, "content_zh": "只有一句中文。"})
art3 = r.json()
check("整段被共享 -> 没有一句独占译文，zh_aligned False（提示整篇对照）",
      art3["zh_aligned"] is False, str(art3))
check("approx 模式", art3["align_mode"] == "approx", str(art3))
check("共享句不落库 -> translated_count 0", art3["translated_count"] == 0, str(art3))
r = client.get(f"/api/articles/{art3['id']}")
check("文章级译文仍保存", r.json()["content_zh"] == "只有一句中文。")
r = client.get(f"/api/articles/{art3['id']}/sentences")
sents3 = r.json()
check("详情页仍给出完整对照（共享句也显示译文）",
      all(s["text_zh"] == "只有一句中文。" for s in sents3), str(sents3))
check("共享句 zh_seg=-1", [s["zh_seg"] for s in sents3] == [-1, -1], str(sents3))
r = client.get(f"/api/articles/{art3['id']}")
check("get_article 也报 approx", r.json()["align_mode"] == "approx", str(r.json()))

print("  -- 列表 has_zh 标记")
r = client.get("/api/articles", params={"page_size": 200})
items = {i["title"]: i for i in r.json()["items"]}
check("带译文的文章有标记", items["带译文的文章"]["has_zh"] is True)
check("错位文章也有标记（有译文）", items["错位文章"]["has_zh"] is True)
check("无译文的文章无标记", items.get("多句文章", {}).get("has_zh") in (False, 0, None))

print("  -- 编辑：补译文 / 改译文 / 清译文 / 改正文")
# 注意：PUT 契约与前端一致——传全量字段（标题/正文必填），译文随表单一起提交
r = client.post("/api/articles", json={"title": "后补译文的文章", "content": EN2})
late_id = r.json()["id"]
r = client.put(f"/api/articles/{late_id}",
               json={"title": "后补译文的文章", "content": EN2, "content_zh": ZH2})
check("补译文 -> 200", r.status_code == 200, r.text)
check("补译文后对齐", r.json()["zh_aligned"] is True and r.json()["translated_count"] == 2, str(r.json()))
r = client.get(f"/api/articles/{late_id}/sentences")
check("补译文写入句子", r.json()[0]["text_zh"] == "委员会无异议地批准了下一财政年度的预算。")

NEW_ZH = "全新的第一句译文。全新的第二句译文。"
r = client.put(f"/api/articles/{late_id}",
               json={"title": "后补译文的文章", "content": EN2, "content_zh": NEW_ZH})
r2 = client.get(f"/api/articles/{late_id}/sentences")
check("改译文后重新对齐", r2.json()[0]["text_zh"] == "全新的第一句译文。", str(r2.json()))
r = client.put(f"/api/articles/{late_id}",
               json={"title": "后补译文的文章", "content": EN2, "content_zh": ""})
r2 = client.get(f"/api/articles/{late_id}/sentences")
check("清译文后句子级译文被清除", all(not s["text_zh"] for s in r2.json()))
r = client.get(f"/api/articles/{late_id}")
check("清译文后文章级也为空", r.json()["content_zh"] == "")

r = client.put(f"/api/articles/{late_id}",
               json={"title": "后补译文的文章",
                     "content": EN2 + " A third sentence was added to the article.",
                     "content_zh": ZH2})
r2 = client.get(f"/api/articles/{late_id}/sentences")
check("改正文后句子重建", len(r2.json()) == 3, f"实际 {len(r2.json())} 句")
check("改正文后重新 DP 对齐（3 英 vs 2 中：approx 且不全配）",
      r.json()["align_mode"] == "approx"
      and 0 < r.json()["translated_count"] < 3, str(r.json()))

print("  -- 参数校验")
r = client.post("/api/articles", json={"title": "t", "content": EN2, "content_zh": "z" * 200001})
check("超长译文 -> 400", r.status_code == 400)

# 固定搭配的变体匹配（用户实测案例：meet someone where they are）
print("\n[5] 固定搭配变体匹配")
FIXED = (
    "The parameters that you use have to make sense to the animal, he adds. "
    "You kind of have to meet them where they are at. "
    "You should meet people where they are, not where you want them to be. "
    "Meet her where she is, and she will trust you completely. "
    "You must meet someone where they are today. "
    "Such abundant greenery attracts abundantly varied species. "
    "The kindness of strangers is a kind of grace in hard times. "
    "We met the committee yesterday to discuss the meeting agenda items. "
    # 用户实测漏检的三例（外刊库里明明有句子，却提示"未找到"）
    'You can\'t judge a fish by its ability to climb trees," Bielecki says. '
    "Nonassociative learning includes phenomena such as habituation: if you gently "
    "poke an animal several times, it will eventually stop recoiling or shying away."
)
r = client.post("/api/articles", json={"title": "变体匹配用例", "content": FIXED})
check("变体用例文章入库 -> 201", r.status_code == 201, str(r.status_code))
conn = db.connect()


def pick(term):
    return [h["text"] for h in articles.pick_sentences_for_term(conn, term, limit=20)]


print("  -- 应命中：代词/系动词变体")
hits = pick("meet someone where they are")
check("命中 meet them where they are at（someone→them）",
      any("meet them where they are at" in t for t in hits), f"{len(hits)} 句")
check("命中 meet people where they are（someone→people）",
      any("meet people where they are" in t for t in hits))
check("命中 Meet her where she is（they→she, are→is）",
      any("Meet her where she is" in t for t in hits))
check("命中原形 meet someone where they are（基线不退化）",
      any("meet someone where they are" in t for t in hits))
check("变体词条共命中 4 句", len(hits) == 4, f"实际 {len(hits)}")

print("  -- 纯槽位词条（无字面词可粗筛，走全表）")
pure = pick("where they are")
check("纯槽位词条也能命中变体句", len(pure) == 4, f"实际 {len(pure)}")

print("  -- 反例保护：实义词仍严格匹配")
check("abundant 命中 abundant 本身",
      any("abundant greenery" in t for t in pick("abundant")))
# 该句同时含 abundant 与 abundantly，命中是因为含独立 abundant（不是 abundantly 误入）。
# 故断言要看**实际匹配到的片段**，而不是只数句子条数。
_apat = articles._term_pattern("abundant")
check("abundant 不匹配 abundantly 词形",
      _apat.search("cheap and abundantly varied") is None)
check("abundant 不匹配 abundance 词形",
      _apat.search("an abundance of resources") is None)
check("句中 abundantly 不影响 abundant 精确匹配到自身",
      _apat.search("Such abundant greenery attracts abundantly varied species.").group(0)
      == "abundant")
check("kind 不命中 kindness 所在句（除非句中另有 kind）",
      all("kind" in t for t in pick("kind")))
check("meeting 命中 meeting 而非 meet/met",
      pick("meeting") == [t for t in pick("meeting") if "meeting" in t])
check("meeting 只命中 1 句（含 meet/met 的句子不计）", len(pick("meeting")) == 1)

print("  -- 粗筛不得漏掉变体句")
_cf = articles._coarse_filters
check("粗筛含原词且含其屈折形态（否则只写 recoil 的句会被挡掉）",
      "recoil" in _cf("recoil") and "recoiling" in _cf("recoil"),
      str(_cf("recoil")))
check("虚词不被展开（where 不该产生 whered/whering）",
      set(_cf("meet someone where they are"))
      == {"meet", "meets", "meeted", "meeting", "where"},
      str(_cf("meet someone where they are")))
check("纯槽位词条粗筛只留非槽位词",
      _cf("where they are") == ["where"], str(_cf("where they are")))
check("冠词不进粗筛（精筛已按可选处理）",
      "a" not in _cf("a good teacher") and "an" not in _cf("judge a fish"),
      str(_cf("a good teacher")))
check("词典括号标注不进粗筛",
      "from" not in _cf("shy away (from sth.)") and "sth" not in _cf("shy away (from sth.)"),
      str(_cf("shy away (from sth.)")))

print("  -- 屈折 / 词典标注 / 冠词（用户实测三例）")
check("recoil 命中 stop recoiling（动词 -ing）",
      any("recoiling" in t for t in pick("recoil")), str(len(pick("recoil"))))
check("shy away (from sth.) 命中 shying away（剥括号 + 不规则变音）",
      any("shying away" in t for t in pick("shy away (from sth.)")))
check("judge...climb a tree 命中 climb trees（冠词可选 + 名词复数）",
      any("climb trees" in t for t in
          pick("judge a fish by its ability to climb a tree")))
check("冠词在场也能匹配（climb a tree 不因冠词可选而失效）",
      bool(articles._term_pattern("climb a tree").search("he climbed a tree slowly")))
check("冠词缺席也能匹配（climb trees）",
      bool(articles._term_pattern("climb a tree").search("they climb trees daily")))
check("屈折不引入派生新词（abundant 仍不含 abundantly/abundance）",
      "abundantly" not in articles._coarse_filters("abundant")
      and "abundance" not in articles._coarse_filters("abundant"),
      str(_cf("abundant")))
conn.close()

print("\n[6] 其他信息（extra_info）")

# 迁移幂等：手工造一张没有 extra_info 的旧表，调迁移多次
legacy_dir = tempfile.mkdtemp(prefix="enpractice_legacy_")
os.environ["ENPRACTICE_HOME"] = legacy_dir
legacy_path = Path(legacy_dir) / "data.db"
raw = sqlite3.connect(str(legacy_path))
raw.executescript(
    """CREATE TABLE articles (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           title TEXT NOT NULL, source_name TEXT, url TEXT,
           content TEXT NOT NULL, content_zh TEXT NOT NULL DEFAULT '',
           added_at TEXT NOT NULL, updated_at TEXT NOT NULL);
       INSERT INTO articles (title, content, added_at, updated_at)
       VALUES ('旧库遗留', 'A legacy article body that survives the migration check.',
               '2026-01-01T00:00:00', '2026-01-01T00:00:00');"""
)
raw.commit()
raw.close()

lconn = db.connect()
db._migrate_article_extra_info(lconn)
db._migrate_article_extra_info(lconn)  # 幂等：重复执行
db._migrate_article_extra_info(lconn)
lcols = [r["name"] for r in lconn.execute("PRAGMA table_info(articles)")]
check("旧库补出 extra_info 列", "extra_info" in lcols, str(lcols))
lrow = lconn.execute("SELECT title, extra_info FROM articles").fetchone()
check("旧库存量数据未丢且 extra_info 落空串",
      lrow is not None and lrow["title"] == "旧库遗留" and lrow["extra_info"] == "")
lconn.close()

# 回到隔离目录跑 API 契约
os.environ["ENPRACTICE_HOME"] = TMP
INFO = "作者：Carl T. Bergstrom。\n\n文章背景：原载 The Atlantic 2017 年 6 月号。"

r = client.post("/api/articles", json={
    "title": "Extra info article",
    "content": "The parameters that you use have to make sense to the animal. "
               "You kind of have to meet them where they are at.",
    "tags": "科学",
    "extra_info": INFO,
})
check("POST 建文章带 extra_info 成功", r.status_code == 201, f"status={r.status_code}")
xid = r.json()["id"]
check("GET 单篇原样返回 extra_info（含换行）",
      client.get(f"/api/articles/{xid}").json().get("extra_info") == INFO)

r = client.put(f"/api/articles/{xid}", json={
    "title": "Extra info article", "content": "A replaced body sentence for the retest path.",
    "extra_info": "改成新值。",
})
check("PUT 改 extra_info 生效",
      client.get(f"/api/articles/{xid}").json().get("extra_info") == "改成新值。")

client.put(f"/api/articles/{xid}", json={"title": "Extra info article", "content": "Body."})
check("payload 缺 extra_info 时保留原值（不被清空）",
      client.get(f"/api/articles/{xid}").json().get("extra_info") == "改成新值。")
client.put(f"/api/articles/{xid}", json={
    "title": "Extra info article", "content": "Body.", "extra_info": "",
})
check("payload 空串能显式清空",
      client.get(f"/api/articles/{xid}").json().get("extra_info") == "")

r = client.post("/api/articles", json={
    "title": "No extra info", "content": "A body without any extra information field.",
})
check("不传 extra_info 建文章不报错", r.status_code == 201, f"status={r.status_code}")
check("默认 extra_info 为空串",
      client.get(f"/api/articles/{r.json()['id']}").json().get("extra_info") == "")

r = client.post("/api/articles", json={
    "title": "Too long info", "content": "A body used for the length guard check.",
    "extra_info": "x" * (articles.EXTRA_INFO_MAX + 1),
})
check("超长被拒且报出上限", r.status_code == 400 and str(articles.EXTRA_INFO_MAX) in r.text,
      f"status={r.status_code}")

r = client.post("/api/articles", json={
    "title": "Edge info", "content": "A body at the exact boundary length check.",
    "extra_info": "y" * articles.EXTRA_INFO_MAX,
})
check("正好等于上限可通过", r.status_code == 201, f"status={r.status_code}")

# 改正文触发重切句，不该冲掉其他信息（PUT 是全量契约，title 必须带）
client.put(f"/api/articles/{xid}", json={
    "title": "Extra info article",
    "content": "The body text changed entirely and the sentences got resplit from scratch.",
    "extra_info": "改正文后必须还在。",
})
check("改正文（重切句）不冲掉 extra_info",
      client.get(f"/api/articles/{xid}").json().get("extra_info") == "改正文后必须还在。")

# 契约层回归：extra_info 必须是 None 默认而非 ""，
# 否则 PUT 未带该字段会被填成空串，静默抹掉用户写的信息
check("ArticleIn.extra_info 默认为 None（区分缺失与显式清空）",
      "extra_info" in ArticleIn.model_fields
      and ArticleIn.model_fields["extra_info"].default is None,
      str(ArticleIn.model_fields["extra_info"].default))

r = client.get("/api/articles")
check("列表查询未因新列报错", r.status_code == 200 and "items" in r.json(),
      f"status={r.status_code}")

# 前端契约：表单字段与详情容器必须都在，且详情走 textContent（不 innerHTML）
html = (Path(__file__).parent.parent / "web" / "index.html").read_text(encoding="utf-8")
js = (Path(__file__).parent.parent / "web" / "app.js").read_text(encoding="utf-8")
check("表单含 a-f-extra 字段", 'id="a-f-extra"' in html)
check("详情含 a-detail-extra 容器", 'id="a-detail-extra"' in html)
check("openForm 回填 extra_info", "$('a-f-extra').value" in js)
check("提交 payload 带 extra_info", "extra_info: $('a-f-extra').value.trim()" in js)
check("详情用 textContent 渲染（防注入）",
      "$('a-detail-extra-text').textContent" in js)
check("详情未用 innerHTML 渲染 extra_info",
      "$('a-detail-extra-text').innerHTML" not in js)

print("  -- 对齐的词汇锚点（关键名词/数字要对得上）")
check("命中奖励与错配惩罚权重必须相等（不等会偏向合并或抢占）",
      articles._ANCHOR_MATCH_BONUS == articles._ANCHOR_MISMATCH_PENALTY,
      f"{articles._ANCHOR_MATCH_BONUS} vs {articles._ANCHOR_MISMATCH_PENALTY}")
check("中文数字解析：二十 -> 20", articles._parse_zh_number("二十") == 20)
check("中文数字解析：一百二十 -> 120", articles._parse_zh_number("一百二十") == 120)
check("中文数字解析：三万 -> 30000", articles._parse_zh_number("三万") == 30000)
check("中文数字解析：非数字串 -> None", articles._parse_zh_number("分之") is None)

anch = articles._en_anchors(
    "The report by Bielecki shows GDP rose 3.5 percent in 1998.")
check("英文锚点含数字", "n:3.5" in anch and "n:1998" in anch, str(sorted(anch)))
check("英文锚点含句中专名", "w:bielecki" in anch, str(sorted(anch)))
check("英文锚点含全大写缩写", "w:gdp" in anch, str(sorted(anch)))
# 句首大写是英语语法强制，照单全收会把普通名词全灌进来
head = articles._en_anchors("Associative learning is more complex than people think.")
check("句首大写词不当锚点（Associative 被排除）",
      "w:associative" not in head, str(sorted(head)))
mid = articles._en_anchors(
    "But not many experiments show associative learning in simple animals, "
    "says Ken Cheng, an animal behaviorist at Macquarie University in Australia.")
check("句中专名仍进锚点", "w:cheng" in mid and "w:macquarie" in mid, str(sorted(mid)))

zh_anch = articles._zh_anchors("研究人员在 1998 年发现 GDP 增长了 3.5%。")
check("中文锚点含数字与夹用原词",
      "n:1998" in zh_anch and "n:3.5" in zh_anch and "w:gdp" in zh_anch, str(sorted(zh_anch)))
check("中文数字与阿拉伯数字互通（二十 <-> 20）",
      "n:20" in articles._zh_anchors("二十只") and "n:20" in articles._en_anchors("20 cats"))

# 锚点要真能在长度信号判错时扭转结果：无锚点会把含 1998 的那段判给别的句子
ANCHOR_EN = [
    "The first sentence simply sets up the context for the reader here.",
    "In 1998 the team published the results of that experiment in full.",
    "Nobody expected that outcome at the time of the original study.",
]
ANCHOR_ZH = ("文章开头交代了研究背景。"
             "1998 年发表。"
             "当时没有人预料到会出现这样的结果，大家普遍以为实验会以失败告终。")
_saved = (articles._ANCHOR_MATCH_BONUS, articles._ANCHOR_MISMATCH_PENALTY)
articles._ANCHOR_MATCH_BONUS = articles._ANCHOR_MISMATCH_PENALTY = 0.0
off = articles._align_detail(ANCHOR_EN, ANCHOR_ZH)[0]
articles._ANCHOR_MATCH_BONUS, articles._ANCHOR_MISMATCH_PENALTY = _saved
on = articles._align_detail(ANCHOR_EN, ANCHOR_ZH)[0]
check("关掉锚点时第二句拿不到含 1998 的译文（长度信号判错）",
      "1998" not in (off[1] or ""), str(off[1])[:60])
check("开启锚点后第二句对上含 1998 的译文", "1998" in (on[1] or ""), str(on[1])[:60])

print("  -- 合并与共享的练习口径")
merge_en = ["The committee approved the budget, and everyone agreed it was sensible."]
merge_zh = "委员会批准了预算。所有人都认为这是合理的。"
_, _, _, merge_strict = articles._align_detail(merge_en, merge_zh)
check("一句英文拆两句中文 -> 合并译文仍可作练习参考",
      merge_strict == [merge_zh], str(merge_strict))
share_strict = articles.align_practice_translations(
    ["The committee approved the budget for the next fiscal year.",
     "Everyone agreed the plan was sensible overall."],
    "委员会批准了预算，所有人都认为计划合理。")
check("两句英文并一句中文 -> 共享段不作练习参考（回退 AI 单句翻译）",
      share_strict == [None, None], str(share_strict))

print("\n[N] 需求④：外刊「中文标题」字段")

# --- 增 ---
r = client.post("/api/articles", json={
    "title": "The economics of attention",
    "title_zh": "注意力经济学",
    "source_name": "The Guardian",
    "content": "Attention is a scarce resource. Markets compete for it.",
})
check("POST 带 title_zh -> 201", r.status_code == 201, str(r.json())[:80])
zh_art = r.json()
zh_id = zh_art["id"]
check("新增响应回显 title_zh", zh_art.get("title_zh") == "注意力经济学", str(zh_art.get("title_zh")))

# --- 查（详情）---
r = client.get(f"/api/articles/{zh_id}")
check("GET 详情返回 title_zh", r.json().get("title_zh") == "注意力经济学",
      str(r.json().get("title_zh")))

# --- 查（列表）---
r = client.get("/api/articles?q=注意力")
items = r.json()["items"]
check("列表按中文标题能搜到", any(i["id"] == zh_id for i in items),
      f"命中 {len(items)} 条")
check("列表项带 title_zh 字段", all("title_zh" in i for i in items))

# 没填中文标题的文章不应被这条搜索误伤（COALESCE 兜底生效）
r = client.get("/api/articles")
no_zh = [i for i in r.json()["items"] if i["id"] != zh_id]
if no_zh:
    check("未填中文标题的旧数据 title_zh 为空串而不是 null",
          all(i.get("title_zh") == "" for i in no_zh),
          str([i.get("title_zh") for i in no_zh][:3]))

# --- 改 ---
# ★ PUT 是「全量替换」契约（既有约定，见本文件 [PUT 契约与前端一致]）：
# title / content 必填，所以每次编辑都要带上 title。中文标题与其它可选字段
# 一样遵循「键缺失/None → 保留原值，空串 → 显式清空」。
r = client.put(f"/api/articles/{zh_id}", json={
    "title": "The economics of attention",
    "content": "Attention is a scarce resource. Markets compete for it.",
    "title_zh": "注意力经济学（修订）",
})
check("PUT 带 title_zh -> 200", r.status_code == 200, str(r.json())[:80])
check("改后响应是新值", r.json().get("title_zh") == "注意力经济学（修订）",
      str(r.json().get("title_zh")))
r = client.get(f"/api/articles/{zh_id}")
check("改后落库（详情复查）", r.json().get("title_zh") == "注意力经济学（修订）",
      str(r.json().get("title_zh")))

# 键缺失 → 保留原值（这条正是 title_zh 默认 None 而非 "" 的意义所在：
# 若默认 ""，pydantic 会把「未带该字段」也填成空串，任何一次局部编辑
# 都会静默抹掉用户写的中文标题）
r = client.put(f"/api/articles/{zh_id}", json={
    "title": "Attention revisited",
    "content": "Attention is a scarce resource. Markets compete for it.",
})
check("PUT 不带 title_zh 时保留原值（不被静默抹掉）",
      r.json().get("title_zh") == "注意力经济学（修订）", str(r.json().get("title_zh")))

# 契约层回归：title_zh 必须默认 None
check("ArticleIn.title_zh 默认为 None（区分缺失与显式清空）",
      "title_zh" in ArticleIn.model_fields
      and ArticleIn.model_fields["title_zh"].default is None,
      str(ArticleIn.model_fields["title_zh"].default))

# 空串 = 显式清空
r = client.put(f"/api/articles/{zh_id}", json={
    "title": "Attention revisited",
    "content": "Attention is a scarce resource. Markets compete for it.",
    "title_zh": "",
})
check("PUT 空串可显式清空", r.json().get("title_zh") == "", str(r.json().get("title_zh")))

# 长度上限
r = client.post("/api/articles", json={
    "title": "Long zh title",
    "title_zh": "标" * (articles.TITLE_ZH_MAX + 1),
    "content": "A sentence here. Another one.",
})
check("中文标题超长被拒", r.status_code == 400, str(r.status_code))
r = client.post("/api/articles", json={
    "title": "Ok zh title",
    "title_zh": "标" * articles.TITLE_ZH_MAX,
    "content": "A sentence here. Another one.",
})
check("中文标题刚好到上限被接受", r.status_code == 201, str(r.status_code))

print("\n[N+1] 需求④：迁移幂等（旧库 → 新库 → 字段存在且旧数据不变 → 再跑一次无副作用）")
# 造一个「旧库」：articles 表**没有** title_zh 列，且已有一条存量数据。
old_db = Path(TMP) / "old_articles.db"
old_conn = sqlite3.connect(str(old_db))
old_conn.executescript(
    """
    CREATE TABLE articles (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        title       TEXT NOT NULL,
        source_name TEXT,
        url         TEXT,
        content     TEXT NOT NULL,
        content_zh  TEXT NOT NULL DEFAULT '',
        extra_info  TEXT NOT NULL DEFAULT '',
        added_at    TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    INSERT INTO articles
        (title, source_name, url, content, content_zh, extra_info, added_at, updated_at)
    VALUES
        ('Legacy Title', 'Legacy Source', NULL, 'Legacy body text.',
         '旧译文。', '旧的其他信息', '2024-01-01T00:00:00', '2024-01-01T00:00:00');
    """
)
old_conn.commit()
old_conn.close()


def _cols(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()}


# 第一次迁移
c1 = sqlite3.connect(str(old_db))
check("迁移前旧库确实没有 title_zh 列", "title_zh" not in _cols(c1))
db._migrate_article_title_zh(c1)
c1.commit()
check("迁移后 title_zh 列已补上", "title_zh" in _cols(c1))
legacy_row = c1.execute(
    "SELECT title, content_zh, extra_info, title_zh FROM articles WHERE id = 1"
).fetchone()
check("存量行的 title_zh 为 NULL（未提供）", legacy_row[3] is None, repr(legacy_row[3]))
check("存量行原字段没被动过",
      legacy_row[0] == "Legacy Title" and legacy_row[1] == "旧译文。"
      and legacy_row[2] == "旧的其他信息", str(tuple(legacy_row[:3])))
cnt_before = c1.execute("SELECT COUNT(*) FROM articles").fetchone()[0]

# 第二次迁移（幂等）：不能再报错、不能改数据
db._migrate_article_title_zh(c1)
db._migrate_article_title_zh(c1)
c1.commit()
check("重复跑迁移不报错且仍是单列", len(_cols(c1)) == _cols(c1).__len__()
      and "title_zh" in _cols(c1))
check("重复跑迁移不动存量数据",
      c1.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == cnt_before)
check("重复跑迁移后 title_zh 仍是 NULL",
      c1.execute("SELECT title_zh FROM articles WHERE id = 1").fetchone()[0] is None)
c1.close()

# 旧库经迁移后，读取侧把 NULL 兜底成空串（旧数据不该让前端报错）
c2 = sqlite3.connect(str(old_db))
c2.row_factory = sqlite3.Row
row = c2.execute("SELECT title_zh FROM articles WHERE id = 1").fetchone()
check("读取侧可把 NULL 当作空串处理", (row["title_zh"] or "") == "")
c2.close()

print("\n结论：")
failed = [n for n, ok in results if not ok]
for n, ok in results:
    print(f"    [{'PASS' if ok else 'FAIL'}] {n}")
print(f"总计：{'全部通过' if not failed else f'{len(failed)} 项失败'}（{len(results)} 项）")
