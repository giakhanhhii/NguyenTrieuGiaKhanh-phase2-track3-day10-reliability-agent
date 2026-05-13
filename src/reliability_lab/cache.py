from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _is_high_risk_metadata(cache_context: dict[str, str] | None) -> bool:
    """Privacy-tagged queries must not be cached or served from cache."""
    if not cache_context:
        return False
    return cache_context.get("expected_risk") == "privacy"


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """TTL in-memory cache with similarity lookup and guardrails."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []

    def get(self, query: str, cache_context: dict[str, str] | None = None) -> tuple[str | None, float]:
        if _is_uncacheable(query) or _is_high_risk_metadata(cache_context):
            return None, 0.0
        best_value: str | None = None
        best_key: str | None = None
        best_score = 0.0
        now = time.time()
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]
        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_value = entry.value
                best_key = entry.key
        if best_score >= self.similarity_threshold and best_key is not None and best_value is not None:
            if _looks_like_false_hit(query, best_key):
                return None, best_score
            return best_value, best_score
        return None, best_score

    def set(
        self,
        query: str,
        value: str,
        metadata: dict[str, str] | None = None,
        *,
        cache_context: dict[str, str] | None = None,
    ) -> None:
        if _is_uncacheable(query) or _is_high_risk_metadata(cache_context):
            return
        self._entries.append(CacheEntry(query, value, time.time(), metadata or {}))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Blend sequence alignment, token Jaccard, and character bigram overlap."""
        a_norm = a.lower().strip()
        b_norm = b.lower().strip()
        if not a_norm or not b_norm:
            return 0.0
        seq = SequenceMatcher(None, a_norm, b_norm).ratio()
        ta, tb = set(a_norm.split()), set(b_norm.split())
        jacc = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
        bigrams_a = {a_norm[i : i + 2] for i in range(len(a_norm) - 1)} if len(a_norm) > 1 else set()
        bigrams_b = {b_norm[i : i + 2] for i in range(len(b_norm) - 1)} if len(b_norm) > 1 else set()
        bi = len(bigrams_a & bigrams_b) / len(bigrams_a | bigrams_b) if bigrams_a and bigrams_b else 0.0
        return (seq + jacc + bi) / 3.0


# ---------------------------------------------------------------------------
# Redis shared cache
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments."""

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str, cache_context: dict[str, str] | None = None) -> tuple[str | None, float]:
        if _is_uncacheable(query) or _is_high_risk_metadata(cache_context):
            return None, 0.0
        try:
            exact_key = f"{self.prefix}{self._query_hash(query)}"
            direct = self._redis.hget(exact_key, "response")
            if direct is not None:
                stored_q = self._redis.hget(exact_key, "query") or ""
                if _looks_like_false_hit(query, stored_q):
                    self.false_hit_log.append(
                        {"query": query, "cached_query": stored_q, "score": 1.0, "reason": "year_or_id_mismatch"}
                    )
                    return None, 1.0
                return direct, 1.0

            best_score = 0.0
            best_response: str | None = None
            best_cached_query: str | None = None
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                cached_q = self._redis.hget(key, "query")
                if not cached_q:
                    continue
                score = ResponseCache.similarity(query, cached_q)
                if score > best_score:
                    best_score = score
                    raw = self._redis.hget(key, "response")
                    best_response = raw if raw is not None else None
                    best_cached_query = cached_q
            if best_score >= self.similarity_threshold and best_response is not None and best_cached_query is not None:
                if _looks_like_false_hit(query, best_cached_query):
                    self.false_hit_log.append(
                        {
                            "query": query,
                            "cached_query": best_cached_query,
                            "score": best_score,
                            "reason": "year_or_id_mismatch",
                        }
                    )
                    return None, best_score
                return best_response, best_score
            return None, best_score
        except Exception:
            return None, 0.0

    def set(
        self,
        query: str,
        value: str,
        metadata: dict[str, str] | None = None,
        *,
        cache_context: dict[str, str] | None = None,
    ) -> None:
        if _is_uncacheable(query) or _is_high_risk_metadata(cache_context):
            return
        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            mapping: dict[str, str] = {"query": query, "response": value}
            if metadata:
                for k, v in metadata.items():
                    mapping[f"meta:{k}"] = v
            self._redis.hset(key, mapping=mapping)
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            return

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        try:
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                self._redis.delete(key)
        except Exception:
            return

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
