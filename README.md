# SELC

The current development source is an **unreleased 0.3.0 candidate** for T011. The stable downloadable release remains v0.2.0; the new native-request and schema-3 explicit-routing preparation features described below are not in its immutable ZIP assets.

SELC v0.2.0 provides a stable **BASIC** path for creating, validating, sealing, packaging, and ordinarily installing a versioned skill suite. It also distributes the existing offline `stats-report` command for a single, double-clickable HTML statistics report. BASIC registration is a direct local skill registration: it installs no hooks, requires no native receipt, and records no automatic task, one-shot, qualification, or success statistics. The authoring meta skill at `.claude/skills/selc/` is for Codex authors only. A WorkBuddy user consumes either the ordinary skill ZIP or a generated suite; a generated suite installs into Codex and WorkBuddy without requiring this repository, a global `selc` command, or Codex at the WorkBuddy installation site.

Get this version from [GitHub Release v0.2.0](https://github.com/arnow117/skill-evolving-life-cycle/releases/tag/v0.2.0). The matching source tag is [Gitee v0.2.0](https://gitee.com/arnow117/skill-evolving-life-cycle/tree/v0.2.0). Release assets include the source archive, portable framework, ordinary skill example, basic suite, and SHA256SUMS. The v0.1.0 tag and assets remain unchanged.

| Capability | v0.2.0 support |
| --- | --- |
| `create`, `validate`, `seal`, and `package` | BASIC |
| Fresh basic installation, explicit basic upgrade, and standard-library portable execution | BASIC |
| Fresh basic installation behavior | No hooks, no receipt, no automatic statistics |
| Offline `stats-report` from source or a complete generated suite | BASIC, read-only local HTML |
| Existing installation compatibility | Omitted mode preserves the saved mode; a legacy registration with no saved mode remains observed |
| Observed hooks, automatic statistics, one-shot results, and native attribution | **EXPERIMENTAL** |
| Real creator and evaluation, automated contribution, and automated release pipeline | **EXPERIMENTAL** |

The EXPERIMENTAL observed path can currently stop with `RECEIPT_UNAVAILABLE`. It does not guarantee Codex or WorkBuddy desktop support, native discovery completeness, support for a new model or host version, or a qualified managed candidate. F-002/T011 remains incomplete. Publishing this framework source and the ordinary example below does not qualify, release, or replace the evidence required for a managed business candidate.

`scripts/selc.py` is the only global usage entrypoint. Its argparse schema is authoritative; use `selc --help` or `selc <command> --help` for the current parameter list. Runtime dependencies are Python 3.11+ and the standard library. `uv` manages the independent command environment; no dependency is installed implicitly.

The original v0.1 regression suite validates local filesystem, SQLite, local Git, a local release center, and controlled fake GitHub/GitLab clients. Those tests do not establish Codex or WorkBuddy desktop support, real remote mutation, or real model execution. AiTaxs is not included in this repository or example.

## Common setup

Run from this repository with a supported interpreter:

```bash
selc() {
  UV_CACHE_DIR=/private/tmp/selc-uv-cache uv run --no-project --python 3.12 python scripts/selc.py "$@"
}
selc_python() {
  UV_CACHE_DIR=/private/tmp/selc-uv-cache uv run --no-project --python 3.12 python "$@"
}
selc doctor
```

All success responses are JSON with `schema_version: 1`. A nonzero response is JSON with a stable `error.code` and recovery instruction. `--project-root` names the owner of the local SQLite database and installed suite state. It must be a directory the caller controls. `--harness` is an explicit `codex` or `workbuddy` registration target; SELC never infers it from a model name.

## BASIC 快速开始

在仓库根目录使用上面的 `selc` 函数。创建一个普通 suite 后，先 seal，再验证和打包；新安装默认进入 BASIC，也可以显式写出 `--mode basic`。

```bash
selc create --suite-id hello-suite --version 1.0.0 --output /tmp/hello-suite --skill hello
selc seal --suite /tmp/hello-suite
selc validate --suite /tmp/hello-suite
selc package --suite /tmp/hello-suite --output /tmp/hello-suite.zip
selc install --source /tmp/hello-suite.zip --project-root /tmp/hello-project \
  --harness workbuddy --mode basic
```

使用 `selc updates apply` 执行显式升级；它保留已保存的安装模式。完整参数、失败恢复和升级行为见 [US-004](#us-004--check-and-explicitly-update-a-suite)。

## Ordinary no-hook example — item-summary

[`examples/item-summary`](examples/item-summary/) is a standalone ordinary skill with its own MIT license. It needs only Python 3.11+ and a request JSON object whose `input` value names a JSON file containing `{"items": [{"value": 3}, {"value": 5}]}`. Its output is `{"count": 2, "sum": 8}`. Decimal values and missing or non-integer values are errors. A WorkBuddy user can upload a ZIP whose root is this standard skill directory; a project user can instead install a generated suite. Neither consumer path requires Codex.

Run the script from the project working directory so a relative input name resolves in that project:

```bash
mkdir -p reports
python3 examples/item-summary/scripts/item_summary.py managed-run \
  --request request.json --output reports/item-summary.json
```

`managed-run` is the script's fixed subcommand name; this example is still ordinary local file processing. It requires no SELC installation, hooks, native receipt, task ID, or automatic statistics configuration.

## Install modes — start with ordinary use

New installations default to `basic`. This is the normal path for a WorkBuddy or Codex project: the registered skill points directly to its ordinary `SKILL.md` body in the immutable installed suite. It creates no hooks, requires no native receipt, and does not create automatic task, one-shot, qualification, or success-statistics records.

```bash
selc install --source /tmp/simple-reports.zip \
  --project-root /tmp/selc-project --harness workbuddy
selc_python /tmp/selc-project/.selc/installs/simple-reports/current/bin/selc-suite.py run text-report \
  --input examples/text-report/input.txt --output /tmp/selc-project/report.md
```

The same generated suite can install itself locally with the same default:

```bash
selc_python /tmp/simple-reports/install.py \
  --project-root /tmp/selc-project --harness workbuddy
```

Pass `--mode basic` only when you want to state that choice explicitly. A missing or malformed unrelated `.codebuddy/settings.json` or `.codex/hooks.json` does not block a fresh basic installation and is left byte-for-byte unchanged. The installation result returns `mode: "basic"` and `observation: {"enabled": false}`.

Choose `--mode observed` only for the managed runtime and automatic evidence path. It creates this suite's collector hooks, requires native receipts, and preserves the existing strict observation and publication checks. An existing installation keeps its saved mode when `--mode` is omitted; an older registration with no saved mode is treated as `observed` for compatibility. `updates apply` and recovery preserve that mode. A mode switch updates every registered harness together and waits for active tasks, calls, sessions, or managed executions to finish.

Managed skills need a separate `basic_path` body before they can be installed in basic mode. Legacy packages that contain only an observed managed body remain usable with `--mode observed`; SELC does not rewrite their instructions to pretend they are ordinary skills. Basic runs produce their requested business artifact but do not claim automatic statistics or evaluation eligibility.

The author owns both bodies when a skill has a `basic_path`: edit and validate its ordinary body for basic use, and its `path` body for observed use. Both directories live below `skills/` and are included in the sealed package. Generated path examples must resolve the physical loaded skill directory because harness registration can be a symbolic link.

## US-001 — Create a suite

Actor: a Codex skill author. Preconditions: Python 3.11+ and an empty output path. The author owns the suite ID, version, and skill names.

```bash
SELC_CREATOR_PYTHON=/opt/homebrew/bin/python3.11
SELC_CREATOR_PYTHON_ROOT=/opt/homebrew/opt/python@3.11
SELC_SKILL_CREATOR="${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator"
selc create \
  --suite-id simple-reports --version 1.0.0 --output /tmp/simple-reports \
  --skill text-report --skill text-check
# Use skill-creator in the active Codex authoring environment to replace the
# generated body and add declared resources. Then validate that skill there.
printf '\nAuthor-approved text-report guidance.\n' >> /tmp/simple-reports/skills/text-report/SKILL.md
"$SELC_CREATOR_PYTHON" "${SELC_SKILL_CREATOR}/scripts/quick_validate.py" \
  /tmp/simple-reports/skills/text-report
selc seal --suite /tmp/simple-reports
selc validate --suite /tmp/simple-reports
selc package --suite /tmp/simple-reports --output /tmp/simple-reports.zip
```

`SELC_CREATOR_PYTHON` names the active Codex creator environment, which supplies PyYAML, and `SELC_SKILL_CREATOR` names its `skill-creator` directory. They are authoring-tool inputs, not portable runtime dependencies. `seal` is explicit: before it succeeds, `validate` and `package` reject creator edits with `CONTENT_HASH_MISMATCH`. It validates manifest paths, portable content, runtime protocol, and the existing `framework.json` runtime hash, then changes only `suite.json`'s content hash. It cannot repair a modified runtime or make an installed local edit clean. The result has `suite.json`, independent `skills/*/SKILL.md`, portable `runtime/`, `bin/selc-suite.py`, `install.py`, and `MAINTENANCE.md`. The package operation includes those portable code and declared-resource entries and, when packaging from a valid Git checkout, a minimal source-baseline descriptor for later isolated contribution. It excludes `.git`, `.selc`, reports, and other business data. The output path must be outside the suite, and symlinks in portable content are rejected. If creation or packaging reports `PATH_CONFLICT`, select an empty output path. If validation reports `INVALID_MANIFEST`, repair the declared relative paths and skill names before packaging.

Portable skill files use the supported core form: a bounded `---` frontmatter block with top-level declared string `name`, top-level non-empty string `description`, and a non-empty body. Top-level keys must be unquoted ASCII identifiers without surrounding whitespace. Write required values as `name: value` and `description: value`, using an ASCII space after the colon; the portable parser accepts ordinary text, JSON double-quoted strings, and simple YAML single-quoted strings. A raw tab is permitted only inside a supported quoted string. Quote YAML-reserved values such as `on`, `off`, `true`, `false`, and `null`, and quote mapping-like text. Unicode content is valid inside a supported string, but raw frontmatter control characters are rejected. Nested YAML fields do not satisfy those portable requirements. This is a runtime safety check, not a replacement for the full creator validator. A manifest may omit a skill `path`, which SELC interprets as `skills/<name>` without rewriting the manifest. SELC v0.1 uses a canonical typed-tree digest for portable content. Development artifacts made with an earlier digest format must be recreated, sealed, evaluated, and reconfirmed; SELC does not accept a weak-digest fallback or silently reseal them.

For Codex-authoring changes, use `.claude/skills/selc/SKILL.md` and the system `skill-creator` validator. Its PyYAML requirement is not a portable suite dependency.

## US-002 — Install and run the text-report example

Actor: a suite user. Preconditions: a package produced by US-001, an empty or compatible project root, and Python 3.11+. The commands below use the default ordinary `basic` mode.

```bash
selc install --source /tmp/simple-reports.zip \
  --project-root /tmp/selc-project --harness codex
selc install --source /tmp/simple-reports.zip \
  --project-root /tmp/selc-project --harness workbuddy
selc_python /tmp/selc-project/.selc/installs/simple-reports/current/bin/selc-suite.py run text-report \
  --input examples/text-report/input.txt --output /tmp/selc-project/report.md
```

The installation result identifies the installed version, digest, source baseline, registered skills, and maintenance guide. Directory, Git, and package installs copy the same explicit portable code/resource surface, not a source repository's `.git`, `.selc`, reports, or interpreter cache. Codex entries are registered below `.agents/skills/`; WorkBuddy entries are registered below `.codebuddy/skills/`. The portable run produces a Markdown report. The installed wrapper also supports `seal`, `validate`, `package`, and `install-local` without this repository. Use `seal` only for an authoring source after creator validation; use `evolve` for installed-suite improvements.

For a downloaded package that carries Git provenance, unpack it and run its bundled CLI against the original ZIP. This needs Python 3.11+, with no source checkout or global `selc` command. For the `simple-item-reports` business suite, replace the example paths with the downloaded files and your project:

```bash
python3.11 /path/to/unpacked-suite/bin/selc-suite.py install \
  --source /path/to/suite-1.0.0.zip --project-root /path/to/project \
  --harness workbuddy --mode basic
python3.11 /path/to/project/.selc/installs/simple-item-reports/current/bin/selc-suite.py updates apply \
  --source /path/to/suite-1.1.0.zip --project-root /path/to/project \
  --harness workbuddy --suite-id simple-item-reports
```

The ZIP source retains the shared Git repository identity across versions. Direct `install.py` instead saves the unpacked directory as its source; a later separately unpacked directory is a different upstream and returns `NAME_CONFLICT`. Choose the ZIP-source route from the first installation when later updates will also come from ZIPs. BASIC mode and existing harness registrations are preserved by an explicit update.

Use `--source <repo> --subdir <relative-suite-dir> --ref <tag-or-commit>` for a local or remote Git suite source. Without `--ref`, a Git source selects the latest stable tag. An explicit `--framework-source` with optional `--framework-ref` or `--framework-subdir` reads `framework.json`, validates runtime protocol 1 and runtime hash, and builds a complete staged composite: business skills/resources are retained while the portable runtime is replaced. Its source, commit, version, protocol, and digest are saved. A later business update preserves that explicit framework; an embedded installation takes its framework from the new business package. Same source and digest reuse the installation only after verifying its content is unchanged. A different source with the same suite name, a conflicting harness skill name, unsafe subdirectory, incompatible framework protocol, or local edit fails with an explicit code. Preserve the local change or choose another suite name before retrying.

## US-003 — Record and aggregate a task

Actor: a harness adapter or user with task evidence. This diagnostic and observed-mode interface is separate from ordinary basic execution, which does not add production rows or automatic statistics. A task ID is the business-operation identity; each invocation gets a separate call ID. `initiator` and `executor` are separate fields. `--test` puts controlled fixtures outside default reports. An installed call must supply the skill entry's `--expected-version` and `--expected-hash`; SELC never fills a missing digest from the current installation, so a stale or incomplete entry cannot mix with a new runtime.

```bash
selc task start --project-root /tmp/selc-project --id task-1 \
  --suite-id simple-reports --initiator author --evidence local-test --test --harness codex
EXPECTED_HASH=$(selc_python -c 'import json; print(json.load(open("/tmp/selc-project/.selc/installations.json"))["installations"]["simple-reports"]["digest"])')
selc call start --project-root /tmp/selc-project --id call-1 --task-id task-1 \
  --skill text-report --executor fixture-runner --evidence local-test --expected-version 1.0.0 \
  --expected-hash "$EXPECTED_HASH"
selc event --project-root /tmp/selc-project --task-id task-1 \
  --kind input_ready --evidence 'input selected for this task'
selc delivery --project-root /tmp/selc-project --id delivery-1 --task-id task-1 --call-id call-1 \
  --artifact-hash abc123 --rule-version v1 --result pass --complete-observation-fixture
selc call complete --project-root /tmp/selc-project --id call-1 --result pass
selc task close --project-root /tmp/selc-project --id task-1
selc stats --project-root /tmp/selc-project --include-test
```

Reports keep independent task and call denominators. Each axis exposes `N`, `S`, `K`, `U`, success lower bound `S_over_N`, known-result success `S_over_K`, and coverage `K_over_N`; zero denominators are `null`. First one-shot and final delivery stay separate. Necessary input and permission clicks are recorded with `event`; a `business_intervention` makes one-shot fail while final delivery can still be separately assessed. Ordinary self-report results are `unknown`; `--complete-observation-fixture` is rejected outside a test task. A trusted business check can establish final artifact success, but first one-shot remains `unknown` until a test fixture or a complete trusted host observation exists. A business intervention makes the task first result fail without copying that result to unrelated calls. A hard delivery failure stays failed. Feedback binds the original delivery requirement: legacy `first` and `final` scopes both refute that delivery, so a negative assessment changes its first and final outcome; a new passing remedial delivery can restore only the final outcome. A higher revision of the same feedback ID supersedes that feedback thread, while independent negative feedback remains effective. `requirement_change` records a later requirement without rewriting the original delivery. For a normal text report, `--checker text-report-v1 --rule-version text-report-v1 --input <input> --output <report>` performs the built-in, installed-resource check and binds the reported artifact hash; an arbitrary checker name does not prove success.

## US-003B — View a static local statistics report

Use `stats-report` when an owner needs an offline, double-clickable count-only snapshot. Pass every selected project explicitly and choose an `.html` output whose parent already exists. The report reads only supported protocol-6 rollback-journal records databases without initializing, exporting, migrating, or mutating them. A project confirmed by its read-only installation registry to be entirely `basic` can appear as an empty source; it does not make an observation or health claim. The v0.2.0 source archive and complete `simple-reports-1.0.0.zip` suite include this command. The portable framework ZIP is a runtime-only package, and the ordinary `item-summary` ZIP is a standalone script; neither includes a global report command.

```bash
selc stats-report \
  --project-root /tmp/selc-project \
  --project-root /tmp/other-selc-project \
  --output /tmp/selc-statistics.html
```

Open `/tmp/selc-statistics.html` directly in a browser. It contains no network dependency or automatic refresh. Regenerate the same file to inspect later records. `--include-test` starts its local test-sample toggle on; without it, test cells remain embedded but start excluded. Filters recompute only the selected project's embedded cells, and projects stay separate because source overlap is not globally deduplicated.

For an unpacked v0.2.0 source archive, use the `selc` function from [Common setup](#common-setup). For an unpacked complete suite, invoke its local runtime instead:

```bash
python3 /path/to/simple-reports/bin/selc-suite.py stats-report \
  --project-root /tmp/selc-project \
  --output /tmp/selc-statistics.html
```

The report exposes only count axes and grouping dimensions. It excludes task/call IDs, evidence, prompts, logs, credentials, and source paths. It rejects WAL databases, source aliases, and unsafe output links. A readable source can still produce a partial report when another selected source fails; if all selected sources fail, it leaves an existing output untouched.

## US-003A — Diagnose a declared artifact check

An optional schema-1 `suite.json` `artifact_checks` object declares a fixed checker identity, rule version, required rule IDs, JSON configuration, and an optional OS-network-isolation requirement. A skill entry can name one declaration with `artifact_check`. Declarations contain no executable path, argv, or environment values. A missing declaration, unsupported rule, installed-byte drift, source change, unavailable checker, or manifest digest pin mismatch cannot produce a pass.

For generic managed authoring, `create --managed-declaration declaration.json` accepts this explicit protocol-1 envelope. `managed.actions` names arbitrary declared skills and either `instruction-staging-v1` or `python-script-v1`; `native_hosts` fixes allowed host versions. `artifact_checks` remains a normal manifest section, and `skill_artifact_checks` binds each declared skill to one named checker rather than hiding checker metadata in `managed`.

```json
{
  "managed": {
    "protocol": 1,
    "native_hosts": {"codex": ["0.163"]},
    "actions": {
      "invoice-stage": {
        "skill": "invoice-writer",
        "kind": "instruction-staging-v1",
        "input": {"fields": {}, "required": []}
      }
    }
  },
  "artifact_checks": {
    "invoice-json": {
      "checker": {"name": "json-item-summary", "version": "1"},
      "rule_version": "1",
      "required_rule_ids": ["count", "sum"],
      "configuration": {"duplicate_ids_count_once": true}
    }
  },
  "skill_artifact_checks": {"invoice-writer": "invoice-json"}
}
```

```bash
selc check --project-root /tmp/selc-project --suite simple-reports \
  --check-id item-summary --input /tmp/items.json --artifact /tmp/summary.json
```

`check` reads fixed input and artifact bytes, runs the selected installed framework's trusted checker, then confirms that those source paths and the installed typed tree did not change. Its JSON result binds the active installation version and typed-tree digest separately from the checker's composite-byte digest, and includes the checker identity, input/artifact/resource digests, and each required rule result. It is diagnostic only: it creates no task, call, delivery, publication, or statistics denominator. A managed adapter can later attach the same internal result to its exact task/call/delivery with one short records transaction after execution.

The built-in `json-item-summary@1` verifies JSON `items` against an artifact `count` and `sum` using exact decimal semantics. For a maintainer-owned extension, register a current digest-pinned local script in the project before any suite declaration selects it:

```bash
SCRIPT_DIGEST=$(shasum -a 256 /tmp/maintainer-checker.py | awk '{print $1}')
selc checker register-script --project-root /tmp/selc-project \
  --name custom-summary --version 1 --script /tmp/maintainer-checker.py \
  --digest "$SCRIPT_DIGEST" --rule custom
selc checker inspect --project-root /tmp/selc-project
```

The project registry is explicit and local; it does not accept a business request's executable, environment, or command line. The runtime verifies the script digest before starting a private snapshot. Script stdin/stdout/stderr and runtime are bounded, but this is not an OS sandbox. A declaration requiring OS-enforced network isolation returns `unknown` unless an implementation can actually provide it.

The runtime also has an internal Python observation store: `selc.observation.ingest`, `replay`, `timeline`, and `assessment`. It accepts only a bounded v1 semantic DTO, persists allowlisted identifiers, hashes, classification facts, and opaque evidence references before projection, and derives formal results from that evidence. Replay restores assessments affected by persisted pending, quarantined, or conflicting evidence after a crash. A formal first pass requires complete execution evidence; a verifier-bound final pass can remain valid when unrelated execution evidence is incomplete, while a verified failure remains fail. Conflicted final-delivery evidence and publication gaps cannot establish a pass.

Installed managed suites add project-owned discovery entries and silent native hooks. The hooks keep unrelated project hooks unchanged, return no command rewrite or permission response, and persist an exact native receipt before activation. A discovery entry invokes the installed `managed invoke` wrapper; it neither carries a receipt nor embeds the skill body. In the current untagged T011 experimental source, Codex may perform exactly one discovery read: `/bin/cat <the current installer-owned routing SKILL.md>`. SELC binds the registry link, immutable entry bytes, the root-owned non-group/other-writable reader's device, inode, and bytes digest, current cwd, Pre/Post callbacks, and one matching native `CommandExecution` with exit zero and exact `aggregated_output`. A missing, duplicate, replaced, or mismatched completion remains `unknown`; compound shell reads, input reads, and all other unmanaged commands remain ordinary unmanaged evidence. The wrapper binds the current native child identity to one durable receipt and then returns the immutable declared body, including a custom declared skill path.

Normal managed `invoke`, `action`, `stage`, and `publish` commands never accept `--receipt`. Each carries the installed `--host`, `--expected-version`, and `--expected-composite` values, and the collector records the exact wrapper command before the child starts it. The current untagged T011 experimental source accepts exactly one action input source: the existing `--request <file>` or `--request-source native`. For the latter, only the trusted current Codex action-Pre collector may derive the DTO from the revalidated P0 and first persisted P1 prompt proof. Later same-turn records may contain completed commands, but may not add a user request, another turn, a fork, compaction, or an identity conflict. The collector writes a receipt-bound short-lived project packet. The child reads that packet with no-follow and digest checks, verifies installation, receipt, subject, request, and expiry bindings, then deletes it after use. Its current packet and deterministic temporary hard-link name both derive from the receipt, so consumption and the exact request's Stop cleanup do not depend on directory enumeration. A suite permits at most 64 ready native packets, including expired packets awaiting cleanup. Before a new action Pre reserves its packet, SELC directly cleans and terminalizes a bounded set of expired receipt-owned pairs; an exact request Stop also includes expired ready pairs and terminalizes a receipt only after both deterministic paths are absent. The separate 64-entry directory batch is best-effort cleanup for expired or orphaned owned artifacts. No host transcript path, raw prompt, model output, oracle, or policy body is persisted. The runtime selects the one matching pre-action receipt from the child’s native identity, including the exact activated task and call plus the real input, staged-byte, artifact, and target arguments. A cached entry with a different installation identity, command, input path, or host identity stops before business execution. Declared `instruction-staging-v1` actions use `managed stage` with bounded artifact and checker-input files; they do not require a user to ferry an opaque receipt.

An otherwise exact installed wrapper may have one literal `cd <absolute-current-project> &&` prefix. The directory must be unquoted, contain no `..` components, and resolve to the verified project; other shell combinations remain unsupported. Native evidence keeps the full original command digest, including this prefix.

Publication creates a previously absent target atomically from the checked staged bytes. On Darwin, a verified unchanged owned target can be corrected with a durable `renameatx_np(RENAME_SWAP)` intent: SELC verifies both exchanged objects before recording the delivery. A detected late user replacement remains uncertain and the displaced bytes are saved as a user-accessible conflict object. Filesystems without the exchange primitive report that capability limit; SELC does not stream-copy over an existing target.

The collector accepts a finite JSON request object, optionally with a genuine skill prefix. It records ordinary language and unsupported shapes as observed unknown classification rather than treating them as a collector outage: a raw prompt summary protects callback immutability, while only confirmed declared fields bind action input equality. Unknown semantics can execute and keep first-result assessment unknown. Replayed callbacks keep their original frozen transcript prefix even when native history later grows. A managed callback without a verified root, host version, native transcript prefix, or exact fixed wrapper command pauses before the action. The collector also records technical `PermissionRequest` callbacks without returning an allow, denial, retry, or permission change; an unmatched request remains unknown. The collector stores identifiers and hashes, never prompt, command, artifact, or native transcript-path bodies. Native request packet metadata retains only its version and digest alongside the existing receipt event identity. The immutable manifest must explicitly allow the observed host version; a missing or changed version pauses. Public fixture payloads remain unverified even if their fields claim a native host. The installed runtime owns `managed hook`; its other `managed` subcommands are coordination and local-fixture interfaces, while ordinary managed skill use never calls legacy `task`, `call`, `event`, or `delivery` record commands. Use `scripts/selc.py managed --help` to inspect those interfaces.

A user may request a correction in the normal Codex UI with structured skill JSON such as `@text-report {"input":"resources/items.json","selc":{"intent":"correct_delivery","target":"reports/item-summary.json","preserve_goal":true}}`. `selc` is a control declaration, not business input: SELC removes it before comparing the fixed input digest. The target is an owned project-relative path, not a task, call, or receipt identifier. A correction binds only when the original input bytes, checker basis, installation facts, and owned target all still match one closed failed publication. Natural-language correction requests and invalid `selc` values remain unknown.

For a supported automatic selection flow, the user supplies one structured business JSON object without naming a skill. SELC stores a hash of that complete object, removes only a top-level `selc` control, and requires the host's verified installed activation to select one declared skill. It never infers a skill from prose, a filesystem path, a shell discovery command, or user-provided trigger metadata. The selected skill, actual action input, checker basis, installation, and owned target remain independently verified; changing selector format alone does not change a correction's fixed goal. WorkBuddy's internal skill-chip transport remains host-only: for supported 5.8.3 and 5.9.2 callbacks it is exactly `@//item-summary/{"input":"resources/items.json"}`. Users should use the normal WorkBuddy skill UI rather than manually constructing that transport; other delimiters, trailing text, versions, hosts, and undeclared skill names remain unknown.

This implementation has local fixture coverage only. It does not claim completed Codex or WorkBuddy desktop discovery, body handoff, correction, root/window, or publication acceptance.

Export and import only minimal result samples, not business artifacts or feedback bodies:

```bash
selc export --project-root /tmp/selc-project --output /tmp/selc-snapshot.json
selc import --project-root /tmp/another-project --input /tmp/selc-snapshot.json
selc delete --project-root /tmp/another-project --suite-id simple-reports
selc delete --project-root /tmp/another-project --suite-id simple-reports --apply
```

`delete` without `--apply` previews its task and imported-sample impact. Imports fully validate the version and content hash before one transaction. Same origin revisions replace older imported samples; same revision with another hash fails; same task/call IDs from different origins count once and conflicts become `unknown`. A deletion records suppression IDs so a previously shared snapshot does not restore deleted samples. It cannot retract a file already shared elsewhere.

## US-004 — Check and explicitly update a suite

Actor: an installed-suite maintainer. The saved source baseline and framework version are in `.selc/installations.json`. Update checking never replaces an active suite.

Upgrading from v0.1.0 to the v0.2.0 framework changes the framework runtime only. The `simple-reports` business suite remains version 1.0.0, and a saved BASIC installation remains BASIC unless the maintainer explicitly changes its mode.

```bash
selc updates check --source /tmp/suite-repository --subdir suite --ref v1.1.0 --force
selc updates check --business-source /tmp/suite-repository --framework-source /tmp/framework-repository
selc updates apply --source /tmp/suite-repository --subdir suite --ref v1.1.0 \
  --project-root /tmp/selc-project --harness codex
selc updates apply --suite-id simple-reports --framework-source /tmp/framework-repository \
  --framework-ref v1.1.0 --project-root /tmp/selc-project --harness codex
selc updates recover --project-root /tmp/selc-project --suite-id simple-reports
```

Task entry reads each configured Git upstream at most once per UTC day and stores either a fresh result or a cache hit. A missing upstream is `unconfigured`; an upstream read failure is `unknown` and retains the usable installation. `updates check --force` always performs a new read but never applies it. `updates check --framework-source` reports its real framework descriptor with `applied: false`; `updates apply --framework-source` stages and validates the real replacement runtime before the atomic switch. `updates apply` checks the full business-framework composite before atomically replacing `current`; an active task or host session, a locally edited current suite, a malformed candidate, incompatible protocol, or registration conflict leaves current untouched. If an interrupted transaction leaves `journal.json`, inspect it and use `updates recover`; SELC never infers that a process exit ended a task or session. Successful replacement removes the prior content after current is activated. There is no local version-selection or rollback command.

Use standard `vX.Y.Z` tags for this repository's stable update path. The current selector excludes tags containing `-`; a namespaced tag such as `simple-item-reports-v1.1.0` needs an explicit `--ref` and is not discovered by the daily default check.

For a suite first installed from a directory, its saved source root and `subdir: "."` are restored by `updates apply --suite-id`; the framework-only command above therefore does not need a repeated `--source`. That command checks that the resolved source still matches the installed business version and digest; when changing business content, pass `--source` explicitly as in the first apply command. Selecting an external framework with bytes identical to the embedded runtime still records that explicit source selection. A later ordinary `install` without `--framework-source`, including a second-harness installation, and a later explicit business update continue to materialize that selected runtime until the maintainer explicitly selects another framework. SELC snapshots source bytes before it derives metadata or assembles a composite. An omitted external framework remains a request to reuse the saved selection: if its directory or package has changed, SELC returns `STALE_FRAMEWORK_SELECTION` and leaves the installation unchanged; repeat with an explicit `--framework-source` to select and validate the new runtime.

## US-005 — EXPERIMENTAL: Prepare, evaluate, contribute, and release

Actor: an installed-suite contributor or maintainer. This entire observed qualification and automation path is **EXPERIMENTAL**. A local `evolve evaluate` run is an explicitly labelled fixture diagnostic. It has `execution_type: fixture` and can never prepare or execute a contribution or release. `RECEIPT_UNAVAILABLE`, unknown evidence, an unsupported host or model, or missing desktop evidence leaves a candidate unqualified. Production qualification starts with a maintainer-owned case specification that declares each immutable version/variant, its supported scope, and its required case and matrix units. An initial integer-and-empty variant can therefore retain its own observed qualification while an improved decimal-and-cancellation variant supplies the additional required units; a candidate pass never authorizes a different snapshot.

Before running this example, set `SELC_CODEX_BINARY` to the resolved regular Codex executable and `SELC_CODEX_VERSION` to its exact reported version. Set `SELC_SKILL_CREATOR` to the available skill-creator directory, `SELC_CREATOR_PYTHON_ROOT` to the selected creator interpreter dependency root, and `SELC_EVALUATION_PYTHON_ROOT` to the narrow readonly Python dependency root needed by the fixed evaluation adapter. These are explicit authoring inputs.

```bash
selc evolve prepare --suite /tmp/suite-repository/suite --output /tmp/candidate
selc evolve policy-template --host-version "$SELC_CODEX_VERSION" \
  --python-read-root "$SELC_EVALUATION_PYTHON_ROOT" \
  --output /private/codex-policy-template.json
# Copy the complete generated object into the Codex matrix's tools_policy_template field.
selc evolve plan --suite /tmp/suite-repository/suite --cases /private/evaluation-cases.json \
  --output /private/evaluation-plan
selc evolve creator prepare --plan /private/evaluation-plan/plan.json \
  --candidate /tmp/candidate/candidate --output /private/creator-request.json \
  --binary "$SELC_CODEX_BINARY" --binary-version "$SELC_CODEX_VERSION" \
  --brief /private/creator-regression-brief.txt \
  --skill-creator-root "$SELC_SKILL_CREATOR" \
  --creator-python-root "$SELC_CREATOR_PYTHON_ROOT"
# A maintainer runs the fixed native creator profile and checks its native evidence.
selc evolve creator verify --request /private/creator-request.json --candidate /tmp/candidate/candidate \
  --native-evidence /private/creator-native-evidence.json
selc evolve bind --plan /private/evaluation-plan/plan.json --candidate /tmp/candidate/candidate \
  --variant improved --output /private/candidate-binding.json
selc evolve attempt prepare --plan /private/evaluation-plan/plan.json \
  --binding /private/candidate-binding.json --case regression-1 --matrix codex-cli \
  --attempt 1 --output /private/evaluation-attempt-1
selc evolve attempt native-evidence-template --attempt-path /private/evaluation-attempt-1
selc evolve attempt collect --attempt-path /private/evaluation-attempt-1
selc evolve finalize --plan /private/evaluation-plan/plan.json \
  --binding /private/candidate-binding.json --attempt-path /private/evaluation-attempt-1 \
  --output /private/evaluation.json
```

`evolve policy-template` writes a schema-3, versioned alpha.11 Codex template. It records the current POSIX account root from the OS, not `HOME`; requires a regular narrow Python dependency directory; and permits only two runtime substitutions in its native permission policy: the canonical isolated attempt project and one account-bound `.codex/tmp/arg0-macos/codex-arg0[A-Za-z0-9]+` bootstrap read path. Schema-1 and schema-2 templates remain accepted with their original profile, normalized object, and argv. The generated schema-3 template has this shape, with the actual generated `account_home` and Python root retained only in the maintainer's private plan:

```json
{
  "schema_version": 3,
  "host": "codex",
  "host_version": "0.163.0-alpha.11",
  "account_home": "<OS-derived-private-path>",
  "parameters": {
    "attempt_project": {"kind": "canonical-attempt-project"},
    "codex_arg0": {"kind": "current-account-alpha11-bootstrap"}
  },
  "policy": {"permission_profile": "<structured restricted profile>", "...": "..."},
  "cli": {"approval_policy": "never", "ignore_user_config": true, "settings": {"...": "..."}},
  "hook_trust": {"mode": "normal-project-and-hook-state-inheritance-v2", "...": "..."},
  "routing": {"parameter": "installed-registered-routing-entry", "renderer": "exact-single-cat-command-v1"},
  "preparation_guidance": {"content_kind": "generic.developer_instructions", "instruction_template": "<fixed routing command template>", "summary": "explicit-installed-routing-read-and-native-managed-action-v2"}
}
```

Use the complete command output, rather than this abbreviated example, as `matrix[].tools_policy_template`. The evaluator requires exact `permission_profile` and `file_system_sandbox_policy` filesystem entries, verifies that their entries are unique and identical, and rejects an additional root, network/tool/approval change, another account, or another bootstrap shape. After the suite is installed, schema 3 derives the only routing command from the frozen explicit `@skill + JSON` request, the unique declared fixed-script action, the registered `.agents/skills/<selected>/SKILL.md` link, and the current installation. It rejects automatic or multiple selection, a path or registration replacement, an entry/reader change, a directory, or a non-literal command before a host launch. The resulting non-business developer instruction includes exactly that `/bin/cat <absolute-routing-entry>` command; it runs the returned routing entry's literal managed `invoke` once, then uses the immutable returned business body only when it declares the bound fixed-script action with `--request-source native`, with no scratch request or early input read, one independent publish, and a stop after the first error or successful publish. It contains no business input, answer, oracle, or holdout. Collection requires one matching-turn initialization `generic.developer_instructions` item rendered from that exact command; its pathless `preparation_guidance` summary is not a tools-policy claim. The private attempt binding retains the path and entry/reader identities; public summaries do not. Schema 1 and schema 2 plans retain their original profile, literal, normalization, and argv. It saves the template hash and prelaunch mapping/configuration hash in every attempt, then records the matched native raw-policy hash and validated mapping during collection. An older `tools_policy_digest`-only Codex matrix remains diagnostic with `qualification_support: unverified`; it cannot authorize contribution or release. This schema-3 source behavior is current untagged T011 experimental code, not support claimed for the immutable v0.2.0 tag or released ZIP assets.

Before a `codex-cli` launch, the isolated project installs five managed Codex hooks and must already lie below one existing direct Git root. The adapter finds the nearest non-symlink `.git` directory upward from the canonical attempt cwd; `git rev-parse --show-toplevel`, `--git-dir`, and `--git-common-dir` must agree with that root and its `.git` directory. It never creates a `.git` directory or project trust. Review and trust that exact enclosing Git root and those exact definitions through Codex's normal UI. The adapter reads only that root's matching raw `projects` key with `trust_level = "trusted"` and the five matching `hooks.state` hashes from the OS account's `~/.codex/config.toml`. It rejects missing, untrusted, aliased, nested, worktree, changed, wrong-source, disabled, or extra relevant entries. A more-specific untrusted project decision is relevant only when it lies between the actual root and attempt cwd; an unrelated sibling entry neither blocks the attempt nor is copied. It then uses public inline-table `-c` settings for only `projects={"<original-key>"={trust_level="trusted"}}` and `hooks.state={"<source:key>"={trusted_hash="..."},...}`. It continues to use `--ignore-user-config`: no other account configuration is inherited or executed. The durable launch claim freezes the account, canonical cwd, Git identity, selected project key/value, config source SHA-256, hook definitions/states, and final argv; prelaunch rechecks the normal configuration, repository, and definitions. Later qualification reconstruction checks those frozen facts without requiring unrelated later account configuration to remain unchanged. The current project-and-hook inheritance template is version 2; an older hook-only plan cannot be upgraded or used as supported evidence.

`evolve plan` copies each exact input into the plan and fixes its trusted checker rules, full baseline runtime, framework, and `runtime/SELC-LICENSE`. Its JSON has `schema_version: 1`, bounded `candidate_budget`, `total_timeout_seconds`, `cases`, `matrix`, and `variants`. Each case names an input, frozen request, trusted checker, and run timeout; each matrix names one supported adapter, host/version, surface, model, versioned policy template, and derived qualification support. A `codex-cli` matrix also pins its regular executable and version. Every variant names its actual immutable manifest version, `baseline` or `candidate` snapshot, scope, required cases, and required matrices. The baseline version must equal the baseline manifest; a candidate snapshot is rewritten to its frozen variant version before binding. It creates a baseline binding when a declared initial version requires one. `evolve bind` materializes the full candidate snapshot before any attempt starts, and `--variant` selects the declared candidate version when a plan has more than one. `evolve finalize` accepts one `--binding` per reported version and lists any unverified variants instead of treating their scope as supported. The creator request pins the supported CLI binary and `--ignore-user-config`, binds a maintainer-owned UTF-8 regression brief into its prompt and run identity, explicitly denies the plan and protected materials, grants writes only to candidate skills and resources, and grants read access only to the explicit skill-creator root and Python runtime root. The brief must contain only the requested regression objective; it must not contain holdout, oracle, or protected-material paths. The skill-creator root must contain `SKILL.md` and `scripts/quick_validate.py`; the Python root covers that validator's selected interpreter dependencies. `creator verify` accepts only the matching native Codex session metadata, user turn, `exec` tool record, effective policy, frozen CLI version, and protected hashes. The real runner accepts only the finite `codex-cli` adapter. It keeps `--ignore-user-config`, then independently supplies only the original trusted actual-root `projects` entry and five normal-UI-reviewed hashes selected by the frozen `hook_trust` template; the post-run native policy remains the effective evidence. In the current untagged T011 experimental source, WorkBuddy 5.9.2 collection can report a bounded diagnostic from the preserved native session cutoff: the exact session/request, one consistent actual model and request model ID/name across its steps, the nested session-meta meta codebuddy.ai/hostKind field, and completed current-request tool calls. A call can follow same-step reasoning or a completed assistant, or it can follow the preceding completed result with a new message ID and a larger step. Function calls may omit status; only their paired results must be completed. Each call/result keeps its own message ID, step, parent chain, and dict or list result shape. It rejects changed locator identity, prompt, model, parent chain, record ID, call ID, result ID, invalid result completion state, metadata type, or serializer shape. The diagnostic never supplies `permission_mode`, initial tools, or an effective policy; WorkBuddy desktop attempts therefore remain `qualification_support: unverified` and collect as unknown. It neither infers a CLI surface from `providerData.agent` nor promotes matrix values to native evidence. This source behavior is not included in the immutable v0.2.0 tag or released ZIP assets. Desktop attempts use `desktop-request` followed by `collect`; collection never starts a model. `native-evidence-template` requires an original host-store JSONL cutoff with its SHA-256, path hash, device, and inode; collection recomputes every root, pre, and Post/Stop prefix from those bytes and binds the hook tool through its matching native `CommandExecution` result. Codex outer `exec` IDs and hook tool IDs are separate namespaces. Every attempt has an independent project, SQLite database, and session directory below its immutable plan. It is durably prepared before launch; launched, timed-out, cancelled, or unknown attempts consume their started budget and never automatically retry.

`evolve finalize` records a schema-v2 `real-harness` report only after it reconstructs production receipt, task, call, closed-window, recorded-publication, original native raw-session cutoff, model, effective policy, and checker evidence from each isolated SQLite database. Each qualification names its version scope, exact portable snapshot, required units, and status; the public summary separately lists unverified versions. Missing or uncertain evidence stays `unknown`; model prose, process exit status, output existence, fixture rows, or a report's `eligible` field cannot establish a pass. `evolve summary --report /private/evaluation.json` produces the pathless public summary. `contribute prepare`, `contribute execute`, `release prepare`, and `release execute` each re-run the same qualification. A later Git commit is accepted only when its complete portable tree, manifest, framework, runtime, MIT license, and qualified manifest version equal the evaluated candidate snapshot.

`contribute prepare` restores the saved source commit in an isolated workspace, creates a `codex/` non-default branch, maps only candidate skills/resources, and writes a real diff and package. `contribute execute` separately checks the staged index, worktree, and complete baseline-to-commit tree: only the prepared suite's skills, resources, and manifest may be committed. It rechecks the workspace manifest, runtime, wrapper, and business hashes before pushing that branch. With `--provider github|gitlab --repository <owner/repository> --client <controlled-client>`, the source provenance must record a public `github.com` or `gitlab.com` origin for that same repository; SELC verifies the namespace, provider, and host before it commits or pushes, then queries all matching PR/MR states and accepts reuse only for a matching open request. An inactive or incomplete response stops recovery. It rejects an unclassified self-hosted origin in v0.1. `contribute verify --plan <contribution-plan> --provider <github|gitlab> --client <controlled-client>` reloads that same bound identity and branch rather than resolving a client default such as `GH_HOST`.

If a local push or provider request is interrupted, rerun the same contribution plan. SELC accepts only its one verified contribution commit, rechecks the fixed remote branch before a request is reused, and verifies the provider response against the bound repository, head branch, base branch, and commit. An unknown provider response stops the recovery rather than creating another request. Local releases stage the package and full target metadata inside the bound center before an atomic tag publish; a partial or corrupt existing tag is reported for repair and is never overwritten.

A provenance-carrying package installed in a consumer project can prepare the same contribution without a development checkout:

```bash
selc_python /tmp/selc-project/.selc/installs/simple-reports/current/bin/selc-suite.py contribute prepare \
  --candidate /tmp/candidate/candidate --suite /tmp/selc-project/.selc/installs/simple-reports/current \
  --evaluation /tmp/evaluation.json --project-root /tmp/selc-project --suite-id simple-reports \
  --workspace /tmp/selc-contribution-workspace --output /tmp/contribution.json
```

Release confirmation binds every prepared field:

```bash
PLAN_HASH=$(selc_python -c 'import json; print(json.load(open("/tmp/release-plan.json"))["plan_hash"])')
selc release confirm --plan /tmp/release-plan.json --summary-hash "$PLAN_HASH" --output /tmp/release-confirmation.json
selc release execute --plan /tmp/release-plan.json --confirmation /tmp/release-confirmation.json --center /tmp/release-center
selc release verify --center /tmp/release-center --tag v1.1.0
```

Release preparation resolves `--target` to the source repository's full commit ID and requires that commit's business skills/resources to equal the evaluated candidate. It binds the framework descriptor, package, target, provider repository or local center, and tag into the hashed plan. Confirmation recomputes that hash, so changing any plan field while retaining an old `plan_hash` is rejected. Local execution rechecks the candidate, evaluation, package business content, target suite and framework before writing the center. Repeating a tag with the same package is idempotent; a different package fails. Any candidate, evaluation, target, package, center, repository, or tag change requires a new plan and confirmation. Local release does not activate local installs.

For a remote plan, execute with `release execute --plan <plan> --confirmation <confirmation> --client <controlled-client>`. It queries the explicit repository before creating the release and accepts reuse only when the tag target and downloaded named package asset both match the bound plan. An inconclusive query stops without creating anything; an ambiguous create response is queried once and is accepted only after the same target-and-asset verification. GitHub uses its explicit target and notes-file path; GitLab uses explicit ref, notes-file, asset download, and `--no-update`. The controlled fake clients in the tests verify both create/query paths. Real remote query, branch push, PR/MR creation, tag, or release mutation remains untested and needs separate authorization and existing provider login.

## Verification scope

The repository test suite creates temporary packages, local projects, local Git commits/tags, controlled fake provider clients, and a local release center. It checks creator-style authoring edits require an explicit seal, framework protocol/runtime tampering stays rejected, portable seal retains installed local-edit protection, dual-host registration, task/call metrics including imported call axes, strict unknown observation, feedback revision, snapshot replay/deletion suppression, fresh/cache/forced Git checks, actual business/framework composite upgrades and protocol rejection, active leases, candidate breakage, isolated contribution commits, target-content release binding, local release/reinstall, and GitHub/GitLab adapter execution. Run it with:

```bash
UV_CACHE_DIR=/private/tmp/selc-uv-cache uv run --no-project --python 3.12 python -m unittest discover -s tests -v
```
