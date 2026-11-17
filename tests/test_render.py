"""End-to-end render check against a real ffmpeg build (skipped if none with libass is installed)."""
import subprocess

import pytest

from shorts_engine import media
from shorts_engine.captions import proportional_words
from shorts_engine.render import Renderer

try:
    FFMPEG, FFPROBE = media.resolve_ffmpeg("auto")
except RuntimeError:
    pytest.skip("no ffmpeg with libass", allow_module_level=True)


def _make(path, args):
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", *args, str(path)], check=True)


def _dims(path):
    s = next(s for s in media.probe(path, FFPROBE)["streams"] if s["codec_type"] == "video")
    return s["width"], s["height"]


@pytest.fixture
def renderer(tmp_path):
    (tmp_path / "backgrounds").mkdir()
    return Renderer({"width": 540, "height": 960, "fps": 24}, tmp_path)


def test_landscape_clip_becomes_vertical_with_intro(tmp_path, renderer):
    src = tmp_path / "src.mp4"
    _make(src, ["-f", "lavfi", "-i", "testsrc2=s=1280x720:r=24:d=12", "-f", "lavfi", "-i", "sine=f=440:d=12",
                "-shortest", "-c:v", "libx264", "-c:a", "aac"])
    intro = tmp_path / "intro.mp3"
    _make(intro, ["-f", "lavfi", "-i", "sine=f=660:d=2"])
    words = proportional_words("this is a fake transcript for the caption test", 2.0, 9.0)
    out = tmp_path / "out.mp4"
    total = renderer.clip(src, 2.0, 9.0, words, out, hook="WAIT FOR IT", credit="Credit: @test",
                          intro_audio=intro, intro_text="here is our take")
    assert out.exists() and _dims(out) == (540, 960)
    assert abs(media.duration(out, FFPROBE) - total) < 0.5
    assert media.has_audio(out, FFPROBE)


def test_original_over_generated_background(tmp_path, renderer):
    narration = tmp_path / "n.mp3"
    _make(narration, ["-f", "lavfi", "-i", "sine=f=300:d=4"])
    out = tmp_path / "orig.mp4"
    renderer.original(narration, proportional_words("three facts you did not know", 0.1, 4.0), out, hook="DID YOU KNOW")
    assert _dims(out) == (540, 960) and media.duration(out, FFPROBE) > 3.5
