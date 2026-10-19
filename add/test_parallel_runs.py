"""Offline process-level contracts for the run manager and real shell preflight.

Run: python -B -m unittest discover -s add -p test_parallel_runs.py
The fixtures are created in a temporary directory. No real Docker daemon,
model, tokenizer, server, repository env.local.env or benchmark is accessed.
"""
from __future__ import annotations

import csv
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

sys.dont_write_bytecode = True
ADD_DIR = Path(__file__).resolve().parent
MANAGER = ADD_DIR / "aisbench_run_manager.py"
SHELL = ADD_DIR / "run_perf_scenarios.sh"

# One helper implements the synthetic worker and Docker CLI. The manager itself
# remains the production implementation: its file loading, environment handling,
# resource locks, process supervision and result serialization all run normally.
FIXTURE = r'''
import hashlib, json, os, pathlib, re, signal, sys, time
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
result = output / 'scenario_results' / tag
result.mkdir(parents=True, exist_ok=False)
container = {'Id': identity, 'Name': '/' + name,
             'Config': {'Image': image, 'Labels': {'io.aisbench-auto-tools-prefix.managed': 'true'}},
             'State': {'Running': True}}
(states / (identity + '.json')).write_text(json.dumps(container))
runtime = {'container_id': identity, 'container_name': name, 'image': image,
           'run_id': tag, 'result_dir': str(result), 'complete': False}
runtime_path = pathlib.Path(env['AISBENCH_RUNTIME_REPORT'])
runtime_path.write_text(json.dumps(runtime))
event('start', model=model, name=name, output=str(output), tag=tag, identity=identity,
      host=env.get('HOST_IP'), api_key=env.get('API_KEY'),
      multiplier=env.get('DATA_NUM_MULTIPLIER'), cap=env.get('PEAK_HARD_CAP'),
      exported_json=env.get('SCENARIOS_JSON'), pod_info=env.get('POD_INFO'),
      config_hash=config_hash, scenario_names=scenario_names)
duration = float(env.get('AISBENCH_TEST_DURATION', '0.4'))
end = time.monotonic() + duration
while time.monotonic() < end:
    if not json.loads((states / (identity + '.json')).read_text())['State']['Running']:
        event('stopped', model=model, identity=identity)
        sys.exit(143)
    time.sleep(0.025)
code = 7 if model == 'fail-after-start' else 0
runtime['complete'] = True
runtime_path.write_text(json.dumps(runtime))
(result / 'all_results.csv').write_text('scenario,status\nfixture,' + ('passed' if code == 0 else 'failed') + '\n')
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
        self.addCleanup(self.stop_children)
        self.binary = self.root / 'bin'
        self.binary.mkdir()
        self.fixture = self.root / 'fixture.py'
        write_lf(self.fixture, FIXTURE)
        python = shlex.quote(Path(sys.executable).as_posix())
        fixture = shlex.quote(self.fixture.as_posix())
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
                    'SCENARIOS_JSON_OVERRIDE', 'SCENARIOS_JSON'):
            self.env.pop(key, None)
        (self.root / 'model').mkdir()
        (self.root / 'tools').mkdir()
        write_lf(self.root / 'tools' / 'aisbench_test.py', '# Offline placeholder.\n')

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
        add = checkout / 'add'
        add.mkdir(parents=True)
        command = (shlex.quote(Path(sys.executable).as_posix()) + ' -B '
                   + shlex.quote(self.fixture.as_posix()) + ' worker "$@"')
        write_lf(add / 'run_perf_scenarios.sh', '#!/usr/bin/env bash\nexec ' + command + '\n')
        write_lf(add / 'env.env', "SCENARIOS_JSON=$(cat <<'JSON'\n"
                 + '[{"name":"small","input_len":16,"output_len":4,"concurrency":[1]}]\nJSON\n)\n')
        return checkout

    def config(self, filename, *, model='model-a', output=None, container=None, extra='', checkout=None):
        target = (checkout or self.checkout) / 'add' / filename
        values = {'MODEL_NAME': model, 'MODEL_HOST_PATH': (self.root / 'model').as_posix(),
                  'TOOLS_HOST_DIR': (self.root / 'tools').as_posix()}
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
                   str((checkout or self.checkout) / 'add' / 'run_perf_scenarios.sh'),
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
            self.assertEqual(row['name'], 'ais_bench-' + alias)
            self.assertEqual(Path(row['output']), self.checkout / 'outputs' / alias)
            self.assertEqual((row['host'], row['multiplier'], row['cap']), (host, multiplier, cap))
            self.assertIsNone(row['exported_json'])
            self.assertNotEqual(row['pod_info'], 'stale-metrics')
        self.assertEqual(by_model['model-a']['api_key'], 'secret-a')
        self.assertNotEqual(by_model['model-b']['api_key'], 'stale-parent-secret')
        summaries = list(self.checkout.glob('outputs/parallel/*/batch_results.json'))
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
        summary = next(self.checkout.glob('outputs/parallel/*/batch_results.json'))
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
        a = self.config('fail.env', model='fail-after-start')
        b = self.config('success.env', model='successful-peer')
        code, output = self.run_pair(a, b)
        self.assertNotEqual(code, 0, output)
        finishes = {row['model']: row['code'] for row in self.events('finish')}
        self.assertEqual(finishes, {'fail-after-start': 7, 'successful-peer': 0})
        path = next(self.checkout.glob('outputs/parallel/*/batch_results.csv'))
        with path.open(encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual({int(row['exit_code']) for row in rows}, {0, 7})

    def test_container_lock_conflicts_across_checkouts_then_releases(self):
        a, b = self.config('a.env'), self.config('b.env')
        first = self.start(a, b, env={'AISBENCH_TEST_DURATION': '6'})
        self.wait_starts(2)
        second_checkout = self.create_checkout('another-checkout')
        c = self.config('c.env', output=self.root / 'c-out', container='ais_bench-a', checkout=second_checkout)
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

    def test_single_config_retains_legacy_parent_environment(self):
        config = self.config('single.env', model='file-must-not-be-sourced')
        for configs in ((), (config,)):
            with self.subTest(config_arguments=len(configs)):
                code, output = self.finish(self.start(*configs, env={'MODEL_NAME': 'legacy-parent-model',
                                                                   'HOST_IP': 'legacy-parent-host', 'DATA_NUM_MULTIPLIER': '9'}))
                self.assertEqual(code, 0, output)
                start = self.events('start')[-1]
                self.assertEqual((start['model'], start['host'], start['multiplier']), ('legacy-parent-model', 'legacy-parent-host', '9'))

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
        self.assertTrue(all(row['tag'].startswith('20261019_120000_') for row in starts))
        directories = [Path(row['output']) / 'scenario_results' / row['tag'] for row in starts]
        self.assertEqual(len(set(directories)), 4)
        self.assertTrue(all(path.is_dir() for path in directories))
        self.assertEqual(len(list(self.checkout.glob('outputs/parallel/*/batch_results.json'))), 2)

    @unittest.skipIf(os.name == 'nt', 'POSIX signal delivery requires Linux CI')
    def test_sigint_and_sigterm_stop_only_recorded_fixed_container_ids(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                before = len(self.events('start'))
                a = self.config('signal_a.env')
                b = self.config('signal_b.env')
                process = self.start(a, b, env={'AISBENCH_TEST_DURATION': '60'})
                self.wait_starts(before + 2)
                starts = self.events('start')[before:]
                process.send_signal(signum)
                code, output = self.finish(process, timeout=20)
                self.assertNotEqual(code, 0, output)
                stopped = {row['args'][-1] for row in self.events('docker') if row['args'][0] == 'stop'}
                self.assertTrue({row['identity'] for row in starts}.issubset(stopped), output)
                for row in starts:
                    state = json.loads((self.root / 'containers' / (row['identity'] + '.json')).read_text())
                    self.assertFalse(state['State']['Running'])
                    # Fixture worker is the actual child, not just a Docker CLI.
                    with self.assertRaises(ProcessLookupError):
                        os.kill(row['pid'], 0)

    def test_real_shell_preflight_uses_production_validation_without_docker(self):
        config = self.config('real.env', model='unused-during-managed-preflight')
        report = self.root / 'real-preflight.json'
        python = shlex.quote(Path(sys.executable).as_posix())
        wrapper = self.root / 'real-preflight.sh'
        code = ('python3() { ' + python + ' -B "$@"; }\nexport -f python3\n'
                'docker() { echo FORBIDDEN_DOCKER_CALL >&2; return 97; }\nexport -f docker\n'
                'exec "$BASH" ' + shlex.quote(SHELL.as_posix()) + ' ' + shlex.quote(config.as_posix()) + '\n')
        write_lf(wrapper, code)
        env = {**self.env, 'AISBENCH_MANAGED_WORKER': '1', 'AISBENCH_PREFLIGHT_REPORT': str(report),
               'MODEL_NAME': 'real-preflight-model', 'MODEL_HOST_PATH': (self.root / 'model').as_posix(),
               'TOOLS_HOST_DIR': (self.root / 'tools').as_posix(), 'HOST_OUTPUT_DIR': (self.root / 'unused-out').as_posix(),
               'HOST_IP': '192.168.1.123', 'HOST_PORT': '8000', 'NPU_NUM': '1', 'PEAK_HARD_CAP': '1',
               'DATA_NUM_MULTIPLIER': '4', 'MAX_CONTEXT_LEN': '4096'}
        process = subprocess.run([self.bash, wrapper.as_posix()], env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertNotIn('FORBIDDEN_DOCKER_CALL', process.stdout + process.stderr)
        payload = json.loads(report.read_text(encoding='utf-8'))
        self.assertEqual(payload['model_name'], 'real-preflight-model')
        self.assertEqual(str(payload['host_ip']), '192.168.1.123')
        self.assertFalse((self.root / 'unused-out').exists(), 'preflight created runtime results')
        # Exercise real parser rejection, rather than only the fake worker path.
        write_lf(config, "SCENARIOS_JSON=$(cat <<'JSON'\n[{\"name\":\"invalid\",\"input_len\":16,\"output_len\":4,\"concurrency\":[0]}]\nJSON\n)\n")
        report.unlink()
        process = subprocess.run([self.bash, wrapper.as_posix()], env=env, text=True, capture_output=True, timeout=30)
        self.assertNotEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertFalse(report.exists())
        self.assertNotIn('FORBIDDEN_DOCKER_CALL', process.stdout + process.stderr)

    def test_real_shell_rejects_replaced_name_before_executing_new_container(self):
        source = SHELL.read_text(encoding='utf-8')
        start = source.index('assert_container_ready() {')
        function = source[start:source.index('\n}\n', start) + 3]
        old_id, new_id = 'a' * 64, 'b' * 64
        code = '''
set -uo pipefail
log() { printf '%s\\n' "$*"; }
docker() {
    if [ "$1" = inspect ] && [ "$4" = "$CURRENT_ID" ]; then
        printf '%s|true\\n' "$CURRENT_ID"
    elif [ "$1" = inspect ] && [ "$4" = "$CONTAINER_NAME" ]; then
        printf '%s\\n' "$REPLACEMENT_ID"
    else
        echo FORBIDDEN_EXEC >&2
        return 97
    fi
}
'''
        code += function + '\nassert_container_ready || exit 23\ndocker exec "$CURRENT_ID" true\n'
        wrapper = self.root / 'identity-contract.sh'
        write_lf(wrapper, code)
        process = subprocess.run([self.bash, wrapper.as_posix()],
                                 env={**self.env, 'CURRENT_ID': old_id,
                                      'CONTAINER_NAME': 'same-name', 'REPLACEMENT_ID': new_id},
                                 text=True, capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 23, process.stdout + process.stderr)
        self.assertIn(old_id, process.stdout)
        self.assertIn(new_id, process.stdout)
        self.assertNotIn('FORBIDDEN_EXEC', process.stdout + process.stderr)


if __name__ == '__main__':
    unittest.main()
