"""公网分享链接的访问令牌透传（服务端侧改写）。

★ 为什么必须在服务端改写，而不能只靠前端脚本 ★
部署网关（CloudStudio Gateway）的硬规则是：**每一个** HTTP 请求都必须带
`?sharecode=XXX`，包括 /style.css、/app.js、/assets/*.jpg。浏览器打开分享链接
时令牌只在首屏 URL 上，而页面里的相对引用不会自动带上：

  * HTML 里的 href="style.css" / src="app.js" / src="assets/x.jpg"
    —— HTML 解析器在插入这些标签的**那一刻**就发起抓取，MutationObserver 回调
       跑在那之后，抢不过这个竞态（实测：4 个 403，页面成了无样式白页）。
  * CSS 里的 url("assets/cypresses.jpg")
    —— 相对 **CSS 文件自身 URL** 解析，更拿不到首屏 URL 上的 query。
       漏掉这条的话，首屏修好了、一切实换到梵高/向日葵等用到背景图的主题
       就会再白屏一次。

网关已实测会把 query string **原样转发给后端**（`/style.css?sharecode=X` 返回
200 + 222171 字节，与直连后端字节数一致），所以后端读得到该参数，可以在响应
里把令牌注入同源相对路径。

★ 桌面路径零改动 ★
`middleware()` 的第一行就是「不是云端模式 → 直接透传」。其次，只有「请求带
sharecode」且「响应是 html/css」时才缓冲改写，其余一律原样转发 —— 不缓冲、
不拷贝、不解码。桌面 exe 的请求既不带 sharecode 也不是 html/css，永远走透传。
"""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs

# ── 判定「本次请求带 sharecode」 ────────────────────────────────────────────
# ★ 只认参数名 sharecode★ 网关的 ?code= / ?token= 都是 403，别名一律当没有。

def extract_code(query_string: bytes) -> str:
    """从 ASGI scope 的 query_string 里取出 sharecode 值；没有则返回空串。"""
    if not query_string:
        return ""
    try:
        qs = query_string.decode("latin-1")
    except Exception:                                  # noqa: BLE001
        return ""
    vals = parse_qs(qs, keep_blank_values=False).get("sharecode") or []
    code = (vals[0] if vals else "").strip()
    # 只保留干净的令牌形态，避免把畸形输入拼进 HTML/CSS
    return code if code and re.fullmatch(r"[A-Za-z0-9_.~-]{4,128}", code) else ""


def normalize_code(raw: str) -> str:
    """校验令牌形态；合法返回原值，否则返回空串。

    ★ 抽出来是因为「从环境变量/网关头里取到的令牌」也要过同一道形态校验 ——
      畸形值拼进 HTML 会污染页面（sharecode.py 的 _append 自己也有一道，
      但那是针对 URL 不是针对令牌本身）。
    """
    raw = (raw or "").strip()
    return raw if raw and re.fullmatch(r"[A-Za-z0-9_.~-]{4,128}", raw) else ""


# ── 网关透传头里的取码 ────────────────────────────────────────────────────────
# ★ 为什么必须有这条兜底（2026-10-24 公网白屏实测）★
#   部署网关（CloudStudio Gateway）用 sharecode 做鉴权：**消费**掉这个参数，
#   不转发给后端。公网实测证据：
#     GET /?sharecode=IKDSXDUEGT        → 59107 字节，HTML 里 sharecode 出现 0 次
#     直连同一份代码（带 sharecode）        → 59254 字节，HTML 里注入 8 处 sharecode
#   ⇒ 后端**收不到** query 里的令牌 ⇒ 无法给 HTML/CSS 里的资源引用注入令牌
#     ⇒ 浏览器请求裸名 style.css/app.js/图片 → 网关 403「链接不完整」→ 白屏。
#   网关常把原始 URI 放在 X-Original-Uri / X-Forwarded-Uri 等头里透传下来，
#   那里仍有令牌 —— 从这些头里取，就不必把令牌硬编码进代码。
_GATEWAY_URI_HEADERS = (
    "x-original-uri", "x-original-url", "x-forwarded-uri", "x-forwarded-url",
    "x-rewrite-url", "x-envoy-original-path",
)


def extract_code_from_headers(headers: dict) -> str:
    """从网关透传头里取 sharecode；取不到返回空串。

    headers：{小写头名: 值}（调用方负责小写化）。
    ★ 只认**带 query** 的头值 —— 只有 path 的头里没有令牌。
    """
    for key in _GATEWAY_URI_HEADERS:
        val = (headers.get(key) or "").strip()
        if not val or "?" not in val:
            continue
        code = extract_code(val.split("?", 1)[1].encode("latin-1", "replace"))
        if code:
            return code
    return ""


def resolve_code(scope: dict) -> tuple[str, str]:
    """★ 三路取码：query → 网关透传头 → 环境变量 ★

    返回 (令牌, 来源)；取不到返回 ("", "none")。
    来源取值：query / header / env。

    ★ 为什么要三路 ★
    部署网关用 sharecode 做鉴权并**消费**该参数 —— 公网实测：
      GET /?sharecode=IKDSXDUEGT  →  HTML 里 sharecode 痕迹 0 处（直连却 8 处）
    ⇒ 后端收不到 query 里的令牌 ⇒ 无法给资源引用注入 ⇒ 网关 403 ⇒ 白屏。
    网关透传头里可能还有原始 URI；环境变量兜底由 web_server.py 自己设置
    （不依赖平台支持），三条路任一命中即可。
    """
    headers_lc = {k.decode("latin-1").lower(): v.decode("latin-1")
                  for k, v in scope.get("headers", [])}
    code = extract_code(scope.get("query_string", b""))
    if code:
        return code, "query"
    code = extract_code_from_headers(headers_lc)
    if code:
        return code, "header"
    code = normalize_code(os.environ.get("ENPRACTICE_SHARECODE", ""))
    if code:
        return code, "env"
    return "", "none"


# ── URL 改写 ────────────────────────────────────────────────────────────────
# 绝对 URL / 协议相对 / data: / 锚点 / 邮件 / javascript: 一律不碰。
_ABS_PREFIXES = ("//", "http://", "https://", "data:", "#", "mailto:",
                 "javascript:", "tel:", "blob:")


def _append(url: str, code: str, version: str = "") -> str:
    """给同源相对路径追加令牌；已带令牌或令牌为空则原样返回。

    ★ 空令牌必须原样返回 ★
    否则会往URL 后面追加一个空的 "?sharecode="（实测 index.html 被改成
    style.css?sharecode=），这既是非法 query，也会让后续判定「已带令牌」的
    逻辑误判。中间件虽然有门禁，但改写函数自身不该依赖调用方守规矩。

    ★ version 是「资源版本戳」★
    追加成 `?sharecode=XXX&v=<版本>`。理由：部署平台的 CDN 会把
    `style.css?sharecode=XXX` 这样的 URL 缓存住，**部署后仍返回旧文件**
    （实测 EO-Cache-Status: HIT，字节数是旧版的，刷新也不更新）。
    代码一改、版本戳就变 ⇒ URL 变 ⇒ 缓存键变 ⇒ 必定回源。
    网关只校验 sharecode，多带一个 v 参数无害。
    """
    if not code:
        return url
    low = url.strip().lower()
    if not url or low.startswith(_ABS_PREFIXES):
        return url
    if "sharecode=" in low:
        return url
    sep = "&" if "?" in url else "?"
    out = f"{url}{sep}sharecode={code}"
    if version:
        out += f"&v={version}"
    return out


# HTML：只匹配属性赋值语法 href/src =引号 值 引号。反向引用 \4 保证开闭引号一致，
# 值不含引号与空白，且 [^"'\s]+ 不跨空白 ⇒ 不会跨标签贪婪匹配。
_HTML_ATTR_RE = re.compile(r"""(?i)\b(href|src)(\s*=\s*)(["'])([^"'\s]+)\3""")

# CSS：url( 可无引号 / 单引号 / 双引号。data: URI 与绝对 URL 由 _append 放行。
_CSS_URL_RE = re.compile(r"""(?i)\burl\(\s*(["']?)([^)"']*)\1\s*\)""")

# ★ 内联脚本 / 样式正文必须整段跳过 ★
# <script>var s = '<img src="x.jpg">';</script> 这种正文里的 src="..."
# 不是真实属性，改正文会把 JS 字符串内容污染掉（实测 var s = '<img src="x.jpg">' 被误改）。
#
# ★★ 但不能无差别挖走所有<script> ★★
# 无非贪婪的 <script\b[^>]*>.*?</script> 会从**第一个** <script> 一路吞到
# **最后一个** </script>，把中间整篇文档（含 <script src="app.js"></script>
# 这种带 src 的真标签）一起挖走 —— 症状是该标签永远拿不到令牌。
# 所以只挖**开标签上不带 src= / href= 属性**的 rawtext 元素：那些才是纯正文。
_RAWTEXT_RE = re.compile(
    r"(?is)<(script|style|textarea|title)\b(?![^>]*\b(?:src|href)\s*=)[^>]*>.*?</\1\s*>")
# ★ 占位符里绝不能出现 "href" / "src" 这类可被属性正则匹配到的形态 ★
# 否则 _HTML_ATTR_RE 会把占位符本身当成属性值改掉，导致填回时匹配不上
# （症状是 <script src="app.js"> 没被改写，且占位符泄漏进正文）。
# 用私用区码点 + 无 '='/引号 的裸串，确保任何属性正则都碰不到它。
_PLACEHOLDER = "\ue000{}\ue001"


def rewrite_html(body: str, code: str, version: str = "") -> str:
    """把 HTML 里同源相对 href/src 补上令牌（+ 版本戳）。

    ★ 先挖走 <script>/<style>/<textarea>/<title> 正文 ★
    这些 rawtext 元素里的 src="..." 是 JS 字符串内容，不是真实属性；
    直接改正文会把脚本内容污染（实测 var s = '<img src="x.jpg">' 被误改）。
    """
    stash: list[str] = []

    def _hide(m: re.Match) -> str:
        stash.append(m.group(0))
        return _PLACEHOLDER.format(len(stash) - 1)

    work = _RAWTEXT_RE.sub(_hide, body)

    def _sub(m: re.Match) -> str:
        new = _append(m.group(4), code, version)
        if new == m.group(4):
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{new}{m.group(3)}"

    work = _HTML_ATTR_RE.sub(_sub, work)

    # 原样填回被挖走的正文
    for i, raw in enumerate(stash):
        work = work.replace(_PLACEHOLDER.format(i), raw)
    return work


def rewrite_css(body: str, code: str, version: str = "") -> str:
    """把 CSS 里同源相对 url() 补上令牌（+ 版本戳）；data: URI 噪声滤镜等不动。"""
    def _sub(m: re.Match) -> str:
        quote, url = m.group(1), m.group(2)
        new = _append(url, code, version)
        if new == url:
            return m.group(0)
        return f"url({quote}{new}{quote})"
    return _CSS_URL_RE.sub(_sub, body)