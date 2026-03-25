"""
Options Ollie — Telegram Notifications
Sends trade signals, alerts, and daily summaries to your Telegram.
"""

import requests
import json
from typing import Dict, List, Optional
from datetime import datetime
from ..config import TelegramConfig


class TelegramBot:
    """Send formatted options alerts to Telegram."""

    def __init__(self, config: TelegramConfig):
        self.config = config
        self.base_url = f"https://api.telegram.org/bot{config.bot_token}"

    def send_message(self, text: str, parse_mode: str = 'HTML') -> bool:
        """Send a message to the configured chat."""
        if not self.config.enabled:
            print(f"[Telegram disabled] Would send:\n{text[:200]}...")
            return False

        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    'chat_id': self.config.chat_id,
                    'text': text,
                    'parse_mode': parse_mode,
                    'disable_web_page_preview': True,
                },
                timeout=10,
            )
            return resp.ok
        except Exception as e:
            print(f"[Telegram error] {e}")
            return False

    # ── Formatted Alert Templates ────────────────────────────────────────

    def send_trade_signal(self, signal: Dict) -> bool:
        """Send a new trade signal alert."""
        strategy = signal.get('strategy', 'Unknown')
        symbol = signal.get('symbol', '???')

        if strategy == 'CSP':
            msg = self._format_csp_signal(signal)
        elif strategy == 'CC':
            msg = self._format_cc_signal(signal)
        elif strategy == 'IC':
            msg = self._format_ic_signal(signal)
        else:
            msg = self._format_generic_signal(signal)

        return self.send_message(msg)

    def send_daily_summary(self, summary: Dict) -> bool:
        """Send daily portfolio summary."""
        now = datetime.now().strftime('%Y-%m-%d %H:%M')

        lines = [
            f"📊 <b>Options Ollie — Daily Summary</b>",
            f"📅 {now}",
            f"",
        ]

        # Portfolio overview
        portfolio = summary.get('portfolio', {})
        lines.append(f"💰 <b>Portfolio Value:</b> ${portfolio.get('total_value', 0):,.0f}")
        lines.append(f"💵 <b>Cash Available:</b> ${portfolio.get('cash', 0):,.0f}")
        lines.append(f"📈 <b>Open Positions:</b> {portfolio.get('open_positions', 0)}")
        lines.append(f"🎯 <b>Total Premium (MTD):</b> ${portfolio.get('premium_mtd', 0):,.0f}")
        lines.append("")

        # Wheel status
        wheel = summary.get('wheel', {})
        if wheel:
            lines.append(f"🎡 <b>Wheel Status:</b>")
            lines.append(f"  CSPs open: {wheel.get('open_csps', 0)}")
            lines.append(f"  CCs open: {wheel.get('open_ccs', 0)}")
            lines.append(f"  Ready for action: {wheel.get('ready_for_action', 0)}")
            lines.append("")

        # Management actions needed
        actions = summary.get('actions', [])
        if actions:
            lines.append(f"⚡ <b>Actions Needed:</b>")
            for a in actions[:5]:
                emoji = "🔴" if a.get('urgency') == 'high' else "🟡"
                lines.append(f"  {emoji} {a.get('symbol', '?')}: {a.get('action', '?')}")
            lines.append("")

        # Top new signals
        signals = summary.get('signals', [])
        if signals:
            lines.append(f"🆕 <b>Top Signals Today:</b>")
            for s in signals[:3]:
                lines.append(
                    f"  • {s['strategy']} {s['symbol']} "
                    f"${s.get('strike', s.get('short_put', '?'))} "
                    f"exp {s.get('expiry', '?')} "
                    f"— {s.get('prob_otm', s.get('prob_profit_est', '?'))}% POP"
                )

        return self.send_message('\n'.join(lines))

    def send_management_alert(self, action: Dict) -> bool:
        """Alert for position management (rolling, closing, etc.)."""
        urgency_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        pos = action.get('position', {})

        msg = (
            f"{urgency_emoji.get(action.get('urgency', 'low'), '⚪')} "
            f"<b>Position Alert — {pos.get('symbol', '?')}</b>\n\n"
            f"Type: {pos.get('position_type', '?').upper()}\n"
            f"Strike: ${pos.get('strike', '?')}\n"
            f"Expiry: {pos.get('expiry_date', '?')}\n"
            f"DTE: {action.get('dte_remaining', '?')} days\n\n"
            f"<b>Action:</b> {action.get('action', '?')}\n"
            f"<b>Reason:</b> {action.get('reason', '?')}"
        )
        return self.send_message(msg)

    def send_rddt_update(self, recommendation: Dict) -> bool:
        """Send RDDT-specific wheel update."""
        msg_lines = [
            f"🔵 <b>RDDT Wheel Update</b>",
            f"",
            f"💲 Current Price: ${recommendation.get('current_price', '?')}",
            f"📊 IV Rank: {recommendation.get('iv_rank', '?')}%",
            f"📦 Shares Held: {recommendation.get('shares_held', 0)}",
            f"📝 Contracts Available: {recommendation.get('contracts_available', 0)}",
            f"",
            f"<b>Recommendation:</b>",
            f"{recommendation.get('action', 'No action')}",
            f"",
            f"<b>Reasoning:</b>",
            f"{recommendation.get('reasoning', '')}",
        ]

        top_ccs = recommendation.get('top_covered_calls', [])
        if top_ccs:
            msg_lines.append("")
            msg_lines.append("<b>Top 3 Covered Call Strikes:</b>")
            for i, cc in enumerate(top_ccs, 1):
                msg_lines.append(
                    f"  {i}. ${cc['strike']} exp {cc['expiry']} "
                    f"— ${cc['mid_price']:.2f} ({cc['prob_otm']:.0f}% OTM)"
                )

        return self.send_message('\n'.join(msg_lines))

    def send_position_monitor_summary(self, report: Dict) -> bool:
        """
        Send a daily position monitor digest — P&L summary + any urgent/action items.
        """
        now = datetime.now().strftime('%Y-%m-%d %H:%M')
        positions = report.get('positions', [])
        pnl = report.get('total_unrealized_pnl', 0)
        pnl_emoji = '🟢' if pnl >= 0 else '🔴'
        pct = report.get('overall_pct_captured', 0)

        lines = [
            f"📊 <b>Options Ollie — Position Monitor</b>",
            f"📅 {now}",
            f"",
            f"<b>Open Positions:</b> {report.get('total_positions', 0)}",
            f"<b>Premium at Risk:</b> ${report.get('total_premium_at_risk', 0):,.0f}",
            f"{pnl_emoji} <b>Unrealized P&amp;L:</b> ${pnl:+,.0f}  ({pct:.0f}% of max profit captured)",
            f"",
        ]

        # Priority positions first
        level_order = {'URGENT': 0, 'ACTION': 1, 'WATCH': 2, 'HOLD': 3}
        sorted_positions = sorted(positions, key=lambda p: level_order.get(p.get('advice_level', 'HOLD'), 9))

        for p in sorted_positions:
            level = p.get('advice_level', 'HOLD')
            icon = {'URGENT': '🚨', 'ACTION': '✅', 'WATCH': '👁', 'HOLD': '✓'}.get(level, '•')
            sym = p.get('symbol', '?')
            tt = p.get('trade_type', '?').upper()
            pos_pnl = p.get('unrealized_pnl', 0)
            pos_pct = p.get('pct_max_profit', 0)
            dte = p.get('dte')
            dte_str = f"{dte}d" if dte is not None else '—'

            lines.append(
                f"{icon} <b>{sym} {tt}</b> [{p.get('trade_id','?')}]"
            )
            lines.append(
                f"   Stock: ${p.get('current_price',0):.2f}  |  DTE: {dte_str}  |  "
                f"P&amp;L: ${pos_pnl:+,.0f} ({pos_pct:.0f}% captured)"
            )
            lines.append(f"   <i>{p.get('advice_headline','')}</i>")
            if level in ('URGENT', 'ACTION') and p.get('advice_actions'):
                for action in p['advice_actions'][:2]:
                    lines.append(f"   • {action}")
            lines.append("")

        lines.append("Run <code>--monitor</code> to refresh and update dashboard.")
        return self.send_message('\n'.join(lines))

    # ── Formatting Helpers ───────────────────────────────────────────────

    def _format_csp_signal(self, s: Dict) -> str:
        return (
            f"🟢 <b>NEW SIGNAL — Cash-Secured Put</b>\n\n"
            f"<b>{s['symbol']}</b> @ ${s.get('stock_price', '?')}\n"
            f"SELL {s['symbol']} ${s['strike']}P exp {s['expiry']}\n"
            f"Premium: ${s.get('mid_price', '?')}/contract (${s.get('premium_100', '?')} per lot)\n"
            f"Capital Required: ${s.get('capital_required', '?'):,.0f}\n"
            f"Annualized Return: {s.get('annualized_return', '?')}%\n"
            f"Prob OTM: {s.get('prob_otm', '?')}%\n"
            f"IV Rank: {s.get('iv_rank', '?')}%\n"
            f"Score: {s.get('score', '?')}"
        )

    def _format_cc_signal(self, s: Dict) -> str:
        return (
            f"🔵 <b>NEW SIGNAL — Covered Call</b>\n\n"
            f"<b>{s['symbol']}</b> @ ${s.get('stock_price', '?')}\n"
            f"SELL {s.get('contracts', '?')}x {s['symbol']} ${s['strike']}C exp {s['expiry']}\n"
            f"Premium: ${s.get('mid_price', '?')}/contract (${s.get('total_premium', '?')} total)\n"
            f"Upside to Strike: {s.get('upside_to_strike_pct', '?')}%\n"
            f"Annualized if Called: {s.get('annualized_if_called', '?')}%\n"
            f"Prob OTM: {s.get('prob_otm', '?')}%"
        )

    def _format_ic_signal(self, s: Dict) -> str:
        return (
            f"🟠 <b>NEW SIGNAL — Iron Condor</b>\n\n"
            f"<b>{s['symbol']}</b> @ ${s.get('stock_price', '?')}\n"
            f"PUT side: -{s.get('short_put', '?')}/+{s.get('long_put', '?')}\n"
            f"CALL side: -{s.get('short_call', '?')}/+{s.get('long_call', '?')}\n"
            f"Expiry: {s['expiry']} ({s.get('dte', '?')} DTE)\n"
            f"Total Credit: ${s.get('total_credit', '?')} (${s.get('total_credit_100', '?')} per lot)\n"
            f"Max Risk: ${s.get('max_risk', '?')}\n"
            f"Return on Risk: {s.get('return_on_risk_pct', '?')}%\n"
            f"Est. Prob Profit: {s.get('prob_profit_est', '?')}%\n"
            f"IV Rank: {s.get('iv_rank', '?')}%"
        )

    def _format_generic_signal(self, s: Dict) -> str:
        return (
            f"📢 <b>NEW SIGNAL — {s.get('strategy', 'Options')}</b>\n\n"
            f"<b>{s.get('symbol', '?')}</b>\n"
            + '\n'.join(f"{k}: {v}" for k, v in s.items() if k not in ('strategy', 'symbol'))
        )
