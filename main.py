# -*- coding: utf-8 -*-
"""Tactical Neural Matrix (TDN) - live NSE trading terminal for Android.

REAL MONEY. EXECUTE sends real intraday orders to your Zerodha Kite or Upstox account.
Screens: live 1-minute chart, 20-agent consensus panel, signals and execution controls,
plus SETTINGS for broker keys, theme/layout switching and the JARVIS voice profile.

Files that must sit next to this one: agents.py, broker.py, risk_manager.py, voice_engine.py
"""
import os
import json
import threading
import time
import traceback

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, Line, Rectangle, RoundedRectangle
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget
from kivy.utils import platform

BOOT_ERROR = ""
try:
    from agents import BUY, SELL, AGENT_NAMES, ConsensusEngine
    from broker import (BrokerError, BrokerFeed, CredentialStore, DemoFeed, MarketStream,
                        YahooFeed, ist_now, make_broker)
    from risk_manager import RiskManager
    from voice_engine import PROFILES, VoiceEngine
except Exception:
    BOOT_ERROR = traceback.format_exc()


# --------------------------------------------------------------------------- #
# Themes and layouts (switchable on the fly in SETTINGS)
# --------------------------------------------------------------------------- #
def _rgb(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0, 1)


THEMES = {
    "CYBER-TIGER": {"bg": "#010103", "panel": "#090C12", "a1": "#00E5FF", "a2": "#FF8800", "up": "#00E5FF",
                    "down": "#FF8800", "text": "#EBF5FF", "muted": "#7F94A8", "danger": "#FF4048", "ok": "#2BFF88"},
    "AMBER TERMINAL": {"bg": "#030201", "panel": "#0F0A04", "a1": "#FF8800", "a2": "#FFC24A", "up": "#FFC24A",
                       "down": "#FF5A1F", "text": "#FFF1DC", "muted": "#9A8266", "danger": "#FF4048", "ok": "#FFC24A"},
    "ICE CYAN": {"bg": "#010305", "panel": "#06101A", "a1": "#00E5FF", "a2": "#7AB8FF", "up": "#00E5FF",
                 "down": "#7A8CFF", "text": "#E8F8FF", "muted": "#6E8EA3", "danger": "#FF4048", "ok": "#00E5FF"},
    "MATRIX GREEN": {"bg": "#010301", "panel": "#06110A", "a1": "#2BFF88", "a2": "#FF8800", "up": "#2BFF88",
                     "down": "#FF4048", "text": "#E6FFF0", "muted": "#6D9A7F", "danger": "#FF4048", "ok": "#2BFF88"},
}
LAYOUTS = {
    "COMMAND": {"chart": 240, "detail": True, "order": ("chart", "signal", "buttons", "agents")},
    "CHART FOCUS": {"chart": 380, "detail": False, "order": ("chart", "signal", "buttons", "agents")},
    "AGENT FOCUS": {"chart": 150, "detail": True, "order": ("signal", "agents", "chart", "buttons")},
}
FEEDS = ("AUTO", "BROKER", "YAHOO", "DEMO")
WATCHLIST = ["SBIN", "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "ITC", "LT", "AXISBANK", "KOTAKBANK"]
REFERENCE_CAPITAL = 100000.0         # only used for the sizing preview while no broker is connected
CHART_CANDLES = 60

P = {}


def apply_theme(name):
    theme = THEMES.get(name, THEMES["CYBER-TIGER"])
    for k, v in theme.items():
        P[k] = _rgb(v)
        P["hx_" + k] = v.lstrip("#").lower()


apply_theme("CYBER-TIGER")


def inr(x):
    """Indian digit grouping, e.g. 1,00,000.00"""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "-"
    sign = "-" if x < 0 else ""
    whole, frac = ("%.2f" % abs(x)).split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return "%s₹%s.%s" % (sign, whole, frac)


def esc(text):
    return str(text).replace("&", "&amp;").replace("[", "&bl;").replace("]", "&br;")


def nse_open():
    """NSE cash market: Mon-Fri 09:15-15:30 India time (exchange holidays are not checked)."""
    now = ist_now()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= mins <= 15 * 60 + 30


def open_url(url):
    try:
        if platform == "android":
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            Uri = autoclass("android.net.Uri")
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            activity.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        else:
            import webbrowser
            webbrowser.open(url)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Widgets
# --------------------------------------------------------------------------- #
class AutoLabel(Label):
    """Wraps to the available width and grows in height, so text never overlaps or gets clipped."""

    def __init__(self, **kw):
        kw.setdefault("size_hint_y", None)
        kw.setdefault("markup", True)
        kw.setdefault("halign", "left")
        kw.setdefault("valign", "top")
        kw.setdefault("color", P["text"])
        kw.setdefault("font_size", sp(14))
        super().__init__(**kw)
        self.height = dp(24)
        self.bind(width=self._wrap, texture_size=self._fit)

    def _wrap(self, *_):
        self.text_size = (max(self.width, 1), None)

    def _fit(self, *_):
        self.height = self.texture_size[1] + dp(4)


class Panel(BoxLayout):
    """Dark card with a glowing outline whose height follows its content."""

    def __init__(self, accent=None, **kw):
        kw.setdefault("orientation", "vertical")
        kw.setdefault("size_hint_y", None)
        kw.setdefault("padding", dp(12))
        kw.setdefault("spacing", dp(4))
        super().__init__(**kw)
        self.accent = accent or P["a1"]
        self.bind(minimum_height=self.setter("height"), pos=self._draw, size=self._draw)

    def _draw(self, *_):
        self.canvas.before.clear()
        with self.canvas.before:
            for grow, alpha in ((dp(5), 0.06), (dp(3), 0.12), (dp(1.5), 0.22)):
                Color(self.accent[0], self.accent[1], self.accent[2], alpha)
                RoundedRectangle(pos=(self.x - grow, self.y - grow),
                                 size=(self.width + 2 * grow, self.height + 2 * grow), radius=[dp(14)])
            Color(*P["panel"])
            RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(12)])
            Color(*self.accent)
            Line(rounded_rectangle=(self.x, self.y, self.width, self.height, dp(12)), width=dp(1.1))


def heading(text, accent=None):
    hx = "%02x%02x%02x" % tuple(int(c * 255) for c in (accent or P["a1"])[:3])
    return AutoLabel(text="[b][color=%s]%s[/color][/b]" % (hx, text), font_size=sp(12))


def button(text, color, fn, dark_text=True, height=48):
    b = Button(text=text, font_size=sp(13), bold=True, background_normal="", background_color=color,
               color=(0.01, 0.01, 0.02, 1) if dark_text else P["text"], size_hint_y=None, height=dp(height))
    b.bind(on_release=lambda inst: fn())
    return b


class CandleChart(Widget):
    """Live 1-minute candlesticks drawn on the canvas."""

    def __init__(self, height=240, **kw):
        kw.setdefault("size_hint_y", None)
        super().__init__(**kw)
        self.height = dp(height)
        self.candles = []
        self.levels = []
        self.bind(pos=self._draw, size=self._draw)

    def update(self, candles, levels=None):
        self.candles = list(candles[-CHART_CANDLES:])
        self.levels = list(levels or [])
        self._draw()

    def _draw(self, *_):
        self.canvas.clear()
        with self.canvas:
            Color(P["bg"][0] + 0.01, P["bg"][1] + 0.012, P["bg"][2] + 0.02, 1)
            Rectangle(pos=self.pos, size=self.size)
            data = self.candles
            if len(data) < 2 or self.width < 10 or self.height < 10:
                return
            hi = max(c.h for c in data)
            lo = min(c.l for c in data)
            for price, _c in self.levels:
                if lo - (hi - lo) <= price <= hi + (hi - lo):
                    hi, lo = max(hi, price), min(lo, price)
            span = (hi - lo) or 1.0
            hi += span * 0.05
            lo -= span * 0.05
            span = hi - lo

            def ypx(p):
                return self.y + (p - lo) / span * self.height

            Color(P["a1"][0], P["a1"][1], P["a1"][2], 0.12)
            for k in range(1, 4):
                gy = self.y + self.height * k / 4.0
                Line(points=[self.x, gy, self.x + self.width, gy], width=1)
            pad = dp(4)
            step = (self.width - 2 * pad) / float(len(data))
            bw = max(1.0, step * 0.6)
            for i, c in enumerate(data):
                cx = self.x + pad + step * (i + 0.5)
                Color(*(P["up"] if c.c >= c.o else P["down"]))
                Line(points=[cx, ypx(c.l), cx, ypx(c.h)], width=1)
                y1, y2 = ypx(min(c.o, c.c)), ypx(max(c.o, c.c))
                Rectangle(pos=(cx - bw / 2.0, y1), size=(bw, max(1.0, y2 - y1)))
            for price, col in self.levels:
                if lo <= price <= hi:
                    Color(*col)
                    Line(points=[self.x, ypx(price), self.x + self.width, ypx(price)], width=dp(1.2))


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
class TDNApp(App):
    title = "Tactical Neural Matrix"

    def build(self):
        Window.clearcolor = P["bg"]
        try:
            Window.softinput_mode = "below_target"
        except Exception:
            pass
        try:
            return self._build_inner()
        except Exception:
            return self._error_screen(traceback.format_exc())

    def _error_screen(self, text):
        root = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        root.add_widget(AutoLabel(text="[b][color=%s]TDN - STARTUP ERROR[/color][/b]" % P["hx_danger"]))
        sv = ScrollView(do_scroll_x=False)
        sv.add_widget(AutoLabel(text=esc(text), font_size=sp(11)))
        root.add_widget(sv)
        return root

    # ---------------- startup ----------------
    def _build_inner(self):
        if BOOT_ERROR:
            return self._error_screen(BOOT_ERROR)
        self.data_dir = self.user_data_dir if platform == "android" else os.path.join(os.path.expanduser("~"), ".tdn")
        os.makedirs(self.data_dir, exist_ok=True)
        self.ui_path = os.path.join(self.data_dir, "ui.json")
        self.ui = {"theme": "CYBER-TIGER", "layout": "COMMAND", "feed": "AUTO", "symbol": WATCHLIST[0]}
        try:
            with open(self.ui_path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            for k in self.ui:
                if k in saved:
                    self.ui[k] = saved[k]
        except Exception:
            pass
        if self.ui["theme"] not in THEMES:
            self.ui["theme"] = "CYBER-TIGER"
        if self.ui["layout"] not in LAYOUTS:
            self.ui["layout"] = "COMMAND"
        if self.ui["symbol"] not in WATCHLIST:
            self.ui["symbol"] = WATCHLIST[0]
        apply_theme(self.ui["theme"])
        Window.clearcolor = P["bg"]

        self.symbol = self.ui["symbol"]
        self.decision = None
        self.candles = []
        self.price = 0.0
        self.source = "NONE"
        self.note = ""
        self.funds = None
        self.busy = {"eval": False, "exits": False, "funds": False, "order": False}
        self.last_eval = self.last_exits = self.last_funds = 0.0
        self.last_key = None
        self.last_signal_voice = 0.0
        self.last_trade_text = ""
        self.popup = None

        self.store = CredentialStore(os.path.join(self.data_dir, "credentials.json"))
        self.risk = RiskManager(os.path.join(self.data_dir, "risk_data.json"))
        self.engine = ConsensusEngine(self.risk)
        self.broker = self._make_broker(self.store.get("broker") or "UPSTOX")
        self.voice = VoiceEngine(self.data_dir, self.status)
        self.stream = MarketStream(self.symbol)
        self.stream.start()

        self.shell = BoxLayout(orientation="vertical")
        self._build_ui()
        threading.Thread(target=self._feed_worker, daemon=True).start()
        Clock.schedule_interval(self._tick, 1.0)
        Clock.schedule_once(lambda dt: self.voice.say("Jarvis online. Live mode. Capital first, profit second."), 2.0)
        return self.shell

    def _make_broker(self, name):
        try:
            return make_broker(name, self.store, self.data_dir)
        except BrokerError:
            return make_broker("UPSTOX", self.store, self.data_dir)

    def _save_ui(self):
        try:
            with open(self.ui_path, "w", encoding="utf-8") as fh:
                json.dump(self.ui, fh)
        except OSError:
            pass

    # ---------------- feed ----------------
    def _feed_worker(self):
        try:
            mode = self.ui["feed"]
            chain = []
            if mode in ("AUTO", "BROKER") and self.broker.has_session():
                chain.append(BrokerFeed(self.broker))
            if mode in ("AUTO", "YAHOO"):
                chain.append(YahooFeed())
            chain.append(DemoFeed())
            self.stream.symbol = self.symbol
            self.stream.configure(chain)
        except Exception as exc:
            self.status("Feed error: %s" % str(exc)[:100])

    def reload_feed(self):
        threading.Thread(target=self._feed_worker, daemon=True).start()

    # ---------------- UI construction ----------------
    def _build_ui(self):
        lay = LAYOUTS[self.ui["layout"]]
        self.shell.clear_widgets()
        self.shell.padding = dp(8)
        self.shell.spacing = dp(6)
        self.shell.add_widget(self._header())
        self.scroll = ScrollView(do_scroll_x=False, bar_width=dp(3))
        col = BoxLayout(orientation="vertical", spacing=dp(12), size_hint_y=None, padding=(0, dp(4)))
        col.bind(minimum_height=col.setter("height"))
        builders = {"chart": lambda: self._chart_panel(lay["chart"]), "signal": self._signal_panel,
                    "buttons": self._buttons, "agents": lambda: self._agents_panel(lay["detail"])}
        for key in lay["order"]:
            col.add_widget(builders[key]())
        self.scroll.add_widget(col)
        self.shell.add_widget(self.scroll)
        self.lbl_status = AutoLabel(text="Ready.", font_size=sp(11), color=P["muted"])
        self.shell.add_widget(self.lbl_status)
        self._render()

    def _header(self):
        box = Panel(accent=P["a2"], padding=(dp(12), dp(8)), spacing=dp(2))
        box.add_widget(AutoLabel(text="[b][color=%s]TACTICAL NEURAL MATRIX[/color][/b]  [color=%s]TDN | LIVE NSE[/color]"
                                 % (P["hx_a2"], P["hx_a1"]), font_size=sp(15)))
        self.lbl_top = AutoLabel(text="Connecting...", font_size=sp(12))
        box.add_widget(self.lbl_top)
        return box

    def _chart_panel(self, height):
        box = Panel(accent=P["a1"])
        box.add_widget(heading("LIVE 1-MINUTE CHART  (NSE)"))
        self.chart = CandleChart(height=height)
        box.add_widget(self.chart)
        self.lbl_chart = AutoLabel(text="", font_size=sp(12), color=P["muted"])
        box.add_widget(self.lbl_chart)
        return box

    def _signal_panel(self):
        box = Panel(accent=P["a2"])
        box.add_widget(heading("LIVE SIGNAL", P["a2"]))
        self.lbl_signal = AutoLabel(text="Waiting for data...", font_size=sp(18))
        self.lbl_levels = AutoLabel(text="", font_size=sp(13))
        self.lbl_reason = AutoLabel(text="", font_size=sp(12), color=P["muted"])
        self.lbl_account = AutoLabel(text="", font_size=sp(12), color=P["muted"])
        self.lbl_position = AutoLabel(text="", font_size=sp(12))
        for w in (self.lbl_signal, self.lbl_levels, self.lbl_reason, self.lbl_account, self.lbl_position):
            box.add_widget(w)
        return box

    def _buttons(self):
        grid = GridLayout(cols=2, spacing=dp(8), size_hint_y=None, row_default_height=dp(48), row_force_default=True)
        grid.bind(minimum_height=grid.setter("height"))
        items = (("EXECUTE LIVE", P["a2"], self.execute, True), ("SCAN NOW", P["a1"], self.scan_now, True),
                 ("KILL SWITCH", P["danger"], self.kill, True), ("RE-ARM", P["a1"], self.rearm, True),
                 ("NEXT SYMBOL", P["a1"], self.next_symbol, True), ("VOICE ON/OFF", P["a2"], self.toggle_voice, True),
                 ("CLOSE POSITION", P["danger"], self.close_position, True), ("SETTINGS", P["panel"], self.open_settings, False))
        for text, color, fn, dark in items:
            grid.add_widget(button(text, color, fn, dark))
        return grid

    def _agents_panel(self, detail):
        box = Panel(accent=P["a1"], spacing=dp(6))
        box.add_widget(heading("20-AGENT CONSENSUS PANEL (LIVE)"))
        self.lbl_tally = AutoLabel(text="", font_size=sp(13))
        box.add_widget(self.lbl_tally)
        self.agent_detail = detail
        self.agent_rows = []
        for name in AGENT_NAMES:
            row = AutoLabel(text="[b]%s[/b]  [color=%s]waiting...[/color]" % (name, P["hx_muted"]), font_size=sp(12))
            self.agent_rows.append(row)
            box.add_widget(row)
        return box

    # ---------------- helpers ----------------
    def status(self, msg):
        def apply(dt):
            lbl = getattr(self, "lbl_status", None)
            if lbl is not None:
                lbl.text = esc(str(msg)[:200])
        Clock.schedule_once(apply, 0)

    def alert(self, text, speak=True, priority=False):
        self.status(text)
        if speak:
            self.voice.say(text, priority)

    def equity(self):
        if self.funds and self.funds.get("equity"):
            return float(self.funds["equity"])
        return REFERENCE_CAPITAL

    def cash(self):
        if self.funds and self.funds.get("cash") is not None:
            return float(self.funds["cash"])
        return None

    # ---------------- live loop ----------------
    def _tick(self, dt):
        try:
            candles, price, source, note = self.stream.snapshot()
            now = time.time()
            if candles:
                self.candles, self.price, self.source, self.note = candles, price, source, note
                self._refresh_top()
                if now - self.last_eval >= 3.0 and not self.busy["eval"]:
                    self.busy["eval"] = True
                    self.last_eval = now
                    threading.Thread(target=self._eval_worker, args=(list(candles), self.equity(), self.cash(), self.symbol),
                                     daemon=True).start()
            else:
                self.lbl_top.text = "Connecting to market data...  %s" % esc(note)
            if now - self.last_exits >= 5.0 and not self.busy["exits"] and self.broker.positions():
                self.busy["exits"] = True
                self.last_exits = now
                threading.Thread(target=self._exits_worker, daemon=True).start()
            if now - self.last_funds >= 20.0 and not self.busy["funds"] and self.broker.has_session():
                self.busy["funds"] = True
                self.last_funds = now
                threading.Thread(target=self._funds_worker, daemon=True).start()
        except Exception as exc:
            self.status("Update error: %s" % str(exc)[:100])

    def _eval_worker(self, candles, equity, cash, symbol):
        try:
            d = self.engine.evaluate(symbol, candles, equity, cash)
            Clock.schedule_once(lambda dt: self._show_decision(d), 0)
        except Exception as exc:
            self.status("Analysis error: %s" % str(exc)[:100])
        finally:
            self.busy["eval"] = False

    def _exits_worker(self):
        try:
            closed = self.broker.check_exits()
            for t in closed:
                self.risk.record_result(t["pnl"])
            if closed:
                Clock.schedule_once(lambda dt: self._after_close(closed), 0)
            elif self.broker.last_error:
                self.status("Position monitor: %s" % self.broker.last_error)
        except Exception as exc:
            self.status("Monitor error: %s" % str(exc)[:100])
        finally:
            self.busy["exits"] = False

    def _funds_worker(self):
        try:
            f = self.broker.funds()
            self.funds = f
        except BrokerError as exc:
            self.status("Funds: %s" % exc)
        except Exception as exc:
            self.status("Funds error: %s" % str(exc)[:100])
        finally:
            self.busy["funds"] = False

    def _after_close(self, closed):
        for t in closed:
            self.last_trade_text = "%s %s closed by %s: %s" % (t["side"], t["symbol"], t["reason"].lower(), inr(t["pnl"]))
            self.alert("%s closed by %s. %s %s." % (t["symbol"], t["reason"].lower(),
                                                    "Profit" if t["pnl"] >= 0 else "Loss", inr(abs(t["pnl"]))), True, True)
        self.last_funds = 0.0
        self._refresh_top()

    # ---------------- rendering ----------------
    def _levels(self):
        out = []
        d = self.decision
        if d is not None and d.approved and d.symbol == self.symbol:
            out = [(d.entry, P["a1"]), (d.sl, P["danger"]), (d.tp, P["ok"])]
        for p in self.broker.positions():
            if p["symbol"] == self.symbol:
                out = [(p["entry"], P["a1"]), (p["sl"], P["danger"]), (p["tp"], P["ok"])]
        return out

    def _render(self):
        self._refresh_top()
        if self.decision is not None:
            self._show_decision(self.decision, speak=False)

    def _refresh_top(self):
        st = self.risk.status()
        if st["killed"]:
            arm = "[color=%s]KILLED[/color]" % P["hx_danger"]
        elif st["locked"]:
            arm = "[color=%s]LOCKED TODAY[/color]" % P["hx_danger"]
        else:
            arm = "[color=%s]ARMED[/color]" % P["hx_ok"]
        conn = ("%s connected" % self.broker.name) if self.broker.has_session() else "%s NOT connected" % self.broker.name
        self.lbl_top.text = "[color=%s]%s[/color]  %s  [color=%s]%s[/color]  |  %s  |  %s  |  %s" % (
            P["hx_a1"], esc(self.source), esc(self.symbol), P["hx_a2"], inr(self.price),
            "NSE OPEN" if nse_open() else "NSE CLOSED", arm, esc(conn))
        if not self.candles:
            return
        c = self.candles[-1]
        self.chart.update(self.candles, self._levels())
        self.lbl_chart.text = "O %.2f  H %.2f  L %.2f  C %.2f  |  %d candles%s" % (
            c.o, c.h, c.l, c.c, len(self.candles), ("  |  " + esc(self.note)) if self.note else "")
        f = self.funds
        acct = ("Equity %s  |  cash %s" % (inr(f["equity"]), inr(f["cash"]))) if f else "Equity: not connected (preview on %s)" % inr(REFERENCE_CAPITAL)
        self.lbl_account.text = "%s  |  today %s  |  losses in row %d  |  trades today %d" % (
            acct, inr(st["daily_pnl"]), st["consecutive_losses"], st["trades_today"])
        pos = [p for p in self.broker.positions()]
        if pos:
            p = pos[0]
            sign = 1 if p["side"] == BUY else -1
            live = (self.price - p["entry"]) * p["qty"] * sign if p["symbol"] == self.symbol else 0.0
            self.lbl_position.text = "[b]OPEN[/b] %s %d %s @ %s  SL %s  TP %s  P&L %s" % (
                p["side"], p["qty"], esc(p["symbol"]), inr(p["entry"]), inr(p["sl"]), inr(p["tp"]), inr(live))
        else:
            self.lbl_position.text = esc(self.last_trade_text) if self.last_trade_text else "No open position."

    def _show_decision(self, d, speak=True):
        if d.symbol != self.symbol:
            return
        self.decision = d
        for row, v in zip(self.agent_rows, d.votes):
            col = P["hx_up"] if v.signal == BUY else P["hx_down"] if v.signal == SELL else P["hx_muted"]
            tag = "  [color=%s][b]VETO[/b][/color]" % P["hx_danger"] if v.veto else ""
            text = "[b]%s[/b]  [color=%s]%s %d%%[/color]%s" % (esc(v.agent), col, v.signal, int(v.confidence * 100), tag)
            if self.agent_detail:
                text += "\n[color=%s]%s[/color]" % (P["hx_muted"], esc(v.detail))
            row.text = text
        buys = sum(1 for v in d.votes if v.signal == BUY)
        sells = sum(1 for v in d.votes if v.signal == SELL)
        vetoes = sum(1 for v in d.votes if v.veto)
        self.lbl_tally.text = "[color=%s]BUY %d[/color]  |  [color=%s]SELL %d[/color]  |  neutral %d  |  [color=%s]VETO %d[/color]  |  need %d" % (
            P["hx_up"], buys, P["hx_down"], sells, len(d.votes) - buys - sells, P["hx_danger"], vetoes, self.engine.min_agree)
        if d.approved:
            col = P["hx_up"] if d.direction == BUY else P["hx_down"]
            self.lbl_signal.text = "[b][color=%s]%s %s[/color][/b]  %d/%d agents" % (col, d.direction, esc(d.symbol), d.agree, d.total)
            self.lbl_levels.text = "Entry %s  |  SL %s  |  TP %s  |  RR 1:%.1f  |  Qty %d (risk %s)" % (
                inr(d.entry), inr(d.sl), inr(d.tp), d.rr, d.qty, inr(d.risk_amount))
        else:
            self.lbl_signal.text = "[b][color=%s]NO TRADE[/color][/b]  %s  %d/%d agree" % (P["hx_muted"], esc(d.symbol), d.agree, d.total)
            self.lbl_levels.text = "Waiting for a clean setup."
        self.lbl_reason.text = esc(d.reason)
        key = (d.symbol, d.direction, d.approved)
        if speak and d.approved and key != self.last_key and time.time() - self.last_signal_voice > 60:
            self.last_signal_voice = time.time()
            self.voice.say("%s signal on %s. %d of %d agents agree. Tap execute live to review the order." % (
                d.direction, d.symbol, d.agree, d.total))
        self.last_key = key

    # ---------------- controls ----------------
    def scan_now(self):
        self.last_eval = 0.0
        self.status("Scanning %s with all 20 agents..." % self.symbol)

    def next_symbol(self):
        i = WATCHLIST.index(self.symbol) if self.symbol in WATCHLIST else -1
        self.symbol = WATCHLIST[(i + 1) % len(WATCHLIST)]
        self.ui["symbol"] = self.symbol
        self._save_ui()
        self.decision = None
        self.last_key = None
        self.candles = []
        self.status("Loading %s ..." % self.symbol)
        threading.Thread(target=self._switch_worker, args=(self.symbol,), daemon=True).start()

    def _switch_worker(self, sym):
        try:
            self.stream.set_symbol(sym)
        except Exception as exc:
            self.status("Switch failed: %s" % str(exc)[:100])

    def toggle_voice(self):
        self.voice.set_enabled(not self.voice.enabled)
        self.status("Voice %s. %s" % ("ON" if self.voice.enabled else "OFF", self.voice.status()))
        if self.voice.enabled:
            self.voice.say("Voice on.", True)

    # ---- live execution ----
    def _block_reason(self):
        d = self.decision
        if self.risk.is_killed():
            return "Kill switch is ON. Tap RE-ARM first."
        if not self.broker.has_session():
            return "Not logged in to %s today. Open SETTINGS and connect." % self.broker.name
        if self.source in ("DEMO", "NONE"):
            return "Live orders are blocked while the screen shows simulated DEMO prices."
        if not nse_open():
            return "NSE is closed. Orders are possible only 09:15 to 15:30 India time."
        if d is None or not d.approved or d.symbol != self.symbol:
            return "There is no approved setup right now. Patience."
        if time.time() - d.ts > 45:
            return "That signal is stale. Scan again."
        if self.broker.positions():
            return "A position is already open. Close it first."
        ok, why = self.risk.can_trade(self.equity())
        if not ok:
            return why
        if self.busy["order"]:
            return "An order is already being sent."
        return ""

    def execute(self):
        why = self._block_reason()
        if why:
            return self.alert(why)
        d = self.decision
        qty = self.risk.position_size(self.equity(), self.price, d.sl, self.cash())
        risk_amount = qty * abs(self.price - d.sl)
        if qty < 1:
            return self.alert("Capital is too small for a 1 percent risk on this setup.")
        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        text = ("[b][color=%s]REAL MONEY ORDER[/color][/b]\n%s %s  about %d shares at %s\nStop-loss %s   Target %s\n"
                "Money at risk %s (1%% cap)\nBroker: %s  |  Intraday\n\nThe order is sent immediately and cannot be undone "
                "from this app except by closing the position.") % (
            P["hx_danger"], d.direction, esc(d.symbol), qty, inr(self.price), inr(d.sl), inr(d.tp),
            inr(risk_amount), self.broker.name)
        content.add_widget(AutoLabel(text=text, font_size=sp(14)))
        row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(52), spacing=dp(8))
        pop = Popup(title="CONFIRM LIVE ORDER", content=content, size_hint=(0.94, None), height=dp(430),
                    auto_dismiss=False)
        row.add_widget(button("CONFIRM", P["a2"], lambda: (pop.dismiss(), self._start_order(d)), True, 52))
        row.add_widget(button("CANCEL", P["panel"], pop.dismiss, False, 52))
        content.add_widget(row)
        pop.open()
        Clock.schedule_once(lambda dt: pop.dismiss(), 45)

    def _start_order(self, d):
        if time.time() - d.ts > 60:
            return self.alert("The signal expired while you were reading. Scan again.")
        now_d = self.decision
        if now_d is None or not now_d.approved or now_d.direction != d.direction or now_d.symbol != d.symbol:
            return self.alert("The signal changed while you were reading. Order cancelled.")
        why = self._block_reason()
        if why:
            return self.alert(why)
        self.busy["order"] = True
        self.status("Sending order to %s ..." % self.broker.name)
        threading.Thread(target=self._order_worker, args=(d, self.price), daemon=True).start()

    def _order_worker(self, d, price):
        try:
            pos = self.broker.open_trade(d.symbol, d.direction, price, d.sl, d.tp, self.risk)
            self.last_funds = 0.0
            self.alert("Live order filled. %s %d of %s at %s. Stop %s, target %s. Risk %s." % (
                pos["side"], pos["qty"], pos["symbol"], inr(pos["entry"]), inr(pos["sl"]), inr(pos["tp"]), inr(pos["risk"])),
                True, True)
            self.decision = None
        except BrokerError as exc:
            self.alert("Order not placed: %s" % exc, True, True)
        except Exception as exc:
            self.alert("Order error: %s" % str(exc)[:120], True, True)
        finally:
            self.busy["order"] = False

    def close_position(self):
        if not self.broker.positions():
            return self.status("No open position.")
        threading.Thread(target=self._close_worker, args=("MANUAL", False), daemon=True).start()
        self.status("Closing position...")

    def kill(self):
        self.risk.kill("manual kill switch")            # backend lock first, instantly
        self.alert("Kill switch on. Trading is locked. Closing open positions.", True, True)
        self._refresh_top()
        threading.Thread(target=self._close_worker, args=("KILL SWITCH", True), daemon=True).start()

    def _close_worker(self, reason, emergency):
        try:
            if emergency:
                closed, errors = self.broker.emergency_exit(self.risk)
            else:
                closed, errors = self.broker.close_all(reason)
            for t in closed:
                self.risk.record_result(t["pnl"])
            if closed:
                Clock.schedule_once(lambda dt: self._after_close(closed), 0)
            if errors:
                self.alert("Could not close: %s. CHECK YOUR BROKER APP NOW." % "; ".join(errors)[:160], True, True)
            elif emergency:
                self.status("Kill switch complete. %d position closed. Tap RE-ARM to trade again." % len(closed))
        except Exception as exc:
            self.alert("Close error: %s. CHECK YOUR BROKER APP." % str(exc)[:100], True, True)

    def rearm(self):
        self.risk.rearm()
        if self.risk.is_locked():
            self.alert("Re-armed, but trading stays locked today after two losses.")
        else:
            self.alert("Re-armed. Live trading allowed again, inside the 1 percent rule.")
        self._refresh_top()

    # ---------------- settings ----------------
    def open_settings(self):
        store = self.store
        body = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(8), padding=dp(10))
        body.bind(minimum_height=body.setter("height"))

        def title(t):
            body.add_widget(heading(t))

        def field(hint, value="", password=False):
            ti = TextInput(text=value, hint_text=hint, multiline=False, password=password, font_size=sp(13),
                           size_hint_y=None, height=dp(44), background_color=P["panel"], foreground_color=P["text"],
                           hint_text_color=P["muted"], cursor_color=P["a1"], padding=(dp(8), dp(12)))
            body.add_widget(ti)
            return ti

        def spinner(values, current, cb):
            sp_ = Spinner(text=current, values=tuple(values), size_hint_y=None, height=dp(44), font_size=sp(13),
                          background_normal="", background_color=P["a1"], color=(0.01, 0.01, 0.02, 1))
            sp_.bind(text=lambda inst, val: cb(val))
            body.add_widget(sp_)
            return sp_

        title("THEME AND LAYOUT (applies instantly)")
        spinner(THEMES.keys(), self.ui["theme"], self._set_theme)
        spinner(LAYOUTS.keys(), self.ui["layout"], self._set_layout)
        title("MARKET DATA FEED")
        spinner(FEEDS, self.ui["feed"], self._set_feed)

        title("BROKER (REAL ORDERS)")
        body.add_widget(AutoLabel(text="Choose your broker, save the keys, open the login page, then paste the address "
                                       "(or just the code / request token) from the browser after you log in. Tokens last "
                                       "one day.", font_size=sp(12), color=P["muted"]))
        broker_spin = spinner(("UPSTOX", "KITE"), "KITE" if self.broker.name == "KITE" else "UPSTOX",
                              lambda v: switch_broker(v))
        f_key = field("API key", store.get("kite_key") if self.broker.name == "KITE" else store.get("upstox_key"))
        f_secret = field("API secret", store.get("kite_secret") if self.broker.name == "KITE" else store.get("upstox_secret"),
                         password=True)
        f_redirect = field("Upstox redirect URL (must match your Upstox app)", store.get("upstox_redirect"))
        lbl_login = AutoLabel(text="", font_size=sp(12), color=P["muted"])
        f_paste = field("Paste redirect address or token here")

        def switch_broker(name):
            if name == "KITE":
                f_key.text, f_secret.text = store.get("kite_key"), store.get("kite_secret")
            else:
                f_key.text, f_secret.text = store.get("upstox_key"), store.get("upstox_secret")

        def save_keys():
            name = broker_spin.text
            if name == "KITE":
                store.update(broker="KITE", kite_key=f_key.text, kite_secret=f_secret.text)
            else:
                store.update(broker="UPSTOX", upstox_key=f_key.text, upstox_secret=f_secret.text,
                             upstox_redirect=f_redirect.text)
            self.broker = self._make_broker(name)
            lbl_login.text = "Saved. Broker is now %s." % name
            self.reload_feed()

        def show_login():
            save_keys()
            try:
                url = self.broker.login_url()
            except BrokerError as exc:
                lbl_login.text = esc(exc)
                return
            ok = open_url(url)
            lbl_login.text = ("Browser opened. Log in, then copy the address of the page you land on and paste it below.\n"
                              if ok else "Open this address in your browser:\n") + esc(url)

        def connect():
            save_keys()
            text = f_paste.text.strip()
            if not text:
                lbl_login.text = "Paste the address or token first."
                return
            lbl_login.text = "Connecting..."

            def work():
                try:
                    who = self.broker.login(text)
                    Clock.schedule_once(lambda dt: setattr(lbl_login, "text", "Connected: %s" % esc(who)), 0)
                    self.last_funds = 0.0
                    self.reload_feed()
                except BrokerError as exc:
                    Clock.schedule_once(lambda dt: setattr(lbl_login, "text", esc(exc)), 0)
            threading.Thread(target=work, daemon=True).start()

        def disconnect():
            self.broker.clear_session()
            self.funds = None
            lbl_login.text = "Logged out."
            self.reload_feed()

        for text, color, fn, dark in (("SAVE KEYS", P["a1"], save_keys, True), ("1. OPEN LOGIN PAGE", P["a2"], show_login, True),
                                      ("2. CONNECT", P["a1"], connect, True), ("LOG OUT", P["panel"], disconnect, False)):
            body.add_widget(button(text, color, fn, dark, 46))
        body.add_widget(lbl_login)

        title("JARVIS VOICE PROFILE")
        labels = {label: key for key, label in self.voice.profiles()}
        current = PROFILES[self.voice.profile]["label"]

        def pick_profile(val):
            self.voice.set_profile(labels.get(val, "JARVIS"))
            self.status("Profile: %s. Tap TEST VOICE." % val)

        def pick_voice(val):
            self.voice.set_voice("" if val == "AUTOMATIC" else val)

        spinner(labels.keys(), current, pick_profile)
        voices = ["AUTOMATIC"] + [n for n, _t in self.voice.installed_voices()]
        spinner(voices, self.voice.voice_name or "AUTOMATIC", pick_voice)
        body.add_widget(button("TEST VOICE", P["a2"], self.voice.test, True, 46))
        body.add_widget(AutoLabel(text=esc(self.voice.status()), font_size=sp(12), color=P["muted"]))

        title("SAFETY RULES (fixed)")
        body.add_widget(AutoLabel(text="1% maximum risk per trade. At least 9 of 20 agents must agree, reward:risk at least "
                                       "1:2, no trade on weak volume or traps. Two losses in a row lock the day. Open "
                                       "positions are closed at 15:15 India time. The kill switch locks the backend until "
                                       "you tap RE-ARM.", font_size=sp(12), color=P["muted"]))

        sv = ScrollView(do_scroll_x=False)
        sv.add_widget(body)
        outer = BoxLayout(orientation="vertical", spacing=dp(6))
        outer.add_widget(sv)
        outer.add_widget(button("CLOSE", P["a2"], lambda: self.popup.dismiss(), True, 48))
        self.popup = Popup(title="SETTINGS", content=outer, size_hint=(0.97, 0.95))
        self.popup.open()

    def _set_theme(self, name):
        if name == self.ui["theme"] or name not in THEMES:
            return
        self.ui["theme"] = name
        self._save_ui()
        apply_theme(name)
        Window.clearcolor = P["bg"]
        self._build_ui()

    def _set_layout(self, name):
        if name == self.ui["layout"] or name not in LAYOUTS:
            return
        self.ui["layout"] = name
        self._save_ui()
        self._build_ui()

    def _set_feed(self, name):
        if name == self.ui["feed"] or name not in FEEDS:
            return
        self.ui["feed"] = name
        self._save_ui()
        self.reload_feed()
        self.status("Data feed: %s" % name)

    def on_stop(self):
        try:
            self.stream.stop()
        except Exception:
            pass


if __name__ == "__main__":
    TDNApp().run()
