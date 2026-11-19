# -*- coding: utf-8 -*-
"""voice_engine.py - JARVIS voice output with a selectable voice profile.

Speech backends, tried in this order:
  0. OpenAI text-to-speech (only for the "AI" profiles and only when an OpenAI key is saved in CONFIG).
     The key is never written into the source code. If OpenAI fails (no internet, bad key, no credit) the
     sentence is spoken by the next backend and OpenAI is left alone for a minute.
  1. Android's own TextToSpeech through pyjnius (supports profiles: language, pitch, speed, installed voices).
  2. plyer.tts (simple fallback, no profile control).
  3. pyttsx3 on a PC, if it is installed.

OpenAIClient is also used by voice_mic.py to turn a recorded sentence into text.

All speech runs in one background worker thread, so the screen never freezes and the Java text-to-speech
object is only touched from that thread. Every error is caught: a missing voice can never crash the app.
The chosen profile is saved in voice.json inside the app's private folder.
"""
import hashlib
import json
import os
import queue
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
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
    "AI_ONYX": {"label": "OpenAI Onyx - natural, deep (needs key)", "tag": "en-IN", "pitch": 0.80, "rate": 0.95,
                "engine": "openai", "voice": "onyx"},
    "AI_NOVA": {"label": "OpenAI Nova - natural, warm (needs key)", "tag": "en-IN", "pitch": 1.05, "rate": 1.00,
                "engine": "openai", "voice": "nova"},
    "AI_ALLOY": {"label": "OpenAI Alloy - natural, neutral (needs key)", "tag": "en-US", "pitch": 0.95, "rate": 0.95,
                 "engine": "openai", "voice": "alloy"},
    "AI_SHIMMER": {"label": "OpenAI Shimmer - natural, bright (needs key)", "tag": "en-IN", "pitch": 1.15, "rate": 1.05,
                   "engine": "openai", "voice": "shimmer"},
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


# --------------------------------------------------------------------------- #
# OpenAI speech and transcription (standard library only)
# --------------------------------------------------------------------------- #
class OpenAIError(Exception):
    pass


_SSL: Optional[ssl.SSLContext] = None


def _ssl_ctx() -> ssl.SSLContext:
    global _SSL
    if _SSL is None:
        try:
            import certifi
            _SSL = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _SSL = ssl.create_default_context()
    return _SSL


def _post(url: str, headers: Dict[str, str], data: bytes, timeout: float) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read()
        except Exception:
            return exc.code, b""
    except Exception as exc:
        raise OpenAIError("No connection to OpenAI: %s" % str(exc)[:80])


def _api_message(status: int, body: bytes) -> str:
    try:
        text = json.loads(body.decode("utf-8", "replace"))["error"]["message"]
    except Exception:
        text = ""
    hint = {401: "The OpenAI key was rejected. Check it in CONFIG.",
            403: "This OpenAI key is not allowed to use that model.",
            429: "OpenAI says rate limit or no credit left."}.get(status, "OpenAI error %d." % status)
    return (hint + " " + text[:100]).strip()


class OpenAIClient:
    """Text to speech (mp3) and speech to text. key_getter returns the saved key, or "" when there is none."""

    SPEECH_URL = "https://api.openai.com/v1/audio/speech"
    TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"
    SPEECH_MODELS = ("gpt-4o-mini-tts", "tts-1")
    TRANSCRIBE_MODELS = ("gpt-4o-mini-transcribe", "whisper-1")

    def __init__(self, key_getter: Callable[[], str]):
        self._key_getter = key_getter

    def key(self) -> str:
        k = ""
        try:
            k = (self._key_getter() or "").strip()
        except Exception:
            k = ""
        return k or os.environ.get("OPENAI_API_KEY", "").strip()

    def ready(self) -> bool:
        return self.key().startswith("sk-")

    def _auth(self) -> Dict[str, str]:
        key = self.key()
        if not key:
            raise OpenAIError("No OpenAI key saved. Add it in CONFIG.")
        return {"Authorization": "Bearer " + key}

    def speech(self, text: str, voice: str = "onyx") -> bytes:
        """mp3 audio for the sentence."""
        last = "OpenAI speech failed."
        for model in self.SPEECH_MODELS:
            payload = {"model": model, "voice": voice, "input": text, "response_format": "mp3"}
            if model.startswith("gpt-4o"):
                payload["instructions"] = "Calm, confident trading assistant. Clear and natural."
            hdrs = dict(self._auth(), **{"Content-Type": "application/json"})
            status, body = _post(self.SPEECH_URL, hdrs, json.dumps(payload).encode("utf-8"), 20)
            if status == 200 and body:
                return body
            last = _api_message(status, body)
            if status in (401, 403, 429):
                break                      # another model will not help
        raise OpenAIError(last)

    def transcribe(self, path: str, language: str = "en") -> str:
        """Text of a recorded audio file (m4a, mp3, wav, ...)."""
        with open(path, "rb") as fh:
            audio = fh.read()
        if len(audio) < 1500:
            raise OpenAIError("The recording was too short.")
        last = "OpenAI transcription failed."
        for model in self.TRANSCRIBE_MODELS:
            boundary = "----tdn" + uuid.uuid4().hex
            parts = []
            for name, value in (("model", model), ("language", language), ("response_format", "json")):
                if not value:
                    continue
                parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                              % (boundary, name, value)).encode("utf-8"))
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"speech.m4a\"\r\n"
                          "Content-Type: audio/mp4\r\n\r\n" % boundary).encode("utf-8"))
            parts.append(audio)
            parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
            hdrs = dict(self._auth(), **{"Content-Type": "multipart/form-data; boundary=" + boundary})
            status, body = _post(self.TRANSCRIBE_URL, hdrs, b"".join(parts), 30)
            if status == 200:
                try:
                    return str(json.loads(body.decode("utf-8", "replace")).get("text", "")).strip()
                except ValueError:
                    last = "OpenAI sent an unreadable answer."
                    continue
            last = _api_message(status, body)
            if status in (401, 403, 429):
                break
        raise OpenAIError(last)


class VoiceEngine:
    def __init__(self, data_dir: str, log: Optional[Callable[[str], None]] = None,
                 openai: Optional[OpenAIClient] = None):
        self._log = log or (lambda msg: None)
        self.openai = openai
        self._data_dir = data_dir
        self._halt = threading.Event()               # set to cut off a sentence that is being played
        self._ai_pause_until = 0.0                    # after an OpenAI failure, skip it for a minute
        self._ai_cache: Dict[str, bytes] = {}
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
        ai = ""
        if PROFILES[self.profile].get("engine") == "openai":
            ai = " | OpenAI key %s" % ("saved" if (self.openai and self.openai.ready()) else "MISSING")
        return "Voice: %s | profile %s%s" % (self.backend, PROFILES[self.profile]["label"], ai)

    # ------------------------------------------------------------------ #
    # public speaking API (thread safe, never raises)
    # ------------------------------------------------------------------ #
    def say(self, text: str, priority: bool = False) -> None:
        if not self.enabled or not text:
            return
        msg = " ".join(str(text).replace("\u20b9", " rupees ").split())[:280]
        if not msg:
            return
        try:
            if priority:
                self._halt.set()
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
        self._halt.set()
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._cmds.put(("stop", None))

    def _stop_speaking(self) -> None:
        self._halt.set()
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

    def _openai_active(self) -> bool:
        prof = PROFILES[self.profile]
        return (prof.get("engine") == "openai" and self.openai is not None and self.openai.ready()
                and time.time() >= self._ai_pause_until)

    def _speak_openai(self, msg: str) -> bool:
        """True when OpenAI spoke the sentence. Any problem returns False so the phone voice takes over."""
        voice = PROFILES[self.profile].get("voice", "onyx")
        try:
            cache_key = hashlib.sha1((voice + "|" + msg).encode("utf-8")).hexdigest()
            audio = self._ai_cache.get(cache_key)
            if audio is None:
                audio = self.openai.speech(msg, voice)
                if len(self._ai_cache) >= 16:
                    self._ai_cache.pop(next(iter(self._ai_cache)))
                self._ai_cache[cache_key] = audio
            self._halt.clear()
            return self._play_mp3(audio)
        except OpenAIError as exc:
            self._ai_pause_until = time.time() + 60
            self._log("OpenAI voice: %s Using the phone voice for a minute." % exc)
        except Exception as exc:
            self._ai_pause_until = time.time() + 60
            self._log("OpenAI voice playback failed: %s" % str(exc)[:80])
        return False

    def _play_mp3(self, audio: bytes) -> bool:
        if _platform != "android":
            return False
        from jnius import autoclass
        path = os.path.join(self._data_dir, "speech.mp3")
        with open(path, "wb") as fh:
            fh.write(audio)
        MediaPlayer = autoclass("android.media.MediaPlayer")
        player = MediaPlayer()
        try:
            player.setDataSource(path)
            player.prepare()
            player.start()
            began = time.time()
            while player.isPlaying() and not self._halt.is_set() and time.time() - began < 90:
                time.sleep(0.1)
            if player.isPlaying():
                player.stop()
        finally:
            player.release()
        return True

    def _speak(self, msg: str, priority: bool) -> None:
        if self._openai_active() and self._speak_openai(msg):
            return
        if self._tts is not None:
            self._tts.speak(msg, 0 if priority else 1, None, "tdn")
            return
        if self._plyer is not None:
            self._plyer.speak(message=msg)
            return
        if self._pyttsx is not None:
            self._pyttsx.say(msg)
            self._pyttsx.runAndWait()
