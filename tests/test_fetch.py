"""Behavioral coverage of Locationforecast cache and error handling."""

import asyncio
import copy
import datetime as dt

import aiohttp
import pytest

import metno

UTC = dt.timezone.utc
START = dt.datetime(2026, 1, 1, tzinfo=UTC)
LAST_MODIFIED = "Wed, 31 Dec 2025 22:00:00 GMT"
PAYLOAD = {
    "type": "Feature",
    "geometry": {"type": "Point", "coordinates": [10, 60, 0]},
    "properties": {
        "meta": {
            "updated_at": "2026-01-01T00:00:00Z",
            "units": {"air_temperature": "celsius"},
        },
        "timeseries": [
            {
                "time": "2026-01-01T00:00:00Z",
                "data": {
                    "instant": {"details": {"air_temperature": 4.0}},
                },
            }
        ],
    },
}


class Response:
    def __init__(self, status=200, payload=None, headers=None, error=None, block=False):
        self.status = status
        self.payload = copy.deepcopy(PAYLOAD if payload is None else payload)
        self.headers = headers or {}
        self.error = error
        self.block = block
        self.closed = False
        self.body_reads = 0
        self.started = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def json(self):
        self.body_reads += 1
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        if self.error:
            raise self.error
        return self.payload


class Request:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        if isinstance(self.response, BaseException):
            raise self.response
        return await self.response.__aenter__()

    async def __aexit__(self, *args):
        return await self.response.__aexit__(*args)

    def __await__(self):
        async def acquire():
            if isinstance(self.response, BaseException):
                raise self.response
            return self.response

        return acquire().__await__()


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return Request(self.responses.pop(0))


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> list[dt.datetime]:
    current = [START]
    monkeypatch.setattr(metno, "_utcnow", lambda: current[0], raising=False)
    monkeypatch.setattr("random.uniform", lambda a, b: b)
    return current


def client(*responses):
    session = Session(*responses)
    return metno.MetWeatherData({"lat": "60", "lon": "10"}, session), session


@pytest.mark.asyncio
async def test_cold_fetch_and_three_hour_boundary(now: list[dt.datetime]) -> None:
    weather, session = client(Response(), Response())
    assert await weather.fetching_data() is True
    assert weather.data == PAYLOAD
    assert weather.next_fetch_at == START + dt.timedelta(hours=3)
    now[0] += dt.timedelta(hours=3, microseconds=-1)
    assert await weather.fetching_data("manual") is True
    assert len(session.requests) == 1
    now[0] += dt.timedelta(microseconds=1)
    assert await weather.fetching_data() is True
    assert len(session.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expires,hours",
    [
        ("Thu, 01 Jan 2026 05:00:00 GMT", 5),
        ("invalid", 3),
        ("Thu, 01 Jan 2026 01:00:00 GMT", 3),
        ("999999999999999999999999999999", 3),
    ],
)
async def test_expires_never_shortens_minimum(
    now: list[dt.datetime], expires: str, hours: int
) -> None:
    weather, _ = client(Response(headers={"Expires": expires}))
    assert await weather.fetching_data()
    assert weather.next_fetch_at == START + dt.timedelta(hours=hours)


@pytest.mark.asyncio
async def test_revalidation_uses_exact_validator_and_does_not_read_304(
    now: list[dt.datetime],
) -> None:
    response = Response(304, headers={"Expires": "Thu, 01 Jan 2026 08:00:00 GMT"})
    weather, session = client(
        Response(headers={"Last-Modified": LAST_MODIFIED}), response
    )
    assert await weather.fetching_data()
    now[0] += dt.timedelta(hours=3)
    assert await weather.fetch_data() is None
    assert session.requests[1][1]["headers"] == {"If-Modified-Since": LAST_MODIFIED}
    assert response.body_reads == 0
    assert response.closed
    assert weather.next_fetch_at == START + dt.timedelta(hours=8)
    assert weather.last_error is None


@pytest.mark.asyncio
async def test_cold_304_is_typed_payload_failure(now: list[dt.datetime]) -> None:
    response = Response(304)
    weather, _ = client(response)
    assert await weather.fetching_data() is False
    assert isinstance(weather.last_error, metno.MetNoPayloadError)
    assert response.body_reads == 0
    assert response.closed
    with pytest.raises(metno.MetNoPayloadError):
        await weather.fetch_data()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("lat", "61"),
        ("lon", "11"),
        ("altitude", "1"),
        ("endpoint", "https://example.test/forecast"),
    ],
)
async def test_changed_request_has_own_representation_and_deadline(
    now: list[dt.datetime], field: str, value: str
) -> None:
    weather, session = client(
        Response(headers={"Last-Modified": LAST_MODIFIED}), Response()
    )
    await weather.fetch_data()
    if field == "endpoint":
        weather._api_url = value
    else:
        weather._urlparams[field] = value
    await weather.fetch_data()
    assert len(session.requests) == 2
    assert session.requests[1][1]["headers"] == {}
    assert session.requests[1][0] == weather._api_url


@pytest.mark.asyncio
async def test_equivalent_normalized_coordinates_reuse_cache(
    now: list[dt.datetime],
) -> None:
    weather, session = client(Response())
    await weather.fetch_data()
    weather._urlparams.update(lat="60.00001", lon="10.00001", altitude="0.9")
    await weather.fetch_data()
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_switching_back_restores_key_data_and_error_state(
    now: list[dt.datetime],
) -> None:
    weather, session = client(Response(), Response(503))
    await weather.fetch_data()
    original = weather.data
    weather._urlparams["lat"] = "61"
    assert await weather.fetching_data() is False
    assert weather.data is None
    weather._urlparams["lat"] = "60"
    await weather.fetch_data()
    assert weather.data is original
    assert weather.last_error is None
    assert len(session.requests) == 2


@pytest.mark.asyncio
async def test_concurrent_calls_share_one_download(now: list[dt.datetime]) -> None:
    response = Response(block=True)
    weather, session = client(response, Response())
    first = asyncio.create_task(weather.fetching_data())
    await response.started.wait()
    second = asyncio.create_task(weather.fetching_data())
    response.block = False
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    # A cancelled download must not populate the cache; queued caller can retry.
    assert await second
    assert len(session.requests) == 2
    assert response.closed


@pytest.mark.asyncio
async def test_concurrent_successes_are_serialized(now: list[dt.datetime]) -> None:
    weather, session = client(Response())
    assert (
        await asyncio.gather(*(weather.fetching_data() for _ in range(10)))
        == [True] * 10
    )
    assert len(session.requests) == 1


BAD_PAYLOADS = [
    None,
    [],
    {},
    {"properties": []},
    {"properties": {"timeseries": []}},
    {
        "properties": {
            "timeseries": [{"time": "bad", "data": {"instant": {"details": {}}}}]
        }
    },
    {
        "properties": {
            "timeseries": [
                {"time": "2026-01-01T00:00:00", "data": {"instant": {"details": {}}}}
            ]
        }
    },
    {
        "properties": {
            "timeseries": [
                {"time": "2026-01-01T00:00:00Z", "data": {"instant": {"details": []}}}
            ]
        }
    },
    {"properties": {"timeseries": [None]}},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", BAD_PAYLOADS)
async def test_schema_failure_preserves_good_data_and_validator(
    now: list[dt.datetime], payload: object
) -> None:
    bad = Response(headers={"Last-Modified": "invalid representation"})
    bad.payload = payload
    weather, session = client(
        Response(headers={"Last-Modified": LAST_MODIFIED}), bad, Response(304)
    )
    await weather.fetch_data()
    original = weather.data
    now[0] += dt.timedelta(hours=3)
    assert await weather.fetching_data() is False
    assert isinstance(weather.last_error, metno.MetNoPayloadError)
    assert weather.data is original
    assert bad.closed
    now[0] = weather.next_fetch_at
    await weather.fetch_data()
    assert session.requests[2][1]["headers"] == {"If-Modified-Since": LAST_MODIFIED}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,error_name",
    [
        (Response(error=ValueError("invalid JSON")), "MetNoPayloadError"),
        (
            Response(error=aiohttp.ContentTypeError(None, (), message="wrong type")),
            "MetNoPayloadError",
        ),
        (aiohttp.ClientConnectionError("offline"), "MetNoTransportError"),
        (asyncio.TimeoutError(), "MetNoTimeoutError"),
        (Response(403), "MetNoHttpError"),
    ],
)
async def test_typed_failures_and_legacy_boolean(
    now: list[dt.datetime],
    response: object,
    error_name: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    weather, session = client(response)
    assert await weather.fetching_data() is False
    error = weather.last_error
    assert isinstance(error, getattr(metno, error_name))
    assert isinstance(error, metno.MetNoError)
    assert weather.next_fetch_at > START
    with pytest.raises(getattr(metno, error_name)) as raised:
        await weather.fetch_data()
    assert raised.value is error
    assert len(session.requests) == 1
    assert not caplog.records


@pytest.mark.asyncio
async def test_timeout_includes_body_read_and_releases_response(
    now: list[dt.datetime], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(metno, "TIMEOUT", 0.01)
    response = Response(block=True)
    weather, _ = client(response)
    assert await asyncio.wait_for(weather.fetching_data(), 0.1) is False
    assert isinstance(weather.last_error, metno.MetNoTimeoutError)
    assert response.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("retry_after", ["18000", "Thu, 01 Jan 2026 05:00:00 GMT"])
async def test_retry_after_and_error_cooldown(
    now: list[dt.datetime], status: int, retry_after: str
) -> None:
    weather, session = client(
        Response(status, headers={"Retry-After": retry_after}), Response()
    )
    assert await weather.fetching_data() is False
    assert weather.last_error.status == status
    assert weather.next_fetch_at == START + dt.timedelta(hours=5)
    now[0] += dt.timedelta(hours=4)
    assert await weather.fetching_data() is False
    assert len(session.requests) == 1
    now[0] += dt.timedelta(hours=1)
    assert await weather.fetching_data() is True
    assert weather.last_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after", ["bad", "-10", "9" * 400])
async def test_invalid_retry_after_keeps_positive_backoff(
    now: list[dt.datetime], retry_after: str
) -> None:
    weather, _ = client(Response(503, headers={"Retry-After": retry_after}))
    assert await weather.fetching_data() is False
    assert START < weather.next_fetch_at <= START + dt.timedelta(hours=1)


@pytest.mark.asyncio
async def test_failure_expires_is_respected_without_validating_body(
    now: list[dt.datetime],
) -> None:
    weather, _ = client(
        Response(503, headers={"Expires": "Thu, 01 Jan 2026 07:00:00 GMT"})
    )
    assert await weather.fetching_data() is False
    assert weather.next_fetch_at == START + dt.timedelta(hours=7)


@pytest.mark.asyncio
async def test_backoff_grows_caps_and_resets_after_success(
    now: list[dt.datetime],
) -> None:
    weather, _ = client(*(Response(503) for _ in range(9)), Response(), Response(503))
    delays = []
    for _ in range(9):
        assert await weather.fetching_data() is False
        delays.append((weather.next_fetch_at - now[0]).total_seconds())
        now[0] = weather.next_fetch_at
    assert delays[:3] == [66, 132, 264]
    assert delays[-1] == 3600
    assert all(a <= b for a, b in zip(delays, delays[1:]))
    await weather.fetch_data()
    now[0] = weather.next_fetch_at
    assert await weather.fetching_data() is False
    assert (weather.next_fetch_at - now[0]).total_seconds() == 66


@pytest.mark.asyncio
async def test_invalid_cold_representation_never_sends_validator(
    now: list[dt.datetime],
) -> None:
    weather, session = client(
        Response(error=ValueError(), headers={"Last-Modified": LAST_MODIFIED}),
        Response(),
    )
    assert await weather.fetching_data() is False
    now[0] = weather.next_fetch_at
    await weather.fetch_data()
    assert session.requests[1][1]["headers"] == {}


@pytest.mark.asyncio
async def test_instances_do_not_share_cache(now: list[dt.datetime]) -> None:
    first, _ = client(Response())
    second, session = client(Response(503))
    await first.fetch_data()
    assert await second.fetching_data() is False
    assert second.data is None
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_old_good_data_does_not_mask_failure_cooldown(
    now: list[dt.datetime],
) -> None:
    weather, session = client(Response(), Response(503))
    await weather.fetch_data()
    original = weather.data
    now[0] = weather.next_fetch_at
    assert await weather.fetching_data() is False
    assert weather.data is original
    error = weather.last_error
    assert await weather.fetching_data() is False
    with pytest.raises(metno.MetNoHttpError) as raised:
        await weather.fetch_data()
    assert raised.value is error
    assert len(session.requests) == 2


@pytest.mark.asyncio
async def test_success_without_validator_drops_old_validator(
    now: list[dt.datetime],
) -> None:
    weather, session = client(
        Response(headers={"Last-Modified": LAST_MODIFIED}), Response(), Response()
    )
    await weather.fetch_data()
    now[0] = weather.next_fetch_at
    await weather.fetch_data()
    now[0] = weather.next_fetch_at
    await weather.fetch_data()
    assert session.requests[2][1]["headers"] == {}


@pytest.mark.asyncio
async def test_optional_weather_fields_not_required(now: list[dt.datetime]) -> None:
    payload = copy.deepcopy(PAYLOAD)
    payload["properties"]["timeseries"][0]["data"]["instant"]["details"] = {}
    weather, _ = client(Response(payload=payload))
    await weather.fetch_data()
    assert weather.data == payload
