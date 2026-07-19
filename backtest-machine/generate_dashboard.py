"""
Dashboard generator - The Backtest Machine
==========================================
Reads all four SQLite state DBs and renders a self-contained HTML dashboard
(inline SVG charts, no external assets) suitable for GitHub Pages.

    python generate_dashboard.py          # writes site/index.html

CI runs this after every trading cycle and pushes site/ to the gh-pages
branch, so the page updates every ~5 minutes during market hours.
"""

import html
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
OUT = HERE / "site" / "index.html"

BOOKS = [  # (label, db file, trades table, positions table, pnl col config)
    ("Stocks", "intraday_trades.db", "closed_trades", "positions"),
    ("Simple", "simple_trades.db", "trades", "positions"),
    ("Options", "options_trades.db", "options_trades", "options_positions"),
]
CAPITAL = 100_000


def q(dbfile, sql, args=()):
    p = HERE / dbfile
    if not p.exists():
        return []
    try:
        conn = sqlite3.connect(p)
        rows = conn.execute(sql, args).fetchall()
        conn.close()
        return rows
    except sqlite3.Error:
        return []


def esc(s):
    return html.escape(str(s))


# ------------------------------------------------------------------ data ---
def collect():
    daily = {}          # date -> pnl across books
    trades = []         # (exit_time, book, label, pnl, reason)
    books = []          # per-book summary
    positions = []      # (book, label, qty, entry)

    for label, dbfile, ttable, ptable in BOOKS:
        cash_row = q(dbfile, "SELECT value FROM meta WHERE key='cash'")
        cash = float(cash_row[0][0]) if cash_row else CAPITAL

        if label == "Options":
            rows = q(dbfile, f"SELECT exit_time, symbol || ' ' || option_type || ' ' "
                             f"|| CAST(strike AS INT), pnl, reason FROM {ttable}")
            pos = q(dbfile, f"SELECT symbol || ' ' || option_type || ' ' "
                            f"|| CAST(strike AS INT), qty, entry_price FROM {ptable}")
        else:
            exit_col = "exit_px" if label == "Stocks" else "exit"
            rows = q(dbfile, f"SELECT exit_time, symbol, pnl, reason FROM {ttable}")
            pos = q(dbfile, f"SELECT symbol, qty, entry FROM {ptable}")

        total = 0.0
        wins = 0
        for et, sym, pnl, reason in rows:
            pnl = pnl or 0.0
            total += pnl
            wins += 1 if pnl > 0 else 0
            d = (et or "")[:10]
            daily[d] = daily.get(d, 0.0) + pnl
            trades.append((et or "", label, sym, pnl, reason or ""))
        books.append({"label": label, "cash": cash, "trades": len(rows),
                      "wins": wins, "pnl": total})
        for prow in pos:
            positions.append((label, *prow))

    trades.sort(key=lambda t: t[0], reverse=True)

    calls = q("market_memory.db",
              "SELECT date, decision, confidence, regime, result, "
              "day_change_pct, call_correct FROM daily_memory "
              "ORDER BY date DESC LIMIT 12")
    acc = q("market_memory.db",
            "SELECT COUNT(*), COALESCE(SUM(call_correct),0) FROM daily_memory "
            "WHERE call_correct IS NOT NULL AND decision != 'NO TRADE'")
    n_calls, n_correct = (acc[0] if acc else (0, 0))
    return daily, trades, books, positions, calls, n_calls, n_correct


# ------------------------------------------------------------------ charts ---
# validated reference palette (light, dark) — see dataviz reference instance
POS = ("#2a78d6", "#3987e5")   # diverging cool pole = profit
NEG = ("#e34948", "#e66767")   # diverging warm pole = loss

CHART_W, CHART_H, PAD_L, PAD_B, PAD_T = 760, 230, 56, 26, 12


def nice_ticks(lo, hi, n=4):
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    span = hi - lo
    step = 10 ** len(str(int(span / n)))
    for s in (step / 10, step / 5, step / 2, step):
        if span / s <= n + 1:
            step = s
            break
    t0 = int(lo // step) * step
    return [t0 + i * step for i in range(int(span / step) + 3)
            if lo - step <= t0 + i * step <= hi + step]


def svg_axis(y_of, ticks, fmt=lambda v: f"{v:,.0f}"):
    parts = []
    for t in ticks:
        y = y_of(t)
        parts.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{CHART_W-8}" y2="{y:.1f}" '
                     f'class="grid"/>')
        parts.append(f'<text x="{PAD_L-8}" y="{y+4:.1f}" class="tick" '
                     f'text-anchor="end">{fmt(t)}</text>')
    return "".join(parts)


def daily_bar_chart(daily):
    days = sorted(daily)[-30:]
    if not days:
        return '<p class="empty">No closed trades yet.</p>'
    vals = [daily[d] for d in days]
    lo, hi = min(min(vals), 0), max(max(vals), 0)
    ticks = nice_ticks(lo, hi)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])
    span = (hi - lo) or 1
    plot_h = CHART_H - PAD_B - PAD_T

    def y_of(v):
        return PAD_T + (hi - v) / span * plot_h

    y0 = y_of(0)
    n = len(days)
    slot = (CHART_W - PAD_L - 12) / n
    bw = max(4, min(26, slot - 2))          # 2px surface gap between bars
    out = [svg_axis(y_of, ticks)]
    out.append(f'<line x1="{PAD_L}" y1="{y0:.1f}" x2="{CHART_W-8}" y2="{y0:.1f}" '
               f'class="baseline"/>')
    for i, d in enumerate(days):
        v = daily[d]
        x = PAD_L + 6 + i * slot + (slot - bw) / 2
        yt, yb = (y_of(v), y0) if v >= 0 else (y0, y_of(v))
        h = max(abs(yb - yt), 1.5)
        cls = "pos" if v >= 0 else "neg"
        # 4px rounded data-end anchored at the baseline via clip
        out.append(
            f'<rect class="bar {cls}" x="{x:.1f}" y="{yt:.1f}" width="{bw:.1f}" '
            f'height="{h:.1f}" rx="3" data-tip="{d}: Rs.{v:,.0f}"/>')
        if n <= 12 or i % max(1, n // 8) == 0:
            out.append(f'<text x="{x+bw/2:.1f}" y="{CHART_H-8}" class="tick" '
                       f'text-anchor="middle">{d[5:]}</text>')
    return (f'<svg viewBox="0 0 {CHART_W} {CHART_H}" role="img" '
            f'aria-label="Daily profit and loss, last {n} trading days">'
            + "".join(out) + "</svg>")


def cumulative_line_chart(daily):
    days = sorted(daily)
    if not days:
        return '<p class="empty">No closed trades yet.</p>'
    cum, running = [], 0.0
    for d in days:
        running += daily[d]
        cum.append(running)
    lo, hi = min(min(cum), 0), max(max(cum), 0)
    ticks = nice_ticks(lo, hi)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])
    span = (hi - lo) or 1
    plot_h = CHART_H - PAD_B - PAD_T

    def y_of(v):
        return PAD_T + (hi - v) / span * plot_h

    n = len(days)
    def x_of(i):
        return PAD_L + 10 + (i * (CHART_W - PAD_L - 24) / max(n - 1, 1))

    pts = " ".join(f"{x_of(i):.1f},{y_of(v):.1f}" for i, v in enumerate(cum))
    out = [svg_axis(y_of, ticks),
           f'<line x1="{PAD_L}" y1="{y_of(0):.1f}" x2="{CHART_W-8}" '
           f'y2="{y_of(0):.1f}" class="baseline"/>',
           f'<polyline points="{pts}" class="line"/>']
    for i, v in enumerate(cum):                 # hover targets, ring on hover
        out.append(f'<circle class="pt" cx="{x_of(i):.1f}" cy="{y_of(v):.1f}" '
                   f'r="9" data-tip="{days[i]}: Rs.{v:,.0f} cumulative"/>')
    step = max(1, n // 8)
    for i in range(0, n, step):
        out.append(f'<text x="{x_of(i):.1f}" y="{CHART_H-8}" class="tick" '
                   f'text-anchor="middle">{days[i][5:]}</text>')
    return (f'<svg viewBox="0 0 {CHART_W} {CHART_H}" role="img" '
            f'aria-label="Cumulative paper P&L">' + "".join(out) + "</svg>")


# ------------------------------------------------------------------ html ---
def render():
    daily, trades, books, positions, calls, n_calls, n_correct = collect()
    today = date.today().isoformat()
    total_pnl = sum(b["pnl"] for b in books)
    today_pnl = daily.get(today, 0.0)
    n_trades = sum(b["trades"] for b in books)
    n_wins = sum(b["wins"] for b in books)
    win_rate = n_wins / n_trades * 100 if n_trades else 0
    acc = n_correct / n_calls * 100 if n_calls else 0

    def delta(v):
        cls = "up" if v >= 0 else "down"
        arrow = "▲" if v >= 0 else "▼"
        return f'<span class="delta {cls}">{arrow} Rs.{abs(v):,.0f}</span>'

    tiles = f"""
    <div class="tiles">
      <div class="tile"><div class="k">All-time P&amp;L (paper)</div>
        <div class="v">Rs.{total_pnl:,.0f}</div>{delta(total_pnl)}</div>
      <div class="tile"><div class="k">Today's realized P&amp;L</div>
        <div class="v">Rs.{today_pnl:,.0f}</div>{delta(today_pnl)}</div>
      <div class="tile"><div class="k">Trades / win rate</div>
        <div class="v">{n_trades} <span class="sub">/ {win_rate:.0f}%</span></div></div>
      <div class="tile"><div class="k">AI call accuracy</div>
        <div class="v">{acc:.0f}% <span class="sub">({n_calls} calls)</span></div></div>
    </div>"""

    book_rows = "".join(
        f"<tr><td>{esc(b['label'])}</td><td class='num'>Rs.{b['cash']:,.0f}</td>"
        f"<td class='num'>{b['trades']}</td>"
        f"<td class='num {'up' if b['pnl']>=0 else 'down'}'>Rs.{b['pnl']:,.0f}</td></tr>"
        for b in books)

    pos_rows = "".join(
        f"<tr><td>{esc(bk)}</td><td>{esc(sym).replace('.NS','')}</td>"
        f"<td class='num'>{qty}</td><td class='num'>{entry:,.2f}</td></tr>"
        for bk, sym, qty, entry in positions) or \
        '<tr><td colspan="4" class="empty">No open positions</td></tr>'

    trade_rows = "".join(
        f"<tr><td>{esc(et[:16]).replace('T',' ')}</td><td>{esc(bk)}</td>"
        f"<td>{esc(sym).replace('.NS','')}</td>"
        f"<td class='num {'up' if pnl>=0 else 'down'}'>Rs.{pnl:,.0f}</td>"
        f"<td>{esc(reason)}</td></tr>"
        for et, bk, sym, pnl, reason in trades[:15]) or \
        '<tr><td colspan="5" class="empty">No trades yet</td></tr>'

    def verdict(c):
        return {1: "CORRECT", 0: "WRONG"}.get(c, "-")

    call_rows = "".join(
        f"<tr><td>{esc(d)}</td><td>{esc(dec or '-')}</td>"
        f"<td class='num'>{conf or 0:.0f}%</td><td>{esc(reg or '-')}</td>"
        f"<td>{esc(res or 'pending')}</td>"
        f"<td class='num'>{'' if chg is None else f'{chg:+.2f}%'}</td>"
        f"<td class='{'up' if cc==1 else 'down' if cc==0 else ''}'>{verdict(cc)}</td></tr>"
        for d, dec, conf, reg, res, chg, cc in calls) or \
        '<tr><td colspan="7" class="empty">No AI calls recorded yet</td></tr>'

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M IST")

    page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Intraday Paper Trading Dashboard</title>
<style>
:root {{
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink2: #52514e;
  --muted: #898781; --grid: #e1e0d9; --base: #c3c2b7;
  --pos: {POS[0]}; --neg: {NEG[0]};
  --up: #006300; --down: #d03b3b;
  --border: rgba(11,11,11,0.10);
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink2: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --base: #383835;
    --pos: {POS[1]}; --neg: {NEG[1]};
    --up: #0ca30c; --down: #e66767;
    --border: rgba(255,255,255,0.10);
  }}
}}
* {{ box-sizing: border-box; margin: 0; }}
body {{ background: var(--page); color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; padding: 20px; }}
.wrap {{ max-width: 860px; margin: 0 auto; }}
h1 {{ font-size: 20px; }} h2 {{ font-size: 15px; color: var(--ink2); margin: 0 0 8px; }}
.stamp {{ color: var(--muted); font-size: 13px; margin-bottom: 18px; }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px,1fr));
  gap: 12px; margin-bottom: 18px; }}
.tile, .card {{ background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 16px; }}
.card {{ margin-bottom: 18px; overflow-x: auto; }}
.tile .k {{ font-size: 12.5px; color: var(--ink2); }}
.tile .v {{ font-size: 26px; font-weight: 650; margin: 2px 0; }}
.tile .sub {{ font-size: 14px; color: var(--muted); font-weight: 400; }}
.delta {{ font-size: 13px; }} .up {{ color: var(--up); }} .down {{ color: var(--down); }}
svg {{ width: 100%; height: auto; display: block; }}
.grid {{ stroke: var(--grid); stroke-width: 1; }}
.baseline {{ stroke: var(--base); stroke-width: 1; }}
.tick {{ fill: var(--muted); font-size: 11px; }}
.bar.pos {{ fill: var(--pos); }} .bar.neg {{ fill: var(--neg); }}
.bar:hover, .pt:hover {{ opacity: 0.75; cursor: default; }}
.line {{ fill: none; stroke: var(--pos); stroke-width: 2; stroke-linejoin: round; }}
.pt {{ fill: transparent; }}
.pt:hover {{ fill: var(--pos); stroke: var(--surface); stroke-width: 2; r: 5; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13.5px; }}
th {{ text-align: left; color: var(--ink2); font-weight: 600;
  border-bottom: 1px solid var(--base); padding: 6px 10px 6px 0; }}
td {{ border-bottom: 1px solid var(--grid); padding: 6px 10px 6px 0; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
th.num {{ text-align: right; }}
.empty {{ color: var(--muted); }}
#tip {{ position: fixed; pointer-events: none; background: var(--ink);
  color: var(--page); padding: 4px 9px; border-radius: 6px; font-size: 12.5px;
  opacity: 0; transition: opacity .08s; z-index: 9; }}
</style></head>
<body><div class="wrap">
<h1>Intraday Paper Trading</h1>
<div class="stamp">Updated {stamp} &middot; paper mode &middot; Rs.{CAPITAL:,.0f} per book</div>
{tiles}
<div class="card"><h2>Daily realized P&amp;L (last 30 days)</h2>{daily_bar_chart(daily)}</div>
<div class="card"><h2>Cumulative P&amp;L</h2>{cumulative_line_chart(daily)}</div>
<div class="card"><h2>Books</h2>
<table><tr><th>Book</th><th class="num">Cash</th><th class="num">Trades</th>
<th class="num">P&amp;L</th></tr>{book_rows}</table></div>
<div class="card"><h2>Open positions</h2>
<table><tr><th>Book</th><th>Symbol</th><th class="num">Qty</th>
<th class="num">Entry</th></tr>{pos_rows}</table></div>
<div class="card"><h2>Recent trades</h2>
<table><tr><th>Time</th><th>Book</th><th>Symbol</th><th class="num">P&amp;L</th>
<th>Reason</th></tr>{trade_rows}</table></div>
<div class="card"><h2>Pre-market AI calls</h2>
<table><tr><th>Date</th><th>Call</th><th class="num">Conf</th><th>Regime</th>
<th>Result</th><th class="num">Day&nbsp;move</th><th>Verdict</th></tr>{call_rows}</table></div>
</div>
<div id="tip"></div>
<script>
const tip = document.getElementById('tip');
document.querySelectorAll('[data-tip]').forEach(el => {{
  el.addEventListener('mousemove', e => {{
    tip.textContent = el.dataset.tip;
    tip.style.left = (e.clientX + 12) + 'px';
    tip.style.top = (e.clientY - 28) + 'px';
    tip.style.opacity = 1;
  }});
  el.addEventListener('mouseleave', () => tip.style.opacity = 0);
}});
</script>
</body></html>"""

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(page, encoding="utf-8")
    print(f"Dashboard written to {OUT} "
          f"({n_trades} trades, {len(calls)} AI calls, {len(positions)} open)")


if __name__ == "__main__":
    render()
