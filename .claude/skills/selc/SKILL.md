---
name: selc
description: Create, package, install, observe, and evolve a portable SELC skill suite from Codex. Use for local suite lifecycle work; generated suites also support WorkBuddy.
---

# SELC suite lifecycle

Resolve this skill's repository root before running commands; the chat may be open in a different directory. Use that repository's runtime at `scripts/selc.py`. Read its root `README.md` for the current command syntax, scenario IDs, parameters, and verified local journey.

The automatic-observation branch is a development preview. Local authoring and packaging are available; generic managed generation, real dual-host acceptance, and the public release workflow remain unfinished. State that scope when asked to deploy a newly generated skill. Read the current SDLC product and engineering context before continuing the tracked implementation; do not treat local fixtures as host acceptance.

Create and maintain a suite as a single versioned unit. Each declared skill keeps its own entry directory. The generated suite contains its runtime, installer, maintenance guide, and both Codex and WorkBuddy registration targets; do not add an absolute local skill path to generated files.

When creating or substantially editing a skill body, use the available `skill-creator` initializer and validator in the active Codex environment. Its optional PyYAML dependency belongs only to that creator environment. The portable suite runtime must remain Python standard-library only.

For a newly created authoring source, use this sequence: `create`, let skill-creator create or update the declared `SKILL.md` body and resources, run its validator, then run `seal --suite <authoring-source>`, `validate`, and `package`. `seal` explicitly refreshes only the source suite's content hash after checking its portable files and existing framework runtime hash. It does not execute business code, install the suite, or treat an installed local edit as a clean baseline. Installed-suite changes continue through an isolated `evolve` candidate.

Ordinary skill execution uses the installer-owned managed entry, automatic native receipts, and checked publication. Do not ask a skill user to run `task`, `call`, `event`, or `delivery` to make statistics work; those legacy interfaces serve evidence diagnostics and explicit fixtures. Artifact checks establish business results separately from native observation: a complete host interval is also required for one-shot success, and a fixture cannot establish production completeness. Missing or ambiguous evidence remains `unknown`. Never store artifact text or feedback text in an exported snapshot.

`updates apply`, `contribute`, and `release` are explicit operations. Keep an installed suite active while tasks or sessions are active; resolve local edits and journal recovery before replacement. Candidate evaluation must leave the oracle and holdout material outside the candidate. A release requires a newly matching final confirmation after the candidate, evaluation, target, and package are fixed.

Use existing authorization for real remote provider mutations and model runs within its scope; request clarification only for a new external effect or missing permission. Local fixtures support regression checks, while real host and remote acceptance require their own observed evidence. Formal release still needs the final confirmation bound to the exact reviewed artifacts.
