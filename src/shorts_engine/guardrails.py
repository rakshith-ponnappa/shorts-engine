"""Checks every candidate must pass before it is rendered or uploaded."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .db import DB

_WORD = re.compile(r"[a-z0-9']+")
LIVE_STATUSES = ("ready", "needs_review", "uploaded")


def shingles(text: str, k: int = 3) -> set[tuple[str, ...]]:
    words = _WORD.findall(text.lower())
    if len(words) < k:
        return {tuple(words)} if words else set()
    return {tuple(words[i : i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap_ratio(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    inter = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    shortest = min(a_end - a_start, b_end - b_start)
    return inter / shortest if shortest > 0 else 0.0


def normalize_title(title: str) -> str:
    return " ".join(_WORD.findall(title.lower()))


@dataclass
class Verdict:
    ok: bool
    reason: str = ""


class Guardrails:
    def __init__(self, db: DB, settings: dict):
        self.db = db
        self.require_rights = settings.get("require_rights", True)
        self.max_text_similarity = float(settings.get("max_text_similarity", 0.6))
        self.max_time_overlap = float(settings.get("max_time_overlap", 0.5))
        marks = ",".join("?" for _ in LIVE_STATUSES)
        rows = db.q(f"SELECT title, transcript FROM clips WHERE status IN ({marks})", *LIVE_STATUSES)
        self._titles = {normalize_title(r["title"]) for r in rows}
        self._texts = [shingles(r["transcript"] or "") for r in rows if r["transcript"]]

    def check_rights(self, source_id: int | None) -> Verdict:
        if source_id is None:
            return Verdict(False, "no source")
        src = self.db.one("SELECT kind, permission_ref FROM sources WHERE id = ?", source_id)
        if src is None:
            return Verdict(False, "unknown source")
        if src["kind"] == "own":
            return Verdict(True)
        if src["kind"] == "licensed" and (src["permission_ref"] or "").strip():
            return Verdict(True)
        return Verdict(not self.require_rights, "source has no recorded rights")

    def check_overlap(self, source_id: int, start: float, end: float) -> Verdict:
        marks = ",".join("?" for _ in LIVE_STATUSES)
        rows = self.db.q(
            f"SELECT start, end FROM clips WHERE source_id = ? AND status IN ({marks})",
            source_id, *LIVE_STATUSES,
        )
        for r in rows:
            if overlap_ratio(start, end, r["start"], r["end"]) > self.max_time_overlap:
                return Verdict(False, "overlaps an existing clip from the same source")
        return Verdict(True)

    def check_text(self, title: str, transcript: str) -> Verdict:
        if normalize_title(title) in self._titles:
            return Verdict(False, "duplicate title")
        sh = shingles(transcript)
        for past in self._texts:
            if jaccard(sh, past) > self.max_text_similarity:
                return Verdict(False, "too similar to a previous video")
        return Verdict(True)

    def remember(self, title: str, transcript: str) -> None:
        self._titles.add(normalize_title(title))
        if transcript:
            self._texts.append(shingles(transcript))

    def check_clip(self, source_id: int, start: float, end: float, title: str, transcript: str) -> Verdict:
        for v in (self.check_rights(source_id), self.check_overlap(source_id, start, end),
                  self.check_text(title, transcript)):
            if not v.ok:
                return v
        return Verdict(True)
