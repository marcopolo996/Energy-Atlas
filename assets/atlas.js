/* ==========================================================================
   Atlas — shared behaviour for every plate.
   Loads the collected data once, then renders whichever plates a page asks for.
   ========================================================================== */
"use strict";

const Atlas = (() => {

  /* Hypsometric ramp. Cold below zero, through pale mid ground, to warm high
     ground, in the manner of an elevation map. */
  const STOPS = [
    [-80, [ 25,  62,  92]],
    [-10, [ 68, 122, 148]],
    [ 25, [149, 180, 168]],
    [ 60, [219, 214, 186]],
    [100, [201, 160,  90]],
    [170, [168,  95,  46]],
    [280, [124,  46,  28]]
  ];
  const rgb = (c) => `rgb(${c[0]},${c[1]},${c[2]})`;

  function colour(v) {
    if (v <= STOPS[0][0]) return rgb(STOPS[0][1]);
    if (v >= STOPS[STOPS.length - 1][0]) return rgb(STOPS[STOPS.length - 1][1]);
    for (let i = 0; i < STOPS.length - 1; i++) {
      const [a, ca] = STOPS[i], [b, cb] = STOPS[i + 1];
      if (v >= a && v <= b) {
        const t = (v - a) / (b - a);
        return rgb(ca.map((c, j) => Math.round(c + t * (cb[j] - c))));
      }
    }
    return rgb(STOPS[3][1]);
  }

  const LINES = ["#1A1C19", "#A8351F", "#1D4E6B", "#5E7A4A", "#8A6D2F", "#6B3A6B", "#2E7C7C", "#9A2B3F"];

  const $ = (id) => document.getElementById(id);
  const num = (v, dp = 0) => (v === null || v === undefined || Number.isNaN(v))
    ? "\u2014"
    : Number(v).toLocaleString("en-GB", { minimumFractionDigits: dp, maximumFractionDigits: dp });
  const pct = (v, dp = 0) => (v === null || v === undefined) ? "\u2014" : (v * 100).toFixed(dp) + "%";
  const dayName = (key, opts) => new Date(key + "T12:00:00Z").toLocaleDateString("en-GB",
    Object.assign({ timeZone: "UTC" }, opts));

  const state = { recent: null, history: null, ancillary: null, fields: [], dates: [], cursor: 0 };

  async function grab(name) {
    const r = await fetch(`data/${name}?v=${Date.now()}`, { cache: "no-store" });
    if (!r.ok) throw new Error(name);
    return r.json();
  }

  async function load() {
    let recent, history, ancillary = { days: {} };
    try {
      [recent, history] = await Promise.all([grab("recent.json"), grab("history.json")]);
    } catch (err) {
      return { ok: false, reason: "missing" };
    }
    try { ancillary = await grab("ancillary.json"); } catch (err) { /* optional */ }

    Object.assign(state, {
      recent, history, ancillary,
      fields: history.fields || [],
      dates: Object.keys(recent.days || {}).filter((d) => Object.keys(recent.days[d]).length).sort()
    });
    state.cursor = state.dates.length - 1;
    if (!state.dates.length) return { ok: false, reason: "empty" };

    if (recent.demo || history.demo) {
      document.title = "DEMO DATA \u00b7 " + document.title;
      const banner = $("demo");
      if (banner) banner.hidden = false;
    }
    const issued = $("issued");
    if (issued) {
      const t = new Date(recent.generated_utc);
      issued.innerHTML = (recent.demo ? "Invented figures, not market data" : "Day-ahead auction, settled")
        + "<br>Issued " + t.toLocaleString("en-GB", {
          day: "2-digit", month: "short", year: "numeric",
          hour: "2-digit", minute: "2-digit", timeZone: "UTC" }) + " UTC";
    }
    return { ok: true };
  }

  function fault(reason) {
    const box = $("fault");
    if (!box) return;
    box.hidden = false;
    box.querySelector("p").innerHTML = reason === "empty"
      ? "The collector ran but stored nothing. Open the run log in the Actions tab: the usual cause is a token that has not yet been activated for API access."
      : "No data has been published yet. Run <code>Update market data</code> once from the Actions tab, or <code>Load demo data</code> to preview the pages first.";
  }

  const zones = () => state.recent.zones || [];
  const zoneColour = (id) => LINES[zones().findIndex((z) => z.id === id) % LINES.length];
  const scalar = (key, zoneId, field) => {
    const row = (state.history.days[key] || {})[zoneId];
    if (!row) return null;
    const v = row[state.fields.indexOf(field)];
    return (v === undefined) ? null : v;
  };

  /* ---------- day strips ---------- */
  function strips(opts) {
    const marks = opts && opts.marks;
    const cells = $("ticks");
    if (cells) cells.innerHTML = Array.from({ length: 24 },
      (_, h) => `<span>${h % 6 === 0 ? String(h).padStart(2, "0") : ""}</span>`).join("");

    function draw() {
      const key = state.dates[state.cursor];
      const day = state.recent.days[key];
      $("dayLabel").textContent = dayName(key, { weekday: "short", day: "2-digit", month: "short", year: "numeric" });
      $("earlier").disabled = state.cursor === 0;
      $("later").disabled = state.cursor === state.dates.length - 1;

      const all = [];
      Object.values(day).forEach((m) => all.push(...m.prices));

      $("strips").innerHTML = zones().filter((z) => day[z.id]).map((z) => {
        const m = day[z.id];
        const c = new Set(marks ? m.charge_hours || [] : []);
        const d = new Set(marks ? m.discharge_hours || [] : []);
        const bar = m.prices.map((p, h) =>
          `<div class="hr${c.has(h) ? " charge" : d.has(h) ? " discharge" : ""}" style="background:${colour(p)}" title="${String(h).padStart(2, "0")}:00 · ${num(p, 1)} EUR/MWh"></div>`).join("");
        const figs = marks
          ? [["Base", num(m.base, 1)], ["Range", num(m.spread, 0)], ["Arbitrage", num(m.arb_revenue, 0)]]
          : [["Base", num(m.base, 1)], ["Solar", pct(m.solar_capture_rate)], ["Wind", pct(m.wind_capture_rate)]];
        return `<div class="strip">
          <div class="zone">${z.label}<small>${z.id.replace(/_/g, "-")}</small></div>
          <div class="hours">${bar}</div>
          <div class="figs">${figs.map(([k, v]) => `<div><span>${k}</span>${v}</div>`).join("")}</div>
        </div>`;
      }).join("");

      const ramp = $("ramp");
      if (ramp) {
        const lo = STOPS[0][0], hi = STOPS[STOPS.length - 1][0];
        ramp.style.background = "linear-gradient(90deg," +
          STOPS.map(([v, c]) => `${rgb(c)} ${((v - lo) / (hi - lo) * 100).toFixed(1)}%`).join(",") + ")";
        $("rampNote").textContent = `${lo} to ${hi} EUR/MWh \u00b7 this day ${num(Math.min(...all))} to ${num(Math.max(...all))}`;
      }
      if (opts && opts.onDraw) opts.onDraw(key, day);
    }

    $("earlier").addEventListener("click", () => { if (state.cursor > 0) { state.cursor--; draw(); } });
    $("later").addEventListener("click", () => { if (state.cursor < state.dates.length - 1) { state.cursor++; draw(); } });
    document.addEventListener("keydown", (e) => {
      if (e.target.tagName === "BUTTON" || e.altKey || e.ctrlKey) return;
      if (e.key === "ArrowLeft" && state.cursor > 0) { state.cursor--; draw(); }
      if (e.key === "ArrowRight" && state.cursor < state.dates.length - 1) { state.cursor++; draw(); }
    });
    draw();
  }

  /* ---------- trend chart ---------- */
  function trend(cfg) {
    const svg = $(cfg.svg), keyBox = $(cfg.keys), toolBox = $(cfg.tools);
    let metric = cfg.metrics[0];
    const on = new Set(zones().map((z) => z.id));

    if (toolBox) {
      toolBox.innerHTML = cfg.metrics.map((m) =>
        `<button type="button" data-m="${m.field}" aria-pressed="${m === metric}">${m.label}</button>`).join("");
      toolBox.addEventListener("click", (e) => {
        const b = e.target.closest("button"); if (!b) return;
        metric = cfg.metrics.find((m) => m.field === b.dataset.m);
        [...toolBox.children].forEach((c) => c.setAttribute("aria-pressed", c.dataset.m === metric.field));
        draw();
      });
    }
    keyBox.innerHTML = zones().map((z) =>
      `<button type="button" data-z="${z.id}" aria-pressed="true"><i style="background:${zoneColour(z.id)}"></i><span class="label">${z.label}</span></button>`).join("");
    keyBox.addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      const id = b.dataset.z;
      on.has(id) ? on.delete(id) : on.add(id);
      b.setAttribute("aria-pressed", on.has(id));
      draw();
    });

    /* A capture rate over a period is not the average of its daily rates. It
       is total revenue divided by total volume, which is what makes windy,
       cheap days pull the figure down. Reconstructed from the stored daily
       capture price and volume. */
    function rolling(span, zoneId, metric) {
      const w = metric.rolling.window;
      const cap = span.map((d) => scalar(d, zoneId, metric.rolling.capture));
      const vol = span.map((d) => scalar(d, zoneId, metric.rolling.volume));
      const base = span.map((d) => scalar(d, zoneId, "base"));
      return span.map((d, x) => {
        if (x < w - 1) return null;
        let revenue = 0, volume = 0, baseSum = 0, days = 0;
        for (let i = x - w + 1; i <= x; i++) {
          if (base[i] === null) continue;
          baseSum += base[i]; days++;
          if (cap[i] === null || vol[i] === null || !vol[i]) continue;
          revenue += cap[i] * vol[i]; volume += vol[i];
        }
        if (!volume || !days) return null;
        const periodBase = baseSum / days;
        if (periodBase <= 0) return null;
        return [x, (revenue / volume) / periodBase * 100];
      });
    }

    function draw() {
      const W = 1000, H = 330, L = 62, R = 14, T = 14, B = 28;
      const span = Object.keys(state.history.days || {}).sort().slice(-cfg.days || -540);
      const rows = zones().filter((z) => on.has(z.id)).map((z) => ({
        colour: zoneColour(z.id),
        pts: metric.rolling ? rolling(span, z.id, metric) : span.map((d, x) => {
          const v = scalar(d, z.id, metric.field);
          return (v === null || v === undefined) ? null : [x, metric.scale ? v * metric.scale : v];
        })
      }));
      const vals = rows.flatMap((r) => r.pts.filter(Boolean).map((p) => p[1]));
      if (!vals.length) {
        svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" text-anchor="middle" class="axis">Select a market to plot</text>`;
        return;
      }
      let lo = metric.zero === false ? Math.min(...vals) : Math.min(0, ...vals);
      let hi = Math.max(...vals);
      if (hi === lo) hi = lo + 1;
      const pad = (hi - lo) * 0.08; hi += pad; lo -= (lo < 0 || metric.zero === false) ? pad : 0;

      const px = (x) => L + (span.length <= 1 ? 0 : x / (span.length - 1) * (W - L - R));
      const py = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);

      let out = "";
      for (let i = 0; i <= 4; i++) {
        const v = lo + i / 4 * (hi - lo), y = py(v).toFixed(1);
        out += `<line class="gridline" x1="${L}" y1="${y}" x2="${W - R}" y2="${y}"/>`;
        out += `<text class="axis" x="${L - 8}" y="${(+y + 3.4).toFixed(1)}" text-anchor="end">${metric.fmt ? metric.fmt(v) : num(v, metric.dp || 0)}</text>`;
      }
      if (lo < 0 && hi > 0) out += `<line class="zeroline" x1="${L}" y1="${py(0).toFixed(1)}" x2="${W - R}" y2="${py(0).toFixed(1)}"/>`;
      if (metric.reference !== undefined && metric.reference > lo && metric.reference < hi) {
        const y = py(metric.reference).toFixed(1);
        out += `<line class="zeroline" x1="${L}" y1="${y}" x2="${W - R}" y2="${y}" stroke-dasharray="4 3"/>`;
      }
      const every = Math.max(1, Math.round(span.length / 7));
      span.forEach((d, x) => {
        if (x % every && x !== span.length - 1) return;
        out += `<text class="axis" x="${px(x).toFixed(1)}" y="${H - 9}" text-anchor="middle">${dayName(d, { day: "2-digit", month: "short" })}</text>`;
      });
      rows.forEach((r) => {
        let path = "", pen = false;
        r.pts.forEach((p) => {
          if (!p) { pen = false; return; }
          path += `${pen ? "L" : "M"}${px(p[0]).toFixed(1)} ${py(p[1]).toFixed(1)} `;
          pen = true;
        });
        if (path) out += `<path class="series" d="${path.trim()}" stroke="${r.colour}"/>`;
      });
      svg.innerHTML = out;
      svg.setAttribute("aria-label", `${metric.label} across ${rows.length} markets, most recent ${span.length} days`);
    }
    draw();
  }

  /* ---------- table ---------- */
  function table(id, columns) {
    const key = state.dates[state.cursor];
    const day = state.recent.days[key];
    $(id).innerHTML = zones().filter((z) => day[z.id]).map((z) => {
      const m = day[z.id];
      return `<tr><td>${z.label}</td>` +
        columns.map((c) => `<td${c.low && c.low(m) ? ' class="low"' : ""}>${c.get(m)}</td>`).join("") +
        "</tr>";
    }).join("");
  }

  return { load, fault, strips, trend, table, state, zones, zoneColour, scalar, colour, num, pct, dayName, $, LINES };
})();
