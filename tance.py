import argparse

import ccxt


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List Bitget swap symbols related to gold.")
    parser.add_argument(
        "--proxy",
        default="",
        help="HTTP(S) proxy URL. Default: direct connection (no proxy).",
    )
    return parser.parse_args()


def create_exchange(proxy: str) -> ccxt.bitget:
    config = {
        "enableRateLimit": True,
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
    return exchange


def main() -> None:
    args = parse_args()
    exchange = create_exchange(args.proxy)
    exchange.load_markets()
    symbols = [s for s in exchange.symbols if "XAU" in s or "XAUT" in s or "PAXG" in s]
    print(symbols)


if __name__ == "__main__":
    main()
