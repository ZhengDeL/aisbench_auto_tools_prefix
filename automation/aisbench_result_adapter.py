"""Read upstream result CSVs by name and preserve the add-on summary schema."""
from __future__ import annotations

import argparse
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
TOKEN_LATENCY_COLUMNS = ("TTFT avg", "TTFT P90", "TPOT avg", "TPOT SLO_P90")

# Match the upstream parser's defaults per field. A partially parsed row must
# not become a passing result merely because its two average latencies exist.
FAILURE_SENTINELS = {
    **dict.fromkeys((
        "input_len", "output_len", "total_req", "max_cc", "cc", "TTFT avg",
        "TTFT P90", "TPOT avg", "TPOT SLO_P90", "E2E_time",
        "output_throughput", "single_output_throughput",
    ), 99999),
    **dict.fromkeys((
        "E2E_throughput", "single_E2E_throughput", "qps", "qpm",
        "input_token_throughput",
    ), 9999),
}


def project_result(row: Mapping[str, Any]) -> list[str]:
    missing = [name for name in CANONICAL_COLUMNS if name not in row or row[name] is None]
    if missing:
        raise ValueError("result CSV missing columns: " + ", ".join(missing))
    return [str(row[name]) for name in CANONICAL_COLUMNS]


def read_last_result(path: Path, test_type: str = "stream") -> dict[str, str]:
    if test_type not in ("stream", "text"):
        raise ValueError("test_type must be stream or text")
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
    if not last["current_time"].strip():
        raise ValueError("result CSV missing current_time")
    optional_latencies = set(TOKEN_LATENCY_COLUMNS) if test_type == "text" else set()
    try:
        if float(last["output_len"]) == 1:
            optional_latencies.update(TOKEN_LATENCY_COLUMNS[2:])
    except (TypeError, ValueError):
        pass  # The regular numeric checks below reject an invalid output_len.
    for name in CANONICAL_COLUMNS[1:]:
        if (name == "prefill_token_throughput" or name in optional_latencies) and not last[name].strip():
            last[name] = ""
            continue
        try:
            value = float(last[name])
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid or missing numeric result: {name}") from error
        if name in optional_latencies:
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"invalid numeric result: {name}")
            last[name] = ""
            continue
        if not math.isfinite(value) or value < 0 or value == FAILURE_SENTINELS.get(name):
            raise ValueError(f"invalid numeric result or failure sentinel: {name}")
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_csv", type=Path)
    parser.add_argument("--test-type", choices=("stream", "text"), default="stream")
    args = parser.parse_args()
    try:
        row = read_last_result(args.result_csv, test_type=args.test_type)
        csv.writer(sys.stdout, lineterminator="\n").writerow(project_result(row))
    except (OSError, ValueError, csv.Error) as error:
        print(f"invalid aisbench result: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
