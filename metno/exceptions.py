"""Typed failures from the Locationforecast client."""


class MetNoError(Exception):
    """Base exception for a failed forecast fetch."""


class MetNoHttpError(MetNoError):
    """The server returned an unsuccessful HTTP status."""

    def __init__(self, status):
        self.status = status
        super().__init__("Locationforecast returned HTTP {}".format(status))


class MetNoPayloadError(MetNoError):
    """The response does not contain a usable forecast representation."""


class MetNoTimeoutError(MetNoError):
    """Response acquisition or body reading exceeded the timeout."""


class MetNoTransportError(MetNoError):
    """The forecast request failed at the transport layer."""
