#!/usr/bin/env bash
# 注意: 本脚本对每个测试/摸高轮次使用手动退出码处理（PERF_PIPE_RC / PEAK_RC / "|| fail"），
# 与 set -e 不兼容（失败轮次会在记录 failed 行之前直接退出整个脚本）。故仅保留 -u 与 pipefail。
set -uo pipefail

######################## 配置区（按需修改） ########################
# 使用前必须修改：
#   HOST_IP       模型服务所在机器 IP
#   HOST_PORT     模型服务端口
#   MODEL_NAME    模型服务名称
#   MODEL_HOST_PATH  模型在宿主机上的路径
#
# 可通过环境变量覆盖，无需改脚本：
#   MODEL_NAME=your-model HOST_IP=192.168.1.10 HOST_PORT=8000 bash run_perf_scenarios.sh

# 容器设置
CONTAINER_NAME="${CONTAINER_NAME:-ais_bench-example}"
EXPECTED_IMAGE="${EXPECTED_IMAGE:-ghcr.io/aisbench/aisbench_benchmark:v3.1-20261013-master-openeuler24.03-py311}"

ADD_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$ADD_DIR/.." && pwd)"

# 宿主机路径（默认取 add 的父目录，即原始工具根目录）
HOST_OUTPUT_DIR="${HOST_OUTPUT_DIR:-${PROJECT_ROOT}/outputs/default}"
MODEL_NAME="${MODEL_NAME:-your-model}"
MODEL_HOST_PATH="${MODEL_HOST_PATH:-/mnt/models/your-model}"
TOOLS_HOST_DIR="${TOOLS_HOST_DIR:-$PROJECT_ROOT}"

# 容器内固定路径
CONTAINER_OUTPUT_DIR="/workspace/aisbench_outputs"
MODEL_CONTAINER_PATH="/workspace/model"
TOOLS_CONTAINER_DIR="/workspace/aisbench_auto_tools_prefix-main"

# 服务地址（必须修改！）
HOST_IP="${HOST_IP:-192.168.1.10}"
HOST_PORT="${HOST_PORT:-8000}"

# API Key 认证（为空则不使用）
# 示例: API_KEY="sk-xxx"
API_KEY="${API_KEY:-}"

# 自定义请求 URL（为空则使用 http://{HOST_IP}:{HOST_PORT}/v1/chat/completions）# 示例: API_URL="http://192.168.1.10:8000"
API_URL="${API_URL:-}"

# Failed Requests 阈值：本轮失败请求数 > 此值即判为不满足（failed）。默认 0（出现任何失败请求即不满足）
FAILED_REQ_LIMIT="${FAILED_REQ_LIMIT:-0}"
MAX_CONTEXT_LEN="${MAX_CONTEXT_LEN:-512000}"
# NPU 卡数，用于传递给 aisbench_test.py 并计算单卡吞吐
NPU_NUM="${NPU_NUM:-1}"
export NPU_NUM
# Prefix Cache metrics endpoints, comma-separated: ip:port,ip:port
POD_INFO="${POD_INFO:-}"
export POD_INFO

# 固定并发测试请求数 = 并发数 × 此倍率（场景指定 data_num 时优先使用场景值）
DATA_NUM_MULTIPLIER="${DATA_NUM_MULTIPLIER:-10}"
export DATA_NUM_MULTIPLIER

# 摸高搜索默认参数（场景 JSON 可覆盖）
PEAK_MIN_CONCURRENCY="${PEAK_MIN_CONCURRENCY:-1}"
PEAK_MAX_MULTIPLIER="${PEAK_MAX_MULTIPLIER:-20}"      # max_concurrency = concurrency[0] * 此值
PEAK_DATA_MULTIPLIER="${PEAK_DATA_MULTIPLIER:-4}"     # 搜索步长
PEAK_WAIT_SECONDS="${PEAK_WAIT_SECONDS:-10}"          # 每轮等待秒数
PEAK_TTFT_LIMIT="${PEAK_TTFT_LIMIT:-5}"               # 默认 TTFT 阈值 (s)
PEAK_TPOT_LIMIT="${PEAK_TPOT_LIMIT:-50}"              # 默认 TPOT 阈值 (ms)
# 摸高硬顶开关: 0=允许通过初始上界后继续扩容, 1=以场景 peak_max_cc 为硬上限
PEAK_HARD_CAP="${PEAK_HARD_CAP:-0}"

# -------------------- 场景定义（直接修改这个 JSON） --------------------
# 必填: name, input_len, output_len, concurrency(并发值数组), request_rate
# 可选: 传给 aisbench_test.py 的额外参数（如 dataset_type, prefix_num 等）
# concurrency 数组中用 "max" 触发摸高搜索，需配合:
#   ttft_limit        TTFT 阈值 (s)
#   tpot_limit        TPOT 阈值 (ms)
#   peak_min_cc       peak search lower bound
#   peak_max_cc       peak search upper bound
#   peak_data_multiplier data_num multiplier
#   peak_wait         wait seconds between peak-search rounds
SCENARIOS_JSON=$(cat <<'JSON'
[
  {
    "name": "0_ai_coding_64k256",
    "input_len": "65536",
    "output_len": "256",
    "concurrency": [1, 10, 20, 50, 100, "max"],
    "request_rate": "0",
    "ttft_limit": 15,
    "tpot_limit": 60,
    "peak_min_cc": 1,
    "peak_max_cc": 200,
    "peak_data_multiplier": 4,
    "peak_wait": 10,
    "dataset_type": "prefix_cache",
    "prefix_num": "1",
    "repeat_rate": "0.9",
    "prefix_test": true
  },
  {
    "name": "0_ai_coding_64k1k",
    "input_len": "65536",
    "output_len": "1024",
    "concurrency": [1, 10, 20, 50, 100, "max"],
    "request_rate": "0",
    "ttft_limit": 15,
    "tpot_limit": 60,
    "peak_min_cc": 1,
    "peak_max_cc": 200,
    "peak_data_multiplier": 4,
    "peak_wait": 10,
    "dataset_type": "prefix_cache",
    "prefix_num": "1",
    "repeat_rate": "0.9",
    "prefix_test": true
  }
]
JSON
)
SCENARIOS_JSON="${SCENARIOS_JSON_OVERRIDE:-$SCENARIOS_JSON}"
# ----------------------------------------------------------------------

######################## 配置区（按需修改） ########################
log() { echo "[$(date '+%H:%M:%S')] $*"; }
fail() { log "ERROR: $*"; exit 1; }

append_empty_result() {
    python3 - "$RESULTS_CSV" "$CSV_HEADER" "$@" <<'PY'
import csv
import sys

columns = next(csv.reader([sys.argv[2]]))
metadata = ["scenario", "current_time", "input_len", "output_len", "max_cc", "cc", "rr", "repeat_rate", "status"]
if len(columns) != 23 or len(columns) != len(set(columns)) or len(sys.argv[3:]) != len(metadata):
    raise ValueError("invalid empty-result schema or argument count")
row = dict.fromkeys(columns, "")
row.update(zip(metadata, sys.argv[3:]))
with open(sys.argv[1], "a", encoding="utf-8", newline="") as handle:
    csv.DictWriter(handle, fieldnames=columns).writerow(row)
PY
}

######################## 3. 解析场景 JSON 并自动展开并发 ########################
log "校验并展开场景配置..."
SCENARIOS_JSON_FILE=$(mktemp /tmp/scenarios_XXXXXX.json)
trap 'rm -f -- "${SCENARIOS_JSON_FILE:-}" "${COMBOS_FILE:-}" "${PEAK_COMBOS_FILE:-}" "${CONTAINER_LOG_TMP:-}" "${PEAK_METRICS_FILE:-}"' EXIT
echo "$SCENARIOS_JSON" > "$SCENARIOS_JSON_FILE"

# 解析结果写入临时文件：COMBOS_FILE=固定并发, PEAK_COMBOS_FILE=摸高
COMBOS_FILE=$(mktemp /tmp/combos_XXXXXX.txt)
PEAK_COMBOS_FILE=$(mktemp /tmp/peak_combos_XXXXXX.txt)
> "$COMBOS_FILE"
> "$PEAK_COMBOS_FILE"

if ! python3 - "$SCENARIOS_JSON_FILE" "$COMBOS_FILE" "$PEAK_COMBOS_FILE" \
  "$PEAK_MIN_CONCURRENCY" "$PEAK_MAX_MULTIPLIER" "$PEAK_DATA_MULTIPLIER" "$PEAK_WAIT_SECONDS" "$PEAK_TTFT_LIMIT" "$PEAK_TPOT_LIMIT" "$MAX_CONTEXT_LEN" "$FAILED_REQ_LIMIT" "$PEAK_HARD_CAP" <<'PY'
import json, sys, os, math, re, shlex

def integer(value, name, minimum=1):
    if isinstance(value, bool) or not re.fullmatch(r"[+-]?\d+", str(value)):
        raise ValueError(f"{name} must be an integer >= {minimum}")
    number = int(value)
    if minimum is not None and number < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return number

def number(value, name, minimum=None, maximum=None):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(result) or (minimum is not None and result < minimum) or (maximum is not None and result > maximum):
        raise ValueError(f"{name} is outside its valid finite range")
    return result

scenarios = json.loads(open(sys.argv[1], encoding="utf-8").read())
if not isinstance(scenarios, list) or not scenarios:
    raise ValueError("scenarios must be a non-empty array")
combos_file, peak_file = sys.argv[2:4]
peak_min_cc = integer(sys.argv[4], "PEAK_MIN_CONCURRENCY")
peak_max_mult = integer(sys.argv[5], "PEAK_MAX_MULTIPLIER")
peak_data_multiplier = integer(sys.argv[6], "PEAK_DATA_MULTIPLIER")
peak_wait = integer(sys.argv[7], "PEAK_WAIT_SECONDS", 0)
peak_ttft_default = number(sys.argv[8], "PEAK_TTFT_LIMIT")
peak_tpot_default = number(sys.argv[9], "PEAK_TPOT_LIMIT")
max_context_len = integer(sys.argv[10], "MAX_CONTEXT_LEN", 0)
integer(sys.argv[11], "FAILED_REQ_LIMIT", 0)
if sys.argv[12] not in ("0", "1"):
    raise ValueError("PEAK_HARD_CAP must be 0 or 1")
integer(os.environ['NPU_NUM'], "NPU_NUM")
DATA_NUM_MULTIPLIER = integer(os.environ['DATA_NUM_MULTIPLIER'], "DATA_NUM_MULTIPLIER")
defaults = {
    'npu_num': os.environ['NPU_NUM'], 'test_type': 'stream',
    'repeat': '1', 'dataset_type': 'normal', 'prefix_num': '1',
    'repeat_rate': '0', 'seed': '1', 'dp': '1'
}

def format_cli_arg(key, value):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key):
        raise ValueError(f"invalid argument name: {key!r}")
    if isinstance(value, bool):
        return f'--{key}' if value else ''
    if not isinstance(value, (str, int, float)) or any(char in str(value) for char in '\r\n\0'):
        raise ValueError(f"unsupported argument value for {key}")
    return f'--{key} {shlex.quote(str(value))}'

combo_lines, peak_lines, names = [], [], set()
task_names, task_slugs = set(), set()
def register_task(name):
    slug = ''.join(char if char.isalnum() or char in '-_' else '_' for char in name.strip()).strip('_') or 'task'
    if name in task_names or slug in task_slugs:
        raise ValueError(f"duplicate task name or archive slug: {name}")
    task_names.add(name)
    task_slugs.add(slug)
for sc in scenarios:
    if not isinstance(sc, dict):
        raise ValueError("each scenario must be an object")
    name = sc.get('name')
    if not isinstance(name, str) or not name.strip() or any(char in name for char in "'|\r\n\0"):
        raise ValueError("scenario name must be non-empty and cannot contain a single quote, pipe or newline")
    if name in names:
        raise ValueError(f"duplicate scenario name: {name}")
    names.add(name)
    input_len = integer(sc.get('input_len', '512'), f"{name}.input_len")
    output_len = integer(sc.get('output_len', '128'), f"{name}.output_len")
    request_rate = sc.get('request_rate', '0')
    number(request_rate, f"{name}.request_rate", 0)
    concurrency_values = sc.get('concurrency', [1])
    if not isinstance(concurrency_values, list) or not concurrency_values:
        raise ValueError(f"{name}.concurrency must be a non-empty array")
    concurrency_values = [c if c == 'max' else integer(c, f"{name}.concurrency") for c in concurrency_values]
    if concurrency_values.count('max') > 1:
        raise ValueError(f"{name}.concurrency may contain max only once")
    special_keys = {'name', 'input_len', 'output_len', 'concurrency', 'request_rate',
                    'ttft_limit', 'tpot_limit', 'peak_min_cc', 'peak_max_cc',
                    'peak_data_multiplier', 'peak_wait'}
    extra = {k: v for k, v in sc.items() if k not in special_keys}
    for key in ('data_num', 'npu_num', 'repeat', 'prefix_num', 'dp'):
        if key in extra:
            integer(extra[key], f"{name}.{key}")
    if 'seed' in extra:
        integer(extra['seed'], f"{name}.seed", None)
    for key in ('prefix_test', 'enable_think', 'test_accuracy'):
        if key in extra and not isinstance(extra[key], bool):
            raise ValueError(f"{name}.{key} must be a JSON boolean")
    if 'repeat_rate' in extra:
        ratio = str(extra['repeat_rate']).strip()
        extra['repeat_rate'] = ratio
        if ratio.endswith('%'):
            ratio = number(ratio[:-1], f"{name}.repeat_rate") / 100
        number(ratio, f"{name}.repeat_rate", 0, 1)
    for key, choices in (('dataset_type', ('normal', 'prefix_cache')), ('test_type', ('text', 'stream'))):
        if key in extra and extra[key] not in choices:
            raise ValueError(f"{name}.{key} must be one of {choices}")
    for first, second in (('length_mean', 'length_std'), ('length_min', 'length_max')):
        if (first in extra) != (second in extra):
            raise ValueError(f"{name}.{first} and {second} must be provided together")
    for key in ('length_mean', 'length_min', 'length_max'):
        if key in extra:
            integer(extra[key], f"{name}.{key}")
    if 'length_std' in extra:
        number(extra['length_std'], f"{name}.length_std", 0)
    effective_length = input_len
    if 'length_max' in extra:
        # The upstream sampler swaps reversed min/max bounds.
        effective_length = max(int(extra['length_min']), int(extra['length_max']))
    elif 'length_mean' in extra:
        effective_length = int(extra['length_mean'])
    if max_context_len > 0 and effective_length + output_len > max_context_len:
        raise ValueError(f"{name}.configured length plus output exceeds MAX_CONTEXT_LEN")
    fixed_concs = [c for c in concurrency_values if c != 'max']
    has_peak = 'max' in concurrency_values
    if has_peak and extra.get('test_type', defaults['test_type']) != 'stream':
        raise ValueError(f"{name}: peak search requires test_type=stream")
    if has_peak and output_len < 2:
        raise ValueError(f"{name}: peak search requires output_len >= 2 for TPOT")
    fixed_occurrences = {}
    for conc in fixed_concs:
        data_num = str(extra['data_num']) if 'data_num' in extra else str(conc * DATA_NUM_MULTIPLIER)
        params = {'input_len': input_len, 'output_len': output_len,
                  'concurrency': str(conc), 'request_rate': request_rate, 'data_num': data_num}
        for k, v in defaults.items():
            if k not in extra:
                params[k] = v
        params.update(extra)
        cmd_str = ' '.join(item for item in (format_cli_arg(k, v) for k, v in params.items()) if item)
        archive_name = f'{name}_c{conc}'
        # Keep every fixed entry in order and give repeated runs distinct artifacts.
        fixed_occurrences[conc] = fixed_occurrences.get(conc, 0) + 1
        if fixed_occurrences[conc] > 1:
            archive_name += f'_run{fixed_occurrences[conc]}'
        register_task(archive_name)
        combo_lines.append(f'{archive_name}|{cmd_str}')
    if has_peak:
        ttft = sc.get('ttft_limit', peak_ttft_default)
        tpot = sc.get('tpot_limit', peak_tpot_default)
        number(ttft, f"{name}.ttft_limit")
        number(tpot, f"{name}.tpot_limit")
        min_cc = integer(sc.get('peak_min_cc', peak_min_cc), f"{name}.peak_min_cc")
        max_cc = integer(sc.get('peak_max_cc', (fixed_concs[0] if fixed_concs else peak_min_cc) * peak_max_mult), f"{name}.peak_max_cc")
        if min_cc > max_cc:
            raise ValueError(f"{name}.peak_min_cc must not exceed peak_max_cc")
        step = integer(sc.get('peak_data_multiplier', peak_data_multiplier), f"{name}.peak_data_multiplier")
        wait = integer(sc.get('peak_wait', peak_wait), f"{name}.peak_wait", 0)
        forward_str = ' '.join(item for item in (format_cli_arg(k, v) for k, v in extra.items()) if item)
        register_task(f'{name}_peak')
        peak_lines.append(f'{name}_peak|{input_len}|{output_len}|{min_cc}|{max_cc}|{step}|{request_rate}|{ttft}|{tpot}|{wait}|{forward_str}')
with open(combos_file, 'w', encoding='utf-8') as f:
    f.write('\n'.join(combo_lines))
with open(peak_file, 'w', encoding='utf-8') as f:
    f.write('\n'.join(peak_lines))
PY
then
    fail "场景 JSON 解析失败或超过 MAX_CONTEXT_LEN=$MAX_CONTEXT_LEN"
fi

rm -f "$SCENARIOS_JSON_FILE"

# 读取结果
COMBOS=$(cat "$COMBOS_FILE" 2>/dev/null || true)
PEAK_COMBOS=$(cat "$PEAK_COMBOS_FILE" 2>/dev/null || true)
rm -f "$COMBOS_FILE" "$PEAK_COMBOS_FILE"

FIXED_COUNT=$(printf '%s\n' "$COMBOS" | awk 'NF{c++} END{print c+0}')
PEAK_COUNT=$(printf '%s\n' "$PEAK_COMBOS" | awk 'NF{c++} END{print c+0}')
TOTAL_TESTS=$((FIXED_COUNT + PEAK_COUNT))

if [ "$TOTAL_TESTS" -eq 0 ]; then
    fail "场景 JSON 解析失败或未生成任何组合"
fi

log "fixed_count=$FIXED_COUNT peak_count=$PEAK_COUNT total_tests=$TOTAL_TESTS"


append_source_result() {
    python3 - "$RESULTS_CSV" "$CSV_HEADER" "$@" <<'PY'
import csv, io, sys
columns = next(csv.reader([sys.argv[2]]))
rows = list(csv.reader(io.StringIO(sys.argv[4]), strict=True))
if len(columns) != 23 or len(rows) != 1 or len(rows[0]) != 20:
    raise ValueError("invalid source row or summary schema")
with open(sys.argv[1], "a", encoding="utf-8", newline="") as handle:
    csv.writer(handle).writerow([sys.argv[3], *rows[0], sys.argv[5], sys.argv[6]])
PY
}

read_request_counts() {
    python3 - "$1" "$FAILED_REQ_LIMIT" <<'PY'
import re, sys
from decimal import Decimal, InvalidOperation
counts = {}
labels = re.compile(r"\b(Failed|Success) Requests\b")
pattern = re.compile(r"\s*(?:[|│]\s*(?:total\s*[|│]\s*)?|[:=]\s*)?([+\-]?\d[\d,]*(?:\.\d+)?)\s*(?:[|│].*)?")
with open(sys.argv[1], encoding="utf-8", errors="replace") as handle:
    for line in handle:
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
        for labelled in labels.finditer(line):
            kind = labelled.group(1)
            counts[kind] = None
            match = pattern.fullmatch(line[labelled.end():].strip())
            if not match:
                continue
            token = match.group(1)
            if "," in token and not re.fullmatch(r"[+\-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", token):
                continue
            try:
                number = Decimal(token.replace(",", ""))
                if number >= 0 and number == number.to_integral_value():
                    counts[kind] = int(number)
            except (InvalidOperation, ValueError, OverflowError):
                pass
failed, success = counts.get("Failed"), counts.get("Success")
rejected = failed is None or success is None or success == 0 or failed > int(sys.argv[2])
print(f"{'' if failed is None else failed}|{'' if success is None else success}|{'yes' if rejected else 'no'}")
PY
}

######################## 0. 初始化结果保存目录 ########################
RUN_TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
RESULTS_HOST_DIR="${HOST_OUTPUT_DIR}/scenario_results/${RUN_TIMESTAMP}"
mkdir -p "$RESULTS_HOST_DIR"

RESULTS_CSV="${RESULTS_HOST_DIR}/all_results.csv"
RESULTS_LOG="${RESULTS_HOST_DIR}/execution.log"

log "结果保存目录: $RESULTS_HOST_DIR"

######################## 1. 容器检查/复用/重建 ########################
log "开始容器检查/复用/重建..."
mkdir -p "$HOST_OUTPUT_DIR"

ACTION="reused"
REASON="image and mount checks passed; reuse existing container"

[ -d "$MODEL_HOST_PATH" ] || fail "宿主机模型目录不存在: $MODEL_HOST_PATH"
[ -f "$TOOLS_HOST_DIR/aisbench_test.py" ] || fail "工具目录缺少 aisbench_test.py: $TOOLS_HOST_DIR"
CONTAINER_LIST=$(docker ps -a --format '{{.Names}}') || fail "查询容器列表失败"
if ! grep -Fx "$CONTAINER_NAME" <<< "$CONTAINER_LIST" >/dev/null; then
    ACTION="created"
    REASON="container not found"
fi

if [ "$ACTION" = "reused" ]; then
    CURRENT_ID=$(docker inspect --format '{{.Id}}' "$CONTAINER_NAME" 2>/dev/null) || fail "查询现有容器身份失败"
    [ -n "$CURRENT_ID" ] || fail "现有容器身份为空"
    CURRENT_IMAGE=$(docker inspect --format '{{.Config.Image}}' "$CURRENT_ID" 2>/dev/null) || fail "查询容器镜像失败"
    CURRENT_MANAGED=$(docker inspect --format '{{index .Config.Labels "io.aisbench-auto-tools-prefix.managed"}}' "$CURRENT_ID" 2>/dev/null) || fail "查询工具容器标识失败"
    [ "$CURRENT_MANAGED" = "true" ] || fail "容器命名冲突: 缺少专用AISBench工具标识，拒绝修改 $CONTAINER_NAME"
    case "$CURRENT_IMAGE" in
        "$EXPECTED_IMAGE"|ghcr.io/aisbench/aisbench_benchmark:*|ghcr.io/aisbench/aisbench_benchmark@sha256:*) ;;
        *) fail "容器命名冲突: 未确认的AISBench镜像，拒绝修改 $CONTAINER_NAME" ;;
    esac
    [ "$CURRENT_IMAGE" != "$EXPECTED_IMAGE" ] && { ACTION="recreated"; REASON="container image mismatch"; }
fi

if [ "$ACTION" = "reused" ]; then
    CURRENT_OUTPUT_SOURCE=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "'"$CONTAINER_OUTPUT_DIR"'"}}{{.Source}}{{end}}{{end}}' "$CURRENT_ID" 2>/dev/null) || fail "查询容器输出挂载失败"
    [ "$CURRENT_OUTPUT_SOURCE" != "$HOST_OUTPUT_DIR" ] && { ACTION="recreated"; REASON="output mount mismatch"; }
fi

if [ "$ACTION" = "reused" ]; then
    CURRENT_MODEL_SOURCE=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "'"$MODEL_CONTAINER_PATH"'"}}{{.Source}}{{end}}{{end}}' "$CURRENT_ID" 2>/dev/null) || fail "查询容器模型挂载失败"
    [ "$CURRENT_MODEL_SOURCE" != "$MODEL_HOST_PATH" ] && { ACTION="recreated"; REASON="model mount mismatch"; }
fi

if [ "$ACTION" = "reused" ]; then
    CURRENT_RUNNING=$(docker inspect --format '{{.State.Running}}' "$CURRENT_ID" 2>/dev/null) || fail "查询容器运行状态失败"
    if [ "$CURRENT_RUNNING" != "true" ]; then
        docker start "$CURRENT_ID" >/dev/null 2>&1 || fail "启动已有容器失败"
    fi
fi

if [ "$ACTION" = "reused" ]; then
    if ! docker exec "$CURRENT_ID" bash -lc 'test -e '"$MODEL_CONTAINER_PATH"'' >/dev/null 2>&1; then
        [ -e "$MODEL_HOST_PATH" ] && { ACTION="recreated"; REASON="容器内模型路径不存在"; } || fail "宿主机模型路径不存在: $MODEL_HOST_PATH"
    fi
fi

if [ "$ACTION" != "reused" ]; then
    log "重建容器 (原因: $REASON)"
    if [ "$ACTION" = "recreated" ]; then
        docker rm -f "$CURRENT_ID" >/dev/null 2>&1 || fail "移除已确认的工具容器失败"
    fi
    DOCKER_RUN_CMD="docker run -d --name '$CONTAINER_NAME' --label io.aisbench-auto-tools-prefix.managed=true --ipc=host --net=host --privileged --shm-size=1g -v '$HOST_OUTPUT_DIR':'$CONTAINER_OUTPUT_DIR'"
    [ -e "$MODEL_HOST_PATH" ] && DOCKER_RUN_CMD+=" -v '$MODEL_HOST_PATH':'$MODEL_CONTAINER_PATH'"
    DOCKER_RUN_CMD+=" '$EXPECTED_IMAGE' bash -lc 'trap : TERM INT; while true; do sleep 3600; done'"
    eval "$DOCKER_RUN_CMD" >/dev/null || fail "容器启动失败"
fi

docker exec "$CONTAINER_NAME" bash -lc 'mkdir -p /workspace /workspace/aisbench_auto_tools_prefix-main /workspace/aisbench_dataset /workspace/aisbench_outputs' || true
docker exec "$CONTAINER_NAME" bash -lc 'test -e '"$MODEL_CONTAINER_PATH"'' >/dev/null 2>&1 || fail "容器内模型路径不存在"

printf '===PERF_CONTAINER_ACTION===\n%s\n' "$ACTION"
printf '===PERF_CONTAINER_REASON===\n%s\n' "$REASON"

######################## 2. 同步工具 + 查询 WORK_PATH + 写入 config.py ########################
log "同步工具目录..."
docker exec "$CONTAINER_NAME" bash -lc 'mkdir -p /workspace/aisbench_auto_tools_prefix-main' || true
# 通过 tar 管道同步，排除 outputs/（结果目录，按运行累积，无需拷入容器）避免逐轮冗余拷贝
tar -C "$TOOLS_HOST_DIR" --exclude='./outputs' -cf - . \
    | docker exec -i "$CONTAINER_NAME" tar -C /workspace/aisbench_auto_tools_prefix-main -xf - \
    || fail "同步失败"

TOOL_ROOT="$TOOLS_CONTAINER_DIR"
TOOL_ROOT=$(docker exec "$CONTAINER_NAME" bash -lc "
    if [ -d '$TOOL_ROOT/aisbench_auto_tools_prefix-main' ]; then
        echo '$TOOL_ROOT/aisbench_auto_tools_prefix-main'
    else
        echo '$TOOL_ROOT'
    fi") || TOOL_ROOT="$TOOLS_CONTAINER_DIR"

log "查询 ais-bench-benchmark 安装路径..."
WORK_PATH_DETECTED=$(docker exec "$CONTAINER_NAME" bash -lc "
    W=\$(python3 -m pip show ais-bench-benchmark 2>/dev/null | sed -n 's/^Editable project location: //p' | head -1)
    [ -z \"\$W\" ] && W=\$(python3 -m pip show ais-bench-benchmark 2>/dev/null | sed -n 's/^Location: //p' | head -1)
    [ -z \"\$W\" ] && { echo 'ERROR'; exit 1; }
    echo \"\$W\"") || fail "查询 WORK_PATH 失败"
log "WORK_PATH=$WORK_PATH_DETECTED"

CONFIG_JSON=$(WORK_PATH_DETECTED="$WORK_PATH_DETECTED" MODEL_NAME="$MODEL_NAME" HOST_IP="$HOST_IP" HOST_PORT="$HOST_PORT" API_KEY="$API_KEY" API_URL="$API_URL" POD_INFO="$POD_INFO" python3 - <<'PY'
import json, os
pod_info = [item.strip() for item in os.environ.get("POD_INFO", "").split(",") if item.strip()]
print(json.dumps({
    "DATASET_PATH": "/workspace/aisbench_dataset",
    "WORK_PATH": os.environ["WORK_PATH_DETECTED"],
    "MODEL_NAME": os.environ["MODEL_NAME"],
    "MODEL_PATH": "/workspace/model",
    "HOST_IP": os.environ["HOST_IP"],
    "HOST_PORT": os.environ["HOST_PORT"],
    "DEFAULT_PERFORMANCE_TEST": "default_perf",
    "OUTPUT_DIR": "/workspace/aisbench_outputs",
    "POD_INFO": pod_info,
    "API_KEY": os.environ["API_KEY"],
    "URL": os.environ["API_URL"],
}, ensure_ascii=False))
PY
)
CONFIG_JSON_B64=$(printf '%s' "$CONFIG_JSON" | base64 | tr -d '\n')

log "写入 config.py..."
docker exec "$CONTAINER_NAME" bash -lc "
export WORK_PATH_DETECTED='$WORK_PATH_DETECTED'
cd '$TOOL_ROOT' || exit 1
python3 - config.py '$CONFIG_JSON_B64' <<'PY'
import base64, json, os, pathlib, re, sys
path = pathlib.Path(sys.argv[1])
cfg = json.loads(base64.b64decode(sys.argv[2]).decode('utf-8'))
cfg[\"WORK_PATH\"] = os.environ.get(\"WORK_PATH_DETECTED\", cfg.get(\"WORK_PATH\", \"\"))
text = path.read_text(encoding=\"utf-8\")
for key, value in cfg.items():
    replacement = f\"{key} = {json.dumps(value, ensure_ascii=False)}\"
    text, count = re.subn(rf\"^{key}\s*=.*$\", lambda _: replacement, text, flags=re.M)
    if count == 0:
        text += f\"\n{replacement}\n\"
path.write_text(text, encoding=\"utf-8\")
PY
" || fail "写入 config.py 失败"

######################## 4. 循环执行固定并发测试 + 结果采集 ########################
# 初始化结果累加 CSV（写入表头）
CSV_HEADER="scenario,current_time,input_len,output_len,total_req,max_cc,cc,rr,TTFT avg,TTFT P90,TPOT avg,TPOT SLO_P90,E2E_time,output_throughput,single_output_throughput,E2E_throughput,single_E2E_throughput,qps,qpm,input_token_throughput,prefill_token_throughput,repeat_rate,status"
echo "$CSV_HEADER" > "$RESULTS_CSV"

TEST_INDEX=0
PASSED_COUNT=0
FAILED_COUNT=0

if [ "$FIXED_COUNT" -gt 0 ]; then
log "start fixed concurrency tests ($FIXED_COUNT groups)"

while IFS='|' read -r ARCHIVE_NAME CMD_PARAMS; do
    [ -z "$ARCHIVE_NAME" ] && continue
    TEST_INDEX=$((TEST_INDEX + 1))
    log "[$TEST_INDEX/$TOTAL_TESTS] 开始测试: $ARCHIVE_NAME"
    ARCHIVE_SINCE_TS=$(python3 -c 'import time; print(time.time())')

    # 提取场景参数用于失败时的记录
    SCENARIO_INPUT_LEN=$(echo "$CMD_PARAMS" | sed -n 's/.*--input_len \([^ ]*\).*/\1/p')
    SCENARIO_OUTPUT_LEN=$(echo "$CMD_PARAMS" | sed -n 's/.*--output_len \([^ ]*\).*/\1/p')
    SCENARIO_CONCURRENCY=$(echo "$CMD_PARAMS" | sed -n 's/.*--concurrency \([^ ]*\).*/\1/p')
    SCENARIO_REQUEST_RATE=$(echo "$CMD_PARAMS" | sed -n 's/.*--request_rate \([^ ]*\).*/\1/p')
    SCENARIO_REPEAT_RATE=$(echo "$CMD_PARAMS" | sed -n 's/.*--repeat_rate \([^ ]*\).*/\1/p')
    : "${SCENARIO_REQUEST_RATE:=0}"
    : "${SCENARIO_REPEAT_RATE:=0}"
    SCENARIO_TEST_TYPE=$(python3 - "$CMD_PARAMS" <<'PY'
import argparse, shlex, sys
parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
parser.add_argument('--test_type', choices=('stream', 'text'), default='stream')
args, _ = parser.parse_known_args(shlex.split(sys.argv[1]))
print(args.test_type)
PY
    ) || fail "无法解析实际测试模式"

    # 执行测试 + 归档 + 结果提取
    # Capture container output in a temp file and tee it to the runtime log.
    CONTAINER_LOG_TMP=$(mktemp /tmp/container_output_XXXXXX.log)
    docker exec "$CONTAINER_NAME" bash -lc "
        export PYTHONPATH='$WORK_PATH_DETECTED'\${PYTHONPATH:+:\$PYTHONPATH}
        cd '$TOOL_ROOT' || exit 1
        rm -f aisbench.log aisbench_result.csv temp_api.py
        python3 aisbench_test.py $CMD_PARAMS
        PERF_EXIT=\$?
        if [ \$PERF_EXIT -ne 0 ]; then
            echo '性能测试失败'
            exit \$PERF_EXIT
        fi
        python3 add/aisbench_artifact_archive.py performance \
            --archive-name '$ARCHIVE_NAME' \
            --output-dir '$CONTAINER_OUTPUT_DIR' \
            --test-type '$SCENARIO_TEST_TYPE' \
            --since-ts '$ARCHIVE_SINCE_TS'
        ARCHIVE_EXIT=\$?
        if [ \$ARCHIVE_EXIT -ne 0 ]; then
            echo '性能测试失败'
            exit \$ARCHIVE_EXIT
        fi
        # 按列名投影最后一行，兼容原仓新增字段；保持汇总所需的20列顺序。
        RESULT_CSV_FILE='$TOOL_ROOT/aisbench_result.csv'
        if [ -f \"\$RESULT_CSV_FILE\" ]; then
            NORMALIZED_RESULT=\$(python3 add/aisbench_result_adapter.py \"\$RESULT_CSV_FILE\" --test-type '$SCENARIO_TEST_TYPE') || exit \$?
            echo '===AISBENCH_RESULT==='
            echo \"\$RESULT_CSV_FILE\"
            printf '%s\\n' \"\$NORMALIZED_RESULT\"
        else
            echo 'aisbench_result.csv missing' >&2
            exit 1
        fi
    " 2>&1 | tee "$CONTAINER_LOG_TMP"
    PERF_PIPE_RC=${PIPESTATUS[0]}
    CONTAINER_OUTPUT=$(cat "$CONTAINER_LOG_TMP")
    REQUEST_COUNTS=$(read_request_counts "$CONTAINER_LOG_TMP") || fail "请求计数解析失败"
    IFS='|' read -r FAILED_REQ_COUNT SUCCESS_REQ_COUNT REQUEST_FAILED <<< "$REQUEST_COUNTS"

    # 追加到执行日志
    echo "$CONTAINER_OUTPUT" >> "$RESULTS_LOG"
    rm -f "$CONTAINER_LOG_TMP"

    if [ $PERF_PIPE_RC -ne 0 ]; then
        FAILED_COUNT=$((FAILED_COUNT + 1))
        log "[$TEST_INDEX/$TOTAL_TESTS] 失败: $ARCHIVE_NAME (退出码: $PERF_PIPE_RC)"
        append_empty_result "$ARCHIVE_NAME" "" "$SCENARIO_INPUT_LEN" "$SCENARIO_OUTPUT_LEN" "" "$SCENARIO_CONCURRENCY" "$SCENARIO_REQUEST_RATE" "$SCENARIO_REPEAT_RATE" "failed"
        continue
    fi

    # Extract the final aisbench result CSV row from container output.
    RESULT_ROW=""
    if echo "$CONTAINER_OUTPUT" | grep -q '===AISBENCH_RESULT==='; then
        RESULT_ROW=$(echo "$CONTAINER_OUTPUT" | awk 'BEGIN{c=0} /===AISBENCH_RESULT===/{c=1; next} c==1{c=2; next} c==2{print; exit}')
    fi

    if [ -z "$RESULT_ROW" ]; then
        FAILED_COUNT=$((FAILED_COUNT + 1))
        log "警告: 无法提取 $ARCHIVE_NAME 的结果行"
        append_empty_result "$ARCHIVE_NAME" "" "$SCENARIO_INPUT_LEN" "$SCENARIO_OUTPUT_LEN" "" "$SCENARIO_CONCURRENCY" "$SCENARIO_REQUEST_RATE" "$SCENARIO_REPEAT_RATE" "no_result"
        continue
    fi

    if [ "$REQUEST_FAILED" = "yes" ]; then
        FAILED_COUNT=$((FAILED_COUNT + 1))
        log "[$TEST_INDEX/$TOTAL_TESTS] 请求失败: $ARCHIVE_NAME (成功=${SUCCESS_REQ_COUNT:-0} 失败=${FAILED_REQ_COUNT:-?})"
        if [ -n "$RESULT_ROW" ]; then
            append_source_result "$ARCHIVE_NAME" "$RESULT_ROW" "$SCENARIO_REPEAT_RATE" "failed" || fail "失败结果写入失败"
        else
            append_empty_result "$ARCHIVE_NAME" "" "$SCENARIO_INPUT_LEN" "$SCENARIO_OUTPUT_LEN" "" "$SCENARIO_CONCURRENCY" "$SCENARIO_REQUEST_RATE" "$SCENARIO_REPEAT_RATE" "failed"
        fi
        continue
    fi

    append_source_result "$ARCHIVE_NAME" "$RESULT_ROW" "$SCENARIO_REPEAT_RATE" "passed" || fail "成功结果写入失败"
    PASSED_COUNT=$((PASSED_COUNT + 1))
    log "[$TEST_INDEX/$TOTAL_TESTS] 测试通过: $ARCHIVE_NAME"

done <<< "$COMBOS"

log "固定并发测试完成: 通过=$PASSED_COUNT, 失败=$FAILED_COUNT"
else
    log "无固定并发测试，跳过"
fi

######################## 4.5 摸高搜索 ########################
if [ "$PEAK_COUNT" -gt 0 ]; then
log "开始摸高搜索 ($PEAK_COUNT 组)..."

while IFS='|' read -r ARCHIVE_NAME PEAK_INPUT_LEN PEAK_OUTPUT_LEN PEAK_MIN_CC PEAK_MAX_CC PEAK_DATA_MULTIPLIER_VAL PEAK_REQUEST_RATE PEAK_TTFT PEAK_TPOT PEAK_WAIT PEAK_FORWARD; do
    [ -z "$ARCHIVE_NAME" ] && continue
    PEAK_REPEAT_RATE=$(echo "$PEAK_FORWARD" | sed -n 's/.*--repeat_rate \([^ ]*\).*/\1/p')
    : "${PEAK_REPEAT_RATE:=0}"
    PEAK_CURRENT_TIME=$(date '+%Y-%m-%d %H:%M:%S')
    TEST_INDEX=$((TEST_INDEX + 1))
    log "[$TEST_INDEX/$TOTAL_TESTS] 摸高搜索: $ARCHIVE_NAME (并发 $PEAK_MIN_CC~$PEAK_MAX_CC, TTFT<$PEAK_TTFT s, TPOT<$PEAK_TPOT ms)"

    # 构建 aisbench_perf_matrix.py 命令
    PEAK_RUN_TAG="${RUN_TIMESTAMP}_${TEST_INDEX}"
    PEAK_CMD="python3 add/aisbench_perf_matrix.py \
        --config-file add/aisbench_perf_matrix_config.py \
        --run-tag '$PEAK_RUN_TAG' \
        --case-name '$ARCHIVE_NAME' \
        --input-len $PEAK_INPUT_LEN \
        --output-len $PEAK_OUTPUT_LEN \
        --min-concurrency $PEAK_MIN_CC \
        --max-concurrency $PEAK_MAX_CC \
        --request-multiplier $PEAK_DATA_MULTIPLIER_VAL \
        --request-rate $PEAK_REQUEST_RATE \
        --ttft-limit $PEAK_TTFT \
        --tpot-limit $PEAK_TPOT \
        --wait-seconds $PEAK_WAIT \
        --failed-req-limit $FAILED_REQ_LIMIT \
        --npu_num '$NPU_NUM' \
        --live-results-csv '$CONTAINER_OUTPUT_DIR/scenario_results/$RUN_TIMESTAMP/all_results.csv' \
        --repeat-rate '$PEAK_REPEAT_RATE'"
    [ -n "$PEAK_FORWARD" ] && PEAK_CMD+=" $PEAK_FORWARD"
    if [ "$PEAK_HARD_CAP" = "1" ]; then
        # cases-json 会绕过单场景的并发参数；硬顶模式拒绝该额外覆盖。
        case " $PEAK_FORWARD " in
            *" --cases"*) fail "PEAK_HARD_CAP=1 does not accept cases-json overrides" ;;
        esac
        # 放在透传参数之后，同时锁住初始上界和向上扩容，避免同名参数覆盖。
        PEAK_CMD+=" --max-concurrency $PEAK_MAX_CC --auto-expand-max-concurrency $PEAK_MAX_CC"
    fi

    CONTAINER_LOG_TMP=$(mktemp /tmp/container_output_XXXXXX.log)
    docker exec "$CONTAINER_NAME" bash -lc "
        export PYTHONPATH='$WORK_PATH_DETECTED'\${PYTHONPATH:+:\$PYTHONPATH}
        cd '$TOOL_ROOT' || exit 1
        $PEAK_CMD
        PEAK_EXIT=\$?
        if [ \$PEAK_EXIT -ne 0 ]; then
            echo '性能测试失败'
            exit \$PEAK_EXIT
        fi
        # Read only this run_tag's peak report to avoid stale historical reports.
        REPORT_JSON=\$(find '$CONTAINER_OUTPUT_DIR' -maxdepth 2 -type f -path '*/peak_*_$PEAK_RUN_TAG/peak_search_report.json' 2>/dev/null | sort | tail -n 1)
        if [ -n \"\$REPORT_JSON\" ] && [ -f \"\$REPORT_JSON\" ]; then
            echo '===PEAK_REPORT_PATH==='
            echo \"\$REPORT_JSON\"
            echo '===PEAK_REPORT_JSON==='
            cat \"\$REPORT_JSON\"
        fi
    " 2>&1 | tee "$CONTAINER_LOG_TMP"
    PEAK_RC=${PIPESTATUS[0]}
    CONTAINER_OUTPUT=$(cat "$CONTAINER_LOG_TMP")
    echo "$CONTAINER_OUTPUT" >> "$RESULTS_LOG"
    rm -f "$CONTAINER_LOG_TMP"

    if [ $PEAK_RC -ne 0 ]; then
        FAILED_COUNT=$((FAILED_COUNT + 1))
        log "摸高失败: $ARCHIVE_NAME (退出码: $PEAK_RC)"
        append_empty_result "$ARCHIVE_NAME" "" "$PEAK_INPUT_LEN" "$PEAK_OUTPUT_LEN" "" "" "$PEAK_REQUEST_RATE" "$PEAK_REPEAT_RATE" "failed"
        continue
    fi

    # 从 best 行分别提取搜索配置并发和20个源指标；配置并发只用于提示，不加入23列汇总。
    PEAK_METRICS=""
    PEAK_TOTAL_REQ=""
    PEAK_CONFIG_CC=""
    PEAK_REPORT_PATH=""
    if echo "$CONTAINER_OUTPUT" | grep -q '===PEAK_REPORT_PATH==='; then
        PEAK_REPORT_PATH=$(echo "$CONTAINER_OUTPUT" | awk 'BEGIN{c=0} /===PEAK_REPORT_PATH===/{c=1; next} c==1{print; exit}' | tr -d '\r')
    fi
    [ -n "$PEAK_REPORT_PATH" ] && log "peak_report_path=$PEAK_REPORT_PATH"
    if [ -n "$PEAK_REPORT_PATH" ]; then
        PEAK_METRICS_FILE=$(mktemp /tmp/peak_metrics_XXXXXX.txt)
        docker exec -i "$CONTAINER_NAME" python3 - "$PEAK_REQUEST_RATE" "$PEAK_REPORT_PATH" > "$PEAK_METRICS_FILE" 2>>"$RESULTS_LOG" <<'PY' || true
import json
import sys

rr = sys.argv[1]
report_path = sys.argv[2]
with open(report_path, encoding="utf-8", errors="replace") as f:
    report = json.load(f)
best = report.get("best", [])
if best and isinstance(best[0], dict) and best[0].get("passed", "yes") in (True, "yes"):
    row = best[0]
    import math
    for key in ("TTFT avg", "TPOT avg"):
        value = float(row.get(key, "nan"))
        if not math.isfinite(value) or value < 0 or value == 99999:
            raise ValueError(f"invalid peak metric: {key}")
    vals = [
        row.get("cc", ""),
        rr,
        row.get("TTFT avg", ""),
        row.get("TTFT P90", ""),
        row.get("TPOT avg", ""),
        row.get("TPOT SLO_P90", ""),
        row.get("E2E_time", ""),
        row.get("output_throughput", ""),
        row.get("single_output_throughput", row.get("output_throughput", "")),
        row.get("E2E_throughput", ""),
        row.get("single_E2E_throughput", row.get("E2E_throughput", "")),
        row.get("qps", ""),
        row.get("qpm", ""),
        row.get("input_token_throughput", ""),
        row.get("prefill_token_throughput", ""),
    ]
    print(
        str(row.get("current_time", "")) + "|"
        + str(row.get("input_len", "")) + "|"
        + str(row.get("output_len", "")) + "|"
        + str(row.get("total_req", "")) + "|"
        + str(row.get("max_cc", "")) + "|"
        + str(row.get("concurrency", "")) + "|"
        + ",".join(str(v) for v in vals)
    )
PY
        PEAK_RAW=$(tail -n 1 "$PEAK_METRICS_FILE" 2>/dev/null || true)
        if [[ "$PEAK_RAW" == *"|"* ]]; then
            PEAK_CURRENT_TIME="${PEAK_RAW%%|*}"
            PEAK_RAW="${PEAK_RAW#*|}"
            PEAK_INPUT_LEN="${PEAK_RAW%%|*}"
            PEAK_RAW="${PEAK_RAW#*|}"
            PEAK_OUTPUT_LEN="${PEAK_RAW%%|*}"
            PEAK_RAW="${PEAK_RAW#*|}"
            PEAK_TOTAL_REQ="${PEAK_RAW%%|*}"
            PEAK_RAW="${PEAK_RAW#*|}"
            PEAK_MAX_CC="${PEAK_RAW%%|*}"
            PEAK_RAW="${PEAK_RAW#*|}"
            PEAK_CONFIG_CC="${PEAK_RAW%%|*}"
            PEAK_METRICS="${PEAK_RAW#*|}"
        fi
        rm -f "$PEAK_METRICS_FILE"
    fi
    [ -n "$PEAK_METRICS" ] && log "peak_metrics_raw=$PEAK_METRICS"

    if [ -n "$PEAK_METRICS" ]; then
        # PEAK_METRICS: cc,rr,ttft_avg,ttft_p90,tpot_avg,tpot_p90,e2e_time,out_tp,single_out_tp,e2e_tp,single_e2e_tp,qps,qpm,input_tp,prefill_tp (15列)
        # 拼接23列：scenario、5个源元数据、15个源指标、repeat_rate、status。
        append_source_result "$ARCHIVE_NAME" "${PEAK_CURRENT_TIME},${PEAK_INPUT_LEN},${PEAK_OUTPUT_LEN},${PEAK_TOTAL_REQ},${PEAK_MAX_CC},${PEAK_METRICS}" "$PEAK_REPEAT_RATE" "peak" || fail "摸高结果写入失败"
        PASSED_COUNT=$((PASSED_COUNT + 1))
        PEAK_CC=$(echo "$PEAK_METRICS" | cut -d',' -f1)
        log "摸高完成: $ARCHIVE_NAME → 最优并发=${PEAK_CONFIG_CC:-未知}（实测cc=$PEAK_CC）"
    else
        FAILED_COUNT=$((FAILED_COUNT + 1))
        log "警告: 无法解析摸高结果 $ARCHIVE_NAME"
        append_empty_result "$ARCHIVE_NAME" "$PEAK_CURRENT_TIME" "$PEAK_INPUT_LEN" "$PEAK_OUTPUT_LEN" "$PEAK_MAX_CC" "" "$PEAK_REQUEST_RATE" "$PEAK_REPEAT_RATE" "no_result"
    fi

done <<< "$PEAK_COMBOS"

log "固定并发测试完成: 通过=$PASSED_COUNT, 失败=$FAILED_COUNT"
else
    log "无固定并发测试，跳过"
fi

######################## 5. 打印汇总表格 ########################
RESULT_COUNTS=$(python3 - "$RESULTS_CSV" "$TOTAL_TESTS" <<'PY'
import csv, sys
with open(sys.argv[1], encoding="utf-8-sig", newline="") as handle:
    reader = csv.DictReader(handle)
    if len(reader.fieldnames or []) != 23 or len(set(reader.fieldnames or [])) != 23:
        raise ValueError("invalid summary header")
    rows = list(reader)
if any(None in row or any(value is None for value in row.values()) for row in rows):
    raise ValueError("invalid summary row width")
if any(row['status'] not in ('passed', 'peak', 'peak_round', 'failed', 'no_result') for row in rows):
    raise ValueError("unknown summary status")
finals = [row for row in rows if row['status'] != 'peak_round']
if len(finals) != int(sys.argv[2]):
    raise ValueError("final result count differs from scheduled jobs")
print(f"{sum(row['status'] in ('passed', 'peak') for row in finals)}|{sum(row['status'] == 'peak' for row in finals)}|{sum(row['status'] in ('failed', 'no_result') for row in finals)}")
PY
) || fail "汇总结果结构或数量不完整"
IFS='|' read -r PASSED_COUNT PEAK_DONE FAILED_COUNT <<< "$RESULT_COUNTS"

log "生成汇总表格..."

DISPLAY_ENDPOINT=$(python3 - "${API_URL:-}" "$HOST_IP" "$HOST_PORT" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit
if not sys.argv[1]:
    print(f"{sys.argv[2]}:{sys.argv[3]}")
else:
    try:
        url = urlsplit(sys.argv[1])
        if url.scheme not in ('http', 'https') or not url.hostname:
            raise ValueError('invalid URL')
        host = f'[{url.hostname}]' if ':' in url.hostname else url.hostname
        port = f':{url.port}' if url.port is not None else ''
        print(urlunsplit((url.scheme, host + port, url.path, '', '')))
    except ValueError:
        print('API_URL (invalid)')
PY
) || fail "无法生成服务地址摘要"

# CSV 列位置: 1-scenario 7-cc 9-TTFT avg 10-TTFT P90 11-TPOT avg 12-TPOT SLO_P90 13-E2E_time 14-output_tp 16-E2E_tp 18-qps 19-qpm 22-repeat_rate 23-status
{
echo ""
echo "=================================================================================================================================="
echo "  Performance scenario results"
echo "=================================================================================================================================="
printf "  运行时间: %s  |  服务: %s  |  通过: %d  |  摸高: %d  |  失败: %d  |  总计: %d\n" \
    "$RUN_TIMESTAMP" "$DISPLAY_ENDPOINT" "$PASSED_COUNT" "$PEAK_DONE" "$FAILED_COUNT" "$TOTAL_TESTS"
echo "=================================================================================================================================="
printf "  %-34s %6s %6s %10s %10s %10s %10s %10s %14s %14s %10s %10s\n" \
    "scenario" "cc" "status" "TTFT avg" "TTFT P90" "TPOT avg" "TPOT P90" "E2E time" "output tp" "E2E tp" "QPS" "QPM"
printf "  %-34s %6s %6s %10s %10s %10s %10s %10s %14s %14s %10s %10s\n" \
    "" "" "" "(ms)" "(ms)" "(ms)" "(ms)" "(s)" "(tok/s)" "(tok/s)" "" ""
echo "----------------------------------------------------------------------------------------------------------------------------------"
python3 - "$RESULTS_CSV" <<'PY' || exit 1
import csv, math, sys

def display(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "N/A"
    return "N/A" if not math.isfinite(number) or number in (9999, 99999) else f"{number:.2f}"
with open(sys.argv[1], encoding="utf-8-sig", newline="") as handle:
    for row in csv.DictReader(handle):
        status = {'passed': 'PASS', 'peak': 'PEAK', 'peak_round': 'ROUND'}.get(row['status'], 'FAIL')
        values = [display(row[key]) for key in ('TTFT avg', 'TTFT P90', 'TPOT avg', 'TPOT SLO_P90', 'E2E_time', 'output_throughput', 'E2E_throughput', 'qps', 'qpm')]
        print("  %-34s %6s %6s %10s %10s %10s %10s %10s %14s %14s %10s %10s" % (row['scenario'][:34], row['cc'][:6], status, *values))
PY
echo "=================================================================================================================================="
echo ""
} 2>&1 | tee -a "$RESULTS_LOG"
SUMMARY_PIPE_RC=${PIPESTATUS[0]}
[ "$SUMMARY_PIPE_RC" -eq 0 ] || fail "汇总表格输出失败"

ln -sf "$RESULTS_HOST_DIR" "${HOST_OUTPUT_DIR}/scenario_results/latest" 2>/dev/null || true

log "汇总 CSV: $RESULTS_CSV"
log "执行日志: $RESULTS_LOG"
log "全部测试完成"

# Continue individual jobs, but report any failed/missing result to the caller.
[ "$FAILED_COUNT" -eq 0 ]
