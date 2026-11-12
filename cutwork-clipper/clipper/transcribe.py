"""Free local transcription with faster-whisper (word-level timestamps)."""
import json
import os
import threading
import wave

_MODELS = {}                 # (size, device) -> WhisperModel, kept so the next video starts at once
_LOCK = threading.Lock()     # one transcription at a time: two at once only make both slower


class TranscribeError(RuntimeError):
    """A problem the user can act on; the message is shown in the page as it is."""


def _load_wav(path):
    """16 kHz mono 16-bit WAV (made by ffmpeg) -> float32 samples. Reading it here means the speech
    library never has to decode a media file itself, which is where its installs most often break."""
    import numpy as np
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise TranscribeError("Internal error: the prepared audio is not 16 kHz mono.")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0


def _model(size, device):
    key = (size, device)
    if key not in _MODELS:
        from faster_whisper import WhisperModel
        try:
            _MODELS[key] = WhisperModel(size, device=device, compute_type="int8")
        except Exception as e:
            low = (type(e).__name__ + " " + str(e)).lower()
            if device == "cpu" and any(k in low for k in ("connect", "internet", "resolve", "timed out", "offline",
                                                         "localentrynotfound", "snapshot", "huggingface")):
                raise TranscribeError("The speech model could not be downloaded. Connect to the internet for the "
                                      "first video; after that the app works offline.")
            raise
    return _MODELS[key]


def _run(audio, size, language, device, progress):
    model = _model(size, device)
    segments, info = model.transcribe(audio, language=language or None, word_timestamps=True, vad_filter=True,
                                      condition_on_previous_text=False)
    total = info.duration or (len(audio) / 16000.0) or 1
    words, last = [], 0.0
    for seg in segments:
        for w in seg.words or []:
            text = (w.word or "").strip()
            if not text or w.start is None or w.end is None:
                continue
            s = max(float(w.start), last)                 # keep times in order, never negative length
            e = max(float(w.end), s + 0.02)
            words.append({"s": round(s, 2), "e": round(e, 2), "w": text})
            last = s
        if progress:
            progress(min(99, int(seg.end / total * 100)))
    return {"language": info.language, "duration": float(total), "words": words}


def transcribe(audio_path, model_size="small", language=None, progress=None):
    """Returns {"language", "duration", "words": [{"s", "e", "w"}]}."""
    fake = os.environ.get("CUTWORK_FAKE_TRANSCRIPT")  # for testing only
    if fake:
        with open(fake, encoding="utf-8") as f:
            return json.load(f)
    try:
        import faster_whisper  # noqa: F401
    except Exception as e:
        raise TranscribeError(f"The speech library is not installed correctly ({e}). Run the start script again.")

    audio = _load_wav(audio_path)
    with _LOCK:
        device = os.environ.get("CUTWORK_DEVICE", "auto")
        if device != "cpu":
            try:
                return _run(audio, model_size, language, device, progress)
            except TranscribeError:
                raise
            except Exception as e:   # a graphics card without its libraries fails here; the processor always works
                print(f"[transcribe] {device} failed ({type(e).__name__}: {e}); using the processor instead", flush=True)
                _MODELS.pop((model_size, device), None)
        try:
            return _run(audio, model_size, language, "cpu", progress)
        except TranscribeError:
            raise
        except MemoryError:
            raise TranscribeError("This computer ran out of memory while transcribing. Close other programs, "
                                  "or choose the Fast quality.")
        except ValueError as e:
            if language:
                raise TranscribeError(f"'{language}' is not a language code the speech model knows. "
                                      "Use a code such as en, zu or fr, or leave it blank.")
            raise TranscribeError(f"Transcription failed: {e}")
