"""云端（网页版）启动入口。

★ 为什么不复用 run.py ★
run.py 里 import 了 pywebview —— Linux 上装不上（还要 GTK 依赖），
云端部署装不上就起不来。所以这里是**平行的入口**，桌面代码一行不改。

★ 三件事，顺序不能反 ★
1. 先 `_bootstrap_env()` 设好环境变量
2. 再 import app.*（因为 config.app_dir() / cloud.cloud_mode() 都在 import 期被读）
3. 最后 uvicorn.run(...)

★ workers=1 是硬性的 ★
访客建库用的是**进程内**的 per-visitor threading.Lock。多 worker 会把锁打散成
跨进程竞态（同一个新访客被两个进程同时建库 → ALTER TABLE duplicate column）。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


def _seed_fingerprint() -> str:
    """演示语料 `app/seed_data.py` 的**字节数 + md5**。

    ★ 为什么印在启动横幅上 ★
    云端首屏的内容全部来自这份种子。部署方在日志里一眼就能确认
    「跑的是哪一版语料」——否则「首屏文章对不对」这类问题永远在
    靠人肉比对才能定位。读不到文件时返回占位，不让启动失败。

    ★ 为什么先打字节数 ★
    只打 md5 的话，部署方拿到一个 md5 还得自己去算一遍才知道对不对。
    字节数是肉眼可读的：两个版本只要语料条数动过，字节数几乎必然变。
    md5 相同则字节数必然相同，反过来不成立 —— 所以字节数在前（快速判
    「是不是同一版」），md5 在后（精确确认）。

    ★★ 这不是安全校验，别当防篡改用 ★★
    md5 在这里只当**版本指纹**：回答「我跑的是哪一版语料」。
    它**不防篡改** —— 攻击者改了 seed_data.py，md5 会跟着一起变，
    横幅照样打出一个「自洽」的指纹。真要防篡改要走代码签名 / 哈希锁定，
    那是另一件事，不在本次范围内。
    写清楚这一点，是为了不让人误以为「日志里有 md5 = 语料被保护了」。
    """
    seed_file = Path(__file__).resolve().parent / "app" / "seed_data.py"
    try:
        data = seed_file.read_bytes()
    except OSError:
        return "(读取失败)"
    return f"{len(data)} 字节  md5={hashlib.md5(data).hexdigest()}"


def _bootstrap_env() -> None:
    """设云端环境变量。**必须在 import app.* 之前调用。**"""
    os.environ.setdefault("ENPRACTICE_MODE", "cloud")
    # ★ 分享令牌兜底 ★
    #   部署网关用 sharecode 做鉴权并**消费**该参数（公网实测：后端收不到，
    #   直连却收得到），于是后端无法给 HTML/CSS 里的资源引用注入令牌 ⇒
    #   浏览器请求裸名资源被网关 403 ⇒ 白屏。
    #   取码顺序见 app/sharecode_mw.py：query → 网关透传头 → 本环境变量。
    #   这个环境变量由**本进程自己**设置（不需要平台支持），
    #   平台若改了分享码，改这一行重新部署即可；
    #   也可由部署环境显式设置 ENPRACTICE_SHARECODE 覆盖。
    os.environ.setdefault("ENPRACTICE_SHARECODE", "IKDSXDUEGT")
    # ★ 这里**不设** ENPRACTICE_WEB_SEARCH ★
    #   云端「联网搜例句」是**代码级**关闭的（app/cloud.py 的
    #   web_search_enabled() 在云端恒返回 False），环境变量一律无效。
    #   所以这里连 setdefault 都不写 —— 写一个「设了也没用」的开关，
    #   等于在下一个人心里埋下「我改改 env 就能开」的雷。
    #   真要开，必须改代码（走一次有留痕记录的显式改动）。
    if not os.environ.get("ENPRACTICE_HOME"):
        # 优先用可写的挂载卷（部署平台的惯例是 /data）；都不行才退到临时目录。
        for cand in ("/data",
                     os.path.join(os.path.expanduser("~"), ".enpractice")):
            try:
                if os.path.isdir(cand) and os.access(cand, os.W_OK):
                    os.environ["ENPRACTICE_HOME"] = cand
                    break
            except OSError:
                continue
        else:
            os.environ["ENPRACTICE_HOME"] = tempfile.mkdtemp(prefix="enp-")


_bootstrap_env()

import uvicorn  # noqa: E402

from app import cloud  # noqa: E402
from app.main import app  # noqa: E402


def main() -> None:
    port = int(os.environ.get("PORT") or 8000)
    print(f"[cloud] 模式={cloud.cloud_mode()} "
          f"联网搜例句={cloud.web_search_enabled()} "
          f"按网址取句={cloud.network_fetch_allowed()} "
          f"数据根={os.environ.get('ENPRACTICE_HOME')} 端口={port}")
    print(f"[cloud] 语料版本指纹 = {_seed_fingerprint()}"
          f"  (app/seed_data.py，仅用于确认跑的是哪一版语料，非防篡改校验)")
    # ★ 单 worker（见模块 docstring）；proxy_headers 让平台反代传的
    #   X-Forwarded-* 生效，日志里的客户端 IP 才是真实访客。
    # ★ access_log=False（B13 加固）★
    #   uvicorn 的 access log 逐请求打印「方法 + 完整 URL（含 query）+
    #   UA + 状态码」。云端场景下这是净负担：
    #     · query 里会带上访客的搜索词 / 翻译内容 —— 访客数据进日志，
    #       留存越久泄露面越大，而我们并不需要它们；
    #     · 日志量随请求数线性涨，很快把真正的 warning/error 冲掉，
    #       排障时反而更难找。
    #   需要访问审计时走平台网关侧的日志（那才是该管这事的一层），
    #   应用自己的 logger 已覆盖排障所需。
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1,
                log_level="info", proxy_headers=True, access_log=False)


if __name__ == "__main__":
    main()