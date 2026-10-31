"""AI 客户端：OpenAI 兼容接口，未配置密钥时降级为 mock 模式。

设计要点：
- 接口格式对齐 OpenAI chat/completions，因此可接 OpenAI / DeepSeek / 通义 / 智谱等
- 未配置密钥时**不崩溃**：返回确定性的 mock 数据，保证「出题→作答→点评」流程可跑通
- ★ 但 mock 是**占位内容**，不是真实点评 ★
  mock 译文**不再伪造中文**：原样返回英文原句（用户自行对照翻译）——
    早先返回「【演示译文·未配置 AI 密钥】…」占位串，那段中文与英文原句
    毫无对应关系，用户照着它回译只会练到一个假句子；
  mock 评分是硬编码的 24/21/32（总分 77），前端据此显示「演示模式：以上为占位评分」。
- 密钥只从环境变量或本地配置读取，禁止硬编码
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading

from . import egress
from .config import get_api_key, load_settings

logger = logging.getLogger(__name__)

# 评分维度与权重（用户确认）。总分 = 三者之和，满分 100。
RUBRIC = {
    "grammar": {"label": "语法正确", "max": 30},
    "fluency": {"label": "表达地道", "max": 30},
    "fidelity": {"label": "语义保真", "max": 40},
}

_TRANSLATE_SYSTEM = (
    "你是一位严谨的英中翻译。把用户给的英文句子译成自然流畅的简体中文，"
    "只输出译文本身，不要解释、不要加引号、不要输出原文。"
)

_REVIEW_SYSTEM = """你是一位英语写作教练。这是一次**中译英**练习：用户看中文参考译文，写出英文。
你将收到三样材料：一段英文原文、其中文参考译文，以及用户写下的英文。
请只依据这三者，按下面的 JSON 结构输出点评（不要输出 JSON 以外的任何文字）：

{
  "grammar": {"score": 0-30 的整数, "issues": ["具体语法错误，没有则空数组"], "comment": "一句话点评"},
  "fluency": {"score": 0-30 的整数, "issues": ["不地道的表达及更优说法，没有则空数组"], "comment": "一句话点评"},
  "fidelity": {"score": 0-40 的整数, "issues": ["与原文意思的偏差，没有则空数组"], "comment": "一句话点评"},
  "suggestion": "一段更优的完整英文表达"
}

总则：
- 只依据上面三段材料打分，不要凭空补充原文里根本不存在的信息。
- 宁严勿松：完全没漏意思、语法无误、表达自然才给满分。
  但"严"针对的是**实质错误**：细节小瑕疵按下面各维度给出的**扣分上限**执行，
  不要因为一处小问题就把整项分数打下来。
- **三个维度的 "score" 字段必须都给出**（grammar / fluency / fidelity 都要有，
  即使是 0 分也要把字段写出来），不要漏写任何一项。

【grammar：三档严重度扣分，每档有累计上限】
- grammar 按三档严重度判分。**三档的 "40%/30%/30%" 说的是每一档最多能扣掉多少分**，
  换算到grammar 满分 30 分上就是：基本语法结构累计最多扣 12 分、时态累计最多扣 9 分、
  其余细节累计最多扣 9 分。**它不是单次扣分**；单次扣分另按下表。
- **基本语法结构**（句子成分残缺、谓语缺失、双谓语、句子不成立）——**这是唯一可以判低分的情况**，
  一处扣 10~12 分，一处即可吃掉本档全部预算。该扣就扣到位。
- **时态错误**（时态错用、时间线矛盾）——每处扣 4~5 分，**本档累计不超过 9 分**（即最多两处）。
- **其余细节**（**大小写**如 People/people 与 I/i、冠词 a/an/the、单复数、介词、标点等）
  ——每处扣 1~3 分，**本档累计不超过 9 分**（即最多三处）。
- **明确写（重要）**：细节问题**单次扣分绝不得超过 3 分**；三处细节小瑕疵扣满也只扣 9 分，
  不允许因为同一处小问题命中多条规则而累加成 10 分以上。
- 该判低分时必须判低分：句子结构不成立、双谓语、谓语缺失要扣到位，不能放过；
  反过来，结构成立、只是大小写/冠词/单复数/介词有小瑕疵的，grammar 应给高分（通常 24 分以上）。
- **同一处错误只归入一档**，不要既算时态又算结构（避免重复扣分）。
- grammar 只扣真正错的地方；"还有别的说法"不算语法错误。
- **修饰语的冗余或取舍属于用词问题，不算语法错误**，不按 grammar 扣分
  （不写进 grammar 的 issues）。

【fluency：只看不地道，不看修饰语取舍】
- fluency 看是否地道（用词搭配、句式自然度、Chinglish 倾向）。
- **修饰语（形容词、副词、程度词、数量限定成分如 a set of / a series of）的增减只影响 fidelity，不影响 fluency**：
  **仅在该修饰语承载了原文的实质信息时才扣**。
  正确做法举例：原文 "reduce emissions"，用户译 "reduce carbon emissions"
  —— 多出的限定词**不扣任何分**（属于合理的意译补充）。
  同理，原文 "needs new equipment"，用户译 "needs a set of new equipment"，
  多出的数量限定**也不扣 fluency 分**。
- **不要因为用户没有直译出某个修饰语就扣分**。中英对照练习中，
  修饰语的取舍属于翻译策略，不属于表达质量问题。
- **修饰语引起的"语义重复 / 冗余"也不扣 fluency 分**：
  例如用户写 "This is just the only option."（中文参考译文为"这正是唯一的选择。"），
  just 对应中文里的"正是"，这是**正确的翻译习惯**，
  即便它与 only 看起来语义重复，也**不扣 fluency 分**（不写进 issues）。
- **句式结构的改变不等于错误**：原文若用前置或倒装的强调句式
  （如 "For anyone who is not X, doing Y would be tempting."），用户改成
  "It is tempting for X to do Y." 属于句式选择；若读起来确实生硬、明显不如原文自然，
  可作为「语序不自然」轻提一条，**但不得写成语法错误，也不得重扣**。
- **不要仅因为某个动词短语接的宾语"看起来不合适"就断言语法错误**：
  例如 "cut down this budget" 在语法上完全成立（cut down 是及物动词，可直接接宾语）。
  觉得不对劲属于**词义选择**问题，放到 fidelity 去判，不要写进 grammar。
- fluency 只扣真正影响"像不像母语者写的"的问题：生硬直译、搭配错位、语序不自然、
  中式英语。措辞更正式或更口语本身不是问题。

【fidelity：逐项比对 + 影响权重扣分】
- fidelity 看意思是否完整准确传达（漏译、误译、添加原文没有的信息）。
- fidelity 用「逐项比对 + 影响权重」的方式判分，**不要去估算百分比或输出任何百分比数字**：
  1. 先把用户的答案翻译成中文，与参考译文**逐项对照**；
  2. 按偏差的**影响程度**分四档，每档给固定的扣分区间（不要计算百分比）：
     · **重要名词错译/漏译**（承载核心信息的实体、概念）→ 扣 6~10 分
       ★ **词的"概念选错"属于本档，不是 fluency 问题**：
         选了一个语义相近但所指完全不同的词，例如原文指的是**实际发生的花费**
         （expense / cost / spending），用户译成**事先分配的额度**（budget）
         —— budget 与 expense 在英语里是两个不同的概念，这不是"搭配不地道"，而是**用错了词**，
         按重要名词错译扣 6~10 分，并写进 fidelity 的 issues。
         同类要警惕的还有：stock（库存）/ inventory、price（价格）/ cost（成本）、
         job（工作）/ occupation（职业）、loan（贷款）/ borrow（借入）、a car / a vehicle（泛指降级为具体）。
     · **动词用错**（动作、关系、方向错）→ 扣 5~8 分
     · **次要名词或形容词错译/漏译**（修饰性信息）→ 扣 1~3 分
     · **表意过多**（增加了原文没有的信息）→ 每增加一处扣 1~2 分
  3. 同类偏差可合并计数，但**总分扣减不得超过 25 分**（保留下限，避免一句话全错就判 0）。
- **每档的扣分区间是硬边界**：判定某一处偏差落在哪一档，就在该档区间内取值，
  不得超出上沿（例如"表意过多"一处只扣 1~2 分，不能扣 5 分）。
- **表意过多要克制**：只有增加了原文**确实没有的实质信息**（凭空添加事实、因果、
  数量、评价、主张）才算表意过多；补充一处限定成分（修饰语、程度词）**不算**表意过多，
  一律不扣分。
- **修饰语（形容词、副词、程度词、数量限定成分）的增减，只在下面这一条规则下判**：
  - **多出来的（用户加了原文没有的修饰语）→ 一律不扣分**，无论它多限定了什么
    （carbon、a set of、just、around 等都算）。这属于合理的意译补充，不是表意过多。
  - **漏掉的（用户没译出原文的某个修饰语）→ 只有当该修饰语承载了原文的实质信息时才扣**，
    按"次要名词或形容词错译/漏译"档扣 1~3 分。
    ★ **程度副词与强度形容词承载实质信息，漏译要扣**：
      drastically（大幅度地）、significantly、slightly、barely、almost 改动的是动作的**程度**；
      ferocious（狂热的）、devastating、crucial、minor 修饰的是**性质强度**。
      漏掉这类词不是"少了个形容词"，而是**信息量下降**（"大幅削减"降级成"削减"、
      "狂热爱好者"降级成"爱好者"），按"次要名词或形容词错译/漏译"档扣 1~3 分，
      并写进 fidelity 的 issues（不要因为它只是修饰语就完全不写）。
- **以下情况一律不扣 fidelity 分：**
  - 用户的中文参考译文里本就没有某个修饰语，用户也没加 —— 不扣；
  - **中文译文里有、而英文原文没有的词（如"正是"、"恰恰"、"大约"这类语气/程度成分），
    用户在英文里补上了对应表达（just / exactly / around）—— 这是正确的翻译习惯，不扣分**；
  - 同义替换、大小写、标点、词序调整 —— 一律不扣。
- 只有承载了实质信息的偏差才扣分；整体意思对得住，就给高分。

【语言知识准确性要求】
- 当你在 issues 里指出某个搭配、短语、介词用法时，**必须先确认该用法本身正确**。
- 特别注意及物动词短语与介词短语的区分（如 cut down 是及物，可直接接宾语；
  cut down on 才是"节制、减少消耗"的介词短语 —— 介词是 on，不是 to）。
- **不要把正确的用法说成错误**。例如 "a set of new equipment" / "a set of tools"
  是**正确**英语（a set of 后面接一组同类事物，equipment 这类不可数名词也常用），
  不要断言它"不能搭配"、不要说成"不可数名词不能用 a set of 修饰"。
  再如 "reduce carbon emissions" 相对 "reduce emissions" 只是补充了限定词，**不是**语法错误。
- **再强调一次及物/介词短语**：`cut down this budget` 语法完全成立，不要断言它"不自然"、
  不要说需要改成 `cut down on the budget`（cut down on 才是介词短语，且介词是 on）。
- **如果你对某个搭配没有把握，不要写进 issues**，宁可漏报也不要给出错误的语言知识。"""


# ---------------------------------------------------------------------------
# 讨论模式：用户对某次评分提出质疑 / 追问，AI 在完整上下文里作答
# ---------------------------------------------------------------------------
#
# ★ 与 _REVIEW_SYSTEM 的分工 ★
# _REVIEW_SYSTEM 是「首次点评」的唯一口径来源，本常量**不复制它的判定标准**，
# 只做两件事：(1) 引用当前口径重新判一次；(2) 把结论组织成可采纳的结构。
# 口径若要调整，改的是 _REVIEW_SYSTEM，本段会自然跟随——复制一份过来必然漂移。
#
# ★ 为什么讨论结果不直接落库 ★
# 与 ai_ratings 同一条原则（见该模块 docstring）：AI 的判断是**建议**，
# 用户保留否决权。落库只有一个入口：用户点「采纳修正评分」后前端调
# PATCH /api/practices/{id}/scores。不点就不写，原评分保持不变。

_DISCUSS_SYSTEM = """你是一位英语写作教练。用户刚做完一次中译英翻译练习，
看到了你的首次点评，现在对你的评分提出质疑或追问。

你会收到：英文原文、中文参考译文、用户当时的翻译、首次三维评分与issues、
首次给出的更优表达建议，以及用户此刻的提问。

请只依据以上材料回答，并只输出如下 JSON（不要输出 JSON 以外的任何文字）：

{
  "reply": "回答文字",
  "revised": true 或 false,
  "scores": {"grammar": 0-30, "fluency": 0-30, "fidelity": 0-40},
  "total": 三维之和，满分 100
}

总则：
- **先判断用户的质疑是否成立**，再作答。
  · 质疑成立 → 在 reply 里**明确承认首评有偏差**，说清是哪一维度、原来为什么这么扣、
    现在按什么依据改。承认偏差时必须给出 revised=true 与完整三维新分。
  · 质疑不成立 → 明确说明首评站得住的理由（如"just 对应参考译文里的'正是'，
    是正确的翻译习惯，按现行口径不扣 fluency"），此时 revised=false，scores 给 {}。
- **不得为了迎合用户而虚高**：用户语气笃定不等于用户说得对。
  你的职责是给出**正确**的分数，不是让用户高兴。
- **不得推翻用户已确认的判断而无理由**：若用户已认可某个扣分，不要反过来辩称那不是问题。
- 回答用中文，直接、具体，不要客套话与免责声明。
- 回答里不要复述原题全文（用户已经看得见），只针对争议点。

【重判时必须沿用当前已生效的评分口径（与首次点评同一把尺）】
- **grammar 0–30**，按三档严重度扣分，三档不得互相替代、同一处错误只归一档：
  · 基本语法结构（成分残缺 / 谓语缺失 / 双谓语 / 句子不成立）——唯一可判低分的情形，一处扣 10~12；
  · 时态错误（时态错用 / 时间线矛盾）——一处扣 4~5；
  · 其余细节（冠词 a/an/the、单复数、介词、标点）——一处扣 1~3，
    **单次绝不超过 3 分**，三处细节瑕疵最多扣 9 分，不允许累加成 10 分以上。
  结构成立、只是细节有小瑕疵的，grammar 通常给 24 分以上。
  修饰语的冗余或取舍属于用词问题，**不算语法错误**。
- **fluency 0–30**，只看是否地道（搭配、句式自然度、中式英语倾向）。
  · 修饰语（形容词/副词/程度词/数量限定成分如 a set of / a series of）的增减
    **一律不扣 fluency**，只在它承载原文实质信息且影响 fidelity 时才扣。
  · 中文译文里的语气成分（"正是""恰恰""大约"）在英文里补成 just / exactly / around，
    是**正确的翻译习惯**，即便与 only 看起来语义重复也**不扣分**。
- **fidelity 0–40**，逐项比对 + 影响权重扣分，**不要输出任何百分比**：
  · 重要名词错译/漏译（承载核心信息的实体、概念）→ 扣 6~10；
  · 动词用错（动作、关系、方向错）→ 扣 5~8；
  · 次要名词或形容词错译/漏译（修饰性信息）→ 扣 1~3；
  · 表意过多（增加了原文确实没有的实质信息）→ 每处扣 1~2；
    但补充一处限定成分（修饰语、程度词）**不算**表意过多，一律不扣。
  · 同类偏差可合并，但**总扣减不得超过 25 分**。
  · 漏掉的修饰语只有承载原文实质信息时才扣（按"次要名词或形容词"档 1~3）。
  · 同义替换、大小写、标点、词序调整一律不扣。

【语言知识准确性要求】
- 指出某个搭配、短语、介词用法前，**必须先确认该用法本身正确**。
- 特别注意及物动词短语与介词短语的区分（cut down 是及物，可直接接宾语；
  cut down on 才是"节制、减少消耗"的介词短语 —— 介词是 on，不是 to）。
- **不要把正确的用法说成错误**。例如 "a set of new equipment" / "a set of tools"
  是**正确**英语，不要断言它"不能搭配"。
- **对某个搭配没有把握就不要写进 reply**，宁可漏答也不要给出错误的语言知识。

【分数的合法性（会被后端校验，不合法将被拒绝采纳）】
- grammar ∈ [0, 30]，fluency ∈ [0, 30]，fidelity ∈ [0, 40]。
- 允许整数或**半星**（如 23.5）。
- total 必须等于三维之和（后端会核对，允许 0.5 以内的差）。
- **不要输出百分数，也不要输出任何超出上述区间的数字。**"""


# 默认输出上限。点评 JSON 与译文都很短，2000 token 足够且能防止模型思维链
# 无止境消耗；不设上限时一旦模型开启深度思考，响应时间会显著变长。
_DEFAULT_MAX_TOKENS = 2000

# 仅对"确认支持 thinking 字段"的服务下发该参数。
#
# 采用**白名单**而非黑名单：各家OpenAI 兼容服务对未知字段的处理不一致
# （方舟按文档支持 thinking；DeepSeek / OpenAI 等可能直接返回 400 参数错误），
# 而黑名单无法穷举所有会拒绝该字段的服务，一旦漏判就会让用户彻底无法使用 AI。
# 白名单的失效方向是安全的：未命中时只是不优化延迟，不会导致请求失败。
_THINKING_CAPABLE_HOST_HINTS = (
    "volces.com",  # 火山方舟（ark.cn-beijing.volces.com 等）
)


def _supports_thinking(base: str) -> bool:
    """判断该 base_url 是否确认支持 thinking 字段（白名单，见上方常量说明）。"""
    low = (base or "").lower()
    return any(hint in low for hint in _THINKING_CAPABLE_HOST_HINTS)


class AIError(RuntimeError):
    """AI 调用失败（网络、鉴权、返回格式等）。"""


class AINotConfiguredError(AIError):
    """需要真实 AI，但当前没有可用的 API 密钥。

    单独一类而不是直接用 AIError：翻译 / 点评在无密钥时会走 mock 降级
    （返回带 is_mock 标记的演示数据，流程照样能跑通），而**评分建议不能降级**——
    编造出来的分数会被用户当成真实建议采纳并写进词库。宁可明确报错。
    """


#: 进程级 AI 出网并发上限（B6 加固）。
#: 云端是单进程多线程（uvicorn + 线程池），一次「开始练习」能触发
#: 批量翻译 + 逐句点评多次调用，访客一多就会同时压上一堆出网连接；
#: 对端稍慢，连接/线程数就随访客数线性涨。16 条并发对正常使用
#: 完全无感（正常响应几秒内完成），但把最坏情况钉死在这个数上。
_ai_slots = threading.BoundedSemaphore(16)

#: AI 响应体上限（B14 加固）。
#: 点评 JSON 与译文最长几百字节，4MB 是三个数量级的余量。
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _read_capped(resp, limit: int = _MAX_RESPONSE_BYTES) -> None:
    """把响应体读进内存，累计超过 ``limit`` 即中断并抛 :class:`AIError`。

    ★ 为什么需要它 ★
    请求带 ``stream=True`` 时 requests **不会**自动读 body；
    此后访问 ``.text`` / ``.json()`` 会把**整个** body 拉进内存 ——
    一个异常对端（或把 Content-Length 骗小后持续灌数据的响应）
    就能用无限流把进程内存吃光。

    ★ 读完写回 ``resp._content`` ★
    这是 requests 自己的 ``Response.content`` 内部机制：``_content``
    一旦就位，``.text`` / ``.json()`` 都走缓存、不再碰网络，
    调用方（错误分支的 ``resp.text[:200]`` 等）逐字不变。

    ★ 没有 ``raw`` 的响应替身原样放行 ★
    测试替身自己管内容，不该被这个上限误伤。
    """
    if getattr(resp, "raw", None) is None:
        return
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            # 先断连接再抛：不把剩余流量继续收进进程
            resp.close()
            raise AIError("AI 接口响应过大，已中断读取")
        chunks.append(chunk)
    resp._content = b"".join(chunks)
    resp._content_consumed = True


def is_mock_mode() -> bool:
    return not get_api_key()


def require_api_key() -> None:
    """要求已配置密钥，否则抛 AINotConfiguredError。

    供「不允许 mock 降级」的调用方在入口处挡一道，把失败提前到发请求之前，
    免得白等一次网络往返。
    """
    if not get_api_key():
        raise AINotConfiguredError(
            "未配置 AI 密钥：请到「设置」里填写 API Key，"
            "或设置环境变量 OPENAI_API_KEY 后重启程序"
        )


def chat(
    system: str,
    user: str,
    *,
    temperature: float = 0.3,
    max_tokens: int | None = None,
) -> str:
    """**唯一的 AI 调用入口**（对外公开）。

    薄封装 _chat：先确认密钥可用，再委托实际请求。业务模块一律走这里，
    不要自己 import requests 打接口——超时、thinking 白名单、401 归一化这些
    规则都收在 _chat 里，散出去调用等于把这些规则复制一遍，迟早漂移。
    """
    require_api_key()
    return _chat(system, user, temperature=temperature, max_tokens=max_tokens)


def extract_json(text: str):
    """从模型输出里提取 JSON（公开入口，容忍 ```json 包裹与前后杂文本）。"""
    return _extract_json(text)


def _settings() -> dict:
    """当前生效的 AI 配置。

    ★ 桌面：逐字返回 settings.json 里的值，行为与改造前完全一致 ★

    ★ 云端：base_url 与 model 的来源有两条 ★
    ① **平台锁死值**（`cloud.ai_base_url()` / `cloud.ai_model()`）——
       访客没填时用它。
    ② **访客自填值**——通过请求头 `X-Enp-Ai-Base-Url` / `X-Enp-Ai-Model` 带来，
       **只在本次请求的内存里**，绝不落盘（与 API Key 完全同构的通道）。

    ★ 为什么这里**不做** SSRF 校验 ★
    校验要解析域名（DNS 查询）。而 `_settings()` 被 `/api/ai/status` 这类
    **纯展示**接口调用 —— 每调一次就替访客解析一次域名，等于把我们的
    DNS 变成访客的匿名解析器（可被用来做 DNS 放大与信息外带）。
    ⇒ 校验统一放在 `_chat()`：**真的要发请求的那一刻**才做。
    这里只负责"取值"，并用 `_visitor_base_url` 标记出
    "这个 base_url 是访客指定的，发请求前必须先过校验"。

    ★ 为什么访客自填地址**不落盘** ★
    它决定"我们的服务器要向哪里发请求"。落盘就等于把访客指定的出网地址
    长期留在服务器上；走请求头则地址只活在这一次请求的内存里。
    同时，"访客自填的地址绝不进服务端磁盘"这条不变量原样保留。
    """
    cfg = load_settings().get("ai", {})
    from . import cloud
    if not cloud.cloud_mode():
        return cfg
    locked = dict(cfg)
    locked["base_url"] = cloud.ai_base_url()
    locked["model"] = cloud.ai_model()

    raw_base = (cloud.request_ai_base_url.get() or "").strip()
    raw_model = (cloud.request_ai_model.get() or "").strip()
    raw_timeout = (cloud.request_ai_timeout.get() or "").strip()

    if raw_base:
        locked["base_url"] = raw_base.rstrip("/")
    if raw_model:
        locked["model"] = raw_model
    if raw_timeout:
        try:
            secs = int(raw_timeout)
        except ValueError:
            secs = 0
        # ★ 上限 60 秒（B6 加固）★
        # 原来放到 300：一次练习能同时触发翻译/点评多次调用，每个都占一个
        # 出网连接；300 秒的慢连接会把这些连接全拖住。点评/译文的正常
        # 响应在几秒内完成，60 秒已是三个数量级的余量。
        if 5 <= secs <= 60:
            locked["timeout"] = secs

    # ★ 标记"这个 base_url 是访客指定的" ★
    #   `_chat` 看到它，就知道发请求前必须先过 app/egress.py 的 SSRF 校验，
    #   并且必须走钉住 IP 的那条路（防 DNS rebinding）。
    locked["_visitor_base_url"] = raw_base
    return locked


def _chat(
    system: str,
    user: str,
    *,
    temperature: float = 0.3,
    max_tokens: int | None = None,
) -> str:
    """调用 OpenAI 兼容 chat/completions，返回文本内容。

    对支持 thinking 的服务（火山方舟）默认下发 ``thinking:{"type":"disabled"}``：
    doubao-seed-evolving / doubao-seed-2-1-* 等模型官方默认开启深度思考，会先输出
    一大段思维链再给结果，而本应用的点评/翻译只需结构化输出，思维链纯属浪费首字
    延迟与总耗时。

    ★ 出网分两条路 ★
    ① **桌面 / 云端平台默认地址** → `requests.post`。
       这里也禁重定向（B10）：跟随重定向等于把请求（连同密钥头）
       带去一个没人校验过的地址；响应流式读入并截到 4MB（B14）。
    ② **云端 + 访客自填地址** → 走 `app/egress.py`：先校验（https + 443 +
       解析出的每个 IP 都是公网），再把连接**钉在已校验的 IP 上**
       （TLS 的 SNI 与证书校验仍用原域名），防 DNS rebinding。
       访客填的地址没通过校验时，`_settings()` 已经回落到平台默认地址，
       所以这里走的一定是「已校验过的目标」。

    ★ 进程级并发上限（B6）★
    两条路都在 :data:`_ai_slots`（16）内发请求：一次练习能触发
    批量翻译 + 逐句点评多次调用，不设上限时并发随访客数线性涨。
    """
    import requests

    cfg = _settings()
    key = get_api_key()
    base = (cfg.get("base_url") or "").rstrip("/")
    model = cfg.get("model") or "deepseek-chat"
    timeout = cfg.get("timeout") or 60
    if not base:
        raise AIError("未配置 AI 接口地址（base_url），请在设置中填写")

    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens or _DEFAULT_MAX_TOKENS,
    }
    # 只对白名单内的服务下发，避免其他兼容服务因未知字段报 400
    if _supports_thinking(base):
        payload["thinking"] = {"type": "disabled"}

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    # ★ 出网并发上限（B6 加固）★
    #   罩住「发请求 + 读响应」整段：限额内的才放行，超出的在门口排队。
    with _ai_slots:
        # ★ 云端 + 访客自填地址 → 走出网白名单 ★
        #   ① 先校验（https + 443 + 解析出的每个 IP 都是公网）；
        #   ② 再钉住已校验的 IP 发请求（TLS 的 SNI/证书仍用原域名），
        #      堵住 DNS rebinding（校验时给公网 IP、请求时给内网 IP）。
        #   校验失败 ⇒ 明确报错给访客（而不是悄悄换成平台默认地址 ——
        #   那样访客会以为"填了没生效"，正是我们要修的那个毛病）。
        visitor_base = cfg.get("_visitor_base_url") or ""
        if visitor_base:
            try:
                target = egress.validate_base_url(visitor_base)
            except egress.EgressError as exc:
                # ★ 记 warning 而不是静默 ★
                #   静默回落会让访客以为"填了没生效"（正是本次要修的毛病）；
                #   明确报错 + 日志留痕，运营也能看出谁在被拦。
                logger.warning("访客自填的接口地址被出网白名单拒绝：%s", exc)
                raise AIError(f"接口地址不被允许：{exc}") from exc
            try:
                model = egress.validate_model(model)
            except egress.EgressError as exc:
                logger.warning("访客自填的模型名称被拒绝：%s", exc)
                raise AIError(f"模型名称不被允许：{exc}") from exc
            try:
                resp = egress.post_json(target, "/chat/completions", payload,
                                        headers, timeout)
            except egress.EgressError as exc:
                raise AIError(f"接口地址不被允许：{exc}") from exc
            except requests.RequestException as exc:
                raise AIError(f"AI 请求失败：{exc}") from exc
        else:
            # ★ 禁重定向（B10）+ 流式读取（B14）★
            #   跟随重定向 = 把请求（连同 Authorization 头）带去一个
            #   没校验过的地址；stream=True 让响应体由下面的
            #   `_read_capped` 带 4MB 上限地读，而不是整个拉进内存。
            try:
                resp = requests.post(
                    f"{base}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.RequestException as exc:
                raise AIError(f"AI 请求失败：{exc}") from exc

        # ★ 读响应体：累计超 4MB 即中断（B14 加固）★
        #   必须发生在任何 .text / .json() 访问**之前** ——
        #   那些访问会静默把整个 body 拉进内存。
        _read_capped(resp)

    if resp.status_code == 401:
        raise AIError("AI 密钥无效或已过期（HTTP 401），请在设置中检查 api_key")
    if resp.status_code != 200:
        raise AIError(f"AI 接口返回 HTTP {resp.status_code}：{resp.text[:200]}")

    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AIError(f"AI 返回格式无法解析：{exc}") from exc


def _escape_inner_quotes(text: str) -> str:
    """修复模型最常见的 JSON 畸形：**字符串值内部的裸英文双引号没转义**。

    典型现场（用户实测报障）：模型在 reason / 例句里写了 `say "hi"`，
    json.loads 报 `Expecting ',' delimiter` —— 解析器把值中间的 `"` 当成了
    字符串收尾，下一个字符又不是 `,` `}`，于是报错。

    修复是个保守状态机：处于字符串内时遇到 `"`，看它后面**跳过空白**的下一个
    字符——是 `,` `}` `]` `:` 之一才认定这个引号是"字符串收尾"，
    否则它是值内引号，转义成 `\\"`。
    ★ 只做**候选**修复：修完必须 json.loads 成功才采纳（调用方把关），
      修不好就维持原文抛原始错误——坏修复不可能比原始错误更糟。
    """
    if '"' not in text:
        return text
    out: list[str] = []
    in_string = False
    escaped = False
    n = len(text)
    for i, ch in enumerate(text):
        if not in_string:
            if ch == '"':
                in_string = True
            out.append(ch)
            continue
        if escaped:  # 上一个字符是反斜杠：转义序列整体放行
            escaped = False
            out.append(ch)
            continue
        if ch == "\\":
            escaped = True
            out.append(ch)
            continue
        if ch == '"':
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            nxt = text[j] if j < n else ""
            if nxt in ",}]:":  # 合法的字符串收尾位置
                in_string = False
                out.append(ch)
            else:  # 后面还是普通字符 ⇒ 这是值内部引号，转义它
                out.append('\\"')
            continue
        out.append(ch)
    return "".join(out)


def _balanced_tail(prefix: str) -> str:
    """计算 prefix 末尾还缺哪些收尾符（把没关的 { [ 按栈序补齐）。

    只在**字符串外**统计；遇到配对错误的收尾符（如 `]` 想关 `{`）按野括号
    忽略——打捞场景下宁可少补也不能补出更乱的嵌套。若截断发生在字符串内部，
    先补一个 `"` 再补括号。"""
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in prefix:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack and stack[-1] == ("{" if ch == "}" else "["):
            stack.pop()
        # 配对不上的收尾符 = 野括号，忽略
    tail = "".join("}" if c == "{" else "]" for c in reversed(stack))
    if in_string:
        tail = '"' + tail  # 截断在字符串中间：先补引号再补括号
    return tail


def _salvage_items(candidate: str) -> dict | None:
    """整批 JSON 结构坏掉时的**逐条打捞**（用户实测形态：模型漏写每个词条
    自己的收尾 `}`，整批解析必挂，但其实每条的内容是完好的）。

    做法：按 `{"entry_id"` 边界把原文切成条目段，每段**补齐括号平衡**后
    单独解析；能解析的条目收回成 {"items": [...]}。打捞只收**解析成功**的
    段，解析失败的段直接放弃——绝不把没验证过的半截数据当建议返回。
    一条都救不回来时返回 None（调用方走原始报错）。"""
    starts = [m.start() for m in re.finditer(r'\{\s*"entry_id"', candidate)]
    if not starts:
        return None
    items: list[dict] = []
    for n, s in enumerate(starts):
        seg_end = starts[n + 1] if n + 1 < len(starts) else len(candidate)
        seg = candidate[s:seg_end].rstrip().rstrip(",")
        try:
            obj = json.loads(seg + _balanced_tail(seg))
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and "entry_id" in obj:
            items.append(obj)
    return {"items": items} if items else None


def _extract_json(text: str):
    """从模型输出中提取 JSON（容忍 ```json 包裹或前后杂文本）。

    解析失败按序尝试三层**保守修复**（每一层都必须真的解析成功才采纳，
    修不好就落到下一层；全部失败则抛原始错误并附出错位置附近原文）：
      1. 值内裸英文双引号转义（模型在理由里写 say "hi" 没转义）；
      2. 截断补尾：输出被 max_tokens 截断时，按栈序补齐缺失的 } ]；
      3. 逐条打捞：结构性畸形（如漏写词条收尾括号）时按 {"entry_id" 切块救回
         完好条目，坏掉的条目由调用方按"未返回"处理。
    """
    cleaned = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned)
    if fence:
        cleaned = fence.group(1).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise AIError("AI 未返回预期的 JSON 结构")
    candidate = cleaned[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as first_exc:
        # 1) 值内裸引号
        try:
            return json.loads(_escape_inner_quotes(candidate))
        except (json.JSONDecodeError, ValueError):
            pass
        # 2) 截断补尾（对修复后的串也试一次）
        for cand in (candidate, _escape_inner_quotes(candidate)):
            try:
                return json.loads(cand + _balanced_tail(cand))
            except (json.JSONDecodeError, ValueError):
                continue
        # 3) 逐条打捞
        salvaged = _salvage_items(candidate)
        if salvaged is not None:
            return salvaged
        at = max(0, (first_exc.pos or 0) - 60)
        snippet = candidate[at : (first_exc.pos or 0) + 60]
        raise AIError(
            f"AI 返回的 JSON 无法解析：{first_exc}；"
            f"出错位置附近原文：…{snippet}…"
        ) from first_exc


def _clamp_score(value, max_score: int) -> int:
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(max_score, n))


def _has_score(item) -> bool:
    """模型是否真的给出了这一维的 score 字段（而不是 null / 缺键 / 非数值）。

    ★ 为什么必须单独判断 ★
    _clamp_score 对 None 返回 0，也就是「模型漏了score」和「模型给了 0 分」
    会被压成同一个结果。而这个0 分会被 practices.py 直接写进数据库 ——
    实测（练习 #9 三次重跑）模型确实会偶发漏掉 score 字段（1/3 次），
    届时用户会看到「语法 0 /地道 0 / 保真 0，总分 0」并且**这条 0 分被存进库**。
    这比评分偏松严重得多：偏松用户还能自己判断，0 分会被当成真实成绩，
    还会连带污染词条复习排期。所以这里只做"识别"，处置交给调用方。
    """
    if not isinstance(item, dict):
        return False
    if "score" not in item:
        return False
    try:
        float(item["score"])
    except (TypeError, ValueError):
        return False
    return True


class ReviewScoreMissingError(AIError):
    """模型返回的点评里缺少某一维的 score 字段。

    单独一类而不是复用 AIError：调用方要区分「**模型没答完**」（值得自动重试一次）
    与「AI 服务故障」（重试也没用）。前者重试通常就好了 —— 实测同一句话
    第二次就带齐了三个分数。
    """


def _normalize_review(raw: dict, *, strict: bool = False) -> dict:
    """把模型返回的点评规整为固定结构，分数夹紧到各维度上限内。

    strict=True 时，缺 score 字段会抛 ReviewScoreMissingError 而不是静默变 0 分。
    """
    out = {"grammar": {}, "fluency": {}, "fidelity": {}}
    if strict:
        missing = [
            key for key, spec in RUBRIC.items()
            if not _has_score((raw or {}).get(key))
        ]
        if missing:
            labels = "/".join(RUBRIC[k]["label"] for k in missing)
            raise ReviewScoreMissingError(
                f"AI 返回的点评缺少数值（{labels}），请重试")
    for key, spec in RUBRIC.items():
        item = raw.get(key) or {}
        if not isinstance(item, dict):
            item = {}
        issues = item.get("issues")
        out[key] = {
            "score": _clamp_score(item.get("score"), spec["max"]),
            "max": spec["max"],
            "label": spec["label"],
            "issues": [str(i) for i in issues] if isinstance(issues, list) else [],
            "comment": str(item.get("comment") or ""),
        }
    out["total"] = sum(out[k]["score"] for k in RUBRIC)
    out["suggestion"] = str(raw.get("suggestion") or "")
    return out


# ---------------------------------------------------------------------------
# mock 模式：未配置密钥时返回确定性演示数据，保证流程可跑通
# ---------------------------------------------------------------------------


def _mock_translate(text_en: str) -> str:
    """mock 模式的「参考译文」：**原样返回英文原句，不伪造中文**。

    ★ 为什么不再给占位中文 ★
    早先返回「【演示译文·未配置 AI 密钥】这是对这段英文（约 N 词）的占位中文翻译」。
    那段中文与英文原句**没有任何对应关系**，而练习是「看中文回译英文」——
    用户照着这句假中文写出来的英文必然对不上原句，等于练了一个假句子，
    比没有参考译文更糟（还会让人以为译文就是这么来的）。
    ⇒ 没有真译文时就给英文原句，让用户自己对照；前端会说明这是原句而非 AI 译文。
    """
    return (text_en or "").strip()


def _mock_review(user_text: str) -> dict:
    length = len((user_text or "").split())
    return _normalize_review({
        "grammar": {"score": 24, "issues": [], "comment": "演示评分：未配置密钥，无法真实点评。"},
        "fluency": {"score": 21, "issues": [], "comment": "演示评分：未配置密钥，无法真实点评。"},
        "fidelity": {"score": 32, "issues": [], "comment": "演示评分：未配置密钥，无法真实点评。"},
        "suggestion": (
            f"【演示模式】你的翻译约 {length} 词。配置 AI 密钥后，这里会给出更优的完整英文表达。"
        ),
    })


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


_TRANSLATE_BATCH_SYSTEM = (
    "你是一位严谨的英中翻译。用户会给出多行英文，每行一句。"
    "请逐行译成自然流畅的简体中文，**每行输出一句对应的中文译文**。\n"
    "严格要求：\n"
    "1. 输出行数必须与输入行数完全一致，一行也不能多、不能少\n"
    "2. 行与行一一对应，第 N 行译文必须对应第 N 行英文\n"
    "3. 每句译文内部不要换行、不要编号、不要加引号、不要解释\n"
    "4. 只输出译文本身，不要任何多余文字"
)


def translate_to_chinese(text_en: str) -> dict:
    """英译中，返回 {text, is_mock}。"""
    text_en = (text_en or "").strip()
    if not text_en:
        raise AIError("待翻译的英文不能为空")
    if is_mock_mode():
        return {"text": _mock_translate(text_en), "is_mock": True}
    content = _chat(_TRANSLATE_SYSTEM, text_en, temperature=0.2, max_tokens=1024)
    return {"text": content.strip(), "is_mock": False}


def translate_batch(list_en: list[str]) -> list[dict]:
    """批量英译中，返回 [{text, is_mock}, ...]，顺序与入参严格一致。

    一次.request 翻完所有句子 —— 逐句调用时 N 句要 N 次网络往返，
    而点「开始练习」可能同时要翻 5 句，这是那段卡顿的主来源。

    行数对不齐时**整体退回逐句调用**：宁可多花点时间，也不能把第 3 句的
    译文配到第 4 句上（错位译文比慢更糟糕，和 外刊对齐「宁缺勿错」同理）。
    """
    cleaned = [(t or "").strip() for t in list_en]
    # 内部都是单行：折行会破坏按行对齐的约定
    cleaned = [" ".join(t.split()) for t in cleaned]

    if is_mock_mode():
        return [
            {"text": _mock_translate(t), "is_mock": True} if t
            else {"text": "", "is_mock": True}
            for t in cleaned
        ]

    pending = [i for i, t in enumerate(cleaned) if t]
    if not pending:
        return [{"text": "", "is_mock": False} for _ in cleaned]

    joined = "\n".join(cleaned[i] for i in pending)
    try:
        content = _chat(
            _TRANSLATE_BATCH_SYSTEM, joined,
            temperature=0.2, max_tokens=max(1024, len(pending) * 200),
        )
        lines = [ln.strip() for ln in content.strip().splitlines() if ln.strip()]
        if len(lines) != len(pending):
            raise AIError(
                f"批量翻译行数不匹配：期望 {len(pending)} 行，实际 {len(lines)} 行"
            )
    except (AIError, OSError, ValueError):
        # 行数对不上或请求失败 → 逐句兜底，保证一定有译文可用
        out: list[dict] = [{"text": "", "is_mock": False} for _ in cleaned]
        for i in pending:
            try:
                res = translate_to_chinese(cleaned[i])
                out[i] = {"text": res["text"], "is_mock": res["is_mock"]}
            except AIError:
                out[i] = {"text": "", "is_mock": False}
        return out

    out: list[dict] = [{"text": "", "is_mock": False} for _ in cleaned]
    # strip 「1. 」这类前缀：模型偶尔不守规矩，但不该因此整批作废
    for idx, i in enumerate(pending):
        txt = lines[idx]
        for pre in ("- ", "* ", "• "):
            if txt.startswith(pre):
                txt = txt[len(pre):].strip()
        if len(txt) > 2 and txt[0].isdigit():
            dot = txt.find(".")
            if 0 < dot <= 3:
                txt = txt[dot + 1:].strip()
        out[i] = {"text": txt, "is_mock": False}
    return out


def review_translation(source_en: str, reference_zh: str, user_en: str) -> dict:
    """点评翻译，返回含三维分数与总分的结构（带 is_mock 标记）。"""
    source_en = (source_en or "").strip()
    user_en = (user_en or "").strip()
    if not source_en:
        raise AIError("英文原文不能为空")
    if not user_en:
        raise AIError("你的翻译不能为空")

    if is_mock_mode():
        result = _mock_review(user_en)
        result["is_mock"] = True
        return result

    user_msg = (
        f"【英文原文】\n{source_en}\n\n"
        f"【中文参考译文】\n{(reference_zh or '').strip()}\n\n"
        f"【我的翻译】\n{user_en}"
    )
    content = _chat(_REVIEW_SYSTEM, user_msg, temperature=0.2, max_tokens=_DEFAULT_MAX_TOKENS)
    try:
        result = _normalize_review(_extract_json(content), strict=True)
    except ReviewScoreMissingError:
        # 模型偶发漏掉score 字段（实测约 1/3 次），重试一次通常就带齐了。
        # ★ 绝不改成"缺失就当0 分" ★ —— 那个 0 分会被 practices.py 写进数据库，
        # 既误导用户（看起来像真实成绩），又会连带污染词条复习排期。
        content = _chat(_REVIEW_SYSTEM, user_msg, temperature=0.2,
                        max_tokens=_DEFAULT_MAX_TOKENS)
        result = _normalize_review(_extract_json(content), strict=True)
    result["is_mock"] = False
    return result


# ---------------------------------------------------------------------------
# 讨论模式：就某条已评分的练习与 AI 争论，并给出（可选的）修正评分
# ---------------------------------------------------------------------------

#: 各维度满分。与 RUBRIC 同源，避免这里再抄一份数字导致两处漂移。
_DISCUSS_LIMITS = {"grammar": 30, "fluency": 30, "fidelity": 40}

#: 提问长度上限。模型 prompt 里塞不进无限长的用户输入，
#: 而且超长提问往往是误粘贴（整篇文章），对「质疑某个扣分」毫无帮助。
MAX_DISCUSS_QUESTION_LEN = 500

#: total 与三维之和允许的最大偏差。半星增量下，"整数 total"与"半星三维之和"
#: 本来就可能差 0.5；超过这个量就不是舍入误差，而是模型自己没算清。
_TOTAL_TOLERANCE = 0.5


class DiscussScoreError(AIError):
    """修正评分不合法（越界 / 缺项 / total 对不上）。

    单独一类而不是复用 AIError：调用方需要把它当成「**用户不能采纳**」的
    业务失败来提示，而不是当成「AI 服务故障」弹 502。
    绝不静默夹取——把 grammar=50 夹成 30 写进库，用户会以为 AI 真的给过 50 分，
    而库里躺着一个来源不明的 30 分，这种"善意的修正"比直接报错有害得多。
    """


def _validate_discuss_scores(raw) -> tuple[dict, float]:
    """校验模型给的修正评分，返回 (scores, total)；不合法抛 DiscussScoreError。

    刻意**不夹取**：越界就是拒绝。理由见 DiscussScoreError 的 docstring。
    """
    if not isinstance(raw, dict):
        raise DiscussScoreError("修正评分缺少 scores 对象")

    scores: dict = {}
    for key, limit in _DISCUSS_LIMITS.items():
        if key not in raw:
            raise DiscussScoreError(f"修正评分缺少 {key} 维度")
        value = raw[key]
        # bool 是 int 的子类：True 会被当成 1 分收下，这里显式排除
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DiscussScoreError(f"{key} 分数不是数字：{value!r}")
        value = float(value)
        if value != value or value in (float("inf"), float("-inf")):
            raise DiscussScoreError(f"{key} 分数不是有限数字：{value!r}")
        if not (0 <= value <= limit):
            raise DiscussScoreError(
                f"{key} 分数越界：{value:g}（合法范围 0 ~ {limit}）"
            )
        scores[key] = value

    expected = sum(scores.values())
    total = raw.get("total", expected)
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        raise DiscussScoreError(f"total 不是数字：{total!r}")
    total = float(total)
    if total != total or total in (float("inf"), float("-inf")):
        raise DiscussScoreError(f"total 不是有限数字：{total!r}")
    if not (0 <= total <= sum(_DISCUSS_LIMITS.values())):
        raise DiscussScoreError(f"total 越界：{total:g}（合法范围 0 ~ 100）")
    if abs(total - expected) > _TOTAL_TOLERANCE:
        raise DiscussScoreError(
            f"total 与三维之和不一致：total={total:g}，三维之和={expected:g}"
        )
    return scores, total


def discuss_practice(
    *,
    source_en: str,
    reference_zh: str,
    user_en: str,
    first_review: dict,
    question: str,
) -> dict:
    """就某次已完成的点评回答用户质疑，必要时给出修正评分。

    first_review 是**首次点评的原始结构**（practice 记录里 ai_feedback 解析出来的
    对象），要把三维分数、issues、comment、suggestion 都带进上下文——用户质疑的
    正是这些具体条目，只给分数不给依据，模型只能靠猜。

    返回 {reply, revised, scores, total}：
    - revised=False 时 scores 为 None、total 为 None（纯讨论，不给修正分）。
    - revised=True 但模型给的分数不合法 → 抛 DiscussScoreError，**不返回半成品**。
      宁可让用户看到"AI 给的修正分不合法"，也不要他点采纳后被静默夹取。

    **本函数绝不写数据库**，与 review_translation 一样是纯计算。
    """
    question = (question or "").strip()
    if not question:
        raise AIError("请先写下你的疑问")
    if len(question) > MAX_DISCUSS_QUESTION_LEN:
        raise AIError(f"提问太长（{len(question)} 字），请精简到 {MAX_DISCUSS_QUESTION_LEN} 字以内")

    if is_mock_mode():
        # 讨论会直接影响用户对分数的判断，进而可能改写数据库里的评分。
        # 与 ai_ratings 同口径：宁可明确报错，也不给编造的回答。
        raise AINotConfiguredError(
            "未配置 AI 密钥，无法进行讨论。请到「设置」里填写 API Key，"
            "或设置环境变量 OPENAI_API_KEY 后重启程序"
        )

    review = first_review if isinstance(first_review, dict) else {}
    def _dim(key: str) -> str:
        item = review.get(key)
        if not isinstance(item, dict):
            return "（无记录）"
        issues = item.get("issues")
        issues = issues if isinstance(issues, list) else []
        lines = [f"- {review.get(key, {}).get('label', key)}："
                 f"{item.get('score', '—')} / {_DISCUSS_LIMITS.get(key, '—')} 分"]
        if issues:
            lines += [f"  · 扣分点：{i}" for i in issues]
        if item.get("comment"):
            lines.append(f"  · 总评：{item['comment']}")
        return "\n".join(lines)

    user_msg = (
        f"【英文原文】\n{(source_en or '').strip() or '（无记录）'}\n\n"
        f"【中文参考译文】\n{(reference_zh or '').strip() or '（无记录）'}\n\n"
        f"【用户当时的翻译】\n{(user_en or '').strip() or '（无记录）'}\n\n"
        f"【首次三维评分与issues】\n"
        f"{_dim('grammar')}\n{_dim('fluency')}\n{_dim('fidelity')}\n"
        f"总分：{review.get('total', '—')} / 100\n\n"
        f"【首次给出的更优表达建议】\n{review.get('suggestion') or '（无）'}\n\n"
        f"【用户此刻的提问】\n{question}"
    )
    content = _chat(
        _DISCUSS_SYSTEM, user_msg, temperature=0.3, max_tokens=_DEFAULT_MAX_TOKENS
    )
    parsed = _extract_json(content)
    if not isinstance(parsed, dict):
        raise AIError("AI 未返回预期的 JSON 结构")

    reply = parsed.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        raise AIError("AI 的回答为空，请重试")

    revised = bool(parsed.get("revised"))
    if not revised:
        return {"reply": reply.strip(), "revised": False, "scores": None, "total": None}

    scores, total = _validate_discuss_scores(parsed.get("scores"))
    return {"reply": reply.strip(), "revised": True, "scores": scores, "total": total}
