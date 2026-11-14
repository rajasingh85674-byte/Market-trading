# -*- coding: utf-8 -*-
"""Tactical Neural Matrix (JARVIS) - Kivy UI, voice interaction, mentoring dashboard.

Runs on Android (APK) and on a PC. PAPER mode only: no real orders are sent.
Needs agents.py and broker.py next to this file.
"""
import os
import re
import threading
import time
import traceback

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, Line, RoundedRectangle
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.utils import platform

BOOT_ERROR = ""
try:
    from agents import BUY, SELL, MAX_RISK_PCT, ConsensusEngine, RiskManager, WealthMentor, build_context, position_size
    from broker import BrokerError, make_broker
except Exception:  # shown on screen instead of closing the app
    BOOT_ERROR = traceback.format_exc()

# ---- cyber-tiger palette: near-black, neon blue, amber/orange ------------------------------ #
BG = (0.035, 0.04, 0.055, 1)
PANEL = (0.07, 0.085, 0.11, 1)
BLUE = (0.0, 0.82, 1.0, 1)
AMBER = (1.0, 0.62, 0.08, 1)
ORANGE = (1.0, 0.40, 0.05, 1)
GREEN = (0.15, 1.0, 0.55, 1)
RED = (1.0, 0.22, 0.25, 1)
TEXT = (0.90, 0.94, 0.98, 1)
MUTED = (0.52, 0.60, 0.68, 1)

EXCHANGE = "NSE"
INTERVAL = "5minute"
WATCHLIST = ["NIFTY", "BANKNIFTY", "RELIANCE", "TCS", "INFY", "HDFCBANK", "SBIN"]
START_CAPITAL = 10000.0


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


# ---- widgets -------------------------------------------------------------------------------- #
class Glow(BoxLayout):
    """Dark panel with a glowing neon outline."""

    def __init__(self, accent=BLUE, **kw):
        super().__init__(**kw)
        self.accent = accent
        self.bind(pos=self._draw, size=self._draw)

    def _draw(self, *_):
        self.canvas.before.clear()
        with self.canvas.before:
            for grow, alpha in ((dp(5), 0.07), (dp(3), 0.13), (dp(1.5), 0.25)):
                Color(*self.accent[:3], alpha)
                RoundedRectangle(pos=(self.x - grow, self.y - grow),
                                 size=(self.width + 2 * grow, self.height + 2 * grow), radius=[dp(14)])
            Color(*PANEL)
            RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(12)])
            Color(*self.accent)
            Line(rounded_rectangle=(self.x, self.y, self.width, self.height, dp(12)), width=dp(1.1))


def lab(text="", size=14, color=TEXT, bold=False, halign="left", height=None):
    w = Label(text=text, font_size=sp(size), color=color, bold=bold, halign=halign, valign="middle",
              markup=True, size_hint_y=None)
    w.height = height if height else dp(size + 10)
    w.bind(width=lambda inst, v: setattr(inst, "text_size", (v, None)))
    return w


def neon_button(text, color, on_press, size=14):
    b = Button(text=text, font_size=sp(size), bold=True, color=(0.02, 0.02, 0.03, 1),
               background_normal="", background_color=color, size_hint_y=None, height=dp(46))
    b.bind(on_release=lambda *_: on_press())
    return b


# ---- voice: native Android TTS / speech recognition, optional desktop libraries ---------------- #
class Voice:
    def __init__(self, on_text, log):
        self.on_text = on_text
        self.log = log
        self.tts = None
        self.pyttsx = None
        self.recognizer = None
        self.busy = False
        self.can_listen = False
        self._init_tts()

    def _init_tts(self):
        try:
            if platform == "android":
                from jnius import autoclass
                activity = autoclass("org.kivy.android.PythonActivity").mActivity
                self.tts = autoclass("android.speech.tts.TextToSpeech")(activity, None)
                self.can_listen = True
            else:
                import pyttsx3
                self.pyttsx = pyttsx3.init()
        except Exception as exc:
            self.log("Voice output unavailable: %s" % exc)

    def speak(self, text):
        text = str(text)[:300]
        try:
            if self.tts is not None:
                self.tts.speak(text, 0, None, "tnm")
            elif self.pyttsx is not None and not self.busy:
                def run():
                    self.busy = True
                    try:
                        self.pyttsx.say(text)
                        self.pyttsx.runAndWait()
                    except Exception:
                        pass
                    self.busy = False
                threading.Thread(target=run, daemon=True).start()
        except Exception as exc:
            self.log("Speak failed: %s" % exc)

    def listen(self):
        """Start one voice-recognition round. The result goes to on_text on the UI thread."""
        try:
            if platform == "android":
                self._listen_android()
            else:
                self._listen_desktop()
        except Exception as exc:
            self.log("Mic unavailable: %s - type the command instead." % exc)

    def _listen_android(self):
        from android.permissions import Permission, request_permissions
        request_permissions([Permission.RECORD_AUDIO], self._after_permission)

    def _after_permission(self, perms, grants):
        if not grants or not all(grants):
            self.log("Microphone permission denied - type the command instead.")
            return
        from android.runnable import run_on_ui_thread
        from jnius import PythonJavaClass, autoclass, java_method

        outer = self

        class Listener(PythonJavaClass):
            __javainterfaces__ = ["android/speech/RecognitionListener"]
            __javacontext__ = "app"

            @java_method("(Landroid/os/Bundle;)V")
            def onReadyForSpeech(self, b):
                outer.log("Listening...")

            @java_method("()V")
            def onBeginningOfSpeech(self):
                pass

            @java_method("(F)V")
            def onRmsChanged(self, v):
                pass

            @java_method("([B)V")
            def onBufferReceived(self, b):
                pass

            @java_method("()V")
            def onEndOfSpeech(self):
                pass

            @java_method("(I)V")
            def onError(self, code):
                outer.log("Did not catch that (code %s). Try again." % code)

            @java_method("(Landroid/os/Bundle;)V")
            def onResults(self, bundle):
                try:
                    res = bundle.getStringArrayList("results_recognition")
                    if res is not None and res.size() > 0:
                        text = str(res.get(0))
                        Clock.schedule_once(lambda dt: outer.on_text(text), 0)
                except Exception as exc:
                    outer.log("Voice result error: %s" % exc)

            @java_method("(Landroid/os/Bundle;)V")
            def onPartialResults(self, b):
                pass

            @java_method("(ILandroid/os/Bundle;)V")
            def onEvent(self, i, b):
                pass

        @run_on_ui_thread
        def start():
            try:
                activity = autoclass("org.kivy.android.PythonActivity").mActivity
                SR = autoclass("android.speech.SpeechRecognizer")
                Intent = autoclass("android.content.Intent")
                if not SR.isRecognitionAvailable(activity):
                    outer.log("Speech recognition is not installed on this phone - type instead.")
                    return
                outer.recognizer = SR.createSpeechRecognizer(activity)
                outer._listener = Listener()
                outer.recognizer.setRecognitionListener(outer._listener)
                intent = Intent("android.speech.action.RECOGNIZE_SPEECH")
                intent.putExtra("android.speech.extra.LANGUAGE_MODEL", "free_form")
                intent.putExtra("android.speech.extra.LANGUAGE", "en-IN")
                intent.putExtra("android.speech.extra.MAX_RESULTS", 1)
                outer.recognizer.startListening(intent)
            except Exception as exc:
                outer.log("Mic start failed: %s" % exc)

        start()

    def _listen_desktop(self):
        import speech_recognition as sr  # optional: pip install SpeechRecognition pyaudio

        def run():
            try:
                r = sr.Recognizer()
                with sr.Microphone() as src:
                    self.log("Listening...")
                    audio = r.listen(src, timeout=6, phrase_time_limit=8)
                text = r.recognize_google(audio, language="en-IN")
                Clock.schedule_once(lambda dt: self.on_text(text), 0)
            except Exception as exc:
                self.log("Did not catch that: %s" % exc)
        threading.Thread(target=run, daemon=True).start()


# ---- app -------------------------------------------------------------------------------------- #
class TacticalApp(App):
    title = "Tactical Neural Matrix"

    def build(self):
        Window.clearcolor = BG
        try:
            return self._build_inner()
        except Exception:
            return self._error_screen(traceback.format_exc())

    def _error_screen(self, text):
        root = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        root.add_widget(lab("TACTICAL NEURAL MATRIX - STARTUP ERROR", 15, RED, True))
        sv = ScrollView()
        msg = Label(text=text, font_size=sp(11), color=TEXT, size_hint_y=None, halign="left", valign="top")
        msg.bind(width=lambda i, w: setattr(i, "text_size", (w, None)),
                 texture_size=lambda i, s: setattr(i, "height", s[1]))
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
        self.scanning = False
        self.killed = os.path.exists(os.path.join(self.data_dir, "kill.flag"))

        self.risk = RiskManager(os.path.join(self.data_dir, "risk_data.json"))
        self.engine = ConsensusEngine(self.risk)
        self.mentor = WealthMentor(os.path.join(self.data_dir, "mentor.json"), START_CAPITAL)
        self.broker = make_broker("PAPER", self.data_dir, paper_capital=START_CAPITAL)
        self.voice = Voice(self.handle_command, self.log)

        root = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(8))
        root.add_widget(self._header())
        body = ScrollView(do_scroll_x=False)
        col = BoxLayout(orientation="vertical", spacing=dp(10), size_hint_y=None, padding=(0, dp(2)))
        col.bind(minimum_height=col.setter("height"))
        col.add_widget(self._mentor_card())
        col.add_widget(self._verdict_card())
        col.add_widget(self._agents_card())
        col.add_widget(self._buttons())
        body.add_widget(col)
        root.add_widget(body)
        root.add_widget(self._bottom_bar())

        Clock.schedule_once(lambda dt: self.refresh_mentor(), 0.2)
        Clock.schedule_interval(lambda dt: self._tick(), 20)
        Clock.schedule_once(lambda dt: self.say("Jarvis online, boss. Paper mode. Capital first, profit second."), 1.0)
        return root

    # -- layout --
    def _header(self):
        box = Glow(accent=AMBER, orientation="horizontal", size_hint_y=None, height=dp(54),
                   padding=(dp(12), dp(4)), spacing=dp(8))
        box.add_widget(lab("[b]TACTICAL NEURAL MATRIX[/b]  [color=00d1ff]JARVIS[/color]", 15, AMBER))
        self.mode_lbl = lab("PAPER", 13, GREEN, True, "right")
        self.mode_lbl.size_hint_x = 0.3
        box.add_widget(self.mode_lbl)
        return box

    def _mentor_card(self):
        card = Glow(accent=BLUE, orientation="vertical", size_hint_y=None, height=dp(176),
                    padding=dp(12), spacing=dp(2))
        card.add_widget(lab("MENTOR  |  DROP BY DROP", 12, BLUE, True))
        self.m_working = lab("", 20, TEXT, True, height=dp(32))
        self.m_banked = lab("", 13, AMBER)
        self.m_day = lab("", 13, TEXT)
        self.m_advice = lab("", 12, MUTED, height=dp(44))
        for w in (self.m_working, self.m_banked, self.m_day, self.m_advice):
            card.add_widget(w)
        return card

    def _verdict_card(self):
        card = Glow(accent=ORANGE, orientation="vertical", size_hint_y=None, height=dp(190),
                    padding=dp(12), spacing=dp(2))
        self.v_title = lab("SIGNAL", 12, ORANGE, True)
        self.v_main = lab("Press SCAN", 20, TEXT, True, height=dp(32))
        self.v_levels = lab("", 13, TEXT)
        self.v_reason = lab("", 12, MUTED, height=dp(60))
        for w in (self.v_title, self.v_main, self.v_levels, self.v_reason):
            card.add_widget(w)
        return card

    def _agents_card(self):
        self.agent_box = Glow(accent=BLUE, orientation="vertical", size_hint_y=None, padding=dp(12), spacing=dp(1))
        self.agent_box.add_widget(lab("11-AGENT CONSENSUS", 12, BLUE, True))
        self.agent_rows = []
        for name in self.engine.agent_names:
            row = lab(name, 12, MUTED, height=dp(22))
            self.agent_rows.append(row)
            self.agent_box.add_widget(row)
        self.agent_box.height = dp(36) + dp(22) * len(self.agent_rows) + dp(12)
        return self.agent_box

    def _buttons(self):
        row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(46), spacing=dp(8))
        row.add_widget(neon_button("SCAN", BLUE, self.scan))
        row.add_widget(neon_button("PAPER TRADE", AMBER, self.execute))
        row.add_widget(neon_button("KILL", RED, self.kill))
        return row

    def _bottom_bar(self):
        bar = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(98), spacing=dp(4))
        self.status = lab("Ready.", 12, MUTED, height=dp(20))
        bar.add_widget(self.status)
        row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(48), spacing=dp(6))
        self.input = TextInput(hint_text="scan | trade | status | plan | watch TCS | kill", multiline=False,
                               font_size=sp(14), background_color=PANEL, foreground_color=TEXT,
                               cursor_color=BLUE, hint_text_color=MUTED, padding=(dp(10), dp(12)))
        self.input.bind(on_text_validate=lambda *_: self._send())
        mic = Button(text="MIC", size_hint_x=None, width=dp(64), bold=True, background_normal="",
                     background_color=BLUE, color=(0.02, 0.02, 0.03, 1))
        mic.bind(on_release=lambda *_: self.voice.listen())
        send = Button(text="GO", size_hint_x=None, width=dp(56), bold=True, background_normal="",
                      background_color=AMBER, color=(0.02, 0.02, 0.03, 1))
        send.bind(on_release=lambda *_: self._send())
        row.add_widget(self.input)
        row.add_widget(mic)
        row.add_widget(send)
        bar.add_widget(row)
        return bar

    # -- helpers --
    def log(self, msg):
        def apply(dt):
            st = getattr(self, "status", None)
            if st is not None:
                st.text = str(msg)[:140]
        Clock.schedule_once(apply, 0)

    def say(self, text):
        self.log(text)
        self.voice.speak(text)

    def _send(self):
        text = self.input.text.strip()
        self.input.text = ""
        if text:
            self.handle_command(text)

    # -- commands --
    def handle_command(self, text):
        t = re.sub(r"[^a-z0-9 ]", " ", str(text).lower())
        try:
            if any(w in t for w in ("kill", "emergency", "stop all")):
                self.kill()
            elif "rearm" in t or "re arm" in t:
                self.rearm()
            elif t.startswith("watch ") or t.startswith("symbol "):
                self.set_symbol(t.split(" ", 1)[1].strip().upper())
            elif any(w in t for w in ("trade", "buy", "sell", "execute", "enter")):
                self.execute()
            elif any(w in t for w in ("scan", "analy", "check", "signal")):
                self.scan()
            elif any(w in t for w in ("plan", "mentor", "advice", "target")):
                p = self.mentor.plan()
                self.say("Working capital %s. Today %s of %s target. %s" % (
                    inr(p["working"]), inr(p["day_pnl"]), inr(p["target"]), p["advice"]))
            elif "status" in t or "balance" in t or "account" in t:
                a = self.broker.account_summary()
                self.say("Paper equity %s. %d open position." % (inr(a.get("equity")), len(self.broker.open_positions())))
            else:
                self.say("Say scan, trade, plan, status, watch followed by a symbol, or kill.")
        except Exception as exc:
            self.log("Command error: %s" % exc)

    def set_symbol(self, sym):
        if not sym:
            return
        self.symbol = sym
        self.decision = None
        self.say("Watching %s. Press scan." % sym)

    # -- scanning --
    def scan(self):
        if self.killed:
            return self.say("Kill switch is on. Say rearm to continue.")
        if self.scanning:
            return
        self.scanning = True
        self.log("Scanning %s ..." % self.symbol)
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            candles = self.broker.get_candles(EXCHANGE, self.symbol, INTERVAL, 200)
            d = self.engine.evaluate(self.symbol, candles)
            Clock.schedule_once(lambda dt: self._show(d), 0)
        except Exception as exc:
            self.log("Scan failed: %s" % exc)
        finally:
            self.scanning = False

    def _show(self, d):
        self.decision = d
        ok = d.approved and d.direction in (BUY, SELL)
        colour = GREEN if (ok and d.direction == BUY) else RED if ok else MUTED
        self.v_title.text = "SIGNAL | %s | %s" % (d.symbol, "APPROVED" if ok else "NO TRADE")
        self.v_main.text = "[color=%s]%s[/color]   %d/%d agents" % (
            "%02x%02x%02x" % tuple(int(c * 255) for c in colour[:3]), d.direction if ok else "WAIT", d.agree, d.total)
        self.v_levels.text = ("Entry %s   SL %s   TP %s   RR 1:%.1f" % (
            inr(d.entry), inr(d.sl), inr(d.tp), d.rr)) if ok else "Mentor says: patience is also a position."
        self.v_reason.text = d.reason or ""
        for row, vote in zip(self.agent_rows, d.votes):
            c = GREEN if vote.signal == BUY else RED if vote.signal == SELL else MUTED
            row.color = RED if vote.veto else c
            row.text = "%s: %s %d%%%s" % (vote.agent, vote.signal, int(vote.confidence * 100), "  VETO" if vote.veto else "")
        self.say(("%s signal on %s, %d of %d agents agree." % (d.direction, d.symbol, d.agree, d.total)) if ok
                 else "No clean setup on %s. Waiting." % d.symbol)

    # -- paper execution --
    def execute(self):
        d = self.decision
        if self.killed:
            return self.say("Kill switch is on. Say rearm to continue.")
        if d is None or not d.approved or d.direction not in (BUY, SELL):
            return self.say("No approved setup. Scan first.")
        if time.time() - d.ts > 300:
            return self.say("That signal is stale. Scan again.")
        if self.risk.is_locked():
            return self.say("Daily lockout after two losses. No new trades today.")
        plan = self.mentor.plan()
        if plan["stop_today"]:
            return self.say(plan["advice"])
        qty = position_size(plan["working"], MAX_RISK_PCT, d.entry, d.sl)
        if qty < 1:
            return self.say("Capital too small for a 1 percent risk on this setup. Skipping.")
        threading.Thread(target=self._exec_worker, args=(d, qty), daemon=True).start()

    def _exec_worker(self, d, qty):
        try:
            pos = self.broker.open_trade(EXCHANGE, d.symbol, d.direction, qty, d.sl, d.tp)
            self.say("Paper trade opened, boss. %s %d of %s. Risk %s." % (
                d.direction, qty, d.symbol, inr(qty * abs(d.entry - d.sl))))
            self.decision = None
        except BrokerError as exc:
            self.log("Order refused: %s" % exc)
        except Exception as exc:
            self.log("Order error: %s" % exc)

    def _tick(self):
        threading.Thread(target=self._tick_worker, daemon=True).start()

    def _tick_worker(self):
        try:
            for t in self.broker.check_exits():
                pnl = float(t.get("pnl", 0.0))
                self.risk.record_trade_result(pnl, t.get("symbol", ""))
                self.mentor.record(pnl)
                self.say("%s closed by %s. %s %s." % (t["symbol"], t.get("reason", "exit"),
                                                      "Profit" if pnl >= 0 else "Loss", inr(abs(pnl))))
            Clock.schedule_once(lambda dt: self.refresh_mentor(), 0)
        except Exception as exc:
            self.log("Monitor error: %s" % exc)

    def refresh_mentor(self):
        try:
            p = self.mentor.plan()
            r = self.risk.status()
            self.m_working.text = "Working  %s" % inr(p["working"])
            self.m_banked.text = "Banked  %s   |   green streak %d" % (inr(p["banked"]), p["streak_green"])
            self.m_day.text = "Today %s / target %s   |   max loss/trade %s" % (
                inr(p["day_pnl"]), inr(p["target"]), inr(p["max_loss_per_trade"]))
            self.m_advice.text = ("LOCKED: %s" % r["reason"]) if r["locked"] else p["advice"]
            self.mode_lbl.text = "KILLED" if self.killed else "PAPER"
            self.mode_lbl.color = RED if self.killed else GREEN
        except Exception as exc:
            self.log("Refresh error: %s" % exc)

    # -- kill switch --
    def kill(self):
        self.killed = True
        try:
            with open(os.path.join(self.data_dir, "kill.flag"), "w") as fh:
                fh.write(str(time.time()))
        except OSError:
            pass

        def close():
            try:
                self.broker.close_all()
            except Exception as exc:
                self.log("Close-all issue: %s" % exc)
        threading.Thread(target=close, daemon=True).start()
        self.refresh_mentor()
        self.say("Kill switch on. All paper positions closed. Say rearm when you are calm.")

    def rearm(self):
        self.killed = False
        try:
            os.remove(os.path.join(self.data_dir, "kill.flag"))
        except OSError:
            pass
        self.refresh_mentor()
        self.say("Re-armed. Trading allowed again, within the risk limits.")


if __name__ == "__main__":
    TacticalApp().run()
