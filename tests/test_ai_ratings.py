# -*- coding: utf-8 -*-
"""需求 4：AI 辅助评分后端测试。

跑法：
    python test_ai_ratings.py

隔离：ENPRACTICE_HOME 指向临时目录，绝不触碰 %APPDATA%\\EnPractice\\data.db。
AI 调用全部由假响应注入（monkeypatch ai.chat），不发真实网络请求；
"未配置密钥" 的分支则反过来 —— 显式清掉 OPENAI_API_KEY 并让 load_settings
返回空 api_key，确保真的走到报错路径。

覆盖的关键行为：
  1. 正常 JSON / markdown 代码块包裹 / 裸数组 三种返回形态都能解析
  2. 非法分值（3.3 / 越界 7 / 非数字）被**规整**成半星而不是丢弃
  3. 空响应、非法 JSON → 该条记error，同批其它条目不受影响
  4. API key 缺失 → 明确报错，不静默降级
  5. overwrite=False 跳过已评分词条；overwrite=True 也**只返回建议不写库**
  6. 全程零写库：调用前后 entries 表的评分列逐行比对
  7. 判定口径落到提示词里：跨文章频率≠单篇高频、掌握复杂度、释义作为依据
  8. 跨来源频率给的是「命中篇数 / 库里总篇数」；库小、空释义都有明确标注
"""

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.gettempdir()) / "enpractice_test_ai_ratings"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
os.environ["ENPRACTICE_HOME"] = str(TMP)  # 必须在导入 app 之前设置

sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import ai, ai_ratings, articles, db, entries  # noqa: E402
from app.main import app  # noqa: E402

PASSED = 0
FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
    else:
        FAILED += 1
    print("    [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  -> " + str(detail)) if detail else ""))


def section(title):
    print("\n" + title)


# ---------------------------------------------------------------------------
# 假 AI 响应
# ---------------------------------------------------------------------------


class FakeAI:
    """替换 ai.chat 的假实现。按批（一次调用 = 一个 batch）返回预设内容。

    responses 可以是：
      - 字符串：该批返回这段内容
      - 异常实例：该批抛出（用来模拟整批失败）
      - 函数 f(user_text) -> str：**从提示词里读出 entry_id 再回**。
        批量用例必须用这种：id 是造数据时动态拿到的，写死 1/2 会因为
        对不上真实 id 而被 parse_suggestions 当成"模型幻觉"丢掉，
        测试就会因为一个假响应的写法问题而假失败。
      - 列表：按调用次序依次取出，用尽后重复最后一个
    """

    def __init__(self, responses):
        if not isinstance(responses, list):
            responses = [responses]
        self.responses = responses
        self.calls = []  # 每次调用的 user prompt 文本

    def __call__(self, system, user, **kwargs):
        self.calls.append(user)
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        item = self.responses[idx]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(user)
        return item


def echo_ids(n_keep, imp=4, use=3):
    """构造一个假响应函数：从 prompt 里抓出 entry_id，只给前 n_keep 条有效建议。

    剩下的 id 不出现在返回里 —— 模拟模型漏给，用来验证"漏给的条目记 error
    而不是让整批报废"。
    """
    def _make(user_text):
        found = re.findall(r"entry_id=(\d+)", user_text)
        items = [{"entry_id": int(eid), "importance": imp, "usefulness": use,
                  "reason": {"importance": "外刊高频", "usefulness": "偏书面"}}
                 for eid in found[:n_keep]]
        return ok_json(items)
    return _make


def install_fake(monkey_holder, responses):
    """装上假 AI 并返回 (fake, 还原函数)。"""
    fake = FakeAI(responses)
    original = ai.chat
    ai.chat = fake
    monkey_holder.append(original)
    return fake


def restore_ai(saved_list):
    for fn in saved_list:
        ai.chat = fn
    saved_list.clear()


def ok_json(entries_payload):
    return json.dumps({"items": entries_payload}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 测试数据
# ---------------------------------------------------------------------------


def seed():
    """建库 + 造数据。返回 (conn, ids)。**幂等**：重复调用不报错、不重复插入。

    test_signals_and_prompt 会自己调一次，主流程又调一次；不去重的话第二次
    撞 DuplicateEntryError。用「查已存在就跳过」而不是清库，是因为主流程
    后面还有用例依赖这些初始评分（ephemeral 两维都有、candid 只有一维）。
    """
    db.init_db()
    conn = db.connect()
    ids = {}
    specs = [
        ("abundant", "adj.", "丰富的；充裕的", 4.5, 3.0),
        ("ample", "adj.", "充足的", None, None),
        ("ubiquitous", "adj.", "无处不在的", None, None),
        # 已评分：overwrite=False 时必须被跳过
        ("ephemeral", "adj.", "短暂的", 2.0, 2.0),
        # 只评了一维：也算「用户已表态」，同样该跳过
        ("candid", "adj.", "坦率的", 3.0, None),
        # 短语：音节数对它无意义，不应出现在统计依据里
        ("take advantage of", "", "利用", None, None),
    ]
    for term, pos, meaning, imp, use in specs:
        existing = entries.find_duplicate(conn, "phrase" if not pos else "word", term)
        if existing:
            ids[term] = existing[0]
            continue
        item = entries.create_entry(conn, {
            "type": "phrase" if not pos else "word",
            "term": term, "pos": pos, "meaning_zh": meaning,
            "usage": "", "source": "",
            "importance": imp, "usefulness": use,
        })
        ids[term] = item["id"]

    # 外刊数据：验证"命中次数 / 命中文章数"真的被算出来。已存在则跳过
    # （create_article 不查重，重复调用会插出第二篇，命中数跟着翻倍）
    if not conn.execute("SELECT 1 FROM articles LIMIT 1").fetchone():
        articles.create_article(conn, {
            "title": "Nature", "content": (
                "Resources are abundant in this region. "
                "Water remains abundant even in the dry season. "
                "An abundant supply of clean water changed local farming. "
                "Candid leaders welcome criticism."
            ),
            "content_zh": "", "source_name": "Nature", "url": "", "tags": [],
        })
        articles.create_article(conn, {
            "title": "Science", "content": (
                "Such abundant resources attracted research funding. "
                "The candid report was published today."
            ),
            "content_zh": "", "source_name": "Science", "url": "", "tags": [],
        })
    conn.commit()
    return conn, ids


def snapshot_ratings(conn):
    """抓取全表评分列，用于事后比对"有没有被写库"。"""
    rows = conn.execute("SELECT id, importance, usefulness FROM entries ORDER BY id").fetchall()
    return [(r["id"], r["importance"], r["usefulness"]) for r in rows]


# ---------------------------------------------------------------------------
# ① 提示词与统计依据
# ---------------------------------------------------------------------------


def test_signals_and_prompt():
    section("[1] 统计依据采集与提示词内容")
    conn, ids = seed()
    try:
        items = [entries.get_entry(conn, ids["abundant"]),
                 entries.get_entry(conn, ids["take advantage of"])]
        signals = ai_ratings.collect_signals(conn, items)

        abundant = signals[0]
        # 实测口径：s1/s2/s3（Nature）+ s5（Science）= 4 个句子，跨 2 篇
        check("abundant 命中 4 个句子 / 2 篇文章",
              abundant["sentence_hits"] == 4 and abundant["article_hits"] == 2,
              "sentence=%s article=%s" % (abundant.get("sentence_hits"), abundant.get("article_hits")))
        check("abundant 有音节数（3）", abundant.get("syllables") == 3, abundant.get("syllables"))
        check("单词给词长", abundant.get("term_length") == 8, abundant.get("term_length"))

        # 变体不匹配派生词：库里没有 abundantly / abundance，命中数不该被撑大
        cand = ai_ratings.collect_signals(conn, [items[0]])[0]
        check("变体不过度扩张：命中数就是 abundant 的真实出现次数（4）",
              cand["sentence_hits"] == 4, cand["sentence_hits"])

        phrase = signals[1]
        check("短语**没有**音节数（算不出就不给，不能硬凑）",
              "syllables" not in phrase, phrase.get("syllables"))
        check("短语在库中无命中时如实给 0（这是真事实，不是「算不出」）",
              phrase.get("sentence_hits") == 0 and phrase.get("article_hits") == 0,
              phrase.get("sentence_hits"))

        text = ai_ratings.format_signals(signals)
        check("提示词含词条与中文释义", "abundant" in text and "丰富的" in text)
        check("提示词含跨来源频率（命中篇数 / 库里总篇数）",
              "跨来源频率=命中 2 篇 / 库里共 2 篇" in text, text)
        check("提示词含句子命中数（仅作参考）",
              "句子命中 4 个，仅作参考" in text, text)
        check("提示词含音节数（估算标注）", "音节数(估算)=3" in text)
        check("提示词含词长", "词长=8" in text)
        # 分篇数必须排在句子次数**前面**：先看到"12 次"会让模型把单篇重复
        # 误读成高频，而"命中几篇"才是"普遍出现"的证据
        line = [ln for ln in text.splitlines() if "abundant" in ln][0]
        check("★ 分篇数排在句子次数前面（防单篇高频误导）",
              line.index("命中 2 篇") < line.index("句子命中 4 个"), line)
        # 短语那行不能出现音节数（少给一项 > 硬凑一项）
        phrase_line = [ln for ln in text.splitlines() if "take advantage of" in ln]
        check("提示词里短语行不含音节数",
              phrase_line and "音节数" not in phrase_line[0], phrase_line)

        sysmsg = ai_ratings._SUGGEST_SYSTEM
        check("系统提示词要求 0-5 且 0.5 整数倍",
              "0.5 的整数倍" in sysmsg and "0 到 5" in sysmsg)
        check("系统提示词要求严格 JSON", "严格 JSON" in sysmsg)
        check("系统提示词要求引用具体数字", "具体数字" in sysmsg)
        check("系统提示词要求优先引用分篇数（而非只引次数）",
              "优先引用命中篇数" in sysmsg, sysmsg[:0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ①b 判定口径是否真的落进提示词
# ---------------------------------------------------------------------------


def test_criteria_wording_in_prompt():
    """用户给的判定口径必须**字面**出现在提示词里。

    用源码文本断言而不是"看起来像"：口径是给模型看的自然语言，
    改动它等于改动产品定义，这种地方模糊不得。

    口径变更：
    - 实用性从「写作与口语常用」改为**四场景**（雅思口语 / 雅思写作 /
      欧美生活·外网 / 职场英语），且**雅思写作占比最高**、点睛之笔给高分；
    - 重要性补上**不对称加权**（越难记但越常见分越高），否则模型退化成
      两维取平均，abundant（高频但极简单）就会拿高分；
    - 两处反例统一换成 **abundant vs analogous to**（设计约束：
      「abundant 怎么会比 analogous to 高呢」）。
    """
    section("[1b] 判定口径落进提示词")
    sysmsg = ai_ratings._SUGGEST_SYSTEM
    appjs = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    # --- 口径一：重要性 = 雅思频率 × 掌握复杂度（综合考量） ---
    check("★ 口径：重要性以**雅思**为主场景",
          "这个词/短语在**雅思**中的重要性" in sysmsg)
    check("★ 口径：重要性由「雅思频率」与「掌握复杂度」综合考量",
          "雅思频率" in sysmsg and "掌握复杂度" in sysmsg)
    check("口径：要求分别考虑两维后再综合",
          "分别考虑这两个维度后再综合" in sysmsg)

    # --- 不对称加权（本轮核心：缺了它模型就会取平均，abundant 就拿高分） ---
    check("★ 口径：写明两维**不是等价两维**、不许各打分再平均",
          "不是等价的两维" in sysmsg and "不要各打一个分再取平均" in sysmsg)
    check("★ 口径：有「不对称加权」小节",
          "不对称加权" in sysmsg)
    check("★ 口径：越难记又越常见 → 给到该档**上沿**",
          "给到该档的**上沿**" in sysmsg and "越难记又越常见，分越高" in sysmsg)
    check("★ 口径：频率高但复杂度低 → **中低分**、不许因高频顶格",
          "频率高但复杂度低" in sysmsg and "中低分" in sysmsg
          and "不许因为高频就顶格" in sysmsg)
    check("★ 口径：复杂度高但频率低 → 中分左右，不许因难顶格",
          "复杂度高但频率低" in sysmsg and "中分左右" in sysmsg
          and "不许因为\"难\"就顶格" in sysmsg)

    # --- 主题文章常见词可加分（是"可上调"不是"必须上调"） ---
    check("★ 口径：主题文章常见词可加分",
          "主题文章常见词可加分" in sysmsg
          and "环境 / 经济 / 科技 / 健康 / 教育" in sysmsg)
    check("★ 口径：写明是「可上调」而非「必须上调」，且要在理由里说明主题",
          "是\"可上调\"不是\"必须上调\"" in sysmsg and "属于哪个主题的常见词" in sysmsg)

    # --- 反例：abundant vs analogous to（本轮点名的一对） ---
    check("★ 反例：点名 abundant 高频但复杂度低、importance 不应高",
          "abundant" in sysmsg and "高频但几乎人人都会、复杂度低" in sysmsg
          and "importance **不应高**" in sysmsg)
    check("★ 反例：点名 analogous to 是短语、复杂度高、应高于 abundant",
          "analogous to" in sysmsg and "是短语不是单词" in sysmsg
          and "importance **应高于 abundant**" in sysmsg)
    check("★ 反例：要求模型判断前先比较这一对",
          "判断前先比较这一对" in sysmsg)
    check("复杂度示例仍在：用法不直观的表达复杂度高",
          "in lieu of" in sysmsg and "复杂度高" in sysmsg)

    # --- 保留：跨文章频率 ≠ 单篇高频（本轮未被推翻） ---
    check("★ 保留：跨文章频率 ≠ 单篇文章内高频",
          "跨文章频率 ≠ 单篇文章内的高频" in sysmsg)
    check("★ 保留：点名 uncanny 作单篇高频的典型",
          "uncanny" in sysmsg and "语料偶然" in sysmsg)
    check("保留：明确要求以命中篇数占比判断「普遍」",
          "命中篇数占库里总篇数的比例" in sysmsg)

    # --- 口径二：实用性 = 四场景，雅思写作占比最高 ---
    check("★ 口径：实用性按**四个场景**列全",
          "**四个场景**" in sysmsg
          and "① **雅思口语**" in sysmsg
          and "② **雅思写作（占比最高）**" in sysmsg
          and "③ **欧美生活 / 外网网络使用**" in sysmsg
          and "④ **职场英语**" in sysmsg)
    check("★ 口径：雅思写作占比最高、是第一主导场景",
          "**雅思写作占比最高**" in sysmsg and "第一主导场景" in sysmsg)
    check("★ 口径：四场景各配了典型用例",
          "P3 论证、描述人与经历时的自然表达" in sysmsg
          and "Task 1 数据描述、Task 2 论证里的学术书面表达" in sysmsg
          and "社交媒体、技术论坛、日常网络交流里的" in sysmsg
          and "含俚语与网络用语" in sysmsg
          and "邮件、会议、汇报、谈判里的专业表达" in sysmsg)

    # --- 点睛之笔给高分 ---
    check("★ 口径：点睛之笔给高分（可替换平庸表达、让论证地道有力）",
          "**点睛之笔给高分**" in sysmsg
          and "替换掉一个平庸表达" in sysmsg and "地道有力" in sysmsg)
    check("★ 口径：冗余表达与易误用生僻词给低分",
          "增加不了什么气色的冗余表达" in sysmsg and "容易误用的生僻词" in sysmsg)
    check("★ 反例：abundant 对写作无出彩作用 usefulness 不该高",
          "对写作无出彩作用的词，usefulness **不该高**" in sysmsg)
    check("★ 反例：analogous to 这类论证连接手段 usefulness 应更高",
          "这类论证连接手段，usefulness **应更高**" in sysmsg)

    # --- 两维差异示例：不能与「雅思写作为第一主导场景」自相矛盾 ---
    check("★ 保留：两维可不同，且示例已避开「阅读高频→usefulness低」的矛盾写法",
          "两维可以不同" in sysmsg
          and "雅思阅读里高频但口语几乎不用" not in sysmsg)

    # --- 口径三：依据 = 用户写的中文释义 + 外部语料 ---
    check("★ 口径：释义本身是重要判断依据（不是标签）",
          "释义内容本身就是重要的判断依据" in sysmsg)
    check("口径：外部语料作为佐证，且弱样本时降权",
          "外部语料" in sysmsg and "佐证" in sysmsg)
    check("口径：明确要求结合释义与语料、不要只看统计",
          "不要只看统计" in sysmsg)

    # --- 数据质量：样本小、空释义 ---
    check("口径：样本量小时要求别过度解读",
          "样本提示" in sysmsg and "不要把一两篇的命中当成" in sysmsg)
    check("口径：空释义要说明只靠词形与语料判断",
          "用户未填写释义" in sysmsg and "不要假装看到了释义" in sysmsg)

    # --- 旧口径不许残留 ---
    # "学术外刊高频"是上一版的措辞：它暗示"在一批学术外刊里常出现"就够了，
    # 与新口径的「跨来源普遍 + 复杂度」直接冲突，必须彻底消失而不是降级保留。
    check("★ 旧口径「学术外刊高频」已无残留", "学术外刊高频" not in sysmsg)
    check("旧口径「词频量级」已无残留（次数不能单独决定重要性）",
          "词频量级" not in sysmsg)
    check("旧口径「脱离母语者的刚需程度」已无残留",
          "脱离母语者的刚需程度" not in sysmsg)
    check("旧示例理由「外刊出现 12 次 / 5 篇」已无残留",
          "外刊出现 12 次" not in sysmsg)
    # 本轮新增的残留检查：这些正是用户点名的"评估有点问题"的根因措辞
    check("★ 旧口径「写作与口语」已无残留（实用性改为四场景 + 雅思写作为主）",
          "写作与口语" not in sysmsg)
    check("★ 旧口径「职场场景或雅思考试」已无残留（重要性改为以雅思为主）",
          "职场场景或雅思考试" not in sysmsg)
    check("★ 旧口径措辞「表达常见但极易掌握的词不算重要」已随反例改写消失",
          "表达常见但极易掌握的词不算重要" not in sysmsg)
    check("★ 旧反例「That's in part because」已被 abundant / analogous to 取代",
          "That's in part because" not in sysmsg)
    check("★ 旧示例理由「口语写作都自然」已无残留", "口语写作都自然" not in sysmsg)
    check("★ 旧示例理由「职场常见」已无残留（理由示例也必须跟雅思口径对齐）",
          "职场常见" not in sysmsg)

    # --- 保留的硬约束一个都不能丢 ---
    check("硬约束保留：0–5 且 0.5 整数倍",
          "0 到 5" in sysmsg and "0.5 的整数倍" in sysmsg)
    check("硬约束保留：每维一句 ≤30 字中文理由",
          "不超过 30 字" in sysmsg)
    check("硬约束保留：理由必须引用具体数字",
          "必须**引用统计依据里的具体数字**" in sysmsg and "优先引用命中篇数" in sysmsg)
    check("硬约束保留：只输出严格 JSON", "严格 JSON" in sysmsg and "markdown 代码块" in sysmsg)
    check("硬约束保留：数据缺失不硬编数字", "绝不硬编数字" in sysmsg)

    # =========================================================================
    # 前端 overwrite 语义（用户第二条：详情页=重新评估，批量不重新评估）
    # =========================================================================
    section("[1b-2] overwrite 语义：详情页=重新评估 / 批量=不重新评估")
    check("★ 详情页入口传 overwrite: true（再次点击 = 重新评估）",
          re.search(r"entry_ids:\s*\[id\],\s*overwrite:\s*true", appjs) is not None)
    check("★ 选中批量入口保持 overwrite: false（不重新评估）",
          re.search(r"entry_ids:\s*ids,\s*overwrite:\s*false", appjs) is not None)
    check("★ 未评分全库批量保持 overwrite: false",
          re.search(r"suggest-unrated',\s*\{\s*overwrite:\s*false", appjs) is not None)
    check("详情页别名 suggestRatings 复用同一条（重新评估）路径",
          re.search(r"suggestRatings:\s*function\s*\(id\)\s*\{\s*return aiRateOne\(", appjs) is not None)
    check("详情页不再残留 overwrite: false",
          re.search(r"entry_ids:\s*\[id\],\s*overwrite:\s*false", appjs) is None)
    check("★ 详情页按钮文案体现「再次点击=重新评估」",
          re.search(r"v-detail-ai[^>]*>\s*重新评估", (ROOT / "web" / "index.html").read_text(
              encoding="utf-8")) is not None)
    check("★ overwrite=true 路径下会提示「接受将覆盖」手动评分",
          "已有手动评分，接受将覆盖" in appjs and "item.had_rating" in appjs)
    # overwrite 只影响"给不给建议"，绝不影响"写不写"——落库仍只有 PATCH 一个入口
    check("★ 落库唯一入口仍是 PATCH /api/entries/{id}/ratings（AI 建议绝不写库）",
          re.search(r"/api/entries/' \+ item\.entry_id \+ '/ratings", appjs) is not None)


# ---------------------------------------------------------------------------
# ①c 跨来源频率：分篇数 + 分母 + 样本量提示
# ---------------------------------------------------------------------------


def patch_stats(saved, hits, corpus_total):
    """临时替换统计来源，用来造「单篇高频」「库很小」等场景。

    为什么不真的去建 5 篇文章：一个词要在一篇里出现 20 次，得拼出 20 个句子，
    而真正想验证的是**渲染出来的文本长什么样**（分篇数在不在、提示在不在）。
    造事实用 monkeypatch，验格式用 format_signals，两边职责分开。
    """
    original_hits = ai_ratings.articles.count_term_occurrences
    original_corpus = ai_ratings.count_corpus_articles
    saved.extend([original_hits, original_corpus])
    ai_ratings.articles.count_term_occurrences = lambda conn, term: dict(hits)
    ai_ratings.count_corpus_articles = lambda conn: corpus_total
    return lambda: _restore_stats(saved)


def _restore_stats(saved):
    original_hits, original_corpus = saved[0], saved[1]
    ai_ratings.articles.count_term_occurrences = original_hits
    ai_ratings.count_corpus_articles = original_corpus
    saved.clear()


def test_cross_source_frequency_denominator():
    """「单篇高频」场景：1 篇里 20 次，库里共 5 篇 —— 上下文必须有分篇数。"""
    section("[1c] 跨来源频率：单篇高频不算高频")
    conn = db.connect()
    saved = []
    try:
        term = "uncanny"
        entry = entries.create_entry(conn, {
            "type": "word", "term": term, "pos": "adj.",
            "meaning_zh": "奇怪的； uncanny 的",
        })
        conn.commit()
        item = entries.get_entry(conn, entry["id"])

        # 假统计：次数很高（20）但只命中 1 篇 —— 正是用户点名的 *uncanny* 情形
        patch_stats(saved, {"sentence_hits": 20, "article_hits": 1}, 5)
        sig = ai_ratings.collect_signals(conn, [item])[0]
        text = ai_ratings.format_signals([sig])

        check("统计里带上了分篇数（article_hits）", sig.get("article_hits") == 1, sig)
        check("统计里带上了分母（库里共几篇）", sig.get("corpus_articles") == 5, sig)
        check("★ 上下文给出「命中 1 篇 / 库里共 5 篇」",
              "跨来源频率=命中 1 篇 / 库里共 5 篇" in text, text)
        check("句子次数仍在，但降级为「仅作参考」",
              "句子命中 20 个，仅作参考" in text, text)
        check("★ 分篇数排在次数前面（单篇高频不会先被看到）",
              text.index("命中 1 篇") < text.index("句子命中 20 个"), text)
        check("分篇口径在提示词里被定义为「命中篇数占总篇数的比例」",
              "命中篇数占库里总篇数的比例" in ai_ratings._SUGGEST_SYSTEM)
    finally:
        _restore_stats(saved)
        conn.rollback()
        conn.close()


def test_small_corpus_note():
    """库里篇数很少时，统计里必须自带样本量提示。"""
    section("[1d] 库很小：统计自带样本量提示")
    conn = db.connect()
    saved = []
    try:
        item = entries.get_entry(conn, SEEDED_IDS["abundant"])

        # 3 篇 < SMALL_CORPUS_ARTICLES(5) → 弱信号，要提示
        patch_stats(saved, {"sentence_hits": 2, "article_hits": 3}, 3)
        sig = ai_ratings.collect_signals(conn, [item])[0]
        check("库小（3 篇）时统计里带样本提示", bool(sig.get("sample_note")), sig)
        check("★ 样本提示出现在发给模型的上下文里",
              "样本提示=" in ai_ratings.format_signals([sig]),
              ai_ratings.format_signals([sig]))
        check("样本提示说明了「样本量小 / 信号弱 / 别过度解读」",
              all(k in sig.get("sample_note", "")
                  for k in ("样本量小", "信号弱", "勿过度解读")), sig.get("sample_note"))

        # 8 篇 ≥ 5 → 不提示（提示只关于置信度，不是打分阈值）
        patch_stats(saved, {"sentence_hits": 2, "article_hits": 3}, 8)
        sig2 = ai_ratings.collect_signals(conn, [item])[0]
        check("库够大（8 篇）时不加样本提示（不是打分阈值）",
              "sample_note" not in sig2, sig2)

        # 边界：恰好等于阈值不提示
        patch_stats(saved, {"sentence_hits": 2, "article_hits": 3},
                    ai_ratings.SMALL_CORPUS_ARTICLES)
        check("库恰好等于阈值 %d 时不提示" % ai_ratings.SMALL_CORPUS_ARTICLES,
              "sample_note" not in ai_ratings.collect_signals(conn, [item])[0])
    finally:
        _restore_stats(saved)
        conn.close()


def test_empty_meaning_marked():
    """释义为空：上下文必须显式标注「用户未填写释义」，且不产生虚假统计。"""
    section("[1e] 空释义：明确标注，不硬编")
    conn = db.connect()
    try:
        # create_entry 要求 meaning_zh 非空，所以先用一个 seed 里没用过的词正常建
        # 再抹掉释义，模拟「用户当初没填」的真实状态
        entry = entries.create_entry(conn, {
            "type": "word", "term": "ubiquity", "pos": "n.", "meaning_zh": "占位的",
        })
        conn.execute("UPDATE entries SET meaning_zh = '' WHERE id = ?", (entry["id"],))
        conn.commit()

        item = entries.get_entry(conn, entry["id"])
        sig = ai_ratings.collect_signals(conn, [item])[0]
        text = ai_ratings.format_signals([sig])

        check("★ 上下文标注「用户未填写释义」",
              "中文释义=（用户未填写释义" in text, text)
        check("标注里说明了只能靠词形与语料判断",
              "只能依据词形与语料判断" in text, text)
        check("提示词要求模型在理由里说明这一点",
              "不要假装看到了释义" in ai_ratings._SUGGEST_SYSTEM)
        # 不硬编：空释义不该被替换成任何"看起来像释义"的占位内容
        check("空释义不产生任何伪造的释义文本",
              "占位的" not in text and "无处不在" not in text, text)
        # 音节仍然只对单词给，且是真实算出来的
        check("空释义不影响可算的事实（音节仍如实给出）",
              sig.get("syllables") == 4, sig.get("syllables"))
    finally:
        conn.close()


def test_corpus_denominator_real_db():
    """分母必须来自真实数据，且排除切不出句子的空文章。"""
    section("[1f] 分母来自真实库（且不为空文章稀释）")
    conn, _ids = seed()
    try:
        real = ai_ratings.count_corpus_articles(conn)
        check("分母等于库里有句子的文章数（seed 造了 2 篇）", real == 2, real)
        with_sentences = conn.execute(
            "SELECT COUNT(DISTINCT article_id) FROM article_sentences"
        ).fetchone()[0]
        check("分母 = COUNT(DISTINCT article_id) FROM article_sentences",
              real == with_sentences, (real, with_sentences))

        # 空文章（切不出句子）永远不可能被命中，计入分母会稀释比例 → 必须排除。
        # 走原生 SQL 而不是 create_article：后者已经用 _check_limits 挡住空内容，
        # 所以这只能代表历史遗留 / 边界数据，而分母对这种数据也必须稳。
        conn.execute(
            "INSERT INTO articles (title, content, content_zh, extra_info, added_at, updated_at)"
            " VALUES ('Legacy', ' ', '', '', '2024-01-01', '2024-01-01')"
        )
        conn.commit()
        legacy_rows = conn.execute(
            "SELECT COUNT(*) FROM articles a WHERE NOT EXISTS"
            " (SELECT 1 FROM article_sentences s WHERE s.article_id = a.id)"
        ).fetchone()[0]
        after = ai_ratings.count_corpus_articles(conn)
        total_rows = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        check("★ 无句子的文章（遗留数据）不计入分母",
              after == real and total_rows == real + 1, (after, total_rows, legacy_rows))
        check("分母确实按「有句子的文章数」而非文章表行数计",
              after < total_rows, (after, total_rows))
    finally:
        conn.rollback()
        conn.close()


def test_prompt_context_end_to_end():
    """端到端：真正发给模型的 user prompt 里必须带分篇数。"""
    section("[1g] 端到端：模型收到的上下文含分篇数")
    saved = []
    os.environ["OPENAI_API_KEY"] = "test-fake-key"
    try:
        fake = install_fake(saved, echo_ids(99))
        conn = db.connect()
        unrated = conn.execute(
            "SELECT id FROM entries WHERE importance IS NULL AND usefulness IS NULL"
        ).fetchall()
        ids = [r["id"] for r in unrated][:2]
        conn.close()

        client = TestClient(app)
        r = client.post("/api/entries/ratings/suggest", json={"entry_ids": ids})
        check("接口 200", r.status_code == 200, r.status_code)
        check("确有发出 AI 调用", len(fake.calls) == 1, len(fake.calls))

        sent = fake.calls[0]
        check("★ 模型收到的上下文里有「命中 N 篇 / 库里共 M 篇」",
              re.search(r"跨来源频率=命中 \d+ 篇 / 库里共 \d+ 篇", sent) is not None, sent[:300])
        check("★ 模型收到的上下文里有中文释义",
              "中文释义=" in sent, sent[:300])
        check("user prompt 明确要求结合释义与语料",
              "释义本身也是判断依据" in sent, sent[:120])
        check("user prompt 指向同一套判定口径（提示词已被换掉）",
              "依据下面的统计事实" not in sent, sent[:120])
    finally:
        restore_ai(saved)
        os.environ.pop("OPENAI_API_KEY", None)


def test_syllables():
    section("[2] 音节估算")
    cases = [
        ("abundant", 3), ("make", 1), ("table", 2), ("apple", 2),
        ("the", 1), ("beautiful", 3), ("take", 1),
    ]
    for word, want in cases:
        got = ai_ratings.count_syllables(word)
        check("count_syllables(%s) == %d" % (word, want), got == want, got)
    for phrase in ("take advantage of", "as a matter of fact", ""):
        check("短语 %r 返回 None（不算）" % phrase,
              ai_ratings.count_syllables(phrase) is None)


# ---------------------------------------------------------------------------
# ② 解析与规整
# ---------------------------------------------------------------------------


def test_parsing():
    section("[3] 模型返回解析与分值规整")

    # 正常 JSON
    parsed = ai_ratings.parse_suggestions(
        ok_json([{"entry_id": 1, "importance": 4, "usefulness": 3.5,
                  "reason": {"importance": "外刊 3 次", "usefulness": "偏书面"}}]),
        {1})
    check("正常 JSON 可解析", parsed.get(1, {}).get("importance") == 4.0)

    # markdown 代码块包裹
    parsed = ai_ratings.parse_suggestions(
        "好的，这是结果：\n```json\n" + ok_json([
            {"entry_id": 2, "importance": 3, "usefulness": 3,
             "reason": {"importance": "a", "usefulness": "b"}}]) + "\n```\n希望有用",
        {2})
    check("包 markdown 代码块也能解析", parsed.get(2, {}).get("usefulness") == 3.0, parsed)

    # 裸数组
    parsed = ai_ratings.parse_suggestions(
        json.dumps([{"entry_id": 3, "importance": 2, "usefulness": 2.5,
                     "reason": {"importance": "x", "usefulness": "y"}}]),
        {3})
    check("裸数组也能解析", parsed.get(3, {}).get("importance") == 2.0)

    # 非法分值被规整
    for raw, want in [(3.3, 3.5), (3.2, 3.0), (7, 5.0), (-1, 0.0), ("四分半", 3.0)]:
        got = ai_ratings._coerce_score(raw)
        check("_coerce_score(%r) -> %s" % (raw, want), got == want, got)
    check("规整后一定是 0.5 的倍数",
          all(abs(ai_ratings._coerce_score(x) * 2 - round(ai_ratings._coerce_score(x) * 2)) < 1e-9
              for x in (3.3, 7, -1, 0.1, 4.9)))

    # 幻觉 id 被丢弃
    parsed = ai_ratings.parse_suggestions(
        ok_json([{"entry_id": 999, "importance": 5, "usefulness": 5,
                  "reason": {"importance": "", "usefulness": ""}}]),
        {1})
    check("模型幻觉的 entry_id 被丢弃", parsed == {}, parsed)

    # reason 给成字符串
    parsed = ai_ratings.parse_suggestions(
        ok_json([{"entry_id": 1, "importance": 3, "usefulness": 3,
                  "reason": "通用词汇"}]), {1})
    check("reason 是字符串时两维共用",
          parsed[1]["reason"]["importance"] == "通用词汇"
          and parsed[1]["reason"]["usefulness"] == "通用词汇")

    # 超长理由被截断
    parsed = ai_ratings.parse_suggestions(
        ok_json([{"entry_id": 1, "importance": 3, "usefulness": 3,
                  "reason": {"importance": "长" * 300, "usefulness": ""}}]), {1})
    check("超长理由被截断到上限内",
          len(parsed[1]["reason"]["importance"]) <= ai_ratings.MAX_REASON_LEN + 1,
          len(parsed[1]["reason"]["importance"]))

    # 非法 JSON 抛 AIError
    try:
        ai_ratings.parse_suggestions("完全不是 JSON", {1})
        check("非法 JSON 抛 AIError", False, "没有抛异常")
    except ai.AIError:
        check("非法 JSON 抛 AIError", True)


def test_extract_json_repair():
    section("[3b] extract_json 保守修复：字符串值内的裸英文双引号（用户实测报障形态）")

    # ★ 负向基线：这个串**不修复就解析不了**——证明后面的修复真的干了活，
    #   而不是「怎么都能过」的恒真判据。
    broken = ('{"items":[{"entry_id":1,"importance":4.5,"usefulness":4.0,'
              '"reason":{"importance":"可替换 say "hi" 这类平庸表达","usefulness":"论证连接手段"}}]}')
    try:
        json.loads(broken)
        check("基线：坏 JSON 本来就解析失败", False, "竟直接解析成功，判据失效")
    except json.JSONDecodeError:
        check("基线：坏 JSON 本来就解析失败", True)

    fixed = ai.extract_json(broken)
    check("值内裸引号被转义后可解析，内容原样保留",
          fixed["items"][0]["reason"]["importance"] == '可替换 say "hi" 这类平庸表达', fixed)
    check("同批其它字段不受修复影响",
          fixed["items"][0]["importance"] == 4.5 and fixed["items"][0]["usefulness"] == 4.0, fixed)

    # 键名里的裸引号也能修（收尾判定看后继字符是不是 , } ] : ）
    keyed = '{"say "hi" now": 2, "note": "命中 3/9 篇"}'
    got = ai.extract_json(keyed)
    check("键名内的裸引号同样被修复", got.get("say \"hi\" now") == 2, got)

    # 合法 JSON 原样通过（修复路径不改变已有正确输出）
    check("合法 JSON 原样解析", ai.extract_json('{"a": "命中 4/12 篇"}') == {"a": "命中 4/12 篇"})

    # 修不好的（缺逗号）仍抛 AIError，且消息带出错位置附近原文（诊断用）
    hopeless = '{"a": "x" "b": 2}'
    try:
        ai.extract_json(hopeless)
        check("修不好的 JSON 仍抛 AIError", False, "没有抛异常")
    except ai.AIError as exc:
        check("修不好的 JSON 仍抛 AIError", "出错位置附近原文" in str(exc), str(exc)[:140])

    # ★ 结构性畸形：模型漏写词条自己的收尾 }（第二轮实测形态：
    #   错误落在 "}] 的 ] 上、周围文本完全合法）⇒ 逐条打捞，救回完好条目
    broken_brace = ('{"items":['
                    '{"entry_id":1,"importance":4.5,"usefulness":4.0,'
                    '"reason":{"importance":"命中1/20篇","usefulness":"环境话题可用"}},'
                    '{"entry_id":2,"importance":2.0,"usefulness":2.0,'
                    '"reason":{"importance":"c","usefulness":"d"}]}')
    try:
        json.loads(broken_brace)
        check("基线：漏括号的 JSON 本来就解析失败", False, "竟直接解析成功")
    except json.JSONDecodeError:
        check("基线：漏括号的 JSON 本来就解析失败", True)
    got = ai_ratings.parse_suggestions(broken_brace, {1, 2})
    check("漏收尾括号：完好条目被逐条打捞回来",
          got.get(1, {}).get("importance") == 4.5, got)
    check("打捞不出的条目不伪造数据（结果里没有 2 号）",
          2 not in got, sorted(got))

    # ★ 截断补尾：输出被 max_tokens 截在条目边界 ⇒ 补齐缺失的 } ] 恢复完整条目
    truncated = ('{"items":[{"entry_id":3,"importance":3.5,"usefulness":2.5,'
                 '"reason":{"importance":"x","usefulness":"y"}}')
    try:
        json.loads(truncated)
        check("基线：截断 JSON 本来就解析失败", False, "竟直接解析成功")
    except json.JSONDecodeError:
        check("基线：截断 JSON 本来就解析失败", True)
    got = ai_ratings.parse_suggestions(truncated, {3})
    check("截断 JSON 补尾后恢复完整条目",
          got.get(3, {}).get("usefulness") == 2.5, got)

    # ★ 解析失败时原始响应落盘 ai_debug/（诊断留证，只在本机数据目录）
    ai_ratings._dump_failed_response("raw-for-debug")
    dumps = sorted((TMP / "ai_debug").glob("suggest_*.txt"))
    check("解析失败原始响应落盘 ai_debug/",
          bool(dumps) and dumps[-1].read_text(encoding="utf-8") == "raw-for-debug",
          [p.name for p in dumps])


# ---------------------------------------------------------------------------
# ③ 无 API key
# ---------------------------------------------------------------------------


def test_no_api_key():
    section("[4] 未配置 API key：明确报错，不静默降级")
    saved = []
    saved_settings = ai.load_settings

    # 显式造出"没有密钥"的环境：环境变量清空 + 设置里 api_key 为空
    old_env = os.environ.pop("OPENAI_API_KEY", None)
    ai.load_settings = lambda: {"ai": {"api_key": "", "base_url": "x", "model": "m", "timeout": 1}}
    db_conn = db.connect()
    try:
        try:
            ai_ratings.suggest(db_conn, [1])
            check("无密钥时抛 AINotConfiguredError", False, "没有抛异常")
        except ai.AINotConfiguredError as exc:
            check("无密钥时抛 AINotConfiguredError", True)
            check("错误消息给出可操作的修复路径",
                  "设置" in str(exc) or "OPENAI_API_KEY" in str(exc), str(exc))
        check("AINotConfiguredError 是 AIError 子类（能被统一处理器捕获）",
              issubclass(ai.AINotConfiguredError, ai.AIError))
    finally:
        db_conn.close()
        ai.load_settings = saved_settings
        if old_env is not None:
            os.environ["OPENAI_API_KEY"] = old_env
        restore_ai(saved)

    # HTTP 层：应为 400（本地配置缺失），而不是 502（上游故障，会误导用户）
    saved2 = []
    fake = install_fake(saved2, ok_json([]))
    old_env = os.environ.pop("OPENAI_API_KEY", None)
    ai.load_settings = lambda: {"ai": {"api_key": "", "base_url": "x", "model": "m", "timeout": 1}}
    try:
        client = TestClient(app)
        r = client.post("/api/entries/ratings/suggest", json={"entry_ids": [1]})
        check("HTTP 层无密钥返回 400 而非 502", r.status_code == 400, r.status_code)
        check("400 响应里有可读的中文原因",
              "API" in r.json().get("detail", "") or "密钥" in r.json().get("detail", ""),
              r.json())
        check("无密钥时**没有**发出任何 AI 调用", len(fake.calls) == 0, len(fake.calls))
    finally:
        ai.load_settings = saved_settings
        if old_env is not None:
            os.environ["OPENAI_API_KEY"] = old_env
        restore_ai(saved2)


# ---------------------------------------------------------------------------
# ④ 端到端：overwrite 语义 + 绝不写库 + 单条失败隔离
# ---------------------------------------------------------------------------


def test_endpoint_overwrite_semantics():
    section("[5] overwrite 语义：跳过已评分，但任何情况都不写库")
    saved = []
    os.environ["OPENAI_API_KEY"] = "test-fake-key"  # 绕过无密钥分支
    try:
        # 用 echo_ids 按 prompt 里真实的 entry_id 回，而不是写死 id：
        # 写死的 id 一旦和 seed 里的实际 id 对不上，会被 parse_suggestions
        # 当成"模型幻觉"丢掉，让本用例变成假失败。
        fake = install_fake(saved, echo_ids(99, imp=5, use=5))
        client = TestClient(app)
        conn = db.connect()
        before = snapshot_ratings(conn)
        conn.close()

        ids = json.loads(json.dumps(SEEDED_IDS))

        # overwrite=False：已评分的 4（两维都有）与 5（只有一维）都应被跳过。
        # 特意用 ample(2) / ubiquitous(3) 这两个**未评分**的词去测"能拿到建议"，
        # 因为 abundant(1) 在 seed 里就带了 4.5/3.0，按 overwrite=False 的
        # 语义本来就该被跳过 —— 用它测"能拿到建议"是测试自己写错了。
        r = client.post("/api/entries/ratings/suggest",
                        json={"entry_ids": [ids["ample"], ids["ubiquitous"],
                                           ids["ephemeral"], ids["candid"]]})
        check("overwrite=False 接口 200", r.status_code == 200, r.status_code)
        data = r.json()
        by_id = {i["entry_id"]: i for i in data["items"]}
        check("未评分词条拿到建议",
              by_id[ids["ample"]]["importance"] == 5.0 and by_id[ids["ample"]]["skipped"] is False,
              by_id[ids["ample"]])
        check("两维都已评分的词条被跳过", by_id[ids["ephemeral"]]["skipped"] is True)
        check("只评了一维的词条也被跳过（任一维有值即视为已表态）",
              by_id[ids["candid"]]["skipped"] is True)
        check("跳过的条目带 skip_reason 说明原因",
              "不会覆盖" in (by_id[ids["ephemeral"]].get("skip_reason") or ""),
              by_id[ids["ephemeral"]].get("skip_reason"))
        check("跳过的条目不带建议分值（不硬塞 0）",
              by_id[ids["ephemeral"]]["importance"] is None)
        check("skipped 计数正确", data["skipped"] == 2, data["skipped"])
        # 4 条里只有 2 条未评分 → 假 AI 只该被调 1 次（且那 1 次是给 2 条的）
        check("overwrite=False 时已评分词条**没被**送去 AI 调用（不白花钱）",
              len(fake.calls) == 1, len(fake.calls))
        check("被跳过的词条不出现在 AI prompt 里",
              "ephemeral" not in fake.calls[0] and "candid" not in fake.calls[0],
              fake.calls[0][:200])

        # overwrite=True：已评分的也会拿到建议，但 had_rating 标记为真
        r = client.post("/api/entries/ratings/suggest",
                        json={"entry_ids": [ids["ephemeral"]], "overwrite": True})
        data = r.json()
        item = data["items"][0]
        check("overwrite=True 时已评分词条也拿到建议", item["skipped"] is False and item["importance"] == 5.0)
        check("overwrite=True 时标记 had_rating（前端据此提示会覆盖）",
              item["had_rating"] is True, item)

        # 关键：两次调用后数据库评分列必须**一字未动**
        conn = db.connect()
        after = snapshot_ratings(conn)
        conn.close()
        check("★ overwrite=True 也**没有写库**（AI 建议绝不自动落库）", before == after,
              "before=%s after=%s" % (before, after))
    finally:
        restore_ai(saved)
        os.environ.pop("OPENAI_API_KEY", None)


def test_single_failure_isolation():
    section("[6] 批量中单条失败不影响整批")
    saved = []
    os.environ["OPENAI_API_KEY"] = "test-fake-key"
    try:
        # 第一批 8 条里，模型只给了 2 条（其余 6 条 = 漏给），第二批整批报错
        fake = install_fake(saved, [
            echo_ids(2),
            ai.AIError("AI 请求失败：模拟整批网络错误"),
        ])
        conn = db.connect()
        # 造 10 个未评分词条，确保会分成 2 批（批大小 8）
        made = []
        for i in range(10):
            made.append(entries.create_entry(conn, {
                "type": "word", "term": "bulkword%d" % i, "pos": "n.",
                "meaning_zh": "批量测试 %d" % i,
            })["id"])
        conn.commit()
        conn.close()

        client = TestClient(app)
        r = client.post("/api/entries/ratings/suggest", json={"entry_ids": made})
        check("整批有单条失败时接口仍返回 200（不是 500）", r.status_code == 200, r.status_code)
        data = r.json()
        check("返回条数与请求一致", len(data["items"]) == 10, len(data["items"]))

        got = [i for i in data["items"] if not i["error"]]
        errs = [i for i in data["items"] if i["error"]]
        check("第一批成功的 2 条照常返回建议", len(got) == 2, len(got))
        check("模型漏给的 6 条各自带 error（不牵连成功项）", len(errs) == 8, len(errs))
        check("漏给的条目 error 说清了原因",
              any("未返回该词条" in i["error"] for i in errs),
              [i["error"] for i in errs][:2])
        check("整批报错的条目 error 里有原因",
              all("模拟整批网络错误" in i["error"] for i in errs if i["entry_id"] in made[8:]),
              [i["error"] for i in errs if i["entry_id"] in made[8:]])
        check("分批调用确实分了 2 批（10 条 / 每批 8 条）", len(fake.calls) == 2, len(fake.calls))
        check("第一批 prompt 里只含前 8 条", len(re.findall(r"entry_id=", fake.calls[0])) == 8,
              len(re.findall(r"entry_id=", fake.calls[0])))
    finally:
        restore_ai(saved)
        os.environ.pop("OPENAI_API_KEY", None)


def test_unrated_endpoint():
    section("[7] 批量评估未评分词条端点")
    saved = []
    os.environ["OPENAI_API_KEY"] = "test-fake-key"
    try:
        # 固定返回 id 1/4/5 的响应在这个用例里没用（请求的是 2/3/4/5），
        # 改用 echo_ids：按 prompt 里实际的 entry_id 回，避免"对不上 id"
        # 被当成模型幻觉而让本用例假失败。
        fake = install_fake(saved, echo_ids(99))
        client = TestClient(app)
        conn = db.connect()
        before = snapshot_ratings(conn)
        unrated_before = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE importance IS NULL AND usefulness IS NULL"
        ).fetchone()[0]
        conn.close()

        r = client.post("/api/entries/ratings/suggest-unrated", json={"limit": 2})
        check("未评分端点 200", r.status_code == 200, r.status_code)
        data = r.json()
        check("返回 2 条", len(data["items"]) == 2, len(data["items"]))
        check("只命中未评分的词条（不含已评分的 abundant/ephemeral/candid）",
              all(i["entry_id"] not in (SEEDED_IDS["abundant"], SEEDED_IDS["ephemeral"],
                                        SEEDED_IDS["candid"]) for i in data["items"]))

        conn = db.connect()
        check("★ 未评分端点也**不写库**", snapshot_ratings(conn) == before)
        conn.close()

        # offset 分页：第二页应返回后续的未评分词条，不与第一页重复
        r2 = client.post("/api/entries/ratings/suggest-unrated", json={"limit": 2, "offset": 2})
        ids2 = [i["entry_id"] for i in r2.json()["items"]]
        ids1 = [i["entry_id"] for i in data["items"]]
        check("offset 分页不重复（前端分批+中断依赖这个）",
              not (set(ids1) & set(ids2)), "page1=%s page2=%s" % (ids1, ids2))

        # limit 超过 pydantic 的 le 约束 → 422（比 400 更准确：这是入参校验层拦的）
        r3 = client.post("/api/entries/ratings/suggest-unrated",
                         json={"limit": ai_ratings.MAX_IDS_PER_REQUEST + 1})
        check("limit 越界被入参校验拦下（422）", r3.status_code == 422, r3.status_code)
        check("越界时不会发 AI 调用（本用例累计 2 次：首页 + offset 页）",
              len(fake.calls) == 2, len(fake.calls))
    finally:
        restore_ai(saved)
        os.environ.pop("OPENAI_API_KEY", None)


def test_limits_and_bad_ids():
    section("[8] 入参边界")
    saved = []
    os.environ["OPENAI_API_KEY"] = "test-fake-key"
    try:
        install_fake(saved, echo_ids(99))
        client = TestClient(app)
        r = client.post("/api/entries/ratings/suggest",
                        json={"entry_ids": list(range(1, ai_ratings.MAX_IDS_PER_REQUEST + 2))})
        check("超过单次上限返回 400（而不是悄悄截断）", r.status_code == 400, r.status_code)

        r = client.post("/api/entries/ratings/suggest", json={"entry_ids": []})
        check("空列表被 pydantic 拦下（422）", r.status_code == 422, r.status_code)

        # 不存在的 id：记 error 而不是整体 500
        r = client.post("/api/entries/ratings/suggest", json={"entry_ids": [999999]})
        check("不存在的 id 返回 200 且带 error",
              r.status_code == 200 and r.json()["items"][0]["error"], r.json())

        # 重复 id 只处理一次
        conn = db.connect()
        one = SEEDED_IDS["ample"]
        conn.close()
        fake = ai.chat
        r = client.post("/api/entries/ratings/suggest", json={"entry_ids": [one, one, one]})
        check("重复 id 只返回一条", len(r.json()["items"]) == 1, len(r.json()["items"]))
    finally:
        restore_ai(saved)
        os.environ.pop("OPENAI_API_KEY", None)


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 70)
    print("需求 4 · AI 辅助评分 后端测试")
    print("测试目录：%s" % TMP)
    print("=" * 70)

    # 造数据一次，各用例共用
    _conn, SEEDED_IDS = seed()
    _conn.close()

    test_signals_and_prompt()
    test_criteria_wording_in_prompt()
    test_cross_source_frequency_denominator()
    test_small_corpus_note()
    test_empty_meaning_marked()
    test_corpus_denominator_real_db()
    test_prompt_context_end_to_end()
    test_syllables()
    test_parsing()
    test_extract_json_repair()
    test_no_api_key()
    test_endpoint_overwrite_semantics()
    test_single_failure_isolation()
    test_unrated_endpoint()
    test_limits_and_bad_ids()

    print("\n" + "=" * 70)
    print("结果：%d 通过 / %d 失败" % (PASSED, FAILED))
    print("=" * 70)
    sys.exit(1 if FAILED else 0)