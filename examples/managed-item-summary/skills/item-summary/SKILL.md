---
name: item-summary
description: Publish a managed count-and-sum JSON report for an input items array with integer values.
---

# Item summary

Use this skill for an input JSON DTO whose declared input has an `items` array of integer `value` fields. Repeated items each count; an empty array produces `{"count": 0, "sum": 0}`. Integers are the complete supported numeric scope: do not accept decimals, floats, missing values, or other numeric forms.

## Managed native-request flow

This is a new creator-authored instruction tree for the experimental managed native-request interface. The underlying count-and-sum implementation and the original user DTO are unchanged.

The installer-owned routing `SKILL.md` contains the one literal `managed invoke` command for frozen creator run `bba56ff804585ff607193ba818c1210e1da3d34b23cadae2b493cab93d69de52`. Execute that command once, exactly as installed. Do not alter it or perform discovery. From its result, retain its exact interpreter, project root, host, expected version, expected composite, `task_id`, `call_id`, and immutable `skill_body_path`. Derive the suite root from that returned immutable body path.

On Codex, do not create, read, or transform a scratch request file. Make exactly one separate managed action command for `item-summary`, with all identity and expected-version/composite arguments copied literally from the invoke result and with `--request-source native`. The managed runtime receives the unchanged structured user JSON and reads its declared input. Read `managed.artifact_path` from that action result.

Next, make exactly one separate managed publish command using that returned artifact path and target `reports/item-summary.json`; retain the same literal identity, host, expected-version, and expected-composite arguments. These are the only action and publication commands.

Do not invoke `resources/item_summary.py` directly; write an artifact or report outside the managed wrapper; register records manually; change hooks; retry a wrapper; add logging after publication; or use shell variables, environment prefixes, compound commands, or extra discovery. Stop at the first wrapper error, and stop after a successful publish.

On WorkBuddy, native request packets are unavailable. Use an explicit existing `--request <request.json>` path only when the caller has already supplied that file; do not claim native mode works there. When an automatic observed request has no supplied request file, recommend the ordinary BASIC entry instead.
