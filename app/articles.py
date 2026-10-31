"""外刊文章库业务逻辑层。

数据关系（用户确认的设计）：
- articles 存外刊文章（仅本地库内容：手动粘贴或本地导入）
- article_sentences 按句切分并保留序号，用于「按词检索选句」
- tags / article_tags 是多对多标签：一篇文章可打多个标签，新标签自动创建并沉淀
- 网络抓取的外刊内容**不入库**，只在当次练习中使用并附真实链接

选句规则（用户确认）：
- 练习按目标词汇/短语，从外刊库检索含该词的句子
- 随机选取 3 个；文章中没有 3 个以上时，有多少取多少
- 外刊库检索不到时，才回退到网络外刊摘取（内容仍不入库）
"""

from __future__ import annotations

import random
import re
import sqlite3

from .db import normalize_term
from .entries import EntryError, now_iso

# 短于此长度的片段不视为句子（过滤标题残留、版权声明碎片等）
SENTENCE_MIN_LEN = 20
# 每个目标词最多选取的句子数（用户要求 3 个）
SENTENCES_PER_TERM = 3

TITLE_MAX = 300

# 网络抓取用的浏览器 UA。**放在 articles.py 而不是 websearch.py**：
# 两条网络路径（外部贴链接直抓 / 自动联网搜索）都必须用同一个 UA，
# 而 websearch 又 import articles —— 把常量放在被依赖方，才不会反向依赖。
# 实测：换成这个 UA 之后 Cambridge / Merriam-Webster / Britannica 从 403 变可访问。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SOURCE_NAME_MAX = 100
URL_MAX = 2000
CONTENT_MAX = 200_000
CONTENT_ZH_MAX = 200_000
# 其他信息（作者信息/文章背景等）：够写作者简介加一段背景，不至于让人粘贴整篇
EXTRA_INFO_MAX = 5_000
# 中文标题：一个外刊标题的中文说法，量级与原标题相当（不会比英文标题长很多）
TITLE_ZH_MAX = 300
TAG_NAME_MAX = 20
MAX_TAGS_PER_ARTICLE = 5

# 对齐的词汇锚点权重（见 _dp_align）。单位与「字符数差」同量纲：
# 每命中一个锚点便宜 MATCH_BONUS 个字符的误差，对称差里每个锚点多罚
# MISMATCH_PENALTY。取值刻意保守——锚点只是辅助信号，主体仍是长度。
#
# 两个权重**必须相等**，代价就是 W × (|A△S| - |A∩S|)：
# 只奖励命中、不罚「这段多出来的锚点」的话，合并两段中文总能收集到更多
# 锚点，DP 会被推向无脑合并（实测因此把正确的两句并成了一句）。对称之后
# 多吃一段带来的命中增量会被多出的无关锚点抵消，合并不再占便宜。
# 同理只罚 miss 不罚 extra 也不行——那会逼着句子去抢占含自己锚点的段落。
_ANCHOR_MATCH_BONUS = 4.0
_ANCHOR_MISMATCH_PENALTY = 4.0
# 连续英文句共享同一段中文时的合并上限。真实语料里 3 句英文挤一段译文
# 已属罕见（多为引语交错 "…," he adds. "…"），再放开只是给 DP 更多乱配空间。
_DP_MAX_EN_SHARE = 3
# DP 的搜索窗口：第 i 句英文的译文起点最多往前回溯这么多句中文。
# 中英句序大体一致，真实错位是「逐句累积漂移」而非隔十几句的大跳跃；
# 放开全区间搜索是 O(n·m²)（92 句实测 437ms，详情页会明显卡顿），
# 限窗后降到 O(n·m·W)，实测结果完全一致而快数倍。
_DP_WINDOW = 12
# 「p 句英文共享一段译文」的代价系数（见 _dp_align）：每多共享一句，多付
# 「组内平均每句期望译文长度」的这个倍数。按长度比例收而非绝对常数，
# 长句文章自动放大，不必手调阈值。
#
# 必须收这个费：共享组的长度代价是 |seg - (exp1+..+expp)|，逐句配对是
# |seg1-exp1| + ...，由三角不等式前者恒不大于后者，再加上 ratio 由全篇
# 总长算出、共享整篇时误差恰好为 0——不收费的话 DP 会无脑把所有英文句
# 并成一组。共享意味着这 p 句在详情页共用同一段译文，是实打实的信息损失。
#
# 取 1.0：多共享一句的代价≈「少一句译文」，于是共享与留空几乎等价，
# 只有译文确实把这几句并成一段（留空要付完整空罚）时才划算——
# 宁可让练习回退 AI 单句翻译，也不错配一段夹带邻句内容的译文。
_SHARE_PENALTY_RATIO = 1.0
# 文档内出现次数超过这个值的锚点视为「太常见、没有区分度」，直接丢弃。
_ANCHOR_MAX_DOC_FREQ = 4
# 锚点位掩码的位宽上限。超过就只保留最稀有的那些，保证 int 位运算够快。
_ANCHOR_MAX_BITS = 64

# 锚点用的正则与字表
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
_EN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_LATIN_RE = re.compile(r"[A-Za-z]{2,}")
_ZH_NUM_RE = re.compile(r"[零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖十百千万亿]+")
_ZH_DIGIT = {
    "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "贰": 2, "三": 3, "叁": 3,
    "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6, "七": 7, "柒": 7,
    "八": 8, "捌": 8, "九": 9, "玖": 9,
}
_ZH_UNIT = {"十": 10, "百": 100, "千": 1000}
_ZH_BIG_UNIT = {"万": 10_000, "亿": 100_000_000}
# 句首大写是英语常态，这些词不该被当成专有名词锚点
_EN_ANCHOR_STOP = {
    "the", "this", "that", "these", "those", "there", "their", "they", "them",
    "then", "than", "a", "an", "and", "as", "at", "but", "by", "for", "from",
    "he", "her", "his", "she", "him", "i", "in", "is", "it", "its", "not", "no",
    "of", "on", "or", "our", "so", "such", "to", "was", "were", "what", "when",
    "where", "which", "while", "who", "will", "with", "would", "you", "your",
    "we", "us", "if", "although", "because", "since", "after", "before",
    "however", "moreover", "meanwhile", "also", "yet", "still", "even", "just",
    "only", "both", "each", "another", "other", "others", "some", "any", "all",
    "most", "many", "much", "few", "less", "now", "new", "old", "one", "two",
    "three", "four", "five", "first", "second", "third", "mr", "mrs", "ms", "dr",
}

# 中文句末标点。刻意不含半角 "."：中文里的半角点多见于数字与缩写
# （3.14 / U.S.），按它切会把一句话切碎。
# 也不含"；"：译文里的分号多对应英文的逗号/从句连接（"失业状态；而丹麦…"），
# 不是句子边界，按它切会无谓多切出句子、破坏与英文的对齐。
_ZH_ENDINGS = "。！？!?\n"
# 切句后要剥掉的首尾包装符号（引号、括号等）
_ZH_TRIM = " \t\r\n\"'“”‘’「」『』《》〈〉（）()[]【】[],.;；,，、"

# 常见英文缩写（去掉句点后比对）。句点前是这些词时不当句末，避免把
# "Dr. Smith" / "Fig. 3" / "U.S. Army" 切开。
_ABBREVIATIONS = {
    "dr", "mr", "mrs", "ms", "prof", "st", "vs", "etc", "eg", "ie", "fig",
    "no", "inc", "ltd", "co", "jr", "sr", "u.s", "u.k", "u.n", "ph.d", "d.c",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "vol", "ch", "pp", "ed", "rev", "gen", "col", "capt", "sgt", "lt", "hon",
}


def _is_abbreviation(word: str) -> bool:
    """判断句点前的词是否为缩写（形如 Dr. / U.S. / Fig. / Ph.D.）。"""
    token = word.strip()
    if not token:
        return False
    if "." in token:  # U.S. / Ph.D. 这类带内点的缩写
        return True
    return token.rstrip(".").lower() in _ABBREVIATIONS


class ArticleError(EntryError):
    """外刊库业务错误，映射为 HTTP 400。"""


class ArticleNotFoundError(ArticleError):
    """文章不存在，映射为 HTTP 404。"""

    def __init__(self, article_id: int) -> None:
        super().__init__(f"文章不存在或已被删除（ID={article_id}）")
        self.article_id = article_id


def _split_paragraphs(text: str) -> list[str]:
    """按换行切段，丢空段。段边界即句边界：标点后紧跟换行时，
    扫描器的「标点后无空白」分支会漏判（j-i==1 被当句中点），
    导致把下一段第一句粘进上一句——先分段可根治。"""
    return [p.strip() for p in (text or "").split("\n") if p.strip()]


def _scan_sentences(para: str) -> list[str]:
    """在单段内逐字符扫描切句（缩写判断逻辑见 split_sentences 文档）。"""
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(para)
    while i < n:
        ch = para[i]
        buf.append(ch)
        if ch in ".!?":
            j = i + 1
            while j < n and para[j] in " \t":
                j += 1
            if j >= n or para[j] in ".!?":
                i += 1
                continue
            if j - i == 1:  # 标点后没有空白，不是句末（如 3.14 / e.g.x）
                i += 1
                continue
            before = "".join(buf).rstrip(".!?").strip()
            last_word = before.split()[-1] if before.split() else ""
            if ch == "." and _is_abbreviation(last_word):
                i += 1
                continue
            if para[j].islower():  # 标点后跟小写，视为句中点
                i += 1
                continue
            out.append("".join(buf).strip())
            buf = []
            i = j
            continue
        i += 1

    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def split_sentences(text: str) -> list[str]:
    """按句末标点切分正文，过滤过短片段。

    先按换行分段、再在段内切句——段边界就是句边界，避免
    "…prospects?\nAs the oil…" 这类跨段粘连（标点后紧跟换行时，
    扫描器无法区分「句中点」与「段末」）。
    首段若无任何句末标点且后面还有正文，视为随正文粘贴的标题行，丢弃。
    """
    paras = _split_paragraphs(text)
    if len(paras) > 1 and not paras[0].endswith((".", "!", "?", "。", "！", "？", "…")):
        paras = paras[1:]
    out: list[str] = []
    for para in paras:
        out.extend(_scan_sentences(para))
    return [s for s in out if len(s) >= SENTENCE_MIN_LEN]


def split_zh_sentences(text: str) -> list[str]:
    """把中文译文按句末标点（。！？；等）与换行切分，剥掉首尾引号/括号。

    不做长度过滤：译文可能整体偏短，且要与英文句数对齐，
    过滤会造成两边句数对不上。空片段直接丢弃。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return []

    out: list[str] = []
    buf: list[str] = []
    for ch in cleaned:
        buf.append(ch)
        if ch in _ZH_ENDINGS:
            piece = "".join(buf).strip().strip(_ZH_TRIM).strip()
            if piece:
                out.append(piece)
            buf = []
    tail = "".join(buf).strip().strip(_ZH_TRIM).strip()
    if tail:
        out.append(tail)
    return out


def _norm_num(raw: str) -> str:
    """把数字写法统一（去千分位/百分号、3.50→3.5），让中英两侧可比。"""
    s = raw.replace(",", "").replace("%", "").strip()
    try:
        return f"{float(s):g}"
    except ValueError:
        return s


def _parse_zh_number(s: str) -> int | None:
    """把「二十 / 一百二十 / 三万」这类中文数字串解析成整数。

    只覆盖最常见的十进制读法，遇到集合外字符返回 None（不当锚点）。
    """
    total = 0     # 万/亿以上已累加的部分
    section = 0   # 当前「万」以内的段
    digit = 0     # 尚未接单位的一位数
    seen = False
    for ch in s:
        if ch in _ZH_DIGIT:
            digit = _ZH_DIGIT[ch]
            seen = True
        elif ch in _ZH_UNIT:
            section += (digit if digit else 1) * _ZH_UNIT[ch]
            digit = 0
            seen = True
        elif ch in _ZH_BIG_UNIT:
            section += digit
            total += (section if section else 1) * _ZH_BIG_UNIT[ch]
            section = 0
            digit = 0
            seen = True
        else:
            return None
    if not seen:
        return None
    return total + section + digit


def _en_anchors(text: str) -> set[str]:
    """从英文句抽取锚点：阿拉伯数字 + 大写开头的专名/全大写缩写。

    小写普通词（recession 之类）不做锚点——它不会出现在中文里，进集合
    只会给所有候选段加同样的常数惩罚，反而稀释真正有用的信号。

    大写词**排除句首那一个**：英语句首字母大写是语法强制，句首词是什么
    都大写（"Associative learning is…"），照单全收会把普通名词/形容词
    全灌进锚点集（实测一篇 40 个英文锚点里大半是这种噪声）。真专名在文中
    总会在句中再出现一次（"…says Jan Bielecki."），不靠句首那一次。
    全大写缩写（GDP / US）不受此限——它大写不是因为句首。
    """
    out: set[str] = set()
    for m in _NUM_RE.finditer(text):
        out.add("n:" + _norm_num(m.group()))
    for idx, m in enumerate(_EN_WORD_RE.finditer(text)):
        w = m.group()
        if len(w) < 2 or w.lower() in _EN_ANCHOR_STOP:
            continue
        if w.isupper() or (idx > 0 and w[0].isupper()):
            out.add("w:" + w.lower())
    return out


def _zh_anchors(text: str) -> set[str]:
    """从中文段抽取锚点：阿拉伯数字 + 中文数字 + 直接夹用的拉丁原词。

    中文译文常把专名音译（Bielecki→比莱茨基），此时原词不出现、锚点失效；
    但人名地名保留原文或「音译（原文）」并列同样常见，抓到就是强信号。
    """
    out: set[str] = set()
    for m in _NUM_RE.finditer(text):
        out.add("n:" + _norm_num(m.group()))
    for m in _ZH_NUM_RE.finditer(text):
        v = _parse_zh_number(m.group())
        if v:
            out.add("n:" + str(v))
    for m in _LATIN_RE.finditer(text):
        out.add("w:" + m.group().lower())
    return out


def _build_anchor_index(
    en_sentences: list[str], zh: list[str]
) -> tuple[list[int], list[list[int]]]:
    """把中英两侧的锚点压成位掩码，供 _dp_align 做 O(1) 的「对得上」判断。

    返回 (每句英文的锚点掩码, 中文区间 [k,j) 的锚点掩码表)。
    锚点集合为空（文章里没有可对照的数字/专名）时返回空表，_dp_align 退化为纯长度。

    三重降噪：① 只留中文侧也出现过的锚点（英文有、译文全篇没有 ⇒ 纯常数惩罚，
    对选择毫无帮助）；② 丢弃文档内出现 >_ANCHOR_MAX_DOC_FREQ 次的高频锚点；
    ③ 位宽超限时按稀有度截断，保留最稀有的那些。
    """
    en_raw = [_en_anchors(s) for s in en_sentences]
    zh_raw = [_zh_anchors(s) for s in zh]
    doc_zh: set[str] = set()
    for s in zh_raw:
        doc_zh |= s
    freq: dict[str, int] = {}
    for s in en_raw:
        for a in s:
            freq[a] = freq.get(a, 0) + 1
    useful = [a for a in doc_zh if freq.get(a, 0) <= _ANCHOR_MAX_DOC_FREQ]
    if not useful or not any(en_raw):
        return [], []
    useful.sort(key=lambda a: (freq.get(a, 0), a))
    useful = useful[:_ANCHOR_MAX_BITS]
    bit = {a: 1 << i for i, a in enumerate(useful)}

    en_masks = [
        _mask_of(s, bit) for s in en_raw
    ]
    m = len(zh)
    # seg_mask[k][j] = zh[k:j] 的锚点并集（k<=j）。上三角，按 j 递增做 OR 前缀。
    seg_mask = [[0] * (m + 1) for _ in range(m + 1)]
    for k in range(m):
        acc = 0
        row = seg_mask[k]
        for j in range(k + 1, m + 1):
            acc |= _mask_of(zh_raw[j - 1], bit)
            row[j] = acc
    return en_masks, seg_mask


def _mask_of(anchors: set[str], bit: dict[str, int]) -> int:
    mask = 0
    for a in anchors:
        b = bit.get(a)
        if b:
            mask |= b
    return mask


def _dp_align(
    en_sentences: list[str], zh: list[str]
) -> tuple[list[str | None], list[int], int]:
    """动态规划对齐：把连续中文句合并成段，逐一对应英文句子。

    翻译实践里「一句英文拆两句中文」「两句英文并一句中文」都常见，
    句数相等也不保证逐句不错位。以句长为代价做全局最优划分：
    - 合并段代价 = |中文段字符数 - 英文句字符数 × 中英长度比|
    - 该句英文暂无译文时按「本应有的译文长度」计罚，防止 DP 靠空配占便宜

    支持**双向**合并不对称场景：
    - 多句中文 → 一句英文（旧能力，k<j 的连续中文段）
    - 连续多句英文 → 共享一段中文（后加。引语交错的文章里，
      「"xxx," he adds. "yyy."」两句常被译成一段中文；旧算法只允许每句英文
      消耗互不重叠的中文区间，逼着 DP 把共享段硬塞给其中一句、
      另一句按空罚处理，连锁把后续全部配对推错位）

    此外叠加**词汇锚点**修正（_build_anchor_index）：配对里英文的数字/专名
    能在该中文段里对上就减 HIT_BONUS，对不上就加 MISS_PENALTY。长度信号在
    连续的短句堆里容易连错，锚点能在长度差不多的候选段之间把正确那段挑出来
    ——但权重刻意小于典型长度差，只在长度难分胜负时才起作用。

    句数一致且逐句长度匹配时，DP 自然给出 1:1；只有存在真实的
    2:1 / 1:2 结构时才给出合并解。返回（每句英文的译文或 None，
    每句英文对应的中文原句数，每句所在共享组的句数 1=独占）。
    """
    n, m = len(en_sentences), len(zh)
    total_en = sum(len(s) for s in en_sentences) or 1
    total_zh = sum(len(s) for s in zh) or 1
    ratio = total_zh / total_en

    prefix = [0]
    for s in zh:
        prefix.append(prefix[-1] + len(s))

    en_masks, seg_mask = _build_anchor_index(en_sentences, zh)
    use_anchor = bool(en_masks) and any(en_masks)

    INF = float("inf")
    # back[i][j] = (k, p)：状态 (i, j) 由 dp[i-p][k] 转移而来，
    # 即第 i-p+1..i 句英文共享中文段 zh[k:j]（p=1 即常规 1:1/多对一）
    dp = [[INF] * (m + 1) for _ in range(n + 1)]
    back_k = [[0] * (m + 1) for _ in range(n + 1)]
    back_p = [[1] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0

    for i in range(1, n + 1):
        expected_i = len(en_sentences[i - 1]) * ratio
        a_mask = en_masks[i - 1] if use_anchor else 0
        # 情形 B 要按 p 预累加组掩码：group_mask[p] = 第 i-p+1..i 句的锚点并集
        group_mask: dict[int, int] = {}
        if use_anchor:
            acc = 0
            for p in range(1, min(i, _DP_MAX_EN_SHARE) + 1):
                acc |= en_masks[i - p]
                group_mask[p] = acc
        for j in range(m + 1):
            best, best_k, best_p = INF, j, 1
            lo = j - _DP_WINDOW if j > _DP_WINDOW else 0
            # 情形 A：当前句单独消耗 zh[k:j]（k 可 = j 表示本句无译文）
            for k in range(lo, j + 1):
                prev = dp[i - 1][k]
                if prev == INF:
                    continue
                seg_len = prefix[j] - prefix[k]
                cost = expected_i if seg_len == 0 else abs(seg_len - expected_i)
                if use_anchor:
                    s_mask = seg_mask[k][j]
                    both = (a_mask & s_mask).bit_count()
                    cost += _ANCHOR_MISMATCH_PENALTY * (a_mask ^ s_mask).bit_count()
                    cost -= _ANCHOR_MATCH_BONUS * both
                if prev + cost < best:
                    best, best_k, best_p = prev + cost, k, 1
            # 情形 B：连续 p 句英文共享 zh[k:j]（p>=2）。组代价按整组英文
            # 的期望总长算；组内不再另收空罚——能共享说明译文就在这段里。
            max_p = min(i, _DP_MAX_EN_SHARE)
            for p in range(2, max_p + 1):
                prev_row = dp[i - p]
                group_expected = sum(
                    len(en_sentences[t]) for t in range(i - p, i)
                ) * ratio
                g_mask = group_mask.get(p, 0) if use_anchor else 0
                for k in range(lo, j + 1):
                    prev = prev_row[k]
                    if prev == INF:
                        continue
                    seg_len = prefix[j] - prefix[k]
                    if seg_len == 0:
                        continue  # 共享组不可能没有译文，空段无意义
                    cost = abs(seg_len - group_expected)
                    cost += (p - 1) * _SHARE_PENALTY_RATIO * group_expected / p
                    if use_anchor:
                        s_mask = seg_mask[k][j]
                        both = (g_mask & s_mask).bit_count()
                        cost += _ANCHOR_MISMATCH_PENALTY * (g_mask ^ s_mask).bit_count()
                        cost -= _ANCHOR_MATCH_BONUS * both
                    if prev + cost < best:
                        best, best_k, best_p = prev + cost, k, p
            dp[i][j] = best
            back_k[i][j] = best_k
            back_p[i][j] = best_p

    aligned: list[str | None] = [None] * n
    seg_lens = [0] * n
    group_of = [1] * n  # 每句所在共享组的句数：1=独占，>1=与邻句共用一段译文
    j = m
    i = n
    while i > 0:
        k, p = back_k[i][j], back_p[i][j]
        seg = "".join(zh[k:j]) if j > k else None
        for t in range(i - p, i):
            aligned[t] = seg
            seg_lens[t] = j - k
            group_of[t] = p
        i -= p
        j = k
    return aligned, seg_lens, group_of


def _align_detail(
    en_sentences: list[str], content_zh: str
) -> tuple[list[str | None] | None, str, int, list[str | None] | None]:
    """对齐并返回（译文列表, 模式, 合并句数, 练习用严格译文）。

    模式：exact=每句英文恰好对应一句中文；approx=存在多句合并、共享或
    某句暂无译文；none=未提供译文/无有效中文句。

    strict 是给练习用的：共享组（连续几句英文并成一段中文）的句子一律置空，
    让练习回退 AI 单句翻译。练习一次只翻一句，参考译文里夹带邻句内容会让
    用户看到「超出这一句」的内容——合并（一句英文拆成两句中文）不受影响，
    那段内容本来就属于这一句。
    """
    content_zh = (content_zh or "").strip()
    if not content_zh or not en_sentences:
        return None, "none", 0, None
    zh = split_zh_sentences(content_zh)
    if not zh:
        return None, "none", 0, None
    aligned, seg_lens, group_of = _dp_align(en_sentences, zh)
    merged = sum(1 for c in seg_lens if c > 1)
    # 共享组里每句的 seg_lens 都等于共享段长度（可能就是 1），只看长度会漏判：
    # 两句英文共用一段译文同样不是逐句对齐，必须显式排除。
    shared = sum(1 for p in group_of if p > 1)
    exact = merged == 0 and shared == 0 and all(c == 1 for c in seg_lens)
    strict = [t if p == 1 else None for t, p in zip(aligned, group_of)]
    return aligned, ("exact" if exact else "approx"), merged, strict


def align_translations(en_sentences: list[str], content_zh: str) -> list[str | None] | None:
    """把文章级中文译文对齐到英文句子，返回与英文等长的译文列表
    （元素为 None 表示该句暂无对应译文）；未提供译文返回 None。

    句数一致时逐句 1:1；句数不一致时不再整篇放弃，而是用动态规划
    找全局最优的「中文连续句段 → 英文单句」映射，详情页会对合并
    配对的句子明确标注，练习中对未配上的句子回退 AI 译文。
    """
    aligned, _, _, _ = _align_detail(en_sentences, content_zh)
    return aligned


def align_practice_translations(
    en_sentences: list[str], content_zh: str
) -> list[str | None] | None:
    """练习用的逐句译文：只保留这一句英文独占的译文，共享句置空。

    与 align_translations 的区别就在共享组：练习一次只翻一句，参考译文
    必须只覆盖这一句。合并（一句英文拆成两句中文）仍保留——内容属于本句；
    共享（连续几句英文并成一段中文）必须放弃，否则会给出夹带邻句内容的参考。
    """
    _, _, _, strict = _align_detail(en_sentences, content_zh)
    return strict


_TERM_PATTERNS: dict[str, re.Pattern] = {}


# ---------------------------------------------------------------------------
# 固定搭配的变体槽位
#
# 问题：固定搭配在词典里以「元语形式」登记（meet **someone** where they are），
# 而真实句子写的是具体指称（meet **them** where **he** **is** at）。逐词精确
# 匹配对这类词条永远抓不到例句，实测 miss 掉用户真实遇到的句子。
#
# 方案：只对**封闭的等价集合**（代词 / 系动词）展开槽位，其余词仍精确匹配。
# 刻意不做通用词形还原（Porter stemming 那类），因为它会把 abundant 匹配上
# abundantly / abundance，污染例句质量——这是选句功能更不能接受的失败。
# ---------------------------------------------------------------------------

# 第三人称单数代词与不定代词：词典里的 someone/they，实际句子可以是 them/he/she…
_PERSON_SLOT = (
    "someone", "somebody", "anyone", "anybody", "everyone", "everybody",
    "one", "ones", "they", "them", "their", "theirs", "he", "him", "his",
    "she", "her", "hers", "it", "its", "we", "us", "our", "ours",
    "you", "your", "yours", "i", "me", "my", "mine",
)
# 系动词：主语换了（they→she），系动词也得跟着换（are→is/was）
_BE_SLOT = (
    "am", "is", "are", "was", "were", "be", "been", "being",
)
# 情态动词与否定/强调助动词：同一个搭配里 can/could/must/would 常互换
_MODAL_SLOT = (
    "can", "could", "may", "might", "must", "shall", "should", "will",
    "would", "ought", "'ll", "'d", "'m", "'re", "'ve", "n't", "not",
)
# 指人名词：词典写 someone，真实句子常写 meet people / meet the student。
# 这类词与代词同指一个成分，故与代词槽合并成一个等价集合；
# 但**不能**反过来把 people 当作「实义词」严格匹配——那正是要修的漏检。
# person/people 的单复数一并纳入（meet a person / meet people 同义）。
_PERSON_NOUN_SLOT = (
    "person", "persons", "people", "folk",
)


# ---------------------------------------------------------------------------
# 同词根屈折（inflection）
#
# 只做「同词根的规则屈折」：recoil -> recoiling、tree -> trees、judge -> judging。
# **刻意不做派生换后缀**（abundant -> abundance / abundantly、kind -> kindness）——
# 派生是另一个词，例句混进来等于给用户错误示范，这是选句功能最不能接受的失败。
# 两条判定线：
#   1. 只在规则明确处生成（-y->ies、-e 去 e+ing/-ed、-es、双写辅音、-f->-ves），
#      不生成 -ance/-ness/-ity/-ly 之类；宁可漏，不错配。
#   2. 短词（<=3 字母）与已在其他槽位里的词不展开，避免 "a"/"be" 这类虚词被搅乱。
# ---------------------------------------------------------------------------

_VOWEL = "aeiou"
_DOUBLE_END = re.compile(r"[^aeiou][aeiou][^aeiouwxy]$")
_Y_AFTER_VOWEL = re.compile(r".*[aeiou]y$")


def _is_consonant_y(word: str) -> bool:
    """辅音 + y（ability→abilities），元音 + y（play）走普通加 s。"""
    return word.endswith("y") and not _Y_AFTER_VOWEL.match(word)


def verb_inflections(word: str) -> set[str]:
    """动词的规则屈折形式：原形 + s/es + ing + ed。

    -ing：die→dying（去 ie）、shy→shying（y→ying）、recoil→recoiling（去 e）、
           stop→stopping（双写末辅音）。
    -ed ：recoil→recoiled（加 d）、carry→carried（y→ied）、stop→stopped（双写）。
    -s  ：try→tries（y→ies）、pass→passes（+es）。
    """
    out = {word}
    # -ing
    if word.endswith("ie"):
        out.add(word[:-2] + "ying")
    if _is_consonant_y(word):
        out.add(word[:-1] + "ying")
    if word.endswith("e") and not word.endswith(("ee", "oe", "ye")):
        out.add(word[:-1] + "ing")
    if _DOUBLE_END.search(word):
        out.add(word + word[-1] + "ing")
    if not any(f.endswith("ing") for f in out):
        out.add(word + "ing")
    # -ed
    if word.endswith("e"):
        out.add(word + "d")
    if _is_consonant_y(word):
        out.add(word[:-1] + "ied")
    if _DOUBLE_END.search(word):
        out.add(word + word[-1] + "ed")
    if not any(f.endswith("ed") for f in out):
        out.add(word + "ed")
    # -s / -es
    if _is_consonant_y(word):
        out.add(word[:-1] + "ies")
    elif word.endswith(("s", "x", "z", "ch", "sh")):
        out.add(word + "es")
    else:
        out.add(word + "s")
    return out


def noun_inflections(word: str) -> set[str]:
    """名词的规则复数：辅音+y→ies、-es 类、-f/-fe→-ves，其余加 s。

    已以 s 结尾的词（books）不再叠 s。
    """
    if word.endswith("s"):
        return {word}
    out = {word}
    if _is_consonant_y(word):
        out.add(word[:-1] + "ies")
    elif word.endswith(("ch", "sh", "ss", "x", "z")):
        out.add(word + "es")
    elif word.endswith("fe"):
        out.add(word[:-2] + "ves")
    elif re.search(r"[^fs]f$", word):
        out.add(word[:-1] + "ves")
    else:
        out.add(word + "s")
    return out


_SLOT_GROUPS: tuple[frozenset[str], ...] = tuple(
    frozenset(group)
    for group in (_PERSON_SLOT + _PERSON_NOUN_SLOT, _BE_SLOT, _MODAL_SLOT)
)
_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
# 词典体例的语法标注：shy away (from sth.) / look forward to (sb/sth) / give up (doing)
# 这类括号**只存在于词典**，真实语料里绝不会原样出现，必须从匹配中剥离，
# 否则按整串匹配必然抓不到句子（用户实测「库里有句子却提示未找到」）。
_ANNOTATION_RE = re.compile(r"\s*[\(\[][^()\[\]]*[\)\]]")
# 不做屈折的功能词/短虚词：展开只会制造噪声（a→as、is→ises 之类）
_NO_INFLECT = frozenset({
    "a", "an", "the", "is", "am", "are", "be", "to", "of", "in", "on", "at",
    "by", "for", "or", "as", "it", "we", "he", "do", "go", "so", "up", "no",
    # 常见虚词/副词/介词：这些词基本不做屈折，展开只会制造垃圾候选
    # （实测 where 被展成 whered/whering/wheres，把 SQL 粗筛条件撑大 3 倍，
    #  且这些形式真实语料里根本不出现）
    "where", "when", "while", "who", "what", "which", "how", "why", "not",
    "if", "then", "than", "into", "out", "off", "over", "under", "about",
    "after", "before", "with", "from", "they", "them", "their", "there",
    "here", "very", "just", "also", "too", "only", "more", "most", "some",
    "any", "all", "each", "both", "other", "such", "own", "same", "such",
    "and", "but", "yet", "nor", "can", "could", "may", "might", "must",
    "shall", "should", "will", "would", "has", "have", "had", "was",
    "were", "been", "being", "does", "did", "done", "one", "two", "you",
})
# 冠词：真实语料里常省略（"climb a tree" vs "climb trees"），按「可选成分」处理。
# 放行 a/an/the/the 的各种省略形态，既避免因冠词缺失漏检，也不会像屈折那样
# 引入派生词风险。
_ARTICLE_OPTIONAL = frozenset({"a", "an", "the"})
# 冠词片段的内部标记：拼接时靠它识别"这段自带分隔"，避免再补一个 \s+
_ARTICLE_PREFIX = "\x00ART\x00"


def strip_annotations(term: str) -> str:
    """剥掉词条里的词典体例括号/方括号标注，并压缩空白。

    只剥**结尾或整段**的成对括号（`(from sth.)`、`[pl.]`），不误伤
    `parent(s)` 这类中间内嵌的复数标记——那种是词条的一部分。
    """
    cleaned = _ANNOTATION_RE.sub("", term or "")
    return " ".join(cleaned.split()).strip()


def _inflections(word: str) -> set[str]:
    """词条的屈折形态集（动词与名词规则形式取并集）。

    同时给出动词形与名词形是刻意的折中：选句接口只拿到词条文本、拿不到词性，
    无法可靠区分；而两者的形式集（recoil/recoiling/... 与 tree/trees）都是
    **同词根屈折**，取并集既覆盖了用户报的三例，又碰不到派生新词
    （abundance / abundantly / kindness 一个都进不来）。

    早先按「长度 <=3 不展开」过滤，结果把 shy / try / fly 这类真需要展开的
    短动词一起误杀了（用户报的 shy away 正是因此修不好），改为按功能词名单排除。
    """
    if word in _NO_INFLECT or len(word) < 2:
        return {word}
    return verb_inflections(word) | noun_inflections(word)


def _alt(group: frozenset[str], word: str) -> str:
    """生成「该词或同组任一变体」的分支，长的排前面（避免 his 先截到 h）。

    末位(?![A-Za-z]) 必须留在分组外：若把词边界一起括进分支，
    (?<!...) 的否定回顾会在 alternation 下产生意外行为。
    """
    variants = sorted(group - {word}, key=lambda v: (-len(v), v))
    parts = [re.escape(word)] + [re.escape(v) for v in variants]
    return "(?:" + "|".join(parts) + ")"


def _variant_regex(term: str) -> str:
    """把词条编译成变体感知正则。

    三层处理：
    1. **剥离词典括号标注**：`shy away (from sth.)` → `shy away`（真实句子不带标注）
    2. **槽位展开**：代词 / 系动词 / 情态词按等价集合互换（someone↔them）
    3. **屈折展开**：其余长词补规则形态（recoil↔recoiling、tree↔trees）
       槽位词不再重复做屈折——它已经是封闭等价集合了。

    词间空白统一放宽成 \\s+，容忍换行与多个空格。

    注意实现顺序：必须**先按词切分、再对每个词单独转义**，最后自己拼分隔符。
    若先对整串 re.escape 再替换空白，空白早已变成 "\\\\ "（反斜杠+空格），
    后续正则再也匹配不到裸 \\s，拼出来的是字面反斜杠，整条模式全部失效。
    """
    term = strip_annotations(term)
    # 先把词条切成 token 流（词 + 其后紧跟的分隔符），再依次拼接。
    tokens: list[tuple[str, str]] = []
    pos = 0
    for m in _WORD_RE.finditer(term):
        if m.start() > pos:
            tokens[-1] = (tokens[-1][0], tokens[-1][1] + term[pos:m.start()])
        tokens.append((m.group(0), ""))
        pos = m.end()
    if pos < len(term) and tokens:
        last_word, last_sep = tokens[-1]
        tokens[-1] = (last_word, last_sep + term[pos:])

    last = len(tokens) - 1
    out: list[str] = []
    for idx, (word, sep) in enumerate(tokens):
        low = word.lower().replace("’", "'")
        if low in _ARTICLE_OPTIONAL:
            # 冠词可选（用户确认："climb trees 和 climb a tree 都是可以的"）。
            # 组自带尾随空白 (?:\s+a\s+)?，连接时不再额外补 \s+，
            # 否则会变成 (?:a\s+)?\s+fish —— 冠词缺席时残留的 \s+ 要求
            # "fish" 前必须还有空白，而 "climb trees" 只有一层空白，直接漏配。
            # 冠词可选（用户确认："climb trees 和 climb a tree 都是可以的"）。
            # 结构：前词 + \s+ + (冠词\s*)? + 后词
            #   "judge a fish" → judge\s+(?:a\s*)?fish
            #   "climb trees"  → climb\s+(?:a\s*)?tree(s)
            # 两条纪律（都是实测踩出来的）：
            #   ① 分隔 \s+ 必须在冠词组**外**——放组内会变成 judge(?:a\s*)?\s+fish，
            #      冠词缺席时仍强制要求 fish 前有空白，而 "climb trees" 的空白
            #      已被 climb 消费，直接漏配。
            #   2. 组内用 \s* 而非 \s+——\s+ 贪婪吃掉 "climb trees" 唯一的空格后
            #      a 匹配失败，回溯时空格已消耗，整串再也匹配不上。
            out.append(_ARTICLE_PREFIX + "(?:" + re.escape(word) + r"\s*)?")
            continue

        slot = next((g for g in _SLOT_GROUPS if low in g), None)
        if slot:
            frag = _alt(slot, low)
        else:
            forms = _inflections(low)
            if len(forms) > 1:
                alts = [re.escape(f) for f in sorted(forms, key=lambda v: (-len(v), v))]
                frag = "(?:" + "|".join(alts) + ")"
            else:
                frag = re.escape(word)
        out.append(frag)

    # 段间分隔：普通片段之间补 \s+；冠词组后**不补**（组内 \s* 已覆盖，
    # 再补就成了 judge\s+(?:a\s*)?\s+fish —— 冠词缺席时多出的 \s+ 会要求
    # fish 前还有一层空白，而"climb trees"只有一个空格）。
    joined = ""
    for i, frag in enumerate(out):
        prev_is_article = i > 0 and out[i - 1].startswith(_ARTICLE_PREFIX)
        if i and not prev_is_article:
            joined += r"\s+"
        joined += frag
    return joined.replace(_ARTICLE_PREFIX, "")


def _term_pattern(term: str) -> re.Pattern:
    """编译目标词的变体感知匹配模式（带缓存，避免每次检索都重编译）。

    词边界用 (?<![A-Za-z]) / (?![A-Za-z]) 而非 \\b：
    \\b 在 term 以非单词字符开头/结尾时行为不稳定，而这里 term 可能带连字符。

    固定搭配（代词/系动词/情态词）按等价集合展开，见上方 _variant_regex 注释；
    普通单词仍要求完整词形一致，abundant 不命中 abundantly / abundance。
    """
    key = normalize_term(term)
    pat = _TERM_PATTERNS.get(key)
    if pat is None:
        if len(_TERM_PATTERNS) > 200:  # 缓存上限，防无界增长
            _TERM_PATTERNS.clear()
        pat = re.compile(
            r"(?<![A-Za-z])" + _variant_regex(term.strip()) + r"(?![A-Za-z])",
            re.IGNORECASE,
        )
        _TERM_PATTERNS[key] = pat
    return pat


def _like_pattern(keyword: str) -> str:
    """转义 LIKE 通配符后生成 %kw% 模式串（仅作为参数值使用）。"""
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _coarse_filters(term: str) -> list[str]:
    """为变体匹配生成 SQL 粗筛关键词。

    粗筛的唯一目的是把候选行从「整表」缩到「可能命中」，最终仍由
    _term_pattern 精筛。若拿整串词条做 LIKE，变体句会在粗筛阶段就被丢掉，
    变体展开就白做了——所以这里逐词取，且**必须带上该词的屈折形态**：
    词条 recoil（粗筛只查 recoil）会把只写了 recoiling 的句子挡在门外，
    正是用户报的「库里有句子却提示未找到」。

    槽位词（代词/系动词）不参与——它们在原串里可能根本没出现，
    拿去 LIKE 等于把不存在的串当过滤条件，会误杀候选。
    """
    terms: list[str] = []
    for m in _WORD_RE.finditer(strip_annotations(term)):
        word = m.group(0)
        low = word.lower().replace("’", "'")
        # 冠词在精筛里已按可选处理，粗筛也必须跳过，否则会要求句子必须含它
        if low in _ARTICLE_OPTIONAL:
            continue
        if any(low in group for group in _SLOT_GROUPS):
            continue
        for form in _inflections(low):
            if form not in terms:
                terms.append(form)
    return terms


def _check_limits(
    title: str, content: str, source_name: str, url: str, content_zh: str = "",
    tags: list[str] | None = None, extra_info: str = "", title_zh: str = "",
) -> None:
    if not title:
        raise ArticleError("文章标题不能为空")
    if not content:
        raise ArticleError("文章内容不能为空")
    if len(title) > TITLE_MAX:
        raise ArticleError(f"标题长度不能超过 {TITLE_MAX} 个字符（当前 {len(title)} 个）")
    if len(title_zh) > TITLE_ZH_MAX:
        raise ArticleError(
            f"中文标题长度不能超过 {TITLE_ZH_MAX} 个字符（当前 {len(title_zh)} 个）"
        )
    if len(content) > CONTENT_MAX:
        raise ArticleError(f"正文长度不能超过 {CONTENT_MAX} 个字符（当前 {len(content)} 个）")
    if len(content_zh) > CONTENT_ZH_MAX:
        raise ArticleError(
            f"中文译文长度不能超过 {CONTENT_ZH_MAX} 个字符（当前 {len(content_zh)} 个）"
        )
    if len(extra_info) > EXTRA_INFO_MAX:
        raise ArticleError(
            f"其他信息长度不能超过 {EXTRA_INFO_MAX} 个字符（当前 {len(extra_info)} 个）"
        )
    tags = tags or []
    if len(tags) > MAX_TAGS_PER_ARTICLE:
        raise ArticleError(
            f"一篇文章最多 {MAX_TAGS_PER_ARTICLE} 个标签（当前 {len(tags)} 个）"
        )
    for name in tags:
        if len(name) > TAG_NAME_MAX:
            shown = name if len(name) <= 30 else name[:30] + "…"
            raise ArticleError(
                f"标签长度不能超过 {TAG_NAME_MAX} 个字符（当前 {len(name)} 个：「{shown}」）"
            )
    if len(source_name) > SOURCE_NAME_MAX:
        raise ArticleError(
            f"来源名称长度不能超过 {SOURCE_NAME_MAX} 个字符（当前 {len(source_name)} 个）"
        )
    if len(url) > URL_MAX:
        raise ArticleError(f"链接长度不能超过 {URL_MAX} 个字符（当前 {len(url)} 个）")


def _insert_sentences(
    conn: sqlite3.Connection, article_id: int, sentences: list[str], zh: list[str | None] | None
) -> None:
    """写入句子；zh 为逐句对齐的译文（可为 None 或含 None 元素，表示该句暂无译文）。"""
    rows = []
    for i, s in enumerate(sentences, start=1):
        rows.append((article_id, i, s, zh[i - 1] if zh else None))
    if rows:
        conn.executemany(
            "INSERT INTO article_sentences (article_id, seq, text, text_zh) VALUES (?, ?, ?, ?)",
            rows,
        )


# 标签输入的分隔符：半角逗号 / 中文逗号 / 顿号（表单里随手输入都认）
_TAG_SPLIT_RE = re.compile(r"[,，、]+")


def normalize_tag_names(value: object) -> list[str]:
    """把标签输入规整为去重列表。

    接受 list[str]（接口传数组）或逗号/中文逗号/顿号分隔的字符串（表单随手输入）。
    逐项去首尾空白、丢弃空项、按原样去重——保留用户输入的大小写形态，
    仅合并精确重复（标签名唯一性同为精确匹配，与旧词汇标签体系一致）。
    """
    if value is None:
        return []
    raw = _TAG_SPLIT_RE.split(value) if isinstance(value, str) else [str(v) for v in value]
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        name = item.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _resolve_tag_ids(conn: sqlite3.Connection, names: list[str]) -> list[int]:
    """按名取标签 ID，不存在的自动创建（用户可随手输入新标签并沉淀复用）。"""
    ids: list[int] = []
    for name in names:
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        ids.append(row[0])
    return ids


def _replace_article_tags(conn: sqlite3.Connection, article_id: int, names: list[str]) -> None:
    """全量替换一篇文章的标签关联（与其它字段的覆盖语义一致）。"""
    conn.execute("DELETE FROM article_tags WHERE article_id = ?", (article_id,))
    if not names:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO article_tags (article_id, tag_id) VALUES (?, ?)",
        [(article_id, tag_id) for tag_id in _resolve_tag_ids(conn, names)],
    )


def _article_tags(conn: sqlite3.Connection, article_id: int) -> list[str]:
    """取一篇文章的标签名（按标签名排序，保证展示顺序稳定）。"""
    rows = conn.execute(
        """SELECT t.name FROM article_tags at
           JOIN tags t ON t.id = at.tag_id
           WHERE at.article_id = ? ORDER BY t.name""",
        (article_id,),
    ).fetchall()
    return [r[0] for r in rows]


#: 批量取标签时单个 IN 子句的参数块大小。
#: SQLite 默认 SQLITE_MAX_VARIABLE_NUMBER=999，取 500 留一倍余量。
_TAGS_CHUNK = 500


def _article_tags_bulk(conn: sqlite3.Connection,
                       article_ids: list[int]) -> dict[int, list[str]]:
    """一次取多篇文章的标签名，返回 {article_id: [标签名...]}（按标签名排序）。

    ★ 为什么要有它 ★
    列表页逐篇调 `_article_tags` 是 N+1：SQL 条数 = 3 + page_size（page_size=20
    即 23 条）。这里用一条 `IN (...)` 把当页全部标签取回，在 Python 里分组。
    结果与逐篇取**逐字相同**：SQL 仍是同一句（只把 `article_id = ?` 换成
    `article_id IN (...)`），ORDER BY t.name 不变，所以每篇文章内的顺序一致；
    没标签的文章给空列表，与逐篇取到的空列表同形。

    ★ 为什么分块 ★
    参数个数超过 SQLite 上限会直接报错。page_size 有上限（见 main.py 的
    list_articles_api），但分块让它对任何 page_size 都成立。
    """
    out: dict[int, list[str]] = {}
    if not article_ids:
        return out
    for i in range(0, len(article_ids), _TAGS_CHUNK):
        chunk = article_ids[i:i + _TAGS_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""SELECT at.article_id, t.name FROM article_tags at
                JOIN tags t ON t.id = at.tag_id
                WHERE at.article_id IN ({placeholders}) ORDER BY t.name""",
            chunk,
        ).fetchall()
        for r in rows:
            out.setdefault(r[0], []).append(r[1])
    return out


def create_article(conn: sqlite3.Connection, data: dict) -> dict:
    """新增外刊文章，按句切分写入 article_sentences。

    用户附带中文译文时，按 DP 全局最优把译文配到每句英文：
    逐句 1:1（align_mode=exact）或存在合并/共享（approx，详情页有标注）。

    落库的 text_zh 是**练习口径**：只写这一句英文独占的译文，被几句共用的
    段落不写——练习一次只翻一句，参考译文里夹带邻句内容会误导。这些句子
    练习时回退 AI 单句翻译；详情页另外实时算一份完整对照（含共享段与标注）。
    文章级译文始终保留，详情页可查看整体对照。
    """
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    source_name = (data.get("source_name") or "").strip()
    url = (data.get("url") or "").strip()
    content_zh = (data.get("content_zh") or "").strip()
    extra_info = (data.get("extra_info") or "").strip()
    # 中文标题（选填）。库里是可空列，None 与空串都归一成 "" 落库 ——
    # 「没填」在存储层统一成空串，读出来再按空串兜底，语义对前端一致。
    title_zh = (data.get("title_zh") or "").strip()
    tags = normalize_tag_names(data.get("tags"))
    _check_limits(title, content, source_name, url, content_zh, tags, extra_info, title_zh)

    sentences = split_sentences(content)
    # 练习口径落库：只写「这一句独占」的译文，共享句留空让练习回退 AI
    zh, align_mode, merged, zh_strict = _align_detail(sentences, content_zh)
    now = now_iso()
    cur = conn.execute(
        """INSERT INTO articles
           (title, source_name, url, content, content_zh, extra_info, title_zh,
            added_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (title, source_name or None, url or None, content, content_zh,
         extra_info, title_zh, now, now),
    )
    article_id = cur.lastrowid
    _insert_sentences(conn, article_id, sentences, zh_strict)
    _replace_article_tags(conn, article_id, tags)
    conn.commit()
    return {
        "id": article_id,
        "title": title,
        "title_zh": title_zh,
        "tags": _article_tags(conn, article_id),
        "sentence_count": len(sentences),
        # 统计口径跟落库一致：只数「这一句独占」的译文，共享句按未配上算
        "translated_count": sum(1 for t in zh_strict if t) if zh_strict else 0,
        "zh_aligned": bool(zh_strict) and any(zh_strict),
        "align_mode": align_mode,
        "merged_count": merged,
    }


def update_article(conn: sqlite3.Connection, article_id: int, data: dict) -> dict:
    """编辑文章；正文变更时重新切分句子，正文或译文变更时重新对齐译文。"""
    row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    if row is None:
        raise ArticleNotFoundError(article_id)

    title = (data.get("title") if data.get("title") is not None else row["title"]).strip()
    content = (
        data.get("content") if data.get("content") is not None else row["content"]
    ).strip()
    source_name = (
        data.get("source_name") if data.get("source_name") is not None else row["source_name"]
    ) or ""
    url = data.get("url") if data.get("url") is not None else row["url"] or ""
    content_zh = (
        data.get("content_zh") if data.get("content_zh") is not None else row["content_zh"]
    ) or ""
    # 其他信息：同样遵循 None 回退原值、空串显式清空的约定。
    # 用 row.keys() 兜底——若连接未经 init_db（测试直接造旧表），
    # row["extra_info"] 会 IndexError，而这里本就该退回空串。
    if data.get("extra_info") is not None:
        extra_info = data["extra_info"] or ""
    else:
        extra_info = (row["extra_info"] if "extra_info" in row.keys() else "") or ""
    # 中文标题：与 extra_info 完全同一套约定（None/缺键 → 保留原值，空串 → 显式清空）。
    # row.keys() 兜底理由同上：测试直接造旧表时 row["title_zh"] 会 IndexError。
    if "title_zh" in data and data["title_zh"] is not None:
        title_zh = data["title_zh"] or ""
    else:
        title_zh = (row["title_zh"] if "title_zh" in row.keys() else "") or ""
    # 标签为全量替换语义：payload 带 tags（含空数组）就按payload替换；
    # 整个键缺失才保留原标签（与 title/content 等字段的缺省回退一致）。
    if data.get("tags") is not None:
        tags = normalize_tag_names(data["tags"])
    else:
        tags = _article_tags(conn, article_id)
    source_name = (source_name or "").strip()
    url = url.strip()
    content_zh = content_zh.strip()
    extra_info = extra_info.strip()
    title_zh = title_zh.strip()
    _check_limits(title, content, source_name, url, content_zh, tags, extra_info, title_zh)

    now = now_iso()
    conn.execute(
        """UPDATE articles
           SET title = ?, source_name = ?, url = ?, content = ?, content_zh = ?,
               extra_info = ?, title_zh = ?, updated_at = ?
           WHERE id = ?""",
        (title, source_name or None, url or None, content, content_zh,
         extra_info, title_zh, now, article_id),
    )

    content_changed = content != row["content"]
    zh_changed = content_zh != (row["content_zh"] or "")
    if content_changed:
        conn.execute("DELETE FROM article_sentences WHERE article_id = ?", (article_id,))
        sentences = split_sentences(content)
        _, _, _, zh = _align_detail(sentences, content_zh)
        _insert_sentences(conn, article_id, sentences, zh)
    elif zh_changed:
        # 正文没变：句子行还在，只需重写每句的译文（先清空再按序号回填）
        rows = conn.execute(
            "SELECT seq, text FROM article_sentences WHERE article_id = ? ORDER BY seq",
            (article_id,),
        ).fetchall()
        _, _, _, zh = _align_detail([r["text"] for r in rows], content_zh)
        conn.execute(
            "UPDATE article_sentences SET text_zh = NULL WHERE article_id = ?", (article_id,)
        )
        if zh:
            conn.executemany(
                "UPDATE article_sentences SET text_zh = ? WHERE article_id = ? AND seq = ?",
                [(t, article_id, i) for i, t in enumerate(zh, start=1) if t],
            )
    _replace_article_tags(conn, article_id, tags)
    conn.commit()

    final_rows = conn.execute(
        "SELECT seq, text, text_zh FROM article_sentences WHERE article_id = ? ORDER BY seq",
        (article_id,),
    ).fetchall()
    count = len(final_rows)
    translated = sum(1 for r in final_rows if r["text_zh"])
    align_mode, merged = "none", 0
    if content_zh and final_rows:
        _, align_mode, merged, _ = _align_detail([r["text"] for r in final_rows], content_zh)
    return {
        "id": article_id,
        "title": title,
        "title_zh": title_zh,
        "tags": _article_tags(conn, article_id),
        "updated_at": now,
        "sentence_count": count,
        "translated_count": translated,
        "zh_aligned": translated > 0,
        "align_mode": align_mode,
        "merged_count": merged,
    }


def delete_article(conn: sqlite3.Connection, article_id: int) -> int:
    """删除文章；article_sentences / article_tags 由外键 CASCADE 清理。"""
    row = conn.execute("SELECT id FROM articles WHERE id = ?", (article_id,)).fetchone()
    if row is None:
        raise ArticleNotFoundError(article_id)
    conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
    conn.commit()
    return article_id


def get_article(conn: sqlite3.Connection, article_id: int) -> dict:
    row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    if row is None:
        raise ArticleNotFoundError(article_id)
    count = conn.execute(
        "SELECT COUNT(*) FROM article_sentences WHERE article_id = ?", (article_id,)
    ).fetchone()[0]
    translated = conn.execute(
        """SELECT COUNT(*) FROM article_sentences
           WHERE article_id = ? AND text_zh IS NOT NULL AND text_zh != ''""",
        (article_id,),
    ).fetchone()[0]
    align_mode = "none"
    if (row["content_zh"] or "").strip():
        texts = [
            r[0]
            for r in conn.execute(
                "SELECT text FROM article_sentences WHERE article_id = ? ORDER BY seq",
                (article_id,),
            )
        ]
        if texts:
            _, align_mode, _, _ = _align_detail(texts, row["content_zh"])
    result = {**dict(row)}
    result.pop("topic", None)  # 旧库可能残留的停用列，不再对外暴露
    # 可空列 → 对外统一成空串：前端拿到 "" 就知道「没填中文标题」，
    # 可以直接判空不显示，不必处理 null（也不该显示 "null"）。
    result["title_zh"] = (row["title_zh"] if "title_zh" in row.keys() else "") or ""
    result.update(
        {
            "tags": _article_tags(conn, article_id),
            "sentence_count": count,
            "translated_count": translated,
            "align_mode": align_mode,
        }
    )
    return result


def list_articles(
    conn: sqlite3.Connection,
    *,
    q: str = "",
    tags: list[str] | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """文章列表：标题/来源/正文关键词搜索 + 多标签 AND 筛选 + 分页，按添加时间倒序。

    列表只取展示所需列（不含正文/译文全文），另给 has_zh 标记是否附带译文、
    tags 给出该文全部标签名（供表格渲染徽标）。
    多标签筛选为 AND 语义：同时勾选多个标签时，只显示带齐全部所选标签的文章。
    """
    q = (q or "").strip()
    tag_names = normalize_tag_names(tags)
    clauses: list[str] = []
    params: list = []
    if q:
        # 中文标题（title_zh）也纳入搜索：用户记不住英文原标题时，
        # 会用中文标题去搜。COALESCE 兜住旧行为空 —— 否则整个条件恒为 NULL，
        # 「没填中文标题」的文章会被静默过滤掉（这是 SQL 三值逻辑的经典坑）。
        clauses.append(
            "(a.title LIKE ? ESCAPE '\\' OR a.source_name LIKE ? ESCAPE '\\' "
            "OR a.content LIKE ? ESCAPE '\\' "
            "OR COALESCE(a.title_zh, '') LIKE ? ESCAPE '\\')"
        )
        like = _like_pattern(q)
        params.extend([like, like, like, like])
    if tag_names:
        # AND 语义：聚合每篇文章命中的标签数，必须等于所选标签数才入选
        placeholders = ",".join("?" * len(tag_names))
        clauses.append(
            f"""a.id IN (
                   SELECT at.article_id FROM article_tags at
                   JOIN tags t ON t.id = at.tag_id
                   WHERE t.name IN ({placeholders})
                   GROUP BY at.article_id
                   HAVING COUNT(DISTINCT t.name) = ?
               )"""
        )
        params.extend(tag_names)
        params.append(len(tag_names))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    total = conn.execute(f"SELECT COUNT(*) FROM articles a {where}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT a.id, a.title, COALESCE(a.title_zh, '') AS title_zh,
                   a.source_name, a.url, a.added_at, a.updated_at,
                   (SELECT COUNT(*) FROM article_sentences s WHERE s.article_id = a.id)
                       AS sentence_count,
                   (a.content_zh IS NOT NULL AND a.content_zh != '') AS has_zh
            FROM articles a {where}
            ORDER BY a.added_at DESC, a.id DESC
            LIMIT ? OFFSET ?""",
        (*params, page_size, (page - 1) * page_size),
    ).fetchall()
    # ★ 一条批量查询取代逐篇查标签（原来是 N+1：SQL 条数 = 3 + page_size）★
    tag_map = _article_tags_bulk(conn, [r["id"] for r in rows])
    items = []
    for r in rows:
        item = {**dict(r), "has_zh": bool(r["has_zh"])}
        item["tags"] = tag_map.get(r["id"], [])
        items.append(item)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def list_tag_names(conn: sqlite3.Connection) -> list[str]:
    """全部已建标签名，按使用文章数倒序、同数按名称排序。

    供前端筛选项渲染：预置标签 ∪ 用户自建标签，自建中用得多的排前面。
    """
    rows = conn.execute(
        """SELECT t.name, COUNT(at.article_id) AS uses
           FROM tags t LEFT JOIN article_tags at ON at.tag_id = t.id
           GROUP BY t.id ORDER BY uses DESC, t.name"""
    ).fetchall()
    return [r[0] for r in rows]


def list_sentences(conn: sqlite3.Connection, article_id: int) -> list[dict]:
    """取一篇文章的全部句子（按原文顺序，含逐句译文与对齐标注）。

    text_zh 用实时对齐结果覆盖库里的值：库里存的是练习口径的「独占译文」，
    共享句留空；详情页要给人看完整对照，所以这里补回来并标注。

    zh_seg：该句英文对应的中文原句数（0=无译文，1=一句，>1=多句合并，
    -1=与相邻句共用一段译文），供详情页标注。
    """
    row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    if row is None:
        raise ArticleNotFoundError(article_id)
    rows = conn.execute(
        "SELECT id, seq, text, text_zh FROM article_sentences WHERE article_id = ? ORDER BY seq",
        (article_id,),
    ).fetchall()
    # 重算对齐：给出每句英文对应几句中文原文（详情页对「合并配对」标注用）
    seg_by_seq: dict[int, int] = {}
    aligned_by_seq: dict[int, str] = {}
    art = conn.execute("SELECT content_zh FROM articles WHERE id = ?", (article_id,)).fetchone()
    if art and (art["content_zh"] or "").strip():
        zh = split_zh_sentences(art["content_zh"])
        if zh:
            aligned, seg_lens, group_of = _dp_align([r["text"] for r in rows], zh)
            # 详情页要的是完整对照：库里按练习口径存的是「独占译文」，
            # 共享句留空了，这里用实时对齐结果补回来并标注。
            aligned_by_seq = {i: t for i, t in enumerate(aligned, start=1) if t}
            seg_by_seq = {i: c for i, c in enumerate(seg_lens, start=1) if c != 1}
            # 连续几句英文指向同一段中文（译文把它们并成了一句）：标 -1，
            # 否则详情页看着像逐句对齐，实际是几句共用同一段译文。
            for i, p in enumerate(group_of, start=1):
                if p > 1:
                    seg_by_seq[i] = -1
    return [
        {
            **dict(r),
            "text_zh": aligned_by_seq.get(r["seq"], r["text_zh"]),
            "zh_seg": seg_by_seq.get(
                r["seq"],
                1 if (aligned_by_seq.get(r["seq"]) or r["text_zh"]) else 0,
            ),
        }
        for r in rows
    ]


def pick_sentences_for_term(
    conn: sqlite3.Connection, term: str, limit: int = SENTENCES_PER_TERM
) -> list[dict]:
    """从本地外刊库检索含 term 的句子，随机取最多 limit 条。

    先用 LIKE 粗筛（C 层执行，快），再用变体正则精筛，
    避免 abundant 命中 abundantly、abundance，同时让固定搭配的变体句
    （meet them where they are at ↔ meet someone where they are）能被抓到。
    """
    term = (term or "").strip()
    if not term:
        raise ArticleError("目标词不能为空")

    select = (
        "SELECT s.id AS sentence_id, s.seq, s.text, s.text_zh, "
        "a.id AS article_id, a.title, a.source_name, a.url "
        "FROM article_sentences s JOIN articles a ON a.id = s.article_id"
    )
    coarse = _coarse_filters(term)
    if coarse:
        # 只用字面词粗筛：变体句可能整串都不含原词条，拿全串 LIKE 会漏
        where = " WHERE " + " OR ".join(["s.text LIKE ? ESCAPE '\\'"] * len(coarse))
        rows = conn.execute(
            select + where, [_like_pattern(w) for w in coarse]
        ).fetchall()
    else:
        # 词条全是槽位词（如 "where they are"），没有字面词可用 → 全表交给正则
        rows = conn.execute(select).fetchall()

    pattern = _term_pattern(term)
    hits = [dict(r) for r in rows if pattern.search(r["text"])]

    if len(hits) > limit:
        hits = random.sample(hits, limit)
    # 随机抽取后按原文顺序排列，便于阅读
    hits.sort(key=lambda h: (h["article_id"], h["seq"]))
    return hits


def count_term_occurrences(conn: sqlite3.Connection, term: str) -> dict:
    """统计某词条在外刊库里的出现情况，供 AI 评分当事实依据。

    返回 {"sentence_hits": n, "article_hits": m}：
    - sentence_hits：命中**句子**数（同一篇文章里出现 3 次算 3 个句子）
    - article_hits：命中**文章**数（同一篇里出现多少次都只算 1 篇）

    匹配口径与 pick_sentences_for_term 完全一致（LIKE 粗筛 + _term_pattern 精筛），
    这样「AI 说这个词在外刊里高频」与用户自己在选句里看到的情况对得上。
    两处若各写一套匹配，日志与选句迟早对不上，AI 的理由也就不可信了。
    """
    term = (term or "").strip()
    if not term:
        return {"sentence_hits": 0, "article_hits": 0}

    select = "SELECT s.text, a.id AS article_id FROM article_sentences s JOIN articles a ON a.id = s.article_id"
    coarse = _coarse_filters(term)
    if coarse:
        where = " WHERE " + " OR ".join(["s.text LIKE ? ESCAPE '\\'"] * len(coarse))
        rows = conn.execute(select + where, [_like_pattern(w) for w in coarse]).fetchall()
    else:
        rows = conn.execute(select).fetchall()

    pattern = _term_pattern(term)
    sentence_hits = 0
    article_ids: set[int] = set()
    for row in rows:
        if pattern.search(row["text"]):
            sentence_hits += 1
            article_ids.add(int(row["article_id"]))
    return {"sentence_hits": sentence_hits, "article_hits": len(article_ids)}


def pick_sentences_for_terms(
    conn: sqlite3.Connection, terms: list[str], limit: int = SENTENCES_PER_TERM
) -> list[dict]:
    """对多个目标词分别选句；某个词在库中检索不到时返回空列表。

    返回结构：[{term, found, sentences: [...]}]，sentence 带 article_id /
    title / source_name / url，便于前端展示来源。
    """
    out = []
    for term in terms:
        if not (term or "").strip():
            raise ArticleError("目标词不能为空")
        hits = pick_sentences_for_term(conn, term, limit=limit)
        out.append({"term": term.strip(), "found": len(hits), "sentences": hits})
    return out


def sentences_from_web(
    url: str, terms: list[str], limit: int = SENTENCES_PER_TERM
) -> list[dict]:
    """从网络外刊抓取正文并选句。**内容一律不入库**，只返回句子与真实链接。

    抓取失败时抛 ArticleError（由上层降级为提示用户手动粘贴），
    不静默返回空——静默会让用户以为"这个词没有例句"。

    ★ UA 必须像浏览器：这里原先用 "Mozilla/5.0 (compatible; EnPractice/0.1)"，
      实测 Cambridge Dictionary / Merriam-Webster / Britannica 全部返回 403。
      现与 app/websearch.py 共用同一个浏览器 UA，两条网络路径口径一致。
    """
    url = (url or "").strip()
    if not url:
        raise ArticleError("外刊链接不能为空")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ArticleError(f"外刊链接必须以 http:// 或 https:// 开头：{url}")

    import requests

    try:
        resp = requests.get(
            url,
            timeout=20,
            headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
        )
        resp.raise_for_status()
        html = resp.text
    except requests.RequestException as exc:
        raise ArticleError(f"外刊抓取失败（{url}）：{exc}") from exc

    text = _html_to_text(html)
    sentences = split_sentences(text)
    if not sentences:
        raise ArticleError(f"未能从该链接解析出正文（{url}），请手动粘贴原文")

    title = _extract_title(html) or url
    out = []
    for term in terms:
        term = (term or "").strip()
        if not term:
            raise ArticleError("目标词不能为空")
        # 复用库内同一套变体匹配（_term_pattern 自带缓存），两条路径口径一致
        pat = _term_pattern(term)
        hits = [
            {
                "sentence_id": None,  # 网络内容不入库，无本地 ID
                "seq": i,
                "text": s,
                "article_id": None,
                "title": title,
                "source_name": _source_from_url(url),
                "url": url,
                "from_web": True,
            }
            for i, s in enumerate(sentences, start=1)
            if pat.search(s)
        ]
        if len(hits) > limit:
            hits = random.sample(hits, limit)
        hits.sort(key=lambda h: h["seq"])
        out.append({"term": term, "found": len(hits), "sentences": hits})
    return out


_TAG_STRIP = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_PARAGRAPH = re.compile(r"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")
_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_ENTITIES = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
    "&nbsp;": " ",
}


def _html_to_text(html: str) -> str:
    """极简正文抽取：去 script/style，优先取 <p> 段落，退回全文去标签。

    刻意做得简单——网络外刊按用户要求"要求不高"，仅作外刊库检索不到时的兜底。
    """
    body = _TAG_STRIP.sub(" ", html or "")
    paragraphs = _PARAGRAPH.findall(body)
    chunks = paragraphs if paragraphs else [body]
    text_parts = []
    for chunk in chunks:
        plain = _TAGS.sub(" ", chunk)
        for entity, char in _ENTITIES.items():
            plain = plain.replace(entity, char)
        plain = re.sub(r"\s+", " ", plain).strip()
        if plain:
            text_parts.append(plain)
    return " ".join(text_parts)


def _extract_title(html: str) -> str:
    m = _TITLE_TAG.search(html or "")
    if not m:
        return ""
    title = _TAGS.sub("", m.group(1))
    for entity, char in _ENTITIES.items():
        title = title.replace(entity, char)
    return re.sub(r"\s+", " ", title).strip()[:TITLE_MAX]


def _source_from_url(url: str) -> str:
    """从 URL 推断刊物名（仅作展示用）。"""
    m = re.match(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else url
