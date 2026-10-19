"""Durable journal, credential redaction and native-tree isolation contracts."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

AUTOMATION_ROOT = Path(__file__).resolve().parent.parent
CORE_ROOT = AUTOMATION_ROOT / 'core'
TOOLS_ROOT = AUTOMATION_ROOT / 'tools'
RELEASE_ROOT = AUTOMATION_ROOT.parent
for folder in (CORE_ROOT, TOOLS_ROOT):
    sys.path.insert(0, str(folder))
from aisbench_logging import (BatchLog, LogWriteError, Redactor, archive_management_logs,
                              private_binary, validate_output_path)


class LoggingContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aisbench_logging_contract_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.native = self.root / "aisbench_auto_tools_prefix"
        self.native.mkdir()

    def test_worker_keeps_durable_logs_when_manager_console_pipes_close(self):
        human = self.root / 'worker.log'
        events = self.root / 'worker.events.jsonl'
        code = (
            "import sys,time\n"
            "sys.path.insert(0," + repr(str(CORE_ROOT)) + ")\n"
            "from aisbench_worker import Worker,console_message\n"
            "from pathlib import Path\n"
            "w=Worker.__new__(Worker)\n"
            "w.values={'MODEL_NAME':'model'};w.sequence=0;w.phase='running';w.current='case'\n"
            "w.human=open(" + repr(str(human)) + ",'wb');w.events=open(" + repr(str(events)) + ",'wb')\n"
            "time.sleep(0.2)\n"
            "w.log('scope_end','complete')\n"
            "w.log('task_end','finished',exit_code=0)\n"
            "console_message('optional stderr',stream=sys.stderr)\n"
            "w.human.close();w.events.close()\n")
        process = subprocess.Popen([sys.executable, '-B', '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.stdout.close()
        process.stderr.close()
        self.assertEqual(process.wait(timeout=20), 0)
        records = [json.loads(line) for line in events.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([record['event'] for record in records], ['scope_end', 'task_end'])
        self.assertIn('finished', human.read_text(encoding='utf-8'))

    def test_container_client_streams_survive_lost_manager_pipe(self):
        # The stand-in client really writes both descriptors after its
        # ancestor's readers close. Inheriting those pipes used to turn a
        # successful inner benchmark into a failed docker exec/worker.
        client = "import sys;sys.stdout.buffer.write(b'complete\\xff\\n');sys.stdout.flush();sys.stderr.buffer.write(b'diagnostic\\n');sys.stderr.flush()"
        code = (
            "import sys,time\nfrom pathlib import Path\n"
            "sys.path.insert(0," + repr(str(CORE_ROOT)) + ")\n"
            "from aisbench_worker import Worker\n"
            "w=Worker.__new__(Worker);w.result=Path(" + repr(str(self.root)) + ")\n"
            "w.docker=[sys.executable,'-B','-c'," + repr(client) + "];w.container_id='fixture';w.work_path='unused'\n"
            "w.assert_container_ready=lambda:None;w.log=lambda *a,**kw:None\n"
            "time.sleep(.2)\nraise SystemExit(w.run_container([],10))\n")
        process = subprocess.Popen([sys.executable, '-B', '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.stdout.close()
        process.stderr.close()
        self.assertEqual(process.wait(timeout=20), 0)
        self.assertEqual(next(self.root.glob('process_logs/*/stdout.raw.log')).read_bytes(), b'complete\xff\n')
        self.assertEqual(next(self.root.glob('process_logs/*/stderr.raw.log')).read_bytes(), b'diagnostic\n')

    def test_critical_events_have_sequence_timezone_identity_and_fsync(self):
        log = BatchLog(self.root / "outputs" / "batch", "batch", [self.native])
        with mock.patch("aisbench_logging.os.fsync", wraps=os.fsync) as sync:
            log.emit("batch_started", "检查全部配置", task="a", configured_concurrency=4)
            log.emit("task_finished", "任务结束", task="a", exit_code=0)
            log.close()
        events = [json.loads(line) for line in (log.directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["seq"] for event in events], [1, 2])
        self.assertEqual({event["batch_id"] for event in events}, {"batch"})
        self.assertEqual(events[0]["task"], "a")
        self.assertRegex(events[0]["time"], r"[+-]\d\d:\d\d$")
        self.assertGreaterEqual(sync.call_count, 6)

    def test_native_and_symlink_targets_are_rejected_before_creation(self):
        for path in (self.native, self.native / "outputs" / "new"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                BatchLog(path, "bad", [self.native])
        self.assertEqual(list(self.native.iterdir()), [])
        alias = self.root / "output-alias"
        try:
            alias.symlink_to(self.native, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest("directory symlink unavailable: " + str(error))
        with self.assertRaises(ValueError):
            BatchLog(alias / "batch", "bad", [self.native])
        self.assertEqual(list(self.native.iterdir()), [])

    def test_redaction_removes_credentials_queries_and_terminal_escape(self):
        redactor = Redactor()
        redactor.register("private-key-123")
        value = redactor.text("\x1b[31mAPI_KEY=private-key-123 Authorization: Bearer token456 https://user:password@192.168.1.10:8000/v1?key=hidden#fragment\x1b[0m")
        for secret in ("private-key-123", "token456", "password@", "hidden", "user:", "\x1b"):
            self.assertNotIn(secret, value)
        self.assertIn("http", value)
        self.assertIn("/v1", value)
        self.assertEqual(redactor.value({"api_key": "unregistered", "nested": {"password": "value"}}),
                         {"api_key": "[redacted]", "nested": {"password": "[redacted]"}})

    def test_raw_files_preserve_invalid_bytes_and_are_private(self):
        target = self.root / "stdout.raw.log"
        payload = b"utf8\xff\x00\r\n"
        with private_binary(target, [self.native]) as handle:
            handle.write(payload)
        self.assertEqual(target.read_bytes(), payload)
        if os.name == "posix":
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_event_write_failure_is_reported_and_not_silently_ignored(self):
        log = BatchLog(self.root / "batch", "batch", [self.native])
        self.addCleanup(log.close)
        class BrokenWriter:
            def write(self, _value):
                raise OSError("synthetic disk full")
        original = log._events
        try:
            log._events = BrokenWriter()
            with self.assertRaises(LogWriteError):
                log.emit("task_started", "开始")
        finally:
            log._events = original

    def test_sibling_output_is_permitted_and_missing_descendants_resolve(self):
        target = self.root / "outputs" / "default" / "batch"
        self.assertEqual(validate_output_path(target, [self.native]), target)

    def test_segment_index_restores_every_raw_byte_and_checksums(self):
        batch = self.root / 'batch'
        logs = batch / 'logs'
        logs.mkdir(parents=True)
        source = logs / 'stdout.raw.log'
        data = b'line\xff\x00\r\n' + bytes(range(64)) + b'no-final-newline'
        external = self.root / 'custom-task' / 'execution.log'
        external.parent.mkdir()
        with private_binary(source, [self.native]) as handle:
            handle.write(data)
        with private_binary(external, [self.native]) as handle:
            handle.write('中文任务日志\n'.encode('utf-8'))
        result = archive_management_logs(batch, [self.native], [external], segment_bytes=17)
        index = json.loads(Path(result['index']).read_text(encoding='utf-8'))
        self.assertEqual(index['state'], 'complete')
        self.assertTrue(index['raw_retained'])
        self.assertEqual(len(index['sources']), 2)
        for record in index['sources']:
            restored = b''
            for segment in record['segments']:
                compressed = logs / segment['path']
                self.assertEqual(hashlib.sha256(compressed.read_bytes()).hexdigest(), segment['gzip_sha256'])
                uncompressed = gzip.decompress(compressed.read_bytes())
                self.assertEqual(hashlib.sha256(uncompressed).hexdigest(), segment['raw_sha256'])
                self.assertEqual(segment['offset'], len(restored))
                self.assertLessEqual(len(uncompressed), 17)
                self.assertEqual(len(uncompressed), segment['raw_bytes'])
                restored += uncompressed
                if os.name == 'posix':
                    self.assertEqual(compressed.stat().st_mode & 0o777, 0o600)
            raw = Path(record['source']).read_bytes()
            self.assertEqual(restored, raw)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), record['raw_sha256'])
            self.assertEqual(len(raw), record['raw_bytes'])
        self.assertEqual(source.read_bytes(), data)

    def test_close_archives_once_after_final_event_and_keeps_raw_logs(self):
        log = BatchLog(self.root / 'batch', 'batch', [self.native])
        log.emit('batch_workload_finished', '执行阶段完成，等待封存')
        before = {name: (log.directory / name).read_bytes() for name in ('batch.log', 'events.jsonl')}
        result = log.close()
        self.assertEqual(log.close(), result)
        self.assertEqual(len(list((log.directory / 'archive').iterdir())), 1)
        for name, data in before.items():
            self.assertEqual((log.directory / name).read_bytes(), data)
        index = json.loads((log.directory / 'archive_index.json').read_text(encoding='utf-8'))
        self.assertEqual(index['file_count'], 2)
        self.assertEqual(index['terminal_state_file'], str(log.batch_path / 'batch_state.json'))
        with self.assertRaises(LogWriteError):
            log.emit('late_event', '已经关闭')

    def test_compression_failure_never_commits_index_or_removes_originals(self):
        log = BatchLog(self.root / 'batch', 'batch', [self.native])
        log.emit('batch_workload_finished', '等待封存')
        original = (log.directory / 'batch.log').read_bytes()
        with mock.patch('aisbench_logging.gzip.GzipFile', side_effect=OSError('synthetic archive disk full')):
            with self.assertRaises(LogWriteError):
                log.close()
        self.assertEqual((log.directory / 'batch.log').read_bytes(), original)
        self.assertFalse((log.directory / 'archive_index.json').exists())
        states = list((log.directory / 'archive').glob('*/archive_state.json'))
        self.assertEqual(len(states), 1)
        self.assertEqual(json.loads(states[0].read_text(encoding='utf-8'))['state'], 'failed')

    def test_log_changed_during_compression_never_commits_complete_index(self):
        logs = self.root / 'batch' / 'logs'
        logs.mkdir(parents=True)
        source = logs / 'stdout.raw.log'
        source.write_bytes(b'original')
        original_write = gzip.GzipFile.write
        changed = []
        def changing_write(encoder, data):
            if not changed:
                with source.open('ab') as stream:
                    stream.write(b'late')
                changed.append(True)
            return original_write(encoder, data)
        with mock.patch.object(gzip.GzipFile, 'write', changing_write):
            with self.assertRaises(LogWriteError):
                archive_management_logs(logs.parent, [self.native], segment_bytes=4)
        self.assertEqual(source.read_bytes(), b'originallate')
        self.assertFalse((logs / 'archive_index.json').exists())

    def test_archive_rejects_native_source_and_retains_existing_native_bytes(self):
        logs = self.root / 'batch' / 'logs'
        logs.mkdir(parents=True)
        source = self.native / 'keep.log'
        source.write_bytes(b'native sentinel')
        with self.assertRaises(ValueError):
            archive_management_logs(logs.parent, [self.native], [source])
        self.assertEqual(source.read_bytes(), b'native sentinel')
        self.assertFalse((logs / 'archive').exists())

    def test_manager_preflight_failure_still_seals_logs_and_persists_terminal_state(self):
        import aisbench_run_manager as manager
        with mock.patch.multiple(manager, _STOP_REQUESTED=0, _LOG_ERROR='', _BATCH_LOG=None, _NATIVE_ROOTS=[]):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = manager.supervise(self.root / 'run_perf_scenarios.sh', [str(self.root / 'absent.env')])
        self.assertNotEqual(code, 0)
        states = list((self.root / 'outputs/default').glob('*/batch_state.json'))
        self.assertEqual(len(states), 1)
        state = json.loads(states[0].read_text(encoding='utf-8'))
        self.assertEqual(state['state'], 'failed')
        self.assertTrue(state['finished_at'])
        index = json.loads(Path(state['log_archive']['index']).read_text(encoding='utf-8'))
        self.assertEqual(index['state'], 'complete')
        self.assertEqual(index['terminal_state_file'], str(states[0]))
        self.assertTrue(any(record['source'].endswith('batch.log') for record in index['sources']))

    def test_manager_archive_failure_cannot_leave_success_exit_or_state(self):
        import aisbench_run_manager as manager
        with mock.patch.multiple(manager, _STOP_REQUESTED=0, _LOG_ERROR='', _BATCH_LOG=None, _NATIVE_ROOTS=[]):
            with mock.patch.object(manager, '_supervise', return_value=0):
                with mock.patch('aisbench_logging.archive_management_logs', side_effect=OSError('synthetic full archive disk')):
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        code = manager.supervise(self.root / 'run_perf_scenarios.sh', [])
        self.assertNotEqual(code, 0)
        states = list((self.root / 'outputs/default').glob('*/batch_state.json'))
        state = json.loads(states[0].read_text(encoding='utf-8'))
        self.assertEqual(state['state'], 'failed')
        self.assertEqual(state['logging_error'], 'BATCH_LOG_CLOSE_FAILED')
        self.assertNotEqual(state['exit_code'], 0)
        self.assertTrue((states[0].parent / 'logs/batch.log').is_file())


if __name__ == "__main__":
    unittest.main()
