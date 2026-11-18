# -*- coding: utf-8 -*-
"""risk_manager.py - hard risk limits and the backend kill switch for Tactical Neural Matrix.

Pure Python (standard library only). Every real order must pass through this module:

  * 1% rule        : a stop-out may never lose more than 1% of account equity (hard cap, not configurable upward).
  * Kill switch    : kill() locks all trading in the BACKEND and the lock is saved to disk, so it survives an app
                     restart. Only an explicit rearm() call clears it.
  * Loss lockout   : 2 losing trades in a row lock trading until the next calendar day. No unlock method exists.
  * Daily limits   : daily loss limit (2% of equity) and a maximum number of trades per day.
"""
import json
import os
import threading
import time
from datetime import date
from typing import Dict, Optional, Tuple

MAX_RISK_PCT = 1.0               # hard cap per trade, % of equity
MAX_CONSECUTIVE_LOSSES = 2       # lock for the rest of the day
DAILY_LOSS_LIMIT_PCT = 2.0       # % of equity
MAX_TRADES_PER_DAY = 4
RISK_TOLERANCE = 1.0001          # rounding tolerance only


class RiskError(Exception):
    """Raised when an action would break a hard risk rule."""


class RiskManager:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.data = self._load()
        self._roll_day()

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fresh() -> Dict:
        return {"day": date.today().isoformat(), "killed": False, "kill_reason": "", "kill_ts": 0.0,
                "consecutive_losses": 0, "locked": False, "daily_pnl": 0.0, "trades_today": 0,
                "wins": 0, "losses": 0}

    def _load(self) -> Dict:
        fresh = self._fresh()
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                fresh.update({k: data[k] for k in fresh if k in data})
        except Exception:
            pass
        return fresh

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def _roll_day(self) -> None:
        """New calendar day: clears the loss lockout and daily counters. The kill switch stays as it is."""
        today = date.today().isoformat()
        if self.data.get("day") != today:
            self.data.update({"day": today, "locked": False, "consecutive_losses": 0,
                              "daily_pnl": 0.0, "trades_today": 0})
            self._save()

    # ------------------------------------------------------------------ #
    # kill switch
    # ------------------------------------------------------------------ #
    def kill(self, reason: str = "manual kill switch") -> None:
        with self._lock:
            self.data["killed"] = True
            self.data["kill_reason"] = str(reason)[:120]
            self.data["kill_ts"] = time.time()
            self._save()

    def rearm(self) -> None:
        """The only way to clear the kill switch. The loss lockout is NOT cleared by this."""
        with self._lock:
            self.data["killed"] = False
            self.data["kill_reason"] = ""
            self._save()

    def is_killed(self) -> bool:
        with self._lock:
            return bool(self.data["killed"])

    def is_locked(self) -> bool:
        with self._lock:
            self._roll_day()
            return bool(self.data["locked"])

    # ------------------------------------------------------------------ #
    # gate for every order
    # ------------------------------------------------------------------ #
    def can_trade(self, equity: float) -> Tuple[bool, str]:
        with self._lock:
            self._roll_day()
            d = self.data
            if d["killed"]:
                return False, "Kill switch is ON (%s). Re-arm first." % (d["kill_reason"] or "manual")
            if d["locked"]:
                return False, "Locked for today after %d losses in a row." % MAX_CONSECUTIVE_LOSSES
            if equity > 0 and d["daily_pnl"] <= -equity * DAILY_LOSS_LIMIT_PCT / 100.0:
                return False, "Daily loss limit of %.0f%% reached." % DAILY_LOSS_LIMIT_PCT
            if d["trades_today"] >= MAX_TRADES_PER_DAY:
                return False, "Maximum %d trades per day reached." % MAX_TRADES_PER_DAY
            return True, "ok"

    def assert_can_trade(self, equity: float) -> None:
        ok, why = self.can_trade(equity)
        if not ok:
            raise RiskError(why)

    @staticmethod
    def max_loss(equity: float) -> float:
        return max(0.0, float(equity)) * MAX_RISK_PCT / 100.0

    @staticmethod
    def position_size(equity: float, entry: float, sl: float, cash: Optional[float] = None,
                      max_qty: int = 100000) -> int:
        """Largest whole quantity whose stop-out loss is within 1% of equity and whose value fits the cash."""
        per_unit = abs(entry - sl)
        if per_unit <= 0 or equity <= 0 or entry <= 0:
            return 0
        qty = int((equity * MAX_RISK_PCT / 100.0) // per_unit)
        budget = equity if cash is None else min(equity, cash)
        qty = min(qty, int(budget // entry))
        return max(0, min(qty, int(max_qty)))

    def validate_order(self, equity: float, qty: int, entry: float, sl: float) -> float:
        """Raises RiskError if the order breaks the 1% rule. Returns the money at risk."""
        if qty < 1:
            raise RiskError("Quantity must be at least 1.")
        risk = qty * abs(entry - sl)
        cap = self.max_loss(equity)
        if risk > cap * RISK_TOLERANCE:
            raise RiskError("Risk %.2f is above the 1%% cap of %.2f." % (risk, cap))
        return round(risk, 2)

    # ------------------------------------------------------------------ #
    # results
    # ------------------------------------------------------------------ #
    def note_trade_opened(self) -> None:
        with self._lock:
            self._roll_day()
            self.data["trades_today"] += 1
            self._save()

    def record_result(self, pnl: float) -> None:
        with self._lock:
            self._roll_day()
            d = self.data
            d["daily_pnl"] = round(d["daily_pnl"] + float(pnl), 2)
            if pnl < 0:
                d["losses"] += 1
                d["consecutive_losses"] += 1
                if d["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
                    d["locked"] = True
            else:
                d["wins"] += 1
                d["consecutive_losses"] = 0
            self._save()

    def status(self) -> Dict:
        with self._lock:
            self._roll_day()
            return dict(self.data)
