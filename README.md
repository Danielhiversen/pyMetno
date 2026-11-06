# pyMetno [![Build Status](https://travis-ci.org/Danielhiversen/pyMetno.svg?branch=master)](https://travis-ci.org/Danielhiversen/pyMetno)
Python library to talk to the met.no api

[Buy me a coffee :)](http://paypal.me/dahoiv)


## Fetching Locationforecast data

Pass an `aiohttp.ClientSession` to `MetWeatherData` and keep the session open
for the lifetime of the client:

```python
import aiohttp
from metno import MetNoError, MetWeatherData

async def forecast():
    async with aiohttp.ClientSession(headers={"User-Agent": "my-app/1.0 contact@example.com"}) as session:
        weather = MetWeatherData({"lat": 60, "lon": 10, "altitude": 0}, session)
        try:
            await weather.fetch_data()
        except MetNoError as error:
            print(error, weather.next_fetch_at)
            return None
        return weather.data
```

`await fetch_data()` returns `True` on success and raises `MetNoError` on
failure. Subclasses distinguish `MetNoHttpError` (with a `status` attribute),
`MetNoPayloadError` (JSON, schema or an unexpected 304), `MetNoTimeoutError`,
and `MetNoTransportError`. The legacy `await fetching_data(*args)` continues
to return `True` or `False`. Both APIs expose the latest typed failure through
`last_error`, clear it on success, and propagate task cancellation. The client
does not log fetch failures; callers can choose their own reporting policy.
A valid HTTP 203 response succeeds and follows the normal cache policy. Each
received 203 logs a deprecation warning as required by the
[MET terms of service](https://api.met.no/doc/TermsOfService); cache hits do not
repeat the warning.

A successful download or 304 revalidation is cached for **at least three
hours**, for all geographic regions. A later valid HTTP `Expires` extends that
deadline. Explicit/manual calls obey the same cache policy. `next_fetch_at`
is an aware UTC datetime for the next permitted attempt, or `None` before
fetching. Revalidation sends the exact `Last-Modified` value as
`If-Modified-Since`; a 304 reuses the validated body without decoding a body.
The timeout covers response acquisition, body reading and JSON decoding.
Invalid responses leave the last known good representation and its validator
intact; invalid new bodies never supply validators.

Each client keeps an in-memory cache keyed by the endpoint and normalized
request: latitude/longitude rounded to four decimal places and altitude
truncated to an integer (`msl` remains a constructor alias for altitude).
Different keys have independent bodies, validators, errors and deadlines;
returning to a key reuses its cache. Requests on a client are serialized to
avoid concurrent duplicate downloads. Nothing is shared across clients or
persisted, and the client starts no background work.

Failures schedule a later attempt instead of retrying inside the call.
Backoff begins at **60 seconds**, doubles after consecutive failures, adds
positive random jitter of up to 10%, and caps the total delay at **one hour**.
This cooldown also applies to non-transient HTTP failures to avoid repeated
manual calls. Later valid `Retry-After` (seconds or HTTP-date), `Expires`, and
an existing cache deadline take precedence. Successful downloading or
revalidation resets backoff. Calls during a failure cooldown return `False`
or raise the same typed error, even when older usable data is still present.
The caller decides when to call again; no sleeps or retry loops are started.

## Tests

```sh
uv run --no-project --with-requirements requirements-test.txt --with aiohttp --with async-timeout python -m pytest
```
