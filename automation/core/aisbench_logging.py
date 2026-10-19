"""Private local diagnostics, safe human messages and durable batch events.

Only the manager writes a BatchLog. Raw subprocess files are separate, private
evidence; they are never copied into the human log or a published source tree.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import threading
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
                  if path.is_file() and path.suffix in {'.log', '.jsonl'} and 'archive' not in path.relative_to(logs).parts]
    candidates.extend(Path(path) for path in related_logs)
    sources = []
    seen = set()
    for path in candidates:
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
        self._events = None
        self._archive_result = None
        try:
            self._human = private_binary(self.directory / "batch.log", native_roots)
            self._events = private_binary(self.directory / "events.jsonl", native_roots)
        except BaseException:
            if self._human is not None:
                self._human.close()
            raise

    def emit(self, event: str, message: str, *, level: str = "INFO", task: str = "manager", **fields: object) -> None:
        with self._lock:
            if self._closed:
                raise LogWriteError("日志已经关闭")
            self.sequence += 1
            now = timestamp()
            safe_message = REDACTOR.text(message)
            payload = {"schema_version": 1, "seq": self.sequence, "time": now,
                       "batch_id": self.batch_id, "task": task, "level": level,
                       "event": event, "message": safe_message, **REDACTOR.value(fields)}
            try:
                self._human.write(f"{now} {level} [{task}] [{event}] {safe_message}\n".encode("utf-8"))
                self._events.write((json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
                self._human.flush()
                self._events.flush()
                # Progress is reproducible; identity, failure and transitions are
                # the write-ahead evidence needed after an interrupted process.
                if event != "worker_output":
                    os.fsync(self._human.fileno())
                    os.fsync(self._events.fileno())
            except (OSError, ValueError) as error:
                raise LogWriteError("批次日志写入失败") from error

    def close(self, related_logs=()):
        with self._lock:
            if self._closed:
                return self._archive_result
            self._closed = True
            failure = None
            for handle in (self._human, self._events):
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
