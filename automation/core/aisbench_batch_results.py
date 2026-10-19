"""Publish task-labelled results without recalculating benchmark measurements."""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
import re
import uuid
import hashlib
from typing import Any
from aisbench_result_adapter import (SUMMARY_COLUMNS, ROW_STATES, SUCCESS_STATES,
                                    validate_summary_row, load_scope_index)
from aisbench_artifact_archive import path_is_relative_to, unlink_if_present
import aisbench_result_validation as result_validation


METRIC_COLUMNS = (
    'scenario,current_time,input_len,output_len,total_req,max_cc,cc,rr,TTFT avg,'
    'TTFT P90,TPOT avg,TPOT SLO_P90,E2E_time,output_throughput,'
    'single_output_throughput,E2E_throughput,single_E2E_throughput,qps,qpm,'
    'input_token_throughput,prefill_token_throughput,repeat_rate,status'
).split(',')
IDENTITY_COLUMNS = ['task', 'model', 'endpoint', 'service_id', 'run_id', 'source_csv']
TERMINAL_STATES = {'success', 'failed', 'cancelled', 'cleanup_failed', 'not_started'}
_SECRET_KEYS = re.compile(r'(?i)^(?:api[_-]?key|password|passwd|secret|authorization|access_token|bearer_token|api_url|url)$')
_COPIED_LOGS: dict[tuple[str, str], tuple[int, int]] = {}
_VOLATILE_STATUS_KEYS = {'elapsed', 'elapsed_seconds', 'updated_at'}
_PUBLICATIONS: dict[tuple[str, str], tuple[str, str, dict[Path, bytes], list[str]]] = {}


def atomic_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        unlink_if_present(temporary)


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8'))


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
        unlink_if_present(output / (label + suffix))


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
    payload['parameter_sources'] = metadata.get('parameter_sources', {})
    atomic_json(task_path / 'config.redacted.json', redacted(payload))


def metric_rows(content: bytes, accuracy_rows: set[int] | None = None) -> list[dict[str, str]]:
    records = list(csv.reader(io.StringIO(content.decode('utf-8-sig'), newline=''), strict=True))
    if not records or records[0] != METRIC_COLUMNS:
        raise ValueError('unexpected metric schema')
    rows = []
    for index, values in enumerate(records[1:]):
        if len(values) != len(METRIC_COLUMNS):
            raise ValueError('incomplete metric row')
        row = dict(zip(METRIC_COLUMNS, values))
        if not row['scenario'] or row['status'] not in ROW_STATES:
            raise ValueError('invalid metric identity or state')
        validate_summary_row(row, accuracy=index in (accuracy_rows or set()))
        rows.append(row)
    return rows


def contained_result(status: dict[str, Any]) -> Path | None:
    raw = status.get('result_dir', '')
    output = status.get('host_output_dir', '')
    if not raw or not output:
        return None
    parent, result = Path(output).resolve(), Path(raw).resolve()
    if result == parent or not path_is_relative_to(result, parent):
        raise ValueError('result outside task output')
    source = result / 'all_results.csv'
    if not path_is_relative_to(source.resolve(), parent):
        raise ValueError('metric source outside task output')
    return source


def stable_status(value: Any) -> Any:
    """Keep identity, phase, progress and failures; drop display-only clocks."""
    if isinstance(value, dict):
        return {key: stable_status(item) for key, item in value.items()
                if key not in _VOLATILE_STATUS_KEYS}
    if isinstance(value, list):
        return [stable_status(item) for item in value]
    return value


def committed_unchanged(batch_path: Path, signature: str,
                        aliases: dict[Path, bytes]) -> bool:
    """Reuse only an intact committed generation, never a failed/stale alias.

    Source parsing and full terminal provenance validation happen before this
    check on every call, including final publication. This is a disk publication
    cache, not a cache of validation conclusions.
    """
    try:
        payload = json.loads((batch_path / 'batch_results.json').read_text(encoding='utf-8'))
        if (payload.get('publication_state') != 'committed'
                or payload.get('content_signature') != signature):
            return False
        generation = (batch_path / '.generations' / payload['generation']).resolve()
        if generation.parent != (batch_path / '.generations').resolve():
            return False
        if Path(payload['generation_dir']).resolve() != generation:
            return False
        if not (generation / 'snapshot.json').is_file():
            return False
        hashes = payload['generation_hashes']
        if not isinstance(hashes, dict) or 'merged_metrics.csv' not in hashes:
            return False
        for name, expected in hashes.items():
            source = (generation / name).resolve()
            if not path_is_relative_to(source, generation):
                return False
            if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                return False
            alias = (batch_path / name).resolve()
            if not path_is_relative_to(alias, batch_path.resolve()):
                return False
            if hashlib.sha256(alias.read_bytes()).hexdigest() != expected:
                return False
        return all(path.read_bytes() == content for path, content in aliases.items())
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return False


def input_signature(batch_id: str, statuses: list[dict[str, Any]], final: bool) -> str:
    """Cheap polling key; final callers always bypass the validation cache."""
    fingerprints = []
    for status in statuses:
        candidates = []
        try:
            source = contained_result(status)
        except (OSError, ValueError):
            source = None
        if source is not None:
            candidates.extend([source, source.parent / 'result_scopes.json'])
            index_path = source.parent / 'result_scopes.json'
            if index_path.is_file():
                roots = set()
                try:
                    payload = json.loads(index_path.read_text(encoding='utf-8'))
                    output_root = Path(status['host_output_dir']).resolve()
                    entries = payload.get('rows', payload.get('entries', []))
                    for entry in entries:
                        scope = entry.get('scope') or {}
                        raw_paths = [entry.get('evaluation_file'), entry.get('report_file'),
                                     scope.get('source_csv'), scope.get('result_csv'),
                                     scope.get('artifact_dir'), scope.get('experiment_dir'),
                                     *scope.get('accuracy_results', [])]
                        for raw in filter(None, raw_paths):
                            path = result_validation.artifact_path(raw, output_root, source.parent, payload.get('output_root'))
                            archive_root = path if path.is_dir() else path.parent
                            for ancestor in (archive_root, *archive_root.parents):
                                if not path_is_relative_to(ancestor, output_root):
                                    break
                                if (ancestor / 'archive_manifest.json').is_file():
                                    archive_root = ancestor
                                    break
                            roots.add(archive_root)
                    # Include names and all artifacts, not just the manifest or
                    # selected CSV. Changes to DB/NPY/accuracy evidence must
                    # invalidate the live cache before final verification too.
                    roots = {path for path in roots if not any(parent != path and path_is_relative_to(path, parent)
                                                               for parent in roots)}
                    for archive_root in sorted(roots):
                        candidates.append(archive_root)
                        candidates.extend(sorted(archive_root.rglob('*')))
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    fingerprints.append((str(index_path), 'invalid_scope_inventory'))
        if status.get('state') in TERMINAL_STATES or final:
            if source is not None:
                candidates.append(source.parent / 'execution.log')
            if status.get('launcher_log'):
                candidates.append(Path(status['launcher_log']))
        for path in candidates:
            try:
                stat = path.lstat()
                value = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_mode)
            except OSError:
                value = None
            fingerprints.append((str(path), value))
    payload = {'batch_id': batch_id, 'final': final, 'tasks': stable_status(statuses),
               'sources': fingerprints}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def publish_batch(batch_path: Path, batch_id: str, statuses: list[dict[str, Any]],
                  final: bool = False) -> list[str]:
    """Commit an immutable generation; the root JSON is the commit pointer.

    Compatibility CSV/index aliases are conveniences. Readers needing a
    consistent snapshot follow generation_dir and verify generation_hashes.
    Failed publication withdraws aliases instead of leaving an old success.
    """
    cache_key = (str(batch_path.resolve()), batch_id)
    poll_signature = input_signature(batch_id, statuses, final)
    cached = _PUBLICATIONS.get(cache_key)
    if not final and cached and cached[0] == poll_signature:
        if committed_unchanged(batch_path, cached[1], cached[2]):
            return list(cached[3])
    _PUBLICATIONS.pop(cache_key, None)
    merged, errors, published, aliases = [], [], [], {}
    for original in statuses:
        status = dict(original)
        label = status.get('output_label', '')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', label):
            raise ValueError('unsafe output label')
        task_path = batch_path / 'tasks' / label
        task_path.mkdir(parents=True, exist_ok=True)
        output = Path(status['host_output_dir'])
        named_csv = output / (label + '__all_results.csv')
        status.update(source_csv='', result_csv='', results_state='pending', results_error='',
                      results_error_code='', results_error_message='',
                      completed_tests=0, passed_tests=0, peak_tests=0, failed_tests=0,
                      peak_rounds=0, named_launcher_log='')
        terminal = status.get('state') in TERMINAL_STATES
        merged_before = len(merged)
        stage = 'result_path'
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
                index = load_scope_index(source.parent)
                stage = 'numeric_schema'
                accuracy_rows = {ordinal for ordinal, entry in (index or {}).items()
                                 if entry.get('test_accuracy') or entry.get('accuracy')
                                 or isinstance(entry.get('scope'), dict) and
                                 entry['scope'].get('test_accuracy', entry['scope'].get('accuracy', False))}
                content = source.read_bytes()
                rows = metric_rows(content, accuracy_rows)
                if not rows and terminal and status.get('state') == 'success':
                    raise ValueError('successful worker has no metric rows')
                finals = [row for row in rows if row['status'] != 'peak_round']
                status['completed_tests'] = len(finals)
                status['passed_tests'] = sum(row['status'] == 'passed' for row in rows)
                status['peak_tests'] = sum(row['status'] in SUCCESS_STATES - {'passed'} for row in rows)
                status['failed_tests'] = sum(row['status'] not in SUCCESS_STATES for row in finals)
                status['peak_rounds'] = len(rows) - len(finals)
                identities = status.get('planned_scenarios')
                stage = 'planned_identity'
                if terminal and identities is not None:
                    actual_identities = [row['scenario'] for row in finals]
                    if actual_identities != identities[:len(actual_identities)]:
                        raise ValueError('unexpected or duplicated completed scenario identity')
                if terminal and status.get('state') == 'success':
                    stage = 'planned_identity'
                    if status['failed_tests']:
                        raise ValueError('successful worker contains failed scenario')
                    planned = status.get('planned_tests', 0)
                    if isinstance(planned, int) and planned > 0 and len(finals) != planned:
                        raise ValueError('incomplete successful result set')
                    if identities is not None and [row['scenario'] for row in finals] != identities:
                        raise ValueError('planned scenario identities differ')
                if terminal and (index is not None or status.get('result_scopes')):
                    stage = 'scope_provenance'
                    checks = result_validation.validate_results(source.parent, output, require_scopes=bool(status.get('result_scopes')))
                    if any(not ok for name, ok, _ in checks
                           if name not in {'final states successful', 'csv no failed/no_result'}):
                        raise ValueError('scope provenance validation failed')
                merged.extend({**{key: str(status.get(key, '')) for key in IDENTITY_COLUMNS}, **row} for row in rows)
                aliases[named_csv] = content
                status.update(result_csv=str(named_csv), results_state='ready' if terminal else 'running')
            if terminal or final:
                stage = 'log_artifacts'
                launcher = Path(status['launcher_log']) if status.get('launcher_log') else None
                if launcher and launcher.is_file():
                    target = output / (label + '__launcher.log')
                    aliases[target] = launcher.read_bytes()
                    status['named_launcher_log'] = str(target)
                if source and (source.parent / 'execution.log').is_file():
                    aliases[output / (label + '__execution.log')] = (source.parent / 'execution.log').read_bytes()
        except (OSError, UnicodeError, ValueError, csv.Error):
            del merged[merged_before:]
            if terminal or final:
                messages = {'result_path': '结果文件缺失或路径不属于本任务', 'numeric_schema': '结果列、数值或状态不合法',
                            'planned_identity': '已完成场景与计划不一致，或成功任务中含失败场景',
                            'scope_provenance': '原始测量范围、计数或指标证据核验失败',
                            'log_artifacts': '日志证据无法读取或归档'}
                status.update(results_state='invalid', results_error='metrics_or_artifact_invalid', result_csv='',
                              results_error_code=stage + '_invalid', results_error_message=messages[stage])
                errors.append(str(status.get('task', label)) + ': ' + stage + '_invalid')
                aliases.pop(named_csv, None)
                unlink_if_present(named_csv)
            else:
                status['results_state'] = 'pending'
        published.append(status)
    metrics = csv_bytes(IDENTITY_COLUMNS + METRIC_COLUMNS, merged)
    signature_data = {
        'batch_id': batch_id, 'final': final, 'tasks': stable_status(published),
        'artifact_errors': errors,
        'metrics_sha256': hashlib.sha256(metrics).hexdigest(),
        'aliases': {str(path): hashlib.sha256(content).hexdigest()
                    for path, content in sorted(aliases.items(), key=lambda item: str(item[0]))},
    }
    signature = hashlib.sha256(json.dumps(signature_data, sort_keys=True, ensure_ascii=False,
                                         allow_nan=False, separators=(',', ':')).encode('utf-8')).hexdigest()
    if committed_unchanged(batch_path, signature, aliases):
        _PUBLICATIONS[cache_key] = (poll_signature, signature, dict(aliases), list(errors))
        return errors
    generation = uuid.uuid4().hex
    generation_dir = batch_path / '.generations' / generation
    files = {'merged_metrics.csv': metrics}
    columns = list(dict.fromkeys(key for status in published for key in status))
    if columns:
        files['batch_results.csv'] = csv_bytes(columns, published)
    for status in published:
        name = 'tasks/' + status['output_label'] + '/result_index.json'
        files[name] = (json.dumps({**status, 'generation': generation}, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    payload = {'schema_version': 3, 'batch_id': batch_id, 'generation': generation,
               'content_signature': signature,
               'generation_dir': str(generation_dir), 'generation_hashes': hashes,
               'publication_state': 'committed', 'metrics_csv': str(generation_dir / 'merged_metrics.csv'),
               'artifact_errors': errors, 'tasks': published}
    for name, content in files.items():
        target = batch_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        aliases[target] = content
    try:
        generation_dir.mkdir(parents=True, exist_ok=False)
        for name, content in files.items():
            path = generation_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_bytes(path, content)
        atomic_json(generation_dir / 'snapshot.json', payload)
        for path, content in aliases.items():
            atomic_bytes(path, content)
        atomic_json(batch_path / 'batch_results.json', payload)
    except OSError:
        for path in aliases:
            try:
                unlink_if_present(path)
            except OSError:
                pass
        payload.update(publication_state='failed', metrics_csv='',
                       artifact_errors=[*errors, 'batch_publication_failed'])
        try:
            atomic_json(batch_path / 'batch_results.json', payload)
        except OSError:
            pass  # Caller must retain the failure in its independent log/state.
        raise
    _PUBLICATIONS[cache_key] = (poll_signature, signature, dict(aliases), list(errors))
    return errors
