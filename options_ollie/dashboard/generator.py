"""
Options Ollie — HTML Dashboard Generator
Creates a self-contained interactive HTML dashboard with all trade data.
"""

import json
from datetime import datetime
from typing import Dict, List
import pandas as pd


class DashboardGenerator:
    """Generates a self-contained HTML dashboard for options analysis."""

    def generate(self, data: Dict, output_path: str = 'dashboard.html') -> str:
        """
        Generate the full dashboard HTML.

        data should contain:
        - rddt_recommendation: Dict from WheelManager.recommend_rddt_action()
        - wheel_candidates: DataFrame.to_dict('records') from screener
        - iron_condors: DataFrame.to_dict('records') from screener
        - credit_spreads: DataFrame.to_dict('records') from screener
        - portfolio: Dict from portfolio state
        - wheel_status: Dict from WheelManager.get_wheel_status()
        """
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        rddt = data.get('rddt_recommendation', {})
        wheel = data.get('wheel_candidates', [])
        condors = data.get('iron_condors', [])
        spreads = data.get('credit_spreads', [])
        portfolio = data.get('portfolio', {})
        monitor_report = data.get('monitor_report', {})

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Options Ollie — Dashboard</title>
<style>
:root {{
    --bg: #0f1117;
    --card: #1a1d27;
    --border: #2a2d3a;
    --text: #e2e8f0;
    --muted: #8b95a5;
    --green: #22c55e;
    --red: #ef4444;
    --blue: #3b82f6;
    --orange: #f59e0b;
    --purple: #a855f7;
    --accent: #6366f1;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    line-height: 1.6;
    padding: 20px;
}}
.header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 20px 0;
    border-bottom: 1px solid var(--border);
    margin-bottom: 24px;
}}
.header h1 {{
    font-size: 28px;
    background: linear-gradient(135deg, var(--accent), var(--purple));
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}}
.header .timestamp {{ color: var(--muted); font-size: 14px; }}
.grid {{ display: grid; gap: 20px; margin-bottom: 24px; }}
.grid-2 {{ grid-template-columns: 1fr 1fr; }}
.grid-3 {{ grid-template-columns: 1fr 1fr 1fr; }}
.grid-4 {{ grid-template-columns: 1fr 1fr 1fr 1fr; }}
@media (max-width: 768px) {{
    .grid-2, .grid-3, .grid-4 {{ grid-template-columns: 1fr; }}
}}
.card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
}}
.card h2 {{
    font-size: 16px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 12px;
}}
.card h3 {{
    font-size: 14px;
    color: var(--muted);
    margin-bottom: 8px;
}}
.metric {{
    font-size: 32px;
    font-weight: 700;
}}
.metric-sm {{
    font-size: 20px;
    font-weight: 600;
}}
.positive {{ color: var(--green); }}
.negative {{ color: var(--red); }}
.neutral {{ color: var(--blue); }}
.warning {{ color: var(--orange); }}
.badge {{
    display: inline-block;
    padding: 2px 10px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 600;
}}
.badge-green {{ background: rgba(34,197,94,0.15); color: var(--green); }}
.badge-blue {{ background: rgba(59,130,246,0.15); color: var(--blue); }}
.badge-orange {{ background: rgba(245,158,11,0.15); color: var(--orange); }}
.badge-purple {{ background: rgba(168,85,247,0.15); color: var(--purple); }}
.badge-red {{ background: rgba(239,68,68,0.15); color: var(--red); }}

/* RDDT Section */
.rddt-box {{
    background: linear-gradient(135deg, rgba(99,102,241,0.1), rgba(168,85,247,0.1));
    border: 1px solid var(--accent);
}}
.rddt-action {{
    background: rgba(34,197,94,0.1);
    border: 1px solid var(--green);
    border-radius: 8px;
    padding: 16px;
    margin: 12px 0;
    font-size: 15px;
}}

/* Tables */
table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
}}
th {{
    text-align: left;
    padding: 10px 12px;
    background: rgba(255,255,255,0.03);
    color: var(--muted);
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
}}
td {{
    padding: 10px 12px;
    border-bottom: 1px solid rgba(255,255,255,0.04);
}}
tr:hover td {{
    background: rgba(255,255,255,0.02);
}}
.table-wrap {{
    max-height: 500px;
    overflow-y: auto;
    border-radius: 8px;
}}

/* Tabs */
.tab-container {{ margin-bottom: 24px; }}
.tab-buttons {{
    display: flex;
    gap: 4px;
    margin-bottom: 16px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 0;
}}
.tab-btn {{
    padding: 10px 20px;
    background: transparent;
    color: var(--muted);
    border: none;
    cursor: pointer;
    font-size: 14px;
    font-weight: 500;
    border-bottom: 2px solid transparent;
    transition: all 0.2s;
}}
.tab-btn:hover {{ color: var(--text); }}
.tab-btn.active {{
    color: var(--accent);
    border-bottom-color: var(--accent);
}}
.tab-content {{ display: none; }}
.tab-content.active {{ display: block; }}

/* Notes */
.note {{
    background: rgba(245,158,11,0.08);
    border-left: 3px solid var(--orange);
    padding: 12px 16px;
    margin: 8px 0;
    border-radius: 0 8px 8px 0;
    font-size: 13px;
}}

/* Position Monitor Cards */
.pos-card {{
    background: var(--card);
    border-radius: 12px;
    padding: 20px;
    border-left: 4px solid var(--border);
    margin-bottom: 12px;
}}
.pos-card.urgent {{ border-left-color: var(--red); background: rgba(239,68,68,0.04); }}
.pos-card.action {{ border-left-color: var(--green); background: rgba(34,197,94,0.04); }}
.pos-card.watch  {{ border-left-color: var(--orange); background: rgba(245,158,11,0.04); }}
.pos-card.hold   {{ border-left-color: var(--border); }}
.pos-header {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px; }}
.pos-meta {{ color: var(--muted); font-size: 13px; margin-bottom: 8px; }}
.pos-stats {{ display: flex; gap: 24px; margin: 12px 0; flex-wrap: wrap; }}
.pos-stat {{ text-align: center; }}
.pos-stat .label {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }}
.pos-stat .value {{ font-size: 18px; font-weight: 600; margin-top: 2px; }}
.advice-headline {{ font-size: 15px; font-weight: 600; margin: 12px 0 6px; }}
.advice-detail {{ font-size: 13px; color: var(--muted); line-height: 1.6; margin-bottom: 10px; }}
.action-list {{ list-style: none; padding: 0; margin: 0; }}
.action-list li {{ font-size: 13px; padding: 4px 0; color: var(--text); }}
.action-list li::before {{ content: "• "; color: var(--accent); font-weight: bold; }}
.monitor-summary {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }}
.monitor-pill {{
    padding: 6px 14px; border-radius: 20px; font-size: 13px; font-weight: 600;
    display: flex; align-items: center; gap: 6px;
}}

/* Sort indicators */
th.sortable {{ cursor: pointer; user-select: none; }}
th.sortable:hover {{ color: var(--accent); }}
th.sort-asc::after {{ content: ' ▲'; font-size: 10px; }}
th.sort-desc::after {{ content: ' ▼'; font-size: 10px; }}

/* Filter */
.filter-bar {{
    display: flex;
    gap: 12px;
    margin-bottom: 16px;
    flex-wrap: wrap;
}}
.filter-bar input, .filter-bar select {{
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 13px;
}}
.filter-bar input:focus, .filter-bar select:focus {{
    outline: none;
    border-color: var(--accent);
}}
</style>
</head>
<body>

<div class="header">
    <h1>Options Ollie</h1>
    <div class="timestamp">Last updated: {now}</div>
</div>

{self._render_monitor_section(monitor_report)}

<!-- RDDT Section -->
<div class="card rddt-box" style="margin-bottom: 24px;">
    <h2>🎯 RDDT Wheel Strategy — Your Active Position</h2>
    <div class="grid grid-4" style="margin: 16px 0;">
        <div>
            <h3>Current Price</h3>
            <div class="metric-sm">${rddt.get('current_price', '—')}</div>
        </div>
        <div>
            <h3>Shares Held</h3>
            <div class="metric-sm">{rddt.get('shares_held', 200)}</div>
        </div>
        <div>
            <h3>IV Rank</h3>
            <div class="metric-sm {'positive' if rddt.get('iv_rank', 0) > 40 else 'warning'}">{rddt.get('iv_rank', '—')}%</div>
        </div>
        <div>
            <h3>Contracts Available</h3>
            <div class="metric-sm">{rddt.get('contracts_available', 2)}</div>
        </div>
    </div>
    <div class="rddt-action">
        <strong>Recommended Action:</strong> {rddt.get('action', 'Loading...')}
    </div>
    <p style="color: var(--muted); margin-bottom: 12px;">{rddt.get('reasoning', '')}</p>

    {''.join(f'<div class="note">{n}</div>' for n in rddt.get('risk_notes', []))}

    {self._render_cc_table(rddt.get('top_covered_calls', []))}
</div>

<!-- Strategy Tabs -->
<div class="tab-container">
    <div class="tab-buttons">
        <button class="tab-btn active" onclick="switchTab(event, 'wheel')">🎡 Wheel Candidates</button>
        <button class="tab-btn" onclick="switchTab(event, 'condors')">🦅 Iron Condors</button>
        <button class="tab-btn" onclick="switchTab(event, 'spreads')">📐 Credit Spreads</button>
    </div>

    <div id="wheel" class="tab-content active">
        <div class="card">
            <h2>Cash-Secured Put Candidates — Ranked by Composite Score</h2>
            <div class="filter-bar">
                <input type="text" id="wheel-search" placeholder="Filter by symbol..." oninput="filterTable('wheel-table', this.value)">
                <select onchange="filterByColumn('wheel-table', 8, this.value)">
                    <option value="">All IV Ranks</option>
                    <option value="50">IV Rank > 50%</option>
                    <option value="30">IV Rank > 30%</option>
                </select>
            </div>
            <div class="table-wrap">
                {self._render_wheel_table(wheel)}
            </div>
        </div>
    </div>

    <div id="condors" class="tab-content">
        <div class="card">
            <h2>Iron Condor Opportunities — High IV, Range-Bound</h2>
            <div class="filter-bar">
                <input type="text" placeholder="Filter by symbol..." oninput="filterTable('condor-table', this.value)">
            </div>
            <div class="table-wrap">
                {self._render_condor_table(condors)}
            </div>
        </div>
    </div>

    <div id="spreads" class="tab-content">
        <div class="card">
            <h2>Credit Spread Opportunities — Bull Put Spreads</h2>
            <div class="filter-bar">
                <input type="text" placeholder="Filter by symbol..." oninput="filterTable('spread-table', this.value)">
            </div>
            <div class="table-wrap">
                {self._render_spread_table(spreads)}
            </div>
        </div>
    </div>
</div>

<!-- Strategy Guide -->
<div class="card" style="margin-top: 24px;">
    <h2>📖 Strategy Reference</h2>
    <div class="grid grid-3" style="margin-top: 12px;">
        <div>
            <h3>🎡 The Wheel</h3>
            <p style="font-size: 13px; color: var(--muted);">
                Sell CSPs on stocks you'd own → Get assigned → Sell CCs above cost basis → Get called away → Repeat.
                Target: ~25 delta, 30-45 DTE, close at 50% profit.
            </p>
        </div>
        <div>
            <h3>🦅 Iron Condors</h3>
            <p style="font-size: 13px; color: var(--muted);">
                Sell OTM put spread + OTM call spread. Profits when stock stays in range.
                Best when: high IV rank, expected low movement, 30-45 DTE.
            </p>
        </div>
        <div>
            <h3>📐 Credit Spreads</h3>
            <p style="font-size: 13px; color: var(--muted);">
                Bull put spreads (bullish bias) or bear call spreads (bearish).
                Defined risk, lower capital requirement than CSPs.
            </p>
        </div>
    </div>
</div>

<div style="text-align: center; color: var(--muted); font-size: 12px; margin-top: 32px; padding: 16px;">
    Options Ollie v0.1 — Data from Yahoo Finance — Not financial advice — Always do your own research
</div>

<script>
function switchTab(e, tabId) {{
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    e.target.classList.add('active');
    document.getElementById(tabId).classList.add('active');
}}

function filterTable(tableId, query) {{
    const table = document.getElementById(tableId);
    if (!table) return;
    const rows = table.querySelectorAll('tbody tr');
    query = query.toLowerCase();
    rows.forEach(row => {{
        const text = row.cells[0]?.textContent.toLowerCase() || '';
        row.style.display = text.includes(query) ? '' : 'none';
    }});
}}

function filterByColumn(tableId, colIdx, minVal) {{
    const table = document.getElementById(tableId);
    if (!table) return;
    const rows = table.querySelectorAll('tbody tr');
    rows.forEach(row => {{
        if (!minVal) {{ row.style.display = ''; return; }}
        const val = parseFloat(row.cells[colIdx]?.textContent) || 0;
        row.style.display = val >= parseFloat(minVal) ? '' : 'none';
    }});
}}

// Sortable columns
document.querySelectorAll('th.sortable').forEach(th => {{
    th.addEventListener('click', function() {{
        const table = this.closest('table');
        const tbody = table.querySelector('tbody');
        const rows = Array.from(tbody.querySelectorAll('tr'));
        const idx = Array.from(this.parentNode.children).indexOf(this);
        const isNum = this.dataset.type === 'num';

        // Toggle direction
        const asc = !this.classList.contains('sort-asc');
        table.querySelectorAll('th').forEach(h => h.classList.remove('sort-asc', 'sort-desc'));
        this.classList.add(asc ? 'sort-asc' : 'sort-desc');

        rows.sort((a, b) => {{
            let va = a.cells[idx]?.textContent.replace(/[$,%]/g, '').trim() || '';
            let vb = b.cells[idx]?.textContent.replace(/[$,%]/g, '').trim() || '';
            if (isNum) {{ va = parseFloat(va) || 0; vb = parseFloat(vb) || 0; }}
            if (va < vb) return asc ? -1 : 1;
            if (va > vb) return asc ? 1 : -1;
            return 0;
        }});
        rows.forEach(r => tbody.appendChild(r));
    }});
}});
</script>
</body>
</html>"""

        with open(output_path, 'w') as f:
            f.write(html)

        return output_path

    def _render_monitor_section(self, report: dict) -> str:
        """Render the open positions monitor section."""
        if not report or not report.get('positions'):
            return ''

        positions = report.get('positions', [])
        total_pnl = report.get('total_unrealized_pnl', 0)
        pnl_class = 'positive' if total_pnl >= 0 else 'negative'
        pnl_pct = report.get('overall_pct_captured', 0)
        urgent = report.get('urgent_count', 0)
        action = report.get('action_count', 0)
        watch = report.get('watch_count', 0)
        hold = report.get('hold_count', 0)

        pills = ''
        if urgent:
            pills += f'<div class="monitor-pill badge-red">🚨 {urgent} URGENT</div>'
        if action:
            pills += f'<div class="monitor-pill badge-green">✅ {action} ACTION</div>'
        if watch:
            pills += f'<div class="monitor-pill badge-orange">👁 {watch} WATCH</div>'
        if hold:
            pills += f'<div class="monitor-pill badge-blue">✓ {hold} HOLD</div>'

        cards = ''.join(self._render_position_card(p) for p in positions)

        return f"""
<div class="card" style="margin-bottom: 24px; border: 1px solid var(--accent);">
    <h2>📊 Open Positions — Live Management Advice</h2>
    <div class="grid grid-4" style="margin: 16px 0 20px;">
        <div><h3>Positions</h3><div class="metric-sm">{len(positions)}</div></div>
        <div><h3>Premium at Risk</h3><div class="metric-sm">${report.get('total_premium_at_risk', 0):,.0f}</div></div>
        <div><h3>Unrealized P&amp;L</h3><div class="metric-sm {pnl_class}">${total_pnl:+,.0f}</div></div>
        <div><h3>% Captured</h3><div class="metric-sm {'positive' if pnl_pct >= 50 else 'warning' if pnl_pct >= 25 else 'neutral'}">{pnl_pct:.0f}%</div></div>
    </div>
    <div class="monitor-summary">{pills}</div>
    {cards}
    <p style="color: var(--muted); font-size: 12px; margin-top: 12px;">
        Last updated: {report.get('as_of', '—')} — Run <code>--monitor</code> to refresh
    </p>
</div>"""

    def _render_position_card(self, p: dict) -> str:
        """Render a single position card."""
        level = p.get('advice_level', 'HOLD').lower()
        level_icon = {'urgent': '🚨', 'action': '✅', 'watch': '👁', 'hold': '✓'}.get(level, '•')
        pnl = p.get('unrealized_pnl', 0)
        pnl_class = 'positive' if pnl >= 0 else 'negative'
        pct = p.get('pct_max_profit', 0)

        # Format strikes
        if p.get('short_put_strike') and p.get('short_call_strike'):
            strike_str = f"${p['short_put_strike']} / ${p['short_call_strike']}"
        elif p.get('strike'):
            strike_str = f"${p['strike']}"
        else:
            strike_str = '—'

        dte = p.get('dte')
        dte_str = f"{dte}d" if dte is not None else '—'
        dte_class = 'negative' if (dte is not None and dte <= 7) else 'warning' if (dte is not None and dte <= 21) else 'neutral'

        actions_html = ''
        if p.get('advice_actions'):
            items = ''.join(f'<li>{a}</li>' for a in p['advice_actions'])
            actions_html = f'<ul class="action-list" style="margin-top:8px;">{items}</ul>'

        pct_bar_width = max(0, min(100, pct))
        pct_bar_color = '#22c55e' if pct >= 50 else '#f59e0b' if pct >= 25 else '#3b82f6'

        dist_info = ''
        if p.get('pct_to_short_put') is not None:
            color = 'negative' if p['pct_to_short_put'] < 5 else 'warning' if p['pct_to_short_put'] < 10 else 'positive'
            dist_info += f'<span class="{color}">{p["pct_to_short_put"]:.1f}% above put</span>'
        if p.get('pct_to_short_call') is not None:
            color = 'negative' if p['pct_to_short_call'] < 5 else 'warning' if p['pct_to_short_call'] < 10 else 'positive'
            dist_info += f'  |  <span class="{color}">{p["pct_to_short_call"]:.1f}% below call</span>'

        return f"""
<div class="pos-card {level}">
    <div class="pos-header">
        <div>
            <strong style="font-size:16px;">{level_icon} {p.get('symbol','?')} — {p.get('trade_type','?').upper()}</strong>
            <div class="pos-meta">
                [{p.get('trade_id','?')}] &nbsp;|&nbsp; Strike: {strike_str} &nbsp;|&nbsp; Exp: {p.get('expiry','—')} &nbsp;|&nbsp;
                Stock: ${p.get('current_price',0):.2f}
                {f'&nbsp;|&nbsp; {dist_info}' if dist_info else ''}
            </div>
        </div>
        <div style="text-align:right;">
            <div class="metric-sm {pnl_class}">${pnl:+,.0f}</div>
            <div style="font-size:12px; color:var(--muted);">Unrealized P&amp;L</div>
        </div>
    </div>

    <div class="pos-stats">
        <div class="pos-stat">
            <div class="label">Max Premium</div>
            <div class="value">${p.get('premium_received',0):.0f}</div>
        </div>
        <div class="pos-stat">
            <div class="label">% Captured</div>
            <div class="value {'positive' if pct >= 50 else 'warning' if pct >= 25 else 'neutral'}">{pct:.0f}%</div>
        </div>
        <div class="pos-stat">
            <div class="label">DTE</div>
            <div class="value {dte_class}">{dte_str}</div>
        </div>
        <div class="pos-stat">
            <div class="label">Entry Date</div>
            <div class="value" style="font-size:14px;">{p.get('entry_date','—')}</div>
        </div>
    </div>

    <div style="background:rgba(255,255,255,0.04); border-radius:4px; height:6px; margin-bottom:14px;">
        <div style="background:{pct_bar_color}; width:{pct_bar_width}%; height:100%; border-radius:4px; transition:width 0.3s;"></div>
    </div>

    <div class="advice-headline">{p.get('advice_headline','')}</div>
    <div class="advice-detail">{p.get('advice_detail','')}</div>
    {actions_html}
</div>"""

    def _render_cc_table(self, calls: List[Dict]) -> str:
        if not calls:
            return '<p style="color: var(--muted); font-style: italic;">No covered call data available</p>'

        rows = ''
        for c in calls:
            rows += f"""<tr>
                <td>${c.get('strike', '?')}</td>
                <td>{c.get('expiry', '?')}</td>
                <td>{c.get('dte', '?')}d</td>
                <td>${c.get('mid_price', '?'):.2f}</td>
                <td>${c.get('total_premium', '?'):,.0f}</td>
                <td>{c.get('prob_otm', '?'):.0f}%</td>
                <td>{c.get('upside_to_strike_pct', '?'):.1f}%</td>
                <td class="positive">{c.get('annualized_if_called', '?'):.1f}%</td>
            </tr>"""

        return f"""
        <table style="margin-top: 12px;">
            <thead><tr>
                <th>Strike</th><th>Expiry</th><th>DTE</th><th>Mid Price</th>
                <th>Total Premium</th><th>Prob OTM</th><th>Upside</th><th>Ann. Return</th>
            </tr></thead>
            <tbody>{rows}</tbody>
        </table>"""

    def _render_wheel_table(self, candidates: List[Dict]) -> str:
        if not candidates:
            return '<p style="color: var(--muted); padding: 20px;">Screening in progress or no candidates found...</p>'

        rows = ''
        for c in candidates:
            score_class = 'positive' if c.get('score', 0) > 0.6 else 'neutral' if c.get('score', 0) > 0.4 else 'muted'
            rows += f"""<tr>
                <td><strong>{c.get('symbol', '?')}</strong></td>
                <td>${c.get('stock_price', '?')}</td>
                <td>${c.get('strike', '?')}</td>
                <td>{c.get('expiry', '?')}</td>
                <td>{c.get('dte', '?')}d</td>
                <td>${c.get('premium_100', '?'):,.0f}</td>
                <td class="positive">{c.get('annualized_return', '?')}%</td>
                <td>{c.get('prob_otm', '?')}%</td>
                <td>{c.get('iv_rank', '?')}%</td>
                <td>${c.get('capital_required', '?'):,.0f}</td>
                <td class="{score_class}">{c.get('score', '?')}</td>
            </tr>"""

        return f"""
        <table id="wheel-table">
            <thead><tr>
                <th class="sortable">Symbol</th>
                <th class="sortable" data-type="num">Price</th>
                <th class="sortable" data-type="num">Strike</th>
                <th class="sortable">Expiry</th>
                <th class="sortable" data-type="num">DTE</th>
                <th class="sortable" data-type="num">Premium</th>
                <th class="sortable" data-type="num">Ann. Return</th>
                <th class="sortable" data-type="num">Prob OTM</th>
                <th class="sortable" data-type="num">IV Rank</th>
                <th class="sortable" data-type="num">Capital Req</th>
                <th class="sortable" data-type="num">Score</th>
            </tr></thead>
            <tbody>{rows}</tbody>
        </table>"""

    def _render_condor_table(self, condors: List[Dict]) -> str:
        if not condors:
            return '<p style="color: var(--muted); padding: 20px;">No iron condor opportunities found at current IV levels...</p>'

        rows = ''
        for c in condors:
            rows += f"""<tr>
                <td><strong>{c.get('symbol', '?')}</strong></td>
                <td>${c.get('stock_price', '?')}</td>
                <td>{c.get('expiry', '?')}</td>
                <td>{c.get('dte', '?')}d</td>
                <td>${c.get('long_put', '?')}/{c.get('short_put', '?')}</td>
                <td>${c.get('short_call', '?')}/{c.get('long_call', '?')}</td>
                <td class="positive">${c.get('total_credit', '?')}</td>
                <td>${c.get('max_risk', '?'):,.0f}</td>
                <td class="positive">{c.get('return_on_risk_pct', '?')}%</td>
                <td>{c.get('prob_profit_est', '?')}%</td>
                <td>{c.get('iv_rank', '?')}%</td>
            </tr>"""

        return f"""
        <table id="condor-table">
            <thead><tr>
                <th class="sortable">Symbol</th>
                <th class="sortable" data-type="num">Price</th>
                <th class="sortable">Expiry</th>
                <th class="sortable" data-type="num">DTE</th>
                <th>Put Spread</th>
                <th>Call Spread</th>
                <th class="sortable" data-type="num">Credit</th>
                <th class="sortable" data-type="num">Max Risk</th>
                <th class="sortable" data-type="num">RoR %</th>
                <th class="sortable" data-type="num">Prob Profit</th>
                <th class="sortable" data-type="num">IV Rank</th>
            </tr></thead>
            <tbody>{rows}</tbody>
        </table>"""

    def _render_spread_table(self, spreads: List[Dict]) -> str:
        if not spreads:
            return '<p style="color: var(--muted); padding: 20px;">No credit spread opportunities found...</p>'

        rows = ''
        for s in spreads:
            rows += f"""<tr>
                <td><strong>{s.get('symbol', '?')}</strong></td>
                <td>${s.get('stock_price', '?')}</td>
                <td><span class="badge badge-green">{s.get('strategy', '?')}</span></td>
                <td>{s.get('expiry', '?')}</td>
                <td>{s.get('dte', '?')}d</td>
                <td>${s.get('short_strike', '?')}/{s.get('long_strike', '?')}</td>
                <td class="positive">${s.get('credit', '?')}</td>
                <td>${s.get('max_risk', '?'):,.0f}</td>
                <td class="positive">{s.get('return_on_risk_pct', '?')}%</td>
                <td>{s.get('prob_otm', '?')}%</td>
            </tr>"""

        return f"""
        <table id="spread-table">
            <thead><tr>
                <th class="sortable">Symbol</th>
                <th class="sortable" data-type="num">Price</th>
                <th>Strategy</th>
                <th class="sortable">Expiry</th>
                <th class="sortable" data-type="num">DTE</th>
                <th>Strikes</th>
                <th class="sortable" data-type="num">Credit</th>
                <th class="sortable" data-type="num">Max Risk</th>
                <th class="sortable" data-type="num">RoR %</th>
                <th class="sortable" data-type="num">Prob OTM</th>
            </tr></thead>
            <tbody>{rows}</tbody>
        </table>"""
