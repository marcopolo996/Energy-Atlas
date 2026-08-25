"""
Generate synthetic data so the Atlas can be viewed before the ENTSO-E token is
available. Run it from the Actions tab via the "Load demo data" workflow.

The numbers are invented. They are shaped to look like European day-ahead
curves, with a winter price level, a summer midday solar depression, an evening
peak, an intra-hour sawtooth and occasional negative quarter-hours, but they
are not real prices. Every file carries a demo flag, the pages show a warning
while it is present, and the first real collection run deletes all of it.
"""

from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from collect import (ANCILLARY_ZONES, DATA, DURATION_H, MAX_CYCLES, RESERVES,
                     ROUND_TRIP, SCALAR_FIELDS, ZONES, compute)

HISTORY_DAYS = 540
RECENT_DAYS = 60
SOLAR = {"ES": 0.62, "IT_SUD": 0.58, "IT_CSUD": 0.52, "IT_NORD": 0.30, "DE_LU": 0.30}
WIND = {"DE_LU": 0.55, "ES": 0.35, "FR": 0.30, "IT_SUD": 0.28, "AT": 0.22}
# Switzerland stays on hourly market time units until November 2026.
HOURLY_ZONES = {"CH"}


def day_series(day: date, zone_id: str, rng: random.Random):
    """
    One synthetic day, built the way merit order actually works: renewable
    output is generated first, then pushed into the price. That is what creates
    the negative correlation between output and price, and therefore capture
    rates below baseload. A price curve invented independently of the
    generation curve would capture exactly baseload and teach the reader
    nothing.
    """
    doy = day.timetuple().tm_yday
    winter = math.cos((doy - 15) / 365 * 2 * math.pi)
    summer = max(0.0, math.cos((doy - 172) / 365 * 2 * math.pi))
    n = 24 if zone_id in HOURLY_ZONES else 96
    step = 24 / n

    level = 96 + 26 * winter + rng.gauss(0, 11) - (9 if day.weekday() >= 5 else 0)
    pv_cap = 46000 * SOLAR.get(zone_id, 0.25) * (0.12 + 0.88 * summer)
    wind_cap = 32000 * WIND.get(zone_id, 0.18)
    load_factor = 0.15 + 0.85 * rng.random() ** 1.4      # how windy this day is
    phase = rng.uniform(0, 6.3)

    prices, solar, wind = [], [], []
    for i in range(n):
        h = i * step
        s = max(0.0, pv_cap * math.exp(-((h - 13) ** 2) / 11) + rng.gauss(0, 220))
        w = max(0.0, wind_cap * load_factor * (1 + 0.45 * math.sin(h / 4.2 + phase)) + rng.gauss(0, 450))
        demand = (-18 * math.cos((h - 3) / 24 * 2 * math.pi)
                  + 32 * math.exp(-((h - 19) ** 2) / 6)
                  + 14 * math.exp(-((h - 8) ** 2) / 5))
        merit = 0.0026 * (s + 0.85 * w)                  # renewables push price down
        sawtooth = (i % 4 - 1.5) * 2.8 * (1 + s / 22000) if n == 96 else 0.0
        prices.append(round(level + demand - merit + sawtooth + rng.gauss(0, 5), 2))
        solar.append(round(s, 1))
        wind.append(round(w, 1))
    return prices, solar, wind


def main() -> int:
    rng = random.Random(20260825)
    DATA.mkdir(exist_ok=True)
    today = date.today()
    history_days, recent_days, anc_days = {}, {}, {}

    for offset in range(HISTORY_DAYS):
        day = today - timedelta(days=offset)
        key = day.isoformat()
        for zone in ZONES:
            metrics = compute(*day_series(day, zone["id"], rng))
            history_days.setdefault(key, {})[zone["id"]] = [metrics[f] for f in SCALAR_FIELDS]
            if offset < RECENT_DAYS:
                recent_days.setdefault(key, {})[zone["id"]] = metrics
        # Ancillary capacity, drifting downwards over time as it has in reality.
        decay = 1.0 + offset / 420
        for zone_id in ANCILLARY_ZONES:
            row = {}
            for reserve, base in zip(RESERVES, (9.0, 14.0, 5.0)):
                mean = max(0.4, base * decay * (0.75 + 0.5 * rng.random()))
                row[reserve["id"]] = [round(mean, 2), round(mean * 0.6, 2), round(mean * 1.7, 2)]
            anc_days.setdefault(key, {})[zone_id] = row

    meta = {
        "demo": True,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": today.isoformat(),
        "assumptions": {"duration_h": DURATION_H, "round_trip_efficiency": ROUND_TRIP,
                        "max_cycles": MAX_CYCLES, "peak_definition": "08:00-20:00 local"},
        "zones": [{"id": z["id"], "label": z["label"]} for z in ZONES],
        "reserves": RESERVES,
    }
    history = {"demo": True, "fields": SCALAR_FIELDS, "days": dict(sorted(history_days.items()))}
    recent = {**meta, "days": dict(sorted(recent_days.items()))}
    ancillary = {"demo": True, "generated_utc": meta["generated_utc"], "reserves": RESERVES,
                 "zones": [z for z in meta["zones"] if z["id"] in ANCILLARY_ZONES],
                 "days": dict(sorted(anc_days.items()))}

    for name, blob in (("history.json", history), ("recent.json", recent),
                       ("ancillary.json", ancillary)):
        (DATA / name).write_text(json.dumps(blob, separators=(",", ":")))
        print(f"{name}: {(DATA / name).stat().st_size // 1024} KB")

    print(f"Wrote demo data: {HISTORY_DAYS} days of figures, {RECENT_DAYS} days of detail.")
    print("These numbers are invented. The first real collection run replaces them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
