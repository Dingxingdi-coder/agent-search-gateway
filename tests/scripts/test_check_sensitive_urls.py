import pytest

from scripts.check_sensitive_urls import has_sensitive_userinfo, is_reserved_example_host


@pytest.mark.parametrize(
    "host",
    [
        "example.com",
        "sub.example.com",
        "example.net",
        "example.org",
        "service.example.test",
        "service.invalid",
        "service.example",
    ],
)
def test_reserved_example_hosts_are_allowed(host: str) -> None:
    assert is_reserved_example_host(host)


@pytest.mark.parametrize(
    "text",
    [
        "https://user:password@example.com/path",
        "prefix https://user:password@service.example.test/path suffix",
        "https://user@service.invalid/path",
        "https://example.com/path?next=user:password@example.com",
        "https://service.example.test/path",
    ],
)
def test_reserved_or_non_userinfo_urls_are_not_sensitive(text: str) -> None:
    assert not has_sensitive_userinfo(text)


@pytest.mark.parametrize(
    "text",
    [
        "https://user:password@internal.company/path",
        "prefix http://user@intranet.local/path suffix",
        "https://user:password@[broken-ipv6/path",
    ],
)
def test_non_example_userinfo_urls_are_sensitive(text: str) -> None:
    assert has_sensitive_userinfo(text)
