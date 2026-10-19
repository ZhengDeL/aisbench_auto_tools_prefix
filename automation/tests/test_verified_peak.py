"""Deterministic peak-search oracles; real process collection is tested separately."""
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys
AUTOMATION_ROOT = Path(__file__).resolve().parent.parent
CORE_ROOT = AUTOMATION_ROOT / 'core'
TOOLS_ROOT = AUTOMATION_ROOT / 'tools'
RELEASE_ROOT = AUTOMATION_ROOT.parent
for folder in (CORE_ROOT, TOOLS_ROOT):
    sys.path.insert(0, str(folder))

import aisbench_perf_matrix as matrix
import aisbench_configuration as configuration


class VerifiedPeakTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.calls = []

    def search(self, model, cap=16, **options):
        values = dict(name="oracle", input_len=1024, output_len=512,
                      min_concurrency=1, max_concurrency=8, request_rate="0",
                      request_multiplier=4, ttft_limit=15, tpot_limit=60, wait_seconds=0)
        values.update(options)
        case = matrix.build_case(values, {}, 1)
        cfg = matrix.RunnerConfig("", "", "", "TTFT avg", "TPOT avg", cap, {}, [], "", [case])
        engine = matrix.PeakSearcher(case, cfg, self.root, logging.getLogger("verified"))

        def evaluate(**kwargs):
            cc, profile = kwargs["concurrency"], kwargs["profile"]
            self.calls.append((cc, profile))
            state = model(cc, profile, len(self.calls))
            row = dict(cc=str(cc), **{"TTFT avg": "100", "TPOT avg": "10"})
            return matrix.EvaluationResult(len(self.calls), cc, cc * 4, state == "PASS", state,
                                            0, row, row, [], "", "", "", "", "", "",
                                            state=state, profile=profile, trial_id="t" + str(len(self.calls)),
                                            selected_scope_id="scope_001")
        with patch.object(matrix, "run_single_evaluation", side_effect=evaluate):
            best, history = engine.search()
        return best, engine, history

    def test_known_boundary_and_real_confirmation_calls(self):
        best, engine, history = self.search(lambda cc, *_: "PASS" if cc <= 5 else "SLO_FAIL")
        self.assertEqual((best.concurrency, engine.termination), (5, "boundary_confirmed"))
        self.assertEqual(self.calls.count((5, "confirm")), 3)
        self.assertEqual(self.calls.count((6, "confirm")), 3)
        self.assertEqual(len(history), len(self.calls))
        self.assertEqual(engine.summary()["highest_confirmed"], 5)

    def test_cap_applies_to_initial_range_and_confirmation(self):
        best, engine, _ = self.search(lambda *_: "PASS", cap=4, min_concurrency=8, max_concurrency=16)
        self.assertEqual((best.concurrency, engine.termination), (4, "capped"))
        self.assertTrue(all(cc <= 4 for cc, _ in self.calls))
        self.assertEqual(self.calls.count((4, "confirm")), 3)

    def test_initial_min_remains_soft_and_floor_reconfirmed(self):
        best, engine, _ = self.search(lambda cc, *_: "PASS" if cc <= 3 else "SLO_FAIL",
                                       min_concurrency=8, max_concurrency=16)
        self.assertEqual(best.concurrency, 3)
        self.assertIn((2, "explore"), self.calls)
        best, engine, _ = self.search(lambda *_: "SLO_FAIL", min_concurrency=8, max_concurrency=16)
        self.assertIsNone(best)
        self.assertEqual(engine.termination, "no_pass")
        self.assertEqual(self.calls.count((1, "confirm")), 3)

    def test_budget_keeps_observed_separate_from_confirmed(self):
        best, engine, _ = self.search(lambda *_: "PASS", cap=0, max_trials=5)
        self.assertIsNone(best)
        self.assertEqual(engine.termination, "budget_limit")
        self.assertIsNotNone(engine.summary()["highest_observed"])
        self.assertIsNone(engine.summary()["highest_confirmed"])

    def test_invalid_retry_counts_and_request_error_never_shrinks_boundary(self):
        best, engine, _ = self.search(lambda cc, profile, n: "INVALID" if n == 1 else "PASS", cap=2)
        self.assertEqual(engine.termination, "capped")
        self.assertEqual(self.calls[:2], [(1, "explore"), (1, "explore")])
        self.calls.clear()
        best, engine, _ = self.search(lambda *_: "REQUEST_ERROR")
        self.assertIsNone(best)
        self.assertEqual(engine.termination, "request_error")
        self.assertEqual(len(self.calls), 1)

    def test_faults_and_missing_data_do_not_become_capacity_failures(self):
        for state in ("ERROR", "INVALID", "UNSTABLE"):
            with self.subTest(state=state):
                self.calls.clear()
                best, engine, _ = self.search(lambda *_: state)
                self.assertIsNone(best)
                self.assertIn(engine.termination, {"error", "unstable"})
                self.assertLessEqual(len(self.calls), 2)

    def test_confirmation_profile_is_permanent_after_boundary_moves(self):
        best, engine, _ = self.search(
            lambda cc, profile, _: "PASS" if cc <= (5 if profile == "explore" else 3) else "SLO_FAIL")
        self.assertEqual((best.concurrency, engine.termination), (3, "boundary_confirmed"))
        first = next(i for i, (_, profile) in enumerate(self.calls) if profile == "confirm")
        self.assertTrue(all(profile == "confirm" for _, profile in self.calls[first:]))

    def test_mixed_confirmations_are_unstable(self):
        counts = {}
        def model(cc, profile, _):
            counts[(cc, profile)] = counts.get((cc, profile), 0) + 1
            if profile == "confirm" and cc == 5:
                return "SLO_FAIL" if counts[(cc, profile)] == 2 else "PASS"
            return "PASS" if cc <= 5 else "SLO_FAIL"
        best, engine, _ = self.search(model)
        self.assertIsNone(best)
        self.assertEqual(engine.termination, "unstable")

    def test_rps_repeat_parameters_preserved(self):
        best, engine, _ = self.search(lambda *_: "PASS", cap=2, request_rate="3.5", args={"repeat": 2})
        self.assertEqual(engine.summary()["mode"], "rps_limited_cc_search")
        self.assertEqual(engine.case.args["repeat"], 2)
        command = matrix.build_run_command(engine.config, engine.case, 2, 8)
        self.assertEqual(command[command.index("--repeat") + 1], "2")
        self.assertEqual(command[command.index("--request_rate") + 1], "3.5")

    def test_confirmed_lower_fail_higher_pass_is_not_a_capacity_result(self):
        def model(cc, profile, _):
            if profile == "confirm":
                return "SLO_FAIL" if cc == 5 else "PASS"
            return "PASS" if cc <= 5 else "SLO_FAIL"
        best, engine, _ = self.search(model)
        self.assertIsNone(best)
        self.assertEqual(engine.termination, "unstable")

    def test_global_time_budget_is_checked_before_confirmation(self):
        clock = [0.0]
        def model(*args):
            clock[0] += 11
            return "PASS"
        with patch.object(matrix.time, "monotonic", side_effect=lambda: clock[0]):
            best, engine, _ = self.search(model, cap=1, total_timeout=10)
        self.assertIsNone(best)
        self.assertEqual(engine.termination, "budget_limit")
        self.assertEqual(len(self.calls), 1)

    def test_repeat_scope_disagreement_is_unstable(self):
        case = matrix.build_case(dict(name="scopes", input_len=1024, output_len=512,
                                      min_concurrency=1, max_concurrency=2, wait_seconds=0,
                                      ttft_limit=15, tpot_limit=60), {}, 1)
        cfg = matrix.RunnerConfig("", "", "", "TTFT avg", "TPOT avg", 2, {}, [], "", [case])
        def scope(number, ttft):
            return dict(scope_id="s" + str(number), raw_row={"TTFT avg": str(ttft), "TPOT avg": "10"},
                        result_csv="", stdout_log="", artifact_dir="")
        scopes = [scope(1, 1000), scope(2, 40000)]
        report = dict(state="PASS", reason="", main_scopes=scopes, scopes=scopes,
                      return_code=0, raw_row=scopes[-1]["raw_row"], trial_id="trial",
                      selected_scope_id="s2", stdout_log="", stderr_log="")
        (self.root / "run_001_cc_1_explore").mkdir()
        with patch.object(matrix, "execute_native", return_value=report):
            result = matrix.run_single_evaluation(case, cfg, self.root, 1, 1, logging.getLogger("scope"))
        self.assertEqual(result.state, "UNSTABLE")

    def test_accuracy_retained_for_fixed_and_rejected_for_both_peak_entries(self):
        scene = dict(name="accuracy", input_len="1024", output_len="512", concurrency=[4], test_accuracy=True)
        environment = configuration.effective_environment({}, RELEASE_ROOT)
        with patch.object(configuration, "read_scenarios", return_value=[scene]):
            plan = configuration.build_plan(environment, RELEASE_ROOT / "env.env")
            self.assertTrue(plan["fixed"][0]["accuracy"])
            self.assertIn("--test_accuracy", plan["fixed"][0]["args"])
        scene["concurrency"] = [4, "max"]
        with patch.object(configuration, "read_scenarios", return_value=[scene]):
            with self.assertRaisesRegex(ValueError, "精度测试不能使用max"):
                configuration.build_plan(environment, RELEASE_ROOT / "env.env")
        with self.assertRaisesRegex(ValueError, "peak search cannot use test_accuracy"):
            matrix.validate_aisbench_arguments(["--test_accuracy", "--output_len", "512"])

    def actual_evaluation(self, scopes, profile="explore", **options):
        values = dict(name="actual", input_len=1024, output_len=512, min_concurrency=1, max_concurrency=2,
                      wait_seconds=0, ttft_limit=15, tpot_limit=60, confirm_min_requests=100)
        values.update(options)
        case = matrix.build_case(values, {}, 1)
        config = matrix.RunnerConfig("", "", "", "TTFT avg", "TPOT avg", 2, {}, [], "", [case])
        report = dict(state="PASS", reason="", main_scopes=scopes, scopes=scopes, return_code=0,
                      raw_row=scopes[-1]["raw_row"], trial_id="actual", selected_scope_id=scopes[-1]["scope_id"],
                      stdout_log="", stderr_log="")
        with patch.object(matrix, "execute_native", return_value=report):
            return matrix.run_single_evaluation(case, config, self.root, 1, 1,
                                                logging.getLogger("actual"), profile=profile)

    def completed_scope(self, sid, requests=100, ttft="1000", tpot="10"):
        return dict(scope_id=sid, raw_row={"TTFT avg":ttft, "TPOT avg":tpot, "total_req":str(requests)},
                    counts=dict(success=requests, failed=0), result_csv="", stdout_log="", artifact_dir="")

    def test_missing_or_illegal_latencies_are_invalid_even_when_slo_disabled(self):
        for ttft, tpot in [("", "10"), ("1000", "NaN"), ("-1", "10"), ("1000", "99999")]:
            with self.subTest(ttft=ttft, tpot=tpot):
                result = self.actual_evaluation([self.completed_scope("s1",ttft=ttft,tpot=tpot)],
                                                ttft_limit=0, tpot_limit=0)
                self.assertEqual(result.state, "INVALID")
                self.assertEqual(result.metrics, {})
        result = self.actual_evaluation([self.completed_scope("s1",ttft="40000")])
        self.assertEqual(result.state, "SLO_FAIL")

    def test_confirmation_minimum_applies_to_each_actual_repeat_scope(self):
        result = self.actual_evaluation([self.completed_scope("s1",100),self.completed_scope("s2",99)],profile="confirm")
        self.assertEqual(result.state,"INVALID")
        self.assertIn("s2 completed 99",result.reason)
        self.assertIn("confirm_min_requests=100",result.reason)
        self.assertEqual(result.csv_row,{})
        result = self.actual_evaluation([self.completed_scope("s1",100),self.completed_scope("s2",101)],profile="confirm")
        self.assertEqual(result.state,"PASS")
        result = self.actual_evaluation([self.completed_scope("s1",16)],profile="explore")
        self.assertEqual(result.state,"PASS")
        result = self.actual_evaluation([self.completed_scope("s1",16)],profile="confirm",confirm_min_requests=0)
        self.assertEqual(result.state,"PASS")

    def test_confirmation_requires_actual_reconciled_counts(self):
        scope = self.completed_scope("s1",100)
        scope["counts"]["success"] = 99
        result = self.actual_evaluation([scope],profile="confirm")
        self.assertEqual(result.state,"INVALID")
        self.assertIn("reconciled actual completed request counters",result.reason)


if __name__ == "__main__":
    unittest.main()
