"""Built-in first-version web provider registrations."""

from .contracts import ProviderCapabilities
from .registry import ProviderRegistry, WebProviderRegistration
from .web.anysearch import AnySearchAdapter
from .web.brave import BraveAdapter
from .web.exa import ExaAdapter
from .web.firecrawl import FirecrawlAdapter
from .web.linkup import LinkupAdapter
from .web.parallel import ParallelAdapter
from .web.tavily import TavilyAdapter
from .web.tinyfish import TinyFishAdapter


def build_default_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registrations = (
        WebProviderRegistration(
            "tavily",
            ProviderCapabilities(search=True, fetch=True),
            TavilyAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "firecrawl",
            ProviderCapabilities(search=True, fetch=True),
            FirecrawlAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "exa",
            ProviderCapabilities(search=True, fetch=True),
            ExaAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "linkup",
            ProviderCapabilities(search=True, fetch=True),
            LinkupAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "brave",
            ProviderCapabilities(search=True, fetch=False),
            BraveAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "anysearch",
            ProviderCapabilities(search=True, fetch=False),
            AnySearchAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "tinyfish",
            ProviderCapabilities(search=True, fetch=True),
            TinyFishAdapter,
            frozenset({"search_api_url", "fetch_api_url"}),
        ),
        WebProviderRegistration(
            "parallel",
            ProviderCapabilities(search=True, fetch=True),
            ParallelAdapter,
            frozenset(
                {"api_url", "mode", "search_fetch_policy", "extract_fetch_policy"}
            ),
        ),
    )
    for registration in registrations:
        registry.register(registration)
    return registry
