"""发往 QQ 的文字处理：超长切分、去掉 Markdown 符号。纯函数，不做网络请求。"""

from __future__ import annotations

import re

# 单条消息的长度上限。官方文档没写明，按其他实现的经验取 4000 字；超过就切成多条
QQ_TEXT_LIMIT = 4000

# 从粗到细的切分点：段落 → 行 → 句末 → 分句 → 逗号 → 空格；都切不开就硬切
_SEPARATORS = ("\n\n", "\n", "。", "！", "？", "!", "?", "；", ";", "，", ",", " ")


def split_text(text: str, limit: int) -> list[str]:
    """把文字切成每段不超过 limit 字的若干段，尽量在段落、句子边界切，并尽量装满每段。"""
    limit = max(1, limit)
    chunks = (c.strip() for c in _split(text.strip(), limit, 0))
    return [c for c in chunks if c]


def _split(text: str, limit: int, level: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    if level >= len(_SEPARATORS):
        return [text[i : i + limit] for i in range(0, len(text), limit)]
    sep = _SEPARATORS[level]
    pieces = text.split(sep)
    pieces = [p + sep for p in pieces[:-1]] + [pieces[-1]]
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if len(current) + len(piece) <= limit:
            current += piece
            continue
        if current:
            chunks.append(current)
        if len(piece) <= limit:
            current = piece
        else:
            *full, current = _split(piece, limit, level + 1)
            chunks.extend(full)
    if current:
        chunks.append(current)
    return chunks


_FENCE = re.compile(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```[ \t]*$", re.M | re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_HR = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$\n?", re.M)
_TABLE_RULE = re.compile(r"^[ \t]*\|?(?:[ \t]*:?-{3,}:?[ \t]*\|)+(?:[ \t]*:?-{3,}:?[ \t]*)?\|?[ \t]*$\n?", re.M)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$", re.M)
_QUOTE = re.compile(r"^[ \t]*>[ \t]?", re.M)
_BULLET = re.compile(r"^([ \t]*)[-*+][ \t]+", re.M)
_IMAGE = re.compile(r"!\[([^\]\n]*)\]\([^)\s]*(?:[ \t]+\"[^\"\n]*\")?\)")
_LINK = re.compile(r"\[([^\]\n]+)\]\((\S+?)(?:[ \t]+\"[^\"\n]*\")?\)")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.S)
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.S)
_PLACEHOLDER = re.compile("\x00(\\d+)\x00")


def strip_markdown(text: str) -> str:
    """去掉常见 Markdown 标记，变成在 QQ 普通文字消息里好读的样子。

    只处理成对、不易误伤的写法：单个 * 不处理，以免弄坏 (*^▽^*) 这类颜文字；代码里的内容原样保留。
    """
    kept: list[str] = []

    def keep(content: str) -> str:
        kept.append(content)
        return f"\x00{len(kept) - 1}\x00"

    text = _FENCE.sub(lambda m: keep(m.group(1).rstrip("\n")), text)
    text = _INLINE_CODE.sub(lambda m: keep(m.group(1)), text)
    text = _HR.sub("", text)
    text = _TABLE_RULE.sub("", text)
    text = _HEADING.sub(r"\1", text)
    text = _QUOTE.sub("", text)
    text = _BULLET.sub(r"\1• ", text)
    text = _IMAGE.sub(lambda m: m.group(1) or "[图片]", text)
    text = _LINK.sub(lambda m: m.group(2) if m.group(1) == m.group(2) else f"{m.group(1)}（{m.group(2)}）", text)
    text = _BOLD.sub(r"\2", text)
    text = _STRIKE.sub(r"\1", text)
    text = _PLACEHOLDER.sub(lambda m: kept[int(m.group(1))], text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
