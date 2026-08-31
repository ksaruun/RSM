"""
PE Valuation Filter: Identifies stocks trading below their 3-year median PE.

The rationale: A stock with a current PE below its historical median may be
undervalued relative to its own history — a value signal for swing traders.

Criteria:
- Current trailing PE is available and positive
- 3-year median PE is available
- Current PE < 3-year median PE
"""
import logging
from typing import Optional

import numpy as np

import config

logger = logging.getLogger("SwingScreener.PEValuation")


class PEValuationFilter:
    """Filters stocks where current PE < 3-year median PE."""

    def __init__(self, history_years: int = None, max_pe_absolute: float = None):
        self.history_years = history_years or config.PE_HISTORY_YEARS
        self.max_pe_absolute = max_pe_absolute if max_pe_absolute is not None else config.MAX_PE_ABSOLUTE

    def apply(self, symbol: str, pe_data: dict) -> Optional[dict]:
        """
        Check if a stock's current PE is below its 3-year median PE.

        Args:
            symbol: Stock symbol
            pe_data: Dict with current_pe, historical_pe_values, median_pe

        Returns:
            Dict with PE analysis if the stock passes, None otherwise.
        """
        if not pe_data:
            return None

        current_pe = pe_data.get("current_pe")
        median_pe = pe_data.get("median_pe")
        historical_values = pe_data.get("historical_pe_values", [])

        # Must have valid current PE
        if current_pe is None or current_pe <= 0:
            logger.debug(f"{symbol}: No valid current PE ({current_pe})")
            return None

        tolerance = config.FILTER_TOLERANCE_PCT  # percentage points

        # Absolute PE cap check (if configured)
        # Tolerance: accept up to FILTER_TOLERANCE_PCT % above the absolute cap
        if self.max_pe_absolute is not None:
            pe_cap = self.max_pe_absolute * (1 + tolerance / 100)
            if current_pe > pe_cap:
                logger.debug(
                    f"{symbol}: Current PE ({current_pe:.2f}) > cap+tolerance ({pe_cap:.2f})"
                )
                return None

        # Must have a median PE to compare against
        if median_pe is None or median_pe <= 0:
            logger.debug(f"{symbol}: No valid median PE ({median_pe})")
            return None

        # Core check: current PE must be below 3-year median PE
        # Tolerance: accept up to FILTER_TOLERANCE_PCT % above the median
        pe_ceiling = median_pe * (1 + tolerance / 100)
        if current_pe > pe_ceiling:
            logger.debug(
                f"{symbol}: Current PE ({current_pe:.2f}) > median+tolerance ({pe_ceiling:.2f}). Skipped."
            )
            return None

        # Calculate discount percentage
        pe_discount_pct = ((median_pe - current_pe) / median_pe) * 100

        # Additional stats
        pe_min = min(historical_values) if historical_values else None
        pe_max = max(historical_values) if historical_values else None
        pe_std = float(np.std(historical_values)) if len(historical_values) > 1 else None

        result = {
            "symbol": symbol,
            "current_pe": round(current_pe, 2),
            "median_pe_3yr": round(median_pe, 2),
            "pe_discount_pct": round(pe_discount_pct, 2),
            "pe_min_3yr": round(pe_min, 2) if pe_min else None,
            "pe_max_3yr": round(pe_max, 2) if pe_max else None,
            "pe_std_3yr": round(pe_std, 2) if pe_std else None,
            "pe_data_points": len(historical_values),
        }

        logger.debug(
            f"{symbol}: PE={current_pe:.2f} vs Median={median_pe:.2f} "
            f"(discount={pe_discount_pct:.1f}%)"
        )

        return result
