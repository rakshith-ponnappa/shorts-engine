from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("yaml")  # shorts_engine.config needs PyYAML

from shorts_engine.config import Campaign, Ramp  # noqa: E402
from shorts_engine.schedule import daily_quota, publish_slots, split_mix  # noqa: E402

CAMPAIGN = Campaign(start_date=date(2026, 11, 16), end_date=date(2026, 12, 4),
                    ramp=Ramp(start_per_day=10, max_per_day=100, ramp_days=14))


def test_ramp_hits_start_and_max_and_stops_after_end():
    assert daily_quota(CAMPAIGN, date(2026, 11, 15)) == 0
    assert daily_quota(CAMPAIGN, date(2026, 11, 16)) == 10
    assert 10 < daily_quota(CAMPAIGN, date(2026, 11, 22)) < 100
    assert daily_quota(CAMPAIGN, date(2026, 11, 29)) == 100
    assert daily_quota(CAMPAIGN, date(2026, 12, 4)) == 100
    assert daily_quota(CAMPAIGN, date(2026, 12, 5)) == 0


def test_split_mix_preserves_total():
    assert split_mix(33, {"clip": 0.7, "ai_original": 0.3}) == {"clip": 23, "ai_original": 10}
    assert sum(split_mix(7, {"a": 1, "b": 1, "c": 1}).values()) == 7


def test_publish_slots_respect_window_gap_lead_and_taken():
    tz = ZoneInfo("Asia/Kolkata")
    now = datetime(2026, 11, 16, 14, 30, tzinfo=tz)
    taken = [datetime(2026, 11, 16, 15, 0, tzinfo=tz)]
    slots = publish_slots(date(2026, 11, 16), 200, (time(8), time(23)), 8, tz, now, taken)
    assert slots[0] >= now + timedelta(minutes=20)
    assert slots[-1] <= datetime(2026, 11, 16, 23, 0, tzinfo=tz)
    everything = sorted(slots + taken)
    assert all(b - a >= timedelta(minutes=8) for a, b in zip(everything, everything[1:]))
    assert len(slots) < 200  # the window physically can't hold 200 at an 8-minute gap


def test_publish_slots_empty_when_window_passed():
    tz = ZoneInfo("UTC")
    now = datetime(2026, 11, 16, 23, 30, tzinfo=tz)
    assert publish_slots(date(2026, 11, 16), 5, (time(8), time(23)), 8, tz, now) == []


def test_default_ramp_is_conservative():
    campaign = Campaign(start_date=date(2026, 1, 1), end_date=date(2026, 3, 1))
    quotas = [daily_quota(campaign, campaign.start_date + timedelta(days=i)) for i in range(60)]
    assert quotas[0] == 1
    assert max(quotas) == 3 and quotas[13] == 3
    assert quotas == sorted(quotas)  # never decreases while ramping


@pytest.mark.parametrize("ramp", [Ramp(start_per_day=2, max_per_day=5, ramp_days=1),
                                  Ramp(start_per_day=5, max_per_day=5, ramp_days=14),
                                  Ramp(start_per_day=8, max_per_day=5, ramp_days=14)])
def test_degenerate_ramp_uses_max_from_day_one(ramp):
    campaign = Campaign(start_date=date(2026, 1, 1), end_date=date(2026, 1, 10), ramp=ramp)
    assert daily_quota(campaign, date(2026, 1, 1)) == 5
