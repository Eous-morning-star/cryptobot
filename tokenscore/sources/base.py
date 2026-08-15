"""
Shared contract for every data source.

The whole point: ten sources return wildly different shapes. Normalise at the
edge so the scoring engine never learns any source's quirks. Adding an
eleventh source should mean writing one file and touching nothing else.
"""
from __future__ import annotations

import abc
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp


@dataclass
class SourceResult:
    """Normalised output from a single source."""

    source: str
    ok: bool
    latency_ms: int = 0

    # 0..1 quality signal. None means "this source only gates, it doesn't score."
    subscore: float | None = None

    # 0..1 — how much this source actually knows about this token.
    # A brand-new token has no volume history, so Birdeye should report low
    # confidence rather than a confidently bad score.
    confidence: float = 1.0

    # Fatal findings. Any non-empty value here rejects the token outright,
    # regardless of how good every other source looks.
    gate_failures: list[str] = field(default_factory=list)

    # Non-fatal observations, surfaced in the alert for human judgement.
    flags: list[str] = field(default_factory=list)

    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def failed(cls, source: str, error: str, latency_ms: int = 0) -> SourceResult:
        """A source being down is NOT a gate failure — it's missing information.

        This distinction matters. Treating an API timeout as a red flag makes
        the scorer flaky; treating it as a pass makes it dangerous. Correct
        behaviour is to score without it and lower overall confidence.
        """
        return cls(
            source=source,
            ok=False,
            latency_ms=latency_ms,
            subscore=None,
            confidence=0.0,
            error=error,
        )


class SourceAdapter(abc.ABC):
    """Base class for all sources.

    Subclasses implement `_fetch`. The base handles timing, timeouts and
    error isolation so one flaky API can never take down an evaluation.
    """

    name: str = "unnamed"
    timeout_s: float = 8.0
    requires_key: bool = False

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key

    @property
    def available(self) -> bool:
        return not self.requires_key or bool(self.api_key)

    @abc.abstractmethod
    async def _fetch(
        self, session: aiohttp.ClientSession, address: str, chain: str
    ) -> SourceResult:
        ...

    async def evaluate(
        self, session: aiohttp.ClientSession, address: str, chain: str = "solana"
    ) -> SourceResult:
        if not self.available:
            return SourceResult.failed(self.name, "missing API key")

        start = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                self._fetch(session, address, chain), timeout=self.timeout_s
            )
            result.latency_ms = int((time.perf_counter() - start) * 1000)
            return result
        except asyncio.TimeoutError:
            elapsed = int((time.perf_counter() - start) * 1000)
            return SourceResult.failed(self.name, "timeout", elapsed)
        except Exception as exc:  # noqa: BLE001 - deliberate: isolate every source
            elapsed = int((time.perf_counter() - start) * 1000)
            return SourceResult.failed(self.name, f"{type(exc).__name__}: {exc}", elapsed)


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def scale(value: float, floor: float, ceiling: float) -> float:
    """Map a raw metric onto 0..1 with saturation at both ends.

    Saturation matters: $10M of liquidity is not ten times safer than $1M,
    and treating it that way lets one huge number dominate the average.
    """
    if ceiling <= floor:
        return 0.0
    return clamp((value - floor) / (ceiling - floor))
