"""应用配置与本地设置持久化。

数据统一放在 %APPDATA%/EnPractice（可用环境变量 ENPRACTICE_HOME 覆盖），
避免写入程序安装目录。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_NAME = "EnPractice"

# 密钥在返回给前端时的脱敏占位值；前端原样回传表示"不修改"
API_KEY_MASK = "***"

# 合法主题 id 白名单。settings.json 用户可手改，读取时不在表内一律回落 DEFAULT_THEME，
# 且绝不抛异常 —— 主题坏掉不该让整个应用读不出设置。
THEME_IDS = (
    "reading",
    "starry",
    "sunflower",
    "vesper",
    "mona",
    "vampire",
    "marvel",
    "silver",
)
DEFAULT_THEME = "reading"

DEFAULT_SETTINGS = {
    "ai": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key": "",
        "timeout": 60,
    },
    "review": {
        "time": "20:00",
        "mode": "interval",  # interval = 间隔重复 | date_order = 按添加日期排序
        "enabled": True,
    },
    "ui": {
        "language": "zh-CN",
        "ui_theme": DEFAULT_THEME,
    },
}


def normalize_theme(value) -> str:
    """把任意输入收敛成合法主题 id；不合法返回 DEFAULT_THEME，永不抛异常。"""
    if isinstance(value, str) and value in THEME_IDS:
        return value
    return DEFAULT_THEME


def app_dir() -> Path:
    override = os.environ.get("ENPRACTICE_HOME")
    path = Path(override) if override else Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    # ★ 云端：按当前请求的访客分流到 tenants/<id>/data.db。
    #   这是「55 处 db.connect() 零改动」的关键 —— db.connect() 只认 db_path()，
    #   让 db_path() 变成访客感知，全站自动隔离。
    #   ★ 桌面分支逐字未改 ★（ENPRACTICE_MODE 未设 cloud 时 tid 恒为 None）
    from . import cloud
    tid = cloud.current_tenant.get()
    if cloud.cloud_mode() and cloud.is_valid_id(tid or ""):
        d = cloud.tenant_dir(tid)
        d.mkdir(parents=True, exist_ok=True)
        return d / "data.db"
    return app_dir() / "data.db"


def settings_path() -> Path:
    # ★ 云端：每访客一份 settings.json（桌面分支逐字未改）
    from . import cloud
    tid = cloud.current_tenant.get()
    if cloud.cloud_mode() and cloud.is_valid_id(tid or ""):
        d = cloud.tenant_dir(tid)
        d.mkdir(parents=True, exist_ok=True)
        return d / "settings.json"
    return app_dir() / "settings.json"


def load_settings() -> dict:
    path = settings_path()
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    # 顶层必须是对象。settings.json 用户可手改，写成 [1,2,3] 或 "abc" 时
    # 下面 .items() 会抛 AttributeError，导致整个应用读不出设置。
    if not isinstance(data, dict):
        data = {}
    merged = {k: dict(v) for k, v in DEFAULT_SETTINGS.items()}
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    # 主题值用户手可改，读时归一化。注意 merged["ui"] 可能被手改成非 dict，
    # 所以这里不假设它还是 dict，而是当作「缺失」处理并补回默认。
    ui = merged.get("ui")
    if not isinstance(ui, dict):
        ui = dict(DEFAULT_SETTINGS["ui"])
        merged["ui"] = ui
    ui["ui_theme"] = normalize_theme(ui.get("ui_theme"))
    return merged


class SettingsWriteError(RuntimeError):
    """设置写盘失败（磁盘只读、空间不足、文件被占用等）。"""


def save_settings(patch: dict) -> dict:
    """按分组合并写回，保留未提及的键；未知顶层键直接丢弃。"""
    current = load_settings()
    for key, value in patch.items():
        if key not in DEFAULT_SETTINGS:
            continue
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            current[key].update(value)
        else:
            current[key] = value
    # 写前再关一道：非法主题 id 存不进盘，落盘即已是合法值。
    ui = current.get("ui")
    if not isinstance(ui, dict):
        ui = dict(DEFAULT_SETTINGS["ui"])
        current["ui"] = ui
    ui["ui_theme"] = normalize_theme(ui.get("ui_theme"))
    # ★ 云端：访客自带的 AI 密钥一律**不落盘** ★
    #   密钥的唯一存处是访客浏览器的 localStorage，请求时经请求头带过来。
    #   这里抹掉是为了「就算前端还没改完、访客已经发了带 key 的 PUT」，
    #   密钥也不会被写进服务端的 settings.json。
    # ★ 云端：base_url / model 一律**覆盖成平台默认值** ★
    #   即使访客 PUT {"ai":{"base_url":"http://169.254.169.254/"}}，
    #   落盘的也不是他写的那个值 —— 磁盘上不留任何"访客指定的出网地址"。
    #   （真正的强制点在 ai._chat()：访客自填的地址走请求头通道，
    #     发请求前必过 app/egress.py 的 SSRF 校验；这里是**第二道**：
    #     让 PUT 进来的地址永远进不了磁盘。）
    #   桌面分支不执行（cloud_mode() 为 False），行为逐字不变。
    from . import cloud
    if cloud.cloud_mode():
        ai_cfg = current.get("ai")
        if isinstance(ai_cfg, dict):
            ai_cfg["api_key"] = ""
            ai_cfg["base_url"] = cloud.ai_base_url()
            ai_cfg["model"] = cloud.ai_model()
    _atomic_write(settings_path(), current)
    return current


def _atomic_write(path: Path, data: dict) -> None:
    """先写临时文件再原子替换，避免中断时留下截断的 JSON。"""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise SettingsWriteError(f"设置写入失败：{path}（{exc}）") from exc


def get_api_key() -> str:
    """取本次调用该用的 AI 密钥。**禁止硬编码任何密钥。**

    ★ 桌面两级（逐字未改） ★
        1) 环境变量 `OPENAI_API_KEY`
        2) 本地 `settings.json` 的 `ai.api_key`

    ★ 云端三级 ★
        1) 请求头 `X-Enp-Api-Key`（访客自带，最高优先级）
        2) ~~环境变量 `OPENAI_API_KEY`~~ —— **云端这一级被刻意跳过**
        3) ~~`settings.json`~~ —— 同上，云端不读

    ★ 为什么云端要把后两级整个跳过，而不是「配了也生效」 ★
    云端部署一旦配了 `OPENAI_API_KEY`，这台机器上**所有访客共用同一把
    平台密钥**：既是白嫖（任何访客都能花平台的额度），也是串号
    （用量算到平台账上）。所以云端分支在**读 env 之前**就 return ——
    不是「读了不用」，是根本不读，代码里也不留残骸。
    云端唯一合法的密钥来源就是访客自己的请求头，那把key 只活在
    访客浏览器的 localStorage 里，不进服务端磁盘。
    真要给平台配一把 key，必须改代码（走一次有评审记录的显式改动）。

    ★ 桌面分支逐字等价 ★
    `cloud_mode()` 为 False 时，下面两行与改造前完全一致。
    """
    from . import cloud
    header_key = (cloud.request_api_key.get() or "").strip()
    if header_key:
        return header_key
    if cloud.cloud_mode():
        return ""
    return os.environ.get("OPENAI_API_KEY") or load_settings()["ai"].get("api_key", "")
