"""翻译练习点评口径（_REVIEW_SYSTEM）的字面锚定测试。

运行：python test_review_prompt.py
说明：纯静态断言 + 一条桩模型的行为断言，不联网、不碰真实数据。

为什么要有这个文件：
点评口径是**写给模型看的自然语言**，改一个词就等于改产品定义
（与 app/ai_ratings.py 的判定口径同理）。口径若被后人"顺手润色"，
用户会看到分数悄悄变样且无从察觉 —— 所以这里**字面**锚住关键锚点串。

本轮口径变更（用户实测反馈驱动）：
  1. grammar 改为**三档严重度**扣分（结构 40% / 时态 30% / 其余 30%），
     细节小瑕疵单次扣分不得超过 3 分，不再"错一处就重扣 10 分"；
  2. 修饰语（形容词/副词/程度词）的增减**只影响 fidelity，不影响 fluency**；
  3. fidelity 改为「逐项比对 + 影响权重」扣分，**不输出任何百分比数字**，
     并写清三类「一律不扣分」的边界反例（含「正是→just」）；
  4. 指出搭配/介词用法前必须先确认用法本身正确，没把握就不写进 issues。

同时守住不许动的硬约束：
  - RUBRIC 三维与满分上限（30/30/40）
  - JSON 输出结构（三个维度 + suggestion）
  - _DEFAULT_MAX_TOKENS = 2000
"""

import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="enpractice_review_prompt_")
os.environ["ENPRACTICE_HOME"] = TMP  # 必须在导入 app 之前设置

from unittest.mock import patch  # noqa: E402

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import ai  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def section(t):
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


SYS = ai._REVIEW_SYSTEM

# ---------------------------------------------------------------------------
print("[1] 硬约束：RUBRIC / JSON 结构 / max_tokens 不许动")
# ---------------------------------------------------------------------------

check("RUBRIC 三维与满分上限仍是 30/30/40",
      ai.RUBRIC == {
          "grammar": {"label": "语法正确", "max": 30},
          "fluency": {"label": "表达地道", "max": 30},
          "fidelity": {"label": "语义保真", "max": 40},
      },
      str(ai.RUBRIC))
check("_DEFAULT_MAX_TOKENS 仍是 2000", ai._DEFAULT_MAX_TOKENS == 2000,
      str(ai._DEFAULT_MAX_TOKENS))
check("提示词里 JSON 三个维度的分数区间仍是 0-30 / 0-30 / 0-40",
      '"score": 0-30 的整数' in SYS and '"score": 0-40 的整数' in SYS)
check("JSON 结构仍含 suggestion 字段",
      '"suggestion": "一段更优的完整英文表达"' in SYS)
check("JSON 结构仍含 grammar/fluency/fidelity 三键",
      all(f'"{k}": {{"score"' in SYS for k in ("grammar", "fluency", "fidelity")))
check("★ 明写三个维度的 score 字段都必须给出（防漏写导致静默 0 分）",
      "三个维度的 \"score\" 字段必须都给出" in SYS)

# ---------------------------------------------------------------------------
print("\n[2] 改动 1：grammar 三档严重度 + 细节小瑕疵封顶 3 分")
# ---------------------------------------------------------------------------

check("★ grammar 写明「按三档严重度扣分」",
      "grammar 按三档严重度扣分" in SYS or "grammar 按三档严重度判分" in SYS)
check("★ 40%/30%/30% 明写为「每档累计上限」而非单次扣分",
      "每一档最多能扣掉多少分" in SYS and "它不是单次扣分" in SYS)
check("★ 三档累计上限换算成12/9/9 分（补上原先凑不满 30 的缺口）",
      "累计最多扣 12 分" in SYS and "累计最多扣 9 分" in SYS)
check("★ 基本语法结构档点明「唯一可以判低分」",
      "唯一可以判低分" in SYS and "10~12 分" in SYS)
check("★ 时态档扣 4~5 分且累计不超过 9 分（最多两处）",
      "时态错误" in SYS and "每处扣 4~5 分" in SYS
      and "本档累计不超过 9 分" in SYS and "即最多两处" in SYS)
check("★ 细节档扣 1~3 分且累计不超过 9 分（最多三处）",
      "每处扣 1~3 分" in SYS
      and "本档累计不超过 9 分" in SYS and "即最多三处" in SYS)
check("★ 细节档枚举含「大小写」（People/people 与 I/i）",
      "大小写" in SYS and "People/people" in SYS)
check("★ 明写「单次扣分绝不得超过 3 分」",
      "单次扣分绝不得超过 3 分" in SYS)
check("★ 明写三处细节小瑕疵扣满也只扣 9 分（防累加成 10 分以上）",
      "三处细节小瑕疵扣满也只扣 9 分" in SYS)
check("★ 明写同一处错误只归入一档（防重复扣分）",
      "只归入一档" in SYS)
check("★ 宁严勿松仍在（针对结构错误不许放过）",
      "宁严勿松" in SYS and "要扣到位，不能放过" in SYS)

# ---------------------------------------------------------------------------
print("\n[3] 改动 2：修饰语增减只影响 fidelity，不影响 fluency")
# ---------------------------------------------------------------------------

check("★ 明写「增减不是 fluency 问题」",
      "的增减不是 fluency 问题" in SYS
      or "的增减只影响 fidelity，不影响 fluency" in SYS)
check("★ 明写只影响 fidelity、仅承载实质信息时才扣",
      "只影响 fidelity" in SYS and "承载了原文的实质信息时才扣" in SYS)
check("★ 举了 reduce emissions -> reduce carbon emissions 的正例",
      '"reduce emissions"' in SYS and '"reduce carbon emissions"' in SYS)
check("★ 明写「不要因为用户没有直译出某个修饰语就扣分」",
      "不要因为用户没有直译出某个修饰语就扣分" in SYS)
check("★ fidelity 段也写了修饰语只影响 fidelity 不影响 fluency",
      "的增减只影响 fidelity，不影响 fluency" in SYS)
check("★ 明写多出来的修饰语一律不扣分（含 carbon / a set of / just / around）",
      "多出来的（用户加了原文没有的修饰语）→ 一律不扣分" in SYS
      and "carbon、a set of、just、around 等都算" in SYS)
check("★ 明写漏掉的修饰语仅在承载实质信息时才扣（1~3 分档）",
      "漏掉的（用户没译出原文的某个修饰语）" in SYS
      and "只有当该修饰语承载了原文的实质信息时才扣" in SYS)
check("★ 明写修饰语引起的「语义重复/冗余」也不扣 fluency",
      "语义重复 / 冗余" in SYS and "也不扣 fluency 分" in SYS)
check("★ 数量限定成分（a set of 等）也归入不扣 fluency",
      "数量限定成分" in SYS and "a set of" in SYS)
check("★ 明写修饰语的冗余/取舍不算语法错误",
      "修饰语的冗余或取舍属于用词问题，不算语法错误" in SYS)

# ---------------------------------------------------------------------------
print("\n[4] 改动 3：fidelity 逐项比对 + 影响权重，禁止百分比")
# ---------------------------------------------------------------------------

check("★ fidelity 用「逐项比对 + 影响权重」判分",
      "逐项比对 + 影响权重" in SYS)
check("★ 明写不要估算百分比、不输出百分比数字",
      "不要去估算百分比或输出任何百分比数字" in SYS)
check("★ 步骤 1：先把用户的答案翻译成中文与参考译文逐项对照",
      "先把用户的答案翻译成中文" in SYS and "逐项对照" in SYS)
check("★ 重要名词档 扣 6~10 分", "重要名词错译/漏译" in SYS and "扣 6~10 分" in SYS)
check("★ 动词用错档 扣 5~8 分", "动词用错" in SYS and "扣 5~8 分" in SYS)
check("★ 次要名词/形容词档 扣 1~3 分",
      "次要名词或形容词错译/漏译" in SYS and "扣 1~3 分" in SYS)
check("★ 表意过多档 每处扣 1~2 分",
      "表意过多" in SYS and "每增加一处扣 1~2 分" in SYS)
check("★ 总扣减封顶 25 分（保留下限）", "总分扣减不得超过 25 分" in SYS)
check("★ 明写「表意过多」扣分不得超出档位上沿",
      "每档的扣分区间是硬边界" in SYS and "不得超出上沿" in SYS)
check("★ 明写补充限定成分不算表意过多、一律不扣分",
      "补充一处限定成分（修饰语、程度词）**不算**表意过多" in SYS)
check("★ 边界反例：中文译文里本就没有的修饰语不扣",
      "用户的中文参考译文里本就没有某个修饰语，用户也没加 —— 不扣" in SYS)
check("★ 边界反例：「正是/恰恰/大约」对应 just/exactly/around 不扣分",
      '"正是"' in SYS and '"恰恰"' in SYS and '"大约"' in SYS
      and "just / exactly / around" in SYS
      and "这是正确的翻译习惯，不扣分" in SYS)
check("★ 边界反例：同义替换/大小写/标点/词序一律不扣",
      "同义替换、大小写、标点、词序调整 —— 一律不扣" in SYS)

# ---------------------------------------------------------------------------
print("\n[5] 改动 4：语言知识准确性（搭配/介词不许讲错）")
# ---------------------------------------------------------------------------

check("★ 指出搭配/介词用法前必须先确认用法正确",
      "必须先确认该用法本身正确" in SYS)
check("★ 区分及物动词短语与介词短语（cut down / cut down on）",
      "cut down 是及物，可直接接宾语" in SYS
      and "cut down on" in SYS and "介词是 on，不是 to" in SYS)
check("★ 明写不要把正确用法说成错误（a set of / reduce carbon emissions）",
      "不要把正确的用法说成错误" in SYS
      and "a set of new equipment" in SYS
      and "不是**语法错误" in SYS)
check("★ 没把握就不写进 issues（宁可漏报）",
      "不要写进 issues" in SYS and "宁可漏报" in SYS)

# ---------------------------------------------------------------------------
print("\n[6] 行为断言：真正发给模型的 system 里带的是新口径")
# ---------------------------------------------------------------------------

_captured = {}


def _spy(system, user, **kw):
    _captured["system"] = system
    return ('{"grammar": {"score": 28, "issues": [], "comment": "c"},'
            ' "fluency": {"score": 28, "issues": [], "comment": "c"},'
            ' "fidelity": {"score": 36, "issues": [], "comment": "c"},'
            ' "suggestion": "s"}')


with patch.object(ai, "_chat", side_effect=_spy), \
     patch.object(ai, "is_mock_mode", return_value=False):
    res = ai.review_translation("We must cut down on this budget.",
                                "我们必须削减这项预算。",
                                "We must cut down on this budget.")

check("review_translation 真实调用时用的正是 _REVIEW_SYSTEM",
      _captured.get("system") == ai._REVIEW_SYSTEM)
check("发给模型的是新口径（含「单次扣分绝不得超过 3 分」）",
      "单次扣分绝不得超过 3 分" in _captured.get("system", ""))
check("发给模型的是新口径（含「不要去估算百分比」）",
      "不要去估算百分比" in _captured.get("system", ""))
check("返回值结构仍是三维 + suggestion + total",
      set(res) >= {"grammar", "fluency", "fidelity", "suggestion", "total"},
      str(sorted(res)))
check("三维分值被夹在各自上限内",
      0 <= res["grammar"]["score"] <= 30
      and 0 <= res["fluency"]["score"] <= 30
      and 0 <= res["fidelity"]["score"] <= 40)

# ---------------------------------------------------------------------------
# 练习 #9 复盘引入的四条规则（passage 15 真实案例）
#   原文 For anyone who is not a ferocious gearhead, drastically reducing
#        this would be tempting.
#   参考译文 ...如果能大幅削减这笔开销...
#   用户答案 It is tempting for People who are not car enthusiasts to
#        cut down this budget.
# ---------------------------------------------------------------------------
print("\n[7] 练习 #9 复盘：四类判定错误")

check("★规则 1People 大小写归入 grammar「其余细节」档（与 fidelity 不扣区分开）",
      "大小写" in SYS and "People/people" in SYS
      and "同义替换、大小写、标点、词序调整 —— 一律不扣" in SYS)
check("★ 规则 2：词的概念选错归 fidelity 重要名词档，不是 fluency 问题",
      "词的\"概念选错\"属于本档，不是 fluency 问题" in SYS
      and "expense / cost / spending" in SYS
      and "事先分配的额度" in SYS)
check("★ 规则 2：点名 budget 与 expense 是两个不同概念",
      "budget 与 expense 在英语里是两个不同的概念" in SYS)
check("★ 规则 2：概念选错按 6~10 分扣并写入 fidelity 的 issues",
      "按重要名词错译扣 6~10 分" in SYS
      and "写进 fidelity 的 issues" in SYS)
check("★ 规则 3：程度副词与强度形容词承载实质信息，漏译要扣",
      "程度副词与强度形容词承载实质信息，漏译要扣" in SYS
      and "drastically" in SYS and "ferocious" in SYS)
check("★ 规则 3：点明漏译是信息量下降（降级）而非少个形容词",
      "信息量下降" in SYS and "降级成" in SYS)
check("★ 规则 4：句式结构的改变不等于错误，且不得重扣",
      "句式结构的改变不等于错误" in SYS
      and "不得写成语法错误，也不得重扣" in SYS)
check("★ 规则 4：宾语看起来不合适属词义选择，应移交 fidelity",
      "不要仅因为某个动词短语接的宾语" in SYS
      and "放到 fidelity 去判" in SYS)
check("★ 规则 4：明写 cut down this budget 语法完全成立（防模型跟着错判走）",
      "cut down this budget" in SYS and "语法完全成立" in SYS)

# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
failed = [n for n, ok in results if not ok]
for n, ok in results:
    if not ok:
        print(f"  FAIL: {n}")
print(f"总计：{'全部通过' if not failed else f'{len(failed)} 项失败'}（{len(results)} 项）")
print("=" * 70)
sys.exit(1 if failed else 0)
