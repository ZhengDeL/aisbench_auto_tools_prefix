"""Run one reserved task using a container copy of the native source."""
from __future__ import annotations
import csv
from datetime import datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
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


class FileConsoleMirror:
    """Observe durable files without putting console backpressure in supervision.

    Docker owns the original files. Each optional daemon observer owns one
    reader and a console target bound at start, so a blocked stdout cannot stop
    stderr, the process deadline, or raw capture. Closing waits only briefly;
    an incomplete console copy is recorded instead of holding the task open.
    """
    CLOSE_TIMEOUT = 0.5

    def __init__(self, directory):
        self.directory = Path(directory)
        self.readers = {}
        self.disabled = set()
        self._threads, self._targets, self._owned_targets = {}, {}, {}
        self._wake = {name: threading.Event() for name in ('stdout', 'stderr')}
        self._closing, self._stop = threading.Event(), threading.Event()
        self._lock = threading.Lock()
        self._failures, self._finalized = [], False
        try:
            for channel in ('stdout', 'stderr'):
                self.readers[channel] = (self.directory / (channel + '.raw.log')).open('rb')
        except BaseException:
            self.close()
            raise

    def _save_degradation(self):
        # Called with _lock held. After close finalizes this snapshot, a late
        # observer must not change evidence that may already be archived.
        if self._failures:
            try:
                atomic_json(self.directory / 'console_forwarding.json', dict(
                    schema_version=1, state='degraded', raw_capture_enabled=True,
                    mirror_complete=False, failures=list(self._failures)))
            except (OSError, ValueError):
                # Optional display diagnostics must not override the mandatory
                # raw-file errors checked by run_container itself.
                pass

    def _disable(self, channel, error, reason='console output unavailable'):
        with self._lock:
            if channel not in self.disabled:
                self.disabled.add(channel)
                self._failures.append(dict(stream=channel, reason=reason,
                    error_type=type(error).__name__, errno=getattr(error, 'errno', None)))
            if not self._finalized:
                self._save_degradation()

    def _write(self, channel, chunk):
        if channel in self.disabled or self._stop.is_set():
            return
        target = self._targets.get(channel)
        if target is None:
            stream = sys.stdout if channel == 'stdout' else sys.stderr
            binary = getattr(stream, 'buffer', None)
            target = (binary, stream)
        binary, stream = target
        try:
            if binary is not None:
                pending = memoryview(chunk)
                while pending and not self._stop.is_set():
                    try:
                        # A bounded write also bounds the already in-flight
                        # tail if close has to abandon an unresponsive terminal.
                        written = binary.write(pending[:4096])
                    except InterruptedError:
                        continue
                    if written is None or written <= 0:
                        raise OSError('console mirror write made no progress')
                    pending = pending[written:]
                if not self._stop.is_set():
                    binary.flush()
            else:
                stream.write(chunk.decode('utf-8', errors='replace'))
                if not self._stop.is_set():
                    stream.flush()
        except (OSError, ValueError, UnicodeError) as error:
            self._disable(channel, error)

    def _drain_channel(self, channel, final=False):
        reader = self.readers.get(channel)
        if reader is None:
            return False
        remaining, observed = (None if final else 1024 * 1024), False
        while not self._stop.is_set() and (remaining is None or remaining > 0):
            chunk = reader.read(65536 if remaining is None else min(65536, remaining))
            if not chunk:
                break
            observed = True
            if remaining is not None:
                remaining -= len(chunk)
            self._write(channel, chunk)
        return observed

    def drain(self, final=False):
        """Synchronous cursor helper; running observers are never joined here."""
        if self._threads:
            for wake in self._wake.values():
                wake.set()
            return
        for channel in tuple(self.readers):
            self._drain_channel(channel, final)

    def _follow(self, channel):
        try:
            while not self._stop.is_set():
                observed = self._drain_channel(channel)
                if self._closing.is_set() and not observed:
                    break
                if not observed:
                    self._wake[channel].wait(0.05)
                    self._wake[channel].clear()
        except (OSError, ValueError) as error:
            self._disable(channel, error, 'console observer could not read raw log')
        finally:
            # The reader and duplicated console descriptor belong exclusively
            # to this thread. Main-thread close never races a blocked write.
            reader = self.readers.pop(channel, None)
            if reader is not None:
                reader.close()
            owned = self._owned_targets.pop(channel, None)
            if owned is not None:
                owned.close()

    def start(self):
        if self._threads:
            return
        if self._closing.is_set():
            raise RuntimeError('console mirror is already closed')
        for channel in tuple(self.readers):
            stream = sys.stdout if channel == 'stdout' else sys.stderr
            binary = getattr(stream, 'buffer', None)
            if binary is not None:
                try:
                    # Bind the original pipe, not the mutable global fd number.
                    # A late observer cannot write into a later fd replacement.
                    binary = os.fdopen(os.dup(binary.fileno()), 'wb', buffering=0)
                    self._owned_targets[channel] = binary
                except (OSError, ValueError, AttributeError):
                    pass
            self._targets[channel] = (binary, stream)
            thread = threading.Thread(target=self._follow, args=(channel,), daemon=True,
                                      name='aisbench-console-' + channel)
            self._threads[channel] = thread
            thread.start()

    def close(self):
        if self._finalized:
            return
        self._closing.set()
        for wake in self._wake.values():
            wake.set()
        deadline = time.monotonic() + self.CLOSE_TIMEOUT
        for thread in self._threads.values():
            thread.join(max(0, deadline - time.monotonic()))
        alive = [name for name, thread in self._threads.items() if thread.is_alive()]
        if alive:
            self._stop.set()
            for channel in alive:
                self._disable(channel, TimeoutError('console observer drain deadline exceeded'),
                              'terminal backpressure; raw log preserved, console copy may be incomplete')
        if not self._threads:
            for reader in self.readers.values():
                reader.close()
            self.readers.clear()
        with self._lock:
            self._save_degradation()
            self._finalized = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


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
        self.current_kind, self.current_scene = '', {}
        self.scenario_outcomes = []
        # Managed workers publish control events through their durable journal.
        # stdout/stderr remain the independent native-detail transport.
        self.managed = all(os.environ.get(name) for name in (
            'AISBENCH_BATCH_ID', 'AISBENCH_TASK_NAME', 'AISBENCH_RUNTIME_REPORT'))
        self.rows, self.scopes = [], []
        self.task_log = Path(os.environ.get('AISBENCH_TASK_LOG') or self.result / 'execution.log')
        validate_output_path(self.task_log, [RELEASE_ROOT / 'aisbench_auto_tools_prefix', values['TOOLS_HOST_DIR']])
        self.human = private_binary(self.task_log, NATIVE_ROOTS)
        self.events_file = self.result / 'events.jsonl'
        self.events = private_binary(self.events_file, NATIVE_ROOTS)
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
                           planned_scenarios=self.plan['planned_scenarios'],
                           events_file=str(self.events_file),
                           outcome_counts=self.outcome_counts(),
                           scenario_outcomes=REDACTOR.value(self.scenario_outcomes),
                           non_success_scenarios=REDACTOR.value([
                               item for item in self.scenario_outcomes if not item['passed']]))
            if getattr(self, 'task_error', None):
                payload['task_error'] = REDACTOR.value(self.task_error)
            if exit_code is not None:
                payload['worker_exit'] = exit_code
            if logging_error:
                payload['logging_error'] = logging_error
            atomic_json(Path(target), payload)

    def scenario_context(self):
        """Describe the configured workload without substituting measurements."""
        scene = getattr(self, 'current_scene', {})
        context = dict(test_index=getattr(self, 'index', 0),
                       planned_tests=len(getattr(self, 'plan', {}).get('planned_scenarios', [])),
                       run_kind=getattr(self, 'current_kind', ''))
        for source, target in (('input_len', 'configured_input_len'),
                               ('output_len', 'configured_output_len'),
                               ('request_rate', 'request_rate'),
                               ('test_type', 'test_type'), ('accuracy', 'accuracy')):
            if source in scene:
                context[target] = scene[source]
        if context['run_kind'] == 'fixed':
            context.update(configured_concurrency=scene.get('concurrency'),
                           configured_requests=scene.get('data_num'))
        elif context['run_kind'] == 'peak':
            tolerance = scene.get('slo_tolerance', 0)
            context.update(initial_concurrency_range=[scene.get('min_cc'), scene.get('max_cc')],
                           request_multiplier=scene.get('multiplier'),
                           ttft_limit=scene.get('ttft'), tpot_limit=scene.get('tpot'),
                           slo_tolerance=tolerance, hard_cap=self.values['PEAK_HARD_CAP'])
        return context

    def outcome_counts(self):
        """Count terminal scene outcomes; a fixed PASS is not an SLO claim."""
        outcomes = getattr(self, 'scenario_outcomes', [])
        counts = dict(planned=len(self.plan['planned_scenarios']), completed=len(outcomes),
                      passed=sum(item['passed'] for item in outcomes),
                      non_success=sum(not item['passed'] for item in outcomes),
                      fixed_passed=sum(item['kind'] == 'fixed' and item['passed'] for item in outcomes),
                      fixed_failed=sum(item['kind'] == 'fixed' and not item['passed'] for item in outcomes))
        for status in ('peak', 'peak_confirmed', 'peak_capped', 'peak_budget', 'peak_unstable',
                       'peak_error', 'peak_request_error', 'peak_no_pass', 'peak_cancelled',
                       'peak_mixed', 'execution_error', 'cancelled'):
            counts[status] = sum(item['status'] == status for item in outcomes)
        return counts

    def record_outcome(self, status, passed, reason, *, evidence='', selected_concurrency=None,
                       actual_concurrency='', return_code=None, **fields):
        """Retain one final outcome per expanded scene, separate from CSV rows."""
        outcome = dict(test_index=self.index, scenario=self.current, kind=self.current_kind,
                       status=status, passed=bool(passed), reason=str(reason), evidence=str(evidence),
                       selected_concurrency=selected_concurrency, actual_concurrency=actual_concurrency,
                       return_code=return_code, **fields)
        safe = REDACTOR.value(outcome)
        self.scenario_outcomes = [item for item in self.scenario_outcomes
                                  if item['test_index'] != self.index] + [safe]
        self.runtime(False)

    def record_interrupted_scene(self, status, reason):
        """Explain an unfinished scene without manufacturing a measurement row."""
        if (not self.current or not self.current_kind
                or any(item['test_index'] == self.index for item in self.scenario_outcomes)):
            return
        evidence = getattr(self, 'current_evidence', self.result)
        if not Path(evidence).exists():
            evidence = self.result
        self.record_outcome(status, False, reason, evidence=evidence)

    def log(self, event, message, *, level='INFO', **fields):
        self.sequence += 1
        now, safe = timestamp(), REDACTOR.text(message)
        scope = ('task' if event.startswith('task_') else 'trial' if event == 'peak_round'
                 else 'scenario' if self.current else 'task')
        payload = dict(schema_version=1, seq=self.sequence, time=now, level=level, event=event,
                       message=safe, phase=self.phase, batch_id=os.environ.get('AISBENCH_BATCH_ID', ''),
                       task=os.environ.get('AISBENCH_TASK_NAME', ''), model=self.values['MODEL_NAME'],
                       scenario=self.current, run_id=getattr(self, 'run_id', ''), scope=scope)
        payload.update(self.scenario_context())
        payload.update(REDACTOR.value(fields))
        try:
            context = 'task' if scope == 'task' else (self.current or 'task')
            self.human.write(f'{now} {level} [{context}] [{event}] {safe}\n'.encode())
            self.events.write((json.dumps(payload, ensure_ascii=False, allow_nan=False) + '\n').encode())
            for stream in (self.human, self.events):
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            self.runtime(False, logging_error='任务持久日志写入失败')
            raise
        if not getattr(self, 'managed', False):
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
        self.log('container_call_start', f'启动容器内工具；原始stdout/stderr独立保存；详情目录：{logs}',
                 call_id=logs.name, stdout_log=str(logs / 'stdout.raw.log'),
                 stderr_log=str(logs / 'stderr.raw.log'))
        # Never let docker exec inherit the manager's console pipe: if only
        # the manager is killed, the completed benchmark must still be able
        # to emit its final summary and return its true exit status.
        with private_binary(logs / 'stdout.raw.log', NATIVE_ROOTS) as stdout, \
                private_binary(logs / 'stderr.raw.log', NATIVE_ROOTS) as stderr, \
                FileConsoleMirror(logs) as mirror:
            mirror.start()
            process = subprocess.Popen([*self.docker, 'exec', '-e', 'PYTHONUNBUFFERED=1', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                '-e', 'AISBENCH_NATIVE_ROOT=' + CONTAINER_NATIVE, '-e', 'PYTHONPATH=' + self.work_path,
                self.container_id, *args], stdout=stdout, stderr=stderr)
            deadline = time.monotonic() + timeout
            next_progress = time.monotonic()
            reported_console_failures = set()
            try:
                while True:
                    for channel in mirror.disabled - reported_console_failures:
                        self.log('console_mirror_degraded',
                                 f'{channel}终端连接不可用；原始输出继续独立写盘，压测不依赖终端连接',
                                 level='WARNING', channel=channel, raw_capture_enabled=True)
                        reported_console_failures.add(channel)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(process.args, timeout)
                    try:
                        result = process.wait(timeout=min(0.2, remaining))
                    except subprocess.TimeoutExpired:
                        if on_progress and time.monotonic() >= next_progress:
                            on_progress()
                            next_progress = time.monotonic() + 1
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
        # Canonical values describe the selected measurement, never the scene plan.
        measured = raw if scope else {}
        values = {name: str(measured.get(name, '')) for name in CANONICAL_COLUMNS}
        repeat = raw.get('repeat_rate', scene.get('repeat_rate', '0'))
        if str(repeat).endswith('%'):
            repeat = str(float(str(repeat)[:-1]) / 100)
        row = dict(scenario=scene['name'], **values, repeat_rate=str(repeat), status=status)
        self.scopes.append(dict(row_index=len(self.rows), scenario=row['scenario'], current_time=row['current_time'],
            status=status, evaluation_file=evaluation_file, scope=scope or {},
            selected_scope_id=(scope or {}).get('scope_id', ''),
            configured_input_len=scene.get('input_len', ''), configured_output_len=scene.get('output_len', ''),
            **metadata))
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
        self.current_kind, self.current_scene = 'fixed', scene
        self.runtime(False)
        evidence = self.result / ('fixed_' + str(self.index))
        self.current_evidence = evidence
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
        reason = report.get('reason') or report.get('selection_reason') or (
            '执行与数据完整性检查通过' if passed else '未取得可信测量报告或作用域')
        if rc != 0:
            reason = f'工具退出码={rc}；{reason}'
        self.record_outcome('passed' if passed else 'failed', passed, reason,
            evidence=str(file), actual_concurrency=raw.get('cc', ''), return_code=rc,
            configured_concurrency=scene['concurrency'], configured_requests=scene['data_num'],
            selected_scope_id=scope.get('scope_id', ''), measurement_state=report.get('state', 'missing'),
            slo_evaluated=False)
        fixed_message = (f"固定测试通过：执行与数据完整性检查通过；配置并发={scene['concurrency']}，"
                         f"实测平均并发={raw.get('cc') or '无适用测量'}；本轮未作摸高SLO判定" if passed else
                         f'固定测试失败：{reason}；证据={file}')
        self.log('fixed_end', fixed_message,
            level='INFO' if passed else 'ERROR', return_code=rc, state=report.get('state', 'missing'),
            status='passed' if passed else 'failed', passed=passed, reason=reason,
            evidence=str(file), actual_concurrency=raw.get('cc', ''),
            selected_scope_id=scope.get('scope_id', ''), slo_evaluated=False,
            measurements={key: raw.get(key, '') for key in (
                'total_req', 'input_len', 'output_len', 'cc', 'TTFT avg', 'TTFT P90',
                'TPOT avg', 'TPOT SLO_P90', 'E2E_time', 'output_throughput', 'qps')})
        return passed

    def peak(self, scene):
        self.current, self.phase = scene['name'], 'peak'
        self.current_kind, self.current_scene = 'peak', scene
        self.current_evidence = self.result
        self.runtime(False)
        tag = self.run_id + '_' + str(self.index)
        tolerance = float(scene.get('slo_tolerance', 0))
        self.log('peak_start', f"摸高：初始{scene['min_cc']}..{scene['max_cc']}，首轮取区间中点；"
                 f"基础阈值TTFT≤{scene['ttft']}s、TPOT≤{scene['tpot']}ms；"
                 f"SLO容差={tolerance:.1%}；硬顶={self.values['PEAK_HARD_CAP']}",
                 confirm_runs=scene.get('peak_confirm_runs'),
                 confirm_min_requests=scene.get('peak_confirm_min_requests'))
        args = ['python3', CONTAINER_AUTOMATION + '/aisbench_perf_matrix.py', '--config-file', CONTAINER_AUTOMATION + '/aisbench_perf_matrix_config.py',
            '--run-tag', tag, '--case-name', scene['name'], '--input-len', str(scene['input_len']), '--output-len', str(scene['output_len']),
            '--min-concurrency', str(scene['min_cc']), '--max-concurrency', str(scene['max_cc']), '--request-multiplier', str(scene['multiplier']),
            '--request-rate', scene['request_rate'], '--ttft-limit', str(scene['ttft']), '--tpot-limit', str(scene['tpot']),
            '--slo-tolerance', str(tolerance),
            '--wait-seconds', str(scene['wait']), '--failed-req-limit', self.values['FAILED_REQ_LIMIT'],
            '--npu_num', self.values['NPU_NUM'], '--repeat-rate', scene['repeat_rate'], *scene['extra_args']]
        for _, (_, field, option) in PEAK_NEW_DEFAULTS.items():
            args.extend(['--' + option, str(scene[field])])
        if self.values['PEAK_HARD_CAP'] == '1':
            args.extend(['--max-concurrency', str(scene['max_cc']), '--auto-expand-max-concurrency', str(scene['max_cc'])])
        seen = set()
        decision_offset, decision_sequence, decision_pending = 0, 0, b''
        decision_identity = None

        def publish_decisions(session, final=False):
            """Bridge only complete, ordered engine events from the owned file."""
            nonlocal decision_offset, decision_sequence, decision_pending, decision_identity
            source = session / 'peak_search_events.jsonl'
            if not source.exists():
                if decision_identity is not None:
                    raise RuntimeError('本轮搜索决策日志已经消失，不能报告日志完整')
                return  # Compatibility with old reports or a not-yet-started engine.
            if (session.is_symlink() or source.is_symlink()
                    or not path_is_relative_to(source.resolve(), self.output.resolve())):
                raise RuntimeError('搜索决策日志不属于本次任务的真实输出目录')
            # Never wait on a FIFO or device in the progress callback. The
            # descriptor check also rejects a replacement after this lstat.
            if not stat.S_ISREG(source.lstat().st_mode):
                raise RuntimeError('搜索决策日志不是普通文件，拒绝读取')
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
            while True:
                with os.fdopen(os.open(source, flags), 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise RuntimeError('搜索决策日志不是普通文件，拒绝读取')
                    identity = (info.st_dev, info.st_ino)
                    if decision_identity is None:
                        decision_identity = identity
                    elif identity != decision_identity:
                        raise RuntimeError('本轮搜索决策日志已被替换，拒绝读取其他日志')
                    if info.st_size < decision_offset:
                        raise RuntimeError('搜索决策日志被截断，不能继续发布不完整决策')
                    stream.seek(decision_offset)
                    block = stream.read(1024 * 1024)
                    decision_offset = stream.tell()
                decision_pending += block
                while b'\n' in decision_pending:
                    line, decision_pending = decision_pending.split(b'\n', 1)
                    try:
                        event = self.convert_paths(json.loads(line.decode('utf-8')))
                    except (UnicodeError, ValueError) as error:
                        raise RuntimeError('搜索决策日志包含无效的完整JSON记录') from error
                    if (not isinstance(event, dict) or type(event.get('schema_version')) is not int
                            or event['schema_version'] != 1
                            or type(event.get('seq')) is not int or event['seq'] != decision_sequence + 1
                            or event.get('scenario') != scene['name']
                            or not isinstance(event.get('event'), str) or not event['event']
                            or not isinstance(event.get('message'), str)
                            or not isinstance(event.get('time'), str)
                            or not isinstance(event.get('fields'), dict)
                            or event.get('level') not in {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}):
                        raise RuntimeError('搜索决策日志身份、顺序或字段无效，不能跳过后继续')
                    try:
                        if datetime.fromisoformat(event['time']).utcoffset() is None:
                            raise ValueError('missing timezone')
                    except ValueError as error:
                        raise RuntimeError('搜索决策日志时间无效或缺少时区') from error
                    fields = event['fields']
                    reserved = {'event', 'message', 'level', 'scope', 'schema_version', 'seq', 'time',
                                'batch_id', 'task', 'run_id', 'scenario', 'phase', 'model',
                                'test_index', 'planned_tests', 'run_kind', 'decision_event',
                                'decision_seq', 'decision_time', 'decision_evidence'}
                    if reserved.intersection(fields):
                        raise RuntimeError('搜索决策字段覆盖了业务事件身份')
                    self.log('peak_decision', event['message'], level=event['level'],
                             decision_event=event['event'], decision_seq=event['seq'],
                             decision_time=event.get('time', ''), decision_evidence=str(source),
                             **fields)
                    decision_sequence = event['seq']
                if len(decision_pending) > 1024 * 1024:
                    raise RuntimeError('搜索决策单条记录超过1MiB，拒绝不受限缓存')
                if not final or not block:
                    break
            if final and decision_pending:
                raise RuntimeError('搜索决策日志结束时仍有未完成记录')

        def publish_rounds(final=False):
            sessions = list(self.output.glob('peak_*_' + tag))
            if len(sessions) != 1:
                return
            report_path = sessions[0] / 'peak_search_report.json'
            current = {}
            if report_path.is_file():
                try:
                    current = self.convert_paths(json.loads(report_path.read_text(encoding='utf-8')))
                except (FileNotFoundError, json.JSONDecodeError):
                    if final:
                        raise
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
                measurement = trial.get('measurement_state', trial.get('state', ''))
                strict = trial.get('strict_slo_passed')
                label = ('严格通过' if strict is True else '容差通过' if strict is False else '通过') if measurement == 'PASS' else {
                    'SLO_FAIL': '超限', 'INVALID': '测量无效', 'ERROR': '执行错误',
                    'REQUEST_ERROR': '请求错误', 'UNSTABLE': '本轮测量不一致',
                }.get(measurement, measurement)
                actual_ttft = trial.get(trial.get('ttft_column', 'TTFT avg'), '')
                try:
                    value = float(actual_ttft)
                    actual_ttft = f'{value / 1000:.4f}s' if math.isfinite(value) else '无有效测量'
                except (ValueError, TypeError):
                    actual_ttft = '无适用测量'
                actual_tpot = trial.get(trial.get('tpot_column', 'TPOT avg'), '')
                actual_tpot = str(actual_tpot) + 'ms' if actual_tpot != '' else '无适用测量'
                profile = '确认复测' if trial.get('profile') == 'confirm' else '区间搜索'
                ttft_limit = f"基础阈值{scene['ttft']}s，容差上限{scene['ttft'] * (1 + tolerance):g}s" if scene['ttft'] > 0 else '此项SLO未启用'
                tpot_limit = f"基础阈值{scene['tpot']}ms，容差上限{scene['tpot'] * (1 + tolerance):g}ms" if scene['tpot'] > 0 else '此项SLO未启用'
                round_fields = dict(evidence=str(file), trial_id=trial.get('trial_id', ''),
                    sequence_id=trial.get('sequence_id'), selected_scope_id=scope.get('scope_id', ''),
                    profile=trial.get('profile', ''), configured_concurrency=trial.get('concurrency'),
                    configured_requests=trial.get('num_prompts'), measurement_state=measurement,
                    strict_slo_passed=strict, actual_concurrency=trial.get('cc', ''),
                    ttft_column=trial.get('ttft_column', 'TTFT avg'),
                    tpot_column=trial.get('tpot_column', 'TPOT avg'),
                    measurements={key: trial.get(key, '') for key in (
                        'total_req', 'input_len', 'output_len', 'cc',
                        trial.get('ttft_column', 'TTFT avg'), trial.get('tpot_column', 'TPOT avg'))},
                    effective_limits=trial.get('effective_limits', {}), reason=trial.get('reason', ''),
                    decision_evidence=str(sessions[0] / 'peak_search.log'))
                # The search engine owns the next decision. Its current trace
                # has no next_action field; do not infer one from PASS/FAIL.
                if 'next_action' in trial:
                    round_fields['next_action'] = trial['next_action']
                self.log('peak_round', f"摸高轮次完成：配置并发={trial.get('concurrency')}，阶段={profile}，测量={label}；"
                         f"TTFT[{trial.get('ttft_column', 'TTFT avg')}]={actual_ttft}（{ttft_limit}）；"
                         f"TPOT[{trial.get('tpot_column', 'TPOT avg')}]={actual_tpot}（{tpot_limit}）；"
                         f"说明={trial.get('reason', '')}",
                    **round_fields)
            publish_decisions(sessions[0], final=final)

        rc = self.run_container(args, float(scene['peak_total_timeout']) + 90, on_progress=publish_rounds)
        paths = list(self.output.glob('peak_*_' + tag + '/peak_search_report.json'))
        if len(paths) != 1:
            publish_rounds(final=True)
            self.append_row(scene, {}, 'peak_error', termination_reason='report_missing', measurement_state='ERROR')
            reason = '本轮报告缺失或身份不唯一，不读取历史报告'
            self.record_outcome('peak_error', False, reason, evidence=str(self.result), return_code=rc,
                                report_candidates=[str(item) for item in paths])
            self.log('peak_error', reason, level='ERROR', status='peak_error', passed=False,
                     return_code=rc, reason=reason, report_candidates=[str(item) for item in paths],
                     evidence=str(self.result))
            return False
        path = paths[0]
        self.current_evidence = path
        report = self.convert_paths(json.loads(path.read_text(encoding='utf-8')))
        atomic_json(path, report)
        publish_rounds(final=True)
        if report.get('complete') is not True or not (path.parent / 'archive_manifest.json').is_file():
            self.append_row(scene, {}, 'peak_error', measurement_state='ERROR', termination_reason='incomplete_report')
            reason = '摸高进程未完成最终报告和归档，不能将临时最佳轮次作为结论'
            self.record_outcome('peak_error', False, reason, evidence=str(path), return_code=rc,
                                termination='incomplete_report')
            self.log('peak_error', reason, level='ERROR', status='peak_error', passed=False,
                return_code=rc, reason=reason, evidence=str(path), termination='incomplete_report')
            write_archive_manifest(path.parent, task_kind='peak-search-incomplete', archive_name=scene['name'],
                run_tag=tag, output_root=self.output, extra_metadata={'termination': 'incomplete_report'})
            return False
        successful = rc == 0 and bool(report.get('best'))
        final_outcomes = []
        for row in report.get('best') or [{}]:
            status = row.get('status', 'peak_error')
            successful = successful and status in {'peak_confirmed', 'peak_capped'}
            reference = row.get('evaluation_file', '')
            _, scope = self.evaluation(Path(reference)) if reference else ({}, {})
            self.append_row(scene, row, status, evaluation_file=reference, scope=scope, report_file=str(path),
                selected_trial_id=row.get('selected_trial_id', ''), termination_reason=row.get('termination', ''))
            reason = row.get('reason') or '最终报告未提供具体原因，需核查选中作用域和搜索轨迹'
            if status == 'peak_unstable':
                summaries = report.get('summaries', [])
                unstable = sorted({point for summary in summaries for point in summary.get('unstable_points', [])})
                message = (f"摸高结束：边界存在波动；已复测稳定并发={row.get('concurrency') or '尚未找到'}；"
                           f"实测平均并发={row.get('cc') or '无适用测量'}；最高边界尚未确认；"
                           f"波动并发={unstable or '详见搜索轨迹'}；"
                           f"说明=同一并发的完整复测出现通过与超限，已保留独立确认的稳定点和原始证据；详细记录={path}")
            else:
                message = (f"摸高结束：{status}；配置并发={row.get('concurrency') or '未确认'}；"
                           f"实测平均并发={row.get('cc') or '无适用测量'}；说明={reason}；详细记录={path}")
            level = ('INFO' if status in {'peak_confirmed', 'peak_capped'} else
                     'WARNING' if status in {'peak_unstable', 'peak_budget', 'peak_no_pass', 'peak_cancelled'} else 'ERROR')
            if rc != 0 and status in {'peak_confirmed', 'peak_capped'}:
                reason = f'工具退出码={rc}，报告记录状态={status}；{reason}'
                message = f'摸高调用未成功：{reason}；保留报告中的测量记录；详细记录={path}'
                level = 'ERROR'
            outcome = dict(status=status, passed=rc == 0 and status in {'peak_confirmed', 'peak_capped'},
                reason=reason, selected_concurrency=row.get('concurrency'),
                actual_concurrency=row.get('cc', ''), selected_trial_id=row.get('selected_trial_id', ''),
                selected_scope_id=scope.get('scope_id', ''), evaluation_file=reference,
                termination=row.get('termination', ''), evidence=str(path), return_code=rc)
            final_outcomes.append(outcome)
            self.log('peak_end', message, level=level, status=status, passed=outcome['passed'],
                evidence=str(path), return_code=rc, raw_reason=reason, reason=reason,
                selected_concurrency=row.get('concurrency'), actual_concurrency=row.get('cc', ''),
                selected_trial_id=row.get('selected_trial_id', ''), selected_scope_id=scope.get('scope_id', ''),
                evaluation_file=reference, termination=row.get('termination', ''),
                unstable_points=(unstable if status == 'peak_unstable' else []))
        # Path conversion changes JSON bytes after the container's final seal.
        # Seal the completed host evidence only after every conversion is done.
        previous = json.loads((path.parent / 'archive_manifest.json').read_text(encoding='utf-8'))
        write_archive_manifest(path.parent, task_kind='peak-search', archive_name=previous['archive_name'],
            run_tag=previous.get('run_tag', tag), output_root=self.output,
            source_root=Path(previous['source_root']) if previous.get('source_root') else None,
            captured_outputs_dir=previous.get('captured_outputs_dir', ''),
            extra_metadata=self.convert_paths(previous.get('extra_metadata', {})))
        if len(final_outcomes) == 1:
            outcome = dict(final_outcomes[0])
            status, reason = outcome.pop('status'), outcome.pop('reason')
            outcome.pop('passed')
            self.record_outcome(status, successful, reason, **outcome)
        else:
            self.record_outcome('peak_mixed', successful,
                '多个最终记录：' + '；'.join(str(item['status']) + ': ' + str(item['reason']) for item in final_outcomes),
                evidence=str(path), return_code=rc, final_records=final_outcomes)
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
        self.phase, self.current, self.current_kind, self.current_scene = 'finished', '', '', {}
        rc = 0 if passed else 1
        counts = self.outcome_counts()
        non_success = [item for item in self.scenario_outcomes if not item['passed']]
        unconfirmed = {'peak_unstable', 'peak_budget', 'peak_no_pass', 'peak_cancelled'}
        terminal_level = ('INFO' if passed else
                          'WARNING' if non_success and all(item['status'] in unconfirmed for item in non_success) else 'ERROR')
        attention = '；需检查=' + '、'.join(item['scenario'] + '[' + item['status'] + ']'
                                         for item in non_success) if non_success else ''
        self.log('task_end', f"任务结束：已完成{counts['completed']}/{counts['planned']}个场景，"
                 f"执行判定通过={counts['passed']}，未通过或未确认={counts['non_success']}；"
                 f"固定测试通过={counts['fixed_passed']}仅表示执行与完整性，摸高结论按各场景状态判断{attention}",
            level=terminal_level, exit_code=rc,
            result_csv=str(self.result / 'all_results.csv'), outcome_counts=counts,
            scenario_outcomes=self.scenario_outcomes, non_success_scenarios=non_success)
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
        config = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else RELEASE_ROOT / 'config.env'
        if 'SCENARIOS_JSON_OVERRIDE' in os.environ:
            raise ValueError('SCENARIOS_JSON_OVERRIDE已停用，请把场景写入所用配置文件（如 config.env）的 SCENARIOS_JSON 块，并 unset 旧变量')
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
            worker.record_interrupted_scene('cancelled', '任务收到中断信号，当前场景未完成')
            worker.runtime(False, 130)
        return 130
    except Exception as error:
        message = REDACTOR.text(str(error))
        if worker:
            try:
                worker.task_error = dict(type=type(error).__name__, reason=message,
                                         scenario=worker.current, phase=worker.phase)
                worker.record_interrupted_scene('execution_error', message)
                worker.log('task_error', message, level='ERROR')
                worker.runtime(False, 1)
            except (OSError, ValueError):
                worker.runtime(False, 1, logging_error='任务日志或结果写入失败')
        console_message('ERROR: ' + message, stream=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
