"""Read upstream result CSVs by name and preserve the add-on summary schema."""
from __future__ import annotations

import csv
import math
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


CANONICAL_COLUMNS = (
    "current_time", "input_len", "output_len", "total_req", "max_cc", "cc", "rr",
    "TTFT avg", "TTFT P90", "TPOT avg", "TPOT SLO_P90", "E2E_time",
    "output_throughput", "single_output_throughput", "E2E_throughput",
    "single_E2E_throughput", "qps", "qpm", "input_token_throughput",
    "prefill_token_throughput",
)


def project_result(row: Mapping[str, Any]) -> list[str]:
    missing = [name for name in CANONICAL_COLUMNS if name not in row or row[name] is None]
    if missing:
        raise ValueError("result CSV missing columns: " + ", ".join(missing))
    return [str(row[name]) for name in CANONICAL_COLUMNS]


def read_last_result(path: Path) -> dict[str, str]:
    last = None
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        if len(headers) != len(set(headers)):
            raise ValueError("result CSV contains duplicate column names")
        if not set(CANONICAL_COLUMNS).issubset(headers):
            missing = [name for name in CANONICAL_COLUMNS if name not in headers]
            raise ValueError("result CSV missing columns: " + ", ".join(missing))
        for row in reader:
            last = row
    if last is None:
        raise ValueError("result CSV has no data rows")
    if None in last:
        raise ValueError("result CSV has more data fields than header columns")
    project_result(last)
    # Upstream uses 9999 when its global prefill throughput parser found no
    # value. The image's per-request PrefillTokenThroughput average has a
    # different denominator and cannot fill this global field.
    try:
        if float(last["prefill_token_throughput"]) == 9999:
            last["prefill_token_throughput"] = ""
    except (TypeError, ValueError):
        pass
    return last


def invalid_result_reason(
    row: Mapping[str, Any], metric_columns: Sequence[str] = ("TTFT avg", "TPOT avg")
) -> str:
    for name in metric_columns:
        try:
            value = float(row.get(name, ""))
        except (TypeError, ValueError):
            return f"invalid or missing metric: {name}"
        if not math.isfinite(value) or value < 0 or value == 99999:
            return f"invalid metric or failure sentinel: {name}"
    return ""


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: aisbench_result_adapter.py RESULT.csv", file=sys.stderr)
        return 2
    try:
        row = read_last_result(Path(sys.argv[1]))
        reason = invalid_result_reason(row)
        if reason:
            raise ValueError(reason)
        csv.writer(sys.stdout, lineterminator="\n").writerow(project_result(row))
    except (OSError, ValueError, csv.Error) as error:
        print(f"invalid aisbench result: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
