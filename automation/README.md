# AISBench 自动化使用与维护

本说明对应 v2026.10.25，介绍配置、运行、结果判读和维护方法。日常启动入口和默认配置均在仓库根目录：`run_benchmark.sh`、`config.env`。说明中的示例和检查命令不代表对应环境已经完成验收。

## 目录与修改位置

| 位置 | 职责及修改边界 |
|---|---|
| 根目录 `config.env` | 服务、镜像、路径和场景；日常只改这里即可 |
| 根目录 `run_benchmark.sh` | 定位安装目录、检查 Python、启动管理器 |
| `aisbench_auto_tools_prefix/` | 选定上游的原生源码；不加入增补代码，不产生运行文件 |
| `automation/core/aisbench_run_manager.py` | 加载及冻结配置、全部任务预检、并行调度、互斥、取消、容器身份监督 |
| `automation/core/aisbench_configuration.py` | 公开参数默认值、参数校验、场景展开和执行计划 |
| `automation/core/aisbench_worker.py` | 单任务容器准备、原生源码副本配置、固定轮次与摸高调用 |
| `automation/core/aisbench_execution.py` | 一次实际执行及请求/结果作用域判定 |
| `automation/core/aisbench_perf_matrix.py` | 摸高搜索、复测、预算及终态 |
| `automation/core/aisbench_perf_matrix_config.py` | 正常摸高和独立摸高入口共用的默认配置接口 |
| `automation/core/aisbench_result_adapter.py` | 按表头读取原生结果、23 列投影、单位及适用性校验 |
| `automation/core/aisbench_result_validation.py` | 运行时及离线共用的作用域、原始来源、完整性和结果检查，也可单独执行 |
| `automation/core/aisbench_artifact_archive.py` | 保存原始证据、作用域、清单与校验值 |
| `automation/core/aisbench_batch_results.py` | 批次/任务索引、合并视图及一致快照发布 |
| `automation/core/aisbench_logging.py` | 人类日志、结构化事件、脱敏及写盘错误传播 |
| `automation/examples/` | 两份可改名的小规模完整配置 |
| `outputs/` | 运行日志、原始结果和验证报告，不作为公开源码提交 |

开发回归测试、发布检查工具、原生版本核对记录和 CI 配置由维护者在本地保存，不随运行版本发布。`core/` 中的结果校验、归档和日志模块属于正常运行流程，继续保留。

新增参数时同步修改参数定义、使用它的执行模块、配置末尾参考和本地回归测试；新增执行行为或算法时修改负责该行为的模块。无需为了修改输入长度、并发、服务地址而改 Python。原生接口发生变化时，兼容逻辑继续放在 `core/`。

## 运行条件与启动

需要 Linux、Bash、Python 3.7+、Docker 和常用 Linux 工具。默认镜像为：

```text
ghcr.io/aisbench/aisbench_benchmark:v3.1-20261013-master-openeuler24.03-py311
```

宿主机架构须匹配镜像；宿主机模型路径应可读，镜像内 tokenizer 应能加载。被测模型服务应已就绪，且能由使用宿主机网络的 AISBench 容器访问。需要缓存指标时，相关 `/metrics` 地址也须可访问。

在仓库根目录运行，无需先 `source`：

```bash
vi config.env
bash run_benchmark.sh ./config.env
```

默认配置包括两组 64K 输入场景、固定并发和摸高。只验证流程时先改小规模示例中的模型和地址：

```bash
vi automation/examples/test_a.env
bash run_benchmark.sh automation/examples/test_a.env
```

该示例为输入 1024、输出 512、固定并发 4，默认主测试请求数为 16，不摸高。原生数据准备、预热或前缀准备不应与主测试请求数混为一谈。

N 份完整配置可一次启动，文件名不写死：

```bash
bash run_benchmark.sh automation/examples/test_a.env automation/examples/test_b.env
```

也可使用自己的文件名，例如 `./prefill.env`、`./decode.local.env`；路径含空格时加引号。任务名取文件名，去掉 `.env` 或 `.local.env` 后，须以英文字母或数字开始，只包含英文字母、数字、点、下划线和短横线。

每份配置启动一个专用 AISBench 客户端容器；默认名为 `aisbench-<任务名>-<批次号>`。模型服务由用户管理。各任务并行，单个任务中的测试轮次顺序执行。容器名与输出标签中的过长或特殊名称会截短并附校验后缀，索引保留原始名称。

## 配置加载与优先级

一份配置包含全局参数和一个 `SCENARIOS_JSON` 块。全局参数由 Bash 加载一次；场景 JSON 由工具从同一份冻结配置中直接读取，不需要导出为环境变量。配置文件会执行其中的 Shell 语句，只运行自己信任的文件。

| 启动方式 | 全局参数来源 | 场景来源 |
|---|---|---|
| `bash run_benchmark.sh ./config.env` | 指定文件；受支持全局参数不继承调用者的同名值 | 指定文件的 JSON 块 |
| `bash run_benchmark.sh ./a.env ./b.env` | 每份文件独立加载，彼此隔离 | 每份文件自己的 JSON 块 |
| `bash run_benchmark.sh` | 先自动加载根目录 `config.env`，再按“是否已导出”保留父进程的受支持全局覆盖值 | 根目录 `config.env` 的 JSON 块 |

零参数模式兼容已有 `export` 用法，例如父进程已导出的 `PEAK_HARD_CAP=0` 会覆盖文件中的 `1`。空 `API_KEY` 也按存在性保留；随后各参数的空值校验和默认回退仍按参数类型处理。推荐显式传文件，避免旧 shell 中的值影响复现。

未提供的参数使用代码默认值；场景中的对应字段覆盖该场景的全局默认。每份配置的内容及 SHA 在启动时冻结，运行中编辑文件只影响下一批次。配置相对路径和用户填写的相对宿主机路径按启动时工作目录解析；配置会从冻结副本加载，不应依靠 `BASH_SOURCE` 在临时位置查找相邻文件。

`config.env` 末尾附有**仅作注释、不会执行**的完整参数参考，覆盖 32 个公开全局参数、场景 JSON 字段及独立摸高 CLI；两个示例末尾也保留参数注释参考。每项说明默认值、单位、作用域、覆盖、空值和示例。应区分：

- **代码默认值**：未配置时采用，例如 `PEAK_HARD_CAP=0`、`MAX_CONTEXT_LEN=512000`。
- **本文件有效赋值**：公开默认文件明确设置 `PEAK_HARD_CAP=1`、`MAX_CONTEXT_LEN=133120`，场景设置 TTFT 15 秒、TPOT 60 毫秒。
- **实际运行值**：以本批次的脱敏配置快照和计划为准；编辑配置后，末尾参考中的示例不会自动变化。

固定轮次未指定 `data_num` 时，使用 `配置并发 × DATA_NUM_MULTIPLIER`，该全局倍数默认 `4`。重复并发合法，如 `[1, 10, 10, 20, "max"]`；重复的固定轮次使用独立名称和归档，不覆盖前一轮。`"max"` 表示增加一次摸高，不是一个固定并发数。

上述请求数公式适用于生成数据。显式设置 `dataset` 时，文件必须在容器内可读；原生使用文件的全部记录，`data_num` 不会截取或补齐该文件。请先确认实际记录数，再安排负载和预算。

`test_accuracy=true` 使用 GSM8K 精度流程，只支持固定并发，不能与 `"max"` 组合。未指定 `dataset` 时，工具从锁定原生仓的 `GSM8K.jsonl` 选择 `data_num` 条真实问题和金标，保留来源行号及校验值；不会按 `input_len` 把问题合成为长输入，配置层的上下文门禁仍会执行。显式数据集使用全部记录，须包含非空 `question` 和带 `#### ` 金标的 `answer`，格式错误在发请求前报错。

精度结果保留实际预测、金标及逐题判定，见该轮 `accuracy_dataset.json`、`evaluation.json` 的精度作用域和 `scope_*/artifacts/experiment/`。模型答错或准确率低不表示工具失败；工具检查评分提取、逐题判定和分母是否一致，不设置模型准确率合格线。精度测试的性能字段留空，不能把准确率、缺失项或原生性能占位值填入性能 CSV。

当前默认镜像的精度命令使用原生默认的 1 次首题预热，再评分全部记录；预热不进入评分分母。性能命令显式设置 `--num-warmups 0`。例如同一份 4 条显式数据分别做性能和精度测试，实际为 4 条性能请求、1 条精度预热和 4 条评分请求，共 9 次 HTTP 请求，正式测量/评分样本为 4+4。其他镜像的预热行为应按实际命令和日志核对。

固定轮次的全局 `PEAK_TRIAL_TIMEOUT` 同样控制一次完整调用的超时，包含前缀准备和该次原生 `repeat`；只有摸高可以用场景字段 `peak_trial_timeout` 覆盖它。`FAILED_REQ_LIMIT` 按每个性能测量作用域检查，也包含前缀准备；精度测试使用独立评分证据。

`FAILED_REQ_LIMIT` 是允许的失败逻辑请求数，不是比例。大于 0 时，未超限且作用域完整、计数可核对才可能通过；`total_req` 仍包含成功和失败。当前默认镜像的 `default_perf/total` 不把失败请求加入时序和 token 数组，时长、`qps` 及吞吐按成功样本窗口计算。超限、零成功或缺少完整可信证据时不发布有效测量；其他镜像或统计模式须重新核对。

`enable_think=true` 按原生方式发送 `enable_thinking=true`；`false` 省略该请求字段，不保证模型关闭思考，需按实际模型响应核验。

锁定上游的已知 generation 模板与原生替换器不匹配时，工具只对每轮隔离的 `native_runtime/` 副本恢复替换位置，宿主机原生文件保持不变。`generation_template.json` 记录原始/适配后 SHA、动作和请求参数；已有合法占位模板保持原样，未知模板在发请求前拒绝并保存诊断。

`API_URL` 是**服务基址**，非空时优先于 `HOST_IP/HOST_PORT`。当前默认镜像先为基址路径补齐尾斜杠，再用 `urljoin` 追加 `v1/chat/completions`：

| 配置中的 `API_URL` | 实际聊天请求地址 |
|---|---|
| `http://192.168.1.10:8000/` | `http://192.168.1.10:8000/v1/chat/completions` |
| `http://192.168.1.10:8000/gateway` 或末尾带 `/` | `http://192.168.1.10:8000/gateway/v1/chat/completions` |

基址中不要重复写客户端负责追加的 `v1`。例如填入 `http://192.168.1.10:8000/v1/chat/completions`，会变成 `.../v1/chat/completions/v1/chat/completions`。留空则使用 `http://HOST_IP:HOST_PORT/`。这里描述的是默认镜像当前客户端合同；更换镜像时应重新核对实际地址拼接行为及网关路由。

场景 `seed` 默认 `1`，传给原生长度采样和前缀构造中的随机逻辑。原生 normal 分支的 `DataPicker` 选题仍使用未被该参数完整控制的全局随机状态，因此相同 seed **不保证删除缓存后重新生成的整份 normal 文本逐字节相同**。前缀生成也不应仅凭相同 seed 宣称整份输入字节一致；严格复现应保留实际生成的数据文件、参数和校验值。

自动化按有效输入参数建立数据缓存 profile：包括 seed、输入长度/长度分布、数据量、数据类型、前缀参数、tokenizer 和原生生成代码身份。参数变化会进入不同 profile，避免复用旧种子或旧分布的缓存；相同 profile 保留原生缓存复用方式。`dataset_profile.json` 记录指纹及参数，可用于核查；目录已存在不等于本轮数据文件必定命中缓存，profile 隔离也不意味着原生选题过程已完全确定化。显式外部 `dataset` 使用所提供文件，不套用生成数据的缓存命名空间。

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

管理器在启动 worker 前持久保存活动资源记录（active lease），包含批次/运行身份、服务/容器/输出资源、管理进程身份和运行报告位置；后续将可信运行报告中的完整容器 ID 写入记录。因此管理器或 worker 被 `SIGKILL` 强杀、内核文件锁随进程退出释放时，仍不能据此认为容器内负载结束。

后续启动先在资源锁保护下核对旧活动记录：原管理进程仍存活时拒绝冲突任务；原管理进程已失踪时，只有以下任一证据可信，才解除对应活动隔离：

- 原运行报告与 run ID、容器名、镜像匹配，明确 `complete=true` 且含整数 `worker_exit`、64 位完整容器 ID，完整 ID 还须与活动记录已知 ID 一致。这里确认的是 worker 已结束，退出码非零仍可满足结束证据，不代表压测成功。
- 在记录的同一 Docker daemon 上，只读检查原固定完整 ID，确认它已不存在；或确认该原容器的名称、镜像、管理标签匹配且 `Running=false`。

ID 未知、身份不符、原容器仍运行、Docker 不可达或记录损坏时均保留隔离；不会按当前同名容器猜测原 ID，也不自动停止身份未知的负载。已进入清理失败的隔离记录还须按原固定 ID 的停止/不存在证据解除。已知镜像不存在、当前新容器名被他人占用等启动错误会在提交活动记录前检查；该名称检查不能用来清除旧活动记录。

恢复检查**不会补跑或自动完成旧批次**。旧批次的 `batch_state.json` 更新为 `interrupted`、`reason_code=MANAGER_LOST`、`exit_code=null`，保留原 `finished_at`，并追加恢复观察时间和原因；旧 CSV、原始日志及 `.generations` 不可变快照不被改写。因此旧 `batch_results.json` 或不可变代仍可能显示强杀前的 `running`，这是旧快照，不能当作当前进程仍运行或已成功的证明。先读 `batch_state.json` 和 `recovery_observations`，再核对记录中的运行报告及原完整 ID；不要删锁/活动记录、改名或换目录绕过隔离。

## 摸高算法与终态

摸高的目标是在本场景、请求率、指标口径和预算下，寻找满足所选延迟 SLO 的已验证并发边界。它不代表模型在其他输入、输出、缓存或流量条件下的通用容量。

给定初始整数区间 `peak_min_cc..peak_max_cc`，先测中点 `(min + max) // 2`；通过后在更高半区间继续，不满足 SLO 时在更低半区间继续。搜索从这个区间开始，不预先倍增，也不把尚未测量的初始上界当作失败点。只有实际有效测量的 PASS 和 SLO_FAIL 才能作为通过/失败边界。

`peak_min_cc` 是初始区间的软下界，不是固定的首个测量点。初始区间内未找到通过点，或临界候选确认失败后仍需向下寻找稳定点时，允许继续下探到 `peak_floor_cc`；`floor` 才是不可越过的硬底。

TTFT 基础阈值用**秒**，TPOT 基础阈值用**毫秒**，从 CSV 的毫秒值换算后比较。可选 `PEAK_SLO_TOLERANCE` 默认 `0.0`，场景 `peak_slo_tolerance` 优先；独立 CLI 为 `--slo-tolerance`，未指定时继承所选 case。容差是有限的 `0..1` 比例，`0.05` 表示 5%，默认关闭，需要用户显式启用。

每个已启用的 SLO 项按 `有效上限 = 基础阈值 × (1 + 容差)` 判断，基准是配置的阈值，不是某次测量中的较好结果。例如基础 TTFT 15 秒、TPOT 60 毫秒，容差 5% 时有效上限分别为 15.75 秒、63 毫秒。对一轮有效测量区分：

- **严格通过**：所有已启用项的实际值均不超过各自基础阈值。
- **容差通过**：至少一项高于基础阈值，但所有已启用项均不超过各自有效上限。
- **超限**：任一已启用项的实际值高于其有效上限。

等于基础阈值归严格通过，等于容差上限仍可通过。容差不会修改原始测量值；原始值、基础阈值、容差比例、有效上限及分类保存在元数据和搜索轨迹中，最终 23 列指标 CSV 不增加列。请求失败、缺失或无效数据、日志/证据错误和基础设施异常仍按原有规则处理，不能作为容差通过；容差也不放宽样本量、独立确认次数或搜索预算。

某基础阈值不大于零时关闭该项 SLO；两项都关闭时，结果仅表示预算内的执行观测，不可解读为延迟容量。请求率大于零时，还受该请求率约束。摸高要求流式测试且输出至少 2 个 token。

`PEAK_HARD_CAP=1` 时不超过计划的最大并发；最大并发确认通过后标记 `peak_capped`。设为 `0` 时，初始最大并发实测 PASS 后建立新窗口 `(max, 2×max]`，先测新窗口的整数中点，再按测量结果二分；例如 200 通过后在 `201..400` 内先测 300，不直接测 400。只有当前窗口上界也实测通过后才继续扩大窗口，整个过程仍受轮次数、总时间预算及既有扩容保护上限约束。

新增的控制量可通过全局参数或对应场景字段调整：

| 全局参数 | 场景字段 | 默认值及含义 |
|---|---|---|
| `PEAK_SLO_TOLERANCE` | `peak_slo_tolerance` | `0.0`，相对基础阈值的有限 `0..1` 比例，默认关闭；`0.05` 表示 5% |
| `PEAK_PRECISION` | `peak_precision` | `1`，边界并发差精度 |
| `PEAK_CONFIRM_RUNS` | `peak_confirm_runs` | `3`，每个确认并发点的新执行次数；通过/失败两端各执行 3 次 |
| `PEAK_CONFIRM_MIN_REQUESTS` | `peak_confirm_min_requests` | `100`，确认轮次最少主测试请求数 |
| `PEAK_MAX_TRIALS` | `peak_max_trials` | `40`，单场景总尝试数预算，含复测和无效重试 |
| `PEAK_TRIAL_TIMEOUT` | `peak_trial_timeout` | `1800` 秒，单次执行超时 |
| `PEAK_TOTAL_TIMEOUT` | `peak_total_timeout` | `14400` 秒，单场景总时间预算 |
| `PEAK_INVALID_RETRIES` | `peak_invalid_retries` | `1`，无效测量的重试次数 |
| `PEAK_FLOOR_CONCURRENCY` | `peak_floor_cc` | `1`，允许下探的最低并发 |

使用生成数据时，普通探索轮次的主测试请求数由并发和 `peak_data_multiplier` 决定；确认阶段至少取 `max(并发 × 倍数, 最少请求数)`。显式 `dataset` 保留全量记录，确认门禁检查每个主测作用域实际完成的请求数；低于配置下限时测量无效，不会补造记录。这与原生 `repeat` 不同。无效或不完整数据只按限制重试；请求失败、容器故障和日志/证据错误不当作“延迟已越界”用于收缩区间。最少样本量和复测次数也不是统计置信度保证。

临界候选使用独立的新执行进行确认，不能用探索阶段的缓存测量充数。若候选的确认出现 `SLO_FAIL`，继续检查更低并发能否通过同样的确认要求，而不是立即丢弃全部结果。例如并发 35 的确认混合了通过和 SLO 不通过，而并发 34 随后的 3 次独立确认全部通过，可以保留 34 的某次真实主测作用域和数值；若 35 等更高点仍波动，最终仍是 `peak_unstable`，提示“已复测稳定 34，上界仍波动，最高边界未确认”。只有新的通过端和失败端证据足以确认配置精度内的边界时，才能返回 `peak_confirmed`。

| 最终 CSV 状态 | 含义 |
|---|---|
| `peak_confirmed` | 新复测确认的通过/失败边界达到配置精度 |
| `peak_capped` | 硬上限已确认通过；尚未观察到更高处的失败边界 |
| `peak_budget` | 次数或总时间预算耗尽，边界未完成确认 |
| `peak_unstable` | 通过/失败边界仍有波动；可保留更低处已独立复测通过的真实测量，不能据此宣称最高边界已确认 |
| `peak_no_pass` | 搜索到最低并发仍无确认通过点；不推断未测点的结果 |
| `peak_request_error` | 请求失败超过允许值，不能当作延迟上限 |
| `peak_error` | 进程、基础设施、日志或证据异常 |
| `peak_cancelled` | 摸高引擎收到取消；整进程取消也可能只有批次取消状态 |

`peak_round` 是中间测量行，不计入最终场景成功数。当前版本仅 `peak_confirmed`、`peak_capped` 作为成功摸高终态；兼容读取旧结果中的 `peak`。预算、错误或不稳定等状态即使保留已确认点的数值，也不能改写成成功。没有确认通过点时，最终指标留空，观察值留在搜索轨迹。

选中结果来自已确认并发的一次实际主测试作用域，并记录试验和作用域身份；边界不稳定时保留的较低稳定点也遵守此规则。不拼接不同轮次的最优字段，不平均各轮 P90；保留稳定下界不改变预算、请求错误、执行故障或不稳定的真实终态。每次测量的执行判定、轨迹和选中依据可在 `evaluation.json`、`search_trace.*`、`case_summary.json` 和 `peak_summary.json` 中追溯。

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
    │   ├── fixed_<轮次>/...          # evaluation、作用域、原始文件及校验清单
    │   └── process_logs/call_<标识>/ # Docker 调用原始 stdout.raw.log / stderr.raw.log
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
| `scenario`, `current_time` | 展开的轮次名、原生解析汇总时的时间标识；后者不是请求开始时间；重复轮次独立标识 |
| `input_len`, `output_len` | 选定主测试作用域内成功请求的平均输入/输出 token 数；不是配置的合成输入长度或最大输出上限 |
| `total_req` | 选定主测试作用域的请求数，不累加预热、其他尝试或其他轮次 |
| `max_cc` | 该作用域归档模型配置的 `batch_size`，即配置目标并发；不是实测在途请求峰值 |
| `cc` | 原生统计窗口内的平均在途并发；当前默认 total 口径为成功请求 E2EL 累计时长除以统计时长 |
| `rr` | 该作用域配置的 `request_rate`，请求/s；0 表示不限制发送速率，不是实测请求率 |
| `TTFT avg`, `TTFT P90` | 首 token 延迟，毫秒 |
| `TPOT avg`, `TPOT SLO_P90` | 逐请求 TPOT 的均值/P90，毫秒；TPOT 为首 token 后耗时除以剩余输出 token 数，不是把所有响应 chunk 间隔混合求分位数 |
| `E2E_time` | 整轮 Benchmark Duration，秒；不是单请求 E2EL |
| `output_throughput`, `E2E_throughput` | 全局输出/输入加输出吞吐，token/s |
| `single_output_throughput`, `single_E2E_throughput` | 对应全局吞吐除以 `NPU_NUM`，分母口径必须注明 |
| `qps`, `qpm` | 成功请求数除以统计时长所得请求/s；`qpm` 为原生已舍入的 `qps × 60` |
| `input_token_throughput`, `prefill_token_throughput` | 原生对应吞吐，token/s；不拿不同分母的字段互相填充 |
| `repeat_rate` | 配置的前缀重复比例，不是实测缓存命中率 |
| `status` | 固定轮次执行判定、摸高中间轮次或最终状态 |

不适用的值留空，不写成零。文本模式没有流式 TTFT/TPOT；流式测试中，每条请求都没有首 token 之后的输出 token 时，TPOT 不适用。平均输出长度为 1 不能单独判定 TPOT 不适用，例如 0 和 2 token 请求混合时仍可能存在有效 TPOT，脚本会保留原始有效数值；只有平均输出不超过 1 且原始 TPOT 缺失或为占位值时才留空。缺失的原生 prefill 吞吐占位值不作为真实吞吐。精度测试使用独立精度证据，不能把缺失性能项伪造为有效性能值。原生新增的 E2EL 等额外字段保留在原始文件中，不悄悄改变 23 列兼容接口。

没有可信的选中测量作用域时，最终行的 `input_len`、`output_len` 及其他测量字段留空，不能用配置长度、最大输出上限、上一轮或未确认的临时值补齐。配置输入/输出长度仍保留在执行计划及摸高元数据中，与实际平均 token 数分开。

当前默认镜像的 total 统计时长从成功请求的最早开始到最晚结束；吞吐和 `cc` 使用其未显示舍入的时长，界面/CSV 的时长值已按原生规则舍入。P90 是逐请求指标分布的线性分位数，不能用各轮 P90 求平均来代替。若采用其他原生统计窗口，应按该作用域原始定义核验，不能套用 total 的分母。token 数来源是实际原始记录中的服务 usage/原生 tokenizer 计数；聚合核对不能独立证明服务 usage 或 tokenizer 绝对正确。

原生 normal 分支按 `repeat` 重复执行，每次 main 作用域分别保留；固定汇总选择该次执行最后一个 main，摸高选中行还绑定确认试验及其具体 main。当前原生 prefix_cache 分支忽略 `repeat`：`prefix_test=true` 只执行一次前缀准备和一次主测，`false` 只执行一次主测。例如 `repeat=2`、主测 16 条、`dp=1`、`prefix_num=2` 且启用前缀准备时，实际是准备 2 条加主测 16 条，不是两轮主测。

汇总的 `total_req`、时长与吞吐来自选中的单个 main 作用域，不是所有 repeat 的总计。前缀准备作用域独立保留，不计入主测汇总。`repeat_rate` 是配置的重复比例，不是实测缓存命中率。启用前缀准备时，原生额外从 `POD_INFO` 读取缓存计数；计算要求测试前后 normal/external 的 queries/hits 字典齐全且引擎集合一致，否则跳过并记录警告。计数增量为 `0/0` 时原生显示 `0.00%`，实际比率未定义；不能将该显示当作测得的零命中率，也不能用一次 `/metrics` HTTP 200 证明计数及比率正确。

固定轮次 `passed` 表示执行和适用的数据/请求完整性检查通过，不表示已经按摸高 SLO 筛选。`failed`、`no_result` 表示执行或结果失败。中间摸高行及最终摸高状态见上一节。控制台为可读性格式化，数值核验以保存的原始作用域和 CSV 为准。

## 日志排查与保留

日志在加载配置和进行预检前建立，因此配置语法错误、预检失败也应有批次目录和原因。推荐依次查看：

1. `batch_state.json`、`batch_results.json` 与 `logs/batch.log`：哪份配置、哪个阶段、为什么结束。
2. 对应任务 `__execution.log` 和 `__launcher.log`：场景、固定并发或摸高轮次及其失败原因。
3. `logs/preflight/`、`.launcher/<运行号>/stdout.raw.log`、`stderr.raw.log`：配置加载和进程原始输出。
4. 该轮 `evaluation.json`、`result_scopes.json`、原始 CSV/JSON/SQLite/NPY 和清单：核对请求数、指标、选中作用域及文件校验值。
5. 摸高 `search_trace.*` 与 `peak_summary.json`：区分 SLO 不通过、无效测量、请求错误、预算和基础设施问题。

人类关键日志带时间、级别、任务和事件；`events.jsonl` 保留序号、批次及结构化原因，任务事件由任务自身写入，不让多个进程混写一个文件。配置加载、任务和各原生测量作用域的 stdout/stderr 分别按原始字节保存；终端实时透传执行输出并带任务身份前缀，不等待整轮测量结束才打印。摸高判定日志同时显示实际指标、基础阈值、容差上限、判定分类和后续动作；`peak_unstable` 的 WARNING 明确区分已复测稳定的下界与仍波动的更高边界。原始保存与终端显示独立，终端断开不影响原始日志落盘；终端显示经过解码和任务标记，不代替原始字节证据。

每次 Docker 调用的原始流保存在 `process_logs/call_<标识>/`，原生各作用域的真实原始流仍在对应轮次下的 `scope_*/stdout.raw.log`、`scope_*/stderr.raw.log`。先按 `evaluation.json`、`result_scopes.json` 或试验报告记录的路径定位作用域，不把批次管理日志、终端转发副本和某次模型请求的原始结果混为同一个来源。

日志和原始证据属于私有运行材料：可能含真实 IP、模型路径、请求内容或输出的凭据。脱敏配置及人类日志也不等于可以直接公开；分享前单独审查。源码仓示例只用 `192.168.1.x` 地址，不提交真实配置和输出。

任务日志读写或原始流保存失败会传给监督器并停止受影响负载，公共批次日志失败停止整个批次；不能一边丢日志一边报告成功。正常日志位置无法创建时，会尝试建立应急日志并打印位置；全部持久位置都不可用时，不启动压测。关键身份及状态事件落盘，关闭也检查写入错误。强杀或掉电仍可能丢失尚未刷盘的尾部，遗留 `running` 状态不等于成功；管理器失踪后的活动记录恢复规则及旧不可变快照的边界见 [服务隔离、容器与取消](#服务隔离容器与取消)。

容器退出时，先从 `runtime.json` 取故障时的**完整 ID**，再只读核查，例如：

```bash
docker inspect '<原完整容器ID>' --format 'ID={{.Id}} OOM={{.State.OOMKilled}} Exit={{.State.ExitCode}} Started={{.State.StartedAt}} Finished={{.State.FinishedAt}} Restarts={{.RestartCount}}'
```

退出码 137 本身不能证明 OOM；还需对应容器状态及宿主机证据。当前同名容器显示 `Up`，也不能证明故障时是同一容器。保留原 ID、时间线和原始错误，不把一次 `docker exec` 错误中的容器 ID 无证据归给更早的失败轮次。

执行阶段结束、日志流关闭后，现有日志模块把批次/预检及任务管理日志按最多 16 MiB 的**原始字节段**额外封存为 gzip，不按行或字符切分。`logs/archive_index.json` 记录每个原文件的字节数/SHA256、各段顺序/偏移、原始段及 gzip 的字节数/SHA256；按顺序解压拼接可还原坏编码、NUL、CR 和没有换行的长输出。索引仅在完整封存后提交；进行中或失败的目录保留 `archive_state.json`。发生写入错误或源日志仍在变化时不报告完整封存，批次以非零结束，原始文件保留。

`batch_workload_finished` 表示执行阶段已结束、日志等待封存；最终结论以 `batch_state.json` 的终态、退出码和 `log_archive` 为准，终端会在封存后再次打印批次结论。封存范围是管理日志；各测量作用域里的模型原始产物继续按其独立清单归档，不混入日志索引。

运行中不自动删除、截断原始日志或清理其他批次。由于原始文件保留，额外 gzip 需要额外磁盘空间；封存不会减少当前 raw 占用。容量应在启动前规划并监控，长场景、复测和重复测试会增加体积。批次结束并核验后，可按完整批次目录归档，连同自定义任务目录、索引指向的文件、日志封存、原始校验清单和 `.generations` 一起保留。确认没有运行负载、没有待处理隔离状态且留存策略允许后，再人工清理；不要只删最大原始文件造成索引失效。

## 验证与发布

任务汇总时会自动检查结果及原始来源。需要单独复核已有结果时，可使用运行模块中的同一入口。从仓库根目录执行，将下列批次号、任务名和运行号替换为 `result_index.json` 中的实际路径：

```bash
BATCH_DIR="outputs/default/<批次号>"
TASK_OUTPUT="$BATCH_DIR/tasks/test_a__your-model-a"
RESULT_DIR="$TASK_OUTPUT/scenario_results/<运行号>"
python3 -B automation/core/aisbench_result_validation.py \
  --result-dir "$RESULT_DIR" --host-output-dir "$TASK_OUTPUT" --require-scopes
```

这个入口检查结果结构、适用字段、作用域、请求计数、原始清单与最终选中证据；可用 `--expected-statuses` 指定完整 CSV 的按行状态列表（包括中间 `peak_round`）。失败终态不是“校验通过的性能结果”，但失败证据仍应完整。校验模块与执行模块共享部分规则，正式数值验收还应从真实原始 JSON/SQLite/NPY 独立复算，逐项对照 CSV。

开发检查和回归测试在维护者本地进行，不要求使用者在运行版本中安装或执行发布工具。本地检查不能代替真实 Docker 和模型验证。维护者发布前应核对实际提交内容，并保留以下验收证据：

- 目标镜像及实际服务的小规模固定测试、重复并发、文本/流式与适用字段、正常/前缀/精度等保留接口。
- 两个及更多独立服务并行，分别生成任务 CSV/日志；同服务、同容器、重叠目录及跨批次冲突被阻止。
- 摸高扩展、下探、二分、硬上限开/关、精度、重新确认、不稳定、无效重试、请求失败、超时/预算和取消，轨迹与终态一致。
- 所有 23 列按实际原始证据核对，重复测试作用域、归一化分母、选中轮次及空值适用性正确，归档损坏能够检出。
- 配置加载失败、启动异常、容器丢失/替换、日志及磁盘错误、终端断开、取消和停止失败均有明确状态及证据，不碰其他服务。
- 发布内容、原生文件字节和模式、公开隐私检查、本地回归结果及实机证据对应同一版本；未覆盖项明确列出。

版本以 `v2026.10.25` 注释标签对应的源码为准。更换镜像、上游或被测服务后应重新验证，不能只凭其他环境的检查结果判断当前运行正常。

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

随后在本地维护记录中更新原生提交号、文件内容和模式清单，审查原生配置、CLI 和结果接口变化，在 `core/` 适配并补充本地回归测试。更新记录不能替代原生文件的实际比对；最后重新验证目标镜像、实际服务和数值提取，保留对应上游提交号及证据。
