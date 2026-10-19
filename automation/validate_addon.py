from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import importlib.util
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


ADD_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = ADD_DIR.parent
RUN_SCRIPT = ADD_DIR / "run_perf_scenarios.sh"
SCENARIOS_FILE = ADD_DIR / "env.env"
REPORT_PATH = ADD_DIR / "VALIDATION_REPORT.md"


@dataclass
class Check:
    name: str
    status: str
    detail: str


checks: List[Check] = []


def add_check(name: str, ok: bool, detail: str, warn: bool = False) -> None:
    if ok:
        status = "PASS"
    elif warn:
        status = "WARN"
    else:
        status = "FAIL"
    checks.append(Check(name=name, status=status, detail=detail))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def line_no(text: str, pattern: str) -> Optional[int]:
    for index, line in enumerate(text.splitlines(), start=1):
        if pattern in line:
            return index
    return None


def rel(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")


def check_required_files() -> None:
    required = [
        ADD_DIR / "run_perf_scenarios.sh",
        SCENARIOS_FILE,
        ADD_DIR / "aisbench_artifact_archive.py",
        ADD_DIR / "aisbench_result_adapter.py",
        ADD_DIR / "aisbench_perf_matrix.py",
        ADD_DIR / "aisbench_perf_matrix_config.py",
        ADD_DIR / "validate_e2e_outputs.py",
        ADD_DIR / "validate_result_integrity.py",
        ADD_DIR / "README.md",
        PROJECT_ROOT / "aisbench_test.py",
        PROJECT_ROOT / "config.py",
        PROJECT_ROOT / "save_file.py",
        PROJECT_ROOT / "default_api.py",
        PROJECT_ROOT / "generate_dataset.py",
    ]
    for path in required:
        add_check(f"file exists: {rel(path)}", path.exists(), str(path))


def check_no_original_overlay() -> None:
    forbidden_root_files = [
        PROJECT_ROOT / "run_perf_scenarios.sh",
        PROJECT_ROOT / "aisbench_artifact_archive.py",
        PROJECT_ROOT / "aisbench_perf_matrix.py",
        PROJECT_ROOT / "aisbench_perf_matrix_config.py",
    ]
    existing = [rel(path) for path in forbidden_root_files if path.exists()]
    add_check(
        "no migrated files placed in original root",
        not existing,
        "unexpected root files: " + ", ".join(existing) if existing else "all migrated files are only under add/",
    )


def check_python_compile() -> None:
    targets = [
        ADD_DIR / "aisbench_artifact_archive.py",
        ADD_DIR / "aisbench_result_adapter.py",
        ADD_DIR / "aisbench_perf_matrix.py",
        ADD_DIR / "aisbench_perf_matrix_config.py",
        ADD_DIR / "validate_addon.py",
        ADD_DIR / "validate_e2e_outputs.py",
        ADD_DIR / "validate_result_integrity.py",
    ]
    errors = []
    for path in targets:
        try:
            compile(read_text(path), str(path), "exec")
        except (SyntaxError, OSError) as error:
            errors.append(str(error))
    add_check(
        "python compile add-on modules",
        not errors,
        "; ".join(errors) or f"{len(targets)} modules compiled in memory",
    )


def find_bash() -> Optional[str]:
    candidate = shutil.which("bash")
    if candidate:
        return candidate
    bundled = Path("C:/Program Files/Git/bin/bash.exe")
    return str(bundled) if bundled.is_file() else None


def run_bash_code(bash: str, code: str, *, env, timeout: int = 45):
    # Windows command-line quoting/length limits are avoided by using a real script.
    with tempfile.TemporaryDirectory(prefix="addon_bash_") as tmp:
        path = Path(tmp) / "check.sh"
        path.write_bytes(code.encode("utf-8"))
        return subprocess.run([bash, "-uo", "pipefail", str(path)], env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def check_shell_availability() -> None:
    bash_path = find_bash()
    if not bash_path:
        add_check(
            "local bash environment",
            True,
            "bash is not available on this Windows host; shell runtime must be executed on the target Linux/docker host",
        )
        return
    add_check("local bash environment", True, bash_path)
    result = subprocess.run(
        [bash_path, "-n", str(RUN_SCRIPT)],
        cwd=str(ADD_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    add_check("bash -n add/run_perf_scenarios.sh", result.returncode == 0, result.stdout.strip() or "syntax ok")


def check_run_script() -> None:
    text = read_text(RUN_SCRIPT)
    required_snippets = {
        "ADD_DIR resolves script directory": 'ADD_DIR="$(cd "$(dirname "$0")" && pwd)"',
        "PROJECT_ROOT resolves add parent": 'PROJECT_ROOT="$(cd "$ADD_DIR/.." && pwd)"',
        "HOST_OUTPUT_DIR defaults to parent outputs": 'HOST_OUTPUT_DIR="${HOST_OUTPUT_DIR:-${PROJECT_ROOT}/outputs/default}"',
        "TOOLS_HOST_DIR defaults to parent root": 'TOOLS_HOST_DIR="${TOOLS_HOST_DIR:-$PROJECT_ROOT}"',
        "MODEL_NAME configurable": 'MODEL_NAME="${MODEL_NAME:-your-model}"',
        "scenario file defaults beside script": 'SCENARIO_CONFIG_FILE="${1:-$ADD_DIR/env.env}"',
        "legacy JSON environment override rejected": 'if [ "${SCENARIOS_JSON_OVERRIDE+x}" = "x" ]; then',
        "inherited scenario JSON cleared": 'unset SCENARIOS_JSON',
        "CONFIG_JSON uses MODEL_NAME variable": '"MODEL_NAME": os.environ["MODEL_NAME"]',
        "CONFIG_JSON generated by json.dumps": "json.dumps({",
        "fixed run clears stale root artifacts": "rm -f aisbench.log aisbench_result.csv temp_api.py",
        "fixed run archives with add tool": "python3 add/aisbench_artifact_archive.py performance",
        "fixed run normalizes source columns": "python3 add/aisbench_result_adapter.py",
        "peak run uses add matrix tool": "python3 add/aisbench_perf_matrix.py",
        "peak run uses add matrix config": "--config-file add/aisbench_perf_matrix_config.py",
        "peak run defines unique tag": 'PEAK_RUN_TAG="${RUN_TIMESTAMP}_${TEST_INDEX}"',
        "peak run forwards unique tag": "--run-tag '$PEAK_RUN_TAG'",
        "peak report lookup scoped by tag": "peak_*_$PEAK_RUN_TAG/peak_search_report.json",
    }
    for name, snippet in required_snippets.items():
        number = line_no(text, snippet)
        add_check(name, number is not None, f"{rel(RUN_SCRIPT)}:{number}" if number else f"missing snippet: {snippet}")

    hardcoded_config = '"MODEL_NAME":"glm' in text or '"MODEL_NAME": "glm' in text
    add_check(
        "CONFIG_JSON has no hardcoded MODEL_NAME",
        not hardcoded_config,
        "MODEL_NAME is read from $MODEL_NAME" if not hardcoded_config else "hardcoded MODEL_NAME found",
    )

    legacy_global_find = "find '$CONTAINER_OUTPUT_DIR' -name 'peak_search_report.json'" in text
    add_check(
        "peak report lookup avoids global historical find",
        not legacy_global_find,
        "only run_tag-scoped lookup remains" if not legacy_global_find else "global historical find still present",
    )

    active_patterns = {
        "set -uo pipefail is active": r"^set -uo pipefail\s*$",
        "fixed_concs assignment is active": r"^\s*fixed_concs\s*=\s*\[c for c in concurrency_values",
        "fixed CONTAINER_LOG_TMP assignment is active": r"^\s*CONTAINER_LOG_TMP=\$\(mktemp /tmp/container_output_XXXXXX\.log\)",
        "RESULT_ROW initialization is active": r"^\s*RESULT_ROW=\"\"\s*$",
        "peak REPORT_JSON lookup is active": r"^\s*REPORT_JSON=\\\$\(find '\$CONTAINER_OUTPUT_DIR'",
        "peak REPORT path marker is active": r"^\s*echo '===PEAK_REPORT_PATH==='\s*$",
        "peak REPORT path extraction is active": r"^\s*PEAK_REPORT_PATH=\$\(echo \"\$CONTAINER_OUTPUT\"",
        "peak metrics parser uses fixed container ID and stdin": r"^\s*docker exec -i \"\$CURRENT_ID\" python3 -",
        "peak metrics output file is read": r"^\s*PEAK_RAW=\$\(tail -n 1 \"\$PEAK_METRICS_FILE\"",
        "peak JSON file load is active": r"^\s*report = json\.load\(f\)",
        "peak metrics vals list is active": r"^\s*vals = \[",
    }
    for name, pattern in active_patterns.items():
        active = re.search(pattern, text, flags=re.M) is not None
        add_check(name, active, "active code line found" if active else f"missing active pattern: {pattern}")

    commented_code_patterns = {
        "no commented-out set -uo pipefail": r"^\s*#.*set -uo pipefail",
        "no commented-out fixed_concs assignment": r"^\s*#.*fixed_concs\s*=",
        "no commented-out CONTAINER_LOG_TMP assignment": r"^\s*#.*CONTAINER_LOG_TMP=",
        "no commented-out RESULT_ROW initialization": r"^\s*#.*RESULT_ROW=",
        "no commented-out REPORT_JSON lookup": r"^\s*#.*REPORT_JSON=",
        "no commented-out peak report path": r"^\s*#.*PEAK_REPORT_PATH=",
        "no commented-out peak json load": r"^\s*#.*report = json\.load",
        "no commented-out peak vals list": r"^\s*#.*vals =",
    }
    for name, pattern in commented_code_patterns.items():
        commented = re.search(pattern, text, flags=re.M) is not None
        add_check(name, not commented, "ok" if not commented else f"comment swallowed code: {pattern}")

    fixed_pass_has_repeat_rate_field = 'append_source_result "$ARCHIVE_NAME" "$RESULT_ROW" "$SCENARIO_REPEAT_RATE" "passed"' in text
    fixed_fail_has_repeat_rate_field = 'append_source_result "$ARCHIVE_NAME" "$RESULT_ROW" "$SCENARIO_REPEAT_RATE" "failed"' in text
    add_check(
        "fixed CSV passed row aligns repeat_rate/status",
        fixed_pass_has_repeat_rate_field,
        "ok" if fixed_pass_has_repeat_rate_field else "missing SCENARIO_REPEAT_RATE before passed",
    )
    add_check(
        "fixed CSV failed row aligns repeat_rate/status",
        fixed_fail_has_repeat_rate_field,
        "ok" if fixed_fail_has_repeat_rate_field else "missing SCENARIO_REPEAT_RATE before failed",
    )

    variables = [
        "CONTAINER_NAME",
        "EXPECTED_IMAGE",
        "HOST_OUTPUT_DIR",
        "MODEL_NAME",
        "MODEL_HOST_PATH",
        "TOOLS_HOST_DIR",
        "CONTAINER_OUTPUT_DIR",
        "MODEL_CONTAINER_PATH",
        "TOOLS_CONTAINER_DIR",
        "HOST_IP",
        "HOST_PORT",
        "API_KEY",
        "API_URL",
        "FAILED_REQ_LIMIT",
        "MAX_CONTEXT_LEN",
        "PEAK_MIN_CONCURRENCY",
        "PEAK_MAX_MULTIPLIER",
        "PEAK_DATA_MULTIPLIER",
        "PEAK_WAIT_SECONDS",
        "PEAK_TTFT_LIMIT",
        "PEAK_TPOT_LIMIT",
        "PEAK_HARD_CAP",
    ]
    for var in variables:
        defined = re.search(rf"^{re.escape(var)}=", text, flags=re.M) is not None
        used_count = len(re.findall(rf"\${var}|\${{{var}", text))
        add_check(
            f"variable wired: {var}",
            defined and used_count > 0,
            f"defined={defined}, references={used_count}",
        )


def resolve_max_context_len(run_script_text: str) -> int:
    """Use the same MAX_CONTEXT_LEN source as run_perf_scenarios.sh."""
    default_match = re.search(
        r'MAX_CONTEXT_LEN="\$\{MAX_CONTEXT_LEN:-([0-9]+)\}"',
        run_script_text,
    )
    if not default_match:
        raise ValueError("MAX_CONTEXT_LEN default not found in run_perf_scenarios.sh")
    default_value = default_match.group(1)
    raw_value = os.environ.get("MAX_CONTEXT_LEN", default_value).strip()
    value = int(raw_value)
    if value < 0:
        raise ValueError("MAX_CONTEXT_LEN must be >= 0")
    return value


def read_scenarios_file(path: Path = SCENARIOS_FILE) -> List[Dict[str, object]]:
    """Read the single literal JSON block; never evaluate or source env code."""
    text = path.read_text(encoding="utf-8-sig")
    definitions = re.findall(r"^[ \t]*(?:export[ \t]+)?SCENARIOS_JSON[ \t]*=", text, flags=re.M)
    blocks = re.findall(
        r"^SCENARIOS_JSON=\$\(cat <<'JSON'[ \t]*\n(.*?)^JSON\n\)[ \t]*(?:\n|$)",
        text,
        flags=re.M | re.S,
    )
    if len(definitions) != 1 or len(blocks) != 1:
        raise ValueError("env file must contain exactly one SCENARIOS_JSON literal JSON block")
    value = json.loads(blocks[0])
    if not isinstance(value, list) or not value:
        raise ValueError("SCENARIOS_JSON must be a non-empty list")
    return value


def check_scenarios_json() -> None:
    text = read_text(RUN_SCRIPT)
    try:
        max_context_len = resolve_max_context_len(text)
        add_check("MAX_CONTEXT_LEN is shared with run script", True, str(max_context_len))
    except (TypeError, ValueError) as exc:
        add_check("MAX_CONTEXT_LEN is shared with run script", False, str(exc))
        return
    try:
        scenarios = read_scenarios_file(SCENARIOS_FILE)
        add_check("SCENARIOS_JSON block is read from env.env", True, str(SCENARIOS_FILE))
    except Exception as exc:
        add_check("SCENARIOS_JSON parses as JSON", False, str(exc))
        return
    add_check("SCENARIOS_JSON parses as JSON", isinstance(scenarios, list), f"scenario_count={len(scenarios)}")
    required = {"name", "input_len", "output_len", "concurrency", "request_rate"}
    for index, scenario in enumerate(scenarios, start=1):
        missing = sorted(required - set(scenario))
        add_check(f"scenario {index} required fields", not missing, f"name={scenario.get('name')!r}, missing={missing}")
        concurrency = scenario.get("concurrency")
        add_check(
            f"scenario {index} concurrency list",
            isinstance(concurrency, list) and bool(concurrency),
            f"concurrency={concurrency!r}",
        )
        try:
            total_context = int(scenario.get("input_len", 0)) + int(scenario.get("output_len", 0))
        except (TypeError, ValueError):
            total_context = -1
        add_check(
            f"scenario {index} total context <= MAX_CONTEXT_LEN({max_context_len})",
            total_context >= 0 and (max_context_len == 0 or total_context <= max_context_len),
            f"name={scenario.get('name')!r}, input+output={total_context}, limit={max_context_len}",
        )


def check_add_python_paths() -> None:
    matrix_text = read_text(ADD_DIR / "aisbench_perf_matrix.py")
    archive_text = read_text(ADD_DIR / "aisbench_artifact_archive.py")
    matrix_config_text = read_text(ADD_DIR / "aisbench_perf_matrix_config.py")
    matrix_snippets = {
        "matrix ADD_DIR is add": "ADD_DIR = Path(__file__).resolve().parent",
        "matrix BASE_DIR is parent root": "BASE_DIR = ADD_DIR.parent",
        "matrix default config in add": 'DEFAULT_CONFIG_FILE = str(ADD_DIR / "aisbench_perf_matrix_config.py")',
        "matrix calls parent aisbench_test": 'AISBENCH_TEST_SCRIPT = BASE_DIR / "aisbench_test.py"',
        "matrix reads parent config": 'runtime_path = BASE_DIR / RUNTIME_CONFIG_FILE',
    }
    for name, snippet in matrix_snippets.items():
        number = line_no(matrix_text, snippet)
        add_check(name, number is not None, f"add/aisbench_perf_matrix.py:{number}" if number else f"missing {snippet}")

    matrix_config_snippets = {
        "matrix config ADD_DIR is add": "ADD_DIR = os.path.dirname(os.path.abspath(__file__))",
        "matrix config BASE_DIR is parent root": "BASE_DIR = os.path.dirname(ADD_DIR)",
        "matrix config loads parent config.py": 'config_path = os.path.join(BASE_DIR, "config.py")',
        "matrix config GLOBAL_ARGS active": "GLOBAL_ARGS = {",
        "matrix config TTFT column active": 'TTFT_COLUMN = "TTFT avg"',
        "matrix config SEARCH_CASES active": "SEARCH_CASES = [",
    }
    for name, snippet in matrix_config_snippets.items():
        number = line_no(matrix_config_text, snippet)
        add_check(name, number is not None, f"add/aisbench_perf_matrix_config.py:{number}" if number else f"missing {snippet}")

    archive_snippets = {
        "archive ADD_DIR is add": "ADD_DIR = Path(__file__).resolve().parent",
        "archive BASE_DIR is parent root": "BASE_DIR = ADD_DIR.parent",
        "archive skips scenario_results": 'top_level == "scenario_results"',
        "archive skips old performance/peak dirs": 'top_level.startswith(("performance_", "peak_"))',
    }
    for name, snippet in archive_snippets.items():
        number = line_no(archive_text, snippet)
        add_check(name, number is not None, f"add/aisbench_artifact_archive.py:{number}" if number else f"missing {snippet}")


def check_config_json_simulation() -> None:
    values = {
        "WORK_PATH_DETECTED": "/benchmark/path with spaces",
        "MODEL_NAME": 'unit-test-model/"quoted"',
        "HOST_IP": "127.0.0.1",
        "HOST_PORT": "8000",
        "API_KEY": 'sk-test-"quote"',
        "API_URL": "http://127.0.0.1:8000/v1?x=1&y=2",
        "POD_INFO": "127.0.0.1:8001,127.0.0.1:8002",
    }
    source = re.search(
        r"^CONFIG_JSON=[^\n]*<<'PY'\n(.*?)^PY\s*$",
        read_text(RUN_SCRIPT),
        flags=re.M | re.S,
    )
    if source is None:
        add_check("actual CONFIG_JSON Python block found", False, "missing config heredoc")
        return
    try:
        result = subprocess.run(
            [sys.executable, "-c", source.group(1)],
            env={**os.environ, **values},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        parsed = json.loads(result.stdout)
    except Exception as exc:
        add_check("CONFIG_JSON renders valid JSON with special characters", False, str(exc))
        return
    add_check("CONFIG_JSON renders valid JSON with special characters", result.returncode == 0, "actual shell config block with synthetic values")
    expected = {
        "WORK_PATH": values["WORK_PATH_DETECTED"],
        "MODEL_NAME": values["MODEL_NAME"],
        "HOST_IP": "127.0.0.1",
        "HOST_PORT": "8000",
        "API_KEY": values["API_KEY"],
        "URL": values["API_URL"],
        "POD_INFO": ["127.0.0.1:8001", "127.0.0.1:8002"],
        "OUTPUT_DIR": "/workspace/aisbench_outputs",
        "MODEL_PATH": "/workspace/model",
    }
    for key, value in expected.items():
        add_check(f"CONFIG_JSON field: {key}", parsed.get(key) == value, "actual shell block matches synthetic configuration")
    empty_url = subprocess.run(
        [sys.executable, "-c", source.group(1)],
        env={**os.environ, **values, "API_URL": ""},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    empty_config = json.loads(empty_url.stdout) if empty_url.returncode == 0 else {}
    add_check("empty API_URL preserves host/port mode", empty_config.get("URL") == "", "runtime URL stays empty")


def load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def simulate_config_py_update() -> None:
    cfg = {
        "DATASET_PATH": "/workspace/aisbench_dataset",
        "WORK_PATH": "/benchmark path",
        "MODEL_NAME": 'model/"safe"\\name',
        "MODEL_PATH": "/workspace/model",
        "HOST_IP": "127.0.0.1",
        "HOST_PORT": "8000",
        "DEFAULT_PERFORMANCE_TEST": "default_perf",
        "OUTPUT_DIR": "/workspace/aisbench_outputs",
        "POD_INFO": [],
        "API_KEY": 'sk-"safe"',
        "URL": "http://127.0.0.1:8000/litellm/?x=1&y=2",
    }
    with tempfile.TemporaryDirectory(prefix="addon_config_") as tmp:
        path = Path(tmp) / "config.py"
        path.write_text(
            '\n'.join(
                [
                    'DATASET_PATH = ""',
                    'WORK_PATH = ""',
                    'MODEL_NAME = "old"',
                    'MODEL_PATH = ""',
                    'HOST_IP = ""',
                    'HOST_PORT = ""',
                    'DEFAULT_PERFORMANCE_TEST = ""',
                    'OUTPUT_DIR = ""',
                    'POD_INFO = []',
                    'API_KEY = ""',
                    'URL=""',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        source = re.search(
            r"^python3 - config\.py [^\n]*<<'PY'\n(.*?)^PY\s*$",
            read_text(RUN_SCRIPT),
            flags=re.M | re.S,
        )
        if source is None:
            add_check("actual config.py writer block found", False, "missing config writer heredoc")
            return
        code = source.group(1).replace('\\"', '"')
        encoded = base64.b64encode(json.dumps(cfg).encode("utf-8")).decode("ascii")
        result = subprocess.run(
            [sys.executable, "-c", code, str(path), encoded],
            env={**os.environ, "WORK_PATH_DETECTED": cfg["WORK_PATH"]},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        add_check("actual config.py writer exits successfully", result.returncode == 0, "temporary synthetic config only")
        namespace: Dict[str, object] = {}
        exec(path.read_text(encoding="utf-8"), namespace)
    for key, value in cfg.items():
        add_check(f"sim config.py write: {key}", namespace.get(key) == value, "synthetic value preserved")


def expand_scenarios_for_simulation(scenarios: List[Dict[str, object]]) -> Tuple[List[str], List[str]]:
    """Execute the production parser, rather than maintain a duplicate parser."""
    source = re.search(r"if ! python3 - .*?<<'PY'\n(.*?)^PY$", read_text(RUN_SCRIPT), flags=re.M | re.S)
    if source is None:
        raise ValueError("production scenario parser not found")
    with tempfile.TemporaryDirectory(prefix="addon_scenarios_") as tmp:
        root = Path(tmp)
        input_path, fixed_path, peak_path = (root / name for name in ("input.json", "fixed.txt", "peak.txt"))
        input_path.write_text(json.dumps(scenarios), encoding="utf-8")
        multiplier = re.search(r'^DATA_NUM_MULTIPLIER="\$\{DATA_NUM_MULTIPLIER:-(\d+)\}"', read_text(RUN_SCRIPT), flags=re.M)
        assert multiplier is not None
        result = subprocess.run(
            [sys.executable, "-c", source.group(1), str(input_path), str(fixed_path), str(peak_path),
             "1", "20", "4", "10", "5", "50", "512000", "0", "0"],
            env={**os.environ, "NPU_NUM": "1", "DATA_NUM_MULTIPLIER": os.environ.get("DATA_NUM_MULTIPLIER") or multiplier.group(1)},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode:
            raise ValueError(result.stderr.strip())
        return fixed_path.read_text(encoding="utf-8").splitlines(), peak_path.read_text(encoding="utf-8").splitlines()


def simulate_scenario_expansion() -> None:
    scenarios = [
        {
            "name": "normal",
            "input_len": "128",
            "output_len": "32",
            "concurrency": [2, "max"],
            "request_rate": "0",
            "ttft_limit": 3,
            "tpot_limit": 40,
            "peak_max_cc": 8,
        },
        {
            "name": "prefix",
            "input_len": "256",
            "output_len": "64",
            "concurrency": [3],
            "request_rate": "1",
            "dataset_type": "prefix_cache",
            "prefix_num": "2",
            "repeat_rate": "0.9",
        },
    ]
    combos, peaks = expand_scenarios_for_simulation(scenarios)
    add_check("sim scenario fixed count", len(combos) == 2, f"combos={combos}")
    add_check("sim scenario peak count", len(peaks) == 1, f"peaks={peaks}")
    multiplier = re.search(r'^DATA_NUM_MULTIPLIER="\$\{DATA_NUM_MULTIPLIER:-(\d+)\}"\s*$', read_text(RUN_SCRIPT), flags=re.M)
    assert multiplier is not None
    count = int(os.environ.get("DATA_NUM_MULTIPLIER") or multiplier.group(1))
    add_check("sim fixed data_num multiplier", f"--data_num {2 * count}" in combos[0] and f"--data_num {3 * count}" in combos[1], f"fixed multiplier={count}")
    add_check("sim prefix args forwarded to fixed run", "--dataset_type prefix_cache" in combos[1] and "--repeat_rate 0.9" in combos[1], combos[1])
    add_check("sim peak max and thresholds", "|1|8|4|0|3|40|".replace("|3|40|", "|3|40|") in peaks[0], peaks[0])


def simulate_peak_report_lookup_and_parse() -> None:
    run_tag = "20260706_999"
    with tempfile.TemporaryDirectory(prefix="addon_peak_") as tmp:
        root = Path(tmp)
        old = root / "peak_old_20200101_000000"
        current = root / f"peak_case_{run_tag}"
        nested = root / "peak_newer_wrong_20990101" / "nested" / f"peak_case_{run_tag}"
        for directory, cc in [(old, 1), (current, 123), (nested, 999)]:
            directory.mkdir(parents=True)
            (directory / "peak_search_report.json").write_text(
                json.dumps(
                    {
                        "best": [
                            {
                                "cc": cc,
                                "TTFT avg": 10.5,
                                "TPOT avg": 20.5,
                                "qps": 3.14,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
        matches = sorted(
            path
            for path in root.glob(f"peak_*_{run_tag}/peak_search_report.json")
            if path.is_file()
        )
        selected = matches[-1] if matches else None
        report = json.loads(selected.read_text(encoding="utf-8")) if selected else {}
    add_check("sim peak lookup selects run_tag report", selected is not None and report["best"][0]["cc"] == 123, str(selected))
    vals = [
        report["best"][0].get("cc", ""),
        "0",
        report["best"][0].get("TTFT avg", ""),
        report["best"][0].get("TPOT avg", ""),
        report["best"][0].get("qps", ""),
    ]
    add_check("sim peak metrics parse", vals == [123, "0", 10.5, 20.5, 3.14], str(vals))


def simulate_fixed_result_extraction() -> None:
    adapter = load_module("addon_result_adapter", ADD_DIR / "aisbench_result_adapter.py")
    integrity = load_module("addon_result_integrity", ADD_DIR / "validate_result_integrity.py")
    matrix = load_module("addon_result_matrix", ADD_DIR / "aisbench_perf_matrix.py")
    columns = list(integrity.SOURCE_RESULT_COLUMNS)
    add_check("adapter preserves the 20 source summary fields", list(adapter.CANONICAL_COLUMNS) == columns, f"columns={len(columns)}")
    sample = dict(zip(columns, [
        "2026-10-15 12:34:56", "3584", "1024", "400", "100", "99", "0.5",
        "1234", "1500", "40", "45", "12", "8000", "250", "9000", "281.25",
        "12", "720", "1000", "2000",
    ]))
    expected = [sample[column] for column in columns]
    new_columns = ["model_name", *columns[:7], "repeat_rate", *columns[7:], "E2EL avg", "E2EL P90"]
    source = {**sample, "model_name": 'synthetic, "model"', "repeat_rate": "0.9", "E2EL avg": "800", "E2EL P90": "900"}
    case = matrix.SearchCase(
        name="fixture_case", input_len=3584, output_len=1024, min_concurrency=1,
        max_concurrency=100, request_rate="0.5", request_multiplier=4,
        ttft_limit=5, tpot_limit=50, wait_seconds=0,
    )
    peak_source = re.search(
        r"^[^\n]*docker exec -i[^\n]*\"\$PEAK_REQUEST_RATE\"[^\n]*<<'PY'[^\n]*\n(.*?)^PY[ \t]*$",
        read_text(RUN_SCRIPT), flags=re.M | re.S,
    )
    add_check("actual peak summary parser found", peak_source is not None, "shell peak-report Python block")

    def write_source(path: Path, headers: List[str], row: Dict[str, str]) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerow(["9876" if name == "TTFT avg" else row.get(name, "extra") for name in headers])
            writer.writerow([row.get(name, "extra") for name in headers])

    def run_adapter(path: Path):
        return subprocess.run(
            [sys.executable, str(ADD_DIR / "aisbench_result_adapter.py"), str(path)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    with tempfile.TemporaryDirectory(prefix="addon_csv_compat_") as tmp:
        root = Path(tmp)
        for name, headers in (
            ("legacy20", columns), ("upstream24", new_columns),
            ("reordered24", list(reversed(new_columns))),
            ("additional_field", [*new_columns, "future_metric"]),
        ):
            path = root / f"{name}.csv"
            write_source(path, headers, source)
            row = adapter.read_last_result(path)
            projected = adapter.project_result(row)
            add_check(f"{name}: actual adapter selects last row by header", projected == expected, f"source_columns={len(headers)} projected_columns={len(projected)}")
            cli = run_adapter(path)
            cli_rows = list(csv.reader(io.StringIO(cli.stdout)))
            add_check(f"{name}: adapter CLI emits exactly one canonical row", cli.returncode == 0 and cli_rows == [expected], f"exit={cli.returncode} rows={len(cli_rows)}")
            archived = integrity.read_last_source_result(path)
            add_check(f"{name}: integrity reader matches canonical source values", archived == sample, "source values matched by header")
            matrix_row = matrix.read_last_csv_row(path)
            add_check(f"{name}: matrix source matches fixed summary", adapter.project_result(matrix_row) == projected, "same 20 values for fixed and peak runs")
            passed, _, _ = matrix.resolve_threshold_reason(case, matrix_row, "TTFT avg", "TPOT avg")
            add_check(f"{name}: TTFT milliseconds converted to SLO seconds", passed, "1234ms <= 5s and TPOT 40ms <= 50ms")

            live_path = root / f"{name}_live.csv"
            evaluation = matrix.EvaluationResult(
                sequence_id=1, concurrency=99, num_prompts=400, passed=True, reason="fixture",
                return_code=0, metrics=matrix_row, csv_row=matrix_row, command=[], run_dir="",
                process_log="", aisbench_log="", aisbench_all_log="", result_csv="",
                captured_outputs_dir="",
            )
            matrix.append_live_peak_round(str(live_path), case, evaluation, "0.9")
            with live_path.open(newline="", encoding="utf-8") as handle:
                live_header, live_row = list(csv.reader(handle))
            expected_live = [case.name, *projected, "0.9", "peak_round"]
            add_check(f"{name}: live peak round matches all fixed fields", live_header == integrity.CSV_COLUMNS and live_row == expected_live, f"summary_columns={len(live_row)}")
            if peak_source is not None:
                report = root / f"{name}_peak.json"
                report.write_text(json.dumps({"best": [{**matrix_row, "concurrency": 99}]}), encoding="utf-8")
                peak = subprocess.run(
                    [sys.executable, "-c", peak_source.group(1), "0.5", str(report)],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                parts = peak.stdout.strip().split("|", 6)
                peak_values = [*parts[:5], *next(csv.reader(parts[6:]))] if len(parts) == 7 else []
                add_check(f"{name}: actual peak summary matches all fixed fields", peak.returncode == 0 and peak_values == projected, f"projected_columns={len(peak_values)}")

        for prefill, expected_prefill in (("9999", ""), ("9999.0", ""), ("6034.4525", "6034.4525")):
            path = root / "prefill_optional.csv"
            write_source(path, new_columns, {**source, "prefill_token_throughput": prefill})
            cli = run_adapter(path)
            cli_rows = list(csv.reader(io.StringIO(cli.stdout)))
            fixed_value = cli_rows[0][columns.index("prefill_token_throughput")] if len(cli_rows) == 1 else None
            matrix_value = matrix.read_last_csv_row(path).get("prefill_token_throughput")
            archived_value = integrity.read_last_source_result(path).get("prefill_token_throughput")
            add_check(
                f"optional prefill source={prefill} shared across fixed/peak/integrity",
                cli.returncode == 0 and fixed_value == matrix_value == archived_value == expected_prefill,
                "unavailable global throughput stays empty; real throughput stays unchanged",
            )

        for name, headers in (
            ("missing_metric", [column for column in new_columns if column != "TTFT avg"]),
            ("duplicate_header", [*new_columns, "TTFT avg"]),
        ):
            path = root / f"{name}.csv"
            write_source(path, headers, source)
            cli = run_adapter(path)
            add_check(f"{name}: adapter rejects invalid source structure", cli.returncode != 0 and not cli.stdout.strip(), f"exit={cli.returncode}")
            try:
                integrity.read_last_source_result(path)
            except (ValueError, OSError, csv.Error):
                rejected = True
            else:
                rejected = False
            add_check(f"{name}: integrity reader rejects same structure", rejected, "invalid source cannot be accepted by archive verification")

        for field in ("TTFT avg", "TPOT avg"):
            for value in ("99999", "99999.0", "", "not_numeric", "nan", "inf", "-1"):
                invalid = {**source, field: value}
                path = root / "invalid_metric.csv"
                write_source(path, new_columns, invalid)
                cli = run_adapter(path)
                add_check(f"fixed rejects {field}={value!r}", cli.returncode != 0 and not cli.stdout.strip(), f"exit={cli.returncode}")
                for limit in (0, 300):
                    case.ttft_limit, case.tpot_limit = limit, limit
                    passed, _, _ = matrix.resolve_threshold_reason(case, invalid, "TTFT avg", "TPOT avg")
                    add_check(f"peak rejects {field}={value!r} with SLO={limit}", not passed, "invalid values rejected independently of SLO")
        case.ttft_limit, case.tpot_limit = 1.234, 40
        passed, _, _ = matrix.resolve_threshold_reason(case, sample, "TTFT avg", "TPOT avg")
        add_check("peak accepts values exactly on SLO boundaries", passed, "1234ms == 1.234s and TPOT == 40ms")
        case.ttft_limit = 1.233
        passed, _, _ = matrix.resolve_threshold_reason(case, sample, "TTFT avg", "TPOT avg")
        add_check("peak rejects TTFT over boundary by one millisecond", not passed, "1234ms > 1.233s")


def simulate_archive_snapshot() -> None:
    module = load_module("addon_archive", ADD_DIR / "aisbench_artifact_archive.py")
    with tempfile.TemporaryDirectory(prefix="addon_archive_") as tmp:
        root = Path(tmp) / "outputs"
        capture = Path(tmp) / "capture"
        (root / "20260101_000000").mkdir(parents=True)
        (root / "20260101_000000" / "aisbench.log").write_text("new log", encoding="utf-8")
        (root / "scenario_results" / "old").mkdir(parents=True)
        (root / "scenario_results" / "old" / "all_results.csv").write_text("bad", encoding="utf-8")
        (root / "performance_old").mkdir(parents=True)
        (root / "performance_old" / "aisbench_result.csv").write_text("bad", encoding="utf-8")
        (root / "peak_old").mkdir(parents=True)
        (root / "peak_old" / "peak_search_report.json").write_text("bad", encoding="utf-8")
        captured = module.snapshot_recent_output_files(root, capture)
        copied = sorted(str(path.relative_to(capture)).replace("\\", "/") for path in capture.rglob("*") if path.is_file())
    add_check("sim archive snapshot created", bool(captured), str(captured))
    add_check("sim archive includes raw aisbench output", copied == ["20260101_000000/aisbench.log"], str(copied))


def simulate_perf_matrix_command() -> None:
    module = load_module("addon_matrix", ADD_DIR / "aisbench_perf_matrix.py")
    case = module.SearchCase(
        name="case",
        input_len=128,
        output_len=32,
        min_concurrency=1,
        max_concurrency=4,
        request_rate="0",
        request_multiplier=4,
        ttft_limit=5,
        tpot_limit=50,
        wait_seconds=0,
        forward_args=["--dataset_type", "prefix_cache", "--repeat_rate", "0.9"],
    )
    cfg = module.RunnerConfig(
        config_file=str(ADD_DIR / "aisbench_perf_matrix_config.py"),
        python_bin=sys.executable,
        run_tag="tag",
        ttft_column="TTFT avg",
        tpot_column="TPOT avg",
        auto_expand_max_concurrency=0,
        global_args={"npu_num": "128"},
        global_forward_args=[],
        output_dir="/workspace/aisbench_outputs",
        cases=[case],
        failed_req_limit=0,
    )
    command = module.build_run_command(cfg, case, 3, 12)
    command_text = " ".join(str(item) for item in command)
    add_check("sim perf matrix calls parent aisbench_test", str(PROJECT_ROOT / "aisbench_test.py") in command_text, command_text)
    add_check("sim perf matrix forwards case args", "--dataset_type prefix_cache" in command_text and "--repeat_rate 0.9" in command_text, command_text)
    add_check("sim perf matrix sets concurrency/data_num", "--concurrency 3" in command_text and "--data_num 12" in command_text, command_text)


def simulate_peak_concurrency_display() -> None:
    """Keep the selected search tier separate from measured Concurrency/Max."""
    bash = find_bash()
    if not bash:
        add_check("actual Bash peak concurrency display", False, "Bash is unavailable; report decoding was not executed", warn=True)
        return
    text = read_text(RUN_SCRIPT)
    parser = re.search(r"^[^\n]*docker exec -i[^\n]*\"\$PEAK_REQUEST_RATE\"[^\n]*<<'PY'[^\n]*\n(.*?)^PY[ \t]*$", text, flags=re.M | re.S)
    decode = re.search(r'^(        if \[\[ "\$PEAK_RAW" == .*?^        fi[ \t]*$)', text, flags=re.M | re.S)
    summary = re.search(r'^(    if \[ -n "\$PEAK_METRICS" \]; then\n.*?^    fi[ \t]*$)', text, flags=re.M | re.S)
    logger = re.search(r"^log\(\) \{.*$", text, flags=re.M)
    header = re.search(r'^CSV_HEADER="([^"]+)"$', text, flags=re.M)
    if not all((parser, decode, summary, logger, header)):
        add_check("actual peak report extraction and Bash decoding found", False, "missing report protocol or summary block")
        return
    columns = next(csv.reader([header.group(1)]))
    with tempfile.TemporaryDirectory(prefix="addon_peak_display_") as tmp:
        root = Path(tmp)
        for selected, measured in ((2, "1.9719"), (1, "0.982"), (None, "1.9719")):
            source = dict.fromkeys(columns[1:21], "1")
            source.update(current_time="2026-10-15 12:34:56", input_len="8192", output_len="1024",
                          total_req="2", max_cc="9", cc=measured, rr="0.5")
            best = {**source, "passed": "yes"}
            if selected is not None:
                best["concurrency"] = selected
            report = root / "peak_report.json"
            report.write_text(json.dumps({"best": [best]}), encoding="utf-8")
            extracted = subprocess.run([sys.executable, "-c", parser.group(1), "0.5", str(report)],
                                       text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output = root / "summary.csv"
            with output.open("w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerow(columns)
            environment = {**os.environ, "PEAK_RAW": extracted.stdout.rstrip("\r\n"),
                           "PEAK_METRICS": "", "PEAK_CONFIG_CC": "stale", "ARCHIVE_NAME": "display_case",
                           "RESULTS_CSV": str(output), "CSV_HEADER": header.group(1), "PEAK_REPEAT_RATE": "0.9", "PASSED_COUNT": "0"}
            writer = re.search(r"^append_source_result\(\) \{\n.*?^\}[ \t]*$", text, flags=re.M | re.S)
            assert writer is not None
            interpreter = shlex.quote(sys.executable.replace("\\", "/"))
            code = 'python3() { ' + interpreter + ' "$@"; }\n' + writer.group(0) + "\n" + logger.group(0) + "\n" + decode.group(1) + "\n" + summary.group(1) + "\n"
            decoded = subprocess.run([bash, "-eu", "-c", code], env=environment,
                                     text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            label = f"peak selected={selected} measured={measured} native_max=9"
            expected_display = "未知" if selected is None else str(selected)
            add_check(label + " display uses report concurrency",
                      extracted.returncode == decoded.returncode == 0 and
                      f"最优并发={expected_display}（实测cc={measured}）" in decoded.stdout,
                      "actual Python extraction and Bash decoder preserve the selected tier")
            with output.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            expected = {"scenario": "display_case", **source, "repeat_rate": "0.9", "status": "peak"}
            add_check(label + " summary keeps all original metrics", rows == [expected], "23 columns unchanged, including measured cc and native max_cc")


def simulate_empty_result_rows() -> None:
    """Run each real failure/no-result call through its actual Bash helper."""
    bash = find_bash()
    if not bash:
        add_check("actual Bash empty-result rows", False, "Bash is unavailable; placeholder rows were not executed", warn=True)
        return
    text = read_text(RUN_SCRIPT)
    helper = re.search(r"^append_empty_result\(\) \{\n.*?^\}[ \t]*$", text, flags=re.M | re.S)
    header = re.search(r'^CSV_HEADER="([^"]+)"$', text, flags=re.M)
    calls = re.findall(r"^[ \t]+(append_empty_result [^\n]+)$", text, flags=re.M)
    if helper is None or header is None:
        add_check("empty-result helper and schema found", False, "missing actual helper or CSV header")
        return
    columns = next(csv.reader([header.group(1)]))
    add_check("all five empty-result branches use the shared helper", len(calls) == 5, f"branches={len(calls)}")
    environment = {
        **os.environ,
        "ARCHIVE_NAME": 'scenario, "quoted"', "SCENARIO_INPUT_LEN": "8192", "SCENARIO_OUTPUT_LEN": "1024",
        "SCENARIO_CONCURRENCY": "2", "SCENARIO_REQUEST_RATE": "0.25", "SCENARIO_REPEAT_RATE": "0.75",
        "PEAK_INPUT_LEN": "8192", "PEAK_OUTPUT_LEN": "1024", "PEAK_CURRENT_TIME": "2026-10-15 12:34:56",
        "PEAK_MAX_CC": "2", "PEAK_REQUEST_RATE": "0.5", "PEAK_REPEAT_RATE": "0.9",
        "CSV_HEADER": header.group(1),
    }
    # Map the host's Python executable to the production helper's python3 command.
    interpreter = shlex.quote(sys.executable.replace("\\", "/"))
    with tempfile.TemporaryDirectory(prefix="addon_empty_rows_") as tmp:
        for index, call in enumerate(calls, 1):
            path = Path(tmp) / f"empty_{index}.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerow(columns)
            code = 'python3() { ' + interpreter + ' "$@"; }\n' + helper.group(0) + "\n" + call + "\n"
            result = subprocess.run([bash, "-eu", "-c", code], env={**environment, "RESULTS_CSV": str(path)},
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            fixed = "$SCENARIO_INPUT_LEN" in call
            status = "no_result" if call.endswith('"no_result"') else "failed"
            label = f"{'fixed' if fixed else 'peak'} {status} branch {index}"
            add_check(label + " actual writer exits successfully", result.returncode == 0, result.stderr.strip() or "actual shared helper executed")
            with path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))
            add_check(label + " exact 23 fields", len(rows) == 2 and len(rows[1]) == 23, f"field_counts={[len(row) for row in rows]}")
            with path.open("r", encoding="utf-8", newline="") as handle:
                named_rows = list(csv.DictReader(handle))
            expected = dict.fromkeys(columns, "")
            expected.update({
                "scenario": environment["ARCHIVE_NAME"], "input_len": "8192", "output_len": "1024",
                "cc": "2" if fixed else "", "rr": "0.25" if fixed else "0.5",
                "repeat_rate": "0.75" if fixed else "0.9", "status": status,
            })
            if not fixed and status == "no_result":
                expected.update(current_time=environment["PEAK_CURRENT_TIME"], max_cc="2")
            add_check(label + " all named fields and quoted scenario match", named_rows == [expected], "metadata remains in its named columns; unavailable metrics stay empty")


def simulate_peak_search_controls() -> None:
    """Execute the real Bash command builder, then the real parser and search."""
    bash = find_bash()
    if not bash:
        add_check("actual Bash peak controls", False, "Bash is unavailable; command construction was not executed", warn=True)
        return
    text = read_text(RUN_SCRIPT)
    construct = re.search(r'^(    PEAK_CMD="python3 add/aisbench_perf_matrix\.py.*?)(?=^    CONTAINER_LOG_TMP=)', text, flags=re.M | re.S)
    declaration = re.search(r"^PEAK_HARD_CAP=.*$", text, flags=re.M)
    functions = re.findall(r"^(?:log|fail)\(\) \{.*$", text, flags=re.M)
    if construct is None or declaration is None or len(functions) != 2:
        add_check("actual Bash peak controls found", False, "missing command builder or switch declaration")
        return
    code = "\n".join(functions) + "\n" + declaration.group(0) + "\n" + construct.group(1) + '\nprintf "%s\\n" "$PEAK_CMD"\n'
    module = load_module("addon_matrix_controls", ADD_DIR / "aisbench_perf_matrix.py")
    common = {
        "PEAK_RUN_TAG": "offline-controls", "ARCHIVE_NAME": "offline_peak", "PEAK_INPUT_LEN": "8192",
        "PEAK_OUTPUT_LEN": "1024", "PEAK_MIN_CC": "1", "PEAK_MAX_CC": "1",
        "PEAK_DATA_MULTIPLIER_VAL": "1", "PEAK_REQUEST_RATE": "0", "PEAK_TTFT": "30",
        "PEAK_TPOT": "1000", "PEAK_WAIT": "0", "FAILED_REQ_LIMIT": "0", "NPU_NUM": "1",
        "CONTAINER_OUTPUT_DIR": "/tmp/unused-offline-controls", "RUN_TIMESTAMP": "offline",
        "PEAK_REPEAT_RATE": "0",
    }

    def run_search(case, config, cutoff):
        calls = []
        original = module.run_single_evaluation

        def measurement(**kwargs):
            cc = kwargs["concurrency"]
            calls.append(cc)
            if len(calls) > 32:
                raise RuntimeError("offline probe guard exceeded")
            return module.EvaluationResult(
                sequence_id=kwargs["sequence_id"], concurrency=cc,
                num_prompts=module.calculate_num_prompts(cc, case.request_multiplier),
                passed=cc <= cutoff, reason="offline monotone predicate", return_code=0,
                metrics={}, csv_row={}, command=[], run_dir="", process_log="", aisbench_log="",
                aisbench_all_log="", result_csv="", captured_outputs_dir="",
            )

        module.run_single_evaluation = measurement
        try:
            searcher = module.PeakSearcher(case, config, ADD_DIR, module.logging.getLogger("offline-controls"))
            best, history = searcher.search()
            for result in list(history):
                searcher.evaluate(result.concurrency)
            return best.concurrency if best else 0, calls, len(history)
        finally:
            module.run_single_evaluation = original

    nested_case = json.dumps([{
        "name": "nested", "input_len": 8192, "output_len": 1024,
        "min_concurrency": 1, "max_concurrency": 1, "request_multiplier": 1,
    }], separators=(",", ":"))
    tests = [
        ("default switch", None, "--auto-expand-max-concurrency 2", 2, False),
        ("switch 0", "0", "--auto-expand-max-concurrency 2", 2, False),
        ("switch 1 overrides cap2", "1", "--auto-expand-max-concurrency 2", 1, False),
        ("switch 1 overrides unlimited cap", "1", "--auto-expand-max-concurrency 0", 1, False),
        ("switch 1 overrides negative cap", "1", "--auto-expand-max-concurrency -1", 1, False),
        ("switch 1 overrides equals syntax", "1", "--auto-expand-max-concurrency=999", 1, False),
        ("switch 1 overrides initial max", "1", "--max-concurrency 99 --auto-expand-max-concurrency 999", 1, False),
        ("switch 1 overrides abbreviated cap", "1", "--auto-expand-max-concurrenc 999", 1, False),
        ("switch 0 permits nested cases", "0", "--cases-json " + shlex.quote(nested_case) + " --auto-expand-max-concurrency 2", 2, False),
        ("switch 1 rejects nested cases", "1", "--cases-json " + shlex.quote(nested_case), 1, True),
        ("switch 1 rejects abbreviated nested cases", "1", "--cases " + shlex.quote(nested_case), 1, True),
    ]
    for name, switch, forwarded, expected, reject in tests:
        environment = {**os.environ, **common, "PEAK_FORWARD": forwarded}
        environment.pop("PEAK_HARD_CAP", None)
        if switch is not None:
            environment["PEAK_HARD_CAP"] = switch
        shell = subprocess.run([bash, "-eu", "-c", code], cwd=str(PROJECT_ROOT), env=environment,
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if reject:
            add_check(name, shell.returncode != 0 and "does not accept cases-json" in shell.stdout + shell.stderr,
                      "unsupported nested case override rejected before executing a benchmark")
            continue
        if shell.returncode != 0:
            add_check(name, False, f"command construction exit={shell.returncode}")
            continue
        command = shlex.split(shell.stdout)
        previous_argv = sys.argv
        try:
            sys.argv = command[1:]
            args, extra = module.parse_args()
            config = module.load_runner_config(args, extra)
        finally:
            sys.argv = previous_argv
        best, calls, history_count = run_search(config.cases[0], config, 2)
        add_check(name + " effective cap", config.auto_expand_max_concurrency == expected, f"effective cap={config.auto_expand_max_concurrency}")
        add_check(name + " actual search", best == expected and max(calls) <= expected,
                  f"probes={calls}, best={best}")
        add_check(name + " cache unique", len(calls) == len(set(calls)) == history_count, f"measurements={len(calls)}")

    # The numerical cap limits expansion; it does not clamp an already measured
    # initial range. These checks cover both previously repaired boundary cases.
    config = module.RunnerConfig(str(ADD_DIR / "aisbench_perf_matrix_config.py"), sys.executable, "", "TTFT avg", "TPOT avg", 4, {}, [], "", [])
    case = module.SearchCase("singleton", 8192, 1024, 1, 1, "0", 1, 30, 1000, 0)
    best, calls, _ = run_search(case, config, 4)
    add_check("singleton expands when cap permits", best == 4 and calls == [1, 2, 4], f"probes={calls}, best={best}")
    case.max_concurrency = 8
    best, calls, _ = run_search(case, config, 8)
    add_check("numeric cap preserves passing initial high", best == 8 and calls == [4, 8], f"probes={calls}, best={best}")


def simulate_shell_batch_failures() -> None:
    bash = find_bash()
    if not bash:
        add_check("actual shell failure orchestration", False, "Bash unavailable", warn=True)
        return
    text = read_text(RUN_SCRIPT)
    tail = text[text.index('######################## 4. 循环执行固定'):]
    helper_names = ("append_empty_result", "append_source_result", "read_request_counts")
    helpers = []
    for name in helper_names:
        match = re.search(r"^" + name + r"\(\) \{\n.*?^\}[ \t]*$", text, flags=re.M | re.S)
        if match is None:
            add_check("actual shell batch helpers found", False, name)
            return
        helpers.append(match.group(0))
    header = re.search(r'^CSV_HEADER="([^"]+)"$', text, flags=re.M)
    assert header is not None
    columns = next(csv.reader([header.group(1)]))
    source = dict.fromkeys(columns[1:21], "1")
    source.update(current_time="2026-10-15 00:00:00", input_len="16", output_len="4", total_req="1",
                  max_cc="1", cc="0.95", rr="0", **{"TTFT avg": "2", "TPOT avg": "1"})
    encoded = io.StringIO()
    csv.writer(encoded, lineterminator="").writerow([source[key] for key in columns[1:21]])
    marker = "===AISBENCH_RESULT===\n/result.csv\n" + encoded.getvalue()
    interpreter = shlex.quote(sys.executable.replace("\\", "/"))
    prefix = ('python3() { ' + interpreter + ' "$@"; }\n' +
              'log() { printf "%s\\n" "$*"; }\nfail() { log "ERROR: $*"; exit 1; }\n' +
              'docker() {\n'
              ' if [ "$2" = "-i" ]; then shift 3; "$@"; return $?; fi\n'
              ' if [[ "$*" == *aisbench_perf_matrix.py* ]]; then printf "%s\\n" "$MOCK_PEAK_OUTPUT"; return 0; fi\n'
              ' if [ "$MOCK_MIXED" = 1 ] && [[ "$*" == *"--concurrency 2"* ]]; then return 3; fi\n'
              ' printf "%s\\n" "$MOCK_OUTPUT"; return "$MOCK_EXIT";\n}\n' + "\n".join(helpers) + "\n")
    cases = [
        ("dated zero failures", "[2026-10-15 09:30:42] | Failed Requests | total | 0 |\n| Success Requests | total | 1 |\n" + marker, 0, 0, ["passed"], 0, 1),
        ("dated positive failures", "[2026-10-15] Failed Requests: 1\nSuccess Requests: 1\n" + marker, 0, 0, ["failed"], 1, 0),
        ("partial failures within limit", "Failed Requests: 1\nSuccess Requests: 1\n" + marker, 0, 1, ["passed"], 0, 1),
        ("all requests fail despite allowance", "Failed Requests: 2\nSuccess Requests: 0\n" + marker, 0, 9, ["failed"], 1, 0),
        ("unknown failure count", marker, 0, 0, ["failed"], 1, 0),
        ("last failed count unknown", "Failed Requests: 0\nSuccess Requests: 1\nFailed Requests: unknown\n" + marker, 0, 0, ["failed"], 1, 0),
        ("thousands and integer decimals", "[2026-10-15] Failed Requests: 0.0\nSuccess Requests: 1,234.0\n" + marker, 0, 0, ["passed"], 0, 1),
        ("nonzero process", "failure", 3, 0, ["failed"], 1, 0),
        ("missing fixed result", "no marker", 0, 0, ["no_result"], 1, 0),
        ("mixed batch", "Failed Requests: 0\nSuccess Requests: 1\n" + marker, 0, 0, ["passed", "failed"], 1, 1),
        ("missing peak result", "", 0, 0, ["no_result"], 1, 0),
        ("quoted peak success", "", 0, 0, ["peak"], 0, 1),
    ]
    with tempfile.TemporaryDirectory(prefix="addon_shell_batch_") as tmp:
        root = Path(tmp)
        for index, (name, output, process_exit, limit, statuses, expected_exit, expected_passes) in enumerate(cases):
            job_root = root / str(index)
            job_root.mkdir()
            results = job_root / "scenario_results" / "offline"
            results.mkdir(parents=True)
            scenario = 'case, "quoted"'
            peak = name in ("missing peak result", "quoted peak success")
            report = job_root / "peak.json"
            report.write_text(json.dumps({"best": [{**source, "concurrency": 2, "passed": "yes"}]}), encoding="utf-8")
            combos = scenario + '_c1|--input_len 16 --output_len 4 --concurrency 1 --request_rate 0 --repeat_rate 0'
            if name == "mixed batch":
                combos += '\nsecond_c2|--input_len 16 --output_len 4 --concurrency 2 --request_rate 0 --repeat_rate 0'
            environment = {
                **os.environ, "FIXED_COUNT": "0" if peak else str(len(statuses)), "PEAK_COUNT": "1" if peak else "0",
                "TOTAL_TESTS": str(len(statuses)), "COMBOS": "" if peak else combos,
                "PEAK_COMBOS": scenario + '_peak|16|4|1|2|1|0|30|50|0|', "RUN_TIMESTAMP": "offline",
                "HOST_OUTPUT_DIR": str(job_root), "RESULTS_HOST_DIR": str(results),
                "RESULTS_CSV": str(job_root / "summary.csv"), "RESULTS_LOG": str(job_root / "execution.log"),
                "WORK_PATH_DETECTED": "/unused", "TOOL_ROOT": "/unused", "CONTAINER_NAME": "offline", "CURRENT_ID": "offline",
                "CONTAINER_OUTPUT_DIR": "/unused", "HOST_IP": "localhost", "HOST_PORT": "8000",
                "FAILED_REQ_LIMIT": str(limit), "MOCK_OUTPUT": output, "MOCK_EXIT": str(process_exit),
                "MOCK_MIXED": "1" if name == "mixed batch" else "0", "NPU_NUM": "1", "PEAK_HARD_CAP": "0",
                "MOCK_PEAK_OUTPUT": ('===PEAK_REPORT_PATH===\n' + str(report)) if name == "quoted peak success" else "",
            }
            result = run_bash_code(bash, prefix + tail, env=environment)
            with (job_root / "summary.csv").open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
                widths_ok = len(reader.fieldnames or []) == 23 and all(None not in row and all(v is not None for v in row.values()) for row in rows)
            detail = f"exit={result.returncode}; statuses={[row['status'] for row in rows]}; stderr={result.stderr[-500:]}; stdout={result.stdout[-400:]}"
            add_check(name + " actual batch exit and states", result.returncode == expected_exit and [row["status"] for row in rows] == statuses, detail)
            add_check(name + " CSV quotes and field widths", widths_ok and bool(rows) and rows[0]["scenario"] == scenario + ('_peak' if peak else '_c1'), detail)
            add_check(name + " final counters and rendered CSV", f"通过: {expected_passes}" in result.stdout and f"失败: {len(statuses) - expected_passes}" in result.stdout and scenario in result.stdout,
                      "real CSV rendering and summary counters, including no_result")


def simulate_scenario_validation() -> None:
    """Run the production prefix to prove rejection happens before Docker."""
    bash = find_bash()
    if not bash:
        add_check("actual invalid scenario rejection", False, "Bash unavailable", warn=True)
        return
    text = read_text(RUN_SCRIPT)
    prefix = text[:text.index('######################## 0. 初始化结果保存目录')]
    interpreter = shlex.quote(sys.executable.replace("\\", "/"))
    prelude = ('python3() { ' + interpreter + ' "$@"; }\n' +
               'docker() { echo UNEXPECTED_DOCKER_CALL; return 88; }\n')
    cases = [
        ("zero concurrency", {"concurrency": [0]}, {}), ("negative concurrency", {"concurrency": [-1]}, {}),
        ("boolean concurrency", {"concurrency": [True]}, {}), ("fraction concurrency", {"concurrency": [1.5]}, {}),
        ("concurrency not a list", {"concurrency": "max"}, {}), ("empty concurrency", {"concurrency": []}, {}),
        ("duplicate peak entry", {"concurrency": [1, "max", "max"]}, {}), ("zero requests", {"data_num": 0}, {}),
        ("negative input", {"input_len": -1}, {}), ("zero output", {"output_len": 0}, {}),
        ("nonfinite rate", {"request_rate": "nan"}, {}), ("negative rate", {"request_rate": -1}, {}),
        ("invalid repeat rate", {"repeat_rate": 1.1}, {}), ("zero npu", {"npu_num": 0}, {}),
        ("zero prefix count", {"prefix_num": 0}, {}), ("zero DP", {"dp": 0}, {}),
        ("fraction seed", {"seed": 1.5}, {}),
        ("string prefix flag", {"prefix_test": "false"}, {}),
        ("numeric thinking flag", {"enable_think": 1}, {}),
        ("string accuracy flag", {"test_accuracy": "true"}, {}),
        ("duplicate task archive slug", {}, {"fixture_scenarios": [
            {"name": "same.name", "concurrency": [1]}, {"name": "same_name", "concurrency": [1]}]}),
        ("duplicate scenario name", {}, {"fixture_scenarios": [
            {"name": "same", "concurrency": [1]}, {"name": "same", "concurrency": [2]}]}),
        ("zero peak multiplier", {"concurrency": ["max"], "peak_data_multiplier": 0}, {}),
        ("reversed peak range", {"concurrency": ["max"], "peak_min_cc": 8, "peak_max_cc": 1}, {}),
        ("negative peak wait", {"concurrency": ["max"], "peak_wait": -1}, {}),
        ("nonfinite SLO", {"concurrency": ["max"], "ttft_limit": "inf"}, {}),
        ("fraction peak wait", {"concurrency": ["max"], "peak_wait": 0.5}, {}),
        ("fraction global peak wait", {}, {"PEAK_WAIT_SECONDS": "0.5"}),
        ("invalid dataset type", {"dataset_type": "typo"}, {}),
        ("invalid test type", {"test_type": "typo"}, {}),
        ("unpaired gaussian", {"length_mean": 16}, {}),
        ("unpaired range", {"length_min": 8}, {}),
        ("negative length std", {"length_mean": 16, "length_std": -1}, {}),
        ("nonfinite length std", {"length_mean": 16, "length_std": "inf"}, {}),
        ("zero minimum length", {"length_min": 0, "length_max": 16}, {}),
        ("variable length exceeds context", {"length_min": 8, "length_max": 1000}, {"MAX_CONTEXT_LEN": "100"}),
        ("pipe scenario", {"name": "bad|name"}, {}), ("single quote scenario", {"name": "bad'name"}, {}),
        ("hard cap typo", {}, {"PEAK_HARD_CAP": "true"}), ("hard cap negative", {}, {"PEAK_HARD_CAP": "-1"}),
        ("zero fixed multiplier", {}, {"DATA_NUM_MULTIPLIER": "0"}),
        ("zero global peak multiplier", {}, {"PEAK_DATA_MULTIPLIER": "0"}),
        ("negative failed allowance", {}, {"FAILED_REQ_LIMIT": "-1"}), ("zero global NPU", {}, {"NPU_NUM": "0"}),
    ]
    for name, changes, options in cases:
        scenario = {"name": "validation", "input_len": 16, "output_len": 4, "concurrency": [1], "request_rate": 0, **changes}
        options = options.copy()
        scenarios = options.pop("fixture_scenarios", [scenario])
        env = {**os.environ, "AISBENCH_MANAGED_WORKER": "1", "NPU_NUM": "1", "PEAK_HARD_CAP": "0",
               "DATA_NUM_MULTIPLIER": "1", "FAILED_REQ_LIMIT": "0", "PEAK_DATA_MULTIPLIER": "1", **options}
        env.pop("SCENARIOS_JSON_OVERRIDE", None)
        with tempfile.TemporaryDirectory(prefix="addon_invalid_env_") as tmp:
            config = Path(tmp) / "scenarios.env"
            config.write_bytes(("SCENARIOS_JSON=$(cat <<'JSON'\n" + json.dumps(scenarios)
                                + "\nJSON\n)\n").encode("utf-8"))
            code = prelude + "set -- " + shlex.quote(config.as_posix()) + "\n" + prefix
            result = run_bash_code(bash, code, env=env, timeout=30)
        add_check(name + " rejected before Docker", result.returncode != 0 and "UNEXPECTED_DOCKER_CALL" not in result.stdout and "ValueError" in result.stderr,
                  f"exit={result.returncode}; actual parser, no Docker/model calls")
    for name, changes in [("quoted name", {"name": 'hello, "quoted"'}), ("repeated normalized concurrency", {"concurrency": [1, "1", 2, 1]}),
                          ("zero SLO disables limit", {"concurrency": ["max"], "ttft_limit": 0, "tpot_limit": 0}),
                          ("percentage ratio", {"repeat_rate": "50%"}), ("zero percentage", {"repeat_rate": "0%"}),
                          ("full prefix percentage", {"repeat_rate": "100%", "dataset_type": "prefix_cache"}),
                          ("fractional Gaussian std", {"length_mean": 16, "length_std": 0.5}),
                          ("reversed length bounds", {"length_min": 32, "length_max": 16}),
                          ("unknown extension argument", {"future_option": "two words"}),
                          ("unknown extension pipe value", {"future_option": "two|words"}),
                          ("negative integer seed", {"seed": -1}),
                          ("false boolean flag", {"prefix_test": False})]:
        try:
            fixed, peak = expand_scenarios_for_simulation([{"name": "validation", "input_len": 16, "output_len": 4, "concurrency": [1], **changes}])
            ok = bool(fixed or peak)
        except Exception as error:
            ok = False
            fixed, peak = [str(error)], []
        add_check(name + " remains supported", ok, str(fixed + peak))


def simulate_container_reuse_guards() -> None:
    bash = find_bash()
    if not bash:
        add_check("actual container reuse guards", False, "Bash unavailable", warn=True)
        return
    text = read_text(RUN_SCRIPT)
    block = text[text.index('ACTION="reused"'):text.index('write_runtime_report false || fail')]
    fake = r'''
log() { printf '%s\n' "$*"; }
fail() { log "ERROR: $*"; exit 1; }
docker() {
    printf '%s\n' "$1" >> "$CALL_LOG"
    case "$1" in
        ps) [ "$MODE" = ps_fail ] && return 7; printf '%s\n' "$CONTAINER_NAME" ;;
        inspect)
            [ "$MODE" = inspect_fail ] && return 7
            case "$3" in
                *Config.Labels*) if [ "$MODE" = missing_marker ]; then echo ''; else echo true; fi ;;
                *'{{.Id}}'*) echo verified-offline-id ;;
                *Config.Image*) if [ "$MODE" = unknown_image ]; then echo model-serving-image; else printf '%s\n' "$EXPECTED_IMAGE"; fi ;;
                *State.Running*) if [ "$MODE" = start_fail ]; then echo false; else echo true; fi ;;
                *"/output"*) printf '%s\n' "$HOST_OUTPUT_DIR" ;;
                *"/model"*) if [ "$MODE" = wrong_model ]; then echo /different/model; else printf '%s\n' "$MODEL_HOST_PATH"; fi ;;
            esac ;;
        start) return 7 ;;
        exec) return 0 ;;
        rm) printf '%s\n' "$3" > "$REMOVED_ID_FILE"; return 0 ;;
        run) echo verified-created-id; return 0 ;;
    esac
}
'''
    with tempfile.TemporaryDirectory(prefix="addon_container_guard_") as tmp:
        root = Path(tmp)
        weights = root / "weights"
        weights.mkdir()
        for mode in ("match", "wrong_model", "ps_fail", "inspect_fail", "start_fail", "unknown_image", "missing_marker"):
            call_log = root / (mode + ".calls")
            env = {**os.environ, "MODE": mode, "CALL_LOG": str(call_log), "MODEL_HOST_PATH": str(weights),
                   "TOOLS_HOST_DIR": str(PROJECT_ROOT), "HOST_OUTPUT_DIR": str(root),
                   "CONTAINER_NAME": "offline-only", "EXPECTED_IMAGE": "offline-image",
                   "CONTAINER_OUTPUT_DIR": "/output", "MODEL_CONTAINER_PATH": "/model", "REMOVED_ID_FILE": str(root / (mode + ".removed"))}
            result = run_bash_code(bash, fake + block + '\nprintf "ACTION=%s\\n" "$ACTION"\n', env=env)
            calls = call_log.read_text().splitlines() if call_log.exists() else []
            query_failure = mode.endswith("fail") or mode in ("unknown_image", "missing_marker")
            ok = (result.returncode != 0 and not any(cmd in calls for cmd in ("rm", "run"))) if query_failure else (result.returncode == 0 and ("ACTION=recreated" in result.stdout if mode == "wrong_model" else "ACTION=reused" in result.stdout))
            add_check(mode + " actual container selection", ok, f"exit={result.returncode}; calls={calls}; {result.stderr[-200:]}")
            if mode == "wrong_model":
                removed = root / (mode + ".removed")
                add_check("recreation removes only the verified immutable tool ID", removed.exists() and removed.read_text().strip() == "verified-offline-id", "never removes a newly conflicting name")


def write_report() -> None:
    total = len(checks)
    passed = sum(1 for item in checks if item.status == "PASS")
    warned = sum(1 for item in checks if item.status == "WARN")
    failed = sum(1 for item in checks if item.status == "FAIL")
    lines = [
        "# add-on validation report",
        "",
        f"- generated_at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- project_root: `{PROJECT_ROOT}`",
        f"- add_dir: `{ADD_DIR}`",
        f"- total_checks: {total}",
        f"- pass: {passed}",
        f"- warn: {warned}",
        f"- fail: {failed}",
        "",
        "## Summary",
        "",
        "| Status | Count |",
        "|---|---:|",
        f"| PASS | {passed} |",
        f"| WARN | {warned} |",
        f"| FAIL | {failed} |",
        "",
        "## Checks",
        "",
        "| Status | Check | Detail |",
        "|---|---|---|",
    ]
    for item in checks:
        detail = item.detail.replace("|", "\\|").replace("\n", "<br>")
        lines.append(f"| {item.status} | {item.name} | {detail} |")
    lines.extend(
        [
            "",
            "## Runtime Notes",
            "",
            "- This Windows host does not provide `bash` unless the bash availability check passes.",
            "- Run `bash -n add/run_perf_scenarios.sh` and then the real scenario command on the target Linux/docker host.",
            "- This validator checks add-on entry-point wiring. Differences in base files require a separate source diff; byte-for-byte base equality is not asserted.",
        ]
    )
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    sys.dont_write_bytecode = True
    check_required_files()
    check_no_original_overlay()
    check_python_compile()
    check_shell_availability()
    check_run_script()
    check_scenarios_json()
    check_add_python_paths()
    check_config_json_simulation()
    simulate_config_py_update()
    simulate_scenario_expansion()
    simulate_peak_report_lookup_and_parse()
    simulate_fixed_result_extraction()
    simulate_archive_snapshot()
    simulate_perf_matrix_command()
    simulate_peak_concurrency_display()
    simulate_empty_result_rows()
    simulate_peak_search_controls()
    simulate_shell_batch_failures()
    simulate_scenario_validation()
    simulate_container_reuse_guards()
    write_report()
    print(f"Validation report written: {REPORT_PATH}")
    failed = sum(1 for item in checks if item.status == "FAIL")
    warned = sum(1 for item in checks if item.status == "WARN")
    print(f"PASS={sum(1 for item in checks if item.status == 'PASS')} WARN={warned} FAIL={failed}")
    for item in checks:
        if item.status == "FAIL":
            print(f"FAIL: {item.name}: {item.detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
