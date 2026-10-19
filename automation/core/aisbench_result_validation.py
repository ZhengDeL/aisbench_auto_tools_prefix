#!/usr/bin/env python3
from __future__ import annotations
import argparse
import csv
import json
import re
import sys
import hashlib
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from aisbench_artifact_archive import sanitize_archive_name, path_is_relative_to
from aisbench_result_adapter import (read_last_result, validate_summary_row, load_scope_index,
                                    normalize_scope_row, scope_counts, ROW_STATES, SUCCESS_STATES,
                                    PEAK_FINAL_STATES, CANONICAL_COLUMNS, PERFORMANCE_COLUMNS)


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
    parser.add_argument("--require-scopes", action="store_true")
    parser.add_argument("--expected-statuses", default="", help="exact ordered comma-separated statuses")
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
        left_number, right_number = Decimal(a), Decimal(b)
        return left_number.is_finite() and right_number.is_finite() and left_number == right_number
    except (ValueError, InvalidOperation):
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
    row = read_last_result(csv_path, test_type=test_type,
                           test_accuracy=bool(manifest.get("extra_metadata", {}).get("test_accuracy", False)) if manifest_path.exists() else False)
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


def artifact_path(raw: str, host_output_dir: Path, base: Optional[Path] = None, origin_root: Optional[Path] = None) -> Path:
    portable = str(raw).replace('\\', '/')
    origin = str(origin_root or '').replace('\\', '/').rstrip('/')
    if origin and portable.startswith(origin + '/'):
        relative_parts = portable[len(origin) + 1:].split('/')
        if '..' in relative_parts:
            raise ValueError('relocated evidence escapes its origin root')
        path = host_output_dir.joinpath(*relative_parts)
    else:
        path = container_to_host(str(raw), host_output_dir)
    if not path.is_absolute():
        path = (base or host_output_dir) / path
    if origin_root and path.is_absolute() and not path_is_relative_to(path.resolve(), host_output_dir.resolve()):
        origin_root = Path(origin_root)
        try:
            relative = path.relative_to(origin_root)
        except ValueError:
            pass
        else:
            if '..' in relative.parts:
                raise ValueError('relocated evidence escapes its origin root')
            path = host_output_dir / relative
    resolved = path.resolve()
    if not path_is_relative_to(resolved, host_output_dir.resolve()):
        raise ValueError("result evidence outside task output")
    return resolved


def verify_archive_manifest(path: Path) -> None:
    if path.is_symlink():
        raise ValueError('archive manifest is linked')
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version", 1) < 2:
        return  # Legacy manifests did not provide content hashes.
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(manifest.get("archived_files", [])):
        raise ValueError("incomplete archive content index")
    actual = set()
    for item in path.parent.rglob('*'):
        if item.is_symlink():
            raise ValueError('archive contains a linked artifact')
        if item.is_file() and item != path:
            actual.add(item.relative_to(path.parent).as_posix())
    if actual != set(artifacts):
        raise ValueError('archive content index omits or invents files')
    for name, expected in artifacts.items():
        relative = Path(name)
        target = path.parent / relative
        if relative.is_absolute() or '..' in relative.parts or target.is_symlink() or not path_is_relative_to(target.resolve(), path.parent.resolve()):
            raise ValueError("archive content escapes its directory")
        if (not isinstance(expected, dict) or isinstance(expected.get('size'), bool)
                or not isinstance(expected.get('size'), int) or expected['size'] < 0
                or not isinstance(expected.get('sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', expected['sha256'])):
            raise ValueError('invalid archive content fingerprint')
        if target.stat().st_size != expected.get("size"):
            raise ValueError("archive content size mismatch")
        digest = hashlib.sha256()
        with target.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != expected.get("sha256"):
            raise ValueError("archive content digest mismatch")


def verify_nearest_manifest(path: Path, output_root: Path, verified: set[Path]) -> None:
    for parent in (path.parent, *path.parents):
        if not path_is_relative_to(parent, output_root.resolve()):
            break
        manifest = parent / 'archive_manifest.json'
        if manifest.is_file():
            if manifest not in verified:
                verify_archive_manifest(manifest)
                verified.add(manifest)
            return


def indexed_scope(entry: dict, result_dir: Path, host_output_dir: Path,
                  verified_manifests: set[Path] | None = None) -> tuple[dict, list[dict]]:
    evaluation = {}
    if entry.get("evaluation_file"):
        path = artifact_path(entry["evaluation_file"], host_output_dir, result_dir, entry.get('_origin_output_root'))
        verify_nearest_manifest(path, host_output_dir, verified_manifests if verified_manifests is not None else set())
        evaluation = json.loads(path.read_text(encoding="utf-8"))
    scopes = evaluation.get("main_scopes", evaluation.get("scopes", []))
    if not isinstance(scopes, list):
        raise ValueError("invalid scope list")
    if any(not isinstance(scope, dict) or not scope.get('scope_id') for scope in scopes):
        raise ValueError('invalid measurement scope identity')
    if len({scope['scope_id'] for scope in scopes}) != len(scopes):
        raise ValueError('duplicate measurement scope identity')
    selected = entry.get("selected_scope_id", entry.get("selected_scope", evaluation.get("selected_scope_id", "")))
    inline = entry.get("scope")
    if isinstance(inline, dict):
        if inline.get('scope_id') != selected or inline.get('phase', 'main') != 'main':
            raise ValueError('inline selected scope identity or phase differs')
        if scopes:
            matches = [scope for scope in scopes if scope.get('scope_id') == selected]
            if len(matches) != 1 or inline != matches[0]:
                raise ValueError('inline scope differs from original evaluation')
        scopes = scopes or [inline]
        return inline, scopes
    matches = [scope for scope in scopes if isinstance(scope, dict) and scope.get("scope_id") == selected]
    if len(matches) != 1:
        raise ValueError("selected scope missing or ambiguous")
    if matches[0].get('phase', 'main') != 'main':
        raise ValueError('selected scope is not a main measurement')
    return matches[0], scopes


def check_indexed_row(row: dict, entry: dict, result_dir: Path, host_output_dir: Path,
                      verified_manifests: set[Path] | None = None) -> None:
    verified_manifests = verified_manifests if verified_manifests is not None else set()
    def resolve(raw):
        return artifact_path(raw, host_output_dir, result_dir, entry.get('_origin_output_root'))
    for key in ("scenario", "current_time", "status"):
        if key not in entry or str(entry[key]) != str(row.get(key, "")):
            raise ValueError("summary row and scope identity differ")
    if row['status'] in PEAK_FINAL_STATES and entry.get('report_file'):
        report_path = resolve(entry['report_file'])
        verify_nearest_manifest(report_path, host_output_dir, verified_manifests)
        report = json.loads(report_path.read_text(encoding='utf-8'))
        best = [item for item in report.get('best', []) if item.get('case_name') == row['scenario']]
        if len(best) != 1 or best[0].get('status', 'peak') != row['status']:
            raise ValueError('final peak identity or status differs from report')
        for key in ('selected_trial_id', 'selected_scope_id'):
            if key in entry and key in best[0] and entry[key] != best[0][key]:
                raise ValueError('final peak selection differs from report')
        if row['status'] in SUCCESS_STATES and best[0].get('passed') not in {'yes', True}:
            raise ValueError('final successful peak is not a passing measurement')
    unmeasured = not entry.get("selected_scope_id") and not entry.get("scope")
    if unmeasured and (entry.get("measurement_state") in {"ERROR", "INVALID", "CANCELLED"}
                       or row['status'] in PEAK_FINAL_STATES - SUCCESS_STATES):
        if row['status'] in SUCCESS_STATES:
            raise ValueError("invalid measurement cannot be successful")
        validate_summary_row(row)
        if any(str(row.get(name, '')).strip() for name in (*PERFORMANCE_COLUMNS, 'total_req', 'max_cc')):
            raise ValueError('unmeasured scope cannot contain measured performance or request counts')
        return
    selected, scopes = indexed_scope(entry, result_dir, host_output_dir, verified_manifests)
    accuracy = bool(selected.get("test_accuracy", selected.get("accuracy", False)))
    validate_summary_row(row, accuracy=accuracy)
    for scope in scopes:
        if scope.get('phase', 'main') != 'main':
            continue
        if row['status'] in SUCCESS_STATES and scope.get('state') not in {'PASS', 'SLO_FAIL'}:
            raise ValueError("successful result contains an invalid or failed scope")
        if accuracy:
            from aisbench_result_adapter import read_accuracy_scope
            accuracy_scope = dict(scope)
            accuracy_scope['accuracy_results'] = [str(resolve(path)) for path in scope.get('accuracy_results', [])]
            accuracy_result = read_accuracy_scope(accuracy_scope, base_dir=host_output_dir)
            if accuracy_result['denominator'] is None:
                raise ValueError('accuracy denominator has no original evidence')
            continue
        counts = scope_counts(scope)
        if scope.get('state') in {'PASS', 'SLO_FAIL', 'REQUEST_ERROR'}:
            if counts['success'] is None or counts['failed'] is None or counts['total'] is None:
                raise ValueError("scope request counters are missing")
            if counts['total'] != counts['success'] + counts['failed'] + (counts['cancelled'] or 0):
                raise ValueError("scope request counters do not reconcile")
            if row['status'] in SUCCESS_STATES and counts['success'] <= 0:
                raise ValueError('successful measurement contains no successful requests')
            if scope.get('planned_verified', (scope.get('counts') or {}).get('planned_verified', False)) and counts['planned'] != counts['total']:
                raise ValueError("scope did not complete planned logical requests")
            original_scope = dict(scope)
            original_file = original_scope.get('source_csv') or original_scope.get('result_csv')
            if original_file:
                original_scope['source_csv'] = str(resolve(original_file))
            original_row = normalize_scope_row(original_scope, host_output_dir)
            if not values_equal(original_row.get('total_req'), counts['total']):
                raise ValueError('scope CSV and terminal request counters differ')
    if row['status'] in {'failed', 'no_result'} and not any(str(row.get(name, '')).strip() for name in CANONICAL_COLUMNS):
        # A failed invocation may deliberately publish N/A while preserving
        # its partially completed native scopes as forensic evidence.
        return
    source_scope = dict(selected)
    raw_source = source_scope.get('source_csv') or source_scope.get('result_csv')
    if raw_source:
        source_scope['source_csv'] = str(resolve(raw_source))
    source = normalize_scope_row(source_scope, host_output_dir)
    fields = list(CANONICAL_COLUMNS)
    if 'repeat_rate' in source:
        fields.append('repeat_rate')
    mismatches = compare_fields(row, source, fields)
    if mismatches:
        raise ValueError("summary measurements differ from selected scope: " + ', '.join(item.split(':', 1)[0] for item in mismatches))


def validate_results(result_dir: Path, host_output_dir: Path, require_scopes: bool = False,
                     expected_statuses: Optional[List[str]] = None) -> List[Tuple[str, bool, str]]:
    checks = []
    csv_path, log_path = result_dir / 'all_results.csv', result_dir / 'execution.log'
    add(checks, 'result dir exists', result_dir.is_dir(), str(result_dir))
    add(checks, 'final csv exists', csv_path.is_file(), str(csv_path))
    add(checks, 'execution log exists', log_path.is_file(), str(log_path))
    if not csv_path.is_file():
        return checks
    try:
        with csv_path.open(newline='', encoding='utf-8-sig') as handle:
            reader = csv.DictReader(handle, strict=True)
            if reader.fieldnames != CSV_COLUMNS:
                raise ValueError('unexpected summary schema')
            rows = list(reader)
        add(checks, 'csv has rows', bool(rows), 'rows=' + str(len(rows)))
        if not rows:
            raise ValueError('empty summary')
        statuses = [row.get('status', '') for row in rows]
        add(checks, 'csv status nonempty', all(statuses), ','.join(statuses))
        add(checks, 'csv row field count', all(len(row) == 23 for row in rows),
            ','.join(str(len(row)) for row in rows))
        add(checks, 'csv no failed/no_result', all(status not in {'failed', 'no_result'} for status in statuses),
            ','.join(statuses))
        if expected_statuses:
            add(checks, 'csv expected statuses', statuses == expected_statuses,
                'actual=%s expected=%s' % (statuses, expected_statuses))
        add(checks, 'final states successful', all(row.get('status') in SUCCESS_STATES | {'peak_round'} for row in rows)
            and any(row.get('status') in SUCCESS_STATES for row in rows), ','.join(str(row.get('status', '')) for row in rows))
        index = load_scope_index(result_dir)
        if require_scopes and index is None:
            raise ValueError('new release requires scope index')
        if index is not None and set(index) != set(range(len(rows))):
            raise ValueError('scope index does not cover every summary row')
        reports = load_peak_best_by_case(extract_peak_report_paths(log_path.read_text(encoding='utf-8') if log_path.exists() else '', host_output_dir)) if index is None else {}
        add(checks, 'scope provenance mode', True, 'scoped' if index is not None else 'legacy; missing source fields are not independently verified')
        verified_manifests = set()
        for ordinal, row in enumerate(rows):
            label = str(ordinal) + ':' + str(row.get('scenario', ''))
            try:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError('incomplete summary record')
                if row.get('status') not in ROW_STATES:
                    raise ValueError('unknown summary status')
                if index is not None:
                    check_indexed_row(row, index[ordinal], result_dir, host_output_dir, verified_manifests)
                else:
                    validate_summary_row(row)
                    if row['status'] == 'passed':
                        archive = find_archive(host_output_dir, row['scenario'], row['current_time'])
                        if archive is None:
                            raise ValueError('fixed archive missing or ambiguous')
                        verify_archive_manifest(archive / 'archive_manifest.json')
                        source = read_last_source_result(archive / 'aisbench_result.csv')
                        if compare_fields(row, source, list(CANONICAL_COLUMNS)):
                            raise ValueError('fixed row differs from original scope')
                    elif row['status'] in PEAK_FINAL_STATES:
                        source = reports.get(row['scenario'])
                        if source is None or source.get('passed') not in {'yes', True}:
                            raise ValueError('peak report missing or not passing')
                        if compare_fields(row, source, [name for name in [*CANONICAL_COLUMNS, 'repeat_rate'] if name in source]):
                            raise ValueError('peak row differs from original report')
                    elif row['status'] == 'peak_round':
                        # Legacy live rounds have no stable per-row trial ID.
                        # Require a matching original trace instead of skipping.
                        paths = extract_peak_report_paths(log_path.read_text(encoding='utf-8'), host_output_dir)
                        candidates = [item for path in paths if path.is_file() for item in json.loads(path.read_text(encoding='utf-8')).get('all', [])]
                        if not any(item.get('case_name') == row['scenario'] and not compare_fields(row, item, [name for name in CANONICAL_COLUMNS if name in item]) for item in candidates):
                            raise ValueError('peak round original trace missing')
                add(checks, 'row provenance: ' + label, True, 'all available source fields and applicability checked')
            except (OSError, ValueError, TypeError, KeyError, csv.Error) as error:
                add(checks, 'row provenance: ' + label, False, type(error).__name__ + ': ' + str(error))
    except (OSError, ValueError, TypeError, KeyError, csv.Error) as error:
        add(checks, 'summary structure and scope coverage', False, type(error).__name__ + ': ' + str(error))
    return checks


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
    expected = [part.strip() for part in args.expected_statuses.split(',') if part.strip()]
    checks = validate_results(Path(args.result_dir), Path(args.host_output_dir), args.require_scopes, expected)
    if args.report_file:
        write_report(Path(args.report_file), checks)
    passed = sum(1 for _, ok, _ in checks if ok)
    failed = len(checks) - passed
    print("PASS=%d FAIL=%d" % (passed, failed))
    for name, ok, detail in checks:
        print("%s | %s | %s" % ("PASS" if ok else "FAIL", name, detail))
    return int(bool(failed))


if __name__ == "__main__":
    sys.exit(main())
