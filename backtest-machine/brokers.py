"""
Broker adapters for The Backtest Machine paper/live trader.

Paper mode needs no broker — fills are simulated at the next day's open.
Live mode requires kiteconnect and valid credentials in config.json:

    "zerodha": {"api_key": "...", "access_token": "..."}

Access tokens for Kite Connect expire daily; regenerate each morning via
the Kite login flow. Live orders are placed as AMO market orders so a
post-market-close run executes at the next day's open, matching the
paper simulation.
"""


class PaperBroker:
    """No-op broker: the trader simulates fills itself."""
    name = "paper"

    def place_order(self, symbol, side, qty, product="CNC", variety="amo"):
        return f"PAPER-{side}-{symbol}-{qty}"


class ZerodhaBroker:
    name = "zerodha"

    def __init__(self, api_key, access_token):
        from kiteconnect import KiteConnect  # pip install kiteconnect
        self.kite = KiteConnect(api_key=api_key)
        self.kite.set_access_token(access_token)

    def place_order(self, symbol, side, qty, product="CNC", variety="amo"):
        # yfinance symbol "RELIANCE.NS" -> NSE tradingsymbol "RELIANCE"
        # product: CNC (delivery/swing) or MIS (intraday, auto square-off)
        # variety: "amo" for after-market orders, "regular" during hours
        tradingsymbol = symbol.replace(".NS", "")
        return self.kite.place_order(
            variety=(self.kite.VARIETY_AMO if variety == "amo"
                     else self.kite.VARIETY_REGULAR),
            exchange=self.kite.EXCHANGE_NSE,
            tradingsymbol=tradingsymbol,
            transaction_type=side,          # "BUY" / "SELL"
            quantity=qty,
            product=(self.kite.PRODUCT_MIS if product == "MIS"
                     else self.kite.PRODUCT_CNC),
            order_type=self.kite.ORDER_TYPE_MARKET,
        )


def get_broker(config):
    if config.get("mode") == "live":
        z = config.get("zerodha", {})
        if not (z.get("api_key") and z.get("access_token")):
            raise SystemExit(
                "mode is 'live' but zerodha api_key/access_token missing "
                "in config.json — refusing to start."
            )
        return ZerodhaBroker(z["api_key"], z["access_token"])
    return PaperBroker()
