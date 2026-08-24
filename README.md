# Day-ahead monitor

A single web page showing settled day-ahead power prices across European
bidding zones, with the storage-relevant metrics derived from them. Data is
collected once a day by a scheduled job; nothing runs on your own machine.

## Setup, once

1. **Get an ENTSO-E API token.** Register at `transparency.entsoe.eu`, then look
   for the API token in your account settings. If there is no token there, email
   `transparency@entsoe.eu` from your registered address and ask for API access.
   It is free.

2. **Create the repository.** On GitHub: *New repository*, name it whatever you
   like, tick *Add a README*, and set it to **Public** (Pages is free on public
   repositories; private needs a paid plan).

3. **Upload these files.** *Add file* → *Upload files*, then drag the whole
   folder in. Keep the structure exactly as it is:
   `index.html`, `scripts/fetch_prices.py`, `.github/workflows/update.yml`.

4. **Store the token.** *Settings* → *Secrets and variables* → *Actions* →
   *New repository secret*. Name it exactly `ENTSOE_TOKEN` and paste the token
   as the value. The name must match; the value is never visible again.

5. **Allow the job to save data.** *Settings* → *Actions* → *General* →
   *Workflow permissions* → select **Read and write permissions** → *Save*.

6. **Run it once by hand.** *Actions* tab → *Update market data* → *Run
   workflow*. It takes a couple of minutes. Green tick means it worked; click
   into the run to read the log if it did not.

7. **Optional: preview it before the token arrives.** *Actions* tab → *Load
   demo data* → *Run workflow*. This fills the page with invented figures so you
   can confirm everything is wired up. The page shows a warning banner while
   demo data is present, and step 6 deletes all of it the first time real
   collection runs.

8. **Turn on the website.** *Settings* → *Pages* → under *Source* choose
   *Deploy from a branch*, branch `main`, folder `/ (root)* → *Save*. After a
   minute the page is live at
   `https://<your-username>.github.io/<repository-name>/`.

Bookmark that address. From then on the job refreshes the data twice a day and
the page always shows the latest.

## Changing what it tracks

Everything configurable sits at the top of `scripts/fetch_prices.py`. Edit the
file directly in the browser (pencil icon), commit, and the next run picks it up.

- `ZONES` — the markets. To add one, find its bidding zone code on the ENTSO-E
  Transparency Platform and copy the pattern of the existing entries. A wrong
  code returns no data rather than an error, so check the run log after adding.
- `DURATION_H`, `ROUND_TRIP`, `MAX_CYCLES` — the battery assumed in the
  arbitrage calculation. Changing these does not rewrite past days; only newly
  collected days use the new assumptions.

## Files

| Path | What it is |
|---|---|
| `index.html` | The page. Self-contained, loads nothing from outside. |
| `scripts/fetch_prices.py` | The collector. Standard library only. |
| `.github/workflows/update.yml` | The schedule. |
| `data/history.json` | Daily figures, about 18 months, compact. |
| `data/recent.json` | Last 60 days including hourly prices. |
| `scripts/make_demo_data.py` | Invented figures for previewing. Safe to delete. |
| `.github/workflows/demo.yml` | The manual demo run. Safe to delete. |
| `scripts/_selftest.py` | Offline checks. Safe to delete. |

## Notes

- The page must be opened from the Pages address, not from a file on disk.
  Browsers block a local file from reading the data files next to it.
- GitHub pauses scheduled jobs in repositories with no activity for 60 days.
  A commit, or one manual run, restarts the schedule.
- Scheduled runs are often a few minutes late. This does not matter for daily data.
- The page shows a short setup message until the first successful run publishes data.

