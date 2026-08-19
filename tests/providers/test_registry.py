from agent_search_gateway.providers.contracts import (
    KeywordSearchHit,
    ProviderCapabilities,
    URLFetchCandidate,
)
from agent_search_gateway.providers.registry import ProviderRegistry, WebProviderRegistration
from tests.support.fakes import FakeKeywordSearchProvider, FakeURLFetchProvider


def _factory() -> object:
    return object()


def test_registry_exposes_exact_capabilities_and_contract_types() -> None:
    registry = ProviderRegistry()
    search_only = WebProviderRegistration(
        name="search",
        capabilities=ProviderCapabilities(search=True, fetch=False),
        factory=_factory,
        allowed_config_keys=frozenset({"api_url"}),
    )
    fetch_only = WebProviderRegistration(
        name="fetch",
        capabilities=ProviderCapabilities(search=False, fetch=True),
        factory=_factory,
        allowed_config_keys=frozenset(),
    )
    dual = WebProviderRegistration(
        name="dual",
        capabilities=ProviderCapabilities(search=True, fetch=True),
        factory=_factory,
        allowed_config_keys=frozenset({"api_url"}),
    )
    for registration in (search_only, fetch_only, dual):
        registry.register(registration)

    assert registry.capabilities("dual") == ProviderCapabilities(search=True, fetch=True)
    assert [item.name for item in registry.for_stage("search")] == ["search", "dual"]
    assert [item.name for item in registry.for_stage("fetch")] == ["fetch", "dual"]
    assert [item.name for item in registry.list_in_registration_order()] == [
        "search",
        "fetch",
        "dual",
    ]

    hit = KeywordSearchHit("https://example.com", "title", "snippet", "raw", "clean")
    candidate = URLFetchCandidate("raw", "clean")
    assert hit.url == "https://example.com"
    assert candidate.raw_content == "raw"

    search_fake = FakeKeywordSearchProvider("fake-search", [hit])
    fetch_fake = FakeURLFetchProvider("fake-fetch", candidate)
    assert search_fake.calls == []
    assert fetch_fake.calls == []

    assert "URLStore" not in str(search_fake.search.__annotations__)
    assert "URLStore" not in str(fetch_fake.fetch.__annotations__)
