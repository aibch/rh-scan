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

import chain_pulse
import db
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
  --up: #006300; --down: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root {
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --baseline: #383835;
    --border: rgba(255,255,255,0.10); --accent: #3987e5; --accent-soft: #104281;
    --up: #0ca30c; --down: #e66767;
  }
}
:root[data-theme="dark"] {
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
  --muted: #898781; --grid: #2c2c2a; --baseline: #383835;
  --border: rgba(255,255,255,0.10); --accent: #3987e5; --accent-soft: #104281;
  --up: #0ca30c; --down: #e66767;
}
:root[data-theme="light"] {
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e;
  --muted: #898781; --grid: #e1e0d9; --baseline: #c3c2b7;
  --border: rgba(11,11,11,0.10); --accent: #2a78d6; --accent-soft: #cde2fb;
  --up: #006300; --down: #d03b3b;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 1080px; margin: 0 auto; padding: 32px 20px 56px; }
header h1 { font-size: 22px; font-weight: 650; margin: 0 0 4px; }
header .meta { color: var(--ink-2); font-size: 13.5px; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: 12px; margin: 24px 0; }
.tile { background: var(--surface); border: 1px solid var(--border);
        border-radius: 8px; padding: 14px 16px; }
.tile .label { font-size: 12.5px; color: var(--ink-2); }
.tile .value { font-size: 26px; font-weight: 600; margin-top: 2px; }
.tile .sub { font-size: 12px; color: var(--muted); margin-top: 2px; }
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
.hoverable { cursor: default; }
#tip { position: fixed; pointer-events: none; background: var(--surface);
       color: var(--ink); border: 1px solid var(--border); border-radius: 6px;
       padding: 7px 10px; font-size: 12.5px; line-height: 1.45;
       box-shadow: 0 4px 14px rgba(0,0,0,0.18); display: none; z-index: 10;
       max-width: 280px; white-space: pre-line; }
footer { color: var(--muted); font-size: 12px; margin-top: 8px; }
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


def build(conn):
    now = datetime.now(timezone.utc)
    latest = db.latest_rows(conn)
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
  <footer>Generated {esc(now.strftime("%Y-%m-%dT%H:%M:%SZ"))} · report_html.py ·
    stage 1 of docs/strategy-plan.md — data collection only, no trading</footer>
</div>
<div id="tip"></div>
<script>{JS}</script>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="report.html")
    ap.add_argument("--fragment", action="store_true",
                    help="emit body content only (no <!doctype>/<html> wrapper)")
    args = ap.parse_args()

    if not os.path.exists(db.DB_PATH):
        raise SystemExit("no data/scanner.db — run scanner.py or build_db.py first")
    conn = db.connect()
    try:
        inner = build(conn)
    finally:
        conn.close()

    if args.fragment:
        doc = inner
    else:
        doc = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
               '<meta name="viewport" content="width=device-width, initial-scale=1">'
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
