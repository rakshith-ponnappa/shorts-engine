"""Build ASS subtitle files: hook title, word-by-word captions, credit line."""
from __future__ import annotations

import re

WHITE, BLACK = "&H00FFFFFF", "&H00000000"
HL_ON, HL_OFF = "{\\c&H00E5FF&}", "{\\c&HFFFFFF&}"


def ass_time(t: float) -> str:
    t = max(t, 0.0)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def proportional_words(text: str, start: float, end: float) -> list[dict]:
    """Spread words over [start, end] by character length (for TTS audio without timestamps)."""
    words = text.split()
    if not words:
        return []
    total = sum(len(w) + 1 for w in words)
    out, t = [], start
    for w in words:
        d = (end - start) * (len(w) + 1) / total
        out.append({"start": t, "end": t + d, "word": w})
        t += d
    return out


def shift_words(words: list[dict], clip_start: float, clip_end: float, offset: float) -> list[dict]:
    """Keep words inside the clip and re-time them relative to the rendered video."""
    return [
        {"start": w["start"] - clip_start + offset, "end": min(w["end"], clip_end) - clip_start + offset, "word": w["word"]}
        for w in words
        if w["start"] >= clip_start - 0.05 and w["start"] < clip_end
    ]


def caption_events(words: list[dict], per_chunk: int) -> list[tuple[float, float, str]]:
    """One event per spoken word; the chunk stays on screen with the active word highlighted."""
    events = []
    words = [w for w in words if re.sub(r"\W", "", w["word"])]
    for i in range(0, len(words), per_chunk):
        chunk = words[i : i + per_chunk]
        nxt = words[i + per_chunk]["start"] if i + per_chunk < len(words) else chunk[-1]["end"]
        for j, w in enumerate(chunk):
            end = chunk[j + 1]["start"] if j + 1 < len(chunk) else max(nxt, w["end"])
            parts = [
                (f"{HL_ON}{ass_escape(x['word']).upper()}{HL_OFF}" if k == j
                 else ass_escape(x["word"]).upper())
                for k, x in enumerate(chunk)
            ]
            events.append((w["start"], max(end, w["start"] + 0.05), " ".join(parts)))
    return events


def build_ass(width: int, height: int, font: str, captions: list[tuple[float, float, str]],
              hook: str = "", hook_seconds: float = 3.0, credit: str = "", total: float = 0.0) -> str:
    fs = round(height / 24)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{fs},{WHITE},{WHITE},{BLACK},&H80000000,-1,0,0,0,100,100,0,0,1,{max(fs // 12, 4)},2,2,60,60,{round(height * 0.28)},1
Style: Hook,{font},{round(fs * 0.9)},{BLACK},{BLACK},&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,8,70,70,{round(height * 0.1)},1
Style: Credit,{font},{round(fs * 0.45)},{WHITE},{WHITE},{BLACK},&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,40,40,{round(height * 0.05)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if hook:
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(hook_seconds)},Hook,,0,0,0,,{ass_escape(hook)}")
    for s, e, text in captions:
        lines.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Caption,,0,0,0,,{text}")
    if credit and total > 0:
        lines.append(f"Dialogue: 0,{ass_time(0)},{ass_time(total)},Credit,,0,0,0,,{ass_escape(credit)}")
    return header + "\n".join(lines) + "\n"
