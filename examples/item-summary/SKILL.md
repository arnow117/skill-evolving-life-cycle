---
name: item-summary
description: Summarize a JSON items array with integer values into a count-and-sum JSON report.
---

# Item summary

Use this skill to summarize a JSON file containing an `items` array. Every entry must have an integer `value`; repeated entries each count. Empty input produces `{"count": 0, "sum": 0}`. Decimal values are outside this version's scope.

Read the user's request object with an `input` filename. Preserve that object in a request JSON file in the current project. Resolve the input filename relative to the project working directory. Ask for a missing input rather than inventing data.

Resolve this skill directory from the actual loaded SKILL.md location. Choose an output file in the project, using the user's requested path or `reports/item-summary.json`, and create its parent directory if needed. Invoke the bundled script with Python 3.11 or newer from the project working directory:

```sh
python3 <SKILL_DIRECTORY>/scripts/item_summary.py managed-run --request <REQUEST_JSON> --output <OUTPUT_JSON>
```

Quote paths as needed. Read the resulting JSON and report its path, `count`, and `sum`. If the script fails, report the error and do not present an old output as a new result. Keep inputs and installed skill files unchanged.

This ordinary skill performs local file processing. It requires no SELC installation, hooks, native receipt, task ID, or automatic statistics configuration.
