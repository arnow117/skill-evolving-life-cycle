# SELC 项目上下文

更新：2026-10-20。项目根为本文件上两级的独立 Git 仓库；F-002分支 `codex/auto-observation`，初始实现 commit `7cab4b8`，当前业务版本以 Git HEAD/正式 validation 绑定为准。origin 为公开 GitHub 仓库 https://github.com/arnow117/skill-evolving-life-cycle；基线及用户选择的MIT许可证`8d8328c`已推到`codex/design`，该分支为当前PR基线。

## 定位与事实

SELC 是在 Codex 创建和维护 skill 套件的 meta skill/脚手架。生成物面向 Codex 与 WorkBuddy；初版产品/工程记录为`context/REQ-001.product.md`和`context/F-001.engineering.md`，当前自动统计交付的权威为`context/REQ-002.product.md`与`context/F-002.engineering.md`。AiTaxs 为延后试点，不是当前实现依赖。进度仅由 `state.json` 表示。

用户已确认本地提交及继续验证/评审，历史验证与评审记录保留。业务版本 `aa079e2` 已通过正式验证（76 项测试、README 的 66 条实际操作及 API014 对照）、独立评审和主 Agent 本地交付验收。Feature 为 reviewed；当前远端选择公开 GitHub；F-001 的旧本地范围 release 仍 pending，不能替代 REQ-002 的完整闭环。技术栈为 Python >=3.11 标准库 CLI + SQLite + Git/uv；实际功能范围和证据以工程上下文为准。

`REQ-002` 的产品规格为 `context/REQ-002.product.md`，已进入F-002交付。用户已确认复用SQLite、统一受管入口、不确定分类unknown、采集不可用时暂停。有限真实探针已验证Codex CLI 0.147与WorkBuddy桌面5.7.6的业务入口自动采集和停用时暂停；Codex桌面、原生skill发现、完整统计和验证器框架待补齐。本轮已授权完整五项交付，采用结构化事件与保守规则，真实验收最多24次串行请求（每次3分钟），正式发布前最后确认。结果及原件引用见产品上下文；不改变F-001的旧验证范围。

## 约定与环境

- 项目定制 skill 正文在 `.claude/skills/`，Codex 入口在 `.agents/skills/`，使用相对链接；不安装到用户全局。
- README 是唯一全局 usage 入口；所有面向用户命令需实际验证。产品决定不写入 project 索引。
- 本机 system python3 为 3.9.6；已验证 uv 可用 Python 3.12.13。受 sandbox 限制使用 `UV_CACHE_DIR=/private/tmp/selc-uv-cache`，不改用户全局配置。
- 遵守父级 AGENTS、soul.md、user.md。实现由 Terra worker，主 Agent 做最终验收；状态只通过 SDLC CLI 由主 Agent 写入。提交 Markdown 前需用户确认。
- F-001 验证使用简单合成数据、本地 Git 与受控 provider；REQ-002 的历史探针与本轮完整交付分开；本轮授权双端真实验收、GitHub公开贡献和发布准备，使用隔离合成数据，不运行 AiTaxs、不购买额度，正式Release仍最后确认。

## Surface 与入口

| surface | 规划位置 | 当前证据 |
| --- | --- | --- |
| 作者 skill/CLI | `.claude/skills/selc/`、`scripts/selc.py`、`src/selc/cli.py` | 创建/creator 改写/seal/打包本地流程通过 |
| 本机记录 | `src/selc/records.py`，项目 `.selc/records.sqlite3` | task/call 双轴结果、反馈修订、导入去重与删除回归通过，见工程 Validation |
| 自动观测与业务检查基础 | `observation.py`、`checkers.py`、`checker_integration.py`；manifest可选检查声明 | F-002 T004/T008/T009增量验收通过，131项本地测试；真实宿主正式接入及one-shot完整区间仍未验收 |
| 安装与宿主适配 | `manifest.py` / `install.py`；实验 `experiments/host_observation/` | F-001 为本地结构与 portable CLI 测试；REQ-002 另有 CLI Codex / WorkBuddy 桌面有限接入证据，未集成生产 runtime |
| 更新/评估/贡献/发布 | `install.py` / `evolve.py`，portable suite runtime | 框架来源复用、目录恢复、缺省保留与选择并发复核已有返修证据；既有回归仅 public GitHub/GitLab 和 local fixture；本轮已授权真实 GitHub贡献验证，结果待执行 |
| 验证 | `tests/` 和 README 的 US-001 至 US-005 | aa079e2 正式 Validate PASS，76项测试及 SC001–007 本地范围证据，见工程 Validation |

F-002源码增量已推送`0293358`；[Draft PR #1](https://github.com/arnow117/skill-evolving-life-cycle/pull/1)仍为开发中。native context实验工具准备通过11项本地检查，真实WorkBuddy请求2只暴露session环境，回执运输仍待验证；不能把原有限探针或这次实验视作正式双端接入完成。具体预算、原件和后续运输选择以F-002工程上下文为准。

## 检查入口

F-002发布目标为公开GitHub源码、SELC框架及简单合成套件的版本化可安装制品。T011先完成真实贡献PR、发布计划和制品准备；正式Validate/Review绑定实现后，由Ship执行已确认的Release和两端正式安装升级smoke。正式发布保留用户最后一次确认；源码推送或Draft PR不等于released。

正式验证使用的主要命令如下，完整原始证据、绑定 commit 和适用范围见工程上下文。

```bash
UV_CACHE_DIR=/private/tmp/selc-uv-cache uv run --no-project --python 3.12 python -m unittest discover -s tests -v
```

README 是面向使用者的唯一全局使用指南，CLI help 为参数权威。F-002已实际执行compileall/py_compile和`git diff --check`；当前Ruff不可用，不把未执行的formatter检查写成通过。官方 skill 格式校验使用当前creator工具环境，不能把其PyYAML当作portable运行依赖。
