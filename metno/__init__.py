"""Library to handle connection with met.no api"""
import asyncio
import datetime
import logging
from xml.parsers.expat import ExpatError

import xmltodict
import aiohttp
import async_timeout


DEFAULT_API_URL = 'https://api.met.no/weatherapi/locationforecast/1.9/'

_LOGGER = logging.getLogger(__name__)


class MetWeatherData:
    """Representation of met weather data."""

    def __init__(self, urlparams, websession=None, api_url=DEFAULT_API_URL):
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
            if resp.status != 200:
                _LOGGER.error('%s returned %s', resp.url, resp.status)
                return False
            text = await resp.text()
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error('%s returned %s', resp.url, err)
            return False
        try:
            self.data = xmltodict.parse(text)['weatherdata']
        except (ExpatError, IndexError) as err:
            _LOGGER.error('%s returned %s', resp.url, err)
            return False
        return True

    def get_current_weather(self):
        """Get the current weather data from met.no."""
        return self.get_weather(datetime.datetime.now())

    def get_forecast(self):
        """Get the forecast weather data from met.no."""
        if self.data is None:
            return []

        times = [datetime.datetime.now() + datetime.timedelta(days=k) for k in range(1, 6)]
        return [self.get_weather(_time, 12) for _time in times]

    def get_weather(self, time, max_hour=6):
        """Get the current weather data from met.no."""
        if self.data is None:
            return {}

        ordered_entries = []
        for time_entry in self._weather_data.data['product']['time']:
            valid_from = dt_util.parse_datetime(time_entry['@from'])
            valid_to = dt_util.parse_datetime(time_entry['@to'])

            if time >= valid_to:
                # Has already passed. Never select this.
                continue

            average_dist = (abs((valid_to - time).total_seconds()) +
                            abs((valid_from - time).total_seconds()))

            if average_dist > max_hour * 3600:
                continue

            ordered_entries.append((average_dist, time_entry))

        if not ordered_entries:
            return {}
        ordered_entries.sort(key=lambda item: item[0])

        res = dict()
        res['temperature'] = get_data('temperature', ordered_entries)
        res['condition'] = get_data('symbol', ordered_entries)
        res['pressure'] = get_data('pressure', ordered_entries)
        res['humidity'] = get_data('humidity', ordered_entries)
        res['wind_speed'] = get_data('windSpeed', ordered_entries)
        res['wind_bearing'] = get_data('windDirection', ordered_entries)
        return res


def get_data(param, data):
    """Retrieve weather parameter."""

    try:
        for (_, selected_time_entry) in data:
            loc_data = selected_time_entry['location']
            if param not in loc_data:
                continue
            if param == 'precipitation':
                new_state = loc_data[param]['@value']
            elif param == 'symbol':
                new_state = int(float(loc_data[param]['@number']))
            elif param in ('temperature', 'pressure', 'humidity',
                           'dewpointTemperature'):
                new_state = round(float(loc_data[param]['@value']), 1)
            elif param in ('windSpeed', 'windGust'):
                new_state = round(float(loc_data[param]['@mps']) * 3.6, 1)
            elif param == 'windDirection':
                new_state = round(float(loc_data[param]['@deg']), 1)
            elif param in ('fog', 'cloudiness', 'lowClouds',
                           'mediumClouds', 'highClouds'):
                new_state = round(float(loc_data[param]['@percent']), 1)
            return new_state
    except (ValueError, IndexError, KeyError):
        return None
