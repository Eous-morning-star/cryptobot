"""
TokenScore web dashboard.

Deployed on Vercel as a FastAPI app (async-native, matches the aiohttp-based
adapters in tokenscore/sources). Reuses tokenscore/sources + tokenscore/scoring
completely unchanged from the Telegram bot -- this is a second front end on
the same engine, not a rewrite of it.

Routes:
  GET /                 -> the dashboard page (HTML)
  GET /api/score?address=<mint or name/ticker>   -> run the full multi-source
                                       scorecard. Accepts either a real mint
                                       address, or a free-text name/ticker
                                       (e.g. "ansem") which gets resolved to
                                       an address via DexScreener search
                                       first, same matching "Search pools"
                                       already uses.
  GET /api/pools?q=<query>        -> DexScreener pool search
  GET /api/scan?...                -> score a batch of currently-boosted tokens
                                       and return only the ones that pass your
                                       filters, ranked -- the "find me something
                                       to buy" button.
"""
from __future__ import annotations

import asyncio
import os
import re

import aiohttp
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from tokenscore.bot.dexscreener import DexScreener, deepest_pair, extract_address
from tokenscore.scoring.categories import build_analysis
from tokenscore.scoring.engine import combine, evaluate_token
from tokenscore.sources.adapters import ALL_ADAPTERS

# Solana mint addresses are base58, 32-44 chars. Anything that doesn't match
# this shape is treated as a name/ticker search instead of a literal address.
_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

app = FastAPI(title="TokenScore")

# Cap concurrent full evaluations, same reasoning as the Telegram bot: each
# one fans out to up to 5 APIs, so scanning 20 tokens with no cap would burn
# free-tier quotas (and this bot's own courtesy) in one click.
SCAN_SEMAPHORE = asyncio.Semaphore(4)


def build_adapters():
    adapters = []
    for cls in ALL_ADAPTERS:
        key = os.getenv(f"{cls.name.upper()}_API_KEY") if cls.requires_key else None
        adapters.append(cls(key))
    return adapters


async def _resolve_to_address(query: str) -> tuple[str, str | None]:
    """Accept either a literal mint address or a free-text name/ticker.

    Returns (address, matched_symbol) -- matched_symbol is None when the
    input was already a real address (nothing needed resolving), and set
    when it was resolved from a search so the UI can show what matched.
    """
    if _ADDRESS_RE.match(query):
        return query, None

    try:
        async with DexScreener() as ds:
            pairs = await ds.search(query)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"couldn't search for \"{query}\": {exc}") from exc

    solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
    best = deepest_pair(solana_pairs)
    address = (best.get("baseToken") or {}).get("address") if best else None
    if not address:
        raise HTTPException(
            status_code=404,
            detail=f"No Solana token found matching \"{query}\". Try the exact ticker, "
                   f"or search it on the \"Search pools\" tab and paste the address instead.",
        )
    symbol = (best.get("baseToken") or {}).get("symbol") or query
    return address, symbol


@app.get("/api/score")
async def api_score(address: str = Query(..., min_length=2, max_length=64)):
    query = address.strip()
    resolved_address, matched_symbol = await _resolve_to_address(query)

    adapters = build_adapters()
    # Fan out manually (rather than using engine.evaluate_token, which only
    # returns the Verdict) so we keep the raw per-source results too -- the
    # category checklist below needs the detailed raw fields, not just the
    # weighted subscore/confidence breakdown that goes into Verdict.
    async with aiohttp.ClientSession() as session:
        results = list(await asyncio.gather(
            *(a.evaluate(session, resolved_address, "solana") for a in adapters)
        ))
    verdict = combine(resolved_address, results)
    analysis = build_analysis(verdict, results)

    return JSONResponse(
        {
            "address": verdict.address,
            "queried_as": query,
            "matched_symbol": matched_symbol,
            "verdict": verdict.verdict,
            "score": verdict.score,
            "confidence": verdict.confidence,
            "gate_failures": verdict.gate_failures,
            "flags": verdict.flags,
            "disagreements": verdict.disagreements,
            "breakdown": verdict.breakdown,
            "sources_ok": verdict.sources_ok,
            "sources_total": verdict.sources_total,
            "analysis": analysis,
        }
    )


# --------------------------------------------------------------------------
# Arbitrage spread scanner.
#
# This is READ-ONLY -- it shows where a token's price disagrees across its
# own pools, it does not touch a wallet or place trades. Deliberately scoped
# that way: an auto-executing version needs private-key custody and has to
# survive gas/slippage/MEV risk, a different order of build and risk
# entirely from everything else in this project.
#
# Also worth being honest about up front: the spreads here are GROSS, before
# swap fees (which vary per DEX and even per pool) and before price impact
# on either leg. Real cross-DEX arbitrage on Solana is dominated by bots
# operating in milliseconds -- by the time a human sees a spread here and
# manually executes two swaps, it has very likely already closed or
# reversed. Treat this as a way to spot a persistently stale/illiquid pool
# worth a manual look, not a reliable, actionable profit signal.
# --------------------------------------------------------------------------

def _pair_price_usd(pair: dict) -> float | None:
    try:
        price = pair.get("priceUsd")
        return float(price) if price is not None else None
    except (TypeError, ValueError):
        return None


async def _arbitrage_for_address(address: str, min_liquidity: float) -> dict:
    async with DexScreener() as ds:
        pairs = await ds.token_pairs(address)

    legs = []
    for p in pairs:
        if p.get("chainId") != "solana":
            continue
        liq = (p.get("liquidity") or {}).get("usd") or 0
        price = _pair_price_usd(p)
        if price is None or price <= 0 or liq < min_liquidity:
            continue
        legs.append({
            "dex": p.get("dexId"),
            "pair_address": p.get("pairAddress"),
            "quote_symbol": (p.get("quoteToken") or {}).get("symbol"),
            "price_usd": price,
            "liquidity_usd": liq,
            "volume_h24": (p.get("volume") or {}).get("h24"),
            "url": p.get("url"),
        })
    legs.sort(key=lambda leg: leg["price_usd"])

    spread_pct = None
    if len(legs) >= 2 and legs[0]["price_usd"] > 0:
        spread_pct = (legs[-1]["price_usd"] - legs[0]["price_usd"]) / legs[0]["price_usd"] * 100

    return {
        "legs": legs,
        "spread_pct": round(spread_pct, 2) if spread_pct is not None else None,
        "pools_total": len(pairs),
        "pools_considered": len(legs),
    }


@app.get("/api/arbitrage")
async def api_arbitrage(
    address: str = Query(..., min_length=2, max_length=64),
    min_liquidity: float = Query(2000, ge=0),
):
    query = address.strip()
    resolved_address, matched_symbol = await _resolve_to_address(query)
    try:
        data = await _arbitrage_for_address(resolved_address, min_liquidity)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"couldn't pull pool data: {exc}") from exc

    return JSONResponse({
        "address": resolved_address,
        "queried_as": query,
        "matched_symbol": matched_symbol,
        **data,
    })


async def _arbitrage_scan(source: str, limit: int, min_spread: float, min_liquidity: float) -> list[dict]:
    try:
        async with DexScreener() as ds:
            entries = await (ds.top_boosts() if source == "alpha" else ds.latest_boosts())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"couldn't pull the candidate list: {exc}") from exc

    seen: set[str] = set()
    addresses: list[str] = []
    for e in entries:
        if e.get("chainId") != "solana":
            continue
        addr = extract_address(e)
        if addr and addr not in seen:
            seen.add(addr)
            addresses.append(addr)
    addresses = addresses[:limit]

    async def one(addr: str):
        async with SCAN_SEMAPHORE:
            try:
                data = await _arbitrage_for_address(addr, min_liquidity)
            except Exception:  # noqa: BLE001 - one bad token shouldn't sink the scan
                return None
        if data["spread_pct"] is None or data["spread_pct"] < min_spread:
            return None
        return {"address": addr, **data}

    rows = await asyncio.gather(*(one(a) for a in addresses))
    passed = [r for r in rows if r]
    passed.sort(key=lambda r: r["spread_pct"], reverse=True)
    return passed


@app.get("/api/arbitrage_scan")
async def api_arbitrage_scan(
    source: str = Query("alpha", pattern="^(alpha|screen)$"),
    min_spread: float = Query(2.0, ge=0, le=100),
    min_liquidity: float = Query(2000, ge=0),
    limit: int = Query(20, ge=1, le=30),
):
    passed = await _arbitrage_scan(source, limit, min_spread, min_liquidity)
    return JSONResponse({
        "evaluated": limit,
        "passed": passed,
        "filters": {
            "source": source, "min_spread": min_spread,
            "min_liquidity": min_liquidity, "limit": limit,
        },
    })


@app.get("/api/pools")
async def api_pools(q: str = Query(..., min_length=1, max_length=64)):
    try:
        async with DexScreener() as ds:
            pairs = await ds.search(q)
    except Exception as exc:  # noqa: BLE001 - surface as a clean 502, not a 500 traceback
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    results = []
    for p in pairs[:20]:
        results.append(
            {
                "base": (p.get("baseToken") or {}).get("symbol"),
                "quote": (p.get("quoteToken") or {}).get("symbol"),
                "dex": p.get("dexId"),
                "liquidity_usd": (p.get("liquidity") or {}).get("usd"),
                "volume_h24": (p.get("volume") or {}).get("h24"),
                "price_change_h24": (p.get("priceChange") or {}).get("h24"),
                "pair_address": p.get("pairAddress"),
                "base_address": (p.get("baseToken") or {}).get("address"),
                "url": p.get("url"),
            }
        )
    return JSONResponse({"query": q, "results": results})


async def _evaluate_one(address: str, session: aiohttp.ClientSession):
    async with SCAN_SEMAPHORE:
        return await evaluate_token(build_adapters(), address, session=session)


async def _scan_candidates(source: str, limit: int) -> list[dict]:
    try:
        async with DexScreener() as ds:
            entries = await (ds.top_boosts() if source == "alpha" else ds.latest_boosts())
    except Exception as exc:  # noqa: BLE001 - surface as a clean 502, not a 500 traceback
        raise HTTPException(status_code=502, detail=f"couldn't pull the candidate list: {exc}") from exc

    seen: set[str] = set()
    addresses: list[str] = []
    for e in entries:
        if e.get("chainId") != "solana":
            continue
        addr = extract_address(e)
        if addr and addr not in seen:
            seen.add(addr)
            addresses.append(addr)
    addresses = addresses[:limit]

    async with aiohttp.ClientSession() as session:
        verdicts = await asyncio.gather(
            *(_evaluate_one(a, session) for a in addresses),
            return_exceptions=True,
        )

    rows = []
    for addr, v in zip(addresses, verdicts):
        if isinstance(v, Exception):
            continue
        ds_data = (v.breakdown or {}).get("dexscreener", {})
        rows.append(
            {
                "address": addr,
                "score": v.score,
                "confidence": v.confidence,
                "gated": v.verdict == "GATED",
                "gate_failures": v.gate_failures,
                "liquidity_usd": ds_data.get("liquidity_usd"),
                "volume_h24": ds_data.get("volume_h24"),
                "disagreements": v.disagreements,
            }
        )
    return rows


@app.get("/api/scan")
async def api_scan(
    source: str = Query("alpha", pattern="^(alpha|screen)$"),
    min_score: float = Query(70, ge=0, le=100),
    min_confidence: float = Query(0.6, ge=0, le=1),
    require_agreement: bool = Query(True),
    limit: int = Query(20, ge=1, le=30),
):
    """
    source=alpha  -> pulls DexScreener's top-boosted list (teams paying to be
                     seen), the same pool /alpha draws from in the bot.
    source=screen -> pulls the latest-boosted list instead, broader and
                     noisier, same as /screen.
    Either way: every candidate is fully scored, then filtered down to what
    actually clears your thresholds -- nothing here is a raw popularity list.
    """
    rows = await _scan_candidates(source, limit)

    passed = [
        r
        for r in rows
        if not r["gated"]
        and (r["score"] or 0) >= min_score
        and r["confidence"] >= min_confidence
        and (not require_agreement or not r["disagreements"])
    ]
    passed.sort(key=lambda r: (r["score"] or 0), reverse=True)

    return JSONResponse(
        {
            "evaluated": len(rows),
            "gated": sum(1 for r in rows if r["gated"]),
            "passed": passed,
            "filters": {
                "source": source,
                "min_score": min_score,
                "min_confidence": min_confidence,
                "require_agreement": require_agreement,
                "limit": limit,
            },
        }
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TokenScore</title>
<style>
  .viz-root {
    color-scheme: light;
    --surface-1:      #fcfcfb;
    --page:           #f9f9f7;
    --text-primary:   #0b0b0b;
    --text-secondary: #52514e;
    --text-muted:     #898781;
    --gridline:       #e1e0d9;
    --border:         rgba(11,11,11,0.10);
    --seq-400:        #3987e5;
    --seq-250:        #86b6ef;
    --seq-track:      #e1e0d9;
    --good:           #0ca30c;
    --warning:        #fab219;
    --serious:        #ec835a;
    --critical:       #d03b3b;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) .viz-root {
      color-scheme: dark;
      --surface-1:      #1a1a19;
      --page:           #0d0d0d;
      --text-primary:   #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted:     #898781;
      --gridline:       #2c2c2a;
      --border:         rgba(255,255,255,0.10);
      --seq-400:        #3987e5;
      --seq-250:        #2a78d6;
      --seq-track:      #383835;
      --good:           #0ca30c;
      --warning:        #fab219;
      --serious:        #ec835a;
      --critical:       #e66767;
    }
  }

  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--page);
    color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 720px; margin: 0 auto; padding: 32px 20px 64px; }

  header h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
  header p { margin: 0 0 28px; color: var(--text-secondary); font-size: 14px; }

  .card {
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
  }

  .search-row { display: flex; gap: 8px; margin-bottom: 12px; }
  input[type=text] {
    flex: 1;
    font: inherit;
    font-size: 14px;
    padding: 11px 12px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--page);
    color: var(--text-primary);
  }
  input[type=text]:focus { outline: 2px solid var(--seq-400); outline-offset: 1px; }
  button {
    font: inherit;
    font-size: 14px;
    font-weight: 600;
    padding: 11px 18px;
    border-radius: 8px;
    border: none;
    background: var(--seq-400);
    color: #ffffff;
    cursor: pointer;
  }
  button:disabled { opacity: 0.55; cursor: default; }
  .tabs { display: flex; gap: 4px; margin-bottom: 16px; }
  .tab {
    font-size: 13px; font-weight: 600; padding: 7px 12px; border-radius: 999px;
    border: 1px solid var(--border); background: transparent; color: var(--text-secondary);
    cursor: pointer;
  }
  .tab[aria-selected="true"] { background: var(--seq-400); color: #fff; border-color: transparent; }

  .panel { display: none; }
  .panel.active { display: block; }

  .hint { font-size: 12px; color: var(--text-muted); margin: 0 0 20px; }

  .result { margin-top: 20px; }
  .badge-row { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
  .status-pill {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 12px; font-weight: 700; letter-spacing: 0.02em; text-transform: uppercase;
    padding: 3px 10px; border-radius: 999px; color: #fff;
  }
  .score-num { font-size: 34px; font-weight: 700; letter-spacing: -0.02em; }
  .score-num small { font-size: 15px; font-weight: 600; color: var(--text-muted); }
  .addr {
    font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12.5px;
    color: var(--text-secondary); word-break: break-all; margin: 6px 0 18px;
  }

  .meter-row { display: grid; grid-template-columns: 100px 1fr 40px; align-items: center;
               gap: 10px; margin-bottom: 10px; }
  .meter-label { font-size: 13px; color: var(--text-secondary); }
  .meter-track { height: 8px; border-radius: 4px; background: var(--seq-track); overflow: hidden; }
  .meter-fill { height: 100%; border-radius: 4px; background: var(--seq-400); }
  .meter-val { font-size: 12.5px; color: var(--text-muted); text-align: right;
               font-variant-numeric: tabular-nums; }

  .section-title { font-size: 12px; font-weight: 700; text-transform: uppercase;
                    letter-spacing: 0.04em; color: var(--text-muted); margin: 20px 0 10px; }

  .finding { display: flex; gap: 8px; align-items: flex-start; font-size: 13.5px;
             padding: 6px 0; border-bottom: 1px solid var(--gridline); }
  .finding:last-child { border-bottom: none; }
  .finding .icon { flex: none; }
  .finding.gate .icon { color: var(--critical); }
  .finding.flag .icon { color: var(--warning); }

  .note { font-size: 12.5px; color: var(--text-muted); margin-top: 16px; line-height: 1.5; }

  .pool-card { border-bottom: 1px solid var(--gridline); padding: 12px 0; }
  .pool-card:last-child { border-bottom: none; }
  .pool-title { font-size: 14px; font-weight: 600; }
  .pool-meta { font-size: 12.5px; color: var(--text-secondary); margin-top: 2px; }
  .pool-addr { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11.5px;
               color: var(--text-muted); margin-top: 4px; word-break: break-all; }

  .filter-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 14px; margin-bottom: 12px; }
  .filter-grid label, .checkbox-row {
    font-size: 12.5px; font-weight: 600; color: var(--text-secondary);
    display: flex; flex-direction: column; gap: 5px;
  }
  .filter-grid select, .filter-grid input[type=number] {
    font: inherit; font-size: 14px; padding: 9px 10px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--page); color: var(--text-primary);
  }
  .checkbox-row { flex-direction: row; align-items: center; gap: 8px; font-weight: 500; }
  .checkbox-row input { width: 16px; height: 16px; }

  .scan-row { display: grid; grid-template-columns: 44px 1fr auto; gap: 12px; align-items: center;
              padding: 12px 0; border-bottom: 1px solid var(--gridline); }
  .scan-row:last-child { border-bottom: none; }
  .scan-rank { font-size: 13px; font-weight: 700; color: var(--text-muted); text-align: center; }
  .scan-score { font-size: 18px; font-weight: 700; text-align: right; white-space: nowrap; }
  .scan-score small { font-size: 11px; font-weight: 600; color: var(--text-muted); display: block; }

  .link-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .link-chip {
    font-size: 11.5px; font-weight: 600; color: var(--text-secondary);
    text-decoration: none; padding: 4px 9px; border-radius: 999px;
    border: 1px solid var(--border); background: var(--page);
    white-space: nowrap;
  }
  .link-chip:hover { border-color: var(--seq-400); color: var(--seq-400); }
  .scan-row .link-row { margin-top: 6px; }

  .analysis { margin-top: 26px; border-top: 1px solid var(--gridline); padding-top: 20px; }
  .analysis-head { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 2px; }
  .analysis-head h3 { font-size: 15px; margin: 0; }
  .analysis-composite { font-size: 15px; font-weight: 700; }
  .analysis-sub { font-size: 12px; color: var(--text-muted); margin: 0 0 16px; line-height: 1.5; }

  .category-block { margin-bottom: 18px; border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
  .category-head { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
  .category-label { font-size: 13.5px; font-weight: 700; flex: 1; }
  .category-weight { font-size: 11px; color: var(--text-muted); font-weight: 600; }
  .category-score { font-size: 13.5px; font-weight: 700; font-variant-numeric: tabular-nums; min-width: 34px; text-align: right; }
  .category-note { font-size: 11.5px; color: var(--text-muted); margin: 6px 0 8px; line-height: 1.5; }

  .check-item { display: flex; gap: 8px; align-items: flex-start; font-size: 13px; padding: 5px 0; }
  .check-icon { flex: none; width: 16px; height: 16px; border-radius: 50%; display: flex; align-items: center;
                justify-content: center; font-size: 10px; font-weight: 700; color: #fff; margin-top: 1px; }
  .check-icon.pass { background: var(--good); }
  .check-icon.warn { background: var(--warning); color: #4a3400; }
  .check-icon.fail { background: var(--critical); }
  .check-icon.unknown { background: var(--text-muted); }
  .check-body { flex: 1; }
  .check-label { color: var(--text-primary); }
  .check-detail { color: var(--text-secondary); font-size: 12px; margin-top: 1px; line-height: 1.4; }

  .error-box { border: 1px solid var(--critical); background: color-mix(in srgb, var(--critical) 10%, transparent);
               color: var(--text-primary); border-radius: 8px; padding: 12px 14px; font-size: 13.5px; margin-top: 16px; }
  .empty { color: var(--text-muted); font-size: 13.5px; padding: 20px 0; text-align: center; }
  .spinner { color: var(--text-muted); font-size: 13.5px; padding: 20px 0; text-align: center; }

  footer { margin-top: 28px; font-size: 12px; color: var(--text-muted); text-align: center; line-height: 1.6; }
</style>
</head>
<body>
<div class="viz-root wrap">
  <header>
    <h1>TokenScore</h1>
    <p>Structural risk screener for Solana tokens &mdash; liquidity, authorities, and rug patterns across multiple sources.</p>
  </header>

  <div class="card">
    <div class="tabs" role="tablist">
      <button class="tab" id="tab-score" role="tab" aria-selected="true">Check a token</button>
      <button class="tab" id="tab-pools" role="tab" aria-selected="false">Search pools</button>
      <button class="tab" id="tab-scan" role="tab" aria-selected="false">Scan for buys</button>
      <button class="tab" id="tab-arb" role="tab" aria-selected="false">Arbitrage</button>
    </div>

    <div class="panel active" id="panel-score">
      <div class="search-row">
        <input type="text" id="address-input" placeholder="Name, ticker, or mint address — e.g. ansem or So111...112" autocomplete="off">
        <button id="score-btn">Check</button>
      </div>
      <p class="hint">Type a name/ticker or paste the mint address directly. Runs DexScreener, RugCheck, and GoPlus (plus Birdeye/Solscan if API keys are configured) and combines them into one verdict.</p>
      <div id="score-result"></div>
    </div>

    <div class="panel" id="panel-pools">
      <div class="search-row">
        <input type="text" id="pools-input" placeholder="Search by name or symbol, e.g. bonk" autocomplete="off">
        <button id="pools-btn">Search</button>
      </div>
      <p class="hint">Free-text pool search via DexScreener.</p>
      <div id="pools-result"></div>
    </div>

    <div class="panel" id="panel-scan">
      <div class="filter-grid">
        <label>List
          <select id="scan-source">
            <option value="alpha">Top boosted (alpha)</option>
            <option value="screen">Latest boosted (screen)</option>
          </select>
        </label>
        <label>Min score
          <input type="number" id="scan-min-score" value="70" min="0" max="100" step="1">
        </label>
        <label>Min confidence %
          <input type="number" id="scan-min-conf" value="60" min="0" max="100" step="5">
        </label>
        <label>Tokens to scan
          <input type="number" id="scan-limit" value="20" min="5" max="30" step="5">
        </label>
      </div>
      <label class="checkbox-row">
        <input type="checkbox" id="scan-agreement" checked>
        Require multi-source agreement (no conflicting signals)
      </label>
      <button id="scan-btn" style="width:100%; margin-top:14px;">Scan now</button>
      <p class="hint">Every candidate gets the full multi-source scorecard before filtering &mdash; this is not a raw popularity list. Click when you want a fresh read; each scan is dozens of live API calls, so it's meant to be run deliberately, not on a timer.</p>
      <div id="scan-result"></div>
    </div>

    <div class="panel" id="panel-arb">
      <p class="hint" style="margin-bottom:16px;">
        Read-only spread check across a token's own pools &mdash; nothing here touches a wallet or places a trade.
        Spreads shown are <b>gross</b>: before swap fees (which vary per DEX/pool) and before price impact on either leg.
        Real cross-DEX arbitrage on Solana runs at bot speed, in milliseconds &mdash; by the time a human sees a spread
        here and executes two manual swaps, it has very likely already closed or reversed. Treat this as a way to spot
        a persistently stale or illiquid pool worth a manual look, not a reliable, actionable profit signal.
      </p>

      <div class="search-row">
        <input type="text" id="arb-input" placeholder="Name, ticker, or mint address" autocomplete="off">
        <button id="arb-btn">Check spread</button>
      </div>
      <div class="filter-grid">
        <label>Min liquidity per pool
          <input type="number" id="arb-min-liq" value="2000" min="0" step="500">
        </label>
      </div>
      <div id="arb-result"></div>

      <div class="section-title" style="margin-top:26px;">Scan boosted tokens for spreads</div>
      <div class="filter-grid">
        <label>List
          <select id="arb-scan-source">
            <option value="alpha">Top boosted (alpha)</option>
            <option value="screen">Latest boosted (screen)</option>
          </select>
        </label>
        <label>Min spread %
          <input type="number" id="arb-scan-min-spread" value="2" min="0" max="100" step="0.5">
        </label>
        <label>Min liquidity per pool
          <input type="number" id="arb-scan-min-liq" value="2000" min="0" step="500">
        </label>
        <label>Tokens to scan
          <input type="number" id="arb-scan-limit" value="20" min="5" max="30" step="5">
        </label>
      </div>
      <button id="arb-scan-btn" style="width:100%; margin-top:4px;">Scan now</button>
      <p class="hint">Same caveats as above, applied across the boosted-token list. Click when you want a fresh read &mdash; each scan is dozens of live API calls.</p>
      <div id="arb-scan-result"></div>
    </div>
  </div>

  <footer>
    Scores rank structural risk only &mdash; not a recommendation.<br>
    A token can pass every check here and still go to zero. Size positions accordingly.
  </footer>
</div>

<script>
const $ = (id) => document.getElementById(id);

const TABS = ['score', 'pools', 'scan', 'arb'];
function switchTab(which) {
  TABS.forEach(name => {
    const isActive = name === which;
    $('tab-' + name).setAttribute('aria-selected', isActive);
    $('panel-' + name).classList.toggle('active', isActive);
  });
}
TABS.forEach(name => $('tab-' + name).addEventListener('click', () => switchTab(name)));

function money(v) {
  if (v === null || v === undefined) return '?';
  const n = Number(v);
  if (Math.abs(n) >= 1e6) return '$' + (n / 1e6).toFixed(1) + 'M';
  if (Math.abs(n) >= 1e3) return '$' + (n / 1e3).toFixed(1) + 'K';
  return '$' + n.toLocaleString(undefined, { maximumFractionDigits: 0 });
}
function shortAddr(a) {
  return a && a.length > 12 ? a.slice(0, 4) + '…' + a.slice(-4) : a;
}
function priceFmt(v) {
  if (v === null || v === undefined) return '?';
  const n = Number(v);
  if (Math.abs(n) >= 1) return '$' + n.toFixed(4);
  if (Math.abs(n) >= 0.0001) return '$' + n.toFixed(6);
  return '$' + n.toFixed(10).replace(/0+$/, '');
}
function explorerLinks(address) {
  const links = [
    { label: 'DexScreener', url: 'https://dexscreener.com/solana/' + address },
    { label: 'Birdeye', url: 'https://birdeye.so/token/' + address + '?chain=solana' },
    { label: 'RugCheck', url: 'https://rugcheck.xyz/tokens/' + address },
    { label: 'GoPlus', url: 'https://gopluslabs.io/token-security/solana/' + address },
    { label: 'Solscan', url: 'https://solscan.io/token/' + address },
  ];
  return '<div class="link-row">' + links.map(l =>
    '<a class="link-chip" href="' + l.url + '" target="_blank" rel="noopener noreferrer">' + l.label + ' ↗</a>'
  ).join('') + '</div>';
}
function statusForScore(score) {
  if (score >= 75) return { role: 'good', label: 'Low structural risk' };
  if (score >= 55) return { role: 'warning', label: 'Caution' };
  return { role: 'serious', label: 'High risk' };
}
const roleColor = (role) => getComputedStyle(document.querySelector('.viz-root')).getPropertyValue('--' + role).trim();

async function runScore() {
  const address = $('address-input').value.trim();
  const out = $('score-result');
  if (!address) { out.innerHTML = '<div class="error-box">Enter a mint address first.</div>'; return; }

  $('score-btn').disabled = true;
  out.innerHTML = '<div class="spinner">Querying sources…</div>';

  try {
    const res = await fetch('/api/score?address=' + encodeURIComponent(address));
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Request failed');
    renderScore(data);
  } catch (err) {
    out.innerHTML = '<div class="error-box">' + (err.message || 'Something went wrong') + '</div>';
  } finally {
    $('score-btn').disabled = false;
  }
}

function scoreColor(score) {
  if (score === null || score === undefined) return roleColor('warning');
  if (score >= 75) return roleColor('good');
  if (score >= 40) return roleColor('warning');
  return roleColor('critical');
}

function renderAnalysis(analysis) {
  if (!analysis) return '';
  let html = '<div class="analysis">';
  html += '<div class="analysis-head"><h3>Due-diligence checklist</h3>'
    + '<span class="analysis-composite" style="color:' + scoreColor(analysis.composite_score) + '">'
    + (analysis.composite_score === null ? '—' : analysis.composite_score.toFixed(0) + ' / 100')
    + '</span></div>';
  html += '<p class="analysis-sub">Weighted by priority — contract safety first, narrative last '
    + '(contract 25% &middot; liquidity 20% &middot; holders 15% &middot; volume 15% &middot; deployer 10% '
    + '&middot; community 10% &middot; narrative 5%, unscored). '
    + (analysis.rejected
        ? 'This token failed a gate, so the checklist score is forced to 0 regardless of the rest — a contract or liquidity fail overrides everything below it.'
        : 'A category with no available data is left out of the composite rather than scored as good or bad.')
    + '</p>';

  analysis.categories.forEach(cat => {
    html += '<div class="category-block">';
    html += '<div class="category-head">'
      + '<span class="category-label">' + cat.label + '</span>'
      + '<span class="category-weight">' + cat.weight + '%</span>'
      + '<span class="category-score" style="color:' + scoreColor(cat.score) + '">'
      + (cat.score === null ? '—' : cat.score.toFixed(0))
      + '</span></div>';
    if (cat.note) html += '<div class="category-note">' + cat.note + '</div>';
    cat.items.forEach(item => {
      const iconMap = { pass: '✓', warn: '!', fail: '✕', unknown: '?' };
      html += '<div class="check-item">'
        + '<span class="check-icon ' + item.status + '">' + iconMap[item.status] + '</span>'
        + '<span class="check-body"><span class="check-label">' + item.label + '</span>'
        + (item.detail ? '<div class="check-detail">' + item.detail + '</div>' : '')
        + '</span></div>';
    });
    html += '</div>';
  });

  html += '</div>';
  return html;
}

function renderScore(v) {
  const out = $('score-result');
  let html = '<div class="result">';

  if (v.matched_symbol) {
    html += '<p class="hint" style="margin-bottom:12px">Matched "' + v.queried_as + '" &rarr; <b>' + v.matched_symbol + '</b> (highest-liquidity pool on DexScreener). Not the token you meant? Search it on "Search pools" and paste the exact address instead.</p>';
  }

  if (v.verdict === 'GATED') {
    html += '<div class="badge-row">'
      + '<span class="status-pill" style="background:' + roleColor('critical') + '">Rejected</span>'
      + '</div>';
    html += '<div class="addr">' + v.address + '</div>';
    html += explorerLinks(v.address);
    html += '<div class="section-title">Fatal findings</div>';
    (v.gate_failures || []).forEach(g => {
      html += '<div class="finding gate"><span class="icon">✖</span><span>' + g + '</span></div>';
    });
    html += '<p class="note">' + v.sources_ok + '/' + v.sources_total + ' sources reporting. Gated tokens are not scored — a single fatal finding rejects the token outright, regardless of anything else.</p>';
    html += renderAnalysis(v.analysis);
    out.innerHTML = html + '</div>';
    return;
  }

  const score = v.score || 0;
  const status = statusForScore(score);
  html += '<div class="badge-row">'
    + '<span class="status-pill" style="background:' + roleColor(status.role) + '">' + status.label + '</span>'
    + '</div>';
  html += '<div class="score-num">' + score.toFixed(0) + ' <small>/ 100</small></div>';
  html += '<div class="addr">' + v.address + '</div>';
  html += explorerLinks(v.address);

  html += '<div class="meter-row"><div class="meter-label">Confidence</div>'
    + '<div class="meter-track"><div class="meter-fill" style="width:' + (v.confidence * 100).toFixed(0) + '%"></div></div>'
    + '<div class="meter-val">' + (v.confidence * 100).toFixed(0) + '%</div></div>';

  html += '<div class="section-title">Sources</div>';
  const breakdown = v.breakdown || {};
  Object.keys(breakdown).forEach(name => {
    const d = breakdown[name];
    if ('subscore' in d) {
      html += '<div class="meter-row"><div class="meter-label">' + name + '</div>'
        + '<div class="meter-track"><div class="meter-fill" style="width:' + (d.subscore * 100).toFixed(0) + '%"></div></div>'
        + '<div class="meter-val">' + d.subscore.toFixed(2) + '</div></div>';
    } else {
      html += '<div class="meter-row"><div class="meter-label">' + name + '</div>'
        + '<div class="meter-track"></div>'
        + '<div class="meter-val" title="' + (d.error || 'no data') + '">—</div></div>';
    }
  });

  if (v.disagreements && v.disagreements.length) {
    html += '<div class="section-title">Sources conflict</div>';
    v.disagreements.forEach(d => {
      html += '<div class="finding flag"><span class="icon">⚠</span><span>' + d + '</span></div>';
    });
  }

  if (v.flags && v.flags.length) {
    html += '<div class="section-title">Flags</div>';
    v.flags.forEach(f => {
      html += '<div class="finding flag"><span class="icon">⚠</span><span>' + f + '</span></div>';
    });
  }

  if (v.confidence < 0.6) {
    html += '<p class="note">Low confidence — sources missing or contradicting. Treat this score as weak evidence.</p>';
  }

  html += renderAnalysis(v.analysis);

  out.innerHTML = html + '</div>';
}

async function runPools() {
  const q = $('pools-input').value.trim();
  const out = $('pools-result');
  if (!q) { out.innerHTML = '<div class="error-box">Enter a search term first.</div>'; return; }

  $('pools-btn').disabled = true;
  out.innerHTML = '<div class="spinner">Searching…</div>';

  try {
    const res = await fetch('/api/pools?q=' + encodeURIComponent(q));
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Request failed');
    renderPools(data.results || []);
  } catch (err) {
    out.innerHTML = '<div class="error-box">' + (err.message || 'Something went wrong') + '</div>';
  } finally {
    $('pools-btn').disabled = false;
  }
}

function renderPools(rows) {
  const out = $('pools-result');
  if (!rows.length) { out.innerHTML = '<div class="empty">No pools found.</div>'; return; }
  let html = '<div class="result">';
  rows.forEach(p => {
    const chg = p.price_change_h24 || 0;
    const arrow = chg >= 0 ? '▲' : '▼';
    html += '<div class="pool-card">'
      + '<div class="pool-title">' + (p.base || '?') + '/' + (p.quote || '?') + ' on ' + (p.dex || '?') + '</div>'
      + '<div class="pool-meta">liq ' + money(p.liquidity_usd) + ' &middot; vol24 ' + money(p.volume_h24)
      + ' &middot; ' + arrow + ' ' + Math.abs(chg).toFixed(1) + '%</div>'
      + '<div class="pool-addr">' + (p.pair_address || '') + '</div>'
      + (p.base_address ? explorerLinks(p.base_address) : '')
      + '</div>';
  });
  out.innerHTML = html + '</div>';
}

$('score-btn').addEventListener('click', runScore);
$('address-input').addEventListener('keydown', e => { if (e.key === 'Enter') runScore(); });
$('pools-btn').addEventListener('click', runPools);
$('pools-input').addEventListener('keydown', e => { if (e.key === 'Enter') runPools(); });

async function runScan() {
  const source = $('scan-source').value;
  const minScore = $('scan-min-score').value;
  const minConf = (Number($('scan-min-conf').value) / 100).toFixed(2);
  const agreement = $('scan-agreement').checked;
  const limit = $('scan-limit').value;
  const out = $('scan-result');

  $('scan-btn').disabled = true;
  $('scan-btn').textContent = 'Scanning ' + limit + ' tokens…';
  out.innerHTML = '<div class="spinner">Scoring candidates across multiple sources — this can take a little while.</div>';

  try {
    const params = new URLSearchParams({
      source, min_score: minScore, min_confidence: minConf,
      require_agreement: agreement, limit,
    });
    const res = await fetch('/api/scan?' + params.toString());
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Request failed');
    renderScan(data);
  } catch (err) {
    out.innerHTML = '<div class="error-box">' + (err.message || 'Something went wrong') + '</div>';
  } finally {
    $('scan-btn').disabled = false;
    $('scan-btn').textContent = 'Scan now';
  }
}

function renderScan(data) {
  const out = $('scan-result');
  let html = '<p class="note">' + data.evaluated + ' evaluated &middot; ' + data.gated
    + ' gated out &middot; ' + data.passed.length + ' passed your filters, ranked by score.</p>';

  if (!data.passed.length) {
    html += '<div class="empty">Nothing cleared the bar this round &mdash; that\'s a valid result, not an error. Try again shortly or loosen the filters.</div>';
    out.innerHTML = html;
    return;
  }

  data.passed.forEach((r, i) => {
    const status = statusForScore(r.score || 0);
    html += '<div class="scan-row">'
      + '<div class="scan-rank">' + (i + 1) + '</div>'
      + '<div>'
      + '<div class="pool-addr" style="margin-top:0">' + r.address + '</div>'
      + '<div class="pool-meta">liq ' + money(r.liquidity_usd) + ' &middot; vol24 ' + money(r.volume_h24) + '</div>'
      + explorerLinks(r.address)
      + '</div>'
      + '<div class="scan-score" style="color:' + roleColor(status.role) + '">' + (r.score || 0).toFixed(0)
      + '<small>' + (r.confidence * 100).toFixed(0) + '% conf</small></div>'
      + '</div>';
  });
  out.innerHTML = html;
}

$('scan-btn').addEventListener('click', runScan);

function spreadColor(pct) {
  if (pct === null || pct === undefined) return roleColor('warning');
  if (pct >= 8) return roleColor('serious');
  if (pct >= 2) return roleColor('good');
  return roleColor('warning');
}

async function runArbitrage() {
  const query = $('arb-input').value.trim();
  const minLiq = $('arb-min-liq').value;
  const out = $('arb-result');
  if (!query) { out.innerHTML = '<div class="error-box">Enter a name, ticker, or address first.</div>'; return; }

  $('arb-btn').disabled = true;
  out.innerHTML = '<div class="spinner">Pulling pools…</div>';

  try {
    const params = new URLSearchParams({ address: query, min_liquidity: minLiq });
    const res = await fetch('/api/arbitrage?' + params.toString());
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Request failed');
    renderArbitrage(data);
  } catch (err) {
    out.innerHTML = '<div class="error-box">' + (err.message || 'Something went wrong') + '</div>';
  } finally {
    $('arb-btn').disabled = false;
  }
}

function renderArbitrage(v) {
  const out = $('arb-result');
  let html = '<div class="result">';

  if (v.matched_symbol) {
    html += '<p class="hint" style="margin-bottom:12px">Matched "' + v.queried_as + '" &rarr; <b>' + v.matched_symbol + '</b> (highest-liquidity pool on DexScreener). Not the token you meant? Search it on "Search pools" and paste the exact address instead.</p>';
  }

  html += '<div class="addr">' + v.address + '</div>';
  html += explorerLinks(v.address);

  if (v.spread_pct === null || v.legs.length < 2) {
    html += '<div class="empty">Not enough qualifying pools to compute a spread (need at least 2 above the liquidity floor). '
      + v.pools_considered + ' of ' + v.pools_total + ' pool(s) cleared the liquidity floor.</div>';
    out.innerHTML = html + '</div>';
    return;
  }

  html += '<div class="score-num" style="color:' + spreadColor(v.spread_pct) + '">' + v.spread_pct.toFixed(2) + '<small>% gross spread</small></div>';
  html += '<p class="note">' + v.pools_considered + ' of ' + v.pools_total + ' pools cleared the liquidity floor, ranked lowest to highest price. Spread is gross &mdash; before fees and slippage.</p>';

  html += '<div class="section-title">Pools, low &rarr; high price</div>';
  v.legs.forEach((leg, i) => {
    const tag = i === 0 ? ' (lowest)' : (i === v.legs.length - 1 ? ' (highest)' : '');
    html += '<div class="pool-card">'
      + '<div class="pool-title">' + (leg.dex || '?') + ' &middot; vs ' + (leg.quote_symbol || '?') + tag + '</div>'
      + '<div class="pool-meta">' + priceFmt(leg.price_usd) + ' &middot; liq ' + money(leg.liquidity_usd) + ' &middot; vol24 ' + money(leg.volume_h24) + '</div>'
      + (leg.url ? '<div class="link-row"><a class="link-chip" href="' + leg.url + '" target="_blank" rel="noopener noreferrer">View pool ↗</a></div>' : '')
      + '</div>';
  });

  out.innerHTML = html + '</div>';
}

$('arb-btn').addEventListener('click', runArbitrage);
$('arb-input').addEventListener('keydown', e => { if (e.key === 'Enter') runArbitrage(); });

async function runArbitrageScan() {
  const source = $('arb-scan-source').value;
  const minSpread = $('arb-scan-min-spread').value;
  const minLiq = $('arb-scan-min-liq').value;
  const limit = $('arb-scan-limit').value;
  const out = $('arb-scan-result');

  $('arb-scan-btn').disabled = true;
  $('arb-scan-btn').textContent = 'Scanning ' + limit + ' tokens…';
  out.innerHTML = '<div class="spinner">Pulling pools across candidates — this can take a little while.</div>';

  try {
    const params = new URLSearchParams({ source, min_spread: minSpread, min_liquidity: minLiq, limit });
    const res = await fetch('/api/arbitrage_scan?' + params.toString());
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Request failed');
    renderArbitrageScan(data);
  } catch (err) {
    out.innerHTML = '<div class="error-box">' + (err.message || 'Something went wrong') + '</div>';
  } finally {
    $('arb-scan-btn').disabled = false;
    $('arb-scan-btn').textContent = 'Scan now';
  }
}

function renderArbitrageScan(data) {
  const out = $('arb-scan-result');
  let html = '<p class="note">' + data.filters.limit + ' candidates checked &middot; ' + data.passed.length + ' cleared the min-spread filter, ranked by spread.</p>';

  if (!data.passed.length) {
    html += '<div class="empty">Nothing cleared the bar this round &mdash; that\'s a valid result, not an error. Try again shortly or loosen the filters.</div>';
    out.innerHTML = html;
    return;
  }

  data.passed.forEach((r, i) => {
    html += '<div class="scan-row">'
      + '<div class="scan-rank">' + (i + 1) + '</div>'
      + '<div>'
      + '<div class="pool-addr" style="margin-top:0">' + r.address + '</div>'
      + '<div class="pool-meta">' + r.pools_considered + ' of ' + r.pools_total + ' pools considered</div>'
      + explorerLinks(r.address)
      + '</div>'
      + '<div class="scan-score" style="color:' + spreadColor(r.spread_pct) + '">' + r.spread_pct.toFixed(2)
      + '<small>% spread</small></div>'
      + '</div>';
  });
  out.innerHTML = html;
}

$('arb-scan-btn').addEventListener('click', runArbitrageScan);
</script>
</body>
</html>
"""
