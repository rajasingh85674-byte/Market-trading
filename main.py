# -*- coding: utf-8 -*-
"""Tactical Neural Matrix (TDN) - live NSE trading terminal for Android. REAL MONEY.

BUY / SELL send real intraday orders to your Zerodha Kite or Upstox account (after a confirm popup).
Screens (bottom bar): HUD (live chart, mic, order buttons), AGENTS (tap an agent: it speaks and logs what it
sees right now), DEPTH (market depth, ticks, positions), CONFIG (theme, feed, broker keys, voice).

Files that must sit next to this one:
  agents.py  broker.py  risk_manager.py  voice_engine.py  voice_mic.py  ui_hud.py
"""
import json
import os
import threading
import time
import traceback

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.screenmanager import NoTransition, Screen, ScreenManager
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget
from kivy.utils import platform

BOOT_ERROR = ""
try:
    import ui_hud
    from ui_hud import (ANIM, P, THEME_NAMES, THEMES, AgentTile, AutoLabel, HoldButton, HudButton, HudChart,
                        HudFrame, HudNav, MicButton, PnlPanel, StatusGrid, apply_theme, esc, heading, hex_of, inr)
    from agents import AGENT_NAMES, BUY, SELL, ConsensusEngine, TrailingStop, copilot_report
    from broker import (BrokerError, BrokerFeed, CredentialStore, DemoFeed, MarketStream, TickStream,
                        YahooFeed, ist_now, make_broker)
    from risk_manager import RiskManager
    from voice_engine import PROFILES, OpenAIClient, VoiceEngine
    from voice_mic import HELP_TEXT, VoiceMic, parse_command
except Exception:
    BOOT_ERROR = traceback.format_exc()

FEEDS = ("AUTO", "BROKER", "YAHOO", "DEMO")
WATCHLIST = ["SBIN", "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "ITC", "LT", "AXISBANK", "KOTAKBANK"]
REFERENCE_CAPITAL = 100000.0          # only for the size preview while no broker is connected
SCREENS = [("hud", "HUD"), ("agents", "AGENTS"), ("depth", "DEPTH"), ("config", "CONFIG")]


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


def scroll_column(spacing=12):
    """(ScrollView, content BoxLayout whose height follows its children)."""
    sv = ScrollView(do_scroll_x=False, bar_width=dp(3))
    col = BoxLayout(orientation="vertical", spacing=dp(spacing), size_hint_y=None, padding=(0, dp(4)))
    col.bind(minimum_height=col.setter("height"))
    sv.add_widget(col)
    return sv, col


class TDNApp(App):
    title = "Tactical Neural Matrix"

    # ------------------------------------------------------------------ #
    # startup
    # ------------------------------------------------------------------ #
    def build(self):
        Window.clearcolor = P.get("bg", (0.004, 0.004, 0.012, 1))
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
        root.add_widget(AutoLabel(text="[b][color=ff4048]TDN - STARTUP ERROR[/color][/b]"))
        sv = ScrollView(do_scroll_x=False)
        sv.add_widget(AutoLabel(text=esc(text), font_size=sp(11)))
        root.add_widget(sv)
        return root

    def _build_inner(self):
        if BOOT_ERROR:
            return self._error_screen(BOOT_ERROR)
        self.data_dir = self.user_data_dir if platform == "android" else os.path.join(os.path.expanduser("~"), ".tdn")
        os.makedirs(self.data_dir, exist_ok=True)
        self.ui_path = os.path.join(self.data_dir, "ui.json")
        self.ui = {"theme": "TIGER HUD", "feed": "AUTO", "symbol": WATCHLIST[0], "anim": True}
        try:
            with open(self.ui_path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            for k in self.ui:
                if k in saved:
                    self.ui[k] = saved[k]
        except Exception:
            pass
        if self.ui["theme"] not in THEMES:
            self.ui["theme"] = "TIGER HUD"
        if self.ui["symbol"] not in WATCHLIST:
            self.ui["symbol"] = WATCHLIST[0]
        apply_theme(self.ui["theme"])
        Window.clearcolor = P["bg"]
        ANIM.enabled = bool(self.ui["anim"])

        self.symbol = self.ui["symbol"]
        self.decision = None
        self.candles = []
        self.price = 0.0
        self.source = "NONE"
        self.note = ""
        self.funds = None
        self.busy = {"eval": False, "exits": False, "funds": False, "order": False, "panic": False}
        self.last_eval = self.last_exits = self.last_funds = 0.0
        self.last_key = None
        self.last_signal_voice = 0.0
        self.last_trade_text = ""
        self.selected_agent = None
        self.agent_log_lines = []
        self.copilot_on = False
        self.popup = None
        self.screen_name = "hud"
        self.mic_text = "HOLD THE MIC AND SPEAK"

        self.store = CredentialStore(os.path.join(self.data_dir, "credentials.json"))
        self.risk = RiskManager(os.path.join(self.data_dir, "risk_data.json"))
        self.engine = ConsensusEngine(self.risk)
        self.broker = self._make_broker(self.store.get("broker") or "UPSTOX")
        # the OpenAI key lives only in the app's private credentials file, never in the source code
        self.openai = OpenAIClient(lambda: self.store.get("openai_key"))
        self.voice = VoiceEngine(self.data_dir, self.status, self.openai)
        self.mic = VoiceMic(on_state=self._mic_state_cb, on_level=self._mic_level_cb,
                            on_result=self._mic_result_cb, on_error=self._mic_error_cb,
                            openai=self.openai, data_dir=self.data_dir)
        self.mic.prefer = "OPENAI" if self.store.get("mic_engine") == "OPENAI" else "ANDROID"
        self.ticks = TickStream(self.broker)
        self.ticks.set_symbol(self.symbol)
        self.ticks.start()
        self.stream = MarketStream(self.symbol)
        self.stream.attach_ticks(self.ticks)
        self.stream.start()

        self.shell = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6))
        self._build_ui()
        ANIM.start()
        threading.Thread(target=self._feed_worker, daemon=True).start()
        Clock.schedule_interval(self._tick, 1.0)
        Clock.schedule_once(lambda dt: self.voice.say("Tactical Neural Matrix online. Live mode. Capital first, profit second."), 2.0)
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

    # ------------------------------------------------------------------ #
    # data feed
    # ------------------------------------------------------------------ #
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

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        self.shell.clear_widgets()
        self.shell.add_widget(self._header())
        self.sm = ScreenManager(transition=NoTransition())
        for key, builder in (("hud", self._screen_hud), ("agents", self._screen_agents),
                             ("depth", self._screen_depth), ("config", self._screen_config)):
            scr = Screen(name=key)
            scr.add_widget(builder())
            self.sm.add_widget(scr)
        if self.screen_name not in dict(SCREENS):
            self.screen_name = "hud"
        self.sm.current = self.screen_name
        self.shell.add_widget(self.sm)
        self.nav = HudNav(SCREENS, self.goto, self.screen_name)
        self.shell.add_widget(self.nav)
        self.lbl_status = AutoLabel(text="Ready.", font_size=sp(11), color=P["muted"])
        self.shell.add_widget(self.lbl_status)
        self._refresh_all()

    def goto(self, key):
        self.screen_name = key
        self.sm.current = key
        self.nav.select(key)

    def _header(self):
        box = HudFrame(accent=P["a2"], scan=True, padding=(dp(12), dp(8)), spacing=dp(2))
        box.add_widget(AutoLabel(text="[b][color=%s]TACTICAL NEURAL MATRIX[/color][/b]  [color=%s]TDN | LIVE NSE[/color]"
                                 % (P["hx_a2"], P["hx_a1"]), font_size=sp(14)))
        self.grid = StatusGrid(["FEED", "PRICE", "NSE", "SYSTEM", "BROKER", "TICKS"], cols=3)
        box.add_widget(self.grid)
        return box

    # ---- HUD screen ----
    def _screen_hud(self):
        sv, col = scroll_column()
        chart_box = HudFrame(accent=P["a1"], scan=True, padding=dp(8))
        chart_box.add_widget(heading("LIVE 1-MINUTE CHART  |  NSE"))
        self.chart = HudChart(height=250)
        chart_box.add_widget(self.chart)
        self.lbl_chart = AutoLabel(text="", font_size=sp(12), color=P["muted"])
        chart_box.add_widget(self.lbl_chart)
        col.add_widget(chart_box)

        # microphone, centred on the HUD
        mic_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(176))
        self.mic_btn = MicButton(on_press=self._mic_press, on_release=self._mic_release)
        mic_row.add_widget(Widget())
        mic_row.add_widget(self.mic_btn)
        mic_row.add_widget(Widget())
        col.add_widget(mic_row)
        self.lbl_mic = AutoLabel(text=self.mic_text, font_size=sp(12), color=P["muted"], halign="center")
        col.add_widget(self.lbl_mic)
        cmd_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(44), spacing=dp(6))
        self.cmd_input = TextInput(hint_text="or type: scan market, status report ...", multiline=False, font_size=sp(13),
                                   background_color=P["panel"], foreground_color=P["text"], hint_text_color=P["muted"],
                                   cursor_color=P["a1"], padding=(dp(8), dp(12)))
        self.cmd_input.bind(on_text_validate=lambda inst: self._send_typed())
        cmd_row.add_widget(self.cmd_input)
        send = HudButton(text="SEND", accent=P["a1"], height=44, size_hint_x=None, width=dp(80))
        send.bind(on_release=lambda inst: self._send_typed())
        cmd_row.add_widget(send)
        col.add_widget(cmd_row)

        self.pnl = PnlPanel()
        col.add_widget(self.pnl)

        sig = HudFrame(accent=P["a2"])
        sig.add_widget(heading("LIVE SIGNAL", P["a2"]))
        self.lbl_signal = AutoLabel(text="Waiting for data...", font_size=sp(18))
        self.lbl_levels = AutoLabel(text="", font_size=sp(13))
        self.lbl_reason = AutoLabel(text="", font_size=sp(12), color=P["muted"])
        self.lbl_account = AutoLabel(text="", font_size=sp(12), color=P["muted"])
        self.lbl_position = AutoLabel(text="", font_size=sp(12))
        for w in (self.lbl_signal, self.lbl_levels, self.lbl_reason, self.lbl_account, self.lbl_position):
            sig.add_widget(w)
        col.add_widget(sig)

        # realistic terminal buttons
        from kivy.uix.gridlayout import GridLayout
        grid = GridLayout(cols=2, spacing=dp(8), size_hint_y=None, row_default_height=dp(50), row_force_default=True)
        grid.bind(minimum_height=grid.setter("height"))
        self.btn_arm = HudButton(text="DISARM", accent=P["ok"], height=50)
        self.btn_arm.bind(on_release=lambda inst: self.toggle_arm())
        self.btn_trail = HudButton(text="TRAIL SL LOCK: ON", accent=P["a1"], height=50)
        self.btn_trail.bind(on_release=lambda inst: self.toggle_trailing())
        self.btn_buy = HudButton(text="BUY", accent=P["up"], filled=True, height=50)
        self.btn_buy.bind(on_release=lambda inst: self.order_request(BUY))
        self.btn_sell = HudButton(text="SELL", accent=P["down"], filled=True, height=50)
        self.btn_sell.bind(on_release=lambda inst: self.order_request(SELL))
        self.btn_copilot = HudButton(text="AI CO-PILOT HUD", accent=P["a2"], height=50)
        self.btn_copilot.bind(on_release=lambda inst: self.toggle_copilot())
        self.btn_scan = HudButton(text="SCAN NOW", accent=P["a1"], height=50)
        self.btn_scan.bind(on_release=lambda inst: self.scan_now())
        for b in (self.btn_arm, self.btn_trail, self.btn_buy, self.btn_sell, self.btn_copilot, self.btn_scan):
            grid.add_widget(b)
        col.add_widget(grid)

        self.copilot_box = HudFrame(accent=P["a2"], scan=True)
        self.copilot_box.add_widget(heading("AI CO-PILOT HUD", P["a2"]))
        self.lbl_copilot = AutoLabel(text="", font_size=sp(12))
        self.copilot_box.add_widget(self.lbl_copilot)
        self._copilot_parent = col

        self.btn_panic = HoldButton(text="PANIC SQUARE-OFF  (HOLD 1 SECOND)", accent=P["danger"], filled=True,
                                    height=60, on_hold=self.panic)
        col.add_widget(self.btn_panic)
        return sv

    # ---- AGENTS screen ----
    def _screen_agents(self):
        sv, col = scroll_column(spacing=8)
        log = HudFrame(accent=P["a2"], scan=True)
        log.add_widget(heading("LIVE AGENT INSPECTOR  (tap any agent)", P["a2"]))
        self.lbl_agent_log = AutoLabel(text="Tap an agent below. It will speak and show exactly what it is "
                                            "analysing in the live feed right now.", font_size=sp(12))
        log.add_widget(self.lbl_agent_log)
        col.add_widget(log)
        self.lbl_tally = AutoLabel(text="", font_size=sp(13))
        col.add_widget(self.lbl_tally)
        self.tiles = {}
        for name in AGENT_NAMES:
            tile = AgentTile(name)
            tile.bind(on_release=lambda inst, n=name: self.agent_touched(n))
            self.tiles[name] = tile
            col.add_widget(tile)
        return sv

    # ---- DEPTH screen ----
    def _screen_depth(self):
        sv, col = scroll_column()
        box = HudFrame(accent=P["a1"], scan=True)
        box.add_widget(heading("MARKET DEPTH  (top 5)"))
        self.lbl_depth = AutoLabel(text="Waiting for the live feed...", font_size=sp(13))
        box.add_widget(self.lbl_depth)
        col.add_widget(box)
        pos = HudFrame(accent=P["a2"])
        pos.add_widget(heading("POSITIONS AND TRADES", P["a2"]))
        self.lbl_positions = AutoLabel(text="", font_size=sp(12))
        pos.add_widget(self.lbl_positions)
        col.add_widget(pos)
        risk = HudFrame(accent=P["danger"])
        risk.add_widget(heading("RISK CONTROL", P["danger"]))
        self.lbl_risk = AutoLabel(text="", font_size=sp(12))
        risk.add_widget(self.lbl_risk)
        col.add_widget(risk)
        return sv

    # ---- CONFIG screen ----
    def _screen_config(self):
        sv, body = scroll_column(spacing=8)
        store = self.store

        def title(t):
            body.add_widget(heading(t))

        def field(hint, value="", password=False):
            ti = TextInput(text=value, hint_text=hint, multiline=False, password=password, font_size=sp(13),
                           size_hint_y=None, height=dp(44), background_color=P["panel"], foreground_color=P["text"],
                           hint_text_color=P["muted"], cursor_color=P["a1"], padding=(dp(8), dp(12)))
            body.add_widget(ti)
            return ti

        def spinner(values, current, cb):
            s = Spinner(text=current, values=tuple(values), size_hint_y=None, height=dp(44), font_size=sp(13),
                        background_normal="", background_color=P["a1"], color=(0.01, 0.01, 0.02, 1))
            s.bind(text=lambda inst, val: cb(val))
            body.add_widget(s)
            return s

        title("THEME (applies instantly)")
        spinner(THEME_NAMES, self.ui["theme"], self._set_theme)
        anim_btn = HudButton(text="ANIMATIONS: %s" % ("ON" if self.ui["anim"] else "OFF"), accent=P["a2"], height=44)
        anim_btn.bind(on_release=lambda inst: self._toggle_anim(anim_btn))
        body.add_widget(anim_btn)
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
            self.ticks.set_broker(self.broker)
            self.ticks.set_symbol(self.symbol)
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
                    msg = esc(exc)
                    Clock.schedule_once(lambda dt: setattr(lbl_login, "text", msg), 0)
            threading.Thread(target=work, daemon=True).start()

        def disconnect():
            self.broker.clear_session()
            self.funds = None
            lbl_login.text = "Logged out."
            self.reload_feed()

        for text, accent, fn in (("SAVE KEYS", P["a1"], save_keys), ("1. OPEN LOGIN PAGE", P["a2"], show_login),
                                 ("2. CONNECT", P["a1"], connect), ("LOG OUT", P["danger"], disconnect)):
            b = HudButton(text=text, accent=accent, height=46)
            b.bind(on_release=lambda inst, f=fn: f())
            body.add_widget(b)
        body.add_widget(lbl_login)

        title("VOICE PROFILE")
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
        tb = HudButton(text="TEST VOICE", accent=P["a2"], height=46)
        tb.bind(on_release=lambda inst: self.voice.test())
        body.add_widget(tb)
        vb = HudButton(text="VOICE ON / OFF", accent=P["a1"], height=46)
        vb.bind(on_release=lambda inst: self.toggle_voice())
        body.add_widget(vb)
        body.add_widget(AutoLabel(text=esc(self.voice.status()), font_size=sp(12), color=P["muted"]))
        title("OPENAI VOICE AND SPEECH (optional)")
        body.add_widget(AutoLabel(text="Paste your OpenAI key here. It is saved only inside this app on this phone. "
                                       "It enables the natural OpenAI voice profiles and OpenAI speech recognition. "
                                       "Never put the key in the code or on GitHub.", font_size=sp(12), color=P["muted"]))
        saved_key = self.store.get("openai_key")
        lbl_ai = AutoLabel(text=("Key saved (ends with %s)." % esc(saved_key[-4:])) if saved_key else "No key saved.",
                           font_size=sp(12), color=P["muted"])
        f_ai = field("OpenAI API key (sk-...)", "", password=True)

        def save_ai():
            key = f_ai.text.strip()
            if key and not key.startswith("sk-"):
                lbl_ai.text = "That does not look like an OpenAI key (it starts with sk-)."
                return
            self.store.update(openai_key=key)
            f_ai.text = ""
            lbl_ai.text = ("Key saved (ends with %s)." % esc(key[-4:])) if key else "Key removed."

        def test_ai():
            if not self.openai.ready():
                lbl_ai.text = "Save a key first."
                return
            lbl_ai.text = "Testing OpenAI voice ..."

            def work():
                try:
                    audio = self.openai.speech("OpenAI voice check complete.", "onyx")
                    msg = "OpenAI works: received %d bytes of audio." % len(audio)
                except Exception as exc:
                    msg = "OpenAI test failed: %s" % str(exc)[:120]
                Clock.schedule_once(lambda dt: setattr(lbl_ai, "text", esc(msg)), 0)
            threading.Thread(target=work, daemon=True).start()

        def pick_mic(val):
            self.mic.prefer = "OPENAI" if val.startswith("OPENAI") else "ANDROID"
            self.store.update(mic_engine=self.mic.prefer)
            self.status("Microphone engine: %s" % self.mic.prefer)

        for text, accent, fn in (("SAVE OPENAI KEY", P["a1"], save_ai), ("TEST OPENAI", P["a2"], test_ai)):
            b = HudButton(text=text, accent=accent, height=46)
            b.bind(on_release=lambda inst, f=fn: f())
            body.add_widget(b)
        body.add_widget(lbl_ai)
        spinner(("ANDROID (phone speech service)", "OPENAI (record and transcribe)"),
                "OPENAI (record and transcribe)" if self.mic.prefer == "OPENAI" else "ANDROID (phone speech service)",
                pick_mic)
        nb = HudButton(text="NEXT SYMBOL", accent=P["a1"], height=46)
        nb.bind(on_release=lambda inst: self.next_symbol())
        body.add_widget(nb)

        title("SAFETY RULES (fixed)")
        body.add_widget(AutoLabel(text="1% maximum risk per trade. At least 9 of 20 agents must agree, reward:risk at "
                                       "least 1:2, no trade on weak volume or traps. Maximum 2% loss per day. Two losses in "
                                       "a row lock the day. Trailing stop only ever tightens. Open positions are closed at "
                                       "15:15 India time. DISARM and the kill switch lock the backend until you tap ARM.",
                                  font_size=sp(12), color=P["muted"]))
        return sv

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
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

    def tick_info(self):
        try:
            return self.ticks.latest()
        except Exception:
            return {"mode": "OFF", "depth": None, "note": "", "age": None}

    # ------------------------------------------------------------------ #
    # live loop
    # ------------------------------------------------------------------ #
    def _tick(self, dt):
        try:
            candles, price, source, note = self.stream.snapshot()
            now = time.time()
            if candles:
                self.candles, self.price, self.source, self.note = candles, price, source, note
                if now - self.last_eval >= 3.0 and not self.busy["eval"]:
                    self.busy["eval"] = True
                    self.last_eval = now
                    depth = self.tick_info().get("depth")
                    threading.Thread(target=self._eval_worker,
                                     args=(list(candles), self.equity(), self.cash(), self.symbol, depth),
                                     daemon=True).start()
            else:
                self.source, self.note = source, note
            if now - self.last_exits >= 3.0 and not self.busy["exits"] and self.broker.positions():
                self.busy["exits"] = True
                self.last_exits = now
                threading.Thread(target=self._exits_worker, daemon=True).start()
            if now - self.last_funds >= 20.0 and not self.busy["funds"] and self.broker.has_session():
                self.busy["funds"] = True
                self.last_funds = now
                threading.Thread(target=self._funds_worker, daemon=True).start()
            for ev in self.broker.pop_events():
                self.alert(ev, True, False)
            self._refresh_all()
        except Exception as exc:
            self.status("Update error: %s" % str(exc)[:100])

    def _eval_worker(self, candles, equity, cash, symbol, depth):
        try:
            d = self.engine.evaluate(symbol, candles, equity, cash, depth)
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
            self.funds = self.broker.funds()
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
        self.last_funds = 0.0                      # re-read the broker balance on the next tick
        self._refresh_all()

    # ------------------------------------------------------------------ #
    # rendering
    # ------------------------------------------------------------------ #
    def _levels(self):
        out = []
        d = self.decision
        if d is not None and d.approved and d.symbol == self.symbol:
            out = [(d.entry, P["a1"]), (d.sl, P["danger"]), (d.tp, P["ok"])]
        for p in self.broker.positions():
            if p["symbol"] == self.symbol:
                out = [(p["entry"], P["a1"]), (p["sl"], P["danger"]), (p["tp"], P["ok"])]
        return out

    def _refresh_all(self):
        st = self.risk.status()
        ti = self.tick_info()
        if st["killed"]:
            system, scol = "DISARMED", P["danger"]
        elif st["locked"]:
            system, scol = "LOCKED TODAY", P["danger"]
        else:
            system, scol = "ARMED", P["ok"]
        connected = self.broker.has_session()
        mode = ti.get("mode", "OFF")
        age = ti.get("age")
        tick_txt = mode if age is None else "%s %ds" % (mode, int(age))
        self.grid.set("FEED", self.source, P["a1"])
        self.grid.set("PRICE", "%s %s" % (self.symbol, inr(self.price) if self.price else "-"), P["a2"])
        self.grid.set("NSE", "OPEN" if nse_open() else "CLOSED", P["ok"] if nse_open() else P["muted"])
        self.grid.set("SYSTEM", system, scol)
        self.grid.set("BROKER", ("%s LIVE" % self.broker.name) if connected else "%s OFF" % self.broker.name,
                      P["ok"] if connected else P["danger"])
        self.grid.set("TICKS", tick_txt, P["a1"] if mode == "WS" else P["a2"] if mode == "REST" else P["muted"])

        killed = st["killed"]
        self.btn_arm.text = "ARM" if killed else "DISARM"
        self.btn_arm.set_accent(P["danger"] if killed else P["ok"], filled=killed)
        self.btn_trail.text = "TRAIL SL LOCK: %s" % ("ON" if self.broker.trailing else "OFF")
        self.btn_trail.set_accent(P["a1"] if self.broker.trailing else P["muted"], filled=self.broker.trailing)

        if self.candles:
            c = self.candles[-1]
            self.chart.update(self.candles, self._levels())
            self.lbl_chart.text = "O %.2f  H %.2f  L %.2f  C %.2f  |  %d candles%s" % (
                c.o, c.h, c.l, c.c, len(self.candles), ("  |  " + esc(self.note)) if self.note else "")
        else:
            self.lbl_chart.text = "Connecting to market data...  %s" % esc(self.note)
        f = self.funds
        acct = ("Equity %s  |  cash %s" % (inr(f["equity"]), inr(f["cash"]))) if f else \
            "Equity: not connected (preview on %s)" % inr(REFERENCE_CAPITAL)
        self.lbl_account.text = "%s  |  today %s  |  losses in row %d  |  trades today %d" % (
            acct, inr(st["daily_pnl"]), st["consecutive_losses"], st["trades_today"])
        pos = self.broker.positions()
        if pos:
            p = pos[0]
            sign = 1 if p["side"] == BUY else -1
            live = (self.price - p["entry"]) * p["qty"] * sign if p["symbol"] == self.symbol else 0.0
            stage = TrailingStop.stage(p["side"], p["entry"], p.get("sl0", p["sl"]), p["sl"])
            self.lbl_position.text = "[b]OPEN[/b] %s %d %s @ %s  SL %s (%s)  TP %s  P&L %s" % (
                p["side"], p["qty"], esc(p["symbol"]), inr(p["entry"]), inr(p["sl"]), stage, inr(p["tp"]), inr(live))
        else:
            self.lbl_position.text = esc(self.last_trade_text) if self.last_trade_text else "No open position."
        self._refresh_ledger(pos)
        self._refresh_depth(ti, st, pos)
        if self.copilot_on:
            self._refresh_copilot()

    def _refresh_ledger(self, pos):
        """Live balance = broker equity + open P&L. Realized numbers come from the broker's own trade book,
        so they survive restarts and update the moment a trade closes."""
        led = self.broker.ledger()
        open_pnl = 0.0
        for p in pos:
            if p["symbol"] == self.symbol and self.price:
                open_pnl += (self.price - p["entry"]) * p["qty"] * (1 if p["side"] == BUY else -1)
        if self.funds and self.funds.get("equity") is not None:
            balance, live, note = float(self.funds["equity"]) + open_pnl, True, ""
        else:
            balance, live, note = REFERENCE_CAPITAL + led["net"] + open_pnl, False, "start %s" % inr(REFERENCE_CAPITAL)
        self.pnl.set_values(balance, led["profit"], led["loss"], led["net"], led["day_net"], open_pnl,
                            led["trades"], led["wins"], led["losses"], live, note)

    def _refresh_depth(self, ti, st, pos):
        depth = ti.get("depth")
        if depth and depth.get("bids") and depth.get("asks"):
            lines = ["[b]%s[/b]  last %s  |  %s  |  age %s" % (
                esc(self.symbol), inr(ti.get("ltp", self.price)), ti.get("mode"),
                ("%ds" % int(ti["age"])) if ti.get("age") is not None else "-"), ""]
            rows = max(len(depth["bids"]), len(depth["asks"]))
            lines.append("[color=%s][b]BID  qty (orders)[/b][/color]          [color=%s][b]ASK  qty (orders)[/b][/color]"
                         % (P["hx_up"], P["hx_down"]))
            for i in range(min(rows, 5)):
                b = depth["bids"][i] if i < len(depth["bids"]) else None
                a = depth["asks"][i] if i < len(depth["asks"]) else None
                bt = "%.2f  %d (%d)" % (b[0], b[1], b[2]) if b else "-"
                at = "%.2f  %d (%d)" % (a[0], a[1], a[2]) if a else "-"
                lines.append("[color=%s]%s[/color]      [color=%s]%s[/color]" % (P["hx_up"], bt, P["hx_down"], at))
            bq = sum(q for _p, q, _o in depth["bids"])
            aq = sum(q for _p, q, _o in depth["asks"])
            if bq + aq:
                lines.append("")
                lines.append("Buyers %d vs sellers %d  (%.0f%% buyers)" % (bq, aq, 100.0 * bq / (bq + aq)))
            self.lbl_depth.text = "\n".join(lines)
        else:
            self.lbl_depth.text = "No market depth yet. Mode %s. %s" % (ti.get("mode", "OFF"), esc(ti.get("note", "")) or
                                                                         "Connect a broker in CONFIG.")
        ptxt = []
        for p in pos:
            ptxt.append("OPEN %s %d %s entry %s SL %s TP %s" % (p["side"], p["qty"], esc(p["symbol"]), inr(p["entry"]),
                                                              inr(p["sl"]), inr(p["tp"])))
        for t in reversed(self.broker.trades(6)):
            ptxt.append("%s %s %d  %s  %s" % (t["side"], esc(t["symbol"]), t["qty"], inr(t["pnl"]), esc(t["reason"])))
        self.lbl_positions.text = "\n".join(ptxt) or "No positions or trades yet."
        self.lbl_risk.text = ("State: %s\nKill reason: %s\nDaily P&L %s  |  losses in a row %d  |  trades %d  |  "
                              "wins %d / losses %d") % (
            "DISARMED" if st["killed"] else ("LOCKED" if st["locked"] else "ARMED"), esc(st.get("kill_reason") or "-"),
            inr(st["daily_pnl"]), st["consecutive_losses"], st["trades_today"], st["wins"], st["losses"])

    def _show_decision(self, d, speak=True):
        if d.symbol != self.symbol:
            return
        self.decision = d
        for v in d.votes:
            tile = self.tiles.get(v.agent)
            if tile is None:
                continue
            edge = P["up"] if v.signal == BUY else P["down"] if v.signal == SELL else P["muted"]
            if v.veto:
                edge = P["danger"]
            tag = "  [color=%s][b]VETO[/b][/color]" % P["hx_danger"] if v.veto else ""
            tile.set_content("[b]%s[/b]  [color=%s]%s %d%%[/color]%s\n[color=%s]%s[/color]" % (
                esc(v.agent), hex_of(edge), v.signal, int(v.confidence * 100), tag, P["hx_muted"], esc(v.detail)),
                edge, v.agent == self.selected_agent)
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
        if self.selected_agent:
            self._show_agent_log(self.selected_agent, speak=False)
        key = (d.symbol, d.direction, d.approved)
        if speak and d.approved and key != self.last_key and time.time() - self.last_signal_voice > 60:
            self.last_signal_voice = time.time()
            self.voice.say("%s signal on %s. %d of %d agents agree. Tap %s to review the order." % (
                d.direction, d.symbol, d.agree, d.total, d.direction.lower()))
        self.last_key = key

    # ------------------------------------------------------------------ #
    # touch-speaking agents
    # ------------------------------------------------------------------ #
    def _agent_meta(self):
        ti = self.tick_info()
        return {"source": self.source, "tick_mode": ti.get("mode") if ti.get("mode") not in (None, "OFF") else "",
                "depth": ti.get("depth"), "now": time.time()}

    def agent_touched(self, name):
        self.selected_agent = name
        for n, tile in self.tiles.items():
            tile.selected = (n == name)
            tile._draw()
        self._show_agent_log(name, speak=True)

    def _show_agent_log(self, name, speak):
        info = self.engine.explain(name, self._agent_meta())
        self.agent_log_lines = info["lines"]
        col = P["hx_up"] if info["signal"] == BUY else P["hx_down"] if info["signal"] == SELL else P["hx_muted"]
        head = "[b][color=%s]%s  %s  %d%%[/color][/b]%s" % (
            col, esc(name), info["signal"], int(info["confidence"] * 100),
            "  [color=%s][b]VETO[/b][/color]" % P["hx_danger"] if info["veto"] else "")
        self.lbl_agent_log.text = head + "\n" + "\n".join(esc(x) for x in info["lines"])
        if speak:
            self.voice.say(info["spoken"], priority=True)
            self.status("%s: speaking live status" % name)

    # ------------------------------------------------------------------ #
    # microphone
    # ------------------------------------------------------------------ #
    def _mic_press(self):
        self.voice.silence()
        self.mic.press()

    def _mic_release(self):
        self.mic.release()

    def _mic_state_cb(self, name):
        def apply(dt):
            self.mic_btn.set_state(name)
            self.lbl_mic.text = {"idle": "HOLD THE MIC AND SPEAK", "listening": "LISTENING ...",
                                 "processing": "PROCESSING ..."}.get(name, name)
        Clock.schedule_once(apply, 0)

    def _mic_level_cb(self, value):
        Clock.schedule_once(lambda dt: self.mic_btn.set_level(value), 0)

    def _mic_result_cb(self, text):
        Clock.schedule_once(lambda dt: self.handle_command(text), 0)

    def _mic_error_cb(self, message):
        def apply(dt):
            self.mic_btn.set_state("idle")
            self.alert(message, True, False)
        Clock.schedule_once(apply, 0)

    def _send_typed(self):
        text = self.cmd_input.text.strip()
        self.cmd_input.text = ""
        if text:
            self.handle_command(text)

    def handle_command(self, text):
        intent, arg = parse_command(text, AGENT_NAMES)
        self.status("Heard: %s  ->  %s" % (text, intent))
        if intent == "SCAN":
            self.scan_now(speak_result=True)
        elif intent == "STATUS":
            self.status_report()
        elif intent == "KILL":
            self.kill_switch()
        elif intent == "REARM" or intent == "ARM":
            self.arm()
        elif intent == "DISARM":
            self.disarm()
        elif intent == "PANIC":
            self.panic()
        elif intent == "TRAIL":
            self.toggle_trailing()
        elif intent == "COPILOT":
            self.toggle_copilot(force=True)
        elif intent in ("BUY", "SELL"):
            self.order_request(intent)
        elif intent == "AGENT":
            self.goto("agents")
            self.agent_touched(arg)
        elif intent == "VOICE_TEST":
            self.voice.test()
        elif intent == "MUTE":
            self.voice.say("Voice off.", True)
            self.voice.set_enabled(False)
        elif intent == "UNMUTE":
            self.voice.set_enabled(True)
            self.voice.say("Voice on.", True)
        elif intent == "HELP":
            self.alert(HELP_TEXT, True, False)
        else:
            self.alert("I did not understand. " + HELP_TEXT, True, False)

    # ------------------------------------------------------------------ #
    # controls
    # ------------------------------------------------------------------ #
    def scan_now(self, speak_result=False):
        self.last_eval = 0.0
        self.status("Scanning %s with all 20 agents..." % self.symbol)
        if speak_result:
            self.voice.say("Scanning %s." % self.symbol, True)
            Clock.schedule_once(lambda dt: self._speak_scan(), 4.5)

    def _speak_scan(self):
        d = self.decision
        if d is None or d.symbol != self.symbol:
            return self.voice.say("No analysis yet.", True)
        if d.approved:
            self.voice.say("%s setup on %s. %d of %d agents agree. Entry %.2f, stop %.2f, target %.2f." % (
                d.direction, d.symbol, d.agree, d.total, d.entry, d.sl, d.tp), True)
        else:
            self.voice.say("No trade on %s. %s" % (d.symbol, d.reason), True)

    def status_report(self):
        st = self.risk.status()
        pos = self.broker.positions()
        state = "disarmed" if st["killed"] else "locked for today" if st["locked"] else "armed"
        parts = ["System %s." % state, "%s price %.2f from %s." % (self.symbol, self.price, self.source),
                 "NSE is %s." % ("open" if nse_open() else "closed"),
                 "%s." % (("Broker %s connected" % self.broker.name) if self.broker.has_session() else "Broker not connected"),
                 "Today's profit and loss %s." % inr(st["daily_pnl"])]
        if pos:
            p = pos[0]
            parts.append("Open %s %d %s, stop %.2f." % (p["side"], p["qty"], p["symbol"], p["sl"]))
        else:
            parts.append("No open position.")
        self.alert(" ".join(parts), True, True)

    def next_symbol(self):
        i = WATCHLIST.index(self.symbol) if self.symbol in WATCHLIST else -1
        self.symbol = WATCHLIST[(i + 1) % len(WATCHLIST)]
        self.ui["symbol"] = self.symbol
        self._save_ui()
        self.decision = None
        self.last_key = None
        self.candles = []
        self.ticks.set_symbol(self.symbol)
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

    def toggle_trailing(self):
        self.broker.set_trailing(not self.broker.trailing)
        self.alert("Trailing stop lock %s." % ("on" if self.broker.trailing else "off"), True, False)

    def toggle_copilot(self, force=False):
        self.copilot_on = True if force else not self.copilot_on
        box, col = self.copilot_box, self._copilot_parent
        if self.copilot_on:
            if box.parent is None:
                col.add_widget(box, index=1)          # just above the panic button
            self.goto("hud")
            lines, spoken = self._copilot()
            self.lbl_copilot.text = "\n".join(esc(x) for x in lines)
            self.voice.say(spoken, True)
        elif box.parent is not None:
            col.remove_widget(box)

    def _copilot(self):
        return copilot_report(self.decision, self.tick_info().get("depth"), self.broker.positions(),
                              self.risk.status(), self.price, self.source)

    def _refresh_copilot(self):
        lines, _spoken = self._copilot()
        self.lbl_copilot.text = "\n".join(esc(x) for x in lines)

    # ---- arm / disarm / kill ----
    def toggle_arm(self):
        if self.risk.is_killed():
            self.arm()
        else:
            self.disarm()

    def disarm(self):
        self.risk.kill("manual disarm")
        self.alert("Disarmed. New trades are locked until you arm again. Open positions keep their stops.", True, True)

    def arm(self):
        self.risk.rearm()
        if self.risk.is_locked():
            self.alert("Armed, but trading stays locked today after the loss limit.", True, True)
        else:
            self.alert("Armed. Live trading allowed again, inside the 1 percent rule.", True, True)

    def kill_switch(self):
        """Lock the backend first, then close everything (same as panic)."""
        self.panic()

    def panic(self):
        if self.busy["panic"]:
            return
        self.busy["panic"] = True
        self.risk.kill("panic square-off")                 # backend lock first, instantly
        self.alert("Panic square off. Trading is locked. Closing every open position now.", True, True)
        threading.Thread(target=self._panic_worker, daemon=True).start()

    def _panic_worker(self):
        try:
            if not self.broker.has_session():
                self.alert("Trading is locked. No broker session, so there is nothing to close from here. "
                           "Check your broker app.", True, True)
                return
            closed, errors, extra = self.broker.square_off_all("PANIC SQUARE-OFF")
            for t in closed:
                self.risk.record_result(t["pnl"])
            if closed:
                Clock.schedule_once(lambda dt: self._after_close(closed), 0)
            if errors:
                self.alert("Could not close everything: %s. CHECK YOUR BROKER APP NOW." % "; ".join(errors)[:160], True, True)
            else:
                self.alert("Square off complete. %d tracked and %d other positions closed. Tap arm to trade again." % (
                    len(closed), extra), True, True)
        except Exception as exc:
            self.alert("Square off error: %s. CHECK YOUR BROKER APP." % str(exc)[:100], True, True)
        finally:
            self.busy["panic"] = False

    # ---- live orders ----
    def _block_reason(self, side):
        d = self.decision
        if self.risk.is_killed():
            return "System is disarmed. Tap ARM first."
        if not self.broker.has_session():
            return "Not logged in to %s today. Open CONFIG and connect." % self.broker.name
        if self.source in ("DEMO", "NONE"):
            return "Live orders are blocked while the screen shows simulated DEMO prices."
        if not nse_open():
            return "NSE is closed. Orders are possible only 09:15 to 15:30 India time."
        if d is None or not d.approved or d.symbol != self.symbol:
            return "There is no approved setup right now. Patience."
        if d.direction != side:
            return "The agents approve %s, not %s. No order." % (d.direction, side)
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

    def order_request(self, side):
        side = str(side).upper()
        if side not in (BUY, SELL):
            return
        why = self._block_reason(side)
        if why:
            return self.alert(why)
        d = self.decision
        qty = self.risk.position_size(self.equity(), self.price, d.sl, self.cash())
        if qty < 1:
            return self.alert("Capital is too small for a 1 percent risk on this setup.")
        risk_amount = qty * abs(self.price - d.sl)
        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        text = ("[b][color=%s]REAL MONEY ORDER[/color][/b]\n%s %s  about %d shares at %s\nStop-loss %s   Target %s\n"
                "Money at risk %s (1%% cap)\nBroker: %s  |  Intraday  |  SL-M stop placed right after the fill\n\n"
                "The order is sent immediately and cannot be undone except by closing the position.") % (
            P["hx_danger"], d.direction, esc(d.symbol), qty, inr(self.price), inr(d.sl), inr(d.tp),
            inr(risk_amount), self.broker.name)
        content.add_widget(AutoLabel(text=text, font_size=sp(14)))
        row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(52), spacing=dp(8))
        pop = Popup(title="CONFIRM LIVE ORDER", content=content, size_hint=(0.94, None), height=dp(450),
                    auto_dismiss=False, background="", background_color=P["bg"], separator_color=P["a2"])
        yes = HudButton(text="CONFIRM " + side, accent=P["a2"], filled=True, height=52)
        yes.bind(on_release=lambda inst: (pop.dismiss(), self._start_order(d)))
        no = HudButton(text="CANCEL", accent=P["a1"], height=52)
        no.bind(on_release=lambda inst: pop.dismiss())
        row.add_widget(yes)
        row.add_widget(no)
        content.add_widget(row)
        pop.open()
        Clock.schedule_once(lambda dt: pop.dismiss(), 45)
        self.voice.say("%s order ready. Confirm on screen." % side.lower(), True)

    def _start_order(self, d):
        if time.time() - d.ts > 60:
            return self.alert("The signal expired while you were reading. Scan again.")
        now_d = self.decision
        if now_d is None or not now_d.approved or now_d.direction != d.direction or now_d.symbol != d.symbol:
            return self.alert("The signal changed while you were reading. Order cancelled.")
        why = self._block_reason(d.direction)
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

    # ------------------------------------------------------------------ #
    # settings actions
    # ------------------------------------------------------------------ #
    def _set_theme(self, name):
        if name == self.ui["theme"] or name not in THEMES:
            return
        self.ui["theme"] = name
        self._save_ui()
        apply_theme(name)
        Window.clearcolor = P["bg"]
        self.copilot_on = False
        Clock.schedule_once(lambda dt: self._rebuild(), 0)

    def _rebuild(self):
        self._build_ui()
        if self.decision is not None:
            self._show_decision(self.decision, speak=False)
        self.status("Theme: %s" % self.ui["theme"])

    def _toggle_anim(self, btn):
        self.ui["anim"] = not self.ui["anim"]
        ANIM.enabled = bool(self.ui["anim"])
        btn.text = "ANIMATIONS: %s" % ("ON" if self.ui["anim"] else "OFF")
        self._save_ui()

    def _set_feed(self, name):
        if name == self.ui["feed"] or name not in FEEDS:
            return
        self.ui["feed"] = name
        self._save_ui()
        self.reload_feed()
        self.status("Data feed: %s" % name)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def on_pause(self):
        return True

    def on_resume(self):
        pass

    def on_stop(self):
        for fn in (getattr(self, "stream", None) and self.stream.stop, getattr(self, "ticks", None) and self.ticks.stop,
                   ANIM.stop, getattr(self, "mic", None) and self.mic.shutdown):
            try:
                if fn:
                    fn()
            except Exception:
                pass


if __name__ == "__main__":
    TDNApp().run()
