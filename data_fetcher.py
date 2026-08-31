"""
Data fetcher using yfinance for historical prices, financials, and valuations.

Kotak Neo API does not provide historical data, so yfinance fills that gap.
NSE India API is used for promoter pledging data.
"""
import io
import logging
import random
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import requests
import yfinance as yf

import config
from utils import nse_to_yfinance

logger = logging.getLogger("SwingScreener.DataFetcher")

# Error fragments that indicate a transient, retryable Yahoo failure rather
# than a genuine "this ticker has no data" outcome.
_TRANSIENT_ERROR_MARKERS = (
    "429",
    "too many requests",
    "rate limit",
    "invalid crumb",
    "unauthorized",
    "401",
    "timed out",
    "timeout",
    "connection",
    "temporarily unavailable",
    "503",
    "502",
    "500",
    "remote end closed",
)


def _is_transient(err: Exception) -> bool:
    """True if the error looks like throttling / a transient network fault."""
    msg = str(err).lower()
    return any(marker in msg for marker in _TRANSIENT_ERROR_MARKERS)


def _is_crumb_error(err: Exception) -> bool:
    """True if the failure is specifically a stale cookie/crumb."""
    msg = str(err).lower()
    return "crumb" in msg or "unauthorized" in msg or "401" in msg


class _RateLimiter:
    """
    Global minimum-interval throttle shared by every worker thread, with
    adaptive backpressure.

    Each caller reserves the next time slot under a lock, then sleeps outside
    the lock, so requests are paced without serialising the threads on the
    sleep itself.

    When Yahoo signals throttling, :meth:`penalise` widens the interval; a
    sustained run of successes narrows it again via :meth:`reward`. This means
    a long scan converges on a pace Yahoo tolerates instead of hammering it at
    a rate it has already rejected.
    """

    def __init__(self, min_interval: float, max_interval: float = None,
                 adaptive: bool = True):
        self._base = max(0.0, min_interval)
        self._interval = self._base
        self._max = max_interval if max_interval is not None else self._base
        self._adaptive = adaptive
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self._successes = 0
        self.peak_interval = self._base

    @property
    def interval(self) -> float:
        return self._interval

    def acquire(self):
        with self._lock:
            interval = self._interval
            if interval <= 0:
                return
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def penalise(self):
        """Widen the interval after a rate-limit signal."""
        if not self._adaptive:
            return
        with self._lock:
            self._successes = 0
            # Seed a floor so growth still works when throttling started at 0.
            current = self._interval if self._interval > 0 else 0.05
            self._interval = min(current * config.YF_THROTTLE_GROWTH, self._max)
            if self._interval > self.peak_interval:
                self.peak_interval = self._interval

    def reward(self):
        """Ease the interval back down after a streak of clean responses."""
        if not self._adaptive or self._interval <= self._base:
            return
        with self._lock:
            self._successes += 1
            if self._successes >= config.YF_THROTTLE_RECOVER_AFTER:
                self._successes = 0
                self._interval = max(
                    self._base, self._interval * config.YF_THROTTLE_DECAY
                )


def reset_yf_session() -> None:
    """
    Force yfinance to re-negotiate its cookie and crumb.

    yfinance caches these on a `YfData` singleton. Once Yahoo invalidates them
    (the 429 -> 401 "Invalid Crumb" cascade) every later call keeps failing
    with the stale pair, so clearing them is what actually breaks the loop.
    """
    try:
        from yfinance.data import YfData

        d = YfData()
        lock = getattr(d, "_cookie_lock", None)
        if lock is not None:
            with lock:
                d._cookie = None
                d._crumb = None
        else:
            d._cookie = None
            d._crumb = None
        logger.debug("yfinance cookie/crumb reset - will re-negotiate")
    except Exception as e:
        logger.debug(f"Could not reset yfinance session: {e}")


def apply_yahoo_login(cookie_t: str = None, cookie_y: str = None) -> bool:
    """
    Optionally authenticate yfinance with Yahoo login cookies.

    IMPORTANT: this controls subscription ENTITLEMENT, not rate limits. Yahoo
    throttles per IP, so logging in does not raise your request quota. It is
    exposed only for entitlement-gated fields.

    Returns True if Yahoo confirms the cookies are logged in.
    """
    cookie_t = cookie_t or config.YF_COOKIE_T
    cookie_y = cookie_y or config.YF_COOKIE_Y
    if not cookie_t or not cookie_y:
        return False
    try:
        auth = yf.Auth()
        logged_in = auth.set_login_cookies(cookie_t, cookie_y)
        if logged_in:
            logger.info("Yahoo Finance login cookies accepted (entitlement active).")
        else:
            logger.warning(
                "Yahoo login cookies were rejected or Yahoo is unreachable. "
                "Continuing anonymously - this does not affect rate limits."
            )
        return logged_in
    except Exception as e:
        logger.warning(f"Yahoo login failed: {e}. Continuing anonymously.")
        return False


class DataFetcher:
    """
    Fetches historical price data, financials, and valuation metrics.

    All Yahoo access goes through :meth:`_retry`, which throttles requests
    globally, retries transient failures with exponential backoff, and resets
    the cached cookie/crumb when Yahoo invalidates it.
    """

    def __init__(self):
        self._cache = {}
        self._cache_lock = threading.Lock()
        self._info_cache = {}
        self._info_locks = {}
        self._limiter = _RateLimiter(
            config.YF_MIN_REQUEST_INTERVAL,
            max_interval=config.YF_MAX_REQUEST_INTERVAL,
            adaptive=config.YF_ADAPTIVE_THROTTLE,
        )
        # Diagnostics so a run can report how much throttling it absorbed.
        self.retry_stats = {"retries": 0, "crumb_resets": 0, "gave_up": 0}
        self._stats_lock = threading.Lock()

    def _get_ticker(self, symbol: str) -> yf.Ticker:
        """Get or create a yfinance Ticker object (with NSE suffix)."""
        yf_symbol = nse_to_yfinance(symbol)
        with self._cache_lock:
            if yf_symbol not in self._cache:
                self._cache[yf_symbol] = yf.Ticker(yf_symbol)
            return self._cache[yf_symbol]

    def _bump(self, key: str):
        with self._stats_lock:
            self.retry_stats[key] = self.retry_stats.get(key, 0) + 1

    def _retry(self, func, symbol: str, what: str, validate=None, default=None):
        """
        Call ``func`` with throttling, exponential backoff and crumb recovery.

        Args:
            func: Zero-arg callable performing the Yahoo request.
            symbol: Stock symbol, for logging.
            what: Short label of the payload, for logging.
            validate: Optional predicate; a result failing it is treated as a
                transient empty response and retried. This is what catches the
                silent failure mode where Yahoo returns ``{}`` instead of
                raising.
            default: Value returned when every attempt is exhausted.

        Returns:
            The first valid result, else ``default``.
        """
        attempts = max(1, config.YF_MAX_RETRIES + 1)

        for attempt in range(1, attempts + 1):
            self._limiter.acquire()
            reason = None
            try:
                result = func()
                if validate is None or validate(result):
                    self._limiter.reward()
                    return result
                # An empty payload is Yahoo's silent throttling signal, so it
                # applies backpressure just like an explicit 429.
                reason = "empty/invalid response"
                self._limiter.penalise()
            except Exception as e:
                if not _is_transient(e):
                    # A real data problem (unknown ticker, missing statement).
                    logger.debug(f"{symbol}: {what} failed permanently: {e}")
                    return default
                reason = str(e)[:120]
                self._limiter.penalise()
                if _is_crumb_error(e):
                    reset_yf_session()
                    self._bump("crumb_resets")

            if attempt >= attempts:
                break

            # Exponential backoff with jitter so parallel workers don't all
            # come back at the same instant and re-trip the limiter.
            delay = config.YF_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            delay = min(delay, config.YF_RETRY_MAX_DELAY)
            delay += random.uniform(0, config.YF_RETRY_JITTER)

            logger.debug(
                f"{symbol}: {what} attempt {attempt}/{attempts} - {reason}; "
                f"retrying in {delay:.1f}s"
            )
            self._bump("retries")
            time.sleep(delay)

            # A stale crumb is the most common cause of a repeated empty
            # response, so refresh it before the final attempts too.
            if attempt == attempts - 1:
                reset_yf_session()
                self._bump("crumb_resets")

        logger.debug(f"{symbol}: {what} exhausted {attempts} attempts")
        self._bump("gave_up")
        return default

    def get_info(self, symbol: str) -> dict:
        """
        Fetch ``Ticker.info`` once per symbol, with retries, and cache it.

        Five different call sites need ``info``; without this they would each
        pay their own rate-limit cost for the same payload. A per-symbol lock
        ensures concurrent callers wait for one fetch instead of duplicating
        it.
        """
        yf_symbol = nse_to_yfinance(symbol)

        with self._cache_lock:
            if yf_symbol in self._info_cache:
                return self._info_cache[yf_symbol]
            lock = self._info_locks.setdefault(yf_symbol, threading.Lock())

        with lock:
            # Another thread may have populated it while we waited.
            with self._cache_lock:
                if yf_symbol in self._info_cache:
                    return self._info_cache[yf_symbol]

            def _fetch():
                # Build a FRESH Ticker each attempt so yfinance's internal
                # caches start clean and a retry genuinely re-requests rather
                # than replaying a cached empty dict.
                #
                # Do NOT try to invalidate by nulling ticker._quote / _info:
                # Ticker.info reads self._quote.info, so clearing it raises
                # AttributeError on every call and breaks info entirely.
                return yf.Ticker(yf_symbol).info or {}

            # A usable info dict must carry at least one of the fields the
            # screener depends on; an empty or near-empty dict means throttled.
            def _valid(d):
                if not d:
                    return False
                keys = ("currentPrice", "regularMarketPrice", "marketCap",
                        "trailingPE", "fiftyTwoWeekHigh", "longName")
                return any(d.get(k) is not None for k in keys)

            info = self._retry(_fetch, symbol, "info", validate=_valid, default={}) or {}

            with self._cache_lock:
                self._info_cache[yf_symbol] = info
            return info

    def get_historical_prices(
        self,
        symbol: str,
        period_years: int = None,
    ) -> Optional[pd.DataFrame]:
        """
        Fetch daily OHLCV data for the given symbol.

        Returns DataFrame with columns: Open, High, Low, Close, Volume
        """
        period_years = period_years or config.HISTORICAL_PERIOD_YEARS
        try:
            ticker = self._get_ticker(symbol)
            end_date = datetime.now()
            start_date = end_date - timedelta(days=period_years * 365 + 30)

            df = self._retry(
                lambda: ticker.history(
                    start=start_date.strftime("%Y-%m-%d"),
                    end=end_date.strftime("%Y-%m-%d"),
                ),
                symbol,
                "price history",
                validate=lambda d: d is not None and not d.empty,
            )

            if df is None or df.empty:
                logger.debug(f"No price data for {symbol}")
                return None

            if len(df) < config.LONG_DMA_PERIOD:
                logger.debug(f"Insufficient price data for {symbol}: {len(df)} rows (need {config.LONG_DMA_PERIOD})")
                return None

            return df

        except Exception as e:
            logger.debug(f"Error fetching prices for {symbol}: {e}")
            return None

    def get_quarterly_financials(self, symbol: str) -> Optional[pd.DataFrame]:
        """
        Fetch quarterly income statement data.

        Tries the full time-series API first (more quarters) then falls back
        to the standard quarterly_income_stmt property.

        Returns DataFrame with quarterly revenue (Total Revenue) and
        net income (Net Income) figures.
        """
        try:
            ticker = self._get_ticker(symbol)

            def _not_empty(d):
                return d is not None and not d.empty

            # Try get_income_time_series for more quarterly data
            qf = None
            try:
                qf = ticker.financials.get_income_time_series(freq="quarterly")
            except Exception:
                pass

            # Fallback to standard property (retried — this is the path that
            # gets throttled most often)
            if qf is None or qf.empty:
                qf = self._retry(
                    lambda: ticker.quarterly_income_stmt,
                    symbol,
                    "quarterly income stmt",
                    validate=_not_empty,
                )

            if qf is None or qf.empty:
                logger.debug(f"No quarterly financials for {symbol}")
                return None

            return qf

        except Exception as e:
            logger.debug(f"Error fetching quarterly financials for {symbol}: {e}")
            return None

    def get_pe_ratio_data(self, symbol: str) -> dict:
        """
        Fetch current and historical PE ratio data.

        Uses yfinance valuation_measures for quarterly historical PE values.
        The row "Trailing P/E" contains quarterly trailing PE ratios.

        Returns dict with: current_pe, historical_pe_values (list), median_pe
        """
        result = {
            "current_pe": None,
            "historical_pe_values": [],
            "median_pe": None,
        }

        try:
            ticker = self._get_ticker(symbol)

            # Current PE from info (cached + retried)
            info = self.get_info(symbol)
            if info:
                result["current_pe"] = info.get("trailingPE") or info.get("forwardPE")

            # Historical PE from valuation measures (quarterly)
            try:
                val = self._retry(
                    lambda: ticker.get_valuation_measures(freq="quarterly", periods=None),
                    symbol,
                    "valuation measures",
                    validate=lambda d: d is not None and not d.empty,
                )
                if val is not None and not val.empty:
                    # Look for the Trailing P/E row
                    pe_row = None
                    pe_candidates = ["Trailing P/E", "TrailingPE", "Forward P/E"]
                    for candidate in pe_candidates:
                        if candidate in val.index:
                            pe_row = candidate
                            break

                    # Fallback: fuzzy match
                    if pe_row is None:
                        for idx_name in val.index:
                            idx_lower = str(idx_name).lower()
                            if "trailing" in idx_lower and ("p/e" in idx_lower or "pe" in idx_lower):
                                pe_row = idx_name
                                break

                    if pe_row is not None:
                        pe_series = val.loc[pe_row].dropna()
                        # Exclude the 'Current' column for historical median
                        historical_cols = [c for c in pe_series.index if str(c) != "Current"]
                        hist_pe = pe_series[historical_cols] if historical_cols else pe_series
                        pe_values = [float(v) for v in hist_pe.values if pd.notna(v) and float(v) > 0]
                        result["historical_pe_values"] = pe_values
                        if pe_values:
                            result["median_pe"] = float(np.median(pe_values))

                        # Fallback for current PE: the valuation-measures table
                        # carries a "Current" column. This keeps the screener
                        # working when ``info`` is rate-limited by Yahoo (the
                        # 429 / "Invalid Crumb" failure mode), which otherwise
                        # nulls out current_pe and collapses the whole pipeline.
                        if result["current_pe"] is None and "Current" in pe_series.index:
                            cur = pe_series["Current"]
                            if pd.notna(cur) and float(cur) > 0:
                                result["current_pe"] = float(cur)
            except Exception:
                pass

            # Second fallback: derive current PE from price / TTM EPS using the
            # quarterly statements, which stay available under rate limiting.
            if result["current_pe"] is None:
                try:
                    qf = self._retry(
                        lambda: ticker.quarterly_income_stmt, symbol,
                        "quarterly income stmt (PE fallback)",
                        validate=lambda d: d is not None and not d.empty,
                    )
                    hist = self._retry(
                        lambda: ticker.history(period="5d"), symbol,
                        "recent price (PE fallback)",
                        validate=lambda d: d is not None and not d.empty,
                    )
                    if (qf is not None and not qf.empty
                            and hist is not None and not hist.empty):
                        ni_row = next(
                            (r for r in ["Net Income", "Net Income Common Stockholders"]
                             if r in qf.index), None)
                        sh_row = next(
                            (r for r in ["Diluted Average Shares", "Basic Average Shares"]
                             if r in qf.index), None)
                        if ni_row and sh_row:
                            ni_vals = qf.loc[ni_row].dropna()
                            sh_vals = qf.loc[sh_row].dropna()
                            if len(ni_vals) >= 4 and len(sh_vals) > 0:
                                ttm_ni = float(ni_vals.iloc[:4].sum())
                                shares = float(sh_vals.iloc[0])
                                price = float(hist["Close"].iloc[-1])
                                if shares > 0 and ttm_ni > 0:
                                    eps_ttm = ttm_ni / shares
                                    pe = price / eps_ttm
                                    if 0 < pe < 1000:
                                        result["current_pe"] = pe
                except Exception:
                    pass

            # Fallback: compute PE from price history and EPS if valuation measures unavailable
            if not result["historical_pe_values"] and result["current_pe"]:
                try:
                    # Use quarterly close prices and trailing EPS to approximate PE
                    hist = self._retry(
                        lambda: ticker.history(period="3y"), symbol,
                        "3y price history (PE median fallback)",
                        validate=lambda d: d is not None and not d.empty,
                    )
                    qf = self._retry(
                        lambda: ticker.quarterly_income_stmt, symbol,
                        "quarterly income stmt (PE median fallback)",
                        validate=lambda d: d is not None and not d.empty,
                    )
                    if hist is not None and not hist.empty and qf is not None and not qf.empty:
                        # Get quarterly EPS values
                        shares_row = None
                        for candidate in ["Diluted Average Shares", "Basic Average Shares"]:
                            if candidate in qf.index:
                                shares_row = candidate
                                break
                        net_income_row = None
                        for candidate in ["Net Income", "Net Income Common Stockholders"]:
                            if candidate in qf.index:
                                net_income_row = candidate
                                break

                        if shares_row and net_income_row:
                            pe_values = []
                            for col in qf.columns:
                                ni = qf.loc[net_income_row, col]
                                shares = qf.loc[shares_row, col]
                                if pd.notna(ni) and pd.notna(shares) and shares > 0:
                                    eps = ni / shares
                                    if eps > 0:
                                        # Find the closest price to this quarter end
                                        quarter_date = pd.Timestamp(col)
                                        price_idx = hist.index.get_indexer([quarter_date], method="nearest")
                                        if len(price_idx) > 0 and price_idx[0] >= 0:
                                            price = hist["Close"].iloc[price_idx[0]]
                                            pe = price / (eps * 4)  # Annualize quarterly EPS
                                            if 0 < pe < 500:
                                                pe_values.append(pe)

                            if pe_values:
                                result["historical_pe_values"] = pe_values
                                result["median_pe"] = float(np.median(pe_values))
                except Exception:
                    pass

            # If we have current PE but no historical, use current as fallback median
            if result["current_pe"] and not result["median_pe"]:
                result["median_pe"] = result["current_pe"]

        except Exception as e:
            logger.debug(f"Error fetching PE data for {symbol}: {e}")

        return result

    def get_debt_quality_data(self, symbol: str) -> dict:
        """
        Fetch Debt-to-Equity ratio and promoter pledged percentage.

        D/E ratio: from yfinance info or computed from balance sheet.
        Pledged %: from NSE India corporate filings API.

        Returns dict with: debt_to_equity, pledged_pct, total_debt, total_equity
        """
        result = {
            "debt_to_equity": None,
            "pledged_pct": None,
            "total_debt": None,
            "total_equity": None,
        }

        try:
            ticker = self._get_ticker(symbol)
            info = self.get_info(symbol)

            # --- Debt-to-Equity ---
            # yfinance reports debtToEquity as a percentage (e.g. 36.65 = 0.3665)
            de_pct = info.get("debtToEquity")
            if de_pct is not None and not np.isnan(de_pct):
                result["debt_to_equity"] = de_pct / 100.0
                result["total_debt"] = info.get("totalDebt")
            else:
                # Compute from balance sheet
                try:
                    bs = self._retry(
                        lambda: ticker.quarterly_balance_sheet, symbol,
                        "quarterly balance sheet",
                        validate=lambda d: d is not None and not d.empty,
                    )
                    if bs is not None and not bs.empty:
                        debt = equity = None
                        for row_name in ["Total Debt", "Total Liabilities Net Minority Interest"]:
                            if row_name in bs.index:
                                val = bs.loc[row_name].dropna()
                                if len(val) > 0:
                                    debt = float(val.iloc[0])
                                    break
                        for row_name in ["Stockholders Equity", "Common Stock Equity",
                                         "Total Equity Gross Minority Interest"]:
                            if row_name in bs.index:
                                val = bs.loc[row_name].dropna()
                                if len(val) > 0:
                                    equity = float(val.iloc[0])
                                    break
                        if debt is not None and equity is not None and equity > 0:
                            result["debt_to_equity"] = debt / equity
                            result["total_debt"] = debt
                            result["total_equity"] = equity
                except Exception:
                    pass

            # --- Promoter Pledged Percentage ---
            result["pledged_pct"] = self._fetch_pledged_pct(symbol)

        except Exception as e:
            logger.debug(f"Error fetching debt quality for {symbol}: {e}")

        return result

    def _fetch_pledged_pct(self, symbol: str) -> Optional[float]:
        """
        Fetch promoter pledged percentage from NSE India API.

        Falls back to 0.0 if the API doesn't return data (many companies
        have zero pledging and may not appear in the pledged-data response).
        """
        try:
            nse_headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.5",
                "Referer": "https://www.nseindia.com/",
            }

            session = requests.Session()
            # Warm-up to get cookies
            session.get("https://www.nseindia.com/", headers=nse_headers, timeout=5)

            url = (
                f"https://www.nseindia.com/api/corporate-share-holdings-master"
                f"?index=equities&symbol={symbol}"
            )
            resp = session.get(url, headers=nse_headers, timeout=8)

            if resp.status_code == 200:
                data = resp.json()
                # Look for promoter pledged data
                if isinstance(data, list):
                    for entry in data:
                        pledged = entry.get("percPledgedOrEncumbered")
                        if pledged is not None:
                            return float(pledged)
                elif isinstance(data, dict):
                    pledged = data.get("percPledgedOrEncumbered")
                    if pledged is not None:
                        return float(pledged)

            # Try alternative shareholding pattern endpoint
            url2 = (
                f"https://www.nseindia.com/api/corporate-shareholding"
                f"?index=equities&symbol={symbol}"
            )
            resp2 = session.get(url2, headers=nse_headers, timeout=8)
            if resp2.status_code == 200:
                data2 = resp2.json()
                if isinstance(data2, dict):
                    promoter = data2.get("promoterHolding", {})
                    pledged = promoter.get("pledgedPercentage")
                    if pledged is not None:
                        return float(pledged)

        except Exception as e:
            logger.debug(f"NSE pledged data fetch failed for {symbol}: {e}")

        # Default: assume 0% pledging if we can't fetch data
        return 0.0

    def get_price_momentum_data(self, symbol: str) -> dict:
        """
        Fetch 52-week high/low, current price, and beta from yfinance.

        All values come from the cached ``info`` dict — no extra network
        round-trip when called after get_pe_ratio_data / get_debt_quality_data.

        Returns dict with:
            current_price, week52_high, week52_low, beta
        """
        result = {
            "current_price": None,
            "week52_high": None,
            "week52_low": None,
            "beta": None,
            "market_cap_cr": None,
        }
        try:
            ticker = self._get_ticker(symbol)
            info = self.get_info(symbol)

            current = info.get("currentPrice") or info.get("regularMarketPrice")
            high52  = info.get("fiftyTwoWeekHigh")
            low52   = info.get("fiftyTwoWeekLow")
            beta    = info.get("beta")
            mcap    = info.get("marketCap")

            result["current_price"] = float(current) if current is not None else None
            result["week52_high"]   = float(high52)  if high52  is not None else None
            result["week52_low"]    = float(low52)   if low52   is not None else None
            result["beta"]          = float(beta)    if beta    is not None else None
            # ₹ crore, used for the bluechip pullback relaxation.
            result["market_cap_cr"] = float(mcap) / 1e7 if mcap is not None else None

        except Exception as e:
            logger.debug(f"Error fetching price momentum data for {symbol}: {e}")

        return result

    def get_fundamental_quality_data(self, symbol: str,
                                      sector: str = "", industry: str = "") -> dict:
        """
        Fetch the fundamental quality metrics used by positional mode.

        Most values come from yfinance ``info``. ROCE and interest coverage
        are not exposed by yfinance, so they are computed from the income
        statement and balance sheet:

            ROCE              = EBIT / (Total Assets − Current Liabilities)
            Interest Coverage = EBIT / Interest Expense

        ``sector`` and ``industry`` are forwarded from the stock-universe entry
        so the quality filter can auto-detect financial-sector stocks without
        making an extra network call.

        Returns dict with:
            market_cap_cr, roe_pct, roce_pct, net_margin_pct,
            operating_margin_pct, gross_margin_pct, current_ratio,
            interest_coverage, free_cashflow, operating_cashflow,
            price_to_book, peg_ratio, roa_pct,
            sector, industry
        """
        result = {
            "market_cap_cr": None,
            "roe_pct": None,
            "roce_pct": None,
            "roa_pct": None,
            "net_margin_pct": None,
            "operating_margin_pct": None,
            "gross_margin_pct": None,
            "current_ratio": None,
            "interest_coverage": None,
            "free_cashflow": None,
            "operating_cashflow": None,
            "price_to_book": None,
            "peg_ratio": None,
            # --- Classic quality gates / signals (Graham / Piotroski) ---
            "net_income": None,            # latest annual net income
            "ocf_to_ni": None,             # operating cash flow / net income
            "has_annual_loss": None,       # any annual net loss in available yrs
            "annual_periods": 0,           # number of annual periods analysed
            "piotroski_score": None,       # 0-9 fundamental-momentum score
            "piotroski_max": None,         # signals actually computable
            "debt_to_ebitda": None,        # leverage vs cash earnings
            "pe_x_pb": None,               # Graham Number proxy (P/E x P/B)
            "graham_ok": None,             # pe_x_pb <= GRAHAM_MAX_PE_X_PB
            "dividend_yield_pct": None,
            "payout_ratio_pct": None,
            # Sector metadata — used by the quality filter for bank detection.
            # Will be overwritten below if yfinance returns richer strings.
            "sector": sector,
            "industry": industry,
        }

        def _pct(value):
            """yfinance returns margins/ROE as decimals (0.185 → 18.5%)."""
            if value is None:
                return None
            try:
                v = float(value)
            except (TypeError, ValueError):
                return None
            if np.isnan(v):
                return None
            return v * 100.0

        def _num(value):
            if value is None:
                return None
            try:
                v = float(value)
            except (TypeError, ValueError):
                return None
            return None if np.isnan(v) else v

        try:
            ticker = self._get_ticker(symbol)
            info = self.get_info(symbol)

            # --- Sector / industry (enrich from yfinance if available) ---
            yf_sector   = info.get("sector", "") or ""
            yf_industry = info.get("industry", "") or ""
            if yf_sector:
                result["sector"] = yf_sector
            if yf_industry:
                result["industry"] = yf_industry

            # --- Size ---
            mcap = _num(info.get("marketCap"))
            if mcap is not None:
                result["market_cap_cr"] = mcap / 1e7   # INR → ₹ crore

            # --- Return ratios & margins (direct from info) ---
            result["roe_pct"]              = _pct(info.get("returnOnEquity"))
            result["roa_pct"]              = _pct(info.get("returnOnAssets"))
            result["net_margin_pct"]       = _pct(info.get("profitMargins"))
            result["operating_margin_pct"] = _pct(info.get("operatingMargins"))
            result["gross_margin_pct"]     = _pct(info.get("grossMargins"))

            # --- Liquidity & cash flow ---
            result["current_ratio"]      = _num(info.get("currentRatio"))
            result["free_cashflow"]      = _num(info.get("freeCashflow"))
            result["operating_cashflow"] = _num(info.get("operatingCashflow"))

            # --- Valuation extras (reported, not filtered on) ---
            result["price_to_book"] = _num(info.get("priceToBook"))
            result["peg_ratio"]     = _num(info.get("trailingPegRatio")) or _num(info.get("pegRatio"))

            # --- Dividend record (Graham quality signal, reported) ---
            # yfinance reports dividendYield for .NS tickers already as a percent
            # (e.g. 2.78 = 2.78%); payoutRatio as a decimal (0.38 = 38%).
            result["dividend_yield_pct"] = _num(info.get("dividendYield"))
            _payout = _num(info.get("payoutRatio"))
            result["payout_ratio_pct"] = _payout * 100.0 if _payout is not None else None

            # --- Graham Number proxy: P/E x P/B (reported) ---
            trailing_pe = _num(info.get("trailingPE"))
            if trailing_pe is not None and result["price_to_book"] is not None:
                result["pe_x_pb"] = trailing_pe * result["price_to_book"]
                result["graham_ok"] = result["pe_x_pb"] <= config.GRAHAM_MAX_PE_X_PB

            # ----------------------------------------------------------------
            # Statement-derived metrics.
            #
            # yfinance's ``info`` dict is unreliable for NSE tickers — ROE,
            # currentRatio and freeCashflow are absent for ~90% of symbols
            # (and degrade further under rate limiting). The annual statements
            # are far more consistently populated, so anything missing from
            # ``info`` is recomputed here from first principles.
            # ----------------------------------------------------------------
            inc = bs = cf = None
            try:
                inc = self._retry(
                    lambda: ticker.income_stmt, symbol, "income statement",
                    validate=lambda d: d is not None and not d.empty,
                )
            except Exception:
                pass
            try:
                bs = self._retry(
                    lambda: ticker.balance_sheet, symbol, "balance sheet",
                    validate=lambda d: d is not None and not d.empty,
                )
            except Exception:
                pass
            try:
                cf = self._retry(
                    lambda: ticker.cashflow, symbol, "cash flow statement",
                    validate=lambda d: d is not None and not d.empty,
                )
            except Exception:
                pass

            def latest(df, names):
                """First non-null value of the most recent period for any row name."""
                if df is None or df.empty:
                    return None
                for name in names:
                    if name in df.index:
                        vals = df.loc[name].dropna()
                        if len(vals) > 0:
                            try:
                                return float(vals.iloc[0])
                            except (TypeError, ValueError):
                                continue
                return None

            # --- Income statement lines ---
            ebit = latest(inc, ["EBIT", "Operating Income",
                                "Total Operating Income As Reported"])
            interest_expense = latest(inc, ["Interest Expense",
                                            "Interest Expense Non Operating"])
            if interest_expense is not None:
                interest_expense = abs(interest_expense)
            net_income = latest(inc, ["Net Income",
                                      "Net Income Common Stockholders",
                                      "Net Income From Continuing Operations"])
            total_revenue = latest(inc, ["Total Revenue", "Operating Revenue"])

            # --- Balance sheet lines ---
            total_assets        = latest(bs, ["Total Assets"])
            current_liabilities = latest(bs, ["Current Liabilities",
                                              "Total Current Liabilities"])
            current_assets      = latest(bs, ["Current Assets",
                                              "Total Current Assets"])
            equity = latest(bs, ["Stockholders Equity", "Common Stock Equity",
                                 "Total Equity Gross Minority Interest"])

            # --- Interest coverage: EBIT / Interest Expense ---
            if result["interest_coverage"] is None and ebit is not None:
                if interest_expense and interest_expense > 0:
                    result["interest_coverage"] = ebit / interest_expense
                elif ebit > 0:
                    # No interest cost at all — effectively debt-free.
                    result["interest_coverage"] = 999.0

            # --- ROCE = EBIT / (Total Assets − Current Liabilities) ---
            if (result["roce_pct"] is None and ebit is not None
                    and total_assets is not None and current_liabilities is not None):
                capital_employed = total_assets - current_liabilities
                if capital_employed > 0:
                    result["roce_pct"] = (ebit / capital_employed) * 100.0

            # --- ROE = Net Income / Shareholders Equity ---
            if result["roe_pct"] is None and net_income is not None and equity and equity > 0:
                result["roe_pct"] = (net_income / equity) * 100.0

            # --- ROA = Net Income / Total Assets ---
            if (result["roa_pct"] is None and net_income is not None
                    and total_assets and total_assets > 0):
                result["roa_pct"] = (net_income / total_assets) * 100.0

            # --- Net margin = Net Income / Total Revenue ---
            if (result["net_margin_pct"] is None and net_income is not None
                    and total_revenue and total_revenue > 0):
                result["net_margin_pct"] = (net_income / total_revenue) * 100.0

            # --- Operating margin = EBIT / Total Revenue ---
            if (result["operating_margin_pct"] is None and ebit is not None
                    and total_revenue and total_revenue > 0):
                result["operating_margin_pct"] = (ebit / total_revenue) * 100.0

            # --- Current ratio = Current Assets / Current Liabilities ---
            if (result["current_ratio"] is None and current_assets is not None
                    and current_liabilities and current_liabilities > 0):
                result["current_ratio"] = current_assets / current_liabilities

            # --- Market cap = price × shares outstanding ---
            # ``info['marketCap']`` disappears under Yahoo rate limiting, so
            # reconstruct it from the share count and the latest close.
            if result["market_cap_cr"] is None:
                shares = latest(bs, ["Ordinary Shares Number", "Share Issued"])
                if shares is None:
                    shares = latest(inc, ["Diluted Average Shares",
                                          "Basic Average Shares"])
                if shares and shares > 0:
                    try:
                        hist = self._retry(
                            lambda: ticker.history(period="5d"), symbol,
                            "recent price (mcap fallback)",
                            validate=lambda d: d is not None and not d.empty,
                        )
                        if hist is not None and not hist.empty:
                            price = float(hist["Close"].iloc[-1])
                            result["market_cap_cr"] = (price * shares) / 1e7
                    except Exception:
                        pass

            # --- Free cash flow = Operating Cash Flow − CapEx ---
            if result["free_cashflow"] is None:
                fcf = latest(cf, ["Free Cash Flow"])
                if fcf is None:
                    ocf = latest(cf, ["Operating Cash Flow",
                                      "Cash Flow From Continuing Operating Activities"])
                    capex = latest(cf, ["Capital Expenditure",
                                        "Purchase Of PPE"])
                    if ocf is not None:
                        fcf = ocf - abs(capex) if capex is not None else ocf
                result["free_cashflow"] = fcf

            if result["operating_cashflow"] is None:
                result["operating_cashflow"] = latest(
                    cf, ["Operating Cash Flow",
                         "Cash Flow From Continuing Operating Activities"]
                )

            # ================================================================
            #  CLASSIC QUALITY SIGNALS  (Graham earnings stability, Piotroski)
            # ================================================================
            def annual_row(df, names):
                """Return the most-recent-first list of a statement row, or []."""
                if df is None or df.empty:
                    return []
                for name in names:
                    if name in df.index:
                        vals = df.loc[name].dropna()
                        if len(vals) > 0:
                            try:
                                return [float(v) for v in vals.values]
                            except (TypeError, ValueError):
                                continue
                return []

            ni_hist   = annual_row(inc, ["Net Income",
                                         "Net Income Common Stockholders",
                                         "Net Income From Continuing Operations"])
            ocf_hist  = annual_row(cf, ["Operating Cash Flow",
                                        "Cash Flow From Continuing Operating Activities"])
            ta_hist   = annual_row(bs, ["Total Assets"])
            rev_hist  = annual_row(inc, ["Total Revenue", "Operating Revenue"])
            gp_hist   = annual_row(inc, ["Gross Profit"])
            ltd_hist  = annual_row(bs, ["Long Term Debt",
                                        "Long Term Debt And Capital Lease Obligation"])
            sh_hist   = annual_row(bs, ["Ordinary Shares Number", "Share Issued"])
            ca_hist   = annual_row(bs, ["Current Assets", "Total Current Assets"])
            cl_hist   = annual_row(bs, ["Current Liabilities",
                                        "Total Current Liabilities"])
            ebitda_hist = annual_row(inc, ["EBITDA", "Normalized EBITDA"])

            # --- Latest annual net income (for gates below) ---
            latest_ni = ni_hist[0] if ni_hist else net_income
            result["net_income"] = latest_ni

            # --- Earnings consistency (Graham): any annual loss? ---
            if ni_hist:
                result["annual_periods"] = len(ni_hist)
                result["has_annual_loss"] = any(v <= 0 for v in ni_hist)

            # --- Earnings quality / accruals (Piotroski #4): OCF / NI ---
            latest_ocf = ocf_hist[0] if ocf_hist else result["operating_cashflow"]
            if latest_ocf is not None and latest_ni not in (None, 0) and latest_ni > 0:
                result["ocf_to_ni"] = latest_ocf / latest_ni

            # --- Debt / EBITDA (leverage vs cash earnings, reported) ---
            total_debt = _num(info.get("totalDebt"))
            ebitda_val = _num(info.get("ebitda"))
            if ebitda_val is None and ebitda_hist:
                ebitda_val = ebitda_hist[0]
            if total_debt is not None and ebitda_val and ebitda_val > 0:
                result["debt_to_ebitda"] = total_debt / ebitda_val

            # --- Piotroski F-Score (report-only composite, 0-9) ---
            # Each signal scored only when the underlying data exists, so
            # piotroski_max records how many of the 9 were actually testable.
            def cur_prev(hist):
                return (hist[0], hist[1]) if len(hist) >= 2 else (None, None)

            score = 0
            testable = 0

            # 1. Positive net income
            if latest_ni is not None:
                testable += 1
                if latest_ni > 0:
                    score += 1
            # 2. Positive operating cash flow
            if latest_ocf is not None:
                testable += 1
                if latest_ocf > 0:
                    score += 1
            # 3. OCF > Net Income (accruals)
            if latest_ocf is not None and latest_ni is not None:
                testable += 1
                if latest_ocf > latest_ni:
                    score += 1
            # 4. ROA improving YoY
            ni_c, ni_p = cur_prev(ni_hist)
            ta_c, ta_p = cur_prev(ta_hist)
            if None not in (ni_c, ni_p, ta_c, ta_p) and ta_c > 0 and ta_p > 0:
                testable += 1
                if (ni_c / ta_c) > (ni_p / ta_p):
                    score += 1
            # 5. Leverage (LT-debt / assets) falling YoY
            ltd_c, ltd_p = cur_prev(ltd_hist)
            if None not in (ltd_c, ltd_p, ta_c, ta_p) and ta_c > 0 and ta_p > 0:
                testable += 1
                if (ltd_c / ta_c) < (ltd_p / ta_p):
                    score += 1
            # 6. Current ratio rising YoY
            ca_c, ca_p = cur_prev(ca_hist)
            cl_c, cl_p = cur_prev(cl_hist)
            if None not in (ca_c, ca_p, cl_c, cl_p) and cl_c > 0 and cl_p > 0:
                testable += 1
                if (ca_c / cl_c) > (ca_p / cl_p):
                    score += 1
            # 7. No share dilution (shares not increased)
            sh_c, sh_p = cur_prev(sh_hist)
            if None not in (sh_c, sh_p) and sh_p > 0:
                testable += 1
                if sh_c <= sh_p * 1.01:   # allow 1% noise
                    score += 1
            # 8. Gross margin rising YoY
            gp_c, gp_p = cur_prev(gp_hist)
            rev_c, rev_p = cur_prev(rev_hist)
            if None not in (gp_c, gp_p, rev_c, rev_p) and rev_c > 0 and rev_p > 0:
                testable += 1
                if (gp_c / rev_c) > (gp_p / rev_p):
                    score += 1
            # 9. Asset turnover rising YoY
            if None not in (rev_c, rev_p, ta_c, ta_p) and ta_c > 0 and ta_p > 0:
                testable += 1
                if (rev_c / ta_c) > (rev_p / ta_p):
                    score += 1

            if testable > 0:
                result["piotroski_score"] = score
                result["piotroski_max"] = testable

        except Exception as e:
            logger.debug(f"Error fetching fundamental quality for {symbol}: {e}")

        return result

    def get_stock_info(self, symbol: str) -> dict:
        """Get basic stock info (name, sector, market cap, etc.)."""
        try:
            ticker = self._get_ticker(symbol)
            info = self.get_info(symbol)
            return {
                "long_name": info.get("longName", ""),
                "sector": info.get("sector", ""),
                "industry": info.get("industry", ""),
                "market_cap": info.get("marketCap"),
                "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
            }
        except Exception:
            return {}
