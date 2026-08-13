"""OracleAI voice input service (server-side STT) — voice_service.py

Adapted from omni_voice_engine_v4 for IN-APP use. It does SPEECH-TO-TEXT only and
hands the text to the NORMAL chat pipeline (the UI drops it into the composer and
sends it through /ws/chat), so voice goes through the REAL Sage — same models,
memory, transcript, streaming — instead of a separate parallel brain.

PRIVACY (by design — nothing is collected):
  * Audio is captured into an in-memory buffer, transcribed, then discarded when
    the call returns. Nothing is written to disk — no recordings, no transcript
    log files (the original engine's plaintext voice_engine.log is gone).
  * The recognized text is treated exactly like typed input: it only enters the
    normal chat, which already rides the Fernet-encrypted memory chain. This
    module persists nothing itself.

BROAD COMPATIBILITY (degrade, never crash):
  * STT: Whisper (CUDA GPU if present, else CPU) with a PocketSphinx fallback for
    legacy / low-power machines. If neither is installed, status() reports it and
    the endpoints return a clear "install X" message instead of failing.
  * Heavy imports (whisper / torch / sounddevice / pocketsphinx) are LAZY, so the
    app boots fine with none of them installed; you only pay for what you use.
"""
from __future__ import annotations

import importlib.util
import queue
import re
import threading
import time
from typing import Optional


def _installed(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def probe_capabilities() -> dict:
    """What's available, WITHOUT importing the heavy libraries."""
    import importlib
    importlib.invalidate_caches()    # so freshly pip-installed deps are seen w/o restart
    caps = {
        "whisper":      _installed("whisper"),
        "torch":        _installed("torch"),
        "sounddevice":  _installed("sounddevice"),
        "pyaudio":      _installed("pyaudio"),
        "pocketsphinx": _installed("pocketsphinx"),
        "webrtcvad":    _installed("webrtcvad"),
        "cuda":         False,
    }
    if caps["torch"]:
        try:
            import torch
            caps["cuda"] = bool(torch.cuda.is_available())
        except Exception:
            caps["cuda"] = False
    caps["can_record"]     = caps["sounddevice"] or caps["pyaudio"]
    caps["can_transcribe"] = caps["whisper"] or caps["pocketsphinx"]
    return caps


# ---------------------------------------------------------------------------
# STT engines (lazy; audio stays in memory and is discarded after transcribe)
# ---------------------------------------------------------------------------
class _WhisperSTT:
    SAMPLE_RATE = 16_000

    def __init__(self, model_name: str = "base", language: str = "en", vad_aggressiveness: int = 1):
        import whisper                                   # lazy; raises if absent
        try:
            import torch
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            self.device = "cpu"
        self.model = whisper.load_model(model_name, device=self.device)
        self.language = (language or "en").lower()
        if _installed("sounddevice"):
            self._backend = "sounddevice"
        elif _installed("pyaudio"):
            self._backend = "pyaudio"
        else:
            raise RuntimeError("no audio capture backend (pip install sounddevice)")
        # 0–3; lower = more sensitive (keeps soft speech / trailing words).
        self._vad_aggr = max(0, min(int(vad_aggressiveness), 3))

    def _record(self, seconds: float):
        import numpy as np
        if self._backend == "sounddevice":
            import sounddevice as sd
            audio = sd.rec(int(seconds * self.SAMPLE_RATE),
                           samplerate=self.SAMPLE_RATE, channels=1, dtype="float32")
            sd.wait()
            return audio.flatten()
        import pyaudio
        pa = pyaudio.PyAudio()
        stream = pa.open(format=pyaudio.paFloat32, channels=1, rate=self.SAMPLE_RATE,
                         input=True, frames_per_buffer=1024)
        frames = []
        try:
            for _ in range(int(self.SAMPLE_RATE / 1024 * seconds)):
                frames.append(stream.read(1024, exception_on_overflow=False))
        finally:
            stream.stop_stream(); stream.close(); pa.terminate()
        return np.frombuffer(b"".join(frames), dtype=np.float32)

    def transcribe(self, seconds: float) -> Optional[str]:
        audio = self._record(seconds)
        if audio is None or len(audio) == 0:
            return None
        if not self._has_speech(audio):                  # silence / background-noise gate
            return None
        return self._whisper(audio)

    def _whisper(self, audio) -> Optional[str]:
        kwargs = {
            "fp16": self.device == "cuda",
            # Avoid chopping mid-phrase; condition on previous text off for short cmds.
            "condition_on_previous_text": False,
            "no_speech_threshold": 0.5,
            "logprob_threshold": -1.0,
            "compression_ratio_threshold": 2.6,
        }
        if self.language:
            kwargs["language"] = self.language
        result = self.model.transcribe(audio, **kwargs)
        del audio
        text = (result.get("text") or "").strip()
        return text or None

    # --- VAD / silence gating ------------------------------------------------
    def _has_speech(self, audio) -> bool:
        """True if the clip holds speech above ambient — webrtcvad if installed,
        else an adaptive energy gate. Keeps silence/room noise out of Whisper."""
        import numpy as np
        if audio is None or len(audio) == 0:
            return False
        frame = int(self.SAMPLE_RATE * 0.03)             # 30 ms
        if _installed("webrtcvad"):
            try:
                import webrtcvad
                vad = webrtcvad.Vad(self._vad_aggr)
                pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
                voiced = 0
                for i in range(0, len(pcm) - frame, frame):
                    if vad.is_speech(pcm[i:i + frame].tobytes(), self.SAMPLE_RATE):
                        voiced += 1
                        if voiced >= 3:                  # ~90 ms of speech
                            return True
                return False
            except Exception:
                pass
        rms = [float(np.sqrt(np.mean(audio[i:i + frame] ** 2)) + 1e-9)
               for i in range(0, len(audio) - frame, frame)]
        if not rms:
            return False
        floor = sorted(rms)[len(rms) // 5]               # ~20th percentile = ambient
        return max(rms) > max(floor * 3.0, 0.012)

    def _record_voiced(self, max_seconds: float, start_timeout: float, hangover_ms: int,
                       min_speech_ms: int = 400, pad_ms: int = 250):
        """Stream 30 ms frames and stop when the speaker pauses (VAD endpointing).

        Accessibility-minded defaults:
          * hangover_ms — silence after speech before we treat the utterance as done
            (short values cut mid-sentence; 1.5–2s is friendlier for pauses)
          * min_speech_ms — don't allow hangover to fire until this much voiced
            audio has been heard (avoids ending on a breath / partial word)
          * pad_ms — keep a little audio after last voiced frame so trailing
            consonants aren't clipped before Whisper runs

        Returns the captured float32 speech, or None if nothing was said.
        """
        import numpy as np
        frame = int(self.SAMPLE_RATE * 0.03)
        frame_ms = 30
        vad = None
        if _installed("webrtcvad"):
            try:
                import webrtcvad
                vad = webrtcvad.Vad(self._vad_aggr)
            except Exception:
                vad = None
        noise = {"floor": None}

        def _voiced(fr):
            if vad is not None:
                try:
                    pcm = (np.clip(fr, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
                    return vad.is_speech(pcm, self.SAMPLE_RATE)
                except Exception:
                    pass
            r = float(np.sqrt(np.mean(fr ** 2)) + 1e-9)
            if noise["floor"] is None:
                noise["floor"] = r
            else:
                # Track ambient floor carefully so soft speech still counts as voiced.
                noise["floor"] = 0.95 * noise["floor"] + 0.05 * min(r, noise["floor"] * 1.5)
            return r > max(noise["floor"] * 2.5, 0.008)

        if self._backend == "sounddevice":
            import sounddevice as sd
            stream = sd.InputStream(samplerate=self.SAMPLE_RATE, channels=1,
                                    dtype="float32", blocksize=frame)
            stream.start()
            _read = lambda: stream.read(frame)[0].reshape(-1)
            _close = stream.stop
        else:
            import pyaudio
            pa = pyaudio.PyAudio()
            s = pa.open(format=pyaudio.paFloat32, channels=1, rate=self.SAMPLE_RATE,
                        input=True, frames_per_buffer=frame)
            _read = lambda: np.frombuffer(s.read(frame, exception_on_overflow=False),
                                          dtype=np.float32)
            def _close():
                s.stop_stream(); s.close(); pa.terminate()

        collected, started = [], False
        voiced_ms, trailing_ms, waited_ms, elapsed_ms = 0, 0, 0, 0
        pad_frames = max(0, int(pad_ms / frame_ms))
        try:
            while elapsed_ms < max_seconds * 1000:
                fr = _read()
                if fr is None or len(fr) == 0:
                    break
                elapsed_ms += frame_ms
                is_v = _voiced(fr)
                if not started:
                    waited_ms += frame_ms
                    if is_v:
                        started = True
                        collected.append(fr)
                        voiced_ms += frame_ms
                    elif waited_ms >= start_timeout * 1000:
                        break                            # nobody spoke
                else:
                    collected.append(fr)
                    if is_v:
                        voiced_ms += frame_ms
                        trailing_ms = 0
                    else:
                        trailing_ms += frame_ms
                        # Only end after enough real speech AND a real pause.
                        if voiced_ms >= min_speech_ms and trailing_ms >= hangover_ms:
                            # Keep a short pad of the trailing silence frames so
                            # Whisper sees the end of the last word cleanly.
                            if pad_frames and len(collected) > pad_frames:
                                # already appended unvoiced frames up to hangover;
                                # trim excess beyond pad so we don't feed long silence
                                excess = trailing_ms - pad_ms
                                if excess > 0:
                                    drop = int(excess / frame_ms)
                                    if drop > 0 and drop < len(collected):
                                        collected = collected[:-drop]
                            break
        finally:
            try:
                _close()
            except Exception:
                pass
        # Need a meaningful amount of voiced audio (~3 frames min was too easy
        # to pass with a click; require ~min_speech_ms).
        if not started or voiced_ms < min(min_speech_ms, 90) or not collected:
            return None
        return np.concatenate(collected)

    def transcribe_voiced(self, max_seconds: float = 30.0,
                          start_timeout: float = 8.0, hangover_ms: int = 1600,
                          min_speech_ms: int = 400) -> Optional[str]:
        audio = self._record_voiced(max_seconds, start_timeout, hangover_ms,
                                    min_speech_ms=min_speech_ms)
        if audio is None or len(audio) == 0:
            return None
        return self._whisper(audio)


class _PocketSphinxSTT:
    """Offline, lightweight, streaming — for legacy / no-GPU hardware."""

    def __init__(self, language: str = "en"):
        from pocketsphinx import LiveSpeech              # lazy; raises if absent
        self._LiveSpeech = LiveSpeech
        self.language = (language or "en").lower()

    def transcribe(self, seconds: Optional[float] = None) -> Optional[str]:
        speech = self._LiveSpeech(verbose=False, sampling_rate=16000,
                                  buffer_size=2048, no_search=False, full_utt=False)
        for phrase in speech:
            txt = str(phrase).strip()
            if txt:
                return txt
        return None

    def transcribe_voiced(self, max_seconds: float = 30.0,
                          start_timeout: float = 8.0, hangover_ms: int = 1600,
                          min_speech_ms: int = 400) -> Optional[str]:
        # PocketSphinx LiveSpeech already endpoints phrases; reuse that path.
        return self.transcribe(max_seconds)


def _build_engine(cfg: dict):
    """Return (engine, kind) per cfg + hardware, or raise RuntimeError w/ guidance."""
    want = (cfg.get("stt_engine") or "whisper").lower()
    caps = probe_capabilities()
    if want == "whisper" and caps["whisper"]:
        if not caps["can_record"]:
            raise RuntimeError("audio backend missing — pip install sounddevice")
        return _WhisperSTT(model_name=cfg.get("model", "base"),
                           language=cfg.get("language", "en"),
                           vad_aggressiveness=int(cfg.get("vad_aggressiveness", 1))), "whisper"
    if caps["pocketsphinx"]:                              # legacy fallback
        return _PocketSphinxSTT(language=cfg.get("language", "en")), "pocketsphinx"
    if want == "whisper" and not caps["whisper"]:
        raise RuntimeError("Whisper not installed — pip install openai-whisper sounddevice")
    raise RuntimeError("no STT engine — pip install openai-whisper sounddevice "
                       "(or pocketsphinx for legacy hardware)")


def _strip_wake(text: str, wake: str) -> str:
    """Return text after the wake word (first match), punctuation-stripped edges."""
    if not text:
        return ""
    low = text.lower()
    w = (wake or "").lower().strip()
    if not w or w not in low:
        return text.strip()
    after = low.split(w, 1)[1]
    # Preserve original casing for the command portion by slicing the original.
    idx = low.find(w)
    after_orig = text[idx + len(w):]
    return after_orig.strip(" \t\r\n.,!?:;—-")


# ---------------------------------------------------------------------------
# Service: push-to-talk + opt-in wake word
# ---------------------------------------------------------------------------
class VoiceService:
    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = dict(cfg or {})
        self.wake_word          = (self.cfg.get("wake_word") or "Sage").strip()
        self.record_seconds     = float(self.cfg.get("record_seconds", 12))
        # Fallback only when VAD path is unavailable (legacy fixed window).
        self.wake_chunk_seconds = float(self.cfg.get("wake_chunk_seconds", 4))
        # VAD / silence-gating for endpointed capture. webrtcvad if installed,
        # adaptive energy gate otherwise; any failure falls back to a fixed window.
        self.vad_enabled        = bool(self.cfg.get("vad_enabled", True))
        # Longer defaults so full sentences (and slower speech) aren't cut off.
        self.vad_max_seconds    = float(self.cfg.get("vad_max_seconds", 45))
        self.vad_hangover_ms    = int(self.cfg.get("vad_hangover_ms", 1600))
        self.vad_start_timeout  = float(self.cfg.get("vad_start_timeout", 8))
        self.vad_min_speech_ms  = int(self.cfg.get("vad_min_speech_ms", 400))
        # Wake loop: how long to wait for *any* speech while always-listening.
        # Large on purpose so we don't thrash open/close the mic every few seconds.
        self.wake_listen_timeout = float(self.cfg.get("wake_listen_timeout", 60))
        # If wake+partial command was heard but looks truncated, keep listening.
        self.wake_continue_if_short = bool(self.cfg.get("wake_continue_if_short", True))
        self.wake_short_word_limit  = int(self.cfg.get("wake_short_word_limit", 4))
        self._engine = None
        self._engine_kind = None
        self._engine_err: Optional[str] = None
        self._mic_lock = threading.Lock()
        self._wake_thread: Optional[threading.Thread] = None
        self._wake_stop = threading.Event()
        self._commands: "queue.Queue[dict]" = queue.Queue()

    def _get_engine(self):
        if self._engine is None:
            try:
                self._engine, self._engine_kind = _build_engine(self.cfg)
                self._engine_err = None
            except Exception as exc:
                self._engine_err = str(exc)
                raise
        return self._engine

    def _capture_utterance(self, *, max_seconds: Optional[float] = None,
                           start_timeout: Optional[float] = None,
                           hangover_ms: Optional[int] = None) -> Optional[str]:
        """Record one spoken utterance (VAD preferred, fixed window fallback)."""
        eng = self._get_engine()
        max_s = float(max_seconds if max_seconds is not None else self.vad_max_seconds)
        start_t = float(start_timeout if start_timeout is not None else self.vad_start_timeout)
        hang = int(hangover_ms if hangover_ms is not None else self.vad_hangover_ms)
        if self.vad_enabled and hasattr(eng, "transcribe_voiced"):
            try:
                return eng.transcribe_voiced(
                    max_seconds=max_s,
                    start_timeout=start_t,
                    hangover_ms=hang,
                    min_speech_ms=self.vad_min_speech_ms)
            except Exception:
                pass
        # Fixed-window fallback (wake chunk / record_seconds).
        secs = max(1.0, min(max_s if max_s <= 30 else self.record_seconds, 45.0))
        return eng.transcribe(secs)

    # ---- push-to-talk -------------------------------------------------------
    def transcribe_once(self, seconds=None) -> Optional[str]:
        with self._mic_lock:
            # Preferred: VAD-endpointed capture (records until you pause and trims
            # surrounding noise). Falls back to a fixed window on any problem.
            if seconds is not None:
                return self._capture_utterance(max_seconds=float(seconds))
            return self._capture_utterance()

    # ---- opt-in wake word ---------------------------------------------------
    @property
    def wake_active(self) -> bool:
        return self._wake_thread is not None and self._wake_thread.is_alive()

    def start_wake(self):
        if self.wake_active:
            return
        self._get_engine()                               # surface errors up front
        self._wake_stop.clear()
        self._wake_thread = threading.Thread(target=self._wake_loop, daemon=True,
                                             name="voice-wake")
        self._wake_thread.start()

    def stop_wake(self):
        self._wake_stop.set()
        self._wake_thread = None

    def _looks_truncated(self, cmd: str) -> bool:
        """Heuristic: very short after wake → user likely still speaking / cut off."""
        if not cmd:
            return True
        words = re.findall(r"[a-z0-9']+", cmd.lower())
        if len(words) <= self.wake_short_word_limit:
            return True
        # Trailing conjunctions / prepositions usually mean more is coming.
        if words and words[-1] in {
            "and", "or", "to", "the", "a", "an", "of", "for", "with", "please",
            "open", "go", "then", "my", "into", "on", "in", "at", "from",
        }:
            return True
        return False

    def _wake_loop(self):
        """Always-listen wake word using full-utterance VAD capture.

        Old behaviour recorded fixed 3s chunks, which is why only the first 2–3
        words (or just the wake word) ever made it through. Now each listen is
        endpointed: speak naturally, pause when done, we keep the whole phrase.
        """
        wake = self.wake_word.lower().strip()
        while not self._wake_stop.is_set():
            try:
                # 1) Capture a full utterance while waiting for the wake word.
                with self._mic_lock:
                    heard = self._capture_utterance(
                        max_seconds=self.vad_max_seconds,
                        start_timeout=self.wake_listen_timeout,
                        hangover_ms=self.vad_hangover_ms)
                if self._wake_stop.is_set():
                    break
                if not heard:
                    continue
                low = heard.lower()
                if wake not in low:
                    continue

                cmd = _strip_wake(heard, wake)

                # 2) Wake alone, or a clearly truncated partial → keep listening
                #    with a shorter start timeout so the follow-on command is
                #    captured without a long dead air wait.
                need_more = (not cmd) or (
                    self.wake_continue_if_short and self._looks_truncated(cmd)
                )
                if need_more:
                    with self._mic_lock:
                        more = self._capture_utterance(
                            max_seconds=self.vad_max_seconds,
                            # If we already have a few words, they may still be
                            # mid-breath — wait a bit; if wake-only, wait longer.
                            start_timeout=2.5 if cmd else self.vad_start_timeout,
                            hangover_ms=self.vad_hangover_ms)
                    if more:
                        more = more.strip()
                        # If they repeated the wake word, strip it again.
                        if wake in more.lower():
                            more = _strip_wake(more, wake)
                        cmd = (cmd + " " + more).strip() if cmd else more

                cmd = (cmd or "").strip()
                if cmd:
                    self._commands.put({"text": cmd, "ts": time.time()})
            except Exception:
                time.sleep(0.25)                         # transient -> keep listening

    def poll(self) -> dict:
        out = []
        while True:
            try:
                out.append(self._commands.get_nowait())
            except queue.Empty:
                break
        return {"commands": out, "wake_active": self.wake_active}

    def status(self) -> dict:
        import sys
        return {
            "capabilities": probe_capabilities(),
            "engine_loaded": self._engine is not None,
            "engine": self._engine_kind,
            "engine_error": self._engine_err,
            "wake_active": self.wake_active,
            "wake_word": self.wake_word,
            "vad": {
                "enabled": self.vad_enabled,
                "webrtcvad": _installed("webrtcvad"),
                "max_seconds": self.vad_max_seconds,
                "hangover_ms": self.vad_hangover_ms,
                "start_timeout": self.vad_start_timeout,
                "min_speech_ms": self.vad_min_speech_ms,
            },
            # Which interpreter is actually running the app — install deps into
            # THIS one (the #1 cause of "I installed it but it's not detected").
            "python": sys.executable,
            "python_version": sys.version.split()[0],
        }
