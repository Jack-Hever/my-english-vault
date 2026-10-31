# -*- coding: utf-8 -*-
"""界面主题切换：令牌完整性 + settings 读写 + 非法值回落 + 硬编码审计。

★ 为什么这个套件单独存在 ★
主题系统最隐蔽的失败不是「主题没生效」，而是这两类：
1) **漏换干净** —— 某个主题块少定义一个变量，于是那一处继续用上一个主题的值，
   肉眼扫一遍界面很难发现，直到用户投诉「这个主题有个地方颜色不对」。
2) **settings.json 被手改后崩掉** —— 主题 id 存在用户可编辑的 JSON 里，
   写成 "../evil" 或超长字符串时，load_settings 绝不能抛异常，
   否则整个应用读不出设置（连AI 配置一起废掉）。
所以这里把两条都变成可重复跑的断言，而不是靠人眼对账。

★ 为什么令牌扫描用正则而不是解析 CSS ★
不引第三方 CSS 解析库（项目是纯原生 + 离线）。这里只需要
「html[data-theme="x"]{ ... }」这一种结构，正则足够；
但正则必须覆盖**三种值形态**：hex（--accent:#1d6b51）、
rgba（--accent-ring:rgba(29,107,81,.26)）、px（--r-card:12px）。
第一版只写 #[0-9a-fA-F]{6}，于是把 rgba 和 px 全判成「缺失」，
一次报了 7×N 个假 FAIL。教训：校验用集合运算，别手打只覆盖一种形态的关键词。
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

# ★ 必须在 import app.config 之前设好，否则会读写用户真实设置文件
_TMP = tempfile.mkdtemp(prefix="enp_theme_")
os.environ["ENPRACTICE_HOME"] = _TMP

from app import config  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if not ok else ""))


def section(t):
    print(f"\n[{t}]")


THEME_IDS = ("reading", "starry", "sunflower", "vesper", "mona", "vampire", "marvel", "silver")

#: 每个主题块必须自带的令牌。少一个就会「漏换干净」。
#: 注意这里**不含** --font-ui/--font-read/--dur/--ease —— 它们是全局的，
#: 所有主题共用同一套字体与动效，本就不该每个块重复定义。
REQUIRED_TOKENS = (
    "--bg", "--panel", "--panel-soft", "--border", "--border-strong",
    "--text", "--text-soft", "--muted",
    "--accent", "--accent-deep", "--accent-soft", "--accent-border", "--accent-ring",
    "--accent-on",
    "--warn", "--warn-bg", "--warn-border",
    "--danger", "--danger-bg", "--danger-border",
    "--shadow-sm", "--shadow-md", "--shadow-lg",
    "--r-card", "--r-ctl", "--r-pill",
)


def read_css() -> str:
    return (BASE / "web" / "style.css").read_text(encoding="utf-8")


def strip_css_comments(css: str) -> str:
    """剥掉 CSS 注释，只留真正会生效的声明。

    ★ 为什么必须剥 ★
    本文件的主题块里大量引用了**候选原型原文**作为对照注释，例如
    「候选 .s-big 是 color:var(--blood-lit)」「var(--night);」。
    这些 var() 只是注释里的说明文字，浏览器根本不会解析。
    而第 9 节的死变量审计是 `re.findall(r'var\\((--[\\w-]+)', css)` 直接扫全文，
    不剥注释就会把注释里的 --night / --blood-lit 判成「引用了但没定义」——
    假 FAIL，且会让人去 CSS 里找根本不存在的样式 bug（真实样式没问题）。
    """
    return re.sub(r"/\*.*?\*/", " ", css, flags=re.S)


def parse_theme_blocks(css: str) -> dict[str, dict[str, str]]:
    """抽出每个 html[data-theme="x"] 块的令牌表。

    正则分两层：
      块：html[data-theme="x"] { ... }（非贪婪，块内无嵌套花括号）
      令牌：--k: v;  —— 值不限形态，交给下一层正则判定非空
    """
    blocks: dict[str, dict[str, str]] = {}
    for m in re.finditer(r'html\[data-theme="(\w+)"\]\s*\{([^{}]*)\}', css):
        tid, body = m.group(1), m.group(2)
        kv = {}
        for k in re.finditer(r'(--[\w-]+)\s*:\s*([^;]+);', body):
            kv[k.group(1)] = k.group(2).strip()
        # 同一主题可能出现多次（如 starry 有主体块 + 星级色覆盖块），合并
        blocks.setdefault(tid, {}).update(kv)
    return blocks


# ---------------------------------------------------------------------------
section("1. 7 个主题块齐全，且每个块的必需令牌一个不少")
# ---------------------------------------------------------------------------
css = read_css()
#: 审计用：剥掉注释的版本。第 9 节的死变量扫描必须用它，否则注释里
#: 引用的候选原型色板（--night / --blood-lit 等）会被误判成死变量。
css_live = strip_css_comments(css)
blocks = parse_theme_blocks(css)

for tid in THEME_IDS:
    check(f"存在主题块 html[data-theme=\"{tid}\"]", tid in blocks,
          f"只找到: {sorted(blocks)}")

for tid in THEME_IDS:
    kv = blocks.get(tid, {})
    missing = [k for k in REQUIRED_TOKENS if k not in kv]
    check(f"{tid} 必需令牌齐全（{len(REQUIRED_TOKENS)} 项）", not missing,
          "缺: " + ", ".join(missing))

    # 令牌值必须真的非空 —— 「定义了但值是空」和没定义一样会漏换
    empty = [k for k in REQUIRED_TOKENS if k in kv and not kv[k].strip()]
    check(f"{tid} 没有空值令牌", not empty, "空: " + ", ".join(empty))


# ---------------------------------------------------------------------------
section("2. 令牌值形态合法（hex / rgba / px 三种都要认，别只认一种）")
# ---------------------------------------------------------------------------
VALUE_RE = re.compile(
    r'^(#[0-9a-fA-F]{3,8}'          # hex：#fff / #1d6b51
    r'|rgba?\(\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*(?:,\s*[\d.]+\s*)?\)'  # rgba(...)
    r'|-?[\d.]+px'                  # px：12px
    r'|0)$'
)
#: 纯色令牌（不含阴影/圆角）。阴影是多层复合值
#: （`0 1px 2px rgba(...)`），单独用单值正则校验必然误报，故不纳入。
COLOR_TOKENS = REQUIRED_TOKENS[:20]   # --bg .. --danger-border
for tid in THEME_IDS:
    kv = blocks.get(tid, {})
    bad = []
    for k in COLOR_TOKENS:
        v = kv.get(k, "")
        if v and not VALUE_RE.match(v):
            bad.append(f"{k}={v}")
    check(f"{tid} 颜色令牌值形态合法", not bad, "; ".join(bad[:3]))

for tid in THEME_IDS:
    kv = blocks.get(tid, {})
    bad = [f"{k}={kv.get(k)}" for k in ("--r-card", "--r-ctl", "--r-pill")
           if not re.match(r'^\d+px$', kv.get(k, ""))]
    check(f"{tid} 圆角令牌是 px 值", not bad, "; ".join(bad))

#: --accent-ring 必须是 rgba(...)：它是半透明焦点环，用 hex 表达不了透明度。
#: 这一条单独断言是因为它最容易被「顺手改成 hex」而静默坏掉。
for tid in THEME_IDS:
    ring = blocks.get(tid, {}).get("--accent-ring", "")
    check(f"{tid} --accent-ring 是 rgba 半透明值", ring.startswith("rgba("), ring)


# ---------------------------------------------------------------------------
section("3. reading 必须与 :root 逐值一致（默认外观不能被主题系统改动）")
# ---------------------------------------------------------------------------
root_body = re.search(r':root\s*\{([^{}]*)\}', css).group(1)
root_kv = {m.group(1): m.group(2).strip()
           for m in re.finditer(r'(--[\w-]+)\s*:\s*([^;]+);', root_body)}
reading_kv = blocks.get("reading", {})

drift = []
for k in REQUIRED_TOKENS:
    if k not in root_kv or k not in reading_kv:
        continue
    if root_kv[k].lower() != reading_kv[k].lower():
        drift.append(f"{k}: :root={root_kv[k]} vs reading={reading_kv[k]}")
check("reading 与 :root 的必需令牌逐值相同", not drift, "; ".join(drift))

# 切换到 reading 必须与「完全不设 data-theme」长得一样 —— 这是用户最在意的
# 「别弄坏我现在的样子」。断言方式：reading 块不能比 :root 多覆盖任何必需令牌。
extra = [k for k in reading_kv if k in root_kv
         and reading_kv[k].lower() != root_kv[k].lower()]
check("reading 未额外覆盖 :root 的任何令牌", not extra, ", ".join(extra))


# ---------------------------------------------------------------------------
section("4. 主题差异真实存在（防止 7 个块被写成同一套的明暗变体）")
# ---------------------------------------------------------------------------
for i, a in enumerate(THEME_IDS):
    for b in THEME_IDS[i + 1:]:
        ka, kb = blocks.get(a, {}), blocks.get(b, {})
        diffs = [k for k in ("--bg", "--accent", "--r-card", "--r-ctl")
                 if ka.get(k) != kb.get(k)]
        check(f"{a} vs {b} 至少差一项（底色/强调色/圆角）", bool(diffs),
              f"实得差异: {diffs or '无'} -> 只是同一套的明暗变体")

# 反向断言：圆角差异必须被保留，不能被「统一」掉。
# mona 的 --r-card:4px 与 sunflower 的 --r-ctl:999px 都是设计意图，不是笔误。
check("mona 保留直角卡片（--r-card:4px）",
      blocks.get("mona", {}).get("--r-card") == "4px",
      blocks.get("mona", {}).get("--r-card", ""))
check("sunflower 保留全圆角控件（--r-ctl:999px）",
      blocks.get("sunflower", {}).get("--r-ctl") == "999px",
      blocks.get("sunflower", {}).get("--r-ctl", ""))
check("marvel 保留硬边胶囊（--r-pill:3px）",
      blocks.get("marvel", {}).get("--r-pill") == "3px",
      blocks.get("marvel", {}).get("--r-pill", ""))


# ---------------------------------------------------------------------------
section("5. settings.json 读写：7 个合法值存得进、读得出")
# ---------------------------------------------------------------------------
check("config.THEME_IDS 与前端 7 个主题一致",
      tuple(config.THEME_IDS) == THEME_IDS,
      f"config={config.THEME_IDS}")

check("DEFAULT_SETTINGS['ui']['ui_theme'] 缺省为 reading",
      config.DEFAULT_SETTINGS["ui"].get("ui_theme") == "reading")

for tid in THEME_IDS:
    saved = config.save_settings({"ui": {"ui_theme": tid}})
    check(f"写入 ui_theme={tid} -> 读回一致", saved["ui"]["ui_theme"] == tid,
          f"实际 {saved['ui']['ui_theme']}")
    on_disk = json.loads(config.settings_path().read_text(encoding="utf-8"))
    check(f"落盘 settings.json 的 ui.ui_theme={tid}", on_disk["ui"]["ui_theme"] == tid,
          f"实际 {on_disk['ui'].get('ui_theme')}")


# ---------------------------------------------------------------------------
section("6. 非法值一律回落 reading，且绝不抛异常")
# ---------------------------------------------------------------------------
# settings.json 是用户可手改的文本文件，这里穷举手滑/手坏的典型形态。
BAD_VALUES = [
    ("空串", ""),
    ("路径穿越", "../evil"),
    ("带尾空格", "starry "),
    ("带首空格", " starry"),
    ("大小写错", "Starry"),
    ("None", None),
    ("数字", 123),
    ("布尔", True),
    ("列表", ["starry"]),
    ("字典", {"id": "starry"}),
    ("超长字符串", "x" * 5000),
    ("单引号注入", "'><img src=x>"),
    ("脚本标签", "<script>alert(1)</script>"),
    ("HTML 实体", "&quot;starry&quot;"),
]
for label, bad in BAD_VALUES:
    try:
        saved = config.save_settings({"ui": {"ui_theme": bad}})
        ok = saved["ui"]["ui_theme"] == "reading"
        detail = f"实际 {saved['ui']['ui_theme']!r}"
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"抛异常了: {exc!r}"
    check(f"写入非法主题（{label}）-> 回落 reading 且不抛异常", ok, detail)

# 读侧也要把关：绕过 save_settings，直接把坏值写进 settings.json
for label, bad in BAD_VALUES:
    p = config.settings_path()
    p.write_text(json.dumps({"ui": {"ui_theme": bad}, "ai": {"api_key": "sk-keepme"}}),
                 encoding="utf-8")
    try:
        got = config.load_settings()["ui"]["ui_theme"]
        ok = got == "reading"
        detail = f"实际 {got!r}"
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"抛异常了: {exc!r}"
    check(f"手改 settings.json 为非法值（{label}）-> 读回落 reading 不抛异常", ok, detail)


# ---------------------------------------------------------------------------
section("7. 坏主题值不牵连 settings 里的其他字段")
# ---------------------------------------------------------------------------
p = config.settings_path()
p.write_text(json.dumps({
    "ai": {"base_url": "https://api.example.com/v1", "model": "my-model",
           "api_key": "sk-secret-123", "timeout": 42},
    "review": {"time": "08:30", "mode": "date_order", "enabled": False},
    "ui": {"language": "en-US", "ui_theme": "<<evil>>"},
}, ensure_ascii=False), encoding="utf-8")

s = config.load_settings()
check("坏主题值不影响 AI 配置", s["ai"] == {
    "base_url": "https://api.example.com/v1", "model": "my-model",
    "api_key": "sk-secret-123", "timeout": 42}, str(s["ai"]))
check("坏主题值不影响复习配置", s["review"] == {
    "time": "08:30", "mode": "date_order", "enabled": False}, str(s["review"]))
check("坏主题值不影响 ui.language", s["ui"]["language"] == "en-US", s["ui"]["language"])
check("只有 ui_theme 被回落", s["ui"]["ui_theme"] == "reading", s["ui"]["ui_theme"])

# 反向：改主题不能把已存的密钥冲掉
p.write_text(json.dumps({
    "ai": {"base_url": "https://api.example.com/v1", "api_key": "sk-secret-123"},
    "ui": {"ui_theme": "reading"},
}, ensure_ascii=False), encoding="utf-8")
s2 = config.save_settings({"ui": {"ui_theme": "marvel"}})
check("切主题后 AI 密钥仍在", s2["ai"]["api_key"] == "sk-secret-123", str(s2["ai"]))
check("切主题后 AI 地址仍在", s2["ai"]["base_url"] == "https://api.example.com/v1")
check("切主题确实生效", s2["ui"]["ui_theme"] == "marvel")
on_disk = json.loads(p.read_text(encoding="utf-8"))
check("密钥真的落盘了（不是只在内存里）", on_disk["ai"]["api_key"] == "sk-secret-123")


# ---------------------------------------------------------------------------
section("8. settings.json 整体损坏时不崩（load_settings 必须兜住）")
# ---------------------------------------------------------------------------
for label, raw in [
    ("不是 JSON", "{ 这不是 json"),
    ("空文件", ""),
    ("JSON 但不是对象", "[1,2,3]"),
    ("ui 是字符串", json.dumps({"ui": "garbage"})),
    ("ui 是 null", json.dumps({"ui": None})),
    ("ui_theme 是对象", json.dumps({"ui": {"ui_theme": {"x": 1}}})),
]:
    p = config.settings_path()
    p.write_text(raw, encoding="utf-8")
    try:
        s = config.load_settings()
        ok = isinstance(s.get("ui"), dict) and s["ui"].get("ui_theme") == "reading"
        detail = f"ui={s.get('ui')!r}"
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"抛异常了: {exc!r}"
    check(f"settings.json {label} -> 仍读出合法默认主题", ok, detail)


# ---------------------------------------------------------------------------
section("9. 死 CSS 变量审计（用 var() 引用了却从没定义 = 声明整条失效）")
# ---------------------------------------------------------------------------
defined = set(re.findall(r'(--[\w-]+)\s*:', css_live))
used = set(re.findall(r'var\((--[\w-]+)', css_live))
#: 运行时才存在的变量：app.js 用 style.setProperty 注入，CSS 里查不到声明是正常的。
#: --cv-zoom      关联画布缩放
#: --rail-a-h     漫威方向右列第二格让位用的侧栏高度（骨架轴，见 test_layout.py）
#: 两者在 CSS 里都写成 var(--x, 兜底值)，所以变量缺失时退化成兜底而不是整条声明失效。
RUNTIME_ONLY = {"--cv-zoom", "--rail-a-h"}
undefined = sorted(used - defined - RUNTIME_ONLY)
check("没有「引用了但没定义」的变量", not undefined,
      "未定义: " + ", ".join(undefined))
js_src = (BASE / "web" / "app.js").read_text(encoding="utf-8")
check("每个运行时变量在 CSS 里都带兜底值（var(--x, ...)）",
      all(re.search(rf'var\(\s*{re.escape(v)}\s*,', css) for v in sorted(RUNTIME_ONLY & used)),
      "缺兜底: " + ", ".join(sorted(v for v in RUNTIME_ONLY & used
                                  if not re.search(rf'var\(\s*{re.escape(v)}\s*,', css))))
check("每个运行时变量在 app.js 里确实被 setProperty 写入",
      all(f"'{v}'" in js_src for v in sorted(RUNTIME_ONLY)),
      "白名单里的变量必须真有 JS 写入点，否则就是在给死变量开后门")


# ---------------------------------------------------------------------------
section("10. 硬编码审计：组件层不该再写死颜色")
# ---------------------------------------------------------------------------
# 主题块内部的色值是**设计令牌本身**，写成字面量是正确的；
# 这里只审主题块之外的组件规则 —— 那里的颜色才都该走变量。
# ★ 剥离必须覆盖「带选择器的规则」，不只纯变量声明块 ★
#   第一版只写了 re.sub(r'html\[data-theme="\w+"\]\s*\{[^{}]*\}', "", css)，
#   它只能剥掉 `html[data-theme="x"] { --bg: ...; }` 这种纯声明块，
#   剥不掉 `html[data-theme="starry"] .topbar { background: linear-gradient(rgba(10,22,38,.90)...) }`
#   —— 而 rgba 硬编码恰恰全在这类「主题 × 组件」规则里。
#   结果：跑出「残留 rgba(10,22,38,.90) / #0b1c2e / 10px」，
#   定位后发现全是**主题专属皮肤规则**（星月夜空顶栏、蒙娜丽莎底色等），
#   按设计就该写字面量 —— 探针的剥离范围划错了，不是样式有问题。
# 正解：凡是选择器里点名了 data-theme 的规则，整条都属于「主题层」，一并排除。
stripped = re.sub(r'/\*.*?\*/', "", css, flags=re.S)          # 先剥注释
#: 剥掉整条「选择器里出现 data-theme」的规则块（花括号配对，逐字符扫）
def _drop_themed_rules(text: str) -> str:
    out, i, n = [], 0, len(text)
    while i < n:
        m = re.search(r'html\[data-theme="\w+"\]', text[i:])
        if not m:
            out.append(text[i:])
            break
        start = i + m.start()
        # 选择器段：从上一个 } 之后到 { 为止
        brace = text.find("{", start)
        if brace == -1:
            out.append(text[i:])
            break
        out.append(text[i:start])
        # 花括号配对跳过整条规则
        depth, j = 0, brace
        while j < n:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        i = j + 1
    return "".join(out)

stripped = _drop_themed_rules(stripped)
stripped = re.sub(r':root\s*\{[^{}]*\}', "", stripped)

hex_hits = []
for i, line in enumerate(stripped.split("\n"), 1):
    if "data:image" in line:
        continue
    for m in re.finditer(r'#[0-9a-fA-F]{3,8}\b', line):
        hex_hits.append(f"#{m.group(0)[1:]}")
check("组件层无 hex 颜色字面量", not hex_hits,
      "残留: " + ", ".join(sorted(set(hex_hits))[:8]))

rgba_hits = [m.group(0) for m in re.finditer(r'rgba\([^)]*\)', stripped)]
check("组件层无 rgba 颜色字面量", not rgba_hits,
      "残留: " + ", ".join(rgba_hits[:5]))

#: 徽标级微圆角（5~8px）应走 --r-micro / --r-ctl。
#: 例外：进度条轨道的 3px（6px 高取半）是形状语义本身，不是漏换主题，
#: 所以只扫 4~10px，把 3px 留给「半高轨道」这一类。
hard_radius = []
for m in re.finditer(r'border-radius:\s*(\d+)px', stripped):
    if 4 <= int(m.group(1)) <= 10:
        hard_radius.append(m.group(1) + "px")
check("组件层无 4~10px 的写死圆角", not hard_radius,
      "残留: " + ", ".join(sorted(set(hard_radius))))

#: 主容器必须自带底色与字色，不能靠 body 继承 —— body 上有纸面颗粒层，
#: 且主题切换时 body 的 bg 会先变，滚动容器若透明会露出错位的底。
content_blk = re.search(r'\.content\s*\{([^}]*)\}', css)
check(".content 显式声明了 background 与 color",
      bool(content_blk)
      and "background: var(--bg)" in content_blk.group(1)
      and "color: var(--text)" in content_blk.group(1),
      content_blk.group(1).replace("\n", " ") if content_blk else "找不到 .content 规则")


# ---------------------------------------------------------------------------
section("11. 前端安全契约：主题 id 绝不进 innerHTML")
# ---------------------------------------------------------------------------
js = (BASE / "web" / "app.js").read_text(encoding="utf-8")
html = (BASE / "web" / "index.html").read_text(encoding="utf-8")

check("app.js 存在 applyTheme", "function applyTheme" in js)
check("applyTheme 用 setAttribute 写 data-theme",
      re.search(r"function applyTheme[\s\S]{0,300}?setAttribute\(\s*'data-theme'", js) is not None)
check("主题按钮用 textContent 渲染中文名",
      re.search(r"\.textContent\s*=\s*t\.name", js) is not None)
check("启动时读 settings 的 ui_theme 并 applyTheme",
      re.search(r"ui_theme[\s\S]{0,200}?applyTheme", js) is not None)
check("前端有主题白名单（normalize/isValid）",
      re.search(r"function (normalize|isValid)", js) is not None)

#: 主题 id 来自 settings.json，任何把它拼进 HTML 的写法都是 XSS 入口。
theme_var_names = r"(?:t\.(?:id|name|desc)|safe|\bid\b)"
innerhtml_ctx = []
for m in re.finditer(r'innerHTML\s*=\s*([^\n;]{0,120})', js):
    frag = m.group(0)
    if re.search(theme_var_names, frag):
        innerhtml_ctx.append(frag[:80])
check("app.js 无「主题字段拼进 innerHTML」", not innerhtml_ctx,
      "; ".join(innerhtml_ctx[:3]))

#: 前端白名单必须与后端一致，否则会「前端让选、后端存不下」
front_ids = re.search(r"var THEMES = \[(.*?)\n  \];", js, re.S)
check("前端 THEMES 数组存在", front_ids is not None)
if front_ids:
    got = tuple(re.findall(r"\{\s*id:\s*'([^']+)'", front_ids.group(1)))
    # ★ 断言写成动态（对 len(THEME_IDS)）而不是写死 7 ★
    #   主题数会随产品增加（2026-10-25 加了第 8 套「银塑」）。
    #   写死数字 = 每加一套主题就要回来改一次判据，忘了就假红。
    check(f"前端 {len(THEME_IDS)} 个主题 id 与后端白名单完全一致",
          got == THEME_IDS, f"前端: {got}")
    check("每个主题都有 4 格色板（sw 数组长度 4）",
          len(re.findall(r"sw:\s*\['#[0-9a-fA-F]{6}'", front_ids.group(1))) == len(THEME_IDS))
    check("每个主题都有中文名（name）",
          len(re.findall(r"name:\s*'", front_ids.group(1))) == len(THEME_IDS))

#: 设置弹窗里必须有主题区，且按钮容器可被 JS 找到
check("index.html 有 #s-theme-grid 容器", 'id="s-theme-grid"' in html)
check("主题容器带 radiogroup 语义", 'role="radiogroup"' in html)
check("触摸目标 ≥44px（.theme-opt min-height）",
      re.search(r"\.theme-opt\s*\{[^}]*min-height:\s*(\d+)px", css) is not None
      and int(re.search(r"\.theme-opt\s*\{[^}]*min-height:\s*(\d+)px", css).group(1)) >= 44)
check("主题选项至少 2 列网格（不是一排 7 个）",
      re.search(r"\.theme-grid\s*\{[^}]*grid-template-columns:\s*repeat\((?:2|auto-fill)", css) is not None)


# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
failed = [n for n, ok in results if not ok]
for n, ok in results:
    if not ok:
        print(f"  FAIL: {n}")
print(f"总计：{'全部通过' if not failed else f'{len(failed)} 项失败'}（{len(results)} 项）")
print("=" * 70)
sys.exit(1 if failed else 0)