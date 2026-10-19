"""Run one reserved task using a container copy of the native source."""
from __future__ import annotations
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from aisbench_configuration import PEAK_NEW_DEFAULTS, build_plan, effective_environment, read_scenarios
from aisbench_logging import REDACTOR, timestamp, validate_output_path, private_binary
from aisbench_result_adapter import CANONICAL_COLUMNS, SUMMARY_COLUMNS, selected_measurement
from aisbench_artifact_archive import write_archive_manifest, path_is_relative_to

AUTOMATION_ROOT = Path(__file__).resolve().parent
RELEASE_ROOT = AUTOMATION_ROOT.parent.parent
CONTAINER_OUTPUT = '/workspace/aisbench_outputs'
CONTAINER_NATIVE = '/workspace/runtime/native'
CONTAINER_AUTOMATION = '/workspace/runtime/automation'
MANAGED_LABEL = 'io.aisbench-auto-tools-prefix.managed'
NATIVE_ROOTS = [RELEASE_ROOT / 'aisbench_auto_tools_prefix']


def atomic_json(path: Path, value: object) -> None:
    validate_output_path(path, NATIVE_ROOTS)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def command(args: list[str], *, input_bytes: bytes | None = None, timeout: float = 60):
    return subprocess.run(args, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def console_message(message, stream=None):
    """Console loss must not discard a task whose durable journal is healthy."""
    stream = sys.stdout if stream is None else stream
    try:
        print(message, file=stream, flush=True)
    except (OSError, UnicodeError):
        # A killed manager closes the pipe. Redirect that descriptor as well:
        # otherwise interpreter shutdown can retry the failed flush and turn
        # a completed worker into exit 120. Persistent log failures still raise
        # from Worker.log before reaching this optional console copy.
        try:
            with open(os.devnull, 'wb') as sink:
                os.dup2(sink.fileno(), stream.fileno())
        except (OSError, ValueError, AttributeError):
            pass


def assert_container_identity(docker: list[str], container_id: str, expected_name: str) -> dict:
    fixed = command([*docker, 'inspect', container_id], timeout=20)
    named = command([*docker, 'inspect', '--format', '{{.Id}}', expected_name], timeout=20)
    if fixed.returncode or named.returncode:
        raise RuntimeError('原固定ID工具容器不可查询，停止本任务；请检查Docker及容器状态')
    details = json.loads(fixed.stdout)[0]
    if (details.get('Id') != container_id or named.stdout.decode().strip() != container_id
            or not details.get('State', {}).get('Running')
            or (details.get('Config', {}).get('Labels') or {}).get(MANAGED_LABEL) != 'true'):
        raise RuntimeError('工具容器身份或运行状态变化，当前轮次无效，停止本任务')
    return details


def preflight(values: dict[str, str], plan: dict, config: Path) -> dict:
    for key in ('MODEL_HOST_PATH', 'TOOLS_HOST_DIR'):
        if not Path(values[key]).is_dir():
            raise ValueError(f'{key}目录不存在')
    if not (Path(values['TOOLS_HOST_DIR']) / 'aisbench_test.py').is_file():
        raise ValueError('TOOLS_HOST_DIR应指向包含aisbench_test.py的原生源码目录')
    validate_output_path(values['HOST_OUTPUT_DIR'], [RELEASE_ROOT / 'aisbench_auto_tools_prefix', values['TOOLS_HOST_DIR']])
    return dict(container_name=values['CONTAINER_NAME'], host_output_dir=values['HOST_OUTPUT_DIR'],
                model_host_path=values['MODEL_HOST_PATH'], tools_host_dir=values['TOOLS_HOST_DIR'],
                expected_image=values['EXPECTED_IMAGE'], host_ip=values['HOST_IP'], host_port=values['HOST_PORT'],
                model_name=values['MODEL_NAME'], api_url=values['API_URL'], service_id=values['SERVICE_ID'],
                planned_tests=len(plan['planned_scenarios']), fixed_count=len(plan['fixed']), peak_count=len(plan['peak']),
                planned_scenarios=plan['planned_scenarios'], effective_config={**values,
                'scenarios': read_scenarios(config), 'expanded_fixed': [s['name'] for s in plan['fixed']],
                'expanded_peak': [s['name'] for s in plan['peak']]})


class Worker:
    def __init__(self, values: dict[str, str], plan: dict):
        self.values, self.plan, self.docker = values, plan, ['docker']
        self.container_id = ''
        self.run_id = os.environ.get('AISBENCH_RUN_TAG') or time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:12]
        self.output = validate_output_path(values['HOST_OUTPUT_DIR'], [RELEASE_ROOT / 'aisbench_auto_tools_prefix', values['TOOLS_HOST_DIR']])
        self.result = self.output / 'scenario_results' / self.run_id
        validate_output_path(self.result, NATIVE_ROOTS)
        self.result.mkdir(parents=True, exist_ok=False)
        self.phase, self.current, self.index, self.sequence = 'initializing', '', 0, 0
        self.rows, self.scopes = [], []
        self.task_log = Path(os.environ.get('AISBENCH_TASK_LOG') or self.result / 'execution.log')
        validate_output_path(self.task_log, [RELEASE_ROOT / 'aisbench_auto_tools_prefix', values['TOOLS_HOST_DIR']])
        self.human = private_binary(self.task_log, NATIVE_ROOTS)
        self.events = private_binary(self.result / 'events.jsonl', NATIVE_ROOTS)
        REDACTOR.register(values['API_KEY'])
        self.runtime(False)
        self.save_rows()

    def runtime(self, complete: bool, exit_code=None, logging_error=''):
        target = os.environ.get('AISBENCH_RUNTIME_REPORT')
        if target:
            payload = dict(container_id=self.container_id, container_name=self.values['CONTAINER_NAME'],
                           image=self.values['EXPECTED_IMAGE'], run_id=self.run_id, result_dir=str(self.result),
                           complete=complete, phase=self.phase, current_scenario=self.current,
                           planned_tests=len(self.plan['planned_scenarios']), test_index=self.index,
                           result_scopes=str(self.result / 'result_scopes.json'),
                           planned_scenarios=self.plan['planned_scenarios'])
            if exit_code is not None:
                payload['worker_exit'] = exit_code
            if logging_error:
                payload['logging_error'] = logging_error
            atomic_json(Path(target), payload)

    def log(self, event, message, *, level='INFO', **fields):
        self.sequence += 1
        now, safe = timestamp(), REDACTOR.text(message)
        payload = dict(schema_version=1, seq=self.sequence, time=now, level=level, event=event,
                       message=safe, phase=self.phase, batch_id=os.environ.get('AISBENCH_BATCH_ID', ''),
                       task=os.environ.get('AISBENCH_TASK_NAME', ''), model=self.values['MODEL_NAME'],
                       scenario=self.current, **REDACTOR.value(fields))
        try:
            self.human.write(f'{now} {level} [{self.current or "initializing"}] [{event}] {safe}\n'.encode())
            self.events.write((json.dumps(payload, ensure_ascii=False, allow_nan=False) + '\n').encode())
            for stream in (self.human, self.events):
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            self.runtime(False, logging_error='任务持久日志写入失败')
            raise
        console_message(f'{now} {level} [{event}] {safe}')

    def checked(self, args, *, input_bytes=None, timeout=60):
        result = command([*self.docker, *args], input_bytes=input_bytes, timeout=timeout)
        if result.returncode:
            detail = REDACTOR.text(result.stderr.decode('utf-8', errors='replace').strip())
            raise RuntimeError(f'Docker操作失败（exit={result.returncode}）：{detail}')
        return result.stdout

    def assert_container_ready(self):
        return assert_container_identity(self.docker, self.container_id, self.values['CONTAINER_NAME'])

    def prepare_container(self):
        self.phase = 'container'
        self.runtime(False)
        lookup = command([*self.docker, 'inspect', self.values['CONTAINER_NAME']], timeout=20)
        existing = json.loads(lookup.stdout)[0] if lookup.returncode == 0 else None
        if existing:
            self.container_id = existing['Id']
            cfg = existing.get('Config', {})
            known_image = cfg.get('Image') == self.values['EXPECTED_IMAGE'] or str(cfg.get('Image', '')).startswith('ghcr.io/aisbench/aisbench_benchmark')
            if (cfg.get('Labels') or {}).get(MANAGED_LABEL) != 'true' or not known_image:
                self.container_id = ''
                raise RuntimeError('容器同名冲突：无法确认属于本工具，拒绝修改')
            mounts = {m.get('Destination'): m.get('Source') for m in existing.get('Mounts', [])}
            if (cfg.get('Image') != self.values['EXPECTED_IMAGE'] or mounts.get(CONTAINER_OUTPUT) != str(self.output)
                    or mounts.get('/workspace/model') != self.values['MODEL_HOST_PATH']):
                self.log('container_recreate', '仅重建已确认管理标签和固定ID的工具容器', container_id=self.container_id)
                self.checked(['rm', '-f', self.container_id])
                existing, self.container_id = None, ''
        if not existing:
            self.container_id = self.checked(['run', '-d', '--name', self.values['CONTAINER_NAME'], '--label',
                MANAGED_LABEL + '=true', '--ipc=host', '--net=host', '--privileged', '--shm-size=1g',
                '-v', str(self.output) + ':' + CONTAINER_OUTPUT,
                '-v', self.values['MODEL_HOST_PATH'] + ':/workspace/model:ro', self.values['EXPECTED_IMAGE'],
                'bash', '-lc', 'trap : TERM INT; while true; do sleep 3600; done']).decode().strip()
        elif not existing.get('State', {}).get('Running'):
            self.checked(['start', self.container_id])
        self.runtime(False)
        details = self.assert_container_ready()
        self.log('container_ready', '专用工具容器已就绪', container_id=self.container_id, image_id=details.get('Image'))
        self.checked(['exec', self.container_id, 'mkdir', '-p', CONTAINER_NATIVE, CONTAINER_AUTOMATION, '/workspace/aisbench_dataset'])
        self.copy_sources()
        self.write_runtime_config()

    def copy_sources(self):
        self.phase = 'source_copy'
        self.runtime(False)
        validate_output_path(self.output / '.source-tmp', NATIVE_ROOTS)
        with tempfile.TemporaryDirectory(prefix='.source-tmp-', dir=self.output) as directory:
            archive = Path(directory) / 'runtime.tar'
            with tarfile.open(archive, 'w') as output:
                native = Path(self.values['TOOLS_HOST_DIR'])
                for path in sorted(native.rglob('*')):
                    relative = path.relative_to(native)
                    if any(p in {'outputs', '.git', '__pycache__', 'add', 'automation'} for p in relative.parts):
                        continue
                    if path.is_symlink():
                        raise ValueError('原生源码不能包含未审查软链接')
                    if path.is_file() and not path.name.endswith(('.log', '.local.env', '.local.json')):
                        output.add(path, arcname='native/' + relative.as_posix(), recursive=False)
                for path in sorted(AUTOMATION_ROOT.glob('*.py')):
                    if not path.name.startswith(('test_', 'validate_')):
                        output.add(path, arcname='automation/' + path.name, recursive=False)
            self.checked(['cp', str(archive), self.container_id + ':/workspace/runtime/source.tar'])
            self.checked(['exec', self.container_id, 'tar', '-xf', '/workspace/runtime/source.tar', '-C', '/workspace/runtime'])
            self.checked(['exec', self.container_id, 'rm', '/workspace/runtime/source.tar'])
        self.log('source_copied', '源码已复制到容器运行副本，宿主机原生目录仅供读取')

    def write_runtime_config(self):
        self.work_path = self.checked(['exec', self.container_id, 'python3', '-c',
            'import importlib.metadata as m,json;d=m.distribution("ais-bench-benchmark");u=d.read_text("direct_url.json");'
            'print(json.loads(u)["url"].removeprefix("file://") if u else str(d.locate_file("")))']).decode().strip()
        cfg = dict(DATASET_PATH='/workspace/aisbench_dataset', WORK_PATH=self.work_path,
                   MODEL_NAME=self.values['MODEL_NAME'], MODEL_PATH='/workspace/model', HOST_IP=self.values['HOST_IP'],
                   HOST_PORT=self.values['HOST_PORT'], DEFAULT_PERFORMANCE_TEST='default_perf', OUTPUT_DIR=CONTAINER_OUTPUT,
                   POD_INFO=[s.strip() for s in self.values['POD_INFO'].split(',') if s.strip()],
                   API_KEY=self.values['API_KEY'], URL=self.values['API_URL'])
        program = ('import json,pathlib,re\n' + f'p=pathlib.Path({CONTAINER_NATIVE!r})/"config.py"\n'
                   + f'values=json.loads({json.dumps(cfg, ensure_ascii=False)!r})\n'
                   + 'text=p.read_text(encoding="utf-8")\nfor key,value in values.items():\n'
                   + ' replacement=key+" = "+repr(value)\n'
                   + ' text,n=re.subn(r"^"+re.escape(key)+r"\\s*=.*$",lambda m:replacement,text,flags=re.M)\n'
                   + ' if not n:text+="\\n"+replacement+"\\n"\np.write_text(text,encoding="utf-8")\n')
        self.checked(['exec', '-i', self.container_id, 'python3', '-'], input_bytes=program.encode())
        self.log('runtime_config', '容器运行配置已生成，认证值不写入人类摘要', work_path=self.work_path)

    def host_path(self, value):
        prefix = CONTAINER_OUTPUT + '/'
        return self.output / value[len(prefix):] if value.startswith(prefix) else Path(value)

    def container_path(self, path):
        return CONTAINER_OUTPUT + '/' + path.relative_to(self.output).as_posix()

    def convert_paths(self, value):
        if isinstance(value, dict):
            return {key: self.convert_paths(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.convert_paths(item) for item in value]
        return str(self.host_path(value)) if isinstance(value, str) and value.startswith(CONTAINER_OUTPUT + '/') else value

    def run_container(self, args, timeout, on_progress=None):
        self.assert_container_ready()
        logs = self.result / 'process_logs' / ('call_' + uuid.uuid4().hex)
        validate_output_path(logs, NATIVE_ROOTS)
        logs.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.log('container_call_start', '启动容器内工具；调用原始输出保存到独立日志',
                 stdout_log=str(logs / 'stdout.raw.log'), stderr_log=str(logs / 'stderr.raw.log'))
        # Never let docker exec inherit the manager's console pipe: if only
        # the manager is killed, the completed benchmark must still be able
        # to emit its final summary and return its true exit status.
        with private_binary(logs / 'stdout.raw.log', NATIVE_ROOTS) as stdout, \
                private_binary(logs / 'stderr.raw.log', NATIVE_ROOTS) as stderr:
            process = subprocess.Popen([*self.docker, 'exec', '-e', 'PYTHONUNBUFFERED=1', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                '-e', 'AISBENCH_NATIVE_ROOT=' + CONTAINER_NATIVE, '-e', 'PYTHONPATH=' + self.work_path,
                self.container_id, *args], stdout=stdout, stderr=stderr)
            deadline = time.monotonic() + timeout
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(process.args, timeout)
                    try:
                        result = process.wait(timeout=min(1, remaining))
                    except subprocess.TimeoutExpired:
                        if on_progress:
                            on_progress()
                        continue
                    if on_progress:
                        on_progress()
                    return result
            except subprocess.TimeoutExpired:
                self.log('trial_timeout', '调用超过预算，停止固定ID工具容器，结束本任务', level='ERROR')
                self.checked(['stop', '--time', '3', self.container_id], timeout=20)
                process.wait(timeout=15)
                raise RuntimeError('工具调用超时，不将其视为SLO失败')
            except BaseException:
                # A logging/publication error must also stop this task's requests.
                if process.poll() is None:
                    self.checked(['stop', '--time', '3', self.container_id], timeout=20)
                    process.wait(timeout=15)
                raise
            finally:
                for stream in (stdout, stderr):
                    stream.flush()
                    os.fsync(stream.fileno())

    def save_rows(self):
        buffer = io.StringIO(newline='')
        writer = csv.DictWriter(buffer, fieldnames=SUMMARY_COLUMNS, lineterminator='\n')
        writer.writeheader()
        writer.writerows(self.rows)
        content = buffer.getvalue().encode()
        path = self.result / 'all_results.csv'
        validate_output_path(path, NATIVE_ROOTS)
        temporary = path.with_suffix('.csv.tmp')
        with temporary.open('wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        atomic_json(self.result / 'result_scopes.json', dict(schema_version=1, rows=self.scopes,
            output_root=str(self.output), csv_sha256=hashlib.sha256(content).hexdigest()))

    def append_row(self, scene, raw, status, *, evaluation_file='', scope=None, **metadata):
        values = {name: str(raw.get(name, '')) for name in CANONICAL_COLUMNS}
        repeat = raw.get('repeat_rate', scene.get('repeat_rate', '0'))
        if str(repeat).endswith('%'):
            repeat = str(float(str(repeat)[:-1]) / 100)
        row = dict(scenario=scene['name'], **values, repeat_rate=str(repeat), status=status)
        self.scopes.append(dict(row_index=len(self.rows), scenario=row['scenario'], current_time=row['current_time'],
            status=status, evaluation_file=evaluation_file, scope=scope or {},
            selected_scope_id=(scope or {}).get('scope_id', ''), **metadata))
        self.rows.append(row)
        self.save_rows()

    def evaluation(self, file):
        if not path_is_relative_to(file.resolve(), self.output.resolve()):
            raise ValueError('测量证据不属于本任务输出目录')
        if not file.is_file():
            return {}, {}
        report = self.convert_paths(json.loads(file.read_text(encoding='utf-8')))
        scope, row, reason = selected_measurement(report, self.output)
        if scope:
            report['raw_row'] = row
        else:
            if report.get('selected_scope_id'):
                report['reported_selected_scope_id'] = report['selected_scope_id']
            report.update(selected_scope_id='', raw_row={}, selection_reason=reason)
            if report.get('state') in {'PASS', 'SLO_FAIL', 'UNSTABLE'}:
                report.update(state='INVALID', reason='selected measurement unavailable: ' + reason)
        atomic_json(file, report)
        return report, scope

    def fixed(self, scene):
        self.current, self.phase = scene['name'], 'fixed'
        self.runtime(False)
        evidence = self.result / ('fixed_' + str(self.index))
        validate_output_path(evidence, NATIVE_ROOTS)
        self.log('fixed_start', f"固定测试：配置并发={scene['concurrency']}，每主测请求={scene['data_num']}，输出配置={scene['output_len']}",
            request_rate=scene['request_rate'], test_type=scene['test_type'], accuracy=scene['accuracy'])
        rc = self.run_container(['python3', CONTAINER_AUTOMATION + '/aisbench_execution.py', '--native-root', CONTAINER_NATIVE,
            '--evidence-dir', self.container_path(evidence), '--timeout', self.values['PEAK_TRIAL_TIMEOUT'],
            '--failed-req-limit', self.values['FAILED_REQ_LIMIT'], '--', *scene['args']], float(self.values['PEAK_TRIAL_TIMEOUT']) + 60)
        # execute_native owns exclusive creation. Docker may fail before it starts.
        if not evidence.exists():
            evidence.mkdir(exist_ok=False)
        file = evidence / 'evaluation.json'
        report, scope = self.evaluation(file)
        passed = rc == 0 and report.get('state') == 'PASS' and bool(scope)
        raw = dict(report.get('raw_row') or {}) if passed else {}
        self.append_row(scene, raw, 'passed' if passed else 'failed', evaluation_file=str(file), scope=scope,
                        test_type=scene['test_type'], test_accuracy=scene['accuracy'],
                        measurement_state=report.get('state', 'ERROR'))
        write_archive_manifest(evidence, task_kind='performance', archive_name=scene['name'],
            run_tag=self.run_id, output_root=self.output, source_root=evidence / 'native_runtime',
            extra_metadata={'test_type': scene['test_type'], 'test_accuracy': scene['accuracy'],
                            'selected_scope_id': scope.get('scope_id', ''), 'evaluation_file': str(file)})
        self.log('fixed_end', '固定测试通过' if passed else '固定测试失败，完整原因见evaluation.json',
            level='INFO' if passed else 'ERROR', return_code=rc, state=report.get('state', 'missing'),
            reason=report.get('reason', ''), evidence=str(file), actual_concurrency=raw.get('cc', ''))
        return passed

    def peak(self, scene):
        self.current, self.phase = scene['name'], 'peak'
        self.runtime(False)
        tag = self.run_id + '_' + str(self.index)
        self.log('peak_start', f"摸高：初始{scene['min_cc']}..{scene['max_cc']}；TTFT≤{scene['ttft']}s，TPOT≤{scene['tpot']}ms；硬顶={self.values['PEAK_HARD_CAP']}")
        args = ['python3', CONTAINER_AUTOMATION + '/aisbench_perf_matrix.py', '--config-file', CONTAINER_AUTOMATION + '/aisbench_perf_matrix_config.py',
            '--run-tag', tag, '--case-name', scene['name'], '--input-len', str(scene['input_len']), '--output-len', str(scene['output_len']),
            '--min-concurrency', str(scene['min_cc']), '--max-concurrency', str(scene['max_cc']), '--request-multiplier', str(scene['multiplier']),
            '--request-rate', scene['request_rate'], '--ttft-limit', str(scene['ttft']), '--tpot-limit', str(scene['tpot']),
            '--wait-seconds', str(scene['wait']), '--failed-req-limit', self.values['FAILED_REQ_LIMIT'],
            '--npu_num', self.values['NPU_NUM'], '--repeat-rate', scene['repeat_rate'], *scene['extra_args']]
        for _, (_, field, option) in PEAK_NEW_DEFAULTS.items():
            args.extend(['--' + option, str(scene[field])])
        if self.values['PEAK_HARD_CAP'] == '1':
            args.extend(['--max-concurrency', str(scene['max_cc']), '--auto-expand-max-concurrency', str(scene['max_cc'])])
        seen = set()

        def publish_rounds():
            paths = list(self.output.glob('peak_*_' + tag + '/peak_search_report.json'))
            if len(paths) != 1:
                return
            try:
                current = self.convert_paths(json.loads(paths[0].read_text(encoding='utf-8')))
            except (FileNotFoundError, json.JSONDecodeError):
                return  # A report write may be in progress; retry the next tick.
            for trial in current.get('all', []):
                identity = trial.get('trial_id') or trial.get('evaluation_file')
                if not identity or identity in seen:
                    continue
                if not trial.get('evaluation_file'):
                    raise RuntimeError('摸高轮次缺少evaluation_file，无法核验测量来源')
                file = Path(trial['evaluation_file'])
                _, scope = self.evaluation(file)
                self.append_row(scene, trial, 'peak_round', evaluation_file=str(file), scope=scope,
                    measurement_state=trial.get('measurement_state', trial.get('state', '')))
                seen.add(identity)
                self.log('peak_round', f"摸高轮次完成：配置并发={trial.get('concurrency')}，测量={trial.get('measurement_state', trial.get('state', ''))}",
                    evidence=str(file))

        rc = self.run_container(args, float(scene['peak_total_timeout']) + 90, on_progress=publish_rounds)
        paths = list(self.output.glob('peak_*_' + tag + '/peak_search_report.json'))
        if len(paths) != 1:
            self.append_row(scene, {}, 'peak_error', termination_reason='report_missing', measurement_state='ERROR')
            self.log('peak_error', '本轮报告缺失或身份不唯一，不读取历史报告', level='ERROR')
            return False
        path = paths[0]
        report = self.convert_paths(json.loads(path.read_text(encoding='utf-8')))
        atomic_json(path, report)
        publish_rounds()
        if report.get('complete') is not True or not (path.parent / 'archive_manifest.json').is_file():
            self.append_row(scene, {}, 'peak_error', measurement_state='ERROR', termination_reason='incomplete_report')
            self.log('peak_error', '摸高进程未完成最终报告和归档，不能将临时最佳轮次作为结论', level='ERROR',
                return_code=rc, evidence=str(path))
            write_archive_manifest(path.parent, task_kind='peak-search-incomplete', archive_name=scene['name'],
                run_tag=tag, output_root=self.output, extra_metadata={'termination': 'incomplete_report'})
            return False
        successful = rc == 0 and bool(report.get('best'))
        for row in report.get('best') or [{}]:
            status = row.get('status', 'peak_error')
            successful = successful and status in {'peak_confirmed', 'peak_capped'}
            reference = row.get('evaluation_file', '')
            _, scope = self.evaluation(Path(reference)) if reference else ({}, {})
            self.append_row(scene, row, status, evaluation_file=reference, scope=scope, report_file=str(path),
                selected_trial_id=row.get('selected_trial_id', ''), termination_reason=row.get('termination', ''))
            self.log('peak_end', f"摸高结束：{status}；配置并发={row.get('concurrency', '未确认')}；实测平均并发={row.get('cc', '无')}；原因={row.get('reason', '')}",
                level='INFO' if status in {'peak_confirmed', 'peak_capped'} else 'ERROR', evidence=str(path))
        # Path conversion changes JSON bytes after the container's final seal.
        # Seal the completed host evidence only after every conversion is done.
        previous = json.loads((path.parent / 'archive_manifest.json').read_text(encoding='utf-8'))
        write_archive_manifest(path.parent, task_kind='peak-search', archive_name=previous['archive_name'],
            run_tag=previous.get('run_tag', tag), output_root=self.output,
            source_root=Path(previous['source_root']) if previous.get('source_root') else None,
            captured_outputs_dir=previous.get('captured_outputs_dir', ''),
            extra_metadata=self.convert_paths(previous.get('extra_metadata', {})))
        return successful

    def run(self):
        self.log('task_start', '任务开始，配置已冻结，原生源目录不会被写入', planned_tests=len(self.plan['planned_scenarios']))
        self.prepare_container()
        passed = True
        for kind in ('fixed', 'peak'):
            for scene in self.plan[kind]:
                self.index += 1
                good = self.fixed(scene) if kind == 'fixed' else self.peak(scene)
                passed = good and passed
                self.assert_container_ready()
        self.phase, self.current = 'finished', ''
        rc = 0 if passed else 1
        self.log('task_end', '全部场景通过' if passed else '存在失败或未确认场景，请按状态和证据检查',
            level='INFO' if passed else 'ERROR', exit_code=rc, result_csv=str(self.result / 'all_results.csv'))
        for stream in (self.human, self.events):
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
        if self.task_log != self.result / 'execution.log':
            validate_output_path(self.result / 'execution.log', NATIVE_ROOTS)
            shutil.copyfile(self.task_log, self.result / 'execution.log')
        atomic_json(self.output / 'scenario_results' / 'latest.json', dict(run_id=self.run_id, result_dir=str(self.result), exit_code=rc))
        if os.name == 'posix':
            latest = self.output / 'scenario_results' / 'latest'
            validate_output_path(latest, NATIVE_ROOTS)
            temporary = latest.with_name('latest.' + uuid.uuid4().hex)
            temporary.symlink_to(self.result.name, target_is_directory=True)
            os.replace(temporary, latest)
        self.runtime(True, rc)
        return rc


def main():
    worker = None
    try:
        config = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else RELEASE_ROOT / 'env.env'
        if 'SCENARIOS_JSON_OVERRIDE' in os.environ:
            raise ValueError('SCENARIOS_JSON_OVERRIDE已停用，请把场景写入所用env.env配置文件并unset旧变量')
        os.environ.pop('SCENARIOS_JSON', None)
        values = effective_environment(dict(os.environ), RELEASE_ROOT)
        NATIVE_ROOTS.append(Path(values['TOOLS_HOST_DIR']))
        values['CONTAINER_NAME'] = values['CONTAINER_NAME'] or 'ais_bench-example'
        plan = build_plan(values, config)
        report = preflight(values, plan, config)
        if os.environ.get('AISBENCH_PREFLIGHT_REPORT'):
            atomic_json(Path(os.environ['AISBENCH_PREFLIGHT_REPORT']), report)
            return 0
        worker = Worker(values, plan)
        return worker.run()
    except KeyboardInterrupt:
        if worker:
            worker.runtime(False, 130)
        return 130
    except Exception as error:
        message = REDACTOR.text(str(error))
        if worker:
            try:
                worker.log('task_error', message, level='ERROR')
                worker.runtime(False, 1)
            except (OSError, ValueError):
                worker.runtime(False, 1, logging_error='任务日志或结果写入失败')
        console_message('ERROR: ' + message, stream=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
