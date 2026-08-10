#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
test_plugin_isthereanydeal
----------------------------------

Tests for the isthereanydeal module
"""

import asyncio
import logging
import unittest
from unittest.mock import AsyncMock, patch

from onebot.plugins.isthereanydeal import (
    IsThereAnyDealPlugin,
    _format_price,
    _redact_key,
)
from onebot.testing import BotTestCase

from .aiohttp_stubs import FakeResponse, fake_session

GAME = {
    "id": "018d937f-1e69-7275-9b04-1c0e1b0dbcd4",
    "slug": "half-life-2",
    "title": "Half-Life 2",
}


def _overview(price=4.99, regular=9.99, cut=50, lowest=2.49, shop="Steam"):
    """Build an ITAD overview entry."""
    entry = {
        "current": {
            "shop": {"id": 61, "name": shop},
            "price": {"amount": price, "currency": "USD"},
            "regular": {"amount": regular, "currency": "USD"},
            "cut": cut,
        },
        "lowest": {
            "shop": {"id": 61, "name": shop},
            "price": {"amount": lowest, "currency": "USD"},
        },
    }
    return entry


class StubBot(object):
    """Just enough bot to construct the plugin outside of irc3."""

    def __init__(self, api_key="test-key"):
        section = {} if api_key is None else {"api_key": api_key}
        self.config = {"onebot.plugins.isthereanydeal": section}
        self.log = logging.getLogger("test.isthereanydeal")


class FormatPriceTest(unittest.TestCase):
    def test_known_currency_uses_symbol(self):
        assert _format_price(9.99, "USD") == "$9.99"
        assert _format_price(9.99, "EUR") == "€9.99"
        assert _format_price(9.99, "GBP") == "£9.99"

    def test_unknown_currency_uses_code(self):
        assert _format_price(9.99, "CAD") == "CAD 9.99"

    def test_always_two_decimals(self):
        assert _format_price(10, "USD") == "$10.00"
        assert _format_price(4.5, "USD") == "$4.50"

    def test_rounds_to_cents(self):
        assert _format_price(1.005, "SEK") == "SEK 1.00"


class RedactKeyTest(unittest.TestCase):
    def test_the_key_is_removed_from_a_url(self):
        text = "url='https://api.isthereanydeal.com/games/lookup/v1?key=s3cret'"
        assert _redact_key(text, "s3cret") == (
            "url='https://api.isthereanydeal.com/games/lookup/v1?key=<redacted>'"
        )

    def test_a_percent_encoded_key_is_removed_too(self):
        assert "s3c/ret" not in _redact_key("key=s3c%2Fret", "s3c/ret")

    def test_text_without_the_key_is_untouched(self):
        assert _redact_key("Cannot connect to host", "s3cret") == (
            "Cannot connect to host"
        )

    def test_no_key_configured(self):
        assert _redact_key("boom", None) == "boom"


class KeyIsNotLoggedTest(unittest.TestCase):
    """aiohttp puts the whole request URL, key included, in its exceptions."""

    def setUp(self):
        self.plugin = IsThereAnyDealPlugin(StubBot(api_key="s3cret"))

    def _log_from(self, coroutine_factory, response):
        factory, _ = fake_session(response)
        with patch("onebot.plugins.isthereanydeal.aiohttp.ClientSession", factory):
            with self.assertLogs("test.isthereanydeal", level="ERROR") as caught:
                asyncio.run(coroutine_factory())
        return "\n".join(caught.output)

    def test_lookup_errors_do_not_leak_the_key(self):
        boom = OSError("url='https://api.isthereanydeal.com/v1?key=s3cret'")
        output = self._log_from(lambda: self.plugin._lookup_game("220"), boom)
        assert "s3cret" not in output
        assert "<redacted>" in output

    def test_overview_errors_do_not_leak_the_key(self):
        boom = OSError("url='https://api.isthereanydeal.com/v2?key=s3cret'")
        output = self._log_from(lambda: self.plugin._get_overview(GAME["id"]), boom)
        assert "s3cret" not in output
        assert "<redacted>" in output


class PluginConstructionTest(unittest.TestCase):
    def test_requires_an_api_key(self):
        with self.assertRaises(Exception) as ctx:
            IsThereAnyDealPlugin(StubBot(api_key=None))
        assert "api_key" in str(ctx.exception)

    def test_reads_the_api_key_from_its_config_section(self):
        plugin = IsThereAnyDealPlugin(StubBot(api_key="s3cret"))
        assert plugin.api_key == "s3cret"


class FormatMessageTest(unittest.TestCase):
    def setUp(self):
        self.plugin = IsThereAnyDealPlugin(StubBot())

    def test_discounted_game_shows_deal_and_historic_low(self):
        message = self.plugin._format_message(GAME, _overview())
        assert message == (
            "Best: $4.99 on Steam (-50%, reg. $9.99) || Historic low: $2.49"
        )

    def test_price_matching_the_historic_low_is_flagged(self):
        overview = _overview(price=2.49, lowest=2.49, cut=75)
        message = self.plugin._format_message(GAME, overview)
        assert message == (
            "Best: $2.49 on Steam (-75%, reg. $9.99) || 🔥 All time low 🔥"
        )

    def test_no_discount_short_circuits(self):
        overview = _overview(price=9.99, cut=0)
        assert self.plugin._format_message(GAME, overview) == "No current deals :("

    def test_untracked_game_without_lowest(self):
        assert self.plugin._format_message(GAME, {}) == "Game not tracked"

    def test_untracked_game_that_still_has_price_history(self):
        """A delisted game keeps its `lowest` while `current` goes null."""
        overview = _overview()
        overview["current"] = None
        assert self.plugin._format_message(GAME, overview) == (
            "Game not tracked || Historic low: $2.49"
        )

    def test_other_currencies_are_passed_through(self):
        overview = _overview()
        for key in ("current", "lowest"):
            overview[key]["price"]["currency"] = "PLN"
        overview["current"]["regular"]["currency"] = "PLN"
        message = self.plugin._format_message(GAME, overview)
        assert message == (
            "Best: PLN 4.99 on Steam (-50%, reg. PLN 9.99) || Historic low: PLN 2.49"
        )


class LookupGameTest(unittest.TestCase):
    def setUp(self):
        self.plugin = IsThereAnyDealPlugin(StubBot())

    def _run(self, *responses):
        factory, session = fake_session(*responses)
        with patch("onebot.plugins.isthereanydeal.aiohttp.ClientSession", factory):
            result = asyncio.run(self.plugin._lookup_game("220"))
        return result, session

    def test_found(self):
        result, session = self._run(
            FakeResponse(json_data={"found": True, "game": GAME})
        )
        assert result == GAME
        method, url, kwargs = session.requests[0]
        assert method == "GET"
        assert url == "https://api.isthereanydeal.com/games/lookup/v1"
        assert kwargs["params"] == {"key": "test-key", "appid": "220"}

    def test_not_found(self):
        result, _ = self._run(FakeResponse(json_data={"found": False}))
        assert result is None

    def test_http_error(self):
        result, _ = self._run(FakeResponse(status=500, json_data={}))
        assert result is None

    def test_connection_error_is_swallowed(self):
        result, _ = self._run(OSError("boom"))
        assert result is None


class GetOverviewTest(unittest.TestCase):
    def setUp(self):
        self.plugin = IsThereAnyDealPlugin(StubBot())

    def _run(self, *responses):
        factory, session = fake_session(*responses)
        with patch("onebot.plugins.isthereanydeal.aiohttp.ClientSession", factory):
            result = asyncio.run(self.plugin._get_overview(GAME["id"]))
        return result, session

    def test_returns_the_first_price_entry(self):
        entry = _overview()
        result, session = self._run(FakeResponse(json_data={"prices": [entry, {}]}))
        assert result == entry
        method, url, kwargs = session.requests[0]
        assert method == "POST"
        assert url == "https://api.isthereanydeal.com/games/overview/v2"
        assert kwargs["params"] == {"key": "test-key", "country": "US"}
        assert kwargs["json"] == [GAME["id"]]

    def test_no_prices(self):
        result, _ = self._run(FakeResponse(json_data={"prices": []}))
        assert result is None

    def test_missing_prices_key(self):
        result, _ = self._run(FakeResponse(json_data={}))
        assert result is None

    def test_http_error(self):
        result, _ = self._run(FakeResponse(status=403, json_data={}))
        assert result is None

    def test_timeout_is_swallowed(self):
        result, _ = self._run(asyncio.TimeoutError())
        assert result is None


class SteamUrlEventTest(BotTestCase):
    config = {
        "includes": ["onebot.plugins.isthereanydeal"],
        "onebot.plugins.isthereanydeal": {"api_key": "test-key"},
        "cmd": "!",
        "nick": "onebot",
        "loop": None,
    }

    def setUp(self):
        super().setUp()
        self.config["loop"] = asyncio.new_event_loop()
        asyncio.set_event_loop(self.config["loop"])
        self.callFTU()
        self.plugin = self.bot.get_plugin(
            "onebot.plugins.isthereanydeal.IsThereAnyDealPlugin"
        )
        self.plugin._lookup_game = AsyncMock(return_value=GAME)
        self.plugin._get_overview = AsyncMock(return_value=_overview())

    def dispatch(self, line):
        """Dispatch a line and let the resulting task run to completion."""
        self.bot.dispatch(line)
        self.bot.loop.run_until_complete(asyncio.sleep(0.01))

    def assertSent(self, lines):
        """Assert that these lines have been sent.

        irc3's version pokes at a mocked event loop; we run a real one.
        """
        self.assertEqual(self.bot.sent, lines)

    def test_steam_url_triggers_a_price_summary(self):
        self.dispatch(
            ":user!user@host PRIVMSG #chan "
            ":look https://store.steampowered.com/app/220/HalfLife_2/"
        )
        self.plugin._lookup_game.assert_awaited_once_with("220")
        self.plugin._get_overview.assert_awaited_once_with(GAME["id"])
        self.assertSent(
            [
                "PRIVMSG #chan :Best: $4.99 on Steam (-50%, reg. $9.99) "
                "|| Historic low: $2.49"
            ]
        )

    def test_plain_url_without_trailing_path(self):
        self.dispatch(
            ":user!user@host PRIVMSG #chan :http://store.steampowered.com/app/220"
        )
        self.plugin._lookup_game.assert_awaited_once_with("220")

    def test_unknown_game_stays_quiet(self):
        self.plugin._lookup_game.return_value = None
        self.dispatch(
            ":user!user@host PRIVMSG #chan :https://store.steampowered.com/app/220/"
        )
        self.plugin._get_overview.assert_not_awaited()
        self.assertSent([])

    def test_missing_overview_stays_quiet(self):
        self.plugin._get_overview.return_value = None
        self.dispatch(
            ":user!user@host PRIVMSG #chan :https://store.steampowered.com/app/220/"
        )
        self.assertSent([])

    def test_ignores_its_own_messages(self):
        self.dispatch(
            ":onebot!bot@host PRIVMSG #chan :https://store.steampowered.com/app/220/"
        )
        self.plugin._lookup_game.assert_not_awaited()
        self.assertSent([])

    def test_ignores_private_messages(self):
        self.dispatch(
            ":user!user@host PRIVMSG onebot :https://store.steampowered.com/app/220/"
        )
        self.plugin._lookup_game.assert_not_awaited()
        self.assertSent([])

    def test_ignores_other_steam_urls(self):
        self.dispatch(
            ":user!user@host PRIVMSG #chan "
            ":https://store.steampowered.com/search/?term=half+life"
        )
        self.plugin._lookup_game.assert_not_awaited()
        self.assertSent([])
