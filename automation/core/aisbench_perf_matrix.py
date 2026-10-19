import argparse
import csv
import importlib.util
import json
import logging
import math
import os
import re
import shlex
import shutil
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from aisbench_artifact_archive import write_archive_manifest
from aisbench_result_adapter import invalid_result_reason, read_last_result, normalize_scope_row, selected_measurement
from aisbench_execution import execute_native, atomic_json, request_count_file
sys.dont_write_bytecode = True


ADD_DIR = Path(__file__).resolve().parent
AUTOMATION_ROOT = ADD_DIR.parent if ADD_DIR.name == "core" else ADD_DIR
RELEASE_ROOT = AUTOMATION_ROOT.parent
_native_default = RELEASE_ROOT / "aisbench_auto_tools_prefix"
BASE_DIR = Path(os.environ.get("AISBENCH_NATIVE_ROOT", str(_native_default if _native_default.is_dir() else RELEASE_ROOT / "native"))).resolve()
DEFAULT_CONFIG_FILE = str(ADD_DIR / "aisbench_perf_matrix_config.py")
RUNTIME_CONFIG_FILE = "config.py"
AISBENCH_TEST_SCRIPT = BASE_DIR / "aisbench_test.py"
ARTIFACT_FILES = ["aisbench.log", "aisbench_all.log", "aisbench_result.csv", "temp_api.py"]
AISBENCH_VALUE_OPTIONS = {
    "--input_len",
    "--output_len",
    "--data_num",
    "--concurrency",
    "--request_rate",
    "--test_type",
    "--dataset",
    "--repeat",
    "--npu_num",
    "--dataset_type",
    "--prefix_num",
    "--repeat_rate",
    "--seed",
    "--dp",
}
AISBENCH_FLAG_OPTIONS = {
    "--enable_think",
    "--test_accuracy",
    "--prefix_test",
}
PEAK_SETTINGS = {
    "precision": (1, "peak_precision"),
    "confirm_runs": (3, "peak_confirm_runs"),
    "confirm_min_requests": (100, "peak_confirm_min_requests"),
    "max_trials": (40, "peak_max_trials"),
    "trial_timeout": (1800, "peak_trial_timeout"),
    "total_timeout": (14400, "peak_total_timeout"),
    "invalid_retries": (1, "peak_invalid_retries"),
    "floor_concurrency": (1, "peak_floor_cc"),
}


@dataclass
class SearchCase:
    name: str
    input_len: int
    output_len: int
    min_concurrency: int
    max_concurrency: int
    request_rate: str
    request_multiplier: int
    ttft_limit: float
    tpot_limit: float
    wait_seconds: int
    args: Dict[str, Any] = field(default_factory=dict)
    forward_args: List[str] = field(default_factory=list)
    precision: int = 1
    confirm_runs: int = 3
    confirm_min_requests: int = 100
    max_trials: int = 40
    trial_timeout: float = 1800
    total_timeout: float = 14400
    invalid_retries: int = 1
    floor_concurrency: int = 1

    @property
    def slug(self) -> str:
        safe = []
        for char in self.name.strip() or "peak_case":
            if char.isalnum() or char in {"-", "_"}:
                safe.append(char)
            else:
                safe.append("_")
        return "".join(safe).strip("_") or "peak_case"


@dataclass
class RunnerConfig:
    config_file: str
    python_bin: str
    run_tag: str
    ttft_column: str
    tpot_column: str
    auto_expand_max_concurrency: int
    global_args: Dict[str, Any]
    global_forward_args: List[str]
    output_dir: str
    cases: List[SearchCase]
    failed_req_limit: int = 0


@dataclass
class EvaluationResult:
    sequence_id: int
    concurrency: int
    num_prompts: int
    passed: bool
    reason: str
    return_code: int
    metrics: Dict[str, Any]
    csv_row: Dict[str, Any]
    command: List[str]
    run_dir: str
    process_log: str
    aisbench_log: str
    aisbench_all_log: str
    result_csv: str
    captured_outputs_dir: str
    state: str = ""
    profile: str = "explore"
    trial_id: str = ""
    selected_scope_id: str = ""
    scopes: List[Dict[str, Any]] = field(default_factory=list)
    evaluation_file: str = ""
    timed_out: bool = False
    execution_timeout: Optional[float] = None
    timeout_source: str = "trial"


def parse_args() -> Tuple[argparse.Namespace, List[str]]:
    parser = argparse.ArgumentParser(description="Run standalone aisbench peak search with binary search and full logs.")
    parser.add_argument("--config-file", type=str, default=DEFAULT_CONFIG_FILE, help="python config file for peak search")
    parser.add_argument("--python-bin", type=str, default=sys.executable, help="python interpreter used to invoke aisbench_test.py")
    parser.add_argument("--run-tag", type=str, default="", help="optional suffix appended to the output directory")
    parser.add_argument("--cases-json", type=str, default="", help="JSON encoded case list; each case can override input/output and TTFT/TPOT")
    parser.add_argument("--case-name", type=str, default="", help="override case name and run a single case")
    parser.add_argument("--input-len", type=int, default=None, help="override input length")
    parser.add_argument("--output-len", type=int, default=None, help="override output length")
    parser.add_argument("--min-concurrency", type=int, default=None, help="override lower concurrency bound")
    parser.add_argument("--max-concurrency", type=int, default=None, help="override upper concurrency bound")
    parser.add_argument("--request-rate", type=str, default=None, help="override request rate")
    parser.add_argument("--request-multiplier", type=int, default=None, help="request count multiplier, data_num = concurrency * multiplier")
    parser.add_argument("--ttft-limit", type=float, default=None, help="override TTFT threshold, 0 disables the threshold")
    parser.add_argument("--tpot-limit", type=float, default=None, help="override TPOT threshold, 0 disables the threshold")
    parser.add_argument("--wait-seconds", type=int, default=None, help="wait time after each run")
    parser.add_argument("--ttft-column", type=str, default=None, help="CSV column used for TTFT threshold evaluation")
    parser.add_argument("--tpot-column", type=str, default=None, help="CSV column used for TPOT threshold evaluation")
    parser.add_argument(
        "--auto-expand-max-concurrency",
        type=int,
        default=None,
        help="hard cap for upward probing; 0 means no cap",
    )
    parser.add_argument(
        "--failed-req-limit",
        type=int,
        default=None,
        help="Failed Requests 超过该值即视为该并发不满足；默认 0（出现任何失败请求即不满足）",
    )
    parser.add_argument("--live-results-csv", type=str, default="", help="append completed peak rounds to a shared CSV")
    parser.add_argument("--repeat-rate", type=str, default="0", help="repeat rate written to live peak-round rows")
    for name in ("precision", "confirm-runs", "confirm-min-requests", "max-trials", "invalid-retries", "floor-concurrency"):
        parser.add_argument("--" + name, type=int, default=None)
    for name in ("trial-timeout", "total-timeout"):
        parser.add_argument("--" + name, type=float, default=None)
    args, forwarded = parser.parse_known_args()
    return args, forwarded


def load_python_config(config_path: str):
    spec = importlib.util.spec_from_file_location(Path(config_path).stem, config_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载配置文件: {config_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def sanitize_output_name(value: str, fallback: str = "peak_search") -> str:
    safe: List[str] = []
    for char in str(value or "").strip():
        if char.isalnum() or char in {"-", "_"}:
            safe.append(char)
        else:
            safe.append("_")
    return "".join(safe).strip("_") or fallback


def setup_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger(f"aisbench_peak_search.{log_path}")
    logger.setLevel(logging.INFO)
    logger.handlers = []
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    class PersistentHandler(logging.FileHandler):
        def handleError(self, record):
            raise OSError("persistent peak log write failed")
    file_handler = PersistentHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False
    return logger


def args_dict_to_cli(values: Dict[str, Any]) -> List[str]:
    cli: List[str] = []
    for key, value in values.items():
        if value is None or value is False:
            continue
        option = f"--{key}"
        if option in AISBENCH_FLAG_OPTIONS and value is not True:
            raise ValueError(f"{option} must use a boolean value in args")
        if value is True:
            cli.append(option)
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                cli.extend([option, str(item)])
            continue
        cli.extend([option, str(value)])
    return cli


def integer_setting(value: Any, name: str, minimum: Optional[int] = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if isinstance(value, bool) or (isinstance(value, float) and value != result):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return result


def finite_setting(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be a finite number") from error
    if isinstance(value, bool) or not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def build_case(raw_case: Dict[str, Any], default_case: Dict[str, Any], index: int) -> SearchCase:
    merged = dict(default_case)
    merged.update(dict(raw_case or {}))
    try:
        name = str(merged.get("name") or f"case_{index}")
        input_len = integer_setting(merged["input_len"], "input_len", 1)
        output_len = integer_setting(merged["output_len"], "output_len", 2)
        min_concurrency = integer_setting(merged["min_concurrency"], "min_concurrency", 1)
        max_concurrency = integer_setting(merged["max_concurrency"], "max_concurrency", 1)
    except KeyError as exc:
        raise ValueError(f"SEARCH_CASES 第 {index} 项缺少必填字段: {exc}") from exc
    if min_concurrency > max_concurrency:
        raise ValueError(f"SEARCH_CASES 第 {index} 项并发下界不能大于上界")
    request_multiplier = integer_setting(merged.get("request_multiplier", default_case.get("request_multiplier", 4)), "request_multiplier", 1)
    wait_seconds = integer_setting(merged.get("wait_seconds", default_case.get("wait_seconds", 10)), "wait_seconds", 0)
    # 单组合(本 case)覆盖：dataset_type/prefix_num/repeat_rate 若本行填写，则追加到 forward_args。
    # forward_args 在执行时位于 global_forward_args 之后，argparse 后者生效 → 本行覆盖全局；
    # 留空(不带该键)则不追加 → 继承全局。向后兼容旧 cases-json。
    forward_args = list(merged.get("forward_args", []) or [])
    case_args = dict(merged.get("args", {}) or {})
    for per_case_key in ("dataset_type", "prefix_num", "repeat_rate", "seed", "dp"):
        per_case_value = raw_case.get(per_case_key)
        if per_case_value is not None and str(per_case_value).strip() != "":
            forward_args.extend([f"--{per_case_key}", str(per_case_value).strip()])
    # prefix_test 为开关：本 case 显式给出布尔/真值时按值控制（DeployX.Ops 侧已解析有效值并逐 case 下发）
    if "prefix_test" in raw_case:
        prefix_test_value = raw_case.get("prefix_test")
        prefix_test_text = str(prefix_test_value).strip().lower()
        if prefix_test_text not in ("1", "true", "on", "yes", "是", "0", "false", "off", "no", "否"):
            raise ValueError("prefix_test must be a boolean value")
        truthy = prefix_test_text in ("1", "true", "on", "yes", "是")
        if truthy:
            forward_args.append("--prefix_test")
        else:
            forward_args = [token for token in forward_args if token != "--prefix_test"]
        case_args["prefix_test"] = truthy
    request_rate = str(merged.get("request_rate", default_case.get("request_rate", "0")))
    if finite_setting(request_rate, "request_rate") < 0:
        raise ValueError("request_rate must be >= 0")
    settings = {}
    for key, (default, scene_key) in PEAK_SETTINGS.items():
        value = merged.get(scene_key, merged.get(key, default))
        if key.endswith("timeout"):
            settings[key] = finite_setting(value, key)
            if settings[key] <= 0:
                raise ValueError(key + " must be positive")
        else:
            settings[key] = integer_setting(value, key, 0 if key in {"invalid_retries", "confirm_min_requests"} else 1)
    if settings["floor_concurrency"] > min_concurrency:
        raise ValueError("floor_concurrency must not exceed initial min_concurrency")
    return SearchCase(
        name=name,
        input_len=input_len,
        output_len=output_len,
        min_concurrency=min_concurrency,
        max_concurrency=max_concurrency,
        request_rate=request_rate,
        request_multiplier=request_multiplier,
        ttft_limit=finite_setting(merged.get("ttft_limit", default_case.get("ttft_limit", 0)), "ttft_limit"),
        tpot_limit=finite_setting(merged.get("tpot_limit", default_case.get("tpot_limit", 0)), "tpot_limit"),
        wait_seconds=wait_seconds,
        args=case_args,
        forward_args=forward_args,
        **settings,
    )


def cli_case_override_requested(args: argparse.Namespace) -> bool:
    return any(
        getattr(args, name) is not None and getattr(args, name) != ""
        for name in [
            "case_name",
            "input_len",
            "output_len",
            "min_concurrency",
            "max_concurrency",
            "request_rate",
            "request_multiplier",
            "ttft_limit",
            "tpot_limit",
            "wait_seconds",
        ]
    )


def resolve_runtime_output_dir() -> str:
    runtime_path = BASE_DIR / RUNTIME_CONFIG_FILE
    if not runtime_path.exists():
        return str((RELEASE_ROOT / "outputs/default").resolve())
    module = load_python_config(str(runtime_path))
    raw_output_dir = str(getattr(module, "OUTPUT_DIR", "./outputs/default") or "./outputs/default")
    output_path = Path(raw_output_dir)
    if not output_path.is_absolute():
        output_path = (RELEASE_ROOT / output_path).resolve()
    if output_path == BASE_DIR or BASE_DIR in output_path.parents:
        raise ValueError("output directory must not be inside native source")
    return str(output_path)


def load_runner_config(args: argparse.Namespace, forwarded: Sequence[str]) -> RunnerConfig:
    config_path = Path(args.config_file)
    if not config_path.is_absolute():
        config_path = (BASE_DIR / config_path).resolve()
    module = load_python_config(str(config_path))

    global_args = dict(getattr(module, "GLOBAL_ARGS", {}) or {})
    global_forward_args = list(getattr(module, "GLOBAL_FORWARD_ARGS", []) or [])
    default_case = dict(getattr(module, "DEFAULT_CASE", {}) or {})
    search_cases = list(getattr(module, "SEARCH_CASES", []) or [])
    if not search_cases:
        if not default_case:
            raise ValueError("SEARCH_CASES 为空时，DEFAULT_CASE 不能为空")
        search_cases = [dict(default_case)]

    python_bin = str(getattr(module, "PYTHON_BIN", "") or args.python_bin)
    run_tag = str(getattr(module, "RUN_TAG", "") or args.run_tag)
    ttft_column = str(args.ttft_column or getattr(module, "TTFT_COLUMN", "TTFT avg"))
    tpot_column = str(args.tpot_column or getattr(module, "TPOT_COLUMN", "TPOT avg"))
    auto_expand_max_concurrency = integer_setting(
        args.auto_expand_max_concurrency
        if args.auto_expand_max_concurrency is not None
        else getattr(module, "AUTO_EXPAND_MAX_CONCURRENCY", 0),
        "AUTO_EXPAND_MAX_CONCURRENCY", 0,
    )

    cases: List[SearchCase] = []
    if str(args.cases_json or "").strip():
        try:
            cli_cases = json.loads(str(args.cases_json))
        except json.JSONDecodeError as exc:
            raise ValueError(f"--cases-json 不是合法 JSON: {exc}") from exc
        if not isinstance(cli_cases, list) or not cli_cases:
            raise ValueError("--cases-json 必须是非空数组")
        for index, raw_case in enumerate(cli_cases, start=1):
            if not isinstance(raw_case, dict):
                raise ValueError(f"--cases-json 第 {index} 项必须是对象")
            cases.append(build_case(dict(raw_case), default_case, index))
    elif cli_case_override_requested(args):
        base_case = dict(default_case)
        base_case.update(dict(search_cases[0] or {}))
        overrides = dict(base_case)
        overrides.update({
            "name": args.case_name or base_case.get("name", "cli_case"),
            "input_len": args.input_len if args.input_len is not None else base_case.get("input_len"),
            "output_len": args.output_len if args.output_len is not None else base_case.get("output_len"),
            "min_concurrency": args.min_concurrency if args.min_concurrency is not None else base_case.get("min_concurrency"),
            "max_concurrency": args.max_concurrency if args.max_concurrency is not None else base_case.get("max_concurrency"),
            "request_rate": args.request_rate if args.request_rate is not None else base_case.get("request_rate"),
            "request_multiplier": args.request_multiplier if args.request_multiplier is not None else base_case.get("request_multiplier"),
            "ttft_limit": args.ttft_limit if args.ttft_limit is not None else base_case.get("ttft_limit"),
            "tpot_limit": args.tpot_limit if args.tpot_limit is not None else base_case.get("tpot_limit"),
            "wait_seconds": args.wait_seconds if args.wait_seconds is not None else base_case.get("wait_seconds"),
            "args": dict(base_case.get("args", {}) or {}),
            "forward_args": list(base_case.get("forward_args", []) or []),
        })
        cases.append(build_case(overrides, default_case, 1))
    else:
        for index, raw_case in enumerate(search_cases, start=1):
            cases.append(build_case(dict(raw_case or {}), default_case, index))

    failed_req_limit = integer_setting(
        args.failed_req_limit
        if args.failed_req_limit is not None
        else getattr(module, "FAILED_REQ_LIMIT", 0),
        "FAILED_REQ_LIMIT", 0,
    )

    global_forward_args.extend(str(item) for item in forwarded)
    for case in cases:
        for key in PEAK_SETTINGS:
            value = getattr(args, key, None)
            if value is not None:
                setattr(case, key, value)
        for key in PEAK_SETTINGS:
            value = getattr(case, key)
            if key.endswith("timeout"):
                if finite_setting(value, key) <= 0:
                    raise ValueError(key + " must be positive")
            else:
                integer_setting(value, key, 0 if key in {"invalid_retries", "confirm_min_requests"} else 1)
        if case.floor_concurrency > case.min_concurrency:
            raise ValueError("floor_concurrency exceeds initial minimum")
        if auto_expand_max_concurrency and auto_expand_max_concurrency < case.floor_concurrency:
            raise ValueError("hard cap is below concurrency floor")
    names = [case.name for case in cases]
    slugs = [case.slug for case in cases]
    if len(set(names)) != len(names) or len(set(slugs)) != len(slugs):
        raise ValueError("case names and sanitized output names must be unique")
    config = RunnerConfig(
        config_file=str(config_path),
        python_bin=python_bin,
        run_tag=run_tag,
        ttft_column=ttft_column,
        tpot_column=tpot_column,
        auto_expand_max_concurrency=auto_expand_max_concurrency,
        global_args=global_args,
        global_forward_args=global_forward_args,
        output_dir=resolve_runtime_output_dir(),
        cases=cases,
        failed_req_limit=failed_req_limit,
    )
    validate_aisbench_arguments(["--repeat_rate", str(args.repeat_rate)])
    # Validate every final command before the first case can issue a request.
    for case in cases:
        build_run_command(config, case, case.min_concurrency, calculate_num_prompts(case.min_concurrency, case.request_multiplier))
    return config


def calculate_num_prompts(concurrency: int, multiplier: int) -> int:
    return max(concurrency * multiplier, concurrency)


def read_last_csv_row(csv_path: Path) -> Dict[str, Any]:
    if not csv_path.exists():
        return {}
    try:
        return read_last_result(csv_path)
    except (OSError, ValueError, csv.Error) as error:
        logging.warning("Invalid result CSV: %s", error)
        return {}


def format_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(item)) for item in command)


def dedupe_aisbench_cli_args(tokens: Sequence[str]) -> List[str]:
    items = [str(token) for token in tokens]
    last_option_index: Dict[str, int] = {}
    index = 0
    while index < len(items):
        token = items[index]
        if token in AISBENCH_FLAG_OPTIONS:
            last_option_index[token] = index
            index += 1
            continue
        if token in AISBENCH_VALUE_OPTIONS:
            last_option_index[token] = index
            index += 2 if index + 1 < len(items) else 1
            continue
        index += 1

    deduped: List[str] = []
    index = 0
    while index < len(items):
        token = items[index]
        if token in AISBENCH_FLAG_OPTIONS:
            if last_option_index.get(token) == index:
                deduped.append(token)
            index += 1
            continue
        if token in AISBENCH_VALUE_OPTIONS:
            if last_option_index.get(token) == index:
                deduped.append(token)
                if index + 1 < len(items):
                    deduped.append(items[index + 1])
            index += 2 if index + 1 < len(items) else 1
            continue
        deduped.append(token)
        index += 1
    return deduped


def parse_metric_float(value: Any) -> Optional[float]:
    text = str(value).strip()
    if not text or text == "/":
        return None
    try:
        value = float(text)
        return value if math.isfinite(value) else None
    except ValueError:
        return None


def convert_ttft_ms_to_seconds(value_ms: float) -> float:
    return value_ms / 1000.0


def parse_request_count(log_path: Path, label: str) -> Optional[int]:
    """Compatibility API using the execution layer's shared counter grammar."""
    if not log_path or not Path(log_path).is_file():
        return None
    try:
        return request_count_file(log_path, label)
    except OSError:
        return None


def parse_failed_requests(log_path: Path) -> Optional[int]:
    return parse_request_count(log_path, "Failed Requests")


def parse_success_requests(log_path: Path) -> Optional[int]:
    return parse_request_count(log_path, "Success Requests")


def resolve_threshold_reason(case: SearchCase, csv_row: Dict[str, Any], ttft_column: str, tpot_column: str) -> Tuple[bool, str, Dict[str, Any]]:
    metrics = dict(csv_row)
    try:
        finite_setting(case.ttft_limit, "ttft_limit")
        finite_setting(case.tpot_limit, "tpot_limit")
    except ValueError as error:
        return False, str(error), metrics
    if not metrics:
        return False, "未生成 aisbench_result.csv 或结果为空", metrics
    invalid_reason = invalid_result_reason(metrics, (ttft_column, tpot_column))
    if invalid_reason:
        return False, invalid_reason, metrics
    if case.ttft_limit > 0:
        ttft_value_ms = parse_metric_float(metrics.get(ttft_column, ""))
        if ttft_value_ms is None:
            return False, f"结果缺少 {ttft_column}", metrics
        ttft_value_seconds = convert_ttft_ms_to_seconds(ttft_value_ms)
        if ttft_value_seconds > case.ttft_limit:
            return False, f"{ttft_column}={ttft_value_ms}ms（{ttft_value_seconds:.3f}s）超过限制 {case.ttft_limit}s", metrics
    if case.tpot_limit > 0:
        tpot_value = parse_metric_float(metrics.get(tpot_column, ""))
        if tpot_value is None:
            return False, f"结果缺少 {tpot_column}", metrics
        if tpot_value > case.tpot_limit:
            return False, f"{tpot_column}={tpot_value}ms 超过限制 {case.tpot_limit}ms", metrics
    return True, "满足阈值", metrics


def validate_aisbench_arguments(tokens: Sequence[str]) -> None:
    if "--test_accuracy" in tokens:
        raise ValueError("peak search cannot use test_accuracy; fixed-concurrency accuracy remains supported")
    numeric_options = {
        "--input_len", "--output_len", "--data_num", "--concurrency", "--npu_num", "--dp",
        "--prefix_num", "--repeat", "--seed", "--request_rate", "--repeat_rate",
        "--length_mean", "--length_std", "--length_min", "--length_max", "--test_type", "--dataset_type",
    }
    values: Dict[str, str] = {}
    index = 0
    while index < len(tokens):
        option, separator, inline = str(tokens[index]).partition("=")
        if option in AISBENCH_FLAG_OPTIONS and separator:
            raise ValueError(f"{option} is a flag and does not accept a value")
        if option in AISBENCH_VALUE_OPTIONS or option in numeric_options:
            if separator:
                value = inline
            else:
                index += 1
                if index >= len(tokens):
                    raise ValueError(f"missing value for {option}")
                value = str(tokens[index])
            if option in numeric_options:
                values[option] = value
        index += 1
    for option in ("--input_len", "--output_len", "--data_num", "--concurrency", "--npu_num", "--dp", "--prefix_num", "--repeat", "--length_mean", "--length_min", "--length_max"):
        if option in values:
            integer_setting(values[option], option, 1)
    if "--seed" in values:
        integer_setting(values["--seed"], "--seed")
    for option in ("--request_rate", "--length_std"):
        if option in values and finite_setting(values[option], option) < 0:
            raise ValueError(f"{option} must be >= 0")
    if "--repeat_rate" in values:
        raw = values["--repeat_rate"].strip()
        ratio = finite_setting(raw[:-1] if raw.endswith("%") else raw, "--repeat_rate")
        ratio = ratio / 100 if raw.endswith("%") else ratio
        if not 0 <= ratio <= 1:
            raise ValueError("--repeat_rate must be in [0,1] or [0%,100%]")
    for first, second in (("--length_mean", "--length_std"), ("--length_min", "--length_max")):
        if (first in values) != (second in values):
            raise ValueError(f"{first} and {second} must be supplied together")
    # Native sampling deliberately swaps reversed length bounds.
    for option, choices in (("--test_type", {"stream", "text"}), ("--dataset_type", {"normal", "prefix_cache"})):
        if option in values and values[option] not in choices:
            raise ValueError(f"invalid {option}")
    if values.get("--test_type", "stream") != "stream":
        raise ValueError("peak search requires test_type=stream for token latency metrics")
    if "--output_len" in values and int(values["--output_len"]) < 2:
        raise ValueError("peak search requires output_len >= 2 for TPOT")


def build_run_command(config: RunnerConfig, case: SearchCase, concurrency: int, num_prompts: int) -> List[str]:
    finite_setting(case.ttft_limit, "ttft_limit")
    finite_setting(case.tpot_limit, "tpot_limit")
    command = [config.python_bin, str(AISBENCH_TEST_SCRIPT)]
    command.extend(args_dict_to_cli(config.global_args))
    command.extend(config.global_forward_args)
    disabled_flags = {f"--{name}" for name, value in case.args.items() if value is False and f"--{name}" in AISBENCH_FLAG_OPTIONS}
    command = [token for token in command if token not in disabled_flags]
    command.extend(args_dict_to_cli(case.args))
    command.extend(case.forward_args)
    command.extend(
        [
            "--input_len",
            str(case.input_len),
            "--output_len",
            str(case.output_len),
            "--data_num",
            str(num_prompts),
            "--concurrency",
            str(concurrency),
            "--request_rate",
            str(case.request_rate),
        ]
    )
    command = command[:2] + dedupe_aisbench_cli_args(command[2:])
    validate_aisbench_arguments(command[2:])
    return command


def run_single_evaluation(
    case: SearchCase, config: RunnerConfig, case_dir: Path, sequence_id: int,
    concurrency: int, logger: logging.Logger, profile: str = "explore", timeout=None,
    timeout_source: str = "trial",
) -> EvaluationResult:
    num_prompts = calculate_num_prompts(concurrency, case.request_multiplier)
    if profile == "confirm":
        num_prompts = max(num_prompts, case.confirm_min_requests)
    run_dir = case_dir / f"run_{sequence_id:03d}_cc_{concurrency}_{profile}"
    command = build_run_command(config, case, concurrency, num_prompts)
    logger.info("开始执行 case=%s cc=%s requests/scope=%s profile=%s", case.name, concurrency, num_prompts, profile)
    execution_timeout = case.trial_timeout if timeout is None else timeout
    report = execute_native(BASE_DIR, run_dir, command[2:],
                            timeout=execution_timeout,
                            failed_req_limit=config.failed_req_limit,
                            python_bin=config.python_bin, native_script=AISBENCH_TEST_SCRIPT)
    state, reason = report["state"], report["reason"]
    main_scopes = report["main_scopes"]
    if state == "PASS":
        outcomes = []
        for scope in main_scopes:
            try:
                raw = normalize_scope_row(scope, run_dir)
                invalid = invalid_result_reason(raw, (config.ttft_column, config.tpot_column))
            except (OSError, ValueError, TypeError, KeyError, csv.Error) as error:
                raw, invalid = {}, 'original scope CSV unavailable: ' + str(error)
            if profile == "confirm" and case.confirm_min_requests > 0:
                counts = scope.get("counts", {})
                success = parse_metric_float(counts.get("success", scope.get("success_requests", "")))
                failed = parse_metric_float(counts.get("failed", scope.get("failed_requests", "")))
                total = parse_metric_float(raw.get("total_req", ""))
                if (success is None or failed is None or total is None or
                        any(value < 0 or value != int(value) for value in (success, failed, total)) or
                        success + failed != total):
                    invalid = "confirmation scope lacks reconciled actual completed request counters: " + scope.get("scope_id", "unknown")
                elif total < case.confirm_min_requests:
                    invalid = ("confirmation scope " + scope.get("scope_id", "unknown")
                               + " completed " + str(int(total)) + " requests, below confirm_min_requests="
                               + str(case.confirm_min_requests)
                               + "; provide a larger explicit dataset or lower the configured confirmation minimum")
            if invalid:
                scope.update(metric_state="INVALID", metric_reason=invalid)
            else:
                passed, why, _ = resolve_threshold_reason(case, raw, config.ttft_column, config.tpot_column)
                scope.update(metric_state="PASS" if passed else "SLO_FAIL", metric_reason=why)
            outcomes.append(scope["metric_state"])
        if not outcomes:
            state, reason = "INVALID", "no main measurement scope for peak evaluation"
        elif "INVALID" in outcomes:
            state = "INVALID"
            reason = "; ".join(scope["metric_reason"] for scope in main_scopes if scope["metric_state"] == "INVALID")
        elif all(value == "PASS" for value in outcomes):
            state, reason = "PASS", "all main scopes satisfy configured constraints"
        elif all(value == "SLO_FAIL" for value in outcomes):
            state, reason = "SLO_FAIL", "all main scopes exceed configured SLO"
        else:
            state, reason = "UNSTABLE", "repeat scopes disagree under the same trial"
    selection_report = dict(report, state=state)
    selected, row, selection_reason = selected_measurement(selection_report, run_dir)
    if not selected and state in {"PASS", "SLO_FAIL", "UNSTABLE"}:
        state, reason = "INVALID", "selected measurement unavailable: " + selection_reason
    selected_scope_id = selected.get("scope_id", "")
    if report.get("selected_scope_id") and not selected:
        report["reported_selected_scope_id"] = report["selected_scope_id"]
    result = EvaluationResult(
        sequence_id=sequence_id, concurrency=concurrency, num_prompts=num_prompts,
        passed=state == "PASS", reason=reason, return_code=report.get("return_code") or 0,
        metrics=row, csv_row=row, command=command, run_dir=str(run_dir),
        process_log=report["stdout_log"], aisbench_log=selected.get("stdout_log", ""),
        aisbench_all_log=report["stderr_log"], result_csv=selected.get("source_csv") or selected.get("result_csv", ""),
        captured_outputs_dir=selected.get("artifact_dir", ""), state=state, profile=profile,
        trial_id=report["trial_id"], selected_scope_id=selected_scope_id,
        scopes=report["scopes"], evaluation_file=str(run_dir / "evaluation.json"),
        timed_out=report.get("timed_out") is True,
        execution_timeout=execution_timeout, timeout_source=timeout_source,
    )
    # Keep execution details and searchable peak policy together in one document.
    report.update(peak_evaluation=asdict(result), state=state, reason=reason, profile=profile,
                  selected_scope_id=selected_scope_id, raw_row=row, selection_reason=selection_reason)
    atomic_json(run_dir / "evaluation.json", report)
    atomic_json(run_dir / "request_meta.json", dict(case=asdict(case), sequence_id=sequence_id,
                                                   concurrency=concurrency, num_prompts=num_prompts,
                                                   profile=profile, ttft_column=config.ttft_column,
                                                   tpot_column=config.tpot_column,
                                                   execution_timeout=execution_timeout,
                                                   timeout_source=timeout_source))
    (run_dir / "command.txt").write_text(format_command(command) + "\n", encoding="utf-8")
    logger.info("单轮完成 case=%s cc=%s state=%s reason=%s", case.name, concurrency, state, reason)
    return result


def trace_row(case: SearchCase, result: EvaluationResult, config: RunnerConfig) -> Dict[str, Any]:
    row = dict(result.metrics)
    row.update(
        {
            "case_name": case.name,
            "input_len": row.get("input_len", ""),
            "output_len": row.get("output_len", ""),
            "configured_input_len": case.input_len,
            "configured_output_len": case.output_len,
            "sequence_id": result.sequence_id,
            "concurrency": result.concurrency,
            "num_prompts": result.num_prompts,
            "request_rate": case.request_rate,
            "request_multiplier": case.request_multiplier,
            "ttft_limit": case.ttft_limit,
            "tpot_limit": case.tpot_limit,
            "ttft_column": config.ttft_column,
            "tpot_column": config.tpot_column,
            "passed": "yes" if result.passed else "no",
            "reason": result.reason,
            "return_code": result.return_code,
            "run_dir": result.run_dir,
            "process_log": result.process_log,
            "aisbench_log": result.aisbench_log,
            "aisbench_all_log": result.aisbench_all_log,
            "result_csv": result.result_csv,
            "captured_outputs_dir": result.captured_outputs_dir,
            "state": result.state,
            "profile": result.profile,
            "trial_id": result.trial_id,
            "selected_scope_id": result.selected_scope_id,
            "evaluation_file": result.evaluation_file,
        }
    )
    return row


class SearchStop(Exception):
    def __init__(self, termination, reason):
        super().__init__(reason)
        self.termination, self.reason = termination, reason


class PeakSearcher:
    """Bounded exponential bracketing and bisection with independent confirmation."""
    def __init__(self, case, config, case_dir, logger, on_evaluation=None):
        self.case, self.config, self.case_dir, self.logger = case, config, case_dir, logger
        self.on_evaluation = on_evaluation
        self.cache = {}
        self.history = []
        self.profile = "explore"
        self.started = time.monotonic()
        self.confirmed_pass = {}
        self.confirmed_fail = {}
        self.termination = ""
        self.reason = ""

    @property
    def remaining(self):
        return self.case.total_timeout - (time.monotonic() - self.started)

    def _check_budget(self):
        if len(self.history) >= self.case.max_trials or self.remaining <= 0:
            raise SearchStop("budget_limit", "trial count or total elapsed time budget exhausted")

    def evaluate(self, concurrency, fresh=False):
        cap = self.config.auto_expand_max_concurrency
        if concurrency < self.case.floor_concurrency or (cap and concurrency > cap):
            raise ValueError("probe outside hard floor/cap")
        key = (self.profile, concurrency)
        if not fresh and key in self.cache:
            return self.cache[key][-1]
        for attempt in range(self.case.invalid_retries + 1):
            self._check_budget()
            remaining = self.remaining
            if remaining <= 0:
                raise SearchStop("budget_limit", "total elapsed time budget exhausted before trial")
            budget_limited = remaining < self.case.trial_timeout
            result = run_single_evaluation(
                case=self.case, config=self.config, case_dir=self.case_dir,
                sequence_id=len(self.history) + 1, concurrency=concurrency, logger=self.logger,
                profile=self.profile, timeout=min(self.case.trial_timeout, remaining),
                timeout_source="total_budget" if budget_limited else "trial",
            )
            # Capture this before publication/waiting can consume the remaining
            # budget. A real process or I/O error must retain its own cause.
            total_budget_timeout = (result.state == "ERROR" and result.timed_out is True
                                    and budget_limited and self.remaining <= 0)
            if not result.state:  # Compatibility for independent injected evaluators.
                result.state = "PASS" if result.passed else "SLO_FAIL"
            result.profile = self.profile
            self.history.append(result)
            if self.on_evaluation:
                self.on_evaluation(result, self.history)
            if result.state in {"PASS", "SLO_FAIL"}:
                self.cache.setdefault(key, []).append(result)
            if self.case.wait_seconds:
                wait = min(self.case.wait_seconds, max(0, self.remaining))
                if wait:
                    time.sleep(wait)
            if result.state == "INVALID":
                if attempt < self.case.invalid_retries:
                    continue
                raise SearchStop("error", "invalid measurement after bounded retry: " + result.reason)
            if result.state == "REQUEST_ERROR":
                raise SearchStop("request_error", result.reason)
            if result.state == "ERROR":
                if total_budget_timeout:
                    raise SearchStop("budget_limit", "total elapsed time budget exhausted during trial")
                raise SearchStop("error", result.reason)
            if result.state == "UNSTABLE":
                raise SearchStop("unstable", result.reason)
            if result.state not in {"PASS", "SLO_FAIL"}:
                raise SearchStop("error", "unknown measurement state: " + result.state)
            return result
        raise AssertionError("unreachable retry state")

    def confirm(self, concurrency):
        self.profile = "confirm"
        results = [self.evaluate(concurrency, fresh=True) for _ in range(self.case.confirm_runs)]
        states = {item.state for item in results}
        if states == {"PASS"}:
            if concurrency in self.confirmed_fail or any(cc < concurrency for cc in self.confirmed_fail):
                raise SearchStop("unstable", "confirmed lower failure contradicts higher pass")
            self.confirmed_pass[concurrency] = results[-1]
            self.confirmed_fail.pop(concurrency, None)
        elif states == {"SLO_FAIL"}:
            if concurrency in self.confirmed_pass or any(cc > concurrency for cc in self.confirmed_pass):
                raise SearchStop("unstable", "confirmed higher pass contradicts lower failure")
            self.confirmed_fail[concurrency] = results[-1]
            self.confirmed_pass.pop(concurrency, None)
        else:
            raise SearchStop("unstable", "fresh confirmation trials disagree")
        return results[-1]

    def binary_between(self, success_result, fail_result):
        left, right = success_result.concurrency, fail_result.concurrency
        best = success_result
        while right - left > self.case.precision:
            result = self.evaluate((left + right) // 2)
            if result.passed:
                best, left = result, result.concurrency
            else:
                fail_result, right = result, result.concurrency
        return best, fail_result

    def find_downward_success(self, failing_result):
        current = failing_result
        floor = self.case.floor_concurrency
        while current.concurrency > floor:
            probe = max(floor, current.concurrency // 2)
            result = self.evaluate(probe)
            if result.passed:
                return result, current
            current = result
        return None, current

    def find_upward_failure(self, success_result):
        current = success_result
        cap = self.config.auto_expand_max_concurrency
        while not cap or current.concurrency < cap:
            probe = max(current.concurrency + 1, current.concurrency * 2)
            if current.concurrency < self.case.max_concurrency < probe:
                probe = self.case.max_concurrency
            if cap:
                probe = min(probe, cap)
            result = self.evaluate(probe)
            if not result.passed:
                return current, result
            current = result
        return current, None

    def _best_confirmed(self):
        return max(self.confirmed_pass.values(), key=lambda item: item.concurrency, default=None)

    def search(self):
        try:
            while True:
                cap = self.config.auto_expand_max_concurrency
                initial = min(self.case.min_concurrency, cap) if cap else self.case.min_concurrency
                baseline = self.evaluate(initial)
                if not baseline.passed:
                    success, failure = self.find_downward_success(baseline)
                    if success is None:
                        floor_result = self.confirm(self.case.floor_concurrency)
                        if not floor_result.passed:
                            self.termination = "no_pass"
                            self.reason = "no passing point found; hard floor independently reconfirmed"
                            return None, self.history
                        # Confirmation changed the floor classification; never reuse explore cache.
                        continue
                else:
                    success, failure = self.find_upward_failure(baseline)
                if failure is not None:
                    candidate, failure = self.binary_between(success, failure)
                    candidate_check = self.confirm(candidate.concurrency)
                    failure_check = self.confirm(failure.concurrency)
                    if candidate_check.passed and not failure_check.passed:
                        self.termination, self.reason = "boundary_confirmed", "fresh boundary confirmed within configured precision"
                        return candidate_check, self.history
                    # Short profile invalidated: profile remains confirm permanently.
                    continue
                cap_check = self.confirm(success.concurrency)
                if cap_check.passed:
                    self.termination, self.reason = "capped", "configured hard cap passed; higher failure not observed"
                    return cap_check, self.history
                # Cap failed with confirmation samples; rebuild under this profile.
        except SearchStop as stop:
            self.termination, self.reason = stop.termination, stop.reason
        except KeyboardInterrupt:
            self.termination, self.reason = "cancelled", "search interrupted"
        return self._best_confirmed(), self.history

    def summary(self):
        observed = max((item for item in self.history if item.passed),
                       key=lambda item: item.concurrency, default=None)
        confirmed = self._best_confirmed()
        scopes = [scope for item in self.history for scope in item.scopes]
        def complete_sum(key):
            values = [scope.get(key) for scope in scopes]
            return sum(values) if values and all(isinstance(value, int) for value in values) else None
        return dict(
            algorithm="exponential-bracket-bisection-confirmation-v1",
            termination=self.termination, reason=self.reason,
            profile=self.profile, trial_count=len(self.history),
            elapsed_seconds=time.monotonic() - self.started,
            highest_observed=observed.concurrency if observed else None,
            highest_confirmed=confirmed.concurrency if confirmed else None,
            failed_boundary=min(self.confirmed_fail) if self.confirmed_fail else None,
            selected_trial_id=confirmed.trial_id if confirmed else "",
            selected_scope_id=confirmed.selected_scope_id if confirmed else "",
            evaluation_file=confirmed.evaluation_file if confirmed else "",
            request_rate=self.case.request_rate,
            mode="rps_limited_cc_search" if float(self.case.request_rate) > 0 else "concurrency_search",
            constraints_enabled=self.case.ttft_limit > 0 or self.case.tpot_limit > 0,
            initial_range=[self.case.min_concurrency, self.case.max_concurrency],
            hard_cap=self.config.auto_expand_max_concurrency,
            main_scope_count=sum(scope.get("phase") == "main" for scope in scopes),
            prefix_scope_count=sum(scope.get("phase") == "prefix" for scope in scopes),
            success_requests=complete_sum("success_requests"),
            failed_requests=complete_sum("failed_requests"),
            attempts=None,
            settings={name: getattr(self.case, name) for name in PEAK_SETTINGS},
        )


def write_csv_report(csv_path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    headers: List[str] = []
    for row in rows:
        for key in row.keys():
            if key not in headers:
                headers.append(key)
    if not headers:
        headers = ["message"]
        rows = [{"message": "no data"}]
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in headers})


def write_markdown_report(markdown_path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    headers: List[str] = []
    for row in rows:
        for key in row.keys():
            if key not in headers:
                headers.append(key)
    if not headers:
        headers = ["message"]
        rows = [{"message": "no data"}]
    with open(markdown_path, "w", encoding="utf-8") as handle:
        handle.write("| " + " | ".join(headers) + " |\n")
        handle.write("|" + "|".join(["---"] * len(headers)) + "|\n")
        for row in rows:
            handle.write("| " + " | ".join(str(row.get(key, "")) for key in headers) + " |\n")


def write_case_summary(
    case: SearchCase,
    config: RunnerConfig,
    case_dir: Path,
    best_result: Optional[EvaluationResult],
    history: Sequence[EvaluationResult],
) -> Dict[str, Any]:
    trace_rows = [trace_row(case, item, config) for item in history]
    best_row = trace_row(case, best_result, config) if best_result is not None else {
        "case_name": case.name,
        "input_len": "",
        "output_len": "",
        "configured_input_len": case.input_len,
        "configured_output_len": case.output_len,
        "passed": "no",
        "reason": "未找到满足阈值的并发",
        "request_rate": case.request_rate,
        "request_multiplier": case.request_multiplier,
        "ttft_limit": case.ttft_limit,
        "tpot_limit": case.tpot_limit,
        "run_dir": str(case_dir),
    }
    write_csv_report(case_dir / "search_trace.csv", trace_rows)
    write_markdown_report(case_dir / "search_trace.md", trace_rows)
    write_csv_report(case_dir / "best_result.csv", [best_row])
    (case_dir / "search_trace.json").write_text(json.dumps(trace_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "case": asdict(case),
        "best": asdict(best_result) if best_result is not None else None,
        "history": [asdict(item) for item in history],
        "best_row": best_row,
    }
    (case_dir / "case_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return best_row


def append_live_peak_round(csv_path: str, case: SearchCase, result: EvaluationResult, repeat_rate: str) -> None:
    """Persist one completed peak probe immediately, before the next probe starts."""
    if not csv_path:
        return
    path = Path(csv_path)
    ensure_dir(path.parent)
    fields = [
        "scenario", "current_time", "input_len", "output_len", "total_req", "max_cc", "cc", "rr",
        "TTFT avg", "TTFT P90", "TPOT avg", "TPOT SLO_P90", "E2E_time", "output_throughput",
        "single_output_throughput", "E2E_throughput", "single_E2E_throughput", "qps", "qpm",
        "input_token_throughput", "prefill_token_throughput", "repeat_rate", "status",
    ]
    metrics = dict(result.csv_row or result.metrics or {})
    values = [
        case.name,
        metrics.get("current_time", ""), metrics.get("input_len", ""),
        metrics.get("output_len", ""), metrics.get("total_req", ""),
        metrics.get("max_cc", ""), metrics.get("cc", ""), metrics.get("rr", ""),
        metrics.get("TTFT avg", ""), metrics.get("TTFT P90", ""), metrics.get("TPOT avg", ""),
        metrics.get("TPOT SLO_P90", ""), metrics.get("E2E_time", ""), metrics.get("output_throughput", ""),
        metrics.get("single_output_throughput", ""), metrics.get("E2E_throughput", ""),
        metrics.get("single_E2E_throughput", ""), metrics.get("qps", ""), metrics.get("qpm", ""),
        metrics.get("input_token_throughput", ""), metrics.get("prefill_token_throughput", ""),
        metrics.get("repeat_rate", repeat_rate), "peak_round",
    ]
    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        if needs_header:
            writer.writerow(fields)
        writer.writerow(values)
        handle.flush()


def main() -> int:
    args, forwarded = parse_args()
    config = load_runner_config(args, forwarded)

    tag = "_" + sanitize_output_name(config.run_tag) if config.run_tag else ""
    output_root = Path(config.output_dir)
    ensure_dir(output_root)
    session_name = sanitize_output_name(
        str(args.case_name or "").strip() or (config.cases[0].name if config.cases else "peak_search"),
        fallback="peak_search",
    )
    session_dir = output_root / f"peak_{session_name}_{timestamp()}_{uuid.uuid4().hex[:10]}{tag}"
    session_dir.mkdir(parents=True, exist_ok=False)
    logger = setup_logger(session_dir / "peak_search.log")
    shutil.copy2(config.config_file, session_dir / Path(config.config_file).name)

    logger.info("配置文件: %s", config.config_file)
    logger.info("输出目录: %s", session_dir)
    logger.info("执行 case 数量: %s", len(config.cases))
    logger.info("TTFT 列: %s", config.ttft_column)
    logger.info("TPOT 列: %s", config.tpot_column)
    logger.info("向上扩容保护上限: %s", config.auto_expand_max_concurrency or "不限制")

    best_rows: List[Dict[str, Any]] = []
    all_rows: List[Dict[str, Any]] = []
    summaries: List[Dict[str, Any]] = []
    all_passed = True
    for case in config.cases:
        case_dir = session_dir / case.slug
        ensure_dir(case_dir)
        logger.info(
            "开始摸高 case=%s, input=%s, output=%s, range=[%s,%s], request_multiplier=%s, request_rate=%s",
            case.name,
            case.input_len,
            case.output_len,
            case.min_concurrency,
            case.max_concurrency,
            case.request_multiplier,
            case.request_rate,
        )
        def on_evaluation(result: EvaluationResult, history: Sequence[EvaluationResult]) -> None:
            live_row = trace_row(case, result, config)
            all_rows.append(live_row)
            current_best = max(
                (item for item in history if item.passed),
                key=lambda item: item.concurrency,
                default=None,
            )
            if current_best is None:
                current_best = history[-1]
            current_best_row = trace_row(case, current_best, config)
            write_csv_report(case_dir / "search_trace.csv", [trace_row(case, item, config) for item in history])
            write_csv_report(case_dir / "best_result.csv", [current_best_row])
            live_best = [current_best_row]
            live_case_names = {str(item.get("case_name", "")) for item in best_rows}
            if case.name in live_case_names:
                live_best = [item for item in best_rows if item.get("case_name") != case.name] + [current_best_row]
            else:
                live_best = best_rows + [current_best_row]
            write_csv_report(session_dir / "peak_search_report.csv", live_best)
            write_csv_report(session_dir / "peak_search_report_all.csv", all_rows)
            atomic_json(session_dir / "peak_search_report.json",
                {"best": best_rows, "observed": live_best, "all": all_rows, "complete": False})
            append_live_peak_round(args.live_results_csv, case, result, args.repeat_rate)
            logger.info("实时写入摸高轮次结果: case=%s, cc=%s, status=peak_round", case.name, result.concurrency)

        searcher = PeakSearcher(case, config, case_dir, logger, on_evaluation=on_evaluation)
        best_result, history = searcher.search()
        best_row = write_case_summary(case, config, case_dir, best_result, history)
        search_summary = dict(case_name=case.name, **searcher.summary())
        summaries.append(search_summary)
        status = {
            "boundary_confirmed": "peak_confirmed", "capped": "peak_capped",
            "budget_limit": "peak_budget", "no_pass": "peak_no_pass",
            "unstable": "peak_unstable", "request_error": "peak_request_error",
            "error": "peak_error", "cancelled": "peak_cancelled",
        }[searcher.termination]
        best_row.update(status=status, termination=searcher.termination,
                        selected_trial_id=search_summary["selected_trial_id"],
                        selected_scope_id=search_summary["selected_scope_id"],
                        evaluation_file=search_summary["evaluation_file"])
        best_row["reason"] = searcher.reason
        write_csv_report(case_dir / "best_result.csv", [best_row])
        atomic_json(case_dir / "peak_summary.json", search_summary)
        best_rows.append(best_row)
        # on_evaluation already persisted every probe; retain compatibility for unusual cached paths.
        if not any(item.get("case_name") == case.name for item in all_rows):
            all_rows.extend(trace_row(case, item, config) for item in history)
        if searcher.termination not in {"boundary_confirmed", "capped"}:
            all_passed = False

    write_csv_report(session_dir / "peak_search_report.csv", best_rows)
    write_csv_report(session_dir / "peak_search_report_all.csv", all_rows)
    write_markdown_report(session_dir / "peak_search_report.md", best_rows)
    atomic_json(session_dir / "peak_search_report.json",
        {"best": best_rows, "all": all_rows, "summaries": summaries, "complete": True})
    atomic_json(session_dir / "peak_summary.json", {"cases": summaries})
    write_csv_report(session_dir / "peak_summary.csv", summaries)
    logger.info("归档 manifest: %s", session_dir)
    logger.info("摸高执行完成，汇总目录: %s", session_dir)
    manifest = write_archive_manifest(
        session_dir,
        task_kind="peak-search",
        archive_name=session_name,
        run_tag=config.run_tag,
        output_root=output_root,
        source_root=BASE_DIR,
        extra_metadata={
            "case_count": len(config.cases),
            "best_report": str(session_dir / "peak_search_report.csv"),
            "all_report": str(session_dir / "peak_search_report_all.csv"),
            "json_report": str(session_dir / "peak_search_report.json"),
        },
    )
    return 0 if all_passed else 1


def cli_main():
    """Persist startup/report failures even before the search logger exists."""
    try:
        return main()
    except (Exception, KeyboardInterrupt, SystemExit) as error:
        if isinstance(error, SystemExit) and not error.code:
            return 0
        container_output = Path("/workspace/aisbench_outputs")
        root = container_output if container_output.is_dir() else RELEASE_ROOT / "outputs" / "default"
        diagnostic = root / "diagnostics" / ("peak_" + uuid.uuid4().hex + ".json")
        try:
            resolved = diagnostic.resolve()
            if resolved == BASE_DIR or BASE_DIR in resolved.parents:
                raise ValueError("diagnostics cannot be written inside native source")
            atomic_json(diagnostic, dict(state="cancelled" if isinstance(error, KeyboardInterrupt) else "error",
                                         exception=type(error).__name__, reason=str(error),
                                         at=datetime.now().astimezone().isoformat()))
            if os.name == "posix":
                diagnostic.chmod(0o600)
            print("peak execution failed (" + type(error).__name__ + "); private diagnostics=" + str(diagnostic),
                  file=sys.stderr)
        except (OSError, ValueError):
            print("peak execution failed; persistent diagnostics unavailable", file=sys.stderr)
        if isinstance(error, KeyboardInterrupt):
            return 130
        return error.code if isinstance(error, SystemExit) and isinstance(error.code, int) else 1


if __name__ == "__main__":
    sys.exit(cli_main())
