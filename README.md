# AISBench automation · v2026.10.19

在原生 AISBench 工具外增加配置加载、多个独立服务并行测试、固定并发测试、并发摸高、结果核验和日志归档。**本版本为发布候选，正式发布以完成验收后的标签为准。**

原生仓完整放在 `aisbench_auto_tools_prefix/`，内容和 Git 文件模式与选定上游提交一致。自动化代码位于 `automation/`，运行输出位于根目录 `outputs/`；运行时不向原生目录写入配置、缓存或结果。

```text
aisbench_auto_tools_prefix/          # 本仓库根目录
├── run_perf_scenarios.sh            # 日常启动入口
├── env.env                          # 默认完整配置，末尾有参数参考
├── README.md
├── aisbench_auto_tools_prefix/      # 原生仓，只通过上游同步更新
├── automation/
│   ├── README.md                    # 配置、算法、结果、维护与验证说明
│   ├── core/                       # 按职责拆分的运行模块
│   ├── tools/                      # 校验工具和离线门禁
│   ├── tests/                      # 自动化测试
│   ├── examples/                   # 两份小规模公开示例
│   └── metadata/                   # 锁定上游的文件清单
└── outputs/default/<批次号>/        # 单份、多份配置采用同一批次结构
```

需要 Linux、Bash、Python 3.7+、Docker、可读取的模型 tokenizer 目录和已启动的模型服务。默认镜像为：

```text
ghcr.io/aisbench/aisbench_benchmark:v3.1-20261013-master-openeuler24.03-py311
```

镜像架构应与宿主机匹配。脚本启动的是 AISBench 请求客户端容器，不负责部署模型服务。

## 启动

在仓库根目录编辑配置，再运行。**不需要先 `source`，也不要求先复制配置文件。**

```bash
vi env.env
bash run_perf_scenarios.sh ./env.env
```

`env.env` 中的服务地址、模型名和路径是公开占位示例，必须填写实际值。当前默认场景为 **64K 输入、256/1024 输出，包含固定并发和摸高**；首次验证流程时可先使用 1K 输入、512 输出、并发 4 的示例：

```bash
vi automation/examples/test_a.env
bash run_perf_scenarios.sh automation/examples/test_a.env
```

该小示例默认主测试请求数为 `4 × DATA_NUM_MULTIPLIER(4) = 16`，不包含摸高。模型需要的 tokenizer、聊天模板和服务行为仍需实际验证。

一次启动多个独立服务的测试：

```bash
vi automation/examples/test_a.env
vi automation/examples/test_b.env
bash run_perf_scenarios.sh automation/examples/test_a.env automation/examples/test_b.env
```

配置文件名可以自定，不限于 `test_a`、`test_b`。一份配置对应一个任务、一个 AISBench 容器；三份配置启动三个，四份启动四个。同一任务中的各轮顺序执行，各任务并行执行。**多份配置必须指向不同的服务；同一有效主机和端口会被拒绝。** 通过不同 DNS 名称或代理访问同一个后端时，应使用相同 `SERVICE_ID` 标识，避免把同一个服务误当成两组独立服务。

也可省略配置参数：

```bash
bash run_perf_scenarios.sh
```

零参数会自动读取根目录 `env.env`，同时保留当前进程已导出的受支持全局参数覆盖值。显式传入 `./env.env` 时，各配置与当前 shell 的这些参数隔离，通常更容易复现。场景始终从对应文件的 `SCENARIOS_JSON` 块读取。完整优先级和空值规则见 [配置说明](automation/README.md#配置加载与优先级)。

## 查看结果

启动和结束时会打印批次目录。例如：

```text
outputs/default/<批次号>/
├── batch_state.json
├── batch_results.json
├── batch_results.csv
├── merged_metrics.csv
├── logs/batch.log
├── logs/events.jsonl
├── logs/archive_index.json
└── tasks/
    ├── test_a__your-model-a/
    │   ├── test_a__your-model-a__all_results.csv
    │   ├── test_a__your-model-a__execution.log
    │   └── result_index.json
    └── test_b__your-model-b/
        ├── test_b__your-model-b__all_results.csv
        ├── test_b__your-model-b__execution.log
        └── result_index.json
```

每组有自己的 CSV 和日志；批次文件提供索引及合并视图，不会把不同服务的吞吐相加或把延迟平均。自定义 `HOST_OUTPUT_DIR` 时，以任务索引中的实际路径为准。

先看 `batch_state.json`、`batch_results.json` 和 `logs/batch.log`，再按任务查看 `execution.log`、摸高报告和原始证据。结束时管理日志按 16 MiB 字节段额外封存为 gzip，`logs/archive_index.json` 保存来源及校验值，原始文件保留；封存失败会使批次失败。`passed` 表示固定测试通过执行及适用的数据完整性检查；摸高需区分 `peak_confirmed`、`peak_capped`、预算耗尽、请求失败和基础设施错误。字段单位、终态及排查顺序见 [自动化说明](automation/README.md#结果与数值口径)。

私有原始日志可能包含服务地址、请求内容或凭据，不能直接上传公开仓库。可以直接编辑默认配置；需要保留公开示例时，也可自行使用被 Git 忽略的 `*.local.env`，这只是可选用法。

## 维护与验证

日常修改测试内容只需编辑配置。新增执行行为、算法或结果字段时，再修改 `automation/core/` 中对应模块及测试。原生目录只跟随选定上游提交更新，方法见 [原仓更新](automation/README.md#更新原生仓)。

Linux 上执行离线门禁：

```bash
bash automation/tools/check_all.sh
```

离线检查通过不等于真实模型验收通过。正式验收还需要在目标镜像和实际服务上验证固定测试、多个独立服务、摸高各终态、故障与取消处理，并将 CSV 与对应原始结果逐项核对。完整命令和证据要求见 [验证与发布](automation/README.md#验证与发布)。
