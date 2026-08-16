"""Billing-cycle tracking: known cycles from the EAC app + estimated future ones.

Cycles are contiguous (one period's `end` equals the next period's `start`) and
`end` is exclusive, matching how the EAC portal displays them.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / "billing_periods.json"


def load_periods(path: Path = DEFAULT_PATH) -> list[dict]:
    with open(path) as f:
        raw = json.load(f)
    periods = [
        {
            "start": dt.date.fromisoformat(p["start"]),
            "end": dt.date.fromisoformat(p["end"]),
            "source": p["source"],
        }
        for p in raw
    ]
    return sorted(periods, key=lambda p: p["start"])


def save_periods(periods: list[dict], path: Path = DEFAULT_PATH) -> None:
    raw = [
        {"start": p["start"].isoformat(), "end": p["end"].isoformat(), "source": p["source"]}
        for p in sorted(periods, key=lambda p: p["start"])
    ]
    with open(path, "w") as f:
        json.dump(raw, f, indent=2)


def _estimated_length(periods: list[dict], lookback: int = 6) -> int:
    lengths = sorted((p["end"] - p["start"]).days for p in periods[-lookback:])
    mid = len(lengths) // 2
    if len(lengths) % 2:
        return lengths[mid]
    return round((lengths[mid - 1] + lengths[mid]) / 2)


def ensure_coverage(periods: list[dict], today: dt.date) -> list[dict]:
    """Append estimated cycles, contiguous with the last known one, until `today`
    falls inside the range covered by `periods`."""
    periods = list(periods)
    while periods[-1]["end"] <= today:
        length = _estimated_length(periods)
        start = periods[-1]["end"]
        periods.append({"start": start, "end": start + dt.timedelta(days=length), "source": "estimated"})
    return periods


def find_period_index(periods: list[dict], target: dt.date) -> int | None:
    for i, p in enumerate(periods):
        if p["start"] <= target < p["end"]:
            return i
    return None
