"""AI originals: a short narrated explainer on a trending topic."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import captions as cap
from . import media
from .db import DB
from .llm import LLM

log = logging.getLogger(__name__)

SYSTEM = (
    "You write original YouTube Shorts scripts (35-50 seconds spoken, 90-130 words) about a trending topic. "
    "Format: a 1-line hook that creates curiosity, fast value (facts, context, a list, or a clear opinion "
    "labelled as opinion), and a closing line that invites comments. Rules: only widely known, verifiable "
    "facts; no invented quotes or statistics; no claims about private lives, health, or crimes of real people; "
    "never impersonate anyone; no medical/financial advice. If the topic is too thin or risky, choose the "
    "closest safe angle. Return JSON: {\"title\": <=70 chars, \"hook\": <=7 words on-screen, \"script\": str, "
    "\"description\": 1-2 sentences, \"tags\": up to 8 lowercase tags, \"risk\": \"low\"|\"medium\"|\"high\"}"
)


@dataclass
class Original:
    topic: str
    title: str
    hook: str
    script: str
    description: str
    tags: list[str] = field(default_factory=list)
    risk: str = "medium"


def pick_topics(db: DB, trends: list[tuple[str, float]], n: int, cooldown_days: int = 3) -> list[str]:
    since = (datetime.now(timezone.utc) - timedelta(days=cooldown_days)).isoformat()
    used = {r["topic"] for r in db.q("SELECT topic FROM clips WHERE kind = 'ai_original' AND created_at > ?", since)}
    picked: list[str] = []
    for kw, _ in trends:
        if kw in used or any(kw in p or p in kw for p in picked):
            continue
        picked.append(kw)
        if len(picked) == n:
            break
    return picked


def write_script(llm: LLM, topic: str, related: list[str], focus: str = "") -> Original:
    prompt = f"Trending topic: {topic}\nRelated trending terms: {', '.join(related[:15])}"
    if focus:
        prompt = f"Channel focus (stay on-brand; pick the closest angle that fits): {focus}\n{prompt}"
    data = llm.chat_json(SYSTEM, prompt)
    return Original(
        topic=topic,
        title=str(data.get("title", topic.title()))[:95],
        hook=str(data.get("hook", ""))[:60],
        script=str(data.get("script", "")).strip(),
        description=str(data.get("description", "")),
        tags=[str(t).lower() for t in data.get("tags", [])][:8],
        risk=str(data.get("risk", "medium")).lower(),
    )


def narration_words(audio: Path, script: str, whisper_settings: dict, ffmpeg: str, ffprobe: str) -> list[dict]:
    """Exact word timings via whisper when installed, otherwise proportional to word length."""
    try:
        from .transcribe import transcribe

        segs = transcribe(audio, audio.with_suffix(".json"), whisper_settings, ffmpeg)
        words = [w for s in segs for w in s["words"]]
        if words:
            return words
    except Exception as e:  # noqa: BLE001 - timings are cosmetic; fall back rather than drop the video
        log.info("whisper timing unavailable for narration, using proportional timing: %s", e)
    return cap.proportional_words(script, 0.1, media.duration(audio, ffprobe))
