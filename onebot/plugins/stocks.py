"""
================================================
:mod:`onebot.plugins.stocks`
================================================

This plugin allows to query stocks
"""

from typing import Self
from irc3 import plugin, event
import yfinance as yf

import logging

logger = logging.getLogger(__name__)


NOT_FOUND_MSG = "Symbol not found"
UNSUPPORTED = "unsupported stock"


def stocks(symbol):
    """collects and parses stock information"""
    comp = yf.Ticker(symbol)

    name = comp.info.get("shortName")
    symbol = comp.info.get("symbol")
    if symbol is None:
        return NOT_FOUND_MSG

    qType = comp.info.get("quoteType")

    # Determine quote Type
    if qType == "EQUITY":
        price = comp.info.get("currentPrice")
        init = comp.info.get("previousClose")
        mod = None
    elif qType == "MUTUALFUND":
        price = comp.info.get("previousClose")
        hist = comp.history(period="1mo")
        init = hist["Close"][hist.index.min()]
        mod = " past month"
    elif qType == "INDEX" or "ETF":
        price = comp.info.get("ask")
        init = comp.info.get("previousClose")
        mod = None
    else:
        return UNSUPPORTED

    diff = price - init
    pct = (diff/price) * 100

    change = f"{diff:.2f}({pct:.1f}%)"
    if diff > 0:
        day_change = f"\x033${price:.2f} ▲ {change}\x03"  # green
    elif diff < 0:
        day_change = f"\x034${price:.2f} ▼ {change}\x03"  # red
    else:
        day_change = f"{price} {change}"

    if symbol == "TSLA":
        symbol = "🚀"

    response = f"\x02{name}\x02 (${symbol}) {day_change}"
    if mod:
        response += mod

    high = comp.info.get("dayHigh")
    low = comp.info.get("dayLow")
    vol = _human(comp.info.get("volume"))
    if None not in (high, low, vol):
        movement = f" \x0314[\x03 H:{high:.2f} \x0314|\x03 L:{low:.2f} \x0314|\x03 Vol:{vol} \x0314]\x03"
        response += movement

    return response


def _human(n):
    units = ['', '', 'K', 'M', 'B']
    if n is None:
        return None
    sn = len(str(n))
    rm = sn % 3
    unit = sn // 3 + (rm > 0)
    val = str(n)[:3] if rm == 0 else str(n)[:rm]
    return f"{val}{units[unit]}"


@plugin
class StocksPlugin(object):
    """Stocks Plugin"""

    def __init__(self, bot):
        """Initialise the plugin"""
        self.bot = bot
        self.log = bot.log.getChild(__name__)
        self.config = bot.config.get(__name__, {})

    @event(
        r"^:(?P<mask>\S+!\S+@\S+) (?P<event>PRIVMSG|NOTICE) "
        r"(?P<target>#\S+) :.*\$(?P<data>\^?[A-Za-z]+)\b"
    )
    def on_msg(self, mask, event, target, data):
        """Check the value of your stonks."""

        if (
            mask.nick == self.bot.nick
            or not target.is_channel
        ):
            return

        message = stocks(data)
        self.bot.privmsg(target, message)

    @classmethod
    def reload(cls, old: Self) -> Self:  # pragma: no cover
        return cls(old.bot)
