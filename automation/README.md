# AISBench 自动化使用与维护

本说明对应 **v2026.10.19 发布候选**。文中的行为和校验命令是使用、实现和验收约定，不代表所有实机场景已经验收。日常启动入口和默认配置均在仓库根目录：`run_perf_scenarios.sh`、`env.env`。

## 目录与修改位置

| 位置 | 职责及修改边界 |
|---|---|
| 根目录 `env.env` | 服务、镜像、路径和场景；日常只改这里即可 |
| 根目录 `run_perf_scenarios.sh` | 定位安装目录、检查 Python、启动管理器 |
| `aisbench_auto_tools_prefix/` | 选定上游的原生源码；不加入增补代码，不产生运行文件 |
| `automation/core/aisbench_run_manager.py` | 加载及冻结配置、全部任务预检、并行调度、互斥、取消、容器身份监督 |
| `automation/core/aisbench_configuration.py` | 公开参数默认值、参数校验、场景展开和执行计划 |
| `automation/core/aisbench_worker.py` | 单任务容器准备、原生源码副本配置、固定轮次与摸高调用 |
| `automation/core/aisbench_execution.py` | 一次实际执行及请求/结果作用域判定 |
| `automation/core/aisbench_perf_matrix.py` | 摸高搜索、复测、预算及终态 |
| `automation/core/aisbench_perf_matrix_config.py` | 独立摸高入口的默认配置接口 |
| `automation/core/aisbench_result_adapter.py` | 按表头读取原生结果、23 列投影、单位及适用性校验 |
| `automation/core/aisbench_result_validation.py` | 运行时及离线共用的作用域、原始来源、完整性和结果检查，也可单独执行 |
| `automation/core/aisbench_artifact_archive.py` | 保存原始证据、作用域、清单与校验值 |
| `automation/core/aisbench_batch_results.py` | 批次/任务索引、合并视图及一致快照发布 |
| `automation/core/aisbench_logging.py` | 人类日志、结构化事件、脱敏及写盘错误传播 |
| `automation/tools/` | `validate_publication.py` 发布审计与 `check_all.sh` 唯一离线门禁 |
| `automation/tests/` | 启动、配置、算法、归档、输出及故障回归测试 |
| `automation/examples/` | 两份可改名的小规模完整配置 |
| `automation/metadata/upstream_manifest.json` | 原生仓基线、文件内容和文件模式清单 |
| `outputs/` | 运行日志、原始结果和验证报告，不作为公开源码提交 |

新增参数时同步修改参数定义、使用它的执行模块、配置末尾参考和相应测试；新增执行行为或算法时修改负责该行为的模块。无需为了修改输入长度、并发、服务地址而改 Python。原生接口发生变化时，兼容逻辑继续放在 `core/`。

## 运行条件与启动

需要 Linux、Bash、Python 3.7+、Docker 和常用 Linux 工具。默认镜像为：

```text
ghcr.io/aisbench/aisbench_benchmark:v3.1-20261013-master-openeuler24.03-py311
```

宿主机架构须匹配镜像；宿主机模型路径应可读，镜像内 tokenizer 应能加载。被测模型服务应已就绪，且能由使用宿主机网络的 AISBench 容器访问。需要缓存指标时，相关 `/metrics` 地址也须可访问。

在仓库根目录运行，无需先 `source`：

```bash
vi env.env
bash run_perf_scenarios.sh ./env.env
```

默认配置包括两组 64K 输入场景、固定并发和摸高。只验证流程时先改小规模示例中的模型和地址：

```bash
vi automation/examples/test_a.env
bash run_perf_scenarios.sh automation/examples/test_a.env
```

该示例为输入 1024、输出 512、固定并发 4，默认主测试请求数为 16，不摸高。原生数据准备、预热或前缀准备不应与主测试请求数混为一谈。

N 份完整配置可一次启动，文件名不写死：

```bash
bash run_perf_scenarios.sh automation/examples/test_a.env automation/examples/test_b.env
```

也可使用自己的文件名，例如 `./prefill.env`、`./decode.local.env`；路径含空格时加引号。任务名取文件名，去掉 `.env` 或 `.local.env` 后，须以英文字母或数字开始，只包含英文字母、数字、点、下划线和短横线。

每份配置启动一个专用 AISBench 客户端容器；默认名为 `aisbench-<任务名>-<批次号>`。模型服务由用户管理。各任务并行，单个任务中的测试轮次顺序执行。容器名与输出标签中的过长或特殊名称会截短并附校验后缀，索引保留原始名称。

## 配置加载与优先级

一份配置包含全局参数和一个 `SCENARIOS_JSON` 块。全局参数由 Bash 加载一次；场景 JSON 由工具从同一份冻结配置中直接读取，不需要导出为环境变量。配置文件会执行其中的 Shell 语句，只运行自己信任的文件。

| 启动方式 | 全局参数来源 | 场景来源 |
|---|---|---|
| `bash run_perf_scenarios.sh ./env.env` | 指定文件；受支持全局参数不继承调用者的同名值 | 指定文件的 JSON 块 |
| `bash run_perf_scenarios.sh ./a.env ./b.env` | 每份文件独立加载，彼此隔离 | 每份文件自己的 JSON 块 |
| `bash run_perf_scenarios.sh` | 先自动加载根目录 `env.env`，再按“是否已导出”保留父进程的受支持全局覆盖值 | 根目录 `env.env` 的 JSON 块 |

零参数模式兼容已有 `export` 用法，例如父进程已导出的 `PEAK_HARD_CAP=0` 会覆盖文件中的 `1`。空 `API_KEY` 也按存在性保留；随后各参数的空值校验和默认回退仍按参数类型处理。推荐显式传文件，避免旧 shell 中的值影响复现。

未提供的参数使用代码默认值；场景中的对应字段覆盖该场景的全局默认。每份配置的内容及 SHA 在启动时冻结，运行中编辑文件只影响下一批次。配置相对路径和用户填写的相对宿主机路径按启动时工作目录解析；配置会从冻结副本加载，不应依靠 `BASH_SOURCE` 在临时位置查找相邻文件。

`env.env` 和两个示例的末尾都附有**仅作注释、不会执行**的完整参数参考，覆盖 31 个公开全局参数、场景 JSON 字段及独立摸高 CLI。每项说明默认值、单位、作用域、覆盖、空值和示例。应区分：

- **代码默认值**：未配置时采用，例如 `PEAK_HARD_CAP=0`、`MAX_CONTEXT_LEN=512000`。
- **本文件有效赋值**：公开默认文件明确设置 `PEAK_HARD_CAP=1`、`MAX_CONTEXT_LEN=133120`，场景设置 TTFT 15 秒、TPOT 60 毫秒。
- **实际运行值**：以本批次的脱敏配置快照和计划为准；编辑配置后，末尾参考中的示例不会自动变化。

固定轮次未指定 `data_num` 时，使用 `配置并发 × DATA_NUM_MULTIPLIER`，该全局倍数默认 `4`。重复并发合法，如 `[1, 10, 10, 20, "max"]`；重复的固定轮次使用独立名称和归档，不覆盖前一轮。`"max"` 表示增加一次摸高，不是一个固定并发数。

`NPU_NUM` 是归一化吞吐分母，不自动发现硬件。按物理卡报告就填物理卡数，按芯片报告就填芯片数，并在报告注明。A3 一张物理卡含两颗 NPU 芯片，两种分母的结果不同。`POD_INFO` 数量不自动设置 `dp`；指标端点共享时，缓存计数也可能包含其他流量。

## 服务隔离、容器与取消

所有配置先完成预检，再开始请求负载。同批次和本机不同批次之间均检查服务、容器和输出资源：

- 有效服务地址来自 `API_URL`；未配置时使用 `HOST_IP`、`HOST_PORT`。同一有效主机和端口禁止同时压测，换 URL 路径、协议或鉴权不能绕过。
- 同一后端可能有不同 DNS 名或代理入口；应显式填写相同的 `SERVICE_ID`。不同 `SERVICE_ID` 也不能绕过已识别的同主机同端口冲突。
- 锁只协调同一压测主机上的本工具，不是跨宿主机的中心锁；其他客户端流量仍需自行协调。不同端口也须确认确实对应独立服务。
- `CONTAINER_NAME`、`HOST_OUTPUT_DIR`、`TOOLS_HOST_DIR` 可按参考设置。输出路径不允许重叠，也不允许指向原生目录或通过符号链接进入原生目录。

工具操作容器时核对管理标签、镜像、挂载和固定的完整容器 ID。未知同名容器会报错；不会因为名称相同就停止或删除其他容器。每轮执行还要确认当前名称仍指向原容器，避免中途重建后错误归属结果。

普通单任务失败后，其他独立任务可继续；批次公共日志不可写时，整个批次停止。`Ctrl+C` 或 `SIGTERM` 取消本批次负载，按已确认的固定 ID 停止本工具拥有的负载及容器，保留容器、结果和日志，不操作模型服务。

无法确认负载已停止时，任务进入 `cleanup_failed` 并持久隔离相关资源。下次启动必须先确认同一 Docker daemon 上记录的原完整 ID、管理身份及停止/不存在状态。不要删除锁或隔离记录、改名绕过；先根据日志只读核对原容器。

## 摸高算法与终态

摸高的目标是在本场景、请求率、指标口径和预算下，寻找满足所选延迟 SLO 的已验证并发边界。它不代表模型在其他输入、输出、缓存或流量条件下的通用容量。

流程为：初始并发测量 → 必要时向下找可通过点 → 向上扩展形成通过/不通过区间 → 区间内二分 → 新一轮复测确认边界。`peak_min_cc` 是初始测量点，允许向下搜索到 `peak_floor_cc`；它不是永不下探的硬下限。

TTFT 阈值用**秒**，TPOT 阈值用**毫秒**，从 CSV 的毫秒值换算后比较；等于阈值视为通过。某阈值不大于零时关闭该项 SLO；两项都关闭时，结果仅表示预算内的执行观测，不可解读为延迟容量。请求率大于零时，还受该请求率约束。摸高要求流式测试且输出至少 2 个 token。

`PEAK_HARD_CAP=1` 时不超过计划的最大并发；最大并发确认通过后标记 `peak_capped`。设为 `0` 时允许超过该上限继续扩展，例如从 200 到 400，但仍受轮次数和总时间预算约束。

新增的控制量可通过全局参数或对应场景字段调整：

| 全局参数 | 场景字段 | 默认值及含义 |
|---|---|---|
| `PEAK_PRECISION` | `peak_precision` | `1`，边界并发差精度 |
| `PEAK_CONFIRM_RUNS` | `peak_confirm_runs` | `3`，确认阶段的新执行次数 |
| `PEAK_CONFIRM_MIN_REQUESTS` | `peak_confirm_min_requests` | `100`，确认轮次最少主测试请求数 |
| `PEAK_MAX_TRIALS` | `peak_max_trials` | `40`，单场景总尝试数预算，含复测和无效重试 |
| `PEAK_TRIAL_TIMEOUT` | `peak_trial_timeout` | `1800` 秒，单次执行超时 |
| `PEAK_TOTAL_TIMEOUT` | `peak_total_timeout` | `14400` 秒，单场景总时间预算 |
| `PEAK_INVALID_RETRIES` | `peak_invalid_retries` | `1`，无效测量的重试次数 |
| `PEAK_FLOOR_CONCURRENCY` | `peak_floor_cc` | `1`，允许下探的最低并发 |

普通探索轮次的主测试请求数由并发和 `peak_data_multiplier` 决定；确认阶段至少取 `max(并发 × 倍数, 最少请求数)`。这与原生 `repeat` 不同，后者保留其正常/前缀分支语义。无效或不完整数据只按限制重试；请求失败、容器故障和日志/证据错误不当作“延迟已越界”用于收缩区间。最少样本量和复测次数也不是统计置信度保证。

| 最终 CSV 状态 | 含义 |
|---|---|
| `peak_confirmed` | 新复测确认的通过/失败边界达到配置精度 |
| `peak_capped` | 硬上限已确认通过；尚未观察到更高处的失败边界 |
| `peak_budget` | 次数或总时间预算耗尽，边界未完成确认 |
| `peak_unstable` | 确认阶段结果发生变化或混合，无法稳定确认 |
| `peak_no_pass` | 搜索到最低并发仍无确认通过点；不推断未测点的结果 |
| `peak_request_error` | 请求失败超过允许值，不能当作延迟上限 |
| `peak_error` | 进程、基础设施、日志或证据异常 |
| `peak_cancelled` | 摸高引擎收到取消；整进程取消也可能只有批次取消状态 |

`peak_round` 是中间测量行，不计入最终场景成功数。当前版本仅 `peak_confirmed`、`peak_capped` 作为成功摸高终态；兼容读取旧结果中的 `peak`。预算、错误或不稳定等状态即使保留已确认点的数值，也不能改写成成功。没有确认通过点时，最终指标留空，观察值留在搜索轨迹。

选中结果来自已确认并发的一次实际主测试作用域，并记录试验和作用域身份；不拼接不同轮次的最优字段，不平均各轮 P90。每次测量的执行判定、轨迹和选中依据可在 `evaluation.json`、`search_trace.*`、`case_summary.json` 和 `peak_summary.json` 中追溯。

## 结果与数值口径

单份与多份配置使用同一结构：

```text
outputs/default/<批次号>/
├── batch_state.json                 # 全局阶段、原因和退出码，包括早期预检失败
├── batch_results.json               # 任务、终态、实际路径及快照入口
├── batch_results.csv                # 每任务状态与计数
├── merged_metrics.csv               # 带任务身份的各任务指标行
├── .generations/<代号>/             # 一次完整发布的不可变快照
├── logs/
│   ├── batch.log                    # 面向人的批次关键日志
│   ├── events.jsonl                 # 批次结构化事件
│   ├── archive_index.json           # 管理日志封存来源、分段顺序及原始/gzip校验值
│   ├── archive/<封存号>/            # gzip字节段，原始日志继续保留
│   └── preflight/                   # 配置加载、预检和 Docker 诊断
└── tasks/<配置名>__<模型名>/
    ├── config.redacted.json
    ├── result_index.json
    ├── <配置名>__<模型名>__all_results.csv
    ├── <配置名>__<模型名>__execution.log
    ├── <配置名>__<模型名>__launcher.log
    ├── .launcher/<运行号>/
    │   ├── runtime.json             # 完整容器 ID、运行身份、进度和结果位置
    │   ├── launcher.log
    │   ├── stdout.raw.log
    │   └── stderr.raw.log
    ├── scenario_results/<运行号>/
    │   ├── all_results.csv
    │   ├── result_scopes.json
    │   ├── execution.log
    │   ├── events.jsonl
    │   └── fixed_<轮次>/...          # evaluation、作用域、原始文件及校验清单
    └── peak_<场景和运行标识>/...       # 每轮原始结果、搜索轨迹及最终摘要
```

自定义输出目录时，批次索引仍是入口，任务原始文件可位于自定义路径。`latest.json` 及可用时的 `latest` 符号链接用于快速访问；失败或中断后不可仅凭 `latest` 判断本批次结果，应核对批次号、运行号和状态。

批次 JSON 的 `generation_dir` 和 `generation_hashes` 指向一次一致快照；需要严格一致的机器读取，应从该入口读取同一代文件。顶层 CSV 为便捷入口。合并视图增加 `task,model,endpoint,service_id,run_id,source_csv` 六个身份字段，随后保留 23 列指标；不合并不同服务的测量口径。

每任务汇总保留以下 **23 列及顺序**，按原生表头名称读取，支持相应旧/新原生列格式，不按位置盲取：

```text
scenario,current_time,input_len,output_len,total_req,max_cc,cc,rr,TTFT avg,TTFT P90,TPOT avg,TPOT SLO_P90,E2E_time,output_throughput,single_output_throughput,E2E_throughput,single_E2E_throughput,qps,qpm,input_token_throughput,prefill_token_throughput,repeat_rate,status
```

| 字段 | 解释与单位 |
|---|---|
| `scenario`, `current_time` | 展开的轮次名、原始测量的时间标识；重复轮次独立标识 |
| `input_len`, `output_len` | 原始测量得到的 token 长度；可能因 tokenizer/模板与配置目标不同 |
| `total_req` | 选定主测试作用域的请求数，不累加预热、其他尝试或其他轮次 |
| `max_cc`, `cc` | 原生测得的最大/平均并发，不是填写的目标并发；目标见场景与摸高摘要 |
| `rr` | 原生请求率字段，保留其数值口径 |
| `TTFT avg`, `TTFT P90` | 首 token 延迟，毫秒 |
| `TPOT avg`, `TPOT SLO_P90` | token 间延迟及其 P90，毫秒 |
| `E2E_time` | 整轮 Benchmark Duration，秒；不是单请求 E2EL |
| `output_throughput`, `E2E_throughput` | 全局输出/输入加输出吞吐，token/s |
| `single_output_throughput`, `single_E2E_throughput` | 对应全局吞吐除以 `NPU_NUM`，分母口径必须注明 |
| `qps`, `qpm` | 请求/s、请求/min |
| `input_token_throughput`, `prefill_token_throughput` | 原生对应吞吐，token/s；不拿不同分母的字段互相填充 |
| `repeat_rate` | 配置的前缀重复比例，不是实测缓存命中率 |
| `status` | 固定轮次执行判定、摸高中间轮次或最终状态 |

不适用的值留空，不写成零。文本模式没有流式 TTFT/TPOT；只有一个输出 token 的流式结果没有有效 TPOT；缺失的原生 prefill 吞吐占位值不作为真实吞吐。精度测试使用独立精度证据，不能把缺失性能项伪造为有效性能值。原生新增的 E2EL 等额外字段保留在原始文件中，不悄悄改变 23 列兼容接口。

固定轮次 `passed` 表示执行和适用的数据/请求完整性检查通过，不表示已经按摸高 SLO 筛选。`failed`、`no_result` 表示执行或结果失败。中间摸高行及最终摸高状态见上一节。控制台为可读性格式化，数值核验以保存的原始作用域和 CSV 为准。

## 日志排查与保留

日志在加载配置和进行预检前建立，因此配置语法错误、预检失败也应有批次目录和原因。推荐依次查看：

1. `batch_state.json`、`batch_results.json` 与 `logs/batch.log`：哪份配置、哪个阶段、为什么结束。
2. 对应任务 `__execution.log` 和 `__launcher.log`：场景、固定并发或摸高轮次及其失败原因。
3. `logs/preflight/`、`.launcher/<运行号>/stdout.raw.log`、`stderr.raw.log`：配置加载和进程原始输出。
4. 该轮 `evaluation.json`、`result_scopes.json`、原始 CSV/JSON/SQLite/NPY 和清单：核对请求数、指标、选中作用域及文件校验值。
5. 摸高 `search_trace.*` 与 `peak_summary.json`：区分 SLO 不通过、无效测量、请求错误、预算和基础设施问题。

人类关键日志带时间、级别、任务和事件；`events.jsonl` 保留序号、批次及结构化原因，任务事件由任务自身写入，不让多个进程混写一个文件。配置加载的 stdout/stderr 和任务 stdout/stderr 分别保真保存；人类日志另行解码、脱敏及压缩重复进度，避免把完整私有输出直接打印到终端。

日志和原始证据属于私有运行材料：可能含真实 IP、模型路径、请求内容或输出的凭据。脱敏配置及人类日志也不等于可以直接公开；分享前单独审查。源码仓示例只用 `192.168.1.x` 地址，不提交真实配置和输出。

任务日志读写或原始流保存失败会传给监督器并停止受影响负载，公共批次日志失败停止整个批次；不能一边丢日志一边报告成功。正常日志位置无法创建时，会尝试建立应急日志并打印位置；全部持久位置都不可用时，不启动压测。关键身份及状态事件落盘，关闭也检查写入错误。强杀或掉电仍可能丢失尚未刷盘的尾部，遗留 `running` 状态不等于成功。

容器退出时，先从 `runtime.json` 取故障时的**完整 ID**，再只读核查，例如：

```bash
docker inspect '<原完整容器ID>' --format 'ID={{.Id}} OOM={{.State.OOMKilled}} Exit={{.State.ExitCode}} Started={{.State.StartedAt}} Finished={{.State.FinishedAt}} Restarts={{.RestartCount}}'
```

退出码 137 本身不能证明 OOM；还需对应容器状态及宿主机证据。当前同名容器显示 `Up`，也不能证明故障时是同一容器。保留原 ID、时间线和原始错误，不把一次 `docker exec` 错误中的容器 ID 无证据归给更早的失败轮次。

执行阶段结束、日志流关闭后，现有日志模块把批次/预检及任务管理日志按最多 16 MiB 的**原始字节段**额外封存为 gzip，不按行或字符切分。`logs/archive_index.json` 记录每个原文件的字节数/SHA256、各段顺序/偏移、原始段及 gzip 的字节数/SHA256；按顺序解压拼接可还原坏编码、NUL、CR 和没有换行的长输出。索引仅在完整封存后提交；进行中或失败的目录保留 `archive_state.json`。发生写入错误或源日志仍在变化时不报告完整封存，批次以非零结束，原始文件保留。

`batch_workload_finished` 表示执行阶段已结束、日志等待封存；最终结论以 `batch_state.json` 的终态、退出码和 `log_archive` 为准，终端会在封存后再次打印批次结论。封存范围是管理日志；各测量作用域里的模型原始产物继续按其独立清单归档，不混入日志索引。

运行中不自动删除、截断原始日志或清理其他批次。由于原始文件保留，额外 gzip 需要额外磁盘空间；封存不会减少当前 raw 占用。容量应在启动前规划并监控，长场景、复测和重复测试会增加体积。批次结束并核验后，可按完整批次目录归档，连同自定义任务目录、索引指向的文件、日志封存、原始校验清单和 `.generations` 一起保留。确认没有运行负载、没有待处理隔离状态且留存策略允许后，再人工清理；不要只删最大原始文件造成索引失效。

## 验证与发布

以下命令从仓库根目录在 Linux 上执行；完整离线门禁不接受跳过必需测试：

```bash
bash automation/tools/check_all.sh
```

需要逐项运行时：

```bash
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD/automation/core:$PWD/automation/tools${PYTHONPATH:+:$PYTHONPATH}"
python3 -B automation/tools/validate_publication.py --root "$PWD"
bash -n run_perf_scenarios.sh
python3 -B -m unittest discover -s automation/tests -p 'test_*.py'
```

`validate_publication.py` 检查公开文件、敏感内容、原生清单/模式和禁止的原生新增文件。`check_all.sh` 还在内存编译全部自动化 Python 文件、检查入口 Bash 语法，并且只运行一次完整 unittest。参数、场景、CSV 和写盘合同集中在现有测试内，不另设重复自测工具。测试覆盖正常、边界和注入故障，但本地/模拟通过不能代替真实 Docker 和模型验收。

正式提交完成后，还应检查将要发布的确切 Git 树和工作区，避免只检查工作区却发布另一份内容：

```bash
python3 -B automation/tools/validate_publication.py --root "$PWD" --ref HEAD --require-clean
```

对一次实际任务，用 `result_index.json` 中记录的真实目录运行；下面的批次号和运行号需替换：

```bash
BATCH_DIR="outputs/default/<批次号>"
TASK_OUTPUT="$BATCH_DIR/tasks/test_a__your-model-a"
RESULT_DIR="$TASK_OUTPUT/scenario_results/<运行号>"
python3 -B automation/core/aisbench_result_validation.py \
  --result-dir "$RESULT_DIR" --host-output-dir "$TASK_OUTPUT" --require-scopes
```

这个统一入口检查结果结构、适用字段、作用域、请求计数、原始清单与最终选中证据；可用 `--expected-statuses` 指定完整 CSV 的按行状态列表（包括中间 `peak_round`）。失败终态不是“校验通过的性能结果”，但失败证据仍应完整。验证器和实现共享部分合同，正式数值验收还应从真实原始 JSON/SQLite/NPY 独立复算，逐项对照 CSV，不能只用程序自校验宣称数值全部正确。

发布验收至少要形成这些证据：

- 目标镜像及实际服务的小规模固定测试、重复并发、文本/流式与适用字段、正常/前缀/精度等保留接口。
- 两个及更多独立服务并行，分别生成任务 CSV/日志；同服务、同容器、重叠目录及跨批次冲突被阻止。
- 摸高扩展、下探、二分、硬上限开/关、精度、重新确认、不稳定、无效重试、请求失败、超时/预算和取消，轨迹与终态一致。
- 所有 23 列按实际原始证据核对，重复测试作用域、归一化分母、选中轮次及空值适用性正确，归档损坏能够检出。
- 配置加载失败、启动异常、容器丢失/替换、日志及磁盘错误、终端断开、取消和停止失败均有明确状态及证据，不碰其他服务。
- 发布树、原生文件字节和模式、公开隐私扫描、离线门禁及实机证据使用同一候选版本；未覆盖项明确列出。

只有完成实际验收后，才能把候选版改为正式发布并创建 `v2026.10.19` 标签。README 中的示例、测试名称或命令不是验收通过记录。

## 更新原生仓

当前锁定 [rayn-zzz/aisbench_auto_tools_prefix 提交 ac06bd6](https://github.com/rayn-zzz/aisbench_auto_tools_prefix/commit/ac06bd619726663ac8c7b0c84c9b3cdbb041b062)。本仓通过 subtree 关联原生历史，维护时选定具体新提交，再同步整个原生子树；运行时不自动跟随浮动分支。

先确认工作区干净、处于升级维护分支，按需添加上游 remote（已有则省略第一条）：

```bash
git remote add upstream https://github.com/rayn-zzz/aisbench_auto_tools_prefix.git
git fetch upstream main
TARGET_UPSTREAM="$(git rev-parse upstream/main)"
git log --oneline ac06bd619726663ac8c7b0c84c9b3cdbb041b062.."$TARGET_UPSTREAM"
git subtree merge --prefix=aisbench_auto_tools_prefix "$TARGET_UPSTREAM"
git diff --exit-code "$TARGET_UPSTREAM^{tree}" HEAD:aisbench_auto_tools_prefix
```

使用完整历史，不添加 `--squash`；命令含义见 [Git subtree 官方文档](https://github.com/git/git/blob/master/contrib/subtree/git-subtree.adoc)。合并后原生目录应与选定提交的整棵树一致，包含文件内容和模式。出现冲突时先查明来源；不要把自动化兼容修改留在原生目录。

随后独立核对新上游，更新 `automation/metadata/upstream_manifest.json` 及发布验证器锁定的提交、清单校验值，审查原生配置/CLI/结果接口变化，在 `core/` 适配并补测试。清单更新只是记录信任基线，不能替代字节/模式比对；最后重新执行离线、真实镜像和数值验收，保留对应上游提交号及证据。
