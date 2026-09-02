import pytest

import scripts.check_sensitive_urls as sensitive_urls
from scripts.check_sensitive_urls import (
    has_sensitive_userinfo,
    is_reserved_example_host,
    without_scanner_self_test_urls,
)


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
        "https" + "://user:password@internal.company/path",
        "prefix " + "http" + "://user@intranet.local/path suffix",
        "https" + "://user:password@[broken-ipv6/path",
    ],
)
def test_non_example_userinfo_urls_are_sensitive(text: str) -> None:
    assert has_sensitive_userinfo(text)


def test_scanner_self_test_exemption_is_exact_and_path_scoped() -> None:
    sentinel = "https" + "://user:password@internal.company/path"
    other = "https" + "://user:password@private.internal/path"
    test_path = "tests/scripts/test_check_sensitive_urls.py"

    assert not has_sensitive_userinfo(without_scanner_self_test_urls(test_path, sentinel))
    assert has_sensitive_userinfo(without_scanner_self_test_urls(test_path, f"{sentinel} {other}"))
    assert has_sensitive_userinfo(without_scanner_self_test_urls("tests/other.py", sentinel))


def test_unreachable_force_push_base_falls_back_to_head_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_revision_exists(revision: str) -> bool:
        return revision == "new-head"

    def fake_run_git(*arguments: str, allow_no_matches: bool = False) -> str:
        del allow_no_matches
        calls.append(arguments)
        assert arguments == ("rev-list", "new-head")
        return "new-head\nparent\n"

    monkeypatch.setattr(sensitive_urls, "revision_exists", fake_revision_exists)
    monkeypatch.setattr(sensitive_urls, "run_git", fake_run_git)

    assert sensitive_urls.commit_snapshots(
        base="unreachable-before",
        head="new-head",
        all_history=False,
    ) == ("new-head", "parent")
    assert calls == [("rev-list", "new-head")]


def test_reachable_event_range_keeps_incremental_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run_git(*arguments: str, allow_no_matches: bool = False) -> str:
        del allow_no_matches
        calls.append(arguments)
        assert arguments == ("rev-list", "--reverse", "base..head")
        return "commit-1\ncommit-2\n"

    monkeypatch.setattr(sensitive_urls, "revision_exists", lambda _revision: True)
    monkeypatch.setattr(sensitive_urls, "run_git", fake_run_git)

    assert sensitive_urls.commit_snapshots(
        base="base",
        head="head",
        all_history=False,
    ) == ("commit-1", "commit-2")
    assert calls == [("rev-list", "--reverse", "base..head")]
