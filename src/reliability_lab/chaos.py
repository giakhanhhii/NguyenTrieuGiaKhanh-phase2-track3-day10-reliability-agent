from __future__ import annotations

import json
import random
import threading
from dataclasses import dataclass
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


@dataclass(frozen=True, slots=True)
class QueryRecord:
    query: str
    expected_risk: str = "technical"


def load_query_records(path: str | Path = "data/sample_queries.jsonl") -> list[QueryRecord]:
    records: list[QueryRecord] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        records.append(
            QueryRecord(
                query=obj["query"],
                expected_risk=str(obj.get("expected_risk", "technical")),
            )
        )
    return records


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    return [r.query for r in load_query_records(path)]


def build_gateway(
    config: LabConfig,
    provider_overrides: dict[str, float] | None = None,
    scenario: ScenarioConfig | None = None,
) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache_enabled = config.cache.enabled
    ttl_seconds = config.cache.ttl_seconds
    similarity_threshold = config.cache.similarity_threshold
    if scenario is not None:
        if scenario.cache_enabled is not None:
            cache_enabled = scenario.cache_enabled
        if scenario.cache_similarity_threshold is not None:
            similarity_threshold = scenario.cache_similarity_threshold

    cache: ResponseCache | SharedRedisCache | None = None
    if cache_enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                ttl_seconds,
                similarity_threshold,
            )
        else:
            cache = ResponseCache(ttl_seconds, similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs.

    Recovery time = time between circuit opening and next successful close.
    Returns the average recovery time across all breakers, or None if no recovery occurred.
    """
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open" and open_ts is None:
                open_ts = float(entry["ts"])
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((float(entry["ts"]) - open_ts) * 1000)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def _scenario_passes(name: str, result: RunMetrics) -> bool:
    if result.total_requests == 0:
        return False
    total = result.total_requests
    if name == "primary_timeout_100":
        return result.fallback_success_rate >= 0.85
    if name == "primary_flaky_50":
        return result.successful_requests > 0 and (
            result.circuit_open_count > 0 or result.fallback_successes > max(1, total // 50)
        )
    if name == "cache_stale_candidate":
        return result.successful_requests / total >= 0.65
    if name == "no_cache_baseline":
        return result.successful_requests > 0
    if name == "all_healthy":
        return result.successful_requests / total >= 0.45
    return result.successful_requests > 0


def run_scenario(
    config: LabConfig,
    query_records: list[QueryRecord],
    scenario: ScenarioConfig,
    concurrency: int = 10,
) -> RunMetrics:
    """Run a single named chaos scenario with concurrent load.

    Uses a thread pool to simulate multiple simultaneous callers hitting the
    gateway. A lock protects the shared RunMetrics aggregator.
    """
    gateway = build_gateway(config, scenario.provider_overrides or None, scenario)
    metrics = RunMetrics()
    lock = threading.Lock()
    request_count = config.load_test.requests

    def _worker(record: QueryRecord) -> None:
        result = gateway.complete(
            record.query,
            query_metadata={"expected_risk": record.expected_risk},
        )
        with lock:
            metrics.total_requests += 1
            metrics.estimated_cost += result.estimated_cost
            if result.cache_hit:
                metrics.cache_hits += 1
                metrics.estimated_cost_saved += 0.001
            if result.route.startswith("fallback:"):
                metrics.fallback_successes += 1
                metrics.successful_requests += 1
            elif result.route == "static_fallback":
                metrics.static_fallbacks += 1
                metrics.failed_requests += 1
            else:
                metrics.successful_requests += 1
            if result.latency_ms:
                metrics.latencies_ms.append(result.latency_ms)

    records_to_run = [random.choice(query_records) for _ in range(request_count)]
    threads = []
    semaphore = threading.Semaphore(concurrency)

    def _bounded_worker(record: QueryRecord) -> None:
        with semaphore:
            _worker(record)

    for record in records_to_run:
        t = threading.Thread(target=_bounded_worker, args=(record,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    metrics.circuit_open_count = sum(
        1 for breaker in gateway.breakers.values() for t in breaker.transition_log if t["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics


def run_simulation(config: LabConfig, query_records: list[QueryRecord]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined."""
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, query_records, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        return metrics

    combined = RunMetrics()
    for scenario in config.scenarios:
        result = run_scenario(config, query_records, scenario)

        combined.scenarios[scenario.name] = "pass" if _scenario_passes(scenario.name, result) else "fail"

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    return combined
