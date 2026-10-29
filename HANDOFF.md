# SELC 换机开发交接

更新日期：2026-10-29。继续开发请使用 `codex/auto-observation`，其中包含当前框架源码、测试和完整 SDLC 记录。

## 获取开发源码

```sh
git clone --branch codex/auto-observation https://github.com/arnow117/skill-evolving-life-cycle.git selc
cd selc
python3.11 scripts/selc.py doctor
```

需要 Python 3.11 或更新版本、Git；运行时只使用 Python 标准库。也可从 Gitee 的同名开发分支克隆：`https://gitee.com/arnow117/skill-evolving-life-cycle.git`。`doctor` 应返回 `runtime_version: 0.3.0`、`stdlib_only: true`。

先读本文件、`.sdlc-v1/project.md`、`.sdlc-v1/state.json` 和 `.sdlc-v1/context/F-002.engineering.md`。后者从上往下保留当前方案、已接受实现、旧失败与消耗。新电脑的路径、宿主版本、登录及 hook 信任需要根据实际环境重新核对，不能直接继承旧机器的运行身份。

## 已同步的版本与分支

| 用途 | Ref / 入口 | 说明 |
| --- | --- | --- |
| 当前框架开发 | `codex/auto-observation` | 0.3.0 开发候选；含最新修复和交接记录 |
| SELC 基础安装版 | [v0.2.0](https://github.com/arnow117/skill-evolving-life-cycle/releases/tag/v0.2.0) | 仍为框架稳定 Latest；含 BASIC 和离线统计报告 |
| 业务示例初版 | [v1.0.0](https://github.com/arnow117/skill-evolving-life-cycle/releases/tag/v1.0.0) | `simple-item-reports` 1.0.0，整数、空数组 |
| 业务示例改进版 | [v1.1.0](https://github.com/arnow117/skill-evolving-life-cycle/releases/tag/v1.1.0) | `simple-item-reports` 1.1.0，精确小数、大数抵消 |
| 业务示例维护 | `codex/item-summary-native-v1` | `examples/managed-item-summary`；[PR #3](https://github.com/arnow117/skill-evolving-life-cycle/pull/3) 已合并 |

业务标签 v1.x 不是 SELC 框架 v1.x。其完整 suite 内嵌 0.3.0 运行时，自动统计仍属实验功能。默认分支 `codex/design` 未调整；从默认分支克隆不能取得全部开发增量。

两个业务发布目标分别为 `e75bca730fa51a24bc42808675e2ef8388cf6095`、`ee3428d31e99a8d879fb8e9f3a7d159deb500c27`；PR 合并提交是 `fce520171957436e3f0dc6a66752d15dfa0d680c`，其整棵树与后者相同。GitHub Release 的实际 ZIP 已下载验证，业务分支与原标签同步 Gitee。

| 发布资产 | SHA256 |
| --- | --- |
| `initial-stable-tag-v6.zip` | `66773afe673135605f166fe539f128d3075406b9032d20f07099f6b0556cee75` |
| `improved-final-v6.zip` | `d375a4bc6017cb5d6afe3749b9f6929db4cd281d50837ca822f597beeb561c57` |

普通 WorkBuddy 使用可按 Release 说明使用包自带 `bin/selc-suite.py install --source ZIP --harness workbuddy --mode basic`。BASIC 不需要 hooks 或自动统计资格。使用原 ZIP 作为来源可保留 Git provenance；直接运行解包目录 `install.py` 保存的是目录来源，从另一个解包目录升级可能返回 `NAME_CONFLICT`。

## 已有验收证据

- Codex CLI `0.163.0-alpha.11` / `gpt-5.6-terra`：同一最终运行时下 6 个实际单元全部通过，初版 2 个、改进版 4 个；每例 1 task / 1 call / 1 delivery，first/final/checker 均 pass，窗口闭合、`unknown_change=0`。
- 真实原件中这 6 例的 shell 均为 `-lc`；`-c` 兼容修复依据保留的实际失败反例及离线完整正向链，不冒充实际 `-c` 成功模型样本。
- 已发布 ZIP 的全新自带 CLI 安装、双宿主 BASIC 注册、安装后 CLI 显式升级及精确 `0.3` 输出通过；本地修改返回 `LOCAL_EDIT_CONFLICT` 并保留安装、修改和数据库记录；实际公开源每日检查首次发现 1.1.0，第二次命中缓存且没有自动应用。
- 源码局部验证分别保留在 `F-002.t011-*-implementation.md`；发布和实际下载验证的可迁移摘要见 [发布记录](.sdlc-v1/context/F-002.t011-remote-release.md)。

## 尚未完成及下一步

`REQ-002 / F-002 / T011` 仍在进行，不能因业务发布完成而改为全部完成。

1. WorkBuddy 5.9.2 只有已接受的诊断解析器：可核实 session/request、模型及实际调用工具；缺少绑定请求的完整生效 FS/network/approval/tool 策略与策略变化证据，资格保持 `unverified`。先寻找可验证的宿主原生来源；静态配置、截图、模型自报或手填 sidecar 都不能替代它。已有出厂 CLI SHA256 为 `29a2e96949fd85143f85c9fa31ae036657a15c351e847c7944b53a218c850a4d`。不同宿主版本需建立新证据合同。
2. 发布后的 BASIC 安装/升级已通过，升级后的真实受管调用和统计尚未验收；不能用普通脚本 smoke 代替。实际业务调用要在已验证的新机器身份和环境下进行必要有界验证。
3. 补齐上述真实闭环后，按原产品标准完成 F-002 正式 Validate / Review / Ship。状态只能由主 Agent 通过 SDLC lifecycle CLI 修改，不能手改 `state.json`。

沿用已有失败历史、累计 70 个模型任务 / 71 次执行启动、已使用的一次有界诊断额度。provider 内部调用数、token 与金额未知。禁止重复运行未变化的模型矩阵、改写旧 unknown，或因换机/重命名/新计划重置失败历史。必要模型调用已获授权，串行、每次最多 180 秒、首个实际 error/unknown 停止；Codex 桌面追加测试已由用户豁免。后续新增正式 Release 仍先准备准确可审查制品，再取得最后确认。

完整原生轨迹、SQLite、旧冻结计划及私有原件仍保留在旧机器 `workspace/20261028-selc-t011-*`、`workspace/20261029-selc-t011-explicit-routing` 等工作区，没有上传公共仓库。若要重建旧资格，应私密迁移这些原件并核对既有摘要；本交接摘要不替代原件，也不授权用新路径重写旧证据。
