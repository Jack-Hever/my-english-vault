"""FastAPI 应用组装。

P0：健康检查、设置读写、统计。
P1：词汇 / 短语条目 CRUD、搜索筛选、标签管理、导入导出。
P2/P3 的复习与翻译练习模块将在本文件继续追加路由。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.datastructures import MutableHeaders

from . import (
    ai,
    ai_ratings,
    analytics,
    articles,
    cloud,
    db,
    entries,
    links,
    nodes,
    practices,
    review,
    security_headers,
    sharecode_static,
)
from .config import (
    API_KEY_MASK,
    SettingsWriteError,
    db_path,
    load_settings,
    save_settings,
)

#: 本模块的日志器（迁移审计等）。云端部署时由 uvicorn 的日志配置接管。
logger = logging.getLogger("enp.main")


def _web_dir() -> Path:
    """前端资源目录：源码运行取仓库内 web/；PyInstaller 打包后取解包目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "web"
    return Path(__file__).resolve().parent.parent / "web"


WEB_DIR = _web_dir()

# 统计用表名白名单（模块级常量，绝不接受外部输入）
STATS_TABLES = ("entries", "review_log", "passages", "practices", "articles")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # ★ 桌面路径逐字未改 ★
    #   云端**不能**在这里调 init_db()：那时还没有访客上下文，
    #   db.connect() 会抛 RuntimeError；就算不抛也会往全局库写 schema。
    #   云端的建库在中间件里按访客逐个做。
    if not cloud.cloud_mode():
        db.init_db()
    else:
        try:
            removed = cloud.prune_tenants()
            if removed:
                print(f"[cloud] 清理了 {removed} 个过期访客目录")
        except Exception as exc:               # noqa: BLE001 - 清理失败不该挡住启动
            print(f"[cloud] 清理访客目录失败（不影响启动）：{exc}")
        # 建好「新访客库模板」：把 168 条 SQL 的建库成本从首个 /api 请求
        # 挪到启动期。失败不阻断 —— ensure_tenant_db 里有懒生成兜底。
        cloud.warm_tenant_templates()
    yield


def _run_tenant_migration(tid: str) -> dict | None:
    """对老访客库跑一次演示语料裁剪迁移。返回审计统计；不需要迁移时返回 None。

    ★ 为什么放在 main.py 而不是 cloud.py ★
    迁移要读写**访客自己的库**，而库路径是访客感知的（config.db_path() 读
    ContextVar）。中间件已经 set 了 `cloud.current_tenant`，这里用
    `db.connect()` 就能拿到访客的库 —— 与业务层 56 处连接点同一条路径。
    ★ 幂等 ★
    `meta.seed_trim_version` 记录迁移版本，对上了就跳过。
    """
    from . import db, seed_migrate

    if tid in _migrated_tenants:
        return None
    conn = db.connect()
    try:
        if not seed_migrate.needs_migration(conn):
            _migrated_tenants.add(tid)
            return None
        stats = seed_migrate.migrate_tenant_db(conn)
    finally:
        conn.close()
    _migrated_tenants.add(tid)
    if stats:
        logger.info(
            "访客 %s 演示语料已迁移：删文章 %d 篇、删词条 %d 个、"
            "跳过文章 %d 篇、跳过词条 %d 个、清僵尸标签 %d 个",
            tid[:8],
            stats.get("articles_removed", 0),
            stats.get("entries_removed", 0),
            len(stats.get("articles_skipped", [])),
            len(stats.get("entries_skipped", [])),
            stats.get("orphan_tags_removed", 0),
        )
    return stats


#: 已经跑过迁移的访客（进程内记忆）。
#: 迁移本身幂等，这个 set 只是为了省掉每次请求查 meta 表的 IO。
_migrated_tenants: set[str] = set()

#: 请求体上限（B7 加固）。
#: 云端是公网服务，请求体可以是任意大；而本应用的实际用途
#: （词条 / 文章 / 练习提交）最大也就几十 KB。8MB 对正常使用毫无影响，
#: 但把「一个 POST 拖进几个 GB 内存」这类资源消耗型请求挡在门外。
#: ★ 只在云端生效 ★：桌面版是本机用户自己打自己，没有这个面。
MAX_REQUEST_BODY = 8 * 1024 * 1024

#: 413 响应体。文案与 FastAPI 的 {"detail": ...} 风格一致。
_TOO_LARGE_BODY = '{"detail":"请求体过大（上限 8MB）"}'.encode("utf-8")


async def _send_json(send, status: int, body: bytes) -> None:
    """纯 ASGI 手发一个 JSON 响应（不经过任何路由）。"""
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("latin-1")),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class VisitorMiddleware:
    """访客上下文中间件（**纯 ASGI**）。

    ★ 为什么用纯 ASGI 而不是 BaseHTTPMiddleware ★
    BaseHTTPMiddleware 为每个请求额外开一层 task，contextvars 的传递路径更长；
    纯 ASGI 在同一 task 内 await 下游，传递路径最短最直接。
    两种写法实测都能把 ContextVar 送进 FastAPI 的线程池（starlette 的
    run_in_threadpool 内部调 anyio.to_thread.run_sync，anyio 4.x 会
    copy_context()），纯 ASGI 少一层不确定性。

    ★ 为什么不用 request.state 下传 ★
    那要改 53 个路由的签名（每处多一个 visitor_id 参数）。ContextVar 是
    **隐式**的，55 处 db.connect() 与 53 个路由签名一个字都不用动。

    ★ 桌面模式：第一行判断后直接透传，**不发任何 header**。

    ★ 云端入口的请求体上限（B7 加固）★
    content-length 超过 :data:`MAX_REQUEST_BODY`（8MB）直接回 413，
    不建库、不迁移、不碰下游 —— 防资源消耗型超大 POST。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not cloud.cloud_mode():
            await self.app(scope, receive, send)      # ★ 桌面直接透传 ★
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        # ★ 请求体上限（B7 加固）★
        #   放在建库 / 迁移**之前**：超大请求连访客库都不该为它建一份。
        #   content-length 缺失（chunked 编码）或非法 ⇒ 放行，
        #   由 uvicorn / 下游自己处理（本层不做解析器该做的事）。
        #   ★ 桌面不走这里 ★：桌面模式第一行就透传了，行为逐字不变。
        raw_len = headers.get("content-length")
        if raw_len:
            try:
                too_big = int(raw_len) > MAX_REQUEST_BODY
            except ValueError:
                too_big = False
            if too_big:
                logger.warning(
                    "云端请求体超过上限被拒：content-length=%s path=%s",
                    raw_len, scope.get("path"))
                await _send_json(send, 413, _TOO_LARGE_BODY)
                return
        tid = cloud.read_visitor_id(headers.get("cookie", ""))
        is_new = not tid
        if is_new:
            tid = cloud.new_tenant_id()

        # 只对 API 路径建库：静态资源（html/css/js/图片）不需要访客库，
        # 为它们建库等于每个访客首屏就分配一份磁盘。
        is_api = scope.get("path", "").startswith("/api/")

        # ★ ContextVar 必须**先**设好 ★
        #   下面的迁移要走 db.connect()，而它在云端模式下要求「当前请求有访客上下文」
        #   （否则抛 RuntimeError，防止把 A 访客的数据写进公共库）。
        #   ★ 曾经把迁移放在 set 之前 ⇒ RuntimeError 被下面的 except 静默吞掉
        #     ⇒ 迁移从未真正执行（QA 实测：老访客访问后仍是 7 篇 / 60 词）。★
        ttok = cloud.current_tenant.set(tid)
        atok = cloud.request_api_key.set(
            (headers.get("x-enp-api-key") or "").strip()[:512])
        # ★ 云端：访客自填的「AI 接口地址 / 模型 / 超时」 ★
        #   与 API Key **完全同构**：请求头 → ContextVar → 只活在本次请求的内存里。
        #   ★ 为什么走请求头而不是落进访客的 settings.json ★
        #     ① 磁盘上因此**不留任何访客指定的出网地址** —— 这条不变量原样保留；
        #     ② 多访客天然隔离：每个浏览器自己带自己的地址，不按租户分流；
        #     ③ 访客填的地址必须先过 SSRF 校验才能用（见 app/egress.py），
        #        校验逻辑集中在服务端，前端绕不过去。
        #   长度截断防超长 header 撑爆；真正的长度/内容校验在 app/egress.py。
        btok = cloud.request_ai_base_url.set(
            (headers.get("x-enp-ai-base-url") or "").strip()[:512])
        mtok = cloud.request_ai_model.set(
            (headers.get("x-enp-ai-model") or "").strip()[:256])
        stok = cloud.request_ai_timeout.set(
            (headers.get("x-enp-ai-timeout") or "").strip()[:16])
        try:
            if is_api:
                try:
                    cloud.ensure_tenant_db(tid)
                except Exception:                      # noqa: BLE001
                    # 建库失败不要 500 掉整站：让下游自己报更准确的错
                    # （比如 db.connect() 的 RuntimeError 会给出可读信息）。
                    pass
                # ★ 老访客库迁移：把演示语料从 7 篇 / 60 词裁到 2 篇 / 5 词。
                # ★ 为什么必须挂在这里、而不是 ensure_tenant_db 里 ★
                #   ensure_tenant_db 第一句就是 `if target.exists(): return target`
                #   —— 老访客的库文件已存在 → 直接 return → 迁移代码永远不执行。
                #   实测：改完 seed_data 再访问老访客，文章 7 / 词条 60、
                #   seed_version=1，什么都没动。
                #   ⇒ 必须挂在**每次请求都会走的路径**上（本中间件的 /api/ 分支）。
                # ★ 为什么用「进程内 set 记住已处理」★
                #   迁移本身幂等，但每次请求都查一次 meta 表是多余的 IO；
                #   进程内记住已处理的 tid ⇒ 每个访客一辈子只查一次。
                #   （进程重启后会再查一次，那时版本已对，直接跳过。）
                try:
                    _run_tenant_migration(tid)
                except Exception:                      # noqa: BLE001
                    # 迁移失败绝不能 500 掉整站：老访客继续用旧数据，
                    # 下次请求还会再试一次（迁移是幂等的）。
                    # ★ 但不能完全静默 ★：静默会让「迁移从未执行」这类 bug
                    #   藏几个月没人发现。记一条 warning，运维在日志里能看见。
                    logger.warning(
                        "访客 %s 演示语料迁移失败（不影响本次请求，下次会重试）",
                        tid[:8], exc_info=True)

            # ★ Set-Cookie 的下发时机（2026-10-24 修正，这是「提交按钮没效果」的根因）★
            #
            #   现象：访客打开首屏 → 点「提交」→ toast「点评失败：练习记录不存在（ID=1）」。
            #
            #   根因：**原来只在 `is_api and is_new` 时下发 cookie**。
            #     首屏访问静态 HTML（is_api=False）⇒ **不种 cookie**；
            #     随后页面 JS **并发**发十几个 /api/* ⇒ 每个都「没有 cookie」
            #     ⇒ 每个都自建一个空库（tid 不同）⇒
            #     POST /api/practices 落库 A（id=1），
            #     POST /api/practices/1/submit 却落到库 B ⇒ **404**
            #     ⇒ 前端 renderResult 永不执行、结果区保持隐藏 ⇒ 看起来「按钮没效果」。
            #
            #   为什么本地测不出来：本地直连后端时 curl 手动带 cookie，
            #     且没有网关强制 sharecode ⇒ 同一套代码本地提交能出分。
            #
            #   修法：**首屏（任何非 API 路径）也下发 cookie**。
            #     这样浏览器在拿到 HTML 的同时就存下了 enp_vid，
            #     后续所有并发 /api/* 请求都带着同一个 tid ⇒ 全部落同一个库。
            #
            #   ★ 为什么不需要担心并发各建各的库 ★
            #     建库本身是并发安全的（app/cloud.py 的 ensure_tenant_db 有
            #     per-visitor 锁 + 双检 + 原子落位），而且 tid 相同 ⇒
            #     同一个目标路径 ⇒ 只会建一份。
            #
            #   ★ 为什么静态资源也可以种 cookie（不会浪费磁盘）★
            #     建库只发生在 is_api 分支（ensure_tenant_db），
            #     静态资源这条路**不建库**，只是把 cookie 头带上 ⇒
            #     访客首屏不分配磁盘，首次 /api/ 请求时才建。
            if is_new:
                cookie_value = cloud.tenant_cookie_header(tid)

                async def send_wrapper(message):
                    if message["type"] == "http.response.start":
                        MutableHeaders(scope=message).append(
                            "Set-Cookie", cookie_value)
                    await send(message)

                await self.app(scope, receive, send_wrapper)
            else:
                await self.app(scope, receive, send)
        finally:
            # ★ 必须复位 ★：不复位会把 A 访客的 id 泄漏给后续请求。
            #   （ContextVar.reset 抛异常会让整个响应 500，所以放在 finally。）
            cloud.current_tenant.reset(ttok)
            cloud.request_api_key.reset(atok)
            cloud.request_ai_base_url.reset(btok)
            cloud.request_ai_model.reset(mtok)
            cloud.request_ai_timeout.reset(stok)


# ★ 云端关掉三件套（B2 加固）★
#   /docs、/redoc、/openapi.json 在桌面是开发便利，在公网是免费的
#   API 侦察清单（每个路由、每个入参 schema 都列得整整齐齐）。
#   判据用 cloud_mode()：web_server.py 先设 ENPRACTICE_MODE 再 import
#   本模块，所以 import 期求值拿到的就是部署形态；桌面逐字不变。
#   （代码指纹的能力仍在 app/buildinfo.py，静态层的资源版本戳
#     sharecode_static.py 照用；只是不再从 health 响应出去 —— 见 B3。
#     main 里原来的 _build_fingerprint() 随 B3 一起删除，没有调用者了。）
app = FastAPI(title="我的英文库 MyEnglishVault", version="0.1.0",
              lifespan=lifespan,
              docs_url=None if cloud.cloud_mode() else "/docs",
              redoc_url=None if cloud.cloud_mode() else "/redoc",
              openapi_url=None if cloud.cloud_mode() else "/openapi.json")
# ★ 加在最前面：中间件是「后注册先执行」，这样它是**最外层**，
#   所有路由（含静态资源）都在它的上下文里。
#
# ★ 顺序有意义：SecurityHeadersMiddleware 后注册 ⇒ 它在更外层 ⇒
#   连 VisitorMiddleware 早退的 413（B7 请求体上限）响应也带得上安全头。
#   它只往 http.response.start 加头、不缓冲 body，与 sharecode 改写层
#   （静态文件读取层）互不干扰 —— 历史白屏事故的三个坑一个都不沾。
#
# ★ 分享令牌注入**不再走中间件** ★
#   原来用 ShareCodeMiddleware 在 ASGI 流式层缓冲改写（扣住 start、
#   等 body 收齐再补发）。那套写法在真实部署里踩了三个坑：
#   body 帧序列各 starlette 版本不同、「body 先于 start」在部分 uvicorn
#   上直接抛异常（公网首屏 500）、新版 starlette 的 http.response.pathsend
#   扩展让 FileResponse 绕过 body 根本收不到内容。
#   ⇒ 改到**静态文件读取层**（app/sharecode_static.py）：
#     文件读完 → 改写 → 交回普通 Response，start 与 content-length
#     都由 starlette 正常生成，零版本兼容风险。
#   详见 app/sharecode_static.py 的模块 docstring。
app.add_middleware(VisitorMiddleware)
# B1：安全响应头（CSP / nosniff / X-Frame-Options / Referrer-Policy / HSTS）。
# 后注册 = 更外层，见上方顺序说明。
app.add_middleware(security_headers.SecurityHeadersMiddleware)

# B16：可选的 Host 白名单（TrustedHostMiddleware），由部署环境显式开启。
#
# ★ 为什么不默认开启 ★
# 桌面版与本机开发常用 localhost / 127.0.0.1 / 局域网 IP 访问，
# 默认开白名单会把它们全 400 掉。而且反代场景下后端看到的 Host 是
# **反代转发后的值**，猜错名单等于全站 404 —— 这个名单只有部署方
# 知道，所以只能由部署方给。
#
# ★ 开了防什么 ★
# Host 头投毒：攻击者带一个假 Host 进来，任何"用请求 Host 拼绝对
# URL"的逻辑（缓存键、重定向目标、邮件里的链接）都会被带偏。
# 本应用自己不拼绝对 URL，但前面随时可能加一层缓存 / CDN，
# 那时这层校验就是第一道闸。
#
# ★ 格式 ★ ENPRACTICE_ALLOWED_HOSTS="example.com,www.example.com"
# 逗号分隔，支持 *.example.com 通配。不设 ⇒ 不注册该中间件
# （现有部署与桌面行为逐字不变）。
_ALLOWED_HOSTS = [h.strip() for h in
                  (os.environ.get("ENPRACTICE_ALLOWED_HOSTS") or "").split(",")
                  if h.strip()]
if _ALLOWED_HOSTS:
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_ALLOWED_HOSTS)

class SettingsPatch(BaseModel):
    ai: dict | None = None
    review: dict | None = None
    ui: dict | None = None


@app.get("/api/health")
def health() -> dict:
    # ★ 云端**不回显 db_path** ★
    #   桌面版这行显示的是 C:\Users\xxx\...\data.db，访客看了没用；
    #   云端会变成 <服务器路径>\tenants\<访客id>\data.db ——
    #   既泄露服务器目录结构，又把访客 id 明文送到页面上。
    #   ★ 云端也**不回显 build 指纹**（B3 加固）★
    #   指纹是「跑的哪一版代码」的版本戳，用途是部署对账与 CDN 缓存键，
    #   不是给访客看的探针 —— 公网回显它等于免费告诉攻击者
    #   「这是哪一版代码、有哪些已知补丁没打」。
    if cloud.cloud_mode():
        return {"ok": True, "mode": "cloud"}
    return {"ok": True, "db_path": str(db_path())}


@app.get("/api/app-info")
def app_info() -> dict:
    """部署形态。**只回形态，不回任何路径或环境变量。**"""
    return {
        "mode": "cloud" if cloud.cloud_mode() else "desktop",
        "web_search": cloud.web_search_enabled(),
        # 前端据此决定：设置弹窗里要不要显示 base_url / model 输入框
        # （云端锁死，显示了也改不动，给用户一个改不动的框比不给更糟），
        # 以及首屏那行提示该怎么写。
        "network_fetch": cloud.network_fetch_allowed(),
    }


def _mask_api_key(settings: dict) -> dict:
    """把真实密钥替换成占位值后再出网。"""
    out = dict(settings)
    ai = dict(out.get("ai", {}))
    if "api_key" in ai:
        ai["api_key"] = API_KEY_MASK if ai.get("api_key") else ""
    out["ai"] = ai
    return out


@app.get("/api/settings")
def get_settings() -> dict:
    return _mask_api_key(load_settings())


@app.put("/api/settings")
def put_settings(patch: SettingsPatch) -> dict:
    data = {k: v for k, v in patch.model_dump().items() if v is not None}

    # 前端会把 GET 到的脱敏值原样回传，表示"不修改密钥"，必须丢弃而不是落盘
    ai = data.get("ai")
    if isinstance(ai, dict) and ai.get("api_key") == API_KEY_MASK:
        data["ai"] = {k: v for k, v in ai.items() if k != "api_key"}

    try:
        saved = save_settings(data)
    except SettingsWriteError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _mask_api_key(saved)


@app.delete("/api/me/data")
def delete_my_data() -> dict:
    """访客一键清空自己的全部数据（**仅云端**）。

    ★ 为什么要有这个 ★
    大赛要求「不含个人信息」。让访客能自己删，比我们承诺"14 天后自动清理"
    强得多：承诺要靠信任，自删不需要。

    ★ 桌面模式 405 ★
    桌面库里全是本机用户自己的积累，"访客"这个概念不存在，
    给一个按钮去删自己的词库是危险品。直接 405（方法在该路径下未开放）。

    ★ 删完之后 ★
    访客的 cookie 还在 → 下一次请求会重新建一份**空库**（有 schema、无演示语料）。
    他删掉的是"我练出来的数据"，不是"这个应用"；而演示语料只是让人上手的样例，
    不该在他明确清空后长回来（靠 `tenants/<tid>.cleared` 标记实现）。
    """
    if not cloud.cloud_mode():
        raise HTTPException(status_code=405, detail="桌面模式没有「访客数据」的概念")
    tid = cloud.current_tenant.get()
    if not tid or not cloud.is_valid_id(tid):
        raise HTTPException(status_code=400, detail="访客上下文缺失或非法")
    try:
        removed = cloud.delete_tenant(tid)
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "removed": removed}


@app.get("/api/stats")
def stats() -> dict:
    conn = db.connect()
    try:
        # ★ 5 条 COUNT 合成 1 条（实测 6 → 2 条 SQL）★
        # 表名**只**来自模块级常量 STATS_TABLES，拼接的是常量本身而不是任何
        # 外部输入 —— 与改造前逐条 `SELECT COUNT(*) FROM {table}` 的安全性质
        # 完全相同，只是把它们并进一个 SELECT 的子查询里。
        row = conn.execute(
            "SELECT "
            + ", ".join(f"(SELECT COUNT(*) FROM {t}) AS {t}" for t in STATS_TABLES)
        ).fetchone()
        counts = {table: row[table] for table in STATS_TABLES}
    finally:
        conn.close()
    return counts


@app.get("/api/analytics")
def analytics_data(days: int = Query(default=analytics.TREND_DAYS, ge=7, le=90)) -> dict:
    """学习进度分析：30 天趋势、热门标签、新词、新外刊。

    只读聚合。days 可调（7~90 天），默认 30 天。
    """
    return analytics.get_analytics(days)


# --------------------------------------------------------------------------
# P1：词汇 / 短语库
# --------------------------------------------------------------------------


class EntryIn(BaseModel):
    """条目新增 / 编辑入参。语义校验交给 entries.normalize_entry_payload。

    评分两列必须显式声明，否则 pydantic 会把未知名段丢掉、用户打的分数根本传不进来。
    落库前一律用 model_dump(exclude_unset=True)：只有**请求里真的带了**的字段才进
    payload，这样 entries 层才能区分「没传」与「显式传 null 要清除」。
    """

    type: str = "word"
    term: str = ""
    pos: str | None = None
    meaning_zh: str = ""
    usage: str | None = None
    source: str | None = None
    importance: float | None = None
    usefulness: float | None = None


def _parse_pagination(
    page: int, page_size: int, max_page_size: int | None = None
) -> tuple[int, int]:
    """校验并归一分页参数。

    max_page_size 缺省用词条模块的通用上限（200）；练习历史有自己的更小上限
    （practices.MAX_PAGE_SIZE = 100），走各自的上限而不是一刀切。
    """
    limit = entries.MAX_PAGE_SIZE if max_page_size is None else max_page_size
    if page < 1:
        raise entries.EntryError(f"page 必须大于等于 1，收到：{page}")
    if not 1 <= page_size <= limit:
        raise entries.EntryError(
            f"page_size 必须在 1 到 {limit} 之间，收到：{page_size}"
        )
    return page, page_size


@app.get("/api/entries")
def list_entries(
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    type: str = Query("", description="word / phrase，留空表示全部"),  # noqa: A002
    page: int = 1,
    page_size: int = entries.DEFAULT_PAGE_SIZE,
    sort: str = "created_at",
    order: str = "desc",
) -> dict:
    """条目列表：关键词（词条/释义/例句）+ 日期区间 + 类型，可叠加。"""
    page, page_size = _parse_pagination(page, page_size)
    conn = db.connect()
    try:
        return entries.list_entries(
            conn,
            q=q,
            date_from=date_from,
            date_to=date_to,
            entry_type=type,
            page=page,
            page_size=page_size,
            sort=sort,
            order=order,
        )
    finally:
        conn.close()


@app.post("/api/entries", status_code=201)
def create_entry(payload: EntryIn) -> dict:
    conn = db.connect()
    try:
        return entries.create_entry(conn, payload.model_dump(exclude_unset=True))
    finally:
        conn.close()


@app.get("/api/entries/export")
def export_entries(
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    type: str = Query(""),  # noqa: A002
) -> Response:
    """导出为 JSON 文件下载；可复用与列表相同的筛选条件。"""
    conn = db.connect()
    try:
        payload = entries.export_entries(
            conn,
            q=q,
            date_from=date_from,
            date_to=date_to,
            entry_type=type,
        )
    finally:
        conn.close()
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    filename = f"enpractice-entries-{payload['exported_at'][:10]}.json"
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/entries/import")
def import_entries(
    payload: dict | list = Body(..., description='{"entries": [...]} 或裸数组'),
    on_duplicate: str = "skip",
) -> dict:
    """导入 JSON：{"entries": [...]} 或裸数组；on_duplicate=skip|overwrite。"""
    conn = db.connect()
    try:
        return entries.import_entries(conn, payload, on_duplicate=on_duplicate)
    finally:
        conn.close()


# 注意：/api/entries/{entry_id} 必须放在 export / import 之后，否则会被路径参数吃掉
@app.get("/api/entries/{entry_id}")
def get_entry(entry_id: int) -> dict:
    conn = db.connect()
    try:
        return entries.get_entry(conn, entry_id)
    finally:
        conn.close()


@app.put("/api/entries/{entry_id}")
def update_entry(entry_id: int, payload: EntryIn) -> dict:
    conn = db.connect()
    try:
        return entries.update_entry(conn, entry_id, payload.model_dump(exclude_unset=True))
    finally:
        conn.close()


@app.delete("/api/entries/{entry_id}")
def delete_entry(entry_id: int) -> dict:
    """删词条。

    响应里的 tombstone 字段告诉前端要不要提示用户：
      - tombstone.tombstoned 非 null → 该词条在概念树里有子项，节点已留成占位节点，
        子项仍挂在它下面；前端应提示「已删除，树中保留占位」。
      - tombstone 为 null → 没有留占位（无子项 / 不在树上），前端不必提示。
    """
    conn = db.connect()
    try:
        return entries.delete_entry(conn, entry_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 词汇关联库（近义词等）+ 重要性 / 实用性评分
# --------------------------------------------------------------------------


class LinkIn(BaseModel):
    """新建关联组。成员至少 2 个（min_length 由 pydantic 先挡一道，
    去重后不足 2 个的情况由业务层再挡——[3,3] 这类重复也得挡住）。"""

    relation: str = "synonym"
    note: str = ""
    entry_ids: list[int] = Field(default_factory=list, min_length=2)


class LinkUpdateIn(BaseModel):
    """局部更新关联组。

    三个字段一律 None = 不改动。写成 "" / [] 会让「只改关系」把备注与成员
    一起抹掉——本项目已在 extra_info / terms 上踩过两次同样的坑。
    """

    relation: str | None = None
    note: str | None = None
    entry_ids: list[int] | None = None


class LinkRemoveIn(BaseModel):
    entry_id: int


class RatingsPatchIn(BaseModel):
    """打分入参：传数字 = 设置，传 null = 清除，不传 = 不动。"""

    importance: float | None = None
    usefulness: float | None = None


@app.get("/api/links")
def list_links_api(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=links.MAX_PAGE_SIZE)
) -> dict:
    conn = db.connect()
    try:
        return links.list_links(conn, page=page, page_size=page_size)
    finally:
        conn.close()


@app.post("/api/links", status_code=201)
def create_link_api(payload: LinkIn) -> dict:
    conn = db.connect()
    try:
        return links.create_link(
            conn, payload.entry_ids, relation=payload.relation, note=payload.note
        )
    finally:
        conn.close()


@app.get("/api/links/{link_id}")
def get_link_api(link_id: int) -> dict:
    conn = db.connect()
    try:
        return links.get_link(conn, link_id)
    finally:
        conn.close()


@app.put("/api/links/{link_id}")
def update_link_api(link_id: int, payload: LinkUpdateIn) -> dict:
    conn = db.connect()
    try:
        # exclude_unset：让「没传」与「显式传 None 要改动」分得开
        return links.update_link(conn, link_id, **payload.model_dump(exclude_unset=True))
    finally:
        conn.close()


@app.delete("/api/links/{link_id}")
def delete_link_api(link_id: int) -> dict:
    conn = db.connect()
    try:
        return {"deleted": links.delete_link(conn, link_id)}
    finally:
        conn.close()


@app.post("/api/links/{link_id}/remove")
def remove_link_member_api(link_id: int, payload: LinkRemoveIn) -> dict:
    conn = db.connect()
    try:
        return links.remove_member(conn, link_id, payload.entry_id)
    finally:
        conn.close()


@app.get("/api/entries/{entry_id}/links")
def entry_links_api(entry_id: int) -> dict:
    """某词条的关联词条，供词汇库释义栏渲染超链接。

    双向由后端的「同组其他成员」自连接保证：A 与 B 同组，查 A 得 B、查 B 得 A。
    """
    conn = db.connect()
    try:
        # 先确认词条存在：一个已被删除的词条若返回空 links，前端会渲染成
        # 「它没有关联」，而真相是「它根本不存在」。多一次主键查询，换语义正确。
        entries.get_entry(conn, entry_id)  # 不存在 → 404
        return {"entry_id": int(entry_id), "links": links.links_of_entry(conn, entry_id)}
    finally:
        conn.close()


@app.patch("/api/entries/{entry_id}/ratings")
def patch_ratings_api(entry_id: int, payload: RatingsPatchIn) -> dict:
    """给词条的重要性 / 实用性打分（0–5，0.5 增量）。

    星级控件直接调这个端点：传 null = 清除评分回到「未评分」，传数字 = 设置。
    """
    data = payload.model_dump(exclude_unset=True)
    conn = db.connect()
    try:
        # 窄接口：只改评分列。校验（越界 / 非半星）在业务层抛 EntryError → 400
        return entries.update_ratings(conn, entry_id, data)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 需求 4：AI 辅助评分（建议值，绝不自动落库）
# --------------------------------------------------------------------------


class RatingsSuggestIn(BaseModel):
    """AI 评分建议入参。

    overwrite 的语义（**只影响给不给建议，不影响写不写**）：
    - False（默认）：已有手动评分的词条被跳过，返回 skipped=true + skip_reason，
      「AI 不覆盖用户评分」这条需求就落在这里；
    - True：连已有评分的词条一起评估，但**仍然只返回建议**。前端必须显示
      「接受将覆盖已有手动评分」的提示，由用户点「接受」才真正写库。

    无论 True 还是 False，本接口**都不会修改数据库**。
    落库唯一路径是前端在确认面板点「接受」后调 PATCH /api/entries/{id}/ratings。
    """

    entry_ids: list[int] = Field(default_factory=list, min_length=1)
    overwrite: bool = False


@app.post("/api/entries/ratings/suggest")
def suggest_ratings_api(payload: RatingsSuggestIn) -> dict:
    """为指定词条生成 AI 建议评分（重要性 / 实用性 + 各一句中文理由）。

    - **绝不写库**：本接口只返回建议，用户点「接受」后前端才 PATCH 落库。
    - 单条失败不影响整批：每条自带 error 字段，整批不会因一条而 500。
    - 内部按 ai_ratings.BATCH_SIZE 分批串行调用，单次上限
      ai_ratings.MAX_IDS_PER_REQUEST，超限 400。
    - 未配置 AI 密钥时返回 400（AINotConfiguredError），**不静默降级**：
      编造的评分会被用户当真实建议采纳并写进词库。
    """
    conn = db.connect()
    try:
        return ai_ratings.suggest(conn, payload.entry_ids, overwrite=payload.overwrite)
    finally:
        conn.close()


class UnratedSuggestIn(BaseModel):
    """「批量评估未评分词条」入参：后端自己挑 id，前端不必先拉全量列表。

    limit / offset 是为前端分批 + 中断服务的：前端每批取 8 条，
    拿够就停，用户点「中断」后不再发下一批。
    offset 必须存在 —— 跳过已评分词条后 id 集合会变，只靠 limit 反复取首页
    会把同一批词无限重复取回。
    """

    limit: int = Field(default=ai_ratings.BATCH_SIZE, ge=1, le=ai_ratings.MAX_IDS_PER_REQUEST)
    offset: int = Field(default=0, ge=0)
    overwrite: bool = False


@app.post("/api/entries/ratings/suggest-unrated")
def suggest_unrated_api(payload: UnratedSuggestIn) -> dict:
    """批量评估**未评分**词条（两维都为空的那些）。

    单独开一个端点而不是让前端先 GET /api/entries 再挑 id：
    「未评分」的口径（importance 与 usefulness 都为 NULL）必须由后端定义，
    否则前端漏一个条件就会把已评分的词条送去重评。
    """
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id FROM entries "
            "WHERE importance IS NULL AND usefulness IS NULL "
            "ORDER BY id LIMIT ? OFFSET ?",
            (int(payload.limit), int(payload.offset)),
        ).fetchall()
        ids = [int(r["id"]) for r in rows]
        if not ids:
            return {"items": [], "total": 0, "evaluated": 0, "skipped": 0,
                    "failed": 0, "is_mock": False}
        return ai_ratings.suggest(conn, ids, overwrite=payload.overwrite)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# P2：外刊文章库
# --------------------------------------------------------------------------


class ArticleIn(BaseModel):
    title: str = ""
    # 中文标题（选填）。默认 None 而非 ""，理由与 extra_info 完全一样：
    # update_article 用「键缺失/None → 保留原值，空串 → 显式清空」区分改与清，
    # 默认 "" 会让任何一次局部更新都静默抹掉用户写的中文标题。
    title_zh: str | None = None
    content: str = ""
    content_zh: str = ""
    source_name: str | None = None
    url: str | None = None
    # 多标签：数组或逗号分隔字符串均可（normalize_tag_names 两种都收）
    tags: list[str] | str = []
    # 其他信息（作者信息/文章背景等文章级备注）。
    # 默认 None 而非 ""：update_article 用「键缺失/None → 保留原值，空串 → 显式清空」
    # 的约定区分改与清。若这里默认 ""，pydantic 会把「PUT 未带该字段」也填成空串，
    # 于是任何一次局部更新都会静默抹掉用户写的信息。
    extra_info: str | None = None


class PickIn(BaseModel):
    """本地外刊库选句入参：只需要目标词列表。"""

    terms: list[str] = Field(default_factory=list, min_length=1, max_length=20)


class WebPickIn(PickIn):
    """网络外刊选句入参：目标词 + 外刊真实链接。"""

    url: str


@app.get("/api/articles")
def list_articles_api(
    q: str = "",
    tags: list[str] = Query(default=[]),
    page: int = 1,
    page_size: int = entries.DEFAULT_PAGE_SIZE,
) -> dict:
    page, page_size = _parse_pagination(page, page_size)
    conn = db.connect()
    try:
        return articles.list_articles(conn, q=q, tags=tags, page=page, page_size=page_size)
    finally:
        conn.close()


@app.get("/api/articles/tags")
def list_article_tags_api() -> list[str]:
    """全部已建标签名（按使用文章数倒序），供外刊库筛选项渲染。"""
    conn = db.connect()
    try:
        return articles.list_tag_names(conn)
    finally:
        conn.close()


@app.post("/api/articles", status_code=201)
def create_article_api(payload: ArticleIn) -> dict:
    conn = db.connect()
    try:
        return articles.create_article(conn, payload.model_dump())
    finally:
        conn.close()


@app.post("/api/articles/pick-sentences")
def pick_sentences_api(payload: PickIn) -> list[dict]:
    """按目标词从本地外刊库选句：随机 3 个，不足则全取。"""
    conn = db.connect()
    try:
        return articles.pick_sentences_for_terms(conn, payload.terms)
    finally:
        conn.close()


@app.post("/api/articles/web-pick-sentences")
def web_pick_sentences_api(payload: WebPickIn) -> list[dict]:
    """外刊库检索不到时的兜底：从网络外刊抓取选句，内容不入库。

    ★ 云端闸门（SSRF）：云端一律 400，桌面逐字不变 ★
    这个路由把**访客给的任意 url**交给 `articles.sentences_from_web`，
    后者是裸的 `requests.get(url)` —— 没有域名白名单、没有内网网段拦截。
    公网部署时，任何访客都能让你的服务器去请求：
      · `http://169.254.169.254/latest/meta-data/`（云厂商元数据，可能拿到临时凭据）
      · `http://127.0.0.1:8000/...` 或任何 `10.x` / `192.168.x` 内网地址（端口探测）
    所以闸门加在**调用点**而不是 `articles.py` 里（后者本轮明令 0 改动）。
    闸门判断只有一行 `cloud.cloud_mode()`，桌面分支的行为一字未变。
    """
    if not cloud.network_fetch_allowed():
        raise HTTPException(status_code=400, detail=cloud.WEB_PICK_DISABLED_REASON)
    return articles.sentences_from_web(payload.url, payload.terms)


@app.get("/api/articles/{article_id}")
def get_article_api(article_id: int) -> dict:
    conn = db.connect()
    try:
        return articles.get_article(conn, article_id)
    finally:
        conn.close()


@app.get("/api/articles/{article_id}/sentences")
def list_article_sentences_api(article_id: int) -> list[dict]:
    conn = db.connect()
    try:
        return articles.list_sentences(conn, article_id)
    finally:
        conn.close()


@app.put("/api/articles/{article_id}")
def update_article_api(article_id: int, payload: ArticleIn) -> dict:
    conn = db.connect()
    try:
        return articles.update_article(conn, article_id, payload.model_dump())
    finally:
        conn.close()


@app.delete("/api/articles/{article_id}")
def delete_article_api(article_id: int) -> dict:
    conn = db.connect()
    try:
        article_id = articles.delete_article(conn, article_id)
    finally:
        conn.close()
    return {"deleted": article_id}


# --------------------------------------------------------------------------
# P3：翻译练习（含内嵌的每日复习）
# --------------------------------------------------------------------------


class PracticeIn(BaseModel):
    """新建练习入参。terms 为空时自动取「当日到期 + 近3天新学」的词。

    ★ max_terms ★
    本轮最多纳入多少个目标词（用户在练习页自己设：3/5/8/10/15/20，默认 8）。
    老客户端不传 → 用默认值 8 → 行为与改造前逐字一致。
    ★ 上限校验放在 pydantic 里 ★
    max_length=20 是给「显式列出的词」的硬上限（terms 列表本身）；
    max_terms 用 Field 的 ge/le 夹住，避免有人 POST 一个 99999 把候选池撑爆。
    """

    terms: list[str] = Field(default_factory=list, max_length=20)
    web_fallback_url: str = ""
    # 按评分下限筛练习内容（需求 3-②）。默认 None = 不筛选。
    # 用 None 而非 0.0：0.0 会让「没传」变成「筛掉所有 0 分词」，
    # 而未评分（NULL）与 0 分是两回事，不该因为没传就被排除。
    min_importance: float | None = None
    min_usefulness: float | None = None
    # ★ 每轮词数：默认 8（= practices.MAX_TERMS，改造前的行为）。
    #   上限 20 与 terms 的 max_length=20 对齐 —— 一轮最多 20 个词。
    max_terms: int = Field(default=8, ge=1, le=20)


class SubmitIn(BaseModel):
    user_text: str = ""
    # 当前题号（1-based）。练习页逐题作答，点评只针对这一句。
    seq: int = 1
    # 本句命中的目标词（文本），用于把复习排期只写回这几个词。
    # 默认 None 而非 []：pydantic 会把「旧客户端没传」也填成 []，
    # 后端就分不清「本句确实没有词条（不该写排期）」与「旧客户端（回退整轮）」。
    terms: list[str] | None = None


class ReviewMarkIn(BaseModel):
    entry_id: int
    remembered: bool


@app.post("/api/practices", status_code=201)
def create_practice_api(payload: PracticeIn) -> dict:
    conn = db.connect()
    try:
        return practices.create_practice(
            conn,
            payload.terms,
            web_fallback_url=payload.web_fallback_url,
            min_importance=payload.min_importance,
            min_usefulness=payload.min_usefulness,
            max_terms=payload.max_terms,
        )
    finally:
        conn.close()


@app.post("/api/practices/{practice_id}/submit")
def submit_practice_api(practice_id: int, payload: SubmitIn) -> dict:
    conn = db.connect()
    try:
        return practices.submit_translation(
            conn, practice_id, payload.user_text,
            seq=payload.seq, terms=payload.terms,
        )
    finally:
        conn.close()


@app.get("/api/practices")
def list_practices_api(page: int = 1, page_size: int = 20) -> dict:
    # 练习历史用自己的单页上限（100），比词条模块的通用上限更紧：
    # 练习流水最多就 25 条，单页再大也没意义，但上限本身要挡着，
    # 免得 page_size=99999 一次性把整张表拉出来。
    page, page_size = _parse_pagination(page, page_size, practices.MAX_PAGE_SIZE)
    conn = db.connect()
    try:
        return practices.list_practices(conn, page=page, page_size=page_size)
    finally:
        conn.close()


@app.get("/api/practices/stats")
def practice_stats_api() -> dict:
    conn = db.connect()
    try:
        return practices.practice_stats(conn)
    finally:
        conn.close()


@app.get("/api/practices/{practice_id}")
def get_practice_api(practice_id: int) -> dict:
    conn = db.connect()
    try:
        return practices.get_practice(conn, practice_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 就评分与 AI 讨论（建议值，绝不自动落库）
#
# ★ 两条路由的分工 ★
#   POST  /api/practices/{id}/discuss     纯讨论，**只读不写**
#   PATCH /api/practices/{id}/revise-scores  回写，**只有用户点「采纳」才调**
#
# 拆成两条而不是让 discuss 顺带写库：discuss 被调用 ≠ 用户认可修正分。
# 混在一起就等于"问一句就改分"，用户失去否决权（同 ai_ratings 的原则）。
# --------------------------------------------------------------------------


class DiscussIn(BaseModel):
    question: str = ""


class ReviseScoresIn(BaseModel):
    """采纳修正评分的入参。

    scores / total 是**待写入**的修正值；expected_old 是用户看到的那组旧分，
    用于并发比对（不一致 → 409，不覆盖别人的写入）。
    question / reply 是留痕用的上下文——写入 ai_feedback.revisions，
    让用户事后能查到"这个分为什么被改过"。
    """

    scores: dict = Field(default_factory=dict)
    total: float = 0
    question: str = ""
    reply: str = ""
    expected_old: dict | None = None


@app.post("/api/practices/{practice_id}/discuss")
def discuss_practice_api(practice_id: int, payload: DiscussIn) -> dict:
    """就本题评分与 AI 讨论。**绝不修改数据库。**

    未配置 AI 密钥时返回 400（AINotConfiguredError），不静默降级成编造回答：
    编造的讨论结论会直接影响用户对分数的判断，进而可能改写真实评分。
    AI 返回的修正分不合法时返回 502（DiscussScoreError 继承 AIError），
    同样不返回半成品——不点采纳不会写库，但点采纳时也必须写不进假分。
    """
    conn = db.connect()
    try:
        return practices.discuss(conn, practice_id, payload.question)
    finally:
        conn.close()


@app.patch("/api/practices/{practice_id}/revise-scores")
def revise_scores_api(practice_id: int, payload: ReviseScoresIn) -> dict:
    """采纳 AI 的修正评分，回写该条练习的四项分数。

    分数不合法（越界 / 缺项 / total 对不上）→ 400，**不静默夹取**。
    expected_old 与库里的当前值不一致 → 409，不覆盖别人的写入。
    """
    conn = db.connect()
    try:
        return practices.adopt_revised_scores(
            conn, practice_id,
            payload.scores, payload.total,
            question=payload.question,
            reply=payload.reply,
            expected_old=payload.expected_old,
        )
    finally:
        conn.close()


# --- 复习（翻译练习模块内的子功能）---


@app.get("/api/review/due")
def review_due_api(mode: str = "interval", limit: int = 20, offset: int = 0) -> list[dict]:
    conn = db.connect()
    try:
        return review.due_entries(conn, mode=mode, limit=limit, offset=offset)
    finally:
        conn.close()


@app.get("/api/review/due-count")
def review_due_count_api() -> dict:
    conn = db.connect()
    try:
        return {"due": review.due_count(conn)}
    finally:
        conn.close()


@app.post("/api/review/mark")
def review_mark_api(payload: ReviewMarkIn) -> dict:
    conn = db.connect()
    try:
        return review.mark_reviewed(conn, payload.entry_id, payload.remembered)
    finally:
        conn.close()


@app.get("/api/ai/status")
def ai_status_api() -> dict:
    """告诉前端当前是真实 AI 还是演示模式，避免把 mock 结果当真实评分。

    ★ 同时回显**当前生效的接口地址与模型** ★
    云端访客可以自填这两项（走请求头通道，见 app/egress.py 的出网白名单）。
    回显出来，前端才能确认"我填的地址确实被服务器用上了"，
    而不是像改造前那样——填了没反应也不知道卡在哪一步。
    """
    cfg = ai._settings()
    return {
        "mock": ai.is_mock_mode(),
        "model": cfg.get("model", ""),
        "base_url": cfg.get("base_url", ""),
    }


# --------------------------------------------------------------------------
# 需求 6：概念节点 + 层级 / 从属关联（放射图数据源）
#
# 与 /api/links（近义词关联组）平行且**互不干涉**：层级是有向父子关系，
# 关联组是无向同伴关系，两套语义不能混在一张表里（详见 nodes.py 模块注释）。
# 写操作一律「不传 = 不改动」，用 model_dump(exclude_unset=True) 传给业务层。
#
# ★ 路由顺序 ★
# /api/nodes/concept、/api/nodes/from-entry 这类字面量路径必须声明在
# /api/nodes/{node_id} 之前，否则会被路径参数吃掉（FastAPI 按声明顺序匹配）。
# --------------------------------------------------------------------------

#: 概念节点的 label：必填，但长度上限由业务层 nodes._clean_label 校验并给中文提示，
#: 不在这里写 max_length——那样错误信息会变成 pydantic 的英文校验话术。
class ConceptIn(BaseModel):
    label: str = ""
    note: str = ""
    parent_id: int | None = None
    position: int | None = None


class FromEntryIn(BaseModel):
    """把真实词条挂到某节点下；节点不存在则 get-or-create。"""

    entry_id: int
    parent_id: int | None = None
    position: int | None = None


class ChildSpecIn(BaseModel):
    """内联子节点定义。kind 缺省按 concept 处理。

    kind=entry   → 必须给 entry_id
    kind=concept → 必须给 label（note 选填）
    """

    kind: str = nodes.KIND_CONCEPT
    entry_id: int | None = None
    label: str = ""
    note: str = ""


class ChildIn(BaseModel):
    child: ChildSpecIn
    position: int | None = None


class AttachIn(BaseModel):
    """把已有节点挂到该父下（幂等，拖拽换父用）。position 不传 = 追加到末尾。"""

    position: int | None = None


class OrderIn(BaseModel):
    """兄弟序全量替换。child_ids 必须与该父的子节点集合完全一致（多/少都 400）。"""

    child_ids: list[int] = Field(default_factory=list)


class NodePatchIn(BaseModel):
    """局部更新概念节点。None = 不改动。

    真实词条节点传 label 会 400：名字归 entries.term 管，在节点上另存一份必然失步。
    """

    label: str | None = None
    note: str | None = None


@app.get("/api/nodes/tombstones")
def list_tombstones_api(q: str = "") -> dict:
    """列全部「已删除词条」的占位节点（用户手动清理的入口）。

    ★ 路由顺序 ★
    必须声明在 `@app.get("/api/nodes/{node_id}")` **之前**，否则 "tombstones"
    会被当成 node_id 解析掉（FastAPI 按声明顺序匹配，见本节顶部的说明）。
    """
    conn = db.connect()
    try:
        return nodes.list_tombstones(conn, q=q)
    finally:
        conn.close()


@app.post("/api/nodes/tombstones/purge-all")
def purge_all_tombstones_api() -> dict:
    """一次性清理全部占位节点（子节点提升，不删用户的树）。"""
    conn = db.connect()
    try:
        return nodes.purge_all_tombstones(conn)
    finally:
        conn.close()


@app.post("/api/nodes/purge-empty")
def purge_empty_nodes_api() -> dict:
    """清理「从没被挂进任何树」的孤立词条节点行（需求①）。

    ★ 必须声明在 `/api/nodes/{node_id}` 之前 ★
    否则 "purge-empty" 会被当成 node_id 解析掉（路由按声明顺序匹配）。

    ★ 只由用户手动触发 ★
    前端在关联库放一个「清理空壳」按钮，用户点了才打到这里。
    绝不在启动时自动跑 —— 自动删会让用户觉得数据莫名消失。

    幂等：第二次调用命中 0 行，返回 deleted=0（见 nodes.purge_empty_nodes）。
    只删 nodes 行，词条（entries）一个都不动。
    """
    conn = db.connect()
    try:
        return nodes.purge_empty_nodes(conn)
    finally:
        conn.close()


@app.post("/api/nodes/{node_id}/purge")
def purge_tombstone_api(node_id: int) -> dict:
    """清理单个占位节点：子节点提升到它的父下，然后删掉这个空壳。

    只接受占位节点——对正常节点调它会 400，避免手滑把用户的真实结构当垃圾清掉。
    """
    conn = db.connect()
    try:
        return nodes.purge_tombstone(conn, node_id)
    finally:
        conn.close()


@app.post("/api/nodes/concept")
def create_concept_api(payload: ConceptIn) -> dict:
    """建概念节点；parent_id 给了就同时挂上（前端主路径之一）。"""
    conn = db.connect()
    try:
        return nodes.create_concept(
            conn,
            payload.label,
            note=payload.note,
            parent_id=payload.parent_id,
            position=payload.position,
        )
    finally:
        conn.close()


@app.post("/api/nodes/from-entry")
def attach_entry_api(payload: FromEntryIn) -> dict:
    """把真实词条挂到某节点下。返回 {"node": ..., "created": 节点行是否本次新建}。

    节点已存在时 created=false，此时这次调用等价于「换父」。
    """
    conn = db.connect()
    try:
        return nodes.attach_entry(
            conn,
            payload.entry_id,
            parent_id=payload.parent_id,
            position=payload.position,
        )
    finally:
        conn.close()


@app.post("/api/nodes/{node_id}/children")
def attach_child_api(node_id: int, payload: ChildIn) -> dict:
    """一次完成「找/建子节点 + 建边」。同一 (parent, child) 重复提交幂等。

    状态码刻意用 200 而不是 201：本端点语义是幂等的 get-or-create，
    重复提交返回同一个节点并非「新建了资源」，回 201 会误导前端当成新资源入栈。
    """
    conn = db.connect()
    try:
        return nodes.attach_child(
            conn, node_id, payload.child.model_dump(exclude_unset=True), payload.position
        )
    finally:
        conn.close()


@app.put("/api/nodes/{node_id}/children/{child_id}")
def attach_existing_child_api(node_id: int, child_id: int, payload: AttachIn) -> dict:
    """把已有节点挂到该父下（幂等）。拖拽换父走这条。"""
    conn = db.connect()
    try:
        nodes._link(conn, node_id, child_id, payload.position)
        return nodes.get_node(conn, child_id)
    finally:
        conn.close()


@app.delete("/api/nodes/{node_id}/children/{child_id}")
def detach_child_api(node_id: int, child_id: int) -> dict:
    """移出子节点：断边，子节点升级为根。**不删节点、不删词条。**

    这是比「删词条」轻得多的摘除操作，应优先引导用户用它。
    """
    conn = db.connect()
    try:
        return nodes.detach_child(conn, node_id, child_id)
    finally:
        conn.close()


@app.patch("/api/nodes/{node_id}/children/order")
def reorder_children_api(node_id: int, payload: OrderIn) -> list[dict]:
    """兄弟序全量替换（拖拽排序）。校验 child_ids 与当前子集合完全一致。"""
    conn = db.connect()
    try:
        return nodes.reorder_children(conn, node_id, payload.child_ids)
    finally:
        conn.close()


@app.patch("/api/nodes/{node_id}")
def update_node_api(node_id: int, payload: NodePatchIn) -> dict:
    """改概念节点名 / 备注。不传 = 不改动。"""
    conn = db.connect()
    try:
        return nodes.update_node(
            conn, node_id, **payload.model_dump(exclude_unset=True)
        )
    finally:
        conn.close()


@app.delete("/api/nodes/{node_id}")
def delete_node_api(node_id: int, mode: str = nodes.DELETE_CASCADE) -> dict:
    """删节点。mode=cascade（默认）删整棵子树；mode=promote 把子节点接到父下。

    两种模式都**不触碰 entries 表**——被删节点对应的词条一个都不会少。
    """
    conn = db.connect()
    try:
        return nodes.delete_node(conn, node_id, mode)
    finally:
        conn.close()


@app.get("/api/nodes/{node_id}/tree")
def get_tree_api(
    node_id: int,
    shape: str = nodes.SHAPE_CHILDREN,
    max_depth: int | None = None,
    default_expanded_depth: int | None = None,
) -> dict:
    """取整棵树（**放射图数据源**）。

    - shape=children（默认）嵌套 children；shape=flat 按 (depth, position) 输出扁平数组
    - default_expanded_depth：depth 超过它的节点返回 children=[] + has_children=true，
      前端无需自己剪枝。展开态本身存 localStorage，不入库（见设计 6.2）。
    """
    conn = db.connect()
    try:
        return nodes.get_tree(
            conn,
            node_id,
            shape=shape,
            max_depth=max_depth,
            default_expanded_depth=default_expanded_depth,
        )
    finally:
        conn.close()


@app.get("/api/nodes")
def list_roots_api(
    q: str = "",
    roots: int = Query(1, description="1 = 只列根节点（放射图列表）；0 = 列全部节点"),
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """列根节点（＝放射图列表），带每棵树的统计。

    为什么没有 concept_trees 表：「一个节点只有一个父」，森林的每个连通分量
    恰好是一棵以根为顶的树，**根节点自己就是那棵树**。
    """
    conn = db.connect()
    try:
        if roots:
            return nodes.list_roots(conn, q=q, page=page, page_size=page_size)
        return nodes.list_all_nodes(conn, q=q, page=page, page_size=page_size)
    finally:
        conn.close()


@app.get("/api/nodes/{node_id}")
def get_node_api(node_id: int) -> dict:
    conn = db.connect()
    try:
        return nodes.get_node(conn, node_id)
    finally:
        conn.close()


@app.get("/api/nodes/{node_id}/parents")
def node_parents_api(node_id: int) -> dict:
    """面包屑：根 → 自己的祖先链（不含自己）。"""
    conn = db.connect()
    try:
        return nodes.node_parents(conn, node_id)
    finally:
        conn.close()


@app.get("/api/entries/{entry_id}/nodes")
def entry_nodes_api(entry_id: int) -> dict:
    """某词条在树上的位置（对称于既有 /api/entries/{id}/links）。

    返回 {"entry_id", "node", "parent", "root", "path"}。
    词条还没有节点行时 node=null——**不报 404**，因为词条确实存在，
    只是还没挂到任何树上。多一次词条主键查询换语义正确。
    """
    conn = db.connect()
    try:
        entries.get_entry(conn, entry_id)  # 不存在 → 404
        return nodes.entry_nodes(conn, entry_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 异常 → HTTP 状态映射（具体异常优先，基类兜底）
# --------------------------------------------------------------------------


@app.exception_handler(nodes.NodeNotFoundError)
async def _node_not_found_handler(request, exc: nodes.NodeNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(nodes.NodeError)
async def _node_error_handler(request, exc: nodes.NodeError) -> JSONResponse:
    """层级树业务校验失败 → 400。

    注意注册顺序无关（Starlette 按 MRO 从具体到基类找处理器），但必须显式注册
    NodeError：它继承自 entries.AppError，若只靠基类兜底也能拿到 400，
    可那样 CycleError 的中文提示会与条目错误混在同一个处理器里，不利排查。
    """
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(practices.PracticeNotFoundError)
async def _practice_not_found_handler(request, exc: practices.PracticeNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(practices.PracticeConflictError)
async def _practice_conflict_handler(request, exc: practices.PracticeConflictError) -> JSONResponse:
    """采纳修正评分时记录已被改动 → 409 而不是 400。

    语义要分开：400 是"你传的参数不对，改了再试"；
    409 是"你看到的分数已经过期"，用户必须重新打开详情确认，
    把它报成 400 会引导用户反复重提同一个已经失效的请求。
    """
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ai.DiscussScoreError)
async def _discuss_score_error_handler(request, exc: ai.DiscussScoreError) -> JSONResponse:
    """修正评分不合法 → 400（用户不能采纳），不是 502（服务故障）。

    它继承自 AIError，会被兜底的 AIError 处理器捞走并报成 502；
    但这是模型的输出不合规、不是接口挂了，用户重试同一个请求毫无意义。
    """
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(ai.AIError)
async def _ai_error_handler(request, exc: ai.AIError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.exception_handler(ai.AINotConfiguredError)
async def _ai_not_configured_handler(
    request, exc: ai.AINotConfiguredError
) -> JSONResponse:
    """未配置密钥 → 400 而不是 502。

    502（Bad Gateway）暗示"上游服务故障"，会引导用户去查网络 / 服务商；
    而这里是本地配置缺失，是用户自己能立刻修好的事。语义要对，否则用户会被
    误导到错误的方向上。消息里直接给出修复路径。
    """
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(articles.ArticleNotFoundError)
async def _article_not_found_handler(request, exc: articles.ArticleNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(articles.ArticleError)
async def _article_error_handler(request, exc: articles.ArticleError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(links.LinkNotFoundError)
async def _link_not_found_handler(request, exc: links.LinkNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(entries.DuplicateEntryError)
async def _duplicate_entry_handler(request, exc: entries.DuplicateEntryError) -> JSONResponse:
    return JSONResponse(
        status_code=409, content={"detail": str(exc), "conflict_id": exc.entry_id}
    )


@app.exception_handler(entries.EntryNotFoundError)
async def _entry_not_found_handler(request, exc: entries.EntryNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(entries.AppError)
async def _app_error_handler(request, exc: entries.AppError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(sqlite3.Error)
async def _sqlite_error_handler(request, exc: sqlite3.Error) -> JSONResponse:
    """数据库错误 → 500，但对外**不回显 exc 原文**（B9 加固）。

    sqlite3 的错误文本里可能带 SQL 片段、文件路径、``no such table``
    这类内部结构信息 —— 原样回给访客等于免费的信息泄露。
    详细错误（含 traceback 与请求路径）记 error 日志，运维排查照样看得见。
    """
    logger.error("数据库错误：path=%s", request.url.path, exc_info=True)
    return JSONResponse(status_code=500,
                        content={"detail": "服务器内部错误，请稍后重试"})


@app.exception_handler(OverflowError)
async def _overflow_error_handler(request, exc: OverflowError) -> JSONResponse:
    """数值运算溢出 → 404（B8 加固）。

    什么场景会走到这里：访客手搓的巨型 id 经过某层运算时超出底层
    C 库的取值范围（时间换算、哈希取模之类）。它既不是「参数格式错」
    （那是 422）也不是「服务故障」（那是 500）—— 绝大多数情况
    等价于「这个资源不存在」。
    ★ 不回显 exc 原文 ★：溢出信息里常带内部数值范围细节，
    对访客只有一个「没找到」是有用信息；细节进日志。
    """
    logger.warning("请求触发 OverflowError：path=%s", request.url.path)
    return JSONResponse(status_code=404,
                        content={"detail": "资源不存在或 ID 不合法"})


app.mount("/", sharecode_static.ShareCodeStaticFiles(
    directory=str(WEB_DIR), html=True), name="web")
