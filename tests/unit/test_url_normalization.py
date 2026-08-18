import pytest

from agent_search_gateway.errors import ErrorCode, InputFailure
from agent_search_gateway.url_normalization import NormalizedURL, normalize_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  https://EXAMPLE.COM/Path?Q=One#Frag  ", "https://example.com/Path?Q=One#Frag"),
        ("http://User:Pass@EXAMPLE.COM:8080/a", "http://User:Pass@example.com:8080/a"),
        ("https://EXAMPLE.COM", "https://example.com"),
        ("https://[2001:DB8::1]:8443/a", "https://[2001:db8::1]:8443/a"),
    ],
)
def test_normalize_url_enforces_http_contract(raw: str, expected: str) -> None:
    normalized = normalize_url(raw)
    assert normalized == expected
    assert isinstance(normalized, str)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "ftp://example.com/a",
        "mailto:user@example.com",
        "https:///missing",
        "https://example.com:bad/",
    ],
)
def test_normalize_url_rejects_invalid_values(raw: str) -> None:
    with pytest.raises(InputFailure) as caught:
        normalize_url(raw)
    assert caught.value.code is ErrorCode.INVALID_URL


def test_normalized_url_is_distinct_type_alias_at_type_checking_boundary() -> None:
    value: NormalizedURL = normalize_url("https://EXAMPLE.COM/a")
    assert value == "https://example.com/a"
