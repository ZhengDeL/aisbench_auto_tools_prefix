"""Offline process-level contracts for the run manager and real shell preflight.

Run: python -B -m unittest discover -s automation/tests -p test_parallel_runs.py
The fixtures are created in a temporary directory. No real Docker daemon,
model, tokenizer, server, repository env.local.env or benchmark is accessed.
"""
from __future__ import annotations

import csv
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
AUTOMATION_ROOT = Path(__file__).resolve().parent.parent
CORE_ROOT = AUTOMATION_ROOT / 'core'
TOOLS_ROOT = AUTOMATION_ROOT / 'tools'
RELEASE_ROOT = AUTOMATION_ROOT.parent
for folder in (CORE_ROOT, TOOLS_ROOT):
    sys.path.insert(0, str(folder))
ADD_DIR = CORE_ROOT
MANAGER = ADD_DIR / "aisbench_run_manager.py"
SHELL = RELEASE_ROOT / "run_perf_scenarios.sh"

# One helper implements the synthetic worker and Docker CLI. The manager itself
# remains the production implementation: its file loading, environment handling,
# resource locks, process supervision and result serialization all run normally.
FIXTURE = r'''
import csv, hashlib, json, os, pathlib, re, signal, sys, tempfile, time
root = pathlib.Path(os.environ['AISBENCH_TEST_ROOT'])
events = root / 'events'
states = root / 'containers'
events.mkdir(exist_ok=True)
states.mkdir(exist_ok=True)

def event(kind, **fields):
    value = {'kind': kind, 'time': time.monotonic(), 'pid': os.getpid(), **fields}
    path = events / (str(os.getpid()) + '.jsonl')
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(value) + '\n')

def state_path(identity):
    direct = states / (identity + '.json')
    if direct.exists():
        return direct
    for path in states.glob('*.json'):
        if json.loads(path.read_text())['Name'].lstrip('/') == identity:
            return path
    return direct

if sys.argv[1] == 'docker':
    args = sys.argv[2:]
    event('docker', args=args)
    if args[0] == 'info':
        print('offline-daemon-' + root.name)
        sys.exit(0)
    if args[:2] == ['image', 'inspect']:
        if os.environ.get('AISBENCH_TEST_IMAGE_MISSING') == '1':
            print('Error: No such image', file=sys.stderr)
            sys.exit(1)
        print('sha256:' + hashlib.sha256(args[-1].encode()).hexdigest())
        sys.exit(0)
    if args[0] == 'inspect':
        path = state_path(args[-1])
        if not path.exists():
            print('Error: No such object: ' + args[-1], file=sys.stderr)
            sys.exit(1)
        payload = json.loads(path.read_text())
        print(json.dumps([payload]))
        sys.exit(0)
    if args[0] == 'stop':
        path = state_path(args[-1])
        if not path.exists():
            print('Error: No such container: ' + args[-1], file=sys.stderr)
            sys.exit(1)
        if os.environ.get('AISBENCH_TEST_STOP_FAIL') == '1':
            event('stop-refused', identity=args[-1])
            print('Synthetic Docker stop failed', file=sys.stderr)
            sys.exit(71)
        payload = json.loads(path.read_text())
        payload['State']['Running'] = False
        path.write_text(json.dumps(payload))
        print(args[-1])
        sys.exit(0)
    print('Unexpected fake Docker operation: ' + repr(args), file=sys.stderr)
    sys.exit(93)

env = os.environ
name = env.get('CONTAINER_NAME', 'ais_bench-example')
image = env.get('EXPECTED_IMAGE', 'offline-image')
output = pathlib.Path(env.get('HOST_OUTPUT_DIR', str(root / 'single-output'))).resolve()
model = env.get('MODEL_NAME', 'default-model')
config_path = pathlib.Path(sys.argv[2])
config_bytes = config_path.read_bytes()
config_hash = hashlib.sha256(config_bytes).hexdigest()
scenario_block = re.search(r"SCENARIOS_JSON=\$\(cat <<'JSON'\n(.*?)\nJSON\n\)", config_bytes.decode('utf-8-sig'), re.S)
scenario_names = [item['name'] for item in json.loads(scenario_block.group(1))]
report = {
    'container_name': name, 'expected_image': image,
    'host_output_dir': str(output), 'model_host_path': str(root / 'model'),
    'tools_host_dir': str(root / 'tools'), 'host_ip': env.get('HOST_IP', 'default-host'),
    'host_port': env.get('HOST_PORT', '8000'), 'model_name': model,
    'api_url': env.get('API_URL', ''), 'service_id': env.get('SERVICE_ID', ''),
    'planned_scenarios': [name + '_c1' for name in scenario_names],
    'planned_tests': len(scenario_names), 'fixed_count': len(scenario_names), 'peak_count': 0,
    'effective_config': {'scenarios': json.loads(scenario_block.group(1))},
}
if env.get('AISBENCH_PREFLIGHT_REPORT'):
    if model == 'invalid-config':
        print('invalid fixture configuration', file=sys.stderr)
        sys.exit(2)
    pathlib.Path(env['AISBENCH_PREFLIGHT_REPORT']).write_text(json.dumps(report))
    event('preflight', model=model, name=name, output=str(output),
          config_hash=config_hash, scenario_names=scenario_names)
    gate = env.get('AISBENCH_TEST_PREFLIGHT_GATE')
    while gate and not pathlib.Path(gate).exists():
        time.sleep(0.025)
    sys.exit(0)

tag = env['AISBENCH_RUN_TAG']
identity = hashlib.sha256((name + '|' + tag).encode()).hexdigest()
lock_base = pathlib.Path('/tmp') if os.name == 'posix' else pathlib.Path(tempfile.gettempdir())
lease_name = hashlib.sha256(('offline-daemon-' + root.name + '\0' + tag).encode()).hexdigest() + '.active.json'
lease_on_entry = json.loads((lock_base / 'aisbench-auto-tools-locks' / lease_name).read_text())
result = output / 'scenario_results' / tag
result.mkdir(parents=True, exist_ok=False)
container = {'Id': identity, 'Name': '/' + name,
             'Config': {'Image': image, 'Labels': {'io.aisbench-auto-tools-prefix.managed': 'true'}},
             'State': {'Running': True}}
(states / (identity + '.json')).write_text(json.dumps(container))
runtime = {'container_id': identity, 'container_name': name, 'image': image,
           'run_id': tag, 'result_dir': str(result), 'complete': False,
           'phase': 'benchmark', 'current_scenario': 'small_c1', 'test_index': 1}
if env.get('AISBENCH_TEST_HIDE_RUNTIME_ID') == '1':
    runtime['container_id'] = ''
runtime_path = pathlib.Path(env['AISBENCH_RUNTIME_REPORT'])
runtime_path.write_text(json.dumps(runtime))
print('worker stdout started', flush=True)
print('worker stderr started', file=sys.stderr, flush=True)
if env.get('AISBENCH_TEST_RAW_BYTES'):
    os.write(1, b'raw-utf8-\xff\x00\r\n')
    print('API_KEY=' + env.get('API_KEY', ''), flush=True)
event('start', model=model, name=name, output=str(output), tag=tag, identity=identity,
      lease_on_entry=lease_on_entry,
      host=env.get('HOST_IP'), api_key=env.get('API_KEY'),
      multiplier=env.get('DATA_NUM_MULTIPLIER'), cap=env.get('PEAK_HARD_CAP'),
       peak_options={key: env.get(key) for key in ('PEAK_PRECISION', 'PEAK_CONFIRM_RUNS', 'PEAK_CONFIRM_MIN_REQUESTS', 'PEAK_MAX_TRIALS', 'PEAK_TRIAL_TIMEOUT', 'PEAK_TOTAL_TIMEOUT', 'PEAK_INVALID_RETRIES', 'PEAK_FLOOR_CONCURRENCY')},
      exported_json=env.get('SCENARIOS_JSON'), pod_info=env.get('POD_INFO'),
      config_hash=config_hash, scenario_names=scenario_names)
duration = float(env.get('AISBENCH_TEST_DURATION', '0.4'))
end = time.monotonic() + duration
while time.monotonic() < end:
    if not json.loads((states / (identity + '.json')).read_text())['State']['Running']:
        event('stopped', model=model, identity=identity)
        sys.exit(143)
    time.sleep(0.025)
if env.get('AISBENCH_TEST_ABNORMAL') == '1':
    event('abnormal-exit', model=model, identity=identity)
    sys.exit(9)  # No completion report or CSV: container cleanup is required.
code = 7 if model == 'fail-after-start' else 0
header = 'scenario,current_time,input_len,output_len,total_req,max_cc,cc,rr,TTFT avg,TTFT P90,TPOT avg,TPOT SLO_P90,E2E_time,output_throughput,single_output_throughput,E2E_throughput,single_E2E_throughput,qps,qpm,input_token_throughput,prefill_token_throughput,repeat_rate,status'.split(',')
row = ['small_c1', '2026-10-19 12:34:56', '16.0000', '4.0', '4', '1', '0.9990', '0',
       '123.' + hashlib.sha256(model.encode()).hexdigest()[:6].translate(str.maketrans('abcdef', '123456')),
       '123.9000', '8.000', '8.1250', '0.2000', '80.0000', '40.0000', '400.0000', '200.0000',
       '20.0000', '1200.0000', '320.0000', '', '0.90', 'passed' if code == 0 else 'failed']
with (result / 'all_results.csv').open('w', encoding='utf-8', newline='') as handle:
    writer = csv.writer(handle)
    writer.writerow(header)
    writer.writerow(row)
(result / 'execution.log').write_text('fixture benchmark log\n')
runtime['complete'] = True
runtime['phase'] = 'completed'
runtime['worker_exit'] = code
runtime_path.write_text(json.dumps(runtime))
event('finish', model=model, tag=tag, code=code)
sys.exit(code)
'''


def write_lf(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode('utf-8'))


def get_bash() -> str | None:
    if os.name == 'nt':
        for name in ('C:/Program Files/Git/bin/bash.exe', 'C:/Program Files/Git/usr/bin/bash.exe'):
            if Path(name).is_file():
                return name
    return shutil.which('bash')


class ParallelProcessContracts(unittest.TestCase):
    """CLI tests with actual subprocesses and kernel-backed cross-process locks."""

    def setUp(self):
        if not MANAGER.is_file():
            self.fail('aisbench_run_manager.py is missing')
        self.bash = get_bash()
        if not self.bash:
            self.skipTest('Bash is required for the manager process contracts')
        temporary = tempfile.TemporaryDirectory(prefix='aisbench_parallel_contract_')
        self.root = Path(temporary.name).resolve()
        self.addCleanup(temporary.cleanup)
        self.processes: list[subprocess.Popen] = []
        self.config_hosts: dict[str, str] = {}
        self.addCleanup(self.clear_fixture_leases)
        self.addCleanup(self.stop_children)
        self.binary = self.root / 'bin'
        self.binary.mkdir()
        self.fixture = self.root / 'fixture.py'
        write_lf(self.fixture, FIXTURE)
        python = shlex.quote(Path(sys.executable).as_posix())
        fixture = shlex.quote(self.fixture.as_posix())
        # The thin Bash entry execs python3; a real executable shim keeps that
        # production path testable on Windows without shell-function shortcuts.
        write_lf(self.binary / 'python3', '#!/usr/bin/env bash\nexec ' + python + ' -B "$@"\n')
        (self.binary / 'python3').chmod(0o755)
        write_lf(self.binary / 'docker', '#!/usr/bin/env bash\nexec ' + python + ' -B ' + fixture + ' docker "$@"\n')
        (self.binary / 'docker').chmod(0o755)
        if os.name == 'nt':
            write_lf(self.binary / 'docker.cmd', '@"' + sys.executable + '" -B "' + str(self.fixture) + '" docker %*\n')
        self.checkout = self.create_checkout('checkout')
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('AISBENCH_', 'BASH_FUNC_'))}
        self.env.update(PATH=str(self.binary) + os.pathsep + self.env.get('PATH', ''),
                        AISBENCH_TEST_ROOT=str(self.root), PYTHONDONTWRITEBYTECODE='1',
                        AISBENCH_TEST_DURATION='0.45')
        # Preserve no real user runtime paths in the synthetic workers.
        for key in ('CONTAINER_NAME', 'HOST_OUTPUT_DIR', 'MODEL_HOST_PATH', 'TOOLS_HOST_DIR',
                    'SCENARIOS_JSON_OVERRIDE', 'SCENARIOS_JSON', 'MODEL_NAME', 'HOST_IP',
                    'HOST_PORT', 'API_URL', 'API_KEY', 'SERVICE_ID', 'DATA_NUM_MULTIPLIER',
                    'POD_INFO', 'EXPECTED_IMAGE', 'PEAK_HARD_CAP'):
            self.env.pop(key, None)
        (self.root / 'model').mkdir()
        (self.root / 'tools').mkdir()
        shutil.copyfile(RELEASE_ROOT / 'aisbench_auto_tools_prefix' / 'aisbench_test.py',
                        self.root / 'tools' / 'aisbench_test.py')

    def clear_fixture_leases(self):
        # Failed/unknown leases must persist in production. Remove only this
        # test's synthetic-daemon records after its child processes are stopped.
        base = Path('/tmp') if os.name == 'posix' else Path(tempfile.gettempdir())
        for path in (base / 'aisbench-auto-tools-locks').glob('*.json'):
            if path.is_symlink():
                continue
            record = json.loads(path.read_text(encoding='utf-8'))
            if record.get('daemon_id') == 'offline-daemon-' + self.root.name:
                path.unlink()

    def stop_children(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=12)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)

    def create_checkout(self, name):
        checkout = self.root / name
        (checkout / 'aisbench_auto_tools_prefix').mkdir(parents=True)
        add = checkout / 'automation'
        add.mkdir(parents=True)
        command = (shlex.quote(Path(sys.executable).as_posix()) + ' -B '
                   + shlex.quote(self.fixture.as_posix()) + ' worker "$@"')
        write_lf(checkout / 'run_perf_scenarios.sh', '#!/usr/bin/env bash\nexec ' + command + '\n')
        write_lf(checkout / 'env.env', "SCENARIOS_JSON=$(cat <<'JSON'\n"
                 + '[{"name":"small","input_len":16,"output_len":4,"concurrency":[1]}]\nJSON\n)\n')
        return checkout

    def config(self, filename, *, model='model-a', output=None, container=None, host=None, extra='', checkout=None):
        target = (checkout or self.checkout) / filename
        address = self.config_hosts.setdefault(str(target), '192.168.1.' + str(len(self.config_hosts) + 1))
        values = {'MODEL_NAME': model, 'MODEL_HOST_PATH': (self.root / 'model').as_posix(),
                  'TOOLS_HOST_DIR': (self.root / 'tools').as_posix(),
                  'HOST_IP': host or address, 'HOST_PORT': '8000'}
        if output is not None:
            values['HOST_OUTPUT_DIR'] = Path(output).as_posix()
        if container is not None:
            values['CONTAINER_NAME'] = container
        # Deliberately omit export: automatic loading must handle normal shell assignments.
        lines = [key + '=' + shlex.quote(str(value)) for key, value in values.items()]
        lines += [extra, "SCENARIOS_JSON=$(cat <<'JSON'", '[{"name":"small","input_len":16,"output_len":4,"concurrency":[1]}]', 'JSON', ')', 'export -n SCENARIOS_JSON', '']
        write_lf(target, '\n'.join(lines))
        return target

    def start(self, *configs, checkout=None, env=None, launcher=None):
        command = [sys.executable, '-B', str(launcher or MANAGER),
                   str((checkout or self.checkout) / 'run_perf_scenarios.sh'),
                   *map(str, configs)]
        process = subprocess.Popen(command, cwd=checkout or self.checkout,
                                   env={**self.env, **(env or {})}, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        return process

    def finish(self, process, timeout=30):
        stdout, stderr = process.communicate(timeout=timeout)
        return process.returncode, stdout + stderr

    def run_pair(self, *configs, **kwargs):
        return self.finish(self.start(*configs, **kwargs))

    def events(self, kind=None):
        result = []
        for path in (self.root / 'events').glob('*.jsonl'):
            for line in path.read_text(encoding='utf-8').splitlines():
                if line:
                    row = json.loads(line)
                    if kind is None or row['kind'] == kind:
                        result.append(row)
        return sorted(result, key=lambda row: row['time'])

    def wait_starts(self, count):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if len(self.events('start')) >= count:
                return
            if all(process.poll() is not None for process in self.processes):
                break
            time.sleep(0.025)
        self.fail('workers did not start: ' + repr([(p.poll(), p.communicate(timeout=1) if p.poll() is not None else '') for p in self.processes]))

    def assert_not_started(self, output):
        self.assertFalse(self.events('start'), output)
        self.assertFalse([row for row in self.events('docker')
                          if row['args'][0] in ('run', 'rm', 'start', 'stop', 'exec')], output)

    def latest_batch(self):
        paths = list(self.checkout.glob('outputs/default/*/batch_results.json'))
        self.assertTrue(paths, 'no persisted batch result')
        path = max(paths, key=lambda item: item.stat().st_mtime_ns)
        return path, json.loads(path.read_text(encoding='utf-8'))

    def test_one_two_three_and_four_explicit_configs_source_and_overlap(self):
        for count in (1, 2, 3, 4):
            with self.subTest(task_count=count):
                before = len(self.events('start'))
                configs = [self.config(f'batch{count}_task{index}.env', model=f'model-{index}')
                           for index in range(count)]
                code, output = self.run_pair(*configs, env={'AISBENCH_TEST_DURATION': '1.5',
                                                           'MODEL_NAME': 'stale-parent-must-not-win'})
                self.assertEqual(code, 0, output)
                starts = self.events('start')[before:]
                finishes = self.events('finish')[before:]
                self.assertEqual(len(starts), count, output)
                self.assertEqual(len(finishes), count, output)
                self.assertEqual({row['model'] for row in starts}, {f'model-{index}' for index in range(count)})
                self.assertLess(max(row['time'] for row in starts), min(row['time'] for row in finishes),
                                'one invocation did not overlap all workers')
                _, batch = self.latest_batch()
                self.assertEqual(len(batch['tasks']), count)
                expected_names = {'aisbench-' + task['task'] + '-' + batch['batch_id']
                                  for task in batch['tasks']}
                self.assertEqual({task['container_name'] for task in batch['tasks']}, expected_names)
                self.assertEqual({row['name'] for row in starts}, expected_names)

    def test_invalid_third_or_fourth_config_starts_no_worker(self):
        for count in (3, 4):
            with self.subTest(task_count=count):
                configs = [self.config(f'candidate{index}.env', model=f'model-{index}')
                           for index in range(count)]
                configs[-1] = self.config(f'candidate{count - 1}.env', model='invalid-config')
                code, output = self.run_pair(*configs)
                self.assertNotEqual(code, 0, output)
                self.assert_not_started(output)

    def test_nonadjacent_config_alias_container_output_and_service_conflicts(self):
        for conflict in ('same-file', 'hardlink-file', 'alias', 'container', 'same-output',
                         'nested-output', 'service', 'service-id'):
            with self.subTest(conflict=conflict):
                shared = self.root / 'overlapping-root'
                first_extra = 'SERVICE_ID=shared-service' if conflict == 'service-id' else ''
                a = self.config('a.env', model='first-model', container='fixed-a',
                                output=shared if conflict in ('same-output', 'nested-output') else None,
                                host='192.168.1.100', extra=first_extra)
                b = self.config('b.env', model='second-model')
                c = self.config('c.env', model='third-model')
                final_name = 'a.local.env' if conflict == 'alias' else 'd.env'
                d = self.config(final_name, model='different-model',
                                container='fixed-a' if conflict == 'container' else 'fixed-d',
                                output=(shared if conflict == 'same-output' else shared / 'nested')
                                if conflict in ('same-output', 'nested-output') else None,
                                host='192.168.1.100' if conflict == 'service' else None,
                                extra='SERVICE_ID=shared-service' if conflict == 'service-id' else '')
                if conflict == 'same-file':
                    d = a
                elif conflict == 'hardlink-file':
                    d.unlink()
                    os.link(a, d)
                code, output = self.run_pair(a, b, c, d)
                self.assertNotEqual(code, 0, output)
                self.assert_not_started(output)

    def test_service_identity_normalizes_effective_api_endpoint(self):
        cases = [
            ('api-path', 'HOST_IP=192.168.1.11\nAPI_URL=http://api.invalid:8000/v1/chat/completions',
             'HOST_IP=192.168.1.12\nAPI_URL=http://api.invalid:8000/alternate-path'),
            ('host-case', 'API_URL=http://API.INVALID:8000/v1/chat/completions',
             'API_URL=http://api.invalid:8000/other'),
            ('http-default-port', 'API_URL=http://api.invalid/v1/chat/completions',
             'API_URL=http://api.invalid:80/other'),
            ('https-default-port', 'API_URL=https://api.invalid/v1/chat/completions',
             'API_URL=https://api.invalid:443/other'),
            ('ipv6-compression', 'API_URL=http://[2001:db8::1]:8000/v1/chat/completions',
             'API_URL=http://[2001:0db8:0:0:0:0:0:1]:8000/other'),
            ('api-vs-host-port', 'HOST_IP=192.168.1.11\nAPI_URL=http://192.168.1.90:8000/v1/chat/completions',
             'HOST_IP=192.168.1.90\nHOST_PORT=8000'),
        ]
        for label, first_extra, second_extra in cases:
            with self.subTest(identity=label):
                a = self.config('endpoint_a.env', model='different-model-a', extra=first_extra)
                b = self.config('endpoint_b.env', model='different-model-b', extra=second_extra)
                code, output = self.run_pair(a, b)
                self.assertNotEqual(code, 0, output)
                self.assert_not_started(output)

    def test_two_workers_overlap_and_configuration_isolated_from_parent(self):
        a = self.config('test_a.local.env', extra='HOST_IP=host-a.invalid\nAPI_KEY=secret-a\nDATA_NUM_MULTIPLIER=3\nPEAK_HARD_CAP=1')
        b = self.config('test_b.local.env', model='model-b', extra='HOST_IP=host-b.invalid\nDATA_NUM_MULTIPLIER=5\nPEAK_HARD_CAP=0')
        code, output = self.run_pair(a, b, env={'MODEL_NAME': 'stale-parent', 'HOST_IP': 'stale-parent',
                                               'API_KEY': 'stale-parent-secret', 'POD_INFO': 'stale-metrics',
                                               'DATA_NUM_MULTIPLIER': '99', 'PEAK_HARD_CAP': '7',
                                               'SCENARIOS_JSON': 'stale-parent-json'})
        self.assertEqual(code, 0, output)
        starts, finishes = self.events('start'), self.events('finish')
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(finishes), 2)
        self.assertLess(max(row['time'] for row in starts), min(row['time'] for row in finishes), 'workers ran sequentially')
        by_model = {row['model']: row for row in starts}
        self.assertEqual(set(by_model), {'model-a', 'model-b'})
        for model, alias, host, multiplier, cap in [('model-a', 'test_a', 'host-a.invalid', '3', '1'), ('model-b', 'test_b', 'host-b.invalid', '5', '0')]:
            row = by_model[model]
            self.assertIn(alias, row['name'])
            self.assertIn(alias + '__' + model, Path(row['output']).name)
            self.assertEqual(Path(row['output']).parent.name, 'tasks')
            self.assertEqual((row['host'], row['multiplier'], row['cap']), (host, multiplier, cap))
            self.assertIsNone(row['exported_json'])
            self.assertNotEqual(row['pod_info'], 'stale-metrics')
        self.assertEqual(by_model['model-a']['api_key'], 'secret-a')
        self.assertNotEqual(by_model['model-b']['api_key'], 'stale-parent-secret')
        summaries = list(self.checkout.glob('outputs/default/*/batch_results.json'))
        self.assertEqual(len(summaries), 1, output)
        self.assertTrue(summaries[0].with_suffix('.csv').is_file())
        for path in (summaries[0], summaries[0].with_suffix('.csv')):
            self.assertNotIn('secret-a', path.read_text(encoding='utf-8'))
            self.assertNotIn('stale-parent-secret', path.read_text(encoding='utf-8'))

    def test_pair_sources_each_file_once_and_freezes_snapshot(self):
        marker = self.root / 'source-count'
        extra = 'printf x >> ' + shlex.quote(marker.as_posix())
        a = self.config('once_a.env', extra=extra)
        b = self.config('once_b.env', model='model-b', extra=extra)
        code, output = self.run_pair(a, b)
        self.assertEqual(code, 0, output)
        self.assertEqual(marker.read_text(), 'xx', 'configuration executed more than once')

    def test_editing_original_after_preflight_does_not_change_worker_snapshot(self):
        a = self.config('snapshot_a.env', model='original-model')
        b = self.config('snapshot_b.env', model='peer-model')
        original_hash = hashlib.sha256(a.read_bytes()).hexdigest()
        gate = self.root / 'release-preflight'
        process = self.start(a, b, env={'AISBENCH_TEST_PREFLIGHT_GATE': str(gate)})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not self.events('preflight'):
            if process.poll() is not None:
                break
            time.sleep(0.025)
        self.assertTrue(self.events('preflight'), self.finish(process) if process.poll() is not None else 'preflight timed out')
        self.config('snapshot_a.env', model='changed-model')
        write_lf(a, a.read_text(encoding='utf-8').replace('"small"', '"changed-scenario"'))
        self.assertNotEqual(hashlib.sha256(a.read_bytes()).hexdigest(), original_hash)
        gate.touch()
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        row, = [row for row in self.events('start') if row['model'] == 'original-model']
        self.assertEqual(row['config_hash'], original_hash)
        self.assertEqual(row['scenario_names'], ['small'])
        summary = next(self.checkout.glob('outputs/default/*/batch_results.json'))
        tasks = json.loads(summary.read_text(encoding='utf-8'))['tasks']
        task, = [task for task in tasks if task['task'] == 'snapshot_a']
        self.assertEqual(task['config_sha256'], original_hash)
        self.assertEqual(Path(task['config']), a)

    def test_invalid_port_and_image_reject_both_workers_before_docker_mutation(self):
        a = self.config('valid.env')
        for value in ('HOST_PORT=0', 'HOST_PORT=65536', 'HOST_PORT=not-a-port',
                      "EXPECTED_IMAGE='image with spaces'", "EXPECTED_IMAGE=$'image\\nname'"):
            with self.subTest(configuration=value):
                b = self.config('invalid_resource.env', extra=value)
                code, output = self.run_pair(a, b)
                self.assertNotEqual(code, 0, output)
                self.assertFalse(self.events('start'), output)
                self.assertFalse([row for row in self.events('docker')
                                  if row['args'][0] in ('run', 'rm', 'start', 'stop', 'exec')], output)

    def test_invalid_second_configuration_starts_neither_worker(self):
        a = self.config('valid.env')
        b = self.config('invalid.env', model='invalid-config')
        code, output = self.run_pair(a, b)
        self.assertNotEqual(code, 0, output)
        self.assertFalse(self.events('start'), output)
        self.assertFalse([row for row in self.events('docker') if row['args'][0] in ('run', 'rm', 'start', 'stop', 'exec')], output)

    def test_invalid_source_and_missing_file_are_rejected_before_workers(self):
        a = self.config('a.env')
        bad = self.config('bad.env', extra='return 9')
        for second in (bad, self.root / 'missing.env'):
            with self.subTest(second=second.name):
                code, output = self.run_pair(a, second)
                self.assertNotEqual(code, 0, output)
                self.assertFalse(self.events('start'), output)

    def test_same_container_and_overlapping_outputs_are_rejected_before_workers(self):
        output = self.root / 'shared-output'
        for label, first_output, second_output, first_name, second_name in (
            ('container', self.root / 'a', self.root / 'b', 'same-container', 'same-container'),
            ('same-root', output, output, 'distinct-a', 'distinct-b'),
            ('parent-child', output, output / 'child', 'distinct-a', 'distinct-b'),
            ('child-parent', output / 'child', output, 'distinct-a', 'distinct-b'),
        ):
            with self.subTest(conflict=label):
                a = self.config('a.env', output=first_output, container=first_name)
                b = self.config('b.env', output=second_output, container=second_name)
                code, text = self.run_pair(a, b)
                self.assertNotEqual(code, 0, text)
                self.assertFalse(self.events('start'), text)

    def test_symlink_output_alias_is_rejected_before_workers(self):
        actual = self.root / 'actual-output'
        actual.mkdir()
        alias = self.root / 'output-alias'
        try:
            alias.symlink_to(actual, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest('directory symlink unavailable: ' + str(error))
        a = self.config('a.env', output=actual)
        b = self.config('b.env', output=alias)
        code, output = self.run_pair(a, b)
        self.assertNotEqual(code, 0, output)
        self.assertFalse(self.events('start'), output)

    def test_same_file_and_same_derived_alias_are_rejected(self):
        a = self.config('same.env')
        b = self.config('same.local.env')
        for configs in ((a, a), (a, b)):
            with self.subTest(configs=configs):
                code, output = self.run_pair(*configs)
                self.assertNotEqual(code, 0, output)
                self.assertFalse(self.events('start'), output)

    def test_failure_of_one_worker_does_not_stop_the_other(self):
        configs = [self.config(f'failure_peer_{index}.env', model=model)
                   for index, model in enumerate(('peer-a', 'fail-after-start', 'peer-c', 'peer-d'))]
        code, output = self.run_pair(*configs, env={'AISBENCH_TEST_DURATION': '1.5'})
        self.assertNotEqual(code, 0, output)
        finishes = {row['model']: row['code'] for row in self.events('finish')}
        self.assertEqual(finishes, {'peer-a': 0, 'fail-after-start': 7, 'peer-c': 0, 'peer-d': 0})
        path = next(self.checkout.glob('outputs/default/*/batch_results.csv'))
        with path.open(encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 4)
        self.assertEqual({int(row['exit_code']) for row in rows}, {0, 7})

    def test_container_lock_conflicts_across_checkouts_then_releases(self):
        a, b = self.config('a.env', container='locked-container'), self.config('b.env')
        first = self.start(a, b, env={'AISBENCH_TEST_DURATION': '6'})
        self.wait_starts(2)
        second_checkout = self.create_checkout('another-checkout')
        c = self.config('c.env', output=self.root / 'c-out', container='locked-container', checkout=second_checkout)
        d = self.config('d.env', output=self.root / 'd-out', checkout=second_checkout)
        code, output = self.run_pair(c, d, checkout=second_checkout)
        self.assertNotEqual(code, 0, output)
        self.assertEqual(len(self.events('start')), 2, output)
        self.assertEqual(self.finish(first)[0], 0)
        code, output = self.run_pair(c, d, checkout=second_checkout)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.events('start')), 4)

    def test_output_lock_blocks_nested_root_across_batches(self):
        shared = self.root / 'locked-output'
        a = self.config('a.env', output=shared)
        b = self.config('b.env', output=self.root / 'b-output')
        first = self.start(a, b, env={'AISBENCH_TEST_DURATION': '6'})
        self.wait_starts(2)
        c = self.config('c.env', output=shared / 'nested')
        d = self.config('d.env', output=self.root / 'd-output')
        code, output = self.run_pair(c, d)
        self.assertNotEqual(code, 0, output)
        self.assertEqual(len(self.events('start')), 2, output)
        self.assertEqual(self.finish(first)[0], 0)

    def test_distinct_output_roots_can_run_in_parallel_and_tags_are_unique(self):
        configs = [self.config(name + '.env', model=name) for name in ('a', 'b', 'c', 'd')]
        first = self.start(*configs[:2], env={'AISBENCH_TEST_DURATION': '8'})
        self.wait_starts(2)
        second = self.start(*configs[2:], env={'AISBENCH_TEST_DURATION': '0.5'})
        self.wait_starts(4)
        self.assertEqual(self.finish(first)[0], 0)
        self.assertEqual(self.finish(second)[0], 0)
        starts = self.events('start')
        self.assertEqual(len({row['tag'] for row in starts}), 4)
        self.assertEqual(len({row['identity'] for row in starts}), 4)
        self.assertLess(max(row['time'] for row in starts), min(row['time'] for row in self.events('finish')))

    def test_zero_config_retains_legacy_parent_environment(self):
        code, output = self.finish(self.start(env={'MODEL_NAME': 'legacy-parent-model',
                                                   'HOST_IP': 'legacy-parent.invalid', 'DATA_NUM_MULTIPLIER': '9'}))
        self.assertEqual(code, 0, output)
        start, = self.events('start')
        self.assertEqual((start['model'], start['host'], start['multiplier']),
                         ('legacy-parent-model', 'legacy-parent.invalid', '9'))

    def test_explicit_single_config_loads_file_automatically_once(self):
        marker = self.root / 'single-source-count'
        config = self.config('single.env', model='explicit-file-model', host='192.168.1.42',
                             extra='DATA_NUM_MULTIPLIER=3\nprintf x >> ' + shlex.quote(marker.as_posix()))
        code, output = self.run_pair(config, env={'MODEL_NAME': 'stale-parent-model',
                                                  'HOST_IP': 'stale.invalid', 'DATA_NUM_MULTIPLIER': '99'})
        self.assertEqual(code, 0, output)
        start, = self.events('start')
        self.assertEqual((start['model'], start['host'], start['multiplier']),
                         ('explicit-file-model', '192.168.1.42', '3'))
        self.assertEqual(marker.read_text(), 'x')

    def test_zero_config_loads_default_once_and_parent_presence_overrides_zero_and_empty(self):
        marker = self.root / 'default-source-count'
        self.config('env.env', model='default-file-model', host='192.168.1.44',
                    extra='API_KEY=file-secret\nPEAK_HARD_CAP=1\nDATA_NUM_MULTIPLIER=4\nprintf x >> '
                    + shlex.quote(marker.as_posix()))
        code, output = self.finish(self.start(env={'MODEL_NAME': 'export-model', 'PEAK_HARD_CAP': '0',
                                                   'API_KEY': '', 'DATA_NUM_MULTIPLIER': '9'}))
        self.assertEqual(code, 0, output)
        start, = self.events('start')
        self.assertEqual((start['model'], start['host'], start['cap'], start['api_key'], start['multiplier']),
                         ('export-model', '192.168.1.44', '0', '', '9'))
        self.assertEqual(marker.read_text(), 'x')
        path, result = self.latest_batch()
        task, = result['tasks']
        self.assertEqual(task['parameter_sources']['PEAK_HARD_CAP'], 'parent_export')
        self.assertTrue((path.parent / 'logs' / 'events.jsonl').is_file())

    def test_zero_config_without_exports_uses_default_file_globals(self):
        self.config('env.env', model='file-model', host='192.168.1.52', extra='PEAK_HARD_CAP=1')
        code, output = self.finish(self.start())
        self.assertEqual(code, 0, output)
        start, = self.events('start')
        self.assertEqual((start['model'], start['host'], start['cap']), ('file-model', '192.168.1.52', '1'))

    def test_missing_config_and_source_failure_have_durable_private_preflight_evidence(self):
        bad = self.config('private_source.env', extra='API_KEY=never-display-source-secret\nprintf "%s" "$API_KEY" >&2\nreturn 7')
        for config in (self.root / 'missing.env', bad):
            with self.subTest(config=config.name):
                code, output = self.run_pair(config)
                self.assertNotEqual(code, 0, output)
                self.assertNotIn('never-display-source-secret', output)
                path, _result = self.latest_batch()
                state = json.loads((path.parent / 'batch_state.json').read_text(encoding='utf-8'))
                self.assertEqual(state['state'], 'failed')
                self.assertNotEqual(state['exit_code'], 0)
                self.assertTrue((path.parent / 'logs' / 'batch.log').is_file())
                if config == bad:
                    diagnostics = list((path.parent / 'logs' / 'preflight').glob('*.stderr.raw.log'))
                    self.assertEqual(len(diagnostics), 1)
                    self.assertIn(b'never-display-source-secret', diagnostics[0].read_bytes())
                    self.assertNotIn('never-display-source-secret', (path.parent / 'logs' / 'batch.log').read_text(encoding='utf-8'))
        self.assertFalse(self.events('start'))

    def test_worker_raw_stdout_and_stderr_are_separate_and_invalid_bytes_preserved(self):
        config = self.config('bytes.env', extra='API_KEY=worker-private-token')
        code, output = self.run_pair(config, env={'AISBENCH_TEST_RAW_BYTES': '1'})
        self.assertEqual(code, 0, output)
        _, result = self.latest_batch()
        task, = result['tasks']
        raw = Path(task['diagnostic_paths']['stdout']).read_bytes()
        stderr = Path(task['diagnostic_paths']['stderr']).read_bytes()
        self.assertIn(b'raw-utf8-\xff\x00\r\n', raw)
        self.assertIn(b'API_KEY=worker-private-token', raw)
        self.assertIn(b'worker stderr started', stderr)
        self.assertNotIn('worker-private-token', output)
        self.assertNotIn('worker-private-token', Path(task['launcher_log']).read_text(encoding='utf-8'))

    def test_native_output_and_bootstrap_symlink_are_rejected_without_native_writes(self):
        native = self.checkout / 'aisbench_auto_tools_prefix'
        config = self.config('native.env', output=native / 'outputs')
        code, output = self.run_pair(config)
        self.assertNotEqual(code, 0, output)
        self.assertFalse(list(native.iterdir()))
        self.assertFalse(self.events('start'))
        custom = self.root / 'custom-output'
        custom.mkdir()
        try:
            (custom / '.launcher').symlink_to(native, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest('directory symlink unavailable: ' + str(error))
        config = self.config('launcher_alias.env', output=custom)
        code, output = self.run_pair(config)
        self.assertNotEqual(code, 0, output)
        self.assertFalse(list(native.iterdir()))
        self.assertFalse(self.events('start'))
        checkout = self.create_checkout('bootstrap-checkout')
        (checkout / 'outputs').mkdir()
        try:
            (checkout / 'outputs' / 'default').symlink_to(checkout / 'aisbench_auto_tools_prefix', target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest('directory symlink unavailable: ' + str(error))
        config = self.config('safe.env', checkout=checkout)
        code, output = self.run_pair(config, checkout=checkout)
        self.assertNotEqual(code, 0, output)
        self.assertFalse(list((checkout / 'aisbench_auto_tools_prefix').iterdir()))
        self.assertFalse(self.events('start'))

    def test_log_writer_failure_reaches_supervisor_and_stops_only_affected_task(self):
        launcher = self.root / 'failed_log_launcher.py'
        write_lf(launcher, 'import sys\nfrom pathlib import Path\n'
                 + 'sys.path.insert(0, ' + repr(str(ADD_DIR)) + ')\n'
                 + 'import aisbench_run_manager as manager\n'
                 + 'original = manager.private_binary\n'
                 + 'class BrokenWriter:\n'
                 + '    def __init__(self, wrapped): self.wrapped = wrapped\n'
                 + '    def write(self, value): raise OSError("synthetic disk full")\n'
                 + '    def __getattr__(self, key): return getattr(self.wrapped, key)\n'
                 + 'def open_log(path, roots, mode="xb"):\n'
                 + '    handle = original(path, roots, mode)\n'
                 + '    return BrokenWriter(handle) if Path(path).name == "stdout.raw.log" and "broken__" in str(path) else handle\n'
                 + 'manager.private_binary = open_log\n'
                 + 'sys.exit(manager.main())\n')
        a = self.config('broken.env', model='model-a')
        b = self.config('healthy.env', model='model-b')
        code, output = self.run_pair(a, b, launcher=launcher, env={'AISBENCH_TEST_DURATION': '2'})
        self.assertNotEqual(code, 0, output)
        _, result = self.latest_batch()
        tasks = {task['task']: task for task in result['tasks']}
        self.assertEqual(tasks['broken']['state'], 'failed')
        self.assertEqual(tasks['broken']['logging_error'], 'TASK_LOG_WRITE_FAILED')
        self.assertEqual(tasks['healthy']['state'], 'success')
        broken, = [row for row in self.events('start') if row['model'] == 'model-a']
        healthy, = [row for row in self.events('start') if row['model'] == 'model-b']
        stopped = {row['args'][-1] for row in self.events('docker') if row['args'][0] == 'stop'}
        self.assertIn(broken['identity'], stopped)
        self.assertNotIn(healthy['identity'], stopped)

    def test_new_peak_global_parameters_are_loaded_and_parent_values_do_not_leak(self):
        values = {'PEAK_PRECISION': '2', 'PEAK_CONFIRM_RUNS': '4', 'PEAK_CONFIRM_MIN_REQUESTS': '16',
                  'PEAK_MAX_TRIALS': '20', 'PEAK_TRIAL_TIMEOUT': '45', 'PEAK_TOTAL_TIMEOUT': '300',
                  'PEAK_INVALID_RETRIES': '0', 'PEAK_FLOOR_CONCURRENCY': '2'}
        config = self.config('peak_options.env', extra='\n'.join(key + '=' + value for key, value in values.items()))
        code, output = self.run_pair(config, env={key: '999' for key in values})
        self.assertEqual(code, 0, output)
        start, = self.events('start')
        self.assertEqual(start['peak_options'], values)

    def test_batch_journal_failure_after_start_stops_owned_workload_and_is_nonzero(self):
        launcher = self.root / 'failed_batch_log_launcher.py'
        write_lf(launcher, 'import sys\n'
                 + 'sys.path.insert(0, ' + repr(str(ADD_DIR)) + ')\n'
                 + 'import aisbench_run_manager as manager\n'
                 + 'original = manager.BatchLog.emit\n'
                 + 'def fail(self, event, message, **fields):\n'
                 + '    if event == "task_started": raise OSError("synthetic disk full")\n'
                 + '    return original(self, event, message, **fields)\n'
                 + 'manager.BatchLog.emit = fail\n'
                 + 'sys.exit(manager.main())\n')
        config = self.config('journal_failure.env')
        code, output = self.run_pair(config, launcher=launcher, env={'AISBENCH_TEST_DURATION': '4'})
        self.assertNotEqual(code, 0, output)
        path, result = self.latest_batch()
        state = json.loads((path.parent / 'batch_state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['logging_error'], 'BATCH_LOG_WRITE_FAILED')
        self.assertEqual(state['state'], 'failed')
        for event in self.events('start'):
            container = json.loads((self.root / 'containers' / (event['identity'] + '.json')).read_text())
            self.assertFalse(container['State']['Running'])

    def test_service_lock_conflicts_across_checkouts_then_releases(self):
        a = self.config('a.env', host='192.168.1.90')
        b = self.config('b.env')
        first = self.start(a, b, env={'AISBENCH_TEST_DURATION': '7'})
        self.wait_starts(2)
        second_checkout = self.create_checkout('service-second-checkout')
        c = self.config('c.env', model='different-model', output=self.root / 'c-output',
                        container='different-container-c', host='192.168.1.90', checkout=second_checkout)
        d = self.config('d.env', output=self.root / 'd-output', container='different-container-d',
                        checkout=second_checkout)
        code, output = self.run_pair(c, d, checkout=second_checkout)
        self.assertNotEqual(code, 0, output)
        self.assertEqual(len(self.events('start')), 2, output)
        self.assertEqual(self.finish(first)[0], 0)
        code, output = self.run_pair(c, d, checkout=second_checkout)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.events('start')), 4)

    def test_abrupt_manager_and_worker_death_keeps_service_blocked_until_fixed_id_stops(self):
        host = self.root.name.replace('_', '-') + '.killed.invalid'
        first = self.config('killed.env', host=host)
        process = self.start(first, env={'AISBENCH_TEST_DURATION': '30'})
        self.wait_starts(1)
        old, = self.events('start')
        self.assertEqual(old['lease_on_entry']['owner']['pid'], process.pid)
        self.assertEqual(old['lease_on_entry']['run_id'], old['tag'])
        self.assertEqual(old['lease_on_entry']['container_id'], '')
        self.assertIn('service\0' + host + ':8000', old['lease_on_entry']['resources'])
        old_batch = next(path for path in self.checkout.glob('outputs/default/*/batch_state.json')
                         if json.loads(path.read_text())['owner']['pid'] == process.pid)
        initial = json.loads(old_batch.read_text())
        self.assertTrue(initial['owner']['start_identity'])
        self.assertEqual(initial['finished_at'], '')
        old_state_path = self.root / 'containers' / (old['identity'] + '.json')
        process.kill()
        os.kill(old['pid'], getattr(signal, 'SIGKILL', signal.SIGTERM))
        self.finish(process)
        later = self.config('after_kill.env', host=host, model='later-model')
        code, output = self.run_pair(later)
        self.assertNotEqual(code, 0, 'orphaned fixed ID was running but service was admitted: ' + output)
        self.assertEqual(len(self.events('start')), 1, output)
        recovered = json.loads(old_batch.read_text())
        self.assertEqual(recovered['state'], 'interrupted')
        self.assertIsNone(recovered['exit_code'])
        self.assertEqual(recovered['finished_at'], '')
        independent = self.config('independent.env', host='different.' + host)
        code, output = self.run_pair(independent)
        self.assertEqual(code, 0, output)
        state = json.loads(old_state_path.read_text())
        self.assertTrue(state['State']['Running'])
        state['State']['Running'] = False
        old_state_path.write_text(json.dumps(state))
        code, output = self.run_pair(later)
        self.assertEqual(code, 0, output)

    def test_manager_death_worker_completion_releases_active_lease_without_stopping_container(self):
        host = self.root.name.replace('_', '-') + '.manager-only.invalid'
        process = self.start(self.config('manager_only.env', host=host),
                             env={'AISBENCH_TEST_DURATION': '1.5'})
        self.wait_starts(1)
        old, = self.events('start')
        process.kill()
        self.finish(process)
        deadline = time.monotonic() + 8
        while len(self.events('finish')) != 1 and time.monotonic() < deadline:
            time.sleep(0.025)
        self.assertEqual(len(self.events('finish')), 1)
        docker_before = len(self.events('docker'))
        code, output = self.run_pair(self.config('worker_completed.env', host=host))
        self.assertEqual(code, 0, output)
        self.assertFalse([row for row in self.events('docker')[docker_before:]
                          if row['args'][0] == 'stop'], output)
        state = json.loads((self.root / 'containers' / (old['identity'] + '.json')).read_text())
        self.assertTrue(state['State']['Running'], 'idle completed tool container need not be stopped')
        previous = next(json.loads(path.read_text()) for path in self.checkout.glob('outputs/default/*/batch_state.json')
                        if json.loads(path.read_text())['owner']['pid'] == process.pid)
        self.assertEqual(previous['state'], 'interrupted')
        self.assertEqual(previous['recovery_observations'][old['tag']]['reason'], 'worker_completed_without_manager')
        self.assertIsNone(previous['exit_code'])
        self.assertEqual(previous['finished_at'], '')

    def test_unknown_fixed_id_stays_isolated_even_when_unidentified_container_was_stopped(self):
        host = self.root.name.replace('_', '-') + '.unknown-id.invalid'
        process = self.start(self.config('unknown.env', host=host),
                             env={'AISBENCH_TEST_DURATION': '30', 'AISBENCH_TEST_HIDE_RUNTIME_ID': '1'})
        self.wait_starts(1)
        old, = self.events('start')
        process.kill()
        os.kill(old['pid'], getattr(signal, 'SIGKILL', signal.SIGTERM))
        self.finish(process)
        old_state_path = self.root / 'containers' / (old['identity'] + '.json')
        state = json.loads(old_state_path.read_text())
        state['State']['Running'] = False
        old_state_path.write_text(json.dumps(state))
        later = self.config('after_unknown.env', host=host)
        docker_before = len(self.events('docker'))
        code, output = self.run_pair(later)
        self.assertNotEqual(code, 0, output)
        self.assertEqual(len(self.events('start')), 1)
        self.assertFalse([row for row in self.events('docker')[docker_before:]
                          if row['args'][0] in ('inspect', 'stop')], output)
        # Restore the original fixed-ID evidence, never infer it by container name.
        runtime_path = Path(old['output']) / '.launcher' / old['tag'] / 'runtime.json'
        runtime = json.loads(runtime_path.read_text())
        runtime['container_id'] = old['identity']
        runtime_path.write_text(json.dumps(runtime))
        code, output = self.run_pair(later)
        self.assertEqual(code, 0, output)

    def test_known_container_or_image_preflight_failure_creates_no_unknown_lease(self):
        for reason in ('unmanaged-container', 'missing-image'):
            with self.subTest(reason=reason):
                before = len(self.events('start'))
                alias = reason.replace('-', '_')
                config = self.config(alias + '.env', container=alias)
                environment = {}
                state_path = None
                if reason == 'unmanaged-container':
                    identity = hashlib.sha256((alias + self.root.name).encode()).hexdigest()
                    state_path = self.root / 'containers' / (identity + '.json')
                    state_path.parent.mkdir(exist_ok=True)
                    state_path.write_text(json.dumps(dict(Id=identity, Name='/' + alias,
                        Config=dict(Image='offline-image', Labels={}), State=dict(Running=True))))
                else:
                    environment['AISBENCH_TEST_IMAGE_MISSING'] = '1'
                code, output = self.run_pair(config, env=environment)
                self.assertNotEqual(code, 0, output)
                self.assertEqual(len(self.events('start')), before)
                base = Path('/tmp') if os.name == 'posix' else Path(tempfile.gettempdir())
                records = [json.loads(path.read_text()) for path in
                           (base / 'aisbench-auto-tools-locks').glob('*.active.json')]
                self.assertFalse([record for record in records if record.get('daemon_id') ==
                                  'offline-daemon-' + self.root.name], 'preflight must precede any lease')
                if state_path is not None:
                    self.assertTrue(json.loads(state_path.read_text())['State']['Running'])
                    state_path.unlink()  # The test owns this synthetic state; no Docker rm is called.
                code, output = self.run_pair(config)
                self.assertEqual(code, 0, output)

    def test_isolation_record_scan_allows_normal_concurrent_removal_but_rejects_corruption_and_denial(self):
        manager = importlib.import_module('aisbench_run_manager')
        locks = manager.ResourceLocks()
        locks.directory = self.root / 'record-scan'
        locks.directory.mkdir()
        for suffix, scanner in (('active', locks._check_active), ('blocked', locks._check_quarantines)):
            with self.subTest(kind=suffix):
                path = locks.directory / ('existing.' + suffix + '.json')
                path.write_text('{}')
                already_listed = list(locks.directory.glob('*.' + suffix + '.json'))
                path.unlink()  # Owner released between directory listing and read.
                with mock.patch.object(Path, 'glob', return_value=iter(already_listed)):
                    scanner([])
                path.write_text('{broken')
                with mock.patch.object(Path, 'glob', return_value=iter([path])):
                    with self.assertRaises(manager.RunError):
                        scanner([])
                with mock.patch.object(Path, 'glob', return_value=iter([path])):
                    with mock.patch.object(Path, 'read_text', side_effect=PermissionError('fixture access denied')):
                        with self.assertRaises(manager.RunError):
                            scanner([])
                path.write_text('[]')
                with mock.patch.object(Path, 'glob', return_value=iter([path])):
                    with self.assertRaises(manager.RunError):
                        scanner([])

    def test_service_id_lock_conflicts_across_distinct_endpoints(self):
        a = self.config('a.env', extra='SERVICE_ID=shared-backend')
        b = self.config('b.env')
        first = self.start(a, b, env={'AISBENCH_TEST_DURATION': '7'})
        self.wait_starts(2)
        c = self.config('c.env', extra='SERVICE_ID=shared-backend')
        d = self.config('d.env')
        code, output = self.run_pair(c, d)
        self.assertNotEqual(code, 0, output)
        self.assertEqual(len(self.events('start')), 2, output)
        self.assertEqual(self.finish(first)[0], 0)

    def test_cleanup_failure_quarantines_service_until_old_fixed_id_is_proven_stopped_or_absent(self):
        for proof in ('stopped', 'absent'):
            with self.subTest(recovery_proof=proof):
                before = len(self.events('start'))
                # Unique synthetic service avoids sharing a failed cleanup marker
                # with unrelated tests that use the public example IP range.
                host = proof + '.' + self.root.name.replace('_', '-') + '.invalid'
                first = self.config('unclean_' + proof + '.env', host=host)
                code, output = self.run_pair(first, env={'AISBENCH_TEST_DURATION': '0.05',
                                                         'AISBENCH_TEST_ABNORMAL': '1',
                                                         'AISBENCH_TEST_STOP_FAIL': '1'})
                self.assertNotEqual(code, 0, output)
                _, failed_batch = self.latest_batch()
                failed, = failed_batch['tasks']
                self.assertEqual(failed['state'], 'cleanup_failed', output)
                old, = self.events('start')[before:]
                old_path = self.root / 'containers' / (old['identity'] + '.json')
                old_state = json.loads(old_path.read_text())
                self.assertTrue(old_state['State']['Running'])
                self.assertTrue(self.events('stop-refused'), output)

                # A new default container and new output path must not bypass
                # the persistent service block after the process lock releases.
                later = self.config('later_' + proof + '.env', model='new-worker', host=host)
                docker_before = len(self.events('docker'))
                code, output = self.run_pair(later)
                self.assertNotEqual(code, 0, output)
                self.assertEqual(len(self.events('start')), before + 1, output)
                inspections = [row['args'] for row in self.events('docker')[docker_before:]
                               if row['args'][0] == 'inspect']
                self.assertTrue(inspections, output)
                self.assertTrue(all(args[-1] == old['identity'] for args in inspections), output)
                self.assertFalse([row for row in self.events('docker')[docker_before:]
                                  if row['args'][0] in ('run', 'rm', 'start', 'stop', 'exec')], output)

                if proof == 'stopped':
                    old_state['State']['Running'] = False
                    old_path.write_text(json.dumps(old_state))
                else:
                    old_path.unlink()
                code, output = self.run_pair(later)
                self.assertEqual(code, 0, output)
                self.assertEqual(len(self.events('start')), before + 2, output)
                _, recovered = self.latest_batch()
                self.assertEqual(recovered['tasks'][0]['state'], 'success', output)

    def test_reused_custom_output_with_new_failure_does_not_publish_old_named_metrics(self):
        output_dir = self.root / 'fixed-output'
        config = self.config('same_output.env', model='same-model', output=output_dir)
        code, output = self.run_pair(config)
        self.assertEqual(code, 0, output)
        _, first = self.latest_batch()
        initial, = first['tasks']
        old_csv = Path(initial['source_csv'])
        old_bytes = old_csv.read_bytes()
        named_csv = Path(initial['result_csv'])
        self.assertTrue(named_csv.is_file())
        code, output = self.run_pair(config, env={'AISBENCH_TEST_DURATION': '0.05',
                                                 'AISBENCH_TEST_ABNORMAL': '1'})
        self.assertNotEqual(code, 0, output)
        _, second = self.latest_batch()
        failed, = second['tasks']
        self.assertEqual(failed['state'], 'failed', output)
        self.assertEqual(failed['result_csv'], '')
        self.assertEqual(failed['results_state'], 'missing')
        self.assertFalse(named_csv.exists(), 'previous run metrics were presented beside the new failure log')
        self.assertFalse((output_dir / (failed['output_label'] + '__execution.log')).exists())
        self.assertTrue((output_dir / (failed['output_label'] + '__launcher.log')).is_file())
        self.assertEqual(old_csv.read_bytes(), old_bytes, 'historical source metrics were removed or rewritten')
        with (self.latest_batch()[0].parent / 'merged_metrics.csv').open(encoding='utf-8-sig', newline='') as handle:
            self.assertEqual(list(csv.DictReader(handle)), [])

    def test_running_batch_refreshes_identity_and_result_paths_for_four_tasks(self):
        configs = [self.config(f'live{index}.env', model=f'model-{index}') for index in range(4)]
        process = self.start(*configs, env={'AISBENCH_TEST_DURATION': '5'})
        self.wait_starts(4)
        deadline = time.monotonic() + 4
        payload = {}
        while time.monotonic() < deadline:
            _, payload = self.latest_batch()
            tasks = payload.get('tasks', [])
            if len(tasks) == 4 and all(task.get('container_id') and task.get('result_dir') for task in tasks):
                break
            time.sleep(0.05)
        self.assertIsNone(process.poll(), 'status was populated only after all workers exited')
        self.assertEqual(len(payload.get('tasks', [])), 4)
        for task in payload['tasks']:
            self.assertEqual(task['state'], 'running')
            self.assertRegex(task['container_id'], r'^[a-f0-9]{64}$')
            self.assertTrue(Path(task['result_dir']).is_dir())
            self.assertTrue(task['started_at'])
            self.assertEqual(task['planned_tests'], 1)
            self.assertEqual(task['test_index'], 1)
            self.assertEqual(task['current_scenario'], 'small_c1')
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)

    def test_four_task_batch_names_and_merged_metrics_preserve_original_strings(self):
        models = ['DeepSeek-V4-Flash', 'Qwen3-32B', 'model-c', 'model-d']
        configs = [self.config(f'task{index}.env', model=model) for index, model in enumerate(models)]
        code, output = self.run_pair(*configs, env={'AISBENCH_TEST_DURATION': '1.5'})
        self.assertEqual(code, 0, output)
        path, payload = self.latest_batch()
        with (path.parent / 'merged_metrics.csv').open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            merged_header = reader.fieldnames
            merged = list(reader)
        self.assertEqual(len(merged), 4)
        self.assertEqual(merged_header[:6], ['task', 'model', 'endpoint', 'service_id', 'run_id', 'source_csv'])
        self.assertEqual(len(merged_header), 29)
        self.assertEqual({row['model'] for row in merged}, set(models))
        for task in payload['tasks']:
            label = task['task'] + '__' + task['model']
            self.assertEqual(task['output_label'], label)
            self.assertEqual(Path(task['host_output_dir']), path.parent / 'tasks' / label)
            self.assertEqual(Path(task['result_csv']).name, label + '__all_results.csv')
            self.assertEqual(Path(task['source_csv']), Path(task['result_dir']) / 'all_results.csv')
            self.assertTrue((path.parent / 'tasks' / label / 'result_index.json').is_file())
            self.assertEqual(Path(task['source_csv']).read_bytes(), Path(task['result_csv']).read_bytes())
            self.assertEqual(task['results_state'], 'ready')
            self.assertEqual(task['planned_tests'], 1)
            self.assertEqual((task['completed_tests'], task['passed_tests'], task['failed_tests']), (1, 1, 0))
            self.assertRegex(task['started_at'], r'[+-]\d{2}:\d{2}$')
            self.assertRegex(task['finished_at'], r'[+-]\d{2}:\d{2}$')
            with Path(task['source_csv']).open(encoding='utf-8-sig', newline='') as handle:
                source_reader = csv.DictReader(handle)
                source_rows = list(source_reader)
                source_header = source_reader.fieldnames
            rows = [row for row in merged if row['task'] == task['task']]
            self.assertEqual(len(rows), len(source_rows))
            self.assertEqual([{key: row[key] for key in source_header} for row in rows], source_rows)
            self.assertEqual(rows[0]['input_len'], '16.0000')
            self.assertEqual(rows[0]['prefill_token_throughput'], '')
            self.assertEqual(rows[0]['repeat_rate'], '0.90')

    def test_special_model_names_have_safe_distinct_bounded_output_labels(self):
        models = ['Org/DeepSeek V4', 'Org DeepSeek/V4', '模型/测试:001', 'very-long-model-' * 16]
        configs = [self.config(f'encoded{index}.env', model=model) for index, model in enumerate(models)]
        code, output = self.run_pair(*configs, env={'AISBENCH_TEST_DURATION': '1.5'})
        self.assertEqual(code, 0, output)
        _, payload = self.latest_batch()
        tasks = payload['tasks']
        self.assertEqual({task['model'] for task in tasks}, set(models))
        self.assertEqual(len({task['output_label'] for task in tasks}), 4)
        for task in tasks:
            self.assertRegex(task['output_label'], r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
            self.assertLessEqual(len(task['output_label']), 114)
            self.assertEqual(os.path.normcase(Path(task['host_output_dir']).name),
                             os.path.normcase(task['output_label']))
            self.assertEqual(Path(task['result_csv']).name, task['output_label'] + '__all_results.csv')
            self.assertTrue(Path(task['result_csv']).is_file())

    def test_same_second_repeated_launches_have_distinct_ids_and_directories(self):
        launcher = self.root / 'frozen_clock_launcher.py'
        write_lf(launcher, 'import sys\nfrom datetime import datetime\n'
                 + 'sys.path.insert(0, ' + repr(str(ADD_DIR)) + ')\n'
                 + 'import aisbench_run_manager as manager\n'
                 + 'class FixedDatetime(datetime):\n'
                 + '    @classmethod\n'
                 + '    def now(cls, tz=None):\n'
                 + '        return cls(2026, 10, 19, 12, 0, 0, tzinfo=tz)\n'
                 + 'manager.datetime = FixedDatetime\n'
                 + 'sys.exit(manager.main())\n')
        a, b = self.config('same_second_a.env'), self.config('same_second_b.env')
        for _ in range(2):
            code, output = self.run_pair(a, b, launcher=launcher)
            self.assertEqual(code, 0, output)
        starts = self.events('start')
        self.assertEqual(len(starts), 4)
        self.assertEqual(len({row['tag'] for row in starts}), 4)
        self.assertEqual(len({row['name'] for row in starts}), 4,
                         'same tasks reused a default container name across batches')
        self.assertTrue(all(row['tag'].startswith('20261019_120000_') for row in starts))
        directories = [Path(row['output']) / 'scenario_results' / row['tag'] for row in starts]
        self.assertEqual(len(set(directories)), 4)
        self.assertTrue(all(path.is_dir() for path in directories))
        self.assertEqual(len(list(self.checkout.glob('outputs/default/*/batch_results.json'))), 2)

    @unittest.skipIf(os.name == 'nt', 'POSIX signal delivery requires Linux CI')
    def test_sigint_and_sigterm_stop_only_recorded_fixed_container_ids(self):
        foreign_id = 'f' * 64
        foreign_path = self.root / 'containers' / (foreign_id + '.json')
        foreign_path.parent.mkdir(exist_ok=True)
        foreign_path.write_text(json.dumps({'Id': foreign_id, 'Name': '/protected-model-service',
                                            'Config': {'Image': 'model-service', 'Labels': {}},
                                            'State': {'Running': True}}))
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                before = len(self.events('start'))
                configs = [self.config(f'signal_{index}.env', model=f'model-{index}') for index in range(4)]
                process = self.start(*configs, env={'AISBENCH_TEST_DURATION': '60'})
                self.wait_starts(before + 4)
                starts = self.events('start')[before:]
                process.send_signal(signum)
                code, output = self.finish(process, timeout=20)
                self.assertNotEqual(code, 0, output)
                stopped = {row['args'][-1] for row in self.events('docker') if row['args'][0] == 'stop'}
                self.assertTrue({row['identity'] for row in starts}.issubset(stopped), output)
                self.assertTrue(all(len(identity) == 64 for identity in stopped), output)
                self.assertNotIn(foreign_id, stopped)
                self.assertTrue(json.loads(foreign_path.read_text())['State']['Running'])
                for row in starts:
                    state = json.loads((self.root / 'containers' / (row['identity'] + '.json')).read_text())
                    self.assertFalse(state['State']['Running'])
                    # Fixture worker is the actual child, not just a Docker CLI.
                    with self.assertRaises(ProcessLookupError):
                        os.kill(row['pid'], 0)

    def test_real_shell_preflight_uses_production_validation_without_docker(self):
        config = self.config('real.env', model='unused-during-managed-preflight')
        report = self.root / 'real-preflight.json'
        env = {**self.env, 'AISBENCH_MANAGED_WORKER': '1', 'AISBENCH_PREFLIGHT_REPORT': str(report),
               'MODEL_NAME': 'real-preflight-model', 'MODEL_HOST_PATH': (self.root / 'model').as_posix(),
               'TOOLS_HOST_DIR': (self.root / 'tools').as_posix(), 'HOST_OUTPUT_DIR': (self.root / 'unused-out').as_posix(),
               'HOST_IP': '192.168.1.123', 'HOST_PORT': '8000', 'NPU_NUM': '1', 'PEAK_HARD_CAP': '1',
               'DATA_NUM_MULTIPLIER': '4', 'MAX_CONTEXT_LEN': '4096',
               'API_URL': 'https://api.invalid:8443/v1/chat/completions', 'SERVICE_ID': 'preflight-service'}
        command = [self.bash, SHELL.as_posix(), config.as_posix()]
        process = subprocess.run(command, env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertNotIn('FORBIDDEN_DOCKER_CALL', process.stdout + process.stderr)
        self.assertFalse(self.events('docker'), 'preflight contacted Docker')
        payload = json.loads(report.read_text(encoding='utf-8'))
        self.assertEqual(payload['model_name'], 'real-preflight-model')
        self.assertEqual(str(payload['host_ip']), '192.168.1.123')
        self.assertEqual(payload['api_url'], 'https://api.invalid:8443/v1/chat/completions')
        self.assertEqual(payload['service_id'], 'preflight-service')
        self.assertEqual(payload['planned_tests'], 1)
        self.assertEqual(payload['effective_config']['DATA_NUM_MULTIPLIER'], '4')
        self.assertFalse((self.root / 'unused-out').exists(), 'preflight created runtime results')
        # Exercise real parser rejection, rather than only the fake worker path.
        write_lf(config, "SCENARIOS_JSON=$(cat <<'JSON'\n[{\"name\":\"invalid\",\"input_len\":16,\"output_len\":4,\"concurrency\":[0]}]\nJSON\n)\n")
        report.unlink()
        process = subprocess.run(command, env=env, text=True, capture_output=True, timeout=30)
        self.assertNotEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertFalse(report.exists())
        self.assertNotIn('FORBIDDEN_DOCKER_CALL', process.stdout + process.stderr)
        self.assertFalse(self.events('docker'), 'invalid preflight contacted Docker')

    def test_real_shell_rejects_replaced_name_before_executing_new_container(self):
        # Use the production Python worker's identity check, rather than a
        # source-text extraction that would test an obsolete shell function.
        sys.path.insert(0, str(ADD_DIR))
        spec = importlib.util.spec_from_file_location('aisbench_worker_identity_contract', ADD_DIR / 'aisbench_worker.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        old_id, new_id = 'a' * 64, 'b' * 64
        old = {'Id': old_id, 'Name': '/same-name', 'State': {'Running': True},
               'Config': {'Image': 'offline-image', 'Labels': {'io.aisbench-auto-tools-prefix.managed': 'true'}}}
        configuration = self.root / 'identity-map.json'
        code = ('import json,pathlib,sys\n'
                'path=pathlib.Path(sys.argv[1]); cfg=json.loads(path.read_text()); args=sys.argv[2:]\n'
                'with pathlib.Path(sys.argv[1]+".calls").open("a") as h: h.write(json.dumps(args)+"\\n")\n'
                'if args[0] != "inspect": raise SystemExit(97)\n'
                'payload=cfg["fixed"] if args[-1] == cfg["fixed"]["Id"] else cfg["named"]\n'
                'print(payload["Id"] if "--format" in args else json.dumps([payload]))\n')
        docker = [sys.executable, '-B', '-c', code, str(configuration)]
        configuration.write_text(json.dumps({'fixed': old, 'named': old}))
        self.assertEqual(module.assert_container_identity(docker, old_id, 'same-name')['Id'], old_id)
        configuration.write_text(json.dumps({'fixed': old, 'named': {**old, 'Id': new_id}}))
        with self.assertRaises(RuntimeError):
            module.assert_container_identity(docker, old_id, 'same-name')
        recorded = [json.loads(line) for line in Path(str(configuration) + '.calls').read_text().splitlines()]
        self.assertTrue(recorded)
        self.assertTrue(all(arguments[0] == 'inspect' for arguments in recorded))


if __name__ == '__main__':
    unittest.main()
