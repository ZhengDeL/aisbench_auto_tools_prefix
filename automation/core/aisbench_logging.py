"""Private local diagnostics, safe human messages and durable batch events.

Only the manager writes a BatchLog. Raw subprocess files are separate, private
evidence; they are never copied into the human log or a published source tree.
"""
from __future__ import annotations

import codecs
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit


class LogWriteError(OSError):
    """Persistent evidence could not be written; the workload must stop."""


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def validate_output_path(path: Path | str, native_roots: list[Path | str]) -> Path:
    """Resolve existing symlinks before creating *any* output or log file."""
    candidate = Path(os.path.realpath(os.path.abspath(path)))
    for root in native_roots:
        native = Path(os.path.realpath(os.path.abspath(root)))
        try:
            if os.path.commonpath((os.path.normcase(candidate), os.path.normcase(native))) == os.path.normcase(native):
                raise ValueError("输出、日志或临时目录不能位于原生源码目录内（含符号链接）")
        except ValueError as error:
            # Different Windows drives have no common ancestor.
            if str(error).startswith("输出"):
                raise
    return candidate


class Redactor:
    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def register(self, value: str) -> None:
        if value:
            with self._lock:
                self._secrets.add(value)
                if value.strip():
                    self._secrets.add(value.strip())

    def text(self, value: object) -> str:
        message = str(value)
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for secret in secrets:
            if len(secret) < 4:
                message = re.sub(r"(?<![A-Za-z0-9])" + re.escape(secret) + r"(?![A-Za-z0-9])", "[redacted]", message)
            else:
                message = message.replace(secret, "[redacted]")
        # Remove credentials and query strings, retaining useful endpoint paths.
        def safe_url(match: re.Match[str]) -> str:
            try:
                parsed = urlsplit(match.group(0))
                host = parsed.hostname or "[invalid-host]"
                if ":" in host:
                    host = "[" + host + "]"
                if parsed.port is not None:
                    host += ":" + str(parsed.port)
                return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
            except ValueError:
                return "[redacted-url]"
        message = re.sub(r"https?://[^\s<>\"']+", safe_url, message, flags=re.I)
        message = re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;\"']+", r"\1[redacted]", message)
        message = re.sub(r"(?i)((?:api[_-]?key|password|passwd|access[_-]?token|secret)\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", r"\1[redacted]", message)
        # Prevent terminal escape injection while keeping tabs and newlines.
        message = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", message)
        return "".join(char if char in "\n\t" or ord(char) >= 32 else " " for char in message)

    def value(self, value: object) -> object:
        if isinstance(value, dict):
            return {str(key): ("[redacted]" if re.search(r"(?i)(api[_-]?key|password|passwd|secret|authorization|access[_-]?token)", str(key)) else self.value(item))
                    for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return self.text(value) if isinstance(value, str) else value


REDACTOR = Redactor()
LOG_SEGMENT_BYTES = 16 * 1024 * 1024
NATIVE_TABLE_MAX_BYTES = 256 * 1024
NATIVE_TABLE_MAX_LINES = 256
NATIVE_TABLE_TIMEOUT = 0.5
NATIVE_REFRESH_INTERVAL = 1.0


class _NativeConsoleView:
    """Build a readable view after raw capture; never parse business outcomes.

    BatchLog's lock protects this assembler and its eventual file write. A table
    is held only until its bottom border, so another stream's warning cannot
    split its rows. Raw files and their chunk journal retain the original order;
    this display may place such warnings immediately before the whole table.
    """
    _TOP = re.compile(r'^╒[═╤]+╕$')
    _BOTTOM = re.compile(r'^╘[═╧]+╛$')
    _SEPARATOR = re.compile(r'^(?:╞[═╪]+╡|├[─┼]+┤)$')
    _HEADERS = {
        ('Performance Parameters', 'Stage', 'Average', 'Min', 'Max',
         'Median', 'P75', 'P90', 'P99', 'N'),
        ('Common Metric', 'Stage', 'Value'),
    }
    _COUNTER = re.compile(
        r'^POST=(\d+) \([0-9.]+/s\)\s+RECV=(\d+) \([0-9.]+/s\)\s+'
        r'FAIL=(\d+) \([0-9.]+/s\)\s+(FIN|FINISH)=(\d+) \([0-9.]+/s\)\s*$')
    _PROGRESS = re.compile(
        r'^(Progress:|Calculating performance details:)\s+(\d+(?:\.\d+)?)%\|[^|]*\|'
        r'\s+\d+/\d+\s+\[[^\]\r\n]*\]\s*$')
    _INTERNAL_PREFIXES = ('AISBENCH_SCOPE_START ', 'AISBENCH_SCOPE_END ',
                          'AISBench trial starting; ', 'AISBench trial finished: ')

    def __init__(self):
        self._streams = {}
        self._section = None

    @staticmethod
    def _context(context):
        return tuple((key, REDACTOR.text((context or {})[key]).replace('\n', '\\n'))
                     for key in ('scenario', 'trial_id', 'scope_id', 'phase')
                     if (context or {}).get(key) not in (None, ''))

    def _native(self, state, text):
        """One title per source switch; the original lines have no prefix."""
        # Both native descriptors belong to one visible source. Switching
        # stdout/stderr must not repeatedly re-title a single task's output.
        identity = (state['task'], tuple((key, value) for key, value in state['context']
                                        if key in ('scenario', 'trial_id', 'scope_id')))
        heading = ''
        if self._section != identity:
            fields = ['task=' + state['task']]
            fields.extend(key + '=' + value for key, value in state['context'])
            fields.append('streams=stdout+stderr')
            heading = '\n=== AISBench | ' + ' | '.join(fields) + ' ===\n'
            self._section = identity
        return heading + text

    def business(self, text):
        return '[AUTOMATION] ' + text

    def _partial_table(self, state, reason):
        table = state['table']
        if table is None:
            return ''
        state['table'] = None
        # Never silently discard a candidate, even if an unexpected native
        # version changed the header/borders or the child exited mid-table.
        note = '[AUTOMATION] [' + state['task'] + '] '
        note += '原生表格显示未完整（' + reason + '）；保留已收到的内容。\n'
        self._section = None
        return note + self._native(state, ''.join(table['lines']))

    def _refreshes(self, state, now, final=False):
        rendered = []
        for progress in state['progress'].values():
            if progress['pending'] is not None and (final or now - progress['written_at'] >= NATIVE_REFRESH_INTERVAL):
                text = progress['pending']
                rendered.append(self._native(state, text + '\n'))
                progress.update(pending=None, written_at=now, last=text)
        return ''.join(rendered)

    def _flush(self, state, now, final=False, reason='输出结束'):
        rendered = self._refreshes(state, now, final)
        table = state['table']
        if table is not None and (final or now - table['started_at'] >= NATIVE_TABLE_TIMEOUT):
            rendered += self._partial_table(state, reason if final else '等待表格结束超时')
        return rendered

    @classmethod
    def _refresh_kind(cls, text):
        match = cls._PROGRESS.match(text)
        if match:
            complete = float(match.group(2)) == 100
            return match.group(1), complete
        match = cls._COUNTER.fullmatch(text)
        if match:
            complete = match.group(4) == 'FINISH' and int(match.group(1)) > 0 and match.group(1) == match.group(5)
            return 'request counters', complete
        return None, False

    def _progress(self, state, text, kind, complete, now):
        progress = state['progress'].get(kind)
        if progress is None:
            state['progress'][kind] = dict(written_at=now, last=text, pending=None)
            return self._native(state, text + '\n')
        if progress['last'] == text:
            progress['pending'] = None
            return ''
        if complete:
            progress.update(pending=None, written_at=now)
            if progress['last'] == text:
                return ''
            progress['last'] = text
            return self._native(state, text + '\n')
        if now - progress['written_at'] >= NATIVE_REFRESH_INTERVAL:
            progress.update(written_at=now, last=text, pending=None)
            return self._native(state, text + '\n')
        progress['pending'] = text
        return ''

    def _line(self, state, text, now):
        rendered = self._flush(state, now)
        table = state['table']
        if table is not None:
            stripped = text.strip()
            if not table['confirmed']:
                columns = tuple(part.strip() for part in stripped.split('│')[1:-1])
                valid = stripped.startswith('│') and stripped.endswith('│') and columns in self._HEADERS
                if not valid:
                    rendered += self._partial_table(state, '未识别的表头')
                    return rendered + self._ordinary(state, text, now)
                table['confirmed'] = True
            elif not (self._BOTTOM.fullmatch(stripped) or self._SEPARATOR.fullmatch(stripped)
                      or (stripped.startswith('│') and stripped.endswith('│'))):
                rendered += self._partial_table(state, '表格中出现其他输出')
                return rendered + self._ordinary(state, text, now)
            table['lines'].append(text + '\n')
            table['bytes'] += len((text + '\n').encode('utf-8'))
            if len(table['lines']) > NATIVE_TABLE_MAX_LINES or table['bytes'] > NATIVE_TABLE_MAX_BYTES:
                return rendered + self._partial_table(state, '超过显示缓冲上限')
            if self._BOTTOM.fullmatch(stripped):
                state['table'] = None
                return rendered + self._native(state, ''.join(table['lines']))
            return rendered
        return rendered + self._ordinary(state, text, now)

    def _ordinary(self, state, text, now):
        # Only the known native refresh formats are compacted. Some tqdm modes
        # prepend CR/cursor movement but end each frame with LF, so the caller's
        # CR flag alone cannot identify them. Warnings and unknown lines pass.
        kind, complete = self._refresh_kind(text)
        if kind is not None:
            return self._progress(state, text, kind, complete, now)
        if not text.strip():
            # Cursor-only tqdm frames can become empty after sanitization.
            # They must not force every buffered progress update to print.
            return self._native(state, text + '\n')
        rendered = self._refreshes(state, now, final=True)
        if text.startswith('[AUTOMATION] '):
            return rendered + self.business('[' + state['task'] + '] ' + text[len('[AUTOMATION] '):] + '\n')
        if text.startswith(self._INTERNAL_PREFIXES):
            return rendered + self.business('[' + state['task'] + '] ' + text + '\n')
        if state['channel'] == 'stdout' and self._TOP.fullmatch(text.strip()):
            state['table'] = dict(lines=[text + '\n'], bytes=len((text + '\n').encode('utf-8')),
                                  confirmed=False, started_at=now)
            if NATIVE_TABLE_MAX_LINES < 1 or state['table']['bytes'] > NATIVE_TABLE_MAX_BYTES:
                return rendered + self._partial_table(state, '超过显示缓冲上限')
            return rendered
        return rendered + self._native(state, text + '\n')

    def feed(self, task, channel, line, context=None, refresh=False):
        """Consume a decoded frame; raw bytes are already durably captured."""
        now = time.monotonic()
        task = REDACTOR.text(task).replace('\n', '\\n')
        channel = REDACTOR.text(channel).replace('\n', '\\n')
        identity = (task, channel)
        context = self._context(context)
        state = self._streams.get(identity)
        rendered = ''
        if state is None or state['context'] != context:
            if state is not None:
                rendered += self._flush(state, now, final=True, reason='场景或作用域切换')
            state = dict(task=task, channel=channel, context=context, table=None, progress={})
            self._streams[identity] = state
        # Blank lines are meaningful around native tables. Keep them rather
        # than treating them as absent messages. Escapes/secrets remain subject
        # to the same human-view sanitizer as the previous implementation.
        for text in REDACTOR.text(line).split('\n'):
            rendered += self._line(state, text, now)
        return rendered

    def flush(self, task=None, channel=None, final=False):
        now = time.monotonic()
        rendered = []
        for identity, state in tuple(self._streams.items()):
            if task is not None and state['task'] != task:
                continue
            if channel is not None and state['channel'] != channel:
                continue
            rendered.append(self._flush(state, now, final))
            if final:
                del self._streams[identity]
        return ''.join(rendered)


def private_binary(path: Path, native_roots: list[Path | str], mode: str = "xb"):
    target = validate_output_path(path, native_roots)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | (os.O_EXCL if "x" in mode else os.O_APPEND), 0o600)
    return os.fdopen(descriptor, mode)


def _archive_json(path, payload, native_roots):
    target = validate_output_path(path, native_roots)
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    with private_binary(temporary, native_roots) as stream:
        stream.write((json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8'))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(target))
    if os.name == 'posix':
        descriptor = os.open(str(target.parent), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def archive_management_logs(batch_path, native_roots, related_logs=(), segment_bytes=LOG_SEGMENT_BYTES):
    """Seal closed management logs without changing/deleting their raw bytes.

    A committed index is published only after all gzip segments and hashes are
    complete. Uncommitted partial directories remain evidence after failure.
    Splits are byte based: joining decompressed segments restores invalid UTF-8,
    NUL, CR and arbitrarily long lines exactly, with bounded working memory.
    """
    if isinstance(segment_bytes, bool) or not isinstance(segment_bytes, int) or segment_bytes < 1:
        raise ValueError('日志分段大小必须为正整数')
    batch = validate_output_path(batch_path, native_roots)
    logs = validate_output_path(batch / 'logs', native_roots)
    candidates = [path for path in logs.rglob('*')
                  if path.is_file() and path.suffix in {'.log', '.jsonl'}
                  and not {'archive', 'aisbench'}.intersection(path.relative_to(logs).parts)]
    candidates.extend(Path(path) for path in related_logs)
    sources = []
    seen = set()
    for path in candidates:
        if logs / 'aisbench' in path.absolute().parents:
            continue  # Native log links are live access points, not manager logs.
        if path.is_symlink():
            raise LogWriteError('日志封存拒绝符号链接来源')
        source = validate_output_path(path, native_roots)
        if not source.is_file():
            raise LogWriteError('日志封存来源缺失: ' + str(source))
        if str(source) not in seen:
            sources.append(source)
            seen.add(str(source))
    archive_root = validate_output_path(logs / 'archive', native_roots)
    archive_root.mkdir(exist_ok=True, mode=0o700)
    identifier = uuid.uuid4().hex
    generation = validate_output_path(archive_root / identifier, native_roots)
    generation.mkdir(exist_ok=False, mode=0o700)
    state = dict(schema_version=1, state='writing', started_at=timestamp(), segment_bytes=segment_bytes,
                 raw_retained=True, generation=identifier, terminal_state_file=str(batch / 'batch_state.json'))
    state_path = generation / 'archive_state.json'
    _archive_json(state_path, state, native_roots)
    records = []
    try:
        for ordinal, source in enumerate(sorted(sources, key=lambda path: str(path))):
            before = source.stat()
            directory = generation / ('source_%04d' % ordinal)
            directory.mkdir(mode=0o700)
            total_digest = hashlib.sha256()
            total = 0
            segments = []
            with source.open('rb') as original:
                for sequence in range(max(1, (before.st_size + segment_bytes - 1) // segment_bytes)):
                    target = validate_output_path(directory / ('%06d.gz' % sequence), native_roots)
                    temporary = target.with_suffix('.gz.tmp')
                    segment_digest = hashlib.sha256()
                    count = 0
                    with private_binary(temporary, native_roots) as compressed:
                        with gzip.GzipFile(filename='', mode='wb', fileobj=compressed, mtime=0, compresslevel=6) as encoder:
                            remaining = segment_bytes
                            while remaining:
                                data = original.read(min(1024 * 1024, remaining))
                                if not data:
                                    break
                                encoder.write(data)
                                total_digest.update(data)
                                segment_digest.update(data)
                                count += len(data)
                                remaining -= len(data)
                        compressed.flush()
                        os.fsync(compressed.fileno())
                    os.replace(str(temporary), str(target))
                    compressed_digest = hashlib.sha256()
                    with target.open('rb') as stream:
                        for data in iter(lambda: stream.read(1024 * 1024), b''):
                            compressed_digest.update(data)
                    segments.append(dict(path=str(target.relative_to(logs)), offset=total,
                        raw_bytes=count, raw_sha256=segment_digest.hexdigest(),
                        gzip_bytes=target.stat().st_size, gzip_sha256=compressed_digest.hexdigest()))
                    total += count
                if original.read(1):
                    raise LogWriteError('封存时日志继续增长，未发布完整索引')
            after = source.stat()
            if (total != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise LogWriteError('封存时日志发生变化，未发布完整索引')
            if os.name == 'posix':
                descriptor = os.open(str(directory), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            records.append(dict(source=str(source), raw_bytes=total, raw_sha256=total_digest.hexdigest(),
                                mode=before.st_mode & 0o777, segments=segments))
        state.update(state='complete', finished_at=timestamp(), file_count=len(records))
        _archive_json(state_path, state, native_roots)
        index = dict(state, sources=records)
        _archive_json(logs / 'archive_index.json', index, native_roots)
        return dict(index=str(logs / 'archive_index.json'), generation=identifier,
                    file_count=len(records), raw_bytes=sum(record['raw_bytes'] for record in records), raw_retained=True)
    except Exception as error:
        state.update(state='failed', finished_at=timestamp(), error_type=type(error).__name__)
        try:
            _archive_json(state_path, state, native_roots)
        except (OSError, ValueError):
            pass
        raise LogWriteError('日志分段封存失败；原始日志保留，不能报告日志完整封存') from error


class AsyncConsoleMirror:
    """Observe a durable UTF-8 view without putting the terminal on its writer.

    The supervisor only starts, wakes and closes this observer. One daemon owns
    its reader and a duplicate of the original target descriptor; all terminal
    writes and flushing happen there. A blocked console may lose its optional
    tail, while source files and workload supervision continue independently.
    """

    CLOSE_TIMEOUT = 0.5
    READ_BYTES = 65536
    WRITE_BYTES = 4096

    def __init__(self, source_path, target=None):
        self.source_path = Path(source_path)
        self._target = sys.stdout if target is None else target
        self._descriptor = None
        self._thread = None
        self._wake = threading.Event()
        self._closing = threading.Event()
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._lock = threading.Lock()
        self._read_bytes = 0
        self._written_bytes = 0
        self._failure = None
        self._final = None

    def _failed(self, reason, error=None):
        with self._lock:
            if self._final is None and self._failure is None:
                self._failure = dict(reason=reason,
                                     error_type=type(error).__name__ if error else '',
                                     errno=getattr(error, 'errno', None))

    def _written(self, count):
        # A late write must not alter already published completion metadata.
        with self._lock:
            if self._final is None:
                self._written_bytes += count

    def _write(self, block, decoder):
        if self._descriptor is not None:
            pending = memoryview(block)
            while pending and not self._stop.is_set():
                try:
                    count = os.write(self._descriptor, pending[:self.WRITE_BYTES])
                except InterruptedError:
                    continue
                if count is None or count <= 0:
                    raise OSError('console write made no progress')
                pending = pending[count:]
                self._written(count)
            return

        # Stream-like targets without a descriptor are useful to embedders and
        # tests. They still run only on this observer, including their flush.
        binary = getattr(self._target, 'buffer', None)
        pending = memoryview(block) if binary is not None else decoder.decode(block)
        while pending and not self._stop.is_set():
            try:
                count = (binary if binary is not None else self._target).write(pending)
            except InterruptedError:
                continue
            if count is None or count <= 0:
                raise OSError('console write made no progress')
            self._written(count if binary is not None else len(pending[:count].encode('utf-8')))
            pending = pending[count:]
        if not self._stop.is_set():
            (binary if binary is not None else self._target).flush()

    def _follow(self):
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        try:
            with self.source_path.open('rb', buffering=0) as source:
                if self._descriptor is not None:
                    source_info, target_info = os.fstat(source.fileno()), os.fstat(self._descriptor)
                    if ((source_info.st_dev, source_info.st_ino)
                            == (target_info.st_dev, target_info.st_ino)):
                        self._failed('console target equals source; recursive forwarding rejected',
                                     ValueError('console target refers to its source file'))
                        return
                while not self._stop.is_set():
                    block = source.read(self.READ_BYTES)
                    if block:
                        with self._lock:
                            if self._final is None:
                                self._read_bytes += len(block)
                        self._write(block, decoder)
                        continue
                    if self._closing.is_set():
                        break
                    self._wake.wait(.05)
                    self._wake.clear()
                if (not self._stop.is_set() and self._descriptor is None
                        and getattr(self._target, 'buffer', None) is None):
                    tail = decoder.decode(b'', final=True)
                    if tail:
                        pending = tail
                        while pending:
                            count = self._target.write(pending)
                            if count is None or count <= 0:
                                raise OSError('console write made no progress')
                            self._written(len(pending[:count].encode('utf-8')))
                            pending = pending[count:]
                        self._target.flush()
        except Exception as error:
            self._failed('console observer read/write failed', error)
        finally:
            if self._descriptor is not None:
                try:
                    os.close(self._descriptor)
                except OSError as error:
                    self._failed('console descriptor close failed', error)
                self._descriptor = None
            self._finished.set()

    def start(self):
        if self._thread is not None:
            return self
        if self._final is not None:
            raise RuntimeError('console observer is already closed')
        try:
            descriptor = (self._target if isinstance(self._target, int)
                          else self._target.fileno())
            self._descriptor = os.dup(descriptor)
        except (OSError, ValueError, AttributeError):
            # A stream without fileno is handled in the same isolated thread.
            self._descriptor = None
        try:
            self._thread = threading.Thread(target=self._follow,
                                            name='aisbench-console-view', daemon=True)
            self._thread.start()
        except Exception as error:
            # Console mirroring is optional. A resource/thread startup failure
            # must not abandon mandatory files or leave an unowned duplicate.
            self._failed('console observer could not start', error)
            descriptor, self._descriptor = self._descriptor, None
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as close_error:
                    self._failed('console descriptor close failed', close_error)
            self._thread = None
            self._finished.set()
        return self

    def drain(self, final=False):
        """Wake the reader; never write, flush or join the console here."""
        if final:
            self._closing.set()
        self._wake.set()

    def close(self):
        with self._lock:
            if self._final is not None:
                return dict(self._final)
        self.drain(final=True)
        if self._thread is not None:
            # Reserve a small part of the public half-second bound for the
            # snapshot/stat work after a terminal write has to be abandoned.
            self._thread.join(max(0, self.CLOSE_TIMEOUT - .05))
        unfinished = self._thread is not None and self._thread.is_alive()
        if unfinished:
            self._stop.set()
            self._failed('terminal backpressure; bounded final drain exhausted',
                         TimeoutError('console observer did not finish within 0.5s'))
        try:
            source_bytes = self.source_path.stat().st_size
        except OSError as error:
            source_bytes = None
            self._failed('console source size unavailable', error)
        with self._lock:
            if self._final is not None:
                return dict(self._final)
            failure = self._failure
            complete = (self._thread is not None and not unfinished and failure is None
                        and self._finished.is_set() and self._written_bytes == source_bytes)
            if not complete and failure is None:
                failure = dict(reason='console copy incomplete', error_type='', errno=None)
            self._final = dict(schema_version=1, complete=complete,
                               state='complete' if complete else 'degraded',
                               source=str(self.source_path), source_bytes=source_bytes,
                               bytes_read=self._read_bytes, bytes_written=self._written_bytes,
                               bytes=self._written_bytes,
                               reason='' if complete else failure['reason'],
                               error_type='' if complete else failure['error_type'],
                               errno=None if complete else failure['errno'],
                               source_retained=True)
            return dict(self._final)


class BatchLog:
    """Serialized writes; flush/fsync critical events, never swallow I/O errors."""
    def __init__(self, batch_path: Path, batch_id: str, native_roots: list[Path | str]) -> None:
        self.native_roots = native_roots
        self.batch_path = validate_output_path(batch_path, native_roots)
        self.batch_path.mkdir(parents=True, exist_ok=False, mode=0o700)
        validate_output_path(self.batch_path, native_roots)
        self.directory = self.batch_path / "logs"
        self.directory.mkdir(mode=0o700)
        (self.directory / "preflight").mkdir(mode=0o700)
        self.batch_id = batch_id
        self.sequence = 0
        self._lock = threading.Lock()
        self._closed = False
        self._human = None
        self._automation = None
        self._events = None
        self._console = None
        self._archive_result = None
        self._view = _NativeConsoleView()
        self.human_path = self.directory / "batch.log"
        self.automation_path = self.directory / "automation.log"
        self.event_path = self.directory / "events.jsonl"
        self.console_path = self.directory / "console.log"
        try:
            self._human = private_binary(self.human_path, native_roots)
            self._automation = private_binary(self.automation_path, native_roots)
            self._events = private_binary(self.event_path, native_roots)
            self._console = private_binary(self.console_path, native_roots)
        except BaseException:
            for handle in (self._human, self._automation, self._events, self._console):
                if handle is not None:
                    handle.close()
            raise

    @staticmethod
    def _context_text(context):
        if not context:
            return ''
        pieces = []
        for key, label in (('scenario', 'scenario'), ('trial_id', 'trial'),
                           ('scope_id', 'scope'), ('phase', 'phase')):
            value = context.get(key)
            if value is not None and value != '':
                clean = REDACTOR.text(value).replace('\n', '\\n')
                pieces.append('[' + label + '=' + clean + ']')
        return (' ' + ' '.join(pieces)) if pieces else ''

    def emit(self, event: str, message: str, *, level: str = "INFO", task: str = "manager", **fields: object):
        with self._lock:
            if self._closed:
                raise LogWriteError("日志已经关闭")
            self.sequence += 1
            now = timestamp()
            safe_message = REDACTOR.text(message)
            safe_task_value = REDACTOR.text(task)
            safe_event_value = REDACTOR.text(event)
            safe_level_value = REDACTOR.text(level)
            payload = {**REDACTOR.value(fields), "schema_version": 1, "seq": self.sequence, "time": now,
                       "batch_id": self.batch_id, "task": safe_task_value, "level": safe_level_value,
                       "event": safe_event_value, "message": safe_message}
            safe_task = safe_task_value.replace('\n', '\\n')
            safe_event = safe_event_value.replace('\n', '\\n')
            safe_level = safe_level_value.replace('\n', '\\n')
            rendered = (f"{now} {safe_level} [{safe_task}] [{safe_event}]"
                        + self._context_text(fields) + ' '
                        + safe_message.replace('\n', '\\n') + '\n').encode('utf-8')
            try:
                self._write_record(self._human, rendered)
                self._write_record(self._automation, rendered)
                self._write_record(self._events, (json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
                console = self._view.flush() + self._view.business(rendered.decode('utf-8'))
                self._write_record(self._console, console.encode('utf-8'))
                self._human.flush()
                self._automation.flush()
                self._events.flush()
                self._console.flush()
                # Progress is reproducible; identity, failure and transitions are
                # the write-ahead evidence needed after an interrupted process.
                if event != "worker_output":
                    os.fsync(self._human.fileno())
                    os.fsync(self._automation.fileno())
                    os.fsync(self._events.fileno())
                    os.fsync(self._console.fileno())
            except (OSError, ValueError) as error:
                raise LogWriteError("批次日志写入失败") from error
            return payload

    @staticmethod
    def _write_record(stream, encoded):
        written = stream.write(encoded)
        if written != len(encoded):
            raise LogWriteError('日志写入未完成')

    def detail(self, task, channel, line, context=None, refresh=False):
        """Append readable native blocks without changing raw stream evidence."""
        with self._lock:
            if self._closed:
                raise LogWriteError('日志已经关闭')
            try:
                rendered = self._view.feed(task, channel, line, context, refresh)
                if rendered:
                    self._write_record(self._console, rendered.encode('utf-8'))
                    self._console.flush()
            except (OSError, ValueError) as error:
                raise LogWriteError('原生详细视图写入失败') from error
            return rendered

    def flush_details(self, task=None, channel=None, final=False):
        """Expire bounded displays while running; flush their last frame at EOF."""
        with self._lock:
            if self._closed:
                raise LogWriteError('日志已经关闭')
            try:
                rendered = self._view.flush(task, channel, final)
                if rendered:
                    self._write_record(self._console, rendered.encode('utf-8'))
                    self._console.flush()
            except (OSError, ValueError) as error:
                raise LogWriteError('原生详细视图写入失败') from error
            return rendered

    def close(self, related_logs=()):
        with self._lock:
            if self._closed:
                return self._archive_result
            failure = None
            try:
                rendered = self._view.flush(final=True)
                if rendered:
                    self._write_record(self._console, rendered.encode('utf-8'))
            except (OSError, ValueError) as error:
                failure = error
            self._closed = True
            for handle in (self._human, self._automation, self._events, self._console):
                if handle is not None:
                    try:
                        handle.flush()
                        os.fsync(handle.fileno())
                    except (OSError, ValueError) as error:
                        failure = error
                    finally:
                        try:
                            handle.close()
                        except (OSError, ValueError) as error:
                            failure = error
            if failure is not None:
                raise LogWriteError("批次日志关闭失败") from failure
            self._archive_result = archive_management_logs(self.batch_path, self.native_roots, related_logs)
            return self._archive_result
