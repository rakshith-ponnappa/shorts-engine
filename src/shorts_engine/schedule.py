"""Daily volume ramp and publish-time slotting."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo
from typing import Iterable

from .config import Campaign


def daily_quota(campaign: Campaign, day: date) -> int:
    if day < campaign.start_date or day > campaign.end_date:
        return 0
    r = campaign.ramp
    if r.ramp_days <= 1 or r.max_per_day <= r.start_per_day:
        return r.max_per_day
    progress = min((day - campaign.start_date).days, r.ramp_days - 1) / (r.ramp_days - 1)
    return round(r.start_per_day + (r.max_per_day - r.start_per_day) * progress)


def split_mix(total: int, mix: dict[str, float]) -> dict[str, int]:
    """Split total across kinds by weight using largest-remainder rounding."""
    weights = {k: max(float(v), 0.0) for k, v in mix.items()}
    s = sum(weights.values())
    if total <= 0 or s == 0:
        return {k: 0 for k in mix}
    raw = {k: total * w / s for k, w in weights.items()}
    out = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)[: total - sum(out.values())]:
        out[k] += 1
    return out


def publish_slots(
    day: date,
    n: int,
    window: tuple[time, time],
    min_gap_minutes: int,
    tz: tzinfo,
    now: datetime,
    taken: Iterable[datetime] = (),
    lead_minutes: int = 20,
) -> list[datetime]:
    """Spread up to n publish times evenly over the window, respecting a min gap and existing slots."""
    if n <= 0:
        return []
    start = max(datetime.combine(day, window[0], tz), now + timedelta(minutes=lead_minutes))
    end = datetime.combine(day, window[1], tz)
    if start >= end:
        return []
    gap = timedelta(minutes=min_gap_minutes)
    step = max((end - start) / n, gap)
    taken = list(taken)
    slots: list[datetime] = []
    t = start
    while t <= end and len(slots) < n:
        if all(abs(t - x) >= gap for x in taken + slots):
            slots.append(t.replace(microsecond=0))
            t += step
        else:
            t += gap
    return slots
