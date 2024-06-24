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


def stocks(symbol):
    """collects and parses stock information"""
    comp = yf.Ticker(symbol)
    try:
        price = comp.info["currentPrice"]
    except KeyError:
        price = comp.info["open"]

# Return error msg if anything is missing.
    try:
        name = comp.info['shortName']
        symbol = comp.info["symbol"]
        high = comp.info["dayHigh"]
        low = comp.info["dayLow"]
        vol = comp.info["volume"]
    except KeyError:
        return NOT_FOUND_MSG

    diff = price - comp.info["previousClose"]
    pct = (diff/price) * 100
    change = f"{diff:.2f}({pct:.1f}%)"
    vol = _human(vol)

    if diff >= 0:
        day_change = f"\x033▲ {change}\x03"  # green
    else:
        day_change = f"\x034▼ {change}\x03"  # red

    if symbol == "TSLA":
        symbol = "🚀"
    msg = f"\x02{name}\x02 ({symbol}) ${price:.2f} {day_change} High:{
        high:.2f}|Low:{low:.2f}|Vol:{vol}"

    return msg


def _human(n):
    units = ['', '', 'K', 'M', 'B']
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
