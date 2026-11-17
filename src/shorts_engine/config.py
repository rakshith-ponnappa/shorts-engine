from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Ramp:
    start_per_day: int = 1
    max_per_day: int = 3
    ramp_days: int = 14


@dataclass
class Campaign:
    start_date: date
    end_date: date
    ramp: Ramp = field(default_factory=Ramp)
    publish_window: tuple[time, time] = (time(8, 0), time(23, 0))
    min_minutes_between_uploads: int = 8
    mix: dict[str, float] = field(default_factory=lambda: {"clip": 0.7, "ai_original": 0.3})
    max_clips_per_source_per_day: int = 6


@dataclass
class SourceEntry:
    key: str
    kind: str  # own | licensed
    path: str | None = None
    urls: list[str] = field(default_factory=list)
    permission_ref: str = ""
    attribution: str = ""


@dataclass
class Config:
    root: Path
    timezone: str
    campaign: Campaign
    data_dir: Path
    inbox_dir: Path
    assets_dir: Path
    sources: list[SourceEntry]
    trends: dict[str, Any]
    clip: dict[str, Any]
    render: dict[str, Any]
    llm: dict[str, Any]
    whisper: dict[str, Any]
    youtube: dict[str, Any]
    guardrails: dict[str, Any]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "shorts.db"

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p


def _parse_time(value: str) -> time:
    h, m = str(value).split(":")
    return time(int(h), int(m))


def _as_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def load_env(path: Path) -> None:
    """Minimal .env loader; existing environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


def load_config(path: str | Path = "config.yaml") -> Config:
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Copy config.example.yaml to config.yaml first.")
    root = path.parent
    load_env(root / ".env")
    raw = yaml.safe_load(path.read_text()) or {}

    c = raw.get("campaign", {})
    window = c.get("publish_window", ["08:00", "23:00"])
    campaign = Campaign(
        start_date=_as_date(c["start_date"]),
        end_date=_as_date(c["end_date"]),
        ramp=Ramp(**c.get("ramp", {})),
        publish_window=(_parse_time(window[0]), _parse_time(window[1])),
        min_minutes_between_uploads=int(c.get("min_minutes_between_uploads", 8)),
        mix=c.get("mix", {"clip": 0.7, "ai_original": 0.3}),
        max_clips_per_source_per_day=int(c.get("max_clips_per_source_per_day", 6)),
    )
    if campaign.end_date < campaign.start_date:
        raise ValueError("campaign.end_date is before campaign.start_date")

    paths = raw.get("paths", {})

    def rp(key: str, default: str) -> Path:
        p = Path(paths.get(key, default))
        return p if p.is_absolute() else root / p

    sources = [SourceEntry(**s) for s in raw.get("sources", [])]
    for s in sources:
        if s.kind not in ("own", "licensed"):
            raise ValueError(f"source {s.key}: kind must be 'own' or 'licensed'")
        if s.kind == "licensed" and not s.permission_ref.strip():
            raise ValueError(f"source {s.key}: licensed sources need a permission_ref")

    cfg = Config(
        root=root,
        timezone=raw.get("timezone", "UTC"),
        campaign=campaign,
        data_dir=rp("data_dir", "data"),
        inbox_dir=rp("inbox_dir", "data/inbox"),
        assets_dir=rp("assets_dir", "assets"),
        sources=sources,
        trends=raw.get("trends", {}),
        clip=raw.get("clip", {}),
        render=raw.get("render", {}),
        llm=raw.get("llm", {"provider": "none"}),
        whisper=raw.get("whisper", {}),
        youtube=raw.get("youtube", {}),
        guardrails=raw.get("guardrails", {}),
    )
    for d in (cfg.data_dir, cfg.inbox_dir, cfg.data_dir / "renders", cfg.data_dir / "sources",
              cfg.data_dir / "transcripts", cfg.data_dir / "tts"):
        d.mkdir(parents=True, exist_ok=True)
    return cfg
