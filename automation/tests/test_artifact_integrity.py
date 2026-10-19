"""Scope, archival handoff and exact numeric provenance contracts."""
from __future__ import annotations
import csv
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
AUTOMATION_ROOT = Path(__file__).resolve().parent.parent
CORE_ROOT = AUTOMATION_ROOT / 'core'
TOOLS_ROOT = AUTOMATION_ROOT / 'tools'
RELEASE_ROOT = AUTOMATION_ROOT.parent
for folder in (CORE_ROOT, TOOLS_ROOT):
    sys.path.insert(0, str(folder))

import aisbench_artifact_archive as archive
from aisbench_result_adapter import (CANONICAL_COLUMNS, SUMMARY_COLUMNS, normalize_result,
                                    read_accuracy_scope)
from aisbench_result_validation import validate_results, verify_archive_manifest


class ArtifactIntegrityContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='aisbench_scope_contract_')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / 'outputs'
        self.result = self.output / 'scenario_results' / 'run'
        self.result.mkdir(parents=True)
        (self.result / 'execution.log').write_text('scope-bound test\n', encoding='utf-8')

    def fixture(self, *, status='passed', accuracy=False):
        raw = dict.fromkeys(CANONICAL_COLUMNS, '1')
        raw.update(current_time='2026-10-20 00:00:01', input_len='1024', output_len='512',
                   total_req='16', max_cc='4', cc='3.8', rr='0', qps='2', qpm='120', repeat_rate='0.9')
        source = self.output / 'trial' / 'scope_001' / 'aisbench_result.csv'
        source.parent.mkdir(parents=True, exist_ok=True)
        with source.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(raw))
            writer.writeheader(); writer.writerow(raw)
        normalized = normalize_result(raw, test_accuracy=accuracy)
        row = {'scenario': 'case_c4', **{name: normalized[name] for name in CANONICAL_COLUMNS},
               'repeat_rate': '0.9', 'status': status}
        scope = dict(scope_id='scope_001', phase='main', state='PASS', source_csv=str(source),
                     raw_row=raw, test_type='stream', test_accuracy=accuracy,
                     counts=dict(planned=16, total=16, success=16, failed=0, cancelled=None, attempts=None),
                     planned_verified=True)
        entry = dict(row_index=0, scenario=row['scenario'], current_time=row['current_time'],
                     status=status, selected_scope_id='scope_001', scope=scope)
        self.write([row], [entry])
        return row, entry

    def write(self, rows, entries):
        with (self.result / 'all_results.csv').open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=SUMMARY_COLUMNS)
            writer.writeheader(); writer.writerows(rows)
        (self.result / 'result_scopes.json').write_text(json.dumps({'schema_version': 1, 'rows': entries}), encoding='utf-8')

    def failures(self):
        return [name + ': ' + detail for name, ok, detail in validate_results(self.result, self.output, True) if not ok]

    def test_every_summary_field_is_checked_against_its_scope(self):
        row, entry = self.fixture()
        self.assertEqual(self.failures(), [])
        for field in CANONICAL_COLUMNS:
            changed = dict(row)
            changed[field] = 'wrong-time' if field == 'current_time' else '987'
            self.write([changed], [entry])
            self.assertTrue(self.failures(), field)
        changed = {**row, 'repeat_rate': '0.7'}
        self.write([changed], [entry])
        self.assertTrue(self.failures())

    def test_unknown_status_and_missing_scope_never_pass(self):
        row, entry = self.fixture()
        self.write([{**row, 'status': 'invented'}], [{**entry, 'status': 'invented'}])
        self.assertTrue(self.failures())
        self.write([row], [])
        self.assertTrue(self.failures())

    def test_consolidated_validation_cli_preserves_expected_status_order_contract(self):
        self.fixture()
        command = [sys.executable, '-B', str(CORE_ROOT / 'aisbench_result_validation.py'),
                   '--result-dir', str(self.result), '--host-output-dir', str(self.output), '--require-scopes']
        for expected, code in (('passed', 0), ('passed,passed', 1), ('failed', 1)):
            with self.subTest(expected=expected):
                process = subprocess.run(command + ['--expected-statuses', expected],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                self.assertEqual(process.returncode, code, process.stdout + process.stderr)
                self.assertIn('csv expected statuses', process.stdout)
    def test_each_peak_round_is_checked_even_if_final_peak_is_valid(self):
        row, entry = self.fixture()
        probe = {**row, 'status': 'peak_round', 'qps': '999'}
        probe_entry = {**entry, 'row_index': 1, 'status': 'peak_round'}
        self.write([row, probe], [entry, probe_entry])
        self.assertTrue(any('row provenance: 1' in item for item in self.failures()))

    def test_scope_counter_mismatch_and_missing_success_are_rejected(self):
        row, entry = self.fixture()
        for counts in (dict(total=16, success=15, failed=0), dict(total=16, success=None, failed=0)):
            entry['scope']['counts'] = counts
            self.write([row], [entry])
            self.assertTrue(self.failures())

    def test_all_repeat_scopes_must_validate_not_only_selected_last_scope(self):
        row, entry = self.fixture()
        first = {**entry['scope'], 'scope_id': 'scope_000', 'state': 'INVALID'}
        evaluation = self.output / 'trial' / 'evaluation.json'
        evaluation.write_text(json.dumps({'main_scopes': [first, entry['scope']], 'selected_scope_id': 'scope_001'}), encoding='utf-8')
        entry.pop('scope')
        entry['evaluation_file'] = str(evaluation)
        self.write([row], [entry])
        self.assertTrue(self.failures())

    def test_inline_scope_must_match_selected_main_scope_in_evaluation(self):
        row, entry = self.fixture()
        evaluation = self.output / 'trial' / 'evaluation.json'
        evaluation.write_text(json.dumps({'main_scopes': [entry['scope']],
                                          'selected_scope_id': 'scope_001'}), encoding='utf-8')
        entry['evaluation_file'] = str(evaluation)
        self.write([row], [entry])
        self.assertEqual(self.failures(), [])
        entry['scope'] = {**entry['scope'], 'counts': dict(total=16, success=0, failed=16)}
        self.write([row], [entry])
        self.assertTrue(self.failures())
        entry['selected_scope_id'] = 'different_scope'
        self.write([row], [entry])
        self.assertTrue(self.failures())

    def test_duplicate_scope_identity_is_not_silently_collapsed(self):
        row, entry = self.fixture()
        evaluation = self.output / 'trial' / 'evaluation.json'
        evaluation.write_text(json.dumps({'main_scopes': [entry['scope'], entry['scope']]}), encoding='utf-8')
        entry['evaluation_file'] = str(evaluation)
        self.write([row], [entry])
        self.assertTrue(self.failures())

    def test_nearest_ancestor_manifest_covers_scope_and_rejects_extra_or_changed_files(self):
        row, entry = self.fixture()
        run = self.output / 'trial'
        evaluation = run / 'nested' / 'evaluation.json'
        evaluation.parent.mkdir()
        evaluation.write_text(json.dumps({'main_scopes': [entry['scope']]}), encoding='utf-8')
        entry['evaluation_file'] = str(evaluation)
        self.write([row], [entry])
        archive.write_archive_manifest(run, task_kind='peak-search', archive_name='fixture')
        self.assertEqual(self.failures(), [])
        extra = run / 'unindexed.npy'
        extra.write_bytes(b'new unindexed measurement')
        self.assertTrue(self.failures())
        extra.unlink()
        original = evaluation.read_bytes()
        evaluation.write_bytes(original + b' ')
        self.assertTrue(self.failures())

    def test_scope_source_outside_output_and_missing_original_are_rejected(self):
        row, entry = self.fixture()
        for source in (self.root / 'outside.csv', self.output / 'missing.csv'):
            entry['scope']['source_csv'] = str(source)
            self.write([row], [entry])
            self.assertTrue(self.failures())

    def test_two_archives_in_same_second_never_overwrite(self):
        with patch.object(archive, 'timestamp', return_value='same_second'):
            first = archive.create_archive_dir(self.output, 'performance', 'same', 'same')
            (first / 'evidence.txt').write_text('original', encoding='utf-8')
            second = archive.create_archive_dir(self.output, 'performance', 'same', 'same')
        self.assertNotEqual(first, second)
        self.assertEqual((first / 'evidence.txt').read_text(), 'original')

    def test_independent_archiver_rejects_native_destinations_before_creating_anything(self):
        native = self.root / 'native_readonly'
        native.mkdir()
        target = native / 'outputs' / 'new'
        with patch.object(archive, 'BASE_DIR', native):
            with self.assertRaises(ValueError):
                archive.create_archive_dir(target, 'performance', 'case')
            with self.assertRaises(ValueError):
                archive.write_archive_manifest(native, task_kind='performance', archive_name='case')
            with self.assertRaises(ValueError):
                archive.archive_performance_run(archive_name='case', output_dir=str(target), source_root=native)
        self.assertEqual(list(native.iterdir()), [])
    def test_scope_bound_capture_includes_old_mtime_binary_arrays_and_databases(self):
        experiment = self.output / 'native_experiment'
        experiment.mkdir()
        for name, content in (('details.json', b'{}'), ('requests.db', b'original-db'),
                              ('times.npy', b'original-array'), ('gsm8k.out', b'runner error evidence')):
            path = experiment / name
            path.write_bytes(content)
            os.utime(path, (1, 1))
        manifest = archive.archive_performance_run(archive_name='handoff', output_dir=str(self.output),
                                                  source_root=self.root / 'empty-source', since_ts=9999999999,
                                                  experiment_dirs=[experiment])
        directory = Path(manifest['archive_dir'])
        for name in ('details.json', 'requests.db', 'times.npy', 'gsm8k.out'):
            self.assertEqual((directory / 'captured_outputs' / 'native_experiment' / name).read_bytes(), (experiment / name).read_bytes())
        verify_archive_manifest(directory / 'archive_manifest.json')
        (directory / 'captured_outputs' / 'native_experiment' / 'requests.db').write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            verify_archive_manifest(directory / 'archive_manifest.json')

    def test_capture_rejects_external_file_symlinks_when_supported(self):
        outside = self.root / 'outside.json'
        outside.write_text('{}', encoding='utf-8')
        link = self.output / 'outside.json'
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest('creating symlinks requires an enabled Windows privilege')
        with self.assertRaises(ValueError):
            archive.snapshot_recent_output_files(self.output, self.root / 'capture')

    def test_accuracy_has_independent_metrics_and_no_fabricated_perf(self):
        row, entry = self.fixture(accuracy=True)
        results, predictions = self.output / 'trial' / 'results', self.output / 'trial' / 'predictions'
        results.mkdir(); predictions.mkdir()
        score, records = results / 'gsm8k.json', predictions / 'gsm8k.json'
        score.write_text(json.dumps({'gsm8k': {'accuracy': 50.0, 'correct': 8, 'total': 16}}), encoding='utf-8')
        records.write_text(json.dumps({str(i): {'prediction': str(i), 'reference': str(i)} for i in range(16)}), encoding='utf-8')
        entry['scope']['accuracy_results'] = [str(score), str(records)]
        self.write([row], [entry])
        self.assertEqual(self.failures(), [])
        evidence = read_accuracy_scope(entry['scope'], self.output)
        self.assertEqual((evidence['numerator'], evidence['denominator'], evidence['prediction_count']), (8, 16, 16))
        self.assertFalse(evidence['independently_scored'])
        self.assertEqual(row['TTFT avg'], '')
        self.assertEqual(row['qps'], '')
        score.write_text(json.dumps({'gsm8k': {'accuracy': 50.0}}), encoding='utf-8')
        evidence = read_accuracy_scope(entry['scope'], self.output)
        self.assertIsNone(evidence['numerator'], 'rounded percentage cannot recreate correct count')

    def test_exact_image_gsm8k_details_are_independently_rescored(self):
        _, entry = self.fixture(accuracy=True)
        directory = self.output / 'trial' / 'results'
        directory.mkdir()
        score = directory / 'gsm8k.json'
        payload = {'accuracy': 50.0, 'details': {
            '0': {'origin_prediction': 'Answer is 1,234', 'predictions': '1234', 'references': '1234', 'correct': True},
            '1': {'origin_prediction': 'Answer is 5', 'predictions': '5', 'references': '6', 'correct': False}}}
        score.write_text(json.dumps(payload), encoding='utf-8')
        entry['scope']['accuracy_results'] = [str(score)]
        evidence = read_accuracy_scope(entry['scope'], self.output)
        self.assertTrue(evidence['independently_scored'])
        self.assertEqual((evidence['numerator'], evidence['denominator']), (1, 2))
        for change in ('score', 'verdict', 'extraction'):
            malformed = json.loads(json.dumps(payload))
            if change == 'score':
                malformed['accuracy'] = 100
            elif change == 'verdict':
                malformed['details']['1']['correct'] = True
            else:
                malformed['details']['0']['predictions'] = '234'
            score.write_text(json.dumps(malformed), encoding='utf-8')
            with self.subTest(change=change), self.assertRaises(ValueError):
                read_accuracy_scope(entry['scope'], self.output)

    def test_exact_image_single_replica_lists_keep_wrong_answers_valid(self):
        row, entry = self.fixture(accuracy=True)
        directory = self.output / 'trial' / 'results'
        directory.mkdir()
        score = directory / 'gsm8k.json'
        payload = {'accuracy': 6.25, 'type': 'GEN', 'details': {
            str(index): {'origin_prediction': 'Answer is 2', 'predictions': ['2'],
                         'references': ['2' if index == 4 else '18'], 'correct': [index == 4]}
            for index in range(16)}}
        score.write_text(json.dumps(payload), encoding='utf-8')
        predictions = self.output / 'trial' / 'predictions'
        predictions.mkdir()
        records = predictions / 'gsm8k.jsonl'
        records.write_text(''.join(json.dumps({'id': index, 'prediction': 'Answer is 2'}) + '\n'
                                   for index in range(16)), encoding='utf-8')
        entry['scope']['accuracy_results'] = [str(score), str(records)]
        self.write([row], [entry])
        evidence = read_accuracy_scope(entry['scope'], self.output)
        self.assertEqual((evidence['numerator'], evidence['denominator'], evidence['prediction_count']), (1, 16, 16))
        self.assertEqual(evidence['metrics']['accuracy'], 6.25)
        self.assertTrue(evidence['independently_scored'])
        self.assertEqual(self.failures(), [], 'low model accuracy is valid scoring evidence')
        self.assertEqual(row['qps'], '', 'accuracy cannot invent performance measurements')

    def test_accuracy_replica_shape_and_per_sample_evidence_remain_strict(self):
        _, entry = self.fixture(accuracy=True)
        directory = self.output / 'trial' / 'results'
        directory.mkdir()
        score = directory / 'gsm8k.json'
        original = {'accuracy': 100.0, 'details': {'0': {
            'origin_prediction': 'Answer is 2', 'predictions': ['2'],
            'references': ['2'], 'correct': [True]}}}
        entry['scope']['accuracy_results'] = [str(score)]
        for change in ('empty', 'multiple', 'mixed', 'nonbool', 'missing', 'nested',
                       'score', 'verdict', 'extraction', 'malformed_sample'):
            payload = json.loads(json.dumps(original))
            sample = payload['details']['0']
            if change == 'empty':
                sample['correct'] = []
            elif change == 'multiple':
                for name in ('predictions', 'references', 'correct'):
                    sample[name] *= 2
            elif change == 'mixed':
                sample['correct'] = True
            elif change == 'nonbool':
                sample['correct'] = [1]
            elif change == 'missing':
                del sample['references']
            elif change == 'nested':
                sample['correct'] = [[True]]
            elif change == 'score':
                payload['accuracy'] = 0
            elif change == 'verdict':
                sample['correct'] = [False]
            elif change == 'extraction':
                sample['predictions'] = ['3']
            else:
                payload['details']['1'] = 'not a scored sample'
            score.write_text(json.dumps(payload), encoding='utf-8')
            with self.subTest(change=change), self.assertRaises(ValueError):
                read_accuracy_scope(entry['scope'], self.output)

    def test_scope_handoff_uses_explicit_origin_root_without_rewriting_evidence(self):
        _, _ = self.fixture()
        index = self.result / 'result_scopes.json'
        payload = json.loads(index.read_text(encoding='utf-8'))
        payload['output_root'] = str(self.output)
        index.write_text(json.dumps(payload), encoding='utf-8')
        relocated = self.root / 'handoff'
        shutil.copytree(self.output, relocated)
        before = (relocated / 'scenario_results' / 'run' / 'result_scopes.json').read_bytes()
        checks = validate_results(relocated / 'scenario_results' / 'run', relocated, True)
        self.assertFalse([detail for _, ok, detail in checks if not ok])
        self.assertEqual(before, (relocated / 'scenario_results' / 'run' / 'result_scopes.json').read_bytes())


class WorkerPeakPublicationContracts(unittest.TestCase):
    """Exercise production worker ingestion and sealing, without Docker/model."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='aisbench_live_peak_contract_')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        from aisbench_configuration import effective_environment, build_plan
        from aisbench_worker import Worker
        config = self.root / 'case.env'
        config.write_text("SCENARIOS_JSON=$(cat <<'JSON'\n" + json.dumps([dict(
            name='live', input_len=16, output_len=4, concurrency=['max'],
            peak_min_cc=1, peak_max_cc=2)]) + '\nJSON\n)\n', encoding='utf-8')
        self.values = effective_environment(dict(MODEL_NAME='offline', MODEL_HOST_PATH=str(self.root / 'model'),
            HOST_OUTPUT_DIR=str(self.root / 'output'), TOOLS_HOST_DIR=str(RELEASE_ROOT / 'aisbench_auto_tools_prefix')),
            RELEASE_ROOT)
        self.plan = build_plan(self.values, config)
        with patch.dict(os.environ, {}, clear=True):
            self.worker = Worker(self.values, self.plan)
        self.addCleanup(self.worker.human.close)
        self.addCleanup(self.worker.events.close)
        self.worker.index = 1
        self.scene = self.plan['peak'][0]
        self.session = self.worker.output / ('peak_fixture_' + self.worker.run_id + '_1')

    def write_trial(self, ordinal):
        raw = dict.fromkeys(CANONICAL_COLUMNS, '1')
        raw.update(current_time='2026-10-20 01:00:00', input_len='16', output_len='4', total_req='8',
                   max_cc='2', cc='1.9', rr='0', repeat_rate='0')
        folder = self.session / 'live_peak' / ('trial_' + str(ordinal))
        folder.mkdir(parents=True)
        source = folder / 'aisbench_result.csv'
        with source.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(raw)); writer.writeheader(); writer.writerow(raw)
        scope = dict(scope_id='scope_001', phase='main', state='PASS', test_type='stream',
            source_csv=self.worker.container_path(source), raw_row=raw,
            counts=dict(planned=8, total=8, success=8, failed=0, planned_verified=True))
        evaluation = folder / 'evaluation.json'
        evaluation.write_text(json.dumps(dict(state='PASS', main_scopes=[scope], scopes=[scope],
            selected_scope_id=scope['scope_id'], raw_row=raw)), encoding='utf-8')
        return dict(raw, case_name=self.scene['name'], trial_id='trial_' + str(ordinal),
            measurement_state='PASS', concurrency=2, evaluation_file=self.worker.container_path(evaluation),
            selected_scope_id=scope['scope_id'])

    def write_report(self, report, *, seal=False):
        self.session.mkdir(parents=True, exist_ok=True)
        target = self.session / 'peak_search_report.json'
        target.write_text(json.dumps(report), encoding='utf-8')
        if seal:
            archive.write_archive_manifest(self.session, task_kind='peak-search', archive_name='fixture',
                source_root=Path('/workspace/runtime/native'))

    def test_live_trials_are_published_once_and_host_evidence_is_resealed(self):
        observations = []
        def run_container(arguments, timeout, on_progress=None):
            first = self.write_trial(1)
            self.write_report(dict(best=[], all=[first], complete=False))
            on_progress(); observations.append(len(self.worker.rows))
            on_progress(); observations.append(len(self.worker.rows))
            second = self.write_trial(2)
            self.write_report(dict(best=[], all=[first, second], complete=False))
            on_progress(); observations.append(len(self.worker.rows))
            on_progress(); observations.append(len(self.worker.rows))
            best = dict(first, passed='yes', status='peak_capped', termination='capped', selected_trial_id=first['trial_id'])
            self.write_report(dict(best=[best], all=[first, second], complete=True), seal=True)
            return 0
        with patch.object(self.worker, 'run_container', side_effect=run_container):
            self.assertTrue(self.worker.peak(self.scene))
        self.assertEqual(observations, [1, 1, 2, 2])
        self.assertEqual([row['status'] for row in self.worker.rows], ['peak_round', 'peak_round', 'peak_capped'])
        self.assertEqual([entry.get('selected_trial_id') for entry in self.worker.scopes[-1:]], ['trial_1'])
        verify_archive_manifest(self.session / 'archive_manifest.json')
        checks = validate_results(self.worker.result, self.worker.output, True)
        self.assertFalse([detail for _, ok, detail in checks if not ok])

    def test_missing_report_has_error_state_and_never_uses_history(self):
        history = self.worker.output / 'peak_history_other_run'
        history.mkdir()
        (history / 'peak_search_report.json').write_text('{"best":[{"passed":"yes"}]}', encoding='utf-8')
        with patch.object(self.worker, 'run_container', return_value=1):
            self.assertFalse(self.worker.peak(self.scene))
        self.assertEqual([row['status'] for row in self.worker.rows], ['peak_error'])
        self.assertEqual(self.worker.scopes[0]['measurement_state'], 'ERROR')
        checks = validate_results(self.worker.result, self.worker.output, True)
        self.assertFalse([detail for name, ok, detail in checks if not ok and name != 'final states successful'])

    def test_fixed_failure_without_evaluation_keeps_unknown_measurements_and_error_state(self):
        from aisbench_configuration import build_plan
        config = self.root / 'fixed.env'
        config.write_text("SCENARIOS_JSON=$(cat <<'JSON'\n" + json.dumps([dict(
            name='missing_fixed', input_len=16, output_len=4, concurrency=[1])]) + '\nJSON\n)\n', encoding='utf-8')
        scene = build_plan(self.values, config)['fixed'][0]
        with patch.object(self.worker, 'run_container', return_value=1):
            self.assertFalse(self.worker.fixed(scene))
        self.assertEqual(self.worker.rows[0]['status'], 'failed')
        self.assertEqual(self.worker.scopes[0]['measurement_state'], 'ERROR')
        self.assertTrue(all(not self.worker.rows[0][name] for name in CANONICAL_COLUMNS))
        verify_archive_manifest(self.worker.result / 'fixed_1' / 'archive_manifest.json')

    def test_absent_selected_evaluation_never_reads_current_directory(self):
        def run_container(arguments, timeout, on_progress=None):
            best = dict(case_name=self.scene['name'], status='peak_budget', passed='no',
                        termination='budget_limit', evaluation_file='', selected_scope_id='', selected_trial_id='')
            self.write_report(dict(best=[best], all=[], complete=True), seal=True)
            return 1
        with patch.object(self.worker, 'run_container', side_effect=run_container), \
                patch.object(self.worker, 'evaluation', side_effect=AssertionError('must not resolve empty evidence')) as reader:
            self.assertFalse(self.worker.peak(self.scene))
            reader.assert_not_called()
        with self.assertRaises(ValueError):
            self.worker.evaluation(Path('evaluation.json'))
        self.assertEqual(self.worker.rows[0]['status'], 'peak_budget')

    def test_incomplete_or_unsealed_final_report_cannot_publish_a_success(self):
        for incomplete, sealed in ((True, True), (False, False)):
            with self.subTest(incomplete=incomplete, sealed=sealed):
                # Each subcase has its own task identity and output directory.
                self.worker.rows.clear(); self.worker.scopes.clear()
                self.worker.run_id += '_next'
                self.session = self.worker.output / ('peak_fixture_' + self.worker.run_id + '_1')
                def run_container(arguments, timeout, on_progress=None):
                    trial = self.write_trial(1)
                    best = dict(trial, passed='yes', status='peak_capped', termination='capped',
                                selected_trial_id=trial['trial_id'])
                    self.write_report(dict(best=[best], all=[trial], complete=not incomplete), seal=sealed)
                    return 0
                with patch.object(self.worker, 'run_container', side_effect=run_container):
                    self.assertFalse(self.worker.peak(self.scene))
                self.assertEqual(self.worker.rows[-1]['status'], 'peak_error')
                self.assertFalse(any(row['status'] in {'peak_confirmed', 'peak_capped'} for row in self.worker.rows))

    def test_live_trial_without_evaluation_file_stops_without_fabricating_measurements(self):
        def run_container(arguments, timeout, on_progress=None):
            trial = dict(trial_id='broken', measurement_state='ERROR', concurrency=2)
            best = dict(case_name=self.scene['name'], status='peak_error', passed='no',
                        termination='error', evaluation_file='', selected_scope_id='', selected_trial_id='')
            self.write_report(dict(best=[best], all=[trial], complete=False))
            on_progress(); on_progress()
            self.write_report(dict(best=[best], all=[trial], complete=True), seal=True)
            return 1
        with patch.object(self.worker, 'run_container', side_effect=run_container), \
                self.assertRaisesRegex(RuntimeError, 'evaluation_file'):
            self.worker.peak(self.scene)
        self.assertEqual(self.worker.rows, [])
        self.assertEqual(self.worker.scopes, [])


if __name__ == '__main__':
    unittest.main()
