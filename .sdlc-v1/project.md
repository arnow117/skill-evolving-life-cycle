# SELC 项目上下文

当前正式基础版为 [v0.1.0](https://github.com/arnow117/skill-evolving-life-cycle/releases/tag/v0.1.0)，2026-10-26 发布。GitHub/Gitee 的同一注释 tag 固定到 `33345ea55492c508d2e153a3b6195d394fcd8d51`；默认 `codex/design` 已整合对应代码。GitHub 提供源码、framework ZIP、普通 skill ZIP、basic suite ZIP、SHA256SUMS。Gitee 同步公开源码/tag，固定资产使用 GitHub 同版入口；没有创建 Gitee native Release。正式验证、独立评审、重新下载后的 hash/doctor/basic 安装输出证据见 F-003 工程上下文。

BASIC 新安装默认不启用 hooks、不要求 receipt、不自动统计。observed/自动统计/真实 creator及评估/自动贡献发布流水线为实验功能，F-002/T011 仍未完成。本次发布没有模型/provider/桌面测试，旧验证范围不能扩推为实验功能完成。以下较早事实保留历史语境，当前发布以 F-003 为准。

更新：2026-10-26。项目根为本文件上两级的独立 Git 仓库；F-002分支 `codex/auto-observation`，初始实现 commit `7cab4b8`，当前业务版本以 Git HEAD/正式 validation 绑定为准。origin 为公开 GitHub 仓库 https://github.com/arnow117/skill-evolving-life-cycle；基线及用户选择的MIT许可证`8d8328c`已推到`codex/design`，该分支为当前PR基线。

当前安装接口按 SRC-212 分为 basic/observed：新安装默认 basic，普通正文直接注册到 `.codebuddy/skills` 或 `.agents/skills`，不安装采集 hooks、不要求 receipt；已有安装省略模式保留原选择，旧无模式登记按 observed。显式 `--mode observed` 才启用自动统计/受管执行。manifest 可选 `basic_path` 为 skills/ 下独立普通正文；受管旧包没有该正文时需作者补齐或继续 observed。README 是唯一 usage 入口，CLI 参数为权威。T011 真实评估仍未完成，不由基础模式的离线检查替代。

当前未发布源码另有本地统计报告入口 `stats-report`：`--project-root` 必需且可重复，`--output` 必需并指定 `.html` 文件，`--include-test` 可选、仅决定初始显示开关。它生成可双击离线查看的单 HTML，更新数据需重新生成；不新增 runtime 依赖。已发布 v0.1.0 资产不含此命令。使用步骤唯一见 [README US-003B](../README.md#us-003b--view-a-static-local-statistics-report)，实现和检查范围见 [F-004 工程上下文](context/F-004.engineering.md)，不扩展 T011 或自动采集支持声明。

2026-10-25按用户当次确认创建公开Gitee源码镜像 https://gitee.com/arnow117/skill-evolving-life-cycle，从现有公开GitHub仓库完成一次导入。匿名Git读取核对`codex/design`为`8d8328c0313a231f194191d6e12e89f10b4cc128`、`codex/auto-observation`为`4df9b7e3d5be79713028eab48fb0afcc339fdaa7`，默认分支仍为`codex/design`，保留MIT。仅同步GitHub已推送源码；本地未提交候选未包含，也未创建Release或自动同步任务。证据位于同级工作区`20261025-selc-gitee-mirror/`。

## 定位与事实

REQ-003/F-003 按用户当次指示独立发布基础版 v0.1.0 至 GitHub/Gitee，自动统计、observed、真实 creator/评估和自动贡献/发布流水线标实验。此源码/框架/普通示例分发不要求实验业务候选的真实资格，也不修改其强制校验；F-002/T011 未完成状态继续保留。目标制品为源码 tar.gz、framework ZIP、标准普通 skill ZIP、basic suite ZIP 与 SHA256SUMS，使用已验证/评审源码 commit；发布事实以 F-003 工程上下文及 state 为准。

SELC 是在 Codex 创建和维护 skill 套件的 meta skill/脚手架。生成物面向 Codex 与 WorkBuddy；初版产品/工程记录为`context/REQ-001.product.md`和`context/F-001.engineering.md`，当前自动统计交付的权威为`context/REQ-002.product.md`与`context/F-002.engineering.md`。AiTaxs 为延后试点，不是当前实现依赖。进度仅由 `state.json` 表示。

用户已确认本地提交及继续验证/评审，历史验证与评审记录保留。业务版本 `aa079e2` 已通过正式验证（76 项测试、README 的 66 条实际操作及 API014 对照）、独立评审和主 Agent 本地交付验收。Feature 为 reviewed；当前远端选择公开 GitHub；F-001 的旧本地范围 release 仍 pending，不能替代 REQ-002 的完整闭环。技术栈为 Python >=3.11 标准库 CLI + SQLite + Git/uv；实际功能范围和证据以工程上下文为准。

`REQ-002` 的产品规格为 `context/REQ-002.product.md`，已进入F-002交付。用户已确认复用SQLite、统一受管入口、不确定分类unknown、采集不可用时暂停。有限真实探针已验证Codex CLI 0.147与WorkBuddy桌面5.7.6的业务入口自动采集和停用时暂停；Codex桌面、原生skill发现、完整统计和验证器框架待补齐。本轮已授权完整五项交付，采用结构化事件与保守规则，真实验收原计划24次串行请求（每次3分钟）；2026-10-21用户整体授权所需预算，不再申请追加，仍按需有界执行，正式发布前最后确认。结果及原件引用见产品上下文；不改变F-001的旧验证范围。

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
| 本地统计 HTML 报告 | `src/selc/stats_report*.py`、`src/selc/cli.py` 的 `stats-report`、`tests/*stats_report*`、`e2e.config.ts` | roles：architect、server-dev、client-dev、design、security、qa；validate modes：correctness、e2e:Web。仅支持 protocol-6 rollback-journal 的只读来源，拒绝 WAL；独立 HTML 无服务或在线依赖。当前源码入口与证据见 F-004 |
| 自动观测与业务检查基础 | `observation.py`、`checkers.py`、`checker_integration.py`；manifest可选检查声明 | F-002 T004/T008/T009增量验收通过，131项本地测试；真实宿主正式接入及one-shot完整区间仍未验收 |
| 安装与宿主适配 | `manifest.py` / `install.py` / `native_adapter.py` / `managed_runtime.py` | 正式native receipt、显式/自动托管激活、action/stage/publish与Stop对账；WorkBuddy5.9.2/GLM-5.3通过实际矩阵，Codex CLI证据单独保留。Codex桌面未验证，已按用户本轮指示取消追加实测；准确范围见F-002工程上下文 |
| 更新/评估/贡献/发布 | `install.py` / `evolve.py`，portable suite runtime | 框架来源复用、目录恢复、缺省保留与选择并发复核已有返修证据；既有回归仅 public GitHub/GitLab 和 local fixture；本轮已授权真实 GitHub贡献验证，结果待执行 |
| 验证 | `tests/` 和 README 的 US-001 至 US-005 | aa079e2 正式 Validate PASS，76项测试及 SC001–007 本地范围证据，见工程 Validation |

已有源码增量见[Draft PR #1](https://github.com/arnow117/skill-evolving-life-cycle/pull/1)，仍为开发中；当前实现和准确证据以F-002工程上下文为准，进度以state.json为准。早期updatedInput与权限失败探针仅保留其历史范围，当前生产采用直接身份绑定。Codex桌面未验证，不从CLI证据扩推桌面支持；用户已取消本轮追加桌面实测。

## 检查入口

F-002发布目标为公开GitHub源码、SELC框架及简单合成套件的版本化可安装制品。T011先完成真实贡献PR、发布计划和制品准备；正式Validate/Review绑定实现后，由Ship执行已确认的Release和两端正式安装升级smoke。正式发布保留用户最后一次确认；源码推送或Draft PR不等于released。

正式验证使用的主要命令如下，完整原始证据、绑定 commit 和适用范围见工程上下文。

```bash
UV_CACHE_DIR=/private/tmp/selc-uv-cache uv run --no-project --python 3.12 python -m unittest discover -s tests -v
```

README 是面向使用者的唯一全局使用指南，CLI help 为参数权威。F-002已实际执行compileall/py_compile和`git diff --check`；本机uv缓存中的Ruff0.15.6可离线调用，已实际验证版本和四项受影响文件的lint/format检查。具体结果仅适用于工程记录所列快照，不能沿用旧候选的通过结果。该作者工具不是portable运行依赖。官方 skill 格式校验使用当前creator工具环境，不能把其PyYAML当作portable运行依赖。

### 统计报告 Web 检查入口

开发工具为 Node 25.9/npm 11.12，`package.json`、`package-lock.json` 固定 dev-only `e2e 0.18.0`、`@e2e-dev/web 0.13.0`；Playwright 浏览器能力仅用于开发检查，不是 SELC runtime 依赖。测试配置为 `e2e.config.ts`，旅程为 `tests/stats_report.e2e.ts`，直接文件补充脚本为 `tests/stats_report_file_smoke.mjs`，fixture 为 `tests/stats_report_fixture.py`。配置、lockfile 与测试源纳入 Git；`node_modules/` 和 `.e2e/` 下的报告、截图、trace、临时 fixture 等运行产物忽略，实际归档引用见 F-004。

已验证的指定 runner 入口：

```bash
npm run test:e2e:stats-report -- --output .e2e/runs/routed-http-fresh-final
```

同一仓库工作目录下已验证的直接文件补充入口：

```bash
SELC_FILE_SMOKE_DIR=.e2e/file-smoke-final-rates node tests/stats_report_file_smoke.mjs .e2e/file-smoke-final-rates/stats-report.html
```

指定 runner 的固定 HTTP URL 通过 `browser.route`/`route.fulfill` 传送实际生成 HTML 的原样字节，无 server command 或监听服务；检查宽度为 1440/768/375px。直接 `file://` 补充检查由上述受跟踪脚本执行，宽度为 1440/375px；两种传送方式的证据分开记录，不能把 HTTP 旅程称为双击文件验收。两者均只用确定性浏览器操作与业务断言，零 agent/provider/模型调用或 token 消耗，无需凭据，不启用动作缓存。具体失败历史、检查结果与未验证范围只在 F-004 维护，不由入口存在推断正式 Validate/Review/Ship 已通过。

2026-10-21增量：T014完整前缀工具33项本地检查通过；实际请求5在WorkBuddy5.8.3、自定义GLM-5.3:glm-5.3中精确匹配原生PreToolUse与执行进程身份。生产方案采用无命令改写的直接绑定，T010仍在实现，完整业务及one-shot验收尚未通过；以F-002实施层决定13和私有证据摘要为准。
