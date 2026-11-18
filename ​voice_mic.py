# -*- coding: utf-8 -*-
"""voice_mic.py - push-to-talk voice commands.

On Android the phone's own SpeechRecognizer is driven through pyjnius. It must be created and used on the
Android UI thread, so every call to it goes through android.runnable.run_on_ui_thread. Results come back through a
RecognitionListener proxy; they are handed to the Kivy side with the callbacks given to VoiceMic (the caller
must move them onto the Kivy thread, for example with Clock.schedule_once).

parse_command() turns the recognised sentence into an intent and works on any platform (also with typed text).
Intents: SCAN, STATUS, KILL, REARM, ARM, DISARM, PANIC, COPILOT, TRAIL, BUY, SELL, AGENT:<name>, VOICE_TEST,
MUTE, UNMUTE, HELP, UNKNOWN.
"""
import re
import threading

try:
    from kivy.utils import platform as _platform
except Exception:
    _platform = "linux"

# (intent, phrases). The first group that matches wins, so order matters: most specific first.
_INTENTS = [
    ("PANIC", ["panic", "square off", "square-off", "squareoff", "close all", "exit all", "sab band", "sab bech"]),
    ("KILL", ["kill switch", "activate kill", "kill the", "stop trading", "trading band", "kill"]),
    ("REARM", ["re arm", "rearm", "re-arm", "release kill", "unlock trading", "resume trading"]),
    ("DISARM", ["disarm", "dis arm", "safe mode", "weapons down"]),
    ("ARM", ["arm the", "arm system", "arm trading", "go live", "arm"]),
    ("TRAIL", ["trail", "trailing", "lock stop", "stop loss lock", "lock profit", "tsl"]),
    ("COPILOT", ["co pilot", "copilot", "co-pilot", "ai pilot", "advice", "what should i do"]),
    ("SCAN", ["scan", "analyse", "analyze", "market check", "check market", "check the market", "dekho", "market dekho"]),
    ("STATUS", ["status", "report", "how are we", "kaisa chal", "summary", "position"]),
    ("VOICE_TEST", ["voice test", "test voice", "test the voice", "mic check"]),
    ("MUTE", ["mute", "be quiet", "silence", "chup"]),
    ("UNMUTE", ["unmute", "speak again", "voice on"]),
    ("HELP", ["help", "commands", "what can you do"]),
    ("BUY", ["buy", "go long", "long", "kharid"]),
    ("SELL", ["sell", "go short", "short", "becho", "bech"]),
]

# spoken agent names -> agent display names (resolved by the caller through its own list too)
_AGENT_WORDS = {
    "structure": "Market Structure", "sweep": "Liquidity Sweep", "liquidity": "Liquidity Sweep",
    "choch": "Choch", "change of character": "Choch", "trend": "Trend EMA", "ema": "Trend EMA",
    "rsi": "Momentum RSI", "momentum": "Momentum RSI", "volume": "Volume Flow", "flow": "Volume Flow",
    "support": "Support/Resistance", "resistance": "Support/Resistance", "volatility": "Volatility Guard",
    "fakeout": "Fakeout Shield", "trap": "Fakeout Shield",
}


def parse_command(text, agent_names=None):
    """Return (intent, argument). Never raises."""
    t = " ".join(re.sub(r"[^a-z0-9ऀ-ॿ/\- ]", " ", str(text or "").lower()).split())
    if not t:
        return "UNKNOWN", ""
    # An agent query: "what is the RSI agent saying", "explain volume flow", "agent trend"
    if "agent" in t or t.startswith(("explain", "tell me about", "what is", "what does", "inspect")):
        names = list(agent_names or [])
        for name in names:
            if name.lower() in t:
                return "AGENT", name
        for word, name in _AGENT_WORDS.items():
            if word in t and (not names or name in names):
                return "AGENT", name
    for intent, phrases in _INTENTS:
        for p in phrases:
            if re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", t):
                return intent, t
    return "UNKNOWN", t


HELP_TEXT = ("Say: scan market, status report, activate kill switch, re arm, arm, disarm, "
             "buy, sell, trail stop, co pilot, panic square off, or ask about an agent, for example volume flow agent.")


class VoiceMic:
    """Push-to-talk speech recognition.

    on_state(name)      name in 'idle', 'listening', 'processing'
    on_level(0..1)      loudness while the user is speaking
    on_result(text)     final recognised sentence
    on_error(message)   short human readable problem
    All callbacks may arrive on an Android thread; the caller schedules them onto the Kivy thread.
    """

    def __init__(self, on_state=None, on_level=None, on_result=None, on_error=None, language="en-IN"):
        self.on_state = on_state or (lambda s: None)
        self.on_level = on_level or (lambda v: None)
        self.on_result = on_result or (lambda t: None)
        self.on_error = on_error or (lambda m: None)
        self.language = language
        self.available = False
        self.listening = False
        self._rec = None
        self._listener = None
        self._intent = None
        self._lock = threading.RLock()
        self._perm_asked = False
        self._native = None
        if _platform == "android":
            try:
                self._build_native()
            except Exception as exc:
                self.available = False
                self._native = None
                self.on_error("Voice recognition setup failed: %s" % str(exc)[:80])

    # ------------------------------------------------------------------ #
    def _build_native(self):
        from jnius import PythonJavaClass, java_method, autoclass
        from android.runnable import run_on_ui_thread

        outer = self

        class Listener(PythonJavaClass):
            __javainterfaces__ = ["android/speech/RecognitionListener"]
            __javacontext__ = "app"

            @java_method("(Landroid/os/Bundle;)V")
            def onReadyForSpeech(self, params):
                outer.on_state("listening")

            @java_method("()V")
            def onBeginningOfSpeech(self):
                pass

            @java_method("(F)V")
            def onRmsChanged(self, rms):
                try:
                    outer.on_level(max(0.0, min(1.0, (float(rms) + 2.0) / 12.0)))
                except Exception:
                    pass

            @java_method("([B)V")
            def onBufferReceived(self, buf):
                pass

            @java_method("()V")
            def onEndOfSpeech(self):
                outer.on_state("processing")

            @java_method("(I)V")
            def onError(self, code):
                outer._finish()
                outer.on_error(outer._error_text(int(code)))

            @java_method("(Landroid/os/Bundle;)V")
            def onResults(self, results):
                outer._finish()
                try:
                    key = autoclass("android.speech.SpeechRecognizer").RESULTS_RECOGNITION
                    arr = results.getStringArrayList(key)
                    text = str(arr.get(0)) if arr is not None and arr.size() > 0 else ""
                except Exception:
                    text = ""
                if text:
                    outer.on_result(text)
                else:
                    outer.on_error("I did not catch that.")

            @java_method("(Landroid/os/Bundle;)V")
            def onPartialResults(self, partial):
                pass

            @java_method("(ILandroid/os/Bundle;)V")
            def onEvent(self, event_type, params):
                pass

        self._Listener = Listener
        self._autoclass = autoclass

        @run_on_ui_thread
        def _create():
            try:
                activity = autoclass("org.kivy.android.PythonActivity").mActivity
                SR = autoclass("android.speech.SpeechRecognizer")
                # On Android 11+ isRecognitionAvailable() can report False for apps without package-visibility
                # entries even though recognition works, so it is only a hint and never blocks the attempt.
                try:
                    hint_ok = bool(SR.isRecognitionAvailable(activity))
                except Exception:
                    hint_ok = True
                self._rec = SR.createSpeechRecognizer(activity)
                self._listener = Listener()
                self._rec.setRecognitionListener(self._listener)
                Intent = autoclass("android.content.Intent")
                RS = autoclass("android.speech.RecognizerIntent")
                intent = Intent(RS.ACTION_RECOGNIZE_SPEECH)
                intent.putExtra(RS.EXTRA_LANGUAGE_MODEL, RS.LANGUAGE_MODEL_FREE_FORM)
                intent.putExtra(RS.EXTRA_LANGUAGE, self.language)
                intent.putExtra(RS.EXTRA_PARTIAL_RESULTS, False)
                intent.putExtra(RS.EXTRA_MAX_RESULTS, 3)
                self._intent = intent
                self.available = True
                if not hint_ok:
                    self.on_error("Speech service not reported by Android. If the mic fails, install or enable the Google app.")
            except Exception as exc:
                self.available = False
                self.on_error("Speech recognizer error: %s" % str(exc)[:80])

        @run_on_ui_thread
        def _start():
            try:
                self._rec.startListening(self._intent)
            except Exception as exc:
                self._finish()
                self.on_error("Could not start listening: %s" % str(exc)[:80])

        @run_on_ui_thread
        def _stop():
            try:
                self._rec.stopListening()
            except Exception:
                pass

        @run_on_ui_thread
        def _cancel():
            try:
                self._rec.cancel()
            except Exception:
                pass

        self._native = {"create": _create, "start": _start, "stop": _stop, "cancel": _cancel}
        _create()

    @staticmethod
    def _error_text(code):
        return {
            1: "Network timeout. Voice recognition needs internet on most phones.",
            2: "Network error. Check your connection.",
            3: "Audio recording problem.",
            4: "Speech service error.",
            5: "Voice client error.",
            6: "I did not hear anything.",
            7: "I did not catch that.",
            8: "Recognizer is busy. Try again.",
            9: "Microphone permission is missing. Allow it in Android settings.",
        }.get(code, "Voice recognition error %d." % code)

    def _finish(self):
        with self._lock:
            self.listening = False
        self.on_state("idle")
        self.on_level(0.0)

    # ------------------------------------------------------------------ #
    def has_permission(self):
        if _platform != "android":
            return True
        try:
            from android.permissions import check_permission, Permission
            return bool(check_permission(Permission.RECORD_AUDIO))
        except Exception:
            return False

    def ask_permission(self, then=None):
        if _platform != "android":
            if then:
                then()
            return
        try:
            from android.permissions import request_permissions, Permission

            def _cb(perms, grants):
                if then and all(grants):
                    then()
                elif not all(grants):
                    self.on_error("Microphone permission was denied.")

            request_permissions([Permission.RECORD_AUDIO], _cb)
        except Exception as exc:
            self.on_error("Permission request failed: %s" % str(exc)[:80])

    def press(self):
        """Button went down: start listening."""
        if _platform != "android" or self._native is None:
            self.on_error("Voice recognition works on the Android phone. Type a command in the box instead.")
            return
        if not self.has_permission():
            self.ask_permission(then=None)
            self.on_error("Allow the microphone, then hold the button again.")
            return
        if not self.available:
            self._native["create"]()
            self.on_error("Voice recognizer is starting. Hold the button again.")
            return
        with self._lock:
            if self.listening:
                return
            self.listening = True
        self.on_state("listening")
        self._native["start"]()

    def release(self):
        """Button released: finish and recognise."""
        if _platform != "android" or self._native is None:
            return
        with self._lock:
            was = self.listening
        if was:
            self.on_state("processing")
            self._native["stop"]()

    def cancel(self):
        if self._native is not None:
            self._native["cancel"]()
        self._finish()

    def shutdown(self):
        if self._native is not None and self._rec is not None:
            try:
                from android.runnable import run_on_ui_thread

                @run_on_ui_thread
                def _destroy():
                    try:
                        self._rec.destroy()
                    except Exception:
                        pass
                _destroy()
            except Exception:
                pass
