# add：多场景性能测试与并发摸高

`run_perf_scenarios.sh` 负责管理 AISBench 压测容器、生成运行配置、执行固定并发和摸高搜索，并汇总、归档结果。实际请求仍由项目根目录的 `aisbench_test.py` 发出。

当前版本的基础文件使用[原仓提交 ef2b6d8](https://github.com/rayn-zzz/aisbench_auto_tools_prefix/commit/ef2b6d8653d57487b622b0dc9db458f8a1e8dec6)，功能适配放在 `add/`。根目录只将 IP 示例改为 `192.168.1.x`，其余基础逻辑沿用原仓。底层参数与数据集生成方式见[上层 README](../README.md)。

## 1. 运行条件

- Linux、Bash、Python 3，以及 `tar`、`awk`、`sed`、`grep`、`find`、`base64` 等常用命令。镜像内使用 Python 3.10。
- Docker 已启动，当前用户可以执行 `docker` 命令。
- 可以使用镜像 `ghcr.io/aisbench/aisbench_benchmark:v3.1-20260912-master-openeuler22.03-py310`；镜像架构须与压测主机匹配。
- 宿主机能读取模型权重目录，镜像内 AISBench、transformers 能加载对应 tokenizer。
- 被测推理服务可从压测容器访问；需要缓存指标时，各 `POD_INFO` 端点的 `/metrics` 也应可访问。

脚本使用宿主机网络创建带管理标签的专用压测容器。复用前核对管理标签、镜像、模型权重和输出挂载；仅已确认属于本工具的容器可按固定容器 ID 重建。遇到同名未知镜像或无标签容器会报错停止。升级旧版无标签工具容器时，可以指定新的 `CONTAINER_NAME`，由脚本创建；它不会部署或自动删除模型服务。

## 2. 快速开始

首次获取仓库：

```bash
git clone --branch main-add-20261015 https://github.com/ZhengDeL/aisbench_auto_tools_prefix.git
cd aisbench_auto_tools_prefix/add
```

已有仓库时，进入其 `add/` 目录。先编辑配置，再加载并运行：

```bash
vi env.env
source ./env.env
bash run_perf_scenarios.sh
```

**脚本不会自动加载 `env.env`。** 仓库中的文件是公开示例，使用前填写模型名、权重路径、服务地址、NPU 数和指标端点。真实配置可另存为被 Git 忽略的 `env.local.env`，用 `source ./env.local.env` 加载。

| 项目 | 当前 `env.env` 的值 |
|---|---|
| `MODEL_NAME` | `your-model` |
| `MODEL_HOST_PATH` | `/mnt/models/your-model` |
| `HOST_IP` / `HOST_PORT` | `192.168.1.10` / `8000` |
| `NPU_NUM` | `1`，按实际统计口径修改 |
| `MAX_CONTEXT_LEN` | `133120` |
| `POD_INFO` | 空；需要前缀指标时填写实际 prefill metrics 端点 |
| `PEAK_HARD_CAP` | `1`，沿用原增补仓加载 env 后到顶即停的策略 |

`NPU_NUM` 是吞吐归一化的分母：`single_output_throughput = output_throughput / NPU_NUM`，`single_E2E_throughput = E2E_throughput / NPU_NUM`；全局吞吐保持整轮统计值。用户必须按报告单位填写参与测试的设备总数：按物理卡报告就填物理卡数，按 NPU 芯片报告就填芯片数，并在报告中注明口径。A3 一张物理卡包含 2 颗 NPU 芯片，两种口径对应的归一化吞吐相差 2 倍。默认 `1` 是示例值，脚本不会自动探测硬件。`POD_INFO` 的端点数量也不会自动设置 `dp`；当前场景没有显式设置 `dp`，底层默认是 `1`。

加载配置后可以继续覆盖变量，例如：

```bash
source ./env.env
DATA_NUM_MULTIPLIER=7 CONTAINER_NAME=ais_bench_check bash run_perf_scenarios.sh
```

`source env.env` 会覆盖该文件中定义的同名变量，因此临时覆盖应放在加载之后。

## 3. 先用小场景验证流程

当前内置场景会执行多轮 64K 测试和摸高搜索。只想检查配置与流程时，可整体替换为一个小场景：

```bash
source ./env.env
export SCENARIOS_JSON_OVERRIDE='[
  {
    "name": "smoke",
    "input_len": "512",
    "output_len": "32",
    "concurrency": [1],
    "request_rate": "0",
    "data_num": "2"
  }
]'
bash run_perf_scenarios.sh
unset SCENARIOS_JSON_OVERRIDE
```

`SCENARIOS_JSON_OVERRIDE` 替换整个场景数组，不与内置场景合并。上例没有 `"max"`，不会摸高，也没有前缀预热。出现结果文件和 `passed` 只能确认该轮流程检查通过，回答内容仍需另外核验。

## 4. 环境变量与配置优先级

下表是**未设置对应环境变量时，脚本自身使用的回退值**。模型、路径和地址均为公开示例；按第 2 节加载配置后，以环境值为准。

| 环境变量 | 脚本回退值 | 用途 |
|---|---|---|
| `MODEL_NAME` | `your-model` | 服务识别的模型名称 |
| `MODEL_HOST_PATH` | `/mnt/models/your-model` | 宿主机权重目录，用于 tokenizer |
| `HOST_IP` | `192.168.1.10` | 推理服务地址 |
| `HOST_PORT` | `8000` | 推理服务端口 |
| `NPU_NUM` | `1` | `single_*` 吞吐的分母；按报告单位填写物理卡数或 NPU 芯片数 |
| `POD_INFO` | 空 | 逗号分隔的 metrics 地址；生成配置时转为列表 |
| `API_KEY` | 空 | 鉴权 Key |
| `API_URL` | 空 | 自定义服务 URL，映射到容器内 `config.py` 的 `URL` |
| `FAILED_REQ_LIMIT` | `0` | 解析到的失败请求数超过此值时判失败 |
| `MAX_CONTEXT_LEN` | `512000` | 场景的输入加输出长度上限；加载当前 env 后为 `133120` |
| `CONTAINER_NAME` | `ais_bench-example` | 压测容器名 |
| `EXPECTED_IMAGE` | `ghcr.io/aisbench/aisbench_benchmark:v3.1-20260912-master-openeuler22.03-py310` | 压测镜像 |
| `HOST_OUTPUT_DIR` | `${PROJECT_ROOT}/outputs/default` | 宿主机结果根目录 |
| `TOOLS_HOST_DIR` | `$PROJECT_ROOT` | 同步到容器的工具目录 |
| `DATA_NUM_MULTIPLIER` | `10` | 固定并发主测试请求数倍率，须为正整数 |
| `PEAK_MIN_CONCURRENCY` | `1` | 场景未指定时的摸高初始下界 |
| `PEAK_MAX_MULTIPLIER` | `20` | 未指定 `peak_max_cc` 时，首个固定并发 × 此值；无固定档时为全局 `PEAK_MIN_CONCURRENCY` × 此值 |
| `PEAK_DATA_MULTIPLIER` | `4` | 场景未指定时的摸高请求数倍率 |
| `PEAK_WAIT_SECONDS` | `10` | 场景未指定时的摸高轮间等待秒数 |
| `PEAK_TTFT_LIMIT` | `5` | 场景未指定时的 TTFT 阈值，单位秒 |
| `PEAK_TPOT_LIMIT` | `50` | 场景未指定时的 TPOT 阈值，单位毫秒 |
| `PEAK_HARD_CAP` | `0` | `0` 保留自动扩容；`1` 以各场景的 `peak_max_cc` 为硬上限 |
| `SCENARIOS_JSON_OVERRIDE` | 未设置，使用内置数组 | 整体替换场景配置 |

`PROJECT_ROOT` 是脚本定位到的项目根目录。容器内固定路径为 `/workspace/model`、`/workspace/aisbench_dataset`、`/workspace/aisbench_outputs` 和 `/workspace/aisbench_auto_tools_prefix-main`，它们不是上述环境变量的宿主机路径。

配置按下面的规则生效：

1. 加载 `env.env` 后，环境变量覆盖 shell 的回退值；后续的 `export` 或 `VAR=value bash ...` 可以再次覆盖。
2. 场景显式填写的 `ttft_limit`、`tpot_limit`、`peak_min_cc`、`peak_max_cc`、`peak_data_multiplier`、`peak_wait` 优先于相应的 `PEAK_*` 环境默认。
3. shell 把最终场景值作为命令行参数传给摸高引擎，覆盖 `aisbench_perf_matrix_config.py` 中对应的默认值。场景填写 `npu_num` 时，也会优先于全局 `NPU_NUM`。

例如，当前场景已写入 TTFT=15 秒、TPOT=60 毫秒，仅设置 `PEAK_TTFT_LIMIT=5`、`PEAK_TPOT_LIMIT=50` 不会改变它们。请修改场景字段或用 `SCENARIOS_JSON_OVERRIDE` 替换。

### API 与缓存指标

`API_URL` 为空时，由 AISBench 根据 `HOST_IP/HOST_PORT` 构造接口地址。它的接线是 `API_URL` → 容器 `config.URL` → 模型配置 `url`。指定镜像的 chat 客户端使用 `urljoin(base_url, "v1/chat/completions")`：例如 `API_URL=http://192.168.1.10:8000/` 最终请求 `/v1/chat/completions`，带网关前缀时也应按此规则保留末尾斜杠并核验最终路径。需要鉴权时，在加载 env 后设置 `API_KEY`。

终端摘要在指定 `API_URL` 时显示它的scheme、host、port和path，省略userinfo、query和fragment；未指定时显示 `HOST_IP:HOST_PORT`。摘要不改变请求接线。

`POD_INFO` 写入容器配置，覆盖基础 `config.py` 原来的同名值。空值生成 `[]`，底层回退到 `HOST_IP:HOST_PORT`。只有 `dataset_type=prefix_cache` 且开启 `prefix_test` 时，这条测试链路才查询缓存指标。PD 分离时应按要观察的角色填写端点；公开 env 不包含任何真实 P/D 地址；例如 `POD_INFO="192.168.1.10:9081,192.168.1.11:9081"`。

## 5. 当前内置场景与请求数量

| 场景名 | 输入 / 输出 token | 固定并发 | 摸高初始范围 | TTFT / TPOT 阈值 |
|---|---|---|---|---|
| `0_ai_coding_64k256` | `65536` / `256` | `1, 10, 20, 50, 100` | `1～200` | `15 秒` / `60 毫秒` |
| `0_ai_coding_64k1k` | `65536` / `1024` | `1, 10, 20, 50, 100` | `1～200` | `15 秒` / `60 毫秒` |

两组场景都使用 `prefix_cache`、`prefix_num=1`、`repeat_rate=0.9`、`prefix_test=true`、`request_rate=0`，摸高倍率 `4`、轮间等待 `10` 秒。`request_rate=0` 表示不限请求发送速率。

**固定并发**：场景写了 `data_num` 就使用该值；否则为并发数 × `DATA_NUM_MULTIPLIER`。默认倍率为 `10`，所以每个场景的五档主测试分别发出 `10、100、200、500、1000` 条请求。

**摸高**：每档主测试请求数为该档并发 × `peak_data_multiplier`，当前为 `4`。它独立于固定倍率；场景额外写入的 `data_num` 不会覆盖摸高引擎计算的请求数。

因此默认流程包含 10 轮固定测试和 2 组摸高搜索，摸高探测轮数不固定。开启 `prefix_test` 的调用还会先执行前缀预热；上述请求数只指主测试。

场景中的数字并发表示固定测试，`"max"` 表示摸高。例如只保留 `"concurrency": [1, 10]` 就不摸高。额外的底层参数可写在场景里，如 `dataset_type`、`prefix_num`、`repeat_rate`、`prefix_test`、`seed`、`dp`、`test_type`；布尔开关使用 JSON 的 `true/false`。

固定测试支持 `test_type: "text"` 非流式模式：汇总中的四个TTFT/TPOT字段留空、终端显示N/A，保留E2E、请求计数和吞吐。非流式只有完整响应时间，即使原生误显示正TTFT，也不能用作首token延迟。流式实际只生成1个token时没有token间隔，两项TPOT同样留空，即使原生写出正值也不展示为TPOT；不伪造0。这些不适用字段仍先检查原值为空或有限非负，拒绝垃圾、NaN、inf和负数。模式写入归档manifest供后续验收，旧归档未记录模式时按stream严格读取。

## 6. 摸高搜索和通过条件

摸高默认比较 `TTFT avg` 和 `TPOT avg`，不是 P90。列名可在 `aisbench_perf_matrix_config.py` 的 `TTFT_COLUMN`、`TPOT_COLUMN` 中修改。

摸高要求 `test_type: "stream"` 且配置输出长度至少2；shell会在初始化容器前拒绝不符合的场景，独立matrix也会在发送首个请求前检查全部case的最终参数。即使关闭SLO，也不能用非流式或无TPOT的单token响应判定通过。

- CSV 中 TTFT 是毫秒，比较时除以 1000，再与秒单位的 `ttft_limit` 比较；TPOT 的数值和阈值都是毫秒。
- 超过阈值才不满足，等于阈值可通过。阈值 ≤0 时关闭该项 SLO 比较，但仍拒绝缺失、非数字、NaN、inf、负数和 `99999` 等无效指标。
- 进程执行失败、结果异常、缺少可靠的 `Failed Requests` 计数，或该计数超过 `FAILED_REQ_LIMIT`，都会判为不满足；已知成功请求数为零也会被拒绝。

搜索先测初始区间中点。中点通过时继续探测初始上界，上界通过后通常继续翻倍；失败时向下寻找通过档位，必要时可探到并发 1，再在通过/失败边界间做整数二分。同一并发在一次搜索中使用已缓存的测量结果。

**是否超过 `peak_max_cc` 继续摸高，由 `PEAK_HARD_CAP` 控制。** 脚本未收到该变量时回退为 `0`，保留自动扩容：初始上界 200 通过后可以继续探到 400、800 等。当前 `env.env` 沿用原增补仓的 `export PEAK_HARD_CAP=1`：加载后以每个场景的 `peak_max_cc` 为硬上限，到顶通过即停止；到顶失败时仍会向下搜索满足 SLO 的并发。

```bash
source ./env.env
PEAK_HARD_CAP=1 bash run_perf_scenarios.sh
```

底层引擎还支持 `aisbench_perf_matrix_config.py` 中的 `AUTO_EXPAND_MAX_CONCURRENCY` 和命令行 `--auto-expand-max-concurrency`。数值 `0` 表示不限制扩容，正数限制后续向上探测；它本身不裁剪已指定的初始区间。shell 的 `PEAK_HARD_CAP=1` 会根据当前场景上界设置该参数。

硬顶模式会在额外透传参数之后锁定初始上界和扩容上限，并拒绝额外的 `cases-json` 覆盖，以保证单个场景的上界有效。不开启硬顶时，保留底层引擎的数值上限及额外参数行为。

二分和扩容依赖通过状态随并发大致单调。服务波动或非单调结果下，最终best仅是本次已测通过档位中的最高并发，不保证所有未测档位满足，更不证明真正容量极限或经过多轮重复验证的稳定容量。

本镜像的原生 CSV 还提供单请求 `PrefillTokenThroughput` 统计均值，它与整轮的全局吞吐口径不同。上游未提取到全局 prefill 吞吐时会写入 `9999` 哨兵，add 汇总将其显示为空，表示该指标不可用；原始 CSV 保留用于核查。输出吞吐、输入吞吐、总 token 吞吐采用原生 JSON 的整轮指标。

## 7. 结果目录、指标和状态

默认结果根目录为项目下的 `outputs/default`，可用 `HOST_OUTPUT_DIR` 覆盖：

```text
<HOST_OUTPUT_DIR>/
├─ scenario_results/<本轮时间戳>/
│  ├─ all_results.csv
│  └─ execution.log
├─ scenario_results/latest -> <本轮结果目录>
├─ performance_<场景>_c<并发>_<归档时间>/
│  ├─ aisbench_result.csv
│  ├─ aisbench.log
│  ├─ captured_outputs/
│  └─ archive_manifest.json
└─ peak_<场景>_peak_<搜索时间>_<本轮标记>/
   ├─ peak_search_report.json
   ├─ peak_search_report.csv
   ├─ peak_search_report_all.csv
   ├─ peak_search_report.md
   ├─ peak_search.log
   ├─ archive_manifest.json
   └─ <场景>_peak/
      ├─ search_trace.csv / search_trace.json / search_trace.md
      ├─ best_result.csv
      ├─ case_summary.json
      └─ run_001_cc_<并发>/
         ├─ command.txt
         ├─ request_meta.json
         ├─ process.log
         ├─ aisbench_result.csv
         └─ evaluation.json
```

`captured_outputs/` 仅在存在符合条件的本轮新产物时生成。`execution.log` 保存测试容器输出和最后的汇总表，不包含所有宿主机初始化及进度信息。固定测试在 benchmark 非零退出时可能尚未归档；不能假定每个失败任务都已有完整 `performance_*` 目录。归档清单记录文件列表，不提供内容哈希校验。

源 CSV 兼容旧版 20 列和当前上游 24 列，按列名投影；原始 CSV 保留在归档中，场景汇总固定为以下 **23 列**：

```text
scenario,current_time,input_len,output_len,total_req,max_cc,cc,rr,TTFT avg,TTFT P90,TPOT avg,TPOT SLO_P90,E2E_time,output_throughput,single_output_throughput,E2E_throughput,single_E2E_throughput,qps,qpm,input_token_throughput,prefill_token_throughput,repeat_rate,status
```

| 字段 | 含义与单位 |
|---|---|
| `scenario` / `current_time` | 测试任务名 / 结果采集时间 |
| `input_len` / `output_len` | 上游记录的输入 / 输出长度统计，token；以实际记录为准 |
| `total_req` / `rr` | 主测试总请求数 / 配置的请求发送速率 |
| `TTFT avg` / `TTFT P90` | 平均 / P90 首字延迟，**毫秒** |
| `TPOT avg` / `TPOT SLO_P90` | 平均 / P90 每输出 token 时延，毫秒 |
| `E2E_time` | 整轮 Benchmark Duration，秒；不是单请求 E2EL |
| `output_throughput` / `E2E_throughput` | 输出 / 总 token 吞吐，token/s |
| `single_output_throughput` / `single_E2E_throughput` | 上述全局吞吐除以 `NPU_NUM`；按所填物理卡数或芯片数解释，并注明报告单位 |
| `qps` / `qpm` | 请求吞吐，每秒 / 每分钟 |
| `input_token_throughput` / `prefill_token_throughput` | 输入 / prefill 吞吐，token/s |
| `cc` / `max_cc` | 上游测量的 Concurrency / Max Concurrency；不保证 `cc` 等于配置档位，固定任务名 `_cN` 中的 N 表示指定并发 |
| `repeat_rate` | 场景配置的前缀重复率，**不是实测缓存命中率** |

新版源 CSV 的 `model_name`、`E2EL avg/P90` 没有加入这张 23 列汇总；查看原始 CSV 可获得这些字段。

| `status` | 实际含义 |
|---|---|
| `passed` | 固定测试执行、按模式适用的结果字段检查通过，成功请求数非零且失败数未超限；text的四延迟及单token缺失TPOT可为空；**不检查 TTFT/TPOT SLO** |
| `peak_round` | 一次摸高探测的实时记录，可能满足，也可能不满足；是否通过及原因见 `search_trace.*`、`evaluation.json` |
| `peak` | 摸高搜索成功完成后选出的最终 best 行 |
| `failed` | 执行、归档或结果校验失败，失败请求数超限，或摸高未找到满足档位等 |
| `no_result` | 流程返回成功但汇总未提取到结果，属于异常 |

场景和已知参数先校验，非法值在 Docker 初始化和发请求前报错。单个测试失败记录状态后继续后续任务；只要出现 `failed/no_result`，整批最终返回非零退出码。全部任务成功才返回 `0`；容器准备或文件同步失败也会立即退出。最终打印“全部测试完成”不代表所有行通过，应同时检查退出码、CSV 和搜索明细。

计数从标签后的值解析，支持时间戳日志和表格格式，日期不会被拼入失败数。CSV 的写入与读取使用标准 CSV 规则，场景名可以包含逗号和双引号；场景名不接受单引号、竖线、换行或空值。非法开关、非整数并发/请求数、负倍率、非有限 SLO，以及冲突的归档名称会明确报错。

## 8. 检查结果

在 `add/` 目录查看本轮汇总：

```bash
OUTPUT_ROOT="${HOST_OUTPUT_DIR:-$(cd .. && pwd)/outputs/default}"
cat "$OUTPUT_ROOT/scenario_results/latest/all_results.csv"
tail -n 30 "$OUTPUT_ROOT/scenario_results/latest/execution.log"
```

离线检查插件本身，不调用 Docker、不请求模型：

```bash
python3 validate_addon.py
python3 -m unittest discover -s . -p 'test_*.py'
```

它使用合成样本检查代码接线、配置、CSV 和搜索结果解析，生成本地 `VALIDATION_REPORT.md`；有 Bash 时还做 shell 语法检查。通过不代表真实镜像或服务已验证。

真实运行后再检查产物：

```bash
OUTPUT_ROOT="${HOST_OUTPUT_DIR:-$(cd .. && pwd)/outputs/default}"
RESULT_DIR="$OUTPUT_ROOT/scenario_results/latest"
python3 validate_e2e_outputs.py \
  --result-dir "$RESULT_DIR" --host-output-dir "$OUTPUT_ROOT"
python3 validate_result_integrity.py \
  --result-dir "$RESULT_DIR" --host-output-dir "$OUTPUT_ROOT"
```

两者可加 `--report-file /已有目录/report.md` 保存报告；检查失败返回非零。有 `failed/no_result` 的汇总会被拒绝。`validate_e2e_outputs.py` 的 `--expected-statuses` 要与全部行的状态和顺序完全一致，包括所有 `peak_round`，一般运行可以省略。

端到端产物检查只检查最后一个摸高报告的首个 best；字段追溯检查比对固定行和最终 peak 行，不逐项验收 `peak_round`。这些检查不替代回答内容、物理缓存命中和长期稳定性验证。

## 9. 常见问题

| 现象 | 检查方法 |
|---|---|
| 使用了错误模型、地址或 NPU 数 | 确认已编辑并 `source ./env.env`，临时覆盖放在加载之后 |
| 只改 `PEAK_*`，摸高阈值没有变化 | 当前场景显式字段优先，修改场景或整体覆盖数组 |
| 改固定倍率后，摸高请求数没变化 | `DATA_NUM_MULTIPLIER` 只控制固定测试；摸高使用独立的 `peak_data_multiplier` |
| 场景超过 MAX_CONTEXT_LEN | 检查输入加输出长度；脚本回退值 512000，当前 env 为 133120；该设置不会扩大模型服务的上下文能力 |
| 容器内模型路径不存在 | 检查宿主机 `MODEL_HOST_PATH` 及容器 `/workspace/model` 的实际挂载；复用容器时不能仅凭路径存在就认定挂载的是正确权重 |
| 99999、无结果或请求失败 | 检查模型名、接口、网关路径、鉴权和各轮 `process.log` / `aisbench.log` |
| 没有缓存命中信息 | 检查前缀场景是否启用 `prefix_test`，以及 `POD_INFO`、各端点 `/metrics` 和计数变化；重复率不等于命中率 |
| 摸高超过 200 | 200 是初始上界，按第 6 节配置扩容限制 |
| 一轮失败但脚本最后正常结束 | 这是逐任务继续的行为，检查 `all_results.csv` 各行状态，勿只看最终退出提示 |

## 10. 文件与调用关系

| 文件 | 作用 |
|---|---|
| [env.env](env.env) | 本次服务器配置，运行前编辑并加载 |
| [run_perf_scenarios.sh](run_perf_scenarios.sh) | 主入口，含环境默认值和场景数组 |
| [aisbench_perf_matrix.py](aisbench_perf_matrix.py) | 并发搜索、逐档执行及证据保存 |
| [aisbench_perf_matrix_config.py](aisbench_perf_matrix_config.py) | 搜索引擎默认配置、指标列名和扩容限制 |
| [aisbench_result_adapter.py](aisbench_result_adapter.py) | 按列名读取源 CSV、检查关键指标并投影汇总字段 |
| [aisbench_artifact_archive.py](aisbench_artifact_archive.py) | 保存固定测试原始产物和本轮输出快照 |
| [validate_addon.py](validate_addon.py) | 离线插件检查 |
| [validate_e2e_outputs.py](validate_e2e_outputs.py) | 运行后产物检查 |
| [validate_result_integrity.py](validate_result_integrity.py) | 运行后汇总与源结果字段追溯 |

调用顺序：shell 准备容器和配置 → 固定测试调用根目录 `aisbench_test.py`、归档及结果适配器 → 摸高由 matrix 多次调用同一基础入口 → 汇总。`aisbench_perf_matrix_config.py` 中的独立默认案例不是 shell 当前两组 64K 场景，二者不要混用。

本版已完成离线检查；指定镜像内的真实端到端运行、tokenizer 加载、业务回答和性能仍须在目标环境验证。

## 公开发布检查

公开配置、注释和文档统一使用 `192.168.1.x` 示例地址。`127.0.0.1` 等回环地址和 `0.0.0.0` 监听地址保留其程序语义。真实地址、鉴权信息、运行日志及本地分析材料放在被忽略的本地配置或输出目录，提交前执行：

```bash
python3 add/validate_publication.py
python3 add/validate_addon.py
```

发布扫描会检查 Git 实际跟踪的文件；无 `.git` 的交付包按公开源码目录检查。提交本地配置、日志、压测产物或真实网络地址会被拒绝。GitHub 工作流也执行同样的检查。
