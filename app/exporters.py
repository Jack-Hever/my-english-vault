# -*- coding: utf-8 -*-
"""导出器：把库里的数据落成可直接用的文件（Markdown / Word）。

★ 为什么单独一个模块 ★
两个导出共用同一套「文件名清洗 + Content-Disposition 头」规则。这套规则
最容易在两处各写一遍、然后悄悄漂移（一个清洗了另一个没清洗，于是同一个
标题在 md 和 docx 里落出不同文件名）。放一起就只有一份实现。

★ 都是纯函数式生成器 ★
输入是「已经取好的数据」，输出是字节。取数据的活儿留给路由（它才管连接
与租户隔离），本模块不碰数据库 —— 于是这两个函数能被单独测，不用起服务。
"""

from __future__ import annotations

import io
import os
import re
import urllib.parse


class ExportError(Exception):
    """导出失败（如缺少 python-docx）。路由层转成 HTTP 错误。"""


# Windows/Word 文件名里非法的字符（\ / : * ? " < > | + 控制字符）。
# 另加结尾的点与空格：Explorer 会静默截掉它们，于是「看起来存不进去」。
_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_TRAILING_DOTS_SPACES = re.compile(r"[. ]+$")

# Markdown 里会改变结构的字符。只转义这些，不动普通标点 ——
# 释义里的逗号句号照常显示，转义过头会让正文难读。
_MD_STRUCTURAL = re.compile(r"([\\`*_\[\]<>])")

#: 文件名（不含扩展名）最长保留长度。Windows 路径段上限 255，
#: 文章标题可能很长，留出扩展名与目录的余量。
_MAX_STEM = 120


def safe_filename(name: str, fallback: str = "export") -> str:
    """把任意文本清洗成 Windows/Word 都能接受的文件名（不含扩展名）。"""
    cleaned = _ILLEGAL_FILENAME.sub("_", (name or "").strip())
    cleaned = _TRAILING_DOTS_SPACES.sub("", cleaned)
    if len(cleaned) > _MAX_STEM:
        cleaned = cleaned[:_MAX_STEM]
    return cleaned or fallback


def content_disposition(filename: str) -> str:
    """生成 Content-Disposition 响应头。

    ★ 为什么同时给两个 filename ★
    中文文件名直接写进 filename="..." 会被浏览器按 latin-1 解释 → 存成乱码名。
    所以同时给：① 一个纯 ASCII 的兜底名（老浏览器 / 直接存 URL 的场景仍能
    落盘）；② RFC 5987 的 filename*（现代浏览器优先用它，中文名正确）。
    """
    stem, ext = os.path.splitext(filename)
    ascii_stem = stem.encode("ascii", "ignore").decode("ascii").strip() or "export"
    ascii_name = (ascii_stem + ext).replace('"', "_")
    quoted = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


def _md_escape(text: str) -> str:
    """转义会破坏 Markdown 结构的字符（见 _MD_STRUCTURAL）。"""
    if not text:
        return ""
    return _MD_STRUCTURAL.sub(r"\\\1", str(text))


def _rating_text(value) -> str:
    """把评分渲染成可读文本。

    ★ 未评分 ≠ 0 分 ★
    库里 importance/usefulness 为 NULL 表示「没评过」，0 表示「评了 0 星」，
    是两件不同的事（词条详情页也这么区分），所以这里不能把 0 当成未评分。
    """
    if value is None:
        return "未评分"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "未评分"
    return f"{num:g} / 5"


def entries_markdown(payload: dict) -> str:
    """把整个词汇库渲染成 Markdown 文本。

    payload 是 entries.export_entries() 的返回结构（不传筛选条件 = 全量）。
    """
    items = payload.get("entries") or []
    lines = [
        "# 我的英文库 · 词汇表",
        "",
        f"- 导出时间：{payload.get('exported_at') or ''}",
        f"- 词条数：{len(items)}",
        "",
        "---",
        "",
    ]
    if not items:
        lines.append("_词汇库还是空的。_")
        lines.append("")
        return "\n".join(lines)

    for item in items:
        lines.append(f"## {_md_escape(item.get('term') or '')}")
        lines.append("")
        pos = (item.get("pos") or "").strip()
        if pos:
            lines.append(f"- **词性**：{_md_escape(pos)}")
        meaning = (item.get("meaning_zh") or "").strip()
        if meaning:
            lines.append(f"- **释义**：{_md_escape(meaning)}")
        usage = (item.get("usage") or "").strip()
        if usage:
            lines.append(f"- **例句**：{_md_escape(usage)}")
        lines.append(f"- **重要性**：{_rating_text(item.get('importance'))}")
        lines.append(f"- **实用性**：{_rating_text(item.get('usefulness'))}")
        created = (item.get("created_at") or "").strip()
        if created:
            lines.append(f"- **创建时间**：{_md_escape(created)}")
        lines.append("")
    return "\n".join(lines)


def article_docx(article: dict, sentences: list[dict]) -> bytes:
    """把一篇文章写成 Word 文档（返回 .docx 字节）。

    ★ 为什么不在模块顶部 import docx ★
    缺库时若在 import 期炸掉，整个 app 起不来（连跟导出无关的功能都没了）。
    延迟到调用点 import，缺库只影响这一个功能。
    """
    try:
        from docx import Document
        from docx.shared import Cm
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底
        raise ExportError("未安装 python-docx，无法导出 Word 文件") from exc

    doc = Document()
    title = (article.get("title") or "").strip() or "未命名文章"
    doc.add_heading(title, level=1)
    title_zh = (article.get("title_zh") or "").strip()
    if title_zh:
        doc.add_heading(title_zh, level=2)
    extra = (article.get("extra_info") or "").strip()
    if extra:
        para = doc.add_paragraph()
        para.add_run("其他信息：").bold = True
        para.add_run(extra)

    doc.add_heading("逐句对照", level=2)
    if not sentences:
        doc.add_paragraph("（这篇文章没有切出有效句子。）")
    for index, sentence in enumerate(sentences, 1):
        para = doc.add_paragraph()
        para.add_run(f"{index}. ").bold = True
        para.add_run((sentence.get("text") or "").strip())
        zh = (sentence.get("text_zh") or "").strip()
        if zh:
            zh_para = doc.add_paragraph(zh)
            # 译文缩进一级：肉眼就能分清哪行是英文、哪行是中文
            zh_para.paragraph_format.left_indent = Cm(0.8)

    content_zh = (article.get("content_zh") or "").strip()
    if content_zh:
        doc.add_heading("整篇中文译文", level=2)
        doc.add_paragraph(content_zh)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
