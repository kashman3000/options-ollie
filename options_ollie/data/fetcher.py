"""
Options Ollie — Data Fetching Layer
Abstracts data sources: yfinance now, MenthorQ when available.
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from scipy.stats import norm
import warnings
warnings.filterwarnings('ignore')


class OptionsDataFetcher:
    """Fetches stock and options data. Swap to MenthorQ by subclassing."""

    def __init__(self, source: str = 'yfinance'):
        self.source = source
        self._cache: Dict[str, any] = {}

    def get_stock_info(self, symbol: str) -> Dict:
        """Get current price, volume, market cap, IV rank, etc."""
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        hist = ticker.history(period='1y')

        if hist.empty:
            return {}

        current_price = hist['Close'].iloc[-1]
        avg_volume = hist['Volume'].mean()

        # Calculate historical volatility (annualized)
        returns = np.log(hist['Close'] / hist['Close'].shift(1)).dropna()
        hv_30 = returns.tail(30).std() * np.sqrt(252) if len(returns) >= 30 else returns.std() * np.sqrt(252)
        hv_60 = returns.tail(60).std() * np.sqrt(252) if len(returns) >= 60 else hv_30

        # 52-week range
        high_52w = hist['High'].max()
        low_52w = hist['Low'].min()
        pct_from_high = (current_price - high_52w) / high_52w * 100
        pct_from_low = (current_price - low_52w) / low_52w * 100

        return {
            'symbol': symbol,
            'price': round(current_price, 2),
            'market_cap': info.get('marketCap', 0),
            'avg_volume': int(avg_volume),
            'hv_30': round(hv_30, 4),
            'hv_60': round(hv_60, 4),
            'high_52w': round(high_52w, 2),
            'low_52w': round(low_52w, 2),
            'pct_from_high': round(pct_from_high, 2),
            'pct_from_low': round(pct_from_low, 2),
            'sector': info.get('sector', 'N/A'),
            'name': info.get('shortName', symbol),
            'earnings_date': str(info.get('earningsTimestamp', 'N/A')),
        }

    def get_options_chain(self, symbol: str, min_dte: int = 20, max_dte: int = 50) -> pd.DataFrame:
        """
        Get full options chain filtered by DTE range.
        Returns DataFrame with puts and calls, enriched with Greeks estimates.
        """
        ticker = yf.Ticker(symbol)
        try:
            expirations = ticker.options
        except Exception:
            return pd.DataFrame()

        if not expirations:
            return pd.DataFrame()

        today = datetime.now().date()
        all_chains = []

        for exp_str in expirations:
            exp_date = datetime.strptime(exp_str, '%Y-%m-%d').date()
            dte = (exp_date - today).days

            if dte < min_dte or dte > max_dte:
                continue

            try:
                chain = ticker.option_chain(exp_str)
            except Exception:
                continue

            for opt_type, df in [('call', chain.calls), ('put', chain.puts)]:
                if df.empty:
                    continue

                df = df.copy()
                df['option_type'] = opt_type
                df['expiry'] = exp_str
                df['dte'] = dte
                df['symbol'] = symbol

                # Get current stock price for moneyness calculation
                hist = ticker.history(period='1d')
                if not hist.empty:
                    stock_price = hist['Close'].iloc[-1]
                    df['stock_price'] = stock_price
                    df['moneyness'] = (df['strike'] - stock_price) / stock_price * 100

                    # Estimate missing Greeks using Black-Scholes if needed
                    df = self._enrich_greeks(df, stock_price, dte)

                all_chains.append(df)

        if not all_chains:
            return pd.DataFrame()

        result = pd.concat(all_chains, ignore_index=True)

        # Liquidity filters
        result['bid_ask_spread'] = result['ask'] - result['bid']
        result['bid_ask_pct'] = np.where(
            result['ask'] > 0,
            result['bid_ask_spread'] / result['ask'],
            1.0
        )
        result['mid_price'] = (result['bid'] + result['ask']) / 2

        return result

    @staticmethod
    def _bs_call_price(S, K, T, r, sigma):
        """Black-Scholes call price."""
        from scipy.stats import norm as _norm
        if T <= 0 or sigma <= 0:
            return max(S - K, 0)
        d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        return S * _norm.cdf(d1) - K * np.exp(-r * T) * _norm.cdf(d2)

    @staticmethod
    def _bs_put_price(S, K, T, r, sigma):
        """Black-Scholes put price."""
        from scipy.stats import norm as _norm
        if T <= 0 or sigma <= 0:
            return max(K - S, 0)
        d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        return K * np.exp(-r * T) * _norm.cdf(-d2) - S * _norm.cdf(-d1)

    @staticmethod
    def _implied_vol(price, S, K, T, r, opt_type='call', fallback=0.5):
        """Invert Black-Scholes via bisection to get real IV from market price."""
        if T <= 0 or price <= 0:
            return fallback
        lo, hi = 0.01, 5.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if opt_type == 'call':
                d1 = (np.log(S / K) + (r + 0.5 * mid**2) * T) / (mid * np.sqrt(T))
                d2 = d1 - mid * np.sqrt(T)
                from scipy.stats import norm as _norm
                theo = S * _norm.cdf(d1) - K * np.exp(-r * T) * _norm.cdf(d2)
            else:
                d1 = (np.log(S / K) + (r + 0.5 * mid**2) * T) / (mid * np.sqrt(T))
                d2 = d1 - mid * np.sqrt(T)
                from scipy.stats import norm as _norm
                theo = K * np.exp(-r * T) * _norm.cdf(-d2) - S * _norm.cdf(-d1)
            if abs(theo - price) < 0.001:
                return round(mid, 4)
            if theo < price:
                lo = mid
            else:
                hi = mid
        return round((lo + hi) / 2, 4)

    def _enrich_greeks(self, df: pd.DataFrame, stock_price: float, dte: int) -> pd.DataFrame:
        """Estimate Greeks using Black-Scholes. Derives IV from lastPrice when yfinance IV is unreliable."""
        T = dte / 365.0
        r = 0.05  # Risk-free rate approximation

        for idx, row in df.iterrows():
            K = row['strike']
            yf_sigma = float(row.get('impliedVolatility', 0) or 0)
            last_px = float(row.get('lastPrice', 0) or 0)
            opt_type = row.get('option_type', 'call')

            # Validate yfinance IV by checking if its theoretical price is within 50% of
            # the actual last traded price. yfinance commonly returns garbage IV (e.g. 0.03
            # or 0.12 for a stock with 60% HV) that produces wildly wrong delta/prob values.
            sigma = None
            if yf_sigma > 0.05 and T > 0 and last_px > 0:
                if opt_type == 'call':
                    theo = self._bs_call_price(stock_price, K, T, r, yf_sigma)
                else:
                    theo = self._bs_put_price(stock_price, K, T, r, yf_sigma)
                if theo > 0 and abs(theo - last_px) / last_px < 0.50:
                    sigma = yf_sigma  # yfinance IV matches market price — use it

            if sigma is None and last_px > 0 and T > 0:
                sigma = self._implied_vol(last_px, stock_price, K, T, r, opt_type)

            if sigma is None or sigma <= 0:
                sigma = 0.50  # last resort

            try:
                d1 = (np.log(stock_price / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
                d2 = d1 - sigma * np.sqrt(T)

                if row['option_type'] == 'call':
                    delta = norm.cdf(d1)
                    prob_itm = norm.cdf(d2)
                else:
                    delta = norm.cdf(d1) - 1
                    prob_itm = norm.cdf(-d2)

                gamma = norm.pdf(d1) / (stock_price * sigma * np.sqrt(T))
                theta = -(stock_price * norm.pdf(d1) * sigma) / (2 * np.sqrt(T)) / 365
                vega = stock_price * norm.pdf(d1) * np.sqrt(T) / 100

                prob_itm_val = round(prob_itm, 4)
                prob_otm_val = round(1 - prob_itm, 4)

                # Prob of Touch ≈ 2 × Prob ITM (tastytrade / TastyWorks convention).
                # Intuition: a 30% Prob ITM option has roughly a 60% chance of touching
                # the strike *at some point* before expiry even if it ultimately expires OTM.
                # Used primarily for stop-loss trigger decisions.
                prob_touch_val = round(min(2 * prob_itm_val, 0.99), 4)

                df.at[idx, 'delta_est'] = round(delta, 4)
                df.at[idx, 'gamma_est'] = round(gamma, 6)
                df.at[idx, 'theta_est'] = round(theta, 4)
                df.at[idx, 'vega_est'] = round(vega, 4)
                df.at[idx, 'prob_itm'] = prob_itm_val
                df.at[idx, 'prob_otm'] = prob_otm_val
                df.at[idx, 'prob_touch'] = prob_touch_val
                df.at[idx, 'iv_used'] = round(sigma, 4)  # store back-solved IV for POP calcs
            except (ZeroDivisionError, ValueError):
                df.at[idx, 'delta_est'] = 0
                df.at[idx, 'gamma_est'] = 0
                df.at[idx, 'theta_est'] = 0
                df.at[idx, 'vega_est'] = 0
                df.at[idx, 'prob_itm'] = 0
                df.at[idx, 'prob_otm'] = 1
                df.at[idx, 'prob_touch'] = 0
                df.at[idx, 'iv_used'] = sigma if sigma else 0.50

        return df

    def get_earnings_calendar(self, symbols: List[str]) -> Dict[str, Optional[str]]:
        """Check upcoming earnings dates — avoid selling options through earnings."""
        earnings = {}
        for sym in symbols:
            try:
                ticker = yf.Ticker(sym)
                cal = ticker.calendar
                if cal is not None and not (isinstance(cal, pd.DataFrame) and cal.empty):
                    if isinstance(cal, dict):
                        ed = cal.get('Earnings Date', [None])
                        earnings[sym] = str(ed[0]) if ed else None
                    else:
                        earnings[sym] = None
                else:
                    earnings[sym] = None
            except Exception:
                earnings[sym] = None
        return earnings

    def get_iv_rank(self, symbol: str, lookback_days: int = 63,
                    current_iv: float = None) -> dict:
        """
        Calculate IV Rank over a 3-month window and Volatility Risk Premium.

        Uses real ATM implied volatility (current_iv) when provided by the caller
        (extracted from the live options chain). Falls back to current HV30 when
        no live IV is available — but still uses the shorter 63-day window so
        post-IPO volatility spikes don't inflate the range.

        Returns a dict:
          iv_rank   — 0-100 percentile of current IV in its 63-day range
          hv30      — current 30-day historical volatility (annualised, 0-100)
          current_iv — the IV used for ranking (real ATM IV or HV proxy), 0-100
          vrop      — Volatility Risk Premium = current_iv - hv30 (percentage points)
                      Positive = IV > realised vol → selling premium is attractive
        """
        ticker = yf.Ticker(symbol)
        # Fetch ~5 months so we have enough history for a 63-day rolling window
        hist = ticker.history(period='5mo')
        if hist.empty or len(hist) < 30:
            return {'iv_rank': 50.0, 'hv30': 0.0, 'current_iv': 0.0, 'vrop': 0.0}

        returns = np.log(hist['Close'] / hist['Close'].shift(1)).dropna()

        # HV30: rolling 30-day realised vol, annualised
        rolling_hv = returns.rolling(window=30).std() * np.sqrt(252)
        rolling_hv = rolling_hv.dropna()

        if rolling_hv.empty:
            return {'iv_rank': 50.0, 'hv30': 0.0, 'current_iv': 0.0, 'vrop': 0.0}

        hv30_current = round(rolling_hv.iloc[-1] * 100, 2)  # as %

        # The value we rank: real ATM IV if caller supplies it, else HV proxy
        if current_iv and current_iv > 0:
            rank_value = current_iv / 100.0   # caller passes as %, convert to decimal
        else:
            rank_value = rolling_hv.iloc[-1]
            current_iv = hv30_current         # report what we used

        # IV rank over the 63-day rolling HV range (3-month window)
        window = rolling_hv.tail(lookback_days)
        min_vol = window.min()
        max_vol = window.max()

        if max_vol == min_vol:
            iv_rank_val = 0.5
        else:
            iv_rank_val = (rank_value - min_vol) / (max_vol - min_vol)
            iv_rank_val = max(0.0, min(1.0, iv_rank_val))

        vrop = round(current_iv - hv30_current, 2)   # IV - HV, in pp

        return {
            'iv_rank':    round(iv_rank_val * 100, 1),
            'hv30':       hv30_current,
            'current_iv': round(current_iv, 2),
            'vrop':       vrop,
        }
