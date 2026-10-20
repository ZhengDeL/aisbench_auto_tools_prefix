#!/usr/bin/env bash
# AISBench 自动化压测统一入口 · v2026.10.20
#
# 职责：定位交付目录、检查宿主机运行条件、把原始参数交给任务管理器。
# 配置加载与冻结、资源互斥、并行调度由 core/aisbench_run_manager.py 负责；
# 单任务容器和场景执行由 core/aisbench_worker.py 负责。
#
# 维护约定：服务/模型/场景参数写在 config.env；业务规则写在对应 core 模块。
# 本入口不 source 配置，避免重复加载或多任务之间相互覆盖。
# 子进程通过 exec 接管当前进程，保留退出码与信号传递行为。
set -uo pipefail


print_usage() {
    cat <<'USAGE'
用法：
  bash run_benchmark.sh                         # 自动读取入口同目录的 config.env
  bash run_benchmark.sh ./service.env           # 运行一份指定配置
  bash run_benchmark.sh ./a.env ./b.env         # 并行运行多份配置，文件名自定
  bash run_benchmark.sh --help                  # 查看本说明

无需单独 source。每份配置对应一个专用压测容器和独立输出目录；
并行配置必须指向不同服务，冲突由任务管理器在发起压测前检查。

宿主机需要 Linux、Bash、Python 3.7+ 和 Docker；镜像内使用自带 Python。
配置参考见 config.env 末尾；使用与排查说明见 automation/README.md。
USAGE
}


# 这里只检查启动入口需要的解释器；Docker、模型路径及场景检查由管理器
# 执行并写入批次日志，保证所有配置沿用同一套预检规则。
check_python() {
    if ! command -v python3 >/dev/null 2>&1; then
        printf '%s\n' 'ERROR: 未找到 python3，需要 Python 3.7 或更高版本；尚未启动压测。' >&2
        return 1
    fi

    python3 -c '
import sys
if sys.version_info < (3, 7):
    sys.stderr.write("ERROR: 需要 Python 3.7 或更高版本，当前为 %s；尚未启动压测。\n" % sys.version.split()[0])
    raise SystemExit(1)
'
}


main() {
    local project_dir core_dir module

    case "${1:-}" in
        -h|--help)
            print_usage
            return 0
            ;;
    esac

    # 从脚本位置定位，允许在其他工作目录启动；始终对带空格的路径加引号。
    project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)" || return 1
    core_dir="${project_dir}/automation/core"

    for module in aisbench_run_manager.py aisbench_worker.py; do
        if [[ ! -r "${core_dir}/${module}" ]]; then
            printf 'ERROR: 缺少运行模块：%s；请使用完整交付目录。\n' "${core_dir}/${module}" >&2
            return 1
        fi
    done

    # 第一次调用 Python 前即禁用字节码缓存，原生目录始终只供读取。
    export PYTHONDONTWRITEBYTECODE=1

    check_python || return 1

    # 管理器为隔离的子任务设置此内部标记；用户正常启动走下面的管理器。
    if [[ "${AISBENCH_MANAGED_WORKER:-0}" == "1" ]]; then
        exec python3 "${core_dir}/aisbench_worker.py" "$@"
    fi

    exec python3 "${core_dir}/aisbench_run_manager.py" \
        "${project_dir}/run_benchmark.sh" "$@"
}


main "$@"
