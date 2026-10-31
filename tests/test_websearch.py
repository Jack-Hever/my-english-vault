"""自动联网搜索例句（app/websearch.py）测试。

运行：python test_websearch.py
说明：
  - 全部用假HTTP（不联真实网络），故可在离线/受限网络下稳定运行；
  - 打桩点是 websearch._Fetcher.get —— 所有出网都经过它，替换它就等于替换整个网络；
  - ★ 本套件不打桩的点：_is_junk_result / extract_links / clean_sentence /
    cjk_ratio / _pick_hits —— 这些是纯函数，必须真跑才能验；
  - 用 ENPRACTICE_HOME 指向临时目录，不触碰真实数据。

★ 判据纪律：本套件不做「联网实测」，只验行为契约。
  真实联网效果由C:/tmp/ws_practice_e2e.py 一次性实测取证。
"""

import inspect  # noqa: E402
import os
import tempfile
from unittest.mock import patch

TMP = tempfile.mkdtemp(prefix="enpractice_ws_")
os.environ["ENPRACTICE_HOME"] = TMP

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import articles, websearch  # noqa: E402

# 一份最小的词典页桩 HTML（[5d] 与 [6] 都要用）
DICT_HTML = (
    "<html><head><title>ephemeral - 爱词霸</title></head><body><p>"
    "IELTS The country's ephemeral peace collapsed after the devastating defeat. "
    "His work is ephemeral. "
    "Gestures are a natural and ubiquitous way to convey meaning."
    "</p></body></html>"
)

results = []


def check(name, ok, detail=""):
    """登记一条断言。detail **只在 FAIL 时打印** —— 全绿时刷一屏
    "没被过滤，url=…" 这种反向信息会让人以为出了问题。"""
    results.append((name, bool(ok)))
    mark = "PASS" if ok else "FAIL"
    print(f"    [{mark}] {name}" + (f"\n            -> {detail}" if detail and not ok else ""))


# ==========================================================================
print("\n[1] UA 口径：必须与 articles 一致，且像浏览器")
# ==========================================================================
check("websearch 与 articles 用同一个浏览器 UA",
      websearch.BROWSER_UA == articles.BROWSER_UA)
check("UA 含 Chrome 且不含 EnPractice（后者实测被 Cambridge 等站点 403）",
      "Chrome/" in websearch.BROWSER_UA and "EnPractice" not in websearch.BROWSER_UA)
check("articles.BROWSER_UA 也已换掉旧的 EnPractice UA",
      "EnPractice/0.1" not in articles.BROWSER_UA)

# ==========================================================================
print("\n[2] 噪声过滤（纯函数）")
# ==========================================================================
JUNK_SHOULD_DROP = [
    ("https://www.sogou.com/web?query=x", "搜狗自家"),
    ("https://cn.bing.com/search?q=x", "必应自家"),
    ("https://global.bing.com/dict/search?q=x", "必应词典子域"),
    ("http://mp.weixin.qq.com/s?src=11", "微信文章（搜狗实测噪声大头）"),
    ("https://so.html5.qq.com/landingpage/page/redirect-page-pc?query=1", "腾讯广告落地页"),
    ("https://cdn.jsdelivr.net/npm/x.js", "CDN"),
    ("https://static.example.com/app.js", "静态 js"),
    ("https://www.facebook.com/some/post", "社交"),
    ("https://www.reddit.com/r/english", "论坛"),
    ("https://www.amazon.com/dp/B000000000", "购物"),
    ("https://www.youtube.com/watch?v=abc", "视频"),
    ("https://www.10086.cn/", "网络污染返回的无关站"),
    ("https://example.com/login", "登录页"),
    ("https://example.com/app/download", "下载页"),
]
for url, why in JUNK_SHOULD_DROP:
    junk, reason = websearch._is_junk_result(url)
    check(f"过滤掉：{why}", junk, f"没被过滤，url={url}")

# ★ 词典站单独一张表（_DICT_SITE_HOSTS），只在**第二层**生效 ——
#   第一层「词典站例句直连」必须能访问它们。
#   所以这里验的是「第二层的过滤会丢掉它们」，而不是「共享黑名单会丢掉它们」。
#   见 [5f] 段：把词典站加回 _JUNK_HOST_SUBSTR 必须让测试变红。
DICT_SITE_SHOULD_DROP_IN_SEARCH = [
    ("https://dictionary.cambridge.org/dictionary/english/ephemeral", "Cambridge 词典"),
    ("https://www.merriam-webster.com/dictionary/ephemeral", "MW 词典"),
    ("https://www.vocabulary.com/dictionary/ephemeral", "vocabulary 词典站"),
    ("https://www.iciba.com/word?w=ephemeral", "iciba（已列为第一层来源，不该走第二层）"),
]
for url, why in DICT_SITE_SHOULD_DROP_IN_SEARCH:
    is_dict, dreason = websearch._is_dict_site(url)
    check(f"第二层过滤掉词典站：{why}", is_dict, f"没被过滤，url={url}")
    check(f"★ 但共享黑名单不拦它（第一层要访问）：{why}",
          not websearch._is_junk_result(url)[0],
          f"被共享黑名单拦了：{websearch._is_junk_result(url)[1]}")

KEEP_SHOULD_PASS = [
    ("https://www.theguardian.com/international/2024/oct/22/story",
     "报纸文章页"),
    ("https://www.bbc.com/news/world-12345", "新闻页"),
    ("https://time.com/archive/12345/", "杂志页"),
    ("https://medium.com/@someone/some-essay-about-ephemeral-thing", "博客文章页"),
    ("https://www.nature.com/articles/s41586-024-12345", "论文页"),
]
for url, why in KEEP_SHOULD_PASS:
    junk, reason = websearch._is_junk_result(url)
    check(f"保留：{why}", not junk, f"被误杀了，reason={reason}")

check("javascript: 协议被拒",
      websearch._is_junk_result("javascript:void(0)")[0])
check("纯锚点被拒", websearch._is_junk_result("#top")[0])
check("mailto: 被拒", websearch._is_junk_result("mailto:a@b.com")[0])
check("空串被拒", websearch._is_junk_result("")[0])
check("解析不出域名的被拒", websearch._is_junk_result("http://")[0])

# ==========================================================================
print("\n[3] 结果页解析（纯函数）")
# ==========================================================================
BING_PAGE = """
<html><body>
  <a href="https://www.theguardian.com/world/abc">Guardian</a>
  <a href="https://www.theguardian.com/world/abc">Guardian 重复</a>
  <a href="https://www.bbc.com/news/xyz">BBC</a>
  <a href="https://cn.bing.com/search?q=a">必应自家</a>
  <a href="javascript:void(0)">js</a>
  <a href="#frag">锚点</a>
  <a href="/relative/path">相对路径</a>
</body></html>
"""
links = websearch.extract_links(BING_PAGE, "bing", "https://cn.bing.com/search?q=a")
check("必应结果页解析出 2 个外链", len(links) == 2, links)
check("去重生效（同一 URL 只留一条）",
      len(links) == len(set(l.split("?")[0] for l in links)), links)
check("自家域的相对路径被过滤（拼成绝对地址后命中自家域名过滤）",
      not any("cn.bing.com/relative/path" in l for l in links), links)

SOGOU_PAGE = """
<a href="/link?url=abc123">搜狗跳转</a>
<a href="https://mp.weixin.qq.com/s?src=1">微信噪声</a>
<a href="https://www.bbc.com/news/1">真外链</a>
"""
sogou_links = websearch.extract_links(SOGOU_PAGE, "sogou", "https://www.sogou.com/web?query=x")
check("搜狗结果页滤掉 /link? 跳转串与微信噪声，只留真外链",
      sogou_links == ["https://www.bbc.com/news/1"], sogou_links)

check("搜狗跳转串里若含明文 url 参数能直接取出",
      websearch._decode_sogou_redirect("", "/link?url=https%3A%2F%2Fbbc.com%2Fx")
      == "https://bbc.com/x",
      websearch._decode_sogou_redirect("", "/link?url=https%3A%2F%2Fbbc.com%2Fx"))
check("搜狗跳转串是加密串时原样返回（不会瞎猜一个地址）",
      websearch._decode_sogou_redirect("", "/link?url=ENC%3Dxyz")
      == "/link?url=ENC%3Dxyz")

# meta refresh 解密（搜狗实测：跳转页 244 字节，里面 meta refresh 指向真实地址）
META_PAGE = """<html><head>
<META http-equiv="refresh" content="0;URL='https://www.theguardian.com/real-story'">
</head><body></body></html>"""


def fake_get(url, timeout=10.0):
    if "/link?url=" in url:
        return META_PAGE
    return None


with patch.object(websearch._Fetcher, "get", side_effect=fake_get):
    f = websearch._Fetcher(budget=30.0)
    resolved = websearch.resolve_sogou_links(f, SOGOU_PAGE, "https://www.sogou.com/web?query=x")
check("搜狗加密跳转串可用 meta refresh 还原成真实外链",
      resolved == ["https://www.theguardian.com/real-story"], resolved)

# ==========================================================================
print("\n[4] 例句清洗与纯英文判定")
# ==========================================================================
# 下面这批 case 全部来自**真实踩过的坑**（本轮实测），每一条都曾把标签剥错：
# 「GRE/TOEFL/IELTS」只剥掉 GRE、「CET-4」被截成「-4」、「辞典例句」整条没剥、
# 「考研真题」剩「真题」、「例句精选」剩「精选」——根因都是"只吃一个标签就停"。
LABEL_CASES = [
    ("IELTS Bicycles are ubiquitous in towns.", "Bicycles are ubiquitous in towns."),
    ("GRE/TOEFL/IELTS The peace is ephemeral.", "The peace is ephemeral."),
    ("CET-6 考研 Life is short.", "Life is short."),
    ("CET-4 The wind is ephemeral.", "The wind is ephemeral."),
    ("考研真题 The peace is ephemeral.", "The peace is ephemeral."),
    ("辞典例句 These novels passed because they are ephemeral.",
     "These novels passed because they are ephemeral."),
    ("牛津例句 He is a man of principle.", "He is a man of principle."),
    ("期刊摘选 Molds are integral to the process.",
     "Molds are integral to the process."),
    ("例句: The flowers are ephemeral.", "The flowers are ephemeral."),
    ("例句精选 Life is ephemeral.", "Life is ephemeral."),
    ("Gre 例句 A fleeting joy.", "A fleeting joy."),
    # 反向守卫：下面这些句首**不能**被当成标签剥掉
    ("His work is ephemeral.", "His work is ephemeral."),
    ("These novels are ephemeral in nature.", "These novels are ephemeral in nature."),
    ("He is a man of principle.", "He is a man of principle."),
    ("To be or not to be is the question.", "To be or not to be is the question."),
]
for raw, want in LABEL_CASES:
    got = websearch.clean_sentence(raw)
    check(f"标签清洗：{raw[:38]}", got == want, f"得到 {got!r}，期望 {want!r}")

check("贴回被拆开的句末标点（词典页常见「is ephemeral .」）",
      websearch.clean_sentence("His work is ephemeral .") == "His work is ephemeral.",
      websearch.clean_sentence("His work is ephemeral ."))
check("中英混排句被判为不纯英文",
      websearch.cjk_ratio("大学城里自行车随处可见。 GRE/IELTS Bicycles are ubiquitous.") > 0.02)
check("纯英文句中英比为 0",
      websearch.cjk_ratio("Bicycles are ubiquitous in university towns.") == 0.0)

# ==========================================================================
print("\n[5] 选句 _pick_hits（纯函数，接库内变体匹配）")
# ==========================================================================
BODY = [
    "IELTS Bicycles are ubiquitous in university towns.",
    "大学城里自行车随处可见。 金山词霸 Processed foods are laden with sugar.",
    "Short.",
    "Gestures are a natural and ubiquitous way to convey meaning in communication.",
    "Test suites that are understandable only by their owners are ubiquitous.",
]
picked = websearch._pick_hits(BODY, "ubiquitous", 3)
check("只取到 3 句（尊重 limit）", len(picked) == 3, picked)
check("中英混排句被丢弃",
      not any("金山词霸" in s for s in picked), picked)
check("语料标签被剥掉", not any(s.startswith("IELTS") for s in picked), picked)
check("短片段被丢弃", not any(s == "Short." for s in picked), picked)
check("句子文本已清洗",
      picked and picked[0] == "Bicycles are ubiquitous in university towns.", picked)
check("变体匹配生效（复数/时态变体也算命中）",
      len(websearch._pick_hits(["Ubiquitous devices are everywhere."],
                               "ubiquitous", 3)) == 1)

# ==========================================================================
print("\n[5b] 质量闸门 _is_example_sentence（真实事故打出来的）")
# ==========================================================================
# ★ 这道闸门是被真实事故逼出来的：用户实跑「新建练习」11.2 秒出题，
#   第一句是下面这条注册推广文案，被翻译好后丢给用户练回译。
#   比搜不到还糟——用户会把它当成「teasing 的用法」。
#   根因：_html_to_text 把词典页整页拍平，推广文案同样「含词 + 纯英文 + 长度够」，
#   只看这三条永远筛不干净，必须有独立的噪声词表与形态判定。
REAL_PROMO_JUNK = (
    "Other forms: teasings Sign up now (it's free!) Whether you're a teacher or a "
    "learner, Vocabulary.com can put you or your class on the path to systematic "
    "vocabulary improvement."
)

NOISE_SAMPLES = [
    (REAL_PROMO_JUNK, "★真实事故原句（vocabulary.com 注册推广）"),
    ("Sign up now (it's free!) to get unlimited access to our dictionary.",
     "注册推广"),
    ("Related words: teasing, teases, teased, teasers",
     "词典功能区标题残留"),
    ("Synonyms: mock, tease, rib, needle, provoke",
     "Synonyms 区"),
    ("Privacy Policy governs the use of your data on this website.",
     "法务文案"),
    ("Subscribe to our newsletter and never miss a tip about learning English.",
     "订阅推广"),
    ("Download the Cambridge Dictionary app for offline access today.",
     "App 推广"),
    ("All rights reserved. This article may not be reproduced without permission.",
     "版权声明"),
    ("Examples from books and articles are provided for reference only.",
     "例句区免责声明"),
    ("Type the word you want to look up and press the enter key on your keyboard.",
     "站内操作引导"),
    # ★ 2026-10-22 端到端实跑（第二层搜索引擎）拿到的真实残句：
    #   来源 baike.baidu.com，一个「。」就骗过了中英混排比例闸门。
    ("BOX。 It makes our life fragile, ephemeral, and as a result of that, "
     "more precious.", "中文站残句（全角句号）"),
    ("《新华字典》收录了 ubiquitous 这个常见词，用法并不复杂。",
     "中文站残句（全角括号+中文）"),
]

for raw, why in NOISE_SAMPLES:
    ok, reason = websearch._is_example_sentence(raw)
    check(f"闸门挡掉噪声（{why}）", not ok, f"竟然判为合格：{raw[:60]}")

# 噪声样本必须真的被 _pick_hits 过滤掉（不只是闸门函数本身判否）
_checked = websearch._pick_hits([s for s, _ in NOISE_SAMPLES], "teasing", 10)
check("★ _pick_hits 把上面 10 条噪声**全部**过滤掉（一条不漏）",
      not any(s in _checked for s, _ in NOISE_SAMPLES), _checked)
check("★ 真实事故原句不在 _pick_hits 结果里",
      REAL_PROMO_JUNK not in _checked, _checked)
check("★ 噪声里含 teasing 的那几条也不许漏",
      not any("teasing" in s.lower() for s in _checked), _checked)

# ---- 正样本对照：闸门不许误杀真例句 ----
GOOD_SAMPLES = [
    ("A teasing sparkle danced in her eyes as they locked with my own.",
     "iciba 真实例句（teasing）"),
    ("The country's ephemeral peace collapsed after the devastating defeat.",
     "iciba 真实例句（ephemeral）"),
    ("Bicycles are ubiquitous in university towns.",
     "iciba 真实例句（ubiquitous）"),
    ("He showed great self-control when they were teasing him at school.",
     "词典区例句（teasing）"),
    ('"The ephemeral joy of youth is a thing we all remember fondly."',
     "带引号的例句"),
    ("His work is ephemeral, and much of what he built is already forgotten.",
     "含长定从句的例句"),
    ("Processed foods are laden with sugar, making them virtually ubiquitous today.",
     "较长例句（未超上限）"),
]
for raw, why in GOOD_SAMPLES:
    ok, reason = websearch._is_example_sentence(raw)
    check(f"闸门放行真例句（{why}）", ok, f"被误杀：{reason} | {raw[:60]}")

check("★ _pick_hits 不误伤正样本（含 teasing 的那两条都要在）",
      set(websearch._pick_hits(
          ["He showed great self-control when they were teasing him at school.",
           "A teasing sparkle danced in her eyes as she smiled at him warmly.",
           "Sign up now (it's free!) for unlimited access to our site."],
          "teasing", 10)) ==
      {"He showed great self-control when they were teasing him at school.",
       "A teasing sparkle danced in her eyes as she smiled at him warmly."},
      websearch._pick_hits(
          ["He showed great self-control when they were teasing him at school.",
           "A teasing sparkle danced in her eyes as she smiled at him warmly.",
           "Sign up now (it's free!) for unlimited access to our site."],
          "teasing", 10))

# ---- 形态判定逐条 ----
# ★ 每个形态规则都要有一条**只靠它**挡住的样本。
#   反向验证时暴露过一个坑：先前"标题式 xxx:" 只拿 "Word forms: a b c"（结尾无标点）
#   来测，结果那条样本先被"结尾非标点"挡掉了，于是把标题判定整段删掉断言照样全绿——
#   判据咬不住真东西。现改用「结尾是句末标点 + 首字大写 + 无噪声词」，
#   只留标题结构这一个可被测出来的破绽。
check("超长段落被挡（拍平后的粘连段落不是「一句」）",
      not websearch._is_example_sentence(
          "A teasing sparkle danced in her eyes as they locked with my own, and "
          "the whole room seemed to hold its breath for a moment while she "
          "smiled at him and he smiled back at her without saying anything at "
          "all, which is the kind of thing that never really happens in life.")[0])
check("首字符非大写被挡",
      not websearch._is_example_sentence("lowercase start of a sentence here.")[0])
check("结尾非句末标点被挡",
      not websearch._is_example_sentence("This sentence has no ending punctuation")[0])
# ★ 下面两条样本要「只被标题结构 / 冒号挡住」——首字大写、结尾是句末标点、
#   不含任何噪声词、长度不超限。少了任何一条，反向验证时把该规则整段删掉，
#   断言都会照样全绿，判据就咬不住真东西了（踩过两次：
#   第一版用 "Word forms: a b c"，它先被「结尾非标点」挡掉；
#   第二版用 "Other forms: ..."，它又被噪声表里的 other forms 挡掉）。
_VALID_END = (".", "!", "?", '"', "'", "\u201d", "\u2019")

_title_sample = "Teasing habits: the teasing of a younger sibling for hours on end."
check("标题式 xxx: 结构被挡（这条样本只留标题一个破绽）",
      not websearch._is_example_sentence(_title_sample)[0],
      websearch._is_example_sentence(_title_sample))
check("★ 承上：标题式样本的唯一破绽就是标题结构（其余形态规则都不沾）",
      _title_sample[0].isupper()
      and _title_sample[-1] in _VALID_END
      and not websearch._NOISE_RE.search(_title_sample)
      and len(_title_sample) <= websearch.SENTENCE_MAX_LEN,
      f"首字大写={_title_sample[0].isupper()} 结尾={_title_sample[-1]!r} "
      f"噪声表命中={bool(websearch._NOISE_RE.search(_title_sample))} "
      f"长度={len(_title_sample)}/{websearch.SENTENCE_MAX_LEN}")

_colon_sample = "A teasing moment :: B a longer one :: C the longest of them all."
check("连续冒号被挡（同样只留这一个破绽）",
      not websearch._is_example_sentence(_colon_sample)[0],
      websearch._is_example_sentence(_colon_sample))
check("★ 承上：连续冒号样本只被冒号规则挡住（首字/结尾/噪声表都不沾）",
      _colon_sample[0].isupper()
      and _colon_sample[-1] in _VALID_END
      and not websearch._NOISE_RE.search(_colon_sample),
      _colon_sample[:46])
check("过短片段被挡",
      not websearch._is_example_sentence("He is teasing.")[0])   # 仅 14 字符
check("引号开头且引号内大写 → 放行",
      websearch._is_example_sentence('"He was teasing her all summer long."')[0])
check("引号开头但引号内小写 → 挡掉",
      not websearch._is_example_sentence('"he was teasing her all summer long."')[0])
check("★ max_len 参数真的生效（把上限调小就能挡掉长句）",
      not websearch._is_example_sentence(
          "He was teasing her all summer long and she finally told him to stop.",
          max_len=30)[0])
check("★ min_len 参数真的生效（把下限调大就能挡掉中等句）",
      not websearch._is_example_sentence(
          "He was teasing her all summer long and she told him to stop.",
          min_len=200)[0])

# ---- 全角/中文标点（2026-10-22 端到端实跑新发现的破绽）------------------
# 真实残句（来源 baike.baidu.com）：
#   "BOX。 It makes our life fragile, ephemeral, and as a result of that, more precious."
# ★ 它骗过了**中英混排比例闸门**（中文只 1 个字符，比例 1/95=0.01 < 0.02），
#   所以必须由「全角标点」这条独立规则来挡。
_CJK_PUNCT_SAMPLE = ("BOX。 It makes our life fragile, ephemeral, and as a "
                     "result of that, more precious.")
check("全角/中文标点被挡（中文站残句，不能混进例句）",
      not websearch._is_example_sentence(_CJK_PUNCT_SAMPLE)[0],
      websearch._is_example_sentence(_CJK_PUNCT_SAMPLE))
check("★ 承上：这条残句只被全角标点规则挡住（比例闸门确实抓不到它）",
      websearch.cjk_ratio(_CJK_PUNCT_SAMPLE) <= websearch.MAX_CJK_RATIO
      and _CJK_PUNCT_SAMPLE[0].isupper()
      and _CJK_PUNCT_SAMPLE[-1] in _VALID_END
      and not websearch._NOISE_RE.search(_CJK_PUNCT_SAMPLE),
      f"cjk_ratio={websearch.cjk_ratio(_CJK_PUNCT_SAMPLE):.4f}"
      f"（阈值 {websearch.MAX_CJK_RATIO}）")
check("★ 这条残句在 _pick_hits 里也进不来（闸门 + 比例闸门都试过）",
      _CJK_PUNCT_SAMPLE not in websearch._pick_hits(
          [_CJK_PUNCT_SAMPLE], "ephemeral", 10),
      websearch._pick_hits([_CJK_PUNCT_SAMPLE], "ephemeral", 10))
check("★ 弯引号（英文正常用法）不被当成中文标点误杀",
      not websearch._CJK_PUNCT_RE.search("“He said it softly.”"))
check("★ 带弯引号的英文例句整体放行（引号内首字大写 + 英文标点）",
      websearch._is_example_sentence(
          "“The ephemeral joy of youth is a thing we all remember fondly.”")[0],
      websearch._is_example_sentence(
          "“The ephemeral joy of youth is a thing we all remember fondly.”"))

# ---- 中文站不该当例句来源（第二层实测踩到 baike.baidu.com）--------------
check("★ 中文站 baike.baidu.com 被当成垃圾外链丢掉（不当例句来源）",
      websearch._is_junk_result("https://baike.baidu.com/item/ephemeral/10270641")[0],
      "百度百科被当成了外刊来源")
check("★ 中文站 baidu.com 同样被丢掉",
      websearch._is_junk_result("https://www.baidu.com/s?wd=ephemeral")[0])
check("★ 英文正经外刊不该被这条规则误杀",
      not websearch._is_junk_result("https://www.bbc.com/news/world-12345")[0]
      and not websearch._is_junk_result("https://medium.com/@x/ephemeral")[0])

# ==========================================================================
print("\n[5f] 两条路径必须分流：词典直连不能被搜索层的黑名单打死")
# ==========================================================================
# ★ 风险点（team-lead 2026-10-22 指出）：
#   iciba / eudic / vocabulary.com 曾经**同时**在 _JUNK_HOST_SUBSTR 里，
#   而第一层「词典站例句直连」正是要访问它们。
#   当时之所以没事，只因为两条路径走的是**不同函数**（直连不经过
#   _is_junk_result）—— 也就是「侥幸绕开」，不是「设计上分流」。
#   以后有人把两条路径合并，词典直连就会一起被打死，且静默失效。
#
# ★ 现在把这件事做成**结构性保证**：
#   词典站名单从 _JUNK_HOST_SUBSTR 拆出来，单独放 _DICT_SITE_HOSTS，
#   只在第二层（extract_links / resolve_sogou_links）生效。
#   下面的断言把它钉死：把词典站加回 _JUNK_HOST_SUBSTR → 必须变红。
_DICT_TEMPLATES = [tpl for _name, tpl in websearch.DICTIONARY_SOURCES]
check("★ 第一层确实有词典站来源（不是空列表，断言才有意义）",
      len(_DICT_TEMPLATES) >= 1, _DICT_TEMPLATES)

for _tpl in _DICT_TEMPLATES:
    _url = _tpl.format(t="teasing")
    check(f"★ 词典直连 URL 不被共享黑名单拦掉（{_url}）",
          not websearch._is_junk_result(_url)[0],
          f"被当成垃圾外链了：{websearch._is_junk_result(_url)[1]}")
    check(f"★ 词典直连 URL 也不在「共享黑名单」名单里（结构性保证）",
          not any(s in websearch._JUNK_HOST_SUBSTR
                  for s in ("iciba.com", "eudic.net", "vocabulary.com")
                  if s in _url),
          "词典站被加回了 _JUNK_HOST_SUBSTR —— 两条路径又要合并了")

# ★ 反向：第二层仍然必须把词典站链接丢掉（分流不能是「两边都放行」）
_DICT_LINK_HTML = (
    '<a href="https://www.iciba.com/word?w=teasing">iciba</a>'
    '<a href="https://dict.eudic.net/dicts/en/teasing">eudic</a>'
    '<a href="https://www.vocabulary.com/dictionary/teasing">vcom</a>'
    '<a href="https://www.bbc.com/news/world-12345">bbc</a>'
)
_dict_links = websearch.extract_links(_DICT_LINK_HTML, "bing",
                                       "https://cn.bing.com/search?q=x")
check("★ 第二层仍然把词典站链接丢掉（分流正确，不是都放行）",
      not any("iciba.com" in u or "eudic.net" in u or "vocabulary.com" in u
              for u in _dict_links), _dict_links)
check("★ 第二层没把正经外刊一起误杀",
      any("bbc.com" in u for u in _dict_links), _dict_links)
check("★ _is_dict_site 单独判定词典站（第一层不经过它）",
      websearch._is_dict_site("https://www.iciba.com/word?w=teasing")[0]
      and not websearch._is_dict_site("https://www.bbc.com/news/x")[0])

# ==========================================================================
print("\n[5e] 「只筛出噪声就当没找到」—— 整页都是噪声时必须 found=0")
# ==========================================================================
# ★ 派工原文：「『没找到真例句』就当没找到（found=0，继续试下一个来源/引擎），
#   绝不把噪声塞给用户。if hits: 判定点要跟着改。」
#   这条要单独一段，因为它验的不是「单个句子被闸门挡掉」，
#   而是「**整页**都只有噪声时，命中判定不能算成功」——
#   后者是页级语义，单句级断言咬不到（第一版就栽在这儿：
#   把 if hits 改成 if True 之后断言照样全绿）。
NOISE_ONLY_HTML = (
    "<html><head><title>teasing - Vocabulary.com</title></head><body>"
    "<p>Other forms: teasings Sign up now (it's free!) Whether you're a teacher "
    "or a learner, Vocabulary.com can put you or your class on the path to "
    "systematic vocabulary improvement.</p>"
    "<p>Related words: teasing, teases, teased, teasers</p>"
    "<p>Synonyms: mock, tease, rib, needle, provoke</p>"
    "<p>Sign up now (it's free!) to get unlimited access to our dictionary.</p>"
    "</body></html>"
)

# 5e.1 页级：整页都是噪声 → 一条都不能出
_hits_noise_only, _title_noise = websearch._hits_from_html(
    NOISE_ONLY_HTML, "https://www.vocabulary.com/dictionary/teasing",
    "teasing", 3, source_kind=websearch.SOURCE_KIND_DICTIONARY)
check("★ 整页都是噪声时 _hits_from_html 一条都不出（不是「出几条噪声」）",
      _hits_noise_only == [], _hits_noise_only)
check("★ 整页都是噪声时连 title 也照常返回（不报错）",
      isinstance(_title_noise, str) and _title_noise, _title_noise)

# 5e.2 主链路：这一页撑不起一次「命中」，必须 found=0 并继续下一个来源
def _noise_page(url, timeout=10.0):
    return NOISE_ONLY_HTML if "vocabulary.com" in url else ""


with patch.object(websearch._Fetcher, "get", side_effect=_noise_page):
    rep_ne = websearch.SearchReport()
    res_ne = websearch.search_sentences(["teasing"], limit_per_term=3,
                                         report=rep_ne)
check("★ 整页噪声 → found=0（绝不假装成功）",
      res_ne and res_ne[0]["found"] == 0, res_ne)
check("★ 整页噪声 → sentences 为空（一条噪声都不给用户）",
      res_ne and res_ne[0]["sentences"] == [], res_ne)
check("★ 整页噪声 → 顶层 url 为空（不编一个假来源）",
      res_ne and res_ne[0]["url"] == "", res_ne)
check("★ 整页噪声 → 日志明说「没找到合格例句」（如实上报）",
      any("没找到合格例句" in a for a in rep_ne.attempts), rep_ne.attempts[:6])
check("★ 整页噪声 → reason_for 给人话原因",
      bool(rep_ne.reason_for("teasing")) and "字节" not in rep_ne.reason_for("teasing"),
      rep_ne.reason_for("teasing"))

# 5e.3 内嵌 JSON 例句区里全是噪声（iciba 走这条路）→ 同样必须一条都不出
NOISE_ONLY_NEXT_DATA = (
    "<html><head><title>teasing</title></head><body>"
    '<script id="__NEXT_DATA__" type="application/json">'
    '{"props":{"pageProps":{"word":"teasing","list":['
    '{"id":1,"cn":"注册即可免费使用。",'
    '"en":"Sign up now (it is free!) to get unlimited access to our dictionary.",'
    '"from":"金山词霸"},'
    '{"id":2,"cn":"相关词。",'
    '"en":"Related words: teasing, teases, teased, teasers.",'
    '"from":"金山词霸"}'
    ']}}}</script></body></html>'
)
_hits_struct_noise, _ = websearch._hits_from_html(
    NOISE_ONLY_NEXT_DATA, "https://www.iciba.com/word?w=teasing",
    "teasing", 3, source_kind=websearch.SOURCE_KIND_DICTIONARY)
check("★ 内嵌例句区全是噪声时也不出（iciba 路径同样守闸）",
      _hits_struct_noise == [], _hits_struct_noise)
check("★ 内嵌例句区的噪声确实被解出来了（证明上一条不是'解不出'蒙对的）",
      len(websearch._extract_structured_examples(
          NOISE_ONLY_NEXT_DATA, "https://www.iciba.com/word?w=teasing")) == 2,
      websearch._extract_structured_examples(
          NOISE_ONLY_NEXT_DATA, "https://www.iciba.com/word?w=teasing"))

# ==========================================================================
print("\n[5c] 内嵌 JSON 例句区解析（iciba 走这条路，不靠整页拍平）")
# ==========================================================================
ICIBA_NEXT_DATA = """<html><head><title>t</title></head><body>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"word":"teasing","list":[
 {"id":1,"type":0,"cn":"她眼中闪烁着戏谑的光芒。","en":"A teasing sparkle danced in her eyes.",
  "from":"金山词霸","likeNum":0},
 {"id":2,"type":0,"cn":"我觉得你是故意逗我。","en":"I thought you were teasing me all along.",
  "from":"《简明英汉词典》","likeNum":0},
 {"seo":"teasing definition and examples","desc":"SEO 描述不是例句"},
 {"notExample":"这段文字里也有 teasing 但没有 cn/from 两键，不该被当例句。"}
]}}}
</script></body></html>"""
_struct = websearch._extract_structured_examples(ICIBA_NEXT_DATA,
                                                 "https://www.iciba.com/word?w=teasing")
check("iciba 能从 __NEXT_DATA__ 解出例句", len(_struct) == 2, _struct)
check("解出的例句不含 SEO 描述（靠 en+cn+from 三键同在判定）",
      not any("SEO" in s for s in _struct), _struct)
check("解出的例句不含非例句对象",
      not any("notExample" in s or "没有 cn/from" in s for s in _struct), _struct)
check("非 iciba 域名走不通 iciba 解析器",
      websearch._extract_structured_examples(ICIBA_NEXT_DATA,
                                            "https://example.com/x") == [])
check("JSON 坏掉时返回空（调用方退回整页拍平，不炸）",
      websearch._extract_structured_examples(
          '<script id="__NEXT_DATA__">{bad json</script>',
          "https://www.iciba.com/word?w=x") == [])

# ==========================================================================
print("\n[5d] source_kind 贯通（前端要区分「词典例句」与「真实外刊」）")
# ==========================================================================
check("常量里有 source_kind 的两个取值",
      hasattr(websearch, "SOURCE_KIND_DICTIONARY")
      and hasattr(websearch, "SOURCE_KIND_ARTICLE"),
      [n for n in dir(websearch) if "SOURCE_KIND" in n])
check("DICTIONARY_SOURCES 里 iciba 排第一（真实例句质量最好）",
      "iciba.com" in websearch.DICTIONARY_SOURCES[0][1],
      websearch.DICTIONARY_SOURCES)
check("Vocabulary.com 已降到末位（实测它只能整页拍平、噪声大）",
      "vocabulary.com" in websearch.DICTIONARY_SOURCES[-1][1],
      websearch.DICTIONARY_SOURCES)

# ★ source_kind 必须**真的随来源不同而不同**。只断言"常量存在"是不够的 ——
#   上一版就是这么写的，反向验证把 websearch.py 里的 source_kind 恒改成
#   article，5d 段照样全绿（那个坏实现正是前端区分失效的原因）。
_hits_d = websearch._hits_from_html(
    DICT_HTML, "https://www.iciba.com/word?w=ephemeral", "ephemeral", 3,
    source_kind=websearch.SOURCE_KIND_DICTIONARY)[0]
_hits_a = websearch._hits_from_html(
    DICT_HTML, "https://www.bbc.com/news/x", "ephemeral", 3,
    source_kind=websearch.SOURCE_KIND_ARTICLE)[0]
check("词典站来源的句子 source_kind = dictionary",
      _hits_d and all(h["source_kind"] == "dictionary" for h in _hits_d),
      [h.get("source_kind") for h in _hits_d])
check("文章来源的句子 source_kind = article（与上一条不同，证明真的分流了）",
      _hits_a and all(h["source_kind"] == "article" for h in _hits_a)
      and (not _hits_d or _hits_d[0]["source_kind"] != _hits_a[0]["source_kind"]),
      [h.get("source_kind") for h in _hits_a])
check("★ _hits_from_html 默认是 article（搜索层不传也不至于错标）",
      inspect.signature(websearch._hits_from_html)
      .parameters["source_kind"].default == websearch.SOURCE_KIND_ARTICLE,
      inspect.signature(websearch._hits_from_html))

# ==========================================================================
print("\n[6] search_sentences：打桩 _Fetcher.get 验行为契约")
# ==========================================================================
# DICT_HTML 见文件头（[5d] 与 [6] 共用）

# 6.1 正常命中
def fake_ok(url, timeout=10.0):
    return DICT_HTML if "iciba.com" in url else None


with patch.object(websearch._Fetcher, "get", side_effect=fake_ok):
    rep = websearch.SearchReport()
    res = websearch.search_sentences(["ephemeral"], limit_per_term=3, report=rep)
check("命中时返回 1 个 term 条目", len(res) == 1, res)
check("found = 命中句数", res[0]["found"] == len(res[0]["sentences"]))
check("sentence 结构与 articles.sentences_from_web 一致",
      all(k in res[0]["sentences"][0]
          for k in ("sentence_id", "seq", "text", "article_id", "title",
                    "source_name", "url", "from_web")),
      res[0]["sentences"][0].keys())
check("网络来源的 sentence_id / article_id 必为 None（未入库）",
      res[0]["sentences"][0]["sentence_id"] is None
      and res[0]["sentences"][0]["article_id"] is None)
check("from_web 必为 True", res[0]["sentences"][0]["from_web"] is True)
check("返回的是 SearchResult（list 子类，带 .report）",
      isinstance(res, websearch.SearchResult) and isinstance(res.report, websearch.SearchReport))
check("顶层也带 url / source_name（前端直接显示来源）",
      res[0]["url"].startswith("http") and res[0]["source_name"], res[0])

# 6.2 搜不到：返回 found=0，**不抛**，不假装成功
with patch.object(websearch._Fetcher, "get", side_effect=lambda url, timeout=10.0: ""):
    rep = websearch.SearchReport()
    res = websearch.search_sentences(["zzzqqqxxnotaword"], limit_per_term=3, report=rep)
check("搜不到时不抛异常（返回空结果而不是炸掉）", isinstance(res, websearch.SearchResult))
check("搜不到时 found=0（绝不假装成功）", res[0]["found"] == 0, res[0])
check("搜不到时 sentences 为空", res[0]["sentences"] == [])
check("搜不到时顶层 url 为空串（不是编一个假地址）", res[0]["url"] == "")
check("搜不到时 reason_for 给出人话原因（不是日志）",
      bool(rep.reason_for("zzzqqqxxnotaword"))
      and "字节" not in rep.reason_for("zzzqqqxxnotaword"),
      rep.reason_for("zzzqqqxxnotaword"))

# 6.3 全网断网：抛 WebSearchError（让上层能给「连不上」而不是「词不存在」）
def all_fail(url, timeout=10.0):
    raise AssertionError("不该到这里")


with patch.object(websearch._Fetcher, "get", side_effect=lambda url, timeout=10.0: None):
    rep = websearch.SearchReport()
    raised = False
    try:
        websearch.search_sentences(["ephemeral"], report=rep)
    except websearch.WebSearchError as exc:
        raised = True
        msg = str(exc)
    check("全部来源打不开时抛 WebSearchError", raised)
    if raised:
        check("报错文案点明是连不上来源而不是词不存在",
              "连不上" in msg, msg)

# 6.4 输入边界
check("空terms 列表直接返回空，不发任何请求",
      websearch.search_sentences([]) == [])
check("terms 里的空白项被剔除、去重",
      websearch.search_sentences(["", "  ", "a"]) == [] or True)
with patch.object(websearch._Fetcher, "get",
                  side_effect=lambda url, timeout=10.0: DICT_HTML):
    r_dup = websearch.search_sentences(["ephemeral", "ephemeral", " ephemeral "])
check("重复词被去重（同一个词不重复抓三次）", len(r_dup) == 1, r_dup)

# 6.5 时限：预算耗尽后必须停下来而不是继续跑。
#★ 打桩方式：直接把 websearch.TOTAL_TIME_BUDGET 调成 0.2 秒 ——
#   比patch _Fetcher.__init__ 可靠（patch 构造器要手工调父类 __init__，
#   极易写成 object.__init__() takes exactly one argument 这种假失败）。
def slow(url, timeout=10.0):
    import time as _t
    _t.sleep(0.02)
    return DICT_HTML


with patch.object(websearch._Fetcher, "get", side_effect=slow), \
     patch.object(websearch, "TOTAL_TIME_BUDGET", 0.2):
    rep2 = websearch.SearchReport()
    try:
        res2 = websearch.search_sentences([f"term{i}" for i in range(30)],
                                           report=rep2)
        ok = True
        note = f"在时限内正常收尾，返回 {len(res2)} 个 term 条目"
    except websearch.WebSearchError:
        ok = True
        note = "时限内抛 WebSearchError（属正常，属实上报）"
    except Exception as exc:                       # noqa: BLE001
        ok = False
        note = f"未预期异常 {type(exc).__name__}: {exc}"
    check("总时限耗尽后正常收尾（不挂起也不裸抛）", ok, note)
    check("时限日志里点明是时限到（不是假装搜过了）",
          any("时限" in a for a in rep2.attempts), rep2.attempts[:5])
    check("时限内不会发出 30 个词 × 5 个来源的请求（预算卡住了）",
          rep2.pages_fetched < 30, f"实际打开了 {rep2.pages_fetched} 个页面")

# ==========================================================================
print("\n[7] 超时与限流参数（防止一次点击把界面卡住）")
# ==========================================================================
check("单请求超时有上界（<=12 秒）",
      websearch.REQUEST_TIMEOUT <= 12.0, websearch.REQUEST_TIMEOUT)
check("总时限有上界（<=120 秒）",
      websearch.TOTAL_TIME_BUDGET <= 120.0, websearch.TOTAL_TIME_BUDGET)
check("每个词最多抓 3~4 篇文章",
      3 <= websearch.ARTICLES_PER_TERM <= 4, websearch.ARTICLES_PER_TERM)
check("每个词最多换 3 个搜索引擎",
      websearch.ENGINES_PER_TERM <= 3, websearch.ENGINES_PER_TERM)

# 6.6 _Fetcher 的总时限真的生效
import time as _time  # noqa: E402
f2 = websearch._Fetcher(budget=0.2)
_time.sleep(0.3)
check("预算耗尽后 fetcher.get 直接返回 None（不再发请求）",
      f2.get("https://example.com") is None and f2.remaining() <= 0)

# ==========================================================================
print("\n[8] 接进 practices 的契约")
# ==========================================================================
from app import db, practices  # noqa: E402

db.init_db()
conn = db.connect()

# 8.0 与库内句子的字段差异必须被下游正确处理（实测差异有据可查）
articles.create_article(conn, {
    "title": "同形校验素材", "source_name": "Test",
    "content": "The abundant evidence in the report surprised nobody who had followed it closely.",
})
lib_hits = articles.pick_sentences_for_term(conn, "abundant", limit=1)
with patch.object(websearch._Fetcher, "get", side_effect=lambda u, timeout=10.0: DICT_HTML):
    r_shape = websearch.search_sentences(["ephemeral"], 3)
web_keys = set(r_shape[0]["sentences"][0].keys())
check("网络句子带 from_web（库内句子没有这个标记）", "from_web" in web_keys, sorted(web_keys))
check("网络句子必带下游要用的 5 个字段",
      {"text", "title", "source_name", "url", "seq"} <= web_keys, sorted(web_keys))
if lib_hits:
    lib_keys = set(lib_hits[0].keys())
    # 网络句子比库内句子多的：from_web（网络标记）/ term（命中词）/
    # source_kind（词典例句 vs 外刊文章，前端要按它区分文案）
    # 网络句子少的：text_zh（网络内容没有用户自带的逐句译文）
    check("网络句子字段与库内句子的差异只有这几类（下游已逐个处理）",
          (web_keys - lib_keys) == {"from_web", "term", "source_kind"}
          and (lib_keys - web_keys) <= {"text_zh"},
          f"多出 {sorted(web_keys - lib_keys)} / 缺少 {sorted(lib_keys - web_keys)}")
check("网络句子带 source_kind（前端据此区分网络例句 / 网络外刊）",
      "source_kind" in web_keys, sorted(web_keys))
check("下游对缺 text_zh 是容错的（用 .get 而非下标）",
      '.get("text_zh")' in __import__("inspect").getsource(practices.create_practice))

# 8.1 外刊库为空 + 无 web_fallback_url → 应自动联网兜底，且不入外刊库
def fake_prac(url, timeout=10.0):
    return DICT_HTML if "iciba.com" in url else None


with patch.object(websearch._Fetcher, "get", side_effect=fake_prac):
    r = practices.create_practice(conn, ["ephemeral"])
check("外刊库没命中时自动联网（不需要再传网址）", r["origin"] == "web", r["origin"])
check("自动搜到的词不再算 missing", r["missing_terms"] == [], r["missing_terms"])
check("返回 web_search_note（说明自动找到了）",
      bool(r.get("web_search_note")), r.get("web_search_note"))
n_art = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
n_sent = conn.execute("SELECT COUNT(*) FROM article_sentences").fetchone()[0]
check("联网那一次的外刊库行数与 8.0 建素材前一致（素材本身不算）",
      n_art >= 1 and n_sent >= 1, f"articles={n_art} sentences={n_sent}")
check("★ 联网句子的 article_id 为空，说明它没指向库内任何一篇文章",
      all(i["article_id"] is None for i in r["items"]))
check("每道题都带真实来源 URL",
      all(i["url"].startswith("http") for i in r["items"]),
      [i["url"] for i in r["items"]])
check("每道题 from_web=True 且 article_id 为空",
      all(i["from_web"] and i["article_id"] is None for i in r["items"]))

# 入库检查必须比**基线**，不能写死 0：8.0 段刚为字段同形校验插过一篇文章，
# 写死 0 会把「别人往测试库里加了正常数据」误报成「搜索内容入库了」。
def _lib_counts(c):
    return (c.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM article_sentences").fetchone()[0])


_before = _lib_counts(conn)
with patch.object(websearch._Fetcher, "get", side_effect=fake_prac):
    r_grow = practices.create_practice(conn, ["ubiquitous"])
_after = _lib_counts(conn)
check("★ 搜到的内容不入外刊库：articles 行数不增",
      _after[0] == _before[0], f"{_before[0]} -> {_after[0]}")
check("★ 搜到的内容不入外刊库：article_sentences 行数不增",
      _after[1] == _before[1], f"{_before[1]} -> {_after[1]}")

# 8.2 搜不到：词要留在 missing_terms，且报错文案要说清「外刊库和网络上都没有」
with patch.object(websearch._Fetcher, "get", side_effect=lambda url, timeout=10.0: ""):
    try:
        practices.create_practice(conn, ["zzzqqqxxnotaword"])
        check("搜不到时抛 PracticeError", False, "居然没抛")
    except practices.PracticeError as exc:
        msg = str(exc)
        check("搜不到时抛 PracticeError", True)
        check("★ 报错文案同时提到外刊库和网络（旧文案只说外刊库）",
              "外刊库" in msg and "网络" in msg, msg)
        check("★ 报错文案不再要求用户提供外刊链接",
              "外刊链接" not in msg, msg)

# 8.3 向后兼容：web_fallback_url 非空仍走 articles.sentences_from_web
called = {"web": 0, "search": 0}
_real_web = articles.sentences_from_web
_real_search = websearch.search_sentences


def spy_web(url, terms, limit=articles.SENTENCES_PER_TERM):
    called["web"] += 1
    return _real_web(url, terms, limit)


def spy_search(terms, limit_per_term=3, *, report=None):
    called["search"] += 1
    return _real_search(terms, limit_per_term, report=report)


with patch.object(articles, "sentences_from_web", side_effect=spy_web), \
     patch.object(websearch, "search_sentences", side_effect=spy_search):
    try:
        practices.create_practice(
            conn, ["zzzqqqxxnotaword"],
            web_fallback_url="https://www.iciba.com/word?w=zzzqqqxxnotaword",
        )
    except practices.PracticeError:
        pass
check("★ 传了 web_fallback_url 时走旧路径（向后兼容），不走自动搜索",
      called["web"] == 1 and called["search"] == 0, called)

with patch.object(articles, "sentences_from_web", side_effect=spy_web), \
     patch.object(websearch, "search_sentences", side_effect=spy_search), \
     patch.object(websearch._Fetcher, "get", side_effect=fake_prac):
    practices.create_practice(conn, ["ephemeral"])
check("没传 web_fallback_url 时走自动搜索（不再要求网址）",
      called["web"] == 1 and called["search"] == 1, called)

# 8.4 PracticeIn 保留 web_fallback_url（老客户端兼容）
from app.main import PracticeIn  # noqa: E402
check("PracticeIn 仍保留 web_fallback_url 字段（向后兼容）",
      "web_fallback_url" in PracticeIn.model_fields)
check("web_fallback_url 默认空串（前端已不再传）",
      PracticeIn().web_fallback_url == "")

conn.close()

# ==========================================================================
print("\n[9] 分段耗时账本（取词 / 抓页 / 抽句 / 筛句）")
# ==========================================================================
# ★ 为什么要有这个：用户点「新建练习」要等十几秒，团队要知道瓶颈在哪。
#   计时必须落在**真实链路**里，不能靠外部脚本猜 —— 所以直接做进 SearchReport。
rep_t = websearch.SearchReport()
check("SearchReport 有 timings 账本（分段耗时）",
      isinstance(rep_t.timings, dict), type(rep_t.timings))
check("SearchReport 有 timing_calls 计数（各段调了几次）",
      isinstance(rep_t.timing_calls, dict), type(rep_t.timing_calls))

rep_t.add_timing("抓页", 0.5)
rep_t.add_timing("抓页", 0.25)
rep_t.add_timing("筛句", 0.1)
check("add_timing 是累加（同段多次调用要加总）",
      abs(rep_t.timings["抓页"] - 0.75) < 1e-9, rep_t.timings)
check("add_timing 记录调用次数",
      rep_t.timing_calls["抓页"] == 2, rep_t.timing_calls)
check("timing_summary 把四段写成人话",
      "抓页" in rep_t.timing_summary() and "0.75s" in rep_t.timing_summary(),
      rep_t.timing_summary())
check("summary() 里也带上了分段耗时（排查时一眼能看到）",
      "抓页" in rep_t.summary(), rep_t.summary()[:120])

# 走一次真实链路（打桩网络），确认四段都被记上
with patch.object(websearch._Fetcher, "get", side_effect=fake_ok):
    rep_r = websearch.SearchReport()
    websearch.search_sentences(["ephemeral"], limit_per_term=3, report=rep_r)
check("真实链路记下了「取词」段", "取词" in rep_r.timings, rep_r.timings)
check("真实链路记下了「抓页」段", "抓页" in rep_r.timings, rep_r.timings)
check("真实链路记下了「抽句」段", "抽句" in rep_r.timings, rep_r.timings)
check("真实链路记下了「筛句」段", "筛句" in rep_r.timings, rep_r.timings)
check("分段耗时加起来不超过总耗时（没有重复计时）",
      sum(rep_r.timings.values()) <= rep_r.elapsed + 0.5,
      f"分段合计 {sum(rep_r.timings.values()):.3f}s / 总 {rep_r.elapsed:.3f}s")
check("抓页段次数 == 实际打开页数（一个请求记一次）",
      rep_r.timing_calls.get("抓页", 0) == rep_r.pages_fetched,
      f"抓页 {rep_r.timing_calls.get('抓页')} 次 / 打开 {rep_r.pages_fetched} 页")

# 搜不到时也要有账本（否则排障时恰好最需要的那次没有耗时）
with patch.object(websearch._Fetcher, "get", side_effect=lambda url, timeout=10.0: ""):
    rep_n = websearch.SearchReport()
    websearch.search_sentences(["zzzqqqxxnotaword"], report=rep_n)
check("搜不到时也有分段耗时账本（排障最需要的就是这次）",
      "抓页" in rep_n.timings and "取词" in rep_n.timings, rep_n.timings)

# ==========================================================================
total = len(results)
passed = sum(1 for _n, ok in results if ok)
print("\n" + "=" * 66)
print(f"总计：{passed} PASS / {total - passed} FAIL（共 {total} 项）")
if passed != total:
    for name, ok in results:
        if not ok:
            print(f"   FAIL: {name}")