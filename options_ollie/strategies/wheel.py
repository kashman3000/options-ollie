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
from .oi_analysis import analyze_oi_structure
from .intelligence import next_best_action


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

    def recommend_action(self, symbol: str, shares: int = 100,
                          avg_cost: float = None,
                          extra_data: dict = None) -> Dict:
        """
        Comprehensive position analysis for any optionable US stock.
        For ASX stocks (symbol ends in .AX) options analysis is skipped —
        yfinance does not provide ASX options chains.
        - Covered call opportunities
        - Downside scenario analysis (-5% to -30%)
        - Protective put pricing
        - Collar strategy modelling
        - Cost-basis reduction roadmap
        - OI-based market structure coaching
        """
        import numpy as np
        from scipy.stats import norm

        symbol = symbol.upper().strip()
        is_asx = symbol.endswith('.AX')

        info = self.fetcher.get_stock_info(symbol)
        if not info:
            return {'error': f'Could not fetch data for {symbol}'}

        current_price = info['price']
        cost_basis = avg_cost or current_price
        contracts = shares // 100
        position_value = round(current_price * shares, 2)
        total_cost = round(cost_basis * shares, 2)
        unrealized_pnl = round((current_price - cost_basis) * shares, 2)

        # ── Fetch options chain (wide DTE range for protection analysis) ──
        # ASX stocks: skip options — yfinance has no ASX options data
        chain_df = pd.DataFrame()
        cc_candidates = pd.DataFrame()
        iv_rank_pct = 0.0
        hv30 = 0.0
        vrop = 0.0
        current_iv = 0.0
        if not is_asx:
            chain_df = self.fetcher.get_options_chain(symbol, min_dte=20, max_dte=60)

            # Extract ATM implied volatility from the live options chain so
            # get_iv_rank uses real IV (not just HV as proxy)
            atm_iv = None
            if not chain_df.empty and 'impliedVolatility' in chain_df.columns:
                try:
                    chain_df['strike_dist'] = (chain_df['strike'] - current_price).abs()
                    atm_row = chain_df.sort_values('strike_dist').iloc[0]
                    raw_iv = atm_row.get('impliedVolatility', 0)
                    if raw_iv and raw_iv > 0:
                        atm_iv = round(float(raw_iv) * 100, 2)  # yfinance returns 0-1 decimal
                except Exception:
                    pass

            iv_data = self.fetcher.get_iv_rank(symbol, current_iv=atm_iv)
            iv_rank_pct = iv_data['iv_rank']   # already 0-100
            hv30       = iv_data['hv30']
            vrop       = iv_data['vrop']
            current_iv = iv_data['current_iv']

            cc_candidates = self.screener.screen_covered_call_candidates(
                symbol, shares, avg_cost=cost_basis
            )

        # ── Earnings date fetch (F4 — Earnings Blackout Filter) ──────────────
        earnings_date = None
        earnings_days_away = None
        if not is_asx:
            try:
                import yfinance as yf
                tk = yf.Ticker(symbol)
                cal = tk.calendar
                # calendar can be a dict or DataFrame depending on yfinance version
                if isinstance(cal, dict):
                    ed = cal.get('Earnings Date')
                    if ed:
                        if isinstance(ed, list):
                            ed = ed[0]
                        if hasattr(ed, 'date'):
                            ed = ed.date()
                        earnings_date = str(ed)
                elif hasattr(cal, 'columns') and 'Earnings Date' in cal.columns:
                    ed_col = cal['Earnings Date'].dropna()
                    if not ed_col.empty:
                        ed = ed_col.iloc[0]
                        if hasattr(ed, 'date'):
                            ed = ed.date()
                        earnings_date = str(ed)
                if earnings_date:
                    from datetime import date as _date
                    ed_parsed = _date.fromisoformat(earnings_date)
                    earnings_days_away = (ed_parsed - _date.today()).days
                    if earnings_days_away < 0:
                        earnings_date = None   # past earnings, clear it
                        earnings_days_away = None
            except Exception:
                pass  # Silently skip if earnings data unavailable

        # ── Ex-dividend date fetch ────────────────────────────────────────────
        exdiv_date = None
        exdiv_days_away = None
        dividend_amount = 0.0
        if not is_asx:
            try:
                import datetime as _datetime
                tk_info = tk.info
                exdiv_ts = tk_info.get('exDividendDate')
                dividend_amount = float(
                    tk_info.get('lastDividendValue') or
                    tk_info.get('dividendRate', 0) or 0
                ) / 4   # quarterly estimate
                if exdiv_ts:
                    exdiv_d = _datetime.datetime.fromtimestamp(exdiv_ts).date()
                    exdiv_days_away = (exdiv_d - _datetime.date.today()).days
                    if exdiv_days_away >= 0:
                        exdiv_date = str(exdiv_d)
                    else:
                        exdiv_days_away = None   # already passed
            except Exception:
                pass

        recommendation = {
            'symbol': symbol,
            'is_asx': is_asx,
            'current_price': current_price,
            'cost_basis': cost_basis,
            'shares_held': shares,
            'contracts_available': contracts,
            'iv_rank': iv_rank_pct,
            'hv30': hv30,
            'vrop': vrop,
            'current_iv': current_iv,
            'position_value': position_value,
            'total_cost': total_cost,
            'unrealized_pnl': unrealized_pnl,
            'action': '',
            'reasoning': '',
            'top_covered_calls': [],
            'risk_notes': [],
            # Earnings blackout data (F4)
            'earnings_date': earnings_date,
            'earnings_days_away': earnings_days_away,
            # Ex-dividend data
            'exdiv_date': exdiv_date,
            'exdiv_days_away': exdiv_days_away,
            'dividend_amount': round(dividend_amount, 4),
            # New sections
            'risk_analysis': {},
            'protective_strategies': [],
            'collar_strategies': [],
            'cost_basis_roadmap': {},
            'market_structure': {},
        }

        # ── CC recommendation ──────────────────────────────────────────────
        if is_asx:
            recommendation['action'] = f"HOLD — Options analysis not available for ASX stocks ({symbol})"
            recommendation['reasoning'] = (
                "yfinance does not provide ASX options chains. "
                "Position value and downside risk are still tracked below. "
                "Consider using a local broker platform (e.g. CommSec, SelfWealth) to check options availability."
            )
        elif not cc_candidates.empty:
            top_3 = cc_candidates.head(3).to_dict('records')
            recommendation['top_covered_calls'] = top_3
            best = top_3[0]
            iv_rank_val = iv_rank_pct / 100
            recommendation['action'] = (
                f"SELL {contracts}x {symbol} ${best['strike']} Call "
                f"exp {best['expiry']} for ~${best['mid_price']:.2f}/contract"
            )
            recommendation['reasoning'] = (
                f"IV Rank {iv_rank_pct:.0f}% — "
                f"{'elevated, good premium opportunity' if iv_rank_val > 0.4 else 'moderate environment'}. "
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
            scenarios.append({
                'drop_pct': int(drop_pct),
                'target_price': float(target_price),
                'dollar_loss_from_now': float(pnl_vs_current),
                'pnl_vs_cost_basis': float(pnl_vs_basis),
                'pct_of_capital': float(pct_of_cost),
                'below_cost_basis': bool(target_price < cost_basis),
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
                    # Prefer live mid_price, fall back to lastPrice (best outside market hours)
                    mid = float(row.get('mid_price') or 0)
                    if mid <= 0:
                        mid = (float(row.get('bid') or 0) + float(row.get('ask') or 0)) / 2
                    if mid <= 0:
                        mid = float(row.get('lastPrice') or 0)
                    if mid <= 0:
                        continue  # skip if genuinely no price data
                    total_cost_puts = round(mid * contracts * 100, 2)
                    effective_floor = round(float(row['strike']) - mid, 2)
                    floor_vs_basis = round((effective_floor - cost_basis) / cost_basis * 100, 1)
                    # Annualised cost of protection
                    dte = int(row.get('dte', 30))
                    ann_cost_pct = round((mid / current_price) * (365 / max(dte, 1)) * 100, 2)
                    cost_as_pct_of_position = round(total_cost_puts / position_value * 100, 1)
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
                        'cost_pct_of_position': cost_as_pct_of_position,
                        'verdict': (
                            f'Good value — only {cost_as_pct_of_position}% of position for {dte}d cover'
                            if cost_as_pct_of_position < 2
                            else f'Reasonable — {cost_as_pct_of_position}% of position for {dte}d cover'
                            if cost_as_pct_of_position < 4
                            else f'Costly — {cost_as_pct_of_position}% of position for {dte}d cover'
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
                    put_cost = float(p_row.get('mid_price') or 0)
                    if put_cost <= 0:
                        put_cost = (float(p_row.get('bid') or 0) + float(p_row.get('ask') or 0)) / 2
                    if put_cost <= 0:
                        put_cost = float(p_row.get('lastPrice') or 0)
                    cc_premium = float(cc.get('mid_price') or 0)
                    if cc_premium <= 0:
                        cc_premium = float(cc.get('lastPrice') or 0)
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
        earnings_note = (
            f"Never sell options through earnings — check next earnings date before placing any trade"
            if not is_asx else
            "Check next earnings date before making portfolio decisions."
        )
        recommendation['risk_notes'] = [
            f"{symbol} 30-day historical vol: {hv*100:.0f}% — "
            f"a typical monthly move is ±${one_sigma_move:.0f} (1 standard deviation)",
            f"Position value: ${position_value:,.0f} | Cost basis: ${total_cost:,.0f} | "
            f"Unrealized P&L: ${unrealized_pnl:+,.0f}",
            f"52-week range: ${key_levels['52w_low']:.2f} – ${info.get('high_52w', current_price):.2f} "
            f"(currently {info.get('pct_from_high', 0):.1f}% off high)",
            earnings_note,
        ]
        if current_price < cost_basis:
            recommendation['risk_notes'].insert(0,
                f"⚠️ {symbol} is ${cost_basis - current_price:.2f} BELOW cost basis. "
                + ("Selling CCs below basis locks in loss if called away. "
                   "Consider a collar for protection while waiting for recovery."
                   if not is_asx else
                   "Consider averaging down or setting a stop-loss.")
            )
        elif (current_price - cost_basis) / cost_basis < 0.03:
            recommendation['risk_notes'].insert(0,
                "⚠️ Near break-even — a 10% drop turns this into a meaningful loss. "
                "Review the downside scenarios below."
            )

        # ── Open Interest / Market Structure analysis (US stocks only) ────────
        if not is_asx:
            try:
                oi_structure = analyze_oi_structure(
                    symbol=symbol,
                    fetcher=self.fetcher,
                    current_price=current_price,
                    hv_30=info.get('hv_30', 0.50),
                    max_dte=90,
                )
                recommendation['market_structure'] = oi_structure

                # Upgrade CC recommendation if price is near/below gamma flip (amplified regime)
                gamma_flip = oi_structure.get('gamma_flip')
                if gamma_flip and oi_structure.get('price_relative_to_flip') == 'below':
                    # In negative GEX regime — downside moves are amplified
                    if not recommendation['action'].startswith('BUY PROTECTION'):
                        recommendation['risk_notes'].insert(0,
                            f"⚠️ {symbol} is below the gamma flip level (~${gamma_flip:.0f}). "
                            "Dealer gamma is negative — price moves may be exaggerated. "
                            "Protective puts or a collar are advisable before adding short premium."
                        )
            except Exception as e:
                recommendation['market_structure'] = {
                    'coaching': f"Market structure analysis temporarily unavailable ({e})",
                    'put_wall': None,
                    'call_wall': None,
                    'gamma_flip': None,
                    'gex_positive': True,
                    'price_relative_to_flip': 'unknown',
                    'range_low': None,
                    'range_high': None,
                    'top_put_strikes': [],
                    'top_call_strikes': [],
                    'oi_levels': [],
                    'total_gex': 0,
                }

        # ── Upgrade HOLD to an actionable protection recommendation ───────────
        # When: action is HOLD (no CC income) AND position is at/near break-even
        # AND real protective put prices are available → tell the user what to do
        near_breakeven = (current_price - cost_basis) / cost_basis < 0.05
        if recommendation['action'].startswith('HOLD') and near_breakeven and protective_puts:
            # Find the cheapest meaningful floor (10-15% protection)
            best_put = next(
                (p for p in protective_puts if p['floor_pct'] in (10, 15)),
                protective_puts[0]
            )
            ann_cost = best_put['annualised_cost_pct']
            cost_total = best_put['total_cost']
            floor_price = best_put['effective_floor']
            strike = best_put['strike']
            expiry = best_put['expiry']
            recommendation['action'] = (
                f"BUY PROTECTION — low IV makes puts cheaper right now"
            )
            recommendation['reasoning'] = (
                f"IV is low (rank {recommendation.get('iv_rank', '—')}%) so CC income is thin — "
                f"but that same low IV makes protective puts less expensive to buy. "
                f"With {symbol} at break-even and every downside scenario cutting into capital, "
                f"the priority is protecting the position. "
                f"Consider buying the ${strike} put (exp {expiry}) for ~${best_put['mid_price']:.2f}/contract "
                f"(~${cost_total:,.0f} total for {contracts} contracts). "
                f"This floors your downside at ~${floor_price:.2f} — costing ~{ann_cost:.1f}%/yr. "
                f"When IV spikes back up, you can layer in a CC to turn this into a zero-cost collar."
            )

        # ── Next Best Action intelligence synthesis ────────────────────────────
        # Merge any caller-supplied context (e.g. wheel_cycle from the ledger)
        # BEFORE calling next_best_action so _score_signals() can see it.
        if extra_data:
            recommendation.update(extra_data)

        gemini_key = getattr(self, '_gemini_key', None)
        try:
            recommendation['next_best_action'] = next_best_action(
                recommendation, gemini_key=gemini_key
            )
        except Exception as e:
            recommendation['next_best_action'] = {
                'action_type': 'ERROR',
                'confidence': 0,
                'headline': 'Intelligence engine error',
                'icon': '⚠️',
                'color': 'grey',
                'signals': [],
                'reasoning': str(e),
                'education': '',
                'specific_trade': {},
                'score_breakdown': {},
            }

        return recommendation

    def recommend_rddt_action(self, shares: int = 200, avg_cost: float = None) -> Dict:
        """Backward-compatibility alias — delegates to recommend_action('RDDT')."""
        return self.recommend_action('RDDT', shares=shares, avg_cost=avg_cost)

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
