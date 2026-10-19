"""Publication contracts: preserve numeric strings and identify every task."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
import hashlib
from unittest.mock import patch
import sys
AUTOMATION_ROOT = Path(__file__).resolve().parent.parent
CORE_ROOT = AUTOMATION_ROOT / 'core'
TOOLS_ROOT = AUTOMATION_ROOT / 'tools'
RELEASE_ROOT = AUTOMATION_ROOT.parent
for folder in (CORE_ROOT, TOOLS_ROOT):
    sys.path.insert(0, str(folder))

import aisbench_batch_results as publisher

from aisbench_batch_results import (IDENTITY_COLUMNS, METRIC_COLUMNS, csv_bytes,
                                   prepare_task_outputs, publish_batch, save_effective_config)


class BatchResultContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='aisbench_result_contract_')
        self.addCleanup(self.temporary.cleanup)
        self.batch = Path(self.temporary.name) / 'batch'
        self.batch.mkdir()

    def job(self, task='a', model='model', states=('passed',), planned=0):
        label = task + '__' + model
        output = self.batch / 'tasks' / label
        result = output / 'scenario_results' / 'run'
        result.mkdir(parents=True)
        rows = []
        for index, state in enumerate(states):
            row = dict.fromkeys(METRIC_COLUMNS, '')
            for field in publisher.METRIC_COLUMNS[2:21]:
                row[field] = '1'
            row.update(scenario='quoted,scenario_' + str(index), status=state,
                       current_time='2026-10-19 12:00:00', input_len='1033.875',
                       output_len='512.0', total_req='16.0', max_cc='4', cc='3.963400',
                       rr='0', qps='0.309400000', qpm='18.564',
                       output_throughput='158.4277000', repeat_rate='0.90')
            row['TTFT avg'] = '2728.4000'
            rows.append(row)
        source = result / 'all_results.csv'
        source.write_bytes(csv_bytes(METRIC_COLUMNS, rows))
        (result / 'execution.log').write_text('execution evidence', encoding='utf-8')
        launcher = output / 'launcher-source.log'
        launcher.write_text('launcher evidence', encoding='utf-8')
        return dict(task=task, model=model, endpoint='http://192.168.1.10:8000',
                    service_id='', run_id='run', output_label=label, host_output_dir=str(output),
                    result_dir=str(result), launcher_log=str(launcher), state='success',
                    planned_tests=planned, exit_code=0), rows

    def summary(self):
        return json.loads((self.batch / 'batch_results.json').read_text(encoding='utf-8'))

    def test_four_tasks_preserve_all_23_source_strings_and_bytes(self):
        fixtures = [self.job(task, 'model-' + task) for task in 'abcd']
        statuses = [item[0] for item in fixtures]
        self.assertEqual(publish_batch(self.batch, 'batch', statuses, final=True), [])
        with (self.batch / 'merged_metrics.csv').open(encoding='utf-8', newline='') as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(reader.fieldnames, IDENTITY_COLUMNS + METRIC_COLUMNS)
            merged = list(reader)
        self.assertEqual(len(merged), 4)
        for actual, (job, rows) in zip(merged, fixtures):
            self.assertEqual({key: actual[key] for key in METRIC_COLUMNS}, rows[0])
            self.assertEqual(actual['task'], job['task'])
            self.assertEqual(actual['model'], job['model'])
            self.assertEqual(actual['source_csv'], str(Path(job['result_dir']) / 'all_results.csv'))
            named = Path(job['host_output_dir']) / (job['output_label'] + '__all_results.csv')
            self.assertEqual(named.read_bytes(), Path(actual['source_csv']).read_bytes())
            self.assertEqual((named.parent / (job['output_label'] + '__launcher.log')).read_text(), 'launcher evidence')
        self.assertEqual(len(self.summary()['tasks']), 4)

    def test_peak_rounds_kept_but_not_counted_as_final_tests(self):
        job, rows = self.job(states=('peak_round', 'peak_round', 'peak', 'passed', 'failed', 'no_result'))
        job.update(state='failed', exit_code=1)
        self.assertFalse(publish_batch(self.batch, 'batch', [job], final=True))
        state = self.summary()['tasks'][0]
        self.assertEqual((state['completed_tests'], state['passed_tests'], state['peak_tests'],
                          state['failed_tests'], state['peak_rounds']), (4, 1, 1, 2, 2))
        merged = list(csv.DictReader(io.StringIO((self.batch / 'merged_metrics.csv').read_text())))
        self.assertEqual([row['status'] for row in merged], [row['status'] for row in rows])

    def test_success_missing_or_incomplete_metrics_is_an_artifact_failure(self):
        for corrupt in ('missing', 'schema', 'partial', 'empty', 'count', 'status'):
            with self.subTest(corrupt=corrupt):
                job, _ = self.job(corrupt, planned=2 if corrupt == 'count' else 0)
                source = Path(job['result_dir']) / 'all_results.csv'
                if corrupt == 'missing':
                    source.unlink()
                elif corrupt == 'schema':
                    source.write_text('scenario,status\nx,passed\n')
                elif corrupt == 'partial':
                    source.write_text(','.join(METRIC_COLUMNS) + '\nx,passed\n')
                elif corrupt == 'empty':
                    source.write_text(','.join(METRIC_COLUMNS) + '\n')
                elif corrupt == 'status':
                    source.write_text(source.read_text().replace('passed', 'invented'))
                self.assertTrue(publish_batch(self.batch, 'batch', [job], final=True))
                self.assertEqual(self.summary()['tasks'][0]['results_state'], 'invalid')

    def test_running_partial_csv_retries_then_publishes_finished_rows(self):
        job, rows = self.job()
        job['state'] = 'running'
        source = Path(job['result_dir']) / 'all_results.csv'
        content = source.read_bytes()
        source.write_text(','.join(METRIC_COLUMNS) + '\nx,passed\n')
        self.assertFalse(publish_batch(self.batch, 'batch', [job]))
        self.assertEqual(self.summary()['tasks'][0]['results_state'], 'pending')
        source.write_bytes(content)
        job['state'] = 'success'
        self.assertFalse(publish_batch(self.batch, 'batch', [job], final=True))
        self.assertEqual(self.summary()['tasks'][0]['results_state'], 'ready')

    def test_result_outside_own_output_is_not_read_or_published(self):
        job, _ = self.job()
        job['result_dir'] = str(self.batch)
        self.assertTrue(publish_batch(self.batch, 'batch', [job], final=True))
        self.assertEqual(self.summary()['tasks'][0]['results_state'], 'invalid')

    def test_failed_worker_without_csv_stays_failed_without_fabricated_metrics(self):
        job, _ = self.job()
        (Path(job['result_dir']) / 'all_results.csv').unlink()
        job.update(state='failed', exit_code=7)
        self.assertFalse(publish_batch(self.batch, 'batch', [job], final=True))
        state = self.summary()['tasks'][0]
        self.assertEqual((state['state'], state['exit_code'], state['results_state']), ('failed', 7, 'missing'))
        self.assertEqual(state['completed_tests'], 0)

    def test_config_snapshot_excludes_credentials_and_expanded_secret_arguments(self):
        job, _ = self.job()
        task = Path(job['host_output_dir'])
        metadata = dict(model_name='model', service_endpoint='http://192.168.1.10:8000',
                        effective_config={'DATA_NUM_MULTIPLIER': '4',
                                          'scenarios': [{'name': 'small', 'api_key': 'nested-secret'}],
                                          'expanded_fixed': ['--api_key expanded-secret'], 'expanded_peak': []})
        environment = {'API_KEY': 'secret-value', 'API_URL': 'http://user:password@192.168.1.10/?token=secret',
                       'ARBITRARY_SECRET': 'another-secret', 'MODEL_NAME': 'model'}
        save_effective_config(task, metadata, environment, 'abc123')
        content = (task / 'config.redacted.json').read_text(encoding='utf-8')
        for forbidden in ('nested-secret', 'expanded-secret', 'secret-value', 'password', 'another-secret'):
            self.assertNotIn(forbidden, content)
        snapshot = json.loads(content)
        self.assertEqual(snapshot['DATA_NUM_MULTIPLIER'], '4')
        self.assertEqual(snapshot['endpoint'], 'http://192.168.1.10:8000')
        self.assertEqual(snapshot['expanded_fixed_count'], 1)

    def test_new_run_withdraws_old_named_aliases_and_preserves_history(self):
        job, _ = self.job()
        self.assertFalse(publish_batch(self.batch, 'batch', [job], final=True))
        output = Path(job['host_output_dir'])
        label = job['output_label']
        history = (Path(job['result_dir']) / 'all_results.csv').read_bytes()
        prepare_task_outputs(output, label)
        for suffix in ('__all_results.csv', '__execution.log', '__launcher.log'):
            self.assertFalse((output / (label + suffix)).exists())
        self.assertEqual((Path(job['result_dir']) / 'all_results.csv').read_bytes(), history)

    def test_terminal_corruption_withdraws_previously_published_numeric_alias(self):
        job, _ = self.job()
        self.assertFalse(publish_batch(self.batch, 'batch', [job], final=True))
        named = Path(job['host_output_dir']) / (job['output_label'] + '__all_results.csv')
        self.assertTrue(named.exists())
        (Path(job['result_dir']) / 'all_results.csv').write_text('invalid')
        self.assertTrue(publish_batch(self.batch, 'batch', [job], final=True))
        self.assertFalse(named.exists())

    def test_successful_worker_with_failed_rows_is_rejected(self):
        job, _ = self.job(states=('failed',))
        self.assertTrue(publish_batch(self.batch, 'batch', [job], final=True))
        self.assertEqual(self.summary()['tasks'][0]['results_state'], 'invalid')

    def test_duplicate_rows_cannot_replace_a_missing_planned_identity(self):
        job, _ = self.job(states=('passed', 'passed'), planned=2)
        job['planned_scenarios'] = ['quoted,scenario_0', 'required-other-scenario']
        self.assertTrue(publish_batch(self.batch, 'batch', [job], final=True))

    def test_nan_and_fractional_request_counts_are_rejected(self):
        for field, value in (('qps', 'nan'), ('total_req', '1.5')):
            job, rows = self.job(task=field)
            rows[0][field] = value
            source = Path(job['result_dir']) / 'all_results.csv'
            source.write_bytes(csv_bytes(METRIC_COLUMNS, rows))
            self.assertTrue(publish_batch(self.batch, 'batch', [job], final=True))

    def test_committed_generation_is_self_consistent_and_retained(self):
        job, _ = self.job()
        self.assertFalse(publish_batch(self.batch, 'batch', [job], final=True))
        first = self.summary()
        generation = Path(first['generation_dir'])
        for name, expected in first['generation_hashes'].items():
            self.assertEqual(hashlib.sha256((generation / name).read_bytes()).hexdigest(), expected)
        self.assertFalse(publish_batch(self.batch, 'batch', [job], final=True))
        self.assertEqual(first['generation'], self.summary()['generation'])
        job['phase'] = 'complete'
        self.assertFalse(publish_batch(self.batch, 'batch', [job], final=True))
        self.assertNotEqual(first['generation'], self.summary()['generation'])
        self.assertTrue((generation / 'snapshot.json').is_file())

    def test_display_clocks_do_not_create_generations_but_progress_and_source_bytes_do(self):
        job, rows = self.job()
        job.update(state='running', phase='fixed', elapsed_seconds=1, updated_at='first', test_index=1)
        self.assertFalse(publish_batch(self.batch, 'batch', [job]))
        first = self.summary()['generation']
        job.update(elapsed_seconds=2, updated_at='second')
        self.assertFalse(publish_batch(self.batch, 'batch', [job]))
        self.assertEqual(first, self.summary()['generation'])
        job['test_index'] = 2
        self.assertFalse(publish_batch(self.batch, 'batch', [job]))
        second = self.summary()['generation']
        self.assertNotEqual(first, second)
        source = Path(job['result_dir']) / 'all_results.csv'
        rows[0]['qps'] = '0.309400001'
        source.write_bytes(csv_bytes(METRIC_COLUMNS, rows))
        self.assertFalse(publish_batch(self.batch, 'batch', [job]))
        self.assertNotEqual(second, self.summary()['generation'])
        self.assertEqual(len(list((self.batch / '.generations').iterdir())), 3)

    def test_final_source_validation_is_repeated_even_when_generation_is_reused(self):
        job, _ = self.job()
        with patch.object(publisher, 'metric_rows', wraps=publisher.metric_rows) as reader:
            self.assertFalse(publish_batch(self.batch, 'batch', [job], final=True))
            first = self.summary()['generation']
            self.assertFalse(publish_batch(self.batch, 'batch', [job], final=True))
            self.assertEqual(reader.call_count, 2)
            self.assertEqual(first, self.summary()['generation'])
        # An explicitly advertised but missing scope index must still be audited
        # on every final call, even when the previous invalid snapshot is reused.
        job['result_scopes'] = str(Path(job['result_dir']) / 'result_scopes.json')
        import aisbench_result_validation as integrity
        with patch.object(integrity, 'validate_results', wraps=integrity.validate_results) as validator:
            self.assertTrue(publish_batch(self.batch, 'batch', [job], final=True))
            self.assertTrue(publish_batch(self.batch, 'batch', [job], final=True))
            self.assertEqual(validator.call_count, 2)

    def test_polling_reuses_validation_only_until_source_progress_or_final_changes(self):
        job, rows = self.job()
        job.update(state='running', phase='fixed', elapsed_seconds=1)
        with patch.object(publisher, 'metric_rows', wraps=publisher.metric_rows) as reader:
            self.assertFalse(publish_batch(self.batch, 'batch', [job]))
            job['elapsed_seconds'] = 2
            self.assertFalse(publish_batch(self.batch, 'batch', [job]))
            self.assertEqual(reader.call_count, 1)
            job['test_index'] = 1
            self.assertFalse(publish_batch(self.batch, 'batch', [job]))
            self.assertEqual(reader.call_count, 2)
            rows[0]['qps'] = '0.309400001'
            (Path(job['result_dir']) / 'all_results.csv').write_bytes(csv_bytes(METRIC_COLUMNS, rows))
            self.assertFalse(publish_batch(self.batch, 'batch', [job]))
            self.assertEqual(reader.call_count, 3)
            self.assertFalse(publish_batch(self.batch, 'batch', [job], final=True))
            self.assertEqual(reader.call_count, 4)

    def test_final_commit_is_distinct_from_running_snapshot_and_repairs_missing_alias(self):
        job, _ = self.job()
        self.assertFalse(publish_batch(self.batch, 'batch', [job]))
        first = self.summary()['generation']
        self.assertFalse(publish_batch(self.batch, 'batch', [job], final=True))
        second = self.summary()['generation']
        self.assertNotEqual(first, second)
        (self.batch / 'merged_metrics.csv').unlink()
        self.assertFalse(publish_batch(self.batch, 'batch', [job], final=True))
        self.assertNotEqual(second, self.summary()['generation'])
        self.assertTrue((self.batch / 'merged_metrics.csv').is_file())

    def test_terminal_artifact_changes_invalidate_poll_cache_and_withdraw_numeric_alias(self):
        from aisbench_artifact_archive import write_archive_manifest
        from aisbench_result_adapter import CANONICAL_COLUMNS
        for change in ('modify', 'add', 'delete'):
            with self.subTest(change=change):
                job, rows = self.job(task=change)
                result = Path(job['result_dir'])
                evidence = result / 'fixed_001'
                evidence.mkdir()
                raw = {name: rows[0][name] for name in CANONICAL_COLUMNS}
                raw['repeat_rate'] = rows[0]['repeat_rate']
                source = evidence / 'aisbench_result.csv'
                source.write_bytes(csv_bytes(list(raw), [raw]))
                detail = evidence / 'requests.db'
                detail.write_bytes(b'original')
                scope = dict(scope_id='scope_001', phase='main', state='PASS',
                             source_csv=str(source), raw_row=raw,
                             counts=dict(total=16, success=16, failed=0))
                evaluation = evidence / 'evaluation.json'
                evaluation.write_text(json.dumps({'main_scopes': [scope]}), encoding='utf-8')
                index = result / 'result_scopes.json'
                index.write_text(json.dumps({'schema_version': 1, 'rows': [dict(
                    row_index=0, scenario=rows[0]['scenario'], current_time=rows[0]['current_time'],
                    status='passed', evaluation_file=str(evaluation), selected_scope_id='scope_001', scope=scope)]}), encoding='utf-8')
                write_archive_manifest(evidence, task_kind='performance', archive_name=rows[0]['scenario'])
                job['result_scopes'] = str(index)
                self.assertFalse(publish_batch(self.batch, 'batch', [job]))
                alias = Path(job['host_output_dir']) / (job['output_label'] + '__all_results.csv')
                self.assertTrue(alias.exists())
                if change == 'modify':
                    detail.write_bytes(b'modified')
                elif change == 'add':
                    (evidence / 'unlisted.npy').write_bytes(b'new')
                else:
                    detail.unlink()
                self.assertTrue(publish_batch(self.batch, 'batch', [job]))
                self.assertFalse(alias.exists())

    def test_failed_publication_withdraws_aliases_without_removing_history(self):
        job, _ = self.job()
        self.assertFalse(publish_batch(self.batch, 'batch', [job], final=True))
        old = Path(self.summary()['generation_dir'])
        job['phase'] = 'complete'
        actual = publisher.atomic_bytes
        def fail_alias(path, content):
            if path == self.batch / 'merged_metrics.csv':
                raise OSError('injected publication failure')
            return actual(path, content)
        with patch.object(publisher, 'atomic_bytes', side_effect=fail_alias), self.assertRaises(OSError):
            publish_batch(self.batch, 'batch', [job], final=True)
        self.assertFalse((self.batch / 'merged_metrics.csv').exists())
        self.assertFalse((Path(job['host_output_dir']) / (job['output_label'] + '__all_results.csv')).exists())
        self.assertEqual(self.summary()['publication_state'], 'failed')
        self.assertTrue((old / 'merged_metrics.csv').is_file())


if __name__ == '__main__':
    unittest.main()
