#!/usr/bin/env python3
"""Create an exact count-and-sum report for JSON-number-valued items."""

import argparse
from decimal import MAX_EMAX, MIN_EMIN, Decimal, localcontext
import json
from pathlib import Path


def load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as source:
        return json.load(
            source,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=reject_constant,
        )


def reject_constant(value: str) -> None:
    """Reject JavaScript-style non-finite constants, which JSON does not support."""
    raise ValueError(f"unsupported JSON numeric constant: {value}")


def exact_sum(values: list[Decimal]) -> Decimal:
    """Add finite decimal literals with sufficient precision for their exact sum."""
    if not values:
        return Decimal(0)

    min_exponent = min(value.as_tuple().exponent for value in values)
    max_adjusted = max(value.adjusted() for value in values)
    # Alignment can require every place from the lowest exponent to the highest
    # adjusted digit. A further digit allows for carries while adding all items.
    precision = max_adjusted - min_exponent + 1 + len(str(len(values)))
    with localcontext() as context:
        context.prec = max(1, precision)
        context.Emax = MAX_EMAX
        context.Emin = MIN_EMIN
        return sum(values, Decimal(0))


def build_report(input_path: Path) -> dict[str, int | Decimal]:
    payload = load_json(input_path)
    if not isinstance(payload, dict):
        raise ValueError("input JSON must be an object")

    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("input JSON must contain an items array")

    values: list[Decimal] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{index}] must be an object")
        value = item.get("value")
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"items[{index}].value must be a finite JSON number")
        values.append(value)

    return {"count": len(items), "sum": exact_sum(values)}


def report_json(report: dict[str, int | Decimal]) -> str:
    """Serialize the report without converting its exact Decimal total to float."""
    total = report["sum"]
    if isinstance(total, Decimal):
        if total.is_zero():
            sum_json = "0"
        else:
            sum_json = str(total)
    else:
        sum_json = str(total)
    return f'{{"count": {report["count"]}, "sum": {sum_json}}}'


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
        output.write(report_json(report))
        output.write("\n")


if __name__ == "__main__":
    main()
