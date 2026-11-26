"""Prepare native AISBench prefix data on every rank behind one vLLM API."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


_METRIC = re.compile(r'^vllm:(prefix_cache_(?:queries|hits)_total|cache_config_info)\{([^}]*)\}\s+(\S+)')
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def _request(url, headers, *, data=None, timeout=10):
    return build_opener(ProxyHandler({})).open(Request(url, data=data, headers=headers), timeout=timeout)


def _read_metrics(base_url, headers, model):
    metrics = {"prefix_cache_queries_total": {}, "prefix_cache_hits_total": {}, "cache_config_info": {}}
    with _request(base_url + "/metrics", headers) as response:
        for line in response.read().decode().splitlines():
            match = _METRIC.match(line)
            if not match:
                continue
            name, raw_labels, value = match.groups()
            labels = dict(_LABEL.findall(raw_labels))
            if "engine" not in labels or labels.get("model_name", model) != model:
                continue
            rank = int(labels["engine"])
            metrics[name][rank] = labels if name == "cache_config_info" else int(float(value))
    return metrics


def _send_prefix(base_url, auth, body, rank):
    headers = {**auth, "Content-Type": "application/json", "X-data-parallel-rank": str(rank)}
    try:
        with _request(base_url + "/v1/chat/completions", headers, data=body, timeout=300) as response:
            usage = json.load(response).get("usage") or {}
    except Exception as error:
        raise RuntimeError(f"Prefill rank {rank} request failed: {error}") from error
    tokens = usage.get("prompt_tokens")
    if not isinstance(tokens, int) or tokens < 1 or usage.get("completion_tokens") != 1:
        raise RuntimeError(f"Prefill rank {rank} did not report a positive prompt and one completion token")
    return tokens


def _send_to_ranks(base_url, auth, body, model, ranks, requested):
    counter = "prefix_cache_queries_total"
    before = _read_metrics(base_url, auth, model)
    with ThreadPoolExecutor(max_workers=min(2, len(requested))) as pool:
        pending = {rank: pool.submit(_send_prefix, base_url, auth, body, rank) for rank in requested}
        tokens = {rank: future.result() for rank, future in pending.items()}
    expected = {rank: tokens.get(rank, 0) for rank in ranks}
    deadline = time.monotonic() + 5
    while True:
        after = _read_metrics(base_url, auth, model)
        actual = {rank: after[counter][rank] - before[counter][rank] for rank in ranks}
        if actual == expected:
            break
        if any(actual[rank] > expected[rank] for rank in ranks) or time.monotonic() >= deadline:
            raise RuntimeError(f"Prefill rank query counters disagree with the requested routing: {actual}, expected {expected}")
        time.sleep(0.2)
    hits = "prefix_cache_hits_total"
    return {
        rank: {"prompt_tokens": count, "cached_tokens": after[hits][rank] - before[hits][rank]}
        for rank, count in tokens.items()
    }


def _required_hit(prompt_tokens, cache_config):
    alignment = cache_config.get("cache_hit_alignment_tokens", "")
    if not alignment.isdecimal() or int(alignment) == 0:
        alignment = cache_config.get("block_size", "")
    if not alignment.isdecimal() or int(alignment) == 0:
        return None
    block_size = int(alignment)
    return (prompt_tokens - 1) // block_size * block_size


def warm_prefill_ranks(url, dataset, model, dp, *, api_key="", enable_thinking=False):
    """Use the generated GSM8K prefix data, leaving both AISBench scopes intact."""
    base_url = url.strip().rstrip("/")
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.path or parts.query or parts.fragment or parts.username:
        raise ValueError("PREFILL_RANK_WARMUP_URL must be the direct Prefill base URL, for example http://127.0.0.1:8367")
    with Path(dataset).open(encoding="utf-8") as stream:
        prefixes = list(dict.fromkeys(json.loads(line)["question"] for line in stream if line.strip()))
    if not prefixes or not all(isinstance(prefix, str) and prefix for prefix in prefixes):
        raise ValueError("Rank warmup requires non-empty GSM8K prefix questions")

    auth = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    metrics = _read_metrics(base_url, auth, model)
    ranks = sorted(metrics["prefix_cache_queries_total"])
    if len(ranks) != dp or set(metrics["prefix_cache_hits_total"]) != set(ranks):
        raise RuntimeError(f"Expected {dp} Prefill ranks with query and hit counters on one endpoint, found {ranks}")
    config = metrics["cache_config_info"]
    if any(config.get(rank, {}).get("enable_prefix_caching") == "False" for rank in ranks):
        raise RuntimeError("Prefill prefix caching is disabled")

    results = []
    bodies = []
    for prefix in prefixes:
        request = {
            "model": model, "messages": [{"role": "user", "content": prefix}],
            "max_tokens": 1, "temperature": 0, "ignore_eos": True, "stream": False,
        }
        if enable_thinking:
            request["chat_template_kwargs"] = {"enable_thinking": True}
        body = json.dumps(request, ensure_ascii=False).encode()
        bodies.append(body)
        results.append(_send_to_ranks(base_url, auth, body, model, ranks, ranks))

    for body, result in zip(bodies, results):
        required = {r: _required_hit(result[r]["prompt_tokens"], config.get(r, {})) for r in ranks}
        verify = [
            r for r in ranks
            if required[r] and (len(prefixes) > 1 or result[r]["cached_tokens"] < required[r])
        ]
        if verify:
            repeated = _send_to_ranks(base_url, auth, body, model, ranks, verify)
            result.update(repeated)
        for rank in ranks:
            result[rank]["required_cached_tokens"] = required[rank]
            if required[rank] and result[rank]["cached_tokens"] < required[rank]:
                raise RuntimeError(f"Prefill rank {rank} did not reuse the full cacheable prefix")
    return {"state": "pass", "endpoint": base_url, "ranks": ranks, "prefixes": results}
