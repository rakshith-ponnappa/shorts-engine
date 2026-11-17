from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from shorts_engine.captions import ass_time, caption_events, shift_words
from shorts_engine.db import DB, now_iso
from shorts_engine.guardrails import Guardrails
from shorts_engine.highlights import Candidate, candidate_windows, heuristic_score
from shorts_engine.trends import aggregate, niche_videos
from shorts_engine.youtube import YouTube


class _Endpoint:
    def __init__(self, fn):
        self.fn = fn

    def list(self, **params):
        return SimpleNamespace(execute=lambda: self.fn(params))


def _db(tmp_path):
    return DB(tmp_path / "t.db")


def _source(db, kind, permission=""):
    return db.insert("sources", {"source_key": "k", "kind": kind, "permission_ref": permission,
                                 "origin": f"{kind}-{permission}", "added_at": now_iso()})


def test_rights_block_licensed_without_permission(tmp_path):
    db = _db(tmp_path)
    g = Guardrails(db, {})
    assert g.check_rights(_source(db, "own")).ok
    assert g.check_rights(_source(db, "licensed", "email 2026-11-10")).ok
    assert not g.check_rights(_source(db, "licensed", "")).ok
    assert not g.check_rights(None).ok


def test_duplicates_and_overlaps_rejected(tmp_path):
    db = _db(tmp_path)
    sid = _source(db, "own")
    text = "this is the craziest thing that happened on stream today and nobody saw it coming at all"
    db.insert("clips", {"kind": "clip", "source_id": sid, "start": 100, "end": 140, "title": "Crazy stream moment",
                        "transcript": text, "status": "uploaded", "created_at": now_iso()})
    g = Guardrails(db, {"max_text_similarity": 0.6, "max_time_overlap": 0.5})
    assert not g.check_clip(sid, 110, 150, "New title", "different words entirely here").ok   # time overlap
    assert not g.check_clip(sid, 300, 340, "crazy STREAM moment!", "other text").ok          # same title
    assert not g.check_clip(sid, 300, 340, "Fresh", text + " wow").ok                        # same transcript
    assert g.check_clip(sid, 300, 340, "Fresh", "a totally separate story about cooking pasta").ok


def test_candidate_windows_bounds_and_sentence_endings():
    segs = [{"start": i * 5.0, "end": i * 5.0 + 5, "text": f"part {i}" + ("." if i % 3 == 2 else ""), "words": []}
            for i in range(30)]
    cands = candidate_windows(segs, 20, 40)
    assert cands and all(20 <= c.duration <= 40 for c in cands)
    assert all(c.text.endswith(".") for c in cands[:5])


def test_trend_match_raises_score():
    trends = [("ishowspeed", 50.0), ("world cup", 40.0)]
    base = dict(start=0, end=30, words=[])
    plain = Candidate(text="we went to the shop and bought some bread then came home", **base)
    hot = Candidate(text="ishowspeed at the world cup was insane, why did nobody expect it?", **base)
    assert heuristic_score(hot, trends) > heuristic_score(plain, trends)
    assert "ishowspeed" in hot.matched


def test_aggregate_keeps_shared_keywords_only():
    now = datetime.now(timezone.utc)
    vids = [
        {"title": "Speed reacts to Messi goal", "views": 900_000, "published_at": now - timedelta(hours=5)},
        {"title": "Messi goal from every angle", "views": 400_000, "published_at": now - timedelta(hours=3)},
        {"title": "Cooking pasta tutorial", "views": 1_000, "published_at": now - timedelta(hours=50)},
    ]
    scored = aggregate(vids, now)
    assert "messi" in scored and "messi goal" in scored
    assert "pasta" not in scored  # single-video unigram is noise


def test_captions_timing_and_highlight():
    assert ass_time(65.5) == "0:01:05.50"
    words = [{"start": 10 + i * 0.4, "end": 10.3 + i * 0.4, "word": w} for i, w in enumerate("one two three four".split())]
    shifted = shift_words(words, 10.0, 11.0, offset=2.0)
    assert [w["word"] for w in shifted] == ["one", "two", "three"]
    assert abs(shifted[0]["start"] - 2.0) < 1e-9
    events = caption_events(shifted, per_chunk=3)
    assert len(events) == 3 and all("\\c&H00E5FF&" in e[2] for e in events)


def test_niche_search_keeps_recent_relevant_videos_once():
    searches = []
    titles = {"kubernetes-1": "Kubernetes at scale", "shared": "Terraform vs Kubernetes",
              "terraform-1": "Terraform stacks", "roblox": "Kubernetes Roblox speedrun", "viral": "Forsaken speedrun"}

    def search(p):
        searches.append(p)
        return {"items": [{"id": {"videoId": v}} for v in (f"{p['q']}-1", "shared", "roblox", "viral")]}

    def videos(p):
        return {"items": [{"id": i, "snippet": {"title": titles[i], "publishedAt": "2026-11-15T00:00:00Z"},
                           "statistics": {"viewCount": "10"}} for i in p["id"].split(",")]}

    yt = SimpleNamespace(search=lambda: _Endpoint(search), videos=lambda: _Endpoint(videos))
    vids = niche_videos(yt, ["kubernetes", "terraform"], {"search_days": 7, "exclude_terms": ["roblox"]},
                        datetime(2026, 11, 17, tzinfo=timezone.utc))
    assert [v["id"] for v in vids] == ["kubernetes-1", "shared", "terraform-1"]
    assert all(s["publishedAfter"] == "2026-11-10T00:00:00Z" and s["order"] == "viewCount" for s in searches)


def test_upload_login_must_match_configured_channel(tmp_path):
    yt = YouTube({"channel_id": "UC_expected"}, tmp_path)
    yt._svc = SimpleNamespace(channels=lambda: _Endpoint(
        lambda p: {"items": [{"id": "UC_other", "snippet": {"title": "Other"}}]}))
    with pytest.raises(RuntimeError, match="config expects UC_expected"):
        yt.verify_channel()
