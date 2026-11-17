"""Find the most clip-worthy moments in a transcript and write their copy."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .llm import LLM
from .trends import trend_score

log = logging.getLogger(__name__)

HOOK_WORDS = {
    "crazy", "insane", "never", "secret", "why", "how", "biggest", "worst", "best", "actually", "mistake",
    "nobody", "everyone", "truth", "shocking", "real", "wrong", "first", "last", "only", "wait", "look",
    "believe", "impossible", "finally", "money", "million", "win", "lost", "fight", "funny", "hilarious",
}
_SENT_END = re.compile(r"[.!?…]$")


@dataclass
class Candidate:
    start: float
    end: float
    text: str
    words: list[dict] = field(default_factory=list)
    score: float = 0.0
    matched: list[str] = field(default_factory=list)
    title: str = ""
    hook: str = ""
    commentary: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start


def candidate_windows(segments: list[dict], min_s: float, max_s: float) -> list[Candidate]:
    """One window per starting segment: the longest span <= max_s, preferring a sentence ending."""
    out: list[Candidate] = []
    for i, first in enumerate(segments):
        best_any = best_sentence = None
        for j in range(i, len(segments)):
            dur = segments[j]["end"] - first["start"]
            if dur > max_s:
                break
            if dur >= min_s:
                best_any = j
                if _SENT_END.search(segments[j]["text"].strip()):
                    best_sentence = j
        j = best_sentence if best_sentence is not None else best_any
        if j is None:
            continue
        span = segments[i : j + 1]
        out.append(Candidate(
            start=first["start"], end=segments[j]["end"],
            text=" ".join(s["text"].strip() for s in span),
            words=[w for s in span for w in s.get("words", [])],
        ))
    return out


def heuristic_score(c: Candidate, trends: list[tuple[str, float]]) -> float:
    words = re.findall(r"[a-z']+", c.text.lower())
    if not words:
        return 0.0
    opening = " ".join(words[:15])
    hook = sum(1 for w in set(words) if w in HOOK_WORDS) / 6
    opening_hook = 1.0 if ("?" in c.text[:120] or any(w in HOOK_WORDS for w in opening.split())) else 0.0
    punch = min((c.text.count("!") + c.text.count("?")) / 4, 1.0)
    wps = len(words) / max(c.duration, 1.0)
    pace = 1.0 - min(abs(wps - 3.0) / 3.0, 1.0)  # lively but intelligible speech
    t, matched = trend_score(c.text, trends)
    c.matched = matched
    return round(0.25 * min(hook, 1.0) + 0.15 * opening_hook + 0.1 * punch + 0.15 * pace + 0.35 * t, 4)


def pick_non_overlapping(cands: list[Candidate], k: int) -> list[Candidate]:
    chosen: list[Candidate] = []
    for c in sorted(cands, key=lambda c: c.score, reverse=True):
        if all(c.end <= o.start or c.start >= o.end for o in chosen):
            chosen.append(c)
        if len(chosen) == k:
            break
    return chosen


def _fallback_copy(c: Candidate, attribution: str) -> None:
    first = re.split(r"(?<=[.!?])\s+", c.text.strip())[0]
    c.title = (first[:67] + "...") if len(first) > 70 else first
    c.hook = " ".join(first.split()[:7]).upper()
    c.tags = c.matched[:8]
    c.description = "\n".join(filter(None, [attribution, " ".join(f"#{t.replace(' ', '')}" for t in c.tags[:5])]))


SYSTEM = (
    "You are a short-form video editor. Given transcript excerpts, rate how likely each is to hold a viewer "
    "on YouTube Shorts (strong opening, payoff, emotion, curiosity, relevance to the trending topics). "
    "Write honest copy: no claims the excerpt does not support, no impersonation, no misleading thumbnails "
    "or titles. Return JSON: {\"clips\": [{\"index\": int, \"virality\": 0-10, \"title\": <=70 chars, "
    "\"hook\": <=7 words on-screen text, \"commentary\": one sentence <=20 words giving our own take or context "
    "(spoken before the clip), \"description\": 1-2 sentences, \"tags\": up to 8 lowercase tags}]}"
)


def refine_with_llm(llm: LLM, cands: list[Candidate], trends: list[tuple[str, float]], attribution: str,
                    focus: str = "") -> None:
    payload = "\n\n".join(f"[{i}] ({c.duration:.0f}s) {c.text[:1200]}" for i, c in enumerate(cands))
    trend_list = ", ".join(k for k, _ in trends[:30]) or "none"
    header = f"Channel focus: {focus}\n" if focus else ""
    try:
        data = llm.chat_json(SYSTEM, f"{header}Trending topics: {trend_list}\n\nExcerpts:\n{payload}")
    except Exception as e:  # noqa: BLE001 - fall back to heuristics if the LLM is down
        log.warning("LLM refine failed, using heuristics: %s", e)
        data = {}
    by_index = {int(x["index"]): x for x in data.get("clips", []) if "index" in x}
    for i, c in enumerate(cands):
        _fallback_copy(c, attribution)
        x = by_index.get(i)
        if not x:
            continue
        c.score = round(0.5 * c.score + 0.5 * min(float(x.get("virality", 0)), 10) / 10, 4)
        c.title = str(x.get("title") or c.title)[:95]
        c.hook = str(x.get("hook") or c.hook)[:60]
        c.commentary = str(x.get("commentary") or "")[:200]
        desc = str(x.get("description") or "")
        tags = [str(t).lower() for t in x.get("tags", [])][:8] or c.tags
        c.tags = tags
        c.description = "\n".join(filter(None, [desc, attribution, " ".join(f"#{t.replace(' ', '')}" for t in tags[:5])]))


def find_highlights(segments: list[dict], trends: list[tuple[str, float]], llm: LLM, settings: dict,
                    want: int, attribution: str = "", focus: str = "") -> list[Candidate]:
    cands = candidate_windows(segments, float(settings.get("min_seconds", 20)), float(settings.get("max_seconds", 58)))
    for c in cands:
        c.score = heuristic_score(c, trends)
    shortlist = pick_non_overlapping(cands, int(settings.get("candidates_for_llm", 12)))
    if llm.enabled and shortlist:
        refine_with_llm(llm, shortlist, trends, attribution, focus)
    else:
        for c in shortlist:
            _fallback_copy(c, attribution)
    return sorted(shortlist, key=lambda c: c.score, reverse=True)[:want]
