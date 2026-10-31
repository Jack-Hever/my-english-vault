"""AI 辅助评分：为「重要性 / 实用性」两维生成**建议**分值（需求 4）。

=========================== 硬约束（改代码前必读） ===========================

1. **本模块绝不写库。**
   `suggest()` 及本文件所有函数都只读 entries / articles，返回建议值。
   落库只有一个入口：用户在确认面板点「接受」后，前端调
   `PATCH /api/entries/{id}/ratings` → `entries.update_ratings()`（窄接口）。
   任何"顺手写一下"的改动都会让用户失去否决权，所以这里没有 conn.execute UPDATE。

2. **`overwrite` 只影响"给不给建议"，永远不影响"写不写"。**
   - overwrite=False（默认）：已有手动评分的词条**跳过**，返回 skipped 标记，
     不消耗 AI 调用。这是"AI 不覆盖用户评分"这条需求的落点。
   - overwrite=True：同样只**返回**建议（可能与用户已有的分不同），由前端在
     确认面板上提示"该词条已有手动评分，接受将覆盖"，用户点接受才写。
   - 两种情况下本模块都不碰数据库。overwrite 是"要不要评估"的开关，
     不是"要不要落库"的开关——把它实现成自动落库会让"AI 不覆盖手动评分"
     变成一句空话。

3. **没有 API key 时明确报错，不静默降级。**
   `ai.is_mock_mode()` 为真时抛 `ai.AINotConfiguredError`。
   翻译 / 点评可以 mock（占位文本不影响正确性），评分**不行**：
   编造的分数会被用户当真实建议采纳并写进词库，污染是永久的。
   宁可报错让用户去设置里填密钥。
==========================================================================
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from . import ai, articles, config, entries

# ---------------------------------------------------------------------------
# 分批参数
# ---------------------------------------------------------------------------

#: 单次 AI 调用里打包多少个词条。
#:
#: 上限 10 的理由：每条都要带 term / 释义 / 外刊命中 / 两维理由，
#: 10 条的 prompt 约 800~1200 token，输出约 600 token，一次往返就能拿到，
#: 既省时间又不会因为输出过长被 max_tokens 截断（截断 = JSON 解析失败 = 全批报废）。
#: 调大会显著提高"整批因超长而失败"的概率，调小则往返次数线性上升。
BATCH_SIZE = 8

#: 单次请求最多接受多少个 entry_ids（分批后的批次数由此决定）。
#:
#: 这是给前端的一个**自律上限**，防手滑一次 POST 上万个 id 把服务打满。
#: 真正的保护是分批循环——超限直接 400，而不是悄悄截断。
MAX_IDS_PER_REQUEST = 60

#: 理由字段的长度上限。模型偶尔会写成整段散文，这里截断并提示，
#: 免得前端的小字区域被撑爆（也顺手挡掉"输出里塞指令"的越狱尝试）。
MAX_REASON_LEN = 80

# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------
#
# 「重要性 / 实用性」的判定口径全部写在这段 _SUGGEST_SYSTEM 里，是唯一的口径来源。
# 改口径 = 改这段提示词；不要把判定标准散落到代码分支里去，那样口径就会
# 分裂成"提示词说一套、统计字段暗示另一套"，模型只能猜。
# 四条口径的来由（设计约束，勿擅自改口径）：
#   1. 重要性 = **雅思频率** × **掌握复杂度** 的综合考量；**越难记但越常见则分越高**；
#      如果是某些主题文章里的常见词，可以多加分。
#   2. 实用性 = **雅思口语 / 雅思写作 / 欧美生活（外网网络使用）/ 职场英语**
#      四个场景的综合考量，其中**雅思写作占比最高**，写作里的**点睛之笔**更是高分。
#   3. 评分依据 = 用户写的中文释义 + 外部语料。
#   4. 跨文章频率 ≠ 单篇文章内高频（频率看的是"普遍出现"，不是"某篇里出现得多"）。
# 每条都配了反例，因为"不要什么"比"要什么"更难被模型执行，光说定义它会按直觉走偏。
# 反例已按用户最新反馈更新：原先用 uncanny（单篇高频）/ That's in part because
# （易懂低价值），现在统一换成 **abundant vs analogous to** 这一对——
# 设计约束是「abundant 怎么会比 analogous to 高呢，前者复杂度较低，后者复杂度较高」。
# 这一对同时能压住"两个维度各自打分再取平均"的退化写法（那样 abundant 会拿高分）。

_SUGGEST_SYSTEM = """你是一位英语词汇教研员，正在为一位中文母语者的个人词汇库评估词条的学习价值。
用户会给你若干词条，以及每个词条的中文释义与可核查的语料统计，请为**每个词条**给出两个维度的建议分值。

判定依据（两者都要用，不要只看统计）：
1. **用户写的中文释义**——这是用户自己认定的意思，它决定该词在用户的语境里
   承担什么角色（雅思高频 / 雅思写作里的点睛之笔 / 地道口语 / 职场术语 / 文学化用词）。
   **释义内容本身就是重要的判断依据**，不要只把它当成一个标签。
2. **外部语料**——跨来源的命中情况（命中 N 篇 / 库里共 M 篇）以及它在雅思语料里的
   常见度。语料是**佐证**而不是唯一依据：库里篇数少时这个信号很弱，
   此时应更多依据释义与语言学知识判断。

- importance（重要性）：这个词/短语在**雅思**中的重要性。
  由**雅思频率**与**掌握复杂度**共同决定，**请分别考虑这两个维度后再综合**，
  但**不要各打一个分再取平均**——这两维**不是等价的两维**，必须按下方的
  **不对称加权**规则给分：
  · **雅思频率**：在雅思阅读 / 听力语料里是否**普遍**出现。
    **跨文章频率 ≠ 单篇文章内的高频**：只在一篇文章里反复出现、在其它文章里
    几乎见不到的词（如 uncanny 在某篇里出现 20 次），那只是语料偶然，**不算重要**。
    判断"普遍"看的是**命中篇数占库里总篇数的比例**；句子出现次数只作参考，
    次数多可能只是同一篇里重复。
  · **掌握复杂度**：掌握这个词的难度。用法 / 搭配不直观的表达（如 in lieu of、
    notwithstanding）复杂度高，更值得优先掌握；人人都会、说完就忘的词复杂度极低。
  · **不对称加权（必须按这个方向给分，写错了就是本条口径没执行）**：
    - 复杂度高**且**频率高 → 给到该档的**上沿**。**越难记又越常见，分越高**。
    - 频率高但复杂度低（人人都会、说完就忘）→ 给**中低分**，不许因为高频就顶格。
    - 复杂度高但频率低（罕见的难词）→ 给**中分左右**，不许因为"难"就顶格。
  · **主题文章常见词可加分**：若释义与语料显示它属于某类主题文章
    （环境 / 经济 / 科技 / 健康 / 教育 等）的常见词，可在这个基础上**上调**分值
    ——是"可上调"不是"必须上调"，并且要在理由里说明是因为属于哪个主题的常见词。
  ★ **判断前先比较这一对**：**abundant** 高频但几乎人人都会、复杂度低，
    importance **不应高**；**analogous to**（是短语不是单词）复杂度高，
    且在雅思写作 / 阅读的论证里常见，importance **应高于 abundant**。

- usefulness（实用性）：这个词/短语在下面**四个场景**里的实用价值，
  其中**雅思写作占比最高**，是 usefulness 的**第一主导场景**，其余三个场景次之：
  ① **雅思口语**：P3 论证、描述人与经历时的自然表达；
  ② **雅思写作（占比最高）**：Task 1 数据描述、Task 2 论证里的学术书面表达；
  ③ **欧美生活 / 外网网络使用**：母语者在社交媒体、技术论坛、日常网络交流里的
     实际用法（含俚语与网络用语）；
  ④ **职场英语**：邮件、会议、汇报、谈判里的专业表达。
  ★ **点睛之笔给高分**：能在雅思写作里替换掉一个平庸表达、让论证显得地道有力的
  地道说法 / 精准搭配，usefulness 应给**高分**。
  ★ 反过来给**低分**：正确但用上去也增加不了什么气色的冗余表达，
  以及容易误用的生僻词。
  ★ 反例：**abundant** 这类通用但对写作无出彩作用的词，usefulness **不该高**；
    **analogous to** 这类论证连接手段，usefulness **应更高**。

分值要求（必须严格遵守）：
1. 每维返回 0 到 5 之间，且**必须是 0.5 的整数倍**（0 / 0.5 / 1 / 1.5 … 5）。
   不要返回 3.3、4.2 这类值。
2. 两维可以不同：难记但用不上的生僻术语（只管"能被记住"不管"用得上"）
   importance 高、usefulness 低；反过来，能让雅思写作出彩但并不难记的
   地道表达，importance 偏低、usefulness 高。
3. 每维都要给**一句**简体中文理由（不超过 30 字），必须**引用统计依据里的具体数字**，
   **优先引用命中篇数 / 库里总篇数**（如"命中 4/12 篇，雅思论证常见"），
   不要只引用句子次数（如"出现 12 次"，那可能全在一篇文章里），更不许写
   "该词很重要"这种空话。
4. 数据缺失时如实说明，**绝不硬编数字**：
   - 外刊库为空、或本地语料未命中：写明"本地语料未命中"，并说明你是依据
     释义与语言学知识判断的；
   - 释义标注"用户未填写释义"：你只能依据词形、搭配与语料判断，
     理由里要明确说明这一点，不要假装看到了释义；
   - 统计里带"样本提示"时，说明库内篇数很少、命中篇数信号弱，
     不要把一两篇的命中当成"普遍高频"。

输出契约（违反任何一条都会让 JSON 解析失败、整批作废，务必遵守）：
1. 只输出严格 JSON，不要任何解释文字、不要 markdown 代码块。
2. **字符串值内部禁止出现未转义的英文双引号 "**——理由、例句里需要引用时，
   一律用中文引号「」或单引号，不要写 "..."。
3. 理由只写你的判断与数字依据，**不要整句照抄语料原文**（原文可能带引号，
   既费 token 又容易破坏 JSON）。

只输出严格 JSON。格式：

{"items":[{"entry_id":1,"importance":4.5,"usefulness":4.0,
"reason":{"importance":"命中 4/12 篇，雅思论证常见","usefulness":"可替换平庸论证表达"}}]}"""

# ---------------------------------------------------------------------------
# 统计依据采集
# ---------------------------------------------------------------------------

#: 字母/数字/连字符组成的单词（音节数只对这种有意义）
_WORD_RE = re.compile(r"^[a-z]+(?:[-'][a-z]+)*$")

#: 外刊库少于这么多篇时，命中篇数就是**弱信号**，要在给模型的上下文里说明。
#:
#: 5 这个数的来由：分篇比例的分辨率是 1/M。M=5 时"命中 1 篇"等于 20 个百分点，
#: 一次命中就能把结论从"常见"翻成"罕见"，模型据此打分是在拟合噪声；
#: M>=10 时每篇才 10 个点，比例才勉强能支撑"普遍 / 偶见"的判断。
#:
#: 刻意不做的事：
#: - **不因此下调或上调分值**，也不把它做成一个阈值分支。这是**数据质量提示**，
#:   不是新口径——判定标准仍由提示词里那两条定义决定，这里只负责告诉模型
#:   "你手上这个比例的误差有多大"，免得它把 1/5 当成 1/500 那样笃定。
#: - 不因为库小就干脆不给频率：命中 1/2 篇仍然是事实，藏起来模型就只能靠猜，
#:   而"给出事实 + 说明置信度低"比"不给事实"更可用。
SMALL_CORPUS_ARTICLES = 5


def count_corpus_articles(conn) -> int:
    """数外刊库里有几篇文章 —— 这是"跨来源频率"的分母。

    为什么分母在这里现算，而不是让 articles.count_term_occurrences 一起返回：
    - articles 层是**通用检索层**，它的口径要跟选句（pick_sentences_for_term）
      严格对齐，多塞一个"库总数"进去只会让两边的职责混在一起；
    - 但"命中 N 篇 / 共 M 篇"这个比值是**评分口径特有的**——只有本模块需要
      "普遍不普遍"这个视角（选句只关心"有没有命中"）。口径属于谁，数据就归谁。

    刻意不做的事：
    - 只 COUNT(DISTINCT article_id) 而不是 COUNT(*) FROM articles：
      **空文章（没切出任何句子）永远不可能被命中**，算进分母只会稀释比例，
      让"库里其实只有 2 篇能检索"被显示成"5 篇里命中 1 篇"。宁可给一个
      口径干净的数字，也不要一个看起来更权威的错数字。
    - 不按字数 / 句子数给文章设"够不够大"的准入门槛。分母是"用户能检索到的
      全部语料"，不是"我们认为合格的语料"；一旦掺入主观筛选，同一个词在
      不同库里会得到不同比例，而用户无法解释这个差异从哪来。
    """
    row = conn.execute(
        "SELECT COUNT(DISTINCT article_id) FROM article_sentences"
    ).fetchone()
    return int(row[0]) if row else 0


def count_syllables(term: str) -> int | None:
    """估算英文单词音节数；**算不出来时返回 None，而不是硬凑一个值**。

    刻意不做的事：
    - 短语（take advantage of）不算——短语没有"音节"这个概念，
      硬按空格加起来会给模型一个看起来精确、实则没意义的数字；
      比"没给"更糟，模型会当成事实用。
    - 不做词形还原（abundant → 4 个音节是对的，abundance → 5 也对，
      但 ruled → 1 这种只能靠词典）。所以明确标注这是"估算"。

    规则（英语教学常用的元音组近似法）：
    统计连续元音串个数，减去词尾不发音的 e（但 -le 结尾要保留：table / little）。
    """
    word = (term or "").strip().lower()
    if not _WORD_RE.match(word):
        return None
    groups = re.findall(r"[aeiouy]+", word)
    if not groups:
        return None
    count = len(groups)
    # 词尾哑 e：make → 1 但元音组数是 2。-le 例外（table 的 le 要发音）
    if word.endswith("e") and not word.endswith(("le", "ee")) and count > 1:
        count -= 1
    return max(1, count)


def collect_signals(conn, items: list[dict]) -> list[dict]:
    """为每个词条采集可核查的事实依据。

    只给**算得出来**的项：跨文章命中比例（含分母）、词长、音节（仅单词）。
    算不出来的键**直接不出现**在结果里，而不是填空串 / 0 ——
    空串和 0 在模型眼里都是"事实"，会污染判断。

    跨来源频率的呈现口径：**命中篇数 / 库里总篇数**。
    句子出现次数也带，但它只是辅助——一篇文章里重复 20 次不代表这个词常见，
    跨 5 篇各出现 1 次才代表它"普遍"。分篇数由 articles.count_term_occurrences
    提供（它已经在数 article_hits），分母由 count_corpus_articles 在这里补上。

    items 需含 id / term / type / pos / meaning_zh（entries.get_entry 的产物）。
    """
    corpus_articles = count_corpus_articles(conn)
    # 库太小 → 分篇比例的分辨率不够，是**数据质量提示**而非打分口径（见常量注释）
    sample_note = (
        "库内仅 %d 篇文章，样本量小，命中篇数的信号弱，请勿过度解读" % corpus_articles
        if 0 < corpus_articles < SMALL_CORPUS_ARTICLES
        else ""
    )

    signals: list[dict] = []
    for item in items:
        term = (item.get("term") or "").strip()
        meaning_zh = (item.get("meaning_zh") or "").strip()
        sig: dict = {
            "entry_id": int(item["id"]),
            "term": term,
            "type": item.get("type") or "",
            "pos": (item.get("pos") or "").strip(),
            "meaning_zh": meaning_zh,
            # 词长是难度/常见度的粗代理：过长的词通常不是日常高频词
            "term_length": len(term),
        }
        # 命中统计。term 为空时跳过（articles 层会直接返回 0，但那不是"命中 0"
        # 而是"没法匹配"，两者对模型意义不同）
        if term:
            sig.update(articles.count_term_occurrences(conn, term))
            # 分母：库里共几篇。没有它，模型只能看到"出现 12 次"这种绝对数，
            # 而绝对数会被当成高频——库里只有 5 篇时 12 次并不稀奇。
            sig["corpus_articles"] = corpus_articles
            if sample_note:
                sig["sample_note"] = sample_note
        # 音节只对单词有意义，短语直接不给这个键
        if (item.get("type") or "") == "word":
            syllables = count_syllables(term)
            if syllables is not None:
                sig["syllables"] = syllables
        signals.append(sig)
    return signals


def format_signals(signals: list[dict]) -> str:
    """把统计依据渲染成提示词里的可读文本块。

    渲染顺序即**优先级**：先给"命中几篇 / 共几篇"，再给句子次数。
    这是刻意的——把"出现 12 次"排在最前面会让模型先看到一个大数字，
    而次数高完全可能只是同一篇文章里重复。先摆分篇数，它才是"普遍出现"的证据。
    """
    lines: list[str] = []
    for sig in signals:
        parts = [
            f"entry_id={sig['entry_id']}",
            f"词条={sig['term']}",
            f"类型={'短语' if sig.get('type') == 'phrase' else '单词'}",
        ]
        if sig.get("pos"):
            parts.append(f"词性={sig['pos']}")
        # 空释义必须**显式**标注，不能只留一个空值让模型自己发现：
        # 它需要知道"用户没写释义"，才能在理由里说明自己只靠词形与语料判断，
        # 而不是默默当作"没有信息"然后编一个释义出来。
        parts.append(
            "中文释义=%s" % (sig["meaning_zh"] if sig.get("meaning_zh")
                             else "（用户未填写释义 —— 只能依据词形与语料判断，请在理由中说明这一点）")
        )
        parts.append(f"词长={sig.get('term_length', 0)}")
        if "sentence_hits" in sig:
            # 分篇在前、次数在后：前者才是"普遍"，后者只是"多"
            parts.append(
                f"跨来源频率=命中 {sig.get('article_hits', 0)} 篇 / 库里共 "
                f"{sig.get('corpus_articles', 0)} 篇"
                f"（句子命中 {sig.get('sentence_hits', 0)} 个，仅作参考）"
            )
            if sig.get("sample_note"):
                parts.append(f"样本提示={sig['sample_note']}")
        else:
            parts.append("跨来源频率=未统计")
        if "syllables" in sig:
            parts.append(f"音节数(估算)={sig['syllables']}")
        lines.append("- " + "；".join(parts))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 返回值解析与规整
# ---------------------------------------------------------------------------


def _clean_reason(value: object) -> str:
    """把模型给的理由压成一行短文本（不可信外部文本，只做长度与空白规整）。"""
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    if len(text) > MAX_REASON_LEN:
        text = text[:MAX_REASON_LEN].rstrip() + "…"
    return text


def _coerce_score(value: object) -> float:
    """把模型给的分值规整成 0–5 且为 0.5 的整数倍。

    模型不守规矩是常态而非意外（3.3、7、"四分半"都出现过），所以这里
    **规整而不是丢弃**：夹到 [0,5] 后按 0.5 步进四舍五入。丢弃整条建议
    对用户没有任何好处——一个 3.3 规成 3.5 完全可用。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        # "四分半"这类中文表述：取不到数字就按中位 3.0 处理，
        # 宁可给一个中间值让用户改，也不要让整批失败
        return entries.DEFAULT_RATING
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return entries.DEFAULT_RATING
    number = max(entries.RATING_MIN, min(entries.RATING_MAX, number))
    # 半星对齐：乘 2 后四舍五入
    return round(number * 2) / 2


def parse_suggestions(content: str, valid_ids: set[int]) -> dict[int, dict]:
    """解析模型返回的 JSON，规整为 {entry_id: {...}}。

    容错点（都是模型真实会犯的错）：
    - ```json 代码块包裹 → ai.extract_json 已处理
    - 顶层是 {"items":[...]} 或裸数组 → 都收
    - 少给某几条 → 那些 id 不出现在结果里，由调用方按"缺失"处理
    - 多给不存在的 id → 丢弃（模型幻觉，不能让它污染结果）
    - 分值不合规 → _coerce_score 规整，不抛错
    """
    raw = ai.extract_json(content)

    if isinstance(raw, dict):
        items = raw.get("items")
        if not isinstance(items, list):
            items = raw.get("results") if isinstance(raw.get("results"), list) else None
        if items is None:
            # 顶层直接就是单个对象：{"entry_id":1,...}
            items = [raw] if "entry_id" in raw else []
    elif isinstance(raw, list):
        items = raw
    else:
        raise ai.AIError(f"AI 返回的 JSON 结构无法识别：{type(raw).__name__}")

    out: dict[int, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            entry_id = int(item.get("entry_id"))
        except (TypeError, ValueError):
            continue
        if entry_id not in valid_ids:
            continue  # 模型幻觉出的 id，丢弃
        reasons = item.get("reason")
        if isinstance(reasons, str):
            # 模型偶尔把两维理由合成一句话塞进 reason：平分给两维，
            # 宁可两维看到同一句，也不让其中一维空着
            shared = _clean_reason(reasons)
            reason_imp = reason_use = shared
        elif isinstance(reasons, dict):
            reason_imp = _clean_reason(reasons.get("importance"))
            reason_use = _clean_reason(reasons.get("usefulness"))
        else:
            reason_imp = reason_use = ""
        out[entry_id] = {
            "importance": _coerce_score(item.get("importance")),
            "usefulness": _coerce_score(item.get("usefulness")),
            "reason": {"importance": reason_imp, "usefulness": reason_use},
        }
    return out


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def _chunked(seq: list, size: int):
    """把列表切成定长块。"""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _dump_failed_response(content: str) -> None:
    """整批解析失败时把模型**原始响应**落到数据目录 ai_debug/ 下留证。

    为什么落盘而不是只塞进报错：错误卡片只能带 ±60 字符片段，
    结构性畸形（漏括号 / 截断点在哪）往往要看到全文才能定位。
    文件只写在本机数据目录里，不上传、不进日志。落盘失败不影响主流程。"""
    try:
        debug_dir = Path(config.db_path()).parent / "ai_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        (debug_dir / f"suggest_{stamp}.txt").write_text(content, encoding="utf-8")
    except OSError:
        pass


def suggest(conn, entry_ids: list[int], *, overwrite: bool = False) -> dict:
    """为一批词条生成 AI 建议评分。**绝不写库。**

    参数
    ----
    entry_ids: 要评估的词条 id 列表（去重后按首次出现顺序处理）
    overwrite: False（默认）时**跳过**已有手动评分的词条并返回 skipped 标记；
               True 时同样评估它们，但**仍然只返回建议**。
               两种模式下本函数都不修改数据库（见模块 docstring 的硬约束 2）。

    返回
    ----
    {"items": [{entry_id, term, importance, usefulness, reason:{...},
                skipped, had_rating, error}, ...],
     "total": n, "evaluated": n, "skipped": n, "failed": n, "is_mock": false}

    每条的 error 字段承载该条自己的失败原因（模型漏给、解析失败等），
    单条失败不影响同批其它条目 —— 这是需求里明确要求的隔离行为。
    """
    # 无密钥明确报错（不 mock 降级，理由见模块 docstring 硬约束 3）
    ai.require_api_key()

    # ---- 1. 去重 + 校验 id ----
    unique_ids: list[int] = []
    seen: set[int] = set()
    for raw_id in entry_ids or []:
        try:
            value = int(raw_id)
        except (TypeError, ValueError):
            continue
        if value in seen:
            continue
        seen.add(value)
        unique_ids.append(value)

    if not unique_ids:
        return _empty_result()
    if len(unique_ids) > MAX_IDS_PER_REQUEST:
        raise entries.EntryError(
            f"一次最多评估 {MAX_IDS_PER_REQUEST} 个词条，收到 {len(unique_ids)} 个；"
            "请分批调用"
        )

    # ---- 2. 取词条 ----
    # 单个 id 不存在不算失败（可能刚被删掉），记 error 继续
    items: list[dict] = []
    for entry_id in unique_ids:
        try:
            items.append(entries.get_entry(conn, entry_id))
        except entries.EntryNotFoundError:
            items.append({"id": entry_id, "missing": True})

    # ---- 3. 按 overwrite 决定谁参与评估 ----
    to_evaluate: list[dict] = []
    results: dict[int, dict] = {}
    for item in items:
        entry_id = int(item["id"])
        if item.get("missing"):
            results[entry_id] = _error_item(entry_id, "", "词条不存在或已被删除")
            continue
        # 已评分 = 任一维有值。注意是"或"不是"且"：
        # 只打过重要性的词条，其实用性同样属于"用户已表态"，不该被 AI 覆盖
        had = item.get("importance") is not None or item.get("usefulness") is not None
        if had and not overwrite:
            results[entry_id] = _skip_item(entry_id, item)
        else:
            to_evaluate.append(item)

    # ---- 4. 采集统计依据 ----
    signals = collect_signals(conn, to_evaluate)

    # ---- 5. 分批调用 ----
    # 同步循环，不引入异步任务框架：一次请求内做完，进度由前端在**批之间**显示。
    # 理由见回报：N 个词条按 8 个一批，一次请求最多 8 次串行 HTTP，
    # 而引入 APScheduler/BackgroundTasks 只会带来"任务状态存哪、进程重启怎么办"
    # 这类新问题，对单机单窗口应用是净负担。
    for batch in _chunked(signals, BATCH_SIZE):
        user_msg = (
            f"请评估以下 {len(batch)} 个词条。每条都给了用户写的中文释义与可核查的语料统计，"
            f"请把两者结合起来判断（释义本身也是判断依据），再给出两维建议分值：\n"
            f"{format_signals(batch)}"
        )
        content = None
        try:
            # ★ max_tokens 3000（原 1600）：实测 8 词一批的完整输出约 1000~1100 字符，
            #   中文按 1~1.5 token/字 折算已贴近 1600 上限——用户实测出现过
            #   "输出被截断 / 结构错位"形态的解析失败。3000 留足一倍余量，
            #   成本可忽略（按实际生成计费，上限不是花销）。
            content = ai.chat(_SUGGEST_SYSTEM, user_msg, temperature=0.2, max_tokens=3000)
            parsed = parse_suggestions(content, {int(s["entry_id"]) for s in batch})
        except ai.AIError as exc:
            # 整批失败：逐条标记错误，同批其它批不受影响。
            # content 非空 = chat 成功但解析挂了 ⇒ 原始响应落盘留证（见 _dump_failed_response）。
            if content is not None:
                _dump_failed_response(content)
            for sig in batch:
                entry_id = int(sig["entry_id"])
                results[entry_id] = _error_item(entry_id, sig.get("term", ""), str(exc))
            continue

        # 模型漏给的 id 单独标记，不让"少一条"变成"整批无结果"
        by_id = {int(s["entry_id"]): s for s in batch}
        for entry_id, sig in by_id.items():
            if entry_id not in parsed:
                results[entry_id] = _error_item(
                    entry_id, sig.get("term", ""), "AI 未返回该词条的建议（返回被截断或格式异常）"
                )
        for entry_id, data in parsed.items():
            item = next((i for i in to_evaluate if int(i["id"]) == entry_id), None)
            results[entry_id] = {
                "entry_id": entry_id,
                "term": (item or {}).get("term", ""),
                "importance": data["importance"],
                "usefulness": data["usefulness"],
                "reason": data["reason"],
                "skipped": False,
                "had_rating": bool(
                    item
                    and (item.get("importance") is not None or item.get("usefulness") is not None)
                ),
                "error": "",
            }

    # ---- 6. 按请求顺序输出 ----
    out: list[dict] = [results[eid] for eid in unique_ids if eid in results]
    return {
        "items": out,
        "total": len(out),
        "evaluated": sum(1 for i in out if not i["skipped"] and not i["error"]),
        "skipped": sum(1 for i in out if i["skipped"]),
        "failed": sum(1 for i in out if i["error"]),
        "is_mock": False,
    }


def _skip_item(entry_id: int, item: dict) -> dict:
    """已评分且 overwrite=False：返回 skipped 标记，不带建议分值。"""
    return {
        "entry_id": entry_id,
        "term": item.get("term", ""),
        "importance": None,
        "usefulness": None,
        "reason": {"importance": "", "usefulness": ""},
        "skipped": True,
        "had_rating": True,
        "error": "",
        "skip_reason": "已有手动评分，AI 不会覆盖（如需重新评估请显式选择「重新评估」）",
    }


def _error_item(entry_id: int, term: str, message: str) -> dict:
    return {
        "entry_id": entry_id,
        "term": term,
        "importance": None,
        "usefulness": None,
        "reason": {"importance": "", "usefulness": ""},
        "skipped": False,
        "had_rating": False,
        "error": message,
    }


def _empty_result() -> dict:
    return {
        "items": [],
        "total": 0,
        "evaluated": 0,
        "skipped": 0,
        "failed": 0,
        "is_mock": False,
    }