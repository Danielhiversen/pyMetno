import logging
from xml.parsers.expat import ExpatError

import aiohttp
import async_timeout
import asyncio
import xmltodict

DEFAULT_API_URL = 'https://api.met.no/weatherapi/locationforecast/1.9/'

_LOGGER = logging.getLogger(__name__)

class MetWeatherData:
    def __init__(self, urlparams, websession=None, api_url=DEFAULT_API_URL):
        self._urlparams = urlparams
        self._api_url = api_url
        if websession is None:
            async def _create_session():
                return aiohttp.ClientSession()
            loop = asyncio.get_event_loop()
            self.websession = loop.run_until_complete(_create_session())
        else:
            self.websession = websession
        self._weather_data = None

    async def fetching_data(self, *_):
        """Get the latest data from met.no."""

        try:
            with async_timeout.timeout(10, loop=self.hass.loop):
                resp = await self._websession.get(self._url, params=self._urlparams)
            if resp.status != 200:
                _LOGGER.error('%s returned %s', resp.url, resp.status)
                return False
            text = await resp.text()
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error('%s returned %s', resp.url, err)
            return False
        try:
            self._weather_data = xmltodict.parse(text)['weatherdata']
        except (ExpatError, IndexError) as err:
            _LOGGER.error('%s returned %s', resp.url, err)
            return False
        return True


def get_forecast(param, data):
    """Retrieve forecast parameter."""

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