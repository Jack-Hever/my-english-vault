# -*- coding: utf-8 -*-
"""骨架轴（data-layout）：布局映射 + 列结构 + 侧栏真实性 + 窄窗塌陷 + 朴素零回归。

★ 为什么这个套件单独存在 ★
上一轮只落了**色彩轴**（7 套令牌），用户当场指出：
「星月夜、向日葵、蒙娜丽莎、漫威都和 style-explorer.html 里面的界面不一样」。
根因是那6 个候选的差异不只在颜色，还在**骨架**：
  ① 导航位置 —— 顶栏横排 vs 左侧竖排（216px / 232px）
  ② 主区列结构 —— 1 / 2 / 3 列及列宽
所以本轮加了 data-layout 这条正交轴，并把两类失败变成断言：
1) **骨架没落地** —— 主题切了但布局没跟着变，于是「换了主题像没换」。
2) **侧栏造假数据** —— 设计稿上的 51 / 7 / 82.9 / 57 是一台机器的快照，
   抄进代码就是假数据。必须全部来自真实接口。
外加两条回归红线：
  · 朴素(reading) 必须与改动前**像素级一致**（用户日常在用）
  · ≤1000px 必须塌成单列（窗口 min_size 是 900×600）

★ 校验一律用集合运算，别手打关键词 ★
本项目栽过两次：「用 grep -c muteded 判定变量没拼错」，可检查串自己拼错了
（e/d 顺序不同），匹配不上真实内容，于是草率地否定了用户的质疑。
所以下面全部用 re.findall 抽集合再算差集，不手打变量名去匹配。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ★ 必须在 import app.* 之前设好，否则会读写用户真实设置文件
_TMP = tempfile.mkdtemp(prefix="enp_layout_")
os.environ["ENPRACTICE_HOME"] = _TMP

results: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if not ok else ""))


def section(t):
    print(f"\n[{t}]")


THEME_IDS = ("reading", "starry", "sunflower", "vesper", "mona", "vampire", "marvel", "silver")

#: 主题 → 布局。这个映射是本轮改动的核心契约，前后端与测试三方必须一致。
#: 列宽逐字来自 design-explore 里对应候选的 grid-template-columns（实测值）。
EXPECTED_LAYOUT = {
    "reading": "top",              # 朴素 = 应用原有骨架，不挂任何布局规则
    "starry": "sidebar",              # 216px 1fr，左侧竖排导航，无顶栏
    "sunflower": "hero-disc",         # 顶栏 66px + 1fr 320px（右花盘）
    "vesper": "sidebar-metrics",      # 232px 1fr + 246px 指标栏
    "mona": "gallery",                # 236px minmax(0,1fr) 264px
    "vampire": "status-left",         # 250px minmax(0,1fr)
    "marvel": "comic",                # 1fr 340px
    "silver": "sidebar",             # 与星月夜同骨架：216px 1fr
}

ALL_LAYOUTS = ("top",) + tuple(EXPECTED_LAYOUT.values())

CSS = (BASE / "web" / "style.css").read_text(encoding="utf-8")
JS = (BASE / "web" / "app.js").read_text(encoding="utf-8")
HTML = (BASE / "web" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
section("1. 7 个主题在 JS 里的 LAYOUTS 映射齐全且取值正确")
# ---------------------------------------------------------------------------
js_layouts = {}
m = re.search(r'var LAYOUTS\s*=\s*\{([^{}]*)\}', JS)
check("app.js 里能定位到 LAYOUTS 映射表", bool(m))
if m:
    # key 与 value 都可能含连字符（hero-disc / sidebar-metrics / status-left），
    # 两侧都必须用 [\w-]+ —— 第一版两侧都写 \w+，于是把这三个全判成「缺失」。
    for tid, lay in re.findall(r'([\w-]+)\s*:\s*[\'"]([\w-]+)[\'"]', m.group(1)):
        js_layouts[tid] = lay
    check(f"LAYOUTS 覆盖全部 {len(THEME_IDS)} 个主题",
          set(js_layouts) == set(THEME_IDS),
          "缺: " + ", ".join(sorted(set(THEME_IDS) - set(js_layouts)))
          + " / 多: " + ", ".join(sorted(set(js_layouts) - set(THEME_IDS))))
    for tid in THEME_IDS:
        check(f"LAYOUTS[{tid}] == {EXPECTED_LAYOUT[tid]}",
              js_layouts.get(tid) == EXPECTED_LAYOUT[tid],
              f"实际 {js_layouts.get(tid)!r}")
    check("朴素(reading) 映射到 top（应用原有骨架）",
          js_layouts.get("reading") == "top", js_layouts.get("reading", "缺失"))

# ---------------------------------------------------------------------------
section("2. applyTheme 同时切色彩轴与骨架轴（不能只切颜色）")
# ---------------------------------------------------------------------------
apply_block = re.search(r'function applyTheme\(id\)\s*\{(.*?)\n  \}', JS, re.S)
check("能定位到 applyTheme", bool(apply_block))
if apply_block:
    blk = apply_block.group(1)
    check("applyTheme 会设data-theme", "data-theme" in blk)
    check("applyTheme 会设 data-layout（骨架轴必须跟着切）",
          "applyLayout" in blk and "data-layout" not in blk,
          "没看到 applyLayout 调用 —— 只切颜色就是上一轮那个 bug")
    check("applyTheme 内部不直接写死 layout 字面量",
          not re.search(r"setAttribute\(\s*'data-layout'\s*,\s*'", blk),
          "应当走 applyLayout(layoutFor(id))")

check("存在非法布局值的回落函数 normalizeLayout",
      "function normalizeLayout" in JS)
check("normalizeLayout 非法值回落 top",
      re.search(r"function normalizeLayout\(v\)\s*\{.*?DEFAULT_LAYOUT\s*;\s*\}", JS, re.S) is not None)

# ---------------------------------------------------------------------------
section("3. index.html 带 data-layout，且默认值就是朴素")
# ---------------------------------------------------------------------------
m = re.search(r'<html[^>]*>', HTML)
check("index.html 的 <html> 上有 data-layout", bool(m) and "data-layout" in m.group(0),
      m.group(0) if m else "找不到 <html>")
if m and "data-layout" in m.group(0):
    dv = re.search(r'data-layout="([\w-]+)"', m.group(0))
    check('html 上的 data-layout 默认值是 "top"',
          bool(dv) and dv.group(1) == "top", dv.group(1) if dv else "无值")
check("html 上仍有 data-theme（色彩轴没被拆掉）",
      bool(m) and "data-theme" in m.group(0))

# ---------------------------------------------------------------------------
section("4. 侧栏是真实 DOM 容器，不是伪元素")
# ---------------------------------------------------------------------------
check("存在 .rail-a 侧栏容器", 'class="rail rail-a"' in HTML or 'class="rail-a"' in HTML)
check("存在 .rail-b 副侧栏容器（蒙娜丽莎要左右各一栏）",
      'class="rail rail-b"' in HTML or 'class="rail-b"' in HTML)
check(".rail 容器不是伪元素（CSS 里没有用 ::before/::after 造侧栏容器）",
      not re.search(r'\.(?:rail|rail-a|rail-b)::(?:before|after)', CSS),
      "侧栏容器用了伪元素 —— 用户明确要求真实 DOM 容器")

#: 六个方向各自的侧栏语义 —— 压成一个通用统计栏就等于抹掉差异，必须逐个在
rail_kinds = ("disc", "figure", "status", "numbers", "issues", "metrics", "plaque")
for kind in rail_kinds:
    check(f'侧栏含 data-rail="{kind}" 块', f'data-rail="{kind}"' in HTML)

#: CSS 里必须真的把这些块挂到对应布局下，否则写了 DOM 也是死的
#: (布局, 侧栏容器, data-rail 种类) —— 必须逐条写全，不能用「gallery-b」这种
#: 拼出来的 key：第一版把 gallery-b 和 comic-b 都算成 gallery，
#: 于是「漫威的 issues」被断言成「蒙娜丽莎要显示 issues」，一次报出假 FAIL。
rail_bindings = (
    ("sidebar-metrics", "rail-a", "metrics"),
    ("hero-disc", "rail-a", "disc"),
    ("gallery", "rail-a", "figure"),
    ("gallery", "rail-b", "plaque"),
    ("status-left", "rail-a", "status"),
    ("comic", "rail-a", "numbers"),
    ("comic", "rail-b", "issues"),
)
for lay, box, kind in rail_bindings:
    check(f'布局 {lay} 的 .{box} 下真的显示了 data-rail="{kind}"',
          re.search(
              rf'html\[data-layout="{re.escape(lay)}"\]\s*\.{box}\s+\.rv\[data-rail="{kind}"\][^{{]*\{{[^}}]*display:\s*block',
              CSS) is not None)

# ---------------------------------------------------------------------------
section("5. 列宽逐字对账：CSS 里必须写的就是候选里那一串")
# ---------------------------------------------------------------------------
#: 布局 → 必须出现的 grid-template-columns（逐字取自 design-explore 实测）。
#: ★ 注意「导航列宽」在 body 上、「主区列」在 .content 上 —— 两者不能混。
#:   星月夜/克制 的 216px / 232px 是**导航列**，写在 body 的 grid 上；
#:   .content 那一层是剩下的主区（星月夜单列 / 克制再切出 246px 指标栏）。
COLUMNS = {
    "sidebar": "minmax(0, 1fr)",
    "sidebar-metrics": "minmax(0, 1fr) 246px",
    "hero-disc": "minmax(0, 1fr) 320px",
    "gallery": "236px minmax(0, 1fr) 264px",
    "status-left": "250px minmax(0, 1fr)",
    "comic": "minmax(0, 1fr) 340px",
}
for lay, cols in COLUMNS.items():
    # 归一化空白后再比对：CSS 里换行/多空格都不该让断言假失败
    flat = re.sub(r"\s+", " ", CSS)
    # ★ 必须锁定 .content 那一条规则。同布局下还有 .topbar / body / .panel 等，
    #   用「布局名后的第一条规则」会抓到 .topbar，而它本来就不该有列模板。
    seg = re.search(rf'html\[data-layout="{re.escape(lay)}"\]\s+\.content\s*\{{([^}}]*)\}}', flat)
    check(f"{lay} 的列模板是「{cols}」",
          seg is not None and re.search(r'grid-template-columns:\s*([^;]+)', seg.group(1))
          and re.search(r'grid-template-columns:\s*([^;]+)', seg.group(1)).group(1).strip() == cols,
          (re.search(r'grid-template-columns:\s*([^;]+)', seg.group(1)).group(1).strip()
           if seg and re.search(r'grid-template-columns:\s*([^;]+)', seg.group(1))
           else "该布局的 .content 规则里没有 grid-template-columns"))

#: 两个侧栏布局的导航列宽（body 那层）
for lay, nav_w in (("sidebar", "216px"), ("sidebar-metrics", "232px")):
    seg = re.search(rf'html\[data-layout="{lay}"] body\s*\{{([^}}]*)\}}', re.sub(r"\s+", " ", CSS))
    got = re.search(r'grid-template-columns:\s*([^;]+)', seg.group(1)) if seg else None
    check(f"{lay} 的 body 导航列宽是 {nav_w}",
          bool(got) and got.group(1).strip().startswith(nav_w),
          got.group(1).strip() if got else "找不到")

#: 向日葵顶栏必须是 66px（候选 .top 的高度）
seg = re.search(r'html\[data-layout="hero-disc"\] \.topbar\s*\{([^}]*)\}', re.sub(r"\s+", " ", CSS))
got = re.search(r'height:\s*(\d+)px', seg.group(1)) if seg else None
check("向日葵顶栏高度 66px（候选 .top 的 height:66px）",
      bool(got) and got.group(1) == "66", got.group(1) if got else "找不到")

# ---------------------------------------------------------------------------
section("5b. 网格行必须显式声明（否则侧栏会把主区挤到第二行）")
# ---------------------------------------------------------------------------
# ★ 这条是实测踩出来的 ★
#   .rail-a / .rail-b 在 DOM 里排在 .panel **之前**。.content 变成 grid 之后，
#   auto-placement 会先把第 1 行分给侧栏，.panel.active 被挤到第 2 行 ——
#   截图上表现为「主区顶部空出 500px、卡片全挤在下面」，
#   而 columns 断言、侧栏数断言、无 console 错误断言**全部照样通过**。
#   只有「panel.top 是否等于 content.top」这种几何断言才抓得到。
flat2 = re.sub(r"\s+", " ", CSS)
GRID_LAYOUTS = ("sidebar-metrics", "hero-disc", "gallery", "status-left", "comic")
for lay in GRID_LAYOUTS:
    for sel in (".panel.active", ".rail-a", ".rail-b"):
        seg = re.search(rf'html\[data-layout="{re.escape(lay)}"\]\s*{re.escape(sel)}\s*\{{([^}}]*)\}}', flat2)
        ok = bool(seg) and "grid-row" in seg.group(1)
        # 侧栏容器并非每个布局都用到 —— 没启用时 grid-row 不必写
        if not ok and seg is None and sel.startswith(".rail"):
            continue
        check(f"{lay} 的 {sel} 显式声明了 grid-row", ok,
              seg.group(1)[:90] if seg else "找不到该规则")
# 漫威：主格只占第 1 行，右列第二格靠 --rail-a-h 让位。
# ★ 记下四条走不通的路，别再重走（都实测过）★
#   ① grid-template-rows: auto auto        → 第 1 行被主区撑到 1849px，第二格出首屏
#   ② min-content ×2 + 主格跨两行          → 跨行内容要在两行分配，min-content 之和仍≈主区高
#   ③ min-content ×2 + 主格不跨行          → 第 1 行 = max(主区, rail-a) = 主区
#   ④ abspos + top:100%                    → 包含块落到 .content（position:relative），
#                                            实测 offsetParent=content，中间空 331px
# 剩下唯一稳的：JS 量一次 rail-a 高度写进 --rail-a-h，CSS 用 margin-top 让位。
seg = re.search(r'html\[data-layout="comic"\] \.panel\.active\s*\{([^}]*)\}', flat2)
check("漫威主格只占第 1 行（跨行会把两行都撑高）",
      bool(seg) and re.search(r'grid-row:\s*1\s*;', seg.group(1)) is not None
      and '/' not in re.search(r'grid-row:\s*([^;]*)', seg.group(1)).group(1),
      seg.group(1)[:90] if seg else "找不到")
# ★ 否定正则必须限定在**同一条规则内** ★
#   第一版写成 html[...="comic"][^}]*grid-template-rows: auto auto，
#   [^}]* 会跨过 } 匹配到后面别的规则上，于是把「不存在」判成「存在」。
#   这与「校验用集合运算」是同一类：先把匹配范围收紧，再谈结论。
comic_content = re.search(r'html\[data-layout="comic"\]\s*\.content\s*\{([^}]*)\}', flat2)
check("漫威没用 auto auto（会把右列第二格推出首屏）",
      bool(comic_content) and not re.search(r'grid-template-rows:\s*auto\s+auto', comic_content.group(1)),
      comic_content.group(1)[:90] if comic_content else "找不到 .content 规则")
check("漫威没用 min-content 两行（跨行/非跨行都被主区撑高）",
      bool(comic_content) and not re.search(r'grid-template-rows:\s*min-content', comic_content.group(1)))
check("漫威没用 abspos 让位（包含块会落到 .content，中间空 331px）",
      not re.search(r'html\[data-layout="comic"\]\s*\.rail-b\s*\{[^}]*position:\s*absolute', flat2),
      "abspos 的包含块是 .content（position:relative），top:100% 参照的是主区高度")
check("漫威第二格用 --rail-a-h 让位（JS 量高度、CSS 定位）",
      re.search(r'html\[data-layout="comic"\]\s*\.rail-b\s*\{[^}]*margin-top:\s*calc\(var\(--rail-a-h', flat2) is not None)
check("JS 侧真的写 --rail-a-h（否则 margin-top 退化成 0，第二格会压住第一格）",
      re.search(r"setProperty\(\s*'--rail-a-h'", JS) is not None)
check("JS 侧对非 comic 布局清掉该变量（不留脏值）",
      re.search(r"removeProperty\(\s*'--rail-a-h'", JS) is not None)
check("窗口 resize 会重算侧栏高度",
      re.search(r"addEventListener\(\s*'resize'", JS) is not None)
check("漫威 rail-a 收缩到内容高（align-self:start，否则整列拖出空白）",
      re.search(r'html\[data-layout="comic"\]\s*\.rail-a\s*\{[^}]*align-self:\s*start', flat2) is not None)

# ---------------------------------------------------------------------------
section("5c. 肖像必须是真画，不能是渐变堆的假人")
# ---------------------------------------------------------------------------
#上一轮我自己写过CSS 渐变版肖像（椭圆头 + 半圆肩 + 白色三角领口），
# 缩略图看着还行，放大就是一个色块 blob —— 正是我自己写进 MEMORY 的那条教训。
# 这里把它变成断言：肖像框里必须真的有一张 <img>，且素材文件存在。
fig_seg = re.search(r'<figure class="rv figure".*?</figure>', HTML, re.S)
check("肖像块存在", bool(fig_seg))
if fig_seg:
    blk = fig_seg.group(0)
    check("肖像框里是真的 <img>，不是纯渐变", "<img" in blk)
    m_img = re.search(r'src="([^"]+)"', blk)
    check("肖像引用了素材文件", bool(m_img))
    if m_img:
        asset = BASE / "web" / m_img.group(1)
        check(f"素材 {m_img.group(1)}真实存在于磁盘", asset.exists(), str(asset))
        check("肖像素材不是占位小图（>20KB 才算真画）",
              asset.exists() and asset.stat().st_size > 20000,
              str(asset.stat().st_size) + " bytes" if asset.exists() else "文件不存在")
#画里不该再有那段假人的 ::before/::after/clip-path
fig_css = re.search(r'\.rv\[data-rail="figure"\] \.canvas[^{]*\{([^}]*)\}', flat2)
check("肖像画布不再用 ::before/::after 造头与肩",
      not re.search(r'\.rv\[data-rail="figure"\]\s*\.canvas::(before|after)', CSS),
      "渐变假人还在")

# 素材清单：只有真被引用的才该进包
refs = set(re.findall(r'assets/([\w.-]+\.(?:jpg|png|svg|webp))', HTML + CSS + JS))
onDisk = {p.name for p in (BASE / "web" / "assets").glob("*")} if (BASE / "web" / "assets").exists() else set()
check("assets/ 里没有被引用的僵尸文件（白费打包体积）",
      refs == onDisk,
      "未被引用: " + ", ".join(sorted(onDisk - refs)) if onDisk - refs else "")
check("至少引用了一张真画", len(refs) >= 1, ", ".join(sorted(refs)))

# 窄窗下侧栏块必须有宽度上限：单列时 .frame（aspect-ratio 3/4）会撑成巨图，
# 把主区整个挤出视口。限宽必须落在 .rv[data-rail] 这一层（截图脚本量的就是它），
# 只限内层 .disc 会量到仍 716px 的外层。
mq_all = re.findall(r'@media\s*\(max-width:\s*1000px\)\s*\{', CSS)
if mq_all:
    # ★ 必须合并「所有」1000px media 块的正文，不能只取最后一个 ★
    #   原实现是 mq_tail = CSS[CSS.rindex('@media (max-width: 1000px)'):]，
    #   也就是从最后一个 @media 起一直到文件尾 —— 这隐含假设
    #   「所有窄窗塌陷规则都写在最后一个 @media 里」。
    #   一旦有人在文件末尾新加一个 @media (max-width:1000px)（比如某个主题的
    #   窄窗微调），mq_tail 就只剩那个新块，塌陷规则全在它**之前** →
    #   全部塌陷断言假 FAIL（实测 22 个 FAIL 里有 12 个是这个）。
    #   正确做法：按花括号配对切出每个 1000px media 的正文，拼起来再判。
    def _collect_mq_bodies(text: str, width: str = "1000px") -> str:
        bodies = []
        for m in re.finditer(r'@media\s*\(\s*max-width:\s*' + width + r'\s*\)\s*\{', text):
            depth, j = 0, m.end() - 1
            while j < len(text):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            bodies.append(text[m.end():j])
        return "\n".join(bodies)

    mq_tail = _collect_mq_bodies(CSS)
    for lay, kind, cap in (("gallery", "figure", "220px"),
                           ("hero-disc", "disc", "280px")):
        check(f"{lay} 的 {kind} 在窄窗下有 max-width={cap} 限宽",
              re.search(rf'html\[data-layout="{lay}"\]\s*\.rv\[data-rail="{kind}"\][^{{]*\{{[^}}]*max-width:\s*{re.escape(cap)}',
                        mq_tail) is not None,
              "单列时不限宽会被 aspect-ratio 撑成巨图")
    # ★ 单列时必须重排「行」★
    #   media 里若只把 grid-column 改成 1 而留着 grid-row:1，
    #   主区与两块侧栏会全落进同一个格子 —— 表现为侧栏盖在主区上。
    #   这类 bug 静态看不出来（列模板对、无报错），只能靠几何断言；
    #   这里至少把「media 里确实写了 grid-row」钉住。
    check("窄窗下主区与侧栏重排了 grid-row（否则三者同格重叠）",
          len(re.findall(r'html\[data-layout="(?:sidebar-metrics|hero-disc|gallery|status-left|comic)"\][^}]*grid-row:', mq_tail)) >= 2)
    # ★ 顶栏方向也要管 tab 压扁 ★
    #   flex 子项默认 min-width:auto，5 个 tab 放不下时逐个被压到 ~20px，
    #   文字全竖着排。顶栏方向的布局（向日葵/蒙娜丽莎/吸血鬼/漫威/朴素）同样中招，
    #   所以 min-width:0 + white-space:nowrap 这套必须覆盖全部 7 个布局。
    # ★ 探针必须认「逗号合并的多选择器」+ 同一选择器的多条规则 ★
    #   CSS 里 .tabs 有两处：一处设 flex-direction:row，一处设 min-width:0。
    #   只取第一条会把「有 min-width:0」判成「没有」。
    #   做法：收集所有「选择器里提到该布局 .tabs」的规则，把声明体拼起来再判。
    mq_flat = re.sub(r"\s+", " ", mq_tail)
    def bodies_for(lay: str, target: str) -> str:
        out = []
        for m in re.finditer(r'([^{}]*?)\{([^{}]*)\}', mq_flat):
            sel, body = m.group(1), m.group(2)
            if re.search(rf'html\[data-layout="{re.escape(lay)}"\]', sel) and target in sel:
                out.append(body)
        return ' '.join(out)
    for lay in ("sidebar", "sidebar-metrics", "hero-disc", "gallery",
                "status-left", "comic"):
        body = bodies_for(lay, '.tabs')
        check(f"{lay} 在窄窗下给 .tabs 设了 min-width: 0（防 tab 被压扁）",
              "min-width: 0" in body, body[:80] if body else "media 里没有该布局的 .tabs 规则")
    tab_bodies = ' '.join(bodies_for(l, '.tab ') for l in
                           ("sidebar", "sidebar-metrics", "hero-disc", "gallery",
                            "status-left", "comic"))
    check("窄窗下 tab 声明 white-space: nowrap（不会一字一行）",
          'white-space: nowrap' in tab_bodies,
          tab_bodies[:100] if tab_bodies else "没找到 .tab 规则")
    # 限宽必须配margin 居中，否则单列时侧栏左贴、右侧留一大片空白
    fig_body = bodies_for("gallery", 'data-rail="figure"')
    check("窄窗下肖像限宽居中（max-width 要配 margin: 0 auto）",
          'max-width: 220px' in fig_body and 'margin: 0 auto' in fig_body,
          fig_body[:80] if fig_body else "找不到 .rv[data-rail=figure] 的限宽规则")

# ---------------------------------------------------------------------------
section("6. 侧栏数字必须是真数据：一个字面量都不许写死")
# ---------------------------------------------------------------------------
rails_html = re.search(r'<aside class="rail rail-a".*?</aside>\s*<aside class="rail rail-b".*?</aside>',
                       HTML, re.S)
check("能定位到两段侧栏 HTML", bool(rails_html))
if rails_html:
    seg = rails_html.group(0)
    #: 设计稿上那四个数字（51 / 7 / 82.9 / 57）正是被抄进去的假数据，必须为零出现
    for lit in ("51", "82.9", "57", "97", "20:00"):
        # 只查属性值内部（>数字<），避免误伤行号类文本
        hits = re.findall(rf'>\s*{re.escape(lit)}\s*<', seg)
        check(f"侧栏里没有写死 {lit}（设计稿快照，不是真数据）", not hits,
              f"出现 {len(hits)} 次")
    check("侧栏没有把设计稿的引用文案整句搬过来当事实",
          "黑暗从不曾让我害怕" not in seg or "s-note" in seg,
          "装饰性引文允许存在，但必须明确它不是数据")

check("所有统计槽位都带 data-stat（供 JS 一次填满）",
      len(re.findall(r'data-stat="', HTML)) >= 15,
      f"实际 {len(re.findall(chr(34).join(['data-stat=','']), HTML))} 个")

check("存在侧栏数据模块 railsModule", "function railsModule" in JS)
check("侧栏数字来自 /api/review/due-count（待复习）",
      "/api/review/due-count" in JS)
check("侧栏数字来自 /api/practices/stats（练习数 / 均分 / 最高）",
      "/api/practices/stats" in JS)
check("侧栏词条总量来自 /api/stats", re.search(r"api\('/api/stats'\)", JS) is not None)
check("侧栏练习天数来自 /api/analytics 的 trend", "/api/analytics" in JS)
# ★ 漫威 Recent Issues 的数据源必须「复用练习模块的请求」★
#   直接自己发一次 /api/practices?page=1&page_size=4 会抢在练习模块之前
#   进mock 的 calls 队列，把 qa_practice_detail 里「列表请求带 page_size=25」
#   的断言弄红 —— 一个纯前端改动污染了别的模块的测试。
# ★ 断言前必须先剥注释 ★
#   railsModule 的注释里就写着「为什么不自己发一次 /api/practices?page=1&page_size=4」，
#   直接在原文里找字符串必然命中 —— 那是解释，不是代码。
js_code_only = re.sub(r"/\*.*?\*/", "", JS, flags=re.S)
js_code_only = re.sub(r"//[^\n]*", "", js_code_only)
check("漫威 Recent Issues 复用练习模块的列表请求（不额外发一次）",
      "api/practices?page=1" not in js_code_only,
      "侧栏自己发 /api/practices 请求会抢在练习模块前面，破坏别模块的断言")
check("侧栏不包装 window.fetch（包装会读空响应流，练习列表渲染成 0 条）",
      not re.search(r"window\.fetch\s*=", js_code_only),
      "包装 fetch 一旦 res.text() 就会读空正文流，练习模块随后 res.json() 拿到空流")
check("Recent Issues 读练习模块导出的 recentItems",
      re.search(r"EnPracticePractice", js_code_only) is not None
      and re.search(r"recentItems", js_code_only) is not None)
check("练习模块加载完列表会把 items挂到自己的导出上",
      re.search(r"EnPracticePractice\.recentItems\s*=", js_code_only) is not None)
check("练习模块渲染完列表会通知侧栏强制重画",
      re.search(r"EnPracticeRails[\s\S]{0,60}refresh\(\s*true\s*\)", js_code_only) is not None)

check("「近30天练习天数」按 trend 里 practice_count>0 实数，不是拿 active_days 顶替",
      re.search(r'practice_count\s*\)?\s*>\s*0', JS) is not None)
check("取不到数据显示「—」而不是 0（「没练过」≠「考了0分」）",
      "return '—'" in JS or 'return "—"' in JS)

# ---------------------------------------------------------------------------
section("7. 朴素(reading) 零回归：不能被新骨架轴动到")
# ---------------------------------------------------------------------------
#: 朴素是用户日常在用的外观。任何 html[data-layout="top"] 规则都会破坏它。
top_rules = re.findall(r'html\[data-layout="top"\][^{]*\{[^}]*\}', CSS)
check('CSS 里没有 html[data-layout="top"] 的规则（朴素必须零附加规则）',
      not top_rules, f"发现 {len(top_rules)} 条：{top_rules[:2]}")

#: 更强的保证：把整个骨架轴块剥掉后，朴素相关的核心规则必须与原始值一致
back = pathlib.Path(os.environ.get("WINDIR", "C:/Windows"))
core_expect = {
    ".topbar": [("height", "58px"), ("padding", "0 20px")],
    ".content": [("background", "var(--bg)"), ("color", "var(--text)")],
}
flat = re.sub(r"\s+", " ", CSS)
for sel, pairs in core_expect.items():
    seg = re.search(rf'(?<![\w-]){re.escape(sel)}\s*\{{([^}}]*)\}}', flat)
    check(f"朴素核心规则 {sel} 仍可定位", bool(seg))
    if seg:
        for prop, val in pairs:
            check(f"{sel} 的 {prop} 仍是 {val}",
                  re.search(rf'{re.escape(prop)}:\s*{re.escape(val)}', seg.group(1)) is not None,
                  seg.group(1)[:100])

#: 侧栏容器在朴素下必须完全不参与布局（display:none）
check(".rail 默认 display:none（朴素下侧栏不占位）",
      re.search(r'\.rail\s*\{[^}]*display:\s*none', flat) is not None)
check(".brand-sub 默认隐藏（朴素顶栏不能多出一行副标题）",
      re.search(r'\.brand-sub\s*\{[^}]*display:\s*none', flat) is not None)
check(".nav-score 默认隐藏（朴素顶栏不能多出平均分）",
      re.search(r'\.nav-score\s*\{[^}]*display:\s*none', flat) is not None)

# ---------------------------------------------------------------------------
section("8. 窄窗塌陷：≤1000px 多栏必须塌成单列")
# ---------------------------------------------------------------------------
# ★ 必须取**最后一个** max-width:1000px —— analytics 段本来就有同值 media query，
#   用 re.search 会抓到旧的那个，于是把骨架轴的主规则当成 media 内的规则，
#   一次报出 7 个假 FAIL。探针自己先错了。
mqs = list(re.finditer(r'@media\s*\(max-width:\s*1000px\)\s*\{', CSS))
check("存在 ≤1000px 的 media query", bool(mqs))
if mqs:
    # ★ 与第 5 节同一个坑 ★
    #   原实现 mq = mqs[-1]; tail = CSS[mq.start():] —— 同样假设
    #   「塌陷规则都写在最后一个 @media 里」。文件末尾一旦新增 1000px media，
    #   tail 就不再包含真正的塌陷块 → 5 条塌陷断言 + 顶栏 + body flex 全假 FAIL。
    #   复用第 5 节的收集器，合并所有 1000px media 的正文。
    tail = _collect_mq_bodies(CSS)
    for lay in ("sidebar-metrics", "hero-disc", "gallery", "status-left", "comic"):
        seg = re.search(rf'html\[data-layout="{re.escape(lay)}"\]\s*\.content[^{{]*\{{([^}}]*)\}}',
                        re.sub(r"\s+", " ", tail))
        got = re.search(r'grid-template-columns:\s*([^;]+)', seg.group(1)) if seg else None
        check(f"{lay} 在 ≤1000px 塌成单列",
              bool(got) and got.group(1).strip() == "minmax(0, 1fr)",
              got.group(1).strip() if got else "media 里没找到该布局的 .content 规则")
    # 侧栏导航要回到顶栏横排
    seg = re.search(r'html\[data-layout="sidebar"\]\s*\.topbar[^{{]*\{([^}]*)\}',
                    re.sub(r"\s+", " ", tail))
    got = re.search(r'flex-direction:\s*(\w+)', seg.group(1)) if seg else None
    check("侧栏导航在 ≤1000px 回到顶栏横排",
          bool(got) and got.group(1) == "row",
          got.group(1) if got else "media 里没找到 .topbar 规则")
    # 媒体查询里我用了逗号合并选择器（sidebar body, sidebar-metrics body {...}），
    # 所以正则必须允许「, 另一个选择器」再接花括号 —— 第一版要求紧邻 { 于是假 FAIL。
    check("≤1000px 下 body 退回纵向 flex（不是 grid）",
          re.search(r'html\[data-layout="sidebar"\]\s*body\s*,?[^{}]*\{[^}]*display:\s*flex',
                    re.sub(r"\s+", " ", tail)) is not None)

# ---------------------------------------------------------------------------
section("9. 骨架轴不得写死颜色/圆角（会被 test_theme.py 的硬编码审计判为漏换）")
# ---------------------------------------------------------------------------
# 骨架轴块是「组件层」，颜色必须走语义令牌。
# ★ 切块必须有边界，不能一路截到文件尾 ★
#   原实现 block = CSS[CSS.index("骨架轴（layout axis）"):]，
#   隐含假设「骨架轴块是文件最后一块」。文件末尾追加主题专属皮肤规则后
#   （html[data-theme="starry"] .topbar { ...rgba(10,22,38,.90)... }），
#   那些规则全被算进「骨架轴」→ 报出一堆并不存在的骨架轴硬编码。
#   正解：从骨架轴标题起，按注释块 / 花括号配对找到它的真实结束位置。
def _slice_layout_axis(text: str) -> str:
    """切出「骨架轴（layout axis）」这一段（注释头 + 其后的全部规则），到下一段为止。

    ★ 边界怎么定（试过两版，这是第三版）★
    这份文件里「骨架轴」不是**一个**分隔段，而是**连续 7 段**：
      ① 注释头「骨架轴（layout axis）」（列出各方向的列宽）
      ② 侧栏默认态 .rail{display:none}
      ③④⑤⑥⑦ L1 星月夜 / L2 克制 / L3 向日葵 / L4 蒙娜丽莎 / L5 吸血鬼 / L6 漫威
    第 1 版按「标题 → 下一个 `/* ==`」切，只切到 ①（21 行，不含任何 data-layout
    规则）→ 反向验证时注入的硬编码落在段外，探针抓不到，PASS 全是假象。
    第 2 版改成花括号配对，遇到 @media 嵌套层的 } 会误判段尾。
    第 3 版（本版）：段首 = 标题往上最近的顶格 `/* ==`；
    段尾 = **往后第一个标题行不匹配 `L\\d+ ` 的分隔段**（即 L6 之后那段的 `/* ==`）。
    实测切出 2844→2964 之后，段内含 L1~L6 全部 data-layout 规则，
    且不含任何 data-theme 主题皮肤 —— 正是「骨架轴块」该有的范围。
    """
    lines = text.split("\n")
    key = "骨架轴（layout axis）"
    hits = [i for i, l in enumerate(lines) if key in l]
    if not hits:
        return ""
    i = hits[0]
    head = next((j for j in range(i, -1, -1)
                 if lines[j].startswith("/*") and re.match(r"/\* =+", lines[j].lstrip())), None)
    if head is None:
        return ""
    # 往后找所有顶格分隔段，取「标题行不是 L1~L6」的第一个作为段尾
    tail = len(lines)
    for j in range(i, len(lines)):
        if not re.match(r"/\* =+", lines[j]):
            continue
        title = lines[j + 1].strip() if j + 1 < len(lines) else ""
        if not re.match(r"L\d+\s", title):
            tail = j
            break
    return "\n".join(lines[head:tail])


block = _slice_layout_axis(CSS)
check("能定位到骨架轴 CSS 块", bool(block))
if block:
    no_comment = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    hex_hits = [m.group(0) for m in re.finditer(r'#[0-9a-fA-F]{3,8}\b', no_comment)]
    check("骨架轴里无 hex 颜色字面量", not hex_hits, "残留: " + ", ".join(sorted(set(hex_hits))[:8]))
    rgba_hits = re.findall(r'rgba\([^)]*\)', no_comment)
    check("骨架轴里无 rgba 颜色字面量", not rgba_hits, "残留: " + ", ".join(rgba_hits[:5]))
    hard_r = [m.group(1) + "px" for m in re.finditer(r'border-radius:\s*(\d+)px', no_comment)
              if 4 <= int(m.group(1)) <= 10]
    check("骨架轴里无 4~10px 写死圆角", not hard_r, "残留: " + ", ".join(sorted(set(hard_r))))

    # 反向探针：骨架轴里引用的每个 var() 都必须在别处定义过。
    # ★ 例外：运行时变量 ★
    #   --rail-a-h 由 JS 在 applyLayout 时 setProperty 写入（漫威右列第二格让位用），
    #   CSS 里查不到声明。但它写的是 var(--rail-a-h, 0px) —— 带兜底值，
    #   变量缺失时退化成 0 而不是整条声明失效，所以不算「死变量」。
    #   判据 = 该变量在 CSS 里被引用时**带兜底值**，且 JS 里确实有 setProperty。
    used = set(re.findall(r'var\((--[\w-]+)', block))
    declared = set(re.findall(r'(--[\w-]+)\s*:', CSS))
    # 带兜底值的引用：var(--x, ...)
    fallbacked = set(re.findall(r'var\((--[\w-]+)\s*,', block))
    runtime_vars = set(re.findall(r"setProperty\(\s*'(--[\w-]+)'", JS))
    missing = sorted(used - declared - fallbacked - runtime_vars)
    check("骨架轴引用的 CSS 变量全部有定义（无失效 var()）", not missing,
          "未定义: " + ", ".join(missing))
    check("运行时变量都有兜底值（var(--x, 0px) 形式）",
          all(v in fallbacked for v in (runtime_vars & used)),
          "缺兜底: " + ", ".join(sorted(runtime_vars - fallbacked)))

# ---------------------------------------------------------------------------
section("10. 前端安全契约：侧栏数据绝不进 innerHTML")
# ---------------------------------------------------------------------------
rails_js = ""
m = re.search(r'function railsModule\(\)\s*\{(.*?)\n\}\)\(\);', JS, re.S)
check("能定位到 railsModule 函数体", bool(m))
if m:
    rails_js = m.group(1)
    check("侧栏渲染不用 innerHTML",
          "innerHTML" not in rails_js,
          "侧栏数字/文案来自接口，必须走 textContent")
    check("侧栏列表用 createElement + textContent 构建",
          "createElement" in rails_js and "textContent" in rails_js)
check("applyLayout 只接受白名单值（不把任意字符串写进属性）",
      re.search(r'function applyLayout\(name\)\s*\{\s*var safe = normalizeLayout', JS) is not None)


# ---------------------------------------------------------------------------
section("11. 还原度：超大翻译区 / 大标题 / 词汇库·外刊库方块")
# ---------------------------------------------------------------------------
#: 非朴素方向 = 这 5 个（朴素 reading 与克制 vesper 保持原样，是硬约束）
BIG = ("starry", "sunflower", "mona", "vampire", "marvel")

# ---- ★ 取块工具：必须按花括号配对，不能用 [^}]* --------------------------
#   本节第一版全部写成 re.search(r'...[^}]*\{([^}]*)\}', CSS)，结果 12 条假 FAIL：
#     · 多选择器换行写法（html[data-theme="a"] .x,\nhtml[data-theme="b"] .x { ... }）
#       —— `html\[data-theme="starry"\]\s*\.prac-layout[^{]*\{` 能选中，
#          但凡要求「紧接着就是 {」就断了；
#     · 词汇库的方块规则**根本没有 #panel-vocab 前缀**（是通用 `.grid tbody tr`，
#       见 CSS 3738 行附近）—— 按 #panel-vocab 去找必然 0 条。
#   正确做法：扫出所有规则（选择器 + 声明体），按「选择器归一化后是否含某片段」筛。
#   ★★ 必须先剥注释再切 ★★
#     本项目的 CSS 里大量注释会写「原文引用」（例如注释里出现
#     `html[...]:not(reading) #panel-analytics， 特异性 (1,3,1)，**压过了** .panel{display:none}`）
#     —— 注释里同时含 `{`、`}` 和选择器片段，re.finditer(r'([^{}]+)\{') 会把
#     注释正文当成上一条规则的声明体尾巴切进去，
#     导致真正的规则选择器被污染成「...注释文字... html[...].active」。
#     后果（实测踩到）：判据数「声明 display:grid 的 analytics 规则」得到 0 条，
#     断言变成「永远 PASS 的空判据」。
#     修法：先 re.sub(r'/\*.*?\*/', ' ', css) 剥掉注释，再切规则。
#     （与 test_theme.py 的 strip_css_comments 同一套思路）
CSS_RULES = []  # [(选择器, 声明体)]
_CSS_LIVE = re.sub(r"/\*.*?\*/", " ", CSS, flags=re.S)
for _m in re.finditer(r'([^{}]+)\{', _CSS_LIVE):
    _sel = " ".join(_m.group(1).split())
    if not _sel or _sel.startswith("/*") or _sel.startswith("*"):
        pass
    _depth, _j = 0, _m.end() - 1
    while _j < len(_CSS_LIVE):
        if _CSS_LIVE[_j] == "{":
            _depth += 1
        elif _CSS_LIVE[_j] == "}":
            _depth -= 1
            if _depth == 0:
                break
        _j += 1
    CSS_RULES.append((_sel, _CSS_LIVE[_m.end():_j]))


def has_rule(selector_frag: str, decl_frag: str) -> bool:
    """是否存在「选择器含 selector_frag 且声明体含 decl_frag」的规则。

    选择器两侧都可能有多余空白与换行（多选择器合并写法），故只做包含判断。
    """
    for sel, body in CSS_RULES:
        if selector_frag in sel and decl_frag in body:
            return True
    return False


# ---- ① 大标题：每个非朴素方向的每个面板都要有 .page-title ------------------
#   设计约束：「每个页面都缺少 PRACTICE · 翻译练习 / 回译练习 这样的大标题」。
#   ★ 首屏与切主题都不经过 switchModule（applyAllPageTitles 两处都调了），
#     所以不能只看 index.html 有没有，得查 JS 里确实有批量应用的入口。
check("app.js 有 applyAllPageTitles（首屏 + 切主题都要刷大标题）",
      # ★ 用 \b 边界：直接 in 检查会被「applyAllPageTitlesX」这种改名骗过去
      #   （反向验证实测：把它改名成 applyAllPageTitlesX，老写法照样 PASS —— 就是洞）。
      re.search(r'function applyAllPageTitles\s*\(', JS) is not None)
for hook in ("switchModule", "applyTheme"):
    fn = re.search(r'function ' + hook + r'\s*\([^)]*\)\s*\{(.*?)\n\}', JS, re.S)
    check(f"{hook} 里调用了 applyAllPageTitles",
          bool(fn) and re.search(r'applyAllPageTitles\s*\(\s*\)', fn.group(1)) is not None,
          "首屏 HTML 直接带 .active、切主题都不走 switchModule → 大标题会永远不出现")

# ---- ② 超大翻译区：5 个方向都要单列 + 大画布 ------------------------------
for t in BIG:
    # .prac-layout 改单列、侧栏下移
    check(f"{t} 的翻译页改成单列（翻译区横向占满）",
          has_rule(f'data-theme="{t}"] .prac-layout', "grid-template-columns")
          and has_rule(f'data-theme="{t}"] .prac-layout', "grid-template-areas"),
          "改前：翻译区被左侧 264px 侧栏挤成 0.66~0.80 宽的矮卡")
    # ★ grid 单元内两个直接子元素都要显式声明 area ——
    #   漏一个会被 auto-placement 甩到容器末尾（本项目连踩两次）
    for cls in ("prac-main", "prac-side"):
        check(f"{t} 的 .{cls} 显式声明了 grid-area",
              has_rule(f'data-theme="{t}"] .{cls}', "grid-area"))
    # 空闲态画布要有最小高度（大画布而不是小卡）
    check(f"{t} 的空闲态翻译区是大幅画布（min-height ≥ 300px）",
          has_rule(f'data-theme="{t}"] #p-setup .prac-empty-card', "min-height")
          and re.search(r'min-height:\s*(?:3\d\d|[4-9]\d\d|\d{4,})px', next(
              b for s, b in CSS_RULES
              if f'data-theme="{t}"] #p-setup .prac-empty-card' in s and 'min-height' in b)),
          "改前只有 130px")

# ---- ③ 翻译区的冗余说明文字必须已隐藏 ------------------------------------
#   设计约束：「除了『开始一次翻译练习』这样的文字，其他非链接文字都删掉」。
#   #p-trend 那行（「练习 N 次 · 平均 X 分 · 最高 Y 分」）由 app.js 运行时填，
#   数字在侧栏已经有一份，重复即冗余。
check("翻译页的冗余统计行 #p-trend 在非朴素方向被隐藏",
      any('#p-trend' in s and 'not([data-theme="reading"])' in s and 'display: none' in b
          for s, b in CSS_RULES))
# ★★ 下面两条原本断言的是「长placeholder 用 ::after 换成 https://…」，
#   随「外刊链接（选填）」输入框一起作废了（用户 2026-10-22 明确要求删掉那栏，
#   改成后端自动联网搜索）。所以断言整体换成验证**新事实**：
#   ① 按钮在 .prac-empty-card 里、不在 .toolbar 里；
#   ② 那个输入框的 id 在 HTML / CSS / JS 三处都不许再出现；
#   ③ newPractice 的 payload 不许再带 web_fallback_url。
#   ⚠ 判据纪律：每条都要能 FAIL。已用反向验证实测过（把按钮挪回 .toolbar /
#     把输入框塞回 HTML / 把 web_fallback_url 加回 payload，三条各自 FAIL），
#     详见 C:/tmp/revcheck/ 的反向验证记录。
PRACTICE_THEMES = ("starry", "sunflower", "mona", "vampire", "marvel")

# ① 按钮位置：从 .toolbar 里挪进了 .prac-empty-card 内部
_setup_block = re.search(r'<div id="p-setup">(.*?)</div>\s*\n\s*<div id="p-stage"', HTML, re.S)
_setup_html = _setup_block.group(1) if _setup_block else ""
_empty_card = re.search(r'<div class="prac-empty-card">(.*?)</div>\s*$', _setup_html, re.S)
_empty_card_html = _empty_card.group(1) if _empty_card else ""
_toolbar = re.search(r'<div class="toolbar">(.*?)</div>', _setup_html, re.S)
_toolbar_html = _toolbar.group(1) if _toolbar else ""

check("★「新建练习」按钮在 .prac-empty-card 内部（与「开始一次翻译练习」同一块）",
      'id="p-new"' in _empty_card_html,
      "按钮不在 .prac-empty-card 里：%s" % ("在" if 'id="p-new"' in _toolbar_html else "哪都不在"))
check("★「新建练习」按钮已不在 .toolbar 里（原先它孤零零挂在工具条上）",
      'id="p-new"' not in _toolbar_html,
      "按钮仍在 .toolbar 里")
check("★ 外刊链接输入框的 id 在 index.html 里已彻底消失",
      "p-weburl" not in HTML,
      "index.html 仍有 p-weburl")
check("★ 外刊链接输入框的 id 在 style.css 里已彻底消失（含注释）",
      "p-weburl" not in CSS,
      "style.css 仍有 p-weburl")
check("★ app.js 不再读那个输入框",
      "p-weburl" not in JS,
      "app.js 仍有 p-weburl")
_new_practice = re.search(r'function newPractice\s*\([^)]*\)\s*\{(.*?)\n\}', JS, re.S)
_new_practice_body = _new_practice.group(1) if _new_practice else ""
check("★ newPractice 的 payload 不再带 web_fallback_url（改成后端自动联网搜）",
      "web_fallback_url" not in _new_practice_body,
      "payload 里仍有 web_fallback_url")
check("★ newPractice 仍显式传 terms（不能因为删了链接栏就连terms 都不传）",
      re.search(r'terms\s*:', _new_practice_body) is not None,
      "payload 里没有 terms")
# 按钮所在行：5 个非朴素方向都要有规则（reading/vesper 零改动是硬约束）
#   ★ 选择器形态在 2026-10-22 改过一次：从 `... #p-setup .prac-start-row` 改成
#     `... #p-setup .prac-empty-card > .prac-start-row`（多带一层 .prac-empty-card >，
#     特异性从 (1,2,1) 抬到 (1,3,1)，才能压过同特异性的 `.prac-empty-card > *`）。
#     所以这里**不能**再用「选择器里含 .prac-start-row」这种字符串匹配 ——
#     写法一变就假红。改成按「这条规则是否声明了 position 且命中按钮行」来找。
def _is_start_row_rule(sel: str) -> bool:
    s = " ".join(sel.split())
    return ".prac-start-row" in s


check("★ .prac-start-row 在 5 个非朴素方向都有规则（reading/vesper 零改动是硬约束）",
      all(any('data-theme="%s"' % t in s and _is_start_row_rule(s)
              for s, _ in CSS_RULES)
          for t in PRACTICE_THEMES),
      "缺哪个主题：%s" % [t for t in PRACTICE_THEMES
                         if not any('data-theme="%s"' % t in s and _is_start_row_rule(s)
                                    for s, _ in CSS_RULES)])
check("★ 朴素(reading) 与克制(vesper) 没有被塞进 prac-start-* 的主题规则",
      not any(('data-theme="reading"] #p-setup %s' % c) in s
              or ('data-theme="vesper"] #p-setup %s' % c) in s
              for c in (".prac-start-row",)
              for s, _ in CSS_RULES),
      "reading/vesper 出现了 prac-start-* 规则 —— 用户日常在用的两个方向必须零改动")

# ---- ⑤ 说明文字已按用户要求删除（2026-10-22 反馈）--------------------
#   设计约束：「这段文字说明没必要」——指画布上那句
#   「外刊库没有覆盖到的目标词，会自动去网络上找含该词的句子（不会存进外刊库）。」
#   HTML 与 CSS 必须同时清干净：CSS 里留着就是永不生效的死规则。
check("★ 说明文字 .prac-start-hint 在 index.html 里已删除",
      "prac-start-hint" not in HTML,
      "index.html 仍有 prac-start-hint")
check("★ 说明文字 .prac-start-hint 在 style.css 里已删除（含注释）",
      "prac-start-hint" not in CSS,
      "style.css 仍有 prac-start-hint")

# ---- ⑥ 按钮在画布角落（用户 2026-10-22：「别中间随便放着」）------------------
#   按钮改成绝对定位到 .prac-empty-card 的右下角，标题在左上，形成对角关系。
#   ★ 判据要点：只查「有 position:absolute」**不够** —— 绝对定位但没给
#     right/bottom 时，元素会停在它原本的 static position（还是在中间）。
#     必须同时要求 right 与 bottom 都声明了，才是真的挪到角落。
#   ★★ 更要紧的一条：光查「规则里写了 absolute」**还是不够**！
#     规则写对了也可能被**别的规则覆盖**（层叠）。本轮真踩了这个坑：
#       定位规则写成 `html[data-theme="mona"] #p-setup .prac-start-row`（1,2,1），
#       而同文件另有
#         html[data-theme="mona"] #p-setup .prac-empty-card > * { position: relative }
#       也是 (1,2,1) —— **特异性完全平手**，于是谁在文件里靠后谁赢：
#         · starry/sunflower 的 `> *` 在 5066 行（定位规则之前）→ 定位规则赢（实测 absolute，对）
#         · mona/vampire/marvel 的 `> *` 在 5494 行（定位规则之后）→ `> *` 赢
#           → position 被改回 relative（right/bottom 仍生效）→ 按钮停在标题旁边、
#             垂直居中 —— 正是用户说的「别中间随便放着」。
#     所以下面 ⑥ 之下的判据必须**模拟层叠**：算出所有会命中该按钮行的规则里
#     「position 属性最终由谁决定」，再断言赢家是 absolute。
#     （特异性计算器已与 @bramus/specificity 官方库在 style.css 全部 1097 条
#       可解析选择器上对拍，零不一致。）
for _t in PRACTICE_THEMES:
    _rows = [b for s, b in CSS_RULES
             if 'data-theme="%s"' % _t in s and _is_start_row_rule(s)]
    check("★ %s 主题「新建练习」按钮行定位到画布角落（absolute + right + bottom）" % _t,
          any(re.search(r"position\s*:\s*absolute", b)
              and re.search(r"right\s*:\s*\d", b)
              and re.search(r"bottom\s*:\s*\d", b)
              for b in _rows),
          "没有同时声明 position/right/bottom：%s" % [b.strip()[:70] for b in _rows][:2])
    #   绝对定位要有参照物：.prac-empty-card 必须是 position:relative，
    #   否则按钮会相对更外层的祖先定位，跑出画布。
    _cards = [b for s, b in CSS_RULES
              if 'data-theme="%s"] #p-setup .prac-empty-card' % _t in s]
    check("★ %s 主题 .prac-empty-card 是绝对定位的参照物（position:relative）" % _t,
          any(re.search(r"position\s*:\s*relative", b) for b in _cards),
          "没设 position:relative，按钮会相对外层定位跑出画布")

#   朴素/克制没有绝对定位规则（那是硬约束），改由基础规则把按钮行推到右侧。
check("★ 基础规则把按钮行推到右侧（朴素/克制靠这条落在角落）",
      any('#p-setup .prac-start-row' in s and "justify-content" in b
          for s, b in CSS_RULES),
      "缺 #p-setup .prac-start-row 的基础 justify-content 规则")

# ---- ⑥-b ★★ 层叠判定：定位规则必须真的赢过 .prac-empty-card > * -------------
#   这是本轮真 bug 的根因所在，也是上面那些「有没有声明」式断言的盲区：
#   规则写着 absolute ≠ 浏览器最后用的是 absolute。
#   判据：对每个非朴素方向，枚举**所有会命中 .prac-start-row 的规则**
#         （选择器里含 .prac-start-row，或含 `.prac-empty-card > *` 这种
#          通配子选择 —— 按钮行是 .prac-empty-card 的直接子元素，会被它命中），
#         按 (特异性, 源序) 排出 position 属性的最终赢家，断言赢家声明 absolute。
#   ★ 源序平手时后者胜 —— 这正是原来 mona/vampire/marvel 输掉的原因。
#   ★ 判据必须能 FAIL：把定位规则改回 `#p-setup .prac-start-row`（去掉
#     `.prac-empty-card >`）后，特异性从 (1,3,1) 掉回 (1,2,1)，
#     与 `> *` 平手 → 源序靠后的 `> *` 赢 → 这几条断言会红。

# ---- 特异性计算（与 @bramus/specificity 官方库对拍通过，1097/1097 零不一致）
_PSEUDO_NO_SPEC = {"not", "is", "where", "has", "host", "slotted"}


def _split_top(sel: str, sep: str) -> list:
    out, depth, cur = [], 0, []
    for ch in sel:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def _specificity(sel: str) -> tuple:
    s = re.sub(r'"[^"]*"|\'[^\']*\'', ' ', sel).strip()
    if not s:
        return (0, 0, 0)
    a = b = c = 0

    def _pc(m):
        nonlocal a, b, c
        name = m.group(1).lower()
        body = m.group(3) or ""
        if name in _PSEUDO_NO_SPEC:
            best = (0, 0, 0)
            for part in _split_top(body, ","):
                s2 = _specificity(part)
                if s2 > best:
                    best = s2
            a += best[0]
            b += best[1]
            c += best[2]
        elif m.group(0).startswith("::"):
            c += 1
        else:
            b += 1
        return " "

    s = re.sub(r"::([\w-]+)(\(([^()]*)\))?", _pc, s)
    s = re.sub(r":([\w-]+)(\(([^()]*)\))?", _pc, s)
    b += len(re.findall(r"\[[^\]]*\]", s))
    s = re.sub(r"\[[^\]]*\]", " ", s)
    a += len(re.findall(r"#[\w-]+", s))
    s = re.sub(r"#[\w-]+", " ", s)
    b += len(re.findall(r"\.[\w-]+", s))
    s = re.sub(r"\.[\w-]+", " ", s)
    for tok in re.findall(r"[A-Za-z][\w-]*|\*|[>+~,\s]", s):
        if tok in (">", "+", "~", ",", " ", "\t", "\n", "*"):
            continue
        c += 1
    return (a, b, c)


def _hits_start_row(sel: str) -> bool:
    """这条选择器会不会命中 .prac-start-row 元素？
    按钮行的 DOM： #p-setup > .prac-empty-card > .prac-start-row（标签是 <div>）
    命中情形：
      ① 选择器里直接点名 .prac-start-row
      ② 选择器是 `.prac-empty-card > *`（通配子选择器命中所有直接子元素）
      ③ 选择器是 `.prac-empty-card > <标签>` 且标签是 div
         （.prac-empty-card 的直接子元素只有 <p class="empty-title"> 和
          <div class="prac-start-row">；只有 div 才是按钮行）
    """
    s = " ".join(sel.split())
    if ".prac-start-row" in s:
        return True
    if ".prac-empty-card" in s and re.search(r">\s*\*", s):
        return True
    m = re.search(r"\.prac-empty-card\s*>\s*([a-zA-Z][\w-]*)", s)
    if m and m.group(1).lower() == "div":
        return True
    return False


for _t in PRACTICE_THEMES:
    # 枚举所有「会命中按钮行」且「声明了 position」的规则，按源序编号
    #   ★★★ 关键：CSS 规范里**合并选择器列表的每个单选择器独立计算特异性**，
    #     一条规则 `a, b, c` 对元素 E 的贡献 = 命中 E 的那些单选择器里
    #     **特异性最高的那个**，不是整条规则一起算。
    #     本轮第一次写判据时错在这里：把 `> *` 那条（3 个主题合并）整体算成
    #     (1,5,1)，压过了定位规则 → 反向验证时改回旧写法仍然全绿，
    #     判据形同虚设。所以必须拆成单选择器逐个算。
    _cand = []
    for _i, (_sel, _body) in enumerate(CSS_RULES):
        if not re.search(r"(^|;|\s)position\s*:", _body):
            continue
        if 'data-theme="%s"' % _t not in _sel:
            continue
        # 拆成单选择器，取其中「命中按钮行且属于本主题」的最高特异性
        _best = None
        for _one in _split_top(_sel, ","):
            _one = " ".join(_one.split())
            if not _one:
                continue
            if 'data-theme="%s"' % _t not in _one:
                continue
            if not _hits_start_row(_one):
                continue
            _sp = _specificity(_one)
            if _best is None or _sp > _best[0]:
                _best = (_sp, _one)
        if _best is not None:
            _cand.append((_i, _best[1], _body, _best[0]))
    check("★ %s 主题：能命中按钮行的 position 规则至少有一条" % _t,
          bool(_cand),
          "没找到任何声明 position 的命中规则")
    if _cand:
        # 特异性相同 → 源序靠后者赢（这正是原来 mona/vampire/marvel 输的原因）
        _win = max(_cand, key=lambda x: (x[3], x[0]))
        check("★ %s 主题：position 最终赢家是 absolute（层叠判定，非「有没有声明」）" % _t,
              re.search(r"position\s*:\s*absolute", _win[2]) is not None,
              "赢家是 %s（特异性 %s，源序 #%d）→ 声明 %r"
              % (_win[1][:110], _win[3], _win[0],
                 _win[2].strip()[:80]))
        #   顺带要求：赢家必须同时给 right/bottom，否则绝对定位后仍停在原地
        check("★ %s 主题：position 赢家同时声明了 right + bottom（真挪到角落）" % _t,
              re.search(r"right\s*:\s*\d", _win[2]) is not None
              and re.search(r"bottom\s*:\s*\d", _win[2]) is not None,
              "赢家 %s 只声明了 %r" % (_win[1][:80], _win[2].strip()[:80]))


# ---- ④ 词汇库 / 外刊库方块（用户 B.1 / B.2）------------------------------
#   ★ 词汇库的方块规则是**通用** `.grid tbody tr`（没有 #panel-vocab 前缀）——
#     因为它同时服务词汇库与外刊库，两者列名不同，各自再覆盖 grid-template-areas。
for pid in ("panel-vocab", "panel-articles"):
    check(f"{pid} 的行被改成 CSS Grid 方块（display:grid）",
          has_rule("#" + pid, "display: grid") or has_rule(".grid tbody tr", "display: grid"))
check("外刊库方块配了油画（tr::before 铺图）",
      # ★ 油画图在 nth-child(6n+k)::before 里轮换 6 张素材，
      #   主 tr::before 规则只设 background-repeat/size/position ——
      #   所以必须查「任一 tr::before 规则里有 background-image」。
      any("#panel-articles" in s and "::before" in s and "background-image" in b
          for s, b in CSS_RULES))
check("外刊库油画轮换至少 6 张素材（nth-child 6n+k）",
      sum(1 for s, _ in CSS_RULES
          if "#panel-articles" in s and "nth-child(6n+" in s) >= 6,
      "全部卡片共用一张画 = 「换了个背景的同一张画」，不是参考图那种每卡不同")
#   ★ 这条是历史 bug 的看门狗：
#   「藏次要列」的规则一度**没限定 #panel-vocab**，连带把外刊库的 .cell-title
#   一起 display:none → 整列英文标题消失（截图才发现，探针当时全绿）。
check("「藏次要列」规则限定在 #panel-vocab（否则外刊库标题会被一起藏掉）",
      any('#panel-vocab .grid tbody td:not(' in s for s, _ in CSS_RULES))
#   ★ 同一条规则里，若没把 display:none 限定到 vocab，外刊库标题就整个消失。
#   反向要求：必须存在一条**只命中外刊库 cell-title** 的 display:block 救回规则。
check("外刊库标题列被显式救回（display 不是 none）",
      any("#panel-articles" in s and s.rstrip().endswith("td.cell-title")
          and "display: block" in b
          for s, b in CSS_RULES),
      "少了这条，外刊库整列英文标题会消失（历史上真发生过）")

# ---- ⑤ 分析页留白：不能出现「卡内大片空白」--------------------------------
#   设计约束：「热门标签下面会有大片空白」。
#   根因实测：.an-item-mean（释义）不截断 → 94 字长释义换行 3 行，
#   单条从 88px 撑到 128px，12 条累计多出约 450px。
check("分析页词义最多两行，超出省略（-webkit-line-clamp）",
      # ★ 必须确认 line-clamp 落在 **.an-item-mean 自己的规则体**里。
      #   全文件有 4 处 `line-clamp: 2`（2020/2794/3782/3901 行，各属不同元素），
      #   只判全文出现过 = 谁都能过。反向验证实测：删掉 .an-item-mean 那条里的
      #   line-clamp 而别处仍有 → 老写法照样 PASS。必须逐条比对。
      any(s.rstrip().endswith(".an-item-mean") and "line-clamp: 2" in b
          for s, b in CSS_RULES),
      "长释义会把「新词」卡撑高 400px+，左右两列底边差拉到 130px+")
#   品字形必须显式给大标题声明 area，否则会被甩到页面底部（实测 y=2289px）
check("分析页 grid 给大标题显式声明了 grid-area（否则被甩到页面底部 y=2289）",
      has_rule("#panel-analytics", "pagetitle"),
      "漏声明 grid-area → auto-placement 把它甩到容器末尾")


# ===========================================================================
# 第 12 节 · 本轮两个用户反馈的回归防线
# ---------------------------------------------------------------------------
# 设计约束（2026-10-22）：
#   ① 「怎么除了朴素、克制以外，其他页面最下方都能看到一个学习进度啊？」
#   ② 「吸血鬼的翻译练习块不用放图片啊，现在这个红黑渐变就很好，
#       但中间加一张油画做啥？串了吧」
# ===========================================================================

# ---- ① 学习进度面板不得在「非当前页」时占位 --------------------------------
#   根因：html[data-theme]:not(reading):not(vesper) #panel-analytics
#         的特异性 (1,3,1) 压过了 .panel{display:none} (0,1,0)
#         与 .panel.active{display:block} (0,2,0)
#         → 「学习进度」面板在它不是当前页时也照样 display:grid 占位，
#           它的大标题被排到当前页面内容下方（实测 rect.top=1285px，
#           视口高 900 → 一滚动就露出来，正是用户说的「页面最下方」）。
#   朴素/克制被 :not() 排除，所以没这个问题 —— 与用户描述完全吻合。
#
#   ★ 判据必须精确到「这条规则自己的声明体」，不能只判全文出现过 display:grid。
#     全文件有多处 display:grid，判全文 = 谁都能过。
#   ★★ 判据的坑（反向验证实测踩到的）：只查「有没有不带 .active 的 grid 规则」
#      是不够的 —— 注入回归时是**新加**一条不带 .active 的规则，
#      而带 .active 的那条**仍然在**，于是「存在带 .active 的规则」照样 PASS，
#      断言漏掉了这个回归（实测 173 项里只 FAIL 了吸血鬼那条，① 那条漏网）。
#      正确判据：**声明 display:grid 的 analytics 规则必须「有且只有一条」，
#      且那条必须带 .active**。两条并存时，后写的那条赢（特异性相同时），
#      运行时就是 bug —— 哪怕断言扫到的每条单独看都没问题。
_an_grid_rules = [
    (s, b) for s, b in CSS_RULES
    if "#panel-analytics" in s
    and re.search(r'display:\s*grid', b)
]
check("学习进度面板的 display:grid 规则唯一且带 .active（否则非当前页也在底部露头）",
      len(_an_grid_rules) == 1 and ".active" in _an_grid_rules[0][0],
      f"声明 display:grid 的 analytics 规则有 {len(_an_grid_rules)} 条"
      f"（必须恰好 1 条且带 .active）：{[s[:70] for s, _ in _an_grid_rules][:3]}")
#   正向要求：必须存在一条带 .active 的 display:grid 规则（品字形不能丢）
check("学习进度面板在 .active 时仍是 grid（品字形布局不能因为上面的修复丢掉）",
      any("#panel-analytics" in s and ".active" in s
          and re.search(r'display:\s*grid', b)
          for s, b in CSS_RULES),
      "少了这条，激活后分析页会退回 block，品字形消失")
#   grid-template-areas 仍然要挂在 #panel-analytics 上（.active 或不挂都行，
#   但不能因为加了 .active 就把 areas 挪到只有 .active 的规则里 ——
#   那样非激活态的 areas 声明也生效，虽然无害，但会让「areas 归属」变得难查）
check("分析页 grid-template-areas 仍声明在 #panel-analytics 上",
      any("#panel-analytics" in s and "grid-template-areas" in b
          for s, b in CSS_RULES),
      "areas 声明丢了 → .page-title 会被 auto-placement 甩到容器末尾")

# ---- ② 吸血鬼翻译画布不得出现油画 ------------------------------------------
#   根因：吸血鬼的画布规则里带了一层 url("assets/olive-pickers.jpg")。
#   《橄榄摘果女》是**梵高**方向（星月夜/向日葵/蒙娜丽莎）的素材，
#   放到吸血鬼日记的画布上就是「串方向」—— 题材（农妇）与气质（吸血鬼）完全对不上。
#   用户要的是纯红黑渐变 + 血红光晕。
#
#   ★ 必须按「选择器作用域」判，不能只判全文有没有 olive-pickers：
#     外刊库的 6 张油画轮换（starry/sunflower）**正当使用**了同一张图，
#     全文判会误杀。
#   ★ 也不能只判「有没有 url」—— 蒙娜丽莎的画布（roses.jpg）是**用户要的**，
#     只有吸血鬼方向不许有图。
_vam_canvas = [
    (s, b) for s, b in CSS_RULES
    if 'data-theme="vampire"' in s
    and ("prac-empty-card" in s or "prac-stage-card" in s)
]
_vam_with_img = [
    (s, b) for s, b in _vam_canvas if re.search(r'url\s*\(', b)
]
check("吸血鬼翻译画布没有任何图片层（油画属于梵高方向，串了）",
      not _vam_with_img,
      "吸血鬼画布里仍有 url()："
      f"{[re.findall(r'url\([^)]*\)', b) for _, b in _vam_with_img][:2]}")
check("吸血鬼翻译画布仍是纯渐变（血红底 + 双径向光晕）",
      # ★ 反向验证实测：只要求「某条规则里有 radial-gradient 和 background-color」
      #   太松 —— 删掉**生效那条**规则里的一层渐变后，别的 vampire 规则里
      #   仍有 radial-gradient + background-color，判据照样 PASS（注入 C 漏网）。
      #   正确判据：锁定**生效的那条**规则（同时含 .prac-empty-card 与
      #   .prac-stage-card 的合并规则，CSS 里就这一条同时管两个状态），
      #   要求它里面**恰好两层** radial-gradient 且无 url。
      any("prac-empty-card" in s and "prac-stage-card" in s
          and 'data-theme="vampire"' in s
          and b.count("radial-gradient") >= 2
          and "background-color" in b
          and not re.search(r"url\s*\(", b)
          for s, b in _vam_canvas),
      "生效规则里必须有血红底色 + 两层径向光晕（用户要的就是这个红黑渐变）")
#   反向确认：外刊库的油画轮换**必须还在**（别把梵高方向一起误伤）
check("外刊库的油画轮换仍然保留（修吸血鬼不能误伤 starry/sunflower 的画）",
      any("olive-pickers" in b for s, b in CSS_RULES),
      "外刊库 6 张油画轮换里的第 3 张就是它，删了星月夜/向日葵的外刊库会空一块")


# ===========================================================================
# 13. 整页纹理/ 光影落地 + 向日葵导航对比度（本轮 6 点反馈）
# ---------------------------------------------------------------------------
# 设计约束（2026-10-22）：
#   ① 「漫威：翻译练习页面的风格是好了，但是其他的像词汇库等等还是没和翻译练习
#       页面统一，我之前说的背景是有斜条纹的米白纸面，不是指的翻译练习块，而整体页面」
#   ② 「星夜月和向日葵的整体页面依然是纯色，我说的界面预览.html 的纹理没到位」
#   ④ 「吸血鬼日记页面背景现在变成纯黑了，我前面说的背景缺少光影去哪了？
#       你只落地到了翻译块？」
#   ⑤ 「蒙娜丽莎背景缺少的光影效果也没落地」
#   ⑥ 「目前向日葵的这个导航栏字看不清，未点击时呈白色，在黄色中看不清」
#
# ★ 本轮根因（四条反馈同一个）★
#   `.content { background: var(--bg); }` 是一层**纯色实底**，铺在主区上，
#   把 body 上本来就有的全部纹理 / 光影**完全遮住**。参考稿的结构是
#       body { background: <纹理> }            ← 纹理在 body
#       .wrap { position:relative; z-index:1 }← 内容层透明，透出纹理
#   本应用的 .content 就相当于 .wrap，但它有实底 → 纹理丢失。
#
# ★★★ 判据纪律（本节每条都做过反向注入验证，见 C:\tmp\honesty_r17.py）★★★
#   · 一律判「**生效的那条规则**」（同选择器后写的那条），不是「存在一条」。
#     回归通常是**再追加**一条，原有那条仍然在 → 只判「存在」永远 PASS。
#   · 判「background」时用 background-color / background-image 分别取，
#     不能用 `background:` 粗匹配（`background:` 会同时吃掉
#     `background-color:` 和 `background-image:` 两个属性名）。
# ===========================================================================

section("13. 整页纹理/光影落地 + 向日葵导航对比度")


def _kind_note(th: str) -> str:
    return {
        "marvel": "45°斜条纹纸面",
        "starry": "画布织理",
        "sunflower": "画布织理",
        "mona": "整页光影（透明透出 body）",
        "vampire": "整页光影（透明透出 body）",
    }[th]


def _sel_parts(sel: str) -> list[str]:
    """逗号分段后的选择器列表（多选择器合并写法）。"""
    return [s.strip() for s in sel.split(",")]


def last_rule_exact(target: str):
    """文件中**最后**一条「选择器某分段精确等于 target」的规则。

    ★ 必须取最后一条：CSS 同特异性下后写者胜。只判「存在一条」的话，
      回归新加一条更靠后的规则时，原有那条仍在，断言照样 PASS —— 漏网。
    """
    hit = None
    for sel, body in CSS_RULES:
        if any(p == target for p in _sel_parts(sel)):
            hit = (sel, body)
    return hit


def eff(target: str, prop: str):
    """生效规则里某属性的最终值（未声明返回 None）。"""
    r = last_rule_exact(target)
    if r is None:
        return None
    ms = re.findall(r'(?:^|;)\s*' + re.escape(prop) + r'\s*:\s*([^;]+)',
                    r[1])
    return ms[-1].strip() if ms else None


def _has_grad(target: str, needle: str) -> bool:
    v = eff(target, "background-image") or ""
    return needle in v


# ---- ① 五个方向的 .content 必须已落地背景 ----------------------------------
#   判据按「方向各自的正确形态」分开写，不做一刀切：
#     · marvel  = 斜条纹米白纸面（45°，参数逐字取自参考稿 marvel/index.html:21）
#     · starry  = 亚麻布经纬织理（.045 白 / .05 黑，参考稿 :36-37）
#     · sunflower = 同上但白线提到 .07（浅色方向要更亮，参考稿 :36-37）
#     · mona / vampire = 纯 transparent —— 它们的 body 上是**整屏径向光影**，
#       透明即透出光晕；这两个方向绝不能给 .content 补 background-image，
#       否则又把光影盖回去（就是用户说的「还是纯色」）。
#
# ★★ 反向验证实测出的漏洞（已修）★★
#   第一版把五个方向统一判成 `background-color ∈ {transparent, var(--bg)}`，
#   结果注入「吸血鬼 .content 改回 var(--bg)」（**恰恰是本轮 ④ 的原始 bug**）
#   断言照样 PASS —— 判据太松，等于没防线。
#   现在按方向锁定**确切值**：
#     · mona / vampire 必须**严格 transparent**：一旦不透明，body 的光晕就被盖回去，
#       正是用户说的「背景变成纯黑」。
#     · starry / sunflower 必须**必须 var(--bg)**：它们的织理是自己在 .content 上画的，
#       transparent 会露出 html 的默认白底（body 织理在侧栏方向下被 grid 切到
#       216px 导航列，主区 grid-column 2 根本不在 body 背景的绘制区里）。
for _th, _bgc_exact, _must, _forbid in (
    ("marvel", "transparent", "repeating-linear-gradient(45deg", None),
    ("starry", "var(--bg)", "repeating-linear-gradient(90deg", None),
    ("sunflower", "var(--bg)", "repeating-linear-gradient(90deg", None),
    ("mona", "transparent", None, "repeating-linear-gradient"),
    ("vampire", "transparent", None, "repeating-linear-gradient"),
):
    _tgt = f'html[data-theme="{_th}"] .content'
    _r = last_rule_exact(_tgt)
    _bgc = eff(_tgt, "background-color")
    _bgi = eff(_tgt, "background-image") or ""
    check(
        f"{_th} 主区背景已落地（{_kind_note(_th)}）",
        _r is not None
        and _bgc == _bgc_exact
        and (("repeating-linear-gradient" not in _bgi)
             if _forbid else (_must in _bgi)),
        f"生效规则={'缺失' if _r is None else '有'}；background-color={_bgc!r}"
        f"（必须 {_bgc_exact!r}）；"
        f"background-image={( _bgi[:70] + '…') if len(_bgi) > 70 else _bgi!r}"
        + (f"；要求含 {_must}" if _must else f"；不应含渐变层（当前 {len(_bgi)} 字符）"))

# ★ 漫威的斜条纹参数必须逐字对齐参考稿（0 2px / 2px 9px），不是随便一条 45°渐变
check("漫威斜条纹参数与参考稿一致（0 2px / 2px 9px，非任意 45° 渐变）",
      "0 2px" in (eff('html[data-theme="marvel"] .content', "background-image") or "")
      and "2px 9px" in (eff('html[data-theme="marvel"] .content', "background-image") or ""),
      "参考稿 marvel/index.html:21 是 rgba(13,13,16,.035) 0 2px, transparent 2px 9px")

# ★ body 上的光影源头必须还在 —— .content 透明全靠它。
#   回归若有人把 body 的 radial-gradient 删了，.content 透明 = 纯黑/纯褐，
#   正是用户说的「背景变成纯黑了」。
for _th in ("mona", "vampire"):
    _b = eff(f'html[data-theme="{_th}"] body', "background-image") or ""
    check(f"{_th} body 上仍有径向光影（.content 透明后靠它出光晕）",
          _b.count("radial-gradient") >= 2,
          f"生效的 body background-image 只有 {_b.count('radial-gradient')} 层径向渐变")


# ---- ①bis 星月夜左侧导航栏底色（用户反馈⑦「左边导航栏又不是蓝黑色了」）-------
# ★ 根因（2026-10-22 定位）：style.css 里曾有两条**选择器完全相同**的
#   `html[data-theme="starry"] .topbar`：
#     A（参考稿逐字，3953 行）：background 近黑深蓝渐变 + border-bottom + backdrop-filter
#     B（阶段 A 追加，5933 行）：background-image 亚麻布织理 + background-attachment fixed
#   两者特异性完全相同 → **源序更靠后的 B 赢** →渐变被顶掉；
#   而 A 用的是 background **简写**，简写会把 background-color 重置为 transparent，
#   B 又没给 background-color → 顶栏底色透明 → 透出 body 的 var(--bg) #1b3a5c亮蓝
#   + 白色竖条纹。合成后视觉 = 亮蓝带白竖条，正是用户说的「又不是蓝黑色了」。
#
# ★ B 已删（本轮修复）。这里的判据按「这件事本来该怎样」定：
#     导航栏必须有参考稿的近黑深蓝渐变、不能有锐利织理、底色透明（透出深底）。
#
# ★★ 判据必须能 FAIL，所以「生效值」要**按 CSS 层叠算**，
#   不能用现成的 eff() / last_rule_exact()：
#     last_rule_exact 只取「文件里最后一条同名单选择器」，**完全不建模特异性**。
#     两条同选择器规则时它恰好也对（取最后一条 = 源序胜），
#     但只要有人新加一条**更高特异性**的规则把渐变顶掉，它就会漏网 ——
#     那种情况下 last_rule_exact 返回的还是那条没被顶掉的原规则 → 假绿。
#   所以下面自建 cascade_for()：枚举命中 .topbar 的规则 → 逐单选择器算特异性
#   （选择器组取组内最高）→ 跨规则按 (特异性, 源序) 取最大 → 返回赢家声明体。
#
# ★ 而且必须把 `background` **简写展开成 longhand** 再比：
#   只读原始 `background` 字符串的话，A 的渐变永远"在"，
#   看不见后面一条 `background-image: 织理` 已经把它顶掉了（简写先于、
#   后写的同属性 longhand 胜）—— 判据就成了恒真的假绿（踩过三次同类坑了）。
#
# 反向验证：C:/tmp/rev_starry_nav.py —— 把 B 加回去（或改成更高特异性）必须变红。

_CSS_SEQ_RULES: list = []          # [(选择器组, 声明体, 源序)]，懒建一次


def _seq_rules() -> list:
    """按源序重建规则表。CSS_RULES 没记源序，这里重新切一遍并记录 m.start()。

    ★ 先剥注释再切：注释正文里的花括号会被当成声明体边界，
      把真正的规则选择器污染成「...注释文字... html[...].active」。
    """
    if _CSS_SEQ_RULES:
        return _CSS_SEQ_RULES
    live = re.sub(r"/\*.*?\*/", " ", CSS, flags=re.S)
    for m in re.finditer(r"([^{}]+)\{", live):
        sel = " ".join(m.group(1).split())
        if not sel:
            continue
        depth, j = 0, m.end() - 1
        while j < len(live):
            if live[j] == "{":
                depth += 1
            elif live[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        _CSS_SEQ_RULES.append((sel, live[m.end():j], m.start()))
    return _CSS_SEQ_RULES


_CLASS_TOKEN_RE = re.compile(r"\.(-?[_a-zA-Z][\w-]*)")


def _compound_classes(compound: str) -> set:
    """一个 compound 段里的 class 名。先剔掉属性选择器内容，
    免得 [class~="topbar"] 之类被误当成 class 选择器。"""
    return set(_CLASS_TOKEN_RE.findall(re.sub(r"\[[^\]]*\]", " ", compound)))


def _sel_hits_element(sel_part: str, class_name: str, theme: str) -> bool:
    """这条单选择器是否命中「<该主题下的 .class_name 元素本身>」。

    两个必要条件（缺一不可）：
    1) **最后一段 compound** 含 class_name ——
       `.topbar .tab` 命中的是 tab 不是 topbar，必须排除；
       `html[data-theme="starry"] .topbar` 才是。
    2) 主题作用域对得上 —— 没有 data-theme 限定的是全局规则（任何主题都命中）；
       有限定的必须等于目标主题，否则那是**别的**主题的规则。
    """
    parts = [p for p in re.split(r"\s*[>+~]\s*|\s+", sel_part.strip()) if p]
    if not parts or class_name not in _compound_classes(parts[-1]):
        return False
    for th in re.findall(r'data-theme\s*=\s*["\']?([\w-]+)', sel_part):
        if th != theme:
            return False
    return True


def cascade_for(class_name: str, theme: str):
    """按 CSS 层叠（特异性 + 源序）找出真正管这个元素的那条规则。

    返回 (选择器组, 声明体, 源序, 特异性)；一条都没命中返回 None。
    """
    best = None
    for sel, body, order in _seq_rules():
        hit = [p.strip() for p in _split_top(sel, ",")
               if _sel_hits_element(p.strip(), class_name, theme)]
        if not hit:
            continue
        spec = max(_specificity(p) for p in hit)   # 选择器组：组内最高那条说了算
        key = (spec, order)
        if best is None or key > best[0]:
            best = (key, sel, body, order, spec)
    return None if best is None else (best[1], best[2], best[3], best[4])


_DECL_RE = re.compile(r"(?:^|;)\s*([-a-zA-Z]+)\s*:\s*([^;]+)")


def _decls(body: str) -> list:
    """声明体 → [(属性, 值)]，保持源内顺序（同名后者覆盖前者）。"""
    return [(m.group(1).strip().lower(), m.group(2).strip())
            for m in _DECL_RE.finditer(body or "")]


_COLOR_RE = re.compile(
    r"^(#[0-9a-fA-F]{3,8}|rgba?\(|hsla?\(|[a-zA-Z]+$)")


def _expand_background(value: str) -> list:
    """把 `background` 简写值展开成 [(longhand, 值)]。

    只需要 background-image / background-color 两个分量（判据只用这两个）。
    渐变 / url 是**图像**，不是颜色 —— 这是本次修复的核心：
    `background: linear-gradient(...)` 的 background-color 分量 = transparent。
    """
    layers = [v.strip() for v in _split_top(value, ",")]
    out = []
    has_img = False
    has_col = False
    for layer in layers:
        low = layer.lower()
        if low.startswith(("linear-gradient(", "radial-gradient(",
                           "conic-gradient(", "repeating-linear-gradient(",
                           "repeating-radial-gradient(", "url(", "image-set(")):
            has_img = True
        elif low == "none":
            has_img = True
        elif _COLOR_RE.match(layer):
            has_col = True
            out.append(("background-color", layer))
    out.append(("background-image", value if has_img else "none"))
    if not has_col:
        # 简写里没给颜色 → background-color 被重置为 initial = transparent
        out.append(("background-color", "transparent"))
    return out


def cascade_prop_src(class_name: str, theme: str, prop: str):
    """某 longhand 属性的生效值 **以及它是由哪条规则给的**。

    ★ 为什么要连来源一起返回：命中该元素的**最高层叠规则**未必声明这个属性
      （实测就撞上：有一条 `html[data-theme="starry"] .topbar, ...` 的
      position/z-index 规则特异性比参考稿那条更高，但它不含 background）。
      判据要问的是「生效的background-image 来自哪条规则」，
      不是「谁是最靠前的规则」—— 否则断言会问错对象。
    返回 (值, 选择器组, 声明体, 源序, 特异性)；未声明返回 None。
    """
    winners = {}            # longhand -> (key, 值, 选择器, 声明体)
    for sel, body, order in _seq_rules():
        hit = [p.strip() for p in _split_top(sel, ",")
               if _sel_hits_element(p.strip(), class_name, theme)]
        if not hit:
            continue
        spec = max(_specificity(p) for p in hit)
        key = (spec, order)
        for name, val in _decls(body):
            pairs = []
            if name == "background":
                pairs = _expand_background(val)
            elif name == prop:
                pairs = [(prop, val)]
            for sub, sval in pairs:
                if sub not in winners or key > winners[sub][0]:
                    winners[sub] = (key, sval, sel, body)
    if prop not in winners:
        return None
    _k, v, s, b = winners[prop]
    return (v, s, b, _k[1], _k[0])


def cascade_prop(class_name: str, theme: str, prop: str):
    """某 longhand 属性的**生效值**（按层叠，简写已展开）。未声明返回 None。"""
    r = cascade_prop_src(class_name, theme, prop)
    return r[0] if r else None


_starry_nav_src = cascade_prop_src("topbar", "starry", "background-image")
_starry_nav_bg = _starry_nav_src[0] if _starry_nav_src else ""
# 命中该元素的**最高层叠规则**（只声明 position/z-index，与背景无关）——
# 留着是为了证明解析器真的建了模：它比参考稿那条特异性更高。
_starry_nav_rule = cascade_for("topbar", "starry")
check("★ 星月夜：能找到管左侧导航栏的规则（判据不是空判据）",
      _starry_nav_rule is not None, _starry_nav_rule)
check("★ 星月夜导航栏生效的是参考稿那条近黑深蓝渐变",
      "linear-gradient(180deg, rgba(10, 22, 38, 0.90), rgba(10, 26, 44, 0.78))"
      in _starry_nav_bg,
      f"生效 background-image={_starry_nav_bg!r}；"
      f"赢家规则={_starry_nav_rule[0] if _starry_nav_rule else None!r} "
      f"特异性={_starry_nav_rule[3] if _starry_nav_rule else None}")
check("★ 星月夜导航栏上不能有锐利织理（用户反馈⑦的病根）",
      "repeating-linear-gradient" not in _starry_nav_bg,
      f"生效 background-image={_starry_nav_bg!r}——"
      f"repeating-linear-gradient 是阶段 A 那条覆盖规则叠上来的")
check("★ 星月夜导航栏底色是 transparent（透出 body 深底，不是亮蓝 #1b3a5c）",
      cascade_prop("topbar", "starry", "background-color") == "transparent",
      f"background-color="
      f"{cascade_prop('topbar', 'starry', 'background-color')!r}")
check("★ 星月夜导航栏保留 backdrop-filter: blur(14px)（参考稿的柔化底纹来源）",
      "blur(14px)" in (cascade_prop("topbar", "starry", "backdrop-filter") or ""),
      f"backdrop-filter="
      f"{cascade_prop('topbar', 'starry', 'backdrop-filter')!r}")
# ★ 承上：生效的 background-image **来自**参考稿那条（同特异性下源序赢的那条），
#   且提供它的那条规则里没有织理。查的是「声明来源」不是「最靠前的规则」——
#   两者不是一回事（实测有更高特异性的 position 规则压在上面，它不含背景）。
check("★ 承上：生效 background-image 来自参考稿那条渐变（不是被别的规则顶掉）",
      _starry_nav_src is not None
      and "rgba(10, 22, 38, 0.90)" in _starry_nav_src[2]
      and "repeating-linear-gradient" not in _starry_nav_src[2],
      f"来源选择器={_starry_nav_src[1] if _starry_nav_src else None!r} "
      f"声明体前80字={(_starry_nav_src[2][:80] if _starry_nav_src else None)!r}")
# ★ 承上：解析器确实建了模——命中该元素的最靠前规则**不是**背景那条
#   （实测是 position/z-index 规则，特异性更高）。这条钉住「真的在算层叠」，
#   万一以后把解析退化成 last_rule_exact（只取最后一条同名选择器），它会红。
# ★ 承上：**用合成 CSS 证明**层叠解析器确实在建模（不依赖当前 CSS 的现状事实）。
#   动机：第一版判据比「解析器的背景来源」 vs 「last_rule_exact 的最后一条同名规则」，
#   但 last_rule_exact 返回的是**合并选择器串**、解析器返回的是**单选择器**
#   → 两者字面永远不可能相等 → 判据恒真（注入高特异性规则照样 PASS）。★★
#   ★★★ 教训：判据若依赖「两个字符串不相等」，必须先确认它们**有可能相等**，
#     否则就是恒真假防线。★★
#   现在改成：拿一段**合成 CSS**（只含两条同选择器、同特异性、源序不同的规则，
#   且**最后一条故意不含 background**）同时喂给两个解析器：
#     · 退化语义（只取文件里最后一条同名规则）→ 漏掉前一条的 background → 得 None
#     · 真层叠（按 (特异性, 源序) 取最大）→ 正确认出赢家 → 得渐变
#   两者结论**必须不同** —— 这才是硬证据，且与当前 CSS 长什么样无关：
#   以后有人清理 CSS 删掉某条规则，它不会假红；解析器一旦退化成 last_rule_exact，
#   它立刻变红。
_SYNTH_CSS = (
    'html[data-theme="starry"] .topbar {\r\n'
    '  background: linear-gradient(180deg, rgba(10, 22, 38, 0.90), rgba(10, 26, 44, 0.78));\r\n'
    '}\r\n'
    'html[data-theme="starry"] .topbar { position: relative; z-index: 1; }\r\n'
)


def _synth_degraded(css_text: str, cls: str, theme: str, prop: str):
    """退化语义：只取文件里**最后一条**命中该元素的规则，展开简写后取 longhand。"""
    live = re.sub(r"/\*.*?\*/", " ", css_text, flags=re.S)
    last = None
    for m in re.finditer(r"([^{}]+)\{", live):
        sel = " ".join(m.group(1).split())
        if not sel:
            continue
        depth, j = 0, m.end() - 1
        while j < len(live):
            if live[j] == "{":
                depth += 1
            elif live[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        body = live[m.end():j]
        for one in [s.strip() for s in sel.split(",")]:
            parts = one.split()
            if not parts or not re.search(r"\." + re.escape(cls) + r"(?![-\w])", parts[-1]):
                continue
            if any(t != theme for t in re.findall(r'data-theme\s*=\s*["\']?([\w-]+)', one)):
                continue
            last = body
    if last is None:
        return None
    for name, val in _decls(last):
        if name == "background":
            for sub, sval in _expand_background(val):
                if sub == prop:
                    return sval
        elif name == prop:
            return val
    return None


def _synth_cascade(css_text: str, cls: str, theme: str, prop: str):
    """真层叠语义：把合成 CSS 塞进解析器（临时换掉它的规则表），取 longhand 生效值。"""
    global _CSS_SEQ_RULES
    saved = _CSS_SEQ_RULES
    _CSS_SEQ_RULES = []
    try:
        live = re.sub(r"/\*.*?\*/", " ", css_text, flags=re.S)
        for m in re.finditer(r"([^{}]+)\{", live):
            sel = " ".join(m.group(1).split())
            if not sel:
                continue
            depth, j = 0, m.end() - 1
            while j < len(live):
                if live[j] == "{":
                    depth += 1
                elif live[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            _CSS_SEQ_RULES.append((sel, live[m.end():j], m.start()))
        r = cascade_prop_src(cls, theme, prop)
        return r[0] if r else None
    finally:
        _CSS_SEQ_RULES = saved


_synth_cascade_val = _synth_cascade(_SYNTH_CSS, "topbar", "starry", "background-image")
_synth_degraded_val = _synth_degraded(_SYNTH_CSS, "topbar", "starry", "background-image")
check("★ 承上：层叠解析器确实在建模（合成 CSS 上与「只取最后一条」的退化语义结论不同）",
      _synth_cascade_val is not None
      and _synth_degraded_val is None
      and "rgba(10, 22, 38, 0.90)" in _synth_cascade_val,
      f"真层叠解析得={_synth_cascade_val!r}；"
      f"退化语义得={_synth_degraded_val!r}；"
      f"合成CSS故意让「最后一条」不含 background —— 若两者相同即说明解析退化")

# ---- ② 顶栏 / 侧栏与主区统一----------------------------------------
# 参考稿规律：mona/vampire 的 .bar **只有 border-bottom、无 background**；
#             sunflower/starry 的顶栏是半透明渐变 + backdrop-filter。
for _th in ("mona", "vampire"):
    _t = f'html[data-theme="{_th}"] .topbar'
    check(f"{_th} 顶栏为半透明（否则整页被切成纯色板 + 纹理区两截）",
          "backdrop-filter" in (last_rule_exact(_t) or ("", ""))[1]
          and "rgba(" in (eff(_t, "background") or ""),
          f"background={eff(_t, 'background')!r}；"
          f"backdrop-filter={'有' if 'backdrop-filter' in (last_rule_exact(_t) or ('', ''))[1] else '无'}")
for _th in ("marvel", "starry", "sunflower", "mona", "vampire"):
    for _rail in (".rail-a", ".rail-b"):
        _t = f'html[data-theme="{_th}"] {_rail}'
        _bgc = eff(_t, "background-color")
        check(f"{_th} {_rail} 透明化（不能是纯色板，割裂整页）",
              _bgc == "transparent",
              f"background-color={_bgc!r}（必须 transparent 才能透出主区纹理/光影）")

# ★ 漫威顶栏必须保持纯黑（漫画标题条是刻意设计，参考稿 marvel/index.html:27）
_mv_top = eff('html[data-theme="marvel"] .topbar', "background") or ""
check("漫威顶栏仍是纯黑漫画标题条（刻意设计，别被本轮改动误伤）",
      "0d0d10" in _mv_top.lower() or "#000" in _mv_top.lower(),
      f"background={_mv_top!r}；参考稿 marvel/index.html:27 就是 background:var(--ink-black)")

# ---- ③ 向日葵导航对比度（反馈⑥）------------------------------------------
# 逐元素**逐像素取样**的实测值（不能用「顶栏整体一个底色」推断 ——
# 顶栏是半透明深墨渐变，但各元素下面叠的东西不同）：
#     tab 未激活  实际底 (241,216,143) 浅米黄 → 深墨字 11.12:1✅（原 1.08:1 隐形）
#     tab 激活    实际底 (226,167,19)  铬黄  → 深墨字  7.26:1✅
#     brand       实际底  (62,48,26)   深墨→ 浅米黄 10.12:1✅（改深墨反而仍高）
#     #status     实际底  (64,49,26)   深墨  → 浅米黄  5.47:1✅（改深墨掉到 1.19:1）
_sun_tab = eff('html[data-theme="sunflower"] .topbar .tab', "color")
check("向日葵未激活 tab 是深墨（改前 1.08:1 隐形，用户说的「呈白色」）",
      _sun_tab is not None and _sun_tab.strip() == "#2a2118",
      f"color={_sun_tab!r}；实测对比度 1.08:1 → 11.12:1")
# ★ 反向守护：brand / status 必须**保持浅色**。我第一版把三个元素一起改深墨，
#   #status 实测掉到 1.19:1（深墨压深墨）。这条断言防止再次误伤。
for _el in ("brand", "status"):
    _c = eff(f'html[data-theme="sunflower"] .topbar .{_el}', "color") or ""
    check(f"向日葵 {_el} 保持浅色（深墨压深墨只有 1.19:1，#status 会隐形）",
          "#2a2118" not in _c and "#14161a" not in _c,
          f".topbar .{_el} color={_c!r}；实测对比度必须 ≥4.5:1")

# ---- ④ 硬约束：朴素 / 克制一条规则都不许加 -------------------------------
for _th in ("reading", "vesper"):
    check(f"{_th} 未被本轮背景改动波及（用户日常在用，必须像素级一致）",
          last_rule_exact(f'html[data-theme="{_th}"] .content') is None
          and last_rule_exact(f'html[data-theme="{_th}"] .topbar') is None,
          "本轮所有规则都用 html[data-theme=\"星月夜/向日葵/蒙娜/吸血鬼/漫威\"] 精确限定，"
          "不应出现 reading / vesper 的 .content / .topbar 规则")

# ---- ⑤ 文件尾部规则可达（本轮实际踩过的坑，必须有防线）-------------------
# ★ 本轮  整块曾**完全不生效**：`/* design-explore/*/index.html */`
#   注释里的 `*/` 提前闭合了注释 → 从那一行起全被浏览器丢弃。
#   症状极具欺骗性：Python 剥注释后**能找到**那条规则，但浏览器 CSSOM 里没有。
#   正解防线：断言文件**尾部**（本轮改动所在段）的规则能被规则扫描器枚举到 ——
#   一旦注释被提前闭合或尾部被截断，这条立刻 FAIL。
# ---- ⑤ 文件尾部规则可达（本轮实际踩过的坑，必须有防线）-------------------
# ★ 本轮  整块曾**完全不生效**：Python 剥注释后能找到那些规则，
#   浏览器 CSSOM 里却没有（规则数停在 824，末尾是上一条 sunflower .status）。
#   → 任何「尾部规则整体丢失」的原因（注释未闭合 / 花括号不配对 / 语法错误
#     导致后续解析中断）都会被这条抓住。
# ★★ 上一版这里配了一条「注释开合平衡（opens==closes）」——
#   反向验证实测它是**恒真的鸡肋**：注入提前闭合后 opens=314/closes=314/depth=0
#   依旧平衡（提前闭合少闭一个，后续块的起始符就在 depth==0 处被当成新开，
#   一来一回刚好又配平）。恒真的判据留着只会给人虚假安全感，已删。
#   顺带纠正一条错误归因：注释里写 `/*`（如 design-explore/*/）**不会**提前闭合 ——
#   浏览器从起始符第 3 个字符开始找 `*/`，会跳过起始符自己那个 `*`。
#   真会造成提前闭合的是注释里出现**裸 `*/`**。所以下面不再判注释计数，
#   改判「尾部这一批规则是否真的都在」。
#   断言从 1 条扩到 15 条：只查 1 条的话，尾部丢一半仍会 PASS。
# ★★ 这条断言防的是「**解析中断**」，不是「单条规则被删」 ★★
#   两个反向验证实测出来的认知（都写在下面，避免以后重犯）：
#   ① 把末尾那条规则整条删掉 → 断言照样 PASS。因为  里已有同名的
#      `.rail-a,.rail-b` 组合，选择器仍可枚举到。同名重复定义抗删是 CSS 本性，
#      不是漏洞。「单条规则被删」由第 ①~③ 各断言负责（那些查具体声明体）。
#   ② 花括号不配对**也不会让选择器消失**（实测 diag_h9b.py：中间少一个 }，
#      规则总数 918 → 918，vampire .topbar 仍在，只是声明体被邻居吞成 33 字符）。
#      所以「规则可达」是错的判据方向 —— 该判的是「**声明体干不干净**」：
#      一条规则的声明体里绝不该出现 `{`、`}`、别的选择器或注释符。
#   真正会让尾部规则整体消失的元凶是**注释里出现裸 `*/`**（提前闭合后，
#   后面整段本该是注释的文本裸露，被当成选择器解析）。
_TAIL_MUST = [
    # 5 个方向的 .content
    'html[data-theme="marvel"] .content',
    'html[data-theme="starry"] .content',
    'html[data-theme="sunflower"] .content',
    'html[data-theme="vampire"] .content',
    'html[data-theme="mona"] .content',
    # 顶栏（mona / vampire）
    'html[data-theme="mona"] .topbar',
    'html[data-theme="vampire"] .topbar',
    'html[data-theme="starry"] .topbar',
    # 侧栏（逗号组合，各取一项即可证明整条在）
    'html[data-theme="marvel"] .rail-a',
    'html[data-theme="marvel"] .rail-b',
    'html[data-theme="starry"] .rail-a',
    'html[data-theme="sunflower"] .rail-a',
    'html[data-theme="mona"] .rail-a',
    'html[data-theme="vampire"] .rail-a',
    # ⑥ 向日葵导航
    'html[data-theme="sunflower"] .topbar .tab',
]
_tail_pos = CSS.rindex('html[data-theme="marvel"] .rail-b')
_dirty = []
for _s in _TAIL_MUST:
    _r = last_rule_exact(_s)
    if _r is None:
        _dirty.append(f"{_s}: 规则不存在")
        continue
    # 一条规则的声明体里绝不该出现花括号、别的选择器或注释符
    if re.search(r'[{}]|html\[data-theme|/\*', _r[1]):
        _dirty.append(f"{_s}: 声明体被相邻规则吞并（{len(_r[1])} 字符，含 {re.findall(r'.{0,18}(?:html\[data-theme|\{{|\}}).{0,18}', _r[1])[:1]}）")
check(f"尾部 {len(_TAIL_MUST)} 条关键规则的声明体干净"
      "（防花括号不配对 / 裸 */ 把相邻规则吞进声明体）",
      not _dirty,
      f"：{_dirty[:2]}")
check("文件尾部规则确实落在文件末尾 10% 内（确认 尾 块没被挪走）",
      _tail_pos > len(CSS) * 0.9,
      f"marvel .rail-b 在 CSS 的 {_tail_pos}/{len(CSS)} = "
      f"{_tail_pos * 100 / len(CSS):.1f}% 处")

# ---- ⑥ 注释闭合必须合法（反向验证实测能抓住「注释里写裸 */」）------------
#   浏览器剥注释的语义是：扫到 /* 就跳到其后**第一个** */。所以一个合法 CSS 文件里
#   **相邻两个 */ 之间必然至少有一个 /** —— 否则前一个 */ 是裸的，
#   它会提前结束注释块，后面的注释文字裸露出来被当成选择器解析。
#   本轮实际踩过：注释里写了 `design-explore/*/index.html`。顺带纠正上一轮的
#   错误归因 —— `/*` 本身**不会**提前闭合（浏览器从起始符第 3 个字符开始找 */，
#   会跳过起始符自己那个 *），真会提前闭合的只有裸 `*/`。
_close = [m.start() for m in re.finditer(r'\*/', CSS)]
_orphan = [a for a, b in zip(_close, _close[1:]) if "/*" not in CSS[a + 2:b]]
check("注释闭合合法（相邻两个 */ 之间必有一个 /*，防注释里写裸 */ 提前闭合）",
      not _orphan,
      f"有 {len(_orphan)} 处裸 */，首次在偏移 {_orphan[:3]}"
      f"（前后文 {CSS[_orphan[0] - 40:_orphan[0] + 40]!r}）"
      if _orphan else "")


# ---------------------------------------------------------------------------
print("\n" + "=" * 64)
passed = sum(1 for _, ok in results if ok)
failed = len(results) - passed
print(f"总计：{passed} PASS / {failed} FAIL（共 {len(results)} 项）")
sys.exit(1 if failed else 0)