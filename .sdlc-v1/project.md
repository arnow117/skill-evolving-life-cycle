# SELC 项目上下文

## 定位与事实边界

SELC 是供 Codex 作者创建、维护和分发 skill 套件的 meta skill 与 Python CLI。生成的 portable suite 面向 Codex 与 WorkBuddy，包含自己的运行时和安装入口；使用者不必安装本仓库或全局 `selc` 命令。AiTaxs 是延后试点，不是实现依赖。

当前公开安装版为 [v0.2.0](https://github.com/arnow117/skill-evolving-life-cycle/releases/tag/v0.2.0)，包含稳定 BASIC 安装路径和离线 `stats-report`。新安装默认 BASIC：不安装 hooks、不要求 native receipt、不自动采集统计；已有安装省略模式时保留原选择，旧无模式登记按 observed 兼容。observed、自动统计、真实 creator/评估及自动贡献/发布流水线仍为实验能力，不能从基础版发布推断其完成。

[README](../README.md) 是唯一全局 usage，`scripts/selc.py` 的 argparse 定义是参数权威。本文件只索引稳定技术、入口、验证资源和分发来源；进度以 [state.json](state.json) 为准，产品与工程证据分别保留在对应上下文。

## 技术栈与环境

| 领域 | 技术与依据 |
| --- | --- |
| 运行时 | Python >=3.11 与标准库，见 `pyproject.toml`；不引入 portable runtime 第三方依赖 |
| CLI | `argparse`，`scripts/selc.py` 加载 `src/selc/cli.py`；成功和失败均返回 schema-1 JSON |
| 本机状态 | SQLite `.selc/records.sqlite3`，records writer protocol 6；安装登记 `.selc/installations.json` |
| portable 格式 | `suite.json`、`framework.json`、runtime protocol 1；运行时、业务内容及来源分别校验 |
| HTML 报告 | Python 生成单文件 HTML/CSS/JavaScript，无服务、网络或前端运行依赖 |
| 开发工具 | uv 管理独立 Python 环境；Ruff 配置在 `pyproject.toml`。本机系统 Python 3.9.6 不满足运行要求，已有 Python 3.11/uv Python 3.12 环境 |
| Web 检查 | dev-only Node 25.9/npm 11.12；`package.json` 和 lockfile 固定 `e2e` 0.18.0、`@e2e-dev/web` 0.13.0，浏览器由其 Web engine 使用 |
| 外部集成 | Codex、WorkBuddy 的本地注册与实验采集；Git/公开 GitHub、Gitee 源码分发；贡献实现另有 GitHub/GitLab provider 适配 |

标准库运行时用于便携安装；SQLite 复用本机记录与事务；HTML 使用内嵌数据和脚本提供离线查看。Node、Ruff 和 creator 的 PyYAML 属于开发/作者工具，不进入 portable 运行依赖。

## 目录与入口

| 区域 | 入口与职责 |
| --- | --- |
| 作者 skill | `.claude/skills/selc/SKILL.md`；`.agents/skills/selc` 是相对符号链接 |
| 源码 CLI | `scripts/selc.py` → `src/selc/cli.py`；创建、校验、seal、打包、安装、统计和维护命令 |
| 套件与安装 | `src/selc/manifest.py`、`install.py`；生成完整 suite、framework-only 包、双宿主注册、显式更新与恢复 |
| portable 入口 | 生成物的 `install.py`、`bin/selc-suite.py`、`runtime/selc/`；framework-only 包没有独立 CLI wrapper |
| 记录与执行 | `records.py`、`observation.py`、`native_adapter.py`、`managed_runtime.py`、`execution_lease.py`；实验 native 归属、受管执行与本机事务 |
| 业务检查 | `checkers.py`、`checker_integration.py`；manifest 可选 artifact check 声明 |
| 演进与发布实现 | `creator.py`、`evaluation.py`、`evolve.py`；真实资格和自动贡献/发布仍受实验限制 |
| 本地报告 | `stats_report.py`、`stats_report_template.py` 和 CLI `stats-report`；明确选择项目并生成可双击的静态 HTML |
| 示例与实验 | `examples/item-summary/` 为独立普通 skill/业务脚本，`examples/text-report/input.txt` 为确定性输入；`experiments/` 保留隔离宿主探针，不属于生产 runtime |
| 测试 | `tests/test_*.py`、`tests/fixtures/`；报告 Web 资产另见下节 |

`stats-report` 要求至少一个可重复的 `--project-root` 和 `.html` 的 `--output`，`--include-test` 只控制初始显示范围。报告读取 protocol-6 rollback-journal 数据库，拒绝 WAL；BASIC 登记可作为空来源。报告不自动刷新，更新数据需重新生成。完整用法只见 [README US-003B](../README.md#us-003b--view-a-static-local-statistics-report)。

## 检查入口与验证资源

以下为已有实际执行记录的入口，具体版本、受测源码和结果见相应工程上下文；入口存在不表示当前任意候选已通过验证。本次索引刷新没有重新运行产品检查。

```yaml
commands:
  unit: "UV_CACHE_DIR=/private/tmp/selc-uv-cache uv run --no-project --python 3.12 python -m unittest discover -s tests -v"
  coverage: "none"
  e2e: "npm run test:e2e:stats-report -- --output .e2e/runs/routed-http-fresh-final"
  typecheck: "none"
  lint: "ruff check ."
  build: "none"
```

Ruff 需使用已准备的开发工具环境，format 检查入口为 `ruff format --check .`；其本机工具来源及实际执行记录见 F-001/F-003/F-004。仓库没有独立 typecheck 或通用 build script；制品通过现有 CLI 打包。coverage 使用工程上下文记录的独占配置与原始命令，没有仓库固定入口。官方 skill 格式检查使用当前作者环境的 system `skill-creator`，不能把其 PyYAML 当作 portable 依赖。

### 报告 Web 资源

- 配置为 `e2e.config.ts`，旅程为 `tests/stats_report.e2e.ts`，fixture 为 `tests/stats_report_fixture.py`，直接文件补充为 `tests/stats_report_file_smoke.mjs`。这些配置、lockfile 与测试资产纳入 Git；无仓库 CI workflow。
- 指定 runner 的固定 `http://127.0.0.1:9/report.html` 仅由 `browser.route`/`route.fulfill` 传送实际 HTML 原样字节，不启动监听服务；检查宽度为 1440/768/375px。
- 已执行的直接文件补充入口为 `SELC_FILE_SMOKE_DIR=.e2e/file-smoke-final-rates node tests/stats_report_file_smoke.mjs .e2e/file-smoke-final-rates/stats-report.html`，覆盖 1440/375px。HTTP 旅程和直接 `file://` 证据分别记录。
- `node_modules/` 与 `.e2e/` 运行产物忽略；报告、截图、trace 与 fixture 历史的实际归档引用在 [F-004](context/F-004.engineering.md)。运行前保留旧证据，使用全新可变 fixture/输出，不能将旧报告当作新结果。
- 现有报告旅程是确定性浏览器操作与业务断言，不调用 agent/provider/模型，不使用凭据或动作缓存。沙箱可能阻止 Chromium 启动；实际环境失败与重试权限应按工程记录区分，不能改称产品通过。

## Surface map

以下 globs 相对仓库根目录；重叠区域合并角色和验证方式，真实模型执行仍需独立满足当前授权条件。

```yaml
surfaces:
  - name: author-cli-package
    globs:
      - ".claude/skills/selc/**"
      - ".agents/skills/selc"
      - "scripts/**"
      - "src/selc/__init__.py"
      - "src/selc/cli.py"
      - "src/selc/manifest.py"
      - "src/selc/creator.py"
      - "src/selc/errors.py"
      - "src/selc/util.py"
      - "src/selc/SELC-LICENSE"
    roles: [architect, server-dev, ai-readiness, security, qa]
    modes: [correctness]
  - name: records-observation-checks
    globs:
      - "src/selc/records.py"
      - "src/selc/observation.py"
      - "src/selc/native_adapter.py"
      - "src/selc/managed_runtime.py"
      - "src/selc/execution_lease.py"
      - "src/selc/checkers.py"
      - "src/selc/checker_integration.py"
    roles: [architect, server-dev, security, qa]
    modes: [correctness]
  - name: install-update
    globs: ["src/selc/install.py"]
    roles: [architect, server-dev, security, qa]
    modes: [correctness]
  - name: evaluation-contribution-release
    globs: ["src/selc/evaluation.py", "src/selc/evolve.py"]
    roles: [architect, server-dev, security, qa]
    modes: [correctness, eval-bench]
  - name: offline-statistics-report
    globs:
      - "src/selc/stats_report*.py"
      - "tests/*stats_report*"
      - "e2e.config.ts"
      - "package.json"
      - "package-lock.json"
    roles: [architect, server-dev, client-dev, design, security, qa]
    modes: [correctness, "e2e:Web"]
  - name: regression-tests
    globs: ["tests/**"]
    roles: [qa]
    modes: [correctness]
  - name: examples-host-experiments
    globs: ["examples/**", "experiments/**"]
    roles: [server-dev, ai-readiness, security, qa]
    modes: [correctness]
  - name: repository-guidance-config
    globs: ["README.md", "LICENSE", "pyproject.toml", ".gitignore", ".sdlc-v1/**"]
    roles: [ai-readiness, qa]
    modes: [correctness]
```

## 工程约定与限制

- 遵守父级 `AGENTS.md`、`soul.md`、`user.md`。项目定制 skill 正文在 `.claude/skills/`，Codex discovery 使用 `.agents/skills/` 相对链接，不安装到用户全局。
- 实现遵循 Terra worker 路由，主 Agent 负责边界与最终验收；状态只由主 Agent 通过 SDLC CLI 写入。提交、推送和外部操作沿用实际用户授权，不从本索引获得新权限。
- manifest、runtime hash、业务 digest、显式框架来源、安装模式及记录协议的兼容性是敏感区域；保留已有事务、恢复、拒绝不明确证据和 unknown 分类约束。
- 源码、包与报告不得携带 `.git`、`.selc`、凭据、私人日志或本机配置；可变测试数据和检查产物使用隔离目录，不覆盖历史证据或修改真实记录补造结果。
- README 保持唯一 usage；产品规格见 `REQ-*.product.md`，实现与正式证据见 `F-*.engineering.md`。本索引不复制产品决定、测试数量或执行流水账。

已知限制：observed 可因 `RECEIPT_UNAVAILABLE` 暂停；不能把某一宿主/版本的实际样本推广到其他入口或版本。Codex 桌面没有完整验收，用户已取消本轮追加桌面实测。F-002/T011 的真实资格、贡献和发布闭环未完成；当前执行授权、精确候选与首次错误停止约定以用户最新指示和 F-002 工程上下文为准。报告目前只支持上述本地只读来源，不代表自动采集健康或真实业务成功资格。

## 分发与部署索引

本项目以源码和可安装 ZIP 分发，没有运行中的 Web 服务、staging/canary 或数据库部署。两个公开源码仓库的默认分支均为 `codex/design`：[GitHub](https://github.com/arnow117/skill-evolving-life-cycle)、[Gitee](https://gitee.com/arnow117/skill-evolving-life-cycle)。

[GitHub v0.2.0 Release](https://github.com/arnow117/skill-evolving-life-cycle/releases/tag/v0.2.0) 已公开五项资产：源码归档、framework ZIP、普通 skill ZIP、完整 suite ZIP 和 `SHA256SUMS`。GitHub/Gitee 的同版 tag 指向批准源 `e0cb7ca478516b01e996eafc4e96e3d2b4a20774`；Gitee 提供源码/tag 和 README 中的 GitHub 同版本下载入口，没有 Gitee native Release。v0.1.0 tag 和资产保留。

v0.2.0 源码 CLI 和完整 `simple-reports-1.0.0.zip` suite 含 `stats-report`；framework-only ZIP 仅含运行时、没有独立 CLI，普通 `item-summary` ZIP 仅有独立业务脚本。最终发布、远端下载核对及新解包 smoke 的结果与适用范围统一见 [F-005](context/F-005.engineering.md)。

## 上下文来源与维护

- 根指令来源：父级 `/Users/arnow117/hansen_agent_team/AGENTS.md`、`soul.md`、`user.md`；目标仓库和当前源码范围内没有额外 scoped `AGENTS.md`。
- 长期入口：`README.md`、`.claude/skills/selc/SKILL.md`、`pyproject.toml`、`package.json`、`package-lock.json`、`e2e.config.ts` 及上列源码/测试。`.repo-context/manifest.yaml` 和 `evidence.json` 不存在，长期 repo-context 来源为 `none`。
- 历史首版冻结源码交付及原验证范围见 [F-001](context/F-001.engineering.md)，交付源为 `aa079e25727e1d37c4a884d8d75bf6d5805ac4ef`；基础 v0.1.0 分发见 [F-003](context/F-003.engineering.md)。历史交付不代替新版安装包或实验资格。
- 报告实现、Web/直接文件证据及源码交付见 [F-004](context/F-004.engineering.md)；v0.2.0 制品与正式分发见 [F-005](context/F-005.engineering.md)。实验宿主与评估的准确范围见 [F-002](context/F-002.engineering.md)。生命周期进度只查询 `state.json`。
- 入口、命令、依赖、协议、验证资源或分发目标变化时刷新本索引；普通修复、测试结果或 Feature 阶段变化只更新其原有证据位置。

源码模块、CLI schema、README 及可发现测试提供主要 AI 工程导航；portable 生成物、缓存和运行证据已有忽略规则。尚无仓库固定 coverage/typecheck/build/CI 入口，不应从本机历史工具环境推断其他机器已准备完毕。
