#!/usr/bin/env python3
"""
Adapter verification probe.

Run this BEFORE trusting any score. It calls each source against a known
token, dumps the raw payload, and reports which fields your parser actually
resolved — because a wrong JSON key produces a plausible-looking score with
no error, which is the worst possible failure mode.

Run from the PARENT directory of tokenscore/:

    python -m tokenscore.probe <mint_address>
    python -m tokenscore.probe <mint_address> --raw   # full JSON dump
    python -m tokenscore.probe --known                # reference tokens

Interpretation:
    OK       parser found real data
    EMPTY    field resolved to 0/None — either genuinely absent, or WRONG KEY
    ERROR    source unreachable

Any EMPTY on a token you know has that data means the mapping is wrong.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

import aiohttp

from .scoring.engine import combine
from .sources.adapters import (
    BirdeyeAdapter, DexScreenerAdapter, GoPlusAdapter,
    RugCheckAdapter, SolscanAdapter,
)

# Reference tokens. WSOL and USDC should pass everything; swap the third for
# a known rug you remember, so you can confirm gates actually fire.
KNOWN = {
    "WSOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
}

# Fields each adapter claims to extract, and whether emptiness is suspicious
# for a large established token.
EXPECTED = {
    "dexscreener": ["liquidity_usd", "volume_h24", "buys", "sells"],
    "rugcheck":    ["score", "lp_locked_pct"],
    "goplus":      ["top10_pct", "mintable", "freezable"],
    "birdeye":     ["holders", "unique_wallets_24h"],
    "solscan":     ["holders", "created_time"],
}


def classify(value) -> str:
    if value is None:
        return "EMPTY"
    if isinstance(value, (int, float)) and value == 0:
        return "EMPTY"
    if isinstance(value, (str, list, dict)) and len(value) == 0:
        return "EMPTY"
    return "OK"


def build_adapters() -> list:
    return [
        DexScreenerAdapter(),
        RugCheckAdapter(os.getenv("RUGCHECK_API_KEY")),
        GoPlusAdapter(),
        BirdeyeAdapter(os.getenv("BIRDEYE_API_KEY")),
        SolscanAdapter(os.getenv("SOLSCAN_API_KEY")),
    ]


async def probe(address: str, show_raw: bool = False) -> None:
    adapters = build_adapters()
    print(f"\n{'=' * 68}\nPROBING  {address}\n{'=' * 68}")

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *(a.evaluate(session, address, "solana") for a in adapters)
        )

    problems: list[str] = []

    for r in results:
        head = f"\n[{r.source}]  {r.latency_ms}ms"
        if not r.ok:
            print(f"{head}  ERROR: {r.error}")
            if "missing API key" not in (r.error or ""):
                problems.append(f"{r.source}: unreachable ({r.error})")
            continue

        print(f"{head}  subscore={r.subscore}  confidence={r.confidence}")

        for field in EXPECTED.get(r.source, []):
            value = r.raw.get(field)
            status = classify(value)
            shown = str(value)
            if len(shown) > 55:
                shown = shown[:52] + "..."
            print(f"    {status:<6} {field:<22} = {shown}")
            if status == "EMPTY":
                problems.append(f"{r.source}.{field} empty — check the JSON key")

        if r.gate_failures:
            print(f"    GATES  {r.gate_failures}")
        if r.flags:
            print(f"    FLAGS  {r.flags}")
        if show_raw:
            print("    RAW:", json.dumps(r.raw, indent=6, default=str)[:1800])

    verdict = combine(address, list(results))
    print(f"\n{'-' * 68}")
    print(f"VERDICT   {verdict.verdict}   score={verdict.score}   "
          f"confidence={verdict.confidence}")
    print(f"SOURCES   {verdict.sources_ok}/{verdict.sources_total} reporting")
    if verdict.gate_failures:
        print(f"GATED BY  {verdict.gate_failures}")
    if verdict.disagreements:
        print(f"CONFLICT  {verdict.disagreements}")

    print(f"\n{'-' * 68}")
    if problems:
        print(f"{len(problems)} ISSUE(S) TO FIX:")
        for p in problems:
            print(f"  - {p}")
        print("\nFor an established token, EMPTY almost always means the")
        print("adapter is reading a key the API doesn't return. Dump the raw")
        print("payload with --raw and correct sources/adapters.py.")
    else:
        print("No parsing issues detected for this token.")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("address", nargs="?", help="mint address")
    ap.add_argument("--raw", action="store_true", help="dump full payloads")
    ap.add_argument("--known", action="store_true", help="probe reference tokens")
    args = ap.parse_args()

    if args.known:
        for label, addr in KNOWN.items():
            print(f"\n\n### {label}")
            await probe(addr, args.raw)
    elif args.address:
        await probe(args.address, args.raw)
    else:
        ap.error("provide an address or --known")


if __name__ == "__main__":
    asyncio.run(main())
