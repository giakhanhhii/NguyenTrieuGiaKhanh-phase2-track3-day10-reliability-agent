from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache

    def complete(self, prompt: str, *, query_metadata: dict[str, str] | None = None) -> GatewayResponse:
        """Return a reliable response or a static fallback.

        Latency covers end-to-end time in this gateway including cache and breaker overhead.
        """
        t0 = time.perf_counter()

        def elapsed_ms() -> float:
            return (time.perf_counter() - t0) * 1000.0

        cache_ctx = dict(query_metadata) if query_metadata else {}

        if self.cache is not None:
            cached, score = self.cache.get(prompt, cache_ctx)
            if cached is not None:
                return GatewayResponse(
                    cached,
                    f"cache_hit:{score:.2f}",
                    None,
                    True,
                    elapsed_ms(),
                    0.0,
                )

        last_error: str | None = None
        for idx, provider in enumerate(self.providers):
            breaker = self.breakers[provider.name]
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
                if self.cache is not None:
                    self.cache.set(
                        prompt,
                        response.text,
                        {"provider": provider.name},
                        cache_context=cache_ctx,
                    )
                if idx == 0:
                    route = f"primary:{provider.name}"
                else:
                    route = f"fallback:{provider.name}"
                return GatewayResponse(
                    text=response.text,
                    route=route,
                    provider=provider.name,
                    cache_hit=False,
                    latency_ms=elapsed_ms(),
                    estimated_cost=response.estimated_cost,
                )
            except (ProviderError, CircuitOpenError) as exc:
                last_error = str(exc)
                continue

        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=elapsed_ms(),
            estimated_cost=0.0,
            error=last_error,
        )
