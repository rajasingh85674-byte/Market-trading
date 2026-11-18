# -*- coding: utf-8 -*-
"""voice_engine.py - JARVIS voice output with a selectable voice profile.

Speech backends, tried in this order:
  1. Android's own TextToSpeech through pyjnius (supports profiles: language, pitch, speed, installed voices).
  2. plyer.tts (simple fallback, no profile control).
  3. pyttsx3 on a PC, if it is installed.

All speech runs in one background worker thread, so the screen never freezes and the Java text-to-speech
object is only touched from that thread. Every error is caught: a missing voice can never crash the app.
The chosen profile is saved in voice.json inside the app's private folder.
"""
import json
import os
import queue
import threading
from typing import Callable, Dict, List, Optional, Tuple

try:
    from kivy.utils import platform as _platform
except Exception:  # running without Kivy (tests)
    _platform = "linux"

PROFILES: Dict[str, Dict] = {
    "JARVIS": {"label": "JARVIS - Indian English, deep", "tag": "en-IN", "pitch": 0.80, "rate": 0.95},
    "RIYA": {"label": "Riya - Hindi", "tag": "hi-IN", "pitch": 1.05, "rate": 1.00},
    "COMMANDER": {"label": "Commander - British English, deep", "tag": "en-GB", "pitch": 0.75, "rate": 0.90},
    "ATLAS": {"label": "Atlas - US English, calm", "tag": "en-US", "pitch": 0.90, "rate": 0.85},
    "ZARA": {"label": "Zara - Indian English, bright", "tag": "en-IN", "pitch": 1.15, "rate": 1.05},
}
DEFAULT_PROFILE = "JARVIS"
TEST_LINES = {
    "hi-IN": "Namaste boss. Main Jarvis hoon. Aapki awaaz test sahi chal rahi hai.",
}
TEST_LINE_DEFAULT = "Good day, boss. This is JARVIS. Voice check complete. Capital first, profit second."

# Java side of the "text-to-speech is ready" callback. Only built on Android.
_InitListener = None
if _platform == "android":
    try:
        from jnius import PythonJavaClass, java_method

        class _InitListener(PythonJavaClass):  # type: ignore
            __javainterfaces__ = ["android/speech/tts/TextToSpeech$OnInitListener"]
            __javacontext__ = "app"

            def __init__(self, ready_event, result):
                super().__init__()
                self._event = ready_event
                self._result = result

            @java_method("(I)V")
            def onInit(self, status):
                self._result["status"] = int(status)
                self._event.set()
    except Exception:
        _InitListener = None


class VoiceEngine:
    def __init__(self, data_dir: str, log: Optional[Callable[[str], None]] = None):
        self._log = log or (lambda msg: None)
        self._path = os.path.join(data_dir, "voice.json")
        os.makedirs(data_dir, exist_ok=True)
        self.enabled = True
        self.profile = DEFAULT_PROFILE
        self.voice_name = ""                 # a specific installed Android voice, "" = automatic
        self.backend = "starting"
        self._q: "queue.Queue[Tuple[str, bool]]" = queue.Queue(maxsize=4)
        self._cmds: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self._lock = threading.RLock()
        self._tts = None                     # Android TextToSpeech object (worker thread only)
        self._ready = threading.Event()
        self._init_result: Dict[str, int] = {}
        self._listener = None
        self._voices: List[Tuple[str, str]] = []
        self._plyer = None
        self._pyttsx = None
        self._warned = False
        self._load()
        threading.Thread(target=self._worker, daemon=True).start()

    # ------------------------------------------------------------------ #
    # settings
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("profile") in PROFILES:
                self.profile = data["profile"]
            self.voice_name = str(data.get("voice_name", ""))
            self.enabled = bool(data.get("enabled", True))
        except Exception:
            pass

    def _save(self) -> None:
        try:
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump({"profile": self.profile, "voice_name": self.voice_name, "enabled": self.enabled}, fh)
        except OSError:
            pass

    def profiles(self) -> List[Tuple[str, str]]:
        return [(k, v["label"]) for k, v in PROFILES.items()]

    def set_profile(self, key: str) -> None:
        if key in PROFILES:
            self.profile = key
            self.voice_name = ""
            self._save()
            self._cmds.put(("apply", None))

    def set_voice(self, name: str) -> None:
        self.voice_name = name
        self._save()
        self._cmds.put(("apply", None))

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)
        self._save()

    def installed_voices(self) -> List[Tuple[str, str]]:
        """(name, language) of the voices installed on this phone for the current profile language."""
        tag = PROFILES[self.profile]["tag"].split("-")[0].lower()
        return [(n, t) for n, t in self._voices if t.lower().startswith(tag)][:40]

    def status(self) -> str:
        return "Voice: %s | profile %s" % (self.backend, PROFILES[self.profile]["label"])

    # ------------------------------------------------------------------ #
    # public speaking API (thread safe, never raises)
    # ------------------------------------------------------------------ #
    def say(self, text: str, priority: bool = False) -> None:
        if not self.enabled or not text:
            return
        msg = " ".join(str(text).replace("₹", " rupees ").split())[:280]
        if not msg:
            return
        try:
            if priority:
                while not self._q.empty():
                    try:
                        self._q.get_nowait()
                    except queue.Empty:
                        break
            self._q.put_nowait((msg, priority))
        except queue.Full:
            try:
                self._q.get_nowait()
                self._q.put_nowait((msg, priority))
            except Exception:
                pass

    def silence(self) -> None:
        """Stop talking at once and forget queued sentences (used when the microphone opens)."""
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._cmds.put(("stop", None))

    def _stop_speaking(self) -> None:
        try:
            if self._tts is not None:
                self._tts.stop()
        except Exception:
            pass

    def test(self) -> None:
        tag = PROFILES[self.profile]["tag"]
        was = self.enabled
        self.enabled = True
        self.say(TEST_LINES.get(tag, TEST_LINE_DEFAULT), priority=True)
        self.enabled = was

    # ------------------------------------------------------------------ #
    # worker thread
    # ------------------------------------------------------------------ #
    def _worker(self) -> None:
        self._start_backend()
        while True:
            try:
                while True:
                    cmd, _arg = self._cmds.get_nowait()
                    if cmd == "apply":
                        self._apply_profile()
                    elif cmd == "stop":
                        self._stop_speaking()
            except queue.Empty:
                pass
            try:
                msg, priority = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._speak(msg, priority)
            except Exception as exc:
                self._warn("Voice error: %s" % str(exc)[:80])

    def _warn(self, text: str) -> None:
        if not self._warned:
            self._warned = True
            self._log(text)

    def _start_backend(self) -> None:
        if _platform == "android" and _InitListener is not None:
            try:
                from jnius import autoclass
                activity = autoclass("org.kivy.android.PythonActivity").mActivity
                TextToSpeech = autoclass("android.speech.tts.TextToSpeech")
                self._listener = _InitListener(self._ready, self._init_result)
                self._tts = TextToSpeech(activity, self._listener)
                if self._ready.wait(6.0) and self._init_result.get("status") == 0:
                    self.backend = "ANDROID TTS"
                    self._read_voices()
                    self._apply_profile()
                    return
                self._tts = None
                self._log("Android voice engine did not start. Trying the fallback.")
            except Exception as exc:
                self._tts = None
                self._log("Android voice engine error: %s" % str(exc)[:80])
        try:
            from plyer import tts as plyer_tts
            self._plyer = plyer_tts
            self.backend = "PLYER TTS"
            return
        except Exception:
            self._plyer = None
        try:
            import pyttsx3
            self._pyttsx = pyttsx3.init()
            self.backend = "PYTTSX3"
            return
        except Exception:
            self._pyttsx = None
        self.backend = "NONE"
        self._warn("No voice engine available. Text only.")

    def _read_voices(self) -> None:
        try:
            arr = self._tts.getVoices().toArray()
            out = []
            for v in arr:
                try:
                    if v.isNetworkConnectionRequired():
                        continue
                    out.append((str(v.getName()), str(v.getLocale().toLanguageTag())))
                except Exception:
                    continue
            self._voices = sorted(out)
        except Exception:
            self._voices = []

    def _apply_profile(self) -> None:
        """Set language, pitch, speed and (optionally) one installed voice. Worker thread only."""
        if self._tts is None:
            return
        prof = PROFILES[self.profile]
        try:
            from jnius import autoclass
            Locale = autoclass("java.util.Locale")
            res = self._tts.setLanguage(Locale.forLanguageTag(prof["tag"]))
            if res is not None and int(res) < 0:
                self._tts.setLanguage(Locale.getDefault())
                self._warn("%s is not installed on this phone, using the default language." % prof["tag"])
            self._tts.setPitch(float(prof["pitch"]))
            self._tts.setSpeechRate(float(prof["rate"]))
            if self.voice_name:
                for v in self._tts.getVoices().toArray():
                    if str(v.getName()) == self.voice_name:
                        self._tts.setVoice(v)
                        break
        except Exception as exc:
            self._warn("Voice profile could not be applied: %s" % str(exc)[:80])

    def _speak(self, msg: str, priority: bool) -> None:
        if self._tts is not None:
            self._tts.speak(msg, 0 if priority else 1, None, "tdn")
            return
        if self._plyer is not None:
            self._plyer.speak(message=msg)
            return
        if self._pyttsx is not None:
            self._pyttsx.say(msg)
            self._pyttsx.runAndWait()
