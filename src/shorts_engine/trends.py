"""Trend discovery: what is gaining views right now, plus what worked on our own channel."""
from __future__ import annotations

import json
import logging
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .db import DB, now_iso

log = logging.getLogger(__name__)

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
STOPWORDS = set("""
a an the and or but if of on in to for from with by at as is are was were be been it its this that these those
i you he she we they me my your our their his her them us not no yes so do does did done can will just than then
too very all any some more most new official video videos music shorts short full hd 4k live stream ep episode part
vs ft feat amp com www http https youtube watch how what why when who where which here there one two get got
tutorial tutorials course beginners beginner explained guide complete crash minutes hours learn learning step
day days week weeks hindi english tamil telugu using use
""".split())


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if len(t) > 2 and t not in STOPWORDS and not t.isdigit()]


def keywords_for(title: str, tags: Iterable[str] = ()) -> set[str]:
    toks = tokenize(title)
    kws = set(toks) | {f"{a} {b}" for a, b in zip(toks, toks[1:])}
    for tag in tags:
        tag_toks = tokenize(tag)
        if 1 <= len(tag_toks) <= 3:
            kws.add(" ".join(tag_toks))
    return kws


def velocity(views: int, published_at: datetime, now: datetime) -> float:
    hours = max((now - published_at).total_seconds() / 3600, 1.0)
    return views / hours


def aggregate(videos: list[dict], now: datetime) -> dict[str, tuple[float, str]]:
    """videos: {title, tags, views, published_at}. Returns keyword -> (score, sample title)."""
    weight: dict[str, float] = defaultdict(float)
    count: dict[str, int] = defaultdict(int)
    sample: dict[str, tuple[float, str]] = {}
    for v in videos:
        w = math.log1p(velocity(v["views"], v["published_at"], now))
        for kw in keywords_for(v["title"], v.get("tags", [])):
            weight[kw] += w
            count[kw] += 1
            if kw not in sample or w > sample[kw][0]:
                sample[kw] = (w, v["title"])
    # Keywords shared by several hot videos are trends; one-offs are noise.
    return {
        kw: (weight[kw] * (1 + 0.5 * (count[kw] - 1)), sample[kw][1])
        for kw in weight
        if count[kw] >= 2 or " " in kw
    }


def fetch_trending_videos(settings: dict) -> list[dict]:
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        log.warning("YOUTUBE_API_KEY not set; skipping YouTube trending lookup")
        return []
    from googleapiclient.discovery import build

    yt = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
    out = chart_videos(yt, settings)
    keywords = settings.get("search_keywords", [])
    if keywords:
        seen = {v["id"] for v in out}
        out += [v for v in niche_videos(yt, keywords, settings, datetime.now(timezone.utc)) if v["id"] not in seen]
    log.info("fetched %d trending videos", len(out))
    return out


def _as_video(item: dict) -> dict:
    sn = item["snippet"]
    return {
        "id": item["id"],
        "title": sn.get("title", ""),
        "tags": sn.get("tags", []),
        "views": int(item.get("statistics", {}).get("viewCount", 0)),
        "published_at": datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00")),
    }


def chart_videos(yt, settings: dict) -> list[dict]:
    """YouTube's mostPopular charts per region/category. An empty category_ids list disables charts."""
    from googleapiclient.errors import HttpError

    out: list[dict] = []
    seen: set[str] = set()
    for region in settings.get("region_codes", ["US"]):
        for cat in settings.get("category_ids", [None]):
            params = dict(part="snippet,statistics", chart="mostPopular", regionCode=region,
                          maxResults=int(settings.get("max_results_per_query", 50)))
            if cat:
                params["videoCategoryId"] = str(cat)
            try:
                resp = yt.videos().list(**params).execute()
            except HttpError as e:
                log.info("trending %s/%s unavailable: %s", region, cat, e.status_code)
                continue
            for item in resp.get("items", []):
                if item["id"] not in seen:
                    seen.add(item["id"])
                    out.append(_as_video(item))
    return out


def niche_videos(yt, keywords: list[str], settings: dict, now: datetime) -> list[dict]:
    """Most-viewed recent uploads per niche keyword. Each keyword costs one search.list call (100/day)."""
    from googleapiclient.errors import HttpError

    since = (now - timedelta(days=int(settings.get("search_days", 7)))).strftime("%Y-%m-%dT%H:%M:%SZ")
    found: dict[str, set[str]] = {}  # video id -> keywords whose search returned it
    for kw in keywords:
        params = dict(part="id", q=kw, type="video", order="viewCount", publishedAfter=since,
                      maxResults=int(settings.get("search_results_per_keyword", 25)))
        if settings.get("search_language"):
            params["relevanceLanguage"] = settings["search_language"]
        try:
            resp = yt.search().list(**params).execute()
        except HttpError as e:
            if e.status_code == 403:
                log.warning("search quota reached after %d keyword(s); using what we have", keywords.index(kw))
                break
            log.info("search '%s' failed: %s", kw, e.status_code)
            continue
        for i in resp.get("items", []):
            if vid := i.get("id", {}).get("videoId"):
                found.setdefault(vid, set()).add(kw)
    ids = list(found)
    exclude = {t for term in settings.get("exclude_terms", []) for t in tokenize(term)}
    out: list[dict] = []
    for i in range(0, len(ids), 50):
        resp = yt.videos().list(part="snippet,statistics", id=",".join(ids[i : i + 50])).execute()
        for item in resp.get("items", []):
            sn = item["snippet"]
            words = set(tokenize(" ".join([sn.get("title", ""), *sn.get("tags", [])])))
            # Search by viewCount drifts into loosely related viral videos (and hashtag-stuffed descriptions);
            # keep only videos whose title or tags actually carry the keyword and none of the excluded terms.
            if words & exclude:
                continue
            if any(set(tokenize(kw)) <= words for kw in found.get(item["id"], ())):
                out.append(_as_video(item))
    return out


def own_performance(db: DB, now: datetime) -> list[dict]:
    """Our uploaded videos with their latest view count, in the same shape as trending videos."""
    rows = db.q("""
        SELECT c.title, c.tags, c.uploaded_at, MAX(s.views) AS views
        FROM clips c JOIN stats s ON s.clip_id = c.id
        WHERE c.status = 'uploaded' AND c.uploaded_at IS NOT NULL
        GROUP BY c.id
    """)
    return [{
        "title": r["title"],
        "tags": json.loads(r["tags"] or "[]"),
        "views": r["views"] or 0,
        "published_at": datetime.fromisoformat(r["uploaded_at"]),
    } for r in rows]


def refresh_trends(db: DB, settings: dict, top_n: int = 200) -> list[tuple[str, float]]:
    now = datetime.now(timezone.utc)
    scored = aggregate(fetch_trending_videos(settings), now)
    # Our own winners count double: they show what this channel's audience responds to.
    for kw, (score, title) in aggregate(own_performance(db, now), now).items():
        prev = scored.get(kw, (0.0, title))
        scored[kw] = (prev[0] + 2 * score, prev[1])
    top = max((s for s, _ in scored.values()), default=10.0)
    for kw in settings.get("seed_keywords", []):
        kw = " ".join(tokenize(kw)) or kw.lower()
        scored[kw] = (max(scored.get(kw, (0.0, ""))[0], top), scored.get(kw, (0.0, kw))[1])

    ranked = sorted(scored.items(), key=lambda kv: kv[1][0], reverse=True)[:top_n]
    ts = now_iso()
    with db.tx() as c:
        c.executemany(
            "INSERT INTO trends (keyword, score, origin, sample_title, captured_at) VALUES (?, ?, ?, ?, ?)",
            [(kw, s, "youtube+own", t, ts) for kw, (s, t) in ranked],
        )
    return [(kw, s) for kw, (s, _) in ranked]


def latest_trends(db: DB, max_age_hours: float | None = None) -> list[tuple[str, float]]:
    row = db.one("SELECT MAX(captured_at) AS ts FROM trends")
    if not row or not row["ts"]:
        return []
    if max_age_hours is not None:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["ts"])
        if age > timedelta(hours=max_age_hours):
            return []
    return [(r["keyword"], r["score"]) for r in
            db.q("SELECT keyword, score FROM trends WHERE captured_at = ? ORDER BY score DESC", row["ts"])]


def trend_score(text: str, trends: list[tuple[str, float]]) -> tuple[float, list[str]]:
    """Normalised 0..1 score of how strongly text matches current trends, plus matched keywords."""
    if not trends:
        return 0.0, []
    kws = keywords_for(text)
    hits = [(k, s) for k, s in trends if k in kws]
    top = trends[0][1] or 1.0
    return min(sum(s for _, s in hits) / (top * 2), 1.0), [k for k, _ in hits]
