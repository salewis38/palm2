"""PALM status page: one self-contained HTML document (CSS and JavaScript inline) served at /.

Kept out of palm.py so the controller stays small and the page can be edited on its own. Put this file next to
palm.py. It is optional: without it palm.py serves a minimal fallback page and /status.json and /metrics are
unaffected. It holds data only (one string, no logic) and needs no packages.

The page reads /status.json and /history.json, inserts values with textContent only (never as markup), and loads
nothing from other sites (the server's Content-Security-Policy would block it anyway).
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PALM</title>
<style>
:root{--bg:#f6f7f9;--fg:#1c1e21;--mut:#6b7280;--card:#fff;--line:#e5e7eb;--ok:#16a34a;--bad:#dc2626;
--s0:#d97706;--s1:#2563eb;--s2:#7c3aed;--s3:#16a34a}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#e6e8eb;--mut:#9aa3af;--card:#171a20;--line:#262a31;
--ok:#22c55e;--bad:#ef4444;--s0:#f59e0b;--s1:#60a5fa;--s2:#a78bfa;--s3:#4ade80}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
header{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:baseline;padding:14px 18px;
border-bottom:1px solid var(--line)}
h1{font-size:20px;margin:0}h1 small{color:var(--mut);font-weight:400;font-size:13px}
#stamp{color:var(--mut);font-size:13px}#stamp.bad{color:var(--bad)}
main{max-width:1100px;margin:0 auto;padding:14px 18px 40px}
h2{font-size:13px;margin:22px 0 8px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.card b{display:block;font-size:22px;font-weight:600}.card span{color:var(--mut);font-size:12px}
.bar{height:6px;background:var(--line);border-radius:3px;margin-top:6px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--ok)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 18px;background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:10px 14px;margin:0}
dt{color:var(--mut)}dd{margin:0}
ul{list-style:none;margin:0;padding:0}
li{padding:6px 12px;border:1px solid var(--line);border-radius:8px;margin-bottom:6px;background:var(--card)}
li.ok{color:var(--ok)}li.bad{border-color:var(--bad)}
.chart{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px;margin-bottom:10px}
.chart h3{margin:0 0 2px;font-size:13px;color:var(--mut);font-weight:500}
svg{width:100%;height:auto;display:block}svg text{fill:var(--mut);font-size:10px}
.legend span{margin-right:12px;font-size:12px;color:var(--mut)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}
</style>
</head>
<body>
<header><h1>PALM <small id="ver"></small></h1><div id="stamp">connecting...</div></header>
<main>
<div class="cards" id="cards"></div>
<h2>Control</h2><dl id="control"></dl>
<h2>System</h2><dl id="system"></dl>
<h2>Health</h2><ul id="health"></ul>
<h2>History</h2><div id="charts"></div>
</main>
<script>
"use strict";
const Q = location.search;
const NS = "http://www.w3.org/2000/svg";
const $ = (id) => document.getElementById(id);

async function get(path) {
  const r = await fetch(path + Q, {cache: "no-store"});
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function num(v, digits, unit) {
  if (v === null || v === undefined) return "-";
  return Number(v).toLocaleString(undefined, {maximumFractionDigits: digits}) + (unit ? " " + unit : "");
}
function ago(s) {
  if (s === null || s === undefined) return "-";
  if (s < 90) return Math.round(s) + " s ago";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  return (s / 3600).toFixed(1) + " h ago";
}
function span(s) {
  if (s < 5400) return Math.max(1, Math.round(s / 60)) + " min";
  if (s < 172800) return (s / 3600).toFixed(1) + " h";
  return (s / 86400).toFixed(1) + " days";
}
function card(label, value, extra) {
  const c = el("div", "card");
  c.appendChild(el("b", "", value));
  c.appendChild(el("span", "", label));
  if (extra) c.appendChild(extra);
  return c;
}
function bar(pct) {
  const b = el("div", "bar");
  const i = el("i");
  i.style.width = Math.max(0, Math.min(100, pct)) + "%";
  b.appendChild(i);
  return b;
}
function row(dl, name, value) {
  dl.appendChild(el("dt", "", name));
  dl.appendChild(el("dd", "", value));
}
function yesno(v) { return v === null || v === undefined ? "unknown" : (v ? "yes" : "no"); }

function render(s) {
  $("ver").textContent = s.service.version;
  const inv = s.inverter, cards = $("cards");
  cards.replaceChildren();
  if (inv) {
    cards.appendChild(card("Battery", num(inv.soc_percent, 0, "%"), bar(inv.soc_percent)));
    cards.appendChild(card("Solar", num(inv.pv_w, 0, "W")));
    cards.appendChild(card("House load", num(inv.load_w, 0, "W")));
    cards.appendChild(card(inv.grid_w >= 0 ? "Grid import" : "Grid export", num(Math.abs(inv.grid_w), 0, "W")));
    const flow = inv.battery_w > 0 ? "Battery discharging" : "Battery charging";
    cards.appendChild(card(flow, num(Math.abs(inv.battery_w), 0, "W")));
    cards.appendChild(card("Grid supply", num(inv.voltage_v, 1, "V") + " / " + num(inv.frequency_hz, 2, "Hz")));
    cards.appendChild(card("Solar today", num(inv.pv_energy_today_wh / 1000, 1, "kWh")));
  }
  if (s.ev.power_w !== null) cards.appendChild(card("EV charger", num(s.ev.power_w, 0, "W")));
  if (s.environment.co2_g_per_kwh !== null) {
    cards.appendChild(card("Grid carbon", num(s.environment.co2_g_per_kwh, 0, "g/kWh")));
  }
  if (s.environment.temp_c !== null) cards.appendChild(card("Outside", num(s.environment.temp_c, 1, "C")));

  const c = s.control, dl = $("control");
  dl.replaceChildren();
  row(dl, "Wanted state", c.desired);
  row(dl, "Applied state", c.applied === null ? "unknown" : c.applied);
  row(dl, "Battery paused", yesno(c.paused));
  row(dl, "Boost until", c.boost_until === null ? "-" : c.boost_until.slice(11, 16));
  row(dl, "Heater on", yesno(c.heater_on));
  row(dl, "EV charging", yesno(c.ev_charging));
  row(dl, "Overnight window", c.offpeak_window + (c.off_peak ? " (open now)" : " (closed)"));
  const done = c.reasserted_window === null ? "-" :
    "window of " + c.reasserted_window + (c.reassert_pending ? " (next one pending)" : "");
  row(dl, "Last overnight re-assert", done);
  if (inv && inv.clock_offset_min !== null) row(dl, "Inverter clock offset", inv.clock_offset_min + " min");
  const p = s.pvoutput;
  const last = p.last_ok === null ? "-" : p.last_ok.slice(11, 16);
  row(dl, "PVOutput", p.enabled ? p.queued + " queued, " + p.writes_ok + " written, last " + last : "disabled");
  row(dl, "Running for", span(s.service.uptime_s));

  const sd = $("system");
  sd.replaceChildren();
  const host = s.system;
  if (host) {
    row(sd, "System uptime", host.uptime_s === null ? "-" : span(host.uptime_s));
    const loads = [host.load1, host.load5, host.load15];
    let text = "-";
    if (loads.every((v) => v !== null && v !== undefined)) {
      const cores = host.cpus ? " (" + host.cpus + (host.cpus === 1 ? " core)" : " cores)") : "";
      const high = host.cpus && host.load1 / host.cpus >= 1 ? " - high" : "";
      text = loads.map((v) => v.toFixed(2)).join(" / ") + cores + high;
    }
    row(sd, "Load average (1 / 5 / 15 min)", text);
  }

  const health = $("health");
  health.replaceChildren();
  const failing = Object.keys(s.health.failing);
  if (inv && !inv.fresh) {
    health.appendChild(el("li", "bad", "Inverter data is stale (last reading " + ago(inv.age_s) + ")"));
  }
  if (!inv) health.appendChild(el("li", "bad", "No inverter reading yet"));
  for (const key of failing) {
    const f = s.health.failing[key];
    health.appendChild(el("li", "bad", key + ": failing for " + span(f.since_s) + ", " + f.count + " occurrence(s)"));
  }
  if (!failing.length && inv && inv.fresh) health.appendChild(el("li", "ok", "All systems normal"));

  const stamp = $("stamp");
  stamp.className = "";
  const reading = inv ? ", inverter reading " + ago(inv.age_s) : "";
  stamp.textContent = "Updated " + new Date().toLocaleTimeString() + reading;
}

function chart(title, t, series, unit) {
  const W = 640, H = 170, L = 46, R = 8, T = 8, B = 20;
  const box = el("div", "chart");
  box.appendChild(el("h3", "", title));
  let lo = Infinity, hi = -Infinity;
  for (const s of series) for (const v of s.d) if (v !== null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  if (!isFinite(lo)) { box.appendChild(el("p", "", "No data yet")); return box; }
  if (unit === "%") { lo = 0; hi = 100; } else if (lo > 0) lo = 0;
  if (hi <= lo) hi = lo + 1;
  const t0 = t[0], t1 = Math.max(t[t.length - 1], t0 + 1);
  const x = (v) => L + (v - t0) / (t1 - t0) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  for (let i = 0; i <= 3; i++) {
    const v = lo + (hi - lo) * i / 3;
    const g = document.createElementNS(NS, "line");
    g.setAttribute("x1", L); g.setAttribute("x2", W - R); g.setAttribute("y1", y(v)); g.setAttribute("y2", y(v));
    g.style.stroke = "var(--line)";
    svg.appendChild(g);
    const lab = document.createElementNS(NS, "text");
    lab.setAttribute("x", L - 4); lab.setAttribute("y", y(v) + 3); lab.setAttribute("text-anchor", "end");
    lab.textContent = Math.round(v);
    svg.appendChild(lab);
  }
  const step = 6 * 3600000;
  for (let ms = Math.ceil(t0 * 1000 / step) * step; ms <= t1 * 1000; ms += step) {
    const lab = document.createElementNS(NS, "text");
    lab.setAttribute("x", x(ms / 1000)); lab.setAttribute("y", H - 5); lab.setAttribute("text-anchor", "middle");
    lab.textContent = new Date(ms).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
    svg.appendChild(lab);
  }
  for (const s of series) {
    let d = "", pen = false;
    s.d.forEach((v, i) => {
      if (v === null) { pen = false; return; }
      d += (pen ? "L" : "M") + x(t[i]).toFixed(1) + " " + y(v).toFixed(1);
      pen = true;
    });
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", d); p.setAttribute("fill", "none"); p.setAttribute("stroke-width", "1.5");
    p.style.stroke = "var(--s" + s.c + ")";
    svg.appendChild(p);
  }
  box.appendChild(svg);
  const legend = el("div", "legend");
  for (const s of series) {
    const item = el("span", "", s.n);
    const sw = el("i");
    sw.style.background = "var(--s" + s.c + ")";
    item.prepend(sw);
    legend.appendChild(item);
  }
  box.appendChild(legend);
  return box;
}

function renderHistory(h) {
  const box = $("charts");
  box.replaceChildren();
  if (!h.t.length) { box.appendChild(el("p", "", "History is still collecting...")); return; }
  box.appendChild(chart("Power (W)", h.t, [{n: "Solar", d: h.pv, c: 0}, {n: "House load", d: h.load, c: 1},
                                           {n: "Grid", d: h.grid, c: 2}, {n: "Battery", d: h.battery, c: 3}], "W"));
  box.appendChild(chart("Battery charge (%)", h.t, [{n: "State of charge", d: h.soc, c: 3}], "%"));
  if (h.ev.some((v) => v !== null)) box.appendChild(chart("EV charger (W)", h.t, [{n: "EV", d: h.ev, c: 1}], "W"));
}

function failed(e) {
  const stamp = $("stamp");
  stamp.className = "bad";
  stamp.textContent = "Connection lost: " + e.message;
}
async function tickStatus() { try { render(await get("/status.json")); } catch (e) { failed(e); } }
async function tickHistory() { try { renderHistory(await get("/history.json")); } catch (e) { failed(e); } }
tickStatus(); tickHistory();
setInterval(tickStatus, 15000);
setInterval(tickHistory, 60000);
</script>
</body>
</html>
"""
