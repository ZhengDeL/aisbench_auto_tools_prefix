# 数据集文件夹
DATASET_PATH = "/home/dataset"
# aisbench 工作路径, 为 git clone aisbench 后得到的 benchmark 目录的绝对路径
# 可通过命令 `pip show ais-bench-benchmark` 查询location
WORK_PATH = "/home/benchmark"
# 服务化配置的模型名称
MODEL_NAME = "ds"
# 模型权重路径, 用于读取 tokenizer
MODEL_PATH = "/home/weights/model_weights"
# 请求目的 IP
HOST_IP = "192.168.1.10"
# 请求目的端口
HOST_PORT = "8004"

# 如果使用稳态测试请将该字段设置为 "stable_stage"
DEFAULT_PERFORMANCE_TEST = "default_perf"

# aisbench输出日志保存路径
OUTPUT_DIR = "./outputs/default"
