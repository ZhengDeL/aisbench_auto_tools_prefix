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
#   MODEL_NAME=glm-5.1-test HOST_IP=192.168.1.16 HOST_PORT=8000 bash run_perf_scenarios.sh

# 容器设置
CONTAINER_NAME="${CONTAINER_NAME:-ais_bench-25}"
EXPECTED_IMAGE="${EXPECTED_IMAGE:-ghcr.io/aisbench/aisbench_benchmark:v3.1-20260912-master-openeuler22.03-py310}"

ADD_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$ADD_DIR/.." && pwd)"

# 宿主机路径（默认取 add 的父目录，即原始工具根目录）
HOST_OUTPUT_DIR="${HOST_OUTPUT_DIR:-${PROJECT_ROOT}/outputs/default}"
MODEL_NAME="${MODEL_NAME:-glm-5.1-test}"
MODEL_HOST_PATH="${MODEL_HOST_PATH:-/mnt/models/DeepSeek-V4-Pro-w4a8-mtp}"
TOOLS_HOST_DIR="${TOOLS_HOST_DIR:-$PROJECT_ROOT}"

# 容器内固定路径
CONTAINER_OUTPUT_DIR="/workspace/aisbench_outputs"
MODEL_CONTAINER_PATH="/workspace/model"
TOOLS_CONTAINER_DIR="/workspace/aisbench_auto_tools_prefix-main"

# 服务地址（必须修改！）
HOST_IP="${HOST_IP:-192.168.1.16}"
HOST_PORT="${HOST_PORT:-8000}"

# API Key 认证（为空则不使用）
# 示例: API_KEY="sk-xxx"
API_KEY="${API_KEY:-}"

# 自定义请求 URL（为空则使用 http://{HOST_IP}:{HOST_PORT}/v1/chat/completions）# 示例: API_URL="http://192.168.1.17:32214/litellm/v1"
API_URL="${API_URL:-}"

# Failed Requests 阈值：本轮失败请求数 > 此值即判为不满足（failed）。默认 0（出现任何失败请求即不满足）
FAILED_REQ_LIMIT="${FAILED_REQ_LIMIT:-0}"
MAX_CONTEXT_LEN="${MAX_CONTEXT_LEN:-512000}"
# NPU 卡数，用于传递给 aisbench_test.py 并计算单卡吞吐
NPU_NUM="${NPU_NUM:-128}"
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

if ! docker ps -a --format '{{.Names}}' | grep -Fx "$CONTAINER_NAME" >/dev/null; then
    ACTION="created"
    REASON="container not found"
fi

if [ "$ACTION" = "reused" ]; then
    CURRENT_IMAGE=$(docker inspect --format '{{.Config.Image}}' "$CONTAINER_NAME" 2>/dev/null || true)
    [ "$CURRENT_IMAGE" != "$EXPECTED_IMAGE" ] && { ACTION="recreated"; REASON="container image mismatch"; }
fi

if [ "$ACTION" = "reused" ]; then
    CURRENT_OUTPUT_SOURCE=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "'"$CONTAINER_OUTPUT_DIR"'"}}{{.Source}}{{end}}{{end}}' "$CONTAINER_NAME" 2>/dev/null || true)
    [ "$CURRENT_OUTPUT_SOURCE" != "$HOST_OUTPUT_DIR" ] && { ACTION="recreated"; REASON="output mount mismatch"; }
fi

if [ "$ACTION" = "reused" ]; then
    docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -qi '^true$' || docker start "$CONTAINER_NAME" >/dev/null 2>&1 || true
fi

if [ "$ACTION" = "reused" ]; then
    if ! docker exec "$CONTAINER_NAME" bash -lc 'test -e '"$MODEL_CONTAINER_PATH"'' >/dev/null 2>&1; then
        [ -e "$MODEL_HOST_PATH" ] && { ACTION="recreated"; REASON="容器内模型路径不存在"; } || fail "宿主机模型路径不存在: $MODEL_HOST_PATH"
    fi
fi

if [ "$ACTION" != "reused" ]; then
    log "重建容器 (原因: $REASON)"
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    DOCKER_RUN_CMD="docker run -d --name '$CONTAINER_NAME' --ipc=host --net=host --privileged --shm-size=1g -v '$HOST_OUTPUT_DIR':'$CONTAINER_OUTPUT_DIR'"
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
    text, count = re.subn(rf\"^{key}\s*=.*$\", replacement, text, flags=re.M)
    if count == 0:
        text += f\"\n{replacement}\n\"
path.write_text(text, encoding=\"utf-8\")
PY
" || fail "写入 config.py 失败"

######################## 3. 解析场景 JSON 并自动展开并发 ########################
log "同步工具目录..."
SCENARIOS_JSON_FILE=$(mktemp /tmp/scenarios_XXXXXX.json)
echo "$SCENARIOS_JSON" > "$SCENARIOS_JSON_FILE"

# 解析结果写入临时文件：COMBOS_FILE=固定并发, PEAK_COMBOS_FILE=摸高
COMBOS_FILE=$(mktemp /tmp/combos_XXXXXX.txt)
PEAK_COMBOS_FILE=$(mktemp /tmp/peak_combos_XXXXXX.txt)
> "$COMBOS_FILE"
> "$PEAK_COMBOS_FILE"

if ! python3 - "$SCENARIOS_JSON_FILE" "$COMBOS_FILE" "$PEAK_COMBOS_FILE" \
  "$PEAK_MIN_CONCURRENCY" "$PEAK_MAX_MULTIPLIER" "$PEAK_DATA_MULTIPLIER" "$PEAK_WAIT_SECONDS" "$PEAK_TTFT_LIMIT" "$PEAK_TPOT_LIMIT" "$MAX_CONTEXT_LEN" <<'PY'
import json, sys, os
scenarios = json.loads(open(sys.argv[1]).read())
combos_file = sys.argv[2]
peak_file = sys.argv[3]
peak_min_cc = int(sys.argv[4])
peak_max_mult = int(sys.argv[5])
peak_data_multiplier = int(sys.argv[6])
peak_wait = int(sys.argv[7])
peak_ttft_default = float(sys.argv[8])
peak_tpot_default = float(sys.argv[9])
max_context_len = int(sys.argv[10])

defaults = {
    'npu_num': os.environ['NPU_NUM'], 'test_type': 'stream',
    'repeat': '1', 'dataset_type': 'normal', 'prefix_num': '1',
    'repeat_rate': '0', 'seed': '1', 'dp': '1'
}
DATA_NUM_MULTIPLIER = int(os.environ['DATA_NUM_MULTIPLIER'])
if DATA_NUM_MULTIPLIER <= 0:
    raise ValueError("DATA_NUM_MULTIPLIER must be a positive integer")

def format_cli_arg(key, value):
    if isinstance(value, bool):
        return f'--{key}' if value else ''
    return f'--{key} {value}'

combo_lines = []
peak_lines = []

for sc in scenarios:
    name = sc['name']
    input_len = sc.get('input_len', '512')
    output_len = sc.get('output_len', '128')
    if max_context_len > 0 and int(input_len) + int(output_len) > max_context_len:
        raise ValueError(f"scenario {name} exceeds max context: input_len({input_len}) + output_len({output_len}) > {max_context_len}")
    request_rate = sc.get('request_rate', '0')
    concurrency_values = sc.get('concurrency', [1])
    special_keys = {'name', 'input_len', 'output_len', 'concurrency', 'request_rate',
                    'ttft_limit', 'tpot_limit', 'peak_min_cc', 'peak_max_cc',
                    'peak_data_multiplier', 'peak_wait'}
    extra = {k: v for k, v in sc.items() if k not in special_keys}

    # Split fixed concurrency values from peak-search marker.
    fixed_concs = [c for c in concurrency_values if c != 'max']
    has_peak = 'max' in concurrency_values

    # 固定并发测试
    for conc in fixed_concs:
        if 'data_num' in extra:
            data_num = str(extra['data_num'])
        else:
            data_num = str(int(conc) * DATA_NUM_MULTIPLIER)
        params = {'input_len': input_len, 'output_len': output_len,
                  'concurrency': str(conc), 'request_rate': request_rate, 'data_num': data_num}
        for k, v in defaults.items():
            if k not in extra:
                params[k] = v
        params.update(extra)
        # 移除摸高专用参数，避免传给 aisbench_test.py
        for pk in ['ttft_limit', 'tpot_limit', 'peak_min_cc', 'peak_max_cc', 'peak_data_multiplier', 'peak_wait']:
            params.pop(pk, None)
        cmd_parts = [item for item in (format_cli_arg(k, v) for k, v in params.items()) if item]
        cmd_str = ' '.join(cmd_parts)
        archive_name = f'{name}_c{conc}'
        combo_lines.append(f'{archive_name}|{cmd_str}')

    # 摸高搜索
    if has_peak:
        ttft = sc.get('ttft_limit', peak_ttft_default)
        tpot = sc.get('tpot_limit', peak_tpot_default)
        # Peak search range: min=peak_min_cc, max=first fixed concurrency * peak_max_mult, unless overridden.
        if fixed_concs:
            max_cc = sc.get('peak_max_cc', int(fixed_concs[0]) * peak_max_mult)
        else:
            max_cc = sc.get('peak_max_cc', peak_min_cc * peak_max_mult)
        min_cc = sc.get('peak_min_cc', peak_min_cc)
        step = sc.get('peak_data_multiplier', peak_data_multiplier)
        wait = sc.get('peak_wait', peak_wait)
        # Forward extra args to aisbench_perf_matrix.py.
        forward_parts = []
        for k, v in extra.items():
            item = format_cli_arg(k, v)
            if item:
                forward_parts.append(item)
        forward_str = ' '.join(forward_parts)
        peak_lines.append(f'{name}_peak|{input_len}|{output_len}|{min_cc}|{max_cc}|{step}|{request_rate}|{ttft}|{tpot}|{wait}|{forward_str}')

with open(combos_file, 'w') as f:
    f.write('\n'.join(combo_lines))
with open(peak_file, 'w') as f:
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
            --since-ts '$ARCHIVE_SINCE_TS'
        ARCHIVE_EXIT=\$?
        if [ \$ARCHIVE_EXIT -ne 0 ]; then
            echo '性能测试失败'
            exit \$ARCHIVE_EXIT
        fi
        # 按列名投影最后一行，兼容原仓新增字段；保持汇总所需的20列顺序。
        RESULT_CSV_FILE='$TOOL_ROOT/aisbench_result.csv'
        if [ -f \"\$RESULT_CSV_FILE\" ]; then
            NORMALIZED_RESULT=\$(python3 add/aisbench_result_adapter.py \"\$RESULT_CSV_FILE\") || exit \$?
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

    # 追加到执行日志
    echo "$CONTAINER_OUTPUT" >> "$RESULTS_LOG"
    rm -f "$CONTAINER_LOG_TMP"

    if [ $PERF_PIPE_RC -ne 0 ]; then
        FAILED_COUNT=$((FAILED_COUNT + 1))
        log "[$TEST_INDEX/$TOTAL_TESTS] 失败: $ARCHIVE_NAME (退出码: $PERF_PIPE_RC)"
        FAIL_ROW="${ARCHIVE_NAME},,${SCENARIO_INPUT_LEN},${SCENARIO_OUTPUT_LEN},,,${SCENARIO_CONCURRENCY},${SCENARIO_REQUEST_RATE},,,,,,,,,,,,,,,${SCENARIO_REPEAT_RATE},failed"
        echo "$FAIL_ROW" >> "$RESULTS_CSV"
        continue
    fi

    # Extract the final aisbench result CSV row from container output.
    RESULT_ROW=""
    if echo "$CONTAINER_OUTPUT" | grep -q '===AISBENCH_RESULT==='; then
        RESULT_ROW=$(echo "$CONTAINER_OUTPUT" | awk 'BEGIN{c=0} /===AISBENCH_RESULT===/{c=1; next} c==1{c=2; next} c==2{print; exit}')
    fi

    # 检测请求是否全部失败（指标全为 99999）    # RESULT_ROW 字段: current_time(1),input_len(2),output_len(3),total_req(4),max_cc(5),cc(6),rr(7),TTFT avg(8),...
    REQUEST_FAILED="no"
    if [ -n "$RESULT_ROW" ]; then
        TTFT_VAL=$(echo "$RESULT_ROW" | awk -F',' '{print $8}')
        if [ "$TTFT_VAL" = "99999" ] || [ "$TTFT_VAL" = "99999.0" ]; then
            REQUEST_FAILED="yes"
        fi
    fi

    # 从 aisbench 输出中提取 Failed Requests 数（取最后一个匹配，避免取到中间状态）
    FAILED_REQ_COUNT=$(echo "$CONTAINER_OUTPUT" | awk '/Failed Requests/{gsub(/[^0-9]/,"",$0); val=$0} END{print val}')
    SUCCESS_REQ_COUNT=$(echo "$CONTAINER_OUTPUT" | awk '/Success Requests/{gsub(/[^0-9]/,"",$0); val=$0} END{print val}')
    if [ -n "$FAILED_REQ_COUNT" ] && [ "$FAILED_REQ_COUNT" -gt "$FAILED_REQ_LIMIT" ] 2>/dev/null; then
        REQUEST_FAILED="yes"
    fi

    if [ "$REQUEST_FAILED" = "yes" ]; then
        FAILED_COUNT=$((FAILED_COUNT + 1))
        log "[$TEST_INDEX/$TOTAL_TESTS] 请求失败: $ARCHIVE_NAME (成功=${SUCCESS_REQ_COUNT:-0} 失败=${FAILED_REQ_COUNT:-?})"
        if [ -n "$RESULT_ROW" ]; then
            echo "${ARCHIVE_NAME},${RESULT_ROW},${SCENARIO_REPEAT_RATE},failed" >> "$RESULTS_CSV"
        else
            FAIL_ROW="${ARCHIVE_NAME},,${SCENARIO_INPUT_LEN},${SCENARIO_OUTPUT_LEN},,,${SCENARIO_CONCURRENCY},${SCENARIO_REQUEST_RATE},,,,,,,,,,,,,,,${SCENARIO_REPEAT_RATE},failed"
            echo "$FAIL_ROW" >> "$RESULTS_CSV"
        fi
        continue
    fi

    PASSED_COUNT=$((PASSED_COUNT + 1))
    log "[$TEST_INDEX/$TOTAL_TESTS] 开始测试: $ARCHIVE_NAME"

    if [ -n "$RESULT_ROW" ]; then
        echo "${ARCHIVE_NAME},${RESULT_ROW},${SCENARIO_REPEAT_RATE},passed" >> "$RESULTS_CSV"
    else
        log "警告: 无法提取 $ARCHIVE_NAME 的结果行"
        FAIL_ROW="${ARCHIVE_NAME},,${SCENARIO_INPUT_LEN},${SCENARIO_OUTPUT_LEN},,,${SCENARIO_CONCURRENCY},${SCENARIO_REQUEST_RATE},,,,,,,,,,,,,,,${SCENARIO_REPEAT_RATE},no_result"
        echo "$FAIL_ROW" >> "$RESULTS_CSV"
    fi

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
        echo "${ARCHIVE_NAME},,${PEAK_INPUT_LEN},${PEAK_OUTPUT_LEN},,,,${PEAK_REQUEST_RATE},,,,,,,,,,,,,,,${PEAK_REPEAT_RATE},failed" >> "$RESULTS_CSV"
        continue
    fi

    PASSED_COUNT=$((PASSED_COUNT + 1))

    # 解析摸高结果：从 peak_search_report.json 提取 best 行    # CSV_HEADER 22列: scenario(1),current_time(2),input_len(3),output_len(4),total_req(5),max_cc(6),
    #   cc(7),rr(8),TTFT avg(9),TTFT P90(10),TPOT avg(11),TPOT SLO_P90(12),E2E_time(13),
    #   output_tp(14),single_output_tp(15),E2E_tp(16),single_E2E_tp(17),qps(18),qpm(19),input_tp(20),prefill_tp(21),status(22)
    PEAK_METRICS=""
    PEAK_TOTAL_REQ=""
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
if best:
    row = best[0]
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
            PEAK_METRICS="${PEAK_RAW#*|}"
        fi
        rm -f "$PEAK_METRICS_FILE"
    fi
    [ -n "$PEAK_METRICS" ] && log "peak_metrics_raw=$PEAK_METRICS"

    if [ -n "$PEAK_METRICS" ]; then
        # PEAK_METRICS: cc,rr,ttft_avg,ttft_p90,tpot_avg,tpot_p90,e2e_time,out_tp,single_out_tp,e2e_tp,single_e2e_tp,qps,qpm,input_tp,prefill_tp (15列)
        # 拼接: scenario(1),current_time(2=空),input(3),output(4),total_req(5=空),max_cc(6=空),[15列](7~21),status(22)
        echo "${ARCHIVE_NAME},${PEAK_CURRENT_TIME},${PEAK_INPUT_LEN},${PEAK_OUTPUT_LEN},${PEAK_TOTAL_REQ},${PEAK_MAX_CC},${PEAK_METRICS},${PEAK_REPEAT_RATE},peak" >> "$RESULTS_CSV"
        PEAK_CC=$(echo "$PEAK_METRICS" | cut -d',' -f1)
        log "摸高完成: $ARCHIVE_NAME → 最优并发=$PEAK_CC"
    else
        log "警告: 无法解析摸高结果 $ARCHIVE_NAME"
        echo "${ARCHIVE_NAME},${PEAK_CURRENT_TIME},${PEAK_INPUT_LEN},${PEAK_OUTPUT_LEN},,${PEAK_MAX_CC},,,,,,,,,,,,,,,,,,,${PEAK_REPEAT_RATE},no_result" >> "$RESULTS_CSV"
    fi

done <<< "$PEAK_COMBOS"

log "固定并发测试完成: 通过=$PASSED_COUNT, 失败=$FAILED_COUNT"
else
    log "无固定并发测试，跳过"
fi

######################## 5. 打印汇总表格 ########################
log "生成汇总表格..."

# CSV 列位置: 1-scenario 7-cc 9-TTFT avg 10-TTFT P90 11-TPOT avg 12-TPOT SLO_P90 13-E2E_time 14-output_tp 16-E2E_tp 18-qps 19-qpm 22-repeat_rate 23-status
{
echo ""
echo "=================================================================================================================================="
echo "  Performance scenario results"
echo "=================================================================================================================================="
PEAK_DONE=$(awk -F',' 'NR>1 && $NF=="peak"{c++} END{print c+0}' "$RESULTS_CSV")
printf "  运行时间: %s  |  服务: %s:%s  |  通过: %d  |  摸高: %d  |  失败: %d  |  总计: %d\n" \
    "$RUN_TIMESTAMP" "$HOST_IP" "$HOST_PORT" "$PASSED_COUNT" "$PEAK_DONE" "$FAILED_COUNT" "$TOTAL_TESTS"
echo "=================================================================================================================================="
printf "  %-34s %6s %6s %10s %10s %10s %10s %10s %14s %14s %10s %10s\n" \
    "scenario" "cc" "status" "TTFT avg" "TTFT P90" "TPOT avg" "TPOT P90" "E2E time" "output tp" "E2E tp" "QPS" "QPM"
printf "  %-34s %6s %6s %10s %10s %10s %10s %10s %14s %14s %10s %10s\n" \
    "" "" "" "(ms)" "(ms)" "(ms)" "(ms)" "(s)" "(tok/s)" "(tok/s)" "" ""
echo "----------------------------------------------------------------------------------------------------------------------------------"
awk -F',' '
function fv(v) {
    if (v == "") return "N/A"
    n = v + 0
    if (n == 99999 || n == 9999) return "N/A"
    return sprintf("%.2f", n)
}
NR>1 && NF>=23 {
    status = $23
    gsub(/\r/, "", status)
    if (status == "passed") st = "PASS"
    else if (status == "peak") st = "PEAK"
    else if (status == "peak_round") st = "ROUND"
    else st = "FAIL"
    printf "  %-34s %6s %6s %10s %10s %10s %10s %10s %14s %14s %10s %10s\n",
        substr($1,1,34), substr($7,1,6), st,
        fv($9), fv($10), fv($11), fv($12), fv($13),
        fv($14), fv($16), fv($18), fv($19)
}' "$RESULTS_CSV"
echo "=================================================================================================================================="
echo ""
} 2>&1 | tee -a "$RESULTS_LOG"

ln -sf "$RESULTS_HOST_DIR" "${HOST_OUTPUT_DIR}/scenario_results/latest" 2>/dev/null || true

log "汇总 CSV: $RESULTS_CSV"
log "执行日志: $RESULTS_LOG"
log "全部测试完成"
