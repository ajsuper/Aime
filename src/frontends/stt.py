"""Local speech-to-text via faster-whisper.

Browsers send WAV (16-bit PCM, mono) and the recognizer returns text. The
Whisper model is loaded lazily on the first request and cached under
`<repo>/models/whisper/` by default. Models download automatically the first
time they're used.

Model selection order:
  1. $WHISPER_MODEL (model size name or absolute path)
  2. Persisted selection in models/whisper/.selected
  3. Default: "small.en"

Sizes (English-only variants are faster and more accurate for en):
  tiny.en, base.en, small.en, medium.en, large-v3, large-v3-turbo
"""

import io
import os
import threading
import time
import wave


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_MODEL_DIR = os.path.join(_REPO_ROOT, "models", "whisper")
_SELECTION_FILE = os.path.join(_DEFAULT_MODEL_DIR, ".selected")
_DEFAULT_MODEL = "small.en"

# Sizes we expose in the UI. faster-whisper resolves these to Hugging Face
# repos and downloads on first use.
_AVAILABLE_MODELS = [
    "tiny.en",
    "base.en",
    "small.en",
    "medium.en",
    "large-v3",
    "large-v3-turbo",
]

# Hugging Face repo each size resolves to. Used to detect which sizes are
# already cached on disk so the UI only offers downloaded models.
_MODEL_REPOS = {
    "tiny.en": "Systran/faster-whisper-tiny.en",
    "base.en": "Systran/faster-whisper-base.en",
    "small.en": "Systran/faster-whisper-small.en",
    "medium.en": "Systran/faster-whisper-medium.en",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}


def _is_downloaded(name: str) -> bool:
    repo = _MODEL_REPOS.get(name)
    if not repo:
        return False
    # huggingface_hub caches a repo "org/name" as "models--org--name/".
    dirname = "models--" + repo.replace("/", "--")
    return os.path.isdir(os.path.join(_DEFAULT_MODEL_DIR, dirname))

_model_lock = threading.Lock()
_model = None  # type: ignore[var-annotated]
_model_name_loaded: str | None = None


class STTError(RuntimeError):
    pass


def _read_selection() -> str | None:
    try:
        with open(_SELECTION_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def list_models() -> dict:
    """Return available model sizes plus selection state.

    Only models that are already cached on disk are returned, so the UI
    dropdown never offers a size that hasn't been downloaded yet.
    """
    selected = _read_selection()
    env = os.environ.get("WHISPER_MODEL")
    downloaded = [name for name in _AVAILABLE_MODELS if _is_downloaded(name)]
    return {
        "models": downloaded,
        "selected": selected,
        "env_override": env or None,
    }


def set_selected_model(name: str) -> str:
    """Persist which model size to use. Returns the stored name."""
    if not isinstance(name, str) or not name:
        raise STTError("model name required")
    # Allow either a known size or an absolute path on disk.
    if name not in _AVAILABLE_MODELS and not os.path.isabs(name):
        raise STTError(f"unknown model: {name!r}")
    os.makedirs(_DEFAULT_MODEL_DIR, exist_ok=True)
    with open(_SELECTION_FILE, "w") as f:
        f.write(name)
    _reset_cached_model()
    return name


def _reset_cached_model() -> None:
    global _model, _model_name_loaded
    with _model_lock:
        _model = None
        _model_name_loaded = None


def _resolve_model_name() -> str:
    env = os.environ.get("WHISPER_MODEL")
    if env:
        return env
    sel = _read_selection()
    if sel:
        return sel
    return _DEFAULT_MODEL


def _get_model():
    global _model, _model_name_loaded
    name = _resolve_model_name()
    if _model is not None and _model_name_loaded == name:
        return _model
    with _model_lock:
        if _model is not None and _model_name_loaded == name:
            return _model
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as e:
            raise STTError("faster-whisper is not installed; pip install faster-whisper") from e
        os.makedirs(_DEFAULT_MODEL_DIR, exist_ok=True)
        # CPU int8 is a good default — fast, no GPU required, minimal accuracy loss.
        # Users with a CUDA GPU can set WHISPER_DEVICE=cuda and WHISPER_COMPUTE=float16.
        device = os.environ.get("WHISPER_DEVICE", "cpu")
        compute_type = os.environ.get("WHISPER_COMPUTE", "int8" if device == "cpu" else "float16")
        # By default use every logical CPU. CTranslate2 otherwise picks a
        # conservative thread count that leaves performance on the table.
        cpu_threads = int(os.environ.get("WHISPER_CPU_THREADS", os.cpu_count() or 4))
        num_workers = int(os.environ.get("WHISPER_NUM_WORKERS", "1"))
        try:
            _model = WhisperModel(
                name,
                device=device,
                compute_type=compute_type,
                download_root=_DEFAULT_MODEL_DIR,
                cpu_threads=cpu_threads,
                num_workers=num_workers,
            )
        except Exception as e:
            raise STTError(f"failed to load whisper model {name!r}: {e}") from e
        _model_name_loaded = name
        return _model


def transcribe_wav(wav_bytes: bytes, user: str | None = None) -> str:
    """Transcribe a complete WAV blob. Mono 16-bit PCM at any common rate.

    `user` is the username to attribute this transcription to in the opt-in
    usage log; pass None to leave it unattributed.
    """
    # Validate the WAV header up front so we return a clean error instead of
    # whatever faster-whisper's ffmpeg layer would emit.
    try:
        wf = wave.open(io.BytesIO(wav_bytes), "rb")
    except wave.Error as e:
        raise STTError(f"invalid WAV: {e}") from e
    try:
        if wf.getnchannels() != 1:
            raise STTError("audio must be mono")
        if wf.getsampwidth() != 2:
            raise STTError("audio must be 16-bit PCM")
        # Audio duration, captured here while the header is open so the usage
        # log can record how much audio each user transcribes.
        rate = wf.getframerate() or 0
        audio_seconds = (wf.getnframes() / rate) if rate else 0.0
    finally:
        wf.close()

    model = _get_model()
    started = time.monotonic()
    try:
        beam_size = int(os.environ.get("WHISPER_BEAM_SIZE", "1"))
        segments, _info = model.transcribe(
            io.BytesIO(wav_bytes),
            beam_size=beam_size,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
        )
        pieces = [seg.text.strip() for seg in segments]
    except Exception as e:
        raise STTError(f"transcription failed: {e}") from e

    # Best-effort: record local compute spent. Never let a stats failure
    # affect the transcription result.
    try:
        import aime.usage as _usage
        _usage.record_stt(
            user,
            _resolve_model_name(),
            audio_seconds,
            (time.monotonic() - started) * 1000.0,
        )
    except Exception:
        pass

    return " ".join(p for p in pieces if p).strip()
