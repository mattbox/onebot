#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
test_plugin_retroachievements
----------------------------------

Tests for the retroachievements module
"""

import asyncio
import json
import logging
import unittest
from unittest.mock import AsyncMock, patch

from onebot.plugins.retroachievements import RetroAchievementsPlugin
from onebot.testing import BotTestCase

from .aiohttp_stubs import FakeResponse, fake_session
from .test_plugin_users import MockDb

API = "https://retroachievements.org/API"
GAME_EXTENDED = API + "/API_GetGameExtended.php"

GAME_INFO = {
    "ID": 1,
    "Title": "Sonic the Hedgehog",
    "ConsoleName": "Mega Drive",
    "NumDistinctPlayers": 4321,
    "NumAchievements": 3,
    "Achievements": {
        "9": {"ID": 9, "Title": "Green Hill", "Points": 5},
        "10": {"ID": 10, "Title": "Marble Zone", "Points": 10},
        "11": {"ID": 11, "Title": "Ring Master", "Points": 25},
    },
}

FORMATTED_GAME = (
    "🎮 \x02Sonic the Hedgehog [Mega Drive]\x02 | 👤 \x03024321\x03 | "
    "\x02\x03083\x02 Achievements worth \x0240\x02 Points\x0f"
)


class StubBot(object):
    """Just enough bot to construct the plugin outside of irc3."""

    def __init__(self, api_key="test-key"):
        self.config = {"onebot.plugins.retroachievements": {"api_key": api_key}}
        self.log = logging.getLogger("test.retroachievements")
        self.db = MockDb()


def _plugin(api_key="test-key"):
    return RetroAchievementsPlugin(StubBot(api_key=api_key))


class FetchRaDataTest(unittest.TestCase):
    def setUp(self):
        self.plugin = _plugin()

    def _run(self, *responses, params=None):
        factory, session = fake_session(*responses)
        with patch("onebot.plugins.retroachievements.aiohttp.ClientSession", factory):
            result = asyncio.run(
                self.plugin._fetch_RA_data(GAME_EXTENDED, dict(params or {"i": 1}))
            )
        return result, session

    def test_returns_the_decoded_payload(self):
        result, session = self._run(FakeResponse(json_data=GAME_INFO))
        assert result == GAME_INFO
        method, url, kwargs = session.requests[0]
        assert method == "GET"
        assert url == GAME_EXTENDED

    def test_adds_the_api_key_to_the_params(self):
        _, session = self._run(FakeResponse(json_data=GAME_INFO))
        assert session.requests[0][2]["params"] == {"i": 1, "y": "test-key"}

    def test_empty_payload_becomes_none(self):
        result, _ = self._run(FakeResponse(json_data={}))
        assert result is None

    def test_http_error_becomes_none(self):
        result, _ = self._run(FakeResponse(status=404, json_data=GAME_INFO))
        assert result is None

    def test_exceptions_are_swallowed(self):
        result, _ = self._run(asyncio.TimeoutError())
        assert result is None


class ConsoleAndGameCacheTest(unittest.TestCase):
    def setUp(self):
        self.plugin = _plugin()

    def test_console_ids_are_ints(self):
        self.plugin._fetch_RA_data = AsyncMock(
            return_value=[{"ID": "1", "Name": "Mega Drive"}, {"ID": 2, "Name": "SNES"}]
        )
        assert asyncio.run(self.plugin._fetch_console_ids()) == [1, 2]

    def test_console_ids_skips_entries_without_id(self):
        self.plugin._fetch_RA_data = AsyncMock(return_value=[{"Name": "Broken"}])
        assert asyncio.run(self.plugin._fetch_console_ids()) == []

    def test_console_ids_when_the_api_fails(self):
        self.plugin._fetch_RA_data = AsyncMock(return_value=None)
        assert asyncio.run(self.plugin._fetch_console_ids()) == []

    def test_build_cache_collects_games_from_every_console(self):
        self.plugin._fetch_console_ids = AsyncMock(return_value=[1, 2])
        self.plugin._fetch_RA_data = AsyncMock(
            side_effect=[
                [{"ID": 10}, {"ID": "11"}],
                [{"ID": 20}, {"NoID": True}],
            ]
        )
        game_ids = asyncio.run(self.plugin._build_game_cache())
        assert game_ids == [10, 11, 20]
        assert json.loads(self.plugin.bot.db["ra_game_ids"]["game_ids"]) == [10, 11, 20]

    def test_build_cache_without_consoles_stores_nothing(self):
        self.plugin._fetch_console_ids = AsyncMock(return_value=[])
        assert asyncio.run(self.plugin._build_game_cache()) == []
        assert "ra_game_ids" not in self.plugin.bot.db

    def test_build_cache_tolerates_a_failing_console(self):
        self.plugin._fetch_console_ids = AsyncMock(return_value=[1, 2])
        self.plugin._fetch_RA_data = AsyncMock(side_effect=[None, [{"ID": 20}]])
        assert asyncio.run(self.plugin._build_game_cache()) == [20]

    def test_cached_ids_are_reused(self):
        self.plugin.bot.db.set("ra_game_ids", game_ids=json.dumps([1, 2, 3]))
        self.plugin._build_game_cache = AsyncMock()
        assert asyncio.run(self.plugin._get_cached_game_ids()) == [1, 2, 3]
        self.plugin._build_game_cache.assert_not_awaited()

    def test_cache_is_built_when_empty(self):
        self.plugin._build_game_cache = AsyncMock(return_value=[7])
        assert asyncio.run(self.plugin._get_cached_game_ids()) == [7]
        self.plugin._build_game_cache.assert_awaited_once()

    def test_cache_is_rebuilt_when_the_stored_list_is_empty(self):
        self.plugin.bot.db.set("ra_game_ids", game_ids=json.dumps([]))
        self.plugin._build_game_cache = AsyncMock(return_value=[7])
        assert asyncio.run(self.plugin._get_cached_game_ids()) == [7]
        self.plugin._build_game_cache.assert_awaited_once()


class FormatGameMessageTest(unittest.TestCase):
    def setUp(self):
        self.plugin = _plugin()

    def test_full_game(self):
        assert self.plugin._format_game_msg(GAME_INFO) == FORMATTED_GAME

    def test_game_without_achievements_omits_the_points(self):
        game = dict(GAME_INFO, NumAchievements=0, Achievements={})
        assert self.plugin._format_game_msg(game) == (
            "🎮 \x02Sonic the Hedgehog [Mega Drive]\x02 | 👤 \x03024321\x03"
        )

    def test_missing_fields_do_not_raise(self):
        assert self.plugin._format_game_msg({}) == (
            "🎮 \x02None [None]\x02 | 👤 \x03020\x03"
        )

    def test_achievements_without_points_count_as_zero(self):
        game = dict(GAME_INFO, NumAchievements=1, Achievements={"9": {"ID": 9}})
        assert self.plugin._format_game_msg(game).endswith(
            "\x02\x03081\x02 Achievements worth \x020\x02 Points\x0f"
        )


class RetroAchievementsBotTestCase(BotTestCase):
    config = {
        "includes": [
            "onebot.plugins.retroachievements",
            "irc3.plugins.command",
        ],
        "onebot.plugins.retroachievements": {"api_key": "test-key"},
        "cmd": "!",
        "nick": "onebot",
        "loop": None,
    }

    @patch("irc3.plugins.storage.Storage")
    def setUp(self, mock):
        super().setUp()
        self.config["loop"] = asyncio.new_event_loop()
        asyncio.set_event_loop(self.config["loop"])
        self.callFTU()
        self.bot.db = MockDb({"the@boss": {"permissions": {"all_permissions"}}})
        self.plugin = self.bot.get_plugin(
            "onebot.plugins.retroachievements.RetroAchievementsPlugin"
        )

    def dispatch(self, line):
        """Dispatch a line and let the resulting task run to completion."""
        self.bot.dispatch(line)
        self.bot.loop.run_until_complete(asyncio.sleep(0.01))

    def assertSent(self, lines):
        """Assert that these lines have been sent.

        irc3's version pokes at a mocked event loop; we run a real one.
        """
        self.assertEqual(self.bot.sent, lines)


class RaCommandTest(RetroAchievementsBotTestCase):
    def test_random_game(self):
        self.plugin._get_cached_game_ids = AsyncMock(return_value=[1, 2, 3])
        self.plugin._fetch_RA_data = AsyncMock(return_value=GAME_INFO)
        with patch("onebot.plugins.retroachievements.random.choice", return_value=2):
            self.dispatch(":user!user@host PRIVMSG #chan :!ra")
        self.plugin._fetch_RA_data.assert_awaited_once_with(GAME_EXTENDED, {"i": 2})
        self.assertSent(
            [
                "PRIVMSG #chan :"
                + FORMATTED_GAME
                + " | https://retroachievements.org/game/2"
            ]
        )

    def test_answers_the_sender_in_private(self):
        self.plugin._get_cached_game_ids = AsyncMock(return_value=[2])
        self.plugin._fetch_RA_data = AsyncMock(return_value=GAME_INFO)
        self.dispatch(":user!user@host PRIVMSG onebot :!ra")
        self.assertSent(
            [
                "PRIVMSG user :"
                + FORMATTED_GAME
                + " | https://retroachievements.org/game/2"
            ]
        )

    def test_empty_cache(self):
        self.plugin._get_cached_game_ids = AsyncMock(return_value=[])
        self.dispatch(":user!user@host PRIVMSG #chan :!ra")
        self.assertSent(["PRIVMSG #chan :No games cached. Try .racache first."])

    def test_game_details_unavailable(self):
        self.plugin._get_cached_game_ids = AsyncMock(return_value=[1])
        self.plugin._fetch_RA_data = AsyncMock(return_value=None)
        self.dispatch(":user!user@host PRIVMSG #chan :!ra")
        self.assertSent(["PRIVMSG #chan :Could not fetch game details. Try again!"])

    def test_game_without_a_title(self):
        self.plugin._get_cached_game_ids = AsyncMock(return_value=[1])
        self.plugin._fetch_RA_data = AsyncMock(return_value={"ID": 1})
        self.dispatch(":user!user@host PRIVMSG #chan :!ra")
        self.assertSent(["PRIVMSG #chan :Could not fetch game details. Try again!"])


class RaCacheCommandTest(RetroAchievementsBotTestCase):
    def test_reports_the_number_of_cached_games(self):
        self.plugin._build_game_cache = AsyncMock(return_value=[1, 2, 3])
        self.dispatch(":im!the@boss PRIVMSG #chan :!racache")
        self.plugin._build_game_cache.assert_awaited_once()
        self.assertSent(["PRIVMSG #chan :Cached 3 games."])

    def test_reports_an_empty_rebuild(self):
        self.plugin._build_game_cache = AsyncMock(return_value=[])
        self.dispatch(":im!the@boss PRIVMSG #chan :!racache")
        self.assertSent(["PRIVMSG #chan :Cached 0 games."])

    def test_answers_the_sender_in_private(self):
        self.plugin._build_game_cache = AsyncMock(return_value=[1, 2, 3])
        self.dispatch(":im!the@boss PRIVMSG onebot :!racache")
        self.assertSent(["PRIVMSG im :Cached 3 games."])


class GameUrlEventTest(RetroAchievementsBotTestCase):
    def test_game_url_is_expanded(self):
        self.plugin._fetch_RA_data = AsyncMock(return_value=GAME_INFO)
        self.dispatch(
            ":user!user@host PRIVMSG #chan "
            ":neat https://retroachievements.org/game/1 right?"
        )
        self.plugin._fetch_RA_data.assert_awaited_once_with(GAME_EXTENDED, {"i": "1"})
        self.assertSent(["PRIVMSG #chan :" + FORMATTED_GAME])

    def test_unknown_game(self):
        self.plugin._fetch_RA_data = AsyncMock(return_value=None)
        self.dispatch(
            ":user!user@host PRIVMSG #chan :https://retroachievements.org/game/1"
        )
        self.assertSent(["PRIVMSG #chan :Game Not found..."])

    def test_ignores_its_own_messages(self):
        self.plugin._fetch_RA_data = AsyncMock(return_value=GAME_INFO)
        self.dispatch(
            ":onebot!bot@host PRIVMSG #chan :https://retroachievements.org/game/1"
        )
        self.plugin._fetch_RA_data.assert_not_awaited()
        self.assertSent([])

    def test_ignores_private_messages(self):
        self.plugin._fetch_RA_data = AsyncMock(return_value=GAME_INFO)
        self.dispatch(
            ":user!user@host PRIVMSG onebot :https://retroachievements.org/game/1"
        )
        self.plugin._fetch_RA_data.assert_not_awaited()
        self.assertSent([])


USER_INFO = {
    "User": "Scott",
    "TotalPoints": 12345,
    "TotalTruePoints": 23456,
    "RichPresenceMsg": "",
    "LastGameID": 1,
}


class UserUrlEventTest(RetroAchievementsBotTestCase):
    def test_user_without_rich_presence(self):
        self.plugin._fetch_RA_data = AsyncMock(return_value=USER_INFO)
        self.dispatch(
            ":user!user@host PRIVMSG #chan :https://retroachievements.org/user/Scott"
        )
        self.plugin._fetch_RA_data.assert_awaited_once_with(
            API + "/API_GetUserProfile.php", {"u": "Scott"}
        )
        self.assertSent(["PRIVMSG #chan :\x02Scott\x02 \x030212345\x03 (23456)"])

    def test_user_with_rich_presence_fetches_the_game(self):
        user_info = dict(USER_INFO, RichPresenceMsg="Playing Green Hill Zone")
        self.plugin._fetch_RA_data = AsyncMock(side_effect=[user_info, GAME_INFO])
        self.dispatch(
            ":user!user@host PRIVMSG #chan :https://retroachievements.org/user/Scott"
        )
        assert self.plugin._fetch_RA_data.await_args_list[1].args == (
            GAME_EXTENDED,
            {"i": 1},
        )
        self.assertSent(
            [
                "PRIVMSG #chan :\x02Scott\x02 \x030212345\x03 (23456) "
                "Most Recent Game: \x0308Sonic the Hedgehog\x03 "
                "[ Playing Green Hill Zone ]"
            ]
        )

    def test_unknown_user(self):
        self.plugin._fetch_RA_data = AsyncMock(return_value=None)
        self.dispatch(
            ":user!user@host PRIVMSG #chan :https://retroachievements.org/user/Nobody"
        )
        self.assertSent(["PRIVMSG #chan :User Not found..."])

    def test_ignores_its_own_messages(self):
        self.plugin._fetch_RA_data = AsyncMock(return_value=USER_INFO)
        self.dispatch(
            ":onebot!bot@host PRIVMSG #chan :https://retroachievements.org/user/Scott"
        )
        self.plugin._fetch_RA_data.assert_not_awaited()
        self.assertSent([])


CHEEVO_INFO = {
    "Achievement": {
        "ID": 9,
        "Title": "Green Hill",
        "Description": "Finish Green Hill Zone",
        "Points": 5,
        "TrueRatio": 7,
    },
    "Game": {"ID": 1, "Title": "Sonic the Hedgehog"},
    "UnlocksCount": 250,
    "UnlocksHardcoreCount": 100,
    "TotalPlayers": 1000,
}


class AchievementUrlEventTest(RetroAchievementsBotTestCase):
    def test_achievement_url_is_expanded(self):
        self.plugin._fetch_RA_data = AsyncMock(return_value=CHEEVO_INFO)
        self.dispatch(
            ":user!user@host PRIVMSG #chan :https://retroachievements.org/achievement/9"
        )
        self.plugin._fetch_RA_data.assert_awaited_once_with(
            API + "/API_GetAchievementUnlocks.php", {"a": "9"}
        )
        self.assertSent(
            [
                "PRIVMSG #chan :🎮 \x02Sonic the Hedgehog\x02 | "
                "\x0308Green Hill\x03 \x03025\x03 (7) - Finish Green Hill Zone "
                "| 25.0% unlock rate [\x0302250 (100) of 1000\x03]"
            ]
        )

    def test_unlock_rate_is_rounded_to_two_decimals(self):
        cheevo = dict(CHEEVO_INFO, UnlocksCount=1, TotalPlayers=3)
        self.plugin._fetch_RA_data = AsyncMock(return_value=cheevo)
        self.dispatch(
            ":user!user@host PRIVMSG #chan :https://retroachievements.org/achievement/9"
        )
        assert "33.33% unlock rate" in self.bot.sent[0]

    def test_unknown_achievement(self):
        self.plugin._fetch_RA_data = AsyncMock(return_value=None)
        self.dispatch(
            ":user!user@host PRIVMSG #chan :https://retroachievements.org/achievement/9"
        )
        self.assertSent(["PRIVMSG #chan :Cheevo Not found..."])

    def test_ignores_private_messages(self):
        self.plugin._fetch_RA_data = AsyncMock(return_value=CHEEVO_INFO)
        self.dispatch(
            ":user!user@host PRIVMSG onebot "
            ":https://retroachievements.org/achievement/9"
        )
        self.plugin._fetch_RA_data.assert_not_awaited()
        self.assertSent([])
