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
    assert await weather.fetch_data() is True
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


@pytest.mark.asyncio
async def test_strict_fetch_returns_true_for_download_and_cache(now: list[dt.datetime]) -> None:
    weather, session = client(Response())
    assert await weather.fetch_data() is True
    assert await weather.fetch_data() is True
    assert len(session.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["fetch_data", "fetching_data"])
async def test_deprecated_response_succeeds_and_warns_only_on_receipt(
    now: list[dt.datetime], caplog: pytest.LogCaptureFixture, method: str
) -> None:
    response = Response(203, headers={"Last-Modified": LAST_MODIFIED})
    weather, session = client(response, Response(304))
    fetch = getattr(weather, method)
    assert await fetch() is True
    assert weather.data == PAYLOAD
    assert weather.last_error is None
    assert weather.next_fetch_at == START + dt.timedelta(hours=3)
    assert response.closed
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"
    assert "deprecated" in caplog.records[0].message.lower()
    assert await fetch() is True
    assert len(session.requests) == 1
    assert len(caplog.records) == 1
    now[0] = weather.next_fetch_at
    assert await fetch() is True
    assert session.requests[1][1]["headers"] == {"If-Modified-Since": LAST_MODIFIED}
    assert len(caplog.records) == 1


@pytest.mark.asyncio
async def test_deprecated_response_is_validated_before_replacing_data(now: list[dt.datetime]) -> None:
    response = Response(203, payload={})
    weather, _ = client(Response(), response)
    await weather.fetch_data()
    original = weather.data
    now[0] = weather.next_fetch_at
    assert await weather.fetching_data() is False
    assert isinstance(weather.last_error, metno.MetNoPayloadError)
    assert weather.data is original
    assert response.closed


INSTANT_NUMERIC_FIELDS = (
    "air_temperature",
    "air_pressure_at_sea_level",
    "relative_humidity",
    "dew_point_temperature",
    "ultraviolet_index_clear_sky",
    "wind_speed",
    "wind_speed_of_gust",
    "wind_from_direction",
    "fog_area_fraction",
    "cloud_area_fraction",
    "cloud_area_fraction_low",
    "cloud_area_fraction_medium",
    "cloud_area_fraction_high",
)
PERIOD_NUMERIC_FIELDS = (
    "precipitation_amount",
    "precipitation_amount_max",
    "precipitation_amount_min",
    "probability_of_precipitation",
    "probability_of_thunder",
)
FORECAST_PERIODS = ("next_1_hours", "next_6_hours", "next_12_hours")
INVALID_WEATHER_DATA = (
    [
        pytest.param({period: value}, id=f"{period}-{name}")
        for period in FORECAST_PERIODS
        for name, value in (
            ("container-list", [1]),
            ("container-null", None),
            ("details-list", {"details": [1]}),
            ("details-null", {"details": None}),
            ("summary-list", {"summary": [1]}),
            ("summary-null", {"summary": None}),
            ("symbol-list", {"summary": {"symbol_code": ["rain"]}}),
            ("symbol-null", {"summary": {"symbol_code": None}}),
        )
    ]
    + [
        pytest.param(
            {"instant": {"details": {field: value}}}, id=f"instant-{field}-{name}"
        )
        for field in INSTANT_NUMERIC_FIELDS
        for name, value in (
            ("string", "4"),
            ("bool", True),
            ("nan", float("nan")),
            ("infinity", float("inf")),
            ("negative-infinity", float("-inf")),
            ("null", None),
        )
    ]
    + [
        pytest.param(
            {period: {"details": {field: value}}}, id=f"{period}-{field}-{name}"
        )
        for period in FORECAST_PERIODS
        for field in PERIOD_NUMERIC_FIELDS
        for name, value in (
            ("string", "4"),
            ("bool", False),
            ("nan", float("nan")),
            ("infinity", float("inf")),
            ("negative-infinity", float("-inf")),
            ("null", None),
        )
    ]
)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_data", INVALID_WEATHER_DATA)
async def test_invalid_weather_preserves_usable_cache_and_validator(
    now: list[dt.datetime], invalid_data: dict
) -> None:
    payload = copy.deepcopy(PAYLOAD)
    payload["properties"]["timeseries"][0]["data"].update(invalid_data)
    weather, session = client(
        Response(headers={"Last-Modified": LAST_MODIFIED}),
        Response(payload=payload, headers={"Last-Modified": "bad replacement"}),
        Response(304),
    )
    await weather.fetch_data()
    original = weather.data
    now[0] += dt.timedelta(hours=3)
    with pytest.raises(metno.MetNoPayloadError):
        await weather.fetch_data()
    assert weather.data is original
    assert isinstance(weather.last_error, metno.MetNoPayloadError)
    assert weather.get_weather(START, hourly=True)["temperature"] == 4.0
    assert weather.get_weather(START)["temperature"] == 4.0
    now[0] = weather.next_fetch_at
    await weather.fetch_data()
    assert session.requests[2][1]["headers"] == {"If-Modified-Since": LAST_MODIFIED}
    assert weather.data is original


@pytest.mark.asyncio
@pytest.mark.parametrize("period", FORECAST_PERIODS)
@pytest.mark.parametrize(
    "period_data",
    [
        pytest.param({}, id="omitted-sections"),
        pytest.param({"details": {}, "summary": {}}, id="omitted-values"),
        pytest.param(
            {"details": {"future_number": [1]}, "summary": {"future_symbol": {}}},
            id="unknown-values",
        ),
        pytest.param(
            {
                "details": dict.fromkeys(PERIOD_NUMERIC_FIELDS, 0),
                "summary": {"symbol_code": "unknown-symbol"},
                "future_section": [1],
            },
            id="numeric-zeros-and-unknown-symbol",
        ),
    ],
)
async def test_optional_weather_and_unknown_fields_remain_accepted(
    now: list[dt.datetime], period: str, period_data: dict
) -> None:
    payload = copy.deepcopy(PAYLOAD)
    data = payload["properties"]["timeseries"][0]["data"]
    data["instant"]["details"] = dict.fromkeys(INSTANT_NUMERIC_FIELDS, 0.0)
    data["instant"]["details"]["future_number"] = []
    data["future_period"] = []
    data[period] = period_data
    weather, _ = client(Response(payload=payload))
    assert await weather.fetch_data() is True
    assert weather.get_current_weather()["temperature"] == 0.0
    assert weather.get_weather(START)["temperature"] == 0.0


@pytest.fixture
def current_payload() -> dict:
    payload = copy.deepcopy(PAYLOAD)
    payload["properties"]["timeseries"] = [
        {
            "time": timestamp,
            "data": {
                "instant": {
                    "details": {"air_temperature": temperature, "wind_speed": wind}
                },
                "next_1_hours": {"summary": {"symbol_code": symbol}},
            },
        }
        for timestamp, temperature, wind, symbol in (
            ("2026-01-01T23:00:00Z", 1.0, 1.0, "clearsky_night"),
            ("2026-01-02T00:00:00Z", 2.0, 2.0, "rain"),
            ("2026-01-02T01:00:00Z", 3.0, 3.0, "snow"),
            ("2026-01-02T03:00:00Z", 4.0, 4.0, "cloudy"),
        )
    ]
    return payload


@pytest.mark.asyncio
async def test_cached_current_weather_advances_through_utc_midnight(
    now: list[dt.datetime], current_payload: dict
) -> None:
    now[0] = dt.datetime(2026, 1, 1, 23, 10, tzinfo=UTC)
    weather, session = client(Response(payload=current_payload))
    await weather.fetch_data()
    assert weather.get_current_weather()["temperature"] == 1.0
    now[0] = dt.datetime(2026, 1, 2, tzinfo=UTC)
    await weather.fetch_data()
    assert weather.get_current_weather()["temperature"] == 2.0
    assert weather.get_current_weather()["wind_speed"] == 7.2
    assert weather.get_current_weather()["condition"] == "rainy"
    now[0] = dt.datetime(2026, 1, 2, 0, 30, tzinfo=UTC)
    await weather.fetch_data()
    assert weather.get_current_weather()["temperature"] == 2.0
    now[0] = dt.datetime(2026, 1, 2, 1, 15, tzinfo=UTC)
    await weather.fetch_data()
    assert weather.get_current_weather()["temperature"] == 3.0
    assert weather.get_current_weather()["wind_speed"] == 10.8
    assert weather.get_current_weather()["condition"] == "snowy"
    assert len(session.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "expected"),
    [
        pytest.param(
            dt.datetime(2026, 1, 1, 22, 59, tzinfo=UTC),
            (1.0, 3.6, "clear-night"),
            id="before-first",
        ),
        pytest.param(
            dt.datetime(2026, 1, 2, tzinfo=UTC),
            (2.0, 7.2, "rainy"),
            id="exact-boundary",
        ),
        pytest.param(
            dt.datetime(2026, 1, 2, 0, 59, tzinfo=UTC),
            (2.0, 7.2, "rainy"),
            id="sub-hour",
        ),
        pytest.param(
            dt.datetime(2026, 1, 2, 2, 30, tzinfo=UTC),
            (3.0, 10.8, "snowy"),
            id="sparse-slot",
        ),
        pytest.param(
            dt.datetime(2026, 1, 2, 4, tzinfo=dt.timezone(dt.timedelta(hours=1))),
            (4.0, 14.4, "cloudy"),
            id="final-offset-boundary",
        ),
    ],
)
async def test_current_weather_selects_actual_forecast_slot(
    now: list[dt.datetime], current_payload: dict, current: dt.datetime, expected: tuple
) -> None:
    weather, _ = client(Response(payload=current_payload))
    await weather.fetch_data()
    now[0] = current
    result = weather.get_current_weather()
    assert (
        result["temperature"],
        result["wind_speed"],
        result["condition"],
    ) == expected


@pytest.mark.asyncio
async def test_current_weather_beyond_final_timestamp_is_empty(
    now: list[dt.datetime], current_payload: dict
) -> None:
    weather, _ = client(Response(payload=current_payload))
    await weather.fetch_data()
    now[0] = dt.datetime(2026, 1, 2, 3, 0, 0, 1, tzinfo=UTC)
    assert weather.get_current_weather() == {}


@pytest.mark.asyncio
async def test_timestamp_validation_without_ciso8601(
    now: list[dt.datetime], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(metno, "ciso8601", None)
    invalid = copy.deepcopy(PAYLOAD)
    invalid["properties"]["timeseries"][0]["time"] = "invalid"
    weather, _ = client(Response(), Response(payload=invalid))
    await weather.fetch_data()
    assert weather.get_current_weather()["temperature"] == 4.0
    now[0] += dt.timedelta(hours=3)
    with pytest.raises(metno.MetNoPayloadError):
        await weather.fetch_data()
    assert weather.get_weather(START, hourly=True)["temperature"] == 4.0


@pytest.mark.asyncio
async def test_out_of_order_timestamps_preserve_cached_forecast(
    now: list[dt.datetime], current_payload: dict
) -> None:
    invalid = copy.deepcopy(current_payload)
    timeseries = invalid["properties"]["timeseries"]
    timeseries[1], timeseries[2] = timeseries[2], timeseries[1]
    weather, session = client(
        Response(payload=current_payload, headers={"Last-Modified": LAST_MODIFIED}),
        Response(payload=invalid, headers={"Last-Modified": "new"}),
        Response(304),
    )
    await weather.fetch_data()
    original = weather.data
    now[0] = weather.next_fetch_at
    with pytest.raises(metno.MetNoPayloadError):
        await weather.fetch_data()
    assert weather.data is original
    now[0] = weather.next_fetch_at
    assert await weather.fetch_data() is True
    assert session.requests[-1][1]["headers"] == {"If-Modified-Since": LAST_MODIFIED}


@pytest.mark.asyncio
async def test_cooldown_does_not_accumulate_exception_tracebacks(
    now: list[dt.datetime],
) -> None:
    weather, session = client(Response(503, headers={"Retry-After": "18000"}))
    assert await weather.fetching_data() is False
    error = weather.last_error

    def traceback_depth() -> int:
        depth = 0
        traceback = error.__traceback__
        while traceback is not None:
            depth += 1
            traceback = traceback.tb_next
        return depth

    assert await weather.fetching_data() is False
    depth = traceback_depth()
    for _ in range(20):
        assert await weather.fetching_data() is False
    assert weather.last_error is error
    assert traceback_depth() == depth
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_retry_after_with_automatic_status_raising(
    now: list[dt.datetime],
) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def unavailable(request: web.Request) -> web.Response:
        return web.Response(status=503, headers={"Retry-After": "18000"})

    app = web.Application()
    app.router.add_get("/", unavailable)
    async with TestServer(app) as server:
        async with aiohttp.ClientSession(raise_for_status=True) as session:
            weather = metno.MetWeatherData(
                {"lat": "60", "lon": "10"}, session, api_url=str(server.make_url("/"))
            )
            with pytest.raises(metno.MetNoHttpError) as exc:
                await weather.fetch_data()
            assert exc.value.status == 503
            assert weather.next_fetch_at == START + dt.timedelta(hours=5)


@pytest.mark.asyncio
async def test_backoff_retains_jitter_at_cap(
    now: list[dt.datetime], monkeypatch: pytest.MonkeyPatch
) -> None:
    weather, _ = client(*(Response(503) for _ in range(8)))
    for _ in range(7):
        assert await weather.fetching_data() is False
        now[0] = weather.next_fetch_at
    monkeypatch.setattr("random.uniform", lambda a, b: a)
    assert await weather.fetching_data() is False
    assert 3200 < (weather.next_fetch_at - now[0]).total_seconds() < 3600
