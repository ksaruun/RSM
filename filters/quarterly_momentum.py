"""
Quarterly Momentum (QoQ Results) Filter — momentum mode.

Confirms the business still has fundamental momentum behind the price move: the
most recent quarter must show sequential (quarter-over-quarter) improvement.

Modes (config.MOM_QOQ_MODE):
  "either"       — pass if revenue OR net profit rose vs the previous quarter.
  "both"         — require both revenue AND profit up QoQ.
  "both_and_yoy" — both up QoQ, and the latest quarter also beats the same
                   quarter a year ago (needs >= 5 quarters; filters seasonality).

Reports qoq_revenue_pct / qoq_profit_pct (and YoY equivalents where available)
for use in the momentum ranking.
"""
import logging
from typing import Optional

import pandas as pd

import config

logger = logging.getLogger("SwingScreener.QuarterlyMomentum")


class QoQResultsFilter:
    """Passes stocks whose latest quarter shows QoQ results momentum."""

    def __init__(self, mode: str = None):
        self.mode = (mode or config.MOM_QOQ_MODE or "either").lower().strip()

    def _row(self, df: pd.DataFrame, names: list[str]) -> Optional[pd.Series]:
        for name in names:
            if name in df.index:
                vals = df.loc[name].sort_index().dropna()  # ascending by date
                if len(vals) >= 2:
                    return vals
        return None

    def apply(self, symbol: str, quarterly_financials: pd.DataFrame) -> Optional[dict]:
        if quarterly_financials is None or quarterly_financials.empty:
            return None

        try:
            qf = quarterly_financials
            rev = self._row(qf, ["Total Revenue", "Revenue", "Operating Revenue",
                                 "Total Operating Revenue", "Net Revenue"])
            prof = self._row(qf, ["Net Income", "Net Income Common Stockholders",
                                  "Net Income From Continuing Operations",
                                  "Net Income From Continuing Operation Net Minority Interest"])
            if rev is None or prof is None:
                logger.debug(f"{symbol}: insufficient quarterly rows for QoQ")
                return None

            def pct(cur, prev):
                if prev is None or prev == 0:
                    return None
                return ((cur - prev) / abs(prev)) * 100

            rev_cur, rev_prev = float(rev.iloc[-1]), float(rev.iloc[-2])
            prof_cur, prof_prev = float(prof.iloc[-1]), float(prof.iloc[-2])
            qoq_rev = pct(rev_cur, rev_prev)
            qoq_prof = pct(prof_cur, prof_prev)

            tol = config.FILTER_TOLERANCE_PCT
            rev_up = qoq_rev is not None and qoq_rev >= -tol
            prof_up = qoq_prof is not None and qoq_prof >= -tol

            # YoY (needs >= 5 quarters)
            qoq_rev_yoy = qoq_prof_yoy = None
            if len(rev) >= 5:
                qoq_rev_yoy = pct(rev_cur, float(rev.iloc[-5]))
            if len(prof) >= 5:
                qoq_prof_yoy = pct(prof_cur, float(prof.iloc[-5]))

            if self.mode == "both":
                passed = rev_up and prof_up
            elif self.mode == "both_and_yoy":
                yoy_ok = (
                    (qoq_rev_yoy is not None and qoq_rev_yoy >= -tol)
                    or (qoq_prof_yoy is not None and qoq_prof_yoy >= -tol)
                )
                passed = rev_up and prof_up and yoy_ok
            else:  # "either"
                passed = rev_up or prof_up

            if not passed:
                logger.debug(
                    f"{symbol}: failed QoQ ({self.mode}) — "
                    f"rev {qoq_rev}, prof {qoq_prof}"
                )
                return None

            # Net profit must be positive in the latest quarter — a momentum
            # stock riding a loss-to-smaller-loss "improvement" is not wanted.
            if prof_cur <= 0:
                logger.debug(f"{symbol}: latest quarterly profit not positive")
                return None

            return {
                "symbol": symbol,
                "qoq_revenue_pct": round(qoq_rev, 2) if qoq_rev is not None else None,
                "qoq_profit_pct": round(qoq_prof, 2) if qoq_prof is not None else None,
                "yoy_revenue_pct": round(qoq_rev_yoy, 2) if qoq_rev_yoy is not None else None,
                "yoy_profit_pct": round(qoq_prof_yoy, 2) if qoq_prof_yoy is not None else None,
                "latest_quarterly_revenue": rev_cur,
                "latest_quarterly_profit": prof_cur,
            }

        except Exception as e:
            logger.debug(f"QoQ results check failed for {symbol}: {e}")
            return None
