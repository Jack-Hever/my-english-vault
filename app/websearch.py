"""自动联网搜索例句：外刊库里找不到含目标词的句子时，后端自己去网络上找。

设计约束（2026-10-22）：
    「外刊库里找不到含目标词的句子时，反馈给 AI，AI 自动去网络搜索有关文章
      （搜索到的有关文章不会进入外刊库），而不是让使用者提供网址。」

所以本模块的定位是**兜底**，不是主路径：
- 只在外刊库检索不到时被 app/practices.py 调用；
- 搜到的文章正文**一律不入库**（不写 articles / article_sentences），
  只把命中的句子连同**真实来源 URL** 返回给当次练习使用；
- 一个词都没搜到时如实上报失败原因，绝不假装成功。

为什么单独成文件而不是塞进 articles.py：
articles.py 已经 1400+ 行，混进搜索引擎的 HTML 解析、去广告过滤、
超时预算这些细节会把「外刊库业务层」撑成一个杂物间。职责分开更好读也更好测。

对外只暴露两个东西：
- search_sentences(terms, limit_per_term=3)：主入口，返回形状与
  articles.sentences_from_web 一致（term / found / sentences / url / source_name）；
- WebSearchError：所有对外的错误都用它抛，上层好统一捕获。

--------------------------------------------------------------------------
来源策略（两层，全部来自 2026-10-22 的实测结果，不是拍脑袋）
--------------------------------------------------------------------------
第一层「词典站例句」直连取句（**主力，实测可用**）：
  iciba（金山词霸）/ dict.eudic.net / vocabulary.com
  这三站的例句区是**真实语料**——iciba 上 ephemeral 的例句直接标着
  「GRE/TOEFL/IELTS」「辞典例句」「期刊摘选」，正是这个软件要的东西
  （用户的目标就是雅思写作/口语例句）。
  ★ 实测（team-lead 独立复跑确认）：ephemeral / ubiquitous / equivocal /
    serendipity 四个词各连试 3 次，**12/12 全部命中 3 句**，
    单次耗时 0.3~1.1 秒。**这一层是当前唯一可靠的路径。**

  ★ iciba 走的是**内嵌 JSON 例句区**（__NEXT_DATA__ 里 en+cn+from 三键同在
    的对象），不是整页拍平 —— 整页拍平会把中文语料标注一起粘上来，
    然后被中英混排闸门全部干掉（实测一条都取不到）。
    vocabulary.com 的例句**根本不在静态 HTML 里**（<vcom:examples> 无 data
    属性，JS 后加载），所以它排末位、只能靠黑名单硬筛整页拍平。

第二层「搜索引擎结果页 → 抓正文」（**兜底，未验证路径**）：
  ★ 2026-10-22 强制走这一层实跑 3 轮的结论：
    - **结果页能打开**（搜狗 5384~5387 字节 / 必应 86~103 KB / 360 413~520 KB）；
    - **候选外链能提取**（搜狗只解出 1 个，必应/360 各 5 个）；
    - 但**抓外链正文后取不到合格例句**，3 个词全部 found=0；
    - 耗时 16.4~19.4 秒（30 次抓页），是词典层的 12 倍 —— 慢且没产出。
  ★ 所以这一层**保留但别当主力**：词典例句只是例句，不是**真实外刊原句**；
    真要「网络外刊」只能靠它。当前网络环境是主因（某次实跑连第一层都一起
    打不开 = 网络在抖），用户换网络/代理可用时它是唯一能走通的路径。
  ★ 降级语义完整：取不到就 found=0，绝不假装成功，绝不把噪声塞给用户
    （质量闸门 _is_example_sentence 守着）。
"""

from __future__ import annotations

import html as html_lib
import json
import random
import re
import time
from urllib.parse import quote_plus, unquote, urljoin, urlparse

from . import articles

# --------------------------------------------------------------------------
# 调参（全部集中在这里，改行为只动这一块）
# --------------------------------------------------------------------------

# 单个 HTTP 请求的超时。桌面软件里点一次「新建练习」不能卡住几十秒，
# 10 秒足够把一个正常文章站的首屏拉回来。
REQUEST_TIMEOUT = 10.0

# 整个 search_sentences 的总时限。第一层三个站是「一个词一次请求」，
# 很便宜；第二层才是耗时大头。75 秒是用户能忍、又够跑完两层的上限。
TOTAL_TIME_BUDGET = 75.0

# 第二层里每个词最多尝试抓几篇文章
ARTICLES_PER_TERM = 4

# 第二层里从搜索结果页挑多少个外链进候选池（> ARTICLES_PER_TERM，留出失败余量）
CANDIDATES_PER_TERM = 8

# 第二层里每个词最多换几个搜索引擎
ENGINES_PER_TERM = 3

# 完整的浏览器 UA。**这个必须像浏览器**：用 "EnPractice/0.1" 这种自定义 UA 时，
# Cambridge Dictionary / Merriam-Webster / Britannica 全部返回 403（实测）。
# ★ 从 articles 取同一个常量：两条网络路径（外部贴链接直抓 / 自动联网搜索）
#   的 UA 口径必须一致，口径分裂过一次就必然有一条路径被站点拒掉。
BROWSER_UA = articles.BROWSER_UA

DEFAULT_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "close",
}

# 中日韩字符在「纯英文例句」里一律视为噪声：中文词典站会把例句和释义
# 粘在一段里，直接当例句会得到「大学城里自行车随处可见。 GRE/IELTS ...」这种。
_CJK_RE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")
# 中日韩字符占比超过这个数就判为「不是纯英文句」，丢掉
MAX_CJK_RATIO = 0.02

# 句子前面常挂的语料来源标签，例句本身从标签之后开始取。
# ★ 三类都必须覆盖，缺一类就会漏（本轮实测各踩一次）：
#   「GRE/TOEFL/IELTS」只剥掉 GRE、「CET-4」被截成「-4」、「辞典例句」整条没剥。
#   根因是「一个标签项只吃掉一个词」——所以改成允许连写的考试标签
#   （GRE/TOEFL/IELTS、CET-4 一条吃掉），再由 clean_sentence 循环剥到不再变化。
# ★ 标签表里绝不放普通英文单词：像 "His work is ephemeral." 的 His 绝不能被当标签剥掉。
_EXAM_TOKEN = (r"(?:GRE|TOEFL|IELTS|CET|SAT|ACT|AP"
               r"|考研(?:真题|词汇)?|雅思|托福|四级|六级)")
_SENTENCE_PREFIX_RE = re.compile(
    r"^\s*(?:"
    # 中文标签：以「例句」结尾的（辞典例句 / 牛津例句 / 权威例句 / 例句: …）
    r"[\u4e00-\u9fff]{0,4}例句精选|[\u4e00-\u9fff]{0,4}例句"
    r"|期刊摘选|例句精选|词频句|例句库|例句集"
    # 英文 …例句（Gre 例句 / OALD 例句）
    r"|[A-Z][A-Za-z&/\.\-]{1,10}\s*例句"
    # 考试标签：连字符、斜杠、多段连写（CET-4 / GRE/TOEFL/IELTS）都要能一次吃掉
    r"|" + _EXAM_TOKEN + r"(?:\s*[-/／]\s*(?:" + _EXAM_TOKEN + r")?\s*\d{0,2})*"
    r")\s*[/／|:：\-]?\s*",
    re.IGNORECASE,
)
# 词典页常见的「词 . 」残留：句末标点前后多插了空格（"is ephemeral ."）
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,;:!?])")


# --------------------------------------------------------------------------
# 质量闸门：把词典页的导航/推广文案挡在例句之外
# --------------------------------------------------------------------------
# ★ 这道闸门是被真实事故逼出来的（2026-10-22，用户实跑抓到）：
#   点「新建练习」11.2 秒出题，第一句是
#     "Other forms: teasings Sign up now (it's free!) Whether you're a teacher
#      or a learner, Vocabulary.com can put you or your class on the path to
#      systematic vocabulary improvement."
#   来源 https://www.vocabulary.com/dictionary/teasing
#   —— 一段注册推广文案，被当成例句翻译好丢给用户练翻译。
#   比搜不到还糟：搜不到用户知道要去粘文章，拿到广告文案会当成"这个词的用法"。
#
# 根因：_html_to_text 把整页（页头/导航/词形变化区/注册推广/词条定义/例句区）
# 拍平成一长串文本，再由 _pick_hits 只按「含词 + 纯英文 + 长度够」筛，
# 推广文案三条全过。**光靠「含词」永远筛不干净**，必须有独立的噪声词表。

# a) 站点与品牌名：出现在句子里就是在讲这个站，不是在用这个词造句
_NOISE_BRANDS = (
    "vocabulary.com", "vocabularycom", "iciba", "eudic", "dictionary.com",
    "merriam", "webster", "britannica", "cambridge", "collins", "oxford",
    "dictionary", "thesaurus", "wiktionary", "baidu", "zhihu",
)

# b) 注册与推广文案
_NOISE_PROMO = (
    "sign up", "sign in", "log in", "login", "register", "registration",
    "sign-up", "signup", "it's free", "it is free", "its free",
    "free trial", "subscribe", "subscription", "newsletter", "advertisement",
    "advertise", "share this", "follow us", "privacy policy", "terms of use",
    "all rights reserved", "copyright", "cookie", "terms of service",
    "contact us", "customer service", "learn more about", "start your",
    "create an account", "already have an account", "unlimited access",
)

# c) 词典功能区标题残留（这些是页面结构词，不是例句）
_NOISE_SECTIONS = (
    "other forms", "related words", "synonyms", "antonyms", "opposites",
    "example sentences", "word forms", "definitions", "definition",
    "pronunciation", "hyphenation", "idioms", "phrases", "thesaurus",
    "noun form", "adjective form", "verb form", "adverb form",
    "past tense", "present participle", "gerund", "inflection",
    "transitive", "intransitive", "etymology", "first known use",
    "also called", "plurals", "singular", "learn more", "examples from",
    "views expressed", "disclaimer", "type the word", "browse",
    "search for", "was this helpful", "rate this",
)

_NOISE_RE = re.compile(
    "|".join(
        re.escape(x) for x in _NOISE_BRANDS + _NOISE_PROMO + _NOISE_SECTIONS
    ),
    re.IGNORECASE,
)

# 例句长度上限。超过 220 字符的多半是段落或被拍平后粘连的多个句子，
# 拿去当"一句"翻译，用户读到的是一整段。
SENTENCE_MAX_LEN = 220

# 来源种类：前端要按它显示「来源：网络例句」还是「来源：网络外刊」。
# ★ 说错的代价：拿一句词典例句说成「网络外刊」，用户会以为那是外刊原句。
SOURCE_KIND_DICTIONARY = "dictionary"   # 词典站例句（iciba 的 GRE/IELTS 语料等）
SOURCE_KIND_ARTICLE = "article"         # 搜索抓到的真实文章正文

# 例句该以什么收尾（允许引号：例句常被引起来）
_VALID_ENDINGS = ('.', '!', '?', '"', "'", '”', '’')

# 标题式结构："Other forms: teasings" / "Related words: xxx" 这种
_HEADING_LIKE_RE = re.compile(r"\b[A-Z][A-Za-z ]{2,20}:")

# 连续冒号（两个以上）一定是结构标签串出来的，不是句子
_DOUBLE_COLON_RE = re.compile(r"::")

# 全角 / 中文标点：英文例句里**绝不会**出现，出现就说明这段文字
# 是从中文页面里抠出来的残句（中英混排比例闸门抓不到它们，
# 因为一个「。」就能把比例拉到阈值以下 —— 实测踩过：
#   "BOX。 It makes our life fragile, ephemeral, and as a result of that, more precious."
#   中文只有 1 个字符，比例 1/95 = 0.01 < 0.02，直接漏过去了）。
# 范围：U+3000-U+303F（。、《》「」…）+ U+FF01-U+FF65（全角标点/字母/数字）
# ★ 不含 U+2018/2019/201C/201D（弯引号）—— 那些英文里正常用。
_CJK_PUNCT_RE = re.compile(r"[\u3000-\u303f\uff01-\uff65]")


def _is_example_sentence(text: str, *, min_len: int | None = None,
                         max_len: int = SENTENCE_MAX_LEN) -> tuple[bool, str]:
    """这句是不是**真例句**。返回 (是否合格, 原因)。

    原因会写进日志 —— 排查"为什么这个词一条都没搜到"时，
    「22 句全被推广文案筛掉」和「22 句全被长度筛掉」是两个完全不同的问题。

    判定顺序刻意按「便宜的先做」：噪声词表是整表扫描，最贵；
    首尾字符、长度都是 O(1)。
    """
    s = (text or "").strip()
    if not s:
        return False, "空串"

    low_len = articles.SENTENCE_MIN_LEN if min_len is None else min_len
    if len(s) < low_len:
        return False, f"短于 {low_len} 字符"
    # 上限：拍平后的粘连段落不是"一句"
    if len(s) > max_len:
        return False, f"长于 {max_len} 字符（多半是段落或粘连句）"

    # 首字符必须是大写字母。真实例句首字母大写；
    # 而 "Sign up now..." / "Other forms..." 这类残片会被这条或噪声词表挡掉。
    # ★ 顺序很重要：**先剥开头的引号再判大小写**，否则
    #   '"The ephemeral joy of youth..."' 这种被引起来的例句会被误杀
    #   （第一版写成「先判首字符是不是 A-Z」再谈引号，引号分支永远走不到）。
    first = s[0]
    if first in ("\"", "'", "“", "‘"):
        m = re.match(r"[\"'\u201c\u2018]\s*([A-Za-z])", s)
        if not m or not m.group(1).isupper():
            return False, "引号开头但引号内不是大写字母"
    elif not ("A" <= first <= "Z"):
        return False, f"首字符 {first!r} 不是大写字母"

    if s[-1] not in _VALID_ENDINGS:
        return False, f"结尾 {s[-1]!r} 不是句末标点"

    if _DOUBLE_COLON_RE.search(s):
        return False, "含连续冒号（结构标签串出来的）"
    if _HEADING_LIKE_RE.search(s):
        return False, "含标题式『xxx:』结构"

    m_cjk = _CJK_PUNCT_RE.search(s)
    if m_cjk:
        return False, f"含全角/中文标点（{m_cjk.group(0)!r}，多半是中文页残句）"

    m = _NOISE_RE.search(s)
    if m:
        return False, f"命中噪声词表（{m.group(0)!r}）"

    return True, ""


class WebSearchError(Exception):
    """联网搜索失败（对外统一抛这个，便于上层给用户一句人话）。"""


class SearchResult(list):
    """搜索结果 = list[{term, found, sentences, url, source_name}] + 一个 report。

    做成 list 子类而不是返回 (list, report) 元组，是为了不破坏
    「与 articles.sentences_from_web 同形状」这个约定 —— 调用方照旧遍历即可，
    需要看报告时多读一个 `.report` 即可。
    """

    report: "SearchReport"

    def __init__(self, iterable=(), report: "SearchReport | None" = None) -> None:
        super().__init__(iterable)
        self.report = report or SearchReport()


class SearchReport:
    """一次搜索的内部账本：每层做了什么、每个词最终取到几句。

    ★ 存在的理由是「绝不假装成功」：上层必须能区分
      「搜了但这个词真没有」与「压根没搜成（断网/被封/引擎全挂）」，
      否则用户会以为「这个词不存在」，而真实原因是他家断网了。
    """

    def __init__(self) -> None:
        self.attempts: list[str] = []          # 人话日志，逐条追加
        self.per_term: dict[str, int] = {}     # 词 -> 最终命中的句数
        self.per_term_source: dict[str, str] = {}   # 词 -> 命中来源名
        self.opened_for: dict[str, int] = {}   # 词 -> 实际打开并解析过的网页数
        self.elapsed: float = 0.0
        self.engines_tried: int = 0
        self.pages_fetched: int = 0
        # ★ 分段耗时账本：取词 / 抓页 / 抽句 / 筛句。
        #   用户点「新建练习」要等十几秒，必须能说清瓶颈在哪一段，
        #   否则只能靠外部脚本猜。这里在真实链路上直接计时。
        self.timings: dict[str, float] = {}
        self.timing_calls: dict[str, int] = {}

    def add_timing(self, stage: str, seconds: float) -> None:
        """累加某一段的耗时（秒）。stage 用固定四段名，便于聚合。"""
        self.timings[stage] = self.timings.get(stage, 0.0) + max(0.0, seconds)
        self.timing_calls[stage] = self.timing_calls.get(stage, 0) + 1

    def timing_summary(self) -> str:
        """把分段耗时写成人话的一行行（开发看，不给用户看）。"""
        if not self.timings:
            return ""
        order = ["取词", "抓页", "抽句", "筛句"]
        parts = []
        for stage in order:
            if stage in self.timings:
                parts.append(f"{stage} {self.timings[stage]:.2f}s"
                             f"（{self.timing_calls.get(stage, 0)} 次）")
        for stage, sec in self.timings.items():
            if stage not in order:
                parts.append(f"{stage} {sec:.2f}s")
        return "  ".join(parts)

    def note(self, message: str) -> None:
        self.attempts.append(message)

    def note_opened(self, term: str) -> None:
        """记一笔「这个词名下确实打开并解析了一个网页」。"""
        self.opened_for[term] = self.opened_for.get(term, 0) + 1
        self.pages_fetched += 1

    @property
    def any_source_answered(self) -> bool:
        """是否至少有一个来源成功返回了内容（区别于「搜了但没命中」）。

        注意判定用的是 `OK` 开头而不是 `startswith("OK ")` ——
        日志行实际形如 `OK「ephemeral」…`，「OK」后面没有空格。
        """
        return any(a.startswith("OK") for a in self.attempts)

    def reason_for(self, term: str) -> str:
        """某个词为什么没取到句子 —— **转成人话**，直接展示给用户。

        ★ 这里只给结论，不给日志：attempts 是给开发看的技术流水
          （"必应结果页 103215 字节"这种），直接拼给用户等于让用户读调试信息。
          结论靠两个信号判断：有没有任何来源答话、有没有真的打开过页面。
        """
        if self.per_term.get(term, 0):
            return ""
        if not self.any_source_answered:
            return "连不上网络"
        opened = self.opened_for.get(term, 0)
        if opened:
            return f"打开了 {opened} 个网页，但里面没有含这个词的英文句子"
        return "搜过了，没找到含这个词的英文句子"

    def summary(self) -> str:
        head = (f"耗时 {self.elapsed:.1f}s，尝试 {self.engines_tried} 个搜索引擎，"
                f"打开 {self.pages_fetched} 个网页")
        lines = [head]
        timing = self.timing_summary()
        if timing:
            lines.append("  " + timing)
        return "\n".join(lines + ["  " + a for a in self.attempts])


# --------------------------------------------------------------------------
# 第一层：词典站例句直连（主力）
# --------------------------------------------------------------------------

# (来源名, URL 模板)。{t} 是 URL 编码后的目标词。
#   ★ 顺序有讲究（2026-10-22 真实事故后调整）：
#     iciba         排第一且只差它能用 —— 它的例句区是**内嵌 JSON**，
#                   语料带 GRE/TOEFL/IELTS / 辞典例句 / 期刊摘选 / 考研真题 标注，
#                   正是这个软件要的东西（用户的目标就是雅思写作/口语例句）。
#     vocabulary.com 降到末位且只能靠黑名单硬筛 ——
#                   实测它的例句**不在静态 HTML 里**（<vcom:examples> 无 data 属性，
#                   由 JS 后加载），只能整页拍平，而整页拍平会捞到
#                   "Sign up now (it's free!) ... systematic vocabulary improvement."
#                   这类注册推广文案（真实事故的来源）。
#     eudic         例句区在 HTML 里，但量少（实测 1~3 条），放中间做补充。
DICTIONARY_SOURCES: list[tuple[str, str]] = [
    ("金山词霸", "https://www.iciba.com/word?w={t}"),
    ("Eudic", "https://dict.eudic.net/dicts/en/{t}"),
    ("Vocabulary.com", "https://www.vocabulary.com/dictionary/{t}"),
]

# 这些站实测 403 打不开（或结构不适合取句），**不要**加进来：
#   Cambridge Dictionary / Merriam-Webster / Britannica（403）
#   British Council（403）
#   en.wiktionary.org（403）
#   百度百科 / 知乎（403，且是中文内容）


def cjk_ratio(text: str) -> float:
    """中日韩字符占比。超过 MAX_CJK_RATIO 的句子视为中英混排，丢掉。"""
    if not text:
        return 1.0
    return len(_CJK_RE.findall(text)) / len(text)


def clean_sentence(sentence: str) -> str:
    """剥掉例句前面的语料标签，并压掉多余空白。

    ★ 循环剥到不再变化：单个替换只能吃掉一个标签，
      「GRE/TOEFL/IELTS」「CET-4 考研」这类连写的会被留一半（实测踩过）。
      上限 4 轮是防正则回溯 —— 一旦剥不动（返回串与入串相同）立即停。
    """
    s = re.sub(r"\s+", " ", (sentence or "")).strip()
    for _ in range(4):
        stripped = _SENTENCE_PREFIX_RE.sub("", s).strip()
        if stripped == s or not stripped:
            break
        s = stripped
    # 词典页常把句末标点跟前面的词隔开（"is ephemeral ."），贴回去
    s = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", s)
    # 贴完再压一次：贴回产生的多余空白与标点后残留一起清掉
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------
# 第二层：搜索引擎（兜底）—— ★ 未验证路径，别当主力看 ★
# --------------------------------------------------------------------------
#
# ★★★ 状态说明（2026-10-22 实测，team-lead 独立复跑确认）★★★：
#
#   **第一层（词典站直连）= 当前可靠路径。**
#     实测 ephemeral / ubiquitous / equivocal / serendipity 四个词各连试 3 次，
#     12/12 全部命中 3 句，单次耗时 0.3~1.1 秒。
#
#   **第二层（本层）= 未验证路径。**
#     强制走这一层实跑 3 轮的结果：**结果页能打开**（搜狗 5384~5387 字节 /
#     必应 86~103 KB / 360 413~520 KB），**候选外链也能提取**
#     （搜狗只解出 1 个，必应/360 各 5 个），但**抓外链正文后取不到合格例句**，
#     3 个词全部 found=0。
#     耗时 16.4~19.4 秒（30 次抓页），是词典层的 12 倍 —— 慢且没产出。
#
#   为什么**保留**而不是删掉：
#     ① 词典例句只是例句，不是**真实外刊原句**。真要「网络外刊」，
#        词典站给不了，只能靠这一层去搜真实文章。
#     ② 当前网络环境是主因（某次实跑连第一层都一起打不开 = 网络在抖）。
#        用户换网络 / 代理可用时，这一层是唯一能走通的路径。
#     ③ 它有完整的降级语义：取不到就 found=0，绝不假装成功，绝不把噪声
#        塞给用户（质量闸门 _is_example_sentence 守着）。
#
#   ★ 所以：想调性能/查问题，先看第一层；这一层**当前别指望它兜底**。
#     真要修，方向是换正规搜索 API 或接一个允许抓取的英文语料源，
#     而不是继续调这几个引擎的解析规则（实测卡在「正文取不到句」，
#     不是卡在「链接提不出来」）。

# 每个引擎 = (名字, 结果页 URL 模板, 解析器种类)
# 按顺序尝试。实测可用性见上面「状态说明」—— 这一层是尽力而为，不是可靠路径。
_ENGINES: list[tuple[str, str, str]] = [
    ("搜狗", "https://www.sogou.com/web?query={q}", "sogou"),
    ("必应", "https://cn.bing.com/search?q={q}&setlang=en", "bing"),
    ("360", "https://www.so.com/s?q={q}", "so360"),
]

# 引擎名 -> 解析器种类（extract_links 需要区分搜狗的跳转链接写法）
ENGINE_KINDS = {name: kind for name, _tpl, kind in _ENGINES}

# ---- 搜索引擎自身域名：结果页里必然有大量自家链接，必须滤掉 ----------------
_SEARCH_HOSTS = {
    "sogou.com", "sogoucdn.com", "sogoustatic.com", "sogouzhijie.cn",
    "bing.com", "cn.bing.com", "msn.com", "live.com", "microsoft.com",
    "microsoftonline.com", "bingj.com", "microsofttranslator.com",
    "global.bing.com",
    "so.com", "s360.cn", "360.cn", "qhimg.com", "360img.com",
    # 搜狗/腾讯系的广告与聚合页（实测占了搜狗直链的绝大多数）
    "weixin.qq.com", "channels.weixin.qq.com", "qq.com", "html5.qq.com",
    "gtimg.com", "idcfengye.com",
}

# 社交 / 购物 / 视频 / 论坛：这些页面的正文抓下来基本是导航与评论，不是外刊
_JUNK_HOST_SUBSTR = (
    "facebook.com", "twitter.com", "instagram.com", "linkedin.com",
    "reddit.com", "pinterest.com", "tumblr.com", "weibo.com", "zhihu.com",
    "douban.com", "xiaohongshu.com", "tiktok.com", "discord.com",
    # ★ medium.com **不在名单里**：Medium 上大量是正经英文长文，
    #   是合格的例句来源，一刀切会误杀真材料（实测 <title> 与正文都干净）。
    #   只挡免费建站平台（那些页90% 是导航与广告）。
    "wordpress.com", "blogspot.com", "wixsite.com",
    "csdn.net", "cnblogs.com",
    # ★ 中文站（2026-10-22 实测踩到）：百度百科的条目正文是中文，
    #   抠出来的「例句」是这种中英混排的残句
    #   （"BOX。 It makes our life fragile, ephemeral, and as a result of that, more precious."），
    #   拿它当「网络外刊」是错的。中文站一律不当例句来源。
    "baidu.com", "baike", "sina.com", "sohu.com", "163.com",
    "toutiao.com", "ifeng.com", "people.com.cn", "xinhuanet.com",
    "cctv.com", "douyin.com", "jianshu.com", "segmentfault.com",
    "51cto.com", "cnki.net", "kaoyan.com", "gongkao",
    "amazon.", "ebay.com", "aliexpress.", "taobao.com", "tmall.com",
    "jd.com", "pinduoduo.com", "etsy.com", "alibaba.com", "walmart.com",
    "youtube.com", "youtu.be", "vimeo.com", "bilibili.com", "iqiyi.com",
    "youku.com", "spotify.com", "soundcloud.com", "netflix.com",
    "play.google.com", "apps.apple.com", "paypal.com", "stripe.com",
    # 通用垃圾后缀
    "cdn.", ".js.", ".css.", "cdnjs.", "jsdelivr.", "googleapis.",
    "gstatic.", "cloudflare.", "doubleclick.", "10086.cn",
)

# ★★ 词典站名单：**只在「第二层 搜索 → 抓正文」里**当垃圾外链丢掉。★★
#
# 为什么不能跟 _JUNK_HOST_SUBSTR 合成一张表：
#   · 第二层（搜索结果里点到 iciba/vocabulary.com 的链接）—— 抓下来是
#     释义表不是文章句，必须丢掉；
#   · 第一层（词典站例句直连，DICTIONARY_SOURCES）—— **必须访问它们**，
#     那是我们当前唯一可靠的例句来源（实测 12/12 命中）。
#   共用一张表 = 把词典直连一起打死。
#
# ★ 所以这里单独一张表，由 _is_dict_site() 判定，且只在第二层生效。
#   第一层走 fetcher.get() 直连，根本不经过 _is_junk_result —— 这一点由
#   test_websearch.py 的 [5f] 段用断言钉死（把词典站加回 _JUNK_HOST_SUBSTR
#   必须让测试变红），防止以后有人把两条路径合并。
_DICT_SITE_HOSTS = (
    "dictionary.cambridge.org", "cambridge.org", "merriam-webster.com",
    "dictionary.com", "collinsdictionary.com", "vocabulary.com",
    "oxfordlearnersdictionaries.com", "yourdictionary.com",
    "thefreedictionary.com", "wordreference.com", "iciba.com", "eudic.net",
)

# 明显不是文章的路径后缀
_JUNK_PATH_SUFFIX = (
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".ico", ".woff", ".woff2", ".ttf", ".pdf", ".zip", ".exe", ".apk",
    ".mp3", ".mp4", ".json", ".xml",
)
_JUNK_PATH_PART = ("/login", "/signin", "/register", "/account", "/cart",
                   "/checkout", "/download", "/app", "/shop", "/product")


def host_of(url: str) -> str:
    """取 URL 的域名（小写）。解析失败返回空串。"""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _is_junk_result(url: str) -> tuple[bool, str]:
    """判断一个结果页外链该不该丢。返回 (是否丢弃, 原因)。

    拆出 reason 是为了写进日志：搜索结果页的噪声占比很高，
    「为什么这条被丢了」比「这条被丢了」有用得多。
    """
    url = (url or "").strip()
    if not url:
        return True, "空链接"
    low = url.lower()
    if low.startswith(("javascript:", "mailto:", "tel:", "#", "data:", "void(")):
        return True, "非 http 协议"
    if not low.startswith(("http://", "https://")):
        return True, "非 http 协议"
    host = host_of(url)
    if not host:
        return True, "解析不出域名"

    bare = host[4:] if host.startswith("www.") else host
    if bare in _SEARCH_HOSTS or host in _SEARCH_HOSTS:
        return True, "搜索引擎自家域名"
    for s in _SEARCH_HOSTS:
        if bare.endswith("." + s):
            return True, "搜索引擎自家域名"

    for frag in _JUNK_HOST_SUBSTR:
        if frag in bare:
            return True, f"命中过滤站点（{frag}）"

    path = urlparse(url).path.lower()
    if any(path.endswith(sfx) for sfx in _JUNK_PATH_SUFFIX):
        return True, "路径指向静态资源"
    if any(part in path for part in _JUNK_PATH_PART):
        return True, "路径指向功能页而非文章"
    return False, ""


# --------------------------------------------------------------------------
# 结果页解析：把 HTML 里的外链抠出来
# --------------------------------------------------------------------------

_HREF_RE = re.compile(r'<a[^>]+href\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_META_REFRESH_RE = re.compile(
    r"""http-equiv=["']?refresh["']?[^>]*?url=([^\s"'>;]+)""", re.IGNORECASE)


def _decode_sogou_redirect(html: str, href: str) -> str:
    """搜狗结果页的正文链接是 /link?url=xxx，且 xxx 是**加密**跳转串
    （明文 query 参数里没有真实地址，实测过）。

    实测解法：请求这个跳转地址，它返回一个 244 字节的中间页，
    里面用<meta http-equiv="refresh" content="0;URL='真实地址'"> 跳转。
    这个函数只在 resolve_links=True 时才会发这一次额外请求。
    """
    if "/link?url=" not in href:
        return href
    m = re.search(r"[?&]url=([^&]+)", href)
    if m:
        candidate = unquote(m.group(1))
        if candidate.startswith(("http://", "https://")):
            return candidate      # 少数情况直接给了明文
    return href


def _is_dict_site(url: str) -> tuple[bool, str]:
    """这个 URL 是不是**词典站**。

    ★ 只在第二层（搜索结果页 → 抓正文）里用：搜索结果里点到词典站的链接，
      抓下来是释义表不是文章句，要丢掉。
      但第一层「词典站例句直连」必须能访问它们 —— 所以它跟 _JUNK_HOST_SUBSTR
      分开，**第一层不受这张表影响**（见 _DICT_SITE_HOSTS 处的说明）。

    拆出 reason 是为了写进日志：搜索结果页的噪声占比很高，
    「为什么这条被丢了」比「这条被丢了」有用得多。
    """
    host = host_of(url)
    if not host:
        return False, ""
    bare = host[4:] if host.startswith("www.") else host
    for s in _DICT_SITE_HOSTS:
        if bare == s or bare.endswith("." + s):
            return True, f"命中词典站名单（{s}）"
    return False, ""


def extract_links(html: str, engine_kind: str, base_url: str) -> list[str]:
    """从结果页 HTML 抽出候选外链（已过滤、已按出现顺序去重）。

    ★ 第二层专用：这里会把词典站链接也丢掉（_is_dict_site）——
      搜索结果里的词典链接不是「外刊文章」。
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in _HREF_RE.findall(html or ""):
        href = raw.strip().replace("&amp;", "&")
        if not href or href.startswith("#"):
            continue
        try:
            absolute = urljoin(base_url, href)
        except ValueError:
            continue
        junk, _reason = _is_junk_result(absolute)
        if junk:
            continue
        is_dict, _dreason = _is_dict_site(absolute)
        if is_dict:
            continue
        # 去掉跟踪参数能显著提高去重率，但保留可能带语义的 id/url 参数
        p = urlparse(absolute)
        query = p.query
        if query and not re.search(r"(^|&)(id|url|q|p|page)=\w", query):
            query = ""
        clean = f"{p.scheme}://{p.netloc}{p.path}" + (f"?{query}" if query else "")
        key = clean.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def resolve_sogou_links(fetcher: "_Fetcher", html: str,
                        base_url: str, limit: int = 6) -> list[str]:
    """搜狗专用：把 /link?url= 加密跳转串还原成真实外链。

    代价是每个串一次额外请求，所以有 limit 上限；失败就跳过，不抛。
    """
    pending: list[str] = []
    for raw in _HREF_RE.findall(html or ""):
        href = raw.strip().replace("&amp;", "&")
        if "/link?url=" not in href:
            continue
        item = _decode_sogou_redirect("", urljoin(base_url, href))
        if item.startswith(("http://", "https://")) and item not in pending:
            pending.append(item)
    out: list[str] = []
    for redirect in pending[:limit]:
        page = fetcher.get(redirect, timeout=8.0)
        if not page:
            continue
        m = _META_REFRESH_RE.search(page) or re.search(
            r"""URL=['"](https?://[^'"\s>]+)""", page, re.IGNORECASE)
        if not m:
            continue
        target = m.group(1).strip().strip("'\"")
        junk, _r = _is_junk_result(target)
        if junk:
            continue
        is_dict, _d = _is_dict_site(target)
        if is_dict:
            continue
        if target not in out:
            out.append(target)
    return out


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class _Fetcher:
    """带总时限预算的 requests 薄封装。"""

    def __init__(self, budget: float | None = None) -> None:
        import requests

        self._s = requests.Session()
        self._s.headers.update(DEFAULT_HEADERS)
        # ★ 用 None 哨兵而不是 `budget: float = TOTAL_TIME_BUDGET`：
        #   默认参数在函数定义时就求值了，运行期改websearch.TOTAL_TIME_BUDGET
        #   （测试打桩、将来调参）不会生效 —— 实测踩过：设成 0.2 秒仍是 90 个页面。
        self.budget = TOTAL_TIME_BUDGET if budget is None else float(budget)
        self.deadline = time.monotonic() + self.budget
        self._requests = requests
        self.calls = 0

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def get(self, url: str, timeout: float = REQUEST_TIMEOUT) -> str | None:
        """抓一个 URL；失败返回 None（**不抛**，调用方只跳过这一条）。"""
        left = self.remaining()
        if left <= 0.5:
            return None
        self.calls += 1
        try:
            resp = self._s.get(url, timeout=min(timeout, left), allow_redirects=True)
        except Exception:  # noqa: BLE001 - 网络异常种类极多，一律降级为跳过
            return None
        if resp.status_code != 200:
            return None
        # 服务器声明了 gzip 但实际没压缩时，requests 会解出乱码
        try:
            resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
        except Exception:  # noqa: BLE001
            resp.encoding = "utf-8"
        return resp.text or ""


# --------------------------------------------------------------------------
# 单页取句
# --------------------------------------------------------------------------

def _pick_hits(sentences: list[str], term: str, limit: int,
               *, report: SearchReport | None = None,
               term_label: str = "") -> list[str]:
    """从正文里挑出含该词、且是**真例句**的句子（按原顺序）。

    ★ 「真例句」= 过质量闸门 _is_example_sentence，不是「含词 + 是英文」。
      词典页整页拍平后，推广文案同样含词、同样是纯英文、长度也够，
      只看这三条会把注册广告交给用户练翻译（2026-10-22 真实事故）。
    """
    pat = articles._term_pattern(term)      # 复用库内变体匹配，两条路径口径一致
    out: list[str] = []
    rejected: dict[str, int] = {}
    for raw in sentences:
        if not pat.search(raw):
            continue
        cleaned = clean_sentence(raw)
        if cjk_ratio(cleaned) > MAX_CJK_RATIO:
            rejected["中英混排"] = rejected.get("中英混排", 0) + 1
            continue               # 中英混排，例句会被中文释义污染
        ok, why = _is_example_sentence(cleaned)
        if not ok:
            rejected[why] = rejected.get(why, 0) + 1
            if report is not None:
                report.note(f"筛掉「{term_label or term}」候选：{why} —— {cleaned[:60]}")
            continue
        if cleaned not in out:
            out.append(cleaned)
    if report is not None and rejected:
        detail = "；".join(f"{k}×{v}" for k, v in
                          sorted(rejected.items(), key=lambda kv: -kv[1])[:4])
        report.note(f"「{term_label or term}」共筛掉 {sum(rejected.values())} 条候选（{detail}）")
    return out[:limit]


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def search_sentences(terms: list[str], limit_per_term: int = 3,
                     *, report: SearchReport | None = None) -> SearchResult:
    """为每个目标词自动联网找含该词的例句。**搜到的内容一律不入外刊库。**

    返回 SearchResult（list 子类，元素形如
    {term, found, sentences, url, source_name}，与 articles.sentences_from_web 同形状），
    额外挂一个 `.report` 属性供上层如实上报。

    ★ 找不到时**不抛异常**，而是返回 found=0 的条目 + 原因；
      两层来源全部彻底跑不起来（断网等）时抛 WebSearchError，由上层决定怎么提示。
    ★ 绝不返回「假装成功」的结果：找不到就是 found=0。
    """
    limit_per_term = max(1, int(limit_per_term or 3))
    rep = report or SearchReport()

    cleaned: list[str] = []
    t_terms = time.monotonic()
    for t in terms or []:
        t = (t or "").strip()
        if t and t not in cleaned:
            cleaned.append(t)
    rep.add_timing("取词", time.monotonic() - t_terms)
    if not cleaned:
        return SearchResult([], rep)

    started = time.monotonic()
    fetcher = _Fetcher()
    by_term: dict[str, list[dict]] = {t: [] for t in cleaned}
    filled: set[str] = set()

    # ---- 第一层：词典站例句直连 ------------------------------------------
    for name, template in DICTIONARY_SOURCES:
        pending = [t for t in cleaned if t not in filled]
        if not pending:
            break
        if fetcher.remaining() <= 5.0:
            rep.note(f"总时限已到，词典站这一层剩下的 {len(pending)} 个词没再查")
            break
        for term in pending:
            if fetcher.remaining() <= 5.0:
                rep.note(f"总时限已到，「{term}」没再查词典站")
                break
            url = template.format(t=quote_plus(term))
            t_fetch = time.monotonic()
            page = fetcher.get(url)
            rep.add_timing("抓页", time.monotonic() - t_fetch)
            if page is None:
                rep.note(f"FAIL「{term}」在 {name} 打开失败")
                continue
            rep.note_opened(term)
            hits, title = _hits_from_html(page, url, term, limit_per_term,
                                          source_kind=SOURCE_KIND_DICTIONARY, report=rep)
            if hits:
                rep.note(f"OK「{term}」在 {name}《{title[:32]}》找到 {len(hits)} 句")
                by_term[term].extend(hits)
                filled.add(term)
            else:
                # ★ 「一条真例句都没有」= 没找到（不是"找到了一堆噪声"）。
                #   继续试下一个来源，绝不把噪声塞给用户。
                rep.note(f"FAIL「{term}」在 {name} 没找到合格例句（其余候选被质量闸门筛掉）")

    # ---- 第二层：搜索引擎 → 抓正文（兜底）--------------------------------
    for term in cleaned:
        if term in filled:
            continue
        if fetcher.remaining() <= 5.0:
            rep.note(f"总时限已到，「{term}」没再走搜索引擎")
            break
        links = _search_then_collect(fetcher, term, rep, limit_per_term)
        if links:
            rep.note(f"OK「{term}」经搜索找到 {len(links)} 句")
            by_term[term].extend(links)
            filled.add(term)

    out = SearchResult()
    for t in cleaned:
        hits = by_term[t]
        if len(hits) > limit_per_term:
            uniq: list[dict] = []
            seen_text: set[str] = set()
            for h in hits:
                if h["text"] in seen_text:
                    continue
                seen_text.add(h["text"])
                uniq.append(h)
            hits = random.sample(uniq, limit_per_term) if len(uniq) > limit_per_term else uniq
        hits.sort(key=lambda h: (h["url"], h["seq"]))
        rep.per_term[t] = len(hits)
        if hits:
            rep.per_term_source[t] = hits[0]["source_name"]
        out.append({
            "term": t,
            "found": len(hits),
            "sentences": hits,
            # 顶层url / source_name 也给一份，方便前端直接显示「来自哪里」
            "url": hits[0]["url"] if hits else "",
            "source_name": hits[0]["source_name"] if hits else "",
            # ★ 新增：dictionary（词典例句）/ article（真实外刊文章）
            #   前端按它显示「来源：网络例句」还是「来源：网络外刊」。
            #   同一批句子可能跨来源，所以逐句带；顶层给首个的种类做兜底。
            "source_kind": (hits[0].get("source_kind", SOURCE_KIND_ARTICLE)
                           if hits else ""),
        })

    rep.elapsed = time.monotonic() - started
    if not rep.any_source_answered:
        raise WebSearchError(
            "连不上任何网络来源：" + "；".join(rep.attempts[-3:])
            + f"（耗时 {rep.elapsed:.1f}s）"
        )
    return SearchResult(out, rep)


def _hits_from_html(html: str, url: str, term: str, limit_per_term: int,
                    *, source_kind: str = SOURCE_KIND_ARTICLE,
                    report: SearchReport | None = None) -> tuple[list[dict], str]:
    """从**已经拿到的** HTML 里选句（不重复发请求）。

    source_kind：dictionary = 词典站例句 / article = 搜索到的真实文章。
    前端据此显示「来源：网络例句」还是「来源：网络外刊」——
    说错会让用户以为拿到的是外刊文章，其实是一句词典例句。
    """
    title = articles._extract_title(html) or url
    source_name = articles._source_from_url(url)

    # 优先走「内嵌 JSON 例句区」：这是词典站把例句结构化给前端组件用的数据，
    # 比整页拍平干净得多（iciba 的例句带 GRE/TOEFL/IELTS / 辞典例句 / 期刊摘选
    # 的真实语料标注，整页拍平会把这些中文标注一起粘上来，然后被中英混排闸门
    # 全部干掉 —— 实测整页拍平 iciba 一条都取不到，走 JSON 能取到 15~41 条）。
    t_extract = time.monotonic()
    structured = _extract_structured_examples(html, url)
    if structured:
        report and report.note(
            f"「{term}」在 {source_name} 的内嵌例句区解出 {len(structured)} 条")
        t_filter = time.monotonic()
        picked = _pick_hits(structured, term, limit_per_term,
                            report=report, term_label=term)
        report and report.add_timing("筛句", time.monotonic() - t_filter)
    else:
        text = articles._html_to_text(html)
        sentences = articles.split_sentences(text) if text else []
        t_filter = time.monotonic()
        picked = _pick_hits(sentences, term, limit_per_term,
                            report=report, term_label=term) if sentences else []
        report and report.add_timing("筛句", time.monotonic() - t_filter)
    report and report.add_timing("抽句", time.monotonic() - t_extract)

    hits = [{
        "sentence_id": None, "seq": i, "text": s, "article_id": None,
        "title": title, "source_name": source_name, "url": url,
        "from_web": True, "term": term,
        # ★ 新增：前端要区分「词典例句」与「真实外刊文章」，说错会让用户误判素材性质
        "source_kind": source_kind,
    } for i, s in enumerate(picked, start=1)]
    return hits, title


# ---- 内嵌 JSON 例句区解析 ---------------------------------------------------
# iciba（Next.js）：<script id="__NEXT_DATA__"> 里递归找 {en, cn, from} 三键同在的对象
_ICIBA_JSON_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S | re.I)
# vocabulary.com：<vcom:examples data="[...]"> （实测该站此属性为空，例句由 JS 后加载，
#   所以这个解析器对它通常解不出东西 —— 保留是因为形态明确，将来它改了能直接用）
_VCOM_EXAMPLES_RE = re.compile(r'<vcom:examples[^>]*\bdata="([^"]*)"', re.I)


def _extract_structured_examples(html: str, url: str) -> list[str]:
    """从页面内嵌 JSON 里解出例句列表（解不出就返回空，调用方退回整页拍平）。"""
    host = (articles._source_from_url(url) or "").lower()

    # iciba 系
    if "iciba" in host:
        m = _ICIBA_JSON_RE.search(html or "")
        if m:
            try:
                data = json.loads(m.group(1))
            except (json.JSONDecodeError, ValueError):
                return []
            out: list[str] = []

            def walk(node: object) -> None:
                if isinstance(node, dict):
                    en, cn, frm = node.get("en"), node.get("cn"), node.get("from")
                    # ★ 三个键同时在且都是字符串 —— 这是 iciba 例句对象的确定形态。
                    #   判据这么严是有理由的：宽松版（只要有 en 字符串）会把页面里
                    #   各种英文片段（按钮文字、SEO 描述）也当成例句。
                    if isinstance(en, str) and isinstance(cn, str) and isinstance(frm, str):
                        out.append(en.strip())
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for v in node:
                        walk(v)

            walk(data)
            return [s for s in out if s]

    # vocabulary.com 系
    if "vocabulary.com" in host:
        out_v: list[str] = []
        for m2 in _VCOM_EXAMPLES_RE.finditer(html or ""):
            raw = html_lib.unescape(m2.group(1))
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(data, list):
                for row in data:
                    if isinstance(row, dict):
                        for k in ("sentence", "text", "example"):
                            v = row.get(k)
                            if isinstance(v, str) and v.strip():
                                out_v.append(v.strip())
        return out_v
    return []


def _search_then_collect(fetcher: _Fetcher, term: str, rep: SearchReport,
                         limit_per_term: int) -> list[dict]:
    """第二层：搜索结果页 → 候选外链 → 逐篇抓正文取句。"""
    pool: list[str] = []
    seen: set[str] = set()
    pages: dict[str, str] = {}          # 引擎名 -> 已抓结果页（同一引擎不重复抓）

    for engine_name, template, kind in _ENGINES[:ENGINES_PER_TERM]:
        if len(pool) >= CANDIDATES_PER_TERM or fetcher.remaining() <= 5.0:
            break
        result_url = template.format(q=quote_plus(f'"{term}"'))
        if engine_name in pages:
            page = pages[engine_name]
        else:
            rep.engines_tried += 1
            t_fetch = time.monotonic()
            page = fetcher.get(result_url)
            rep.add_timing("抓页", time.monotonic() - t_fetch)
            if page is None:
                rep.note(f"FAIL「{term}」{engine_name}结果页打不开")
                pages[engine_name] = ""
            else:
                rep.note(f"OK「{term}」{engine_name}结果页 {len(page)} 字节")
                pages[engine_name] = page
        if not page:
            continue
        links = extract_links(page, kind, result_url)
        if kind == "sogou" and not links and fetcher.remaining() > 10.0:
            # 搜狗正文链接是加密跳转串，需要额外请求一次中间页才能还原
            t_fetch = time.monotonic()
            links = resolve_sogou_links(fetcher, page, result_url)
            rep.add_timing("抓页", time.monotonic() - t_fetch)
            if links:
                rep.note(f"OK「{term}」搜狗加密跳转还原出 {len(links)} 个外链")
        fresh = [l for l in links if l.split("?")[0] not in seen]
        seen.update(l.split("?")[0] for l in fresh)
        pool.extend(fresh)
        rep.note(f"OK「{term}」{engine_name}候选外链累计 {len(pool)}")

    hits: list[dict] = []
    tried = 0
    for url in pool[:CANDIDATES_PER_TERM]:
        if len(hits) >= limit_per_term or tried >= ARTICLES_PER_TERM:
            break
        if fetcher.remaining() <= 5.0:
            break
        tried += 1
        t_fetch = time.monotonic()
        page = fetcher.get(url)
        rep.add_timing("抓页", time.monotonic() - t_fetch)
        if page is None:
            continue
        rep.note_opened(term)
        got, title = _hits_from_html(page, url, term, limit_per_term,
                                     source_kind=SOURCE_KIND_ARTICLE, report=rep)
        if got:
            rep.note(f"OK「{term}」在 {host_of(url)}《{title[:28]}》找到 {len(got)} 句")
        hits.extend(got)
    if not hits and pool:
        rep.note(f"FAIL「{term}」打开了 {tried} 个搜索结果页但都没取到例句")
    return hits