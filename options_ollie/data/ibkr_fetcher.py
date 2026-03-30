"""
Options Ollie — IBKR Data Fetcher
Fetches ASX options chain data via Interactive Brokers TWS/Gateway using ib_insync.

Uses asyncio.run() in a dedicated thread to avoid Flask/asyncio conflicts.
Each call spins up a fresh thread with its own event loop via ThreadPoolExecutor.
"""

import asyncio
import threading
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional, Dict, List
from scipy.stats import norm
from concurrent.futures import ThreadPoolExecutor
import warnings
warnings.filterwarnings('ignore')

ASX_CONTRACT_MULTIPLIER = 100   # ASX equity options: 100 shares per contract
ASX_RISK_FREE_RATE = 0.041


def _safe_int(val):
    """Convert to int, returning 0 for None/NaN/invalid values."""
    try:
        if val is None:
            return 0
        f = float(val)
        return 0 if f != f else int(f)  # f != f is True only for NaN
    except (TypeError, ValueError):
        return 0


class IBKRConnectionError(Exception):
    pass


class IBKRDataFetcher:
    MULTIPLIER = ASX_CONTRACT_MULTIPLIER
    RISK_FREE_RATE = ASX_RISK_FREE_RATE

    def __init__(self, host='127.0.0.1', port=4001, client_id=10, timeout=15):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.timeout = timeout
        self.last_error = None   # Surfaced in UI when chain fetch fails

    @staticmethod
    def _strip_asx(symbol):
        return symbol.upper().replace('.AX', '').strip()

    def is_available(self):
        def _check():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            from ib_insync import IB, util
            util.patchAsyncio()
            ib = IB()
            try:
                ib.connect(self.host, self.port, clientId=self.client_id + 50,
                           readonly=True, timeout=5)
                return True
            except Exception:
                return False
            finally:
                ib.disconnect()
                loop.close()
        try:
            with ThreadPoolExecutor(1) as pool:
                return pool.submit(_check).result(timeout=10)
        except Exception:
            return False

    # ── Stock info (yfinance — works for .AX) ────────────────────────────────

    def get_stock_info(self, symbol):
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period='14mo')
        if hist.empty:
            return {}
        info = ticker.info or {}
        price = hist['Close'].iloc[-1]
        returns = np.log(hist['Close'] / hist['Close'].shift(1)).dropna()
        hv30 = returns.tail(30).std() * np.sqrt(252) if len(returns) >= 30 else returns.std() * np.sqrt(252)
        hv60 = returns.tail(60).std() * np.sqrt(252) if len(returns) >= 60 else hv30
        high = hist['High'].max()
        low = hist['Low'].min()
        return {
            'symbol': symbol, 'price': round(price, 3),
            'market_cap': info.get('marketCap', 0),
            'avg_volume': int(hist['Volume'].mean()),
            'hv_30': round(hv30, 4), 'hv_60': round(hv60, 4),
            'high_52w': round(high, 3), 'low_52w': round(low, 3),
            'pct_from_high': round((price - high) / high * 100, 2),
            'pct_from_low': round((price - low) / low * 100, 2),
            'sector': info.get('sector', 'N/A'),
            'name': info.get('shortName', symbol),
            'earnings_date': str(info.get('earningsTimestamp', 'N/A')),
            'currency': 'AUD', 'multiplier': self.MULTIPLIER,
        }

    # ── Options chain ────────────────────────────────────────────────────────

    def get_options_chain(self, symbol, min_dte=14, max_dte=90):
        """Fetch ASX options chain using asyncio.run() in a clean thread."""
        self.last_error = None
        host, port, cid, timeout = self.host, self.port, self.client_id, self.timeout
        ib_sym = self._strip_asx(symbol)

        def _worker():
            """Run the async chain fetch in a brand-new event loop via asyncio.run()."""

            async def _fetch():
                from ib_insync import IB, Stock, Option
                ib = IB()
                try:
                    await ib.connectAsync(host, port, clientId=cid, readonly=True, timeout=timeout)
                    ib.reqMarketDataType(3)  # Delayed data (free, no subscription needed)

                    # Qualify stock
                    stock = Stock(ib_sym, 'ASX', 'AUD')
                    await ib.qualifyContractsAsync(stock)
                    if not stock.conId:
                        return pd.DataFrame(), f"Could not qualify {ib_sym} on ASX"

                    # Get option parameters
                    chains = await ib.reqSecDefOptParamsAsync(
                        stock.symbol, '', stock.secType, stock.conId
                    )
                    if not chains:
                        return pd.DataFrame(), f"No option chains returned for {ib_sym}"
                    chain = next((c for c in chains if c.exchange == 'ASX'), chains[0])

                    # Filter expiries by DTE
                    today = datetime.now().date()
                    valid_expiries = []
                    for exp in sorted(chain.expirations):
                        exp_date = datetime.strptime(exp, '%Y%m%d').date()
                        dte = (exp_date - today).days
                        if min_dte <= dte <= max_dte:
                            valid_expiries.append((exp, exp_date, dte))
                    if not valid_expiries:
                        return pd.DataFrame(), f"No expiries in {min_dte}-{max_dte} DTE range"

                    # Stock price
                    import yfinance as yf
                    h = yf.Ticker(symbol).history(period='1d')
                    if h.empty:
                        return pd.DataFrame(), "Could not get stock price from yfinance"
                    stock_price = h['Close'].iloc[-1]

                    # Filter strikes ±30%
                    strikes = [s for s in sorted(chain.strikes)
                               if stock_price * 0.70 <= s <= stock_price * 1.30]
                    if not strikes:
                        strikes = list(sorted(chain.strikes))

                    all_rows = []
                    for exp_str, exp_date, dte in valid_expiries:
                        exp_ymd = exp_date.strftime('%Y-%m-%d')

                        # Build and qualify option contracts
                        opts = [Option(ib_sym, exp_str, float(s), r, 'ASX')
                                for s in strikes for r in ('C', 'P')]

                        qualified = []
                        for i in range(0, len(opts), 20):
                            batch = opts[i:i+20]
                            try:
                                await ib.qualifyContractsAsync(*batch)
                                qualified.extend([c for c in batch if c.conId])
                            except Exception:
                                continue

                        if not qualified:
                            continue

                        # Request snapshot market data
                        for c in qualified:
                            ib.reqMktData(c, '', snapshot=True, regulatorySnapshot=False)
                        await asyncio.sleep(4)  # Let data arrive

                        for c in qualified:
                            tkr = ib.ticker(c)
                            ib.cancelMktData(c)
                            if not tkr:
                                continue

                            def _safe_float(v):
                                try:
                                    f = float(v)
                                    return f if f == f and f > 0 else 0.0  # f==f rejects NaN
                                except Exception:
                                    return 0.0

                            bid = _safe_float(tkr.bid)
                            ask = _safe_float(tkr.ask)
                            last = _safe_float(tkr.last)

                            ib_iv = 0.0
                            if tkr.modelGreeks:
                                try:
                                    v = float(tkr.modelGreeks.impliedVol or 0)
                                    ib_iv = v if v == v else 0.0  # reject NaN
                                except Exception:
                                    ib_iv = 0.0

                            # No live quotes but model IV available → compute theoretical price
                            if bid <= 0 and ask <= 0 and last <= 0 and ib_iv > 0:
                                T = dte / 365.0
                                K = float(c.strike)
                                r_rate = ASX_RISK_FREE_RATE
                                if c.right == 'C':
                                    theo = IBKRDataFetcher._bs_call_price(stock_price, K, T, r_rate, ib_iv)
                                else:
                                    theo = IBKRDataFetcher._bs_put_price(stock_price, K, T, r_rate, ib_iv)
                                if theo > 0.001:
                                    mid = round(theo, 4)
                                    last = mid
                                    bid = round(theo * 0.95, 4)
                                    ask = round(theo * 1.05, 4)

                            if bid <= 0 and ask <= 0 and last <= 0:
                                continue

                            mid = round((bid + ask) / 2, 4) if (bid > 0 or ask > 0) else last
                            mult = _safe_int(c.multiplier) or ASX_CONTRACT_MULTIPLIER
                            opt_type = 'call' if c.right == 'C' else 'put'

                            all_rows.append({
                                'symbol': symbol, 'option_type': opt_type,
                                'expiry': exp_ymd, 'dte': dte,
                                'strike': float(c.strike),
                                'bid': bid, 'ask': ask, 'lastPrice': last,
                                'mid_price': mid,
                                'volume': _safe_int(tkr.volume if hasattr(tkr, 'volume') else None),
                                'openInterest': _safe_int((tkr.callOpenInterest if c.right == 'C' else tkr.putOpenInterest) if hasattr(tkr, 'callOpenInterest') else None),
                                'impliedVolatility': ib_iv,
                                'stock_price': stock_price,
                                'moneyness': round((c.strike - stock_price) / stock_price * 100, 2),
                                'bid_ask_spread': round(ask - bid, 4),
                                'bid_ask_pct': round((ask - bid) / ask, 4) if ask > 0 else 1.0,
                                'multiplier': mult, 'currency': 'AUD',
                            })

                    if not all_rows:
                        return pd.DataFrame(), f"All contracts filtered out (qualified but no prices/IV)"

                    df = pd.DataFrame(all_rows)
                    return IBKRDataFetcher._enrich_greeks_static(df, stock_price), None

                except Exception as e:
                    import traceback
                    return pd.DataFrame(), f"IBKR error: {e}\n{traceback.format_exc()}"
                finally:
                    ib.disconnect()

            # Run the async function with asyncio.run() — creates and closes its own loop
            return asyncio.run(_fetch())

        try:
            with ThreadPoolExecutor(1) as pool:
                result = pool.submit(_worker).result(timeout=120)
                if isinstance(result, tuple):
                    df, error = result
                    if error:
                        self.last_error = error
                    return df
                # Shouldn't happen, but handle gracefully
                return result if isinstance(result, pd.DataFrame) else pd.DataFrame()
        except Exception as e:
            self.last_error = f"ThreadPool error: {e}"
            return pd.DataFrame()

    # ── Greeks (static — callable without instance) ──────────────────────────

    @staticmethod
    def _bs_call_price(S, K, T, r, sigma):
        if T <= 0 or sigma <= 0: return max(S - K, 0)
        d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)

    @staticmethod
    def _bs_put_price(S, K, T, r, sigma):
        if T <= 0 or sigma <= 0: return max(K - S, 0)
        d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    @staticmethod
    def _implied_vol(price, S, K, T, r, opt_type='call', fallback=0.3):
        if T <= 0 or price <= 0: return fallback
        lo, hi = 0.01, 5.0
        for _ in range(60):
            mid = (lo + hi) / 2
            d1 = (np.log(S / K) + (r + 0.5 * mid**2) * T) / (mid * np.sqrt(T))
            d2 = d1 - mid * np.sqrt(T)
            theo = (S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2) if opt_type == 'call'
                    else K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1))
            if abs(theo - price) < 0.0001: return round(mid, 4)
            lo, hi = (mid, hi) if theo < price else (lo, mid)
        return round((lo + hi) / 2, 4)

    @staticmethod
    def _enrich_greeks_static(df, stock_price):
        r = ASX_RISK_FREE_RATE
        for idx, row in df.iterrows():
            T = row['dte'] / 365.0
            K, ot = row['strike'], row['option_type']
            px = float(row.get('mid_price') or row.get('lastPrice') or 0)
            ib_iv = float(row.get('impliedVolatility') or 0)

            sigma = None
            if ib_iv > 0.05 and T > 0 and px > 0:
                theo = (IBKRDataFetcher._bs_call_price(stock_price, K, T, r, ib_iv) if ot == 'call'
                        else IBKRDataFetcher._bs_put_price(stock_price, K, T, r, ib_iv))
                if theo > 0 and abs(theo - px) / px < 0.5:
                    sigma = ib_iv
            if sigma is None and px > 0 and T > 0:
                sigma = IBKRDataFetcher._implied_vol(px, stock_price, K, T, r, ot)
            sigma = sigma or 0.30
            try:
                d1 = (np.log(stock_price / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
                d2 = d1 - sigma * np.sqrt(T)
                delta = norm.cdf(d1) if ot == 'call' else norm.cdf(d1) - 1
                prob_itm = norm.cdf(d2) if ot == 'call' else norm.cdf(-d2)
                df.at[idx, 'delta_est'] = round(delta, 4)
                df.at[idx, 'gamma_est'] = round(norm.pdf(d1) / (stock_price * sigma * np.sqrt(T)), 6)
                df.at[idx, 'theta_est'] = round(-(stock_price * norm.pdf(d1) * sigma) / (2 * np.sqrt(T)) / 365, 4)
                df.at[idx, 'vega_est'] = round(stock_price * norm.pdf(d1) * np.sqrt(T) / 100, 4)
                df.at[idx, 'prob_itm'] = round(prob_itm, 4)
                df.at[idx, 'prob_otm'] = round(1 - prob_itm, 4)
                df.at[idx, 'prob_touch'] = round(min(2 * prob_itm, 0.99), 4)
                df.at[idx, 'iv_used'] = round(sigma, 4)
            except Exception:
                for col in ('delta_est', 'gamma_est', 'theta_est', 'vega_est', 'prob_itm', 'prob_touch'):
                    df.at[idx, col] = 0
                df.at[idx, 'prob_otm'] = 1
                df.at[idx, 'iv_used'] = sigma
        return df

    # ── IV rank ──────────────────────────────────────────────────────────────

    def get_iv_rank(self, symbol, lookback_days=252, current_iv=None):
        import yfinance as yf
        hist = yf.Ticker(symbol).history(period='14mo')
        if hist.empty or len(hist) < 30:
            return {'iv_rank': 50.0, 'hv30': 0.0, 'current_iv': 0.0, 'vrop': 0.0}
        returns = np.log(hist['Close'] / hist['Close'].shift(1)).dropna()
        rhv = returns.rolling(window=30).std() * np.sqrt(252)
        rhv = rhv.dropna()
        if rhv.empty:
            return {'iv_rank': 50.0, 'hv30': 0.0, 'current_iv': 0.0, 'vrop': 0.0}
        hv30 = round(rhv.iloc[-1] * 100, 2)
        rv = (current_iv / 100.0) if current_iv and current_iv > 0 else rhv.iloc[-1]
        if not (current_iv and current_iv > 0): current_iv = hv30
        w = rhv.tail(lookback_days)
        mn, mx = w.min(), w.max()
        ivr = 0.5 if mx == mn else max(0.0, min(1.0, (rv - mn) / (mx - mn)))
        return {'iv_rank': round(ivr * 100, 1), 'hv30': hv30,
                'current_iv': round(current_iv, 2), 'vrop': round(current_iv - hv30, 2)}

    # ── Earnings ─────────────────────────────────────────────────────────────

    def get_earnings_calendar(self, symbols):
        import yfinance as yf
        earnings = {}
        for sym in symbols:
            try:
                cal = yf.Ticker(sym).calendar
                if isinstance(cal, dict):
                    ed = cal.get('Earnings Date', [None])
                    earnings[sym] = str(ed[0]) if ed else None
                else:
                    earnings[sym] = None
            except Exception:
                earnings[sym] = None
        return earnings
