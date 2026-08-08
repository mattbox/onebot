#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
test_plugin_drink
----------------------------------

Tests for the drink module
"""

import asyncio
from unittest.mock import patch

from onebot.testing import BotTestCase


class DrinkTestCase(BotTestCase):
    config = {
        "includes": ["onebot.plugins.drink", "irc3.plugins.command"],
        "cmd": "!",
        "nick": "onebot",
    }

    def setUp(self):
        super().setUp()
        # irc3's testing call_soon() needs a current event loop to build its
        # (never scheduled) Handle with. The bot itself runs on a mocked loop.
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())
        self.callFTU()

    def test_serves_the_chosen_beverage(self):
        with patch("onebot.plugins.drink.random.choice", return_value="beer 🍺"):
            self.bot.dispatch(":user!user@host PRIVMSG #chan :!drink")
        self.assertSent(["PRIVMSG #chan :beer 🍺"])

    def test_answers_the_sender_in_private(self):
        with patch("onebot.plugins.drink.random.choice", return_value="coffee ☕"):
            self.bot.dispatch(":user!user@host PRIVMSG onebot :!drink")
        self.assertSent(["PRIVMSG user :coffee ☕"])

    def test_picks_from_the_full_beverage_list(self):
        with patch(
            "onebot.plugins.drink.random.choice", side_effect=lambda seq: seq[0]
        ) as choice:
            self.bot.dispatch(":user!user@host PRIVMSG #chan :!drink")
        (beverages,) = choice.call_args[0]
        assert len(beverages) == 20
        assert len(set(beverages)) == 20, "beverages should be unique"
        assert "coffee ☕" in beverages
        assert "whiskey 🥃" in beverages
        self.assertSent(["PRIVMSG #chan :coffee ☕"])

    def test_every_pick_is_a_known_beverage(self):
        with patch(
            "onebot.plugins.drink.random.choice", side_effect=lambda seq: seq[0]
        ) as choice:
            self.bot.dispatch(":user!user@host PRIVMSG #chan :!drink")
        beverages = choice.call_args[0][0]
        self.bot.sent  # discard, and reset the recorded lines

        for _ in range(25):
            self.bot.dispatch(":user!user@host PRIVMSG #chan :!drink")
            sent = self.bot.sent
            assert len(sent) == 1
            beverage = sent[0][len("PRIVMSG #chan :") :]
            assert beverage in beverages

    def test_takes_no_arguments(self):
        self.bot.dispatch(":user!user@host PRIVMSG #chan :!drink whisky")
        sent = self.bot.sent
        assert len(sent) == 1
        assert sent[0] == "PRIVMSG #chan :Invalid arguments."
