#!/usr/bin/env python3
import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate run_perf_scenarios.sh output artifacts.")
    parser.add_argument("--result-dir", required=True, help="scenario_results/<timestamp> directory")
    parser.add_argument("--host-output-dir", required=True, help="host output directory mounted as /workspace/aisbench_outputs")
    parser.add_argument("--expected-statuses", default="", help="comma separated expected statuses, for example passed,peak")
    parser.add_argument("--report-file", default="", help="optional markdown report path")
    return parser.parse_args()


def add(checks: List[Tuple[str, bool, str]], name: str, ok: bool, detail: str) -> None:
    checks.append((name, ok, detail))


def container_to_host(path: str, host_output_dir: Path) -> Path:
    prefix = "/workspace/aisbench_outputs/"
    if path.startswith(prefix):
        return host_output_dir / path[len(prefix):]
    return Path(path)


def extract_latest_peak_report_path(log_text: str, host_output_dir: Path) -> Optional[Path]:
    marker = "===PEAK_REPORT_PATH===\n"
    idx = log_text.rfind(marker)
    if idx < 0:
        return None
    line = log_text[idx + len(marker):].splitlines()[0].strip()
    if not line:
        return None
    return container_to_host(line, host_output_dir)


def find_latest_fixed_archive(host_output_dir: Path, scenario_prefix: str) -> Optional[Path]:
    pattern = "performance_%s_*" % scenario_prefix
    matches = sorted(path for path in host_output_dir.glob(pattern) if path.is_dir())
    return matches[-1] if matches else None


def write_report(path: Path, checks: List[Tuple[str, bool, str]]) -> None:
    passed = sum(1 for _, ok, _ in checks if ok)
    failed = len(checks) - passed
    lines = [
        "# E2E output validation report",
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
    checks: List[Tuple[str, bool, str]] = []

    csv_path = result_dir / "all_results.csv"
    log_path = result_dir / "execution.log"
    add(checks, "result dir exists", result_dir.is_dir(), str(result_dir))
    add(checks, "all_results.csv exists", csv_path.is_file(), str(csv_path))
    add(checks, "execution.log exists", log_path.is_file(), str(log_path))

    rows = []
    if csv_path.is_file():
        with csv_path.open(newline="", encoding="utf-8", errors="replace") as f:
            rows = list(csv.DictReader(f))
    add(checks, "csv has rows", len(rows) > 0, "rows=%d" % len(rows))

    if rows:
        statuses = [row.get("status", "") for row in rows]
        add(checks, "csv status nonempty", all(statuses), ",".join(statuses))
        add(checks, "csv row field count", all(len(row) == 23 for row in rows), ",".join(str(len(row)) for row in rows))
        expected = [part.strip() for part in args.expected_statuses.split(",") if part.strip()]
        if expected:
            add(checks, "csv expected statuses", statuses == expected, "actual=%s expected=%s" % (statuses, expected))
        add(checks, "csv no failed/no_result", all(s not in {"failed", "no_result"} for s in statuses), ",".join(statuses))

    log_text = ""
    if log_path.is_file():
        log_text = log_path.read_text(encoding="utf-8", errors="replace")

    peak_report = extract_latest_peak_report_path(log_text, host_output_dir) if log_text else None
    has_peak_row = any(row.get("status") == "peak" for row in rows)
    if has_peak_row:
        add(checks, "peak report path found", peak_report is not None, str(peak_report or ""))
        if peak_report is not None:
            add(checks, "peak report exists", peak_report.is_file(), str(peak_report))
            if peak_report.is_file():
                with peak_report.open(encoding="utf-8") as f:
                    report = json.load(f)
                best = report.get("best", [])
                add(checks, "peak report has best", len(best) > 0, "best_count=%d" % len(best))
                add(checks, "peak best passed", bool(best) and best[0].get("passed") == "yes", str(best[0].get("passed") if best else ""))

    fixed_rows = [row for row in rows if row.get("status") == "passed"]
    for row in fixed_rows:
        scenario = row.get("scenario", "")
        archive = find_latest_fixed_archive(host_output_dir, scenario)
        add(checks, "fixed archive exists: %s" % scenario, archive is not None, str(archive or ""))
        if archive is not None:
            add(checks, "fixed archive manifest: %s" % scenario, (archive / "archive_manifest.json").is_file(), str(archive))
            add(checks, "fixed archive result csv: %s" % scenario, (archive / "aisbench_result.csv").is_file(), str(archive))

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
