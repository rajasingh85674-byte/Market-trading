# -*- coding: utf-8 -*-
"""broker.py - universal live broker connector for NSE equities (Zerodha Kite Connect and Upstox).

REAL MONEY. Orders sent from this file are real orders on your broker account. There is no paper mode.
Pure Python (standard library only; certifi is used for HTTPS certificates when it is installed).

Order flow for every trade (see BaseLiveBroker.open_trade):
  1. RiskManager gate: kill switch, loss lockout, daily limits, 1% sizing.
  2. Real broker price check: the signal price must be within 0.5% of the broker's own last price.
  3. MARKET entry (intraday product), wait for the fill.
  4. Protective SL-M order placed on the exchange side immediately. If that fails, the position is flattened.
  5. Target and end-of-day exit (15:15 IST) are watched by the app in check_exits().

Market data comes from the broker when your plan includes it, otherwise from Yahoo (delayed) or from a
simulated DEMO feed. The DEMO feed is for looking at the screen only: live orders are refused on it.
"""
import csv
import gzip
import hashlib
import io
import json
import os
import random
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from agents import BUY, SELL, Candle
from risk_manager import RiskError, RiskManager

IST = timezone(timedelta(hours=5, minutes=30))
EOD_EXIT = (15, 15)              # open intraday positions are closed at 15:15 IST
MAX_LTP_DEVIATION = 0.005        # signal price vs broker price, 0.5%
TICK = 0.05
HISTORY = 240
MAX_CANDLES = 400

KITE_ROOT = "https://api.kite.trade"
KITE_LOGIN = "https://kite.zerodha.com/connect/login"
UPSTOX_ROOT = "https://api.upstox.com/v2"
UPSTOX_INSTRUMENTS = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

# ISIN based Upstox keys for common NSE stocks. Every key is double-checked against the broker's own
# quote response before any order, so a wrong entry here can only cause a refusal, never a wrong order.
UPSTOX_KEYS = {
    "RELIANCE": "NSE_EQ|INE002A01018", "TCS": "NSE_EQ|INE467B01029", "INFY": "NSE_EQ|INE009A01021",
    "HDFCBANK": "NSE_EQ|INE040A01034", "SBIN": "NSE_EQ|INE062A01020", "ICICIBANK": "NSE_EQ|INE090A01021",
    "ITC": "NSE_EQ|INE154A01025", "LT": "NSE_EQ|INE018A01030", "AXISBANK": "NSE_EQ|INE238A01034",
    "KOTAKBANK": "NSE_EQ|INE237A01028", "BHARTIARTL": "NSE_EQ|INE397D01024", "WIPRO": "NSE_EQ|INE075A01022",
    "MARUTI": "NSE_EQ|INE585B01010",
}
DEMO_BASE = {"RELIANCE": 1450.0, "TCS": 3900.0, "INFY": 1650.0, "HDFCBANK": 1700.0, "SBIN": 820.0,
             "ICICIBANK": 1300.0, "ITC": 420.0, "LT": 3600.0, "AXISBANK": 1150.0, "KOTAKBANK": 2100.0}


class BrokerError(Exception):
    pass


def ist_now() -> datetime:
    return datetime.now(IST)


def round_tick(x: float) -> float:
    return round(round(x / TICK) * TICK, 2)


def opposite(side: str) -> str:
    return SELL if side == BUY else BUY


# --------------------------------------------------------------------------- #
# HTTP (standard library only)
# --------------------------------------------------------------------------- #
_CTX: Optional[ssl.SSLContext] = None


def _ssl_context() -> ssl.SSLContext:
    global _CTX
    if _CTX is None:
        try:
            import certifi
            _CTX = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _CTX = ssl.create_default_context()
    return _CTX


def http(method: str, url: str, headers: Optional[Dict[str, str]] = None, params: Optional[Dict] = None,
         form: Optional[Dict] = None, body: Optional[Dict] = None, timeout: int = 15) -> Tuple[int, str]:
    """Returns (status code, text). Raises BrokerError on network failure."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    hdrs = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    hdrs.setdefault("User-Agent", "Mozilla/5.0 TDN")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            raw = resp.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        return exc.code, text
    except Exception as exc:
        raise BrokerError("Network error: %s" % str(exc)[:100])


# --------------------------------------------------------------------------- #
# Credentials (private app storage, file mode 600)
# --------------------------------------------------------------------------- #
class CredentialStore:
    """Broker keys and daily access tokens. Stored only inside the app's private folder.
    The file is not encrypted: do not install this app on a phone you do not control."""

    FIELDS = ("broker", "kite_key", "kite_secret", "kite_token", "kite_day",
              "upstox_key", "upstox_secret", "upstox_redirect", "upstox_token", "upstox_day")

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.data = {k: "" for k in self.FIELDS}
        self.data["broker"] = "UPSTOX"
        self.data["upstox_redirect"] = "https://127.0.0.1/"
        try:
            with open(path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                for k in self.FIELDS:
                    if k in saved and isinstance(saved[k], str):
                        self.data[k] = saved[k]
        except Exception:
            pass

    def get(self, key: str) -> str:
        with self._lock:
            return self.data.get(key, "")

    def update(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                if k in self.FIELDS:
                    self.data[k] = str(v).strip()
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self.data, fh)
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
                os.replace(tmp, self.path)
            except OSError:
                pass


def _today() -> str:
    return ist_now().date().isoformat()


def extract_param(text: str, name: str) -> str:
    """Accepts a bare token or a whole redirect URL and returns the value of `name`."""
    text = (text or "").strip()
    if "=" in text:
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
            if name in q and q[name]:
                return q[name][0].strip()
            q = urllib.parse.parse_qs(text.split("?", 1)[-1])
            if name in q and q[name]:
                return q[name][0].strip()
        except Exception:
            pass
    return text


# --------------------------------------------------------------------------- #
# Live broker base class
# --------------------------------------------------------------------------- #
class BaseLiveBroker:
    name = "BASE"
    product_label = ""

    def __init__(self, store: CredentialStore, data_dir: str):
        self.store = store
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._lock = threading.RLock()
        self.last_error = ""
        self._state_path = os.path.join(data_dir, "positions_%s.json" % self.name.lower())
        self.state = self._load_state()

    # -- to be provided by each broker --
    def has_session(self) -> bool:
        raise NotImplementedError

    def login_url(self) -> str:
        raise NotImplementedError

    def login(self, token_or_url: str) -> str:
        raise NotImplementedError

    def clear_session(self) -> None:
        raise NotImplementedError

    def funds(self) -> Dict[str, float]:
        """{'equity': total account value, 'cash': money available for new positions}"""
        raise NotImplementedError

    def ltp(self, symbol: str) -> float:
        raise NotImplementedError

    def candles(self, symbol: str, count: int = HISTORY) -> List[Candle]:
        raise NotImplementedError

    def _place(self, symbol: str, side: str, qty: int, order_type: str, trigger: Optional[float]) -> str:
        raise NotImplementedError

    def _order_status(self, order_id: str) -> Dict:
        raise NotImplementedError

    def _cancel(self, order_id: str) -> None:
        raise NotImplementedError

    # -- state --
    def _load_state(self) -> Dict:
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                s = json.load(fh)
            if isinstance(s, dict) and "positions" in s:
                return s
        except Exception:
            pass
        return {"positions": [], "trades": [], "seq": 0}

    def _save_state(self) -> None:
        tmp = self._state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh)
            os.replace(tmp, self._state_path)
        except OSError:
            pass

    def positions(self) -> List[Dict]:
        with self._lock:
            return [dict(p) for p in self.state["positions"]]

    def trades(self, n: int = 20) -> List[Dict]:
        with self._lock:
            return [dict(t) for t in self.state["trades"][-n:]]

    # -- order helpers --
    def _wait_fill(self, order_id: str, qty: int, fallback: float) -> Tuple[float, int]:
        status: Dict = {}
        for _ in range(10):
            status = self._order_status(order_id)
            if status["status"] == "COMPLETE":
                return (status["average_price"] or fallback), (status["filled_quantity"] or qty)
            if status["status"] in ("REJECTED", "CANCELLED"):
                raise BrokerError("Entry order %s: %s" % (status["status"], status["message"] or "no reason given"))
            time.sleep(0.6)
        try:
            self._cancel(order_id)
        except BrokerError:
            pass
        status = self._order_status(order_id)
        if status["filled_quantity"] > 0:
            return (status["average_price"] or fallback), status["filled_quantity"]
        raise BrokerError("Entry order was not filled (status %s). Cancel attempted - check your broker app."
                          % status.get("status"))

    def _flatten(self, symbol: str, side: str, qty: int) -> None:
        self._place(symbol, opposite(side), qty, "MARKET", None)

    # -- trade lifecycle --
    def open_trade(self, symbol: str, side: str, signal_entry: float, sl: float, tp: float,
                   risk: RiskManager) -> Dict:
        side = side.upper()
        if side not in (BUY, SELL):
            raise BrokerError("Direction must be BUY or SELL")
        if not self.has_session():
            raise BrokerError("Not logged in to %s today. Open SETTINGS and log in." % self.name)
        with self._lock:
            if self.state["positions"]:
                raise BrokerError("One open position at a time. Close it first.")
        f = self.funds()
        equity, cash = f["equity"], f["cash"]
        try:
            risk.assert_can_trade(equity)
        except RiskError as exc:
            raise BrokerError(str(exc))
        ltp = self.ltp(symbol)
        if signal_entry <= 0 or abs(ltp - signal_entry) / signal_entry > MAX_LTP_DEVIATION:
            raise BrokerError("Signal price %.2f is more than 0.5%% away from the broker price %.2f. Skipped."
                              % (signal_entry, ltp))
        if side == BUY and not (sl < ltp < tp):
            raise BrokerError("Plan no longer valid at price %.2f (need SL < price < TP)." % ltp)
        if side == SELL and not (tp < ltp < sl):
            raise BrokerError("Plan no longer valid at price %.2f (need TP < price < SL)." % ltp)
        qty = risk.position_size(equity, ltp, sl, cash)
        try:
            risk_amount = risk.validate_order(equity, qty, ltp, sl)
        except RiskError as exc:
            raise BrokerError(str(exc))

        entry_oid = self._place(symbol, side, qty, "MARKET", None)
        entry, filled = self._wait_fill(entry_oid, qty, ltp)
        qty = int(filled)
        stopped_out = (side == BUY and entry <= sl) or (side == SELL and entry >= sl)
        if stopped_out:
            self._flatten(symbol, side, qty)
            raise BrokerError("Fill price %.2f was already beyond the stop. Position flattened." % entry)
        try:
            sl_oid = self._place(symbol, opposite(side), qty, "SL-M", round_tick(sl))
        except BrokerError as exc:
            try:
                self._flatten(symbol, side, qty)
            except BrokerError as exc2:
                raise BrokerError("SL order failed (%s) AND closing failed (%s). CLOSE THE POSITION IN YOUR "
                                  "BROKER APP NOW." % (exc, exc2))
            raise BrokerError("Stop-loss order failed, position closed again: %s" % exc)
        with self._lock:
            self.state["seq"] += 1
            pos = {"id": "T%d" % self.state["seq"], "symbol": symbol, "side": side, "qty": qty,
                   "entry": round(entry, 2), "sl": round_tick(sl), "tp": round_tick(tp),
                   "risk": round(risk_amount, 2), "entry_order_id": entry_oid, "sl_order_id": sl_oid,
                   "opened_at": ist_now().isoformat(timespec="seconds")}
            self.state["positions"].append(pos)
            self._save_state()
        risk.note_trade_opened()
        return dict(pos)

    def _finalize(self, pos: Dict, exit_price: float, reason: str) -> Dict:
        sign = 1 if pos["side"] == BUY else -1
        pnl = round((exit_price - pos["entry"]) * pos["qty"] * sign, 2)
        trade = dict(pos, exit=round(exit_price, 2), pnl=pnl, reason=reason,
                     closed_at=ist_now().isoformat(timespec="seconds"))
        with self._lock:
            self.state["positions"] = [p for p in self.state["positions"] if p["id"] != pos["id"]]
            self.state["trades"] = (self.state["trades"] + [trade])[-300:]
            self._save_state()
        return trade

    def _exit_now(self, pos: Dict, reason: str) -> Dict:
        """Cancel the protective stop, close at market. If closing fails the stop is placed again."""
        if pos.get("sl_order_id"):
            try:
                self._cancel(pos["sl_order_id"])
            except BrokerError:
                pass
        try:
            oid = self._place(pos["symbol"], opposite(pos["side"]), pos["qty"], "MARKET", None)
            price, _ = self._wait_fill(oid, pos["qty"], self.ltp(pos["symbol"]))
        except BrokerError as exc:
            try:
                new_sl = self._place(pos["symbol"], opposite(pos["side"]), pos["qty"], "SL-M", pos["sl"])
                with self._lock:
                    for p in self.state["positions"]:
                        if p["id"] == pos["id"]:
                            p["sl_order_id"] = new_sl
                    self._save_state()
            except BrokerError:
                pass
            raise BrokerError("Exit failed (%s). CHECK YOUR BROKER APP." % exc)
        return self._finalize(pos, price, reason)

    def check_exits(self) -> List[Dict]:
        """Called every few seconds. Returns trades that closed. Never raises."""
        closed: List[Dict] = []
        for pos in self.positions():
            try:
                st = self._order_status(pos["sl_order_id"]) if pos.get("sl_order_id") else None
                if st and st["status"] == "COMPLETE":
                    closed.append(self._finalize(pos, st["average_price"] or pos["sl"], "STOP LOSS"))
                    continue
                if st and st["status"] in ("REJECTED", "CANCELLED"):
                    closed.append(self._exit_now(pos, "STOP ORDER LOST"))
                    continue
                now = ist_now()
                if (now.hour, now.minute) >= EOD_EXIT:
                    closed.append(self._exit_now(pos, "END OF DAY"))
                    continue
                price = self.ltp(pos["symbol"])
                if (pos["side"] == BUY and price >= pos["tp"]) or (pos["side"] == SELL and price <= pos["tp"]):
                    closed.append(self._exit_now(pos, "TARGET"))
                self.last_error = ""
            except BrokerError as exc:
                self.last_error = str(exc)[:140]
        return closed

    def close_all(self, reason: str = "MANUAL") -> Tuple[List[Dict], List[str]]:
        closed, errors = [], []
        for pos in self.positions():
            try:
                closed.append(self._exit_now(pos, reason))
            except BrokerError as exc:
                errors.append("%s: %s" % (pos["symbol"], exc))
        return closed, errors

    def emergency_exit(self, risk: RiskManager) -> Tuple[List[Dict], List[str]]:
        """Kill switch. The backend lock is set FIRST, then every open position is closed."""
        risk.kill("emergency kill switch")
        return self.close_all("KILL SWITCH")


# --------------------------------------------------------------------------- #
# Zerodha Kite Connect
# --------------------------------------------------------------------------- #
class KiteBroker(BaseLiveBroker):
    name = "KITE"

    def has_session(self) -> bool:
        s = self.store
        return bool(s.get("kite_key") and s.get("kite_token") and s.get("kite_day") == _today())

    def clear_session(self) -> None:
        self.store.update(kite_token="", kite_day="")

    def login_url(self) -> str:
        key = self.store.get("kite_key")
        if not key:
            raise BrokerError("Enter your Kite API key first.")
        return "%s?v=3&api_key=%s" % (KITE_LOGIN, urllib.parse.quote(key))

    def login(self, token_or_url: str) -> str:
        s = self.store
        req_token = extract_param(token_or_url, "request_token")
        if not (s.get("kite_key") and s.get("kite_secret") and req_token):
            raise BrokerError("API key, API secret and request token are all needed.")
        checksum = hashlib.sha256((s.get("kite_key") + req_token + s.get("kite_secret")).encode()).hexdigest()
        code, text = http("POST", KITE_ROOT + "/session/token", headers={"X-Kite-Version": "3"},
                          form={"api_key": s.get("kite_key"), "request_token": req_token, "checksum": checksum},
                          timeout=20)
        try:
            payload = json.loads(text)
        except ValueError:
            raise BrokerError("Kite login: unreadable answer (HTTP %s)" % code)
        if code != 200 or payload.get("status") != "success":
            raise BrokerError("Kite login failed: %s" % payload.get("message", "HTTP %s" % code))
        data = payload["data"]
        s.update(kite_token=data["access_token"], kite_day=_today())
        return data.get("user_id", "OK")

    def _call(self, method: str, path: str, params=None, form=None, raw: bool = False, timeout: int = 15):
        if not self.has_session():
            raise BrokerError("Kite session missing or expired. Log in again (tokens last one day).")
        headers = {"X-Kite-Version": "3",
                   "Authorization": "token %s:%s" % (self.store.get("kite_key"), self.store.get("kite_token"))}
        code, text = http(method, KITE_ROOT + path, headers=headers, params=params, form=form, timeout=timeout)
        if raw:
            if code != 200:
                raise BrokerError("Kite HTTP %s on %s" % (code, path))
            return text
        try:
            payload = json.loads(text)
        except ValueError:
            raise BrokerError("Kite: unreadable answer (HTTP %s)" % code)
        if code != 200 or payload.get("status") != "success":
            if payload.get("error_type") == "TokenException":
                self.clear_session()
            raise BrokerError("Kite: %s" % (payload.get("message") or "HTTP %s" % code))
        return payload.get("data")

    def funds(self) -> Dict[str, float]:
        data = self._call("GET", "/user/margins")
        eq = data["equity"]
        cash = float(eq["available"].get("live_balance", eq["available"].get("cash", 0.0)))
        total = float(eq.get("net", cash))
        return {"equity": max(total, cash), "cash": cash}

    def ltp(self, symbol: str) -> float:
        key = "NSE:%s" % symbol
        data = self._call("GET", "/quote/ltp", params={"i": key})
        if key not in data:
            raise BrokerError("Kite returned no price for %s (market data may need the paid Connect plan)." % key)
        return float(data[key]["last_price"])

    def _token(self, symbol: str) -> str:
        path = os.path.join(self.data_dir, "kite_instruments.json")
        mapping = None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
            if blob.get("day") == _today():
                mapping = blob["map"]
        except Exception:
            mapping = None
        if mapping is None:
            text = self._call("GET", "/instruments/NSE", raw=True, timeout=60)
            mapping = {}
            for row in csv.DictReader(io.StringIO(text)):
                if row.get("instrument_type") == "EQ" and row.get("segment") == "NSE":
                    mapping[row.get("tradingsymbol", "").upper()] = row.get("instrument_token", "")
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"day": _today(), "map": mapping}, fh)
            except OSError:
                pass
        token = mapping.get(symbol.upper())
        if not token:
            raise BrokerError("%s was not found on NSE." % symbol)
        return token

    def candles(self, symbol: str, count: int = HISTORY) -> List[Candle]:
        token = self._token(symbol)
        now = ist_now()
        frm = now - timedelta(days=5)
        data = self._call("GET", "/instruments/historical/%s/minute" % token,
                          params={"from": frm.strftime("%Y-%m-%d %H:%M:%S"), "to": now.strftime("%Y-%m-%d %H:%M:%S")},
                          timeout=30)
        rows = data.get("candles", []) if isinstance(data, dict) else []
        out = []
        for r in rows:
            ts = datetime.strptime(str(r[0]), "%Y-%m-%dT%H:%M:%S%z").timestamp()
            out.append(Candle(ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
        if len(out) < 30:
            raise BrokerError("Kite returned %d candles (historical data needs the paid Connect plan)." % len(out))
        return out[-count:]

    def _place(self, symbol: str, side: str, qty: int, order_type: str, trigger: Optional[float]) -> str:
        form = {"tradingsymbol": symbol, "exchange": "NSE", "transaction_type": side, "order_type": order_type,
                "quantity": int(qty), "product": "MIS", "validity": "DAY", "tag": "TDN"}
        if order_type == "SL-M":
            form["trigger_price"] = round_tick(float(trigger))
        data = self._call("POST", "/orders/regular", form=form)
        return str(data["order_id"])

    def _order_status(self, order_id: str) -> Dict:
        data = self._call("GET", "/orders/%s" % order_id)
        last = data[-1] if isinstance(data, list) and data else {}
        return {"status": str(last.get("status", "UNKNOWN")).upper(),
                "average_price": float(last.get("average_price") or 0.0),
                "filled_quantity": int(last.get("filled_quantity") or 0),
                "message": last.get("status_message") or ""}

    def _cancel(self, order_id: str) -> None:
        self._call("DELETE", "/orders/regular/%s" % order_id)


# --------------------------------------------------------------------------- #
# Upstox
# --------------------------------------------------------------------------- #
class UpstoxBroker(BaseLiveBroker):
    name = "UPSTOX"

    def has_session(self) -> bool:
        s = self.store
        return bool(s.get("upstox_token") and s.get("upstox_day") == _today())

    def clear_session(self) -> None:
        self.store.update(upstox_token="", upstox_day="")

    def login_url(self) -> str:
        s = self.store
        if not (s.get("upstox_key") and s.get("upstox_redirect")):
            raise BrokerError("Enter your Upstox API key and redirect URL first.")
        return "%s/login/authorization/dialog?%s" % (UPSTOX_ROOT, urllib.parse.urlencode(
            {"response_type": "code", "client_id": s.get("upstox_key"), "redirect_uri": s.get("upstox_redirect")}))

    def login(self, token_or_url: str) -> str:
        s = self.store
        code_value = extract_param(token_or_url, "code")
        if not (s.get("upstox_key") and s.get("upstox_secret") and s.get("upstox_redirect") and code_value):
            raise BrokerError("API key, API secret, redirect URL and the code are all needed.")
        code, text = http("POST", UPSTOX_ROOT + "/login/authorization/token",
                          headers={"Accept": "application/json"},
                          form={"code": code_value, "client_id": s.get("upstox_key"),
                                "client_secret": s.get("upstox_secret"), "redirect_uri": s.get("upstox_redirect"),
                                "grant_type": "authorization_code"}, timeout=20)
        try:
            payload = json.loads(text)
        except ValueError:
            raise BrokerError("Upstox login: unreadable answer (HTTP %s)" % code)
        if code != 200 or "access_token" not in payload:
            raise BrokerError("Upstox login failed: %s" % self._error_text(payload, code))
        s.update(upstox_token=payload["access_token"], upstox_day=_today())
        return payload.get("user_name") or payload.get("user_id") or "OK"

    @staticmethod
    def _error_text(payload, code) -> str:
        try:
            errs = payload.get("errors")
            if errs:
                return str(errs[0].get("message", errs[0]))[:140]
            return str(payload.get("message") or "HTTP %s" % code)[:140]
        except Exception:
            return "HTTP %s" % code

    def _call(self, method: str, path: str, params=None, body=None, timeout: int = 15):
        if not self.has_session():
            raise BrokerError("Upstox session missing or expired. Log in again (tokens last one day).")
        headers = {"Accept": "application/json", "Authorization": "Bearer %s" % self.store.get("upstox_token")}
        code, text = http(method, UPSTOX_ROOT + path, headers=headers, params=params, body=body, timeout=timeout)
        try:
            payload = json.loads(text)
        except ValueError:
            raise BrokerError("Upstox: unreadable answer (HTTP %s)" % code)
        if code == 401:
            self.clear_session()
            raise BrokerError("Upstox session expired. Log in again.")
        if code != 200 or payload.get("status") != "success":
            raise BrokerError("Upstox: %s" % self._error_text(payload, code))
        return payload.get("data")

    def _key(self, symbol: str) -> str:
        symbol = symbol.upper()
        if symbol in UPSTOX_KEYS:
            return UPSTOX_KEYS[symbol]
        path = os.path.join(self.data_dir, "upstox_instruments.json")
        mapping = None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
            if blob.get("day") == _today():
                mapping = blob["map"]
        except Exception:
            mapping = None
        if mapping is None:
            code, text = http("GET", UPSTOX_INSTRUMENTS, timeout=90)
            if code != 200:
                raise BrokerError("Could not download the Upstox instrument list (HTTP %s)." % code)
            mapping = {}
            for row in json.loads(text):
                if row.get("segment") == "NSE_EQ" and row.get("instrument_type") == "EQ":
                    mapping[str(row.get("trading_symbol", "")).upper()] = row.get("instrument_key", "")
            del text
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"day": _today(), "map": mapping}, fh)
            except OSError:
                pass
        key = mapping.get(symbol)
        if not key:
            raise BrokerError("%s was not found on NSE." % symbol)
        return key

    def funds(self) -> Dict[str, float]:
        data = self._call("GET", "/user/get-funds-and-margin", params={"segment": "SEC"})
        eq = data["equity"]
        cash = float(eq.get("available_margin", 0.0))
        used = float(eq.get("used_margin", 0.0))
        return {"equity": cash + used, "cash": cash}

    def ltp(self, symbol: str) -> float:
        key = self._key(symbol)
        data = self._call("GET", "/market-quote/ltp", params={"instrument_key": key})
        if not isinstance(data, dict) or not data:
            raise BrokerError("Upstox returned no price for %s." % symbol)
        for k, v in data.items():
            if str(k).split(":")[-1].upper() == symbol.upper() or str(v.get("instrument_token", "")) == key:
                return float(v["last_price"])
        raise BrokerError("Upstox price answer does not match %s. Refusing to trade it." % symbol)

    def candles(self, symbol: str, count: int = HISTORY) -> List[Candle]:
        key = urllib.parse.quote(self._key(symbol), safe="")
        data = self._call("GET", "/historical-candle/intraday/%s/1minute" % key, timeout=30)
        rows = data.get("candles", []) if isinstance(data, dict) else []
        out = []
        for r in reversed(rows):                      # Upstox sends the newest candle first
            ts = datetime.fromisoformat(str(r[0])).timestamp()
            out.append(Candle(ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
        if len(out) < 30:
            raise BrokerError("Upstox returned only %d candles today (market closed or just opened)." % len(out))
        return out[-count:]

    def _place(self, symbol: str, side: str, qty: int, order_type: str, trigger: Optional[float]) -> str:
        body = {"quantity": int(qty), "product": "I", "validity": "DAY", "price": 0, "tag": "TDN",
                "instrument_token": self._key(symbol), "order_type": order_type, "transaction_type": side,
                "disclosed_quantity": 0, "trigger_price": round_tick(float(trigger)) if trigger else 0,
                "is_amo": False}
        data = self._call("POST", "/order/place", body=body)
        return str(data["order_id"])

    def _order_status(self, order_id: str) -> Dict:
        data = self._call("GET", "/order/details", params={"order_id": order_id})
        raw = str(data.get("status", "unknown")).lower()
        status = {"complete": "COMPLETE", "rejected": "REJECTED", "cancelled": "CANCELLED"}.get(raw, raw.upper())
        return {"status": status, "average_price": float(data.get("average_price") or 0.0),
                "filled_quantity": int(data.get("filled_quantity") or 0),
                "message": data.get("status_message") or ""}

    def _cancel(self, order_id: str) -> None:
        self._call("DELETE", "/order/cancel", params={"order_id": order_id})


BROKERS = {"KITE": KiteBroker, "ZERODHA": KiteBroker, "UPSTOX": UpstoxBroker}


def make_broker(name: str, store: CredentialStore, data_dir: str) -> BaseLiveBroker:
    cls = BROKERS.get(str(name).upper())
    if cls is None:
        raise BrokerError("Unknown broker %s. Choose KITE or UPSTOX." % name)
    return cls(store, data_dir)


# --------------------------------------------------------------------------- #
# Market data feeds (for the chart and the agents)
# --------------------------------------------------------------------------- #
class DemoFeed:
    """Simulated prices. For looking at the screen only: live orders are refused on this feed."""
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
    """Public Yahoo Finance chart data for NSE stocks (delayed, unofficial)."""
    label = "YAHOO"
    poll_seconds = 15.0
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1m&range=1d"

    def _fetch(self, symbol: str) -> List[Candle]:
        code, text = http("GET", self.URL % urllib.parse.quote(symbol + ".NS"), timeout=12)
        if code != 200:
            raise BrokerError("Yahoo feed: HTTP %s" % code)
        try:
            res = json.loads(text)["chart"]["result"][0]
            ts, q = res["timestamp"], res["indicators"]["quote"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            raise BrokerError("Yahoo feed: unreadable answer")
        out = []
        for i, t in enumerate(ts):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if None in (o, h, l, c):
                continue
            out.append(Candle(float(t), float(o), float(h), float(l), float(c), float(q["volume"][i] or 0)))
        if len(out) < 30:
            raise BrokerError("Yahoo returned only %d candles (market closed?)." % len(out))
        return out[-MAX_CANDLES:]

    def bootstrap(self, symbol: str) -> List[Candle]:
        return self._fetch(symbol)

    def poll(self, symbol: str, candles: List[Candle]) -> None:
        candles[:] = self._fetch(symbol)


class BrokerFeed:
    """Candles straight from the connected broker."""
    poll_seconds = 10.0

    def __init__(self, broker: BaseLiveBroker):
        self.broker = broker
        self.label = broker.name

    def bootstrap(self, symbol: str) -> List[Candle]:
        return self.broker.candles(symbol, HISTORY)

    def poll(self, symbol: str, candles: List[Candle]) -> None:
        candles[:] = self.broker.candles(symbol, MAX_CANDLES)


class MarketStream:
    """Keeps the candles of one symbol fresh. The feed chain is tried in order and moves down on repeated failure."""

    def __init__(self, symbol: str = "SBIN"):
        self._lock = threading.RLock()
        self.symbol = symbol.upper()
        self._candles: List[Candle] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.feed = None
        self.source = "NONE"
        self.note = ""
        self.chain: List = []
        self._fails = 0
        self._index = 0
        self._next_poll = 0.0

    def configure(self, chain: List) -> None:
        """chain: list of feed objects, best first. The last one should be a DemoFeed."""
        self.chain = list(chain)
        self._activate(0)

    def _activate(self, start: int) -> None:
        notes = []
        for i in range(start, len(self.chain)):
            feed = self.chain[i]
            try:
                candles = feed.bootstrap(self.symbol)
            except BrokerError as exc:
                notes.append("%s: %s" % (feed.label, str(exc)[:70]))
                continue
            except Exception as exc:
                notes.append("%s: %s" % (feed.label, str(exc)[:70]))
                continue
            with self._lock:
                self.feed, self._candles, self._fails = feed, candles, 0
                self.source = feed.label
                self.note = " | ".join(notes)
                self._index = i
                self._next_poll = time.time() + feed.poll_seconds
            return
        with self._lock:
            self.feed, self._candles, self.source = None, [], "NONE"
            self.note = " | ".join(notes) or "no data feed available"

    def set_symbol(self, symbol: str) -> None:
        self.symbol = symbol.upper()
        self._activate(0)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            with self._lock:
                feed, sym = self.feed, self.symbol
                work = list(self._candles)
                due = now >= self._next_poll
            if feed is not None and due:
                try:
                    feed.poll(sym, work)
                    with self._lock:
                        if sym == self.symbol and feed is self.feed:
                            self._candles, self._fails = work, 0
                except Exception as exc:
                    with self._lock:
                        self._fails += 1
                        self.note = "%s: %s" % (feed.label, str(exc)[:80])
                        failed = self._fails >= 3
                    if failed and self._index + 1 < len(self.chain):
                        self._activate(self._index + 1)
                with self._lock:
                    self._next_poll = time.time() + getattr(self.feed or feed, "poll_seconds", 5.0)
            self._stop.wait(0.25)

    def snapshot(self) -> Tuple[List[Candle], float, str, str]:
        with self._lock:
            copy = [Candle(c.t, c.o, c.h, c.l, c.c, c.v) for c in self._candles]
            price = copy[-1].c if copy else 0.0
            return copy, price, self.source, self.note
