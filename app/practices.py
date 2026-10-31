"""翻译练习编排：选词 → 选句 → AI 译文 → 我翻译 → AI 点评 → 记录。

流程（用户确认）：
1. 取词 = 当日到期待复习 + 新学的词条（terms 由上层给定）
2. 优先从本地外刊库按词检索句子，随机取 3 个，不足 3 个则有多少取多少
3. 外刊库没有命中时，**自动去网络上搜索**含该词的句子（句子 + 真实网址，
   内容不入库）；使用者不必再提供网址（用户 2026-10-22 明确要求）
4. AI 把英文句译成中文展示给用户
5. 用户看中文译文写出英文（中译英方向）
6. AI 按 rubric（语法30/地道30/语义保真40）点评并记录
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from . import ai, articles, websearch
from .entries import EntryError, now_iso

# 一次练习最多携带的目标词数量 —— **这是默认值，不是硬上限**。
# ★ 用户可以在练习页自己设「每轮词数」（3/5/8/10/15/20）★
#   早先这个值写死为 8，于是「一轮永远 8 个词」。裁剪演示语料到 5 个词之后，
#   用户无论怎么点都凑不满一轮 —— 必须让它可配。
# ★ 不传 max_terms 时行为与改造前**逐字一致**（老客户端 / 桌面零回归）。
MAX_TERMS = 8

# 用户可自选的每轮词数上下限。MAX_TERMS 是默认值，落在区间内。
MIN_TERMS_PER_ROUND = 1
MAX_TERMS_PER_ROUND = 20

# 练习记录保留上限（用户规定）：满 25 条后每新增一条就删掉最老的一条。
# 只作用于 practices（练习流水）。**词汇库 entries 与外刊库 articles 一律不自动删除**——
# 那是用户积累的资产，删掉不可恢复，再乱也不能碰。
# 25 与前端练习历史每页条数（web/app.js 的 state.pageSize）一致：
# 后端留 25 条、前端一页也取 25 条，用户翻页时不会看到「明明还有更多却翻不出来」。
MAX_PRACTICE_RECORDS = 25

# 练习历史列表的单页上限。列表接口不该让人一次拉走整张表——
# 上限比 MAX_PRACTICE_RECORDS 宽（练习流水最多就 25 条），
# 留出余量给未来放宽保留条数，同时挡住手滑传进来的 page_size=99999。
MAX_PAGE_SIZE = 100

# 翻译即复习的及格线：评分满分 = grammar 30 + fluency 30 + fidelity 40 = 100，
# 取「六成」= 60 分。口径与词汇卡复习的「记住 / 没记住」二分对齐：
# 达到这条线说明目标词已能在语境中用对，值得拉长复习间隔；
# 低于这条线视为没掌握，下次立刻重排。阈值随 RUBRIC 量表推导，不另拍数。
SCORE_MAX = sum(spec["max"] for spec in ai.RUBRIC.values())
PASS_SCORE = round(SCORE_MAX * 0.6)


class PracticeError(EntryError):
    """练习业务错误。"""


class PracticeNotFoundError(PracticeError):
    """练习记录不存在。"""

    def __init__(self, practice_id: int) -> None:
        super().__init__(f"练习记录不存在（ID={practice_id}）")
        self.practice_id = practice_id


class PracticeConflictError(PracticeError):
    """采纳修正评分时发现记录已被改动（并发冲突）。

    单列一类并映射到 409：这不是"参数错了"（400），而是"你看到的分数已经过期"，
    用户需要重新看一眼详情再决定，前端据此提示"记录已更新，请重新打开"。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


# 采纳修正评分时在 practices.ai_feedback 里追加的留痕键名。
#
# 不改表结构：ai_feedback 本来就是存模型输出 JSON 的 TEXT 列，
# 解析出来是个 dict，直接在**顶层**挂一个 revisions 数组就能留痕。
# 历史记录解析后没有这个键，get_practice 一律给 []，前端自然显示"无修正记录"。
REVISIONS_KEY = "revisions"


# 评分缺失时的兜底权重。NULL（未评分）**不能等价于 0**：否则用户新录入的词会永远
# 排不进练习，而它们恰恰是最需要练的。取 3.0 = 量表（0–5）中位，含义是「暂按中等看待」。
# 加权与下限筛选共用这一个兜底值，不另立第二套规则。
DEFAULT_RATING = 3.0
# 权重里的基准项。即使两个属性都打了 0 分，词条仍有 0.5 的权重不会永久沉底——
# 评分是主观的，把 0 分词完全排除会让一次误评分不可逆。
# 满分词权重约为 0 分词的 (0.5+1.0)/(0.5+0) = 3 倍（用户确认的强度）。
_RATING_BASE = 0.5
# 候选池放大倍数：评分要真能影响「谁进这 MAX_TERMS 个名额」，
# 只在已截断的结果里重排是没有意义的——得先有富余的候选可挑。
_CANDIDATE_MULTIPLIER = 4


def _rating_value(entry: dict, field: str) -> float:
    """取评分，未评分按 DEFAULT_RATING 兜底。"""
    value = entry.get(field)
    return DEFAULT_RATING if value is None else float(value)


def _due_urgency(entry: dict) -> float:
    """到期紧迫度：逾期越久越大；未到期或无 due_at 记 1.0。

    与评分相乘而非相加：紧迫度（天）和评分（0–5）量纲不同，加法需要人为设定
    「逾期 1 天等于重要性涨几分」这种没有依据的换算；乘法保持「比例放大」语义——
    同等逾期程度下高分更可能被选中，但高分压不过严重逾期的词。
    """
    due = entry.get("due_at")
    if not due:
        return 1.0
    try:
        days_overdue = (datetime.now() - datetime.fromisoformat(str(due))).total_seconds() / 86400.0
    except (TypeError, ValueError):
        return 1.0
    return 1.0 + max(0.0, days_overdue)


def _practice_weight(entry: dict) -> float:
    """练习优先级权重，越大越优先 = 到期紧迫度 × 评分乘子。

    两维取均值而非加权和：需求里二者并列、未说明谁更重要，取均值最中性。
    若后续明确「重要性优先」，改成 (imp*0.6 + use*0.4) 即可。
    """
    imp = _rating_value(entry, "importance")
    use = _rating_value(entry, "usefulness")
    rating_weight = (imp + use) / 2.0 / 5.0  # 0.0–1.0，未评分时 0.6
    return _due_urgency(entry) * (_RATING_BASE + rating_weight)


def _meets_rating(
    entry: dict, min_importance: float | None, min_usefulness: float | None
) -> bool:
    """是否达到评分下限。未评分按 DEFAULT_RATING 参与比较（与加权同一口径）。

    不把未评分当 0（那会把所有新词排除掉），也不当成一定达标（那筛选就形同虚设）。
    """
    if min_importance is not None and _rating_value(entry, "importance") < min_importance:
        return False
    if min_usefulness is not None and _rating_value(entry, "usefulness") < min_usefulness:
        return False
    return True


def _clamp_max_terms(max_terms: int) -> int:
    """把「每轮词数」夹到合法区间。

    ★ 为什么要夹而不是直接用 ★
    前端是下拉框（只会给 3/5/8/10/15/20），但这个接口是公开的 ——
    有人直接 POST 一个 0 或 -5 或 99999 进来时，不能让它把候选池撑爆
    （pool_limit = max_terms * 4，99999 会让 due_entries 去捞十万行）。
    ★ 越界时**夹到最近的合法值**而不是报错 ★
    用户在界面上永远只会拿到合法值，报错只会让「换个数字试一下」变成「整个练习崩了」。
    """
    try:
        n = int(max_terms)
    except (TypeError, ValueError):
        return MAX_TERMS
    if n < MIN_TERMS_PER_ROUND:
        return MIN_TERMS_PER_ROUND
    if n > MAX_TERMS_PER_ROUND:
        return MAX_TERMS_PER_ROUND
    return n


def _resolve_terms(
    conn: sqlite3.Connection,
    terms: list[str],
    *,
    min_importance: float | None = None,
    min_usefulness: float | None = None,
    max_terms: int = MAX_TERMS,
) -> list[str]:
    """校验并去重目标词；空列表时自动取「当日到期 + 近 3 天新学」的词。

    ★ max_terms ★
    本轮最多纳入多少个目标词。不传时用 MAX_TERMS（= 改造前的行为，逐字不变）。
    ★ 库里的词不够时**如实少给**，不报错、不静默补齐 ★
    例如用户设 20 而库里只有 5 个词 → 返回 5 个（不是 0 个，也不是报错）。
    """
    max_terms = _clamp_max_terms(max_terms)
    cleaned, seen = [], set()
    for t in terms or []:
        t = (t or "").strip()
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(t)
        if len(cleaned) >= max_terms:
            break
    if cleaned:
        return cleaned

    # 未指定词时：取当日到期的复习词 + 近 3 天新学的词。
    # 用户显式指定了词就原样返回——那是明确意图，不该被评分排序改写。
    from . import review as review_mod

    pool_limit = max_terms * _CANDIDATE_MULTIPLIER
    candidates: list[dict] = []
    seen_ids: set[int] = set()
    for e in review_mod.due_entries(conn, mode=review_mod.MODE_INTERVAL, limit=pool_limit):
        if e["id"] in seen_ids:
            continue
        seen_ids.add(e["id"])
        candidates.append(e)
    if len(candidates) < pool_limit:
        since = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
        for e in review_mod.new_entries_since(conn, since, limit=pool_limit):
            if e["id"] in seen_ids or e["term"].lower() in seen:
                continue
            seen_ids.add(e["id"])
            candidates.append(e)

    if min_importance is not None or min_usefulness is not None:
        candidates = [
            e for e in candidates if _meets_rating(e, min_importance, min_usefulness)
        ]
        if not candidates:
            # 不许静默返回空练习：那会让用户以为「没词可练」，实际是筛选太严
            raise PracticeError(
                "没有符合评分条件的练习词条：请放宽重要性与实用性的下限，或先给词条打分。"
            )

    if not candidates:
        raise PracticeError(
            "没有可用的练习词条：请先在词汇库添加单词或短语，或等到有条目进入复习周期。"
        )
    # 排序只决定「这 max_terms 个名额归谁」。紧迫度是乘法里的主项（逾期 7 天 = 8 倍，
    # 评分最多差 3 倍），所以严重逾期的词仍会优先；评分只在紧迫度接近时决定胜负——
    # 高分词更常出现，但不会挤掉本该复习的到期词。
    candidates.sort(key=_practice_weight, reverse=True)
    return [e["term"] for e in candidates[:max_terms]]


def _resolve_entry_ids(conn: sqlite3.Connection, terms: list[str]) -> list[int]:
    """把本轮目标词文本解析成 entries.id（存进 passages.focus_entry_ids）。

    字段名承诺的是 id，历史上这里却存了词条文本（'["abundant"]'），
    导致「翻译即复习」无法把练习结果写回对应词条的复习状态。现按
    归一化词条匹配（norm_term 与查重口径一致）取回真实 id。

    词条不存在（用户可能手输了一个库里没有的词去外刊库选句）时跳过，
    该词不参与复习排期——它本来就不在词汇库里，没有可调度的复习状态。
    """
    ids: list[int] = []
    for term in terms:
        row = conn.execute(
            "SELECT id FROM entries WHERE norm_term(term) = norm_term(?) LIMIT 1",
            (term,),
        ).fetchone()
        if row is not None:
            ids.append(row["id"])
    return ids


def create_practice(
    conn: sqlite3.Connection,
    terms: list[str],
    *,
    web_fallback_url: str = "",
    min_importance: float | None = None,
    min_usefulness: float | None = None,
    max_terms: int = MAX_TERMS,
) -> dict:
    """创建一次练习：选词 → 选句（库优先，网络兜底）→ 参考译文。

    网络兜底的口径（用户 2026-10-22 定的）：
      「外刊库里找不到含目标词的句子时，反馈给 AI，AI 自动去网络搜索有关文章
        （搜索到的有关文章不会进入外刊库），而不是让使用者提供网址。」
    所以外刊库没命中的词会**自动**走 app/websearch 去网络上找句子，
    使用者不需要再填网址。web_fallback_url 参数**保留向后兼容**：
    老客户端与既有测试仍会传它，非空时仍走原来的 articles.sentences_from_web 直抓路径。
    ★ 云端例外：上面两条出网路径在云端**都关**（闸门在本函数内的调用点，
      articles.py / websearch.py 一行未改），此时这些词会留在 missing 里并
      附带 `web_failed_reason` 如实说明为什么没找到。

    参考译文优先使用用户添加文章时附带的逐句译文，其次才调 AI 翻译。

    min_importance / min_usefulness 只在**自动选词**时生效：用户显式传了 terms
    就是明确意图，不该再被评分筛选改写。

    ★ max_terms ★
    本轮最多纳入多少个目标词（用户可自选）。库里不够就少给，不报错。
    """
    for name, value in (
        ("重要性", min_importance),
        ("实用性", min_usefulness),
    ):
        if value is None:
            continue
        if value < 0 or value > 5:
            raise PracticeError(f"{name}下限应在 0 ~ 5 之间，收到：{value}")
    resolved = _resolve_terms(
        conn, terms,
        min_importance=min_importance, min_usefulness=min_usefulness,
        max_terms=max_terms,
    )

    picked = articles.pick_sentences_for_terms(conn, resolved)
    missing = [p["term"] for p in picked if not p["found"]]

    # 外刊库没命中的词，去网络上自动找句子（内容不入库）。
    # 两条路径：web_fallback_url 非空 = 老客户端显式给了网址，走原来的直抓；
    # 否则走 websearch 自动搜索。自动搜索补上的词不再算missing。
    #
    # ★ 云端两条全关（闸门加在**调用点**，websearch.py 与 articles.py 都不改）★
    #   · web_fallback_url 直抓 = SSRF：访客给什么 URL 我们就 requests.get 什么，
    #     内网地址与 169.254.169.254 全放行。
    #   · websearch 自动搜索 = 实测 16~19 秒零产出，且共享出口 IP 会被判机器人。
    #   桌面分支**一个字都没改**（cloud 的两个开关在桌面下都放行）。
    #   关闭时如实写 web_failed_reason，绝不静默返回空 ——
    #   静默会让用户以为「这个词不存在」，真实原因是我们主动关了出网。
    from . import cloud

    search_note = ""
    web_failed_reason = ""
    if missing and web_fallback_url and cloud.network_fetch_allowed():
        try:
            web = articles.sentences_from_web(web_fallback_url, missing)
        except articles.ArticleError as exc:
            raise PracticeError(f"外刊库未找到 {missing} 的句子，网络抓取也失败：{exc}") from exc
        by_term = {p["term"]: p for p in picked}
        for item in web:
            if item["found"]:
                by_term[item["term"]] = item
        picked = [by_term[p["term"]] for p in picked]
        missing = []
    elif missing and (web_fallback_url or not cloud.web_search_enabled()):
        # 出网已关：不再往下发那个必然白等十几秒的请求，直接说明原因。
        # ★ 措辞按部署形态选 ★
        #   云端是「平台主动关的」，桌面是「用户自己用 env 关的」——
        #   桌面版文案里绝不能出现「云端」两个字（app/cloud.py 里另有一条）。
        web_failed_reason = (cloud.WEB_PICK_DISABLED_REASON
                             if web_fallback_url
                             else cloud.web_search_disabled_reason())
    elif missing:
        report = websearch.SearchReport()
        try:
            found = websearch.search_sentences(missing, report=report)
        except websearch.WebSearchError as exc:
            # 整个搜索链路跑不起来（断网等）：如实说明，仍然把词留在 missing 里。
            # ★ 不静默返回空——那会让用户以为「这个词不存在」，真实原因是他家断网。
            web_failed_reason = str(exc)
        else:
            by_term = {p["term"]: p for p in picked}
            for item in found:
                if item["found"]:
                    by_term[item["term"]] = {
                        "term": item["term"],
                        "found": item["found"],
                        "sentences": item["sentences"],
                    }
            picked = [by_term[p["term"]] for p in picked]
            still_missing = [p["term"] for p in picked if not p["found"]]
            missing = still_missing
            if missing:
                # 逐词说清为什么没找到（搜了但真没有 vs 压根没搜成）
                search_note = "；".join(
                    f"「{t}」{report.reason_for(t)}" for t in missing
                )
            else:
                search_note = "已自动去网络上找到含这些词的句子"

    sentences = []
    for item in picked:
        for s in item["sentences"]:
            sentences.append({**s, "term": item["term"]})
    if not sentences:
        detail = f"（自动联网搜索的情况：{search_note or web_failed_reason}）" \
            if (search_note or web_failed_reason) else ""
        raise PracticeError(
            "外刊库和网络上都没有找到含这些词的句子，可以把这些词的文章粘贴进外刊库"
            f"{detail}"
        )

    # 组装素材：同一句被多个目标词命中时只保留一次（不重复消耗翻译/点评），
    # 但必须把**所有命中该句的词都记下来**——否则「翻译即复习」会给排期漏词：
    # 早先只保留首个词的 term，另一个词练过了却永远不会被排进复习。
    unique_texts, seen_text = [], {}
    for s in sentences:
        key = s["text"]
        if key in seen_text:
            seen_text[key]["terms"].append(s["term"])
            continue
        merged = {**s, "terms": [s["term"]]}
        seen_text[key] = merged
        unique_texts.append(merged)

    # 参考译文：句子带了用户自己附带的译文（text_zh）时优先用它——
    # 这是用户信任的对照，也不消耗 AI 调用；没有时才让 AI 翻译。
    # 缺译文的句子合并成**一次**调用：逐句调用时 N 句 = N 次网络往返，
    # 5 道题就要等 5 轮，这是「点开始练习后卡住」的主因。
    # 行数对不齐时 translate_batch 内部会自行降级回逐句调用。
    pending_idx = [i for i, s in enumerate(unique_texts)
                   if not (s.get("text_zh") or "").strip()]
    batch = ai.translate_batch([unique_texts[i]["text"] for i in pending_idx])
    batch_map = {idx: batch[n] for n, idx in enumerate(pending_idx)}

    refs = []
    is_mock = False
    for i, s in enumerate(unique_texts):
        own_zh = (s.get("text_zh") or "").strip()
        if own_zh:
            refs.append({"text_zh": own_zh, "zh_from": "user"})
            continue
        item = batch_map.get(i, {"text": "", "is_mock": False})
        is_mock = is_mock or item["is_mock"]
        refs.append({"text_zh": item["text"], "zh_from": "ai"})

    origin = "library"
    source_url = None
    for s in unique_texts:
        if s.get("from_web"):
            origin = "web"
            source_url = s.get("url")
            break

    now = now_iso()
    # 全部命中过任一句的词（不是每句的首词），否则同句共现的词会漏掉排期
    all_terms = []
    for s in unique_texts:
        for t in s["terms"]:
            if t not in all_terms:
                all_terms.append(t)
    focus_ids = _resolve_entry_ids(conn, all_terms)
    cur = conn.execute(
        """INSERT INTO passages
           (title, source_url, origin, text_en, text_zh, focus_entry_ids, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            "翻译练习素材",
            source_url,
            origin,
            "\n\n".join(s["text"] for s in unique_texts),
            "\n\n".join(r["text_zh"] for r in refs),
            json.dumps(focus_ids),
            now,
        ),
    )
    practice_id = cur.lastrowid
    conn.commit()

    return {
        "id": practice_id,
        "origin": origin,
        "source_url": source_url,
        "is_mock": is_mock,
        "created_at": now,
        # ★ 「每轮词数」的如实反馈 ★
        #   requested_terms = 用户设的（或默认 8）；actual_terms = 实际纳入的。
        #   两者不等时前端会说明「你设了 N 个，库里目前有 M 个可练的词」——
        #   不静默截断，否则用户会以为自己数错了。
        #   ★ 用目标词数（resolved）而不是题目数（items）★
        #   一轮 5 个词可能出 14 道题（每词多句），题数≠词数，说「练了 14 个词」是错的。
        "requested_terms": max_terms,
        "actual_terms": len(resolved),
        "items": [
            {
                # 展示用主词（该句首次命中它的目标词）；terms 含同句命中的全部词
                "term": s["terms"][0],
                "terms": s["terms"],
                "text_en": s["text"],
                "text_zh": r["text_zh"],
                "zh_from": r["zh_from"],
                "article_id": s.get("article_id"),
                "article_title": s.get("title"),
                "source_name": s.get("source_name"),
                "url": s.get("url"),
                "from_web": bool(s.get("from_web")),
                # ★ 来源种类（dictionary=词典例句 / article=真实外刊文章）。
                #   前端据此显示「来源：网络例句」还是「来源：网络外刊」——
                #   拿词典例句说成「外刊」是错的，用户会以为那是外刊原句。
                #   库内句子没有这个键，统一按 article（外刊库）处理。
                "source_kind": s.get("source_kind", "article"),
            }
            for s, r in zip(unique_texts, refs)
        ],
        # 自动联网搜索补上的词已经不在 missing 里了（上面重算过missing），
        # 所以这里直接返回：库内没命中 + 网络也没搜到的词。
        "missing_terms": missing,
        # 搜索过程的人话说明（成功说明 / 未搜到原因 / 网络不可达原因）。
        # 前端可展示；★ 没有它用户就只知道「没找到」，不知道是断网还是真没有。
        "web_search_note": search_note or web_failed_reason,
    }


def _focus_entry_ids(row: sqlite3.Row) -> list[int]:
    """读出 passages.focus_entry_ids 中的 entry_id 列表。

    兼容历史数据：旧版本该字段存的是**词条文本**（'["abundant"]'）而非 id。
    解析时只接受整数，纯文本一律忽略——把单词当 id 用会误改到别的词条的
    复习状态，那比不写更糟。
    """
    raw = row["focus_entry_ids"] if "focus_entry_ids" in row.keys() else None
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: list[int] = []
    for item in data:
        # bool 是 int 的子类，需显式排除，否则 True 会被当成 id 1
        if isinstance(item, int) and not isinstance(item, bool) and item > 0:
            out.append(item)
    return out


def _apply_review_to_entries(
    conn: sqlite3.Connection, entry_ids: list[int], review: dict
) -> list[dict]:
    """把本轮翻译评分写回目标词条的复习状态（翻译即复习）。

    判定口径：总分 ≥ PASS_SCORE 记「记住」并拉长间隔，否则「没记住」重置。
    **mock 评分（未配置 AI 密钥）不参与排期**——那是演示数据不是用户水平，
    拿它调度复习会凭空把词条推到几天后，用户真实的掌握情况被覆盖。
    """
    if review.get("is_mock") or not entry_ids:
        return []
    from . import review as review_mod

    remembered = review["total"] >= PASS_SCORE
    out = []
    for entry_id in entry_ids:
        try:
            out.append(review_mod.mark_reviewed(conn, entry_id, remembered))
        except EntryError:
            # 词条已被删除：跳过，不让整个提交失败
            continue
    return out


def _sentence_at(text: str, seq: int) -> str:
    """按段落取出第 seq 句（1-based）。create_practice 用 "\\n\\n" 合并句子。

    句数不足时退回整段，而不是抛错——宁可给个宽口径，也不能让提交失败。
    """
    segs = [s for s in (text or "").split("\n\n") if s.strip()]
    if not segs:
        return text or ""
    if 1 <= seq <= len(segs):
        return segs[seq - 1]
    return "\n\n".join(segs)


def _prune_old_practices(conn: sqlite3.Connection) -> int:
    """只保留最近 MAX_PRACTICE_RECORDS 条练习记录，删掉更早的。

    练习流水是过程数据，看多了没意义，且会一直涨。词汇库与外刊库不受此规则约束。
    返回本次删除的条数（0 表示未触发）。
    """
    total = conn.execute("SELECT COUNT(*) FROM practices").fetchone()[0]
    if total <= MAX_PRACTICE_RECORDS:
        return 0
    keep_rows = conn.execute(
        "SELECT id FROM practices ORDER BY created_at DESC, id DESC LIMIT ?",
        (MAX_PRACTICE_RECORDS,),
    ).fetchall()
    keep_ids = [r["id"] for r in keep_rows]
    placeholders = ",".join("?" * len(keep_ids))
    cur = conn.execute(
        f"DELETE FROM practices WHERE id NOT IN ({placeholders})", keep_ids
    )
    conn.commit()
    return cur.rowcount or 0


def submit_translation(
    conn: sqlite3.Connection,
    practice_id: int,
    user_text: str,
    *,
    seq: int = 1,
    terms: list[str] | None = None,
) -> dict:
    """提交翻译，AI 点评 + 评分 + 记录。

    **一次只翻一句**：练习页是按 item 逐题作答的，所以点评必须拿「当前这一句」
    去比对，而不是整篇 passage。历史上这里传的是合并全文，导致三个后果：
    保真度因「漏翻其余几句」乱扣分、更优表达被模型复述成整段、
    结果页显示的原文/译文也超出本题范围。

    seq 为题号（1-based）。terms 是本句命中的目标词，用于把排期只写回这几个词——
    本轮其他句子的词条不该因为用户还没做到就被记成绩。
    """
    row = conn.execute("SELECT * FROM passages WHERE id = ?", (practice_id,)).fetchone()
    if row is None:
        raise PracticeNotFoundError(practice_id)
    user_text = (user_text or "").strip()
    if not user_text:
        raise PracticeError("翻译内容不能为空")

    try:
        idx = max(1, int(seq))
    except (TypeError, ValueError):
        idx = 1
    source_en = _sentence_at(row["text_en"], idx)
    reference_zh = _sentence_at(row["text_zh"], idx)

    review = ai.review_translation(source_en, reference_zh, user_text)
    now = now_iso()
    cur = conn.execute(
        """INSERT INTO practices
           (passage_id, user_text, ai_feedback, score_grammar, score_fluency,
            score_fidelity, score_total, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            practice_id,
            user_text,
            json.dumps(review, ensure_ascii=False),
            review["grammar"]["score"],
            review["fluency"]["score"],
            review["fidelity"]["score"],
            review["total"],
            now,
        ),
    )
    record_id = cur.lastrowid
    conn.commit()

    # 翻译即复习：把本句评分写回**本句命中的**词条。
    # 只写本句词条：其余句子的词用户还没练到，不该提前记成绩。
    # 取 passage.focus_entry_ids 的子集做校验——前端传来的词文本若不在本轮范围内，
    # 解析出的 id 会落到别的词条上，那会把无关词条的复习排期改掉，比不写更糟。
    focus_ids = _focus_entry_ids(row)
    if terms is not None:
        # 新客户端：只写本句词条。空数组是「本句没有词条」的明确表达，
        # 不回退整轮 —— 回退会把没练到的词也记上成绩。
        wanted = set(_resolve_entry_ids(conn, terms))
        entry_ids = [i for i in focus_ids if i in wanted]
    else:
        # 旧客户端（键缺失）：退回整轮，保持历史行为，避免升级后彻底不排期
        entry_ids = focus_ids
    review_updates = _apply_review_to_entries(conn, entry_ids, review)

    # 记完这一条就裁剪：练习流水只留最近 10 条。放在最后，避免影响本次返回。
    pruned = _prune_old_practices(conn)

    return {
        "id": record_id,
        "practice_id": practice_id,
        "is_mock": review.get("is_mock", False),
        "created_at": now,
        "scores": {
            "grammar": review["grammar"],
            "fluency": review["fluency"],
            "fidelity": review["fidelity"],
            "total": review["total"],
        },
        "suggestion": review["suggestion"],
        # 只回本题那一句：结果页展示的原文/译文必须与用户刚翻的范围一致
        "source_en": source_en,
        "reference_zh": reference_zh,
        # 本次因超出上限而清理掉的最老练习记录条数（前端可提示）
        "pruned": pruned,
        # 本次复习排期变化（前端用于提示「X 天后再见」）
        "review_updates": [
            {
                "entry_id": u["entry_id"],
                "term": u["term"],
                "remembered": u["remembered"],
                "interval_days": u["interval_days"],
                "due_at": u["due_at"],
            }
            for u in review_updates
        ],
        "pass_score": PASS_SCORE,
    }


def list_practices(conn: sqlite3.Connection, *, page: int = 1, page_size: int = 20) -> dict:
    """练习历史列表（含每次评分），按时间倒序。

    列表只给**摘要**（原句、分数、用户翻译），完整点评在 get_practice() 里取。
    """
    page = max(1, int(page))
    # 单页上限在这里也夹一次：API 层已按 MAX_PAGE_SIZE 校验过，
    # 但这个函数也可能被直接调用（测试、脚本），自己夹住才不漏。
    page_size = min(MAX_PAGE_SIZE, max(1, int(page_size)))
    total = conn.execute("SELECT COUNT(*) FROM practices").fetchone()[0]
    rows = conn.execute(
        """SELECT p.id, p.created_at, p.score_total, p.score_grammar, p.score_fluency,
                  p.score_fidelity, p.user_text, g.origin, g.source_url, g.text_en
           FROM practices p LEFT JOIN passages g ON g.id = p.passage_id
           ORDER BY p.created_at DESC, p.id DESC
           LIMIT ? OFFSET ?""",
        (page_size, (page - 1) * page_size),
    ).fetchall()
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def get_practice(conn: sqlite3.Connection, practice_id: int) -> dict:
    """单条练习记录的完整内容（详情弹窗用）。

    ★ id 是 **practices 表的行 id**（列表接口 items[].id 就是它），
    不是 passages 的 id。历史记录的 id 必须唯一指向「这一次翻译」，
    所以这里以 practices 为主表，passages 只用来补原句与参考译文
    （passage 被删时 passage_id 置空，因此用 LEFT JOIN）。
    """
    row = conn.execute(
        """SELECT p.id, p.created_at, p.user_text, p.ai_feedback,
                  p.score_grammar, p.score_fluency, p.score_fidelity, p.score_total,
                  g.text_en, g.text_zh, g.origin, g.source_url
           FROM practices p LEFT JOIN passages g ON g.id = p.passage_id
           WHERE p.id = ?""",
        (practice_id,),
    ).fetchone()
    if row is None:
        raise PracticeNotFoundError(practice_id)
    item = dict(row)
    # ai_feedback 存的是模型输出的 JSON 文本，**原样透传**：
    # 解析放在前端做（前端要按维度展开 issues），后端不因解析失败就吞掉原文。
    # 唯一加工：把采纳修正留下的 revisions 单独取出来作为一个真实字段，
    # 免得前端每次渲染都要重复解析一遍 JSON 才能知道修正过没有。
    item["ai_feedback"] = item.get("ai_feedback") or ""
    item["revisions"] = _read_revisions(item["ai_feedback"])
    return item


def _parse_feedback(raw) -> dict:
    """把 ai_feedback 解析成 dict；解析不了或不是对象时返回 {}（不抛错）。

    脏数据不该让功能整块失效：ai_feedback 可能被截断、可能是空、可能是旧格式。
    """
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _read_revisions(raw) -> list[dict]:
    """从 ai_feedback 里取出修正评分留痕；没有或结构不对时返回 []。"""
    revisions = _parse_feedback(raw).get(REVISIONS_KEY)
    if not isinstance(revisions, list):
        return []
    return [r for r in revisions if isinstance(r, dict)]


def _append_revision(raw, entry: dict) -> str:
    """把一条修正留痕写进 ai_feedback 文本，返回新的 JSON 文本。

    **不新增表结构**：复用 ai_feedback 这一列（它本来就是存模型输出 JSON 的 TEXT）。
    脏数据（解析不了的 ai_feedback）不阻断采纳——原值原样塞进 _raw_feedback 保留，
    修正留痕另起一个合法 JSON。否则一次历史脏数据会让用户永远无法采纳修正评分。
    """
    if not isinstance(entry, dict):
        raise PracticeError("修正留痕必须是对象")
    feedback = _parse_feedback(raw)
    if not feedback:
        # 解析失败：把原文留在 _raw_feedback 里，不丢用户/模型的原话
        if raw:
            feedback = {"_raw_feedback": str(raw)}
        else:
            feedback = {}
    revisions = feedback.get(REVISIONS_KEY)
    if not isinstance(revisions, list):
        revisions = []
    revisions = [r for r in revisions if isinstance(r, dict)]
    revisions.append(entry)
    feedback[REVISIONS_KEY] = revisions
    return json.dumps(feedback, ensure_ascii=False)


def discuss(
    conn: sqlite3.Connection,
    practice_id: int,
    question: str,
) -> dict:
    """就一条练习记录与 AI 讨论评分。**绝不写数据库。**

    上下文与首次点评完全对齐：原句英文、参考中文译文、用户当时的翻译、
    首次三维评分与 issues、suggestion，外加用户这次的新提问。
    少了 issues 那一段，模型无从知道用户到底在质疑哪一条扣分。

    素材（passage）被删时仍可讨论——此时原句/参考译文为空串，
    模型会依据"（无记录）"作答。这比直接报错好：用户要问的往往是评分本身。
    """
    if not isinstance(question, str) or not question.strip():
        # 用 PracticeError（→400）而不是让它走到 ai 层抛 AIError（→502）：
        # 空提问是用户的输入问题，报"服务故障"会把他引到错误的方向上去查网络。
        raise PracticeError("请先写下你的疑问")

    row = conn.execute(
        """SELECT p.id, p.user_text, p.ai_feedback,
                  g.text_en, g.text_zh
           FROM practices p LEFT JOIN passages g ON g.id = p.passage_id
           WHERE p.id = ?""",
        (practice_id,),
    ).fetchone()
    if row is None:
        raise PracticeNotFoundError(practice_id)

    result = ai.discuss_practice(
        source_en=row["text_en"] or "",
        reference_zh=row["text_zh"] or "",
        user_en=row["user_text"] or "",
        first_review=_parse_feedback(row["ai_feedback"]),
        question=question,
    )
    return {"practice_id": int(practice_id), **result}


def _normalize_old_score(value) -> float | None:
    """把库里的旧分读成 float；NULL / 非法值返回 None（表示"原来没分"）。"""
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num == num else None  # NaN 视作无值


def adopt_revised_scores(
    conn: sqlite3.Connection,
    practice_id: int,
    scores: dict,
    total: float,
    *,
    question: str = "",
    reply: str = "",
    expected_old: dict | None = None,
) -> dict:
    """把 AI 的修正评分写回该条练习记录。

    ★ 只有用户点「采纳修正评分」才会走到这里 ★
    （与 ai_ratings 同一条原则：AI 的判断是建议，用户保留否决权；
      不采纳就关掉弹窗，原评分一个字节都不动。）

    四道校验，缺一不可：
    1. **分数合法性**：交给 ai._validate_discuss_scores，越界/缺项/total 对不上直接拒绝。
       绝不静默夹取——夹取会让用户以为 AI 真给过那个分，而库里躺着另一个来源不明的值。
    2. **先读旧值**：UPDATE 前把当前分数读出来，既用于留痕（改前→改后），
       也用于并发比对。
    3. **并发冲突**：expected_old 带上用户看到的旧分；与库里当前值不一致说明
       期间有人改过（另一个窗口采纳过、或记录被重算），此时拒绝写入并报冲突，
       **不覆盖别人的写入**。
    4. **留痕**：在 ai_feedback 里追加一条 revisions（时间 / 提问 / AI 回复 /
       改前分 / 改后分），用户事后能查到"这个分为什么被改过"。
    """
    if not isinstance(scores, dict) or not isinstance(total, (int, float)) \
            or isinstance(total, bool):
        raise ai.DiscussScoreError("修正评分结构不合法")

    validated, validated_total = ai._validate_discuss_scores(
        {**scores, "total": total}
    )

    row = conn.execute(
        """SELECT id, ai_feedback, score_grammar, score_fluency,
                  score_fidelity, score_total
           FROM practices WHERE id = ?""",
        (practice_id,),
    ).fetchone()
    if row is None:
        raise PracticeNotFoundError(practice_id)

    # 先读旧值：留痕要用，也用来做并发比对
    old = {
        "score_grammar": _normalize_old_score(row["score_grammar"]),
        "score_fluency": _normalize_old_score(row["score_fluency"]),
        "score_fidelity": _normalize_old_score(row["score_fidelity"]),
        "score_total": _normalize_old_score(row["score_total"]),
    }

    if isinstance(expected_old, dict):
        for key in ("score_grammar", "score_fluency", "score_fidelity", "score_total"):
            if key not in expected_old:
                continue
            want = _normalize_old_score(expected_old[key])
            if want != old[key]:
                raise PracticeConflictError(
                    "这条练习记录的分数在你讨论期间已被改动，"
                    "为避免覆盖他人的修改，本次修正未写入。请重新打开详情确认后再采纳。"
                )

    now = now_iso()
    entry = {
        "at": now,
        "question": str(question or ""),
        "reply": str(reply or ""),
        "before": {k: (v if v is not None else 0) for k, v in old.items()},
        "after": {**validated, "total": validated_total},
    }
    feedback_text = _append_revision(row["ai_feedback"], entry)

    cur = conn.execute(
        """UPDATE practices
           SET score_grammar = ?, score_fluency = ?, score_fidelity = ?,
               score_total = ?, ai_feedback = ?
           WHERE id = ?""",
        (
            validated["grammar"],
            validated["fluency"],
            validated["fidelity"],
            validated_total,
            feedback_text,
            practice_id,
        ),
    )
    if cur.rowcount != 1:
        # 记录在读与写之间被删了：不能当成成功
        raise PracticeNotFoundError(practice_id)
    conn.commit()

    return {
        "id": int(practice_id),
        "before": old,
        "scores": {**validated, "total": validated_total},
        "applied_at": now,
    }


def practice_stats(conn: sqlite3.Connection) -> dict:
    """进步追踪：练习次数、平均/最近总分、最高分。"""
    row = conn.execute(
        """SELECT COUNT(*) AS n, AVG(score_total) AS avg_total,
                  MAX(score_total) AS best
           FROM practices"""
    ).fetchone()
    last = conn.execute(
        "SELECT score_total, created_at FROM practices ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    first = conn.execute(
        "SELECT score_total, created_at FROM practices ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    return {
        "count": row["n"] or 0,
        "avg_total": round(row["avg_total"], 1) if row["avg_total"] else None,
        "best_total": row["best"],
        "last_total": last["score_total"] if last else None,
        "first_total": first["score_total"] if first else None,
    }
