"""
Kotak Neo API client wrapper.

Provides authentication, live quotes, and scrip master access.
Note: Kotak Neo does NOT provide historical data, so yfinance is used for that.
"""
import logging
import time
from typing import Optional

import config

logger = logging.getLogger("SwingScreener.KotakNeo")


class KotakNeoClient:
    """Wrapper around Kotak Neo API for live market data and authentication."""

    def __init__(
        self,
        access_token: str,
        consumer_key: str = "",
        consumer_secret: str = "",
        environment: str = "prod",
    ):
        self.access_token = access_token
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.environment = environment
        self._client = None
        self._authenticated = False

    def initialize(self) -> bool:
        """Initialize the Kotak Neo API client with the provided token."""
        try:
            from neo_api_client import NeoAPI

            self._client = NeoAPI(
                consumer_key=self.consumer_key or config.KOTAK_NEO_CONSUMER_KEY,
                consumer_secret=self.consumer_secret or config.KOTAK_NEO_CONSUMER_SECRET,
                environment=self.environment,
                access_token=self.access_token,
            )
            self._authenticated = True
            logger.info("Kotak Neo API client initialized successfully.")
            return True

        except ImportError:
            logger.warning(
                "neo_api_client not installed. Install via: pip install neo_api_client. "
                "Continuing with yfinance-only mode."
            )
            return False
        except Exception as e:
            logger.error(f"Failed to initialize Kotak Neo client: {e}")
            return False

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    def get_quotes(
        self,
        symbols: list[str],
        exchange: str = "nse_cm",
        quote_type: str = "ltp",
    ) -> dict:
        """
        Fetch live quotes for a list of symbols.

        Args:
            symbols: List of NSE/BSE symbols
            exchange: Exchange segment (nse_cm, bse_cm)
            quote_type: Type of quote (ltp, ohlc, 52w, all, scrip_details)

        Returns:
            Dict mapping symbol -> quote data
        """
        if not self._authenticated or self._client is None:
            logger.warning("Kotak Neo not authenticated. Skipping live quotes.")
            return {}

        results = {}
        # Process in small batches to avoid rate limits
        batch_size = 10
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            instrument_tokens = []
            for sym in batch:
                try:
                    scrip_info = self._client.search_scrip(
                        exchange_segment=exchange, symbol=sym
                    )
                    if scrip_info and isinstance(scrip_info, list) and len(scrip_info) > 0:
                        token = scrip_info[0].get("pSymbol", scrip_info[0].get("instrument_token", ""))
                        if token:
                            instrument_tokens.append({
                                "instrument_token": str(token),
                                "exchange_segment": exchange,
                            })
                except Exception as e:
                    logger.debug(f"Could not search scrip for {sym}: {e}")
                    continue

            if not instrument_tokens:
                continue

            try:
                quote_data = self._client.quotes(
                    instrument_tokens=instrument_tokens,
                    quote_type=quote_type,
                )
                if isinstance(quote_data, list):
                    for q in quote_data:
                        sym = q.get("trading_symbol", q.get("symbol", ""))
                        if sym:
                            results[sym] = q
                elif isinstance(quote_data, dict):
                    results.update(quote_data)
            except Exception as e:
                logger.debug(f"Quote fetch error for batch: {e}")

            time.sleep(config.REQUEST_DELAY)

        logger.info(f"Fetched live quotes for {len(results)} symbols via Kotak Neo.")
        return results

    def get_scrip_master(self, exchange: str = "nse_cm") -> Optional[list]:
        """Download the scrip master file for an exchange segment."""
        if not self._authenticated or self._client is None:
            return None
        try:
            return self._client.scrip_master(exchange_segment=exchange)
        except Exception as e:
            logger.error(f"Failed to fetch scrip master: {e}")
            return None

    def enrich_with_live_data(self, candidates: list[dict]) -> list[dict]:
        """
        Enrich screened candidates with live market data from Kotak Neo.

        Adds: ltp, day_change, day_change_pct, volume
        """
        if not self._authenticated:
            logger.info("Kotak Neo not available. Returning candidates without live enrichment.")
            return candidates

        symbols = [c["symbol"] for c in candidates]
        quotes = self.get_quotes(symbols, quote_type="all")

        for candidate in candidates:
            sym = candidate["symbol"]
            if sym in quotes:
                q = quotes[sym]
                candidate["live_ltp"] = q.get("ltp", q.get("last_traded_price"))
                candidate["live_volume"] = q.get("volume", q.get("total_traded_quantity"))
                candidate["live_change"] = q.get("change", q.get("net_change"))
                candidate["live_change_pct"] = q.get("change_percent", q.get("percent_change"))

        return candidates
