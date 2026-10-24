"""User configuration, defaults and structured scenario planning.

This module only reads source files. Native Python modules are never imported
on the host: their files must stay identical to the pinned upstream tree.
"""
from __future__ import annotations

import ast
import json
import math
import os
import re
from pathlib import Path
from typing import Any

IMAGE = 'ghcr.io/aisbench/aisbench_benchmark:v3.1-20261013-master-openeuler24.03-py311'
PEAK_NEW_DEFAULTS = {
    'PEAK_PRECISION': ('1', 'peak_precision', 'precision'),
    'PEAK_CONFIRM_RUNS': ('3', 'peak_confirm_runs', 'confirm-runs'),
    'PEAK_CONFIRM_MIN_REQUESTS': ('100', 'peak_confirm_min_requests', 'confirm-min-requests'),
    'PEAK_MAX_TRIALS': ('40', 'peak_max_trials', 'max-trials'),
    'PEAK_TRIAL_TIMEOUT': ('1800', 'peak_trial_timeout', 'trial-timeout'),
    'PEAK_TOTAL_TIMEOUT': ('14400', 'peak_total_timeout', 'total-timeout'),
    'PEAK_INVALID_RETRIES': ('1', 'peak_invalid_retries', 'invalid-retries'),
    'PEAK_FLOOR_CONCURRENCY': ('1', 'peak_floor_cc', 'floor-concurrency'),
}
PEAK_NEW_NAMES = tuple(PEAK_NEW_DEFAULTS)
ENV_DEFAULTS = {
    'CONTAINER_NAME': '', 'EXPECTED_IMAGE': IMAGE, 'HOST_OUTPUT_DIR': '',
    'MODEL_NAME': 'your-model', 'MODEL_HOST_PATH': '/mnt/models/your-model',
    'TOOLS_HOST_DIR': '', 'HOST_IP': '192.168.1.10', 'HOST_PORT': '8000',
    'API_KEY': '', 'API_URL': '', 'SERVICE_ID': '', 'FAILED_REQ_LIMIT': '0',
    'MAX_CONTEXT_LEN': '512000', 'NPU_NUM': '1', 'POD_INFO': '',
    'DATA_NUM_MULTIPLIER': '4', 'PEAK_MIN_CONCURRENCY': '1',
    'PEAK_MAX_MULTIPLIER': '20', 'PEAK_DATA_MULTIPLIER': '4',
    'PEAK_WAIT_SECONDS': '10', 'PEAK_TTFT_LIMIT': '5', 'PEAK_TPOT_LIMIT': '50',
    'PEAK_HARD_CAP': '0', 'PEAK_SLO_TOLERANCE': '0.0',
    **{name: entry[0] for name, entry in PEAK_NEW_DEFAULTS.items()},
}
PUBLIC_ENV_NAMES = tuple(ENV_DEFAULTS)
BOOLEAN_OPTIONS = {'prefix_test', 'enable_think', 'test_accuracy'}
VALUE_OPTIONS = {
    'input_len', 'output_len', 'data_num', 'concurrency', 'request_rate',
    'test_type', 'dataset', 'repeat', 'npu_num', 'dataset_type', 'prefix_num',
    'repeat_rate', 'seed', 'dp', 'length_mean', 'length_std', 'length_min', 'length_max',
}
SCENE_KEYS = {'name', 'input_len', 'output_len', 'concurrency', 'request_rate',
              'ttft_limit', 'tpot_limit', 'peak_min_cc', 'peak_max_cc',
              'peak_data_multiplier', 'peak_wait', 'peak_slo_tolerance',
              *(entry[1] for entry in PEAK_NEW_DEFAULTS.values())}


def integer(value: Any, name: str, minimum: int | None = 1) -> int:
    if isinstance(value, bool) or not re.fullmatch(r'[+-]?\d+', str(value)):
        raise ValueError(f'{name} 必须为整数')
    result = int(value)
    if minimum is not None and result < minimum:
        raise ValueError(f'{name} 必须 >= {minimum}')
    return result


def number(value: Any, name: str, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f'{name} 必须为有限数值')
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name} 必须为有限数值') from None
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError(f'{name} 超出有效数值范围')
    return result


def effective_environment(environment: dict[str, str], release_root: Path) -> dict[str, str]:
    values = {key: str(environment.get(key) or default) for key, default in ENV_DEFAULTS.items()}
    values['TOOLS_HOST_DIR'] = str(Path(values['TOOLS_HOST_DIR'] or release_root / 'aisbench_auto_tools_prefix').resolve())
    values['HOST_OUTPUT_DIR'] = str(Path(values['HOST_OUTPUT_DIR'] or release_root / 'outputs/default').resolve())
    values['MODEL_HOST_PATH'] = str(Path(values['MODEL_HOST_PATH']).resolve())
    for key, minimum in [('DATA_NUM_MULTIPLIER', 1), ('NPU_NUM', 1),
                         ('FAILED_REQ_LIMIT', 0), ('MAX_CONTEXT_LEN', 0),
                         ('PEAK_MIN_CONCURRENCY', 1), ('PEAK_MAX_MULTIPLIER', 1),
                         ('PEAK_DATA_MULTIPLIER', 1), ('PEAK_WAIT_SECONDS', 0)]:
        integer(values[key], key, minimum)
    for key in ('PEAK_TTFT_LIMIT', 'PEAK_TPOT_LIMIT'):
        number(values[key], key)
    if not 0 <= number(values['PEAK_SLO_TOLERANCE'], 'PEAK_SLO_TOLERANCE') <= 1:
        raise ValueError('PEAK_SLO_TOLERANCE 必须为0..1之间的有限比例，例如0.05表示5%')
    for key in ('PEAK_TTFT_LIMIT', 'PEAK_TPOT_LIMIT'):
        limit = number(values[key], key)
        if limit > 0:
            number(limit * (1 + float(values['PEAK_SLO_TOLERANCE'])), key + '有效上限')
    for key in PEAK_NEW_NAMES:
        if key.endswith('TIMEOUT'):
            if number(values[key], key, 0) <= 0:
                raise ValueError(f'{key} 必须 > 0 秒')
        else:
            integer(values[key], key, 0 if key in {'PEAK_CONFIRM_MIN_REQUESTS', 'PEAK_INVALID_RETRIES'} else 1)
    if values['PEAK_HARD_CAP'] not in {'0', '1'}:
        raise ValueError('PEAK_HARD_CAP 必须为0或1')
    if not values['MODEL_NAME'] or any(c in values['MODEL_NAME'] for c in '\r\n\0'):
        raise ValueError('MODEL_NAME 不能为空或包含换行')
    return values


def read_scenarios(config_path: Path) -> list[dict[str, Any]]:
    text = config_path.read_text(encoding='utf-8-sig')
    definitions = re.findall(r'^[ \t]*(?:export[ \t]+)?SCENARIOS_JSON[ \t]*=', text, re.M)
    blocks = re.findall(r"^SCENARIOS_JSON=\$\(cat <<'JSON'[ \t]*\n(.*?)^JSON\n\)[ \t]*(?:\n|$)", text, re.M | re.S)
    if len(definitions) != 1 or len(blocks) != 1:
        raise ValueError("配置需要且仅需要一个非export的 SCENARIOS_JSON=$(cat <<'JSON' ... JSON ) 块")
    try:
        result = json.loads(blocks[0])
    except json.JSONDecodeError as error:
        raise ValueError(f'SCENARIOS_JSON格式错误：第{error.lineno}行第{error.colno}列；请检查所用配置文件') from error
    if not isinstance(result, list) or not result:
        raise ValueError('SCENARIOS_JSON 必须为非空场景数组')
    return result


def native_options(native_root: Path) -> set[str]:
    tree = ast.parse((native_root / 'aisbench_test.py').read_text(encoding='utf-8'))
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'add_argument':
            for argument in node.args:
                value = vars(argument).get('value', vars(argument).get('s'))
                if isinstance(value, str) and value.startswith('--'):
                    result.add(value[2:])
    return result


def cli_arguments(values: dict[str, Any]) -> list[str]:
    result = []
    for key, value in values.items():
        if isinstance(value, bool):
            if value:
                result.append('--' + key)
        elif isinstance(value, (str, int, float)) and not any(c in str(value) for c in '\r\n\0'):
            result.extend(['--' + key, str(value)])
        else:
            raise ValueError(f'参数{key}值类型不支持或包含换行')
    return result


def build_plan(environment: dict[str, str], config_path: Path) -> dict[str, Any]:
    env = environment
    allowed = native_options(Path(env['TOOLS_HOST_DIR']))
    fixed, peaks = [], []
    names, archive_names, slugs = set(), set(), set()

    def register(name: str) -> None:
        slug = ''.join(c if c.isalnum() or c in '-_' else '_' for c in name.strip()).strip('_') or 'task'
        if name in archive_names or slug in slugs:
            raise ValueError('场景归档名称冲突，请调整name')
        archive_names.add(name)
        slugs.add(slug)

    for scene in read_scenarios(config_path):
        if not isinstance(scene, dict):
            raise ValueError('场景必须为JSON对象')
        name = scene.get('name')
        if not isinstance(name, str) or not name.strip() or any(c in name for c in "'|\r\n\0"):
            raise ValueError('场景name不能为空，不能包含单引号、竖线或换行')
        if name in names:
            raise ValueError('场景name重复')
        names.add(name)
        input_len = integer(scene.get('input_len', '512'), f'{name}.input_len')
        output_len = integer(scene.get('output_len', '128'), f'{name}.output_len')
        request_rate = str(scene.get('request_rate', '0'))
        number(request_rate, 'request_rate', 0)
        concurrency = scene.get('concurrency', [1])
        if not isinstance(concurrency, list) or not concurrency:
            raise ValueError(f'{name}.concurrency 必须为非空数组')
        concurrency = [value if value == 'max' else integer(value, 'concurrency') for value in concurrency]
        if concurrency.count('max') > 1:
            raise ValueError('同一场景max只能写一次；固定并发可以重复')
        extra = {key: value for key, value in scene.items() if key not in SCENE_KEYS}
        unknown = set(extra) - allowed
        if unknown:
            raise ValueError('原生CLI不支持的场景参数: ' + ', '.join(sorted(unknown)))
        for key in ('data_num', 'npu_num', 'repeat', 'prefix_num', 'dp'):
            if key in extra:
                integer(extra[key], key)
        if 'seed' in extra:
            integer(extra['seed'], 'seed', None)
        for key in BOOLEAN_OPTIONS:
            if key in extra and not isinstance(extra[key], bool):
                raise ValueError(f'{key} 必须为JSON布尔值')
        for key, choices in [('dataset_type', {'normal', 'prefix_cache'}), ('test_type', {'stream', 'text'})]:
            if key in extra and extra[key] not in choices:
                raise ValueError(f'{key} 不在支持范围')
        if 'repeat_rate' in extra:
            ratio = str(extra['repeat_rate']).strip()
            value = number(ratio[:-1] if ratio.endswith('%') else ratio, 'repeat_rate')
            if not 0 <= (value / 100 if ratio.endswith('%') else value) <= 1:
                raise ValueError('repeat_rate 必须在0..1或0%..100%')
            extra['repeat_rate'] = ratio
        for first, second in [('length_mean', 'length_std'), ('length_min', 'length_max')]:
            if (first in extra) != (second in extra):
                raise ValueError(f'{first}和{second}必须同时配置')
        for key in ('length_mean', 'length_min', 'length_max'):
            if key in extra:
                integer(extra[key], key)
        if 'length_std' in extra:
            number(extra['length_std'], 'length_std', 0)
        effective_length = max(int(extra['length_min']), int(extra['length_max'])) if 'length_max' in extra else int(extra.get('length_mean', input_len))
        context = int(env['MAX_CONTEXT_LEN'])
        if context > 0 and effective_length + output_len > context:
            raise ValueError(f'{name} 输入加输出超过 MAX_CONTEXT_LEN={context}')
        defaults = dict(npu_num=env['NPU_NUM'], test_type='stream', repeat='1',
                        dataset_type='normal', prefix_num='1', repeat_rate='0', seed='1', dp='1')
        fixed_cc = [c for c in concurrency if c != 'max']
        occurrences: dict[int, int] = {}
        for cc in fixed_cc:
            occurrences[cc] = occurrences.get(cc, 0) + 1
            archive = f'{name}_c{cc}' + (f'_run{occurrences[cc]}' if occurrences[cc] > 1 else '')
            register(archive)
            params = {**defaults, **extra, 'input_len': input_len, 'output_len': output_len,
                      'concurrency': cc, 'request_rate': request_rate,
                      'data_num': extra.get('data_num', cc * int(env['DATA_NUM_MULTIPLIER']))}
            fixed.append({'name': archive, 'args': cli_arguments(params), **params,
                          'accuracy': bool(params.get('test_accuracy', False))})
        if 'max' not in concurrency:
            continue
        if extra.get('test_accuracy', False):
            raise ValueError(f'{name} 精度测试不能使用max摸高；请使用固定并发执行精度测试')
        if extra.get('test_type', 'stream') != 'stream' or output_len < 2:
            raise ValueError(f'{name} 摸高需要stream且输出长度至少2；固定text/单token仍支持')
        minimum = integer(scene.get('peak_min_cc', env['PEAK_MIN_CONCURRENCY']), 'peak_min_cc')
        maximum = integer(scene.get('peak_max_cc', (fixed_cc[0] if fixed_cc else int(env['PEAK_MIN_CONCURRENCY'])) * int(env['PEAK_MAX_MULTIPLIER'])), 'peak_max_cc')
        if minimum > maximum:
            raise ValueError('peak_min_cc不能超过peak_max_cc')
        archive = name + '_peak'
        register(archive)
        peak = dict(name=archive, input_len=input_len, output_len=output_len,
                    min_cc=minimum, max_cc=maximum, request_rate=request_rate,
                    multiplier=integer(scene.get('peak_data_multiplier', env['PEAK_DATA_MULTIPLIER']), 'peak_data_multiplier'),
                    wait=integer(scene.get('peak_wait', env['PEAK_WAIT_SECONDS']), 'peak_wait', 0),
                    ttft=number(scene.get('ttft_limit', env['PEAK_TTFT_LIMIT']), 'ttft_limit'),
                    tpot=number(scene.get('tpot_limit', env['PEAK_TPOT_LIMIT']), 'tpot_limit'),
                    slo_tolerance=number(scene.get('peak_slo_tolerance', env['PEAK_SLO_TOLERANCE']), 'peak_slo_tolerance'),
                    extra_args=cli_arguments(extra), repeat_rate=str(extra.get('repeat_rate', '0')))
        if not 0 <= peak['slo_tolerance'] <= 1:
            raise ValueError('peak_slo_tolerance 必须为0..1之间的有限比例，例如0.05表示5%')
        for key in ('ttft', 'tpot'):
            if peak[key] > 0:
                number(peak[key] * (1 + peak['slo_tolerance']), key + '有效上限')
        for key, (_, scene_key, option) in PEAK_NEW_DEFAULTS.items():
            value = scene.get(scene_key, env[key])
            if key.endswith('TIMEOUT'):
                if number(value, scene_key, 0) <= 0:
                    raise ValueError(f'{scene_key}必须>0秒')
            else:
                integer(value, scene_key, 0 if key in {'PEAK_CONFIRM_MIN_REQUESTS', 'PEAK_INVALID_RETRIES'} else 1)
            peak[scene_key] = value
        if int(peak['peak_floor_cc']) > minimum:
            raise ValueError('peak_floor_cc不能超过初始peak_min_cc')
        peaks.append(peak)
    return {'fixed': fixed, 'peak': peaks,
            'planned_scenarios': [item['name'] for item in fixed + peaks]}
