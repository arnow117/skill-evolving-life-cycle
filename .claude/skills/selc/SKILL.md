---
name: selc
description: Create, package, install, observe, and evolve a portable SELC skill suite from Codex. Use for local suite lifecycle work; generated suites also support WorkBuddy.
---

# SELC suite lifecycle

Use the repository-local runtime at `scripts/selc.py`. Read the root `README.md` for the current command syntax, scenario IDs, parameters, and verified local journey.

Create and maintain a suite as a single versioned unit. Each declared skill keeps its own entry directory. The generated suite contains its runtime, installer, maintenance guide, and both Codex and WorkBuddy registration targets; do not add an absolute local skill path to generated files.

When creating or substantially editing a skill body, use the available `skill-creator` initializer and validator in the active Codex environment. Its optional PyYAML dependency belongs only to that creator environment. The portable suite runtime must remain Python standard-library only.

For a newly created authoring source, use this sequence: `create`, let skill-creator create or update the declared `SKILL.md` body and resources, run its validator, then run `seal --suite <authoring-source>`, `validate`, and `package`. `seal` explicitly refreshes only the source suite's content hash after checking its portable files and existing framework runtime hash. It does not execute business code, install the suite, or treat an installed local edit as a clean baseline. Installed-suite changes continue through an isolated `evolve` candidate.

Use `task`, `call`, `delivery`, and `feedback` only for actual lifecycle evidence. Ordinary agent reports cannot establish complete host observation: use `unknown` unless the built-in `text-report-v1` artifact check or a test-only fixture supplies reliable evidence. Never store artifact text or feedback text in an exported snapshot.

`updates apply`, `contribute`, and `release` are explicit operations. Keep an installed suite active while tasks or sessions are active; resolve local edits and journal recovery before replacement. Candidate evaluation must leave the oracle and holdout material outside the candidate. A release requires a newly matching final confirmation after the candidate, evaluation, target, and package are fixed.

Do not execute real remote provider mutations or real model batches without separate authorization. Use local fixtures for the documented verification path.
