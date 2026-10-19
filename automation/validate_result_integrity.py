#!/usr/bin/env python3
import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from aisbench_artifact_archive import sanitize_archive_name
from aisbench_result_adapter import read_last_result


CSV_COLUMNS = [
    "scenario",
    "current_time",
    "input_len",
    "output_len",
    "total_req",
    "max_cc",
    "cc",
    "rr",
    "TTFT avg",
    "TTFT P90",
    "TPOT avg",
    "TPOT SLO_P90",
    "E2E_time",
    "output_throughput",
    "single_output_throughput",
    "E2E_throughput",
    "single_E2E_throughput",
    "qps",
    "qpm",
    "input_token_throughput",
    "prefill_token_throughput",
    "repeat_rate",
    "status",
]


SOURCE_RESULT_COLUMNS = CSV_COLUMNS[1:21]
PEAK_METRIC_COLUMNS = CSV_COLUMNS[6:21]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate final CSV values against original aisbench and peak artifacts.")
    parser.add_argument("--result-dir", required=True)
    parser.add_argument("--host-output-dir", required=True)
    parser.add_argument("--report-file", default="")
    return parser.parse_args()


def add(checks: List[Tuple[str, bool, str]], name: str, ok: bool, detail: str) -> None:
    checks.append((name, ok, detail))


def norm(value: object) -> str:
    return "" if value is None else str(value).strip()


def values_equal(left: object, right: object) -> bool:
    a = norm(left)
    b = norm(right)
    if a == b:
        return True
    try:
        return abs(float(a) - float(b)) < 1e-9
    except Exception:
        return False


def container_to_host(path: str, host_output_dir: Path) -> Path:
    prefix = "/workspace/aisbench_outputs/"
    if path.startswith(prefix):
        return host_output_dir / path[len(prefix):]
    return Path(path)


def read_final_rows(csv_path: Path) -> List[Dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    return rows


def read_last_source_result(csv_path: Path) -> Dict[str, str]:
    test_type = "stream"
    manifest_path = csv_path.parent / "archive_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("extra_metadata", {}), dict):
            raise ValueError("invalid archive result context")
        test_type = manifest.get("extra_metadata", {}).get("test_type", "stream")
    row = read_last_result(csv_path, test_type=test_type)
    return {column: str(row[column]) for column in SOURCE_RESULT_COLUMNS}


def find_archive(host_output_dir: Path, scenario: str, current_time: Optional[str] = None) -> Optional[Path]:
    """Resolve a unique archive by its original name and optional source time."""
    if current_time is not None and not str(current_time).strip():
        return None
    pattern = "performance_%s_*" % sanitize_archive_name(scenario)
    matches = []
    for path in host_output_dir.glob(pattern):
        if not path.is_dir():
            continue
        try:
            manifest = json.loads((path / "archive_manifest.json").read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get("archive_name") != scenario:
                continue
            source = read_last_source_result(path / "aisbench_result.csv")
            if current_time is not None and norm(source.get("current_time")) != norm(current_time):
                continue
        except (OSError, ValueError, csv.Error):
            continue
        matches.append(path)
    return matches[0] if len(matches) == 1 else None


def extract_peak_report_paths(log_text: str, host_output_dir: Path) -> List[Path]:
    paths: List[Path] = []
    lines = log_text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "===PEAK_REPORT_PATH===" and index + 1 < len(lines):
            raw = lines[index + 1].strip()
            if raw:
                paths.append(container_to_host(raw, host_output_dir))
    return paths


def load_peak_best_by_case(paths: List[Path]) -> Dict[str, Dict[str, object]]:
    by_case: Dict[str, Dict[str, object]] = {}
    for path in paths:
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", errors="replace") as f:
            report = json.load(f)
        for row in report.get("best", []):
            case_name = norm(row.get("case_name"))
            if case_name:
                by_case[case_name] = row
    return by_case


def compare_fields(row: Dict[str, str], source: Dict[str, object], fields: List[str]) -> List[str]:
    mismatches = []
    for field in fields:
        if not values_equal(row.get(field, ""), source.get(field, "")):
            mismatches.append("%s: final=%r source=%r" % (field, row.get(field, ""), source.get(field, "")))
    return mismatches


def write_report(path: Path, checks: List[Tuple[str, bool, str]]) -> None:
    passed = sum(1 for _, ok, _ in checks if ok)
    failed = len(checks) - passed
    lines = [
        "# Result integrity validation report",
        "",
        "| Status | Check | Detail |",
        "|---|---|---|",
    ]
    for name, ok, detail in checks:
        lines.append("| %s | %s | %s |" % ("PASS" if ok else "FAIL", name, detail.replace("|", "\\|")))
    lines.extend(["", "PASS=%d FAIL=%d" % (passed, failed)])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    result_dir = Path(args.result_dir)
    host_output_dir = Path(args.host_output_dir)
    csv_path = result_dir / "all_results.csv"
    log_path = result_dir / "execution.log"
    checks: List[Tuple[str, bool, str]] = []

    add(checks, "final csv exists", csv_path.is_file(), str(csv_path))
    add(checks, "execution log exists", log_path.is_file(), str(log_path))
    if not csv_path.is_file():
        if args.report_file:
            write_report(Path(args.report_file), checks)
        return 1

    rows = read_final_rows(csv_path)
    add(checks, "final csv has rows", bool(rows), "rows=%d" % len(rows))
    add(checks, "final csv headers exact", list(rows[0].keys()) == CSV_COLUMNS if rows else False, ",".join(rows[0].keys()) if rows else "")
    add(checks, "final rows field count exact", all(len(row) == len(CSV_COLUMNS) for row in rows), ",".join(str(len(row)) for row in rows))
    add(checks, "no failed/no_result status", all(row.get("status") not in {"failed", "no_result"} for row in rows), ",".join(row.get("status", "") for row in rows))

    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    peak_paths = extract_peak_report_paths(log_text, host_output_dir)
    peak_by_case = load_peak_best_by_case(peak_paths)

    for row in rows:
        scenario = norm(row.get("scenario"))
        status = norm(row.get("status"))
        if status == "passed":
            archive = find_archive(host_output_dir, str(row.get("scenario") or ""), row.get("current_time", ""))
            add(checks, "fixed archive found: %s" % scenario, archive is not None, str(archive or ""))
            if archive is None:
                continue
            source_csv = archive / "aisbench_result.csv"
            add(checks, "fixed source csv exists: %s" % scenario, source_csv.is_file(), str(source_csv))
            if source_csv.is_file():
                source = read_last_source_result(source_csv)
                mismatches = compare_fields(row, source, SOURCE_RESULT_COLUMNS)
                add(checks, "fixed row matches source: %s" % scenario, not mismatches, "; ".join(mismatches) if mismatches else "all fields match")
        elif status == "peak":
            add(checks, "peak source report found: %s" % scenario, scenario in peak_by_case, ",".join(sorted(peak_by_case)))
            if scenario not in peak_by_case:
                continue
            source = peak_by_case[scenario]
            mismatches = compare_fields(row, source, PEAK_METRIC_COLUMNS)
            add(checks, "peak row matches report: %s" % scenario, not mismatches, "; ".join(mismatches) if mismatches else "all fields match")

    if args.report_file:
        write_report(Path(args.report_file), checks)

    passed = sum(1 for _, ok, _ in checks if ok)
    failed = len(checks) - passed
    print("PASS=%d FAIL=%d" % (passed, failed))
    for name, ok, detail in checks:
        print("%s | %s | %s" % ("PASS" if ok else "FAIL", name, detail))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
