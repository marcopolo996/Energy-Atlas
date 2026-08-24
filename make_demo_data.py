"""
Generate synthetic data so the dashboard can be viewed before the ENTSO-E token
is available. Run it from the Actions tab via the "Load demo data" workflow.

The numbers are invented. They are shaped to look like European day-ahead
curves, with a winter price level, a summer solar depression around midday, an
evening peak and occasional negative hours, but they are not real prices and
must not be read as such. Every file it writes carries a demo flag, the page
shows a warning banner while that flag is present, and the first real
collection run deletes all of it.
"""

from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fetch_prices import (
    DATA,
    DURATION_H,
    MAX_CYCLES,
    ROUND_TRIP,
    SCALAR_FIELDS,
    ZONES,
    compute_metrics,
)

HISTORY_DAYS = 540
RECENT_DAYS = 60

# Zones with a heavier solar fleet get a deeper midday depression.
SOLAR_WEIGHT = {"ES": 0.60, "IT_SUD": 0.55, "IT_CSUD": 0.50, "IT_NORD": 0.30}


def synthetic_day(day: date, zone_id: str, rng: random.Random) -> list[float]:
    doy = day.timetuple().tm_yday
    winter = math.cos((doy - 15) / 365 * 2 * math.pi)
    summer = max(0.0, math.cos((doy - 172) / 365 * 2 * math.pi))
    weekend = day.weekday() >= 5

    level = 78 + 26 * winter + rng.gauss(0, 12) - (9 if weekend else 0)
    solar = SOLAR_WEIGHT.get(zone_id, 0.28) * summer
    windy = rng.random() < 0.18  # occasional cheap, volatile day

    prices = []
    for h in range(24):
        shape = (
            -18 * math.cos((h - 3) / 24 * 2 * math.pi)          # overnight trough
            + 32 * math.exp(-((h - 19) ** 2) / 6)               # evening peak
            + 14 * math.exp(-((h - 8) ** 2) / 5)                # morning ramp
            - 105 * solar * math.exp(-((h - 13) ** 2) / 9)      # midday depression
        )
        if windy:
            shape -= 34 * math.exp(-((h - 4) ** 2) / 20)
        prices.append(round(level + shape + rng.gauss(0, 7), 2))
    return prices


def main() -> int:
    rng = random.Random(20260824)
    DATA.mkdir(exist_ok=True)

    today = date.today()
    history_days: dict[str, dict] = {}
    recent_days: dict[str, dict] = {}

    for offset in range(HISTORY_DAYS):
        day = today - timedelta(days=offset)
        key = day.isoformat()
        for zone in ZONES:
            metrics = compute_metrics(synthetic_day(day, zone["id"], rng))
            history_days.setdefault(key, {})[zone["id"]] = [metrics[f] for f in SCALAR_FIELDS]
            if offset < RECENT_DAYS:
                recent_days.setdefault(key, {})[zone["id"]] = metrics

    history = {"demo": True, "fields": SCALAR_FIELDS, "days": dict(sorted(history_days.items()))}
    recent = {
        "demo": True,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": today.isoformat(),
        "assumptions": {
            "duration_h": DURATION_H,
            "round_trip_efficiency": ROUND_TRIP,
            "max_cycles": MAX_CYCLES,
            "peak_definition": "08:00-20:00 local",
        },
        "zones": [{"id": z["id"], "label": z["label"]} for z in ZONES],
        "days": dict(sorted(recent_days.items())),
    }

    (DATA / "history.json").write_text(json.dumps(history, separators=(",", ":")))
    (DATA / "recent.json").write_text(json.dumps(recent, separators=(",", ":")))

    print(f"Wrote demo data: {HISTORY_DAYS} days of figures, {RECENT_DAYS} days of hourly detail.")
    print("These numbers are invented. The first real collection run replaces them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
