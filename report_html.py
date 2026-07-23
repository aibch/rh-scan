"""Generate a self-contained HTML dashboard (report.html) from scanner data.

Reads data/scanner.db (run build_db.py first if your data lives in JSONL) and
writes report.html — no external assets, works offline, light & dark theme.

Usage:
    python3 report_html.py [--out report.html] [--fragment]

--fragment omits the <!doctype>/<html> wrapper (used for embedding).
"""

import argparse
import html
import math
import os
from datetime import datetime, timedelta, timezone

import auto_paper
import chain_pulse
import db
import paper_trades
import report
import scoring
import surges
from scoring import SCORE_WEIGHTS, parse_ts

RUG_DROP_PCT = -90.0
MATURITY_DAYS = 7

CSS = """
:root {
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e;
  --muted: #898781; --grid: #e1e0d9; --baseline: #c3c2b7;
  --border: rgba(11,11,11,0.10); --accent: #2a78d6; --accent-soft: #cde2fb;
  --up: #006300; --down: #d03b3b; --paper-realized: #9a6b13;
}
@media (prefers-color-scheme: dark) {
  :root {
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --baseline: #383835;
    --border: rgba(255,255,255,0.10); --accent: #3987e5; --accent-soft: #104281;
    --up: #0ca30c; --down: #e66767; --paper-realized: #e1ad45;
  }
}
:root[data-theme="dark"] {
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
  --muted: #898781; --grid: #2c2c2a; --baseline: #383835;
  --border: rgba(255,255,255,0.10); --accent: #3987e5; --accent-soft: #104281;
  --up: #0ca30c; --down: #e66767; --paper-realized: #e1ad45;
}
:root[data-theme="light"] {
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e;
  --muted: #898781; --grid: #e1e0d9; --baseline: #c3c2b7;
  --border: rgba(11,11,11,0.10); --accent: #2a78d6; --accent-soft: #cde2fb;
  --up: #006300; --down: #d03b3b; --paper-realized: #9a6b13;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 1080px; margin: 0 auto; padding: 32px 20px 56px; }
header h1 { font-size: 22px; font-weight: 650; margin: 0 0 4px; }
header .meta { color: var(--ink-2); font-size: 13.5px; }
.dashboard-tabs {
  display: none; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px;
  margin: 24px 0 20px; padding: 4px; background: var(--surface);
  border: 1px solid var(--border); border-radius: 10px;
}
.tabs-ready .dashboard-tabs { display: grid; }
.dashboard-tab {
  min-height: 44px; padding: 9px 14px; border: 1px solid transparent;
  border-radius: 7px; background: transparent; color: var(--ink-2);
  font: inherit; font-weight: 620; cursor: pointer;
}
.dashboard-tab:hover { background: var(--page); color: var(--ink); }
.dashboard-tab[aria-selected="true"] {
  background: var(--accent); border-color: var(--accent); color: #fff;
  box-shadow: inset 0 -2px 0 rgba(0,0,0,0.18);
}
.dashboard-tab:focus-visible, .dashboard-panel:focus-visible {
  outline: 3px solid var(--accent); outline-offset: 2px;
}
.dashboard-panel { min-width: 0; }
.dashboard-panel > .kpis:first-child { margin-top: 0; }
.tabs-ready .dashboard-panel[hidden] { display: none; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: 12px; margin: 24px 0; }
.tile { background: var(--surface); border: 1px solid var(--border);
        border-radius: 8px; padding: 14px 16px; }
.tile .label { font-size: 12.5px; color: var(--ink-2); }
.tile .value { font-size: 26px; font-weight: 600; margin-top: 2px; }
.tile .sub { font-size: 12px; color: var(--muted); margin-top: 2px; }
.paper-kpis { margin: 14px 0 18px; }
.paper-kpis .value { font-size: 22px; }
.auto-kpis { margin: 14px 0 18px; }
.auto-kpis .value { font-size: 21px; }
.card { background: var(--surface); border: 1px solid var(--border);
        border-radius: 8px; padding: 18px 20px; margin: 0 0 20px; }
.card h2 { font-size: 15px; font-weight: 650; margin: 0 0 2px; }
.card .sub { font-size: 12.5px; color: var(--ink-2); margin: 0 0 14px; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
@media (max-width: 860px) { .cols { grid-template-columns: 1fr; } }
.note { color: var(--ink-2); font-size: 13.5px; }
.picks { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
         gap: 14px; }
.pick { border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
.pickhead { display: flex; justify-content: space-between; align-items: flex-start;
            gap: 8px; margin-bottom: 6px; }
.rank { display: inline-block; background: var(--accent); color: #fff;
        border-radius: 5px; font-size: 12px; font-weight: 650;
        padding: 1px 7px; vertical-align: 2px; }
.sym { font-size: 18px; font-weight: 650; margin-left: 4px; }
.poolname { font-size: 12px; color: var(--muted); margin-top: 2px; }
.score { font-size: 30px; font-weight: 650; line-height: 1; white-space: nowrap; }
.score span { font-size: 13px; font-weight: 400; color: var(--muted); }
.pickstats { font-size: 12.5px; color: var(--ink-2); margin-bottom: 10px; }
.brow { display: grid; grid-template-columns: 108px 1fr 26px; align-items: center;
        gap: 8px; font-size: 12px; color: var(--ink-2); margin-top: 5px; }
.brow b { text-align: right; font-weight: 550; color: var(--ink);
          font-variant-numeric: tabular-nums; }
.meter { height: 6px; background: var(--grid); border-radius: 3px; overflow: hidden; }
.meter > div { height: 100%; background: var(--accent); border-radius: 3px; }
.progress { height: 6px; background: var(--accent-soft); border-radius: 3px;
            margin-top: 10px; overflow: hidden; }
.progress > div { height: 100%; background: var(--accent); border-radius: 3px; }
svg text { font: 11.5px system-ui, -apple-system, "Segoe UI", sans-serif;
           font-variant-numeric: tabular-nums; }
.tablewrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th { text-align: left; color: var(--ink-2); font-weight: 550; font-size: 12.5px;
     border-bottom: 1px solid var(--baseline); padding: 6px 10px 6px 0; }
td { border-bottom: 1px solid var(--grid); padding: 7px 10px 7px 0;
     font-variant-numeric: tabular-nums; white-space: nowrap; }
th.num, td.num { text-align: right; }
.up { color: var(--up); } .down { color: var(--down); }
.paper-head { display: flex; justify-content: space-between; align-items: flex-start;
              gap: 12px; }
@media (max-width: 600px) {
  .paper-head { flex-direction: column; }
  .paper-head code { white-space: normal; }
}
.paper-head code, .empty-code { font: 12px/1.5 ui-monospace, SFMono-Regular,
                               Consolas, monospace; background: var(--page);
                               border: 1px solid var(--border); border-radius: 5px;
                               padding: 4px 7px; white-space: nowrap; }
.paper-legend { display: flex; flex-wrap: wrap; gap: 14px; color: var(--ink-2);
                font-size: 12px; margin: 4px 0 10px; }
.legend-line { display: inline-block; width: 22px; margin-right: 5px;
               vertical-align: 3px; border-top: 2px solid var(--accent); }
.legend-line.realized { border-color: var(--paper-realized); border-top-style: dashed; }
.status { display: inline-block; border: 1px solid var(--border); border-radius: 10px;
          padding: 1px 7px; font-size: 11.5px; color: var(--ink-2); }
.status.open, .status.pending, .status.awaiting_fill {
  border-color: var(--accent); color: var(--accent);
}
.status.realized { border-color: var(--paper-realized); color: var(--paper-realized); }
.status.censored, .status.missed_fill, .status.unpriced { border-style: dashed; }
.auto-charts { display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(280px, 1fr);
               gap: 20px; margin-top: 18px; }
.auto-chart { min-width: 0; }
.auto-chart h3 { font-size: 13px; font-weight: 620; margin: 0 0 2px; }
.auto-chart .sub { margin-bottom: 8px; }
.auto-badge { display: inline-block; border: 1px solid var(--accent);
              border-radius: 10px; color: var(--accent); font-size: 11.5px;
              padding: 1px 7px; margin-left: 6px; vertical-align: 1px; }
.auto-badge.historical { border-color: var(--paper-realized);
                         color: var(--paper-realized); }
.auto-waiting { border-left: 3px solid var(--accent); padding: 10px 12px;
                background: var(--page); margin: 14px 0 4px; }
.auto-preview { border-style: dashed; }
.auto-preview .tile { background: var(--page); }
.auto-token { max-width: 180px; overflow: hidden; text-overflow: ellipsis; }
.auto-marks { font-size: 11.5px; line-height: 1.55; min-width: 150px; }
.token-sub { color: var(--muted); font-size: 11.5px; }
.hoverable { cursor: default; }
#tip { position: fixed; pointer-events: none; background: var(--surface);
       color: var(--ink); border: 1px solid var(--border); border-radius: 6px;
       padding: 7px 10px; font-size: 12.5px; line-height: 1.45;
       box-shadow: 0 4px 14px rgba(0,0,0,0.18); display: none; z-index: 10;
       max-width: 280px; white-space: pre-line; }
footer { color: var(--muted); font-size: 12px; margin-top: 8px; }
@media (max-width: 860px) {
  .auto-charts { grid-template-columns: 1fr; }
}
@media print {
  .dashboard-tabs { display: none !important; }
  .dashboard-panel[hidden] { display: block !important; }
}
"""

JS = """
(function () {
  var tip = document.getElementById('tip');
  function show(e) {
    var t = e.target.closest('[data-tip]');
    if (!t) { tip.style.display = 'none'; return; }
    tip.textContent = t.getAttribute('data-tip');
    tip.style.display = 'block';
    var x = e.clientX + 14, y = e.clientY + 14;
    var r = tip.getBoundingClientRect();
    if (x + r.width > window.innerWidth - 8) x = e.clientX - r.width - 10;
    if (y + r.height > window.innerHeight - 8) y = e.clientY - r.height - 10;
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }
  document.addEventListener('mousemove', show);
  document.addEventListener('mouseleave', function () { tip.style.display = 'none'; });
})();
(function () {
  var root = document.querySelector('.wrap');
  var tablist = root && root.querySelector('[data-dashboard-tabs]');
  if (!root || !tablist) return;

  var tabs = Array.prototype.slice.call(
    tablist.querySelectorAll('[role="tab"]')
  );
  var panels = tabs.map(function (tab) {
    return document.getElementById(tab.getAttribute('aria-controls'));
  });
  if (tabs.length !== 2 || panels.some(function (panel) { return !panel; })) return;

  function tabForHash() {
    var id;
    try {
      id = decodeURIComponent(window.location.hash.slice(1));
    } catch (_) {
      return null;
    }
    if (!id) return null;
    if (id === 'scanner-tab') return tabs[0];
    if (id === 'paper-tab') return tabs[1];
    var target = document.getElementById(id);
    if (!target) return null;
    var panel = target.closest('[role="tabpanel"]');
    if (!panel) return null;
    return tabs.find(function (tab) {
      return tab.getAttribute('aria-controls') === panel.id;
    }) || null;
  }

  function replaceHash(panel) {
    if (!window.history || !window.history.replaceState) return;
    try {
      window.history.replaceState(null, '', '#' + panel.id);
    } catch (_) {
      // The report remains fully usable when history mutation is unavailable.
    }
  }

  function activate(tab, focus, updateHash) {
    var activePanel = null;
    tabs.forEach(function (item, index) {
      var selected = item === tab;
      item.setAttribute('aria-selected', selected ? 'true' : 'false');
      item.tabIndex = selected ? 0 : -1;
      panels[index].hidden = !selected;
      if (selected) activePanel = panels[index];
    });
    if (focus) tab.focus();
    if (updateHash && activePanel) replaceHash(activePanel);
  }

  tabs.forEach(function (tab, index) {
    tab.addEventListener('click', function () {
      activate(tab, false, true);
    });
    tab.addEventListener('keydown', function (event) {
      var next = null;
      if (event.key === 'ArrowRight') next = tabs[(index + 1) % tabs.length];
      if (event.key === 'ArrowLeft') {
        next = tabs[(index - 1 + tabs.length) % tabs.length];
      }
      if (event.key === 'Home') next = tabs[0];
      if (event.key === 'End') next = tabs[tabs.length - 1];
      if (!next) return;
      event.preventDefault();
      activate(next, true, true);
    });
  });

  window.addEventListener('hashchange', function () {
    var hashTab = tabForHash();
    if (!hashTab) return;
    activate(hashTab, false, false);
    var id;
    try {
      id = decodeURIComponent(window.location.hash.slice(1));
    } catch (_) {
      return;
    }
    var target = document.getElementById(id);
    if (target && target.getAttribute('role') !== 'tabpanel') {
      window.requestAnimationFrame(function () { target.scrollIntoView(); });
    }
  });

  var initialTab = tabForHash()
    || tabs.find(function (tab) {
      return tab.getAttribute('aria-selected') === 'true';
    })
    || tabs[0];
  activate(initialTab, false, false);
  root.classList.add('tabs-ready');
  tablist.hidden = false;

  if (window.location.hash && tabForHash()) {
    var initialTarget;
    try {
      initialTarget = document.getElementById(
        decodeURIComponent(window.location.hash.slice(1))
      );
    } catch (_) {
      initialTarget = null;
    }
    if (initialTarget && initialTarget.getAttribute('role') !== 'tabpanel') {
      window.requestAnimationFrame(function () { initialTarget.scrollIntoView(); });
    }
  }
})();
"""


def esc(s):
    return html.escape(str(s if s is not None else "?"), quote=True)


def fmt_usd(v):
    if v is None:
        return "–"
    a = abs(v)
    if a >= 1e9: return f"${v/1e9:.2f}B"
    if a >= 1e6: return f"${v/1e6:.2f}M"
    if a >= 1e3: return f"${v/1e3:.1f}k"
    if a >= 1: return f"${v:.2f}"
    return f"${v:.6f}".rstrip("0").rstrip(".")


def fmt_pct(v):
    if v is None:
        return '<span class="note">–</span>'
    cls = "up" if v >= 0 else "down"
    arrow = "▲" if v >= 0 else "▼"
    mag = f"{abs(v):,.0f}" if abs(v) >= 100 else f"{abs(v):.1f}"
    return f'<span class="{cls}">{arrow} {mag}%</span>'


def fmt_signed_usd(v):
    """Human-readable signed money; the sign is explicit without relying on colour."""
    if v is None:
        return "–"
    sign = "+" if v > 0 else "−" if v < 0 else ""
    return sign + fmt_usd(abs(v))


def fmt_quantity(v):
    if v is None:
        return "–"
    v = float(v)
    if abs(v) >= 1_000_000:
        return f"{v:,.0f}"
    if abs(v) >= 1:
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    return f"{v:.8f}".rstrip("0").rstrip(".")


def fmt_token_price(v):
    """Preserve tiny token prices that portfolio-dollar formatting rounds away."""
    if v is None:
        return "–"
    v = float(v)
    a = abs(v)
    if a == 0:
        return "$0"
    if a >= 1:
        text = f"{v:,.8f}"
    elif a >= 1e-2:
        text = f"{v:.10f}"
    elif a >= 1e-6:
        text = f"{v:.12f}"
    elif a >= 1e-12:
        text = f"{v:.16f}"
    else:
        return f"${v:.6e}"
    return "$" + text.rstrip("0").rstrip(".")


def pnl_html(v):
    if v is None:
        return '<span class="note">–</span>'
    cls = "up" if v >= 0 else "down"
    return f'<span class="{cls}">{esc(fmt_signed_usd(v))}</span>'


def meter(frac):
    return (f'<div class="meter"><div style="width:{frac*100:.0f}%"></div></div>')


def picks_section(rows, now):
    cands = scoring.ranked_candidates(rows, now)
    if not cands:
        return ('<div class="card"><h2>Top candidates by health score</h2>'
                '<p class="note">No pools currently pass the eligibility gates '
                '(liquidity ≥ $10k, 24h volume ≥ $1k, ≥ 5 buys and 5 sells).</p></div>')
    cards = []
    for i, c in enumerate(cands[:3], 1):
        r = c["r"]
        breakdown = "".join(
            f'<div class="brow"><span>{esc(name)}</span>{meter(s)}'
            f'<b>{s*100:.0f}</b></div>'
            for (name, _), s in zip(SCORE_WEIGHTS, c["subs"]))
        if c["top10_pct"] is not None:
            ver = "✓ verified" if c["verified"] else "✗ unverified"
            tok = ("transfers OK" if c["transfer_ok"] == 1
                   else "transfer sim n/a" if c["transfer_ok"] is None else "BLOCKED")
            hold = (f' · {c["holders"]:,} holders' if c["holders"] else "")
            safety = f'{ver} · top-10 hold {c["top10_pct"]:.0f}% · {tok}{hold}'
        else:
            safety = "on-chain checks pending"
        cards.append(f"""
      <div class="pick">
        <div class="pickhead">
          <div><span class="rank">#{i}</span>
            <span class="sym">{esc((c["symbol"] or "?")[:14])}</span>
            <div class="poolname">{esc((r["name"] or "?")[:30])}</div></div>
          <div class="score">{c["score"]:.0f}<span>/100</span></div>
        </div>
        <div class="pickstats">liq {esc(fmt_usd(r["liquidity_usd"]))} ·
          vol {esc(fmt_usd(r["vol_h24_usd"]))} ·
          {esc(int(r["buys_h24"] or 0))}/{esc(int(r["sells_h24"] or 0))} buys/sells · {fmt_pct(r["price_change_h24"])}
          <br>{esc(safety)}</div>
        {breakdown}
      </div>""")
    runner_cols = [
        ("#", lambda c: str(c["rank"]), ""),
        ("Token", lambda c: esc((c["symbol"] or "?")[:14]), ""),
        ("Score", lambda c: f'{c["score"]:.0f}', "num"),
        ("Liquidity", lambda c: esc(fmt_usd(c["r"]["liquidity_usd"])), "num"),
        ("Vol 24h", lambda c: esc(fmt_usd(c["r"]["vol_h24_usd"])), "num"),
        ("Vol/liq", lambda c: f'{c["r"]["vol_liq_ratio"]:.1f}'
            if c["r"]["vol_liq_ratio"] is not None else "–", "num"),
        ("Δ 24h", lambda c: fmt_pct(c["r"]["price_change_h24"]), "num"),
    ]
    runners = [{**c, "rank": i} for i, c in enumerate(cands[:10], 1)][3:]
    runners_html = (f'<p class="sub" style="margin-top:16px">Next in line</p>'
                    f'{table_html(runners, runner_cols)}') if runners else ""
    return f"""
  <div class="card">
    <h2>Top 3 candidates by health score</h2>
    <p class="sub">Best pool per token, ranked on market structure: liquidity depth,
      healthy (not manic) volume, balanced two-sided flow, trader breadth, survival
      age, and price stability. {len(cands)} of {len(rows)} pools pass the gates.</p>
    <div class="picks">{"".join(cards)}</div>
    {runners_html}
    <p class="note" style="margin-top:14px">⚠ Research watchlist, not a buy signal.
      Scores now include on-chain safety (top-10 holder concentration, contract
      verification, transfer-block simulation); tokens failing the transfer check
      are excluded outright. Still unchecked: LP lock/burn and deployer history —
      either can still zero a token. Per <b>docs/strategy-plan.md</b> you are in
      Stage 1–2: collect and paper trade before real money.</p>
  </div>"""


def rounded_bar(x, y, w, h, r=4):
    """Horizontal bar path: square left (baseline), rounded right (data end)."""
    w = max(w, r + 1)
    return (f"M{x:.1f},{y:.1f} H{x+w-r:.1f} Q{x+w:.1f},{y:.1f} {x+w:.1f},{y+r:.1f} "
            f"V{y+h-r:.1f} Q{x+w:.1f},{y+h:.1f} {x+w-r:.1f},{y+h:.1f} H{x:.1f} Z")


def bar_chart(rows):
    """Top pools by 24h volume — horizontal bars, sequential accent hue."""
    data = sorted([r for r in rows
                   if r["vol_h24_usd"] and (r["liquidity_usd"] or 0) >= 1000],
                  key=lambda r: r["vol_h24_usd"], reverse=True)[:12]
    if not data:
        return '<p class="note">No volume data yet.</p>'
    vmax = data[0]["vol_h24_usd"]
    label_w, val_w, bar_h, gap, pad_t = 168, 66, 20, 10, 4
    plot_w = 720 - label_w - val_w
    height = pad_t + len(data) * (bar_h + gap)
    parts = [f'<svg viewBox="0 0 720 {height}" width="100%" role="img" '
             f'aria-label="Top pools by 24h volume">']
    for i, r in enumerate(data):
        y = pad_t + i * (bar_h + gap)
        w = plot_w * (r["vol_h24_usd"] / vmax)
        name = (r["name"] or "?")[:24]
        tipv = (f'{r["name"]}\\n24h volume {fmt_usd(r["vol_h24_usd"])}\\n'
                f'liquidity {fmt_usd(r["liquidity_usd"])}\\n'
                f'vol/liq {r["vol_liq_ratio"]:.1f}' if r["vol_liq_ratio"] is not None
                else f'{r["name"]}\\n24h volume {fmt_usd(r["vol_h24_usd"])}')
        tipv = tipv.replace("\\n", "\n")
        parts.append(
            f'<text x="{label_w-8}" y="{y+bar_h/2+4}" text-anchor="end" '
            f'fill="var(--ink-2)">{esc(name)}</text>'
            f'<path d="{rounded_bar(label_w, y, w, bar_h)}" fill="var(--accent)" '
            f'class="hoverable" data-tip="{esc(tipv)}"></path>'
            f'<text x="{label_w+w+6}" y="{y+bar_h/2+4}" fill="var(--muted)">'
            f'{esc(fmt_usd(r["vol_h24_usd"]))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def log_ticks(lo, hi):
    return [10 ** e for e in range(math.floor(math.log10(lo)),
                                   math.ceil(math.log10(hi)) + 1)]


def scatter(rows):
    """Liquidity vs 24h volume, log-log — one hue, hover tooltips."""
    pts = [r for r in rows
           if (r["liquidity_usd"] or 0) >= 100 and (r["vol_h24_usd"] or 0) > 1]
    if len(pts) < 3:
        return '<p class="note">Not enough data yet.</p>'
    W, H, ml, mr, mt, mb = 720, 340, 56, 12, 8, 34
    xs = [r["liquidity_usd"] for r in pts]
    ys = [r["vol_h24_usd"] for r in pts]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)

    def sx(v): return ml + (math.log10(v) - math.log10(x0)) / \
        (math.log10(x1) - math.log10(x0) or 1) * (W - ml - mr)

    def sy(v): return H - mb - (math.log10(v) - math.log10(y0)) / \
        (math.log10(y1) - math.log10(y0) or 1) * (H - mt - mb)

    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
             f'aria-label="Liquidity vs 24h volume, log scales">']
    for t in log_ticks(x0, x1):
        if x0 <= t <= x1:
            x = sx(t)
            parts.append(f'<line x1="{x:.1f}" y1="{mt}" x2="{x:.1f}" y2="{H-mb}" '
                         f'stroke="var(--grid)" stroke-width="1"></line>'
                         f'<text x="{x:.1f}" y="{H-mb+16}" text-anchor="middle" '
                         f'fill="var(--muted)">{esc(fmt_usd(t))}</text>')
    for t in log_ticks(y0, y1):
        if y0 <= t <= y1:
            y = sy(t)
            parts.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{W-mr}" y2="{y:.1f}" '
                         f'stroke="var(--grid)" stroke-width="1"></line>'
                         f'<text x="{ml-6}" y="{y+4:.1f}" text-anchor="end" '
                         f'fill="var(--muted)">{esc(fmt_usd(t))}</text>')
    parts.append(f'<line x1="{ml}" y1="{H-mb}" x2="{W-mr}" y2="{H-mb}" '
                 f'stroke="var(--baseline)" stroke-width="1"></line>')
    for r in pts:
        tipv = (f'{r["name"]}\nliquidity {fmt_usd(r["liquidity_usd"])}\n'
                f'24h volume {fmt_usd(r["vol_h24_usd"])}\n'
                f'price {fmt_usd(r["price_usd"])}')
        parts.append(f'<circle cx="{sx(r["liquidity_usd"]):.1f}" '
                     f'cy="{sy(r["vol_h24_usd"]):.1f}" r="4.5" fill="var(--accent)" '
                     f'stroke="var(--surface)" stroke-width="2" fill-opacity="0.85" '
                     f'class="hoverable" data-tip="{esc(tipv)}"></circle>')
    parts.append(f'<text x="{W-mr}" y="{H-6}" text-anchor="end" fill="var(--ink-2)">'
                 f'liquidity (USD, log) →</text>')
    parts.append(f'<text x="{ml}" y="{mt+2}" fill="var(--ink-2)">24h volume ↑</text>')
    parts.append("</svg>")
    return "".join(parts)


def rug_section(conn, latest, now):
    first_ts = conn.execute("SELECT MIN(ts) FROM snapshots").fetchone()[0]
    days = (now - parse_ts(first_ts)).total_seconds() / 86400 if first_ts else 0
    journeys, censored, pending, unpriced = report.pool_journeys(conn, MATURITY_DAYS)
    mature = [j["change_pct"] for j in journeys]
    if not mature:
        pct = min(100, days / MATURITY_DAYS * 100)
        return (f'<div class="card"><h2>Rug rate</h2>'
                f'<p class="sub">Unlocks once tracked pools are {MATURITY_DAYS} days old</p>'
                f'<p class="note">Collecting data: day {days:.1f} of {MATURITY_DAYS}. '
                f'When pools mature, this section shows the share that lost 90%+ '
                f'or drained within {MATURITY_DAYS} days of first sighting — your '
                f'baseline rug rate.</p>'
                f'<div class="progress"><div style="width:{pct:.0f}%"></div></div></div>')
    rugs = sum(1 for c in mature if c <= RUG_DROP_PCT)
    tol_h = scoring.horizon_tolerance_s(MATURITY_DAYS) / 3600
    denom = len(mature) + censored
    return (f'<div class="card"><h2>Rug rate</h2>'
            f'<p class="sub">Outcome at {MATURITY_DAYS}d after first sighting, '
            f'observed within +{tol_h:.0f}h of target · asset-side price · '
            f'drained pools count as rugs</p>'
            f'<p style="font-size:34px;font-weight:650;margin:0">'
            f'{100*rugs/len(mature):.0f}%</p>'
            f'<p class="note">{rugs} of {len(mature)} measured pools rugged '
            f'(strict bounds {100*rugs/denom:.0f}%–{100*(rugs+censored)/denom:.0f}% '
            f'counting censored) · {censored} censored · {unpriced} unpriced '
            f'excluded · {pending} not yet mature</p></div>')


def sparkline(vals, width=110, height=26):
    """Tiny single-hue trend line; de-emphasized, endpoint dotted."""
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    pts = []
    for i, v in enumerate(vals):
        x = 2 + i * (width - 4) / (len(vals) - 1)
        y = height - 3 - (v - lo) / span * (height - 6)
        pts.append(f"{x:.1f},{y:.1f}")
    lx, ly = pts[-1].split(",")
    return (f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'aria-hidden="true"><polyline points="{" ".join(pts)}" fill="none" '
            f'stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" '
            f'stroke-linecap="round" opacity="0.75"></polyline>'
            f'<circle cx="{lx}" cy="{ly}" r="3" fill="var(--accent)" '
            f'stroke="var(--surface)" stroke-width="2"></circle></svg>')


PULSE_LABELS = {
    "traders": ("Trader slots (24h)", "unique buyers+sellers summed over pools"),
    "volume": ("Real volume (24h)", "pools with real liquidity only"),
    "tvl": ("Total liquidity", "sum across real pools"),
    "active": ("Active markets", "pools with ≥ $1k volume (24h)"),
}


def pulse_section(conn):
    """Additive chain-level view — all math lives in chain_pulse.py."""
    p = chain_pulse.pulse(conn)
    if p is None:
        return ('<div class="card"><h2>Chain pulse</h2><p class="note">'
                'Needs about two days of history to compute trends.</p></div>')
    hist = p["series"][-28:]   # ~7 days of 6h buckets
    rows = []
    for key, w in chain_pulse.PULSE_WEIGHTS:
        c = p["components"][key]
        label, sub = PULSE_LABELS[key]
        val = fmt_usd(c["now"]) if key in ("volume", "tvl") else f'{c["now"]:,.0f}'
        rows.append(
            f'<tr><td>{esc(label)}<br><span class="note" style="font-size:11.5px">'
            f'{esc(sub)}</span></td>'
            f'<td class="num">{esc(val)}</td>'
            f'<td class="num">{fmt_pct(c["change_pct"])}</td>'
            f'<td class="num">{sparkline([b[key] for b in hist])}</td></tr>')
    rows.append(
        f'<tr><td>Launches<br><span class="note" style="font-size:11.5px">'
        f'new pools per 6h — informational, not scored</span></td>'
        f'<td class="num">{p["latest"]["launches"]:,}</td><td class="num">–</td>'
        f'<td class="num">{sparkline([b["launches"] for b in hist])}</td></tr>')
    verdict = ("expanding" if p["score"] >= 55 else
               "contracting" if p["score"] <= 45 else "flat")
    return (f'<div class="card"><h2>Chain pulse</h2>'
            f'<p class="sub">Whole-chain activity momentum vs 24h ago · '
            f'50 = flat, 100 = doubled, 0 = halved · measures participation '
            f'and money flow, not price or social sentiment</p>'
            f'<p style="font-size:34px;font-weight:650;margin:0 0 2px">'
            f'{p["score"]:.0f}<span style="font-size:14px;font-weight:400;'
            f'color:var(--muted)">/100 · {verdict}</span></p>'
            f'<div class="tablewrap"><table><thead><tr><th>Signal</th>'
            f'<th class="num">Now</th><th class="num">vs 24h</th>'
            f'<th class="num">~7d trend</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></div>')


def collection_section(conn):
    """Snapshots captured per 6h — the data-collection trend itself."""
    s = chain_pulse.series(conn)
    if len(s) < 3:
        return ""
    W, H, ml, mr, mt, mb = 720, 200, 52, 10, 10, 30
    xs = [b["epoch"] for b in s]
    ys = [b["snapshots"] for b in s]
    x0, x1 = min(xs), max(xs)
    ymax = max(ys) or 1
    # clean y ticks
    step = 10 ** max(0, len(str(ymax)) - 1)
    while ymax / step < 2:
        step //= 2 or 1
        if step < 1:
            step = 1
            break
    sx = lambda v: ml + (v - x0) / ((x1 - x0) or 1) * (W - ml - mr)
    sy = lambda v: H - mb - v / ymax * (H - mt - mb)
    pts = " ".join(f"{sx(b['epoch']):.1f},{sy(b['snapshots']):.1f}" for b in s)
    area = (f"{ml},{H-mb} " + pts + f" {sx(x1):.1f},{H-mb}")
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
             f'aria-label="Snapshots captured per 6 hours over time">']
    t = step
    while t <= ymax:
        y = sy(t)
        parts.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{W-mr}" y2="{y:.1f}" '
                     f'stroke="var(--grid)" stroke-width="1"></line>'
                     f'<text x="{ml-6}" y="{y+4:.1f}" text-anchor="end" '
                     f'fill="var(--muted)">{t:,}</text>')
        t += step
    # daily x ticks
    day = 86400
    d = (int(x0) // day + 1) * day
    while d <= x1:
        x = sx(d)
        label = datetime.fromtimestamp(d, tz=timezone.utc).strftime("%b %d")
        parts.append(f'<text x="{x:.1f}" y="{H-10}" text-anchor="middle" '
                     f'fill="var(--muted)">{esc(label)}</text>')
        d += day
    parts.append(f'<polygon points="{area}" fill="var(--accent)" '
                 f'fill-opacity="0.1"></polygon>')
    parts.append(f'<polyline points="{pts}" fill="none" stroke="var(--accent)" '
                 f'stroke-width="2" stroke-linejoin="round" '
                 f'stroke-linecap="round"></polyline>')
    lx, ly = sx(xs[-1]), sy(ys[-1])
    parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4" fill="var(--accent)" '
                 f'stroke="var(--surface)" stroke-width="2"></circle>')
    parts.append(f'<line x1="{ml}" y1="{H-mb}" x2="{W-mr}" y2="{H-mb}" '
                 f'stroke="var(--baseline)" stroke-width="1"></line>')
    parts.append("</svg>")
    total = sum(ys)
    return (f'<div class="card"><h2>Collection trend</h2>'
            f'<p class="sub">Snapshots captured per 6 hours · {total:,} total · '
            f'dips mirror scan-cadence gaps (see Coverage KPI)</p>'
            f'{"".join(parts)}</div>')


def surges_section(conn):
    """Additive view over existing snapshot data — reads via surges.py only."""
    items = surges.recent_surges(conn, hours=24)
    if not items:
        return ('<div class="card"><h2>Volume surges (24h)</h2>'
                '<p class="note">No surges detected in the last 24 hours '
                f'(threshold: ≥ {fmt_usd(surges.MIN_DVOL_USD)} traded in one '
                f'scan gap and ≥ {surges.LIQ_RATIO:.0f}× pool liquidity).'
                '</p></div>')
    cols = [
        ("Pool", lambda s: esc((s["name"] or "?")[:26]), ""),
        ("Detected", lambda s: esc(s["ts"][5:16].replace("T", " ")), ""),
        ("Volume in gap", lambda s: esc(fmt_usd(s["dvol"])), "num"),
        ("× liquidity", lambda s: esc(f'{s["ratio"]:.1f}x') if s["ratio"] else "–", "num"),
        ("Price move", lambda s: fmt_pct(s["price_move_pct"]), "num"),
        ("Liq then → now", lambda s: esc(f'{fmt_usd(s["liq_before"])} → '
                                         f'{fmt_usd(s["liq_now"])}'), "num"),
    ]
    return (f'<div class="card"><h2>Volume surges (24h)</h2>'
            f'<p class="sub">Bursts of ≥ {fmt_usd(surges.MIN_DVOL_USD)} traded '
            f'within one scan gap at ≥ {surges.LIQ_RATIO:.0f}× pool liquidity · '
            f'granularity limited by scan cadence</p>'
            f'{table_html(items, cols)}'
            f'<p class="note" style="margin-top:12px">⚠ Watchlist signal, not '
            f'a buy signal: in this dataset, entries at spike detection ran a '
            f'median −37% at +6h and 61% rugged by +24h — most surges here are '
            f'pump-and-dump ignitions. See spike_watch.py for real-time '
            f'alerts.</p></div>')


def table_html(rows, cols):
    head = "".join(f'<th class="{c[2]}">{esc(c[0])}</th>' for c in cols)
    body = []
    for r in rows:
        tds = "".join(f'<td class="{c[2]}">{c[1](r)}</td>' for c in cols)
        body.append(f"<tr>{tds}</tr>")
    return (f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def paper_pnl_chart(points):
    """Portfolio P&L line with a zero baseline and explicit price coverage."""
    points = [p for p in points if p.get("ts")]
    valid_indexes = [i for i, p in enumerate(points) if p.get("pnl_usd") is not None]
    if not valid_indexes:
        return ('<p class="note">Portfolio P&amp;L is unavailable until every open '
                'lot has a post-entry scanner price.</p>')

    W, H, ml, mr, mt, mb = 920, 290, 68, 14, 12, 38
    epochs = [_paper_ts(p["ts"]).timestamp() for p in points]
    totals = [float(points[i]["pnl_usd"]) for i in valid_indexes]
    realized = [float(p.get("realized_pnl_usd") or 0) for p in points]
    raw_lo, raw_hi = min([0.0] + totals + realized), max([0.0] + totals + realized)
    if raw_lo == raw_hi:
        pad = max(1.0, abs(raw_hi) * 0.1)
    else:
        pad = (raw_hi - raw_lo) * 0.12
    lo, hi = raw_lo - pad, raw_hi + pad
    x0, x1 = min(epochs), max(epochs)

    def sx(v):
        if x0 == x1:
            return (ml + W - mr) / 2
        return ml + (v - x0) / (x1 - x0) * (W - ml - mr)

    def sy(v):
        return H - mb - (v - lo) / (hi - lo) * (H - mt - mb)

    realized_pts = " ".join(
        f"{sx(x):.1f},{sy(y):.1f}" for x, y in zip(epochs, realized))
    zero_y = sy(0)
    segments = []
    current = []
    for i, p in enumerate(points):
        if p.get("pnl_usd") is None:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(i)
    if current:
        segments.append(current)
    has_gaps = len(valid_indexes) != len(points)
    parts = [
        '<div class="paper-legend">'
        '<span><i class="legend-line"></i>Total P&amp;L</span>'
        '<span><i class="legend-line realized"></i>Realized P&amp;L</span>'
        '<span>Unrealized = total − realized</span>'
        + ('<span>Shaded gaps = incomplete price coverage</span>' if has_gaps else '')
        + '</div>',
        f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
        f'aria-label="Paper portfolio profit and loss over time in US dollars">',
    ]
    for i in range(5):
        val = lo + (hi - lo) * i / 4
        y = sy(val)
        parts.append(
            f'<line x1="{ml}" y1="{y:.1f}" x2="{W-mr}" y2="{y:.1f}" '
            f'stroke="var(--grid)" stroke-width="1"></line>'
            f'<text x="{ml-7}" y="{y+4:.1f}" text-anchor="end" '
            f'fill="var(--muted)">{esc(fmt_signed_usd(val))}</text>')
    parts.append(
        f'<line x1="{ml}" y1="{zero_y:.1f}" x2="{W-mr}" y2="{zero_y:.1f}" '
        f'stroke="var(--baseline)" stroke-width="1.5"></line>'
        f'<text x="{W-mr}" y="{zero_y-5:.1f}" text-anchor="end" '
        f'fill="var(--muted)">$0 break-even</text>')

    tick_count = min(6, len(points))
    tick_indexes = sorted(set(
        round(i * (len(points) - 1) / max(1, tick_count - 1))
        for i in range(tick_count)))
    short_span = (x1 - x0) < 2 * 86400
    for i in tick_indexes:
        dt = _paper_ts(points[i]["ts"])
        label = dt.strftime("%b %d %H:%M" if short_span else "%b %d")
        parts.append(
            f'<text x="{sx(epochs[i]):.1f}" y="{H-12}" text-anchor="middle" '
            f'fill="var(--muted)">{esc(label)}</text>')

    # Visibly reserve incomplete intervals instead of connecting the total-P&L
    # line across periods whose value is unknown.
    i = 0
    while i < len(points):
        if points[i].get("pnl_usd") is not None:
            i += 1
            continue
        start = i
        while i + 1 < len(points) and points[i + 1].get("pnl_usd") is None:
            i += 1
        end = i
        left = ((sx(epochs[start - 1]) + sx(epochs[start])) / 2
                if start else sx(epochs[start]) - 3)
        right = ((sx(epochs[end]) + sx(epochs[end + 1])) / 2
                 if end + 1 < len(points) else sx(epochs[end]) + 3)
        min_coverage = min(float(points[j].get("price_coverage_pct") or 0)
                           for j in range(start, end + 1))
        tipv = (f'{points[start]["ts"]} to {points[end]["ts"]}\n'
                f'Portfolio P&L unavailable\nMinimum price coverage {min_coverage:.0f}%')
        parts.append(
            f'<rect x="{left:.1f}" y="{mt}" width="{max(right-left, 2):.1f}" '
            f'height="{H-mt-mb}" fill="var(--baseline)" fill-opacity="0.18" '
            f'class="hoverable" data-gap="true" tabindex="0" '
            f'aria-label="{esc(tipv)}" data-tip="{esc(tipv)}"></rect>')
        i += 1

    for segment in segments:
        total_pts = " ".join(
            f'{sx(epochs[i]):.1f},{sy(float(points[i]["pnl_usd"])):.1f}'
            for i in segment)
        if len(segment) > 1:
            area_pts = (f'{sx(epochs[segment[0]]):.1f},{zero_y:.1f} '
                        f'{total_pts} '
                        f'{sx(epochs[segment[-1]]):.1f},{zero_y:.1f}')
            parts.append(
                f'<polygon points="{area_pts}" fill="var(--accent)" '
                f'fill-opacity="0.08"></polygon>')
        parts.append(
            f'<polyline class="paper-total-segment" points="{total_pts}" '
            f'fill="none" stroke="var(--accent)" stroke-width="2.5" '
            f'stroke-linejoin="round" stroke-linecap="round"></polyline>')

    parts.append(
        f'<polyline points="{realized_pts}" fill="none" '
        f'stroke="var(--paper-realized)" stroke-width="1.8" '
        f'stroke-dasharray="6 4" stroke-linejoin="round"></polyline>')
    for i, p in enumerate(points):
        if p.get("pnl_usd") is None:
            continue
        tipv = (f'{p["ts"]}\nTotal P&L {fmt_signed_usd(p["pnl_usd"])}\n'
                f'Realized {fmt_signed_usd(p.get("realized_pnl_usd") or 0)}\n'
                f'Unrealized {fmt_signed_usd(p.get("unrealized_pnl_usd") or 0)}\n'
                f'Price coverage {float(p.get("price_coverage_pct") or 0):.0f}%')
        stale = int(p.get("stale_lots") or 0)
        unpriced = int(p.get("unpriced_lots") or 0)
        if stale or unpriced:
            tipv += f'\n{stale} stale · {unpriced} unpriced open lots'
        parts.append(
            f'<circle cx="{sx(epochs[i]):.1f}" '
            f'cy="{sy(float(p["pnl_usd"])):.1f}" r="6" '
            f'fill="var(--accent)" fill-opacity="0.01" class="hoverable" '
            f'tabindex="0" aria-label="{esc(tipv)}" '
            f'data-tip="{esc(tipv)}"></circle>')
    last = valid_indexes[-1]
    parts.append(
        f'<circle cx="{sx(epochs[last]):.1f}" '
        f'cy="{sy(float(points[last]["pnl_usd"])):.1f}" r="4" '
        f'fill="var(--accent)" stroke="var(--surface)" stroke-width="2"></circle>'
        f'<line x1="{ml}" y1="{H-mb}" x2="{W-mr}" y2="{H-mb}" '
        f'stroke="var(--baseline)" stroke-width="1"></line></svg>')
    return "".join(parts)


def _compact_ts(value):
    if not value:
        return "–"
    return str(value)[:16].replace("T", " ") + " UTC"


def _paper_ts(value):
    """Parse ledger timestamps, including fractional seconds and offsets."""
    raw = str(value).strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw).astimezone(timezone.utc)


def _delay_text(minutes):
    minutes = float(minutes or 0)
    if minutes < 60:
        return f"{minutes:.0f}m"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"


def paper_section(portfolio):
    """Render the append-only paper ledger as a summary-first dashboard."""
    summary = portfolio["summary"]
    lots = portfolio["lots"]
    if not summary.get("total_lots"):
        return """
  <section id="paper-tracker">
    <div class="card">
      <h2>Paper trade tracker</h2>
      <p class="sub">Separate lots, latest recorded scanner marks, and portfolio P&amp;L over time</p>
      <p class="note">No paper trades yet. From the private checkout, run:</p>
      <p><code class="empty-code">python paper_trades.py add</code></p>
      <p class="note">The command prompts for token address, entry price, UTC timestamp,
        and USD amount. Each repeat buy becomes its own lot.</p>
    </div>
  </section>"""

    def tile(label, value, sub="", value_class=""):
        cls = f' {value_class}' if value_class else ""
        return (f'<div class="tile"><div class="label">{esc(label)}</div>'
                f'<div class="value{cls}">{value}</div>'
                + (f'<div class="sub">{sub}</div>' if sub else "") + '</div>')

    coverage = float(summary.get("price_coverage_pct") or 0)
    total_pnl = summary.get("total_pnl_usd")
    total_return = summary.get("total_return_pct")
    pnl_cls = ("up" if total_pnl >= 0 else "down") if total_pnl is not None else ""
    if total_return is not None:
        total_sub = f'{fmt_pct(total_return)} of cumulative deployed'
    elif not summary.get("total_deployed_usd"):
        total_sub = "no deployed capital"
    else:
        total_sub = f'unavailable until price coverage reaches 100% (now {coverage:.0f}%)'
    kpis = "".join([
        tile("Cumulative deployed", esc(fmt_usd(summary.get("total_deployed_usd"))),
             f'{summary.get("total_lots", 0)} recorded lots'),
        tile("Open cost basis", esc(fmt_usd(summary.get("open_cost_basis_usd"))),
             f'{summary.get("open_lots", 0)} open lots'),
        tile("Open marked value", esc(fmt_usd(summary.get("open_market_value_usd"))),
             f'{coverage:.0f}% of open cost priced'),
        tile("Marked unrealized P&L", pnl_html(summary.get("unrealized_pnl_usd")),
             "priced open lots only"),
        tile("Realized P&L", pnl_html(summary.get("realized_pnl_usd")),
             f'{summary.get("closed_lots", 0)} closed lots'),
        tile("Total P&L", esc(fmt_signed_usd(total_pnl)),
             total_sub, pnl_cls),
    ])

    status_order = {"open": 0, "closed": 1, "void": 2}
    lots = sorted(lots, key=lambda lot: (
        status_order.get(lot.get("status"), 9),
        -_paper_ts(lot["entry_ts"]).timestamp()))
    rows = []
    for lot in lots:
        token = lot.get("token") or "?"
        symbol = lot.get("symbol") or (token[:8] + "…" if len(token) > 10 else token)
        trade_id = lot.get("trade_id") or "?"
        note = lot.get("note") or lot.get("close_note") or lot.get("void_reason")
        tipv = (f'{token}\nLot {trade_id}\nRecorded {lot.get("recorded_at") or "?"}'
                + (f'\n{note}' if note else ""))
        status = lot.get("status") or "?"
        if status == "closed":
            mark = (f'{esc(fmt_token_price(lot.get("exit_price_usd")))}<br>'
                    f'<span class="token-sub">exit '
                    f'{esc(_compact_ts(lot.get("exit_ts")))}</span>')
        elif status == "open" and lot.get("mark_price_usd") is not None:
            pstatus = str(lot.get("price_status") or "recorded").replace("_", " ")
            mark = (f'{esc(fmt_token_price(lot.get("mark_price_usd")))}<br>'
                    f'<span class="token-sub">{esc(pstatus)} · '
                    f'{esc(_compact_ts(lot.get("mark_ts")))}</span>')
        elif status == "void":
            mark = '<span class="note">excluded</span>'
        else:
            pstatus = str(lot.get("price_status") or "unpriced").replace("_", " ")
            mark = f'<span class="note">{esc(pstatus)}</span>'
        value = lot.get("value_usd")
        return_pct = lot.get("return_pct")
        entered = esc(_compact_ts(lot.get("entry_ts")))
        if lot.get("backfilled"):
            entered += (f'<br><span class="token-sub">backfilled +'
                        f'{esc(_delay_text(lot.get("entry_delay_minutes")))} later</span>')
        rows.append(
            f'<tr><td class="hoverable" data-tip="{esc(tipv)}"><b>{esc(symbol)}</b><br>'
            f'<span class="token-sub">{esc(trade_id)}</span></td>'
            f'<td>{entered}</td>'
            f'<td class="num">{esc(fmt_usd(lot.get("invested_usd")))}</td>'
            f'<td class="num">{esc(fmt_token_price(lot.get("entry_price_usd")))}</td>'
            f'<td class="num">{esc(fmt_quantity(lot.get("quantity")))}</td>'
            f'<td class="num">{mark}</td>'
            f'<td class="num">{esc(fmt_usd(value))}</td>'
            f'<td class="num">{pnl_html(lot.get("pnl_usd"))}</td>'
            f'<td class="num">{fmt_pct(return_pct)}</td>'
            f'<td><span class="status {esc(status)}">{esc(status)}</span></td></tr>')

    stale = int(summary.get("stale_open_lots") or 0)
    unpriced = int(summary.get("unpriced_open_lots") or 0)
    freshness = (f'{stale} stale and {unpriced} unpriced open lots' if stale or unpriced
                 else 'all open capital has a recent recorded mark')
    warning_html = "".join(
        f'<p class="note">⚠ {esc(w)}</p>' for w in portfolio.get("warnings", []))
    return f"""
  <section id="paper-tracker">
    <div class="card">
      <div class="paper-head"><div><h2>Paper trade tracker</h2>
        <p class="sub">Realized exits + unrealized latest scanner marks · as of
          {esc(portfolio.get("as_of"))}</p></div>
        <code>python paper_trades.py add</code></div>
      <div class="kpis paper-kpis">{kpis}</div>
      <h2>Paper P&amp;L over time</h2>
      <p class="sub">Marked at the deepest-liquidity recorded token price per scan;
        forward-filled between scans · hover points for coverage</p>
      {paper_pnl_chart(portfolio.get("trend", []))}
      <p class="note">Latest recorded prices are research marks, not executable live quotes ·
        {esc(freshness)}.</p>
      {warning_html}
    </div>
    <div class="card">
      <h2>Paper lots</h2>
      <p class="sub">Every entry is independent, including repeat buys of the same token ·
        close one with <code>python paper_trades.py close LOT_ID</code></p>
      <div class="tablewrap"><table><thead><tr>
        <th>Token / lot</th><th>Entered / recorded</th><th class="num">Invested</th>
        <th class="num">Entry price</th><th class="num">Quantity</th>
        <th class="num">Latest mark / exit</th><th class="num">Value / proceeds</th>
        <th class="num">P&amp;L</th><th class="num">Return</th><th>Status</th>
      </tr></thead><tbody>{"".join(rows)}</tbody></table></div>
    </div>
  </section>"""


def _auto_value(row, *keys, default=None):
    """First present non-None value from a strategy payload mapping."""
    if not isinstance(row, dict):
        return default
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def _auto_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _auto_count(value):
    number = _auto_float(value)
    return f"{int(number):,}" if number is not None else "–"


def _auto_entry_epoch(entry):
    raw = _auto_value(entry, "entry_ts", "scan_ts", "ts")
    if not raw:
        return float("-inf")
    try:
        return _paper_ts(raw).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


def _auto_book_has_data(book):
    if not isinstance(book, dict):
        return False
    summary = book.get("summary") or {}
    count = _auto_float(_auto_value(summary, "entry_count", "cohort_count", default=0))
    return bool((count or 0) > 0 or book.get("entries"))


def _auto_median_24h_return(book):
    returns = []
    for entry in (book.get("entries") or []):
        if str(entry.get("status") or "").lower() != "realized":
            continue
        value = _auto_float(_auto_value(entry, "exit_return_pct", "return_pct"))
        if value is not None:
            returns.append(value)
    return scoring.median(returns)


def _auto_summary_kpis(book):
    """Six contract KPIs for one book; callers keep live/history in separate cards."""
    summary = book.get("summary") or {}
    recorded_coverage = _auto_float(_auto_value(
        summary, "recorded_price_coverage_pct", "price_coverage_pct"
    ))
    fresh_coverage = _auto_float(_auto_value(
        summary,
        "fresh_price_coverage_pct",
        "recorded_price_coverage_pct",
        "price_coverage_pct",
    ))
    win_rate = _auto_float(_auto_value(
        summary, "win_rate_observed_pct", "win_rate_pct"
    ))
    win_lower = _auto_float(_auto_value(summary, "win_rate_lower_bound_pct"))
    win_upper = _auto_float(_auto_value(summary, "win_rate_upper_bound_pct"))
    known_pnl = _auto_float(_auto_value(summary, "known_pnl_usd"))
    median_return = _auto_median_24h_return(book)
    realized = _auto_count(_auto_value(summary, "realized_entries", default=0))
    pending = _auto_count(_auto_value(summary, "pending_entries", default=0))
    awaiting = _auto_count(_auto_value(summary, "awaiting_fill_entries", default=0))
    missed = _auto_count(_auto_value(summary, "missed_fill_entries", default=0))
    unpriced = _auto_count(_auto_value(summary, "unpriced_entries", default=0))
    censored = _auto_count(_auto_value(summary, "censored_entries", default=0))
    stale = _auto_count(_auto_value(summary, "stale_pending_entries", default=0))
    unmarked = _auto_count(_auto_value(summary, "unmarked_pending_entries", default=0))

    def tile(label, value, sub="", value_class=""):
        cls = f" {value_class}" if value_class else ""
        return (
            f'<div class="tile"><div class="label">{esc(label)}</div>'
            f'<div class="value{cls}">{value}</div>'
            + (f'<div class="sub">{sub}</div>' if sub else "")
            + "</div>"
        )

    pnl_class = ""
    if known_pnl is not None:
        pnl_class = "up" if known_pnl >= 0 else "down"
    return "".join([
        tile(
            "Cohorts",
            esc(_auto_count(_auto_value(summary, "cohort_count", default=0))),
            f'{esc(_auto_count(_auto_value(summary, "unique_tokens", default=0)))} unique tokens',
        ),
        tile(
            "Deployed",
            esc(fmt_usd(_auto_float(_auto_value(
                summary, "deployed_notional_usd", "total_notional_usd"
            )))),
            f"{pending} open · {realized} realized · {censored} censored · "
            f"{awaiting} awaiting fill · {missed} missed fill · {unpriced} unpriced",
        ),
        tile(
            "Known P&L",
            esc(fmt_signed_usd(known_pnl)),
            f"last-recorded open marks + realized exits · {stale} stale",
            pnl_class,
        ),
        tile(
            "Median 24h outcome",
            fmt_pct(median_return),
            f"{realized} realized entries",
        ),
        tile(
            "Observed win rate",
            fmt_pct(win_rate),
            (
                f"strict bounds {win_lower:.1f}%–{win_upper:.1f}% incl. censored"
                if win_lower is not None and win_upper is not None
                else "realized exits only; bounds await mature entries"
            ),
        ),
        tile(
            "Recorded-price coverage",
            esc(
                f"{recorded_coverage:.0f}%"
                if recorded_coverage is not None else "–"
            ),
            (
                f"fresh {fresh_coverage:.0f}% · {stale} stale · {unmarked} unmarked"
                if fresh_coverage is not None
                else f"{stale} stale · {unmarked} unmarked"
            ),
        ),
    ])


def auto_capture_panel(capture):
    """Logged public-scan acceptance; deliberately not a scheduler uptime claim."""
    capture = capture if isinstance(capture, dict) else {}
    attempted = int(_auto_float(capture.get("attempted_scans")) or 0)
    stamped = int(_auto_float(capture.get("stamped_scans")) or 0)
    gated = int(_auto_float(capture.get("gated_scans")) or 0)
    rate = _auto_float(capture.get("capture_rate_pct"))
    if not attempted:
        return (
            '<div class="auto-waiting"><b>Strategy scan accounting has not started.</b><br>'
            '<span class="note">The first public run after deployment will record '
            'whether its complete, priceable Top-10 cohort was accepted or gated.</span>'
            '</div>'
        )
    reasons = capture.get("reason_counts")
    reason_labels = {
        "stamped": "accepted",
        "partial_scan": "partial scan",
        "missing_scan_metadata": "missing scan metadata",
        "fewer_than_10_priceable_candidates": "fewer than 10 priceable",
    }
    reason_text = ""
    if isinstance(reasons, dict):
        reason_text = " · ".join(
            f"{int(_auto_float(count) or 0)} {reason_labels.get(str(reason), str(reason))}"
            for reason, count in sorted(reasons.items())
        )
    return (
        '<div class="kpis auto-kpis auto-capture-kpis">'
        f'<div class="tile"><div class="label">Logged scan attempts</div>'
        f'<div class="value">{attempted:,}</div>'
        f'<div class="sub">{esc(str(capture.get("first_manifest_ts") or "–"))} onward</div></div>'
        f'<div class="tile"><div class="label">Accepted cohorts</div>'
        f'<div class="value">{stamped:,}</div>'
        f'<div class="sub">{gated:,} gated</div></div>'
        f'<div class="tile"><div class="label">Logged-attempt acceptance</div>'
        f'<div class="value">{esc(f"{rate:.0f}%" if rate is not None else "–")}</div>'
        f'<div class="sub">{esc(reason_text or "reason counts unavailable")}</div></div>'
        '</div><p class="note">Acceptance uses only public runs that reached and '
        'persisted the pick-log step. Scheduler misses, failures before logging, '
        'and dropped pushes are monitored by workflow history and are not in this denominator.</p>'
    )


def auto_strategy_segments(book):
    """Compact live-book audit by immutable strategy/score version."""
    segments = book.get("segments") if isinstance(book, dict) else None
    if not isinstance(segments, list) or not segments:
        return ""
    rows = []
    entries = book.get("entries") or []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        sid = str(segment.get("strategy_id") or "?")
        summary = segment.get("summary") if isinstance(segment.get("summary"), dict) else {}
        returns = [
            _auto_float(entry.get("exit_return_pct"))
            for entry in entries
            if entry.get("strategy_id") == sid and entry.get("status") == "realized"
        ]
        median_return = scoring.median([value for value in returns if value is not None])
        rows.append(
            '<tr>'
            f'<td><code>{esc(sid)}</code></td>'
            f'<td class="num">v{esc(segment.get("score_version", "?"))}</td>'
            f'<td class="num">{esc(_auto_count(summary.get("cohort_count", 0)))}</td>'
            f'<td class="num">{esc(_auto_count(summary.get("entry_count", 0)))}</td>'
            f'<td class="num">{esc(_auto_count(summary.get("deployed_entries", 0)))}</td>'
            f'<td class="num">{esc(_auto_count(summary.get("realized_entries", 0)))}</td>'
            f'<td class="num">{esc(_auto_count(summary.get("censored_entries", 0)))}</td>'
            f'<td class="num">{esc(_auto_count(summary.get("missed_fill_entries", 0)))}</td>'
            f'<td class="num">{fmt_pct(median_return)}</td>'
            '</tr>'
        )
    if not rows:
        return ""
    return (
        '<h3 style="margin-top:16px">Live strategy versions</h3>'
        '<p class="sub">Combined headline totals retain every immutable stamped '
        'version; this table keeps their outcomes auditable separately.</p>'
        '<div class="tablewrap"><table><thead><tr>'
        '<th>Strategy</th><th class="num">Score</th><th class="num">Cohorts</th>'
        '<th class="num">Signals</th><th class="num">Deployed</th>'
        '<th class="num">Observed</th><th class="num">Censored</th>'
        '<th class="num">Missed fill</th><th class="num">Median 24h</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def auto_realized_pnl_chart(points):
    """Compact cumulative realized-P&L line for a prospective or historical book."""
    rows = []
    for point in points or []:
        raw_ts = _auto_value(point, "ts", "exit_ts", "timestamp")
        value = _auto_float(_auto_value(
            point,
            "cumulative_pnl_usd",
            "cumulative_realized_pnl_usd",
            "realized_pnl_usd",
        ))
        if raw_ts and value is not None:
            try:
                rows.append((_paper_ts(raw_ts), value, point))
            except (TypeError, ValueError):
                continue
    rows.sort(key=lambda item: item[0])
    if not rows:
        return ('<p class="note">The realized curve appears after the first '
                '24-hour exit is observed.</p>')

    rows = _auto_sample_trend(rows)

    W, H, ml, mr, mt, mb = 610, 238, 66, 14, 12, 34
    epochs = [row[0].timestamp() for row in rows]
    values = [row[1] for row in rows]
    raw_lo, raw_hi = min([0.0] + values), max([0.0] + values)
    pad = max((raw_hi - raw_lo) * 0.12, 1.0)
    lo, hi = raw_lo - pad, raw_hi + pad
    x0, x1 = min(epochs), max(epochs)

    def sx(value):
        if x0 == x1:
            return (ml + W - mr) / 2
        return ml + (value - x0) / (x1 - x0) * (W - ml - mr)

    def sy(value):
        return H - mb - (value - lo) / (hi - lo) * (H - mt - mb)

    parts = [
        f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
        'aria-label="Cumulative realized 24-hour profit and loss in US dollars">',
    ]
    for i in range(4):
        value = lo + (hi - lo) * i / 3
        y = sy(value)
        parts.append(
            f'<line x1="{ml}" y1="{y:.1f}" x2="{W-mr}" y2="{y:.1f}" '
            f'stroke="var(--grid)" stroke-width="1"></line>'
            f'<text x="{ml-7}" y="{y+4:.1f}" text-anchor="end" '
            f'fill="var(--muted)">{esc(fmt_signed_usd(value))}</text>'
        )
    zero_y = sy(0)
    parts.append(
        f'<line x1="{ml}" y1="{zero_y:.1f}" x2="{W-mr}" y2="{zero_y:.1f}" '
        'stroke="var(--baseline)" stroke-width="1.5"></line>'
    )
    path = " ".join(
        f"{sx(epoch):.1f},{sy(value):.1f}"
        for epoch, value in zip(epochs, values)
    )
    parts.append(
        f'<polyline points="{path}" fill="none" stroke="var(--accent)" '
        'stroke-width="2.5" stroke-linecap="round" '
        'stroke-linejoin="round"></polyline>'
    )

    tick_count = min(5, len(rows))
    tick_indexes = sorted(set(
        round(i * (len(rows) - 1) / max(tick_count - 1, 1))
        for i in range(tick_count)
    ))
    short_span = (x1 - x0) < 2 * 86400
    for i in tick_indexes:
        label = rows[i][0].strftime("%b %d %H:%M" if short_span else "%b %d")
        parts.append(
            f'<text x="{sx(epochs[i]):.1f}" y="{H-10}" text-anchor="middle" '
            f'fill="var(--muted)">{esc(label)}</text>'
        )
    for i, (dt, value, raw) in enumerate(rows):
        period = _auto_float(_auto_value(raw, "period_pnl_usd"))
        entries = _auto_value(raw, "cumulative_entries", "period_entries")
        tip = f"{canonical_auto_ts(dt)}\nCumulative P&L {fmt_signed_usd(value)}"
        if period is not None:
            tip += f"\nPeriod P&L {fmt_signed_usd(period)}"
        if entries is not None:
            tip += f"\nEntries {entries}"
        parts.append(
            f'<circle cx="{sx(epochs[i]):.1f}" cy="{sy(value):.1f}" r="5" '
            'fill="var(--accent)" fill-opacity="0.02" class="hoverable" '
            f'tabindex="0" aria-label="{esc(tip)}" data-tip="{esc(tip)}"></circle>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _auto_sample_trend(rows, max_points=180):
    """Bound trend payload while retaining each bucket's first/min/max/last."""
    rows = list(rows)
    if len(rows) <= max_points:
        return rows
    bucket_count = max((max_points - 2) // 4, 1)
    interior = rows[1:-1]
    bucket_size = max(math.ceil(len(interior) / bucket_count), 1)
    sampled = [rows[0]]
    for start in range(0, len(interior), bucket_size):
        bucket = interior[start:start + bucket_size]
        candidates = {
            0,
            len(bucket) - 1,
            min(range(len(bucket)), key=lambda index: bucket[index][1]),
            max(range(len(bucket)), key=lambda index: bucket[index][1]),
        }
        sampled.extend(bucket[index] for index in sorted(candidates))
    sampled.append(rows[-1])
    return sampled[:max_points - 1] + [rows[-1]] if len(sampled) > max_points else sampled


def canonical_auto_ts(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def auto_rank_return_chart(stats):
    """Horizontal median-return bars for entry ranks 1–10."""
    rows = []
    for row in stats or []:
        rank = _auto_float(_auto_value(row, "rank"))
        value = _auto_float(_auto_value(
            row, "median_return_pct", "mean_return_pct", "return_pct"
        ))
        if rank is not None and value is not None:
            rows.append((int(rank), value, row))
    rows.sort(key=lambda item: item[0])
    rows = rows[:10]
    if not rows:
        return ('<p class="note">Rank returns appear after the first realized '
                '24-hour cohorts.</p>')

    W, row_h, ml, mr, mt, mb = 410, 25, 46, 64, 10, 28
    H = mt + mb + row_h * len(rows)
    values = [row[1] for row in rows]
    lo, hi = min([0.0] + values), max([0.0] + values)
    if lo == hi:
        pad = max(abs(hi) * 0.1, 1.0)
        lo, hi = lo - pad, hi + pad
    else:
        pad = (hi - lo) * 0.08
        lo, hi = lo - pad, hi + pad

    def sx(value):
        return ml + (value - lo) / (hi - lo) * (W - ml - mr)

    zero_x = sx(0)
    parts = [
        f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
        'aria-label="Median 24-hour return by entry rank">',
        f'<line x1="{zero_x:.1f}" y1="{mt}" x2="{zero_x:.1f}" '
        f'y2="{H-mb}" stroke="var(--baseline)" stroke-width="1.5"></line>',
    ]
    for i, (rank, value, raw) in enumerate(rows):
        y = mt + i * row_h + 4
        x = sx(value)
        left, width = min(zero_x, x), max(abs(x - zero_x), 1.5)
        color = "var(--up)" if value >= 0 else "var(--down)"
        realized = _auto_value(raw, "realized", "realized_entries", default=0)
        tip = (
            f"Rank {rank}\nMedian 24h return {value:+.1f}%\n"
            f"Realized entries {realized}"
        )
        parts.append(
            f'<text x="{ml-8}" y="{y+12:.1f}" text-anchor="end" '
            f'fill="var(--ink-2)">#{rank}</text>'
            f'<rect x="{left:.1f}" y="{y:.1f}" width="{width:.1f}" height="16" '
            f'fill="{color}" fill-opacity="0.72" class="hoverable" tabindex="0" '
            f'aria-label="{esc(tip)}" data-tip="{esc(tip)}"></rect>'
            f'<text x="{W-mr+7}" y="{y+12:.1f}" fill="{color}">'
            f'{value:+.1f}%</text>'
        )
    parts.append(
        f'<text x="{ml}" y="{H-8}" fill="var(--muted)">loss</text>'
        f'<text x="{W-mr}" y="{H-8}" text-anchor="end" '
        'fill="var(--muted)">gain</text></svg>'
    )
    return "".join(parts)


def auto_score_band_chart(stats):
    """Bounded median-return bars for the engine's score-band rows."""
    rows = []
    for row in stats or []:
        band = str(_auto_value(row, "band", default="?"))
        value = _auto_float(_auto_value(
            row, "median_return_pct", "mean_return_pct"
        ))
        if value is not None:
            rows.append((band, value, row))
    rows = rows[:8]
    if not rows:
        return ('<p class="note">Score-band outcomes appear after the first '
                'realized 24-hour cohorts.</p>')

    W, row_h, ml, mr, mt, mb = 430, 27, 76, 66, 10, 28
    H = mt + mb + row_h * len(rows)
    values = [row[1] for row in rows]
    lo, hi = min([0.0] + values), max([0.0] + values)
    if lo == hi:
        pad = max(abs(hi) * 0.1, 1.0)
        lo, hi = lo - pad, hi + pad
    else:
        pad = (hi - lo) * 0.08
        lo, hi = lo - pad, hi + pad

    def sx(value):
        return ml + (value - lo) / (hi - lo) * (W - ml - mr)

    zero_x = sx(0)
    parts = [
        f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
        'aria-label="Median 24-hour return by entry score band">',
        '<title>Median 24-hour return by entry score band</title>',
        f'<line x1="{zero_x:.1f}" y1="{mt}" x2="{zero_x:.1f}" '
        f'y2="{H-mb}" stroke="var(--baseline)" stroke-width="1.5"></line>',
    ]
    for i, (band, value, raw) in enumerate(rows):
        y = mt + i * row_h + 5
        x = sx(value)
        left, width = min(zero_x, x), max(abs(x - zero_x), 1.5)
        color = "var(--up)" if value >= 0 else "var(--down)"
        realized = int(_auto_float(_auto_value(raw, "realized", default=0)) or 0)
        pending = int(_auto_float(_auto_value(raw, "pending", default=0)) or 0)
        censored = int(_auto_float(_auto_value(raw, "censored", default=0)) or 0)
        win_rate = _auto_float(_auto_value(raw, "win_rate_pct"))
        rugs = int(_auto_float(_auto_value(raw, "rug_count", default=0)) or 0)
        tip = (
            f"Score band {band}\nMedian 24h return {value:+.1f}%\n"
            f"Realized {realized} · pending {pending} · censored {censored}\n"
            f"Win rate {win_rate:.1f}% · rugs {rugs}"
            if win_rate is not None else
            f"Score band {band}\nMedian 24h return {value:+.1f}%\n"
            f"Realized {realized} · pending {pending} · censored {censored}\n"
            f"Win rate unavailable · rugs {rugs}"
        )
        display_band = band if len(band) <= 10 else band[:9] + "…"
        parts.append(
            f'<text x="{ml-8}" y="{y+12:.1f}" text-anchor="end" '
            f'fill="var(--ink-2)">{esc(display_band)}</text>'
            f'<rect x="{left:.1f}" y="{y:.1f}" width="{width:.1f}" height="16" '
            f'fill="{color}" fill-opacity="0.72" class="hoverable" tabindex="0" '
            f'aria-label="{esc(tip)}" data-tip="{esc(tip)}"></rect>'
            f'<text x="{W-mr+7}" y="{y+12:.1f}" fill="{color}">'
            f'{value:+.1f}%</text>'
        )
    parts.append(
        f'<text x="{ml}" y="{H-8}" fill="var(--muted)">loss</text>'
        f'<text x="{W-mr}" y="{H-8}" text-anchor="end" '
        'fill="var(--muted)">gain</text></svg>'
    )
    return "".join(parts)


def auto_token_exposure_chart(entries, pending_only=False):
    """Top-token notional shares, bounded to six tokens plus Other."""
    grouped = {}
    for entry in entries or []:
        if pending_only and str(entry.get("status") or "").lower() != "pending":
            continue
        notional = _auto_float(_auto_value(entry, "notional_usd"))
        if notional is None or notional <= 0:
            continue
        token = str(_auto_value(entry, "token", default="?"))
        symbol = str(_auto_value(entry, "symbol", default="") or "")
        item = grouped.setdefault(token, {
            "token": token,
            "symbol": symbol,
            "notional": 0.0,
            "entries": 0,
        })
        item["notional"] += notional
        item["entries"] += 1

    items = sorted(grouped.values(), key=lambda item: (
        -item["notional"], item["token"]
    ))
    if not items:
        return (
            '<p class="note">No pending live exposure yet.</p>'
            if pending_only else
            '<p class="note">Token concentration appears after entries are logged.</p>'
        )
    total = sum(item["notional"] for item in items)
    visible = items[:6]
    if len(items) > 6:
        visible.append({
            "token": None,
            "symbol": "Other",
            "notional": sum(item["notional"] for item in items[6:]),
            "entries": sum(item["entries"] for item in items[6:]),
        })

    W, row_h, ml, mr, mt, mb = 430, 27, 92, 58, 10, 27
    H = mt + mb + row_h * len(visible)
    aria_title = (
        "Pending notional exposure share by token"
        if pending_only else
        "Historical entry notional concentration by token"
    )
    parts = [
        f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
        f'aria-label="{esc(aria_title)}">',
        f"<title>{esc(aria_title)}</title>",
    ]
    for i, item in enumerate(visible):
        share = item["notional"] / total * 100
        y = mt + i * row_h + 5
        width = max(share / 100 * (W - ml - mr), 1.5)
        token = item["token"]
        label = item["symbol"] or (
            token[:7] + "…" if token and len(token) > 8 else token or "Other"
        )
        display_label = label if len(label) <= 12 else label[:11] + "…"
        tip = (
            f"{label}\nNotional {fmt_usd(item['notional'])}\n"
            f"Share {share:.1f}%\nEntries {item['entries']}"
        )
        if token:
            tip += f"\n{token}"
        parts.append(
            f'<text x="{ml-8}" y="{y+12:.1f}" text-anchor="end" '
            f'fill="var(--ink-2)">{esc(display_label)}</text>'
            f'<rect x="{ml}" y="{y}" width="{width:.1f}" height="16" '
            'fill="var(--accent)" fill-opacity="0.72" class="hoverable" '
            f'tabindex="0" aria-label="{esc(tip)}" data-tip="{esc(tip)}"></rect>'
            f'<text x="{W-mr+7}" y="{y+12:.1f}" fill="var(--accent)">'
            f'{share:.1f}%</text>'
        )
    source_label = "pending notional" if pending_only else "all entry notional"
    parts.append(
        f'<text x="{ml}" y="{H-7}" fill="var(--muted)">share of '
        f'{esc(source_label)}</text></svg>'
    )
    return "".join(parts)


def auto_entry_table(entries, limit=24):
    """Recent strategy entries with fixed columns and a bounded row count."""
    rows = sorted(entries or [], key=_auto_entry_epoch, reverse=True)[:limit]
    if not rows:
        return '<p class="note">No entries in this book yet.</p>'

    body = []
    for entry in rows:
        status = str(_auto_value(entry, "status", default="pending")).lower()
        fill_ts = _auto_value(entry, "entry_ts", "fill_ts")
        decision_ts = _auto_value(entry, "decision_ts", "logged_at", "scan_ts")
        ts = fill_ts or decision_ts
        token = str(_auto_value(entry, "token", default="?"))
        symbol = str(_auto_value(entry, "symbol", default="") or "")
        token_label = symbol or (token[:8] + "…" if len(token) > 10 else token)
        entry_id = _auto_value(entry, "entry_id", default="?")
        pool = _auto_value(entry, "pool", default="?")
        tip = f"{token}\nEntry {entry_id}\nPool {pool}"

        entry_price = _auto_float(_auto_value(entry, "entry_price_usd"))
        if status == "realized":
            current_price = _auto_float(_auto_value(entry, "exit_price_usd"))
            current_ts = _auto_value(entry, "exit_ts")
            current_label = "24h-window exit"
            pnl = _auto_float(_auto_value(entry, "realized_pnl_usd"))
            return_pct = _auto_float(_auto_value(entry, "exit_return_pct"))
            notional = _auto_float(_auto_value(entry, "notional_usd"))
            value = (
                notional + pnl
                if notional is not None and pnl is not None else None
            )
        else:
            current_price = _auto_float(_auto_value(entry, "mark_price_usd"))
            current_ts = _auto_value(entry, "mark_ts")
            current_label = "latest mark"
            pnl = _auto_float(_auto_value(entry, "marked_pnl_usd"))
            return_pct = _auto_float(_auto_value(entry, "mark_return_pct"))
            value = _auto_float(_auto_value(entry, "marked_value_usd"))

        if current_price is None:
            current_html = '<span class="note">unpriced</span>'
        else:
            current_html = esc(fmt_token_price(current_price))
            if current_ts:
                current_html += (
                    f'<br><span class="token-sub">{esc(current_label)} · '
                    f'{esc(_compact_ts(current_ts))}</span>'
                )
            mark_age = _auto_float(_auto_value(entry, "mark_age_hours"))
            if status == "pending" and mark_age is not None:
                freshness = (
                    f"stale · {_delay_text(mark_age * 60)} old"
                    if entry.get("stale_mark") else
                    f"{_delay_text(mark_age * 60)} old"
                )
                current_html += (
                    f'<br><span class="token-sub">{esc(freshness)}</span>'
                )
        rank = _auto_value(entry, "rank", default="?")
        score = _auto_float(_auto_value(entry, "score"))
        version = _auto_value(entry, "score_version", default="?")
        marks = entry.get("marks") if isinstance(entry.get("marks"), dict) else {}
        mark_parts = []
        for key, horizon_label in (
            ("1h", "1h"), ("6h", "6h"), ("72h", "3d"), ("168h", "7d")
        ):
            mark = marks.get(key) if isinstance(marks.get(key), dict) else {}
            mark_status = str(mark.get("status") or "pending")
            mark_return = _auto_float(mark.get("return_pct"))
            if mark_return is not None:
                mark_text = f"{mark_return:+.1f}%"
            elif mark_status == "pending":
                mark_text = "pending"
            elif mark_status == "awaiting_fill":
                mark_text = "awaiting fill"
            elif mark_status == "missed_fill":
                mark_text = "missed fill"
            elif mark_status == "unpriced":
                mark_text = "unpriced"
            else:
                mark_text = "censored"
            target_ts = mark.get("target_ts")
            observed_ts = mark.get("observed_ts")
            window_end_ts = mark.get("window_end_ts")
            delay_hours = _auto_float(mark.get("observation_delay_hours"))
            mark_tip = [f"{horizon_label} target"]
            if target_ts:
                mark_tip.append(f"target {target_ts}")
            if observed_ts:
                observed_note = f"observed {observed_ts}"
                if delay_hours is not None:
                    observed_note += f" ({delay_hours:+.1f}h from target)"
                mark_tip.append(observed_note)
            elif window_end_ts:
                mark_tip.append(f"window ends {window_end_ts}")
            mark_parts.append(
                f'<span class="hoverable" tabindex="0" '
                f'data-tip="{esc(chr(10).join(mark_tip))}" '
                f'aria-label="{esc(" — ".join(mark_tip))}: {esc(mark_text)}">'
                f'<b>{esc(horizon_label)}</b> {esc(mark_text)}</span>'
            )
        marks_html = " · ".join(mark_parts[:2]) + "<br>" + " · ".join(mark_parts[2:])
        entered_html = esc(_compact_ts(ts))
        logged_at = _auto_value(entry, "logged_at")
        if status == "awaiting_fill":
            entered_html += (
                '<br><span class="token-sub">awaiting price within 2h fill window</span>'
            )
        elif status == "missed_fill":
            deadline = _auto_value(entry, "fill_deadline_ts")
            entered_html += (
                '<br><span class="token-sub">fill window expired'
                + (f' · {esc(_compact_ts(deadline))}' if deadline else "")
                + '</span>'
            )
        elif logged_at and fill_ts:
            try:
                delay_minutes = max(
                    (_paper_ts(fill_ts) - _paper_ts(logged_at)).total_seconds() / 60,
                    0,
                )
                entered_html += (
                    f'<br><span class="token-sub">filled +'
                    f'{esc(_delay_text(delay_minutes))} after signal</span>'
                )
            except (TypeError, ValueError):
                pass
        body.append(
            f'<tr><td>{entered_html}</td>'
            f'<td class="num">#{esc(rank)}</td>'
            f'<td class="auto-token hoverable" data-tip="{esc(tip)}">'
            f'<b>{esc(token_label)}</b><br>'
            f'<span class="token-sub">{esc(token)}</span></td>'
            f'<td class="num">{esc(f"{score:.1f}" if score is not None else "–")}</td>'
            f'<td class="num">v{esc(version)}</td>'
            f'<td class="num">{esc(fmt_token_price(entry_price))}</td>'
            f'<td class="num">{current_html}</td>'
            f'<td class="num auto-marks">{marks_html}</td>'
            f'<td class="num">{esc(fmt_usd(value))}</td>'
            f'<td class="num">{pnl_html(pnl)}</td>'
            f'<td class="num">{fmt_pct(return_pct)}</td>'
            f'<td><span class="status {esc(status)}">'
            f'{esc(status.replace("_", " "))}</span></td></tr>'
        )
    return (
        '<div class="tablewrap"><table><thead><tr>'
        '<th>Signal / fill</th><th class="num">Rank</th><th>Token</th>'
        '<th class="num">Score</th><th class="num">Version</th>'
        '<th class="num">Entry price</th><th class="num">Current / exit</th>'
        '<th class="num">Forward marks</th>'
        '<th class="num">Value</th><th class="num">P&amp;L</th>'
        '<th class="num">Return</th><th>Status</th>'
        f'</tr></thead><tbody>{"".join(body)}</tbody></table></div>'
    )


def _auto_book_dashboard(book, heading, badge, historical=False):
    summary = book.get("summary") or {}
    rank_stats = book.get("rank_stats") or []
    score_stats = book.get("score_stats") or []
    trend = book.get("realized_trend") or []
    entries = book.get("entries") or []
    realized = _auto_count(_auto_value(summary, "realized_entries", default=0))
    qualifier = (
        "Retrospective, backdated scan-price replay only: ranks were finalized "
        "later in each workflow, so these non-executable outcomes are not "
        "directly comparable to live post-log fills and never enter live KPIs."
        if historical else
        "Prospective signals fill only at the first valid recorded price within "
        "two hours after logging; later quotes cannot revive a missed fill."
    )
    segments_html = "" if historical else auto_strategy_segments(book)
    return f"""
    <div class="card{" auto-preview" if historical else ""}">
      <h2>{esc(heading)}<span class="auto-badge{" historical" if historical else ""}">
        {esc(badge)}</span></h2>
      <p class="sub">{esc(qualifier)}</p>
      <div class="kpis auto-kpis">{_auto_summary_kpis(book)}</div>
      {segments_html}
      <div class="auto-charts">
        <div class="auto-chart">
          <h3>Cumulative realized 24h P&amp;L</h3>
          <p class="sub">Closed 24-hour cohorts only · {esc(realized)} realized entries</p>
          {auto_realized_pnl_chart(trend)}
        </div>
        <div class="auto-chart">
          <h3>Median 24h return by entry rank</h3>
          <p class="sub">Ranks remain separate; pending and censored entries are excluded</p>
          {auto_rank_return_chart(rank_stats)}
        </div>
      </div>
      <div class="auto-charts">
        <div class="auto-chart">
          <h3>Median 24h return by score band</h3>
          <p class="sub">Realized exits in this {esc("historical" if historical else "live")} book
            only · pending and censored entries are excluded</p>
          {auto_score_band_chart(score_stats)}
        </div>
        <div class="auto-chart">
          <h3>{esc("Historical entry concentration" if historical else "Pending exposure by token")}</h3>
          <p class="sub">{
            esc("All historical entry notionals; persistence-weighted, not current exposure"
                if historical else
                "Pending prospective notionals only; realized and censored entries are excluded")
          }</p>
          {auto_token_exposure_chart(entries, pending_only=not historical)}
        </div>
      </div>
      <h2 style="margin-top:18px">Recent {esc("historical" if historical else "live")} entries</h2>
      <p class="sub">Latest {min(len(entries), 12 if historical else 24)} of
        {len(entries):,} entries · exact token address appears below the symbol</p>
      {auto_entry_table(entries, limit=12 if historical else 24)}
    </div>"""


def auto_strategy_section(payload):
    """Render prospective and historical Top-10 books without blending metrics."""
    prospective = payload.get("prospective") or {}
    historical = payload.get("historical") or {}
    version = _auto_value(payload, "score_version", default="?")
    notional = _auto_float(_auto_value(payload, "notional_usd"))
    hold = _auto_float(_auto_value(payload, "hold_hours"))
    fill_window = _auto_float(_auto_value(payload, "fill_window_hours"))
    outcome_tolerance = _auto_float(_auto_value(payload, "outcome_tolerance_hours"))
    strategy_id = _auto_value(payload, "strategy_id", default="top-10")
    capture = payload.get("capture") if isinstance(payload.get("capture"), dict) else {}
    as_of = _auto_value(payload, "as_of", default="?")
    metadata = [
        f"current strategy {strategy_id}",
        f"current score v{version}",
        f"{fmt_usd(notional)} per ranked entry" if notional is not None else None,
        f"{fill_window:g}h fill window" if fill_window is not None else None,
        f"{hold:g}h exit target" if hold is not None else None,
        (
            f"+{outcome_tolerance:g}h observation tolerance"
            if outcome_tolerance is not None else None
        ),
        f"as of {as_of}",
    ]
    metadata_text = " · ".join(str(item) for item in metadata if item)

    parts = [
        '<section id="automatic-top10-strategy">',
        '<div class="card">',
        '<h2>Automatic Top-10 strategy'
        '<span class="auto-badge">live prospective</span></h2>',
        f'<p class="sub">{esc(metadata_text)}</p>',
        '<p class="note">Method: the 24h target uses the first recorded quote '
        'from target through +6h and is censored if none arrives. Live signals '
        'have a two-hour post-log fill window. The historical preview instead '
        'replays backdated scan quotes, so it is non-executable and not directly '
        'comparable to live fills. All values are gross of costs.</p>',
        auto_capture_panel(capture),
    ]
    live_has_data = _auto_book_has_data(prospective)
    if live_has_data:
        parts.append(
            '<p class="note">Each scan creates separate ranked paper entries. '
            'They remain separate from the manual paper-trade ledger. The scan '
            'quote is provenance only: a prospective signal fills at the first '
            'valid recorded pool-side price within two hours after ranking, or '
            'expires as a missed fill. Its 24-hour target starts at the fill; the '
            'exit is the first recorded quote from that target through +6h, then '
            'is censored if absent. Forward-mark tooltips show their actual '
            'observation time and delay. P&amp;L uses last-recorded marks and '
            'labels stale coverage.'
            '</p></div>'
        )
        strategy_ids = prospective.get("strategy_ids") or []
        combined = len(strategy_ids) > 1
        parts.append(_auto_book_dashboard(
            prospective,
            "Combined live prospective book" if combined else "Live prospective book",
            f"{len(strategy_ids)} versions" if combined else "live only",
            historical=False,
        ))
    else:
        attempted = int(_auto_float(capture.get("attempted_scans")) or 0)
        waiting_title = (
            "No public scan has passed the strategy gate yet."
            if attempted else
            "Waiting for the first public scan."
        )
        parts.append(
            f'<div class="auto-waiting"><b>{esc(waiting_title)}</b><br>'
            '<span class="note">Only a complete, priceable Top-10 scan creates '
            'live entries; gated attempts remain visible above and historical '
            'rows are never inserted into live KPIs.</span>'
            '</div></div>'
        )

    # During rollout the live book is intentionally empty. Keep the historical
    # preview visible even if its own source is temporarily empty, so readers
    # never mistake a missing card for live/historical metric blending.
    if _auto_book_has_data(historical) or not live_has_data:
        parts.append(_auto_book_dashboard(
            historical,
            f"Historical score-v{version} preview",
            "retrospective",
            historical=True,
        ))
    parts.append("</section>")
    return "".join(parts)


def build(
    conn,
    ledger_path=paper_trades.LEDGER_PATH,
    now=None,
    picks_dir=auto_paper.PICKS_DIR,
):
    now = now or datetime.now(timezone.utc)
    latest = db.latest_rows(conn)
    portfolio = paper_trades.build_portfolio(conn, ledger_path=ledger_path, now=now)
    automatic_strategy = auto_paper.build_strategy(
        conn, picks_dir=picks_dir, now=now
    )
    n_tokens = conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
    n_scans = conn.execute("SELECT COUNT(DISTINCT ts) FROM snapshots").fetchone()[0]
    first_ts, last_ts = conn.execute("SELECT MIN(ts), MAX(ts) FROM snapshots").fetchone()
    new24 = [r for r in latest if r["pool_created_at"] and
             (now - parse_ts(r["pool_created_at"])).total_seconds() < 86400]
    span_days = (parse_ts(last_ts) - parse_ts(first_ts)).total_seconds() / 86400 if first_ts else 0

    tiles = [
        ("Pools tracked", f"{len(latest):,}", ""),
        ("Tokens", f"{n_tokens:,}", ""),
        ("Scans", f"{n_scans:,}", f"over {span_days:.1f} days"),
        ("New pools (24h)", f"{len(new24):,}", "by creation time"),
        ("Total 24h volume", fmt_usd(sum(r["vol_h24_usd"] or 0 for r in latest
                                         if (r["liquidity_usd"] or 0) >= 100)),
         "pools with real liquidity"),
        ("Median liquidity", fmt_usd(scoring.median(
            [r["liquidity_usd"] or 0 for r in latest])), "per pool"),
    ]
    kpis = "".join(f'<div class="tile"><div class="label">{esc(l)}</div>'
                   f'<div class="value">{v}</div>'
                   + (f'<div class="sub">{esc(s)}</div>' if s else "")
                   + "</div>" for l, v, s in tiles)

    launch_cols = [
        ("Pool", lambda r: esc((r["name"] or "?")[:28]), ""),
        ("Created", lambda r: esc((r["pool_created_at"] or "?")[5:16].replace("T", " ")), ""),
        ("Liquidity", lambda r: esc(fmt_usd(r["liquidity_usd"])), "num"),
        ("Vol 24h", lambda r: esc(fmt_usd(r["vol_h24_usd"])), "num"),
        ("Buys/Sells", lambda r: esc(f'{int(r["buys_h24"] or 0)}/{int(r["sells_h24"] or 0)}'), "num"),
        ("Δ 24h", lambda r: fmt_pct(r["price_change_h24"]), "num"),
    ]
    new24_sorted = sorted(new24, key=lambda r: r["pool_created_at"] or "", reverse=True)[:25]

    return f"""
<title>Robinhood Chain Scanner</title>
<style>{CSS}</style>
<div class="wrap">
  <header>
    <h1>Robinhood Chain Scanner</h1>
    <div class="meta">Last scan {esc(last_ts)} · collecting since {esc(first_ts)} ·
      {n_scans} scans · data: GeckoTerminal, hourly via GitHub Actions</div>
  </header>
  <div class="dashboard-tabs" role="tablist" aria-label="Dashboard views"
       data-dashboard-tabs hidden>
    <button class="dashboard-tab" id="scanner-tab" type="button" role="tab"
            aria-selected="true" aria-controls="scanner-panel" tabindex="0">
      Market scanner
    </button>
    <button class="dashboard-tab" id="paper-tab" type="button" role="tab"
            aria-selected="false" aria-controls="paper-panel" tabindex="-1">
      Paper trades
    </button>
  </div>
  <section class="dashboard-panel" id="scanner-panel" role="tabpanel"
           aria-labelledby="scanner-tab" tabindex="0">
    <div class="kpis">{kpis}</div>
    {pulse_section(conn)}
    {collection_section(conn)}
    {picks_section(latest, now)}
    {rug_section(conn, latest, now)}
    <div class="cols">
      <div class="card"><h2>Top pools by 24h volume</h2>
        <p class="sub">Pools with ≥ $1k liquidity · hover for detail</p>{bar_chart(latest)}</div>
      <div class="card"><h2>Liquidity vs 24h volume</h2>
        <p class="sub">Each dot is a pool, log scales · pools far above the diagonal
          churn hard relative to their depth</p>{scatter(latest)}</div>
    </div>
    {surges_section(conn)}
    <div class="card"><h2>Newest pools (created in the last 24h)</h2>
      <p class="sub">{len(new24)} pools created · showing the latest {len(new24_sorted)}</p>
      {table_html(new24_sorted, launch_cols)}</div>
  </section>
  <section class="dashboard-panel" id="paper-panel" role="tabpanel"
           aria-labelledby="paper-tab" tabindex="0">
    {paper_section(portfolio)}
    {auto_strategy_section(automatic_strategy)}
  </section>
  <footer>Generated {esc(now.strftime("%Y-%m-%dT%H:%M:%SZ"))} · report_html.py ·
    Stage 2 research with paper-only tracking — no real trading</footer>
</div>
<div id="tip"></div>
<script>{JS}</script>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="report.html")
    ap.add_argument("--paper-ledger", default=str(paper_trades.LEDGER_PATH),
                    help="paper-trade JSONL path (default: data/paper_trades.jsonl)")
    ap.add_argument("--auto-picks", default=str(auto_paper.PICKS_DIR),
                    help="automatic-strategy pick-log directory")
    ap.add_argument("--fragment", action="store_true",
                    help="emit body content only (no <!doctype>/<html> wrapper)")
    args = ap.parse_args()

    if not os.path.exists(db.DB_PATH):
        raise SystemExit("no data/scanner.db — run scanner.py or build_db.py first")
    conn = db.connect()
    try:
        inner = build(
            conn,
            ledger_path=args.paper_ledger,
            picks_dir=args.auto_picks,
        )
    finally:
        conn.close()

    if args.fragment:
        doc = inner
    else:
        doc = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
               '<meta name="viewport" content="width=device-width, initial-scale=1">'
               '<link rel="icon" href="data:,">'
               f'</head><body>{inner}</body></html>')
    # atomic + explicit encoding: never truncate the old report before the
    # new one is fully written, and never depend on the platform default
    # encoding (Windows cp1252 chokes on the symbols in this page)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(doc)
    os.replace(tmp, args.out)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
