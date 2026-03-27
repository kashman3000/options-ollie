"""
Options Ollie — Options Screener
Scans the watchlist and ranks candidates by strategy suitability.
"""

import pandas as pd
import numpy as np
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
from scipy.stats import norm as _norm
from .fetcher import OptionsDataFetcher
from ..config import RiskProfile, FULL_WATCHLIST


# ── Probability helpers ───────────────────────────────────────────────────────

def _iv_rank_label(iv_rank: float) -> str:
    """
    Human-readable IV environment label with trading implication.
    iv_rank is a 0-1 float (e.g. 0.28 = 28th percentile).
    """
    if iv_rank < 0.20:
        return "Low IV — thin premiums, probabilities may understate real risk"
    elif iv_rank < 0.40:
        return "Below-avg IV — moderate premiums, use wider stops"
    elif iv_rank < 0.60:
        return "Average IV — solid probability estimates, fair premiums"
    elif iv_rank < 0.80:
        return "Elevated IV — rich premiums, high-probability setups"
    else:
        return "High IV — exceptional premium, consider mean reversion"


def _iv_rank_tier(iv_rank: float) -> str:
    """Short single-word tier for badges / column values."""
    if iv_rank < 0.20:   return "Low"
    elif iv_rank < 0.40: return "Below Avg"
    elif iv_rank < 0.60: return "Average"
    elif iv_rank < 0.80: return "Elevated"
    else:                return "High"


def _calc_pop_cc(S: float, K: float, premium: float, T: float, r: float, sigma: float) -> float:
    """
    Prob of Profit for a short call (covered call).
    You profit as long as the stock stays below the breakeven = K + premium at expiry.
    POP = N(-d2) evaluated at the breakeven strike.
    """
    K_be = K + premium
    if K_be <= 0 or T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    try:
        d2 = (np.log(S / K_be) + (r - 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        return float(round(_norm.cdf(-d2), 4))   # prob stock stays below K_be
    except Exception:
        return 0.0


def _calc_pop_csp(S: float, K: float, premium: float, T: float, r: float, sigma: float) -> float:
    """
    Prob of Profit for a short put (cash-secured put).
    You profit as long as the stock stays above the breakeven = K - premium at expiry.
    POP = N(d2) evaluated at the breakeven strike.
    """
    K_be = K - premium
    if K_be <= 0 or T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    try:
        d2 = (np.log(S / K_be) + (r - 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        return float(round(_norm.cdf(d2), 4))    # prob stock stays above K_be
    except Exception:
        return 0.0


class OptionsScreener:
    """Screens options universe for high-probability trade setups."""

    def __init__(self, fetcher: OptionsDataFetcher, risk: RiskProfile):
        self.fetcher = fetcher
        self.risk = risk

    def screen_wheel_candidates(self, symbols: List[str] = None,
                                 max_stock_price: float = 200.0) -> pd.DataFrame:
        """
        Find best cash-secured put candidates for the wheel strategy.

        Criteria:
        - Stock price affordable for 100-share lots
        - High IV rank (selling premium when IV is elevated)
        - 70%+ probability OTM at target delta
        - Good liquidity (volume, open interest, tight spreads)
        - Avoid earnings within DTE window
        """
        symbols = symbols or FULL_WATCHLIST
        candidates = []

        for sym in symbols:
            try:
                info = self.fetcher.get_stock_info(sym)
                if not info or info['price'] > max_stock_price or info['price'] < 5:
                    continue

                # ── Earnings blackout check ───────────────────────────────────
                # Never recommend selling premium through an earnings event —
                # IV collapses after the print and the move risk is undefined.
                earnings_days = None
                try:
                    import yfinance as _yf
                    from datetime import date as _date
                    _tk = _yf.Ticker(sym)
                    _cal = _tk.calendar
                    _ed = None
                    if isinstance(_cal, dict):
                        _ed = _cal.get('Earnings Date')
                        if isinstance(_ed, list): _ed = _ed[0]
                        if hasattr(_ed, 'date'): _ed = _ed.date()
                    elif hasattr(_cal, 'columns') and 'Earnings Date' in _cal.columns:
                        _col = _cal['Earnings Date'].dropna()
                        if not _col.empty:
                            _ed = _col.iloc[0]
                            if hasattr(_ed, 'date'): _ed = _ed.date()
                    if _ed:
                        earnings_days = (_date.fromisoformat(str(_ed)) - _date.today()).days
                        if earnings_days < 0:
                            earnings_days = None  # past earnings, ignore
                except Exception:
                    pass

                # Hard block: earnings within 21 days = skip entirely
                if earnings_days is not None and earnings_days <= 21:
                    continue

                # ── Ex-dividend check ─────────────────────────────────────────
                # If ex-div falls within the option DTE window, the stock will
                # drop by ~dividend amount on that date — POP is overstated.
                # We surface this as a warning and adjust expected value downward.
                exdiv_days = None
                dividend_amount = 0.0
                try:
                    _info = _tk.info
                    _exdiv_ts = _info.get('exDividendDate')
                    dividend_amount = float(_info.get('lastDividendValue') or
                                            _info.get('dividendRate', 0) or 0) / 4  # quarterly
                    if _exdiv_ts:
                        import datetime as _datetime
                        _exdiv_date = _datetime.datetime.fromtimestamp(_exdiv_ts).date()
                        exdiv_days = (_exdiv_date - _date.today()).days
                        if exdiv_days < 0:
                            exdiv_days = None  # already passed
                except Exception:
                    pass

                chain = self.fetcher.get_options_chain(
                    sym,
                    min_dte=self.risk.min_days_to_expiry,
                    max_dte=self.risk.max_days_to_expiry
                )

                if chain.empty:
                    continue

                # Build best_price: prefer live mid, fall back to lastPrice (after-hours safe)
                chain = chain.copy()
                chain['best_price'] = chain['mid_price'].where(chain['mid_price'] > 0, chain['lastPrice'])

                # Extract ATM implied volatility from the live chain so get_iv_rank
                # uses real IV (not HV proxy) for a meaningful VRP calculation.
                atm_iv = None
                if 'impliedVolatility' in chain.columns:
                    try:
                        chain['_strike_dist'] = (chain['strike'] - info['price']).abs()
                        atm_row = chain.sort_values('_strike_dist').iloc[0]
                        raw_iv  = atm_row.get('impliedVolatility', 0)
                        if raw_iv and float(raw_iv) > 0:
                            atm_iv = round(float(raw_iv) * 100, 2)  # yfinance returns 0-1
                    except Exception:
                        pass

                iv_data    = self.fetcher.get_iv_rank(sym, current_iv=atm_iv)
                iv_rank    = iv_data['iv_rank'] / 100.0   # 0-1 for internal calcs
                vrop       = iv_data.get('vrop', 0) or 0
                hv30       = iv_data.get('hv30', 0) or 0
                current_iv = iv_data.get('current_iv', 0) or 0

                # Hard gate: if VRP is meaningfully negative, selling premium has no
                # statistical edge here — skip rather than pollute the recommendations.
                if vrop < -5:
                    continue

                # Filter to OTM puts only
                puts = chain[
                    (chain['option_type'] == 'put') &
                    (chain['strike'] < info['price']) &
                    (chain['openInterest'] >= self.risk.min_open_interest) &
                    (chain['volume'] >= self.risk.min_volume) &
                    (chain['prob_otm'] >= self.risk.min_probability_of_profit) &
                    (abs(chain['delta_est']) <= self.risk.target_delta_csp + 0.10) &
                    (abs(chain['delta_est']) >= self.risk.target_delta_csp - 0.10) &
                    (chain['best_price'] > 0.05)
                ].copy()

                if puts.empty:
                    continue

                r_val = 0.05
                for _, put in puts.iterrows():
                    best_px = float(put['best_price'])
                    capital_required = put['strike'] * 100
                    premium = best_px * 100
                    annualized_return = (premium / capital_required) * (365 / put['dte'])
                    T = float(put['dte']) / 365.0
                    sigma = float(put.get('iv_used', 0.50) or 0.50)

                    pop = _calc_pop_csp(
                        S=float(info['price']),
                        K=float(put['strike']),
                        premium=best_px,
                        T=T, r=r_val, sigma=sigma
                    )
                    prob_touch = float(put.get('prob_touch', min(2 * float(put.get('prob_itm', 0)), 0.99)))
                    delta_abs  = float(abs(put['delta_est']))

                    # ── Ollie Score: 6-dimension professional framework ──────────
                    #
                    # Based on tastytrade / professional theta-selling criteria:
                    #   1. IV Quality   (25%) — Are we selling when vol is actually elevated?
                    #   2. Expected EV  (25%) — Risk-adjusted annualised return (ann_ret × POP)
                    #   3. Prob Profit  (20%) — Statistical edge at the chosen strike
                    #   4. DTE Quality  (15%) — How close to the 30-45d theta sweet spot
                    #   5. Liquidity    (10%) — Tight spreads + sufficient open interest
                    #   6. Delta        ( 5%) — Strike selection quality (0.20-0.30 ideal)

                    # 1. IV Quality: rank × VRP multiplier
                    vrp_mult = 1.2 if vrop >= 15 else (1.0 if vrop >= 5 else (0.8 if vrop >= 0 else 0.6))
                    s_iv = min(iv_rank * vrp_mult, 1.0)

                    # 2. Expected EV: annualised return weighted by POP
                    # Adjust POP downward if ex-div falls within this option's DTE:
                    # the stock will predictably drop ~dividend amount on ex-date,
                    # nudging it closer to (or through) the put strike.
                    exdiv_in_window = (exdiv_days is not None and 0 < exdiv_days <= dte)
                    pop_adj = pop
                    if exdiv_in_window and dividend_amount > 0:
                        strike_val = float(put['strike'])
                        breakeven = strike_val - float(best_px)
                        # Rough adjustment: if stock drops by dividend, new effective
                        # distance to breakeven shrinks — reduce POP proportionally
                        price_val = float(info['price'])
                        adj_price = price_val - dividend_amount
                        shortfall = max(0.0, breakeven - adj_price)
                        pop_adj = max(0.0, pop - shortfall / price_val)

                    ev = annualized_return * pop_adj
                    s_ev = min(ev / 0.25, 1.0)

                    # 3. Probability of profit (direct 0-1)
                    s_pop = float(pop_adj)

                    # 4. DTE quality — 30-45d is the theta sweet spot
                    dte = int(put['dte'])
                    if 30 <= dte <= 45:
                        s_dte = 1.0
                    elif 21 <= dte < 30:
                        s_dte = 0.80
                    elif 45 < dte <= 60:
                        s_dte = 0.70
                    elif dte < 21:
                        s_dte = 0.40
                    else:
                        s_dte = 0.50   # > 60 DTE

                    # 5. Liquidity: bid-ask spread quality + OI depth
                    bap = float(put.get('bid_ask_pct', 0.20) or 0.20)
                    s_spread = max(0.0, 1.0 - bap * 5)   # 0% spread = 1.0; 20% spread = 0.0
                    s_oi = min(float(put['openInterest']) / 2000.0, 1.0)
                    s_liq = 0.65 * s_spread + 0.35 * s_oi

                    # 6. Delta quality: tastytrade sweet spot 0.20-0.30
                    if 0.20 <= delta_abs <= 0.30:
                        s_delta = 1.0
                    elif 0.15 <= delta_abs < 0.20 or 0.30 < delta_abs <= 0.35:
                        s_delta = 0.75
                    else:
                        s_delta = 0.50

                    # Weighted composite
                    score = (
                        0.25 * s_iv  +
                        0.25 * s_ev  +
                        0.20 * s_pop +
                        0.15 * s_dte +
                        0.10 * s_liq +
                        0.05 * s_delta
                    )

                    candidates.append({
                        'symbol': sym,
                        'name': info['name'],
                        'stock_price': info['price'],
                        'strike': put['strike'],
                        'expiry': put['expiry'],
                        'dte': dte,
                        'bid': put['bid'],
                        'ask': put['ask'],
                        'mid_price': put['mid_price'],
                        'premium_100': round(premium, 2),
                        'capital_required': round(capital_required, 2),
                        'annualized_return': round(annualized_return * 100, 2),
                        'delta': round(delta_abs, 4),
                        'prob_otm': round(float(put['prob_otm']) * 100, 2),
                        'pop': round(pop * 100, 1),
                        'prob_touch': round(prob_touch * 100, 1),
                        'iv_rank': round(iv_rank * 100, 2),
                        'iv_rank_tier': _iv_rank_tier(iv_rank),
                        'iv_rank_label': _iv_rank_label(iv_rank),
                        'vrop': round(vrop, 1),
                        'hv30': round(hv30, 1),
                        'current_iv': round(current_iv, 1),
                        'open_interest': int(put['openInterest']),
                        'volume': int(put['volume']),
                        'bid_ask_pct': round(bap * 100, 2),
                        # Ollie Score breakdown (each 0-100 for display)
                        'score': round(score, 4),
                        's_iv':    round(s_iv    * 100, 1),
                        's_ev':    round(s_ev    * 100, 1),
                        's_pop':   round(s_pop   * 100, 1),
                        's_dte':   round(s_dte   * 100, 1),
                        's_liq':   round(s_liq   * 100, 1),
                        's_delta': round(s_delta * 100, 1),
                        'strategy': 'CSP',
                        'sector': info.get('sector', 'N/A'),
                        'earnings_days': earnings_days,
                        'exdiv_days': exdiv_days,
                        'dividend_amount': round(dividend_amount, 4),
                        'exdiv_in_window': exdiv_in_window,
                    })

            except Exception as e:
                continue

        if not candidates:
            return pd.DataFrame()

        df = pd.DataFrame(candidates)
        df = df.sort_values('score', ascending=False).reset_index(drop=True)
        return df

    def screen_covered_call_candidates(self, symbol: str, shares: int,
                                        avg_cost: float = None) -> pd.DataFrame:
        """
        Find best covered call strikes for shares you already own.
        For the wheel: sell calls above your cost basis.
        Includes POP (Prob of Profit), Prob of Touch, and IV rank context label.
        """
        info = self.fetcher.get_stock_info(symbol)
        if not info:
            return pd.DataFrame()

        iv_rank = self.fetcher.get_iv_rank(symbol)['iv_rank'] / 100.0
        cost_basis = avg_cost or info['price']
        chain = self.fetcher.get_options_chain(
            symbol,
            min_dte=self.risk.min_days_to_expiry,
            max_dte=self.risk.max_days_to_expiry
        )

        if chain.empty:
            return pd.DataFrame()

        num_contracts = shares // 100

        # Build best_price: prefer live mid, fall back to lastPrice (after-hours safe)
        chain = chain.copy()
        chain['best_price'] = chain['mid_price'].where(chain['mid_price'] > 0, chain['lastPrice'])

        # Filter OTM calls above cost basis
        calls = chain[
            (chain['option_type'] == 'call') &
            (chain['strike'] > cost_basis) &
            (chain['openInterest'] >= self.risk.min_open_interest // 2) &
            (chain['best_price'] > 0.10) &
            (abs(chain['delta_est']) <= self.risk.target_delta_cc + 0.10) &
            (abs(chain['delta_est']) >= max(self.risk.target_delta_cc - 0.10, 0.05))
        ].copy()

        if calls.empty:
            # Fallback: relax delta filter, keep price quality check
            calls = chain[
                (chain['option_type'] == 'call') &
                (chain['strike'] > info['price']) &
                (chain['best_price'] > 0.05)
            ].copy()

        if calls.empty:
            return pd.DataFrame()

        r_val = 0.05  # risk-free rate
        results = []
        for _, call in calls.iterrows():
            best_px = float(call['best_price'])
            premium = best_px * 100 * num_contracts
            upside_to_strike = (call['strike'] - info['price']) / info['price'] * 100
            total_return = (best_px + call['strike'] - info['price']) / info['price']
            annualized = total_return * (365 / call['dte'])
            T = float(call['dte']) / 365.0
            sigma = float(call.get('iv_used', 0.50) or 0.50)

            # Prob of Profit: prob stock stays below breakeven (strike + premium) at expiry
            pop = _calc_pop_cc(
                S=float(info['price']),
                K=float(call['strike']),
                premium=best_px,
                T=T, r=r_val, sigma=sigma
            )

            # Prob of Touch: ~2× Prob ITM (from enriched greeks, already computed)
            prob_touch = float(call.get('prob_touch', min(2 * float(call.get('prob_itm', 0)), 0.99)))

            results.append({
                'symbol': symbol,
                'stock_price': info['price'],
                'cost_basis': cost_basis,
                'strike': call['strike'],
                'expiry': call['expiry'],
                'dte': call['dte'],
                'bid': call['bid'],
                'ask': call['ask'],
                'mid_price': round(best_px, 2),
                'contracts': num_contracts,
                'total_premium': round(premium, 2),
                'upside_to_strike_pct': round(upside_to_strike, 2),
                'annualized_if_called': round(annualized * 100, 2),
                'delta': round(abs(float(call['delta_est'])), 4),
                'prob_otm': round(float(call.get('prob_otm', 0)) * 100, 2),
                'pop': round(pop * 100, 1),                           # Prob of Profit %
                'prob_touch': round(prob_touch * 100, 1),             # Prob of Touch %
                'iv_rank': round(iv_rank * 100, 1),
                'iv_rank_tier': _iv_rank_tier(iv_rank),               # Low/Avg/Elevated/High
                'iv_rank_label': _iv_rank_label(iv_rank),             # Full context sentence
                'open_interest': int(call['openInterest']),
                'strategy': 'CC',
            })

        df = pd.DataFrame(results)
        df = df.sort_values('total_premium', ascending=False).reset_index(drop=True)
        return df

    def screen_iron_condors(self, symbols: List[str] = None,
                            min_credit: float = 0.50) -> pd.DataFrame:
        """
        Find iron condor opportunities: sell OTM put spread + OTM call spread.
        Best on range-bound, high-IV underlyings.
        """
        symbols = symbols or ['SPY', 'QQQ', 'IWM', 'AAPL', 'MSFT', 'AMZN', 'GOOGL', 'META']
        candidates = []

        for sym in symbols:
            try:
                info = self.fetcher.get_stock_info(sym)
                if not info:
                    continue

                iv_rank = self.fetcher.get_iv_rank(sym)['iv_rank'] / 100.0
                if iv_rank < 0.30:  # Only sell condors when IV is elevated
                    continue

                chain = self.fetcher.get_options_chain(
                    sym,
                    min_dte=self.risk.min_days_to_expiry,
                    max_dte=self.risk.max_days_to_expiry
                )
                if chain.empty:
                    continue

                price = info['price']
                expiries = chain['expiry'].unique()

                for exp in expiries:
                    exp_chain = chain[chain['expiry'] == exp]
                    puts = exp_chain[exp_chain['option_type'] == 'put'].sort_values('strike')
                    calls = exp_chain[exp_chain['option_type'] == 'call'].sort_values('strike')

                    if puts.empty or calls.empty:
                        continue

                    # Short put: ~15-20 delta (higher prob OTM)
                    short_puts = puts[
                        (abs(puts['delta_est']) >= 0.12) &
                        (abs(puts['delta_est']) <= 0.22) &
                        (puts['bid'] > 0.10)
                    ]

                    # Short call: ~15-20 delta
                    short_calls = calls[
                        (abs(calls['delta_est']) >= 0.12) &
                        (abs(calls['delta_est']) <= 0.22) &
                        (calls['bid'] > 0.10)
                    ]

                    if short_puts.empty or short_calls.empty:
                        continue

                    # Pick the best short strikes
                    sp = short_puts.iloc[len(short_puts) // 2]  # Middle of range
                    sc = short_calls.iloc[len(short_calls) // 2]

                    # Long put: 5 delta below short put (wing)
                    wing_width = max(price * 0.03, 2.0)  # ~3% or $2 minimum
                    long_put_strike = sp['strike'] - wing_width
                    long_puts = puts[
                        (puts['strike'] <= long_put_strike + 1) &
                        (puts['strike'] >= long_put_strike - 1)
                    ]

                    # Long call: 5 delta above short call
                    long_call_strike = sc['strike'] + wing_width
                    long_calls = calls[
                        (calls['strike'] <= long_call_strike + 1) &
                        (calls['strike'] >= long_call_strike - 1)
                    ]

                    if long_puts.empty or long_calls.empty:
                        continue

                    lp = long_puts.iloc[-1]  # Closest to target
                    lc = long_calls.iloc[0]

                    # Calculate credit and risk
                    put_spread_credit = sp['mid_price'] - lp['mid_price']
                    call_spread_credit = sc['mid_price'] - lc['mid_price']
                    total_credit = put_spread_credit + call_spread_credit

                    if total_credit < min_credit:
                        continue

                    put_spread_width = sp['strike'] - lp['strike']
                    call_spread_width = lc['strike'] - sc['strike']
                    max_risk = max(put_spread_width, call_spread_width) * 100 - total_credit * 100

                    if max_risk <= 0:
                        continue

                    pop_estimate = min(sp.get('prob_otm', 0.8), sc.get('prob_otm', 0.8))
                    return_on_risk = total_credit * 100 / max_risk
                    annualized_ror = return_on_risk * (365 / sp['dte'])

                    candidates.append({
                        'symbol': sym,
                        'stock_price': price,
                        'expiry': exp,
                        'dte': int(sp['dte']),
                        'short_put': sp['strike'],
                        'long_put': lp['strike'],
                        'short_call': sc['strike'],
                        'long_call': lc['strike'],
                        'put_credit': round(put_spread_credit, 2),
                        'call_credit': round(call_spread_credit, 2),
                        'total_credit': round(total_credit, 2),
                        'total_credit_100': round(total_credit * 100, 2),
                        'max_risk': round(max_risk, 2),
                        'return_on_risk_pct': round(return_on_risk * 100, 2),
                        'annualized_ror_pct': round(annualized_ror * 100, 2),
                        'prob_profit_est': round(pop_estimate * 100, 2),
                        'iv_rank': round(iv_rank * 100, 2),
                        'strategy': 'IC',
                    })

            except Exception as e:
                continue

        if not candidates:
            return pd.DataFrame()

        df = pd.DataFrame(candidates)
        df = df.sort_values('return_on_risk_pct', ascending=False).reset_index(drop=True)
        return df

    def screen_credit_spreads(self, symbols: List[str] = None,
                               spread_type: str = 'put') -> pd.DataFrame:
        """
        Screen for bull put spreads (default) or bear call spreads.
        Simpler than iron condors — one directional bias.
        """
        symbols = symbols or FULL_WATCHLIST[:15]  # Top 15 for speed
        candidates = []

        for sym in symbols:
            try:
                info = self.fetcher.get_stock_info(sym)
                if not info:
                    continue

                iv_rank = self.fetcher.get_iv_rank(sym)['iv_rank'] / 100.0
                chain = self.fetcher.get_options_chain(
                    sym,
                    min_dte=self.risk.min_days_to_expiry,
                    max_dte=self.risk.max_days_to_expiry
                )
                if chain.empty:
                    continue

                price = info['price']
                opt_type = 'put' if spread_type == 'put' else 'call'
                options = chain[chain['option_type'] == opt_type].copy()

                if options.empty:
                    continue

                for exp in options['expiry'].unique():
                    exp_opts = options[options['expiry'] == exp].sort_values('strike')

                    if spread_type == 'put':
                        # Bull put spread: sell higher put, buy lower put
                        short_opts = exp_opts[
                            (abs(exp_opts['delta_est']) >= 0.15) &
                            (abs(exp_opts['delta_est']) <= 0.30) &
                            (exp_opts['bid'] > 0.10)
                        ]
                    else:
                        # Bear call spread: sell lower call, buy higher call
                        short_opts = exp_opts[
                            (abs(exp_opts['delta_est']) >= 0.15) &
                            (abs(exp_opts['delta_est']) <= 0.30) &
                            (exp_opts['bid'] > 0.10)
                        ]

                    if short_opts.empty:
                        continue

                    short = short_opts.iloc[len(short_opts) // 2]
                    width = max(price * 0.025, 1.0)

                    if spread_type == 'put':
                        long_strike = short['strike'] - width
                        long_opts = exp_opts[
                            (exp_opts['strike'] <= long_strike + 0.5) &
                            (exp_opts['strike'] >= long_strike - 0.5)
                        ]
                    else:
                        long_strike = short['strike'] + width
                        long_opts = exp_opts[
                            (exp_opts['strike'] <= long_strike + 0.5) &
                            (exp_opts['strike'] >= long_strike - 0.5)
                        ]

                    if long_opts.empty:
                        continue

                    long = long_opts.iloc[0]
                    credit = short['mid_price'] - long['mid_price']
                    spread_width = abs(short['strike'] - long['strike'])
                    max_risk = (spread_width - credit) * 100

                    if credit <= 0.10 or max_risk <= 0:
                        continue

                    ror = credit * 100 / max_risk

                    candidates.append({
                        'symbol': sym,
                        'stock_price': price,
                        'strategy': 'Bull Put Spread' if spread_type == 'put' else 'Bear Call Spread',
                        'expiry': exp,
                        'dte': int(short['dte']),
                        'short_strike': short['strike'],
                        'long_strike': long['strike'],
                        'credit': round(credit, 2),
                        'credit_100': round(credit * 100, 2),
                        'max_risk': round(max_risk, 2),
                        'return_on_risk_pct': round(ror * 100, 2),
                        'prob_otm': round(short.get('prob_otm', 0.75) * 100, 2),
                        'iv_rank': round(iv_rank * 100, 2),
                        'delta': round(abs(short['delta_est']), 4),
                    })

            except Exception:
                continue

        if not candidates:
            return pd.DataFrame()

        df = pd.DataFrame(candidates)
        df = df.sort_values('return_on_risk_pct', ascending=False).reset_index(drop=True)
        return df
