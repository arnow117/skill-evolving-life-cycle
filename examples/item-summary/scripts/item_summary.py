#!/usr/bin/env python3
"""Create a count-and-sum report for integer-valued JSON items."""

import argparse
import json
from pathlib import Path


def load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def build_report(input_path: Path) -> dict[str, int]:
    payload = load_json(input_path)
    if not isinstance(payload, dict):
        raise ValueError("input JSON must be an object")

    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("input JSON must contain an items array")

    total = 0
    for index, item in enumerate(items):
        if not isinstance(item, dict) or type(item.get("value")) is not int:
            raise ValueError(f"items[{index}].value must be an integer")
        total += item["value"]

    return {"count": len(items), "sum": total}


def main() -> None:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    managed_run = subcommands.add_parser("managed-run")
    managed_run.add_argument("--request", type=Path, required=True)
    managed_run.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    request = load_json(arguments.request)
    if not isinstance(request, dict) or not isinstance(request.get("input"), str):
        raise ValueError("request JSON must contain an input filename")

    report = build_report(Path(request["input"]))
    with arguments.output.open("w", encoding="utf-8") as output:
        json.dump(report, output)
        output.write("\n")


if __name__ == "__main__":
    main()
