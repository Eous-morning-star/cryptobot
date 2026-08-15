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
"""
from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from tokenscore.bot.dexscreener import DexScreener
from tokenscore.scoring.engine import evaluate_token
from tokenscore.sources.adapters import ALL_ADAPTERS

app = FastAPI(title="TokenScore")


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
  </div>

  <footer>
    Scores rank structural risk only &mdash; not a recommendation.<br>
    A token can pass every check here and still go to zero. Size positions accordingly.
  </footer>
</div>

<script>
const $ = (id) => document.getElementById(id);

function switchTab(which) {
  const scoreTab = $('tab-score'), poolsTab = $('tab-pools');
  const scorePanel = $('panel-score'), poolsPanel = $('panel-pools');
  const onScore = which === 'score';
  scoreTab.setAttribute('aria-selected', onScore);
  poolsTab.setAttribute('aria-selected', !onScore);
  scorePanel.classList.toggle('active', onScore);
  poolsPanel.classList.toggle('active', !onScore);
}
$('tab-score').addEventListener('click', () => switchTab('score'));
$('tab-pools').addEventListener('click', () => switchTab('pools'));

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
      + '</div>';
  });
  out.innerHTML = html + '</div>';
}

$('score-btn').addEventListener('click', runScore);
$('address-input').addEventListener('keydown', e => { if (e.key === 'Enter') runScore(); });
$('pools-btn').addEventListener('click', runPools);
$('pools-input').addEventListener('keydown', e => { if (e.key === 'Enter') runPools(); });
</script>
</body>
</html>
"""
