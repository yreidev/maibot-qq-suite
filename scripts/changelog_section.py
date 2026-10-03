"""打印 CHANGELOG.md 里某个版本那一节的内容（不含标题），给 GitHub Release 当说明用。

用法：python scripts/changelog_section.py 0.1.0
找不到该版本或内容为空时退出码为 1，CI 据此拒绝发版（每个版本都必须写更新日志）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"


def section(version: str, text: str) -> str:
    pattern = re.compile(rf"^## {re.escape(version)}(?:（[^）]*）)?\s*$(.*?)(?=^## |\Z)", re.M | re.S)
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    body = section(sys.argv[1], CHANGELOG.read_text(encoding="utf-8"))
    if not body:
        sys.exit(f"CHANGELOG.md 里没有 {sys.argv[1]} 的内容")
    print(body)
