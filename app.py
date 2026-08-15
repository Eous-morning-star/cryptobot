"""
TokenScore web dashboard.

Deployed on Vercel as a FastAPI app (async-native, matches the aiohttp-based
adapters in tokenscore/sources). Reuses tokenscore/sources + tokenscore/scoring
completely unchanged from the Telegram bot -- this is a second front end on
the same engine, not a rewrite of it.

Routes:
  GET /                 -> the dashboard page (HTML)
  GET /api/score?address=<mint>   -> run the full multi-source scorecard
  GET /api/pools?q=<query>        -> DexScreener pool search
  GET /api/scan?...                -> score a batch of currently-boosted tokens
                                       and return only the ones that pass your
                                       filters, ranked -- the "find me something
                                       to buy" button.
"""
from __future__ import annotations

import asyncio
import os

import aiohttp
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from tokenscore.bot.dexscreener import DexScreener, extract_address
from tokenscore.scoring.engine import evaluate_token
from tokenscore.sources.adapters import ALL_ADAPTERS

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


@app.get("/api/score")
async def api_score(address: str = Query(..., min_length=20, max_length=64)):
    address = address.strip()
    adapters = build_adapters()
    verdict = await evaluate_token(adapters, address)
    return JSONResponse(
        {
            "address": verdict.address,
            "verdict": verdict.verdict,
            "score": verdict.score,
            "confidence": verdict.confidence,
            "gate_failures": verdict.gate_failures,
            "flags": verdict.flags,
            "disagreements": verdict.disagreements,
            "breakdown": verdict.breakdown,
            "sources_ok": verdict.sources_ok,
            "sources_total": verdict.sources_total,
        }
    )


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
    </div>

    <div class="panel active" id="panel-score">
      <div class="search-row">
        <input type="text" id="address-input" placeholder="Mint address, e.g. So11111111111111111111111111111111111111112" autocomplete="off">
        <button id="score-btn">Check</button>
      </div>
      <p class="hint">Runs DexScreener, RugCheck, and GoPlus (plus Birdeye/Solscan if API keys are configured) and combines them into one verdict.</p>
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
  </div>

  <footer>
    Scores rank structural risk only &mdash; not a recommendation.<br>
    A token can pass every check here and still go to zero. Size positions accordingly.
  </footer>
</div>

<script>
const $ = (id) => document.getElementById(id);

const TABS = ['score', 'pools', 'scan'];
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

function renderScore(v) {
  const out = $('score-result');
  let html = '<div class="result">';

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
</script>
</body>
</html>
"""
