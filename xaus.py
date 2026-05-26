# 这个脚本的目标：
# 1) 从 Bitget 拉取 3 个黄金相关永续合约的价格
# 2) 计算它们之间的价差
# 3) 把价差画成曲线，方便观察是否有规律

import argparse
import json
import logging
import os
import time
from typing import Dict

import ccxt
from ccxt.base.errors import ExchangeError, NetworkError, RequestTimeout
import matplotlib.pyplot as plt
import pandas as pd

# 默认直连；只有显式传入 --proxy 才启用代理。
DEFAULT_PROXY = ""
DEFAULT_TIMEFRAME = "1h"
DEFAULT_LIMIT = 500
LOOP_RESULTS_DIR = "results"
LOOP_PLOT_FILENAME = "spreads_latest.png"
DEFAULT_ALERT_CONFIG = ""
SPREAD_COLUMNS = ["XAU_minus_XAUT", "XAU_minus_PAXG", "PAXG_minus_XAUT"]

# 这里定义我们要分析的 3 个交易对：
# key 是我们自己的简称，value 是 ccxt 识别的标准 symbol
DEFAULT_SYMBOLS: Dict[str, str] = {
    "XAU": "XAU/USDT:USDT",
    "XAUT": "XAUT/USDT:USDT",
    "PAXG": "PAXG/USDT:USDT",
}


# 解析命令行参数（运行 python xaus.py --help 可以看到这些参数）
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch Bitget perpetual prices and plot gold spread series."
    )

    # K 线周期，例如 1m、5m、1h、1d
    parser.add_argument(
        "--timeframe",
        default=DEFAULT_TIMEFRAME,
        help=f"OHLCV timeframe, default: {DEFAULT_TIMEFRAME}. e.g. 1m/5m/1h/4h/1d",
    )

    # 一次拉取多少根 K 线
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Number of OHLCV candles to fetch, default: {DEFAULT_LIMIT}",
    )

    # 单次请求超时（毫秒）
    parser.add_argument("--timeout-ms", type=int, default=30000, help="Exchange request timeout in milliseconds")

    # 网络抖动时最多重试次数
    parser.add_argument("--retries", type=int, default=3, help="Retries for transient API failures")

    # 每次重试前等待多久（秒）
    parser.add_argument("--retry-delay", type=float, default=2.0, help="Seconds to wait between retries")

    # 代理地址。默认直连，只有显式传入 --proxy 才启用代理。
    parser.add_argument(
        "--proxy",
        default=DEFAULT_PROXY,
        help="HTTP(S) proxy URL. Default: direct connection (no proxy).",
    )

    # 如果不想弹出图表窗口，可以加 --no-plot（仅单次模式）
    parser.add_argument("--no-plot", action="store_true", help="Do not show matplotlib chart in single-run mode")
    parser.add_argument(
        "--loop",
        dest="loop",
        action="store_true",
        help="Run continuously (every 60 seconds)",
    )
    parser.add_argument(
        "--alert-config",
        default=DEFAULT_ALERT_CONFIG,
        help="Alert config JSON path. Alerts are disabled unless this is provided.",
    )
    # 兼容旧参数名，帮助信息中隐藏
    parser.add_argument("--every-60s", dest="loop", action="store_true", help=argparse.SUPPRESS)

    return parser.parse_args()


# 创建交易所客户端对象（这里只配置 Bitget + swap 永续）
def create_exchange(timeout_ms: int, proxy: str) -> ccxt.bitget:
    config = {
        # 让 ccxt 自动做请求频率控制，减少被限流概率
        "enableRateLimit": True,
        "timeout": timeout_ms,
        "options": {
            # 指定默认使用永续/合约市场
            "defaultType": "swap",
            # 某些网络环境下拉 currencies 可能不稳定，关闭可减少初始化失败
            "fetchCurrencies": False,
        },
    }

    # 只有你传了 proxy 才设置代理，避免硬编码
    if proxy:
        config["proxies"] = {"http": proxy, "https": proxy}

    exchange = ccxt.bitget(config)
    session = getattr(exchange, "session", None)
    if session is not None:
        # Ignore HTTP(S)_PROXY env vars; only allow proxy from --proxy.
        session.trust_env = False

    # 双保险：不同版本 ccxt 对该选项的读取路径可能不同。
    # 显式设置可以最大限度避免 load_markets 时请求 currencies 接口。
    exchange.options["defaultType"] = "swap"
    exchange.options["fetchCurrencies"] = False
    if isinstance(exchange.has, dict):
        exchange.has["fetchCurrencies"] = False

    return exchange


# 通用重试函数：把任何“可能暂时失败”的操作包一层重试
# func: 要执行的函数
# retries: 最多尝试几次
# retry_delay: 两次尝试间隔
# action_name: 日志里显示的动作名，便于排查

def call_with_retry(func, retries: int, retry_delay: float, action_name: str):
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            return func()
        except (NetworkError, RequestTimeout, ExchangeError) as err:
            last_error = err
            logging.warning("%s failed (%d/%d): %s", action_name, attempt, retries, err)

            # 还没到最后一次就等待再试
            if attempt < retries:
                time.sleep(retry_delay)

    # 全部失败后抛出统一错误
    raise RuntimeError(f"{action_name} failed after {retries} attempts") from last_error


# 加载市场信息（symbol 列表、精度等），并带重试
def load_markets_with_retry(exchange: ccxt.bitget, retries: int, retry_delay: float) -> None:
    # 强制只加载 swap 市场，避免触发 spot market 的接口请求。
    # 在某些网络环境里，spot/public/symbols 更容易连接失败。
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


# 校验我们想要的 symbol 是否真的存在于当前交易所市场中
def validate_symbols(exchange: ccxt.bitget, symbols: Dict[str, str]) -> None:
    missing = [symbol for symbol in symbols.values() if symbol not in exchange.markets]
    if missing:
        raise ValueError(f"Symbols not available on Bitget swap market: {missing}")


# 拉取单个交易对的收盘价序列
# 返回值是 pandas Series，索引是时间，值是 close

def fetch_close_series(
    exchange: ccxt.bitget,
    symbol: str,
    timeframe: str,
    limit: int,
    retries: int,
    retry_delay: float,
) -> pd.Series:
    # 把“怎么拉取 K 线”定义成一个小函数，方便传给重试器
    def _fetch_ohlcv():
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    ohlcv = call_with_retry(
        func=_fetch_ohlcv,
        retries=retries,
        retry_delay=retry_delay,
        action_name=f"fetch_ohlcv({symbol})",
    )

    # 交易所返回空数据时直接报错，避免后续静默算错
    if not ohlcv:
        raise ValueError(f"No OHLCV data returned for {symbol}")

    # ccxt 返回结构：[timestamp, open, high, low, close, volume]
    df = pd.DataFrame(
        ohlcv,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )

    # 时间戳单位是毫秒，转成 UTC 时间，避免时区混乱
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

    # 设置时间为索引，后面多个交易对按时间对齐会更方便
    df.set_index("timestamp", inplace=True)

    # 这里只做价差分析，所以只返回 close 列
    return df["close"]


# 拉取全部交易对并按时间对齐，得到一个总表
# 表结构大概是：index=timestamp，columns=[XAU, XAUT, PAXG]
def build_price_df(
    exchange: ccxt.bitget,
    symbols: Dict[str, str],
    timeframe: str,
    limit: int,
    retries: int,
    retry_delay: float,
) -> pd.DataFrame:
    series_list = []

    for alias, symbol in symbols.items():
        close_series = fetch_close_series(
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            limit=limit,
            retries=retries,
            retry_delay=retry_delay,
        )

        # 给每列命名为 XAU / XAUT / PAXG
        close_series.name = alias
        series_list.append(close_series)

    # join="inner"：只保留三者都有数据的时间点，保证比较公平
    price_df = pd.concat(series_list, axis=1, join="inner")

    if price_df.empty:
        raise ValueError("Joined price dataframe is empty. Check symbols/timeframe/limit.")

    return price_df


# 在价格表中新增 3 列“价差”
def add_spreads(price_df: pd.DataFrame) -> pd.DataFrame:
    price_df["XAU_minus_XAUT"] = price_df["XAU"] - price_df["XAUT"]
    price_df["XAU_minus_PAXG"] = price_df["XAU"] - price_df["PAXG"]
    price_df["PAXG_minus_XAUT"] = price_df["PAXG"] - price_df["XAUT"]
    return price_df


def load_alert_config(config_path: str) -> dict:
    if not os.path.exists(config_path):
        logging.warning("Alert config not found: %s (alerts disabled)", config_path)
        return {"enabled": False, "use_absolute": True, "thresholds": {}}

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("Alert config must be a JSON object")

    enabled = bool(data.get("enabled", True))
    use_absolute = bool(data.get("use_absolute", True))
    raw_thresholds = data.get("thresholds", {})
    if not isinstance(raw_thresholds, dict):
        raise ValueError("'thresholds' in alert config must be a JSON object")

    thresholds = {}
    for spread_name in SPREAD_COLUMNS:
        value = raw_thresholds.get(spread_name)
        if value is None:
            continue
        threshold = float(value)
        if threshold <= 0:
            raise ValueError(f"Threshold for {spread_name} must be > 0")
        thresholds[spread_name] = threshold

    if enabled and not thresholds:
        logging.warning("Alert config loaded but no valid thresholds set (alerts disabled)")
        enabled = False

    return {"enabled": enabled, "use_absolute": use_absolute, "thresholds": thresholds}


def check_alerts(price_df: pd.DataFrame, alert_config: dict) -> None:
    if not alert_config.get("enabled"):
        return

    latest = price_df.tail(1).iloc[0]
    latest_ts = price_df.index[-1]
    use_absolute = bool(alert_config.get("use_absolute", True))
    thresholds = alert_config.get("thresholds", {})

    for spread_name, threshold in thresholds.items():
        value = float(latest[spread_name])
        measured = abs(value) if use_absolute else value
        if measured > threshold:
            if use_absolute:
                logging.warning(
                    "ALERT %s | %s=%.6f | abs=%.6f > threshold=%.6f",
                    latest_ts,
                    spread_name,
                    value,
                    measured,
                    threshold,
                )
            else:
                logging.warning(
                    "ALERT %s | %s=%.6f > threshold=%.6f",
                    latest_ts,
                    spread_name,
                    value,
                    threshold,
                )


# 执行一次完整的数据拉取和计算流程
def run_once(exchange: ccxt.bitget, args: argparse.Namespace) -> pd.DataFrame:
    price_df = build_price_df(
        exchange=exchange,
        symbols=DEFAULT_SYMBOLS,
        timeframe=args.timeframe,
        limit=args.limit,
        retries=args.retries,
        retry_delay=args.retry_delay,
    )
    return add_spreads(price_df)


# 构造价差图（供“显示”和“保存”复用）
def build_spread_figure(price_df: pd.DataFrame) -> None:
    plt.figure(figsize=(14, 8))

    # 每条线表示一组价差
    plt.plot(price_df.index, price_df["XAU_minus_XAUT"], label="XAU - XAUT")
    plt.plot(price_df.index, price_df["XAU_minus_PAXG"], label="XAU - PAXG")
    plt.plot(price_df.index, price_df["PAXG_minus_XAUT"], label="PAXG - XAUT")

    # y=0 参考线：方便你看价差是正还是负
    plt.axhline(0, linestyle="--", alpha=0.5)

    plt.title("Bitget Gold Perpetual Spreads")
    plt.xlabel("Time (UTC)")
    plt.ylabel("Price Difference (USDT)")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()


# 绘制价差曲线
def plot_spreads(price_df: pd.DataFrame) -> None:
    build_spread_figure(price_df)
    plt.show()


# 保存价差曲线到文件（覆盖保存）
def save_spreads(price_df: pd.DataFrame, output_path: str) -> None:
    build_spread_figure(price_df)
    plt.savefig(output_path, dpi=150)
    plt.close()


# 主流程函数：串起“解析参数 -> 拉数据 -> 算价差 -> 画图”
def main() -> int:
    args = parse_args()

    # 设置日志格式，便于查看运行过程和报错
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    logging.info("Program running")
    logging.info("timeframe=%s limit=%s", args.timeframe, args.limit)
    if args.alert_config:
        alert_config = load_alert_config(args.alert_config)
    else:
        alert_config = {"enabled": False, "use_absolute": True, "thresholds": {}}
    logging.info("alert_config=%s enabled=%s", args.alert_config or "(none)", alert_config["enabled"])
    if args.loop:
        logging.info("continuous mode enabled: run every 60 seconds")
        os.makedirs(LOOP_RESULTS_DIR, exist_ok=True)
        loop_plot_path = os.path.join(LOOP_RESULTS_DIR, LOOP_PLOT_FILENAME)
        logging.info("loop chart output: %s (overwrite each run)", loop_plot_path)

    # 创建交易所对象
    exchange = create_exchange(timeout_ms=args.timeout_ms, proxy=args.proxy)

    # 初始化市场信息并校验交易对
    load_markets_with_retry(exchange, retries=args.retries, retry_delay=args.retry_delay)
    validate_symbols(exchange, DEFAULT_SYMBOLS)

    if args.loop:
        while True:
            try:
                price_df = run_once(exchange, args)
                logging.info("Fetched %d aligned rows", len(price_df))
                logging.info("Latest row:\n%s", price_df.tail(1).to_string())
                check_alerts(price_df, alert_config)
                save_spreads(price_df, loop_plot_path)
                logging.info("Saved chart: %s", loop_plot_path)
            except Exception as err:
                # 持续模式下单轮失败不退出，等待下一轮继续
                logging.exception("Iteration failed: %s", err)
            time.sleep(60)
    else:
        # 拉取价格并计算价差
        price_df = run_once(exchange, args)

        # 打印一些结果，让你不画图也能看到数据
        logging.info("Fetched %d aligned rows", len(price_df))
        logging.info("Latest row:\n%s", price_df.tail(1).to_string())
        check_alerts(price_df, alert_config)

        # 默认画图；传 --no-plot 则跳过
        if not args.no_plot:
            plot_spreads(price_df)

    return 0


# 只有“直接运行这个文件”时，才执行 main()
# 如果这个文件被别的文件 import，不会自动跑，便于复用和测试
if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logging.info("Stopped by user (Ctrl+C)")
        raise SystemExit(0)
    except Exception as err:
        # 捕获未处理异常并打印堆栈日志
        logging.exception("Fatal error: %s", err)
        raise SystemExit(1)
