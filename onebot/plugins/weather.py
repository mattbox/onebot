# -*- coding: utf-8 -*-
"""
=================================================
:mod:`onebot.plugins.weather` Weather plugin
=================================================

Reports the current conditions and a short narrative forecast for a
location, using the free `wttr.in <https://wttr.in/:help>`_ API.

The location is remembered per user (via :mod:`onebot.plugins.users`, so it
follows the user's account rather than their nick) the first time they pass
one::

    <user> .w 90210
    <onebot> Parklabrea, California, USA: [Now: ☀ Clear | 86°F | ...]
    <user> .w
    <onebot> Parklabrea, California, USA: [Now: ☀ Clear | 86°F | ...]

Units default to imperial in countries that use them and metric everywhere
else; ``--metric`` or ``--imperial`` overrides that and is remembered too.

Conditions are drawn with Unicode weather symbols rather than emoji. Every
glyph is chosen from the codepoints with ``Emoji_Presentation=No``, so a
conforming client renders them as narrow monochrome text instead of wide
colour emoji, and no U+FE0F variation selector is ever emitted.

The finer-grained symbols (🌤 🌥 🌦 🌧 🌨 🌩 🌫, all Unicode 7.0) have thin
font coverage outside Linux systems carrying Noto Sans Symbols2, where they
may show as tofu. Channels with such clients can set ``symbols = safe`` to
fall back to a table that stays inside the BMP and inside DejaVu Sans, at
the cost of merging some conditions together.
"""

import asyncio
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Self, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import irc3
from irc3.plugins.command import command

WTTR_BASE = "https://wttr.in"
USER_AGENT = "onebot (+https://github.com/thomwiggers/onebot)"

#: How long a forecast is reused before wttr.in is asked again.
FORECAST_TTL = 600
#: How far ahead the narrative forecast looks.
FORECAST_HOURS = 24
#: How many locations each cache holds. Anyone in a channel can ask for a
#: location nobody has asked for before, so the caches need a ceiling or they
#: grow without bound for the lifetime of the process.
CACHE_MAX_LOCATIONS = 128

LOCATION_SETTING = "weather_location"
UNITS_SETTING = "weather_units"

BOLD = "\x02"

# mIRC colour codes
GREY = 14
BLUE = 12
CYAN = 11
GREEN = 9
YELLOW = 8
ORANGE = 7
RED = 4

#: Countries that report the weather in Fahrenheit and miles per hour.
IMPERIAL_COUNTRIES = frozenset(
    (
        "united states of america",
        "united states",
        "usa",
        "puerto rico",
        "guam",
        "american samoa",
        "united states virgin islands",
        "northern mariana islands",
        "bahamas",
        "belize",
        "cayman islands",
        "palau",
        "marshall islands",
        "micronesia",
        "liberia",
        "myanmar",
        "burma",
    )
)

#: Long country names shortened for the location label.
COUNTRY_ABBREVIATIONS = {
    "united states of america": "USA",
    "united states": "USA",
    "united kingdom": "UK",
    "united arab emirates": "UAE",
    "russian federation": "Russia",
    "republic of korea": "South Korea",
    "korea, republic of": "South Korea",
    "czech republic": "Czechia",
    "netherlands": "Netherlands",
}

#: All 60 World Weather Online condition codes mapped to a Unicode weather
#: symbol. Every glyph here has ``Emoji_Presentation=No``, so a conforming
#: renderer draws it as monochrome text rather than a wide colour emoji --
#: which is why ⛅ U+26C5, ☔ U+2614, ⚡ U+26A1 and 🌙 U+1F319 are all absent
#: despite being the obvious picks. No U+FE0F variation selector appears
#: anywhere, for the same reason.
WEATHER_SYMBOLS = {
    # -- sky cover --------------------------------------------------------
    113: "☀",  # ☀ Sunny / Clear         BLACK SUN WITH RAYS
    116: "\U0001f324",  # 🌤 Partly cloudy         WHITE SUN WITH SMALL CLOUD
    119: "\U0001f325",  # 🌥 Cloudy                WHITE SUN BEHIND CLOUD
    122: "☁",  # ☁ Overcast              CLOUD
    # -- obscuration: mist, fog, and the dust/smoke/smog family -----------
    143: "\U0001f32b",  # 🌫 Mist                  FOG
    248: "\U0001f32b",  # 🌫 Fog
    260: "\U0001f32b",  # 🌫 Freezing fog
    125: "\U0001f32b",  # 🌫 Haze
    128: "\U0001f32b",  # 🌫 Dust haze
    131: "\U0001f32b",  # 🌫 Blowing dust
    134: "\U0001f32b",  # 🌫 Dust storm
    137: "\U0001f32b",  # 🌫 Sandstorm
    140: "\U0001f32b",  # 🌫 Severe sandstorm
    146: "\U0001f32b",  # 🌫 Smoke
    149: "\U0001f32b",  # 🌫 Smoky haze
    152: "\U0001f32b",  # 🌫 Smog
    155: "\U0001f32b",  # 🌫 Severe smog
    158: "\U0001f32b",  # 🌫 Saharan dust
    161: "\U0001f32b",  # 🌫 Dust
    # -- intermittent rain: the sun-behind-cloud axis marks "patchy" ------
    176: "\U0001f326",  # 🌦 Patchy rain nearby    WHITE SUN BEHIND CLOUD WITH RAIN
    263: "\U0001f326",  # 🌦 Patchy light drizzle
    293: "\U0001f326",  # 🌦 Patchy light rain
    353: "\U0001f326",  # 🌦 Light rain shower
    # -- steady rain, ascending by intensity ------------------------------
    266: "\U0001f322",  # 🌢 Light drizzle         BLACK DROPLET
    296: "\U0001f327",  # 🌧 Light rain            CLOUD WITH RAIN
    299: "\U0001f327",  # 🌧 Moderate rain at times
    302: "\U0001f327",  # 🌧 Moderate rain
    356: "\U0001f327",  # 🌧 Moderate or heavy rain shower
    305: "⛆",  # ⛆ Heavy rain at times   RAIN
    308: "⛆",  # ⛆ Heavy rain
    359: "⛆",  # ⛆ Torrential rain shower
    # -- freezing rain and drizzle (glaze ice) ----------------------------
    185: "❆",  # ❆ Patchy freezing drizzle   HEAVY CHEVRON SNOWFLAKE
    281: "❆",  # ❆ Freezing drizzle
    284: "❆",  # ❆ Heavy freezing drizzle
    311: "❆",  # ❆ Light freezing rain
    314: "❆",  # ❆ Moderate or heavy freezing rain
    # -- ice pellets ------------------------------------------------------
    350: "⛇",  # ⛇ Ice pellets           BLACK SNOWMAN
    374: "⛇",  # ⛇ Light showers of ice pellets
    377: "⛇",  # ⛇ Moderate or heavy showers of ice pellets
    # -- sleet (rain and snow mixed) --------------------------------------
    182: "\U0001f328",  # 🌨 Patchy sleet nearby   CLOUD WITH SNOW
    317: "\U0001f328",  # 🌨 Light sleet
    320: "\U0001f328",  # 🌨 Moderate or heavy sleet
    362: "\U0001f328",  # 🌨 Light sleet showers
    365: "\U0001f328",  # 🌨 Moderate or heavy sleet showers
    # -- snow, ascending by intensity -------------------------------------
    179: "❄",  # ❄ Patchy snow nearby    SNOWFLAKE
    323: "❄",  # ❄ Patchy light snow
    326: "❄",  # ❄ Light snow
    368: "❄",  # ❄ Light snow showers
    329: "❅",  # ❅ Patchy moderate snow  TIGHT TRIFOLIATE SNOWFLAKE
    332: "❅",  # ❅ Moderate snow
    371: "❅",  # ❅ Moderate or heavy snow showers
    227: "❅",  # ❅ Blowing snow
    335: "☃",  # ☃ Patchy heavy snow     SNOWMAN
    338: "☃",  # ☃ Heavy snow
    230: "☃",  # ☃ Blizzard
    # -- thunder ----------------------------------------------------------
    200: "☈",  # ☈ Thundery outbreaks    THUNDERSTORM
    386: "\U0001f329",  # 🌩 Patchy light rain w/ thunder  CLOUD WITH LIGHTNING
    389: "⛈",  # ⛈ Mod/heavy rain w/ thunder  THUNDER CLOUD AND RAIN
    392: "☇",  # ☇ Patchy light snow w/ thunder  LIGHTNING
    395: "☇",  # ☇ Mod/heavy snow w/ thunder
}

#: Night-time replacements for the codes whose day symbol contains a sun.
#: ☾ U+263E is not in the Emoji set at all, so unlike 🌙 U+1F319 it can never
#: be promoted to a wide colour emoji.
NIGHT_SYMBOLS = {
    113: "☾",  # ☾ Clear (night)         LAST QUARTER MOON
    116: "☁",  # ☁ Partly cloudy (night) CLOUD
    119: "☁",  # ☁ Cloudy (night)        CLOUD
}

#: Used for any code not listed above. CLOUD is in essentially every font.
UNKNOWN_SYMBOL = "☁"  # ☁

#: A conservative alternative to :data:`WEATHER_SYMBOLS`, for channels whose
#: clients lack the fonts for the Unicode 7.0 additions at U+1F32x. Every
#: glyph is in the Basic Multilingual Plane and in DejaVu Sans. Selected with
#: ``symbols = safe`` in the config section.
SAFE_WEATHER_SYMBOLS = {
    code: {
        "\U0001f324": "☁",  # 🌤 -> ☁
        "\U0001f325": "☁",  # 🌥 -> ☁
        "\U0001f326": "☂",  # 🌦 -> ☂
        "\U0001f327": "☂",  # 🌧 -> ☂
        "\U0001f322": "☂",  # 🌢 -> ☂
        "⛆": "☂",  # ⛆ -> ☂
        "\U0001f328": "❅",  # 🌨 -> ❅
        "\U0001f32b": "░",  # 🌫 -> ░  LIGHT SHADE
        "\U0001f329": "☈",  # 🌩 -> ☈
        "⛈": "☈",  # ⛈ -> ☈
        "⛇": "❆",  # ⛇ -> ❆
    }.get(symbol, symbol)
    for code, symbol in WEATHER_SYMBOLS.items()
}


def _cache_get(cache: "OrderedDict[str, Any]", key: str) -> Any:
    """Read a key, marking it as the most recently used.

    >>> cache = OrderedDict([("a", 1), ("b", 2)])
    >>> _cache_get(cache, "a")
    1
    >>> list(cache)
    ['b', 'a']
    >>> _cache_get(cache, "missing") is None
    True
    """
    if key not in cache:
        return None
    cache.move_to_end(key)
    return cache[key]


def _cache_put(
    cache: "OrderedDict[str, Any]",
    key: str,
    value: Any,
    limit: int = CACHE_MAX_LOCATIONS,
) -> None:
    """Store a key, evicting least recently used entries beyond ``limit``.

    >>> cache = OrderedDict()
    >>> for name in "abc":
    ...     _cache_put(cache, name, name.upper(), limit=2)
    >>> list(cache.items())
    [('b', 'B'), ('c', 'C')]

    Reading an entry protects it from the next eviction:

    >>> _ = _cache_get(cache, "b")
    >>> _cache_put(cache, "d", "D", limit=2)
    >>> list(cache)
    ['b', 'd']
    """
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def _colour(text: str, code: int) -> str:
    r"""Wrap ``text`` in an mIRC colour code.

    >>> _colour("hot", RED)
    '\x0304hot\x03'
    """
    return "\x03{:02d}{}\x03".format(code, text)


def _temperature_colour(celsius: float) -> int:
    """Pick a colour for a temperature, given in degrees Celsius.

    >>> _temperature_colour(-5) == BLUE
    True
    >>> _temperature_colour(18) == GREEN
    True
    >>> _temperature_colour(35) == RED
    True
    """
    if celsius <= 0:
        return BLUE
    if celsius <= 10:
        return CYAN
    if celsius <= 20:
        return GREEN
    if celsius <= 27:
        return YELLOW
    if celsius <= 33:
        return ORANGE
    return RED


def _units_for_country(country: str) -> str:
    """Return the unit system customarily used in ``country``.

    >>> _units_for_country("United States of America")
    'imperial'
    >>> _units_for_country("Netherlands")
    'metric'
    """
    return "imperial" if country.strip().lower() in IMPERIAL_COUNTRIES else "metric"


def _format_location(nearest_area: Dict[str, Any]) -> str:
    """Build a human readable place name from wttr.in's ``nearest_area``.

    >>> _format_location({"areaName": [{"value": "Parklabrea"}],
    ...                   "region": [{"value": "California"}],
    ...                   "country": [{"value": "United States of America"}]})
    'Parklabrea, California, USA'
    >>> _format_location({"areaName": [{"value": "Amsterdam"}],
    ...                   "region": [{"value": "Amsterdam"}],
    ...                   "country": [{"value": "Netherlands"}]})
    'Amsterdam, Netherlands'
    """
    parts = []
    for key in ("areaName", "region", "country"):
        value = _first_value(nearest_area, key)
        if key == "country":
            value = COUNTRY_ABBREVIATIONS.get(value.lower(), value)
        # wttr.in happily reports "Amsterdam, Amsterdam, Netherlands"
        if value and value not in parts:
            parts.append(value)
    return ", ".join(parts)


def _first_value(data: Dict[str, Any], key: str) -> str:
    """Unwrap wttr.in's ``[{"value": ...}]`` style fields.

    >>> _first_value({"weatherDesc": [{"value": "Clear "}]}, "weatherDesc")
    'Clear'
    >>> _first_value({}, "weatherDesc")
    ''
    """
    values = data.get(key) or []
    if not values:
        return ""
    return str(values[0].get("value", "")).strip()


def _slot_hour(value: str) -> int:
    """Convert a wttr.in hourly ``time`` field into an hour of the day.

    >>> _slot_hour("0"), _slot_hour("300"), _slot_hour("2100")
    (0, 3, 21)
    """
    return int(value) // 100


def _symbol_for(code: int, night: bool = False, safe: bool = False) -> str:
    """Pick a Unicode weather symbol for a World Weather Online code.

    ``night`` swaps the sun out of the few symbols that contain one, and
    ``safe`` selects the BMP-only table for clients with thin font coverage.

    >>> _symbol_for(113), _symbol_for(113, night=True)
    ('☀', '☾')
    >>> _symbol_for(116), _symbol_for(116, night=True)
    ('🌤', '☁')
    >>> _symbol_for(302), _symbol_for(302, safe=True)
    ('🌧', '☂')
    >>> _symbol_for(999)
    '☁'

    The night table only covers codes whose day symbol shows a sun, so
    everything else is unaffected by it:

    >>> _symbol_for(338, night=True)
    '☃'
    """
    if night and code in NIGHT_SYMBOLS:
        return NIGHT_SYMBOLS[code]
    table = SAFE_WEATHER_SYMBOLS if safe else WEATHER_SYMBOLS
    return table.get(code, UNKNOWN_SYMBOL)


def _parse_clock(value: str, reference: datetime) -> Optional[datetime]:
    """Parse a ``"06:59 AM"`` style time onto ``reference``'s date.

    >>> ref = datetime(2026, 8, 8, 12, 0)
    >>> _parse_clock("06:59 AM", ref)
    datetime.datetime(2026, 8, 8, 6, 59)
    >>> _parse_clock("No sunrise", ref) is None
    True
    """
    try:
        parsed = datetime.strptime(value.strip(), "%I:%M %p")
    except ValueError:
        return None
    return reference.replace(
        hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0
    )


def _is_night(now: datetime, astronomy: Dict[str, Any]) -> bool:
    """Is it dark out, according to the day's sunrise and sunset?

    Falls back to a fixed 06:00--20:00 day when the times cannot be parsed
    (wttr.in returns "No sunrise" inside the polar circles).

    >>> astro = {"sunrise": "06:59 AM", "sunset": "08:21 PM"}
    >>> _is_night(datetime(2026, 8, 8, 22, 0), astro)
    True
    >>> _is_night(datetime(2026, 8, 8, 12, 0), astro)
    False
    >>> _is_night(datetime(2026, 8, 8, 3, 0), {"sunrise": "No sunrise"})
    True

    Near the midnight sun the sun sets after midnight, which belongs to the
    following day rather than to this morning:

    >>> midnight_sun = {"sunrise": "02:55 AM", "sunset": "12:04 AM"}
    >>> _is_night(datetime(2026, 6, 21, 14, 0), midnight_sun)
    False
    >>> _is_night(datetime(2026, 6, 21, 1, 0), midnight_sun)
    True
    """
    sunrise = _parse_clock(astronomy.get("sunrise", ""), now)
    sunset = _parse_clock(astronomy.get("sunset", ""), now)
    if sunrise is None or sunset is None:
        return not 6 <= now.hour < 20
    if sunset <= sunrise:
        sunset += timedelta(days=1)
    return now < sunrise or now >= sunset


def _daypart(hour: int) -> str:
    """Name the part of the day an hour falls in.

    >>> _daypart(3), _daypart(9), _daypart(14), _daypart(19), _daypart(22)
    ('early morning', 'morning', 'afternoon', 'evening', 'night')
    """
    if hour < 6:
        return "early morning"
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    if hour < 21:
        return "evening"
    return "night"


def _clock(moment: datetime) -> str:
    """Render an hour as a short 12-hour clock reading.

    >>> _clock(datetime(2026, 8, 8, 21, 0)), _clock(datetime(2026, 8, 8, 9, 0))
    ('9pm', '9am')
    >>> _clock(datetime(2026, 8, 8, 0, 0)), _clock(datetime(2026, 8, 8, 12, 0))
    ('midnight', 'noon')
    """
    if moment.hour == 0:
        return "midnight"
    if moment.hour == 12:
        return "noon"
    return "{}{}".format(moment.hour % 12 or 12, "am" if moment.hour < 12 else "pm")


def _friendly_time(moment: datetime, now: datetime, precise: bool = False) -> str:
    """Describe ``moment`` relative to ``now``.

    Times later today get a clock reading; anything further out is named by
    the part of the day, which reads better than "21:00 on Monday". Pass
    ``precise`` to keep the day but use the clock, which is needed when two
    forecast changes would otherwise land on the same label.

    >>> now = datetime(2026, 8, 8, 14, 0)
    >>> _friendly_time(datetime(2026, 8, 8, 21, 0), now)
    '9pm'
    >>> _friendly_time(datetime(2026, 8, 9, 9, 0), now)
    'tomorrow morning'
    >>> _friendly_time(datetime(2026, 8, 9, 9, 0), now, precise=True)
    'tomorrow 9am'
    >>> _friendly_time(datetime(2026, 8, 10, 15, 0), now)
    'Monday afternoon'
    """
    days = (moment.date() - now.date()).days
    when = _clock(moment) if precise else _daypart(moment.hour)
    if days <= 0:
        return _clock(moment)
    if days == 1:
        return "tomorrow {}".format(when)
    return "{} {}".format(moment.strftime("%A"), when)


def _group_conditions(
    slots: List[Tuple[datetime, str]],
) -> List[Tuple[datetime, str]]:
    """Collapse consecutive forecast slots sharing a condition.

    >>> slots = [(datetime(2026, 8, 8, 15), "Sunny"),
    ...          (datetime(2026, 8, 8, 18), "Sunny"),
    ...          (datetime(2026, 8, 8, 21), "Clear")]
    >>> [(m.hour, d) for m, d in _group_conditions(slots)]
    [(15, 'Sunny'), (21, 'Clear')]
    """
    groups: List[Tuple[datetime, str]] = []
    for moment, desc in slots:
        if not groups or groups[-1][1] != desc:
            groups.append((moment, desc))
    return groups


def _narrative(slots: List[Tuple[datetime, str]], now: datetime) -> str:
    """Describe how conditions change over the coming slots.

    >>> now = datetime(2026, 8, 8, 14, 0)
    >>> _narrative([(datetime(2026, 8, 8, 15), "Sunny"),
    ...             (datetime(2026, 8, 8, 18), "Sunny")], now)
    'Sunny throughout.'
    >>> _narrative([(datetime(2026, 8, 8, 15), "Sunny"),
    ...             (datetime(2026, 8, 8, 21), "Clear")], now)
    'Sunny until 9pm, then Clear.'
    >>> _narrative([(datetime(2026, 8, 8, 15), "Sunny"),
    ...             (datetime(2026, 8, 8, 21), "Light rain"),
    ...             (datetime(2026, 8, 9, 9), "Sunny")], now)
    'Sunny until 9pm, then Light rain until tomorrow morning, then Sunny again.'

    Two changes within one daypart fall back to the clock, so that the same
    label never appears twice:

    >>> _narrative([(datetime(2026, 8, 8, 15), "Clear"),
    ...             (datetime(2026, 8, 9, 6), "Partly Cloudy"),
    ...             (datetime(2026, 8, 9, 9), "Cloudy")], now)
    'Clear until tomorrow 6am, then Partly Cloudy until tomorrow 9am, then Cloudy.'
    """
    groups = _group_conditions(slots)
    if not groups:
        return ""
    if len(groups) == 1:
        return "{} throughout.".format(groups[0][1])

    first = groups[0][1]
    changes = groups[1:3]
    labels = [_friendly_time(moment, now) for moment, _ in changes]
    if len(set(labels)) != len(labels):
        labels = [_friendly_time(moment, now, precise=True) for moment, _ in changes]

    pieces = [first]
    for index, ((_, desc), label) in enumerate(zip(changes, labels), start=1):
        # "again" reads better than naming the same condition twice
        suffix = " again" if index == 2 and desc == first else ""
        pieces.append("until {}, then {}{}".format(label, desc, suffix))
    return " ".join(pieces) + "."


def _forecast_slots(
    days: List[Dict[str, Any]], now: datetime, hours: int = FORECAST_HOURS
) -> List[Tuple[datetime, str]]:
    """Flatten wttr.in's per-day hourly forecasts into upcoming slots.

    Only slots strictly after ``now`` and within ``hours`` are kept, so the
    narrative never talks about weather that has already happened.
    """
    horizon = now + timedelta(hours=hours)
    slots: List[Tuple[datetime, str]] = []
    for day in days:
        try:
            date = datetime.strptime(day["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        for hourly in day.get("hourly", []):
            try:
                moment = datetime.combine(
                    date, datetime.min.time(), tzinfo=now.tzinfo
                ).replace(hour=_slot_hour(hourly["time"]))
            except (KeyError, ValueError):
                continue
            if now < moment <= horizon:
                slots.append((moment, _first_value(hourly, "weatherDesc")))
    return slots


def _nearest_hourly(day: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
    """Find the hourly entry closest to ``now``.

    ``current_condition`` has no wind gust, so it is borrowed from here.
    Entries whose ``time`` cannot be placed on the clock are skipped, as in
    :func:`_forecast_slots`.
    """
    placed = []
    for hourly in day.get("hourly") or []:
        try:
            distance = abs(_slot_hour(hourly.get("time", "0")) - now.hour)
        except (TypeError, ValueError):
            continue
        placed.append((distance, hourly))
    if not placed:
        return None
    # Keyed, so that two equidistant slots never compare their dicts
    return min(placed, key=lambda entry: entry[0])[1]


@irc3.plugin
class WeatherPlugin(object):
    """Plugin to provide:

    * Weather lookups via wttr.in

    Configuration settings:
        - ``ignored_channels``: Channels to stay quiet in
        - ``symbols``: ``full`` (default) or ``safe`` for BMP-only glyphs
    """

    requires = ["irc3.plugins.command", "onebot.plugins.users"]

    def __init__(self, bot):
        """Initialise the plugin"""
        self.bot = bot
        self.log = bot.log.getChild(__name__)
        self.config = bot.config.get(__name__, {})
        self.ignored_channels = self.config.get("ignored_channels", [])
        # "safe" falls back to BMP-only glyphs for clients with thin fonts
        self.safe_symbols = self.config.get("symbols", "full") == "safe"
        # location -> (fetched_at, payload); wttr.in asks callers to be gentle.
        # Bounded, because the key is whatever a user typed.
        self._forecasts: "OrderedDict[str, Tuple[float, Dict[str, Any]]]" = (
            OrderedDict()
        )
        # location -> IANA timezone name; a location's zone never changes, so
        # these are only dropped to make room, never because they went stale
        self._timezones: "OrderedDict[str, str]" = OrderedDict()

    @command
    async def w(self, mask, target, args):
        """Weather - current conditions and how they change from here.

        Remembers the location you last looked up.

        %%w [--metric | --imperial] [<location>...]
        """
        if mask.nick == self.bot.nick or target in self.ignored_channels:
            return

        location = " ".join(args["<location>"]).strip()
        units = None
        if args.get("--metric"):
            units = "metric"
        elif args.get("--imperial"):
            units = "imperial"

        user = self.bot.get_user(mask.nick)
        if location:
            stored_location = None
        else:
            stored_location = await self._get_setting(user, LOCATION_SETTING)
            # Settings round-trip through JSON, so "90210" comes back as an int
            location = "" if stored_location is None else str(stored_location)
            if not location:
                return (
                    "{}: I don't know where you are. Try: {}w <city or zipcode>".format(
                        mask.nick, self.bot.config.cmd
                    )
                )

        if units is None:
            units = await self._get_setting(user, UNITS_SETTING)

        try:
            data, timezone = await self._fetch(location)
        except LookupError:
            return "{}: Sorry, I couldn't find “{}”.".format(mask.nick, location)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            self.log.warning("wttr.in request for %r failed: %s", location, e)
            return "{}: Sorry, wttr.in isn't answering right now.".format(mask.nick)

        # Only remember a location that actually resolved
        if stored_location is None:
            await self._set_setting(user, LOCATION_SETTING, location)
        if args.get("--metric") or args.get("--imperial"):
            await self._set_setting(user, UNITS_SETTING, units)

        try:
            return self.format_weather(data, timezone, units)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            # A 200 is no promise of a j1 payload: wttr.in also answers with
            # an error document and, when rate limiting, with HTML
            self.log.warning("Unreadable wttr.in payload for %r: %s", location, e)
            return "{}: Sorry, wttr.in sent something I couldn't read.".format(
                mask.nick
            )

    def format_weather(
        self, data: Dict[str, Any], timezone: Optional[str], units: Optional[str]
    ) -> str:
        """Render a wttr.in ``j1`` payload as a single IRC line."""
        current = data["current_condition"][0]
        area = data["nearest_area"][0]
        today = data["weather"][0]

        place = _format_location(area)
        if units not in ("metric", "imperial"):
            units = _units_for_country(_first_value(area, "country"))
        imperial = units == "imperial"

        now = self._local_now(timezone)
        night = _is_night(now, (today.get("astronomy") or [{}])[0])

        temp = int(current["temp_F" if imperial else "temp_C"])
        feels = int(current["FeelsLikeF" if imperial else "FeelsLikeC"])
        celsius = int(current["temp_C"])
        degree = "°F" if imperial else "°C"

        temp_str = _colour("{}{}".format(temp, degree), _temperature_colour(celsius))
        # A feels-like within a couple of degrees is noise, not information
        if abs(feels - temp) >= (3 if imperial else 2):
            temp_str += " (feels {}{})".format(feels, degree)

        symbol = _symbol_for(
            int(current.get("weatherCode", 0)),
            night=night,
            safe=self.safe_symbols,
        )
        condition = _first_value(current, "weatherDesc")

        now_parts = [
            "{} {}".format(symbol, condition),
            temp_str,
            "{}% RH".format(current["humidity"]),
            self._format_wind(current, today, now, imperial),
        ]

        upcoming_parts = []
        narrative = _narrative(_forecast_slots(data["weather"], now), now)
        if narrative:
            upcoming_parts.append(narrative)
        upcoming_parts.append(self._format_range(today, imperial))

        return "{bold}{place}{bold}: [{now_label} {now}] [{next_label} {next}]".format(
            bold=BOLD,
            place=place,
            now_label=_colour("Now:", GREY),
            now=" | ".join(now_parts),
            next_label=_colour("Upcoming:", GREY),
            next=" | ".join(upcoming_parts),
        )

    def _format_wind(
        self,
        current: Dict[str, Any],
        today: Dict[str, Any],
        now: datetime,
        imperial: bool,
    ) -> str:
        """Format wind speed, gust and direction."""
        unit = "MPH" if imperial else "KMPH"
        speed = int(current["windspeedMiles" if imperial else "windspeedKmph"])
        direction = current.get("winddir16Point", "")

        gust = None
        hourly = _nearest_hourly(today, now)
        if hourly:
            try:
                gust = int(hourly["WindGustMiles" if imperial else "WindGustKmph"])
            except (KeyError, ValueError):
                gust = None

        if gust and gust > speed:
            return "W: {} -> {} {} {}".format(speed, gust, unit, direction).strip()
        return "W: {} {} {}".format(speed, unit, direction).strip()

    def _format_range(self, today: Dict[str, Any], imperial: bool) -> str:
        """Format today's high and low, each coloured by how warm it is."""
        high = int(today["maxtempF" if imperial else "maxtempC"])
        low = int(today["mintempF" if imperial else "mintempC"])
        return "{}-{}".format(
            _colour("{}°".format(high), _temperature_colour(int(today["maxtempC"]))),
            _colour("{}°".format(low), _temperature_colour(int(today["mintempC"]))),
        )

    @staticmethod
    def _local_now(timezone: Optional[str]) -> datetime:
        """Current time at the location, falling back to UTC."""
        if timezone:
            try:
                return datetime.now(ZoneInfo(timezone))
            except (ZoneInfoNotFoundError, ValueError):
                pass
        return datetime.now(ZoneInfo("UTC"))

    async def _fetch(self, location: str) -> Tuple[Dict[str, Any], Optional[str]]:
        """Get the forecast and the location's timezone.

        Raises :exc:`LookupError` when wttr.in cannot resolve the location.
        """
        cached = _cache_get(self._forecasts, location)
        if cached is not None:
            fetched_at, payload = cached
            if (time.monotonic() - fetched_at) < FORECAST_TTL:
                return payload, _cache_get(self._timezones, location)
            # Release the stale payload now rather than holding it until some
            # later lookup happens to evict it
            del self._forecasts[location]

        async with aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as session:
            timezone = _cache_get(self._timezones, location)
            if timezone is not None:
                data = await self._fetch_forecast(session, location)
            else:
                # Same latency as one request, and the timezone is then cached
                data, timezone = await asyncio.gather(
                    self._fetch_forecast(session, location),
                    self._fetch_timezone(session, location),
                )
                if timezone:
                    _cache_put(self._timezones, location, timezone)

        _cache_put(self._forecasts, location, (time.monotonic(), data))
        return data, timezone

    async def _fetch_forecast(self, session, location: str) -> Dict[str, Any]:
        """Fetch the ``j1`` JSON forecast for a location."""
        async with session.get(
            "{}/{}".format(WTTR_BASE, location), params={"format": "j1"}
        ) as resp:
            if resp.status != 200:
                raise LookupError(location)
            # wttr.in serves the j1 payload as text/plain
            return await resp.json(content_type=None)

    async def _fetch_timezone(self, session, location: str) -> Optional[str]:
        """Fetch the IANA timezone name, which the ``j1`` payload omits."""
        try:
            async with session.get(
                "{}/{}".format(WTTR_BASE, location), params={"format": "%Z"}
            ) as resp:
                if resp.status != 200:
                    return None
                return (await resp.text()).strip() or None
        except Exception as e:
            # The timezone only sharpens the forecast wording, so nothing here
            # should be able to fail the lookup. UTC is the fallback.
            self.log.debug("No timezone for %r: %s", location, e)
            return None

    async def _get_setting(self, user, setting: str) -> Optional[str]:
        if user is None:
            return None
        return await user.get_setting(setting)

    async def _set_setting(self, user, setting: str, value: Any) -> None:
        if user is None:
            return
        self.log.info("Storing %s=%r", setting, value)
        await user.set_setting(setting, value)

    @classmethod
    def reload(cls, old: Self) -> Self:  # pragma: no cover
        return cls(old.bot)
