"""Publish task-labelled results without recalculating benchmark measurements."""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
import re
import uuid
from typing import Any


METRIC_COLUMNS = (
    'scenario,current_time,input_len,output_len,total_req,max_cc,cc,rr,TTFT avg,'
    'TTFT P90,TPOT avg,TPOT SLO_P90,E2E_time,output_throughput,'
    'single_output_throughput,E2E_throughput,single_E2E_throughput,qps,qpm,'
    'input_token_throughput,prefill_token_throughput,repeat_rate,status'
).split(',')
IDENTITY_COLUMNS = ['task', 'model', 'endpoint', 'service_id', 'run_id', 'source_csv']
TERMINAL_STATES = {'success', 'failed', 'cancelled', 'cleanup_failed', 'not_started'}
ROW_STATES = {'passed', 'peak', 'peak_round', 'failed', 'no_result'}
_SECRET_KEYS = re.compile(r'(?i)^(?:api[_-]?key|password|passwd|secret|authorization|access_token|bearer_token|api_url|url)$')
_COPIED_LOGS: dict[tuple[str, str], tuple[int, int]] = {}


def atomic_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


def copy_log(source: Path, target: Path) -> None:
    stat = source.stat()
    signature = (stat.st_mtime_ns, stat.st_size)
    key = (str(source), str(target))
    if _COPIED_LOGS.get(key) != signature or not target.is_file():
        atomic_bytes(target, source.read_bytes())
        _COPIED_LOGS[key] = signature


def csv_bytes(columns: list[str], rows: list[dict[str, Any]]) -> bytes:
    handle = io.StringIO(newline='')
    writer = csv.DictWriter(handle, fieldnames=columns, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return handle.getvalue().encode('utf-8')


def redacted(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ('[redacted]' if _SECRET_KEYS.fullmatch(str(key)) else redacted(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [redacted(item) for item in value]
    return value


def prepare_task_outputs(output: Path, label: str) -> None:
    """Called only while the task output lock is held, before launching workers.

    Withdraw the three generated aliases from an earlier run. Detailed runtime
    directories are immutable history and are deliberately retained.
    """
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', label):
        raise ValueError('unsafe output label')
    for suffix in ('__all_results.csv', '__execution.log', '__launcher.log'):
        (output / (label + suffix)).unlink(missing_ok=True)


def save_effective_config(task_path: Path, metadata: dict[str, Any],
                          environment: dict[str, str], config_sha256: str) -> None:
    """Save a whitelist, never a copy of a sourced shell/environment or API URL."""
    task_path.mkdir(parents=True, exist_ok=True)
    names = ('MODEL_NAME', 'MODEL_HOST_PATH', 'TOOLS_HOST_DIR', 'CONTAINER_NAME',
             'EXPECTED_IMAGE', 'HOST_OUTPUT_DIR', 'HOST_IP', 'HOST_PORT', 'SERVICE_ID',
             'DATA_NUM_MULTIPLIER', 'NPU_NUM', 'PEAK_HARD_CAP', 'FAILED_REQ_LIMIT',
             'MAX_CONTEXT_LEN', 'PEAK_MIN_CONCURRENCY', 'PEAK_MAX_MULTIPLIER',
             'PEAK_DATA_MULTIPLIER', 'PEAK_WAIT_SECONDS', 'PEAK_TTFT_LIMIT', 'PEAK_TPOT_LIMIT')
    payload = {key: environment[key] for key in names if key in environment}
    # Shell preflight reports real defaults and expanded scenes, not guesses.
    effective = metadata.get('effective_config', {})
    if isinstance(effective, dict):
        # Expanded CLI strings can contain arbitrary user-defined arguments.
        # Preserve their counts; structured scenarios can be redacted by key.
        payload.update({key: item for key, item in effective.items()
                        if key not in {'expanded_fixed', 'expanded_peak'}})
        for key in ('expanded_fixed', 'expanded_peak'):
            if isinstance(effective.get(key), list):
                payload[key + '_count'] = len(effective[key])
    payload.update(model=metadata.get('model_name', ''),
                   endpoint=metadata.get('service_endpoint', metadata.get('endpoint', '')),
                   config_sha256=config_sha256, credentials='omitted')
    atomic_json(task_path / 'config.redacted.json', redacted(payload))


def metric_rows(content: bytes) -> list[dict[str, str]]:
    records = list(csv.reader(io.StringIO(content.decode('utf-8-sig'), newline=''), strict=True))
    if not records or records[0] != METRIC_COLUMNS:
        raise ValueError('unexpected metric schema')
    rows = []
    for values in records[1:]:
        if len(values) != len(METRIC_COLUMNS):
            raise ValueError('incomplete metric row')
        row = dict(zip(METRIC_COLUMNS, values))
        if not row['scenario'] or row['status'] not in ROW_STATES:
            raise ValueError('invalid metric identity or state')
        rows.append(row)
    return rows


def contained_result(status: dict[str, Any]) -> Path | None:
    raw = status.get('result_dir', '')
    output = status.get('host_output_dir', '')
    if not raw or not output:
        return None
    parent, result = Path(output).resolve(), Path(raw).resolve()
    if result == parent or not result.is_relative_to(parent):
        raise ValueError('result outside task output')
    source = result / 'all_results.csv'
    if not source.resolve().is_relative_to(parent):
        raise ValueError('metric source outside task output')
    return source


def publish_batch(batch_path: Path, batch_id: str, statuses: list[dict[str, Any]],
                  final: bool = False) -> list[str]:
    """Write one consistent snapshot; return terminal artifact errors to caller.

    Measurement values remain strings from the 23-column source. A running
    worker may be between CSV writes; its last complete snapshot is retried.
    No timestamp, quantile, throughput or failed measurement is synthesized.
    """
    merged: list[dict[str, str]] = []
    errors: list[str] = []
    published: list[dict[str, Any]] = []
    for original in statuses:
        status = dict(original)
        label = status.get('output_label', '')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', label):
            raise ValueError('unsafe output label')
        task_path = batch_path / 'tasks' / label
        task_path.mkdir(parents=True, exist_ok=True)
        output = Path(status['host_output_dir'])
        named_csv = output / (label + '__all_results.csv')
        named_launcher = output / (label + '__launcher.log')
        status.update(source_csv='', result_csv='', results_state='pending', results_error='',
                      completed_tests=0, passed_tests=0, peak_tests=0, failed_tests=0,
                      peak_rounds=0, named_launcher_log='')
        terminal = status.get('state') in TERMINAL_STATES
        try:
            source = contained_result(status)
            if source is not None:
                status['source_csv'] = str(source)
            if source is None or not source.is_file():
                if terminal:
                    status['results_state'] = 'missing'
                    if status.get('state') == 'success':
                        raise ValueError('successful worker has no metrics')
            else:
                content = source.read_bytes()
                rows = metric_rows(content)
                if not rows and terminal and status.get('state') == 'success':
                    raise ValueError('successful worker has no metric rows')
                status['completed_tests'] = sum(row['status'] != 'peak_round' for row in rows)
                status['passed_tests'] = sum(row['status'] == 'passed' for row in rows)
                status['peak_tests'] = sum(row['status'] == 'peak' for row in rows)
                status['failed_tests'] = sum(row['status'] in {'failed', 'no_result'} for row in rows)
                status['peak_rounds'] = sum(row['status'] == 'peak_round' for row in rows)
                planned = status.get('planned_tests', 0)
                if (terminal and status.get('state') == 'success' and isinstance(planned, int)
                        and planned > 0 and status['completed_tests'] != planned):
                    raise ValueError('incomplete successful result set')
                identity = {key: str(status.get(key, '')) for key in IDENTITY_COLUMNS}
                merged.extend({**identity, **row} for row in rows)
                atomic_bytes(named_csv, content)
                status.update(result_csv=str(named_csv), results_state='ready' if terminal else 'running')
            if terminal or final:
                launcher = Path(status['launcher_log']) if status.get('launcher_log') else None
                if launcher and launcher.is_file():
                    copy_log(launcher, named_launcher)
                    status['named_launcher_log'] = str(named_launcher)
                if source and (source.parent / 'execution.log').is_file():
                    copy_log(source.parent / 'execution.log', output / (label + '__execution.log'))
        except (OSError, UnicodeError, ValueError, csv.Error):
            if terminal or final:
                # Do not include raw config, URL, CSV values or parser messages.
                status.update(results_state='invalid', results_error='metrics_or_artifact_invalid')
                errors.append(str(status.get('task', label)) + ': metrics_or_artifact_invalid')
                named_csv.unlink(missing_ok=True)
            else:
                status['results_state'] = 'pending'
        atomic_json(task_path / 'result_index.json', status)
        published.append(status)
    atomic_bytes(batch_path / 'merged_metrics.csv', csv_bytes(IDENTITY_COLUMNS + METRIC_COLUMNS, merged))
    atomic_json(batch_path / 'batch_results.json', {'schema_version': 2, 'batch_id': batch_id,
                                                  'metrics_csv': str(batch_path / 'merged_metrics.csv'),
                                                  'artifact_errors': errors, 'tasks': published})
    if published:
        atomic_bytes(batch_path / 'batch_results.csv', csv_bytes(list(published[0]), published))
    return errors
