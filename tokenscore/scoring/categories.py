"""
Checklist-style analysis, ordered and weighted the way a careful degen
actually works through a token: contract safety first, narrative last.

This is a presentation layer on top of the SAME SourceResult data the main
scoring engine (engine.combine) already consumes -- it does not replace or
alter Verdict/combine(), and nothing here changes what the Telegram bot
shows. It re-reads the adapters' raw fields into named checklist items,
grouped into the seven categories below, each weighted and scored 0-100.

Two categories don't have a real automatable signal from the sources this
project has:
  - community:  no social/API source exists here, so this is a clearly
                labelled weak proxy from on-chain activity, never presented
                as a real community-size measurement.
  - narrative:  genuinely not automatable. Score is left as None and
                excluded from the composite rather than faked.

Same "missing != bad" principle as engine.py: a category with no data
lowers the composite's denominator, not its numerator.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CATEGORY_WEIGHTS: dict[str, int] = {
    "contract_security": 25,
    "liquidity": 20,
    "holders": 15,
    "volume": 15,
    "deployer": 10,
    "community": 10,
    "narrative": 5,
}

CATEGORY_LABELS: dict[str, str] = {
    "contract_security": "Contract safety",
    "liquidity": "Liquidity quality",
    "holders": "Holder distribution",
    "volume": "Volume & buy/sell quality",
    "deployer": "Deployer history",
    "community": "Community / attention",
    "narrative": "Narrative",
}


@dataclass
class CheckItem:
    label: str
    status: str  # "pass" | "warn" | "fail" | "unknown"
    detail: str = ""


@dataclass
class Category:
    key: str
    label: str
    weight: int
    score: float | None  # 0..100; None means not automatable / no data
    items: list[CheckItem] = field(default_factory=list)
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "weight": self.weight,
            "score": self.score,
            "items": [
                {"label": i.label, "status": i.status, "detail": i.detail}
                for i in self.items
            ],
            "note": self.note,
        }


def _raw(results_by_source: dict, name: str) -> dict:
    r = results_by_source.get(name)
    return (r.raw if r and r.ok else {}) or {}


def _score_from_items(items: list[CheckItem], fail_zeroes: bool = True) -> float | None:
    """Simple, legible scoring: fail is heavily penalised, warn moderately,
    unknown items are excluded (don't punish missing data). Matches the
    checklist's own logic -- a single hard fail should read as failed, not
    as "85/100, pretty good"."""
    scored = [i for i in items if i.status != "unknown"]
    if not scored:
        return None
    if fail_zeroes and any(i.status == "fail" for i in scored):
        return 0.0
    penalty = sum(0.0 if i.status == "pass" else 20.0 if i.status == "warn" else 40.0 for i in scored)
    return max(0.0, 100.0 - penalty)


def _contract_security(by_source: dict) -> Category:
    gp = _raw(by_source, "goplus")
    rug = _raw(by_source, "rugcheck")
    items: list[CheckItem] = []

    def gp_active(field: str) -> bool | None:
        block = gp.get(field)
        if not isinstance(block, dict):
            return None
        return str(block.get("status")) == "1"

    mint = gp_active("mintable")
    items.append(CheckItem("No active mint authority",
                            "unknown" if mint is None else ("fail" if mint else "pass"),
                            "Deployer can create unlimited new supply at will" if mint else ""))

    freeze = gp_active("freezable")
    items.append(CheckItem("No freeze / wallet-blacklist authority",
                            "unknown" if freeze is None else ("fail" if freeze else "pass"),
                            "Deployer can freeze your specific wallet's tokens" if freeze else ""))

    closable = gp_active("closable")
    items.append(CheckItem("Token program cannot be closed by the owner",
                            "unknown" if closable is None else ("fail" if closable else "pass"),
                            "Closing the program can wipe every holder's balance" if closable else ""))

    mutable_bal = gp_active("balance_mutable_authority")
    items.append(CheckItem("Owner cannot directly edit holder balances",
                            "unknown" if mutable_bal is None else ("fail" if mutable_bal else "pass"),
                            "This authority is a direct theft mechanism" if mutable_bal else ""))

    non_transferable = gp.get("non_transferable")
    frozen_default = gp.get("default_account_state")
    honeypot = str(non_transferable) == "1" or str(frozen_default) == "2"
    honeypot_unknown = non_transferable is None and frozen_default is None
    items.append(CheckItem("Not a structural honeypot",
                            "unknown" if honeypot_unknown else ("fail" if honeypot else "pass"),
                            "Token cannot be transferred / new accounts are frozen by default" if honeypot else ""))

    tax_present = bool(gp.get("transfer_fee_present"))
    items.append(CheckItem("No punishing buy/sell tax mechanism",
                            "warn" if tax_present else "pass",
                            "Transfer-fee extension is active — verify the real rate before trading" if tax_present else ""))

    malicious = gp.get("creator_malicious")
    is_malicious = str(malicious) == "1"
    items.append(CheckItem("Deployer not on a known-malicious list",
                            "unknown" if malicious is None else ("fail" if is_malicious else "pass"),
                            "GoPlus's own blacklist flags this creator address" if is_malicious else ""))

    rugged = rug.get("rugged")
    items.append(CheckItem("Not already confirmed rugged",
                            "unknown" if rugged is None else ("fail" if rugged else "pass"),
                            "RugCheck's own database marks this token as rugged" if rugged else ""))

    risky = [r for r in (rug.get("risks") or []) if (r.get("level") or "").lower() in ("danger", "critical")]
    items.append(CheckItem("No critical findings from RugCheck's risk scan",
                            "fail" if risky else ("unknown" if not rug else "pass"),
                            ", ".join(r.get("name", "?") for r in risky) if risky else ""))

    return Category("contract_security", CATEGORY_LABELS["contract_security"],
                     CATEGORY_WEIGHTS["contract_security"], _score_from_items(items), items,
                     note="A single fail here should be read as disqualifying, regardless of "
                          "how good everything else looks — that's the whole point of gates.")


def _liquidity(by_source: dict) -> Category:
    dex = _raw(by_source, "dexscreener")
    rug = _raw(by_source, "rugcheck")
    gp = _raw(by_source, "goplus")
    items: list[CheckItem] = []

    liq = dex.get("liquidity_usd")
    if liq is None:
        items.append(CheckItem("Liquidity depth", "unknown"))
    else:
        status = "pass" if liq >= 50_000 else "warn" if liq >= 20_000 else "fail"
        items.append(CheckItem("Liquidity depth", status, f"${liq:,.0f} in the deepest pool"))

    mcap = dex.get("market_cap")
    if mcap and liq:
        ratio = liq / mcap if mcap > 0 else 0
        status = "pass" if ratio >= 0.15 else "warn" if ratio >= 0.05 else "fail"
        items.append(CheckItem("Liquidity vs. market cap",
                                status,
                                f"liquidity is {ratio:.0%} of market cap — a low ratio means a "
                                f"relatively small sell can move the price a lot"))
    else:
        items.append(CheckItem("Liquidity vs. market cap", "unknown", "market cap not available from current sources"))

    lp_rug = rug.get("lp_locked_pct")
    lp_gp = gp.get("lp_locked_pct")
    lp_best = lp_rug if lp_rug is not None else lp_gp
    if lp_best is None:
        items.append(CheckItem("LP locked or burned", "unknown", "no indexed pool lock data from either RugCheck or GoPlus"))
    else:
        status = "pass" if lp_best >= 80 else "warn" if lp_best >= 50 else "fail"
        detail = f"~{lp_best:.0f}% locked"
        if lp_rug is not None and lp_gp is not None and abs(lp_rug - lp_gp) > 20:
            detail += f" (RugCheck and GoPlus disagree: {lp_rug:.0f}% vs {lp_gp:.0f}% — treat as uncertain)"
        items.append(CheckItem("LP locked or burned", status, detail))

    return Category("liquidity", CATEGORY_LABELS["liquidity"], CATEGORY_WEIGHTS["liquidity"],
                     _score_from_items(items), items,
                     note="Unlocked, removable liquidity is the classic rug-pull setup no matter "
                          "how good the other numbers look.")


def _holders(by_source: dict) -> Category:
    gp = _raw(by_source, "goplus")
    rug = _raw(by_source, "rugcheck")
    items: list[CheckItem] = []

    top10 = gp.get("top10_pct")
    if top10 is None:
        items.append(CheckItem("Top-10 wallet concentration", "unknown"))
    else:
        status = "pass" if top10 < 35 else "warn" if top10 < 60 else "fail"
        holder_count = rug.get("total_holders") or gp.get("holder_count")
        detail = f"top 10 wallets hold {top10:.0f}% of supply"
        if holder_count:
            detail += f" (across {holder_count} total holders — a big headline count doesn't rule out concentration)"
        items.append(CheckItem("Top-10 wallet concentration", status, detail))

    insider_pct = rug.get("insider_pct")
    insider_clusters = rug.get("insider_clusters") or 0
    if insider_pct is not None:
        status = "pass" if insider_pct < 10 else "warn" if insider_pct < 30 else "fail"
        items.append(CheckItem("Connected / insider wallets", status,
                                f"wallets RugCheck links together hold {insider_pct:.0f}% of supply"))
    elif insider_clusters:
        items.append(CheckItem("Connected / insider wallets", "warn",
                                f"{insider_clusters} connected-wallet cluster(s) detected, holdings not itemised"))
    else:
        items.append(CheckItem("Connected / insider wallets", "unknown"))

    return Category("holders", CATEGORY_LABELS["holders"], CATEGORY_WEIGHTS["holders"],
                     _score_from_items(items), items,
                     note="5,000 holders can still mean a handful of real owners — always check "
                          "concentration, never just the headline holder count.")


def _volume(by_source: dict) -> Category:
    dex = _raw(by_source, "dexscreener")
    items: list[CheckItem] = []

    buys, sells = dex.get("buys"), dex.get("sells")
    if buys is None or sells is None:
        items.append(CheckItem("Organic transaction volume", "unknown"))
        items.append(CheckItem("Real sell-side activity", "unknown"))
    else:
        total = buys + sells
        status = "pass" if total >= 100 else "warn" if total >= 30 else "fail"
        items.append(CheckItem("Organic transaction volume", status, f"{buys} buys / {sells} sells in 24h"))

        sell_ratio = sells / total if total else 0
        if total < 20:
            items.append(CheckItem("Real sell-side activity", "unknown", "too little volume to judge"))
        else:
            status = "pass" if sell_ratio >= 0.15 else "warn" if sell_ratio >= 0.05 else "fail"
            items.append(CheckItem(
                "Real sell-side activity", status,
                f"{sell_ratio:.0%} of trades are sells — heavy buy skew alone isn't bullish, it can "
                f"mean a honeypot, bundled buys, or bots"
            ))

    liq, vol = dex.get("liquidity_usd"), dex.get("volume_h24")
    if liq and vol:
        ratio = vol / liq if liq else 0
        status = "pass" if ratio <= 15 else "warn" if ratio <= 50 else "fail"
        items.append(CheckItem("Volume not wildly out of proportion to liquidity", status,
                                f"24h volume is {ratio:.0f}x liquidity" + (" — classic wash-trading pattern" if ratio > 50 else "")))
    else:
        items.append(CheckItem("Volume not wildly out of proportion to liquidity", "unknown"))

    return Category("volume", CATEGORY_LABELS["volume"], CATEGORY_WEIGHTS["volume"],
                     _score_from_items(items), items,
                     note="A wall of buys and almost no sells doesn't confirm demand — confirm "
                          "people can actually exit before reading it as bullish.")


def _deployer(by_source: dict) -> Category:
    rug = _raw(by_source, "rugcheck")
    items: list[CheckItem] = []

    rugged = rug.get("rugged")
    if rugged is not None:
        items.append(CheckItem("This token isn't already flagged as rugged", "fail" if rugged else "pass"))
    else:
        items.append(CheckItem("This token isn't already flagged as rugged", "unknown"))

    prior_tokens, prior_rugged = rug.get("prior_tokens"), rug.get("prior_rugged")
    if prior_tokens is None:
        items.append(CheckItem("Deployer's prior token history", "unknown",
                                "not available for this token — RugCheck doesn't populate this for every mint"))
    elif prior_rugged:
        items.append(CheckItem("Deployer's prior token history", "fail",
                                f"{prior_rugged} of {prior_tokens} previous token(s) by this wallet were rugged"))
    elif prior_tokens >= 5:
        items.append(CheckItem("Deployer's prior token history", "warn",
                                f"serial launcher — {prior_tokens} previous tokens, none flagged rugged"))
    else:
        items.append(CheckItem("Deployer's prior token history", "pass",
                                f"{prior_tokens} previous token(s), none flagged rugged"))

    creator, launchpad = rug.get("creator"), rug.get("launchpad")
    detail_bits = []
    if creator:
        detail_bits.append(f"creator {creator[:4]}…{creator[-4:]}")
    if launchpad:
        detail_bits.append(f"launched via {launchpad}")
    note = " · ".join(detail_bits) if detail_bits else None

    return Category("deployer", CATEGORY_LABELS["deployer"], CATEGORY_WEIGHTS["deployer"],
                     _score_from_items(items), items, note=note)


def _community(by_source: dict) -> Category:
    dex = _raw(by_source, "dexscreener")
    items: list[CheckItem] = []

    buys, sells, vol = dex.get("buys"), dex.get("sells"), dex.get("volume_h24")
    if buys is None:
        items.append(CheckItem("On-chain activity level", "unknown"))
    else:
        total = buys + sells
        status = "pass" if total >= 500 else "warn" if total >= 100 else "fail"
        items.append(CheckItem("On-chain activity level", status,
                                f"{total} trades / ${vol or 0:,.0f} 24h volume"))

    return Category("community", CATEGORY_LABELS["community"], CATEGORY_WEIGHTS["community"],
                     _score_from_items(items), items,
                     note="This is a proxy from on-chain trading activity only — none of the "
                          "connected sources measure real social reach (Twitter/Discord/Telegram "
                          "following). Verify community size yourself; don't trust this number alone.")


def _narrative() -> Category:
    return Category("narrative", CATEGORY_LABELS["narrative"], CATEGORY_WEIGHTS["narrative"], None, [
        CheckItem("Story, roadmap, and website quality", "unknown",
                  "not something on-chain data can verify"),
    ], note="Deliberately not scored. A polished website, an AI-written roadmap, and a big "
            "follower count are all consistent with a well-executed rug — this should be the "
            "last thing you weigh, never the first.")


def build_analysis(verdict, results: list) -> dict:
    by_source = {r.source: r for r in results}

    categories = [
        _contract_security(by_source),
        _liquidity(by_source),
        _holders(by_source),
        _volume(by_source),
        _deployer(by_source),
        _community(by_source),
        _narrative(),
    ]

    rejected = verdict.verdict == "GATED"
    scored = [c for c in categories if c.score is not None]
    weight_available = sum(c.weight for c in scored)
    if rejected:
        composite = 0.0
    elif weight_available:
        composite = sum(c.score * c.weight for c in scored) / weight_available
    else:
        composite = None

    return {
        "rejected": rejected,
        "composite_score": round(composite, 1) if composite is not None else None,
        "weight_covered": weight_available,
        "weight_total": sum(CATEGORY_WEIGHTS.values()),
        "categories": [c.as_dict() for c in categories],
    }
