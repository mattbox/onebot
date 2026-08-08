# -*- coding: utf8 -*-
"""
======================================================
:mod:`onebot.plugins.drink` fun drink plugin for OneBot
======================================================

"""

from typing import Self
import irc3
import random
from irc3.plugins.command import command


@irc3.plugin
class DrinkPlugin(object):
    """
    A simple beverage serving plugin for OneBot.
    """

    requires = ["irc3.plugins.command"]

    def __init__(self, bot):
        """Initialise the plugin"""
        self.bot = bot
        self.log = bot.log.getChild(__name__)

    @command
    def drink(self, mask, target, args):
        """Offers a random beverage to the user.

        %%drink
        """
        beverages = [
            "coffee ☕",
            "green tea 🍵",
            "pot of tea 🫖",
            "soda 🥤",
            "juice 🧃",
            "glass of milk 🥛",
            "beer 🍺",
            "several beers 🍻",
            "wine 🍷",
            "martini 🍸",
            "cocktail 🍹",
            "sake 🍶",
            "whiskey 🥃",
            "Champagne 🍾",
            "sparkling wine 🥂",
            "mate 🧉",
            "boba tea 🧋",
            "pour one out 🫗",
            "tears 😭",
            "baby bottle 🍼",
        ]

        selected = random.choice(beverages)
        return self.bot.privmsg(target, selected)

    @classmethod
    def reload(cls, old: Self) -> Self:  # pragma: no cover
        return cls(old.bot)
