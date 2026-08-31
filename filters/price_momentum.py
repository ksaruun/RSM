"""
Price Momentum Filter: two complementary swing-trade signals in one pass.

1. 52-Week High Pullback
   The stock must have pulled back from its 52-week high by at least
   MIN_PULLBACK_PCT and at most MAX_PULLBACK_PCT.
   Rationale: a stock 10-30% below its annual peak still has a proven
   high-water mark and is entering a potential re-entry zone after the
   dip-and-recover (golden cross) pattern.

2. Beta (optional)
   The stock's beta versus the market must be >= MIN_BETA and <= MAX_BETA.
   Higher beta means the stock moves more than the market — desirable for
   swing trades where you want amplified moves.
   This filter is disabled if ENABLE_BETA_FILTER = False in config.

Both checks share the same yfinance ``info`` dict, so they are bundled into
one filter to avoid a double network round-trip.
"""
import logging
import math
from typing import Optional

import config

logger = logging.getLogger("SwingScreener.PriceMomentum")


class PriceMomentumFilter:
    """
    Checks 52-week high pullback range and (optionally) minimum beta.
    """

    def __init__(
        self,
        enable_pullback: bool = None,
        min_pullback_pct: float = None,
        max_pullback_pct: float = None,
        enable_beta: bool = None,
        min_beta: float = None,
        max_beta: float = None,
        bluechip_min_pullback_pct: float = None,
    ):
        self.enable_pullback  = enable_pullback  if enable_pullback  is not None else config.ENABLE_PULLBACK_FILTER
        self.min_pullback_pct = min_pullback_pct if min_pullback_pct is not None else config.MIN_PULLBACK_PCT
        self.max_pullback_pct = max_pullback_pct if max_pullback_pct is not None else config.MAX_PULLBACK_PCT
        self.enable_beta      = enable_beta      if enable_beta      is not None else config.ENABLE_BETA_FILTER
        self.min_beta         = min_beta         if min_beta         is not None else config.MIN_BETA
        self.max_beta         = max_beta         if max_beta         is not None else config.MAX_BETA
        # When set, stocks with MCap >= config.BLUECHIP_MCAP_CR use this lower
        # pullback floor instead of min_pullback_pct. None disables the relaxation
        # (e.g. swing mode); positional mode passes the config value.
        self.bluechip_min_pullback_pct = bluechip_min_pullback_pct

    def apply(self, symbol: str, momentum_data: dict) -> Optional[dict]:
        """
        Check 52-week pullback and beta criteria.

        Args:
            symbol: Stock symbol
            momentum_data: Dict from DataFetcher.get_price_momentum_data()
                Keys: current_price, week52_high, week52_low, beta

        Returns:
            Dict with momentum metrics if the stock passes, None otherwise.
        """
        if not momentum_data:
            return None

        current_price = momentum_data.get("current_price")
        week52_high   = momentum_data.get("week52_high")
        week52_low    = momentum_data.get("week52_low")
        beta          = momentum_data.get("beta")
        market_cap_cr = momentum_data.get("market_cap_cr")

        # ── 52-Week High Pullback ──────────────────────────────────────────
        if self.enable_pullback:
            if current_price is None or week52_high is None:
                logger.debug(f"{symbol}: Missing price/52W data for pullback check")
                return None

            if math.isnan(current_price) or math.isnan(week52_high):
                return None

            if week52_high <= 0:
                return None

            pullback_pct = ((week52_high - current_price) / week52_high) * 100

            tolerance = config.FILTER_TOLERANCE_PCT  # percentage points

            # Bluechips (large-caps) may enter on a shallower dip.
            effective_min_pullback = self.min_pullback_pct
            is_bluechip = (
                self.bluechip_min_pullback_pct is not None
                and market_cap_cr is not None
                and market_cap_cr >= config.BLUECHIP_MCAP_CR
            )
            if is_bluechip:
                effective_min_pullback = self.bluechip_min_pullback_pct

            pullback_floor   = effective_min_pullback - tolerance
            pullback_ceiling = self.max_pullback_pct + tolerance  # e.g. 30% → 30.5%

            if pullback_pct < pullback_floor:
                logger.debug(
                    f"{symbol}: Pullback too small ({pullback_pct:.1f}% < floor {pullback_floor:.1f}%"
                    + (" [bluechip]" if is_bluechip else "") + ")"
                )
                return None

            if pullback_pct > pullback_ceiling:
                logger.debug(
                    f"{symbol}: Pullback too large ({pullback_pct:.1f}% > ceiling {pullback_ceiling:.1f}%)"
                )
                return None
        else:
            pullback_pct = (
                ((week52_high - current_price) / week52_high) * 100
                if current_price and week52_high and week52_high > 0
                else None
            )

        # ── Beta Filter ───────────────────────────────────────────────────
        if self.enable_beta:
            if beta is None or math.isnan(beta):
                # If beta is unavailable, pass with a warning (data gap)
                logger.debug(f"{symbol}: Beta unavailable, skipping beta check")
            else:
                if beta < self.min_beta:
                    logger.debug(
                        f"{symbol}: Beta too low ({beta:.2f} < {self.min_beta})"
                    )
                    return None

                if beta > self.max_beta:
                    logger.debug(
                        f"{symbol}: Beta too high ({beta:.2f} > {self.max_beta})"
                    )
                    return None

        result = {
            "symbol": symbol,
            "week52_high": week52_high,
            "week52_low": week52_low,
            "pullback_from_52w_high_pct": round(pullback_pct, 2) if pullback_pct is not None else None,
            "beta": round(beta, 3) if beta is not None and not math.isnan(beta) else None,
        }

        logger.debug(
            f"{symbol}: Pullback={pullback_pct:.1f}% | Beta={beta}"
        )
        return result
