"""Sync the rights registry in config into the sources table and fetch licensed media."""
from __future__ import annotations

import logging
from pathlib import Path

from . import media
from .config import Config, SourceEntry
from .db import DB, now_iso

log = logging.getLogger(__name__)
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def _register(db: DB, entry: SourceEntry, origin: str, file_path: Path, title: str, ffprobe: str) -> None:
    try:
        dur = media.duration(file_path, ffprobe)
    except Exception as e:  # noqa: BLE001 - unreadable media is skipped, not fatal
        log.warning("skipping %s: cannot probe (%s)", file_path, e)
        return
    db.insert("sources", {
        "source_key": entry.key, "kind": entry.kind, "permission_ref": entry.permission_ref,
        "attribution": entry.attribution, "origin": origin, "file_path": str(file_path),
        "title": title, "duration": dur, "added_at": now_iso(),
    })
    log.info("registered %s source: %s (%.0fs)", entry.kind, title, dur)


def _download(url: str, dest: Path) -> tuple[Path, str]:
    try:
        import yt_dlp
    except ImportError as e:
        raise RuntimeError("pip install 'shorts-engine[download]' to fetch licensed URLs") from e
    opts = {
        "outtmpl": str(dest / "%(id)s.%(ext)s"),
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info)).with_suffix(".mp4"), info.get("title", url)


def sync_sources(cfg: Config, db: DB) -> int:
    _, ffprobe = media.resolve_ffmpeg(cfg.render.get("ffmpeg_bin", "auto"))
    known = {r["origin"] for r in db.q("SELECT origin FROM sources")}
    before = len(known)
    for entry in cfg.sources:
        if entry.path:
            folder = cfg.path(entry.path)
            folder.mkdir(parents=True, exist_ok=True)
            for f in sorted(folder.rglob("*")):
                if f.suffix.lower() in VIDEO_EXT and str(f.resolve()) not in known:
                    _register(db, entry, str(f.resolve()), f.resolve(), f.stem, ffprobe)
        for url in entry.urls:
            if url in known:
                continue
            if entry.kind != "licensed" or not entry.permission_ref.strip():
                log.warning("refusing to download %s: no recorded permission", url)
                continue
            try:
                path, title = _download(url, cfg.data_dir / "sources" / entry.key)
            except Exception as e:  # noqa: BLE001
                log.error("download failed for %s: %s", url, e)
                continue
            _register(db, entry, url, path, title, ffprobe)
    return len(db.q("SELECT id FROM sources")) - before
