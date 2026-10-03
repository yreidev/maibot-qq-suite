from __future__ import annotations

import pytest

from qq_suite.qq.text import split_text, strip_markdown


def test_split_short_and_empty():
    assert split_text("  你好  ", 10) == ["你好"]
    assert split_text("   ", 10) == []


def test_split_prefers_paragraphs_and_packs_chunks():
    paragraphs = ["第一段" * 10, "第二段" * 10, "第三段" * 10]  # 每段 30 字
    chunks = split_text("\n\n".join(paragraphs), 70)
    assert chunks == ["\n\n".join(paragraphs[:2]), paragraphs[2]]


def test_split_falls_back_to_sentences_then_hard_cut():
    text = "这是一句话。" * 10  # 60 字，没有换行
    chunks = split_text(text, 25)
    assert all(len(c) <= 25 for c in chunks)
    assert all(c.endswith("。") for c in chunks)
    assert "".join(chunks) == text

    no_breaks = "啊" * 55
    assert [len(c) for c in split_text(no_breaks, 20)] == [20, 20, 15]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("## 标题\n正文", "标题\n正文"),
        ("**重点**和__强调__，~~删掉~~", "重点和强调，删掉"),
        (
            "看[官网](https://example.com)和[https://a.com](https://a.com)",
            "看官网（https://example.com）和https://a.com",
        ),
        ("- 一\n* 二\n  + 三", "• 一\n• 二\n  • 三"),
        ("> 引用\n\n---\n\n下面", "引用\n\n下面"),
        ("| a | b |\n|---|:---:|\n| 1 | 2 |", "| a | b |\n| 1 | 2 |"),
        ("![图](https://x/y.png)", "图"),
        ("```python\nx = a**2 + b**2\nprint(__name__)\n```", "x = a**2 + b**2\nprint(__name__)"),
        ("用 `__init__` 和 `**kwargs`", "用 __init__ 和 **kwargs"),
        ("好耶 (*^▽^*) 2*3=6", "好耶 (*^▽^*) 2*3=6"),
    ],
)
def test_strip_markdown(source, expected):
    assert strip_markdown(source) == expected
