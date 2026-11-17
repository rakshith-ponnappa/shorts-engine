"""Render vertical 9:16 Shorts with ffmpeg."""
from __future__ import annotations

import random
from pathlib import Path

from . import captions as cap
from . import media

ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart"]


class Renderer:
    def __init__(self, settings: dict, assets_dir: Path):
        self.ffmpeg, self.ffprobe = media.resolve_ffmpeg(settings.get("ffmpeg_bin", "auto"))
        self.w = int(settings.get("width", 1080))
        self.h = int(settings.get("height", 1920))
        self.fps = int(settings.get("fps", 30))
        self.font = settings.get("font", "Arial")
        self.per_chunk = int(settings.get("caption_words_per_chunk", 3))
        self.hook_seconds = float(settings.get("hook_seconds", 3.0))
        self.max_total = float(settings.get("max_total_seconds", 60))
        self.backgrounds = sorted(p for p in (assets_dir / "backgrounds").glob("*")
                                  if p.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"})

    def _write_ass(self, out: Path, events, hook: str, credit: str, total: float) -> str:
        ass = out.with_suffix(".ass")
        ass.write_text(cap.build_ass(self.w, self.h, self.font, events, hook, self.hook_seconds, credit, total))
        return ass.name  # ffmpeg runs with cwd=out.parent so the filter arg needs no path escaping

    def clip(self, src: Path, start: float, end: float, words: list[dict], out: Path,
             hook: str = "", credit: str = "", intro_audio: Path | None = None, intro_text: str = "") -> float:
        """Cut [start, end] from src into a captioned vertical short. Returns final duration."""
        intro = media.duration(intro_audio, self.ffprobe) + 0.25 if intro_audio else 0.0
        end = min(end, start + self.max_total - intro)
        length = end - start
        total = intro + length

        events = []
        if intro:
            events += cap.caption_events(cap.proportional_words(intro_text, 0.1, intro - 0.25), self.per_chunk)
        events += cap.caption_events(cap.shift_words(words, start, end, intro), self.per_chunk)
        ass = self._write_ass(out, events, hook, credit, total)

        w, h = self.w, self.h
        vf = (
            f"[0:v]setpts=PTS-STARTPTS,fps={self.fps},split=2[a][b];"
            f"[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:2,"
            f"eq=brightness=-0.08[bg];"
            f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=({w}-w)/2:({h}-h)/2,setsar=1[v0];"
        )
        vf += f"[v0]tpad=start_duration={intro:.3f}:start_mode=clone[v1];" if intro else "[v0]null[v1];"
        vf += f"[v1]subtitles={ass}[v]"

        src_audio = media.has_audio(src, self.ffprobe)
        cmd = [self.ffmpeg, "-y", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(src.resolve())]
        af = ""
        if intro:
            cmd += ["-i", str(intro_audio.resolve())]
            if src_audio:
                af = (f";[0:a]asetpts=PTS-STARTPTS,adelay={int(intro * 1000)}:all=1[a0];"
                      f"[1:a][a0]amix=inputs=2:duration=longest:normalize=0,loudnorm=I=-14:TP=-1.5[a]")
            else:
                af = ";[1:a]apad,loudnorm=I=-14:TP=-1.5[a]"
        elif src_audio:
            af = ";[0:a]asetpts=PTS-STARTPTS,loudnorm=I=-14:TP=-1.5[a]"
        cmd += ["-filter_complex", vf + af, "-map", "[v]"]
        cmd += ["-map", "[a]"] if af else ["-an"]
        cmd += [*ENCODE, "-t", f"{total:.3f}", out.name]
        _run_in(cmd, out.parent)
        return total

    def original(self, narration: Path, words: list[dict], out: Path, hook: str = "") -> float:
        """Narrated AI original over licensed b-roll (assets/backgrounds) or an animated gradient."""
        total = media.duration(narration, self.ffprobe) + 0.4
        ass = self._write_ass(out, cap.caption_events(words, self.per_chunk), hook, "", total)
        w, h = self.w, self.h
        if self.backgrounds:
            bg = random.choice(self.backgrounds)
            inputs = ["-stream_loop", "-1", "-i", str(bg.resolve())]
            bg_chain = f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={self.fps}"
        elif media.has_filter(self.ffmpeg, "gradients"):
            inputs = ["-f", "lavfi", "-i", f"gradients=s={w}x{h}:r={self.fps}:c0=0x1d2671:c1=0xc33764:speed=0.008"]
            bg_chain = "[0:v]null"
        else:
            inputs = ["-f", "lavfi", "-i", f"color=c=0x1d2671:s={w}x{h}:r={self.fps}"]
            bg_chain = "[0:v]null"
        fc = f"{bg_chain},eq=brightness=-0.12,setsar=1,subtitles={ass}[v];[1:a]apad,loudnorm=I=-14:TP=-1.5[a]"
        cmd = [self.ffmpeg, "-y", *inputs, "-i", str(narration.resolve()), "-filter_complex", fc,
               "-map", "[v]", "-map", "[a]", *ENCODE, "-t", f"{total:.3f}", out.name]
        _run_in(cmd, out.parent)
        return total


def _run_in(cmd: list[str], cwd: Path) -> None:
    media.run(cmd, cwd=cwd)
