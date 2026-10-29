---
name: "item-summary"
description: Run the simple-item-reports item summary locally from an already supplied request file.
---

# Item summary — BASIC

Use this independent BASIC entry for ordinary local execution, including WorkBuddy when an observed automatic request has no explicit request file. It has no managed hook or receipt prerequisite.

With Python 3.11 or newer and an already supplied request file, use the installed script's ordinary interface:

```sh
<python3.11-or-newer> <suite-root>/resources/item_summary.py managed-run --request <request.json> --output <artifact>
```

Keep the request JSON unchanged. The implementation handles an `items` array with finite JSON-numeric `value` fields, counts repeated items separately, preserves exact JSON-decimal arithmetic (including integers and numeric cancellation), and emits `{"count": 0, "sum": 0}` for an empty array. Do not add managed invocation, hooks, receipts, or publication steps to this BASIC path.
