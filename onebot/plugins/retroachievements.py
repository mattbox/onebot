# -*- coding: utf8 -*-
"""
======================================================
:mod:`onebot.plugins.retroachievements` RetroAchievements plugin for OneBot
======================================================

"""

import json
import random

import aiohttp
import irc3
from irc3.plugins.command import command

ASCII_BLU = "\x0302"
ASCII_YLW = "\x0308"


@irc3.plugin
class RetroAchievementsPlugin(object):
    """
    A OneBot plugin for RetroAchievements.
    """

    requires = [
        "irc3.plugins.command",
        "irc3.plugins.storage",
    ]

    def __init__(self, bot):
        """Initialise the plugin"""
        self.bot = bot
        self.log = bot.log.getChild(__name__)
        self.config = bot.config.get(__name__, {})

        try:
            self.api_key = self.config.get("api_key")

        except KeyError:  # pragma: no cover
            raise Exception(
                "You need to set the RetroAchievements api_key "
                "in the config section [{}]".format(__name__)
            )

    async def _fetch_RA_data(self, base_url, params):
        """
        Communicates with the RA API to retrieve game details.
        Returns a dictionary of data or None if the request fails.
        """
        params.update({"y": self.api_key})
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(base_url, params=params, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # The API returns an empty dict or error fields if the ID is invalid
                        return data if data else None
                    return None
        except Exception as e:
            self.bot.log.error(f"Fatal exception occurred. Aborting: {str(e)}")
            return None

    async def _fetch_console_ids(self):
        """Fetch list of active gaming console IDs from RetroAchievements."""
        base_url = "https://retroachievements.org/API/API_GetConsoleIDs.php"
        data = await self._fetch_RA_data(base_url, {"a": 1, "g": 1})
        if not data:
            return []
        return [int(console["ID"]) for console in data if "ID" in console]

    async def _build_game_cache(self):
        """Build cache of all game IDs that have achievements."""
        console_ids = await self._fetch_console_ids()
        if not console_ids:
            self.log.warning("No console IDs returned from API")
            return []

        game_ids = []
        base_url = "https://retroachievements.org/API/API_GetGameList.php"
        for console_id in console_ids:
            data = await self._fetch_RA_data(base_url, {"i": console_id, "f": 1})
            if data:
                game_ids.extend(int(game["ID"]) for game in data if "ID" in game)

        self.log.info(
            "Cached %d games from %d consoles", len(game_ids), len(console_ids)
        )
        self.bot.db.set("ra_game_ids", game_ids=json.dumps(game_ids))
        return game_ids

    async def _get_cached_game_ids(self):
        """Get cached game IDs, building the cache if needed."""
        stored = self.bot.db.get("ra_game_ids", {})
        raw = stored.get("game_ids") if stored else None
        if raw:
            game_ids = json.loads(raw)
            if game_ids:
                return game_ids
        return await self._build_game_cache()

    def _format_game_msg(self, game_info):
        title = game_info.get("Title")
        console = game_info.get("ConsoleName")
        players = game_info.get("NumDistinctPlayers", 0)
        msg = [f"🎮 \x02{title} [{console}]\x02"]
        msg.append(f"👤 {ASCII_BLU}{players}\x03")
        count = game_info.get("NumAchievements", 0)
        if count > 0:
            total_points = sum(
                ach.get("Points", 0)
                for ach in game_info.get("Achievements", {}).values()
            )
            msg.append(
                f"\x02{ASCII_YLW}{count}\x02 "
                f"Achievements worth \x02{total_points}\x02 Points\x0f"
            )
        return " | ".join(msg)

    @command
    async def ra(self, mask, target, args):
        """Pick a random RetroAchievements game

        %%ra
        """
        game_ids = await self._get_cached_game_ids()
        if not game_ids:
            return "No games cached. Try .racache first."

        game_id = random.choice(game_ids)
        base_url = "https://retroachievements.org/API/API_GetGameExtended.php"
        game_info = await self._fetch_RA_data(base_url, {"i": game_id})
        if game_info and game_info.get("Title"):
            msg = self._format_game_msg(game_info)
            url = f"https://retroachievements.org/game/{game_id}"
            return f"{msg} | {url}"
        return "Could not fetch game details. Try again!"

    @command(permission="admin")
    async def racache(self, mask, target, args):
        """Refresh the RetroAchievements game cache

        %%racache
        """
        game_ids = await self._build_game_cache()
        return f"Cached {len(game_ids)} games."

    @irc3.event(
        r"^:(?P<mask>\S+!\S+@\S+) (?P<event>(PRIVMSG)) "
        r"(?P<target>\S+) :\s*(?P<data>.*https?://retroachievements\.org/game/(?P<id>\d+).*)$"
    )
    async def on_ra_game_detected(self, mask, target, id, **kwargs):
        """Event trigger that handles game detection and response."""
        if mask.nick == self.bot.nick or not target.is_channel:
            return

        # 1. Fetch data using the separate helper function
        base_url = "https://retroachievements.org/API/API_GetGameExtended.php"
        game_info = await self._fetch_RA_data(base_url, {"i": id})

        # 2. Handle response using separate format function
        if game_info:
            msg = self._format_game_msg(game_info)
            self.bot.privmsg(target, msg)
        else:
            self.bot.privmsg(target, "Game Not found...")

    @irc3.event(
        r"^:(?P<mask>\S+!\S+@\S+) (?P<event>(PRIVMSG)) "
        r"(?P<target>\S+) :\s*(?P<data>.*https?://retroachievements\.org/user/(?P<id>[^/\s]+).*)$"
    )
    async def on_ra_user_detected(self, mask, target, id, **kwargs):
        """Event trigger that handles user detection and response."""
        if mask.nick == self.bot.nick or not target.is_channel:
            return

        # 1. Fetch data using the separate helper function
        base_url = "https://retroachievements.org/API/API_GetUserProfile.php"
        user_info = await self._fetch_RA_data(base_url, {"u": id})

        # 2. Handle the response
        if user_info:
            user = user_info.get("User")
            totalPoints = user_info.get("TotalPoints")
            truePoints = user_info.get("TotalTruePoints")
            msg = [f"\x02{user}\x02 {ASCII_BLU}{totalPoints}\x03 ({truePoints})"]

            status = user_info.get("RichPresenceMsg")
            if status:
                gameID = user_info.get("LastGameID")
                game_url = "https://retroachievements.org/API/API_GetGameExtended.php"
                game_info = await self._fetch_RA_data(game_url, {"i": gameID})
                title = game_info.get("Title")
                msg.append(f"Most Recent Game: {ASCII_YLW}{title}\x03 [ {status} ]")
            self.bot.privmsg(target, "{}".format(" ".join(msg)))
        else:
            self.bot.privmsg(target, "User Not found...")

    @irc3.event(
        r"^:(?P<mask>\S+!\S+@\S+) (?P<event>(PRIVMSG)) "
        r"(?P<target>\S+) :\s*(?P<data>.*https?://retroachievements\.org/achievement/(?P<id>\d+).*)$"
    )
    async def on_ra_cheevo_detected(self, mask, target, id, **kwargs):
        """Event trigger that handles achievement detection and response."""
        if mask.nick == self.bot.nick or not target.is_channel:
            return

        # 1. Fetch data using the separate helper function
        base_url = "https://retroachievements.org/API/API_GetAchievementUnlocks.php"
        cheevo_info = await self._fetch_RA_data(base_url, {"a": id})

        # 2. Handle the response
        if cheevo_info:
            cheevo = cheevo_info.get("Achievement", {})
            game_title = cheevo_info.get("Game", {}).get("Title")
            msg = [
                f"🎮 \x02{game_title}\x02 | {ASCII_YLW}{cheevo.get('Title')}\x03 "
                f"{ASCII_BLU}{cheevo.get('Points')}\x03 "
                f"({cheevo.get('TrueRatio')}) - {cheevo.get('Description')}"
            ]

            unlocks = cheevo_info.get("UnlocksCount")
            unlocksHC = cheevo_info.get("UnlocksHardcoreCount")
            total_players = cheevo_info.get("TotalPlayers")
            percentage = round((unlocks / total_players) * 100, 2)

            msg.append(
                f"| {percentage}% unlock rate "
                f"[{ASCII_BLU}{unlocks} ({unlocksHC}) of {total_players}\x03]"
            )
            self.bot.privmsg(target, "{}".format(" ".join(msg)))
        else:
            self.bot.privmsg(target, "Cheevo Not found...")
