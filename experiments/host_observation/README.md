# Isolated host-observation probe

This is a private `probe-v1` spike for a single prepared workspace. It is not SELC runtime,
a daemon, a production schema, or evidence that a host observes every execution path.

Use Python 3.11 and create one empty workspace per host:

```sh
/opt/homebrew/bin/python3.11 experiments/host_observation/probe.py prepare \
  --host codex --workspace /private/tmp/selc-host-probe-codex
/opt/homebrew/bin/python3.11 experiments/host_observation/probe.py prepare \
  --host workbuddy --workspace /private/tmp/selc-host-probe-workbuddy
```

`prepare` creates a nonce, a private SQLite database, a `.claude/skills/host-observation-probe`
skill, and the host hook configuration. The Codex fixture creates the required relative
`.agents/skills/host-observation-probe` link; the WorkBuddy fixture creates the corresponding
relative `.codebuddy/skills/host-observation-probe` link. Open the generated workspace in the
intended host, review and trust its generated configuration, then invoke the skill naturally. Its
body contains one business command only; it never instructs an agent to invoke `receive` or
lifecycle record commands. The business command writes `result.txt` only after a matching native
receipt exists.

Inspect generated configuration and evidence with:

```sh
/opt/homebrew/bin/python3.11 experiments/host_observation/probe.py inspect \
  --workspace /private/tmp/selc-host-probe-codex
```

For parser-only testing, an explicit synthetic event can be sent to `receive` with
`--source synthetic`. Synthetic events are stored as such and can never authorize `execute`. `execute`
requires the fixture nonce, runs only from the fixture root, consumes a one-time receipt, and
accepts a receipt for at most 60 seconds.

Generated Codex configuration uses the documented project `.codex/hooks.json` shape, matcher
`^Bash$`, and a synchronous command hook. It must be natively trusted; do not use trust bypasses.
The generated WorkBuddy `.codebuddy/settings.json` mirrors the hook structure observed in the
bundled WorkBuddy 5.7.6 CodeBuddy CLI (`settingsManager.get("hooks")`, `hooks[event]`, matcher,
and command hook entries), including `PreToolUse` input fields. The desktop experiment below
confirms this configuration for the specific tested path; other desktop paths remain unverified.

The receiver accepts only JSON hook events whose `cwd` is inside the prepared fixture. A native
receipt further requires `PreToolUse`, a supported shell tool name, a stable native tool ID, and
an exact shell argv match to the generated `execute` command. It stores raw payloads synchronously
with their source type and arrival ID. Without a stable native ID, it stores the arrival but never
uses a raw hash as a de-duplication substitute. A scan command creates no receipt or invocation.

## Observed host scope

On 2026-10-20, an explicit skill request in Codex CLI 0.147 and a local-file request in WorkBuddy
desktop 5.7.6 (official GLM-5.3) were exercised, followed by a collector-disabled case on each.
Each enabled host produced two
native `PreToolUse` events (one business command and one non-business scan), one receipt, one
automatic invocation, and a matching result digest. In separate disabled workspaces, the business
command exited with `MISSING_NATIVE_RECEIPT` and left no result or invocation.

This does not validate Codex desktop, automatic skill selection, subagents, or one-shot success.
WorkBuddy's home-page slash menu did not expose this skill; the observed path requested reading the
local `SKILL.md` and then executed its business command. It does not establish slash registration
or a WorkBuddy Skill-tool invocation. These observations used no `task`, `call`, or `delivery`
hand-entry steps.

Run local verification:

```sh
/opt/homebrew/bin/python3.11 -m unittest discover \
  -s experiments/host_observation/tests -p 'test_*.py'
```

When the repository's `ruff` executable is installed, also run
`ruff check experiments/host_observation`. The current probe environment has no `ruff` binary.
