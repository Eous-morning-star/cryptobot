"""
DexScreener API client.

Rate limits differ per endpoint family, so we keep separate buckets:
  token-profiles / token-boosts / orders  -> 60 req/min
  latest/dex/*  (pairs, tokens, search)   -> 300 req/min

No auth required on any of these.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

import aiohttp

BASE = "https://api.dexscreener.com"


class RateBucket:
    """Simple sliding-window limiter. Shared across all callers of a family."""

    def __init__(self, limit: int, window_s: float = 60.0):
        self.limit = limit
        self.window = window_s
        self._hits: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            while self._hits and now - self._hits[0] > self.window:
                self._hits.popleft()
            if len(self._hits) >= self.limit:
                await asyncio.sleep(self.window - (now - self._hits[0]) + 0.05)
                return await self.acquire()
            self._hits.append(now)


class DexScreener:
    def __init__(self, session: aiohttp.ClientSession | None = None):
        self._session = session
        self._owns = session is None
        self.slow = RateBucket(60)    # profiles, boosts, orders
        self.fast = RateBucket(300)   # pairs, tokens, search

    async def __aenter__(self) -> "DexScreener":
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns and self._session:
            await self._session.close()

    async def _get(self, path: str, bucket: RateBucket,
                   params: dict | None = None) -> Any:
        await bucket.acquire()
        assert self._session is not None
        async with self._session.get(f"{BASE}{path}", params=params,
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                raise RuntimeError(f"dexscreener {path} -> HTTP {r.status}")
            return await r.json()

    # --- 60/min family ---------------------------------------------------

    async def latest_profiles(self) -> list[dict]:
        """/profiles_latest — newly created token profiles."""
        data = await self._get("/token-profiles/latest/v1", self.slow)
        return data if isinstance(data, list) else [data]

    async def latest_boosts(self) -> list[dict]:
        """/boosts_latest — most recently boosted tokens."""
        data = await self._get("/token-boosts/latest/v1", self.slow)
        return data if isinstance(data, list) else [data]

    async def top_boosts(self) -> list[dict]:
        """/boosts_top — tokens with the most active boosts."""
        data = await self._get("/token-boosts/top/v1", self.slow)
        return data if isinstance(data, list) else [data]

    async def orders(self, address: str, chain: str = "solana") -> list[dict]:
        """/orders — paid orders (profile, boost, ads) placed for a token.

        Useful as a spend signal: a team that paid for three products is more
        committed than one that paid for none. It says nothing about honesty.
        """
        data = await self._get(f"/orders/v1/{chain}/{address}", self.slow)
        return data if isinstance(data, list) else []

    # --- 300/min family --------------------------------------------------

    async def token_pairs(self, address: str) -> list[dict]:
        """/tokens — every pair trading a given token."""
        data = await self._get(f"/latest/dex/tokens/{address}", self.fast)
        return data.get("pairs") or []

    async def pair(self, pair_address: str, chain: str = "solana") -> dict | None:
        """/pair — a single pool."""
        data = await self._get(f"/latest/dex/pairs/{chain}/{pair_address}", self.fast)
        pairs = data.get("pairs") or []
        return pairs[0] if pairs else None

    async def search(self, query: str) -> list[dict]:
        """/pools — free-text search across pairs."""
        data = await self._get("/latest/dex/search", self.fast, {"q": query})
        return data.get("pairs") or []


def deepest_pair(pairs: list[dict]) -> dict | None:
    """Pick the pool that actually matters. Dust pairs distort every metric."""
    if not pairs:
        return None
    return max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))


def extract_address(entry: dict) -> str | None:
    """Boost/profile entries use tokenAddress; pair entries nest baseToken."""
    return entry.get("tokenAddress") or (entry.get("baseToken") or {}).get("address")
