"""
Fetches the stock universe for the screener.

Supports two modes (config.STOCK_UNIVERSE):
  "nifty500" – NIFTY 500 index constituents (~500 large-caps) from NSE India.
  "nse_all"  – NSE All Listed Equities (EQ series), capped at config.UNIVERSE_SIZE.
               Provides ~2500 stocks covering large, mid, and small caps.

Falls back to a cached local copy if any download fails.
"""
import io
import logging
import os

import pandas as pd
import requests

import config

logger = logging.getLogger("SwingScreener.Universe")

# NSE website requires browser-like headers
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
}

CACHE_FILE      = os.path.join(config.OUTPUT_DIR, "nifty500_cache.csv")
CACHE_FILE_ALL  = os.path.join(config.OUTPUT_DIR, "nse_all_cache.csv")


def _download_nifty500_csv() -> pd.DataFrame:
    """Download NIFTY 500 list from NSE India archives."""
    logger.info("Downloading NIFTY 500 list from NSE India...")
    session = requests.Session()
    # First hit the main page to get cookies
    session.get("https://www.nseindia.com/", headers=NSE_HEADERS, timeout=10)

    resp = session.get(config.NIFTY500_CSV_URL, headers=NSE_HEADERS, timeout=15)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))
    return df


def _download_nifty500_api() -> pd.DataFrame:
    """Download NIFTY 500 constituents via NSE API."""
    logger.info("Trying NSE API for NIFTY 500 list...")
    session = requests.Session()
    session.get("https://www.nseindia.com/", headers=NSE_HEADERS, timeout=10)

    resp = session.get(config.NIFTY500_URL, headers=NSE_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    records = data.get("data", [])
    df = pd.DataFrame(records)
    return df


def get_nifty500_symbols() -> list[dict]:
    """
    Returns a list of dicts with keys: symbol, company_name, industry, isin.

    Tries multiple sources, caches the result locally.
    """
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    df = None

    # Try downloading from NSE
    for fetcher in [_download_nifty500_csv, _download_nifty500_api]:
        try:
            df = fetcher()
            if df is not None and len(df) > 0:
                break
        except Exception as e:
            logger.warning(f"Download method failed: {e}")
            continue

    # Fall back to cache
    if df is None or len(df) == 0:
        if os.path.exists(CACHE_FILE):
            logger.info("Using cached NIFTY 500 list.")
            df = pd.read_csv(CACHE_FILE)
        else:
            raise RuntimeError(
                "Cannot fetch NIFTY 500 list and no cache available. "
                "Check your internet connection or place a CSV at: " + CACHE_FILE
            )

    # Normalize column names
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    # Identify the symbol column
    sym_col = None
    for candidate in ["symbol", "trading_symbol", "nse_symbol", "ticker"]:
        if candidate in df.columns:
            sym_col = candidate
            break
    if sym_col is None:
        # Pick the first column that looks like stock symbols
        for col in df.columns:
            if df[col].dtype == "object" and df[col].str.match(r"^[A-Z&]+$").mean() > 0.5:
                sym_col = col
                break
    if sym_col is None:
        raise RuntimeError(f"Cannot identify symbol column in data. Columns: {list(df.columns)}")

    # Build result
    name_col = next((c for c in df.columns if "company" in c or "name" in c), None)
    industry_col = next((c for c in df.columns if "industry" in c or "sector" in c), None)
    isin_col = next((c for c in df.columns if "isin" in c), None)

    stocks = []
    for _, row in df.iterrows():
        symbol = str(row[sym_col]).strip()
        if not symbol or symbol == "nan":
            continue
        stocks.append({
            "symbol": symbol,
            "company_name": str(row.get(name_col, "")).strip() if name_col else "",
            "industry": str(row.get(industry_col, "")).strip() if industry_col else "",
            "isin": str(row.get(isin_col, "")).strip() if isin_col else "",
        })

    # Cache for future use
    try:
        cache_df = pd.DataFrame(stocks)
        cache_df.to_csv(CACHE_FILE, index=False)
        logger.info(f"Cached {len(stocks)} stocks to {CACHE_FILE}")
    except Exception as e:
        logger.warning(f"Could not cache stock list: {e}")

    logger.info(f"Loaded {len(stocks)} stocks from NIFTY 500 universe.")
    return stocks


def _nse_session() -> requests.Session:
    """Create a requests.Session pre-warmed with NSE cookies."""
    session = requests.Session()
    try:
        session.get("https://www.nseindia.com/", headers=NSE_HEADERS, timeout=10)
    except Exception:
        pass
    return session


def get_nse_all_symbols(max_stocks: int = None) -> list[dict]:
    """
    Fetch all NSE-listed equity (EQ series) stocks.

    Source: https://archives.nseindia.com/content/equities/EQUITY_L.csv
    Returns a list of dicts with keys: symbol, company_name, industry, isin.
    Results are capped to config.UNIVERSE_SIZE (or max_stocks if supplied).
    Falls back to cache on failure.
    """
    max_stocks = max_stocks or config.UNIVERSE_SIZE
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    df = None

    try:
        logger.info(
            f"Downloading NSE All Listed Equities from NSE India "
            f"(capped to {max_stocks} stocks)..."
        )
        session = _nse_session()
        resp = session.get(config.NSE_EQUITY_LIST_URL, headers=NSE_HEADERS, timeout=20)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        logger.info(f"Downloaded {len(df)} equities from NSE.")
    except Exception as e:
        logger.warning(f"NSE equity list download failed: {e}")

    # Fall back to cache
    if df is None or df.empty:
        if os.path.exists(CACHE_FILE_ALL):
            logger.info("Using cached NSE All equity list.")
            df = pd.read_csv(CACHE_FILE_ALL)
        else:
            raise RuntimeError(
                "Cannot fetch NSE equity list and no cache available. "
                "Check internet or place a CSV at: " + CACHE_FILE_ALL
            )

    # Normalise column names
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    # Keep only EQ series (exclude SME, BE, etc. — they're illiquid)
    series_col = next((c for c in df.columns if "series" in c), None)
    if series_col:
        df = df[df[series_col].str.strip().str.upper() == "EQ"].copy()
        logger.info(f"Filtered to {len(df)} EQ-series stocks.")

    # Identify key columns
    sym_col  = next((c for c in df.columns if c in ("symbol",)), None) or df.columns[0]
    name_col = next((c for c in df.columns if "company" in c or "name" in c), None)
    isin_col = next((c for c in df.columns if "isin" in c), None)

    # Cap to max_stocks (sorted alphabetically — no bias toward any sector)
    df = df.head(max_stocks)

    stocks = []
    for _, row in df.iterrows():
        symbol = str(row[sym_col]).strip()
        if not symbol or symbol == "nan":
            continue
        stocks.append({
            "symbol": symbol,
            "company_name": str(row.get(name_col, "")).strip() if name_col else "",
            "industry": "",   # EQUITY_L.csv has no sector column
            "isin": str(row.get(isin_col, "")).strip() if isin_col else "",
        })

    # Cache for future runs
    try:
        pd.DataFrame(stocks).to_csv(CACHE_FILE_ALL, index=False)
        logger.info(f"Cached {len(stocks)} stocks to {CACHE_FILE_ALL}")
    except Exception as e:
        logger.warning(f"Could not cache NSE equity list: {e}")

    logger.info(f"Loaded {len(stocks)} stocks from NSE All Equities universe.")
    return stocks


def get_stock_universe() -> list[dict]:
    """
    Main entry point — returns the stock list based on config.STOCK_UNIVERSE.

      "nifty500" -> get_nifty500_symbols()  (default, ~500 stocks)
      "nse_all"  -> get_nse_all_symbols()   (up to config.UNIVERSE_SIZE, ~2000+)
    """
    mode = getattr(config, "STOCK_UNIVERSE", "nifty500").lower().strip()
    if mode == "nse_all":
        return get_nse_all_symbols(max_stocks=config.UNIVERSE_SIZE)
    return get_nifty500_symbols()
