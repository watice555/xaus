import argparse
import logging
import time
from pathlib import Path
from typing import Dict, List

import ccxt
from ccxt.base.errors import ExchangeError, NetworkError, RequestTimeout
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter
import numpy as np
import pandas as pd

DEFAULT_PROXY = ""
DEFAULT_DAYS = 7
DEFAULT_TIMEOUT_MS = 30000
DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY = 2.0
DEFAULT_PAGE_LIMIT = 200
DEFAULT_OUTPUT_DIR = "results"

DEFAULT_SYMBOLS: Dict[str, str] = {
    "XAU": "XAU/USDT:USDT",
    "XAUT": "XAUT/USDT:USDT",
    "PAXG": "PAXG/USDT:USDT",
}


def parse_args() -> argparse.Namespace:
    # Keep CLI style close to xaus.py for easier reuse.
    parser = argparse.ArgumentParser(
        description="Fetch Bitget funding-rate history and plot cumulative series.",
    )
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Lookback window in days.")
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS, help="Exchange timeout in ms.")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Retries for transient failures.")
    parser.add_argument("--retry-delay", type=float, default=DEFAULT_RETRY_DELAY, help="Seconds between retries.")
    parser.add_argument(
        "--proxy",
        default=DEFAULT_PROXY,
        help="HTTP(S) proxy URL. Default: direct connection (no proxy).",
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        default=DEFAULT_PAGE_LIMIT,
        help="Page size for each funding-history request.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=20,
        help="Maximum pages to request per symbol.",
    )
    parser.add_argument(
        "--output",
        default="",
        help=(
            "Optional output image path. "
            "If omitted, filename is auto-generated with timestamp and params."
        ),
    )
    parser.add_argument("--no-plot", action="store_true", help="Do not show matplotlib window.")
    return parser.parse_args()


def create_exchange(timeout_ms: int, proxy: str) -> ccxt.bitget:
    # Use swap market only to avoid unnecessary unstable endpoints.
    config = {
        "enableRateLimit": True,
        "timeout": timeout_ms,
        "options": {
            "defaultType": "swap",
            "fetchCurrencies": False,
        },
    }
    if proxy:
        config["proxies"] = {"http": proxy, "https": proxy}
    exchange = ccxt.bitget(config)
    session = getattr(exchange, "session", None)
    if session is not None:
        # Ignore HTTP(S)_PROXY env vars; only allow proxy from --proxy.
        session.trust_env = False
    exchange.options["defaultType"] = "swap"
    exchange.options["fetchCurrencies"] = False
    if isinstance(exchange.has, dict):
        exchange.has["fetchCurrencies"] = False
    return exchange


def call_with_retry(func, retries: int, retry_delay: float, action_name: str):
    # Retry transient API failures to reduce interruptions.
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return func()
        except (NetworkError, RequestTimeout, ExchangeError) as err:
            last_error = err
            logging.warning("%s failed (%d/%d): %s", action_name, attempt, retries, err)
            if attempt < retries:
                time.sleep(retry_delay)
    raise RuntimeError(f"{action_name} failed after {retries} attempts") from last_error


def load_markets_with_retry(exchange: ccxt.bitget, retries: int, retry_delay: float) -> None:
    # Load USDT-margined swap markets only.
    def _load_swap_markets():
        return exchange.load_markets(
            reload=True,
            params={
                "type": "swap",
                "productType": "USDT-FUTURES",
            },
        )

    call_with_retry(
        func=_load_swap_markets,
        retries=retries,
        retry_delay=retry_delay,
        action_name="load_markets",
    )


def validate_symbols(exchange: ccxt.bitget, symbols: Dict[str, str]) -> None:
    missing = [symbol for symbol in symbols.values() if symbol not in exchange.markets]
    if missing:
        raise ValueError(f"Symbols not available on Bitget swap market: {missing}")


def extract_rate(entry: dict):
    # Field names may differ across ccxt versions, so parse defensively.
    rate = entry.get("fundingRate")
    if rate is None:
        info = entry.get("info") or {}
        for key in ("fundingRate", "funding_rate", "rate"):
            if key in info and info[key] is not None:
                rate = info[key]
                break
    if rate is None:
        return None
    return float(rate)


def fetch_funding_history(
    exchange: ccxt.bitget,
    symbol: str,
    since_ms: int,
    page_limit: int,
    max_pages: int,
    retries: int,
    retry_delay: float,
) -> List[dict]:
    # Paginate until no new data or page cap is reached.
    all_rows: List[dict] = []
    cursor = since_ms
    now_ms = exchange.milliseconds()

    for _ in range(max_pages):
        def _fetch_one_page():
            return exchange.fetch_funding_rate_history(
                symbol=symbol,
                since=cursor,
                limit=page_limit,
                params={"productType": "USDT-FUTURES"},
            )

        batch = call_with_retry(
            func=_fetch_one_page,
            retries=retries,
            retry_delay=retry_delay,
            action_name=f"fetch_funding_rate_history({symbol})",
        )

        if not batch:
            break

        all_rows.extend(batch)

        last_ts = batch[-1].get("timestamp")
        if last_ts is None:
            break
        if len(batch) < page_limit:
            break
        if last_ts >= now_ms:
            break

        next_cursor = int(last_ts) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor

    return all_rows


def to_series(alias: str, rows: List[dict], since_ms: int) -> pd.Series:
    # Normalize funding rows into a UTC-indexed series.
    cleaned = []
    for entry in rows:
        ts = entry.get("timestamp")
        rate = extract_rate(entry)
        if ts is None or rate is None:
            continue
        cleaned.append((int(ts), float(rate)))

    if not cleaned:
        raise ValueError(f"No funding-rate rows parsed for {alias}")

    df = pd.DataFrame(cleaned, columns=["timestamp", alias])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
    series = df.set_index("timestamp")[alias]
    since_dt = pd.to_datetime(since_ms, unit="ms", utc=True)
    return series[series.index >= since_dt]


def build_funding_df(
    exchange: ccxt.bitget,
    symbols: Dict[str, str],
    since_ms: int,
    page_limit: int,
    max_pages: int,
    retries: int,
    retry_delay: float,
) -> pd.DataFrame:
    # Merge three funding series and keep gaps for later fill.
    series_list = []
    for alias, symbol in symbols.items():
        rows = fetch_funding_history(
            exchange=exchange,
            symbol=symbol,
            since_ms=since_ms,
            page_limit=page_limit,
            max_pages=max_pages,
            retries=retries,
            retry_delay=retry_delay,
        )
        series = to_series(alias=alias, rows=rows, since_ms=since_ms)
        series_list.append(series)
        logging.info("%s rows: %d", alias, len(series))

    funding_df = pd.concat(series_list, axis=1, join="outer").sort_index()
    if funding_df.empty:
        raise ValueError("Funding dataframe is empty after merge.")
    return funding_df


def annualize_period_return(period_return, days: int):
    """
    Convert a period return over `days` into annualized return:
    annualized = (1 + r) ** (365 / days) - 1
    """
    values = np.asarray(period_return, dtype=float)
    # Guard against r <= -100%, which makes power invalid.
    values = np.maximum(values, -0.999999999)
    return np.power(1.0 + values, 365.0 / float(days)) - 1.0


def deannualize_return(annualized_return, days: int):
    # Inverse mapping required by secondary_yaxis.
    values = np.asarray(annualized_return, dtype=float)
    values = np.maximum(values, -0.999999999)
    return np.power(1.0 + values, float(days) / 365.0) - 1.0


def plot_cumulative(cumulative_df: pd.DataFrame, days: int, output_path: Path, no_plot: bool) -> None:
    fig, ax = plt.subplots(figsize=(14, 8))
    for column in cumulative_df.columns:
        ax.step(cumulative_df.index, cumulative_df[column], where="post", label=column)

    ax.axhline(0, linestyle="--", alpha=0.5)
    ax.set_title(f"Bitget Cumulative Funding Rate (Last {days} Days)")
    ax.set_xlabel("Time (UTC)")
    ax.set_ylabel("Period Total Return (%)")
    ax.yaxis.set_major_formatter(PercentFormatter(xmax=1.0))

    # Right axis maps period return to annualized equivalent for this window.
    secax = ax.secondary_yaxis(
        "right",
        functions=(
            lambda y: annualize_period_return(y, days),
            lambda y: deannualize_return(y, days),
        ),
    )
    secax.set_ylabel(f"Annualized Return Equivalent ({days}d Window) (%)")
    secax.yaxis.set_major_formatter(PercentFormatter(xmax=1.0))

    ax.legend()
    ax.grid(True)
    fig.tight_layout()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=180)
    logging.info("Saved chart to %s", output_path)

    if not no_plot:
        plt.show()
    else:
        plt.close(fig)


def build_output_path(output_arg: str, days: int, run_ts: pd.Timestamp) -> Path:
    # Default naming includes runtime + key params to avoid overwrite.
    if output_arg:
        return Path(output_arg).expanduser().resolve()
    stamp = run_ts.strftime("%Y%m%d_%H%M%S")
    filename = f"bitget_funding_cumulative_{days}days_{stamp}.png"
    return Path(DEFAULT_OUTPUT_DIR, filename).expanduser().resolve()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    exchange = create_exchange(timeout_ms=args.timeout_ms, proxy=args.proxy)
    load_markets_with_retry(exchange, retries=args.retries, retry_delay=args.retry_delay)
    validate_symbols(exchange, DEFAULT_SYMBOLS)

    now_ms = exchange.milliseconds()
    run_ts = pd.to_datetime(now_ms, unit="ms", utc=True)
    since_ms = now_ms - args.days * 24 * 60 * 60 * 1000
    logging.info("Fetching funding rates since %s", pd.to_datetime(since_ms, unit="ms", utc=True))

    funding_df = build_funding_df(
        exchange=exchange,
        symbols=DEFAULT_SYMBOLS,
        since_ms=since_ms,
        page_limit=args.page_limit,
        max_pages=args.max_pages,
        retries=args.retries,
        retry_delay=args.retry_delay,
    )

    cumulative_df = funding_df.fillna(0.0).cumsum()
    logging.info("Merged rows: %d", len(cumulative_df))

    # Log period-total and annualized summary for quick cross-check.
    period_total = cumulative_df.iloc[-1]
    annualized_total = pd.Series(
        annualize_period_return(period_total.values, args.days),
        index=period_total.index,
        name="annualized_return",
    )
    summary_df = pd.DataFrame(
        {
            "period_total_return": period_total,
            "annualized_return": annualized_total,
        }
    )
    logging.info("Latest return summary (%dd):\n%s", args.days, summary_df.to_string(float_format=lambda v: f"{v:.6%}"))

    output_path = build_output_path(output_arg=args.output, days=args.days, run_ts=run_ts)
    plot_cumulative(cumulative_df=cumulative_df, days=args.days, output_path=output_path, no_plot=args.no_plot)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as err:
        logging.exception("Fatal error: %s", err)
        raise SystemExit(1)
