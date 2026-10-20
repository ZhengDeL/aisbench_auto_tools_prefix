#!/usr/bin/env python3
"""Supervise isolated AISBench runs without changing their benchmark contract.

The shell entry point delegates here before reading any configuration. Every
file is sourced once into an isolated environment. Zero arguments load config.env
and retain explicitly exported public parameters as compatibility overrides.
"""

from __future__ import annotations

import hashlib
import codecs
import ipaddress
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlsplit

from aisbench_batch_results import prepare_task_outputs, publish_batch, save_effective_config
from aisbench_logging import BatchLog, LogWriteError, REDACTOR, private_binary, validate_output_path
from aisbench_configuration import PUBLIC_ENV_NAMES as CONFIG_PUBLIC_ENV_NAMES


TOOL_PARAMETERS = frozenset(CONFIG_PUBLIC_ENV_NAMES) | frozenset({
    "CONTAINER_OUTPUT_DIR", "MODEL_CONTAINER_PATH", "TOOLS_CONTAINER_DIR", "SCENARIOS_JSON",
    "SCENARIOS_JSON_OVERRIDE", "SCENARIO_CONFIG_FILE", "ADD_DIR", "PROJECT_ROOT",
})
MANAGED_LABEL = "io.aisbench-auto-tools-prefix.managed"
INTERNAL_PARAMETERS = frozenset({
    "AISBENCH_MANAGED_WORKER", "AISBENCH_PREFLIGHT_REPORT",
    "AISBENCH_RUNTIME_REPORT", "AISBENCH_RUN_TAG",
    "AISBENCH_BATCH_ID", "AISBENCH_TASK_NAME", "AISBENCH_TASK_LOG",
    "AISBENCH_EVENTS_PATH",
})
_STOP_REQUESTED = 0
_PRINT_LOCK = threading.Lock()
_BATCH_LOG: BatchLog | None = None
_LOG_ERROR = ""
_NATIVE_ROOTS: list[Path] = []

# Only these names may override the default file in zero-argument mode.
# Internal paths and the literal SCENARIOS_JSON block are never inherited.
PUBLIC_ENV_NAMES = frozenset(CONFIG_PUBLIC_ENV_NAMES)


class RunError(RuntimeError):
    """An actionable, safe-to-display orchestration error."""


def bash_executable() -> str:
    if os.name == "nt":
        bundled = Path("C:/Program Files/Git/bin/bash.exe")
        if bundled.is_file():
            return str(bundled)
    executable = shutil.which("bash")
    if executable:
        return executable
    raise RunError("找不到 Bash，请安装 Bash 后重试")


def docker_executable(environment: dict[str, str]) -> str:
    executable = shutil.which("docker", path=environment.get("PATH"))
    if executable:
        return executable
    raise RunError("找不到 docker 命令，请检查 PATH")


def emit(alias: str, message: str, event: str = "manager_progress", level: str = "INFO", **fields: Any) -> None:
    global _LOG_ERROR
    with _PRINT_LOCK:
        message = REDACTOR.text(message)
        if _BATCH_LOG is not None and not _LOG_ERROR:
            try:
                _BATCH_LOG.emit(event, message, task=alias, level=level, **fields)
            except (OSError, ValueError):
                _LOG_ERROR = "BATCH_LOG_WRITE_FAILED"
                try:
                    print("ERROR [日志写入失败] 停止新轮次并清理本批次工具任务；请检查磁盘与日志目录权限。", file=sys.stderr, flush=True)
                except OSError:
                    pass
        try:
            print(f"[{timestamp()}] [{alias}] {message}", flush=True)
        except (BrokenPipeError, OSError, UnicodeError):
            # A closed terminal must not abandon supervised container processes.
            pass


def check_batch_logging() -> None:
    if _LOG_ERROR:
        raise RunError("批次持久日志写入失败，本批次不能继续运行")


def atomic_json(path: Path, payload: Any) -> None:
    validate_output_path(path, _NATIVE_ROOTS)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def canonical_path(value: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(value)))


def paths_overlap(first: str, second: str) -> bool:
    try:
        return os.path.commonpath((first, second)) in (first, second)
    except ValueError:
        return False


def task_alias(config: Path) -> str:
    name = config.name
    for suffix in (".local.env", ".env"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise RunError("配置文件名须以字母或数字开头，仅使用字母、数字、点、下划线和短横线")
    return name


def safe_slug(value: str, limit: int) -> str:
    """Keep labels readable while distinguishing escaped and shortened values."""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-_") or "model"
    # Reserve the double underscore for the task/model boundary. Otherwise
    # (task=a__b, model=c) and (task=a, model=b__c) share a label.
    slug = re.sub(r"__+", "_", slug)
    if slug != value or len(slug) > limit:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        slug = slug[:limit - len(digest) - 1].rstrip(".-_") + "-" + digest
    return slug


def task_output_label(alias: str, model: str) -> str:
    return safe_slug(alias, 48) + "__" + safe_slug(model, 64)


def service_authority(api_url: str, host: str, port: str) -> tuple[str, str]:
    """Return a credential-free target and a host:port resource key.

    Paths, model names, and HTTP versus HTTPS on the same explicit port do not
    create independent services. DNS aliases and proxy backends need SERVICE_ID;
    resolving them here cannot prove backend independence.
    """
    address = api_url.strip()
    if not address:
        clean_host = host.strip()
        if clean_host.startswith("["):
            clean_host = clean_host[1:]
        if clean_host.endswith("]"):
            clean_host = clean_host[:-1]
        address = "http://" + ("[" + clean_host + "]" if ":" in clean_host else clean_host) + ":" + port
    try:
        if any(character.isspace() or ord(character) < 32 for character in address):
            raise ValueError("URL contains unescaped whitespace")
        parsed = urlsplit(address)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
            raise ValueError("an absolute HTTP(S) URL is required")
        hostname = parsed.hostname.rstrip(".").lower()
        if ":" in hostname and "%25" in hostname:
            # RFC 6874 encodes the zone separator in an IPv6 URL, while a
            # HOST_IP value commonly contains the literal percent character.
            hostname = hostname.replace("%25", "%", 1)
        if not hostname or any(character.isspace() for character in hostname):
            raise ValueError("invalid host")
        try:
            address_object = ipaddress.ip_address(hostname)
            if isinstance(address_object, ipaddress.IPv6Address) and address_object.ipv4_mapped:
                address_object = address_object.ipv4_mapped
            hostname = address_object.compressed
        except ValueError:
            if re.fullmatch(r"[0-9.]+", hostname) or ":" in hostname:
                raise ValueError("invalid IP address") from None
            hostname = hostname.encode("idna").decode("ascii")
            if not re.fullmatch(r"[a-z0-9_][a-z0-9_.-]*", hostname):
                raise ValueError("invalid hostname")
        effective_port = parsed.port if parsed.port is not None else (443 if scheme == "https" else 80)
        if not 1 <= effective_port <= 65535:
            raise ValueError("invalid port")
    except (ValueError, UnicodeError) as error:
        # Do not repeat a URL: it can contain an API key in its query string.
        raise RunError("模型服务地址无效，请使用有效的 HTTP(S) 地址和端口") from error
    authority = ("[" + hostname + "]" if ":" in hostname else hostname) + ":" + str(effective_port)
    return scheme + "://" + authority, authority


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def process_identity(pid: int) -> dict[str, Any]:
    """PID plus kernel start identity; a reused PID is never the old owner."""
    identity = {"pid": pid, "start_identity": "", "boot_id": ""}
    if os.name == "posix":
        try:
            raw = Path("/proc/{}/stat".format(pid)).read_text(encoding="utf-8")
            fields = raw.rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                return identity
            identity["start_identity"] = fields[19]
            identity["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except (OSError, IndexError):
            pass
    elif os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            try:
                exit_code = wintypes.DWORD()
                if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
                    return identity
                times = [wintypes.FILETIME() for _ in range(4)]
                if kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                    identity["start_identity"] = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
            finally:
                kernel.CloseHandle(handle)
    return identity


def _signal_request(signum: int, _frame: Any) -> None:
    global _STOP_REQUESTED
    _STOP_REQUESTED = signum


def _process_options() -> dict[str, Any]:
    if os.name == "posix":
        return {"start_new_session": True}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


def terminate_process(process: subprocess.Popen[Any], timeout: float = 5) -> None:
    if os.name == "posix":
        # The shell can exit before its docker-exec client.  Address the original
        # process group even when its leader has already been reaped.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.wait()
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            process.poll()
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                process.wait()
                return
            time.sleep(0.05)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        return
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    except ProcessLookupError:
        pass


def command(command_line: list[str], environment: dict[str, str],
            timeout: float = 60, diagnostics: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Capture a short command while allowing Ctrl+C during preflight."""
    if diagnostics is not None:
        stdout_path = diagnostics.with_name(diagnostics.name + ".stdout.log")
        stderr_path = diagnostics.with_name(diagnostics.name + ".stderr.log")
        with private_binary(stdout_path, _NATIVE_ROOTS) as stdout, private_binary(stderr_path, _NATIVE_ROOTS) as stderr:
            process = subprocess.Popen(command_line, env=environment, stdout=stdout, stderr=stderr,
                                       **_process_options())
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if _STOP_REQUESTED or time.monotonic() >= deadline:
                    terminate_process(process)
                    stdout.flush()
                    stderr.flush()
                    os.fsync(stdout.fileno())
                    os.fsync(stderr.fileno())
                    raise RunError(f"启动检查{'已取消' if _STOP_REQUESTED else '超时'}，未启动压测；私有诊断: {stderr_path}")
                time.sleep(0.025)
            stdout.flush()
            stderr.flush()
            os.fsync(stdout.fileno())
            os.fsync(stderr.fileno())
        def tail(path: Path) -> str:
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 65536))
                return handle.read().decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(command_line, process.returncode, tail(stdout_path), tail(stderr_path))
    process = subprocess.Popen(command_line, env=environment, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               errors="replace", **_process_options())
    deadline = time.monotonic() + timeout
    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.15)
            return subprocess.CompletedProcess(command_line, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            if _STOP_REQUESTED or time.monotonic() >= deadline:
                terminate_process(process)
                if _STOP_REQUESTED:
                    raise RunError("启动检查已取消")
                raise RunError("启动检查超时，未启动任何压测任务")


def isolated_environment(config: Path, private: Path, diagnostics: Path | None = None) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if key not in TOOL_PARAMETERS and key not in INTERNAL_PARAMETERS
                   and key not in {"BASH_ENV", "ENV"}}
    capture = private / ("environment-" + uuid.uuid4().hex + ".json")
    # Explicitly export supported parameters after source, including variables
    # without an export keyword.  Keep SCENARIOS_JSON out of every child env.
    exports = " ".join(sorted(TOOL_PARAMETERS - {"SCENARIOS_JSON"}))
    source_code = (
        'source "$1" || exit $?\n'
        'unset SCENARIOS_JSON\n'
        f'for parameter in {exports}; do\n'
        '  if [[ -v "$parameter" ]]; then export "$parameter"; fi\n'
        'done\n'
        'exec "$2" -c "$3" "$4"\n'
    )
    capture_code = (
        "import json,os,sys; "
        "open(sys.argv[1],'x',encoding='utf-8').write(json.dumps(dict(os.environ)))"
    )
    diagnostic_base = diagnostics or private / ("source-" + uuid.uuid4().hex)
    for candidate in (private, capture, diagnostic_base):
        validate_output_path(candidate, _NATIVE_ROOTS)
    stdout_path = diagnostic_base.with_name(diagnostic_base.name + ".stdout.raw.log")
    stderr_path = diagnostic_base.with_name(diagnostic_base.name + ".stderr.raw.log")
    with private_binary(stdout_path, _NATIVE_ROOTS) as stdout, private_binary(stderr_path, _NATIVE_ROOTS) as stderr:
        # Source output can contain credentials or an unbounded stream. Spool
        # directly to private files; never discard it or buffer it all in RAM.
        for command_line in ([bash_executable(), "-n", str(config)],
                             [bash_executable(), "-c", source_code, "aisbench-config", str(config),
                              sys.executable, capture_code, str(capture)]):
            process = subprocess.Popen(command_line, env=environment, stdout=stdout, stderr=stderr,
                                       **_process_options())
            deadline = time.monotonic() + 60
            while process.poll() is None:
                if _STOP_REQUESTED or time.monotonic() >= deadline:
                    terminate_process(process)
                    raise RunError("配置加载已取消" if _STOP_REQUESTED else "配置加载超时，未启动任何压测")
                time.sleep(0.025)
            stdout.flush()
            stderr.flush()
            os.fsync(stdout.fileno())
            os.fsync(stderr.fileno())
            if process.returncode:
                raise RunError(f"无法加载配置文件（Bash退出码{process.returncode}）；诊断仅保存在本地私有文件: {stderr_path}")
    if not capture.is_file():
        raise RunError(f"配置未完成加载；本地私有诊断: {stderr_path}")
    loaded = read_json(capture)
    if not loaded or any(not isinstance(k, str) or not isinstance(v, str)
                         for k, v in loaded.items()):
        raise RunError(f"读取配置环境失败: {config.name}")
    loaded.pop("SCENARIOS_JSON", None)
    # The configuration is trusted Bash, but internal supervisor switches may
    # only be assigned by this process, never inherited or loaded from a file.
    result = {key: value for key, value in loaded.items() if key not in INTERNAL_PARAMETERS}
    REDACTOR.register(result.get("API_KEY", ""))
    return result


@dataclass
class Job:
    alias: str
    config: Path | None
    environment: dict[str, str]
    worker_config: Path | None = None
    config_sha256: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    daemon_id: str = ""
    run_id: str = ""
    runtime_path: Path | None = None
    launcher_path: Path | None = None
    process: subprocess.Popen[str] | None = None
    reader: threading.Thread | None = None
    stderr_reader: threading.Thread | None = None
    log_handle: IO[str] | None = None
    stdout_handle: IO[bytes] | None = None
    stderr_handle: IO[bytes] | None = None
    log_lock: Any = field(default_factory=threading.Lock)
    logging_error: str = ""
    parameter_sources: dict[str, str] = field(default_factory=dict)
    diagnostic_paths: dict[str, str] = field(default_factory=dict)
    state: str = "pending"
    exit_code: int | None = None
    cleanup: str = "not_needed"
    output_label: str = ""
    started_at: str = ""
    finished_at: str = ""
    started_monotonic: float | None = None
    finished_monotonic: float | None = None

    def status(self) -> dict[str, Any]:
        runtime = read_json(self.runtime_path) if self.runtime_path else {}
        matching = runtime.get("run_id") == self.run_id
        elapsed = 0.0
        if self.started_monotonic is not None:
            end = self.finished_monotonic if self.finished_monotonic is not None else time.monotonic()
            elapsed = round(max(0.0, end - self.started_monotonic), 3)
        scopes = runtime.get("result_scopes", "") if matching else ""
        if matching and not scopes and runtime.get("result_dir"):
            candidate = Path(runtime["result_dir"]) / "result_scopes.json"
            if candidate.is_file():
                scopes = str(candidate)
        return {
            "task": self.alias,
            "config": str(self.config) if self.config else "",
            "config_sha256": self.config_sha256,
            "model": self.metadata.get("model_name", ""),
            "endpoint": self.metadata.get("service_endpoint", ""),
            "service_id": self.metadata.get("service_id", ""),
            "output_label": self.output_label,
            "host_output_dir": self.metadata.get("host_output_dir", ""),
            "container_name": self.metadata.get("container_name", ""),
            "container_id": runtime.get("container_id", "") if matching else "",
            "image": self.metadata.get("expected_image", ""),
            "run_id": self.run_id,
            "state": self.state,
            "phase": runtime.get("phase", self.state) if matching else self.state,
            "current_scenario": runtime.get("current_scenario", "") if matching else "",
            "planned_tests": self.metadata.get("planned_tests", ""),
            "test_index": runtime.get("test_index", 0) if matching else 0,
            "exit_code": self.exit_code,
            "cleanup": self.cleanup,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": elapsed,
            "result_dir": runtime.get("result_dir", "") if matching else "",
            "launcher_log": str(self.launcher_path) if self.launcher_path else "",
            "logging_error": self.logging_error or (runtime.get("logging_error", "") if matching else ""),
            "parameter_sources": self.parameter_sources,
            "diagnostic_paths": self.diagnostic_paths,
            "planned_scenarios": self.metadata.get("planned_scenarios", []),
            "result_scopes": scopes,
            "scope_metadata": runtime.get("scope_metadata", {}) if matching else {},
        }


class ResourceLocks:
    """Nonblocking cross-checkout locks; descendants share each ancestor lock.

    A run targeting /a/b takes shared locks for / and /a, plus an exclusive
    lock for /a/b.  Thus siblings can run, but a second run targeting /a or
    /a/b/c conflicts.  Keep lock files permanently: unlinking a locked inode
    would allow another process to acquire a different lock for the same key.
    """

    def __init__(self) -> None:
        # TMPDIR is caller-controlled and can differ between terminals.  Linux
        # supervisors must agree on one host-wide location to share locks.
        base = Path("/tmp") if os.name == "posix" else Path(tempfile.gettempdir())
        self.directory = base / "aisbench-auto-tools-locks"
        validate_output_path(self.directory, _NATIVE_ROOTS)
        self.handles: list[IO[bytes]] = []
        self.jobs: list[Job] = []
        self.active: dict[str, Path] = {}
        self.active_records: dict[str, dict[str, Any]] = {}

    @property
    def descriptors(self) -> tuple[int, ...]:
        return tuple(handle.fileno() for handle in self.handles)

    def acquire(self, jobs: list[Job]) -> None:
        self.jobs = jobs
        requested: dict[str, bool] = {}
        for job in jobs:
            requested["container\0" + job.daemon_id + "\0" + job.metadata["container_name"]] = True
            requested["service\0" + job.metadata["service_key"]] = True
            if job.metadata.get("service_id"):
                requested["service-id\0" + job.metadata["service_id"]] = True
            target = Path(job.metadata["host_output_dir"])
            for ancestor in target.parents:
                requested.setdefault("output\0" + str(ancestor), False)
            requested["output\0" + str(target)] = True
        self.directory.mkdir(mode=0o700, exist_ok=True)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise RunError("资源锁目录必须是普通目录")
        try:
            for resource, exclusive in sorted(requested.items()):
                name = hashlib.sha256(resource.encode("utf-8")).hexdigest() + ".lock"
                path = self.directory / name
                flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(path, flags, 0o600)
                handle = os.fdopen(descriptor, "r+b", buffering=0)
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise OSError("lock is not a regular file")
                    if os.name == "posix":
                        import fcntl
                        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                        fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                    else:
                        # Windows is for offline tests; LockFileEx is needed to
                        # retain shared ancestor locks instead of serializing all.
                        _windows_lock(descriptor, exclusive)
                except BaseException:
                    handle.close()
                    raise
                self.handles.append(handle)
            self._check_quarantines(jobs)
            self._check_active(jobs)
        except OSError as error:
            self.close()
            raise RunError("服务、容器名或输出目录已被其他压测占用；本批次未启动") from error
        except RunError:
            self.close()
            raise

    @staticmethod
    def _exclusive_resources(job: Job) -> list[str]:
        resources = ["container\0" + job.daemon_id + "\0" + job.metadata["container_name"],
                     "service\0" + job.metadata["service_key"],
                     "output\0" + job.metadata["host_output_dir"]]
        if job.metadata.get("service_id"):
            resources.append("service-id\0" + job.metadata["service_id"])
        return resources

    def activate(self, job: Job, batch_path: Path, batch_id: str) -> None:
        """Commit before Popen: Docker exec can outlive every inherited flock."""
        record = {"schema_version": 1, "created_at": timestamp(), "run_id": job.run_id,
                  "task": job.alias, "batch_id": batch_id, "batch_path": str(batch_path),
                  "runtime_path": str(job.runtime_path), "owner": process_identity(os.getpid()),
                  "worker": {}, "daemon_id": job.daemon_id, "container_id": "",
                  "container_name": job.metadata["container_name"],
                  "image": job.metadata["expected_image"],
                  "host_output_dir": job.metadata["host_output_dir"],
                  "resources": self._exclusive_resources(job)}
        name = hashlib.sha256((job.daemon_id + "\0" + job.run_id).encode()).hexdigest() + ".active.json"
        target = self.directory / name
        self._persist_record(target, record)
        self.active[job.run_id], self.active_records[job.run_id] = target, record

    @staticmethod
    def _matching_runtime(record: dict[str, Any]) -> dict[str, Any]:
        expected = Path(record["host_output_dir"]) / ".launcher" / record["run_id"] / "runtime.json"
        if canonical_path(record.get("runtime_path", "")) != canonical_path(str(expected)):
            return {}
        runtime = read_json(expected)
        if (runtime.get("run_id") != record["run_id"]
                or runtime.get("container_name") != record["container_name"]
                or runtime.get("image") != record["image"]):
            return {}
        return runtime

    def refresh(self, job: Job) -> None:
        record = self.active_records.get(job.run_id)
        if record is None:
            return
        changed = False
        if job.process is not None and not record["worker"]:
            record["worker"] = process_identity(job.process.pid)
            changed = True
        runtime = self._matching_runtime(record)
        identifier = runtime.get("container_id", "")
        if isinstance(identifier, str) and re.fullmatch(r"[a-fA-F0-9]{64}", identifier) and not record["container_id"]:
            record["container_id"] = identifier
            changed = True
        if changed:
            self._persist_record(self.active[job.run_id], record)

    @staticmethod
    def _complete(record: dict[str, Any], runtime: dict[str, Any]) -> bool:
        identifier = runtime.get("container_id", "")
        return (runtime.get("complete") is True and type(runtime.get("worker_exit")) is int
                and isinstance(identifier, str) and bool(re.fullmatch(r"[a-fA-F0-9]{64}", identifier))
                and (not record.get("container_id") or record["container_id"] == identifier))

    @staticmethod
    def _fixed_stopped(record: dict[str, Any], inspector: Job, identifier: str) -> bool:
        inspected = inspect_container(inspector, identifier)
        if inspected is None:
            return True
        config, state = inspected.get("Config"), inspected.get("State")
        return (inspected.get("Id") == identifier
                and inspected.get("Name", "").lstrip("/") == record["container_name"]
                and isinstance(config, dict) and config.get("Image") == record["image"]
                and isinstance(config.get("Labels"), dict)
                and config["Labels"].get(MANAGED_LABEL) == "true"
                and isinstance(state, dict) and state.get("Running") is False)

    @staticmethod
    def _mark_interrupted(record: dict[str, Any], reason: str, identifier: str) -> None:
        batch = Path(record["batch_path"]) / "batch_state.json"
        state = read_json(batch)
        if state.get("batch_id") != record["batch_id"]:
            raise RunError("旧批次状态身份无法确认，保留资源隔离")
        if state.get("state") in ("preflight", "running", "interrupted", "unknown"):
            state.update(state="interrupted", reason_code="MANAGER_LOST",
                         exit_code=None, recovery_observed_at=timestamp())
            observations = state.setdefault("recovery_observations", {})
            observations[record["run_id"]] = {"reason": reason, "container_id": identifier,
                                             "runtime_path": record["runtime_path"]}
            # Preserve original finish time (normally empty), raw logs and CSVs.
            atomic_json(batch, state)

    def _check_active(self, jobs: list[Job]) -> None:
        for path in self.directory.glob("*.active.json"):
            if path.is_symlink():
                raise RunError("发现无法确认的活动记录，保留资源隔离")
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                # An independent owner may normally release after glob.
                continue
            except (OSError, ValueError) as error:
                raise RunError("活动记录无法读取或损坏，保留资源隔离") from error
            if not isinstance(record, dict):
                raise RunError("活动记录无效，保留资源隔离")
            resources, output = record.get("resources"), record.get("host_output_dir")
            if (record.get("schema_version") != 1 or not isinstance(resources, list) or not resources
                    or any(not isinstance(value, str) for value in resources)
                    or not isinstance(output, str) or not output):
                raise RunError("活动记录无效，保留资源隔离")
            matching = [job for job in jobs if set(resources).intersection(self._exclusive_resources(job))
                        or paths_overlap(output, job.metadata["host_output_dir"])]
            if not matching:
                continue
            message = "此前管理进程中断，旧任务未确认结束，资源保持隔离；请检查原固定ID工具容器"
            if (any(not isinstance(record.get(key), str) or not record[key]
                    for key in ("run_id", "runtime_path", "batch_path", "batch_id", "daemon_id", "container_name", "image"))
                    or not isinstance(record.get("owner"), dict)):
                raise RunError(message)
            owner = record["owner"]
            if (type(owner.get("pid")) is not int or not owner.get("start_identity")):
                raise RunError(message)
            if process_identity(owner["pid"]) == owner:
                raise RunError(message)
            runtime = self._matching_runtime(record)
            identifier = record.get("container_id") or runtime.get("container_id", "")
            complete = self._complete(record, runtime)
            safe = complete
            if not safe and isinstance(identifier, str) and re.fullmatch(r"[a-fA-F0-9]{64}", identifier):
                inspector = next((job for job in matching if job.daemon_id == record["daemon_id"]), None)
                if inspector is not None:
                    try:
                        safe = self._fixed_stopped(record, inspector, identifier)
                    except (OSError, subprocess.SubprocessError, RunError):
                        safe = False
            reason = ("worker_completed_without_manager" if complete else
                      "fixed_container_stopped_or_absent" if safe else
                      "fixed_container_running_or_unknown" if identifier else "container_identity_unknown")
            self._mark_interrupted(record, reason, identifier)
            if not safe:
                raise RunError(message)
            path.unlink()
            self._sync_directory()
            emit("manager", "旧批次管理进程已中断；根据可信完成记录或原固定ID停止证据解除对应隔离")

    def _sync_directory(self) -> None:
        if os.name == "posix":
            descriptor = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _persist_record(self, target: Path, record: dict[str, Any]) -> None:
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            self._sync_directory()
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _release_finished(self) -> None:
        for job in self.jobs:
            path = self.active.get(job.run_id)
            if path is None:
                continue
            self.refresh(job)
            record = self.active_records[job.run_id]
            safe = job.process is None or self._complete(record, self._matching_runtime(record))
            identifier = record.get("container_id", "")
            if not safe and identifier:
                try:
                    safe = self._fixed_stopped(record, job, identifier)
                except (OSError, subprocess.SubprocessError, RunError):
                    safe = False
            if safe:
                path.unlink()
                self._sync_directory()
            else:
                job.state, job.cleanup = "cleanup_failed", "active_lease_unconfirmed"
                emit(job.alias, "本次工具任务结束未能确认，执行前活动记录保留，服务资源继续隔离")

    def _check_quarantines(self, jobs: list[Job]) -> None:
        """Only a verified stopped fixed ID can release a failed cleanup marker.

        Scan output paths as well as exact resource keys: a blocked child output
        must also block a later run using its parent directory. The kernel locks
        already acquired above serialize every overlapping claimant.
        """
        for path in self.directory.glob("*.blocked.json"):
            if path.is_symlink():
                raise RunError("发现无法确认的资源隔离记录，本批次未启动")
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as error:
                raise RunError("资源隔离记录无法读取或损坏，保留隔离；本批次未启动") from error
            if not isinstance(record, dict):
                raise RunError("资源隔离记录无效，保留隔离；本批次未启动")
            resources = record.get("resources")
            output = record.get("host_output_dir")
            if (record.get("schema_version") != 1 or not isinstance(resources, list)
                    or not resources or any(not isinstance(item, str) for item in resources)
                    or not isinstance(output, str) or not output):
                raise RunError("资源隔离记录无效，保留隔离；本批次未启动")
            blocked_resources = set(resources)
            matching = [job for job in jobs
                        if blocked_resources.intersection(self._exclusive_resources(job))
                        or paths_overlap(output, job.metadata["host_output_dir"])]
            if not matching:
                continue
            identifier = record.get("container_id", "")
            daemon = record.get("daemon_id", "")
            container_name, image = record.get("container_name", ""), record.get("image", "")
            inspector = next((job for job in matching if job.daemon_id == daemon), None)
            message = "此前任务清理未确认，资源保持隔离；请检查原固定ID工具容器后重试"
            if (not isinstance(identifier, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", identifier)
                    or not isinstance(daemon, str) or not daemon or inspector is None
                    or not isinstance(container_name, str) or not container_name
                    or not isinstance(image, str) or not image):
                raise RunError(message)
            try:
                inspected = inspect_container(inspector, identifier)
            except (OSError, subprocess.SubprocessError, RunError) as error:
                raise RunError(message) from error
            if inspected is not None:
                config, state = inspected.get("Config"), inspected.get("State")
                if (inspected.get("Id") != identifier
                        or inspected.get("Name", "").lstrip("/") != container_name
                        or not isinstance(config, dict) or config.get("Image") != image
                        or not isinstance(config.get("Labels"), dict)
                        or config["Labels"].get(MANAGED_LABEL) != "true"
                        or not isinstance(state, dict) or state.get("Running") is not False):
                    raise RunError(message)
            # Never remove a lock inode. The independent marker can disappear
            # only after this read-only fixed-ID proof under the claimant's lock.
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            emit("manager", "已确认此前固定ID工具容器停止或不存在，解除对应资源隔离")

    def _write_quarantine(self, job: Job) -> None:
        runtime = read_json(job.runtime_path) if job.runtime_path else {}
        identifier = runtime.get("container_id", "")
        if (runtime.get("run_id") != job.run_id
                or runtime.get("container_name") != job.metadata["container_name"]
                or runtime.get("image") != job.metadata["expected_image"]
                or not isinstance(identifier, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", identifier)):
            identifier = ""  # Unknown identity must remain blocked, never guessed by name.
        record = {"schema_version": 1, "created_at": timestamp(), "run_id": job.run_id,
                  "daemon_id": job.daemon_id, "container_id": identifier,
                  "container_name": job.metadata["container_name"],
                  "image": job.metadata["expected_image"],
                  "host_output_dir": job.metadata["host_output_dir"],
                  "resources": self._exclusive_resources(job), "reason": "cleanup_unconfirmed"}
        identity = json.dumps([job.daemon_id, job.run_id, job.alias], ensure_ascii=True).encode("utf-8")
        name = hashlib.sha256(identity).hexdigest() + ".blocked.json"
        target = self.directory / name
        self._persist_record(target, record)

    def _quarantine_failed_jobs(self) -> None:
        for job in self.jobs:
            if job.state != "cleanup_failed":
                continue
            if job.run_id in self.active and self.active[job.run_id].is_file():
                # New runs already committed the stronger pre-execution lease.
                # Keep legacy blocked markers readable without duplicating an
                # unknown-ID marker over a known fixed ID retained in a lease.
                continue
            last_warning = float("-inf")
            while True:
                try:
                    self._write_quarantine(job)
                    emit(job.alias, "清理未确认，已隔离本机对应服务、容器和输出资源")
                    break
                except OSError:
                    # Releasing an ordinary flock here would permit overlapping
                    # requests. Keep it until the persistent block is durable.
                    now = time.monotonic()
                    if now - last_warning >= 10:
                        emit(job.alias, "资源隔离记录写入失败，保持资源锁并等待重试；请恢复锁目录可写")
                        last_warning = now
                    time.sleep(1)

    def close(self) -> None:
        for handle in reversed(self.handles):
            handle.close()
        self.handles.clear()

    def __enter__(self) -> "ResourceLocks":
        return self

    def __exit__(self, *_args: Any) -> None:
        try:
            self._release_finished()
            self._quarantine_failed_jobs()
        finally:
            self.close()


def _windows_lock(descriptor: int, exclusive: bool) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    lock = ctypes.WinDLL("kernel32", use_last_error=True).LockFileEx
    lock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                     wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(OVERLAPPED)]
    lock.restype = wintypes.BOOL
    flags = 1 | (2 if exclusive else 0)  # FAIL_IMMEDIATELY, EXCLUSIVE_LOCK
    if not lock(msvcrt.get_osfhandle(descriptor), flags, 0, 1, 0, ctypes.byref(OVERLAPPED())):
        raise OSError(ctypes.get_last_error(), "resource already locked")


def validate_metadata(raw: dict[str, Any], alias: str,
                      environment: dict[str, str] | None = None) -> dict[str, Any]:
    required = {"container_name", "host_output_dir", "model_host_path", "tools_host_dir",
                "expected_image", "host_ip", "host_port", "model_name"}
    if not required.issubset(raw) or any(not isinstance(raw[key], str) or not raw[key]
                                       for key in required):
        raise RunError(f"{alias}: 预检未返回完整资源信息")
    metadata = {key: raw[key] for key in required}
    for key in ("planned_tests", "fixed_count", "peak_count"):
        if key in raw:
            if not isinstance(raw[key], int) or isinstance(raw[key], bool) or raw[key] < 0:
                raise RunError(f"{alias}: 预检任务数量无效")
            metadata[key] = raw[key]
    if "effective_config" in raw:
        if not isinstance(raw["effective_config"], dict):
            raise RunError(f"{alias}: 预检有效配置无效")
        metadata["effective_config"] = raw["effective_config"]
    planned = raw.get("planned_scenarios")
    if planned is None:
        effective = raw.get("effective_config", {})
        planned = [line.split("|", 1)[0] for key in ("expanded_fixed", "expanded_peak")
                   for line in effective.get(key, []) if isinstance(line, str)] if isinstance(effective, dict) else []
    if not isinstance(planned, list) or any(not isinstance(name, str) or not name for name in planned):
        raise RunError(f"{alias}: 预检场景计划无效")
    metadata["planned_scenarios"] = planned
    if planned and (len(planned) != len(set(planned)) or len(planned) != metadata.get("planned_tests", len(planned))):
        raise RunError(f"{alias}: 预检场景身份或数量不一致")
    environment = environment or {}
    for field_name, variable in (("api_url", "API_URL"), ("service_id", "SERVICE_ID")):
        value = raw.get(field_name, environment.get(variable, ""))
        if not isinstance(value, str):
            raise RunError(f"{alias}: {variable} 须为字符串")
        metadata[field_name] = value.strip()
    if (len(metadata["service_id"]) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in metadata["service_id"])):
        raise RunError(f"{alias}: SERVICE_ID 过长或包含控制字符")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", metadata["container_name"]):
        raise RunError(f"{alias}: CONTAINER_NAME 格式非法")
    if not re.fullmatch(r"[0-9]+", metadata["host_port"]) or not 1 <= int(metadata["host_port"]) <= 65535:
        raise RunError(f"{alias}: HOST_PORT 须为 1 至 65535 的整数")
    if any(character.isspace() for character in metadata["expected_image"]):
        raise RunError(f"{alias}: EXPECTED_IMAGE 不能包含空白字符")
    metadata["service_endpoint"], metadata["service_key"] = service_authority(
        metadata["api_url"], metadata["host_ip"], metadata["host_port"])
    metadata["endpoint"] = metadata["service_endpoint"]
    for key in ("host_output_dir", "model_host_path", "tools_host_dir"):
        metadata[key] = canonical_path(metadata[key])
    validate_output_path(metadata["host_output_dir"], [*_NATIVE_ROOTS, metadata["tools_host_dir"]])
    for key in ("model_host_path", "tools_host_dir"):
        if not Path(metadata[key]).is_dir():
            raise RunError(f"{alias}: {key} 目录不存在")
    output = Path(metadata["host_output_dir"])
    if output.exists() and not output.is_dir():
        raise RunError(f"{alias}: HOST_OUTPUT_DIR 不是目录")
    return metadata


def inspect_container(job: Job, identifier: str) -> dict[str, Any] | None:
    result = subprocess.run([docker_executable(job.environment), "inspect", identifier], env=job.environment,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=20)
    if result.returncode:
        if re.search(r"no such (?:object|container)", result.stderr, re.I):
            return None
        raise RunError("无法查询本次容器状态")
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise RunError("容器状态响应无效")
        return payload[0]
    except (ValueError, IndexError, TypeError) as error:
        raise RunError("容器状态响应无效") from error


def validate_container_preflight(job: Job, diagnostics: Path) -> None:
    """Reject known startup errors before committing a workload lease.

    The planned name is only inspected for this new launch. Recovery above
    continues to use the previous lease's fixed ID, never a name lookup.
    """
    result = command([docker_executable(job.environment), "image", "inspect", "--format", "{{.Id}}",
                      job.metadata["expected_image"]], job.environment, 30, diagnostics=diagnostics)
    if result.returncode or not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", result.stdout.strip()):
        raise RunError(f"{job.alias}: 本地压测镜像不存在或无法读取，请先拉取配置镜像；未启动压测")
    existing = inspect_container(job, job.metadata["container_name"])
    if existing is None:
        return
    config, state = existing.get("Config"), existing.get("State")
    if (not isinstance(existing.get("Id"), str) or not re.fullmatch(r"[a-fA-F0-9]{64}", existing["Id"])
            or existing.get("Name", "").lstrip("/") != job.metadata["container_name"]
            or not isinstance(config, dict) or not isinstance(config.get("Labels"), dict)
            or config["Labels"].get(MANAGED_LABEL) != "true"
            or not isinstance(config.get("Image"), str)
            or not (config["Image"] == job.metadata["expected_image"]
                    or config["Image"].startswith("ghcr.io/aisbench/aisbench_benchmark"))
            or not isinstance(state, dict) or type(state.get("Running")) is not bool):
        raise RunError(f"{job.alias}: 同名容器无法确认属于本工具或镜像不匹配，未启动压测，未修改容器")


def stop_owned_container(job: Job) -> bool:
    """Stop only the fixed, managed container recorded for this worker."""
    runtime = read_json(job.runtime_path) if job.runtime_path else {}
    identifier = runtime.get("container_id", "")
    if not identifier:
        job.cleanup = "no_container_recorded"
        return True
    if (runtime.get("run_id") != job.run_id
            or runtime.get("container_name") != job.metadata["container_name"]
            or runtime.get("image") != job.metadata["expected_image"]
            or not isinstance(identifier, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", identifier)):
        job.cleanup = "refused_identity_mismatch"
        emit(job.alias, "无法确认本次容器身份，未执行容器停止")
        return False
    try:
        inspected = inspect_container(job, identifier)
        if inspected is None:
            job.cleanup = "already_removed"
            return True
        config = inspected.get("Config") or {}
        labels = config.get("Labels") or {}
        if (inspected.get("Id") != identifier
                or inspected.get("Name", "").lstrip("/") != job.metadata["container_name"]
                or config.get("Image") != job.metadata["expected_image"]
                or labels.get(MANAGED_LABEL) != "true"):
            job.cleanup = "refused_identity_mismatch"
            emit(job.alias, "固定 ID 的容器身份校验失败，未执行容器停止")
            return False
        if not (inspected.get("State") or {}).get("Running"):
            job.cleanup = "already_stopped"
            return True
        emit(job.alias, "停止本次压测容器中的任务，保留容器与结果")
        result = subprocess.run([docker_executable(job.environment), "stop", "--time", "5", identifier],
                                env=job.environment, capture_output=True, text=True,
                                timeout=25)
        inspected = inspect_container(job, identifier)
        if inspected is None or not (inspected.get("State") or {}).get("Running"):
            job.cleanup = "stopped"
            return True
        job.cleanup = "stop_failed"
        emit(job.alias, f"容器停止未确认（docker 返回 {result.returncode}），需要人工检查")
    except (OSError, subprocess.SubprocessError, RunError):
        job.cleanup = "stop_failed"
        emit(job.alias, "容器停止未确认，需要人工检查")
    return False


def copy_launcher(job: Job) -> None:
    runtime = read_json(job.runtime_path) if job.runtime_path else {}
    value = runtime.get("result_dir")
    if runtime.get("run_id") != job.run_id or not isinstance(value, str) or not value:
        return
    result = canonical_path(value)
    output = job.metadata["host_output_dir"]
    if result == output or not paths_overlap(output, result) or not Path(result).is_dir():
        return
    if os.path.commonpath((output, result)) != output:
        return
    try:
        validate_output_path(Path(result) / "launcher.log", _NATIVE_ROOTS)
        shutil.copyfile(job.launcher_path, Path(result) / "launcher.log")
    except (OSError, ValueError):
        job.logging_error = "LAUNCHER_COPY_FAILED"
        emit(job.alias, "launcher.log 保留在启动日志目录，未能复制到场景结果目录")


def stream_output(job: Job, channel: str = "stdout") -> None:
    assert job.process is not None
    pipe = job.process.stdout if channel == "stdout" else job.process.stderr
    raw = job.stdout_handle if channel == "stdout" else job.stderr_handle
    assert pipe is not None and raw is not None
    assert job.log_handle is not None
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    last_console_progress = float("-inf")
    last_raw_sync = time.monotonic()

    def display(line: str) -> None:
        nonlocal last_console_progress
        safe = REDACTOR.text(line)
        with job.log_lock:
            job.log_handle.write(f"{timestamp()} [{channel}] {safe}\n")
            job.log_handle.flush()
        # Raw output belongs in the task's diagnostics, not repeated as large
        # structured batch events. The console still identifies its task.
        # Carriage-return progress remains complete in the private raw and
        # launcher files; keep a busy benchmark from flooding the terminal.
        if re.search(r"Progress:|\bPOST=|\bRECV=|\d+%\|", safe):
            now = time.monotonic()
            if now - last_console_progress < 2:
                return
            last_console_progress = now
        with _PRINT_LOCK:
            try:
                print(f"[{job.alias}][{channel}] {safe}", flush=True)
            except (OSError, UnicodeError):
                pass

    try:
        while True:
            chunk = pipe.read1(65536)
            if not chunk:
                break
            raw.write(chunk)
            raw.flush()
            if time.monotonic() - last_raw_sync >= 1:
                os.fsync(raw.fileno())
                last_raw_sync = time.monotonic()
            pending += decoder.decode(chunk)
            while True:
                newline = re.search(r"\r\n|\r|\n", pending)
                if newline is None or (newline.group(0) == "\r" and newline.end() == len(pending)):
                    break
                line, pending = pending[:newline.start()], pending[newline.end():]
                display(line)
            if len(pending) >= 65536:
                display(pending)
                pending = ""
        pending += decoder.decode(b"", final=True)
        if pending:
            display(pending.rstrip("\r\n"))
        raw.flush()
        os.fsync(raw.fileno())
    except Exception as error:
        job.logging_error = "TASK_LOG_WRITE_FAILED"
        emit(job.alias, "任务原始或详细日志写入失败，停止本任务工具负载；请检查磁盘与权限。",
             "task_logging_failed", "ERROR", stream=channel, error_type=type(error).__name__)
    finally:
        try:
            pipe.close()
        except (OSError, ValueError):
            job.logging_error = job.logging_error or "LOG_STREAM_CLOSE_FAILED"


def write_batch(path: Path | None, batch_id: str, jobs: list[Job],
                final: bool = False) -> list[str]:
    if path is None:
        return []
    statuses = [job.status() for job in jobs]
    return publish_batch(path, batch_id, statuses, final=final)


def finish_job(job: Job, state: str, exit_code: int | None) -> None:
    job.state = state
    job.exit_code = exit_code
    job.finished_at = timestamp()
    job.finished_monotonic = time.monotonic()


def abort_job(job: Job, cancelled: bool) -> None:
    """End one owned workload; re-read its identity after its shell is gone."""
    assert job.process is not None
    runtime = read_json(job.runtime_path) if job.runtime_path else {}
    complete = (not cancelled and not job.logging_error and runtime.get("run_id") == job.run_id
                and runtime.get("complete") is True)
    if not complete:
        stop_owned_container(job)
    host_stopped = True
    try:
        terminate_process(job.process)
    except (OSError, subprocess.SubprocessError):
        host_stopped = False
        emit(job.alias, "宿主机压测进程停止未确认，需要人工检查")
    # Runtime can first appear between the first read and SIGTERM delivery.
    cleaned = True if complete else stop_owned_container(job)
    if not host_stopped or not cleaned:
        state = "cleanup_failed"
    else:
        state = "cancelled" if cancelled else "failed"
    finish_job(job, state, job.process.returncode)


def abort_jobs(jobs: list[Job], cancelled: bool, path: Path | None, batch_id: str,
               all_jobs: list[Job]) -> None:
    """Bound stop latency by parallelizing independent fixed-ID cleanup."""
    if not jobs:
        return
    for job in jobs:
        job.state = "cancelling" if cancelled else "cleaning_up"
    report_failed = False

    def report_cleanup() -> None:
        nonlocal report_failed
        try:
            write_batch(path, batch_id, all_jobs)
        except Exception:
            # An output failure must never prevent stopping owned workloads.
            if not report_failed:
                emit("manager", "停止期间无法更新批次文件，继续停止本批次压测")
                report_failed = True

    report_cleanup()
    with ThreadPoolExecutor(max_workers=min(32, len(jobs)), thread_name_prefix="aisbench-stop") as executor:
        pending = {executor.submit(abort_job, job, cancelled) for job in jobs}
        last_refresh = time.monotonic()
        while pending:
            done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for result in done:
                result.result()
            if done or time.monotonic() - last_refresh >= 0.5:
                report_cleanup()
                last_refresh = time.monotonic()


def validate_job_conflicts(jobs: list[Job]) -> None:
    """Locks protect batches from each other; check every pair within a batch."""
    for index, job in enumerate(jobs):
        for previous in jobs[:index]:
            pair = f"{previous.alias} / {job.alias}"
            if job.metadata["container_name"] == previous.metadata["container_name"]:
                raise RunError(f"{pair}: CONTAINER_NAME 相同，请使用不同容器名")
            if paths_overlap(job.metadata["host_output_dir"], previous.metadata["host_output_dir"]):
                raise RunError(f"{pair}: HOST_OUTPUT_DIR 相同或互为父子目录，请使用互不重叠目录")
            if job.metadata["service_key"] == previous.metadata["service_key"]:
                raise RunError(f"{pair}: 指向同一个模型服务，禁止同时压测（模型别名不区分服务）")
            if (job.metadata["service_id"]
                    and job.metadata["service_id"] == previous.metadata["service_id"]):
                raise RunError(f"{pair}: SERVICE_ID 相同，禁止同时压测同一后端")


def _supervise(script: Path, arguments: list[str], batch_path: Path, batch_id: str,
               jobs: list[Job], batch_state: dict[str, Any]) -> int:
    if not script.is_file():
        raise RunError("压测入口不是可读普通文件，本批次未启动")
    project_root = script.parent
    explicit = bool(arguments)
    config_arguments = arguments or [str(script.parent / "config.env")]
    configs = [Path(value).resolve(strict=True) for value in config_arguments]
    if any(not config.is_file() for config in configs):
        raise RunError("配置路径必须指向可读文件")
    for index, config in enumerate(configs):
        if any(os.path.samefile(config, previous) for previous in configs[:index]):
            raise RunError("同批任务不能重复使用同一份配置文件")
    aliases = [task_alias(config) for config in configs]
    if len(set(aliases)) != len(aliases):
        raise RunError("配置的任务名冲突，请使用不同配置文件名")
    publication_errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix=".private-manager-", dir=batch_path) as temporary:
        private = Path(temporary)
        for index, alias in enumerate(aliases):
            check_batch_logging()
            config = configs[index]
            snapshot = private / (str(index) + "-config.env")
            config_bytes = config.read_bytes()
            snapshot.write_bytes(config_bytes)
            emit(alias, f"加载配置并检查场景: {config}", "config_loading", config=str(config), input_index=index + 1)
            diagnostic = batch_path / "logs" / "preflight" / f"{index + 1:04d}"
            environment = isolated_environment(snapshot, private, diagnostic)
            sources = {key: "config_file" if key in environment else "code_default" for key in PUBLIC_ENV_NAMES}
            if not explicit:
                # Preserve exported overrides by presence; '0' and an explicit
                # empty credential must not disappear in a truthiness check.
                for key in PUBLIC_ENV_NAMES:
                    if key in os.environ:
                        environment[key] = os.environ[key]
                        sources[key] = "parent_export"
                if "SCENARIOS_JSON_OVERRIDE" in os.environ:
                    environment["SCENARIOS_JSON_OVERRIDE"] = os.environ["SCENARIOS_JSON_OVERRIDE"]
            REDACTOR.register(environment.get("API_KEY", ""))
            default_container = not environment.get("CONTAINER_NAME")
            default_output = not environment.get("HOST_OUTPUT_DIR")
            environment["CONTAINER_NAME"] = environment.get("CONTAINER_NAME") or "ais_bench-" + alias
            environment["HOST_OUTPUT_DIR"] = (environment.get("HOST_OUTPUT_DIR")
                                              or str(batch_path / "tasks" / alias))
            job = Job(alias, config, environment)
            job.parameter_sources = sources
            job.diagnostic_paths = {"config_stdout": str(diagnostic) + ".stdout.raw.log",
                                    "config_stderr": str(diagnostic) + ".stderr.raw.log"}
            job.worker_config = snapshot
            job.config_sha256 = hashlib.sha256(config_bytes).hexdigest()
            report = private / (str(index) + "-preflight.json")
            preflight_environment = {**environment, "AISBENCH_MANAGED_WORKER": "1",
                                     "AISBENCH_PREFLIGHT_REPORT": str(report)}
            result = command([bash_executable(), str(script), str(snapshot)],
                             preflight_environment, diagnostics=diagnostic.with_name(diagnostic.name + ".preflight"))
            for channel in ("stdout", "stderr"):
                path = diagnostic.with_name(diagnostic.name + f".preflight.{channel}.log")
                job.diagnostic_paths["preflight_" + channel] = str(path)
            if result.returncode:
                # Preserve the shell validator's useful messages.  It never
                # dumps the environment/API key; no sourced-file output leaks.
                for line in (result.stdout + result.stderr).splitlines():
                    emit(alias, line)
                raise RunError(f"{alias}: 配置预检失败，本批次所有压测均未启动")
            raw_metadata = read_json(report)
            model = raw_metadata.get("model_name", "")
            job.output_label = task_output_label(alias, model if isinstance(model, str) else "")
            if default_container:
                raw_metadata["container_name"] = "aisbench-" + safe_slug(alias, 48) + "-" + batch_id
                environment["CONTAINER_NAME"] = raw_metadata["container_name"]
                sources["CONTAINER_NAME"] = "derived_default"
            if default_output:
                raw_metadata["host_output_dir"] = str(batch_path / "tasks" / job.output_label)
                sources["HOST_OUTPUT_DIR"] = "derived_default"
            job.metadata = validate_metadata(raw_metadata, alias, environment)
            job.metadata["parameter_sources"] = sources
            # Freeze normalized host paths so the worker cannot use a different
            # path than the path protected by the supervisor's resource lock.
            for field_name, variable in (("host_output_dir", "HOST_OUTPUT_DIR"),
                                         ("model_host_path", "MODEL_HOST_PATH"),
                                         ("tools_host_dir", "TOOLS_HOST_DIR")):
                job.environment[variable] = job.metadata[field_name]
            effective = job.metadata.get("effective_config")
            if isinstance(effective, dict):
                effective.update(CONTAINER_NAME=job.metadata["container_name"],
                                 HOST_OUTPUT_DIR=job.metadata["host_output_dir"],
                                 MODEL_HOST_PATH=job.metadata["model_host_path"],
                                 TOOLS_HOST_DIR=job.metadata["tools_host_dir"])
            jobs.append(job)
            emit(alias, "配置和场景预检通过，尚未启动压测。", "config_validated", parameter_sources=sources)
        if len({job.output_label for job in jobs}) != len(jobs):
            raise RunError("任务与模型生成的输出标记重复，请调整任务文件名")
        validate_job_conflicts(jobs)
        for job in jobs:
            diagnostic = batch_path / "logs" / "preflight" / (job.output_label + ".docker-info")
            result = command([docker_executable(job.environment), "info", "--format", "{{.ID}}"], job.environment, 30,
                             diagnostics=diagnostic)
            job.diagnostic_paths.update(docker_stdout=str(diagnostic) + ".stdout.log",
                                        docker_stderr=str(diagnostic) + ".stderr.log")
            job.daemon_id = result.stdout.strip()
            if result.returncode or not job.daemon_id or "\n" in job.daemon_id:
                raise RunError(f"{job.alias}: 无法获取 Docker daemon 身份，本批次未启动")
        if _STOP_REQUESTED:
            return 128 + _STOP_REQUESTED
        with ResourceLocks() as locks:
            locks.acquire(jobs)
            check_batch_logging()
            if _STOP_REQUESTED:
                return 128 + _STOP_REQUESTED
            for job in jobs:
                diagnostic = batch_path / "logs" / "preflight" / (job.output_label + ".image-inspect")
                job.diagnostic_paths.update(image_stdout=str(diagnostic) + ".stdout.log",
                                            image_stderr=str(diagnostic) + ".stderr.log")
                validate_container_preflight(job, diagnostic)
            for job in jobs:
                prepare_task_outputs(Path(job.metadata["host_output_dir"]), job.output_label)
            if batch_path:
                for job in jobs:
                    task_path = batch_path / "tasks" / job.output_label
                    task_path.mkdir(parents=True, exist_ok=True)
                    save_effective_config(task_path, job.metadata, job.environment, job.config_sha256)
                emit("parallel", f"共 {len(jobs)} 组独立服务并行压测，共享宿主机客户端资源")
            try:
                batch_state.update(state="running", updated_at=timestamp())
                atomic_json(batch_path / "batch_state.json", batch_state)
                for job in jobs:
                    if _STOP_REQUESTED:
                        raise RunError("启动已取消")
                    job.run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:12]
                    validate_output_path(job.metadata["host_output_dir"], _NATIVE_ROOTS)
                    log_directory = validate_output_path(
                        Path(job.metadata["host_output_dir"]) / ".launcher" / job.run_id,
                        [*_NATIVE_ROOTS, job.metadata["tools_host_dir"]])
                    log_directory.mkdir(parents=True, exist_ok=False, mode=0o700)
                    # Identity/progress must outlive the temporary sourced
                    # environment: final publication and incident recovery
                    # still need the original fixed container ID and paths.
                    job.runtime_path = log_directory / "runtime.json"
                    job.launcher_path = log_directory / "launcher.log"
                    job.log_handle = job.launcher_path.open("x", encoding="utf-8", newline="\n")
                    os.chmod(job.launcher_path, 0o600)
                    job.stdout_handle = private_binary(log_directory / "stdout.raw.log", _NATIVE_ROOTS)
                    job.stderr_handle = private_binary(log_directory / "stderr.raw.log", _NATIVE_ROOTS)
                    job.diagnostic_paths.update(stdout=str(log_directory / "stdout.raw.log"),
                                                stderr=str(log_directory / "stderr.raw.log"))
                    worker_environment = {**job.environment, "AISBENCH_MANAGED_WORKER": "1",
                                          "AISBENCH_RUN_TAG": job.run_id,
                                          "AISBENCH_RUNTIME_REPORT": str(job.runtime_path),
                                          "AISBENCH_BATCH_ID": batch_id, "AISBENCH_TASK_NAME": job.alias,
                                          "AISBENCH_TASK_LOG": str(Path(job.metadata["host_output_dir"]) / (job.output_label + "__execution.log")),
                                          "AISBENCH_EVENTS_PATH": str(log_directory / "worker_events.jsonl")}
                    options = _process_options()
                    if os.name == "posix":
                        options["pass_fds"] = locks.descriptors
                    job.started_at = timestamp()
                    job.started_monotonic = time.monotonic()
                    locks.activate(job, batch_path, batch_id)
                    job.process = subprocess.Popen(
                        [bash_executable(), str(script), str(job.worker_config)],
                        env=worker_environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
                    locks.refresh(job)
                    job.state = "running"
                    job.reader = threading.Thread(target=stream_output, args=(job,), daemon=True)
                    job.reader.start()
                    job.stderr_reader = threading.Thread(target=stream_output, args=(job, "stderr"), daemon=True)
                    job.stderr_reader.start()
                    emit(job.alias, f"已启动，启动日志: {job.launcher_path}", "task_started", run_id=job.run_id)
                    check_batch_logging()
                write_batch(batch_path, batch_id, jobs)
                last_refresh = time.monotonic()
                remaining = list(jobs)
                while remaining:
                    if _LOG_ERROR:
                        abort_jobs(remaining, False, batch_path, batch_id, jobs)
                        remaining.clear()
                        break
                    if _STOP_REQUESTED:
                        emit("manager", "收到停止信号，正在停止本批次任务")
                        abort_jobs(remaining, True, batch_path, batch_id, jobs)
                        remaining.clear()
                        break
                    for job in list(remaining):
                        locks.refresh(job)
                        runtime = read_json(job.runtime_path)
                        if runtime.get("run_id") == job.run_id and runtime.get("logging_error"):
                            job.logging_error = "WORKER_LOG_WRITE_FAILED"
                        if job.logging_error:
                            abort_jobs([job], False, batch_path, batch_id, jobs)
                            remaining.remove(job)
                            continue
                        returncode = job.process.poll()
                        if returncode is None:
                            continue
                        # Drain and validate the tail before declaring success.
                        # A reader failure can occur after the worker exits.
                        terminate_process(job.process)
                        for reader in (job.reader, job.stderr_reader):
                            if reader is not None:
                                reader.join(timeout=10)
                                if reader.is_alive():
                                    job.logging_error = "LOG_READER_DID_NOT_FINISH"
                        if job.logging_error:
                            abort_jobs([job], False, batch_path, batch_id, jobs)
                            remaining.remove(job)
                            continue
                        job.exit_code = returncode
                        runtime = read_json(job.runtime_path)
                        complete = runtime.get("run_id") == job.run_id and runtime.get("complete") is True
                        if not complete:
                            cleaned = stop_owned_container(job)
                            job.state = "failed" if cleaned else "cleanup_failed"
                        else:
                            job.state = "success" if returncode == 0 else "failed"
                        terminate_process(job.process)
                        if not complete:
                            cleaned = stop_owned_container(job)
                            job.state = "failed" if cleaned else "cleanup_failed"
                        finish_job(job, job.state, returncode)
                        emit(job.alias, f"任务结束：状态={job.state}，退出码={returncode}。", "task_finished",
                             state=job.state, exit_code=returncode, cleanup=job.cleanup)
                        remaining.remove(job)
                        write_batch(batch_path, batch_id, jobs)
                        last_refresh = time.monotonic()
                    if remaining:
                        if time.monotonic() - last_refresh >= 0.5:
                            write_batch(batch_path, batch_id, jobs)
                            last_refresh = time.monotonic()
                        time.sleep(0.1)
            except BaseException:
                unfinished = []
                for job in jobs:
                    if job.process is not None:
                        if job.finished_monotonic is not None:
                            continue
                        unfinished.append(job)
                    else:
                        finish_job(job, "not_started", None)
                abort_jobs(unfinished, bool(_STOP_REQUESTED), batch_path, batch_id, jobs)
                raise
            finally:
                for job in jobs:
                    for reader in (job.reader, job.stderr_reader):
                        if reader is not None:
                            reader.join(timeout=10)
                            if reader.is_alive():
                                job.logging_error = "LOG_READER_DID_NOT_FINISH"
                    for handle in (job.log_handle, job.stdout_handle, job.stderr_handle):
                        if handle is not None:
                            try:
                                handle.flush()
                                os.fsync(handle.fileno())
                                handle.close()
                            except (OSError, ValueError):
                                job.logging_error = "TASK_LOG_CLOSE_FAILED"
                    if job.logging_error and job.state == "success":
                        job.state = "failed"
                    copy_launcher(job)
                    if job.logging_error and job.state == "success":
                        job.state = "failed"
                publication_errors = write_batch(batch_path, batch_id, jobs, final=True)
            if batch_path:
                emit("parallel", f"批次结果: {batch_path / 'batch_results.json'}")
            if _STOP_REQUESTED:
                return 128 + _STOP_REQUESTED
            for error in publication_errors:
                emit("parallel", error)
            return 0 if all(job.state == "success" and not job.logging_error for job in jobs) and not publication_errors and not _LOG_ERROR else 1


def _emergency_log(batch_id: str, error: BaseException) -> None:
    """Leave a durable startup failure if the intended output cannot be used."""
    candidates = [Path(tempfile.gettempdir())]
    candidates.extend([Path("C:/Windows/Temp")] if os.name == "nt" else [Path("/var/tmp"), Path("/tmp")])
    for base in candidates:
        try:
            safe = validate_output_path(base, _NATIVE_ROOTS)
            if not safe.is_dir():
                continue
            emergency = Path(tempfile.mkdtemp(prefix="aisbench-startup-failure-", dir=safe))
            logger = BatchLog(emergency / batch_id, batch_id, _NATIVE_ROOTS)
            logger.emit("bootstrap_failed", "正常结果日志目录不可用，本批次未启动任何压测。",
                        level="ERROR", error_type=type(error).__name__, reason=REDACTOR.text(str(error)))
            logger.close()
            print(f"ERROR: 无法建立批次日志，未启动压测。原因: {REDACTOR.text(str(error))}；应急日志: {logger.directory}", file=sys.stderr, flush=True)
            return
        except (OSError, ValueError):
            continue
    try:
        print("ERROR: 所有持久日志位置均不可用，本批次未启动任何压测；请检查磁盘、权限和原生目录隔离。", file=sys.stderr, flush=True)
    except OSError:
        pass


def supervise(script: Path, arguments: list[str]) -> int:
    global _BATCH_LOG, _LOG_ERROR, _NATIVE_ROOTS
    _LOG_ERROR = ""
    release_root = script.parent
    # Protect the authoritative release even if a caller supplies a bogus
    # script path whose derived output root lies inside the native subtree.
    module_root = Path(__file__).resolve().parent
    if module_root.name == "core":
        module_root = module_root.parent
    _NATIVE_ROOTS = [module_root.parent / "aisbench_auto_tools_prefix",
                     release_root / "aisbench_auto_tools_prefix"]
    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:12]
    batch_path = release_root / "outputs" / "default" / batch_id
    try:
        _BATCH_LOG = BatchLog(batch_path, batch_id, _NATIVE_ROOTS)
    except (OSError, ValueError) as error:
        _emergency_log(batch_id, error)
        return 1
    jobs: list[Job] = []
    state = {"schema_version": 1, "batch_id": batch_id, "state": "preflight",
             "started_at": timestamp(), "finished_at": "", "exit_code": None,
             "owner": process_identity(os.getpid()),
             "reason_code": "", "logging_error": "", "log_dir": str(_BATCH_LOG.directory),
             "configuration_count": len(arguments) or 1,
             "configurations": arguments or [str(script.parent / "config.env")]}
    code = 1
    try:
        atomic_json(batch_path / "batch_state.json", state)
        emit("manager", f"批次 {batch_id} 开始；先验证全部配置，日志目录: {_BATCH_LOG.directory}", "batch_started",
             configuration_count=len(arguments) or 1, mode="explicit" if arguments else "default_with_export_overrides")
        check_batch_logging()
        code = _supervise(script, arguments, batch_path, batch_id, jobs, state)
        state["state"] = "cancelled" if _STOP_REQUESTED else ("success" if code == 0 else "failed")
        state["reason_code"] = "USER_CANCELLED" if _STOP_REQUESTED else ("COMPLETED" if code == 0 else "TASK_OR_ARTIFACT_FAILED")
    except Exception as error:
        state.update(state="cancelled" if _STOP_REQUESTED else "failed",
                     reason_code="USER_CANCELLED" if _STOP_REQUESTED else "PREFLIGHT_OR_SUPERVISION_FAILED")
        code = (128 + _STOP_REQUESTED) if _STOP_REQUESTED else 1
        # Raw source diagnostics remain private. Never render arbitrary command
        # output, complete environment values or a traceback containing a URL.
        safe_error = str(error) if isinstance(error, (RunError, OSError, ValueError)) else type(error).__name__
        state["error_message"] = REDACTOR.text(safe_error)
        emit("manager", f"批次失败：{safe_error}", "batch_failed", "ERROR", error_type=type(error).__name__)
    finally:
        for job in jobs:
            if job.process is None and job.state == "pending":
                finish_job(job, "not_started", None)
        try:
            # Preflight failures (including zero jobs) still have a result index.
            final_errors = write_batch(batch_path, batch_id, jobs, final=True)
            if final_errors:
                code = code or 1
                state.update(state="failed", reason_code="BATCH_ARTIFACT_INVALID")
                emit("manager", "结果发布或原始证据核验失败；请查看各任务result_index.json中的错误分类与证据路径。",
                     "batch_artifact_invalid", "ERROR", artifact_error_count=len(final_errors))
        except Exception as error:
            code = code or 1
            state.update(state="failed", reason_code="BATCH_ARTIFACT_WRITE_FAILED")
            emit("manager", "批次结果索引写入失败；已有私有日志保留，不能认定测试成功。", "batch_artifact_failed", "ERROR", error_type=type(error).__name__)
        if _LOG_ERROR:
            code = code or 1
            state.update(state="failed", logging_error=_LOG_ERROR, reason_code=_LOG_ERROR)
        state.update(exit_code=code, workload_finished_at=timestamp())
        emit("manager", f"任务执行阶段结束：{state['state']}，开始封存日志；最终批次状态见 {batch_path / 'batch_state.json'}",
             "batch_workload_finished", workload_state=state["state"], exit_code=code,
             reason_code=state["reason_code"], log_archive_pending=True)
        if _LOG_ERROR:
            code = code or 1
            state.update(state="failed", logging_error=_LOG_ERROR, reason_code=_LOG_ERROR, exit_code=code)
        try:
            related_logs = []
            for job in jobs:
                related_logs.extend(job.diagnostic_paths.values())
                if job.launcher_path is not None and job.launcher_path.is_file():
                    related_logs.append(job.launcher_path)
                output = job.metadata.get('host_output_dir')
                if output and job.output_label:
                    for suffix in ('__execution.log', '__launcher.log'):
                        candidate = Path(output) / (job.output_label + suffix)
                        if candidate.is_file():
                            related_logs.append(candidate)
            state['log_archive'] = _BATCH_LOG.close(related_logs)
        except (OSError, ValueError) as error:
            code = code or 1
            state.update(state="failed", logging_error="BATCH_LOG_CLOSE_FAILED", reason_code="BATCH_LOG_CLOSE_FAILED", exit_code=code)
            state['log_archive_error_type'] = type(error).__name__
            try:
                print('ERROR: 日志关闭或分段封存失败，原始日志保留；请检查批次日志目录和batch_state.json，不能视为成功。',
                      file=sys.stderr, flush=True)
            except OSError:
                pass
        state['finished_at'] = timestamp()
        try:
            atomic_json(batch_path / "batch_state.json", state)
        except OSError:
            code = code or 1
            state.update(state='failed', exit_code=code, reason_code='BATCH_STATE_WRITE_FAILED')
            try:
                print("ERROR: 批次终态未能写入；请检查日志和磁盘，不能视为成功。", file=sys.stderr, flush=True)
            except OSError:
                pass
        try:
            archive = state.get('log_archive') or {}
            print(f"[{timestamp()}] [manager] 批次结束：{state['state']}，退出码={code}，结果目录={batch_path}，"
                  f"日志封存索引={archive.get('index', '未完成，请检查错误和原始日志')}", flush=True)
        except (BrokenPipeError, OSError, UnicodeError):
            pass
        _BATCH_LOG = None
    return code


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        emit("manager", "缺少 run_benchmark.sh 路径")
        return 1
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, _signal_request)
    try:
        script = Path(arguments.pop(0)).resolve()
        return supervise(script, arguments)
    except (RunError, OSError) as error:
        emit("manager", f"ERROR: {error}")
        return (128 + _STOP_REQUESTED) if _STOP_REQUESTED else 1


if __name__ == "__main__":
    sys.exit(main())
