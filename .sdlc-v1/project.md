# SELC 项目上下文

更新：2026-10-20。项目根为本文件上两级的独立 Git 仓库；分支 `codex/design`，初始实现 commit `7cab4b8`，修正版沿同分支推进，当前业务版本以 Git HEAD/正式 validation 绑定为准，无 remote。

## 定位与事实

SELC 是在 Codex 创建和维护 skill 套件的 meta skill/脚手架。生成物面向 Codex 与 WorkBuddy；产品权威为 `context/REQ-001.product.md`，当前工程方案为 `context/F-001.engineering.md`。AiTaxs 为延后试点，不是当前实现依赖。进度仅由 `state.json` 表示。

用户已确认本地提交及继续验证/评审。前四轮正式验证失败和全部 Review 记录保留。`6a5814f` 正式 Validate PASS；第二轮独立 Review 的四项边界缺陷按 API012 修正，主 Agent 已接受同一 T003 Build（71 tests、README 实际旅程和 journal 恢复回归），待新 commit 的正式验证/评审。技术栈为 Python >=3.11 标准库 CLI + SQLite + Git/uv；实际功能范围和证据以工程上下文的独立实际记录为准。

## 约定与环境

- 项目定制 skill 正文在 `.claude/skills/`，Codex 入口在 `.agents/skills/`，使用相对链接；不安装到用户全局。
- README 是唯一全局 usage 入口；所有面向用户命令需实际验证。产品决定不写入 project 索引。
- 本机 system python3 为 3.9.6；已验证 uv 可用 Python 3.12.13。受 sandbox 限制使用 `UV_CACHE_DIR=/private/tmp/selc-uv-cache`，不改用户全局配置。
- 遵守父级 AGENTS、soul.md、user.md。实现由 Terra worker，主 Agent 做最终验收；状态只通过 SDLC CLI 由主 Agent 写入。提交 Markdown 前需用户确认。
- 验证只使用本项目/临时目录的简单合成数据、本地 Git 与受控 provider，不运行 AiTaxs/真实模型或写真实远端。

## Surface 与入口

| surface | 规划位置 | 当前证据 |
| --- | --- | --- |
| 作者 skill/CLI | `.claude/skills/selc/`、`scripts/selc.py`、`src/selc/cli.py` | 创建/creator 改写/seal/打包本地流程通过 |
| 本机记录 | `src/selc/records.py`，项目 `.selc/records.sqlite3` | 行为测试候选见工程实际记录 |
| 安装与宿主适配 | `manifest.py` / `install.py`，生成物独立 entry | 本地结构与 portable CLI 测试；真实宿主 smoke 未运行 |
| 更新/评估/贡献/发布 | `install.py` / `evolve.py`，portable suite runtime | 框架来源复用、目录恢复、缺省保留与选择并发复核已有返修证据；贡献仅 public GitHub/GitLab 和 local，真实外部执行未授权 |
| 验证 | `tests/` 和 README 的 US-001 至 US-005 | 6a5814f 正式 Validate PASS，66 项测试及 SC001–007 本地范围证据，见工程 Validation |

## 检查入口

实现者已执行的代表性命令如下，完整原始证据和适用范围留在工程上下文；它们不等于 commit-bound lifecycle 验证已通过。

```bash
UV_CACHE_DIR=/private/tmp/selc-uv-cache uv run --no-project --python 3.12 python -m unittest discover -s tests -v
```

README 是面向使用者的唯一全局使用指南，CLI help 为参数权威。格式与静态检查采用实现者已记录的 Ruff 和 compileall；官方 skill 格式校验使用当前 creator 工具环境，不能把其 PyYAML 当作 portable 运行依赖。
