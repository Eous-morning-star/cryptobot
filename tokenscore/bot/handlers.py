"""
Telegram command handlers.

Auth model: OWNER_IDS whitelist, enforced by a decorator on every handler.
This bot produces buy signals — do not leave it open. There is no rate
limiting for strangers because strangers never reach the handler.
"""
from __future__ import annotations

import asyncio
import logging
import os

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from . import formatters as fmt
from .dexscreener import DexScreener, deepest_pair, extract_address
from ..scoring.engine import combine
from ..sources.adapters import (
    BirdeyeAdapter, DexScreenerAdapter, GoPlusAdapter,
    RugCheckAdapter, SolscanAdapter,
)

log = logging.getLogger(__name__)

OWNER_IDS = {
    int(x) for x in os.getenv("TELEGRAM_OWNER_IDS", "").replace(" ", "").split(",") if x
}

# Cap concurrent full evaluations. Each one fans out to 5 APIs, so a /screen
# over 30 tokens without this would burn your Birdeye quota in one command.
EVAL_SEMAPHORE = asyncio.Semaphore(4)


def build_adapters() -> list:
    return [
        DexScreenerAdapter(),
        RugCheckAdapter(os.getenv("RUGCHECK_API_KEY")),
        GoPlusAdapter(),
        BirdeyeAdapter(os.getenv("BIRDEYE_API_KEY")),
        SolscanAdapter(os.getenv("SOLSCAN_API_KEY")),
    ]


def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not OWNER_IDS or (user and user.id not in OWNER_IDS):
            log.warning("rejected user_id=%s", user.id if user else "?")
            if update.message:
                await update.message.reply_text("Not authorised.")
            return
        return await func(update, context)
    return wrapper


async def _reply(update: Update, text: str) -> None:
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def _evaluate(address: str, session: aiohttp.ClientSession):
    async with EVAL_SEMAPHORE:
        adapters = build_adapters()
        results = await asyncio.gather(
            *(a.evaluate(session, address, "solana") for a in adapters)
        )
    return combine(address, list(results))


def _arg(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    return context.args[0].strip() if context.args else None


# --------------------------------------------------------------------------
# Basics
# --------------------------------------------------------------------------

@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, (
        "<b>tokenscore</b>\n\n"
        "<b>Analysis</b>\n"
        "/score &lt;mint&gt; — full multi-source scorecard\n"
        "/check &lt;mint&gt; — fast gate check only\n"
        "/pair &lt;pair&gt; — single pool detail\n"
        "/tokens &lt;mint&gt; — every pair for a token\n"
        "/pools &lt;query&gt; — search pools\n\n"
        "<b>Discovery</b>\n"
        "/screen — score the boosted list, ranked\n"
        "/early — recent profiles, gate-filtered\n"
        "/alpha — tokens with multi-source agreement\n"
        "/boosts_latest · /boosts_top · /profiles_latest\n"
        "/orders &lt;mint&gt; — what the team paid for\n"
        "/market — portfolio-wide summary\n\n"
        "<i>Scores rank structural risk only. Every token here "
        "can still go to zero.</i>"
    ))


@owner_only
async def score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    address = _arg(context)
    if not address:
        await _reply(update, "Usage: <code>/score &lt;mint address&gt;</code>")
        return

    msg = await update.message.reply_text("Querying 5 sources…")
    try:
        async with aiohttp.ClientSession() as session:
            verdict = await _evaluate(address, session)
            ds = verdict.breakdown.get("dexscreener", {})
        await msg.edit_text(
            fmt.render_verdict(verdict, market=ds),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("/score failed")
        await msg.edit_text(fmt.render_error("score", str(exc)),
                            parse_mode=ParseMode.HTML)


@owner_only
async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gates only — skips Birdeye/Solscan to stay inside free quotas."""
    address = _arg(context)
    if not address:
        await _reply(update, "Usage: <code>/check &lt;mint address&gt;</code>")
        return

    msg = await update.message.reply_text("Checking gates…")
    try:
        adapters = [DexScreenerAdapter(), GoPlusAdapter(),
                    RugCheckAdapter(os.getenv("RUGCHECK_API_KEY"))]
        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(
                *(a.evaluate(session, address, "solana") for a in adapters)
            )
        verdict = combine(address, list(results))
        await msg.edit_text(fmt.render_verdict(verdict),
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True)
    except Exception as exc:  # noqa: BLE001
        await msg.edit_text(fmt.render_error("check", str(exc)),
                            parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------
# Market data passthrough
# --------------------------------------------------------------------------

@owner_only
async def pools(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args) if context.args else None
    if not query:
        await _reply(update, "Usage: <code>/pools &lt;search term&gt;</code>")
        return
    async with DexScreener() as ds:
        pairs = await ds.search(query)
    await _reply(update, fmt.render_pairs(pairs, f"Pools matching “{query}”"))


@owner_only
async def tokens(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    address = _arg(context)
    if not address:
        await _reply(update, "Usage: <code>/tokens &lt;mint&gt;</code>")
        return
    async with DexScreener() as ds:
        pairs = await ds.token_pairs(address)
    await _reply(update, fmt.render_pairs(pairs, f"Pairs for {fmt.short(address)}"))


@owner_only
async def pair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pair_address = _arg(context)
    if not pair_address:
        await _reply(update, "Usage: <code>/pair &lt;pair address&gt;</code>")
        return
    async with DexScreener() as ds:
        p = await ds.pair(pair_address)
    await _reply(update, fmt.render_pairs([p] if p else [], "Pool detail"))


@owner_only
async def orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    address = _arg(context)
    if not address:
        await _reply(update, "Usage: <code>/orders &lt;mint&gt;</code>")
        return
    async with DexScreener() as ds:
        data = await ds.orders(address)

    if not data:
        await _reply(update, "No paid orders found for this token.")
        return

    lines = [f"<b>Paid orders — {fmt.short(address)}</b>", ""]
    for o in data[:20]:
        lines.append(f"• {o.get('type', '?')} — {o.get('status', '?')}")
    lines += ["", "<i>Spend signals commitment, not honesty. "
                  "Rug teams buy boosts too.</i>"]
    await _reply(update, "\n".join(lines))


async def _boost_list(update: Update, fetch, title: str) -> None:
    async with DexScreener() as ds:
        entries = await fetch(ds)
    solana = [e for e in entries if e.get("chainId") == "solana"][:15]
    if not solana:
        await _reply(update, f"<b>{title}</b>\n\n<i>No Solana entries.</i>")
        return
    lines = [f"<b>{title}</b>", ""]
    for e in solana:
        addr = extract_address(e) or "?"
        amt = e.get("totalAmount") or e.get("amount")
        lines.append(f"• <code>{addr}</code>" + (f" — {amt}" if amt else ""))
    await _reply(update, "\n".join(lines))


@owner_only
async def boosts_latest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _boost_list(update, lambda ds: ds.latest_boosts(), "Latest boosts")


@owner_only
async def boosts_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _boost_list(update, lambda ds: ds.top_boosts(), "Top boosts")


@owner_only
async def profiles_latest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _boost_list(update, lambda ds: ds.latest_profiles(), "Latest profiles")


# --------------------------------------------------------------------------
# Discovery — the commands that actually use the scoring engine
# --------------------------------------------------------------------------

async def _score_many(addresses: list[str], limit: int) -> list[dict]:
    rows: list[dict] = []
    async with aiohttp.ClientSession() as session:
        verdicts = await asyncio.gather(
            *(_evaluate(a, session) for a in addresses[:limit]),
            return_exceptions=True,
        )
    for addr, v in zip(addresses[:limit], verdicts):
        if isinstance(v, Exception):
            continue
        ds = (v.breakdown or {}).get("dexscreener", {})
        rows.append({
            "address": addr,
            "score": v.score,
            "confidence": v.confidence,
            "gated": v.verdict == "GATED",
            "liquidity_usd": ds.get("liquidity_usd"),
            "volume_h24": ds.get("volume_h24"),
            "disagreements": v.disagreements,
        })
    return rows


@owner_only
async def screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("Pulling boosted tokens…")
    try:
        async with DexScreener() as ds:
            entries = await ds.latest_boosts()
        addresses = [
            a for a in (extract_address(e) for e in entries
                        if e.get("chainId") == "solana") if a
        ]
        seen, unique = set(), []
        for a in addresses:
            if a not in seen:
                seen.add(a)
                unique.append(a)

        await msg.edit_text(f"Scoring {min(len(unique), 20)} tokens across 5 sources…")
        rows = await _score_many(unique, limit=20)

        passed = [r for r in rows if not r["gated"]]
        passed.sort(key=lambda r: (r["score"] or 0), reverse=True)
        gated = len(rows) - len(passed)

        await msg.edit_text(
            fmt.render_ranked(
                "Screen — boosted tokens",
                passed[:10],
                note=f"{len(rows)} evaluated · {gated} gated out · "
                     f"ranked by score, not boost size",
            ),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("/screen failed")
        await msg.edit_text(fmt.render_error("screen", str(exc)),
                            parse_mode=ParseMode.HTML)


@owner_only
async def early(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Newest profiles, gate-filtered.

    NOTE: liquidity gates in adapters.py will reject most genuinely early
    tokens. Until gates.py splits into per-mode tiers, this leans toward
    tokens that are already established.
    """
    msg = await update.message.reply_text("Pulling latest profiles…")
    try:
        async with DexScreener() as ds:
            entries = await ds.latest_profiles()
        addresses = [
            a for a in (extract_address(e) for e in entries
                        if e.get("chainId") == "solana") if a
        ]
        rows = await _score_many(addresses, limit=15)
        passed = [r for r in rows if not r["gated"]]
        passed.sort(key=lambda r: (r["score"] or 0), reverse=True)

        await msg.edit_text(
            fmt.render_ranked("Early — latest profiles", passed[:10],
                              note="gates tuned for established tokens; "
                                   "expect over-rejection here"),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001
        await msg.edit_text(fmt.render_error("early", str(exc)),
                            parse_mode=ParseMode.HTML)


@owner_only
async def alpha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """High-conviction only: strong score AND high confidence AND no conflicts."""
    msg = await update.message.reply_text("Hunting high-conviction candidates…")
    try:
        async with DexScreener() as ds:
            entries = await ds.top_boosts()
        addresses = [
            a for a in (extract_address(e) for e in entries
                        if e.get("chainId") == "solana") if a
        ]
        rows = await _score_many(addresses, limit=20)

        strong = [
            r for r in rows
            if not r["gated"]
            and (r["score"] or 0) >= 70
            and r["confidence"] >= 0.6
            and not r["disagreements"]
        ]
        strong.sort(key=lambda r: (r["score"] or 0), reverse=True)

        await msg.edit_text(
            fmt.render_ranked(
                "Alpha — multi-source agreement", strong[:8],
                note="score ≥70, confidence ≥60%, zero source conflicts",
            ),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001
        await msg.edit_text(fmt.render_error("alpha", str(exc)),
                            parse_mode=ParseMode.HTML)


@owner_only
async def market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with DexScreener() as ds:
        boosts, profiles = await asyncio.gather(
            ds.latest_boosts(), ds.latest_profiles()
        )
    sol_boosts = [b for b in boosts if b.get("chainId") == "solana"]
    sol_profiles = [p for p in profiles if p.get("chainId") == "solana"]
    await _reply(update, (
        "<b>Market scope</b>\n\n"
        f"Solana boosts in feed: <b>{len(sol_boosts)}</b>\n"
        f"Solana profiles in feed: <b>{len(sol_profiles)}</b>\n\n"
        "<i>Feed volume only — not a market health indicator. "
        "Deploy counts spike hardest during the worst conditions.</i>"
    ))


@owner_only
async def takeovers_latest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, (
        "<b>Latest CTO</b>\n\n"
        "<i>Not implemented.</i> DexScreener has no public endpoint for "
        "community takeovers — the label appears on their site but isn't in "
        "the documented API.\n\n"
        "Options: detect it yourself by watching profile updates where socials "
        "change while the mint stays the same, or drop the command."
    ))
