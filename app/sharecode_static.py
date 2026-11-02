# -*- coding: utf-8 -*-
"""★ 公网分享链接的令牌注入（**静态文件层**）★★

★ 为什么不用 ASGI 中间件做缓冲改写（2026-10-24 的血泪教训）★
  中间件方案必须「扣住 http.response.start，等 body 收齐后连同新的
  content-length 一起补发」。这在真实部署里踩了三个坑：

  ① **body 帧序列各版本不同**
     starlette 1.6.0 的 FileResponse._handle_simple：最后一个**非空** chunk
     就带 more_body=False；而老版本是最后一个非空 chunk 带 more_body=True、
     后面再补一个空帧。中间件若用「more_body=False」判定「收齐」，
     在老版本上会**提前收齐**（body 被截断）或**收不齐**（永远等下去）。
  ② **「body 先于 start」在部分 uvicorn 上直接抛异常**
     扣住 start 之后，ASGI 服务器先收到的是 http.response.body ⇒
     公网首屏直接 500（本地 uvicorn 0.53 容忍，所以本地测不出来）。
  ③ **http.response.pathsend 扩展**
     新版 starlette 支持「把文件路径直接交给服务器发」，那条路**根本不走
     http.response.body** ⇒ 中间件收不到 body ⇒ 永远改写不了。

★ 所以改到静态文件读取层 ★
  文件读完 → 改写 → 交回一个**普通 Response**：
  start 帧、content-length、状态码全部由 starlette 正常生成，
  不碰任何 ASGI 帧序列 ⇒ 上面三个坑全部消失。

★ 顺带去掉 etag ★
  内容改了还带着旧 etag，浏览器/网关会拿 304 继续用旧内容 ——
  这正是「公网缓存住旧版」的原因之一。
"""

from __future__ import annotations

import logging

from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from . import buildinfo, cloud, sharecode

logger = logging.getLogger(__name__)

_HTML_TYPES = ("text/html",)
_CSS_TYPES = ("text/css", "text/plain")


class ShareCodeStaticFiles(StaticFiles):
    """在静态文件层给 HTML/CSS 的相对引用注入分享令牌。

    桌面模式：第一个判断就原样返回（桌面版不涉及公网分享链接）。
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if not cloud.cloud_mode():
            return response

        code, source = sharecode.resolve_code(scope)
        if not code:
            return response

        ctype = (getattr(response, "media_type", "") or "").lower()
        is_html = any(t in ctype for t in _HTML_TYPES)
        is_css = any(t in ctype for t in _CSS_TYPES)
        if not (is_html or is_css):
            return response

        try:
            raw = self._read(response, path)
            text = raw.decode("utf-8", errors="replace")
            # ★ 版本戳：代码一改，资源 URL 就变 ⇒ CDN 缓存键就变 ⇒ 必定回源 ★
            version = buildinfo.build_fingerprint()
            out = (sharecode.rewrite_html(text, code, version) if is_html
                   else sharecode.rewrite_css(text, code, version))
            new = out.encode("utf-8")
        except Exception:                              # noqa: BLE001
            # ★ 注入失败绝不把页面弄坏 —— 原样返回 ★
            logger.warning("分享令牌注入失败，原样返回该文件", exc_info=True)
            return response

        headers = {}
        for k, v in response.headers.items():
            lk = k.lower()
            if lk in ("content-length", "etag", "accept-ranges", "last-modified"):
                continue
            headers[lk] = v
        # ★ HTML 不许被缓存 ★
        #   部署平台的 CDN 会把首屏 HTML 缓存住，**部署后仍返回旧版本**
        #   （实测 EO-Cache-Status: HIT，字节数是旧版的，反复请求也不刷新）。
        #   ⇒ 每次部署都要换分享链接才能让用户看到新版，非常容易漏。
        #   加 no-store 后，首屏永远回源拿最新代码。
        #   ★ CSS 不加 ★：CSS 的 URL 自带版本戳（&v=<代码指纹>），
        #     代码一改 URL 就变 ⇒ 缓存键就变 ⇒ 照样能回源，
        #     而版本不变时还能命中缓存，不必每次传 220KB。
        if is_html:
            headers["cache-control"] = "no-store"
        # 诊断头：只回来源与长度，不回令牌本身
        headers["x-enp-sc-source"] = source
        headers["x-enp-sc-len"] = str(len(code))
        return Response(content=new, status_code=response.status_code,
                        media_type=ctype, headers=headers)

    def _read(self, response, path: str) -> bytes:
        """读出要改写的那个文件的原始字节。

        ★ 必须用 response 里的真实路径，不能用传入的 path ★
        html 模式下访问目录时，父类返回的是 index.html 的 FileResponse，
        但传进来的 path 还是目录（""）—— 用 path 去读会拿到目录、读失败。
        """
        real = getattr(response, "path", None)
        if real:
            with open(real, "rb") as f:
                return f.read()
        full_path, _ = self.lookup_path(path)
        with open(full_path, "rb") as f:
            return f.read()
