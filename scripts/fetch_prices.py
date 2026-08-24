"""
Day-ahead power price collector and storage-economics calculator.

Pulls settled day-ahead prices from the ENTSO-E Transparency Platform for a set
of European bidding zones, derives a small number of metrics that matter for
battery storage and general market monitoring, and writes them to JSON files
that the dashboard page reads.

Runs on GitHub Actions. Requires one secret: ENTSOE_TOKEN.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://web-api.tp.entsoe.eu/api"

# ---------------------------------------------------------------------------
# Configuration. Edit this block to add or remove markets.
# The "eic" value is the ENTSO-E bidding zone code. Verify a new code on the
# Transparency Platform before adding it: a wrong code returns no data rather
# than an error.
# ---------------------------------------------------------------------------
ZONES = [
    {"id": "DE_LU",   "label": "Germany / Luxembourg", "eic": "10Y1001A1001A82H", "tz": "Europe/Berlin"},
    {"id": "IT_NORD", "label": "Italy North",          "eic": "10Y1001A1001A73I", "tz": "Europe/Rome"},
    {"id": "IT_CSUD", "label": "Italy Centre-South",   "eic": "10Y1001A1001A71M", "tz": "Europe/Rome"},
    {"id": "IT_SUD",  "label": "Italy South",          "eic": "10Y1001A1001A788", "tz": "Europe/Rome"},
    {"id": "AT",      "label": "Austria",              "eic": "10YAT-APG------L", "tz": "Europe/Vienna"},
    {"id": "ES",      "label": "Spain",                "eic": "10YES-REE------0", "tz": "Europe/Madrid"},
    {"id": "FR",      "label": "France",               "eic": "10YFR-RTE------C", "tz": "Europe/Paris"},
    {"id": "CH",      "label": "Switzerland",          "eic": "10YCH-SWISSGRIDZ", "tz": "Europe/Zurich"},
]

# Battery assumptions used for the arbitrage calculation.
DURATION_H = 2.0          # hours of storage at rated power
ROUND_TRIP = 0.85         # round-trip efficiency
MAX_CYCLES = 2.0          # equivalent full cycles allowed per day
SOC_STEPS = 8             # state-of-charge granularity in the optimiser

# How many days back to refresh on every run. Re-fetching a few days repairs
# any gaps left by a failed run or a late publication.
LOOKBACK_DAYS = 4

# Two stores, because the hourly price arrays are bulky and only the recent ones
# are ever displayed. history.json holds daily scalars in a compact array form
# for the trend charts; recent.json holds full hourly detail for the day strips.
HISTORY_LIMIT_DAYS = 900
RECENT_LIMIT_DAYS = 60

SCALAR_FIELDS = [
    "base", "peak", "offpeak", "min", "max", "spread",
    "spread_2h", "volatility", "negative_hours", "arb_revenue", "cycles",
]

PEAK_HOURS = range(8, 20)  # 08:00-20:00 local, the conventional European peak

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


# ---------------------------------------------------------------------------
# ENTSO-E access
# ---------------------------------------------------------------------------

def local_day_bounds(day: date, tz_name: str) -> tuple[str, str]:
    """Return the UTC request window covering one full local calendar day."""
    tz = ZoneInfo(tz_name)
    start_local = datetime(day.year, day.month, day.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    fmt = "%Y%m%d%H%M"
    return (
        start_local.astimezone(timezone.utc).strftime(fmt),
        end_local.astimezone(timezone.utc).strftime(fmt),
    )


def strip_ns(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def request_prices(token: str, eic: str, start: str, end: str) -> str | None:
    params = {
        "securityToken": token,
        "documentType": "A44",          # day-ahead prices
        "in_Domain": eic,
        "out_Domain": eic,
        "periodStart": start,
        "periodEnd": end,
    }
    url = f"{API}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # 400 with "No matching data found" is normal for a day not yet published.
        body = exc.read().decode("utf-8", errors="replace")[:200]
        print(f"    HTTP {exc.code}: {body}", file=sys.stderr)
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"    request failed: {exc}", file=sys.stderr)
        return None


def parse_series(xml_text: str) -> dict[datetime, float]:
    """
    Flatten every TimeSeries in the document into {utc_timestamp: price}.

    Two details of the ENTSO-E format matter here. Periods carry a resolution
    that may be hourly, half-hourly or quarter-hourly, and a position may be
    omitted when its value repeats the previous one, so gaps are forward-filled.
    """
    out: dict[datetime, float] = {}
    root = ET.fromstring(xml_text)

    for period in root.iter():
        if strip_ns(period.tag) != "Period":
            continue

        resolution = None
        period_start = None
        points: dict[int, float] = {}

        for child in period:
            name = strip_ns(child.tag)
            if name == "resolution":
                resolution = (child.text or "").strip()
            elif name == "timeInterval":
                for sub in child:
                    if strip_ns(sub.tag) == "start":
                        period_start = datetime.strptime(
                            (sub.text or "").strip(), "%Y-%m-%dT%H:%MZ"
                        ).replace(tzinfo=timezone.utc)
            elif name == "Point":
                pos, price = None, None
                for sub in child:
                    sub_name = strip_ns(sub.tag)
                    if sub_name == "position":
                        pos = int((sub.text or "0").strip())
                    elif sub_name in ("price.amount", "price_amount"):
                        price = float((sub.text or "0").strip())
                if pos is not None and price is not None:
                    points[pos] = price

        if not points or period_start is None:
            continue

        minutes = {"PT60M": 60, "PT30M": 30, "PT15M": 15}.get(resolution or "PT60M", 60)
        step = timedelta(minutes=minutes)

        last = None
        for pos in range(1, max(points) + 1):
            if pos in points:
                last = points[pos]
            if last is None:
                continue
            out[period_start + step * (pos - 1)] = last

    return out


def to_local_hours(series: dict[datetime, float], day: date, tz_name: str) -> list[float] | None:
    """
    Collapse the raw series into one average price per local clock hour.

    Sub-hourly resolutions are averaged up so that every zone and every date is
    directly comparable. A day is accepted only if at least 20 hours are present.
    """
    tz = ZoneInfo(tz_name)
    buckets: dict[int, list[float]] = {}

    for ts, price in series.items():
        local = ts.astimezone(tz)
        if local.date() != day:
            continue
        buckets.setdefault(local.hour, []).append(price)

    if len(buckets) < 20:
        return None

    hours = sorted(buckets)
    return [round(statistics.fmean(buckets[h]), 2) for h in hours]


# ---------------------------------------------------------------------------
# Storage economics
# ---------------------------------------------------------------------------

def optimise_battery(prices: list[float]) -> dict:
    """
    Perfect-foresight dispatch of a 1 MW / 2 MWh battery over one day.

    Uses dynamic programming over discrete states of charge, so the result
    respects chronology: the battery cannot sell energy before it has bought it.
    That makes the figure a realistic upper bound on day-ahead arbitrage rather
    than the simpler top-hours-minus-bottom-hours difference, which is not
    always physically achievable.

    Efficiency is applied on discharge. Returns revenue in EUR per MW per day
    together with the hours chosen, which the dashboard marks on the day strip.
    """
    n = len(prices)
    energy_per_step = DURATION_H / SOC_STEPS          # MWh moved in one step
    max_steps_per_hour = max(1, int(round(SOC_STEPS / DURATION_H)))  # power limit
    budget = int(round(MAX_CYCLES * SOC_STEPS))        # discharge steps allowed per day

    NEG = float("-inf")
    # value[soc][used] = best profit reaching that state of charge having
    # discharged "used" steps so far. The second dimension enforces the cycle cap.
    value = [[NEG] * (budget + 1) for _ in range(SOC_STEPS + 1)]
    value[0][0] = 0.0
    trace: list[dict[tuple[int, int], tuple[int, int]]] = []

    for hour in range(n):
        price = prices[hour]
        nxt = [[NEG] * (budget + 1) for _ in range(SOC_STEPS + 1)]
        back: dict[tuple[int, int], tuple[int, int]] = {}

        for soc in range(SOC_STEPS + 1):
            for used in range(budget + 1):
                if value[soc][used] == NEG:
                    continue
                for delta in range(-max_steps_per_hour, max_steps_per_hour + 1):
                    target = soc + delta
                    if target < 0 or target > SOC_STEPS:
                        continue
                    if delta > 0:      # charging, pays the market
                        cash = -price * energy_per_step * delta
                        used_next = used
                    elif delta < 0:    # discharging, sells at efficiency
                        used_next = used - delta
                        if used_next > budget:
                            continue
                        cash = price * energy_per_step * (-delta) * ROUND_TRIP
                    else:
                        cash = 0.0
                        used_next = used
                    total = value[soc][used] + cash
                    if total > nxt[target][used_next]:
                        nxt[target][used_next] = total
                        back[(target, used_next)] = (soc, used)
        value = nxt
        trace.append(back)

    # Require the battery to finish empty so each day stands on its own.
    best_used, revenue = 0, NEG
    for used in range(budget + 1):
        if value[0][used] > revenue:
            revenue, best_used = value[0][used], used

    if revenue == NEG:
        return {"revenue": 0.0, "cycles": 0.0, "charge_hours": [], "discharge_hours": []}

    # Walk the decisions backwards to recover the schedule.
    charge_hours: list[int] = []
    discharge_hours: list[int] = []
    state = (0, best_used)
    for hour in range(n - 1, -1, -1):
        prev = trace[hour].get(state)
        if prev is None:
            break
        if state[0] > prev[0]:
            charge_hours.append(hour)
        elif state[0] < prev[0]:
            discharge_hours.append(hour)
        state = prev

    return {
        "revenue": round(revenue, 2),
        "cycles": round(best_used / SOC_STEPS, 2),
        "charge_hours": sorted(charge_hours),
        "discharge_hours": sorted(discharge_hours),
    }


def compute_metrics(prices: list[float]) -> dict:
    ordered = sorted(prices)
    top2 = statistics.fmean(ordered[-2:])
    bottom2 = statistics.fmean(ordered[:2])
    peak = [p for h, p in enumerate(prices) if h in PEAK_HOURS]
    offpeak = [p for h, p in enumerate(prices) if h not in PEAK_HOURS]
    battery = optimise_battery(prices)

    return {
        "prices": prices,
        "base": round(statistics.fmean(prices), 2),
        "peak": round(statistics.fmean(peak), 2) if peak else None,
        "offpeak": round(statistics.fmean(offpeak), 2) if offpeak else None,
        "min": round(min(prices), 2),
        "max": round(max(prices), 2),
        "spread": round(max(prices) - min(prices), 2),
        "spread_2h": round(top2 - bottom2, 2),
        "volatility": round(statistics.pstdev(prices), 2),
        "negative_hours": sum(1 for p in prices if p < 0),
        "arb_revenue": battery["revenue"],
        "cycles": battery["cycles"],
        "charge_hours": battery["charge_hours"],
        "discharge_hours": battery["discharge_hours"],
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def load_json(name: str, fallback: dict) -> dict:
    path = DATA / name
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            print(f"{name} unreadable, starting fresh", file=sys.stderr)
    return fallback


def prune(days: dict, limit: int) -> None:
    cutoff = (date.today() - timedelta(days=limit)).isoformat()
    for key in [d for d in days if d < cutoff]:
        del days[key]


def main() -> int:
    token = os.environ.get("ENTSOE_TOKEN", "").strip()
    if not token:
        print("ENTSOE_TOKEN is not set. Add it as a repository secret.", file=sys.stderr)
        return 1

    DATA.mkdir(exist_ok=True)
    history = load_json("history.json", {"fields": SCALAR_FIELDS, "days": {}})
    recent = load_json("recent.json", {"days": {}})

    # Demo data is invented. The moment real collection starts it is discarded,
    # so that no synthetic figure can ever survive into the real history.
    if history.get("demo") or recent.get("demo"):
        print("Discarding demo data before first real collection.")
        history = {"fields": SCALAR_FIELDS, "days": {}}
        recent = {"days": {}}

    # If the field order ever changes, the old compact rows cannot be read.
    if history.get("fields") != SCALAR_FIELDS:
        print("Field layout changed, rebuilding history from scratch", file=sys.stderr)
        history = {"fields": SCALAR_FIELDS, "days": {}}

    today = date.today()
    targets = [today + timedelta(days=1)] + [today - timedelta(days=i) for i in range(LOOKBACK_DAYS)]

    collected = 0
    for zone in ZONES:
        print(f"{zone['id']}")
        for day in targets:
            key = day.isoformat()
            settled = day < today
            if settled and zone["id"] in history["days"].get(key, {}):
                continue  # already stored and no longer subject to revision

            start, end = local_day_bounds(day, zone["tz"])
            xml_text = request_prices(token, zone["eic"], start, end)
            if not xml_text:
                continue
            try:
                series = parse_series(xml_text)
            except ET.ParseError as exc:
                print(f"    {key}: could not parse response ({exc})", file=sys.stderr)
                continue

            prices = to_local_hours(series, day, zone["tz"])
            if not prices:
                print(f"    {key}: incomplete")
                continue

            metrics = compute_metrics(prices)
            history["days"].setdefault(key, {})[zone["id"]] = [metrics[f] for f in SCALAR_FIELDS]
            recent["days"].setdefault(key, {})[zone["id"]] = metrics
            collected += 1
            print(f"    {key}: {len(prices)}h stored")

    if collected == 0:
        print("No new data collected.", file=sys.stderr)

    prune(history["days"], HISTORY_LIMIT_DAYS)
    prune(recent["days"], RECENT_LIMIT_DAYS)

    latest_key = max((d for d, z in history["days"].items() if z), default=None)
    recent.update({
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": latest_key,
        "assumptions": {
            "duration_h": DURATION_H,
            "round_trip_efficiency": ROUND_TRIP,
            "max_cycles": MAX_CYCLES,
            "peak_definition": "08:00-20:00 local",
        },
        "zones": [{"id": z["id"], "label": z["label"]} for z in ZONES],
    })

    (DATA / "history.json").write_text(json.dumps(history, separators=(",", ":")))
    (DATA / "recent.json").write_text(json.dumps(recent, separators=(",", ":")))

    hist_kb = (DATA / "history.json").stat().st_size // 1024
    recent_kb = (DATA / "recent.json").stat().st_size // 1024
    print(f"Wrote {collected} zone-days. Latest: {latest_key}. "
          f"history {hist_kb} KB over {len(history['days'])} days, recent {recent_kb} KB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
