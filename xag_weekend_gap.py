import argparse
import datetime as dt
import lzma
import struct
import time
from typing import List, Optional

import pandas as pd
import requests


DEFAULT_PROXY = ""
BASE_URL = "https://datafeed.dukascopy.com/datafeed/XAGUSD"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Query latest weekend gap stats for XAGUSD (Dukascopy minute candles)."
    )
    parser.add_argument("--weeks", type=int, default=10, help="How many latest weekends to return.")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=120,
        help="How many days to fetch backward from today (UTC).",
    )
    parser.add_argument(
        "--proxy",
        default=DEFAULT_PROXY,
        help="HTTP(S) proxy URL. Default: direct connection (no proxy).",
    )
    parser.add_argument(
        "--timezone",
        default="Asia/Shanghai",
        help="Display timezone for output timestamps.",
    )
    parser.add_argument(
        "--min-closed-minutes",
        type=int,
        default=40 * 60,
        help="Minimum continuous closed minutes to classify a weekend closure.",
    )
    parser.add_argument("--timeout", type=int, default=25, help="HTTP timeout in seconds.")
    parser.add_argument("--retries", type=int, default=3, help="Retries for each day file.")
    parser.add_argument("--sleep", type=float, default=0.4, help="Sleep seconds between retries.")
    return parser.parse_args()


def day_url(day_utc: dt.date) -> str:
    # Dukascopy path uses zero-based month/day indexes.
    month_idx = day_utc.month - 1
    day_idx = day_utc.day - 1
    return f"{BASE_URL}/{day_utc.year:04d}/{month_idx:02d}/{day_idx:02d}/BID_candles_min_1.bi5"


def decode_volume(bits: int) -> float:
    return struct.unpack(">f", struct.pack(">I", bits))[0]


def fetch_day_df(
    session: requests.Session,
    day_utc: dt.date,
    timeout: int,
    retries: int,
    sleep_s: float,
) -> Optional[pd.DataFrame]:
    url = day_url(day_utc)
    last_err = None
    for _ in range(retries):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            payload = lzma.decompress(resp.content, format=lzma.FORMAT_ALONE)
            if len(payload) % 24 != 0:
                raise ValueError(f"Unexpected candle payload length: {len(payload)}")

            rows = []
            day_start = pd.Timestamp(day_utc, tz="UTC")
            for offset in range(0, len(payload), 24):
                rec = struct.unpack(">6I", payload[offset : offset + 24])
                sec_of_day, _, close_raw, _, _, vol_bits = rec
                ts = day_start + pd.Timedelta(seconds=int(sec_of_day))
                rows.append((ts, close_raw / 1000.0, decode_volume(vol_bits)))

            return pd.DataFrame(rows, columns=["timestamp", "close", "volume"])
        except Exception as err:  # transient network/proxy/decode issues
            last_err = err
            time.sleep(sleep_s)
    raise RuntimeError(f"Failed to fetch/decode {day_utc}: {last_err}") from last_err


def fetch_range_df(args: argparse.Namespace) -> pd.DataFrame:
    today_utc = dt.datetime.now(dt.timezone.utc).date()
    start_day = today_utc - dt.timedelta(days=args.lookback_days)
    days = [start_day + dt.timedelta(days=i) for i in range((today_utc - start_day).days + 1)]

    session = requests.Session()
    session.trust_env = False
    if args.proxy:
        session.proxies.update({"http": args.proxy, "https": args.proxy})

    frames: List[pd.DataFrame] = []
    failed_days: List[str] = []
    for day in days:
        try:
            day_df = fetch_day_df(
                session=session,
                day_utc=day,
                timeout=args.timeout,
                retries=args.retries,
                sleep_s=args.sleep,
            )
        except Exception:
            failed_days.append(str(day))
            continue
        if day_df is not None:
            frames.append(day_df)

    if not frames:
        raise RuntimeError("No minute data fetched.")
    if failed_days:
        print(f"Warning: skipped {len(failed_days)} day(s) due fetch/decode errors.")

    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
    df.reset_index(drop=True, inplace=True)
    return df


def extract_weekend_events(df: pd.DataFrame, min_closed_minutes: int) -> pd.DataFrame:
    df = df.copy()
    df["is_trading"] = df["volume"] > 0
    df["is_closed"] = ~df["is_trading"]
    minute_gap = df["timestamp"].diff().gt(pd.Timedelta(minutes=1))
    state_change = df["is_closed"] != df["is_closed"].shift(1, fill_value=df["is_closed"].iloc[0])
    df["grp"] = (state_change | minute_gap).cumsum()

    events = []
    now_utc = pd.Timestamp.now(tz="UTC")

    for _, g in df.groupby("grp", sort=True):
        if not bool(g["is_closed"].iloc[0]):
            continue
        closed_minutes = int((g["timestamp"].iloc[-1] - g["timestamp"].iloc[0]).total_seconds() / 60) + 1
        if len(g) < min_closed_minutes or closed_minutes < min_closed_minutes:
            continue

        start_idx = int(g.index.min())
        end_idx = int(g.index.max())
        prev_idx = start_idx - 1
        next_idx = end_idx + 1
        if prev_idx < 0 or next_idx >= len(df):
            continue
        if not bool(df.loc[prev_idx, "is_trading"]) or not bool(df.loc[next_idx, "is_trading"]):
            continue

        close_ts = df.loc[prev_idx, "timestamp"]
        open_ts = df.loc[next_idx, "timestamp"]
        if open_ts > now_utc:
            continue

        close_price = float(df.loc[prev_idx, "close"])
        open_price = float(df.loc[next_idx, "close"])
        events.append(
            {
                "friday_close_utc": close_ts,
                "monday_open_utc": open_ts,
                "friday_close": close_price,
                "monday_open": open_price,
                "diff_open_minus_close": open_price - close_price,
                "gap_hours": (open_ts - close_ts).total_seconds() / 3600.0,
            }
        )

    if not events:
        raise RuntimeError("No weekend closure events detected. Try increasing lookback-days.")

    out = pd.DataFrame(events).sort_values("monday_open_utc", ascending=False).reset_index(drop=True)
    return out


def main() -> int:
    args = parse_args()
    minute_df = fetch_range_df(args)
    events_df = extract_weekend_events(minute_df, min_closed_minutes=args.min_closed_minutes).head(args.weeks)

    tz = args.timezone
    events_df["friday_close_time"] = events_df["friday_close_utc"].dt.tz_convert(tz)
    events_df["monday_open_time"] = events_df["monday_open_utc"].dt.tz_convert(tz)

    display_df = events_df[
        [
            "friday_close_time",
            "friday_close",
            "monday_open_time",
            "monday_open",
            "diff_open_minus_close",
            "gap_hours",
        ]
    ].copy()

    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 20)
    pd.set_option("display.max_rows", 50)
    print(display_df.to_string(index=False, justify="left", col_space=12, float_format=lambda v: f"{v:,.3f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
