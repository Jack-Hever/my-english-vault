# -*- coding: utf-8 -*-
"""演示语料（seed data）—— 公开部署用的初始内容。

★ 版权原则（活动规则第 5 条「不得侵犯第三方著作权」）：
  本文件中**全部自撰文章均为本项目自撰**（原创，不含任何外刊原文）；
  `PUBLIC_DOMAIN_ARTICLE` 单独标注为 Project Gutenberg 公有领域作品。
  ★ 严禁把真实库里的 The Economist / NYT / Esquire / Psychology Today /
    Sixth Tone / Shanbay 内容放进本文件 —— 那是第三方版权内容。

★ 结构约定：每篇文章 = 若干「句对」，英文句与中文句**严格 1:1**。
  这样 article_sentences.text_zh 逐句落库后，练习时不需要 AI 密钥
  就有参考译文，评审打开链接即可跑通核心闭环。

★ 难度：英文平均词长控制在 4.2~4.9（B1~B2）。

★ 规模（2026-10-24 裁剪后）：**2 篇 / 41 句对 / 5 词条**。
  · 自撰 1 篇《Learning a Language After Twenty》（26 句对 / 471 词）
  · 公有领域 1 篇《The Time Traveller》（15 句对）
  · 词条 5 个，全部来自第一篇
  （原来是 7 篇 / 171 句对 / 60 词条 —— 设计约束「词和文章又不是产品内容」，
    演示语料只是让人上手的样例，不该喧宾夺主。）
  ★ 老访客的旧库不会因为改这个文件而自动瘦身 ——
    删除型裁剪在 `app/seed_migrate.py`，触发点挂在 `app/main.py` 的中间件。

★ ⚠️ 标签数已卡死上限，加第6 个会让**新访客首屏直接报错** ⚠️
  业务层 `app/articles.py` 的 `MAX_TAGS_PER_ARTICLE = 5` 是**硬校验**：
  `create_article` 一旦收到第 6 个标签就抛 `ArticleError`，整篇文章建不出来。
  而云端是在 `cloud.ensure_tenant_db` 里灌种子的 —— 一篇失败 = 那个访客的
  库建不出来 = 首屏 500。**所以给文章加标签前先减一个。**
  · 当前状态（2026-10-24）：2 篇**全部恰好 5 个，已卡满**。
  · 想要更多标签只有两条路：① 复用已有标签 ② 改 `MAX_TAGS_PER_ARTICLE`（会动
    桌面业务代码，需评估回归）。
  · 灌库路径**不会**帮你裁 —— `app/seed.py` 的 `declared_tags()` 是硬报错，
    宁可当场炸也不静默丢标签（静默裁剪会让"语料不合规"伪装成"灌库成功"）。
  · 改完本文件请跑 `python tools/validate_seed.py`（B12/B13 会查这件事）。
"""

# ---------------------------------------------------------------------------
# 自撰文章
# 每个元素：
#   {
#     "title": str, "title_zh": str, "source_name": str, "topic": str,
#     "tags": [str, ...],
#     "pairs": [(英文句, 中文句), ...],
#   }
# ★ 约束：pairs 的每一对里，英文句必须以 . ! ? 之一结尾（切句器据此切），
#   中文句必须以 。 之一结尾。不得在句中使用会导致误切的内容
#   （例如句中的 "e.g." / "Mr." / 省略号 / 句中问号）。
#   ★ 一句之内**不得出现第二个句号**，否则切句器会把一句切成两句、
#     破坏中英 1:1；需要并列时用逗号 / 从句 / 分号 / 破折号连接。
# ---------------------------------------------------------------------------

ARTICLES: list[dict] = [
    {
        "title": "Learning a Language After Twenty",
        "title_zh": "二十岁之后学一门语言",
        "source_name": "本项目原创",
        "topic": "语言学习",
        "tags": ["语言学习", "原创", "B1-B2", "英语学习", "日常表达"],
        "pairs": [
            ("Adults often believe that children have a special gift for languages, and that gift disappears with age.",
             "成年人常相信孩子有学语言的天赋，而这份天赋会随年龄消失。"),
            ("Children are not gifted, they are simply given a much larger share of one thing, which is time.",
             "孩子并不天才，他们只是得到了大得多的某种东西，那就是时间。"),
            ("A child meets a new word in the shop, in a park, and again at dinner, without ever deciding to study it.",
             "孩子在商店、在公园、又在晚饭时遇到一个新词，从没刻意去学它。"),
            ("An adult usually meets the word inside a lesson, and then must carry it alone for the next week.",
             "成年人通常在课程里遇到这个词，然后必须独自背着它走过接下来的一周。"),
            ("The gap is not ability, it is the number of encounters, and encounters can be manufactured.",
             "差距不在能力，而在于相遇的次数，而相遇是可以人为制造的。"),
            ("If you learn one word a day and meet it five times that same week, it becomes yours.",
             "如果你每天学一个词，并且同一周内遇到它五次，它就属于你了。"),
            ("Reading alone does not give you enough encounters, because you choose the text and can avoid the hard parts.",
             "只靠阅读不足以给你足够的相遇，因为书是你自己选的，你可以绕开难的部分。"),
            ("This is why a sentence matters more than a word.",
             "这就是为什么一个句子比一个单词更重要。"),
            ("A word in a sentence has a job, and the job makes the word easy to recall.",
             "句子里的词有职责，而职责让词变得容易记住。"),
            ("Once you can use the word, you can keep using it, but once you only recognise it, you forget it.",
             "一旦你能用这个词，你就能一直用下去，可一旦只是认得它，你就会忘掉它。"),
            ("So the useful habit is to collect sentences, not lists.",
             "所以有用的习惯是收集句子，而不是收集词表。"),
            ("Keep a small number of sentences that you personally want to say.",
             "留下一小批你个人想说出口的句子。"),
            ("Return to them often, rehearse them once, and use them in real messages within a day or two.",
             "经常回看它们，先复述一遍，再在一天两天之内把它们用进真实的消息里。"),
            ("The grammar will be imperfect, but the word will stay.",
             "语法会不完美，但那个词会留下来。"),
            ("Fluency is not a level you reach, it is a habit of meeting sentences that you can actually finish.",
             "流利不是某个你抵达的等级，而是一种去读那些你真能读完的句子的习惯。"),
            ("A word you can use is worth more than ten words you can only recognise.",
             "一个你能用出来的词，胜过十个你只能认出来的词。"),
            ("Good retention comes from returning to the same few sentences, not from collecting more.",
             "好的记忆保持来自反复回到同样那几句，而不是收集更多。"),
            ("You can rehearse a sentence before you ever need it, by saying it out loud once.",
             "你可以在真正需要之前就排练一个句子，只要出声说一遍。"),
            ("A new word that you never use is a mistake you will pay for every week.",
             "一个你从来不用的新词，是你每周都要付一次代价的错误。"),
            ("Review a short list aloud in the morning, and the same words come back easily at night, because retention needs a rhythm and recall needs repetition.",
             "早上出声复习一张短表，同一批词晚上就容易回来，因为记忆保持需要节奏，而回忆需要重复。"),
            ("A sentence you can say without stopping is a sentence you own, and the words inside it are yours too, and that is fluency.",
             "一个你能不停顿说出来的句子，就是你拥有的句子，里面的词也一样，而那就是流利。"),
            ("The mistake most adults make is waiting for a natural moment, and no natural moment ever arrives, so review on a fixed day instead.",
             "多数成年人最常犯的错是等待一个自然的时刻，而自然的时刻从来不会来，所以改成固定某天复习。"),
            ("Each encounter with a word in a real message counts double, because you had a reason to look it up.",
             "在真实消息里与一个词相遇，份量要算两倍，因为你有理由去查它。"),
            ("Fluency is not speed, and a review that makes you feel slow is often the one that keeps the words, because rehearsing is cheaper than regretting.",
             "流利不是速度，而让你觉得慢的那次复习，往往才是保住那些词的复习，因为排练比后悔便宜。"),
            ("Retention improves when you say the sentence out loud, because speaking leaves a stronger trace than reading does.",
             "把句子说出声时，记忆保持会更好，因为开口留下的痕迹比阅读更深。"),
            ("Rehearse the awkward line until it stops being awkward, and you will be able to recall it under pressure, so the mistake of sounding stiff will disappear.",
             "把那句别扭的话练到不再别扭，你就能在压力下回想起来，语调生硬这个错误也会消失。"),
        ],
    },
]

# ---------------------------------------------------------------------------
# 公有领域文章（Project Gutenberg，标注来源以满足「资料来源与版权声明」）
# 英文正文逐字摘自公版文本（H. G. Wells《The Time Machine》ebook #35）；
# 中文译文为本项目自撰（译文亦为原创）。
# ★ 挑选原则：以第一章开篇的连续叙述为主，句子长度 45~220 字符、
#   平均词长 4.4~4.9（B1~B2），便于与自撰文章体量一致。
# ★ 句子必须逐句列出（不是整块文本），以便与中文严格 1:1 对齐。
# ★ 全部英文句均可在原文中逐字检索到（摘录，未改写）。
# ---------------------------------------------------------------------------
PUBLIC_DOMAIN_ARTICLE: dict = {
    "title": "The Time Traveller (excerpt from H. G. Wells, 1895)",
    "title_zh": "时间旅行者（选自 H·G·威尔斯《时间机器》，1895）",
    "source_name": "Project Gutenberg（公有领域）",
    "url": "https://www.gutenberg.org/ebooks/35",
    "topic": "科普经典",
    "tags": ["公有领域", "科普", "Gutenberg", "B1-B2", "英语学习"],
    "pairs": [
        ("The Time Traveller (for so it will be convenient to speak of him) was expounding a recondite matter to us.",
         "时间旅行者（为方便起见，我们就这么称呼他）正在向我们讲述一个晦涩的问题。"),
        ("His grey eyes shone and twinkled, and his usually pale face was flushed and animated.",
         "他灰色的眼睛闪闪发亮，平日里苍白的脸此刻涨得通红，生动异常。"),
        ("The fire burnt brightly, and the soft radiance of the incandescent lights in the lilies of silver caught the bubbles that flashed and passed in our glasses.",
         "炉火烧得很旺，银制灯盏里白炽灯柔和的光芒，照亮了我们杯中一闪而过的小气泡。"),
        ("Our chairs, being his patents, embraced and caressed us rather than submitted to be sat upon, and there was that luxurious after-dinner atmosphere, when thought runs gracefully free of the trammels of precision.",
         "我们的椅子是他的专利，它们拥抱着我们、爱抚着我们，而不是被动地承受我们的重量，饭后那种舒适的气氛正是如此，思想在其中摆脱了精确的束缚，优雅地自由驰骋。"),
        ("And he put it to us in this way—marking the points with a lean forefinger—as we sat and lazily admired his earnestness over this new paradox (as we thought it) and his fecundity.",
         "他就是这样向我们提出他的看法的——用一根瘦长的食指点着要点——我们坐在那里，懒洋洋地欣赏他讲这个新悖论（我们这么以为）时的认真，以及他的多才多艺。"),
        ("I shall have to controvert one or two ideas that are almost universally accepted.",
         "我得把一两个几乎被普遍接受的观念辩一辩。"),
        ("But through a natural infirmity of the flesh, which I will explain to you in a moment, we incline to overlook this fact.",
         "不过由于肉体天生的局限——这一点我待会儿会解释——我们往往忽略这个事实。"),
        ("There are really four dimensions, three which we call the three planes of Space, and a fourth, Time.",
         "其实有四个维度，其中三个我们称为空间的三个平面，第四个就是时间。"),
        ("But some foolish people have got hold of the wrong side of that idea.",
         "不过有些蠢人抓住了这个想法错误的那一面。"),
        ("Professor Simon Newcomb was expounding this to the New York Mathematical Society only a month or so ago.",
         "西蒙·纽科布教授大约一个月前才向纽约数学学会讲过这个。"),
        ("For instance, here is a portrait of a man at eight years old, another at fifteen, another at seventeen, another at twenty-three, and so on.",
         "比如说，这里有一张八岁时的人像，还有一张十五岁的、十七岁的、二十三岁的，如此类推。"),
        ("Here is a popular scientific diagram, a weather record.",
         "这是一张常见的科学图解，是一份天气记录。"),
        ("This line I trace with my finger shows the movement of the barometer.",
         "我用手指描出的这条线，显示了气压计的移动。"),
        ("Yesterday it was so high, yesterday night it fell, then this morning it rose again, and so gently upward to here.",
         "昨天它那么高，昨夜降了下去，今早又升了回来，就这样缓缓地升到这里。"),
        ("Our mental existences, which are immaterial and have no dimensions, are passing along the Time-Dimension with a uniform velocity from the cradle to the grave.",
         "我们的精神存在是无形的、没有维度，正以恒定的速度沿着时间维度，从摇篮走向坟墓。"),
    ],
}

# ---------------------------------------------------------------------------
# 入门词表（5 词）：词汇库 + 重要性/实用性评分 + 关联到上面的文章
# importance / usefulness 用 0~5 的整数（与现有星级评分一致）
# ★ 每个 term 在其 article 的正文里至少命中 3 句，练习池够深；
#   usage 均为该文章正文里真实存在的一整句（逐字相同）。
# ★ 评分分配（实测命中句数）：
#   fluency 3 / encounter 3 / mistake 3 / retention 3 / sentence 9
#   ⇒ 25 种 (importance, usefulness) 阈值组合**每一组都至少剩 1 个词**
#     （imp>=5 且 use>=5 时只剩 fluency）⇒ 筛选不会静默返 0。
#   ★ 词条数是「大约 5 个」—— 设计约束「我说的是大约五个词」，
#     不是精确 5 个；所以判据 C3 的口径是「筛选太严时给人话提示，
#     不静默返 0」，而不是「每种组合都必须有词」。
# ---------------------------------------------------------------------------
VOCABULARY: list[dict] = [
    {"term": "encounter", "pos": "n./v.", "meaning_zh": "相遇；遭遇",
     "usage": "The gap is not ability, it is the number of encounters, and encounters can be manufactured.",
     "importance": 4, "usefulness": 4, "article": "Learning a Language After Twenty", "tags": ["语言学习"]},
    {"term": "retention", "pos": "n.", "meaning_zh": "记忆保持；留存",
     "usage": "Good retention comes from returning to the same few sentences, not from collecting more.",
     "importance": 3, "usefulness": 5, "article": "Learning a Language After Twenty", "tags": ["语言学习"]},
    {"term": "fluency", "pos": "n.", "meaning_zh": "流利程度",
     "usage": "Fluency is not a level you reach, it is a habit of meeting sentences that you can actually finish.",
     "importance": 5, "usefulness": 5, "article": "Learning a Language After Twenty", "tags": ["语言学习"]},
    {"term": "sentence", "pos": "n.", "meaning_zh": "句子",
     "usage": "This is why a sentence matters more than a word.",
     "importance": 2, "usefulness": 2, "article": "Learning a Language After Twenty", "tags": ["语言学习"]},
    {"term": "mistake", "pos": "n.", "meaning_zh": "错误；失误",
     "usage": "A new word that you never use is a mistake you will pay for every week.",
     "importance": 3, "usefulness": 4, "article": "Learning a Language After Twenty", "tags": ["语言学习"]},
]

