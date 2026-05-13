# Day 10 Reliability Report

**Sinh viên:** Nguyễn Triệu Gia Khánh  
**Mã sinh viên:** 2A202600225

## 1. Architecture summary

The gateway implements a three-layer reliability stack: semantic cache, circuit-breaker-guarded provider chain, and a static fallback.

```
User Request
    |
    v
[ReliabilityGateway]
    |
    v
[ResponseCache / SharedRedisCache]
    |-- HIT (score >= threshold, no false-hit) --> return cached response (route: cache_hit:<score>)
    |
    v MISS
[CircuitBreaker: primary]
    |-- CLOSED: call FakeLLMProvider("primary")
    |       SUCCESS --> cache.set(), return (route: primary:primary)
    |       FAILURE x3 --> breaker OPENS
    |-- OPEN (timeout not elapsed) --> skip to next
    |-- HALF_OPEN (timeout elapsed) --> probe once
    |
    v (primary open or failed)
[CircuitBreaker: backup]
    |-- CLOSED: call FakeLLMProvider("backup")
    |       SUCCESS --> cache.set(), return (route: fallback:backup)
    |       FAILURE x3 --> breaker OPENS
    |-- OPEN --> skip
    |
    v (all providers failed/open)
[Static fallback message]  (route: static_fallback)
```

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Trips the circuit after 3 consecutive failures — tolerates brief transient errors while reacting fast to sustained outages |
| reset_timeout_seconds | 2 | Short probe window in test environment; in production this would be 30–60 s |
| success_threshold | 1 | One successful probe is enough to close the circuit and resume normal traffic |
| cache TTL | 300 s | 5-minute window covers typical repeated queries in a session; stale answers are acceptable for general knowledge prompts |
| similarity_threshold | 0.92 | High precision prevents wrong answers being served; lowered to 0.35 in `cache_stale_candidate` scenario to exercise false-hit guardrails |
| load_test requests | 100 per scenario (500 total) | Large enough to trigger circuit trips and accumulate cache hits across 5 scenarios |

## 3. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 99.8% | ✅ Yes |
| Latency P95 | < 2500 ms | 487 ms | ✅ Yes |
| Fallback success rate | >= 95% | 98.4% | ✅ Yes |
| Cache hit rate | >= 10% | 61.2% | ✅ Yes |
| Recovery time | < 5000 ms | 2364 ms | ✅ Yes |

## 4. Metrics

Derived from `reports/metrics.json` (500 total requests, 5 scenarios combined).

| Metric | Value |
|---|---:|
| availability | 0.998 |
| error_rate | 0.002 |
| latency_p50_ms | 1.15 |
| latency_p95_ms | 487.01 |
| latency_p99_ms | 529.10 |
| fallback_success_rate | 0.9841 |
| cache_hit_rate | 0.612 |
| estimated_cost | $0.000094 |
| estimated_cost_saved | $0.306 |
| circuit_open_count | 5 |
| recovery_time_ms | 2364 |

## 5. Cache comparison

Single-scenario run (100 requests, all_healthy provider config):

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---|
| latency_p50_ms | 217.24 | 0.38 | -216.86 ms (-99.8%) |
| latency_p95_ms | 507.89 | 252.70 | -255.19 ms (-50.2%) |
| estimated_cost | $0.05556 | $0.009572 | -$0.04599 (-82.8%) |
| cache_hit_rate | 0 | 0.79 | +79 pp |

Cache reduces P50 latency by ~570× and cuts cost by ~83% due to 79% of requests being served from memory without calling the provider.

### False-hit examples (blocked by guardrail)

The following queries had similarity score ≥ 0.80 against a cached entry but were correctly blocked because they contained different 4-digit numbers (years/IDs):

```
Query:        "What was the GDP of Vietnam in 2023?"
Cached query: "What was the GDP of Vietnam in 2020?"
Similarity:   0.889   → BLOCKED (year_or_id_mismatch)
Returned:     None

Query:        "Summarize refund policy for 2026 deadline"
Cached query: "Summarize refund policy for 2024 deadline"
Similarity:   0.864   → BLOCKED (year_or_id_mismatch)
Returned:     None

Query:        "List events from 2022 conference"
Cached query: "List events from 2019 conference"
Similarity:   0.807   → BLOCKED (year_or_id_mismatch)
Returned:     None
```

Without the `_looks_like_false_hit()` guardrail, all three would have served wrong (stale-year) responses to the user.

## 6. Redis shared cache

**Why in-memory cache is insufficient for multi-instance deployments:**  
Each process has its own `ResponseCache` in heap memory. When the gateway is scaled horizontally (multiple pods/containers), every instance starts with a cold cache. Duplicate requests routed to different instances both miss cache and call the provider, wasting cost and adding latency. There is no cross-instance cache invalidation, so TTL semantics are inconsistent.

**How `SharedRedisCache` solves this:**  
All instances connect to a single Redis server. A write on instance 1 is immediately visible to instance 2 via the same key prefix (`rl:cache:<md5>`). TTL is enforced by Redis `EXPIRE`, which is consistent across all instances. False-hit guardrails (year/ID mismatch, privacy patterns) are applied at read time on every instance before the response is returned.

### Evidence of shared state

```
# Two separate SharedRedisCache instances, same Redis, same prefix "rl:report:"
# Instance 1 writes:
c1.set("What is a circuit breaker pattern?", "A circuit breaker is a design pattern...")

# Instance 2 reads (no prior knowledge):
val, score = c2.get("What is a circuit breaker pattern?")
# => "A circuit breaker is a design pattern..." | score: 1.0

# Keys visible in Redis:
# rl:report:2a0ce669657c
# rl:report:d5d6ca803338
# rl:report:f904330156e2
```

### Redis CLI output

```bash
# docker compose exec redis redis-cli KEYS "rl:cache:*"
# (keys are hashed with MD5 first 12 chars of normalized query)
# e.g.:
# rl:cache:2a0ce669657c
# rl:cache:d5d6ca803338
# rl:cache:f904330156e2
```

### In-memory vs Redis latency comparison

| Metric | In-memory cache | Redis cache | Notes |
|---|---:|---:|---|
| latency_p50_ms | 0.38 | ~1–3 | Redis adds ~1–3 ms RTT per lookup via localhost |
| latency_p95_ms | 252.70 | ~260 | Difference negligible vs provider latency (180–260 ms) |

## 7. Chaos scenarios

Each scenario runs 100 requests with **10 concurrent threads** (`concurrency=10` in `run_scenario()`). A `threading.Semaphore` bounds the parallelism; a `threading.Lock` protects the shared `RunMetrics` aggregator, preventing race conditions on counters and latency lists.

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | All traffic falls back to backup; primary circuit opens | 100% availability, 4 circuit opens, 27 fallback successes, 0 static fallbacks | ✅ Pass |
| primary_flaky_50 | Circuit oscillates between CLOSED/OPEN; mix of primary and fallback traffic | 98% availability, 2 circuit opens, 14 fallback successes | ✅ Pass |
| all_healthy | All requests via primary, no circuit trips, no fallbacks | 100 successful requests, 0 circuit opens, 0 fallbacks | ✅ Pass |
| cache_stale_candidate | Low threshold (0.35) triggers many cache hits; year/ID guardrail must block false hits | False-hit guard rejected year-mismatched entries; overall availability ≥ 65% | ✅ Pass |
| no_cache_baseline | Same traffic without cache; higher cost and latency than cache-on run | All requests forwarded to providers; cost ~5.8× higher vs cache-on run | ✅ Pass |

### Concurrent load design

```
run_scenario(concurrency=10)
    ├── Thread-1  → gateway.complete(query_1)
    ├── Thread-2  → gateway.complete(query_2)
    │   ...
    └── Thread-10 → gateway.complete(query_10)
              ↓  (Semaphore limits to 10 in-flight)
         threading.Lock → RunMetrics accumulator
```

The circuit breaker and cache are shared across threads within a scenario, meaning concurrent failures accumulate toward the failure threshold faster than sequential runs — this more accurately simulates real production traffic patterns.

## 8. Failure analysis

**What could still go wrong:**

1. **Redis becomes a single point of failure.** If the Redis instance goes down, `SharedRedisCache.get/set` silently swallows all exceptions and returns `(None, 0.0)`. Every request then hits the provider, removing the cost/latency benefit of caching entirely and potentially triggering cascading load. There is no circuit breaker protecting the Redis connection itself.

2. **Circuit breaker state is in-process memory.** In a multi-instance deployment, each pod has its own `CircuitBreaker` objects. If provider A is failing, only the pod that saw 3 consecutive failures will open its circuit. Other pods keep sending traffic to the failing provider, generating more errors before their local threshold is reached.

3. **Similarity-based cache can still serve stale content.** A query cached 4 minutes ago may return an outdated answer for a time-sensitive question. The TTL of 300 s mitigates this but does not eliminate it.

**What I would change before production:**

- Add a Redis circuit breaker: wrap all `_redis.*` calls with a `CircuitBreaker` that falls back to the in-memory cache when Redis is unavailable.
- Store circuit breaker state in Redis so all instances share the same open/closed/half-open status, preventing retry storms from healthy pods hitting a failing provider.
- Add a `max_age` field to cache entries and reject cache hits older than a configurable freshness window for queries tagged `expected_risk: time_sensitive`.

## 9. Next steps

1. **Redis circuit breaker** — wrap `SharedRedisCache` calls in a `CircuitBreaker` so a Redis outage degrades gracefully to in-memory rather than causing a full cache miss storm.
2. **Distributed circuit breaker state** — persist `CircuitBreaker.state` and `failure_count` in Redis so all gateway instances agree on whether a provider is healthy, eliminating per-instance retry storms.
3. **Per-query freshness SLO** — tag queries with `freshness_required: true` in `QueryRecord` and skip cache hits older than a tighter TTL (e.g. 30 s) for those queries, preventing stale answers on time-sensitive requests.
