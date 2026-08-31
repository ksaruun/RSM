"""
Price Breakout Filter (momentum mode): stocks making fresh highs.

A stock passes when it is BOTH:
  1. Near its 52-week high  — current close within MOM_NEAR_52W_HIGH_PCT of the
     highest close over the last ~252 trading days.
  2. At a fresh N-day high  — current close is the highest close over the last
     MOM_BREAKOUT_LOOKBACK_DAYS (~20 = one month), i.e. a genuine breakout.

Both together capture stocks in a sustained uptrend that are also breaking out
right now, rather than drifting sideways just below an old peak.

The filter also computes momentum-strength metrics used later for ranking:
3-month and 6-month price returns, distance below the 52-week high, and the
premium of price over its 200-day moving average.
"""
import logging
import math
from typing import Optional

import pandas as pd

import config

logger = logging.getLogger("SwingScreener.PriceBreakout")


class PriceBreakoutFilter:
    """Passes stocks that are near their 52W high and breaking to a new N-day high."""

    def __init__(
        self,
        near_52w_high_pct: float = None,
        breakout_lookback_days: int = None,
    ):
        self.near_52w_high_pct = (
            near_52w_high_pct if near_52w_high_pct is not None
            else config.MOM_NEAR_52W_HIGH_PCT
        )
        self.breakout_lookback_days = (
            breakout_lookback_days if breakout_lookback_days is not None
            else config.MOM_BREAKOUT_LOOKBACK_DAYS
        )

    def apply(self, symbol: str, price_data: pd.DataFrame) -> Optional[dict]:
        """
        Args:
            symbol: Stock symbol
            price_data: Daily OHLCV DataFrame (ascending dates) from
                DataFetcher.get_historical_prices().

        Returns:
            Dict of breakout + momentum metrics if the stock passes, else None.
        """
        lookback = self.breakout_lookback_days
        if price_data is None or len(price_data) < lookback + 1:
            return None

        try:
            close = price_data["Close"].squeeze()
            current = float(close.iloc[-1])
            if math.isnan(current) or current <= 0:
                return None

            tol = config.FILTER_TOLERANCE_PCT  # percentage points

            # --- 52-week high (use up to 252 trading days) ---
            window_52w = close.iloc[-252:] if len(close) >= 252 else close
            high_52w = float(window_52w.max())
            if high_52w <= 0:
                return None
            dist_from_52w_high = ((high_52w - current) / high_52w) * 100  # >= 0

            near_ceiling = self.near_52w_high_pct + tol
            if dist_from_52w_high > near_ceiling:
                logger.debug(
                    f"{symbol}: {dist_from_52w_high:.1f}% below 52W high "
                    f"(> {near_ceiling:.1f}% ceiling) — not near high"
                )
                return None

            # --- Fresh N-day high (breakout) ---
            window_nd = close.iloc[-lookback:]
            high_nd = float(window_nd.max())
            # current must be at/above the highest close in the window (with a
            # tiny tolerance so an exact tie / rounding still counts).
            if current < high_nd * (1 - tol / 100):
                logger.debug(
                    f"{symbol}: not a fresh {lookback}-day high "
                    f"({current:.2f} < {high_nd:.2f})"
                )
                return None

            # --- Momentum-strength metrics (for ranking) ---
            def ret_over(days: int) -> Optional[float]:
                if len(close) <= days:
                    return None
                past = float(close.iloc[-days - 1])
                return ((current - past) / past) * 100 if past > 0 else None

            ret_3m = ret_over(config.MOM_RETURN_SHORT_DAYS)
            ret_6m = ret_over(config.MOM_RETURN_LONG_DAYS)

            # Premium over the 200-day moving average (trend confirmation).
            above_200dma_pct = None
            if len(close) >= config.LONG_DMA_PERIOD:
                dma200 = float(close.rolling(config.LONG_DMA_PERIOD).mean().iloc[-1])
                if not math.isnan(dma200) and dma200 > 0:
                    above_200dma_pct = ((current - dma200) / dma200) * 100

            result = {
                "symbol": symbol,
                "current_price": round(current, 2),
                "high_52w": round(high_52w, 2),
                "high_20d": round(high_nd, 2),
                "breakout_lookback_days": lookback,
                "dist_from_52w_high_pct": round(dist_from_52w_high, 2),
                "ret_3m_pct": round(ret_3m, 2) if ret_3m is not None else None,
                "ret_6m_pct": round(ret_6m, 2) if ret_6m is not None else None,
                "above_200dma_pct": round(above_200dma_pct, 2) if above_200dma_pct is not None else None,
            }
            logger.debug(
                f"{symbol}: breakout OK — {dist_from_52w_high:.1f}% off 52W high, "
                f"3M {ret_3m}, 6M {ret_6m}"
            )
            return result

        except Exception as e:
            logger.debug(f"Breakout check failed for {symbol}: {e}")
            return None
