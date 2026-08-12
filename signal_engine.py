"""
signal_engine.py

EMA + RSI + volatility confluence engine.
Votes-based: each indicator casts a vote (CALL / PUT / NEUTRAL).
Fires only when votes agree strongly enough. Abstains otherwise.

Input: a list of OHLC candles (oldest -> newest), each a dict:
    {"open": float, "high": float, "low": float, "close": float}

Output: a Signal (see dataclass below), which may be a real signal
or an ABSTAIN with the reasoning attached.
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class Direction(Enum):
    CALL = "CALL"
    PUT = "PUT"
    ABSTAIN = "ABSTAIN"


@dataclass
class VoteResult:
    name: str
    direction: Direction
    detail: str


@dataclass
class Signal:
    direction: Direction
    confidence: float  # 0-100, real vote-agreement based, not fabricated
    votes: list = field(default_factory=list)  # list[VoteResult]
    price: Optional[float] = None
    reason: str = ""


# ---------- indicator math ----------

def ema(values: list, period: int) -> list:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for price in values[period:]:
        out.append(price * k + out[-1] * (1 - k))
    return out


def rsi(closes: list, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def volatility(candles: list, period: int = 14) -> Optional[float]:
    """Average true range as a simple volatility proxy."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, prev_close = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
    return sum(trs[-period:]) / period


# ---------- voting logic ----------

EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
RSI_EXTREME_OVERBOUGHT = 85  # deep overextension -> treated as too risky, not a CALL/PUT vote
RSI_EXTREME_OVERSOLD = 15
VOL_PERIOD = 14
VOL_CHOPPY_MULTIPLIER = 1.8  # if current TR vs avg ATR is this much higher, market is choppy -> abstain-leaning
MIN_CANDLES = EMA_SLOW + 5

# confluence rule: need at least 2 of 3 votes agreeing on the same
# non-neutral direction to fire at all. All 3 agreeing = highest confidence.
MIN_AGREEING_VOTES = 2


def _ema_vote(closes: list) -> VoteResult:
    fast = ema(closes, EMA_FAST)
    slow = ema(closes, EMA_SLOW)
    if not fast or not slow:
        return VoteResult("EMA", Direction.ABSTAIN, "insufficient data")
    f, s = fast[-1], slow[-1]
    spread_pct = abs(f - s) / s * 100 if s else 0
    if f > s and spread_pct > 0.02:
        return VoteResult("EMA", Direction.CALL, f"EMA{EMA_FAST} above EMA{EMA_SLOW} ({spread_pct:.3f}% spread)")
    elif f < s and spread_pct > 0.02:
        return VoteResult("EMA", Direction.PUT, f"EMA{EMA_FAST} below EMA{EMA_SLOW} ({spread_pct:.3f}% spread)")
    return VoteResult("EMA", Direction.ABSTAIN, "EMAs too close / no clear trend")


def _rsi_vote(closes: list) -> VoteResult:
    r = rsi(closes, RSI_PERIOD)
    if r is None:
        return VoteResult("RSI", Direction.ABSTAIN, "insufficient data")
    if r >= RSI_EXTREME_OVERBOUGHT or r <= RSI_EXTREME_OVERSOLD:
        # deep overextension is treated as too risky to chase, not a directional vote
        return VoteResult("RSI", Direction.ABSTAIN, f"RSI {r:.1f} deep overextension, too risky to chase")
    if r >= RSI_OVERBOUGHT:
        return VoteResult("RSI", Direction.PUT, f"RSI {r:.1f} overbought, favors reversal down")
    if r <= RSI_OVERSOLD:
        return VoteResult("RSI", Direction.CALL, f"RSI {r:.1f} oversold, favors reversal up")
    # mid-range RSI leans with momentum direction
    if r > 55:
        return VoteResult("RSI", Direction.CALL, f"RSI {r:.1f} mid-range bullish momentum")
    if r < 45:
        return VoteResult("RSI", Direction.PUT, f"RSI {r:.1f} mid-range bearish momentum")
    return VoteResult("RSI", Direction.ABSTAIN, f"RSI {r:.1f} neutral zone")


def _volatility_vote(candles: list, closes: list) -> VoteResult:
    atr = volatility(candles, VOL_PERIOD)
    if atr is None:
        return VoteResult("Volatility", Direction.ABSTAIN, "insufficient data")
    last = candles[-1]
    current_tr = max(
        last["high"] - last["low"],
        abs(last["high"] - candles[-2]["close"]),
        abs(last["low"] - candles[-2]["close"]),
    )
    if current_tr > atr * VOL_CHOPPY_MULTIPLIER:
        return VoteResult("Volatility", Direction.ABSTAIN, f"current range {current_tr:.5f} vs ATR {atr:.5f} — choppy/erratic")
    # calm/normal volatility supports whatever direction the last candle closed
    last_body_up = last["close"] > last["open"]
    if last_body_up:
        return VoteResult("Volatility", Direction.CALL, f"stable volatility, last candle bullish")
    else:
        return VoteResult("Volatility", Direction.PUT, f"stable volatility, last candle bearish")


def evaluate(candles: list) -> Signal:
    """Main entry point. Pass a list of OHLC candle dicts, oldest first."""
    if len(candles) < MIN_CANDLES:
        return Signal(
            direction=Direction.ABSTAIN,
            confidence=0.0,
            reason=f"Need at least {MIN_CANDLES} candles, got {len(candles)}",
        )

    closes = [c["close"] for c in candles]

    votes = [
        _ema_vote(closes),
        _rsi_vote(closes),
        _volatility_vote(candles, closes),
    ]

    call_votes = [v for v in votes if v.direction == Direction.CALL]
    put_votes = [v for v in votes if v.direction == Direction.PUT]

    if len(call_votes) >= MIN_AGREEING_VOTES and len(call_votes) > len(put_votes):
        direction = Direction.CALL
        agreeing = call_votes
    elif len(put_votes) >= MIN_AGREEING_VOTES and len(put_votes) > len(call_votes):
        direction = Direction.PUT
        agreeing = put_votes
    else:
        return Signal(
            direction=Direction.ABSTAIN,
            confidence=0.0,
            votes=votes,
            price=closes[-1],
            reason=f"No confluence ({len(call_votes)} CALL / {len(put_votes)} PUT votes) — staying out",
        )

    # confidence = agreeing votes out of 3, scaled — real number from actual agreement,
    # not a fabricated figure
    confidence = (len(agreeing) / 3) * 100

    return Signal(
        direction=direction,
        confidence=confidence,
        votes=votes,
        price=closes[-1],
        reason=f"{len(agreeing)}/3 indicators agree on {direction.value}",
    )
