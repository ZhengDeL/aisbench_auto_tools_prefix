"""Offline contracts: python -B -m unittest discover -s add -p test_matrix_contracts.py.

Uses synthetic CSVs and a tiny Python child process, with no model, Docker,
tokenizer, network, or repository runtime configuration access.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
ADD_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ADD_DIR))
import aisbench_perf_matrix as matrix
import aisbench_result_adapter as adapter
import aisbench_artifact_archive as archive_tools
import validate_e2e_outputs as e2e
import validate_result_integrity as integrity


BASE_CASE = dict(
    name="offline_case", input_len=8192, output_len=1024,
    min_concurrency=1, max_concurrency=2, request_rate="0",
    request_multiplier=1, ttft_limit=30, tpot_limit=1000, wait_seconds=0,
)


def config(cap=2, failed=0):
    return matrix.RunnerConfig("offline", sys.executable, "", "TTFT avg", "TPOT avg",
                               cap, {}, [], "", [], failed)


def evaluation(concurrency=1, passed=True, metrics=None):
    row = dict(metrics or {})
    return matrix.EvaluationResult(1, concurrency, concurrency, passed, "offline", 0,
                                   row, row, [], "", "", "", "", "", "")


def source_row():
    row = dict.fromkeys(adapter.CANONICAL_COLUMNS, "1")
    row.update(current_time="offline", input_len="8192", output_len="1024", total_req="2")
    row.update({"TTFT avg": "1234", "TTFT P90": "1500", "TPOT avg": "20", "TPOT SLO_P90": "25"})
    return row


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aisbench_offline_contract_")
        self.root = Path(temporary.name).resolve()
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.root.name.startswith("aisbench_offline_contract_"))
        self.addCleanup(temporary.cleanup)


class ResultContracts(IsolatedTest):
    def write(self, headers=None, row=None, rows=True, extra=()):
        self.path = self.root / "source.csv"
        headers = list(headers if headers is not None else adapter.CANONICAL_COLUMNS)
        row = source_row() if row is None else row
        with self.path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            if rows:
                writer.writerow([row.get(name, "extra") for name in headers] + list(extra))
        return self.path

    def cli(self, test_type="stream"):
        return subprocess.run([sys.executable, "-B", str(ADD_DIR / "aisbench_result_adapter.py"), str(self.path), "--test-type", test_type],
                              text=True, capture_output=True)

    def assert_rejected(self):
        with self.assertRaises((ValueError, csv.Error)):
            adapter.read_last_result(self.path)
        with self.assertLogs(level="WARNING"):
            self.assertEqual(matrix.read_last_csv_row(self.path), {})
        with self.assertRaises((ValueError, csv.Error)):
            integrity.read_last_source_result(self.path)

    def test_legacy_new_reordered_and_extra_columns(self):
        old = list(adapter.CANONICAL_COLUMNS)
        new = ["model_name", *old[:7], "repeat_rate", *old[7:], "E2EL avg", "E2EL P90"]
        for headers in (old, new, list(reversed(new)), [*new, "future_metric"]):
            with self.subTest(columns=len(headers), first=headers[0]):
                self.write(headers)
                expected = [source_row()[name] for name in old]
                self.assertEqual(adapter.project_result(adapter.read_last_result(self.path)), expected)
                self.assertEqual(adapter.project_result(matrix.read_last_csv_row(self.path)), expected)
                self.assertEqual(integrity.read_last_source_result(self.path), source_row())
                process = self.cli()
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(list(csv.reader(io.StringIO(process.stdout))), [expected])

    def test_partial_upstream_sentinels_cannot_pass(self):
        for name, sentinel in adapter.FAILURE_SENTINELS.items():
            for value in (str(sentinel), str(sentinel) + ".0"):
                with self.subTest(field=name, value=value):
                    self.write(row={**source_row(), name: value})
                    self.assert_rejected()
        self.write(row={**source_row(), "qps": "9999"})
        process = self.cli()
        self.assertEqual(process.returncode, 2)
        self.assertFalse(process.stdout.strip())

    def test_missing_nonfinite_and_negative_numeric_values(self):
        for name in adapter.CANONICAL_COLUMNS[1:]:
            for value in ("unknown", "nan", "inf", "-inf", "-1", ""):
                if name == "prefill_token_throughput" and value == "":
                    continue
                with self.subTest(field=name, value=value):
                    self.write(row={**source_row(), name: value})
                    self.assert_rejected()

    def test_optional_global_prefill_is_shared(self):
        for value, expected in (("9999", ""), ("9999.0", ""), ("", ""), ("6034.4525", "6034.4525"), ("0", "0")):
            with self.subTest(value=value):
                self.write(row={**source_row(), "prefill_token_throughput": value})
                self.assertEqual(adapter.read_last_result(self.path)["prefill_token_throughput"], expected)
                self.assertEqual(matrix.read_last_csv_row(self.path)["prefill_token_throughput"], expected)
                self.assertEqual(integrity.read_last_source_result(self.path)["prefill_token_throughput"], expected)

    def test_bad_structure_and_incomplete_final_row(self):
        columns = list(adapter.CANONICAL_COLUMNS)
        for headers, rows, extra in ((columns[:-1], True, ()), (columns + ["qps"], True, ()),
                                     (columns, False, ()), (columns, True, ("extra",))):
            with self.subTest(columns=len(headers), rows=rows, extra=extra):
                self.write(headers, rows=rows, extra=extra)
                self.assert_rejected()
        self.write(row={**source_row(), "current_time": ""})
        self.assert_rejected()
        self.write()
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow(["offline", "1"])
        self.assert_rejected()

    def test_live_repeat_rate_prefers_source_and_preserves_legacy_fallback(self):
        case = matrix.build_case(BASE_CASE, {}, 1)
        for actual, fallback, expected in (("0.5", "50%", "0.5"), ("50%", "0", "50%"), (None, "50%", "50%")):
            with self.subTest(source=actual, fallback=fallback):
                path = self.root / "live.csv"
                if path.exists():
                    path.unlink()
                row = {"repeat_rate": actual} if actual is not None else {}
                matrix.append_live_peak_round(str(path), case, evaluation(metrics=row), fallback)
                with path.open(encoding="utf-8", newline="") as handle:
                    self.assertEqual(next(csv.DictReader(handle))["repeat_rate"], expected)

    def test_text_latency_values_are_checked_then_all_unavailable(self):
        for value in ("", "0", "0.0", "99999", "99999.0", "15974.2"):
            with self.subTest(value=value):
                raw = {**source_row(), **dict.fromkeys(adapter.TOKEN_LATENCY_COLUMNS, value)}
                self.write(row=raw)
                original = self.path.read_bytes()
                normalized = adapter.read_last_result(self.path, test_type="text")
                self.assertTrue(all(normalized[name] == "" for name in adapter.TOKEN_LATENCY_COLUMNS))
                for name in set(adapter.CANONICAL_COLUMNS) - set(adapter.TOKEN_LATENCY_COLUMNS):
                    self.assertEqual(normalized[name], raw[name])
                process = self.cli("text")
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(next(csv.reader(io.StringIO(process.stdout))), adapter.project_result(normalized))
                self.assertEqual(self.path.read_bytes(), original)

    def test_text_does_not_accept_garbage_or_bad_remaining_metrics(self):
        for name in (*adapter.TOKEN_LATENCY_COLUMNS, "qps", "output_len"):
            for value in ("unknown", "nan", "inf", "-inf", "-1"):
                with self.subTest(field=name, value=value):
                    self.write(row={**source_row(), name: value})
                    with self.assertRaises(ValueError):
                        adapter.read_last_result(self.path, test_type="text")
        self.write(row={**source_row(), "TPOT avg": "99999"})
        self.assertNotEqual(self.cli("stream").returncode, 0)
        self.assertEqual(self.cli("text").returncode, 0)

    def test_stream_single_actual_output_token_checks_then_clears_tpot(self):
        for value in ("", "0", "99999", "99999.0", "20"):
            with self.subTest(value=value):
                row = {**source_row(), "output_len": "1.0", "TPOT avg": value, "TPOT SLO_P90": value}
                self.write(row=row)
                normalized = adapter.read_last_result(self.path)
                self.assertEqual(normalized["TPOT avg"], "")
                self.assertEqual(normalized["TPOT SLO_P90"], "")
                self.assertEqual(normalized["TTFT avg"], row["TTFT avg"])
                self.assertEqual(self.cli().returncode, 0)
                case = matrix.build_case(BASE_CASE, {}, 1)
                case.ttft_limit = case.tpot_limit = 0
                self.assertFalse(matrix.resolve_threshold_reason(case, normalized, "TTFT avg", "TPOT avg")[0])
                self.write(row={**row, "output_len": "2"})
                if value not in ("0", "20"):
                    with self.assertRaises(ValueError):
                        adapter.read_last_result(self.path)
                else:
                    self.assertEqual(adapter.read_last_result(self.path)["TPOT avg"], value)
        for name in ("TTFT avg", "TPOT avg"):
            self.write(row={**source_row(), "output_len": "1", name: "nan"})
            with self.assertRaises(ValueError):
                adapter.read_last_result(self.path)


class ArchiveLookupContracts(IsolatedTest):
    def make_archive(self, scenario, current_time, tag, first_time=None, test_type="stream", latency_values=None):
        source = self.root / "synthetic_source"
        source.mkdir(exist_ok=True)
        outputs = self.root / "outputs"
        row = {**source_row(), "current_time": current_time}
        row.update(latency_values or {})
        with (source / "aisbench_result.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=adapter.CANONICAL_COLUMNS)
            writer.writeheader()
            if first_time is not None:
                writer.writerow({**row, "current_time": first_time})
            writer.writerow(row)
        manifest = archive_tools.archive_performance_run(
            archive_name=scenario, output_dir=str(outputs), run_tag=tag, source_root=source,
            test_type=test_type,
        )
        return Path(manifest["archive_dir"]), row

    def assert_lookup(self, scenario, current_time, expected):
        outputs = self.root / "outputs"
        self.assertEqual(integrity.find_archive(outputs, scenario, current_time), expected)
        self.assertEqual(e2e.find_latest_fixed_archive(outputs, scenario, current_time), expected)

    def test_quoted_glob_name_and_sanitized_collision_use_manifest_identity(self):
        scenario = 'quoted, "name"[*]?_c1'
        actual, _ = self.make_archive(scenario, "2026-01-01 12:34:56", "original")
        # A different original name can map to exactly the same directory prefix.
        collision = archive_tools.sanitize_archive_name(scenario)
        self.assertNotEqual(collision, scenario)
        self.make_archive(collision, "2026-01-01 12:34:56", "collision")
        self.assert_lookup(scenario, "2026-01-01 12:34:56", actual)
        self.assert_lookup(scenario, None, actual)

    def test_current_row_time_uses_last_source_row_and_never_guesses_latest(self):
        scenario = 'repeated, "task"_c1'
        old_time, new_time = "2026-01-01 12:34:56", "2026-01-01 12:34:57"
        old, _ = self.make_archive(scenario, old_time, "a_old")
        newer, _ = self.make_archive(scenario, new_time, "z_new", first_time=old_time)
        self.assert_lookup(scenario, old_time, old)
        self.assert_lookup(scenario, new_time, newer)
        self.assert_lookup(scenario, "2026-01-01 12:34:58", None)
        self.assert_lookup(scenario, "", None)
        self.assert_lookup(scenario, None, None)
        self.make_archive(scenario, old_time, "duplicate_same_time")
        self.assert_lookup(scenario, old_time, None)

    def test_missing_or_wrong_manifest_and_bad_source_do_not_fallback(self):
        scenario = "fixed_case_c1"
        path, _ = self.make_archive(scenario, "offline", "invalid")
        manifest_path = path / "archive_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for payload in ("not json", "[]", json.dumps({**manifest, "archive_name": "other"})):
            manifest_path.write_text(payload, encoding="utf-8")
            self.assert_lookup(scenario, "offline", None)
        manifest_path.unlink()
        self.assert_lookup(scenario, "offline", None)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (path / "aisbench_result.csv").write_text("bad header\n", encoding="utf-8")
        self.assert_lookup(scenario, "offline", None)

    def test_both_validator_entrypoints_match_each_quoted_row_time(self):
        scenario = 'entrypoint, "case"[*]?_c1'
        _, old = self.make_archive(scenario, "2026-01-01 12:34:56", "a_old")
        _, newer = self.make_archive(scenario, "2026-01-01 12:34:57", "z_new")
        result_dir = self.root / "scenario_results"
        result_dir.mkdir()
        final_csv = result_dir / "all_results.csv"
        (result_dir / "execution.log").write_text("offline synthetic run\n", encoding="utf-8")

        def write_final(rows):
            with final_csv.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=integrity.CSV_COLUMNS)
                writer.writeheader()
                for row in rows:
                    writer.writerow({"scenario": scenario, **row, "repeat_rate": "0", "status": "passed"})

        for invalid in (False, True):
            write_final([old, newer] if not invalid else [{**old, "current_time": "unmatched"}])
            for filename in ("validate_e2e_outputs.py", "validate_result_integrity.py"):
                with self.subTest(validator=filename, invalid_time=invalid):
                    process = subprocess.run(
                        [sys.executable, "-B", str(ADD_DIR / filename), "--result-dir", str(result_dir),
                         "--host-output-dir", str(self.root / "outputs")], text=True, capture_output=True,
                    )
                    self.assertEqual(process.returncode, 1 if invalid else 0, process.stdout + process.stderr)
                    if invalid:
                        self.assertIn("FAIL", process.stdout)

    def test_text_archive_context_reaches_both_real_validator_clis(self):
        scenario = 'text, "case"_c1'
        path, _ = self.make_archive(scenario, "offline_text", "text", test_type="text",
                                   latency_values={"TTFT avg": "15974.2", "TTFT P90": "16868.5",
                                                   "TPOT avg": "99999", "TPOT SLO_P90": "99999"})
        normalized = integrity.read_last_source_result(path / "aisbench_result.csv")
        self.assertTrue(all(normalized[name] == "" for name in adapter.TOKEN_LATENCY_COLUMNS))
        self.assert_lookup(scenario, "offline_text", path)
        results = self.root / "results"
        results.mkdir()
        (results / "execution.log").write_text("offline text\n", encoding="utf-8")
        with (results / "all_results.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=integrity.CSV_COLUMNS)
            writer.writeheader()
            writer.writerow({"scenario": scenario, **normalized, "repeat_rate": "0", "status": "passed"})
        for filename in ("validate_e2e_outputs.py", "validate_result_integrity.py"):
            process = subprocess.run([sys.executable, "-B", str(ADD_DIR / filename), "--result-dir", str(results),
                                      "--host-output-dir", str(self.root / "outputs")], text=True, capture_output=True)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        manifest_path = path / "archive_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["extra_metadata"] = {}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assert_lookup(scenario, "offline_text", None)  # Legacy context stays strict stream.
        manifest["extra_metadata"] = {"test_type": "invalid"}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assert_lookup(scenario, "offline_text", None)

    def test_archiver_cli_records_explicit_mode(self):
        source = self.root / "source"
        source.mkdir()
        output = self.root / "outputs"
        with patch.object(archive_tools, "BASE_DIR", source), patch.object(sys, "argv", [
            "archive", "performance", "--archive-name", "text_cli", "--output-dir", str(output), "--test-type", "text",
        ]), patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(archive_tools.main(), 0)
        manifests = list(output.glob("performance_*/archive_manifest.json"))
        self.assertEqual(len(manifests), 1)
        self.assertEqual(json.loads(manifests[0].read_text(encoding="utf-8"))["extra_metadata"]["test_type"], "text")


class ParameterContracts(IsolatedTest):
    def load(self, cases=None, cap=2, failed=0, extra_cli=()):
        path = self.root / "matrix_config.py"
        path.write_text("import json\nDEFAULT_CASE=json.loads(" + repr(json.dumps(BASE_CASE)) + ")\n"
                        + "SEARCH_CASES=json.loads(" + repr(json.dumps([BASE_CASE] if cases is None else cases)) + ")\n"
                        + f"AUTO_EXPAND_MAX_CONCURRENCY={cap!r}\nFAILED_REQ_LIMIT={failed!r}\n", encoding="utf-8")
        with patch.object(matrix, "BASE_DIR", self.root), patch.object(sys, "argv", ["offline", "--config-file", str(path), *extra_cli]):
            args, forwarded = matrix.parse_args()
            return matrix.load_runner_config(args, forwarded)

    def test_positive_integer_dimensions_and_finite_settings(self):
        for name in ("input_len", "output_len", "min_concurrency", "max_concurrency", "request_multiplier"):
            for value in (0, -1, 1.5, True):
                with self.subTest(field=name, value=value), self.assertRaises(ValueError):
                    matrix.build_case({**BASE_CASE, name: value}, {}, 1)
        for name in ("ttft_limit", "tpot_limit", "request_rate"):
            for value in ("nan", "inf", "-inf", True):
                with self.subTest(field=name, value=value), self.assertRaises(ValueError):
                    matrix.build_case({**BASE_CASE, name: value}, {}, 1)
        for value in (-1, 0.5, True):
            with self.subTest(wait=value), self.assertRaises(ValueError):
                matrix.build_case({**BASE_CASE, "wait_seconds": value}, {}, 1)

    def test_caps_failure_limits_and_all_cases_validate_before_execution(self):
        for value in (-1, 1.5, True):
            with self.subTest(cap=value), self.assertRaises(ValueError):
                self.load(cap=value)
            with self.subTest(failed=value), self.assertRaises(ValueError):
                self.load(failed=value)
        for cases in ([BASE_CASE, BASE_CASE],
                      [{**BASE_CASE, "name": "a/b"}, {**BASE_CASE, "name": "a_b"}],
                      [BASE_CASE, {**BASE_CASE, "name": "second", "args": {"npu_num": 0}}]):
            with self.subTest(cases=cases), self.assertRaises(ValueError):
                self.load(cases=cases)
        loaded = self.load(cases=[{**BASE_CASE, "min_concurrency": 4, "max_concurrency": 8}], cap=2)
        self.assertEqual((loaded.auto_expand_max_concurrency, loaded.cases[0].max_concurrency), (2, 8))
        self.assertEqual(self.load(cap=0).auto_expand_max_concurrency, 0)
        with self.assertRaises(ValueError):
            self.load(extra_cli=["--repeat-rate", "nan"])

    def test_prefix_false_overrides_inherited_flag_and_typos_fail(self):
        for defaults, globals_, global_forward in (({}, {"prefix_test": True}, []),
                                                    ({}, {}, ["--prefix_test"]),
                                                    ({"forward_args": ["--prefix_test"]}, {}, [])):
            with self.subTest(defaults=defaults, global_args=globals_):
                case = matrix.build_case({**BASE_CASE, "prefix_test": False}, defaults, 1)
                runner = config()
                runner.global_args, runner.global_forward_args = globals_, global_forward
                self.assertNotIn("--prefix_test", matrix.build_run_command(runner, case, 1, 1))
        with self.assertRaises(ValueError):
            matrix.build_case({**BASE_CASE, "prefix_test": "tru"}, {}, 1)
        with self.assertRaises(ValueError):
            matrix.args_dict_to_cli({"prefix_test": "false"})

    def test_single_case_cli_override_preserves_prefix_case_properties(self):
        case = {**BASE_CASE, "dataset_type": "prefix_cache", "prefix_num": 2,
                "repeat_rate": "50%", "seed": 7, "dp": 2, "prefix_test": True}
        loaded = self.load(cases=[case], extra_cli=["--max-concurrency", "3"])
        command = matrix.build_run_command(loaded, loaded.cases[0], 1, 1)
        self.assertEqual(loaded.cases[0].max_concurrency, 3)
        for name, expected in (("dataset_type", "prefix_cache"), ("prefix_num", "2"),
                               ("repeat_rate", "50%"), ("seed", "7"), ("dp", "2")):
            self.assertEqual(command[command.index("--" + name) + 1], expected)
        self.assertIn("--prefix_test", command)

    def test_effective_known_arguments_validate_and_extensions_pass_through(self):
        invalid = (["--npu_num", "0"], ["--dp=0"], ["--prefix_num", "-1"], ["--repeat", "0"],
                   ["--length_mean", "2"], ["--length_min", "3", "--length_max", "2"],
                   ["--length_mean", "2", "--length_std", "nan"], ["--repeat_rate", "101%"],
                   ["--test_type", "typo"], ["--dataset_type", "typo"], ["--prefix_test=false"])
        for tokens in invalid:
            with self.subTest(tokens=tokens), self.assertRaises(ValueError):
                matrix.validate_aisbench_arguments(tokens)
        for tokens in (["--npu_num", "0", "--npu_num=2"], ["--repeat_rate", "50%"],
                       ["--new_extension", "preserved"], ["--seed", "-1"]):
            with self.subTest(tokens=tokens):
                matrix.validate_aisbench_arguments(tokens)
        loaded = self.load(extra_cli=["--new_extension", "preserved"])
        command = matrix.build_run_command(loaded, loaded.cases[0], 2, 2)
        self.assertEqual(command[command.index("--new_extension") + 1], "preserved")

    def test_peak_rejects_text_and_single_token_before_any_case_runs(self):
        for case in ({**BASE_CASE, "output_len": 1},
                     {**BASE_CASE, "args": {"test_type": "text"}},
                     {**BASE_CASE, "forward_args": ["--test_type=text"]}):
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.load(cases=[BASE_CASE, {**case, "name": "second_invalid"}])
        case = matrix.build_case(BASE_CASE, {}, 1)
        runner = config()
        runner.global_args = {"test_type": "text"}
        case.args = {"test_type": "stream"}
        matrix.build_run_command(runner, case, 1, 1)  # Effective override wins.

    def test_threshold_units_equality_disabled_and_invalid_metrics(self):
        case = matrix.build_case({**BASE_CASE, "ttft_limit": 1.234, "tpot_limit": 20}, {}, 1)
        row = source_row()
        self.assertTrue(matrix.resolve_threshold_reason(case, row, "TTFT avg", "TPOT avg")[0])
        case.ttft_limit = 1.233
        self.assertFalse(matrix.resolve_threshold_reason(case, row, "TTFT avg", "TPOT avg")[0])
        for limit in (0, -1):
            case.ttft_limit = case.tpot_limit = limit
            self.assertTrue(matrix.resolve_threshold_reason(case, row, "TTFT avg", "TPOT avg")[0])
            for value in ("nan", "inf", "-1", "99999", ""):
                with self.subTest(limit=limit, value=value):
                    self.assertFalse(matrix.resolve_threshold_reason(case, {**row, "TTFT avg": value}, "TTFT avg", "TPOT avg")[0])
        for value in (float("nan"), float("inf")):
            case.ttft_limit = value
            self.assertFalse(matrix.resolve_threshold_reason(case, row, "TTFT avg", "TPOT avg")[0])


class RepeatedConcurrencyContracts(IsolatedTest):
    def expand(self, values, **changes):
        from validate_addon import expand_scenarios_for_simulation
        scenario = dict(name="repeat", input_len=16, output_len=4, request_rate=0,
                        concurrency=values, **changes)
        with patch.dict(os.environ, {"DATA_NUM_MULTIPLIER": "7"}):
            return expand_scenarios_for_simulation([scenario])

    def test_user_list_preserves_order_and_adds_one_peak(self):
        fixed, peak = self.expand([1, 10, 10, 20, 20, 50, 100, "max"])
        self.assertEqual([line.split("|", 1)[0] for line in fixed], [
            "repeat_c1", "repeat_c10", "repeat_c10_run2", "repeat_c20",
            "repeat_c20_run2", "repeat_c50", "repeat_c100"])
        self.assertEqual(len(peak), 1)
        self.assertTrue(peak[0].startswith("repeat_peak|"))
        for line, concurrency in zip(fixed, [1, 10, 10, 20, 20, 50, 100]):
            args = shlex.split(line.split("|", 1)[1])
            self.assertEqual(args[args.index("--concurrency") + 1], str(concurrency))
            self.assertEqual(args[args.index("--data_num") + 1], str(concurrency * 7))

    def test_nonadjacent_numeric_strings_and_explicit_request_count(self):
        for extra in ({}, {"data_num": 3}):
            with self.subTest(extra=extra):
                fixed, peak = self.expand([1, "1", 10, 1, "10", 1], **extra)
                self.assertEqual([line.split("|", 1)[0] for line in fixed], [
                    "repeat_c1", "repeat_c1_run2", "repeat_c10", "repeat_c1_run3",
                    "repeat_c10_run2", "repeat_c1_run4"])
                self.assertEqual(peak, [])
                for line, concurrency in zip(fixed, [1, 1, 10, 1, 10, 1]):
                    args = shlex.split(line.split("|", 1)[1])
                    self.assertEqual(args.count("--data_num"), 1)
                    self.assertEqual(args[args.index("--data_num") + 1], str(extra.get("data_num", concurrency * 7)))

    def test_duplicate_max_and_cross_scenario_collisions_remain_invalid(self):
        from validate_addon import expand_scenarios_for_simulation
        with self.assertRaises(ValueError):
            self.expand([1, "max", 1, "max"])
        for names in (("same", "same"), ("same.name", "same_name")):
            with self.subTest(names=names), self.assertRaises(ValueError):
                expand_scenarios_for_simulation([
                    {"name": name, "concurrency": [1, 1]} for name in names])

    def test_real_shell_batch_keeps_repeated_csv_rows_and_archives(self):
        from validate_addon import find_bash, run_bash_code
        bash = find_bash()
        if not bash:
            self.skipTest("Bash unavailable; no Docker or model requests are used")
        fixed, peak = self.expand([1, 10, 10, 20, 20, 50, 100, "max"])
        source = (ADD_DIR / "run_perf_scenarios.sh").read_text(encoding="utf-8")
        tail = source[source.index("######################## 4. 循环执行固定"):]
        helpers = []
        for name in ("append_empty_result", "append_source_result", "read_request_counts"):
            match = re.search(r"^" + name + r"\(\) \{\n.*?^\}[ \t]*$", source, re.M | re.S)
            self.assertIsNotNone(match)
            helpers.append(match.group(0))
        tools = self.root / "tools"
        add = tools / "add"
        add.mkdir(parents=True)
        for name in ("aisbench_artifact_archive.py", "aisbench_result_adapter.py"):
            shutil.copy2(ADD_DIR / name, add / name)
        output = self.root / "output"
        results = output / "scenario_results" / "offline"
        results.mkdir(parents=True)
        calls = self.root / "calls.jsonl"
        row = source_row()
        row.update(input_len="16", output_len="4", rr="0")
        stub = """import argparse, csv, json, os
from pathlib import Path
p = argparse.ArgumentParser()
for name in ('concurrency', 'data_num'):
    p.add_argument('--' + name, type=int, required=True)
a, _ = p.parse_known_args()
calls = Path(os.environ['MOCK_CALLS'])
index = len(calls.read_text().splitlines()) + 1 if calls.exists() else 1
with calls.open('a', encoding='utf-8') as f:
    f.write(json.dumps({'concurrency': a.concurrency, 'data_num': a.data_num}) + '\\n')
row = BASE_ROW.copy()
row.update(current_time='offline_' + str(index), total_req=str(a.data_num),
           max_cc=str(a.concurrency), cc=str(a.concurrency), qps=str(index))
with open('aisbench_result.csv', 'w', newline='', encoding='utf-8') as f:
    w = csv.DictWriter(f, fieldnames=list(row)); w.writeheader(); w.writerow(row)
Path('aisbench.log').write_text('round=' + str(index), encoding='utf-8')
print('Failed Requests: 0')
print('Success Requests: ' + str(a.data_num))
"""
        (tools / "aisbench_test.py").write_text("BASE_ROW = " + repr(row) + "\n" + stub, encoding="utf-8", newline="\n")
        peak_stub = """import argparse, json, os
from pathlib import Path
p = argparse.ArgumentParser()
for name in ('run-tag', 'case-name'):
    p.add_argument('--' + name, required=True)
a, _ = p.parse_known_args()
root = Path(os.environ['MOCK_OUTPUT_ROOT']) / ('peak_' + a.case_name + '_' + a.run_tag)
root.mkdir(parents=True)
row = BASE_ROW.copy()
row.update(concurrency=2, passed='yes')
(root / 'peak_search_report.json').write_text(json.dumps({'best': [row]}), encoding='utf-8')
"""
        (add / "aisbench_perf_matrix.py").write_text("BASE_ROW = " + repr(row) + "\n" + peak_stub, encoding="utf-8", newline="\n")
        interpreter = shlex.quote(sys.executable.replace("\\", "/"))
        prefix = ("python3() { " + interpreter + ' "$@"; }\nexport -f python3\n'
                  'log() { printf "%s\\n" "$*"; }\nfail() { log "ERROR: $*"; exit 1; }\n'
                  'docker() {\n'
                  ' [ "$1" = exec ] || return 91\n shift\n'
                  ' [ "$1" != -i ] || shift\n shift\n'
                  ' if [ "$1" = bash ]; then shift 2; "$BASH" -c "$1"; else "$@"; fi\n}\n'
                  + "\n".join(helpers) + "\n")
        env = {**os.environ, "FIXED_COUNT": str(len(fixed)), "PEAK_COUNT": "1", "TOTAL_TESTS": "8",
               "COMBOS": "\n".join(fixed), "PEAK_COMBOS": "\n".join(peak), "RUN_TIMESTAMP": "offline",
               "HOST_OUTPUT_DIR": output.as_posix(), "RESULTS_HOST_DIR": results.as_posix(),
               "RESULTS_CSV": (results / "all_results.csv").as_posix(), "RESULTS_LOG": (results / "execution.log").as_posix(),
               "WORK_PATH_DETECTED": add.as_posix(), "TOOL_ROOT": tools.as_posix(),
               "CONTAINER_NAME": "offline", "CONTAINER_OUTPUT_DIR": output.as_posix(),
               "HOST_IP": "localhost", "HOST_PORT": "8000", "API_URL": "", "FAILED_REQ_LIMIT": "0",
               "NPU_NUM": "1", "PEAK_HARD_CAP": "0", "MOCK_CALLS": str(calls), "MOCK_OUTPUT_ROOT": str(output)}
        process = run_bash_code(bash, prefix + tail, env=env, timeout=90)
        self.assertEqual(process.returncode, 0, process.stdout[-6000:] + process.stderr)
        invoked = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(invoked, [{"concurrency": cc, "data_num": cc * 7} for cc in [1, 10, 10, 20, 20, 50, 100]])
        with (results / "all_results.csv").open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            summary = list(reader)
            self.assertEqual(len(reader.fieldnames), 23)
        expected_names = [line.split("|", 1)[0] for line in fixed]
        self.assertEqual([r["scenario"] for r in summary], expected_names + ["repeat_peak"])
        self.assertEqual([r["status"] for r in summary], ["passed"] * 7 + ["peak"])
        self.assertEqual(len(set(r["scenario"] for r in summary)), 8)
        manifests = list(output.glob("performance_*/archive_manifest.json"))
        self.assertEqual(len(manifests), 7)
        archived = {json.loads(p.read_text(encoding="utf-8"))["archive_name"]: p.parent for p in manifests}
        self.assertEqual(set(archived), set(expected_names))
        for index, entry in enumerate(summary[:7], 1):
            path = archived[entry["scenario"]]
            self.assertEqual((path / "aisbench.log").read_text(encoding="utf-8"), "round=" + str(index))
            with (path / "aisbench_result.csv").open(encoding="utf-8", newline="") as f:
                self.assertEqual(next(csv.DictReader(f)), {key: entry[key] for key in adapter.CANONICAL_COLUMNS})


class ScenarioFileContracts(IsolatedTest):
    @staticmethod
    def block(scenarios):
        return "SCENARIOS_JSON=$(cat <<'JSON'\n" + json.dumps(scenarios) + "\nJSON\n)\n"

    @staticmethod
    def scenario(**changes):
        return {"name": "file_config", "input_len": 16, "output_len": 4,
                "concurrency": [1], "request_rate": 0, **changes}

    def run_prefix(self, content=None, *, default=False, options=None, missing=False, allexport=False):
        """Execute the real shell preflight with Docker forbidden and no service."""
        from validate_addon import find_bash
        bash = find_bash()
        if not bash:
            self.skipTest("Bash unavailable; scenario file parsing was not executed")
        source = (ADD_DIR / "run_perf_scenarios.sh").read_text(encoding="utf-8")
        prefix = source[:source.index("######################## 0. 初始化结果保存目录")]
        add = self.root / "isolated checkout" / "add"
        add.mkdir(parents=True, exist_ok=True)
        config_path = add / "env.env" if default else self.root / "selected scenarios.env"
        if content is None:
            content = (ADD_DIR / "env.env").read_text(encoding="utf-8")
        if not missing:
            config_path.write_bytes(content.encode("utf-8"))
        elif config_path.exists():
            config_path.unlink()
        interpreter = shlex.quote(sys.executable.replace("\\", "/"))
        prelude = ('set -a\n' if allexport else '') + ('python3() { ' + interpreter + ' "$@"; }\n'
                   'docker() { echo UNEXPECTED_DOCKER_CALL; return 88; }\n')
        tail = '''
python3 - "$COMBOS" "$PEAK_COMBOS" "$SCENARIOS_JSON" "$HOST_IP" <<'PY'
import json, os, sys
print('FILE_CONTRACT=' + json.dumps({
    'fixed': sys.argv[1].splitlines(), 'peak': sys.argv[2].splitlines(),
    'scenarios': json.loads(sys.argv[3]), 'host': sys.argv[4],
    'exported_scenarios': os.environ.get('SCENARIOS_JSON'),
}))
PY
'''
        script = add / "run_perf_scenarios.sh"
        script.write_bytes((prelude + prefix + tail).encode("utf-8"))
        env = {**os.environ, "NPU_NUM": "1", "DATA_NUM_MULTIPLIER": "7", "PEAK_HARD_CAP": "0",
               "MAX_CONTEXT_LEN": "512000", "PEAK_MIN_CONCURRENCY": "1", "PEAK_MAX_MULTIPLIER": "20",
               "PEAK_DATA_MULTIPLIER": "4", "PEAK_WAIT_SECONDS": "0", "PEAK_TTFT_LIMIT": "5",
               "PEAK_TPOT_LIMIT": "50", "FAILED_REQ_LIMIT": "0", "HOST_IP": "environment-host.invalid"}
        env.pop("SCENARIOS_JSON_OVERRIDE", None)
        env.pop("SCENARIOS_JSON", None)
        env.update(options or {})
        command = [bash, "-uo", "pipefail", script.as_posix()]
        if not default:
            command.append(config_path.as_posix())
        result = subprocess.run(command, cwd=self.root, env=env, text=True, capture_output=True, timeout=45)
        payloads = [line[len("FILE_CONTRACT="):] for line in result.stdout.splitlines()
                    if line.startswith("FILE_CONTRACT=")]
        return result, json.loads(payloads[-1]) if payloads else None

    def assert_success(self, result, payload):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNotNone(payload, result.stdout + result.stderr)
        self.assertNotIn("UNEXPECTED_DOCKER_CALL", result.stdout)

    def test_default_env_preserves_both_user_scenarios_and_all_fields(self):
        common = {"input_len": "65536", "concurrency": [1, 10, 20, 50, 100, "max"],
                  "request_rate": "0", "ttft_limit": 15, "tpot_limit": 60,
                  "peak_min_cc": 1, "peak_max_cc": 200, "peak_data_multiplier": 4,
                  "peak_wait": 10, "dataset_type": "prefix_cache", "prefix_num": "1",
                  "repeat_rate": "0.9", "prefix_test": True}
        expected = [{**common, "name": "0_ai_coding_64k256", "output_len": "256"},
                    {**common, "name": "0_ai_coding_64k1k", "output_len": "1024"}]
        result, payload = self.run_prefix(default=True)
        self.assert_success(result, payload)
        self.assertEqual(payload["scenarios"], expected)
        self.assertEqual((len(payload["fixed"]), len(payload["peak"])), (10, 2))
        self.assertIsNone(payload["exported_scenarios"])

    def test_selected_file_and_immediate_edits_override_inherited_json(self):
        for name, count in (("first_file", 2), ("edited_file", 4)):
            scenarios = [self.scenario(name=name, concurrency=[count])]
            result, payload = self.run_prefix(self.block(scenarios), options={
                "SCENARIOS_JSON": self.block([self.scenario(name="stale_environment")])})
            self.assert_success(result, payload)
            self.assertEqual(payload["scenarios"], scenarios)
            self.assertEqual(payload["fixed"][0].split("|", 1)[0], name + "_c" + str(count))
            args = shlex.split(payload["fixed"][0].split("|", 1)[1])
            self.assertEqual(args[args.index("--data_num") + 1], str(count * 7))
            self.assertIsNone(payload["exported_scenarios"])

    def test_file_read_does_not_source_shell_or_scalar_configuration(self):
        marker = self.root / "must_not_execute"
        content = ('export HOST_IP=file-host.invalid\n'
                   + "touch " + shlex.quote(marker.as_posix()) + "\n"
                   + self.block([self.scenario()]))
        result, payload = self.run_prefix(content)
        self.assert_success(result, payload)
        self.assertEqual(payload["host"], "environment-host.invalid")
        self.assertFalse(marker.exists(), "scenario file must be read, never executed")

    def test_scenario_json_remains_unexported_when_allexport_is_enabled(self):
        result, payload = self.run_prefix(self.block([self.scenario()]), allexport=True)
        self.assert_success(result, payload)
        self.assertIsNone(payload["exported_scenarios"])

    def test_sourcing_public_env_retains_json_without_exporting_to_children(self):
        from validate_addon import find_bash, read_scenarios_file, run_bash_code
        bash = find_bash()
        if not bash:
            self.skipTest("Bash unavailable; public env source contract was not executed")
        copied = self.root / "source scenarios.env"
        copied.write_bytes((ADD_DIR / "env.env").read_bytes())
        interpreter = shlex.quote(sys.executable.replace("\\", "/"))
        for setup in ("export SCENARIOS_JSON=stale_environment\n", "set -a\n",
                      "export SCENARIOS_JSON=stale_environment\nset -a\n"):
            with self.subTest(setup=setup):
                code = 'python3() { ' + interpreter + ' "$@"; }\n' + setup
                code += 'source "$PUBLIC_ENV_PATH"\n'
                code += '''python3 - "$SCENARIOS_JSON" <<'PY'
import json, os, sys
print(json.dumps({'scenarios': json.loads(sys.argv[1]),
                  'exported': os.environ.get('SCENARIOS_JSON')}))
PY
'''
                process = run_bash_code(bash, code, env={**os.environ, "PUBLIC_ENV_PATH": copied.as_posix()})
                self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                payload = json.loads(process.stdout)
                self.assertEqual(payload["scenarios"], read_scenarios_file())
                self.assertIsNone(payload["exported"])

    def test_file_concurrency_repetitions_remain_ordered_and_separate(self):
        values = [1, 10, 10, 20, 20, 50, 100, "max"]
        result, payload = self.run_prefix(self.block([self.scenario(concurrency=values)]))
        self.assert_success(result, payload)
        self.assertEqual([line.split("|", 1)[0] for line in payload["fixed"]], [
            "file_config_c1", "file_config_c10", "file_config_c10_run2", "file_config_c20",
            "file_config_c20_run2", "file_config_c50", "file_config_c100"])
        self.assertEqual(len(payload["peak"]), 1)

    def test_missing_multiple_malformed_blocks_and_json_fail_before_docker(self):
        valid = self.block([self.scenario()])
        cases = (("missing block", "export HOST_IP=unused\n"),
                 ("multiple blocks", valid + valid),
                 ("unterminated block", valid.replace("\nJSON\n)", "\nJSON")),
                 ("unquoted delimiter", valid.replace("<<'JSON'", "<<JSON")),
                 ("malformed JSON", "SCENARIOS_JSON=$(cat <<'JSON'\n[broken]\nJSON\n)\n"),
                 ("wrong JSON root", self.block({"name": "object"})),
                 ("empty scenario list", self.block([])))
        for label, content in cases:
            with self.subTest(label=label):
                result, payload = self.run_prefix(content)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIsNone(payload)
                self.assertNotIn("UNEXPECTED_DOCKER_CALL", result.stdout)
                self.assertRegex(result.stdout + result.stderr, r"SCENARIOS|ValueError|JSONDecodeError")
        result, payload = self.run_prefix(missing=True)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(payload)
        self.assertNotIn("UNEXPECTED_DOCKER_CALL", result.stdout)

    def test_legacy_override_even_empty_fails_with_migration_instruction(self):
        for value in ("", "[]", json.dumps([self.scenario(name="old_override")])):
            with self.subTest(value=value):
                result, payload = self.run_prefix(self.block([self.scenario()]),
                                                  options={"SCENARIOS_JSON_OVERRIDE": value})
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIsNone(payload)
                self.assertNotIn("UNEXPECTED_DOCKER_CALL", result.stdout)
                message = result.stdout + result.stderr
                self.assertIn("SCENARIOS_JSON_OVERRIDE", message)
                self.assertIn("env", message)


class ProductionModeContracts(IsolatedTest):
    def fragment(self, name):
        source = (ADD_DIR / "run_perf_scenarios.sh").read_text(encoding="utf-8")
        match = re.search(re.escape(name) + r"=\$\(python3 -[^\n]*<<'PY'\n(.*?)^PY$", source, re.M | re.S)
        self.assertIsNotNone(match, name)
        return match.group(1)

    def test_fixed_mode_is_parsed_from_effective_command_and_forwarded(self):
        source = self.fragment("SCENARIO_TEST_TYPE")
        for command, expected in (("--data_num 1", "stream"), ("--test_type text", "text"),
                                  ("--test_type stream --test_type=text --future_option 'two words'", "text")):
            process = subprocess.run([sys.executable, "-c", source, command], text=True, capture_output=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stdout.strip(), expected)
        process = subprocess.run([sys.executable, "-c", source, "--test_type typo"], text=True, capture_output=True)
        self.assertNotEqual(process.returncode, 0)
        shell = (ADD_DIR / "run_perf_scenarios.sh").read_text(encoding="utf-8")
        self.assertEqual(shell.count("--test-type '$SCENARIO_TEST_TYPE'"), 2)

    def test_real_shell_preflight_rejects_peak_text_and_one_token(self):
        from validate_addon import expand_scenarios_for_simulation
        for changes in ({"test_type": "text", "output_len": 1024}, {"output_len": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                expand_scenarios_for_simulation([{"name": "invalid_peak", "input_len": 16, "concurrency": ["max"], **changes}])
        for changes in ({"test_type": "text", "output_len": 1}, {"test_type": "stream", "output_len": 1}):
            fixed, peak = expand_scenarios_for_simulation([{"name": "valid_fixed", "input_len": 16, "concurrency": [1], **changes}])
            self.assertEqual((len(fixed), len(peak)), (1, 0))

    def test_actual_endpoint_display_uses_url_and_redacts_sensitive_parts(self):
        source = self.fragment("DISPLAY_ENDPOINT")
        cases = (("", "fallback.invalid:9"),
                 ("https://api.example.invalid:8443/v1/chat/completions", "https://api.example.invalid:8443/v1/chat/completions"),
                 ("https://user:private-value@api.example.invalid:8443/v1?key=private-value#private-value", "https://api.example.invalid:8443/v1"),
                 ("https://api.example.invalid/v1?key=private-value", "https://api.example.invalid/v1"),
                 ("not a valid URL", "API_URL (invalid)"))
        for url, expected in cases:
            with self.subTest(url=url):
                process = subprocess.run([sys.executable, "-c", source, url, "fallback.invalid", "9"], text=True, capture_output=True)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(process.stdout.strip(), expected)
                self.assertNotIn("private-value", process.stdout)

    def test_public_env_is_actually_sourced_without_carriage_returns(self):
        from validate_addon import find_bash, run_bash_code
        bash = find_bash()
        if not bash:
            self.skipTest("Bash is required to execute the public environment contract")
        content = (ADD_DIR / "env.env").read_bytes()
        self.assertNotIn(b"\r", content)
        self.assertFalse(content.startswith(b"\xef\xbb\xbf"), "public shell environment must not contain a UTF-8 BOM")
        copied = self.root / "public.env"
        copied.write_bytes(content)
        names = ("HOST_IP", "MODEL_NAME", "NPU_NUM", "PEAK_HARD_CAP")
        expected = []
        for name in names:
            match = re.search(r"^export " + re.escape(name) + r"=(.+)$", content.decode("utf-8"), re.M)
            self.assertIsNotNone(match, name)
            value = shlex.split(match.group(1))
            self.assertEqual(len(value), 1)
            expected.append(value[0])
        code = 'unset HOST_IP MODEL_NAME NPU_NUM PEAK_HARD_CAP\nsource "$PUBLIC_ENV_PATH"\n'
        code += 'printf "%s\\0" "$HOST_IP" "$MODEL_NAME" "$NPU_NUM" "$PEAK_HARD_CAP"\n'
        process = run_bash_code(bash, code, env={**os.environ, "PUBLIC_ENV_PATH": copied.as_posix()})
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.split("\0"), [*expected, ""])


class EvaluationContracts(IsolatedTest):
    def test_native_and_labelled_request_counts(self):
        path = self.root / "counter.log"
        cases = (("2026-01-01 12:34:56 Failed Requests: 0\n", 0),
                 ("│ Failed Requests │ total │ \x1b[31m1\x1b[0m │\n", 1),
                 ("Failed Requests: 1,000\n", 1000), ("Failed Requests: 3.000\n", 3),
                 ("Failed Requests: 0.5\n", None), ("Failed Requests: -1\n", None),
                 ("Failed Requests: 1e3\n", None), ("Failed Requests: unknown 0\n", None),
                 ("Failed Requests: 0\nFailed Requests: unknown\n", None))
        for text, expected in cases:
            with self.subTest(log=text):
                path.write_text(text, encoding="utf-8")
                self.assertEqual(matrix.parse_failed_requests(path), expected)
        path.write_text("│ Success Requests │ total │ 0 │\n", encoding="utf-8")
        self.assertEqual(matrix.parse_success_requests(path), 0)

    def test_real_child_evaluation_fails_closed_for_missing_counters(self):
        script = self.root / "synthetic_benchmark.py"
        logger = logging.getLogger("aisbench-offline-child")
        logger.addHandler(logging.NullHandler())
        runner = config(failed=2)
        runner.output_dir = str(self.root / "raw_outputs")
        case = matrix.build_case(BASE_CASE, {}, 1)
        cases = (("Failed Requests: 0\nSuccess Requests: 1", True),
                 ("Failed Requests: 1,000\nSuccess Requests: 1", False),
                 ("Success Requests: 1", False),
                 ("Failed Requests: unknown\nSuccess Requests: 1", False),
                 ("Failed Requests: 0\nSuccess Requests: 0", False))
        with patch.object(matrix, "BASE_DIR", self.root), patch.object(matrix, "AISBENCH_TEST_SCRIPT", script):
            for sequence, (text, passed) in enumerate(cases, 1):
                with self.subTest(log=text):
                    row = source_row()
                    script.write_text("import csv\nwith open('aisbench_result.csv','w',newline='',encoding='utf-8') as f:\n"
                                      + " w=csv.writer(f);w.writerow(" + repr(list(row)) + ");w.writerow(" + repr(list(row.values())) + ")\n"
                                      + "print(" + repr(text) + ")\n", encoding="utf-8")
                    result = matrix.run_single_evaluation(case, runner, self.root / "runs", sequence, 1, logger)
                    self.assertEqual(result.return_code, 0)
                    self.assertEqual(result.passed, passed, result.reason)

    def test_search_oracle_boundaries_cap_and_probe_cache(self):
        # cap bounds expansion only; an initial range above it remains valid.
        cases = ((1, 1, 4, 4, 4), (1, 1, 1, 4, 1), (1, 2, 2, 2, 2),
                 (8, 16, 16, 0, None), (8, 16, 16, 3, 3),
                 (1, 4, 13, 20, 13), (1, 4, 16, 7, 7),
                 (1, 8, 4, 8, 8), (1, 4, 0, 7, 7))
        for low, high, cap, cutoff, expected in cases:
            with self.subTest(low=low, high=high, cap=cap, cutoff=cutoff):
                calls = []

                def measure(**kwargs):
                    concurrency = kwargs["concurrency"]
                    calls.append(concurrency)
                    return evaluation(concurrency, concurrency <= cutoff)

                case = matrix.build_case({**BASE_CASE, "min_concurrency": low, "max_concurrency": high}, {}, 1)
                searcher = matrix.PeakSearcher(case, config(cap), self.root / "search", logging.getLogger("oracle"))
                with patch.object(matrix, "run_single_evaluation", side_effect=measure):
                    best, history = searcher.search()
                    self.assertEqual(best.concurrency if best else None, expected)
                    self.assertEqual(len(calls), len(set(calls)))
                    self.assertEqual(calls, [item.concurrency for item in history])
                    for item in history:
                        self.assertIs(searcher.evaluate(item.concurrency), item)
                    self.assertEqual(len(calls), len(history))
        self.assertEqual(matrix.calculate_num_prompts(3, 10), 30)
        self.assertEqual(matrix.calculate_num_prompts(3, 7), 21)


if __name__ == "__main__":
    unittest.main()
