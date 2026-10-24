"""Run a private native copy, recording each benchmark invocation separately.

The private child instruments subprocess.run and applies narrowly recorded
compatibility changes only to its per-trial runtime copy. Original native source
files remain untouched. Each repeat and prefix preparation has explicit evidence.
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import runpy
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from aisbench_result_adapter import read_last_result, read_accuracy_scope
_ACTIVE_PROCESSES = []
_CONSOLE_FAILURES = {}


def disable_console_mirror(descriptor, error):
    _CONSOLE_FAILURES[descriptor] = dict(
        stream="stdout" if descriptor == 1 else "stderr",
        error_type=type(error).__name__, errno=getattr(error, "errno", None))
    try:
        sink = os.open(os.devnull, os.O_WRONLY)
        if sink != descriptor:
            try:
                os.dup2(sink, descriptor)
            finally:
                os.close(sink)
    except OSError:
        pass


def console_message(message):
    """Status text is optional; the independent raw files own the evidence."""
    try:
        print(message, flush=True)
    except (OSError, ValueError, UnicodeError) as error:
        disable_console_mirror(1, error)


class RawConsoleMirror:
    """Best-effort bounded-memory tails; console I/O never owns raw capture."""

    def __init__(self, stdout_path, stderr_path, enabled):
        self.enabled = enabled
        self.paths = {1: Path(stdout_path), 2: Path(stderr_path)}
        self.done = threading.Event()
        self.notice = threading.Event()
        self.stopped = {1: threading.Event(), 2: threading.Event()}
        self.lock = threading.Lock()
        self.failures = list(_CONSOLE_FAILURES.values())
        self.threads = {}
        self.sealed = False
        if not enabled:
            return
        for descriptor in self.paths:
            if descriptor in _CONSOLE_FAILURES:
                self.stopped[descriptor].set()
            else:
                reader = threading.Thread(target=self._tail, args=(descriptor,), daemon=True)
                self.threads[descriptor] = reader
                reader.start()

    def _save_failure(self, failures):
        try:
            atomic_json(self.paths[1].parent / "console_forwarding.json",
                        dict(schema_version=1, state="degraded", raw_capture_enabled=True,
                             console_complete=False, failures=failures))
        except (OSError, ValueError):
            # Raw-file failures remain fatal in the mandatory capture path.
            pass

    def _degrade(self, descriptor, error, reason):
        with self.lock:
            if self.sealed or self.stopped[descriptor].is_set():
                return
            self.stopped[descriptor].set()
            disable_console_mirror(descriptor, error)
            self.failures.append(dict(_CONSOLE_FAILURES[descriptor], reason=reason))

    def _tail(self, descriptor):
        try:
            with self.paths[descriptor].open("rb", buffering=0) as source:
                while not self.stopped[descriptor].is_set():
                    block = source.read(65536)
                    if not block:
                        if self.done.is_set():
                            return
                        self.notice.wait(.02)
                        self.notice.clear()
                        continue
                    pending = memoryview(block)
                    while pending and not self.stopped[descriptor].is_set():
                        try:
                            written = os.write(descriptor, pending)
                        except InterruptedError:
                            continue
                        if written <= 0:
                            raise OSError("console write made no progress")
                        pending = pending[written:]
        except (OSError, ValueError) as error:
            self._degrade(descriptor, error, "console tail write/read failed")

    def finish(self):
        if not self.enabled:
            return
        self.done.set()
        self.notice.set()
        # A blocked observer must not extend a request/process deadline or
        # keep the engine alive. Each tail owns at most one 64KiB block.
        deadline = time.monotonic() + .25
        for reader in self.threads.values():
            reader.join(timeout=max(0, deadline - time.monotonic()))
        for descriptor, reader in self.threads.items():
            if reader.is_alive():
                self._degrade(descriptor, TimeoutError("console tail did not drain"),
                              "console backpressure; bounded 0.25s final drain exhausted")
        with self.lock:
            self.sealed = True
            failures = list(self.failures)
        if failures:
            # Only the controlling thread commits evidence, once all observers
            # are finished or sealed. Late blocked writes cannot mutate files
            # after the caller creates its artifact manifest.
            self._save_failure(failures)


def split_native_pipeline(command):
    """Split only the exact upstream performance command template."""
    suffix = " 2>&1 | tee -a aisbench.log | tee -a aisbench_all.log"
    template = (r"ais_bench --models vllm_api_chat_temp --datasets "
                r"gsm8k_gen_0_shot_cot_str_perf --mode perf --summarizer \S+ "
                r"--work-dir .+ --debug --num-warmups 0")
    if command.endswith(suffix) and re.fullmatch(template, command[:-len(suffix)]):
        return command[:-len(suffix)], True
    return command, False


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def request_count(text, label):
    return request_count_lines(text.splitlines(), label)


def request_count_lines(lines, label):
    value = None
    for line in lines:
        plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
        match = re.search(r"\b" + re.escape(label) + r"\b(.*)", plain)
        if not match:
            continue
        value = None
        token = re.fullmatch(r"\s*(?:[|│]\s*(?:total\s*[|│]\s*)?|[:=]\s*)?([+\-]?\d[\d,]*(?:\.\d+)?)\s*(?:[|│].*)?", match.group(1))
        if token:
            raw = token.group(1)
            if "," in raw and not re.fullmatch(r"[+\-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", raw):
                continue
            try:
                from decimal import Decimal
                number = Decimal(raw.replace(",", ""))
                if number >= 0 and number == number.to_integral_value():
                    value = int(number)
            except (ValueError, ArithmeticError):
                pass
    return value


def request_count_file(path, label):
    with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
        return request_count_lines(handle, label)


class StreamingLogReader:
    """The pinned native parser iterates readlines once; avoid materializing it."""
    def __init__(self, handle):
        self.handle = handle
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return self.handle.__exit__(*args)
    def readlines(self):
        return iter(self.handle)
    def __iter__(self):
        return iter(self.handle)
    def __next__(self):
        return next(self.handle)
    def __getattr__(self, name):
        return getattr(self.handle, name)


def streaming_native_open(filename, mode="r", *args, **kwargs):
    handle = open(filename, mode, *args, **kwargs)
    return StreamingLogReader(handle) if "r" in mode and "b" not in mode else handle


def option(arguments, name, default=None):
    value = default
    for index, token in enumerate(arguments):
        if token == name and index + 1 < len(arguments):
            value = arguments[index + 1]
        elif token.startswith(name + "="):
            value = token.split("=", 1)[1]
    return value


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def restore_generation_template(native_root, evidence_dir, source_root, arguments):
    """Restore only the pinned template's lost native substitution marker."""
    native_root, evidence_dir, source_root = map(lambda p: Path(p).resolve(),
                                               (native_root, evidence_dir, source_root))
    metadata_file = evidence_dir / "generation_template.json"
    target, source = native_root / "default_api.py", source_root / "default_api.py"
    metadata = dict(schema_version=1, compatibility="ac06-generation-placeholder-v1",
                    runtime_file=str(target), source_file=str(source),
                    evidence_file=str(metadata_file), state="checking", action="none",
                    original_sha256=None, patched_sha256=None, requests_started=False,
                    stage="before_native_execution",
                    requested=dict(enable_think="--enable_think" in arguments,
                                   test_accuracy="--test_accuracy" in arguments))
    try:
        if (native_root != evidence_dir / "native_runtime" or native_root == source_root
                or source_root in native_root.parents or source_root == evidence_dir
                or source_root in evidence_dir.parents):
            raise ValueError("generation compatibility requires an isolated per-trial native_runtime copy")
        if target.is_symlink() or source.is_symlink():
            raise ValueError("generation template must not be a symlink")
        if not target.exists():
            if source.exists():
                raise ValueError("canonical generation template was not copied into the private runtime")
            metadata.update(state="not_applicable", action="template_not_present")
            atomic_json(metadata_file, metadata)
            return metadata
        if not source.is_file():
            raise ValueError("generation template must be copied from a regular canonical source file")
        original = target.read_bytes()
        metadata.update(original_sha256=hashlib.sha256(original).hexdigest(),
                        source_sha256=file_digest(source))
        if metadata["original_sha256"] != metadata["source_sha256"]:
            raise ValueError("private generation template differs from its canonical source before adaptation")
        tree = ast.parse(original.decode("utf-8"), filename=str(target))
        keywords = [node for node in ast.walk(tree) if isinstance(node, ast.keyword)
                    and node.arg == "generation_kwargs"]
        if len(keywords) != 1:
            raise ValueError("generation template has missing or ambiguous generation_kwargs")
        value = keywords[0].value
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name) or value.func.id != "dict":
            raise ValueError("unsupported generation_kwargs template structure")
        marker = b"generation_kwargs_for_replace"
        if marker in original:
            if (original.count(marker) != 1 or value.keywords or len(value.args) != 1
                    or not isinstance(value.args[0], ast.Name) or value.args[0].id != marker.decode()):
                raise ValueError("legacy generation marker is ambiguous or outside its dict argument")
            patched, action = original, "legacy_placeholder_preserved"
        else:
            expected = "10bf4220fa786299c3b0f6296fce1e9ad830dc2c8f4aabe1865ef9812774c778"
            block = (b"generation_kwargs=dict(\n            temperature=0,\n"
                     b"            ignore_eos=True,\n        )")
            replacement = (b"generation_kwargs=dict(\n            generation_kwargs_for_replace\n        )")
            if metadata["original_sha256"] != expected or original.count(block) != 1:
                raise ValueError("unknown generation template; only exact pinned ac06 bytes may be restored")
            patched, action = original.replace(block, replacement, 1), "restored_known_ac06_placeholder"
        original_file, patched_file = (evidence_dir / "generation_template.original.py",
                                       evidence_dir / "generation_template.patched.py")
        original_file.write_bytes(original)
        patched_file.write_bytes(patched)
        if patched != original:
            target.write_bytes(patched)
        # The canonical source remains byte-identical even if a future caller
        # changes the runtime layout; never silently broaden the patch target.
        if file_digest(source) != metadata["source_sha256"]:
            raise ValueError("canonical generation template changed during private adaptation")
        metadata.update(state="ready", action=action, patched_sha256=file_digest(target),
                        original_snapshot=str(original_file), patched_snapshot=str(patched_file),
                        native_modifier_sha256=file_digest(native_root / "aisbench_test.py")
                        if (native_root / "aisbench_test.py").is_file() else None)
        atomic_json(metadata_file, metadata)
        return metadata
    except (OSError, ValueError, SyntaxError, UnicodeError) as error:
        metadata.update(state="invalid", reason=str(error))
        atomic_json(metadata_file, metadata)
        raise


def prepare_accuracy_dataset(native_root, evidence_dir, arguments, source_root):
    """Preserve genuine GSM8K questions and gold answers for accuracy mode."""
    if "--test_accuracy" not in arguments:
        return list(arguments), None
    explicit = option(arguments, "--dataset", "none")
    injected = explicit == "none"
    source = source_root / "GSM8K.jsonl" if injected else Path(explicit)
    if not source.is_absolute():
        source = source_root / source
    source = source.resolve()
    metadata = dict(schema_version=1, state="preparing", source_file=str(source),
                    mode="native_gsm8k_subset" if injected else "explicit_dataset",
                    input_len_policy="original_questions_without_synthetic_padding",
                    requested_input_len=option(arguments, "--input_len", "3500"),
                    source_line_numbers=[], sample_count=0, skipped_invalid_lines=[])
    destination = evidence_dir / "accuracy_dataset.jsonl" if injected else source
    metadata["dataset_file"] = str(destination)
    try:
        metadata["source_sha256"] = file_digest(source)
        requested = int(option(arguments, "--data_num", "8192"))
        if requested < 1:
            raise ValueError("accuracy data_num must be positive")
        selected = []
        with source.open("rb") as handle:
            for line_number, raw in enumerate(handle, 1):
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw.decode("utf-8-sig"))
                    if not isinstance(record, dict) or not isinstance(record.get("question"), str) or not record["question"].strip():
                        raise ValueError("question must be a non-empty string")
                    answer = record.get("answer")
                    if not isinstance(answer, str) or "#### " not in answer or not answer.split("#### ", 1)[1].strip():
                        raise ValueError("answer must contain a non-empty original '#### ' gold answer")
                except (UnicodeError, ValueError) as error:
                    if not injected:
                        raise ValueError("accuracy dataset line " + str(line_number) + ": " + str(error)) from error
                    metadata["skipped_invalid_lines"].append(line_number)
                    continue
                selected.append(raw.rstrip(b"\r\n") + b"\n")
                metadata["source_line_numbers"].append(line_number)
                if injected and len(selected) == requested:
                    break
        if not selected:
            raise ValueError("accuracy dataset contains no valid GSM8K question/gold records")
        if injected and len(selected) != requested:
            raise ValueError("accuracy requested " + str(requested) + " samples but source has only "
                             + str(len(selected)) + " valid records")
        if injected:
            with destination.open("wb") as handle:
                for raw in selected:
                    handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        metadata.update(state="ready", sample_count=len(selected), dataset_sha256=file_digest(destination),
                        data_num_applied=injected, synthetic_labels=False)
        atomic_json(evidence_dir / "accuracy_dataset.json", metadata)
        # Last option wins, preserving custom-dataset full-row semantics.
        return [*arguments, "--dataset", str(destination)], metadata
    except (OSError, ValueError) as error:
        metadata.update(state="invalid", reason=str(error), requests_started=False)
        atomic_json(evidence_dir / "accuracy_dataset.json", metadata)
        raise


def configure_dataset_profile(native_root, evidence_dir, arguments, source_root):
    """Namespace native caches by effective input-generation parameters.

    Native normal-cache filenames omit seed and length distributions.  Changing
    only the private copied config prevents those false hits, while a stable
    directory preserves native cache reuse for identical input profiles.
    """
    try:
        config = importlib.import_module("config")
    except ModuleNotFoundError as error:
        if error.name != "config":
            raise
        config = None
    if config is None or not hasattr(config, "DATASET_PATH"):
        profile = dict(schema_version=1, mode="not_configured", fingerprint=None)
        atomic_json(evidence_dir / "dataset_profile.json", profile)
        return profile
    external = option(arguments, "--dataset", "none")
    if external != "none":
        profile = dict(schema_version=1, mode="external_dataset", fingerprint=None,
                       dataset=str(Path(external).resolve()), cache_applied=False)
        atomic_json(evidence_dir / "dataset_profile.json", profile)
        return profile
    integers = {"input_len": 3500, "data_num": 8192, "seed": 1, "dp": 1, "prefix_num": 1}
    parameters = {key: int(option(arguments, "--" + key, default)) for key, default in integers.items()}
    parameters["dataset_type"] = option(arguments, "--dataset_type", "normal")
    ratio = str(option(arguments, "--repeat_rate", "0")).strip()
    parameters["repeat_rate"] = float(ratio[:-1]) / 100 if ratio.endswith("%") else float(ratio)
    for key in ("length_mean", "length_min", "length_max"):
        value = option(arguments, "--" + key)
        parameters[key] = int(value) if value is not None else None
    std = option(arguments, "--length_std")
    parameters["length_std"] = float(std) if std is not None else None
    # These two bounds are swapped by the pinned native sampler.
    if parameters["length_min"] is not None and parameters["length_max"] is not None:
        parameters["length_min"], parameters["length_max"] = sorted((parameters["length_min"], parameters["length_max"]))
    model_path = Path(getattr(config, "MODEL_PATH", ""))
    if not model_path.is_absolute():
        model_path = source_root / model_path
    model_path = model_path.resolve()
    tokenizer_files = {}
    for name in ("tokenizer_config.json", "tokenizer.json", "special_tokens_map.json", "added_tokens.json",
                 "tokenizer.model", "vocab.json", "vocab.txt", "merges.txt", "config.json"):
        path = model_path / name
        if path.is_file():
            tokenizer_files[name] = file_digest(path)
    parameters["tokenizer"] = dict(path=str(model_path), files=tokenizer_files)
    parameters["native_generation"] = {
        name: file_digest(native_root / name) for name in
        # Length sampling lives in the entry module, so upstream sampler
        # changes must invalidate datasets just like generator changes.
        ("aisbench_test.py", "generate_dataset.py", "data_picker.py", "GSM8K.jsonl")
        if (native_root / name).is_file()}
    canonical = json.dumps(parameters, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    cache_root = Path(config.DATASET_PATH)
    if not cache_root.is_absolute():
        cache_root = source_root / cache_root
    cache_root = cache_root.resolve()
    if cache_root == source_root or source_root in cache_root.parents or cache_root == native_root or native_root in cache_root.parents:
        cache_root = evidence_dir.parent / "dataset_cache"
    directory = (cache_root / "profiles" / fingerprint).resolve()
    if directory == source_root or source_root in directory.parents or directory == native_root or native_root in directory.parents:
        raise ValueError("input cache profile must stay outside native source")
    preexisting = directory.is_dir()
    directory.mkdir(parents=True, exist_ok=True)
    profile = dict(schema_version=1, mode="generated", fingerprint=fingerprint, parameters=parameters,
                   directory=str(directory), profile_directory_preexisted=preexisting,
                   cache_applied=True, native_cache_reuse="unchanged_within_profile")
    atomic_json(evidence_dir / "dataset_profile.json", profile)
    atomic_json(directory / "dataset_profile.json", dict(schema_version=1, fingerprint=fingerprint, parameters=parameters))
    # This file is already a per-trial copy.  Keep the imported module and its
    # on-disk config consistent for native code and later forensic inspection.
    with (native_root / "config.py").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n# Runtime-only input-cache namespace; original source is untouched.\nDATASET_PATH = "
                     + json.dumps(str(directory), ensure_ascii=False) + "\n")
    config.DATASET_PATH = str(directory)
    return profile


def stop_process(process):
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        if process.poll() is not None:
            return
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait(timeout=10)
    if os.name == "posix":
        # A shell leader may have exited while descendants still own its pipes.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def stream_process(command, cwd, stdout_path, stderr_path, *, timeout=None, echo=False,
                   group=True, tee_paths=(), chunk_index=None, **kwargs):
    """Keep raw streams mandatory; a disconnected console is only a mirror."""
    errors = []
    from contextlib import ExitStack
    with ExitStack() as stack:
        out = stack.enter_context(Path(stdout_path).open("wb"))
        err = stack.enter_context(Path(stderr_path).open("wb"))
        tees = [stack.enter_context(Path(path).open("ab")) for path in tee_paths]
        journal = stack.enter_context(Path(chunk_index).open("w", encoding="utf-8")) if chunk_index else None
        write_lock = threading.Lock()
        sequence = [0]
        options = dict(cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if group and os.name == "posix":
            options["start_new_session"] = True
        options.update(kwargs)
        process = subprocess.Popen(command, **options)
        _ACTIVE_PROCESSES.append(process)
        mirror = RawConsoleMirror(stdout_path, stderr_path, echo)
        stack.callback(mirror.finish)

        def drain(pipe, target, descriptor):
            try:
                while True:
                    block = pipe.read1(65536) if hasattr(pipe, "read1") else pipe.read(65536)
                    if not block:
                        break
                    with write_lock:
                        offset = target.tell()
                        target.write(block)
                        target.flush()
                        for tee in tees:
                            tee.write(block)
                            tee.flush()
                        if journal:
                            sequence[0] += 1
                            journal.write(json.dumps(dict(sequence=sequence[0], stream="stdout" if descriptor == 1 else "stderr",
                                                          offset=offset, size=len(block))) + "\n")
                            journal.flush()
                    mirror.notice.set()
            except BaseException as error:
                errors.append(str(error))
                stop_process(process)
            finally:
                pipe.close()

        readers = [threading.Thread(target=drain, args=(process.stdout, out, 1), daemon=True),
                   threading.Thread(target=drain, args=(process.stderr, err, 2), daemon=True)]
        for reader in readers:
            reader.start()
        timed_out = False
        try:
            process.wait(timeout=timeout if timeout and timeout > 0 else None)
        except subprocess.TimeoutExpired:
            timed_out = True
            stop_process(process)
        except BaseException:
            stop_process(process)
            raise
        for reader in readers:
            reader.join(timeout=10)
        if any(reader.is_alive() for reader in readers):
            errors.append("output reader did not finish")
            stop_process(process)
            for reader in readers:
                reader.join(timeout=3)
        out.flush()
        err.flush()
        os.fsync(out.fileno())
        os.fsync(err.fileno())
        for tee in tees:
            tee.flush()
            os.fsync(tee.fileno())
        _ACTIVE_PROCESSES.remove(process)
    if errors:
        raise OSError("; ".join(errors))
    return process.returncode, timed_out


def artifact_manifest(root):
    files = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_symlink():
            raise ValueError("artifact symlinks are not accepted")
        if path.is_file() and path.name != "artifact_manifest.json":
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            files.append(dict(path=str(path.relative_to(root)), size=path.stat().st_size, sha256=digest.hexdigest()))
    atomic_json(Path(root) / "artifact_manifest.json", {"files": files})
    return files


def collect_subtask_diagnostics(scope, parser_log):
    """Recognize the image's explicit failed runner task, even with exit 0."""
    paths = [Path(parser_log)]
    paths.extend(sorted(Path(scope["artifact_dir"]).rglob("*.out")))
    failures, exceptions = [], []
    runner_failure = re.compile(r"\[RUNNER-TASK(?:-\d+)?\].*\btask failed with code\s+([1-9]\d*)\b", re.I)
    exception_line = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception):\s*.+")
    for path in paths:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line).strip()
                failed = runner_failure.search(plain)
                evidence = dict(file=str(path), line=line_number, message=plain[:2048])
                if failed and len(failures) < 32:
                    failures.append(dict(evidence, return_code=int(failed.group(1))))
                if exception_line.match(plain) and len(exceptions) < 32:
                    exceptions.append(evidence)
    scope.update(runner_errors=failures, exception_evidence=exceptions,
                 diagnostic_logs=[str(path) for path in paths])


def classify_scope(scope, failed_limit, test_type):
    if scope["return_code"] != 0:
        scope.update(state="ERROR", reason="benchmark process exited " + str(scope["return_code"]))
        return scope
    if scope.get("runner_errors"):
        failed = scope["runner_errors"][0]
        detail = scope.get("exception_evidence", [failed])[0] if scope.get("exception_evidence") else failed
        scope.update(state="ERROR", reason="AISBench runner subtask failed with code "
                     + str(failed["return_code"]) + "; " + detail["message"]
                     + "; evidence=" + detail["file"] + ":" + str(detail["line"]))
        return scope
    if scope.get("accuracy"):
        try:
            scope["accuracy_evaluation"] = read_accuracy_scope(scope)
            scope["raw_row"] = read_last_result(Path(scope["result_csv"]), test_type=test_type, test_accuracy=True)
            scope.update(state="PASS", reason="original accuracy metrics and artifacts captured; independent scoring remains separate")
        except (OSError, ValueError, csv.Error) as error:
            scope.update(state="INVALID", reason=str(error))
        return scope
    try:
        row = read_last_result(Path(scope["result_csv"]), test_type=test_type)
        scope["raw_row"] = row
    except (OSError, ValueError, csv.Error) as error:
        scope.update(state="INVALID", reason=str(error))
        return scope
    success, failed = scope.get("success_requests"), scope.get("failed_requests")
    if success is None or failed is None:
        scope.update(state="INVALID", reason="missing or invalid Success/Failed Requests counters")
    elif success == 0:
        scope.update(state="REQUEST_ERROR", reason="Success Requests=0")
    elif failed > failed_limit:
        scope.update(state="REQUEST_ERROR", reason="Failed Requests exceeds allowed count")
    else:
        try:
            total = float(row["total_req"])
            if total != success + failed:
                scope.update(state="INVALID", reason="Total Requests does not reconcile with completed logical requests")
            elif scope.get("planned_verified") and total != int(scope["planned_requests"]):
                scope.update(state="INVALID", reason="Total Requests differs from the actual input dataset row count")
            else:
                scope.update(state="PASS", reason="complete scope with valid request counters")
        except (KeyError, ValueError):
            scope.update(state="INVALID", reason="invalid Total Requests")
    return scope


def _child(native_root, evidence_dir, arguments, native_script, source_root=None):
    native_root, evidence_dir = Path(native_root).resolve(), Path(evidence_dir).resolve()
    os.chdir(native_root)
    sys.path.insert(0, str(native_root))
    sys.dont_write_bytecode = True
    if os.name == "posix":
        def terminate_child(signum, frame):
            for process in list(_ACTIVE_PROCESSES):
                stop_process(process)
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, terminate_child)
    scopes = []
    source_root = Path(source_root or native_root).resolve()
    generation_template = restore_generation_template(native_root, evidence_dir, source_root, arguments)
    arguments, accuracy_dataset = prepare_accuracy_dataset(native_root, evidence_dir, arguments, source_root)
    dataset_profile = configure_dataset_profile(native_root, evidence_dir, arguments,
                                               source_root)
    original_run = subprocess.run
    accuracy = "--test_accuracy" in arguments
    is_prefix = option(arguments, "--dataset_type", "normal") == "prefix_cache" and "--prefix_test" in arguments

    def observed_run(command, *positional, **keywords):
        is_benchmark = isinstance(command, str) and re.search(r"(^|[\s;&])ais_bench\b", command)
        if not is_benchmark:
            return original_run(command, *positional, **keywords)
        index = len(scopes) + 1
        scope_id = "scope_%03d" % index
        root = evidence_dir / scope_id
        root.mkdir(exist_ok=False)
        artifacts = root / "artifacts"
        artifacts.mkdir()
        phase = "prefix" if is_prefix and index == 1 else "main"
        scope = dict(scope_id=scope_id, phase=phase, state="running", reason="", return_code=None,
                     raw_row={}, result_csv=str(root / "aisbench_result.csv"),
                     stdout_log=str(root / "stdout.raw.log"), stderr_log=str(root / "stderr.raw.log"),
                     artifact_dir=str(root / "artifacts"), accuracy=accuracy,
                     started_at=time.time())
        scope["dataset_profile"] = {key: dataset_profile.get(key) for key in ("mode", "fingerprint", "directory")}
        scope["generation_template"] = {key: generation_template.get(key) for key in
                                        ("state", "action", "original_sha256", "patched_sha256", "evidence_file")}
        if accuracy_dataset:
            scope["accuracy_dataset"] = accuracy_dataset
        scope["planned_requests"] = (
            int(option(arguments, "--dp", "1")) * int(option(arguments, "--prefix_num", "1"))
            if phase == "prefix" else option(arguments, "--data_num"))
        scope.update(planned_source="cli_unverified", planned_verified=False)
        try:
            config = importlib.import_module("config")
            dataset = Path(config.WORK_PATH) / "ais_bench/datasets/gsm8k/test.jsonl"
            if dataset.is_file():
                with dataset.open("rb") as data:
                    scope["planned_requests"] = sum(bool(line.strip()) for line in data)
                scope.update(planned_source="dataset_rows", planned_verified=True)
                dataset_copy = artifacts / "dataset.jsonl"
                shutil.copyfile(dataset, dataset_copy)
                scope.update(dataset_file=str(dataset_copy), dataset_rows=scope["planned_requests"])
                scope.update(dataset_source_file=str(dataset.resolve()),
                             dataset_source_size=dataset.stat().st_size,
                             dataset_source_mtime_ns=dataset.stat().st_mtime_ns)
        except (AttributeError, OSError, ImportError):
            pass
        scopes.append(scope)
        console_message("AISBENCH_SCOPE_START " + json.dumps({"scope_id": scope_id, "phase": phase}))
        atomic_json(evidence_dir / "scopes.json", scopes)
        # Bash pipefail repairs shell pipeline status at runtime; no native file changes.
        original_command = command
        command, split_streams = split_native_pipeline(command)
        scope.update(instrumentation_version="native-scope-v1",
                     stream_profile="upstream-tee-split-v1" if split_streams else "original-command-v1",
                     original_command=original_command, effective_command=command)
        if os.name == "posix" and keywords.get("shell"):
            command = "set -o pipefail; " + command
            executable = "/bin/bash"
        else:
            executable = keywords.get("executable")
        try:
            rc, _ = stream_process(command, native_root, scope["stdout_log"], scope["stderr_log"],
                                   echo=True, group=True, shell=keywords.get("shell", False),
                                   tee_paths=[native_root / name for name in ("aisbench.log", "aisbench_all.log")] if split_streams else (),
                                   chunk_index=root / "stream_chunks.jsonl",
                                   **({"executable": executable} if executable else {}))
        except BaseException as error:
            scope.update(state="ERROR", reason="scope output/execution failure: " + str(error), return_code=1)
            atomic_json(evidence_dir / "scopes.json", scopes)
            raise
        scope.update(return_code=rc, ended_at=time.time())
        parser_log = root / "parser.log"
        with parser_log.open("w", encoding="utf-8") as parsed:
            for stream in ("stdout_log", "stderr_log"):
                with Path(scope[stream]).open("r", encoding="utf-8", errors="replace") as raw:
                    shutil.copyfileobj(raw, parsed, length=65536)
                parsed.write("\n")
        scope.update(success_requests=request_count_file(parser_log, "Success Requests"),
                     failed_requests=request_count_file(parser_log, "Failed Requests"))
        artifacts = root / "artifacts"
        artifacts.mkdir(exist_ok=True)
        experiment = []
        with parser_log.open("r", encoding="utf-8") as parsed:
            for line in parsed:
                match = re.search(r"Current exp folder:\s*(.+)", re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line))
                if match:
                    experiment = [match.group(1)]
        if experiment:
            source = Path(experiment[-1].strip()).resolve()
            config = importlib.import_module("config")
            output_root = Path(config.OUTPUT_DIR).resolve()
            if source != output_root and output_root in source.parents and source.is_dir():
                # Reject links before traversing any artifact tree.
                if any(path.is_symlink() for path in source.rglob("*")):
                    raise ValueError("scope artifact contains a symlink")
                shutil.copytree(source, artifacts / "experiment")
                scope["experiment_dir"] = str(source)
        if accuracy:
            scope["accuracy_results"] = [str(path) for path in artifacts.rglob("*")
                                         if path.is_file() and path.suffix.lower() in {".json", ".jsonl", ".csv"}]
        try:
            save_file = importlib.import_module("save_file")
            save_file.open = streaming_native_open
            scope["parser_profile"] = "upstream-get-data-iterator-v1"
            npu = int(option(arguments, "--npu_num", "1"))
            rr = "0" if phase == "prefix" else option(arguments, "--request_rate", "0")
            row, _ = save_file.get_data(str(parser_log), rr, npu, option(arguments, "--repeat_rate", "0"))
            with Path(scope["result_csv"]).open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
            scope["raw_row"] = row
        except Exception as error:
            scope["parse_error"] = str(error)
        collect_subtask_diagnostics(scope, parser_log)
        artifact_manifest(root)
        atomic_json(root / "scope.json", scope)
        atomic_json(evidence_dir / "scopes.json", scopes)
        console_message("AISBENCH_SCOPE_END " + json.dumps({"scope_id": scope_id, "phase": phase, "return_code": rc}))
        if rc and keywords.get("check"):
            raise subprocess.CalledProcessError(rc, command)
        return subprocess.CompletedProcess(command, rc)

    subprocess.run = observed_run
    sys.argv = [str(native_script)] + list(arguments)
    try:
        runpy.run_path(str(native_script), run_name="__main__")
    finally:
        subprocess.run = original_run
        atomic_json(evidence_dir / "scopes.json", scopes)


def execute_native(native_root, evidence_dir, arguments, *, timeout=1800, failed_req_limit=0,
                   python_bin=None, native_script=None, echo=True):
    started_monotonic = time.monotonic()
    if not math.isfinite(float(timeout)) or float(timeout) <= 0:
        raise ValueError("trial timeout must be a finite positive number")
    if not isinstance(failed_req_limit, int) or isinstance(failed_req_limit, bool) or failed_req_limit < 0:
        raise ValueError("failed request limit must be a nonnegative integer")
    native_root = Path(native_root).resolve()
    evidence_dir = Path(evidence_dir).resolve()
    native_script = Path(native_script or native_root / "aisbench_test.py").resolve()
    # Creating evidence in native would violate the upstream boundary.
    if evidence_dir == native_root or native_root in evidence_dir.parents:
        raise ValueError("evidence directory must be outside native source")
    evidence_dir.mkdir(parents=True, exist_ok=False)
    working_root = evidence_dir / "native_runtime"
    working_script = working_root / native_script.name
    trial_id = evidence_dir.name
    report = dict(schema_version=1, trial_id=trial_id, state="running", reason="", return_code=None,
                  timed_out=False, scopes=[], main_scopes=[], raw_row={}, selected_scope_id="",
                  stdout_log=str(evidence_dir / "stdout.raw.log"),
                  stderr_log=str(evidence_dir / "stderr.raw.log"), started_at=time.time())
    report["requested"] = {name: option(arguments, "--" + name) for name in (
        "input_len", "output_len", "concurrency", "data_num", "request_rate", "repeat", "npu_num",
        "dataset_type", "prefix_num", "repeat_rate", "seed", "dp", "dataset")}
    atomic_json(evidence_dir / "evaluation.json", report)
    command = [python_bin or sys.executable, str(Path(__file__).resolve()), "--_child",
               "--native-root", str(working_root), "--evidence-dir", str(evidence_dir),
               "--source-root", str(native_root),
               "--native-script", str(working_script), "--", *map(str, arguments)]
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        working_root.mkdir()
        # Run only a private source copy, even for the independent host CLI.
        for source in native_root.iterdir():
            if source.is_file() and not source.is_symlink() and source.suffix in {".py", ".jsonl"}:
                shutil.copy2(source, working_root / source.name)
        if not working_script.exists():
            shutil.copy2(native_script, working_script)
        remaining = float(timeout) - (time.monotonic() - started_monotonic)
        if remaining <= 0:
            raise TimeoutError("trial preparation exhausted its time budget")
        rc, timed_out = stream_process(command, working_root, report["stdout_log"], report["stderr_log"],
                                       timeout=remaining, echo=echo, env=environment)
        report.update(return_code=rc, timed_out=timed_out)
        generation_file = evidence_dir / "generation_template.json"
        if generation_file.is_file():
            report["generation_template"] = json.loads(generation_file.read_text(encoding="utf-8"))
        profile_file = evidence_dir / "dataset_profile.json"
        if profile_file.is_file():
            report["dataset_profile"] = json.loads(profile_file.read_text(encoding="utf-8"))
        accuracy_file = evidence_dir / "accuracy_dataset.json"
        if accuracy_file.is_file():
            report["accuracy_dataset"] = json.loads(accuracy_file.read_text(encoding="utf-8"))
        scopes_path = evidence_dir / "scopes.json"
        scopes = json.loads(scopes_path.read_text(encoding="utf-8")) if scopes_path.exists() else []
        # Compatibility for standalone native-like wrappers that do not launch ais_bench.
        if not scopes and rc == 0 and (working_root / "aisbench_result.csv").is_file():
            csv_path = evidence_dir / "aisbench_result.csv"
            shutil.copy2(working_root / "aisbench_result.csv", csv_path)
            scopes = [dict(scope_id="scope_001", phase="main", return_code=rc, result_csv=str(csv_path),
                           stdout_log=report["stdout_log"], stderr_log=report["stderr_log"], raw_row={},
                           artifact_dir="", accuracy="--test_accuracy" in arguments,
                           success_requests=request_count_file(report["stdout_log"], "Success Requests"),
                           failed_requests=request_count_file(report["stdout_log"], "Failed Requests"))]
        test_type = option(arguments, "--test_type", "stream")
        scopes = [classify_scope(scope, failed_req_limit, test_type) for scope in scopes]
        for scope in scopes:
            scope.update(source_csv=scope.get("result_csv", ""), test_type=test_type,
                         test_accuracy=bool(scope.get("accuracy")))
            scope["counts"] = dict(planned=scope.get("planned_requests", option(arguments, "--data_num")),
                                  total=scope.get("raw_row", {}).get("total_req"),
                                  success=scope.get("success_requests"), failed=scope.get("failed_requests"),
                                  cancelled=None, attempts=None)
            scope["counts"].update(planned_source=scope.get("planned_source", "cli_unverified"),
                                   planned_verified=scope.get("planned_verified", False))
        main = [scope for scope in scopes if scope["phase"] == "main"]
        report.update(scopes=scopes, main_scopes=main)
        if rc or timed_out:
            report.update(state="ERROR", reason="trial timed out" if timed_out else "native process exited " + str(rc))
            if not timed_out and report.get("generation_template", {}).get("state") == "invalid":
                report["reason"] = "generation template compatibility rejected: " + report["generation_template"]["reason"]
            if report.get("accuracy_dataset", {}).get("state") == "invalid":
                report["reason"] = report["accuracy_dataset"]["reason"]
        elif not main:
            report.update(state="INVALID", reason="no main measurement scope")
        else:
            # Prefix preparation errors also invalidate this invocation.
            priorities = {"ERROR": 4, "INVALID": 3, "REQUEST_ERROR": 2, "PASS": 0}
            worst = max(scopes, key=lambda item: priorities.get(item["state"], 5))
            report.update(state=worst["state"], reason=worst["reason"])
            report["raw_row"] = main[-1].get("raw_row", {})
            report["selected_scope_id"] = main[-1]["scope_id"]
        for scope in scopes:
            scope_file = evidence_dir / scope["scope_id"] / "scope.json"
            if scope_file.parent.is_dir():
                atomic_json(scope_file, scope)
    except BaseException as error:
        report.update(state="ERROR", reason=type(error).__name__ + ": " + str(error))
        if isinstance(error, TimeoutError):
            report["timed_out"] = True
        if report["return_code"] is None:
            report["return_code"] = 1
        atomic_json(evidence_dir / "evaluation.json", report)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
    report.update(ended_at=time.time())
    atomic_json(evidence_dir / "evaluation.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--native-root", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--failed-req-limit", type=int, default=0)
    parser.add_argument("--native-script")
    parser.add_argument("--source-root", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="print full machine-readable report; default prints a concise summary")
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    args, arguments = parser.parse_known_args()
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    if args._child:
        _child(args.native_root, args.evidence_dir, arguments,
               Path(args.native_script or Path(args.native_root) / "aisbench_test.py"), args.source_root)
        return 0
    if not args.timeout > 0 or args.failed_req_limit < 0:
        parser.error("timeout must be positive and failed-req-limit nonnegative")
    console_message("AISBench trial starting; raw streams are saved under " + str(args.evidence_dir))
    result = execute_native(args.native_root, args.evidence_dir, arguments,
                            timeout=args.timeout, failed_req_limit=args.failed_req_limit,
                            native_script=args.native_script, echo=True)
    console_message("AISBench trial finished: state=" + result['state'] + ", main_scopes="
          + str(len(result.get('main_scopes', []))) + "; evidence="
          + str(Path(args.evidence_dir) / 'evaluation.json'))
    if args.json:
        console_message(json.dumps(result, ensure_ascii=False))
    return 0 if result["state"] == "PASS" else (1 if result["state"] == "ERROR" else 2)


if __name__ == "__main__":
    sys.exit(main())
