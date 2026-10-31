# -*- coding: utf-8 -*-
"""安全响应头中间件（B1 加固：CSP / X-Content-Type-Options / X-Frame-Options /
Referrer-Policy / HSTS）。

★ 为什么是纯 ASGI 中间件，而不是 BaseHTTPMiddleware ★
本仓库在 ShareCodeMiddleware 上踩过三个坑（starlette 各版本 body 帧序列不同、
「body 先于 start」在部分 uvicorn 上直接抛异常、http.response.pathsend 扩展
让 FileResponse 绕过 body）—— 全部源于「必须缓冲/改写 body」。本中间件
**只往 http.response.start 的头里加字段，不碰 body**，所以那些坑一个都不沾：
纯 ASGI 在同一 task 内 await 下游，零缓冲、零帧序假设。

★ 为什么 HSTS 只对云端加 ★
桌面版跑在 http://127.0.0.1:8765。给 http 响应加 HSTS，浏览器会记住
「这个域名以后只走 https」——本机调试时浏览器可能因此拒绝连接。
云端是公网 https 部署，HSTS 是标准加固。判据用 cloud_mode()。

★ CSP 的内联脚本哈希 ★
web/index.html 有两段**内联** <script>（无 src）：
  ① sharecode 引导（把 ?sharecode= 存进 sessionStorage 并补全资源引用）
  ② onboarding 接线（「重新查看新手引导」按钮）
script-src 不含 'unsafe-inline' 时，内联脚本会被浏览器直接拦停 → 白屏。
正确做法是给每段内联脚本登记 sha256 哈希（CSP hash-source）。
★★ hash-source 语法：必须带单引号 ★★
CSP 规范里 hash-source 的语法是 'sha256-<base64>'（**带单引号**）。
裸写 sha256-... 会被浏览器判为「无效源」直接忽略 —— 整条 script-src
等于没登记，内联脚本照样全拦。2026-10-25 红队复验抓到过这个 bug
（Chrome console: "contains an invalid source ... It will be ignored"）。

★★ 哈希口径：按 LF 归一化后的文本算，不是按磁盘字节 ★★
index.html 磁盘上是 CRLF，但 HTML 规范在解析前先把 CRLF/CR 归一成 LF，
浏览器对内联脚本文本按 **LF 内容**算哈希。按 CRLF 字节算出来的值
浏览器永远认不了（2026-10-25 同一个 bug 的第二个根因；当时的注释
还写着"必须按字节读、LF 会错"，恰好搞反）。Chrome 的
securitypolicyviolation 报文会直接给出它要求的哈希值，可拿来对拍。

★★ 内联脚本内容一变，这两个常量必须重算 ★★
重算方法见下方常量区的「重算方法」注释（要点：**先归一化 CRLF→LF**；
先剥 HTML 注释再提取 —— 第 20 行注释里含有 `<script>` 字样）。
test_cloud_security.py 的 [B1] 节会从 **served** HTML（TestClient 响应正文）
现场提取重算并比对，并断言 CSP 里哈希带单引号 —— 源文件改了这里忘了改，
那条断言会 FAIL。
（sharecode 改写层只动 href/src 属性、rawtext 元素正文整段跳过，
  所以 served HTML 里两段脚本的文本与源文件逐字节一致。）
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders

from . import cloud

#: 内联脚本 ①：sharecode 引导（web/index.html 第 25 行起）。
#: ★ 值按浏览器口径：CRLF→LF 归一化后的脚本文本 ★
#:   旧值 H8V+bzq... 是 CRLF 字节口径，浏览器不认（2026-10-25 红队抓到的
#:   真 bug：两段内联脚本全被拦，「重新查看新手引导」按钮没接线）。
_INLINE_BOOT_HASH = "sha256-O1vzw5NfkGNH3M0vpgKozz6tHznFDHCoesd3/pkqjCw="

#: 内联脚本 ②：onboarding 接线（web/index.html 第 1132 行起）。
_INLINE_ONBOARDING_HASH = "sha256-SCW2HRTCJuQzwlospse4PjBXj77apSh1NPNNpzIhLAc="

# ★★ 重算方法（内联脚本内容一变必须重算）★★
#   ① 读 web/index.html 后**先把 CRLF/CR 归一成 LF** —— 浏览器口径
#      （HTML 规范解析前归一化；按 CRLF 字节算的值浏览器不认）；
#   ② 剥掉 HTML 注释再提取 <script>…</script> —— index.html 第 20 行的
#      注释里含有 `<script>` 字样，不剥注释会把注释文本算进脚本内容；
#   ③ 对每个无 src 的 script 元素的**文本内容**做 sha256 → base64，
#      前面加 "sha256-"；写进 CSP 时**外面再包一层单引号**。
#   ④ 对拍基准：Chrome 的 securitypolicyviolation 报文里会写明它要求的
#      哈希（"...a hash ('sha256-...') ... is required"），那才是终审。
#   test_cloud_security.py 的 [B1] 节会从 **served** HTML（TestClient 响应
#   正文，即浏览器真正收到的字节）现场提取重算并比对，且断言哈希在 CSP
#   里带单引号 —— 源文件改了这里忘了改，那条断言会 FAIL。
#   （sharecode 改写层只动 href/src 属性、rawtext 元素正文整段跳过，
#     所以 served HTML 里两段脚本的文本与源文件逐字节一致。）

#: 内容安全策略。
#:   · style-src 留 'unsafe-inline'：index.html 有 6 处 style="" 属性
#:     （主题作用域的内联样式），逐条改哈希不现实；CSS 注入的危害由
#:     其余指令（script-src 无 unsafe-inline、object-src none）兜住。
#:   · img-src 放行 data:：CSS 噪声滤镜用 data URI 纹理。
#:   · connect-src 'self'：前端只 fetch 同源 /api/*（sharecode 只是给同源
#:     URL 加 query，不改变源）。
#:   · frame-ancestors 'none'：本应用没有任何被 iframe 嵌入的场景。
#:   · base-uri / form-action：堵 <base> 劫持与表单外发。
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    f"script-src 'self' '{_INLINE_BOOT_HASH}' '{_INLINE_ONBOARDING_HASH}'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

#: 与部署形态无关、始终加的头。
_BASE_HEADERS = (
    ("content-security-policy", _CONTENT_SECURITY_POLICY),
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "same-origin"),
)

#: 仅云端（https 公网部署）加的头。桌面 http 本地调试加了会让浏览器
#: 把 127.0.0.1 钉死成 https-only，反而连不上。
_HSTS = ("strict-transport-security", "max-age=31536000")


class SecurityHeadersMiddleware:
    """给每个 http 响应补安全头。**只碰 start 消息的头，绝不缓冲 body。**"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # ★ cloud_mode() 在**请求时**求值，不在 __init__ ★
        #   测试会先 import app 再切 ENPRACTICE_MODE；import 期求值会把
        #   桌面/云端钉死在与测试相反的状态上。
        with_hsts = cloud.cloud_mode()

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                # ★ 用 [] 赋值而不是 append ★
                #   append 在头已存在时会追第二个同头条，下游若也设过一次
                #   就出现重复头（CSP 重复 = 浏览器取交集，直接全拦）。
                #   [] 赋值是「有则替换、无则添加」，幂等。
                headers = MutableHeaders(scope=message)
                for key, value in _BASE_HEADERS:
                    headers[key] = value
                if with_hsts:
                    headers[_HSTS[0]] = _HSTS[1]
            await send(message)

        await self.app(scope, receive, send_wrapper)
