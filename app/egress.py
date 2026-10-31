"""出网白名单：访客自填「AI 接口地址」的 SSRF 防护。

★ 为什么需要这个模块 ★
云端版允许访客自己填 AI「接口地址」（桌面版本来就能填）。
表面看只是"多两个输入框"，实际风险是**服务器会代访客向那个地址发请求**：
访客把地址填成 `https://169.254.169.254/`（云元数据接口）或某个内网地址，
我们的服务器就替他把内网打了一遍 —— 这是教科书式的 SSRF。

★ 两道防线，缺一不可 ★
① **校验**（:func:`validate_base_url`）
   只允许 `https` + 443 端口；把域名解析出来，**每一个** IP 都必须是公网地址。
   注意是"每一个"：只查第一个 A 记录的话，
   `evil.com → 1.2.3.4, 127.0.0.1` 这种多记录就能混过去。

② **钉住**（:func:`post_json`）
   校验时解析到的 IP，和真正发请求时用的 IP，**必须是同一个**。
   否则 DNS rebinding 就绕过了①：第一次解析给公网 IP（校验通过），
   第二次解析给 `127.0.0.1`（真请求打内网）。
   实现方式：请求 URL 里用 **IP**，但 TLS 的 SNI 与证书校验仍用**原域名**，
   并手动带 `Host: 原域名` 让服务端正确路由。

②' **降级路径的集合比对**（:func:`revalidate`，B5 加固）
   钉不住、只能按域名请求时，请求发出前**重新解析一次**，把解析结果
   与校验时记下的 IP 集合逐项比对：**任何一个 IP 变了就整笔拒绝** ——
   哪怕新 IP 也全是公网地址。公网→公网的变化同样说明"解析结果被做了手脚"，
   此时放行 = 把没校验过的目的地放进请求里。

★ 残余风险（写清楚，不装作没有）★
   · **代理降级路径没有钉住**。实测：本机环境设了 MITM 代理时，
     "连 IP + 按域名做 SNI"会被代理打断（`SSL: UNEXPECTED_EOF_WHILE_READING`），
     而"按域名直连"与"按域名走代理"都正常。
     ⇒ 代码里优先走**钉住直连**；只有钉住失败**且**环境确实配了代理时，
     才降级到"按域名请求"，并在降级**之前重新解析 + 重新校验**一次
     （把 rebinding 窗口从"整个请求时长"压到"两次解析之间"，
     但它没有归零 —— 这一点不装作没有）。
   · 目标服务自身的漏洞不在本模块职责内。

★ 桌面模式完全不经过这里 ★
桌面版的接口地址是**本机用户自己**填的（他填 `127.0.0.1` 也没有别人受害），
所以桌面路径继续走 `app/ai.py` 里原来的 `requests.post`，逐字不变。
本模块只在**云端 + 访客自填地址**时被调用。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: 只允许 https。http 明文传 API Key，且内网探测无需证书，收益为零风险无穷。
ALLOWED_SCHEMES = ("https",)

#: 只允许 443。用我们的服务器扫公网其它端口的收益（端口扫描）远大于
#: "某个自建兼容服务恰好用非标端口"的收益 —— 主流厂商全是 443。
ALLOWED_PORTS = (443,)

#: 接口地址长度上限。防止超长串把日志/头部撑爆。
MAX_URL_LENGTH = 300

#: 模型名长度上限（拼进 JSON body，不影响目的地，但长度仍要有限）。
MAX_MODEL_LENGTH = 128

#: IPv4 级 NAT（运营商网段）。Python 的 ``is_private`` 判它为公网，
#: 但它在云厂商内部网段里，必须单独排除。
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")

#: NAT64 前缀：IPv4 被藏在 IPv6 里。
_NAT64_V6 = ipaddress.ip_network("64:ff9b::/96")


class EgressError(ValueError):
    """接口地址不被允许（消息已是中文，可直接展示给访客）。"""


@dataclass(frozen=True)
class Target:
    """一个校验通过的出网目标。

    :attr:`ip` 是**钉住**用的实际连接地址；:attr:`host` 是原域名，
    用于 TLS 的 SNI、证书校验与 ``Host`` 头。
    :attr:`base_path` 是接口地址里的路径前缀（如 ``/api/v3``），
    实际请求路径 = ``base_path + 业务路径``。
    :attr:`ips` 是校验时解析出的**全部** IP（B5 加固）：
    降级路径请求前重解析，结果必须与它逐项相同，否则整笔拒绝
    （见 :func:`revalidate`）。
    """

    host: str
    port: int
    ip: str
    base_path: str
    ips: tuple[str, ...] = ()


def is_public_ip(ip_text: str) -> bool:
    """这个 IP 是否是**公网可路由**地址。

    判定要覆盖 IPv6 里内嵌 IPv4 的写法，否则 ``::ffff:127.0.0.1``、
    ``2002:7f00:1::``（6to4）、``2001::`` （Teredo）会被当成公网地址放行。
    """
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False

    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return is_public_ip(str(mapped))
        sixtofour = ip.sixtofour
        if sixtofour is not None:
            return is_public_ip(str(sixtofour))
        teredo = ip.teredo
        if teredo is not None:
            return is_public_ip(str(teredo[1]))
        if ip in _NAT64_V6:
            return is_public_ip(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))

    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_V4:
        return False

    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def validate_base_url(raw: str) -> Target:
    """校验访客填的「接口地址」，返回可安全使用的目标。

    校验通过 ⇒ 请求无论打到哪个 IP 都不会落到内网 / 云元数据上。
    校验失败 ⇒ 抛 :class:`EgressError`（消息可直接展示）。

    ★ 为什么不查"域名在不在白名单" ★
    白名单会把访客挡在自己的中转站 / 自建兼容服务外面 —— 那是桌面版本来
    就有的能力，云端版没理由不给。白名单能挡的，IP 校验都能挡；
    IP 校验挡得住的（任意公网服务），恰好是白名单挡不住的。
    """
    text = (raw or "").strip()
    if not text:
        raise EgressError("接口地址不能为空")
    if len(text) > MAX_URL_LENGTH:
        raise EgressError(f"接口地址过长（上限 {MAX_URL_LENGTH} 字符）")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise EgressError("接口地址里不能有空格或控制字符")

    parts = urlsplit(text)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise EgressError("接口地址必须以 https:// 开头")

    if parts.username or parts.password:
        raise EgressError("接口地址里不能带用户名或密码")
    if parts.query or parts.fragment:
        raise EgressError("接口地址里不能有 ? 查询参数或 # 片段")

    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise EgressError("接口地址缺少主机名")

    try:
        port = parts.port or 443
    except ValueError:
        raise EgressError("接口地址的端口不合法") from None
    if port not in ALLOWED_PORTS:
        raise EgressError("接口地址只支持 443 端口")

    path = (parts.path or "").rstrip("/")
    if ".." in path.split("/"):
        raise EgressError("接口地址的路径里不能有 ..")

    # ★ 主机名本身就是 IP 字面量 ⇒ **不查 DNS**，直接拿这个 IP 判 ★
    #   ① 少一次没必要的 DNS 查询；
    #   ② 更重要：不依赖"解析器会把 IP 字面量原样返回"这个隐含约定 ——
    #      公网字面量照常放行、内网字面量照常拒绝，判定权在我们自己的规则上。
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public_ip(str(literal)):
            raise EgressError(
                f"接口地址指向内网或保留地址（{host}），"
                "云端版不允许服务器代访客访问内网")
        return Target(host=host, port=port, ip=str(literal),
                      ips=(str(literal),), base_path=path)

    ips = _resolve_ips(host, port)
    if not ips:
        raise EgressError(f"接口地址的主机名解析失败：{host}")

    for ip in ips:
        if not is_public_ip(ip):
            # ★ 对外消息不含 IP（B4 加固）★
            #   回显解析出的 IP = 给访客一个 DNS 解析 oracle：
            #   填任意域名，看报错里是哪个 IP / 还是"解析失败"，
            #   就能拿我们的服务器探测内网资产与域名归属。
            #   IP 只进服务端日志，运维排查照样看得见。
            logger.warning(
                "访客自填的接口地址解析到非公网 IP：host=%s ip=%s", host, ip)
            raise EgressError(
                "接口地址不被允许：请填写公网可访问的 https 接口地址")

    return Target(host=host, port=port, ip=ips[0], ips=tuple(ips),
                  base_path=path)


def validate_model(raw: str) -> str:
    """校验「模型名称」。

    模型名只拼进 JSON body（不像 URL 那样能改目的地），风险低，
    但仍要限长、限字符 —— 它会被写进日志与错误提示。
    """
    text = (raw or "").strip()
    if not text:
        raise EgressError("模型名称不能为空")
    if len(text) > MAX_MODEL_LENGTH:
        raise EgressError(f"模型名称过长（上限 {MAX_MODEL_LENGTH} 字符）")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise EgressError("模型名称里不能有控制字符")
    return text


def _resolve_ips(host: str, port: int) -> list[str]:
    """解析主机名，返回**全部** A / AAAA 记录（去重，保持顺序）。"""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        return []
    ips: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in ips:
            ips.append(addr)
    return ips


def _proxy_configured() -> bool:
    """环境里是否设了 HTTP 代理（设了就说明出网可能必须经代理）。"""
    import os
    return bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))


def _urllib3_supports_pinning() -> bool:
    """当前 urllib3 是否支持"连 IP 但按域名做 SNI/证书校验"。

    urllib3 1.x 的 HTTPSConnection 不认 ``server_hostname``，
    传进去会 TypeError —— 那时宁可不钉住（退回域名路径），也不要崩。
    """
    try:
        import urllib3
    except ImportError:
        return False
    version = getattr(urllib3, "__version__", "0")
    try:
        major = int(str(version).split(".")[0])
    except ValueError:
        return False
    return major >= 2


def revalidate(target: Target) -> None:
    """**重新**解析目标主机并再校验一次所有 IP 都是公网地址。

    降级路径（按域名请求）专用：那一路没有钉住，DNS 由对端在请求时刻解析，
    所以在请求发出前再解析一次、再判一次，把 rebinding 窗口压到最小。
    失败 ⇒ 抛 :class:`EgressError`（绝不带着可疑地址发请求）。

    ★ B5 加固：集合比对 ★
    重解析结果必须与校验时记下的 ``target.ips`` **逐项相同**。
    注意判据是"变了就拒"，而不是"新的也全是公网就放行" ——
    公网→公网的变化同样意味着解析结果被做了手脚（例如把请求引向
    另一台我们没校验过的服务器），此时放行等于把未校验目的地写进请求。
    """
    ips = _resolve_ips(target.host, target.port)
    if not ips:
        raise EgressError(f"接口地址的主机名解析失败：{target.host}")
    if set(ips) != set(target.ips):
        logger.warning(
            "访客自填的接口地址（降级路径重校验）解析结果发生变化："
            "host=%s 校验时=%s 重解析=%s",
            target.host, sorted(target.ips), sorted(ips))
        raise EgressError("域名解析结果发生变化，本次请求已拒绝")
    for ip in ips:
        if not is_public_ip(ip):
            # 同 validate_base_url：对外消息不含 IP（B4 加固），只进日志。
            logger.warning(
                "访客自填的接口地址（降级路径重校验）解析到非公网 IP："
                "host=%s ip=%s", target.host, ip)
            raise EgressError(
                "接口地址不被允许：请填写公网可访问的 https 接口地址")


def post_json(target: Target, url_path: str, payload: dict,
              headers: dict, timeout: int):
    """向已校验的目标发一个 JSON POST，返回 ``requests.Response``。

    ★ 返回 Response 而不是 (状态码, 字节) ★
    调用方（`app/ai.py::_chat`）本来就在用 `requests.Response` 的
    `.status_code` / `.text` / `.json()`，返回同一种对象可以让它
    **一条出网路径、一套错误处理**，不必为云端再写一遍。

    ★ 钉住的实现 ★
    URL 里用 **IP**（避免请求时重新解析域名 → 堵住 rebinding），
    但通过 urllib3 的 ``server_hostname`` + ``assert_hostname`` 让 TLS
    仍按**原域名**做 SNI 与证书校验，并手动带 ``Host: 原域名``。
    （urllib3 见到 headers 里已有 host 就不会再自己塞一个，
      所以不会出现两个 Host 头。）

    ★ 为什么先试直连、失败才降级 ★
    实测：设了 MITM 代理时，"连 IP + 按域名做 SNI"会被代理打断；
    而"按域名直连"和"按域名走代理"都正常。
    所以顺序是：钉住直连 →（仅当环境配了代理）重新校验后按域名请求。
    降级那一路**没有钉住**，残余风险已在模块 docstring 里写明。

    ★ 永不跟随重定向 ★
    3xx 直接当异常处理：跟随重定向等于把"校验过的目标"换成
    "没校验过的目标"，是 SSRF 的经典绕过手法。

    ★ 响应体流式返回（B14 加固）★
    两处请求都带 ``stream=True``：响应体不预读，交由调用方
    （`app/ai.py::_read_capped`）带 4MB 上限地读 —— 不流式的话，
    requests 会把**整个** body 拉进内存，异常对端一个无限流响应
    就能把进程内存吃光。
    """
    import requests

    # ★ SNI / 证书校验用**裸主机名**；HTTP Host 头用**带方括号的 IPv6 字面量** ★
    #   （`Host: [::1]:443` 才是合法写法；`Host: ::1` 会被服务端解析错。）
    sni_name = target.host
    host_header = sni_name
    if ":" in host_header:
        host_header = f"[{host_header}]"
    ip_host = f"[{target.ip}]" if ":" in target.ip else target.ip
    full_path = f"{target.base_path}{url_path}"
    pinned_url = f"https://{ip_host}{full_path}"
    domain_url = f"https://{host_header}{full_path}"

    if _urllib3_supports_pinning():
        from requests.adapters import HTTPAdapter

        class _PinnedAdapter(HTTPAdapter):
            """把连接钉在已校验的 IP 上，但 TLS 仍按原域名校验。"""

            def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
                pool_kwargs["server_hostname"] = sni_name
                pool_kwargs["assert_hostname"] = sni_name
                return super().init_poolmanager(connections, maxsize, block=block,
                                               **pool_kwargs)

        session = requests.Session()
        session.trust_env = False          # 直连，不走环境里的代理
        session.mount("https://", _PinnedAdapter())
        request_headers = dict(headers)
        request_headers["Host"] = host_header
        try:
            return session.post(pinned_url, headers=request_headers, json=payload,
                                timeout=timeout, allow_redirects=False,
                                stream=True)
        except requests.RequestException as exc:
            if not _proxy_configured():
                raise
            logger.warning("钉住直连失败（出网疑似必须经代理），"
                           "重新校验后降级为按域名请求：%s", exc)
        finally:
            session.close()

    # 降级路径：没有钉住 ⇒ 请求前必须重新解析 + 重新校验。
    revalidate(target)
    return requests.post(domain_url, headers=dict(headers), json=payload,
                         timeout=timeout, allow_redirects=False, stream=True)

