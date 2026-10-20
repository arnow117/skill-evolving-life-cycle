# SELC v0.1

SELC creates and maintains a versioned suite of skills, then records task and invocation outcomes locally. The authoring skill at `.claude/skills/selc/` is for Codex. A generated suite installs into Codex and WorkBuddy without requiring this repository, a global `selc` command, or Codex at the WorkBuddy installation site.

`scripts/selc.py` is the only global usage entrypoint. Its argparse schema is authoritative; use `selc --help` or `selc <command> --help` for the current parameter list. Runtime dependencies are Python 3.11+ and the standard library. `uv` manages the independent command environment; no dependency is installed implicitly.

This v0.1 validates local filesystem, SQLite, local Git, a local release center, and controlled fake GitHub/GitLab clients. It does not claim tested Codex or WorkBuddy desktop hooks, real GitHub/GitLab credentials, real remote mutation, or real model calls. AiTaxs is not included in this repository or example.

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

## US-001 — Create a suite

Actor: a Codex skill author. Preconditions: Python 3.11+ and an empty output path. The author owns the suite ID, version, and skill names.

```bash
SELC_CREATOR_PYTHON=/opt/homebrew/bin/python3.11
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

Actor: a suite user. Preconditions: a package produced by US-001, an empty or compatible project root, and Python 3.11+.

```bash
selc install --source /tmp/simple-reports.zip \
  --project-root /tmp/selc-project --harness codex
selc install --source /tmp/simple-reports.zip \
  --project-root /tmp/selc-project --harness workbuddy
selc_python /tmp/selc-project/.selc/installs/simple-reports/current/bin/selc-suite.py run text-report \
  --input examples/text-report/input.txt --output /tmp/selc-project/report.md
```

The installation result identifies the installed version, digest, source baseline, registered skills, and maintenance guide. Directory, Git, and package installs copy the same explicit portable code/resource surface, not a source repository's `.git`, `.selc`, reports, or interpreter cache. Codex entries are registered below `.agents/skills/`; WorkBuddy entries are registered below `.codebuddy/skills/`. The portable run produces a Markdown report. The installed wrapper also supports `seal`, `validate`, `package`, and `install-local` without this repository. Use `seal` only for an authoring source after creator validation; use `evolve` for installed-suite improvements.

Use `--source <repo> --subdir <relative-suite-dir> --ref <tag-or-commit>` for a local or remote Git suite source. Without `--ref`, a Git source selects the latest stable tag. An explicit `--framework-source` with optional `--framework-ref` or `--framework-subdir` reads `framework.json`, validates runtime protocol 1 and runtime hash, and builds a complete staged composite: business skills/resources are retained while the portable runtime is replaced. Its source, commit, version, protocol, and digest are saved. A later business update preserves that explicit framework; an embedded installation takes its framework from the new business package. Same source and digest reuse the installation only after verifying its content is unchanged. A different source with the same suite name, a conflicting harness skill name, unsafe subdirectory, incompatible framework protocol, or local edit fails with an explicit code. Preserve the local change or choose another suite name before retrying.

## US-003 — Record and aggregate a task

Actor: a harness adapter or user with task evidence. A task ID is the business-operation identity; each invocation gets a separate call ID. `initiator` and `executor` are separate fields. `--test` puts controlled fixtures outside default reports. An installed call must supply the skill entry's `--expected-version` and `--expected-hash`; SELC never fills a missing digest from the current installation, so a stale or incomplete entry cannot mix with a new runtime.

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

## US-003A — Diagnose a declared artifact check

An optional schema-1 `suite.json` `artifact_checks` object declares a fixed checker identity, rule version, required rule IDs, JSON configuration, and an optional OS-network-isolation requirement. A skill entry can name one declaration with `artifact_check`. Declarations contain no executable path, argv, or environment values. A missing declaration, unsupported rule, installed-byte drift, source change, unavailable checker, or manifest digest pin mismatch cannot produce a pass.

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

The runtime also has an internal Python observation store for future managed host adapters: `selc.observation.ingest`, `replay`, `timeline`, and `assessment`. It accepts only a bounded v1 semantic DTO, persists its allowlisted identifiers, hashes, classification facts, and opaque evidence references before projection, and derives formal results from that evidence. Replay also restores assessments affected by persisted pending, quarantined, or conflicting evidence after a crash. A formal first pass requires complete execution evidence; a verifier-bound final pass can remain valid when unrelated execution evidence is incomplete, while a verified failure remains fail. Conflicted final-delivery evidence and publication gaps cannot establish a pass. There is no CLI input, automatic host discovery, managed receipt transport, or claim that either desktop host has complete observation coverage yet.

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

For a suite first installed from a directory, its saved source root and `subdir: "."` are restored by `updates apply --suite-id`; the framework-only command above therefore does not need a repeated `--source`. That command checks that the resolved source still matches the installed business version and digest; when changing business content, pass `--source` explicitly as in the first apply command. Selecting an external framework with bytes identical to the embedded runtime still records that explicit source selection. A later ordinary `install` without `--framework-source`, including a second-harness installation, and a later explicit business update continue to materialize that selected runtime until the maintainer explicitly selects another framework. SELC snapshots source bytes before it derives metadata or assembles a composite. An omitted external framework remains a request to reuse the saved selection: if its directory or package has changed, SELC returns `STALE_FRAMEWORK_SELECTION` and leaves the installation unchanged; repeat with an explicit `--framework-source` to select and validate the new runtime.

## US-005 — Prepare, evaluate, contribute, and release

Actor: an installed-suite contributor or maintainer. Candidate code is isolated from the trusted runner, rules, and holdout cases. This v0.1 runner has two regression and two holdout text cases; it runs the baseline and candidate in the same deterministic environment, with at most two candidates, eight runs, 10 seconds per runner, 60 seconds total, and zero real model calls.

```bash
selc evolve prepare --suite /tmp/suite-repository/suite --output /tmp/candidate
# Follow the creator brief, then make an explicit candidate-only proposal.
printf '\nCreator-approved report wording.\n' >> /tmp/candidate/candidate/skills/text-report/SKILL.md
selc evolve evaluate --candidate /tmp/candidate/candidate --output /tmp/evaluation.json
selc contribute prepare --candidate /tmp/candidate/candidate \
  --suite /tmp/suite-repository/suite --evaluation /tmp/evaluation.json \
  --source-repo /tmp/suite-repository --workspace /tmp/selc-contribution-workspace \
  --output /tmp/contribution.json
selc contribute execute --plan /tmp/contribution.json --provider local
selc release prepare --suite /tmp/suite-repository/suite --candidate /tmp/candidate/candidate \
  --evaluation /tmp/evaluation.json --provider local --target <contribution-commit> --tag v1.1.0 \
  --center /tmp/release-center --output /tmp/release-plan.json
```

`evolve prepare` only creates a creator brief; it does not claim an LLM changed the candidate. Follow that brief to make an explicit candidate-only edit such as the command above; an unchanged candidate is rejected with `NO_CONTRIBUTION_CHANGES`. An invalid candidate resource produces a failed report. `--model-calls` must remain zero in v0.1. Holdout details are not copied to the creator brief. `contribute prepare` restores the saved source commit in an isolated workspace, creates a `codex/` non-default branch, maps only candidate skills/resources, and writes a real diff and package. `contribute execute` separately checks the staged index, worktree, and complete baseline-to-commit tree: only the prepared suite's skills, resources, and manifest may be committed. It rechecks the workspace manifest, runtime, wrapper, and business hashes before pushing that branch. With `--provider github|gitlab --repository <owner/repository> --client <controlled-client>`, the source provenance must record a public `github.com` or `gitlab.com` origin for that same repository; SELC verifies the namespace, provider, and host before it commits or pushes, then queries all matching PR/MR states and accepts reuse only for a matching open request. An inactive or incomplete response stops recovery. It rejects an unclassified self-hosted origin in v0.1. `contribute verify --plan <contribution-plan> --provider <github|gitlab> --client <controlled-client>` reloads that same bound identity and branch rather than resolving a client default such as `GH_HOST`.

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
