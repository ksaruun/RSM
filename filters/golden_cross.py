"""
Golden Cross Filter: Detects a genuine bullish 50/200 DMA crossover.

Two conditions (either satisfies):

  Option A — Fresh Golden Cross (last 15 trading days):
    • 50 DMA crossed ABOVE 200 DMA within the last 15 trading days.
    • At the moment of crossing, the spread must be >= MIN_CROSS_SPREAD_PCT
      (avoids razor-thin noise crosses like ±0.05%).
    • The prior crossover before this one must NOT be a golden cross
      (i.e., the stock was genuinely in a death-cross state before now).

  Option B — Sustained Golden Cross (last 2 quarters, ~126 trading days):
    • NO death cross (50 DMA dropped below 200 DMA) has occurred in the
      last 2 quarters.
    • 50 DMA is currently above 200 DMA.

  Both options also require:
    • 50 DMA > 200 DMA today.
    • Current price >= 200 DMA (price has not fallen back below the
      long-term moving average — filters out failed/reversed breakouts
      like CCAVENUE where price dropped below both DMAs after the cross).
    • Minimum current DMA spread: 50 DMA must be at least
      MIN_CURRENT_SPREAD_PCT above 200 DMA.
"""
import logging
import math
from typing import Optional

import pandas as pd

import config

logger = logging.getLogger("SwingScreener.GoldenCross")

# ~1 quarter of trading days
QUARTER_TRADING_DAYS = 63
# 2 quarters
TWO_QUARTER_DAYS = 126
# Fresh crossover window (Option A)
RECENT_CROSSOVER_DAYS = 15
# Minimum DMA spread at the moment of crossover to count as valid (not noise)
MIN_CROSS_SPREAD_PCT = 0.10    # 0.10% minimum spread at crossing point
# Minimum current DMA spread (avoids stocks where DMAs are about to reverse)
MIN_CURRENT_SPREAD_PCT = 0.20  # 0.20% minimum current spread


class GoldenCrossFilter:
    """
    Filters stocks that show a confirmed golden cross pattern:
      Option A: Fresh golden cross within the last 15 trading days.
      Option B: Sustained golden cross — no death cross in last 2 quarters.
    Both require price >= 200 DMA and a minimum DMA spread.
    """

    def __init__(
        self,
        short_period: int = None,
        long_period: int = None,
    ):
        self.short_period = short_period or config.SHORT_DMA_PERIOD
        self.long_period = long_period or config.LONG_DMA_PERIOD

    def apply(self, symbol: str, price_data: pd.DataFrame) -> Optional[dict]:
        """
        Check if a stock satisfies the refined golden cross criteria.

        Returns:
            Dict with analysis results if the stock passes, None otherwise.
        """
        # Minimum data needed: enough to compute the long DMA + a small crossover buffer.
        # TWO_QUARTER_DAYS is NOT required here — recently listed stocks with fewer
        # data points are still analysed over their available history.
        min_data = self.long_period + RECENT_CROSSOVER_DAYS + 1
        if price_data is None or len(price_data) < min_data:
            return None

        try:
            close = price_data["Close"].squeeze()

            # Calculate moving averages
            dma_short = close.rolling(window=self.short_period).mean()
            dma_long  = close.rolling(window=self.long_period).mean()

            # Current values
            current_short = float(dma_short.iloc[-1])
            current_long  = float(dma_long.iloc[-1])
            current_price = float(close.iloc[-1])

            if math.isnan(current_short) or math.isnan(current_long) or math.isnan(current_price):
                return None

            tolerance = config.FILTER_TOLERANCE_PCT  # percentage points

            # ----------------------------------------------------------------
            # Mandatory gate 1: 50 DMA must be above 200 DMA right now
            # (No tolerance here — this is the definition of a golden cross)
            # ----------------------------------------------------------------
            if current_short <= current_long:
                logger.debug(f"{symbol}: 50DMA ({current_short:.2f}) <= 200DMA ({current_long:.2f}) — not golden")
                return None

            # ----------------------------------------------------------------
            # Mandatory gate 2: Current price must be >= 200 DMA
            # Tolerance: price may be up to FILTER_TOLERANCE_PCT% below 200 DMA
            # (catches stocks that are just barely below the long-term MA)
            # ----------------------------------------------------------------
            price_floor = current_long * (1 - tolerance / 100)
            if current_price < price_floor:
                logger.debug(
                    f"{symbol}: Price ({current_price:.2f}) < 200DMA×(1-{tolerance}%) "
                    f"({price_floor:.2f}) — failed breakout, excluding."
                )
                return None

            # ----------------------------------------------------------------
            # Mandatory gate 3: Minimum current DMA spread
            # Tolerance: effective floor is reduced by FILTER_TOLERANCE_PCT
            # ----------------------------------------------------------------
            current_spread_pct = ((current_short - current_long) / current_long) * 100
            effective_spread_floor = max(0.0, MIN_CURRENT_SPREAD_PCT - tolerance)
            if current_spread_pct < effective_spread_floor:
                logger.debug(
                    f"{symbol}: DMA spread {current_spread_pct:.2f}% < "
                    f"floor {effective_spread_floor:.2f}% — too thin"
                )
                return None

            # ----------------------------------------------------------------
            # Find all crossovers in the full history (we need the 2 most
            # recent ones to classify Option A vs Option B)
            # ----------------------------------------------------------------
            n = len(dma_short)

            crossovers = []   # list of (idx, kind) where kind is 'golden' or 'death'
            for i in range(1, n):
                ps = float(dma_short.iloc[i - 1])
                pl = float(dma_long.iloc[i - 1])
                cs = float(dma_short.iloc[i])
                cl = float(dma_long.iloc[i])
                if math.isnan(ps) or math.isnan(pl) or math.isnan(cs) or math.isnan(cl):
                    continue
                if ps <= pl and cs > cl:
                    crossovers.append((i, "golden"))
                elif ps >= pl and cs < cl:
                    crossovers.append((i, "death"))

            # ----------------------------------------------------------------
            # Option A: Fresh golden cross within last RECENT_CROSSOVER_DAYS
            # ----------------------------------------------------------------
            option_a = False
            crossover_idx = None
            crossover_date = None
            days_since_crossover = None

            recent_golden = None
            for idx, kind in reversed(crossovers):
                if kind == "golden":
                    recent_golden = idx
                    break

            if recent_golden is not None:
                days_ago = n - 1 - recent_golden
                if days_ago <= RECENT_CROSSOVER_DAYS:
                    # Verify the cross spread was meaningful (not noise)
                    cross_s = float(dma_short.iloc[recent_golden])
                    cross_l = float(dma_long.iloc[recent_golden])
                    cross_spread_pct = ((cross_s - cross_l) / cross_l) * 100
                    effective_cross_floor = max(0.0, MIN_CROSS_SPREAD_PCT - tolerance)
                    if cross_spread_pct >= effective_cross_floor:
                        # Verify the crossover before this was a death cross
                        # (i.e., stock was genuinely bearish before turning golden)
                        prev_kind = None
                        for idx2, kind2 in reversed(crossovers):
                            if idx2 < recent_golden:
                                prev_kind = kind2
                                break
                        # Accept if previous cross was a death cross, OR if
                        # this is the first crossover ever recorded (long-term golden)
                        if prev_kind is None or prev_kind == "death":
                            option_a = True
                            crossover_idx = recent_golden
                            crossover_date = price_data.index[recent_golden]
                            days_since_crossover = days_ago
                    else:
                        logger.debug(
                            f"{symbol}: Recent golden cross {days_ago}d ago has "
                            f"thin spread {cross_spread_pct:.2f}% < floor {effective_cross_floor:.2f}% — rejected"
                        )

            # ----------------------------------------------------------------
            # Option B: Sustained golden cross — no death cross in last 2Q,
            # AND the most recent golden cross is well established (> 15 days).
            #
            # If the golden cross happened within 15 days but failed Option A
            # (e.g. too-thin spread), Option B must NOT rescue it — that would
            # be a false signal (e.g. BASF: golden cross 7d ago, 0.036% spread).
            # ----------------------------------------------------------------
            option_b = False
            window_start_idx = max(n - TWO_QUARTER_DAYS, self.long_period)

            # Check if any death cross occurred in the last 2 quarters
            death_in_2q = any(
                kind == "death" and idx >= window_start_idx
                for idx, kind in crossovers
            )

            if not death_in_2q:
                # Determine age of the most recent golden cross
                if recent_golden is None:
                    # No golden cross in full history — stock has been in a
                    # sustained golden cross since inception (long-running)
                    option_b = True
                else:
                    days_since_golden = n - 1 - recent_golden
                    if days_since_golden > RECENT_CROSSOVER_DAYS:
                        # Golden cross is established (> 15 days old) — valid
                        option_b = True
                        if crossover_idx is None:
                            crossover_idx = recent_golden
                            crossover_date = price_data.index[recent_golden]
                            days_since_crossover = days_since_golden
                    # else: fresh cross (≤15d) — evaluated only via Option A;
                    # don't let a thin-spread fresh cross slip through Option B

            # ----------------------------------------------------------------
            # Final decision
            # ----------------------------------------------------------------
            if not option_a and not option_b:
                reason = "no fresh cross (15d) and death cross found in last 2 quarters"
                logger.debug(f"{symbol}: FAIL — {reason}")
                return None

            pass_type = "fresh" if option_a else "sustained"

            result = {
                "symbol": symbol,
                "current_price": round(current_price, 2),
                "dma_50": round(current_short, 2),
                "dma_200": round(current_long, 2),
                "dma_spread_pct": round(current_spread_pct, 2),
                "crossover_date": str(crossover_date.date()) if crossover_date else None,
                "days_since_crossover": days_since_crossover if days_since_crossover is not None else 999,
                "is_fresh_crossover": option_a,
                "price_above_both_dma": current_price >= current_short,
                "pass_type": pass_type,
            }

            logger.debug(
                f"{symbol}: PASS ({pass_type}) | 50DMA={current_short:.2f} > "
                f"200DMA={current_long:.2f} (spread {current_spread_pct:.2f}%), "
                f"XD={days_since_crossover}d ago, price={current_price:.2f}"
            )

            return result

        except Exception as e:
            logger.debug(f"Golden cross analysis failed for {symbol}: {e}")
            return None
