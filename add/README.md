# add/ 增强压测工具使用说明（新手友好版）

> 本目录最初基于 v0602 工具开发，是放在 `add/` 内的**非侵入式增强插件**。当前搭配下方固定的上游版本，
> 只在旁边加了一套「一键多场景压测」能力。如果你只想跑单条命令测一次，用上层目录的
> `aisbench_test.py` 就够了；如果你想**一次跑多个场景、自动摸最高并发、自动出汇总表**，
> 就用本目录的 `run_perf_scenarios.sh`。

### 2026-10-15 同步说明

- `add/` 源码及配置以 server37 的版本为基础，上层基础文件同步到原仓 `ef2b6d8`。
- 保留服务器实际使用的 `env.env`、场景参数及摸高配置；下文通用场景示例不覆盖这些配置。
- 结果适配按列名读取旧版 20 列或新版 24 列源 CSV，选取共同的 20 列后生成原有 23 列汇总；原始结果仍保留在归档中。
- 外部环境变量继续使用 `API_URL`，生成容器配置时写入原仓的 `URL` 字段。
- 默认镜像更新为 `ghcr.io/aisbench/aisbench_benchmark:v3.1-20260912-master-openeuler22.03-py310`。本轮尚未在该镜像内真实运行，也未完成推理服务验证。

---

## 一、这是什么？能帮我做什么？（先看这里）

一句话：**它帮你把"配环境 → 跑多个测试场景 → 找最高并发 → 出汇总结果"全自动串起来。**

具体帮你自动完成的事：

1. **自动管理容器**：检查/复用/重建 aisbench 的 Docker 容器，不用手动 `docker run`。
2. **自动同步工具**：把本工具打进容器，不用手动 `docker cp`。
3. **自动写 config.py**：你只需在命令行传几个环境变量，脚本自动生成容器内的 `config.py`，不用手动改文件。
4. **批量跑场景**：一次跑多个测试场景（如"日常对话""RAG 办公""AI 写代码"），每个场景可以跑多个固定并发。
5. **自动摸高**：在你想测的场景里，自动二分搜索"满足延迟要求的最高并发"（即系统扛得住多少并发）。
6. **自动归档 + 汇总**：每轮结果归档，最后出一张汇总表和一个 `all_results.csv`。

**适合谁**：想做模型/服务性能压测，但不想一条条手敲命令、不想手动算并发的人。

---

## 二、前置条件（开始前请逐项确认）

压测机（跑脚本的那台机器）上必须具备：

| # | 条件 | 怎么检查 |
|---|---|---|
| 1 | 已安装 Docker | `docker --version` 能出版本号 |
| 2 | 能拉到/已有镜像 `ghcr.io/aisbench/aisbench_benchmark:v3.1-20260912-master-openeuler22.03-py310` | `docker images \| grep aisbench` |
| 3 | 模型权重目录存在（用于读 tokenizer） | 例如 `/mnt/models/your_model`（**替换成你自己的路径**） |
| 4 | 被测推理服务可访问（知道它的 IP 和端口） | `curl http://你的IP:端口/v1/chat/completions` 能通 |
| 5 | 本机有 `python3`、`bash`、`tar` | `python3 --version` 等 |

> 注意：运行主机架构须与所用镜像匹配。本轮未查询指定 tag 的架构清单；如需切换镜像，可覆盖环境变量 `EXPECTED_IMAGE`。

---

## 三、快速开始（3 步跑起来）

```bash
# 第 1 步：进入 add 目录（脚本必须在这里运行）
cd /path/to/aisbench_auto_tools_prefix/add

# 第 2 步：设置 3 个必填项（替换成你自己的值）
export MODEL_NAME=your-model-name          # 服务化配置里的模型名
export MODEL_HOST_PATH=/mnt/models/your_model  # 宿主机模型权重路径
export HOST_IP=your.host.ip                # 推理服务 IP
export HOST_PORT=8000                      # 推理服务端口

# 第 3 步：运行
bash run_perf_scenarios.sh
```

跑完后，结果在**上层目录的** `outputs/default/scenario_results/<时间戳>/` 下。

> 💡 第一次跑建议先看「六、看懂结果」再回头看详细配置。

---

## 四、配置说明（所有可调项）

### 4.1 必须修改的项

| 变量 | 必填 | 说明 |
|---|---|---|
| `MODEL_NAME` | ✅ | 服务化配置的模型名称（推理服务认识的那个名字） |
| `MODEL_HOST_PATH` | ✅ | 模型权重在**宿主机**上的路径（用于读 tokenizer） |
| `HOST_IP` | ✅ | 推理服务 IP（**最常出错的一项，务必确认能 ping 通/curl 通**） |
| `HOST_PORT` | ✅ | 推理服务端口 |

### 4.2 两种鉴权模式

**模式 A：直连 IP:PORT（不带 Key）**

服务直接暴露在 `IP:PORT`，不需要鉴权：
```bash
export MODEL_NAME=your-model-name
export MODEL_HOST_PATH=/mnt/models/your_model
export HOST_IP=your.host.ip HOST_PORT=8000
bash run_perf_scenarios.sh
```
请求地址自动用 `http://你的IP:端口/v1/chat/completions`。

**模式 B：API Key + 自定义 URL（经网关/litellm）**

服务前面有网关，需要 Key 和自定义地址：
```bash
export MODEL_NAME=your-model-name
export MODEL_HOST_PATH=/mnt/models/your_model
export HOST_IP=your.host.ip HOST_PORT=8000
export API_KEY=sk-xxxxxxxx                        # 你的 API Key
export API_URL=http://your.host.ip:port/litellm/v1 # 完整请求地址
bash run_perf_scenarios.sh
```
- `API_KEY` 非空时，会写入模型配置做鉴权。
- `API_URL` 写入容器内 `config.py` 的 `URL` 字段，再由原仓传给 AISBench 模型配置的 `url`。非空时优先使用自定义 URL，最终请求路径遵循镜像内 AISBench 的 URL 处理规则；网关示例中的路径须按实际服务确认。

### 4.3 全部环境变量（都可以覆盖，无需改脚本）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MODEL_NAME` | `your-model-name` | 模型名（**必改**） |
| `MODEL_HOST_PATH` | `/mnt/models/your_model` | 宿主机模型权重路径（**必改**） |
| `HOST_IP` / `HOST_PORT` | `your.host.ip` / `8000` | 服务地址（**必改**） |
| `API_KEY` | 空 | API Key，空则不鉴权 |
| `API_URL` | 空 | 自定义请求 URL，空则用 `IP:PORT/v1/chat/completions` |
| `FAILED_REQ_LIMIT` | `0` | **失败请求阈值**：本轮 Failed Requests 超过此值即判"不达标"。默认 0 = 出现任何失败即不达标 |
| `MAX_CONTEXT_LEN` | `16384` | 单场景 `input_len + output_len` 上限，超过会报错（防止配错爆显存） |
| `CONTAINER_NAME` | `ais_bench` | 容器名 |
| `EXPECTED_IMAGE` | `ghcr.io/aisbench/aisbench_benchmark:v3.1-20260912-master-openeuler22.03-py310` | 期望镜像（与现有容器不一致会自动重建） |
| `HOST_OUTPUT_DIR` | `脚本上层目录/outputs/default` | 结果输出目录 |
| `TOOLS_HOST_DIR` | 脚本上层目录 | 要打进容器的工具目录 |
| `DATA_NUM_MULTIPLIER` | `10` | 固定并发请求数 = 并发 × 倍率；须为正整数，可通过环境变量覆盖，场景中的 `data_num` 优先 |
| `PEAK_MIN_CONCURRENCY` | `1` | 摸高默认起始并发 |
| `PEAK_MAX_MULTIPLIER` | `20` | 摸高默认上界 = 首个固定并发 × 此值 |
| `PEAK_DATA_MULTIPLIER` | `4` | 摸高默认数据条数倍数 |
| `PEAK_WAIT_SECONDS` | `10` | 摸高每轮等待秒数 |
| `PEAK_TTFT_LIMIT` | `5` | 摸高默认 TTFT 阈值（秒） |
| `PEAK_TPOT_LIMIT` | `50` | 摸高默认 TPOT 阈值（毫秒） |

> 💡 两种配置方式：① 命令行 `export 变量=值`（推荐，不改脚本）；② 直接改脚本顶部的默认值。

示例：把"失败 ≤5 才算不达标"放宽：
```bash
export FAILED_REQ_LIMIT=5
bash run_perf_scenarios.sh
```

---

## 五、测试场景怎么配（脚本里的 `SCENARIOS_JSON`）

脚本顶部有一段 `SCENARIOS_JSON`，是一个场景数组。**想改测试内容，就改这里。**

### 5.1 每个场景的字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | ✅ | 场景名（会出现在结果里，建议用英文+下划线） |
| `input_len` | ✅ | 输入长度（token 数） |
| `output_len` | ✅ | 输出长度（token 数） |
| `concurrency` | ✅ | **并发数组**，见 5.2 |
| `request_rate` | ✅ | 请求发送频率，`0` 表示尽量打满（不限速） |

**摸高相关**（仅当 `concurrency` 含 `"max"` 时才用）：

| 字段 | 说明 |
|---|---|
| `ttft_limit` | TTFT 阈值（秒），超过判不满足 |
| `tpot_limit` | TPOT 阈值（毫秒），超过判不满足 |
| `peak_min_cc` | 摸高搜索起始并发 |
| `peak_max_cc` | 摸高搜索区间上界 |
| `peak_data_multiplier` | 数据条数倍数 |
| `peak_wait` | 每轮等待秒数 |

**可选**（透传给底层 `aisbench_test.py` 的额外参数）：`dataset_type`、`prefix_num`、`repeat_rate`、`seed`、`dp`、`test_type`、`npu_num`、`data_num` 等。

### 5.2 并发数组的两种用法

`concurrency` 是个数组，里面可以放两类东西：

- **数字**（如 `100`、`50`）→ **固定并发测试**：用这个并发跑一轮，数据条数 = 并发 × 4。
- **字符串 `"max"`** → **摸高搜索**：自动在这个场景的并发区间里二分，找满足 TTFT/TPOT/失败率要求的最高并发。

举例：
```json
"concurrency": [100, "max"]
```
意思是：先用并发 100 跑一轮固定测试；再做一次摸高搜索找最高并发。

### 5.3 三个典型场景示例

以下保留原说明中的 3 个典型场景作为配置示例。当前实际场景以服务器同步下来的 `run_perf_scenarios.sh` 中 `SCENARIOS_JSON` 为准：

| 场景名 | 输入/输出 | 固定并发 | 摸高 | 说明 |
|---|---|---|---|---|
| `daily_chat_3p5k1k` | 3584 / 1024 | 100 | ✅（TTFT<5s, TPOT<50ms） | 日常对话，短输入 |
| `rag_office_15k1k` | 15360 / 1024 | 100 | ✅（TTFT<10s, TPOT<50ms） | RAG 办公，长输入 |
| `ai_coding_15k1k` | 15360 / 1024 | 50 | ✅（TTFT<300s, TPOT<50ms） | AI 写代码，带前缀缓存(prefix_cache, repeat_rate=0.9) |

### 5.4 自定义场景示例

**示例 1：只跑固定并发，不摸高**
```json
{
  "name": "quick_test",
  "input_len": "2048",
  "output_len": "2048",
  "concurrency": [8, 16, 32],
  "request_rate": "0"
}
```
分别跑并发 8、16、32 三轮，不摸高。

**示例 2：固定 + 摸高混合**
```json
{
  "name": "daily_chat",
  "input_len": "3584",
  "output_len": "1024",
  "concurrency": [3, 4, "max"],
  "request_rate": "0",
  "ttft_limit": 5,
  "tpot_limit": 50,
  "peak_min_cc": 1,
  "peak_max_cc": 200
}
```
先跑并发 3、4 两轮固定测试；再摸高找出 TTFT<5s 且 TPOT<50ms 且失败率达标的最**高并发**。

---

## 六、运行 & 看懂结果

### 6.1 怎么运行

```bash
cd /path/to/aisbench_auto_tools_prefix/add
bash run_perf_scenarios.sh
```

运行过程中终端会打印进度，每个场景开始/完成都会提示。**某一轮失败不会中断整体**，会记一条 `failed` 然后继续下一个。

### 6.2 结果在哪

| 路径 | 内容 |
|---|---|
| `outputs/default/scenario_results/<时间戳>/all_results.csv` | 汇总结果（**核心文件**，23 列） |
| `outputs/default/scenario_results/<时间戳>/execution.log` | 全量执行日志 + 末尾汇总表 |
| `outputs/default/scenario_results/latest` | 指向最新一次结果的软链 |
| `outputs/default/peak_<场景>_<时间戳>/peak_search_report.json` | 摸高明细 |

> 📌 `outputs/` 在**脚本上层目录**（即项目根目录下），不在 `add/` 里。

### 6.3 `all_results.csv` 各列含义（共 23 列，顺序固定）

```
scenario, current_time, input_len, output_len, total_req, max_cc, cc, rr,
TTFT avg, TTFT P90, TPOT avg, TPOT SLO_P90, E2E_time,
output_throughput, single_output_throughput, E2E_throughput, single_E2E_throughput,
qps, qpm, input_token_throughput, prefill_token_throughput, repeat_rate, status
```

| 列 | 含义 |
|---|---|
| `scenario` | 场景名 |
| `cc` | 本轮并发数 |
| `TTFT avg` / `TTFT P90` | 首字延迟 平均 / P90（秒） |
| `TPOT avg` / `TPOT SLO_P90` | 每个输出 token 生成时间 平均 / P90（毫秒） |
| `E2E_time` | 端到端总耗时（秒） |
| `output_throughput` / `E2E_throughput` | 输出吞吐 / 端到端吞吐（token/s） |
| `qps` / `qpm` | 每秒 / 每分钟请求数 |
| `input_token_throughput` / `prefill_token_throughput` | 输入 / prefill 吞吐 |
| `status` | 状态，见 6.4 |

### 6.4 `status` 状态含义

| status | 含义 |
|---|---|
| `passed` | 固定并发测试，达标 |
| `failed` | 执行失败 / 请求失败 / 失败请求数超过 `FAILED_REQ_LIMIT` |
| `no_result` | 跑完了但没提取到结果（异常） |
| `peak` | 摸高搜索得到的最优并发行 |

### 6.5 汇总表

终端和 `execution.log` 末尾会打印一张对齐的表格，类似：

```
scenario              cc  status  TTFT avg  TTFT P90  TPOT avg  ...  QPS   QPM
daily_chat_3p5k1k    100  PASS      0.85      1.20       35      ...  120   ...
daily_chat_3p5k1k    180  PEAK      4.20      4.80       48      ...  200   ...
```
一眼看清每个场景的并发、状态、延迟、吞吐。

### 6.6 关键指标科普（不懂就看这里）

- **并发（concurrency）**：同时发多少个请求。并发越高，系统压力越大。
- **TTFT（Time To First Token）**：从发出请求到收到第一个字的延迟。**越小越快**。用户体验上"快不快"主要看它。
- **TPOT（Time Per Output Token）**：生成每个输出 token 的时间（毫秒）。**越小越快**。
- **吞吐（throughput）**：单位时间处理的 token 数。**越大越好**。
- **QPS（Queries Per Second）**：每秒完成多少个请求。**越大越好**。
- **摸高（peak search）**：在延迟不超标的前提下，自动找系统能扛住的最高并发。回答"这套服务最多能同时服务多少用户"。
- **prefix cache（前缀缓存）**：多个请求共享相同前缀时，缓存前缀加速。`repeat_rate` 是前缀重复率。

---

## 七、达标 / 不达标 判定规则

一轮被判为**不达标**，满足任一条件即可：

1. 进程退出码非 0（执行失败 / aisbench 崩溃）。
2. 关键指标为哨兵值 `99999`（请求基本全失败）。
3. **Failed Requests > `FAILED_REQ_LIMIT`**（默认 >0 即不达标）。
4. 摸高额外判断：TTFT > `ttft_limit` 或 TPOT > `tpot_limit`。

**摸高搜索逻辑**：从起始并发往上试，一旦某一档"不达标"就作为上界，向下二分，最终给出满足全部条件的最高并发。

---

## 八、注意事项 / 已知行为

- **摸高上界不是硬顶**：`peak_max_cc` 只是初始搜索区间上界。若该上界也通过，摸高会继续 ×2 向上探测（200→400→…），直到某档不满足才停。**如需把 `peak_max_cc` 变成硬上限**，请联系维护者在摸高命令加 `--auto-expand-max-concurrency $PEAK_MAX_CC`。
- **容器复用**：镜像、输出挂载、模型路径三者一致就复用现有 `ais_bench` 容器，否则自动重建。复用的旧容器里若残留早期拷入的 `outputs/`，删容器重建即可清理。
- **工具同步**：用 `tar` 管道同步并排除 `outputs/`，避免把历史结果反复拷进容器。
- **单轮失败不中断**：某一轮失败只会记 `failed` 行，继续跑后续场景。
- **MAX_CONTEXT_LEN 保护**：任一场景 `input_len + output_len` 超过 `MAX_CONTEXT_LEN`（默认 16384）会直接报错退出，防止配错爆显存。

---

## 九、常见问题（FAQ）

| 现象 | 排查 |
|---|---|
| `容器内模型路径不存在` | 确认 `MODEL_HOST_PATH` 在宿主机真实存在且可读 |
| 请求全失败 / 指标全是 99999 | 检查 `HOST_IP/HOST_PORT` 是否对；走网关时检查 `API_URL`（是否需要 `/v1`）和 `API_KEY` |
| `场景 JSON 解析失败或超过 MAX_CONTEXT_LEN` | 检查 `SCENARIOS_JSON` 格式；确认 `input_len + output_len ≤ MAX_CONTEXT_LEN` |
| `SUMM-MTRC-001 different structure of perf data` | aisbench 端数据异常，多为服务/端点返回不稳定，重试或检查推理服务 |
| 摸高并发涨得过高 | 见第八节"摸高上界不是硬顶" |
| 想只快速验证流程是否通 | 把 `SCENARIOS_JSON` 改成小输入/输出、并发 `[1,2]`、去掉 `"max"`，先跑通再说 |
| 打屏不显示 prefix cache 命中率 | 只有带前缀的场景（`dataset_type=prefix_cache`）才涉及；其余可参考上层目录 README 的 FAQ |
| 想改默认场景 | 直接编辑脚本顶部的 `SCENARIOS_JSON`，或用 `SCENARIOS_JSON_OVERRIDE` 环境变量整体覆盖 |

---

## 十、add/ 目录文件说明

| 文件 | 作用 |
|---|---|
| `run_perf_scenarios.sh` | **主入口**。一键编排：容器管理 → 同步工具 → 写 config.py → 跑场景 → 摸高 → 汇总 |
| `aisbench_perf_matrix.py` | 摸高搜索引擎，自动找满足延迟要求的最高并发 |
| `aisbench_perf_matrix_config.py` | 摸高的默认配置（搜索区间、阈值、全局参数等） |
| `aisbench_artifact_archive.py` | 每轮固定并发跑完后做产物归档（抓取 aisbench 原始日志/CSV 快照） |
| `aisbench_result_adapter.py` | 按列名读取源 CSV，兼容旧版 20 列与新版 24 列，保持汇总格式 |
| `validate_addon.py` | 校验插件本身完整性（文件存在、变量接线、JSON 渲染等），开发/自检用 |
| `validate_e2e_outputs.py` | 校验端到端输出（结果目录、CSV 行数、状态等），跑完后自检用 |
| `validate_result_integrity.py` | 校验结果完整性（表头、列数、归档文件等），跑完后自检用 |
| `README.md` | 本文件 |

> 普通用户只需关心 `run_perf_scenarios.sh`；其余 `*.py` 是它调用的子模块或校验工具，无需手动运行。

---

## 十一、和上游基础工具的关系

- 本目录最初为 v0602 工具增加功能，当前已适配固定上游版本 `ef2b6d8`；基础文件直接使用该上游版本，兼容逻辑放在 `add/`。它通过 `ADD_DIR`/`PROJECT_ROOT` 定位上层工具，复用上层的 `aisbench_test.py`、`config.py`、`generate_dataset.py` 等核心能力。
- 上层目录的 `README.md` 解释了底层参数（`--input_len`、`--repeat_rate`、`--prefix_num` 等）和数据集生成逻辑；本 README 只讲"怎么用 `run_perf_scenarios.sh` 一键跑"。
- 想深入理解单个参数，看上层 README；想一键出汇总结果，看本 README。

---

## 附：完整运行示例（带 API Key 的网关场景）

```bash
# 1. 进入 add 目录
cd /path/to/aisbench_auto_tools_prefix/add

# 2. 配置（按你的实际环境改）
export MODEL_NAME=your-model-name
export MODEL_HOST_PATH=/mnt/models/your_model
export HOST_IP=your.host.ip
export HOST_PORT=8000
export API_KEY=sk-xxxxxxxx
export API_URL=http://your.host.ip:port/litellm/v1
export FAILED_REQ_LIMIT=0          # 出现任何失败即不达标（严格）
export PEAK_TTFT_LIMIT=5           # 摸高 TTFT 阈值 5 秒
export PEAK_TPOT_LIMIT=50          # 摸高 TPOT 阈值 50 毫秒

# 3. 运行
bash run_perf_scenarios.sh

# 4. 看结果
cat ../outputs/default/scenario_results/latest/all_results.csv
tail -30 ../outputs/default/scenario_results/latest/execution.log
```

---

## Supplement: NPU_NUM, POD_INFO, and call flow

### NPU_NUM

`run_perf_scenarios.sh` defines one shared variable near the top:

```bash
NPU_NUM="${NPU_NUM:-128}"
```

The default is 128. Override it without editing the script:

```bash
NPU_NUM=64 bash run_perf_scenarios.sh
```

The value is passed to the original `aisbench_test.py --npu_num` and is used to calculate single-card throughput. It is not detected automatically from the physical device count.

### Configure POD_INFO from the add-on entrypoint

`run_perf_scenarios.sh` now accepts `POD_INFO` as a comma-separated environment variable and writes it into the container-side `config.py`:

```bash
POD_INFO="192.168.1.11:8000,192.168.1.12:8000" bash run_perf_scenarios.sh
```

The generated configuration becomes:

```python
POD_INFO = ["192.168.1.11:8000", "192.168.1.12:8000"]
```

If `POD_INFO` is empty or unset, the generated value remains `[]`, and the original `aisbench_test.py` falls back to `HOST_IP:HOST_PORT`. Use this setting together with `dataset_type=prefix_cache` and `prefix_test` when prefix-cache metrics from multiple Pods are required.

### POD_INFO behavior

The original `aisbench_test.py` uses `POD_INFO` only for `dataset_type=prefix_cache` when `prefix_test` is enabled:

```python
if not POD_INFO:
    pod_info = [HOST_IP + ":" + HOST_PORT]
else:
    pod_info = POD_INFO
```

`POD_INFO=[]` uses the fallback `HOST_IP:HOST_PORT`; a non-empty list such as `["ip1:port1", "ip2:port2"]` is used to query metrics from those Pod endpoints for prefix-cache hit-rate calculation. Normal `dataset_type=normal` runs do not use it.

Important: `run_perf_scenarios.sh` generates the container-side `config.py` from the add-on environment. Set `POD_INFO` in that environment, for example through `env.env`; a value manually placed in the upstream `config.py` is overwritten by this entrypoint. An empty environment value generates `POD_INFO=[]`.

### Add-on call flow

```text
run_perf_scenarios.sh
├─ manage/reuse Docker and sync files
├─ generate container-side config.py
├─ fixed runs: original aisbench_test.py -> add/aisbench_artifact_archive.py
├─ peak runs: add/aisbench_perf_matrix.py
│  ├─ add/aisbench_perf_matrix_config.py
│  ├─ original aisbench_test.py
│  └─ add/aisbench_artifact_archive.py
└─ aggregate results
```

`validate_addon.py`, `validate_e2e_outputs.py`, and `validate_result_integrity.py` are validation utilities, not part of the normal test execution chain. The actual benchmark still runs through the original `aisbench_test.py`; files outside `add` do not need to be modified.

遇到问题先看「九、常见问题」，再去看 `execution.log` 里的详细日志。
