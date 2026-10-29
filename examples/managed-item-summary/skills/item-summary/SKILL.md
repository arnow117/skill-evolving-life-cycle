---
name: item-summary
description: Publish a managed count-and-sum JSON report for an input items array with exact JSON numeric values.
---

# Item summary

Use this observed managed entry to publish the report for a request DTO whose declared `input` identifies the source JSON. The installed implementation is authoritative: it counts every item (including repeated identifiers) and sums finite JSON-decimal `value` fields exactly. It preserves integer results, returns `{"count": 0, "sum": 0}` for an empty array, and avoids binary-floating-point loss during large decimal cancellation. Invalid or non-finite values are reported by the managed action; do not construct a report yourself.

## Use the completed activation

This business body has already been returned by one successful `managed invoke`. Do not run `managed invoke` again. Reuse the exact interpreter, project root, host, expected version, and expected composite from that original literal routing command. Use only the returned `task_id`, `call_id`, and immutable `skill_body_path` for this activation.

Derive `SUITE_ROOT` by removing `/skills/item-summary/SKILL.md` from the returned `skill_body_path`. The angle-bracket names below are placeholders to replace with current literal values; they are not shell variables. Do not discover, guess, or register an identity.

## Codex: run and publish

Make exactly one native shell action call, replacing each placeholder with its current literal value:

```sh
<INVOKE_INTERPRETER> <SUITE_ROOT>/bin/selc-suite.py managed action --project-root <PROJECT_ROOT> --suite simple-item-reports --skill item-summary --task-id <TASK_ID> --call-id <CALL_ID> --action item-summary --request-source native --host <HOST> --expected-version <EXPECTED_VERSION> --expected-composite <EXPECTED_COMPOSITE>
```

The runtime obtains the original structured user JSON unchanged and reads its declared input. Do not create a scratch request or read the business input yourself. Read `managed.artifact_path` from the successful action result, then make this one separate native shell publication call:

```sh
<INVOKE_INTERPRETER> <SUITE_ROOT>/bin/selc-suite.py managed publish --project-root <PROJECT_ROOT> --suite simple-item-reports --skill item-summary --task-id <TASK_ID> --call-id <CALL_ID> --artifact <ACTION_ARTIFACT_PATH> --target reports/item-summary.json --host <HOST> --expected-version <EXPECTED_VERSION> --expected-composite <EXPECTED_COMPOSITE>
```

Stop at the first wrapper error and after successful publication. Do not retry, call the resource script directly, write an artifact or report yourself, add post-publication logs, modify hooks or records, or manually register lifecycle data. Keep each wrapper command separate, with no shell variables, environment prefixes, command chaining, or extra discovery.

## WorkBuddy observed use

WorkBuddy does not provide native request packets. Only when the caller already supplied a request file, use the action template above with `--request <SUPPLIED_REQUEST_JSON>` in place of `--request-source native`, preserving all other current activation values; publish separately as above. If no request file was supplied, recommend the independent BASIC entry. BASIC has no managed activation, hook, receipt, or publication prerequisite.
