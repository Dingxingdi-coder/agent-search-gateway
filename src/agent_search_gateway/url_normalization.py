"""Stable URL normalization used for state and concurrency keys."""

from typing import NewType
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .errors import ErrorCode, InputFailure

NormalizedURL = NewType("NormalizedURL", str)


def _invalid_url() -> InputFailure:
    return InputFailure(ErrorCode.INVALID_URL, "URL must be a valid HTTP or HTTPS URL")


def _lowercase_host(parsed: SplitResult) -> str:
    netloc = parsed.netloc
    userinfo, separator, hostport = netloc.rpartition("@")
    prefix = f"{userinfo}@" if separator else ""

    if hostport.startswith("["):
        closing = hostport.find("]")
        if closing < 0:
            raise _invalid_url()
        host = hostport[1:closing].lower()
        return f"{prefix}[{host}]{hostport[closing + 1 :]}"

    host, port_separator, port = hostport.rpartition(":")
    if port_separator:
        return f"{prefix}{host.lower()}:{port}"
    return f"{prefix}{hostport.lower()}"


def normalize_url(value: str) -> NormalizedURL:
    stripped = value.strip()
    if not stripped:
        raise _invalid_url()

    try:
        parsed = urlsplit(stripped)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise _invalid_url()
        _ = parsed.port
        netloc = _lowercase_host(parsed)
    except (ValueError, UnicodeError) as exc:
        raise _invalid_url() from exc

    return NormalizedURL(
        urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    )
