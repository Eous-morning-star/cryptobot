"""
Scoring engine.

Three rules that keep this honest:

  1. GATES ARE ABSOLUTE. A single fatal finding rejects the token. It is never
     averaged against good news. This is the difference between a screener and
     a lottery ticket generator.

  2. MISSING != BAD. A source that errored contributes nothing and lowers
     overall confidence. It does not penalise the token.

  3. DISAGREEMENT IS SURFACED, NOT SMOOTHED. When two sources contradict each
     other, that goes in the alert. Averaging it away destroys the most useful
     information you have.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from ..sources.base import SourceAdapter, SourceResult


# Relative weights. These are starting guesses — retune them against the
# `outcomes` table once you have a few hundred labelled tokens.
DEFAULT_WEIGHTS: dict[str, float] = {
    "rugcheck": 1.4,      # most directly predictive of rugs
    "goplus": 1.3,        # authority + concentration
    "dexscreener": 1.0,   # market structure
    "birdeye": 0.9,       # holder dynamics
    "solscan": 0.6,       # mostly corroboration
    "social": 0.7,        # reddit + telegram narrative
    "early_buyers": 1.2,  # smart-money clustering
}

# Below this, don't alert regardless of score — you're guessing.
MIN_CONFIDENCE = 0.45


@dataclass
class Verdict:
    address: str
    verdict: str                      # "GATED" or "SCORED"
    score: float | None = None        # 0..100
    confidence: float = 0.0
    gate_failures: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)
    breakdown: dict[str, Any] = field(default_factory=dict)
    sources_ok: int = 0
    sources_total: int = 0

    @property
    def should_alert(self) -> bool:
        return (
            self.verdict == "SCORED"
            and self.confidence >= MIN_CONFIDENCE
            and (self.score or 0) >= 65
        )


def _detect_disagreements(results: list[SourceResult]) -> list[str]:
    """Find places where sources contradict each other.

    Two independent APIs disagreeing about mint authority means one of them is
    stale — and stale security data is exactly how a screener gets someone
    rugged. Always show it rather than picking a winner.
    """
    out: list[str] = []
    by_name = {r.source: r for r in results if r.ok}

    goplus, solscan = by_name.get("goplus"), by_name.get("solscan")
    if goplus and solscan:
        gp_mint = "mint_authority_active" in goplus.gate_failures
        ss_mint = "solscan_reports_mint_authority" in solscan.flags
        if gp_mint != ss_mint:
            out.append("goplus/solscan disagree on mint authority")

    # Wide spread in subscores means the sources are seeing different things.
    scored = [(r.source, r.subscore) for r in results if r.ok and r.subscore is not None]
    if len(scored) >= 3:
        values = [s for _, s in scored]
        if max(values) - min(values) > 0.55:
            hi = max(scored, key=lambda x: x[1])[0]
            lo = min(scored, key=lambda x: x[1])[0]
            out.append(f"wide spread: {hi} bullish, {lo} bearish")

    return out


def combine(address: str, results: list[SourceResult],
            weights: dict[str, float] | None = None) -> Verdict:
    weights = weights or DEFAULT_WEIGHTS

    ok_results = [r for r in results if r.ok]
    verdict = Verdict(
        address=address,
        verdict="SCORED",
        sources_ok=len(ok_results),
        sources_total=len(results),
    )

    # --- Rule 1: gates, before anything else -----------------------------
    for r in ok_results:
        verdict.gate_failures.extend(r.gate_failures)
        verdict.flags.extend(r.flags)

    if verdict.gate_failures:
        verdict.verdict = "GATED"
        verdict.confidence = 1.0 if len(ok_results) >= 2 else 0.5
        verdict.breakdown = {
            r.source: {"ok": r.ok, "gates": r.gate_failures, "error": r.error}
            for r in results
        }
        return verdict

    # --- Rule 2: weight by source weight AND that source's confidence -----
    numerator = denominator = 0.0
    breakdown: dict[str, Any] = {}

    for r in results:
        w = weights.get(r.source, 1.0)
        if r.ok and r.subscore is not None:
            effective = w * r.confidence
            numerator += r.subscore * effective
            denominator += effective
            breakdown[r.source] = {
                "subscore": round(r.subscore, 3),
                "confidence": r.confidence,
                "weight": w,
                "contribution": round(r.subscore * effective, 3),
                "flags": r.flags,
            }
        else:
            breakdown[r.source] = {
                "ok": r.ok, "error": r.error,
                "note": "excluded — no penalty applied",
            }

    if denominator == 0:
        verdict.verdict = "GATED"
        verdict.gate_failures = ["no_usable_sources"]
        verdict.confidence = 0.0
        verdict.breakdown = breakdown
        return verdict

    verdict.score = round(100.0 * (numerator / denominator), 1)

    # Confidence = how much of the available weight actually reported.
    max_possible = sum(weights.get(r.source, 1.0) for r in results)
    verdict.confidence = round(min(1.0, denominator / max_possible), 3)

    # --- Rule 3: surface conflict ----------------------------------------
    verdict.disagreements = _detect_disagreements(results)
    if verdict.disagreements:
        # Contradiction means we know less than the raw numbers suggest.
        verdict.confidence = round(verdict.confidence * 0.7, 3)

    verdict.breakdown = breakdown
    return verdict


async def evaluate_token(
    adapters: list[SourceAdapter],
    address: str,
    chain: str = "solana",
    session: aiohttp.ClientSession | None = None,
) -> Verdict:
    """Fan out to every source in parallel, then combine."""
    owns_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        results = await asyncio.gather(
            *(a.evaluate(session, address, chain) for a in adapters)
        )
        return combine(address, list(results))
    finally:
        if owns_session:
            await session.close()
