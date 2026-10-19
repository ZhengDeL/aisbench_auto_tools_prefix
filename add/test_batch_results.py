"""Publication contracts: preserve numeric strings and identify every task."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

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


if __name__ == '__main__':
    unittest.main()
