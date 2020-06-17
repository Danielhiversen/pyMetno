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
DEFAULT_API_URL = "https://api.met.no/weatherapi/locationforecast/1.9/"
EARTH_RADIUS = 6371 * 1000  # earth radius

_LOGGER = logging.getLogger(__name__)


class MetWeatherData:
    """Representation of met weather data."""

    def __init__(self, urlparams, websession=None, api_url=DEFAULT_API_URL):
        """Initialize the Weather object."""
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

    def get_forecast(self, time_zone):
        """Get the forecast weather data from met.no."""
        if self.data is None:
            return []

        now = datetime.datetime.now(time_zone).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        times = [now + datetime.timedelta(days=k) for k in range(1, 6)]
        return [self.get_weather(_time) for _time in times]

    def get_weather(self, time, max_hour=6):
        """Get the current weather data from met.no."""
        if self.data is None:
            return {}

        ordered_entries = []
        for time_entry in self.data["product"]["time"]:
            valid_from = parse_datetime(time_entry["@from"])
            valid_to = parse_datetime(time_entry["@to"])
            if time > valid_to:
                # Has already passed. Never select this.
                continue

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
        res["temperature"] = get_data("temperature", ordered_entries)
        res["condition"] = CONDITIONS.get(get_data("symbol", ordered_entries))
        res["pressure"] = get_data("pressure", ordered_entries)
        res["humidity"] = get_data("humidity", ordered_entries)
        res["wind_speed"] = get_data("windSpeed", ordered_entries)
        res["wind_bearing"] = get_data("windDirection", ordered_entries)
        return res


def get_data(param, data):
    """Retrieve weather parameter."""
    try:
        for (_, selected_time_entry) in data:
            loc_data = selected_time_entry["location"]
            if param not in loc_data:
                continue
            if param == "precipitation":
                new_state = loc_data[param]["@value"]
            elif param == "symbol":
                new_state = int(float(loc_data[param]["@number"]))
            elif param in ("temperature", "pressure", "humidity", "dewpointTemperature"):
                new_state = round(float(loc_data[param]["@value"]), 1)
            elif param in ("windSpeed", "windGust"):
                new_state = round(float(loc_data[param]["@mps"]) * 3.6, 1)
            elif param == "windDirection":
                new_state = round(float(loc_data[param]["@deg"]), 1)
            elif param in ("fog", "cloudiness", "lowClouds", "mediumClouds", "highClouds"):
                new_state = round(float(loc_data[param]["@percent"]), 1)
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


class LightningData:
    """Get the latest data."""

    # pylint: disable=too-many-locals, too-few-public-methods

    def __init__(self, websession):
        """Initialize the Lightning object."""
        self._websession = websession
        self._api_url = "https://api.met.no/weatherapi/lightning/1.0/"

    async def within_radius(self, latitude, longitude, radius):
        """Get lightning with radius."""
        try:
            with async_timeout.timeout(10):
                resp = await self._websession.get(self._api_url)
            if resp.status >= 400:
                _LOGGER.error("%s returned %s", self._api_url, resp.status)
                return []
            data = await resp.text()
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("%s returned %s", self._api_url, err)
            return []

        res = {}
        for row in data.split("\n"):
            elements = row.split(" ")
            if len(elements) < 25:
                continue

            lat = float(elements[8])
            long = float(elements[9])

            dlat = math.radians(lat - latitude)
            dlon = math.radians(long - longitude)
            var_a = math.sin(dlat / 2) * math.sin(dlat / 2) + math.cos(
                math.radians(lat)
            ) * math.cos(math.radians(latitude)) * math.sin(dlon / 2) * math.sin(
                dlon / 2
            )
            var_c = 2 * math.atan2(math.sqrt(var_a), math.sqrt(1 - var_a))
            distance = EARTH_RADIUS * var_c
            if distance > radius:
                continue

            date = datetime.datetime.strptime(
                "".join(elements[1:7]), "%Y%m%d%H%M%S"
            ).astimezone(pytz.utc)

            res["".join(elements[1:8])] = {
                "distance": distance,
                "lat": lat,
                "long": long,
                "date": date,
            }

        return res


def parse_datetime(dt_str):
    """Parse datetime."""
    date_format = "%Y-%m-%dT%H:%M:%S %z"
    dt_str = dt_str.replace("Z", " +0000")
    return datetime.datetime.strptime(dt_str, date_format)
