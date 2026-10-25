---
name: item-summary
description: Publish a managed count-and-sum JSON report for an input items array with integer values.
---

# Item summary

Use this skill when a JSON input file has an `items` array with integer `value` fields and needs a compact managed count-and-sum report. Repeated items each count; an empty array produces `{"count": 0, "sum": 0}`. This capability supports integers only, not decimals, floats, missing values, or other numeric forms.

## Use the active managed identity

The installer-owned discovery entry has already run `managed invoke` once. From that **exact activation command**, reuse its interpreter, project root, host, expected version, and expected composite; from its result, use only the returned `task_id`, `call_id`, and `skill_body_path`.

Derive `SUITE_ROOT` by removing `/skills/item-summary/SKILL.md` from `skill_body_path`. Do not invent identity values or manually register task, call, event, delivery, or receipt data.

For a user-supplied JSON request DTO containing `input`, preserve that DTO unchanged in a project scratch request JSON file outside the immutable installation. Use its real `input` value; never hard-code input data, expected answers, or evaluation paths.

## Run and publish

Make exactly this native shell action call, replacing every placeholder with the current literal value:

```sh
<INVOKE_INTERPRETER> <SUITE_ROOT>/bin/selc-suite.py managed action --project-root <PROJECT_ROOT> --suite simple-item-reports --skill item-summary --task-id <TASK_ID> --call-id <CALL_ID> --action item-summary --request <SCRATCH_REQUEST_JSON> --host <HOST> --expected-version <EXPECTED_VERSION> --expected-composite <EXPECTED_COMPOSITE>
```

Read `managed.artifact_path` from that result. Then make this separate native shell publication call:

```sh
<INVOKE_INTERPRETER> <SUITE_ROOT>/bin/selc-suite.py managed publish --project-root <PROJECT_ROOT> --suite simple-item-reports --skill item-summary --task-id <TASK_ID> --call-id <CALL_ID> --artifact <ACTION_ARTIFACT_PATH> --target reports/item-summary.json --host <HOST> --expected-version <EXPECTED_VERSION> --expected-composite <EXPECTED_COMPOSITE>
```

The managed action owns the installed implementation, frozen request, and artifact output path. Do not call `resources/item_summary.py` directly, write the formal report yourself, edit installed data, hooks, or records, or reshape/combine the wrapper commands. Stop immediately after the first wrapper error without retrying, or after successful publication. Keep normal skill discovery enabled.
