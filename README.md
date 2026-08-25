# Atlas

European power market monitoring. Settled day-ahead prices, solar and wind
capture rates, and storage economics across the coupled bidding zones,
collected daily by a scheduled job. Nothing runs on your own machine.

## Pages

| File | Contents |
|---|---|
| `index.html` | Frontispiece. Entry point. |
| `power.html` | Plate I price strips, Plate II capture rates, Plate III settled figures. |
| `storage.html` | Plate I arbitrage, Plate II dispatch, Plate III resolution premium, Plate IV ancillary. |

## Setup, once

1. **ENTSO-E token.** Register at `transparency.entsoe.eu` and find the API token
   in your account settings, or email `transparency@entsoe.eu` to request access.
2. **Upload the files**, keeping the folder structure exactly as it is.
3. **Store the token** under *Settings* → *Secrets and variables* → *Actions* →
   *New repository secret*, named exactly `ENTSOE_TOKEN`.
4. **Allow writes**: *Settings* → *Actions* → *General* → *Workflow permissions*
   → **Read and write permissions** → *Save*.
5. **Preview first, optionally**: *Actions* → *Load demo data* → *Run workflow*.
   Fills the pages with invented figures. A warning appears while they are
   present, and real collection deletes them.
6. **Collect**: *Actions* → *Update market data* → *Run workflow*.
7. **Publish**: *Settings* → *Pages* → *Deploy from a branch*, `main`, `/ (root)`.
   Live at `https://<username>.github.io/<repository>/`.

## Structure

```
index.html                      frontispiece
power.html  storage.html        the two plates pages
assets/atlas.css                shared styling
assets/atlas.js                 shared behaviour
scripts/collect.py              the collector
scripts/make_demo_data.py       invented figures for previewing
.github/workflows/update.yml    twice daily schedule
.github/workflows/demo.yml      manual demo run
data/history.json               daily figures, compact, about 18 months
data/recent.json                last 60 days including hourly prices
data/ancillary.json             procured balancing capacity, where published
```

## What the figures mean

**Capture rate** is generation-weighted price divided by average price. The
thirty day series is the one worth quoting: a capture rate computed daily and
then averaged understates cannibalisation, because it normalises within each day
and so misses that windy days are also cheap days. On the demo data the gap
between the two methods is around ten points for German wind.

**Arbitrage revenue** is a perfect-foresight upper bound for a 1 MW / 2 MWh
battery at 85 per cent round trip, capped at two cycles, starting and finishing
empty. It excludes ancillary services, intraday, imbalance, degradation and all
operating costs.

**Resolution premium** compares the dispatch at the resolution the market clears
in against the same day averaged to hours. European day-ahead markets moved to
quarter-hourly market time units on 1 October 2025, Switzerland excepted until
November 2026. The premium indicates how much of a revenue case depends on
intra-hour execution.

**Ancillary** figures are procured balancing capacity prices where the
transmission operator publishes them. Coverage is uneven and the plate will say
so plainly rather than showing nothing. Product definitions differ between
control areas and are not directly comparable without adjustment.

## Changing what it tracks

The configuration block at the top of `scripts/collect.py` holds the markets,
the battery assumptions, the ancillary zones and the retention periods. Edit in
the browser, commit, and the next run picks it up. Changing the battery
assumptions does not rewrite past days.

## Notes

- Open the pages from the Pages address. Browsers block a local file from
  reading the data files beside it.
- GitHub pauses scheduled jobs after 60 days without activity. Any commit or
  manual run restarts them.
- Adding a market: find its bidding zone code on the Transparency Platform. A
  wrong code returns no data rather than an error, so check the run log.
