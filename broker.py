# -*- coding: utf-8 -*-
"""broker.py - universal market-data connector and 1%-risk paper trading handler.

Pure Python (standard library only).
  * Feeds: DemoFeed (always works, simulated 1-minute candles) and YahooFeed (public chart data,
    delayed, needs internet). New data sources can be added with register_feed().
  * MarketStream: background thread that keeps the newest 1-minute candles ready for the app.
  * PaperBroker: virtual-money orders. No real order is ever sent from this file.
"""
import json
import os
import random
import threading
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

from agents import BUY, SELL, MAX_RISK_PCT, Candle, position_size

DEMO_BASE = {"NIFTY": 24500.0, "BANKNIFTY": 52000.0, "RELIANCE": 1450.0, "TCS": 3900.0,
             "INFY": 1650.0, "HDFCBANK": 1700.0, "SBIN": 820.0}
HISTORY = 240
MAX_CANDLES = 400


class BrokerError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Data feeds
# --------------------------------------------------------------------------- #
class DemoFeed:
    """Simulated live feed: random walk with trending and ranging regimes."""
    label = "DEMO"
    poll_seconds = 1.0

    def __init__(self):
        self._st: Dict[str, Dict] = {}

    def _state(self, symbol: str) -> Dict:
        if symbol not in self._st:
            seed = sum(ord(ch) * (i + 1) for i, ch in enumerate(symbol))
            rnd = random.Random(seed + int(time.time() // 3600))
            base = DEMO_BASE.get(symbol, 100.0 + (seed % 900))
            self._st[symbol] = {"rnd": rnd, "px": base, "vol": base * 0.0006, "drift": 0.0, "left": 0}
        return self._st[symbol]

    def _step(self, st: Dict) -> float:
        rnd = st["rnd"]
        if st["left"] <= 0:
            st["left"] = rnd.randint(25, 90)
            st["drift"] = rnd.choice([-1.0, 0.0, 1.0, 1.0, -1.0]) * st["vol"] * 0.35
        st["left"] -= 1
        st["px"] = max(1.0, st["px"] + st["drift"] + rnd.gauss(0.0, st["vol"]))
        return st["px"]

    def bootstrap(self, symbol: str) -> List[Candle]:
        st = self._state(symbol)
        now_min = int(time.time() // 60) * 60
        out: List[Candle] = []
        for i in range(HISTORY, 0, -1):
            o = st["px"]
            hi = lo = o
            for _ in range(6):
                p = self._step(st)
                hi, lo = max(hi, p), min(lo, p)
            vol = abs(st["rnd"].gauss(1000, 300)) + 100 + 400 * abs(st["px"] - o) / (st["vol"] + 1e-9)
            out.append(Candle(now_min - i * 60, o, hi, lo, st["px"], round(vol)))
        return out

    def poll(self, symbol: str, candles: List[Candle]) -> None:
        st = self._state(symbol)
        p = self._step(st)
        now_min = int(time.time() // 60) * 60
        last = candles[-1]
        vol = abs(st["rnd"].gauss(40, 15)) + 5
        if now_min > last.t:
            candles.append(Candle(now_min, last.c, max(last.c, p), min(last.c, p), p, round(vol)))
        else:
            last.h, last.l, last.c = max(last.h, p), min(last.l, p), p
            last.v += round(vol)
        del candles[:-MAX_CANDLES]


class YahooFeed:
    """Public Yahoo Finance chart data (delayed, unofficial). Raises BrokerError when unreachable."""
    label = "YAHOO"
    poll_seconds = 15.0
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1m&range=1d"
    MAP = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}

    def _fetch(self, symbol: str) -> List[Candle]:
        ysym = self.MAP.get(symbol, symbol if (symbol.startswith("^") or "." in symbol) else symbol + ".NS")
        req = urllib.request.Request(self.URL % ysym, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                payload = json.loads(r.read().decode("utf-8"))
            res = payload["chart"]["result"][0]
            ts = res["timestamp"]
            q = res["indicators"]["quote"][0]
        except Exception as exc:
            raise BrokerError("Yahoo feed failed: %s" % exc)
        out = []
        for i, t in enumerate(ts):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if None in (o, h, l, c):
                continue
            out.append(Candle(float(t), float(o), float(h), float(l), float(c), float(q["volume"][i] or 0)))
        if len(out) < 30:
            raise BrokerError("Yahoo returned too few candles (%d). Market closed?" % len(out))
        return out[-MAX_CANDLES:]

    def bootstrap(self, symbol: str) -> List[Candle]:
        return self._fetch(symbol)

    def poll(self, symbol: str, candles: List[Candle]) -> None:
        candles[:] = self._fetch(symbol)


FEEDS = {"DEMO": DemoFeed, "YAHOO": YahooFeed}


def register_feed(name: str, cls: type) -> None:
    """Add another data source. The class needs bootstrap(symbol), poll(symbol, candles), label, poll_seconds."""
    for m in ("bootstrap", "poll"):
        if not callable(getattr(cls, m, None)):
            raise BrokerError("Feed class %s lacks %s()" % (cls.__name__, m))
    FEEDS[str(name).upper()] = cls


class MarketStream:
    """Keeps candles of one symbol fresh in a background thread. Falls back to DEMO if a feed keeps failing."""

    def __init__(self, symbol: str = "NIFTY", source: str = "DEMO"):
        self._lock = threading.RLock()
        self.symbol = symbol.upper()
        self._candles: List[Candle] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.feed = None
        self.source = "DEMO"
        self.note = ""
        self._fails = 0
        self._use(source)

    def _use(self, source: str) -> None:
        cls = FEEDS.get(str(source).upper(), DemoFeed)
        feed = cls()
        note = ""
        try:
            candles = feed.bootstrap(self.symbol)
        except BrokerError as exc:
            feed = DemoFeed()
            candles = feed.bootstrap(self.symbol)
            note = "%s - using DEMO data" % exc
        with self._lock:
            self.feed, self._candles, self._fails = feed, candles, 0
            self.source = feed.label
            self.note = note

    def set_symbol(self, symbol: str) -> None:
        self.symbol = symbol.upper()
        self._use(self.source)

    def set_source(self, source: str) -> None:
        self._use(source)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        next_poll = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now >= next_poll:
                try:
                    with self._lock:
                        feed, sym = self.feed, self.symbol
                        work = list(self._candles)
                    feed.poll(sym, work)
                    with self._lock:
                        if sym == self.symbol and feed is self.feed:
                            self._candles = work
                            self._fails = 0
                except Exception as exc:
                    self._fails += 1
                    self.note = str(exc)[:120]
                    if self._fails >= 3 and not isinstance(self.feed, DemoFeed):
                        self._use("DEMO")
                        self.note = "Live feed failed 3 times. Switched to DEMO."
                next_poll = now + max(0.5, getattr(self.feed, "poll_seconds", 1.0))
            self._stop.wait(0.25)

    def snapshot(self) -> Tuple[List[Candle], float, str, str]:
        with self._lock:
            copy = [Candle(c.t, c.o, c.h, c.l, c.c, c.v) for c in self._candles]
            price = copy[-1].c if copy else 0.0
            return copy, price, self.source, self.note


# --------------------------------------------------------------------------- #
# Paper trading with strict 1% risk
# --------------------------------------------------------------------------- #
class PaperBroker:
    """Virtual-money execution. Refuses any order whose stop-out loss exceeds 1% of equity."""

    def __init__(self, path: str, start_capital: float = 10000.0):
        self.path = path
        self._lock = threading.RLock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.state = self._load(start_capital)

    def _load(self, start_capital: float) -> Dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                s = json.load(fh)
            if isinstance(s, dict) and "cash" in s:
                return s
        except Exception:
            pass
        return {"start": float(start_capital), "cash": float(start_capital), "positions": [], "trades": [], "seq": 0}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # -- accounting --
    def positions(self) -> List[Dict]:
        with self._lock:
            return [dict(p) for p in self.state["positions"]]

    def trades(self, n: int = 20) -> List[Dict]:
        with self._lock:
            return [dict(t) for t in self.state["trades"][-n:]]

    def equity(self, price_of: Optional[Dict[str, float]] = None) -> float:
        with self._lock:
            eq = self.state["cash"]
            for p in self.state["positions"]:
                px = (price_of or {}).get(p["symbol"], p["entry"])
                eq += (px - p["entry"]) * p["qty"] * (1 if p["side"] == BUY else -1)
            return round(eq, 2)

    # -- orders --
    def open_trade(self, symbol: str, side: str, qty: int, entry: float, sl: float, tp: float) -> Dict:
        side = side.upper()
        if side not in (BUY, SELL):
            raise BrokerError("Direction must be BUY or SELL")
        if qty < 1:
            raise BrokerError("Quantity must be at least 1")
        if side == BUY and not (sl < entry < tp):
            raise BrokerError("For BUY we need SL < entry < TP")
        if side == SELL and not (tp < entry < sl):
            raise BrokerError("For SELL we need TP < entry < SL")
        with self._lock:
            if self.state["positions"]:
                raise BrokerError("One open position at a time (keeps total risk at 1%)")
            equity = self.equity()
            risk = qty * abs(entry - sl)
            cap = equity * MAX_RISK_PCT / 100.0
            if risk > cap * 1.0001:
                raise BrokerError("Refused: risk %.2f is above the 1%% cap %.2f" % (risk, cap))
            if qty * entry > equity:
                raise BrokerError("Refused: position value is larger than the account")
            self.state["seq"] += 1
            pos = {"id": "P%d" % self.state["seq"], "symbol": symbol, "side": side, "qty": int(qty),
                   "entry": round(entry, 2), "sl": round(sl, 2), "tp": round(tp, 2),
                   "risk": round(risk, 2), "opened_ts": time.time()}
            self.state["positions"].append(pos)
            self._save()
            return dict(pos)

    def _close(self, pos: Dict, price: float, reason: str) -> Dict:
        sign = 1 if pos["side"] == BUY else -1
        pnl = round((price - pos["entry"]) * pos["qty"] * sign, 2)
        trade = dict(pos, exit=round(price, 2), pnl=pnl, reason=reason, closed_ts=time.time())
        self.state["positions"] = [p for p in self.state["positions"] if p["id"] != pos["id"]]
        self.state["cash"] = round(self.state["cash"] + pnl, 2)
        self.state["trades"] = (self.state["trades"] + [trade])[-300:]
        self._save()
        return trade

    def check_exits(self, symbol: str, candles: List[Candle]) -> List[Dict]:
        """Close positions whose SL/TP was touched. If both were touched, the stop is assumed hit first."""
        closed: List[Dict] = []
        if not candles:
            return closed
        with self._lock:
            for pos in list(self.state["positions"]):
                if pos["symbol"] != symbol:
                    continue
                price = candles[-1].c
                hi = lo = price
                for c in candles:
                    if c.t > pos["opened_ts"]:
                        hi, lo = max(hi, c.h), min(lo, c.l)
                if pos["side"] == BUY:
                    if lo <= pos["sl"]:
                        closed.append(self._close(pos, pos["sl"], "STOP LOSS"))
                    elif hi >= pos["tp"]:
                        closed.append(self._close(pos, pos["tp"], "TARGET"))
                else:
                    if hi >= pos["sl"]:
                        closed.append(self._close(pos, pos["sl"], "STOP LOSS"))
                    elif lo <= pos["tp"]:
                        closed.append(self._close(pos, pos["tp"], "TARGET"))
        return closed

    def close_all(self, price_of: Dict[str, float]) -> List[Dict]:
        with self._lock:
            return [self._close(p, price_of.get(p["symbol"], p["entry"]), "KILL SWITCH")
                    for p in list(self.state["positions"])]

    def summary(self, price_of: Optional[Dict[str, float]] = None) -> Dict:
        with self._lock:
            wins = sum(1 for t in self.state["trades"] if t["pnl"] > 0)
            return {"mode": "PAPER", "equity": self.equity(price_of),
                    "realised": round(self.state["cash"] - self.state["start"], 2),
                    "open": len(self.state["positions"]), "trades": len(self.state["trades"]), "wins": wins}

    def plan_for(self, entry: float, sl: float, price_of: Optional[Dict[str, float]] = None) -> int:
        return position_size(self.equity(price_of), entry, sl)
