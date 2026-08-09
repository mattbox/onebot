#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
test_plugin_weather
----------------------------------

Tests for the weather module
"""

import asyncio
import json
import logging
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from freezegun import freeze_time
from irc3.testing import MagicMock

from onebot.testing import BotTestCase
from onebot.plugins.users import deserialize_setting
from onebot.plugins.weather import (
    CACHE_MAX_LOCATIONS,
    WeatherPlugin,
    _symbol_for,
    _format_location,
    _forecast_slots,
    _friendly_time,
    _group_conditions,
    _is_night,
    _narrative,
    _nearest_hourly,
    _temperature_colour,
    _units_for_country,
    BLUE,
    GREEN,
    NIGHT_SYMBOLS,
    RED,
    SAFE_WEATHER_SYMBOLS,
    WEATHER_SYMBOLS,
    YELLOW,
)

from .aiohttp_stubs import FakeResponse, fake_session

BEVERLY_HILLS = {
    "areaName": [{"value": "Parklabrea"}],
    "region": [{"value": "California"}],
    "country": [{"value": "United States of America"}],
}
AMSTERDAM = {
    "areaName": [{"value": "De Wallen"}],
    "region": [{"value": "North Holland"}],
    "country": [{"value": "Netherlands"}],
}


def _desc(value):
    return [{"value": value}]


def _hourly(time, desc, code=113, gust_miles=16, gust_kmph=26):
    return {
        "time": time,
        "weatherDesc": _desc(desc),
        "weatherCode": str(code),
        "WindGustMiles": str(gust_miles),
        "WindGustKmph": str(gust_kmph),
    }


def _payload(
    area=None,
    temp_f=86,
    temp_c=30,
    feels_f=86,
    feels_c=30,
    humidity=57,
    code=113,
    desc="Clear",
    wind_miles=11,
    wind_kmph=18,
    direction="ESE",
    hourly=None,
    days=None,
):
    """Build a wttr.in ``j1`` style payload."""
    if hourly is None:
        hourly = [
            _hourly("1200", "Sunny", 113),
            _hourly("1500", "Sunny", 113),
            _hourly("1800", "Sunny", 113),
            _hourly("2100", "Clear", 113),
        ]
    weather = [
        {
            "date": "2026-08-08",
            "maxtempF": "95",
            "maxtempC": "35",
            "mintempF": "77",
            "mintempC": "25",
            "astronomy": [{"sunrise": "06:59 AM", "sunset": "08:21 PM"}],
            "hourly": hourly,
        }
    ]
    if days:
        weather.extend(days)
    return {
        "current_condition": [
            {
                "temp_F": str(temp_f),
                "temp_C": str(temp_c),
                "FeelsLikeF": str(feels_f),
                "FeelsLikeC": str(feels_c),
                "humidity": str(humidity),
                "weatherCode": str(code),
                "weatherDesc": _desc(desc),
                "windspeedMiles": str(wind_miles),
                "windspeedKmph": str(wind_kmph),
                "winddir16Point": direction,
            }
        ],
        "nearest_area": [area or BEVERLY_HILLS],
        "weather": weather,
    }


class StubBot(object):
    """Just enough bot to construct the plugin outside of irc3."""

    def __init__(self, ignored_channels=None, symbols=None):
        section = {}
        if ignored_channels is not None:
            section["ignored_channels"] = ignored_channels
        if symbols is not None:
            section["symbols"] = symbols
        self.config = {"onebot.plugins.weather": section}
        self.log = logging.getLogger("test.weather")


class UnitsForCountryTest(unittest.TestCase):
    def test_united_states_is_imperial(self):
        assert _units_for_country("United States of America") == "imperial"
        assert _units_for_country("  usa  ") == "imperial"

    def test_other_imperial_holdouts(self):
        assert _units_for_country("Liberia") == "imperial"
        assert _units_for_country("Myanmar") == "imperial"

    def test_everywhere_else_is_metric(self):
        assert _units_for_country("Netherlands") == "metric"
        assert _units_for_country("Japan") == "metric"

    def test_unknown_country_defaults_to_metric(self):
        assert _units_for_country("") == "metric"


class TemperatureColourTest(unittest.TestCase):
    def test_scale_is_monotonic_by_band(self):
        assert _temperature_colour(-20) == BLUE
        assert _temperature_colour(0) == BLUE
        assert _temperature_colour(15) == GREEN
        assert _temperature_colour(25) == YELLOW
        assert _temperature_colour(40) == RED

    def test_boundaries_belong_to_the_lower_band(self):
        assert _temperature_colour(20) == GREEN
        assert _temperature_colour(20.1) != GREEN


class FormatLocationTest(unittest.TestCase):
    def test_country_is_abbreviated(self):
        assert _format_location(BEVERLY_HILLS) == "Parklabrea, California, USA"

    def test_repeated_parts_are_dropped(self):
        area = {
            "areaName": _desc("Amsterdam"),
            "region": _desc("Amsterdam"),
            "country": _desc("Netherlands"),
        }
        assert _format_location(area) == "Amsterdam, Netherlands"

    def test_missing_parts_are_skipped(self):
        area = {"areaName": _desc("Nowhere"), "region": [], "country": []}
        assert _format_location(area) == "Nowhere"


class SymbolTest(unittest.TestCase):
    def test_sunny_codes_differ_by_day_and_night(self):
        assert _symbol_for(113) == "☀"
        assert _symbol_for(113, night=True) == "☾"
        assert _symbol_for(116) == "\U0001f324"
        assert _symbol_for(116, night=True) == "☁"

    def test_night_only_affects_codes_whose_symbol_has_a_sun(self):
        for code in (302, 338, 200, 122):
            assert _symbol_for(code, night=True) == _symbol_for(code)

    def test_unknown_code_falls_back(self):
        assert _symbol_for(4242) == "☁"
        assert _symbol_for(4242, safe=True) == "☁"

    def test_safe_table_avoids_the_astral_plane(self):
        for code in SAFE_WEATHER_SYMBOLS:
            symbol = _symbol_for(code, safe=True)
            assert all(ord(c) <= 0xFFFF for c in symbol), (code, symbol)

    def test_safe_table_covers_every_code_the_full_table_does(self):
        assert set(SAFE_WEATHER_SYMBOLS) == set(WEATHER_SYMBOLS)

    def test_every_wwo_code_is_mapped(self):
        # The full list from worldweatheronline.com/feed/wwoConditionCodes.xml
        codes = {
            int(code)
            for code in (
                "113 116 119 122 125 128 131 134 137 140 143 146 149 152 155 "
                "158 161 176 179 182 185 200 227 230 248 260 263 266 281 284 "
                "293 296 299 302 305 308 311 314 317 320 323 326 329 332 335 "
                "338 350 353 356 359 362 365 368 371 374 377 386 389 392 395"
            ).split()
        }
        assert len(codes) == 60
        assert codes == set(WEATHER_SYMBOLS)

    def test_no_symbol_uses_emoji_presentation(self):
        """A variation selector would widen the line unpredictably."""
        for table in (WEATHER_SYMBOLS, SAFE_WEATHER_SYMBOLS, NIGHT_SYMBOLS):
            for code, symbol in table.items():
                assert "️" not in symbol, (code, symbol)
                assert "︎" not in symbol, (code, symbol)

    def test_symbols_are_single_glyphs(self):
        for table in (WEATHER_SYMBOLS, SAFE_WEATHER_SYMBOLS, NIGHT_SYMBOLS):
            for code, symbol in table.items():
                assert len(symbol) == 1, (code, symbol)

    def test_intensity_ladders_are_distinct(self):
        # light -> moderate -> heavy must not collapse to one glyph
        assert len({_symbol_for(c) for c in (326, 332, 338)}) == 3
        assert len({_symbol_for(c) for c in (296, 308)}) == 2


class IsNightTest(unittest.TestCase):
    astronomy = {"sunrise": "06:59 AM", "sunset": "08:21 PM"}

    def test_before_sunrise_and_after_sunset(self):
        assert _is_night(datetime(2026, 8, 8, 5, 0), self.astronomy)
        assert _is_night(datetime(2026, 8, 8, 21, 0), self.astronomy)

    def test_daytime(self):
        assert not _is_night(datetime(2026, 8, 8, 7, 0), self.astronomy)
        assert not _is_night(datetime(2026, 8, 8, 20, 0), self.astronomy)

    def test_polar_locations_fall_back_to_fixed_hours(self):
        astro = {"sunrise": "No sunrise", "sunset": "No sunset"}
        assert _is_night(datetime(2026, 8, 8, 2, 0), astro)
        assert not _is_night(datetime(2026, 8, 8, 12, 0), astro)

    def test_a_sunset_after_midnight_belongs_to_the_next_day(self):
        """Reykjavik in June: otherwise it would be night around the clock."""
        astro = {"sunrise": "02:55 AM", "sunset": "12:04 AM"}
        assert not _is_night(datetime(2026, 6, 21, 8, 0), astro)
        assert not _is_night(datetime(2026, 6, 21, 14, 0), astro)
        assert not _is_night(datetime(2026, 6, 21, 23, 0), astro)
        assert _is_night(datetime(2026, 6, 21, 1, 0), astro)
        assert _is_night(datetime(2026, 6, 21, 2, 0), astro)


class NearestHourlyTest(unittest.TestCase):
    now = datetime(2026, 8, 8, 14, 0)

    def test_the_closest_slot_wins(self):
        day = {"hourly": [_hourly("300", "Clear"), _hourly("1500", "Sunny")]}
        assert _nearest_hourly(day, self.now)["time"] == "1500"

    def test_unparseable_times_are_skipped(self):
        day = {"hourly": [{"time": ""}, {"time": "oops"}, _hourly("1500", "Sunny")]}
        assert _nearest_hourly(day, self.now)["time"] == "1500"

    def test_nothing_placeable_yields_nothing(self):
        assert _nearest_hourly({"hourly": [{"time": ""}]}, self.now) is None

    def test_no_hourly_data_yields_nothing(self):
        assert _nearest_hourly({}, self.now) is None

    def test_equidistant_slots_do_not_compare_their_payloads(self):
        day = {"hourly": [_hourly("1300", "Sunny"), _hourly("1500", "Clear")]}
        assert _nearest_hourly(day, self.now) is day["hourly"][0]


class FriendlyTimeTest(unittest.TestCase):
    now = datetime(2026, 8, 8, 14, 0)

    def test_today_uses_the_clock(self):
        assert _friendly_time(datetime(2026, 8, 8, 21, 0), self.now) == "9pm"
        assert _friendly_time(datetime(2026, 8, 8, 9, 0), self.now) == "9am"

    def test_precise_keeps_the_day_but_uses_the_clock(self):
        assert _friendly_time(datetime(2026, 8, 9, 6, 0), self.now, True) == (
            "tomorrow 6am"
        )
        assert _friendly_time(datetime(2026, 8, 10, 18, 0), self.now, True) == (
            "Monday 6pm"
        )

    def test_noon_and_midnight_are_named(self):
        assert _friendly_time(datetime(2026, 8, 8, 12, 0), self.now) == "noon"
        assert _friendly_time(datetime(2026, 8, 8, 0, 0), self.now) == "midnight"

    def test_tomorrow_uses_dayparts(self):
        assert _friendly_time(datetime(2026, 8, 9, 3, 0), self.now) == (
            "tomorrow early morning"
        )
        assert _friendly_time(datetime(2026, 8, 9, 15, 0), self.now) == (
            "tomorrow afternoon"
        )

    def test_further_out_names_the_weekday(self):
        assert _friendly_time(datetime(2026, 8, 10, 18, 0), self.now) == (
            "Monday evening"
        )


class GroupConditionsTest(unittest.TestCase):
    def test_consecutive_duplicates_collapse(self):
        slots = [
            (datetime(2026, 8, 8, 15), "Sunny"),
            (datetime(2026, 8, 8, 18), "Sunny"),
            (datetime(2026, 8, 8, 21), "Clear"),
            (datetime(2026, 8, 9, 0), "Clear"),
        ]
        assert [d for _, d in _group_conditions(slots)] == ["Sunny", "Clear"]

    def test_repeats_that_are_not_adjacent_are_kept(self):
        slots = [
            (datetime(2026, 8, 8, 15), "Sunny"),
            (datetime(2026, 8, 8, 18), "Light rain"),
            (datetime(2026, 8, 8, 21), "Sunny"),
        ]
        assert len(_group_conditions(slots)) == 3

    def test_empty(self):
        assert _group_conditions([]) == []


class NarrativeTest(unittest.TestCase):
    now = datetime(2026, 8, 8, 14, 0)

    def test_no_slots(self):
        assert _narrative([], self.now) == ""

    def test_single_condition(self):
        slots = [
            (datetime(2026, 8, 8, 15), "Sunny"),
            (datetime(2026, 8, 8, 18), "Sunny"),
        ]
        assert _narrative(slots, self.now) == "Sunny throughout."

    def test_one_change(self):
        slots = [
            (datetime(2026, 8, 8, 15), "Sunny"),
            (datetime(2026, 8, 8, 21), "Clear"),
        ]
        assert _narrative(slots, self.now) == "Sunny until 9pm, then Clear."

    def test_returning_condition_says_again(self):
        slots = [
            (datetime(2026, 8, 8, 15), "Sunny"),
            (datetime(2026, 8, 8, 21), "Light rain"),
            (datetime(2026, 8, 9, 9), "Sunny"),
        ]
        assert _narrative(slots, self.now) == (
            "Sunny until 9pm, then Light rain until tomorrow morning, then Sunny again."
        )

    def test_third_distinct_condition(self):
        slots = [
            (datetime(2026, 8, 8, 15), "Sunny"),
            (datetime(2026, 8, 8, 21), "Light rain"),
            (datetime(2026, 8, 9, 9), "Overcast"),
        ]
        assert _narrative(slots, self.now) == (
            "Sunny until 9pm, then Light rain until tomorrow morning, then Overcast."
        )

    def test_changes_in_one_daypart_fall_back_to_the_clock(self):
        slots = [
            (datetime(2026, 8, 8, 15), "Clear"),
            (datetime(2026, 8, 9, 6), "Partly Cloudy"),
            (datetime(2026, 8, 9, 9), "Cloudy"),
        ]
        assert _narrative(slots, self.now) == (
            "Clear until tomorrow 6am, then Partly Cloudy until tomorrow 9am, "
            "then Cloudy."
        )

    def test_distinct_dayparts_keep_the_friendly_label(self):
        slots = [
            (datetime(2026, 8, 8, 15), "Clear"),
            (datetime(2026, 8, 9, 9), "Partly Cloudy"),
            (datetime(2026, 8, 9, 15), "Cloudy"),
        ]
        assert _narrative(slots, self.now) == (
            "Clear until tomorrow morning, then Partly Cloudy until "
            "tomorrow afternoon, then Cloudy."
        )

    def test_at_most_three_groups_are_described(self):
        slots = [
            (datetime(2026, 8, 8, 15), "Sunny"),
            (datetime(2026, 8, 8, 18), "Light rain"),
            (datetime(2026, 8, 8, 21), "Overcast"),
            (datetime(2026, 8, 9, 0), "Blizzard"),
        ]
        assert "Blizzard" not in _narrative(slots, self.now)


class ForecastSlotsTest(unittest.TestCase):
    days = [
        {
            "date": "2026-08-08",
            "hourly": [_hourly(t, "Sunny") for t in ("0", "1200", "1800", "2100")],
        },
        {
            "date": "2026-08-09",
            "hourly": [_hourly(t, "Cloudy") for t in ("0", "1200", "2100")],
        },
    ]

    def test_past_slots_are_dropped(self):
        now = datetime(2026, 8, 8, 14, 0)
        slots = [(m.day, m.hour) for m, _ in _forecast_slots(self.days, now)]
        # today's 00:00 and 12:00 have been and gone; tomorrow's have not
        assert slots == [(8, 18), (8, 21), (9, 0), (9, 12)]

    def test_horizon_is_respected(self):
        now = datetime(2026, 8, 8, 14, 0)
        slots = _forecast_slots(self.days, now, hours=6)
        assert [m.hour for m, _ in slots] == [18]

    def test_slots_cross_into_the_next_day(self):
        now = datetime(2026, 8, 8, 20, 0)
        slots = _forecast_slots(self.days, now)
        assert [(m.day, m.hour) for m, _ in slots] == [(8, 21), (9, 0), (9, 12)]

    def test_malformed_entries_are_skipped(self):
        days = [
            {"date": "not-a-date", "hourly": [_hourly("1200", "Sunny")]},
            {"date": "2026-08-08", "hourly": [{"weatherDesc": _desc("Sunny")}]},
        ]
        assert _forecast_slots(days, datetime(2026, 8, 8, 0, 0)) == []

    def test_slots_inherit_the_timezone(self):
        now = datetime(2026, 8, 8, 14, 0, tzinfo=ZoneInfo("UTC"))
        slots = _forecast_slots(self.days, now)
        assert all(m.tzinfo is now.tzinfo for m, _ in slots)


@freeze_time("2026-08-08 19:00:00")  # 14:00 in America/Chicago
class FormatWeatherTest(unittest.TestCase):
    def setUp(self):
        self.plugin = WeatherPlugin(StubBot())

    def format(self, payload=None, timezone="America/Chicago", units=None):
        return self.plugin.format_weather(payload or _payload(), timezone, units)

    def test_full_line(self):
        assert self.format() == (
            "\x02Parklabrea, California, USA\x02: "
            "[\x0314Now:\x03 ☀ Clear | \x030786°F\x03 | 57% RH | "
            "W: 11 -> 16 MPH ESE] "
            "[\x0314Upcoming:\x03 Sunny until 9pm, then Clear. | "
            "\x030495°\x03-\x030877°\x03]"
        )

    def test_us_locations_default_to_imperial(self):
        line = self.format()
        assert "86°F" in line and "MPH" in line

    def test_non_us_locations_default_to_metric(self):
        line = self.format(_payload(area=AMSTERDAM))
        assert "30°C" in line
        assert "KMPH" in line
        assert "°F" not in line

    def test_explicit_units_override_the_country(self):
        line = self.format(_payload(area=AMSTERDAM), units="imperial")
        assert "86°F" in line and "MPH" in line

        line = self.format(units="metric")
        assert "30°C" in line and "KMPH" in line

    def test_feels_like_is_shown_when_it_differs(self):
        line = self.format(_payload(temp_f=86, feels_f=95))
        assert "(feels 95°F)" in line

    def test_feels_like_is_hidden_when_close(self):
        line = self.format(_payload(temp_f=86, feels_f=88))
        assert "feels" not in line

    def test_metric_feels_like_uses_a_smaller_threshold(self):
        line = self.format(_payload(area=AMSTERDAM, temp_c=30, feels_c=32))
        assert "(feels 32°C)" in line

    def test_gust_is_omitted_when_not_gusting(self):
        payload = _payload(wind_miles=20)
        assert "W: 20 MPH ESE" in self.format(payload)

    def test_wind_without_a_direction(self):
        assert "W: 11 -> 16 MPH]" in self.format(_payload(direction=""))

    def test_missing_gust_data_is_tolerated(self):
        hourly = [{"time": "1500", "weatherDesc": _desc("Sunny")}]
        assert "W: 11 MPH ESE" in self.format(_payload(hourly=hourly))

    def test_an_unparseable_hourly_time_is_tolerated(self):
        hourly = [{"time": "", "weatherDesc": _desc("Sunny")}]
        assert "W: 11 MPH ESE" in self.format(_payload(hourly=hourly))

    def test_night_uses_the_moon(self):
        with freeze_time("2026-08-09 04:00:00"):  # 23:00 in Chicago
            assert "☾" in self.format()

    def test_high_and_low_are_coloured_independently(self):
        line = self.format()
        assert "\x030495°\x03-\x030877°\x03" in line

    def test_temperature_colour_tracks_celsius_not_the_display_unit(self):
        # 5°C shown as 41°F should still be cold-coloured
        payload = _payload(temp_f=41, temp_c=5, feels_f=41, feels_c=5)
        assert "\x031141°F\x03" in self.format(payload)

    def test_narrative_is_omitted_when_there_is_no_forecast_left(self):
        payload = _payload(hourly=[_hourly("0", "Sunny")])
        line = self.format(payload)
        assert "Upcoming:\x03 \x0304" in line

    def test_symbols_safe_config_selects_the_bmp_table(self):
        plugin = WeatherPlugin(StubBot(symbols="safe"))
        assert plugin.safe_symbols is True
        payload = _payload(code=302, desc="Moderate rain")
        line = plugin.format_weather(payload, "America/Los_Angeles", None)
        assert "☂ Moderate rain" in line

    def test_symbols_default_to_the_full_table(self):
        assert WeatherPlugin(StubBot()).safe_symbols is False
        payload = _payload(code=302, desc="Moderate rain")
        assert "\U0001f327 Moderate rain" in self.format(payload)

    def test_unknown_timezone_falls_back_to_utc(self):
        # 19:00 UTC is still daylight, so this must not blow up
        assert "Parklabrea" in self.format(timezone="Mars/Olympus_Mons")

    def test_missing_timezone_falls_back_to_utc(self):
        assert "Parklabrea" in self.format(timezone=None)


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.plugin = WeatherPlugin(StubBot())

    def _run(self, *responses, location="90210"):
        factory, session = fake_session(*responses)
        with patch("onebot.plugins.weather.aiohttp.ClientSession", factory):
            result = asyncio.run(self.plugin._fetch(location))
        return result, session

    def test_forecast_and_timezone_are_fetched_together(self):
        payload = _payload()
        (data, timezone), session = self._run(
            FakeResponse(json_data=payload),
            FakeResponse(text="America/Chicago\n"),
        )
        assert data == payload
        assert timezone == "America/Chicago"

        forecast, tz = session.requests
        assert forecast[1] == "https://wttr.in/90210"
        assert forecast[2]["params"] == {"format": "j1"}
        assert tz[2]["params"] == {"format": "%Z"}

    def test_unresolvable_location_raises_lookup_error(self):
        # wttr.in answers 500 with a "location not found" body
        with self.assertRaises(LookupError):
            self._run(FakeResponse(status=500), FakeResponse(text="America/Chicago"))

    def test_timezone_failure_is_not_fatal(self):
        (data, timezone), _ = self._run(
            FakeResponse(json_data=_payload()), FakeResponse(status=500)
        )
        assert data is not None
        assert timezone is None

    def test_timezone_connection_error_is_not_fatal(self):
        (_, timezone), _ = self._run(
            FakeResponse(json_data=_payload()), OSError("boom")
        )
        assert timezone is None

    def test_forecast_errors_propagate(self):
        with self.assertRaises(OSError):
            self._run(OSError("boom"), FakeResponse(text="UTC"))

    def test_second_lookup_is_served_from_cache(self):
        payload = _payload()
        self._run(FakeResponse(json_data=payload), FakeResponse(text="America/Chicago"))
        # No responses queued: any request would raise AssertionError
        (data, timezone), session = self._run()
        assert data == payload
        assert timezone == "America/Chicago"
        assert session.requests == []

    def test_expired_cache_refetches_but_reuses_the_timezone(self):
        self._run(
            FakeResponse(json_data=_payload()), FakeResponse(text="America/Chicago")
        )
        self.plugin._forecasts["90210"] = (
            self.plugin._forecasts["90210"][0] - 10_000,
            self.plugin._forecasts["90210"][1],
        )
        (_, timezone), session = self._run(FakeResponse(json_data=_payload()))
        assert timezone == "America/Chicago"
        assert len(session.requests) == 1

    def test_cache_is_bounded(self):
        """Any user can name a location nobody has asked for before."""
        for n in range(CACHE_MAX_LOCATIONS + 20):
            self._run(
                FakeResponse(json_data=_payload()),
                FakeResponse(text="UTC"),
                location="loc-{}".format(n),
            )
        assert len(self.plugin._forecasts) == CACHE_MAX_LOCATIONS
        assert len(self.plugin._timezones) == CACHE_MAX_LOCATIONS

    def test_eviction_drops_the_least_recently_used_location(self):
        for name in ("first", "second"):
            self._run(
                FakeResponse(json_data=_payload()),
                FakeResponse(text="UTC"),
                location=name,
            )
        # Touch "first" so "second" becomes the eviction candidate
        self._run(location="first")
        for n in range(CACHE_MAX_LOCATIONS - 1):
            self._run(
                FakeResponse(json_data=_payload()),
                FakeResponse(text="UTC"),
                location="filler-{}".format(n),
            )
        assert "first" in self.plugin._forecasts
        assert "second" not in self.plugin._forecasts

    def test_stale_entry_is_released_on_access(self):
        self._run(FakeResponse(json_data=_payload()), FakeResponse(text="UTC"))
        stale = (self.plugin._forecasts["90210"][0] - 10_000, {"old": True})
        self.plugin._forecasts["90210"] = stale
        self._run(FakeResponse(json_data=_payload()))
        assert self.plugin._forecasts["90210"][1] != {"old": True}

    def test_stale_payload_is_dropped_even_if_the_refetch_fails(self):
        """Otherwise an unreachable location pins its payload indefinitely."""
        self._run(FakeResponse(json_data=_payload()), FakeResponse(text="UTC"))
        self.plugin._forecasts["90210"] = (
            self.plugin._forecasts["90210"][0] - 10_000,
            {"old": True},
        )
        with self.assertRaises(OSError):
            self._run(OSError("boom"))
        assert "90210" not in self.plugin._forecasts

    def test_cache_is_per_location(self):
        self._run(FakeResponse(json_data=_payload()), FakeResponse(text="UTC"))
        (_, timezone), session = self._run(
            FakeResponse(json_data=_payload(area=AMSTERDAM)),
            FakeResponse(text="Europe/Amsterdam"),
            location="Amsterdam",
        )
        assert timezone == "Europe/Amsterdam"
        assert len(session.requests) == 2


class StubUser(object):
    """Stands in for onebot.plugins.users.User.

    Settings go through the same JSON round trip the real user does, because
    that round trip is what turns a stored "90210" back into an int.
    """

    def __init__(self, settings=None):
        self.settings = {}
        for setting, value in (settings or {}).items():
            self._store(setting, value)

    def _store(self, setting, value):
        self.settings[setting] = value if isinstance(value, str) else json.dumps(value)

    async def get_setting(self, setting, default=None):
        return deserialize_setting(self.settings.get(setting, default))

    async def set_setting(self, setting, value):
        self._store(setting, value)


class CommandBot(StubBot):
    def __init__(self, user=None, nick="onebot", cmd="."):
        super().__init__()
        self.nick = nick
        self.config = dict(self.config, cmd=cmd)
        self.config = _AttrConfig(self.config)
        self._user = user

    def get_user(self, nick):
        return self._user


class _AttrConfig(dict):
    """irc3's config exposes keys as attributes too."""

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError:
            raise AttributeError(item)


class Mask(str):
    @property
    def nick(self):
        return self.split("!")[0]


@freeze_time("2026-08-08 19:00:00")
class CommandTest(unittest.TestCase):
    def setUp(self):
        self.user = StubUser()
        self.bot = CommandBot(user=self.user)
        self.plugin = WeatherPlugin(self.bot)
        self.plugin._fetch = AsyncMock(return_value=(_payload(), "America/Chicago"))

    def call(self, location=None, metric=False, imperial=False, target="#chan"):
        args = {
            "<location>": location.split() if location else [],
            "--metric": metric,
            "--imperial": imperial,
        }
        return asyncio.run(self.plugin.w(Mask("user!u@h"), target, args))

    def test_explicit_location_is_looked_up_and_remembered(self):
        response = self.call("90210")
        self.plugin._fetch.assert_awaited_once_with("90210")
        assert "Parklabrea" in response
        assert self.user.settings["weather_location"] == "90210"

    def test_multi_word_locations_are_joined(self):
        self.call("Beverly Hills CA")
        self.plugin._fetch.assert_awaited_once_with("Beverly Hills CA")

    def test_stored_location_is_reused(self):
        self.user.settings["weather_location"] = "Amsterdam"
        self.call()
        self.plugin._fetch.assert_awaited_once_with("Amsterdam")

    def test_a_stored_numeric_location_is_looked_up_as_a_string(self):
        """Settings round-trip through JSON, so a zipcode returns as an int."""
        self.call("90210")
        self.plugin._fetch.reset_mock()
        self.call()
        self.plugin._fetch.assert_awaited_once_with("90210")

    def test_a_new_location_replaces_the_stored_one(self):
        self.user.settings["weather_location"] = "Amsterdam"
        self.call("90210")
        assert self.user.settings["weather_location"] == "90210"

    def test_no_location_and_nothing_stored_explains_itself(self):
        response = self.call()
        self.plugin._fetch.assert_not_awaited()
        assert "I don't know where you are" in response
        assert ".w <city or zipcode>" in response

    def test_unresolvable_location_is_not_stored(self):
        self.plugin._fetch.side_effect = LookupError
        response = self.call("Atlantis")
        assert "couldn't find" in response
        assert "weather_location" not in self.user.settings

    def test_network_failure_is_reported(self):
        self.plugin._fetch.side_effect = asyncio.TimeoutError
        assert "isn't answering" in self.call("90210")

    def test_a_payload_that_is_not_j1_is_reported(self):
        """wttr.in answers 200 with an error document, and HTML when limiting."""
        self.plugin._fetch.return_value = ({"data": {"error": [{}]}}, "UTC")
        assert "couldn't read" in self.call("90210")

    def test_a_truncated_payload_is_reported(self):
        payload = _payload()
        del payload["current_condition"][0]["temp_C"]
        self.plugin._fetch.return_value = (payload, "America/Chicago")
        assert "couldn't read" in self.call("90210")

    def test_metric_flag_is_applied_and_remembered(self):
        response = self.call("90210", metric=True)
        assert "30°C" in response
        assert self.user.settings["weather_units"] == "metric"

    def test_imperial_flag_is_applied_and_remembered(self):
        response = self.call("Amsterdam", imperial=True)
        assert "86°F" in response
        assert self.user.settings["weather_units"] == "imperial"

    def test_stored_units_are_reused(self):
        self.user.settings["weather_units"] = "metric"
        assert "30°C" in self.call("90210")

    def test_units_are_not_stored_unless_asked_for(self):
        self.call("90210")
        assert "weather_units" not in self.user.settings

    def test_ignored_channels_stay_quiet(self):
        self.plugin.ignored_channels = ["#quiet"]
        assert self.call("90210", target="#quiet") is None
        self.plugin._fetch.assert_not_awaited()

    def test_the_bot_does_not_answer_itself(self):
        assert (
            asyncio.run(self.plugin.w(Mask("onebot!u@h"), "#chan", {"<location>": []}))
            is None
        )

    def test_an_unknown_user_still_gets_an_answer(self):
        self.bot._user = None
        assert "Parklabrea" in self.call("90210")

    def test_an_unknown_user_without_a_location_is_told_so(self):
        self.bot._user = None
        assert "I don't know where you are" in self.call()


async def one_moment():
    with freeze_time("2026-08-08 19:00:00", tick=True):
        await asyncio.sleep(0.01)


@freeze_time("2026-08-08 19:00:00")
@patch("onebot.plugins.users.UsersPlugin", new=MagicMock())
class WeatherCommandIntegrationTest(BotTestCase):
    """Drive the command the way irc3 does, through docopt and dispatch."""

    config = {
        "includes": ["onebot.plugins.weather"],
        "onebot.plugins.weather": {},
        "onebot.plugins.users": {"identified_by": "mask"},
        "irc3.plugins.command": {"antiflood": False},
        "cmd": "!",
        "loop": None,
    }

    @patch("irc3.plugins.storage.Storage", spec=True)
    def setUp(self, mock):
        super().setUp()
        self.config["loop"] = asyncio.new_event_loop()
        asyncio.set_event_loop(self.config["loop"])
        self.callFTU()
        self.plugin = self.bot.get_plugin("onebot.plugins.weather.WeatherPlugin")
        self.plugin._fetch = AsyncMock(return_value=(_payload(), "America/Chicago"))
        self.user = StubUser()
        self.bot.get_user = lambda nick: self.user

    def tearDown(self):
        super().tearDown()
        self.bot.SIGINT()

    def dispatch(self, line):
        async def wrap():
            self.bot.dispatch(line)
            await one_moment()

        self.bot.loop.run_until_complete(wrap())
        return self.bot.sent

    def test_lookup_with_a_location(self):
        sent = self.dispatch(":bar!foo@host PRIVMSG #chan :!w 90210")
        self.plugin._fetch.assert_awaited_once_with("90210")
        assert len(sent) == 1
        assert "Parklabrea, California, USA" in sent[0]
        assert sent[0].startswith("PRIVMSG #chan :")

    def test_multi_word_location_survives_docopt(self):
        self.dispatch(":bar!foo@host PRIVMSG #chan :!w Beverly Hills CA")
        self.plugin._fetch.assert_awaited_once_with("Beverly Hills CA")

    def test_remembered_location_is_used_on_the_next_call(self):
        self.dispatch(":bar!foo@host PRIVMSG #chan :!w 90210")
        self.dispatch(":bar!foo@host PRIVMSG #chan :!w")
        assert self.plugin._fetch.await_args_list[1].args == ("90210",)

    def test_metric_flag_survives_docopt(self):
        sent = self.dispatch(":bar!foo@host PRIVMSG #chan :!w --metric 90210")
        self.plugin._fetch.assert_awaited_once_with("90210")
        assert "30°C" in sent[0]

    def test_works_in_a_private_message(self):
        sent = self.dispatch(":bar!foo@host PRIVMSG {} :!w 90210".format(self.bot.nick))
        assert sent[0].startswith("PRIVMSG bar :")

    def test_no_stored_location(self):
        sent = self.dispatch(":bar!foo@host PRIVMSG #chan :!w")
        self.plugin._fetch.assert_not_awaited()
        assert "I don't know where you are" in sent[0]
        assert "!w <city or zipcode>" in sent[0]
