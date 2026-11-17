"""Speech-to-text with word timestamps (faster-whisper), cached as JSON."""
from __future__ import annotations

import functools
import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)
SAMPLE_RATE = 16000


@functools.lru_cache(maxsize=2)
def _model(name: str, device: str, compute_type: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError("pip install 'shorts-engine[transcribe]' for transcription") from e
    return WhisperModel(name, device=device, compute_type=compute_type)


def load_audio(media_path: Path, ffmpeg: str):
    """Decode to 16 kHz mono float32 with our ffmpeg (avoids faster-whisper's PyAV version coupling)."""
    import numpy as np

    raw = subprocess.run(
        [ffmpeg, "-nostdin", "-v", "error", "-i", str(media_path), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
         "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def transcribe(media_path: Path, out_json: Path, settings: dict, ffmpeg: str) -> list[dict]:
    """Return [{start, end, text, words: [{start, end, word}]}]."""
    if out_json.exists():
        return json.loads(out_json.read_text())
    model = _model(settings.get("model", "small"), settings.get("device", "auto"),
                   settings.get("compute_type", "int8"))
    log.info("transcribing %s", media_path.name)
    segments, _ = model.transcribe(load_audio(media_path, ffmpeg), word_timestamps=True, vad_filter=True)
    out = [{
        "start": s.start, "end": s.end, "text": s.text.strip(),
        "words": [{"start": w.start, "end": w.end, "word": w.word.strip()} for w in (s.words or [])],
    } for s in segments]
    out_json.write_text(json.dumps(out))
    return out
