---
name: "item-summary"
description: item-summary ordinary-use entry from the simple-item-reports suite.
---

# item-summary

Use this independent BASIC entry for ordinary local execution. It has no managed hook or receipt prerequisite. With a Python 3.11+ interpreter and an already supplied request file, run the installed Python script's ordinary managed-run interface:

```sh
<python3.11-or-newer> <suite-root>/resources/item_summary.py managed-run --request <request.json> --output <artifact>
```

Keep the caller's request JSON unchanged. The declared input supports an `items` array whose `value` fields are integers only; repeated items count separately, and an empty array returns `{"count": 0, "sum": 0}`. Do not broaden this to decimals, floats, missing values, or other numeric forms.
