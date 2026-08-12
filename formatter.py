"""
formatter.py

Turns a Signal (from signal_engine.py) into a Telegram-ready message.
Uses HTML parse mode. Flat stake only — no martingale ladder.
"""

from datetime import datetime
from signal_engine import Signal, Direction

FLAT_STAKE_LABEL = "Flat stake"  # actual $ amount is set by the user in config, not hardcoded here


def format_signal(signal: Signal, pair: str, timeframe: str, stake_amount: str = None) -> str:
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    if signal.direction == Direction.ABSTAIN:
        return _format_abstain(signal, pair, timeframe, ts)

    arrow = "🟢 CALL ▲" if signal.direction == Direction.CALL else "🔴 PUT ▼"
    stake_line = f"{FLAT_STAKE_LABEL}: {stake_amount}" if stake_amount else FLAT_STAKE_LABEL

    lines = [
        f"<b>{pair}</b> · {timeframe}",
        f"{arrow}",
        "",
        f"Confidence: <b>{signal.confidence:.0f}%</b>  ({signal.reason})",
        f"Price: {signal.price}",
        "",
        "<b>Vote breakdown</b>",
    ]
    for v in signal.votes:
        icon = {"CALL": "✅", "PUT": "✅", "ABSTAIN": "⚪️"}[v.direction.value]
        lines.append(f"{icon} {v.name}: {v.direction.value} — {v.detail}")

    lines += ["", stake_line, "", f"<i>{ts}</i>"]
    return "\n".join(lines)


def _format_abstain(signal: Signal, pair: str, timeframe: str, ts: str) -> str:
    lines = [
        f"<b>{pair}</b> · {timeframe}",
        "⚪️ NO SIGNAL — staying out",
        "",
        f"Reason: {signal.reason}",
    ]
    if signal.votes:
        lines.append("")
        lines.append("<b>Vote breakdown</b>")
        for v in signal.votes:
            icon = "✅" if v.direction != Direction.ABSTAIN else "⚪️"
            lines.append(f"{icon} {v.name}: {v.direction.value} — {v.detail}")
    lines += ["", f"<i>{ts}</i>"]
    return "\n".join(lines)
