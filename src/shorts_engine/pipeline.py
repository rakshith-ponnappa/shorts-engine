"""Daily orchestration: trends -> sources -> highlights/originals -> guardrails -> render -> schedule -> stats."""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import original as orig
from .config import Config
from .db import DB, now_iso
from .guardrails import Guardrails
from .highlights import find_highlights
from .llm import LLM
from .render import Renderer
from .schedule import daily_quota, publish_slots, split_mix
from .sources import sync_sources
from .trends import latest_trends, refresh_trends

log = logging.getLogger(__name__)


@dataclass
class DayReport:
    day: date
    quota: int
    already_scheduled: int
    produced: dict[str, int] = field(default_factory=dict)
    rejected: dict[str, int] = field(default_factory=dict)
    uploaded: int = 0
    planned: list[tuple[str, str]] = field(default_factory=list)  # (publish_at, title) in dry-run
    notes: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, cfg: Config, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.db = DB(cfg.db_path)
        self.llm = LLM(cfg.llm)
        self.tz = ZoneInfo(cfg.timezone)
        self._renderer: Renderer | None = None
        self._youtube = None

    @property
    def renderer(self) -> Renderer:
        if self._renderer is None:
            self._renderer = Renderer(self.cfg.render, self.cfg.assets_dir)
        return self._renderer

    @property
    def youtube(self):
        if self._youtube is None:
            from .youtube import YouTube

            self._youtube = YouTube(self.cfg.youtube, self.cfg.root)
        return self._youtube

    # ---------- planning ----------

    def today(self) -> date:
        return datetime.now(self.tz).date()

    def scheduled_on(self, day: date) -> list[datetime]:
        out = []
        for r in self.db.q("SELECT publish_at FROM clips WHERE status = 'uploaded' AND publish_at IS NOT NULL"):
            t = datetime.fromisoformat(r["publish_at"]).astimezone(self.tz)
            if t.date() == day:
                out.append(t)
        return out

    def inventory(self, statuses: tuple[str, ...] = ("ready",)) -> dict[str, int]:
        marks = ",".join("?" for _ in statuses)
        rows = self.db.q(f"SELECT kind, COUNT(*) AS n FROM clips WHERE status IN ({marks}) GROUP BY kind", *statuses)
        inv = {"clip": 0, "ai_original": 0}
        inv.update({r["kind"]: r["n"] for r in rows})
        return inv

    def trends(self) -> list[tuple[str, float]]:
        hours = float(self.cfg.trends.get("refresh_hours", 6))
        current = latest_trends(self.db, max_age_hours=hours)
        if current:
            return current
        fresh = refresh_trends(self.db, self.cfg.trends)
        return fresh or latest_trends(self.db)

    # ---------- production ----------

    def _render_dir(self) -> Path:
        d = self.cfg.data_dir / "renders" / self.today().isoformat()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def make_clips(self, n: int, trends, guard: Guardrails, report: DayReport) -> int:
        if n <= 0:
            return 0
        from .transcribe import transcribe

        per_source = self.cfg.campaign.max_clips_per_source_per_day
        day_start = datetime.combine(self.today(), datetime.min.time(), self.tz).astimezone(timezone.utc).isoformat()
        sources = self.db.q("""
            SELECT s.*, (SELECT COUNT(*) FROM clips c WHERE c.source_id = s.id AND c.status != 'rejected') AS used,
                   (SELECT COUNT(*) FROM clips c WHERE c.source_id = s.id AND c.created_at >= ?) AS today
            FROM sources s ORDER BY used ASC, s.added_at DESC
        """, day_start)
        made = 0
        for src in sources:
            if made >= n:
                break
            budget = min(per_source - src["today"], n - made)
            if budget <= 0:
                continue
            rights = guard.check_rights(src["id"])
            if not rights.ok:
                report.notes.append(f"skipped source {src['title']}: {rights.reason}")
                continue
            tpath = self.cfg.data_dir / "transcripts" / f"{src['id']}.json"
            try:
                segments = transcribe(Path(src["file_path"]), tpath, self.cfg.whisper, self.renderer.ffmpeg)
            except Exception as e:  # noqa: BLE001
                report.notes.append(f"transcription failed for {src['title']}: {e}")
                continue
            if not src["transcript_path"]:
                self.db.update("sources", src["id"], transcript_path=str(tpath))
            cands = find_highlights(segments, trends, self.llm, self.cfg.clip, want=budget * 3,
                                    attribution=src["attribution"] or "",
                                    focus=self.cfg.youtube.get("channel_focus", ""))
            src_made = 0
            for c in cands:
                if src_made >= budget:
                    break
                verdict = guard.check_clip(src["id"], c.start, c.end, c.title, c.text)
                if not verdict.ok:
                    report.rejected[verdict.reason] = report.rejected.get(verdict.reason, 0) + 1
                    continue
                out = self._render_dir() / f"clip_{uuid.uuid4().hex[:10]}.mp4"
                intro_audio = None
                if c.commentary and self.llm.enabled and self.cfg.llm.get("commentary_voiceover", True):
                    try:
                        intro_audio = self.llm.tts(c.commentary, out.with_suffix(".intro.mp3"))
                    except Exception as e:  # noqa: BLE001
                        log.warning("TTS failed, rendering without intro: %s", e)
                try:
                    self.renderer.clip(Path(src["file_path"]), c.start, c.end, c.words, out, hook=c.hook,
                                       credit=src["attribution"] or "", intro_audio=intro_audio,
                                       intro_text=c.commentary if intro_audio else "")
                except Exception as e:  # noqa: BLE001
                    report.notes.append(f"render failed: {e}")
                    continue
                self.db.insert("clips", {
                    "kind": "clip", "source_id": src["id"], "start": c.start, "end": c.end,
                    "topic": (c.matched or [""])[0], "title": c.title, "description": c.description,
                    "tags": c.tags, "hook": c.hook, "commentary": c.commentary, "transcript": c.text,
                    "score": c.score, "synthetic": 1 if intro_audio else 0, "file_path": str(out),
                    "status": "ready", "created_at": now_iso(),
                })
                guard.remember(c.title, c.text)
                made += 1
                src_made += 1
        return made

    def make_originals(self, n: int, trends, guard: Guardrails, report: DayReport) -> int:
        if n <= 0:
            return 0
        if not self.llm.enabled:
            report.notes.append("AI originals skipped: set llm.provider to openai or azure")
            return 0
        g = self.cfg.guardrails
        made = 0
        for topic in orig.pick_topics(self.db, trends, n * 2):
            if made >= n:
                break
            try:
                o = orig.write_script(self.llm, topic, [k for k, _ in trends],
                                      focus=self.cfg.youtube.get("channel_focus", ""))
            except Exception as e:  # noqa: BLE001
                report.notes.append(f"script failed for '{topic}': {e}")
                continue
            if not o.script:
                continue
            verdict = guard.check_text(o.title, o.script)
            if not verdict.ok:
                report.rejected[verdict.reason] = report.rejected.get(verdict.reason, 0) + 1
                continue
            out = self._render_dir() / f"orig_{uuid.uuid4().hex[:10]}.mp4"
            try:
                audio = self.llm.tts(o.script, out.with_suffix(".mp3"))
                words = orig.narration_words(audio, o.script, self.cfg.whisper, self.renderer.ffmpeg,
                                             self.renderer.ffprobe)
                self.renderer.original(audio, words, out, hook=o.hook)
            except Exception as e:  # noqa: BLE001
                report.notes.append(f"original render failed for '{topic}': {e}")
                continue
            needs_review = g.get("review_ai_originals", True) or o.risk == "high"
            self.db.insert("clips", {
                "kind": "ai_original", "topic": topic, "title": o.title,
                "description": "\n".join(filter(None, [o.description, " ".join(f"#{t.replace(' ', '')}" for t in o.tags[:5])])),
                "tags": o.tags, "hook": o.hook, "transcript": o.script, "score": 0.5,
                "synthetic": 1 if g.get("synthetic_disclosure", True) else 0, "file_path": str(out),
                "status": "needs_review" if needs_review else "ready", "created_at": now_iso(),
            })
            guard.remember(o.title, o.script)
            made += 1
        return made

    # ---------- upload ----------

    def upload(self, day: date, n: int, report: DayReport) -> None:
        if n <= 0:
            return
        c = self.cfg.campaign
        slots = publish_slots(day, n, c.publish_window, c.min_minutes_between_uploads, self.tz,
                              datetime.now(self.tz), taken=self.scheduled_on(day))
        if len(slots) < n:
            report.notes.append(f"only {len(slots)} publish slots left today (wanted {n})")
        ready = self.db.q("SELECT * FROM clips WHERE status = 'ready' ORDER BY score DESC, created_at ASC LIMIT ?",
                          len(slots))
        ready = _interleave(ready)
        from .youtube import QuotaExceeded

        if not self.dry_run and ready:
            try:
                ch = self.youtube.verify_channel()  # fail fast on auth/channel problems, before any upload
            except Exception as e:  # noqa: BLE001
                report.notes.append(f"upload skipped, {len(ready)} clip(s) kept ready: {e}")
                return
            log.info("uploading to channel %s (%s)", ch["title"], ch["id"])

        for clip, slot in zip(ready, slots):
            if self.dry_run:
                report.planned.append((slot.isoformat(), clip["title"]))
                continue
            try:
                vid = self.youtube.upload(Path(clip["file_path"]), clip["title"], clip["description"] or "",
                                          json.loads(clip["tags"] or "[]"), slot, bool(clip["synthetic"]))
            except QuotaExceeded as e:
                report.notes.append(f"YouTube quota reached, stopping: {e}")
                break
            except Exception as e:  # noqa: BLE001
                self.db.update("clips", clip["id"], status="failed", error=str(e)[:1000])
                report.notes.append(f"upload failed for #{clip['id']}: {e}")
                continue
            self.db.update("clips", clip["id"], status="uploaded", youtube_id=vid,
                           publish_at=slot.isoformat(), uploaded_at=now_iso())
            report.uploaded += 1
            log.info("uploaded #%s -> %s publishes %s", clip["id"], vid, slot)

    def refresh_stats(self, days: int = 14) -> int:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.db.q("SELECT id, youtube_id FROM clips WHERE status = 'uploaded' AND uploaded_at >= ?", since)
        if not rows:
            return 0
        stats = self.youtube.stats([r["youtube_id"] for r in rows])
        ts = now_iso()
        with self.db.tx() as conn:
            conn.executemany(
                "INSERT INTO stats (clip_id, captured_at, views, likes, comments) VALUES (?, ?, ?, ?, ?)",
                [(r["id"], ts, s["views"], s["likes"], s["comments"])
                 for r in rows if (s := stats.get(r["youtube_id"]))],
            )
        return len(stats)

    # ---------- the daily run ----------

    def run_day(self, day: date | None = None) -> DayReport:
        day = day or self.today()
        quota = daily_quota(self.cfg.campaign, day)
        already = len(self.scheduled_on(day))
        report = DayReport(day=day, quota=quota, already_scheduled=already)
        remaining = max(quota - already, 0)
        if remaining == 0:
            report.notes.append("nothing to do: outside campaign window or quota already met")
            return report

        trends = self.trends()
        if not trends:
            report.notes.append("no trend data (set YOUTUBE_API_KEY or trends.seed_keywords); ranking on hooks only")
        new_sources = sync_sources(self.cfg, self.db)
        if new_sources:
            report.notes.append(f"{new_sources} new source video(s) registered")

        want = split_mix(remaining, self.cfg.campaign.mix)
        guard = Guardrails(self.db, self.cfg.guardrails)
        # Originals still waiting for review count as produced, so top-up runs don't pile up the queue.
        pending = self.inventory(("ready", "needs_review"))
        made_orig = self.make_originals(max(want.get("ai_original", 0) - pending["ai_original"], 0), trends, guard, report)
        # Clips fill whatever today's uploadable inventory can't cover (no LLM, originals awaiting review, ...).
        ready = self.inventory()
        clip_target = max(want.get("clip", 0) - ready["clip"], remaining - ready["clip"] - ready["ai_original"], 0)
        made_clips = self.make_clips(clip_target, trends, guard, report)
        if made_clips < clip_target:
            report.notes.append(
                f"only {made_clips}/{clip_target} clips made: add more source videos to {self.cfg.inbox_dir} "
                f"or licensed sources (each source yields at most "
                f"{self.cfg.campaign.max_clips_per_source_per_day}/day)")
        # Originals cover a clip shortfall; ones awaiting review count, so top-up runs don't overproduce.
        if self.llm.enabled:
            pending = self.inventory(("ready", "needs_review"))
            made_orig += self.make_originals(max(remaining - pending["clip"] - pending["ai_original"], 0),
                                             trends, guard, report)
        report.produced = {"clip": made_clips, "ai_original": made_orig}

        self.upload(day, remaining, report)
        if not self.dry_run:
            try:
                self.refresh_stats()
            except Exception as e:  # noqa: BLE001
                report.notes.append(f"stats refresh failed: {e}")
        return report


def _interleave(rows: list) -> list:
    """Alternate content kinds so the feed doesn't get long runs of the same format/source."""
    by_kind: dict[str, list] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    out, queues = [], list(by_kind.values())
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out
