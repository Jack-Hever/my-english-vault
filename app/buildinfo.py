# -*- coding: utf-8 -*-
"""★ 构建版本指纹（代码级）★★

★ 它解决两个问题 ★
 ① 「线上跑的到底是哪一版代码」——一条 curl 就能确认：
      GET /api/health?sharecode=XXX  →  {"ok":true,"mode":"cloud","build":"94d555ae"}
 ② **CDN 缓存住旧版本**（2026-10-24 实测：部署后标准 URL 仍 HIT 旧条目，
    字节数是旧版的，刷新也不会更新）——把指纹作为资源 URL 的版本戳
    （style.css?sharecode=XXX&v=94d555ae），代码一改 URL 就变 ⇒ 缓存键就变
    ⇒ 浏览器与网关都会回源拿新版本。

★ 为什么只覆盖这几个文件 ★
  只覆盖「影响公网行为」的那几个：路由与中间件、令牌注入、静态文件注入、云端判定、
  安全响应头（CSP 等）。
  ★ 2026-10-25 补入 security_headers.py ★：CSP 修复（内联脚本哈希引号 + LF 口径）
  是纯 Python 中间件改动，不进清单的话指纹不变、?v= 版本戳不变 ——
  「线上到底跑没跑修复」就少了一个可自动比对的锚点（云端 /api/health 已按 B3
  不再回显 build，资源 URL 的 ?v= 戳是仅剩的版本信号）。
  ★ 刻意**不**覆盖全目录 ★：部署平台只覆盖同名文件、**不删多余文件**
  （实测：沙箱里还留着早已删除的 app/sharecode_mw.py），
  若把目录里所有 .py 都算进指纹，那个「多出来的旧文件」会让本地与沙箱
  永远对不上 —— 指纹就失去了「判断版本」的作用。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

# ★ 刻意不含已删除的 sharecode_mw.py（沙箱里可能还留着它）★
# ★ security_headers.py 是固定名单成员（不是 glob）—— CSP 等安全头的唯一来源 ★
_FILES = ("main.py", "sharecode.py", "sharecode_static.py", "cloud.py",
          "security_headers.py")
# ★ web/ 里的三个关键文件也必须算进来 ★
#   只算 Python 文件的话，「只改了前端」时指纹不变 ⇒ 资源 URL 不变
#   ⇒ CDN 照样把旧 HTML/CSS 缓存住（实测：部署后标准 URL 一直 HIT 旧条目）。
_WEB_FILES = ("index.html", "app.js", "style.css")

_cache: str | None = None


def build_fingerprint() -> str:
    """8 位短哈希。读不到文件时用 "-" 占位，保证任何环境都能算出值。"""
    global _cache
    if _cache is not None:
        return _cache
    here = Path(__file__).resolve().parent
    parts = []
    for rel in _FILES:
        try:
            parts.append(hashlib.md5((here / rel).read_bytes()).hexdigest())
        except OSError:
            parts.append("-")
    web_dir = here.parent / "web"
    for rel in _WEB_FILES:
        try:
            parts.append(hashlib.md5((web_dir / rel).read_bytes()).hexdigest())
        except OSError:
            parts.append("-")
    _cache = hashlib.md5("|".join(parts).encode()).hexdigest()[:8]
    return _cache
