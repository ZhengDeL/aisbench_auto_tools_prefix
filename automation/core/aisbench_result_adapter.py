"""Read upstream result CSVs by name and preserve the add-on summary schema."""
from __future__ import annotations

import argparse
import csv
import math
import json
import re
import hashlib
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence
from aisbench_artifact_archive import path_is_relative_to


CANONICAL_COLUMNS = (
    "current_time", "input_len", "output_len", "total_req", "max_cc", "cc", "rr",
    "TTFT avg", "TTFT P90", "TPOT avg", "TPOT SLO_P90", "E2E_time",
    "output_throughput", "single_output_throughput", "E2E_throughput",
    "single_E2E_throughput", "qps", "qpm", "input_token_throughput",
    "prefill_token_throughput",
)
TOKEN_LATENCY_COLUMNS = ("TTFT avg", "TTFT P90", "TPOT avg", "TPOT SLO_P90")
SUMMARY_COLUMNS = ["scenario", *CANONICAL_COLUMNS, "repeat_rate", "status"]
PEAK_FINAL_STATES = {"peak", "peak_confirmed", "peak_capped", "peak_budget", "peak_unstable",
                     "peak_error", "peak_request_error", "peak_no_pass", "peak_cancelled"}
ROW_STATES = {"passed", "failed", "no_result", "peak_round", *PEAK_FINAL_STATES}
SUCCESS_STATES = {"passed", "peak", "peak_confirmed", "peak_capped"}
PERFORMANCE_COLUMNS = ("cc", *TOKEN_LATENCY_COLUMNS, "E2E_time", "output_throughput",
                       "single_output_throughput", "E2E_throughput", "single_E2E_throughput",
                       "qps", "qpm", "input_token_throughput", "prefill_token_throughput")

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


def read_last_result(path: Path, test_type: str = "stream", test_accuracy: bool = False) -> dict[str, str]:
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
    return normalize_result(last, test_type=test_type, test_accuracy=test_accuracy)


def normalize_result(row: Mapping[str, Any], test_type: str = "stream", test_accuracy: bool = False) -> dict[str, str]:
    """Normalize one actual scope; never pool measurements from different runs."""
    if test_type not in ("stream", "text"):
        raise ValueError("test_type must be stream or text")
    project_result(row)
    last = {str(key): str(value) for key, value in row.items()}
    if not last["current_time"].strip():
        raise ValueError("result CSV missing current_time")
    if test_accuracy:
        # Native accuracy summaries can contain every performance sentinel.
        # The independent accuracy artifact is the evidence, not those values.
        return {**last, **dict.fromkeys(CANONICAL_COLUMNS[1:], "")}
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
    text_latencies = set(TOKEN_LATENCY_COLUMNS) if test_type == "text" else set()
    optional_latencies = set(text_latencies)
    try:
        if float(last["output_len"]) <= 1:
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
        if name in text_latencies:
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"invalid numeric result: {name}")
            last[name] = ""
            continue
        # An average output count <= 1 can mix zero-token and multi-token
        # requests. Preserve a real TPOT from those requests; only the native
        # missing-value sentinel is inapplicable at this average length.
        if name in optional_latencies and value == FAILURE_SENTINELS.get(name):
            last[name] = ""
            continue
        if not math.isfinite(value) or value < 0 or value == FAILURE_SENTINELS.get(name):
            raise ValueError(f"invalid numeric result or failure sentinel: {name}")
    return last


def validate_summary_row(row: Mapping[str, Any], *, accuracy: bool = False) -> None:
    """Validate the summary contract without inventing absent measurements."""
    if list(row) != SUMMARY_COLUMNS or row.get("status") not in ROW_STATES or not str(row.get("scenario", "")).strip():
        raise ValueError("invalid summary identity or schema")
    required = row["status"] in SUCCESS_STATES and not accuracy
    for name in CANONICAL_COLUMNS[1:] + ("repeat_rate",):
        text = str(row.get(name, "")).strip()
        if not text:
            if required and name not in (*TOKEN_LATENCY_COLUMNS, "prefill_token_throughput"):
                raise ValueError("missing summary measurement: " + name)
            continue
        try:
            value = float(text)
        except (ValueError, TypeError) as error:
            raise ValueError("invalid summary measurement: " + name) from error
        if not math.isfinite(value) or value < 0 or value == FAILURE_SENTINELS.get(name):
            raise ValueError("invalid summary measurement: " + name)
        if name in ("total_req", "max_cc") and value != int(value):
            raise ValueError("nonintegral summary count: " + name)
        if name == "repeat_rate" and value > 1:
            raise ValueError("invalid repeat rate")
        if accuracy and name in PERFORMANCE_COLUMNS:
            raise ValueError("accuracy cannot report performance measurements")


def scope_counts(scope: Mapping[str, Any]) -> dict[str, int | None]:
    counts = dict(scope.get("counts") or {})
    aliases = {"success": "success_requests", "failed": "failed_requests", "total": "total_requests",
               "planned": "planned_requests", "cancelled": "cancelled_requests", "attempts": "attempts"}
    result = {}
    for name, alias in aliases.items():
        raw = counts.get(name, scope.get(alias))
        if raw is None:
            result[name] = None
            continue
        if isinstance(raw, bool):
            raise ValueError("invalid scope request count")
        try:
            value = float(raw)
        except (ValueError, TypeError) as error:
            raise ValueError("invalid scope request count") from error
        if not math.isfinite(value) or value < 0 or value != int(value):
            raise ValueError("invalid scope request count")
        result[name] = int(value)
    return result


def normalize_scope_row(scope: Mapping[str, Any], base_dir: Path | None = None) -> dict[str, str]:
    raw = scope.get("raw_row")
    test_type = str(scope.get("test_type", "stream"))
    accuracy = bool(scope.get("test_accuracy", scope.get("accuracy", False)))
    source = scope.get("source_csv") or scope.get("result_csv")
    if source:
        path = Path(str(source))
        if base_dir is not None and not path.is_absolute():
            path = base_dir / path
        if not path.is_file() or path.is_symlink():
            raise ValueError('scope original result is missing or linked')
        return read_last_result(path, test_type, accuracy)
    if not isinstance(raw, dict):
        raise ValueError("scope has no original result")
    return normalize_result(raw, test_type, accuracy)


def read_accuracy_scope(scope: Mapping[str, Any], base_dir: Path | None = None) -> dict[str, Any]:
    """Read accuracy artifacts independently from the unavailable perf columns.

    Keep dataset/metric identities. A percentage alone does not establish its
    numerator or denominator; never reconstruct those from a rounded score.
    """
    files = scope.get('accuracy_results')
    if not isinstance(files, list) or not files:
        raise ValueError('accuracy result artifacts are missing')
    metrics, predictions, artifacts, scored = {}, [], [], []
    numerator = denominator = None
    for raw_path in files:
        path = Path(str(raw_path))
        if base_dir is not None:
            prefix = '/workspace/aisbench_outputs/'
            if str(raw_path).startswith(prefix):
                path = base_dir / str(raw_path)[len(prefix):]
            elif not path.is_absolute():
                path = base_dir / path
            if not path_is_relative_to(path.resolve(), base_dir.resolve()):
                raise ValueError('accuracy artifact outside task output')
        if path.is_symlink() or not path.is_file():
            raise ValueError('accuracy artifact missing or linked')
        artifacts.append(str(path))
        if path.suffix.lower() not in {'.json', '.jsonl'}:
            continue
        text = path.read_text(encoding='utf-8')
        payload = ([json.loads(line) for line in text.splitlines() if line.strip()]
                   if path.suffix.lower() == '.jsonl' else json.loads(text))
        parts = {part.casefold() for part in path.parts}
        if 'predictions' in parts:
            records = list(payload.values()) if isinstance(payload, dict) else payload
            if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
                raise ValueError('invalid accuracy prediction records')
            predictions.extend(records)
        if 'results' in parts or path.stem.casefold() in {'accuracy', 'accuracy_result', 'accuracy_results'}:
            def collect(value, prefix=''):
                nonlocal numerator, denominator
                if not isinstance(value, dict):
                    return
                details = value.get('details')
                if 'gsm8k' in path.stem.casefold() and 'accuracy' in value and isinstance(details, (dict, list)):
                    records = list(details.values()) if isinstance(details, dict) else details
                    records = [item for item in records if isinstance(item, dict)]
                    if not records:
                        raise ValueError('accuracy details contain no samples')
                    correct = 0
                    for record in records:
                        prediction = record.get('predictions', record.get('pred'))
                        reference = record.get('references', record.get('answer'))
                        if prediction is None or reference is None or not isinstance(record.get('correct'), bool):
                            raise ValueError('accuracy detail lacks prediction/reference/verdict')
                        origin = record.get('origin_prediction')
                        if isinstance(origin, str):
                            numbers = re.findall(r'\-?\d+[\d,]*(?:\.\d+)?', origin.split('Question:')[0])
                            extracted = numbers[-1].replace(' ', '').replace(',', '') if numbers else 'NULL'
                            if extracted != prediction:
                                raise ValueError('accuracy prediction differs from original GSM8K extraction')
                        equal = prediction == reference
                        if not equal:
                            try:
                                equal = abs(float(prediction) - int(reference)) < 1e-6
                            except (ValueError, TypeError, OverflowError):
                                equal = False
                        if equal != record['correct']:
                            raise ValueError('accuracy verdict differs from original GSM8K definition')
                        correct += int(equal)
                    score = 100 * correct / len(records)
                    if not math.isclose(float(value['accuracy']), score, rel_tol=1e-12, abs_tol=1e-9):
                        raise ValueError('accuracy score differs from per-sample recomputation')
                    scored.append({'artifact': str(path), 'metric': prefix + 'accuracy',
                                   'numerator': correct, 'denominator': len(records), 'score': score})
                for name, item in value.items():
                    key = prefix + str(name)
                    if isinstance(item, dict):
                        collect(item, key + '.')
                    elif isinstance(item, (int, float)) and not isinstance(item, bool):
                        if not math.isfinite(item):
                            raise ValueError('nonfinite accuracy result')
                        if str(name).casefold() in {'accuracy', 'acc', 'exact_match', 'score', 'f1', 'pass@1'}:
                            metrics[key] = item
                        elif str(name).casefold() in {'correct', 'numerator'}:
                            numerator = item
                        elif str(name).casefold() in {'total', 'denominator'}:
                            denominator = item
            collect(payload)
    if not metrics:
        raise ValueError('no supported accuracy metric in original results')
    for value in (numerator, denominator):
        if value is not None and (value < 0 or int(value) != value):
            raise ValueError('invalid accuracy count')
    if numerator is not None and denominator is not None and numerator > denominator:
        raise ValueError('accuracy numerator exceeds denominator')
    if predictions and denominator is None:
        denominator = len(predictions)
    if len(scored) == 1:
        numerator, denominator = scored[0]['numerator'], scored[0]['denominator']
        if predictions and len(predictions) != denominator:
            raise ValueError('accuracy prediction and evaluated sample counts differ')
    return {'metrics': metrics, 'prediction_count': len(predictions) if predictions else None,
            'numerator': numerator, 'denominator': denominator,
            'denominator_source': 'per_sample_details' if scored else ('original_results' if not predictions else 'prediction_records'),
            'artifacts': artifacts, 'independently_scored': bool(scored), 'recomputed': scored}


def load_scope_index(result_dir: Path) -> dict[int, dict[str, Any]] | None:
    path = result_dir / "result_scopes.json"
    if not path.exists():
        return None  # Legacy artifacts remain readable, explicitly unscoped.
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and payload.get('csv_sha256'):
        if hashlib.sha256((result_dir / 'all_results.csv').read_bytes()).hexdigest() != payload['csv_sha256']:
            raise ValueError('scope index belongs to a different CSV generation')
    entries = payload.get("rows", payload.get("entries")) if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError("invalid result scope index")
    result = {}
    for ordinal, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError("invalid result scope entry")
        number = entry.get("row_index", ordinal)
        if isinstance(number, bool) or not isinstance(number, int) or number < 0 or number in result:
            raise ValueError("duplicate or invalid result scope identity")
        result[number] = {**entry, '_origin_output_root': payload.get('output_root', '')}
    return result


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
