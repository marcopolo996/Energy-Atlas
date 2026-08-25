"""
Atlas data collector.

Pulls three things from the ENTSO-E Transparency Platform for each configured
bidding zone, and derives the figures the Atlas pages display:

  1. Day-ahead prices, at whatever resolution the market clears in.
  2. Actual solar and wind generation, used to derive capture prices.
  3. Procured balancing capacity prices, where the TSO publishes them.

Runs on GitHub Actions. Requires one secret: ENTSOE_TOKEN.

A note on resolution. European day-ahead markets moved from hourly to
quarter-hourly market time units on 1 October 2025, Switzerland excepted until
November 2026. Storage economics are computed at whatever resolution the market
actually publishes, because averaging quarter-hours up to hours erases the
intra-hour price shape that a battery monetises. The hourly equivalent is
computed alongside it so the difference can be seen.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://web-api.tp.entsoe.eu/api"

# ---------------------------------------------------------------------------
# Configuration
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

# Zones to attempt ancillary collection for. Publication of procured balancing
# capacity is patchy across Europe, so this is deliberately a short list.
ANCILLARY_ZONES = ["DE_LU", "AT", "IT_NORD"]

# Reserve products. processType is what ENTSO-E keys these on.
RESERVES = [
    {"id": "FCR",  "label": "Frequency containment",   "process": "A52"},
    {"id": "aFRR", "label": "Automatic restoration",   "process": "A51"},
    {"id": "mFRR", "label": "Manual restoration",      "process": "A47"},
]

PSR = {"solar": ["B16"], "wind": ["B19", "B18"]}   # onshore and offshore combined

DURATION_H = 2.0
ROUND_TRIP = 0.85
MAX_CYCLES = 2.0
SOC_STEPS = 8

LOOKBACK_DAYS = 4
HISTORY_LIMIT_DAYS = 900
RECENT_LIMIT_DAYS = 60
ANCILLARY_LIMIT_DAYS = 900

PEAK_HOURS = range(8, 20)
PAUSE = 0.35          # seconds between requests, to stay a polite client

SCALAR_FIELDS = [
    "base", "peak", "offpeak", "min", "max", "spread", "spread_2h",
    "volatility", "negative_hours", "arb_revenue", "arb_revenue_hourly",
    "cycles", "resolution_min",
    "solar_capture", "solar_capture_rate", "solar_share",
    "wind_capture", "wind_capture_rate", "wind_share",
]

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


# ---------------------------------------------------------------------------
# ENTSO-E client
# ---------------------------------------------------------------------------

def local_day_bounds(day: date, tz_name: str) -> tuple[str, str]:
    tz = ZoneInfo(tz_name)
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    fmt = "%Y%m%d%H%M"
    return (start.astimezone(timezone.utc).strftime(fmt),
            (start + timedelta(days=1)).astimezone(timezone.utc).strftime(fmt))


def strip_ns(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def fetch(token: str, params: dict, label: str) -> str | None:
    """One API call. Returns None on any failure, having said why."""
    url = f"{API}?{urllib.parse.urlencode({**params, 'securityToken': token})}"
    try:
        time.sleep(PAUSE)
        with urllib.request.urlopen(url, timeout=60) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        # "No matching data found" is the normal answer for an unpublished day.
        quiet = "No matching data" in body
        if not quiet:
            print(f"      {label}: HTTP {exc.code} {body[:120]}", file=sys.stderr)
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"      {label}: {exc}", file=sys.stderr)
        return None


def parse_periods(xml_text: str, value_tags: tuple[str, ...],
                  want_psr: list[str] | None = None) -> dict[datetime, float]:
    """
    Flatten a market document into {utc_timestamp: value}.

    Handles any resolution, forward-fills omitted repeating positions, and can
    filter to particular production types. Series representing consumption
    rather than generation are skipped, since ENTSO-E reports pumped storage
    consumption in the same document.
    """
    out: dict[datetime, float] = {}
    root = ET.fromstring(xml_text)

    for ts in root.iter():
        if strip_ns(ts.tag) != "TimeSeries":
            continue

        psr_type, is_consumption = None, False
        for node in ts.iter():
            name = strip_ns(node.tag)
            if name == "psrType":
                psr_type = (node.text or "").strip()
            elif name == "outBiddingZone_Domain.mRID":
                is_consumption = True

        if want_psr is not None and psr_type not in want_psr:
            continue
        if want_psr is not None and is_consumption:
            continue

        for period in ts.iter():
            if strip_ns(period.tag) != "Period":
                continue
            resolution, start, points = None, None, {}
            for child in period:
                name = strip_ns(child.tag)
                if name == "resolution":
                    resolution = (child.text or "").strip()
                elif name == "timeInterval":
                    for sub in child:
                        if strip_ns(sub.tag) == "start":
                            start = datetime.strptime(
                                (sub.text or "").strip(), "%Y-%m-%dT%H:%MZ"
                            ).replace(tzinfo=timezone.utc)
                elif name == "Point":
                    pos, val = None, None
                    for sub in child:
                        sub_name = strip_ns(sub.tag)
                        if sub_name == "position":
                            pos = int((sub.text or "0").strip())
                        elif sub_name in value_tags:
                            val = float((sub.text or "0").strip())
                    if pos is not None and val is not None:
                        points[pos] = val

            if not points or start is None:
                continue

            minutes = {"PT60M": 60, "PT30M": 30, "PT15M": 15}.get(resolution or "PT60M", 60)
            step = timedelta(minutes=minutes)
            last = None
            for pos in range(1, max(points) + 1):
                if pos in points:
                    last = points[pos]
                if last is None:
                    continue
                stamp = start + step * (pos - 1)
                # Several series can cover the same slot; sum them, which is
                # what we want for wind onshore plus offshore.
                out[stamp] = out.get(stamp, 0.0) + last if want_psr else last

    return out


def to_local_day(series: dict[datetime, float], day: date, tz_name: str) -> list[float]:
    """Values falling on one local calendar day, in chronological order."""
    tz = ZoneInfo(tz_name)
    rows = [(ts, v) for ts, v in series.items() if ts.astimezone(tz).date() == day]
    rows.sort()
    return [v for _, v in rows]


def to_hourly(values: list[float]) -> list[float]:
    """Average a native-resolution day up to 24 hourly values."""
    if not values or len(values) % 24:
        return values
    per_hour = len(values) // 24
    return [round(statistics.fmean(values[h * per_hour:(h + 1) * per_hour]), 2) for h in range(24)]


# ---------------------------------------------------------------------------
# Storage economics
# ---------------------------------------------------------------------------

def optimise_battery(prices: list[float], interval_h: float) -> dict:
    """
    Perfect-foresight dispatch of a 1 MW battery over one day, by dynamic
    programming over state of charge and cumulative cycling.

    Chronology is respected, so the battery cannot sell before it has bought.
    Efficiency is applied on discharge, cycling is capped, and the battery must
    finish empty so each day stands alone. The result is an upper bound on
    day-ahead arbitrage, not an achievable revenue.
    """
    n = len(prices)
    if not n:
        return {"revenue": 0.0, "cycles": 0.0, "charge": [], "discharge": []}

    per_step = DURATION_H / SOC_STEPS
    max_move = max(1, int(round(SOC_STEPS * interval_h / DURATION_H)))
    budget = int(round(MAX_CYCLES * SOC_STEPS))
    NEG = float("-inf")

    value = [[NEG] * (budget + 1) for _ in range(SOC_STEPS + 1)]
    value[0][0] = 0.0
    trace = []

    for price in prices:
        nxt = [[NEG] * (budget + 1) for _ in range(SOC_STEPS + 1)]
        back = {}
        for soc in range(SOC_STEPS + 1):
            for used in range(budget + 1):
                base = value[soc][used]
                if base == NEG:
                    continue
                for delta in range(-max_move, max_move + 1):
                    target = soc + delta
                    if target < 0 or target > SOC_STEPS:
                        continue
                    used_next = used
                    if delta > 0:
                        cash = -price * per_step * delta
                    elif delta < 0:
                        used_next = used - delta
                        if used_next > budget:
                            continue
                        cash = price * per_step * (-delta) * ROUND_TRIP
                    else:
                        cash = 0.0
                    total = base + cash
                    if total > nxt[target][used_next]:
                        nxt[target][used_next] = total
                        back[(target, used_next)] = (soc, used)
        value, _ = nxt, trace.append(back)

    revenue, best = NEG, 0
    for used in range(budget + 1):
        if value[0][used] > revenue:
            revenue, best = value[0][used], used
    if revenue == NEG:
        return {"revenue": 0.0, "cycles": 0.0, "charge": [], "discharge": []}

    charge, discharge, state = [], [], (0, best)
    for i in range(n - 1, -1, -1):
        prev = trace[i].get(state)
        if prev is None:
            break
        if state[0] > prev[0]:
            charge.append(i)
        elif state[0] < prev[0]:
            discharge.append(i)
        state = prev

    return {"revenue": round(revenue, 2), "cycles": round(best / SOC_STEPS, 2),
            "charge": sorted(charge), "discharge": sorted(discharge)}


def capture(prices: list[float], generation: list[float]) -> tuple[float | None, float | None]:
    """
    Volume-weighted price earned by a technology, and its share of the day.

    Returns (capture price, share of total generation-weighted volume). If the
    generation series does not line up with the price series, both are None
    rather than a number that would be quietly wrong.
    """
    if not generation or len(generation) != len(prices):
        return None, None
    total = sum(generation)
    if total <= 0:
        return None, None
    weighted = sum(p * g for p, g in zip(prices, generation))
    return round(weighted / total, 2), round(total, 1)


def compute(prices: list[float], solar: list[float], wind: list[float]) -> dict:
    n = len(prices)
    interval_h = 24 / n
    hourly = to_hourly(prices)
    ordered = sorted(prices)
    take = max(1, int(round(2 / interval_h)))     # two hours' worth of intervals

    peak = [p for i, p in enumerate(prices) if int(i * interval_h) in PEAK_HOURS]
    offpeak = [p for i, p in enumerate(prices) if int(i * interval_h) not in PEAK_HOURS]
    base = statistics.fmean(prices)

    native = optimise_battery(prices, interval_h)
    hourly_run = optimise_battery(hourly, 1.0) if len(hourly) == 24 else native

    solar_price, solar_vol = capture(prices, solar)
    wind_price, wind_vol = capture(prices, wind)

    return {
        "prices": [round(p, 2) for p in hourly],
        "resolution_min": round(60 * interval_h),
        "base": round(base, 2),
        "peak": round(statistics.fmean(peak), 2) if peak else None,
        "offpeak": round(statistics.fmean(offpeak), 2) if offpeak else None,
        "min": round(min(prices), 2),
        "max": round(max(prices), 2),
        "spread": round(max(prices) - min(prices), 2),
        "spread_2h": round(statistics.fmean(ordered[-take:]) - statistics.fmean(ordered[:take]), 2),
        "volatility": round(statistics.pstdev(prices), 2),
        "negative_hours": round(sum(1 for p in prices if p < 0) * interval_h, 2),
        "arb_revenue": native["revenue"],
        "arb_revenue_hourly": hourly_run["revenue"],
        "cycles": native["cycles"],
        "charge_hours": sorted({int(i * interval_h) for i in native["charge"]}),
        "discharge_hours": sorted({int(i * interval_h) for i in native["discharge"]}),
        "solar_capture": solar_price,
        "solar_capture_rate": round(solar_price / base, 3) if solar_price and base > 0 else None,
        "solar_share": solar_vol,
        "wind_capture": wind_price,
        "wind_capture_rate": round(wind_price / base, 3) if wind_price and base > 0 else None,
        "wind_share": wind_vol,
    }


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def load(name: str, fallback: dict) -> dict:
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


def collect_day(token: str, zone: dict, day: date) -> dict | None:
    start, end = local_day_bounds(day, zone["tz"])
    common = {"periodStart": start, "periodEnd": end}

    xml = fetch(token, {**common, "documentType": "A44",
                        "in_Domain": zone["eic"], "out_Domain": zone["eic"]},
                f"{zone['id']} prices")
    if not xml:
        return None
    try:
        prices = to_local_day(parse_periods(xml, ("price.amount",)), day, zone["tz"])
    except ET.ParseError:
        return None
    if len(prices) < 20:
        return None

    # Generation is optional. A missing series costs us the capture figure for
    # that day and nothing else.
    gen = {}
    for tech, codes in PSR.items():
        xml = fetch(token, {**common, "documentType": "A75", "processType": "A16",
                            "in_Domain": zone["eic"], "psrType": codes[0]},
                    f"{zone['id']} {tech}")
        series = {}
        if xml:
            try:
                series = parse_periods(xml, ("quantity",), want_psr=codes)
            except ET.ParseError:
                series = {}
        if tech == "wind" and len(codes) > 1:
            xml2 = fetch(token, {**common, "documentType": "A75", "processType": "A16",
                                 "in_Domain": zone["eic"], "psrType": codes[1]},
                         f"{zone['id']} wind offshore")
            if xml2:
                try:
                    for k, v in parse_periods(xml2, ("quantity",), want_psr=[codes[1]]).items():
                        series[k] = series.get(k, 0.0) + v
                except ET.ParseError:
                    pass
        values = to_local_day(series, day, zone["tz"])
        # Align to the price grid where the two resolutions differ.
        if values and len(values) != len(prices):
            if len(values) % len(prices) == 0:
                factor = len(values) // len(prices)
                values = [statistics.fmean(values[i * factor:(i + 1) * factor])
                          for i in range(len(prices))]
            elif len(prices) % len(values) == 0:
                factor = len(prices) // len(values)
                values = [v for v in values for _ in range(factor)]
            else:
                values = []
        gen[tech] = values

    return compute(prices, gen.get("solar", []), gen.get("wind", []))


def collect_ancillary(token: str, days: list[date], store: dict) -> int:
    """
    Procured balancing capacity prices, where published.

    Coverage is uneven across Europe and this is best effort by design: a zone
    or product that returns nothing is reported and skipped, not treated as an
    error.
    """
    found = 0
    for zone_id in ANCILLARY_ZONES:
        zone = next(z for z in ZONES if z["id"] == zone_id)
        for day in days:
            key = day.isoformat()
            if store.get(key, {}).get(zone_id):
                continue
            start, end = local_day_bounds(day, zone["tz"])
            for reserve in RESERVES:
                xml = fetch(token, {
                    "documentType": "A15", "processType": reserve["process"],
                    "controlArea_Domain": zone["eic"],
                    "periodStart": start, "periodEnd": end,
                }, f"{zone_id} {reserve['id']}")
                if not xml:
                    continue
                try:
                    series = parse_periods(xml, ("procurement_Price.amount", "price.amount"))
                except ET.ParseError:
                    continue
                values = to_local_day(series, day, zone["tz"])
                if not values:
                    continue
                # Stored as [mean, min, max]; key names would triple the file size.
                store.setdefault(key, {}).setdefault(zone_id, {})[reserve["id"]] = [
                    round(statistics.fmean(values), 2),
                    round(min(values), 2),
                    round(max(values), 2),
                ]
                found += 1
    return found


def main() -> int:
    token = os.environ.get("ENTSOE_TOKEN", "").strip()
    if not token:
        print("ENTSOE_TOKEN is not set. Add it as a repository secret.", file=sys.stderr)
        return 1

    DATA.mkdir(exist_ok=True)
    history = load("history.json", {"fields": SCALAR_FIELDS, "days": {}})
    recent = load("recent.json", {"days": {}})
    ancillary = load("ancillary.json", {"days": {}})

    if history.get("demo") or recent.get("demo") or ancillary.get("demo"):
        print("Discarding demo data before first real collection.")
        history, recent, ancillary = {"fields": SCALAR_FIELDS, "days": {}}, {"days": {}}, {"days": {}}
    if history.get("fields") != SCALAR_FIELDS:
        print("Field layout changed, rebuilding history.", file=sys.stderr)
        history = {"fields": SCALAR_FIELDS, "days": {}}

    today = date.today()
    targets = [today + timedelta(days=1)] + [today - timedelta(days=i) for i in range(LOOKBACK_DAYS)]

    collected = 0
    for zone in ZONES:
        print(zone["id"])
        for day in targets:
            key = day.isoformat()
            if day < today and zone["id"] in history["days"].get(key, {}):
                continue
            metrics = collect_day(token, zone, day)
            if not metrics:
                continue
            history["days"].setdefault(key, {})[zone["id"]] = [metrics[f] for f in SCALAR_FIELDS]
            recent["days"].setdefault(key, {})[zone["id"]] = metrics
            collected += 1
            cap = metrics["solar_capture_rate"]
            print(f"    {key}: {metrics['resolution_min']}min, arbitrage {metrics['arb_revenue']:.0f}"
                  + (f", solar capture {cap:.0%}" if cap else ""))

    print("ancillary")
    anc_found = collect_ancillary(token, targets, ancillary["days"])
    print(f"    {anc_found} series stored"
          + ("" if anc_found else " (no published data returned for these zones)"))

    prune(history["days"], HISTORY_LIMIT_DAYS)
    prune(recent["days"], RECENT_LIMIT_DAYS)
    prune(ancillary["days"], ANCILLARY_LIMIT_DAYS)

    latest = max((d for d, z in history["days"].items() if z), default=None)
    meta = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": latest,
        "assumptions": {
            "duration_h": DURATION_H, "round_trip_efficiency": ROUND_TRIP,
            "max_cycles": MAX_CYCLES, "peak_definition": "08:00-20:00 local",
        },
        "zones": [{"id": z["id"], "label": z["label"]} for z in ZONES],
        "reserves": RESERVES,
    }
    recent.update(meta)
    ancillary.update({"generated_utc": meta["generated_utc"], "reserves": RESERVES,
                      "zones": [z for z in meta["zones"] if z["id"] in ANCILLARY_ZONES]})

    for name, blob in (("history.json", history), ("recent.json", recent),
                       ("ancillary.json", ancillary)):
        (DATA / name).write_text(json.dumps(blob, separators=(",", ":")))
        print(f"{name}: {(DATA / name).stat().st_size // 1024} KB")

    print(f"Wrote {collected} zone-days. Latest: {latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
