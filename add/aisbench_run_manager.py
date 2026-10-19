#!/usr/bin/env python3
"""Supervise isolated AISBench runs without changing their benchmark contract.

The shell entry point delegates here before reading any configuration.  One
configuration retains the historical inherited-environment contract; two files
are sourced separately into clean tool environments and run concurrently.
"""

from __future__ import annotations

import csv
import hashlib
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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any


TOOL_PARAMETERS = frozenset({
    "CONTAINER_NAME", "EXPECTED_IMAGE", "HOST_OUTPUT_DIR", "MODEL_NAME",
    "MODEL_HOST_PATH", "TOOLS_HOST_DIR", "CONTAINER_OUTPUT_DIR",
    "MODEL_CONTAINER_PATH", "TOOLS_CONTAINER_DIR", "HOST_IP", "HOST_PORT",
    "API_KEY", "API_URL", "FAILED_REQ_LIMIT", "MAX_CONTEXT_LEN", "NPU_NUM",
    "POD_INFO", "DATA_NUM_MULTIPLIER", "PEAK_MIN_CONCURRENCY",
    "PEAK_MAX_MULTIPLIER", "PEAK_DATA_MULTIPLIER", "PEAK_WAIT_SECONDS",
    "PEAK_TTFT_LIMIT", "PEAK_TPOT_LIMIT", "PEAK_HARD_CAP", "SCENARIOS_JSON",
    "SCENARIOS_JSON_OVERRIDE", "SCENARIO_CONFIG_FILE", "ADD_DIR", "PROJECT_ROOT",
})
MANAGED_LABEL = "io.aisbench-auto-tools-prefix.managed"
INTERNAL_PARAMETERS = frozenset({
    "AISBENCH_MANAGED_WORKER", "AISBENCH_PREFLIGHT_REPORT",
    "AISBENCH_RUNTIME_REPORT", "AISBENCH_RUN_TAG",
})
_STOP_REQUESTED = 0
_PRINT_LOCK = threading.Lock()


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


def emit(alias: str, message: str) -> None:
    with _PRINT_LOCK:
        try:
            print(f"[{alias}] {message}", flush=True)
        except (BrokenPipeError, OSError):
            # A closed terminal must not abandon supervised container processes.
            pass


def atomic_json(path: Path, payload: Any) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
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
            timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """Capture a short command while allowing Ctrl+C during preflight."""
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


def isolated_environment(config: Path, private: Path) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if key not in TOOL_PARAMETERS and key not in INTERNAL_PARAMETERS
                   and key not in {"BASH_ENV", "ENV"}}
    capture = private / ("environment-" + uuid.uuid4().hex + ".json")
    # Explicitly export supported parameters after source, including variables
    # without an export keyword.  Keep SCENARIOS_JSON out of every child env.
    exports = " ".join(sorted(TOOL_PARAMETERS - {"SCENARIOS_JSON"}))
    source_code = (
        'source "$1" >/dev/null 2>&1 || exit 40\n'
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
    result = command([bash_executable(), "-c", source_code, "aisbench-config", str(config),
                      sys.executable, capture_code, str(capture)], environment)
    if result.returncode or not capture.is_file():
        raise RunError(f"无法加载配置文件: {config.name}（请检查 Bash 语法和命令退出码）")
    loaded = read_json(capture)
    if not loaded or any(not isinstance(k, str) or not isinstance(v, str)
                         for k, v in loaded.items()):
        raise RunError(f"读取配置环境失败: {config.name}")
    loaded.pop("SCENARIOS_JSON", None)
    # The configuration is trusted Bash, but internal supervisor switches may
    # only be assigned by this process, never inherited or loaded from a file.
    return {key: value for key, value in loaded.items() if key not in INTERNAL_PARAMETERS}


@dataclass
class Job:
    alias: str
    config: Path | None
    environment: dict[str, str]
    worker_config: Path | None = None
    config_sha256: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    daemon_id: str = ""
    run_id: str = ""
    runtime_path: Path | None = None
    launcher_path: Path | None = None
    process: subprocess.Popen[str] | None = None
    reader: threading.Thread | None = None
    log_handle: IO[str] | None = None
    state: str = "pending"
    exit_code: int | None = None
    cleanup: str = "not_needed"

    def status(self) -> dict[str, Any]:
        runtime = read_json(self.runtime_path) if self.runtime_path else {}
        matching = runtime.get("run_id") == self.run_id
        return {
            "task": self.alias,
            "config": str(self.config) if self.config else "",
            "config_sha256": self.config_sha256,
            "container_name": self.metadata.get("container_name", ""),
            "container_id": runtime.get("container_id", "") if matching else "",
            "image": self.metadata.get("expected_image", ""),
            "run_id": self.run_id,
            "state": self.state,
            "exit_code": self.exit_code,
            "cleanup": self.cleanup,
            "result_dir": runtime.get("result_dir", "") if matching else "",
            "launcher_log": str(self.launcher_path) if self.launcher_path else "",
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
        self.handles: list[IO[bytes]] = []

    @property
    def descriptors(self) -> tuple[int, ...]:
        return tuple(handle.fileno() for handle in self.handles)

    def acquire(self, jobs: list[Job]) -> None:
        requested: dict[str, bool] = {}
        for job in jobs:
            requested["container\0" + job.daemon_id + "\0" + job.metadata["container_name"]] = True
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
        except OSError as error:
            self.close()
            raise RunError("容器名或输出目录已被其他压测占用；本批次未启动") from error

    def close(self) -> None:
        for handle in reversed(self.handles):
            handle.close()
        self.handles.clear()

    def __enter__(self) -> "ResourceLocks":
        return self

    def __exit__(self, *_args: Any) -> None:
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


def validate_metadata(raw: dict[str, Any], alias: str) -> dict[str, str]:
    required = {"container_name", "host_output_dir", "model_host_path", "tools_host_dir",
                "expected_image", "host_ip", "host_port", "model_name"}
    if not required.issubset(raw) or any(not isinstance(raw[key], str) or not raw[key]
                                       for key in required):
        raise RunError(f"{alias}: 预检未返回完整资源信息")
    metadata = {key: raw[key] for key in required}
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", metadata["container_name"]):
        raise RunError(f"{alias}: CONTAINER_NAME 格式非法")
    if not re.fullmatch(r"[0-9]+", metadata["host_port"]) or not 1 <= int(metadata["host_port"]) <= 65535:
        raise RunError(f"{alias}: HOST_PORT 须为 1 至 65535 的整数")
    if any(character.isspace() for character in metadata["expected_image"]):
        raise RunError(f"{alias}: EXPECTED_IMAGE 不能包含空白字符")
    for key in ("host_output_dir", "model_host_path", "tools_host_dir"):
        metadata[key] = canonical_path(metadata[key])
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
        shutil.copyfile(job.launcher_path, Path(result) / "launcher.log")
    except OSError:
        emit(job.alias, "launcher.log 保留在启动日志目录，未能复制到场景结果目录")


def stream_output(job: Job) -> None:
    assert job.process is not None and job.process.stdout is not None
    assert job.log_handle is not None
    try:
        for line in job.process.stdout:
            job.log_handle.write(line)
            job.log_handle.flush()
            emit(job.alias, line.rstrip("\r\n"))
    finally:
        job.process.stdout.close()


def write_batch(path: Path | None, batch_id: str, jobs: list[Job]) -> None:
    if path is None:
        return
    statuses = [job.status() for job in jobs]
    atomic_json(path / "batch_results.json", {"batch_id": batch_id, "tasks": statuses})
    temporary = path / "batch_results.csv.tmp"
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(statuses[0]))
        writer.writeheader()
        writer.writerows(statuses)
    os.replace(temporary, path / "batch_results.csv")


def supervise(script: Path, arguments: list[str]) -> int:
    if len(arguments) > 2:
        raise RunError("用法: bash run_perf_scenarios.sh [配置文件 [第二份配置文件]]")
    project_root = script.parent.parent
    dual = len(arguments) == 2
    config_arguments = arguments or [str(script.parent / "env.env")]
    configs = [Path(value).resolve(strict=True) for value in config_arguments]
    if any(not config.is_file() for config in configs):
        raise RunError("配置路径必须指向可读文件")
    if dual and os.path.samefile(configs[0], configs[1]):
        raise RunError("两组测试不能使用同一份配置文件")
    aliases = [task_alias(config) for config in configs] if dual else ["single"]
    if len(set(aliases)) != len(aliases):
        raise RunError("两份配置的任务名冲突，请使用不同配置文件名")
    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:12]
    jobs: list[Job] = []
    batch_path: Path | None = None
    with tempfile.TemporaryDirectory(prefix="aisbench-manager-") as temporary:
        private = Path(temporary)
        for index, alias in enumerate(aliases):
            config = configs[index]
            snapshot = private / (alias + "-config.env")
            config_bytes = config.read_bytes()
            snapshot.write_bytes(config_bytes)
            if dual:
                environment = isolated_environment(snapshot, private)
                environment["CONTAINER_NAME"] = environment.get("CONTAINER_NAME") or "ais_bench-" + alias
                environment["HOST_OUTPUT_DIR"] = (environment.get("HOST_OUTPUT_DIR")
                                                  or str(project_root / "outputs" / alias))
            else:
                environment = {key: value for key, value in os.environ.items()
                               if key not in INTERNAL_PARAMETERS and key != "SCENARIOS_JSON"}
            job = Job(alias, config, environment)
            job.worker_config = snapshot
            job.config_sha256 = hashlib.sha256(config_bytes).hexdigest()
            report = private / (alias + "-preflight.json")
            preflight_environment = {**environment, "AISBENCH_MANAGED_WORKER": "1",
                                     "AISBENCH_PREFLIGHT_REPORT": str(report)}
            result = command([bash_executable(), str(script), str(snapshot)],
                             preflight_environment)
            if result.returncode:
                # Preserve the shell validator's useful messages.  It never
                # dumps the environment/API key; no sourced-file output leaks.
                for line in (result.stdout + result.stderr).splitlines():
                    emit(alias, line)
                raise RunError(f"{alias}: 配置预检失败，两组压测均未启动")
            job.metadata = validate_metadata(read_json(report), alias)
            # Freeze normalized host paths so the worker cannot use a different
            # path than the path protected by the supervisor's resource lock.
            for field_name, variable in (("host_output_dir", "HOST_OUTPUT_DIR"),
                                         ("model_host_path", "MODEL_HOST_PATH"),
                                         ("tools_host_dir", "TOOLS_HOST_DIR")):
                job.environment[variable] = job.metadata[field_name]
            jobs.append(job)
        if dual:
            if jobs[0].metadata["container_name"] == jobs[1].metadata["container_name"]:
                raise RunError("两组 CONTAINER_NAME 相同，请分别配置不同容器名")
            if paths_overlap(jobs[0].metadata["host_output_dir"], jobs[1].metadata["host_output_dir"]):
                raise RunError("两组 HOST_OUTPUT_DIR 相同或互为父子目录，请使用互不重叠目录")
        for job in jobs:
            result = command([docker_executable(job.environment), "info", "--format", "{{.ID}}"], job.environment, 30)
            job.daemon_id = result.stdout.strip()
            if result.returncode or not job.daemon_id or "\n" in job.daemon_id:
                raise RunError(f"{job.alias}: 无法获取 Docker daemon 身份，本批次未启动")
        if _STOP_REQUESTED:
            return 128 + _STOP_REQUESTED
        with ResourceLocks() as locks:
            locks.acquire(jobs)
            if _STOP_REQUESTED:
                return 128 + _STOP_REQUESTED
            if dual:
                batch_path = project_root / "outputs" / "parallel" / batch_id
                batch_path.mkdir(parents=True, exist_ok=False)
                emit("parallel", "两组共享宿主机资源；若指向同一服务，负载和缓存指标会相互影响")
            try:
                for job in jobs:
                    if _STOP_REQUESTED:
                        raise RunError("启动已取消")
                    job.run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:12]
                    job.runtime_path = private / (job.alias + "-runtime.json")
                    log_directory = Path(job.metadata["host_output_dir"]) / ".launcher" / job.run_id
                    log_directory.mkdir(parents=True, exist_ok=False)
                    job.launcher_path = log_directory / "launcher.log"
                    job.log_handle = job.launcher_path.open("x", encoding="utf-8", newline="\n")
                    worker_environment = {**job.environment, "AISBENCH_MANAGED_WORKER": "1",
                                          "AISBENCH_RUN_TAG": job.run_id,
                                          "AISBENCH_RUNTIME_REPORT": str(job.runtime_path)}
                    options = _process_options()
                    if os.name == "posix":
                        options["pass_fds"] = locks.descriptors
                    job.process = subprocess.Popen(
                        [bash_executable(), str(script), str(job.worker_config)],
                        env=worker_environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace", **options)
                    job.state = "running"
                    job.reader = threading.Thread(target=stream_output, args=(job,), daemon=True)
                    job.reader.start()
                    emit(job.alias, f"已启动，启动日志: {job.launcher_path}")
                write_batch(batch_path, batch_id, jobs)
                remaining = list(jobs)
                while remaining:
                    if _STOP_REQUESTED:
                        emit("manager", "收到停止信号，正在停止本批次任务")
                        # Stop container work before ending docker-exec clients.
                        for job in remaining:
                            cleaned = stop_owned_container(job)
                            job.state = "cancelled" if cleaned else "cleanup_failed"
                        for job in remaining:
                            terminate_process(job.process)
                            # The worker may have recorded its container between
                            # the first read and delivery of SIGTERM.  Re-read
                            # after the host group is gone before releasing locks.
                            cleaned = stop_owned_container(job)
                            job.state = "cancelled" if cleaned else "cleanup_failed"
                            job.exit_code = job.process.returncode
                        remaining.clear()
                        break
                    for job in list(remaining):
                        returncode = job.process.poll()
                        if returncode is None:
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
                        remaining.remove(job)
                        write_batch(batch_path, batch_id, jobs)
                    if remaining:
                        time.sleep(0.1)
            except BaseException:
                for job in jobs:
                    if job.process is not None:
                        runtime = read_json(job.runtime_path)
                        complete = runtime.get("run_id") == job.run_id and runtime.get("complete") is True
                        if not complete:
                            stop_owned_container(job)
                        terminate_process(job.process)
                        cleaned = True if complete else stop_owned_container(job)
                        job.exit_code = job.process.returncode
                        job.state = "failed" if cleaned else "cleanup_failed"
                    else:
                        job.state = "not_started"
                raise
            finally:
                for job in jobs:
                    if job.reader is not None:
                        job.reader.join(timeout=10)
                    if job.log_handle is not None:
                        job.log_handle.close()
                    copy_launcher(job)
                write_batch(batch_path, batch_id, jobs)
            if batch_path:
                emit("parallel", f"批次结果: {batch_path / 'batch_results.json'}")
            if _STOP_REQUESTED:
                return 128 + _STOP_REQUESTED
            return 0 if all(job.state == "success" for job in jobs) else 1


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        emit("manager", "缺少 run_perf_scenarios.sh 路径")
        return 1
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, _signal_request)
    try:
        script = Path(arguments.pop(0)).resolve(strict=True)
        if not script.is_file():
            raise RunError("压测入口不是普通文件")
        return supervise(script, arguments)
    except (RunError, OSError) as error:
        emit("manager", f"ERROR: {error}")
        return (128 + _STOP_REQUESTED) if _STOP_REQUESTED else 1


if __name__ == "__main__":
    sys.exit(main())
