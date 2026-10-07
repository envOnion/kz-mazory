import json
from decimal import Decimal, InvalidOperation

from ..providers import ProviderUnavailable
from .data import fail, validate
from .text import financial_values

ENCODINGS = ["category", "series", "value", "x", "y", "label"]
PRESENTATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["version", "title", "blocks"],
    "properties": {
        "version": {"const": "1.0"},
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "summary": {"type": "string", "maxLength": 2000},
        "blocks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "kind", "dataset_id"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,64}$"},
                    "kind": {
                        "enum": [
                            "bar",
                            "line",
                            "area",
                            "scatter",
                            "donut",
                            "funnel",
                            "waterfall",
                            "table",
                            "kpi",
                        ]
                    },
                    "dataset_id": {"type": "string", "maxLength": 64},
                    "title": {"type": "string", "maxLength": 200},
                    "size": {"enum": ["wide", "half", "third"]},
                    "encoding": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            k: {"type": "string", "maxLength": 64} for k in ENCODINGS
                        },
                    },
                    "columns": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 10,
                        "uniqueItems": True,
                        "items": {"type": "string", "maxLength": 64},
                    },
                },
            },
        },
    },
}


def invalid(message):
    raise ProviderUnavailable(
        "presentation_invalid", diagnostics={"validation_errors": [message]}
    )


def build(context, arguments):
    context.check()
    validate(PRESENTATION_SCHEMA, arguments)
    used = {}
    blocks = []
    ids = set()
    for block in arguments["blocks"]:
        if block["id"] in ids:
            invalid("Block id must be unique within this presentation.")
        if block["dataset_id"] not in context.registry:
            invalid(
                "dataset_id must match a query_dataset result in this request. "
                f"Available dataset IDs: {list(context.registry)}."
            )
        ids.add(block["id"])
        dataset = context.registry[block["dataset_id"]]
        columns = {c["name"]: c for c in dataset["columns"]}
        encoding = dict(block.get("encoding", {}))
        kind = block["kind"]
        # Cartesian x/y are common native-model output and already part of the
        # tool schema. Normalize only explicit, nonconflicting column references.
        if kind in ["bar", "line", "area"]:
            for axis, channel in [("x", "category"), ("y", "value")]:
                if axis in encoding:
                    if channel in encoding and encoding[channel] != encoding[axis]:
                        invalid(
                            f"Encoding {axis} and {channel} must reference the same column."
                        )
                    encoding[channel] = encoding.pop(axis)
        required = {
            "bar": ["category", "value"],
            "line": ["category", "value"],
            "area": ["category", "value"],
            "scatter": ["x", "y"],
            "donut": ["category", "value"],
            "funnel": ["category", "value"],
            "waterfall": [],
            "table": [],
            "kpi": ["value"],
        }[kind]
        allowed = required + (
            ["series"]
            if kind in ["bar", "line", "area"]
            else ["label"]
            if kind == "scatter"
            else []
        )
        if (
            not set(required) <= encoding.keys()
            or not set(encoding) <= set(allowed)
            or any(name not in columns for name in encoding.values())
        ):
            invalid(
                f"For {kind}, required encoding keys: {required}; allowed keys: {allowed}. "
                f"Use exact column names: {list(columns)}. Received encoding: {encoding}."
            )
        for channel in ["value", "x", "y"]:
            if channel in encoding and columns[encoding[channel]]["type"] not in [
                "money",
                "count",
                "percent",
            ]:
                invalid(
                    f"Encoding {channel} must reference a money, count or percent column."
                )
        if kind == "table" and (
            not block.get("columns") or not set(block["columns"]) <= columns.keys()
        ):
            invalid(
                f"Table columns must select existing column names: {list(columns)}."
            )
        if kind != "table" and "columns" in block:
            invalid("Only table blocks accept columns; charts use encoding.")
        if kind == "kpi" and len(dataset["rows"]) != 1:
            invalid(
                "KPI requires exactly one dataset row. Aggregate or choose a chart/table."
            )
        if kind in ["donut", "funnel"] and any(
            row[encoding["value"]] is not None
            and Decimal(str(row[encoding["value"]])) < 0
            for row in dataset["rows"]
        ):
            invalid(
                "Donut and funnel values must be nonnegative. Choose bar/line for signed values."
            )
        if kind == "funnel" and encoding.get("category") != "status":
            invalid(
                "Funnel category must be the status column. Choose bar for other categories."
            )
        if kind in ["bar", "line", "area", "donut", "funnel"]:
            channels = [encoding["category"]] + (
                [encoding["series"]] if "series" in encoding else []
            )
            keys = [tuple(row[name] for name in channels) for row in dataset["rows"]]
            if len(keys) != len(set(keys)):
                invalid(
                    "Category/series keys must be unique. Include the series dimension or aggregate the dataset."
                )
        if financial_values(block.get("title", "")):
            invalid(
                "Block titles cannot contain financial claims. Render amounts from the dataset."
            )
        normalized = {**block, "size": block.get("size", "wide"), "encoding": encoding}
        if kind == "waterfall":
            if (
                dataset["normalized_query"]["dataset"] != "projects"
                or not {"contract_amount", "confirmed_cost"} <= columns.keys()
                or any(
                    r["confirmed_cost"] is None or r["contract_amount"] is None
                    for r in dataset["rows"]
                )
            ):
                fail("unsupported_query")
            contract = sum(
                (Decimal(r["contract_amount"]) for r in dataset["rows"]), Decimal(0)
            )
            cost = sum(
                (Decimal(r["confirmed_cost"]) for r in dataset["rows"]), Decimal(0)
            )
            normalized["steps"] = [
                {"label": "Договор", "value": str(contract), "kind": "base"},
                {
                    "label": "Подтверждённая стоимость",
                    "value": str(-cost),
                    "kind": "delta",
                },
                {
                    "label": "Расчётный остаток",
                    "value": str(contract - cost),
                    "kind": "total",
                },
            ]
        for row in dataset["rows"]:
            for c in dataset["columns"]:
                value = row[c["name"]]
                if value is not None and c["type"] in ["money", "count", "percent"]:
                    try:
                        v = Decimal(str(value))
                        if not v.is_finite() or abs(v) > 9007199254740991:
                            fail("presentation_invalid")
                    except InvalidOperation:
                        fail("presentation_invalid")
        used[dataset["dataset_id"]] = dataset
        blocks.append(normalized)
    context.check()
    result = {
        "version": "1.0",
        "title": arguments["title"],
        "summary": arguments.get("summary", "")
        if not financial_values(arguments.get("summary", ""))
        else "",
        "blocks": blocks,
        "datasets": used,
    }
    if len(json.dumps(result, ensure_ascii=False).encode()) > 1024 * 1024:
        fail("query_limit_exceeded")
    return result
