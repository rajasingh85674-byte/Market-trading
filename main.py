# -*- coding: utf-8 -*-
"""Tactical Neural Matrix (TDN) - voice-first AI trading mentor.

Kivy app for Android and PC. Cyber-Tiger theme, live 1-minute chart, 20-agent activity panel,
JARVIS mentor chat and plyer text-to-speech. PAPER (virtual money) trading only.
Needs agents.py and broker.py next to this file.
"""
import os
import queue
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, Line, Rectangle, RoundedRectangle
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget
from kivy.utils import platform

try:
    from plyer import tts as plyer_tts
except Exception:
    plyer_tts = None

try:
    from plyer import speech as plyer_speech
except Exception:
    plyer_speech = None

BOOT_ERROR = ""
try:
    from agents import (BUY, SELL, AGENT_NAMES, MAX_RISK_PCT, ConsensusEngine, Mentor, RiskState, position_size)
    from broker import BrokerError, MarketStream, PaperBroker
except Exception:
    BOOT_ERROR = traceback.format_exc()

# ---- Cyber-Tiger palette --------------------------------------------------------------------- #
BG = (1 / 255.0, 1 / 255.0, 3 / 255.0, 1)          # #010103 obsidian
PANEL = (0.035, 0.045, 0.07, 1)
CYAN = (0.0, 229 / 255.0, 1.0, 1)                   # #00E5FF
ORANGE = (1.0, 136 / 255.0, 0.0, 1)                 # #FF8800
RED = (1.0, 0.25, 0.28, 1)
TEXT = (0.92, 0.96, 1.0, 1)
MUTED = (0.50, 0.58, 0.66, 1)
HEX_CYAN, HEX_ORANGE, HEX_RED, HEX_TEXT, HEX_MUTED = "00e5ff", "ff8800", "ff4048", "ebf5ff", "7f94a8"

WATCHLIST = ["NIFTY", "BANKNIFTY", "RELIANCE", "TCS", "INFY", "HDFCBANK", "SBIN"]
START_CAPITAL = 100000.0
CHART_CANDLES = 60


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


def nse_status():
    """NSE cash market hours: Mon-Fri 09:15-15:30 India time (holidays are not checked)."""
    ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    if ist.weekday() >= 5:
        return "NSE CLOSED"
    mins = ist.hour * 60 + ist.minute
    return "NSE OPEN" if 9 * 60 + 15 <= mins <= 15 * 60 + 30 else "NSE CLOSED"


def esc(text):
    """Make user text safe inside Kivy markup."""
    return str(text).replace("&", "&amp;").replace("[", "&bl;").replace("]", "&br;")


# ---- widgets ---------------------------------------------------------------------------------- #
class AutoLabel(Label):
    """Wraps to the available width and grows in height, so text never overlaps or gets clipped."""

    def __init__(self, **kw):
        kw.setdefault("size_hint_y", None)
        kw.setdefault("markup", True)
        kw.setdefault("halign", "left")
        kw.setdefault("valign", "top")
        kw.setdefault("color", TEXT)
        kw.setdefault("font_size", sp(14))
        super().__init__(**kw)
        self.height = dp(24)
        self.bind(width=self._wrap, texture_size=self._fit)

    def _wrap(self, *_):
        self.text_size = (max(self.width, 1), None)

    def _fit(self, *_):
        self.height = self.texture_size[1] + dp(4)


class Panel(BoxLayout):
    """Dark card with a neon glow outline whose height follows its content."""

    def __init__(self, accent=CYAN, **kw):
        kw.setdefault("orientation", "vertical")
        kw.setdefault("size_hint_y", None)
        kw.setdefault("padding", dp(12))
        kw.setdefault("spacing", dp(4))
        super().__init__(**kw)
        self.accent = accent
        self.bind(minimum_height=self.setter("height"), pos=self._draw, size=self._draw)

    def _draw(self, *_):
        self.canvas.before.clear()
        with self.canvas.before:
            for grow, alpha in ((dp(5), 0.06), (dp(3), 0.12), (dp(1.5), 0.22)):
                Color(self.accent[0], self.accent[1], self.accent[2], alpha)
                RoundedRectangle(pos=(self.x - grow, self.y - grow),
                                 size=(self.width + 2 * grow, self.height + 2 * grow), radius=[dp(14)])
            Color(*PANEL)
            RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(12)])
            Color(*self.accent)
            Line(rounded_rectangle=(self.x, self.y, self.width, self.height, dp(12)), width=dp(1.1))


def heading(text, color_hex=HEX_CYAN):
    return AutoLabel(text="[b][color=%s]%s[/color][/b]" % (color_hex, text), font_size=sp(12))


class CandleChart(Widget):
    """Live 1-minute candlestick chart drawn straight on the canvas."""

    def __init__(self, **kw):
        kw.setdefault("size_hint_y", None)
        kw.setdefault("height", dp(240))
        super().__init__(**kw)
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
            Color(0.015, 0.02, 0.035, 1)
            Rectangle(pos=self.pos, size=self.size)
            data = self.candles
            if len(data) < 2 or self.width < 10 or self.height < 10:
                return
            hi = max(c.h for c in data)
            lo = min(c.l for c in data)
            for price, _ in self.levels:
                if lo - (hi - lo) <= price <= hi + (hi - lo):
                    hi, lo = max(hi, price), min(lo, price)
            span = (hi - lo) or 1.0
            hi += span * 0.05
            lo -= span * 0.05
            span = hi - lo

            def ypx(p):
                return self.y + (p - lo) / span * self.height

            Color(CYAN[0], CYAN[1], CYAN[2], 0.12)
            for k in range(1, 4):
                gy = self.y + self.height * k / 4.0
                Line(points=[self.x, gy, self.x + self.width, gy], width=1)
            pad = dp(4)
            step = (self.width - 2 * pad) / float(len(data))
            bw = max(1.0, step * 0.6)
            for i, c in enumerate(data):
                cx = self.x + pad + step * (i + 0.5)
                Color(*(CYAN if c.c >= c.o else ORANGE))
                Line(points=[cx, ypx(c.l), cx, ypx(c.h)], width=1)
                y1, y2 = ypx(min(c.o, c.c)), ypx(max(c.o, c.c))
                Rectangle(pos=(cx - bw / 2.0, y1), size=(bw, max(1.0, y2 - y1)))
            for price, col in self.levels:
                if lo <= price <= hi:
                    Color(*col)
                    Line(points=[self.x, ypx(price), self.x + self.width, ypx(price)], width=dp(1.2))


# ---- voice ------------------------------------------------------------------------------------ #
class Voice:
    """JARVIS speaks through plyer.tts. Calls run in a worker thread so the screen never freezes."""

    def __init__(self, log):
        self.log = log
        self.enabled = True
        self._q = queue.Queue(maxsize=3)
        self._warned = False
        self._native = None
        self.listening = False
        threading.Thread(target=self._worker, daemon=True).start()

    def say(self, text):
        if not self.enabled or not text:
            return
        msg = " ".join(str(text).replace("₹", " rupees ").split())[:280]
        try:
            self._q.put_nowait(msg)
        except queue.Full:
            try:
                self._q.get_nowait()
                self._q.put_nowait(msg)
            except Exception:
                pass

    def _worker(self):
        while True:
            msg = self._q.get()
            try:
                if plyer_tts is None:
                    raise RuntimeError("plyer is not installed")
                plyer_tts.speak(message=msg)
            except Exception as exc:
                if not self._native_speak(msg) and not self._warned:
                    self._warned = True
                    self.log("Voice output unavailable (%s). Text chat still works." % str(exc)[:80])
            time.sleep(min(8.0, 0.4 + 0.055 * len(msg)))

    def _native_speak(self, msg):
        """Backup voice through Android's own TextToSpeech (pyjnius, called only from this worker thread)."""
        if platform != "android":
            return False
        try:
            if self._native is None:
                from jnius import autoclass
                activity = autoclass("org.kivy.android.PythonActivity").mActivity
                self._native = autoclass("android.speech.tts.TextToSpeech")(activity, None)
                time.sleep(1.0)
            self._native.speak(msg, 0, None, "tdn")
            return True
        except Exception:
            self._native = None
            return False

    # -- microphone (plyer speech recognition, best effort) --
    def listen(self, on_text):
        if self.listening:
            return
        if plyer_speech is None:
            return self.log("Voice input is not available here. Type your command.")
        if platform == "android":
            try:
                from android.permissions import Permission, request_permissions
                request_permissions([Permission.RECORD_AUDIO], lambda p, g: self._begin(on_text, g))
                return
            except Exception as exc:
                return self.log("Microphone permission error: %s" % str(exc)[:80])
        self._begin(on_text, [True])

    def _begin(self, on_text, grants):
        if not grants or not all(grants):
            return self.log("Microphone permission denied. Type your command.")
        try:
            try:
                plyer_speech.language = "en-IN"
            except Exception:
                pass
            plyer_speech.start()
        except Exception as exc:
            return self.log("Microphone could not start: %s" % str(exc)[:80])
        self.listening = True
        self.log("Listening...")
        state = {"tries": 0}

        def poll(dt):
            state["tries"] += 1
            text = ""
            try:
                res = getattr(plyer_speech, "results", None) or []
                text = str(res[0]) if res else ""
            except Exception:
                text = ""
            if text or state["tries"] >= 16:
                try:
                    plyer_speech.stop()
                except Exception:
                    pass
                self.listening = False
                if text:
                    on_text(text)
                else:
                    self.log("I did not hear anything. Tap MIC and try again.")
                return False
            return True

        Clock.schedule_interval(poll, 0.5)


# ---- application ------------------------------------------------------------------------------ #
class TacticalApp(App):
    title = "Tactical Neural Matrix"

    def build(self):
        Window.clearcolor = BG
        try:
            Window.softinput_mode = "resize"
        except Exception:
            pass
        try:
            return self._build_inner()
        except Exception:
            return self._error_screen(traceback.format_exc())

    def _error_screen(self, text):
        root = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        root.add_widget(AutoLabel(text="[b][color=%s]TACTICAL NEURAL MATRIX - STARTUP ERROR[/color][/b]" % HEX_RED))
        sv = ScrollView(do_scroll_x=False)
        msg = AutoLabel(text=esc(text), font_size=sp(11))
        sv.add_widget(msg)
        root.add_widget(sv)
        return root

    def _build_inner(self):
        if BOOT_ERROR:
            return self._error_screen(BOOT_ERROR)
        self.data_dir = self.user_data_dir if platform == "android" else os.path.join(os.path.expanduser("~"), ".tnm")
        os.makedirs(self.data_dir, exist_ok=True)
        self.symbol = WATCHLIST[0]
        self.decision = None
        self.eval_busy = False
        self.last_eval = 0.0
        self.last_key = None
        self.last_speech_ts = 0.0
        self.price = 0.0
        self.candles = []
        self.source = "DEMO"
        self.note = ""

        self.risk = RiskState(os.path.join(self.data_dir, "risk_data.json"))
        self.engine = ConsensusEngine(self.risk)
        self.paper = PaperBroker(os.path.join(self.data_dir, "paper.json"), START_CAPITAL)
        self.killed = self.paper.is_locked()
        self.mentor = Mentor()
        self.stream = MarketStream(self.symbol, "DEMO")
        self.stream.start()
        self.voice = Voice(self.show_status)

        root = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6))
        root.add_widget(self._build_header())
        self.scroll = ScrollView(do_scroll_x=False, bar_width=dp(3))
        col = BoxLayout(orientation="vertical", spacing=dp(12), size_hint_y=None, padding=(0, dp(4)))
        col.bind(minimum_height=col.setter("height"))
        col.add_widget(self._build_chart())
        col.add_widget(self._build_signal())
        col.add_widget(self._build_buttons())
        col.add_widget(self._build_agents())
        col.add_widget(self._build_chat())
        self.scroll.add_widget(col)
        root.add_widget(self.scroll)
        root.add_widget(self._build_input_bar())

        self.chat("JARVIS", "Online, boss. Paper mode, 1 percent risk cap. Say scan, trade, plan, or ask me anything.")
        Clock.schedule_interval(self._tick, 1.0)
        Clock.schedule_once(lambda dt: self.voice.say("Jarvis online. Capital first, profit second."), 1.5)
        return root

    # ---------------- layout builders ----------------
    def _build_header(self):
        box = Panel(accent=ORANGE, padding=(dp(12), dp(8)), spacing=dp(2))
        box.add_widget(AutoLabel(text="[b][color=%s]TACTICAL NEURAL MATRIX[/color][/b]  [color=%s]TDN | JARVIS[/color]"
                                 % (HEX_ORANGE, HEX_CYAN), font_size=sp(15)))
        self.lbl_price = AutoLabel(text="Loading market data...", font_size=sp(13))
        box.add_widget(self.lbl_price)
        return box

    def _build_chart(self):
        box = Panel(accent=CYAN)
        box.add_widget(heading("LIVE 1-MINUTE CHART"))
        self.chart = CandleChart()
        box.add_widget(self.chart)
        self.lbl_chart = AutoLabel(text="", font_size=sp(12), color=MUTED)
        box.add_widget(self.lbl_chart)
        return box

    def _build_signal(self):
        box = Panel(accent=ORANGE)
        box.add_widget(heading("CONSENSUS SIGNAL", HEX_ORANGE))
        self.lbl_signal = AutoLabel(text="Scanning...", font_size=sp(18))
        self.lbl_levels = AutoLabel(text="", font_size=sp(13))
        self.lbl_reason = AutoLabel(text="", font_size=sp(12), color=MUTED)
        self.lbl_account = AutoLabel(text="", font_size=sp(12), color=MUTED)
        for w in (self.lbl_signal, self.lbl_levels, self.lbl_reason, self.lbl_account):
            box.add_widget(w)
        return box

    def _build_buttons(self):
        grid = GridLayout(cols=2, spacing=dp(8), size_hint_y=None, row_default_height=dp(48),
                          row_force_default=True)
        grid.bind(minimum_height=grid.setter("height"))
        for text, color, fn in (("PAPER TRADE", ORANGE, self.execute), ("SCAN NOW", CYAN, self.scan_now),
                                ("KILL SWITCH", RED, self.kill), ("RE-ARM", CYAN, self.rearm),
                                ("NEXT SYMBOL", CYAN, self.next_symbol), ("VOICE ON/OFF", ORANGE, self.toggle_voice)):
            b = Button(text=text, font_size=sp(13), bold=True, background_normal="", background_color=color,
                       color=(0.01, 0.01, 0.02, 1))
            b.bind(on_release=lambda inst, f=fn: f())
            grid.add_widget(b)
        return grid

    def _build_agents(self):
        box = Panel(accent=CYAN, spacing=dp(6))
        box.add_widget(heading("20-AGENT ACTIVITY PANEL (LIVE)"))
        self.lbl_tally = AutoLabel(text="", font_size=sp(13))
        box.add_widget(self.lbl_tally)
        self.agent_rows = []
        for name in AGENT_NAMES:
            row = AutoLabel(text="[b]%s[/b]  [color=%s]waiting...[/color]" % (name, HEX_MUTED), font_size=sp(12))
            self.agent_rows.append(row)
            box.add_widget(row)
        return box

    def _build_chat(self):
        box = Panel(accent=ORANGE)
        box.add_widget(heading("JARVIS MENTOR CHAT", HEX_ORANGE))
        self.chat_box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        self.chat_box.bind(minimum_height=self.chat_box.setter("height"))
        box.add_widget(self.chat_box)
        return box

    def _build_input_bar(self):
        bar = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(78), spacing=dp(4))
        self.lbl_status = AutoLabel(text="Ready.", font_size=sp(11), color=MUTED)
        self.lbl_status.size_hint_y = None
        bar.add_widget(self.lbl_status)
        row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(48), spacing=dp(6))
        self.input = TextInput(hint_text="Talk to JARVIS: scan, trade, plan, why no trade...", multiline=False,
                               font_size=sp(14), background_color=PANEL, foreground_color=TEXT, cursor_color=CYAN,
                               hint_text_color=MUTED, padding=(dp(10), dp(13)))
        self.input.bind(on_text_validate=lambda *_: self.send())
        mic = Button(text="MIC", size_hint_x=None, width=dp(62), bold=True, background_normal="",
                     background_color=CYAN, color=(0.01, 0.01, 0.02, 1))
        mic.bind(on_release=lambda *_: self.voice.listen(self.handle_text))
        go = Button(text="SEND", size_hint_x=None, width=dp(66), bold=True, background_normal="",
                    background_color=ORANGE, color=(0.01, 0.01, 0.02, 1))
        go.bind(on_release=lambda *_: self.send())
        row.add_widget(self.input)
        row.add_widget(mic)
        row.add_widget(go)
        bar.add_widget(row)
        return bar

    # ---------------- helpers ----------------
    def show_status(self, msg):
        def apply(dt):
            lbl = getattr(self, "lbl_status", None)
            if lbl is not None:
                lbl.text = esc(str(msg)[:160])
        Clock.schedule_once(apply, 0)

    def chat(self, who, text):
        if who == "JARVIS":
            line = "[b][color=%s]JARVIS[/color][/b]  %s" % (HEX_ORANGE, esc(text))
        else:
            line = "[b][color=%s]YOU[/color][/b]  %s" % (HEX_CYAN, esc(text))
        self.chat_box.add_widget(AutoLabel(text=line, font_size=sp(13)))
        while len(self.chat_box.children) > 30:
            self.chat_box.remove_widget(self.chat_box.children[-1])
        Clock.schedule_once(lambda dt: setattr(self.scroll, "scroll_y", 0), 0.15)

    def jarvis(self, text, speak=True):
        self.chat("JARVIS", text)
        if speak:
            self.voice.say(text)

    def prices(self):
        return {self.symbol: self.price}

    def equity(self):
        return self.paper.equity(self.prices())

    # ---------------- live loop ----------------
    def _tick(self, dt):
        try:
            candles, price, source, note = self.stream.snapshot()
            if not candles:
                return
            self.candles, self.price, self.source, self.note = candles, price, source, note
            for t in self.paper.check_exits(self.symbol, candles):
                self.risk.record(t["pnl"])
                self.jarvis("%s closed by %s. %s %s. Equity %s." % (
                    t["symbol"], t["reason"].lower(), "Profit" if t["pnl"] >= 0 else "Loss",
                    inr(abs(t["pnl"])), inr(self.equity())))
            self._refresh_top()
            if time.time() - self.last_eval >= 3.0 and not self.eval_busy:
                self.eval_busy = True
                self.last_eval = time.time()
                threading.Thread(target=self._eval_worker, args=(list(candles), self.equity()), daemon=True).start()
        except Exception as exc:
            self.show_status("Update error: %s" % exc)

    def _eval_worker(self, candles, equity):
        try:
            d = self.engine.evaluate(self.symbol, candles, equity)
            Clock.schedule_once(lambda dt: self._show_decision(d), 0)
        except Exception as exc:
            self.show_status("Analysis error: %s" % exc)
        finally:
            self.eval_busy = False

    def _levels(self):
        out = []
        d = self.decision
        if d is not None and d.approved:
            out = [(d.entry, CYAN), (d.sl, RED), (d.tp, (0.2, 1.0, 0.5, 1))]
        for p in self.paper.positions():
            if p["symbol"] == self.symbol:
                out = [(p["entry"], CYAN), (p["sl"], RED), (p["tp"], (0.2, 1.0, 0.5, 1))]
        return out

    def _refresh_top(self):
        c = self.candles[-1]
        pos = self.paper.positions()
        self.lbl_price.text = "[color=%s]%s[/color]  %s  [color=%s]%s[/color]  |  %s  |  %s  |  [color=%s]%s[/color]" % (
            HEX_CYAN, esc(self.source), esc(self.symbol), HEX_ORANGE, inr(self.price), "PAPER", nse_status(),
            HEX_RED if self.killed else HEX_CYAN, "KILLED" if self.killed else "ARMED")
        self.chart.update(self.candles, self._levels())
        self.lbl_chart.text = "O %.2f  H %.2f  L %.2f  C %.2f  |  %d candles%s" % (
            c.o, c.h, c.l, c.c, len(self.candles), ("  |  " + esc(self.note)) if self.note else "")
        st = self.risk.status()
        self.lbl_account.text = ("Equity %s  |  open %d  |  today %s  |  losses in row %d%s" % (
            inr(self.equity()), len(pos), inr(st["daily_pnl"]), st["consecutive_losses"],
            "  |  [color=%s]LOCKED[/color]" % HEX_RED if st["locked"] else ""))

    def _show_decision(self, d):
        self.decision = d
        for row, v in zip(self.agent_rows, d.votes):
            col = HEX_CYAN if v.signal == BUY else HEX_ORANGE if v.signal == SELL else HEX_MUTED
            tag = "  [color=%s][b]VETO[/b][/color]" % HEX_RED if v.veto else ""
            row.text = "[b]%s[/b]  [color=%s]%s %d%%[/color]%s\n[color=%s]%s[/color]" % (
                esc(v.agent), col, v.signal, int(v.confidence * 100), tag, HEX_MUTED, esc(v.detail))
        buys = sum(1 for v in d.votes if v.signal == BUY)
        sells = sum(1 for v in d.votes if v.signal == SELL)
        self.lbl_tally.text = "[color=%s]BUY %d[/color]  |  [color=%s]SELL %d[/color]  |  neutral %d  |  need %d" % (
            HEX_CYAN, buys, HEX_ORANGE, sells, len(d.votes) - buys - sells, self.engine.min_agree)
        if d.approved:
            col = HEX_CYAN if d.direction == BUY else HEX_ORANGE
            self.lbl_signal.text = "[b][color=%s]%s %s[/color][/b]  %d/%d agents" % (col, d.direction, esc(d.symbol), d.agree, d.total)
            self.lbl_levels.text = "Entry %s  |  SL %s  |  TP %s  |  RR 1:%.1f  |  Qty %d (risk %s)" % (
                inr(d.entry), inr(d.sl), inr(d.tp), d.rr, d.qty, inr(d.risk_amount))
        else:
            self.lbl_signal.text = "[b][color=%s]NO TRADE[/color][/b]  %s  %d/%d agree" % (HEX_MUTED, esc(d.symbol), d.agree, d.total)
            self.lbl_levels.text = "Waiting for a clean setup."
        self.lbl_reason.text = esc(d.reason)
        key = (d.symbol, d.direction, d.approved)
        if d.approved and key != self.last_key and time.time() - self.last_speech_ts > 45:
            self.last_speech_ts = time.time()
            self.jarvis("%s signal on %s. %d of %d agents agree. Say trade to take it on paper." % (
                d.direction, d.symbol, d.agree, d.total))
        self.last_key = key

    # ---------------- user actions ----------------
    def send(self):
        text = self.input.text.strip()
        self.input.text = ""
        if text:
            self.handle_text(text)

    def handle_text(self, text):
        self.chat("YOU", text)
        t = " ".join("".join(ch if ch.isalnum() or ch == " " else " " for ch in text.lower()).split())
        words = t.split()
        try:
            if "kill" in words or "emergency" in words:
                return self.kill()
            if "rearm" in words or t.startswith("re arm"):
                return self.rearm()
            if len(words) >= 2 and words[0] in ("watch", "symbol", "switch"):
                return self.set_symbol(words[1].upper())
            if len(words) <= 3 and ("source" in words or "feed" in words):
                return self.set_source("YAHOO" if ("yahoo" in words or "live" in words) else "DEMO")
            if len(words) <= 3 and "voice" in words and ("off" in words or "on" in words):
                self.voice.enabled = "on" in words
                return self.show_status("Voice %s" % ("ON" if self.voice.enabled else "OFF"))
            if words and (words[0] in ("trade", "execute", "enter", "buy", "sell")
                          or t in ("take trade", "take the trade", "paper trade", "place trade")) and len(words) <= 3:
                return self.execute()
            if len(words) <= 2 and words and words[0] in ("scan", "analyse", "analyze"):
                return self.scan_now()
            snap = {"decision": self.decision, "equity": self.equity(), "open": len(self.paper.positions()),
                    "daily_pnl": self.risk.status()["daily_pnl"], "losses_in_row": self.risk.status()["consecutive_losses"]}
            self.jarvis(self.mentor.reply(text, snap))
        except Exception as exc:
            self.show_status("Command error: %s" % exc)

    def scan_now(self):
        self.last_eval = 0.0
        self.jarvis("Scanning %s with all 20 agents." % self.symbol, speak=False)

    def next_symbol(self):
        i = WATCHLIST.index(self.symbol) if self.symbol in WATCHLIST else -1
        self.set_symbol(WATCHLIST[(i + 1) % len(WATCHLIST)])

    def set_symbol(self, sym):
        self.symbol = sym
        self.decision = None
        self.last_key = None
        if self.paper.positions():
            self.show_status("Position open in %s. Watching it until it closes." % self.paper.positions()[0]["symbol"])
        threading.Thread(target=self._switch_worker, args=(sym, None), daemon=True).start()

    def set_source(self, source):
        threading.Thread(target=self._switch_worker, args=(None, source), daemon=True).start()

    def _switch_worker(self, sym, source):
        try:
            if sym:
                self.stream.set_symbol(sym)
            if source:
                self.stream.set_source(source)
            Clock.schedule_once(lambda dt: self.jarvis("Now watching %s on %s data." % (
                self.stream.symbol, self.stream.source)), 0)
        except Exception as exc:
            self.show_status("Switch failed: %s" % exc)

    def toggle_voice(self):
        self.voice.enabled = not self.voice.enabled
        self.show_status("Voice %s" % ("ON" if self.voice.enabled else "OFF"))
        if self.voice.enabled:
            self.voice.say("Voice on.")

    def execute(self):
        d = self.decision
        if self.killed:
            return self.jarvis("Kill switch is on. Say rearm first.")
        if d is None or not d.approved or d.symbol != self.symbol:
            return self.jarvis("There is no approved setup right now, boss. Patience.")
        if time.time() - d.ts > 30:
            return self.jarvis("That signal is stale. Scanning again.")
        if self.risk.is_locked():
            return self.jarvis("Trading is locked for today after two losses. Rest and review.")
        price = self.price
        if d.direction == BUY and not (d.sl < price < d.tp) or d.direction == SELL and not (d.tp < price < d.sl):
            return self.jarvis("Price has moved past the plan. Skipping this one.")
        qty = position_size(self.equity(), price, d.sl)
        if qty < 1:
            return self.jarvis("Capital is too small for a 1 percent risk on this setup.")
        try:
            pos = self.paper.open_trade(d.symbol, d.direction, qty, price, d.sl, d.tp)
        except BrokerError as exc:
            return self.jarvis("Order refused: %s" % exc)
        self.decision = None
        self.jarvis("Paper trade opened. %s %d of %s at %s. Stop %s, target %s. Risk %s, within 1 percent." % (
            pos["side"], pos["qty"], pos["symbol"], inr(pos["entry"]), inr(pos["sl"]), inr(pos["tp"]), inr(pos["risk"])))

    def kill(self):
        self.killed = True
        closed = self.paper.kill(self.prices())
        for t in closed:
            self.risk.record(t["pnl"])
        self.jarvis("Kill switch on. %d position closed. No new trades until you say rearm." % len(closed))

    def rearm(self):
        self.killed = False
        self.paper.rearm()
        self.jarvis("Re-armed. Trading is allowed again, inside the 1 percent rule.")

    def on_stop(self):
        try:
            self.stream.stop()
        except Exception:
            pass


if __name__ == "__main__":
    TacticalApp().run()
