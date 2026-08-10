# -*- coding: utf-8 -*-
"""
====================================================================
:mod:`onebot.plugins.isthereanydeal` IsThereAnyDeal plugin
====================================================================

Shows price information from IsThereAnyDeal.com when a Steam store
URL is posted in chat. Requires an API key from isthereanydeal.com.
"""

from urllib.parse import quote

import aiohttp
from irc3 import plugin, event

ITAD_API_BASE = "https://api.isthereanydeal.com"
CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}


def _redact_key(text, key):
    """Strip an API key out of text bound for the log.

    aiohttp puts the whole request URL in its exception messages, and the key
    travels there as a query parameter.

    >>> _redact_key("url='https://x/v1?key=s3cret&appid=220'", "s3cret")
    "url='https://x/v1?key=<redacted>&appid=220'"
    >>> _redact_key("nothing to hide", None)
    'nothing to hide'
    """
    if not key:
        return text
    for form in (key, quote(key, safe="")):
        text = text.replace(form, "<redacted>")
    return text


def _format_price(amount, currency):
    """Format a price with currency symbol or code.

    >>> _format_price(9.99, "USD")
    '$9.99'
    >>> _format_price(9.99, "EUR")
    '€9.99'
    >>> _format_price(9.99, "CAD")
    'CAD 9.99'
    """
    symbol = CURRENCY_SYMBOLS.get(currency)
    if symbol:
        return f"{symbol}{amount:.2f}"
    return f"{currency} {amount:.2f}"


@plugin
class IsThereAnyDealPlugin(object):
    """
    A OneBot plugin for IsThereAnyDeal.com price lookups.

    Triggers on Steam store URLs and returns current best price,
    discount info, and historic low price from IsThereAnyDeal.
    """

    def __init__(self, bot):
        """Initialise the plugin"""
        self.bot = bot
        self.log = bot.log.getChild(__name__)
        self.config = bot.config.get(__name__, {})
        self.api_key = self.config.get("api_key")
        if not self.api_key:
            raise Exception(
                "You need to set the IsThereAnyDeal api_key "
                "in the config section [{}]".format(__name__)
            )

    async def _lookup_game(self, appid):
        """Look up ITAD game by Steam App ID.

        Returns the game dict (id, slug, title) or None if not found.
        """
        url = f"{ITAD_API_BASE}/games/lookup/v1"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    params={"key": self.api_key, "appid": appid},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    if not data.get("found"):
                        return None
                    return data["game"]
        except Exception as e:
            self.log.error(
                "Error looking up game %s: %s", appid, _redact_key(str(e), self.api_key)
            )
            return None

    async def _get_overview(self, game_id):
        """Get price overview for a game by ITAD UUID.

        Returns the price entry dict or None if unavailable.
        """
        url = f"{ITAD_API_BASE}/games/overview/v2"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    params={"key": self.api_key, "country": "US"},
                    json=[game_id],
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    prices = data.get("prices", [])
                    return prices[0] if prices else None
        except Exception as e:
            self.log.error(
                "Error fetching overview for %s: %s",
                game_id,
                _redact_key(str(e), self.api_key),
            )
            return None

    def _format_message(self, game, overview):
        """Format the price summary message."""
        # URL info bot plugin will usually fetch the game title for us
        # parts = [f"\x02{game['title']}\x02"]
        parts = []

        current = overview.get("current")
        if current:
            price_str = _format_price(
                current["price"]["amount"], current["price"]["currency"]
            )
            shop = current["shop"]["name"]
            cut = current.get("cut", 0)
            if cut > 0:
                regular_str = _format_price(
                    current["regular"]["amount"], current["regular"]["currency"]
                )
                parts.append(
                    f"Best: {price_str} on {shop} (-{cut}%, reg. {regular_str})"
                )
            else:
                parts.append("No current deals :(")
                # parts.append(f"{price_str} on {shop}")
                return " || ".join(parts)
        else:
            parts.append("Game not tracked")

        lowest = overview.get("lowest")
        if lowest:
            # `current` and `lowest` are independently nullable: a delisted
            # game keeps its price history but has nothing on sale today
            if current and lowest["price"]["amount"] == current["price"]["amount"]:
                parts.append("🔥 All time low 🔥")
                return " || ".join(parts)
            low_str = _format_price(
                lowest["price"]["amount"], lowest["price"]["currency"]
            )
            parts.append(f"Historic low: {low_str}")

        # parts.append(f"https://isthereanydeal.com/game/{game["slug"]}/")
        return " || ".join(parts)

    @event(
        r"^:(?P<mask>\S+!\S+@\S+) (?P<event>(PRIVMSG)) "
        r"(?P<target>\S+) :\s*(?P<data>.*https?://store\.steampowered\.com"
        r"/app/(?P<appid>\d+).*)$"
    )
    async def on_steam_url(self, mask, target, appid, **kwargs):
        """Triggered when a Steam store URL is posted in a channel."""
        if mask.nick == self.bot.nick or not target.is_channel:
            return

        game = await self._lookup_game(appid)
        if not game:
            return

        overview = await self._get_overview(game["id"])
        if not overview:
            return

        self.bot.privmsg(target, self._format_message(game, overview))
