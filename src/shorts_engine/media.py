"""ffmpeg/ffprobe helpers."""
from __future__ import annotations

import functools
import json
import shutil
import subprocess
from pathlib import Path

_FULL_BUILDS = ("/opt/homebrew/opt/ffmpeg-full/bin", "/usr/local/opt/ffmpeg-full/bin")


def _has_libass(ffmpeg: str) -> bool:
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return " subtitles " in out


@functools.lru_cache(maxsize=None)
def resolve_ffmpeg(setting: str = "auto") -> tuple[str, str]:
    """Return (ffmpeg, ffprobe) paths. 'auto' picks a build with libass for burned-in captions."""
    if setting and setting != "auto":
        p = Path(setting)
        return str(p), str(p.with_name("ffprobe"))
    candidates = [str(Path(d) / "ffmpeg") for d in _FULL_BUILDS] + [shutil.which("ffmpeg") or ""]
    for c in candidates:
        if c and Path(c).exists() and _has_libass(c):
            return c, str(Path(c).with_name("ffprobe"))
    raise RuntimeError(
        "No ffmpeg with libass found. macOS: `brew install ffmpeg-full`; Ubuntu: `apt install ffmpeg`."
    )


def run(cmd: list[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if proc.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} failed ({proc.returncode}):\n{proc.stderr[-2000:]}")


def probe(path: Path, ffprobe: str) -> dict:
    out = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def duration(path: Path, ffprobe: str) -> float:
    return float(probe(path, ffprobe)["format"]["duration"])


def has_audio(path: Path, ffprobe: str) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe(path, ffprobe).get("streams", []))


@functools.lru_cache(maxsize=None)
def has_filter(ffmpeg: str, name: str) -> bool:
    out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return f" {name} " in out
