from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import load_config


def _pipeline(args, dry_run: bool = False):
    from .pipeline import Pipeline

    return Pipeline(load_config(args.config), dry_run=dry_run)


def cmd_plan(args) -> None:
    from .schedule import daily_quota, split_mix

    cfg = load_config(args.config)
    c = cfg.campaign
    total, d = 0, c.start_date
    print(f"{'date':<12}{'quota':>6}{'clips':>7}{'orig':>6}")
    while d <= c.end_date:
        q = daily_quota(c, d)
        m = split_mix(q, c.mix)
        total += q
        print(f"{d.isoformat():<12}{q:>6}{m.get('clip', 0):>7}{m.get('ai_original', 0):>6}")
        d += timedelta(days=1)
    print(f"{'total':<12}{total:>6}")


def cmd_auth(args) -> None:
    from .youtube import YouTube

    cfg = load_config(args.config)
    yt = YouTube(cfg.youtube, cfg.root)
    if not yt.client_secrets.exists():
        downloads = sorted(Path.home().glob("Downloads/client_secret_*.json"), key=lambda p: p.stat().st_mtime)
        if downloads:
            yt.client_secrets.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.move(str(downloads[-1]), yt.client_secrets)
            yt.client_secrets.chmod(0o600)
            print(f"Moved {downloads[-1].name} -> {yt.client_secrets}")
    yt.credentials(interactive=True, force=True)
    try:
        ch = yt.verify_channel()
    except RuntimeError:
        yt.token_file.unlink(missing_ok=True)  # never keep a login for the wrong channel
        raise
    print(f"Authorised. Uploads will go to: {ch['title']} {ch['handle']} ({ch['id']})")


def cmd_doctor(args) -> None:
    import math
    import os
    from zoneinfo import ZoneInfo

    from . import media
    from .schedule import daily_quota
    from .youtube import YouTube

    cfg = load_config(args.config)
    failures = 0

    def check(label: str, passed: bool, hint: str = "", required: bool = True) -> None:
        nonlocal failures
        mark = "ok" if passed else ("!!" if required else "--")
        print(f"[{mark}] {label}" + (f"\n     -> {hint}" if hint and not passed else ""))
        failures += 0 if passed or not required else 1

    try:
        ff, _ = media.resolve_ffmpeg(cfg.render.get("ffmpeg_bin", "auto"))
        check(f"ffmpeg with captions support: {ff}", True)
    except RuntimeError as e:
        check("ffmpeg with captions support", False, str(e))

    c = cfg.campaign
    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    check(f"campaign {c.start_date} -> {c.end_date}; today's quota {daily_quota(c, today)}",
          c.start_date <= today <= c.end_date, "today is outside the campaign dates", required=False)

    channel_id = cfg.youtube.get("channel_id", "")
    check(f"target channel id set: {channel_id or '-'}", bool(channel_id), "set youtube.channel_id in config.yaml")
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        check("YOUTUBE_API_KEY in .env", False, "run: deploy/gcloud-setup.sh <google-account-email> [project-id]")
    else:
        try:
            from googleapiclient.discovery import build

            items = build("youtube", "v3", developerKey=key, cache_discovery=False).channels().list(
                part="snippet", id=channel_id or "UC_x5XG1OV2P6uZZ5FSM9Ttw").execute().get("items", [])
            check(f"API key works (channel lookup: {items[0]['snippet']['title'] if items else 'not found'})",
                  bool(items), "channel_id not found; check config")
        except Exception as e:  # noqa: BLE001
            check("API key works", False, str(e)[:300])

    kws = cfg.trends.get("search_keywords", [])
    per_day = len(kws) * math.ceil(24 / float(cfg.trends.get("refresh_hours", 6)))
    check(f"trend searches per day ~{per_day} (YouTube limit 100)", per_day <= 100,
          "use fewer trends.search_keywords or a larger trends.refresh_hours")

    provider = (cfg.llm.get("provider") or "none").lower()
    env_key = {"openai": "OPENAI_API_KEY", "azure": "AZURE_OPENAI_API_KEY"}.get(provider)
    check(f"LLM provider: {provider}", bool(env_key and os.environ.get(env_key)),
          "set llm.provider (openai|azure) and its key in .env; without it AI originals and commentary are off",
          required=False)

    yt = YouTube(cfg.youtube, cfg.root)
    check(f"OAuth client file: {yt.client_secrets}", yt.client_secrets.exists(),
          "Cloud Console > Google Auth Platform > Clients > Create client > Desktop app > Download JSON")
    if yt.token_file.exists():
        try:
            ch = yt.verify_channel()
            check(f"upload login -> {ch['title']} {ch['handle']} ({ch['id']})", True)
        except Exception as e:  # noqa: BLE001
            check("upload login", False, str(e)[:300])
    else:
        check("upload login", False, "run: shorts-engine auth")

    from .sources import VIDEO_EXT

    n = sum(1 for p in cfg.inbox_dir.rglob("*") if p.suffix.lower() in VIDEO_EXT)
    check(f"{n} source video(s) in {cfg.inbox_dir}", n > 0,
          "add your long-form videos here; each yields up to "
          f"{c.max_clips_per_source_per_day} clips/day", required=False)
    print("\nReminder: until the YouTube API audit is approved, API uploads stay private.")
    if failures:
        sys.exit(1)


def cmd_trends(args) -> None:
    from .db import DB
    from .trends import refresh_trends

    cfg = load_config(args.config)
    for kw, score in refresh_trends(DB(cfg.db_path), cfg.trends)[: args.top]:
        print(f"{score:8.1f}  {kw}")


def cmd_sources(args) -> None:
    from .db import DB
    from .sources import sync_sources

    cfg = load_config(args.config)
    db = DB(cfg.db_path)
    print(f"{sync_sources(cfg, db)} new")
    for r in db.q("SELECT id, kind, source_key, title, duration FROM sources ORDER BY id"):
        print(f"#{r['id']:<4} {r['kind']:<9} {r['source_key']:<16} {r['duration'] or 0:7.0f}s  {r['title']}")


def _print_report(r) -> None:
    print(f"\n{r.day}: quota {r.quota}, already scheduled {r.already_scheduled}")
    print(f"produced: {r.produced}  uploaded: {r.uploaded}")
    if r.rejected:
        print(f"rejected by guardrails: {r.rejected}")
    for when, title in r.planned:
        print(f"  [dry-run] {when}  {title}")
    for n in r.notes:
        print(f"  - {n}")


def cmd_run(args) -> None:
    p = _pipeline(args, dry_run=args.dry_run)
    day = date.fromisoformat(args.day) if args.day else None
    _print_report(p.run_day(day))


def cmd_review(args) -> None:
    from .db import DB

    cfg = load_config(args.config)
    db = DB(cfg.db_path)
    for cid in args.approve or []:
        db.update("clips", cid, status="ready")
    for cid in args.reject or []:
        db.update("clips", cid, status="rejected")
    if args.approve_all:
        with db.tx() as c:
            c.execute("UPDATE clips SET status = 'ready' WHERE status = 'needs_review'")
    rows = db.q("SELECT id, kind, title, file_path, transcript FROM clips WHERE status = 'needs_review' ORDER BY id")
    if not rows:
        print("Review queue is empty.")
    for r in rows:
        print(f"\n#{r['id']} [{r['kind']}] {r['title']}\n  file: {r['file_path']}\n  script: {(r['transcript'] or '')[:400]}")


def cmd_status(args) -> None:
    p = _pipeline(args)
    from .schedule import daily_quota

    day = p.today()
    print(f"today {day}: quota {daily_quota(p.cfg.campaign, day)}, scheduled {len(p.scheduled_on(day))}")
    print(f"ready inventory: {p.inventory()}")
    counts = {r['status']: r['n'] for r in p.db.q("SELECT status, COUNT(*) n FROM clips GROUP BY status")}
    print(f"all clips by status: {counts}")
    top = p.db.q("""SELECT c.title, MAX(s.views) v FROM clips c JOIN stats s ON s.clip_id = c.id
                    GROUP BY c.id ORDER BY v DESC LIMIT 5""")
    if top:
        print("top performers:")
        for r in top:
            print(f"  {r['v']:>9,}  {r['title']}")


def cmd_daemon(args) -> None:
    """Run once now, then daily at --at (local time) until the campaign ends."""
    p = _pipeline(args)
    log = logging.getLogger("daemon")
    hh, mm = map(int, args.at.split(":"))
    while True:
        try:
            _print_report(p.run_day())
        except Exception:  # noqa: BLE001 - keep the daemon alive; next run retries
            log.exception("daily run failed")
        now = datetime.now(p.tz)
        if now.date() > p.cfg.campaign.end_date:
            log.info("campaign finished")
            return
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        if args.every_hours > 0:
            nxt = min(nxt, now + timedelta(hours=args.every_hours))
        log.info("next run at %s", nxt)
        time.sleep((nxt - now).total_seconds())


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="shorts-engine")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("plan", help="print the daily volume ramp for the campaign").set_defaults(fn=cmd_plan)
    sub.add_parser("auth", help="authorise YouTube uploads (OAuth) and confirm the channel").set_defaults(fn=cmd_auth)
    sub.add_parser("doctor", help="check every setup step and say what's missing").set_defaults(fn=cmd_doctor)
    t = sub.add_parser("trends", help="refresh and show trending keywords")
    t.add_argument("--top", type=int, default=25)
    t.set_defaults(fn=cmd_trends)
    sub.add_parser("sources", help="sync the rights registry and list sources").set_defaults(fn=cmd_sources)
    r = sub.add_parser("run", help="run today's pipeline once")
    r.add_argument("--dry-run", action="store_true", help="render but don't upload")
    r.add_argument("--day", help="YYYY-MM-DD (defaults to today)")
    r.set_defaults(fn=cmd_run)
    rv = sub.add_parser("review", help="approve/reject AI originals")
    rv.add_argument("--approve", type=int, nargs="*")
    rv.add_argument("--reject", type=int, nargs="*")
    rv.add_argument("--approve-all", action="store_true")
    rv.set_defaults(fn=cmd_review)
    sub.add_parser("status", help="inventory, today's schedule, top performers").set_defaults(fn=cmd_status)
    d = sub.add_parser("daemon", help="run daily until the campaign ends")
    d.add_argument("--at", default="06:30", help="local time for the daily run")
    d.add_argument("--every-hours", type=float, default=3,
                   help="also top up every N hours (picks up newly approved originals); 0 = daily only")
    d.set_defaults(fn=cmd_daemon)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        args.fn(args)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
