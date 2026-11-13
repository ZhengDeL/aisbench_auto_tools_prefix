# AISBench 自动化压测工具

版本：v2026.11.13

基于 AISBench，对已部署的模型服务执行固定并发测试和并发摸高，支持前缀缓存、GSM8K 精度测试，以及多个独立服务并行测试。每份配置独立执行，分别保存结果、日志和原始数据。

原生仓保留在 `aisbench_auto_tools_prefix/`，自动化代码位于 `automation/`。运行时使用原生代码副本，配置、缓存和结果均写在原生目录之外，便于后续同步上游。

本版原生代码采用 [原仓 PR #28 的固定提交 4c2c484](https://github.com/rayn-zzz/aisbench_auto_tools_prefix/pull/28)，修复重复解析日志时 E2EL 被指标字典覆盖的问题。PR 尚未合并到原仓 `main`；原生子树与该提交完整一致，来源及后续同步方法见 [更新原生仓](automation/README.md#更新原生仓)。

## 运行条件

- Linux、Bash、Python 3.7 或更高版本、Docker。
- 已启动且可从压测主机访问的模型服务。
- 本机可读取的模型 tokenizer 目录，以及与宿主机架构匹配的 AISBench 镜像。

默认镜像：

```text
ghcr.io/aisbench/aisbench_benchmark:v3.1-20261013-master-openeuler24.03-py311
```

脚本启动的是 AISBench 压测客户端容器，模型服务需提前部署。

## 配置与启动

日常使用只需编辑配置文件，不需要先执行 `source`。主要配置项如下，完整参数参考位于配置文件末尾。

| 配置项 | 填写内容 |
|---|---|
| `MODEL_NAME` | 服务提供的模型名称或别名 |
| `MODEL_HOST_PATH` | 压测主机上的模型 tokenizer 目录 |
| `HOST_IP`、`HOST_PORT` | 模型服务地址和端口 |
| `NPU_NUM` | 吞吐归一化使用的卡数或 NPU 芯片数，报告时注明口径 |
| `SCENARIOS_JSON` | 输入长度、输出长度、并发、摸高阈值等测试场景 |

仓库中的地址、模型名和路径均为示例，使用前应填写实际值。默认 `config.env` 包含两组 64K 输入场景、固定并发和摸高。首次检查流程时，建议先编辑小规模示例：

```bash
vi automation/examples/test_a.env
bash run_benchmark.sh automation/examples/test_a.env
```

该示例配置输入 1024、输出 512、固定并发 4，不进行摸高。默认主测试请求数为 `4 × DATA_NUM_MULTIPLIER(4) = 16`；配置长度与实际 token 数可能因 tokenizer 和聊天模板而不同。

使用默认配置时，在仓库根目录运行：

```bash
vi config.env
bash run_benchmark.sh
```

不带参数时自动读取入口脚本同目录的 `config.env`；当前 shell 已导出的受支持全局参数可以覆盖文件中的值。需要隔离这些值时，显式指定配置：

```bash
bash run_benchmark.sh ./config.env
```

通常填写 `HOST_IP` 和 `HOST_PORT` 即可。使用 `API_URL` 时，应填写服务基址，例如 `http://192.168.1.10:8000/` 或 `http://192.168.1.10:8000/gateway/`。默认镜像会追加 `v1/chat/completions`，不要在基址中重复填写 `/v1/` 或完整聊天接口。地址与配置优先级见 [配置说明](automation/README.md#配置加载与优先级)。

## 多个服务并行测试

配置文件名可以自定，例如 `dsv4.env`、`qwen.env`。每份文件都包含完整的全局参数和场景，运行时自动加载：

```bash
bash run_benchmark.sh ./dsv4.env ./qwen.env
```

一份配置对应一个任务、一个专用 AISBench 容器；传入三份配置就启动三个，四份就启动四个。默认容器名为 `aisbench-<配置名>-<批次号>`。各任务并行执行，每个任务内部的测试轮次顺序执行。

并行配置必须指向不同的独立服务。同一有效主机和端口不能同时测试，换模型别名或 URL 路径也不能绕过检查。不同域名或代理实际共用同一后端时，应填写相同的 `SERVICE_ID`。互斥检查仅协调本压测主机上的本工具，其他主机的流量仍需自行协调。

## 目录与结果

```text
仓库根目录/
├── run_benchmark.sh             # 启动入口
├── config.env                   # 默认配置，末尾附参数参考
├── aisbench_auto_tools_prefix/  # 原生仓
├── automation/                  # 自动化模块、使用说明和配置示例
└── outputs/default/<批次号>/    # 本批次结果与日志
```

启动和结束时会打印批次目录。单份与多份配置使用相同结构：

```text
outputs/default/<批次号>/
├── batch_state.json
├── batch_results.json
├── batch_results.csv
├── merged_metrics.csv
├── logs/                       # 日常只看对应API的这一份日志
│   ├── dsv4__<模型名>.log
│   └── qwen__<模型名>.log
└── tasks/
    ├── dsv4__<模型名>/
    │   ├── dsv4__<模型名>__all_results.csv
    │   ├── dsv4__<模型名>__execution.log
    │   └── result_index.json
    └── qwen__<模型名>/...
```

每组有自己的 CSV 和日志；批次汇总保留任务、模型、服务和原始文件位置，不将不同服务的吞吐相加或延迟平均。自定义输出目录时，以 `batch_results.json` 和任务 `result_index.json` 中的路径为准。

固定测试的 `passed` 表示执行及适用的数据完整性检查通过，不代表满足摸高 SLO。摸高在给定的初始 `min..max` 内先测整数中点并二分，不预先倍增，也不假定上界已经失败。`PEAK_HARD_CAP=0` 时，实测初始上界通过后进入新窗口 `(max, 2×max]`，仍先测中点；例如 200 通过后先测 300，不直接跳到 400。`PEAK_HARD_CAP=1` 则在配置上界封顶；`min` 是可向 `floor` 下探的软下界。详见 [摸高算法](automation/README.md#摸高算法与终态)。

SLO 容差默认关闭（`PEAK_SLO_TOLERANCE=0.0`），需要时由用户显式配置；场景 `peak_slo_tolerance` 优先。它是相对**配置阈值**的比例，不以某次好结果为基准。例如 `0.05` 表示 5%，TTFT 15 秒的有效上限为 15.75 秒，TPOT 60 毫秒的有效上限为 63 毫秒。原始值、基础阈值和容差上限保留在元数据与搜索轨迹中，区分严格通过、容差通过和超限；23 列指标 CSV 不增加列。请求失败、缺数据和基础设施错误不能用容差放行。

`peak_confirmed` 表示通过/失败边界经过新复测确认并达到配置精度，`peak_capped` 表示硬上限已确认通过。临界候选复测出现 SLO 不通过时，会继续寻找更低的稳定点；例如 35 波动而 34 的独立复测全部通过，可保留 34 的真实结果，但若更高边界仍波动，终态仍为 `peak_unstable`，不能把 34 写成已确认的最高并发。预算、请求失败和执行错误仍分别记录。23 列指标的单位、空值及选中轮次规则见 [结果说明](automation/README.md#结果与数值口径)。

## 排查与维护

**每个 API 只看一份日志：`logs/<配置名>__<模型名>.log`。** 同一个 API 的多个场景、固定并发、前缀测试及摸高轮次按执行顺序追加到这份文件，不必逐轮找文件。不同 API 分别保存，启动和结束时打印各自的实际路径。

每轮 AISBench 原生日志按原始字节完整保存，内容、进度、换行、表格、警告和错误均不改写。工具的流程说明只放在轮次前后，不插入原生正文。运行时即可跟踪这份日志：

```bash
bash run_benchmark.sh ./service.env

# 在另一个终端查看；替换为启动时打印的实际文件路径。
tail -n 50 -f 'outputs/default/<批次号>/logs/service__<模型名>.log'

# 只需要业务摘要时，主动指定 summary。
bash run_benchmark.sh --console summary ./service.env
```

脚本自动保存日志，无需再用 `tee` 创建另一份 `benchmark.log`。默认终端显示全部任务详情，切换 API 时标明来源，结果表整块显示；`--console summary` 只减少终端显示，不影响每个 API 的完整日志。显式 `--console auto` 仍按 TTY 选择显示模式。

内部来源清单、显示传输文件及预检诊断放在 `logs/.diagnostics/`，原始测量文件继续留在任务目录，供故障和数值核查。它们不是另一套日常日志入口；已有批次和原生源文件不会被删除。具体规则见 [日志排查与保留](automation/README.md#日志排查与保留)。

管理器被强杀后，容器中的请求可能仍在执行。不要删锁、改容器名或换输出路径绕过隔离，应按 [恢复规则](automation/README.md#服务隔离容器与取消) 核对原任务和完整容器 ID。日志、配置和原始结果可能含真实地址、请求内容或凭据，分享前需单独审查。

日常调整服务和测试场景只需修改配置。新增功能时修改 `automation/core/` 中对应模块；原生目录只随选定源提交同步更新，方法见 [更新原生仓](automation/README.md#更新原生仓)。本项目自动化模块的开发回归测试、发布检查工具和 CI 配置保留在维护者本地；原生子树保留所选提交自带的全部文件。

更换镜像、上游或模型后，应重新检查请求参数、运行流程，并将 CSV 与对应原始结果核对。运行模块保留结果核对入口，使用方法见 [验证与发布](automation/README.md#验证与发布)。
