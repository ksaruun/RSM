"""
Quarterly Growth Filter: Checks for compound sales and profit growth
over the last 4 quarters.

Criteria:
- Last 4 quarters must all have positive revenue and net income
- Compound Annual Growth Rate (CAGR) for revenue >= MIN_COMPOUND_GROWTH_PCT
- Compound Annual Growth Rate (CAGR) for profit >= MIN_COMPOUND_GROWTH_PCT
- YoY growth (where available) must be positive for both metrics
- Overall trend across available quarters must be positive

Strategy:
1. Primary: YoY comparison (quarter vs same quarter last year) - most reliable
2. Fallback: TTM comparison (trailing 12 months vs prior available period)
3. All recent quarters must have positive net income (profitable)
4. CAGR calculation: (Ending / Beginning)^(1/n) - 1, where n = years

BLUECHIP RELAXATION
-------------------
Very large companies (market cap >= config.BLUECHIP_MCAP_CR, default ₹1L Cr)
often have only 3 quarters available in yfinance due to reporting lags or API
data gaps. Rather than rejecting an HDFC Bank-sized company over one missing
quarter, the filter accepts BLUECHIP_MIN_QUARTERS (default 3) for bluechips.
The CAGR floor is unchanged — the stock must still show adequate growth on
whatever data is available. A log note records the relaxation for transparency.
"""
import logging
from typing import Optional

import numpy as np
import pandas as pd

import config

logger = logging.getLogger("SwingScreener.QuarterlyGrowth")


class QuarterlyGrowthFilter:
    """Filters stocks with compound sales and profit growth over recent quarters."""

    def __init__(self, min_quarters: int = None, min_compound_growth_pct: float = None):
        self.min_quarters = min_quarters or config.MIN_QUARTERS
        self.min_compound_growth_pct = min_compound_growth_pct or config.MIN_COMPOUND_GROWTH_PCT

    def _calculate_cagr(self, start_value: float, end_value: float, periods: int) -> float:
        """
        Calculate Compound Annual Growth Rate (CAGR).

        CAGR = (Ending / Beginning)^(1/n) - 1, where n is number of years.
        For quarterly data, n = periods / 4.
        """
        if start_value <= 0 or periods <= 0:
            return 0.0
        years = periods / 4.0  # Convert quarters to years
        cagr = ((end_value / start_value) ** (1.0 / years)) - 1.0
        return cagr * 100  # Return as percentage

    def apply(
        self,
        symbol: str,
        quarterly_financials: pd.DataFrame,
        market_cap_cr: float = None,
    ) -> Optional[dict]:
        """
        Check if a stock has positive sales and profit growth
        over the last N quarters.

        Args:
            symbol: Stock symbol
            quarterly_financials: DataFrame from yfinance quarterly_income_stmt
            market_cap_cr: Market capitalisation in ₹ crore (optional).  When
                provided and the stock is a bluechip (>= config.BLUECHIP_MCAP_CR),
                the minimum-quarters requirement is relaxed to
                config.BLUECHIP_MIN_QUARTERS so a single missing quarter doesn't
                disqualify an otherwise strong large-cap.

        Returns:
            Dict with growth analysis if the stock passes, None otherwise.
        """
        if quarterly_financials is None or quarterly_financials.empty:
            return None

        # --- Determine effective minimum quarters ---
        is_bluechip = (
            market_cap_cr is not None
            and market_cap_cr >= config.BLUECHIP_MCAP_CR
        )
        effective_min_q = (
            config.BLUECHIP_MIN_QUARTERS if is_bluechip else self.min_quarters
        )

        try:
            qf = quarterly_financials

            # Find revenue row
            revenue_row = self._find_row(qf, [
                "Total Revenue", "Revenue", "Operating Revenue",
                "Total Operating Revenue", "Net Revenue",
            ])

            # Find profit row
            profit_row = self._find_row(qf, [
                "Net Income", "Net Income Common Stockholders",
                "Net Income From Continuing Operations",
                "Net Income From Continuing Operation Net Minority Interest",
            ])

            if revenue_row is None:
                logger.debug(f"{symbol}: No revenue row found")
                return None
            if profit_row is None:
                logger.debug(f"{symbol}: No profit row found")
                return None

            # Extract and clean series (sorted by date, oldest first)
            revenues = qf.loc[revenue_row].sort_index().dropna()
            profits = qf.loc[profit_row].sort_index().dropna()

            if len(revenues) < effective_min_q or len(profits) < effective_min_q:
                if is_bluechip:
                    logger.debug(
                        f"{symbol}: Insufficient data even with bluechip relaxation "
                        f"(rev={len(revenues)}, prof={len(profits)}, need={effective_min_q})"
                    )
                else:
                    logger.debug(
                        f"{symbol}: Insufficient data (rev={len(revenues)}, "
                        f"prof={len(profits)}, need={effective_min_q})"
                    )
                return None

            if is_bluechip and len(revenues) < self.min_quarters:
                logger.debug(
                    f"{symbol}: Bluechip (MCap ₹{market_cap_cr:,.0f}Cr >= ₹{config.BLUECHIP_MCAP_CR:,.0f}Cr) — "
                    f"relaxed from {self.min_quarters} to {effective_min_q} quarters "
                    f"(only {len(revenues)} available)"
                )

            # Get the last N quarters
            recent_rev = revenues.iloc[-effective_min_q:]
            recent_prof = profits.iloc[-effective_min_q:]

            # Check 1: All recent quarters must have positive revenue and profit
            if not all(float(v) > 0 for v in recent_rev.values):
                logger.debug(f"{symbol}: Not all recent quarters have positive revenue")
                return None
            if not all(float(v) > 0 for v in recent_prof.values):
                logger.debug(f"{symbol}: Not all recent quarters have positive profit")
                return None

            # Check 2: YoY growth for each quarter where prior year data is available
            rev_yoy_growth = []
            prof_yoy_growth = []

            for i in range(len(revenues) - 1, max(len(revenues) - self.min_quarters - 1, -1), -1):
                if i < 0:
                    break
                # Find the quarter ~1 year back (index i-4)
                if i - 4 >= 0:
                    cur_rev = float(revenues.iloc[i])
                    prev_rev = float(revenues.iloc[i - 4])
                    if prev_rev > 0:
                        rev_yoy_growth.append(((cur_rev - prev_rev) / prev_rev) * 100)

                    cur_prof = float(profits.iloc[i])
                    prev_prof = float(profits.iloc[i - 4])
                    if abs(prev_prof) > 0:
                        prof_yoy_growth.append(((cur_prof - prev_prof) / abs(prev_prof)) * 100)

            tolerance = config.FILTER_TOLERANCE_PCT  # percentage points

            # Check 3: If we have YoY data, it must show positive growth
            # Tolerance: allow avg growth down to -FILTER_TOLERANCE_PCT%
            has_yoy = len(rev_yoy_growth) > 0 and len(prof_yoy_growth) > 0
            if has_yoy:
                avg_rev_yoy = np.mean(rev_yoy_growth)
                avg_prof_yoy = np.mean(prof_yoy_growth)
                if avg_rev_yoy < -tolerance:
                    logger.debug(f"{symbol}: Avg YoY revenue growth {avg_rev_yoy:.1f}% < -{tolerance}%")
                    return None
                if avg_prof_yoy < -tolerance:
                    logger.debug(f"{symbol}: Avg YoY profit growth {avg_prof_yoy:.1f}% < -{tolerance}%")
                    return None
            else:
                # No YoY data: check overall trend (latest vs earliest)
                first_rev, last_rev = float(recent_rev.iloc[0]), float(recent_rev.iloc[-1])
                first_prof, last_prof = float(recent_prof.iloc[0]), float(recent_prof.iloc[-1])

                avg_rev_yoy = ((last_rev - first_rev) / first_rev) * 100 if first_rev > 0 else 0
                avg_prof_yoy = ((last_prof - first_prof) / abs(first_prof)) * 100 if first_prof != 0 else 0

                if avg_rev_yoy < -tolerance:
                    logger.debug(f"{symbol}: Revenue trend {avg_rev_yoy:.1f}% < -{tolerance}%")
                    return None
                if avg_prof_yoy < -tolerance:
                    logger.debug(f"{symbol}: Profit trend {avg_prof_yoy:.1f}% < -{tolerance}%")
                    return None

                rev_yoy_growth = [avg_rev_yoy]
                prof_yoy_growth = [avg_prof_yoy]

            # Check 4: CAGR must meet minimum threshold
            # Tolerance: accept CAGR down to (threshold - FILTER_TOLERANCE_PCT)
            first_rev, last_rev = float(recent_rev.iloc[0]), float(recent_rev.iloc[-1])
            first_prof, last_prof = float(recent_prof.iloc[0]), float(recent_prof.iloc[-1])

            rev_cagr = self._calculate_cagr(first_rev, last_rev, effective_min_q)
            prof_cagr = self._calculate_cagr(abs(first_prof), last_prof, effective_min_q)

            cagr_floor = self.min_compound_growth_pct - tolerance
            if rev_cagr < cagr_floor:
                logger.debug(f"{symbol}: Revenue CAGR {rev_cagr:.1f}% < floor {cagr_floor:.1f}%")
                return None
            if prof_cagr < cagr_floor:
                logger.debug(f"{symbol}: Profit CAGR {prof_cagr:.1f}% < floor {cagr_floor:.1f}%")
                return None

            latest_revenue = float(revenues.iloc[-1])
            latest_profit = float(profits.iloc[-1])

            result = {
                "symbol": symbol,
                "revenue_growth_4q": [round(g, 2) for g in rev_yoy_growth],
                "profit_growth_4q": [round(g, 2) for g in prof_yoy_growth],
                "avg_revenue_growth_pct": round(np.mean(rev_yoy_growth), 2),
                "avg_profit_growth_pct": round(np.mean(prof_yoy_growth), 2),
                "revenue_cagr_pct": round(rev_cagr, 2),
                "profit_cagr_pct": round(prof_cagr, 2),
                "latest_quarterly_revenue": latest_revenue,
                "latest_quarterly_profit": latest_profit,
                "quarters_analyzed": effective_min_q,
                "bluechip_relaxed": is_bluechip and effective_min_q < self.min_quarters,
                "comparison_type": "YoY" if has_yoy else "Trend",
            }

            logger.debug(
                f"{symbol}: Rev Growth={np.mean(rev_yoy_growth):.1f}%, "
                f"Prof Growth={np.mean(prof_yoy_growth):.1f}% "
                f"({'YoY' if has_yoy else 'Trend'})"
            )

            return result

        except Exception as e:
            logger.debug(f"Quarterly growth analysis failed for {symbol}: {e}")
            return None

    def _find_row(self, df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
        """Find a row in the DataFrame matching one of the candidate names."""
        for name in candidates:
            if name in df.index:
                return name
            for idx in df.index:
                if str(idx).lower().strip() == name.lower().strip():
                    return idx
        return None
