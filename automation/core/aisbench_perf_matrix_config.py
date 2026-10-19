import importlib.util
import os
import sys

sys.dont_write_bytecode = True


ADD_DIR = os.path.dirname(os.path.abspath(__file__))
AUTOMATION_ROOT = os.path.dirname(ADD_DIR) if os.path.basename(ADD_DIR) == "core" else ADD_DIR
RELEASE_ROOT = os.path.dirname(AUTOMATION_ROOT)
_NATIVE_DEFAULT = os.path.join(RELEASE_ROOT, "aisbench_auto_tools_prefix")
BASE_DIR = os.environ.get("AISBENCH_NATIVE_ROOT", _NATIVE_DEFAULT if os.path.isdir(_NATIVE_DEFAULT) else os.path.join(RELEASE_ROOT, "native"))


def _load_runtime_config():
    config_path = os.path.join(BASE_DIR, "config.py")
    spec = importlib.util.spec_from_file_location("aisbench_runtime_config", config_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load runtime config: %s" % config_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_RUNTIME_CONFIG = _load_runtime_config()
_PERF_GLOBAL_ARGS = dict(getattr(_RUNTIME_CONFIG, "PERF_GLOBAL_ARGS", {}) or {})


# Common args forwarded to aisbench_test.py for peak-search rounds.
GLOBAL_ARGS = {
    "dataset_type": "normal",
    "repeat": 1,
    "seed": 1,
    "npu_num": 1,
    "test_type": "stream",
}
GLOBAL_ARGS.update(_PERF_GLOBAL_ARGS)


# Extra raw CLI args forwarded to aisbench_test.py.
GLOBAL_FORWARD_ARGS = [
    # "--enable_think",
]


TTFT_COLUMN = "TTFT avg"
TPOT_COLUMN = "TPOT avg"


DEFAULT_REQUEST_RATE = "0"
DEFAULT_REQUEST_MULTIPLIER = 4
DEFAULT_TTFT_LIMIT = 10.0
DEFAULT_TPOT_LIMIT = 100.0
DEFAULT_WAIT_SECONDS = 10
AUTO_EXPAND_MAX_CONCURRENCY = 0


PYTHON_BIN = ""
RUN_TAG = ""


DEFAULT_CASE = {
    "name": "default_peak_case",
    "input_len": 2048,
    "output_len": 2048,
    "min_concurrency": 100,
    "max_concurrency": 200,
    "request_rate": DEFAULT_REQUEST_RATE,
    "request_multiplier": DEFAULT_REQUEST_MULTIPLIER,
    "ttft_limit": DEFAULT_TTFT_LIMIT,
    "tpot_limit": DEFAULT_TPOT_LIMIT,
    "wait_seconds": DEFAULT_WAIT_SECONDS,
    "args": {},
    "forward_args": [],
    "precision": 1,
    "confirm_runs": 3,
    "confirm_min_requests": 100,
    "max_trials": 40,
    "trial_timeout": 1800,
    "total_timeout": 14400,
    "invalid_retries": 1,
    "floor_concurrency": 1,
}


SEARCH_CASES = [
    {
        "name": "bf16_fp16_in2048_out2048",
        "input_len": 2048,
        "output_len": 2048,
        "min_concurrency": 100,
        "max_concurrency": 200,
        "request_rate": "0",
        "request_multiplier": 4,
        "ttft_limit": 10,
        "tpot_limit": 100,
        "wait_seconds": 10,
        "args": {},
        "forward_args": [],
    },
]
