#!/usr/bin/env python3
"""
Options Ollie — Main Orchestrator
Run the full scan, generate dashboard, and send Telegram alerts.

Usage:
    python -m options_ollie.main                  # Full scan + dashboard
    python -m options_ollie.main --rddt            # RDDT analysis only
    python -m options_ollie.main --scan wheel      # Wheel candidates only
    python -m options_ollie.main --scan condors    # Iron condors only
    python -m options_ollie.main --scan spreads    # Credit spreads only
    python -m options_ollie.main --telegram        # Send results via Telegram
    python -m options_ollie.main --log-trade       # Log a confirmed trade entry
    python -m options_ollie.main --monitor         # Monitor open positions + get advice
    python -m options_ollie.main --positions       # Show current open positions
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Dict

from .config import OllieConfig, Position, FULL_WATCHLIST, WATCHLIST_ETFS
from .data.fetcher import OptionsDataFetcher
from .data.screener import OptionsScreener
from .strategies.wheel import WheelManager
from .strategies.trade_ledger import TradeLedger, TradeType
from .strategies.position_monitor import PositionMonitor, ADVICE_URGENT, ADVICE_ACTION, ADVICE_WATCH
from .notifications.telegram import TelegramBot
from .dashboard.generator import DashboardGenerator


def print_banner():
    print("""
╔══════════════════════════════════════════════════════╗
║                                                      ║
║   🎯  OPTIONS OLLIE — Your Options Trading Agent     ║
║                                                      ║
║   Wheel Strategy • Iron Condors • Credit Spreads     ║
║   Moderate Risk • 70%+ Probability • Data-Driven     ║
║                                                      ║
╚══════════════════════════════════════════════════════╝
    """)


def run_full_scan(config: OllieConfig, args) -> Dict:
    """Run the complete options scanning pipeline."""
    fetcher = OptionsDataFetcher()
    screener = OptionsScreener(fetcher, config.risk)
    wheel_mgr = WheelManager(config, fetcher)
    telegram = TelegramBot(config.telegram)
    dashboard = DashboardGenerator()

    results = {}
    start = time.time()

    # ── 1. RDDT Analysis ────────────────────────────────────────────────
    print("\n📊 Analyzing RDDT position...")
    rddt_rec = wheel_mgr.recommend_rddt_action(
        shares=args.rddt_shares,
        avg_cost=args.rddt_cost
    )
    results['rddt_recommendation'] = rddt_rec

    if rddt_rec.get('action'):
        print(f"   → {rddt_rec['action']}")
        print(f"   → IV Rank: {rddt_rec.get('iv_rank', '?')}%")
        top_ccs = rddt_rec.get('top_covered_calls', [])
        if top_ccs:
            print(f"   → Top CC: ${top_ccs[0]['strike']} exp {top_ccs[0]['expiry']} "
                  f"(${top_ccs[0]['mid_price']:.2f}, {top_ccs[0]['prob_otm']:.0f}% OTM)")

    if args.rddt_only:
        # Still generate dashboard with just RDDT data
        output_path = os.path.join(args.output_dir, 'dashboard.html')
        dashboard.generate(results, output_path)
        print(f"\n✅ Dashboard saved to: {output_path}")
        if args.telegram:
            telegram.send_rddt_update(rddt_rec)
        return results

    # ── 2. Wheel Candidate Screening ────────────────────────────────────
    if args.scan in ('all', 'wheel'):
        print("\n🎡 Screening wheel candidates...")
        max_price = args.max_stock_price or 200
        # Use a focused list for speed, expand for thoroughness
        scan_list = FULL_WATCHLIST if args.thorough else FULL_WATCHLIST[:20]

        wheel_df = screener.screen_wheel_candidates(
            symbols=scan_list,
            max_stock_price=max_price
        )
        if not wheel_df.empty:
            results['wheel_candidates'] = wheel_df.head(25).to_dict('records')
            print(f"   → Found {len(wheel_df)} candidates, showing top 25")
            top = wheel_df.iloc[0]
            print(f"   → #1: {top['symbol']} ${top['strike']}P exp {top['expiry']} "
                  f"— {top['annualized_return']}% ann, {top['prob_otm']}% OTM, score {top['score']}")
        else:
            results['wheel_candidates'] = []
            print("   → No candidates matched filters")

    # ── 3. Iron Condor Screening ────────────────────────────────────────
    if args.scan in ('all', 'condors'):
        print("\n🦅 Screening iron condors...")
        ic_symbols = WATCHLIST_ETFS[:6] + ['AAPL', 'MSFT', 'AMZN', 'GOOGL', 'META', 'NVDA']
        condor_df = screener.screen_iron_condors(symbols=ic_symbols)
        if not condor_df.empty:
            results['iron_condors'] = condor_df.head(15).to_dict('records')
            print(f"   → Found {len(condor_df)} opportunities, showing top 15")
            top = condor_df.iloc[0]
            print(f"   → #1: {top['symbol']} IC ${top['short_put']}/{top['short_call']} "
                  f"exp {top['expiry']} — ${top['total_credit']:.2f} credit, "
                  f"{top['return_on_risk_pct']:.1f}% RoR")
        else:
            results['iron_condors'] = []
            print("   → No iron condor setups found (IV may be too low)")

    # ── 4. Credit Spread Screening ──────────────────────────────────────
    if args.scan in ('all', 'spreads'):
        print("\n📐 Screening credit spreads...")
        spread_df = screener.screen_credit_spreads(
            symbols=FULL_WATCHLIST[:15],
            spread_type='put'
        )
        if not spread_df.empty:
            results['credit_spreads'] = spread_df.head(15).to_dict('records')
            print(f"   → Found {len(spread_df)} spreads, showing top 15")
        else:
            results['credit_spreads'] = []
            print("   → No credit spread opportunities found")

    # ── 5. Generate Dashboard ───────────────────────────────────────────
    print("\n📊 Generating dashboard...")
    output_path = os.path.join(args.output_dir, 'dashboard.html')
    dashboard.generate(results, output_path)
    print(f"   → Dashboard saved to: {output_path}")

    # ── 6. Save scan results ────────────────────────────────────────────
    results_path = os.path.join(args.output_dir, 'latest_scan.json')
    with open(results_path, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"   → Results saved to: {results_path}")

    # ── 7. Telegram Alerts ──────────────────────────────────────────────
    if args.telegram:
        print("\n📱 Sending Telegram alerts...")
        telegram.send_rddt_update(rddt_rec)

        # Send top signals
        for strategy_key, label in [
            ('wheel_candidates', 'CSP'),
            ('iron_condors', 'IC'),
            ('credit_spreads', 'Spread')
        ]:
            signals = results.get(strategy_key, [])
            for sig in signals[:2]:  # Top 2 per strategy
                telegram.send_trade_signal(sig)

        # Daily summary
        summary = {
            'portfolio': {
                'total_value': config.portfolio.total_value(),
                'cash': config.portfolio.cash,
                'open_positions': len(config.portfolio.open_options()),
            },
            'signals': (
                results.get('wheel_candidates', [])[:2] +
                results.get('iron_condors', [])[:1]
            ),
        }
        telegram.send_daily_summary(summary)
        print("   → Alerts sent!")

    elapsed = time.time() - start
    print(f"\n✅ Scan complete in {elapsed:.1f}s")
    print(f"   Dashboard: file://{os.path.abspath(output_path)}")

    return results


def get_ledger_path(output_dir: str) -> str:
    return os.path.join(output_dir, 'data', 'trade_ledger.json')


def run_log_trade(output_dir: str):
    """
    Interactive CLI wizard to confirm a trade you just entered.
    Records the trade in the persistent ledger.
    """
    ledger = TradeLedger(get_ledger_path(output_dir))

    print("\n╔══════════════════════════════════════════════════════╗")
    print("║         📝  LOG A CONFIRMED TRADE ENTRY              ║")
    print("╚══════════════════════════════════════════════════════╝\n")
    print("Trade types:")
    print("  1. CSP         — Cash-secured put (sold)")
    print("  2. CC          — Covered call (sold)")
    print("  3. IC          — Iron condor (sold)")
    print("  4. BULL_PUT    — Bull put spread (credit)")
    print("  5. BEAR_CALL   — Bear call spread (credit)")
    print("  6. SHARES      — Long shares (purchase / assignment)\n")

    choice = input("Select trade type [1-6]: ").strip()
    type_map = {
        '1': 'csp', '2': 'cc', '3': 'ic',
        '4': 'bull_put', '5': 'bear_call', '6': 'shares'
    }
    trade_type = type_map.get(choice)
    if not trade_type:
        print("❌ Invalid selection.")
        return

    symbol = input("Ticker symbol (e.g. RDDT): ").strip().upper()
    if not symbol:
        print("❌ Symbol required.")
        return

    contracts_str = input("Number of contracts (default 1): ").strip() or '1'
    try:
        contracts = int(contracts_str)
    except ValueError:
        contracts = 1

    commission_str = input("Total commission paid (default 0): ").strip() or '0'
    try:
        commission = float(commission_str)
    except ValueError:
        commission = 0.0

    notes = input("Notes (optional, press Enter to skip): ").strip()

    trade = None

    if trade_type == 'csp':
        strike = float(input("Put strike price (e.g. 85): "))
        expiry = input("Expiry date (YYYY-MM-DD): ").strip()
        premium = float(input("Premium received PER CONTRACT (e.g. 1.45): "))
        trade = ledger.enter_csp(symbol, strike, expiry, premium, contracts, commission, notes)

    elif trade_type == 'cc':
        strike = float(input("Call strike price (e.g. 105): "))
        expiry = input("Expiry date (YYYY-MM-DD): ").strip()
        premium = float(input("Premium received PER CONTRACT (e.g. 1.20): "))
        trade = ledger.enter_covered_call(symbol, strike, expiry, premium, contracts, commission, notes)

    elif trade_type == 'ic':
        print("Iron Condor — enter all 4 strikes:")
        short_put  = float(input("  Short put strike  (e.g. 90): "))
        long_put   = float(input("  Long put strike   (e.g. 85): "))
        short_call = float(input("  Short call strike (e.g. 110): "))
        long_call  = float(input("  Long call strike  (e.g. 115): "))
        expiry     = input("Expiry date (YYYY-MM-DD): ").strip()
        credit     = float(input("Net credit received PER CONTRACT (e.g. 2.30): "))
        trade = ledger.enter_iron_condor(symbol, expiry, short_put, long_put,
                                         short_call, long_call, credit, contracts, commission, notes)

    elif trade_type == 'bull_put':
        short_strike = float(input("Short put strike (e.g. 90): "))
        long_strike  = float(input("Long put strike  (e.g. 85): "))
        expiry       = input("Expiry date (YYYY-MM-DD): ").strip()
        credit       = float(input("Net credit received PER CONTRACT (e.g. 1.50): "))
        trade = ledger.enter_credit_spread(symbol, expiry, short_strike, long_strike,
                                           'bull_put', credit, contracts, commission, notes)

    elif trade_type == 'bear_call':
        short_strike = float(input("Short call strike (e.g. 110): "))
        long_strike  = float(input("Long call strike  (e.g. 115): "))
        expiry       = input("Expiry date (YYYY-MM-DD): ").strip()
        credit       = float(input("Net credit received PER CONTRACT (e.g. 1.20): "))
        trade = ledger.enter_credit_spread(symbol, expiry, short_strike, long_strike,
                                           'bear_call', credit, contracts, commission, notes)

    elif trade_type == 'shares':
        shares   = int(input("Number of shares (e.g. 100): "))
        cost     = float(input("Cost per share (e.g. 87.50): "))
        trade = ledger.enter_shares(symbol, shares, cost, notes)
        contracts = shares

    if trade:
        print(f"\n✅ Trade confirmed and saved!")
        print(f"   ID:              {trade.id}")
        print(f"   Symbol:          {trade.symbol}")
        print(f"   Type:            {trade.trade_type}")
        if hasattr(trade, 'premium_received') and trade.premium_received:
            print(f"   Premium received: ${trade.premium_received:.2f}")
        if trade.expiry:
            print(f"   Expiry:          {trade.expiry}")
        print(f"\n   Use --monitor to track this position daily.")


def run_monitor(output_dir: str, telegram_enabled: bool = False, config: OllieConfig = None):
    """
    Monitor all open positions, show live P&L, and print management advice.
    """
    ledger = TradeLedger(get_ledger_path(output_dir))
    monitor = PositionMonitor(ledger)

    open_trades = ledger.open_trades()
    if not open_trades:
        print("\n📭 No open positions to monitor.")
        print("   Use --log-trade to confirm a trade entry first.")
        return {}

    print(f"\n⏳ Fetching live market data for {len(open_trades)} open position(s)...")
    snapshots = monitor.monitor_all()
    report = monitor.summary_report(snapshots)

    # ── Print summary ──────────────────────────────────────────────────────
    pnl = report['total_unrealized_pnl']
    pnl_color = '🟢' if pnl >= 0 else '🔴'
    print(f"\n{'═'*60}")
    print(f"  POSITION MONITOR  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'═'*60}")
    print(f"  Open positions:     {report['total_positions']}")
    print(f"  Premium at risk:   ${report['total_premium_at_risk']:.2f}")
    print(f"  Unrealized P&L:    {pnl_color} ${pnl:+.2f}  ({report['overall_pct_captured']:.1f}% captured)")
    if report['urgent_count']:
        print(f"  🚨 URGENT actions:  {report['urgent_count']}")
    if report['action_count']:
        print(f"  ✅ ACTION items:    {report['action_count']}")
    if report['watch_count']:
        print(f"  👁  WATCH items:     {report['watch_count']}")
    print(f"{'═'*60}\n")

    # ── Per-position details ───────────────────────────────────────────────
    level_order = {ADVICE_URGENT: 0, ADVICE_ACTION: 1, ADVICE_WATCH: 2, 'HOLD': 3}
    snapshots_sorted = sorted(snapshots, key=lambda s: level_order.get(s.advice_level, 9))

    for snap in snapshots_sorted:
        level_icon = {
            ADVICE_URGENT: '🚨', ADVICE_ACTION: '✅', ADVICE_WATCH: '👁 ', 'HOLD': '✓ '
        }.get(snap.advice_level, '  ')

        dte_str = f"{snap.dte}d" if snap.dte is not None else "—"
        pnl_str = f"${snap.unrealized_pnl:+.0f}" if snap.unrealized_pnl != 0 else "$0"

        print(f"  {level_icon} [{snap.trade_id}] {snap.symbol} — {snap.trade_type.upper()}")
        if snap.expiry:
            print(f"     Strike: {_format_strikes(snap)}  |  Exp: {snap.expiry}  |  DTE: {dte_str}")
        print(f"     Stock: ${snap.current_price:.2f}  |  P&L: {pnl_str}  |  Captured: {snap.pct_max_profit:.0f}%")
        print(f"     ➤ {snap.advice_headline}")
        if snap.advice_detail:
            # Word-wrap detail to 70 chars
            words = snap.advice_detail.split()
            line = "       "
            for word in words:
                if len(line) + len(word) + 1 > 76:
                    print(line)
                    line = "       " + word
                else:
                    line += (" " if line.strip() else "") + word
            if line.strip():
                print(line)
        if snap.advice_actions:
            print("     Actions:")
            for action in snap.advice_actions:
                print(f"       • {action}")
        print()

    # ── Telegram alert ────────────────────────────────────────────────────
    if telegram_enabled and config:
        telegram = TelegramBot(config.telegram)
        # Send full digest summary
        telegram.send_position_monitor_summary(report)
        # Also send individual urgent/action alerts for immediate attention
        urgent_and_action = [s for s in snapshots if s.advice_level == ADVICE_URGENT]
        for snap in urgent_and_action:
            _send_position_alert(telegram, snap)

    return report


def _format_strikes(snap) -> str:
    """Format strike display for a snapshot."""
    if snap.short_put_strike and snap.short_call_strike:
        return f"${snap.short_put_strike}/{snap.short_call_strike}"
    elif snap.strike:
        return f"${snap.strike}"
    return "—"


def _send_position_alert(telegram, snap):
    """Send a position management alert via Telegram."""
    try:
        icon = '🚨' if snap.advice_level == 'URGENT' else '✅'
        msg = (
            f"{icon} *Options Ollie Position Alert*\n\n"
            f"*{snap.symbol}* {snap.trade_type.upper()} [{snap.trade_id}]\n"
            f"Strike: {_format_strikes(snap)} | Exp: {snap.expiry} | DTE: {snap.dte}\n"
            f"Stock: ${snap.current_price:.2f} | P&L: ${snap.unrealized_pnl:+.0f} ({snap.pct_max_profit:.0f}% captured)\n\n"
            f"*{snap.advice_headline}*\n"
            f"{snap.advice_detail}\n\n"
            f"*Actions:*\n" + "\n".join(f"• {a}" for a in snap.advice_actions)
        )
        telegram.send_message(msg)
    except Exception:
        pass


def run_show_positions(output_dir: str):
    """Show all open positions from the ledger (no live data fetch)."""
    ledger = TradeLedger(get_ledger_path(output_dir))
    open_trades = ledger.open_trades()

    if not open_trades:
        print("\n📭 No open positions in ledger.")
        print("   Use --log-trade to record a confirmed trade entry.")
        return

    summary = ledger.summary()
    print(f"\n{'═'*60}")
    print(f"  OPEN POSITIONS  ({len(open_trades)} total)")
    print(f"{'═'*60}")
    print(f"  Total premium at risk: ${summary['open_premium_at_risk']:.2f}")
    print(f"  Collateral deployed:   ${summary['total_collateral_deployed']:.2f}")
    print(f"{'═'*60}\n")

    for trade in open_trades:
        dte = trade.days_to_expiry()
        dte_str = f"{dte}d" if dte is not None else "—"
        print(f"  [{trade.id}] {trade.symbol} — {trade.trade_type.upper()}")
        print(f"    Entry: {trade.entry_date}  |  Premium: ${trade.premium_received:.2f}  |  DTE: {dte_str}")
        if trade.expiry:
            print(f"    Expiry: {trade.expiry}  |  Strike(s): {_format_trade_strikes(trade)}")
        if trade.notes:
            print(f"    Notes: {trade.notes}")
        print()


def _format_trade_strikes(trade) -> str:
    if trade.short_put_strike and trade.short_call_strike:
        return f"${trade.short_put_strike}/{trade.short_call_strike}"
    elif trade.strike:
        return f"${trade.strike}"
    return "—"


def main():
    parser = argparse.ArgumentParser(description='Options Ollie — Your AI Options Trading Agent')
    parser.add_argument('--scan', choices=['all', 'wheel', 'condors', 'spreads'],
                        default='all', help='Which strategies to scan')
    parser.add_argument('--rddt-only', action='store_true',
                        help='Only analyze RDDT position')
    parser.add_argument('--rddt-shares', type=int, default=200,
                        help='Number of RDDT shares held (default: 200)')
    parser.add_argument('--rddt-cost', type=float, default=None,
                        help='Average cost basis for RDDT')
    parser.add_argument('--max-stock-price', type=float, default=200,
                        help='Max stock price for wheel candidates')
    parser.add_argument('--telegram', action='store_true',
                        help='Send results via Telegram')
    parser.add_argument('--thorough', action='store_true',
                        help='Scan full watchlist (slower but more comprehensive)')
    parser.add_argument('--output-dir', default='.',
                        help='Directory for output files')
    parser.add_argument('--log-trade', action='store_true',
                        help='Interactive wizard to confirm a trade you just entered')
    parser.add_argument('--monitor', action='store_true',
                        help='Monitor open positions with live P&L and management advice')
    parser.add_argument('--positions', action='store_true',
                        help='Show all open positions from the ledger')

    args = parser.parse_args()

    # Ensure output directory exists
    os.makedirs(args.output_dir, exist_ok=True)
    os.makedirs(os.path.join(args.output_dir, 'data'), exist_ok=True)

    print_banner()

    # ── Trade logging mode ─────────────────────────────────────────────────
    if args.log_trade:
        run_log_trade(args.output_dir)
        return

    # ── Position monitoring mode ────────────────────────────────────────────
    if args.monitor:
        config = OllieConfig()
        config.load_portfolio()
        report = run_monitor(args.output_dir, telegram_enabled=args.telegram, config=config)
        # Also regenerate dashboard with position data
        if report:
            dashboard = DashboardGenerator()
            output_path = os.path.join(args.output_dir, 'dashboard.html')
            dashboard.generate({'monitor_report': report}, output_path)
            print(f"   Dashboard updated: file://{os.path.abspath(output_path)}")
        return

    # ── Show positions mode ────────────────────────────────────────────────
    if args.positions:
        run_show_positions(args.output_dir)
        return

    config = OllieConfig()
    config.load_portfolio()

    # Set up RDDT position if not already tracked
    rddt_shares = config.portfolio.shares_held('RDDT')
    if rddt_shares == 0 and args.rddt_shares > 0:
        config.portfolio.positions.append(Position(
            symbol='RDDT',
            position_type='shares',
            quantity=args.rddt_shares,
            entry_price=args.rddt_cost or 0,
            entry_date=datetime.now().strftime('%Y-%m-%d'),
            status='open',
            notes='Pre-existing position'
        ))

    results = run_full_scan(config, args)

    # Save portfolio state
    config.save_portfolio()

    return results


if __name__ == '__main__':
    main()
