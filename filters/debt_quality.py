"""
Debt Quality Filter: Checks for low leverage and low promoter pledging.

Criteria (non-financial stocks):
- Debt-to-Equity ratio < 0.5  (conservative balance sheet)
- Promoter pledged shares < 5% (low insider risk)

For banks, NBFCs, insurance and AMCs:
- D/E ceiling is raised to POS_BANK_MAX_DEBT_TO_EQUITY (default 10x) because
  leverage is intrinsic to their business model.
- A missing D/E is treated as a pass (benefit of doubt) rather than a reject,
  since yfinance often cannot compute it for bank balance sheets.

Data sources:
- D/E ratio: yfinance ``info['debtToEquity']`` or computed from balance sheet
- Pledged %: NSE India corporate-filings API, with fallback to yfinance
  insider data heuristics
- is_financial_sector flag: passed in via debt_data (set by screener from
  the fundamental quality result)
"""
import logging
import math
from typing import Optional

import numpy as np
import pandas as pd

import config
from filters.fundamental_quality import is_financial_sector

logger = logging.getLogger("SwingScreener.DebtQuality")

MAX_DEBT_TO_EQUITY = 0.5   # Ratio (not percentage)
MAX_PLEDGED_PCT = 5.0       # Percentage


class DebtQualityFilter:
    """Filters stocks with low D/E ratio and low promoter pledging."""

    def __init__(
        self,
        max_de: float = MAX_DEBT_TO_EQUITY,
        max_pledged: float = MAX_PLEDGED_PCT,
    ):
        self.max_de = max_de
        self.max_pledged = max_pledged

    def apply(
        self,
        symbol: str,
        debt_data: dict,
        sector: str = "",
        industry: str = "",
    ) -> Optional[dict]:
        """
        Check if a stock passes the debt quality criteria.

        Args:
            symbol: Stock symbol
            debt_data: Dict from DataFetcher.get_debt_quality_data()
            sector: yfinance sector string (used for bank detection)
            industry: yfinance industry string (used for bank detection)

        Returns:
            Dict with analysis if the stock passes, None otherwise.
        """
        if not debt_data:
            return None

        # Honour the flag set upstream by the quality filter, or re-detect.
        is_fin = debt_data.get("is_financial_sector") or is_financial_sector(sector, industry)

        de_ratio = debt_data.get("debt_to_equity")
        pledged_pct = debt_data.get("pledged_pct")

        tolerance = config.FILTER_TOLERANCE_PCT  # percentage points

        # --- Debt-to-Equity check ---
        if de_ratio is None or (isinstance(de_ratio, float) and math.isnan(de_ratio)):
            if is_fin:
                # Banks often have unparseable balance sheets in yfinance;
                # missing D/E is not a disqualifier for financial stocks.
                logger.debug(
                    f"{symbol}: D/E unavailable (financial sector) — skipping D/E gate"
                )
            else:
                logger.debug(f"{symbol}: D/E ratio unavailable")
                return None
        elif de_ratio < 0:
            logger.debug(f"{symbol}: Invalid D/E ratio ({de_ratio})")
            return None
        else:
            # Use the bank-specific ceiling for financial stocks.
            effective_max_de = (
                config.POS_BANK_MAX_DEBT_TO_EQUITY if is_fin else self.max_de
            )
            # D/E tolerance: accept up to effective_max_de × (1 + tolerance%)
            de_ceiling = effective_max_de * (1 + tolerance / 100)
            if de_ratio > de_ceiling:
                logger.debug(
                    f"{symbol}: D/E too high ({de_ratio:.3f} > ceiling {de_ceiling:.3f}"
                    + (" [bank limit]" if is_fin else "") + ")"
                )
                return None

        # --- Pledged percentage check ---
        # Tolerance: accept up to (max_pledged + tolerance) percentage points
        # If pledged data is unavailable, we pass the stock (benefit of doubt).
        if pledged_pct is not None and not math.isnan(pledged_pct):
            pledged_ceiling = self.max_pledged + tolerance
            if pledged_pct > pledged_ceiling:
                logger.debug(
                    f"{symbol}: Pledged % too high ({pledged_pct:.2f}% > ceiling {pledged_ceiling:.2f}%)"
                )
                return None

        result = {
            "symbol": symbol,
            "debt_to_equity": round(de_ratio, 3),
            "pledged_pct": round(pledged_pct, 2) if pledged_pct is not None and not math.isnan(pledged_pct) else 0.0,
            "total_debt": debt_data.get("total_debt"),
            "total_equity": debt_data.get("total_equity"),
        }

        logger.debug(
            f"{symbol}: D/E={de_ratio:.3f}, Pledged={pledged_pct if pledged_pct else 0:.1f}%"
        )

        return result
