"""
Telegram message rendering.

Design rule: every alert must show WHY. A bare number teaches you nothing and
can't be audited later. Show the per-source breakdown, the flags, and any
disagreement — you'll be reading these fifty times a day.
"""
from __future__ import annotations

from html import escape

MAX_LEN = 4096
SAFE_LEN = 3900  # leave headroom for keyboard callbacks

BAR_FULL = "█"
BAR_EMPTY = "░"


def bar(value: float, width: int = 10) -> str:
    filled = int(round(value * width))
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def money(value: float | None) -> str:
    if value is None:
        return "?"
    v = float(value)
    for unit, div in (("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"${v / div:.1f}{unit}"
    return f"${v:,.0f}"


def short(address: str, keep: int = 4) -> str:
    return f"{address[:keep]}…{address[-keep:]}" if len(address) > 12 else address


def truncate(text: str, limit: int = SAFE_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 40] + "\n\n<i>…truncated</i>"


def render_verdict(verdict, symbol: str | None = None,
                   market: dict | None = None) -> str:
    """Full scorecard for /score and /check."""
    addr = verdict.address
    title = escape(symbol or short(addr))

    if verdict.verdict == "GATED":
        lines = [
            f"🛑 <b>{title}</b> — <b>REJECTED</b>",
            f"<code>{escape(addr)}</code>",
            "",
            "<b>Fatal findings:</b>",
        ]
        for g in verdict.gate_failures:
            lines.append(f"  ✖️ {escape(g)}")
        lines += [
            "",
            f"<i>{verdict.sources_ok}/{verdict.sources_total} sources reporting</i>",
            "<i>Gated tokens are not scored.</i>",
        ]
        return truncate("\n".join(lines))

    score = verdict.score or 0
    icon = "🟢" if score >= 75 else "🟡" if score >= 55 else "🟠"
    conf_icon = "" if verdict.confidence >= 0.6 else " ⚠️"

    lines = [
        f"{icon} <b>{title}</b> — <b>{score:.0f}</b>/100",
        f"<code>{escape(addr)}</code>",
        "",
        f"confidence  {bar(verdict.confidence)} {verdict.confidence:.0%}{conf_icon}",
    ]

    if market:
        lines.append(
            f"liq {money(market.get('liquidity_usd'))}  ·  "
            f"vol24 {money(market.get('volume_h24'))}"
        )

    lines.append("")
    lines.append("<b>Sources</b>")
    for name, detail in (verdict.breakdown or {}).items():
        if "subscore" in detail:
            sub = detail["subscore"]
            lines.append(
                f"  {bar(sub, 8)} <b>{sub:.2f}</b>  {escape(name)} "
                f"<i>(w{detail['weight']})</i>"
            )
        else:
            note = detail.get("error") or "no data"
            lines.append(f"  {BAR_EMPTY * 8}  ---   {escape(name)} <i>({escape(str(note))})</i>")

    if verdict.disagreements:
        lines += ["", "<b>⚠️ Sources conflict</b>"]
        for d in verdict.disagreements:
            lines.append(f"  • {escape(d)}")

    if verdict.flags:
        lines += ["", "<b>Flags</b>"]
        for f in verdict.flags[:10]:
            lines.append(f"  • {escape(f)}")
        if len(verdict.flags) > 10:
            lines.append(f"  <i>+{len(verdict.flags) - 10} more</i>")

    if verdict.confidence < 0.6:
        lines += ["", "<i>⚠️ Low confidence — sources missing or contradicting. "
                      "Treat this score as weak evidence.</i>"]

    return truncate("\n".join(lines))


def render_ranked(title: str, rows: list[dict], note: str | None = None) -> str:
    """Compact leaderboard for /screen, /early, /alpha."""
    lines = [f"<b>{escape(title)}</b>"]
    if note:
        lines.append(f"<i>{escape(note)}</i>")
    lines.append("")

    if not rows:
        lines.append("<i>Nothing passed the filters.</i>")
        return "\n".join(lines)

    for i, row in enumerate(rows, 1):
        sym = escape(row.get("symbol") or short(row["address"]))
        score = row.get("score")
        if score is None:
            lines.append(f"{i}. 🛑 <b>{sym}</b> — gated")
        else:
            icon = "🟢" if score >= 75 else "🟡" if score >= 55 else "🟠"
            conf = row.get("confidence", 0)
            lines.append(
                f"{i}. {icon} <b>{sym}</b> — {score:.0f} "
                f"<i>(conf {conf:.0%})</i>"
            )
        lines.append(f"    <code>{escape(row['address'])}</code>")
        if row.get("liquidity_usd") is not None:
            lines.append(
                f"    liq {money(row['liquidity_usd'])} · "
                f"vol {money(row.get('volume_h24'))}"
            )
        lines.append("")

    return truncate("\n".join(lines))


def render_pairs(pairs: list[dict], header: str) -> str:
    lines = [f"<b>{escape(header)}</b>", ""]
    if not pairs:
        lines.append("<i>No pairs found.</i>")
        return "\n".join(lines)

    for p in pairs[:12]:
        base = (p.get("baseToken") or {}).get("symbol") or "?"
        quote = (p.get("quoteToken") or {}).get("symbol") or "?"
        liq = (p.get("liquidity") or {}).get("usd")
        vol = (p.get("volume") or {}).get("h24")
        chg = (p.get("priceChange") or {}).get("h24")
        arrow = "▲" if (chg or 0) > 0 else "▼"
        lines.append(
            f"<b>{escape(base)}/{escape(quote)}</b> on {escape(p.get('dexId') or '?')}\n"
            f"  liq {money(liq)} · vol24 {money(vol)} · "
            f"{arrow}{abs(chg or 0):.1f}%\n"
            f"  <code>{escape(p.get('pairAddress') or '')}</code>"
        )
        lines.append("")

    return truncate("\n".join(lines))


def render_error(command: str, error: str) -> str:
    return (
        f"⚠️ <b>/{escape(command)} failed</b>\n\n"
        f"<code>{escape(error[:400])}</code>\n\n"
        f"<i>Source may be rate limited or down. Retry shortly.</i>"
    )
