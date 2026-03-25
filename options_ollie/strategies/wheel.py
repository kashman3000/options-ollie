"""
Options Ollie — Wheel Strategy Manager
Manages the full wheel cycle: Cash-Secured Put → Assignment → Covered Call → Called Away → Repeat
"""

from typing import Dict, List, Optional, Tuple
from datetime import datetime
import pandas as pd
from ..config import OllieConfig, Position, Portfolio
from ..data.fetcher import OptionsDataFetcher
from ..data.screener import OptionsScreener


class WheelManager:
    """
    Manages the options wheel strategy lifecycle.

    The Wheel:
    1. SELL cash-secured put (CSP) on a stock you're happy to own
    2. If assigned → you now own 100 shares at strike - premium
    3. SELL covered call (CC) above your cost basis
    4. If called away → profit on shares + premium collected
    5. Repeat from step 1

    Key principles:
    - Only wheel stocks you'd be comfortable holding long-term
    - Sell at ~25 delta (75%+ probability of expiring OTM)
    - Target 30-45 DTE for optimal theta decay
    - Close at 50% profit to redeploy capital
    - Avoid selling through earnings dates
    """

    def __init__(self, config: OllieConfig, fetcher: OptionsDataFetcher):
        self.config = config
        self.fetcher = fetcher
        self.screener = OptionsScreener(fetcher, config.risk)

    def get_wheel_status(self) -> Dict:
        """Get current status of all wheel positions."""
        portfolio = self.config.portfolio

        status = {
            'shares_positions': [],
            'open_csps': [],
            'open_ccs': [],
            'ready_for_csp': [],     # Cash available, no open position
            'ready_for_cc': [],      # Shares held, no open call
            'total_premium_collected': 0,
            'active_wheels': 0,
        }

        # Catalog all open positions
        for pos in portfolio.positions:
            if pos.status != 'open':
                continue

            if pos.position_type == 'shares':
                status['shares_positions'].append(pos)
            elif pos.position_type == 'csp':
                status['open_csps'].append(pos)
            elif pos.position_type == 'covered_call':
                status['open_ccs'].append(pos)

        # Find shares without covered calls (ready for CC)
        symbols_with_shares = set()
        for pos in status['shares_positions']:
            symbols_with_shares.add(pos.symbol)

        symbols_with_ccs = set()
        for pos in status['open_ccs']:
            symbols_with_ccs.add(pos.symbol)

        for sym in symbols_with_shares:
            if sym not in symbols_with_ccs:
                shares = portfolio.shares_held(sym)
                if shares >= 100:
                    status['ready_for_cc'].append({
                        'symbol': sym,
                        'shares': shares,
                        'contracts_available': shares // 100
                    })

        # Calculate total premium collected from history
        for trade in portfolio.trade_history:
            status['total_premium_collected'] += trade.get('premium', 0)

        status['active_wheels'] = (
            len(status['open_csps']) +
            len(status['open_ccs']) +
            len(status['ready_for_cc'])
        )

        return status

    def recommend_rddt_action(self, shares: int = 200,
                               avg_cost: float = None) -> Dict:
        """
        Comprehensive RDDT position analysis:
        - Covered call opportunities
        - Downside scenario analysis (-5% to -30%)
        - Protective put pricing
        - Collar strategy modelling
        - Cost-basis reduction roadmap
        """
        import numpy as np
        from scipy.stats import norm

        info = self.fetcher.get_stock_info('RDDT')
        if not info:
            return {'error': 'Could not fetch RDDT data'}

        current_price = info['price']
        cost_basis = avg_cost or current_price
        contracts = shares // 100
        position_value = round(current_price * shares, 2)
        total_cost = round(cost_basis * shares, 2)
        unrealized_pnl = round((current_price - cost_basis) * shares, 2)

        # ── Fetch options chain (wide DTE range for protection analysis) ──
        chain_df = self.fetcher.get_options_chain('RDDT', min_dte=20, max_dte=60)
        iv_rank = self.fetcher.get_iv_rank('RDDT')
        iv_rank_pct = round(iv_rank * 100, 1)

        # ── Covered calls ─────────────────────────────────────────────────
        cc_candidates = self.screener.screen_covered_call_candidates(
            'RDDT', shares, avg_cost=cost_basis
        )

        recommendation = {
            'symbol': 'RDDT',
            'current_price': current_price,
            'cost_basis': cost_basis,
            'shares_held': shares,
            'contracts_available': contracts,
            'iv_rank': iv_rank_pct,
            'position_value': position_value,
            'total_cost': total_cost,
            'unrealized_pnl': unrealized_pnl,
            'action': '',
            'reasoning': '',
            'top_covered_calls': [],
            'risk_notes': [],
            # New sections
            'risk_analysis': {},
            'protective_strategies': [],
            'collar_strategies': [],
            'cost_basis_roadmap': {},
        }

        # ── CC recommendation ──────────────────────────────────────────────
        if not cc_candidates.empty:
            top_3 = cc_candidates.head(3).to_dict('records')
            recommendation['top_covered_calls'] = top_3
            best = top_3[0]
            recommendation['action'] = (
                f"SELL {contracts}x RDDT ${best['strike']} Call "
                f"exp {best['expiry']} for ~${best['mid_price']:.2f}/contract"
            )
            recommendation['reasoning'] = (
                f"IV Rank {iv_rank_pct:.0f}% — "
                f"{'elevated, good premium opportunity' if iv_rank > 0.4 else 'moderate environment'}. "
                f"${best['strike']} strike is {best['upside_to_strike_pct']:.1f}% above current price "
                f"with {best['prob_otm']:.0f}% probability of expiring OTM. "
                f"Total premium: ~${best['total_premium']:.0f} "
                f"({best['annualized_if_called']:.1f}% annualized if called away)."
            )
        else:
            recommendation['action'] = "HOLD — premiums thin at current IV. See downside risk analysis below."
            recommendation['reasoning'] = (
                f"IV Rank is only {iv_rank_pct:.0f}% — premiums are compressed. "
                "No CC strikes pass the minimum return threshold above your cost basis. "
                "With no income coming in, protecting the position becomes the priority."
            )

        # ── Downside scenario analysis ─────────────────────────────────────
        scenarios = []
        key_levels = {
            'cost_basis': cost_basis,
            '52w_low': info.get('low_52w', current_price * 0.6),
            '10pct_below': round(current_price * 0.90, 2),
            '20pct_below': round(current_price * 0.80, 2),
        }
        for drop_pct in [5, 10, 15, 20, 30]:
            target_price = round(current_price * (1 - drop_pct / 100), 2)
            pnl_vs_current = round((target_price - current_price) * shares, 2)
            pnl_vs_basis = round((target_price - cost_basis) * shares, 2)
            pct_of_cost = round(pnl_vs_basis / total_cost * 100, 1)
            below_basis = target_price < cost_basis
            scenarios.append({
                'drop_pct': drop_pct,
                'target_price': target_price,
                'dollar_loss_from_now': pnl_vs_current,
                'pnl_vs_cost_basis': pnl_vs_basis,
                'pct_of_capital': pct_of_cost,
                'below_cost_basis': below_basis,
            })

        recommendation['risk_analysis'] = {
            'position_value': position_value,
            'total_cost': total_cost,
            'unrealized_pnl': unrealized_pnl,
            'pct_from_cost_basis': round((current_price - cost_basis) / cost_basis * 100, 1),
            'pct_to_52w_low': round((current_price - key_levels['52w_low']) / current_price * 100, 1),
            '52w_low': key_levels['52w_low'],
            '52w_high': info.get('high_52w', current_price),
            'hv_30': info.get('hv_30', 0),
            'scenarios': scenarios,
            'one_sigma_down': round(current_price * (1 - info.get('hv_30', 0.5) * (30/252)**0.5), 2),
            'one_sigma_up': round(current_price * (1 + info.get('hv_30', 0.5) * (30/252)**0.5), 2),
        }

        # ── Protective put analysis ────────────────────────────────────────
        protective_puts = []
        if not chain_df.empty:
            puts = chain_df[chain_df['option_type'] == 'put'].copy()
            if not puts.empty:
                for floor_pct in [5, 10, 15, 20]:
                    target_strike = current_price * (1 - floor_pct / 100)
                    nearest = puts.iloc[(puts['strike'] - target_strike).abs().argsort()[:1]]
                    if nearest.empty:
                        continue
                    row = nearest.iloc[0]
                    mid = float(row.get('mid_price', row.get('ask', 0)))
                    if mid <= 0:
                        mid = float(row.get('ask', 0.5))
                    total_cost_puts = round(mid * contracts * 100, 2)
                    effective_floor = round(float(row['strike']) - mid, 2)
                    floor_vs_basis = round((effective_floor - cost_basis) / cost_basis * 100, 1)
                    # Annualised cost of protection
                    dte = int(row.get('dte', 30))
                    ann_cost_pct = round((mid / current_price) * (365 / max(dte, 1)) * 100, 2)
                    protective_puts.append({
                        'floor_pct': floor_pct,
                        'strike': float(row['strike']),
                        'expiry': str(row.get('expiry', '')),
                        'dte': dte,
                        'mid_price': round(mid, 2),
                        'total_cost': total_cost_puts,
                        'effective_floor': effective_floor,
                        'floor_vs_basis_pct': floor_vs_basis,
                        'annualised_cost_pct': ann_cost_pct,
                        'verdict': (
                            'Cheap insurance' if ann_cost_pct < 8
                            else 'Moderate cost' if ann_cost_pct < 15
                            else 'Expensive — consider collar instead'
                        ),
                    })

        recommendation['protective_strategies'] = protective_puts

        # ── Collar analysis (sell CC + buy put = near-zero cost hedge) ────
        collar_strategies = []
        if not chain_df.empty and not cc_candidates.empty:
            puts = chain_df[chain_df['option_type'] == 'put'].copy()
            # For each CC candidate, find a matching put for a collar
            for cc in cc_candidates.head(3).to_dict('records'):
                cc_expiry = cc.get('expiry', '')
                # Match put expiry closest to CC expiry
                if puts.empty:
                    continue
                exp_puts = puts[puts['expiry'] == cc_expiry] if cc_expiry in puts['expiry'].values else puts
                for floor_pct in [10, 15]:
                    target_put_strike = current_price * (1 - floor_pct / 100)
                    nearest = exp_puts.iloc[(exp_puts['strike'] - target_put_strike).abs().argsort()[:1]]
                    if nearest.empty:
                        continue
                    p_row = nearest.iloc[0]
                    put_cost = float(p_row.get('mid_price', p_row.get('ask', 0)))
                    cc_premium = float(cc.get('mid_price', 0))
                    net_credit = round((cc_premium - put_cost) * contracts * 100, 2)
                    collar_strategies.append({
                        'cc_strike': cc['strike'],
                        'cc_premium': round(cc_premium, 2),
                        'put_strike': float(p_row['strike']),
                        'put_cost': round(put_cost, 2),
                        'expiry': cc_expiry,
                        'dte': cc.get('dte', 0),
                        'net_credit': net_credit,
                        'floor_pct': floor_pct,
                        'upside_capped_at': cc['strike'],
                        'downside_floored_at': float(p_row['strike']),
                        'structure': (
                            f"SELL ${cc['strike']}C / BUY ${float(p_row['strike']):.0f}P exp {cc_expiry}"
                        ),
                        'verdict': (
                            f"Net {'credit' if net_credit >= 0 else 'debit'}: "
                            f"${abs(net_credit):.0f} — "
                            f"{'earns income AND limits loss' if net_credit > 0 else 'small cost for defined risk'}"
                        ),
                    })

        recommendation['collar_strategies'] = collar_strategies[:4]  # top 4

        # ── Cost-basis reduction roadmap ────────────────────────────────────
        # Calculate how many months of selling CCs reduces basis to key targets
        monthly_income_estimate = 0
        if not cc_candidates.empty:
            best_cc = cc_candidates.iloc[0]
            monthly_income_estimate = round(
                float(best_cc.get('total_premium', 0)) * (30 / max(int(best_cc.get('dte', 30)), 1)), 2
            )

        targets = []
        for target_pct in [5, 10, 15]:
            target_basis = round(cost_basis * (1 - target_pct / 100), 2)
            reduction_needed = round((cost_basis - target_basis) * shares, 2)
            months_needed = (
                round(reduction_needed / monthly_income_estimate, 1)
                if monthly_income_estimate > 0 else None
            )
            targets.append({
                'target_pct': target_pct,
                'target_basis': target_basis,
                'reduction_needed': reduction_needed,
                'months_at_current_cc': months_needed,
            })

        recommendation['cost_basis_roadmap'] = {
            'current_basis': cost_basis,
            'monthly_cc_income_estimate': monthly_income_estimate,
            'targets': targets,
            'strategy_note': (
                "Each covered call you sell permanently reduces your effective cost basis. "
                "Even in low-IV environments, selling slightly OTM calls consistently "
                "builds a cushion against downside over time."
                if monthly_income_estimate > 0
                else
                "IV too low for meaningful CC income right now. "
                "A collar (CC + protective put) may be the best risk-managed approach "
                "until volatility picks up."
            ),
        }

        # ── Risk notes (enhanced) ──────────────────────────────────────────
        hv = info.get('hv_30', 0)
        one_sigma_move = round(current_price * hv * (30 / 252) ** 0.5, 2)
        recommendation['risk_notes'] = [
            f"RDDT 30-day historical vol: {hv*100:.0f}% — "
            f"a typical monthly move is ±${one_sigma_move:.0f} (1 standard deviation)",
            f"Position value: ${position_value:,.0f} | Cost basis: ${total_cost:,.0f} | "
            f"Unrealized P&L: ${unrealized_pnl:+,.0f}",
            f"52-week range: ${key_levels['52w_low']:.2f} – ${info.get('high_52w', current_price):.2f} "
            f"(currently {info.get('pct_from_high', 0):.1f}% off high)",
            "Never sell options through earnings — check next earnings date before placing any trade",
        ]
        if current_price < cost_basis:
            recommendation['risk_notes'].insert(0,
                f"⚠️ Stock is ${cost_basis - current_price:.2f} BELOW cost basis. "
                "Selling CCs below basis locks in loss if called away. "
                "Consider a collar for protection while waiting for recovery."
            )
        elif (current_price - cost_basis) / cost_basis < 0.03:
            recommendation['risk_notes'].insert(0,
                "⚠️ Near break-even — a 10% drop turns this into a meaningful loss. "
                "Review the downside scenarios and protective strategies below."
            )

        return recommendation

    def generate_wheel_plan(self, capital: float,
                             num_positions: int = 3) -> Dict:
        """
        Generate a complete wheel strategy deployment plan.
        Allocates capital across multiple positions for diversification.
        """
        allocation_per_position = capital / num_positions

        # Screen for CSP candidates that fit the allocation
        max_price = allocation_per_position / 100  # Must afford 100 shares

        csp_candidates = self.screener.screen_wheel_candidates(
            max_stock_price=max_price
        )

        plan = {
            'total_capital': capital,
            'num_positions': num_positions,
            'allocation_per_position': round(allocation_per_position, 2),
            'max_stock_price': round(max_price, 2),
            'candidates': [],
            'estimated_monthly_income': 0,
            'portfolio_strategy': '',
        }

        if csp_candidates.empty:
            plan['portfolio_strategy'] = "No candidates found. Try increasing capital or relaxing filters."
            return plan

        # Pick top candidates with sector diversification
        selected = []
        sectors_used = set()

        for _, row in csp_candidates.iterrows():
            if len(selected) >= num_positions:
                break

            sector = row.get('sector', 'Unknown')
            # Allow max 2 from same sector
            if list(sectors_used).count(sector) >= 2:
                continue

            if row['capital_required'] <= allocation_per_position:
                selected.append(row.to_dict())
                sectors_used.add(sector)

        plan['candidates'] = selected

        total_monthly = sum(
            c['premium_100'] * (30 / c['dte']) for c in selected
        )
        plan['estimated_monthly_income'] = round(total_monthly, 2)
        plan['estimated_monthly_return_pct'] = round(total_monthly / capital * 100, 2)

        plan['portfolio_strategy'] = (
            f"Deploy {len(selected)} wheel positions across "
            f"{len(set(c.get('sector', 'N/A') for c in selected))} sectors. "
            f"Estimated monthly income: ${total_monthly:.0f} "
            f"({total_monthly/capital*100:.1f}% monthly return). "
            f"Manage at 50% profit target, 2x stop loss."
        )

        return plan

    def check_management_actions(self) -> List[Dict]:
        """
        Check all open positions for management actions needed:
        - Close at 50% profit
        - Roll if < 5 DTE
        - Stop loss check
        - Approaching earnings
        """
        actions = []
        today = datetime.now().date()

        for pos in self.config.portfolio.positions:
            if pos.status != 'open' or pos.position_type == 'shares':
                continue

            if not pos.expiry_date:
                continue

            expiry = datetime.strptime(pos.expiry_date, '%Y-%m-%d').date()
            dte = (expiry - today).days

            action = {
                'position': vars(pos),
                'dte_remaining': dte,
                'action': 'HOLD',
                'urgency': 'low',
                'reason': '',
            }

            # Check DTE — roll if close to expiry
            if dte <= 5:
                action['action'] = 'ROLL or CLOSE'
                action['urgency'] = 'high'
                action['reason'] = f"Only {dte} DTE remaining. Roll out for more premium or close."

            # Check if approaching earnings
            elif dte <= 7:
                action['action'] = 'MONITOR CLOSELY'
                action['urgency'] = 'medium'
                action['reason'] = f"{dte} DTE — watch for early assignment risk."

            if action['action'] != 'HOLD':
                actions.append(action)

        return actions
