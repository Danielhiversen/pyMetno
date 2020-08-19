"""Library to handle connection with met.no api."""
import asyncio
import datetime
import logging
import math
from xml.parsers.expat import ExpatError

import aiohttp
import async_timeout
import pytz
import xmltodict

# https://api.met.no/weatherapi/weathericon/_/documentation/#___top
CONDITIONS = {
    1: "sunny",
    2: "partlycloudy",
    3: "partlycloudy",
    4: "cloudy",
    5: "rainy",
    6: "lightning-rainy",
    7: "snowy-rainy",
    8: "snowy",
    9: "rainy",
    10: "rainy",
    11: "lightning-rainy",
    12: "snowy-rainy",
    13: "snowy",
    14: "snowy",
    15: "fog",
    20: "lightning-rainy",
    21: "lightning-rainy",
    22: "lightning-rainy",
    23: "lightning-rainy",
    24: "lightning-rainy",
    25: "lightning-rainy",
    26: "lightning-rainy",
    27: "lightning-rainy",
    28: "lightning-rainy",
    29: "lightning-rainy",
    30: "lightning-rainy",
    31: "lightning-rainy",
    32: "lightning-rainy",
    33: "lightning-rainy",
    34: "lightning-rainy",
    40: "rainy",
    41: "rainy",
    42: "snowy-rainy",
    43: "snowy-rainy",
    44: "snowy",
    45: "snowy",
    46: "rainy",
    47: "snowy-rainy",
    48: "snowy-rainy",
    49: "snowy",
    50: "snowy",
}
DEFAULT_API_URL = "https://api.met.no/weatherapi/locationforecast/2.0/classic"

EARTH_RADIUS = 6371 * 1000  # earth radius

_LOGGER = logging.getLogger(__name__)


class MetWeatherData:
    """Representation of met weather data."""

    def __init__(self, urlparams, websession=None, api_url=DEFAULT_API_URL):
        """Initialize the Weather object."""
        urlparams = {"lat": str(round(float(urlparams['lat']), 4)),
                     "lon": str(round(float(urlparams['lon']), 4)),
                     "altitude": str(int(float(urlparams.get('altitude', urlparams.get('msl', 0))))),
                     }
        self._urlparams = urlparams
        self._api_url = api_url
        if websession is None:

            async def _create_session():
                return aiohttp.ClientSession()

            loop = asyncio.get_event_loop()
            self._websession = loop.run_until_complete(_create_session())
        else:
            self._websession = websession
        self.data = None

    async def fetching_data(self, *_):
        """Get the latest data from met.no."""
        try:
            with async_timeout.timeout(10):
                resp = await self._websession.get(self._api_url, params=self._urlparams)
            if resp.status >= 400:
                _LOGGER.error("%s returned %s", self._api_url, resp.status)
                return False
            text = await resp.text()
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("%s returned %s", self._api_url, err)
            return False
        try:
            self.data = xmltodict.parse(text)["weatherdata"]
        except (ExpatError, IndexError) as err:
            _LOGGER.error("%s returned %s", resp.url, err)
            return False
        return True

    def get_current_weather(self):
        """Get the current weather data from met.no."""
        return self.get_weather(datetime.datetime.now(pytz.utc))

    def get_forecast(self, time_zone, hourly=False):
        """Get the forecast weather data from met.no."""
        if self.data is None:
            return []

        if hourly:
            now = datetime.datetime.now(time_zone).replace(
                minute=0, second=0, microsecond=0
            )
            times = [now + datetime.timedelta(hours=k) for k in range(1, 25)]
        else:
            now = datetime.datetime.now(time_zone).replace(
                hour=12, minute=0, second=0, microsecond=0
            )
            times = [now + datetime.timedelta(days=k) for k in range(1, 6)]
        return [self.get_weather(_time, hourly=hourly) for _time in times]

    def get_weather(self, time, max_hour=6, hourly=False):
        """Get the current weather data from met.no."""
        # pylint: disable=too-many-locals
        if self.data is None:
            return {}

        day = time.date()
        daily_temperatures = []
        daily_precipitation = []
        daily_windspeed = []
        daily_windgust = []
        ordered_entries = []
        for time_entry in self.data["product"]["time"]:
            valid_from = parse_datetime(time_entry["@from"])
            valid_to = parse_datetime(time_entry["@to"])
            if time > valid_to:
                # Has already passed. Never select this.
                continue

            # Collect all daily values to calculate min/max/sum
            if valid_from.date() == day or valid_to.date() == day:

                if "temperature" in time_entry["location"]:
                    daily_temperatures.append(
                        get_value(time_entry["location"]["temperature"], "@value")
                    )
                if "precipitation" in time_entry["location"]:
                    daily_precipitation.append(
                        get_value(time_entry["location"]["precipitation"], "@value")
                    )
                if "windSpeed" in time_entry["location"]:
                    daily_windspeed.append(
                        get_value(time_entry["location"]["windSpeed"], "@mps")
                    )
                if "windGust" in time_entry["location"]:
                    daily_windgust.append(
                        get_value(time_entry["location"]["windGust"], "@mps")
                    )

            average_dist = abs((valid_to - time).total_seconds()) + abs(
                (valid_from - time).total_seconds()
            )

            if average_dist > max_hour * 3600:
                continue

            ordered_entries.append((average_dist, time_entry))

        if not ordered_entries:
            return {}
        ordered_entries.sort(key=lambda item: item[0])
        res = dict()
        res["datetime"] = time
        res["condition"] = CONDITIONS.get(get_data("symbol", ordered_entries))
        res["pressure"] = get_data("pressure", ordered_entries)
        res["humidity"] = get_data("humidity", ordered_entries)
        res["wind_bearing"] = get_data("windDirection", ordered_entries)
        if hourly:
            res["temperature"] = get_data("temperature", ordered_entries)
            res["precipitation"] = get_data("precipitation", ordered_entries)
            res["wind_speed"] = get_data("windSpeed", ordered_entries)
            res["wind_gust"] = get_data("windGust", ordered_entries)
            res["cloudiness"] = get_data("cloudiness", ordered_entries)
        else:
            res["temperature"] = (
                None if daily_temperatures == [] else max(daily_temperatures)
            )
            res["templow"] = (
                None if daily_temperatures == [] else min(daily_temperatures)
            )
            res["precipitation"] = (
                None if daily_precipitation == [] else math.fsum(daily_precipitation)
            )
            res["wind_speed"] = (
                None if daily_windspeed == [] else max(daily_windspeed)
            )
            res["wind_gust"] = (
                None if daily_windgust == [] else max(daily_windgust)
            )
        return res


def get_value(data, value):
    """Retrieve weather value."""
    try:
        if value == "@mps":
            return round(float(data[value]) * 3.6, 1)
        return round(float(data[value]), 1)
    except (ValueError, IndexError, KeyError):
        return None


def get_data(param, data):
    """Retrieve weather parameter."""
    try:
        for (_, selected_time_entry) in data:
            loc_data = selected_time_entry["location"]
            if param not in loc_data:
                continue
            if param == "symbol":
                new_state = int(float(loc_data[param]["@number"]))
            elif param in (
                    "temperature",
                    "pressure",
                    "humidity",
                    "dewpointTemperature",
                    "precipitation",
            ):
                new_state = get_value(loc_data[param], "@value")
            elif param in ("windSpeed", "windGust"):
                new_state = get_value(loc_data[param], "@mps")
            elif param == "windDirection":
                new_state = get_value(loc_data[param], "@deg")
            elif param in (
                    "fog",
                    "cloudiness",
                    "lowClouds",
                    "mediumClouds",
                    "highClouds",
            ):
                new_state = get_value(loc_data[param], "@percent")
            return new_state
    except (ValueError, IndexError, KeyError):
        return None


class AirQualityData:
    """Get the latest data."""

    # pylint: disable=too-many-instance-attributes, too-few-public-methods

    def __init__(self, coordinates, forecast, websession):
        """Initialize the Air quality object."""
        self._urlparams = coordinates
        self._urlparams["areaclass"] = "grunnkrets"
        self._forecast = forecast
        self._websession = websession
        self._api_url = "https://api.met.no/weatherapi/airqualityforecast/0.1/"
        self.data = dict()
        self.units = dict()
        self._last_update = None
        self._data = dict()

    async def update(self):
        """Update data."""
        if self._last_update is None or datetime.datetime.now() - self._last_update > datetime.timedelta(3600):
            try:
                with async_timeout.timeout(10):
                    resp = await self._websession.get(
                        self._api_url, params=self._urlparams
                    )
                if resp.status >= 400:
                    _LOGGER.error("%s returned %s", self._api_url, resp.status)
                    return False
                self._data = await resp.json()
            except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.error("%s returned %s", self._api_url, err)
                return False
        try:
            forecast_time = datetime.datetime.now(pytz.utc) + datetime.timedelta(
                hours=self._forecast
            )

            data = None
            min_dist = 24 * 3600
            for _data in self._data["data"]["time"]:
                valid_from = parse_datetime(_data["from"])
                valid_to = parse_datetime(_data["to"])

                if forecast_time >= valid_to:
                    # Has already passed. Never select this.
                    continue

                average_dist = abs((valid_to - forecast_time).total_seconds()) + abs(
                    (valid_from - forecast_time).total_seconds()
                )
                if average_dist < min_dist:
                    min_dist = average_dist
                    data = _data
            if not data:
                return False
            self.data["aqi"] = data.get("variables", {}).get("AQI", {}).get("value")
            self.data["pm10_concentration"] = (
                data.get("variables", {}).get("pm10_concentration", {}).get("value")
            )
            self.data["o3_concentration"] = (
                data.get("variables", {}).get("o3_concentration", {}).get("value")
            )
            self.data["no2_concentration"] = (
                data.get("variables", {}).get("no2_concentration", {}).get("value")
            )
            self.data["pm25_concentration"] = (
                data.get("variables", {}).get("pm25_concentration", {}).get("value")
            )
            self.data["location"] = "{}, {}".format(
                self._data.get("meta", {}).get("location", {}).get("name"),
                self._data.get("meta", {}).get("superlocation", {}).get("name"),
            )
            state = data.get("variables", {}).get("AQI", {}).get("value")
            if state < 2:
                level = "low"
            elif state < 3:
                level = "medium"
            else:
                level = "high"
            self.data["level"] = level

            self.units["aqi"] = data.get("variables", {}).get("AQI", {}).get("units")
            self.units["pm10_concentration"] = (
                data.get("variables", {}).get("pm10_concentration", {}).get("units")
            )
            self.units["o3_concentration"] = (
                data.get("variables", {}).get("o3_concentration", {}).get("units")
            )
            self.units["no2_concentration"] = (
                data.get("variables", {}).get("no2_concentration", {}).get("units")
            )
            self.units["pm25_concentration"] = (
                data.get("variables", {}).get("pm25_concentration", {}).get("units")
            )
            self.units["aqi"] = data.get("variables", {}).get("AQI", {}).get("value")

        except IndexError as err:
            _LOGGER.error("%s returned %s", resp.url, err)
            return False
        return True


def parse_datetime(dt_str):
    """Parse datetime."""
    date_format = "%Y-%m-%dT%H:%M:%S %z"
    dt_str = dt_str.replace("Z", " +0000")
    return datetime.datetime.strptime(dt_str, date_format)
