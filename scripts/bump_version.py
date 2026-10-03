"""升级插件版本号：同时改 pyproject.toml 和 _manifest.json，并把 CHANGELOG 里「未发布」一节归到新版本下。

用法：uv run python scripts/bump_version.py 0.2.0
规则（语义化版本 MAJOR.MINOR.PATCH）：
- PATCH：只修 bug，配置不变
- MINOR：新功能；配置只新增字段（旧配置能自动升级）
- MAJOR：不兼容变化，例如需要手动迁移配置、提高 MaiBot 最低版本
配置结构有变化时，另外要升 qq_suite/host/config.py 里的 config_version（MaiBot 据此自动补齐新字段）。
"""

from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def main(version: str) -> None:
    if not SEMVER.match(version):
        sys.exit(f"版本号必须是 x.y.z：{version}")

    pyproject = ROOT / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    text, n = re.subn(r'(?m)^version = "[^"]+"$', f'version = "{version}"', text, count=1)
    if n != 1:
        sys.exit("pyproject.toml 里找不到 version")
    pyproject.write_text(text, encoding="utf-8")

    manifest_path = ROOT / "_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    old = manifest["version"]
    manifest["version"] = version
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    changelog = ROOT / "CHANGELOG.md"
    log = changelog.read_text(encoding="utf-8")
    unreleased = re.search(r"^## 未发布\s*$(.*?)(?=^## |\Z)", log, re.M | re.S)
    if unreleased is None or not unreleased.group(1).strip():
        sys.exit("CHANGELOG.md 的「未发布」一节是空的：先写这次改了什么再发版")
    today = datetime.date.today().isoformat()
    log = log.replace("## 未发布", f"## 未发布\n\n## {version}（{today}）", 1)
    changelog.write_text(log, encoding="utf-8")

    print(f"{old} → {version}")
    print(f"接下来：uv lock && git commit -am '发布 v{version}' && git tag v{version} && git push --follow-tags")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
