# -*- coding: utf-8 -*-
"""agents.py - 20-agent consensus engine, 1% risk shield and AI mentor for Tactical Neural Matrix.

Pure Python (standard library only). Every agent returns a live status:
signal (BUY / SELL / NEUTRAL), confidence 0..1 and a text with the numbers it used.
Three agents are guards that can veto a trade. The Risk Manager enforces the 1% cap.
"""
import json
import math
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

BUY = "BUY"
SELL = "SELL"
NEUTRAL = "NEUTRAL"

MAX_RISK_PCT = 1.0        # hard cap: a stop-out never loses more than 1% of equity
MIN_RR = 2.0              # minimum reward : risk
MIN_AGREE = 9             # agents (of 20) that must point the same way
MIN_CANDLES = 60
MAX_CONSECUTIVE_LOSSES = 2


@dataclass
class Candle:
    t: float
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


@dataclass
class Vote:
    agent: str
    signal: str = NEUTRAL
    confidence: float = 0.0
    detail: str = ""
    veto: bool = False


@dataclass
class Decision:
    symbol: str
    direction: str = NEUTRAL
    approved: bool = False
    agree: int = 0
    against: int = 0
    total: int = 20
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    rr: float = 0.0
    qty: int = 0
    risk_amount: float = 0.0
    reason: str = ""
    votes: List[Vote] = field(default_factory=list)
    ts: float = field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# Indicators (pure Python)
# --------------------------------------------------------------------------- #
def ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out, e = [], values[0]
    for v in values:
        e = v * k + e * (1.0 - k)
        out.append(e)
    return out


def sma(values: List[float], period: int) -> float:
    part = values[-period:]
    return sum(part) / len(part) if part else 0.0


def rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 50.0
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def true_ranges(candles: List[Candle]) -> List[float]:
    out = []
    for i, c in enumerate(candles):
        if i == 0:
            out.append(c.h - c.l)
        else:
            pc = candles[i - 1].c
            out.append(max(c.h - c.l, abs(c.h - pc), abs(c.l - pc)))
    return out


def atr(candles: List[Candle], period: int = 14) -> float:
    trs = true_ranges(candles)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def adx(candles: List[Candle], period: int = 14):
    """Returns (adx, +DI, -DI)."""
    if len(candles) < period * 2 + 2:
        return 0.0, 0.0, 0.0
    pdm, mdm, trs = [], [], []
    for i in range(1, len(candles)):
        up = candles[i].h - candles[i - 1].h
        dn = candles[i - 1].l - candles[i].l
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
        pc = candles[i - 1].c
        trs.append(max(candles[i].h - candles[i].l, abs(candles[i].h - pc), abs(candles[i].l - pc)))
    sp_, sm_, st_ = sum(pdm[:period]), sum(mdm[:period]), sum(trs[:period])
    dxs, pdi, mdi = [], 0.0, 0.0
    for i in range(period, len(trs) + 1):
        if i > period:
            sp_ = sp_ - sp_ / period + pdm[i - 1]
            sm_ = sm_ - sm_ / period + mdm[i - 1]
            st_ = st_ - st_ / period + trs[i - 1]
        if st_ <= 0:
            continue
        pdi, mdi = 100.0 * sp_ / st_, 100.0 * sm_ / st_
        dxs.append(100.0 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0.0)
    if not dxs:
        return 0.0, 0.0, 0.0
    val = sum(dxs[:period]) / min(period, len(dxs))
    for d in dxs[period:]:
        val = (val * (period - 1) + d) / period
    return val, pdi, mdi


def swings(candles: List[Candle], k: int = 2):
    """Swing points as (index, 'H'|'L', price)."""
    out = []
    for i in range(k, len(candles) - k):
        win = candles[i - k:i + k + 1]
        if candles[i].h == max(c.h for c in win):
            out.append((i, "H", candles[i].h))
        if candles[i].l == min(c.l for c in win):
            out.append((i, "L", candles[i].l))
    return out


def slope(values: List[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mx, my = (n - 1) / 2.0, sum(values) / n
    num = sum((i - mx) * (v - my) for i, v in enumerate(values))
    den = sum((i - mx) ** 2 for i in range(n))
    return num / den if den else 0.0


# --------------------------------------------------------------------------- #
# Risk state (persistent hard lockout) and position sizing
# --------------------------------------------------------------------------- #
class RiskState:
    """Two losses in a row lock trading until the next calendar day. There is no unlock method."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.data = self._load()
        self._roll()

    def _load(self) -> Dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "day" in data:
                return data
        except Exception:
            pass
        return {"day": date.today().isoformat(), "consecutive_losses": 0, "locked": False,
                "daily_pnl": 0.0, "wins": 0, "losses": 0}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def _roll(self) -> None:
        today = date.today().isoformat()
        if self.data.get("day") != today:
            self.data.update({"day": today, "locked": False, "consecutive_losses": 0, "daily_pnl": 0.0})
            self._save()

    def is_locked(self) -> bool:
        with self._lock:
            self._roll()
            return bool(self.data["locked"])

    def record(self, pnl: float) -> None:
        with self._lock:
            self._roll()
            self.data["daily_pnl"] = round(self.data["daily_pnl"] + float(pnl), 2)
            if pnl < 0:
                self.data["losses"] += 1
                self.data["consecutive_losses"] += 1
                if self.data["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
                    self.data["locked"] = True
            else:
                self.data["wins"] += 1
                self.data["consecutive_losses"] = 0
            self._save()

    def status(self) -> Dict:
        with self._lock:
            self._roll()
            return dict(self.data)


def position_size(equity: float, entry: float, sl: float, risk_pct: float = MAX_RISK_PCT,
                  max_qty: int = 100000) -> int:
    """Largest whole quantity whose stop-out loss stays within min(risk_pct, 1%) of equity."""
    risk_pct = min(float(risk_pct), MAX_RISK_PCT)
    per_unit = abs(entry - sl)
    if per_unit <= 0 or equity <= 0 or risk_pct <= 0:
        return 0
    qty = int((equity * risk_pct / 100.0) // per_unit)
    # never let one position cost more than the whole account
    qty = min(qty, int(equity // entry) if entry > 0 else 0)
    return max(0, min(qty, int(max_qty)))


# --------------------------------------------------------------------------- #
# Context shared by all agents
# --------------------------------------------------------------------------- #
class Ctx:
    pass


def build_ctx(candles: List[Candle], equity: float, risk: Optional[RiskState]) -> Ctx:
    x = Ctx()
    x.candles = candles
    x.closes = [c.c for c in candles]
    x.last = candles[-1]
    x.prev = candles[-2]
    x.price = x.last.c
    x.e9, x.e21, x.e50 = ema(x.closes, 9), ema(x.closes, 21), ema(x.closes, 50)
    x.atr = max(atr(candles), 1e-9)
    x.rsi = rsi(x.closes)
    x.sw = swings(candles[:-1])
    x.equity = equity
    x.risk = risk
    return x


def vote(name, signal, conf, detail, veto=False) -> Vote:
    return Vote(name, signal, max(0.0, min(1.0, conf)), detail, veto)


# --------------------------------------------------------------------------- #
# The 20 agents
# --------------------------------------------------------------------------- #
def a_structure(x):
    hs = [p for _, k, p in x.sw if k == "H"][-2:]
    ls = [p for _, k, p in x.sw if k == "L"][-2:]
    if len(hs) < 2 or len(ls) < 2:
        return vote("Market Structure", NEUTRAL, 0.1, "not enough swings")
    hh, hl = hs[-1] > hs[-2], ls[-1] > ls[-2]
    txt = "%s highs, %s lows" % ("higher" if hh else "lower", "higher" if hl else "lower")
    if hh and hl:
        return vote("Market Structure", BUY, 0.8, txt)
    if not hh and not hl:
        return vote("Market Structure", SELL, 0.8, txt)
    return vote("Market Structure", NEUTRAL, 0.2, txt + " (mixed)")


def a_sweep(x):
    hs = [p for i, k, p in x.sw if k == "H" and i < len(x.candles) - 4]
    ls = [p for i, k, p in x.sw if k == "L" and i < len(x.candles) - 4]
    for c in (x.last, x.prev):
        if hs and c.h > hs[-1] and c.c < hs[-1]:
            return vote("Liquidity Sweep", SELL, 0.7, "swept high %.2f, closed back below" % hs[-1])
        if ls and c.l < ls[-1] and c.c > ls[-1]:
            return vote("Liquidity Sweep", BUY, 0.7, "swept low %.2f, closed back above" % ls[-1])
    return vote("Liquidity Sweep", NEUTRAL, 0.1, "no sweep")


def a_choch(x):
    hs = [p for _, k, p in x.sw if k == "H"][-2:]
    ls = [p for _, k, p in x.sw if k == "L"][-2:]
    if len(hs) == 2 and hs[1] < hs[0] and x.price > hs[1]:
        return vote("CHoCH", BUY, 0.65, "close %.2f broke lower-high %.2f" % (x.price, hs[1]))
    if len(ls) == 2 and ls[1] > ls[0] and x.price < ls[1]:
        return vote("CHoCH", SELL, 0.65, "close %.2f broke higher-low %.2f" % (x.price, ls[1]))
    return vote("CHoCH", NEUTRAL, 0.1, "no character change")


def a_trend(x):
    e9, e21, e50 = x.e9[-1], x.e21[-1], x.e50[-1]
    spread = abs(e9 - e50) / x.atr
    txt = "EMA9 %.2f / 21 %.2f / 50 %.2f" % (e9, e21, e50)
    if e9 > e21 > e50 and x.price > e21:
        return vote("Trend EMA", BUY, 0.5 + min(0.4, spread * 0.1), txt)
    if e9 < e21 < e50 and x.price < e21:
        return vote("Trend EMA", SELL, 0.5 + min(0.4, spread * 0.1), txt)
    return vote("Trend EMA", NEUTRAL, 0.2, txt + " (tangled)")


def a_rsi(x):
    r = x.rsi
    txt = "RSI %.1f" % r
    if 52 <= r <= 72:
        return vote("Momentum RSI", BUY, 0.35 + (r - 50) / 40.0, txt)
    if 28 <= r <= 48:
        return vote("Momentum RSI", SELL, 0.35 + (50 - r) / 40.0, txt)
    if r > 72:
        return vote("Momentum RSI", NEUTRAL, 0.2, txt + " overbought")
    if r < 28:
        return vote("Momentum RSI", NEUTRAL, 0.2, txt + " oversold")
    return vote("Momentum RSI", NEUTRAL, 0.1, txt + " flat")


def a_volume(x):
    part = x.candles[-12:]
    up = sum(c.v for c in part if c.c >= c.o)
    dn = sum(c.v for c in part if c.c < c.o)
    if up + dn <= 0:
        return vote("Volume Flow", NEUTRAL, 0.0, "no volume data")
    ratio = (up + 1e-9) / (dn + 1e-9)
    txt = "up/down volume %.2f" % ratio
    if ratio > 1.25:
        return vote("Volume Flow", BUY, min(0.85, 0.4 + (ratio - 1) * 0.3), txt)
    if ratio < 0.8:
        return vote("Volume Flow", SELL, min(0.85, 0.4 + (1 / ratio - 1) * 0.3), txt)
    return vote("Volume Flow", NEUTRAL, 0.1, txt)


def a_sr(x):
    sup = [p for _, k, p in x.sw[-30:] if k == "L" and p < x.price]
    res = [p for _, k, p in x.sw[-30:] if k == "H" and p > x.price]
    ns = max(sup) if sup else None
    nr = min(res) if res else None
    d_s = (x.price - ns) / x.atr if ns is not None else 99.0
    d_r = (nr - x.price) / x.atr if nr is not None else 99.0
    txt = "support %s (%.1f ATR) / resistance %s (%.1f ATR)" % (
        "%.2f" % ns if ns else "-", d_s if d_s < 99 else 0, "%.2f" % nr if nr else "-", d_r if d_r < 99 else 0)
    if d_s < 0.8 and d_r >= 1.5:
        return vote("Support/Resistance", BUY, 0.6, txt)
    if d_r < 0.8 and d_s >= 1.5:
        return vote("Support/Resistance", SELL, 0.6, txt)
    return vote("Support/Resistance", NEUTRAL, 0.1, txt)


def a_vol_guard(x):
    trs = true_ranges(x.candles)
    recent, base = sum(trs[-3:]) / 3.0, sum(trs[-50:]) / len(trs[-50:])
    ratio = recent / base if base > 0 else 1.0
    txt = "range ratio %.2f" % ratio
    if ratio > 2.5:
        return vote("Volatility Guard", NEUTRAL, 1.0, txt + " SPIKE", veto=True)
    if ratio < 0.4:
        return vote("Volatility Guard", NEUTRAL, 1.0, txt + " dead market", veto=True)
    return vote("Volatility Guard", NEUTRAL, 0.0, txt + " calm")


def a_risk(x):
    cap = x.equity * MAX_RISK_PCT / 100.0
    st = x.risk.status() if x.risk else {"locked": False, "consecutive_losses": 0, "daily_pnl": 0.0}
    txt = "1%% cap = %.2f | losses in row %d" % (cap, st["consecutive_losses"])
    if st["locked"]:
        return vote("Risk Manager 1%", NEUTRAL, 1.0, "LOCKED today after %d losses" % MAX_CONSECUTIVE_LOSSES, veto=True)
    if st["daily_pnl"] <= -2.0 * cap:
        return vote("Risk Manager 1%", NEUTRAL, 1.0, "daily loss limit hit (%.2f)" % st["daily_pnl"], veto=True)
    return vote("Risk Manager 1%", NEUTRAL, 0.0, txt)


def a_macd(x):
    line = [a - b for a, b in zip(ema(x.closes, 12), ema(x.closes, 26))]
    sig = ema(line, 9)
    h, hp = line[-1] - sig[-1], line[-2] - sig[-2]
    txt = "hist %.3f (prev %.3f)" % (h, hp)
    if h > 0 and h >= hp:
        return vote("MACD", BUY, 0.55, txt)
    if h < 0 and h <= hp:
        return vote("MACD", SELL, 0.55, txt)
    return vote("MACD", NEUTRAL, 0.15, txt + " fading")


def a_bollinger(x):
    part = x.closes[-20:]
    m = sum(part) / len(part)
    sd = math.sqrt(sum((v - m) ** 2 for v in part) / len(part)) or 1e-9
    pb = (x.price - (m - 2 * sd)) / (4 * sd)
    txt = "%%B %.2f" % pb
    if 0.6 <= pb <= 1.0:
        return vote("Bollinger", BUY, 0.5, txt + " riding upper band")
    if 0.0 <= pb <= 0.4:
        return vote("Bollinger", SELL, 0.5, txt + " riding lower band")
    return vote("Bollinger", NEUTRAL, 0.1, txt)


def a_vwap(x):
    part = x.candles[-60:]
    pv = sum(((c.h + c.l + c.c) / 3.0) * c.v for c in part)
    vv = sum(c.v for c in part)
    vwap = pv / vv if vv > 0 else sum(c.c for c in part) / len(part)
    txt = "VWAP %.2f, price %.2f" % (vwap, x.price)
    if x.price > vwap + 0.1 * x.atr:
        return vote("VWAP", BUY, 0.5, txt)
    if x.price < vwap - 0.1 * x.atr:
        return vote("VWAP", SELL, 0.5, txt)
    return vote("VWAP", NEUTRAL, 0.1, txt)


def a_stoch(x):
    ks = []
    for i in range(len(x.candles) - 5, len(x.candles)):
        win = x.candles[i - 13:i + 1]
        hi, lo = max(c.h for c in win), min(c.l for c in win)
        ks.append(100.0 * (x.candles[i].c - lo) / (hi - lo) if hi > lo else 50.0)
    k, d = ks[-1], sum(ks[-3:]) / 3.0
    txt = "%%K %.0f / %%D %.0f" % (k, d)
    if k > d and 20 < k < 80:
        return vote("Stochastic", BUY, 0.45, txt)
    if k < d and 20 < k < 80:
        return vote("Stochastic", SELL, 0.45, txt)
    return vote("Stochastic", NEUTRAL, 0.1, txt)


def a_adx(x):
    a, p, m = adx(x.candles)
    txt = "ADX %.1f (+DI %.1f / -DI %.1f)" % (a, p, m)
    if a >= 20 and p > m:
        return vote("ADX Strength", BUY, min(0.9, a / 50.0 + 0.2), txt)
    if a >= 20 and m > p:
        return vote("ADX Strength", SELL, min(0.9, a / 50.0 + 0.2), txt)
    return vote("ADX Strength", NEUTRAL, 0.1, txt + " weak trend")


def a_pattern(x):
    c, p = x.last, x.prev
    body, rng = abs(c.c - c.o), max(c.h - c.l, 1e-9)
    lower, upper = min(c.o, c.c) - c.l, c.h - max(c.o, c.c)
    if c.c > c.o and p.c < p.o and c.c >= p.o and c.o <= p.c:
        return vote("Candle Pattern", BUY, 0.6, "bullish engulfing")
    if c.c < c.o and p.c > p.o and c.c <= p.o and c.o >= p.c:
        return vote("Candle Pattern", SELL, 0.6, "bearish engulfing")
    if lower > 2 * body and upper < body + 0.1 * rng and x.e21[-1] >= x.e50[-1]:
        return vote("Candle Pattern", BUY, 0.5, "hammer")
    if upper > 2 * body and lower < body + 0.1 * rng and x.e21[-1] <= x.e50[-1]:
        return vote("Candle Pattern", SELL, 0.5, "shooting star")
    return vote("Candle Pattern", NEUTRAL, 0.1, "no pattern")


def a_breakout(x):
    prior = x.candles[-21:-1]
    hi, lo = max(c.h for c in prior), min(c.l for c in prior)
    txt = "20-bar range %.2f - %.2f" % (lo, hi)
    if x.price > hi:
        return vote("Breakout", BUY, 0.65, txt + " broke up")
    if x.price < lo:
        return vote("Breakout", SELL, 0.65, txt + " broke down")
    return vote("Breakout", NEUTRAL, 0.1, txt + " inside")


def a_pullback(x):
    e21, e50 = x.e21[-1], x.e50[-1]
    c = x.last
    if e21 > e50 and abs(c.l - e21) <= 0.3 * x.atr and c.c > e21:
        return vote("Pullback", BUY, 0.6, "tagged EMA21 %.2f and held" % e21)
    if e21 < e50 and abs(c.h - e21) <= 0.3 * x.atr and c.c < e21:
        return vote("Pullback", SELL, 0.6, "rejected at EMA21 %.2f" % e21)
    return vote("Pullback", NEUTRAL, 0.1, "no pullback to EMA21")


def a_range(x):
    part = x.candles[-20:]
    hi, lo = max(c.h for c in part), min(c.l for c in part)
    rng = hi - lo
    txt = "20-bar range %.1f ATR" % (rng / x.atr)
    if rng < 3.0 * x.atr:
        return vote("Range Filter", NEUTRAL, 0.1, txt + " choppy")
    pos = (x.price - lo) / rng
    if pos > 0.66:
        return vote("Range Filter", BUY, 0.45, txt + ", price in top third")
    if pos < 0.34:
        return vote("Range Filter", SELL, 0.45, txt + ", price in bottom third")
    return vote("Range Filter", NEUTRAL, 0.1, txt + ", mid-range")


def a_slope(x):
    s = slope(x.closes[-20:]) * 20.0 / x.atr
    txt = "slope %.2f ATR / 20 bars" % s
    if s > 1.0:
        return vote("Momentum Slope", BUY, min(0.85, 0.3 + s * 0.15), txt)
    if s < -1.0:
        return vote("Momentum Slope", SELL, min(0.85, 0.3 + -s * 0.15), txt)
    return vote("Momentum Slope", NEUTRAL, 0.1, txt)


def a_trap(x):
    prior = x.candles[-21:-1]
    hi, lo = max(c.h for c in prior), min(c.l for c in prior)
    avg_v = sum(c.v for c in prior) / len(prior)
    c = x.last
    rng = max(c.h - c.l, 1e-9)
    broke = c.c > hi or c.c < lo
    if broke and avg_v > 0 and c.v < 0.8 * avg_v:
        return vote("Fakeout Shield", NEUTRAL, 1.0, "breakout on weak volume (%.0f%% of avg)" % (100 * c.v / avg_v), veto=True)
    if broke and (c.h - max(c.o, c.c) > 0.6 * rng or min(c.o, c.c) - c.l > 0.6 * rng):
        return vote("Fakeout Shield", NEUTRAL, 1.0, "breakout candle has a huge wick", veto=True)
    return vote("Fakeout Shield", NEUTRAL, 0.0, "no trap detected")


AGENTS = [a_structure, a_sweep, a_choch, a_trend, a_rsi, a_volume, a_sr, a_vol_guard, a_risk, a_macd,
          a_bollinger, a_vwap, a_stoch, a_adx, a_pattern, a_breakout, a_pullback, a_range, a_slope, a_trap]

AGENT_NAMES = ["Market Structure", "Liquidity Sweep", "CHoCH", "Trend EMA", "Momentum RSI", "Volume Flow",
               "Support/Resistance", "Volatility Guard", "Risk Manager 1%", "MACD", "Bollinger", "VWAP",
               "Stochastic", "ADX Strength", "Candle Pattern", "Breakout", "Pullback", "Range Filter",
               "Momentum Slope", "Fakeout Shield"]


class ConsensusEngine:
    def __init__(self, risk: Optional[RiskState] = None, min_agree: int = MIN_AGREE, min_rr: float = MIN_RR):
        self.risk = risk
        self.min_agree = max(8, int(min_agree))
        self.min_rr = max(MIN_RR, float(min_rr))
        self.agent_names = list(AGENT_NAMES)

    def evaluate(self, symbol: str, candles: List[Candle], equity: float) -> Decision:
        d = Decision(symbol=symbol, total=len(AGENTS))
        if len(candles) < MIN_CANDLES:
            d.reason = "Need %d candles, have %d" % (MIN_CANDLES, len(candles))
            return d
        x = build_ctx(candles, equity, self.risk)
        for i, fn in enumerate(AGENTS):
            try:
                d.votes.append(fn(x))
            except Exception as exc:  # one broken agent must never stop the others
                d.votes.append(Vote(AGENT_NAMES[i], NEUTRAL, 0.0, "error: %s" % exc))
        buys = sum(1 for v in d.votes if v.signal == BUY)
        sells = sum(1 for v in d.votes if v.signal == SELL)
        d.direction = BUY if buys > sells else SELL if sells > buys else NEUTRAL
        d.agree, d.against = (buys, sells) if d.direction == BUY else (sells, buys)
        vetoes = [v for v in d.votes if v.veto]
        d.entry = x.price
        if d.direction == NEUTRAL:
            d.reason = "Agents are split %d-%d. No edge." % (buys, sells)
            return d
        risk_dist = 1.5 * x.atr
        reward_dist = max(self.min_rr, 2.0) * risk_dist
        d.sl = d.entry - risk_dist if d.direction == BUY else d.entry + risk_dist
        d.tp = d.entry + reward_dist if d.direction == BUY else d.entry - reward_dist
        d.rr = reward_dist / risk_dist
        d.qty = position_size(equity, d.entry, d.sl)
        d.risk_amount = round(d.qty * abs(d.entry - d.sl), 2)
        if vetoes:
            d.reason = "VETO by %s: %s" % (vetoes[0].agent, vetoes[0].detail)
        elif d.agree < self.min_agree:
            d.reason = "Only %d of %d agents agree (need %d)." % (d.agree, d.total, self.min_agree)
        elif d.agree < 2 * d.against:
            d.reason = "Opposition too strong (%d for, %d against)." % (d.agree, d.against)
        elif d.qty < 1:
            d.reason = "Capital too small for a 1%% risk on this setup."
        elif d.rr < self.min_rr:
            d.reason = "Reward:risk %.1f is below %.1f." % (d.rr, self.min_rr)
        else:
            d.approved = True
            d.reason = "%d of %d agents agree %s. Risk %.2f (<= 1%% of equity)." % (
                d.agree, d.total, d.direction, d.risk_amount)
        return d


# --------------------------------------------------------------------------- #
# JARVIS mentor: rule-based conversation (no internet, no external AI)
# --------------------------------------------------------------------------- #
class Mentor:
    """Answers the trader in short, calm sentences. Advice only, never orders."""

    GLOSSARY = {
        "stop loss": "A stop loss is the price where you accept you were wrong and exit. It caps the loss before you enter.",
        "take profit": "Take profit is the pre-planned exit at your target. Plan it before entering so greed cannot decide.",
        "risk reward": "Risk to reward compares what you can lose with what you can win. I only take trades that pay at least 2 for every 1 risked.",
        "rsi": "RSI measures how strong recent moves were, from 0 to 100. Above 70 is stretched, below 30 is washed out.",
        "ema": "An EMA is a moving average that reacts faster to new prices. When short EMAs sit above long ones, the trend is up.",
        "choch": "CHoCH means change of character: price breaks the last swing against the old trend, an early hint of reversal.",
        "liquidity": "A liquidity sweep is a quick poke past an old high or low that grabs stop orders, then price snaps back.",
        "paper": "Paper trading uses virtual money with real prices. Prove the method here for weeks before risking real money.",
        "compound": "Compounding means letting small gains add to your base. Drop by drop, a steady 0.5 percent a day beats one lucky jackpot.",
    }

    def reply(self, text: str, snap: Dict) -> str:
        t = " ".join(str(text).lower().replace("?", " ").replace(",", " ").split())
        d = snap.get("decision")
        eq = snap.get("equity", 0.0)
        for key, answer in self.GLOSSARY.items():
            if key in t or (key == "risk reward" and ("rr" in t.split() or "reward" in t)):
                return answer
        if any(w in t for w in ("hello", "hi", "hey", "namaste", "jarvis")) and len(t.split()) <= 3:
            return "At your service, boss. Ask me about the market, risk, or say scan, trade, plan, status."
        if "why" in t and ("no trade" in t or "wait" in t or "not" in t or "skip" in t):
            return d.reason if d else "I have not scanned yet. Say scan."
        if any(w in t for w in ("signal", "market", "view", "scan", "setup", "analy")):
            if not d:
                return "No scan yet. Give me a moment, boss."
            if d.approved:
                return "%s on %s. Entry %.2f, stop %.2f, target %.2f. %s" % (d.direction, d.symbol, d.entry, d.sl, d.tp, d.reason)
            return "No trade on %s right now. %s Patience is a position too." % (d.symbol, d.reason)
        if any(w in t for w in ("risk", "size", "quantity", "how much")):
            return "I never risk more than 1 percent of equity per trade. On %.2f that is %.2f rupees." % (eq, eq * MAX_RISK_PCT / 100.0)
        if any(w in t for w in ("plan", "mentor", "advice", "target", "goal")):
            return ("Plan: aim for about 0.5 percent a day, take at most 3 trades, stop after 2 losses in a row. "
                    "Bank some profit, compound the rest. Small drops fill the bucket.")
        if any(w in t for w in ("status", "balance", "account", "pnl", "profit")):
            return "Paper equity %.2f. Open positions %d. Today's result %.2f. Losses in a row %d." % (
                eq, snap.get("open", 0), snap.get("daily_pnl", 0.0), snap.get("losses_in_row", 0))
        if any(w in t for w in ("scared", "fear", "greed", "revenge", "angry", "loss", "lost")):
            return "Breathe, boss. A loss is the fee for doing business. Do not chase it. Follow the plan and stop at two losses."
        if any(w in t for w in ("guarantee", "sure shot", "100", "double", "jackpot")):
            return "No one can guarantee profit, boss. I protect your capital first. Growth comes from many small, disciplined wins."
        if "help" in t or "what can" in t:
            return "Say: scan, trade, kill, rearm, plan, status, watch followed by a symbol, or ask what is RSI, stop loss, CHoCH."
        return "I did not get that, boss. Try scan, plan, status, or ask why there is no trade."
