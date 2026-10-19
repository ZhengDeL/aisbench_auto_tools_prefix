"""Real child-process checks for immutable-source scope collection."""
import ast
import contextlib
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

AUTOMATION_ROOT = Path(__file__).resolve().parent.parent
CORE_ROOT = AUTOMATION_ROOT / 'core'
TOOLS_ROOT = AUTOMATION_ROOT / 'tools'
RELEASE_ROOT = AUTOMATION_ROOT.parent
for folder in (CORE_ROOT, TOOLS_ROOT):
    sys.path.insert(0, str(folder))

from aisbench_execution import (execute_native, request_count, restore_generation_template,
                               split_native_pipeline, stream_process)
from aisbench_result_adapter import CANONICAL_COLUMNS


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.native = self.root / "native"
        self.native.mkdir()
        self.number = 0
        self.row = dict.fromkeys(CANONICAL_COLUMNS, "1")
        self.row.update(current_time="2026-10-20 00:00:00", input_len="1024", output_len="512",
                        total_req="1", max_cc="1", cc="1", rr="0")

    def run_native(self, code, arguments=(), **kwargs):
        (self.native / "aisbench_test.py").write_text(code, encoding="utf-8")
        before = {path.name: path.read_bytes() for path in self.native.iterdir()}
        self.number += 1
        report = execute_native(self.native, self.root / ("trial_" + str(self.number)),
                                arguments, **kwargs)
        after = {path.name: path.read_bytes() for path in self.native.iterdir()}
        self.assertEqual(before, after, "native source changed or gained files")
        return report

    def direct(self, counters):
        return ("import csv\n"
                + "row=" + repr(self.row) + "\n"
                + "with open('aisbench_result.csv','w',newline='') as h:\n"
                + " w=csv.DictWriter(h,fieldnames=list(row));w.writeheader();w.writerow(row)\n"
                + "print(" + repr(counters) + ")\n")

    def instrument_fixture(self):
        (self.native / "config.py").write_text("OUTPUT_DIR='.'\n", encoding="utf-8")
        (self.native / "save_file.py").write_text(
            "import json\n"
            "def get_data(path,rr,npu,repeat):\n"
            " for line in open(path):\n"
            "  if line.startswith('METRICS '): return json.loads(line[8:]),''\n"
            " raise ValueError('no metrics')\n", encoding="utf-8")
        (self.native / "emit.py").write_text(
            "import json,sys\n"
            + "print('METRICS '+json.dumps(" + repr(self.row) + "))\n"
            + "print('Success Requests: '+('0' if sys.argv[-1]=='bad' else '1'))\n"
            + "print('Failed Requests: '+('1' if sys.argv[-1]=='bad' else '0'))\n"
            + "sys.stderr.buffer.write(b'raw-stderr-\\xff\\n')\n", encoding="utf-8")

    def test_valid_and_missing_counters_fail_closed(self):
        for counters, state in (("Success Requests: 1\nFailed Requests: 0", "PASS"),
                                ("Failed Requests: 0", "INVALID"),
                                ("Success Requests: 1", "INVALID"),
                                ("Success Requests: 0\nFailed Requests: 1", "REQUEST_ERROR")):
            with self.subTest(state=state):
                report = self.run_native(self.direct(counters))
                self.assertEqual(report["state"], state, report["reason"])

    def test_real_repeat_scopes_and_byte_preservation(self):
        self.instrument_fixture()
        code = ("import subprocess,sys,shlex\n"
                "for n in range(2):\n"
                " command='\\\"'+sys.executable+'\\\" emit.py ais_bench ok'\n"
                " subprocess.run(command,shell=True,check=True)\n")
        report = self.run_native(code, ["--repeat", "2"])
        self.assertEqual(report["state"], "PASS", report["reason"])
        self.assertEqual(len(report["main_scopes"]), 2)
        self.assertEqual([s["scope_id"] for s in report["main_scopes"]], ["scope_001", "scope_002"])
        for scope in report["main_scopes"]:
            self.assertIn(b"\xff", Path(scope["stderr_log"]).read_bytes())
            self.assertTrue(Path(scope["result_csv"]).is_file())
            self.assertEqual(scope["counts"]["success"], 1)

    def test_prior_repeat_failure_is_not_hidden_by_last_success(self):
        self.instrument_fixture()
        code = ("import subprocess,sys,shlex\n"
                "for flag in ['bad','ok']:\n"
                " subprocess.run('\\\"'+sys.executable+'\\\" emit.py ais_bench '+flag,shell=True,check=True)\n")
        report = self.run_native(code, ["--repeat", "2"])
        self.assertEqual(report["main_scopes"][-1]["state"], "PASS")
        self.assertEqual(report["state"], "REQUEST_ERROR")

    def test_prefix_preparation_is_not_a_main_repeat(self):
        self.instrument_fixture()
        code = ("import subprocess,sys,shlex\n"
                "for n in range(2):\n"
                " subprocess.run('\\\"'+sys.executable+'\\\" emit.py ais_bench ok',shell=True,check=True)\n")
        report = self.run_native(code, ["--dataset_type", "prefix_cache", "--prefix_test", "--repeat", "5"])
        self.assertEqual([s["phase"] for s in report["scopes"]], ["prefix", "main"])
        self.assertEqual(len(report["main_scopes"]), 1)

    def test_exit_and_timeout_are_execution_errors(self):
        report = self.run_native("import sys\nsys.exit(137)\n")
        self.assertEqual((report["state"], report["return_code"]), ("ERROR", 137))
        report = self.run_native("import time\ntime.sleep(30)\n", timeout=0.3)
        self.assertEqual(report["state"], "ERROR")
        self.assertTrue(report["timed_out"])

    def test_no_result_does_not_reuse_old_csv(self):
        report = self.run_native(self.direct("Success Requests: 1\nFailed Requests: 0"))
        self.assertEqual(report["state"], "PASS")
        (self.native / "aisbench_result.csv").write_text("old data", encoding="utf-8")
        report = self.run_native("print('Success Requests: 1\\nFailed Requests: 0')\n")
        self.assertEqual(report["state"], "INVALID")

    def test_counter_parser_rejects_bad_last_label(self):
        self.assertEqual(request_count("Failed Requests: 1,000", "Failed Requests"), 1000)
        for text in ("Failed Requests: 1e3", "Failed Requests: 0.5",
                     "Failed Requests: 0\nFailed Requests: unknown"):
            self.assertIsNone(request_count(text, "Failed Requests"))

    def test_peak_confirmation_checks_custom_dataset_actual_rows_without_rewriting(self):
        import logging
        import aisbench_perf_matrix as matrix
        dataset = self.root / "custom.jsonl"
        original = b'{"question":"first","answer":"original1"}\n{"question":"second","answer":"original2"}\n'
        dataset.write_bytes(original)
        code = ("import argparse,csv\n"
                "p=argparse.ArgumentParser();p.add_argument('--dataset');p.add_argument('--data_num');a,_=p.parse_known_args()\n"
                "with open(a.dataset) as h:n=sum(bool(x.strip()) for x in h)\n"
                + "row=" + repr(self.row) + ";row['total_req']=str(n)\n"
                + "with open('aisbench_result.csv','w',newline='') as h:\n"
                + " w=csv.DictWriter(h,fieldnames=list(row));w.writeheader();w.writerow(row)\n"
                + "print('Success Requests: '+str(n));print('Failed Requests: 0')\n")
        script = self.native / "aisbench_test.py"
        script.write_text(code, encoding="utf-8")
        case = matrix.build_case(dict(name="custom",input_len=1024,output_len=512,min_concurrency=1,
            max_concurrency=2,wait_seconds=0,confirm_min_requests=4,args={"dataset":str(dataset)}),{},1)
        config = matrix.RunnerConfig("",sys.executable,"","TTFT avg","TPOT avg",2,{},[],"",[case])
        with patch.object(matrix,"BASE_DIR",self.native), patch.object(matrix,"AISBENCH_TEST_SCRIPT",script):
            result = matrix.run_single_evaluation(case,config,self.root,1,1,logging.getLogger("dataset"),profile="confirm")
            self.assertEqual(result.state,"INVALID",result.reason)
            self.assertIn("completed 2 requests, below confirm_min_requests=4",result.reason)
            self.assertEqual(result.metrics,{})
            report = json.loads(Path(result.evaluation_file).read_text(encoding="utf-8"))
            self.assertEqual(report["main_scopes"][0]["counts"]["success"],2)
            self.assertEqual(report["state"],"INVALID")
            result = matrix.run_single_evaluation(case,config,self.root,2,1,logging.getLogger("dataset"),profile="explore")
            self.assertEqual(result.state,"PASS",result.reason)
        self.assertEqual(dataset.read_bytes(),original)
        self.assertEqual(script.read_text(encoding="utf-8"),code)

    def test_only_exact_upstream_tee_template_is_split(self):
        base = ("ais_bench --models vllm_api_chat_temp --datasets gsm8k_gen_0_shot_cot_str_perf "
                "--mode perf --summarizer default_perf --work-dir /workspace/out --debug --num-warmups 0")
        suffix = " 2>&1 | tee -a aisbench.log | tee -a aisbench_all.log"
        self.assertEqual(split_native_pipeline(base + suffix), (base, True))
        for command in ("echo " + base + suffix, base + suffix + " ; echo done", base + " | tee custom.log"):
            self.assertEqual(split_native_pipeline(command), (command, False))

    def test_tee_write_failure_stops_real_process(self):
        original_open = Path.open
        class BrokenWriter:
            def __init__(self, wrapped): self.wrapped = wrapped
            def __enter__(self): return self
            def __exit__(self, *args): return self.wrapped.__exit__(*args)
            def write(self, data): raise OSError("injected disk failure")
            def flush(self): return self.wrapped.flush()
            def fileno(self): return self.wrapped.fileno()
        def opened(path, *args, **kwargs):
            handle = original_open(path, *args, **kwargs)
            return BrokenWriter(handle) if path.name == "bad-tee.log" else handle
        start = time.monotonic()
        with patch.object(Path, "open", opened):
            with self.assertRaises(OSError):
                stream_process([sys.executable, "-c", "import time;print('ready',flush=True);time.sleep(30)"],
                               self.root, self.root / "out.raw", self.root / "err.raw",
                               timeout=10, tee_paths=[self.root / "bad-tee.log"])
        self.assertLess(time.monotonic() - start, 8)

    def test_independent_peak_cli_retains_repeat_and_selected_scope(self):
        self.instrument_fixture()
        code = ("import subprocess,sys\n"
                "for n in range(2):\n"
                " subprocess.run('\\\"'+sys.executable+'\\\" emit.py ais_bench ok',shell=True,check=True)\n")
        (self.native / "aisbench_test.py").write_text(code, encoding="utf-8")
        output = self.root / "outputs"
        (self.native / "config.py").write_text("OUTPUT_DIR=" + repr(str(output)) + "\n", encoding="utf-8")
        case = dict(name="cli", input_len=1024, output_len=512,
                    min_concurrency=1, max_concurrency=1, request_rate="2",
                    request_multiplier=4, ttft_limit=15, tpot_limit=60, wait_seconds=0,
                    confirm_min_requests=1, args={"repeat": 2})
        config = self.root / "peak_config.py"
        config.write_text("DEFAULT_CASE=" + repr(case) + "\nSEARCH_CASES=[]\nAUTO_EXPAND_MAX_CONCURRENCY=1\n", encoding="utf-8")
        before = {path.name: path.read_bytes() for path in self.native.iterdir()}
        env = dict(os.environ, AISBENCH_NATIVE_ROOT=str(self.native), PYTHONDONTWRITEBYTECODE="1")
        completed = subprocess.run([sys.executable, "-B", str(CORE_ROOT / "aisbench_perf_matrix.py"),
                                    "--config-file", str(config)], env=env, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        report = json.loads(next(output.glob("peak_*/peak_search_report.json")).read_text(encoding="utf-8"))
        best = report["best"][0]
        self.assertEqual(best["status"], "peak_capped")
        self.assertEqual(best["selected_scope_id"], "scope_002")
        self.assertEqual(report["summaries"][0]["trial_count"], 4)
        self.assertEqual(report["summaries"][0]["mode"], "rps_limited_cc_search")
        trial = json.loads(Path(best["evaluation_file"]).read_text(encoding="utf-8"))
        self.assertEqual(len(trial["main_scopes"]), 2)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.native.iterdir()})

    def test_accuracy_keeps_original_metric_evidence_and_blanks_perf(self):
        self.instrument_fixture()
        (self.native / "GSM8K.jsonl").write_text(
            "".join(json.dumps({"question": "Question " + str(i), "answer": "Original reasoning.\\n#### " + str(i)})
                    + "\n" for i in range(4)), encoding="utf-8")
        output = self.root / "accuracy_output"
        (self.native / "config.py").write_text("OUTPUT_DIR=" + repr(str(output)) + "\n", encoding="utf-8")
        with (self.native / "emit.py").open("a", encoding="utf-8") as handle:
            handle.write("from pathlib import Path\n"
                         + "exp=Path(" + repr(str(output / "experiment")) + ")\n"
                         + "(exp/'results').mkdir(parents=True)\n"
                         + "(exp/'predictions').mkdir()\n"
                         + "(exp/'results'/'accuracy.json').write_text(json.dumps({'accuracy':75,'correct':3,'total':4}))\n"
                         + "(exp/'predictions'/'items.json').write_text(json.dumps([{'prediction':'x'}]*4))\n"
                         + "print('Current exp folder: '+str(exp))\n")
        code = ("import subprocess,sys\n"
                "subprocess.run('\\\"'+sys.executable+'\\\" emit.py ais_bench ok',shell=True,check=True)\n")
        report = self.run_native(code, ["--test_accuracy", "--data_num", "4"])
        self.assertEqual(report["state"], "PASS", report["reason"])
        scope = report["main_scopes"][0]
        self.assertEqual(scope["accuracy_evaluation"]["numerator"], 3)
        self.assertEqual(scope["accuracy_evaluation"]["denominator"], 4)
        self.assertFalse(scope["accuracy_evaluation"]["independently_scored"])
        self.assertEqual(scope["raw_row"]["TTFT avg"], "")

    def test_exact_native_pipeline_preserves_streams_and_real_exit(self):
        self.instrument_fixture()
        binary = self.root / "bin"
        binary.mkdir()
        emitter = self.native / "emit.py"
        if os.name == "nt":
            (binary / "ais_bench.cmd").write_text('@\"' + sys.executable + '\" \"' + str(emitter) + '\" %*\n', encoding="utf-8")
        else:
            import shlex
            command_file = binary / "ais_bench"
            command_file.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(emitter)) + ' "$@"\n', encoding="utf-8")
            command_file.chmod(0o755)
        command = ("ais_bench --models vllm_api_chat_temp --datasets gsm8k_gen_0_shot_cot_str_perf "
                   "--mode perf --summarizer default_perf --work-dir unused --debug --num-warmups 0"
                   " 2>&1 | tee -a aisbench.log | tee -a aisbench_all.log")
        code = ("import os,subprocess\n"
                + "os.environ['PATH']=" + repr(str(binary)) + "+os.pathsep+os.environ['PATH']\n"
                + "subprocess.run(" + repr(command) + ",shell=True,check=True)\n")
        report = self.run_native(code)
        self.assertEqual(report["state"], "PASS", report["reason"])
        scope = report["main_scopes"][0]
        self.assertEqual(scope["stream_profile"], "upstream-tee-split-v1")
        stdout = Path(scope["stdout_log"]).read_bytes()
        stderr = Path(scope["stderr_log"]).read_bytes()
        self.assertIn(b"METRICS", stdout)
        self.assertNotIn(b"raw-stderr", stdout)
        self.assertIn(b"raw-stderr-\xff", stderr)
        chunks = [json.loads(line) for line in (Path(scope["stdout_log"]).parent / "stream_chunks.jsonl").read_text().splitlines()]
        reconstructed = b"".join((stdout if item["stream"] == "stdout" else stderr)[item["offset"]:item["offset"]+item["size"]]
                                 for item in chunks)
        self.assertEqual((self.root / "trial_1" / "native_runtime" / "aisbench.log").read_bytes(), reconstructed)
        with emitter.open("a", encoding="utf-8") as handle:
            handle.write("sys.exit(7)\n")
        failed = self.run_native(code)
        self.assertEqual(failed["state"], "ERROR")
        self.assertEqual(failed["scopes"][0]["return_code"], 7)

    def test_input_cache_profiles_isolate_seed_and_lengths_but_reuse_same_profile(self):
        cache = self.root / "cache"
        cache.mkdir()
        model = self.root / "model"
        model.mkdir()
        (self.native / "config.py").write_text(
            "DATASET_PATH=" + repr(str(cache)) + "\nMODEL_PATH=" + repr(str(model)) + "\n",
            encoding="utf-8")
        # This is exactly the incomplete cache name checked by pinned native
        # normal generation.  It must never hide a new seed/distribution.
        legacy = cache / "GSM8K-in1024-num2-model.jsonl"
        legacy.write_text('{"seed":999,"length_min":999}\n', encoding="utf-8")
        code = ("import argparse,csv,json\nfrom pathlib import Path\n"
                "from config import DATASET_PATH,MODEL_PATH\n"
                "p=argparse.ArgumentParser()\n"
                "for name,default in [('input_len','1024'),('data_num','2'),('seed','1'),('length_min',None),('length_max',None)]:\n"
                " p.add_argument('--'+name,default=default)\n"
                "a,_=p.parse_known_args()\n"
                "source=Path(DATASET_PATH)/('GSM8K-in'+a.input_len+'-num'+a.data_num+'-'+Path(MODEL_PATH).name+'.jsonl')\n"
                "reused=source.exists()\n"
                "if not reused:\n"
                " source.parent.mkdir(parents=True,exist_ok=True)\n"
                " source.write_text(json.dumps(vars(a))+'\\n')\n"
                "observation={'source':str(source),'reused':reused,'content':json.loads(source.read_text().splitlines()[0])}\n"
                "Path('cache_observation.json').write_text(json.dumps(observation))\n"
                + "row=" + repr({**self.row, "total_req": "2"}) + "\n"
                "with open('aisbench_result.csv','w',newline='') as h:\n"
                " w=csv.DictWriter(h,fieldnames=list(row));w.writeheader();w.writerow(row)\n"
                "print('Success Requests: 2\\nFailed Requests: 0')\n")
        base = ["--input_len", "1024", "--data_num", "2", "--seed", "1"]
        first = self.run_native(code, base)
        self.assertEqual(first["state"], "PASS", first["reason"])
        profile = first["dataset_profile"]
        observation = json.loads((self.root / "trial_1/native_runtime/cache_observation.json").read_text())
        self.assertFalse(observation["reused"])
        self.assertEqual(observation["content"]["seed"], "1")
        cached = Path(observation["source"])
        stamp = cached.stat().st_mtime_ns
        second = self.run_native(code, base + ["--request_rate", "3", "--concurrency", "8", "--repeat", "2"])
        self.assertEqual(second["dataset_profile"]["fingerprint"], profile["fingerprint"])
        observation2 = json.loads((self.root / "trial_2/native_runtime/cache_observation.json").read_text())
        self.assertTrue(observation2["reused"])
        self.assertEqual(cached.stat().st_mtime_ns, stamp)
        changed_seed = self.run_native(code, [*base[:-1], "2"])
        changed_range = self.run_native(code, base + ["--length_min", "10", "--length_max", "20"])
        self.assertNotEqual(changed_seed["dataset_profile"]["fingerprint"], profile["fingerprint"])
        self.assertNotEqual(changed_range["dataset_profile"]["fingerprint"], profile["fingerprint"])
        range_observation = json.loads((self.root / "trial_4/native_runtime/cache_observation.json").read_text())
        self.assertFalse(range_observation["reused"])
        self.assertEqual(range_observation["content"]["length_min"], "10")
        self.assertEqual(legacy.read_text(), '{"seed":999,"length_min":999}\n')
        for trial in ("trial_1", "trial_2", "trial_3", "trial_4"):
            private_config = (self.root / trial / "native_runtime/config.py").read_text()
            self.assertIn("Runtime-only input-cache namespace", private_config)
        # The native entry contains length sampling.  A new upstream sampler
        # must never reuse the old dataset even when CLI arguments match.
        changed_native = self.run_native(code + "\n# upstream sampler revision\n", base)
        self.assertEqual(changed_native["state"], "PASS", changed_native["reason"])
        self.assertNotEqual(changed_native["dataset_profile"]["fingerprint"], profile["fingerprint"])
        observation5 = json.loads((self.root / "trial_5/native_runtime/cache_observation.json").read_text())
        self.assertFalse(observation5["reused"])

    def test_external_dataset_bypasses_generated_cache_namespace(self):
        cache = self.root / "unused_cache"
        config_text = "DATASET_PATH=" + repr(str(cache)) + "\nMODEL_PATH='model'\n"
        (self.native / "config.py").write_text(config_text, encoding="utf-8")
        dataset = self.root / "external.jsonl"
        dataset.write_text('{}\n', encoding="utf-8")
        report = self.run_native(self.direct("Success Requests: 1\nFailed Requests: 0"),
                                 ["--dataset", str(dataset)])
        self.assertEqual(report["dataset_profile"]["mode"], "external_dataset")
        self.assertFalse(report["dataset_profile"]["cache_applied"])
        self.assertFalse(cache.exists())
        self.assertEqual((self.root / "trial_1/native_runtime/config.py").read_text(), config_text)

    def test_default_accuracy_uses_original_golds_and_records_provenance(self):
        records = [
            {"question": "not gold", "answer": "synthetic answer"},
            {"question": "How much is 1+2?", "answer": "Original reasoning.\n#### 3"},
            {"question": "How much is 2+3?", "answer": "Original reasoning.\n#### 5"}]
        source = self.native / "GSM8K.jsonl"
        original = "".join(json.dumps(record) + "\n" for record in records).encode()
        source.write_bytes(original)
        report = self.run_native(self.direct("Success Requests: 1\nFailed Requests: 0"),
                                 ["--test_accuracy", "--data_num", "2", "--input_len", "65536"])
        provenance = report["accuracy_dataset"]
        selected = Path(provenance["dataset_file"])
        self.assertEqual([json.loads(line) for line in selected.read_text().splitlines()], records[1:])
        self.assertEqual(provenance["source_line_numbers"], [2, 3])
        self.assertEqual(provenance["sample_count"], 2)
        self.assertFalse(provenance["synthetic_labels"])
        self.assertEqual(provenance["input_len_policy"], "original_questions_without_synthetic_padding")
        self.assertEqual(source.read_bytes(), original)
        import hashlib
        self.assertEqual(provenance["source_sha256"], hashlib.sha256(original).hexdigest())

    def test_invalid_explicit_accuracy_dataset_fails_before_native_requests(self):
        source = self.root / "invalid_accuracy.jsonl"
        source.write_text(json.dumps({"question": "1+2?", "answer": "3"}) + "\n", encoding="utf-8")
        marker = self.root / "request_started"
        code = "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('started')\n"
        report = self.run_native(code, ["--test_accuracy", "--dataset", str(source), "--data_num", "1"])
        self.assertEqual(report["state"], "ERROR")
        self.assertIn("accuracy dataset line 1", report["reason"])
        self.assertIn("#### ", report["reason"])
        self.assertFalse(marker.exists())
        self.assertFalse(report["accuracy_dataset"]["requests_started"])

    def test_explicit_accuracy_dataset_keeps_all_valid_rows(self):
        source = self.root / "custom_accuracy.jsonl"
        records = [dict(question="Question " + str(i), answer="Reasoning.\n#### " + str(i)) for i in range(3)]
        source.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        report = self.run_native(self.direct("Success Requests: 1\nFailed Requests: 0"),
                                 ["--test_accuracy", "--dataset", str(source), "--data_num", "1"])
        provenance = report["accuracy_dataset"]
        self.assertEqual(provenance["mode"], "explicit_dataset")
        self.assertEqual(provenance["sample_count"], 3)
        self.assertFalse(provenance["data_num_applied"])
        self.assertEqual(Path(provenance["dataset_file"]), source)

    def test_runner_failure_with_outer_zero_keeps_out_exception_evidence(self):
        self.instrument_fixture()
        output = self.root / "failed_task"
        (self.native / "config.py").write_text("OUTPUT_DIR=" + repr(str(output)) + "\n", encoding="utf-8")
        with (self.native / "emit.py").open("a", encoding="utf-8") as handle:
            handle.write("from pathlib import Path\n"
                         + "exp=Path(" + repr(str(output / "experiment")) + ")\n"
                         + "(exp/'logs'/'eval').mkdir(parents=True)\n"
                         + "(exp/'logs'/'eval'/'gsm8k.out').write_text('Traceback (most recent call last):\\nIndexError: list index out of range\\n')\n"
                         + "print('Current exp folder: '+str(exp))\n"
                         + "print('ERROR [RUNNER-TASK-001] task failed with code 1')\n")
        code = ("import subprocess,sys\n"
                "subprocess.run('\\\"'+sys.executable+'\\\" emit.py ais_bench ok',shell=True,check=True)\n")
        report = self.run_native(code)
        self.assertEqual(report["return_code"], 0)
        self.assertEqual(report["state"], "ERROR")
        self.assertIn("IndexError", report["reason"])
        scope = report["main_scopes"][0]
        self.assertEqual(scope["runner_errors"][0]["return_code"], 1)
        self.assertTrue(scope["exception_evidence"][0]["file"].endswith("gsm8k.out"))
        self.assertTrue(Path(scope["exception_evidence"][0]["file"]).is_file())

    def generation_copy(self, content=None):
        original = (RELEASE_ROOT / 'aisbench_auto_tools_prefix/default_api.py').read_bytes()
        (self.native / 'default_api.py').write_bytes(original if content is None else content)
        self.number += 1
        evidence = self.root / ('template_' + str(self.number))
        runtime = evidence / 'native_runtime'
        runtime.mkdir(parents=True)
        shutil.copyfile(self.native / 'default_api.py', runtime / 'default_api.py')
        shutil.copyfile(RELEASE_ROOT / 'aisbench_auto_tools_prefix/aisbench_test.py', runtime / 'aisbench_test.py')
        return evidence, runtime

    def render_actual_native_generation(self, runtime, *, accuracy=False, thinking=False):
        # Compile the actual pinned native function, with only its environment
        # and final symlink stubbed. No copied generation-selection algorithm.
        text = (RELEASE_ROOT / 'aisbench_auto_tools_prefix/aisbench_test.py').read_text(encoding='utf-8')
        tree = ast.parse(text)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == 'modify_aisbench_api')
        module = ast.Module(body=[function])
        module.type_ignores = []  # Accepted by 3.7; required by newer CPython.
        import logging
        import re
        namespace = dict(re=re, logging=logging, os=os, MODEL_PATH='/model', MODEL_NAME='offline',
                         API_KEY='', HOST_IP='192.168.1.1', HOST_PORT='8000', URL='', WORK_PATH=str(runtime),
                         api_test_type='FakeStream', api_test_abbr='offline', request_rate='0',
                         test_accuracy=accuracy, enable_think=thinking, symlink_force=lambda *args: None)
        exec(compile(module, '<actual-native-modify_aisbench_api>', 'exec'), namespace)
        previous = Path.cwd()
        try:
            os.chdir(runtime)
            with contextlib.redirect_stdout(io.StringIO()):
                namespace['modify_aisbench_api']('4', '512')
        finally:
            os.chdir(previous)
        rendered = ast.parse((runtime / 'temp_api.py').read_text(encoding='utf-8'))
        call = next(node.value for node in ast.walk(rendered)
                    if isinstance(node, ast.keyword) and node.arg == 'generation_kwargs')
        return {keyword.arg: ast.literal_eval(keyword.value) for keyword in call.keywords}

    def test_exact_template_restores_native_normal_thinking_and_accuracy_parameters(self):
        before = (RELEASE_ROOT / 'aisbench_auto_tools_prefix/default_api.py').read_bytes()
        for accuracy, thinking in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(accuracy=accuracy, thinking=thinking):
                evidence, runtime = self.generation_copy()
                arguments = (['--test_accuracy'] if accuracy else []) + (['--enable_think'] if thinking else [])
                metadata = restore_generation_template(runtime, evidence, self.native, arguments)
                self.assertEqual(metadata['action'], 'restored_known_ac06_placeholder')
                self.assertEqual(metadata['original_sha256'], hashlib.sha256(before).hexdigest())
                self.assertNotEqual(metadata['patched_sha256'], metadata['original_sha256'])
                self.assertEqual(metadata['patched_sha256'], hashlib.sha256((runtime / 'default_api.py').read_bytes()).hexdigest())
                self.assertEqual((evidence / 'generation_template.original.py').read_bytes(), before)
                self.assertEqual((evidence / 'generation_template.patched.py').read_bytes(), (runtime / 'default_api.py').read_bytes())
                expected = dict(temperature=0.6, top_p=0.95) if accuracy else dict(temperature=0, ignore_eos=True)
                if thinking:
                    expected['chat_template_kwargs'] = {'enable_thinking': True}
                self.assertEqual(self.render_actual_native_generation(runtime, accuracy=accuracy, thinking=thinking), expected)
                self.assertEqual((self.native / 'default_api.py').read_bytes(), before)
        self.assertEqual((RELEASE_ROOT / 'aisbench_auto_tools_prefix/default_api.py').read_bytes(), before)

    def test_legacy_placeholder_is_preserved_and_native_thinking_still_renders(self):
        original = (RELEASE_ROOT / 'aisbench_auto_tools_prefix/default_api.py').read_bytes()
        legacy = original.replace(b'temperature=0,\n            ignore_eos=True,', b'generation_kwargs_for_replace')
        evidence, runtime = self.generation_copy(legacy)
        metadata = restore_generation_template(runtime, evidence, self.native, ['--enable_think'])
        self.assertEqual(metadata['action'], 'legacy_placeholder_preserved')
        self.assertEqual(metadata['original_sha256'], metadata['patched_sha256'])
        self.assertEqual((runtime / 'default_api.py').read_bytes(), legacy)
        self.assertEqual(self.render_actual_native_generation(runtime, thinking=True),
                         dict(temperature=0, ignore_eos=True, chat_template_kwargs={'enable_thinking': True}))

    def test_unknown_multiple_and_ambiguous_generation_templates_fail_before_native_starts(self):
        original = (RELEASE_ROOT / 'aisbench_auto_tools_prefix/default_api.py').read_bytes()
        legacy = original.replace(b'temperature=0,\n            ignore_eos=True,', b'generation_kwargs_for_replace')
        samples = [original.replace(b'temperature=0', b'temperature=1'),
                   original + b'\nextra=dict(generation_kwargs=dict(temperature=0,ignore_eos=True))\n',
                   legacy + b'\n# generation_kwargs_for_replace\n',
                   original.replace(b'generation_kwargs=dict(', b'generation_kwargs=unknown(')]
        marker = self.root / 'native_started'
        for sample in samples:
            with self.subTest(template_sha256=hashlib.sha256(sample).hexdigest()):
                (self.native / 'default_api.py').write_bytes(sample)
                report = self.run_native('from pathlib import Path\nPath(' + repr(str(marker)) + ").write_text('started')\n")
                self.assertEqual(report['state'], 'ERROR')
                self.assertFalse(marker.exists())
                self.assertEqual(report['generation_template']['state'], 'invalid')
                self.assertFalse(report['generation_template']['requests_started'])
                self.assertEqual((self.root / ('trial_' + str(self.number)) / 'native_runtime/default_api.py').read_bytes(), sample)

    def test_generation_compatibility_rejects_canonical_source_as_patch_target(self):
        original = (RELEASE_ROOT / 'aisbench_auto_tools_prefix/default_api.py').read_bytes()
        (self.native / 'default_api.py').write_bytes(original)
        with self.assertRaisesRegex(ValueError, 'isolated per-trial'):
            restore_generation_template(self.native, self.root / 'bad_trial', self.native, [])
        self.assertEqual((self.native / 'default_api.py').read_bytes(), original)

    def test_generation_provenance_is_bound_to_real_child_report_and_scope(self):
        self.instrument_fixture()
        original = (RELEASE_ROOT / 'aisbench_auto_tools_prefix/default_api.py').read_bytes()
        (self.native / 'default_api.py').write_bytes(original)
        code = ("import subprocess,sys\n"
                "subprocess.run('\\\"'+sys.executable+'\\\" emit.py ais_bench ok',shell=True,check=True)\n")
        report = self.run_native(code, ['--enable_think'])
        self.assertEqual(report['state'], 'PASS', report['reason'])
        metadata = report['generation_template']
        self.assertEqual(metadata['action'], 'restored_known_ac06_placeholder')
        self.assertTrue(metadata['requested']['enable_think'])
        scope = report['main_scopes'][0]
        for field in ('state', 'action', 'original_sha256', 'patched_sha256', 'evidence_file'):
            self.assertEqual(scope['generation_template'][field], metadata[field])


if __name__ == "__main__":
    unittest.main()
