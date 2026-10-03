# 开发与发版

## 开发

```bash
uv sync                 # Python 3.13，与 MaiBot 镜像一致
uv run pytest           # 单元测试（本地假服务跑真实 HTTP / WebSocket）
uv run ruff check . && uv run ruff format --check .
```

架构与设计取舍见 [DESIGN.md](DESIGN.md)。

## 发版

平时每次改动都在 `CHANGELOG.md` 的「未发布」一节记一笔（分「新增 / 修复 / 变更」）。发版时：

```bash
uv run python scripts/bump_version.py 0.2.0   # 同时改 pyproject.toml、_manifest.json，并把「未发布」归到 0.2.0
uv lock && git commit -am "发布 v0.2.0" && git tag v0.2.0 && git push --follow-tags
```

版本号遵循语义化版本：只修 bug 升第三位，加功能升第二位，需要手动迁移配置等不兼容变化升第一位。
推送标签后 GitHub Actions 自动：跑检查 → 核对三处版本号一致 → 创建 Release，说明取自 CHANGELOG 对应一节，
附件是可直接解压到 `MaiBot/plugins` 的压缩包（不含测试和开发文件）。

配置结构有变化（加字段、改选项）时，同时升 `qq_suite/host/config.py` 里的 `config_version`，
MaiBot 会按新结构重写用户的 `config.toml`（保留已填的值）。

`_manifest.json` 的 `host_application.max_version` 锁在当前 MaiBot 小版本（如 1.3.99）：插件依赖宿主的一些内部行为，
MaiBot 升到新的小版本后先实测，再放宽这个上限。
