"""
Concrete source adapters.

ENDPOINT WARNING: these APIs change without notice and several are
undocumented. Verify each response shape against a live call before trusting
the parsing here — run `python -m tokenscore.probe <address>` to see raw
payloads. Treat this file as scaffolding with correct structure, not as
verified field mappings.
"""
from __future__ import annotations

import aiohttp

from .base import SourceAdapter, SourceResult, clamp, scale


# --------------------------------------------------------------------------
# DexScreener — free, no auth, generous rate limit. Market structure.
# --------------------------------------------------------------------------
class DexScreenerAdapter(SourceAdapter):
    name = "dexscreener"
    BASE = "https://api.dexscreener.com/latest/dex/tokens/"

    async def _fetch(self, session, address, chain) -> SourceResult:
        async with session.get(f"{self.BASE}{address}") as resp:
            if resp.status != 200:
                return SourceResult.failed(self.name, f"HTTP {resp.status}")
            data = await resp.json()

        pairs = data.get("pairs") or []
        if not pairs:
            # No pair yet is genuinely unknown, not bad. Low confidence, no gate.
            return SourceResult(
                self.name, ok=True, subscore=None, confidence=0.1,
                flags=["no_trading_pair"], raw=data,
            )

        # Deepest pool is the one that matters; ignore dust pairs.
        pair = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))

        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        vol24 = float((pair.get("volume") or {}).get("h24") or 0)
        txns24 = pair.get("txns", {}).get("h24", {}) or {}
        buys, sells = int(txns24.get("buys") or 0), int(txns24.get("sells") or 0)
        # marketCap is preferred when DexScreener has it; fdv (fully diluted)
        # is the fallback for tokens where circulating supply isn't resolved.
        market_cap = pair.get("marketCap") or pair.get("fdv")
        market_cap = float(market_cap) if market_cap else None

        gates, flags = [], []

        if liq < 5_000:
            gates.append("liquidity_below_5k")
        elif liq < 20_000:
            flags.append(f"thin_liquidity_${liq:,.0f}")

        # Volume far exceeding liquidity usually means wash trading.
        if liq > 0 and vol24 / liq > 50:
            flags.append("volume_liquidity_ratio_suspicious")

        # Near-total absence of sells is a honeypot tell.
        total_tx = buys + sells
        if total_tx > 30 and sells / max(total_tx, 1) < 0.05:
            gates.append("almost_no_sells_possible_honeypot")

        subscore = (
            0.45 * scale(liq, 5_000, 250_000)
            + 0.35 * scale(vol24, 10_000, 500_000)
            + 0.20 * scale(total_tx, 50, 2_000)
        )

        return SourceResult(
            self.name, ok=True, subscore=clamp(subscore), confidence=0.9,
            gate_failures=gates, flags=flags,
            raw={"liquidity_usd": liq, "volume_h24": vol24,
                 "buys": buys, "sells": sells, "pair": pair.get("pairAddress"),
                 "market_cap": market_cap},
        )


# --------------------------------------------------------------------------
# RugCheck — Solana risk scoring. Free tier ~3 req/s with a FluxRPC key.
# --------------------------------------------------------------------------
class RugCheckAdapter(SourceAdapter):
    name = "rugcheck"
    BASE = "https://api.rugcheck.xyz/v1/tokens/"

    async def _fetch(self, session, address, chain) -> SourceResult:
        if chain != "solana":
            return SourceResult.failed(self.name, "solana only")

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        async with session.get(f"{self.BASE}{address}/report", headers=headers) as resp:
            if resp.status != 200:
                return SourceResult.failed(self.name, f"HTTP {resp.status}")
            data = await resp.json()

        gates, flags = [], []

        for risk in data.get("risks") or []:
            level = (risk.get("level") or "").lower()
            label = risk.get("name") or "unknown_risk"
            if level in ("danger", "critical"):
                gates.append(f"rugcheck:{label}")
            elif level == "warn":
                flags.append(f"rugcheck:{label}")

        # RugCheck maintains its own "already confirmed rugged" flag on some
        # tokens. This is about as unambiguous as a signal gets.
        if data.get("rugged") is True:
            gates.append("rugcheck_marked_rugged")

        # RugCheck's own score: LOWER is safer. Invert it.
        raw_score = data.get("score_normalised", data.get("score"))
        subscore = None
        if raw_score is not None:
            subscore = clamp(1.0 - (float(raw_score) / 100.0))

        # --- Deployer history -------------------------------------------
        # "creatorTokens" is undocumented and not populated for every token
        # (verified null on several live tokens) -- treat it as a bonus
        # signal when present, never assume its absence means a clean
        # history. Handle a couple of plausible shapes defensively rather
        # than trusting one guessed structure.
        creator = data.get("creator")
        creator_balance = data.get("creatorBalance")
        creator_tokens_raw = data.get("creatorTokens")
        prior_tokens = prior_rugged = None
        if isinstance(creator_tokens_raw, list):
            prior_tokens = len(creator_tokens_raw)
            prior_rugged = sum(
                1 for t in creator_tokens_raw
                if isinstance(t, dict) and (t.get("rugged") or t.get("isRugged"))
            )
            if prior_rugged:
                gates.append(f"deployer_rugged_{prior_rugged}_prior_token"
                             f"{'s' if prior_rugged != 1 else ''}")
            elif prior_tokens and prior_tokens >= 5:
                flags.append(f"deployer_serial_launcher_{prior_tokens}_tokens")

        # --- Insider / connected-wallet clusters -------------------------
        # RugCheck clusters wallets it believes are connected (funded from
        # the same source, transferred between each other, etc). A large
        # cluster holding a meaningful chunk of supply is exactly the
        # "5,000 holders but a handful of real owners" trap.
        insider_networks = data.get("insiderNetworks") or []
        insiders_detected = data.get("graphInsidersDetected")
        total_supply_amt = None
        top_holders = data.get("topHolders") or []
        insider_pct = None
        if top_holders:
            insider_pct = sum(
                float(h.get("pct") or 0) for h in top_holders if h.get("insider")
            )
            if insider_pct and insider_pct > 30:
                gates.append(f"insider_wallets_hold_{insider_pct:.0f}pct")
            elif insider_pct and insider_pct > 10:
                flags.append(f"insider_wallets_hold_{insider_pct:.0f}pct")
        elif insider_networks:
            # No per-holder detail, but clusters were still detected.
            flags.append(f"insider_clusters_detected_{len(insider_networks)}")

        # LP status is the single most predictive field here.
        #
        # BUG FIX: RugCheck does not return "lpLockedPct" at the top level —
        # verified against a live response. It lives at markets[].lp.lpLockedPct,
        # one entry per pool. A token with no indexed pool has markets == null,
        # which is "unknown", not "0% locked" — treating it as 0 gated every
        # single token, including WSOL/USDC, regardless of real lock status.
        markets = data.get("markets") or []
        lp_locked = None
        if markets:
            weighted_sum, weight_total = 0.0, 0.0
            for market in markets:
                lp = market.get("lp") or {}
                pct = lp.get("lpLockedPct")
                if pct is None:
                    continue
                # Weight by pool size so the deepest pool dominates, same
                # principle as DexScreener picking its deepest pair.
                pool_usd = float(lp.get("quoteUSD") or 0) + float(lp.get("baseUSD") or 0)
                weight = max(pool_usd, 1.0)
                weighted_sum += float(pct) * weight
                weight_total += weight
            if weight_total > 0:
                lp_locked = weighted_sum / weight_total

        if lp_locked is None:
            # No pool RugCheck has indexed — flag as unknown, don't gate on it.
            flags.append("rugcheck_no_market_data")
        elif lp_locked < 50:
            gates.append(f"lp_locked_only_{lp_locked:.0f}pct")

        return SourceResult(
            self.name, ok=True, subscore=subscore, confidence=0.95,
            gate_failures=gates, flags=flags,
            raw={
                "score": raw_score,
                "lp_locked_pct": lp_locked,
                "risks": data.get("risks"),
                "rugged": data.get("rugged"),
                "creator": creator,
                "creator_balance": creator_balance,
                "prior_tokens": prior_tokens,
                "prior_rugged": prior_rugged,
                "insiders_detected": insiders_detected,
                "insider_pct": insider_pct,
                "insider_clusters": len(insider_networks),
                "total_holders": data.get("totalHolders"),
                "launchpad": (data.get("launchpad") or {}).get("name"),
            },
        )


# --------------------------------------------------------------------------
# GoPlus Security — free, multichain. Authority + honeypot checks.
# --------------------------------------------------------------------------
class GoPlusAdapter(SourceAdapter):
    name = "goplus"
    SOLANA = "https://api.gopluslabs.io/api/v1/solana/token_security"

    async def _fetch(self, session, address, chain) -> SourceResult:
        if chain != "solana":
            return SourceResult.failed(self.name, "configure EVM endpoint for this chain")

        params = {"contract_addresses": address}
        async with session.get(self.SOLANA, params=params) as resp:
            if resp.status != 200:
                return SourceResult.failed(self.name, f"HTTP {resp.status}")
            payload = await resp.json()

        result = (payload.get("result") or {}).get(address)
        if not result:
            return SourceResult.failed(self.name, "token not indexed")

        gates, flags = [], []

        # These are absolute. No weighting, no exceptions.
        if str((result.get("mintable") or {}).get("status")) == "1":
            gates.append("mint_authority_active")
        if str((result.get("freezable") or {}).get("status")) == "1":
            gates.append("freeze_authority_active")
        if str(result.get("transfer_hook")) not in ("", "0", "None", "null"):
            flags.append("transfer_hook_present")

        # closable: the developer can close the token program entirely,
        # wiping every holder's balance. Per GoPlus's own docs this is not a
        # cosmetic risk -- it is asset-destroying. Was previously a soft
        # flag; that undersold it.
        if str((result.get("closable") or {}).get("status")) == "1":
            gates.append("token_program_closable")

        # balance_mutable_authority: the developer can directly edit a
        # holder's balance outside of normal transfers -- functionally a
        # theft mechanism, not a risk to weigh against good news elsewhere.
        if str((result.get("balance_mutable_authority") or {}).get("status")) == "1":
            gates.append("balance_mutable_by_authority")

        # non_transferable: "1" means the token literally cannot be sent —
        # a hard honeypot, not a probabilistic one.
        if str(result.get("non_transferable")) == "1":
            gates.append("token_non_transferable")

        # default_account_state == "2" means every new holder account is
        # created frozen. Combined with an active freeze authority this is
        # a live "nobody can sell" switch, not a theoretical risk.
        if str(result.get("default_account_state")) == "2":
            gates.append("accounts_frozen_by_default")

        # GoPlus keeps its own blacklist of known-malicious deployer
        # addresses -- when it flags this token's own creator, trust it.
        creator_info = result.get("creator") or {}
        if isinstance(creator_info, dict) and str(creator_info.get("malicious")) == "1":
            gates.append("creator_flagged_malicious_by_goplus")

        # Transfer-fee (buy/sell tax) extension. GoPlus's exact fee-rate
        # field names for this are unconfirmed against a live populated
        # example (only saw it empty on tokens without the extension) --
        # rather than guess a percentage and risk repeating the RugCheck
        # lp_locked_pct mistake, only flag that the mechanism exists and
        # expose the raw block so a human can read the real numbers.
        transfer_fee = result.get("transfer_fee") or {}
        if transfer_fee:
            flags.append("transfer_fee_extension_present")

        holders = result.get("holders") or []
        top_pct = 0.0
        for h in holders[:10]:
            try:
                top_pct += float(h.get("percent") or 0)
            except (TypeError, ValueError):
                pass
        # GoPlus reports percent as 0..1 in some responses, 0..100 in others.
        if top_pct <= 1.0:
            top_pct *= 100

        if top_pct > 60:
            gates.append(f"top10_holds_{top_pct:.0f}pct")
        elif top_pct > 35:
            flags.append(f"concentrated_top10_{top_pct:.0f}pct")

        # Second opinion on LP lock, straight from GoPlus's own lp_holders
        # list -- independent of RugCheck's lockers/markets data. When the
        # two disagree that is itself worth surfacing, not averaging away.
        lp_holders = result.get("lp_holders") or []
        lp_locked_pct_goplus = None
        if lp_holders:
            locked = 0.0
            for h in lp_holders:
                try:
                    pct = float(h.get("percent") or 0)
                except (TypeError, ValueError):
                    continue
                if pct <= 1.0:
                    pct *= 100
                if str(h.get("is_locked")) in ("1", "True", "true"):
                    locked += pct
            lp_locked_pct_goplus = locked

        subscore = clamp(1.0 - scale(top_pct, 15, 70))

        return SourceResult(
            self.name, ok=True, subscore=subscore, confidence=0.85,
            gate_failures=gates, flags=flags,
            raw={
                "top10_pct": top_pct,
                "mintable": result.get("mintable"),
                "freezable": result.get("freezable"),
                "closable": result.get("closable"),
                "balance_mutable_authority": result.get("balance_mutable_authority"),
                "non_transferable": result.get("non_transferable"),
                "default_account_state": result.get("default_account_state"),
                "creator_malicious": creator_info.get("malicious") if isinstance(creator_info, dict) else None,
                "transfer_fee_present": bool(transfer_fee),
                "holder_count": result.get("holder_count"),
                "lp_locked_pct": lp_locked_pct_goplus,
            },
        )


# --------------------------------------------------------------------------
# Birdeye — richest analytics, free tier 30k CU/month. Needs a key.
# --------------------------------------------------------------------------
class BirdeyeAdapter(SourceAdapter):
    name = "birdeye"
    requires_key = True
    BASE = "https://public-api.birdeye.so/defi/token_overview"

    async def _fetch(self, session, address, chain) -> SourceResult:
        headers = {"X-API-KEY": self.api_key, "x-chain": chain}
        async with session.get(self.BASE, params={"address": address},
                               headers=headers) as resp:
            if resp.status == 429:
                return SourceResult.failed(self.name, "rate limited")
            if resp.status != 200:
                return SourceResult.failed(self.name, f"HTTP {resp.status}")
            payload = await resp.json()

        data = payload.get("data") or {}
        if not data:
            return SourceResult.failed(self.name, "empty response")

        holders = int(data.get("holder") or 0)
        unique24 = int(data.get("uniqueWallet24h") or 0)
        change = data.get("uniqueWallet24hChangePercent")

        flags = []
        if holders < 100:
            flags.append(f"only_{holders}_holders")
        if change is not None and float(change) < -40:
            flags.append("holder_count_falling_fast")

        subscore = (
            0.5 * scale(holders, 50, 3_000)
            + 0.5 * scale(unique24, 25, 1_000)
        )

        return SourceResult(
            self.name, ok=True, subscore=clamp(subscore), confidence=0.8,
            flags=flags,
            raw={"holders": holders, "unique_wallets_24h": unique24,
                 "liquidity": data.get("liquidity"), "mc": data.get("mc")},
        )


# --------------------------------------------------------------------------
# Solscan — authoritative on-chain metadata. Pro API requires a key.
# --------------------------------------------------------------------------
class SolscanAdapter(SourceAdapter):
    name = "solscan"
    requires_key = True
    BASE = "https://pro-api.solscan.io/v2.0/token/meta"

    async def _fetch(self, session, address, chain) -> SourceResult:
        headers = {"token": self.api_key}
        async with session.get(self.BASE, params={"address": address},
                               headers=headers) as resp:
            if resp.status != 200:
                return SourceResult.failed(self.name, f"HTTP {resp.status}")
            payload = await resp.json()

        data = payload.get("data") or {}
        flags = []

        # Cross-check authorities against GoPlus. Disagreement is itself signal.
        if data.get("mint_authority"):
            flags.append("solscan_reports_mint_authority")
        if data.get("freeze_authority"):
            flags.append("solscan_reports_freeze_authority")

        holders = int(data.get("holder") or 0)
        subscore = scale(holders, 50, 3_000)

        return SourceResult(
            self.name, ok=True, subscore=clamp(subscore), confidence=0.7,
            flags=flags,
            raw={"holders": holders,
                 "mint_authority": data.get("mint_authority"),
                 "freeze_authority": data.get("freeze_authority"),
                 "created_time": data.get("created_time")},
        )


ALL_ADAPTERS = [
    DexScreenerAdapter,
    RugCheckAdapter,
    GoPlusAdapter,
    BirdeyeAdapter,
    SolscanAdapter,
]
